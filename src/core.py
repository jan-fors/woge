"""
core – everything woge needs that does not depend on Qt:

* Tracker      : SQLite log of every answer + a small spaced-repetition state per exercise
* build_sheet  : picks exercises for a worksheet (prefers new/due/hard ones, avoids
                 recently seen ones, balances difficulty and exercise types)
* SheetStore   : saves/loads worksheets as JSON (+ Markdown/PDF export via the CLI renderer)
* to_html      : turns exercise text (Markdown + LaTeX) into HTML with formula images
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import random
import re
import sqlite3
from pathlib import Path

from . import cli as X

RATINGS = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}


def now() -> dt.datetime:
    return dt.datetime.now().replace(microsecond=0)


# =============================================================================
# Decks (= bank files, grouped by folder)
# =============================================================================
def load_decks(bank_root: Path) -> dict[str, list[dict]]:
    """{'algebra/ch1_preliminaries': [exercises...], ...} for every JSON file below bank_root."""
    decks: dict[str, list[dict]] = {}
    for f in sorted(Path(bank_root).glob("**/*.json")):
        try:
            exs = X.load_banks([str(f)])
        except SystemExit:
            continue  # broken or empty bank file: skip it instead of closing the app
        name = str(f.relative_to(bank_root).with_suffix("")).replace("\\", "/")
        for e in exs:
            e["_deck"] = name
        decks[name] = exs
    return decks


# =============================================================================
# Tracker (SQLite)
# =============================================================================
class Tracker:
    """Answer log + SM-2-like scheduling state per exercise id."""

    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ex_id TEXT NOT NULL, ts TEXT NOT NULL,
                rating INTEGER NOT NULL, variant INTEGER NOT NULL DEFAULT 0, sheet TEXT);
            CREATE TABLE IF NOT EXISTS state (
                ex_id TEXT PRIMARY KEY, reps INTEGER NOT NULL DEFAULT 0, lapses INTEGER NOT NULL DEFAULT 0,
                ease REAL NOT NULL DEFAULT 2.5, interval REAL NOT NULL DEFAULT 0,
                due TEXT, last TEXT, last_rating INTEGER);
            CREATE INDEX IF NOT EXISTS idx_reviews_ex ON reviews(ex_id);
        """)
        self.db.commit()

    # ---- reading -------------------------------------------------------------
    def state(self, ex_id: str) -> dict | None:
        cur = self.db.execute("SELECT reps, lapses, ease, interval, due, last, last_rating FROM state WHERE ex_id=?",
                              (ex_id,))
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip(("reps", "lapses", "ease", "interval", "due", "last", "last_rating"), row))

    def all_states(self) -> dict[str, dict]:
        cur = self.db.execute("SELECT ex_id, reps, lapses, ease, interval, due, last, last_rating FROM state")
        keys = ("reps", "lapses", "ease", "interval", "due", "last", "last_rating")
        return {r[0]: dict(zip(keys, r[1:])) for r in cur.fetchall()}

    def history(self, ex_id: str, limit: int = 20) -> list[tuple[str, int, int]]:
        cur = self.db.execute("SELECT ts, rating, variant FROM reviews WHERE ex_id=? ORDER BY id DESC LIMIT ?",
                              (ex_id, limit))
        return cur.fetchall()

    def reviews_on(self, day: dt.date) -> int:
        cur = self.db.execute("SELECT COUNT(*) FROM reviews WHERE substr(ts,1,10)=?", (day.isoformat(),))
        return cur.fetchone()[0]

    # ---- writing -------------------------------------------------------------
    def record(self, ex_id: str, rating: int, variant: bool = False, sheet: str | None = None,
               when: dt.datetime | None = None) -> dict:
        """Log an answer and update the schedule. rating: 1 Again, 2 Hard, 3 Good, 4 Easy."""
        when = when or now()
        st = self.state(ex_id) or {"reps": 0, "lapses": 0, "ease": 2.5, "interval": 0.0}
        reps, lapses, ease, ivl = st["reps"], st["lapses"], st["ease"], st["interval"]
        if rating == 1:                      # forgot / couldn't solve: see it again soon
            lapses += 1
            reps = 0
            ease = max(1.3, ease - 0.2)
            ivl = 0.0
            due = when + dt.timedelta(minutes=10)
        else:
            if rating == 2:
                ease = max(1.3, ease - 0.15)
                ivl = 1.0 if reps == 0 else max(1.0, ivl * 1.2)
            elif rating == 3:
                ivl = 1.0 if reps == 0 else (3.0 if reps == 1 else ivl * ease)
            else:                            # easy
                ease += 0.15
                ivl = 4.0 if reps == 0 else ivl * ease * 1.3
            reps += 1
            due = when + dt.timedelta(days=ivl)
        self.db.execute("INSERT INTO reviews (ex_id, ts, rating, variant, sheet) VALUES (?,?,?,?,?)",
                        (ex_id, when.isoformat(), rating, int(variant), sheet))
        self.db.execute("""INSERT INTO state (ex_id, reps, lapses, ease, interval, due, last, last_rating)
                           VALUES (?,?,?,?,?,?,?,?)
                           ON CONFLICT(ex_id) DO UPDATE SET reps=excluded.reps, lapses=excluded.lapses,
                           ease=excluded.ease, interval=excluded.interval, due=excluded.due,
                           last=excluded.last, last_rating=excluded.last_rating""",
                        (ex_id, reps, lapses, ease, ivl, due.isoformat(), when.isoformat(), rating))
        self.db.commit()
        return self.state(ex_id)

    def reset(self, ex_id: str):
        self.db.execute("DELETE FROM state WHERE ex_id=?", (ex_id,))
        self.db.commit()


