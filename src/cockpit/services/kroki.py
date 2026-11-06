"""Stan krokow runu ze zdarzen (HAR-78).

Jedyne miejsce, ktore zapisuje i czyta wiersze `StepRun`. Status runu wylicza `runs.py`
ze stanow krokow i ze zdarzenia `run_finished`, nie z ostatniego zdarzenia kroku.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.models import Run, StepRun, StepStatus

MAPA_KROKOW: dict[str, StepStatus] = {
    "step_started": StepStatus.running,
    "step_finished": StepStatus.finished,
    "step_failed": StepStatus.failed,
    "step_aborted": StepStatus.failed,
    "step_skipped": StepStatus.skipped,
    "step_waiting": StepStatus.waiting,
}

KONCZACE_KROK = frozenset({"step_finished", "step_failed"})


def _zapisz_pomiar(
    wiersz: StepRun, cost_usd: float | None, tokens_in: int | None,
    tokens_out: int | None, num_turns: int | None, session_id: str | None,
) -> None:
    wiersz.cost_usd = cost_usd
    wiersz.tokens_in = tokens_in
    wiersz.tokens_out = tokens_out
    wiersz.num_turns = num_turns
    wiersz.session_id = session_id


async def zastosuj_zdarzenie_kroku(
    s: AsyncSession, run: Run, kind: str, step_id: str, agent_id: str,
    attempt: int | None, text: str,
    *, cost_usd: float | None = None, tokens_in: int | None = None,
    tokens_out: int | None = None, num_turns: int | None = None,
    session_id: str | None = None,
) -> None:
    """Ustawia stan proby kroku wynikajacy ze zdarzenia; bez `commit`.

    Wolane pod blokada wiersza runu w `zapisz_zdarzenie`, wiec odczyt i zapis
    wiersza kroku jest atomowy wzgledem innych zdarzen tego runu."""
    status = MAPA_KROKOW.get(kind)
    if status is None:
        return
    proba = 1 if attempt is None else attempt
    powod = text if status == StepStatus.failed else None
    # pomiar (HAR-174) tylko z konca kroku; None to brak pomiaru (NULL), nigdy 0
    koniec = kind in KONCZACE_KROK
    wiersz = (
        await s.execute(
            select(StepRun).where(
                StepRun.run_id == run.id, StepRun.tenant_id == run.tenant_id,
                StepRun.step_id == step_id, StepRun.agent_id == agent_id,
                StepRun.attempt == proba,
            )
        )
    ).scalar_one_or_none()
    if wiersz is None:
        nowy = StepRun(tenant_id=run.tenant_id, run_id=run.id, step_id=step_id,
                       agent_id=agent_id, attempt=proba, status=status, reason=powod)
        if koniec:
            _zapisz_pomiar(nowy, cost_usd, tokens_in, tokens_out, num_turns, session_id)
        s.add(nowy)
        return
    wiersz.status = status
    wiersz.reason = powod
    if koniec:
        _zapisz_pomiar(wiersz, cost_usd, tokens_in, tokens_out, num_turns, session_id)


async def kroki_runu(s: AsyncSession, run: Run) -> list[StepRun]:
    return list(
        (
            await s.execute(
                select(StepRun)
                .where(StepRun.run_id == run.id, StepRun.tenant_id == run.tenant_id)
                .order_by(StepRun.step_id, StepRun.agent_id, StepRun.attempt)
            )
        ).scalars().all()
    )


async def nieudane_kroki(s: AsyncSession, run: Run) -> list[str]:
    """Posortowane `step_id` krokow, ktorych ostatnia proba (ktoregos agenta) jest `failed`."""
    ostatnie: dict[tuple[str, str], StepRun] = {}
    for w in await kroki_runu(s, run):   # rosnaco po probie: pozniejsza podmienia wczesniejsza
        ostatnie[(w.step_id, w.agent_id)] = w
    return sorted({
        krok for (krok, _), w in ostatnie.items() if w.status == StepStatus.failed
    })
