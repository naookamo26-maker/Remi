"""左ペイン: サーバー / プロジェクトのツリー(設計書 §3.2, モック「メインウィンドウ」左)。"""

from __future__ import annotations

import webbrowser
from dataclasses import dataclass

from PySide6.QtCore import QModelIndex, QRect, QSize, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QPainter, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeView,
    QWidget,
)

from remi.context import AppContext
from remi.store.projects import PROJECT_STATUS_ACTIVE, Project

KeyRole = Qt.ItemDataRole.UserRole + 1
UnreadRole = Qt.ItemDataRole.UserRole + 2
CountRole = Qt.ItemDataRole.UserRole + 3
MutedRole = Qt.ItemDataRole.UserRole + 4
StatusRole = Qt.ItemDataRole.UserRole + 5  # サーバーの接続状態: "ok" / "error" / "unknown"


@dataclass(frozen=True)
class NodeKey:
    kind: str  # all / server / server_all / server_unread / project
    server_id: int | None = None
    project_id: int | None = None

    def token(self) -> str:
        return f"{self.kind}:{self.server_id}:{self.project_id}"


@dataclass(frozen=True)
class Selection:
    """一覧の絞り込み条件。"""

    title: str
    server_ids: list[int] | None  # None = すべてのサーバー
    project_ids: set[int] | None  # None = すべてのプロジェクト
    unread_only: bool = False
    breadcrumb: str = ""


def visible_projects(projects: list[Project], monitored_project_ids: set[int], show_all: bool) -> set[int]:
    """表示するプロジェクト: 既定は「監視対象のチケットを含むプロジェクトとその祖先」。"""
    by_id = {p.project_id: p for p in projects}
    if show_all:
        base = {p.project_id for p in projects if p.accessible and p.status == PROJECT_STATUS_ACTIVE}
    else:
        base = set()
    base |= {pid for pid in monitored_project_ids if pid in by_id}
    out = set()
    for pid in base:
        cur: int | None = pid
        while cur is not None and cur in by_id and cur not in out:
            out.add(cur)
            cur = by_id[cur].parent_id
    return out


class BadgeDelegate(QStyledItemDelegate):
    """名前の右に未読数のバッジ(または件数)を描く。"""

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        unread = index.data(UnreadRole) or 0
        count = index.data(CountRole)
        status = index.data(StatusRole)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        pal = self.ctx.theme.palette if self.ctx.theme is not None else None
        if index.data(MutedRole) and pal is not None:
            opt.palette.setColor(opt.palette.ColorRole.Text, QColor(pal.text_muted))
        text = opt.text
        opt.text = ""
        style = opt.widget.style() if opt.widget else None
        if style:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        rect = opt.rect.adjusted(4, 0, -8, 0)
        painter.save()
        fm = opt.fontMetrics
        right = rect.right()
        if unread:
            label = str(unread) if unread < 1000 else "999+"
            w = max(fm.horizontalAdvance(label) + 12, 20)
            h = fm.height() + 2
            pill = QRect(right - w, rect.center().y() - h // 2, w, h)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(pal.accent) if pal else opt.palette.highlight().color())
            painter.drawRoundedRect(pill, h / 2, h / 2)
            painter.setPen(QColor(pal.on_accent) if pal else opt.palette.highlightedText().color())
            painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, label)
            right = pill.left() - 6
        elif count:
            label = str(count)
            painter.setPen(QColor(pal.text_muted) if pal else opt.palette.mid().color())
            w = fm.horizontalAdvance(label)
            painter.drawText(QRect(right - w, rect.top(), w, rect.height()), Qt.AlignmentFlag.AlignVCenter, label)
            right -= w + 6
        if status in ("ok", "error"):
            d = 8
            color = (pal.success if status == "ok" else pal.danger) if pal else ("#1A7F37" if status == "ok" else "#BC4C00")
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawEllipse(right - d, rect.center().y() - d // 2, d, d)
            right -= d + 6
        text_color = opt.palette.color(opt.palette.ColorRole.Text)
        if index.data(MutedRole) and pal:
            text_color = QColor(pal.text_muted)
        painter.setPen(text_color)
        painter.setFont(opt.font)
        text_rect = QRect(opt.rect.left(), rect.top(), right - opt.rect.left(), rect.height())
        if style:
            text_rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, opt.widget)
            text_rect.setRight(right)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter,
                         fm.elidedText(text, Qt.TextElideMode.ElideRight, max(0, text_rect.width())))
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        s = super().sizeHint(option, index)
        return QSize(s.width() + 40, max(s.height(), option.fontMetrics.height() + 12))


