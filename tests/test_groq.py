import httpx
import pytest

from bot.ai import groq_client
from bot.ai.groq_client import GroqClient, GroqRateLimitError, RateWindow


def make_client(handler):
    http = httpx.AsyncClient(base_url=groq_client.BASE_URL, transport=httpx.MockTransport(handler))
    return GroqClient("key", "openai/gpt-oss-120b", "whisper-large-v3-turbo", client=http)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def fake_sleep(_):
        return None

    monkeypatch.setattr(groq_client.asyncio, "sleep", fake_sleep)


async def test_429_then_success():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "1"}, json={"error": {"message": "rate"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"actions": []}'}}]})

    client = make_client(handler)
    assert await client.chat_json([{"role": "user", "content": "x"}]) == '{"actions": []}'
    assert calls["n"] == 2


async def test_429_long_wait_raises():
    client = make_client(lambda r: httpx.Response(429, headers={"retry-after": "120"}))
    with pytest.raises(GroqRateLimitError) as info:
        await client.chat_json([{"role": "user", "content": "x"}])
    assert "120" in info.value.user_message


async def test_transcribe_sends_model_without_language():
    seen = {}

    def handler(request):
        seen["body"] = request.content
        return httpx.Response(200, json={"text": " Ertaga soat 10 da uchrashuv "})

    client = make_client(handler)
    assert await client.transcribe(b"OggS...") == "Ertaga soat 10 da uchrashuv"
    assert b"whisper-large-v3-turbo" in seen["body"] and b"language" not in seen["body"]


def test_rate_window():
    w = RateWindow(per_minute=2, per_day=3)
    w.hit(0)
    w.hit(1)
    wait, daily = w.wait_time(2)
    assert wait == pytest.approx(58) and not daily
    assert w.wait_time(61) == (0.0, False)
    w.hit(61)
    wait, daily = w.wait_time(200)
    assert daily


async def test_json_mode_400_falls_back():
    import json as _json

    bodies = []

    def handler(request):
        body = _json.loads(request.content)
        bodies.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"code": "json_validate_failed", "message": "bad"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"actions": []}'}}]})

    client = make_client(handler)
    assert await client.chat_json([{"role": "user", "content": "x"}]) == '{"actions": []}'
    assert len(bodies) == 2 and "response_format" not in bodies[1]


def test_parse_reset():
    from bot.ai.groq_client import parse_reset

    assert parse_reset("7.66s") == pytest.approx(7.66)
    assert parse_reset("2m59.5s") == pytest.approx(179.5)
    assert parse_reset("120ms") == pytest.approx(0.12)
    assert parse_reset("1h") == 3600
    assert parse_reset("") is None and parse_reset("abc") is None


def test_token_window():
    from bot.ai.groq_client import TokenWindow

    w = TokenWindow(per_minute=8000, per_day=20000)
    w.add(5000, now=0)
    assert w.wait_time(1) == (0.0, False)
    w.add(3500, now=2)
    wait, daily = w.wait_time(3)
    assert wait == pytest.approx(57) and not daily
    assert w.wait_time(61) == (0.0, False)
    w.add(12000, now=70)
    assert w.wait_time(80)[1] is True


async def test_search_request_and_usage_log(caplog):
    import json as _json
    import logging

    seen = {}

    def handler(request):
        seen.update(_json.loads(request.content))
        return httpx.Response(
            200,
            headers={"x-ratelimit-remaining-tokens": "7100", "x-ratelimit-remaining-requests": "999"},
            json={
                "choices": [{"message": {"content": "Ответ 【1†L1】", "executed_tools": [{"type": "search"}]}}],
                "usage": {"prompt_tokens": 600, "completion_tokens": 300, "total_tokens": 900},
            },
        )

    client = make_client(handler)
    with caplog.at_level(logging.INFO, logger="usage"):
        res = await client.chat([{"role": "user", "content": "погода"}], search=True, json_mode=True, agent="assistant")
    assert seen["tools"] == [{"type": "browser_search"}] and "response_format" not in seen
    assert res.searches == 1 and res.usage["total_tokens"] == 900
    line = caplog.records[-1].getMessage()
    assert "remaining_tokens=7100" in line and "remaining_requests=999" in line and "total=900" in line
    assert "key" not in line


async def test_low_remaining_tokens_waits(monkeypatch):
    waits = []

    async def fake_sleep(sec):
        waits.append(sec)

    monkeypatch.setattr(groq_client.asyncio, "sleep", fake_sleep)
    client = make_client(lambda r: httpx.Response(
        200, headers={"x-ratelimit-remaining-tokens": "100", "x-ratelimit-reset-tokens": "3.5s"},
        json={"choices": [{"message": {"content": "{}"}}], "usage": {"total_tokens": 10}},
    ))
    await client.chat_json([{"role": "user", "content": "x"}])
    await client.chat_json([{"role": "user", "content": "x"}])
    assert waits == [3.5]


async def test_stt_disabled():
    from bot.ai.groq_client import SttDisabledError

    client = GroqClient("k", "openai/gpt-oss-120b", "", client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: None)))
    with pytest.raises(SttDisabledError):
        await client.transcribe(b"x")
