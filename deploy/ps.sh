#!/usr/bin/env bash
set -euo pipefail
docker ps -a --filter name=kometa --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
