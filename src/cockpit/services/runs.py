"""Reguly runow. Jedyne miejsce, ktore dotyka bazy w sprawie runow.

api/ nie wykonuje zapytan - sprawdza to `tools/check_layers.py` w CI.
"""
from __future__ import annotations

import hashlib
from collections.abc import Collection, Sequence
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.models import Event, Run, Runner, RunStatus

from cockpit.settings import settings

from .errors import (
    BrakDostepu, NaruszonyNiezmiennik, NiespojneDane, NieZnaleziono, StanNiemozliwy,
)
from .walidacja import sprawdz_pomiar


POWOD_RUNNER_OFFLINE = "runner offline"  # (HAR-37)
KIND_PRZERWANIA = "run_aborted"
_KONCZACE_KROK = frozenset({"step_finished", "step_failed"})
_PROBY_SEQ = 5  # ile razy powtarzamy wyliczenie seq po kolizji (HAR-25)
_OGRANICZENIE_SEQ = "uq_event_run_seq"  # nazwa z UniqueConstraint(run_id, seq) w modelu
_KOLUMNY_SEQ_SQLITE = frozenset({"event.run_id", "event.seq"})
_OGRANICZENIE_ZDARZENIA = "uq_event_run_event"  # UniqueConstraint(run_id, event_id) w modelu
_KOLUMNY_ZDARZENIA_SQLITE = frozenset({"event.run_id", "event.event_id"})


def _kolizja_unikalnosci(
    e: IntegrityError, nazwa_pg: str, kolumny_sqlite: frozenset[str],
) -> bool:
    """Czy to naruszenie wlasnie tego ograniczenia unikalnosci, a nie inny blad zapisu."""
    sqlstate = getattr(e.orig, "sqlstate", None)
    if sqlstate is not None:
        # PostgreSQL: rozpoznajemy po kodzie i nazwie ograniczenia, nie po tresci
        nazwa = getattr(getattr(e.orig, "orig", None), "constraint_name", None)
        return sqlstate == "23505" and nazwa == nazwa_pg
    # SQLite nie niesie nazwy ograniczenia, tylko liste kolumn w komunikacie
    prefiks = "UNIQUE constraint failed: "
    komunikat = str(e.orig)
    if not komunikat.startswith(prefiks):
        return False
    kolumny = {k.strip() for k in komunikat.removeprefix(prefiks).split(",")}
    return kolumny == kolumny_sqlite


def _kolizja_numeru(e: IntegrityError) -> bool:
    """Czy to naruszenie unikalnosci (run_id, seq), a nie inny blad zapisu (HAR-138)."""
    return _kolizja_unikalnosci(e, _OGRANICZENIE_SEQ, _KOLUMNY_SEQ_SQLITE)


def _kolizja_zdarzenia(e: IntegrityError) -> bool:
    """Czy to naruszenie unikalnosci (run_id, event_id): to samo zdarzenie dwa razy (HAR-39)."""
    return _kolizja_unikalnosci(e, _OGRANICZENIE_ZDARZENIA, _KOLUMNY_ZDARZENIA_SQLITE)


async def _zdarzenie_o_id(
    s: AsyncSession, run_id: str, tenant_id: str, event_id: str,
) -> Event | None:
    return (
        await s.execute(
            select(Event).where(
                Event.run_id == run_id, Event.tenant_id == tenant_id,
                Event.event_id == event_id,
            )
        )
    ).scalar_one_or_none()


async def _nastepny_seq(s: AsyncSession, run_id: str, tenant_id: str) -> int:
    return (
        await s.execute(
            select(func.coalesce(func.max(Event.seq), 0)).where(
                Event.run_id == run_id, Event.tenant_id == tenant_id
            )
        )
    ).scalar_one() + 1


def komunikat_niepelnego_strumienia(zapisano: int, wyslano: int) -> str | None:
    if zapisano == wyslano:
        return None
    return f"niepelny strumien: zapisano {zapisano} z {wyslano} zdarzen output"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def runner_po_tokenie(s: AsyncSession, authorization: str | None) -> Runner:
    if not authorization or not authorization.startswith("Bearer "):
        raise BrakDostepu("brak tokenu runnera")
    token = authorization.removeprefix("Bearer ")
    r = (
        await s.execute(select(Runner).where(Runner.token_hash == hash_token(token)))
    ).scalar_one_or_none()
    if r is None:
        raise BrakDostepu("token nieznany - wygeneruj nowy w Ustawieniach")
    return r


