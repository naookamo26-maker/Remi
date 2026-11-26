// 右ペイン: バージョンを選んだときの表示。概要(期日・進捗)と、変化の記録(追加・完了・再開・除外など)(設計書 §5.5)
import { h, spinner } from "./dom.js";
import { dayKey, formatDateHeading, formatTime, t } from "./i18n.js";
import { icon } from "./icons.js";

/** @typedef {import("./types.js").VersionData} VersionData */
/** @typedef {import("./types.js").VersionEvent} VersionEvent */

/** 絞り込みのボタンの並び。「その他」は、期日・ステータス・名前・作成の変化 */
const FILTER_KINDS = ["added", "closed", "reopened", "removed", "other"];
const kindGroup = (e) => (FILTER_KINDS.includes(e.kind) ? e.kind : "other");
/** 1枚のカードにまとめる、チケットの変化の種類と、まとめる時刻の幅(まとまりの最初の変化から。一括編集でも、チケットごとに数秒ずれるため) */
const GROUP_KINDS = new Set(["added", "closed", "reopened", "removed"]);
const GROUP_WINDOW_MS = 2 * 60 * 1000;
/** これより多いまとまりは、最初は畳んで、先頭のこの件数だけを出す */
const GROUP_FOLD = 5;

/**
 * 続いて起きた同じ種類のチケットの変化を、まとまりにする。日付・「ここから新しい変化」の境目では分ける
 * @param {VersionEvent[]} events
 * @returns {VersionEvent[][]}
 */
export function groupEvents(events) {
  const groups = [];
  let cur = null;
  for (const e of events) {
    const first = cur?.[0];
    const joins = first && GROUP_KINDS.has(e.kind) && e.kind === first.kind && !!e.unread === !!first.unread
      && dayKey(e.createdAt) === dayKey(first.createdAt)
      && Math.abs(Date.parse(e.createdAt) - Date.parse(first.createdAt)) <= GROUP_WINDOW_MS;
    if (joins) cur.push(e);
    else groups.push((cur = [e]));
  }
  return groups;
}

/**
 * @param {VersionData} d
 * @param {import("./main.js").App} app
 */
export function renderVersion(d, app) {
  const percent = d.total ? Math.round((d.done / d.total) * 100) : 0;
  const openBtn = h("button", { type: "button", class: "btn icon-only", title: t("conv.openRedmineHint"),
    "aria-label": t("conv.openRedmine"), onClick: (e) => app.openRedmine(d.url, { external: e.shiftKey }) }, icon("external"));
  const meta = h("div", { class: "fields" },
    h("span", { class: `pill${d.status === "closed" ? " closed" : ""}` }, t(`version.status.${d.status}`)),
    h("span", { class: "static" }, t("version.dueDate"), " ", h("b", {}, d.dueDate || t("version.noDueDate"))),
    h("span", { class: "progress", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(d.total),
      "aria-valuenow": String(d.done), "aria-label": t("version.progress") },
    h("span", { class: "bar" }, h("span", { style: { width: `${percent}%` } })),
    d.synced ? t("version.doneOf", { done: d.done, total: d.total })
      : app.tree.failedVersions.has(d.versionId) ? t("version.loadFailed") : [spinner(), t("version.loading")]));
  const header = h("header", { class: "conv-header" },
    h("div", { class: "top" },
      h("div", { class: "titles" },
        h("div", { class: "crumb" }, [...d.breadcrumb, t("version.roadmap")].join(" › ")),
        h("h1", {}, icon("flag"), " ", d.name)),
      h("div", { class: "actions" }, openBtn)),
    meta);

  const tl = h("div", { class: "timeline version-timeline", tabindex: "0", "aria-label": t("version.changes") });
  // 種類での絞り込み(1つだけ選ぶ)。覚えておくのは、画面を開いている間だけ。別のバージョンを開くと「すべて」に戻る
  const key = `${d.serverId}:${d.versionId}`;
  // 開いたまとまり(先頭の変化の ID)も、画面を開いている間だけ覚える
  if (app.versionFilter?.key !== key) app.versionFilter = { key, kind: "all", expanded: new Set() };
  const present = FILTER_KINDS.filter((k) => d.events.some((e) => kindGroup(e) === k));
  if (!present.includes(app.versionFilter.kind)) app.versionFilter.kind = "all";
  const fillTimeline = () => {
    const kind = app.versionFilter.kind;
    tl.replaceChildren();
    if (!d.events.length) tl.append(h("div", { class: "system" }, t("version.noChanges")));
    let lastDay = "";
    let unreadShown = false;
    for (const group of groupEvents(d.events.filter((e) => kind === "all" || kindGroup(e) === kind))) {
      const e = group[0];
      const k = dayKey(e.createdAt);
      if (k && k !== lastDay) {
        lastDay = k;
        tl.append(h("div", { class: "date-sep", role: "separator" }, formatDateHeading(e.createdAt)));
      }
      if (e.unread && !unreadShown) {
        unreadShown = true;
        tl.append(h("div", { class: "unread-sep", role: "separator" }, t("version.unreadFrom")));
      }
      tl.append(group.length > 1 ? groupCard(group, d, app) : eventLine(e, d, app));
    }
  };
  fillTimeline();
  const buttons = ["all", ...present].map((kind) => h("button", { type: "button", dataset: { kind }, title: t(`version.filter.${kind}`),
    "aria-pressed": String(kind === app.versionFilter.kind),
    onClick: () => {
      app.versionFilter.kind = kind;
      for (const b of buttons) b.setAttribute("aria-pressed", String(b.dataset.kind === kind));
      fillTimeline();
      tl.scrollTop = tl.scrollHeight;
    } }, t(`version.filter.${kind}`)));
  // 種類が1つだけ(または変化がない)ときは、絞り込む意味がないので出さない
  const filter = present.length > 1
    ? h("div", { class: "version-filter" }, h("div", { class: "segmented", role: "group", "aria-label": t("version.filter.label") }, buttons))
    : null;
  const root = h("div", { class: "version-view" }, header, filter, tl);
  // 最新の変化が見えるように、最下部から表示する
  requestAnimationFrame(() => { tl.scrollTop = tl.scrollHeight; });
  return root;
}

