"""Names for application-owned provider and publisher adapters."""

from enum import StrEnum


class WeatherProviderName(StrEnum):
    """Identify application-owned weather provider adapters."""

    QWEATHER = "qweather"
    OPEN_METEO = "open-meteo"
    NEA_SINGAPORE = "nea-sg"


class PublisherName(StrEnum):
    """Identify application-owned delivery provider adapters."""

    STDOUT = "stdout"
    TELEGRAM = "telegram"
