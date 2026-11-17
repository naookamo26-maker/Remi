// 右ペイン: 会話ビューと入力欄(設計書 §5.2, §6、モック「メインウィンドウ」右)
import { call } from "./api.js";
import { clear, debounce, h, html, toast } from "./dom.js";
import { dayKey, formatDateHeading, formatSize, formatTime, t } from "./i18n.js";
import { icon } from "./icons.js";
import { lightbox } from "./ui.js";

const PAGE = 50;
const AVATAR_COLORS = ["#FCE7D6", "#E0E7FF", "#DCFCE7", "#FDE68A", "#FBCFE8", "#CFFAFE", "#E9D5FF", "#FED7AA"];

function avatar(name, id) {
  const label = (name || "?").trim().charAt(0);
  const color = AVATAR_COLORS[Math.abs(Number(id) || [...(name || "")].reduce((a, c) => a + c.charCodeAt(0), 0)) % AVATAR_COLORS.length];
  return h("div", { class: "avatar", "aria-hidden": "true", style: { background: color } }, label);
}

export class Conversation {
  constructor(app) {
    this.app = app;
    this.data = null;
    this.limit = PAGE;
    this.sending = false;
    this.root = h("section", { class: "pane pane-conv", "aria-label": t("conv.label") });
    this.showEmpty();
  }

  showEmpty() {
    this.data = null;
    clear(this.root).append(h("div", { class: "welcome" }, h("p", { class: "muted" }, t("conv.selectIssue"))));
  }

  showWelcome() {
    this.data = null;
    clear(this.root).append(h("div", { class: "welcome" },
      h("h2", {}, t("welcome.title")),
      h("p", { class: "muted" }, t("welcome.body")),
      h("button", { type: "button", class: "btn primary", onClick: () => this.app.openServerDialog() }, icon("plus"), t("welcome.addServer"))));
  }

  // 開く(open=true)と既読にする。届いた更新で描き直すときは markRead を app が判断する
  async load(serverId, issueId, { markRead = true, keepScroll = false, resetLimit = false } = {}) {
    if (resetLimit) this.limit = PAGE;
    let data;
    try {
      data = await call("getConversation", { serverId, issueId, limit: this.limit, markRead });
    } catch (e) {
      if (e.code === "not_found") return this.showEmpty();
      throw e;
    }
    const cur = this.app.current;
    if (!cur || cur.serverId !== serverId || cur.issueId !== issueId) return; // 別のチケットに移った
    const sameIssue = this.data && this.data.serverId === serverId && this.data.issueId === issueId;
    const timeline = this.timeline;
    const atBottom = timeline && timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 40;
    const prevScroll = timeline ? timeline.scrollTop : 0;
    const prevHeight = timeline ? timeline.scrollHeight : 0;
    this.data = data;
    this.render(sameIssue);
    if (sameIssue && keepScroll) {
      // 過去分を読み込んだ: 表示位置を保つ
      this.timeline.scrollTop = this.timeline.scrollHeight - prevHeight + prevScroll;
    } else if (!sameIssue || atBottom) {
      const firstUnread = this.timeline.querySelector(".unread-sep");
      if (!sameIssue && firstUnread) firstUnread.scrollIntoView({ block: "start" });
      else this.timeline.scrollTop = this.timeline.scrollHeight;
    } else {
      this.timeline.scrollTop = prevScroll;
    }
    this.stickBottom = this.isNearBottom();
  }

  isNearBottom() {
    const tl = this.timeline;
    return !!tl && tl.scrollHeight - tl.scrollTop - tl.clientHeight < 40;
  }

  render(sameIssue) {
    const d = this.data;
    const composerState = sameIssue && this.textarea ? { value: this.textarea.value, private: this.privateBox.checked } : null;
    clear(this.root);
    this.root.append(this.header(d));
    this.timeline = h("div", { class: "timeline", role: "log", "aria-label": t("conv.timeline"),
      onClick: (e) => this.onTimelineClick(e),
      onScroll: () => { this.stickBottom = this.isNearBottom(); } });
    // 画像の読み込みで高さが変わっても、最下部を見ていたら最下部を保つ
    this.timeline.addEventListener("load", (e) => {
      if (e.target.tagName === "IMG" && this.stickBottom) this.timeline.scrollTop = this.timeline.scrollHeight;
    }, true);
    this.root.append(this.timeline, this.composer(d, composerState));
    this.fillTimeline(d);
  }

