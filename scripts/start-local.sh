#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -f .runtime/local.env ]; then
  if [ -x .venv/bin/python ]; then .venv/bin/python scripts/init_local.py
  else
    docker run --rm -u "$(id -u):$(id -g)" -v "$PWD:/app" -w /app python:3.12-slim sh -c 'pip install --quiet --target /tmp/pdvdeps cryptography && PYTHONPATH=/tmp/pdvdeps python scripts/init_local.py'
  fi
fi
docker compose --env-file .runtime/local.env -f compose.local.yaml up -d --build
docker compose --env-file .runtime/local.env -f compose.local.yaml exec -T cloud python manage.py seed_demo
printf '%s\n' 'PDV2: http://127.0.0.1:8000/ • Terminal: http://127.0.0.1:8010/' 'Credenciais locais em .runtime/access.txt'
