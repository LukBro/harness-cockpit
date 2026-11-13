#!/usr/bin/env bash
# Pelny zestaw testow jednym poleceniem: PostgreSQL z compose.yaml i trzy przebiegi pytest:
# bez bazy i e2e rownolegle (-n 4), testy bazy sekwencyjnie, pytest -m e2e (-n $COCKPIT_E2E_PROCESY).
# Zmienne: PYTHON (interpreter z pytest, playwright i pytest-xdist), COCKPIT_PG_PORT (port hosta, domyslnie 5432).
#
# Baza przebiegu (HAR-236): bez jawnego URL kazdy przebieg ma wlasna baze
# cockpit_test_<skrot sciezki korzenia repo (cksum)>_<PID skryptu> na wspolnym serwerze. Skrypt tworzy ja
# na starcie i usuwa w trapie EXIT (DROP DATABASE ... WITH (FORCE), razem z bazami workerow e2e
# <baza>_gw*). Na starcie usuwa tez bazy tego worktree po martwych PID-ach (przebieg zabity SIGKILL);
# baz innego worktree ani baz bez sufiksu PID nie rusza. Nazwy w SQL maja tylko [a-z0-9_].
#   COCKPIT_TEST_DATABASE_URL    jawny URL ma pierwszenstwo: pytest dostaje dokladnie ten URL,
#                                a skrypt bazy nie tworzy ani nie usuwa (CI bez zmian)
#
# Wspolne zasoby i blokady (mkdir jest atomowe; dziala tez w bashu 3.2 z macOS). W pliku pid w katalogu
# blokady jest PID wlasciciela; blokada martwego procesu jest przejmowana od razu (katalog .przejecie
# z ponownym sprawdzeniem). Do oceny, czy wlasciciel zyje, potrzebne jest ps; jego brak to blad (kod 1),
# nie zgoda na przejecie. Czekanie na przejecie liczy sie do tego samego limitu.
#  * Semafor pelnych przebiegow: slot 1 = katalog $COCKPIT_TESTY_BLOKADA, slot i > 1 = $BLOKADA.<i>.
#    Skrypt co sekunde probuje wszystkie sloty po kolei, wiec rownolegle moze isc tyle przebiegow, ile
#    jest slotow (wlasna baza i wlasny port demo, bez kolizji).
#  * Krotka blokada wspolnego zasobu $BLOKADA.zasob: tylko wokol sprawdzenia portu, docker compose up,
#    sprawdzenia tozsamosci serwera, tworzenia baz, sprzatania baz martwych PID-ow i instalacji
#    przegladarki (wspolny cache). Build frontu i testy ida poza nia.
#   COCKPIT_TESTY_BLOKADA        katalog slotu 1 (domyslnie ${TMPDIR:-/tmp}/cockpit_testy_pelne.lock)
#   COCKPIT_TESTY_SLOTY          liczba slotow, liczba calkowita >= 1 (domyslnie 2; pusta = domyslna)
#   COCKPIT_TESTY_LIMIT_BLOKADY  limit lacznego oczekiwania (slot + zasob) w sekundach, liczba calkowita
#                                >= 0 (domyslnie 3600); po jego przekroczeniu kod wyjscia 75.
#                                Limit 0 = jedna proba bez czekania.
#   COCKPIT_E2E_PROCESY          liczba procesow xdist przebiegu e2e, liczba calkowita >= 1 (domyslnie 4;
#                                pusta = domyslna). Kazdy worker e2e ma wlasna baze (HAR-236).
# Zla wartosc ktorejkolwiek zmiennej konczy skrypt kodem 1 przed uruchomieniem czegokolwiek (takze Dockera).
# Port demo (COCKPIT_DEMO_PORT, tools/demo_szkielet.py): skrypt wybiera wolny port i eksportuje go dla testow.
# Kody wyjscia: 0 zielony, 1 blad (narzedzia, baza, testy), 75 blokada zajeta po limicie.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
KORZEN_REPO="$(pwd -P)"
# Jawny URL zapamietujemy przed jakimkolwiek eksportem: pusty znaczy brak.
JAWNY_URL_BAZY="${COCKPIT_TEST_DATABASE_URL:-}"

