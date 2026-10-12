# woge

Worksheet PDF → exercise bank (JSON) → random exercise sheets with fresh numbers, as Obsidian Markdown or PDF.

## Install

**Arch Linux (makepkg):**

```bash
cd packaging && makepkg -si
```

This installs the commands `woge` (CLI) and `woge-gui` (study app, also in your application menu). Your data then lives in `~/.local/share/woge/` (`banks/`, `worksheets/`, `tracker.sqlite`); the example banks are copied there on first run. `WOGE_HOME` overrides the folder.

**Elsewhere:** `pip install .` (add `.[gui]` for the study app).

**From a checkout (no install):** `pip install sympy PySide6 matplotlib`, then use `python -m src …` and `python -m src.gui`. Data stays in `dat/`.

Optional for PDF output: pandoc + a LaTeX engine (TeX Live, MiKTeX or tectonic).

## Workflow

1. Open a Claude chat, paste `EXTRACTION_PROMPT.md`, attach the worksheet PDF.
2. Save the returned JSON as `banks/<name>.json` in your data folder (`dat/banks/` in a checkout), or use File → Import in the app.
3. Check it: `woge validate`
4. Generate sheets:

```bash
woge list                                  # what's in the bank
woge preview --id ex-1a                    # original + 3 variants
woge sample -n 5                           # 5 random exercises → .md
woge sample -t 30 -f pdf                   # a worksheet for about 30 minutes
woge sample -n 8 -f pdf --solutions end    # PDF, solutions on last pages
woge sample -n 10 --topic algebra --difficulty 2-3 -f both -d ~/Vault/Practice -o algebra
woge sample -n 6 --mode originals          # worksheet exercises unchanged
woge sample -n 5 --seed 1234               # reproduce an earlier sheet
```

## Options for `sample`

| Option | Meaning |
|---|---|
| `-n` | number of exercises; if larger than the pool, templated exercises repeat with new numbers |
| `-t MIN` / `--minutes MIN` | time budget: picks exercises whose estimated times add up to about MIN minutes (within 10 %, usually exact); balancing and filters still apply, `-n` then acts as a maximum count |
| `-f md\|pdf\|both` | output format |
| `-d DIR` | output folder, created if missing (e.g. a folder in your Obsidian vault). Default: `$WOGE_OUTDIR` if set, else the current folder |
| `-o NAME` | file name without extension (default `exercises_<timestamp>`); an absolute path ignores `-d` |
| `--mode variants\|originals\|mix` | new numbers / as on the sheet / 50:50 |
| `--solutions callout\|end\|separate\|none` | foldable Obsidian callouts (solution first, then explanation) / section at the end / extra file / none. PDFs use `end` instead of `callout`. |
| `--balance even\|off` | even (default): spread the picks evenly over the difficulty levels, preferring different exercise types; off: purely random |
| `--order difficulty\|random` | order on the sheet: easy → hard (default) or shuffled |
| `--type`, `--topic`, `--tag`, `--id`, `--difficulty` | filters (repeatable, substring match); `--difficulty 2-4` limits the levels, balancing then happens within them |
| `--lang en\|de` | labels ("Solution"/"Lösung", …) |
| `--seed` | reproducible selection; printed after every run |

Tip: add an alias, e.g. `alias ex='woge sample'` → `ex -n 5 -f pdf`.

## Study app (desktop, Anki-style)

```bash
pip install PySide6 sympy matplotlib
woge-gui        # from a checkout: python -m src.gui
```

Runs locally as a normal desktop window (Qt via PySide6, the same toolkit Anki uses). It reads all bank files under `dat/banks/` (one deck per JSON file, folders become deck groups) and stores its data in `dat/`: `tracker.sqlite` (answer log + schedule) and `dat/worksheets/` (one JSON per worksheet, plus exported .md/.pdf).

- **Decks**: total / new / due counts per deck. Tick decks, choose a time budget (or a number of exercises), difficulty range and variants/originals, then *Create worksheet and start*. *Review due exercises now* collects everything that is due.
- **Study**: card view. Space reveals the solution, then the explanation; rate with 1 Again, 2 Hard, 3 Good, 4 Easy (S skips). *Again* brings the card back at the end of the session.
- **Worksheets**: every worksheet is saved automatically with your ratings. Continue, restart, export to Markdown (Obsidian callouts possible) or PDF, open the folder.
- **Browse**: search and filter all exercises (new, due, seen, last answered Again/Hard), view one separately with or without solution, generate new variants, see its history, practise just that one, reset its progress.

How the tracker varies the exercises: unseen exercises get the highest priority, due ones next (more if overdue or often failed), exercises seen in the last 20 hours are almost never picked again. Ratings set the next due date (like Anki: Good roughly doubles the interval, Again resets it). On top of that, difficulty levels and exercise types are interleaved, and templated exercises come with new numbers.

Formulas are rendered with matplotlib (no LaTeX installation needed); PDF export still needs pandoc + LaTeX.

## Ratings

Every exercise has `difficulty` (1 = direct application … 5 = open-ended / trap) and `minutes` (estimated working time). `list` shows both, `validate` warns if they're missing, and every sheet shows its difficulty mix, average and total time at the top and in the terminal. Edit the numbers in the bank JSON if a rating doesn't match your experience.

## Notes

Template expressions are evaluated with Python `eval` in a restricted namespace (no builtins). That's fine for you generated yourself, but don't run bank files from untrusted sources.