  header(d) {
    const meta = [
      [t("field.status"), d.status, d.closed ? "closed" : ""],
      [t("field.assignee"), d.assignee || t("conv.none")],
      [t("field.priority"), d.priority],
      [t("conv.updated"), formatTime(d.updatedOn)],
    ].filter((m) => m[1]);
    return h("header", { class: "conv-header" },
      h("div", { class: "top" },
        h("div", { class: "titles" },
          h("div", { class: "crumb" }, [d.serverName, ...d.breadcrumb].join(" › "), " · ", `${d.tracker} #${d.issueId}`),
          h("h1", {}, d.subject)),
        h("button", { type: "button", class: "btn", onClick: () => call("openExternal", { url: d.url }) },
          icon("external"), t("conv.openInBrowser"))),
      h("div", { class: "meta" }, meta.map(([k, v, cls]) => h("span", {}, `${k} `, h("b", { class: cls || null }, v)))));
  }

  fillTimeline(d) {
    const tl = this.timeline;
    if (d.hasMore) {
      tl.append(h("div", { class: "system" }, h("button", { type: "button", class: "btn", onClick: () => {
        this.limit += PAGE;
        this.load(d.serverId, d.issueId, { markRead: false, keepScroll: true });
      } }, t("conv.loadMore", { count: d.total - d.journals.length }))));
    }
    let lastDay = "";
    const day = (iso) => {
      const k = dayKey(iso);
      if (k && k !== lastDay) {
        lastDay = k;
        tl.append(h("div", { class: "date-sep", role: "separator" }, formatDateHeading(iso)));
      }
    };
    if (!d.hasMore && d.descriptionHtml) {
      day(d.createdOn);
      tl.append(this.message({
        userName: d.author, userId: d.authorId, mine: d.authorId === d.myUserId, html: d.descriptionHtml,
        createdOn: d.createdOn, label: t("conv.description"), cls: "description",
      }));
    }
    let unreadShown = false;
    const firstNote = d.total - d.journals.length + 1;
    d.journals.forEach((j, i) => {
      day(j.createdOn);
      if (j.unread && !unreadShown) {
        unreadShown = true;
        tl.append(h("div", { class: "unread-sep", role: "separator" }, t("conv.unreadFrom")));
      }
      const noteNo = firstNote + i;
      for (const det of j.details) tl.append(this.systemLine(j, det));
      if (j.html) tl.append(this.message({ ...j, noteNo }));
    });
    if (!d.journalsSynced) tl.append(h("div", { class: "system" }, t("conv.loading")));
    if (this.sending) tl.append(this.pendingMessage());
  }

  message(m) {
    const cls = ["msg", m.mine ? "mine" : "", m.private ? "private" : "", m.cls || ""].filter(Boolean).join(" ");
    const who = h("div", { class: "who" },
      m.mine ? h("b", {}, t("conv.you")) : h("b", {}, m.userName || "?"),
      m.label ? ` · ${m.label}` : "", ` · ${formatTime(m.createdOn)}`,
      m.private ? h("span", { class: "private-badge" }, icon("lock"), t("conv.private")) : null,
      m.noteNo ? h("span", { class: "muted" }, ` #${m.noteNo}`) : null);
    const bubble = html(h("div", { class: "bubble content" }), m.html);
    return h("article", { class: cls, id: m.noteNo ? `note-${m.noteNo}` : null },
      m.mine ? null : avatar(m.userName, m.userId), h("div", { class: "body" }, who, bubble));
  }

  pendingMessage() {
    return h("article", { class: "msg mine pending" },
      h("div", { class: "body" }, h("div", { class: "who" }, t("conv.sending")),
        h("div", { class: "bubble" }, this.textarea?.value || "")));
  }

  systemLine(j, det) {
    const who = j.mine ? t("conv.you") : (j.userName || "?");
    const line = h("div", { class: "system" }, `${who}: ${det.text} · ${formatTime(j.createdOn)}`);
    if (det.kind === "attachment" && det.url) {
      if (det.isImage) {
        line.append(h("a", { href: det.url, class: "attachment image" },
          h("img", { class: "att-thumb", src: det.url, alt: det.filename, loading: "lazy" })));
      } else {
        line.append(" ", h("a", { href: det.url, class: "attachment", dataset: { name: det.filename } },
          t("conv.openFile")), det.filesize ? ` (${formatSize(det.filesize)})` : "");
      }
    }
    return line;
  }

