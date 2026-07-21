import pytest

from weather_briefing.languages import LanguageSupport, localized_labels, normalize_language_tag
from weather_briefing.models import BriefingResult, LocationSpec, ResolvedLocation, SourceDocument
from weather_briefing.weather_context import OPEN_METEO_LANGUAGE_SUPPORT, QWEATHER_LANGUAGE_SUPPORT


def test_language_tags_are_normalized() -> None:
    assert normalize_language_tag(" zh-hans ") == "zh-Hans"
    assert normalize_language_tag("EN-us") == "en-US"


@pytest.mark.parametrize("value", ("", "en_US", "english"))
def test_language_tags_reject_invalid_values(value: str) -> None:
    with pytest.raises(ValueError, match="BCP 47"):
        normalize_language_tag(value)


def test_language_support_selects_supported_api_language() -> None:
    support = LanguageSupport(
        default="zh-CN",
        supported=("zh-CN", "ja"),
        api_codes=(("zh-CN", "zh"), ("ja", "ja")),
    )

    assert support.select(None) == "zh-CN"
    assert support.select("JA") == "ja"
    assert support.api_code("ja") == "ja"
    assert support.selectable is True


def test_language_support_matches_region_variant_to_provider_language() -> None:
    support = LanguageSupport(default="zh-CN", supported=("zh-CN", "ja"))

    assert support.match(None) == "zh-CN"
    assert support.match("ja-JP") == "ja"
    assert support.match("fr-FR") == "zh-CN"


def test_fixed_language_rejects_other_output_language() -> None:
    support = LanguageSupport.fixed("en")

    assert support.selectable is False
    assert support.api_code() == "en"
    with pytest.raises(ValueError, match="does not support"):
        support.select("ja")


def test_weather_provider_language_metadata_distinguishes_selectable_and_fixed_sources() -> None:
    assert QWEATHER_LANGUAGE_SUPPORT.select("ja") == "ja"
    assert QWEATHER_LANGUAGE_SUPPORT.api_code("ja") == "ja"
    assert OPEN_METEO_LANGUAGE_SUPPORT.selectable is False
    assert OPEN_METEO_LANGUAGE_SUPPORT.default == "en"


@pytest.mark.parametrize(
    ("default", "supported"),
    (("en", ()), ("ja", ("en",)), ("en", ("en", "EN"))),
)
def test_language_support_rejects_invalid_supported_sets(default: str, supported: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="unique supported default"):
        LanguageSupport(default=default, supported=supported)


@pytest.mark.parametrize(
    "api_codes",
    (
        (("en", "en"),),
        (("en", "en"), ("ja", "ja"), ("JA", "jp")),
        (("en", "en"), ("fr", "fr")),
        (("en", "en"), ("ja", " ")),
    ),
)
def test_language_support_rejects_incomplete_or_invalid_api_codes(
    api_codes: tuple[tuple[str, str], ...],
) -> None:
    with pytest.raises(ValueError, match="API codes"):
        LanguageSupport(default="en", supported=("en", "ja"), api_codes=api_codes)


def test_localized_labels_match_exact_then_primary_language() -> None:
    translations = {"en": {"label": "Label"}, "zh-TW": {"label": "標籤"}}

    assert localized_labels("en-US", translations)["label"] == "Label"
    assert localized_labels("zh-TW", translations)["label"] == "標籤"
    with pytest.raises(ValueError, match="No document scaffold"):
        localized_labels("ja", translations)


def test_source_document_normalizes_language_at_model_boundary() -> None:
    document = SourceDocument("source", "Source", "https://example.invalid/source", "内容", language="JA")

    assert document.language == "ja"


def test_source_document_rejects_invalid_language() -> None:
    with pytest.raises(ValueError, match="language tag"):
        SourceDocument("source", "Source", "https://example.invalid/source", "内容", language="english")


def test_location_and_briefing_models_normalize_language_fields() -> None:
    location_spec = LocationSpec("tokyo", "Tokyo", summary_language="JA-jp")
    resolved_location = ResolvedLocation(
        "tokyo",
        "Tokyo",
        1.0,
        1.0,
        "JP",
        None,
        "Asia/Tokyo",
        False,
        summary_language="JA-jp",
    )
    result = BriefingResult("Headline", ("source",), (), output_language="JA-jp")

    assert location_spec.summary_language == "ja-JP"
    assert resolved_location.summary_language == "ja-JP"
    assert result.output_language == "ja-JP"


def test_location_and_briefing_models_reject_invalid_language_fields() -> None:
    with pytest.raises(ValueError, match="language tag"):
        LocationSpec("tokyo", "Tokyo", summary_language="english")
    with pytest.raises(ValueError, match="language tag"):
        ResolvedLocation(
            "tokyo",
            "Tokyo",
            1.0,
            1.0,
            "JP",
            None,
            "Asia/Tokyo",
            False,
            summary_language="english",
        )
    with pytest.raises(ValueError, match="language tag"):
        BriefingResult("Headline", ("source",), (), output_language="english")
