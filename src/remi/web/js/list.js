// 中央ペイン: チケット一覧(設計書 §5.1)
import { call } from "./api.js";
import { clear, countText, debounce, h, spinner } from "./dom.js";
import { formatShort, t } from "./i18n.js";
import { icon } from "./icons.js";
import { RELATIONS } from "./tree.js";
import { closeMenu, contextMenu, isMenuOpen, popover } from "./ui.js";

const RELATION_LABELS = { assigned: "list.assigned", group: "list.group", watching: "list.watching", authored: "list.authored" };

/** 並べ替えの項目(上から、メニューに出す順)と、その項目に切り替えたときの順序の初期値(降順なのは更新日時と優先度) */
const SORT_KEYS = ["updated", "number", "subject", "status", "tracker", "priority", "due"];
const SORT_DEFAULT_DESC = { updated: true, priority: true };
const DEFAULT_SORT = { key: "updated", desc: true };
/** 絞り込みの項目(ポップアップに並べる順) */
const GROUPS = ["status", "tracker", "priority", "assignee", "due"];
/** 属性の絞り込みの設定(list_filter)のキー */
const FILTER_KEYS = { tracker: "trackers", priority: "priorities", assignee: "assignees", due: "due" };
const EMPTY_FILTER = { trackers: [], priorities: [], assignees: [], due: [] };
const ME = "@me";
const NOBODY = "@none";
/** 並べ替えのタグを、警告の色にする優先度(Redmine の標準の名前) */
/** この幅(px)より狭いときは、見た目を詰める */
const NARROW_WIDTH = 300;
const URGENT = new Set(["緊急", "至急", "Immediate", "Urgent"]);

