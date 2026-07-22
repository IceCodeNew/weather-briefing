"""Default endpoints and identifiers for external data services."""

from weather_briefing import __version__

AQICN_BASE_URL = "https://api.waqi.info"
JMA_FORECAST_BASE_URL = "https://www.jma.go.jp/bosai/forecast/data/forecast"
NEA_BASE_URL = "https://api-open.data.gov.sg"
NOMINATIM_BASE_URL = "https://nominatim.openstreetmap.org"
NOMINATIM_USER_AGENT = f"weather-briefing/{__version__} (+https://github.com/IceCodeNew/weather-briefing)"
OPEN_METEO_AIR_QUALITY_BASE_URL = "https://air-quality-api.open-meteo.com"
OPEN_METEO_GEOCODING_BASE_URL = "https://geocoding-api.open-meteo.com"
OPEN_METEO_WEATHER_BASE_URL = "https://api.open-meteo.com"
