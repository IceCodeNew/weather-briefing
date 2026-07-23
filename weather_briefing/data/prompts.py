"""Access to packaged LLM prompt resources."""

from importlib import resources

SYSTEM_PROMPT = resources.files("weather_briefing.data").joinpath("system_prompt.txt").read_text(encoding="utf-8")
