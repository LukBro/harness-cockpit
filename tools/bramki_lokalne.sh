#!/usr/bin/env bash
# Bramki lokalne rownolegle (HAR-236): testy i mutanty zmienionych funkcji naraz, wiec calosc
# trwa tyle co dluzsza z nich.
#   tools/bramki_lokalne.sh [argumenty dla mutanty_zmian.py ...]   (np. --od-przebiegu KATALOG)
#
# Testy:   sh -c "${COCKPIT_BRAMKI_TESTY:-pytest -q -n 4}"  -> $KAT/testy.log (stdout + stderr)
# Mutanty: $PYTHON tools/mutanty_zmian.py --max-children $COCKPIT_MUTMUT_DZIECI "$@"
#          -> $KAT/mutanty.stdout i $KAT/mutanty.stderr; bez COCKPIT_TEST_DATABASE_URL i
#          COCKPIT_WYMAGAJ_BAZY (mutmut nie potrzebuje bazy testow).
# Zmienne: PYTHON (domyslnie python), COCKPIT_MUTMUT_DZIECI (liczba calkowita >= 1, domyslnie 8),
#   COCKPIT_BRAMKI_KATALOG (katalog logow, domyslnie nowy z mktemp -d), COCKPIT_BRAMKI_TESTY.
# Stdout, dokladnie trzy linie:
#   testy: <X>
#   mutanty: <Y>
#   bramki: <Z>
# Kod Z: Y spoza {0,1,3} -> 2 (brak dowodu z mutantow); inaczej X != 0 albo Y = 1 -> 1; inaczej
# Y = 3 -> 3; inaczej 0. SIGTERM / SIGINT zabija obie bramki z ich dziecmi (kazda w wlasnej grupie
# procesow, `set -m`) i konczy skrypt kodem 143 / 130.
set -u

blad() { echo "BLAD: $*" >&2; exit 2; }

PY="${PYTHON:-python}"
DZIECI="${COCKPIT_MUTMUT_DZIECI:-8}"
case "$DZIECI" in
  ''|*[!0-9]*) blad "COCKPIT_MUTMUT_DZIECI musi byc liczba calkowita >= 1 (jest: '$DZIECI')" ;;
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

cd "$(dirname "${BASH_SOURCE[0]}")/.." || blad "nie udalo sie przejsc do korzenia repozytorium"

TESTY="${COCKPIT_BRAMKI_TESTY:-pytest -q -n 4}"

PID_TESTY=""
PID_MUTANTY=""

# Przerwanie: TERM do obu grup, po 10 s KILL (straznik w wlasnej grupie), potem czekamy na liderow
# i dobijamy to, co zostalo w grupach. Trap stoi PRZED startem bramek (pid-y moga byc puste).
przerwij() {
  trap '' TERM INT
  local pid
  for pid in "$PID_TESTY" "$PID_MUTANTY"; do
    [ -z "$pid" ] || kill -TERM -- "-$pid" 2>/dev/null || true
  done
  ( sleep 10; kill -KILL -- "-$PID_TESTY" "-$PID_MUTANTY" 2>/dev/null ) &
  local straznik=$!
  [ -z "$PID_TESTY" ] || wait "$PID_TESTY" 2>/dev/null
  [ -z "$PID_MUTANTY" ] || wait "$PID_MUTANTY" 2>/dev/null
  kill -KILL -- "-$PID_TESTY" "-$PID_MUTANTY" 2>/dev/null || true
  kill -KILL -- "-$straznik" 2>/dev/null || true
  wait "$straznik" 2>/dev/null
  exit "$1"
}
# set -m: kazde zadanie w tle ma wlasna grupe (pgid = pid), wiec jedno `kill -- -pgid` zabija
# cale drzewo; set -m przed trapami, zeby straznik tez mial wlasna grupe.
set -m
trap 'przerwij 143' TERM
trap 'przerwij 130' INT

sh -c "$TESTY" < /dev/null > "$KAT/testy.log" 2>&1 &
PID_TESTY=$!
env -u COCKPIT_TEST_DATABASE_URL -u COCKPIT_WYMAGAJ_BAZY \
  "$PY" tools/mutanty_zmian.py --max-children "$DZIECI" "$@" \
  < /dev/null > "$KAT/mutanty.stdout" 2> "$KAT/mutanty.stderr" &
PID_MUTANTY=$!

wait "$PID_TESTY"
KOD_TESTOW=$?
wait "$PID_MUTANTY"
KOD_MUTANTOW=$?

echo "testy: $KOD_TESTOW"
echo "mutanty: $KOD_MUTANTOW"
echo "logi: $KAT" >&2

case "$KOD_MUTANTOW" in
  0|1|3) ;;
  *) echo "bramki: 2"; exit 2 ;;
esac
if [ "$KOD_TESTOW" -ne 0 ] || [ "$KOD_MUTANTOW" -eq 1 ]; then
  KOD=1
elif [ "$KOD_MUTANTOW" -eq 3 ]; then
  KOD=3
else
  KOD=0
fi
echo "bramki: $KOD"
exit "$KOD"
