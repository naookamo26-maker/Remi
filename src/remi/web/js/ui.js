// メニュー・ダイアログ・画像の拡大表示
import { h } from "./dom.js";
import { icon } from "./icons.js";
import { t } from "./i18n.js";

let openMenu = null;

export function closeMenu() {
  openMenu?.remove();
  openMenu = null;
}

// items: {label, onClick, disabled} | {sep:true} | {label, hint:true} | {segmented: {label, options:[{label, value}], value, onChange}, hint}
export function contextMenu(x, y, items) {
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
      menu.append(h("button", { type: "button", role: "menuitem", disabled: it.disabled,
        onClick: () => { closeMenu(); it.onClick(); } },
        it.checked !== undefined ? h("span", { class: "check" }, it.checked ? icon("check") : "") : null, it.label));
    }
  }
  document.body.append(menu);
  const r = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(4, Math.min(x, innerWidth - r.width - 4))}px`;
  menu.style.top = `${Math.max(4, Math.min(y, innerHeight - r.height - 4))}px`;
  openMenu = menu;
  menu.querySelector("button")?.focus();
}

addEventListener("mousedown", (e) => {
  if (openMenu && !openMenu.contains(e.target)) closeMenu();
});
addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeMenu();
});
addEventListener("blur", closeMenu);

// モーダルダイアログ。close() で閉じる。
export function modal({ title, body, footer = [], onClose = null, width = null }) {
  const previous = document.activeElement;
  const close = () => {
    backdrop.remove();
    removeEventListener("keydown", onKey, true);
    onClose?.();
    previous?.focus?.();
  };
  const onKey = (e) => {
    if (e.key === "Escape" && !openMenu) {
      e.stopPropagation();
      close();
    }
  };
  const dialog = h("div", { class: "dialog", role: "dialog", "aria-modal": "true", "aria-label": title, style: width ? { width } : null },
    h("header", {}, h("h2", {}, title),
      h("button", { type: "button", class: "btn icon-only", "aria-label": t("common.close"), onClick: () => close() }, icon("close"))),
    body,
    footer.length ? h("footer", {}, footer) : null);
  const backdrop = h("div", { class: "backdrop", onMousedown: (e) => { if (e.target === backdrop) close(); } }, dialog);
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

export function lightbox(src, alt) {
  const close = () => { box.remove(); removeEventListener("keydown", onKey, true); };
  const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  const box = h("div", { class: "lightbox", role: "dialog", "aria-label": alt || t("conv.image"), onClick: close },
    h("img", { src, alt: alt || "" }));
  document.body.append(box);
  addEventListener("keydown", onKey, true);
}