/** 種別のチップ。「追加」などのチップだけでは足りない文章は、ツールチップと読み上げで補う */
function kindChip(e) {
  const badge = { created: "created", added: "added", removed: "removed", closed: "closed", reopened: "reopened",
    due_date: "dueDate", status: "status", name: "name" }[e.kind];
  const phrase = { added: "version.event.added", removed: "version.event.removed", closed: "version.event.closed",
    reopened: "version.event.reopened", created: "version.event.created" }[e.kind];
  return h("span", { class: "badge", title: phrase ? t(phrase) : null }, badge ? t(`version.badge.${badge}`) : e.kind);
}

/** チケットの番号(押すとそのチケットを開く。除外したものは、Redmine にあるかを確かめてから開く) */
function issueRef(e, d, app) {
  return h("a", { href: "#", class: "issue-ref", onClick: (ev) => {
    ev.preventDefault();
    if (e.kind === "removed") app.openIssueOrBrowser(d.serverId, e.issueId);
    else app.openIssue(d.serverId, e.issueId);
  } }, `#${e.issueId}`);
}

/**
 * 一括で起きた同じ種類の変化(2件以上)を、1枚のカードにする。左: チップと「件数 · 時刻」、右: 「#番号 件名」の行。
 * GROUP_FOLD 件より多いときは、最初は先頭だけを出して畳み、「ほか N 件を表示」「折りたたむ」で開閉する
 */
function groupCard(group, d, app) {
  const first = group[0];
  const foldable = group.length > GROUP_FOLD;
  const expanded = app.versionFilter.expanded;
  const rows = h("div", { class: "rows" });
  const fill = () => {
    const open = !foldable || expanded.has(first.eventId);
    rows.replaceChildren(...(open ? group : group.slice(0, GROUP_FOLD)).flatMap((e) =>
      [issueRef(e, d, app), h("span", { class: "subject" }, e.subject || "")]));
    if (foldable) {
      rows.append(h("button", { type: "button", class: "toggle", "aria-expanded": String(open), onClick: () => {
        if (open) expanded.delete(first.eventId);
        else expanded.add(first.eventId);
        fill();
      } }, open ? t("version.group.fold") : t("version.group.more", { count: group.length - GROUP_FOLD })));
    }
  };
  fill();
  return h("div", { class: `version-event ${first.kind} group` },
    h("div", { class: "side" }, kindChip(first),
      h("span", { class: "time" }, t("version.group.countTime", { count: group.length, time: formatTime(first.createdAt) }))),
    h("div", { class: "body" }, rows));
}

/** 変化1件のカード。左: 種別のチップと時刻、右: チケットの番号(押すとそのチケットを開く)と件名、または変化の文章 */
function eventLine(e, d, app) {
  const chip = kindChip(e);
  let body;
  if (e.issueId) {
    body = [issueRef(e, d, app), e.subject ? h("span", { class: "subject" }, e.subject) : null];
  } else {
    body = [{
      created: () => t("version.event.created"),
      due_date: () => t("version.event.dueDate", { old: e.oldValue || t("version.noDueDate"), new: e.newValue || t("version.noDueDate") }),
      status: () => t("version.event.status", { old: t(`version.status.${e.oldValue}`), new: t(`version.status.${e.newValue}`) }),
      name: () => t("version.event.name", { old: e.oldValue || "", new: e.newValue || "" }),
    }[e.kind]?.() || e.kind];
  }
  return h("div", { class: `version-event ${e.kind}` },
    h("div", { class: "side" }, chip, h("span", { class: "time" }, formatTime(e.createdAt))),
    h("div", { class: "body" }, ...body));
}
