// 画面の入り口: 状態の管理と、各ペインのつなぎ込み
import { call, subscribe } from "./api.js";
import { Conversation } from "./conversation.js";
import { clear, debounce, h, toast } from "./dom.js";
import { serverDialog, settingsDialog } from "./dialogs.js";
import { confirmDialog } from "./ui.js";
import { setCatalog, t } from "./i18n.js";
import { icon } from "./icons.js";
import { IssueList } from "./list.js";
import { Tabs } from "./tabs.js";
import { selectionKey, Tree } from "./tree.js";

const darkQuery = matchMedia("(prefers-color-scheme: dark)");
const CONV_MIN_WIDTH = 360;

class App {
  constructor(boot) {
    this.boot = boot;
    this.settings = boot.settings;
    /** @type {Record<string, number>} 保存中の設定と、その最新の保存の番号 */
    this.latestSave = {};
    this.selection = { kind: boot.hasServers ? "all" : "none" };
    this.current = null; // {serverId, issueId}
    this.pendingRead = false;
    this.tree = new Tree(this);
    this.list = new IssueList(this);
    this.conv = new Conversation(this);
    this.tabs = new Tabs(this);
    this.settingsDialog = null;
    /** 更新のお知らせの帯(Remi.exe のフォルダに、取り込んでいない更新があるとき。設計書 §2.5) */
    this.updateBar = h("div", { class: "update-bar", role: "status", hidden: true });
  }

  mount(root) {
    const splitter = (index) => h("div", { class: "splitter", role: "separator", "aria-orientation": "vertical",
      onPointerdown: (e) => this.dragSplitter(e, index) });
    this.layout = h("div", { class: "layout" }, this.tree.root, splitter(0), this.list.root, splitter(1), this.conv.root);
    root.replaceChildren(this.tabs.el, this.updateBar, this.layout);
    this.renderUpdate(this.boot.update);
    this.tabs.load();
    root.removeAttribute("aria-busy");
    root.classList.remove("app-loading");
    this.applyLayout();
    this.applyTheme();
    this.applyMineSide();
    darkQuery.addEventListener("change", () => this.applyTheme());
    addEventListener("keydown", (e) => this.onGlobalKey(e));
    addEventListener("focus", () => this.onActive(true));
    addEventListener("blur", () => this.onActive(false));
    document.addEventListener("visibilitychange", () => {
      // 見えていない(トレイに格納・最小化)間は、開いているチケットの確認を止めてもらう(サーバーの負荷を抑える)
      call("setVisible", { visible: document.visibilityState === "visible" }).catch(() => {});
      this.onActive(document.visibilityState === "visible" && document.hasFocus());
    });
    if (document.visibilityState !== "visible") call("setVisible", { visible: false }).catch(() => {});
    subscribe((e) => this.onEvent(e));
  }

  async start() {
    await this.tree.refresh();
    if (!this.boot.hasServers) {
      this.conv.showWelcome();
      this.list.render();
      return;
    }
    if (this.tree.data.servers.length === 1) this.selection = { kind: "server", serverId: this.tree.data.servers[0].serverId };
    this.tree.render();
    await this.list.refresh();
    // 通知を押して起動されたときは、そのチケットを開く
    const target = await call("takePendingOpen").catch(() => null);
    if (target) this.openIssue(target.serverId, target.issueId, { reveal: true });
  }

  // --- 更新のお知らせ(設計書 §2.5) -------------------------------------------------------
  renderUpdate(state) {
    const bar = this.updateBar;
    bar.hidden = !state?.available;
    if (bar.hidden) return clear(bar);
    bar.replaceChildren(icon("reload"),
      h("span", { class: "text" }, t("update.available"), state.canUpdate ? "" : ` ${t("update.noBatch")}`),
      h("button", { type: "button", class: "btn primary", disabled: !state.canUpdate, onClick: () => this.startUpdate() },
        t("update.restart")),
      h("button", { type: "button", class: "btn", onClick: () => {
        // 「あとで」: 次の起動までは知らせない
        call("dismissUpdate").catch(() => {});
        this.renderUpdate({ ...state, available: false });
      } }, t("update.later")));
  }