def is_due(st: dict | None, t: dt.datetime) -> bool:
    return bool(st and st.get("due") and dt.datetime.fromisoformat(st["due"]) <= t)


def priority(st: dict | None, t: dt.datetime) -> float:
    """How much an exercise 'wants' to be on the next worksheet.

    new > due (more if overdue / often failed) > not due yet > seen in the last 20 hours.
    Nothing is ever impossible, so sheets stay varied even with few exercises.
    """
    if st is None:
        return 3.0
    last = dt.datetime.fromisoformat(st["last"]) if st.get("last") else None
    if last and (t - last).total_seconds() < 20 * 3600 and st.get("last_rating", 3) != 1:
        return 0.05
    if is_due(st, t):
        overdue = (t - dt.datetime.fromisoformat(st["due"])).total_seconds() / 86400
        return 2.0 + min(overdue, 10) * 0.1 + 0.3 * min(st.get("lapses", 0), 5)
    return 0.4


def deck_counts(exs: list[dict], states: dict[str, dict], t: dt.datetime) -> tuple[int, int, int]:
    """(total, new, due) for a list of exercises."""
    new = sum(1 for e in exs if e["id"] not in states)
    due = sum(1 for e in exs if is_due(states.get(e["id"]), t))
    return len(exs), new, due


