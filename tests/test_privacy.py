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


def test_visible_environment_example_is_tracked_and_not_ignored() -> None:
    root = Path(__file__).parents[1]

    assert (root / "env.example").is_file()
    assert (root / "locations.example.json").is_file()
    assert not (root / ".env.example").exists()


def test_environment_example_does_not_enable_service_status_monitoring() -> None:
    root = Path(__file__).parents[1]
    lines = (root / "env.example").read_text(encoding="utf-8").splitlines()

    provider_lines = [line for line in lines if line.startswith("SERVICE_STATUS_PROVIDERS=")]
    assert provider_lines == ["SERVICE_STATUS_PROVIDERS="]
