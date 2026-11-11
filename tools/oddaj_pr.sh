#!/usr/bin/env bash
# Moment oddania pracy (HAR-240): sprawdzenia z CLAUDE.md "Commity" i regul sesji, potem push i PR.
#
#   tools/oddaj_pr.sh --tytul TYTUL --tresc PLIK [--baza origin/main] [--dozwolone PLIK]...
#
# --tresc    plik z trescia PR (--body-file); musi istniec i nie byc pusty
# --baza     ref bazowy (domyslnie origin/main); galaz PR to nazwa bazy bez `origin/`
# --dozwolone  sciezka (wzgledem korzenia repo) pliku, ktory moze byc brudny w `git status` (powtarzalne)
#
# Kolejnosc: autor (user.email = lukaszbrowar@gmail.com, user.name = LukBro) -> brak `Co-Authored-By`
# w commitach baza..HEAD -> czyste drzewo poza dozwolonymi -> push -u -> gh pr create.
# Sciezka wrazliwa w diffie (.github/, pyproject.toml, alembic/versions/, CLAUDE.md, .claude/settings,
# .claude/hooks/, src/runner/secrets.py): PR powstaje, skrypt wypisuje `merge reczny` i niczego wiecej
# nie zmienia (zadnych etykiet, zatwierdzen ani scalania).
# Nigdy: --force, --amend, push z galezi main/master.
# Kody wyjscia: 0 PR utworzony, 1 odmowa (sprawdzenie nie przeszlo, nic nie wypchniete),
# 2 blad narzedzia albo uzycia (brak dowodu). Ostatnia linia stdout: `ODDAJ-PR: ok <url>` albo
# `ODDAJ-PR: odmowa <powod>`.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

OCZEKIWANY_EMAIL="lukaszbrowar@gmail.com"
OCZEKIWANA_NAZWA="LukBro"
SCIEZKI_WRAZLIWE=(".github/" "pyproject.toml" "alembic/versions/" "CLAUDE.md" ".claude/settings" ".claude/hooks/" "src/runner/secrets.py")

TYTUL=""
TRESC=""
BAZA="origin/main"
DOZWOLONE=()

odmowa() {
  echo "BLAD: $*" >&2
  echo "ODDAJ-PR: odmowa $*"
  exit 1
}

blad() {
  echo "BLAD: $*" >&2
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --tytul) [ "$#" -ge 2 ] || blad "--tytul wymaga wartosci"; TYTUL="$2"; shift 2 ;;
    --tresc) [ "$#" -ge 2 ] || blad "--tresc wymaga wartosci"; TRESC="$2"; shift 2 ;;
    --baza) [ "$#" -ge 2 ] || blad "--baza wymaga wartosci"; BAZA="$2"; shift 2 ;;
    --dozwolone) [ "$#" -ge 2 ] || blad "--dozwolone wymaga wartosci"; DOZWOLONE+=("$2"); shift 2 ;;
    *) blad "nieznany argument: $1" ;;
  esac
done

[ -n "$TYTUL" ] || blad "brak --tytul"
[ -n "$TRESC" ] || blad "brak --tresc"
[ -s "$TRESC" ] || blad "plik z trescia PR nie istnieje albo jest pusty: $TRESC"

# --- autor ------------------------------------------------------------------------------------
EMAIL="$(git config user.email)" || EMAIL=""
NAZWA="$(git config user.name)" || NAZWA=""
[ "$EMAIL" = "$OCZEKIWANY_EMAIL" ] || odmowa "git config user.email to '$EMAIL', oczekiwano $OCZEKIWANY_EMAIL"
[ "$NAZWA" = "$OCZEKIWANA_NAZWA" ] || odmowa "git config user.name to '$NAZWA', oczekiwano $OCZEKIWANA_NAZWA"

GALAZ="$(git branch --show-current)" || blad "git branch --show-current zakonczyl sie bledem"
[ -n "$GALAZ" ] || odmowa "detached HEAD - nie ma galezi do wypchniecia"
case "$GALAZ" in
  main|master) odmowa "galaz $GALAZ - nigdy nie pushuje na main ani master" ;;
