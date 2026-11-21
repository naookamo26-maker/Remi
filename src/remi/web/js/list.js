// 中央ペイン: チケット一覧(設計書 §5.1)
import { call } from "./api.js";
import { clear, countText, debounce, h, spinner } from "./dom.js";
import { formatShort, t } from "./i18n.js";
import { icon } from "./icons.js";
import { RELATIONS } from "./tree.js";
import { contextMenu } from "./ui.js";

const RELATION_LABELS = { assigned: "list.assigned", group: "list.group", watching: "list.watching", authored: "list.authored" };

export class IssueList {
  constructor(app) {
    this.app = app;
    this.issues = [];
    /** サーバーが返した並び(未読を上にしたもの) */
    this.raw = [];
    /** 並び順の上で「未読」として扱うチケット(読んで既読になっても、会話を開いている間は元の位置に置く) */
    this.frozenUnread = new Set();
    this.search = "";
    this.title = h("h2", {});
    this.crumb = h("span", { class: "crumb" });
    // 表示密度(標準 / コンパクト)の切り替え。選んだ表示は設定 list_density に保存して、次回も使う
    this.densityButton = h("button", { type: "button", class: "density", title: t("list.density"), "aria-label": t("list.density"),
      "aria-pressed": "false",
      onClick: () => app.saveSetting("list_density", this.compact() ? "standard" : "compact").then(() => this.render()) },
      icon("density"));
    // 標準から変えている絞り込み(検索語・関係・クローズ済み)の要約。何も変えていないときは出さない
    this.summary = h("div", { class: "filter-summary", hidden: true });
    const onSearch = debounce(() => { this.search = this.searchInput.value; this.refresh(); }, 200);
    this.searchInput = h("input", { type: "search", placeholder: t("list.searchPlaceholder"), "aria-label": t("list.search"),
      onInput: onSearch,
      onKeydown: (e) => {
        // Esc: 絞り込みを解除する。↓: 一覧へ移る
        if (e.key === "Escape" && this.searchInput.value) {
          e.preventDefault();
          e.stopPropagation();
          this.searchInput.value = "";
          onSearch.flush();
        } else if (e.key === "ArrowDown") {
          e.preventDefault();
          this.el.focus();
        }
      } });
    this.unreadFirst = h("input", { type: "checkbox", onChange: () => app.saveSetting("unread_first", this.unreadFirst.checked).then(() => this.refresh()) });
    // 「クローズ済みも表示」は、マイルストーン(バージョン)を開いているときはチケット一覧とは別の設定にする
    this.showClosed = h("input", { type: "checkbox", onChange: () => app.saveSetting(this.showClosedKey(), this.showClosed.checked).then(() => this.refresh()) });
    // 関係での絞り込み(すべて / 担当 / ウォッチ / 作成)
    this.relationButtons = ["all", "assigned", "watching", "authored"].map((value) =>
      h("button", { type: "button", dataset: { value }, "aria-pressed": "false",
        // 絞り込みは、真ん中の一覧だけに効く(ツリーの件数・未読数は変えない)
        onClick: () => app.saveSetting("relation_filter", value).then(() => this.refresh()) },
        t(`list.filter.${value}`)));
    this.relationGroup = h("div", { class: "segmented", role: "group", "aria-label": t("list.filter.label") }, this.relationButtons);
    this.el = h("ul", { class: "issue-list", role: "listbox", "aria-label": t("list.label"), tabindex: "0",
      onKeydown: (e) => this.onKey(e) });
    this.root = h("section", { class: "pane pane-list", "aria-label": t("list.label") },
      h("div", { class: "list-header" },
        h("div", { class: "list-title" }, this.title, h("span", { class: "title-right" }, this.densityButton, this.crumb)),
        h("label", { class: "search" }, icon("search"), this.searchInput),
        this.relationGroup,
        h("div", { class: "toggles" },
          this.unreadFirstLabel = h("label", {}, this.unreadFirst, t("list.unreadFirst")),
          h("label", {}, this.showClosed, t("list.showClosed"))),
        this.summary),
      this.el);
  }

  async refresh() {
    const sel = this.app.selection;
    const params = { search: this.search };
    if (sel.serverId) params.serverId = sel.serverId;
    if (sel.projectId) params.projectId = sel.projectId;
    if (sel.kind === "unread") params.unreadOnly = true;
    if (sel.kind === "version") params.versionId = sel.versionId;
    params.relation = this.relationFilter();
    const token = Symbol();
    this._token = token;
    const res = await call("listIssues", params);
    if (this._token !== token) return; // 古い結果は捨てる
    this.raw = res.issues;
    this.render();
  }

