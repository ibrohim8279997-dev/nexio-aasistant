#!/usr/bin/env bash
# Публикует API Mini App (localhost:API_PORT) в интернет через Tailscale Funnel. Домен и Cloudflare не нужны.
# Один раз в админке Tailscale: DNS -> включить MagicDNS и HTTPS Certificates; Access controls -> разрешить Funnel для узла
# (в файле политик: "nodeAttrs": [{"target": ["autogroup:member"], "attr": ["funnel"]}]).
set -euo pipefail
PORT="${API_PORT:-8080}"
command -v tailscale >/dev/null || { echo "tailscale не найден. Установите/запустите tailscaled (см. README)."; exit 1; }
tailscale status >/dev/null || { echo "Tailscale не подключён: tailscale up"; exit 1; }
tailscale funnel --bg "$PORT"
echo
tailscale funnel status
NAME="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
echo
echo "Публичный адрес Mini App: https://${NAME}"
echo "Добавьте в .env:  MINIAPP_URL=https://${NAME}   и перезапустите Кимо: ./run.sh restart kimo"
echo "Проверка: curl -s https://${NAME}/health"