  async startUpdate() {
    if (!(await confirmDialog(t("update.confirmTitle"), t("update.confirmBody"), t("update.restart")))) return;
    await this.conv.composer?.saveDraft.flushPending(); // 入力中のコメントは、保存が終わってから更新する
    try {
      await call("startUpdate");
      toast(t("update.starting"));
    } catch (e) {
      toast(t(`errors.${e.code || "update_failed"}`));
    }
  }

  // --- 表示の設定 -------------------------------------------------------------------
  applyTheme() {
    const theme = this.settings.theme;
    const dark = theme === "dark" || (theme === "system" && darkQuery.matches);
    document.documentElement.classList.toggle("theme-dark", dark);
    document.documentElement.classList.toggle("theme-light", !dark);
  }

  applyMineSide() {
    this.layout?.classList.toggle("mine-left", this.settings.mine_side === "left");
  }

  applyLayout() {
    const [a, b] = this.settings.pane_sizes || [260, 360];
    this.tree.root.style.width = `${Math.max(180, a)}px`;
    this.list.root.style.width = `${Math.max(240, b)}px`;
  }

  dragSplitter(e, index) {
    const target = index === 0 ? this.tree.root : this.list.root;
    const startX = e.clientX;
    const startW = target.getBoundingClientRect().width;
    const sp = e.currentTarget;
    sp.setPointerCapture(e.pointerId);
    sp.classList.add("dragging");
    const min = index === 0 ? 180 : 240;
    // 会話ペインの最小幅(CSSの .pane-conv と同じ)を残す
    const other = (index === 0 ? this.list.root : this.tree.root).getBoundingClientRect().width;
    const max = Math.max(min, Math.min(innerWidth * 0.5, innerWidth - other - CONV_MIN_WIDTH));
    const move = (ev) => { target.style.width = `${Math.max(min, Math.min(max, startW + ev.clientX - startX))}px`; };
    const up = () => {
      sp.classList.remove("dragging");
      sp.removeEventListener("pointermove", move);
      sp.removeEventListener("pointerup", up);
      this.saveSetting("pane_sizes", [Math.round(this.tree.root.getBoundingClientRect().width),
        Math.round(this.list.root.getBoundingClientRect().width)]);
    };
    sp.addEventListener("pointermove", move);
    sp.addEventListener("pointerup", up);
  }

  async saveSetting(key, value) {
    const seq = (this.saveSeq = (this.saveSeq || 0) + 1);
    this.latestSave[key] = seq;
    const saved = await call("setSetting", { key, value });
    // 応答は全設定の、その時点の写し。別の項目の保存と重なると古い値を含むので、この項目の値だけを取り込む
    // 応答を待つ間に同じ項目を保存し直していたら(ツリーの折りたたみを続けて切り替えたときなど)、
    // 古い応答で画面の状態を巻き戻さない
    if (this.latestSave[key] === seq) {
      delete this.latestSave[key];
      this.settings = { ...this.settings, [key]: saved[key] };
    }
    if (key === "theme") this.applyTheme();
    if (key === "mine_side") this.applyMineSide();
    if (key === "language") {
      // 画面は再読み込みだけで切り替わる(設計書 §10.1)。入力中の文章は、先に下書きとして保存する
      await this.conv.composer?.saveDraft.flushPending();
      setTimeout(() => location.reload(), 400);
    }
    return this.settings;
  }

