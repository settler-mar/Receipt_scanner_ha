#!/usr/bin/env bash
# Link this repo's Codex prompts into ~/.codex/prompts (idempotent).
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dest="${CODEX_HOME:-$HOME/.codex}/prompts"
mkdir -p "$dest"
for f in "$repo"/.codex/prompts/*.md; do
  ln -sfn "$f" "$dest/$(basename "$f")"
  echo "linked $(basename "$f")"
done
echo "done -> $dest"
