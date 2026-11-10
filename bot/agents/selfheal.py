"""Диагностика и самолечение агентов (Кимо — любые агенты, Мимо — только созданные им).

Порядок: диагностика (логи, last_seen, failed-задания, ошибка из heartbeat) -> полный отчёт владельцу на русском
-> «да» в личке -> бэкап конфигурации в backups -> запись в audit_log -> правка -> перезапуск ТОЛЬКО этого агента
-> ожидание heartbeat до 60 с -> ping -> если не поднялся, откат из бэкапа и отчёт.

Ограничения:
- менять можно только конфигурацию проблемного агента (запись в таблице agents) и его карточку agents/<имя>.json;
  общий код (bot/**), слой доступа, шифрование, run_all.sh и базу никто из агентов не правит;
- агенты с ролью admin и creator (Кимо, Мимо) автоматически не чинятся: для них готовится описание и diff;
- не больше 2 попыток исправления одного агента за час; после этого только отчёт;
- никаких произвольных shell-команд: допустимы лишь действия этого модуля (чтение лога, отчёт, правка конфигурации
  из белого списка полей, перезапуск процесса агента, откат из бэкапа).
Значения секретов и токены в отчёты, diff и бэкапы не попадают.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from html import escape
from typing import Any

from bot.agents import procs
from bot.agents.admin_ops import AdminOps
from bot.agents.access import HARD_DENIED_TABLES, RESTRICTED_TABLES
from bot.agents.bus import BusError
from bot.agents.confirm import Confirmations, ConfirmError
from bot.agents.context import AgentContext
from bot.agents.health import effective_status, fmt_ago, reason_text, seconds_since
from bot.agents.registry import AgentConfig
from bot.ai.llm import PROVIDERS
from bot.logging_setup import redact_text
from bot.timeutil import now_utc, to_db

log = logging.getLogger(__name__)

MAX_ATTEMPTS_PER_HOUR = 2
PING_TIMEOUT = 30.0
FIX_FIELDS = {"models", "config", "allowed_tables", "allowed_commands"}  # белый список изменяемых полей
CONFIG_NUMBERS = {"task_timeout_s": 60, "taken_timeout_s": 180, "max_searches": 2}
_TRACE_FRAME = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')
_LOG_PATTERNS = [
    (re.compile(r"Нет токена для"), "не задан токен бота этого агента", "Задайте токен: python manage.py agents set-token <имя> (значение мне не показывайте)"),
    (re.compile(r"TelegramUnauthorizedError|Unauthorized"), "Telegram не принимает токен бота (он отозван или неверен)", "Выпустите новый токен в @BotFather и задайте его командой set-token"),
    (re.compile(r"GroqUnavailableError|Groq 401|auth"), "Groq отклонил запросы (ключ недействителен или сервис недоступен)", "Проверьте GROQ_API_KEY в .env"),
    (re.compile(r"database is locked"), "база данных занята другим процессом", "Закройте сторонние программы, открывшие bot.db на запись"),
    (re.compile(r"TelegramNetworkError|ConnectTimeout|ConnectError"), "нет связи с Telegram или интернетом", "Проверьте сеть телефона-сервера"),
]


class SelfHealError(ConfirmError):
    """Понятная владельцу причина отказа."""


@dataclass
class Problem:
    code: str
    text: str  # что именно не так
    where: str  # где: таблица agents, поле ...
    changes: dict[str, Any] = field(default_factory=dict)  # поле agents -> новое значение (если исправимо конфигурацией)


@dataclass
class Diagnosis:
    target: AgentConfig
    symptom: str
    problems: list[Problem]
    notes: list[tuple[str, str]]  # (причина, что делать) — найдено в логах, конфигурацией не лечится
    evidence: list[str]
    traceback_frame: str | None
    cause_found: bool

    @property
    def changes(self) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for p in self.problems:
            for key, value in p.changes.items():
                merged[key] = value
        return merged


def flat(value: Any, prefix: str = "") -> dict[str, str]:
    if isinstance(value, dict):
        out: dict[str, str] = {}
        for k, v in value.items():
            out.update(flat(v, f"{prefix}.{k}" if prefix else str(k)))
        return out or {prefix: "{}"}
    return {prefix: json.dumps(value, ensure_ascii=False)}


def make_diff(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    lines = []
    for key in after:
        old, new = flat(before.get(key), key), flat(after[key], key)
        for path in sorted(set(old) | set(new)):
            if old.get(path) != new.get(path):
                lines.append(f"- {path}: {redact_text(old.get(path, '—'))}")
                lines.append(f"+ {path}: {redact_text(new.get(path, '—'))}")
    return lines


def validate_row(cfg: AgentConfig, default_model: str | None) -> list[Problem]:
    """Проверка конфигурации агента: что мешает ему запуститься или работать."""
    problems: list[Problem] = []
    models = dict(cfg.models)
    if not models.get("text"):
        fixed = {**models, "text": default_model or "openai/gpt-oss-120b"}
        problems.append(Problem("no_text_model", "не задана текстовая модель — агент не может вызывать ИИ и не стартует",
                                "таблица agents, поле models.text", {"models": fixed}))
    if str(models.get("provider") or "groq").lower() not in PROVIDERS:
        fixed = {**models, "provider": "groq"}
        if not fixed.get("text"):
            fixed["text"] = default_model or "openai/gpt-oss-120b"
        problems.append(Problem("bad_provider", f"неизвестный провайдер «{models.get('provider')}» — доступен только groq",
                                "таблица agents, поле models.provider", {"models": fixed}))
    config = dict(cfg.config)
    bad_numbers = {k: CONFIG_NUMBERS[k] for k in CONFIG_NUMBERS if k in config and not _positive_number(config[k])}
    if bad_numbers:
        problems.append(Problem("bad_numbers", "некорректные числовые настройки: " + ", ".join(bad_numbers),
                                "таблица agents, поле config", {"config": {**config, **bad_numbers}}))
    forbidden = [t for t in cfg.allowed_tables if t in HARD_DENIED_TABLES or (t in RESTRICTED_TABLES and cfg.access_role != "admin")]
    if forbidden:
        problems.append(Problem("forbidden_tables", "в разрешённых таблицах есть закрытые: " + ", ".join(forbidden),
                                "таблица agents, поле allowed_tables",
                                {"allowed_tables": [t for t in cfg.allowed_tables if t not in forbidden]}))
    return problems


def _positive_number(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def last_frame(lines: list[str]) -> str | None:
    """Последний кадр traceback из проекта: файл:строка, функция."""
    found = None
    for line in lines:
        m = _TRACE_FRAME.search(line)
        if m:
            path = m.group(1)
            if "site-packages" in path or "/usr/lib" in path or "/usr/local/lib" in path:
                continue
            rel = path.split("nexio-aasistant/")[-1] if "nexio-aasistant/" in path else os.path.basename(path)
            found = f"{rel}:{m.group(2)} ({m.group(3)})"
    return found


class SelfHeal:
    def __init__(self, ctx: AgentContext, ops: AdminOps, confirm: Confirmations,
                 online_timeout: float = procs.ONLINE_TIMEOUT, ping_timeout: float = PING_TIMEOUT) -> None:
        self.ctx = ctx
        self.ops = ops
        self.confirm = confirm
        self.db = ctx.system_db
        self.online_timeout = online_timeout
        self.ping_timeout = ping_timeout
        confirm.executors["apply_fix"] = self.apply

    # ---------- защита ----------

    def protected(self, target: AgentConfig) -> bool:
        """Агенты admin/creator (Кимо, Мимо) и сам актор автоматически не правятся."""
        return target.access_role in ("admin", "creator") or target.name == self.ctx.agent.name

    async def attempts_last_hour(self, target: str) -> int:
        since = to_db(now_utc() - timedelta(hours=1))
        rows = await self.db.audit_list(limit=100, target=target, action="selfheal_apply", since=since)
        return len(rows)

    # ---------- диагностика ----------

    async def diagnose(self, target_query: str) -> tuple[Diagnosis, str]:
        """Собрать факты и подготовить полный отчёт. Если есть безопасная правка конфигурации, она уходит на подтверждение."""
        found = await self.ctx.registry.find(target_query)
        if found is None:
            raise SelfHealError("Не нашёл такого агента.")
        target = await self.ops.scope(found.name, "diagnose")  # Кимо: любой агент; Мимо: только созданные им
        now = now_utc()
        status = effective_status(target.last_seen, target.status, now)
        evidence = [
            f"heartbeat: {status}, last_seen {fmt_ago(seconds_since(target.last_seen, now))}",
            f"последняя ошибка из heartbeat: {redact_text(target.last_error) if target.last_error else 'нет'}",
        ]
        failed = [t for t in await self.db.queue_recent_failed(5, target.name)]
        evidence.append(f"неудачных заданий в очереди (последние): {len(failed)}")
        recent = await self.ops.read_log(target.name, None, 200)
        marks = ("ERROR", "CRITICAL", "Traceback", "Error:", "Exception")
        err_lines = [ln for ln in recent if any(m in ln for m in marks)][-10:]
        evidence.append(f"строк с ошибками в logs/{target.name}.log: {len(err_lines)}")
        evidence.append("конфигурация агента в таблице agents (модели, провайдер, таблицы, настройки)")

        default_model = self.ctx.agent.text_model
        problems = validate_row(target, default_model)
        text_blob = "\n".join(err_lines + [target.last_error or ""] + [(t["result"] or "") for t in failed])
        notes = [(why, todo) for pattern, why, todo in _LOG_PATTERNS if pattern.search(text_blob)]
        frame = last_frame(recent)  # последний кадр traceback из кода проекта: файл:строка
        symptom = reason_text(target.display_name, target.last_seen, target.status, target.last_error, target.status_detail, now)
        diagnosis = Diagnosis(target, symptom, problems, notes, evidence, frame, bool(problems or notes or frame))
        await self.ops._audit("diagnose", target.name, {"problems": [p.code for p in problems], "notes": len(notes)})
        text = await self._report_and_propose(diagnosis)
        return diagnosis, text

    async def _report_and_propose(self, d: Diagnosis) -> str:
        target = d.target
        changes = {k: v for k, v in d.changes.items() if k in FIX_FIELDS}
        diff = make_diff({k: getattr(target, k) if k != "models" else target.models for k in changes}, changes) if changes else []
        reason_no_apply = None
        if changes and self.protected(target):
            reason_no_apply = ("это Кимо или Мимо (роль admin/creator): автоматически я их не чиню. Готов diff, "
                               "применить его должен владелец вручную")
        elif changes and await self.attempts_last_hour(target.name) >= MAX_ATTEMPTS_PER_HOUR:
            reason_no_apply = f"достигнут лимит {MAX_ATTEMPTS_PER_HOUR} попыток исправления этого агента за час: только отчёт"
        report = self._format(d, diff, reason_no_apply)
        if not changes or reason_no_apply:
            return report
        params = {"changes": changes, "before": {k: (target.models if k == "models" else getattr(target, k)) for k in changes},
                  "diff": diff}
        proposal = await self.confirm.propose_raw("apply_fix", target.name, params, report[:3300])
        sent = await self.confirm.send_confirmation(self.ctx.admin.bot, proposal) if self.ctx.admin else False
        tail = (f"\n\n📨 Полный отчёт #{proposal.id} и план правки отправил вам в личку, жду «да»."
                if sent else f"\n\nПлан правки #{proposal.id} готов, но не смог написать в личку: нажмите /start у меня.")
        return report + tail

    def _format(self, d: Diagnosis, diff: list[str], blocked: str | None) -> str:
        t = d.target
        lines = [f"🔎 <b>Диагностика: {escape(t.display_name)} ({t.name})</b>", "", "<b>Что сломалось</b>", f"Симптом: {escape(d.symptom)}."]
        if d.problems:
            for p in d.problems:
                lines.append(f"Причина: {escape(p.text)}. Где: {escape(p.where)}.")
        for why, todo in d.notes:
            lines.append(f"Причина по логам: {escape(why)}. Что делать: {escape(todo)}.")
        if d.traceback_frame:
            lines.append(f"Место ошибки в коде: {escape(d.traceback_frame)} (общий код, я его не меняю).")
        if not d.cause_found:
            lines.append("Причину найти не удалось: в логах и конфигурации явных признаков нет. Ничего не выдумываю и не меняю.")
        lines += ["", "<b>Как определил</b>"] + [f"• {escape(e)}" for e in d.evidence]
        if diff:
            lines += ["", "<b>Что хочу изменить</b>", "Только конфигурацию этого агента (таблица agents):", "<pre>" + escape("\n".join(diff)) + "</pre>"]
            lines += ["<b>Зачем</b>: убрать причину, из-за которой агент не стартует или не работает.",
                      "<b>Что может затронуть</b>: только этого агента; перезапущу его процесс, остальные не затрагиваются. Бэкап прежней конфигурации сохраню в backups.",
                      f"<b>Как проверю</b>: дождусь heartbeat до {int(self.online_timeout)} с и отправлю тестовое задание ping. "
                      "Если агент не поднимется или ping не пройдёт — верну прежнюю конфигурацию и запущу на ней."]
        if blocked:
            lines += ["", f"⛔ Правка не применяется: {escape(blocked)}."]
        return "\n".join(lines)

    # ---------- применение после «да» ----------

    async def apply(self, row: dict[str, Any], cfg: AgentConfig, confirm: Confirmations) -> tuple[str, dict[str, Any]]:
        target = cfg.name
        params = row["params"]
        changes = {k: v for k, v in params["changes"].items() if k in FIX_FIELDS}  # белый список полей
        if not changes:
            raise SelfHealError("В плане нет допустимых изменений")
        if self.protected(cfg):
            raise SelfHealError("Кимо и Мимо автоматически не правятся: примените diff вручную")
        if await self.attempts_last_hour(target) >= MAX_ATTEMPTS_PER_HOUR:
            raise SelfHealError(f"Лимит {MAX_ATTEMPTS_PER_HOUR} попытки исправления за час исчерпан")
        report_id = row["id"]
        backup = await self._backup(cfg)
        await self.db.audit(to_db(now_utc()), self.ctx.agent.name, "selfheal_apply", target,
                            {"backup": backup.name, "fields": sorted(changes), "diff": "; ".join(params.get("diff", []))[:500]},
                            "started", confirmed_by=confirm.owner_id, report_id=report_id)
        await self.db.update_agent(target, **changes)
        ok, why = await self._restart_and_check(target)
        if ok:
            await self.db.audit(to_db(now_utc()), self.ctx.agent.name, "selfheal_ok", target, {"backup": backup.name},
                                "ok", confirmed_by=confirm.owner_id, report_id=report_id)
            return f"✅ {cfg.display_name}: исправление #{report_id} применено, агент online, ping прошёл.", {"report": report_id, "backup": backup.name}
        # откат: прежняя конфигурация из бэкапа, запуск на старой версии
        await self._restore(target, backup)
        back_ok, back_why = await self._restart_and_check(target)
        await self.db.audit(to_db(now_utc()), self.ctx.agent.name, "selfheal_rollback", target,
                            {"backup": backup.name, "why": why, "back_online": back_ok}, "rolled_back",
                            confirmed_by=confirm.owner_id, report_id=report_id)
        state = "запущен на прежней конфигурации" if back_ok else f"на прежней конфигурации тоже не поднялся ({back_why})"
        return (f"↩️ {cfg.display_name}: исправление #{report_id} не сработало — {why}.\n"
                f"Прежняя конфигурация возвращена из бэкапа {backup.name}, агент {state}. "
                "Нужен ручной разбор: пришлите «покажи логи» или проверьте токен и сеть."), {
            "report": report_id, "rolled_back": True, "why": why}

    async def _restart_and_check(self, target: str) -> tuple[bool, str]:
        control = procs.get_control()
        await control.stop(target)
        since = to_db(now_utc())
        started, message = await control.start(target)
        if not started:
            return False, f"не удалось запустить процесс: {redact_text(message)[:120]}"
        if not await procs.wait_online(self.db, target, since, self.online_timeout):
            return False, f"heartbeat не пришёл за {int(self.online_timeout)} с"
        try:
            pong = await self.ctx.bus.request(target, {"type": "ping"}, timeout=self.ping_timeout)
        except BusError as exc:
            return False, f"ping не прошёл: {redact_text(str(exc))[:120]}"
        return (pong == "pong"), ("ping вернул неожиданный ответ" if pong != "pong" else "ok")

    # ---------- бэкап и откат ----------

    async def _backup(self, cfg: AgentConfig) -> Any:
        """Бэкап изменяемой конфигурации в backups: agent-<имя>-<дата>.json (без секретов)."""
        directory = self.db.backup_dir()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = now_utc().strftime("%Y%m%d-%H%M%S")
        path = directory / f"agent-{cfg.name}-{stamp}.json"
        n = 1
        while path.exists():
            n += 1
            path = directory / f"agent-{cfg.name}-{stamp}-{n}.json"
        snapshot = {k: getattr(cfg, k) for k in ("models", "config", "allowed_tables", "allowed_commands")}
        path.write_text(redact_text(json.dumps(snapshot, ensure_ascii=False, indent=2)), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path

    async def _restore(self, target: str, backup: Any) -> None:
        snapshot = json.loads(backup.read_text(encoding="utf-8"))
        await self.db.update_agent(target, **{k: v for k, v in snapshot.items() if k in FIX_FIELDS})