  // --- キー操作 ---------------------------------------------------------------------
  onGlobalKey(e) {
    const ctrl = (e.ctrlKey || e.metaKey) && !e.altKey;
    if (ctrl && !e.shiftKey && e.key.toLowerCase() === "w" && this.tabs.closeActive()) {
      e.preventDefault(); // Redmineのタブを閉じる
    } else if (e.key === "F5" || (ctrl && e.key.toLowerCase() === "r")) {
      // 画面の再読み込み(送信前の添付などを失う)はせず、Redmineから読み込み直す。Shift・Ctrl付きも同じ
      e.preventDefault();
      if (!e.repeat) this.syncNow();
    } else if (ctrl && !e.shiftKey && e.key.toLowerCase() === "f" && !document.querySelector(".backdrop")) {
      e.preventDefault();
      this.list.focusSearch();
    }
  }

  // 今すぐ同期する。開いているチケットは、更新日時が同じでもコメントごと取り直す。
  // 同期している間は受け付けない(ボタンの回転と状態欄で同期中だと分かる。押したときは「同期中です」と伝える)
  async syncNow() {
    try {
      const { started } = await call("syncNow", this.current ?? {});
      if (!started) toast(t("tree.syncRunning"));
    } catch (e) {
      toast(t(`errors.${e.code || "connection_failed"}`));
    }
  }

  // --- 選択 -------------------------------------------------------------------------
  select(sel) {
    if (selectionKey(sel) === selectionKey(this.selection)) {
      // 選択中の項目をもう一度押したら、開いていた会話を閉じて、その項目の表示(バージョンの変化など)に戻る
      if (this.current) this.closeIssue({ keepDraft: true });
      return;
    }
    this.selection = sel;
    // 終了したバージョン・まだ取得していないバージョンは、開いたときにチケットを取得する(届いたら versionsChanged)
    if (sel.kind === "version") {
      this.tree.failedVersions.delete(sel.versionId); // 選び直したら、取得をもう一度試す
      call("fetchVersion", { serverId: sel.serverId, versionId: sel.versionId }).catch(() => {});
    }
    // 開いていた会話は閉じる(別のプロジェクトなどを選んだのに、前のチケットが残って見えると紛らわしいので)
    if (this.current) this.closeIssue({ keepDraft: true });
    else this.showSelectionView();
    this.tree.render();
    this.list.refresh();
  }

  // 選んでいたバージョンがツリーから消えた(ウォッチをやめた・削除された)ら、そのサーバーを選び直す
  checkSelection() {
    const sel = this.selection;
    if (sel.kind !== "version" || !this.tree.data) return;
    const server = this.tree.data.servers.find((s) => s.serverId === sel.serverId);
    const has = (nodes) => nodes.some((n) => n.versions.some((v) => v.versionId === sel.versionId) || has(n.children));
    if (server && has(server.projects)) return;
    this.selection = server ? { kind: "server", serverId: server.serverId } : { kind: "all" };
    if (!this.current) this.conv.showEmpty();
    this.list.refresh().catch(() => {});
  }

  selectionLabels() {
    const sel = this.selection;
    const data = this.tree.data;
    const server = data?.servers.find((s) => s.serverId === sel.serverId);
    if (sel.kind === "all" || sel.kind === "none") return { title: t("tree.allServers"), crumb: "" };
    if (sel.kind === "unread") return { title: t("tree.unreadOnly"), crumb: server?.name || "" };
    if (sel.kind === "server") return { title: server?.name || "", crumb: "" };
    if (sel.kind === "version") {
      // バージョンを持つプロジェクトの階層をたどる
      const trail = (nodes, path) => {
        for (const n of nodes) {
          const v = n.versions.find((x) => x.versionId === sel.versionId);
          if (v) return { title: v.name, crumb: [...path, n.name, t("tree.roadmap")].join(" › ") };
          const r = trail(n.children, [...path, n.name]);
          if (r) return r;
        }
        return null;
      };
      return (server && trail(server.projects, [])) || { title: "", crumb: server?.name || "" };
    }
    const path = [];
    const find = (nodes, trail) => {
      for (const n of nodes) {
        if (n.projectId === sel.projectId) return [...trail, n.name];
        const r = find(n.children, [...trail, n.name]);
        if (r) return r;
      }
      return null;
    };
    const names = server ? find(server.projects, []) || [] : [];
    path.push(...names);
    return { title: names.at(-1) || "", crumb: names.slice(0, -1).join(" › ") || server?.name || "" };
  }

