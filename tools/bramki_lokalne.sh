#!/usr/bin/env bash
# Bramki lokalne rownolegle (HAR-236): pelne testy i mutanty zmienionych funkcji naraz, wiec calosc
# trwa tyle co dluzsza z nich.
#   tools/bramki_lokalne.sh [argumenty dla mutanty_zmian.py ...]   (np. --od-przebiegu PLIK)
#
# Testy:   "${COCKPIT_BRAMKI_TESTY:-tools/testy_pelne.sh}"  -> $KAT/testy.log (stdout + stderr)
# Mutanty: python tools/mutanty_zmian.py --max-children $COCKPIT_MUTMUT_DZIECI "$@"
#          -> $KAT/mutanty.stdout i $KAT/mutanty.stderr; bez COCKPIT_TEST_DATABASE_URL i
#          COCKPIT_WYMAGAJ_BAZY (mutmut nie potrzebuje bazy testow, a jej adres wylaczalby testy).
# Zmienne:
#   PYTHON                  interpreter z mutmutem (domyslnie python)
#   COCKPIT_MUTMUT_DZIECI   liczba procesow mutmuta, liczba calkowita >= 1 (domyslnie 8)
#   COCKPIT_BRAMKI_KATALOG  katalog logow (domyslnie nowy katalog z mktemp -d); powstaje, jesli go nie ma
#   COCKPIT_BRAMKI_TESTY    skrypt testow (domyslnie tools/testy_pelne.sh)
# Na starcie stdout podaje sciezki logow, na koncu dokladnie trzy linie:
#   testy: kod <X>, log <sciezka>
#   mutanty: kod <Y>, stdout <sciezka>, stderr <sciezka>
#   bramki: kod <Z>
# Kod Z: Y spoza {0,1,3} -> 2 (brak dowodu z mutantow); inaczej X != 0 albo Y = 1 -> 1; inaczej Y = 3 -> 3;
# inaczej 0. SIGTERM / SIGINT zabija obie bramki razem z ich dziecmi (kazda w wlasnej grupie procesow,
# `set -m`) i konczy skrypt kodem 143 / 130.
set -u

blad() { echo "BLAD: $*" >&2; exit 2; }

PY="${PYTHON:-python}"
DZIECI="${COCKPIT_MUTMUT_DZIECI:-8}"
case "$DZIECI" in
  *[!0-9]*) blad "COCKPIT_MUTMUT_DZIECI musi byc liczba calkowita >= 1 (jest: '$DZIECI')" ;;
esac
[ "$DZIECI" -ge 1 ] || blad "COCKPIT_MUTMUT_DZIECI musi byc liczba calkowita >= 1 (jest: '$DZIECI')"

if [ -n "${COCKPIT_BRAMKI_KATALOG:-}" ]; then
  KAT="$COCKPIT_BRAMKI_KATALOG"
  mkdir -p "$KAT" || blad "nie udalo sie utworzyc katalogu logow '$KAT'"
else
  KAT="$(mktemp -d "${TMPDIR:-/tmp}/bramki_lokalne.XXXXXX")" \
    || blad "nie udalo sie utworzyc katalogu tymczasowego w '${TMPDIR:-/tmp}'"
fi
[ -w "$KAT" ] || blad "katalog logow '$KAT' nie jest katalogiem do zapisu"
case "$KAT" in
  /*) ;;
  *) KAT="$PWD/$KAT" ;;
esac

# `cd` do katalogu nad tools/ nie zawodzi: katalog skryptu istnieje, skoro skrypt dziala.
cd "$(dirname "${BASH_SOURCE[0]}")/.."

TESTY="${COCKPIT_BRAMKI_TESTY:-tools/testy_pelne.sh}"
echo "testy: log $KAT/testy.log"
echo "mutanty: stdout $KAT/mutanty.stdout, stderr $KAT/mutanty.stderr"

PID_TESTY=""
PID_MUTANTY=""

zabij_grupy() {
  local pid
  for pid in "$PID_TESTY" "$PID_MUTANTY"; do
    [ -z "$pid" ] || kill -TERM -- "-$pid" 2>/dev/null || true
  done
}

# Przerwanie: TERM do obu grup, po 10 s KILL (straznik w wlasnej grupie), potem czekamy na liderow
# i dobijamy to, co zostalo w grupach. Bez trapu zostalyby sieroty (runner testow, mutmut, ich workery).
# Trap jest ustawiony PRZED startem bramek (pid-y moga byc jeszcze puste, stad straznik `-z`):
# sygnal w oknie miedzy startem a ustawieniem trapu zabilby skrypt domyslna akcja, zostawiajac sieroty.
przerwij() {
  trap '' TERM INT
  # Straznik ma wlasna grupe (globalne set -m), wiec koniec skryptu zabija go razem z `sleep 10`
  # jednym KILL na grupe: dziedziczony ignorowany TERM uniemozliwialby zabicie go przez TERM.
  zabij_grupy
  ( sleep 10; kill -KILL -- "-$PID_TESTY" "-$PID_MUTANTY" 2>/dev/null ) &
  local straznik=$!
  [ -z "$PID_TESTY" ] || wait "$PID_TESTY" 2>/dev/null
  [ -z "$PID_MUTANTY" ] || wait "$PID_MUTANTY" 2>/dev/null
  kill -KILL -- "-$PID_TESTY" "-$PID_MUTANTY" 2>/dev/null || true
  kill -KILL -- "-$straznik" 2>/dev/null || true
  wait "$straznik" 2>/dev/null
  exit "$1"
}
# Kazde zadanie w tle we wlasnej grupie procesow (set -m: pgid = pid zadania): bramki, zeby jedno
# `kill -- -pgid` zabilo cale drzewo (runner testow, mutmut i ich dzieci), i straznik przerwania.
# set -m przed trapami: przerwanie tuz po ich ustawieniu startuje straznika juz we wlasnej grupie.
set -m
trap 'przerwij 143' TERM
trap 'przerwij 130' INT

"$TESTY" < /dev/null > "$KAT/testy.log" 2>&1 &
PID_TESTY=$!
env -u COCKPIT_TEST_DATABASE_URL -u COCKPIT_WYMAGAJ_BAZY \
  "$PY" tools/mutanty_zmian.py --max-children "$DZIECI" "$@" \
  < /dev/null > "$KAT/mutanty.stdout" 2> "$KAT/mutanty.stderr" &
PID_MUTANTY=$!

wait "$PID_TESTY"
KOD_TESTOW=$?
wait "$PID_MUTANTY"
KOD_MUTANTOW=$?

echo "testy: kod $KOD_TESTOW, log $KAT/testy.log"
echo "mutanty: kod $KOD_MUTANTOW, stdout $KAT/mutanty.stdout, stderr $KAT/mutanty.stderr"

case "$KOD_MUTANTOW" in
  0|1|3) ;;
  *) echo "bramki: kod 2"; exit 2 ;;
esac
if [ "$KOD_TESTOW" -ne 0 ] || [ "$KOD_MUTANTOW" -eq 1 ]; then
  KOD=1
elif [ "$KOD_MUTANTOW" -eq 3 ]; then
  KOD=3
else
  KOD=0
fi
echo "bramki: kod $KOD"
exit "$KOD"