export class IssueList {
  constructor(app) {
    this.app = app;
    this.issues = [];
    /** サーバーが返した並び(未読を上にしたもの) */
    this.raw = [];
    /** 並び順の上で「未読」として扱うチケット(読んで既読になっても、会話を開いている間は元の位置に置く) */
    this.frozenUnread = new Set();
    /** 絞り込みの選択肢と件数(listIssues が返したもの)と、0件のときの、条件を当てる前の件数 */
    this.facets = {};
    this.scopeTotal = null;
    /** 絞り込みのポップアップで開いている項目 */
    this.filterOpen = "status";
    this.assigneeQuery = "";
    this.filterPop = null;
    this.searchPop = null;
    this.sortMenu = null;
    /** 並び順を決める条件(選択・検索・絞り込み・並べ替え)。変わったら、開いている間の「並びの固定」をやめて、新しい並びにする */
    this.condKey = "";
    /** ボタンを押した瞬間に、その小さなウィンドウが開いていたか(外側のクリックで閉じたあと、開き直さないため) */
    this.wasOpen = false;
    this.title = h("h2", {});
    // パンくずと件数。狭いときは、パンくずを隠し、件数を数字だけにする(CSS。件数の data-short)
    this.crumbPath = h("span", { class: "crumb-path" });
    this.crumbSep = h("span", { class: "crumb-sep" });
    this.count = h("span", { class: "count" });
    this.crumb = h("span", { class: "crumb" }, this.crumbPath, this.crumbSep, this.count);

    // 検索・並べ替え・絞り込み・表示密度のボタン。検索・並べ替え・絞り込みは、条件があるときだけ色が付く
    const button = (cls, iconName, onClick, popup = null) => h("button", { type: "button", class: `lbtn ${cls}`, "aria-pressed": "false",
      // 開いている小さなウィンドウは、このボタンのクリック(外側のクリック)で先に閉じる。そのクリックでは、開き直さない
      onMousedown: () => { this.wasOpen = popup ? isMenuOpen(popup()) : false; },
      onClick: () => { const was = this.wasOpen; this.wasOpen = false; if (!was) onClick(); } }, icon(iconName));
    this.searchButton = button("lbtn-search", "search", () => this.openSearch(), () => this.searchPop);
    this.sortButton = button("lbtn-sort", "sort", () => this.openSortMenu(), () => this.sortMenu);
    this.filterButton = button("lbtn-filter", "filter", () => this.toggleFilter(), () => this.filterPop);
    this.densityButton = button("lbtn-density", "density",
      () => app.saveSetting("list_density", this.compact() ? "standard" : "compact").then(() => this.render()));
    this.densityButton.setAttribute("aria-label", t("list.density"));
    this.densityButton.title = t("list.density");

    // 検索ボックスは、🔍を押したときの小さなウィンドウに入れる。検索語は保存して、次回も使う
    const save = debounce(async () => {
      await app.saveSetting("list_search", this.searchInput.value);
      this.refresh();
    }, 200);
    this.searchInput = h("input", { type: "search", placeholder: t("list.searchPlaceholder"), "aria-label": t("list.search"),
      onInput: save,
      onKeydown: (e) => {
        if (e.key === "Escape" && this.searchInput.value) {
          // 検索語があるときは、消す(閉じない)。空のときは、そのまま閉じる
          e.preventDefault();
          e.stopPropagation();
          this.searchInput.value = "";
          save.flush();
        } else if (e.key === "Enter") {
          e.preventDefault();
          save.flush();
          this.closePopups();
        } else if (e.key === "ArrowDown") {
          e.preventDefault();
          save.flush();
          this.closePopups();
          this.el.focus();
        }
      } });

    // 関係での絞り込み(すべて / 担当 / ウォッチ / 作成)
    this.relationButtons = ["all", "assigned", "watching", "authored"].map((value) =>
      h("button", { type: "button", dataset: { value }, "aria-pressed": "false",
        // 絞り込みは、真ん中の一覧だけに効く(ツリーの件数・未読数は変えない)
        onClick: () => app.saveSetting("relation_filter", value).then(() => this.refresh()) },
        t(`list.filter.${value}`)));
    this.relationGroup = h("div", { class: "segmented", role: "group", "aria-label": t("list.filter.label") }, this.relationButtons);
    // いま有効な条件(検索・絞り込み・並べ替え)。✕で1つずつ外せる
    this.chipsEl = h("div", { class: "cchips", hidden: true });
    this.el = h("ul", { class: "issue-list", role: "listbox", "aria-label": t("list.label"), tabindex: "0",
      onKeydown: (e) => this.onKey(e) });
    this.root = h("section", { class: "pane pane-list", "aria-label": t("list.label") },
      h("div", { class: "list-header" },
        h("div", { class: "list-title" }, this.title,
          h("span", { class: "title-right" }, this.searchButton, this.sortButton, this.filterButton, this.densityButton, this.crumb)),
        this.relationGroup,
        this.chipsEl),
      this.el);
    // 一覧の幅が狭いときは、ヘッダ・行・絞り込みのポップアップを詰める(CSS の .narrow)
    new ResizeObserver(() => this.root.classList.toggle("narrow", this.isNarrow())).observe(this.root);
  }

  /** 一覧のペインが狭いか(最小幅の240px付近で、見た目を詰める) */
  isNarrow() {
    return this.root.getBoundingClientRect().width < NARROW_WIDTH;
  }

  // --- 条件(設定に保存して、次回も使う) ---------------------------------------------
  get version() {
    return this.app.selection.kind === "version";
  }

  searchText() {
    return this.app.settings.list_search || "";
  }

  /** ステータスの絞り込み。チケット一覧とマイルストーンは、別々に覚える(既定も違う: 未完了 / すべて) */
  statusKey() {
    return this.version ? "version_status" : "list_status";
  }

  statusDefault() {
    return { mode: this.version ? "all" : "open", names: [] };
  }

  status() {
    const s = this.app.settings[this.statusKey()] || this.statusDefault();
    return { mode: s.mode, names: s.names || [] };
  }

  statusIsDefault() {
    const s = this.status();
    return !s.names.length && s.mode === this.statusDefault().mode;
  }

  filters() {
    const f = this.app.settings.list_filter || {};
    return { trackers: f.trackers || [], priorities: f.priorities || [], assignees: f.assignees || [], due: f.due || [] };
  }

  selected(group) {
    return group === "status" ? this.status().names : this.filters()[FILTER_KEYS[group]];
  }

  sort() {
    return { ...DEFAULT_SORT, ...(this.app.settings.list_sort || {}) };
  }

