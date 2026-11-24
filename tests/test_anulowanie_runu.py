"""HAR-126: czlowiek anuluje trwajacy run (`POST /api/runs/{run_id}/cancel`).

Run anulowany ma status `aborted` z powodem "anulowany przez czlowieka" (nie `failed`),
kokpit zapisuje zdarzenie `run_cancelled` (z `user_id`) i wysyla runnerowi polecenie
`cancel_run`. Anulowac mozna run `running` albo `waiting`; kazdy inny stan to 409, run
innego zespolu to 404, viewer dostaje 403 (osobno w `test_dostep_http.py`).

Skutki czytamy z bazy (nowa sesja), przez prawdziwa aplikacje i prawdziwy serwis
(`cockpit.services.runs`). Wyscig `cancel` <-> `run_finished` idzie na PostgreSQL
(marker `baza`), dwiema sesjami. Test z prawdziwym procesem potomnym laczy kokpit
z prawdziwym `Runner` (atrapa kokpitu tylko po stronie HTTP runnera).

Funkcje serwisu wolamy przez modul (`uslugi.anuluj_run`), a nie importem na gorze pliku:
przed implementacja testy oblewaja sie na braku funkcji, a nie na bledzie zbierania.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import uuid
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cockpit.api import bus
from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import (
    Base,
    Event,
    RolaWZespole,
    Run,
    Runner,
    RunStatus,
    StepRun,
    StepStatus,
)
from cockpit.services import runs as uslugi
from cockpit.services.errors import NaruszonyNiezmiennik, NieZnaleziono, StanNiemozliwy
from procesy import (
    czekaj,
    czekaj_na_warunek,
    grupa_umarla,
    zadanie_tla,
    zapamietaj_proces,
)
from runner import client as runner_client
from runner.client import Runner as RunnerKlienta
from sesja_testowa import zaloguj_klienta, zaloz_czlonka, zaloz_runnera, zaloz_wlasciciela
from test_runner_klient import (
    KOD_AGENTA_Z_WNUKIEM,
    Odczekania,
    StrumienPolecen,
    kokpit_ze_strumieniem,
)

POWOD = "anulowany przez człowieka"
KIND = "run_cancelled"
TOKEN_A, TOKEN_B = "token-zespolu-a", "token-zespolu-b"
LIMIT_S = 15

URL = os.environ.get("COCKPIT_TEST_DATABASE_URL", "")
tylko_postgres = pytest.mark.skipif(
    not URL.startswith("postgresql"),
    reason="wyscig dwoch sesji wymaga PostgreSQL: ustaw "
    "COCKPIT_TEST_DATABASE_URL=postgresql+asyncpg://.../cockpit_test",
)


@pytest.fixture(autouse=True)
def katalog_runow_runnera(tmp_path, monkeypatch):
    """`cancel_run` zamyka checkpoint runu w katalogu runow runnera: testy nie dotykaja ~/.cockpit."""
    monkeypatch.setenv("COCKPIT_RUNS_DIR", str(tmp_path / "katalog-runow-runnera"))


# --- swiat: dwa zespoly; w A wlasciciel, czlonek (autor anulowania) i obserwator ----------------

@pytest.fixture
async def swiat(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'anulowanie.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    wlasciciel_a, tenant_a = await zaloz_wlasciciela(maker, "a@example.com", nazwa_zespolu="Browarek")
    wlasciciel_b, tenant_b = await zaloz_wlasciciela(maker, "b@example.com", nazwa_zespolu="Piekarnia")
    czlonek_a = await zaloz_czlonka(maker, tenant_a, RolaWZespole.member, "czlonek-a@example.com")
    runner_a = await zaloz_runnera(maker, tenant_a, "runner-a", TOKEN_A)
    runner_b = await zaloz_runnera(maker, tenant_b, "runner-b", TOKEN_B)
    klienci: list[AsyncClient] = []

    async def zalogowany(user) -> AsyncClient:
        k = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        klienci.append(k)
        await zaloguj_klienta(k, maker, user)
        return k

    run_ids: list[str] = []

    async def run(tenant_id: str, runner_id: str, status: RunStatus = RunStatus.running,
                  reason: str | None = None) -> str:
        async with maker() as s:
            r = Run(tenant_id=tenant_id, runner_id=runner_id, status=status, reason=reason)
            s.add(r)
            await s.commit()
            run_ids.append(r.id)
            return r.id

    yield SimpleNamespace(
        maker=maker, zalogowany=zalogowany, run=run,
        tenant_a=tenant_a, tenant_b=tenant_b, runner_a=runner_a, runner_b=runner_b,
        wlasciciel_a=wlasciciel_a, czlonek_a=czlonek_a, wlasciciel_b=wlasciciel_b,
    )
    for k in klienci:
        await k.aclose()
    for rid in (runner_a, runner_b):
        bus._runner_queues.pop(rid, None)
    for run_id in run_ids:
        bus._run_subscribers.pop(run_id, None)
    await engine.dispose()


async def _stan(swiat, run_id: str) -> tuple[RunStatus, str | None]:
    async with swiat.maker() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        return run.status, run.reason


async def _zdarzenia(swiat, run_id: str) -> list[Event]:
    async with swiat.maker() as s:
        return list((await s.execute(
            select(Event).where(Event.run_id == run_id).order_by(Event.seq))).scalars().all())


async def _rodzaje(swiat, run_id: str) -> list[str]:
    return [e.kind for e in await _zdarzenia(swiat, run_id)]


async def _dopisz_output(swiat, run_id: str, tenant_id: str, ile: int) -> None:
    async with swiat.maker() as s:
        for seq in range(1, ile + 1):
            s.add(Event(tenant_id=tenant_id, run_id=run_id, seq=seq, kind="output", text=f"l{seq}"))
        await s.commit()


def _polecenia(runner_id: str) -> list[dict]:
    """Polecenia czekajace w kolejce runnera (opróżnia ją)."""
    q = bus.runner_queue(runner_id)
    wynik = []
    while not q.empty():
        wynik.append(q.get_nowait())
    return wynik


def _anuluj(klient: AsyncClient, run_id: str):
    return klient.post(f"/api/runs/{run_id}/cancel")


def _zdarzenie_runnera(klient: AsyncClient, run_id: str, kind: str, **pola):
    tresc = {"run_id": run_id, "kind": kind, "step_id": "s0",
             "event_id": str(uuid.uuid4()), **pola}
    return klient.post("/api/events", json=tresc, headers={"Authorization": f"Bearer {TOKEN_A}"})


def _tresc_zdarzenia(ev: Event) -> str:
    """Tekst i dane zdarzenia razem: kokpit moze zapisac `user_id` w jednym albo w drugim."""
    return (ev.text or "") + json.dumps(ev.dane or {})


# --- run trwajacy i oczekujacy: aborted z powodem ----------------------------------------------

async def test_anulowanie_trwajacego_runu_daje_odpowiedz_ze_statusem_aborted_i_powodem(swiat):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)
    klient = await swiat.zalogowany(swiat.czlonek_a)

    odp = await _anuluj(klient, run_id)

    assert odp.status_code == 200
    assert odp.json() == {"run_id": run_id, "status": "aborted", "reason": POWOD}


async def test_anulowany_trwajacy_run_ma_w_bazie_status_aborted_a_nie_failed_z_powodem(swiat):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)
    klient = await swiat.zalogowany(swiat.czlonek_a)

    await _anuluj(klient, run_id)

    assert await _stan(swiat, run_id) == (RunStatus.aborted, POWOD)


async def test_anulowany_oczekujacy_run_ma_w_bazie_status_aborted_z_powodem_bez_zadania_u_runnera(
    swiat,
):
    # run na bramce nie ma zadania u runnera: o `aborted` decyduje wylacznie kokpit
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.waiting)
    klient = await swiat.zalogowany(swiat.czlonek_a)

    odp = await _anuluj(klient, run_id)

    assert odp.status_code == 200
    assert odp.json() == {"run_id": run_id, "status": "aborted", "reason": POWOD}
    assert await _stan(swiat, run_id) == (RunStatus.aborted, POWOD)


async def test_anulowanie_nie_wymaga_polaczonego_runnera_bo_run_i_tak_ma_zostac_przerwany(swiat):
    assert swiat.runner_a not in bus.connected_runner_ids()
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)
    klient = await swiat.zalogowany(swiat.czlonek_a)

    odp = await _anuluj(klient, run_id)

    assert odp.status_code == 200
    assert await _stan(swiat, run_id) == (RunStatus.aborted, POWOD)


# --- zdarzenie run_cancelled: autor, zespol, numer ----------------------------------------------

@pytest.mark.parametrize("status", [RunStatus.running, RunStatus.waiting])
async def test_anulowanie_zapisuje_jedno_zdarzenie_run_cancelled_w_zespole_runu(swiat, status):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, status)
    klient = await swiat.zalogowany(swiat.czlonek_a)

    await _anuluj(klient, run_id)

    zdarzenia = await _zdarzenia(swiat, run_id)
    assert [(e.kind, e.tenant_id) for e in zdarzenia] == [(KIND, swiat.tenant_a)]


async def test_zdarzenie_anulowania_niesie_id_uzytkownika_ktory_anulowal_a_nie_wlasciciela(swiat):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)
    klient = await swiat.zalogowany(swiat.czlonek_a)

    await _anuluj(klient, run_id)

    (zdarzenie,) = await _zdarzenia(swiat, run_id)
    tresc = _tresc_zdarzenia(zdarzenie)
    assert swiat.czlonek_a.id in tresc
    assert swiat.wlasciciel_a.id not in tresc


async def test_zdarzenie_anulowania_dostaje_kolejny_numer_w_obrebie_runu(swiat):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)
    inny = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)
    await _dopisz_output(swiat, run_id, swiat.tenant_a, 2)
    await _dopisz_output(swiat, inny, swiat.tenant_a, 5)    # inny run nie przesuwa numeracji
    klient = await swiat.zalogowany(swiat.czlonek_a)

    await _anuluj(klient, run_id)

    assert [(e.seq, e.kind) for e in await _zdarzenia(swiat, run_id)] == [
        (1, "output"), (2, "output"), (3, KIND)]


async def test_anulowanie_jednego_runu_nie_rusza_innego_runu_tego_samego_zespolu(swiat):
    anulowany = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)
    inny = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)
    klient = await swiat.zalogowany(swiat.czlonek_a)

    await _anuluj(klient, anulowany)

    assert await _stan(swiat, anulowany) == (RunStatus.aborted, POWOD)
    assert await _stan(swiat, inny) == (RunStatus.running, None)
    assert await _zdarzenia(swiat, inny) == []


# --- polecenie dla runnera i szyna ---------------------------------------------------------------

@pytest.mark.parametrize("status", [RunStatus.running, RunStatus.waiting])
async def test_anulowanie_wysyla_runnerowi_runu_dokladnie_jedno_polecenie_cancel_run(swiat, status):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, status)
    klient = await swiat.zalogowany(swiat.czlonek_a)

    await _anuluj(klient, run_id)

    assert _polecenia(swiat.runner_a) == [{"type": "cancel_run", "run_id": run_id}]
    assert _polecenia(swiat.runner_b) == []


async def test_anulowanie_jest_publikowane_przegladarce_jako_zdarzenie_run_cancelled(swiat):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)
    klient = await swiat.zalogowany(swiat.czlonek_a)
    q = bus.subscribe_run(run_id)
    try:
        await _anuluj(klient, run_id)
        ev = await asyncio.wait_for(q.get(), 2)
    finally:
        bus.unsubscribe_run(run_id, q)

    assert (ev["seq"], ev["kind"]) == (1, KIND)


# --- uslugi: kontrakt funkcji serwisu ------------------------------------------------------------

async def test_serwis_anulowania_zwraca_run_zdarzenie_i_polecenie_dla_runnera(swiat):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)

    async with swiat.maker() as s:
        run, zdarzenie, polecenie = await uslugi.anuluj_run(
            s, run_id, swiat.tenant_a, swiat.czlonek_a.id)

    assert (run.id, run.status, run.reason) == (run_id, RunStatus.aborted, POWOD)
    assert (zdarzenie.kind, zdarzenie.run_id, zdarzenie.tenant_id) == (KIND, run_id, swiat.tenant_a)
    assert polecenie == {"type": "cancel_run", "run_id": run_id}


async def test_serwis_anulowania_runu_obcego_zespolu_rzuca_nieznaleziono_i_nic_nie_zmienia(swiat):
    run_obcy = await swiat.run(swiat.tenant_b, swiat.runner_b, RunStatus.running)

    async with swiat.maker() as s:
        with pytest.raises(NieZnaleziono):
            await uslugi.anuluj_run(s, run_obcy, swiat.tenant_a, swiat.czlonek_a.id)

    assert await _stan(swiat, run_obcy) == (RunStatus.running, None)
    assert await _zdarzenia(swiat, run_obcy) == []


async def test_serwis_anulowania_nieznanego_runu_rzuca_nieznaleziono(swiat):
    async with swiat.maker() as s:
        with pytest.raises(NieZnaleziono):
            await uslugi.anuluj_run(s, "nieznany-run", swiat.tenant_a, swiat.czlonek_a.id)


# --- tresc zdarzenia, blokada wiersza, komunikaty i kolizja numeru (bez PostgreSQL) --------------

@pytest.fixture
def zapytania_o_runy_dla_postgresa():
    """SELECT-y z tabeli runow wykonane przez sesje ORM, skompilowane dla PostgreSQL: SQLite nie
    renderuje FOR UPDATE, wiec blokade widac dopiero w zapytaniu dla PostgreSQL."""
    import re

    from sqlalchemy import event
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.orm import Session

    zebrane: list[str] = []

    def podsluch(stan):
        if not stan.is_select:
            return
        sql = str(stan.statement.compile(dialect=postgresql.dialect()))
        if re.search(rf"FROM {Run.__tablename__}\b", sql):
            zebrane.append(sql)

    event.listen(Session, "do_orm_execute", podsluch)
    try:
        yield zebrane
    finally:
        event.remove(Session, "do_orm_execute", podsluch)


async def test_anulowanie_czyta_status_runu_pod_blokada_wiersza(swiat, zapytania_o_runy_dla_postgresa):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)

    async with swiat.maker() as s:
        await uslugi.anuluj_run(s, run_id, swiat.tenant_a, swiat.czlonek_a.id)

    assert [q for q in zapytania_o_runy_dla_postgresa if "FOR UPDATE" in q]


async def test_zdarzenie_anulowania_ma_tekst_powodu_pelna_dlugosc_i_dane_z_id_uzytkownika(swiat):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)
    klient = await swiat.zalogowany(swiat.czlonek_a)

    await _anuluj(klient, run_id)

    (zdarzenie,) = await _zdarzenia(swiat, run_id)
    assert zdarzenie.text == POWOD
    assert zdarzenie.text_full_len == len(POWOD)
    assert zdarzenie.text_truncated is False
    assert zdarzenie.dane == {"user_id": swiat.czlonek_a.id}


async def test_odmowa_anulowania_podaje_stan_runu_i_dozwolone_stany_w_komunikacie(swiat):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.to_accept)

    async with swiat.maker() as s:
        with pytest.raises(StanNiemozliwy) as wyjatek:
            await uslugi.anuluj_run(s, run_id, swiat.tenant_a, swiat.czlonek_a.id)

    assert str(wyjatek.value) == (
        "run w stanie to_accept nie moze byc anulowany (tylko run w toku albo oczekujacy)")


async def test_odmowa_anulowania_konczy_transakcje_sesji_zamiast_zostawic_ja_z_blokada_wiersza(swiat):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.to_accept)

    async with swiat.maker() as s:
        with pytest.raises(StanNiemozliwy):
            await uslugi.anuluj_run(s, run_id, swiat.tenant_a, swiat.czlonek_a.id)

        assert not s.in_transaction()      # blokada wiersza zwolniona w tej samej sesji


def _zajety_numer(
    monkeypatch, zajety: int, zawsze: bool = False, ile_razy: int = 1,
) -> list[int]:
    """Podstawia `_nastepny_seq`: pierwsze `ile_razy` wywolan (albo kazde, gdy `zawsze`) zwraca
    zajety numer. Zwraca liste, do ktorej trafia wpis przy kazdym wywolaniu (liczba prob)."""
    prawdziwa = uslugi._nastepny_seq
    wywolania: list[int] = []

    async def atrapa(s, run_id, tenant_id):
        wywolania.append(1)
        if zawsze or len(wywolania) <= ile_razy:
            return zajety
        return await prawdziwa(s, run_id, tenant_id)

    monkeypatch.setattr(uslugi, "_nastepny_seq", atrapa)
    return wywolania


async def test_anulowanie_udaje_sie_w_ostatniej_dozwolonej_probie_po_kolizjach_numeru(
    swiat, monkeypatch,
):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)
    await _dopisz_output(swiat, run_id, swiat.tenant_a, 1)
    wywolania = _zajety_numer(monkeypatch, zajety=1, ile_razy=uslugi._PROBY_SEQ - 1)

    async with swiat.maker() as s:
        await uslugi.anuluj_run(s, run_id, swiat.tenant_a, swiat.czlonek_a.id)

    assert len(wywolania) == uslugi._PROBY_SEQ
    assert await _stan(swiat, run_id) == (RunStatus.aborted, POWOD)
    assert [(e.seq, e.kind) for e in await _zdarzenia(swiat, run_id)] == [
        (1, "output"), (2, KIND)]


async def test_kolizja_numeru_zdarzenia_przy_anulowaniu_jest_ponawiana_i_run_zostaje_anulowany(
    swiat, monkeypatch,
):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)
    await _dopisz_output(swiat, run_id, swiat.tenant_a, 1)
    _zajety_numer(monkeypatch, zajety=1)

    async with swiat.maker() as s:
        await uslugi.anuluj_run(s, run_id, swiat.tenant_a, swiat.czlonek_a.id)

    assert await _stan(swiat, run_id) == (RunStatus.aborted, POWOD)
    assert [(e.seq, e.kind) for e in await _zdarzenia(swiat, run_id)] == [
        (1, "output"), (2, KIND)]


async def test_inny_blad_zapisu_niz_kolizja_numeru_przy_anulowaniu_nie_jest_ponawiany_ani_polykany(
    swiat, monkeypatch,
):
    from sqlalchemy.exc import IntegrityError

    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)
    await _dopisz_output(swiat, run_id, swiat.tenant_a, 1)
    _zajety_numer(monkeypatch, zajety=1)
    monkeypatch.setattr(uslugi, "_kolizja_numeru", lambda e: False)

    async with swiat.maker() as s:
        with pytest.raises(IntegrityError):
            await uslugi.anuluj_run(s, run_id, swiat.tenant_a, swiat.czlonek_a.id)

    assert await _stan(swiat, run_id) == (RunStatus.running, None)
    assert [e.kind for e in await _zdarzenia(swiat, run_id)] == ["output"]


async def test_anulowanie_przy_stale_zajetym_numerze_zdarzenia_odmawia_po_wyczerpaniu_prob_i_nie_rusza_runu(
    swiat, monkeypatch,
):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)
    await _dopisz_output(swiat, run_id, swiat.tenant_a, 1)
    wywolania = _zajety_numer(monkeypatch, zajety=1, zawsze=True)

    async with swiat.maker() as s:
        with pytest.raises(StanNiemozliwy) as wyjatek:
            await uslugi.anuluj_run(s, run_id, swiat.tenant_a, swiat.czlonek_a.id)

    assert len(wywolania) == uslugi._PROBY_SEQ == 5
    assert str(wyjatek.value) == (
        "nie udalo sie nadac numeru zdarzenia po 5 probach - ponow zapis")
    assert await _stan(swiat, run_id) == (RunStatus.running, None)
    assert [e.kind for e in await _zdarzenia(swiat, run_id)] == ["output"]


# --- stany, w ktorych nie wolno anulowac ---------------------------------------------------------

# Lista niezalezna od kodu: dopisanie nowego stanu do `RunStatus` wymusza decyzje w tescie ponizej.
ANULOWALNE = {RunStatus.running, RunStatus.waiting}
NIEANULOWALNE = [RunStatus.pending, RunStatus.to_accept, RunStatus.failed, RunStatus.skipped,
                 RunStatus.aborted, RunStatus.rejected]


def test_kazdy_stan_runu_jest_albo_anulowalny_albo_odrzucany_z_409():
    assert ANULOWALNE | set(NIEANULOWALNE) == set(RunStatus)
    assert not ANULOWALNE & set(NIEANULOWALNE)


@pytest.mark.parametrize("status", NIEANULOWALNE, ids=[s.value for s in NIEANULOWALNE])
async def test_anulowanie_runu_w_stanie_innym_niz_w_toku_lub_oczekujacy_daje_409_bez_zmian(
    swiat, status,
):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, status, reason="stary powod")
    klient = await swiat.zalogowany(swiat.czlonek_a)

    odp = await _anuluj(klient, run_id)

    assert odp.status_code == 409
    assert await _stan(swiat, run_id) == (status, "stary powod")
    assert await _zdarzenia(swiat, run_id) == []
    assert _polecenia(swiat.runner_a) == []


async def test_drugie_anulowanie_tego_samego_runu_daje_409_i_nie_dopisuje_zdarzenia(swiat):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)
    klient = await swiat.zalogowany(swiat.czlonek_a)
    assert (await _anuluj(klient, run_id)).status_code == 200
    _polecenia(swiat.runner_a)

    odp = await _anuluj(klient, run_id)

    assert odp.status_code == 409
    assert await _rodzaje(swiat, run_id) == [KIND]
    assert await _stan(swiat, run_id) == (RunStatus.aborted, POWOD)
    assert _polecenia(swiat.runner_a) == []


async def test_serwis_anulowania_zakonczonego_runu_rzuca_stan_niemozliwy_i_zostawia_run(swiat):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.to_accept)

    async with swiat.maker() as s:
        with pytest.raises(StanNiemozliwy):
            await uslugi.anuluj_run(s, run_id, swiat.tenant_a, swiat.czlonek_a.id)

    assert await _stan(swiat, run_id) == (RunStatus.to_accept, None)
    assert await _zdarzenia(swiat, run_id) == []


# --- zespol: run innego zespolu jest nieznany ----------------------------------------------------

async def test_anulowanie_runu_obcego_zespolu_daje_404_a_run_i_runnery_zostaja_nietkniete(swiat):
    run_obcy = await swiat.run(swiat.tenant_b, swiat.runner_b, RunStatus.running)
    run_wlasny = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)
    klient = await swiat.zalogowany(swiat.czlonek_a)
    assert (await _anuluj(klient, run_wlasny)).status_code == 200   # 404 to nie brak trasy
    _polecenia(swiat.runner_a)

    odp = await _anuluj(klient, run_obcy)

    assert odp.status_code == 404
    assert await _stan(swiat, run_obcy) == (RunStatus.running, None)
    assert await _zdarzenia(swiat, run_obcy) == []
    assert _polecenia(swiat.runner_a) == [] and _polecenia(swiat.runner_b) == []


async def test_anulowanie_nieistniejacego_runu_daje_404(swiat):
    run_wlasny = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)
    klient = await swiat.zalogowany(swiat.czlonek_a)
    assert (await _anuluj(klient, run_wlasny)).status_code == 200   # 404 to nie brak trasy

    assert (await _anuluj(klient, "nie-ma-takiego-runu")).status_code == 404


# --- runner nie moze zglosic anulowania przez czlowieka ------------------------------------------

async def test_runner_nie_moze_zglosic_zdarzenia_run_cancelled_bo_to_decyzja_czlowieka(swiat):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)
    klient = await swiat.zalogowany(swiat.czlonek_a)

    odp = await _zdarzenie_runnera(klient, run_id, KIND, text=POWOD)

    assert odp.status_code == 422
    assert await _zdarzenia(swiat, run_id) == []
    assert await _stan(swiat, run_id) == (RunStatus.running, None)


async def test_zdarzenie_run_cancelled_od_runnera_odrzucone_przez_serwis_z_niezmiennikiem(swiat):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.waiting)

    async with swiat.maker() as s:
        run = await s.get(Run, run_id)
        with pytest.raises(NaruszonyNiezmiennik) as wyjatek:
            await uslugi.zapisz_zdarzenie(s, run, event_id=str(uuid.uuid4()), kind=KIND)

    assert wyjatek.value.nazwa == "approval_required"
    assert await _stan(swiat, run_id) == (RunStatus.waiting, None)
    assert await _zdarzenia(swiat, run_id) == []


# --- zdarzenia runnera po anulowaniu nie cofaja aborted ------------------------------------------

async def test_koniec_kroku_i_runu_zgloszone_po_anulowaniu_nie_zmieniaja_statusu_ani_powodu(swiat):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)
    klient = await swiat.zalogowany(swiat.czlonek_a)
    await _anuluj(klient, run_id)

    for kind in ("step_finished", "run_finished"):
        assert (await _zdarzenie_runnera(klient, run_id, kind)).status_code == 200

    assert await _stan(swiat, run_id) == (RunStatus.aborted, POWOD)
    assert await _rodzaje(swiat, run_id) == [KIND, "step_finished", "run_finished"]


async def test_przerwanie_kroku_zgloszone_po_anulowaniu_nie_nadpisuje_powodu_anulowania(swiat):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)
    klient = await swiat.zalogowany(swiat.czlonek_a)
    await _anuluj(klient, run_id)

    odp = await _zdarzenie_runnera(klient, run_id, "step_aborted", text="inny powod")

    assert odp.status_code == 200
    assert await _stan(swiat, run_id) == (RunStatus.aborted, POWOD)


async def test_decyzja_na_bramce_anulowanego_runu_daje_409_i_nie_wznawia_runu(swiat):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.waiting)
    klient = await swiat.zalogowany(swiat.czlonek_a)
    await _anuluj(klient, run_id)
    _polecenia(swiat.runner_a)

    odp = await klient.post(f"/api/runs/{run_id}/decision", json={"decyzja": "approve"})

    assert odp.status_code == 409
    assert await _stan(swiat, run_id) == (RunStatus.aborted, POWOD)
    assert _polecenia(swiat.runner_a) == []     # runner nie dostaje `resume` anulowanego runu


# --- polecenie cancel_run dochodzi do runnera takze po jego powrocie ----------------------------

# `cancel_run` nie jest krokiem runu (`_POLECENIA_RUNU`): polecenie_aktualne przepuszcza je dla runu
# `aborted`. Gdyby trafilo na te liste, runner po powrocie nigdy by go nie dostal.

async def test_polecenie_cancel_run_anulowanego_runu_jest_aktualne_dla_runnera(swiat):
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)
    klient = await swiat.zalogowany(swiat.czlonek_a)
    await _anuluj(klient, run_id)
    (polecenie,) = _polecenia(swiat.runner_a)

    async with swiat.maker() as s:
        aktualne = await uslugi.polecenie_aktualne(s, polecenie, swiat.runner_a, swiat.tenant_a)

    assert aktualne is True


# --- od POST cancel do zabitej grupy procesow: kokpit + prawdziwy Runner + prawdziwy proces ----

async def test_anulowanie_przez_api_zabija_grupe_procesow_kroku_u_runnera_i_run_zostaje_aborted(
    swiat, tmp_path, monkeypatch,
):
    plik = tmp_path / "agent.pid"
    monkeypatch.setattr(runner_client, "OPERACJE_RUNU", {
        "spij": (sys.executable, "-I", "-c", KOD_AGENTA_Z_WNUKIEM, str(plik))})
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)
    klient = await swiat.zalogowany(swiat.czlonek_a)
    strumien = StrumienPolecen()
    transport, zdarzenia = kokpit_ze_strumieniem(strumien)
    runner = RunnerKlienta("http://kokpit.test", TOKEN_A, transport=transport,
                           odczekaj=Odczekania())
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij({"type": "run_step", "run_id": run_id, "step_id": "s0",
                             "operacja": "spij"})
            await czekaj_na_warunek(plik.exists, LIMIT_S, "agent zapisal pid i pgid")
            _, pgid, _ = (int(x) for x in plik.read_text().split())
            zapamietaj_proces(pgid)

            assert (await _anuluj(klient, run_id)).status_code == 200
            (polecenie,) = _polecenia(swiat.runner_a)      # to, co kokpit wysyla w dol strumienia
            strumien.wyslij(polecenie)

            assert await grupa_umarla(pgid, limit=LIMIT_S), "grupa procesow kroku przezyla anulowanie"
            # zadanie kroku skonczylo sie anulowaniem: nic nie zmieni aborted po fakcie
            await czekaj_na_warunek(lambda: not runner._runy, LIMIT_S, "koniec zadania kroku")
    finally:
        await czekaj(runner.stop(), LIMIT_S, "stop() runnera")

    assert await _stan(swiat, run_id) == (RunStatus.aborted, POWOD)
    assert not {z["kind"] for z in zdarzenia} & {"step_finished", "step_failed", "run_finished"}


# --- step_aborted po anulowaniu: zdarzenia runnera trafiaja do prawdziwej bazy ----------------------

def _kokpit_z_prawdziwa_aplikacja(strumien: StrumienPolecen, klient_app: AsyncClient):
    """Rejestracja i strumien polecen to atrapa; zdarzenia runnera ida do prawdziwej aplikacji
    (token runnera, baza), a runner dostaje jej kod odpowiedzi."""

    async def handler(request: httpx.Request) -> httpx.Response:
        sciezka = request.url.path
        if sciezka == "/api/runners/register":
            return httpx.Response(
                200, json={"runner_id": "r-42", "tenant_id": "t1", "heartbeat_co_s": 15})
        if sciezka == "/api/runners/stream":
            return httpx.Response(
                200, stream=strumien, headers={"content-type": "text/event-stream"})
        if sciezka == "/api/events":
            odp = await klient_app.post(
                "/api/events", content=request.content,
                headers={"Authorization": f"Bearer {TOKEN_A}",
                         "content-type": "application/json"})
            return httpx.Response(odp.status_code)
        return httpx.Response(200)

    return httpx.MockTransport(handler)


async def _kroki(swiat, run_id: str) -> list[tuple]:
    async with swiat.maker() as s:
        wiersze = (await s.execute(
            select(StepRun).where(StepRun.run_id == run_id)
            .order_by(StepRun.step_id, StepRun.agent_id, StepRun.attempt))).scalars().all()
        return [(w.step_id, w.agent_id, w.attempt, w.status, w.reason) for w in wiersze]


async def test_po_anulowaniu_przez_api_runner_zglasza_step_aborted_a_w_bazie_krok_nie_jest_w_toku(
    swiat, tmp_path, monkeypatch,
):
    plik = tmp_path / "agent.pid"
    monkeypatch.setattr(runner_client, "OPERACJE_RUNU", {
        "spij": (sys.executable, "-I", "-c", KOD_AGENTA_Z_WNUKIEM, str(plik))})
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)
    klient = await swiat.zalogowany(swiat.czlonek_a)
    klient_runnera = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    strumien = StrumienPolecen()
    runner = RunnerKlienta("http://kokpit.test", TOKEN_A, odczekaj=Odczekania(),
                           transport=_kokpit_z_prawdziwa_aplikacja(strumien, klient_runnera))
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij({"type": "run_step", "run_id": run_id, "step_id": "s0",
                             "operacja": "spij"})
            await czekaj_na_warunek(plik.exists, LIMIT_S, "agent zapisal pid i pgid")
            _, pgid, _ = (int(x) for x in plik.read_text().split())
            zapamietaj_proces(pgid)

            assert (await _anuluj(klient, run_id)).status_code == 200
            (polecenie,) = _polecenia(swiat.runner_a)
            strumien.wyslij(polecenie)

            assert await grupa_umarla(pgid, limit=LIMIT_S), "grupa procesow kroku przezyla anulowanie"
            await czekaj_na_warunek(lambda: not runner._runy, LIMIT_S, "koniec zadania kroku")
    finally:
        await czekaj(runner.stop(), LIMIT_S, "stop() runnera")
        await klient_runnera.aclose()

    # skutki w bazie, nie w odpowiedzi HTTP: krok jest nieudany z powodem anulowania
    assert await _kroki(swiat, run_id) == [("s0", "", 1, StepStatus.failed, POWOD)]
    assert not [k for k in await _kroki(swiat, run_id) if k[3] == StepStatus.running]
    assert await _stan(swiat, run_id) == (RunStatus.aborted, POWOD)      # powod runu zostaje
    assert await _rodzaje(swiat, run_id) == [KIND, "step_aborted"]


async def test_po_anulowaniu_przez_api_krok_workflow_z_agentem_i_proba_nie_zostaje_w_bazie_w_toku(
    swiat, monkeypatch,
):
    # silnik zglasza start kroku z agentem i numerem proby: wiersz kroku w bazie ma ten klucz,
    # wiec `step_aborted` musi trafic w ten sam wiersz, a nie zalozyc drugi
    run_id = await swiat.run(swiat.tenant_a, swiat.runner_a, RunStatus.running)
    wystartowal = asyncio.Event()

    async def wykonaj(cmd, emit):
        await emit({"run_id": cmd["run_id"], "kind": "step_started", "step_id": "b",
                    "agent_id": "b", "attempt": 2, "text": ""})
        wystartowal.set()
        await asyncio.sleep(300)

    monkeypatch.setattr(runner_client.silnik, "wykonaj_run", wykonaj)
    klient = await swiat.zalogowany(swiat.czlonek_a)
    klient_runnera = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    strumien = StrumienPolecen()
    runner = RunnerKlienta("http://kokpit.test", TOKEN_A, odczekaj=Odczekania(),
                           transport=_kokpit_z_prawdziwa_aplikacja(strumien, klient_runnera))
    try:
        async with zadanie_tla(runner.listen(), "nasluch runnera"):
            strumien.wyslij({"type": "run_workflow", "run_id": run_id, "workflow": "w",
                             "sciezka": "/repo"})
            await czekaj(wystartowal.wait(), LIMIT_S, "start kroku w silniku")
            assert await _kroki(swiat, run_id) == [("b", "b", 2, StepStatus.running, None)]

            assert (await _anuluj(klient, run_id)).status_code == 200
            (polecenie,) = _polecenia(swiat.runner_a)
            strumien.wyslij(polecenie)
            await czekaj_na_warunek(lambda: not runner._runy, LIMIT_S, "koniec zadania runu")
    finally:
        await czekaj(runner.stop(), LIMIT_S, "stop() runnera")
        await klient_runnera.aclose()

    # ten sam wiersz (b, b, 2) zmienil stan: bez drugiego wiersza i bez wiersza w toku
    assert await _kroki(swiat, run_id) == [("b", "b", 2, StepStatus.failed, POWOD)]
    assert await _stan(swiat, run_id) == (RunStatus.aborted, POWOD)
    assert await _rodzaje(swiat, run_id) == ["step_started", KIND, "step_aborted"]


# --- wyscig cancel <-> run_finished na PostgreSQL ------------------------------------------------

class SesjaZBramka(AsyncSession):
    """Przed pierwszym `commit` zglasza, ze trzyma blokade wiersza, i czeka na zwolnienie."""

    po_zapisach: asyncio.Event
    zwolnij: asyncio.Event
    zatrzymana = False

    async def commit(self):
        if not self.zatrzymana:
            self.zatrzymana = True
            self.po_zapisach.set()
            await asyncio.wait_for(self.zwolnij.wait(), LIMIT_S)
        await super().commit()


@pytest.fixture
async def maker_pg():
    engine = create_async_engine(URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _swiat_pg(maker) -> SimpleNamespace:
    wlasciciel, tenant_id = await zaloz_wlasciciela(maker, "pg@example.com", nazwa_zespolu="Browarek")
    async with maker() as s:
        runner = Runner(tenant_id=tenant_id, name="runner-pg",
                        token_hash=hashlib.sha256(b"pg").hexdigest())
        s.add(runner)
        await s.flush()
        run = Run(tenant_id=tenant_id, runner_id=runner.id, status=RunStatus.running)
        s.add(run)
        await s.flush()
        # ukonczony krok bez zdarzenia: `run_finished` bez kroku nie dalby `to_accept` (HAR-207),
        # a lista rodzajow zdarzen w asercjach zostaje taka, jak przed ta zmiana
        s.add(StepRun(tenant_id=tenant_id, run_id=run.id, step_id="s0", agent_id="",
                      attempt=1, status=StepStatus.finished))
        await s.commit()
        return SimpleNamespace(tenant_id=tenant_id, user_id=wlasciciel.id, run_id=run.id)


async def _czeka_na_blokade_wiersza(maker) -> bool:
    async with maker() as s:
        return bool((await s.execute(text(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE wait_event_type = 'Lock' AND datname = current_database()"))).scalar_one())


async def _przeplot(maker, swiat_pg, pierwsza: str, druga: str) -> tuple[object, object]:
    """Operacja `pierwsza` ("cancel" albo "koniec" runu) zapisala wszystko i stoi przed commitem,
    trzymajac blokade wiersza; `druga` startuje w drugiej sesji, czeka na blokade (albo
    konczy sie), potem pierwsza dostaje commit. Zwraca (wynik_pierwszej, wynik_drugiej);
    wyjatki jako wartosci."""
    bramka = SesjaZBramka(bind=maker.kw["bind"], expire_on_commit=False)
    bramka.po_zapisach, bramka.zwolnij = asyncio.Event(), asyncio.Event()

    async def cancel(s: AsyncSession):
        return await uslugi.anuluj_run(s, swiat_pg.run_id, swiat_pg.tenant_id, swiat_pg.user_id)

    async def koniec(s: AsyncSession):
        run = await s.get(Run, swiat_pg.run_id)
        return await uslugi.zapisz_zdarzenie(
            s, run, event_id=str(uuid.uuid4()), kind="run_finished", step_id="s0")

    async def wykonaj(operacja, sesja):
        async with sesja:
            return await operacja(sesja)

    sesja_druga = maker()
    operacje = {"cancel": cancel, "koniec": koniec}
    pierwsza_op, druga_op = operacje[pierwsza], operacje[druga]
    zadanie_1 = asyncio.create_task(wykonaj(pierwsza_op, bramka))
    zadanie_2 = None
    try:
        stoi_przed_commitem = asyncio.ensure_future(
            czekaj(bramka.po_zapisach.wait(), LIMIT_S, "pierwsza sesja przed commitem"))
        await asyncio.wait({stoi_przed_commitem, zadanie_1}, timeout=LIMIT_S,
                           return_when=asyncio.FIRST_COMPLETED)
        if not bramka.po_zapisach.is_set():
            stoi_przed_commitem.cancel()
            wynik = zadanie_1.exception() if zadanie_1.done() else "brak commitu w limicie"
            raise AssertionError(f"pierwsza sesja nie doszla do commitu: {wynik!r}")
        zadanie_2 = asyncio.create_task(wykonaj(druga_op, sesja_druga))
        koniec_czekania = asyncio.get_running_loop().time() + LIMIT_S
        while not zadanie_2.done() and not await _czeka_na_blokade_wiersza(maker):
            assert asyncio.get_running_loop().time() < koniec_czekania, (
                "druga sesja ani nie skonczyla, ani nie czeka na blokade wiersza")
            await asyncio.sleep(0.05)
    finally:
        bramka.zwolnij.set()
    wyniki = await asyncio.wait_for(asyncio.gather(zadanie_1, zadanie_2, return_exceptions=True), LIMIT_S)
    return wyniki[0], wyniki[1]


async def _stan_pg(maker, run_id: str) -> tuple[RunStatus, str | None, list[str]]:
    async with maker() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        rodzaje = list((await s.execute(
            select(Event.kind).where(Event.run_id == run_id).order_by(Event.seq))).scalars().all())
        return run.status, run.reason, rodzaje


@pytest.mark.baza
@tylko_postgres
async def test_koniec_runu_zgloszony_w_trakcie_anulowania_nie_cofa_aborted_a_oba_zdarzenia_zostaja(
    maker_pg,
):
    swiat_pg = await _swiat_pg(maker_pg)

    wynik_cancel, wynik_koniec = await _przeplot(maker_pg, swiat_pg, "cancel", "koniec")

    assert not isinstance(wynik_cancel, BaseException), wynik_cancel
    assert not isinstance(wynik_koniec, BaseException), wynik_koniec
    assert await _stan_pg(maker_pg, swiat_pg.run_id) == (
        RunStatus.aborted, POWOD, [KIND, "run_finished"])


@pytest.mark.baza
@tylko_postgres
async def test_anulowanie_po_koncu_runu_zapisanym_w_trakcie_odczytu_daje_409_a_run_zostaje_do_odbioru(
    maker_pg,
):
    # `run_finished` trzyma blokade wiersza, a anulowanie przeczytalo juz `running`: bez
    # sprawdzenia stanu pod blokada (albo warunku w UPDATE) nadpisaloby `to_accept` na `aborted`.
    swiat_pg = await _swiat_pg(maker_pg)

    wynik_koniec, wynik_cancel = await _przeplot(maker_pg, swiat_pg, "koniec", "cancel")

    assert type(wynik_cancel) is StanNiemozliwy, wynik_cancel
    assert not isinstance(wynik_koniec, BaseException), wynik_koniec
    assert await _stan_pg(maker_pg, swiat_pg.run_id) == (
        RunStatus.to_accept, None, ["run_finished"])


@pytest.mark.baza
@tylko_postgres
async def test_drugie_anulowanie_runu_przeplecione_z_pierwszym_daje_409_i_jedno_zdarzenie(maker_pg):
    swiat_pg = await _swiat_pg(maker_pg)

    pierwsze, drugie = await _przeplot(maker_pg, swiat_pg, "cancel", "cancel")

    assert not isinstance(pierwsze, BaseException), pierwsze
    assert type(drugie) is StanNiemozliwy, drugie
    assert await _stan_pg(maker_pg, swiat_pg.run_id) == (RunStatus.aborted, POWOD, [KIND])