# =============================================================================
# Worksheet selection
# =============================================================================
def build_sheet(exercises: list[dict], tracker: Tracker, *, n: int | None = None, minutes: int | None = None,
                mode: str = "variants", balance: bool = True, seed: int | None = None) -> list[dict]:
    """Return a list of concrete exercise instances (dicts) for a new worksheet."""
    if not exercises:
        return []
    seed = seed if seed is not None else random.randrange(10 ** 6)
    rng = random.Random(seed)
    t = now()
    states = tracker.all_states()

    # 1) weighted random order (Efraimidis–Spirakis keys): high priority tends to come first
    keyed, weight = [], {}
    for e in exercises:
        w = max(priority(states.get(e["id"]), t), 1e-6)
        weight[id(e)] = w
        keyed.append((rng.random() ** (1.0 / w), e))
    order = [e for _, e in sorted(keyed, key=lambda p: -p[0])]

    # 2) interleave difficulty levels round-robin and prefer exercise types not used yet
    if balance:
        buckets: dict[int, list[dict]] = {}
        for e in order:
            buckets.setdefault(e.get("difficulty") or 0, []).append(e)
        levels = list(buckets)
        rng.shuffle(levels)
        seq, used_types = [], set()
        while any(buckets.values()):
            best = max(weight[id(b[0])] for b in buckets.values() if b)
            for lvl in levels:
                b = buckets[lvl]
                if not b:
                    continue
                if weight[id(b[0])] < 0.2 * best:
                    continue  # this level only has recently seen exercises left: don't force it in
                pick = next((i for i, e in enumerate(b[:4]) if e.get("type") not in used_types), 0)
                e = b.pop(pick)
                used_types.add(e.get("type"))
                seq.append(e)
        order = seq

    # 3) cut to size: count or time budget (fill without overshooting, then best small overshoot)
    if minutes:
        chosen, total, rest = [], 0, []
        for e in order:
            m = X.ex_minutes(e)
            if total + m <= minutes and (not n or len(chosen) < n):
                chosen.append(e)
                total += m
            else:
                rest.append(e)
        tol = max(2, round(minutes * 0.1))
        if total < minutes and (not n or len(chosen) < n):
            fits = [e for e in rest if total + X.ex_minutes(e) <= minutes + tol]
            if fits:
                chosen.append(min(fits, key=X.ex_minutes))
    else:
        chosen = order[: (n or 5)]

    chosen.sort(key=lambda e: e.get("difficulty") or 0)
    items = []
    for e in chosen:
        variant = {"variants": True, "originals": False}.get(mode, rng.random() < 0.5)
        inst = X.make_instance(e, rng, variant)
        inst.update(deck=e.get("_deck", ""), rating=None, answered=None)
        items.append(inst)
    return items


# =============================================================================
# Worksheet storage
# =============================================================================
class SheetStore:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def new(self, items: list[dict], settings: dict, title: str | None = None) -> dict:
        t = now()
        sid = t.strftime("%Y-%m-%d_%H%M%S")
        sheet = {"id": sid, "created": t.isoformat(), "title": title or f"Worksheet {t:%Y-%m-%d %H:%M}",
                 "settings": settings, "items": items}
        self.save(sheet)
        return sheet

    def path(self, sid: str) -> Path:
        return self.folder / f"{sid}.json"

    def save(self, sheet: dict):
        self.path(sheet["id"]).write_text(json.dumps(sheet, indent=2, ensure_ascii=False), encoding="utf-8")

    def load(self, sid: str) -> dict:
        return json.loads(self.path(sid).read_text(encoding="utf-8"))

    def list(self) -> list[dict]:
        out = []
        for f in sorted(self.folder.glob("*.json"), reverse=True):
            try:
                s = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            done = sum(1 for i in s["items"] if i.get("rating"))
            mins = sum(i.get("minutes") or X.DEFAULT_MINUTES for i in s["items"])
            out.append({"id": s["id"], "title": s.get("title", s["id"]), "created": s.get("created", ""),
                        "n": len(s["items"]), "done": done, "minutes": mins})
        return out

    def delete(self, sid: str):
        self.path(sid).unlink(missing_ok=True)

    def export(self, sheet: dict, fmt: str = "md", solutions: str = "end", lang: str = "en",
               pdf_engine: str | None = None) -> list[Path]:
        """Write Markdown and/or PDF next to the worksheet JSON (uses the CLI renderer)."""
        labels = X.LABELS.get(lang, X.LABELS["en"])
        base = self.folder / sheet["id"]
        out = []
        if fmt in ("md", "both"):
            main, sep = X.render_markdown(sheet["items"], sheet["title"], 0, solutions, labels)
            base.with_suffix(".md").write_text(main, encoding="utf-8")
            out.append(base.with_suffix(".md"))
        if fmt in ("pdf", "both"):
            mode = "end" if solutions == "callout" else solutions
            main, _ = X.render_markdown(sheet["items"], sheet["title"], 0, mode, labels, for_pdf=True)
            X.to_pdf(main, base.with_suffix(".pdf"), pdf_engine)
            out.append(base.with_suffix(".pdf"))
        return out


