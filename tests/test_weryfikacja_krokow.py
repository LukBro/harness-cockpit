"""HAR-178: runner wskazuje kroki weryfikujace i dosyla ich liste w koncowym `run_finished`.

Krok jest weryfikujacy (D27), gdy jego skill ma role `reviewer` albo potrzebuje capability-bramki
(test_narrow, test_full, lint, typecheck, build). Kokpit nie zna skilli, wiec wiedze ma tylko
runner. Brak pola w `run_finished` oznacza brak klasyfikacji (kokpit zapisze brak pomiaru).

Czesc silnikowa uzywa prawdziwego `runner.silnik` z zaslepionym agentem (jak
tests/test_silnik_runu.py); ostatni test przekazuje zdarzenia silnika do prawdziwego kokpitu.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Run, Runner, RunStatus
from cockpit.services.invariants import Rola
from cockpit.services.skills import Rejestr, Skill
from cockpit.services.workflow import Krok, Workflow

from sesja_testowa import zaloguj_klienta, zaloz_wlasciciela
from test_silnik_runu import RUN_ID, K, Szpieg, jedno, rodzaje, swiat, uruchom

TOKEN = "token-browarka"

# Lista przypadkow wpisana niezaleznie od stalej w kodzie runnera.
BRAMKI = ["test_narrow", "test_full", "lint", "typecheck", "build"]


def _skill(id: str, rola: Rola = Rola.author, needs: list[str] | None = None) -> Skill:
    return Skill(id=id, name=id, role=rola, needs=needs or [], brief="Brief.",
                 katalog=Path("/nie/istnieje"))


def _rejestr(*skille: Skill) -> Rejestr:
    return Rejestr(skille={s.id: s for s in skille})


def _workflow(*kroki: tuple[str, str]) -> Workflow:
    """Kroki jako pary (id kroku, id skilla)."""
    return Workflow(nazwa="w", kroki=[Krok(id=k, skill=s) for k, s in kroki])


def _weryfikujace(workflow: Workflow, rejestr: Rejestr) -> list[str]:
    from runner.weryfikacja import kroki_weryfikujace   # import leniwy: brak modulu czerwieni test

    return kroki_weryfikujace(workflow, rejestr)


# --- klasyfikacja kroku ---

def test_krok_ze_skillem_recenzenta_jest_weryfikujacy():
    rejestr = _rejestr(_skill("rec", Rola.reviewer))

    assert _weryfikujace(_workflow(("r", "rec")), rejestr) == ["r"]


@pytest.mark.parametrize("bramka", BRAMKI)
def test_krok_ze_skillem_wymagajacym_capability_bramki_jest_weryfikujacy(bramka):
    rejestr = _rejestr(_skill("s", Rola.author, [bramka]))

    assert _weryfikujace(_workflow(("k", "s")), rejestr) == ["k"]


@pytest.mark.parametrize("rola", [Rola.author, Rola.executor, Rola.gatherer])
def test_krok_innej_roli_niz_recenzent_bez_bramek_nie_jest_weryfikujacy(rola):
    rejestr = _rejestr(_skill("s", rola))

    assert _weryfikujace(_workflow(("k", "s")), rejestr) == []


def test_krok_wymagajacy_capability_spoza_listy_bramek_nie_jest_weryfikujacy():
    rejestr = _rejestr(_skill("s", Rola.executor, ["docker"]))

    assert _weryfikujace(_workflow(("k", "s")), rejestr) == []


def test_krok_z_bramka_obok_innej_capability_jest_weryfikujacy():
    rejestr = _rejestr(_skill("s", Rola.executor, ["docker", "lint"]))

    assert _weryfikujace(_workflow(("k", "s")), rejestr) == ["k"]


def test_kroki_weryfikujace_sa_w_kolejnosci_krokow_workflow_a_nie_rejestru():
    rejestr = _rejestr(
        _skill("zwykly"),
        _skill("lintuje", Rola.author, ["lint"]),
        _skill("recenzent", Rola.reviewer),
        _skill("buduje", Rola.executor, ["build"]),
    )
    workflow = _workflow(
        ("z1", "zwykly"), ("kolejny", "buduje"), ("a", "recenzent"), ("m", "lintuje"),
        ("z2", "zwykly"))

    assert _weryfikujace(workflow, rejestr) == ["kolejny", "a", "m"]


def test_workflow_bez_krokow_weryfikujacych_daje_pusta_liste():
    rejestr = _rejestr(_skill("zwykly"))

    assert _weryfikujace(_workflow(("a", "zwykly"), ("b", "zwykly")), rejestr) == []


def test_krok_systemowy_bez_skilla_nie_jest_weryfikujacy_a_recenzent_po_nim_jest():
    # HAR-176: krok `system: diff` nie ma skilla; wczesniej klasyfikacja rzucala KeyError(None)
    rejestr = _rejestr(_skill("recenzent", Rola.reviewer))
    workflow = Workflow(nazwa="w", kroki=[
        Krok(id="diff", system="diff"), Krok(id="rec", skill="recenzent", needs=["diff"])])

    assert _weryfikujace(workflow, rejestr) == ["rec"]


# --- silnik: pole w run_finished ---

async def test_koniec_wykonanego_runu_niesie_liste_krokow_weryfikujacych(tmp_path):
    s = swiat(tmp_path, [K("a"), K("b", ("a",)), K("c", ("b",))],
              opcje_skilli={"b": {"needs": ["lint"]}, "c": {"role": "reviewer"}})

    zdarzenia = await uruchom(s, Szpieg())

    koniec = zdarzenia[-1]
    assert koniec["kind"] == "run_finished"
    assert koniec["kroki_weryfikujace"] == ["b", "c"]


async def test_koniec_runu_bez_krokow_weryfikujacych_niesie_pusta_liste_a_nie_brak_pola(tmp_path):
    s = swiat(tmp_path, [K("a"), K("b", ("a",))])

    zdarzenia = await uruchom(s, Szpieg())

    assert zdarzenia[-1]["kind"] == "run_finished"
    assert zdarzenia[-1]["kroki_weryfikujace"] == []


async def test_koniec_runu_z_bledem_przed_startem_nie_niesie_listy_krokow_weryfikujacych(tmp_path):
    s = swiat(tmp_path, [K("a")], workflow=None)

    zdarzenia = await uruchom(s, Szpieg())

    assert rodzaje(zdarzenia) == ["step_failed", "run_finished"]
    assert "kroki_weryfikujace" not in zdarzenia[-1]


async def test_koniec_runu_po_wznowieniu_niesie_liste_krokow_weryfikujacych(tmp_path):
    from runner import silnik

    s = swiat(tmp_path, [K("a", extra=", approval: true"), K("b", ("a",))],
              opcje_skilli={"b": {"needs": ["lint"]}})
    await uruchom(s, Szpieg())      # zatrzymanie na bramce kroku a
    zdarzenia: list[dict] = []

    async def zapisz(zdarzenie: dict) -> None:
        zdarzenia.append(zdarzenie)

    await silnik.wznow_run(RUN_ID, "a", "approve", zapisz, Szpieg(),
                           profil=s.profil, dom=s.dom, root=s.root)

    assert zdarzenia[-1]["kind"] == "run_finished"
    assert zdarzenia[-1]["kroki_weryfikujace"] == ["b"]


# --- kryterium: nic nie sprawdzilo, a run jest "do odbioru" ---

@pytest.fixture
async def kokpit(tmp_path, monkeypatch):
    """Zespol "Browarek" z runnerem i runem o identyfikatorze, ktory nadaje silnik."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'k.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    wlasciciel, tenant_id = await zaloz_wlasciciela(maker, nazwa_zespolu="Browarek")
    async with maker() as sesja:
        runner = Runner(tenant_id=tenant_id, name="macbook",
                        token_hash=hashlib.sha256(TOKEN.encode()).hexdigest(),
                        last_seen_at=datetime.now(timezone.utc))
        sesja.add(runner)
        await sesja.flush()
        sesja.add(Run(id=RUN_ID, tenant_id=tenant_id, runner_id=runner.id,
                      status=RunStatus.running))
        await sesja.commit()
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as c:
            await zaloguj_klienta(c, maker, wlasciciel)
            yield c
    finally:
        await engine.dispose()


