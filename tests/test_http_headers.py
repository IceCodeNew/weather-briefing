import json

import pytest

from weather_briefing.config.base import ConfigurationError
from weather_briefing.config.http_headers import headers_from_env


@pytest.mark.parametrize(
    ("configured", "message"),
    (
        ("not-json", "valid JSON object"),
        ("null", "must be a JSON object"),
        ("[]", "must be a JSON object"),
        ('"value"', "valid JSON object"),
        ('{"X-Count":1}', "header values must be strings"),
        ('{"Bad Name":"value"}', "invalid HTTP header name"),
        ('{"":"value"}', "invalid HTTP header name"),
        ('{"X-Test":"café"}', "only ASCII characters"),
        ('{"X-Test":"line\\nbreak"}', "control characters"),
        ('{"X-Test":"value","x-test":"other"}', "duplicate HTTP header names"),
    ),
)
def test_headers_from_env_rejects_invalid_json_objects(monkeypatch, configured: str, message: str) -> None:
    monkeypatch.setenv("LLM_EXTRA_HEADERS", configured)

    with pytest.raises(ConfigurationError, match=message):
        headers_from_env("LLM_EXTRA_HEADERS")


def test_header_errors_do_not_disclose_header_data(monkeypatch) -> None:
    private_name = "X-Private-Token"
    private_value = "private-value\nsecond-line"
    monkeypatch.setenv("LLM_EXTRA_HEADERS", json.dumps({private_name: private_value}))

    with pytest.raises(ConfigurationError) as error:
        headers_from_env("LLM_EXTRA_HEADERS")

    assert private_name not in str(error.value)
    assert private_value not in str(error.value)
