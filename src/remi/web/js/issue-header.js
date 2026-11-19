// 会話ビューのヘッダ: 件名・パンくず・ウォッチ・Redmineで開く、ステータス・担当者・優先度の選択(設計書 §6.2)
import { call } from "./api.js";
import { clear, h, toast } from "./dom.js";
import { formatTime, t } from "./i18n.js";
import { icon } from "./icons.js";
import { contextMenu } from "./ui.js";

/** @typedef {import("./types.js").ConversationData} ConversationData */
/** @typedef {import("./types.js").FieldKey} FieldKey */

/** ヘッダで変更できる項目と、その表示名のキー(カスタムフィールドの名前は FieldChange.name に持つ) */
export const FIELD_LABELS = {
  status_id: "field.status", assigned_to_id: "field.assignee", priority_id: "field.priority",
  tracker_id: "field.tracker", category_id: "field.category", fixed_version_id: "field.version",
  parent_issue_id: "field.parent", start_date: "field.startDate", due_date: "field.dueDate",
  done_ratio: "field.doneRatio", estimated_hours: "field.estimatedHours",
};

/** 変更の表示名(チップなど) @param {string} key @param {import("./types.js").FieldChange} [change] */
export function fieldLabel(key, change) {
  return change?.name || t(FIELD_LABELS[key] || key);
}

