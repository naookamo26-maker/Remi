// 画面の入り口: 状態の管理と、各ペインのつなぎ込み
import { call, subscribe } from "./api.js";
import { Conversation } from "./conversation.js";
import { debounce, h, toast } from "./dom.js";
import { serverDialog, settingsDialog } from "./dialogs.js";
import { setCatalog, t } from "./i18n.js";
import { IssueList } from "./list.js";
import { selectionKey, Tree } from "./tree.js";

const darkQuery = matchMedia("(prefers-color-scheme: dark)");

class App {
  constructor(boot) {
    this.boot = boot;
    this.settings = boot.settings;
    this.selection = { kind: boot.hasServers ? "all" : "none" };
    this.current = null; // {serverId, issueId}
    this.pendingRead = false;
    this.tree = new Tree(this);
    this.list = new IssueList(this);
    this.conv = new Conversation(this);
    this.settingsDialog = null;
  }

  mount(root) {
    const splitter = (index) => h("div", { class: "splitter", role: "separator", "aria-orientation": "vertical",
      onPointerdown: (e) => this.dragSplitter(e, index) });
    this.layout = h("div", { class: "layout" }, this.tree.root, splitter(0), this.list.root, splitter(1), this.conv.root);
    root.replaceChildren(this.layout);
    root.removeAttribute("aria-busy");
    root.classList.remove("app-loading");
    this.applyLayout();
    this.applyTheme();
    this.applyMineSide();
    darkQuery.addEventListener("change", () => this.applyTheme());
    addEventListener("focus", () => this.onActive(true));
    addEventListener("blur", () => this.onActive(false));
    document.addEventListener("visibilitychange", () => this.onActive(document.visibilityState === "visible" && document.hasFocus()));
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
    const move = (ev) => { target.style.width = `${Math.max(min, Math.min(innerWidth * 0.5, startW + ev.clientX - startX))}px`; };
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
    this.settings = await call("setSetting", { key, value });
    if (key === "theme") this.applyTheme();
    if (key === "mine_side") this.applyMineSide();
    if (key === "language") {
      // 画面は再読み込みだけで切り替わる(設計書 §10.1)
      setTimeout(() => location.reload(), 300);
    }
    return this.settings;
  }

  // --- 選択 -------------------------------------------------------------------------
  select(sel) {
    if (selectionKey(sel) === selectionKey(this.selection)) return;
    this.selection = sel;
    this.tree.render();
    this.list.refresh();
  }

  selectionLabels() {
    const sel = this.selection;
    const data = this.tree.data;
    const server = data?.servers.find((s) => s.serverId === sel.serverId);
    if (sel.kind === "all" || sel.kind === "none") return { title: t("tree.allServers"), crumb: "" };
    if (sel.kind === "unread") return { title: t("tree.unreadOnly"), crumb: server?.name || "" };
    if (sel.kind === "server") return { title: server?.name || "", crumb: "" };
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

  async openIssue(serverId, issueId, { note = null } = {}) {
    this.current = { serverId, issueId };
    this.list.markSelected();
    call("setOpenIssue", { serverId, issueId });
    await this.conv.load(serverId, issueId, { markRead: document.hasFocus() || document.visibilityState === "visible", resetLimit: true });
    if (note) this.conv.scrollToNote(note);
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

  openIssueInBrowser(serverId, issueId, note = null) {
    const server = this.tree.data?.servers.find((s) => s.serverId === serverId);
    if (server) call("openExternal", { url: `${server.url}/issues/${issueId}${note ? `#note-${note}` : ""}` });
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
    } else if (this.selection.kind === "none" || (this.selection.serverId && !servers.some((s) => s.serverId === this.selection.serverId))) {
      this.selection = servers.length === 1 ? { kind: "server", serverId: servers[0].serverId } : { kind: "all" };
      if (!this.current) this.conv.showEmpty();
    }
    this.tree.render();
    this.list.refresh();
  }

  // --- アクティブ状態と既読 ------------------------------------------------------------
  onActive(active) {
    call("setActive", { active }).catch(() => {});
    if (active && this.pendingRead && this.current) {
      // 非アクティブの間に届いたコメントは、アクティブになった時点で既読にする(設計書 §7.1)
      this.pendingRead = false;
      call("markRead", this.current);
    }
  }

  // --- サーバーからのイベント -----------------------------------------------------------
  onEvent(e) {
    switch (e.type) {
      case "dataChanged":
        this.refreshSoon();
        if (this.current && e.serverId === this.current.serverId && (e.issueIds || []).includes(this.current.issueId)
          && e.source !== "read") {
          const active = document.hasFocus();
          if (!active) this.pendingRead = true;
          this.conv.load(this.current.serverId, this.current.issueId, { markRead: active });
        }
        break;
      case "syncState":
      case "projectSettingsChanged":
        this.refreshSoon();
        break;
      case "serversChanged":
        this.onServersChanged();
        break;
      case "noteFinished":
        this.conv.noteFinished(e);
        if (e.error) toast(t("conv.sendFailed"));
        break;
      case "openIssue":
        this.openIssue(e.serverId, e.issueId);
        break;
      case "resync":
        this.refreshSoon.flush();
        if (this.current) this.conv.load(this.current.serverId, this.current.issueId, { markRead: false });
        break;
    }
  }

  refreshSoon = debounce(() => {
    this.tree.refresh();
    this.list.refresh();
  }, 300);
}

async function main() {
  const root = document.getElementById("app");
  try {
    const boot = await call("bootstrap");
    setCatalog(boot.lang, boot.catalog, boot.fallbackCatalog);
    document.documentElement.lang = boot.lang;
    document.title = "Remi";
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