blad() { echo "BLAD: $*" >&2; exit 1; }

PY="${PYTHON:-python}"
# Walidacja przed startem czegokolwiek (docker, blokady, testy): zla wartosc to blad, nie domyslna.
E2E_PROCESY="${COCKPIT_E2E_PROCESY:-4}"
case "$E2E_PROCESY" in
  *[!0-9]*) blad "COCKPIT_E2E_PROCESY musi byc liczba calkowita >= 1 (jest: '$E2E_PROCESY')" ;;
esac
[ "$E2E_PROCESY" -ge 1 ] || blad "COCKPIT_E2E_PROCESY musi byc liczba calkowita >= 1 (jest: '$E2E_PROCESY')"
SLOTY="${COCKPIT_TESTY_SLOTY:-2}"
case "$SLOTY" in
  *[!0-9]*) blad "COCKPIT_TESTY_SLOTY musi byc liczba calkowita >= 1 (jest: '$SLOTY')" ;;
esac
[ "$SLOTY" -ge 1 ] || blad "COCKPIT_TESTY_SLOTY musi byc liczba calkowita >= 1 (jest: '$SLOTY')"
LIMIT="${COCKPIT_TESTY_LIMIT_BLOKADY:-3600}"
case "$LIMIT" in
  ''|*[!0-9]*) blad "COCKPIT_TESTY_LIMIT_BLOKADY musi byc liczba sekund (jest: '${COCKPIT_TESTY_LIMIT_BLOKADY}')" ;;
esac
PORT="${COCKPIT_PG_PORT:-5432}"
export COCKPIT_PG_PORT="$PORT"
TMP_BAZOWY="${TMPDIR:-/tmp}"
BLOKADA="${COCKPIT_TESTY_BLOKADA:-${TMP_BAZOWY%/}/cockpit_testy_pelne.lock}"
ZASOB="$BLOKADA.zasob"
# Katalogi slotow: slot 1 = $BLOKADA, slot i > 1 = $BLOKADA.<i>.
KATALOGI_SLOTOW=("$BLOKADA")
i=2
while [ "$i" -le "$SLOTY" ]; do
  KATALOGI_SLOTOW+=("$BLOKADA.$i")
  i=$((i + 1))
done
LOG_PEL=""
LOG_BAZA=""
LOG_E2E=""
PRZEJECIE_KAT=""
BAZA_PRZEBIEGU=""

# Zwalnia katalog blokady, ale tylko wlasny (PID w pliku pid == $$): przebieg, ktory poddal sie na
# limicie, nie moze usunac blokady zywego wlasciciela.
zwolnij_wlasny() {
  local kat="$1" wlasciciel=""
  if [ -f "$kat/pid" ]; then
    wlasciciel="$(cat "$kat/pid" 2>/dev/null || true)"
  fi
  if [ "$wlasciciel" = "$$" ]; then
    rm -rf "$kat" || echo "BLAD: nie udalo sie zwolnic wlasnej blokady $kat (usun ja recznie)" >&2
  fi
  return 0
}

psql_db() { docker compose exec -T db psql -U cockpit -d cockpit_test -tA -c "$1"; }

# Usuwa baze o nazwie $1 (tylko [a-z0-9_]) i jej bazy workerow e2e <baza>_gw*.
usun_baze_z_workerami() {
  local nazwa="$1" w
  local lista
  lista="$(psql_db "SELECT datname FROM pg_database WHERE datname LIKE '${nazwa}\\_gw%'")" || return 1
  while IFS= read -r w; do
    case "$w" in
      ''|*[!a-z0-9_]*) continue ;;
    esac
    psql_db "DROP DATABASE IF EXISTS \"$w\" WITH (FORCE)" >/dev/null || return 1
  done <<EOF
