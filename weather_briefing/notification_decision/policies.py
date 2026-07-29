"""Packaged prompts for application-supported notification types."""

from importlib import resources

WEATHER_NOTIFICATION_KIND = "weather"
SERVICE_STATUS_NOTIFICATION_KIND = "service_status"
_DEFAULT_PACKAGE = "weather_briefing.notification_decision"


def _notification_prompt_package() -> str:
    """Return a stable package anchor even during direct module execution."""
    return __package__ or _DEFAULT_PACKAGE


def _load_notification_prompt(filename: str) -> str:
    """Load one notification policy with an actionable failure."""
    try:
        return resources.files(_notification_prompt_package()).joinpath(filename).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Unable to load notification policy: {filename}") from exc


WEATHER_NOTIFICATION_PROMPT = _load_notification_prompt("weather.txt")
SERVICE_STATUS_NOTIFICATION_PROMPT = _load_notification_prompt("service_status.txt")
