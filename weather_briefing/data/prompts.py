"""Access to packaged LLM prompt resources."""

from importlib import resources


def _load_prompt(filename: str) -> str:
    """Load one packaged prompt with an actionable failure."""
    try:
        return resources.files("weather_briefing.data").joinpath(filename).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Unable to load prompt: {filename}") from exc


def _load_system_prompt() -> str:
    """Load the weather briefing prompt plus the shared notification policy."""
    return (
        _load_prompt("system_prompt.txt")
        + "\n"
        + _load_prompt("notification_policy.txt")
        + "\n本次 weather 任务把通知判断写入 should_publish。forecast 模式必须设为 true。"
    )


NOTIFICATION_POLICY = _load_prompt("notification_policy.txt")
SYSTEM_PROMPT = _load_system_prompt()
