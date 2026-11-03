#!/usr/bin/env bash
# Pelny zestaw testow jednym poleceniem: PostgreSQL z compose.yaml i trzy przebiegi pytest:
# bez bazy i e2e rownolegle (-n 4), testy bazy sekwencyjnie, pytest -m e2e.
# Zmienne: PYTHON (interpreter z pytest, playwright i pytest-xdist), COCKPIT_PG_PORT (port hosta, domyslnie 5432).
#
# Wszystkie pelne przebiegi uzywaja jednej bazy cockpit_test, wiec skrypt sam je serializuje
# blokada katalogowa (mkdir jest atomowe; dziala tez w bashu 3.2 z macOS). W pliku pid
# w katalogu blokady jest PID wlasciciela; blokada martwego procesu jest przejmowana od razu.
# Do oceny, czy wlasciciel zyje, potrzebne jest ps; jego brak to blad (kod 1), nie zgoda na przejecie.
# Czekanie na przejecie blokady (katalog .przejecie) liczy sie do tego samego limitu.
#   COCKPIT_TESTY_BLOKADA        katalog blokady (domyslnie ${TMPDIR:-/tmp}/cockpit_testy_pelne.lock)
#   COCKPIT_TESTY_LIMIT_BLOKADY  limit oczekiwania w sekundach, liczba calkowita >= 0 (domyslnie 3600);
#                                po jego przekroczeniu kod wyjscia 75. Limit 0 = jedna proba bez czekania.
# Kody wyjscia: 0 zielony, 1 blad (narzedzia, baza, testy), 75 blokada zajeta po limicie.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

blad() { echo "BLAD: $*" >&2; exit 1; }

PY="${PYTHON:-python}"
PORT="${COCKPIT_PG_PORT:-5432}"
export COCKPIT_PG_PORT="$PORT"
TMP_BAZOWY="${TMPDIR:-/tmp}"
BLOKADA="${COCKPIT_TESTY_BLOKADA:-${TMP_BAZOWY%/}/cockpit_testy_pelne.lock}"
LOG_PEL=""
LOG_BAZA=""
LOG_E2E=""
PRZEJECIE=0

# Jedyny trap EXIT: usuwa logi (jesli juz powstaly) i zwalnia blokade, ale tylko wlasna -
# przebieg, ktory poddal sie na limicie, nie moze usunac blokady zywego wlasciciela.
sprzatnij() {
  [ -z "$LOG_PEL" ] || rm -f "$LOG_PEL"
  [ -z "$LOG_BAZA" ] || rm -f "$LOG_BAZA"
  [ -z "$LOG_E2E" ] || rm -f "$LOG_E2E"
  [ "$PRZEJECIE" -eq 0 ] || rmdir "$BLOKADA.przejecie" 2>/dev/null || true
  local wlasciciel=""
  if [ -f "$BLOKADA/pid" ]; then
    wlasciciel="$(cat "$BLOKADA/pid" 2>/dev/null || true)"
  fi
  if [ "$wlasciciel" = "$$" ]; then
    rm -rf "$BLOKADA"
  fi
  return 0
}
# Tylko TERM ma osobny trap: bash wykonuje trap EXIT przy SIGINT takze bez niego (sprawdzone na
# /bin/bash 3.2: EXIT wykonany, proces konczy sie sygnalem INT), a przy SIGTERM bez trapu ginie bez EXIT.
trap sprzatnij EXIT
trap 'exit 143' TERM

command -v docker >/dev/null 2>&1 || blad "brak docker - zainstaluj Docker (Docker Desktop albo Colima)"
docker compose version >/dev/null 2>&1 || blad "brak docker compose v2 - zainstaluj wtyczke docker compose"
docker info >/dev/null 2>&1 || blad "demon Dockera nie dziala - uruchom Docker i ponow"
command -v npm >/dev/null 2>&1 || blad "brak npm - potrzebny do zbudowania frontu (web/)"
command -v "$PY" >/dev/null 2>&1 || blad "brak interpretera '$PY' - aktywuj venv albo ustaw PYTHON=..."
"$PY" -c "import pytest, playwright, xdist" >/dev/null 2>&1 \
  || blad "brak pytest/playwright/pytest-xdist w '$PY' - aktywuj venv z: pip install -e '.[dev]'"