$lista
EOF
  psql_db "DROP DATABASE IF EXISTS \"$nazwa\" WITH (FORCE)" >/dev/null
}

# Jedyny trap EXIT: usuwa logi (jesli juz powstaly), baze przebiegu (blad dropu idzie na stderr, ale nie
# zmienia kodu wyjscia przebiegu: trap nie robi z czerwonego zielonego ani odwrotnie) i zwalnia
# wlasne blokady (zasob, sloty).
sprzatnij() {
  [ -z "$LOG_PEL" ] || rm -f "$LOG_PEL"
  [ -z "$LOG_BAZA" ] || rm -f "$LOG_BAZA"
  [ -z "$LOG_E2E" ] || rm -f "$LOG_E2E"
  [ -z "$PRZEJECIE_KAT" ] || rmdir "$PRZEJECIE_KAT.przejecie" 2>/dev/null || true
  if [ -n "$BAZA_PRZEBIEGU" ]; then
    usun_baze_z_workerami "$BAZA_PRZEBIEGU" \
      || echo "BLAD: nie udalo sie usunac bazy przebiegu $BAZA_PRZEBIEGU (usun ja recznie: DROP DATABASE \"$BAZA_PRZEBIEGU\")" >&2
  fi
  local kat
  zwolnij_wlasny "$ZASOB"
  for kat in "${KATALOGI_SLOTOW[@]}"; do
    zwolnij_wlasny "$kat"
  done
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

# Czyta PID wlasciciela katalogu $1 do WLASCICIEL i zwraca 0, gdy blokada jest po martwym procesie
# (PID nieliczbowy albo brak procesu). Pusty PID = wlasciciel wlasnie zapisuje, nie martwy.
# Do sprawdzenia procesu sluzy ps -p, nie kill -0 (ten daje EPERM dla cudzego uzytkownika).
WLASCICIEL=""
katalog_martwy() {
  WLASCICIEL=""
  if [ -f "$1/pid" ]; then
    WLASCICIEL="$(tr -d '[:space:]' < "$1/pid" 2>/dev/null || true)"
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

# Jedna proba zdobycia katalogu blokady $1 (wspolna dla slotow i zasobu). Kod zwrotny:
#   0 zdobyty (PID zapisany), 1 zajety przez zywy proces (WLASCICIEL), 2 przejecie martwego trzyma
#   inny proces, 3 martwa blokada usunieta - ponow od razu (tylko po faktycznym usunieciu katalogu).
# Funkcja jest wolana w kontekscie `||` (set -e nie dziala w jej ciele), wiec bledy polecen obslugujemy
# jawnie: nieudany zapis PID albo nieudane usuniecie martwej blokady to blad (kod 1), nie ciche ponawianie.
sprobuj_katalog() {
  local kat="$1" usunieta=0
  if mkdir "$kat" 2>/dev/null; then
    if ! echo "$$" > "$kat/pid"; then
      rmdir "$kat" 2>/dev/null || true
      blad "nie da sie zapisac PID w blokadzie $kat"
    fi
    return 0
  fi
  if katalog_martwy "$kat"; then
    if mkdir "$kat.przejecie" 2>/dev/null; then
      PRZEJECIE_KAT="$kat"
      # Ponowne sprawdzenie pod pomocnicza blokada: inny przebieg mogl juz przejac blokade
      # i wpisac swoj PID; bez tego usunelibysmy blokade zywego wlasciciela.
      if katalog_martwy "$kat"; then
        echo "Przejmuje blokade martwego procesu PID $WLASCICIEL: $kat" >&2
        if ! rm -rf "$kat"; then
          rmdir "$kat.przejecie" 2>/dev/null || true
          PRZEJECIE_KAT=""
          blad "nie da sie usunac martwej blokady $kat (sprawdz prawa do katalogu i usun go recznie)"
        fi
        usunieta=1
      fi
      rmdir "$kat.przejecie" 2>/dev/null || true
      PRZEJECIE_KAT=""
      # Ponawiamy bez czekania tylko po usunieciu; gdy wlasciciel zmienil sie pod pomocnicza blokada,
      # wynik to "zajety" i petla czeka sekunde.
      [ "$usunieta" -eq 1 ] && return 3
      return 1
    fi
    return 2
  fi
  return 1
}

# Zdobywa JEDEN z katalogow blokady $2... (opis $1). W kazdej sekundzie czekania
# probuje wszystkie po kolei. Czas czekania liczymy sami (jedna sekunda na kazde sleep, wspolny licznik
# CZEKANO dla slotu i zasobu): SECONDS ma rozdzielczosc calych sekund i skrocilby czekanie o prawie sekunde.
CZEKANO=0
zdobadz_blokade() {
  local opis="$1" kat rc zajete przejecie_zajete lista ponow zglaszano=0
  shift
  lista=""
  for kat in "$@"; do
    lista="${lista:+$lista, }$kat"
  done
  while :; do
    zajete=""
    przejecie_zajete=""
    ponow=0
    for kat in "$@"; do
      rc=0
      sprobuj_katalog "$kat" || rc=$?
      case "$rc" in
        0)
          echo "Blokada $opis zdobyta: $kat (PID $$)" >&2
          return 0
          ;;
        3) ponow=1; break ;;
        2) [ -n "$przejecie_zajete" ] || przejecie_zajete="$kat" ;;
        *) zajete="${zajete:+$zajete, }${WLASCICIEL:-?}" ;;
      esac
    done
    [ "$ponow" -eq 0 ] || continue
    if [ "$CZEKANO" -ge "$LIMIT" ]; then
      if [ -n "$przejecie_zajete" ]; then
        # Inny proces trzyma przejecie: to tez czekanie i liczy sie do limitu (osierocone .przejecie).
        echo "BLAD: przejecie blokady $opis zajete ($przejecie_zajete.przejecie) - limit $LIMIT s minal; jesli zaden przebieg testow nie trwa, usun ten katalog recznie" >&2
      else
        echo "BLAD: blokada $opis zajeta przez PID ${zajete:-?} ($lista) - limit $LIMIT s minal; inne przebiegi testow trwaja, ponow pozniej albo zwieksz COCKPIT_TESTY_LIMIT_BLOKADY" >&2
      fi
      exit 75
    fi
    if [ "$zglaszano" -eq 0 ]; then
      echo "Czekam na blokade $opis (trzyma PID ${zajete:-?}, limit $LIMIT s)" >&2
      zglaszano=1
    fi
    sleep 1
    CZEKANO=$((CZEKANO + 1))
  done
}

