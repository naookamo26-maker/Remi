// 左ペイン: サーバー / プロジェクトのツリー(設計書 §3.2、モック「メインウィンドウ」左)
import { call } from "./api.js";
import { clear, h, toast } from "./dom.js";
import { formatTime, t } from "./i18n.js";
import { icon } from "./icons.js";
import { contextMenu } from "./ui.js";

export function selectionKey(sel) {
  return `${sel.kind}:${sel.serverId ?? ""}:${sel.projectId ?? ""}`;
}

export class Tree {
  constructor(app) {
    this.app = app;
    this.data = null;
    this.el = h("ul", { class: "tree", role: "tree", "aria-label": t("tree.label"), tabindex: "0",
      onKeydown: (e) => this.onKey(e) });
    this.status = h("div", { class: "sync-status", "aria-live": "polite" });
    this.root = h("nav", { class: "pane pane-tree", "aria-label": t("tree.label") },
      this.el,
      h("div", { class: "tree-footer" },
        this.status,
        h("div", { class: "row" },
          h("button", { type: "button", class: "btn", onClick: () => app.openSettings() }, icon("gear"), t("tree.settings")),
          h("button", { type: "button", class: "btn icon-only", title: t("tree.syncNow"), "aria-label": t("tree.syncNow"),
            onClick: () => { call("syncNow"); toast(t("tree.syncStarted")); } }, icon("sync")))));
  }

  get collapsed() {
    return new Set(this.app.settings.tree_collapsed || []);
  }

  async refresh() {
    this.data = await call("getTree");
    this.render();
  }

  render() {
    const data = this.data;
    if (!data) return;
    clear(this.el);
    const sel = selectionKey(this.app.selection);
    const collapsed = this.collapsed;
    const node = (label, sel_, opts = {}) => {
      const key = selectionKey(sel_);
      const hasKids = opts.children && opts.children.length > 0;
      const token = opts.token;
      const isCollapsed = token && collapsed.has(token);
      const row = h("div", {
        class: ["node", opts.cls, key === sel && !opts.header ? "selected" : ""].filter(Boolean).join(" "),
        style: { paddingLeft: `${8 + (opts.depth || 0) * 16}px` },
        title: opts.title || null,
        onClick: () => this.app.select(sel_),
        onContextmenu: (e) => { e.preventDefault(); this.menu(e, sel_); },
      },
        h("span", { class: "twisty", onClick: hasKids ? (e) => { e.stopPropagation(); this.toggle(token); } : null },
          hasKids ? (isCollapsed ? "▶" : "▼") : ""),
        opts.icon ? icon(opts.icon) : null,
        h("span", { class: "name" }, label),
        opts.dot ? h("span", { class: `dot ${opts.dot}`, title: opts.dotLabel, "aria-label": opts.dotLabel }) : null,
        opts.unread ? h("span", { class: "badge", "aria-label": t("tree.unreadCount", { count: opts.unread }) }, opts.unread > 999 ? "999+" : opts.unread)
          : (opts.count ? h("span", { class: "count" }, opts.count) : null));
      const li = h("li", { role: "treeitem", "aria-selected": String(key === sel && !opts.header),
        dataset: { key: opts.header ? "" : key, token: hasKids ? token : "" },
        "aria-expanded": hasKids ? String(!isCollapsed) : null }, row);
      if (hasKids && !isCollapsed) li.append(h("ul", { role: "group" }, opts.children));
      return li;
    };

    if (data.servers.length !== 1) {
      this.el.append(node(t("tree.allServers"), { kind: "all" }, { unread: data.unread, icon: "menu", cls: "all" }));
    }
    for (const s of data.servers) {
      const project = (p, depth) => node(p.name, { kind: "project", serverId: s.serverId, projectId: p.projectId }, {
        depth, unread: p.unread, count: p.unread ? 0 : p.count, token: `p:${s.serverId}:${p.projectId}`,
        cls: [!p.accessible ? "inaccessible" : "", !p.active ? "inactive" : ""].join(" "),
        title: !p.accessible ? t("tree.noAccess") : null,
        children: p.children.map((c) => project(c, depth + 1)),
      });
      const kids = [
        node(t("tree.all"), { kind: "server", serverId: s.serverId }, { depth: 1, count: s.count }),
        node(t("tree.unreadOnly"), { kind: "unread", serverId: s.serverId }, { depth: 1, unread: s.unread }),
        ...s.projects.map((p) => project(p, 1)),
      ];
      const dotLabel = s.status === "error" ? t("tree.statusError") : s.status === "ok" ? t("tree.statusOk") : null;
      const server = node(s.name, { kind: "server", serverId: s.serverId }, {
        icon: "server", cls: "server", header: true, token: `s:${s.serverId}`, children: kids,
        dot: s.status === "unknown" ? null : s.status, dotLabel, title: s.url,
      });
      this.el.append(server);
      if (s.error) {
        const msg = t(`errors.${s.error}`);
        server.querySelector(":scope > .node").after(h("div", { class: "node-error" }, t("tree.cannotSync"), " ", msg));
      }
    }
    if (!data.servers.length) {
      this.el.append(h("li", { class: "empty" }, t("tree.noServers")));
    }
    const times = data.servers.map((s) => s.lastSyncedAt).filter(Boolean).sort();
    this.status.textContent = times.length ? t("tree.lastSynced", { time: formatTime(times.at(-1)) }) : "";
  }

