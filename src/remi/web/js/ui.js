// メニュー・ダイアログ・画像の拡大表示
import { h } from "./dom.js";
import { icon } from "./icons.js";
import { t } from "./i18n.js";

let openMenu = null;
let onMenuClose = null;

export function closeMenu() {
  openMenu?.remove();
  openMenu = null;
  const f = onMenuClose;
  onMenuClose = null;
  f?.();
}

/** el が、いま開いているメニュー・小さなウィンドウ(popover)か */
export function isMenuOpen(el) {
  return !!el && openMenu === el;
}

/**
 * ボタンの下などに出す、小さなウィンドウ(検索・絞り込み)。メニューと同じ見た目で、外側のクリック・Esc・
 * ウィンドウの非アクティブ化で閉じる(同時に開くのは1つだけ)。
 * @param {HTMLElement} el 中身(クラス menu を付ける)
 * @param {{x: number, y: number, width?: number|null, onClose?: (() => void)|null}} opts x・y は左上の位置
 */
export function popover(el, { x, y, width = null, onClose = null }) {
  closeMenu();
  el.classList.add("menu", "popover");
  if (width) {
    el.style.width = `${width}px`;
    el.style.maxWidth = `${width}px`;
    el.style.minWidth = "0";
  }
  el.style.maxHeight = `${Math.max(160, innerHeight - y - 8)}px`;
  el.style.overflowY = "auto";
  document.body.append(el);
  const r = el.getBoundingClientRect();
  el.style.left = `${Math.max(4, Math.min(x, innerWidth - r.width - 4))}px`;
  el.style.top = `${Math.max(4, y)}px`;
  openMenu = el;
  onMenuClose = onClose;
}

// items: {label, onClick, disabled, checked, keepOpen} | {sep:true} | {label, hint:true}
//      | {segmented: {label, options:[{label, value}], value, onChange}, hint}
// keepOpen: 押してもメニューを閉じない(チェックを続けて切り替えるとき)。onClick が返した真偽値(Promise可)をチェックの状態にする
// focusLabel: 開いたときにフォーカスする項目の文言(開き直したときに、操作した項目へ戻すため)。省略すると先頭
export function contextMenu(x, y, items, { focusLabel = null } = {}) {
  closeMenu();
  const menu = h("div", { class: "menu", role: "menu" });
  for (const it of items) {
    if (it.sep) menu.append(h("div", { class: "sep", role: "separator" }));
    else if (it.segmented) {
      const s = it.segmented;
      menu.append(h("div", { class: "label" }, s.label));
      const seg = h("div", { class: "seg", role: "group", "aria-label": s.label });
      for (const o of s.options) {
        seg.append(h("button", { type: "button", "aria-pressed": String(o.value === s.value),
          onClick: () => { closeMenu(); s.onChange(o.value); } }, o.label));
      }
      menu.append(seg);
      if (it.hint) menu.append(h("div", { class: "hint" }, it.hint));
    } else if (it.hint) menu.append(h("div", { class: "hint" }, it.label));
    else {
      const checkable = it.checked !== undefined;
      const check = checkable ? h("span", { class: "check" }, it.checked ? icon("check") : "") : null;
      const button = h("button", { type: "button", role: checkable ? "menuitemcheckbox" : "menuitem", disabled: it.disabled,
        "aria-checked": checkable ? String(!!it.checked) : null,
        onClick: async () => {
          if (!it.keepOpen) {
            closeMenu();
            it.onClick();
            return;
          }
          const on = await it.onClick();
          if (typeof on === "boolean" && check) {
            check.replaceChildren(...(on ? [icon("check")] : []));
            button.setAttribute("aria-checked", String(on));
          }
        } }, check, it.label);
      menu.append(button);
    }
  }
  document.body.append(menu);
  const r = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(4, Math.min(x, innerWidth - r.width - 4))}px`;
  menu.style.top = `${Math.max(4, Math.min(y, innerHeight - r.height - 4))}px`;
  openMenu = menu;
  const buttons = [...menu.querySelectorAll("button")];
  (buttons.find((b) => focusLabel !== null && b.textContent === focusLabel) || buttons[0])?.focus();
}

addEventListener("mousedown", (e) => {
  if (openMenu && !openMenu.contains(e.target)) closeMenu();
});
addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeMenu();
});
addEventListener("blur", closeMenu);

// 開いているモーダルダイアログ(重なったときは、いちばん手前のものだけがEscで閉じる)
const modals = [];

// モーダルダイアログ。close() で閉じる。closeOnBackdrop が false なら、外側をクリックしても閉じない
// (入力の途中で、うっかり消さないように。Escと閉じるボタンでは閉じる)
export function modal({ title, body, footer = [], onClose = null, width = null, closeOnBackdrop = true }) {
  const previous = document.activeElement;
  const token = {};
  const close = () => {
    backdrop.remove();
    removeEventListener("keydown", onKey, true);
    const i = modals.indexOf(token);
    if (i >= 0) modals.splice(i, 1);
    onClose?.();
    previous?.focus?.();
  };
  const onKey = (e) => {
    if (e.key === "Escape" && !openMenu && modals.at(-1) === token) {
      e.stopPropagation();
      close();
    }
  };
  modals.push(token);
  const dialog = h("div", { class: "dialog", role: "dialog", "aria-modal": "true", "aria-label": title, style: width ? { width } : null },
    h("header", {}, h("h2", {}, title),
      h("button", { type: "button", class: "btn icon-only", "aria-label": t("common.close"), onClick: () => close() }, icon("close"))),
    body,
    footer.length ? h("footer", {}, footer) : null);
  const backdrop = h("div", { class: "backdrop", onMousedown: (e) => { if (closeOnBackdrop && e.target === backdrop) close(); } }, dialog);
  document.body.append(backdrop);
  addEventListener("keydown", onKey, true);
  setTimeout(() => dialog.querySelector("input, select, textarea, button.primary, button")?.focus(), 0);
  return { close, dialog };
}

export function confirmDialog(title, message, okLabel, { danger = false } = {}) {
  return new Promise((resolve) => {
    let result = false;
    const ok = h("button", { type: "button", class: danger ? "btn primary danger" : "btn primary",
      onClick: () => { result = true; m.close(); } }, okLabel);
    const cancel = h("button", { type: "button", class: "btn", onClick: () => m.close() }, t("common.cancel"));
    const m = modal({ title, body: h("div", { class: "dialog-body" }, h("p", { style: { margin: 0 } }, message)),
      footer: [cancel, ok], onClose: () => resolve(result), width: "min(460px, 100%)" });
    setTimeout(() => ok.focus(), 0);
  });
}

// 画像の拡大表示。どこをクリックしても、Esc・Enter・閉じるボタンでも閉じる。閉じたら元の場所へフォーカスを戻す
export function lightbox(src, alt) {
  const previous = document.activeElement;
  const close = () => {
    box.remove();
    removeEventListener("keydown", onKey, true);
    previous?.focus?.();
  };
  const onKey = (e) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      close();
    } else if (e.key === "Tab") {
      e.preventDefault(); // 操作できるのは閉じるボタンだけなので、フォーカスを外へ出さない
      closeButton.focus();
    }
  };
  const closeButton = h("button", { type: "button", class: "btn icon-only close", "aria-label": t("common.close"),
    title: t("common.close") }, icon("close"));
  const box = h("div", { class: "lightbox", role: "dialog", "aria-modal": "true", "aria-label": alt || t("conv.image"),
    onClick: close }, h("img", { src, alt: alt || "" }), closeButton);
  document.body.append(box);
  addEventListener("keydown", onKey, true);
  closeButton.focus();
}
