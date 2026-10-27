"""Reguly runow. Jedyne miejsce, ktore dotyka bazy w sprawie runow.

api/ nie wykonuje zapytan - sprawdza to `tools/check_layers.py` w CI.
"""
from __future__ import annotations

import hashlib
from collections.abc import Collection
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.models import Event, Run, Runner, RunStatus, Tenant

from .errors import BrakDostepu, NaruszonyNiezmiennik, NieZnaleziono, StanNiemozliwy
from .walidacja import sprawdz_pomiar


_KONCZACE_KROK = frozenset({"step_finished", "step_failed"})
_PROBY_SEQ = 5  # ile razy powtarzamy wyliczenie seq po kolizji (HAR-25)


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
    s: AsyncSession, runner: Runner, nazwa: str, wersja: str
) -> Runner:
    runner.name, runner.version = nazwa, wersja
    runner.last_seen_at = datetime.now(timezone.utc)
    await s.commit()
    return runner


def runner_online(runner: Runner, polaczone: Collection[str]) -> bool:
    """Online: tylko runner z aktywnym strumieniem.

    Zmiana reguly BRO-109 (HAR-26): swiezy last_seen_at (< 60 s) NIE czyni
    runnera dostepnym. Runner zabity miedzy rejestracja a otwarciem strumienia
    byl "online" przez minute: run powstawal, a polecenie czekalo w kolejce
    bez odbiorcy. last_seen_at zostaje informacja, nie warunkiem.
    """
    return runner.id in polaczone


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


async def domyslny_tenant(s: AsyncSession) -> Tenant:
    t = (await s.execute(select(Tenant).limit(1))).scalar_one_or_none()
    if t is None:
        raise StanNiemozliwy("brak zespolu - zarejestruj sie najpierw")
    return t


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
_STATUS_PO_ZDARZENIU = {"step_finished": RunStatus.to_accept, "step_failed": RunStatus.failed}
_STATUSY_RUCHOME_ZDARZENIAMI = {RunStatus.running, RunStatus.to_accept}


def nastepny_status(obecny: RunStatus, kind: str) -> RunStatus:
    docelowy = _STATUS_PO_ZDARZENIU.get(kind)
    if docelowy is None or obecny not in _STATUSY_RUCHOME_ZDARZENIAMI:
        return obecny
    # pozniejsza porazka przykrywa sukces, ale sukces nie cofa porazki
    if obecny == RunStatus.to_accept and docelowy != RunStatus.failed:
        return obecny
    return docelowy


async def zapisz_zdarzenie(
    s: AsyncSession,
    run: Run,
    *,
    kind: str,
    step_id: str = "",
    agent_id: str = "",
    text: str = "",
    cost_usd: float | None = None,
    cost_source: str = "unavailable",
    tokens: int | None = None,
    duration_ms: int | None = None,
    wyslane_output: int | None = None,
) -> Event:
    naruszenie = sprawdz_pomiar(cost_usd, cost_source)
    if naruszenie is not None:
        raise NaruszonyNiezmiennik(naruszenie.niezmiennik, naruszenie.szczegol)

    # po rollbacku obiekty sesji sa wygaszone - trzymamy same napisy
    run_id, tenant_id = run.id, run.tenant_id

    for _ in range(_PROBY_SEQ):
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
                   agent_id=agent_id, kind=kind, text=text, cost_usd=cost_usd,
                   cost_source=cost_source, tokens=tokens, duration_ms=duration_ms)
        s.add(ev)
        run.status = nastepny_status(run.status, kind)
        try:
            await s.commit()
        except IntegrityError:
            # numer zajety przez rownolegly zapis: cofamy cala transakcje (zdarzenie
            # i zmiany runu), wczytujemy run od nowa i liczymy numer, status i reason
            # jeszcze raz. Rollback zamiast SAVEPOINT - dziala tak samo na SQLite i PG.
            await s.rollback()
            await s.refresh(run)
            continue
        return ev

    raise StanNiemozliwy(
        f"nie udalo sie nadac numeru zdarzenia po {_PROBY_SEQ} probach - ponow zapis"
    )


async def zdarzenia_runu(s: AsyncSession, run: Run) -> list[Event]:
    return list(
        (
            await s.execute(select(Event).where(Event.run_id == run.id).order_by(Event.seq))
        ).scalars().all()
    )
