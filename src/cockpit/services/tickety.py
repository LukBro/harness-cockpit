"""Tickety przez runnera (HAR-104, BRO-111): kokpit nie rozmawia z dostawca ticketow.

Lista i pobranie ida do runnera PROJEKTU, bo to on ma konfiguracje i token tego repozytorium.
Odpowiedz runnera to dane z granicy: jej ksztalt sprawdzamy przed oddaniem dalej.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.models import Project

from .errors import NieZnaleziono, RunnerOffline, StanNiemozliwy
from .zapytania_runnera import Wyslij, zapytaj_runnera_online

KOMUNIKAT_OFFLINE = "wybór ticketu niedostępny — runner offline; użyj wklejenia ręcznego"
_POLA_SKROTU = ("id", "tytul", "stan")


def _skrot_poprawny(wpis: Any) -> bool:
    return isinstance(wpis, dict) and all(isinstance(wpis.get(p), str) for p in _POLA_SKROTU)


def _ticket_poprawny(wynik: Any) -> bool:
    return (
        isinstance(wynik, dict)
        and isinstance(wynik.get("ticket"), dict)
        and isinstance(wynik.get("ostrzezenia"), list)
        and all(isinstance(o, str) for o in wynik["ostrzezenia"])
    )


async def _zapytaj(
    s: AsyncSession, tenant_id: str, project_id: str, typ: str, dane: dict[str, Any],
    polaczone: frozenset[str], wyslij: Wyslij,
) -> Any:
    projekt = (await s.execute(
        select(Project).where(Project.id == project_id, Project.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if projekt is None:
        raise NieZnaleziono("projekt nieznany")
    try:
        _, wynik = await zapytaj_runnera_online(
            s, tenant_id, typ, {"projekt": projekt.path, **dane}, polaczone, wyslij,
            runner_id=projekt.runner_id)
    except RunnerOffline:
        raise RunnerOffline(KOMUNIKAT_OFFLINE) from None
    return wynik


async def lista(
    s: AsyncSession, tenant_id: str, project_id: str, filtr: str | None,
    polaczone: frozenset[str], wyslij: Wyslij,
) -> list[dict[str, Any]]:
    wynik = await _zapytaj(
        s, tenant_id, project_id, "ticket_lista", {"filtr": filtr}, polaczone, wyslij)
    if not isinstance(wynik, list) or not all(_skrot_poprawny(w) for w in wynik):
        raise StanNiemozliwy("runner zwrócił nieczytelną listę ticketów")
    return wynik


async def pobierz(
    s: AsyncSession, tenant_id: str, project_id: str, id_ticketu: str,
    polaczone: frozenset[str], wyslij: Wyslij,
) -> dict[str, Any]:
    wynik = await _zapytaj(
        s, tenant_id, project_id, "ticket_pobierz", {"id": id_ticketu}, polaczone, wyslij)
    if not _ticket_poprawny(wynik):
        raise StanNiemozliwy("runner zwrócił nieczytelny ticket")
    return wynik
