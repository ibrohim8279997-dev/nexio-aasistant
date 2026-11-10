"""Промпт для модели и строгая проверка JSON-действий перед выполнением."""
from __future__ import annotations

import json
import re
import datetime as dt
from datetime import date, timedelta
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, field_validator

MAX_ACTIONS = 10
MAX_TASK_LEN = 500
MAX_CATEGORY_LEN = 50
MAX_NOTE_LEN = 200
MAX_AMOUNT = {"UZS": 10_000_000_000.0, "USD": 1_000_000.0}

_CURRENCY_ALIASES = {
    "uzs": "UZS", "sum": "UZS", "сум": "UZS", "сумов": "UZS", "so'm": "UZS", "som": "UZS", "сўм": "UZS",
    "usd": "USD", "$": "USD", "доллар": "USD", "долларов": "USD", "dollar": "USD", "долл": "USD",
}


class ActionParseError(Exception):
    """Ответ модели не удалось превратить в допустимые действия."""


class _Base(BaseModel):
    model_config = {"extra": "ignore", "str_strip_whitespace": True}


class AddTask(_Base):
    type: Literal["add_task"]
    text: str = Field(min_length=1, max_length=MAX_TASK_LEN)
    date: dt.date | None = None
    time: dt.time | None = None


class AddExpense(_Base):
    type: Literal["add_expense"]
    amount: float = Field(gt=0)
    currency: Literal["UZS", "USD"] = "UZS"
    category: str = Field(default="Другое", min_length=1, max_length=MAX_CATEGORY_LEN)
    note: str | None = Field(default=None, max_length=MAX_NOTE_LEN)

    @field_validator("currency", mode="before")
    @classmethod
    def _norm_currency(cls, v: Any) -> Any:
        if v is None or v == "":
            return "UZS"
        if isinstance(v, str):
            return _CURRENCY_ALIASES.get(v.strip().lower(), v.strip().upper())
        return v

    @field_validator("category", mode="before")
    @classmethod
    def _norm_category(cls, v: Any) -> Any:
        if not v:
            return "Другое"
        v = str(v).strip()
        return v[:1].upper() + v[1:]

    @field_validator("note", mode="before")
    @classmethod
    def _empty_note(cls, v: Any) -> Any:
        return v or None


class CompleteTask(_Base):
    type: Literal["complete_task"]
    task_id: int = Field(gt=0)


class ListTasks(_Base):
    type: Literal["list_tasks"]
    day: Literal["today", "tomorrow"] = "today"


class ShowStats(_Base):
    type: Literal["show_stats"]
    topic: Literal["expenses", "tasks"] = "expenses"
    period: Literal["day", "week", "month"] = "day"


class Reply(_Base):
    type: Literal["reply"]
    text: str = Field(min_length=1, max_length=3000)


class ListReminders(_Base):
    type: Literal["list_reminders"]


class Delegate(_Base):
    """Передать задачу другому агенту (диспетчер)."""

    type: Literal["delegate"]
    agent: str = Field(min_length=1, max_length=32)
    task: str = Field(min_length=1, max_length=1500)


class Clarify(_Base):
    """Неясно, чья задача, — один короткий вопрос владельцу."""

    type: Literal["clarify"]
    question: str = Field(min_length=1, max_length=300)


ManageOp = Literal[
    "start", "stop", "restart", "rename", "set_role", "set_prompt", "set_models",
    "grant_tables", "revoke_tables", "grant_commands", "revoke_commands",
]


class ManageAgent(_Base):
    """Управление агентом (только владелец; исполняется после его «да» в личке)."""

    type: Literal["manage_agent"]
    agent: str = Field(min_length=1, max_length=64)
    op: ManageOp
    value: Any = None


class AgentsReport(_Base):
    """«Кто работает»: статусы агентов, проблемы, очередь заданий."""

    type: Literal["agents_report"]


class AgentLogs(_Base):
    type: Literal["agent_logs"]
    agent: str = Field(min_length=1, max_length=64)
    minutes: int | None = Field(default=None, ge=1, le=1440)


class AgentTables(_Base):
    type: Literal["agent_tables"]
    agent: str | None = Field(default=None, max_length=64)  # None — вся структура БД (только admin)


class AgentData(_Base):
    type: Literal["agent_data"]
    agent: str = Field(min_length=1, max_length=64)
    table: str = Field(min_length=1, max_length=80)
    limit: int = Field(default=10, ge=1, le=50)


class DiagnoseAgent(_Base):
    """Диагностика агента: полный отчёт; исправление применяется только после «да» владельца."""

    type: Literal["diagnose_agent"]
    agent: str = Field(min_length=1, max_length=64)


class CreateAgent(_Base):
    type: Literal["create_agent"]
    description: str = Field(min_length=10, max_length=3000)


Action = Annotated[
    Union[
        AddTask, AddExpense, CompleteTask, ListTasks, ShowStats, Reply, ListReminders, Delegate, Clarify, CreateAgent,
        ManageAgent, AgentsReport, AgentLogs, AgentTables, AgentData, DiagnoseAgent,
    ],
    Field(discriminator="type"),
]
DISPATCH_TYPES = (Delegate, CreateAgent)
ADMIN_TYPES: tuple[type, ...] = (ManageAgent, AgentLogs, AgentTables, AgentData, DiagnoseAgent)  # только от владельца, не из шины
_ACTION_ADAPTER: TypeAdapter[Any] = TypeAdapter(Action)


def _norm(value: str) -> str:
    return re.sub(r"[\s\"'«»“”.,:;!?()]+", " ", (value or "").casefold()).strip()


def _extract_json(raw: str) -> Any:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ActionParseError("в ответе нет JSON")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ActionParseError("некорректный JSON") from exc


