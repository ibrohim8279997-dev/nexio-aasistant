#!/usr/bin/env bash
# Запуск бота с автоперезапуском в tmux (или screen).
#   ./run.sh          — запустить в фоне (tmux/screen)
#   ./run.sh fg       — запустить в текущем терминале
#   ./run.sh stop | restart | attach | status | logs
set -u
cd "$(dirname "$(readlink -f "$0")")" || exit 1

SESSION="${BOT_SESSION:-tgbot}"
VENV="${VENV:-.venv}"
RESTART_DELAY="${RESTART_DELAY:-5}"
SELF="$(readlink -f "$0")"

loop() {
  if [ ! -f "$VENV/bin/activate" ]; then
    echo "Не найдено виртуальное окружение $VENV. См. README, шаг установки."
    exit 1
  fi
  # shellcheck disable=SC1091
  . "$VENV/bin/activate"
  mkdir -p logs data
  trap 'echo "Остановлено."; exit 0' INT TERM
  while true; do
    echo "[$(date '+%F %T')] Запуск бота"
    python -m bot.main
    code=$?
    if [ "$code" -eq 2 ]; then
      echo "Ошибка конфигурации (.env). Исправьте и запустите снова."
      exit 2
    fi
    echo "[$(date '+%F %T')] Бот завершился с кодом $code. Перезапуск через ${RESTART_DELAY} с."
    sleep "$RESTART_DELAY"
  done
}

is_running() {
  if command -v tmux >/dev/null 2>&1 && tmux has-session -t "$SESSION" 2>/dev/null; then return 0; fi
  if command -v screen >/dev/null 2>&1 && screen -list 2>/dev/null | grep -q "[.]$SESSION[[:space:]]"; then return 0; fi
  return 1
}

case "${1:-start}" in
  start)
    if is_running; then echo "Бот уже запущен (сессия $SESSION)."; exit 0; fi
    if command -v tmux >/dev/null 2>&1; then
      tmux new-session -d -s "$SESSION" "$SELF fg"
      echo "Бот запущен в tmux. Подключиться: tmux attach -t $SESSION (выйти: Ctrl+B, затем D)"
    elif command -v screen >/dev/null 2>&1; then
      screen -dmS "$SESSION" "$SELF" fg
      echo "Бот запущен в screen. Подключиться: screen -r $SESSION (выйти: Ctrl+A, затем D)"
    else
      echo "Не найден tmux или screen. Установите: apt install -y tmux"
      exit 1
    fi
    ;;
  fg) loop ;;
  stop)
    tmux kill-session -t "$SESSION" 2>/dev/null || screen -S "$SESSION" -X quit 2>/dev/null || true
    echo "Бот остановлен."
    ;;
  restart) "$SELF" stop; sleep 1; "$SELF" start ;;
  attach) tmux attach -t "$SESSION" 2>/dev/null || screen -r "$SESSION" ;;
  status) if is_running; then echo "Работает (сессия $SESSION)"; else echo "Не запущен"; fi ;;
  logs) tail -n 100 -f logs/bot.log ;;
  *) echo "Использование: $0 [start|fg|stop|restart|attach|status|logs]"; exit 1 ;;
esac
