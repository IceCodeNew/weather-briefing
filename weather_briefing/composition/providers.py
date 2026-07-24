"""Runtime composition of LLM, delivery, and weather providers."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

import httpx

from ..air_quality import AirQualityProvider, AQICNProvider
from ..capabilities import CapabilityName, CapabilityProviderSet, ProviderCapabilities
from ..config import Settings
from ..config import environment as config_environment
from ..delivery import (
    BarkPublisher,
    BarkTextRenderer,
    DeliveryProvider,
    PlainTextRenderer,
    RenderedTextDiagnostics,
    StdoutPublisher,
    TelegramHTMLRenderer,
    TelegramPublisher,
)
from ..llm import CompleteLLMProvider, FallbackLLMProvider, SensitiveLLMDiagnostics, any_llm
from ..models import ResolvedLocation
from ..registries import LOCAL_WEATHER_CAPABILITY_PROVIDERS, PublisherName, WeatherProviderName
from ..weather import (
    JMA_LANGUAGE_SUPPORT,
    NEA_LANGUAGE_SUPPORT,
    OPEN_METEO_LANGUAGE_SUPPORT,
    QWEATHER_LANGUAGE_SUPPORT,
    FallbackWeatherContextProvider,
    JMAJapanForecastProvider,
    LoggedWeatherContextProvider,
    NEASingaporeNowcastProvider,
    OpenMeteoProvider,
    QWeatherJWTAuthenticator,
    QWeatherProvider,
    WeatherContextProvider,
)

_LOGGER = logging.getLogger("weather_briefing")


def llm_provider(
    settings: Settings,
    diagnostics: SensitiveLLMDiagnostics | None = None,
) -> CompleteLLMProvider:
    """Build the configured primary and optional fallback LLM adapters."""
    primary = any_llm.create_any_llm_provider(
        settings.llm_provider,
        settings.llm_model,
        settings.llm_max_output_tokens,
        api_key=settings.api_key,
        api_base=settings.llm_base_url,
        extra_headers=settings.llm_extra_headers,
        diagnostics=diagnostics,
    )
    if settings.llm_fallback_provider is None or settings.llm_fallback_model is None:
        return primary
    fallback = any_llm.create_any_llm_provider(
        settings.llm_fallback_provider,
        settings.llm_fallback_model,
        settings.llm_max_output_tokens,
        api_key=settings.llm_fallback_api_key,
        api_base=settings.llm_fallback_base_url,
        extra_headers=settings.llm_fallback_extra_headers,
        diagnostics=diagnostics,
    )
    return FallbackLLMProvider(
        primary,
        fallback,
        primary_name=settings.llm_provider,
        fallback_name=settings.llm_fallback_provider,
    )


def delivery_provider(
    settings: Settings,
    client: httpx.AsyncClient,
    diagnostics: RenderedTextDiagnostics | None = None,
    *,
    publisher: str | None = None,
) -> DeliveryProvider:
    """Build the configured publisher and renderer pair."""
    selected = publisher or settings.publisher
    builder = PUBLISHER_BUILDERS.get(selected)
    if builder is None:
        raise ValueError(f"Unsupported publisher: {selected}")
    return builder(settings, client, diagnostics)


def delivery_providers(
    settings: Settings,
    client: httpx.AsyncClient,
    publishers: tuple[str, ...],
    diagnostics: RenderedTextDiagnostics | None = None,
) -> tuple[DeliveryProvider, ...]:
    """Build an ordered group of delivery targets."""
    if not publishers:
        raise ValueError("At least one publisher is required")
    return tuple(delivery_provider(settings, client, diagnostics, publisher=publisher) for publisher in publishers)


def _build_stdout_publisher(
    settings: Settings,
    client: httpx.AsyncClient,
    diagnostics: RenderedTextDiagnostics | None,
) -> DeliveryProvider:
    return DeliveryProvider(PlainTextRenderer(), StdoutPublisher(), diagnostics=diagnostics)


def _build_telegram_publisher(
    settings: Settings,
    client: httpx.AsyncClient,
    diagnostics: RenderedTextDiagnostics | None,
) -> DeliveryProvider:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise ValueError("Telegram publisher requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    return DeliveryProvider(
        TelegramHTMLRenderer(),
        TelegramPublisher(client, settings.telegram_bot_token, settings.telegram_chat_id, diagnostics),
        single_message_limit=TelegramPublisher.MAX_MESSAGE_LENGTH,
        diagnostics=diagnostics,
    )


def _build_bark_publisher(
    settings: Settings,
    client: httpx.AsyncClient,
    diagnostics: RenderedTextDiagnostics | None,
) -> DeliveryProvider:
    if not settings.bark_device_key:
        raise ValueError("Bark publisher requires BARK_DEVICE_KEY")
    return DeliveryProvider(
        BarkTextRenderer(),
        BarkPublisher(
            client,
            settings.bark_device_key,
            settings.bark_encryption_key,
            settings.bark_encryption_iv,
            diagnostics,
            base_url=settings.bark_base_url,
            group=settings.bark_group,
        ),
        single_message_limit=BarkPublisher.MAX_MESSAGE_LENGTH,
        briefing_max_messages=2,
        diagnostics=diagnostics,
    )


PUBLISHER_BUILDERS: dict[
    str,
    Callable[[Settings, httpx.AsyncClient, RenderedTextDiagnostics | None], DeliveryProvider],
] = {
    PublisherName.BARK: _build_bark_publisher,
    PublisherName.STDOUT: _build_stdout_publisher,
    PublisherName.TELEGRAM: _build_telegram_publisher,
}

_WEATHER_PROVIDER_METADATA: dict[str, ProviderCapabilities] = {
    WeatherProviderName.QWEATHER: ProviderCapabilities(
        provider_id=WeatherProviderName.QWEATHER,
        provider_name="QWeather",
        capabilities=frozenset({CapabilityName.WEATHER, CapabilityName.AIR_QUALITY, CapabilityName.LIFESTYLE}),
        language_support=QWEATHER_LANGUAGE_SUPPORT,
    ),
    WeatherProviderName.OPEN_METEO: ProviderCapabilities(
        provider_id=WeatherProviderName.OPEN_METEO,
        provider_name="Open-Meteo",
        capabilities=frozenset({CapabilityName.WEATHER, CapabilityName.AIR_QUALITY, CapabilityName.ALLERGEN}),
        language_support=OPEN_METEO_LANGUAGE_SUPPORT,
    ),
    WeatherProviderName.NEA_SINGAPORE: ProviderCapabilities(
        provider_id=WeatherProviderName.NEA_SINGAPORE,
        provider_name="Singapore NEA",
        capabilities=frozenset({CapabilityName.NOWCAST}),
        language_support=NEA_LANGUAGE_SUPPORT,
    ),
    WeatherProviderName.JMA_JAPAN: ProviderCapabilities(
        provider_id=WeatherProviderName.JMA_JAPAN,
        provider_name="Japan JMA",
        capabilities=frozenset({CapabilityName.WEATHER}),
        language_support=JMA_LANGUAGE_SUPPORT,
    ),
}


def weather_provider_metadata(names: Sequence[str]) -> ProviderCapabilities:
    """Describe capabilities common to every active fallback provider."""
    metadata: list[ProviderCapabilities] = []
    for name in names:
        item = _WEATHER_PROVIDER_METADATA.get(name)
        if item is None:
            raise ValueError(f"Weather provider {name!r} has no capability metadata")
        metadata.append(item)
    if len(metadata) == 1:
        return metadata[0]
    capabilities = metadata[0].capabilities
    for item in metadata[1:]:
        capabilities &= item.capabilities
    return ProviderCapabilities(
        provider_id="weather-composite",
        provider_name="Weather provider composite",
        capabilities=capabilities,
    )


def weather_context_provider(
    settings: Settings,
    client: httpx.AsyncClient,
    location: ResolvedLocation,
) -> CapabilityProviderSet:
    """Compose complete and supplemental weather providers for one location."""
    names = config_environment.weather_providers_for(location, settings.weather_providers)
    if (
        settings.weather_providers is not None
        and WeatherProviderName.NEA_SINGAPORE in settings.weather_providers
        and WeatherProviderName.NEA_SINGAPORE not in names
    ):
        reason = "missing-country-code" if location.country_code is None else "known-non-singapore-country"
        _LOGGER.warning("Skipping explicit NEA provider reason=%s", reason)
    jma_available = location.jma_office_code is not None and location.country_code in {None, "JP"}
    if settings.weather_providers is not None and WeatherProviderName.JMA_JAPAN in names and not jma_available:
        reason = "missing-jma-office-code" if location.jma_office_code is None else "known-non-japan-country"
        _LOGGER.warning("Skipping explicit JMA provider reason=%s", reason)
    main_names = [name for name in names if name not in LOCAL_WEATHER_CAPABILITY_PROVIDERS]
    supplement_names = [
        name
        for name in names
        if name in LOCAL_WEATHER_CAPABILITY_PROVIDERS and (name != WeatherProviderName.JMA_JAPAN or jma_available)
    ]
    if not main_names:
        main_names, supplement_names = supplement_names, []
    providers: list[WeatherContextProvider] = []
    active_names: list[str] = []
    for name in main_names:
        if name == WeatherProviderName.QWEATHER and not qweather_is_configured(settings):
            if settings.weather_providers is not None:
                raise ValueError("Explicit QWeather provider is missing JWT configuration")
            continue
        providers.append(
            LoggedWeatherContextProvider(
                name,
                build_weather_provider(
                    name,
                    settings,
                    client,
                    location.summary_language,
                    jma_office_code=location.jma_office_code,
                ),
            )
        )
        active_names.append(name)
    if not providers:
        raise ValueError("No configured weather provider is available")
    supplements = tuple(
        LoggedWeatherContextProvider(
            name,
            build_weather_provider(
                name,
                settings,
                client,
                location.summary_language,
                jma_office_code=location.jma_office_code,
            ),
        )
        for name in supplement_names
    )
    _LOGGER.info(
        "Weather provider order providers=%s",
        ",".join(
            name
            for name in (*main_names, *supplement_names)
            if name != WeatherProviderName.QWEATHER or qweather_is_configured(settings)
        ),
    )
    weather_provider: WeatherContextProvider = (
        providers[0] if len(providers) == 1 else FallbackWeatherContextProvider(*providers)
    )
    air_quality_provider = aqicn_provider(settings, client)
    return CapabilityProviderSet(
        weather=weather_provider,
        weather_metadata=weather_provider_metadata(active_names),
        air_quality=air_quality_provider,
        air_quality_metadata=(
            ProviderCapabilities(
                provider_id="air-quality:aqicn",
                provider_name="AQICN",
                capabilities=frozenset({CapabilityName.AIR_QUALITY}),
            )
            if air_quality_provider is not None
            else None
        ),
        supplements=supplements,
        supplement_metadata=tuple(_WEATHER_PROVIDER_METADATA[name] for name in supplement_names),
    )


def qweather_is_configured(settings: Settings) -> bool:
    """Return whether all QWeather JWT settings are present."""
    return all(
        (
            settings.qweather_project_id,
            settings.qweather_credential_id,
            settings.qweather_private_key,
            settings.qweather_base_url,
        )
    )


def build_weather_provider(
    name: str,
    settings: Settings,
    client: httpx.AsyncClient,
    output_language: str = "en",
    *,
    jma_office_code: str | None = None,
) -> WeatherContextProvider:
    """Build one configured weather provider adapter."""
    if name == WeatherProviderName.QWEATHER:
        return _build_qweather(settings, client, output_language=output_language)
    if name == WeatherProviderName.OPEN_METEO:
        return _build_open_meteo(settings, client)
    if name == WeatherProviderName.NEA_SINGAPORE:
        return _build_nea(settings, client)
    if name == WeatherProviderName.JMA_JAPAN:
        return _build_jma(settings, client, office_code=jma_office_code)
    raise ValueError(f"Unsupported weather provider: {name}")


def _build_qweather(
    settings: Settings,
    client: httpx.AsyncClient,
    *,
    output_language: str = "en",
) -> WeatherContextProvider:
    project_id = settings.qweather_project_id
    credential_id = settings.qweather_credential_id
    private_key = settings.qweather_private_key
    base_url = settings.qweather_base_url
    if not project_id or not credential_id or not private_key or not base_url:
        raise ValueError("QWeather provider requires project, credential, private key, and API host settings")
    return QWeatherProvider(
        client,
        authenticator=QWeatherJWTAuthenticator(
            project_id=project_id,
            credential_id=credential_id,
            private_key_base64=private_key,
            lifetime_seconds=settings.qweather_jwt_lifetime_seconds,
        ),
        base_url=base_url,
        output_language=output_language,
    )


def _build_nea(settings: Settings, client: httpx.AsyncClient) -> WeatherContextProvider:
    return NEASingaporeNowcastProvider(client, api_key=settings.nea_api_key)


def _build_jma(
    settings: Settings,
    client: httpx.AsyncClient,
    *,
    office_code: str | None = None,
) -> WeatherContextProvider:
    if office_code is None:
        raise ValueError("JMA provider requires locations.json jma_office_code")
    return JMAJapanForecastProvider(client, office_code=office_code)


def _build_open_meteo(settings: Settings, client: httpx.AsyncClient) -> WeatherContextProvider:
    return OpenMeteoProvider(client, api_key=settings.open_meteo_api_key)


def aqicn_provider(settings: Settings, client: httpx.AsyncClient) -> AirQualityProvider | None:
    """Build the optional AQICN supplement."""
    if not settings.aqicn_api_token:
        return None
    return AQICNProvider(client, token=settings.aqicn_api_token)