def parse_actions(raw: str, today: date, agents: set[str] | None = None) -> list[Any]:
    """Строка от модели -> список проверенных действий. Невалидные действия отбрасываются.

    agents — системные имена агентов, которым можно передавать задачи. None — передача запрещена
    (например, запрос пришёл из шины от другого агента).
    """
    data = _extract_json(raw)
    if isinstance(data, dict) and "actions" in data:
        items = data["actions"]
    elif isinstance(data, dict) and "type" in data:
        items = [data]
    elif isinstance(data, list):
        items = data
    else:
        raise ActionParseError("нет поля actions")
    if not isinstance(items, list):
        raise ActionParseError("actions — не список")

    actions: list[Any] = []
    for item in items[:MAX_ACTIONS]:
        try:
            action = _ACTION_ADAPTER.validate_python(item)
        except ValidationError:
            continue
        if isinstance(action, AddTask) and action.date is not None:
            if not (today - timedelta(days=1) <= action.date <= today + timedelta(days=5 * 366)):
                continue
        if isinstance(action, AddExpense) and action.amount > MAX_AMOUNT[action.currency]:
            continue
        if isinstance(action, Delegate) and (agents is None or _norm(action.agent) not in {_norm(a) for a in agents}):
            continue
        if isinstance(action, CreateAgent) and agents is None:
            continue
        if isinstance(action, ADMIN_TYPES) and agents is None:
            continue
        actions.append(action)
    if not actions and items:
        raise ActionParseError("ни одно действие не прошло проверку")
    return actions


SYSTEM_PROMPT = """{identity}

Ты работаешь через действия: превращай сообщение владельца в действия.
Отвечай ТОЛЬКО JSON-объектом: {{"actions": [ ... ]}}. Никакого текста вне JSON.

Сейчас: {now}.

Типы действий:
1. {{"type":"add_task","text":"<что сделать>","date":"YYYY-MM-DD"|null,"time":"HH:MM"|null}}
2. {{"type":"add_expense","amount":<число>,"currency":"UZS"|"USD","category":"<категория>","note":"<заметка>"|null}}
3. {{"type":"complete_task","task_id":<id из списка открытых задач>}}
4. {{"type":"list_tasks","day":"today"|"tomorrow"}}
5. {{"type":"show_stats","topic":"expenses"|"tasks","period":"day"|"week"|"month"}}
6. {{"type":"reply","text":"<короткий ответ>"}}
7. {{"type":"list_reminders"}} — ближайшие напоминания (задачи со временем)
8. {{"type":"delegate","agent":"<системное имя агента>","task":"<полная формулировка задачи>"}}
9. {{"type":"clarify","question":"<один короткий вопрос>"}}
10. {{"type":"create_agent","description":"<полное описание нужного агента: задачи, что можно, что нельзя, конфиденциальность>"}}
11a. {{"type":"agents_report"}} — «кто работает», статусы и проблемы агентов
11b. {{"type":"agent_logs","agent":"<имя>","minutes":<N или null>}} — логи агента; 11c. {{"type":"agent_tables","agent":"<имя>"|null}} — структура таблиц; 11d. {{"type":"agent_data","agent":"<имя>","table":"<таблица>","limit":10}}
11e. {{"type":"diagnose_agent","agent":"<имя>"}} — найти причину сбоя агента и подготовить исправление (применится только после «да»)
11. {{"type":"manage_agent","agent":"<имя или системное имя>","op":"start|stop|restart|rename|set_role|set_prompt|set_models|grant_tables|revoke_tables|grant_commands|revoke_commands","value":<строка или список, если нужно>}} — управление агентом; владелец подтвердит в личке

Правила:
- Даты «сегодня», «завтра», «послезавтра», «в пятницу», «через 2 часа», «bugun», «ertaga» переводи в date и time по текущему времени Ташкента.
- Время не названо — time = null. Названо только время — date = сегодня, а если это время уже прошло — завтра.
- text задачи — коротко, без слов про дату и время, на языке пользователя.
- Суммы: «25 тысяч» = 25000, «1,5 млн» = 1500000. Валюта по умолчанию UZS. Доллары, $, dollar — USD.
- category — одно слово на русском: Еда, Транспорт, Покупки, Жильё, Связь, Здоровье, Развлечения, Образование, Подарки, Другое.
- Пользователь говорит, что сделал задачу — найди её id в списке открытых задач и верни complete_task. Не нашёл — reply с вопросом.
- В одном сообщении может быть несколько действий.
- Если сообщение не про задачи, расходы или статистику — верни reply с коротким полезным ответом на языке пользователя (русский или узбекский).
- Не выдумывай данные. Сомневаешься — reply с уточняющим вопросом.
- Спрашивают, как тебя зовут или кто ты — reply со своим именем и ролью.
- Ты диспетчер. Расходы, задачи и напоминания делаешь сам. Остальное передавай агенту, чья роль подходит
  (delegate, только агентам из списка «Другие агенты»). Вопросы, поиск информации, тексты — универсальному помощнику.
- Просят создать нового агента — create_agent с описанием словами владельца, ничего не добавляй.
- Неясно, чья это задача, — clarify с одним коротким вопросом. Не передавай задачу наугад.

Открытые задачи пользователя:
{tasks}"""


DEFAULT_IDENTITY = "Ты — Кимо, личный ассистент владельца в Telegram."


def build_messages(
    user_text: str, now_text: str, open_tasks: list[str], identity: str = ""
) -> list[dict[str, str]]:
    tasks_block = "\n".join(open_tasks) if open_tasks else "(нет)"
    system = SYSTEM_PROMPT.format(identity=identity or DEFAULT_IDENTITY, now=now_text, tasks=tasks_block)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_text},
    ]