# Slot pelnego przebiegu (semafor): po kontrolach narzedzi, przed jakimkolwiek zasobem wspolnym.
zdobadz_blokade "pelnego przebiegu" "${KATALOGI_SLOTOW[@]}"

# Wspolny zasob (port bazy, kontener, serwer, cache przegladarek) pod krotka blokada $ZASOB.
zdobadz_blokade "wspolnego zasobu" "$ZASOB"

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
if [ -z "$(psql_db "SELECT 1 FROM pg_database WHERE datname='cockpit'")" ]; then
  psql_db "CREATE DATABASE cockpit OWNER cockpit" >/dev/null
fi

# Baza przebiegu: nazwa z cksum sciezki korzenia (POSIX, bash 3.2 i Linux) i PID skryptu. Jawny URL wygrywa.
if [ -z "$JAWNY_URL_BAZY" ]; then
  SKROT="$(printf '%s' "$KORZEN_REPO" | cksum | cut -d' ' -f1)"
  case "$SKROT" in
    ''|*[!0-9]*) blad "nie udalo sie wyliczyc skrotu sciezki korzenia repo ('$SKROT')" ;;
  esac
  PREFIKS_BAZ="cockpit_test_${SKROT}_"
  # Sprzatanie po przebiegach zabitych SIGKILL: bazy tego worktree (prefiks z cksum) z martwym PID-em.
  # Baz innego worktree ani baz bez sufiksu PID nie ruszamy; sufiks to <PID> albo <PID>_gw<worker>.
  LISTA_BAZ="$(psql_db "SELECT datname FROM pg_database WHERE datname LIKE '${PREFIKS_BAZ}%'")"
  while IFS= read -r stara; do
    case "$stara" in
      ''|*[!a-z0-9_]*) continue ;;
    esac
    reszta="${stara#"$PREFIKS_BAZ"}"
    pid_bazy="${reszta%%_*}"
    case "$pid_bazy" in
      ''|*[!0-9]*) continue ;;
    esac
    [ "$reszta" = "$pid_bazy" ] || case "$reszta" in "${pid_bazy}_gw"*) ;; *) continue ;; esac
    if ps -p "$pid_bazy" >/dev/null 2>&1; then
      continue
    fi
    echo "Usuwam baze po martwym przebiegu PID $pid_bazy: $stara" >&2
    psql_db "DROP DATABASE IF EXISTS \"$stara\" WITH (FORCE)" >/dev/null
  done <<EOF