  /**
   * @param {{note?: string|null, reveal?: boolean}} [opts] reveal: 通知から開くとき、左(ツリー)・真ん中(一覧)の選択も
   *   そのチケットに合わせる
   */
  async openIssue(serverId, issueId, { note = null, reveal = false } = {}) {
    this.current = { serverId, issueId };
    this.list.markSelected();
    call("setOpenIssue", { serverId, issueId }).catch(() => {});
    try {
      await this.conv.load(serverId, issueId, { markRead: document.hasFocus() || document.visibilityState === "visible", resetLimit: true });
    } catch (e) {
      toast(t(`errors.${e.code || "connection_failed"}`));
      return;
    }
    if (reveal) await this.revealIssue(serverId, issueId);
    if (note) this.conv.scrollToNote(note);
  }

  // 開いたチケットが今の一覧になければ、そのプロジェクトを選び直す(あれば選択は変えず、一覧で強調するだけ)
  async revealIssue(serverId, issueId) {
    const cur = this.current;
    const data = this.conv.data;
    if (!cur || cur.serverId !== serverId || cur.issueId !== issueId) return; // 読み込み中に別の表示へ移った
    const inList = () => this.list.raw.some((i) => i.serverId === serverId && i.issueId === issueId);
    if (!inList() && data?.serverId === serverId && data.issueId === issueId && data.projectId) {
      this.selection = { kind: "project", serverId, projectId: data.projectId };
      this.tree.revealProject(serverId, data.projectId);
      this.tree.render();
      try {
        await this.list.refresh();
      } catch (e) {
        console.warn("list refresh failed", e);
      }
    }
    this.list.markSelected();
  }

  // 開いている会話を閉じる。送信前の添付は捨てる。keepDraft なら、入力中でまだ保存していない文章を
  // 下書きとして保存する(サーバーの削除・ユーザーの変更で閉じるときは、前のデータを書き戻さないよう保存しない)
  closeIssue({ keepDraft = false } = {}) {
    if (keepDraft) this.conv.composer?.saveDraft.flushPending();
    else this.conv.composer?.saveDraft.cancel();
    this.current = null;
    this.pendingRead = false;
    call("setOpenIssue", { serverId: null, issueId: null }).catch(() => {});
    this.showSelectionView();
    this.list.render(); // 閉じたら、既読になったチケットを並び順にも反映する(開いている間は、位置を動かさない)
  }

  // チケットを開いていないときの右ペイン: バージョンを選んでいれば、その変化の記録。ほかは案内だけ
  showSelectionView() {
    const sel = this.selection;
    if (sel.kind === "version") {
      this.conv.showVersion(sel.serverId, sel.versionId, { markRead: document.hasFocus() })
        .catch((e) => toast(t(`errors.${e.code || "connection_failed"}`)));
    } else {
      this.conv.showEmpty();
    }
  }

  // 本文中の #123: 監視中なら開き、そうでなければブラウザで開く
  async openIssueOrBrowser(serverId, issueId, note) {
    try {
      await call("getConversation", { serverId, issueId, limit: 1, markRead: false });
      await this.openIssue(serverId, issueId, { note });
    } catch {
      this.openIssueInBrowser(serverId, issueId, note);
    }
  }

  /**
   * 「Remiで開く」「外部ブラウザで開く」のメニュー項目。開く場所の設定が「外部ブラウザ」のときは、1つだけにする。
   * @param {() => string} url 開くURL(選んだときに求める)
   * @param {{extra?: object[]}} [opts]
   */
  openMenuItems(url, { open = (external) => this.openRedmine(url(), { external }) } = {}) {
    const external = { label: t("menu.openInBrowser"), onClick: () => open(true) };
    if (this.settings.redmine_open_in === "browser") return [external];
    return [{ label: t("menu.openRedmine"), onClick: () => open(false) }, external];
  }

