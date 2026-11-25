"""HAR-83: krok z `fanout: N` uruchamia N rownoleglych wariantow agenta (TDD, testy przed kodem).

Prawdziwy kod: `runner.silnik.wykonaj_run`, `wykonaj_krok` na prawdziwych procesach. Zaslepiony
jest tylko agent (`claude`): atrapa wariantu zapisuje swoj `agent_id` jako tresc artefaktu (rola
z zapisem) albo na stdout (rola bez zapisu, runner sam zapisuje artefakt) i czeka na pozostale
warianty (bariera z limitem czasu), wiec trzy procesy musza zyc naraz, zeby krok sie udal.
Asercje czytaja dane: pliki w worktree i katalogu runu oraz zdarzenia runu.
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Run, Runner, RunStatus, StepRun, StepStatus
from cockpit.services.invariants import Rola, profil_roli
from runner import silnik
from runner.agent import SpecKroku
from runner.bufor import BladBufora, BrakMiejsca
from runner.checkpoint import StanKrokuCheckpointu, wczytaj_checkpoint, zapisz_checkpoint
from runner.krok import WynikKroku, wykonaj_krok
from sesja_testowa import zaloguj_klienta, zaloz_wlasciciela
from test_silnik_runu import RUN_ID, K, jedno, rodzaje, swiat, uruchom

TOKEN = "token-browarka"

LIMIT_S = 60
BARIERA_S = 30

# Atrapa wariantu: znacznik startu z pid-em, bariera (czeka az wystartuja wszystkie warianty, z
# limitem czasu; po limicie kod 9), spanie, zapis artefaktow z trescia = wlasny agent_id (tylko
# gdy zapis dozwolony), linia z agent_id na stdout, kod wyjscia. Znaczniki leza POZA repozytorium.
KOD_WARIANTU = r"""
import json, os, sys, time
from pathlib import Path

p = json.loads(sys.argv[1])
znaczniki = Path(p["znaczniki"])
agent = p["agent_id"]
(znaczniki / ("start_" + agent)).write_text(str(os.getpid()))
if p["bariera"]:
    koniec = time.time() + p["limit_bariery"]
    while len(list(znaczniki.glob("start_*"))) < p["bariera"]:
        if time.time() > koniec:
            sys.exit(9)
        time.sleep(0.05)
time.sleep(p["spanie"])
for sciezka in p["zapisz"]:
    Path(sciezka).parent.mkdir(parents=True, exist_ok=True)
    Path(sciezka).write_text(agent)
tekst = "wariant " + agent
print(json.dumps({"type": "assistant", "message": {
    "content": [{"type": "text", "text": tekst}]}}), flush=True)
wynik = {"type": "result", "subtype": "success", "is_error": False, "result": tekst}
if p["koszt"] is not None:
    wynik["total_cost_usd"] = p["koszt"]
