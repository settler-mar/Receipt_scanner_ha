#!/bin/sh
set -eu

project_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
image_name="receipt-scanner:local"
container_name="receipt-scanner-dev"
data_dir="$project_dir/backend/data"
host_port="${PORT:-8001}"

usage() {
  cat <<'EOF'
Usage: ./dev-docker.sh COMMAND

Commands:
  build   Build the image with the development dependencies/runtime
  up      Build and start the backend with Python hot-reload
  logs    Follow backend logs
  status  Show backend status
  stop    Stop the backend container
  down    Remove the backend container (data is kept)
EOF
}

build_image() {
  docker build -t "$image_name" "$project_dir"
}

case "${1:-}" in
  build)
    build_image
    ;;
  up)
    build_image
    mkdir -p "$data_dir"
    docker rm -f "$container_name" >/dev/null 2>&1 || true
    docker run -d \
      --name "$container_name" \
      --init \
      --shm-size=512m \
      -p "$host_port:8001" \
      -e TZ=Europe/Kyiv \
      -e RS_DATABASE_URL=sqlite:////app/data/receipts.db \
      -e RS_BASE_URL="${RS_BASE_URL:-http://localhost:$host_port}" \
      -e RS_SECRET_KEY="${RS_SECRET_KEY:-CHANGE_ME_dev_only_insecure_key_______________=}" \
      -v "$data_dir:/app/data" \
      -v "$project_dir/backend/app:/app/app" \
      -v "$project_dir/backend/alembic:/app/alembic" \
      -v "$project_dir/backend/alembic.ini:/app/alembic.ini" \
      -v "$project_dir/backend/scripts:/app/scripts" \
      -v "$project_dir/backend/dnipro.py:/app/dnipro.py" \
      "$image_name" \
      uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload --reload-dir /app/app
    echo "Backend: http://localhost:$host_port"
    ;;
  logs)
    docker logs -f --tail 200 "$container_name"
    ;;
  status)
    docker container inspect -f \
      'status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}' \
      "$container_name"
    ;;
  stop)
    docker stop "$container_name"
    ;;
  down)
    docker rm -f "$container_name"
    echo "Persistent data remains in: $data_dir"
    ;;
  *)
    usage
    exit 1
    ;;
esac
