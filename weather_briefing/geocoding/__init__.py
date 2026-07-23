"""Geocoding contracts, adapters, composition, and cached resolution."""

from .base import GeocodingError, GeocodingProvider, ReverseGeocodingProvider
from .composition import FallbackGeocodingProvider, PrecisionReducingGeocodingProvider
from .matching import possibly_mainland_china
from .nominatim import NominatimGeocodingProvider
from .open_meteo import OpenMeteoGeocodingProvider
from .resolver import CachedLocationResolver

__all__ = [
    "CachedLocationResolver",
    "FallbackGeocodingProvider",
    "GeocodingError",
    "GeocodingProvider",
    "NominatimGeocodingProvider",
    "OpenMeteoGeocodingProvider",
    "PrecisionReducingGeocodingProvider",
    "ReverseGeocodingProvider",
    "possibly_mainland_china",
]
