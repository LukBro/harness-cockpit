"""Sonda ekosystemu (HAR-171): czy komendy projektu (testy, lint, typecheck) w ogole dzialaja.

Runner dostaje stos wykryty po znacznikach w korzeniu repozytorium, uruchamia komendy z tabeli
i zwraca propozycje `project.yaml`: do `capabilities` trafiaja tylko komendy, ktore sie
uruchomily, zakonczyly kodem 0 i cos wypisaly (`evidence_required`: kod 0 bez wyjscia to brak
dowodu, nie obecnosc). Propozycja nie jest zapisywana w repozytorium.

Sonda ustawia `PYTHONDONTWRITEBYTECODE=1` i `BUNDLE_FROZEN=true` oraz dodaje `-p no:cacheprovider`
(pytest), `--no-cache` (ruff) i `--tsBuildInfoFile /dev/null` (tsc). Sprawdzone tylko dla `tsc`
(zdrowy projekt i `composite: true`: kod 0, wyjscie niepuste, w katalogu nic nie przybywa);
pytest i ruff pokrywaja testy na atrapach. Dla `bundle`, `go` i `npm` brak zapisu NIE jest
sprawdzony ani gwarantowany; testy projektu moga pisac swoje pliki.

Powody nieobecnosci sa stale i nie niosa tresci wyjscia ani argumentow komendy (moga miec sekret).
Proces: bez powloki, wlasna grupa procesow zabijana po limicie, stdin z /dev/null; wzor procesu
to `runner.dowody`. Komenda docelowa capability jest ta sama, ktora przeszla probe, wiec przechodzi
tez krok `system: check` (kod 0 i niepuste wyjscie).
"""
from __future__ import annotations

import asyncio
import errno
import io
import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from runner.dowody import _uruchom, _Wyjscie
from runner.operacje import _wykryj_stos

POWOD_BRAK_KOMENDY = "brak komendy"
POWOD_BLAD_STARTU = "błąd uruchomienia"   # inny OSError niz brak pliku/praw (np. EMFILE)
POWOD_LIMIT_CZASU = "limit czasu"
POWOD_PUSTE_WYJSCIE = "puste wyjście"

# `tsc --noEmit` przy sukcesie milczy, a kod 0 bez wyjscia to brak dowodu: --extendedDiagnostics
# wypisuje statystyki. `--tsBuildInfoFile /dev/null` nie zostawia tsbuildinfo (takze przy
# `composite`). `npx --no --` uzywa tylko lokalnego `tsc` (npm 10.9.9: bez `--` argument `tsc`
# bylby wartoscia `--no`); bez lokalnego typescript trafia na obcy pakiet `tsc` i konczy kodem 1.
TYPECHECK_NODE = "npx --no -- tsc --noEmit --extendedDiagnostics --tsBuildInfoFile /dev/null"

# stos -> [(capability, komenda docelowa (do project.yaml), komenda probna (bez zapisu w repo))]
TABELA: dict[str, list[tuple[str, str, str]]] = {
    "python": [
        ("test_narrow", "pytest -q", "pytest -q -p no:cacheprovider"),
        ("lint", "ruff check .", "ruff check --no-cache ."),
    ],
    "ruby": [
        ("test_narrow", "bundle exec rspec", "bundle exec rspec"),
        ("lint", "bundle exec rubocop", "bundle exec rubocop"),
    ],
    "go": [
        ("test_narrow", "go test ./...", "go test ./..."),
        ("lint", "go vet ./...", "go vet ./..."),
    ],
    "node": [
        ("test_narrow", "npm test", "npm test"),
        ("typecheck", TYPECHECK_NODE, TYPECHECK_NODE),
    ],
}


@dataclass(frozen=True)
class WynikCapability:
    obecna: bool
    powod: str   # "" gdy obecna; inaczej jedna ze stalych wyzej albo "kod wyjścia N"


def _srodowisko() -> dict[str, str]:
    # PATH zostaje: bez niego nie znajdziemy narzedzi projektu.
    # BUNDLE_FROZEN: bundler nie zapisuje Gemfile.lock.
    return {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "BUNDLE_FROZEN": "true"}


async def sprawdz_capability(komenda: str, worktree: Path, limit_s: float) -> WynikCapability:
    """Uruchamia `komenda` w `worktree` i ocenia, czy jest obecna (kod 0 i niepuste wyjscie)."""
    try:
        argv = shlex.split(komenda)
    except ValueError:
        argv = []
    if not argv:
        return WynikCapability(False, POWOD_BRAK_KOMENDY)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=worktree, env=_srodowisko(), start_new_session=True,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
    except (FileNotFoundError, PermissionError, NotADirectoryError, ValueError):
        # brak pliku, brak prawa wykonania, skladnik sciezki nie jest katalogiem, NUL w argumencie
        return WynikCapability(False, POWOD_BRAK_KOMENDY)
    except OSError as blad:
        if blad.errno == errno.ENOEXEC:   # plik bez shebanga i nie binarka
            return WynikCapability(False, POWOD_BRAK_KOMENDY)
        return WynikCapability(False, POWOD_BLAD_STARTU)
    wyjscie = _Wyjscie()
    przekroczony = await _uruchom(proc, wyjscie, limit_s)
    if przekroczony:
        return WynikCapability(False, POWOD_LIMIT_CZASU)
    if proc.returncode != 0:
        return WynikCapability(False, f"kod wyjścia {proc.returncode}")
    if not wyjscie.ma_tresc:
        return WynikCapability(False, POWOD_PUSTE_WYJSCIE)
    return WynikCapability(True, "")


def _yaml(stos: list[str], capabilities: dict[str, str]) -> str:
    mapa = CommentedMap()
    mapa["stack"] = CommentedSeq(stos)
    mapa["capabilities"] = CommentedMap(capabilities)
    y = YAML()
    y.width = 4096
    wyjscie = io.StringIO()
    y.dump(mapa, wyjscie)
    return wyjscie.getvalue()


async def zaproponuj(worktree: Path, limit_s: float, stos: list[str] | None = None) -> dict:
    """Propozycja `project.yaml`: stos, capabilities (tylko obecne), status.

    `stos` podaje wolajacy (operacja `probe` wykrywa go raz); bez niego stos wykrywa
    `operacje._wykryj_stos`. Capability o tej samej nazwie w dwoch ekosystemach (test_narrow):
    wygrywa pierwsza dzialajaca komenda w kolejnosci stosu, a gdy zadna - status pierwszej.
    Status nie wymienia przegranych kandydatow."""
    if stos is None:
        stos = (await asyncio.to_thread(_wykryj_stos, str(worktree)))["stack"]
    pozycje = [p for ekosystem in stos for p in TABELA.get(ekosystem, [])]
    wyniki = await asyncio.gather(
        *(sprawdz_capability(proba, worktree, limit_s) for _, _, proba in pozycje))
    capabilities: dict[str, str] = {}
    status: dict[str, dict] = {}
    for (nazwa, docelowa, _), wynik in zip(pozycje, wyniki, strict=True):
        if nazwa in capabilities:
            continue
        if nazwa not in status or wynik.obecna:
            status[nazwa] = {"komenda": docelowa, "obecna": wynik.obecna, "powod": wynik.powod}
        if wynik.obecna:
            capabilities[nazwa] = docelowa
    return {"stack": stos, "capabilities": capabilities, "status": status,
            "yaml": _yaml(stos, capabilities)}
