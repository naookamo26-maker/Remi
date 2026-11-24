// 選択状態の「戻る・進む」の履歴(ツリーの選択+開いているチケットの組)。DOMには触れない
import { selectionKey } from "./tree.js";

const MAX_ENTRIES = 50;

/** @typedef {{selection: object, current: {serverId: number, issueId: number}|null}} NavEntry */

/** 同じ状態か(選択と、開いているチケットが同じ) */
export function entryKey(e) {
  return `${selectionKey(e.selection)}|${e.current ? `${e.current.serverId}:${e.current.issueId}` : ""}`;
}

export class NavHistory {
  constructor() {
    /** @type {NavEntry[]} */
    this.entries = [];
    this.index = -1;
  }

  get canBack() {
    return this.index > 0;
  }

  get canForward() {
    return this.index >= 0 && this.index < this.entries.length - 1;
  }

  /** 今の状態を積む。直前と同じなら積まない。戻ったあとに積むと、進む側の履歴は捨てる */
  push(entry) {
    const top = this.entries[this.index];
    if (top && entryKey(top) === entryKey(entry)) return;
    this.entries.splice(this.index + 1, this.entries.length, entry);
    if (this.entries.length > MAX_ENTRIES) this.entries.splice(0, this.entries.length - MAX_ENTRIES);
    this.index = this.entries.length - 1;
  }
}
