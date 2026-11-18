// 会話ビューのヘッダ: 件名・パンくず・ウォッチ・Redmineで開く、ステータス・担当者・優先度の選択(設計書 §6.2)
import { call } from "./api.js";
import { clear, h, toast } from "./dom.js";
import { formatTime, t } from "./i18n.js";
import { icon } from "./icons.js";
import { contextMenu } from "./ui.js";

/** @typedef {import("./types.js").ConversationData} ConversationData */
/** @typedef {import("./types.js").FieldKey} FieldKey */

/** ヘッダで変更できる項目と、その表示名のキー */
export const FIELD_LABELS = { status_id: "field.status", assigned_to_id: "field.assignee", priority_id: "field.priority" };

export class IssueHeader {
  /** @param {import("./conversation.js").Conversation} conv 選択肢(editOptions)と送信前の変更(changes)を持つ */
  constructor(conv) {
    this.conv = conv;
    /** @type {Record<FieldKey, HTMLSelectElement>|null} */
    this.selects = null;
  }

  /**
   * ヘッダを作る。描き直すたびに新しい要素を返す。
   * @param {ConversationData} d
   */
  render(d) {
    const app = this.conv.app;
    const openBtn = h("button", { type: "button", class: "btn", title: t("conv.openRedmineHint"),
      onClick: (e) => app.openRedmine(d.url, { external: e.shiftKey }) }, icon("external"), t("conv.openRedmine"));
    const moreBtn = h("button", { type: "button", class: "btn", "aria-label": t("conv.moreActions"),
      onClick: (e) => {
        const r = e.currentTarget.getBoundingClientRect();
        contextMenu(r.left, r.bottom + 4, [
          { label: t("conv.openInBrowser"), onClick: () => app.openRedmine(d.url, { external: true }) },
          { label: t("conv.copyUrl"), onClick: () => navigator.clipboard.writeText(d.url).then(() => toast(t("conv.copied"))) },
        ]);
      } }, "▾");
    const watchBtn = h("button", { type: "button", class: `btn${d.watching ? " on" : ""}`, "aria-pressed": String(d.watching),
      title: d.watching ? t("conv.unwatchHint") : t("conv.watchHint"),
      onClick: () => {
        // 結果は watchFinished / dataChanged イベントで届き、ヘッダが描き直される
        watchBtn.disabled = true;
        call("setWatching", { serverId: d.serverId, issueId: d.issueId, watching: !d.watching })
          .catch((e) => { watchBtn.disabled = false; toast(t(`errors.${e.code}`)); });
      } }, d.watching ? t("conv.watching") : t("conv.watch"));
    /** 書き込めないときの説明(選択肢を読み込んでから表示する) */
    this.readOnlyNote = h("span", { class: "read-only-note", hidden: true });
    this.selects = {
      status_id: this.fieldSelect("status_id", d.status, d.statusId),
      assigned_to_id: this.fieldSelect("assigned_to_id", d.assignee || t("conv.none"), d.assignedToId),
      priority_id: this.fieldSelect("priority_id", d.priority, d.priorityId),
    };
    const field = (key) => h("label", {}, t(FIELD_LABELS[key]), this.selects[key]);
    const rel = (d.relations || []).map((r) => t(`list.${r}`)).join(" / ");
    const crumb = [d.serverName, ...d.breadcrumb].join(" › ");
    const el = h("header", { class: "conv-header" },
      h("div", { class: "top" },
        h("div", { class: "titles" },
          h("div", { class: "crumb", title: crumb }, crumb, " · ", `${d.tracker} #${d.issueId}`),
          h("h1", {}, d.subject)),
        h("div", { class: "actions" }, watchBtn, h("span", { class: "split" }, openBtn, moreBtn))),
      h("div", { class: "fields" },
        field("status_id"), field("assigned_to_id"), field("priority_id"),
        // Redmine上でチケットが最後に更新された日時(このアプリが情報を取得した日時ではない)
        h("span", { class: "static", title: t("conv.updatedHint") }, `${t("conv.updated")} `, h("b", {}, formatTime(d.updatedOn))),
        rel ? h("span", { class: "static" }, `${t("conv.yourRelation")} `, h("b", {}, rel)) : null,
        this.readOnlyNote));
    this.fill();
    return el;
  }

