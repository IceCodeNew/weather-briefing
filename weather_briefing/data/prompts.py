"""Access to packaged LLM prompt resources."""

from importlib import resources


def _load_system_prompt() -> str:
    """Load the packaged system prompt with an actionable failure."""
    filename = "system_prompt.txt"
    try:
        return resources.files("weather_briefing.data").joinpath(filename).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Unable to load system prompt: {filename}") from exc


SYSTEM_PROMPT = _load_system_prompt()
