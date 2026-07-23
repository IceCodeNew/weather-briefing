"""Names for application-owned provider and publisher adapters."""

from enum import StrEnum


class WeatherProviderName(StrEnum):
    """Identify application-owned weather provider adapters."""

    QWEATHER = "qweather"
    OPEN_METEO = "open-meteo"
    NEA_SINGAPORE = "nea-sg"
    JMA_JAPAN = "jma-jp"


LOCAL_WEATHER_CAPABILITY_PROVIDERS = frozenset(
    {
        WeatherProviderName.NEA_SINGAPORE,
        WeatherProviderName.JMA_JAPAN,
    }
)


class PublisherName(StrEnum):
    """Identify application-owned delivery provider adapters."""

    BARK = "bark"
    STDOUT = "stdout"
    TELEGRAM = "telegram"
