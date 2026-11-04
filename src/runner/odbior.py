"""Odbior runu (HAR-89): diff worktree runu wzgledem `base_commit`, wylacznie do odczytu.

Niezmienniki: `no_git_write` (tylko `git diff-index`, `git diff --no-index` i `git ls-files`,
zero zapisu w indeksie; `GIT_OPTIONAL_LOCKS=0` chroni pozostale wywolania). Diff plikow
sledzonych idzie przez plumbing `diff-index -p`, nie przez porcelane `git diff`.
WHY: porcelana `git diff` odswieza indeks (stat-cache plikow o zmienionym mtime) mimo
`GIT_OPTIONAL_LOCKS=0`; `diff-index` indeksu nie zmienia i nie pokazuje plikow tylko
stat-dirty. `run_dir_outside_repo` (pliki z `.cockpit/_run/` to artefakty
runu, nie zmiany do odbioru: wykluczenie jawne, niezalezne od `.gitignore`), `evidence_required`
(blad gita to blad, nie pusty diff). Komunikaty bledow sa stale: bez sciezek i stderr gita.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from cockpit.services.errors import BladDziedziny

SHA_PELNY = re.compile(r"[0-9a-f]{40}")

# Format diffu nie zalezy od konfiguracji uzytkownika (kolor, zewnetrzny program, textconv, prefiksy).
_FORMAT_DIFFU = [
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--src-prefix=a/",
    "--dst-prefix=b/",
]
_WYKLUCZ_RUN = ":(exclude).cockpit/_run"
# WHY stala modulu: polskie nazwy plikow doslownie w naglowku diffu. W funkcji mutmut zmienia
# literal na wielkie litery - mutant rownowazny (git czyta klucz i bool bez rozroznienia), ktory
# oblewalby bramke; linie chroni reczny mutant w tests/mutanty_reczne/HAR-89.toml.
_BEZ_CYTOWANIA_NAZW = "core.quotepath=false"


class BladOdbioru(BladDziedziny):
    """Git nie zebral diffu runu (brak binarki, nie-repozytorium, zagniezdzone repozytorium,
    zly commit bazowy)."""


def _git(worktree: Path, argumenty: list[str], dozwolone: tuple[int, ...]) -> bytes:
    return _git_z_kodem(worktree, argumenty, dozwolone)[0]


def _git_z_kodem(
    worktree: Path, argumenty: list[str], dozwolone: tuple[int, ...]
) -> tuple[bytes, int]:
    """Jedyne miejsce uruchamiajace gita: (stdout, kod) albo BladOdbioru (bez stderr w komunikacie)."""
    try:
        wynik = subprocess.run(
            ["git", "-c", _BEZ_CYTOWANIA_NAZW, "-C", str(worktree), *argumenty],
            capture_output=True,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except OSError:
        raise BladOdbioru("git nie jest dostepny") from None
    if wynik.returncode not in dozwolone:
        raise BladOdbioru("git nie zebral diffu runu")
    return wynik.stdout, wynik.returncode


def _tekst(surowe: bytes) -> str:
    return surowe.decode(errors="replace")


def zbierz_diff(worktree: Path, base_commit: str) -> str:
    if not SHA_PELNY.fullmatch(base_commit):
        raise BladOdbioru("base_commit nie jest SHA")
    diff = _tekst(_git(
        worktree,
        ["diff-index", "-p", *_FORMAT_DIFFU, base_commit, "--", ".", _WYKLUCZ_RUN],
        (0,),
    ))
    nowe = _git(
        worktree,
        ["ls-files", "--others", "--exclude-standard", "-z", ".", _WYKLUCZ_RUN],
        (0,),
    )
    for surowa in nowe.split(b"\0"):
        sciezka = os.fsdecode(surowa)
        if not sciezka or sciezka.startswith(".cockpit/_run/"):
            continue
        if sciezka.endswith("/"):
            # wpis zakonczony `/` to zagniezdzone repozytorium: git nie poda jego tresci
            raise BladOdbioru("worktree zawiera zagniezdzone repozytorium")
        # kod 1 = rozni sie (nowy plik), 0 = brak wpisu dla pliku (np. pusty), >=2 = blad
        wynik, kod = _git_z_kodem(
            worktree,
            ["diff", "--no-index", *_FORMAT_DIFFU, "--", "/dev/null", sciezka],
            (0, 1),
        )
        if kod == 1 and not wynik:
            # kod 1 ma znaczyc "rozni sie", wiec musi byc lata; pusty stdout to blad gita
            raise BladOdbioru("git nie zebral diffu runu")
        diff += _tekst(wynik)
    return diff
