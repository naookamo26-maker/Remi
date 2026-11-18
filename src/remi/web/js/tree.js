// 左ペイン: サーバー / プロジェクトのツリー(設計書 §3.2、モック「メインウィンドウ」左)
import { call } from "./api.js";
import { clear, countText, h, toast } from "./dom.js";
import { formatTime, t } from "./i18n.js";
import { icon } from "./icons.js";
import { contextMenu } from "./ui.js";

/** 表示するチケットの関係(プロジェクトごとの設定)。src/remi/store/issues.py の RELATIONS と同じ */
export const RELATIONS = ["assigned", "group", "watching", "authored"];

export function selectionKey(sel) {
  return `${sel.kind}:${sel.serverId ?? ""}:${sel.projectId ?? ""}:${sel.versionId ?? ""}`;
}

export class Tree {
  constructor(app) {
    this.app = app;
    this.data = null;
    this.el = h("ul", { class: "tree", role: "tree", "aria-label": t("tree.label"), tabindex: "0",
      onKeydown: (e) => this.onKey(e) });
    this.status = h("div", { class: "sync-status", "aria-live": "polite" });
    // 設定・サーバーの追加・同期は、ツリーの上に並べる(アプリ名とバージョンはウィンドウのタイトルに出す)
    const textButton = (iconName, label, onClick) => h("button", { type: "button", class: "btn", title: label, onClick },
      icon(iconName), h("span", { class: "label" }, label));
    this.root = h("nav", { class: "pane pane-tree", "aria-label": t("tree.label") },
      h("header", { class: "tree-header" },
        textButton("gear", t("tree.settings"), () => app.openSettings()),
        textButton("plus", t("tree.addServer"), () => app.openServerDialog()),
        h("span", { class: "spacer" }),
        h("button", { type: "button", class: "btn icon-only", title: t("tree.syncNow"), "aria-label": t("tree.syncNow"),
          onClick: () => app.syncNow() }, icon("sync"))),
      this.el,
      h("div", { class: "tree-footer" }, this.status));
  }

  get collapsed() {
    return new Set(this.app.settings.tree_collapsed || []);
  }

