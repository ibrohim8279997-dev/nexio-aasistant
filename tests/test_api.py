"""Этап 5: API Mini App — аутентификация initData, роли и права, изоляция данных, лимиты, кэш советов, связь с Кимо."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.agents.access import ScopedDB
from bot.agents.bus import Bus, BusWorker
from bot.agents.context import AgentContext
from bot.agents.kimo import KimoBusHandler
from bot.agents.registry import Registry
from bot.api.app import ApiContext, create_app
from bot.api.auth import AuthError, RateLimiter, sign_init_data, validate_init_data
from bot.services import grants, perms
from bot.timeutil import now_utc, to_db
from tests.conftest import ADMIN_ID, FakeGroq, FakeRates, make_settings
from tests.fakebot import FAKE_TOKEN, FakeBot

USER_A, USER_B, STRANGER = 4001, 4002, 4003


def init(uid, token=FAKE_TOKEN, age=0, **extra):
    fields = {"auth_date": str(int(time.time()) - age), "query_id": "AAH", "user": json.dumps({"id": uid, "first_name": f"U{uid}"}), **extra}
    return sign_init_data(fields, token)


def hdr(uid, **kw):
    return {"Authorization": f"tma {init(uid, **kw)}"}


class Env:
    pass


@pytest.fixture
async def env(db, tmp_path):
    e = Env()
    e.settings = make_settings(tmp_path)
    e.db = db
    reg = Registry(db)
    await reg.seed_defaults()
    e.notified = []

    async def notify(tg_id, text):
        e.notified.append((tg_id, text))

    e.ctx = ApiContext(settings=e.settings, db=db, bot_token=FAKE_TOKEN, bus=Bus(db, "miniapp", owner_id=ADMIN_ID),
                       rates=FakeRates(db), notify=notify, bot_username="kimo_test_bot", kimo_wait=3.0)
    e.client = TestClient(TestServer(create_app(e.ctx)))
    await e.client.start_server()
    for tg in (USER_A, USER_B):
        user = await db.ensure_user(tg, to_db(now_utc()))
        await grants.grant(db, user, 10, ADMIN_ID)
    e.a = await db.get_user_by_tg(USER_A)
    e.b = await db.get_user_by_tg(USER_B)
    await db.ensure_user(ADMIN_ID, to_db(now_utc()))
    e.kimo_online()
    yield e
    await e.client.close()


def _online(self):
    async def go():
        await self.db.agent_heartbeat("kimo", "online", to_db(now_utc()))
    return asyncio.get_event_loop().create_task(go())


Env.kimo_online = _online


async def kimo_responder(db, result_fn):
    """Фоновый «Кимо»: забирает задания и отвечает result_fn(payload)."""
    bus = Bus(db, "kimo")
    seen = []
    while True:
        task = await bus.claim()
        if task is None:
            await asyncio.sleep(0.02)
            continue
        seen.append(task)
        try:
            await bus.finish(task["id"], result_fn(task["payload"]), ok=True)
        except Exception as exc:  # noqa: BLE001
            await bus.finish(task["id"], f"⚠️ {exc}", ok=False)
        env_seen.append(task)


env_seen: list = []


async def get(env, path, uid=USER_A, **kw):
    r = await env.client.get(path, headers=hdr(uid, **kw))
    return r.status, await r.json()


async def send(env, method, path, data=None, uid=USER_A):
    r = await env.client.request(method, path, headers=hdr(uid), json=data)
    return r.status, await r.json()


# ---------- initData ----------

def test_init_data_validation():
    ok = init(7)
    assert validate_init_data(ok, FAKE_TOKEN)["id"] == 7
    for bad in ("", "hash=abc", ok + "&x=1", init(7, token="1:" + "B" * 35), init(7, age=90000)):
        with pytest.raises(AuthError):
            validate_init_data(bad, FAKE_TOKEN)
    tampered = ok.replace("%22id%22%3A+7", "%22id%22%3A+8")
    assert tampered != ok
    with pytest.raises(AuthError):
        validate_init_data(tampered, FAKE_TOKEN)
    with pytest.raises(AuthError):  # auth_date из будущего
        validate_init_data(init(7, age=-1000), FAKE_TOKEN)
    with pytest.raises(AuthError):
        validate_init_data(ok, "")


def test_init_data_bot_user_rejected():
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": 5, "is_bot": True})}
    with pytest.raises(AuthError):
        validate_init_data(sign_init_data(fields, FAKE_TOKEN), FAKE_TOKEN)


def test_rate_limiter():
    t = [0.0]
    r = RateLimiter(2, 60, clock=lambda: t[0])
    assert r.allow("a") and r.allow("a") and not r.allow("a") and r.allow("b")
    t[0] = 61
    assert r.allow("a")


async def test_health_open_and_api_requires_auth(env):
    r = await env.client.get("/health")
    assert r.status == 200
    for headers in ({}, {"Authorization": "tma garbage"}, {"Authorization": "Bearer x"}):
        r = await env.client.get("/api/me", headers=headers)
        assert r.status == 401
    r = await env.client.get("/api/me", headers=hdr(USER_A, token="1:" + "C" * 35))
    assert r.status == 401


async def test_no_access_user_blocked_with_id(env):
    status, data = await get(env, "/api/me", STRANGER)
    assert status == 403 and data["error"] == "no_access" and str(STRANGER) in data["message"]
    status, _ = await get(env, "/api/tasks", STRANGER)
    assert status == 403


async def test_me_roles(env):
    status, me_a = await get(env, "/api/me", USER_A)
    assert status == 200 and me_a["role"] == "user" and "agents_manage" not in me_a["perms"] and me_a["access_until"]
    status, me_admin = await get(env, "/api/me", ADMIN_ID)
    assert me_admin["role"] == "admin" and "agents_manage" in me_admin["perms"]


async def test_expired_grant_loses_access(env):
    await env.db._write("UPDATE access_grants SET expires_at = ? WHERE user_id = ?",
                        (to_db(now_utc() - timedelta(minutes=1)), env.a["id"]))
    status, data = await get(env, "/api/me", USER_A)
    assert status == 403 and data["error"] == "no_access"


async def test_rate_limit_per_user_and_failed_auth(env):
    env.ctx.limiter = RateLimiter(3)
    codes = [(await get(env, "/api/me", USER_A))[0] for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    assert (await get(env, "/api/me", USER_B))[0] == 200  # у другого пользователя свой счётчик
    env.ctx.auth_fail_limiter = RateLimiter(2)
    codes = [(await env.client.get("/api/me", headers={"Authorization": "tma bad"})).status for _ in range(4)]
    assert codes == [401, 401, 429, 429]


async def test_security_headers(env):
    r = await env.client.get("/api/me", headers=hdr(USER_A))
    assert r.headers["Cache-Control"] == "no-store" and r.headers["X-Content-Type-Options"] == "nosniff"


# ---------- задачи / напоминания ----------

async def test_tasks_crud_and_isolation(env):
    status, created = await send(env, "POST", "/api/tasks", {"text": "Купить мышь", "date": None, "time": None})
    assert status == 201
    tid = created["id"]
    _, listed = await get(env, "/api/tasks?day=today")
    assert [t["text"] for t in listed["tasks"]] == ["Купить мышь"]
    # пользователь B ничего не видит и не может менять
    _, other = await get(env, "/api/tasks?day=today", USER_B)
    assert other["tasks"] == []
    assert (await send(env, "POST", f"/api/tasks/{tid}/done", uid=USER_B))[0] == 404
    assert (await send(env, "DELETE", f"/api/tasks/{tid}", uid=USER_B))[0] == 404
    assert (await send(env, "POST", f"/api/tasks/{tid}/done"))[0] == 200
    _, stats = await get(env, "/api/tasks/stats?days=7")
    assert stats["done"] == 1 and stats["total"] == 1 and stats["percent"] == 100 and len(stats["days"]) == 7
    assert (await send(env, "DELETE", f"/api/tasks/{tid}"))[0] == 200
    assert (await send(env, "POST", "/api/tasks", {"text": ""}))[0] == 400
    assert (await send(env, "POST", "/api/tasks", {"text": "x", "date": "вчера"}))[0] == 400


async def test_reminders_need_time(env):
    assert (await send(env, "POST", "/api/reminders", {"text": "позвонить"}))[0] == 400
    tomorrow = (now_utc() + timedelta(days=1)).astimezone().date().isoformat()
    status, _ = await send(env, "POST", "/api/reminders", {"text": "позвонить", "date": tomorrow, "time": "09:30"})
    assert status == 201
    _, data = await get(env, "/api/reminders")
    assert len(data["reminders"]) == 1 and data["reminders"][0]["has_time"]
    assert (await get(env, "/api/reminders", USER_B))[1]["reminders"] == []


# ---------- финансы ----------

async def test_finance_flow_and_isolation(env):
    assert (await send(env, "POST", "/api/finance", {"kind": "income", "amount": 6_000_000, "currency": "UZS", "category": "Зарплата"}))[0] == 201
    assert (await send(env, "POST", "/api/finance", {"kind": "expense", "amount": 120_000, "currency": "UZS", "category": "Еда"}))[0] == 201
    assert (await send(env, "POST", "/api/finance", {"kind": "expense", "amount": 10, "currency": "USD", "category": "Еда"}))[0] == 201
    _, s = await get(env, "/api/finance/summary?period=month")
    assert s["income_uzs"] == 6_000_000 and s["expense_uzs"] == 240_000 and s["balance_uzs"] == 5_760_000
    assert s["expense_usd"] == pytest.approx(20.0) and s["income_usd"] == pytest.approx(500.0)
    _, c = await get(env, "/api/finance/categories?kind=expense")
    assert c["categories"][0]["category"] == "Еда" and c["categories"][0]["share"] == 1.0
    _, ser = await get(env, "/api/finance/series?group=day&count=3")
    assert len(ser["points"]) == 3 and ser["points"][-1]["income_uzs"] == 6_000_000
    _, wk = await get(env, "/api/finance/series?group=week&count=4")
    assert len(wk["points"]) == 4 and sum(p["expense_uzs"] for p in wk["points"]) == 240_000
    _, mo = await get(env, "/api/finance/series?group=month&count=6")
    assert len(mo["points"]) == 6 and mo["points"][-1]["income_uzs"] == 6_000_000
    # B видит пустоту
    _, sb = await get(env, "/api/finance/summary?period=month", USER_B)
    assert sb["count"] == 0 and sb["income_uzs"] == 0
    _, items = await get(env, "/api/finance/items")
    iid = items["items"][0]["id"]
    assert (await send(env, "DELETE", f"/api/finance/{iid}", uid=USER_B))[0] == 404
    assert (await send(env, "DELETE", f"/api/finance/{iid}"))[0] == 200
    for bad in ({"kind": "gift", "amount": 1}, {"amount": "abc"}, {"amount": -5}, {"amount": 5, "currency": "EUR"}):
        assert (await send(env, "POST", "/api/finance", bad))[0] == 400
    for q in ("period=year", "group=hour", "kind=gift"):
        path = "/api/finance/categories?" + q if "kind" in q or "period" in q else "/api/finance/series?" + q
        assert (await get(env, path))[0] == 400
    _, rate = await get(env, "/api/rate")
    assert rate["usd_uzs"] == 12000.0


# ---------- права ----------

async def test_perm_flags_enforced(env):
    await perms.set_perm(env.db, env.a, "finance", False)
    await perms.set_perm(env.db, env.a, "broadcasts", False)
    assert (await get(env, "/api/finance/summary"))[0] == 403
    assert (await send(env, "POST", "/api/finance", {"amount": 5}))[0] == 403
    assert (await get(env, "/api/broadcasts"))[0] == 403
    assert (await get(env, "/api/tasks"))[0] == 200  # остальные права работают
    assert (await get(env, "/api/home"))[0] == 200


async def test_admin_only_endpoints_forbidden_for_user(env):
    for method, path, data in (("GET", "/api/agents", None), ("POST", "/api/agents", {"description": "агент для тестов тестов"}),
                               ("POST", "/api/agents/kimo/toggle", {"enabled": False}), ("GET", "/api/access", None),
                               ("POST", "/api/access/grant", {"tg_id": 9, "days": 3}), ("POST", "/api/access/revoke", {"tg_id": USER_B})):
        status, data = await send(env, method, path, data)
        assert status == 403, path
    _, st = await get(env, "/api/settings")
    assert "payments" not in st and "tokens" not in st
    assert await env.db.grant_active(env.b["id"]) is not None  # доступ B не тронут


async def test_settings_roundtrip_and_validation(env):
    status, data = await send(env, "PUT", "/api/settings", {"accent": "#ff8800", "lang": "uz", "tz": "Asia/Samarkand", "broadcast_max_per_day": 2})
    assert status == 200 and data["settings"] == {"accent": "#ff8800", "lang": "uz", "tz": "Asia/Samarkand", "broadcast_max_per_day": 2}
    _, other = await get(env, "/api/settings", USER_B)
    assert other["settings"]["accent"] == "#19d3c5" and other["settings"]["lang"] == "ru"
    for bad in ({"accent": "red"}, {"lang": "en"}, {"tz": "Mars/Base"}, {"broadcast_max_per_day": 99}, {"hack": 1}):
        assert (await send(env, "PUT", "/api/settings", bad))[0] == 400


async def test_admin_settings_hide_tokens(env, monkeypatch):
    monkeypatch.setenv("KIMO_TOKEN", "999999:SECRET_TOKEN_VALUE_" + "x" * 20)
    r = await env.client.get("/api/settings", headers=hdr(ADMIN_ID))
    text = await r.text()
    data = json.loads(text)
    assert data["payments"]["subscription_enabled"] is True and data["groq_key_set"] is True
    assert {"agent": "kimo", "set": True} in data["tokens"]
    for secret in ("SECRET_TOKEN_VALUE", FAKE_TOKEN, "gsk_test", env.settings.encryption_key):
        assert secret not in text


# ---------- Business ----------

async def test_business_api(env):
    db = env.db
    await db.bconn_upsert("c1", env.a["id"], 1, True, True, to_db(now_utc()))
    await db.bchat_touch(env.a["id"], 777, "Клиент", "cl", to_db(now_utc()), True)
    await db.bmsg_add(env.a["id"], 777, "client", "привет", 1, to_db(now_utc()))
    await db.bchat_touch(env.b["id"], 888, "Чужой", None, to_db(now_utc()), True)
    _, st = await get(env, "/api/business")
    assert [c["chat_id"] for c in st["chats"]] == [777] and st["connections"][0]["can_reply"]
    assert (await send(env, "PUT", "/api/business/rules", {"prompt": "Скидок не давать", "pause": 3}))[0] == 200
    assert (await send(env, "PUT", "/api/business/rules", {"pause": 999}))[0] == 400
    assert (await send(env, "PUT", "/api/business/rules", {"evil": "x"}))[0] == 400
    _, st = await get(env, "/api/business")
    assert st["rules"]["prompt"] == "Скидок не давать" and st["rules"]["pause"] == 3
    assert (await get(env, "/api/business", USER_B))[1]["rules"]["prompt"] == ""  # правила изолированы
    assert (await send(env, "POST", "/api/business/chats/777/pause", {"excluded": True}))[0] == 200
    assert (await send(env, "POST", "/api/business/chats/888/pause", {"excluded": True}))[0] == 404  # чужой чат
    assert (await send(env, "POST", "/api/business/pause", {"ai_enabled": False}))[0] == 200
    assert (await db.bconn_get("c1"))["ai_enabled"] == 0
    _, log = await get(env, "/api/business/log?chat_id=777")
    assert log["messages"][0]["text"] == "привет"
    _, foreign = await get(env, "/api/business/log?chat_id=888")
    assert foreign["messages"] == []


# ---------- рассылки ----------

async def test_broadcasts_api(env):
    db = env.db
    await db.bchat_touch(env.a["id"], 777, "Клиент", None, to_db(now_utc()), True)
    status, created = await send(env, "POST", "/api/broadcasts", {"chat_id": 777, "time": "10:30", "days": "weekdays", "mode": "ai", "topic": "спроси, как дела"})
    assert status == 201
    sid = created["id"]
    _, st = await get(env, "/api/broadcasts")
    assert st["schedules"][0]["days"] == "12345" and st["chats"][0]["chat_id"] == 777 and "start=sb_" in st["link"]
    assert (await get(env, "/api/broadcasts", USER_B))[1]["schedules"] == []
    assert (await send(env, "PUT", f"/api/broadcasts/{sid}", {"time": "11:00", "active": False}, uid=USER_B))[0] == 404
    assert (await send(env, "PUT", f"/api/broadcasts/{sid}", {"time": "11:00", "active": False}))[0] == 200
    sch = await db.sched_get(env.a["id"], sid)
    assert sch["send_time"] == "11:00" and sch["active"] == 0
    for bad in ({"time": "99:00"}, {"days": "xx"}, {"mode": "voice"}, {"tz": "Mars/Base"}, {"active": "yes"}, {"mode": "text"}):
        assert (await send(env, "PUT", f"/api/broadcasts/{sid}", bad))[0] == 400, bad
    for bad in ({"chat_id": 1, "time": "10:00", "mode": "text", "text": ""}, {"chat_id": "x", "time": "10:00"},
                {"chat_id": 1, "time": "10:00", "mode": "text", "text": "я" * 2000}):
        assert (await send(env, "POST", "/api/broadcasts", bad))[0] == 400
    await db.optout_add(env.a["id"], 777, to_db(now_utc()))
    assert (await send(env, "PUT", f"/api/broadcasts/{sid}", {"active": True}))[0] == 400  # «стоп» нельзя обойти
    assert (await send(env, "DELETE", f"/api/broadcasts/{sid}", uid=USER_B))[0] == 404
    assert (await send(env, "DELETE", f"/api/broadcasts/{sid}"))[0] == 200


# ---------- доступы ----------

async def test_access_admin_api(env):
    status, data = await send(env, "POST", "/api/access/grant", {"tg_id": STRANGER, "days": 7}, uid=ADMIN_ID)
    assert status == 200 and any(t == STRANGER for t, _ in env.notified)
    assert (await get(env, "/api/me", STRANGER))[0] == 200
    _, listing = await get(env, "/api/access", ADMIN_ID)
    assert STRANGER in [g["tg_id"] for g in listing["grants"]]
    assert (await send(env, "POST", "/api/access/revoke", {"tg_id": STRANGER}, uid=ADMIN_ID))[0] == 200
    assert (await get(env, "/api/me", STRANGER))[0] == 403
    assert (await send(env, "POST", "/api/access/revoke", {"tg_id": STRANGER}, uid=ADMIN_ID))[0] == 404
    assert (await send(env, "POST", "/api/access/revoke", {"tg_id": ADMIN_ID}, uid=ADMIN_ID))[0] == 404
    assert (await send(env, "POST", "/api/access/grant", {"tg_id": 5, "days": 0}, uid=ADMIN_ID))[0] == 400
    assert (await send(env, "POST", "/api/access/grant", {"tg_id": "x", "days": 3}, uid=ADMIN_ID))[0] == 400


# ---------- Кимо: ассистент, агенты, советы ----------

async def test_assistant_goes_through_kimo(env):
    env_seen.clear()
    worker = asyncio.create_task(kimo_responder(env.db, lambda p: json.dumps({"text": f"эхо: {p['text']}"})))
    try:
        status, data = await send(env, "POST", "/api/assistant", {"text": "что у меня сегодня?"})
        assert status == 200 and data["text"] == "эхо: что у меня сегодня?"
        task = env_seen[0]
        assert task["from_agent"] == "miniapp" and task["payload"]["tg_id"] == USER_A and task["payload"]["source"] == "miniapp"
        assert (await send(env, "POST", "/api/assistant", {"text": ""}))[0] == 400
        assert (await send(env, "POST", "/api/assistant", {"text": "я" * 1001}))[0] == 400
    finally:
        worker.cancel()


async def test_kimo_offline_gives_503(env):
    await env.db._write("UPDATE agents SET last_seen = ? WHERE name = 'kimo'", (to_db(now_utc() - timedelta(hours=1)),))
    assert (await send(env, "POST", "/api/assistant", {"text": "привет"}))[0] == 503


async def test_agents_list_hides_config_and_toggle_goes_to_kimo(env):
    _, data = await get(env, "/api/agents", ADMIN_ID)
    names = {a["name"] for a in data["agents"]}
    assert {"kimo", "agent_builder", "assistant"} <= names
    assert all("config" not in a and "token" not in json.dumps(a).lower() for a in data["agents"])
    env_seen.clear()
    worker = asyncio.create_task(kimo_responder(env.db, lambda p: json.dumps({"text": "Подтвердите в Telegram"})))
    try:
        status, res = await send(env, "POST", "/api/agents/assistant/toggle", {"enabled": False}, uid=ADMIN_ID)
        assert status == 200 and "Telegram" in res["text"]
        assert env_seen[0]["payload"]["action"] == {"type": "manage_agent", "agent": "assistant", "op": "stop"}
        assert (await send(env, "POST", "/api/agents/nope/toggle", {"enabled": True}, uid=ADMIN_ID))[0] == 404
        assert (await send(env, "POST", "/api/agents/assistant/toggle", {"enabled": "yes"}, uid=ADMIN_ID))[0] == 400
        assert (await send(env, "POST", "/api/agents", {"description": "коротко"}, uid=ADMIN_ID))[0] == 400
        status, _ = await send(env, "POST", "/api/agents", {"description": "Агент, который ведёт заказы клиентов магазина"}, uid=ADMIN_ID)
        assert status == 200 and env_seen[-1]["payload"]["action"]["type"] == "create_agent"
    finally:
        worker.cancel()
    audit = await env.db.audit_list(limit=10, action="agent_toggle_request")
    assert audit and audit[0]["actor"] == f"miniapp:{ADMIN_ID}"


async def test_advice_generated_once_per_day_and_cached(env):
    from bot.services import expenses as exp

    await exp.create_expense(env.db, FakeRates(env.db), env.a["id"], 50000, "UZS", "Еда", None)
    env_seen.clear()
    worker = asyncio.create_task(kimo_responder(env.db, lambda p: json.dumps({"tips": [{"area": "expenses", "text": "Следите за едой"}]})))
    try:
        _, first = await get(env, "/api/advice")
        assert first["tips"][0]["text"] == "Следите за едой" and first["cached"] is False
        _, second = await get(env, "/api/advice")
        assert second["cached"] is True and second["tips"] == first["tips"]
        results = await asyncio.gather(*[get(env, "/api/advice") for _ in range(3)])
        assert all(r[1]["cached"] for r in results)
        assert len([t for t in env_seen if t["payload"]["type"] == "advice"]) == 1  # лимит Groq не тратится
        sent = env_seen[0]["payload"]
        assert sent["stats"]["expense_uzs"] == 50000 and "Еда" in json.dumps(sent["stats"], ensure_ascii=False)
        assert (await get(env, "/api/advice", USER_B))[1]["empty"] is True  # у B нет данных — ИИ не зовём
        assert len([t for t in env_seen if t["payload"]["type"] == "advice"]) == 1
    finally:
        worker.cancel()


async def test_advice_failure_falls_back_to_previous(env):
    from bot.services import expenses as exp

    await exp.create_expense(env.db, FakeRates(env.db), env.a["id"], 1000, "UZS", "Еда", None)
    await env.db.advice_set(env.a["id"], "2000-01-01", "ru", json.dumps({"tips": [{"area": "tasks", "text": "старый совет"}]}), to_db(now_utc()))

    def boom(payload):
        raise RuntimeError("лимит")

    worker = asyncio.create_task(kimo_responder(env.db, boom))
    try:
        _, data = await get(env, "/api/advice")
        assert data["stale"] is True and data["tips"][0]["text"] == "старый совет"
        await env.db._write("DELETE FROM ai_advice")
        _, data = await get(env, "/api/advice")
        assert data["unavailable"] is True
    finally:
        worker.cancel()


async def test_home_summary(env):
    await send(env, "POST", "/api/tasks", {"text": "a"})
    _, home = await get(env, "/api/home")
    assert home["tasks"] == {"done": 0, "total": 1} and "agents" not in home
    _, admin_home = await get(env, "/api/home", ADMIN_ID)
    assert len(admin_home["agents"]) >= 3 and admin_home["pulse"] >= 1


# ---------- обработчики Кимо для Mini App ----------

async def kimo_handler(db, tmp_path, llm):
    settings = make_settings(tmp_path)
    reg = Registry(db)
    await reg.seed_defaults()
    cfg = await reg.get("kimo")
    scoped = ScopedDB(db, "kimo", cfg.allowed_tables, role="admin")
    ctx = AgentContext(cfg, reg, db, Bus(db, "kimo", owner_id=ADMIN_ID))
    return KimoBusHandler(ctx, scoped, llm, FakeRates(scoped), settings, FakeBot()), settings


def task_for(from_agent="miniapp", **payload):
    return {"id": 1, "from_agent": from_agent, "depth": 1, "payload": {"source": "miniapp", **payload}}


async def test_kimo_user_command_enforces_rights(db, tmp_path):
    llm = FakeGroq()
    handler, _ = await kimo_handler(db, tmp_path, llm)
    user = await db.ensure_user(USER_A, to_db(now_utc()))
    from bot.agents.bus import BusError

    with pytest.raises(BusError):  # без доступа
        await handler.user_command(task_for(tg_id=USER_A, text="привет"))
    await grants.grant(db, user, 5, ADMIN_ID)
    with pytest.raises(BusError):  # чужой источник
        await handler.user_command(task_for(from_agent="assistant", tg_id=USER_A, text="привет"))
    with pytest.raises(BusError):  # типизированные действия — только владельцу
        await handler.user_command(task_for(tg_id=USER_A, action={"type": "manage_agent", "agent": "assistant", "op": "stop"}))
    llm.replies.append(json.dumps({"actions": [{"type": "add_expense", "amount": 5000, "category": "Еда"}]}))
    result = json.loads(await handler.user_command(task_for(tg_id=USER_A, text="еда 5000")))
    assert "Записал" in result["text"]
    rows = await db.expenses_between(user["id"], "2000-01-01 00:00:00", "2100-01-01 00:00:00")
    assert len(rows) == 1
    await perms.set_perm(db, user, "finance", False)  # право выключено — действие не выполняется
    llm.replies.append(json.dumps({"actions": [{"type": "add_expense", "amount": 7000, "category": "Еда"}]}))
    result = json.loads(await handler.user_command(task_for(tg_id=USER_A, text="еда 7000")))
    assert "недоступна" in result["text"]
    assert len(await db.expenses_between(user["id"], "2000-01-01 00:00:00", "2100-01-01 00:00:00")) == 1
    # попытка управлять агентами через ИИ-фразу от обычного пользователя не проходит
    llm.replies.append(json.dumps({"actions": [{"type": "manage_agent", "agent": "assistant", "op": "stop"}]}))
    with pytest.raises(BusError):
        await handler.user_command(task_for(tg_id=USER_A, text="останови мими"))


async def test_kimo_advice_handler(db, tmp_path):
    llm = FakeGroq()
    handler, _ = await kimo_handler(db, tmp_path, llm)
    user = await db.ensure_user(USER_A, to_db(now_utc()))
    await grants.grant(db, user, 5, ADMIN_ID)
    llm.replies.append(json.dumps({"tips": [{"area": "expenses", "text": "Совет"}, {"area": "tasks", "text": ""}]}))
    out = json.loads(await handler.advice(task_for(tg_id=USER_A, lang="uz", stats={"expense_uzs": 1})))
    assert out["tips"] == [{"area": "expenses", "text": "Совет"}]
    assert "узбекский" in llm.prompts[-1][0]["content"]
    from bot.agents.bus import BusError

    llm.replies.append("мусор")
    with pytest.raises(BusError):
        await handler.advice(task_for(tg_id=USER_A, lang="ru", stats={}))