async def test_workflow_z_trzema_bramkami_przy_zerze_capabilities_daje_run_do_odbioru_niesprawdzony(
        tmp_path, kokpit):
    s = swiat(
        tmp_path, [K("a"), K("b"), K("c")],
        opcje_skilli={"a": {"needs": ["lint"]}, "b": {"needs": ["test_full"]},
                      "c": {"needs": ["build"]}},
        project="stack: [python]\n")      # zero capabilities projektu
    zdarzenia = await uruchom(s, Szpieg())
    for krok, bramka in (("a", "lint"), ("b", "test_full"), ("c", "build")):
        assert jedno(zdarzenia, "step_skipped", krok)["text"] == (
            f"brak_capability: brak capability {bramka}")
    assert zdarzenia[-1]["kroki_weryfikujace"] == ["a", "b", "c"]

    for zdarzenie in zdarzenia:      # jak runner: to samo zdarzenie, plus identyfikator
        odpowiedz = await kokpit.post(
            "/api/events", json={**zdarzenie, "event_id": str(uuid.uuid4())},
            headers={"Authorization": f"Bearer {TOKEN}"})
        assert odpowiedz.status_code == 200, odpowiedz.text

    async with db_session.SessionLocal() as sesja:
        run = (await sesja.execute(select(Run).where(Run.id == RUN_ID))).scalar_one()
    assert run.status == RunStatus.to_accept
    assert (run.bramki_zaplanowane, run.bramki_wykonane, run.bramki_pominiete) == (3, 0, 3)
    assert run.stan_pokrycia == "niesprawdzony"
    assert run.bramki_powody == {
        "a": "brak_capability", "b": "brak_capability", "c": "brak_capability"}
    szczegoly = (await kokpit.get(f"/api/runs/{RUN_ID}")).json()
    assert szczegoly["status"] == "to_accept"
    assert szczegoly["pokrycie"]["stan"] == "niesprawdzony"
    assert (szczegoly["pokrycie"]["zaplanowane"], szczegoly["pokrycie"]["wykonane"],
            szczegoly["pokrycie"]["pominiete"]) == (3, 0, 3)
