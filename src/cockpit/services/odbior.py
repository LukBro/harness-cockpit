"""Odbior runu (HAR-89): diff runu zbiera runner, kokpit tylko pyta i pilnuje regul.

Diff jest tylko do odczytu: serwis nic nie zapisuje w bazie. Pytamy runnera TEGO runu
(`Run.runner_id`), nie pierwszego online - worktree lezy na jego dysku.

HAR-179: `integruj_run` i `commituj_run` to osobne akcje czlowieka po odbiorze. Kazda wysyla
zapytanie do runnera i dopiero po jego sukcesie zapisuje zdarzenie (`integrated`, `committed`)
z uzytkownikiem sesji; czas to `Event.at`, status runu sie nie zmienia. Wiersz runu jest
zablokowany (`FOR UPDATE`) od sprawdzenia stanu do commitu zdarzenia, wiec dwa rownolegle
zadania nie wykonaja akcji dwa razy.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.models import Event, Membership, Run, RunStatus, User

from .errors import StanNiemozliwy
from .runs import (
    KIND_ZACOMMITOWANO,
    KIND_ZINTEGROWANO,
    STATUSY_AKTYWNE,
    _nastepny_seq,
    run_dla_najemcy,
)
from .zapytania_runnera import Wyslij, zapytaj_runnera

_SHA_PELNY = re.compile(r"[0-9a-f]{40}")


async def run_do_odbioru(s: AsyncSession, run_id: str, tenant_id: str) -> Run:
    run = await run_dla_najemcy(s, run_id, tenant_id)
    if run.status in STATUSY_AKTYWNE:
        raise StanNiemozliwy("run jeszcze trwa")
    if run.base_commit is None:
        raise StanNiemozliwy("run nie ma base_commit")
    if run.runner_id is None:
        raise StanNiemozliwy("run nie ma runnera")
    return run


async def diff_runu(
    s: AsyncSession, run_id: str, tenant_id: str, polaczone: frozenset[str], wyslij: Wyslij,
) -> str:
    run = await run_do_odbioru(s, run_id, tenant_id)
    wynik = await zapytaj_runnera(
        s, tenant_id, run.runner_id, "collect",
        {"run_id": run.id, "base_commit": run.base_commit}, polaczone, wyslij)
    if not isinstance(wynik, dict) or not isinstance(wynik.get("diff"), str):
        raise StanNiemozliwy("runner zwrocil zly wynik collect")
    return wynik["diff"]


@dataclass(frozen=True)
class WynikOdbioru:
    """Zapisane zdarzenie odbioru i dane z odpowiedzi runnera, ktorych nie ma w jego tekscie.

    Zapisane zdarzenie jest w polu `zdarzenie`; trasa dostaje dodatkowo liste plikow
    (integrate) albo SHA (commit)."""

    zdarzenie: Event
    pliki: list[str] = field(default_factory=list)
    sha: str | None = None


async def _zdarzenie_istnieje(s: AsyncSession, run: Run, kind: str) -> bool:
    return (await s.execute(
        select(Event.id).where(
            Event.run_id == run.id, Event.tenant_id == run.tenant_id, Event.kind == kind
        ).limit(1)
    )).first() is not None


async def _run_pod_blokada(s: AsyncSession, run_id: str, tenant_id: str) -> Run:
    """Run do odbioru zablokowany do konca transakcji; tylko `to_accept` mozna odbierac."""
    run = await run_do_odbioru(s, run_id, tenant_id)
    # blokada wiersza: druga rownolegla akcja czeka i po odblokowaniu widzi zdarzenie
    # zapisane przez pierwsza, wiec dostaje 409 (wyscig-sprawdz-zapisz)
    await s.refresh(run, with_for_update=True)
    if run.status != RunStatus.to_accept:
        await s.rollback()
        raise StanNiemozliwy("run nie jest do odbioru")
    return run


async def _email_uzytkownika(s: AsyncSession, tenant_id: str, user_id: str) -> str:
    """Adres czlonka TEGO zespolu; uzytkownik spoza zespolu nie jest autorem akcji."""
    email = (await s.execute(
        select(User.email)
        .join(Membership, Membership.user_id == User.id)
        .where(User.id == user_id, Membership.tenant_id == tenant_id)
    )).scalar_one_or_none()
    if email is None:
        await s.rollback()
        raise StanNiemozliwy("uzytkownik nie nalezy do zespolu runu")
    return email


async def _zapisz_zdarzenie_odbioru(
    s: AsyncSession, run: Run, kind: str, tresc: dict,
) -> Event:
    tekst = json.dumps(tresc)
    zdarzenie = Event(
        tenant_id=run.tenant_id, run_id=run.id,
        seq=await _nastepny_seq(s, run.id, run.tenant_id),
        kind=kind, text=tekst, text_full_len=len(tekst),
    )
    s.add(zdarzenie)
    await s.commit()
    return zdarzenie


async def integruj_run(
    s: AsyncSession, run_id: str, tenant_id: str, user_id: str,
    polaczone: frozenset[str], wyslij: Wyslij,
) -> WynikOdbioru:
    """Przenosi zmiany runu do drzewa uzytkownika (jeden raz) i zapisuje zdarzenie `integrated`."""
    run = await _run_pod_blokada(s, run_id, tenant_id)
    if await _zdarzenie_istnieje(s, run, KIND_ZINTEGROWANO):
        await s.rollback()
        raise StanNiemozliwy("zmiany runu zostaly juz zintegrowane")
    email = await _email_uzytkownika(s, tenant_id, user_id)
    wynik = await zapytaj_runnera(
        s, tenant_id, run.runner_id, "integrate",
        {"run_id": run.id, "base_commit": run.base_commit}, polaczone, wyslij)
    pliki = wynik.get("pliki") if isinstance(wynik, dict) else None
    if (not isinstance(pliki, list) or not pliki
            or not all(isinstance(p, str) for p in pliki)):
        raise StanNiemozliwy("runner zwrocil zly wynik integrate")
    zdarzenie = await _zapisz_zdarzenie_odbioru(
        s, run, KIND_ZINTEGROWANO, {"user_id": user_id, "email": email})
    return WynikOdbioru(zdarzenie=zdarzenie, pliki=pliki)


async def commituj_run(
    s: AsyncSession, run_id: str, tenant_id: str, user_id: str,
    polaczone: frozenset[str], wyslij: Wyslij,
) -> WynikOdbioru:
    """Zapisuje zmiany runu w historii repozytorium (jeden raz, po `integrated`) i zdarzenie
    `committed` z SHA. Opis commita buduje kokpit, liste plikow liczy runner."""
    run = await _run_pod_blokada(s, run_id, tenant_id)
    if not await _zdarzenie_istnieje(s, run, KIND_ZINTEGROWANO):
        await s.rollback()
        raise StanNiemozliwy("zmiany runu nie zostaly jeszcze zintegrowane")
    if await _zdarzenie_istnieje(s, run, KIND_ZACOMMITOWANO):
        await s.rollback()
        raise StanNiemozliwy("zmiany runu zostaly juz zapisane w historii repozytorium")
    email = await _email_uzytkownika(s, tenant_id, user_id)
    wynik = await zapytaj_runnera(
        s, tenant_id, run.runner_id, "commit",
        {"run_id": run.id, "base_commit": run.base_commit, "opis": f"cockpit: run {run.id}"},
        polaczone, wyslij)
    sha = wynik.get("sha") if isinstance(wynik, dict) else None
    if not isinstance(sha, str) or not _SHA_PELNY.fullmatch(sha):
        raise StanNiemozliwy("runner zwrocil zly wynik commit")
    zdarzenie = await _zapisz_zdarzenie_odbioru(
        s, run, KIND_ZACOMMITOWANO, {"user_id": user_id, "email": email, "sha": sha})
    return WynikOdbioru(zdarzenie=zdarzenie, sha=sha)