# =============================================================================
# Text + LaTeX -> HTML with formula images (matplotlib mathtext, no LaTeX install needed)
# =============================================================================
_SUBS = [  # constructs mathtext doesn't know -> close equivalents
    (r"\dfrac", r"\frac"), (r"\tfrac", r"\frac"), (r"\displaystyle", ""),
    (r"\left|", r"|"), (r"\right|", r"|"), (r"\left\{", r"\{"), (r"\right\}", r"\}"),
    (r"\left[", r"["), (r"\right]", r"]"), (r"\left(", r"("), (r"\right)", r")"), (r"\right.", ""),
    (r"\checkmark", r"\surd"), (r"\iff", r"\Leftrightarrow"), (r"\implies", r"\Rightarrow"),
    (r"\lvert", "|"), (r"\rvert", "|"), (r"\mid", "|"), (r"\le ", r"\leq "), (r"\ge ", r"\geq "),
    (r"\,", r"\ "), (r"\;", r"\ "), (r"\!", ""), (r"\quad", r"\ \ "), (r"\qquad", r"\ \ \ \ "),
    (r"\lVert", "||"), (r"\rVert", "||"), (r"\lfloor", "⌊"), (r"\rfloor", "⌋"), (r"\%", "%"), (r"\ldots", r"\dots"), (r"\cdots", r"\dots"),
]


def _sanitize(tex: str) -> str:
    s = tex
    for a, b in _SUBS:
        s = s.replace(a, b)
    s = re.sub(r"\\(?:text|textrm|operatorname|mathrm)\{([^{}]*)\}", lambda m: r"\mathrm{" + m.group(1).replace(" ", r"\ ") + "}", s)
    s = re.sub(r"\\le(?![a-zA-Z])", r"\\leq", s)
    s = re.sub(r"\\ge(?![a-zA-Z])", r"\\geq", s)
    s = re.sub(r"\\ne(?![a-zA-Z])", r"\\neq", s)
    # TeX shorthand without braces: \frac14, \frac1c, \frac{a}b, \sqrt3
    tok = r"([0-9a-zA-Z]|\\[a-zA-Z]+)"
    s = re.sub(r"\\frac\s*" + tok + r"\s*" + tok, r"\\frac{\1}{\2}", s)
    s = re.sub(r"\\frac\s*(\{[^{}]*\})\s*" + tok, r"\\frac\1{\2}", s)
    s = re.sub(r"\\frac\s*" + tok + r"\s*(\{)", r"\\frac{\1}\2", s)
    s = re.sub(r"\\sqrt\s*([0-9a-zA-Z])", r"\\sqrt{\1}", s)
    s = re.sub(r"\\(mathcal|mathbb|mathbf)\s*([A-Za-z])", r"\\\1{\2}", s)
    s = re.sub(r"\\begin\{(?:cases|smallmatrix|matrix|pmatrix)\}.*?\\end\{(?:cases|smallmatrix|matrix|pmatrix)\}",
               lambda m: _flatten_env(m.group(0)), s, flags=re.S)
    return s


def _flatten_env(env: str) -> str:
    """mathtext has no environments: show cases/matrices as '[row; row]' on one line."""
    body = re.sub(r"\\(?:begin|end)\{\w+\}", "", env)
    rows = [r.replace("&", r"\ ") for r in body.split(r"\\")]
    return r"\{\ " + r";\ \ ".join(r.strip() for r in rows if r.strip()) + r"\ \}"