async def zamelduj_runnera(
    s: AsyncSession, runner: Runner, nazwa: str, wersja: str,
    backendy: Sequence[dict],
) -> Runner:
    runner.name, runner.version = nazwa, wersja
    runner.backends = list(backendy)    # zastepuje deklaracje, nie mutuje w miejscu
    runner.last_seen_at = datetime.now(timezone.utc)
    await s.commit()
    return runner


async def odnotuj_heartbeat(s: AsyncSession, runner: Runner) -> Runner:
    runner.last_seen_at = datetime.now(timezone.utc)
    await s.commit()
    return runner


def runner_online(
    runner: Runner, polaczone: Collection[str], teraz: datetime | None = None,
) -> bool:
    """Online: otwarty strumien ORAZ heartbeat mlodszy niz `runner_offline_after_s`.

    Zmiana reguly HAR-26 (HAR-35, D12): sam strumien nie wystarcza. Strumien
    polotwarty (runner zabity `kill -9`, zerwana siec) bywa po stronie serwera
    "otwarty" dlugo, wiec dowodem zycia jest heartbeat. Wiek rowny progowi to offline.
    """
    if runner.id not in polaczone or runner.last_seen_at is None:
        return False
    widziany = runner.last_seen_at
    if widziany.tzinfo is None:       # SQLite oddaje daty bez strefy; zapisujemy UTC
        widziany = widziany.replace(tzinfo=timezone.utc)
    teraz = teraz or datetime.now(timezone.utc)
    return teraz - widziany < timedelta(seconds=settings.runner_offline_after_s)


async def utworz_run(
    s: AsyncSession,
    tenant_id: str,
    workflow: str,
    polaczone: Collection[str] = frozenset(),
) -> Run:
    """Run powstaje tylko wtedy, gdy zespol ma runnera z otwartym strumieniem
    (`polaczone`; HAR-2, HAR-26)."""
    runnery = (
        await s.execute(
            select(Runner).where(Runner.tenant_id == tenant_id).order_by(Runner.id)
        )
    ).scalars().all()
    runner = next(
        (r for r in runnery
         if runner_online(r, polaczone)),
        None,
    )
    if runner is None:
        raise StanNiemozliwy(
            "runner offline - uruchom runnera na swojej maszynie (cockpit-runner)"
        )

    run = Run(tenant_id=tenant_id, runner_id=runner.id, workflow=workflow,
              status=RunStatus.running)
    s.add(run)
    await s.commit()
    return run


async def run_dla_najemcy(s: AsyncSession, run_id: str, tenant_id: str) -> Run:
    """Kazde siegniecie po run przechodzi przez tenant_id (BRO-85)."""
    run = await s.get(Run, run_id)
    if run is None or run.tenant_id != tenant_id:
        raise NieZnaleziono("run nieznany")
    return run


async def run_runnera(s: AsyncSession, run_id: str, runner: Runner) -> Run:
    """Zdarzenia przyjmujemy tylko od runnera, ktoremu run zlecono (HAR-19).

    Run cudzego runnera jest dla niego nieznany - ten sam komunikat co dla
    nieistniejacego, zeby nie ujawniac istnienia runu.
    """
    run = await s.get(Run, run_id)
    if (
        run is None
        or run.tenant_id != runner.tenant_id
        or run.runner_id != runner.id
    ):
        raise NieZnaleziono("run nieznany")
    return run


# Status runu wylicza kokpit ze zdarzenia; deklaracja runnera nie jest zrodlem prawdy.
_STATUS_PO_ZDARZENIU = {
    "step_finished": RunStatus.to_accept,
    "step_failed": RunStatus.failed,
    "step_aborted": RunStatus.aborted,     # runner przerywa krok sam, np. brak miejsca (HAR-47)
}
_PORAZKI = frozenset({RunStatus.failed, RunStatus.aborted})
# `aborted` jest koncowy, bo nie ma go w zbiorze ruchomych (HAR-37).
_STATUSY_RUCHOME_ZDARZENIAMI = {RunStatus.running, RunStatus.to_accept}


def nastepny_status(obecny: RunStatus, kind: str) -> RunStatus:
    docelowy = _STATUS_PO_ZDARZENIU.get(kind)
    if docelowy is None or obecny not in _STATUSY_RUCHOME_ZDARZENIAMI:
        return obecny
    # pozniejsza porazka (failed albo aborted) przykrywa sukces, ale sukces nie cofa porazki
    if obecny == RunStatus.to_accept and docelowy not in _PORAZKI:
        return obecny
    return docelowy


