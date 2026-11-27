"""HAR-202: pobranie tylko nowszych zdarzen runu (`po_seq`) i koszt zapisu zdarzenia w zapytaniach.

Prawdziwa aplikacja i prawdziwe uslugi na SQLite w pliku; oczekiwane wartosci czytane z bazy
i z odpowiedzi API.
"""
from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Event, Run, Runner, RunStatus, StepRun, StepStatus, Tenant
from cockpit.services import runs as uslugi_runow
from sesja_testowa import zaloguj_klienta, zaloz_wlasciciela

MAKS_SEQ = 2**31 - 1


@dataclass
class Swiat:
    klient: AsyncClient
    tenant_id: str
    obcy_tenant_id: str
    runner_id: str


@pytest.fixture
async def swiat(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    wlasciciel, tenant_id = await zaloz_wlasciciela(maker, nazwa_zespolu="Browarek")
    async with maker() as s:
        obcy = Tenant(name="Piekarnia")
        runner = Runner(tenant_id=tenant_id, name="wlasciciel", token_hash="h")
        s.add_all([obcy, runner])
        await s.commit()
        ids = (tenant_id, obcy.id, runner.id)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await zaloguj_klienta(c, maker, wlasciciel)
            yield Swiat(c, *ids)
    finally:
        await engine.dispose()


async def _run(swiat: Swiat, *, tenant_id: str | None = None, **pola) -> str:
    async with db_session.SessionLocal() as s:
        run = Run(tenant_id=tenant_id or swiat.tenant_id, runner_id=swiat.runner_id,
                  **{"status": RunStatus.running, **pola})
        s.add(run)
        await s.commit()
        return run.id


async def _zdarzenia(run_id: str, tenant_id: str, numery, kind: str = "output",
                     step_id: str = "s0", tekst: str | None = None) -> None:
    async with db_session.SessionLocal() as s:
        s.add_all([Event(tenant_id=tenant_id, run_id=run_id, seq=n, kind=kind, step_id=step_id,
                         text=f"zdarzenie-{n}" if tekst is None else tekst) for n in numery])
        await s.commit()


async def _kroki(run_id: str, tenant_id: str, stany: dict[tuple[str, int], StepStatus]) -> None:
    async with db_session.SessionLocal() as s:
        s.add_all([StepRun(tenant_id=tenant_id, run_id=run_id, step_id=krok, attempt=proba,
                           status=stan) for (krok, proba), stan in stany.items()])
        await s.commit()


async def _pobierz(swiat: Swiat, run_id: str, **parametry) -> dict:
    odpowiedz = await swiat.klient.get(f"/api/runs/{run_id}", params=parametry)
    assert odpowiedz.status_code == 200, odpowiedz.text
    return odpowiedz.json()


def _numery(tresc: dict) -> list[int]:
    return [e["seq"] for e in tresc["events"]]


# =====================================================================================
# Pobranie zdarzen nowszych niz juz posiadane
# =====================================================================================


@pytest.fixture
async def run_z_historia(swiat) -> str:
    """Numery z przerwami: numer zdarzenia to nie jego pozycja na liscie."""
    run_id = await _run(swiat)
    await _zdarzenia(run_id, swiat.tenant_id, [2, 3, 7, 11, 12])
    return run_id


@pytest.mark.parametrize(("po_seq", "oczekiwane"), [
    (0, [2, 3, 7, 11, 12]),
    (1, [2, 3, 7, 11, 12]),
    (2, [3, 7, 11, 12]),          # zdarzenie o numerze po_seq juz jest u pytajacego
    (3, [7, 11, 12]),
    (5, [7, 11, 12]),             # numer, ktorego nie ma
    (11, [12]),
    (12, []),
    (MAKS_SEQ, []),
])
async def test_pobranie_runu_od_numeru_zwraca_tylko_zdarzenia_nowsze_niz_ten_numer(
    swiat, run_z_historia, po_seq, oczekiwane,
):
    tresc = await _pobierz(swiat, run_z_historia, po_seq=po_seq)

    assert _numery(tresc) == oczekiwane
    assert [e["text"] for e in tresc["events"]] == [f"zdarzenie-{n}" for n in oczekiwane]


async def test_pobranie_runu_bez_numeru_zwraca_cala_historie(swiat, run_z_historia):
    tresc = await _pobierz(swiat, run_z_historia)

    assert _numery(tresc) == [2, 3, 7, 11, 12]


async def test_nowsze_zdarzenia_maja_te_sama_tresc_co_w_pelnej_historii(swiat, run_z_historia):
    pelna = await _pobierz(swiat, run_z_historia)

    nowsze = await _pobierz(swiat, run_z_historia, po_seq=7)

    assert nowsze["events"] == pelna["events"][3:]


async def test_pobranie_od_numeru_nie_zmienia_stanu_krokow_ani_pokrycia_runu(swiat):
    run_id = await _run(
        swiat, status=RunStatus.failed, reason="nieudane kroki: lint",
        bramki_zaplanowane=2, bramki_wykonane=1, bramki_pominiete=1, stan_pokrycia="czesciowo",
        bramki_powody={"test": "brak_capability"})
    await _kroki(run_id, swiat.tenant_id, {("lint", 1): StepStatus.failed,
                                           ("test", 1): StepStatus.skipped})
    await _zdarzenia(run_id, swiat.tenant_id, [1], kind="step_skipped", step_id="test",
                     tekst="brak_capability: brak narzedzia")
    await _zdarzenia(run_id, swiat.tenant_id, [2])
    pelna = await _pobierz(swiat, run_id)

    najnowsze = await _pobierz(swiat, run_id, po_seq=2)

    assert najnowsze["events"] == []
    assert {k: v for k, v in najnowsze.items() if k != "events"} == {
        k: v for k, v in pelna.items() if k != "events"}
    # kontrast: pola niepuste, wiec porownanie wyzej cos sprawdza; powod pominiecia kroku
    # pochodzi ze stanu, nie z odcietych zdarzen
    assert len(najnowsze["steps"]) == 2
    assert najnowsze["pokrycie"]["stan"] == "czesciowo"
    assert (najnowsze["status"], najnowsze["reason"]) == ("failed", "nieudane kroki: lint")


async def test_pobranie_od_numeru_nie_pokazuje_zdarzen_innego_zespolu_ani_innego_runu(swiat):
    run_id = await _run(swiat)
    inny_run = await _run(swiat)
    await _zdarzenia(run_id, swiat.tenant_id, [1, 2])
    await _zdarzenia(run_id, swiat.obcy_tenant_id, [3])
    await _zdarzenia(inny_run, swiat.tenant_id, [4])

    tresc = await _pobierz(swiat, run_id, po_seq=1)

    assert _numery(tresc) == [2]


async def test_pobranie_od_numeru_runu_innego_zespolu_to_run_nieznany(swiat):
    obcy_run = await _run(swiat, tenant_id=swiat.obcy_tenant_id)
    await _zdarzenia(obcy_run, swiat.obcy_tenant_id, [1, 2])

    odpowiedz = await swiat.klient.get(f"/api/runs/{obcy_run}", params={"po_seq": 1})

    assert odpowiedz.status_code == 404


@pytest.mark.parametrize("po_seq", ["-1", str(MAKS_SEQ + 1), "abc", "1.5"])
async def test_numer_spoza_zakresu_numerow_zdarzen_jest_odrzucany(swiat, run_z_historia, po_seq):
    odpowiedz = await swiat.klient.get(f"/api/runs/{run_z_historia}", params={"po_seq": po_seq})

    assert odpowiedz.status_code == 422, odpowiedz.text


def test_kontrakt_opisuje_numer_zdarzenia_jako_opcjonalny_od_zera_do_granicy_kolumny():
    parametry = app.openapi()["paths"]["/api/runs/{run_id}"]["get"]["parameters"]

    po_seq = [p for p in parametry if p["name"] == "po_seq"]

    assert len(po_seq) == 1, parametry
    assert po_seq[0]["in"] == "query" and not po_seq[0].get("required", False)
    schemat = po_seq[0]["schema"]
    assert (schemat["type"], schemat["minimum"], schemat["maximum"], schemat["default"]) == (
        "integer", 0, MAKS_SEQ, 0)


# =====================================================================================
# Koszt zapisu zdarzenia
# =====================================================================================


@contextmanager
def _zapytania():
    """Kazde zapytanie wyslane do bazy przez silnik testu."""
    wykonane: list[str] = []

    def zanotuj(conn, cursor, statement, parameters, context, executemany):
        wykonane.append(statement)

    silnik = db_session.engine.sync_engine
    sa_event.listen(silnik, "before_cursor_execute", zanotuj)
    try:
        yield wykonane
    finally:
        sa_event.remove(silnik, "before_cursor_execute", zanotuj)


def _odczyty_krokow(zapytania: list[str]) -> int:
    return sum(1 for z in zapytania
               if z.lstrip().startswith("SELECT") and re.search(r"FROM step_run\b", z))


async def _zapisz(swiat: Swiat, run_id: str, kind: str, **pola) -> list[str]:
    """Produkcyjny `zapisz_zdarzenie`; zwraca zapytania wykonane przez sam zapis."""
    async with db_session.SessionLocal() as s:
        run = await uslugi_runow.run_dla_najemcy(s, run_id, swiat.tenant_id)
        with _zapytania() as wykonane:
            _, nowe = await uslugi_runow.zapisz_zdarzenie(
                s, run, event_id=str(uuid.uuid4()), kind=kind, **pola)
    assert nowe and wykonane, "zapis nie wykonal zadnego zapytania: licznik nic nie mierzy"
    return wykonane


async def _run_przed_koncem(swiat: Swiat, kroki: dict, dlugosc: int = 1) -> str:
    """Run w toku z krokami `kroki`; `dlugosc` to liczba zdarzen output, dodatkowych prob
    i powtorzonych pominiec w historii."""
    run_id = await _run(swiat)
    await _kroki(run_id, swiat.tenant_id, {
        **{("s0", proba): StepStatus.finished for proba in range(1, dlugosc + 1)}, **kroki})
    await _zdarzenia(run_id, swiat.tenant_id, range(1, dlugosc + 1))
    await _zdarzenia(run_id, swiat.tenant_id, range(dlugosc + 1, 2 * dlugosc + 1),
                     kind="step_skipped", step_id="test", tekst="brak_capability: x")
    return run_id


# Kazdy koniec runu: kroki przed nim, lista krokow weryfikujacych, oczekiwany skutek
# (status, powod, wykonane bramki) i limit zapytan zapisu.
# Przed HAR-202 (wyliczone z kodu): 9, 8, 7 i 8 zapytan, w tym 3, 2, 2 i 3 odczyty tych samych
# wierszy krokow; limit to ta liczba bez powtorzonych odczytow.
KONCE_RUNU = {
    "sukces_z_pokryciem": (
        {("lint", 1): StepStatus.finished, ("test", 1): StepStatus.skipped},
        ["lint", "test"], (RunStatus.to_accept, None, 1), 7),
    "porazka_z_pokryciem": (
        {("lint", 1): StepStatus.failed, ("test", 1): StepStatus.skipped},
        ["lint", "test"], (RunStatus.failed, "nieudane kroki: lint", 1), 7),
    "sukces_bez_pokrycia": (
        {("lint", 1): StepStatus.finished},
        None, (RunStatus.to_accept, None, None), 6),
    "brak_dowodu_z_pokryciem": (
        {("lint", 1): StepStatus.running},
        ["lint"], (RunStatus.failed, uslugi_runow.POWOD_BRAK_DOWODU, None), 6),
}


@pytest.mark.parametrize("nazwa", list(KONCE_RUNU))
async def test_zakonczenie_runu_czyta_stan_krokow_tylko_raz(swiat, nazwa):
    kroki, weryfikujace, (status, powod, wykonane_bramki), limit = KONCE_RUNU[nazwa]
    run_id = await _run_przed_koncem(swiat, kroki)

    zapytania = await _zapisz(swiat, run_id, "run_finished", kroki_weryfikujace=weryfikujace)

    # skutek w bazie bez zmian: oszczednosc nie moze zmienic werdyktu
    async with db_session.SessionLocal() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
    assert (run.status, run.reason, run.bramki_wykonane) == (status, powod, wykonane_bramki)
    assert _odczyty_krokow(zapytania) == 1, "\n".join(zapytania)
    assert len(zapytania) <= limit, "\n".join(zapytania)


@pytest.mark.parametrize(("kind", "pola", "odczyty_stanu_kroku"), [
    ("output", {"step_id": "s0"}, 0),                      # nie zmienia stanu zadnego kroku
    ("step_started", {"step_id": "nowy"}, 1),              # tylko wiersz tej proby
    ("step_finished", {"step_id": "s0", "wyslane_output": 0}, 1),
    ("step_aborted", {"step_id": "s0"}, 1),
])
async def test_zdarzenie_inne_niz_koniec_runu_nie_czyta_stanu_wszystkich_krokow(
    swiat, kind, pola, odczyty_stanu_kroku,
):
    run_id = await _run_przed_koncem(swiat, {("lint", 1): StepStatus.finished})

    zapytania = await _zapisz(swiat, run_id, kind, **pola)

    assert _odczyty_krokow(zapytania) == odczyty_stanu_kroku, "\n".join(zapytania)


@pytest.mark.parametrize(("kind", "pola"), [
    ("output", {"step_id": "s0"}),
    ("step_started", {"step_id": "nowy"}),
    ("step_finished", {"step_id": "s0", "wyslane_output": 0}),
    ("step_aborted", {"step_id": "s0"}),
    ("run_finished", {"kroki_weryfikujace": ["lint", "test"]}),
])
async def test_liczba_zapytan_zapisu_zdarzenia_nie_rosnie_z_dlugoscia_historii(swiat, kind, pola):
    kroki = {("lint", 1): StepStatus.finished, ("test", 1): StepStatus.skipped}
    krotki = await _run_przed_koncem(swiat, kroki, dlugosc=1)
    dlugi = await _run_przed_koncem(swiat, kroki, dlugosc=60)

    na_krotkim = await _zapisz(swiat, krotki, kind, **pola)
    na_dlugim = await _zapisz(swiat, dlugi, kind, **pola)

    assert len(na_dlugim) == len(na_krotkim), "\n".join(na_dlugim)
