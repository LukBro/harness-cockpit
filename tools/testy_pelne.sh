#!/usr/bin/env bash
# Pelny zestaw testow jednym poleceniem: PostgreSQL z compose.yaml i trzy przebiegi pytest:
# bez bazy i e2e rownolegle (-n 4), testy bazy sekwencyjnie, pytest -m e2e.
# Zmienne: PYTHON (interpreter z pytest, playwright i pytest-xdist), COCKPIT_PG_PORT (port hosta, domyslnie 5432).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

blad() { echo "BLAD: $*" >&2; exit 1; }

PY="${PYTHON:-python}"
PORT="${COCKPIT_PG_PORT:-5432}"
export COCKPIT_PG_PORT="$PORT"

command -v docker >/dev/null 2>&1 || blad "brak docker - zainstaluj Docker (Docker Desktop albo Colima)"
docker compose version >/dev/null 2>&1 || blad "brak docker compose v2 - zainstaluj wtyczke docker compose"
docker info >/dev/null 2>&1 || blad "demon Dockera nie dziala - uruchom Docker i ponow"
command -v npm >/dev/null 2>&1 || blad "brak npm - potrzebny do zbudowania frontu (web/)"
command -v "$PY" >/dev/null 2>&1 || blad "brak interpretera '$PY' - aktywuj venv albo ustaw PYTHON=..."
"$PY" -c "import pytest, playwright, xdist" >/dev/null 2>&1 \
  || blad "brak pytest/playwright/pytest-xdist w '$PY' - aktywuj venv z: pip install -e '.[dev]'"

# Port zajety przez cudzy proces: nie ruszamy go, bo testy robia downgrade bazy.
# Kontener na INNYM porcie nie usprawiedliwia pominiecia sprawdzenia: up odtworzy go na $PORT.
BIND="$(docker compose port db 5432 2>/dev/null || true)"
if [ "${BIND##*:}" != "$PORT" ] \
   && (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  echo "BLAD: port $PORT jest zajety przez proces spoza tego compose:" >&2
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2 2>/dev/null || echo "  (nie udalo sie ustalic procesu)" >&2
  echo "Uzyj innego portu: COCKPIT_PG_PORT=5433 $0" >&2
  exit 1
fi

docker compose up -d --wait db

# Testy robia downgrade base, wiec URL z hosta musi trafic w nasz kontener, a nie w inny serwer.
ID_KONTENER="$(docker compose exec -T db psql -U cockpit -d cockpit_test -tA \
  -c "SELECT system_identifier FROM pg_control_system()" | tr -d '[:space:]')"
ID_HOST="$("$PY" -c '
import asyncio, sys, asyncpg
async def main():
    c = await asyncpg.connect(sys.argv[1], timeout=10)
    try:
        print(await c.fetchval("SELECT system_identifier FROM pg_control_system()"))
    finally:
        await c.close()
asyncio.run(main())
' "postgresql://cockpit:cockpit@localhost:$PORT/cockpit_test" 2>/dev/null | tr -d '[:space:]' || true)"
if [ -z "$ID_KONTENER" ] || [ "$ID_HOST" != "$ID_KONTENER" ]; then
  echo "BLAD: pod localhost:$PORT odpowiada inny Postgres niz kontener z compose.yaml" >&2
  echo "Uzyj wolnego portu: COCKPIT_PG_PORT=5433 $0" >&2
  exit 1
fi

# Obraz tworzy tylko POSTGRES_DB; baza aplikacji musi powstac tez na istniejacym wolumenie.
psql_db() { docker compose exec -T db psql -U cockpit -d cockpit_test -tA -c "$1"; }
if [ -z "$(psql_db "SELECT 1 FROM pg_database WHERE datname='cockpit'")" ]; then
  psql_db "CREATE DATABASE cockpit OWNER cockpit" >/dev/null
fi

export COCKPIT_TEST_DATABASE_URL="postgresql+asyncpg://cockpit:cockpit@localhost:$PORT/cockpit_test"

# Jak w CI (npm ci); pomijamy tylko gdy node_modules juz jest, zeby powtorki byly szybkie.
(
  cd web
  [ -d node_modules ] || npm ci
  npm run build
)

"$PY" -m playwright install chromium

LOG_PEL="$(mktemp)"
LOG_BAZA="$(mktemp)"
LOG_E2E="$(mktemp)"
trap 'rm -f "$LOG_PEL" "$LOG_BAZA" "$LOG_E2E"' EXIT

# Wszystkie przebiegi leca zawsze; kod pytest zachowujemy mimo tee.
# Bez bazy i e2e rownolegle (-n 4). Testy bazy ida sekwencyjnie: dziela jedna baze
# cockpit_test (alembic downgrade base) i port 8799 w demo (straznik w tests/conftest.py).
rc_pel=0
"$PY" -m pytest -q -rs -n 4 -m "not baza and not e2e" 2>&1 | tee "$LOG_PEL" || rc_pel=$?
rc_baza=0
"$PY" -m pytest -q -rs -m "baza and not e2e" 2>&1 | tee "$LOG_BAZA" || rc_baza=$?
rc_e2e=0
"$PY" -m pytest -m e2e -rs 2>&1 | tee "$LOG_E2E" || rc_e2e=$?

# evidence_required: pominiety test migracji albo e2e to blad przebiegu, nie zielone przejscie.
rc_skip=0
if grep -hE '^SKIPPED .*tests/(test_migracje\.py|e2e/)' "$LOG_PEL" "$LOG_BAZA" "$LOG_E2E" >&2; then
  echo "BLAD: pominieto testy migracji lub e2e - to nie jest zielony przebieg" >&2
  rc_skip=1
fi

[ "$rc_pel" -eq 0 ] || echo "BLAD: pytest -n 4 (bez bazy i e2e) zakonczyl sie kodem $rc_pel" >&2
[ "$rc_baza" -eq 0 ] || echo "BLAD: pytest -m baza zakonczyl sie kodem $rc_baza" >&2
[ "$rc_e2e" -eq 0 ] || echo "BLAD: pytest -m e2e zakonczyl sie kodem $rc_e2e" >&2

if [ "$rc_pel" -ne 0 ] || [ "$rc_baza" -ne 0 ] || [ "$rc_e2e" -ne 0 ] || [ "$rc_skip" -ne 0 ]; then
  exit 1
fi
echo "OK: pelny zestaw testow zielony"