  async refresh() {
    this.data = await call("getTree");
    this.app.checkSelection();
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
      // 既定で畳んでおく項目(終了したバージョン)は、開いたときだけ token を覚える
      const isCollapsed = token && (opts.collapsedByDefault ? !collapsed.has(token) : collapsed.has(token));
      const row = h("div", {
        class: ["node", opts.cls, key === sel && !opts.header ? "selected" : ""].filter(Boolean).join(" "),
        style: { paddingLeft: `${8 + (opts.depth || 0) * 16}px` },
        title: opts.title || null,
        // 見出しだけの行(ロードマップ・終了したバージョン)は、押すと開閉する
        onClick: () => (opts.toggleOnly ? this.toggle(token) : this.app.select(sel_)),
        onContextmenu: (e) => { e.preventDefault(); if (!opts.toggleOnly) this.menu(e, sel_); },
      },
        h("span", { class: "twisty", onClick: hasKids ? (e) => { e.stopPropagation(); this.toggle(token); } : null },
          hasKids ? (isCollapsed ? "▶" : "▼") : ""),
        opts.icon ? icon(opts.icon) : null,
        h("span", { class: "name" }, label),
        opts.extra || null,
        opts.dot ? h("span", { class: `dot ${opts.dot}`, title: opts.dotLabel, "aria-label": opts.dotLabel }) : null,
        opts.progress ? h("span", { class: "count" }, opts.progress) : null,
        opts.unread ? h("span", { class: "badge", "aria-label": t("tree.unreadCount", { count: opts.unread }) }, countText(opts.unread))
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
      // バージョン: 変化があれば印を付け、完了数/全体を出す。終了したものは、畳んだ「終了したバージョン」の下に出す
      const version = (p, v, depth) => node(v.name, { kind: "version", serverId: s.serverId, versionId: v.versionId }, {
        depth, icon: "flag", cls: ["version", v.changed ? "changed" : "", v.closed ? "closed" : ""].join(" "),
        title: v.dueDate ? t("tree.versionDue", { date: v.dueDate }) : null,
        dot: v.changed ? "changed" : null, dotLabel: t("tree.versionChanged"),
        progress: `${v.done}/${v.total}`,
      });
      // プロジェクトの下に「ロードマップ」を置き、その中にバージョンを並べる
      const roadmap = (p, depth) => {
        if (!p.versions.length) return [];
        const kids = p.versions.filter((v) => !v.closed).map((v) => version(p, v, depth + 1));
        const closed = p.versions.filter((v) => v.closed);
        if (closed.length) {
          kids.push(node(t("tree.closedVersions", { count: closed.length }), { kind: "closedVersions", serverId: s.serverId,
            projectId: p.projectId }, { depth: depth + 1, header: true, toggleOnly: true, collapsedByDefault: true,
            cls: "version-group", token: `vc:${s.serverId}:${p.projectId}`,
            children: closed.map((v) => version(p, v, depth + 2)) }));
        }
        // 畳んでいても気づけるよう、中のバージョンに変化があればロードマップにも印を付ける
        const changed = p.versions.some((v) => v.changed);
        return [node(t("tree.roadmap"), { kind: "roadmap", serverId: s.serverId, projectId: p.projectId }, {
          depth, header: true, toggleOnly: true, icon: "flag", cls: `roadmap${changed ? " changed" : ""}`,
          token: `rm:${s.serverId}:${p.projectId}`, dot: changed ? "changed" : null, dotLabel: t("tree.versionChanged"),
          children: kids })];
      };
      const project = (p, depth) => node(p.name, { kind: "project", serverId: s.serverId, projectId: p.projectId }, {
        depth, unread: p.unread, count: p.unread ? 0 : p.count, token: `p:${s.serverId}:${p.projectId}`,
        cls: [!p.accessible ? "inaccessible" : "", !p.active ? "inactive" : ""].join(" "),
        title: !p.accessible ? t("tree.noAccess") : null,
        extra: this.relationsButton(s, p, depth),
        children: [...roadmap(p, depth + 1), ...p.children.map((c) => project(c, depth + 1))],
      });
      const kids = [
        node(t("tree.all"), { kind: "server", serverId: s.serverId }, { depth: 1, count: s.count }),
        node(t("tree.unreadOnly"), { kind: "unread", serverId: s.serverId }, { depth: 1, unread: s.unread }),
        ...s.projects.map((p) => project(p, 1)),
      ];
      const dotLabel = s.status === "error" ? t("tree.statusError") : s.status === "ok" ? t("tree.statusOk") : null;
      const server = node(s.name, { kind: "server", serverId: s.serverId }, {
        icon: "server", cls: "server", header: true, token: `s:${s.serverId}`, children: kids,
        extra: this.relationsButton(s),
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

  // サーバー・プロジェクトの横の「表示するチケット」ボタン。絞っているものでは常に表示し、
  // 自分で設定したものは強調する(継承しているものは控えめに)。ほかは行にマウスを乗せたときだけ表示する。
  // p を省略するとサーバー(既定)のボタン
  relationsButton(server, p = null, depth = 0) {
    const target = p || server;
    const restricted = target.relations.length < RELATIONS.length;
    const summary = t("tree.relationsSummary", { list: target.relations.map((r) => t(`tree.relation.${r}`)).join("、") });
    const from = depth === 1 ? "tree.relationsFromServer" : "tree.relationsInherited";
    const title = !restricted ? t("tree.visibleRelations") : (target.relationsOwn || !p ? summary : t(from, { summary }));
    const state = !restricted ? "" : (target.relationsOwn ? "own" : "inherited");
    return h("button", { type: "button", class: `node-filter ${state}`.trim(), title,
      "aria-label": `${t("tree.visibleRelations")}: ${target.name}`, "aria-haspopup": "menu",
      onClick: (e) => {
        e.stopPropagation();
        const r = e.currentTarget.getBoundingClientRect();
        this.relationsMenu(r.right + 4, r.top, server.serverId, p?.projectId ?? null, depth === 1);
      } }, icon("filter"));
  }

  findProject(serverId, projectId) {
    const server = this.data?.servers.find((s) => s.serverId === serverId);
    const find = (nodes) => {
      for (const n of nodes) {
        if (n.projectId === projectId) return n;
        const r = find(n.children);
        if (r) return r;
      }
      return null;
    };
    return server ? find(server.projects) : null;
  }

  // 「表示するチケット」を選ぶメニュー。プロジェクトでは、上で[親と同じ|独自]を切り替え、独自のときだけ
  // 下のチェックを変えられる。サーバー(projectId が null)では既定を選ぶ。変えるたびに開き直して描き直す
  relationsMenu(x, y, serverId, projectId, isRoot, focusLabel = null) {
    const server = this.data?.servers.find((s) => s.serverId === serverId);
    const target = projectId === null ? server : this.findProject(serverId, projectId);
    if (!target) return;
    const reopen = (label) => this.relationsMenu(x, y, serverId, projectId, isRoot, label);
    const save = async (value, label) => {
      if (projectId === null) {
        // サーバーの既定: すべてを選んだら「既定から変えていない」として保存する
        await call("setServerSetting", { serverId, key: "visible_relations",
          value: value && value.length === RELATIONS.length ? null : value });
      } else {
        await call("setProjectSetting", { serverId, projectId, key: "visible_relations", value });
      }
      await Promise.all([this.refresh(), this.app.list.refresh()]);
      reopen(label);
    };
    const own = projectId === null || target.relationsOwn;
    const title = t("tree.visibleRelationsFor", { name: target.name });
    const items = [];
    if (projectId === null) {
      items.push({ label: title, hint: true });
    } else {
      // 最上位のプロジェクトはサーバーの既定を、ほかは親プロジェクトの設定を引き継ぐ。
      // 独自にしたときは、引き継いでいた値から始める
      const inheritLabel = isRoot ? t("tree.relationsServer") : t("tree.relationsInherit");
      const ownLabel = t("tree.relationsOwnMode");
      items.push({ segmented: { label: title, value: own,
        options: [{ label: inheritLabel, value: false }, { label: ownLabel, value: true }],
        onChange: (v) => {
          if (v === own) return reopen(v ? ownLabel : inheritLabel);
          save(v ? [...target.relations] : null, v ? ownLabel : inheritLabel);
        } } });
    }
    items.push({ sep: true });
    for (const rel of RELATIONS) {
      const label = t(`tree.relation.${rel}`);
      items.push({
        label, checked: target.relations.includes(rel), disabled: !own,
        onClick: () => {
          const next = target.relations.includes(rel) ? target.relations.filter((r) => r !== rel)
            : RELATIONS.filter((r) => r === rel || target.relations.includes(r));
          if (!next.length) {
            toast(t("tree.visibleRelationsAtLeastOne"));
            reopen(label);
            return;
          }
          save(next, label);
        },
      });
    }
    items.push({ sep: true },
      { label: projectId === null ? t("tree.visibleRelationsServerHint") : t("tree.visibleRelationsHint"), hint: true });
    contextMenu(x, y, items, { focusLabel });
  }

  async menu(e, sel) {
    if (sel.kind === "version") {
      const server = this.data.servers.find((x) => x.serverId === sel.serverId);
      const url = `${server?.url}/versions/${sel.versionId}`;
      contextMenu(e.clientX, e.clientY, [
        { label: t("menu.openRedmine"), onClick: () => this.app.openRedmine(url) },
        { label: t("menu.openInBrowser"), onClick: () => this.app.openRedmine(url, { external: true }) },
      ]);
      return;
    }
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
      const depth = server?.projects.some((p) => p.projectId === sel.projectId) ? 1 : 2;
      items.push({ sep: true }, seg("notify", t("menu.notifications")),
        seg("watch_authored", t("menu.watchAuthored"), t("menu.appliesToSubprojects")),
        seg("watch_versions", t("menu.watchVersions"), t("menu.appliesToSubprojects")),
        { sep: true },
        { label: t("menu.visibleRelations"),
          onClick: () => this.relationsMenu(e.clientX, e.clientY, sel.serverId, sel.projectId, depth === 1) });
    } else if (server) {
      // チケットのないプロジェクトも表示するかは、サーバーごとに選ぶ
      items.push({ label: t("menu.openInBrowser"), onClick: () => call("openExternal", { url: server.url }) },
        { sep: true },
        { label: t("menu.showAllProjects"), checked: !!server.showAllProjects,
          onClick: () => call("setServerSetting", { serverId: server.serverId, key: "show_all_projects",
            value: !server.showAllProjects }).then(() => this.refresh()) },
        { label: t("menu.visibleRelations"),
          onClick: () => this.relationsMenu(e.clientX, e.clientY, server.serverId, null, false) });
    }
    contextMenu(e.clientX, e.clientY, items);
  }
}