  sortIsDefault() {
    const s = this.sort();
    return s.key === DEFAULT_SORT.key && s.desc === DEFAULT_SORT.desc;
  }

  compact() {
    return this.app.settings.list_density === "compact";
  }

  /** 絞り込み(検索・関係・ステータス・属性)が、既定から変わっているか。0件のときの「すべて解除」を出すかの判断に使う */
  hasFilterConditions() {
    return !!this.searchText().trim() || this.relationFilter() !== "all" || !this.statusIsDefault()
      || GROUPS.some((g) => g !== "status" && this.selected(g).length);
  }

  async refresh() {
    const sel = this.app.selection;
    const f = this.filters();
    const st = this.status();
    const so = this.sort();
    const params = { search: this.searchText(), statusMode: st.mode, statuses: st.names, trackers: f.trackers,
      priorities: f.priorities, assignees: f.assignees, due: f.due, sort: so.key, desc: so.desc };
    if (sel.serverId) params.serverId = sel.serverId;
    if (sel.projectId) params.projectId = sel.projectId;
    if (sel.kind === "unread") params.unreadOnly = true;
    if (sel.kind === "version") params.versionId = sel.versionId;
    params.relation = this.relationFilter();
    const token = Symbol();
    this._token = token;
    const res = await call("listIssues", params);
    if (this._token !== token) return; // 古い結果は捨てる
    // 並べ替え・絞り込みなどを変えたときは、開いている間の並びの固定(stabilize)をやめて、サーバーの並びにする
    const condKey = JSON.stringify(params);
    if (condKey !== this.condKey) {
      this.condKey = condKey;
      this.issues = [];
    }
    this.raw = res.issues;
    this.facets = res.facets || {};
    this.scopeTotal = res.scopeTotal ?? null;
    this.render();
    this.updateFilterPopup();
  }

