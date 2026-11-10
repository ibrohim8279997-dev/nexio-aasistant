"""Ассистент: своя таблица, веб-поиск (ошибки и лимит), 429 с повтором и уведомлением, общий лимит токенов."""
import json
import time

import httpx
import pytest

from bot.agents.access import AccessDenied, ScopedDB
from bot.agents.bus import Bus
from bot.agents.context import AgentContext
from bot.agents.generic import GenericAgent
from bot.agents.registry import Registry
from bot.ai import groq_client
from bot.ai.groq_client import GroqClient, GroqRateLimitError, GroqSearchError, SharedLimits
from tests.conftest import ADMIN_ID, FakeGroq, make_settings

NOW = "2026-11-10 10:00:00"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(groq_client.asyncio, "sleep", fake_sleep)
    return sleeps


def client(handler, **kw):
    http = httpx.AsyncClient(base_url=groq_client.BASE_URL, transport=httpx.MockTransport(handler))
    return GroqClient("key", "openai/gpt-oss-120b", "", client=http, **kw)


OK = {"choices": [{"message": {"content": "{}"}}], "usage": {"total_tokens": 10}}


async def test_429_waits_retry_after_two_retries_then_notifies(no_sleep):
    calls, notices = {"n": 0}, []

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "limit"}})

    c = client(handler)
    c.on_rate_limit = lambda wait, exhausted: _note(notices, wait, exhausted)
    with pytest.raises(GroqRateLimitError):
        await c.chat_json([{"role": "user", "content": "x"}])
    assert calls["n"] == 3  # запрос + 2 повтора
    assert no_sleep.count(7.0) == 2  # пауза по retry-after
    assert c.rate_limit_until > time.time()  # heartbeat увидит busy/rate_limit
    assert len(notices) == 1 and notices[0][1] is False  # владельцу сообщили (не чаще раза в 5 минут)


async def _note(notices, wait, exhausted):
    notices.append((wait, exhausted))


async def test_429_then_success_clears_rate_limit():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "1"})
        return httpx.Response(200, json=OK)

    c = client(handler)
    await c.chat_json([{"role": "user", "content": "x"}])
    assert state["n"] == 2 and c.rate_limit_until == 0.0


async def test_shared_limit_between_processes(tmp_path, no_sleep):
    path = tmp_path / "groq_shared.json"
    a = client(lambda r: httpx.Response(200, headers={"x-ratelimit-remaining-tokens": "100", "x-ratelimit-reset-tokens": "4s"}, json=OK),
               shared_path=path)
    await a.chat_json([{"role": "user", "content": "x"}])  # процесс A выбрал минутный лимит
    assert json.loads(path.read_text())["remaining"] == 100
    b = client(lambda r: httpx.Response(200, json=OK), shared_path=path)  # другой процесс (другой агент)
    await b.chat_json([{"role": "user", "content": "x"}])
    assert no_sleep and 3.0 < no_sleep[0] <= 4.0  # B подождал сброса окна, а не получил 429
    assert SharedLimits(tmp_path / "missing.json").wait_seconds() == 0.0


async def test_search_request_uses_browser_search_and_logs_error_reason(caplog):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if "tool_choice" in body:
            return httpx.Response(400, json={"error": {"code": "invalid_request", "message": "tool_choice not supported"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "курс 12000", "executed_tools": [{}]}}], "usage": {"total_tokens": 900}})

    c = client(handler)
    res = await c.chat([{"role": "user", "content": "курс"}], search=True, agent="assistant")
    assert res.searches == 1 and seen[0]["tools"] == [{"type": "browser_search"}] and seen[0]["tool_choice"] == "required"
    assert "tool_choice" not in seen[1] and seen[1]["reasoning_effort"] == "low" and "response_format" not in seen[1]


async def test_search_failure_gives_understandable_error(caplog):
    c = client(lambda r: httpx.Response(400, json={"error": {"code": "x", "message": "browser_search is not enabled"}}))
    with caplog.at_level("ERROR"):
        with pytest.raises(GroqSearchError) as info:
            await c.chat([{"role": "user", "content": "курс"}], search=True)
    assert info.value.status == 400 and "Веб-поиск" in info.value.user_message
    assert "browser_search" in caplog.text and "key" not in caplog.text.replace("Ключ", "")  # причина в логе, ключа нет


async def _generic(db, tmp_path, name="assistant", **settings_over):
    reg = Registry(db)
    await reg.seed_defaults()
    cfg = await reg.get(name)
    ctx = AgentContext(cfg, reg, db, Bus(db, name, owner_id=ADMIN_ID))
    ctx.data = ScopedDB(db, name, cfg.allowed_tables, cfg.access_role, live=True)
    llm = FakeGroq()
    return GenericAgent(ctx, llm, make_settings(tmp_path, **settings_over)), llm, ctx


class SearchFails(FakeGroq):
    async def chat(self, messages, *, json_mode=False, search=False, max_tokens=2048, agent=""):
        if search:
            raise GroqSearchError("browser_search: HTTP 400", status=400)
        return await super().chat(messages, json_mode=json_mode, search=search, max_tokens=max_tokens, agent=agent)


async def test_search_failure_falls_back_to_plain_answer(db, tmp_path):
    agent, _, ctx = await _generic(db, tmp_path)
    agent.llm = SearchFails()
    agent.llm.replies += ['{"action":"search","query":"курс доллара"}', "По моим данным около 12 000 сум"]
    answer = await agent.answer("какой курс доллара сегодня?")
    assert "Веб-поиск сейчас недоступен" in answer and "12 000" in answer
    assert ctx.state.last_error == "Веб-поиск Groq недоступен"  # heartbeat покажет причину


async def test_search_flag_and_prompt(db, tmp_path):
    agent, llm, _ = await _generic(db, tmp_path)
    llm.replies += ['{"action":"search","query":"погода"}', "Солнечно"]
    await agent.answer("погода в Ташкенте?")
    assert "через search" in llm.prompts[0][0]["content"]  # подсказка: свежие факты — только через поиск
    assert llm.calls[1]["search"] is True and "не больше 2 поисков" in llm.prompts[1][0]["content"]
    off, llm_off, _ = await _generic(db, tmp_path, search_enabled=False)
    assert '"action": "search"' not in (await _plan_prompt(off, llm_off))


async def _plan_prompt(agent, llm):
    llm.replies.append('{"action":"answer","text":"ok"}')
    await agent.answer("привет")
    return llm.prompts[-1][0]["content"]


async def test_assistant_has_own_table_and_remembers(db, tmp_path):
    agent, llm, ctx = await _generic(db, tmp_path)
    assert "agent_assistant_notes" in ctx.agent.allowed_tables  # таблица зарегистрирована у ассистента
    llm.replies.append('{"action":"remember","text":"пароль от wifi в сейфе"}')
    assert "Запомнил" in await agent.answer("запомни: пароль от wifi в сейфе")
    llm.replies.append('{"action":"answer","text":"ok"}')
    await agent.answer("что я просил запомнить?")
    assert "пароль от wifi в сейфе" in llm.prompts[-1][0]["content"]  # заметки подмешиваются в контекст
    # другие агенты к таблице ассистента не имеют доступа
    other = ScopedDB(db, "midi", [], "agent", live=False)
    with pytest.raises(AccessDenied):
        await other.list_notes()
