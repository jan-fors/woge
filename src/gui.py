#!/usr/bin/env python3
"""
woge – an Anki-style desktop frontend for woge exercise banks.

    pip install PySide6 sympy matplotlib
    woge-gui                        (installed: data in ~/.local/share/woge; checkout: python -m src.gui, data in dat/)

Tabs
  Decks      – bank files with total/new/due counts; create today's worksheet (time or count)
  Study      – card view: exercise → solution → explanation → rate Again/Hard/Good/Easy
  Worksheets – every worksheet is saved; reopen, continue, export to Markdown/PDF
  Browse     – search all exercises, view one separately, generate new variants, see history
Keys in Study: Space = reveal next part, 1–4 = rate, S = skip
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, QSettings, QTimer
from PySide6.QtGui import QAction, QDesktopServices, QImage, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QPlainTextEdit,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton, QSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import cli as X
from . import core as C
from .paths import BANK_DIR, DATA_DIR, ensure_user_dirs

APP_CSS = """
QMainWindow, QWidget { font-size: 13px; }
QTextBrowser#card { background: #fdfdfb; color: #202020; border: 1px solid #ddd; border-radius: 8px;
                    padding: 18px; font-size: 16px; }
QPushButton { padding: 6px 14px; }
QPushButton#again { background: #e57373; color: white; font-weight: bold; }
QPushButton#hard  { background: #ffb74d; color: black; font-weight: bold; }
QPushButton#good  { background: #81c784; color: black; font-weight: bold; }
QPushButton#easy  { background: #64b5f6; color: black; font-weight: bold; }
QPushButton#reveal { font-weight: bold; padding: 8px 28px; }
QLabel#cardmeta { color: #666; }
QLabel#big { font-size: 18px; font-weight: bold; }
"""


# =============================================================================
# Widgets
# =============================================================================
class MathBrowser(QTextBrowser):
    """QTextBrowser that serves formula images rendered by core.MathRenderer."""

    def __init__(self, renderer: C.MathRenderer, object_name: str = "card"):
        super().__init__()
        self.setObjectName(object_name)
        self.renderer = renderer
        self.images: dict[str, bytes] = {}
        self.setOpenLinks(False)

    def set_sections(self, header_html: str, sections: list[tuple[str, str]]):
        """sections: (heading, markdown+latex text). Headings may be empty."""
        self.images = {}
        parts = [header_html]
        for heading, text in sections:
            if not text:
                continue
            if heading:
                parts.append(f'<hr><p style="color:#888; font-size:12px; margin-bottom:2px">{heading.upper()}</p>')
            parts.append(C.to_html(text, self.renderer, self.images))
        self.setHtml("".join(parts))

    def loadResource(self, rtype, url: QUrl):  # noqa: N802 (Qt API name)
        if url.scheme() == "math":
            data = self.images.get(url.path())
            if data:
                img = QImage()
                img.loadFromData(data)
                return img
        return super().loadResource(rtype, url)


def card_header(inst: dict) -> str:
    stars = "★" * (inst.get("difficulty") or 0)
    variant = " · <i>new numbers</i>" if inst.get("variant") else ""
    return (f'<p style="color:#888; font-size:12px">{inst.get("deck", "")} · {inst.get("type", "").replace("_", " ")}'
            f' · {stars} · ~{inst.get("minutes") or X.DEFAULT_MINUTES} min{variant}</p>'
            f'<h3 style="margin-top:0">{inst.get("title", inst["id"])}</h3>')


def ro_item(text, align_right: bool = False) -> QTableWidgetItem:
    it = QTableWidgetItem(str(text))
    it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if align_right:
        it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return it


# =============================================================================
# Decks tab
# =============================================================================
class DecksTab(QWidget):
    def __init__(self, app: "MainWindow"):
        super().__init__()
        self.app = app
        lay = QHBoxLayout(self)

        left = QVBoxLayout()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Deck", "Total", "New", "Due"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        left.addWidget(QLabel("Tick the decks to draw exercises from (none ticked = all):"))
        left.addWidget(self.tree)
        self.today = QLabel()
        left.addWidget(self.today)
        lay.addLayout(left, 3)

        box = QGroupBox("Today's worksheet")
        form = QFormLayout(box)
        self.size_mode = QComboBox()
        self.size_mode.addItems(["Time budget (minutes)", "Number of exercises"])
        self.size_val = QSpinBox()
        self.size_val.setRange(1, 600)
        self.size_val.setValue(30)
        self.size_mode.currentIndexChanged.connect(lambda i: self.size_val.setValue(30 if i == 0 else 8))
        self.dmin, self.dmax = QSpinBox(), QSpinBox()
        for sb, v in ((self.dmin, 1), (self.dmax, 5)):
            sb.setRange(1, 5)
            sb.setValue(v)
        drow = QHBoxLayout()
        drow.addWidget(self.dmin)
        drow.addWidget(QLabel("to"))
        drow.addWidget(self.dmax)
        self.mode = QComboBox()
        self.mode.addItems(["variants", "originals", "mix"])
        self.mode.setToolTip("variants: new numbers where possible · originals: as in the source · mix: 50/50")
        self.balance = QCheckBox("balance difficulty levels and exercise types")
        self.balance.setChecked(True)
        self.skip_prog = QCheckBox("skip programming exercises")
        self.title = QLineEdit()
        self.title.setPlaceholderText("optional title")
        form.addRow("Size", self.size_mode)
        form.addRow("", self.size_val)
        form.addRow("Difficulty", drow)
        form.addRow("Exercises", self.mode)
        form.addRow("", self.balance)
        form.addRow("", self.skip_prog)
        form.addRow("Title", self.title)
        create = QPushButton("Create worksheet and start")
        create.setObjectName("reveal")
        create.clicked.connect(lambda *_, f_=self.create_sheet: f_())
        form.addRow(create)
        due_btn = QPushButton("Review due exercises now")
        due_btn.clicked.connect(lambda *_, f_=self.review_due: f_())
        form.addRow(due_btn)
        hint = QLabel("Selection prefers new and due exercises, avoids ones you saw in the last 20 hours "
                      "and spreads difficulty levels and exercise types.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#777")
        form.addRow(hint)
        lay.addWidget(box, 2)

    def refresh(self):
        states = self.app.tracker.all_states()
        t = C.now()
        checked = set(self.checked_decks())
        self.tree.clear()
        folders: dict[str, QTreeWidgetItem] = {}
        for name, exs in self.app.decks.items():
            parts = name.split("/")
            parent = self.tree.invisibleRootItem()
            path = ""
            for p in parts[:-1]:
                path = f"{path}/{p}" if path else p
                if path not in folders:
                    it = QTreeWidgetItem([p])
                    it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
                    it.setCheckState(0, Qt.CheckState.Unchecked)
                    parent.addChild(it)
                    folders[path] = it
                parent = folders[path]
            total, new, due = C.deck_counts(exs, states, t)
            it = QTreeWidgetItem([parts[-1], str(total), str(new), str(due)])
            it.setData(0, Qt.ItemDataRole.UserRole, name)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(0, Qt.CheckState.Checked if name in checked else Qt.CheckState.Unchecked)
            it.setForeground(2, Qt.GlobalColor.darkBlue)
            it.setForeground(3, Qt.GlobalColor.darkGreen)
            parent.addChild(it)
        # folder sums
        for path, it in folders.items():
            exs = [e for n, v in self.app.decks.items() if n.startswith(path + "/") for e in v]
            total, new, due = C.deck_counts(exs, states, t)
            for col, val in ((1, total), (2, new), (3, due)):
                it.setText(col, str(val))
        self.tree.expandAll()
        self.today.setText(f"Reviewed today: {self.app.tracker.reviews_on(t.date())}   ·   "
                           f"exercises: {sum(len(v) for v in self.app.decks.values())}")

    def checked_decks(self) -> list[str]:
        out = []

        def walk(it: QTreeWidgetItem):
            name = it.data(0, Qt.ItemDataRole.UserRole)
            if name and it.checkState(0) == Qt.CheckState.Checked:
                out.append(name)
            for i in range(it.childCount()):
                walk(it.child(i))
        walk(self.tree.invisibleRootItem())
        return out

    def pool(self) -> list[dict]:
        names = self.checked_decks() or list(self.app.decks)
        exs = [e for n in names for e in self.app.decks[n]]
        lo, hi = sorted((self.dmin.value(), self.dmax.value()))
        exs = [e for e in exs if lo <= (e.get("difficulty") or 1) <= hi]
        if self.skip_prog.isChecked():
            exs = [e for e in exs if "programming" not in e.get("tags", []) and e.get("type") != "simulation_design"]
        return exs

    def create_sheet(self):
        pool = self.pool()
        if not pool:
            QMessageBox.information(self, "No exercises", "No exercises match the chosen decks and difficulty.")
            return
        by_time = self.size_mode.currentIndex() == 0
        settings = {"decks": self.checked_decks() or "all", "mode": self.mode.currentText(),
                    "difficulty": [self.dmin.value(), self.dmax.value()],
                    ("minutes" if by_time else "n"): self.size_val.value()}
        items = C.build_sheet(pool, self.app.tracker, n=None if by_time else self.size_val.value(),
                              minutes=self.size_val.value() if by_time else None,
                              mode=self.mode.currentText(), balance=self.balance.isChecked())
        sheet = self.app.store.new(items, settings, self.title.text().strip() or None)
        self.title.clear()
        self.app.start_session(sheet)

    def review_due(self):
        states = self.app.tracker.all_states()
        t = C.now()
        due = [e for e in self.pool() if C.is_due(states.get(e["id"]), t)]
        if not due:
            QMessageBox.information(self, "Nothing due", "No exercises are due in the selected decks. "
                                    "Create a worksheet to get new ones.")
            return
        items = C.build_sheet(due, self.app.tracker, n=len(due), mode=self.mode.currentText(), balance=False)
        sheet = self.app.store.new(items, {"due_review": True}, f"Due review {t:%Y-%m-%d %H:%M}")
        self.app.start_session(sheet)


# =============================================================================
# Study tab
# =============================================================================
class StudyTab(QWidget):
    def __init__(self, app: "MainWindow"):
        super().__init__()
        self.app = app
        self.sheet: dict | None = None
        self.queue: list[int] = []
        self.stage = 0          # 0 statement, 1 + solution, 2 + explanation
        lay = QVBoxLayout(self)

        top = QHBoxLayout()
        self.sheet_label = QLabel("No worksheet open")
        self.sheet_label.setObjectName("big")
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(260)
        top.addWidget(self.sheet_label, 1)
        top.addWidget(self.progress)
        lay.addLayout(top)

        self.card = MathBrowser(app.renderer)
        lay.addWidget(self.card, 1)

        self.reveal_row = QHBoxLayout()
        self.reveal = QPushButton("Show solution  (Space)")
        self.reveal.setObjectName("reveal")
        self.reveal.clicked.connect(lambda *_, f_=self.next_stage: f_())
        self.skip = QPushButton("Skip  (S)")
        self.skip.clicked.connect(lambda *_, f_=self.skip_card: f_())
        self.reveal_row.addStretch(1)
        self.reveal_row.addWidget(self.reveal)
        self.reveal_row.addWidget(self.skip)
        self.reveal_row.addStretch(1)
        lay.addLayout(self.reveal_row)

        self.rate_row = QHBoxLayout()
        self.rate_btns = []
        for r, (name, tip) in {1: ("again", "couldn't solve it – show again soon"),
                               2: ("hard", "solved with difficulty"), 3: ("good", "solved"),
                               4: ("easy", "trivial")}.items():
            b = QPushButton(f"{C.RATINGS[r]}  ({r})")
            b.setObjectName(name)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, rr=r: self.rate(rr))
            self.rate_row.addWidget(b)
            self.rate_btns.append(b)
        lay.addLayout(self.rate_row)

        for key, fn in (("Space", self.next_stage), ("S", self.skip_card)):
            QShortcut(QKeySequence(key), self, activated=lambda f=fn: self._if_active(f))
        for r in range(1, 5):
            QShortcut(QKeySequence(str(r)), self, activated=lambda rr=r: self._if_active(lambda: self.rate(rr)))
        self.show_empty()

    def _if_active(self, fn):
        if self.app.tabs.currentWidget() is self and self.sheet:
            fn()

    # ---- session -------------------------------------------------------------
    def open(self, sheet: dict):
        self.sheet = sheet
        self.queue = [i for i, it in enumerate(sheet["items"]) if not it.get("rating")]
        self.sheet_label.setText(sheet.get("title", sheet["id"]))
        self.show_current()

    def show_empty(self):
        self.card.set_sections("<h3>Nothing to study</h3><p>Create a worksheet in the <b>Decks</b> tab or open one "
                               "in <b>Worksheets</b>.</p>", [])
        self._buttons(reveal=False, rate=False)
        self.progress.setValue(0)

    def current(self) -> dict | None:
        return self.sheet["items"][self.queue[0]] if self.sheet and self.queue else None

    def show_current(self):
        items = self.sheet["items"]
        done = sum(1 for it in items if it.get("rating"))
        self.progress.setRange(0, len(items))
        self.progress.setValue(done)
        self.progress.setFormat(f"{done}/{len(items)}")
        inst = self.current()
        if inst is None:
            self.finish()
            return
        self.stage = 0
        self.render()

    def render(self):
        inst = self.current()
        secs = [("", inst["statement"])]
        if self.stage >= 1:
            secs.append(("Solution", inst["solution"]))
        if self.stage >= 2:
            secs.append(("Explanation", inst["explanation"]))
        self.card.set_sections(card_header(inst), secs)
        has_expl = bool(inst.get("explanation"))
        if self.stage == 0:
            self.reveal.setText("Show solution  (Space)")
        elif self.stage == 1 and has_expl:
            self.reveal.setText("Show explanation  (Space)")
        self._buttons(reveal=self.stage == 0 or (self.stage == 1 and has_expl), rate=self.stage >= 1)

    def _buttons(self, reveal: bool, rate: bool):
        self.reveal.setVisible(reveal)
        self.skip.setVisible(self.sheet is not None and bool(self.queue))
        for b in self.rate_btns:
            b.setEnabled(rate)

    def next_stage(self):
        inst = self.current()
        if inst is None:
            return
        if self.stage == 0 or (self.stage == 1 and inst.get("explanation")):
            self.stage += 1
            self.render()

    def rate(self, r: int):
        inst = self.current()
        if inst is None or self.stage == 0:
            return
        self.app.tracker.record(inst["id"], r, variant=bool(inst.get("variant")), sheet=self.sheet.get("id"))
        inst["rating"] = r
        inst["answered"] = C.now().isoformat()
        idx = self.queue.pop(0)
        if r == 1:
            self.queue.append(idx)      # like Anki: failed cards come back at the end of the session
        self.save()
        self.show_current()

    def skip_card(self):
        if len(self.queue) > 1:
            self.queue.append(self.queue.pop(0))
            self.show_current()

    def save(self):
        if self.sheet and not self.sheet.get("temp"):
            self.app.store.save(self.sheet)

    def finish(self):
        items = self.sheet["items"]
        counts = {k: sum(1 for it in items if it.get("rating") == k) for k in C.RATINGS}
        mins = sum(it.get("minutes") or X.DEFAULT_MINUTES for it in items)
        summary = ", ".join(f"{C.RATINGS[k]}: {v}" for k, v in counts.items())
        self.card.set_sections(f"<h2>Worksheet finished 🎉</h2><p>{len(items)} exercises, about {mins} minutes.</p>"
                               f"<p>{summary}</p><p>The exercises you rated <b>Again</b> or <b>Hard</b> will come "
                               f"back sooner on future worksheets.</p>", [])
        self._buttons(reveal=False, rate=False)
        self.skip.setVisible(False)


# =============================================================================
# Worksheets tab
# =============================================================================
class SheetsTab(QWidget):
    def __init__(self, app: "MainWindow"):
        super().__init__()
        self.app = app
        lay = QVBoxLayout(self)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Title", "Created", "Exercises", "Done", "Minutes"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.doubleClicked.connect(lambda *_, f_=self.open_selected: f_())
        lay.addWidget(self.table)
        row = QHBoxLayout()
        for text, fn in (("Open / continue", self.open_selected), ("Restart", self.restart_selected),
                         ("Export Markdown", lambda: self.export("md")), ("Export PDF", lambda: self.export("pdf")),
                         ("Open folder", self.open_folder), ("Delete", self.delete_selected)):
            b = QPushButton(text)
            b.clicked.connect(lambda *_, f_=fn: f_())
            row.addWidget(b)
        row.addStretch(1)
        self.solutions = QComboBox()
        self.solutions.addItems(["end", "callout", "separate", "none"])
        self.solutions.setToolTip("Where solutions go in exports (callout = foldable Obsidian callouts)")
        row.addWidget(QLabel("Solutions in export:"))
        row.addWidget(self.solutions)
        lay.addLayout(row)

    def refresh(self):
        rows = self.app.store.list()
        self.table.setRowCount(len(rows))
        for r, s in enumerate(rows):
            vals = [s["title"], s["created"].replace("T", " ")[:16], s["n"], f'{s["done"]}/{s["n"]}', s["minutes"]]
            for c, v in enumerate(vals):
                it = ro_item(v, align_right=c >= 2)
                if c == 0:
                    it.setData(Qt.ItemDataRole.UserRole, s["id"])
                self.table.setItem(r, c, it)

    def selected_id(self) -> str | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)

    def open_selected(self):
        sid = self.selected_id()
        if sid:
            self.app.start_session(self.app.store.load(sid))

    def restart_selected(self):
        sid = self.selected_id()
        if not sid:
            return
        sheet = self.app.store.load(sid)
        for it in sheet["items"]:
            it["rating"] = it["answered"] = None
        self.app.store.save(sheet)
        self.app.start_session(sheet)

    def export(self, fmt: str):
        sid = self.selected_id()
        if not sid:
            return
        try:
            paths = self.app.store.export(self.app.store.load(sid), fmt, self.solutions.currentText(),
                                          self.app.settings.value("lang", "en"))
        except SystemExit as e:   # the CLI reports missing pandoc/LaTeX this way
            QMessageBox.warning(self, "Export failed", str(e))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths[-1])))

    def open_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.app.store.folder)))

    def delete_selected(self):
        sid = self.selected_id()
        if sid and QMessageBox.question(self, "Delete worksheet", "Delete this worksheet? "
                                        "(Your answer history in the tracker is kept.)") \
                == QMessageBox.StandardButton.Yes:
            self.app.store.delete(sid)
            self.refresh()


# =============================================================================
# Browse tab
# =============================================================================
class BrowseTab(QWidget):
    COLS = ["ID", "Title", "Type", "★", "Min", "Seen", "Last", "Due"]

    def __init__(self, app: "MainWindow"):
        super().__init__()
        self.app = app
        self.rows: list[dict] = []
        self.inst: dict | None = None
        lay = QVBoxLayout(self)
        filt = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("search id, title, type, text …")
        self.deck = QComboBox()
        self.status = QComboBox()
        self.status.addItems(["all", "new", "due", "seen", "last: Again/Hard"])
        for w in (self.search, self.deck, self.status):
            (w.textChanged if isinstance(w, QLineEdit) else w.currentIndexChanged).connect(lambda *_: self.fill())
        filt.addWidget(self.search, 2)
        filt.addWidget(self.deck, 1)
        filt.addWidget(self.status)
        lay.addLayout(filt)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setSortingEnabled(True)
        self.table.itemSelectionChanged.connect(self.show_selected)
        split.addWidget(self.table)

        right = QWidget()
        rl = QVBoxLayout(right)
        self.preview = MathBrowser(app.renderer)
        rl.addWidget(self.preview, 1)
        btns = QHBoxLayout()
        self.show_sol = QCheckBox("show solution && explanation")
        self.show_sol.toggled.connect(lambda *_, f_=self.render: f_())
        self.variant_btn = QPushButton("New variant")
        self.variant_btn.clicked.connect(lambda *_: self.make(True))
        self.orig_btn = QPushButton("Original")
        self.orig_btn.clicked.connect(lambda *_: self.make(False))
        self.practice_btn = QPushButton("Practice this now")
        self.practice_btn.clicked.connect(lambda *_, f_=self.practice: f_())
        self.reset_btn = QPushButton("Reset progress")
        self.reset_btn.clicked.connect(lambda *_, f_=self.reset: f_())
        for w in (self.show_sol, self.orig_btn, self.variant_btn, self.practice_btn, self.reset_btn):
            btns.addWidget(w)
        rl.addLayout(btns)
        edit_row = QHBoxLayout()
        for text, fn in (("New exercise…", lambda: self.app.new_exercise()), ("Edit…", self.edit),
                         ("Delete", self.delete), ("Import…", lambda: self.app.import_dialog())):
            b = QPushButton(text)
            b.clicked.connect(lambda *_, f_=fn: f_())
            edit_row.addWidget(b)
        edit_row.addStretch(1)
        rl.addLayout(edit_row)
        self.hist = QLabel()
        self.hist.setWordWrap(True)
        self.hist.setStyleSheet("color:#666")
        rl.addWidget(self.hist)
        split.addWidget(right)
        split.setSizes([520, 600])
        lay.addWidget(split, 1)

    def refresh(self):
        current = self.deck.currentText()
        self.deck.blockSignals(True)
        self.deck.clear()
        self.deck.addItem("all decks")
        self.deck.addItems(list(self.app.decks))
        idx = self.deck.findText(current)
        self.deck.setCurrentIndex(max(idx, 0))
        self.deck.blockSignals(False)
        self.fill()

    def fill(self):
        states = self.app.tracker.all_states()
        t = C.now()
        q = self.search.text().strip().lower()
        deck = self.deck.currentText()
        status = self.status.currentText()
        rows = []
        for name, exs in self.app.decks.items():
            if deck not in ("", "all decks") and name != deck:
                continue
            for e in exs:
                st = states.get(e["id"])
                if status == "new" and st:
                    continue
                if status == "due" and not C.is_due(st, t):
                    continue
                if status == "seen" and not st:
                    continue
                if status.startswith("last") and not (st and st.get("last_rating") in (1, 2)):
                    continue
                if q:
                    hay = " ".join([e["id"], e.get("title", ""), e.get("type", ""), e.get("topic", ""),
                                    e["original"].get("statement", "")]).lower()
                    if q not in hay:
                        continue
                rows.append((e, st))
        self.rows = [e for e, _ in rows]
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for r, (e, st) in enumerate(rows):
            seen = len(self.app.tracker.history(e["id"], 1000)) if st else 0
            vals = [e["id"], e.get("title", ""), e.get("type", ""), e.get("difficulty") or "", e.get("minutes") or "",
                    seen, C.RATINGS.get(st.get("last_rating"), "") if st else "",
                    (st or {}).get("due", "")[:10] if st else "new"]
            for c, v in enumerate(vals):
                it = ro_item(v, align_right=c in (3, 4, 5))
                if c == 0:
                    it.setData(Qt.ItemDataRole.UserRole, e["id"])
                self.table.setItem(r, c, it)
        self.table.setSortingEnabled(True)

    def selected_ex(self) -> dict | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        eid = self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        return self.app.by_id.get(eid)

    def show_selected(self):
        self.make(False)

    def make(self, variant: bool):
        e = self.selected_ex()
        if not e:
            return
        self.inst = X.make_instance(e, random.Random(), variant and bool(e.get("template")))
        self.inst["deck"] = e.get("_deck", "")
        self.variant_btn.setEnabled(bool(e.get("template")))
        hist = self.app.tracker.history(e["id"], 8)
        self.hist.setText("History: " + (", ".join(f"{ts[:10]} {C.RATINGS[r]}{' (variant)' if v else ''}"
                                                   for ts, r, v in hist) or "never practised"))
        self.render()

    def render(self):
        if not self.inst:
            return
        secs = [("", self.inst["statement"])]
        if self.show_sol.isChecked():
            secs += [("Solution", self.inst["solution"]), ("Explanation", self.inst["explanation"])]
        self.preview.set_sections(card_header(self.inst), secs)

    def practice(self):
        if self.inst:
            item = dict(self.inst, rating=None, answered=None)
            self.app.start_session({"id": None, "title": f"Practice: {item['title']}", "temp": True, "items": [item]})

    def edit(self):
        e = self.selected_ex()
        if e:
            ExerciseEditor(self.app, e).exec()

    def delete(self):
        e = self.selected_ex()
        if e and QMessageBox.question(self, "Delete exercise", f"Delete {e['id']} from deck '{e.get('_deck')}'?\n"
                                      "This changes the bank file; your answer history stays in the tracker.") \
                == QMessageBox.StandardButton.Yes:
            C.delete_exercise(self.app.bank_dir, e["_deck"], e["id"])
            self.inst = None
            self.preview.clear()
            self.app.load_decks()

    def reset(self):
        e = self.selected_ex()
        if e and QMessageBox.question(self, "Reset", f"Forget the schedule of {e['id']}? (It becomes 'new' again; "
                                      "the answer log is kept.)") == QMessageBox.StandardButton.Yes:
            self.app.tracker.reset(e["id"])
            self.fill()



# =============================================================================
# Dialogs for adding / editing exercises
# =============================================================================
class ExerciseEditor(QDialog):
    """Create a new exercise or edit an existing one, with live preview."""

    def __init__(self, app: "MainWindow", ex: dict | None = None):
        super().__init__(app)
        self.app, self.orig = app, ex
        self.setWindowTitle("Edit exercise" if ex else "New exercise")
        self.resize(1150, 760)
        lay = QHBoxLayout(self)

        left = QVBoxLayout()
        form = QFormLayout()
        self.deck = QComboBox()
        self.deck.setEditable(True)
        self.deck.addItems(sorted(app.decks))
        self.deck.setToolTip("Pick a deck or type a new name, e.g. 'own/analysis' (folders with /)")
        self.eid = QLineEdit()
        self.title_ = QLineEdit()
        self.type_ = QComboBox()
        self.type_.setEditable(True)
        self.type_.addItems(sorted({e.get("type", "") for e in app.by_id.values() if e.get("type")}))
        self.topic = QLineEdit()
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("comma separated, optional")
        self.diff, self.mins = QSpinBox(), QSpinBox()
        self.diff.setRange(1, 5)
        self.mins.setRange(1, 180)
        row = QHBoxLayout()
        row.addWidget(QLabel("Difficulty"))
        row.addWidget(self.diff)
        row.addWidget(QLabel("Minutes"))
        row.addWidget(self.mins)
        row.addStretch(1)
        form.addRow("Deck", self.deck)
        form.addRow("ID", self.eid)
        form.addRow("Title", self.title_)
        form.addRow("Type", self.type_)
        form.addRow("Topic", self.topic)
        form.addRow("Tags", self.tags)
        form.addRow(row)
        left.addLayout(form)
        self.texts = {}
        for key, label, h in (("statement", "Statement", 150), ("solution", "Solution (final answer only)", 70),
                              ("explanation", "Explanation (steps)", 150)):
            left.addWidget(QLabel(f"{label} — Markdown, math in $…$ or $$…$$:"))
            ed = QPlainTextEdit()
            ed.setMinimumHeight(h)
            ed.textChanged.connect(lambda *_: self.timer.start())
            left.addWidget(ed)
            self.texts[key] = ed
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color:#a05a00")
        left.addWidget(self.note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(lambda *_: self.save())
        buttons.rejected.connect(lambda *_: self.reject())
        left.addWidget(buttons)
        lay.addLayout(left, 1)

        right = QVBoxLayout()
        right.addWidget(QLabel("Preview"))
        self.preview = MathBrowser(app.renderer)
        right.addWidget(self.preview, 1)
        lay.addLayout(right, 1)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(400)
        self.timer.timeout.connect(lambda *_: self.update_preview())

        if ex:
            self.deck.setCurrentText(ex.get("_deck", ""))
            self.eid.setText(ex["id"])
            self.title_.setText(ex.get("title", ""))
            self.type_.setCurrentText(ex.get("type", ""))
            self.topic.setText(ex.get("topic", ""))
            self.tags.setText(", ".join(ex.get("tags", [])))
            self.diff.setValue(ex.get("difficulty") or 2)
            self.mins.setValue(ex.get("minutes") or 5)
            for k, ed in self.texts.items():
                ed.setPlainText(ex.get("original", {}).get(k, ""))
            if ex.get("template"):
                self.note.setText("This exercise has a template for generated variants. It is kept unchanged; your "
                                  "edits change the original version only.")
        else:
            start = app.settings.value("last_deck", "own/my_exercises")
            self.deck.setCurrentText(start)
            self.eid.setText(C.next_id(app.bank_dir, start))
            self.diff.setValue(2)
            self.mins.setValue(5)
            self.deck.currentTextChanged.connect(lambda t: self.eid.setText(C.next_id(app.bank_dir, t.strip() or "own")))
        self.update_preview()

    def exercise(self) -> dict:
        ex = dict(self.orig) if self.orig else {}
        ex.update({
            "id": self.eid.text().strip(), "title": self.title_.text().strip() or self.eid.text().strip(),
            "type": slugify_type(self.type_.currentText()), "topic": self.topic.text().strip(),
            "tags": [t.strip() for t in self.tags.text().split(",") if t.strip()],
            "difficulty": self.diff.value(), "minutes": self.mins.value(),
            "original": {k: ed.toPlainText().strip() for k, ed in self.texts.items()},
        })
        ex.setdefault("template", None)
        return ex

    def update_preview(self):
        ex = self.exercise()
        inst = dict(ex, **ex["original"], deck=self.deck.currentText(), variant=False)
        self.preview.set_sections(card_header(inst), [("", inst["statement"]), ("Solution", inst["solution"]),
                                                      ("Explanation", inst["explanation"])])

    def save(self):
        deck = self.deck.currentText().strip().strip("/")
        ex = self.exercise()
        if not deck:
            QMessageBox.warning(self, "Deck missing", "Choose or type a deck name.")
            return
        old_deck = self.orig.get("_deck") if self.orig else None
        errors, warnings = C.check_exercises([ex], C.all_ids(self.app.bank_dir), own_deck=old_deck or deck)
        if self.orig and ex["id"] == self.orig["id"]:
            errors = [e for e in errors if "already used" not in e]
        if errors:
            QMessageBox.warning(self, "Cannot save", "\n".join(errors))
            return
        C.save_exercise(self.app.bank_dir, deck, ex, old_id=self.orig["id"] if self.orig else None, old_deck=old_deck)
        self.app.settings.setValue("last_deck", deck)
        self.app.load_decks()
        self.accept()


def slugify_type(t: str) -> str:
    return "_".join(t.strip().lower().split()) or "general"


class ImportDialog(QDialog):
    """Paste the JSON Claude returned (or open a .json file), check it, save it as a deck."""

    def __init__(self, app: "MainWindow"):
        super().__init__(app)
        self.app = app
        self.data: dict | None = None
        self.setWindowTitle("Import exercises")
        self.resize(900, 700)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Paste the answer from Claude (chat text around the JSON is fine), or open a bank file:"))
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText('{ "schema_version": 1, "source": {...}, "exercises": [ ... ] }')
        self.text.textChanged.connect(lambda *_: self.timer.start())
        lay.addWidget(self.text, 1)
        row = QHBoxLayout()
        open_btn = QPushButton("Open file…")
        open_btn.clicked.connect(lambda *_: self.open_file())
        prompt_btn = QPushButton("Copy extraction prompt")
        prompt_btn.setToolTip("Copies the prompt to paste into a Claude chat together with your worksheet PDF")
        prompt_btn.clicked.connect(lambda *_: self.copy_prompt())
        row.addWidget(open_btn)
        row.addWidget(prompt_btn)
        row.addStretch(1)
        row.addWidget(QLabel("Save as deck:"))
        self.deck = QComboBox()
        self.deck.setEditable(True)
        self.deck.addItems(sorted(app.decks))
        self.deck.setCurrentText("")
        self.deck.setMinimumWidth(260)
        self.deck.currentTextChanged.connect(lambda *_: self.timer.start())
        row.addWidget(self.deck)
        lay.addLayout(row)
        self.report = QLabel("Nothing pasted yet.")
        self.report.setWordWrap(True)
        self.report.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.report)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.import_btn = self.buttons.addButton("Import", QDialogButtonBox.ButtonRole.AcceptRole)
        self.import_btn.setEnabled(False)
        self.buttons.accepted.connect(lambda *_: self.do_import())
        self.buttons.rejected.connect(lambda *_: self.reject())
        lay.addWidget(self.buttons)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(500)
        self.timer.timeout.connect(lambda *_: self.check())

    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open bank file", str(Path.home()), "JSON (*.json);;All files (*)")
        if path:
            self.text.setPlainText(Path(path).read_text(encoding="utf-8"))
            if not self.deck.currentText():
                self.deck.setCurrentText(Path(path).stem)

    def copy_prompt(self):
        from .paths import prompt_file
        f = prompt_file()
        if f:
            QApplication.clipboard().setText(f.read_text(encoding="utf-8").split("---", 1)[-1].strip())
            QMessageBox.information(self, "Copied", "Prompt copied. Paste it into a Claude chat and attach your PDF.")
            return
        QMessageBox.warning(self, "Not found", "EXTRACTION_PROMPT.md was not found.")

    def check(self):
        self.data = None
        self.import_btn.setEnabled(False)
        raw = self.text.toPlainText().strip()
        if not raw:
            self.report.setText("Nothing pasted yet.")
            return
        try:
            data = C.parse_bank_text(raw)
        except (ValueError, json.JSONDecodeError) as e:
            self.report.setText(f"<b style='color:#c62828'>Not readable as a bank:</b> {e}<br>"
                                "Tip: LaTeX backslashes must be doubled inside JSON (\\\\frac).")
            return
        if not self.deck.currentText().strip():
            self.deck.setCurrentText(C.slug(data.get("source", {}).get("title", "imported")).replace("-", "_"))
        deck = self.deck.currentText().strip().strip("/")
        errors, warnings = C.check_exercises(data["exercises"], C.all_ids(self.app.bank_dir), own_deck=deck)
        n = len(data["exercises"])
        gen = sum(1 for e in data["exercises"] if e.get("template"))
        exists = deck in self.app.decks
        html = [f"<b>{n} exercises</b> ({gen} with generated variants) → deck <b>{deck}</b>"
                + (" <i>(exists: new exercises are added, same ids are replaced)</i>" if exists else " <i>(new deck)</i>")]
        if errors:
            html.append("<b style='color:#c62828'>Errors (fix them or remove those exercises):</b><br>" + "<br>".join(errors[:15]))
        if warnings:
            html.append("<span style='color:#a05a00'>Warnings:</span><br>" + "<br>".join(warnings[:10])
                        + ("<br>…" if len(warnings) > 10 else ""))
        if not errors:
            html.append("<span style='color:#2e7d32'>Ready to import.</span>")
            self.data = data
            self.import_btn.setEnabled(True)
        self.report.setText("<br><br>".join(html))

    def do_import(self):
        if not self.data:
            return
        deck = self.deck.currentText().strip().strip("/")
        path = C.import_bank(self.app.bank_dir, self.data, deck)
        self.app.load_decks()
        QMessageBox.information(self, "Imported", f"Saved {len(self.data['exercises'])} exercises to\n{path}")
        self.accept()

# =============================================================================
# Main window
# =============================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("woge", "woge")
        ensure_user_dirs()
        self.bank_dir = Path(self.settings.value("bank_dir", str(BANK_DIR)))
        if not self.bank_dir.is_dir():   # stale saved choice (e.g. folder moved) -> back to dat/banks
            self.bank_dir = BANK_DIR
            self.settings.remove("bank_dir")
        self.data_dir = Path(self.settings.value("data_dir", str(DATA_DIR)))
        self.tracker = C.Tracker(self.data_dir / "tracker.sqlite")
        self.store = C.SheetStore(self.data_dir / "worksheets")
        self.renderer = C.MathRenderer(fontsize=15)
        self.decks: dict[str, list[dict]] = {}
        self.by_id: dict[str, dict] = {}

        self.setWindowTitle("woge")
        self.resize(1200, 800)
        self.tabs = QTabWidget()
        self.decks_tab, self.study_tab = DecksTab(self), StudyTab(self)
        self.sheets_tab, self.browse_tab = SheetsTab(self), BrowseTab(self)
        for w, name in ((self.decks_tab, "Decks"), (self.study_tab, "Study"), (self.sheets_tab, "Worksheets"),
                        (self.browse_tab, "Browse")):
            self.tabs.addTab(w, name)
        self.tabs.currentChanged.connect(self.on_tab)
        self.setCentralWidget(self.tabs)
        self.build_menu()
        self.load_decks()

    def build_menu(self):
        m = self.menuBar().addMenu("&File")
        for text, fn, key in (("New exercise…", self.new_exercise, "Ctrl+N"),
                              ("Import exercises (paste JSON / file)…", self.import_dialog, "Ctrl+I"),
                              ("Choose bank folder…", self.choose_banks, None), ("Reload banks", self.load_decks, "F5"),
                              ("Open data folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_dir))), None),
                              ("Quit", self.close, "Ctrl+Q")):
            a = QAction(text, self)
            if key:
                a.setShortcut(QKeySequence(key))
            a.triggered.connect(lambda *_, f_=fn: f_())
            m.addAction(a)
        lang = self.menuBar().addMenu("&Export language")
        for code in X.LABELS:
            a = QAction(code, self, checkable=True)
            a.setChecked(self.settings.value("lang", "en") == code)
            a.triggered.connect(lambda _=False, c=code: self.settings.setValue("lang", c))
            lang.addAction(a)

    def choose_banks(self):
        d = QFileDialog.getExistingDirectory(self, "Choose the folder that contains your bank JSON files",
                                             str(self.bank_dir))
        if d:
            self.bank_dir = Path(d)
            self.settings.setValue("bank_dir", d)
            self.load_decks()

    def load_decks(self):
        if not self.bank_dir.exists():
            QMessageBox.warning(self, "Bank folder missing", f"{self.bank_dir} does not exist. Use File → Choose bank folder.")
            self.decks = {}
        else:
            self.decks = C.load_decks(self.bank_dir)
        self.by_id = {e["id"]: e for v in self.decks.values() for e in v}
        self.statusBar().showMessage(f"{len(self.by_id)} exercises in {len(self.decks)} decks from {self.bank_dir}")
        self.on_tab(self.tabs.currentIndex())

    def on_tab(self, _idx: int):
        w = self.tabs.currentWidget()
        if w is self.decks_tab:
            self.decks_tab.refresh()
        elif w is self.sheets_tab:
            self.sheets_tab.refresh()
        elif w is self.browse_tab:
            self.browse_tab.refresh()

    def new_exercise(self):
        ExerciseEditor(self).exec()

    def import_dialog(self):
        ImportDialog(self).exec()

    def start_session(self, sheet: dict):
        if not sheet["items"]:
            QMessageBox.information(self, "Empty worksheet", "No exercises could be selected.")
            return
        self.study_tab.open(sheet)
        self.tabs.setCurrentWidget(self.study_tab)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("woge")
    app.setStyleSheet(APP_CSS)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
