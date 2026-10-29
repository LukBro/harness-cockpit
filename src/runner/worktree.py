"""Worktree per run poza repozytorium uzytkownika (HAR-52, no_git_write, run_dir_outside_repo).

Run dostaje wlasny `git worktree` w katalogu runu, odlaczony (detached) od SHA HEAD z chwili
startu - nie zajmuje galezi uzytkownika i nie rusza jego drzewa roboczego. Git idzie przez
`subprocess`. Modul nie ma commit/push/checkout galezi: silnik nie zapisuje w historii.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cockpit.services.errors import BladDziedziny, NaruszonyNiezmiennik
from cockpit.services.walidacja import sprawdz_katalog_runu

_SHA = re.compile(r"[0-9a-f]{40}")
_NIEDOZWOLONY_ID = {"", ".", ".."}


class BladWorktree(BladDziedziny):
    """Git nie dal rady (brak binarki, nie-repozytorium, kolizja) albo zly identyfikator runu."""


@dataclass(frozen=True)
class WorktreeRunu:
    sciezka: Path
    base_commit: str
    katalog_runu: Path


def _git(repo: Path | str, *argumenty: str) -> str:
    """Jedyne miejsce uruchamiajace gita: stdout albo BladWorktree ze stderr gita."""
    try:
        wynik = subprocess.run(
            ["git", "-C", str(repo), *argumenty],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as e:  # brak binarki, brak praw do uruchomienia
        raise BladWorktree(f"git {' '.join(argumenty)}: {e}") from e
    if wynik.returncode != 0:
        raise BladWorktree(f"git {' '.join(argumenty)}: {wynik.stderr.strip()}")
    return wynik.stdout


def katalog_runu(run_id: str, root: Path | str | None = None) -> Path:
    """Katalog runu: root, inaczej COCKPIT_RUNS_DIR, inaczej ~/.cockpit/runs; potem run_id."""
    if run_id in _NIEDOZWOLONY_ID or "/" in run_id or "\\" in run_id:
        raise BladWorktree(f"niedozwolony identyfikator runu: {run_id!r}")
    if root is not None:
        baza = Path(root)
    else:
        # Czytane przy wywolaniu: zmiana zmiennej dziala bez reimportu.
        env = os.environ.get("COCKPIT_RUNS_DIR")
        baza = Path(env) if env else Path.home() / ".cockpit" / "runs"
    return baza / run_id


def utworz_worktree(repo: Path | str, run_id: str, root: Path | str | None = None) -> WorktreeRunu:
    repo = Path(repo)
    katalog = katalog_runu(run_id, root)
    sha = _git(repo, "rev-parse", "HEAD").strip()
    if not _SHA.fullmatch(sha):
        raise BladWorktree(f"git rev-parse HEAD zwrocil nie-SHA: {sha!r}")
    # WHY: `repo` moze byc podkatalogiem repozytorium (monorepo) - porownujemy z korzeniem gita,
    # inaczej katalog runu obok podkatalogu, a w srodku repozytorium, przeszedlby walidator.
    korzen = Path(_git(repo, "rev-parse", "--show-toplevel").strip())
    # Przed jakimkolwiek zapisem na dysku: katalog runu nie moze lezec w repozytorium.
    naruszenie = sprawdz_katalog_runu(katalog, korzen)
    if naruszenie is not None:
        raise NaruszonyNiezmiennik(naruszenie.niezmiennik.value, naruszenie.szczegol)
    try:
        katalog.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise BladWorktree(f"nie mozna utworzyc katalogu runu {katalog}: {e}") from e
    sciezka = katalog / "worktree"
    _git(repo, "worktree", "add", "--detach", str(sciezka), sha)
    return WorktreeRunu(sciezka=sciezka, base_commit=sha, katalog_runu=katalog)


def usun_worktree(repo: Path | str, wt: WorktreeRunu) -> None:
    """Usuwa worktree razem z niezacommitowanymi zmianami runu; katalog runu zostaje."""
    _git(repo, "worktree", "remove", "--force", str(wt.sciezka))
    _git(repo, "worktree", "prune")


def liczba_zmienionych_plikow(repo: Path | str) -> int:
    """Zmienione i nieslediowne pliki w drzewie uzytkownika (ostrzezenie D8, emisja w HAR-80)."""
    # porcelain v1 jest stabilny niezaleznie od configu; --untracked-files=all
    # przebija status.showUntrackedFiles=no, ktory ukrylby nieslediowne.
    wyjscie = _git(repo, "status", "--porcelain", "--untracked-files=all")
    return sum(1 for linia in wyjscie.splitlines() if linia.strip())
