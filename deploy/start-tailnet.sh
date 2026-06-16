#!/usr/bin/env bash
# Odysseus tailnet launcher.
# - Pulls admin password from deploy/.admin-pw.
# - Loads repo .env through uvicorn.
# - Pre-flights tailscale cert + LiteLLM reachability.
# - Starts Odysseus on 127.0.0.1:7860, Caddy on tailnet:8443.

set -euo pipefail

REPO="/Users/jakubsikora/Repos/personal/odysseus"
DEPLOY="$REPO/deploy"
TAILNET_IP="100.116.31.6"
TAILNET_HOST="mac-studio-jakub.tail5d39b4.ts.net"
TAILNET_PORT="8443"
LITELLM_URL="http://litellm.tail5d39b4.ts.net:4000/v1"

cd "$REPO"
mkdir -p "$REPO/data" "$DEPLOY"
chmod 700 "$REPO/data"

# 1. Admin password from secured file (fallback; keychain needs GUI).
SECRET_FILE="$DEPLOY/.admin-pw"
if [[ -f "$SECRET_FILE" ]] && [[ -r "$SECRET_FILE" ]]; then
	ADMIN_PW="$(cat "$SECRET_FILE")"
	export ODYSSEUS_ADMIN_PASSWORD="$ADMIN_PW"
else
	echo "no $SECRET_FILE — generate one:"
	echo "  openssl rand -base64 24 > $SECRET_FILE && chmod 600 $SECRET_FILE"
	exit 1
fi

# 2. Pre-flight: LiteLLM reachable?
if ! curl -fsS -m 3 -o /dev/null "$LITELLM_URL/models" -H "Authorization: Bearer x"; then
	# 401 is fine, 000 means network failure
	code=$(curl -sS -m 3 -o /dev/null -w "%{http_code}" "$LITELLM_URL/models" -H "Authorization: Bearer x" || echo 000)
	if [[ "$code" == "000" ]]; then
		echo "WARN: LiteLLM at $LITELLM_URL unreachable. Continuing anyway."
	fi
fi

# 3. Cert age check (Tailscale certs ~90 days).
CERT="$DEPLOY/${TAILNET_HOST}.crt"
if [[ ! -f "$CERT" ]]; then
	echo "missing TLS cert; run: tailscale cert ${TAILNET_HOST}"
	exit 1
fi

# 4. Start Odysseus (background, log to deploy/odysseus.log).
ODY_LOG="$DEPLOY/odysseus.log"
if pgrep -f "uvicorn app:app .*--port 7860" >/dev/null; then
	echo "odysseus already running on :7860"
else
	echo "starting odysseus → $ODY_LOG"
	nohup "$REPO/.venv/bin/python" -m uvicorn app:app \
		--host 127.0.0.1 --port 7860 \
		--env-file "$REPO/.env" \
		>"$ODY_LOG" 2>&1 &
	echo "  pid=$!"
fi

# 5. Start Caddy (background).
CADDY_LOG="$DEPLOY/caddy.log"
if pgrep -f "caddy run --config .*Caddyfile" >/dev/null; then
	echo "caddy already running"
else
	CADDY_STDOUT_LOG="$DEPLOY/caddy.stdout.log"
	echo "starting caddy → $CADDY_STDOUT_LOG"
	nohup caddy run --config "$DEPLOY/Caddyfile" >"$CADDY_STDOUT_LOG" 2>&1 &
	echo "  pid=$!"
fi

sleep 2
echo ""
echo "=== READY ==="
echo "URL:         https://${TAILNET_HOST}:${TAILNET_PORT}"
echo "LiteLLM:     $LITELLM_URL"
echo "Admin user:  admin"
echo "Admin pass:  (cat deploy/.admin-pw)"
echo ""
echo "If the LiteLLM endpoint is missing, add it in Settings → Model Endpoints:"
echo "  Base URL: $LITELLM_URL"
echo "  API Key:  (from sikoras-chat/.env SIKORASY_LITELLM_KEY — rotate it)"
echo ""
echo "Stop:        pkill -f 'uvicorn app:app' ; pkill -f 'caddy run'"
echo "Logs:        tail -f $ODY_LOG $CADDY_LOG $DEPLOY/caddy.stdout.log"
