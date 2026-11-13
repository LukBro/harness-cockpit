#!/usr/bin/env bash
# Moment po `git merge origin/main` (HAR-240, regula merge-rownoleglych-ticketow): git laczy bez
# konfliktu kod, ktory razem nie dziala, wiec po merge idzie caly zestaw, nie tylko testy plikow z konfliktem.
# Cztery etapy, wszystkie zawsze sie wykonuja (kazdy wypisuje swoj wynik):
#   konflikt  - zaden sledzony plik nie ma znacznikow konfliktu ani wpisu niescalonego w indeksie
#   alembic   - `alembic heads` zwraca dokladnie jedna glowe (kolizja numeru migracji z rownoleglego ticketu)
#   mutanty   - tools/mutanty_reczne.py --sprawdz-wszystkie: kazdy wiersz kazdej tabeli nadal trafia w kod
#   testy     - tools/testy.py -q -n 4 (pytest z limitem czasu bez postepu)
# Kod wyjscia: 0 wszystko poprawne, 1 etap zglosil blad w kodzie (konflikt, glowy, czerwony test),
# 2 blad narzedzia albo wiersz tabeli niewykonywalny (brak dowodu); priorytet 2 > 1 > 0.
# Ostatnia linia stdout: `PO-MERGE: ok` albo `PO-MERGE: blad <etap>` (pierwszy etap o najwyzszym kodzie).
# Zmienne: PYTHON (interpreter z pytest, xdist i alembic; domyslnie python),
#   PO_MERGE_TESTY_ARGS (argumenty testy.py; domyslnie "-q -n 4").
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
PY="${PYTHON:-python}"
ARGS_TESTOW="${PO_MERGE_TESTY_ARGS:--q -n 4}"

KOD=0
ETAP=""

# zglos <etap> <kod>: zapamietuje pierwszy etap o najwyzszym kodzie (2 > 1 > 0).
zglos() {
  local etap="$1" kod="$2"
  echo "PO-MERGE etap $etap: blad (kod $kod)" >&2
  if [ "$kod" -gt "$KOD" ]; then
    KOD="$kod"
    ETAP="$etap"
  fi
}

# --- konflikt ---------------------------------------------------------------------------------
# git grep: 0 trafienie, 1 brak, inny kod to blad samego narzedzia.
trafienia="$(git grep -nIE '^(<{7}|>{7})( |$)' 2>&1)"
kod_grep=$?
if [ "$kod_grep" -eq 0 ]; then
  echo "$trafienia" >&2
  zglos konflikt 1
elif [ "$kod_grep" -ne 1 ]; then
  echo "BLAD: git grep zakonczyl sie kodem $kod_grep: $trafienia" >&2
  zglos konflikt 2
else
  niescalone="$(git ls-files -u 2>&1)"
  kod_indeksu=$?
  if [ "$kod_indeksu" -ne 0 ]; then
    echo "BLAD: git ls-files -u zakonczyl sie kodem $kod_indeksu: $niescalone" >&2
    zglos konflikt 2
  elif [ -n "$niescalone" ]; then
    echo "$niescalone" >&2
    zglos konflikt 1
  else
    echo "PO-MERGE etap konflikt: ok"
  fi
fi

# --- alembic ----------------------------------------------------------------------------------
glowy="$("$PY" -m alembic heads 2>&1)"
kod_alembic=$?
if [ "$kod_alembic" -ne 0 ]; then
  echo "BLAD: alembic heads zakonczyl sie kodem $kod_alembic: $glowy" >&2
  zglos alembic 2
else
  liczba_glow="$(printf '%s\n' "$glowy" | grep -c '(head)')"
  if [ "$liczba_glow" -eq 1 ]; then
    echo "PO-MERGE etap alembic: ok (jedna glowa)"
  else
    echo "BLAD: alembic heads: oczekiwano jednej glowy, jest $liczba_glow" >&2
    printf '%s\n' "$glowy" >&2
    zglos alembic 1
  fi
fi

# --- mutanty ----------------------------------------------------------------------------------
"$PY" tools/mutanty_reczne.py --sprawdz-wszystkie
kod_mutantow=$?
if [ "$kod_mutantow" -eq 0 ]; then
  echo "PO-MERGE etap mutanty: ok"
else
  zglos mutanty 2
fi

# --- testy ------------------------------------------------------------------------------------
# ARGS_TESTOW bez cudzyslowow: ma sie rozbic na slowa.
"$PY" tools/testy.py $ARGS_TESTOW
kod_testow=$?
if [ "$kod_testow" -eq 0 ]; then
  echo "PO-MERGE etap testy: ok"
elif [ "$kod_testow" -eq 1 ]; then
  zglos testy 1
else
  # 2 zawieszenie albo przerwanie, 3 blad wewnetrzny, 4 uzycie, 5 nic nie zebrano: brak dowodu, nie czerwony test.
  zglos testy 2
fi

if [ "$KOD" -eq 0 ]; then
  echo "PO-MERGE: ok"
else
  echo "PO-MERGE: blad $ETAP"
fi
exit "$KOD"
