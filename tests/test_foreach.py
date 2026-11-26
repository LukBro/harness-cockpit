"""HAR-98: krok z `foreach: <artefakt>#<pole>` ma tyle procesow, ile pozycji ma lista z artefaktu (TDD).

Prawdziwy kod: `runner.silnik.wykonaj_run` i `wznow_run` na prawdziwym worktree, workflow, skillach i
walidatorze. Zaslepiony jest tylko agent (`claude`): atrapa zapisuje artefakty kroku tam, gdzie zapisalby
je agent albo runner (rola z zapisem w worktree, rola bez zapisu w katalogu runu; krok producenta `plan`
zapisuje zadana tresc `plan.json`), a przy kroku z foreach czyta plik `element.json` ze sciezki z
`spec.wejscia` w chwili wywolania, wiec silnik musi zapisac go przed startem agenta. W trybie procesow
(`znaczniki`) agenci inni niz producent to prawdziwe procesy przez `wykonaj_krok`.
Asercje czytaja dane: pliki w worktree i katalogu runu, zdarzenia runu i wiersze w bazie kokpitu.
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).

Liczbe procesow kroku silnik utrwala w `<katalog_runu>/<krok>/foreach.json` ({"liczba": N}); wiersze
`a1..aN` otwarte przez `step_waiting`, krok po decyzji bramki i konsument korzystaja z tego N.
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from cockpit.db import session as db_session
from cockpit.db.models import Run, RunStatus, StepRun, StepStatus
from cockpit.services.errors import BladDziedziny
from cockpit.services.invariants import profil_roli
from runner import silnik
from runner.agent import podkatalog_wariantu, sciezka_wyjscia_wariantu
from runner.checkpoint import StanKrokuCheckpointu, wczytaj_checkpoint, zapisz_checkpoint
from runner.krok import WynikKroku, wykonaj_krok
from test_fanout import KOD_WARIANTU, Baza, wyslij_do_kokpitu, wznow
from test_fanout import baza as baza_z_fanoutu
from test_silnik_runu import RUN_ID, K, jedno, rodzaje, swiat, uruchom

baza = baza_z_fanoutu   # fixture bazy z tests/test_fanout.py pod ta sama nazwa

LIMIT_S = 60
BARIERA_S = 10

SPEC = "plan.json#zadania"
SCIEZKA_PLANU = "plan.json"

ELEMENTY_3 = [
    {"id": 1, "opis": "unikalny-opis-1"},
    {"id": 2, "opis": "unikalny-opis-2"},
    {"id": 3, "opis": "unikalny-opis-3"},
]

TEKST_NOTATKA = "foreach z notatką zmiany nieobsługiwany"

KATALOG = object()   # znacznik: zamiast pliku powstaje katalog o tej nazwie


def plan(elementy: Any, pole: str = "zadania") -> str:
    return json.dumps({pole: elementy})


def sciezka_elementu(krok: str, agent_id: str) -> str:
    return sciezka_wyjscia_wariantu(krok, agent_id, "element.json")


def blad_zrodla(tresc: str) -> str:
    return f"foreach {SPEC}: {tresc}"


@dataclass
class AtrapaForeach:
    """Agent bez procesu (chyba ze `znaczniki`). Producent (`plany`) zapisuje kolejne tresci planu, kazdy
    inny agent zapisuje swoje `produces`, a gdy dostal `element.json`, czyta go od razu."""

    plany: dict[str, list[Any]] = field(default_factory=dict)   # krok producenta -> tresci per wywolanie
    bariera: int = 0                                            # tylu agentow musi zyc naraz
    porazki: set[tuple[str, str, int]] = field(default_factory=set)   # (krok, agent, nr wywolania)
    zablokuj_zapis_elementow: bool = False                      # producent kladzie plik w miejscu katalogu
    po_kroku: dict[str, Callable[[Path, Path], None]] = field(default_factory=dict)  # (worktree, katalog runu)
    spanie: dict[str, float] = field(default_factory=dict)      # tylko tryb procesow
    znaczniki: Path | None = None                               # tryb procesow: katalog znacznikow
    wywolania: list = field(default_factory=list)                # (krok, agent, wejscia)
    elementy: dict[tuple[str, str, int], tuple[str, Any]] = field(default_factory=dict)
    prompty: dict[tuple[str, str], str] = field(default_factory=dict)
    _numery: dict[tuple[str, str], int] = field(default_factory=dict)
    _wystartowali: int = 0
    _wszyscy: asyncio.Event = field(default_factory=asyncio.Event)

    async def __call__(
        self, spec, polecenie, katalog, emit, *, katalog_runu=None, prompt=None, **dodatkowe
    ):
        klucz = (spec.step_id, spec.agent_id)
        nr = self._numery[klucz] = self._numery.get(klucz, 0) + 1
        self.wywolania.append((spec.step_id, spec.agent_id, list(spec.wejscia)))
        self.prompty[klucz] = prompt or ""
        korzen, runu = Path(katalog), Path(katalog_runu)
        zapis = profil_roli(spec.rola).zapis
        elementy = [w for w in spec.wejscia if w.endswith("element.json")]
        for wejscie in elementy:
            self.elementy[(*klucz, nr)] = (
                wejscie, json.loads((korzen / wejscie).read_text(encoding="utf-8")))
        if spec.step_id in self.plany:
            tresci = self.plany[spec.step_id]
            podkatalog = podkatalog_wariantu(spec.agent_id) if spec.wariant else ""
            cel = (korzen / spec.wyjscia[0] if zapis
                   else runu / spec.step_id / podkatalog / spec.wyjscia[0])
            self._zapisz_plan(korzen, cel, tresci[min(nr, len(tresci)) - 1])
        elif self.znaczniki is not None:
            opis = {
                "koszt": None, "znaczniki": str(self.znaczniki), "agent_id": spec.agent_id,
                "bariera": 0, "limit_bariery": BARIERA_S,
                "spanie": self.spanie.get(spec.step_id, 0), "kod": 0,
                "zapisz": list(spec.wyjscia) if zapis else [],
            }
            argv = [sys.executable, "-I", "-c", KOD_WARIANTU, json.dumps(opis)]
            return await wykonaj_krok(
                spec, argv, katalog, emit, katalog_runu=katalog_runu, prompt=prompt, **dodatkowe)
        else:
            podkatalog = podkatalog_wariantu(spec.agent_id) if spec.wariant else ""
            for wyjscie in spec.wyjscia:
                cel = korzen / wyjscie if zapis else runu / spec.step_id / podkatalog / wyjscie
                cel.parent.mkdir(parents=True, exist_ok=True)
                cel.write_text("wynik", encoding="utf-8")
        if self.bariera and elementy:
            self._wystartowali += 1
            if self._wystartowali >= self.bariera:
                self._wszyscy.set()
            await asyncio.wait_for(self._wszyscy.wait(), BARIERA_S)
        if spec.step_id in self.po_kroku:
            self.po_kroku[spec.step_id](korzen, runu)
        if (spec.step_id, spec.agent_id, nr) in self.porazki:
            return WynikKroku(status="nieudany", powod="atrapa: porażka", kod_wyjscia=3)
        return WynikKroku(status="zakonczony", powod=None, kod_wyjscia=0)

    def _zapisz_plan(self, korzen: Path, cel: Path, tresc: Any) -> None:
        cel.parent.mkdir(parents=True, exist_ok=True)
        if tresc is KATALOG:
            cel.mkdir()
        elif isinstance(tresc, bytes):
            cel.write_bytes(tresc)
        elif tresc is not None:
            cel.write_text(tresc, encoding="utf-8")
        if self.zablokuj_zapis_elementow:
            blokada = korzen / ".cockpit" / "_run" / "artifacts" / "rozdziel"
            blokada.parent.mkdir(parents=True, exist_ok=True)
            blokada.write_text("to jest plik, nie katalog", encoding="utf-8")

    def agenci(self, krok: str) -> list[str]:
        return sorted(a for s, a, _ in self.wywolania if s == krok)

    def wejscia(self, krok: str) -> list[str]:
        (wejscia,) = [w for s, _, w in self.wywolania if s == krok]
        return wejscia

    def element(self, krok: str, agent_id: str, nr: int = 1) -> Any:
        return self.elementy[(krok, agent_id, nr)][1]

    def sciezka_elementu(self, krok: str, agent_id: str, nr: int = 1) -> str:
        return self.elementy[(krok, agent_id, nr)][0]


def swiat_foreach(
    tmp_path, *, foreach: str = SPEC, dopisek: str = "", dopisek_planu: str = "",
    produces_rozdziel: tuple[str, ...] = ("wynik.md",), zbierz: bool = False,
    sprawdz: bool = False, rola_planu: str = "author", rola_rozdziel: str = "author",
    rola_zbierz: str = "author", rola_sprawdz: str = "author", sprawdz_po_planie: bool = True,
):
    """`plan` (producent `plan.json`) -> `rozdziel` z foreach; opcjonalnie konsument `zbierz` wyniku
    `rozdziel` i niezalezny od `rozdziel` krok `sprawdz` (czeka tylko na `plan`)."""
    kroki = [
        K("plan", extra=dopisek_planu),
        K("rozdziel", ("plan",), extra=f', foreach: "{foreach}"{dopisek}'),
    ]
    opcje: dict[str, dict] = {
        "plan": {"produces": [SCIEZKA_PLANU], "role": rola_planu},
        "rozdziel": {"produces": list(produces_rozdziel), "role": rola_rozdziel},
    }
    if zbierz:
        kroki.append(K("zbierz", ("rozdziel",)))
        opcje["zbierz"] = {"consumes": ["wynik.md"], "role": rola_zbierz}
    if sprawdz:
        kroki.append(K("sprawdz", ("plan",) if sprawdz_po_planie else ()))
        opcje["sprawdz"] = {"role": rola_sprawdz}
    return swiat(tmp_path, kroki, opcje_skilli=opcje)


def plik_liczby(s, run_id: str = RUN_ID) -> Path:
    return s.root / run_id / "rozdziel" / "foreach.json"


def worktree_runu(s, run_id: str) -> Path:
    return s.root / run_id / "worktree"


def zdarzenia_kroku(zdarzenia: list[dict], rodzaj: str, krok: str) -> list[dict]:
    return [z for z in zdarzenia if z["kind"] == rodzaj and z["step_id"] == krok]


def agenci_zdarzen(zdarzenia: list[dict], rodzaj: str, krok: str, proba: int | None = None):
    return sorted(z["agent_id"] for z in zdarzenia_kroku(zdarzenia, rodzaj, krok)
                  if proba is None or z["attempt"] == proba)


def kazdy_start_ma_koniec(zdarzenia: list[dict]) -> None:
    """Kazdy wiersz otwarty przez `step_started` albo `step_waiting` ma pozniej koniec tego samego
    (krok, agent): `step_finished` albo `step_failed`. Tylko dla przebiegow doprowadzonych do konca."""
    for i, z in enumerate(zdarzenia):
        if z["kind"] in ("step_started", "step_waiting"):
            konce = [k for k in zdarzenia[i + 1:]
                     if k["kind"] in ("step_finished", "step_failed")
                     and (k["step_id"], k["agent_id"]) == (z["step_id"], z["agent_id"])]
            assert konce, f"wiersz {z['kind']} {z['step_id']}/{z['agent_id']} nie ma konca"


def sprawdz_porazke_kroku(zdarzenia: list[dict], agent: AtrapaForeach, tekst: str) -> None:
    nieudane = zdarzenia_kroku(zdarzenia, "step_failed", "rozdziel")
    assert [(z["agent_id"], z["text"]) for z in nieudane] == [("rozdziel", tekst)]
    assert agent.agenci("rozdziel") == []                    # zaden agent nie wystartowal
    assert zdarzenia_kroku(zdarzenia, "step_finished", "rozdziel") == []
    assert "step_waiting" not in rodzaje(zdarzenia)
    assert [z["step_id"] for z in zdarzenia if z["kind"] == "step_failed"
            and z["step_id"] != "rozdziel"] == []
    kazdy_start_ma_koniec(zdarzenia)
    assert rodzaje(zdarzenia).count("run_finished") == 1
    assert zdarzenia[-1]["kind"] == "run_finished"


# --- k1: trzy pozycje, trzy procesy z roznymi elementami -----------------------------------------


async def test_foreach_z_trzema_pozycjami_uruchamia_trzy_rownolegle_procesy_agenta(tmp_path):
    s = swiat_foreach(tmp_path)
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]}, bariera=3)

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert agent.agenci("rozdziel") == ["a1", "a2", "a3"]
    assert "step_failed" not in rodzaje(zdarzenia)           # bariera nie padla: zyly naraz
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_foreach_z_trzema_pozycjami_emituje_start_i_koniec_osobno_dla_kazdego_agent_id(tmp_path):
    s = swiat_foreach(tmp_path)
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    for rodzaj in ("step_started", "step_finished", "context_manifest"):
        assert agenci_zdarzen(zdarzenia, rodzaj, "rozdziel") == ["a1", "a2", "a3"], rodzaj


async def test_liczba_procesow_foreach_jest_utrwalona_w_katalogu_runu_poza_repozytorium(tmp_path):
    s = swiat_foreach(tmp_path)
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})

    await uruchom(s, agent, limit=LIMIT_S)

    assert json.loads(plik_liczby(s).read_text(encoding="utf-8")) == {"liczba": 3}
    assert not (s.worktree / "foreach.json").exists()
    assert not list(s.worktree.rglob("foreach.json"))


async def test_kazdy_proces_foreach_dostaje_element_ze_swojej_pozycji_listy(tmp_path):
    s = swiat_foreach(tmp_path)
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]}, bariera=3)

    await uruchom(s, agent, limit=LIMIT_S)

    assert [agent.element("rozdziel", a) for a in ("a1", "a2", "a3")] == ELEMENTY_3


async def test_element_trafia_do_pliku_element_json_w_katalogu_wariantu_agenta(tmp_path):
    s = swiat_foreach(tmp_path)
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})

    await uruchom(s, agent, limit=LIMIT_S)

    for numer, agent_id in enumerate(("a1", "a2", "a3")):
        oczekiwana = f".cockpit/_run/artifacts/rozdziel/agent_{agent_id}/element.json"
        assert agent.sciezka_elementu("rozdziel", agent_id) == oczekiwana
        zapisany = json.loads((s.worktree / oczekiwana).read_text(encoding="utf-8"))
        assert zapisany == ELEMENTY_3[numer]


async def test_sciezka_elementu_jest_wsrod_wejsc_agenta_i_nie_ma_jej_u_agentow_bez_foreach(tmp_path):
    s = swiat_foreach(tmp_path, sprawdz=True)
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})

    await uruchom(s, agent, limit=LIMIT_S)

    for agent_id in ("a1", "a2", "a3"):
        wejscia = [w for s_, a, w in agent.wywolania if s_ == "rozdziel" and a == agent_id][0]
        assert sciezka_elementu("rozdziel", agent_id) in wejscia
    assert agent.wejscia("sprawdz") == []
    assert agent.elementy.keys() == {("rozdziel", a, 1) for a in ("a1", "a2", "a3")}


async def test_prompt_wariantu_foreach_niesie_sciezke_jego_elementu_w_sekcji_wejscie_a_nie_tresc(
    tmp_path,
):
    s = swiat_foreach(tmp_path)
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})

    await uruchom(s, agent, limit=LIMIT_S)

    for agent_id, inny in (("a1", "a2"), ("a2", "a3"), ("a3", "a1")):
        prompt = agent.prompty[("rozdziel", agent_id)]
        sekcja = prompt[prompt.index("wejście:"):]
        assert f"- {sciezka_elementu('rozdziel', agent_id)}" in sekcja
        assert f"agent_{inny}/element.json" not in prompt
        assert "unikalny-opis" not in prompt                 # sciezka, nigdy tresc elementu


@pytest.mark.parametrize(
    "element",
    [{"id": 1, "opis": "zażółć gęślą"}, "sam tekst", 7, 2.5, [1, [2]], None, True],
    ids=["obiekt", "napis", "liczba_calkowita", "liczba_zmiennoprzecinkowa", "lista", "null", "bool"],
)
async def test_element_dowolnego_typu_json_trafia_do_pliku_bez_zmian(tmp_path, element):
    s = swiat_foreach(tmp_path)
    agent = AtrapaForeach(plany={"plan": [plan([element, "drugi"])]})

    await uruchom(s, agent, limit=LIMIT_S)

    assert agent.element("rozdziel", "a1") == element
    assert type(agent.element("rozdziel", "a1")) is type(element)
    assert agent.element("rozdziel", "a2") == "drugi"


async def test_element_z_samotnym_surrogatem_utf16_trafia_do_pliku_bez_zmian_a_krok_sie_udaje(
    tmp_path,
):
    s = swiat_foreach(tmp_path)
    agent = AtrapaForeach(plany={"plan": ['{"zadania": ["\\ud800", "zwykly"]}']})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert agent.element("rozdziel", "a1") == "\ud800"
    zapisany = (s.worktree / sciezka_elementu("rozdziel", "a1")).read_bytes()
    assert zapisany == b'"\\ud800"'
    assert agenci_zdarzen(zdarzenia, "step_finished", "rozdziel") == ["a1", "a2"]
    assert "step_failed" not in rodzaje(zdarzenia)


async def test_dwie_jednakowe_pozycje_listy_daja_dwa_procesy(tmp_path):
    s = swiat_foreach(tmp_path)
    agent = AtrapaForeach(plany={"plan": [plan(["to samo", "to samo"])]}, bariera=2)

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert agent.agenci("rozdziel") == ["a1", "a2"]
    assert "step_failed" not in rodzaje(zdarzenia)


async def test_pole_foreach_z_hashem_w_nazwie_jest_czytane_od_pierwszego_hasha(tmp_path):
    s = swiat_foreach(tmp_path, foreach="plan.json#x#y")
    agent = AtrapaForeach(plany={"plan": [plan(["jeden", "dwa"], pole="x#y")]})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert agent.agenci("rozdziel") == ["a1", "a2"]
    assert "step_failed" not in rodzaje(zdarzenia)


# --- jedna pozycja: nadal sciezka wariantow, nie zwykly krok ------------------------------------


async def test_foreach_z_jedna_pozycja_to_jeden_wariant_a1_z_elementem_w_katalogu_wariantu(tmp_path):
    s = swiat_foreach(tmp_path)
    agent = AtrapaForeach(plany={"plan": [plan([ELEMENTY_3[0]])]})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert agent.agenci("rozdziel") == ["a1"]
    assert agenci_zdarzen(zdarzenia, "step_started", "rozdziel") == ["a1"]
    assert agenci_zdarzen(zdarzenia, "step_finished", "rozdziel") == ["a1"]
    assert agent.sciezka_elementu("rozdziel", "a1") == sciezka_elementu("rozdziel", "a1")
    assert agent.element("rozdziel", "a1") == ELEMENTY_3[0]
    assert not (s.worktree / "element.json").exists()
    assert json.loads(plik_liczby(s).read_text(encoding="utf-8")) == {"liczba": 1}


# --- konsument kroku z foreach dostaje sciezki wszystkich wariantow ------------------------------


async def test_konsument_kroku_z_foreach_dostaje_sciezki_wynikow_wszystkich_wariantow(tmp_path):
    s = swiat_foreach(tmp_path, zbierz=True)
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    wejscia = agent.wejscia("zbierz")
    assert sorted(wejscia) == [
        f".cockpit/_run/artifacts/rozdziel/agent_{a}/wynik.md" for a in ("a1", "a2", "a3")]
    for sciezka in wejscia:
        assert (s.worktree / sciezka).read_text() == "wynik"   # sciezka wskazuje prawdziwy plik
    assert "step_failed" not in rodzaje(zdarzenia)


async def test_konsument_kroku_z_foreach_o_jednej_pozycji_dostaje_sciezke_wariantu_a_nie_nazwe(tmp_path):
    s = swiat_foreach(tmp_path, zbierz=True)
    agent = AtrapaForeach(plany={"plan": [plan(["jedyny"])]})

    await uruchom(s, agent, limit=LIMIT_S)

    assert agent.wejscia("zbierz") == [".cockpit/_run/artifacts/rozdziel/agent_a1/wynik.md"]


async def test_lista_oproznona_przez_agenta_foreach_nie_zmienia_wejsc_konsumenta_bo_liczba_jest_utrwalona(
    tmp_path,
):
    s = swiat_foreach(tmp_path, zbierz=True)

    def oprozni_liste(korzen: Path, runu: Path) -> None:
        (korzen / SCIEZKA_PLANU).write_text(plan([]), encoding="utf-8")

    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]}, po_kroku={"rozdziel": oprozni_liste})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert sorted(agent.wejscia("zbierz")) == [
        f".cockpit/_run/artifacts/rozdziel/agent_{a}/wynik.md" for a in ("a1", "a2", "a3")]
    assert "step_failed" not in rodzaje(zdarzenia)
    assert zdarzenia[-1]["kind"] == "run_finished"


def zepsuj_plik(plik: Path, zawartosc: Any) -> None:
    """Zamienia plik na brak (`None`), katalog (`KATALOG`), bajty albo tekst."""
    if plik.is_dir():
        plik.rmdir()
    plik.unlink(missing_ok=True)
    if zawartosc is KATALOG:
        plik.mkdir()
    elif isinstance(zawartosc, bytes):
        plik.write_bytes(zawartosc)
    elif zawartosc is not None:
        plik.write_text(zawartosc, encoding="utf-8")


NIEPOPRAWNA = "niepoprawna liczba wierszy kroku rozdziel w foreach.json"


def nie_mozna_odczytac(typ: str) -> str:
    return f"nie można odczytać liczby wierszy kroku rozdziel ({typ})"


# (zawartosc foreach.json, tekst bledu z foreach.json)
ZEPSUTE_LICZBY = [
    pytest.param(None, nie_mozna_odczytac("FileNotFoundError"), id="brak_pliku"),
    pytest.param(KATALOG, nie_mozna_odczytac("IsADirectoryError"), id="katalog_zamiast_pliku"),
    pytest.param(b"\xff\xfe", nie_mozna_odczytac("UnicodeDecodeError"), id="plik_nie_w_utf8"),
    pytest.param("{nie json", nie_mozna_odczytac("JSONDecodeError"), id="zly_json"),
    pytest.param("[" * 200000, nie_mozna_odczytac("RecursionError"), id="zbyt_gleboki_json"),
    pytest.param("{}", NIEPOPRAWNA, id="brak_klucza_liczba"),
    pytest.param("[3]", NIEPOPRAWNA, id="korzen_nie_jest_obiektem"),
    pytest.param('{"liczba": "3"}', NIEPOPRAWNA, id="liczba_jest_napisem"),
    pytest.param('{"liczba": 3.0}', NIEPOPRAWNA, id="liczba_jest_zmiennoprzecinkowa"),
    pytest.param('{"liczba": true}', NIEPOPRAWNA, id="liczba_jest_bool"),
    pytest.param('{"liczba": 0}', NIEPOPRAWNA, id="liczba_zero"),
    pytest.param('{"liczba": -1}', NIEPOPRAWNA, id="liczba_ujemna"),
]


@pytest.mark.parametrize(("zawartosc", "tekst"), ZEPSUTE_LICZBY)
async def test_konsument_kroku_foreach_z_zepsutym_plikiem_liczby_konczy_sie_porazka_z_powodem(
    tmp_path, zawartosc, tekst,
):
    s = swiat_foreach(tmp_path, zbierz=True)

    def zepsuj(korzen: Path, runu: Path) -> None:
        zepsuj_plik(runu / "rozdziel" / "foreach.json", zawartosc)

    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]}, po_kroku={"rozdziel": zepsuj})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    nieudane = zdarzenia_kroku(zdarzenia, "step_failed", "zbierz")
    assert [(z["agent_id"], z["text"]) for z in nieudane] == [("zbierz", blad_zrodla(tekst))]
    assert agent.agenci("zbierz") == []
    kazdy_start_ma_koniec(zdarzenia)
    assert zdarzenia[-1]["kind"] == "run_finished"


# --- k2: pusta lista ----------------------------------------------------------------------------


async def test_pusta_lista_pomija_krok_z_powodem_pusty_foreach_i_nazwa_zrodla(tmp_path):
    s = swiat_foreach(tmp_path)
    agent = AtrapaForeach(plany={"plan": [plan([])]})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    pominiety = jedno(zdarzenia, "step_skipped", "rozdziel")
    assert pominiety["text"] == f"pusty_foreach: pusta lista {SPEC}"
    assert pominiety["attempt"] == 1
    assert zdarzenia[-1]["kind"] == "run_finished"
    assert rodzaje(zdarzenia).count("run_finished") == 1


async def test_pusta_lista_w_drugiej_probie_kroku_ma_w_pominieciu_numer_proby_2(tmp_path):
    s = swiat_foreach(tmp_path, dopisek=", on_fail: {retry: plan, max: 1}")
    agent = AtrapaForeach(
        plany={"plan": [plan(ELEMENTY_3), plan([])]}, porazki={("rozdziel", "a1", 1)})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    pominiety = jedno(zdarzenia, "step_skipped", "rozdziel")
    assert pominiety["text"] == f"pusty_foreach: pusta lista {SPEC}"
    assert pominiety["attempt"] == 2
    assert agent.agenci("rozdziel") == ["a1", "a2", "a3"]       # tylko 1. proba
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_pusta_lista_nie_uruchamia_agenta_i_nie_otwiera_wiersza_kroku(tmp_path):
    s = swiat_foreach(tmp_path)
    agent = AtrapaForeach(plany={"plan": [plan([])]})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert agent.agenci("rozdziel") == []
    for rodzaj in ("step_started", "step_finished", "step_failed", "context_manifest"):
        assert zdarzenia_kroku(zdarzenia, rodzaj, "rozdziel") == [], rodzaj
    assert not plik_liczby(s).exists()                       # nie ma N do utrwalenia
    assert "step_failed" not in rodzaje(zdarzenia)


async def test_pusta_lista_nie_zatrzymuje_kroku_niezaleznego_od_kroku_z_foreach(tmp_path):
    s = swiat_foreach(tmp_path, sprawdz=True)
    agent = AtrapaForeach(plany={"plan": [plan([])]})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    jedno(zdarzenia, "step_skipped", "rozdziel")             # to pominiecie, nie zwykly przebieg
    assert agent.agenci("sprawdz") == ["sprawdz"]
    jedno(zdarzenia, "step_finished", "sprawdz")
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_pusta_lista_daje_konsumentowi_wyniku_foreach_pominiecie_z_powodem_brak_wejscia(tmp_path):
    s = swiat_foreach(tmp_path, zbierz=True)
    agent = AtrapaForeach(plany={"plan": [plan([])]})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    pominiety = jedno(zdarzenia, "step_skipped", "zbierz")
    assert pominiety["text"] == "brak_wejscia: brak wejścia: wynik.md z kroku rozdziel"
    assert agent.agenci("zbierz") == []
    assert "step_failed" not in rodzaje(zdarzenia)
    assert zdarzenia[-1]["kind"] == "run_finished"


# --- k3: zle wejscie to step_failed z powodem, nie wyjatek runnera --------------------------------


def _brak_pliku(sciezka: str) -> str:
    return blad_zrodla(f"nie można odczytać {sciezka} (FileNotFoundError)")


def _json(typ: str):
    return lambda sciezka: blad_zrodla(f"niepoprawny JSON w {sciezka} ({typ})")


def _nie_obiekt(sciezka: str) -> str:
    return blad_zrodla(f"{sciezka} nie jest obiektem JSON")


BLEDNE_ARTEFAKTY = [
    pytest.param(None, _brak_pliku, id="brak_pliku_artefaktu"),
    pytest.param(
        KATALOG, lambda p: blad_zrodla(f"nie można odczytać {p} (IsADirectoryError)"),
        id="katalog_zamiast_pliku"),
    pytest.param("", _json("JSONDecodeError"), id="pusty_plik"),
    pytest.param("{nie json", _json("JSONDecodeError"), id="zly_json"),
    pytest.param("[" * 200000, _json("RecursionError"), id="zbyt_gleboko_zagniezdzony_json"),
    pytest.param(
        b'{"zadania": ["\xff"]}', lambda p: blad_zrodla(f"{p} nie jest poprawnym UTF-8"),
        id="plik_nie_w_utf8"),
    pytest.param(
        '{"inne": [1, 2]}', lambda p: blad_zrodla(f"brak pola zadania w {p}"), id="brak_pola"),
    pytest.param("[1, 2]", _nie_obiekt, id="korzen_jest_lista_a_nie_obiektem"),
    pytest.param('["zadania"]', _nie_obiekt, id="korzen_jest_lista_zawierajaca_nazwe_pola"),
    pytest.param("null", _nie_obiekt, id="korzen_jest_null"),
    pytest.param("5", _nie_obiekt, id="korzen_jest_liczba"),
    pytest.param(
        '{"zadania": "abc"}', lambda p: blad_zrodla("pole zadania nie jest listą"),
        id="pole_jest_napisem"),
    pytest.param(
        '{"zadania": {"a": 1}}', lambda p: blad_zrodla("pole zadania nie jest listą"),
        id="pole_jest_obiektem"),
    pytest.param(
        '{"zadania": null}', lambda p: blad_zrodla("pole zadania nie jest listą"),
        id="pole_jest_null"),
    pytest.param(
        '{"zadania": 3}', lambda p: blad_zrodla("pole zadania nie jest listą"),
        id="pole_jest_liczba"),
    pytest.param(
        '{"zadania": true}', lambda p: blad_zrodla("pole zadania nie jest listą"),
        id="pole_jest_bool"),
]


@pytest.mark.parametrize(("tresc", "oczekiwany"), BLEDNE_ARTEFAKTY)
async def test_bledny_artefakt_zrodlowy_konczy_krok_foreach_porazka_z_powodem_a_run_sie_konczy(
    tmp_path, tresc, oczekiwany,
):
    s = swiat_foreach(tmp_path)
    agent = AtrapaForeach(plany={"plan": [tresc]})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    sprawdz_porazke_kroku(zdarzenia, agent, oczekiwany(str(s.worktree / SCIEZKA_PLANU)))
    assert not plik_liczby(s).exists()


async def test_zly_artefakt_foreach_pomija_konsumenta_z_powodem_brak_wejscia_a_niezalezny_dziala(
    tmp_path,
):
    s = swiat_foreach(tmp_path, zbierz=True, sprawdz=True)
    agent = AtrapaForeach(plany={"plan": ['{"inne": []}']})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert jedno(zdarzenia, "step_skipped", "zbierz")["text"] == (
        "brak_wejscia: brak wejścia: wynik.md z kroku rozdziel")
    assert agent.agenci("sprawdz") == ["sprawdz"]
    assert zdarzenia[-1]["kind"] == "run_finished"


@pytest.mark.parametrize(
    ("wyjatek", "tekst"),
    [(OSError("dysk niedostepny"), "dysk niedostepny"),
     (ValueError("zla wartosc"), "zla wartosc"),
     (KeyError("klucz"), "brak klucza 'klucz'"),
     (BladDziedziny("blad dziedziny"), "blad dziedziny")],
    ids=["OSError", "ValueError", "KeyError", "BladDziedziny"])
async def test_wyjatek_granicy_przy_czytaniu_zrodla_foreach_konczy_krok_a_nie_walidacje_runu(
    tmp_path, monkeypatch, wyjatek, tekst,
):
    s = swiat_foreach(tmp_path)
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})

    def zrodlo_nie_dziala(*argumenty, **opcje):
        raise wyjatek

    monkeypatch.setattr(silnik, "_zrodlo_foreach", zrodlo_nie_dziala)

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    sprawdz_porazke_kroku(zdarzenia, agent, tekst)
    assert zdarzenia_kroku(zdarzenia, "step_failed", silnik.KROK_WALIDACJI) == []
    jedno(zdarzenia, "step_finished", "plan")


async def test_zrodlo_z_bledem_wartosci_przy_odczycie_konczy_krok_porazka_z_nazwa_typu(
    tmp_path, monkeypatch,
):
    s = swiat_foreach(tmp_path)
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})
    prawdziwy = Path.read_bytes

    def read_bytes(self):
        if self.name == SCIEZKA_PLANU:
            raise ValueError("embedded null byte")
        return prawdziwy(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    sprawdz_porazke_kroku(zdarzenia, agent, blad_zrodla(
        f"nie można odczytać {s.worktree / SCIEZKA_PLANU} (ValueError)"))


async def test_blad_zapisu_elementu_na_dysku_konczy_krok_foreach_porazka_z_powodem(tmp_path):
    s = swiat_foreach(tmp_path)
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]}, zablokuj_zapis_elementow=True)

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    nieudane = zdarzenia_kroku(zdarzenia, "step_failed", "rozdziel")
    assert [z["text"] for z in nieudane] == [
        blad_zrodla("nie można zapisać elementu (NotADirectoryError)")] * 3
    assert agenci_zdarzen(zdarzenia, "step_failed", "rozdziel") == ["a1", "a2", "a3"]
    assert agent.agenci("rozdziel") == []
    kazdy_start_ma_koniec(zdarzenia)
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_blad_zapisu_pliku_liczby_konczy_krok_foreach_porazka_bez_startu_agentow(
    tmp_path, monkeypatch,
):
    s = swiat_foreach(tmp_path)
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})
    prawdziwy = silnik._zapisz_atomowo

    def zapis(sciezka, tresc, *argumenty, **opcje):
        if Path(sciezka).name == "foreach.json":
            raise PermissionError(13, "brak praw")
        return prawdziwy(sciezka, tresc, *argumenty, **opcje)

    monkeypatch.setattr(silnik, "_zapisz_atomowo", zapis)

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    sprawdz_porazke_kroku(
        zdarzenia, agent,
        blad_zrodla("nie można zapisać liczby wierszy kroku rozdziel (PermissionError)"))


async def test_foreach_na_artefakcie_producenta_z_fanout_bez_consensus_jest_porazka_niejednoznacznego_zrodla(
    tmp_path,
):
    s = swiat_foreach(tmp_path, dopisek_planu=", fanout: 2")
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    nieudane = zdarzenia_kroku(zdarzenia, "step_failed", "rozdziel")
    assert [z["text"] for z in nieudane] == [blad_zrodla(
        "niejednoznaczne źródło (2 wersji artefaktu plan.json bez consensus)")]
    assert agent.agenci("rozdziel") == []
    assert agent.agenci("plan") == ["a1", "a2"]
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_foreach_ze_skillem_z_notatka_zmiany_konczy_krok_przed_startem_procesow(tmp_path):
    s = swiat_foreach(tmp_path, produces_rozdziel=("notatka-zmiany.md",))
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3[:2])]})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    nieudane = zdarzenia_kroku(zdarzenia, "step_failed", "rozdziel")
    assert [(z["agent_id"], z["text"]) for z in nieudane] == [
        ("a1", TEKST_NOTATKA), ("a2", TEKST_NOTATKA)]
    assert agent.agenci("rozdziel") == []
    kazdy_start_ma_koniec(zdarzenia)
    assert zdarzenia[-1]["kind"] == "run_finished"


# --- role bez zapisu: gatherer jako producent, reviewer jako krok foreach i konsument -------------


async def test_zrodlo_foreach_od_producenta_bez_prawa_zapisu_jest_czytane_z_katalogu_runu(tmp_path):
    s = swiat_foreach(tmp_path, rola_planu="gatherer")
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert (s.katalog_runu / "plan" / SCIEZKA_PLANU).is_file()
    assert not (s.worktree / SCIEZKA_PLANU).exists()
    assert agent.agenci("rozdziel") == ["a1", "a2", "a3"]
    assert [agent.element("rozdziel", a) for a in ("a1", "a2", "a3")] == ELEMENTY_3
    assert "step_failed" not in rodzaje(zdarzenia)


async def test_krok_foreach_z_rola_bez_zapisu_trzyma_elementy_i_wyniki_w_katalogu_runu_a_konsument_je_czyta(
    tmp_path,
):
    s = swiat_foreach(tmp_path, rola_rozdziel="reviewer", zbierz=True, rola_zbierz="reviewer")
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    for numer, agent_id in enumerate(("a1", "a2", "a3")):
        plik = s.katalog_runu / "rozdziel" / f"agent_{agent_id}" / "element.json"
        assert agent.sciezka_elementu("rozdziel", agent_id) == str(plik)
        assert json.loads(plik.read_text(encoding="utf-8")) == ELEMENTY_3[numer]
    assert not (s.worktree / ".cockpit" / "_run" / "artifacts" / "rozdziel").exists()
    assert sorted(agent.wejscia("zbierz")) == [
        str(s.katalog_runu / "rozdziel" / f"agent_{a}" / "wynik.md") for a in ("a1", "a2", "a3")]
    assert "step_failed" not in rodzaje(zdarzenia)


async def test_krok_foreach_recenzenta_nie_robi_rownoleglemu_recenzentowi_falszywego_naruszenia_odczytu(
    tmp_path,
):
    # `sprawdz` startuje od razu (bez `needs`) i spi: jego migawka worktree powstaje przed startem
    # `rozdziel`, wiec kazdy zapis `rozdziel` w worktree bylby dla niego naruszeniem; plan bez zapisu
    # (gatherer) nie dotyka worktree
    s = swiat_foreach(
        tmp_path, rola_planu="gatherer", rola_rozdziel="reviewer", sprawdz=True,
        rola_sprawdz="reviewer", sprawdz_po_planie=False)
    znaczniki = tmp_path / "znaczniki_procesow"
    znaczniki.mkdir()
    agent = AtrapaForeach(
        plany={"plan": [plan(ELEMENTY_3)]}, znaczniki=znaczniki, spanie={"sprawdz": 2.0})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert "step_failed" not in rodzaje(zdarzenia)
    assert agenci_zdarzen(zdarzenia, "step_finished", "rozdziel") == ["a1", "a2", "a3"]
    jedno(zdarzenia, "step_finished", "sprawdz")
    for agent_id in ("a1", "a2", "a3"):
        assert (s.katalog_runu / "rozdziel" / f"agent_{agent_id}" / "element.json").is_file()
    assert zdarzenia[-1]["kind"] == "run_finished"


# --- liczba procesow jest liczona z dysku przy kazdym starcie kroku (nie przy kazdym obiegu) -------


async def test_ponowienie_kroku_foreach_liczy_procesy_od_nowa_z_nowej_wersji_artefaktu(tmp_path):
    s = swiat_foreach(tmp_path, dopisek=", on_fail: {retry: plan, max: 1}")
    nowe = [{"id": 10, "opis": "nowy-1"}, {"id": 11, "opis": "nowy-2"}]
    agent = AtrapaForeach(
        plany={"plan": [plan(ELEMENTY_3), plan(nowe)]}, porazki={("rozdziel", "a1", 1)})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert agenci_zdarzen(zdarzenia, "step_started", "rozdziel", proba=1) == ["a1", "a2", "a3"]
    assert agenci_zdarzen(zdarzenia, "step_started", "rozdziel", proba=2) == ["a1", "a2"]
    assert agenci_zdarzen(zdarzenia, "step_finished", "rozdziel", proba=2) == ["a1", "a2"]
    assert agent.element("rozdziel", "a1", 2) == nowe[0]
    assert agent.element("rozdziel", "a2", 2) == nowe[1]
    assert json.loads(plik_liczby(s).read_text(encoding="utf-8")) == {"liczba": 2}
    kazdy_start_ma_koniec(zdarzenia)
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_ponowienie_kroku_foreach_z_zepsuta_druga_wersja_planu_zamyka_wiersz_kroku_w_probie_2(
    tmp_path,
):
    s = swiat_foreach(tmp_path, dopisek=", on_fail: {retry: plan, max: 1}")
    agent = AtrapaForeach(
        plany={"plan": [plan(ELEMENTY_3), "{nie json"]}, porazki={("rozdziel", "a1", 1)})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    proba_2 = [(z["agent_id"], z["text"]) for z in zdarzenia
               if z["kind"] == "step_failed" and z["step_id"] == "rozdziel" and z["attempt"] == 2]
    assert proba_2 == [("rozdziel", "wyczerpany limit 1 ponowień kroku rozdziel")]
    assert agenci_zdarzen(zdarzenia, "step_started", "rozdziel", proba=2) == []
    assert agent.agenci("rozdziel") == ["a1", "a2", "a3"]       # tylko 1. proba
    kazdy_start_ma_koniec(zdarzenia)
    assert zdarzenia[-1]["kind"] == "run_finished"


# --- bramka (approval) na kroku foreach ----------------------------------------------------------


async def test_bramka_na_kroku_foreach_czeka_per_pozycja_a_po_decyzji_kazdy_proces_ma_swoj_element(
    tmp_path,
):
    s = swiat_foreach(tmp_path, dopisek=", approval: true")
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})

    przed = await uruchom(s, agent, limit=LIMIT_S)

    assert agenci_zdarzen(przed, "step_waiting", "rozdziel") == ["a1", "a2", "a3"]
    assert agent.agenci("rozdziel") == []                    # bramka stoi przed procesami
    assert json.loads(plik_liczby(s).read_text(encoding="utf-8")) == {"liczba": 3}
    po = await wznow(s, agent, RUN_ID, "rozdziel")

    assert agent.agenci("rozdziel") == ["a1", "a2", "a3"]
    assert [agent.element("rozdziel", a) for a in ("a1", "a2", "a3")] == ELEMENTY_3
    assert agenci_zdarzen(po, "step_finished", "rozdziel") == ["a1", "a2", "a3"]
    kazdy_start_ma_koniec(przed + po)
    assert po[-1]["kind"] == "run_finished"


async def test_bramka_foreach_zakonczona_sukcesem_zamyka_wiersze_w_bazie_a_run_jest_do_odbioru(
    tmp_path, baza,
):
    s = swiat_foreach(tmp_path, dopisek=", approval: true")
    polecenie = {**s.polecenie, "run_id": baza.run_id}
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})

    przed = await uruchom(s, agent, polecenie=polecenie, limit=LIMIT_S)
    po = await wznow(s, agent, baza.run_id, "rozdziel")
    await wyslij_do_kokpitu(baza, przed + po)

    wiersze, run = await wiersze_i_run(baza)
    assert wiersze == [
        ("plan", "plan", 1, StepStatus.finished),
        *[("rozdziel", a, 1, StepStatus.finished) for a in ("a1", "a2", "a3")]]
    assert run.status == RunStatus.to_accept
    kazdy_start_ma_koniec(przed + po)


async def wiersze_i_run(baza: Baza) -> tuple[list[tuple], Run]:
    async with db_session.SessionLocal() as sesja:
        wiersze = (await sesja.execute(
            select(StepRun).where(StepRun.run_id == baza.run_id))).scalars().all()
        run = (await sesja.execute(select(Run).where(Run.id == baza.run_id))).scalar_one()
    return sorted((w.step_id, w.agent_id, w.attempt, w.status) for w in wiersze), run


@pytest.mark.parametrize(
    "nowa_lista", [ELEMENTY_3[:2], ELEMENTY_3 + [{"id": 4}], []],
    ids=["lista_krotsza", "lista_dluzsza", "lista_pusta"])
async def test_lista_zmieniona_po_bramce_foreach_konczy_krok_porazka_i_zamyka_wiersze_utrwalonej_liczby(
    tmp_path, baza, nowa_lista,
):
    s = swiat_foreach(tmp_path, dopisek=", approval: true")
    polecenie = {**s.polecenie, "run_id": baza.run_id}
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})

    przed = await uruchom(s, agent, polecenie=polecenie, limit=LIMIT_S)
    (worktree_runu(s, baza.run_id) / SCIEZKA_PLANU).write_text(plan(nowa_lista), encoding="utf-8")
    po = await wznow(s, agent, baza.run_id, "rozdziel")
    await wyslij_do_kokpitu(baza, przed + po)

    tekst = blad_zrodla(
        f"lista zmieniła się po bramce: było 3 pozycji, jest {len(nowa_lista)}")
    assert [(z["agent_id"], z["text"]) for z in zdarzenia_kroku(po, "step_failed", "rozdziel")] == [
        (a, tekst) for a in ("a1", "a2", "a3")]
    assert zdarzenia_kroku(po, "step_skipped", "rozdziel") == []
    assert agent.agenci("rozdziel") == []
    wiersze, run = await wiersze_i_run(baza)
    assert [w for w in wiersze if w[0] == "rozdziel"] == [
        ("rozdziel", a, 1, StepStatus.failed) for a in ("a1", "a2", "a3")]
    assert run.status == RunStatus.failed
    kazdy_start_ma_koniec(przed + po)
    assert po[-1]["kind"] == "run_finished"


async def test_zrodlo_zepsute_po_bramce_foreach_zamyka_wiersze_otwarte_przez_step_waiting(
    tmp_path, baza,
):
    s = swiat_foreach(tmp_path, dopisek=", approval: true")
    polecenie = {**s.polecenie, "run_id": baza.run_id}
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})

    przed = await uruchom(s, agent, polecenie=polecenie, limit=LIMIT_S)
    (worktree_runu(s, baza.run_id) / SCIEZKA_PLANU).write_text("{nie json", encoding="utf-8")
    po = await wznow(s, agent, baza.run_id, "rozdziel")
    await wyslij_do_kokpitu(baza, przed + po)

    assert agent.agenci("rozdziel") == []
    wiersze, run = await wiersze_i_run(baza)
    assert {w[3] for w in wiersze if w[0] == "rozdziel"} == {StepStatus.failed}, wiersze
    assert run.status == RunStatus.failed
    kazdy_start_ma_koniec(przed + po)


async def test_zepsute_zrodlo_przed_bramka_foreach_konczy_krok_bez_step_waiting(tmp_path):
    s = swiat_foreach(tmp_path, dopisek=", approval: true")
    agent = AtrapaForeach(plany={"plan": ['{"inne": []}']})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    sprawdz_porazke_kroku(
        zdarzenia, agent, blad_zrodla(f"brak pola zadania w {s.worktree / SCIEZKA_PLANU}"))


async def test_nieudany_zapis_checkpointu_na_bramce_foreach_daje_step_failed_dla_kazdej_pozycji(
    tmp_path, monkeypatch,
):
    from test_bramka_zatwierdzenia import PREFIKS_BLEDU_ZAPISU, _zepsuj_zapis_checkpointu

    s = swiat_foreach(tmp_path, dopisek=", approval: true")
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})
    _zepsuj_zapis_checkpointu(monkeypatch, od_zapisu=1)

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    nieudane = zdarzenia_kroku(zdarzenia, "step_failed", "rozdziel")
    assert [z["agent_id"] for z in nieudane] == ["a1", "a2", "a3"]
    assert all(z["text"].startswith(PREFIKS_BLEDU_ZAPISU) and "dysk pelny" in z["text"]
               for z in nieudane)
    assert "step_waiting" not in rodzaje(zdarzenia)
    assert rodzaje(zdarzenia).count("run_finished") == 1
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_wyjatek_planu_po_wznowieniu_bramki_foreach_zamyka_wiersze_pozycji_z_proba_bramki(
    tmp_path, monkeypatch, baza,
):
    s = swiat_foreach(tmp_path, dopisek=", approval: true")
    polecenie = {**s.polecenie, "run_id": baza.run_id}
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})
    przed = await uruchom(s, agent, polecenie=polecenie, limit=LIMIT_S)

    def plan_nie_dziala(*argumenty, **opcje):
        raise OSError("plan niedostepny")

    monkeypatch.setattr(silnik, "nastepne", plan_nie_dziala)
    po = await wznow(s, agent, baza.run_id, "rozdziel")
    await wyslij_do_kokpitu(baza, przed + po)

    assert [(z["agent_id"], z["attempt"], z["text"]) for z in zdarzenia_kroku(
        po, "step_failed", "rozdziel")] == [(a, 1, "plan niedostepny") for a in ("a1", "a2", "a3")]
    wiersze, run = await wiersze_i_run(baza)
    assert [w for w in wiersze if w[0] == "rozdziel"] == [
        ("rozdziel", a, 1, StepStatus.failed) for a in ("a1", "a2", "a3")]
    assert run.status == RunStatus.failed


# --- wiersze kroku foreach przy wznowieniu: N z utrwalonego pliku, nigdy zgadywane ----------------


def swiat_przerwanego_foreach(tmp_path):
    """`rozdziel` (foreach) konczy sie, `zatwierdz` czeka na bramce; krok `koniec` za bramka."""
    return swiat(
        tmp_path,
        [K("plan"), K("rozdziel", ("plan",), extra=f', foreach: "{SPEC}"'),
         K("zatwierdz", extra=", approval: true"), K("koniec", ("zatwierdz",))],
        opcje_skilli={"plan": {"produces": [SCIEZKA_PLANU]}, "rozdziel": {"produces": ["wynik.md"]}})


async def wznow_z_przerwanym_foreach(s, agent, przygotuj=None) -> list[dict]:
    przed = await uruchom(s, agent, limit=LIMIT_S)
    assert przed[-1]["kind"] == "step_waiting"
    cp = wczytaj_checkpoint(s.katalog_runu)
    zapisz_checkpoint(s.katalog_runu, replace(cp, kroki={
        **cp.kroki, "rozdziel": StanKrokuCheckpointu("w_toku", cp.kroki["rozdziel"].proby)}))
    if przygotuj is not None:
        przygotuj()
    return await wznow(s, AtrapaForeach(), RUN_ID, "zatwierdz")


def przerwane(po: list[dict]) -> list[tuple[str, str, str]]:
    return [(z["step_id"], z["agent_id"], z["text"]) for z in po
            if z["kind"] == "step_failed" and z["step_id"] == "rozdziel"]


@pytest.mark.parametrize("elementy", [ELEMENTY_3, ELEMENTY_3[:1]], ids=["trzy_pozycje", "jedna_pozycja"])
async def test_wznowienie_zamyka_przerwany_krok_foreach_wierszem_na_kazda_pozycje(tmp_path, elementy):
    s = swiat_przerwanego_foreach(tmp_path)
    agent = AtrapaForeach(plany={"plan": [plan(elementy)]})

    po = await wznow_z_przerwanym_foreach(s, agent)

    assert przerwane(po) == [
        ("rozdziel", f"a{i}", silnik.TEKST_PRZERWANIA) for i in range(1, len(elementy) + 1)]
    assert po[-1]["kind"] == "run_finished"


async def test_wznowienie_zamyka_przerwany_krok_foreach_wedlug_utrwalonej_liczby_a_nie_listy_z_dysku(
    tmp_path,
):
    s = swiat_przerwanego_foreach(tmp_path)
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})

    def skroc_liste():
        (s.worktree / SCIEZKA_PLANU).write_text(plan(ELEMENTY_3[:1]), encoding="utf-8")

    po = await wznow_z_przerwanym_foreach(s, agent, skroc_liste)

    assert przerwane(po) == [("rozdziel", a, silnik.TEKST_PRZERWANIA) for a in ("a1", "a2", "a3")]


@pytest.mark.parametrize(("zawartosc", "tekst"), ZEPSUTE_LICZBY)
async def test_wznowienie_przerwanego_kroku_foreach_z_zepsutym_plikiem_liczby_zamyka_jeden_wiersz(
    tmp_path, zawartosc, tekst,
):
    s = swiat_przerwanego_foreach(tmp_path)
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})

    po = await wznow_z_przerwanym_foreach(
        s, agent, lambda: zepsuj_plik(plik_liczby(s), zawartosc))

    assert przerwane(po) == [("rozdziel", "rozdziel", silnik.TEKST_PRZERWANIA)]
    assert po[-1]["kind"] == "run_finished"


@pytest.mark.parametrize(
    ("wyjatek", "tekst"),
    [(OSError("dysk niedostepny"), "dysk niedostepny"),
     (ValueError("zla wartosc"), "zla wartosc"),
     (KeyError("klucz"), "brak klucza 'klucz'"),
     (BladDziedziny("blad dziedziny"), "blad dziedziny")],
    ids=["OSError", "ValueError", "KeyError", "BladDziedziny"])
async def test_wyjatek_granicy_przy_liczeniu_wierszy_przerwanego_foreach_konczy_wznowienie_porazka_bramki(
    tmp_path, monkeypatch, wyjatek, tekst,
):
    s = swiat_przerwanego_foreach(tmp_path)
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})

    def liczba_nie_dziala(*argumenty, **opcje):
        raise wyjatek

    po = await wznow_z_przerwanym_foreach(
        s, agent, lambda: monkeypatch.setattr(silnik, "_liczba_wierszy", liczba_nie_dziala))

    assert [(z["kind"], z["step_id"], z["agent_id"], z["text"]) for z in po] == [
        ("step_failed", "zatwierdz", "zatwierdz", tekst),
        ("run_finished", "zatwierdz", "", tekst)]


def swiat_bramki_foreach(tmp_path):
    return swiat_foreach(tmp_path, dopisek=", approval: true")


def zepsuj_zapis_decyzji(monkeypatch) -> None:
    def zapis_nie_dziala(*argumenty, **opcje):
        raise OSError("dysk pelny")

    monkeypatch.setattr(silnik, "_zapisz_atomowo", zapis_nie_dziala)


def koniec_wznowienia(zdarzenia: list[dict]) -> list[tuple]:
    return [(z["kind"], z["step_id"], z["agent_id"], z.get("attempt")) for z in zdarzenia]


async def test_blad_zapisu_decyzji_na_bramce_foreach_zamyka_wiersze_pozycji_z_utrwalonej_liczby(
    tmp_path, monkeypatch,
):
    s = swiat_bramki_foreach(tmp_path)
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})
    await uruchom(s, agent, limit=LIMIT_S)
    zepsuj_zapis_decyzji(monkeypatch)

    po = await wznow(s, agent, RUN_ID, "rozdziel")

    assert koniec_wznowienia(po) == [
        *[("step_failed", "rozdziel", a, 1) for a in ("a1", "a2", "a3")],
        ("run_finished", "rozdziel", "", None)]
    assert po[0]["text"] == "zapis artefaktu decyzji dla kroku rozdziel: dysk pelny"


@pytest.mark.parametrize(("zawartosc", "tekst"), ZEPSUTE_LICZBY)
async def test_blad_zapisu_decyzji_na_bramce_foreach_z_zepsutym_plikiem_liczby_zamyka_jeden_wiersz(
    tmp_path, monkeypatch, zawartosc, tekst,
):
    s = swiat_bramki_foreach(tmp_path)
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})
    await uruchom(s, agent, limit=LIMIT_S)
    zepsuj_plik(plik_liczby(s), zawartosc)
    zepsuj_zapis_decyzji(monkeypatch)

    po = await wznow(s, agent, RUN_ID, "rozdziel")

    assert koniec_wznowienia(po) == [
        ("step_failed", "rozdziel", "rozdziel", None), ("run_finished", "rozdziel", "", None)]


@pytest.mark.parametrize("wyjatek", [OSError, ValueError, KeyError, BladDziedziny],
                         ids=lambda w: w.__name__)
async def test_wyjatek_granicy_przy_liczeniu_wierszy_po_bledzie_wznowienia_zostawia_jeden_wiersz_kroku(
    tmp_path, monkeypatch, wyjatek,
):
    s = swiat_bramki_foreach(tmp_path)
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})
    await uruchom(s, agent, limit=LIMIT_S)
    zepsuj_zapis_decyzji(monkeypatch)

    def liczba_nie_dziala(*argumenty, **opcje):
        raise wyjatek("liczba niedostepna")

    monkeypatch.setattr(silnik, "_liczba_wierszy", liczba_nie_dziala)

    po = await wznow(s, agent, RUN_ID, "rozdziel")

    assert koniec_wznowienia(po) == [
        ("step_failed", "rozdziel", "rozdziel", None), ("run_finished", "rozdziel", "", None)]


# --- krok foreach na bramce nie jest rozstrzygany od nowa w kazdym obiegu petli -------------------


@pytest.mark.parametrize(
    "lista_po_obiegu", [[], ELEMENTY_3[:2]], ids=["lista_pusta", "lista_krotsza"])
async def test_krok_foreach_czekajacy_na_bramce_nie_liczy_listy_od_nowa_gdy_rownolegly_krok_ja_zmieni(
    tmp_path, lista_po_obiegu,
):
    s = swiat_foreach(tmp_path, dopisek=", approval: true", sprawdz=True)

    def zmien_liste(korzen: Path, runu: Path) -> None:
        (korzen / SCIEZKA_PLANU).write_text(plan(lista_po_obiegu), encoding="utf-8")

    agent = AtrapaForeach(
        plany={"plan": [plan(ELEMENTY_3)]}, po_kroku={"sprawdz": zmien_liste})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    jedno(zdarzenia, "step_finished", "sprawdz")
    assert agenci_zdarzen(zdarzenia, "step_waiting", "rozdziel") == ["a1", "a2", "a3"]
    assert zdarzenia_kroku(zdarzenia, "step_skipped", "rozdziel") == []
    assert json.loads(plik_liczby(s).read_text(encoding="utf-8")) == {"liczba": 3}
    assert zdarzenia[-1]["kind"] == "step_waiting"


async def test_ponowiona_bramka_foreach_liczy_liste_od_nowa_a_nie_porownuje_jej_z_liczba_sprzed_ponowienia(
    tmp_path,
):
    s = swiat_foreach(tmp_path, dopisek=", approval: true, on_fail: {retry: plan, max: 1}")
    agent = AtrapaForeach(
        plany={"plan": [plan(ELEMENTY_3), plan(ELEMENTY_3[:2])]},
        porazki={("rozdziel", "a1", 1)})

    przed = await uruchom(s, agent, limit=LIMIT_S)
    po = await wznow(s, agent, RUN_ID, "rozdziel")

    assert agenci_zdarzen(przed, "step_waiting", "rozdziel") == ["a1", "a2", "a3"]
    assert agenci_zdarzen(po, "step_started", "rozdziel", proba=1) == ["a1", "a2", "a3"]
    czeka_ponownie = zdarzenia_kroku(po, "step_waiting", "rozdziel")
    assert [(z["agent_id"], z["attempt"]) for z in czeka_ponownie] == [("a1", 2), ("a2", 2)]
    assert json.loads(plik_liczby(s).read_text(encoding="utf-8")) == {"liczba": 2}
    assert not [z for z in przed + po if "lista zmieniła się po bramce" in z.get("text", "")]
    assert po[-1]["kind"] == "step_waiting"


# --- zrodlo od producenta bez zapisu, ktorego sciezka nie jest goła nazwa -------------------------


async def test_zrodlo_foreach_od_producenta_bez_zapisu_z_foreach_jest_czytane_z_katalogu_wariantu(
    tmp_path,
):
    s = swiat(
        tmp_path,
        [K("lista"), K("plan", ("lista",), extra=', foreach: "lista.json#x"'),
         K("rozdziel", ("plan",), extra=f', foreach: "{SPEC}"')],
        opcje_skilli={
            "lista": {"produces": ["lista.json"]},
            "plan": {"produces": [SCIEZKA_PLANU], "role": "gatherer"},
            "rozdziel": {"produces": ["wynik.md"]}})
    agent = AtrapaForeach(plany={
        "lista": [json.dumps({"x": ["jedyna"]})], "plan": [plan(ELEMENTY_3)]})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert (s.katalog_runu / "plan" / "agent_a1" / SCIEZKA_PLANU).is_file()
    assert not (s.katalog_runu / "plan" / SCIEZKA_PLANU).exists()
    assert agent.agenci("rozdziel") == ["a1", "a2", "a3"]
    assert [agent.element("rozdziel", a) for a in ("a1", "a2", "a3")] == ELEMENTY_3
    assert "step_failed" not in rodzaje(zdarzenia)


# --- niedozwolony identyfikator kroku w nazwie pliku liczby ----------------------------------------

ZLE_IDENTYFIKATORY = ["../x", "a/b", "a\\b", "..", ".", ""]


@pytest.mark.parametrize("funkcja", ["_plik_liczby", "_liczba_wierszy"])
@pytest.mark.parametrize("zly_id", ZLE_IDENTYFIKATORY)
def test_niedozwolony_identyfikator_kroku_foreach_nie_tworzy_sciezki_pliku_liczby(
    tmp_path, funkcja, zly_id,
):
    katalog_runu = tmp_path / "run"
    katalog_runu.mkdir()
    krok = SimpleNamespace(id=zly_id, foreach=SPEC)

    with pytest.raises(silnik.PorazkaKroku) as wyjatek:
        getattr(silnik, funkcja)(katalog_runu, krok)

    assert str(wyjatek.value) == f"niedozwolony identyfikator kroku: {zly_id!r}"
    assert list(katalog_runu.iterdir()) == []
    assert list(tmp_path.iterdir()) == [katalog_runu]


@pytest.mark.parametrize("zly_id", ZLE_IDENTYFIKATORY)
def test_niedozwolony_identyfikator_kroku_foreach_nie_zapisuje_pliku_liczby_nigdzie(tmp_path, zly_id):
    katalog_runu = tmp_path / "run"
    katalog_runu.mkdir()
    krok = SimpleNamespace(id=zly_id, foreach=SPEC)

    with pytest.raises(silnik.PorazkaKroku) as wyjatek:
        silnik._zapisz_liczbe(katalog_runu, krok, 3)

    assert str(wyjatek.value) == f"niedozwolony identyfikator kroku: {zly_id!r}"
    assert list(katalog_runu.iterdir()) == []
    assert list(tmp_path.iterdir()) == [katalog_runu]


# --- najblizszy przodek foreach nie jest producentem skillowym artefaktu --------------------------


async def test_foreach_za_krokiem_systemowym_czyta_zrodlo_od_producenta_skillowego_dalej_w_grafie(
    tmp_path,
):
    # `_producent_skilla` mija krok systemowy (`diff`, skill None) bez zagladania do rejestru skilli
    tresc = (
        "name: w\nsteps:\n"
        "  plan: {skill: s_plan}\n"
        "  diff: {system: diff, needs: [plan]}\n"
        f'  rozdziel: {{skill: s_rozdziel, needs: [diff], foreach: "{SPEC}"}}\n')
    s = swiat(
        tmp_path, [K("plan"), K("rozdziel")], tresc=tresc,
        opcje_skilli={"plan": {"produces": [SCIEZKA_PLANU]}, "rozdziel": {"produces": ["wynik.md"]}})
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert agent.agenci("rozdziel") == ["a1", "a2", "a3"]
    assert [agent.element("rozdziel", a) for a in ("a1", "a2", "a3")] == ELEMENTY_3
    assert "step_failed" not in rodzaje(zdarzenia)
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_foreach_za_posrednikiem_bez_zapisu_ktory_nie_produkuje_zrodla_czyta_je_z_worktree(
    tmp_path,
):
    # `_producent_skilla` mija `posrednik` (skill bez `plan.json` w produces), wiec producentem jest
    # autor `plan` z zapisem; gdyby wzial posrednika, zrodlo lezaloby w katalogu runu i krok by padl
    s = swiat(
        tmp_path,
        [K("plan"), K("posrednik", ("plan",)),
         K("rozdziel", ("posrednik",), extra=f', foreach: "{SPEC}"')],
        opcje_skilli={
            "plan": {"produces": [SCIEZKA_PLANU]},
            "posrednik": {"produces": ["notatki.md"], "role": "gatherer"},
            "rozdziel": {"produces": ["wynik.md"]}})
    agent = AtrapaForeach(plany={"plan": [plan(ELEMENTY_3)]})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert (s.worktree / SCIEZKA_PLANU).is_file()
    assert not (s.katalog_runu / "posrednik" / SCIEZKA_PLANU).exists()
    assert agent.agenci("rozdziel") == ["a1", "a2", "a3"]
    assert [agent.element("rozdziel", a) for a in ("a1", "a2", "a3")] == ELEMENTY_3
    assert "step_failed" not in rodzaje(zdarzenia)
    assert zdarzenia[-1]["kind"] == "run_finished"
