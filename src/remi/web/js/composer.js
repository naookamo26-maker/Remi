// 会話ビューの入力欄: 下書き・プレビュー・添付・送信前の変更のチップ・送信ボタンの状態(設計書 §5.3, §6)
import { call } from "./api.js";
import { clear, debounce, h, html, spinner } from "./dom.js";
import { t } from "./i18n.js";
import { icon } from "./icons.js";
import { fieldLabel } from "./issue-header.js";

/** @typedef {import("./types.js").ConversationData} ConversationData */
/** @typedef {import("./types.js").Upload} Upload */

export class Composer {
  /**
   * 入力欄は、チケットを開いたときに1回だけ作る(同じチケットの更新では作り直さない)。
   * @param {import("./conversation.js").Conversation} conv 送信前の変更(changes)と送信中の状態を持つ
   * @param {ConversationData} d 開いたときのデータ(下書き・テキスト書式)
   */
  constructor(conv, d) {
    this.conv = conv;
    this.serverId = d.serverId;
    this.issueId = d.issueId;
    this.textFormat = d.textFormat;
    /** @type {Upload[]} 送信前の添付 */
    this.uploads = [];
    this.textarea = h("textarea", { "aria-label": t("conv.comment"), placeholder: t("conv.placeholder"), rows: "3",
      onInput: () => { this.saveDraft(); this.update(); },
      onKeydown: (e) => this.onKey(e),
      onPaste: (e) => this.onPaste(e) });
    this.textarea.value = d.draft.body || "";
    this.privateBox = h("input", { type: "checkbox", onChange: () => this.saveDraft() });
    this.privateBox.checked = !!d.draft.private;
    this.preview = h("div", { class: "preview content", hidden: true });
    this.chips = h("div", { class: "chips", "aria-live": "polite" });
    this.fileInput = h("input", { type: "file", multiple: true, hidden: true,
      onChange: () => { this.addFiles([...this.fileInput.files], false); this.fileInput.value = ""; } });
    this.attachButton = h("button", { type: "button", class: "btn", onClick: () => this.fileInput.click() }, t("conv.attach"));
    this.privateLabel = h("label", {}, this.privateBox, t("conv.sendPrivate"));
    this.sendButton = h("button", { type: "button", class: "btn primary", onClick: () => this.conv.send() }, icon("send"), t("conv.send"));
    this.error = h("div", { class: "error", role: "alert" });
    this.saveDraft = debounce(() => {
      call("saveDraft", { serverId: this.serverId, issueId: this.issueId, body: this.textarea.value, private: this.privateBox.checked })
        .catch(() => {});
    }, 600);
    if (d.draft.failed) this.showFailed();
    const tab = (label, selected, onClick) => h("button", { type: "button", role: "tab", "aria-selected": String(selected), onClick }, label);
    const editTab = tab(t("conv.edit"), true, () => this.setPreview(false, editTab, previewTab));
    const previewTab = tab(t("conv.preview"), false, () => this.setPreview(true, editTab, previewTab));
    const fmt = { common_mark: "Markdown", textile: "Textile", plain: t("conv.plainText") }[d.textFormat] || d.textFormat;
    this.el = h("div", { class: "composer",
      onDragover: (e) => { if (e.dataTransfer?.types?.includes("Files") && this.can.attach) { e.preventDefault(); this.el.classList.add("dragging"); } },
      onDragleave: () => this.el.classList.remove("dragging"),
      onDrop: (e) => {
        this.el.classList.remove("dragging");
        if (e.dataTransfer?.files?.length && this.can.attach) {
          e.preventDefault();
          this.addFiles([...e.dataTransfer.files], true);
        }
      } },
      h("div", { class: "tabs", role: "tablist", "aria-label": t("conv.inputMode") }, editTab, previewTab,
        h("span", { class: "spacer" }), h("span", { class: "fmt" }, t("conv.format", { format: fmt }))),
      this.textarea, this.preview, this.chips, this.error,
      h("div", { class: "actions" },
        this.attachButton, this.fileInput,
        this.privateLabel,
        h("span", { class: "muted" }, t("conv.sendHint")),
        h("span", { class: "spacer" }),
        this.sendButton));
    this.renderChips();
    this.update();
  }

  // --- 送信 -------------------------------------------------------------------
  /** 送る内容。添付はアップロードが終わったものだけ */
  payload() {
    return {
      body: this.can.comment ? this.textarea.value : "", private: this.privateBox.checked,
      uploads: this.uploads.filter((u) => u.state === "done")
        .map((u) => ({ token: u.token, filename: u.filename, contentType: u.contentType })),
    };
  }

  /** 送信中の吹き出しに出す内容 */
  pendingParts() {
    return [this.can.comment ? this.textarea.value : "", ...Object.values(this.conv.changes).map((c) => `${c.from} → ${c.label}`),
      ...this.uploads.filter((u) => u.state === "done").map((u) => u.filename)];
  }

  /** 送信を始めた: 古い下書きが保存し直されないようにする(下書きはサーバー側で保存する) */
  beginSend() {
    this.saveDraft.cancel();
    this.error.textContent = "";
    this.update();
  }

  /**
   * 送信できた。送信中に書き足した分は消さずに残す。
   * @param {string} sent 送った本文
   */
  finishSent(sent) {
    const value = this.textarea.value;
    this.textarea.value = value.startsWith(sent) ? value.slice(sent.length).replace(/^\s*\n/, "") : value;
    if (this.textarea.value) this.saveDraft();
    this.error.textContent = "";
    this.uploads = [];
    this.renderChips();
  }