esac

# --- Co-Authored-By ---------------------------------------------------------------------------
TRESCI_COMMITOW="$(git log --format='@@AUTORZY%x09%ae%x09%ce%x09%an%x09%cn%n%B' "$BAZA..HEAD" 2>&1)" \
  || blad "git log $BAZA..HEAD zakonczyl sie bledem: $TRESCI_COMMITOW"
# Autor i committer KAZDEGO wypychanego commita (nie tylko biezace git config): push jest nieodwracalny.
LINIE_AUTORZY="$(printf '%s\n' "$TRESCI_COMMITOW" | grep '^@@AUTORZY')"
while IFS=$'\t' read -r _znacznik autor committer nazwa_autora nazwa_committera; do
  [ -n "$_znacznik" ] || continue
  if [ "$autor" != "$OCZEKIWANY_EMAIL" ] || [ "$committer" != "$OCZEKIWANY_EMAIL" ]; then
    odmowa "commit galezi ma autora '$autor' albo committera '$committer', oczekiwano $OCZEKIWANY_EMAIL"
  fi
  if [ "$nazwa_autora" != "$OCZEKIWANA_NAZWA" ] || [ "$nazwa_committera" != "$OCZEKIWANA_NAZWA" ]; then
    odmowa "commit galezi ma nazwe autora '$nazwa_autora' albo committera '$nazwa_committera', oczekiwano $OCZEKIWANA_NAZWA"
  fi
done <<KONIEC_AUTORZY
$LINIE_AUTORZY
KONIEC_AUTORZY
if printf '%s\n' "$TRESCI_COMMITOW" | grep -qi '^co-authored-by:'; then
  odmowa "commit galezi ma linie Co-Authored-By (CLAUDE.md: zadnego wspolautora)"
fi

# --- czyste drzewo ----------------------------------------------------------------------------
STATUS="$(git status --porcelain --untracked-files=all 2>&1)" || blad "git status zakonczyl sie bledem: $STATUS"
BRUDNE=""
while IFS= read -r wiersz; do
  [ -n "$wiersz" ] || continue
  sciezka="${wiersz:3}"
  dozwolony=0
  for d in ${DOZWOLONE[@]+"${DOZWOLONE[@]}"}; do
    if [ "$sciezka" = "$d" ]; then
      dozwolony=1
    fi
  done
  if [ "$dozwolony" -eq 0 ]; then
    BRUDNE="$BRUDNE $sciezka"
  fi
done <<EOF
$STATUS
EOF
[ -z "$BRUDNE" ] || odmowa "drzewo robocze nie jest czyste, brudne:$BRUDNE"

# --- sciezki wrazliwe -------------------------------------------------------------------------
ZMIANY="$(git -c core.quotePath=false diff --no-ext-diff --no-renames --name-only "$BAZA...HEAD" 2>&1)" \
  || blad "git diff $BAZA...HEAD zakonczyl sie bledem: $ZMIANY"
WRAZLIWE=""
while IFS= read -r sciezka; do
  [ -n "$sciezka" ] || continue
  for wzorzec in "${SCIEZKI_WRAZLIWE[@]}"; do
    case "$sciezka" in
      "$wzorzec"*) WRAZLIWE="$WRAZLIWE $sciezka"; break ;;
    esac
  done
done <<EOF
$ZMIANY
EOF

# --- push i PR --------------------------------------------------------------------------------
git push -u origin "$GALAZ" || blad "git push -u origin $GALAZ nie powiodl sie"
URL="$(gh pr create --base "${BAZA#origin/}" --head "$GALAZ" --title "$TYTUL" --body-file "$TRESC")" \
  || blad "gh pr create nie powiodl sie (galaz $GALAZ jest juz wypchnieta)"

if [ -n "$WRAZLIWE" ]; then
  echo "merge reczny: diff dotyka sciezek wrazliwych:$WRAZLIWE"
fi
echo "ODDAJ-PR: ok $URL"
