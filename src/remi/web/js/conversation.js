// 右ペイン: 会話ビュー(設計書 §5.2, §6、モック「メインウィンドウ」右)
// ヘッダ(issue-header.js)・会話(timeline.js)・入力欄(composer.js)をまとめ、読み込み・スクロール・送信の流れを受け持つ。
import { call } from "./api.js";
import { Composer } from "./composer.js";
import { clear, h, toast } from "./dom.js";
import { t } from "./i18n.js";
import { icon } from "./icons.js";
import { IssueHeader } from "./issue-header.js";
import { pendingMessage, renderTimeline } from "./timeline.js";
import { lightbox } from "./ui.js";
import { renderVersion } from "./version-view.js";

/** @typedef {import("./types.js").ConversationData} ConversationData */
/** @typedef {import("./types.js").EditOptions} EditOptions */
/** @typedef {import("./types.js").FieldChange} FieldChange */
/** @typedef {import("./types.js").FieldKey} FieldKey */

const PAGE = 50;
const NEAR_BOTTOM = 40; // px。これより最下部に近ければ「最下部を見ている」とみなす

const issueKey = (d) => (d ? `${d.serverId}:${d.issueId}` : "");

export class Conversation {
  constructor(app) {
    this.app = app;
    /** @type {ConversationData|null} 表示中のチケット */
    this.data = null;
    this.limit = PAGE;
    /** @type {Map<string, string>} 送信中のチケット(issueKey → 送った本文)。別のチケットへ移っても状態が混ざらない */
    this.inFlight = new Map();
    /** @type {Partial<Record<FieldKey, FieldChange>>} ヘッダで選んだ、送信前の変更 */
    this.changes = {};
    /** @type {EditOptions|null} ヘッダの選択肢(Redmineに問い合わせて別に読み込む) */
    this.editOptions = null;
    /**
     * @type {import("./types.js").WritePermissions|null} 書き込めるか(null: まだ分からない)。
     * 選択肢を読み直す間も、同じチケットのあいだは前の値を使う(入力欄が一瞬有効になるのを避ける)
     */
    this.permissions = null;
    this.header = new IssueHeader(this);
    /** @type {Composer|null} */
    this.composer = null;
    this.root = h("section", { class: "pane pane-conv", "aria-label": t("conv.label") });
    this.showEmpty();
  }

  get sending() {
    return this.inFlight.has(issueKey(this.data));
  }

  showEmpty() {
    this.data = null;
    this.version = null;
    this._versionToken = null; // 読み込み中のバージョンの表示は描かない
    this.composer = null;
    clear(this.root).append(h("div", { class: "welcome" }, h("p", { class: "muted" }, t("conv.selectIssue"))));
  }

  /**
   * バージョンの概要と変化の記録を表示する。markRead なら、変化を見たことにする
   * @param {number} serverId
   * @param {number} versionId
   */
  async showVersion(serverId, versionId, { markRead = true } = {}) {
    const token = Symbol();
    this._versionToken = token;
    this.data = null; // 閉じたチケットの内容は使わない(読み込み中にチケットを開いたら、load が token を消す)
    this.composer = null;
    let d;
    try {
      d = await call("getVersion", { serverId, versionId, markRead });
    } catch (e) {
      if (this._versionToken === token) this.showEmpty();
      if (e.code !== "not_found") throw e;
      return;
    }
    // 読み込みの間に、チケットを開いた・別の項目を選んだときは描かない
    if (this._versionToken !== token) return;
    this.version = { serverId, versionId };
    clear(this.root).append(renderVersion(d, this.app));
  }

  showWelcome() {
    this.data = null;
    this.version = null;
    this.composer = null;
    clear(this.root).append(h("div", { class: "welcome" },
      h("h2", {}, t("welcome.title")),
      h("p", { class: "muted" }, t("welcome.body")),
      h("button", { type: "button", class: "btn primary", onClick: () => this.app.openServerDialog() }, icon("plus"), t("welcome.addServer"))));
  }

