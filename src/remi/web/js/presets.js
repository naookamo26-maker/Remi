// 一覧の条件のプリセット: 保存・名前の変更・並べ替え・削除のダイアログ(設計書 §5.1)
import { h } from "./dom.js";
import { t } from "./i18n.js";
import { icon } from "./icons.js";
import { moved, startReorder } from "./reorder.js";
import { confirmDialog, modal } from "./ui.js";

/** 名前の長さの上限(バックエンドの PRESET_NAME_MAX と同じ) */
export const PRESET_NAME_MAX = 60;

/**
 * 条件を、チップの形(種類と文言)の並びにする。保存のダイアログと、管理のダイアログの説明に使う
 * @param {import("./list.js").IssueList} list
 * @param {object} p プリセットの形の条件
 */
export function presetParts(list, p) {
  const parts = [];
  if (p.search.trim()) parts.push({ kind: "search", label: t("list.chip.search", { term: p.search.trim() }) });
  if (p.relation && p.relation !== "all") {
    parts.push({ kind: "relation", label: t("list.preset.relation", { value: t(`list.filter.${p.relation}`) }) });
  }
  parts.push({ kind: "filter", label: t("list.chip.group", { name: t("list.f.status"), values: list.statusText(p.status) }) });
  for (const [group, key] of Object.entries({ tracker: "trackers", priority: "priorities", assignee: "assignees", due: "due" })) {
    const values = p.filter[key] || [];
    if (values.length) {
      parts.push({ kind: "filter", label: t("list.chip.group", { name: t(`list.f.${group}`),
        values: values.map((v) => list.valueLabel(group, v)).join("・") }) });
    }
  }
  if (p.sort.key !== "updated" || p.sort.desc !== true) {
    parts.push({ kind: "sort", label: t("list.chip.sort", { name: t(`list.sort.${p.sort.key}`), dir: p.sort.desc ? "↓" : "↑" }) });
  }
  return parts;
}

/**
 * 名前を入れるダイアログ。OK で、入れた名前(前後の空白を除く)を返す。取り消したら null
 * @param {object} o
 * @param {string} o.title
 * @param {string} o.okLabel
 * @param {string} o.initial
 * @param {(name: string) => string|null} [o.check] 保存できないときの理由(なければ null)
 * @param {(name: string) => string|null} [o.note] 保存はできるが、知らせておくこと(同じ名前を上書きするなど)
 * @param {Node|null} [o.extra] 名前の下に出すもの
 */
function nameDialog({ title, okLabel, initial, check = () => null, note = () => null, extra = null }) {
  return new Promise((resolve) => {
    let result = null;
    const input = h("input", { type: "text", value: initial, maxlength: String(PRESET_NAME_MAX), autocomplete: "off",
      "aria-label": t("list.preset.name"),
      onInput: () => update(),
      onKeydown: (e) => { if (e.key === "Enter" && !ok.disabled) { e.preventDefault(); ok.click(); } } });
    const error = h("div", { class: "preset-error", "aria-live": "polite" });
    const ok = h("button", { type: "button", class: "btn primary", onClick: () => { result = input.value.trim(); m.close(); } }, okLabel);
    const cancel = h("button", { type: "button", class: "btn", onClick: () => m.close() }, t("common.cancel"));
    const update = () => {
      const name = input.value.trim();
      const reason = name ? check(name) : null;
      const info = name && !reason ? note(name) : null;
      error.textContent = reason || info || "";
      error.classList.toggle("info", !reason);
      error.hidden = !error.textContent;
      ok.disabled = !name || !!reason;
    };
    const m = modal({ title, closeOnBackdrop: false, width: "min(460px, 100%)",
      body: h("div", { class: "dialog-body preset-form" },
        h("div", { class: "field" }, h("label", {}, t("list.preset.name")), input, error),
        extra),
      footer: [cancel, ok], onClose: () => resolve(result) });
    update();
    setTimeout(() => { input.focus(); input.select(); }, 0);
  });
}

/**
 * 「いまの条件を保存…」: 名前を付けて、いまの条件を保存する。同じ名前があれば、その場所で上書きする
 * @param {import("./list.js").IssueList} list
 */
