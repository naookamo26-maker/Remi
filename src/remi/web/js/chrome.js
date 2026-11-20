// 切り離したRedmineのウィンドウの上の操作行(設計書 §5.4)。ページの上に、Windows側の部品としてこの画面を置く。
// 戻る・進む・再読み込み、URLの表示(入力はしない)、「タブに戻す」。タブの操作行と同じ部品・同じ色の仕組みで描くので、
// ライト/ダークの切り替えにも追従する。
import { call, subscribe } from "./api.js";
import { h } from "./dom.js";
import { setCatalog, t } from "./i18n.js";
import { displayUrl, navButtons } from "./tabs.js";

const darkQuery = matchMedia("(prefers-color-scheme: dark)");

async function main() {
  const root = document.getElementById("app");
  const windowId = Number(new URLSearchParams(location.search).get("w"));
  const boot = await call("bootstrap");
  setCatalog(boot.lang, boot.catalog, boot.fallbackCatalog);
  document.documentElement.lang = boot.lang;
  let settings = boot.settings;
  let state = (await call("getWindowState", { windowId }).catch(() => null)) || {};

  const applyTheme = () => {
    const dark = settings.theme === "dark" || (settings.theme === "system" && darkQuery.matches);
    document.documentElement.classList.toggle("theme-dark", dark);
    document.documentElement.classList.toggle("theme-light", !dark);
  };
  const act = (action) => call("windowAction", { windowId, action }).catch(() => {});
  const render = () => {
    const bar = h("div", { class: "tab-tools" },
      ...navButtons({ canBack: !!state.canBack, canForward: !!state.canForward }, act),
      h("span", { class: "tab-url", title: state.url || "" }, displayUrl(state.url)),
      state.attachable ? h("button", { type: "button", class: "btn", onClick: () => act("attach") }, t("tabs.attach")) : null);
    root.replaceChildren(bar);
  };

  applyTheme();
  render();
  darkQuery.addEventListener("change", applyTheme);
  subscribe((e) => {
    if (e.type === "windowState" && e.windowId === windowId) {
      state = e;
      render();
    } else if (e.type === "settingsChanged" && e.key === "theme") {
      // アプリの設定でライト/ダークを切り替えたら、この操作行も追従する
      call("getSettings").then((s) => { settings = s; applyTheme(); }).catch(() => {});
    }
  });
}

main().catch((e) => console.error(e));
