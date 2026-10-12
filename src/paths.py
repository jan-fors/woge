"""Where woge keeps its files.

Run from a checkout (``python -m src``) everything lives in the repository: code in src/, data in dat/.
Installed system-wide (pacman/pip), data goes to the user's XDG data folder (~/.local/share/woge) and
the default banks shipped in /usr/share/woge/banks are copied there on first run.
Set WOGE_HOME to use any other data folder.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
ROOT_DIR = PKG_DIR.parent
IN_CHECKOUT = (ROOT_DIR / "pyproject.toml").exists() and (ROOT_DIR / "dat").is_dir()

SHARE_DIRS = [Path(sys.prefix) / "share" / "woge", Path("/usr/share/woge")]


def _xdg_data_home() -> Path:
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))


if os.environ.get("WOGE_HOME"):
    DATA_DIR = Path(os.environ["WOGE_HOME"])
elif IN_CHECKOUT:
    DATA_DIR = ROOT_DIR / "dat"
else:
    DATA_DIR = _xdg_data_home() / "woge"
BANK_DIR = DATA_DIR / "banks"


def _share_file(*parts: str) -> Path | None:
    for base in [ROOT_DIR, *SHARE_DIRS]:
        p = base.joinpath(*parts)
        if p.exists():
            return p
    return None


def prompt_file() -> Path | None:
    return _share_file("EXTRACTION_PROMPT.md")


def ensure_user_dirs() -> None:
    """Create the data folders; on first run copy the bundled banks into an empty bank folder."""
    first_run = not BANK_DIR.exists()
    BANK_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "worksheets").mkdir(parents=True, exist_ok=True)
    bundled = _share_file("banks") if not IN_CHECKOUT else None
    if first_run and bundled and bundled != BANK_DIR:
        shutil.copytree(bundled, BANK_DIR, dirs_exist_ok=True)
