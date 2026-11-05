"""Checkpoint runu na dysku: stan potrzebny, by zawieszony run przezyl smierc silnika (HAR-172).

Plik `checkpoint.json` lezy w katalogu runu (poza repozytorium uzytkownika). Zapis jest atomowy:
plik tymczasowy w tym samym katalogu, `fsync`, `os.replace` - czytajacy widzi stary albo nowy
komplet, nigdy polowe. Odczyt tlumaczy kazdy blad wejscia (dysk, kodowanie, JSON, ksztalt)
na `BladCheckpointu`.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from cockpit.services.errors import BladDziedziny

PLIK_CHECKPOINTU = "checkpoint.json"
ZMIENNA_PAUZY = "COCKPIT_TEST_PAUSE_AFTER_CHECKPOINT"


class BladCheckpointu(BladDziedziny):
    """Checkpoint nie daje sie zapisac albo odczytac; tekst to powod."""


@dataclass(frozen=True)
class Oczekiwanie:
    step_id: str
    od: str                      # ISO 8601 UTC


@dataclass(frozen=True)
class StanKrokuCheckpointu:
    stan: str                    # StanKroku.value
    proby: int                   # liczba startow kroku (0 = nie startowal)


@dataclass(frozen=True)
class Checkpoint:
    run_id: str
    repozytorium: str
    workflow: str
    base_commit: str
    worktree: str
    kroki: dict[str, StanKrokuCheckpointu]
    waiting_on: Oczekiwanie | None


def zapisz_checkpoint(katalog_runu: Path, cp: Checkpoint) -> Path:
    """Zapisuje checkpoint atomowo; blad dysku to `BladCheckpointu`."""
    cel = katalog_runu / PLIK_CHECKPOINTU
    # mutant None jest rownowazny (json czyta ensure_ascii jako bool);
    # wariant True chroni reczny mutant
    tresc = json.dumps(asdict(cp), ensure_ascii=False)  # pragma: no mutate
    tymczasowy: str | None = None
    try:
        fd, tymczasowy = tempfile.mkstemp(
            dir=katalog_runu, prefix=".tmp-", suffix=".json")
        # mutant UTF-8 jest rownowazny (nazwa kodeka bez rozroznienia wielkosci liter);
        # brak encoding chroni reczny mutant
        with os.fdopen(fd, "w", encoding="utf-8") as f:  # pragma: no mutate
            f.write(tresc)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tymczasowy, cel)
    except OSError as e:
        if tymczasowy is not None:
            with contextlib.suppress(OSError):
                os.unlink(tymczasowy)
        raise BladCheckpointu(f"zapis checkpointu {cel}: {e}") from e
    return cel


def _napis(wartosc: Any, opis: str) -> str:
    if not isinstance(wartosc, str):
        raise BladCheckpointu(f"checkpoint: {opis} nie jest napisem")
    return wartosc


def _obiekt(wartosc: Any, opis: str) -> dict:
    if not isinstance(wartosc, dict):
        raise BladCheckpointu(f"checkpoint: {opis} nie jest obiektem")
    return wartosc


def _krok(id_kroku: str, wartosc: Any) -> StanKrokuCheckpointu:
    dane = _obiekt(wartosc, f"krok {id_kroku}")
    proby = dane.get("proby")
    # bool jest podklasa int: `proby: true` to zly typ
    if not isinstance(proby, int) or isinstance(proby, bool):
        raise BladCheckpointu(f"checkpoint: proby kroku {id_kroku} nie sa liczba calkowita")
    return StanKrokuCheckpointu(_napis(dane.get("stan"), f"stan kroku {id_kroku}"), proby)


def _oczekiwanie(wartosc: Any) -> Oczekiwanie | None:
    if wartosc is None:
        return None
    dane = _obiekt(wartosc, "waiting_on")
    return Oczekiwanie(
        _napis(dane.get("step_id"), "waiting_on.step_id"), _napis(dane.get("od"), "waiting_on.od"))


def wczytaj_checkpoint(katalog_runu: Path) -> Checkpoint:
    """Odczyt checkpointu; kazdy blad wejscia to `BladCheckpointu`."""
    plik = katalog_runu / PLIK_CHECKPOINTU
    try:
        # mutant UTF-8 jest rownowazny (nazwa kodeka bez rozroznienia wielkosci liter);
        # brak encoding chroni reczny mutant
        tekst = plik.read_text(encoding="utf-8")  # pragma: no mutate
    except (OSError, UnicodeDecodeError) as e:
        raise BladCheckpointu(f"odczyt checkpointu {plik}: {e}") from e
    try:
        surowe = json.loads(tekst)
    except (json.JSONDecodeError, RecursionError) as e:
        raise BladCheckpointu(f"checkpoint {plik} nie jest poprawnym JSON: {e}") from e
    dane = _obiekt(surowe, "korzen")
    if "waiting_on" not in dane:
        raise BladCheckpointu("checkpoint: brak klucza waiting_on")
    kroki = _obiekt(dane.get("kroki"), "kroki")
    return Checkpoint(
        run_id=_napis(dane.get("run_id"), "run_id"),
        repozytorium=_napis(dane.get("repozytorium"), "repozytorium"),
        workflow=_napis(dane.get("workflow"), "workflow"),
        base_commit=_napis(dane.get("base_commit"), "base_commit"),
        worktree=_napis(dane.get("worktree"), "worktree"),
        kroki={i: _krok(i, w) for i, w in kroki.items()},
        waiting_on=_oczekiwanie(dane["waiting_on"]),
    )