  toggle(token) {
    const set = this.collapsed;
    if (set.has(token)) set.delete(token);
    else set.add(token);
    // 保存の完了を待たずに表示へ反映する(render は app.settings を見るので先に書き換える)
    this.app.settings = { ...this.app.settings, tree_collapsed: [...set] };
    this.render();
    this.app.saveSetting("tree_collapsed", [...set]);
  }

  // ↑↓: 選択を移動(サーバーの見出し行は「すべて」と同じ選択なので飛ばす)、←→: 折りたたみ・展開
  onKey(e) {
    const rows = [...this.el.querySelectorAll("[role=treeitem]")].filter((li) => li.dataset.key);
    const idx = rows.findIndex((li) => li.dataset.key === selectionKey(this.app.selection));
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const next = rows[Math.max(0, Math.min(rows.length - 1, idx + (e.key === "ArrowDown" ? 1 : -1)))];
      next?.querySelector(".node")?.click();
    } else if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && idx >= 0) {
      const token = rows[idx].dataset.token;
      if (!token) return;
      e.preventDefault();
      if (this.collapsed.has(token) === (e.key === "ArrowRight")) this.toggle(token);
    }
  }

  async menu(e, sel) {
    const app = this.app;
    const items = [{ label: t("menu.markAllRead"), onClick: () => call("markAllRead", { serverId: sel.serverId ?? null, projectId: sel.projectId ?? null }) }];
    const server = this.data.servers.find((s) => s.serverId === sel.serverId);
    if (sel.kind === "project") {
      const ps = await call("getProjectSettings", { serverId: sel.serverId, projectId: sel.projectId });
      if (ps.url) items.push({ label: t("menu.openInBrowser"), onClick: () => call("openExternal", { url: ps.url }) });
      const options = [
        { label: t("menu.inherit"), value: null }, { label: t("menu.on"), value: true }, { label: t("menu.off"), value: false },
      ];
      const seg = (key, label, extra) => ({
        segmented: {
          label, options, value: ps[key].value,
          onChange: (value) => call("setProjectSetting", { serverId: sel.serverId, projectId: sel.projectId, key, value })
            .then(() => this.refresh()),
        },
        hint: [t("menu.currently", { value: ps[key].effective ? t("menu.on") : t("menu.off") }), extra].filter(Boolean).join(" · "),
      });
      items.push({ sep: true }, seg("notify", t("menu.notifications")),
        seg("watch_authored", t("menu.watchAuthored"), t("menu.appliesToSubprojects")));
    } else if (server) {
      items.push({ label: t("menu.openInBrowser"), onClick: () => call("openExternal", { url: server.url }) });
    }
    items.push({ sep: true }, {
      label: t("menu.showAllProjects"), checked: !!this.data.showAllProjects,
      onClick: () => app.saveSetting("show_all_projects", !this.data.showAllProjects).then(() => this.refresh()),
    });
    contextMenu(e.clientX, e.clientY, items);
  }
}
