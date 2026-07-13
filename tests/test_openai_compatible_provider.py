import json

import httpx

from weather_briefing.llm import OpenAICompatibleChatCompletionsProvider


async def test_openai_compatible_provider_uses_chat_completions_json_mode() -> None:
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
        provider = OpenAICompatibleChatCompletionsProvider(
            client,
            api_key="runtime-key",
            base_url="https://api.example.invalid/v1",
            model="requested-model",
            max_output_tokens=4096,
        )
        result = await provider.summarize("Return JSON", {"input": "data"})

    request_body = json.loads(requests[0].content)
    assert requests[0].url.path == "/v1/chat/completions"
    assert request_body["model"] == "requested-model"
    assert request_body["response_format"] == {"type": "json_object"}
    assert request_body["messages"][0] == {"role": "system", "content": "Return JSON"}
    assert result == model_result
