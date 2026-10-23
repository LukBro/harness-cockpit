"""Reguly runow. Jedyne miejsce, ktore dotyka bazy w sprawie runow.

api/ nie wykonuje zapytan - sprawdza to `tools/check_layers.py` w CI.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.models import Event, Run, Runner, RunStatus, Tenant

from .errors import BrakDostepu, NaruszonyNiezmiennik, NieZnaleziono, StanNiemozliwy
from .invariants import Niezmiennik


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


async def utworz_run(s: AsyncSession, tenant_id: str, workflow: str) -> Run:
    """Run powstaje tylko wtedy, gdy zespol ma runnera online."""
    runner = (
        await s.execute(select(Runner).where(Runner.tenant_id == tenant_id).limit(1))
    ).scalar_one_or_none()
    if runner is None:
        raise StanNiemozliwy("runner offline - uruchom runnera na swojej maszynie")

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
    status: RunStatus | None = None,
) -> Event:
    if cost_usd == 0.0 and cost_source == "unavailable":
        # BRO-87: zero znaczy ZMIERZONE zero. Brak pomiaru to None.
        raise NaruszonyNiezmiennik(
            Niezmiennik.measured_or_null,
            "koszt 0 przy zrodle 'unavailable' - brak pomiaru zapisz jako None",
        )

    seq = (
        await s.execute(
            select(func.coalesce(func.max(Event.seq), 0)).where(Event.run_id == run.id)
        )
    ).scalar_one() + 1

    ev = Event(tenant_id=run.tenant_id, run_id=run.id, seq=seq, step_id=step_id,
               agent_id=agent_id, kind=kind, text=text, cost_usd=cost_usd,
               cost_source=cost_source, tokens=tokens, duration_ms=duration_ms)
    s.add(ev)
    if status is not None:
        run.status = status
    await s.commit()
    return ev


async def zdarzenia_runu(s: AsyncSession, run: Run) -> list[Event]:
    return list(
        (
            await s.execute(select(Event).where(Event.run_id == run.id).order_by(Event.seq))
        ).scalars().all()
    )