class ProjectTree(QTreeView):
    selectionChangedTo = Signal(object)  # Selection

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.model_ = QStandardItemModel(self)
        self.setModel(self.model_)
        self.setHeaderHidden(True)
        self.setItemDelegate(BadgeDelegate(ctx, self))
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self.setUniformRowHeights(True)
        self.setAnimated(True)
        self._items: dict[str, QStandardItem] = {}
        self._parents: dict[int, dict[int, int | None]] = {}
        self._selected_token: str | None = None
        self.expanded.connect(lambda _i: self._save_expanded())
        self.collapsed.connect(lambda _i: self._save_expanded())
        self.selectionModel().currentChanged.connect(self._on_current_changed)
        ctx.signals.serversChanged.connect(self.rebuild)
        ctx.signals.dataChanged.connect(lambda _sid, _ids: self.rebuild())
        ctx.signals.syncStateChanged.connect(lambda _sid: self.rebuild())
        ctx.signals.projectSettingsChanged.connect(lambda _sid: self.rebuild())
        ctx.signals.settingsChanged.connect(lambda key: key == "show_all_projects" and self.rebuild())
        self.rebuild()

    # --- 構築 -----------------------------------------------------------------
    def _item(self, text: str, key: NodeKey, *, unread: int = 0, count: int | None = None,
              bold: bool = False, muted: bool = False, status: str | None = None,
              tooltip: str | None = None) -> QStandardItem:
        it = QStandardItem(text)
        it.setData(key, KeyRole)
        it.setData(unread, UnreadRole)
        it.setData(count, CountRole)
        it.setData(muted, MutedRole)
        it.setData(status, StatusRole)
        if bold:
            f = QFont(it.font())
            f.setBold(True)
            it.setFont(f)
        if tooltip:
            it.setToolTip(tooltip)
        self._items[key.token()] = it
        return it

    def rebuild(self) -> None:
        current = self._selected_token
        if self.selectionModel().hasSelection():
            idx = self.currentIndex()
            k = idx.data(KeyRole)
            if k:
                current = k.token()
        self.model_.clear()
        self._items.clear()
        self._parents.clear()
        show_all = bool(self.ctx.settings.get("show_all_projects"))
        servers = self.ctx.servers.list(enabled_only=True)

        total_unread = 0
        server_items = []
        for s in servers:
            sid = s.server_id
            projects = self.ctx.projects.list(sid)
            by_id = {p.project_id: p for p in projects}
            self._parents[sid] = {p.project_id: p.parent_id for p in projects}
            unread = self.ctx.issues.unread_by_project(sid)
            counts = self.ctx.issues.count_by_project(sid)
            visible = visible_projects(projects, set(counts), show_all)
            s_unread = sum(unread.values())
            total_unread += s_unread
            status = "error" if s.last_error else ("ok" if s.last_synced_at else "unknown")
            tip = None
            if s.last_error:
                tip = self.tr("Could not sync: {error}").format(error=s.last_error)
            sitem = self._item(s.name, NodeKey("server", sid), bold=True, status=status, tooltip=tip or s.url)
            sitem.appendRow(self._item(self.tr("All"), NodeKey("server_all", sid), count=sum(counts.values())))
            sitem.appendRow(self._item(self.tr("Unread only"), NodeKey("server_unread", sid), unread=s_unread))

            children: dict[int | None, list[Project]] = {}
            for pid in visible:
                p = by_id[pid]
                parent = p.parent_id if p.parent_id in visible else None
                children.setdefault(parent, []).append(p)

            def subtotal(pid: int) -> tuple[int, int]:
                u, c = unread.get(pid, 0), counts.get(pid, 0)
                for ch in children.get(pid, []):
                    cu, cc = subtotal(ch.project_id)
                    u += cu
                    c += cc
                return u, c

            def add(parent_item: QStandardItem, parent_id: int | None) -> None:
                for p in sorted(children.get(parent_id, []), key=lambda x: x.name.lower()):
                    u, c = subtotal(p.project_id)
                    muted = not p.accessible or p.status != PROJECT_STATUS_ACTIVE
                    tip = self.tr("You do not have access to this project") if not p.accessible else None
                    it = self._item(p.name, NodeKey("project", sid, p.project_id), unread=u,
                                    count=None if u else (c or None), muted=muted, tooltip=tip)
                    parent_item.appendRow(it)
                    add(it, p.project_id)

            add(sitem, None)
            server_items.append(sitem)

        if len(servers) != 1:
            self.model_.appendRow(self._item(self.tr("All servers"), NodeKey("all"), unread=total_unread, bold=True))
        for it in server_items:
            self.model_.appendRow(it)
        if not servers:
            empty = QStandardItem(self.tr("No servers yet. Add one in Settings."))
            empty.setEnabled(False)
            self.model_.appendRow(empty)

        self._restore_expanded()
        target = self._items.get(current or "") or self._items.get(NodeKey("all").token())
        if target is None and server_items:
            target = self._items.get(NodeKey("server_all", servers[0].server_id).token())
        if target is not None:
            self.setCurrentIndex(target.index())

    # --- 展開状態の保存 ---------------------------------------------------------
    def _save_expanded(self) -> None:
        tokens = [t for t, it in self._items.items() if self.isExpanded(it.index())]
        self.ctx.settings.set("tree_expanded", tokens)

    def _restore_expanded(self) -> None:
        saved = self.ctx.settings.get("tree_expanded")
        self.blockSignals(True)
        for token, it in self._items.items():
            if saved:
                self.setExpanded(it.index(), token in saved)
            else:
                # 初回はサーバーと最上位のプロジェクトを開いておく
                self.setExpanded(it.index(), token.startswith("server:") or
                                 (it.parent() is not None and it.parent().data(KeyRole).kind == "server"))
        self.blockSignals(False)

    # --- 選択 -----------------------------------------------------------------
    def _on_current_changed(self, current: QModelIndex, _prev: QModelIndex) -> None:
        key: NodeKey | None = current.data(KeyRole)
        if key is None:
            return
        self._selected_token = key.token()
        self.selectionChangedTo.emit(self.selection_for(key, current.data(Qt.ItemDataRole.DisplayRole)))

    def selection_for(self, key: NodeKey, title: str) -> Selection:
        if key.kind == "all":
            return Selection(title, None, None)
        server = self.ctx.servers.get(key.server_id)
        sname = server.name if server else ""
        if key.kind in ("server", "server_all"):
            return Selection(sname, [key.server_id], None, breadcrumb=sname)
        if key.kind == "server_unread":
            return Selection(self.tr("Unread only"), [key.server_id], None, unread_only=True, breadcrumb=sname)
        ids = {key.project_id} | self.ctx.projects.descendants(key.server_id, key.project_id)
        names = []
        parents = self._parents.get(key.server_id, {})
        for pid in reversed([key.project_id, *self.ctx.projects.ancestors(key.server_id, key.project_id, parents)]):
            p = self.ctx.projects.get(key.server_id, pid)
            if p:
                names.append(p.name)
        return Selection(title, [key.server_id], ids, breadcrumb=" › ".join(names))

    def current_selection(self) -> Selection | None:
        idx = self.currentIndex()
        key = idx.data(KeyRole)
        return self.selection_for(key, idx.data(Qt.ItemDataRole.DisplayRole)) if key else None

    # --- 右クリックメニュー(モック「プロジェクトの右クリックメニュー」) -------------
    def _context_menu(self, pos) -> None:
        idx = self.indexAt(pos)
        key: NodeKey | None = idx.data(KeyRole)
        if key is None:
            return
        menu = QMenu(self)
        if key.kind in ("server", "server_all", "server_unread", "project"):
            mark = menu.addAction(self.tr("Mark all as read"))
            ids = None
            if key.kind == "project":
                ids = {key.project_id} | self.ctx.projects.descendants(key.server_id, key.project_id)
            mark.triggered.connect(lambda: self._mark_all_read(key.server_id, ids))
        if key.kind == "all":
            mark = menu.addAction(self.tr("Mark all as read"))
            mark.triggered.connect(lambda: [self._mark_all_read(s.server_id, None)
                                            for s in self.ctx.servers.list(enabled_only=True)])
        server = self.ctx.servers.get(key.server_id) if key.server_id else None
        if server is not None:
            url = server.url if key.kind != "project" else self._project_url(server.url, key)
            menu.addAction(self.tr("Open in browser")).triggered.connect(lambda: webbrowser.open(url))
        if key.kind == "project":
            menu.addSeparator()
            self._tri_state_menu(menu, key, "notify", self.tr("Notifications"))
            self._tri_state_menu(menu, key, "watch_authored", self.tr("Follow issues I created"))
        menu.addSeparator()
        show_all = menu.addAction(self.tr("Show all projects"))
        show_all.setCheckable(True)
        show_all.setChecked(bool(self.ctx.settings.get("show_all_projects")))
        show_all.toggled.connect(self._toggle_show_all)
        menu.exec(self.viewport().mapToGlobal(pos))

    def _project_url(self, base: str, key: NodeKey) -> str:
        p = self.ctx.projects.get(key.server_id, key.project_id)
        return f"{base}/projects/{p.identifier or p.project_id}" if p else base

    def _tri_state_menu(self, menu: QMenu, key: NodeKey, setting: str, title: str) -> None:
        sub = menu.addMenu(title)
        current = self.ctx.projects.get_setting(key.server_id, key.project_id, setting)
        effective = self.ctx.projects.effective_setting(key.server_id, key.project_id, setting)
        if effective is None:  # 通知: サーバー設定に従う
            server = self.ctx.servers.get(key.server_id)
            effective = bool(server and server.notify_enabled)
        group = QActionGroup(sub)
        for value, label in ((None, self.tr("Inherit")), (True, self.tr("On")), (False, self.tr("Off"))):
            act = QAction(label, sub)
            act.setCheckable(True)
            act.setChecked(current is value)
            act.triggered.connect(lambda _c=False, v=value: self._set_project_setting(key, setting, v))
            group.addAction(act)
            sub.addAction(act)
        sub.addSeparator()
        info = sub.addAction(self.tr("Currently: {value}").format(
            value=self.tr("On") if effective else self.tr("Off")))
        info.setEnabled(False)
        if setting == "watch_authored":
            note = sub.addAction(self.tr("Also applies to subprojects"))
            note.setEnabled(False)

    def _set_project_setting(self, key: NodeKey, setting: str, value: bool | None) -> None:
        self.ctx.projects.set_setting(key.server_id, key.project_id, setting, value)
        self.ctx.signals.projectSettingsChanged.emit(key.server_id)

    def _mark_all_read(self, server_id: int, project_ids: set[int] | None) -> None:
        self.ctx.issues.mark_all_read(server_id, project_ids)
        self.ctx.signals.dataChanged.emit(server_id, None)

    def _toggle_show_all(self, value: bool) -> None:
        self.ctx.settings.set("show_all_projects", value)
        self.ctx.signals.settingsChanged.emit("show_all_projects")
