# Telegram-ассистент

Личный бот-ассистент: задачи с напоминаниями, учёт расходов в сумах и долларах по курсу ЦБ, голосовые на русском и узбекском, графики и трекер выполнения задач. Готов к продаже по месячной подписке через Telegram Stars.

- Python 3.11+, aiogram 3, SQLite (WAL), APScheduler, matplotlib (Agg), httpx, Fernet
- ИИ: Groq. Текст обрабатывает `openai/gpt-oss-120b`, голос — `whisper-large-v3-turbo`. Модели задаются в `.env`
- Бот работает через long polling: Docker, вебхуки и туннели не нужны
- Данные пользователей изолированы по `user_id`

## Возможности

| Раздел | Что умеет |
|---|---|
| Задачи | Добавление текстом или голосом («завтра в 15:00 позвонить маме»). Списки «Сегодня» и «Завтра», выполнено, перенос (+1 час, на завтра, своя дата), удаление, напоминания |
| Расходы | Суммы в UZS и USD, категории, заметки. Курс USD с cbu.uz кэшируется на 1 час; если сайт недоступен, берётся последний сохранённый курс. Итоги за день, 7 и 30 дней в обеих валютах |
| Статистика | Графики расходов по дням и по категориям, выполнение задач по дням. Трекер: процент выполненных задач за 7 и 30 дней и серия дней подряд |
| Подписка | 250 ⭐ за 30 дней (Telegram Stars, XTR). Напоминание за 3 дня до окончания. Админ пользуется ботом бесплатно |
| Админ | `/grant`, `/revoke`, `/users`, `/stats`, `/setsecret`, `/secrets`, `/delsecret`, `/adminhelp` |

Обычный текст и голосовые отправляются в Groq. Модель возвращает JSON с действиями, бот проверяет его и только потом выполняет. Если ИИ недоступен, все функции работают через кнопки и команды `/add`, `/spend`.

---

## Установка на телефон-сервер (Termux + Debian)

### 1. Termux

Установите **Termux** из F-Droid. Версия из Google Play устарела. Затем выполните в Termux:

```bash
pkg update && pkg upgrade -y
pkg install -y proot-distro openssh tmux termux-api
termux-wake-lock          # не даёт Android усыпить процесс
proot-distro install debian
```

Отключите для Termux оптимизацию батареи: Настройки Android → Приложения → Termux → Батарея → «Без ограничений».

**SSH (необязательно).** В Termux задайте пароль командой `passwd` и запустите `sshd`. Подключение с компьютера: `ssh -p 8022 <любое_имя>@<IP_телефона>`.

### 2. Debian

```bash
proot-distro login debian
apt update && apt install -y python3 python3-venv python3-pip git tmux
python3 --version          # нужна 3.11 или новее
```

### 3. Код и зависимости

Репозиторий приватный, поэтому для клонирования нужен токен GitHub (Settings → Developer settings → Personal access tokens) или SSH-ключ.