# Straznik blokady nie moze ocenic zycia wlasciciela bez ps; brak narzedzia to blad, nie zgoda na przejecie.
command -v ps >/dev/null 2>&1 || blad "brak ps - potrzebny do sprawdzenia, czy wlasciciel blokady zyje"

# Blokada pelnego przebiegu: po kontrolach narzedzi, przed baza i portem.
LIMIT="${COCKPIT_TESTY_LIMIT_BLOKADY:-3600}"
case "$LIMIT" in
  ''|*[!0-9]*) blad "COCKPIT_TESTY_LIMIT_BLOKADY musi byc liczba sekund (jest: '${COCKPIT_TESTY_LIMIT_BLOKADY}')" ;;
esac

# Czyta PID wlasciciela do WLASCICIEL i zwraca 0, gdy blokada jest po martwym procesie
# (PID nieliczbowy albo brak procesu). Pusty PID = wlasciciel wlasnie zapisuje, nie martwy.
# Do sprawdzenia procesu sluzy ps -p, nie kill -0 (ten daje EPERM dla cudzego uzytkownika).
WLASCICIEL=""
blokada_martwa() {
  WLASCICIEL=""
  if [ -f "$BLOKADA/pid" ]; then
    WLASCICIEL="$(tr -d '[:space:]' < "$BLOKADA/pid" 2>/dev/null || true)"
  fi
  [ -n "$WLASCICIEL" ] || return 1
  case "$WLASCICIEL" in
    *[!0-9]*) return 0 ;;
  esac
  if ps -p "$WLASCICIEL" >/dev/null 2>&1; then
    return 1
  fi
  return 0
}

# Czas czekania liczymy sami (jedna sekunda na kazde sleep): SECONDS ma rozdzielczosc calych
# sekund i mogloby skrocic czekanie o prawie sekunde.
CZEKANO=0
ZGLOSZONO_CZEKANIE=0
while :; do
  if mkdir "$BLOKADA" 2>/dev/null; then
    echo "$$" > "$BLOKADA/pid"
    echo "Blokada pelnego przebiegu zdobyta: $BLOKADA (PID $$)" >&2
    break
  fi
  if blokada_martwa; then
    if mkdir "$BLOKADA.przejecie" 2>/dev/null; then
      PRZEJECIE=1
      # Ponowne sprawdzenie pod pomocnicza blokada: inny przebieg mogl juz przejac blokade
      # i wpisac swoj PID; bez tego usunelibysmy blokade zywego wlasciciela.
      if blokada_martwa; then
        echo "Przejmuje blokade martwego procesu PID $WLASCICIEL: $BLOKADA" >&2
        rm -rf "$BLOKADA"
      fi
      rmdir "$BLOKADA.przejecie" 2>/dev/null || true
      PRZEJECIE=0
    else
      # Inny proces trzyma przejecie: to tez czekanie i liczy sie do limitu (osierocone .przejecie).
      if [ "$CZEKANO" -ge "$LIMIT" ]; then
        echo "BLAD: przejecie blokady pelnego przebiegu zajete ($BLOKADA.przejecie) - limit $LIMIT s minal; jesli zaden przebieg testow nie trwa, usun ten katalog recznie" >&2
        exit 75
      fi
      sleep 1
      CZEKANO=$((CZEKANO + 1))
    fi
    continue
  fi
  if [ "$CZEKANO" -ge "$LIMIT" ]; then
    echo "BLAD: blokada pelnego przebiegu zajeta przez PID ${WLASCICIEL:-?} ($BLOKADA) - limit $LIMIT s minal; inny przebieg testow trwa, ponow pozniej albo zwieksz COCKPIT_TESTY_LIMIT_BLOKADY" >&2
    exit 75
  fi
  if [ "$ZGLOSZONO_CZEKANIE" -eq 0 ]; then
    echo "Czekam na blokade pelnego przebiegu (trzyma PID $WLASCICIEL, limit $LIMIT s)" >&2
    ZGLOSZONO_CZEKANIE=1
  fi
  sleep 1
  CZEKANO=$((CZEKANO + 1))
done

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

if ! docker compose up -d --wait db; then
  blad "nie udalo sie uruchomic bazy (docker compose up db) - sprawdz, czy port $PORT jest wolny i Docker dziala; inny port: COCKPIT_PG_PORT=5433 $0"
fi

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
