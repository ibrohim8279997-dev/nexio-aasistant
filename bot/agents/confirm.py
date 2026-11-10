"""Подтверждения владельца («да» в личке от ADMIN_ID) для опасных действий над агентами.

Любое изменение (старт, остановка, перезапуск, имя, роль, промпт, модели, таблицы, команды, запись в чужую таблицу)
сначала записывается в pending_actions, владельцу в личку уходит описание с кнопками «Да»/«Нет». Выполняется только
после «да» от ADMIN_ID в личке; перед выполнением права актора проверяются заново. Расширение прав (новые таблицы,
новые команды) — всегда отдельное подтверждение. Каждое действие пишется в audit_log: кто, что, когда, над кем.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from html import escape
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from bot.agents import procs
from bot.agents.admin_ops import AdminError, AdminOps
from bot.agents.cards import GRANTABLE_TABLES
from bot.agents.registry import AgentConfig, norm_name
from bot.ai.llm import PROVIDERS
from bot.db import Database
from bot.keyboards import confirm_kb
from bot.logging_setup import redact_text
from bot.timeutil import now_utc, to_db

log = logging.getLogger(__name__)

CONFIRM_TTL = timedelta(minutes=15)
EXPANDING = {"grant_tables", "grant_commands"}  # расширение прав: отдельное «да», не объединяется с другими
BASE_COMMANDS = {"start", "help"}
YES_WORDS = {"да", "yes", "ок", "подтверждаю", "давай"}
NO_WORDS = {"нет", "no", "отмена", "не надо"}
ACTION_TITLES = {
    "start": "Запустить", "stop": "Остановить", "restart": "Перезапустить",
    "set_display_name": "Изменить имя", "set_role_description": "Изменить описание роли",
    "set_prompt": "Изменить системный промпт", "set_models": "Изменить модели",
    "grant_tables": "РАСШИРЕНИЕ ПРАВ: дать доступ к таблицам", "revoke_tables": "Отозвать доступ к таблицам",
    "grant_commands": "РАСШИРЕНИЕ ПРАВ: разрешить команды", "revoke_commands": "Запретить команды",
    "write_row": "Записать в чужую таблицу", "apply_fix": "Применить исправление",
}


class ConfirmError(Exception):
    """Понятная владельцу причина отказа (неверные параметры, чужое подтверждение, истёк срок)."""


@dataclass
class Proposal:
    id: int
    summary: str
    expands: bool


def clean_display_name(value: Any) -> str:
    name = re.sub(r"\s+", " ", str(value or "")).strip()
    if not 2 <= len(name) <= 30 or len(name.split()) > 3 or re.search(r"[,;:!?()\[\]{}<>]", name):
        raise ConfirmError("Имя должно быть коротким: до 30 символов, не больше трёх слов, без знаков препинания.")
    return name


def _text(value: Any, low: int, high: int, what: str) -> str:
    text = str(value or "").strip()
    if not low <= len(text) <= high:
        raise ConfirmError(f"{what}: от {low} до {high} символов.")
    return text


def _short(value: Any, limit: int = 300) -> str:
    return redact_text(str(value))[:limit]


class Confirmations:
    def __init__(self, db: Database, actor: str, ops: AdminOps, owner_id: int,
                 online_timeout: float = procs.ONLINE_TIMEOUT) -> None:
        self._db = db
        self.actor = actor
        self.ops = ops
        self.owner_id = owner_id
        self.online_timeout = online_timeout

    # ---------- предложение ----------

    async def propose(self, action: str, target: str, params: dict[str, Any] | None = None,
                      chat_id: int | None = None) -> Proposal:
        if action not in ACTION_TITLES:
            raise ConfirmError("Неизвестное действие")
        cfg = await self.ops.scope(target, f"propose_{action}")  # права актора на цель (admin / creator)
        if action == "write_row":
            await self.ops.require_admin("propose_write_row")
        clean = await self._validate(action, cfg, params or {})
        summary = await self._summary(action, cfg, clean)
        now = now_utc()
        pid = await self._db.pending_create(
            self.actor, action, target, clean, summary, to_db(now), to_db(now + CONFIRM_TTL), chat_id,
            expands_rights=action in EXPANDING,
        )
        await self.ops._audit("propose", target, {"pending": pid, "action": action, "params": _short(clean, 500)})
        return Proposal(pid, summary, action in EXPANDING)

    async def _validate(self, action: str, cfg: AgentConfig, params: dict[str, Any]) -> dict[str, Any]:
        if action in ("stop", "restart") and cfg.name == self.actor:
            raise ConfirmError("Свой процесс я сам не останавливаю и не перезапускаю: это делает владелец через run.sh.")
        if action == "start" and (cfg.config.get("draft") or cfg.config.get("awaiting_token") or cfg.config.get("awaiting_name")):
            raise ConfirmError("Агент ещё не готов: нужны подтверждённое имя и токен бота.")
        if action in ("start", "stop", "restart"):
            return {}
        if action == "set_display_name":
            name = clean_display_name(params.get("value"))
            for other in await self._db.list_agents():
                if other["name"] != cfg.name and norm_name(name) in (norm_name(other["name"]), norm_name(other.get("display_name"))):
                    raise ConfirmError(f"Имя «{name}» уже занято агентом {other['name']}.")
            return {"value": name}
        if action == "set_role_description":
            return {"value": _text(params.get("value"), 5, 500, "Описание роли")}
        if action == "set_prompt":
            return {"value": _text(params.get("value"), 5, 3000, "Системный промпт")}
        if action == "set_models":
            return {"value": self._clean_models(cfg, params.get("value"))}
        if action in ("grant_tables", "revoke_tables"):
            tables = self._as_list(params.get("value"))
            if action == "grant_tables":
                bad = [t for t in tables if t not in GRANTABLE_TABLES]
                if bad or not tables:
                    raise ConfirmError(f"Выдать можно только: {', '.join(sorted(GRANTABLE_TABLES))}. Расходы, платежи и секреты недоступны никому, кроме Кимо.")
            elif not tables:
                raise ConfirmError("Укажите таблицы")
            return {"value": tables}
        if action in ("grant_commands", "revoke_commands"):
            commands = [c.lstrip("/").lower() for c in self._as_list(params.get("value"))]
            if not commands or any(not re.fullmatch(r"[a-z0-9_]{1,32}", c) for c in commands):
                raise ConfirmError("Команды: латиница, цифры и _ (например: report, find)")
            if action == "revoke_commands" and set(commands) & BASE_COMMANDS:
                raise ConfirmError("Команды start и help отключить нельзя")
            return {"value": commands}
        if action == "write_row":
            return await self._clean_write(cfg, params)
        raise ConfirmError("Это действие готовится отдельным отчётом")

    @staticmethod
    def _as_list(value: Any) -> list[str]:
        if isinstance(value, str):
            value = re.split(r"[,\s]+", value)
        return [str(v).strip() for v in (value or []) if str(v).strip()][:15]

    @staticmethod
    def _clean_models(cfg: AgentConfig, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ConfirmError("Модели: нужен набор provider, text, stt, search")
        models = dict(cfg.models)
        if "provider" in value:
            if str(value["provider"]).lower() not in PROVIDERS:
                raise ConfirmError(f"Провайдер: {', '.join(PROVIDERS)}")
            models["provider"] = str(value["provider"]).lower()
        for key in ("text", "stt"):
            if key in value:
                v = value[key]
                if v is not None and not re.fullmatch(r"[\w./:-]{2,80}", str(v)):
                    raise ConfirmError(f"Некорректное название модели {key}")
                models[key] = v
        if "search" in value:
            models["search"] = bool(value["search"])
        if not models.get("text"):
            raise ConfirmError("Текстовая модель обязательна")
        return models

    async def _clean_write(self, cfg: AgentConfig, params: dict[str, Any]) -> dict[str, Any]:
        table, column = str(params.get("table", "")), str(params.get("column", ""))
        try:
            row_id = int(params.get("row_id"))
        except (TypeError, ValueError):
            raise ConfirmError("row_id: целое число") from None
        if not table.startswith("agent_"):
            raise ConfirmError("Записывать можно только в таблицы агентов (agent_*): основные таблицы защищены")
        tables = {t.name: t for t in await self.ops.list_tables(cfg.name)}
        info = tables.get(table)
        if info is None or column not in info.columns or re.search(r"token|secret|password|encrypted", column, re.I):
            raise ConfirmError("Нет такой таблицы или колонки у этого агента")
        value = params.get("value")
        if not isinstance(value, (str, int, float)) and value is not None:
            raise ConfirmError("Значение: текст или число")
        if isinstance(value, str) and len(value) > 500:
            raise ConfirmError("Значение длиннее 500 символов")
        return {"table": table, "row_id": row_id, "column": column, "value": value}

    async def _summary(self, action: str, cfg: AgentConfig, p: dict[str, Any]) -> str:
        who = f"{cfg.display_name} ({cfg.name})"
        title = ACTION_TITLES[action]
        if action in ("start", "stop", "restart"):
            extra = " Остальные агенты не затрагиваются." if action != "start" else ""
            return f"{title} агента {who}.{extra}"
        if action == "set_display_name":
            return f"{title} агента {cfg.name}: «{cfg.display_name}» → «{p['value']}»"
        if action in ("set_role_description", "set_prompt"):
            return f"{title} агента {who}.\nБыло: {_short(cfg.system_prompt if action == 'set_prompt' else cfg.role_description, 200)}\nСтанет: {_short(p['value'], 400)}"
        if action == "set_models":
            return f"{title} агента {who}.\nБыло: {cfg.models}\nСтанет: {p['value']}"
        if action in ("grant_tables", "revoke_tables", "grant_commands", "revoke_commands"):
            return f"{title} {', '.join(p['value'])} — агент {who}.\nСейчас таблицы: {', '.join(cfg.allowed_tables) or 'нет'}; команды: {', '.join(cfg.allowed_commands) or 'нет'}"
        if action == "write_row":
            return f"{title}: {p['table']} (строка {p['row_id']}), колонка {p['column']} → {_short(p['value'], 200)!r} — агент {who}"
        return f"{title}: {who}"

    # ---------- отправка владельцу ----------

    async def send_confirmation(self, bot: Bot, proposal: Proposal) -> bool:
        """Подтверждение уходит владельцу только в личку, даже если просьба пришла из группы."""
        mark = "⚠️ " if proposal.expands else "❓ "
        text = f"{mark}<b>Подтвердите действие #{proposal.id}</b>\n{escape(proposal.summary)}\n\nСрок: 15 минут."
        try:
            await bot.send_message(self.owner_id, text, reply_markup=confirm_kb(proposal.id), parse_mode="HTML")
            return True
        except TelegramAPIError as exc:
            log.warning("Не удалось отправить подтверждение владельцу: %s", type(exc).__name__)
            return False

    async def open_pending(self) -> list[dict[str, Any]]:
        return await self._db.pending_open(self.actor, to_db(now_utc()))

    # ---------- решение ----------

    async def decide(self, pending_id: int, yes: bool, user_id: int, private: bool) -> str:
        if user_id != self.owner_id or not private:
            raise ConfirmError("Подтверждать действия может только владелец и только в личке.")
        row = await self._db.pending_get(pending_id)
        if row is None or row["actor"] != self.actor:
            raise ConfirmError("Это подтверждение относится к другому агенту или не найдено.")
        now = to_db(now_utc())
        if not yes:
            if await self._db.pending_take(pending_id, "rejected", now):
                await self.ops._audit("reject", row["target"], {"pending": pending_id, "action": row["action"]}, "rejected")
                return "Отменено. Ничего не изменено."
            raise ConfirmError("Запрос уже обработан или истёк.")
        if not await self._db.pending_take(pending_id, "confirmed", now):
            raise ConfirmError("Запрос уже обработан или истёк (срок 15 минут).")
        try:
            result, details = await self._execute(row)
            status = "done"
        except (AdminError, ConfirmError, ValueError) as exc:
            result, details, status = f"Не выполнено: {exc}", {"error": str(exc)}, "failed"
        except Exception:  # noqa: BLE001
            log.exception("Подтверждённое действие #%s упало", pending_id)
            result, details, status = "Не выполнено: внутренняя ошибка, подробности в логе.", {"error": "internal"}, "failed"
        await self._db.pending_finish(pending_id, status, _short(result, 500))
        safe = {k: (_short(v) if isinstance(v, str) else v) for k, v in {"pending": pending_id, **details}.items()}
        await self._db.audit(
            now, self.actor, row["action"], row["target"], safe, "ok" if status == "done" else "failed",
            confirmed_by=user_id, report_id=pending_id,
        )
        return result

    # ---------- выполнение (права проверяются заново) ----------

    async def _execute(self, row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        action, target, p = row["action"], row["target"], row["params"]
        cfg = await self.ops.scope(target, f"execute_{action}")  # роль актора могла измениться после предложения
        who = f"{cfg.display_name} ({cfg.name})"
        db = self._db
        if action == "stop":
            await db.update_agent(target, enabled=False)
            await procs.get_control().stop(target)
            return f"⏸ Агент {who} остановлен.", {"agent": target}
        if action in ("start", "restart"):
            if action == "restart":
                await procs.get_control().stop(target)
            await db.update_agent(target, enabled=True)
            since = to_db(now_utc())
            ok, message = await procs.get_control().start(target)
            if not ok:
                return f"⚠️ Не удалось запустить {who}: {_short(message)}", {"agent": target, "started": False}
            online = await procs.wait_online(db, target, since, self.online_timeout)
            verb = "перезапущен" if action == "restart" else "запущен"
            return (f"✅ Агент {who} {verb} и онлайн." if online else
                    f"⚠️ Агент {who} {verb}, но heartbeat не пришёл за {int(self.online_timeout)} с. Проверьте лог."), {"agent": target, "online": online}
        if action == "set_display_name":
            await db.update_agent(target, display_name=p["value"])
            details = {"before": cfg.display_name, "after": p["value"]}
            if cfg.config.get("awaiting_name"):  # новый агент: имя подтверждено, дальше токен
                await db.update_agent(target, config={**cfg.config, "awaiting_name": False, "awaiting_token": True})
                return (f"✅ Имя подтверждено: «{p['value']}».\n🔑 Теперь создайте бота в @BotFather и пришлите токен мне в личку: "
                        "сообщение с токеном я удалю, а токен сохраню зашифрованным."), details
            return f"✅ Имя изменено: «{cfg.display_name}» → «{p['value']}».", details
        if action == "set_role_description":
            await db.update_agent(target, role_description=p["value"], role=p["value"])
            return f"✅ Описание роли {who} обновлено.", {"before": _short(cfg.role_description), "after": _short(p["value"])}
        if action == "set_prompt":
            await db.update_agent(target, system_prompt=p["value"])
            return f"✅ Системный промпт {who} обновлён. Перезапустите агента, чтобы он применился.", {"before": _short(cfg.system_prompt), "after": _short(p["value"])}
        if action == "set_models":
            await db.update_agent(target, models=p["value"])
            return f"✅ Модели {who} обновлены. Перезапустите агента.", {"before": str(cfg.models), "after": str(p["value"])}
        if action in ("grant_tables", "revoke_tables"):
            current = list(cfg.allowed_tables)
            new = sorted(set(current) | set(p["value"])) if action == "grant_tables" else [t for t in current if t not in p["value"]]
            await db.update_agent(target, allowed_tables=new)
            return f"✅ Таблицы {who}: {', '.join(new) or 'нет доступа'}.", {"before": current, "after": new}
        if action in ("grant_commands", "revoke_commands"):
            current = list(cfg.allowed_commands)
            new = sorted(set(current) | set(p["value"])) if action == "grant_commands" else [c for c in current if c not in p["value"]]
            await db.update_agent(target, allowed_commands=new)
            return f"✅ Команды {who}: {', '.join(new)}.", {"before": current, "after": new}
        if action == "write_row":
            await self.ops.require_admin("execute_write_row")
            ok = await db.table_update_row(p["table"], p["row_id"], p["column"], p["value"])
            return ("✅ Запись изменена." if ok else "Строка не найдена."), {"table": p["table"], "row": p["row_id"], "column": p["column"]}
        raise ConfirmError("Это действие выполняется другим сервисом")
