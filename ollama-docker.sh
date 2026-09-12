#!/bin/sh
set -eu

# Local LLM backend (Ollama) for Receipt Scanner.
#
# Two ways to run it:
#
#   ./ollama-docker.sh native   RECOMMENDED for a CPU box (dev machine, Orange Pi).
#                               Installs Ollama on the host (~1.5 GB), tunes the
#                               systemd unit, pulls the model. No Docker.
#
#   ./ollama-docker.sh up       Containerised (docker-compose.ollama.yml). The
#                               official ollama/ollama image bundles CUDA+ROCm
#                               and is ~4.5 GB — only use this if you specifically
#                               want it in Docker and have the disk for it.
#
# Environment:
#   OLLAMA_MODEL   Model to pull/run (default: qwen2.5:3b)
#   OLLAMA_PORT    Host port for the container path (default: 11434)
#
# Commands: native | up | pull | down | logs | status | models | shell

project_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
compose_file="$project_dir/docker-compose.ollama.yml"
model="${OLLAMA_MODEL:-qwen2.5:3b}"

if docker compose version >/dev/null 2>&1; then
  dc() { docker compose -f "$compose_file" "$@"; }
elif command -v docker-compose >/dev/null 2>&1; then
  dc() { docker-compose -f "$compose_file" "$@"; }
else
  dc() { echo "docker compose not found" >&2; exit 1; }
fi

case "${1:-}" in
  native)
    if ! command -v ollama >/dev/null 2>&1; then
      echo "Installing Ollama on the host..."
      curl -fsSL https://ollama.com/install.sh | sh
    fi
    if command -v systemctl >/dev/null 2>&1; then
      sudo mkdir -p /etc/systemd/system/ollama.service.d
      sudo tee /etc/systemd/system/ollama.service.d/receipt-scanner.conf >/dev/null <<'EOF'
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_KEEP_ALIVE=30m"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
# Orange Pi 5 Plus / RK3588: pin inference to the A76 "big" cores (4-7).
# Uncomment on RK3588; leave commented on other CPUs.
#CPUAffinity=4 5 6 7
EOF
      sudo systemctl daemon-reload
      sudo systemctl restart ollama
    fi
    echo "Pulling $model (first run downloads ~1-2 GB)..."
    ollama pull "$model"
    echo
    echo "Ollama ready:  http://localhost:11434"
    echo "In the app Settings: URL http://<host-ip>:11434, model '$model', num_ctx 8192."
    ;;
  up)
    dc up -d --remove-orphans ollama
    printf 'Waiting for Ollama to come up'
    i=0
    until dc exec -T ollama ollama list >/dev/null 2>&1; do
      i=$((i + 1)); [ "$i" -gt 60 ] && { echo; echo "Ollama did not become ready" >&2; exit 1; }
      printf '.'; sleep 2
    done
    echo " ok"
    echo "Pulling $model (first run downloads ~1-2 GB, be patient)..."
    dc exec -T ollama ollama pull "$model"
    echo
    echo "Ollama ready:  http://localhost:${OLLAMA_PORT:-11434}"
    echo "In the app Settings: URL http://<host-ip>:${OLLAMA_PORT:-11434}, model '$model', num_ctx 8192."
    ;;
  pull)
    if command -v ollama >/dev/null 2>&1 && ! dc ps >/dev/null 2>&1; then
      ollama pull "$model"
    else
      dc exec -T ollama ollama pull "$model"
    fi
    ;;
  down)
    dc down --remove-orphans
    echo "Pulled models remain in the 'ollama-models' volume."
    ;;
  logs)
    dc logs -f ollama
    ;;
  status)
    if command -v ollama >/dev/null 2>&1; then ollama list 2>/dev/null || true; fi
    dc ps 2>/dev/null || true
    ;;
  models)
    if command -v ollama >/dev/null 2>&1; then ollama list; else dc exec -T ollama ollama list; fi
    ;;
  shell)
    dc exec ollama /bin/sh
    ;;
  *)
    sed -n '3,26p' "$0"
    exit 1
    ;;
esac
