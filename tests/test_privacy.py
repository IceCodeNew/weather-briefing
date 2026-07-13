from __future__ import annotations

from pathlib import Path

REQUIRED_IGNORE_PATTERNS = {
    ".env",
    "*.sqlite3",
    "/locations.json",
    "/rss-sources.json",
}


def test_private_runtime_files_are_ignored() -> None:
    root = Path(__file__).parents[1]
    patterns = {
        line.strip()
        for line in (root / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert patterns >= REQUIRED_IGNORE_PATTERNS