class MathRenderer:
    """Renders $...$ snippets to PNG (cached). Falls back to showing the TeX source."""

    def __init__(self, fontsize: int = 15, color: str = "#202020", dpi: int = 200):
        from matplotlib.font_manager import FontProperties
        from matplotlib import mathtext  # noqa: F401  (import check)
        self.prop = FontProperties(size=fontsize)
        self.color, self.dpi = color, dpi
        self.cache: dict[str, tuple[bytes, int, int] | None] = {}

    def png(self, tex: str) -> tuple[bytes, int, int] | None:
        key = hashlib.sha1(f"{tex}|{self.color}|{self.prop.get_size()}|{self.dpi}".encode()).hexdigest()
        if key in self.cache:
            return self.cache[key]
        from matplotlib.mathtext import math_to_image
        result = None
        for candidate in (tex, _sanitize(tex)):
            buf = io.BytesIO()
            try:
                math_to_image(f"${candidate}$", buf, prop=self.prop, dpi=self.dpi, format="png", color=self.color)
                data = buf.getvalue()
                w, h = _png_size(data)
                result = (data, w, h)
                break
            except Exception:  # noqa: BLE001  (mathtext raises ValueError & friends)
                continue
        self.cache[key] = result
        return result


def _png_size(data: bytes) -> tuple[int, int]:
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


_DOLLAR = "\x00DOLLAR\x00"


def _md_inline(text: str) -> str:
    text = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return text


def to_html(text: str, renderer: MathRenderer | None, images: dict[str, bytes]) -> str:
    """Markdown-ish text with $..$ / $$..$$ math -> HTML. Formula PNGs are put into `images`
    (name -> bytes) and referenced as <img src="math:NAME">."""
    if not text:
        return ""
    text = text.replace(r"\$", _DOLLAR)
    parts = re.split(r"(\$\$.+?\$\$|\$.+?\$)", text, flags=re.S)
    html = []
    for part in parts:
        if part.startswith("$") and part.endswith("$") and len(part) > 1:
            display = part.startswith("$$")
            tex = part[2:-2] if display else part[1:-1]
            img = renderer.png(tex.strip()) if renderer else None
            if img:
                data, w, h = img
                name = hashlib.sha1(data).hexdigest()[:16]
                images[name] = data
                scale = 72 / renderer.dpi * 1.33       # PNG pixels at render dpi -> screen pixels
                tag = (f'<img src="math:{name}" width="{int(w * scale)}" height="{int(h * scale)}" '
                       f'style="vertical-align: middle">')
                html.append(f'<div align="center">{tag}</div>' if display else tag)
            else:
                src = _md_inline(tex)
                html.append(f'<div align="center"><code>{src}</code></div>' if display else f"<code>{src}</code>")
        else:
            html.append(_md_inline(part.replace(_DOLLAR, "$")))
    out = "".join(html)
    paras = [p.strip() for p in re.split(r"\n\s*\n", out) if p.strip()]
    return "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paras)


# =============================================================================
# Editing banks (used by the GUI's editor and import dialog)
# =============================================================================
def deck_file(bank_dir: Path, deck: str) -> Path:
    return Path(bank_dir) / f"{deck}.json"


def _read_bank(path: Path, deck: str) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"schema_version": 1, "source": {"title": Path(deck).name.replace("_", " "), "subject": "", "language": "en"},
            "exercises": []}


def _write_bank(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)                      # atomic: a crash never leaves a half-written bank


def clean_exercise(e: dict) -> dict:
    """Drop runtime fields (_deck, _file, _source) before saving."""
    return {k: v for k, v in e.items() if not k.startswith("_")}


