"""日時などの表示用の書式(言語の QLocale に従う。設計書 §10.1)。"""

from __future__ import annotations

from datetime import UTC, datetime

from PySide6.QtCore import QCoreApplication, QDateTime, QLocale, QTimeZone


def _to_local(iso: str) -> QDateTime | None:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    q = QDateTime.fromSecsSinceEpoch(int(dt.timestamp()), QTimeZone.utc())
    return q.toLocalTime()


def format_time(iso: str | None, now: datetime | None = None) -> str:
    """一覧用の短い表記: 今日は時刻、昨日は「昨日」、今年は月日、それ以前は年月日。"""
    if not iso:
        return ""
    q = _to_local(iso)
    if q is None:
        return iso
    loc = QLocale()
    now_q = QDateTime.fromSecsSinceEpoch(int((now or datetime.now(UTC)).timestamp()), QTimeZone.utc()).toLocalTime()
    d, today = q.date(), now_q.date()
    if d == today:
        return loc.toString(q.time(), QLocale.FormatType.ShortFormat)
    if d == today.addDays(-1):
        return QCoreApplication.translate("Format", "Yesterday")
    if d.year() == today.year():
        fmt = "M/d" if loc.language() == QLocale.Language.Japanese else "MMM d"
        return loc.toString(d, fmt)
    return loc.toString(d, QLocale.FormatType.ShortFormat)


def format_datetime(iso: str | None) -> str:
    """会話ビュー用: 時刻まで表示する。"""
    if not iso:
        return ""
    q = _to_local(iso)
    if q is None:
        return iso
    return QLocale().toString(q, QLocale.FormatType.ShortFormat)


def format_date_heading(iso: str | None) -> str:
    """会話ビューの日付の区切り(例: 2026年11月16日(月))。"""
    q = _to_local(iso or "")
    if q is None:
        return ""
    return QLocale().toString(q.date(), QLocale.FormatType.LongFormat)


def local_date_key(iso: str | None) -> str:
    q = _to_local(iso or "")
    return q.date().toString("yyyy-MM-dd") if q else ""


def format_size(n: int | None) -> str:
    if n is None:
        return ""
    return QLocale().formattedDataSize(n, 0)


__all__ = ["format_time", "format_datetime", "format_date_heading", "local_date_key", "format_size"]