print(json.dumps(wynik), flush=True)
sys.exit(p["kod"])
"""


@dataclass
class AtrapaWariantow:
    """Agent z prawdziwym procesem przez prawdziwe `wykonaj_krok`; jeden proces na wywolanie."""

    znaczniki: Path
    bariera: int = 0                                   # tyle procesow musi zyc naraz
    kody: dict[str, int] = field(default_factory=dict)       # per agent_id
    spanie: dict[str, float] = field(default_factory=dict)   # per agent_id
    koszty: dict[str, float] = field(default_factory=dict)   # per agent_id; brak = brak pomiaru
    rzuca: dict[str, Exception] = field(default_factory=dict)  # per agent_id: wyjatek zamiast wyniku
    wywolania: list = field(default_factory=list)            # (step_id, agent_id, wejscia)

    async def __call__(
        self, spec, polecenie, katalog, emit, *, katalog_runu=None, prompt=None, **dodatkowe
    ):
        self.wywolania.append((spec.step_id, spec.agent_id, list(spec.wejscia)))
        if spec.agent_id in self.rzuca:
            raise self.rzuca[spec.agent_id]
        opis = {
            "koszt": self.koszty.get(spec.agent_id),
            "znaczniki": str(self.znaczniki), "agent_id": spec.agent_id,
            "bariera": self.bariera, "limit_bariery": BARIERA_S,
            "spanie": self.spanie.get(spec.agent_id, 0),
            "kod": self.kody.get(spec.agent_id, 0),
            "zapisz": list(spec.wyjscia) if profil_roli(spec.rola).zapis else [],
        }
        argv = [sys.executable, "-I", "-c", KOD_WARIANTU, json.dumps(opis)]
        # prompt i dodatkowe argumenty idą do produkcyjnego `wykonaj_krok` (tryb agenta, stream-json)
        return await wykonaj_krok(
            spec, argv, katalog, emit, katalog_runu=katalog_runu, prompt=prompt, **dodatkowe)

    def agenci(self, krok: str) -> list[str]:
        return sorted(a for s, a, _ in self.wywolania if s == krok)

    def wejscia(self, krok: str) -> list[str]:
        (wejscia,) = [w for s, _, w in self.wywolania if s == krok]
        return wejscia

    def pidy(self) -> dict[str, str]:
        return {p.name.removeprefix("start_"): p.read_text() for p in self.znaczniki.glob("start_*")}


def swiat_fanoutu(
    tmp_path, fanout: int, *, rola: str = "author", zalezny: bool = False, konsument: bool = True,
):
    """Krok `analiza` z `fanout` wariantami produkujacy `plan.md`; opcjonalnie krok `synteza`
    (z `konsument` czyta `plan.md`, bez - tylko czeka na `analiza` przez `needs`)."""
    extra = f", fanout: {fanout}" if fanout != 1 else ""
    kroki = [K("analiza", extra=extra)]
    opcje = {"analiza": {"role": rola, "produces": ["plan.md"]}}
    if zalezny:
        kroki.append(K("synteza", ("analiza",)))
        opcje["synteza"] = {"consumes": ["plan.md"]} if konsument else {}
    return swiat(tmp_path, kroki, opcje_skilli=opcje)


def artefakt_wariantu(korzen: Path, agent: str) -> Path:
    return korzen / ".cockpit" / "_run" / "artifacts" / "analiza" / f"agent_{agent}" / "plan.md"


def katalogi_wariantow(korzen: Path) -> list[str]:
    katalog = korzen / ".cockpit" / "_run" / "artifacts" / "analiza"
    if not katalog.is_dir():
        return []
    return sorted(p.name for p in katalog.iterdir() if p.name.startswith("agent_"))


# --- czyste funkcje kontraktu ------------------------------------------------------------


def test_identyfikatory_wariantow_numerowane_sa_od_jedynki_jako_a1_a2_a3():
    from runner.agent import id_wariantu

    assert [id_wariantu(i) for i in (1, 2, 3)] == ["a1", "a2", "a3"]


def test_sciezka_wyjscia_wariantu_lezy_w_podkatalogu_agent_z_jego_identyfikatorem():
    from runner.agent import sciezka_wyjscia_wariantu

    assert sciezka_wyjscia_wariantu("analiza", "a2", "plan.md") == (
        ".cockpit/_run/artifacts/analiza/agent_a2/plan.md")


# --- k1: trzy procesy i trzy agent_id ---------------------------------------------------------


async def test_krok_z_fanout_3_uruchamia_trzy_rownolegle_procesy_agenta(tmp_path):
    s = swiat_fanoutu(tmp_path, 3)
    agent = AtrapaWariantow(s.znaczniki, bariera=3)

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert agent.agenci("analiza") == ["a1", "a2", "a3"]
    assert len(set(agent.pidy().values())) == 3          # trzy rozne procesy
    assert zdarzenia[-1]["kind"] == "run_finished"
    assert "step_failed" not in rodzaje(zdarzenia)       # bariera nie padla: zyly naraz


async def test_krok_z_fanout_3_emituje_start_i_koniec_osobno_dla_kazdego_agent_id(tmp_path):
    s = swiat_fanoutu(tmp_path, 3)

    zdarzenia = await uruchom(s, AtrapaWariantow(s.znaczniki, bariera=3), limit=LIMIT_S)

    for rodzaj in ("step_started", "step_finished", "context_manifest"):
        agenci = sorted(z["agent_id"] for z in zdarzenia
                        if z["kind"] == rodzaj and z["step_id"] == "analiza")
        assert agenci == ["a1", "a2", "a3"], rodzaj
    assert all(z["run_id"] == RUN_ID for z in zdarzenia)


async def test_wyjscie_agenta_z_wariantu_niesie_jego_agent_id(tmp_path):
    s = swiat_fanoutu(tmp_path, 3)

    zdarzenia = await uruchom(s, AtrapaWariantow(s.znaczniki, bariera=3), limit=LIMIT_S)

    for agent_id in ("a1", "a2", "a3"):
        teksty = [z["text"] for z in zdarzenia
                  if z["kind"] == "output" and z["agent_id"] == agent_id]
        assert teksty == [f"wariant {agent_id}"]


# --- k2: trzy podkatalogi, brak nadpisan ----------------------------------------------------


async def test_krok_z_fanout_3_zapisuje_trzy_pliki_w_trzech_podkatalogach_z_trescia_wlasnego_agenta(
    tmp_path,
):
    s = swiat_fanoutu(tmp_path, 3)

    await uruchom(s, AtrapaWariantow(s.znaczniki, bariera=3), limit=LIMIT_S)

    assert katalogi_wariantow(s.worktree) == ["agent_a1", "agent_a2", "agent_a3"]
    for agent_id in ("a1", "a2", "a3"):
        assert artefakt_wariantu(s.worktree, agent_id).read_text() == agent_id
    assert not (s.worktree / "plan.md").exists()         # nikt nie pisze we wspolnej sciezce


async def test_rola_bez_zapisu_z_fanout_3_zapisuje_artefakty_wariantow_w_katalogu_runu(tmp_path):
    s = swiat_fanoutu(tmp_path, 3, rola="reviewer")

    zdarzenia = await uruchom(s, AtrapaWariantow(s.znaczniki, bariera=3), limit=LIMIT_S)

    for agent_id in ("a1", "a2", "a3"):
        plik = s.katalog_runu / "analiza" / f"agent_{agent_id}" / "plan.md"
        assert plik.read_text().strip() == f"wariant {agent_id}"
    assert not (s.katalog_runu / "analiza" / "plan.md").exists()
    assert not (s.worktree / ".cockpit" / "_run" / "artifacts" / "analiza").exists()
    assert "step_failed" not in rodzaje(zdarzenia)       # recenzent nie naruszyl odczytu


async def test_manifest_kazdego_wariantu_ma_osobny_plik_z_jego_agent_id_w_nazwie(tmp_path):
    s = swiat_fanoutu(tmp_path, 3)

    await uruchom(s, AtrapaWariantow(s.znaczniki, bariera=3), limit=LIMIT_S)

    manifesty = sorted(p.name for p in s.katalog_runu.glob("manifest-*.json"))
    assert len(manifesty) == 3
    for agent_id in ("a1", "a2", "a3"):
        assert len([m for m in manifesty if agent_id in m]) == 1


# --- k3: fanout 1 bez zmian ------------------------------------------------------------------


async def test_krok_z_fanout_1_ma_agent_id_rowny_step_id_i_sciezke_artefaktu_bez_podkatalogu(
    tmp_path,
):
    s = swiat_fanoutu(tmp_path, 1)
    agent = AtrapaWariantow(s.znaczniki, bariera=1)

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert agent.agenci("analiza") == ["analiza"]
    assert jedno(zdarzenia, "step_started", "analiza")["agent_id"] == "analiza"
    assert jedno(zdarzenia, "step_finished", "analiza")["agent_id"] == "analiza"
    assert (s.worktree / "plan.md").read_text() == "analiza"
    assert katalogi_wariantow(s.worktree) == []
    assert (s.katalog_runu / "manifest-analiza.json").is_file()


async def test_rola_bez_zapisu_z_fanout_1_zachowuje_artefakt_w_katalogu_kroku(tmp_path):
    s = swiat_fanoutu(tmp_path, 1, rola="reviewer")
    agent = AtrapaWariantow(s.znaczniki, bariera=1)

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert (s.katalog_runu / "analiza" / "plan.md").read_text().strip() == "wariant analiza"
    assert list((s.katalog_runu / "analiza").glob("agent_*")) == []
    assert jedno(zdarzenia, "step_finished", "analiza")["agent_id"] == "analiza"


async def test_krok_zalezny_od_kroku_z_fanout_1_dostaje_nazwe_artefaktu_jak_dotad(tmp_path):
    s = swiat_fanoutu(tmp_path, 1, zalezny=True)
    agent = AtrapaWariantow(s.znaczniki)

    await uruchom(s, agent, limit=LIMIT_S)

    assert agent.wejscia("synteza") == ["plan.md"]


# --- k4: porazka jednego wariantu ----------------------------------------------------------


async def test_porazka_jednego_wariantu_daje_krok_nieudany_z_agent_id_w_powodzie_a_zalezny_pominiety(
    tmp_path,
):
    s = swiat_fanoutu(tmp_path, 3, zalezny=True, konsument=False)
    agent = AtrapaWariantow(s.znaczniki, bariera=3, kody={"a2": 3})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    nieudane = [z for z in zdarzenia if z["kind"] == "step_failed" and z["step_id"] == "analiza"]
    assert nieudane, "krok z nieudanym wariantem musi miec step_failed"
    assert [z["agent_id"] for z in nieudane] == ["a2"]            # tylko wariant, ktory padl
    assert nieudane[0]["text"] == "wariant a2: kod wyjścia 3"
    assert not [z for z in zdarzenia
                if z["kind"] == "step_finished" and z["step_id"] == "analiza"
                and z["agent_id"] == "a2"]
    pominiety = jedno(zdarzenia, "step_skipped", "synteza")
    assert pominiety["text"].startswith("zaleznosc_nieudana: ")
    assert agent.agenci("synteza") == []
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_porazka_jednego_wariantu_nie_przerywa_pozostalych_ktore_dokanczaja_artefakty(tmp_path):
    s = swiat_fanoutu(tmp_path, 3)
    # a1 i a3 pisza dopiero po spaniu, a a2 pada od razu: zapis dowodzi, ze dobiegly do konca
    agent = AtrapaWariantow(
        s.znaczniki, bariera=3, kody={"a2": 3}, spanie={"a1": 1.0, "a3": 1.0})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert artefakt_wariantu(s.worktree, "a1").read_text() == "a1"
    assert artefakt_wariantu(s.worktree, "a3").read_text() == "a3"
    assert any("a2" in z["text"] for z in zdarzenia
               if z["kind"] == "step_failed" and z["step_id"] == "analiza")


async def test_fanout_bez_porazki_wariantow_konczy_krok_sukcesem_i_uruchamia_zalezny(tmp_path):
    s = swiat_fanoutu(tmp_path, 3, zalezny=True)
    agent = AtrapaWariantow(s.znaczniki, bariera=3)

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert "step_failed" not in rodzaje(zdarzenia)
    assert "step_skipped" not in rodzaje(zdarzenia)
    assert agent.agenci("synteza") == ["synteza"]


# --- krok zalezny dostaje sciezki wszystkich wariantow ----------------------------------------


async def test_krok_zalezny_dostaje_sciezki_artefaktow_wszystkich_wariantow(tmp_path):
    s = swiat_fanoutu(tmp_path, 3, zalezny=True)
    agent = AtrapaWariantow(s.znaczniki, bariera=3)

    await uruchom(s, agent, limit=LIMIT_S)

    wejscia = agent.wejscia("synteza")
    assert len(wejscia) == 3
    for agent_id in ("a1", "a2", "a3"):
        (sciezka,) = [w for w in wejscia if w.endswith(f"agent_{agent_id}/plan.md")]
        assert (s.worktree / sciezka).read_text() == agent_id   # sciezka wskazuje prawdziwy plik


async def test_krok_zalezny_od_roli_bez_zapisu_z_fanout_dostaje_sciezki_wszystkich_wariantow(tmp_path):
    s = swiat_fanoutu(tmp_path, 3, rola="reviewer", zalezny=True)
    agent = AtrapaWariantow(s.znaczniki, bariera=3)

    await uruchom(s, agent, limit=LIMIT_S)

    wejscia = agent.wejscia("synteza")
    assert len(wejscia) == 3
    for agent_id in ("a1", "a2", "a3"):
        (sciezka,) = [w for w in wejscia if w.endswith(f"agent_{agent_id}/plan.md")]
        assert (s.worktree / sciezka).read_text().strip() == f"wariant {agent_id}"


# --- numer wariantu i skladniki sciezek z danych: zle wejscie to ValueError -----------------------


@pytest.mark.parametrize("numer", [0, -1, True, "1", 1.0, None], ids=repr)
def test_numer_wariantu_inny_niz_calkowity_od_jedynki_jest_odrzucony(numer):
    from runner.agent import id_wariantu

    with pytest.raises(ValueError, match="numer wariantu"):
        id_wariantu(numer)


ZLE_SEGMENTY = ["", ".", "..", "a/b", "a\\b", "a\x00b"]


@pytest.mark.parametrize("segment", ZLE_SEGMENTY, ids=repr)
def test_segment_sciezki_z_separatorem_kropkami_pustym_napisem_albo_nul_jest_odrzucony(segment):
    from runner.agent import _segment_sciezki

    with pytest.raises(ValueError, match="niedozwolony pole_testu"):
        _segment_sciezki("pole_testu", segment)


def test_segment_sciezki_z_zwyklej_nazwy_jest_przyjety():
    from runner.agent import _segment_sciezki

    assert _segment_sciezki("pole", "a1") is None
    assert _segment_sciezki("pole", "analiza-2.v1") is None


@pytest.mark.parametrize("agent_id", ZLE_SEGMENTY, ids=repr)
def test_podkatalog_wariantu_odrzuca_zly_identyfikator_wariantu(agent_id):
    from runner.agent import podkatalog_wariantu

    with pytest.raises(ValueError, match="identyfikator wariantu"):
        podkatalog_wariantu(agent_id)


@pytest.mark.parametrize("nazwa", ["/etc/plan.md", "../plan.md", "a/../../plan.md", "", "a\x00.md"],
                         ids=repr)
def test_sciezka_wyjscia_wariantu_odrzuca_nazwe_bezwzgledna_z_kropkami_pusta_albo_z_nul(nazwa):
    from runner.agent import sciezka_wyjscia_wariantu

    with pytest.raises(ValueError, match="niedozwolona ścieżka artefaktu"):
        sciezka_wyjscia_wariantu("analiza", "a1", nazwa)


@pytest.mark.parametrize("step_id", ZLE_SEGMENTY, ids=repr)
def test_sciezka_wyjscia_wariantu_odrzuca_zly_identyfikator_kroku(step_id):
    from runner.agent import sciezka_wyjscia_wariantu

    with pytest.raises(ValueError, match="identyfikator kroku"):
        sciezka_wyjscia_wariantu(step_id, "a1", "plan.md")


@pytest.mark.parametrize("agent_id", ZLE_SEGMENTY, ids=repr)
def test_sciezka_wyjscia_wariantu_odrzuca_zly_identyfikator_wariantu(agent_id):
    from runner.agent import sciezka_wyjscia_wariantu

    with pytest.raises(ValueError, match="identyfikator wariantu"):
        sciezka_wyjscia_wariantu("analiza", agent_id, "plan.md")


def test_sciezka_wyjscia_wariantu_zachowuje_podkatalogi_nazwy_artefaktu():
    from runner.agent import sciezka_wyjscia_wariantu

    assert sciezka_wyjscia_wariantu("analiza", "a3", "raporty/plan.md") == (
        ".cockpit/_run/artifacts/analiza/agent_a3/raporty/plan.md")


def test_etykieta_pliku_bez_wariantu_to_sam_step_id_a_z_wariantem_dopisuje_agent_id():
    from runner.agent import etykieta_pliku

    assert etykieta_pliku("analiza", "analiza", False) == "analiza"
    assert etykieta_pliku("analiza", "a2", True) == "analiza-a2"
    # bez wariantu agent_id nie wchodzi do nazwy, wiec nie jest sprawdzany
    assert etykieta_pliku("analiza", "a/b", False) == "analiza"


@pytest.mark.parametrize("agent_id", ZLE_SEGMENTY, ids=repr)
def test_etykieta_pliku_wariantu_odrzuca_zly_identyfikator_wariantu(agent_id):
    from runner.agent import etykieta_pliku

    with pytest.raises(ValueError, match="identyfikator wariantu"):
        etykieta_pliku("analiza", agent_id, True)


# --- zly agent_id wariantu na granicy runnera: porazka kroku, nie wyjatek --------------------------

KOD_ZNACZNIKA = "import sys; open(sys.argv[1], 'w').write('proces wystartowal')"


def _spec_wariantu(rola: Rola, agent_id: str, wyjscia: list[str]) -> SpecKroku:
    return SpecKroku(
        run_id=RUN_ID, step_id="analiza", agent_id=agent_id, skill_id="s_analiza", rola=rola,
        wejscia=[], wyjscia=wyjscia, komendy={}, wariant=True)


async def test_rola_bez_zapisu_ze_zlym_agent_id_wariantu_jest_porazka_bez_uruchomienia_procesu(
    tmp_path,
):
    katalog, katalog_runu, znacznik = tmp_path / "repo", tmp_path / "runy", tmp_path / "znacznik"
    katalog.mkdir()
    spec = _spec_wariantu(Rola.reviewer, "a/b", ["plan.md"])

    wynik = await asyncio.wait_for(wykonaj_krok(
        spec, [sys.executable, "-I", "-c", KOD_ZNACZNIKA, str(znacznik)], katalog,
        _bez_zdarzen, katalog_runu=katalog_runu), LIMIT_S)

    assert wynik.status == "nieudany"
    assert wynik.powod is not None and "niedozwolony identyfikator wariantu" in wynik.powod
    assert not znacznik.exists()
    assert not katalog_runu.exists()


async def test_prompt_wariantu_ze_zlym_agent_id_jest_porazka_bez_uruchomienia_procesu(tmp_path):
    katalog, katalog_runu, znacznik = tmp_path / "repo", tmp_path / "runy", tmp_path / "znacznik"
    katalog.mkdir()
    spec = _spec_wariantu(Rola.author, "..", ["plan.md"])

    wynik = await asyncio.wait_for(wykonaj_krok(
        spec, [sys.executable, "-I", "-c", KOD_ZNACZNIKA, str(znacznik)], katalog,
        _bez_zdarzen, katalog_runu=katalog_runu, prompt="przeczytaj skill"), LIMIT_S)

    assert wynik.status == "nieudany"
    assert wynik.powod is not None and "niedozwolony identyfikator wariantu" in wynik.powod
    assert not znacznik.exists()
    assert not (katalog_runu / "prompty").exists()


async def test_manifest_wariantu_ze_zlym_agent_id_konczy_sie_porazka_kroku_z_przyczyna_wartosci():
    krok = SimpleNamespace(id="analiza", skill="s_analiza")

    with pytest.raises(silnik.PorazkaKroku, match="niedozwolony identyfikator wariantu") as blad:
        await silnik._zloz_i_zapisz_manifest(None, krok, [], "a/b")

    assert isinstance(blad.value.__cause__, ValueError)


async def _bez_zdarzen(zdarzenie: dict) -> None:
    return None


# --- wynik zbiorczy wariantow: wyjatek wariantu to jego porazka -------------------------------------

ZDANY = WynikKroku(status="zakonczony", powod=None, kod_wyjscia=0)


def test_wyjatek_wariantu_daje_porazke_kroku_z_agent_id_i_sama_nazwa_typu_bez_tresci_wyjatku():
    wynik = silnik._zbierz_warianty(
        "analiza", ["a1", "a2"], [ZDANY, RuntimeError("haslo=tajne")])

    assert wynik.status == "nieudany"
    assert wynik.powod == "wariant a2: nieoczekiwany wyjątek kroku: RuntimeError"
    assert "tajne" not in wynik.powod
    assert wynik.kod_wyjscia is None
    assert [a for a, _ in wynik.warianty] == ["a1", "a2"]
    assert wynik.warianty[0][1] is ZDANY


def test_blad_granicy_w_wariancie_niesie_swoj_tekst_z_agent_id():
    wynik = silnik._zbierz_warianty("analiza", ["a1", "a2"], [OSError("dysk pelny"), ZDANY])

    assert wynik.powod == "wariant a1: dysk pelny"


def test_porazka_kroku_wariantu_niesie_swoj_tekst_z_agent_id():
    wynik = silnik._zbierz_warianty(
        "analiza", ["a1", "a2"], [ZDANY, silnik.PorazkaKroku("brak modelu")])

    assert wynik.powod == "wariant a2: brak modelu"


def test_pierwszy_nieudany_wariant_wedlug_kolejnosci_agentow_nadaje_powod_krokowi():
    nieudany_a1 = WynikKroku(
        status="nieudany", powod="kod wyjścia 3", kod_wyjscia=3, werdykt="odrzucone",
        dowod={"komenda": "x"})

    wynik = silnik._zbierz_warianty(
        "analiza", ["a1", "a2", "a3"], [nieudany_a1, RuntimeError("x"), ZDANY])

    assert wynik.powod == "wariant a1: kod wyjścia 3"
    assert wynik.kod_wyjscia == 3
    assert wynik.werdykt == "odrzucone"
    assert wynik.dowod == {"komenda": "x"}


def test_wszystkie_warianty_zdane_daja_krok_zdany_z_kodem_pierwszego_wariantu():
    drugi = WynikKroku(status="zakonczony", powod=None, kod_wyjscia=7)    # kod inny niz pierwszego

    wynik = silnik._zbierz_warianty("analiza", ["a1", "a2"], [ZDANY, drugi])

    assert wynik.status == "zakonczony"
    assert wynik.kod_wyjscia == ZDANY.kod_wyjscia == 0
    assert wynik.powod is None
    assert [a for a, _ in wynik.warianty] == ["a1", "a2"]


@pytest.mark.parametrize("blad", [BladBufora("zepsuty bufor"), BrakMiejsca("brak miejsca")],
                         ids=lambda b: type(b).__name__)
def test_blad_bufora_w_wariancie_nie_jest_porazka_wariantu_tylko_leci_do_wolajacego(blad):
    with pytest.raises(BladBufora):
        silnik._zbierz_warianty("analiza", ["a1", "a2"], [ZDANY, blad])


def test_anulowanie_wariantu_nie_jest_porazka_wariantu_tylko_leci_do_wolajacego():
    with pytest.raises(asyncio.CancelledError):
        silnik._zbierz_warianty("analiza", ["a1", "a2"], [ZDANY, asyncio.CancelledError()])


# --- zdarzenia konca kroku z wariantami: komplet, tresc, kolejnosc ---------------------------------


def koniec_kroku(zdarzenia: list[dict], krok: str, proba: int | None = None) -> list[tuple[str, str]]:
    """(rodzaj, agent_id) zdarzen konca kroku w kolejnosci emisji."""
    return [(z["kind"], z["agent_id"]) for z in zdarzenia
            if z["step_id"] == krok and z["kind"] in ("step_finished", "step_failed")
            and (proba is None or z["attempt"] == proba)]


def kazdy_start_ma_koniec(zdarzenia: list[dict]) -> None:
    """Kazdy (krok, agent_id, proba) z `step_started` ma `step_finished` albo `step_failed`."""
    def klucze(rodzaje_zdarzen):
        return {(z["step_id"], z["agent_id"], z["attempt"]) for z in zdarzenia
                if z["kind"] in rodzaje_zdarzen}

    starty = klucze({"step_started"})
    assert starty
    assert starty - klucze({"step_finished", "step_failed"}) == set()


def teksty_porazek(zdarzenia: list[dict], krok: str, proba: int) -> dict[str, str]:
    return {z["agent_id"]: z["text"] for z in zdarzenia
            if z["kind"] == "step_failed" and z["step_id"] == krok and z["attempt"] == proba}


async def test_wyjatek_jednego_wariantu_nie_przerywa_drugiego_ktory_dopisuje_artefakt_po_spaniu(
    tmp_path,
):
    s = swiat_fanoutu(tmp_path, 2)
    agent = AtrapaWariantow(
        s.znaczniki, spanie={"a2": 1.0}, rzuca={"a1": RuntimeError("haslo=tajne")})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert artefakt_wariantu(s.worktree, "a2").read_text() == "a2"
    assert koniec_kroku(zdarzenia, "analiza") == [("step_finished", "a2"), ("step_failed", "a1")]
    assert teksty_porazek(zdarzenia, "analiza", 1) == {
        "a1": "wariant a1: nieoczekiwany wyjątek kroku: RuntimeError"}
    kazdy_start_ma_koniec(zdarzenia)
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_prompt_kazdego_wariantu_ma_osobny_plik_z_jego_sciezka_wyjscia(tmp_path):
    s = swiat_fanoutu(tmp_path, 2)

    await uruchom(s, AtrapaWariantow(s.znaczniki, bariera=2), limit=LIMIT_S)

    prompty = s.katalog_runu / "prompty"
    assert sorted(p.name for p in prompty.iterdir()) == ["analiza-a1.txt", "analiza-a2.txt"]
    pierwszy = (prompty / "analiza-a1.txt").read_text()
    drugi = (prompty / "analiza-a2.txt").read_text()
    assert pierwszy != drugi
    assert ".cockpit/_run/artifacts/analiza/agent_a1/plan.md" in pierwszy
    assert "agent_a2" not in pierwszy
    assert ".cockpit/_run/artifacts/analiza/agent_a2/plan.md" in drugi
    assert "agent_a1" not in drugi


async def test_prompt_kroku_z_fanout_1_zostaje_w_pliku_o_nazwie_kroku(tmp_path):
    s = swiat_fanoutu(tmp_path, 1)

    await uruchom(s, AtrapaWariantow(s.znaczniki), limit=LIMIT_S)

    assert [p.name for p in (s.katalog_runu / "prompty").iterdir()] == ["analiza.txt"]


async def test_pomiar_kosztu_trafia_do_zdarzenia_konca_wlasnego_wariantu_a_brak_pomiaru_to_brak(
    tmp_path,
):
    s = swiat_fanoutu(tmp_path, 3)
    agent = AtrapaWariantow(s.znaczniki, koszty={"a1": 0.25, "a2": 0.5})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    konce = {z["agent_id"]: z for z in zdarzenia if z["kind"] == "step_finished"}
    assert {a: (z["cost_usd"], z["cost_source"]) for a, z in konce.items()} == {
        "a1": (0.25, "measured"), "a2": (0.5, "measured"), "a3": (None, "unavailable")}


async def test_dwa_nieudane_warianty_maja_kazdy_swoje_step_failed_z_wlasnym_kodem_i_agent_id(
    tmp_path,
):
    s = swiat_fanoutu(tmp_path, 3)
    agent = AtrapaWariantow(s.znaczniki, bariera=3, kody={"a1": 3, "a2": 4})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert teksty_porazek(zdarzenia, "analiza", 1) == {
        "a1": "wariant a1: kod wyjścia 3", "a2": "wariant a2: kod wyjścia 4"}
    assert koniec_kroku(zdarzenia, "analiza")[0] == ("step_finished", "a3")
    kazdy_start_ma_koniec(zdarzenia)


async def test_wyczerpany_limit_ponowien_zastepuje_powod_pierwszego_nieudanego_wariantu_a_drugi_zostaje(
    tmp_path,
):
    s = swiat(
        tmp_path,
        [K("plan"), K("analiza", ("plan",), extra=", fanout: 2, on_fail: {retry: plan, max: 1}")],
        opcje_skilli={"analiza": {"produces": ["plan.md"]}})
    agent = AtrapaWariantow(s.znaczniki, kody={"a1": 3, "a2": 4})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert teksty_porazek(zdarzenia, "analiza", 1) == {
        "a1": "wariant a1: kod wyjścia 3", "a2": "wariant a2: kod wyjścia 4"}
    assert teksty_porazek(zdarzenia, "analiza", 2) == {
        "a1": "wyczerpany limit 1 ponowień kroku analiza", "a2": "wariant a2: kod wyjścia 4"}
    kazdy_start_ma_koniec(zdarzenia)
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_zdarzenia_konca_kroku_ida_udane_przed_nieudanymi_a_w_obu_grupach_po_kolei_wariantow(
    tmp_path,
):
    # Kolejnosc jest zachowaniem silnika (`sorted` po statusie wariantu), ale nic w tickecie ani
    # w kokpicie jej nie wymaga: test ja utrwala, wiec usuniecie sortowania usuwa tez ten test.
    s = swiat_fanoutu(tmp_path, 4)
    agent = AtrapaWariantow(s.znaczniki, bariera=4, kody={"a1": 3, "a3": 3})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert koniec_kroku(zdarzenia, "analiza") == [
        ("step_finished", "a2"), ("step_finished", "a4"),
        ("step_failed", "a1"), ("step_failed", "a3")]


async def test_krok_systemowy_z_fanout_3_ma_jedno_step_started_i_jedno_step_finished_pod_step_id(
    tmp_path,
):
    from test_krok_diff import SzpiegZmieniajacyDrzewo, WORKFLOW_Z_DIFFEM, swiat_z_diffem, zmien_drzewo

    tresc = WORKFLOW_Z_DIFFEM.replace(
        "diff: {system: diff, needs: [autor]}", "diff: {system: diff, needs: [autor], fanout: 3}")
    assert "fanout: 3" in tresc
    s = swiat_z_diffem(tmp_path, tresc)

    zdarzenia = await uruchom(
        s, SzpiegZmieniajacyDrzewo(zmiany={"autor": zmien_drzewo}), limit=LIMIT_S)

    assert [z["agent_id"] for z in zdarzenia if z["kind"] == "step_started" and z["step_id"] == "diff"] == [
        "diff"]
    assert koniec_kroku(zdarzenia, "diff") == [("step_finished", "diff")]
    assert "step_failed" not in rodzaje(zdarzenia)


# --- lancuch producentow: najblizszy producent artefaktu wygrywa -----------------------------------


async def test_konsument_artefaktu_dostaje_jego_nazwe_gdy_najblizszy_producent_nie_ma_fanoutu(tmp_path):
    s = swiat(
        tmp_path,
        [K("analiza", extra=", fanout: 3"), K("synteza", ("analiza",)),
         K("konsument", ("synteza",))],
        opcje_skilli={
            "analiza": {"produces": ["plan.md"]},
            "synteza": {"consumes": ["plan.md"], "produces": ["plan.md"]},
            "konsument": {"consumes": ["plan.md"]},
        })
    agent = AtrapaWariantow(s.znaczniki, bariera=3)

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert "step_failed" not in rodzaje(zdarzenia)
    assert len(agent.wejscia("synteza")) == 3                # synteza widzi trzy warianty
    assert agent.wejscia("konsument") == ["plan.md"]         # a konsument tylko wynik syntezy


async def test_konsument_artefaktu_ktorego_producent_z_fanoutem_nie_produkuje_dostaje_nazwe(tmp_path):
    s = swiat(
        tmp_path,
        [K("analiza", extra=", fanout: 3"), K("uwagi"), K("konsument", ("analiza", "uwagi"))],
        opcje_skilli={
            "analiza": {"produces": ["plan.md"]},
            "uwagi": {"produces": ["uwagi.md"]},
            "konsument": {"consumes": ["uwagi.md"]},
        })
    agent = AtrapaWariantow(s.znaczniki, bariera=0)

    await uruchom(s, agent, limit=LIMIT_S)

    assert agent.wejscia("konsument") == ["uwagi.md"]


# --- bramka, wyjatek proby, przerwanie, zapis checkpointu i notatka zmiany dla kroku z wariantami -----


def zdarzenia_do_przeslania(zdarzenia: list[dict]) -> list[dict]:
    return [{**z, "event_id": str(uuid.uuid4())} for z in zdarzenia]


@dataclass
class Baza:
    klient: AsyncClient
    run_id: str


@pytest.fixture
async def baza(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'baza.db'}")
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
        run = Run(tenant_id=tenant_id, runner_id=runner.id, status=RunStatus.running)
        sesja.add(run)
        await sesja.commit()
        run_id = run.id
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as klient:
            await zaloguj_klienta(klient, maker, wlasciciel)
            yield Baza(klient, run_id)
    finally:
        await engine.dispose()


async def wyslij_do_kokpitu(baza: Baza, zdarzenia: list[dict]) -> None:
    for z in zdarzenia_do_przeslania(zdarzenia):
        odpowiedz = await baza.klient.post(
            "/api/events", json=z, headers={"Authorization": f"Bearer {TOKEN}"})
        assert odpowiedz.status_code == 200, (odpowiedz.text, z)


async def wznow(s, agent, run_id: str, krok: str, decyzja: str = "approve") -> list[dict]:
    zdarzenia: list[dict] = []

    async def zapisz(zdarzenie: dict) -> None:
        zdarzenia.append(zdarzenie)

    await asyncio.wait_for(
        silnik.wznow_run(
            run_id, krok, decyzja, zapisz, agent, profil=s.profil, dom=s.dom, root=s.root),
        LIMIT_S)
    return zdarzenia


async def test_bramka_na_kroku_z_fanout_3_czeka_per_agent_a_po_decyzji_dane_maja_tylko_wiersze_wariantow(
    tmp_path, baza,
):
    s = swiat(tmp_path, [K("analiza", extra=", approval: true, fanout: 3")],
              opcje_skilli={"analiza": {"produces": ["plan.md"]}})
    polecenie = {**s.polecenie, "run_id": baza.run_id}
    agent = AtrapaWariantow(s.znaczniki, bariera=3)

    przed = await uruchom(s, agent, polecenie=polecenie, limit=LIMIT_S)

    assert [(z["kind"], z["agent_id"]) for z in przed] == [
        ("step_waiting", "a1"), ("step_waiting", "a2"), ("step_waiting", "a3")]
    assert agent.wywolania == []                            # bramka stoi przed procesami
    po = await wznow(s, agent, baza.run_id, "analiza")
    await wyslij_do_kokpitu(baza, przed + po)

    async with db_session.SessionLocal() as sesja:
        wiersze = (await sesja.execute(
            select(StepRun).where(StepRun.run_id == baza.run_id))).scalars().all()
        run = (await sesja.execute(select(Run).where(Run.id == baza.run_id))).scalar_one()
    assert sorted((w.agent_id, w.status) for w in wiersze) == [
        ("a1", StepStatus.finished), ("a2", StepStatus.finished), ("a3", StepStatus.finished)]
    assert run.status == RunStatus.to_accept
    assert sorted(agent.agenci("analiza")) == ["a1", "a2", "a3"]


async def test_wyjatek_zadania_kroku_w_ponowionej_probie_konczy_wszystkie_warianty_step_failed(
    tmp_path, monkeypatch,
):
    s = swiat(
        tmp_path,
        [K("analiza", extra=", fanout: 2"),
         K("testuj", ("analiza",), extra=", on_fail: {retry: analiza, max: 1}")])
    prawdziwy_stat = Path.stat

    def stat(self, *argumenty, **opcje):
        if self.as_posix().endswith("artifacts/analiza/_naprawa/proba_1.md"):
            raise PermissionError(13, "brak praw")
        return prawdziwy_stat(self, *argumenty, **opcje)

    monkeypatch.setattr(Path, "stat", stat)
    agent = AtrapaWariantow(s.znaczniki, kody={"testuj": 1})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert teksty_porazek(zdarzenia, "analiza", 2) == {
        "a1": "nie sprawdzono raportu porażki (PermissionError)",
        "a2": "nie sprawdzono raportu porażki (PermissionError)"}
    assert sorted(a for st, a, _ in agent.wywolania if st == "analiza") == ["a1", "a2"]  # tylko 1. proba
    kazdy_start_ma_koniec(zdarzenia)
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_wznowienie_runu_z_przerwanym_krokiem_fanout_3_daje_trzy_step_failed_z_tekstem_przerwania(
    tmp_path,
):
    s = swiat(tmp_path, [K("dlugi", extra=", fanout: 3"),
                         K("zatwierdz", extra=", approval: true"), K("koniec", ("zatwierdz",))])
    przed = await uruchom(s, AtrapaWariantow(s.znaczniki, bariera=3), limit=LIMIT_S)
    assert przed[-1]["kind"] == "step_waiting"
    cp = wczytaj_checkpoint(s.katalog_runu)
    zapisz_checkpoint(s.katalog_runu, replace(cp, kroki={
        **cp.kroki, "dlugi": StanKrokuCheckpointu("w_toku", cp.kroki["dlugi"].proby)}))

    po = await wznow(s, AtrapaWariantow(s.znaczniki), RUN_ID, "zatwierdz")

    przerwane = [z for z in po if z["kind"] == "step_failed"]
    assert [(z["step_id"], z["agent_id"], z["text"]) for z in przerwane] == [
        ("dlugi", a, silnik.TEKST_PRZERWANIA) for a in ("a1", "a2", "a3")]
    assert po[:3] == przerwane                               # przed dalszym wykonaniem planu
    assert po[-1]["kind"] == "run_finished"


async def test_nieudany_zapis_checkpointu_na_bramce_z_fanoutem_daje_step_failed_dla_kazdego_agenta(
    tmp_path, monkeypatch,
):
    from test_bramka_zatwierdzenia import PREFIKS_BLEDU_ZAPISU, _zepsuj_zapis_checkpointu

    s = swiat(tmp_path, [K("analiza", extra=", approval: true, fanout: 3")])
    _zepsuj_zapis_checkpointu(monkeypatch, od_zapisu=1)

    zdarzenia = await uruchom(s, AtrapaWariantow(s.znaczniki), limit=LIMIT_S)

    nieudane = [z for z in zdarzenia if z["kind"] == "step_failed"]
    assert [z["agent_id"] for z in nieudane] == ["a1", "a2", "a3"]
    assert all(z["text"].startswith(PREFIKS_BLEDU_ZAPISU) and "dysk pelny" in z["text"]
               for z in nieudane)
    assert "step_waiting" not in rodzaje(zdarzenia)
    assert rodzaje(zdarzenia).count("run_finished") == 1
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_fanout_2_ze_skillem_z_notatka_zmiany_konczy_krok_przed_startem_procesow(tmp_path):
    s = swiat(tmp_path, [K("analiza", extra=", fanout: 2")],
              opcje_skilli={"analiza": {"produces": ["notatka-zmiany.md"]}})
    agent = AtrapaWariantow(s.znaczniki)

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert teksty_porazek(zdarzenia, "analiza", 1) == {
        "a1": "fanout z notatką zmiany nieobsługiwany", "a2": "fanout z notatką zmiany nieobsługiwany"}
    assert agent.wywolania == []
    assert agent.pidy() == {}                                # zaden proces nie wystartowal
    kazdy_start_ma_koniec(zdarzenia)
    assert zdarzenia[-1]["kind"] == "run_finished"


# --- blad wznowienia bramki z fanoutem: wiersze a1..aN zamkniete, bez wiersza (krok, krok) ---------


def swiat_bramki_fanoutu(tmp_path):
    return swiat(
        tmp_path,
        [K("analiza", extra=", approval: true, fanout: 3"), K("inny")],
        opcje_skilli={"analiza": {"produces": ["plan.md"]}})


async def do_bramki(s, run_id: str = RUN_ID) -> list[dict]:
    zdarzenia = await uruchom(
        s, AtrapaWariantow(s.znaczniki), polecenie={**s.polecenie, "run_id": run_id},
        limit=LIMIT_S)
    assert zdarzenia[-1]["kind"] == "step_waiting"
    return zdarzenia


def koniec_wznowienia(zdarzenia: list[dict]) -> list[tuple]:
    """(rodzaj, step_id, agent_id, proba) zdarzen konca nieudanego wznowienia."""
    return [(z["kind"], z["step_id"], z["agent_id"], z.get("attempt")) for z in zdarzenia]


WIERSZE_BRAMKI_Z_FANOUTEM = [
    ("step_failed", "analiza", "a1", 1), ("step_failed", "analiza", "a2", 1),
    ("step_failed", "analiza", "a3", 1), ("run_finished", "analiza", "", None)]
WIERSZ_BRAMKI_BEZ_PROBY = [
    ("step_failed", "analiza", "analiza", None), ("run_finished", "analiza", "", None)]


async def wiersze_i_run(baza: Baza) -> tuple[list[tuple], Run]:
    async with db_session.SessionLocal() as sesja:
        wiersze = (await sesja.execute(
            select(StepRun).where(StepRun.run_id == baza.run_id))).scalars().all()
        run = (await sesja.execute(select(Run).where(Run.id == baza.run_id))).scalar_one()
    # tylko krok z bramka; niezalezny krok `inny` ma wlasny wiersz
    return sorted((w.step_id, w.agent_id, w.attempt, w.status)
                  for w in wiersze if w.step_id == "analiza"), run


async def test_blad_zapisu_decyzji_na_bramce_z_fanout_3_zamyka_wiersze_wariantow_w_bazie(
    tmp_path, monkeypatch, baza,
):
    s = swiat_bramki_fanoutu(tmp_path)
    przed = await do_bramki(s, baza.run_id)

    def zapis_nie_dziala(*argumenty, **opcje):
        raise OSError("dysk pelny")

    monkeypatch.setattr(silnik, "_zapisz_atomowo", zapis_nie_dziala)
    po = await wznow(s, AtrapaWariantow(s.znaczniki), baza.run_id, "analiza")

    assert [z["text"] for z in po[:3]] == ["zapis artefaktu decyzji dla kroku analiza: dysk pelny"] * 3
    await wyslij_do_kokpitu(baza, przed + po)
    wiersze, run = await wiersze_i_run(baza)
    assert wiersze == [("analiza", a, 1, StepStatus.failed) for a in ("a1", "a2", "a3")]
    assert run.status == RunStatus.failed


async def test_blad_planu_po_decyzji_na_bramce_z_fanout_3_zamyka_wiersze_wariantow_w_bazie(
    tmp_path, monkeypatch, baza,
):
    s = swiat_bramki_fanoutu(tmp_path)
    przed = await do_bramki(s, baza.run_id)

    def plan_nie_dziala(*argumenty, **opcje):
        raise OSError("plan niedostepny")

    monkeypatch.setattr(silnik, "nastepne", plan_nie_dziala)
    po = await wznow(s, AtrapaWariantow(s.znaczniki), baza.run_id, "analiza")

    assert [(z["agent_id"], z.get("attempt")) for z in po if z["kind"] == "step_failed"] == [
        ("a1", 1), ("a2", 1), ("a3", 1)]
    await wyslij_do_kokpitu(baza, przed + po)
    wiersze, run = await wiersze_i_run(baza)
    assert wiersze == [("analiza", a, 1, StepStatus.failed) for a in ("a1", "a2", "a3")]
    assert run.status == RunStatus.failed


async def test_brak_worktree_przy_wznowieniu_bramki_z_fanoutem_zamyka_wiersze_wariantow(tmp_path):
    s = swiat_bramki_fanoutu(tmp_path)
    await do_bramki(s)
    s.worktree.rename(s.worktree.with_name("worktree-przeniesiony"))   # bez usuwania plikow

    po = await wznow(s, AtrapaWariantow(s.znaczniki), RUN_ID, "analiza")

    assert koniec_wznowienia(po) == WIERSZE_BRAMKI_Z_FANOUTEM
    assert po[0]["text"].startswith("brak worktree runu")


async def test_uszkodzony_checkpoint_przy_wznowieniu_zostawia_jeden_wiersz_kroku_bez_proby(tmp_path):
    s = swiat_bramki_fanoutu(tmp_path)
    await do_bramki(s)
    (s.katalog_runu / "checkpoint.json").write_text("{nie json", encoding="utf-8")

    po = await wznow(s, AtrapaWariantow(s.znaczniki), RUN_ID, "analiza")

    assert koniec_wznowienia(po) == WIERSZ_BRAMKI_BEZ_PROBY


async def test_brak_kopii_workflow_przy_czytelnym_checkpoincie_zostawia_jeden_wiersz_bez_proby(
    tmp_path,
):
    s = swiat_bramki_fanoutu(tmp_path)
    await do_bramki(s)
    (s.katalog_runu / "workflow.yaml").rename(s.katalog_runu / "workflow-przeniesiony.yaml")

    po = await wznow(s, AtrapaWariantow(s.znaczniki), RUN_ID, "analiza")

    assert koniec_wznowienia(po) == WIERSZ_BRAMKI_BEZ_PROBY


async def test_uszkodzona_kopia_workflow_przy_czytelnym_checkpoincie_zostawia_jeden_wiersz_bez_proby(
    tmp_path,
):
    s = swiat_bramki_fanoutu(tmp_path)
    await do_bramki(s)
    (s.katalog_runu / "workflow.yaml").write_text("steps: [", encoding="utf-8")

    po = await wznow(s, AtrapaWariantow(s.znaczniki), RUN_ID, "analiza")

    assert koniec_wznowienia(po) == WIERSZ_BRAMKI_BEZ_PROBY


async def test_checkpoint_bez_wpisu_kroku_bramki_zostawia_jeden_wiersz_bez_proby(tmp_path):
    s = swiat_bramki_fanoutu(tmp_path)
    await do_bramki(s)
    cp = wczytaj_checkpoint(s.katalog_runu)
    zapisz_checkpoint(s.katalog_runu, replace(
        cp, kroki={k: v for k, v in cp.kroki.items() if k != "analiza"}))

    po = await wznow(s, AtrapaWariantow(s.znaczniki), RUN_ID, "analiza")

    assert koniec_wznowienia(po) == WIERSZ_BRAMKI_BEZ_PROBY


async def test_kopia_workflow_bez_kroku_bramki_przy_czytelnym_checkpoincie_zostawia_jeden_wiersz(
    tmp_path,
):
    s = swiat_bramki_fanoutu(tmp_path)
    await do_bramki(s)
    (s.katalog_runu / "workflow.yaml").write_text(
        "name: w\nsteps:\n  inny: {skill: s_inny}\n", encoding="utf-8")

    po = await wznow(s, AtrapaWariantow(s.znaczniki), RUN_ID, "analiza")

    assert koniec_wznowienia(po) == WIERSZ_BRAMKI_BEZ_PROBY


@pytest.mark.parametrize("wyjatek", [OSError, ValueError], ids=lambda w: w.__name__)
async def test_blad_odczytu_kopii_workflow_przy_wznowieniu_zostawia_jeden_wiersz_bez_proby(
    tmp_path, monkeypatch, wyjatek,
):
    s = swiat_bramki_fanoutu(tmp_path)
    await do_bramki(s)

    def nie_czyta(*argumenty, **opcje):
        raise wyjatek("workflow nieczytelny")

    monkeypatch.setattr(silnik, "wczytaj_workflow", nie_czyta)
    po = await wznow(s, AtrapaWariantow(s.znaczniki), RUN_ID, "analiza")

    assert koniec_wznowienia(po) == WIERSZ_BRAMKI_BEZ_PROBY
    assert po[0]["text"] == "workflow nieczytelny"


async def test_zly_identyfikator_runu_przy_wznowieniu_zostawia_jeden_wiersz_kroku_bez_proby(tmp_path):
    s = swiat_bramki_fanoutu(tmp_path)

    po = await wznow(s, AtrapaWariantow(s.znaczniki), "..", "analiza")

    assert koniec_wznowienia(po) == WIERSZ_BRAMKI_BEZ_PROBY
    assert "niedozwolony identyfikator runu" in po[0]["text"]


# --- uwagi krytyka: dwie galezie producentow i artefakty udanego wariantu ---------------------------


async def test_konsument_dostaje_nazwe_gdy_blizszy_producent_nie_ma_fanoutu_a_dalszy_na_innej_galezi_ma(
    tmp_path,
):
    s = swiat(
        tmp_path,
        [K("analiza", extra=", fanout: 3"), K("posredni", ("analiza",)), K("blizszy"),
         K("konsument", ("blizszy", "posredni"))],
        opcje_skilli={
            "analiza": {"produces": ["plan.md"]},
            "posredni": {"consumes": ["plan.md"]},
            "blizszy": {"produces": ["plan.md"]},
            "konsument": {"consumes": ["plan.md"]},
        })
    agent = AtrapaWariantow(s.znaczniki)

    await uruchom(s, agent, limit=LIMIT_S)

    assert agent.wejscia("konsument") == ["plan.md"]
    assert len(agent.wejscia("posredni")) == 3               # on sam widzi trzy warianty


async def test_udany_wariant_niesie_artefakty_w_step_finished_gdy_inny_wariant_pada(tmp_path):
    s = swiat_fanoutu(tmp_path, 3)
    agent = AtrapaWariantow(s.znaczniki, bariera=3, kody={"a2": 3})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    konce = {z["agent_id"]: z for z in zdarzenia if z["step_id"] == "analiza"
             and z["kind"] in ("step_finished", "step_failed")}
    assert konce["a1"]["kind"] == konce["a3"]["kind"] == "step_finished"
    assert konce["a1"]["artefakty"] == konce["a3"]["artefakty"] == ["plan.md"]
    assert konce["a2"]["kind"] == "step_failed"
    assert "artefakty" not in konce["a2"]