```bash
cd ~
git clone https://github.com/ibrohim8279997-dev/nexio-aasistant.git
cd nexio-aasistant
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Настройка `.env`

```bash
cp .env.example .env
python gen_key.py          # выведет ENCRYPTION_KEY — вставьте его в .env
nano .env
chmod 600 .env
```

| Переменная | Что указать |
|---|---|
| `BOT_TOKEN` | токен от @BotFather |
| `GROQ_API_KEY` | ключ с https://console.groq.com/keys |
| `ADMIN_ID` | ваш числовой Telegram ID (узнать можно у @userinfobot) |
| `ENCRYPTION_KEY` | вывод `python gen_key.py`. **Сохраните копию.** Без этого ключа зашифрованные секреты в БД не прочитать |
| `TEXT_MODEL` | `openai/gpt-oss-120b` |
| `STT_MODEL` | `whisper-large-v3-turbo` |
| `DB_PATH` | `data/bot.db` |
| `CLICK_ENABLED` | `false` (Click — только заготовка) |

Необязательные переменные: `RATE_LIMIT_PER_MIN` (антиспам, по умолчанию 30), `SUB_PRICE_XTR` (250), `SUB_DAYS` (30), `LOG_LEVEL` (INFO).

### 5. Запуск

```bash
chmod +x run.sh
./run.sh            # запуск в фоне в tmux; при падении бот перезапустится через 5 с
./run.sh status     # работает ли бот
./run.sh attach     # смотреть вывод (выйти: Ctrl+B, затем D)
./run.sh logs       # хвост logs/bot.log
./run.sh restart
./run.sh stop
./run.sh fg         # запуск в текущем терминале (для отладки)
```

После первого запуска закройте доступ к базе:

```bash
chmod 600 data/bot.db*
```

Бот сам ставит права 600 на файл БД, если файловая система это поддерживает.

### 6. Автозапуск после перезагрузки телефона (необязательно)

Установите **Termux:Boot** из F-Droid и один раз откройте его. Затем в Termux выполните:

```bash
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/start-bot.sh <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
proot-distro login debian -- bash -c "cd ~/nexio-aasistant && ./run.sh"
EOF
chmod +x ~/.termux/boot/start-bot.sh
```

## Обновление

```bash
cd ~/nexio-aasistant
git pull
. .venv/bin/activate && pip install -r requirements.txt
./run.sh restart
```

Миграции БД применяются автоматически при запуске. Номер версии схемы хранится в `PRAGMA user_version`.

**Резервная копия:**

```bash
sqlite3 data/bot.db ".backup data/backup-$(date +%F).db"
```

Если нет `sqlite3`, установите его: `apt install sqlite3`. Храните копию вместе с `ENCRYPTION_KEY`.

## Тесты

```bash
pip install -r requirements-dev.txt
pytest -q
```

Тесты покрывают разбор JSON от модели, расчёт подписки, конвертацию валют, шифрование, изоляцию данных по пользователям, ответ 429 от Groq, разбор ввода, графики и прогон апдейтов через диспетчер с фейковым Telegram.

---

## Подписка и оплата

- **Telegram Stars.** Бот выставляет счёт через `send_invoice` с `currency="XTR"` и пустым `provider_token`. Обрабатываются `pre_checkout_query` (проверка суммы, валюты и владельца счёта) и `successful_payment` (запись платежа, продление на 30 дней). Повторный апдейт по тому же `charge_id` игнорируется. Если подписка ещё активна, новый срок добавляется к текущему. Звёзды выводятся через @BotFather → Bot → Payments / Fragment.
- **Click** — заготовка. Код в `bot/handlers/payments.py` выключен флагом `CLICK_ENABLED=false`, и кнопка Click пользователям не показывается. Порядок включения:
  1. получите токен провайдера Click в @BotFather;
  2. сохраните его командой `/setsecret click_provider_token <токен>` (хранится зашифрованным) или переменной `CLICK_PROVIDER_TOKEN`;
  3. проверьте оплату в тестовом режиме;
  4. поставьте `CLICK_ENABLED=true`.
- `/paysupport` — команда поддержки по оплате. Telegram требует её у ботов, которые принимают Stars.

## Безопасность

- `.env` не попадает в git (см. `.gitignore`). В репозитории лежит только `.env.example`.
- Секреты, которые бот хранит в SQLite (таблица `secrets`), шифруются Fernet. Ключ лежит только в `.env`.
- Логи проходят через фильтр, который заменяет токены и ключи на `***`, в том числе в traceback.
- Все SQL-запросы параметризованные. Каждый запрос к задачам и расходам фильтруется по `user_id`.
- Антиспам: не больше `RATE_LIMIT_PER_MIN` событий в минуту на пользователя.
- Ввод проверяется: суммы (больше 0, верхний предел), даты (не в прошлом, не дальше 5 лет), длина текста (задача — 500 символов, сообщение для ИИ — 1000, категория — 50, заметка — 200).
- Права на файлы: `chmod 600 .env data/bot.db*`.

## Частые проблемы

| Симптом | Решение |
|---|---|
| `Ошибка конфигурации: Не задана переменная …` | Заполните `.env`. При этой ошибке `run.sh` не перезапускает бота |
| `ENCRYPTION_KEY некорректен` | Сгенерируйте ключ командой `python gen_key.py` и вставьте строку целиком |
| `TelegramConflictError: terminated by other getUpdates request` | Запущены две копии бота. Выполните `./run.sh stop` и проверьте `tmux ls` |
| Бот засыпает при выключенном экране | Выполните `termux-wake-lock` и отключите оптимизацию батареи для Termux |
| «Слишком много запросов к ИИ» | Сработал лимит Groq free (GPT-OSS: 30 в минуту и 1000 в день; Whisper: 20 в минуту и 2000 в день). Бот сам повторяет запрос после 429. Если лимит дневной, пользуйтесь кнопками |
| «cbu.uz недоступен, использую последний сохранённый курс» | Сайт ЦБ не отвечает. Бот повторит запрос не раньше чем через 5 минут |
| `pip install` долго собирает matplotlib или cryptography | На 32-битном ARM нет готовых wheel. Поставьте пакеты из Debian: `apt install python3-matplotlib python3-cryptography`, затем создайте venv с флагом `--system-site-packages` |
| Время напоминаний сдвинуто | Бот всегда работает по Asia/Tashkent. Проверьте, что установился пакет `tzdata` (`pip install tzdata`) |
| Кнопки без цвета | Цветные кнопки (`style`) работают в новых клиентах Telegram. В старых показываются обычные кнопки, на версиях aiogram без поля `style` — эмодзи 🟢 🔴 🔵 |
| Голосовое не распознаётся | Telegram отдаёт ботам файлы до 20 МБ, Groq принимает до 25 МБ. Запишите сообщение короче |

## Структура

```
bot/
  main.py              точка входа: python -m bot.main
  config.py            чтение .env
  db.py                схема, миграции, доступ к SQLite
  crypto.py            шифрование секретов (Fernet)
  middlewares.py       антиспам, пользователь из БД, доступ по подписке
  keyboards.py         inline-кнопки (style или эмодзи)
  timeutil.py          часовой пояс Asia/Tashkent, в БД хранится UTC
  logging_setup.py     логи без секретов
  ai/groq_client.py    Groq: текст и голос, лимиты, ответ 429
  ai/actions.py        промпт и проверка JSON-действий
  handlers/            start, tasks, expenses, stats, voice, payments, admin
  services/            rates, charts, subscription, scheduler, tasks, expenses, tracker, assistant
tests/                 pytest
gen_key.py             генерация ENCRYPTION_KEY
run.sh                 запуск в tmux/screen с автоперезапуском
```

«Неделя» и «месяц» в итогах — это последние 7 и 30 дней, включая сегодня.
