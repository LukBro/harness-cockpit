"""Odbior runu - ekran dowodow (HAR-170): twierdzenia, dowody silnika, braki, ryzyko, decyzja.

Odpowiedz sklada sie WYLACZNIE z bazy kokpitu (`Event.dane`, `Run.bramki_*`, `StepRun`); kokpit
nie czyta plikow dowodow z dysku. Braki (czerwone wiersze) liczy tylko serwer, na stanie
koncowym kroku (ostatnie zdarzenie konca kroku o danym `step_id`), wiec decyzja `accept` nie
ufa klientowi.

Decyzja odbioru to zdarzenie `acceptance` (kto, kiedy, uzasadnienie); nie zmienia `RunStatus`,
nie pyta runnera i nie dotyka repozytorium. Wiersz runu jest zablokowany (`FOR UPDATE`) od
sprawdzenia stanu do commitu, wiec dwie rownolegle decyzje nie zapisza sie obie.
"""
from __future__ import annotations

import json

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.models import Event, Membership, Run, RunStatus, StepRun, User

from .errors import NiespojneDane, StanNiemozliwy
from .kroki import kroki_runu
from .runs import KIND_ODBIORU, _nastepny_seq, czas_zdarzenia, run_dla_najemcy

_KONCZACE_KROK = ("step_finished", "step_failed")


async def _zdarzenia_konca_kroku(s: AsyncSession, run: Run) -> list[Event]:
    return list((await s.execute(
        select(Event)
        .where(Event.run_id == run.id, Event.tenant_id == run.tenant_id,
               Event.kind.in_(_KONCZACE_KROK))
        .order_by(Event.seq)
    )).scalars().all())


def _stany_koncowe(zdarzenia: list[Event]) -> list[Event]:
    """Ostatnie zdarzenie konca kazdego kroku, rosnaco po numerze zdarzenia."""
    ostatnie: dict[str, Event] = {}
    for ev in zdarzenia:
        ostatnie[ev.step_id] = ev
    return sorted(ostatnie.values(), key=lambda ev: ev.seq)


def _braki_krokow(koncowe: list[Event]) -> tuple[list[dict], list[dict]]:
    """Braki `werdykt_fail` i `brak_dowodu` z nieudanych stanow koncowych (w kolejnosci seq)."""
    werdykty: list[dict] = []
    dowody: list[dict] = []
    for ev in koncowe:
        if ev.kind != "step_failed":
            continue
        dane = ev.dane or {}
        if "verdict" in dane:
            powod = f"werdykt {dane['verdict']}: {ev.text}"
            werdykty.append({"rodzaj": "werdykt_fail", "step_id": ev.step_id, "powod": powod})
        elif "dowod" in dane:
            powod = f"dowód {dane['dowod']['capability']}: {ev.text}"
            dowody.append({"rodzaj": "brak_dowodu", "step_id": ev.step_id, "powod": powod})
        else:
            dowody.append({"rodzaj": "brak_dowodu", "step_id": ev.step_id, "powod": ev.text})
    return werdykty, dowody


def _braki(run: Run, koncowe: list[Event], kroki: list[StepRun]) -> list[dict]:
    """Czerwone wiersze w ustalonej kolejnosci rodzajow: bramki, pokrycie, werdykty, dowody, koszt."""
    braki = [
        {"rodzaj": "pominieta_bramka", "step_id": krok, "powod": f"bramka pominięta: {powod}"}
        for krok, powod in (run.bramki_powody or {}).items()
    ]
    if run.stan_pokrycia == "niesprawdzony":
        braki.append({
            "rodzaj": "niesprawdzony", "step_id": None,
            "powod": "żadna bramka nie sprawdziła runu"})
    werdykty, dowody = _braki_krokow(koncowe)
    braki += werdykty + dowody
    niezmierzone = [k.step_id for k in kroki if k.cost_usd is None]
    if niezmierzone:
        braki.append({
            "rodzaj": "niezmierzone", "step_id": None,
            "powod": f"koszt niezmierzony: {', '.join(niezmierzone)}"})
    return braki


def decyzja_z_zdarzenia(ev: Event) -> dict:
    """Decyzja odbioru zapisana w tekscie zdarzenia `acceptance`; czas to `Event.at`."""
    tresc = json.loads(ev.text)
    return {
        "decyzja": tresc["decyzja"], "user_id": tresc["user_id"], "email": tresc["email"],
        "uzasadnienie": tresc["uzasadnienie"], "at": czas_zdarzenia(ev),
    }


async def _decyzja_runu(s: AsyncSession, run: Run) -> Event | None:
    return (await s.execute(
        select(Event)
        .where(Event.run_id == run.id, Event.tenant_id == run.tenant_id,
               Event.kind == KIND_ODBIORU)
        .order_by(Event.seq)
    )).scalars().first()


