// 翻訳と、日時・数値の書式(設計書 §10)
let lang = "en";
let cat = {};
let fallback = {};

export function setCatalog(language, catalog, fallbackCatalog) {
  lang = language;
  cat = catalog || {};
  fallback = fallbackCatalog || {};
}

export function currentLang() {
  return lang;
}

// 使い方: t(キー, {name: "x"})。複数形は {"one": ..., "other": ...} の形で持つ
export function t(key, params = {}) {
  let v = cat[key] ?? fallback[key] ?? key;
  if (v && typeof v === "object") {
    const rule = new Intl.PluralRules(lang).select(Number(params.count ?? 0));
    v = v[rule] ?? v.other ?? key;
  }
  return String(v).replace(/\{(\w+)\}/g, (m, name) => (name in params ? String(params[name]) : m));
}

function toDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return isNaN(d) ? null : d;
}

function sameDay(a, b) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

// 一覧用の短い表記: 今日は時刻、昨日は「昨日」、今年は月日、それ以前は年月日
export function formatShort(iso, now = new Date()) {
  const d = toDate(iso);
  if (!d) return "";
  if (sameDay(d, now)) return new Intl.DateTimeFormat(lang, { hour: "2-digit", minute: "2-digit" }).format(d);
  const y = new Date(now);
  y.setDate(now.getDate() - 1);
  if (sameDay(d, y)) return t("time.yesterday");
  if (d.getFullYear() === now.getFullYear()) return new Intl.DateTimeFormat(lang, { month: "numeric", day: "numeric" }).format(d);
  return new Intl.DateTimeFormat(lang, { year: "numeric", month: "numeric", day: "numeric" }).format(d);
}

export function formatTime(iso) {
  const d = toDate(iso);
  return d ? new Intl.DateTimeFormat(lang, { hour: "2-digit", minute: "2-digit" }).format(d) : "";
}

export function formatDateTime(iso) {
  const d = toDate(iso);
  return d ? new Intl.DateTimeFormat(lang, { dateStyle: "medium", timeStyle: "short" }).format(d) : "";
}

export function formatDateHeading(iso) {
  const d = toDate(iso);
  return d ? new Intl.DateTimeFormat(lang, { year: "numeric", month: "long", day: "numeric", weekday: "short" }).format(d) : "";
}

export function dayKey(iso) {
  const d = toDate(iso);
  return d ? `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}` : "";
}

export function formatSize(bytes) {
  if (bytes == null) return "";
  const units = ["B", "KB", "MB", "GB"];
  let v = bytes;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${new Intl.NumberFormat(lang, { maximumFractionDigits: i ? 1 : 0 }).format(v)} ${units[i]}`;
}
