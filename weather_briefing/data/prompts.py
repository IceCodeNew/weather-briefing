"""Access to packaged LLM prompt resources."""

from importlib import resources


def _load_prompt(filename: str) -> str:
    """Load one packaged prompt with an actionable failure."""
    try:
        return resources.files("weather_briefing.data").joinpath(filename).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Unable to load prompt: {filename}") from exc


def _load_system_prompt() -> str:
    """Load the weather content-generation prompt."""
    return _load_prompt("system_prompt.txt")


SYSTEM_PROMPT = _load_system_prompt()