$LISTA_BAZ
EOF
  BAZA_PRZEBIEGU="${PREFIKS_BAZ}$$"
  # Baza o tej nazwie moze zostac po martwym przebiegu z tym samym PID-em (PID wrocil do obiegu).
  usun_baze_z_workerami "$BAZA_PRZEBIEGU" >/dev/null
  psql_db "CREATE DATABASE \"$BAZA_PRZEBIEGU\" OWNER cockpit" >/dev/null
  echo "Baza testowa tego przebiegu: $BAZA_PRZEBIEGU (usuwana na koniec przebiegu)" >&2
  export COCKPIT_TEST_DATABASE_URL="postgresql+asyncpg://cockpit:cockpit@localhost:$PORT/$BAZA_PRZEBIEGU"
fi

# Wspolny cache przegladarek: instalacja pod blokada zasobu.
"$PY" -m playwright install chromium

# Koniec operacji na wspolnym zasobie: build frontu i testy ida poza krotka blokada.
zwolnij_wlasny "$ZASOB"

# Jak w CI (npm ci); pomijamy tylko gdy node_modules juz jest, zeby powtorki byly szybkie.
(
  cd web
  [ -d node_modules ] || npm ci
  npm run build
)

# Port serwera przelotu demo (tools/demo_szkielet.py): wolny port z systemu, osobny dla kazdego przebiegu.
PORT_DEMO="$("$PY" -c '
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
' | tr -d '[:space:]')"
case "$PORT_DEMO" in
  ''|*[!0-9]*) blad "brak wolnego portu demo (wynik: '$PORT_DEMO')" ;;
esac
export COCKPIT_DEMO_PORT="$PORT_DEMO"

LOG_PEL="$(mktemp)"
LOG_BAZA="$(mktemp)"
LOG_E2E="$(mktemp)"

# Wszystkie przebiegi leca zawsze; kod pytest zachowujemy mimo tee.
# Bez bazy i e2e rownolegle (-n 4). Testy bazy ida sekwencyjnie: dziela jedna baze przebiegu
# (alembic downgrade base) i port demo z COCKPIT_DEMO_PORT (straznik w tests/conftest.py).
rc_pel=0
"$PY" -m pytest -q -rs -n 4 -m "not baza and not e2e" 2>&1 | tee "$LOG_PEL" || rc_pel=$?
rc_baza=0
"$PY" -m pytest -q -rs -m "baza and not e2e" 2>&1 | tee "$LOG_BAZA" || rc_baza=$?
rc_e2e=0
"$PY" -m pytest -m e2e -rs -n "$E2E_PROCESY" 2>&1 | tee "$LOG_E2E" || rc_e2e=$?

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
