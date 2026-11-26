"""Этап 6: статика Mini App отдаётся с безопасными заголовками, перевод полный, данные не попадают в innerHTML."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.api.app import ApiContext, create_app
from tests.conftest import make_settings
from tests.fakebot import FAKE_TOKEN

WEBAPP = Path(__file__).resolve().parent.parent / "webapp"


@pytest.fixture
async def client(db, tmp_path):
    ctx = ApiContext(settings=make_settings(tmp_path), db=db, bot_token=FAKE_TOKEN, webapp_dir=WEBAPP)
    c = TestClient(TestServer(create_app(ctx)))
    await c.start_server()
    yield c
    await c.close()


async def test_index_and_static_served(client):
    r = await client.get("/")
    assert r.status == 200 and "Кимо" in await r.text()
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self' https://telegram.org" in csp and "frame-ancestors" in csp and "object-src" not in csp.replace("default-src", "")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    for name in ("app.js", "app.css", "i18n.js", "core.js"):
        s = await client.get(f"/static/{name}")
        assert s.status == 200, name
    assert (await client.get("/static/../../.env")).status in (403, 404)
    assert (await client.get("/api/me")).status == 401  # API по-прежнему закрыт без initData


def test_no_unsafe_html_sinks():
    for name in ("app.js", "core.js", "i18n.js"):
        src = (WEBAPP / "static" / name).read_text(encoding="utf-8")
        assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function", src), name


def test_index_has_no_inline_scripts():
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)  # CSP не пустит inline-скрипт


@pytest.mark.skipif(shutil.which("node") is None, reason="нужен node только для проверки перевода (на сервере его нет)")
def test_translations_complete_and_js_parses():
    script = r"""
const fs=require('fs'),vm=require('vm'),d=process.env.WEBAPP_DIR;
const win={};vm.runInNewContext(fs.readFileSync(d+'/static/i18n.js','utf8'),{window:win});
const src=fs.readFileSync(d+'/static/app.js','utf8');
const keys=new Set([...src.matchAll(/\bt\('([a-z0-9_]+)'/g)].map(m=>m[1]));
for (const k of ['wd1','wd2','wd3','wd4','wd5','wd6','wd7','st_online','st_busy','st_error','st_offline','st_ok','st_warn','st_bad','role_client','role_assistant','role_owner','role_admin','role_user','nav_home','nav_tasks','nav_stats','nav_business','nav_more','bs_sent','bs_manual_needed','bs_skipped','bs_failed','bs_stopped','bs_blocked','bs_missed','bs_retry']) keys.add(k);
for (const k of ['bs_','wd','st_','role_']) keys.delete(k);
const miss=[];for (const l of ['ru','uz']) for (const k of keys) if(!(k in win.I18N.dicts[l])) miss.push(l+':'+k);
for (const k of Object.keys(win.I18N.dicts.ru)) if(!(k in win.I18N.dicts.uz)) miss.push('uz:'+k);
if (miss.length){console.log(miss.join(' '));process.exit(1)}
"""
    for name in ("app.js", "core.js", "i18n.js"):
        subprocess.run(["node", "--check", str(WEBAPP / "static" / name)], check=True)
    import os
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, env={**os.environ, "WEBAPP_DIR": str(WEBAPP)})
    assert out.returncode == 0, out.stdout + out.stderr