def all_ids(bank_dir: Path) -> dict[str, str]:
    """id -> deck name, read directly from the files."""
    out = {}
    for f in Path(bank_dir).glob("**/*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        deck = str(f.relative_to(bank_dir).with_suffix("")).replace("\\", "/")
        for e in data.get("exercises", []):
            if "id" in e:
                out[e["id"]] = deck
    return out


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "ex"


def next_id(bank_dir: Path, deck: str) -> str:
    prefix = slug(Path(deck).name)
    nums = [int(m.group(1)) for i in all_ids(bank_dir) if (m := re.fullmatch(re.escape(prefix) + r"-(\d+)", i))]
    return f"{prefix}-{max(nums, default=0) + 1:03d}"


def save_exercise(bank_dir: Path, deck: str, ex: dict, old_id: str | None = None, old_deck: str | None = None) -> Path:
    """Insert or replace an exercise. When editing, old_id/old_deck locate the previous version
    (it is removed there, so moving an exercise to another deck works too)."""
    ex = clean_exercise(ex)
    if old_id and old_deck and (old_deck != deck or old_id != ex["id"]):
        delete_exercise(bank_dir, old_deck, old_id)
        old_id = None
    path = deck_file(bank_dir, deck)
    data = _read_bank(path, deck)
    exs = data.setdefault("exercises", [])
    for i, e in enumerate(exs):
        if e.get("id") == (old_id or ex["id"]):
            exs[i] = ex
            break
    else:
        exs.append(ex)
    _write_bank(path, data)
    return path


def delete_exercise(bank_dir: Path, deck: str, ex_id: str) -> bool:
    path = deck_file(bank_dir, deck)
    if not path.exists():
        return False
    data = _read_bank(path, deck)
    before = len(data.get("exercises", []))
    data["exercises"] = [e for e in data.get("exercises", []) if e.get("id") != ex_id]
    if len(data["exercises"]) == before:
        return False
    if data["exercises"]:
        _write_bank(path, data)
    else:
        path.unlink()                      # last exercise gone: remove the empty deck file
    return True


def parse_bank_text(text: str) -> dict:
    """Accept a bank as Claude returns it: plain JSON, inside ```json fences, or with chat text around it."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    candidate = m.group(1) if m else text
    start = candidate.find("{")
    if start < 0:
        raise ValueError("No JSON object found.")
    obj, _ = json.JSONDecoder().raw_decode(candidate[start:])
    if isinstance(obj, dict) and "exercises" not in obj and "id" in obj:
        obj = {"schema_version": 1, "source": {}, "exercises": [obj]}   # a single exercise
    if not isinstance(obj, dict) or not isinstance(obj.get("exercises"), list):
        raise ValueError("The JSON has no 'exercises' list.")
    return obj


def check_exercises(exs: list[dict], existing: dict[str, str] | None = None, own_deck: str | None = None,
                    k: int = 8) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors block saving/importing, warnings don't."""
    errors, warnings = [], []
    existing = existing or {}
    seen = set()
    rng = random.Random(0)
    for e in exs:
        eid = e.get("id")
        if not eid:
            errors.append("An exercise has no id.")
            continue
        if eid in seen:
            errors.append(f"{eid}: id appears twice.")
        seen.add(eid)
        if eid in existing and existing[eid] != own_deck:
            errors.append(f"{eid}: id already used in deck '{existing[eid]}'.")
        o = e.get("original") or {}
        if not o.get("statement"):
            errors.append(f"{eid}: no statement.")
        if not o.get("solution"):
            errors.append(f"{eid}: no solution.")
        if e.get("difficulty") not in (1, 2, 3, 4, 5):
            warnings.append(f"{eid}: no difficulty 1–5 (counts as unrated).")
        if not isinstance(e.get("minutes"), int):
            warnings.append(f"{eid}: no minutes estimate (counts as {X.DEFAULT_MINUTES}).")
        if e.get("template"):
            try:
                for _ in range(k):
                    inst = X.make_instance(e, rng, True)
                    for part in ("statement", "solution", "explanation"):
                        if re.search(r"\{\{[^{}]*\}\}", inst[part]):
                            raise ValueError(f"unfilled placeholder in {part}")
            except Exception as ex:  # noqa: BLE001
                errors.append(f"{eid}: template fails ({type(ex).__name__}: {ex}).")
    return errors, warnings


def import_bank(bank_dir: Path, data: dict, deck: str, replace: bool = False) -> Path:
    """Write an already checked bank as deck `deck`. replace=False appends to an existing deck."""
    path = deck_file(bank_dir, deck)
    if path.exists() and not replace:
        old = _read_bank(path, deck)
        ids = {e["id"] for e in data["exercises"]}
        old["exercises"] = [e for e in old["exercises"] if e.get("id") not in ids] + data["exercises"]
        data = old
    _write_bank(path, {**data, "exercises": [clean_exercise(e) for e in data["exercises"]]})
    return path
