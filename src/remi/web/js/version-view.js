// 右ペイン: バージョンを選んだときの表示。概要(期日・進捗)と、変化の記録(追加・外れた・完了など)(設計書 §5.5)
import { h, spinner } from "./dom.js";
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

/** 変化1件のカード。左: 種別のチップと時刻、右: チケットの番号(押すとそのチケットを開く)と件名、または変化の文章 */
function eventLine(e, d, app) {
  const badge = { created: "created", added: "added", removed: "removed", closed: "closed", reopened: "reopened",
    due_date: "dueDate", status: "status", name: "name" }[e.kind];
  // 「追加」などのチップだけでは足りない文章は、ツールチップと読み上げで補う
  const phrase = { added: "version.event.added", removed: "version.event.removed", closed: "version.event.closed",
    reopened: "version.event.reopened", created: "version.event.created" }[e.kind];
  const chip = h("span", { class: "badge", title: phrase ? t(phrase) : null }, badge ? t(`version.badge.${badge}`) : e.kind);
  let body;
  if (e.issueId) {
    const issue = h("a", { href: "#", class: "issue-ref", onClick: (ev) => {
      ev.preventDefault();
      if (e.kind === "removed") app.openIssueOrBrowser(d.serverId, e.issueId);
      else app.openIssue(d.serverId, e.issueId);
    } }, `#${e.issueId}`);
    body = [issue, e.subject ? h("span", { class: "subject" }, e.subject) : null];
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