  onTimelineClick(e) {
    const a = e.target.closest("a");
    if (!a) return;
    e.preventDefault();
    const href = a.getAttribute("href") || "";
    const d = this.data;
    let m;
    if ((m = href.match(/^remi:\/\/issue\/(\d+)(?:#note-(\d+))?/))) {
      this.app.openIssueOrBrowser(d.serverId, Number(m[1]), m[2] ? Number(m[2]) : null);
    } else if ((m = href.match(/^remi:\/\/note\/(\d+)/))) {
      this.scrollToNote(Number(m[1]));
    } else if (href.startsWith("/attachments/")) {
      const img = a.querySelector("img");
      if (img) lightbox(href, img.getAttribute("alt"));
      else call("openAttachment", { serverId: d.serverId, attachmentId: Number(href.split("/").pop()) });
    } else if (/^https?:\/\//i.test(href)) {
      call("openExternal", { url: href });
    }
  }

  scrollToNote(n) {
    const el = this.timeline?.querySelector(`#note-${n}`);
    if (!el) return toast(t("conv.noteNotLoaded"));
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    el.classList.add("highlight");
    setTimeout(() => el.classList.remove("highlight"), 2000);
  }

  // --- 入力欄 -------------------------------------------------------------------
  composer(d, state) {
    const draft = state ?? { value: d.draft.body, private: d.draft.private };
    this.textarea = h("textarea", { "aria-label": t("conv.comment"), placeholder: t("conv.placeholder"), rows: "3",
      onInput: () => { this.saveDraft(); this.updateSend(); },
      onKeydown: (e) => this.onComposerKey(e) });
    this.textarea.value = draft.value || "";
    this.privateBox = h("input", { type: "checkbox", onChange: () => this.saveDraft() });
    this.privateBox.checked = !!draft.private;
    this.preview = h("div", { class: "preview content", hidden: true });
    this.sendButton = h("button", { type: "button", class: "btn primary", onClick: () => this.send() }, icon("send"), t("conv.send"));
    this.error = h("div", { class: "error", role: "alert" });
    if (d.draft.failed && !state) this.showFailed();
    const tab = (label, selected, onClick) => h("button", { type: "button", role: "tab", "aria-selected": String(selected), onClick }, label);
    const editTab = tab(t("conv.edit"), true, () => this.setPreview(false, editTab, previewTab));
    const previewTab = tab(t("conv.preview"), false, () => this.setPreview(true, editTab, previewTab));
    this.saveDraft = debounce(() => {
      const cur = this.data;
      if (cur) call("saveDraft", { serverId: cur.serverId, issueId: cur.issueId, body: this.textarea.value, private: this.privateBox.checked });
    }, 600);
    const fmt = { common_mark: "Markdown", textile: "Textile", plain: t("conv.plainText") }[d.textFormat] || d.textFormat;
    const el = h("div", { class: "composer" },
      h("div", { class: "tabs", role: "tablist", "aria-label": t("conv.inputMode") }, editTab, previewTab,
        h("span", { class: "spacer" }), h("span", { class: "fmt" }, t("conv.format", { format: fmt }))),
      this.textarea, this.preview, this.error,
      h("div", { class: "actions" },
        h("label", {}, this.privateBox, t("conv.sendPrivate")),
        h("span", { class: "muted" }, t("conv.sendHint")),
        h("span", { class: "spacer" }),
        this.sendButton));
    this.updateSend();
    return el;
  }

  updateSend() {
    if (this.sendButton) this.sendButton.disabled = this.sending || !this.textarea.value.trim();
  }

  async setPreview(on, editTab, previewTab) {
    editTab.setAttribute("aria-selected", String(!on));
    previewTab.setAttribute("aria-selected", String(on));
    this.textarea.hidden = on;
    this.preview.hidden = !on;
    if (on) {
      const d = this.data;
      const out = await call("preview", { serverId: d.serverId, issueId: d.issueId, text: this.textarea.value });
      html(this.preview, out || "");
      if (!out) this.preview.append(h("span", { class: "muted" }, t("conv.nothingToPreview")));
    } else {
      this.textarea.focus();
    }
  }

  onComposerKey(e) {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      this.send();
    } else if (e.key === "Tab" && !e.shiftKey && !e.ctrlKey && !e.altKey) {
      // Tabキーでインデントを入力できるようにする(設計書 §5.3)
      e.preventDefault();
      document.execCommand("insertText", false, "    ");
    }
  }

  async send() {
    const d = this.data;
    const body = this.textarea.value;
    if (!d || this.sending || !body.trim()) return;
    this.sending = true;
    this.error.textContent = "";
    this.updateSend();
    this.timeline.append(this.pendingMessage());
    this.timeline.scrollTop = this.timeline.scrollHeight;
    try {
      await call("postNote", { serverId: d.serverId, issueId: d.issueId, body, private: this.privateBox.checked });
    } catch (e) {
      this.noteFinished({ serverId: d.serverId, issueId: d.issueId, error: e.code, detail: e.detail });
    }
  }

  // バックエンドから送信結果が届いた
  noteFinished(ev) {
    const d = this.data;
    if (!d || ev.serverId !== d.serverId || ev.issueId !== d.issueId) return;
    this.sending = false;
    this.timeline.querySelector(".msg.pending")?.remove();
    if (ev.error) {
      this.showFailed(ev.error);
    } else {
      this.textarea.value = "";
      this.error.textContent = "";
    }
    this.updateSend();
  }

  showFailed(code = null) {
    clear(this.error).append(
      t("conv.sendFailed"), code ? ` (${t(`errors.${code}`)})` : "", " ",
      h("button", { type: "button", class: "btn", onClick: () => this.send() }, t("conv.retry")));
  }
}
