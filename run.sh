#!/usr/bin/env bash
# Запуск агентов с автоперезапуском. Каждый агент — отдельный процесс в своём окне tmux (сессия agents).
#   ./run.sh                    — поднять всех включённых агентов (то же, что ./run_all.sh)
#   ./run.sh start <агент>      — запустить агента в отдельном окне tmux
#   ./run.sh fg <агент>         — запустить в текущем терминале (перезапуск при падении)
#   ./run.sh stop <агент> | restart <агент> | logs <агент>
#   ./run.sh status | attach
set -u
cd "$(dirname "$(readlink -f "$0")")" || exit 1

SESSION="${BOT_SESSION:-agents}"
VENV="${VENV:-.venv}"
RESTART_DELAY="${RESTART_DELAY:-5}"
SELF="$(readlink -f "$0")"

valid_name() { [[ "${1:-}" =~ ^[a-z][a-z0-9_]{2,31}$ ]] || { echo "Некорректное имя агента: '${1:-}'"; exit 1; }; }

activate() {
  if [ ! -f "$VENV/bin/activate" ]; then
    echo "Не найдено виртуальное окружение $VENV. См. README, шаг установки."
    exit 1
  fi
  # shellcheck disable=SC1091
  . "$VENV/bin/activate"
}

loop() {
  local name="$1"
  activate
  mkdir -p logs data
  trap 'echo "Агент $name остановлен."; exit 0' INT TERM
  while true; do
    echo "[$(date '+%F %T')] Запуск агента $name"
    python run_agent.py "$name"
    code=$?
    case "$code" in
      0) echo "[$(date '+%F %T')] Агент $name завершился штатно." ;;
      2) echo "Ошибка конфигурации агента $name (.env / токен). Перезапуск отменён."; exit 2 ;;
      3) echo "Агент $name выключен или не найден. Перезапуск отменён."; exit 3 ;;
      *) echo "[$(date '+%F %T')] Агент $name упал (код $code)." ;;
    esac
    echo "Перезапуск через ${RESTART_DELAY} с."
    sleep "$RESTART_DELAY"
  done
}

need_tmux() { command -v tmux >/dev/null 2>&1 || { echo "Нужен tmux: apt install -y tmux"; exit 1; }; }
window_exists() { tmux list-windows -t "$SESSION" -F '#W' 2>/dev/null | grep -qx "$1"; }

start_agent() {
  local name="$1"
  need_tmux
  if window_exists "$name"; then echo "Агент $name уже запущен (tmux: $SESSION:$name)."; return 0; fi
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    tmux new-window -d -t "$SESSION" -n "$name" "$SELF fg $name"
  else
    tmux new-session -d -s "$SESSION" -n "$name" "$SELF fg $name"
  fi
  echo "Агент $name запущен в окне tmux $SESSION:$name"
}

case "${1:-all}" in
  all) exec "$(dirname "$SELF")/run_all.sh" start ;;
  start) valid_name "${2:-}"; start_agent "$2" ;;
  fg) valid_name "${2:-}"; loop "$2" ;;
  stop)
    valid_name "${2:-}"
    tmux kill-window -t "$SESSION:$2" 2>/dev/null && echo "Агент $2 остановлен." || echo "Агент $2 не запущен."
    ;;
  restart) valid_name "${2:-}"; "$SELF" stop "$2"; sleep 1; "$SELF" start "$2" ;;
  logs) valid_name "${2:-}"; tail -n 100 -f "logs/$2.log" ;;
  status)
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      echo "Запущены (tmux $SESSION):"; tmux list-windows -t "$SESSION" -F '  #W'
    else
      echo "Агенты не запущены"
    fi
    ;;
  attach) tmux attach -t "$SESSION" ;;
  *) echo "Использование: $0 [start|fg|stop|restart|logs <агент>] | status | attach"; exit 1 ;;
esac
