// DOMを安全に組み立てる小さな関数。文字列は必ずテキストとして入れる(innerHTMLは使わない)。
// 例外: サーバーでサニタイズ済みの本文は html() で入れる。

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// サーバーでサニタイズ済みのHTMLを入れる
export function html(el, sanitized) {
  el.innerHTML = sanitized;
  return el;
}

export function clear(el) {
  while (el.firstChild) el.firstChild.remove();
  return el;
}

// flush: 待たずにすぐ実行する。flushPending: 実行を待っているときだけ、すぐ実行する
export function debounce(fn, ms) {
  let timer = null;
  let pending = null; // 待っている呼び出しの引数
  const wrapped = (...args) => {
    clearTimeout(timer);
    pending = args;
    timer = setTimeout(() => { pending = null; fn(...args); }, ms);
  };
  wrapped.flush = (...args) => {
    clearTimeout(timer);
    pending = null;
    fn(...args);
  };
  wrapped.flushPending = () => {
    if (pending) wrapped.flush(...pending);
  };
  wrapped.cancel = () => {
    clearTimeout(timer);
    pending = null;
  };
  return wrapped;
}

// 未読数のバッジの文字(大きな数は 999+ にまとめる)
export function countText(n) {
  return n > 999 ? "999+" : String(n);
}

let toastTimer = null;
export function toast(message) {
  document.querySelector(".toast")?.remove();
  const el = h("div", { class: "toast", role: "status" }, message);
  document.body.append(el);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.remove(), 3500);
}
