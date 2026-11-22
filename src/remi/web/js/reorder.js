// 上下に並んだ行を、ドラッグ&ドロップで並べ替える(サーバーの並び。設計書 §3.1)。
// ツリーと設定のサーバー一覧で使う。行は描き直されてもよい(rows() で、そのときの行を探し直す)。
import { h } from "./dom.js";

/** これより動かしたら、ドラッグを始める(ふつうのクリックと区別する) */
const THRESHOLD = 5;

/**
 * 行を押したとき(pointerdown)に呼ぶ。少し動かすと、ドラッグを始める。
 * @param {PointerEvent} e
 * @param {object} o
 * @param {number[]} o.ids 今の並び
 * @param {number} o.sourceId 動かす行
 * @param {() => [number, HTMLElement][]} o.rows 今の行(ID と要素)。上から順に
 * @param {() => HTMLElement} o.area この中で離したときだけ並べ替える(外で離したら取り消す)
 * @param {Node} o.label ドラッグ中にポインタに付いていく名札の中身
 * @param {() => void} [o.onBegin] ドラッグを始めたとき(ツリーは、すべて折りたたんで描き直す)
 * @param {(order: number[]|null) => void} o.onEnd 終わったとき。取り消し・並びが変わらないときは null
 * @returns {{active: () => boolean, repaint: () => void}|null} repaint: 行を描き直したあとに、線と印を付け直す
 */
export function startReorder(e, o) {
  if (e.button !== 0 || o.ids.length < 2) return null;
  const start = { x: e.clientX, y: e.clientY };
  let active = false;
  let slot = o.ids.indexOf(o.sourceId); // 動かす行を除いた並びの中で、何番目に入れるか
  let ghost = null;

  const others = () => o.rows().filter(([id]) => id !== o.sourceId);
  const repaint = () => {
    if (!active) return;
    for (const el of document.querySelectorAll(".drop-line")) el.remove();
    for (const [id, el] of o.rows()) el.classList.toggle("drag-source", id === o.sourceId);
    const rest = others();
    if (!rest.length) return;
    const line = h("li", { class: "drop-line", role: "none", "aria-hidden": "true" });
    if (slot < rest.length) rest[slot][1].before(line);
    else rest[rest.length - 1][1].after(line);
  };
  const place = (x, y) => {
    if (ghost) Object.assign(ghost.style, { left: `${x + 12}px`, top: `${y + 8}px` });
    const rest = others();
    // 行の見出し(ツリーではサーバーの行。設定の一覧では行そのもの)の上半分より上なら、その行の前に入れる
    let next = rest.length;
    for (let i = 0; i < rest.length; i++) {
      const head = rest[i][1].querySelector(":scope > .node") || rest[i][1];
      const r = head.getBoundingClientRect();
      if (y < r.top + r.height / 2) {
        next = i;
        break;
      }
    }
    if (next !== slot) {
      slot = next;
      repaint();
    }
  };
  const order = () => {
    const rest = o.ids.filter((id) => id !== o.sourceId);
    rest.splice(slot, 0, o.sourceId);
    return rest;
  };
  const end = (commit) => {
    stop();
    if (!active) return;
    active = false;
    ghost?.remove();
    document.body.classList.remove("reordering");
    for (const el of document.querySelectorAll(".drop-line")) el.remove();
    for (const el of document.querySelectorAll(".drag-source")) el.classList.remove("drag-source");
    const next = order();
    // ドラッグのあとに来るクリック(行の選択・開閉)を、1回だけ無視する
    const swallow = (ev) => { ev.stopPropagation(); ev.preventDefault(); };
    addEventListener("click", swallow, { capture: true, once: true });
    setTimeout(() => removeEventListener("click", swallow, { capture: true }), 0);
    o.onEnd(commit && next.some((id, i) => id !== o.ids[i]) ? next : null);
  };
  const move = (ev) => {
    if (!active) {
      if (Math.abs(ev.clientX - start.x) < THRESHOLD && Math.abs(ev.clientY - start.y) < THRESHOLD) return;
      active = true;
      document.body.classList.add("reordering");
      ghost = h("div", { class: "drag-ghost", "aria-hidden": "true" }, o.label);
      document.body.append(ghost);
      o.onBegin?.();
      repaint();
    }
    ev.preventDefault();
    place(ev.clientX, ev.clientY);
  };
  const up = (ev) => {
    const r = o.area().getBoundingClientRect();
    const inside = ev.clientX >= r.left && ev.clientX <= r.right && ev.clientY >= r.top && ev.clientY <= r.bottom;
    end(inside);
  };
  const key = (ev) => {
    if (ev.key === "Escape" && active) {
      ev.preventDefault();
      ev.stopPropagation(); // ダイアログを閉じない
      end(false);
    }
  };
  const cancel = () => end(false);
  function stop() {
    removeEventListener("pointermove", move);
    removeEventListener("pointerup", up);
    removeEventListener("pointercancel", cancel);
    removeEventListener("keydown", key, true);
    removeEventListener("blur", cancel);
  }
  addEventListener("pointermove", move);
  addEventListener("pointerup", up);
  addEventListener("pointercancel", cancel);
  addEventListener("keydown", key, true);
  addEventListener("blur", cancel);
  return { active: () => active, repaint };
}

/** 並びの中で、id を delta(-1: 上へ / +1: 下へ)だけ動かした並び。動かせなければ null(キーボードでの並べ替え) */
export function moved(ids, id, delta) {
  const i = ids.indexOf(id);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= ids.length) return null;
  const next = [...ids];
  [next[i], next[j]] = [next[j], next[i]];
  return next;
}
