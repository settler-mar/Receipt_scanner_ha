#!/bin/sh
set -eu

display_number="${DISPLAY#:}"
display_number="${display_number%%.*}"
socket_path="/tmp/.X11-unix/X${display_number}"
lock_path="/tmp/.X${display_number}-lock"

mkdir -p /app/data /tmp/.X11-unix

# Home Assistant writes app options into the persistent data directory. Keep
# the same image usable under both the Supervisor and plain Docker Compose.
options_file=""
for candidate in /data/options.json /app/data/options.json; do
  if [ -f "$candidate" ]; then
    options_file="$candidate"
    break
  fi
done

if [ -n "$options_file" ]; then
  configured_base_url="$(python -c 'import json,sys; print(json.load(open(sys.argv[1])).get("base_url", ""))' "$options_file")"
  configured_secret_key="$(python -c 'import json,sys; print(json.load(open(sys.argv[1])).get("secret_key", ""))' "$options_file")"
  configured_google_client_id="$(python -c 'import json,sys; print(json.load(open(sys.argv[1])).get("google_client_id", ""))' "$options_file")"
  configured_google_client_secret="$(python -c 'import json,sys; print(json.load(open(sys.argv[1])).get("google_client_secret", ""))' "$options_file")"
  if [ -n "$configured_base_url" ]; then
    export RS_BASE_URL="$configured_base_url"
  fi
  if [ -n "$configured_secret_key" ]; then
    export RS_SECRET_KEY="$configured_secret_key"
  fi
  if [ -n "$configured_google_client_id" ]; then
    export RS_GOOGLE_CLIENT_ID="$configured_google_client_id"
  fi
  if [ -n "$configured_google_client_secret" ]; then
    export RS_GOOGLE_CLIENT_SECRET="$configured_google_client_secret"
  fi
fi

# A Docker restart kills Xvfb but preserves the container's writable layer,
# including its Unix socket and lock file. Checking the socket alone therefore
# treats a dead display as healthy and headed Chromium exits with
# TargetClosedError. The entrypoint is the owner of this private display, so
# remove any leftovers before starting it on every container boot.
rm -f "$socket_path" "$lock_path"
Xvfb "$DISPLAY" -screen 0 1400x900x24 -nolisten tcp -ac &
xvfb_pid=$!

attempts=0
while [ ! -S "$socket_path" ]; do
  if ! kill -0 "$xvfb_pid" 2>/dev/null; then
    echo "Xvfb failed to start on $DISPLAY" >&2
    exit 1
  fi
  attempts=$((attempts + 1))
  if [ "$attempts" -ge 100 ]; then
    echo "Timed out waiting for Xvfb on $DISPLAY" >&2
    exit 1
  fi
  sleep 0.05
done

exec "$@"
