#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
docker rm -f kometa >/dev/null 2>&1 || true
echo "kometa stopped"
