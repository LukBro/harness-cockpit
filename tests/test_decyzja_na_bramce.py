"""HAR-172 (czesc 2): czlowiek zatwierdza albo odrzuca run czekajacy na bramce (TDD).

Prawdziwy kod: serwis `cockpit.services.decyzje.podejmij_decyzje` i trasa
`POST /api/runs/{run_id}/decision` (ASGITransport, sesja uzytkownika, SQLite w tmp_path).
Run dochodzi do `waiting` produkcyjna sciezka: `runs.zapisz_zdarzenie` ze `step_waiting`
(albo `POST /api/events` od runnera), nie reczna zmiana statusu - wyjatek to stany, ktorych
runner nie wyprodukuje (run `waiting` bez kroku `waiting`, krok czekajacy w innym statusie).
Skutki czytane z BAZY (status, powod, zdarzenia `decision`, stan kroku) i z kolejki runnera na
szynie; kazda odmowa sprawdza, ze baza jest taka sama jak przed decyzja.

Zalozenia ponad kontrakt (zob. raport test-writera):
- `decyzje.py` wola sprawdzenie niezmiennika przez modul: `walidacja.sprawdz_decyzje(True, ...)`
  (test podmienia `cockpit.services.walidacja.sprawdz_decyzje`);
- atomowosc zapewnia blokada wiersza runu (`SELECT ... FOR UPDATE`), nie warunkowy UPDATE.
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import re
import uuid
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.sql import Select

from cockpit.api import bus
from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import (
    Base, Event, RolaWZespole, Run, RunStatus, StepRun, StepStatus,
)
from cockpit.services import decyzje, runs, walidacja
from cockpit.services.errors import (
    NaruszonyNiezmiennik, NiespojneDane, NieZnaleziono, StanNiemozliwy, WymagaPotwierdzenia,
)
from cockpit.services.invariants import Niezmiennik
from cockpit.services.walidacja import Naruszenie
from sesja_testowa import zaloguj_klienta, zaloz_czlonka, zaloz_runnera, zaloz_wlasciciela

TOKEN_A, TOKEN_B = "token-zespolu-a-172", "token-zespolu-b-172"
TEKST_CZEKANIA = "czeka na decyzję człowieka (approval)"
SPACJE = " \t\n "


@pytest.fixture
async def swiat(tmp_path, monkeypatch):
    """Dwa zespoly: A (Browarek: wlasciciel, czlonek, obserwator, runner A) i B (Piekarnia)."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'decyzje.db'}"
    engine = create_async_engine(url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    wlasciciel_a, tenant_a = await zaloz_wlasciciela(maker, "a@example.com", nazwa_zespolu="Browarek")
    wlasciciel_b, tenant_b = await zaloz_wlasciciela(maker, "b@example.com", nazwa_zespolu="Piekarnia")
    obserwator = await zaloz_czlonka(maker, tenant_a, RolaWZespole.viewer, "obserwator@example.com")
    czlonek = await zaloz_czlonka(maker, tenant_a, RolaWZespole.member, "czlonek@example.com")
    runner_a = await zaloz_runnera(maker, tenant_a, "runner-a", TOKEN_A)
    runner_b = await zaloz_runnera(maker, tenant_b, "runner-b", TOKEN_B)
    for rid in (runner_a, runner_b):
        bus.mark_runner_connected(rid)

    klienci: list[AsyncClient] = []

    def nowy_klient() -> AsyncClient:
        k = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        klienci.append(k)
        return k

    async def zalogowany(user) -> AsyncClient:
        k = nowy_klient()
        await zaloguj_klienta(k, maker, user)
        return k

    swiat = SimpleNamespace(
        maker=maker, engine=engine, url=url, tenant_a=tenant_a, tenant_b=tenant_b,
        runner_a=runner_a, runner_b=runner_b,
        klient_a=await zalogowany(wlasciciel_a), klient_b=await zalogowany(wlasciciel_b),
        klient_obserwatora=await zalogowany(obserwator), klient_czlonka=await zalogowany(czlonek),
        klient_anonimowy=nowy_klient(), engines=[engine],
    )
    try:
        yield swiat
    finally:
        for k in klienci:
            await k.aclose()
        for rid in (runner_a, runner_b):
            while rid in bus.connected_runner_ids():
                bus.mark_runner_disconnected(rid)
            bus._rozlaczone.pop(rid, None)
            bus._runner_queues.pop(rid, None)
        bus._run_subscribers.clear()
        for e in swiat.engines:
            await e.dispose()


# --- pomocnicy: dojscie do bramki produkcyjna sciezka, odczyt stanu z bazy ------------------


async def _zdarzenie_runu(maker, run_id: str, kind: str, krok: str, text: str = "") -> None:
    """Zdarzenie od runnera zapisane tym, czym zapisuje je trasa `/api/events`."""
    async with maker() as s:
        run = await s.get(Run, run_id)
        await runs.zapisz_zdarzenie(
            s, run, event_id=str(uuid.uuid4()), kind=kind, step_id=krok, agent_id=krok, text=text)


async def _run_na_bramce(swiat, *, tenant_id=None, runner_id=None, krok: str = "a") -> str:
    """Run `running` -> `step_waiting` kroku: status `waiting`, krok `waiting`, zdarzenie nr 1."""
    async with swiat.maker() as s:
        run = Run(tenant_id=tenant_id or swiat.tenant_a, runner_id=runner_id or swiat.runner_a,
                  status=RunStatus.running)
        s.add(run)
        await s.commit()
        run_id = run.id
    await _zdarzenie_runu(swiat.maker, run_id, "step_waiting", krok, TEKST_CZEKANIA)
    return run_id


async def _ustaw_status(maker, run_id: str, wartosc: str) -> None:
    async with maker() as s:
        await s.execute(update(Run).where(Run.id == run_id).values(status=RunStatus(wartosc)))
        await s.commit()


async def _stan(maker, run_id: str) -> tuple:
    """Wszystko, co decyzja moze zmienic: status, powod, zdarzenia i kroki runu."""
    async with maker() as s:
        run = await s.get(Run, run_id)
        zdarzenia = (await s.execute(
            select(Event.seq, Event.kind, Event.step_id, Event.text, Event.tenant_id)
            .where(Event.run_id == run_id).order_by(Event.seq))).all()
        kroki = (await s.execute(
            select(StepRun.step_id, StepRun.agent_id, StepRun.attempt, StepRun.status,
                   StepRun.reason)
            .where(StepRun.run_id == run_id)
            .order_by(StepRun.step_id, StepRun.agent_id, StepRun.attempt))).all()
        return (run.status.value, run.reason,
                [tuple(z) for z in zdarzenia], [tuple(k) for k in kroki])


async def _zdarzenia_decyzji(maker, run_id: str) -> list:
    async with maker() as s:
        return (await s.execute(
            select(Event).where(Event.run_id == run_id, Event.kind == "decision")
            .order_by(Event.seq))).scalars().all()


async def _decyzja(swiat, run_id: str, decyzja: str, powod: str | None = None, *, tenant_id=None):
    async with swiat.maker() as s:
        return await decyzje.podejmij_decyzje(
            s, run_id, tenant_id or swiat.tenant_a, decyzja, powod)


def _polecenia(runner_id: str) -> list[dict]:
    kolejka = bus.runner_queue(runner_id)
    wynik = []
    while not kolejka.empty():
        wynik.append(kolejka.get_nowait())
    return wynik


# =====================================================================================
# 1. Status `rejected`: wartosc, stan koncowy
# =====================================================================================


def test_status_odrzucony_na_bramce_ma_wartosc_rejected():
    assert RunStatus("rejected").name == "rejected"
    assert RunStatus("rejected").value == "rejected"


def test_odrzucony_run_nie_jest_aktywny():
    assert RunStatus("rejected") not in runs.STATUSY_AKTYWNE


@pytest.mark.parametrize("rodzaj", [
    "step_started", "step_waiting", "step_finished", "step_failed", "step_aborted", "run_finished",
])
@pytest.mark.parametrize("nieudane", [(), ("a",)], ids=["bez_porazek", "z_porazka"])
def test_odrzucony_run_jest_koncowy_zadne_zdarzenie_runnera_go_nie_zmienia(rodzaj, nieudane):
    assert runs.nastepny_status(RunStatus("rejected"), rodzaj, nieudane) == RunStatus("rejected")


@pytest.mark.parametrize(("status", "blokuje"), [("waiting", True), ("rejected", False)])
async def test_tylko_aktywny_run_na_tickecie_wymaga_potwierdzenia_odrzucony_nie(swiat, status, blokuje):
    async with swiat.maker() as s:
        s.add(Run(tenant_id=swiat.tenant_a, runner_id=swiat.runner_a, ticket_id="HAR-1",
                  status=RunStatus(status)))
        await s.commit()

    async with swiat.maker() as s:
        if blokuje:
            with pytest.raises(WymagaPotwierdzenia):
                await runs.utworz_run(s, swiat.tenant_a, "w", {swiat.runner_a}, ticket_id="HAR-1")
        else:
            nowy = await runs.utworz_run(s, swiat.tenant_a, "w", {swiat.runner_a}, ticket_id="HAR-1")
            assert nowy.status == RunStatus.running


# =====================================================================================
# 2. Serwis: zatwierdzenie
# =====================================================================================


async def test_zatwierdzenie_zmienia_run_czekajacy_na_bramce_na_running(swiat):
    run_id = await _run_na_bramce(swiat)
    assert (await _stan(swiat.maker, run_id))[0] == "waiting"      # kontrast

    wynik = await _decyzja(swiat, run_id, "approve")

    assert (await _stan(swiat.maker, run_id))[0] == "running"
    assert wynik.run.id == run_id
    assert wynik.run.status == RunStatus.running


async def test_zatwierdzenie_zostawia_powod_runu_bez_zmian(swiat):
    run_id = await _run_na_bramce(swiat)
    async with swiat.maker() as s:
        await s.execute(update(Run).where(Run.id == run_id).values(reason="wczesniejsza uwaga"))
        await s.commit()

    await _decyzja(swiat, run_id, "approve", "ok")

    assert (await _stan(swiat.maker, run_id))[1] == "wczesniejsza uwaga"


async def test_zatwierdzenie_daje_polecenie_resume_z_id_kroku_bramki(swiat):
    run_id = await _run_na_bramce(swiat, krok="wdrozenie")

    wynik = await _decyzja(swiat, run_id, "approve")

    assert wynik.step_id == "wdrozenie"
    assert wynik.polecenie == {
        "type": "resume", "run_id": run_id, "step_id": "wdrozenie",
        "decyzja": "approve", "powod": None,
    }


async def test_zatwierdzenie_z_powodem_zapisuje_powod_bez_spacji_w_zdarzeniu_decision(swiat):
    run_id = await _run_na_bramce(swiat)

    wynik = await _decyzja(swiat, run_id, "approve", "  wszystko gra \n")

    (zapisane,) = await _zdarzenia_decyzji(swiat.maker, run_id)
    assert zapisane.text == "approve: wszystko gra"
    assert wynik.polecenie["powod"] == "wszystko gra"       # w poleceniu tez bez spacji na brzegach


async def test_zatwierdzenie_z_samymi_bialymi_znakami_w_powodzie_daje_polecenie_bez_powodu(swiat):
    run_id = await _run_na_bramce(swiat)

    wynik = await _decyzja(swiat, run_id, "approve", SPACJE)

    (zapisane,) = await _zdarzenia_decyzji(swiat.maker, run_id)
    assert zapisane.text == "approve"
    assert wynik.polecenie["powod"] is None


async def test_wynik_decyzji_jest_niezmienny(swiat):
    run_id = await _run_na_bramce(swiat)
    wynik = await _decyzja(swiat, run_id, "approve")

    for pole in ("run", "step_id", "polecenie", "zdarzenie"):
        with pytest.raises(FrozenInstanceError):
            setattr(wynik, pole, None)


async def test_zatwierdzenie_zapisuje_zdarzenie_decision_kroku_bramki_z_kolejnym_numerem_i_zespolem(swiat):
    run_id = await _run_na_bramce(swiat, krok="wdrozenie")

    wynik = await _decyzja(swiat, run_id, "approve")

    (zapisane,) = await _zdarzenia_decyzji(swiat.maker, run_id)
    assert (zapisane.kind, zapisane.step_id, zapisane.text) == ("decision", "wdrozenie", "approve")
    assert zapisane.seq == 2                      # po step_waiting o numerze 1
    assert zapisane.tenant_id == swiat.tenant_a
    assert (wynik.zdarzenie.kind, wynik.zdarzenie.seq) == ("decision", 2)


async def test_zatwierdzenie_zostawia_krok_bramki_w_stanie_waiting_do_zdarzenia_runnera(swiat):
    run_id = await _run_na_bramce(swiat)

    await _decyzja(swiat, run_id, "approve")

    assert (await _stan(swiat.maker, run_id))[3] == [("a", "a", 1, StepStatus.waiting, None)]


async def test_decyzja_dotyczy_kroku_ktory_czeka_a_nie_innego_kroku_runu(swiat):
    run_id = await _run_na_bramce(swiat, krok="a")
    # krok `b` zakonczony wczesniej w tym samym runie: status runu przestawia dopiero bramka
    async with swiat.maker() as s:
        s.add(StepRun(tenant_id=swiat.tenant_a, run_id=run_id, step_id="b", agent_id="b",
                      attempt=1, status=StepStatus.finished))
        await s.commit()

    wynik = await _decyzja(swiat, run_id, "approve")

    assert wynik.step_id == "a"
    assert wynik.polecenie["step_id"] == "a"
    (zapisane,) = await _zdarzenia_decyzji(swiat.maker, run_id)
    assert zapisane.step_id == "a"


# =====================================================================================
# 3. Serwis: odrzucenie
# =====================================================================================


async def test_odrzucenie_konczy_run_statusem_rejected_z_powodem(swiat):
    run_id = await _run_na_bramce(swiat)

    wynik = await _decyzja(swiat, run_id, "reject", "za duzy zakres zmian")

    status, powod, _, _ = await _stan(swiat.maker, run_id)
    assert (status, powod) == ("rejected", "za duzy zakres zmian")
    assert wynik.run.status == RunStatus("rejected")


async def test_powod_odrzucenia_jest_zapisany_bez_spacji_na_brzegach(swiat):
    run_id = await _run_na_bramce(swiat)

    await _decyzja(swiat, run_id, "reject", "  zly plan \t\n")

    (zapisane,) = await _zdarzenia_decyzji(swiat.maker, run_id)
    assert (await _stan(swiat.maker, run_id))[1] == "zly plan"
    assert zapisane.text == "reject: zly plan"


async def test_odrzucenie_nie_daje_polecenia_dla_runnera(swiat):
    run_id = await _run_na_bramce(swiat)

    wynik = await _decyzja(swiat, run_id, "reject", "nie")

    assert wynik.polecenie is None


async def test_odrzucenie_zapisuje_zdarzenie_decision_kroku_bramki_i_zostawia_krok_waiting(swiat):
    run_id = await _run_na_bramce(swiat, krok="wdrozenie")

    wynik = await _decyzja(swiat, run_id, "reject", "nie")

    (zapisane,) = await _zdarzenia_decyzji(swiat.maker, run_id)
    assert (zapisane.step_id, zapisane.text, zapisane.seq) == ("wdrozenie", "reject: nie", 2)
    assert zapisane.tenant_id == swiat.tenant_a
    assert wynik.step_id == "wdrozenie"
    assert (await _stan(swiat.maker, run_id))[3] == [("wdrozenie", "wdrozenie", 1, StepStatus.waiting, None)]


@pytest.mark.parametrize("powod", [None, "", SPACJE], ids=["brak", "pusty", "same_biale_znaki"])
async def test_odrzucenie_bez_powodu_jest_odmowa_i_baza_sie_nie_zmienia(swiat, powod):
    run_id = await _run_na_bramce(swiat)
    przed = await _stan(swiat.maker, run_id)

    with pytest.raises(NiespojneDane):
        await _decyzja(swiat, run_id, "reject", powod)

    assert await _stan(swiat.maker, run_id) == przed
    assert (await _stan(swiat.maker, run_id))[0] == "waiting"


# =====================================================================================
# 4. Serwis: odmowy bez zmian w bazie, kolejnosc sprawdzen
# =====================================================================================


@pytest.mark.parametrize("zla", ["maybe", "", "APPROVE", "approve ", " reject", "approved"])
async def test_decyzja_spoza_listy_jest_odrzucona_i_baza_sie_nie_zmienia(swiat, zla):
    run_id = await _run_na_bramce(swiat)
    przed = await _stan(swiat.maker, run_id)

    with pytest.raises(NiespojneDane, match=re.escape("nieznana decyzja: dozwolone approve, reject")):
        await _decyzja(swiat, run_id, zla, "powod")

    assert await _stan(swiat.maker, run_id) == przed


async def test_nieistniejacy_run_to_nie_znaleziono(swiat):
    with pytest.raises(NieZnaleziono):
        await _decyzja(swiat, "nie-ma-takiego-runu", "approve")


async def test_run_innego_zespolu_to_nie_znaleziono_a_jego_rekord_sie_nie_zmienia(swiat):
    run_obcy = await _run_na_bramce(swiat, tenant_id=swiat.tenant_b, runner_id=swiat.runner_b)
    przed = await _stan(swiat.maker, run_obcy)

    with pytest.raises(NieZnaleziono):
        await _decyzja(swiat, run_obcy, "approve", tenant_id=swiat.tenant_a)

    assert await _stan(swiat.maker, run_obcy) == przed
    assert przed[0] == "waiting"


async def test_ten_sam_run_zatwierdza_jego_zespol(swiat):
    run_obcy = await _run_na_bramce(swiat, tenant_id=swiat.tenant_b, runner_id=swiat.runner_b)

    await _decyzja(swiat, run_obcy, "approve", tenant_id=swiat.tenant_b)

    assert (await _stan(swiat.maker, run_obcy))[0] == "running"


@pytest.mark.parametrize("status", ["running", "pending", "to_accept", "failed", "skipped", "aborted",
                                    "rejected"])
async def test_run_ktory_nie_czeka_na_bramce_to_stan_niemozliwy_i_baza_sie_nie_zmienia(swiat, status):
    run_id = await _run_na_bramce(swiat)             # krok `waiting` jest, tylko status runu inny
    await _ustaw_status(swiat.maker, run_id, status)
    przed = await _stan(swiat.maker, run_id)

    with pytest.raises(StanNiemozliwy):
        await _decyzja(swiat, run_id, "approve")

    assert await _stan(swiat.maker, run_id) == przed
    assert przed[0] == status


async def test_run_waiting_bez_zadnego_kroku_to_stan_niemozliwy_i_baza_sie_nie_zmienia(swiat):
    async with swiat.maker() as s:
        run = Run(tenant_id=swiat.tenant_a, runner_id=swiat.runner_a, status=RunStatus.waiting)
        s.add(run)
        await s.commit()
        run_id = run.id
    przed = await _stan(swiat.maker, run_id)

    with pytest.raises(StanNiemozliwy):
        await _decyzja(swiat, run_id, "approve")

    assert await _stan(swiat.maker, run_id) == przed


async def test_run_waiting_z_krokiem_w_innym_stanie_niz_waiting_to_stan_niemozliwy(swiat):
    async with swiat.maker() as s:
        run = Run(tenant_id=swiat.tenant_a, runner_id=swiat.runner_a, status=RunStatus.waiting)
        s.add(run)
        await s.flush()
        s.add(StepRun(tenant_id=swiat.tenant_a, run_id=run.id, step_id="a", agent_id="a",
                      attempt=1, status=StepStatus.finished))
        await s.commit()
        run_id = run.id
    przed = await _stan(swiat.maker, run_id)

    with pytest.raises(StanNiemozliwy):
        await _decyzja(swiat, run_id, "approve")

    assert await _stan(swiat.maker, run_id) == przed


@pytest.mark.parametrize(("pierwsza", "druga", "status"), [
    ("approve", "approve", "running"),
    ("approve", "reject", "running"),
    ("reject", "approve", "rejected"),
    ("reject", "reject", "rejected"),
])
async def test_druga_decyzja_po_pierwszej_to_stan_niemozliwy_a_zdarzenie_jest_jedno(
    swiat, pierwsza, druga, status,
):
    run_id = await _run_na_bramce(swiat)
    await _decyzja(swiat, run_id, pierwsza, "powod")
    po_pierwszej = await _stan(swiat.maker, run_id)

    with pytest.raises(StanNiemozliwy):
        await _decyzja(swiat, run_id, druga, "inny powod")

    assert await _stan(swiat.maker, run_id) == po_pierwszej
    assert po_pierwszej[0] == status
    assert len(await _zdarzenia_decyzji(swiat.maker, run_id)) == 1


async def test_odrzucenie_bez_powodu_runu_ktory_nie_czeka_to_stan_niemozliwy_nie_dane(swiat):
    run_id = await _run_na_bramce(swiat)
    await _ustaw_status(swiat.maker, run_id, "running")

    with pytest.raises(StanNiemozliwy):
        await _decyzja(swiat, run_id, "reject", None)


async def test_odrzucenie_bez_powodu_nieistniejacego_runu_to_nie_znaleziono_nie_dane(swiat):
    with pytest.raises(NieZnaleziono):
        await _decyzja(swiat, "nie-ma-takiego-runu", "reject", None)


async def test_zla_decyzja_dla_nieistniejacego_runu_to_dane_nie_nie_znaleziono(swiat):
    with pytest.raises(NiespojneDane):
        await _decyzja(swiat, "nie-ma-takiego-runu", "maybe")


async def test_zla_decyzja_dla_runu_ktory_nie_czeka_to_dane_nie_stan_niemozliwy(swiat):
    run_id = await _run_na_bramce(swiat)
    await _ustaw_status(swiat.maker, run_id, "running")

    with pytest.raises(NiespojneDane):
        await _decyzja(swiat, run_id, "maybe")


async def test_run_innego_zespolu_ktory_nie_czeka_to_nie_znaleziono_nie_stan_niemozliwy(swiat):
    run_obcy = await _run_na_bramce(swiat, tenant_id=swiat.tenant_b, runner_id=swiat.runner_b)
    await _ustaw_status(swiat.maker, run_obcy, "running")

    with pytest.raises(NieZnaleziono):
        await _decyzja(swiat, run_obcy, "approve", tenant_id=swiat.tenant_a)


# =====================================================================================
# 5. Serwis: niezmiennik approval_required przez walidacje
# =====================================================================================


@pytest.mark.parametrize(("decyzja", "powod"), [("approve", None), ("reject", "nie")])
async def test_decyzja_przechodzi_przez_sprawdzenie_niezmiennika_approval_required(
    swiat, monkeypatch, decyzja, powod,
):
    wywolania: list[tuple] = []

    def sprawdz(krok_ma_approval, d):
        wywolania.append((krok_ma_approval, d))
        return None

    monkeypatch.setattr(walidacja, "sprawdz_decyzje", sprawdz)
    run_id = await _run_na_bramce(swiat)

    await _decyzja(swiat, run_id, decyzja, powod)

    assert wywolania == [(True, decyzja)]


@pytest.mark.parametrize(("decyzja", "powod"), [("approve", None), ("reject", "nie")])
async def test_naruszenie_niezmiennika_przy_decyzji_to_naruszony_niezmiennik_i_baza_bez_zmian(
    swiat, monkeypatch, decyzja, powod,
):
    monkeypatch.setattr(
        walidacja, "sprawdz_decyzje",
        lambda krok_ma_approval, d: Naruszenie(Niezmiennik.approval_required, "brak decyzji"))
    run_id = await _run_na_bramce(swiat)
    przed = await _stan(swiat.maker, run_id)

    with pytest.raises(NaruszonyNiezmiennik):
        await _decyzja(swiat, run_id, decyzja, powod)

    assert await _stan(swiat.maker, run_id) == przed


async def test_naruszenie_niezmiennika_przy_decyzji_podaje_nazwe_niezmiennika_i_szczegol_w_komunikacie(
    swiat, monkeypatch,
):
    monkeypatch.setattr(
        walidacja, "sprawdz_decyzje",
        lambda krok_ma_approval, d: Naruszenie(Niezmiennik.approval_required, "brak decyzji"))
    run_id = await _run_na_bramce(swiat)

    with pytest.raises(NaruszonyNiezmiennik) as blad:
        await _decyzja(swiat, run_id, "approve")

    assert blad.value.nazwa == Niezmiennik.approval_required
    assert str(blad.value) == f"niezmiennik {Niezmiennik.approval_required}: brak decyzji"


async def test_odmowa_runowi_ktory_nie_czeka_mowi_czlowiekowi_ze_run_nie_czeka_na_decyzje(swiat):
    run_id = await _run_na_bramce(swiat)
    await _ustaw_status(swiat.maker, run_id, "running")

    with pytest.raises(StanNiemozliwy) as blad:
        await _decyzja(swiat, run_id, "approve")

    assert str(blad.value) == "run nie czeka na decyzje czlowieka"


async def test_odmowa_odrzucenia_bez_powodu_mowi_czlowiekowi_ze_odrzucenie_wymaga_powodu(swiat):
    run_id = await _run_na_bramce(swiat)

    with pytest.raises(NiespojneDane) as blad:
        await _decyzja(swiat, run_id, "reject", None)

    assert str(blad.value) == "odrzucenie wymaga powodu"


async def test_niezmiennik_jest_sprawdzany_po_stanie_runu_run_ktory_nie_czeka_to_stan_niemozliwy(
    swiat, monkeypatch,
):
    monkeypatch.setattr(
        walidacja, "sprawdz_decyzje",
        lambda krok_ma_approval, d: Naruszenie(Niezmiennik.approval_required, "x"))
    run_id = await _run_na_bramce(swiat)
    await _ustaw_status(swiat.maker, run_id, "running")

    with pytest.raises(StanNiemozliwy):
        await _decyzja(swiat, run_id, "approve")


async def test_niezmiennik_jest_sprawdzany_po_powodzie_odrzucenie_bez_powodu_to_dane(swiat, monkeypatch):
    monkeypatch.setattr(
        walidacja, "sprawdz_decyzje",
        lambda krok_ma_approval, d: Naruszenie(Niezmiennik.approval_required, "x"))
    run_id = await _run_na_bramce(swiat)

    with pytest.raises(NiespojneDane):
        await _decyzja(swiat, run_id, "reject", None)


# =====================================================================================
# 6. Serwis: atomowosc sprawdzenia i zapisu (wariant: blokada wiersza runu)
# =====================================================================================


async def test_zapytanie_wybierajace_run_do_decyzji_blokuje_wiersz_przez_for_update(swiat):
    run_id = await _run_na_bramce(swiat)
    zapytania: list = []

    def zbierz(conn, clauseelement, multiparams, params, execution_options):
        zapytania.append(clauseelement)

    event.listen(swiat.engine.sync_engine, "before_execute", zbierz)
    try:
        await _decyzja(swiat, run_id, "approve")
    finally:
        event.remove(swiat.engine.sync_engine, "before_execute", zbierz)

    sql_pg = [str(z.compile(dialect=postgresql.dialect())) for z in zapytania if isinstance(z, Select)]
    zablokowane = [q for q in sql_pg if re.search(r"FROM run\b", q) and "FOR UPDATE" in q]
    assert zablokowane, f"zaden SELECT z tabeli run nie ma FOR UPDATE: {sql_pg}"


# =====================================================================================
# 7. Trasa POST /api/runs/{run_id}/decision
# =====================================================================================


async def _decyzja_http(klient: AsyncClient, run_id: str, **cialo):
    return await klient.post(f"/api/runs/{run_id}/decision", json=cialo)


async def test_zatwierdzenie_przez_api_zwraca_run_running_i_krok_bramki(swiat):
    run_id = await _run_na_bramce(swiat, krok="wdrozenie")

    r = await _decyzja_http(swiat.klient_a, run_id, decyzja="approve")

    assert r.status_code == 200
    assert r.json() == {"run_id": run_id, "status": "running", "step_id": "wdrozenie", "reason": None}
    assert (await _stan(swiat.maker, run_id))[0] == "running"


async def test_zatwierdzenie_przez_api_kolejkuje_runnerowi_runu_polecenie_resume_z_id_kroku(swiat):
    run_id = await _run_na_bramce(swiat, krok="wdrozenie")

    await _decyzja_http(swiat.klient_a, run_id, decyzja="approve")

    assert _polecenia(swiat.runner_a) == [{
        "type": "resume", "run_id": run_id, "step_id": "wdrozenie",
        "decyzja": "approve", "powod": None,
    }]
    assert _polecenia(swiat.runner_b) == []


async def test_zatwierdzenie_przez_api_z_powodem_zapisuje_powod_w_zdarzeniu(swiat):
    run_id = await _run_na_bramce(swiat)

    r = await _decyzja_http(swiat.klient_a, run_id, decyzja="approve", powod="  dobra robota ")

    assert r.status_code == 200
    (zapisane,) = await _zdarzenia_decyzji(swiat.maker, run_id)
    assert zapisane.text == "approve: dobra robota"
    assert _polecenia(swiat.runner_a)[0]["powod"] == "dobra robota"


@pytest.mark.parametrize("cialo", [
    {"decyzja": "approve", "powod": None},
    {"decyzja": "approve"},
], ids=["jawny_null", "bez_pola"])
async def test_zatwierdzenie_przez_api_bez_powodu_przechodzi_a_odpowiedz_ma_reason_null(swiat, cialo):
    run_id = await _run_na_bramce(swiat)

    r = await _decyzja_http(swiat.klient_a, run_id, **cialo)

    assert r.status_code == 200
    assert r.json()["reason"] is None
    assert _polecenia(swiat.runner_a)[0]["powod"] is None


async def test_odrzucenie_przez_api_konczy_run_rejected_z_powodem_i_nie_wysyla_polecenia(swiat):
    run_id = await _run_na_bramce(swiat, krok="wdrozenie")

    r = await _decyzja_http(swiat.klient_a, run_id, decyzja="reject", powod="  za duzo zmian ")

    assert r.status_code == 200
    assert r.json() == {"run_id": run_id, "status": "rejected", "step_id": "wdrozenie",
                        "reason": "za duzo zmian"}
    status, powod, _, _ = await _stan(swiat.maker, run_id)
    assert (status, powod) == ("rejected", "za duzo zmian")
    assert _polecenia(swiat.runner_a) == []


@pytest.mark.parametrize("cialo", [
    {"decyzja": "reject"},
    {"decyzja": "reject", "powod": None},
    {"decyzja": "reject", "powod": ""},
    {"decyzja": "reject", "powod": SPACJE},
], ids=["bez_pola", "null", "pusty", "same_biale_znaki"])
async def test_odrzucenie_przez_api_bez_powodu_to_422_i_baza_sie_nie_zmienia(swiat, cialo):
    run_id = await _run_na_bramce(swiat)
    przed = await _stan(swiat.maker, run_id)

    r = await _decyzja_http(swiat.klient_a, run_id, **cialo)

    assert r.status_code == 422
    assert await _stan(swiat.maker, run_id) == przed
    assert _polecenia(swiat.runner_a) == []


@pytest.mark.parametrize("decyzja", ["approve", "reject"])
async def test_powod_dluzszy_niz_2000_znakow_to_422_i_baza_sie_nie_zmienia(swiat, decyzja):
    run_id = await _run_na_bramce(swiat)
    przed = await _stan(swiat.maker, run_id)

    r = await _decyzja_http(swiat.klient_a, run_id, decyzja=decyzja, powod="x" * 2001)

    assert r.status_code == 422
    assert await _stan(swiat.maker, run_id) == przed
    assert _polecenia(swiat.runner_a) == []


async def test_powod_o_dokladnie_2000_znakach_jest_przyjety_w_calosci(swiat):
    run_id = await _run_na_bramce(swiat)

    r = await _decyzja_http(swiat.klient_a, run_id, decyzja="reject", powod="y" * 2000)

    assert r.status_code == 200
    assert (await _stan(swiat.maker, run_id))[1] == "y" * 2000


@pytest.mark.parametrize("cialo", [
    {"decyzja": "maybe"}, {"decyzja": ""}, {"decyzja": "APPROVE"}, {"decyzja": None}, {},
    {"powod": "bez decyzji"},
], ids=["maybe", "pusta", "wielkie_litery", "null", "puste_cialo", "tylko_powod"])
async def test_decyzja_spoza_listy_przez_api_to_422_i_baza_sie_nie_zmienia(swiat, cialo):
    run_id = await _run_na_bramce(swiat)
    przed = await _stan(swiat.maker, run_id)

    r = await _decyzja_http(swiat.klient_a, run_id, **cialo)

    assert r.status_code == 422
    assert await _stan(swiat.maker, run_id) == przed
    assert _polecenia(swiat.runner_a) == []


async def test_run_innego_zespolu_przez_api_to_404_a_jego_rekord_i_kolejka_sie_nie_zmieniaja(swiat):
    run_obcy = await _run_na_bramce(swiat, tenant_id=swiat.tenant_b, runner_id=swiat.runner_b)
    przed = await _stan(swiat.maker, run_obcy)

    r = await _decyzja_http(swiat.klient_a, run_obcy, decyzja="approve")

    assert r.status_code == 404
    assert await _stan(swiat.maker, run_obcy) == przed
    assert przed[0] == "waiting"
    assert _polecenia(swiat.runner_b) == []


async def test_nieistniejacy_run_przez_api_to_404(swiat):
    r = await _decyzja_http(swiat.klient_a, "nie-ma-takiego-runu", decyzja="approve")

    assert r.status_code == 404


@pytest.mark.parametrize("status", ["running", "to_accept", "failed", "rejected"])
async def test_decyzja_dla_runu_ktory_nie_czeka_to_409_i_baza_sie_nie_zmienia(swiat, status):
    run_id = await _run_na_bramce(swiat)
    await _ustaw_status(swiat.maker, run_id, status)
    przed = await _stan(swiat.maker, run_id)

    r = await _decyzja_http(swiat.klient_a, run_id, decyzja="approve")

    assert r.status_code == 409
    assert await _stan(swiat.maker, run_id) == przed
    assert _polecenia(swiat.runner_a) == []


async def test_decyzja_dla_runu_waiting_bez_kroku_waiting_to_409_i_baza_sie_nie_zmienia(swiat):
    async with swiat.maker() as s:
        run = Run(tenant_id=swiat.tenant_a, runner_id=swiat.runner_a, status=RunStatus.waiting)
        s.add(run)
        await s.commit()
        run_id = run.id
    przed = await _stan(swiat.maker, run_id)

    r = await _decyzja_http(swiat.klient_a, run_id, decyzja="reject", powod="nie")

    assert r.status_code == 409
    assert await _stan(swiat.maker, run_id) == przed


async def test_druga_decyzja_przez_api_to_409_a_runner_dostaje_jedno_polecenie(swiat):
    run_id = await _run_na_bramce(swiat)
    pierwsza = await _decyzja_http(swiat.klient_a, run_id, decyzja="approve")
    po_pierwszej = await _stan(swiat.maker, run_id)

    druga = await _decyzja_http(swiat.klient_a, run_id, decyzja="approve")

    assert (pierwsza.status_code, druga.status_code) == (200, 409)
    assert await _stan(swiat.maker, run_id) == po_pierwszej
    assert len(_polecenia(swiat.runner_a)) == 1


async def test_obserwator_nie_moze_podjac_decyzji_403_baza_i_kolejka_bez_zmian(swiat):
    run_id = await _run_na_bramce(swiat)
    przed = await _stan(swiat.maker, run_id)

    r = await _decyzja_http(swiat.klient_obserwatora, run_id, decyzja="approve")

    assert r.status_code == 403
    assert await _stan(swiat.maker, run_id) == przed
    assert _polecenia(swiat.runner_a) == []


async def test_obserwator_dostaje_403_takze_gdy_run_nie_czeka(swiat):
    run_id = await _run_na_bramce(swiat)
    await _ustaw_status(swiat.maker, run_id, "running")

    r = await _decyzja_http(swiat.klient_obserwatora, run_id, decyzja="approve")

    assert r.status_code == 403


async def test_czlonek_zespolu_moze_zatwierdzic_bramke(swiat):
    run_id = await _run_na_bramce(swiat)

    r = await _decyzja_http(swiat.klient_czlonka, run_id, decyzja="approve")

    assert r.status_code == 200
    assert (await _stan(swiat.maker, run_id))[0] == "running"


async def test_decyzja_bez_sesji_to_401_i_baza_sie_nie_zmienia(swiat):
    run_id = await _run_na_bramce(swiat)
    przed = await _stan(swiat.maker, run_id)

    r = await _decyzja_http(swiat.klient_anonimowy, run_id, decyzja="approve")

    assert r.status_code == 401
    assert await _stan(swiat.maker, run_id) == przed
    assert _polecenia(swiat.runner_a) == []


async def test_decyzja_jest_publikowana_na_szynie_dla_przegladarki(swiat):
    run_id = await _run_na_bramce(swiat, krok="wdrozenie")
    kolejka = bus.subscribe_run(run_id)

    await _decyzja_http(swiat.klient_a, run_id, decyzja="reject", powod="nie teraz")

    opublikowane = kolejka.get_nowait()
    assert opublikowane["kind"] == "decision"
    assert opublikowane["text"] == "reject: nie teraz"
    assert opublikowane["step_id"] == "wdrozenie"
    assert opublikowane["seq"] == 2
    assert opublikowane["text_truncated"] is False
    assert opublikowane["text_full_len"] == len("reject: nie teraz")
    assert opublikowane["at"].endswith("+00:00")     # czas zdarzenia w UTC (HAR-121)
    assert set(opublikowane) == {"seq", "kind", "text", "step_id", "text_truncated", "text_full_len",
                                 "at"}
    assert kolejka.empty()


async def test_odrzucona_decyzja_nie_jest_publikowana_na_szynie(swiat):
    run_id = await _run_na_bramce(swiat)
    kolejka = bus.subscribe_run(run_id)

    await _decyzja_http(swiat.klient_a, run_id, decyzja="reject")

    assert kolejka.empty()


def test_trasa_decyzji_deklaruje_w_schemacie_odpowiedzi_bledow_z_kontraktu():
    operacja = app.openapi()["paths"]["/api/runs/{run_id}/decision"]["post"]

    assert {"200", "401", "403", "404", "409", "422"} <= set(operacja["responses"])
    # wartość, nie obecność: bez jawnego 422 FastAPI dokłada domyślne HTTPValidationError,
    # którego kształt nie zgadza się z `Blad` (schematy.odpowiedzi_bledow)
    for kod in ("401", "403", "404", "409", "422"):
        schemat = operacja["responses"][kod]["content"]["application/json"]["schema"]
        assert schemat == {"$ref": "#/components/schemas/Blad"}, kod


async def test_run_doprowadzony_do_bramki_przez_api_zdarzeniem_runnera_da_sie_zatwierdzic(swiat):
    utworzony = await swiat.klient_a.post("/api/runs", json={})
    run_id = utworzony.json()["run_id"]
    _polecenia(swiat.runner_a)                      # polecenie startu runu
    zdarzenie = await swiat.klient_a.post(
        "/api/events",
        json={"run_id": run_id, "kind": "step_waiting", "step_id": "a", "agent_id": "a",
              "text": TEKST_CZEKANIA, "event_id": str(uuid.uuid4())},
        headers={"Authorization": f"Bearer {TOKEN_A}"})
    assert zdarzenie.status_code == 200
    assert (await _stan(swiat.maker, run_id))[0] == "waiting"

    r = await _decyzja_http(swiat.klient_a, run_id, decyzja="approve")

    assert r.status_code == 200
    assert (await _stan(swiat.maker, run_id))[0] == "running"
    assert _polecenia(swiat.runner_a) == [{
        "type": "resume", "run_id": run_id, "step_id": "a", "decyzja": "approve", "powod": None}]


async def _zdarzenie_od_runnera(swiat, run_id: str, kind: str, krok: str) -> None:
    """Zdarzenie dokladnie tak, jak wysyla je runner: POST /api/events z tokenem runnera."""
    r = await swiat.klient_a.post(
        "/api/events",
        json={"run_id": run_id, "kind": kind, "step_id": krok, "agent_id": krok,
              "text": TEKST_CZEKANIA if kind == "step_waiting" else "",
              "event_id": str(uuid.uuid4())},
        headers={"Authorization": f"Bearer {TOKEN_A}"})
    assert r.status_code == 200, r.text


async def test_run_po_sekwencji_silnika_krok_niezalezny_potem_bramka_czeka_i_da_sie_zatwierdzic(swiat):
    # kolejnosc zdarzen, jaka produkuje silnik: step_waiting dopiero po krokach niezaleznych
    utworzony = await swiat.klient_a.post("/api/runs", json={})
    run_id = utworzony.json()["run_id"]
    _polecenia(swiat.runner_a)                      # polecenie startu runu
    await _zdarzenie_od_runnera(swiat, run_id, "step_started", "c")
    await _zdarzenie_od_runnera(swiat, run_id, "step_finished", "c")
    await _zdarzenie_od_runnera(swiat, run_id, "step_waiting", "a")
    status, _, _, kroki = await _stan(swiat.maker, run_id)
    assert status == "waiting"
    assert [(k[0], k[3]) for k in kroki] == [("a", StepStatus.waiting), ("c", StepStatus.finished)]

    r = await _decyzja_http(swiat.klient_a, run_id, decyzja="approve")

    assert r.status_code == 200
    assert r.json()["step_id"] == "a"
    assert (await _stan(swiat.maker, run_id))[0] == "running"
    assert _polecenia(swiat.runner_a) == [{
        "type": "resume", "run_id": run_id, "step_id": "a", "decyzja": "approve", "powod": None}]


async def test_run_po_sekwencji_silnika_krok_niezalezny_potem_bramka_da_sie_odrzucic_z_powodem(swiat):
    utworzony = await swiat.klient_a.post("/api/runs", json={})
    run_id = utworzony.json()["run_id"]
    await _zdarzenie_od_runnera(swiat, run_id, "step_started", "c")
    await _zdarzenie_od_runnera(swiat, run_id, "step_finished", "c")
    await _zdarzenie_od_runnera(swiat, run_id, "step_waiting", "a")

    r = await _decyzja_http(swiat.klient_a, run_id, decyzja="reject", powod="nie ta galaz")

    assert r.status_code == 200
    assert (await _stan(swiat.maker, run_id))[:2] == ("rejected", "nie ta galaz")


async def test_start_kroku_niezaleznego_po_ogloszeniu_bramki_cofa_run_do_running_i_decyzja_to_409(swiat):
    # sekwencja kontrolna (odwrotna kolejnosc): dokumentuje, dlaczego silnik ogłasza bramke na koncu
    utworzony = await swiat.klient_a.post("/api/runs", json={})
    run_id = utworzony.json()["run_id"]
    _polecenia(swiat.runner_a)
    await _zdarzenie_od_runnera(swiat, run_id, "step_waiting", "a")
    assert (await _stan(swiat.maker, run_id))[0] == "waiting"      # kontrast: sama bramka daje waiting
    await _zdarzenie_od_runnera(swiat, run_id, "step_started", "c")
    przed = await _stan(swiat.maker, run_id)
    assert przed[0] == "running"

    r = await _decyzja_http(swiat.klient_a, run_id, decyzja="approve")

    assert r.status_code == 409
    assert await _stan(swiat.maker, run_id) == przed
    assert _polecenia(swiat.runner_a) == []


# =====================================================================================
# 8. Restart kokpitu: stan czekania przezywa (nowa sesja + czysta szyna)
# =====================================================================================


def _restart_kokpitu(swiat, monkeypatch) -> None:
    """Nowy silnik i fabryka sesji na tym samym pliku bazy oraz szyna w pamieci od zera."""
    nowy_engine = create_async_engine(swiat.url)
    nowy_maker = async_sessionmaker(nowy_engine, expire_on_commit=False)
    swiat.engines.append(nowy_engine)
    monkeypatch.setattr(db_session, "engine", nowy_engine)
    monkeypatch.setattr(db_session, "SessionLocal", nowy_maker)
    swiat.maker = nowy_maker
    bus._runner_queues.clear()
    bus._run_subscribers.clear()
    bus._polaczone.clear()
    bus._rozlaczone.clear()
    bus._oczekujace.clear()


async def test_run_czekajacy_na_bramce_po_restarcie_kokpitu_pokazuje_ten_sam_krok_i_na_co_czeka(
    swiat, monkeypatch,
):
    run_id = await _run_na_bramce(swiat, krok="wdrozenie")
    przed = (await swiat.klient_a.get(f"/api/runs/{run_id}")).json()

    _restart_kokpitu(swiat, monkeypatch)
    po = (await swiat.klient_a.get(f"/api/runs/{run_id}")).json()

    assert po["status"] == "waiting"
    assert po["steps"] == [{"step_id": "wdrozenie", "agent_id": "wdrozenie", "attempt": 1,
                            "status": "waiting", "reason": None, "cost_usd": None,
                            "tokens_in": None, "tokens_out": None, "num_turns": None,
                            "session_id": None}]
    (czekanie,) = [e for e in po["events"] if e["kind"] == "step_waiting"]
    assert (czekanie["step_id"], czekanie["text"]) == ("wdrozenie", TEKST_CZEKANIA)
    assert po == przed


async def test_zatwierdzenie_po_restarcie_kokpitu_wysyla_resume_przez_nowa_szyne(swiat, monkeypatch):
    run_id = await _run_na_bramce(swiat, krok="wdrozenie")
    _restart_kokpitu(swiat, monkeypatch)

    r = await _decyzja_http(swiat.klient_a, run_id, decyzja="approve")

    assert r.status_code == 200
    assert (await _stan(swiat.maker, run_id))[0] == "running"
    assert _polecenia(swiat.runner_a) == [{
        "type": "resume", "run_id": run_id, "step_id": "wdrozenie",
        "decyzja": "approve", "powod": None}]


# =====================================================================================
# HAR-234: runner nie zapisuje zdarzen zarezerwowanych dla decyzji czlowieka
# =====================================================================================

# Lista przypadkow niezalezna od `runs.RODZAJE_CZLOWIEKA` (usuniecie wpisu z kodu nie usuwa testu).
RODZAJE_DECYZJI_CZLOWIEKA = ("decision", "integrated", "committed", "acceptance")


async def _zdarzenia_rodzaju(maker, run_id: str, kind: str) -> list:
    async with maker() as s:
        return (await s.execute(
            select(Event).where(Event.run_id == run_id, Event.kind == kind))).scalars().all()


async def test_runner_nie_odblokuje_bramki_zdarzeniem_decyzji_a_czlowiek_nadal_moze(swiat):
    run_id = await _run_na_bramce(swiat, krok="wdrozenie")
    przed = await _stan(swiat.maker, run_id)

    r = await swiat.klient_a.post(
        "/api/events",
        json={"run_id": run_id, "kind": "decision", "step_id": "wdrozenie",
              "agent_id": "wdrozenie", "text": "approve", "event_id": str(uuid.uuid4())},
        headers={"Authorization": f"Bearer {TOKEN_A}"})

    assert r.status_code == 422
    assert await _stan(swiat.maker, run_id) == przed
    assert przed[0] == "waiting"
    assert await _zdarzenia_decyzji(swiat.maker, run_id) == []

    czlowiek = await _decyzja_http(swiat.klient_a, run_id, decyzja="approve")

    assert czlowiek.status_code == 200
    assert (await _stan(swiat.maker, run_id))[0] == "running"
    assert len(await _zdarzenia_decyzji(swiat.maker, run_id)) == 1


@pytest.mark.parametrize("rodzaj", RODZAJE_DECYZJI_CZLOWIEKA, ids=RODZAJE_DECYZJI_CZLOWIEKA)
async def test_zdarzenie_runnera_z_rodzajem_zarezerwowanym_dla_czlowieka_jest_odrzucone(
    swiat, rodzaj,
):
    run_id = await _run_na_bramce(swiat)
    przed = await _stan(swiat.maker, run_id)

    with pytest.raises(NaruszonyNiezmiennik) as wyjatek:
        await _zdarzenie_runu(swiat.maker, run_id, rodzaj, "a")

    assert wyjatek.value.nazwa == "approval_required"
    assert str(wyjatek.value) == (
        f"niezmiennik approval_required: rodzaj zdarzenia {rodzaj!r} jest zarezerwowany "
        "dla decyzji czlowieka; runner nie moze go zapisac")
    assert await _zdarzenia_rodzaju(swiat.maker, run_id, rodzaj) == []
    assert await _stan(swiat.maker, run_id) == przed


async def test_ponowienie_istniejacego_event_id_z_rodzajem_czlowieka_jest_odrzucone_nie_idempotentne(
    swiat,
):
    run_id = await _run_na_bramce(swiat)
    event_id = str(uuid.uuid4())
    async with swiat.maker() as s:
        run = await s.get(Run, run_id)
        await runs.zapisz_zdarzenie(s, run, event_id=event_id, kind="step_started",
                                    step_id="b", agent_id="b")
    przed = await _stan(swiat.maker, run_id)

    async with swiat.maker() as s:
        run = await s.get(Run, run_id)
        with pytest.raises(NaruszonyNiezmiennik) as wyjatek:
            await runs.zapisz_zdarzenie(s, run, event_id=event_id, kind="decision",
                                        step_id="b", agent_id="b")

    assert wyjatek.value.nazwa == "approval_required"
    assert await _stan(swiat.maker, run_id) == przed


async def test_zwykle_zdarzenie_od_runnera_nadal_sie_zapisuje(swiat):
    run_id = await _run_na_bramce(swiat)
    przed = await _stan(swiat.maker, run_id)

    await _zdarzenie_runu(swiat.maker, run_id, "step_started", "b")

    po = await _stan(swiat.maker, run_id)
    assert len(po[2]) == len(przed[2]) + 1
    assert po[2][-1][1] == "step_started"


def test_rodzaje_zdarzen_zarezerwowane_dla_czlowieka_to_dokladnie_cztery():
    assert runs.RODZAJE_CZLOWIEKA == frozenset({"decision", "integrated", "committed", "acceptance"})
    # niezmienna lista: kod nie moze jej rozszerzyc w runtime (set == frozenset daje True)
    assert type(runs.RODZAJE_CZLOWIEKA) is frozenset
