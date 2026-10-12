# woge – project notes

Worksheet PDF → exercise bank (JSON) → random sheets with fresh numbers (CLI) + Anki-style Qt study app.

## Layout (intentional)
- `src/` – the code. Installed as Python package **`woge`** (`pyproject.toml`: `package-dir = {"woge" = "src"}`), so only relative imports inside it. Run from a checkout with `python -m src …` / `python -m src.gui`.
- `dat/` – user data in a checkout (`banks/`, `worksheets/`, `tracker.sqlite`). Installed copies use `~/.local/share/woge`; `WOGE_HOME` overrides. Logic in `src/paths.py` (`IN_CHECKOUT` = `pyproject.toml` and `dat/` exist next to `src/`).
- `EXTRACTION_PROMPT.md` – prompt used in a Claude chat to turn a worksheet PDF into a bank JSON.
- `packaging/` – `PKGBUILD`, `.desktop`, icon. Build: `cd packaging && makepkg -si`. The PKGBUILD must stay in `packaging/` because makepkg uses `./src` as its srcdir, which would clash with the code folder.

## Gotchas
- Name is "woge" everywhere (formerly "exbank"); env vars `WOGE_HOME`, `WOGE_OUTDIR`; Qt settings `woge/woge`.
- System python has no sympy/PySide6 venv set up; test in a venv (`python -m venv --system-site-packages` + `pip install sympy`).
- GUI: a stale saved `bank_dir` in `~/.config/woge/woge.conf` falls back to `BANK_DIR` (otherwise a modal warning blocks startup, also headless).
- Templates are evaluated with restricted `eval`; don't load untrusted banks.
- No commits exist yet; makepkg output (`packaging/src`, `pkg`, `*.pkg.tar.*`) is gitignored.