  /**
   * 「未読を上に」の並びを、会話を開いている間は動かさない。開いたチケットは既読になって下へ移るので、
   * そのまま並べると、↓で順に読んでいるとき未読のチケットを飛ばしてしまう。会話を閉じたら、並びに反映する。
   */
  stabilize(issues) {
    const key = (i) => `${i.serverId}:${i.issueId}`;
    if (!this.app.settings.unread_first || this.version) return issues;
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
    const countText_ = t("list.count", { count: this.issues.length });
    this.crumbPath.textContent = crumb;
    this.crumbSep.textContent = crumb ? " · " : "";
    this.count.textContent = countText_;
    this.count.dataset.short = String(this.issues.length);
    this.crumb.title = [crumb, countText_].filter(Boolean).join(" · ");
    const rel = this.relationFilter();
    const shown = this.shownFilters();
    for (const b of this.relationButtons) {
      b.setAttribute("aria-pressed", String(b.dataset.value === rel));
      b.hidden = !shown.includes(b.dataset.value);
    }
    // 絞り込める関係が1つだけなら「すべて」と同じなので、ボタンごと隠す。バージョンでは関係で絞り込まない
    this.relationGroup.hidden = shown.length <= 2 || this.version;
    this.renderButtons();
    this.renderChips();
    const compact = this.compact();
    this.el.classList.toggle("compact", compact);
    const scroll = this.el.scrollTop;
    clear(this.el);
    if (!this.issues.length) {
      this.el.append(this.emptyItem());
      return;
    }
    const current = this.app.current;
    const showServer = !this.app.selection.serverId && new Set(this.issues.map((i) => i.serverId)).size > 1;
    const sortKey = this.sort().key;
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
          // ステータスで並べているときは、ステータスのラベルを、並べ替えの色にする
          it.status ? h("span", { class: `pill${it.closed ? " closed" : ""}${sortKey === "status" ? " sorted" : ""}` }, it.status) : null,
          this.sortTag(it, sortKey),
          // コンパクトでは、関係のラベルを3行目ではなく、ここ(ステータスの隣)に出す
          compact ? it.relations.map((r) => h("span", { class: "tag" }, t(RELATION_LABELS[r]))) : null,
          showServer ? h("span", {}, it.serverName) : null,
          h("span", { class: "spacer" }),
          this.timeEl(it.updatedOn),
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

  /** 更新日時。狭いときは、午前・午後を省いた短い文字にする(CSS。data-short) */
  timeEl(iso) {
    const full = formatShort(iso);
    const short = formatShort(iso, new Date(), { short: true });
    return h("span", { class: "time", dataset: short !== full ? { short } : null }, full);
  }

  /** 並べ替えの値を、ステータスの隣のタグで見せる(優先度・トラッカー・期日のとき)。緊急・期限切れは警告の色 */
  sortTag(it, key) {
    if (key === "priority" && it.priority) {
      return h("span", { class: `tag skey${URGENT.has(it.priority) ? " warn" : ""}` }, t("list.tag.priority", { value: it.priority }));
    }
    if (key === "tracker" && it.tracker) return h("span", { class: "tag skey" }, t("list.tag.tracker", { value: it.tracker }));
    if (key === "due") {
      return h("span", { class: `tag skey${it.overdue ? " warn" : ""}` },
        t("list.tag.due", { value: it.dueDate ? shortDate(it.dueDate) : t("list.tag.dueNone") }));
    }
    return null;
  }

  /** 表示するものがないとき。監視対象のチケットがあるのに出ないなら「条件に一致するものがない」(すべて解除つき) */
  emptyItem() {
    // まだチケットを取得していないバージョンは、空ではなく取得中と出す
    const sel = this.app.selection;
    const loading = !this.searchText() && sel.kind === "version"
      && this.app.tree.findVersion(sel.serverId, sel.versionId)?.synced === false
      && !this.app.tree.failedVersions.has(sel.versionId);
    if (loading) return h("li", { class: "empty loading" }, spinner(), t("list.versionLoading"));
    if (!(this.scopeTotal > 0)) return h("li", { class: "empty" }, t("list.empty"));
    let action = null;
    if (this.hasFilterConditions()) {
      action = h("button", { type: "button", class: "btn", onClick: () => this.resetAll() }, t("list.f.clearAll"));
    } else if (this.status().mode === "open") {
      // 既定の「未完了」だけでは、クローズ済みしかないとき。「すべて解除」では何も変わらないので、ステータスを「すべて」にする
      action = h("button", { type: "button", class: "btn", onClick: () => this.save({ [this.statusKey()]: { mode: "all", names: [] } }) },
        t("list.f.showAllStatus"));
    }
    return h("li", { class: "empty" }, t("list.noMatches"), action);
  }

  /** 検索・並べ替え・絞り込みのボタンの色(条件があるときだけ)と、説明 */
  renderButtons() {
    const search = !!this.searchText().trim();
    const so = this.sort();
    const sortOn = !this.sortIsDefault();
    const filterOn = !this.statusIsDefault() || GROUPS.some((g) => g !== "status" && this.selected(g).length);
    const set = (btn, on, label) => {
      btn.setAttribute("aria-pressed", String(on));
      btn.setAttribute("aria-label", label);
      btn.title = label;
    };
    set(this.searchButton, search, t("list.searchButton"));
    set(this.sortButton, sortOn, sortOn ? t("list.sort.current", { name: t(`list.sort.${so.key}`), dir: so.desc ? "↓" : "↑" }) : t("list.sort.label"));
    set(this.filterButton, filterOn, t("list.f.title"));
    const compact = this.compact();
    this.densityButton.setAttribute("aria-pressed", String(compact));
  }

  /** いま有効な条件をチップにする。関係(ボタンが常に見えている)は出さない */
  conditionChips() {
    /** @type {{kind: string, name: string, label: string, onRemove: () => void}[]} */
    const chips = [];
    const search = this.searchText().trim();
    if (search) {
      chips.push({ kind: "search", name: t("list.search"), label: t("list.chip.search", { term: search }),
        onRemove: () => this.save({ list_search: "" }) });
    }
    if (!this.statusIsDefault()) {
      chips.push(this.groupChip("status", t("list.f.status"), () => this.save({ [this.statusKey()]: this.statusDefault() })));
    }
    for (const g of GROUPS.slice(1)) {
      if (this.selected(g).length) {
        chips.push(this.groupChip(g, t(`list.f.${g}`), () => this.save({ list_filter: { ...this.filters(), [FILTER_KEYS[g]]: [] } })));
      }
    }
    if (!this.sortIsDefault()) {
      const so = this.sort();
      chips.push({ kind: "sort", name: t("list.sort.label"), label: t("list.chip.sort", { name: t(`list.sort.${so.key}`), dir: so.desc ? "↓" : "↑" }),
        onRemove: () => this.save({ list_sort: DEFAULT_SORT }) });
    }
    return chips;
  }

  groupChip(group, name, onRemove) {
    return { kind: "filter", name, label: t("list.chip.group", { name, values: this.groupValueText(group) }), onRemove };
  }

  renderChips() {
    const chips = this.conditionChips();
    clear(this.chipsEl);
    this.chipsEl.hidden = !chips.length;
    for (const c of chips) {
      this.chipsEl.append(h("span", { class: `cchip cchip-${c.kind}`, title: c.label },
        h("span", { class: "t" }, c.label),
        h("button", { type: "button", "aria-label": t("list.chip.remove", { name: c.name }), onClick: c.onRemove }, icon("close"))));
    }
    if (chips.length) this.chipsEl.append(h("button", { type: "button", class: "link", onClick: () => this.resetAll() }, t("list.f.clearAll")));
  }

  /** 設定をまとめて保存して、一覧を読み直す */
  async save(values) {
    for (const [k, v] of Object.entries(values)) await this.app.saveSetting(k, v);
    if ("list_search" in values) this.searchInput.value = values.list_search;
    await this.refresh();
  }

  /** 既定から変わっているものを、すべて既定に戻す(検索・関係・ステータス・属性・並べ替え。「未読を上に」は触らない) */
  async resetAll() {
    const values = {};
    if (this.searchText()) values.list_search = "";
    if (this.relationFilter() !== "all") values.relation_filter = "all";
    if (!this.statusIsDefault()) values[this.statusKey()] = this.statusDefault();
    if (GROUPS.some((g) => g !== "status" && this.selected(g).length)) values.list_filter = EMPTY_FILTER;
    if (!this.sortIsDefault()) values.list_sort = DEFAULT_SORT;
    await this.save(values);
  }

  // --- 検索(🔍を押したときの小さなウィンドウ) ------------------------------------------
  openSearch() {
    if (isMenuOpen(this.searchPop)) {
      this.searchInput.focus();
      return;
    }
    this.searchInput.value = this.searchText();
    this.searchPop = h("div", { class: "search-pop" },
      h("label", { class: "search" }, icon("search"), this.searchInput),
      h("div", { class: "hint" }, t("list.searchHint")));
    const pane = this.root.getBoundingClientRect();
    const b = this.searchButton.getBoundingClientRect();
    popover(this.searchPop, { x: pane.left + 8, y: b.bottom + 4, width: pane.width - 16 });
    this.searchInput.focus();
    this.searchInput.select();
  }

  focusSearch() {
    this.openSearch();
  }

  closePopups() {
    closeMenu();
  }

  // --- 並べ替え(⇅のメニュー) ---------------------------------------------------------
  openSortMenu() {
    const so = this.sort();
    const b = this.sortButton.getBoundingClientRect();
    const items = [
      { label: t("list.sort.items"), hint: true },
      ...SORT_KEYS.map((k) => ({ label: t(`list.sort.${k}`), checked: so.key === k,
        onClick: () => this.save({ list_sort: { key: k, desc: so.key === k ? so.desc : !!SORT_DEFAULT_DESC[k] } }) })),
      { sep: true },
      { segmented: { label: t("list.sort.order"), value: so.desc ? "desc" : "asc",
        options: [{ label: t("list.sort.asc"), value: "asc" }, { label: t("list.sort.desc"), value: "desc" }],
        onChange: (v) => this.save({ list_sort: { key: so.key, desc: v === "desc" } }) } },
    ];
    // 「未読を上に」(全体の設定)がオンのときは、未読が先に並ぶ(マイルストーンのチケットは未読として数えない)
    if (this.app.settings.unread_first && !this.version) items.push({ label: t("list.sort.unreadFirst"), hint: true });
    this.sortMenu = contextMenu(b.right - 230, b.bottom + 4, items);
  }

  // --- 絞り込み(⏷のポップアップ) -----------------------------------------------------
  toggleFilter() {
    if (isMenuOpen(this.filterPop)) {
      this.closePopups();
      return;
    }
    this.filterPop = h("div", { class: `fpop${this.isNarrow() ? " narrow" : ""}` });
    this.assigneeQuery = ""; // 開き直したときに、前の検索語が残って「選べる値はありません」と見えないように
    this.renderFilterPopup();
    const pane = this.root.getBoundingClientRect();
    const b = this.filterButton.getBoundingClientRect();
    popover(this.filterPop, { x: pane.left + 8, y: b.bottom + 4, width: pane.width - 16 });
  }

  updateFilterPopup() {
    if (this.filterPop && isMenuOpen(this.filterPop)) this.renderFilterPopup();
  }

  valueLabel(group, value) {
    if (group === "assignee") return value === ME ? t("list.f.me") : value === NOBODY ? t("list.f.nobody") : value;
    if (group === "due") return t(`list.f.due.${value}`);
    return value;
  }

  /** 選んでいる内容の文字(見出しの右側と、チップに使う) */
  groupValueText(group) {
    const names = this.selected(group);
    if (group === "status" && !names.length) return t(`list.f.${this.status().mode}`);
    return names.map((v) => this.valueLabel(group, v)).join("・");
  }

  /** ポップアップの中でフォーカスしているもの(描き直したあとに、同じものへ戻すための印)と、担当者の検索欄の選択範囲 */
  focusMark() {
    const el = document.activeElement;
    if (!el || !this.filterPop?.contains(el)) return null;
    if (el.matches(".find input")) return { kind: "find", start: el.selectionStart, end: el.selectionEnd };
    const d = el.dataset;
    if (d.group && el.matches(".frow")) return { kind: "frow", key: d.group };
    if (d.mode) return { kind: "mode", key: d.mode };
    if (d.group && d.value !== undefined) return { kind: "fit", group: d.group, key: d.value };
    return null;
  }

  restoreFocus(mark) {
    if (!mark) return;
    const pop = this.filterPop;
    const found = {
      find: () => pop.querySelector(".find input"),
      frow: () => [...pop.querySelectorAll(".frow")].find((e) => e.dataset.group === mark.key),
      mode: () => [...pop.querySelectorAll(".modes .o")].find((e) => e.dataset.mode === mark.key),
      fit: () => [...pop.querySelectorAll(".fit")].find((e) => e.dataset.group === mark.group && e.dataset.value === mark.key),
    }[mark.kind]();
    found?.focus();
    if (mark.kind === "find" && found) found.setSelectionRange(mark.start ?? found.value.length, mark.end ?? found.value.length);
  }

  renderFilterPopup() {
    const pop = this.filterPop;
    // 担当者の名前を入力している間(日本語の変換中を含む)は、入力欄を作り直さない。選択肢のリストだけを更新する
    if (pop.querySelector(".find input") && document.activeElement === pop.querySelector(".find input")) {
      this.renderAssigneeList();
      return;
    }
    const mark = this.focusMark();
    const scroll = pop.parentElement ? pop.scrollTop : 0;
    const listScroll = pop.querySelector(".plist")?.scrollTop ?? 0;
    clear(pop);
    GROUPS.forEach((g, i) => {
      if (i) pop.append(h("div", { class: "sep" }));
      const open = this.filterOpen === g;
      const on = g === "status" ? !this.statusIsDefault() : this.selected(g).length > 0;
      const summary = g === "status" || on ? this.groupValueText(g) : t("list.f.all");
      pop.append(h("button", { type: "button", class: "frow", "aria-expanded": String(open), dataset: { group: g },
        onClick: () => { this.filterOpen = open ? null : g; this.renderFilterPopup(); } },
        h("span", {}, t(`list.f.${g}`)), h("span", { class: `v${on ? " on" : ""}` }, summary), h("span", { class: "chev" }, open ? "▴" : "▾")));
      if (open) pop.append(h("div", { class: "fbody" }, this.groupBody(g)));
    });
    pop.append(h("div", { class: "sep" }),
      h("div", { class: "ffoot" }, h("button", { type: "button", class: "link", onClick: () => this.resetAll() }, t("list.f.clearAll"))));
    pop.scrollTop = scroll;
    const list = pop.querySelector(".plist");
    if (list) list.scrollTop = listScroll;
    this.restoreFocus(mark);
  }

  groupBody(group) {
    const parts = [];
    if (group === "status") {
      const st = this.status();
      const def = this.statusDefault().mode;
      // 大分類(未完了 / クローズ済み / すべて)。個別のステータスを選んでいる間は、どれも選ばれていない
      parts.push(h("div", { class: "modes", role: "group", "aria-label": t("list.f.status") },
        ["open", "closed", "all"].map((m) => h("button", { type: "button", class: `o${!st.names.length && st.mode === m ? " on" : ""}`, dataset: { mode: m },
          "aria-pressed": String(!st.names.length && st.mode === m), onClick: () => this.save({ [this.statusKey()]: { mode: m, names: [] } }) },
          t(`list.f.${m}`), m === def ? h("i", {}, t("list.f.default")) : null))),
        h("div", { class: "sep inner" }),
        h("div", { class: "note" }, t("list.f.note")));
    }
    if (group === "assignee") {
      parts.push(h("label", { class: "find" }, icon("search"),
        h("input", { type: "search", placeholder: t("list.f.findName"), "aria-label": t("list.f.findName"), value: this.assigneeQuery,
          // 入力欄は作り直さず、リストだけを更新する(日本語の変換を途切れさせない)
          onInput: (e) => { this.assigneeQuery = e.target.value; this.renderAssigneeList(); } })));
    }
    parts.push(h("div", { class: "plist-slot", dataset: { group } }, this.listBody(group)));
    return parts;
  }

  /** 選択肢のリスト(または、選べる値がないという案内) */
  listBody(group) {
    const items = this.facets[group] || [];
    const q = group === "assignee" ? this.assigneeQuery.trim().toLowerCase() : "";
    const shown = items.filter((x) => !q || this.valueLabel(group, x.value).toLowerCase().includes(q));
    if (!shown.length) return h("div", { class: "note" }, t("list.f.noValues"));
    const selected = this.selected(group);
    return h("div", { class: `plist${group === "assignee" ? " scroll" : ""}`, role: "group", "aria-label": t(`list.f.${group}`) },
      shown.map((x, i) => {
        const on = selected.includes(x.value);
        const pinned = group === "assignee" && (x.value === ME || x.value === NOBODY);
        const nextPinned = group === "assignee" && shown[i + 1] && !(shown[i + 1].value === ME || shown[i + 1].value === NOBODY);
        return h("button", { type: "button", class: `fit${on ? " on" : ""}${pinned && nextPinned ? " pin" : ""}`, role: "menuitemcheckbox",
          dataset: { group, value: x.value }, "aria-checked": String(on), onClick: () => this.toggleValue(group, x.value) },
          h("span", { class: "chk" }, on ? icon("check") : ""), this.valueLabel(group, x.value), h("span", { class: "cnt" }, String(x.count)));
      }));
  }

  /** 担当者のリストだけを描き直す(検索欄はそのまま) */
  renderAssigneeList() {
    const slot = this.filterPop?.querySelector('.plist-slot[data-group="assignee"]');
    if (!slot) return;
    const scroll = slot.querySelector(".plist")?.scrollTop ?? 0;
    slot.replaceChildren(this.listBody("assignee"));
    const list = slot.querySelector(".plist");
    if (list) list.scrollTop = scroll;
  }

  /** 選択肢を1つ選ぶ・外す。ステータスの個別は、どれか選ぶと大分類が外れ、すべて外すと既定に戻る */
  toggleValue(group, value) {
    const cur = this.selected(group);
    const next = cur.includes(value) ? cur.filter((v) => v !== value) : [...cur, value];
    if (group === "status") return this.save({ [this.statusKey()]: { mode: this.statusDefault().mode, names: next } });
    return this.save({ list_filter: { ...this.filters(), [FILTER_KEYS[group]]: next } });
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

/** 期日(YYYY-MM-DD)を、今年なら 11/25、ほかの年なら 2027/1/5 の形にする */
function shortDate(iso) {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return y === new Date().getFullYear() ? `${m}/${d}` : `${y}/${m}/${d}`;
}
