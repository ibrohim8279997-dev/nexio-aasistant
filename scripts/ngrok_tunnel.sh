#!/usr/bin/env bash
# Туннель ngrok к API Mini App. Запускается из run.sh (окно tmux «ngrok»), работает в цикле вместе с ngrok.
# После старта берёт публичный адрес из локального API ngrok (127.0.0.1:4040), печатает его и пишет в data/miniapp_url.
# Кимо читает этот файл при старте (если MINIAPP_URL в .env не задан) и ставит кнопку меню Mini App.
# Если адрес изменился, а Кимо уже запущен, Кимо перезапускается сам (отключить: NGROK_RESTART_KIMO=0).
# Один раз: ngrok config add-authtoken <токен>. Постоянный адрес: NGROK_DOMAIN=<имя>.ngrok-free.app в .env.
set -u
cd "$(dirname "$(readlink -f "$0")")/.." || exit 1
[ -f .env ] && { set -a; . ./.env; set +a; }
PORT="${API_PORT:-8080}"
FILE="${NGROK_URL_FILE:-data/miniapp_url}"
command -v ngrok >/dev/null || { echo "ngrok не найден: поставьте его (см. README) и повторите."; exit 2; }
mkdir -p "$(dirname "$FILE")"
old="$(cat "$FILE" 2>/dev/null || true)"

ARGS=(http "127.0.0.1:$PORT" --log=stdout --log-level=warn)   # именно 127.0.0.1: «localhost» ngrok открывает как IPv6 (::1), а API слушает IPv4
[ -n "${NGROK_DOMAIN:-}" ] && ARGS+=(--url="${NGROK_DOMAIN}")
ngrok "${ARGS[@]}" &
NG=$!
trap 'kill "$NG" 2>/dev/null; exit 0' INT TERM

url=""
for _ in $(seq 1 40); do
  sleep 1
  kill -0 "$NG" 2>/dev/null || { echo "ngrok завершился (проверьте authtoken: ngrok config add-authtoken ...)"; exit 1; }
  url="$(curl -s http://127.0.0.1:4040/api/tunnels | python3 -c 'import json,sys
try:
    t=[x["public_url"] for x in json.load(sys.stdin)["tunnels"] if x["public_url"].startswith("https://")]
    print(t[0] if t else "")
except Exception:
    print("")' 2>/dev/null)"
  [ -n "$url" ] && break
done
if [ -z "$url" ]; then echo "Не получил адрес от ngrok за 40 с."; kill "$NG" 2>/dev/null; exit 1; fi

printf '%s' "$url" > "$FILE"
echo "=============================================="
echo " Mini App: $url"
echo " Адрес сохранён в $FILE (MINIAPP_URL подхватится при старте Кимо)"
echo "=============================================="
if [ -n "$old" ] && [ "$old" != "$url" ] && [ "${NGROK_RESTART_KIMO:-1}" = "1" ] && tmux list-windows -t "${BOT_SESSION:-agents}" -F '#W' 2>/dev/null | grep -qx kimo; then
  echo "Адрес изменился, перезапускаю Кимо, чтобы обновить кнопку Mini App."
  ./run.sh restart kimo
fi
wait "$NG"
