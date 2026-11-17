// 中央ペイン: チケット一覧(設計書 §5.1)
import { call } from "./api.js";
import { clear, debounce, h } from "./dom.js";
import { formatShort, t } from "./i18n.js";
import { icon } from "./icons.js";
import { contextMenu } from "./ui.js";

const RELATION_LABELS = { assigned: "list.assigned", group: "list.group", watching: "list.watching", authored: "list.authored" };

export class IssueList {
  constructor(app) {
    this.app = app;
    this.issues = [];
    this.search = "";
    this.title = h("h2", {});
    this.crumb = h("span", { class: "crumb" });
    this.searchInput = h("input", { type: "search", placeholder: t("list.searchPlaceholder"), "aria-label": t("list.search"),
      onInput: debounce(() => { this.search = this.searchInput.value; this.refresh(); }, 200) });
    this.unreadFirst = h("input", { type: "checkbox", onChange: () => app.saveSetting("unread_first", this.unreadFirst.checked).then(() => this.refresh()) });
    this.showClosed = h("input", { type: "checkbox", onChange: () => app.saveSetting("show_closed", this.showClosed.checked).then(() => this.refresh()) });
    this.el = h("ul", { class: "issue-list", role: "listbox", "aria-label": t("list.label"), tabindex: "0",
      onKeydown: (e) => this.onKey(e) });
    this.root = h("section", { class: "pane pane-list", "aria-label": t("list.label") },
      h("div", { class: "list-header" },
        h("div", { class: "list-title" }, this.title, this.crumb),
        h("label", { class: "search" }, icon("search"), this.searchInput),
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
          it.unread ? h("span", { class: "badge", "aria-label": t("tree.unreadCount", { count: it.unread }) }, it.unread) : null),
        h("div", { class: "subject" }, it.subject),
        h("div", { class: "line3" },
          it.relations.map((r) => h("span", { class: "tag" }, t(RELATION_LABELS[r]))),
          preview ? h("span", { class: "preview" }, preview) : null)));
    }
    this.el.scrollTop = scroll;
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

  onKey(e) {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const cur = this.app.current;
    let idx = this.issues.findIndex((i) => cur && i.serverId === cur.serverId && i.issueId === cur.issueId);
    idx = Math.max(0, Math.min(this.issues.length - 1, idx + (e.key === "ArrowDown" ? 1 : -1)));
    const it = this.issues[idx];
    if (it) this.app.openIssue(it.serverId, it.issueId);
  }

  menu(e, it) {
    contextMenu(e.clientX, e.clientY, [
      { label: t("menu.markRead"), disabled: !it.unread, onClick: () => call("markRead", { serverId: it.serverId, issueId: it.issueId }) },
      { label: t("menu.markUnread"), onClick: () => call("markUnread", { serverId: it.serverId, issueId: it.issueId }) },
      { sep: true },
      { label: t("menu.openInBrowser"), onClick: () => this.app.openIssueInBrowser(it.serverId, it.issueId) },
    ]);
  }
}