async def zapisz_zdarzenie(
    s: AsyncSession,
    run: Run,
    *,
    event_id: str,
    kind: str,
    step_id: str = "",
    agent_id: str = "",
    agent_type: str = "",
    text: str = "",
    cost_usd: float | None = None,
    cost_source: str = "unavailable",
    tokens: int | None = None,
    duration_ms: int | None = None,
    wyslane_output: int | None = None,
    text_full_len: int | None = None,
) -> tuple[Event, bool]:
    """Zapisuje zdarzenie od runnera; zwraca `(zdarzenie, nowe)`.

    Ponowienie z tym samym `event_id` w tym runie zwraca zapisane zdarzenie
    i `False`: bez zapisu, bez zmiany statusu i powodu runu (HAR-39).
    """
    naruszenie = sprawdz_pomiar(cost_usd, cost_source)
    if naruszenie is not None:
        raise NaruszonyNiezmiennik(naruszenie.niezmiennik, naruszenie.szczegol)
    if text_full_len is not None and text_full_len < len(text):
        raise NiespojneDane(
            f"text_full_len ({text_full_len} znakow) jest krotsza niz przyslany tekst "
            f"({len(text)} znakow)"
        )
    # tekst ponad limit nigdy nie udaje calosci: znacznik i pelna dlugosc (HAR-56)
    pelna = max(len(text), text_full_len or 0)
    limit = settings.limit_tekstu_zdarzenia
    zapisany = text[:limit]
    obciety = pelna > len(zapisany)

    # po rollbacku obiekty sesji sa wygaszone - trzymamy same napisy
    run_id, tenant_id = run.id, run.tenant_id

    istniejace = await _zdarzenie_o_id(s, run_id, tenant_id, event_id)
    if istniejace is not None:
        return istniejace, False

    for _ in range(_PROBY_SEQ):
        # blokada wiersza: status odczytany tu nie zmieni sie do commitu, wiec powod
        # przerwania dostaje tylko zdarzenie, ktore faktycznie przestawilo run (HAR-47)
        # obiekt spoza sesji nie ma wiersza do zablokowania, a zapis zdarzenia i tak
        # padnie na kluczu obcym - blad ma wyjsc stamtad, nie z odswiezenia.
        # Obiekt wygaszony rollbackiem kolizji nadal jest w sesji (`in` daje True).
        if run in s:
            await s.refresh(run, with_for_update=True)
        status_przed = run.status
        seq = await _nastepny_seq(s, run_id, tenant_id)
        if kind in _KONCZACE_KROK and wyslane_output is not None:
            zapisano = (
                await s.execute(
                    select(func.count()).select_from(Event).where(
                        Event.run_id == run_id,
                        Event.tenant_id == tenant_id,
                        Event.step_id == step_id,
                        Event.kind == "output",
                    )
                )
            ).scalar_one()
            komunikat = komunikat_niepelnego_strumienia(zapisano, wyslane_output)
            if komunikat is not None:
                run.reason = komunikat

        ev = Event(tenant_id=tenant_id, run_id=run_id, seq=seq, step_id=step_id,
                   event_id=event_id, agent_id=agent_id, agent_type=agent_type, kind=kind, text=zapisany, cost_usd=cost_usd,
                   cost_source=cost_source, tokens=tokens, duration_ms=duration_ms,
                   text_truncated=obciety, text_full_len=pelna)
        s.add(ev)
        run.status = nastepny_status(status_przed, kind)
        if (kind == "step_aborted" and status_przed != RunStatus.aborted
                and run.status == RunStatus.aborted):
            run.reason = zapisany
        try:
            await s.commit()
        except IntegrityError as e:
            if _kolizja_zdarzenia(e):
                # ten sam event_id zapisany rownolegle: oddajemy zapisane, bez zmian runu
                await s.rollback()
                istniejace = await _zdarzenie_o_id(s, run_id, tenant_id, event_id)
                if istniejace is None:
                    raise   # kolizja z cudzym zespolem: nie jest to nasz duplikat
                return istniejace, False
            if not _kolizja_numeru(e):
                # inny blad zapisu (klucz obcy, NOT NULL): ponowienie tylko by go ukrylo
                await s.rollback()
                raise
            # numer zajety przez rownolegly zapis: cofamy cala transakcje (zdarzenie
            # i zmiany runu); poczatek petli wczytuje run od nowa i liczy numer, status
            # i reason jeszcze raz. Rollback zamiast SAVEPOINT - dziala tak samo na SQLite i PG.
            await s.rollback()
            continue
        return ev, True

    raise StanNiemozliwy(
        f"nie udalo sie nadac numeru zdarzenia po {_PROBY_SEQ} probach - ponow zapis"
    )


