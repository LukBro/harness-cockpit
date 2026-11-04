"""Projekty zespolu (HAR-79): katalog repozytorium na maszynie z runnera.

Kokpit nie widzi dysku uzytkownika: sciezke i stos podaje runner (`detect_stack`), a w bazie
leza wylacznie metadane projektu. Zapisujemy sciezke ROZWIAZANA przez runnera (dowiazania,
kropki), wiec ten sam katalog pod dwiema nazwami to jeden projekt.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.models import Project, Runner

from . import zapytania_runnera
from .errors import NieZnaleziono, RunnerOffline, StanNiemozliwy
from .runs import runner_online
from .zapytania_runnera import Wyslij

_MAX_SCIEZKA = 4096   # dlugosci kolumn `project`
_MAX_NAZWA = 200
KOMUNIKAT_OFFLINE = "runner offline - uruchom runnera na swojej maszynie (cockpit-runner)"


def _zly_ksztalt(powod: str) -> StanNiemozliwy:
    return StanNiemozliwy(f"odpowiedz runnera ma zly ksztalt ({powod})")


def _niepusty_napis(wynik: dict, pole: str, maksimum: int) -> str:
    wartosc = wynik.get(pole)
    if not isinstance(wartosc, str) or not wartosc or len(wartosc) > maksimum:
        raise _zly_ksztalt(f"pole {pole} nie jest niepustym napisem do {maksimum} znakow")
    return wartosc


def _sprawdz_ksztalt(wynik: Any) -> tuple[str, str, list[str]]:
    """(sciezka, nazwa, stos); walidacja PRZED zapisem: zly ksztalt to nigdy projekt."""
    if not isinstance(wynik, dict):
        raise _zly_ksztalt("wynik nie jest slownikiem")
    sciezka = _niepusty_napis(wynik, "sciezka", _MAX_SCIEZKA)
    nazwa = _niepusty_napis(wynik, "nazwa", _MAX_NAZWA)
    stos = wynik.get("stack")
    if not isinstance(stos, list) or not all(isinstance(e, str) for e in stos):
        raise _zly_ksztalt("pole stack nie jest lista napisow")
    return sciezka, nazwa, list(stos)


def _iso(moment: datetime) -> str:
    """ISO 8601 z +00:00; SQLite oddaje daty bez strefy - to zawsze UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc).isoformat()
    return moment.astimezone(timezone.utc).isoformat()


def _pozycja(p: Project) -> dict:
    return {
        "id": p.id, "runner_id": p.runner_id, "path": p.path, "name": p.name,
        "stack": list(p.stack), "created_at": _iso(p.created_at),
    }


async def _istnieje(s: AsyncSession, tenant_id: str, runner_id: str, path: str) -> bool:
    return (await s.scalar(
        select(Project.id).where(
            Project.tenant_id == tenant_id, Project.runner_id == runner_id,
            Project.path == path)
    )) is not None


def _duplikat() -> StanNiemozliwy:
    return StanNiemozliwy("ten katalog jest juz projektem zespolu na tym runnerze")


async def podlacz_projekt(
    s: AsyncSession, tenant_id: str, runner_id: str, path: str,
    polaczone: frozenset[str], wyslij: Wyslij,
) -> dict:
    """Prosi wskazanego runnera o `detect_stack` i zapisuje projekt zespolu."""
    runner = (await s.execute(
        select(Runner).where(Runner.id == runner_id, Runner.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if runner is None:
        raise NieZnaleziono("runner nie istnieje")
    if not runner_online(runner, polaczone):
        raise RunnerOffline(KOMUNIKAT_OFFLINE)
    _, wynik = await zapytania_runnera.zapytaj_runnera_online(
        s, tenant_id, "detect_stack", {"sciezka": path}, polaczone, wyslij, runner_id=runner_id)
    sciezka, nazwa, stos = _sprawdz_ksztalt(wynik)
    projekt = Project(
        tenant_id=tenant_id, runner_id=runner_id, path=sciezka, name=nazwa, stack=stos)
    s.add(projekt)
    try:
        await s.flush()
        pozycja = _pozycja(projekt)   # przed commitem: po nim obiekt bywa wygasniety
        await s.commit()
    except IntegrityError:
        # rownolegle podlaczenie tego samego katalogu: przegralismy z ograniczeniem unikalnosci
        await s.rollback()
        if await _istnieje(s, tenant_id, runner_id, sciezka):
            raise _duplikat() from None
        raise
    return pozycja


async def sciezka_projektu(s: AsyncSession, tenant_id: str, project_id: str) -> str:
    """Sciezka katalogu projektu zespolu na maszynie runnera (HAR-80); obcy projekt to NieZnaleziono."""
    sciezka = (await s.execute(
        select(Project.path).where(Project.id == project_id, Project.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if sciezka is None:
        raise NieZnaleziono("projekt nieznany")
    return sciezka


async def projekty_zespolu(s: AsyncSession, tenant_id: str) -> list[dict]:
    """Projekty zespolu od najnowszego."""
    projekty = (await s.execute(
        select(Project).where(Project.tenant_id == tenant_id)
        .order_by(Project.created_at.desc(), Project.id.desc())
    )).scalars().all()
    return [_pozycja(p) for p in projekty]
