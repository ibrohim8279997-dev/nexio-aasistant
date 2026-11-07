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