  showFailed(code = null, detail = "") {
    clear(this.error).append(
      t("conv.sendFailed"), code ? ` (${t(`errors.${code}`)}${detail ? `: ${detail}` : ""})` : "", " ",
      h("button", { type: "button", class: "btn", onClick: () => this.conv.send() }, t("conv.retry")));
  }

  /**
   * できる操作。権限がまだ分からない(選択肢を読み込む前)ときは、すべてできるものとして扱う
   * (できなければ送信時にRedmineが拒否する)
   */
  get can() {
    const p = this.conv.permissions;
    return { comment: !p || p.comment, attach: !p || p.attach, privateNotes: !p || p.privateNotes };
  }

  /** 送信ボタンを押せるか(送信中・アップロード中は押せない)と、書き込めない入力欄の無効化 */
  update() {
    const can = this.can;
    this.textarea.disabled = !can.comment;
    this.textarea.placeholder = can.comment ? t("conv.placeholder") : t("conv.noCommentPermission");
    this.attachButton.disabled = !can.attach;
    this.attachButton.title = can.attach ? "" : t("conv.noAttachPermission");
    if (!can.privateNotes && this.privateBox.checked) {
      // 権限がないとRedmineは黙って公開のコメントにするので、チェックを外して見えるようにしておく
      this.privateBox.checked = false;
    }
    this.privateBox.disabled = !can.privateNotes;
    this.privateLabel.classList.toggle("disabled", !can.privateNotes);
    this.privateLabel.title = can.privateNotes ? "" : t("conv.noPrivatePermission");
    const busy = this.uploads.some((u) => u.state === "uploading");
    const hasUpload = this.uploads.some((u) => u.state === "done");
    const hasContent = (can.comment && this.textarea.value.trim()) || Object.keys(this.conv.changes).length || hasUpload;
    this.sendButton.disabled = this.conv.sending || busy || !hasContent;
  }

  // --- チップ(送信前の変更と添付) -------------------------------------------------
  renderChips() {
    clear(this.chips);
    for (const [key, c] of Object.entries(this.conv.changes)) {
      this.chips.append(h("span", { class: "chip" }, `${fieldLabel(key, c)}: ${c.from} → ${c.label}`,
        h("button", { type: "button", "aria-label": t("conv.undoChange"), onClick: () => this.conv.setChange(key, null) }, "×")));
    }
    for (const u of this.uploads) {
      const cls = `chip${u.state === "uploading" ? " busy" : ""}${u.state === "error" ? " error" : ""}`;
      this.chips.append(h("span", { class: cls, title: u.error || null },
        u.isImage && u.url ? h("img", { src: u.url, alt: "" }) : null,
        u.state === "uploading" ? [spinner(), t("conv.uploading", { name: u.filename })]
          : u.state === "error" ? t("conv.uploadFailed", { name: u.filename, reason: u.error }) : u.filename,
        h("button", { type: "button", "aria-label": t("conv.removeAttachment"), onClick: () => this.removeUpload(u) }, "×")));
    }
    this.chips.hidden = !this.chips.childElementCount;
  }

  // --- 添付 -------------------------------------------------------------------
  onPaste(e) {
    // Redmineと同じく、クリップボードの画像を貼り付けると添付になり、本文に画像の記法が入る(設計書 §6.3)
    const files = [...(e.clipboardData?.items || [])]
      .filter((it) => it.kind === "file")
      .map((it) => it.getAsFile())
      .filter(Boolean);
    if (!files.length) return;
    e.preventDefault();
    if (this.can.attach) this.addFiles(files, true);
  }

  addFiles(files, insertImages) {
    for (const file of files) {
      const isImage = file.type.startsWith("image/");
      let name = file.name;
      if (!name || name === "image.png" || name === "blob") name = clipboardName(file.type);
      /** @type {Upload} */
      const u = { key: Symbol(), filename: uniqueName(name, this.uploads), isImage, state: "uploading" };
      this.uploads.push(u);
      if (isImage && insertImages) this.insertAtCursor(imageMarkup(u.filename, this.textFormat));
      this.upload(u, file);
    }
    this.renderChips();
    this.update();
  }

  async upload(u, file) {
    try {
      const res = await fetch(`/upload?serverId=${this.serverId}&filename=${encodeURIComponent(u.filename)}`, {
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
      this.update();
    }
  }

  removeUpload(u) {
    this.uploads = this.uploads.filter((x) => x !== u);
    // 本文に入れた画像の記法も取り除く
    const markup = imageMarkup(u.filename, this.textFormat);
    if (markup && this.textarea.value.includes(markup)) {
      this.textarea.value = this.textarea.value.replace(markup, "");
      this.saveDraft();
    }
    this.renderChips();
    this.update();
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

  // --- 編集・プレビュー ------------------------------------------------------------
  async setPreview(on, editTab, previewTab) {
    editTab.setAttribute("aria-selected", String(!on));
    previewTab.setAttribute("aria-selected", String(on));
    this.textarea.hidden = on;
    this.preview.hidden = !on;
    if (on) {
      const pending = this.uploads.filter((u) => u.id).map((u) => ({ filename: u.filename, id: u.id }));
      this.preview.replaceChildren(h("span", { class: "muted loading" }, spinner(), t("conv.previewLoading")));
      const out = await call("preview", { serverId: this.serverId, issueId: this.issueId, text: this.textarea.value, pending });
      html(this.preview, out || "");
      if (!out) this.preview.append(h("span", { class: "muted" }, t("conv.nothingToPreview")));
    } else {
      this.textarea.focus();
    }
  }

  onKey(e) {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      this.conv.send();
    } else if (e.key === "Tab" && !e.shiftKey && !e.ctrlKey && !e.altKey) {
      // Tabキーでインデントを入力できるようにする(設計書 §5.3)
      e.preventDefault();
      document.execCommand("insertText", false, "    ");
    }
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
