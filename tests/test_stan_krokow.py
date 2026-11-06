"""HAR-78: kokpit trzyma stan kazdego kroku (osobno dla kroku, agenta i proby),
a status runu wynika ze stanow krokow i z `run_finished`, nie z ostatniego zdarzenia.

Skutek czytamy z bazy (tabele `step_run` i `run`), nie z samej odpowiedzi HTTP."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Event, Run, Runner, RunStatus, StepRun, StepStatus, Tenant
from cockpit.settings import settings

from sesja_testowa import zaloguj_klienta, zaloz_wlasciciela

TOKEN = "token-browarka"
NAGLOWEK = {"Authorization": f"Bearer {TOKEN}"}
INT_MAX = 2_147_483_647


@dataclass
class Swiat:
    klient: AsyncClient      # zalogowany wlasciciel zespolu "Browarek"
    run_id: str              # run Browarka, zlecony jego runnerowi
    drugi_run_id: str        # drugi run Browarka (do sprawdzenia zakresu w obrebie runu)
    tenant_id: str           # Browarek
    obcy_tenant_id: str      # Piekarnia
    run_obcego_id: str       # run Piekarni


@pytest.fixture
async def swiat(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    wlasciciel, tenant_id = await zaloz_wlasciciela(maker, nazwa_zespolu="Browarek")
    teraz = datetime.now(timezone.utc)
    async with maker() as s:
        piekarnia = Tenant(name="Piekarnia")
        s.add(piekarnia)
        await s.flush()
        runner = Runner(tenant_id=tenant_id, name="macbook",
                        token_hash=hashlib.sha256(TOKEN.encode()).hexdigest(),
                        last_seen_at=teraz)
        obcy_runner = Runner(tenant_id=piekarnia.id, name="obcy",
                             token_hash=hashlib.sha256(b"token-obcego").hexdigest(),
                             last_seen_at=teraz)
        s.add_all([runner, obcy_runner])
        await s.flush()
        run = Run(tenant_id=tenant_id, runner_id=runner.id, status=RunStatus.running)
        drugi = Run(tenant_id=tenant_id, runner_id=runner.id, status=RunStatus.running)
        obcy = Run(tenant_id=piekarnia.id, runner_id=obcy_runner.id, status=RunStatus.running)
        s.add_all([run, drugi, obcy])
        await s.commit()
        ids = (run.id, drugi.id, tenant_id, piekarnia.id, obcy.id)
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            await zaloguj_klienta(c, maker, wlasciciel)
            yield Swiat(c, *ids)
    finally:
        await engine.dispose()


async def _zdarzenie(swiat: Swiat, kind: str, *, run_id: str | None = None,
                     step_id: str = "s0", agent_id: str | None = None,
                     attempt: int | None = None, text: str = "",
                     event_id: str | None = None, **reszta):
    cialo = {"run_id": run_id or swiat.run_id, "kind": kind, "step_id": step_id,
             "event_id": event_id or str(uuid.uuid4()), "text": text, **reszta}
    if agent_id is not None:
        cialo["agent_id"] = agent_id
    if attempt is not None:
        cialo["attempt"] = attempt
    return await swiat.klient.post("/api/events", json=cialo, headers=NAGLOWEK)


async def _ok(swiat: Swiat, kind: str, **kw) -> None:
    r = await _zdarzenie(swiat, kind, **kw)
    assert r.status_code == 200, r.text


async def _kroki(run_id: str) -> dict[tuple[str, str, int], tuple[StepStatus, str | None]]:
    """Wiersze `step_run` runu: (step_id, agent_id, attempt) -> (status, reason)."""
    async with db_session.SessionLocal() as s:
        wiersze = (await s.execute(select(StepRun).where(StepRun.run_id == run_id))).scalars().all()
    wynik = {(w.step_id, w.agent_id, w.attempt): (w.status, w.reason) for w in wiersze}
    assert len(wynik) == len(wiersze)
    return wynik


async def _run(run_id: str) -> Run:
    async with db_session.SessionLocal() as s:
        return (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()


async def _status_i_powod(run_id: str) -> tuple[RunStatus, str | None]:
    run = await _run(run_id)
    return run.status, run.reason


async def _liczba_wierszy_krokow() -> int:
    async with db_session.SessionLocal() as s:
        return (await s.execute(select(func.count()).select_from(StepRun))).scalar_one()


async def _wstaw_krok(tenant_id: str, run_id: str, step_id: str, status: StepStatus, *,
                      agent_id: str = "", attempt: int = 1, reason: str | None = None) -> None:
    async with db_session.SessionLocal() as s:
        s.add(StepRun(tenant_id=tenant_id, run_id=run_id, step_id=step_id, agent_id=agent_id,
                      attempt=attempt, status=status, reason=reason))
        await s.commit()


# --- stala tabela mapowania ---

def test_mapa_krokow_przypisuje_kazdemu_zdarzeniu_kroku_jego_stan():
    from cockpit.services.kroki import MAPA_KROKOW
    assert MAPA_KROKOW == {
        "step_started": StepStatus.running,
        "step_finished": StepStatus.finished,
        "step_failed": StepStatus.failed,
        "step_aborted": StepStatus.failed,
        "step_skipped": StepStatus.skipped,
        "step_waiting": StepStatus.waiting,
    }


def test_stany_kroku_to_dokladnie_pieciu_znanych_stanow():
    assert {m.value for m in StepStatus} == {"running", "finished", "failed", "skipped", "waiting"}


# --- osobny wiersz per krok, agent i proba ---

async def test_dwa_kroki_tego_samego_runu_maja_osobne_stany(swiat):
    await _ok(swiat, "step_started", step_id="build")
    await _ok(swiat, "step_finished", step_id="lint")
    assert await _kroki(swiat.run_id) == {
        ("build", "", 1): (StepStatus.running, None),
        ("lint", "", 1): (StepStatus.finished, None),
    }


async def test_dwaj_agenci_tego_samego_kroku_maja_osobne_stany(swiat):
    await _ok(swiat, "step_failed", agent_id="a1", text="boom")
    await _ok(swiat, "step_finished", agent_id="a2")
    assert await _kroki(swiat.run_id) == {
        ("s0", "a1", 1): (StepStatus.failed, "boom"),
        ("s0", "a2", 1): (StepStatus.finished, None),
    }


async def test_dwie_proby_tego_samego_kroku_maja_osobne_stany(swiat):
    await _ok(swiat, "step_failed", attempt=1, text="boom")
    await _ok(swiat, "step_started", attempt=2)
    assert await _kroki(swiat.run_id) == {
        ("s0", "", 1): (StepStatus.failed, "boom"),
        ("s0", "", 2): (StepStatus.running, None),
    }


async def test_ten_sam_krok_agent_i_proba_to_jeden_wiersz_ze_stanem_ostatniego_zdarzenia(swiat):
    await _ok(swiat, "step_started", agent_id="a1", attempt=2)
    await _ok(swiat, "step_finished", agent_id="a1", attempt=2)
    assert await _kroki(swiat.run_id) == {("s0", "a1", 2): (StepStatus.finished, None)}


async def test_krok_ma_osobne_stany_w_roznych_runach_tego_samego_zespolu(swiat):
    await _ok(swiat, "step_failed", text="boom")
    await _ok(swiat, "step_finished", run_id=swiat.drugi_run_id)
    assert await _kroki(swiat.run_id) == {("s0", "", 1): (StepStatus.failed, "boom")}
    assert await _kroki(swiat.drugi_run_id) == {("s0", "", 1): (StepStatus.finished, None)}


async def test_zdarzenie_bez_numeru_proby_dotyczy_pierwszej_proby(swiat):
    await _ok(swiat, "step_started")
    assert list(await _kroki(swiat.run_id)) == [("s0", "", 1)]


async def test_zdarzenie_bez_numeru_proby_i_z_proba_pierwsza_to_ten_sam_krok(swiat):
    await _ok(swiat, "step_started")
    await _ok(swiat, "step_finished", attempt=1)
    assert await _kroki(swiat.run_id) == {("s0", "", 1): (StepStatus.finished, None)}


async def test_zdarzenie_bez_numeru_proby_zapisuje_brak_proby_a_nie_pierwsza(swiat):
    await _ok(swiat, "output", text="a")
    await _ok(swiat, "output", text="b", attempt=1)
    await _ok(swiat, "output", text="c", attempt=3)
    async with db_session.SessionLocal() as s:
        zdarzenia = (await s.execute(
            select(Event).where(Event.run_id == swiat.run_id).order_by(Event.seq)
        )).scalars().all()
    assert [z.attempt for z in zdarzenia] == [None, 1, 3]


async def test_krok_zapisany_przez_model_bez_agenta_ma_agenta_pustego_napisem(swiat):
    async with db_session.SessionLocal() as s:
        s.add(StepRun(tenant_id=swiat.tenant_id, run_id=swiat.run_id, step_id="s0",
                      status=StepStatus.running))
        await s.commit()
    assert [w[1] for w in await _kroki(swiat.run_id)] == [""]


async def test_krok_zapisany_przez_model_bez_proby_ma_probe_pierwsza(swiat):
    async with db_session.SessionLocal() as s:
        s.add(StepRun(tenant_id=swiat.tenant_id, run_id=swiat.run_id, step_id="s0",
                      status=StepStatus.running))
        await s.commit()
    assert [w[2] for w in await _kroki(swiat.run_id)] == [1]


async def test_zdarzenie_zapisane_przez_model_bez_proby_ma_probe_nieznana_a_nie_pierwsza(swiat):
    async with db_session.SessionLocal() as s:
        s.add(Event(tenant_id=swiat.tenant_id, run_id=swiat.run_id, seq=1, kind="output"))
        await s.commit()
        assert (await s.execute(select(Event.attempt))).scalar_one() is None


# --- mapowanie zdarzenie -> stan kroku ---

@pytest.mark.parametrize("kind,oczekiwany", [
    ("step_started", StepStatus.running),
    ("step_finished", StepStatus.finished),
    ("step_failed", StepStatus.failed),
    ("step_aborted", StepStatus.failed),
    ("step_skipped", StepStatus.skipped),
    ("step_waiting", StepStatus.waiting),
])
async def test_zdarzenie_kroku_ustawia_jego_stan(swiat, kind, oczekiwany):
    await _ok(swiat, kind, text="tresc")
    assert (await _kroki(swiat.run_id))[("s0", "", 1)][0] == oczekiwany


@pytest.mark.parametrize("kind", ["step_failed", "step_aborted"])
async def test_porazka_i_przerwanie_kroku_niosa_powod_z_tekstu_zdarzenia(swiat, kind):
    await _ok(swiat, kind, text="kod wyjscia 3")
    assert (await _kroki(swiat.run_id))[("s0", "", 1)] == (StepStatus.failed, "kod wyjscia 3")


@pytest.mark.parametrize("kind", ["step_started", "step_finished", "step_skipped", "step_waiting"])
async def test_krok_bez_porazki_nie_ma_powodu_mimo_tekstu_zdarzenia(swiat, kind):
    await _ok(swiat, kind, text="tresc, ktora nie jest powodem")
    assert (await _kroki(swiat.run_id))[("s0", "", 1)][1] is None


@pytest.mark.parametrize("kind", ["step_started", "step_finished", "step_skipped", "step_waiting"])
async def test_ponowienie_nieudanej_proby_czysci_powod_porazki(swiat, kind):
    await _ok(swiat, "step_failed", text="boom")
    await _ok(swiat, kind)
    status, powod = (await _kroki(swiat.run_id))[("s0", "", 1)]
    assert status == {"step_started": StepStatus.running, "step_finished": StepStatus.finished,
                      "step_skipped": StepStatus.skipped, "step_waiting": StepStatus.waiting}[kind]
    assert powod is None


async def test_druga_porazka_tej_samej_proby_podmienia_powod_zamiast_dodawac_wiersz(swiat):
    await _ok(swiat, "step_failed", text="pierwszy")
    await _ok(swiat, "step_failed", text="drugi")
    assert await _kroki(swiat.run_id) == {("s0", "", 1): (StepStatus.failed, "drugi")}


async def test_powod_porazki_kroku_to_ten_sam_obcienty_tekst_co_w_zdarzeniu(swiat, monkeypatch):
    monkeypatch.setattr(settings, "limit_tekstu_zdarzenia", 4, raising=False)
    await _ok(swiat, "step_failed", text="abcdefghij")
    async with db_session.SessionLocal() as s:
        tekst_zdarzenia = (await s.execute(select(Event.text))).scalar_one()
    assert tekst_zdarzenia == "abcd"
    assert (await _kroki(swiat.run_id))[("s0", "", 1)][1] == "abcd"


@pytest.mark.parametrize("kind", ["output", "run_finished", "cost"])
async def test_zdarzenie_niebedace_zdarzeniem_kroku_nie_tworzy_wiersza_kroku(swiat, kind):
    await _ok(swiat, kind, text="tresc")
    assert await _liczba_wierszy_krokow() == 0


async def test_zdarzenie_niebedace_zdarzeniem_kroku_nie_zmienia_istniejacego_kroku(swiat):
    await _ok(swiat, "step_failed", text="boom")
    await _ok(swiat, "output", text="linia")
    await _ok(swiat, "run_finished")
    assert await _kroki(swiat.run_id) == {("s0", "", 1): (StepStatus.failed, "boom")}


# --- status runu wynika ze stanow krokow i z run_finished ---

async def test_sam_koniec_kroku_nie_daje_runu_do_odbioru(swiat):
    await _ok(swiat, "step_finished")
    assert (await _status_i_powod(swiat.run_id))[0] == RunStatus.running


async def test_koniec_runu_bez_nieudanych_krokow_daje_run_do_odbioru(swiat):
    await _ok(swiat, "step_finished")
    await _ok(swiat, "run_finished")
    assert (await _status_i_powod(swiat.run_id))[0] == RunStatus.to_accept


@pytest.mark.parametrize("kolejnosc", [("a", "b"), ("b", "a")])
async def test_porazka_jednej_galezi_nie_jest_przykryta_sukcesem_drugiej(swiat, kolejnosc):
    for krok in kolejnosc:
        if krok == "a":
            await _ok(swiat, "step_failed", step_id="a", text="boom")
        else:
            await _ok(swiat, "step_finished", step_id="b")
    await _ok(swiat, "run_finished", step_id="b")
    assert await _status_i_powod(swiat.run_id) == (RunStatus.failed, "nieudane kroki: a")


async def test_powod_runu_wymienia_nieudane_kroki_posortowane(swiat):
    await _ok(swiat, "step_failed", step_id="b", text="x")
    await _ok(swiat, "step_failed", step_id="a", text="y")
    await _ok(swiat, "step_finished", step_id="c")
    await _ok(swiat, "run_finished", step_id="c")
    assert await _status_i_powod(swiat.run_id) == (RunStatus.failed, "nieudane kroki: a, b")


async def test_krok_nieudany_u_dwoch_agentow_jest_w_powodzie_runu_raz(swiat):
    await _ok(swiat, "step_failed", step_id="a", agent_id="a1", text="x")
    await _ok(swiat, "step_failed", step_id="a", agent_id="a2", text="y")
    await _ok(swiat, "run_finished", step_id="a")
    assert await _status_i_powod(swiat.run_id) == (RunStatus.failed, "nieudane kroki: a")


async def test_krok_pominiety_nie_czyni_runu_nieudanym(swiat):
    await _ok(swiat, "step_finished", step_id="a")
    await _ok(swiat, "step_skipped", step_id="b")
    await _ok(swiat, "run_finished", step_id="b")
    assert (await _status_i_powod(swiat.run_id))[0] == RunStatus.to_accept


async def test_run_z_samymi_krokami_pominietymi_nie_jest_nieudany(swiat):
    await _ok(swiat, "step_skipped", step_id="a")
    await _ok(swiat, "run_finished", step_id="a")
    assert (await _status_i_powod(swiat.run_id))[0] == RunStatus.to_accept


async def test_porazka_wczesniejszej_proby_przykryta_udana_pozniejsza_nie_psuje_runu(swiat):
    await _ok(swiat, "step_failed", attempt=1, text="boom")
    await _ok(swiat, "step_finished", attempt=2)
    await _ok(swiat, "run_finished", attempt=2)
    assert (await _status_i_powod(swiat.run_id))[0] == RunStatus.to_accept


async def test_porazka_pozniejszej_proby_po_sukcesie_wczesniejszej_psuje_run(swiat):
    await _ok(swiat, "step_finished", attempt=1)
    await _ok(swiat, "step_failed", attempt=2, text="boom")
    await _ok(swiat, "run_finished", attempt=2)
    assert await _status_i_powod(swiat.run_id) == (RunStatus.failed, "nieudane kroki: s0")


async def test_ostatnia_proba_to_najwiekszy_numer_a_nie_ostatnio_zapisana_udana(swiat):
    await _ok(swiat, "step_finished", attempt=2)
    await _ok(swiat, "step_failed", attempt=1, text="spozniona porazka")  # dociera pozniej
    await _ok(swiat, "run_finished", attempt=2)
    assert (await _status_i_powod(swiat.run_id))[0] == RunStatus.to_accept


async def test_ostatnia_proba_to_najwiekszy_numer_a_nie_ostatnio_zapisana_nieudana(swiat):
    await _ok(swiat, "step_failed", attempt=2, text="boom")
    await _ok(swiat, "step_finished", attempt=1)  # dociera pozniej
    await _ok(swiat, "run_finished", attempt=2)
    assert await _status_i_powod(swiat.run_id) == (RunStatus.failed, "nieudane kroki: s0")


async def test_porazka_jednego_agenta_liczy_sie_mimo_sukcesu_innego_agenta_tego_kroku(swiat):
    await _ok(swiat, "step_failed", agent_id="a1", attempt=1, text="boom")
    await _ok(swiat, "step_finished", agent_id="a2", attempt=1)
    await _ok(swiat, "run_finished", agent_id="a2", attempt=1)
    assert await _status_i_powod(swiat.run_id) == (RunStatus.failed, "nieudane kroki: s0")


async def test_wyzsza_proba_jednego_agenta_nie_przykrywa_porazki_innego_agenta(swiat):
    await _ok(swiat, "step_failed", agent_id="a1", attempt=1, text="boom")
    await _ok(swiat, "step_finished", agent_id="a2", attempt=2)
    await _ok(swiat, "run_finished", agent_id="a2", attempt=2)
    assert await _status_i_powod(swiat.run_id) == (RunStatus.failed, "nieudane kroki: s0")


async def test_krok_czekajacy_na_decyzje_daje_run_czekajacy(swiat):
    await _ok(swiat, "step_waiting")
    assert (await _status_i_powod(swiat.run_id))[0] == RunStatus.waiting


async def test_start_kroku_po_decyzji_wznawia_run_czekajacy(swiat):
    await _ok(swiat, "step_waiting")
    await _ok(swiat, "step_started")
    assert (await _status_i_powod(swiat.run_id))[0] == RunStatus.running


async def test_start_kroku_w_biegnacym_runie_nie_zmienia_jego_statusu(swiat):
    await _ok(swiat, "step_started")
    assert (await _status_i_powod(swiat.run_id))[0] == RunStatus.running


async def test_koniec_runu_po_oczekiwaniu_na_decyzje_daje_run_do_odbioru(swiat):
    await _ok(swiat, "step_waiting")
    await _ok(swiat, "run_finished")
    assert (await _status_i_powod(swiat.run_id))[0] == RunStatus.to_accept


async def test_koniec_runu_nie_nadpisuje_runu_nieudanego(swiat):
    await _ok(swiat, "step_failed", step_id="a", text="boom")
    await _ok(swiat, "run_finished", step_id="a")
    assert await _status_i_powod(swiat.run_id) == (RunStatus.failed, "nieudane kroki: a")
    # krok zostaje naprawiony: gdyby koniec runu przeliczal status od nowa, run wrocilby do odbioru
    await _ok(swiat, "step_finished", step_id="a")
    await _ok(swiat, "run_finished", step_id="a")
    assert await _status_i_powod(swiat.run_id) == (RunStatus.failed, "nieudane kroki: a")


async def test_przerwanie_kroku_daje_run_przerwany_z_powodem_z_tekstu(swiat):
    await _ok(swiat, "step_aborted", text="brak miejsca")
    assert await _status_i_powod(swiat.run_id) == (RunStatus.aborted, "brak miejsca")


async def test_przerwanie_kroku_nie_rusza_runu_czekajacego_na_decyzje_ani_jego_powodu(swiat):
    await _ok(swiat, "step_waiting", step_id="a")
    await _ok(swiat, "step_aborted", step_id="b", text="brak miejsca")
    assert await _status_i_powod(swiat.run_id) == (RunStatus.waiting, None)
    # sam krok zostaje oznaczony jako nieudany z powodem
    assert (await _kroki(swiat.run_id))[("b", "", 1)] == (StepStatus.failed, "brak miejsca")


async def test_koniec_runu_nie_nadpisuje_runu_przerwanego_ani_jego_powodu(swiat):
    await _ok(swiat, "step_aborted", text="brak miejsca")
    # przerwany krok jest nieudany, wiec lista nieudanych nie jest pusta
    await _ok(swiat, "run_finished")
    assert await _status_i_powod(swiat.run_id) == (RunStatus.aborted, "brak miejsca")


async def test_przerwanie_runu_do_odbioru_zapisuje_powod_przerwania(swiat):
    await _ok(swiat, "step_finished", step_id="a")
    await _ok(swiat, "run_finished", step_id="a")
    await _ok(swiat, "step_aborted", step_id="b", text="runner offline")
    assert await _status_i_powod(swiat.run_id) == (RunStatus.aborted, "runner offline")


async def test_koniec_runu_do_odbioru_zostawia_powod_niepelnego_strumienia(swiat):
    await _ok(swiat, "step_finished", wyslane_output=2)  # zero zapisanych output
    status, powod = await _status_i_powod(swiat.run_id)
    assert status == RunStatus.running
    assert powod is not None and powod.startswith("niepelny strumien")
    await _ok(swiat, "run_finished")
    assert await _status_i_powod(swiat.run_id) == (RunStatus.to_accept, powod)


async def test_porazka_po_odbiorze_zamienia_powod_runu_na_liste_nieudanych_krokow(swiat):
    await _ok(swiat, "step_finished", step_id="a", wyslane_output=1)
    await _ok(swiat, "run_finished", step_id="a")
    assert (await _status_i_powod(swiat.run_id))[0] == RunStatus.to_accept
    await _ok(swiat, "step_failed", step_id="b", text="boom")
    await _ok(swiat, "run_finished", step_id="b")
    assert await _status_i_powod(swiat.run_id) == (RunStatus.failed, "nieudane kroki: b")


async def test_powtorzone_zdarzenie_nie_przywraca_stanu_kroku(swiat):
    event_id = str(uuid.uuid4())
    await _ok(swiat, "step_failed", text="boom", event_id=event_id)
    await _ok(swiat, "step_started")  # ta sama proba: wznowienie czyści porazke
    assert (await _kroki(swiat.run_id))[("s0", "", 1)] == (StepStatus.running, None)

    await _ok(swiat, "step_failed", text="boom", event_id=event_id)  # ponowienie z bufora runnera

    assert await _kroki(swiat.run_id) == {("s0", "", 1): (StepStatus.running, None)}


async def test_powtorzone_zakonczenie_runu_nie_przelicza_statusu(swiat):
    event_id = str(uuid.uuid4())
    await _ok(swiat, "step_failed", step_id="a", text="boom")
    await _ok(swiat, "step_finished", step_id="b")
    await _ok(swiat, "run_finished", step_id="b", event_id=event_id)
    assert (await _status_i_powod(swiat.run_id))[0] == RunStatus.failed
    async with db_session.SessionLocal() as s:  # run wraca do biegu (np. reczna korekta)
        run = await s.get(Run, swiat.run_id)
        run.status, run.reason = RunStatus.running, None
        await s.commit()

    await _ok(swiat, "run_finished", step_id="b", event_id=event_id)

    assert await _status_i_powod(swiat.run_id) == (RunStatus.running, None)


# --- izolacja zespolow (tenant_id) ---

async def test_nieudane_kroki_nie_obejmuja_wierszy_innego_zespolu(swiat):
    from cockpit.services.kroki import nieudane_kroki
    await _ok(swiat, "step_finished", step_id="a", attempt=1)
    # obcy wiersz z wyzsza proba tego samego kroku: bez filtra tenant_id przykrylby wlasny
    await _wstaw_krok(swiat.obcy_tenant_id, swiat.run_id, "a", StepStatus.failed, attempt=2,
                      reason="obce")
    await _wstaw_krok(swiat.obcy_tenant_id, swiat.run_id, "b", StepStatus.failed, reason="obce")
    async with db_session.SessionLocal() as s:
        run = await s.get(Run, swiat.run_id)
        assert await nieudane_kroki(s, run) == []


async def test_nieudane_kroki_zwracaja_posortowane_unikalne_identyfikatory_wlasnego_zespolu(swiat):
    from cockpit.services.kroki import nieudane_kroki
    await _wstaw_krok(swiat.tenant_id, swiat.run_id, "b", StepStatus.failed)
    await _wstaw_krok(swiat.tenant_id, swiat.run_id, "a", StepStatus.failed, agent_id="x")
    await _wstaw_krok(swiat.tenant_id, swiat.run_id, "a", StepStatus.failed, agent_id="y")
    await _wstaw_krok(swiat.tenant_id, swiat.run_id, "c", StepStatus.finished)
    await _wstaw_krok(swiat.tenant_id, swiat.drugi_run_id, "z", StepStatus.failed)  # inny run
    async with db_session.SessionLocal() as s:
        run = await s.get(Run, swiat.run_id)
        assert await nieudane_kroki(s, run) == ["a", "b"]


async def test_nieudane_kroki_nie_obejmuja_krokow_innego_runu_tego_samego_zespolu(swiat):
    from cockpit.services.kroki import nieudane_kroki
    await _wstaw_krok(swiat.tenant_id, swiat.drugi_run_id, "z", StepStatus.failed)
    async with db_session.SessionLocal() as s:
        run = await s.get(Run, swiat.run_id)
        assert await nieudane_kroki(s, run) == []


async def test_kroki_runu_nie_obejmuja_wierszy_innego_zespolu_ani_innego_runu(swiat):
    from cockpit.services.kroki import kroki_runu
    await _wstaw_krok(swiat.tenant_id, swiat.run_id, "wlasny", StepStatus.finished)
    await _wstaw_krok(swiat.obcy_tenant_id, swiat.run_id, "obcy", StepStatus.failed,
                      reason="obce")
    await _wstaw_krok(swiat.tenant_id, swiat.drugi_run_id, "inny-run", StepStatus.finished)
    async with db_session.SessionLocal() as s:
        run = await s.get(Run, swiat.run_id)
        wiersze = await kroki_runu(s, run)
    assert [w.step_id for w in wiersze] == ["wlasny"]


async def test_kroki_runu_sa_posortowane_wedlug_kroku_agenta_i_proby(swiat):
    from cockpit.services.kroki import kroki_runu
    for step, agent, proba in [("b", "", 1), ("a", "z", 2), ("a", "", 1), ("a", "z", 1)]:
        await _wstaw_krok(swiat.tenant_id, swiat.run_id, step, StepStatus.finished,
                          agent_id=agent, attempt=proba)
    async with db_session.SessionLocal() as s:
        run = await s.get(Run, swiat.run_id)
        wiersze = await kroki_runu(s, run)
    assert [(w.step_id, w.agent_id, w.attempt) for w in wiersze] == [
        ("a", "", 1), ("a", "z", 1), ("a", "z", 2), ("b", "", 1)]


async def test_kroki_runu_sa_zawsze_porzadkowane_wedlug_kroku_agenta_i_proby_takze_bez_indeksu():
    """Na SQLite wiersze wracaja po indeksie unikalnym i tak posortowane; na PostgreSQL
    kolejnosc bez ORDER BY nie jest gwarantowana. Dlatego sprawdzamy samo zapytanie
    skompilowane dialektem PostgreSQL, nie kolejnosc wynikow."""
    from types import SimpleNamespace

    from sqlalchemy.dialects import postgresql

    from cockpit.services.kroki import kroki_runu

    class Wynik:
        def scalars(self):
            return self

        def all(self):
            return []

    class SesjaPrzechwytujaca:
        def __init__(self):
            self.zapytania = []

        async def execute(self, zapytanie):
            self.zapytania.append(zapytanie)
            return Wynik()

    sesja = SesjaPrzechwytujaca()
    await kroki_runu(sesja, SimpleNamespace(id="r1", tenant_id="t1"))

    assert len(sesja.zapytania) == 1
    sql = str(sesja.zapytania[0].compile(dialect=postgresql.dialect()))
    assert sql.split("ORDER BY ")[1].strip() == (
        "step_run.step_id, step_run.agent_id, step_run.attempt")


async def test_zdarzenie_kroku_nie_zmienia_wiersza_innego_zespolu_o_tym_samym_kluczu(swiat):
    """Wiersz obcego zespolu z tym samym kluczem nie jest nadpisany. Unikalnosc
    (run_id, step_id, agent_id, attempt) moze zablokowac wstawienie wlasnego wiersza,
    wiec test dopuszcza IntegrityError - ale nigdy zmiane cudzego wiersza."""
    from cockpit.services.kroki import zastosuj_zdarzenie_kroku
    await _wstaw_krok(swiat.obcy_tenant_id, swiat.run_id, "s0", StepStatus.failed, reason="obce")
    async with db_session.SessionLocal() as s:
        run = await s.get(Run, swiat.run_id)
        try:
            await zastosuj_zdarzenie_kroku(s, run, "step_finished", "s0", "", 1, "")
            await s.commit()
        except IntegrityError:
            await s.rollback()
    async with db_session.SessionLocal() as s:
        obcy = (await s.execute(select(StepRun).where(StepRun.tenant_id == swiat.obcy_tenant_id)
                                )).scalars().one()
    assert (obcy.status, obcy.reason) == (StepStatus.failed, "obce")


async def test_zdarzenie_kroku_wlasnego_zespolu_zapisuje_wiersz_z_jego_tenant_id(swiat):
    await _ok(swiat, "step_started")
    async with db_session.SessionLocal() as s:
        wiersz = (await s.execute(select(StepRun))).scalars().one()
    assert (wiersz.tenant_id, wiersz.run_id) == (swiat.tenant_id, swiat.run_id)


async def test_zastosowanie_zdarzenia_kroku_aktualizuje_wiersz_wlasnego_zespolu(swiat):
    from cockpit.services.kroki import zastosuj_zdarzenie_kroku
    await _wstaw_krok(swiat.tenant_id, swiat.run_id, "s0", StepStatus.running)
    async with db_session.SessionLocal() as s:
        run = await s.get(Run, swiat.run_id)
        await zastosuj_zdarzenie_kroku(s, run, "step_failed", "s0", "", None, "boom")
        await s.commit()
    assert await _kroki(swiat.run_id) == {("s0", "", 1): (StepStatus.failed, "boom")}


async def test_zmiana_stanu_kroku_odswieza_czas_ostatniej_zmiany_wiersza(swiat):
    await _ok(swiat, "step_started")
    async with db_session.SessionLocal() as s:
        przed = (await s.execute(select(StepRun.updated_at))).scalar_one()
    await _ok(swiat, "step_finished")
    async with db_session.SessionLocal() as s:
        po = (await s.execute(select(StepRun.updated_at))).scalar_one()
    assert po > przed


def test_wiersz_kroku_ma_indeksy_po_zespole_i_po_runie():
    zindeksowane = {c.name for i in StepRun.__table__.indexes for c in i.columns}
    assert {"tenant_id", "run_id"} <= zindeksowane


async def test_zastosowanie_zdarzenia_niebedacego_zdarzeniem_kroku_nic_nie_zmienia(swiat):
    from cockpit.services.kroki import zastosuj_zdarzenie_kroku
    async with db_session.SessionLocal() as s:
        run = await s.get(Run, swiat.run_id)
        for kind in ("output", "run_finished", "nieznany"):
            await zastosuj_zdarzenie_kroku(s, run, kind, "s0", "", 1, "x")
        await s.commit()
    assert await _liczba_wierszy_krokow() == 0


# --- GET /api/runs/{id} ---

async def test_szczegoly_runu_zwracaja_kroki_ze_stanem_i_powodem_w_stalym_porzadku(swiat):
    await _ok(swiat, "step_finished", step_id="b")
    await _ok(swiat, "step_failed", step_id="a", agent_id="z", attempt=2, text="boom")
    await _ok(swiat, "step_started", step_id="a")
    await _ok(swiat, "step_failed", step_id="a", agent_id="z", attempt=1, text="stara")
    r = await swiat.klient.get(f"/api/runs/{swiat.run_id}")
    assert r.status_code == 200
    brak_pomiaru = {"cost_usd": None, "tokens_in": None, "tokens_out": None,
                    "num_turns": None, "session_id": None}      # HAR-174: null, nigdy 0
    assert r.json()["steps"] == [
        {"step_id": "a", "agent_id": "", "attempt": 1, "status": "running", "reason": None,
         **brak_pomiaru},
        {"step_id": "a", "agent_id": "z", "attempt": 1, "status": "failed", "reason": "stara",
         **brak_pomiaru},
        {"step_id": "a", "agent_id": "z", "attempt": 2, "status": "failed", "reason": "boom",
         **brak_pomiaru},
        {"step_id": "b", "agent_id": "", "attempt": 1, "status": "finished", "reason": None,
         **brak_pomiaru},
    ]


@pytest.mark.parametrize("stan", list(StepStatus))
async def test_szczegoly_runu_pokazuja_krok_w_kazdym_ze_znanych_stanow(swiat, stan):
    await _wstaw_krok(swiat.tenant_id, swiat.run_id, "s0", stan)
    r = await swiat.klient.get(f"/api/runs/{swiat.run_id}")
    assert r.status_code == 200
    assert [k["status"] for k in r.json()["steps"]] == [stan.value]


async def test_szczegoly_runu_bez_krokow_maja_pusta_liste_krokow(swiat):
    r = await swiat.klient.get(f"/api/runs/{swiat.run_id}")
    assert r.status_code == 200
    assert r.json()["steps"] == []


async def test_szczegoly_runu_nie_pokazuja_krokow_innego_zespolu_ani_innego_runu(swiat):
    await _ok(swiat, "step_finished", step_id="wlasny")
    await _wstaw_krok(swiat.obcy_tenant_id, swiat.run_id, "obcy", StepStatus.failed,
                      reason="obce")
    await _ok(swiat, "step_started", step_id="inny-run", run_id=swiat.drugi_run_id)
    r = await swiat.klient.get(f"/api/runs/{swiat.run_id}")
    assert [k["step_id"] for k in r.json()["steps"]] == ["wlasny"]


async def test_szczegoly_runu_innego_zespolu_to_404_i_nie_zdradzaja_krokow(swiat):
    await _wstaw_krok(swiat.obcy_tenant_id, swiat.run_obcego_id, "obcy", StepStatus.failed,
                      reason="obce")
    r = await swiat.klient.get(f"/api/runs/{swiat.run_obcego_id}")
    assert r.status_code == 404
    assert "steps" not in r.json()


async def test_szczegoly_runu_pokazuja_status_wyliczony_ze_stanow_krokow(swiat):
    await _ok(swiat, "step_failed", step_id="a", text="boom")
    await _ok(swiat, "step_finished", step_id="b")
    await _ok(swiat, "run_finished", step_id="b")
    r = await swiat.klient.get(f"/api/runs/{swiat.run_id}")
    assert r.json()["status"] == "failed"
    assert r.json()["reason"] == "nieudane kroki: a"


# --- numer proby na wejsciu ---

@pytest.mark.parametrize("proba", [0, -1, INT_MAX + 1])
async def test_numer_proby_poza_zakresem_jest_odrzucony_i_nic_nie_zapisuje(swiat, proba):
    r = await _zdarzenie(swiat, "step_started", attempt=proba)
    assert r.status_code == 422
    assert await _liczba_wierszy_krokow() == 0
    async with db_session.SessionLocal() as s:
        zapisane = (await s.execute(select(func.count()).select_from(Event))).scalar_one()
    assert zapisane == 0


async def test_numer_proby_bedacy_napisem_jest_odrzucony(swiat):
    r = await swiat.klient.post(
        "/api/events",
        json={"run_id": swiat.run_id, "kind": "step_started", "step_id": "s0",
              "event_id": str(uuid.uuid4()), "attempt": "druga"},
        headers=NAGLOWEK)
    assert r.status_code == 422
    assert await _liczba_wierszy_krokow() == 0


async def test_numer_proby_rowny_najwiekszej_liczbie_calkowitej_bazy_jest_przyjety(swiat):
    r = await _zdarzenie(swiat, "step_started", attempt=INT_MAX)
    assert r.status_code == 200
    assert list(await _kroki(swiat.run_id)) == [("s0", "", INT_MAX)]


async def test_numer_proby_pierwszy_jest_przyjety(swiat):
    r = await _zdarzenie(swiat, "step_started", attempt=1)
    assert r.status_code == 200
    assert list(await _kroki(swiat.run_id)) == [("s0", "", 1)]
