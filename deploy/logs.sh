#!/usr/bin/env bash
set -euo pipefail
docker logs -f --tail "${TAIL:-200}" kometa
