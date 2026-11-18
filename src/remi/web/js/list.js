// 中央ペイン: チケット一覧(設計書 §5.1)
import { call } from "./api.js";
import { clear, countText, debounce, h } from "./dom.js";
import { formatShort, t } from "./i18n.js";
import { icon } from "./icons.js";
import { visibleRelations } from "./tree.js";
import { contextMenu } from "./ui.js";

const RELATION_LABELS = { assigned: "list.assigned", group: "list.group", watching: "list.watching", authored: "list.authored" };

export class IssueList {
  constructor(app) {
    this.app = app;
    this.issues = [];
    this.search = "";
    this.title = h("h2", {});
    this.crumb = h("span", { class: "crumb" });
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
    this.showClosed = h("input", { type: "checkbox", onChange: () => app.saveSetting("show_closed", this.showClosed.checked).then(() => this.refresh()) });
    // 関係での絞り込み(すべて / 担当 / ウォッチ / 作成)
    this.relationButtons = ["all", "assigned", "watching", "authored"].map((value) =>
      h("button", { type: "button", dataset: { value }, "aria-pressed": "false",
        onClick: () => app.saveSetting("relation_filter", value).then(() => this.refresh()) },
        t(`list.filter.${value}`)));
    this.relationGroup = h("div", { class: "segmented", role: "group", "aria-label": t("list.filter.label") }, this.relationButtons);
    this.el = h("ul", { class: "issue-list", role: "listbox", "aria-label": t("list.label"), tabindex: "0",
      onKeydown: (e) => this.onKey(e) });
    this.root = h("section", { class: "pane pane-list", "aria-label": t("list.label") },
      h("div", { class: "list-header" },
        h("div", { class: "list-title" }, this.title, this.crumb),
        h("label", { class: "search" }, icon("search"), this.searchInput),
        this.relationGroup,
        h("div", { class: "toggles" },
          h("label", {}, this.unreadFirst, t("list.unreadFirst")),
          h("label", {}, this.showClosed, t("list.showClosed")))),
      this.el);
  }

  async refresh() {
    const sel = this.app.selection;
    const params = { search: this.search };
    if (sel.serverId) params.serverId = sel.serverId;
    if (sel.projectId) params.projectId = sel.projectId;
    if (sel.kind === "unread") params.unreadOnly = true;
    params.relation = this.relationFilter();
    const token = Symbol();
    this._token = token;
    const res = await call("listIssues", params);
    if (this._token !== token) return; // 古い結果は捨てる
    this.issues = res.issues;
    this.render();
  }

  render() {
    const { title, crumb } = this.app.selectionLabels();
    this.title.textContent = title;
    this.crumb.textContent = [crumb, t("list.count", { count: this.issues.length })].filter(Boolean).join(" · ");
    this.unreadFirst.checked = !!this.app.settings.unread_first;
    this.showClosed.checked = !!this.app.settings.show_closed;
    const rel = this.relationFilter();
    const shown = this.shownFilters();
    for (const b of this.relationButtons) {
      b.setAttribute("aria-pressed", String(b.dataset.value === rel));
      b.hidden = !shown.includes(b.dataset.value);
    }
    // 絞り込める関係が1つだけなら「すべて」と同じなので、ボタンごと隠す
    this.relationGroup.hidden = shown.length <= 2;
    const scroll = this.el.scrollTop;
    clear(this.el);
    if (!this.issues.length) {
      this.el.append(h("li", { class: "empty" }, this.search ? t("list.noMatches") : t("list.empty")));
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
        onContextmenu: (e) => { e.preventDefault(); this.menu(e, it); },
      },
        h("div", { class: "line1" },
          h("span", {}, `#${it.issueId}`),
          it.status ? h("span", { class: `pill${it.closed ? " closed" : ""}` }, it.status) : null,
          showServer ? h("span", {}, it.serverName) : null,
          h("span", { class: "spacer" }),
          h("span", {}, formatShort(it.updatedOn)),
          it.unread ? h("span", { class: "badge", "aria-label": t("tree.unreadCount", { count: it.unread }) }, countText(it.unread)) : null),
        h("div", { class: "subject" }, it.subject),
        h("div", { class: "line3" },
          it.relations.map((r) => h("span", { class: "tag" }, t(RELATION_LABELS[r]))),
          preview ? h("span", { class: "preview" }, preview) : null)));
    }
    this.el.scrollTop = scroll;
  }

  /** 絞り込みのボタンのうち、表示するチケット(ツリーの上で選ぶ)に含まれる関係のもの */
  shownFilters() {
    const vis = visibleRelations(this.app.settings);
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

  menu(e, it) {
    contextMenu(e.clientX, e.clientY, [
      { label: t("menu.markRead"), disabled: !it.unread, onClick: () => call("markRead", { serverId: it.serverId, issueId: it.issueId }) },
      { label: t("menu.markUnread"), onClick: () => call("markUnread", { serverId: it.serverId, issueId: it.issueId }) },
      { sep: true },
      { label: t("menu.openRedmine"), onClick: () => this.app.openIssueInBrowser(it.serverId, it.issueId) },
      { label: t("menu.openInBrowser"), onClick: () => this.app.openIssueInBrowser(it.serverId, it.issueId, null, { external: true }) },
    ]);
  }
}