  openIssueInBrowser(serverId, issueId, note = null, { external = false } = {}) {
    const server = this.tree.data?.servers.find((s) => s.serverId === serverId);
    if (server) this.openRedmine(`${server.url}/issues/${issueId}${note ? `#note-${note}` : ""}`, { external });
  }

  // Redmineのページを開く。設定に従ってRemi(タブ・別ウィンドウ)か外部ブラウザ(Shift+クリックで常に外部ブラウザ)
  openRedmine(url, { external = false } = {}) {
    return call("openRedmine", { url, external });
  }

  /**
   * リンクを開く。http(s) は、Redmine(serverUrl 以下)ならRedmineの開き方、それ以外は外部ブラウザ。
   * ほかのスキーム(link_schemes.json・mailto)は、関連付けられたアプリで開く。
   */
  openLink(href, { serverUrl = "", external = false } = {}) {
    if (!/^https?:\/\//i.test(href)) return this.openUri(href);
    if (isUnder(href, serverUrl)) return this.openRedmine(href, { external });
    return call("openExternal", { url: href });
  }

  /** 本文の自動リンク(link_schemes.json のスキーム・mailto)を開く。指定に従って、開く前に確認する */
  async openUri(url) {
    const m = url.match(/^([a-z][a-z0-9+.-]*):/i);
    const scheme = m ? m[1].toLowerCase() : "";
    const conf = (this.boot.linkSchemes || []).find((x) => x.scheme === scheme);
    if (!conf && scheme !== "mailto") return;
    if (conf?.confirm && !(await confirmDialog(t("conv.openUriTitle"), t("conv.openUriBody", { url }), t("conv.openUriOk")))) return;
    call("openUri", { url }).catch((e) => toast(t(`errors.${e.code || "connection_failed"}`)));
  }

  openSettings() {
    settingsDialog(this);
  }

  openServerDialog(server = null) {
    serverDialog(this, server);
  }

  async onServersChanged() {
    this.boot.hasServers = true;
    this.settingsDialog?.refresh();
    await this.tree.refresh();
    const servers = this.tree.data.servers;
    if (!servers.length) {
      this.selection = { kind: "none" };
      this.current = null;
      this.conv.showWelcome();
    } else {
      if (this.current && !servers.some((s) => s.serverId === this.current.serverId)) this.closeIssue(); // サーバーを削除した
      if (this.selection.kind === "none" || (this.selection.serverId && !servers.some((s) => s.serverId === this.selection.serverId))) {
        this.selection = servers.length === 1 ? { kind: "server", serverId: servers[0].serverId } : { kind: "all" };
        if (!this.current) this.showSelectionView();
      }
    }
    this.tree.render();
    this.list.refresh();
  }

  // --- アクティブ状態と既読 ------------------------------------------------------------
  onActive(active) {
    call("setActive", { active }).catch(() => {});
    if (active && !this.current && this.selection.kind === "version") this.showSelectionView(); // 変化を見たことにする
    if (active) this.flushRead();
  }

  // 既読を待っていたコメント(非アクティブの間や、上へスクロール中に届いたもの)を、読める状態になったら既読にする(設計書 §7.1)
  flushRead() {
    if (!this.pendingRead || !this.current || !document.hasFocus() || !this.conv.isNearBottom()) return;
    this.pendingRead = false;
    call("markRead", this.current);
  }

  // --- サーバーからのイベント -----------------------------------------------------------
  onEvent(e) {
    switch (e.type) {
      case "dataChanged":
        this.refreshSoon();
        if (this.current && e.serverId === this.current.serverId && (e.issueIds || []).includes(this.current.issueId)
          && e.source !== "read") {
          // 上へスクロールして読んでいる間に届いた新着は、最下部まで見たとき(新着ボタンを押したときを含む)に既読にする
          const markRead = document.hasFocus() && this.conv.isNearBottom();
          if (!markRead) this.pendingRead = true;
          this.conv.load(this.current.serverId, this.current.issueId, { markRead });
        }
        break;
      case "syncState":
      case "projectSettingsChanged":
        this.refreshSoon();
        break;
      case "versionsChanged": {
        this.refreshSoon();
        const sel = this.selection;
        if (sel.kind === "version" && sel.serverId === e.serverId && (e.versionIds || []).includes(sel.versionId)) {
          if (!this.tree.data || e.source === "read") break;
          if (!this.current) this.showSelectionView(); // 表示中のバージョンに変化が届いた
        }
        break;
      }
      case "serversChanged":
        this.onServersChanged();
        break;
      case "tabsUnavailable":
        toast(t("tabs.unavailable"));
        break;
      case "tabsChanged":
        this.tabs.set({ tabs: e.tabs, active: e.active });
        break;
      case "serverCacheCleared":
        // 接続先や認証ユーザーが変わった: 前のユーザーのデータ(入力中の内容・添付を含む)を残さない
        if (this.current?.serverId === e.serverId) this.closeIssue();
        break;
      case "versionFetchFailed": {
        const sel = this.selection;
        this.tree.failedVersions.add(e.versionId); // 「取得中」の表示を止める
        this.tree.render();
        this.list.render();
        if (sel.kind === "version" && sel.serverId === e.serverId && sel.versionId === e.versionId) {
          toast(t(`errors.${e.error}`));
        }
        break;
      }
      case "noteFinished":
        this.conv.noteFinished(e);
        if (e.error) toast(t("conv.sendFailed"));
        break;
      case "openIssue":
        this.openIssue(e.serverId, e.issueId, { reveal: true });
        break;
      case "watchFinished":
        if (e.error) {
          toast(t(`errors.${e.error}`));
          // 失敗したときは dataChanged が来ないので、描き直してボタンを押せる状態に戻す
          if (this.current && e.serverId === this.current.serverId && e.issueId === this.current.issueId) {
            this.conv.load(this.current.serverId, this.current.issueId, { markRead: false });
          }
        }
        break;
      case "openSettings":
        this.openSettings();
        break;
      case "updateAvailable":
        this.renderUpdate(e);
        break;
      case "resync":
        this.refreshSoon.flush();
        if (this.current) this.conv.load(this.current.serverId, this.current.issueId, { markRead: false });
        // 受け取り損ねた送信の結果(noteFinished)を問い合わせ直す(送信中のまま戻らないことがないように)
        this.conv.recoverSends();
        break;
    }
  }

  // 届いたイベントでの描き直し。失敗しても(アプリの終了中など)次のイベントで描き直されるので無視する
  refreshSoon = debounce(() => {
    this.tree.refresh().catch((e) => console.warn("tree refresh failed", e));
    this.list.refresh().catch((e) => console.warn("list refresh failed", e));
  }, 300);
}

/**
 * href が、Redmine(serverUrl)の中のページか。前方一致だけだと https://redmine.example.com.evil.net/ も
 * 含んでしまうので、serverUrl の直後が区切り(/ ? #)か、末尾であることも確かめる。
 * @param {string} href @param {string} serverUrl
 */
function isUnder(href, serverUrl) {
  if (!serverUrl) return false;
  const base = serverUrl.replace(/\/+$/, "");
  if (href.slice(0, base.length).toLowerCase() !== base.toLowerCase()) return false;
  return href.length === base.length || "/?#".includes(href[base.length]);
}

async function main() {
  const root = document.getElementById("app");
  try {
    const boot = await call("bootstrap");
    setCatalog(boot.lang, boot.catalog, boot.fallbackCatalog);
    document.documentElement.lang = boot.lang;
    document.title = `Remi v${boot.version}`;
    const app = new App(boot);
    window.remi = app; // 開発・テスト用
    app.mount(root);
    await app.start();
  } catch (e) {
    console.error(e);
    root.textContent = `Remi: ${e.code || e.message}`;
  }
}

main();
