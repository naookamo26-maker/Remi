// 右ペイン: 会話ビューと入力欄(設計書 §5.2, §6、モック「メインウィンドウ」右)
import { call } from "./api.js";
import { clear, debounce, h, html, toast } from "./dom.js";
import { dayKey, formatDateHeading, formatSize, formatTime, t } from "./i18n.js";
import { icon } from "./icons.js";
import { contextMenu, lightbox } from "./ui.js";

const PAGE = 50;
// ヘッダで変更できる項目と、その表示名のキー
const FIELD_LABELS = { status_id: "field.status", assigned_to_id: "field.assignee", priority_id: "field.priority" };
const AVATAR_COLORS = ["#FCE7D6", "#E0E7FF", "#DCFCE7", "#FDE68A", "#FBCFE8", "#CFFAFE", "#E9D5FF", "#FED7AA"];

const issueKey = (d) => (d ? `${d.serverId}:${d.issueId}` : "");

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
    // 送信中のチケット(issueKey → 送った本文)。送信中に別のチケットへ移っても状態が混ざらないようにする
    this.inFlight = new Map();
    this.composerEl = null;
    this.changes = {}; // ヘッダで選んだ、送信前の変更 {status_id: {value, label, from}}
    this.uploads = []; // 送信前の添付 [{key, id, token, filename, contentType, isImage, url, state}]
    this.editOptions = null;
    this.root = h("section", { class: "pane pane-conv", "aria-label": t("conv.label") });
    this.showEmpty();
  }

  get sending() {
    return this.inFlight.has(issueKey(this.data));
  }

  showEmpty() {
    this.data = null;
    this.composerEl = null;
    clear(this.root).append(h("div", { class: "welcome" }, h("p", { class: "muted" }, t("conv.selectIssue"))));
  }

  showWelcome() {
    this.data = null;
    this.composerEl = null;
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
    if (!sameIssue) {
      this.changes = {};
      this.uploads = [];
      this.editOptions = null;
    }
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
    if (!this.editOptions) this.loadEditOptions(serverId, issueId);
  }

  // ヘッダで変更できる項目の選択肢を、表示とは別に読み込む(Redmineへの問い合わせを伴うため)
  async loadEditOptions(serverId, issueId) {
    try {
      const opts = await call("getEditOptions", { serverId, issueId });
      const d = this.data;
      if (!d || d.serverId !== serverId || d.issueId !== issueId) return;
      this.editOptions = opts;
      this.fillFields();
    } catch (e) {
      for (const sel of this.root.querySelectorAll(".fields select")) sel.title = t(`errors.${e.code}`);
    }
  }

  isNearBottom() {
    const tl = this.timeline;
    return !!tl && tl.scrollHeight - tl.scrollTop - tl.clientHeight < 40;
  }

  render(sameIssue) {
    const d = this.data;
    const header = this.header(d);
    const timeline = h("div", { class: "timeline", role: "log", "aria-label": t("conv.timeline"),
      onClick: (e) => this.onTimelineClick(e),
      onScroll: () => { this.stickBottom = this.isNearBottom(); } });
    // 画像の読み込みで高さが変わっても、最下部を見ていたら最下部を保つ
    timeline.addEventListener("load", (e) => {
      if (e.target.tagName === "IMG" && this.stickBottom) timeline.scrollTop = timeline.scrollHeight;
    }, true);
    if (sameIssue && this.composerEl?.isConnected) {
      // 同じチケットの更新では入力欄を作り直さない(入力中のフォーカス・カーソル位置・IMEの変換・
      // 元に戻す履歴・プレビューの状態を保つ)
      this.headerEl.replaceWith(header);
      this.timeline.replaceWith(timeline);
    } else {
      this.composerEl = this.composer(d);
      clear(this.root).append(header, timeline, this.composerEl);
    }
    this.headerEl = header;
    this.timeline = timeline;
    this.fillTimeline(d);
    this.updateSend();
  }

  header(d) {
    const openBtn = h("button", { type: "button", class: "btn", title: t("conv.openRedmineHint"),
      onClick: (e) => this.app.openRedmine(d.url, { external: e.shiftKey }) }, icon("external"), t("conv.openRedmine"));
    const moreBtn = h("button", { type: "button", class: "btn", "aria-label": t("conv.moreActions"),
      onClick: (e) => {
        const r = e.currentTarget.getBoundingClientRect();
        contextMenu(r.left, r.bottom + 4, [
          { label: t("conv.openInBrowser"), onClick: () => this.app.openRedmine(d.url, { external: true }) },
          { label: t("conv.copyUrl"), onClick: () => navigator.clipboard.writeText(d.url).then(() => toast(t("conv.copied"))) },
        ]);
      } }, "▾");
    const watchBtn = h("button", { type: "button", class: `btn${d.watching ? " on" : ""}`, "aria-pressed": String(d.watching),
      title: d.watching ? t("conv.unwatchHint") : t("conv.watchHint"),
      onClick: () => {
        watchBtn.disabled = true;
        call("setWatching", { serverId: d.serverId, issueId: d.issueId, watching: !d.watching })
          .catch((e) => { watchBtn.disabled = false; toast(t(`errors.${e.code}`)); });
      } }, d.watching ? t("conv.watching") : t("conv.watch"));
    this.selects = {
      status_id: this.fieldSelect("status_id", d.status, d.statusId),
      assigned_to_id: this.fieldSelect("assigned_to_id", d.assignee || t("conv.none"), d.assignedToId),
      priority_id: this.fieldSelect("priority_id", d.priority, d.priorityId),
    };
    const field = (label, sel) => h("label", {}, label, sel);
    const rel = (d.relations || []).map((r) => t(`list.${r === "group" ? "group" : r}`)).join(" / ");
    const el = h("header", { class: "conv-header" },
      h("div", { class: "top" },
        h("div", { class: "titles" },
          h("div", { class: "crumb", title: [d.serverName, ...d.breadcrumb].join(" › ") },
            [d.serverName, ...d.breadcrumb].join(" › "), " · ", `${d.tracker} #${d.issueId}`),
          h("h1", {}, d.subject)),
        h("div", { class: "actions" }, watchBtn, h("span", { class: "split" }, openBtn, moreBtn))),
      h("div", { class: "fields" },
        field(t("field.status"), this.selects.status_id),
        field(t("field.assignee"), this.selects.assigned_to_id),
        field(t("field.priority"), this.selects.priority_id),
        h("span", { class: "static" }, `${t("conv.updated")} `, h("b", {}, formatTime(d.updatedOn))),
        rel ? h("span", { class: "static" }, `${t("conv.yourRelation")} `, h("b", {}, rel)) : null));
    if (this.editOptions) setTimeout(() => this.fillFields(), 0);
    return el;
  }

  // 選択肢が読み込まれるまでは、今の値だけを表示して操作できないようにしておく
  fieldSelect(key, label, value) {
    const sel = h("select", { disabled: true, "aria-label": t(FIELD_LABELS[key]),
      onChange: () => this.onFieldChange(key, sel) });
    sel.append(h("option", { value: value ?? "" }, label || "-"));
    sel.dataset.current = value ?? "";
    return sel;
  }

  fillFields() {
    const o = this.editOptions;
    if (!o || !this.selects) return;
    const lists = {
      status_id: o.statuses.map((x) => [x.id, x.name]),
      assigned_to_id: [["", t("conv.none")], ...(o.me ? [[o.me, t("conv.assignToMe")]] : []),
        ...o.assignees.filter((a) => a.id !== o.me).map((a) => [a.id, a.group ? `${a.name} ${t("conv.groupSuffix")}` : a.name])],
      priority_id: o.priorities.map((x) => [x.id, x.name]),
    };
    for (const [key, sel] of Object.entries(this.selects)) {
      const current = o.current[key] ?? "";
      const chosen = this.changes[key] ? this.changes[key].value : current;
      clear(sel);
      for (const [v, label] of lists[key]) sel.append(h("option", { value: v, selected: String(v) === String(chosen ?? "") }, label));
      sel.dataset.current = current;
      sel.disabled = false;
      sel.classList.toggle("changed", !!this.changes[key]);
    }
  }

  onFieldChange(key, sel) {
    const value = sel.value === "" ? null : Number(sel.value);
    const current = sel.dataset.current === "" ? null : Number(sel.dataset.current);
    if (value === current) delete this.changes[key];
    else {
      const fromOpt = [...sel.options].find((o) => String(o.value) === String(current ?? ""));
      this.changes[key] = { value, label: sel.selectedOptions[0].textContent, from: fromOpt ? fromOpt.textContent : "-" };
    }
    sel.classList.toggle("changed", !!this.changes[key]);
    this.renderChips();
    this.updateSend();
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
      const server = this.data.url.replace(/\/issues\/\d+$/, "");
      if (href.startsWith(server)) this.app.openRedmine(href, { external: e.shiftKey });
      else call("openExternal", { url: href });
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
  composer(d) {
    const draft = { value: d.draft.body, private: d.draft.private };
    this.textarea = h("textarea", { "aria-label": t("conv.comment"), placeholder: t("conv.placeholder"), rows: "3",
      onInput: () => { this.saveDraft(); this.updateSend(); },
      onKeydown: (e) => this.onComposerKey(e),
      onPaste: (e) => this.onPaste(e) });
    this.textarea.value = draft.value || "";
    this.privateBox = h("input", { type: "checkbox", onChange: () => this.saveDraft() });
    this.privateBox.checked = !!draft.private;
    this.preview = h("div", { class: "preview content", hidden: true });
    this.chips = h("div", { class: "chips", "aria-live": "polite" });
    this.fileInput = h("input", { type: "file", multiple: true, hidden: true,
      onChange: () => { this.addFiles([...this.fileInput.files], false); this.fileInput.value = ""; } });
    this.sendButton = h("button", { type: "button", class: "btn primary", onClick: () => this.send() }, icon("send"), t("conv.send"));
    this.error = h("div", { class: "error", role: "alert" });
    if (d.draft.failed) this.showFailed();
    const tab = (label, selected, onClick) => h("button", { type: "button", role: "tab", "aria-selected": String(selected), onClick }, label);
    const editTab = tab(t("conv.edit"), true, () => this.setPreview(false, editTab, previewTab));
    const previewTab = tab(t("conv.preview"), false, () => this.setPreview(true, editTab, previewTab));
    this.saveDraft = debounce(() => {
      const cur = this.data;
      if (cur) call("saveDraft", { serverId: cur.serverId, issueId: cur.issueId, body: this.textarea.value, private: this.privateBox.checked });
    }, 600);
    const fmt = { common_mark: "Markdown", textile: "Textile", plain: t("conv.plainText") }[d.textFormat] || d.textFormat;
    const el = h("div", { class: "composer",
      onDragover: (e) => { if (e.dataTransfer?.types?.includes("Files")) { e.preventDefault(); el.classList.add("dragging"); } },
      onDragleave: () => el.classList.remove("dragging"),
      onDrop: (e) => {
        el.classList.remove("dragging");
        if (e.dataTransfer?.files?.length) {
          e.preventDefault();
          this.addFiles([...e.dataTransfer.files], true);
        }
      } },
      h("div", { class: "tabs", role: "tablist", "aria-label": t("conv.inputMode") }, editTab, previewTab,
        h("span", { class: "spacer" }), h("span", { class: "fmt" }, t("conv.format", { format: fmt }))),
      this.textarea, this.preview, this.chips, this.error,
      h("div", { class: "actions" },
        h("button", { type: "button", class: "btn", onClick: () => this.fileInput.click() }, t("conv.attach")), this.fileInput,
        h("label", {}, this.privateBox, t("conv.sendPrivate")),
        h("span", { class: "muted" }, t("conv.sendHint")),
        h("span", { class: "spacer" }),
        this.sendButton));
    this.renderChips();
    this.updateSend();
    return el;
  }

  // 送信前の変更と添付を、入力欄の下にチップで表示する
  renderChips() {
    if (!this.chips) return;
    clear(this.chips);
    for (const [key, c] of Object.entries(this.changes)) {
      this.chips.append(h("span", { class: "chip" }, `${t(FIELD_LABELS[key])}: ${c.from} → ${c.label}`,
        h("button", { type: "button", "aria-label": t("conv.undoChange"), onClick: () => {
          delete this.changes[key];
          this.fillFields();
          this.renderChips();
          this.updateSend();
        } }, "×")));
    }
    for (const u of this.uploads) {
      const cls = `chip${u.state === "uploading" ? " busy" : ""}${u.state === "error" ? " error" : ""}`;
      this.chips.append(h("span", { class: cls, title: u.error || null },
        u.isImage && u.url ? h("img", { src: u.url, alt: "" }) : null,
        u.state === "uploading" ? t("conv.uploading", { name: u.filename })
          : u.state === "error" ? t("conv.uploadFailed", { name: u.filename, reason: u.error }) : u.filename,
        h("button", { type: "button", "aria-label": t("conv.removeAttachment"), onClick: () => this.removeUpload(u) }, "×")));
    }
    this.chips.hidden = !this.chips.childElementCount;
  }

  onPaste(e) {
    // Redmineと同じく、クリップボードの画像を貼り付けると添付になり、本文に画像の記法が入る(設計書 §6.3)
    const files = [...(e.clipboardData?.items || [])]
      .filter((it) => it.kind === "file")
      .map((it) => it.getAsFile())
      .filter(Boolean);
    if (!files.length) return;
    e.preventDefault();
    this.addFiles(files, true);
  }

  addFiles(files, insertImages) {
    for (const file of files) {
      const isImage = file.type.startsWith("image/");
      let name = file.name;
      if (!name || name === "image.png" || name === "blob") name = clipboardName(file.type);
      const u = { key: Symbol(), filename: uniqueName(name, this.uploads), isImage, state: "uploading" };
      this.uploads.push(u);
      if (isImage && insertImages) this.insertAtCursor(imageMarkup(u.filename, this.data.textFormat));
      this.upload(u, file);
    }
    this.renderChips();
    this.updateSend();
  }

  async upload(u, file) {
    const d = this.data;
    try {
      const res = await fetch(`/upload?serverId=${d.serverId}&filename=${encodeURIComponent(u.filename)}`, {
        method: "POST", body: file, credentials: "same-origin",
        headers: { "X-Remi-Upload": "1", "Content-Type": "application/octet-stream" },
      });
      const body = await res.json();
      if (body.error) throw Object.assign(new Error(body.error.code), { code: body.error.code, detail: body.error.detail });
      Object.assign(u, body.result, { state: "done" });
    } catch (e) {
      Object.assign(u, { state: "error", error: e.detail || t(`errors.${e.code || "connection_failed"}`) });
    }
    if (this.uploads.includes(u)) {
      this.renderChips();
      this.updateSend();
    }
  }

  removeUpload(u) {
    this.uploads = this.uploads.filter((x) => x !== u);
    // 本文に入れた画像の記法も取り除く
    const markup = imageMarkup(u.filename, this.data.textFormat);
    if (markup && this.textarea.value.includes(markup)) {
      this.textarea.value = this.textarea.value.replace(markup, "");
      this.saveDraft();
    }
    this.renderChips();
    this.updateSend();
  }

  insertAtCursor(text) {
    if (!text) return;
    const ta = this.textarea;
    const before = ta.value.slice(0, ta.selectionStart);
    const sep = before && !before.endsWith("\n") ? "\n" : "";
    ta.focus();
    document.execCommand("insertText", false, `${sep}${text}\n`);
    this.saveDraft();
  }

  updateSend() {
    if (!this.sendButton) return;
    const busy = this.uploads.some((u) => u.state === "uploading");
    const hasUpload = this.uploads.some((u) => u.state === "done");
    const hasContent = this.textarea.value.trim() || Object.keys(this.changes).length || hasUpload;
    this.sendButton.disabled = this.sending || busy || !hasContent;
  }

  async setPreview(on, editTab, previewTab) {
    editTab.setAttribute("aria-selected", String(!on));
    previewTab.setAttribute("aria-selected", String(on));
    this.textarea.hidden = on;
    this.preview.hidden = !on;
    if (on) {
      const d = this.data;
      const pending = this.uploads.filter((u) => u.id).map((u) => ({ filename: u.filename, id: u.id }));
      const out = await call("preview", { serverId: d.serverId, issueId: d.issueId, text: this.textarea.value, pending });
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
    if (!d || this.sendButton?.disabled) return;
    const body = this.textarea.value;
    const changes = Object.fromEntries(Object.entries(this.changes).map(([k, c]) => [k, c.value]));
    const uploads = this.uploads.filter((u) => u.state === "done")
      .map((u) => ({ token: u.token, filename: u.filename, contentType: u.contentType }));
    this.saveDraft.cancel(); // 送信後に古い下書きが保存し直されないようにする(下書きはサーバー側で保存する)
    this.inFlight.set(issueKey(d), body);
    this.error.textContent = "";
    this.updateSend();
    this.timeline.append(this.pendingMessage());
    this.timeline.scrollTop = this.timeline.scrollHeight;
    try {
      await call("postNote", { serverId: d.serverId, issueId: d.issueId, body, private: this.privateBox.checked, changes, uploads });
    } catch (e) {
      this.noteFinished({ serverId: d.serverId, issueId: d.issueId, error: e.code, detail: e.detail });
    }
  }

  pendingMessage() {
    const parts = [this.textarea?.value || "", ...Object.values(this.changes).map((c) => `${c.from} → ${c.label}`),
      ...this.uploads.filter((u) => u.state === "done").map((u) => u.filename)].filter(Boolean);
    return h("article", { class: "msg mine pending" },
      h("div", { class: "body" }, h("div", { class: "who" }, t("conv.sending")),
        h("div", { class: "bubble" }, parts.join("\n"))));
  }

  // バックエンドから送信結果が届いた
  noteFinished(ev) {
    const key = issueKey(ev);
    if (!this.inFlight.has(key)) return;
    const sent = this.inFlight.get(key);
    this.inFlight.delete(key);
    const d = this.data;
    if (!d || issueKey(d) !== key) return; // 別のチケットを表示中: 開き直したときに下書きから復元される
    this.timeline.querySelector(".msg.pending")?.remove();
    if (ev.error) {
      this.showFailed(ev.error, ev.detail);
    } else {
      // 送信中に書き足した分は消さずに残す
      const value = this.textarea.value;
      this.textarea.value = value.startsWith(sent) ? value.slice(sent.length).replace(/^\s*\n/, "") : value;
      if (this.textarea.value) this.saveDraft();
      this.error.textContent = "";
      this.changes = {};
      this.uploads = [];
      this.editOptions = null; // ステータスが変わると遷移できる先も変わるので読み直す
      this.renderChips();
    }
    this.updateSend();
  }

  showFailed(code = null, detail = "") {
    clear(this.error).append(
      t("conv.sendFailed"), code ? ` (${t(`errors.${code}`)}${detail ? `: ${detail}` : ""})` : "", " ",
      h("button", { type: "button", class: "btn", onClick: () => this.send() }, t("conv.retry")));
  }
}

// 貼り付けた画像の名前(Redmineと同じ形式: clipboard-YYYYMMDDHHMM-xxxxx.png)
function clipboardName(type) {
  const ext = ({ "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp", "image/bmp": "bmp" })[type] || "png";
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  const stamp = `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}${p(d.getHours())}${p(d.getMinutes())}`;
  const rand = Math.random().toString(36).slice(2, 7);
  return `clipboard-${stamp}-${rand}.${ext}`;
}

function uniqueName(name, uploads) {
  const used = new Set(uploads.map((u) => u.filename.toLowerCase()));
  if (!used.has(name.toLowerCase())) return name;
  const dot = name.lastIndexOf(".");
  const [base, ext] = dot > 0 ? [name.slice(0, dot), name.slice(dot)] : [name, ""];
  for (let i = 2; ; i++) if (!used.has(`${base}-${i}${ext}`.toLowerCase())) return `${base}-${i}${ext}`;
}

// 本文に入れる画像の記法(サーバーのテキスト書式に合わせる)
function imageMarkup(filename, format) {
  if (format === "common_mark") return `![](${filename.includes(" ") ? `<${filename}>` : filename})`;
  if (format === "textile") return `!${filename}!`;
  return "";
}
