"""Repo hygiene: no em dashes anywhere in tracked text files."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWED = {"LICENSE", "uv.lock"}


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    return [REPO_ROOT / line for line in out.stdout.splitlines() if line]


def test_no_emdash_anywhere() -> None:
    offenders: list[str] = []
    for path in _tracked_files():
        if path.name in ALLOWED or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if "\u2014" in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"em dash found in: {offenders}"