  // --- 読み込みと描画 -------------------------------------------------------------
  // 開く(open=true)と既読にする。届いた更新で描き直すときは markRead を app が判断する
  async load(serverId, issueId, { markRead = true, keepScroll = false, resetLimit = false } = {}) {
    if (resetLimit) this.limit = PAGE;
    this._versionToken = null; // 読み込み中のバージョンの表示は描かない
    this.version = null;
    let data;
    try {
      data = await call("getConversation", { serverId, issueId, limit: this.limit, markRead });
    } catch (e) {
      if (e.code === "not_found") return this.showEmpty();
      throw e;
    }
    const cur = this.app.current;
    if (!cur || cur.serverId !== serverId || cur.issueId !== issueId) return; // 別のチケットに移った
    const sameIssue = issueKey(this.data) === issueKey(data);
    if (!sameIssue) {
      this.changes = {};
      this.editOptions = null;
      this.permissions = null;
    }
    const prev = this.timeline;
    const atBottom = this.isNearBottom();
    const prevScroll = prev ? prev.scrollTop : 0;
    const prevHeight = prev ? prev.scrollHeight : 0;
    this.data = data;
    this.render(sameIssue);
    const tl = this.timeline;
    if (sameIssue && keepScroll) {
      // 過去分を読み込んだ: 表示位置を保つ
      tl.scrollTop = tl.scrollHeight - prevHeight + prevScroll;
    } else if (!sameIssue || atBottom) {
      const firstUnread = tl.querySelector(".unread-sep");
      if (!sameIssue && firstUnread) firstUnread.scrollIntoView({ block: "start" });
      else tl.scrollTop = tl.scrollHeight;
    } else {
      tl.scrollTop = prevScroll;
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
      this.permissions = opts.permissions;
      this.header.fill();
      this.composer?.update();
    } catch (e) {
      this.header.showError(e.code);
    }
  }

  isNearBottom() {
    const tl = this.timeline;
    return !!tl && tl.isConnected && tl.scrollHeight - tl.scrollTop - tl.clientHeight < NEAR_BOTTOM;
  }

  render(sameIssue) {
    const d = this.data;
    const header = this.header.render(d);
    const timeline = h("div", { class: "timeline", role: "log", "aria-label": t("conv.timeline"),
      onClick: (e) => this.onTimelineClick(e),
      onScroll: () => { this.stickBottom = this.isNearBottom(); } });
    // 画像の読み込みで高さが変わっても、最下部を見ていたら最下部を保つ
    timeline.addEventListener("load", (e) => {
      if (e.target.tagName === "IMG" && this.stickBottom) timeline.scrollTop = timeline.scrollHeight;
    }, true);
    if (sameIssue && this.composer?.el.isConnected) {
      // 同じチケットの更新では入力欄を作り直さない(入力中のフォーカス・カーソル位置・IMEの変換・
      // 元に戻す履歴・プレビューの状態を保つ)
      this.headerEl.replaceWith(header);
      this.timeline.replaceWith(timeline);
    } else {
      this.composer = new Composer(this, d);
      clear(this.root).append(header, timeline, this.composer.el);
    }
    this.headerEl = header;
    this.timeline = timeline;
    renderTimeline(timeline, d, { onLoadMore: () => {
      this.limit += PAGE;
      this.load(d.serverId, d.issueId, { markRead: false, keepScroll: true });
    } });
    if (this.sending) timeline.append(pendingMessage(this.composer.pendingParts()));
    this.composer.update();
  }

  /**
   * ヘッダで選んだ変更を記録し、チップと送信ボタンに反映する。change が null なら取り消す。
   * @param {FieldKey} key
   * @param {FieldChange|null} change
   * @param {{refill?: boolean}} [opts] refill: 選択欄も描き直す(チップの×から取り消したとき)
   */
  setChange(key, change, { refill = true } = {}) {
    if (change) this.changes[key] = change;
    else delete this.changes[key];
    if (refill) this.header.fill();
    this.composer?.renderChips();
    this.composer?.update();
  }

  // --- 本文中のリンク ---------------------------------------------------------------
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
      const server = d.url.replace(/\/issues\/\d+$/, "");
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

  // --- 送信 -------------------------------------------------------------------
  async send() {
    const d = this.data;
    const c = this.composer;
    if (!d || !c || c.sendButton.disabled) return;
    const { body, private: isPrivate, uploads } = c.payload();
    const changes = Object.fromEntries(Object.entries(this.changes).map(([k, ch]) => [k, ch.value]));
    this.inFlight.set(issueKey(d), body);
    c.beginSend();
    this.timeline.append(pendingMessage(c.pendingParts()));
    this.timeline.scrollTop = this.timeline.scrollHeight;
    try {
      await call("postNote", { serverId: d.serverId, issueId: d.issueId, body, private: isPrivate, changes, uploads });
    } catch (e) {
      this.noteFinished({ serverId: d.serverId, issueId: d.issueId, error: e.code, detail: e.detail });
    }
  }

  // バックエンドから送信結果が届いた(noteFinished イベント、または送信の受付に失敗した)
  noteFinished(ev) {
    const key = issueKey(ev);
    if (!this.inFlight.has(key)) return;
    const sent = this.inFlight.get(key);
    this.inFlight.delete(key);
    const d = this.data;
    const c = this.composer;
    if (!d || !c || issueKey(d) !== key) return; // 別のチケットを表示中: 開き直したときに下書きから復元される
    this.timeline.querySelector(".msg.pending")?.remove();
    if (ev.error) {
      c.showFailed(ev.error, ev.detail);
    } else {
      c.finishSent(sent);
      this.changes = {};
      this.editOptions = null; // ステータスが変わると遷移できる先も変わるので読み直す
      c.renderChips();
    }
    c.update();
  }
}
