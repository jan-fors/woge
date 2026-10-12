#!/usr/bin/env python3
"""
woge - turn extracted worksheets into a randomized exercise generator.

Workflow:
  1. Give Claude a worksheet PDF together with EXTRACTION_PROMPT.md
  2. Save the returned JSON as a bank file (e.g. dat/banks/analysis_ws3.json)
  3. Use this CLI to list, validate, preview and sample exercises

Commands (bank paths are optional; default is your bank folder, dat/banks in a checkout):
  woge list      BANKS...                     overview of all exercises
  woge validate  BANKS... [-k 30]             test-generate variants, report problems
  woge preview   BANKS... --id ID [-k 3]      print a few variants of one exercise
  woge sample    BANKS... -n 5 [options]      pick N exercises -> .md and/or .pdf

BANKS may be JSON files or directories containing JSON files.
Requires: sympy.  PDF output additionally requires pandoc + a LaTeX engine.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import sympy as sp
except ImportError:
    sys.exit("woge needs sympy:  pip install sympy")

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_]\w*)\s*(?::([^{}]*))?\}\}")
MAX_TRIES = 2000


# ----------------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------------
def load_banks(paths: list[str]) -> list[dict]:
    """Load every exercise from the given files/directories into one flat list."""
    files: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            files += sorted(p.glob("**/*.json"))
        elif p.exists():
            files.append(p)
        else:
            sys.exit(f"Not found: {p}")
    exercises = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            sys.exit(f"{f}: invalid JSON ({e}). Tip: LaTeX backslashes must be doubled in JSON (\\\\frac).")
        src = data.get("source", {})
        for ex in data.get("exercises", []):
            ex.setdefault("_source", src.get("title", f.stem))
            ex.setdefault("_file", str(f))
            exercises.append(ex)
    if not exercises:
        sys.exit("No exercises found.")
    return exercises


# ----------------------------------------------------------------------------
# Safe-ish evaluation namespace (expressions come from your own bank files;
# builtins are disabled, but this is not a security sandbox for untrusted input)
# ----------------------------------------------------------------------------
def num(v, digits: int = 4) -> str:
    """Decimal display without trailing zeros: num(2.50) -> '2.5'."""
    s = f"{float(v):.{digits}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def signed(v) -> str:
    """'+ 5' / '- 5' for appending a term, e.g. 'x {{signed(c)}}'."""
    v = sp.sympify(v)
    return f"- {sp.latex(-v)}" if v.could_extract_minus_sign() else f"+ {sp.latex(v)}"


def tex(v) -> str:
    return sp.latex(sp.sympify(v))


def _namespace() -> dict:
    names = ["Rational", "Integer", "Float", "sqrt", "root", "exp", "log", "ln", "sin",
             "cos", "tan", "asin", "acos", "atan", "sinh", "cosh", "pi", "E", "I", "oo",
             "simplify", "expand", "factor", "solve", "diff", "integrate", "limit",
             "latex", "nsimplify", "Abs", "Matrix", "gcd", "lcm", "binomial",
             "factorial", "symbols", "Symbol", "Eq", "N", "S", "Poly", "apart",
             "together", "cancel", "floor", "ceiling", "isprime", "summation",
             "series", "degree", "sign", "re", "im", "conjugate", "trigsimp",
             "Piecewise", "Sum", "Product", "Integral", "Derivative",
             "solveset", "Interval", "Union", "FiniteSet", "Complement", "Intersection",
             "EmptySet", "real_roots", "Mod", "Abs", "div", "divisors", "quo", "rem",
             "linsolve", "Poly", "roots", "nsolve", "sympify"]
    ns = {n: getattr(sp, n) for n in names if hasattr(sp, n)}
    ns.update({n: sp.Symbol(n) for n in ["x", "y", "z", "t", "u", "v", "w"]})
    ns.update(abs=abs, min=min, max=max, round=round, int=int, float=float, str=str,
              len=len, sum=sum, range=range, sorted=sorted, list=list, tuple=tuple,
              set=set, any=any, all=all, zip=zip, enumerate=enumerate, bool=bool,
              num=num, signed=signed, tex=tex, True_=True, False_=False)
    ns["__builtins__"] = {}
    return ns


def evaluate(expr, ns: dict):
    if not isinstance(expr, str):
        return expr
    return eval(expr, ns)  # noqa: S307  (namespace restricted, see above)


# ----------------------------------------------------------------------------
# Templates
# ----------------------------------------------------------------------------
def sample_param(spec: dict, rng: random.Random):
    kind = spec.get("type", "int")
    if kind == "int":
        exclude = set(spec.get("exclude", []))
        values = [v for v in range(spec["min"], spec["max"] + 1, spec.get("step", 1))
                  if v not in exclude]
        return sp.Integer(rng.choice(values))
    if kind == "float":
        return round(rng.uniform(spec["min"], spec["max"]), spec.get("decimals", 1))
    if kind == "choice":
        v = rng.choice(spec["values"])
        return sp.Integer(v) if isinstance(v, int) and not isinstance(v, bool) else v
    raise ValueError(f"unknown parameter type '{kind}'")


def draw_values(template: dict, rng: random.Random) -> dict:
    """Sample parameters, compute derived values, retry until constraints hold."""
    last_err = None
    for _ in range(MAX_TRIES):
        ns = _namespace()
        try:
            for name, spec in template.get("parameters", {}).items():
                ns[name] = sample_param(spec, rng)
            for name, expr in template.get("derived", {}).items():
                ns[name] = evaluate(expr, ns)
            if all(bool(evaluate(c, ns)) for c in template.get("constraints", [])):
                return ns
        except Exception as e:  # noqa: BLE001  e.g. division by zero for this draw -> redraw
            last_err = e  # a template bug shows up as 'no valid parameter set' with this error
    raise RuntimeError(f"no valid parameter set after {MAX_TRIES} tries"
                       + (f" (last error: {last_err})" if last_err else ""))


def fmt_value(v, spec: str | None) -> str:
    if spec:
        if isinstance(v, sp.Basic):
            v = int(v) if spec[-1] in "dxXob" else float(v)
        return format(v, spec)
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, sp.Basic):
        return sp.latex(v)
    if isinstance(v, float):
        return num(v)
    return str(v)


def fill(text: str | None, ns: dict) -> str:
    if not text:
        return ""

    def repl(m):
        name, spec = m.group(1), m.group(2)
        if name not in ns:
            raise KeyError(f"placeholder '{{{{{name}}}}}' has no parameter/derived value")
        return fmt_value(ns[name], spec)

    return PLACEHOLDER.sub(repl, text)


def make_instance(ex: dict, rng: random.Random, variant: bool) -> dict:
    tpl = ex.get("template")
    if variant and tpl:
        ns = draw_values(tpl, rng)
        body = {k: fill(tpl.get(k), ns) for k in ("statement", "solution", "explanation")}
        body["variant"] = True
    else:
        o = ex["original"]
        body = {k: o.get(k, "") for k in ("statement", "solution", "explanation")}
        body["variant"] = False
    body.update(id=ex["id"], title=ex.get("title", ex["id"]), type=ex.get("type", ""),
                topic=ex.get("topic", ""), difficulty=ex.get("difficulty"),
                minutes=ex.get("minutes"),
                source=ex.get("_source", ""))
    return body


# ----------------------------------------------------------------------------
# Selection
# ----------------------------------------------------------------------------
def parse_range(s: str) -> tuple[int, int]:
    lo, _, hi = s.partition("-")
    return int(lo), int(hi or lo)


def filter_pool(exs: list[dict], a) -> list[dict]:
    pool = exs
    if a.id:
        pool = [e for e in pool if e["id"] in a.id]
    if a.type:
        pool = [e for e in pool if any(t.lower() in e.get("type", "").lower() for t in a.type)]
    if a.topic:
        pool = [e for e in pool if any(t.lower() in e.get("topic", "").lower() for t in a.topic)]
    if a.tag:
        pool = [e for e in pool if set(a.tag) & set(e.get("tags", []))]
    if a.difficulty:
        lo, hi = parse_range(a.difficulty)
        pool = [e for e in pool if lo <= (e.get("difficulty") or 0) <= hi]
    if a.mode == "variants" and a.only_generatable:
        pool = [e for e in pool if e.get("template")]
    return pool


def pick_random(pool: list[dict], n: int, mode: str, rng: random.Random) -> list[dict]:
    """Plain random selection, ignoring difficulty."""
    chosen = rng.sample(pool, min(n, len(pool)))
    if n > len(pool):
        templated = [e for e in pool if e.get("template")] if mode != "originals" else []
        if not templated:
            print(f"warning: only {len(pool)} distinct exercises available", file=sys.stderr)
        else:
            chosen += [rng.choice(templated) for _ in range(n - len(pool))]
    return chosen


def pick_balanced(pool: list[dict], n: int, mode: str, rng: random.Random) -> list[dict]:
    """Spread the N picks as evenly as possible over the difficulty levels in the pool.

    Levels that run out of exercises hand their share to the nearest level; exercises
    with a template can fill a level repeatedly (with new numbers). Within a level,
    exercise types not yet on the sheet are preferred, so the sheet stays varied.
    """
    can_repeat = mode != "originals"
    by_level: dict[int, list[dict]] = {}
    for e in pool:
        by_level.setdefault(e.get("difficulty") or 0, []).append(e)
    levels = sorted(by_level)

    def cap(lvl):
        if can_repeat and any(e.get("template") for e in by_level[lvl]):
            return float("inf")
        return len(by_level[lvl])

    # even quota; the remainder goes to the middle levels first
    quota = {lvl: n // len(levels) for lvl in levels}
    centre = (levels[0] + levels[-1]) / 2
    for lvl in sorted(levels, key=lambda l: (abs(l - centre), rng.random()))[: n % len(levels)]:
        quota[lvl] += 1
    # move overflow to the nearest levels that still have room
    for lvl in levels:
        extra = quota[lvl] - cap(lvl)
        if extra <= 0:
            continue
        quota[lvl] = int(cap(lvl))
        for other in sorted(levels, key=lambda m: (abs(m - lvl), rng.random())):
            if other != lvl and quota[other] < cap(other):
                give = int(min(extra, cap(other) - quota[other]))
                quota[other] += give
                extra -= give
            if extra <= 0:
                break
        if extra > 0:
            print(f"warning: not enough exercises for {n} picks", file=sys.stderr)

    chosen, used_types = [], set()
    for lvl in rng.sample(levels, len(levels)):
        items = by_level[lvl][:]
        rng.shuffle(items)
        picked = []
        while len(picked) < quota[lvl] and items:
            items.sort(key=lambda e: e.get("type") in used_types)  # unused types first
            e = items.pop(0)
            picked.append(e)
            used_types.add(e.get("type"))
        templated = [e for e in by_level[lvl] if e.get("template")]
        while len(picked) < quota[lvl] and templated and can_repeat:
            picked.append(rng.choice(templated))
        chosen += picked
    return chosen


DEFAULT_MINUTES = 5  # assumed working time for exercises without a "minutes" rating


def ex_minutes(e: dict) -> int:
    return e.get("minutes") or DEFAULT_MINUTES


def pick_by_time(pool: list[dict], minutes: int, mode: str, rng: random.Random,
                 balance: str = "even", max_n: int | None = None, tries: int = 300) -> list[dict]:
    """Choose exercises whose estimated times add up to roughly `minutes`.

    Each try draws a (balanced) candidate list and fills the time budget greedily in
    random order; the try that lands closest to the target wins. A small overshoot is
    allowed (10 %, at least 2 min), undershoot is penalised a bit more than overshoot.
    """
    tol = max(2, round(minutes * 0.1))
    shortest = min(ex_minutes(e) for e in pool)
    n_cand = min(max_n or 10**6, minutes // shortest + 3)
    picker = pick_balanced if balance == "even" else pick_random
    best, best_score = [], float("inf")
    for _ in range(tries):
        cand = picker(pool, n_cand, mode, rng)
        rng.shuffle(cand)
        chosen, total, rest = [], 0, []
        for e in cand:  # fill up to the target without overshooting
            if max_n and len(chosen) >= max_n:
                break
            if total + ex_minutes(e) <= minutes:
                chosen.append(e)
                total += ex_minutes(e)
            else:
                rest.append(e)
        if total < minutes and (not max_n or len(chosen) < max_n):
            # still short: add the exercise that overshoots least (within the tolerance)
            fits = [e for e in rest if total + ex_minutes(e) <= minutes + tol]
            if fits:
                e = min(fits, key=ex_minutes)
                chosen.append(e)
                total += ex_minutes(e)
        diff = total - minutes
        score = diff if diff >= 0 else -1.5 * diff
        levels = len({e.get("difficulty") for e in chosen})
        score -= 0.01 * levels  # tie-break: prefer a wider difficulty spread
        if chosen and score < best_score:
            best, best_score = chosen, score
        if abs(diff) == 0 and levels >= min(3, len({e.get("difficulty") for e in pool})):
            break
    return best


def select(pool: list[dict], n: int | None, mode: str, rng: random.Random,
           balance: str = "even", order: str = "difficulty", minutes: int | None = None) -> list[dict]:
    if minutes:
        chosen_ex = pick_by_time(pool, minutes, mode, rng, balance, max_n=n)
        total = sum(ex_minutes(e) for e in chosen_ex)
        if abs(total - minutes) > max(2, round(minutes * 0.1)):
            print(f"warning: closest match is ~{total} min for a {minutes} min target", file=sys.stderr)
        unrated = sum(1 for e in chosen_ex if not e.get("minutes"))
        if unrated:
            print(f"note: {unrated} exercise(s) without time estimate counted as {DEFAULT_MINUTES} min",
                  file=sys.stderr)
    else:
        chosen_ex = (pick_balanced if balance == "even" else pick_random)(pool, n or 5, mode, rng)
    rng.shuffle(chosen_ex)
    if order == "difficulty":
        chosen_ex.sort(key=lambda e: e.get("difficulty") or 0)  # stable: random within a level

    out, seen = [], set()
    for ex in chosen_ex:
        for _ in range(25):  # avoid printing the exact same variant twice
            variant = {"variants": True, "originals": False}.get(mode, rng.random() < 0.5)
            inst = make_instance(ex, rng, variant)
            if inst["statement"] not in seen:
                break
        seen.add(inst["statement"])
        out.append(inst)
    return out


def sheet_stats(insts: list[dict]) -> dict:
    diffs = [i["difficulty"] for i in insts if i["difficulty"]]
    mins = [i["minutes"] for i in insts if i["minutes"]]
    dist = {d: diffs.count(d) for d in sorted(set(diffs))}
    return {"avg": sum(diffs) / len(diffs) if diffs else None, "dist": dist,
            "minutes": sum(mins) if mins else None}


def stats_line(st: dict, plain: bool = False) -> str:
    """plain=True avoids the ★ glyph, which many LaTeX fonts lack (PDF output)."""
    parts = []
    if st["dist"]:
        fmt = (lambda d, c: f"{c}× level {d}") if plain else (lambda d, c: f"★{d}×{c}")
        parts.append("difficulty " + ", ".join(fmt(d, c) for d, c in st["dist"].items())
                     + f" (avg {st['avg']:.1f})")
    if st["minutes"]:
        parts.append(f"~{st['minutes']} min")
    return " | ".join(parts)


# ----------------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------------
def quote(text: str) -> str:
    return "\n".join("> " + line if line.strip() else ">" for line in text.strip().splitlines())


def heading(i: int, inst: dict, plain: bool = False) -> str:
    star = (f"level {inst['difficulty']}/5" if plain else f"★{inst['difficulty']}") if inst["difficulty"] else ""
    meta = " · ".join(x for x in [inst["type"].replace("_", " "), star,
                                  f"~{inst['minutes']} min" if inst.get("minutes") else ""] if x)
    return f"## {i}. {inst['title']}" + (f"  \n*{meta}*" if meta else "")


def solution_block(i: int, inst: dict, labels: dict) -> str:
    parts = [f"### {i}. {inst['title']}", f"**{labels['sol']}:** {inst['solution'].strip()}"]
    if inst["explanation"].strip():
        parts.append(f"**{labels['exp']}:**\n\n{inst['explanation'].strip()}")
    return "\n\n".join(parts)


def render_markdown(insts: list[dict], title: str, seed: int, solutions: str,
                    labels: dict, for_pdf: bool = False) -> tuple[str, str | None]:
    """Returns (main_markdown, separate_solutions_markdown_or_None)."""
    topics = sorted({i["topic"] for i in insts if i["topic"]})
    tags = ", ".join(["exercises"] + [t.replace(" ", "-") for t in topics])
    st = sheet_stats(insts)
    extra = ""
    if st["avg"] is not None:
        extra += f"difficulty_avg: {st['avg']:.1f}\n"
    if st["minutes"]:
        extra += f"minutes: {st['minutes']}\n"
    front = (f"---\ntitle: \"{title}\"\ndate: {dt.date.today().isoformat()}\n"
             f"seed: {seed}\n" + extra + ("" if for_pdf else f"tags: [{tags}]\n") + "---\n\n")
    if not for_pdf:
        front += f"# {title}\n\n"
    summary = stats_line(st, plain=for_pdf)
    main_intro = f"*{summary}*\n\n" if summary else ""

    blocks = []
    for n, inst in enumerate(insts, 1):
        b = f"{heading(n, inst, for_pdf)}\n\n{inst['statement'].strip()}"
        if solutions == "callout":
            b += f"\n\n> [!success]- {labels['sol']}\n{quote(inst['solution'])}"
            if inst["explanation"].strip():
                b += f"\n\n> [!info]- {labels['exp']}\n{quote(inst['explanation'])}"
        blocks.append(b)
    main = front + main_intro + "\n\n".join(blocks) + "\n"

    sol_md = "\n\n".join(solution_block(n, i, labels) for n, i in enumerate(insts, 1))
    if solutions == "end":
        main += ("\n\\newpage\n" if for_pdf else "\n---\n") + f"\n# {labels['sols']}\n\n{sol_md}\n"
        return main, None
    if solutions == "separate":
        return main, front.replace(title, f"{title} – {labels['sols']}", 1) + sol_md + "\n"
    return main, None


def to_pdf(md_text: str, out: Path, engine: str | None):
    if not shutil.which("pandoc"):
        sys.exit("PDF output needs pandoc (https://pandoc.org) plus a LaTeX engine "
                 "(e.g. TeX Live / MiKTeX / tectonic). The .md output works without it.")
    engines = [e for e in ([engine] if engine else
                           ["xelatex", "lualatex", "tectonic", "pdflatex"]) if shutil.which(e)]
    if not engines:
        sys.exit("No LaTeX engine found (install TeX Live, MiKTeX or tectonic).")
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as tmp:
        tmp.write(md_text)
    base = ["pandoc", tmp.name, "-o", str(out), "-V", "geometry:margin=2.2cm", "-V", "fontsize=11pt"]
    errors = []
    try:
        # try each engine; if the default font (Latin Modern) is missing, fall back to Times
        for eng in engines:
            for extra in ([], ["-V", "fontfamily=times"]):
                hint = " (first run downloads LaTeX packages – can take a few minutes)" \
                    if eng == "tectonic" else ""
                print(f"building {out.name} with {eng}{hint} …", file=sys.stderr, flush=True)
                res = subprocess.run(base + [f"--pdf-engine={eng}"] + extra,
                                     capture_output=True, text=True)
                if res.returncode == 0:
                    return
                errors.append(f"[{eng}{' +times' if extra else ''}] {res.stderr.strip()[:300]}")
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    sys.exit("pandoc could not build the PDF:\n" + "\n".join(errors))


LABELS = {
    "en": {"sol": "Solution", "exp": "Explanation", "sols": "Solutions"},
    "de": {"sol": "Lösung", "exp": "Erklärung", "sols": "Lösungen"},
}


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------
def cmd_list(a):
    exs = load_banks(a.banks)
    w = max(len(e["id"]) for e in exs)
    print(f"{'ID':<{w}}  GEN  ★  MIN  {'TYPE':<24} TITLE")
    for e in exs:
        gen = "yes" if e.get("template") else " - "
        print(f"{e['id']:<{w}}  {gen}  {e.get('difficulty') or '-'}  {e.get('minutes') or '-':>3}  "
              f"{e.get('type', '')[:24]:<24} {e.get('title', '')}")
    types = {}
    for e in exs:
        types[e.get("type", "?")] = types.get(e.get("type", "?"), 0) + 1
    dist = {}
    for e in exs:
        dist[e.get("difficulty") or 0] = dist.get(e.get("difficulty") or 0, 0) + 1
    print(f"\n{len(exs)} exercises, {sum(1 for e in exs if e.get('template'))} generatable, "
          f"{len(types)} types")
    print("difficulty: " + "  ".join(f"★{d}×{c}" if d else f"unrated×{c}" for d, c in sorted(dist.items())))


def cmd_validate(a):
    exs = load_banks(a.banks)
    rng = random.Random(a.seed)
    problems = 0
    ids = [e.get("id") for e in exs]
    for dup in {i for i in ids if ids.count(i) > 1}:
        print(f"✗ duplicate id: {dup}")
        problems += 1
    for e in exs:
        eid = e.get("id", "<no id>")
        for field in ("id", "original"):
            if field not in e:
                print(f"✗ {eid}: missing '{field}'")
                problems += 1
        if e.get("difficulty") not in (1, 2, 3, 4, 5):
            print(f"! {eid}: difficulty should be an integer 1–5 (is {e.get('difficulty')!r})")
        if not isinstance(e.get("minutes"), int):
            print(f"! {eid}: no 'minutes' estimate")
        if "original" in e and not e["original"].get("solution"):
            print(f"✗ {eid}: original has no solution")
            problems += 1
        tpl = e.get("template")
        if not tpl:
            continue
        statements = set()
        try:
            for _ in range(a.k):
                inst = make_instance(e, rng, True)
                for k in ("statement", "solution", "explanation"):
                    if re.search(r"\{\{[^{}]*\}\}", inst[k]):
                        raise ValueError(f"unfilled placeholder in {k}: {inst[k][:80]}")
                statements.add(inst["statement"])
            status = "✓"
            note = f"{len(statements)}/{a.k} distinct variants"
            if len(statements) < max(2, a.k // 5):
                status, note = "!", note + " (low variety – widen parameter ranges?)"
            print(f"{status} {eid}: {note}")
        except Exception as ex:  # noqa: BLE001
            print(f"✗ {eid}: {type(ex).__name__}: {ex}")
            problems += 1
    print(f"\n{problems} problem(s)")
    sys.exit(1 if problems else 0)


def cmd_preview(a):
    exs = [e for e in load_banks(a.banks) if e["id"] == a.id]
    if not exs:
        sys.exit(f"No exercise with id '{a.id}'")
    ex, rng = exs[0], random.Random(a.seed)
    for k in range(a.k):
        if k > 0 and not ex.get("template"):
            print("(no template – this exercise cannot be varied)")
            break
        inst = make_instance(ex, rng, variant=k > 0)  # first = original, rest = variants
        tag = "variant" if inst["variant"] else "original"
        print(f"--- {tag} ---\n{inst['statement']}\n  → {inst['solution']}\n")


def cmd_sample(a):
    seed = a.seed if a.seed is not None else random.randrange(10**6)
    rng = random.Random(seed)
    pool = filter_pool(load_banks(a.banks), a)
    if not pool:
        sys.exit("No exercises match the filters.")
    insts = select(pool, a.n, a.mode, rng, a.balance, a.order, a.minutes)
    labels = LABELS[a.lang]

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = Path(a.outdir or os.environ.get("WOGE_OUTDIR") or ".").expanduser()
    name = Path(a.output).expanduser() if a.output else Path(f"exercises_{stamp}")
    base = name if name.is_absolute() else outdir / name
    base = base.with_suffix("") if base.suffix in (".md", ".pdf") else base
    base.parent.mkdir(parents=True, exist_ok=True)
    written = []

    if a.format in ("md", "both"):
        main, sep = render_markdown(insts, a.title, seed, a.solutions, labels)
        base.with_suffix(".md").write_text(main, encoding="utf-8")
        written.append(base.with_suffix(".md"))
        if sep:
            p = base.parent / f"{base.name}_solutions.md"
            p.write_text(sep, encoding="utf-8")
            written.append(p)

    if a.format in ("pdf", "both"):
        # Obsidian callouts don't exist in PDF -> put solutions at the end instead
        mode = "end" if a.solutions == "callout" else a.solutions
        main, sep = render_markdown(insts, a.title, seed, mode, labels, for_pdf=True)
        to_pdf(main, base.with_suffix(".pdf"), a.pdf_engine)
        written.append(base.with_suffix(".pdf"))
        if sep:
            p = base.parent / f"{base.name}_solutions.pdf"
            to_pdf(sep, p, a.pdf_engine)
            written.append(p)

    for i, inst in enumerate(insts, 1):
        tag = "variant " if inst["variant"] else "original"
        star = f"★{inst['difficulty']}" if inst["difficulty"] else "  "
        print(f"{i:>3}. {star} [{tag}] {inst['id']}  –  {inst['title']}")
    summary = stats_line(sheet_stats(insts))
    if summary:
        print("\n" + summary)
    print(f"\nseed {seed}  (reuse with --seed {seed})")
    for p in written:
        print(f"wrote {p}")


def main():
    ap = argparse.ArgumentParser(prog="woge", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="overview of all exercises")
    p.add_argument("banks", nargs="*", help="bank files/folders (default: your bank folder)")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("validate", help="generate test variants and report problems")
    p.add_argument("banks", nargs="*", help="bank files/folders (default: your bank folder)")
    p.add_argument("-k", type=int, default=30, help="variants per exercise (default 30)")
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("preview", help="show original + variants of one exercise")
    p.add_argument("banks", nargs="*", help="bank files/folders (default: your bank folder)")
    p.add_argument("--id", required=True)
    p.add_argument("-k", type=int, default=4)
    p.add_argument("--seed", type=int, default=None)
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("sample", help="pick N exercises and write md/pdf")
    p.add_argument("banks", nargs="*", help="bank files/folders (default: your bank folder)")
    p.add_argument("-n", type=int, default=None,
                   help="number of exercises (default 5); with --minutes it is an upper limit")
    p.add_argument("-t", "--minutes", type=int, default=None,
                   help="time budget: pick exercises whose estimated times add up to about this many minutes")
    p.add_argument("-f", "--format", choices=["md", "pdf", "both"], default="md")
    p.add_argument("-o", "--output", help="file name without extension (default: exercises_<timestamp>); "
                                          "relative names are placed inside the output folder")
    p.add_argument("-d", "--outdir", help="output folder, created if missing "
                                          "(default: $WOGE_OUTDIR, else current folder)")
    p.add_argument("--mode", choices=["variants", "originals", "mix"], default="variants",
                   help="variants: new numbers where possible (default); originals: worksheet as-is; mix: 50/50")
    p.add_argument("--only-generatable", action="store_true",
                   help="with --mode variants: skip exercises that have no template")
    p.add_argument("--solutions", choices=["callout", "end", "separate", "none"], default="callout",
                   help="callout: foldable Obsidian callouts (md); end: solutions section; "
                        "separate: extra file; none")
    p.add_argument("--type", action="append", help="filter by type (substring, repeatable)")
    p.add_argument("--topic", action="append", help="filter by topic (substring, repeatable)")
    p.add_argument("--tag", action="append", help="filter by tag (repeatable)")
    p.add_argument("--id", action="append", help="restrict to specific ids (repeatable)")
    p.add_argument("--difficulty", help="only these difficulty levels, e.g. 2 or 1-3")
    p.add_argument("--balance", choices=["even", "off"], default="even",
                   help="even: spread picks evenly over the difficulty levels (default); off: purely random")
    p.add_argument("--order", choices=["difficulty", "random"], default="difficulty",
                   help="order on the sheet: easy → hard (default) or random")
    p.add_argument("--title", default="Exercises")
    p.add_argument("--lang", choices=list(LABELS), default="en", help="language of labels")
    p.add_argument("--seed", type=int, help="make the selection reproducible")
    p.add_argument("--pdf-engine", help="force a LaTeX engine (xelatex, lualatex, tectonic, pdflatex)")
    p.set_defaults(func=cmd_sample)

    a = ap.parse_args()
    if hasattr(a, "banks") and not a.banks:
        from .paths import BANK_DIR, ensure_user_dirs
        ensure_user_dirs()
        a.banks = [str(BANK_DIR)]
    a.func(a)


if __name__ == "__main__":
    main()
