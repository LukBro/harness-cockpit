"""HAR-178: pokrycie bramkami w rekordzie runu i oznaczenie runu, ktorego nic nie sprawdzilo.

Runner dosyla w `run_finished` liste krokow weryfikujacych (`kroki_weryfikujace`); kokpit
wylicza z niej i ze swoich wierszy krokow pokrycie (HAR-64) i zapisuje je w kolumnach runu
w tej samej transakcji co status. Pokrycie nie zmienia statusu: run, w ktorym pominieto
wszystkie bramki, jest dalej "do odbioru". Brak pomiaru to NULL i `pokrycie: null`, nigdy zero.

Skutek czytamy z bazy (kolumny `run`), nie tylko z odpowiedzi HTTP.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import (
    Base, Event, Run, Runner, RunStatus, StepRun, StepStatus, Tenant,
)

from sesja_testowa import zaloguj_klienta, zaloz_wlasciciela

TOKEN = "token-browarka"
NAGLOWEK = {"Authorization": f"Bearer {TOKEN}"}
BRAK_CAPABILITY_LINT = "brak_capability: brak capability lint"
BRAK_CAPABILITY_TEST = "brak_capability: brak capability test_full"
BRAK_POMIARU = (None, None, None, None, None)


@dataclass
class Swiat:
    klient: AsyncClient      # zalogowany wlasciciel zespolu "Browarek"
    run_id: str              # run Browarka
    drugi_run_id: str        # drugi run Browarka (inny run tego samego zespolu)
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
                     step_id: str = "s0", text: str = "", event_id: str | None = None,
                     **reszta):
    cialo = {"run_id": run_id or swiat.run_id, "kind": kind, "step_id": step_id,
             "event_id": event_id or str(uuid.uuid4()), "text": text, **reszta}
    return await swiat.klient.post("/api/events", json=cialo, headers=NAGLOWEK)


async def _ok(swiat: Swiat, kind: str, **kw) -> None:
    r = await _zdarzenie(swiat, kind, **kw)
    assert r.status_code == 200, r.text


async def _wykonany(swiat: Swiat, krok: str) -> None:
    await _ok(swiat, "step_started", step_id=krok)
    await _ok(swiat, "step_finished", step_id=krok)


async def _nieudany(swiat: Swiat, krok: str) -> None:
    await _ok(swiat, "step_started", step_id=krok)
    await _ok(swiat, "step_failed", step_id=krok, text="kod wyjscia 1")


async def _pominiety(swiat: Swiat, krok: str, powod: str = BRAK_CAPABILITY_LINT) -> None:
    await _ok(swiat, "step_skipped", step_id=krok, text=powod)


async def _koniec(swiat: Swiat, kroki_weryfikujace, **kw) -> None:
    await _ok(swiat, "run_finished", kroki_weryfikujace=kroki_weryfikujace, **kw)


KOLUMNY = ("bramki_zaplanowane", "bramki_wykonane", "bramki_pominiete", "stan_pokrycia",
           "bramki_powody")


async def _kolumny(run_id: str) -> tuple:
    """Piec kolumn pokrycia runu odczytanych z bazy jednym zapytaniem."""
    async with db_session.SessionLocal() as s:
        run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
    return tuple(getattr(run, k) for k in KOLUMNY)


async def _status(run_id: str) -> RunStatus:
    async with db_session.SessionLocal() as s:
        return (await s.execute(select(Run.status).where(Run.id == run_id))).scalar_one()


async def _pokrycie_w_get(swiat: Swiat, run_id: str | None = None):
    r = await swiat.klient.get(f"/api/runs/{run_id or swiat.run_id}")
    assert r.status_code == 200, r.text
    assert "pokrycie" in r.json()    # pole wymagane, puste to null
    return r.json()["pokrycie"]


async def _liczba(model) -> int:
    async with db_session.SessionLocal() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


# --- wynik pokrycia ---

async def test_run_z_wykonanymi_bramkami_ma_pokrycie_sprawdzone_w_kolumnach_runu(swiat):
    await _wykonany(swiat, "lint")
    await _wykonany(swiat, "test")
    await _koniec(swiat, ["lint", "test"])

    assert await _kolumny(swiat.run_id) == (2, 2, 0, "sprawdzony", {})


async def test_run_z_wykonanymi_bramkami_zwraca_pokrycie_sprawdzone_w_odpowiedzi_api(swiat):
    await _wykonany(swiat, "lint")
    await _wykonany(swiat, "test")
    await _koniec(swiat, ["lint", "test"])

    assert await _pokrycie_w_get(swiat) == {
        "zaplanowane": 2, "wykonane": 2, "pominiete": 0, "powody": {}, "stan": "sprawdzony"}


async def test_run_z_jedna_bramka_wykonana_i_jedna_pominieta_ma_pokrycie_czesciowe_z_powodem(swiat):
    await _wykonany(swiat, "lint")
    await _pominiety(swiat, "test", BRAK_CAPABILITY_TEST)
    await _koniec(swiat, ["lint", "test"])

    assert await _kolumny(swiat.run_id) == (
        2, 1, 1, "czesciowo", {"test": "brak_capability"})
    assert await _pokrycie_w_get(swiat) == {
        "zaplanowane": 2, "wykonane": 1, "pominiete": 1,
        "powody": {"test": "brak_capability"}, "stan": "czesciowo"}


async def test_run_w_ktorym_pominieto_wszystkie_bramki_ma_pokrycie_niesprawdzone(swiat):
    await _pominiety(swiat, "lint", BRAK_CAPABILITY_LINT)
    await _pominiety(swiat, "test", BRAK_CAPABILITY_TEST)
    await _koniec(swiat, ["lint", "test"])

    assert await _kolumny(swiat.run_id) == (
        2, 0, 2, "niesprawdzony", {"lint": "brak_capability", "test": "brak_capability"})
    assert await _pokrycie_w_get(swiat) == {
        "zaplanowane": 2, "wykonane": 0, "pominiete": 2,
        "powody": {"lint": "brak_capability", "test": "brak_capability"},
        "stan": "niesprawdzony"}


async def test_run_niesprawdzony_jest_dalej_do_odbioru_bo_pominiecie_nie_zmienia_statusu(swiat):
    await _pominiety(swiat, "lint")
    await _koniec(swiat, ["lint"])

    assert await _status(swiat.run_id) == RunStatus.to_accept
    r = await swiat.klient.get(f"/api/runs/{swiat.run_id}")
    assert r.json()["status"] == "to_accept"


async def test_pominiecie_bramki_z_innego_powodu_niz_brak_capability_trafia_do_powodow(swiat):
    await _pominiety(swiat, "lint", "brak_wejscia: brak wejścia: raport z kroku a")
    await _koniec(swiat, ["lint"])

    assert await _kolumny(swiat.run_id) == (
        1, 0, 1, "niesprawdzony", {"lint": "brak_wejscia"})


async def test_krok_weryfikujacy_nieudany_liczy_sie_jako_wykonany_a_run_jest_nieudany(swiat):
    await _nieudany(swiat, "lint")
    await _wykonany(swiat, "test")
    await _koniec(swiat, ["lint", "test"])

    assert await _status(swiat.run_id) == RunStatus.failed
    assert await _kolumny(swiat.run_id) == (2, 2, 0, "sprawdzony", {})


async def test_bramka_pominieta_przez_nieudana_zaleznosc_nie_jest_nazwanym_pominieciem(swiat):
    await _nieudany(swiat, "build")
    await _pominiety(swiat, "lint", "zaleznosc_nieudana: zależność nieudana: build")
    await _koniec(swiat, ["build", "lint"])

    assert await _status(swiat.run_id) == RunStatus.failed
    assert await _kolumny(swiat.run_id) == (2, 1, 0, "czesciowo", {})


async def test_powod_pominiecia_pochodzi_z_ostatniego_zdarzenia_pominiecia_tego_kroku(swiat):
    await _pominiety(swiat, "lint", "brak_wejscia: brak wejścia: raport z kroku a")
    await _pominiety(swiat, "lint", BRAK_CAPABILITY_LINT)
    await _koniec(swiat, ["lint"])

    assert (await _kolumny(swiat.run_id))[4] == {"lint": "brak_capability"}


async def test_krok_wymieniony_dwa_razy_w_liscie_liczy_sie_do_zaplanowanych_raz(swiat):
    await _wykonany(swiat, "lint")
    await _koniec(swiat, ["lint", "lint"])

    assert await _kolumny(swiat.run_id) == (1, 1, 0, "sprawdzony", {})


# --- brak pomiaru to NULL, nie zero ---

async def test_run_w_toku_nie_ma_pokrycia_a_wszystkie_kolumny_sa_puste_a_nie_zerami(swiat):
    await _wykonany(swiat, "lint")

    assert await _kolumny(swiat.run_id) == BRAK_POMIARU
    assert await _pokrycie_w_get(swiat) is None


async def test_run_zakonczony_bez_listy_krokow_weryfikujacych_nie_ma_pokrycia(swiat):
    await _wykonany(swiat, "lint")
    await _ok(swiat, "run_finished")

    assert await _status(swiat.run_id) == RunStatus.to_accept
    assert await _kolumny(swiat.run_id) == BRAK_POMIARU
    assert await _pokrycie_w_get(swiat) is None


async def test_krok_z_listy_bez_wiersza_stanu_daje_brak_pokrycia_a_nie_zero_bramek(swiat):
    await _wykonany(swiat, "lint")
    await _koniec(swiat, ["lint", "test"])       # "test" nie zostawil zadnego sladu

    assert await _status(swiat.run_id) == RunStatus.to_accept
    assert await _kolumny(swiat.run_id) == BRAK_POMIARU
    assert await _pokrycie_w_get(swiat) is None


async def test_krok_z_listy_wciaz_w_toku_daje_brak_pokrycia(swiat):
    await _wykonany(swiat, "lint")
    await _ok(swiat, "step_started", step_id="test")
    await _koniec(swiat, ["lint", "test"])

    assert await _kolumny(swiat.run_id) == BRAK_POMIARU
    assert await _pokrycie_w_get(swiat) is None


async def test_kod_pominiecia_spoza_listy_daje_brak_pokrycia_a_zdarzenie_jest_przyjete(swiat):
    await _pominiety(swiat, "lint", "wymyslony_kod: cos")
    r = await _zdarzenie(swiat, "run_finished", kroki_weryfikujace=["lint"])

    assert r.status_code == 200
    assert await _status(swiat.run_id) == RunStatus.to_accept
    assert await _kolumny(swiat.run_id) == BRAK_POMIARU
    assert await _pokrycie_w_get(swiat) is None


async def test_pominiecie_bez_tekstu_daje_brak_pokrycia_a_zdarzenie_jest_przyjete(swiat):
    await _pominiety(swiat, "lint", "")
    r = await _zdarzenie(swiat, "run_finished", kroki_weryfikujace=["lint"])

    assert r.status_code == 200
    assert await _status(swiat.run_id) == RunStatus.to_accept
    assert await _kolumny(swiat.run_id) == BRAK_POMIARU


async def test_run_przerwany_nie_ma_pokrycia_nawet_gdy_runner_przyslal_liste(swiat):
    await _wykonany(swiat, "lint")
    await _ok(swiat, "step_aborted", step_id="test", text="brak miejsca")
    await _koniec(swiat, ["lint"])

    assert await _status(swiat.run_id) == RunStatus.aborted
    assert await _kolumny(swiat.run_id) == BRAK_POMIARU


# --- kontrakt pola kroki_weryfikujace ---

@pytest.mark.parametrize("kind", ["step_finished", "step_started", "output"])
async def test_lista_krokow_weryfikujacych_przy_zdarzeniu_innym_niz_koniec_runu_jest_odrzucona(
        swiat, kind):
    przed = await _liczba(Event)

    r = await _zdarzenie(swiat, kind, kroki_weryfikujace=["lint"])

    assert r.status_code == 422
    assert await _liczba(Event) == przed
    assert await _liczba(StepRun) == 0
    assert await _status(swiat.run_id) == RunStatus.running


async def test_pusta_lista_przy_zdarzeniu_innym_niz_koniec_runu_jest_przyjeta(swiat):
    r = await _zdarzenie(swiat, "step_finished", kroki_weryfikujace=[])

    assert r.status_code == 200
    assert await _liczba(Event) == 1


async def test_pusty_napis_w_liscie_krokow_weryfikujacych_jest_odrzucony(swiat):
    przed = await _liczba(Event)

    r = await _zdarzenie(swiat, "run_finished", kroki_weryfikujace=[""])

    assert r.status_code == 422
    assert await _liczba(Event) == przed
    assert await _status(swiat.run_id) == RunStatus.running


async def test_element_listy_krokow_weryfikujacych_dluzszy_niz_200_znakow_jest_odrzucony(swiat):
    przed = await _liczba(Event)

    r = await _zdarzenie(swiat, "run_finished", kroki_weryfikujace=["k" * 201])

    assert r.status_code == 422
    assert await _liczba(Event) == przed
    assert await _status(swiat.run_id) == RunStatus.running


async def test_element_listy_krokow_weryfikujacych_o_200_znakach_jest_przyjety(swiat):
    r = await _zdarzenie(swiat, "run_finished", kroki_weryfikujace=["k" * 200])

    assert r.status_code == 200
    assert await _status(swiat.run_id) == RunStatus.to_accept


async def test_lista_krokow_weryfikujacych_dluzsza_niz_1000_elementow_jest_odrzucona(swiat):
    przed = await _liczba(Event)

    r = await _zdarzenie(swiat, "run_finished",
                         kroki_weryfikujace=[f"k{i}" for i in range(1001)])

    assert r.status_code == 422
    assert await _liczba(Event) == przed
    assert await _status(swiat.run_id) == RunStatus.running


async def test_lista_krokow_weryfikujacych_o_1000_elementach_jest_przyjeta(swiat):
    r = await _zdarzenie(swiat, "run_finished",
                         kroki_weryfikujace=[f"k{i}" for i in range(1000)])

    assert r.status_code == 200
    assert await _status(swiat.run_id) == RunStatus.to_accept


async def test_pusta_lista_krokow_weryfikujacych_przy_koncu_runu_nie_daje_pokrycia(swiat):
    await _wykonany(swiat, "lint")
    await _koniec(swiat, [])

    assert await _status(swiat.run_id) == RunStatus.to_accept
    assert await _kolumny(swiat.run_id) == BRAK_POMIARU


# --- idempotencja, atomowosc, izolacja ---

async def test_ponowione_zdarzenie_konca_runu_nie_zmienia_zapisanego_pokrycia(swiat):
    await _wykonany(swiat, "lint")
    await _pominiety(swiat, "test", BRAK_CAPABILITY_TEST)
    id_konca = str(uuid.uuid4())
    await _koniec(swiat, ["lint", "test"], event_id=id_konca)
    zapisane = await _kolumny(swiat.run_id)
    assert zapisane[3] == "czesciowo"      # kontrast: pokrycie zostalo zapisane

    r = await _zdarzenie(swiat, "run_finished", event_id=id_konca,
                         kroki_weryfikujace=["test"])

    assert r.status_code == 200
    assert await _kolumny(swiat.run_id) == zapisane


async def test_po_zakonczeniu_runu_status_i_pokrycie_sa_zapisane_razem(swiat):
    await _pominiety(swiat, "lint")
    await _koniec(swiat, ["lint"])

    async with db_session.SessionLocal() as s:
        run = (await s.execute(select(Run).where(Run.id == swiat.run_id))).scalar_one()
    assert (run.status, run.stan_pokrycia) == (RunStatus.to_accept, "niesprawdzony")


async def test_pokrycie_nie_uwzglednia_stanow_krokow_innego_zespolu(swiat):
    # "test" w runie Browarka nie ma wiersza; wiersz o tym samym run_id, ale zespolu Piekarni,
    # nie moze go uzupelnic. Odrzuca go wylacznie filtr tenant_id.
    await _wykonany(swiat, "lint")
    async with db_session.SessionLocal() as s:
        s.add(StepRun(tenant_id=swiat.obcy_tenant_id, run_id=swiat.run_id, step_id="test",
                      agent_id="", attempt=1, status=StepStatus.finished))
        await s.commit()

    await _koniec(swiat, ["lint", "test"])

    assert await _kolumny(swiat.run_id) == BRAK_POMIARU


async def test_powod_pominiecia_nie_pochodzi_ze_zdarzenia_pominiecia_innego_zespolu(swiat):
    await _pominiety(swiat, "lint", BRAK_CAPABILITY_LINT)
    async with db_session.SessionLocal() as s:
        s.add(Event(tenant_id=swiat.obcy_tenant_id, run_id=swiat.run_id, seq=1000,
                    event_id=str(uuid.uuid4()), step_id="lint", kind="step_skipped",
                    text="pusty_foreach: obce"))
        await s.commit()

    await _koniec(swiat, ["lint"])

    assert (await _kolumny(swiat.run_id))[4] == {"lint": "brak_capability"}


async def test_powod_pominiecia_nie_pochodzi_ze_zdarzenia_innego_runu_tego_samego_zespolu(swiat):
    await _pominiety(swiat, "lint", BRAK_CAPABILITY_LINT)
    for _ in range(3):
        await _ok(swiat, "output", run_id=swiat.drugi_run_id, text="x")
    await _ok(swiat, "step_skipped", run_id=swiat.drugi_run_id, step_id="lint",
              text="brak_wejscia: brak wejścia: x")

    await _koniec(swiat, ["lint"])

    assert (await _kolumny(swiat.run_id))[4] == {"lint": "brak_capability"}


async def test_zakonczenie_runu_innego_zespolu_nie_zapisuje_pokrycia_w_zadnym_runie(swiat):
    await _pominiety(swiat, "lint")

    r = await _zdarzenie(swiat, "run_finished", run_id=swiat.run_obcego_id,
                         kroki_weryfikujace=["lint"])

    assert r.status_code == 404
    assert await _kolumny(swiat.run_id) == BRAK_POMIARU
    assert await _kolumny(swiat.run_obcego_id) == BRAK_POMIARU


async def test_pokrycie_cudzego_runu_nie_jest_dostepne_przez_api(swiat):
    r = await swiat.klient.get(f"/api/runs/{swiat.run_obcego_id}")

    assert r.status_code == 404


# --- dodatkowe wejscia rozstrzygajace (tura 3) ---

async def test_powody_pominiecia_dwoch_bramek_sa_osobne_a_pozniejsze_zdarzenie_innego_kroku_ich_nie_nadpisuje(swiat):
    await _pominiety(swiat, "lint", BRAK_CAPABILITY_LINT)
    await _pominiety(swiat, "test", "brak_wejscia: brak wejścia: raport z kroku a")
    await _koniec(swiat, ["lint", "test"])

    assert (await _kolumny(swiat.run_id))[4] == {
        "lint": "brak_capability", "test": "brak_wejscia"}


async def test_powod_pominiecia_nie_pochodzi_ze_zdarzenia_innego_rodzaju_tego_samego_kroku(swiat):
    await _pominiety(swiat, "lint", BRAK_CAPABILITY_LINT)
    await _ok(swiat, "output", step_id="lint", text="brak_wejscia: to tylko wyjscie kroku")
    await _koniec(swiat, ["lint"])

    assert (await _kolumny(swiat.run_id))[4] == {"lint": "brak_capability"}


async def test_samo_zdarzenie_pominiecia_bez_wiersza_stanu_kroku_daje_brak_pokrycia(swiat):
    async with db_session.SessionLocal() as s:
        s.add(Event(tenant_id=swiat.tenant_id, run_id=swiat.run_id, seq=1000,
                    event_id=str(uuid.uuid4()), step_id="lint", kind="step_skipped",
                    text=BRAK_CAPABILITY_LINT))
        await s.commit()
    assert await _liczba(StepRun) == 0

    await _koniec(swiat, ["lint"])

    assert await _status(swiat.run_id) == RunStatus.to_accept
    assert await _kolumny(swiat.run_id) == BRAK_POMIARU


async def test_lista_krokow_weryfikujacych_przy_zdarzeniu_innym_niz_koniec_runu_ma_komunikat_bledu(swiat):
    r = await _zdarzenie(swiat, "step_finished", kroki_weryfikujace=["lint"])

    assert r.status_code == 422
    assert r.json() == {
        "detail": "kroki_weryfikujace wolno przeslac tylko w zdarzeniu run_finished"}


async def test_jawny_null_w_polu_krokow_weryfikujacych_jest_brakiem_klasyfikacji(swiat):
    await _wykonany(swiat, "lint")
    r = await _zdarzenie(swiat, "run_finished", kroki_weryfikujace=None)

    assert r.status_code == 200
    assert await _status(swiat.run_id) == RunStatus.to_accept
    assert await _kolumny(swiat.run_id) == BRAK_POMIARU


async def test_krok_weryfikujacy_ktorego_jeden_agent_jest_w_toku_a_drugi_skonczyl_daje_brak_pokrycia(swiat):
    await _ok(swiat, "step_started", step_id="lint", agent_id="a")
    await _ok(swiat, "step_started", step_id="lint", agent_id="b")
    await _ok(swiat, "step_finished", step_id="lint", agent_id="b")
    await _koniec(swiat, ["lint"])

    assert await _status(swiat.run_id) == RunStatus.to_accept
    assert await _kolumny(swiat.run_id) == BRAK_POMIARU


async def test_krok_weryfikujacy_ktorego_jeden_agent_czeka_a_drugi_skonczyl_daje_brak_pokrycia(swiat):
    await _ok(swiat, "step_started", step_id="lint", agent_id="a")
    await _ok(swiat, "step_waiting", step_id="lint", agent_id="a")
    await _ok(swiat, "step_started", step_id="lint", agent_id="b")
    await _ok(swiat, "step_finished", step_id="lint", agent_id="b")
    await _koniec(swiat, ["lint"])

    assert await _status(swiat.run_id) == RunStatus.to_accept
    assert await _kolumny(swiat.run_id) == BRAK_POMIARU


async def test_krok_weryfikujacy_ktorego_jeden_agent_skonczyl_a_drugi_zostal_pominiety_jest_wykonany(swiat):
    await _ok(swiat, "step_started", step_id="lint", agent_id="a")
    await _ok(swiat, "step_finished", step_id="lint", agent_id="a")
    await _ok(swiat, "step_skipped", step_id="lint", agent_id="b", text=BRAK_CAPABILITY_LINT)
    await _koniec(swiat, ["lint"])

    assert await _kolumny(swiat.run_id) == (1, 1, 0, "sprawdzony", {})


async def test_run_przerwany_z_wierszami_wszystkich_bramek_nie_zapisuje_pokrycia(swiat):
    await _wykonany(swiat, "lint")
    await _ok(swiat, "step_aborted", step_id="test", text="brak miejsca")
    await _koniec(swiat, ["lint", "test"])

    assert await _status(swiat.run_id) == RunStatus.aborted
    assert await _kolumny(swiat.run_id) == BRAK_POMIARU