  /**
   * 「未読を上に」の並びを、会話を開いている間は動かさない。開いたチケットは既読になって下へ移るので、
   * そのまま並べると、↓で順に読んでいるとき未読のチケットを飛ばしてしまう。会話を閉じたら、並びに反映する。
   */
  stabilize(issues) {
    const key = (i) => `${i.serverId}:${i.issueId}`;
    if (!this.app.settings.unread_first || this.app.selection.kind === "version") return issues;
    if (!this.app.current) {
      this.frozenUnread = new Set(issues.filter((i) => i.unread).map(key));
      return issues;
    }
    for (const i of issues) if (i.unread) this.frozenUnread.add(key(i));
    // 未読として扱うものを上に、それぞれの中は、いま表示している並びを保つ(新しく届いたものは、グループの先頭)
    const before = new Map(this.issues.map((i, n) => [key(i), n]));
    const group = (i) => (this.frozenUnread.has(key(i)) ? 0 : 1);
    const pos = (i) => before.get(key(i)) ?? -1;
    return [...issues].sort((a, b) => group(a) - group(b) || pos(a) - pos(b));
  }

  render() {
    this.issues = this.stabilize(this.raw);
    const { title, crumb } = this.app.selectionLabels();
    this.title.textContent = title;
    this.crumb.textContent = [crumb, t("list.count", { count: this.issues.length })].filter(Boolean).join(" · ");
    this.unreadFirst.checked = !!this.app.settings.unread_first;
    this.showClosed.checked = !!this.app.settings[this.showClosedKey()];
    // マイルストーンのチケットは未読として数えないので、「未読を上に」は出さない
    this.unreadFirstLabel.hidden = this.app.selection.kind === "version";
    const rel = this.relationFilter();
    const shown = this.shownFilters();
    for (const b of this.relationButtons) {
      b.setAttribute("aria-pressed", String(b.dataset.value === rel));
      b.hidden = !shown.includes(b.dataset.value);
    }
    // 絞り込める関係が1つだけなら「すべて」と同じなので、ボタンごと隠す。バージョンでは関係で絞り込まない
    this.relationGroup.hidden = shown.length <= 2 || this.app.selection.kind === "version";
    this.renderSummary(rel);
    const compact = this.compact();
    this.densityButton.setAttribute("aria-pressed", String(compact));
    this.el.classList.toggle("compact", compact);
    const scroll = this.el.scrollTop;
    clear(this.el);
    if (!this.issues.length) {
      // まだチケットを取得していないバージョンは、空ではなく取得中と出す
      const sel = this.app.selection;
      const loading = !this.search && sel.kind === "version"
        && this.app.tree.findVersion(sel.serverId, sel.versionId)?.synced === false
        && !this.app.tree.failedVersions.has(sel.versionId);
      this.el.append(h("li", { class: loading ? "empty loading" : "empty" },
        loading ? [spinner(), t("list.versionLoading")] : this.search ? t("list.noMatches") : t("list.empty")));
      return;
    }
    const current = this.app.current;
    const showServer = !this.app.selection.serverId && new Set(this.issues.map((i) => i.serverId)).size > 1;
    for (const it of this.issues) {
      const selected = current && current.serverId === it.serverId && current.issueId === it.issueId;
      const preview = it.lastNote ? `${it.lastNoteUser ?? ""}: ${it.lastNote}` : "";
      this.el.append(h("li", {
        class: ["issue", it.unread ? "unread" : "", selected ? "selected" : ""].join(" "),
        role: "option", "aria-selected": String(!!selected), dataset: { server: it.serverId, issue: it.issueId },
        onClick: () => this.app.openIssue(it.serverId, it.issueId),
        onContextmenu: (e) => { e.preventDefault(); this.menu(e.clientX, e.clientY, it); },
      },
        h("div", { class: "line1" },
          h("span", {}, `#${it.issueId}`),
          it.status ? h("span", { class: `pill${it.closed ? " closed" : ""}` }, it.status) : null,
          // コンパクトでは、関係のラベルを3行目ではなく、ここ(ステータスの隣)に出す
          compact ? it.relations.map((r) => h("span", { class: "tag" }, t(RELATION_LABELS[r]))) : null,
          showServer ? h("span", {}, it.serverName) : null,
          h("span", { class: "spacer" }),
          h("span", { class: "time" }, formatShort(it.updatedOn)),
          // マウスを乗せると、時刻の代わりに操作のボタンを出す
          h("button", { type: "button", class: "row-more", "aria-label": t("list.rowMenu"), title: t("list.rowMenu"),
            "aria-haspopup": "menu", onClick: (e) => {
              e.stopPropagation();
              const r = e.currentTarget.getBoundingClientRect();
              this.menu(r.right - 4, r.bottom + 4, it);
            } }, "⋯"),
          it.unread ? h("span", { class: "badge", "aria-label": t("tree.unreadCount", { count: it.unread }) }, countText(it.unread)) : null),
        h("div", { class: "subject" }, it.subject),
        compact ? null : h("div", { class: "line3" },
          it.relations.map((r) => h("span", { class: "tag" }, t(RELATION_LABELS[r]))),
          preview ? h("span", { class: "preview" }, preview) : null)));
    }
    this.el.scrollTop = scroll;
  }