async def dowody_runu(s: AsyncSession, run_id: str, tenant_id: str) -> dict:
    """Ekran dowodow runu w dowolnym stanie; obcy albo nieznany run to `NieZnaleziono`."""
    run = await run_dla_najemcy(s, run_id, tenant_id)
    zdarzenia = await _zdarzenia_konca_kroku(s, run)
    koncowe = _stany_koncowe(zdarzenia)
    kroki = await kroki_runu(s, run)
    twierdzenia = [
        {"step_id": ev.step_id, "artefakt": artefakt}
        for ev in zdarzenia if ev.kind == "step_finished"
        for artefakt in (ev.dane or {}).get("artefakty", [])
    ]
    dowody = [
        {"step_id": ev.step_id, "capability": ev.dane["dowod"]["capability"],
         "source": ev.dane["dowod"]["source"], "exit_code": ev.dane["dowod"]["exit_code"],
         "sha256": ev.dane["dowod"]["sha256"], "plik": ev.dane["dowod"]["plik"]}
        for ev in koncowe if "dowod" in (ev.dane or {})
    ]
    werdykty = [
        {"step_id": ev.step_id, "verdict": ev.dane["verdict"],
         "zaliczony": ev.kind == "step_finished"}
        for ev in koncowe if "verdict" in (ev.dane or {})
    ]
    # None = zaden krok nie zglosil pola (brak danych), [] = zglosil pusta liste
    kotwice: list[str] | None = None
    for ev in zdarzenia:
        if ev.kind == "step_finished" and "kotwice" in (ev.dane or {}):
            kotwice = (kotwice or []) + ev.dane["kotwice"]
    decyzja = await _decyzja_runu(s, run)
    return {
        "run_id": run.id,
        "twierdzenia": twierdzenia,
        "dowody": dowody,
        "werdykty": werdykty,
        "braki": _braki(run, koncowe, kroki),
        # mutanty: brak danych do czasu HAR-168
        "ryzyka": {"kotwice": kotwice, "mutanty": None},
        "pomiary": [
            {"step_id": k.step_id, "cost_usd": k.cost_usd, "tokens_in": k.tokens_in,
             "tokens_out": k.tokens_out}
            for k in kroki
        ],
        "decyzja": None if decyzja is None else decyzja_z_zdarzenia(decyzja),
    }


async def _email_uzytkownika(s: AsyncSession, tenant_id: str, user_id: str) -> str:
    """Adres czlonka TEGO zespolu; uzytkownik spoza zespolu nie jest autorem decyzji."""
    email = (await s.execute(
        select(User.email)
        .where(
            Membership.user_id == User.id, User.id == user_id,
            Membership.tenant_id == tenant_id,
        )
    )).scalar_one_or_none()
    if email is None:
        await s.rollback()
        raise StanNiemozliwy("uzytkownik nie nalezy do zespolu runu")
    return email


async def podejmij_decyzje_odbioru(
    s: AsyncSession, run_id: str, tenant_id: str, user_id: str,
    decyzja: str, uzasadnienie: str | None,
) -> Event:
    """Zapisuje decyzje odbioru (jedna na run) jako zdarzenie `acceptance`.

    `accept` przy niepustej liscie brakow wymaga uzasadnienia; `send_back` nie."""
    run = await run_dla_najemcy(s, run_id, tenant_id)
    # blokada wiersza: druga rownolegla decyzja czeka i po odblokowaniu widzi zdarzenie
    # zapisane przez pierwsza, wiec dostaje 409 (wyscig-sprawdz-zapisz)
    await s.refresh(run, with_for_update=True)
    if run.status != RunStatus.to_accept:
        await s.rollback()
        raise StanNiemozliwy("run nie jest do odbioru")
    if (await s.execute(select(exists().where(
        Event.run_id == run.id, Event.tenant_id == run.tenant_id, Event.kind == KIND_ODBIORU
    )))).scalar_one():
        await s.rollback()
        raise StanNiemozliwy("decyzja odbioru zostala juz podjeta")
    uzasadnienie = (uzasadnienie or "").strip() or None
    if decyzja == "accept" and uzasadnienie is None:
        braki = _braki(
            run, _stany_koncowe(await _zdarzenia_konca_kroku(s, run)), await kroki_runu(s, run))
        if braki:
            await s.rollback()
            raise NiespojneDane("przyjecie runu z brakami wymaga uzasadnienia")
    email = await _email_uzytkownika(s, tenant_id, user_id)
    tresc = {"user_id": user_id, "email": email, "decyzja": decyzja, "uzasadnienie": uzasadnienie}
    # mutant None jest rownowazny (json czyta ensure_ascii jako bool);
    # wariant True chroni reczny mutant
    tekst = json.dumps(tresc, ensure_ascii=False)  # pragma: no mutate
    zdarzenie = Event(
        tenant_id=run.tenant_id, run_id=run.id,
        seq=await _nastepny_seq(s, run.id, run.tenant_id),
        kind=KIND_ODBIORU, text=tekst, text_full_len=len(tekst))
    s.add(zdarzenie)
    await s.commit()
    return zdarzenie
