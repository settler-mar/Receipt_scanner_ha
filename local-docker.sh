#!/bin/sh
set -eu

project_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
image_name="receipt-scanner:local"
container_name="receipt-scanner-local"
data_dir="$project_dir/backend/data"
host_port="${PORT:-8001}"

usage() {
  cat <<'EOF'
Usage: ./local-docker.sh COMMAND

Environment:
  PORT     Host port (default: 8001)

Commands:
  up       Build the image and start/update the local container
  start    Start an existing stopped container
  stop     Stop the container without deleting it
  down     Stop and remove the container (persistent data is kept)
  restart  Restart the running container
  logs     Follow application logs
  status   Show container status and health
  build    Build the image only
EOF
}

container_exists() {
  docker container inspect "$container_name" >/dev/null 2>&1
}

container_running() {
  [ "$(docker container inspect -f '{{.State.Running}}' "$container_name" 2>/dev/null || true)" = "true" ]
}

build_image() {
  docker build \
    --build-arg BUILD_VERSION=0.1.0 \
    --build-arg BUILD_ARCH=amd64 \
    -t "$image_name" \
    "$project_dir"
}

start_new_container() {
  mkdir -p "$data_dir"
  if ! docker run -d \
    --name "$container_name" \
    --restart unless-stopped \
    --init \
    --shm-size=512m \
    -p "$host_port:8001" \
    -e TZ=Europe/Kyiv \
    -e RS_DATABASE_URL=sqlite:////app/data/receipts.db \
    -e RS_BASE_URL="${RS_BASE_URL:-http://localhost:$host_port}" \
    -e RS_SECRET_KEY="${RS_SECRET_KEY:-CHANGE_ME_dev_only_insecure_key_______________=}" \
    -v "$data_dir:/app/data" \
    "$image_name"; then
    docker rm -f "$container_name" >/dev/null 2>&1 || true
    return 1
  fi
}

command="${1:-}"
case "$command" in
  up)
    build_image
    if container_exists; then
      docker rm -f "$container_name" >/dev/null
    fi
    start_new_container
    echo "Receipt Scanner: http://localhost:$host_port"
    ;;
  build)
    build_image
    ;;
  start)
    if container_exists; then
      docker start "$container_name"
    else
      echo "Container does not exist. Run: ./local-docker.sh up" >&2
      exit 1
    fi
    ;;
  stop)
    if container_running; then
      docker stop "$container_name"
    else
      echo "Container is not running"
    fi
    ;;
  down)
    if container_exists; then
      docker rm -f "$container_name"
    else
      echo "Container does not exist"
    fi
    echo "Persistent data remains in: $data_dir"
    ;;
  restart)
    docker restart "$container_name"
    ;;
  logs)
    docker logs -f --tail 200 "$container_name"
    ;;
  status)
    if container_exists; then
      docker container inspect -f \
        "status={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}} url=http://localhost:$host_port" \
        "$container_name"
    else
      echo "status=not-created"
    fi
    ;;
  *)
    usage
    exit 1
    ;;
esac
