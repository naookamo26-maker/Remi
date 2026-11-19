// 右ペイン: バージョンを選んだときの表示。概要(期日・進捗)と、変化の記録(追加・外れた・完了など)(設計書 §5.5)
import { h } from "./dom.js";
import { dayKey, formatDateHeading, formatTime, t } from "./i18n.js";
import { icon } from "./icons.js";

/** @typedef {import("./types.js").VersionData} VersionData */
/** @typedef {import("./types.js").VersionEvent} VersionEvent */

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
    d.synced ? t("version.doneOf", { done: d.done, total: d.total }) : t("version.loading")));
  const header = h("header", { class: "conv-header" },
    h("div", { class: "top" },
      h("div", { class: "titles" },
        h("div", { class: "crumb" }, [...d.breadcrumb, t("version.roadmap")].join(" › ")),
        h("h1", {}, icon("flag"), " ", d.name)),
      h("div", { class: "actions" }, openBtn)),
    meta);

  const tl = h("div", { class: "timeline version-timeline", tabindex: "0", "aria-label": t("version.changes") });
  if (!d.events.length) tl.append(h("div", { class: "system" }, t("version.noChanges")));
  let lastDay = "";
  let unreadShown = false;
  for (const e of d.events) {
    const k = dayKey(e.createdAt);
    if (k && k !== lastDay) {
      lastDay = k;
      tl.append(h("div", { class: "date-sep", role: "separator" }, formatDateHeading(e.createdAt)));
    }
    if (e.unread && !unreadShown) {
      unreadShown = true;
      tl.append(h("div", { class: "unread-sep", role: "separator" }, t("version.unreadFrom")));
    }
    tl.append(eventLine(e, d, app));
  }
  const root = h("div", { class: "version-view" }, header, tl);
  // 最新の変化が見えるように、最下部から表示する
  requestAnimationFrame(() => { tl.scrollTop = tl.scrollHeight; });
  return root;
}

/** 変化1件の行。チケットの番号を押すと、そのチケットを開く */
function eventLine(e, d, app) {
  const issue = e.issueId ? h("a", { href: "#", class: "issue-ref", onClick: (ev) => {
    ev.preventDefault();
    if (e.kind === "removed") app.openIssueOrBrowser(d.serverId, e.issueId);
    else app.openIssue(d.serverId, e.issueId);
  } }, `#${e.issueId}`) : null;
  const subject = e.subject ? h("span", { class: "subject" }, ` ${e.subject}`) : null;
  const text = {
    created: () => [t("version.event.created")],
    added: () => [issue, subject, " ", t("version.event.added")],
    removed: () => [issue, subject, " ", t("version.event.removed")],
    closed: () => [issue, subject, " ", t("version.event.closed")],
    reopened: () => [issue, subject, " ", t("version.event.reopened")],
    due_date: () => [t("version.event.dueDate", { old: e.oldValue || t("version.noDueDate"), new: e.newValue || t("version.noDueDate") })],
    status: () => [t("version.event.status", { old: t(`version.status.${e.oldValue}`), new: t(`version.status.${e.newValue}`) })],
    name: () => [t("version.event.name", { old: e.oldValue || "", new: e.newValue || "" })],
  }[e.kind]?.() || [e.kind];
  return h("div", { class: `system version-event ${e.kind}` },
    h("span", { class: "time" }, formatTime(e.createdAt)), " ", ...text);
}
