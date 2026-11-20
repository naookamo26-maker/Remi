// ウィンドウ全体のタブ: 固定の「Remi」のタブ(本体の画面)と、Redmineのタブ(設計書 §5.4)。
// Redmineのタブがないときは、タブの列も出さない(これまでと同じ画面)。ページの表示はWindows側の部品が行い、
// ここでは、タブの列と操作行(戻る・進む・再読み込み・URLの表示・別ウィンドウへ)だけを描く。
import { call } from "./api.js";
import { debounce, h, spinner, toast } from "./dom.js";
import { t } from "./i18n.js";

/** @typedef {{id: number, url: string, title: string, canBack: boolean, canForward: boolean, loading: boolean}} TabInfo */

/** URLの表示用: 先頭の http(s):// を省く */
export function displayUrl(url) {
  return String(url || "").replace(/^https?:\/\//i, "");
}

export class Tabs {
  /** @param {import("./main.js").App} app */
  constructor(app) {
    this.app = app;
    /** @type {{tabs: TabInfo[], active: number|null}} */
    this.state = { tabs: [], active: null };
    this.bar = h("div", { class: "apptabs", role: "tablist" });
    this.tools = h("div", { class: "tab-tools", hidden: true });
    this.el = h("div", { class: "tabs-chrome", hidden: true }, this.bar, this.tools);
    // 実際の高さを、Windows側に知らせる(Redmineのタブを選んでいる間、本体の画面をこの高さだけにする)
    this.report = debounce(() => {
      const height = this.el.getBoundingClientRect().height;
      if (height > 0) call("setTabChromeHeight", { height }).catch(() => {});
    }, 50);
    new ResizeObserver(() => this.report()).observe(this.el);
  }

  async load() {
    try {
      this.set(await call("getTabs"));
    } catch {
      // タブを使えない環境: 何も出さない
    }
  }

  /** @param {{tabs: TabInfo[], active: number|null}} state */
  set(state) {
    this.state = state;
    const browsing = state.active !== null && state.tabs.some((x) => x.id === state.active);
    document.body.classList.toggle("browse", browsing);
    this.el.hidden = !state.tabs.length;
    this.render();
  }

  get activeTab() {
    return this.state.tabs.find((x) => x.id === this.state.active) || null;
  }

  render() {
    const { tabs, active } = this.state;
    const remiOn = active === null || !this.activeTab;
    this.bar.replaceChildren(
      h("div", { class: `atab fixed${remiOn ? " on" : ""}`, role: "tab", "aria-selected": String(remiOn), tabindex: "0",
        onClick: () => this.activate(null), onKeydown: (e) => this.onTabKey(e, null) }, h("span", { class: "t" }, t("tabs.remi"))),
      ...tabs.map((x) => this.tabEl(x, x.id === active)));
    const tab = this.activeTab;
    this.tools.hidden = !tab;
    if (tab) this.renderTools(tab);
    this.report();
  }

  /** @param {TabInfo} x @param {boolean} on */
  tabEl(x, on) {
    const title = x.title || displayUrl(x.url);
    return h("div", { class: `atab${on ? " on" : ""}`, role: "tab", "aria-selected": String(on), tabindex: "0", title,
      dataset: { tab: x.id },
      onClick: () => this.activate(x.id),
      // 中ボタンで閉じる(ブラウザと同じ)
      onAuxclick: (e) => { if (e.button === 1) { e.preventDefault(); this.close(x.id); } },
      onKeydown: (e) => this.onTabKey(e, x.id) },
    x.loading ? spinner() : null,
    h("span", { class: "t" }, title),
    h("button", { type: "button", class: "x", "aria-label": t("tabs.close"), title: t("tabs.close"),
      onClick: (e) => { e.stopPropagation(); this.close(x.id); } }, "×"));
  }

  /** @param {TabInfo} tab */
  renderTools(tab) {
    const nav = (label, glyph, action, enabled) => h("button", { type: "button", class: "nb", "aria-label": label, title: label,
      disabled: !enabled, onClick: () => call("tabAction", { tabId: tab.id, action }).catch(() => {}) }, glyph);
    this.tools.replaceChildren(
      nav(t("tabs.back"), "←", "back", tab.canBack),
      nav(t("tabs.forward"), "→", "forward", tab.canForward),
      nav(t("tabs.reload"), "⟳", "reload", true),
      // URLは表示のみ(入力はしない)
      h("span", { class: "tab-url", title: tab.url }, displayUrl(tab.url)),
      h("button", { type: "button", class: "btn", title: t("tabs.detachHint"), onClick: () => this.detach(tab.id) }, t("tabs.detach")));
  }

  onTabKey(e, id) {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      this.activate(id);
    }
  }

  activate(id) {
    if (id === this.state.active) return;
    call("activateTab", { tabId: id }).catch(() => {});
  }

  close(id) {
    call("closeTab", { tabId: id }).catch(() => {});
  }

  detach(id) {
    call("detachTab", { tabId: id }).catch((e) => toast(t(`errors.${e.code || "not_supported"}`)));
  }

  /** Ctrl+W: 選んでいるRedmineのタブを閉じる。閉じたら true */
  closeActive() {
    const tab = this.activeTab;
    if (!tab) return false;
    this.close(tab.id);
    return true;
  }
}
