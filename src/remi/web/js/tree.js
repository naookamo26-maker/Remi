// 左ペイン: サーバー / プロジェクトのツリー(設計書 §3.2、モック「メインウィンドウ」左)
import { call } from "./api.js";
import { clear, h, toast } from "./dom.js";
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
      // 変化の印(青い丸)。dotCollapsed / dotExpanded は、畳んでいるとき・開いているときに出すか
      const dot = opts.dot ? opts.dot
        : (hasKids && isCollapsed ? opts.dotCollapsed : opts.dotExpanded) || null;
      const row = h("div", {
        class: ["node", opts.cls, key === sel && !opts.header ? "selected" : ""].filter(Boolean).join(" "),
        style: { paddingLeft: `${8 + (opts.depth || 0) * 16}px` },
        title: opts.title || null,
        // 見出しだけの行(ロードマップ・終了したバージョン)は、押すと開閉する
        onClick: () => (opts.onClick ? opts.onClick() : opts.toggleOnly ? this.toggle(token) : this.app.select(sel_)),
        onContextmenu: (e) => { e.preventDefault(); if (!opts.toggleOnly && !opts.onClick) this.menu(e, sel_); },
      },
        // 開閉の ▼ ▶ の場所には、開閉できない行ではアイコン(バージョンの旗)を置く
        h("span", { class: "twisty", onClick: hasKids ? (e) => { e.stopPropagation(); this.toggle(token); } : null },
          hasKids ? (isCollapsed ? "▶" : "▼") : opts.twistyIcon ? icon(opts.twistyIcon) : ""),
        opts.icon ? icon(opts.icon) : null,
        h("span", { class: "name" }, label),
        // 件数は名前のすぐ右に、(5) のように控えめに出す(バージョンは 完了/全体)
        opts.count ? h("span", { class: "count-inline" }, `(${opts.count})`) : null,
        // 変化の印は青い丸だけ。親(畳んでいる行)では、中の変化も拾う。開いている行は、自分の分だけ
        dot ? h("span", { class: `dot ${dot.kind}`, title: dot.label, "aria-label": dot.label }) : null,
        // 右端は、歯車とフィルタだけ(ふだんは行にマウスを乗せたときだけ見える)
        opts.gear || opts.filter ? h("span", { class: "node-tools" }, opts.gear, opts.filter) : null);
      const li = h("li", { role: "treeitem", "aria-selected": String(key === sel && !opts.header),
        dataset: { key: opts.header ? "" : key, token: hasKids ? token : "" },
        "aria-expanded": hasKids ? String(!isCollapsed) : null }, row);
      if (hasKids && !isCollapsed) li.append(h("ul", { role: "group" }, opts.children));
      return li;
    };

    if (data.servers.length !== 1) {
      this.el.append(node(t("tree.allServers"), { kind: "all" }, { icon: "menu", cls: "all",
        dot: data.unread ? { kind: "changed", label: t("tree.unreadCount", { count: data.unread }) } : null }));
    }
    for (const s of data.servers) {
      // バージョン: 変化があれば青い丸を付け、(完了数/全体) を名前の右に出す。旗は ▼ の場所に置く。
      // 終了したものは、畳んだ「終了したバージョン」の下に出す
      const version = (p, v, depth) => node(v.name, { kind: "version", serverId: s.serverId, versionId: v.versionId }, {
        depth, noTools: true, twistyIcon: "flag", cls: ["version", v.closed ? "closed" : ""].filter(Boolean).join(" "),
        title: [v.dueDate ? t("tree.versionDue", { date: v.dueDate }) : "",
          v.synced ? "" : t(v.closed ? "tree.versionNotFetched" : "tree.versionLoading")]
          .filter(Boolean).join("\n") || null,
        dot: v.changed ? { kind: "changed", label: t("tree.versionChanged") } : null,
        // チケットをまだ取得していないバージョンは 0/0 ではなく「…」にする(空のバージョンと見分ける)
        count: v.synced ? `${v.done}/${v.total}` : "…",
      });
      // バージョンの並び(終了したものは、畳んだ「終了したバージョン」にまとめる)。depth は並べる階層
      const versionNodes = (p, depth) => {
        const kids = p.versions.filter((v) => !v.closed).map((v) => version(p, v, depth));
        const closed = p.versions.filter((v) => v.closed);
        if (closed.length) {
          const closedChanged = closed.some((v) => v.changed);
          kids.push(node(t("tree.closedVersions", { count: closed.length }), { kind: "closedVersions", serverId: s.serverId,
            projectId: p.projectId }, { depth, header: true, toggleOnly: true, collapsedByDefault: true, noTools: true,
            cls: "version-group", token: `vc:${s.serverId}:${p.projectId}`,
            dotCollapsed: closedChanged ? { kind: "changed", label: t("tree.versionChanged") } : null,
            children: closed.map((v) => version(p, v, depth + 1)) }));
        }
        return kids;
      };
      // 子プロジェクトがあるプロジェクトだけ、「ロードマップ」の見出しでバージョンをまとめる
      // (子プロジェクトと並んで混ざらないように)。子がなければ、バージョンをプロジェクトの直下に並べる
      const roadmap = (p, depth) => {
        if (!p.versions.length) return [];
        if (!p.children.length) return versionNodes(p, depth);
        // 畳んでいても気づけるよう、中のバージョンに変化があればロードマップにも印を付ける
        const changed = p.versions.some((v) => v.changed);
        return [node(t("tree.roadmap"), { kind: "roadmap", serverId: s.serverId, projectId: p.projectId }, {
          depth, header: true, toggleOnly: true, noTools: true, cls: "roadmap",
          token: `rm:${s.serverId}:${p.projectId}`,
          dotCollapsed: changed ? { kind: "changed", label: t("tree.versionChanged") } : null,
          children: versionNodes(p, depth + 1) })];
      };
      // 中(子プロジェクト・バージョン)に、未読のコメントやバージョンの変化があるか
      const changedBelow = (p) => p.unread > 0 || p.versions.some((v) => v.changed) || p.children.some(changedBelow);
      const project = (p, depth) => {
        // 開いているときは自分の未読だけ(子の分は、子の行に出る)。畳んでいるときは中の変化も含める
        const own = p.unread - p.children.reduce((sum, c) => sum + c.unread, 0);
        return node(p.name, { kind: "project", serverId: s.serverId, projectId: p.projectId }, {
          depth, count: p.count || null, token: `p:${s.serverId}:${p.projectId}`,
          cls: [!p.accessible ? "inaccessible" : "", !p.active ? "inactive" : ""].join(" "),
          title: !p.accessible ? t("tree.noAccess") : null,
          dotExpanded: own > 0 ? { kind: "changed", label: t("tree.unreadCount", { count: own }) } : null,
          dotCollapsed: changedBelow(p) ? { kind: "changed", label: p.unread > 0 && !p.versions.some((v) => v.changed)
            ? t("tree.unreadCount", { count: p.unread }) : t("tree.changedInside") } : null,
          filter: this.relationsButton(s, p, depth),
          gear: this.settingsButton(s, p),
          children: [...roadmap(p, depth + 1), ...p.children.map((c) => project(c, depth + 1))],
        });
      };
      // 表示するものがない(プロジェクトが1つもない)サーバーには、「すべて」「未読のみ」も出さない
      const kids = s.projects.length ? [
        node(t("tree.all"), { kind: "server", serverId: s.serverId }, { depth: 1, count: s.count || null }),
        node(t("tree.unreadOnly"), { kind: "unread", serverId: s.serverId }, { depth: 1, count: s.unread || null,
          dot: s.unread ? { kind: "changed", label: t("tree.unreadCount", { count: s.unread }) } : null }),
        ...s.projects.map((p) => project(p, 1)),
      ] : [];
      // Teams の「非表示のチャネル」のように、プロジェクトの下に、隠れているプロジェクトを出す・隠すリンクを置く
      if (s.showAllProjects || s.hiddenProjects) {
        kids.push(node(s.showAllProjects ? t("tree.hideEmptyProjects") : t("tree.showAllProjects"),
          { kind: "toggleProjects", serverId: s.serverId }, {
            depth: 1, header: true, noTools: true, cls: "link-row", count: s.showAllProjects ? null : s.hiddenProjects,
            title: t("tree.showAllProjectsHint"),
            onClick: () => call("setServerSetting", { serverId: s.serverId, key: "show_all_projects",
              value: !s.showAllProjects }).then(() => this.refresh()) }));
      }
      const dotLabel = s.status === "error" ? t("tree.statusError") : s.status === "ok" ? t("tree.statusOk") : null;
      const server = node(s.name, { kind: "server", serverId: s.serverId }, {
        icon: "server", cls: "server", header: true, token: `s:${s.serverId}`, children: kids,
        filter: this.relationsButton(s),
        gear: this.serverSettingsButton(s),
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
    const list = target.relations.length ? target.relations.map((r) => t(`tree.relation.${r}`)).join("、") : t("tree.relationsNone");
    const summary = t("tree.relationsSummary", { list });
    const from = depth === 1 ? "tree.relationsFromServer" : "tree.relationsInherited";
    const title = !restricted ? t("tree.visibleRelations") : (target.relationsOwn || !p ? summary : t(from, { summary }));
    const state = !restricted ? "" : (target.relationsOwn ? "own" : "inherited");
    return h("button", { type: "button", class: `node-tool node-filter ${state}`.trim(), title,
      "aria-label": `${t("tree.visibleRelations")}: ${target.name}`, "aria-haspopup": "menu",
      onClick: (e) => {
        e.stopPropagation();
        const r = e.currentTarget.getBoundingClientRect();
        this.relationsMenu(r.right + 4, r.top, server.serverId, p?.projectId ?? null, depth === 1);
      } }, icon("filter"));
  }

  // サーバーの名前の横の歯車ボタン: そのサーバーの設定(名前・URL・APIキーなど)を開く
  serverSettingsButton(server) {
    return h("button", { type: "button", class: "node-tool node-gear", title: t("tree.serverSettingsShort"),
      "aria-label": t("tree.serverSettings", { name: server.name }),
      onClick: async (e) => {
        e.stopPropagation();
        try {
          const sv = (await call("listServers")).find((x) => x.serverId === server.serverId);
          if (sv) this.app.openServerDialog(sv);
        } catch (err) {
          toast(t(`errors.${err.code || "connection_failed"}`));
        }
      } }, icon("gear"));
  }

  // プロジェクトの横の歯車ボタン: 通知・作成したチケットのウォッチ・バージョンのウォッチの設定を開く
  settingsButton(server, p) {
    return h("button", { type: "button", class: "node-tool node-gear", title: t("tree.projectSettingsShort"),
      "aria-label": t("tree.projectSettings", { name: p.name }), "aria-haspopup": "menu",
      onClick: (e) => {
        e.stopPropagation();
        const r = e.currentTarget.getBoundingClientRect();
        this.projectSettingsMenu(r.right + 4, r.top, server.serverId, p.projectId);
      } }, icon("gear"));
  }

  // プロジェクトの設定(継承あり)。変えるたびに開き直して、今の値を描き直す
  async projectSettingsMenu(x, y, serverId, projectId, focusLabel = null) {
    const ps = await call("getProjectSettings", { serverId, projectId });
    const options = [
      { label: t("menu.inherit"), value: null }, { label: t("menu.on"), value: true }, { label: t("menu.off"), value: false },
    ];
    const seg = (key, label, extra) => ({
      segmented: {
        label, options, value: ps[key].value,
        onChange: (value) => call("setProjectSetting", { serverId, projectId, key, value })
          .then(() => this.refresh())
          .then(() => this.projectSettingsMenu(x, y, serverId, projectId, label)),
      },
      hint: [t("menu.currently", { value: ps[key].effective ? t("menu.on") : t("menu.off") }), extra].filter(Boolean).join(" · "),
    });
    const target = this.findProject(serverId, projectId);
    contextMenu(x, y, [
      { label: t("tree.projectSettings", { name: target?.name ?? "" }), hint: true },
      { sep: true },
      seg("notify", t("menu.notifications")),
      seg("watch_authored", t("menu.watchAuthored"), t("menu.appliesToSubprojects")),
      seg("watch_versions", t("menu.watchVersions"), t("menu.appliesToSubprojects")),
    ], { focusLabel });
  }

  findVersion(serverId, versionId) {
    const server = this.data?.servers.find((s) => s.serverId === serverId);
    const find = (nodes) => {
      for (const n of nodes) {
        const v = n.versions.find((x) => x.versionId === versionId) || find(n.children);
        if (v) return v;
      }
      return null;
    };
    return server ? find(server.projects) : null;
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
          // すべて外してもよい(表示するものがないプロジェクトはツリーから消える。戻すには、サーバーの下の
          // 「すべてのプロジェクトを表示」のリンクを使う)
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
      // 設定は行の右端のボタン(フィルタ・歯車)から開く。右クリックは操作だけにする
      const ps = await call("getProjectSettings", { serverId: sel.serverId, projectId: sel.projectId });
      if (ps.url) items.push({ label: t("menu.openInBrowser"), onClick: () => call("openExternal", { url: ps.url }) });
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