  // 選択肢が読み込まれるまでは、今の値だけを表示して操作できないようにしておく
  fieldSelect(key, label, value) {
    const sel = h("select", { disabled: true, "aria-label": t(FIELD_LABELS[key]),
      onChange: () => this.onChange(key, sel) });
    sel.append(h("option", { value: value ?? "" }, label || "-"));
    sel.dataset.current = value ?? "";
    return sel;
  }

  /** 読み込んだ選択肢と、送信前の変更を選択欄に反映する(選択肢がまだなければ何もしない) */
  fill() {
    const o = this.conv.editOptions;
    const changes = this.conv.changes;
    if (!o || !this.selects) return;
    const lists = {
      status_id: o.statuses.map((x) => [x.id, x.name]),
      assigned_to_id: [["", t("conv.none")], ...(o.me ? [[o.me, t("conv.assignToMe")]] : []),
        ...o.assignees.filter((a) => a.id !== o.me).map((a) => [a.id, a.group ? `${a.name} ${t("conv.groupSuffix")}` : a.name])],
      priority_id: o.priorities.map((x) => [x.id, x.name]),
    };
    const p = o.permissions;
    // 書き込めない項目は、今の値だけを表示して操作できないようにする(理由はツールチップで示す)
    const denied = {
      status_id: !p.editFields ? this.deniedReason(p) : !p.changeStatus ? t("conv.noStatusTransition") : null,
      assigned_to_id: !p.editFields ? this.deniedReason(p) : null,
      priority_id: !p.editFields ? this.deniedReason(p) : null,
    };
    for (const [key, sel] of Object.entries(this.selects)) {
      const current = o.current[key] ?? "";
      const chosen = changes[key] ? changes[key].value : current;
      if (denied[key]) {
        sel.disabled = true;
        sel.title = denied[key];
        continue;
      }
      clear(sel);
      for (const [v, label] of lists[key]) sel.append(h("option", { value: v, selected: String(v) === String(chosen ?? "") }, label));
      sel.dataset.current = current;
      sel.disabled = false;
      sel.title = "";
      sel.classList.toggle("changed", !!changes[key]);
    }
    const note = p.reason === "project_closed" ? t("conv.readOnlyProjectClosed")
      : !p.editFields && !p.comment ? t("conv.readOnly")
        : !p.editFields ? t("conv.commentOnly") : "";
    this.readOnlyNote.textContent = note;
    this.readOnlyNote.hidden = !note;
  }

  /** 項目を変えられない理由 @param {import("./types.js").WritePermissions} p */
  deniedReason(p) {
    return p.reason === "project_closed" ? t("conv.readOnlyProjectClosed") : t("conv.noEditPermission");
  }

  /** 選択肢を読み込めなかった: 理由をツールチップで示す(選択欄は操作できないまま) */
  showError(code) {
    for (const sel of Object.values(this.selects || {})) sel.title = t(`errors.${code}`);
  }

  onChange(key, sel) {
    const value = sel.value === "" ? null : Number(sel.value);
    const current = sel.dataset.current === "" ? null : Number(sel.dataset.current);
    let change = null;
    if (value !== current) {
      const fromOpt = [...sel.options].find((o) => String(o.value) === String(current ?? ""));
      change = { value, label: sel.selectedOptions[0].textContent, from: fromOpt ? fromOpt.textContent : "-" };
    }
    sel.classList.toggle("changed", !!change);
    this.conv.setChange(key, change, { refill: false });
  }
}
