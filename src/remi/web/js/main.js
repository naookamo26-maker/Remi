// 画面の入り口。画面の本体は開発ステップ6で実装する。
import { call } from "./api.js";

function applyTheme(theme) {
  const dark = theme === "dark" || (theme === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("theme-dark", dark);
  document.documentElement.classList.toggle("theme-light", !dark);
}

async function start() {
  const boot = await call("bootstrap");
  document.documentElement.lang = boot.lang;
  applyTheme(boot.settings.theme);
  const app = document.getElementById("app");
  app.textContent = `Remi ${boot.version}`;
  app.removeAttribute("aria-busy");
}

start();