const sameValue = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

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
    // 件名の幅を確保するため、ウォッチ(星)と「Redmineで開く」は、アイコンだけのボタンを縦に並べる。
    // 開くボタンは、右の「▾」でブラウザで開く・URLのコピーを選べる(Shift+クリックでも既定のブラウザで開く)
    const openBtn = h("button", { type: "button", class: "btn icon-only", title: t("conv.openRedmineHint"),
      "aria-label": t("conv.openRedmine"),
      onClick: (e) => app.openRedmine(d.url, { external: e.shiftKey }) }, icon("external"));
    const moreBtn = h("button", { type: "button", class: "btn more", "aria-label": t("conv.moreActions"),
      title: t("conv.moreActions"), "aria-haspopup": "menu",
      onClick: (e) => {
        const r = e.currentTarget.getBoundingClientRect();
        contextMenu(r.right - 4, r.bottom + 4, [
          { label: t("conv.openInBrowser"), onClick: () => app.openRedmine(d.url, { external: true }) },
          { label: t("conv.copyUrl"), onClick: () => navigator.clipboard.writeText(d.url).then(() => toast(t("conv.copied"))) },
        ]);
      } }, "▾");
    const watchBtn = h("button", { type: "button", class: `btn icon-only watch${d.watching ? " on" : ""}`,
      "aria-pressed": String(d.watching), "aria-label": d.watching ? t("conv.watching") : t("conv.watch"),
      title: d.watching ? t("conv.unwatchHint") : t("conv.watchHint"),
      onClick: () => {
        // 結果は watchFinished / dataChanged イベントで届き、ヘッダが描き直される
        watchBtn.disabled = true;
        call("setWatching", { serverId: d.serverId, issueId: d.issueId, watching: !d.watching })
          .catch((e) => { watchBtn.disabled = false; toast(t(`errors.${e.code}`)); });
      } }, icon("star"));
    /** 書き込めないときの説明(選択肢を読み込んでから表示する) */
    this.readOnlyNote = h("span", { class: "read-only-note", hidden: true });
    this.selects = {
      status_id: this.fieldSelect("status_id", d.status, d.statusId),
      assigned_to_id: this.fieldSelect("assigned_to_id", d.assignee || t("conv.none"), d.assignedToId),
      priority_id: this.fieldSelect("priority_id", d.priority, d.priorityId),
    };
    const field = (key) => h("label", {}, t(FIELD_LABELS[key]), this.selects[key]);
    const rel = (d.relations || []).map((r) => t(`list.${r}`)).join(" / ");
    // 「詳細」: ほかの項目(トラッカー・日付・カスタムフィールドなど)。開いた状態はすべてのチケットで共通に覚える
    const open = !!app.settings.issue_details_open;
    this.detailsToggle = h("button", { type: "button", class: "btn details-toggle", "aria-expanded": String(open),
      onClick: () => this.toggleDetails() }, open ? "▾ " : "▸ ", t("conv.details"));
    this.detailsBox = h("div", { class: "details-fields", hidden: !open });
    const crumb = [d.serverName, ...d.breadcrumb].join(" › ");
    const el = h("header", { class: "conv-header" },
      h("div", { class: "top" },
        h("div", { class: "titles" },
          h("div", { class: "crumb", title: crumb }, crumb, " · ", `${d.tracker} #${d.issueId}`),
          h("h1", {}, d.subject)),
        h("div", { class: "actions" }, watchBtn, h("span", { class: "split" }, openBtn, moreBtn))),
      h("div", { class: "fields" },
        field("status_id"), field("assigned_to_id"), field("priority_id"),
        h("span", { class: "static" }, `${t("conv.updated")} `, h("b", {}, formatTime(d.updatedOn))),
        rel ? h("span", { class: "static" }, `${t("conv.yourRelation")} `, h("b", {}, rel)) : null,
        this.detailsToggle,
        this.readOnlyNote),
      this.detailsBox);
    this.fill();
    return el;
  }

  // --- 詳細 -----------------------------------------------------------------------
  toggleDetails() {
    const app = this.conv.app;
    const open = !app.settings.issue_details_open;
    // 保存の完了を待たずに表示へ反映する
    app.settings = { ...app.settings, issue_details_open: open };
    app.saveSetting("issue_details_open", open);
    this.detailsToggle.setAttribute("aria-expanded", String(open));
    this.detailsToggle.firstChild.textContent = open ? "▾ " : "▸ ";
    this.detailsBox.hidden = !open;
    if (open && !this.conv.details) this.conv.loadDetails();
    else this.fillDetails();
  }

  showDetailsLoading() {
    if (this.detailsBox && !this.conv.details) {
      this.detailsBox.replaceChildren(h("span", { class: "muted" }, t("conv.detailsLoading")));
    }
  }

  showDetailsError(code) {
    this.detailsBox?.replaceChildren(h("span", { class: "muted" }, t("conv.detailsFailed", { reason: t(`errors.${code}`) })));
  }

  /** 「詳細」の項目を描く。書き込めるか(editOptions)が分かるまでは、操作できないようにしておく */
  fillDetails() {
    const det = this.conv.details;
    if (!this.detailsBox || !det || this.detailsBox.hidden) return;
    const p = this.conv.editOptions?.permissions;
    const denied = !p ? t("conv.detailsLoading") : !p.editFields ? this.deniedReason(p) : null;
    const box = this.detailsBox;
    box.replaceChildren();
    for (const f of det.fields) {
      const label = f.label || t(FIELD_LABELS[f.key]);
      const reason = denied || (!f.editable ? t(`conv.${f.reason === "custom_field_unknown" ? "cfUnknown" : "cfUnsupported"}`) : null);
      box.append(h("label", { class: "detail" }, h("span", { class: "name" }, label), this.detailInput(f, label, reason)));
    }
    if (!det.customFieldsKnown && det.fields.some((f) => f.key.startsWith("cf:"))) {
      box.append(h("div", { class: "help" }, t("conv.cfUnknownHelp")));
    }
  }

  /** 項目1つの入力欄。reason があれば、今の値だけを表示して操作できないようにする */
  detailInput(f, label, reason) {
    const change = this.conv.changes[f.key];
    const chosen = change ? change.value : f.value;
    const display = (v) => this.displayValue(f, v);
    const record = (value) => {
      const next = sameValue(value, f.value) ? null
        : { value, label: display(value) || t("conv.none"), from: display(f.value) || t("conv.none"), name: f.label || null };
      this.conv.setChange(f.key, next, { refill: false });
      return next;
    };
    if (reason) {
      return h("span", { class: "static", title: reason }, h("b", {}, display(f.value) || "-"));
    }
    let el;
    if (f.type === "select") {
      el = h("select", { onChange: () => { el.classList.toggle("changed", !!record(f.options[el.selectedIndex].value)); } },
        f.options.map((o) => h("option", { selected: sameValue(o.value, chosen) }, this.optionLabel(f, o) || t("conv.none"))));
    } else if (f.type === "multiselect") {
      // 複数選択: 押すとチェックのメニューを開く
      let current = [...(chosen || [])];
      el = h("button", { type: "button", class: "multi", onClick: (e) => {
        const r = e.currentTarget.getBoundingClientRect();
        contextMenu(r.left, r.bottom + 4, f.options.map((o) => ({ label: o.label, checked: current.includes(o.value), keepOpen: true,
          onClick: () => {
            current = current.includes(o.value) ? current.filter((x) => x !== o.value)
              : f.options.map((x) => x.value).filter((x) => x === o.value || current.includes(x));
            el.textContent = display(current) || t("conv.none");
            el.classList.toggle("changed", !!record(current));
            return current.includes(o.value);
          } })));
      } }, display(current) || t("conv.none"));
    } else {
      const types = { date: "date", issue: "number", int: "number", float: "number", text: "text" };
      el = h("input", { type: types[f.type] || "text", value: chosen ?? "", "aria-label": label,
        step: f.type === "float" ? "any" : f.type === "int" || f.type === "issue" ? "1" : null,
        min: f.type === "issue" ? "1" : null, placeholder: f.type === "issue" ? "#" : null,
        // 入力のたびに記録する(届いた更新でヘッダが描き直されても、書きかけの値が残るように)
        onInput: () => { el.classList.toggle("changed", !!record(this.parseInput(f, el.value))); } });
    }
    el.setAttribute("aria-label", label);
    el.classList.toggle("changed", !!change);
    return el;
  }

  /** 入力欄の文字を、送る値にする。標準項目の数値は数に、カスタムフィールドは文字列のままにする */
  parseInput(f, text) {
    const v = text.trim();
    if (!v) return null;
    if (f.key.startsWith("cf:")) return v;
    if (f.type === "issue") return Number.isInteger(Number(v)) && Number(v) > 0 ? Number(v) : f.value;
    if (f.type === "float") return Number.isFinite(Number(v)) ? Number(v) : f.value;
    return v;
  }

  optionLabel(f, o) {
    if (f.format === "bool" && o.value !== null) return o.value === "1" ? t("conv.yes") : t("conv.no");
    return o.label;
  }

  /** 値の表示名(選択肢があれば、その名前) */
  displayValue(f, v) {
    if (v === null || v === undefined || v === "") return "";
    if (Array.isArray(v)) return v.map((x) => this.displayValue(f, x)).join(", ");
    const o = (f.options || []).find((x) => sameValue(x.value, v));
    if (o) return this.optionLabel(f, o);
    return f.type === "issue" ? `#${v}` : String(v);
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
    this.fillDetails();
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