  compact() {
    return this.app.settings.list_density === "compact";
  }

  /**
   * 絞り込みの要約。既定から変えている条件だけを並べる(並べ替えの「未読を上に」は、絞り込みではないので含めない)。
   * 検索語は長いと省略して、全文は title に入れる。
   * @param {string} rel 関係での絞り込み(all なら含めない)
   */
  renderSummary(rel) {
    const version = this.app.selection.kind === "version";
    const search = this.search.trim();
    /** @type {{text: string, grow?: boolean}[]} */
    const parts = [];
    if (search) parts.push({ text: t("list.summary.search", { term: search }), grow: true });
    if (rel !== "all" && !this.relationGroup.hidden) parts.push({ text: t("list.summary.relation", { name: t(`list.filter.${rel}`) }) });
    // 「クローズ済みも表示」の既定は、チケット一覧では「しない」、バージョンでは「する」
    const showClosed = !!this.app.settings[this.showClosedKey()];
    if (showClosed !== version) parts.push({ text: t(showClosed ? "list.summary.closedShown" : "list.summary.closedHidden") });
    this.summary.hidden = !parts.length;
    clear(this.summary);
    if (!parts.length) return;
    this.summary.title = [t("list.summary.label"), parts.map((p) => p.text).join(" / ")].join(" ");
    this.summary.append(h("span", { class: "k" }, t("list.summary.label")));
    parts.forEach((p, i) => {
      if (i) this.summary.append(h("span", { class: "sep" }, "/"));
      this.summary.append(h("span", { class: p.grow ? "term" : "f" }, p.text));
    });
  }

  /** 「クローズ済みも表示」の設定のキー: マイルストーンを開いているときは、チケット一覧と分けて覚える */
  showClosedKey() {
    return this.app.selection.kind === "version" ? "version_show_closed" : "show_closed";
  }

  /** 絞り込みのボタンのうち、どこかのプロジェクトで表示している(表示するチケットに含まれる)関係のもの */
  shownFilters() {
    const vis = this.app.tree.data?.relationsInUse || RELATIONS;
    const has = { all: true, assigned: vis.includes("assigned") || vis.includes("group"),
      watching: vis.includes("watching"), authored: vis.includes("authored") };
    return Object.keys(has).filter((k) => has[k]);
  }

  /** 今の絞り込み。表示しないことにした関係で絞り込んでいたら「すべて」に戻す */
  relationFilter() {
    const rel = this.app.settings.relation_filter || "all";
    return this.shownFilters().includes(rel) ? rel : "all";
  }

  markSelected() {
    const cur = this.app.current;
    for (const li of this.el.querySelectorAll(".issue")) {
      const on = !!cur && Number(li.dataset.server) === cur.serverId && Number(li.dataset.issue) === cur.issueId;
      li.classList.toggle("selected", on);
      li.setAttribute("aria-selected", String(on));
      if (on) li.scrollIntoView({ block: "nearest" });
    }
  }

  focusSearch() {
    this.searchInput.focus();
    this.searchInput.select();
  }

  // ↑↓: 前後のチケット、Home/End: 先頭・末尾のチケットを開く
  onKey(e) {
    const last = this.issues.length - 1;
    const cur = this.app.current;
    const idx = this.issues.findIndex((i) => cur && i.serverId === cur.serverId && i.issueId === cur.issueId);
    const next = { ArrowDown: idx + 1, ArrowUp: idx - 1, Home: 0, End: last }[e.key];
    if (next === undefined) return;
    e.preventDefault();
    const it = this.issues[Math.max(0, Math.min(last, next))];
    if (it) this.app.openIssue(it.serverId, it.issueId);
  }

  menu(x, y, it) {
    contextMenu(x, y, [
      { label: t("menu.markRead"), disabled: !it.unread, onClick: () => call("markRead", { serverId: it.serverId, issueId: it.issueId }) },
      { label: t("menu.markUnread"), onClick: () => call("markUnread", { serverId: it.serverId, issueId: it.issueId }) },
      { sep: true },
      ...this.app.openMenuItems(null, { open: (external) => this.app.openIssueInBrowser(it.serverId, it.issueId, null, { external }) }),
    ]);
  }
}
