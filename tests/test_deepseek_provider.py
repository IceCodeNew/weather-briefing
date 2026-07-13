import json

import httpx

from weather_briefing.llm import DeepSeekProvider


async def test_deepseek_provider_reuses_chat_completions_with_default_base_url() -> None:
    requests: list[httpx.Request] = []
    model_result = {
        "headline": "Briefing",
        "overview": "Overview",
        "conclusions": [],
        "active_warnings": [],
        "resolved_warning_ids": [],
        "advice": [],
        "disaster_tracking": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(model_result)}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = DeepSeekProvider(
            client,
            api_key="runtime-key",
            model="requested-model",
            max_output_tokens=4096,
        )
        result = await provider.summarize("Return JSON", {"input": "data"})

    assert requests[0].url == "https://api.deepseek.com/chat/completions"
    assert result == model_result
