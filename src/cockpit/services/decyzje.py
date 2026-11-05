"""Decyzja czlowieka na bramce approval (HAR-172, dawny HAR-102).

Zatwierdzenie wznawia run (polecenie `resume` dla runnera), odrzucenie konczy go
statusem `rejected` z powodem. Stan czekania trzyma tabela krokow (HAR-78), wiec
przezywa restart kokpitu. Serwis nie zna szyny: polecenie i zdarzenie oddaje trasie.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.models import Event, Run, RunStatus, StepStatus
from cockpit.services import kroki, runs, walidacja

from .errors import NaruszonyNiezmiennik, NiespojneDane, StanNiemozliwy

DECYZJE = ("approve", "reject")


@dataclass(frozen=True)
class Decyzja:
    run: Run
    step_id: str                    # krok, ktory czekal na bramce
    polecenie: dict | None          # approve: polecenie `resume` dla runnera; reject: None
    zdarzenie: Event                # zapisane zdarzenie `decision`


async def podejmij_decyzje(
    s: AsyncSession, run_id: str, tenant_id: str, decyzja: str, powod: str | None,
) -> Decyzja:
    """Zatwierdza albo odrzuca run czekajacy na bramce; kazda odmowa zostawia baze bez zmian.

    Kolejnosc sprawdzen: decyzja, run zespolu, stan (pod blokada wiersza runu),
    powod odrzucenia, niezmiennik `approval_required`.
    """
    if decyzja not in DECYZJE:
        raise NiespojneDane(f"nieznana decyzja: dozwolone {', '.join(DECYZJE)}")
    run = await runs.run_dla_najemcy(s, run_id, tenant_id)
    # blokada wiersza runu: druga rownolegla decyzja czeka i po odblokowaniu widzi
    # status zmieniony przez pierwsza, wiec dostaje 409 (wyscig-sprawdz-zapisz)
    await s.refresh(run, with_for_update=True)
    czekajace = [
        k for k in await kroki.kroki_runu(s, run) if k.status == StepStatus.waiting
    ]
    if run.status != RunStatus.waiting or not czekajace:
        await s.rollback()
        raise StanNiemozliwy("run nie czeka na decyzje czlowieka")
    step_id = czekajace[0].step_id
    powod_czysty = powod.strip() if powod is not None else ""
    if decyzja == "reject" and not powod_czysty:
        await s.rollback()
        raise NiespojneDane("odrzucenie wymaga powodu")
    naruszenie = walidacja.sprawdz_decyzje(True, decyzja)
    if naruszenie is not None:
        await s.rollback()
        raise NaruszonyNiezmiennik(naruszenie.niezmiennik, naruszenie.szczegol)

    tekst = f"{decyzja}: {powod_czysty}" if powod_czysty else decyzja
    zdarzenie = Event(
        tenant_id=run.tenant_id, run_id=run.id,
        seq=await runs._nastepny_seq(s, run.id, run.tenant_id),
        step_id=step_id, kind="decision", text=tekst, text_full_len=len(tekst),
    )
    s.add(zdarzenie)
    polecenie: dict | None = None
    if decyzja == "approve":
        run.status = RunStatus.running
        polecenie = {
            "type": "resume", "run_id": run.id, "step_id": step_id,
            "decyzja": "approve", "powod": powod_czysty or None,
        }
    else:
        run.status = RunStatus.rejected
        run.reason = powod_czysty
    await s.commit()
    return Decyzja(run=run, step_id=step_id, polecenie=polecenie, zdarzenie=zdarzenie)
