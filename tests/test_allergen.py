import pendulum
import pytest

from weather_briefing.allergen import (
    _allergen_bands,
    allergen_guidance,
    allergen_to_document,
    pollen_type_names,
)
from weather_briefing.models import AllergenLevel, AllergenSnapshot
from weather_briefing.reference_data import ReferenceDataError


def test_allergen_guidance_zero_is_none() -> None:
    category, guidance = allergen_guidance(0)
    assert category == "None"
    assert guidance


def test_allergen_guidance_low_band() -> None:
    category, _ = allergen_guidance(5)
    assert category == "Low"


def test_allergen_guidance_moderate_band() -> None:
    category, _ = allergen_guidance(20)
    assert category == "Moderate"


def test_allergen_guidance_high_band() -> None:
    category, _ = allergen_guidance(50)
    assert category == "High"


def test_allergen_guidance_extreme_band() -> None:
    category, _ = allergen_guidance(500)
    assert category == "Very high"


@pytest.mark.parametrize("concentration", [-1, float("nan"), float("inf"), float("-inf")])
def test_allergen_guidance_rejects_invalid_concentrations(concentration: float) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        allergen_guidance(concentration)


def test_pollen_type_names_includes_expected_types() -> None:
    keys = {key for key, _ in pollen_type_names()}
    assert {"alder", "birch", "grass", "mugwort", "olive", "ragweed"} <= keys
    names = {name for _, name in pollen_type_names()}
    assert all(name for name in names)


def test_allergen_to_document_format() -> None:
    snapshot = AllergenSnapshot(
        source_id="allergen:test",
        source_name="Test 花粉",
        source_url="https://example.invalid/allergen",
        observed_at=pendulum.datetime(2026, 7, 13, 8, tz="UTC"),
        levels=(
            AllergenLevel(name="桦木", category="中", concentration=15),
            AllergenLevel(name="禾本", category="低", concentration=3),
        ),
        overall_category="中",
        health_guidance="花粉浓度中等，过敏体质人群户外活动后建议清洗面部和鼻腔。",
    )

    document = allergen_to_document(snapshot)

    assert document.id == "allergen:test"
    assert document.name == "Test 花粉"
    assert document.has_allergen_information
    assert "桦木：15 粒/m³（中）" in document.content
    assert "禾本：3 粒/m³（低）" in document.content
    assert "总体等级：中" in document.content
    assert document.history_value is not None
    assert "2026-07-13T08:00:00Z" not in document.history_value


def test_allergen_document_scaffold_matches_english_source_language() -> None:
    snapshot = AllergenSnapshot(
        source_id="allergen:test",
        source_name="Test pollen",
        source_url="https://example.invalid/allergen",
        observed_at=None,
        levels=(AllergenLevel(name="Birch", category="Moderate", concentration=15),),
        overall_category="Moderate",
        health_guidance="Reduce prolonged outdoor activity.",
        output_language="en",
    )

    document = allergen_to_document(snapshot)

    assert document.language == "en"
    assert "Pollen allergens:" in document.content
    assert "Birch: 15 grains/m³ (Moderate)" in document.content
    assert "花粉过敏原" not in document.content


def test_allergen_to_document_without_observed_at() -> None:
    snapshot = AllergenSnapshot(
        source_id="allergen:test",
        source_name="Test 花粉",
        source_url="https://example.invalid",
        observed_at=None,
        levels=(AllergenLevel(name="蒿属", category="无", concentration=0),),
        overall_category="无",
        health_guidance="花粉浓度极低。",
    )

    document = allergen_to_document(snapshot)

    assert "观测时间：不可用" in document.content


def test_allergen_to_document_with_empty_levels() -> None:
    snapshot = AllergenSnapshot(
        source_id="allergen:test",
        source_name="Test 花粉",
        source_url="https://example.invalid",
        observed_at=None,
        levels=(),
        overall_category="无",
        health_guidance="无花粉。",
    )

    document = allergen_to_document(snapshot)

    assert "花粉过敏原：\n不可用" in document.content


def test_pollen_type_names_rejects_empty_dict(monkeypatch) -> None:
    monkeypatch.setattr(
        "weather_briefing.allergen.reference_value",
        lambda *args: {},
    )
    pollen_type_names.cache_clear()
    try:
        with pytest.raises(ReferenceDataError, match="non-empty object"):
            pollen_type_names()
    finally:
        pollen_type_names.cache_clear()


def test_allergen_bands_rejects_empty_list(monkeypatch) -> None:
    monkeypatch.setattr(
        "weather_briefing.allergen.reference_value",
        lambda *args: [],
    )
    _allergen_bands.cache_clear()
    try:
        with pytest.raises(ReferenceDataError, match="non-empty list"):
            _allergen_bands()
    finally:
        _allergen_bands.cache_clear()


def test_allergen_bands_rejects_missing_concentration(monkeypatch) -> None:
    monkeypatch.setattr(
        "weather_briefing.allergen.reference_value",
        lambda *args: [{"category": "低", "guidance": "x"}],
    )
    _allergen_bands.cache_clear()
    try:
        with pytest.raises(ReferenceDataError, match="Invalid allergen guidance band"):
            _allergen_bands()
    finally:
        _allergen_bands.cache_clear()


def test_allergen_bands_requires_unbounded_last_band(monkeypatch) -> None:
    monkeypatch.setattr(
        "weather_briefing.allergen.reference_value",
        lambda *args: [
            {"maximum_concentration": 10, "category": "低", "guidance": "x"},
            {"maximum_concentration": 50, "category": "高", "guidance": "y"},
        ],
    )
    _allergen_bands.cache_clear()
    try:
        with pytest.raises(ReferenceDataError, match="unbounded band"):
            _allergen_bands()
    finally:
        _allergen_bands.cache_clear()


def test_allergen_bands_requires_increasing_bounds(monkeypatch) -> None:
    monkeypatch.setattr(
        "weather_briefing.allergen.reference_value",
        lambda *args: [
            {"maximum_concentration": 50, "category": "低", "guidance": "x"},
            {"maximum_concentration": 10, "category": "中", "guidance": "y"},
            {"maximum_concentration": None, "category": "极高", "guidance": "z"},
        ],
    )
    _allergen_bands.cache_clear()
    try:
        with pytest.raises(ReferenceDataError, match="unique, increasing"):
            _allergen_bands()
    finally:
        _allergen_bands.cache_clear()


def test_allergen_guidance_fallback_when_all_bands_bounded(monkeypatch) -> None:
    monkeypatch.setattr(
        "weather_briefing.allergen._allergen_bands",
        lambda: ((10.0, "低", "x"), (50.0, "高", "y")),
    )
    with pytest.raises(ReferenceDataError, match="unbounded band"):
        allergen_guidance(10_000)


def test_pollen_type_names_rejects_non_dict(monkeypatch) -> None:
    monkeypatch.setattr(
        "weather_briefing.allergen.reference_value",
        lambda *args: ["not", "a", "dict"],
    )
    pollen_type_names.cache_clear()
    try:
        with pytest.raises(ReferenceDataError, match="non-empty object"):
            pollen_type_names()
    finally:
        pollen_type_names.cache_clear()
