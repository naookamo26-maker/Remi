// 会話ビューの本体: 説明・コメントの吹き出し、変更履歴の行、日付と未読の区切り(設計書 §5.2)
import { h, html } from "./dom.js";
import { dayKey, formatDateHeading, formatSize, formatTime, t } from "./i18n.js";
import { icon } from "./icons.js";

/** @typedef {import("./types.js").ConversationData} ConversationData */
/** @typedef {import("./types.js").Journal} Journal */
/** @typedef {import("./types.js").JournalDetail} JournalDetail */

/**
 * コメントの編集・削除、添付の削除の操作(会話ビューが持つ。権限がなければ null を返す)。
 * @typedef {object} TimelineActions
 * @property {(j: Journal) => HTMLElement|null} noteTools      吹き出しの「⋯」メニューのボタン
 * @property {(j: Journal) => HTMLElement|null} noteEditor     編集中なら、吹き出しの代わりの編集欄
 * @property {(det: JournalDetail) => HTMLElement|null} attachmentTools  添付の削除ボタン
 */

const AVATAR_COLORS = ["#FCE7D6", "#E0E7FF", "#DCFCE7", "#FDE68A", "#FBCFE8", "#CFFAFE", "#E9D5FF", "#FED7AA"];

function avatar(name, id) {
  const label = (name || "?").trim().charAt(0);
  const color = AVATAR_COLORS[Math.abs(Number(id) || [...(name || "")].reduce((a, c) => a + c.charCodeAt(0), 0)) % AVATAR_COLORS.length];
  return h("div", { class: "avatar", "aria-hidden": "true", style: { background: color } }, label);
}

/**
 * 会話を tl に描く。
 * @param {HTMLElement} tl
 * @param {ConversationData} d
 * @param {{onLoadMore: () => void, actions?: TimelineActions}} handlers
 */
export function renderTimeline(tl, d, { onLoadMore, actions = null }) {
  if (d.hasMore) {
    tl.append(h("div", { class: "system" }, h("button", { type: "button", class: "btn", onClick: onLoadMore },
      t("conv.loadMore", { count: d.total - d.journals.length }))));
  }
  let lastDay = "";
  const day = (iso) => {
    const k = dayKey(iso);
    if (k && k !== lastDay) {
      lastDay = k;
      tl.append(h("div", { class: "date-sep", role: "separator" }, formatDateHeading(iso)));
    }
  };
  if (!d.hasMore && d.descriptionHtml) {
    day(d.createdOn);
    tl.append(message({
      userName: d.author, userId: d.authorId, mine: d.authorId === d.myUserId, html: d.descriptionHtml,
      createdOn: d.createdOn, label: t("conv.description"), cls: "description",
    }));
  }
  let unreadShown = false;
  // コメントの番号(#note-N)は、Redmineと同じく履歴の通し番号
  const firstNote = d.total - d.journals.length + 1;
  d.journals.forEach((j, i) => {
    day(j.createdOn);
    if (j.unread && !unreadShown) {
      unreadShown = true;
      tl.append(h("div", { class: "unread-sep", role: "separator" }, t("conv.unreadFrom")));
    }
    if (j.details.length) tl.append(changeGroup(j, actions));
    if (j.html) {
      tl.append(message({ ...j, noteNo: firstNote + i, tools: actions?.noteTools(j), editor: actions?.noteEditor(j) }));
    }
  });
  if (!d.journalsSynced) tl.append(h("div", { class: "system" }, t("conv.loading")));
}

/**
 * 送信中のコメント(送信が終わるまで仮に表示する)。
 * @param {string[]} parts 本文・項目の変更・添付のファイル名
 */
export function pendingMessage(parts) {
  return h("article", { class: "msg mine pending" },
    h("div", { class: "body" }, h("div", { class: "who" }, t("conv.sending")),
      h("div", { class: "bubble" }, parts.filter(Boolean).join("\n"))));
}

function message(m) {
  const cls = ["msg", m.mine ? "mine" : "", m.private ? "private" : "", m.cls || ""].filter(Boolean).join(" ");
  const who = h("div", { class: "who" },
    m.mine ? h("b", {}, t("conv.you")) : h("b", {}, m.userName || "?"),
    m.label ? ` · ${m.label}` : "", ` · ${formatTime(m.createdOn)}`,
    m.private ? h("span", { class: "private-badge" }, icon("lock"), t("conv.private")) : null,
    m.noteNo ? h("span", { class: "muted" }, ` #${m.noteNo}`) : null,
    m.tools || null);
  const bubble = m.editor || html(h("div", { class: "bubble content" }), m.html);
  return h("article", { class: cls, id: m.noteNo ? `note-${m.noteNo}` : null },
    m.mine ? null : avatar(m.userName, m.userId), h("div", { class: "body" }, who, bubble));
}

/**
 * 1回の更新(journal)で行われた項目の変更・添付の追加などを、1つにまとめて描く。
 * 誰が・いつ を先頭に1回だけ出し、変更は1行ずつ並べる。
 * @param {Journal} j
 */
function changeGroup(j, actions) {
  const who = j.mine ? t("conv.you") : (j.userName || "?");
  const items = j.details.map((det) => changeItem(det, actions));
  // 変更が1つだけなら、これまでどおり1行で出す
  if (items.length === 1) {
    return h("div", { class: "system" }, `${who}: `, [...items[0].childNodes], ` · ${formatTime(j.createdOn)}`);
  }
  return h("div", { class: "system change-group" },
    h("div", { class: "change-who" }, `${who} · ${formatTime(j.createdOn)}`),
    h("ul", { class: "change-list" }, items));
}

/** @param {JournalDetail} det */
function changeItem(det, actions) {
  const li = h("li", {}, det.text);
  if (det.kind === "attachment" && det.url) {
    if (det.isImage) {
      li.append(h("a", { href: det.url, class: "attachment image" },
        h("img", { class: "att-thumb", src: det.url, alt: det.filename, loading: "lazy" })));
    } else {
      li.append(" ", h("a", { href: det.url, class: "attachment", dataset: { name: det.filename } },
        t("conv.openFile")), det.filesize ? ` (${formatSize(det.filesize)})` : "");
    }
    const tools = actions?.attachmentTools(det);
    if (tools) li.append(" ", tools);
  }
  return li;
}