export async function savePresetDialog(list) {
  const current = list.currentPreset();
  const parts = presetParts(list, current);
  const presets = list.presets();
  const extra = h("div", { class: "field" },
    h("label", {}, t("list.preset.conditions")),
    h("div", { class: "pchips" }, parts.map((p) => h("span", { class: `cchip cchip-${p.kind}` },
      h("span", { class: "t" }, p.label)))),
    h("div", { class: "help" }, list.version ? t("list.preset.saveHelpVersion") : t("list.preset.saveHelp")));
  const name = await nameDialog({ title: t("list.preset.saveTitle"), okLabel: t("common.save"),
    initial: list.matchedPreset()?.name || "", extra,
    note: (n) => (presets.some((p) => p.name === n) ? t("list.preset.overwriteNote") : null) });
  if (name === null) return;
  const preset = { ...current, name };
  const i = presets.findIndex((p) => p.name === name);
  const next = i >= 0 ? presets.map((p, n) => (n === i ? preset : p)) : [...presets, preset];
  await list.savePresets(next);
}

/**
 * 「プリセットの管理…」: 名前の変更・上書き(いまの条件で)・削除と、並べ替え
 * @param {import("./list.js").IssueList} list
 */
export function managePresetsDialog(list) {
  const ul = h("ul", { class: "server-list preset-list" });
  const hint = h("div", { class: "help" }, t("list.preset.manageHelp"));
  let focusGrip = null;
  const render = () => {
    const presets = list.presets();
    const names = presets.map((p) => p.name);
    ul.replaceChildren();
    if (!presets.length) ul.append(h("li", { class: "muted" }, t("list.preset.none")));
    const ids = presets.map((_, i) => i);
    const reorder = (order) => list.savePresets(order.map((i) => presets[i])).then(render);
    presets.forEach((p, i) => {
      const grip = presets.length > 1 ? h("span", { class: "grip", role: "button", tabindex: "0",
        title: t("settings.reorderHandle"), "aria-label": t("settings.reorderLabel", { name: p.name }),
        onPointerdown: (e) => startReorder(e, {
          ids, sourceId: i,
          rows: () => [...ul.querySelectorAll(":scope > li[data-index]")].map((li) => [Number(li.dataset.index), li]),
          area: () => ul,
          label: h("span", {}, p.name),
          onEnd: (order) => { if (order) reorder(order); },
        }),
        onKeydown: (e) => {
          // Alt+↑/↓: 1つ上・下へ動かす
          if (!e.altKey || (e.key !== "ArrowUp" && e.key !== "ArrowDown")) return;
          e.preventDefault();
          const order = moved(ids, i, e.key === "ArrowUp" ? -1 : 1);
          if (!order) return;
          focusGrip = p.name;
          reorder(order);
        } }, icon("grip")) : null;
      const summary = presetParts(list, p).map((x) => x.label).join(" · ");
      ul.append(h("li", { dataset: { index: i, name: p.name } }, grip,
        h("div", { class: "info" }, h("div", { class: "pname" }, p.name), h("div", { class: "url", title: summary }, summary)),
        h("button", { type: "button", class: "btn", onClick: async () => {
          const name = await nameDialog({ title: t("list.preset.renameTitle"), okLabel: t("common.save"), initial: p.name,
            check: (n) => (n !== p.name && names.includes(n) ? t("list.preset.duplicate") : null) });
          if (name === null || name === p.name) return;
          await list.savePresets(list.presets().map((x) => (x.name === p.name ? { ...x, name } : x)));
          render();
        } }, t("list.preset.rename")),
        h("button", { type: "button", class: "btn", title: t("list.preset.overwriteHint"), onClick: async () => {
          if (!await confirmDialog(t("list.preset.overwriteTitle"), t("list.preset.overwriteBody", { name: p.name }), t("list.preset.overwrite"))) return;
          await list.savePresets(list.presets().map((x) => (x.name === p.name ? { ...list.currentPreset(), name: p.name } : x)));
          render();
        } }, t("list.preset.overwrite")),
        h("button", { type: "button", class: "btn danger", onClick: async () => {
          if (!await confirmDialog(t("list.preset.deleteTitle"), t("list.preset.deleteBody", { name: p.name }), t("common.delete"), { danger: true })) return;
          await list.savePresets(list.presets().filter((x) => x.name !== p.name));
          render();
        } }, t("common.delete"))));
    });
    hint.hidden = presets.length < 2;
    if (focusGrip !== null) {
      [...ul.querySelectorAll("li[data-name]")].find((li) => li.dataset.name === focusGrip)?.querySelector(".grip")?.focus();
      focusGrip = null;
    }
  };
  render();
  const m = modal({ title: t("list.preset.manageTitle"), width: "min(640px, 100%)",
    body: h("div", { class: "dialog-body" }, ul, hint),
    footer: [h("button", { type: "button", class: "btn primary", onClick: () => m.close() }, t("common.close"))] });
}