async def zdarzenia_runu(s: AsyncSession, run: Run) -> list[Event]:
    return list(
        (
            await s.execute(select(Event).where(Event.run_id == run.id).order_by(Event.seq))
        ).scalars().all()
    )


_STATUSY_TRWAJACE = (RunStatus.running, RunStatus.waiting)


async def przerwij_runy_offline(
    s: AsyncSession, teraz: datetime, polaczone: Collection[str],
) -> list[str]:
    """Run w toku lub oczekujacy na runnerze offline dostaje `aborted` (HAR-37).

    Status i zdarzenie przerwania zapisujemy w jednej transakcji. Update jest
    warunkowy: gdy `step_finished` z innej sesji zdazyl zmienic status, run
    zostaje jak byl i nie dostaje zdarzenia. Zwraca id faktycznie przerwanych.
    """
    wiersze = (
        await s.execute(
            select(Run, Runner)
            .join(Runner, (Run.runner_id == Runner.id) & (Run.tenant_id == Runner.tenant_id))
            .where(Run.status.in_(_STATUSY_TRWAJACE))
        )
    ).all()
    # po rollbacku obiekty sesji sa wygaszone - trzymamy same napisy
    cele = [(run.id, run.tenant_id) for run, runner in wiersze
            if not runner_online(runner, polaczone, teraz)]

    przerwane: list[str] = []
    for run_id, tenant_id in cele:
        for _ in range(_PROBY_SEQ):
            wynik = await s.execute(
                update(Run)
                .where(Run.id == run_id, Run.tenant_id == tenant_id,
                       Run.status.in_(_STATUSY_TRWAJACE))
                .values(status=RunStatus.aborted, reason=POWOD_RUNNER_OFFLINE)
            )
            if wynik.rowcount != 1:
                await s.rollback()      # wyscig: status zmieniony przez inna sesje
                break
            s.add(Event(tenant_id=tenant_id, run_id=run_id, kind=KIND_PRZERWANIA,
                        text=POWOD_RUNNER_OFFLINE,
                        seq=await _nastepny_seq(s, run_id, tenant_id)))
            try:
                await s.commit()
            except IntegrityError as e:
                await s.rollback()
                if not _kolizja_numeru(e):
                    raise
                continue    # numer zajety rownolegle: update i seq liczymy od nowa
            przerwane.append(run_id)
            break
        else:
            raise StanNiemozliwy(
                f"nie udalo sie nadac numeru zdarzenia po {_PROBY_SEQ} probach - ponow zapis"
            )
    return przerwane


async def zdarzenia_przerwania(
    s: AsyncSession, run_ids: Sequence[str],
) -> list[tuple[str, dict]]:
    """Ostatnie zdarzenie przerwania kazdego runu, w ksztalcie dla szyny (HAR-37)."""
    wynik: list[tuple[str, dict]] = []
    for run_id in run_ids:
        run = await s.get(Run, run_id)
        if run is None:
            continue
        ev = (
            await s.execute(
                select(Event)
                .where(Event.run_id == run_id, Event.tenant_id == run.tenant_id,
                       Event.kind == KIND_PRZERWANIA)
                .order_by(Event.seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if ev is None:
            continue
        wynik.append((run_id, {
            "seq": ev.seq, "kind": ev.kind, "text": ev.text, "step_id": ev.step_id,
            "status": run.status.value, "reason": run.reason,
            "text_truncated": ev.text_truncated, "text_full_len": ev.text_full_len,
        }))
    return wynik


async def polecenie_aktualne(
    s: AsyncSession, polecenie: dict, runner_id: str, tenant_id: str,
) -> bool:
    """Czy polecenie wolno jeszcze wyslac runnerowi (HAR-37).

    Odstepstwo od sygnatury z ticketu: `runner_id` i `tenant_id`, bo CLAUDE.md
    wymaga `tenant_id` w kazdym zapytaniu, a polecenie go nie niesie. Krok runu
    jest aktualny tylko dla runu `running` tego runnera i zespolu.
    """
    if polecenie.get("type") != "run_step":
        return True
    run_id = polecenie.get("run_id")
    if not run_id:
        return False
    status = (
        await s.execute(
            select(Run.status).where(
                Run.id == run_id, Run.tenant_id == tenant_id, Run.runner_id == runner_id
            )
        )
    ).scalar_one_or_none()
    return status == RunStatus.running
