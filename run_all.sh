#!/usr/bin/env bash
# Все включённые агенты из таблицы agents, каждый в своём окне tmux (сессия agents).
#   ./run_all.sh [start] | stop | restart | status
set -u
cd "$(dirname "$(readlink -f "$0")")" || exit 1
SESSION="${BOT_SESSION:-agents}"
VENV="${VENV:-.venv}"

if [ ! -f "$VENV/bin/activate" ]; then echo "Не найдено окружение $VENV. См. README."; exit 1; fi
# shellcheck disable=SC1091
. "$VENV/bin/activate"

case "${1:-start}" in
  start)
    names="$(python manage.py agents enabled)" || { echo "Не удалось прочитать список агентов"; exit 1; }
    [ -n "$names" ] || { echo "Нет включённых агентов"; exit 0; }
    # Кимо первым: остальные агенты обращаются к нему через шину
    if grep -qx kimo <<<"$names"; then ./run.sh start kimo; sleep 3; fi
    for name in $names; do
      [ "$name" = "kimo" ] || ./run.sh start "$name"
    done
    echo "Окна: tmux attach -t $SESSION (переключение Ctrl+B, затем номер; выйти Ctrl+B, затем D)"
    ;;
  stop) tmux kill-session -t "$SESSION" 2>/dev/null && echo "Все агенты остановлены." || echo "Агенты не запущены." ;;
  restart) "$0" stop; sleep 1; "$0" start ;;
  status) ./run.sh status ;;
  *) echo "Использование: $0 [start|stop|restart|status]"; exit 1 ;;
esac
