"""HAR-80: silnik runu u runnera - workflow wykonywany wedlug grafu `needs`, rownolegle (TDD).

Prawdziwy kod: `runner.silnik.wykonaj_run`, `utworz_worktree`, `rozstrzygnij_rejestr`,
`wczytaj_workflow`, `graf.nastepne`, `wykonaj_krok` na prawdziwych procesach. Zaslepiony jest
tylko agent (`claude`): atrapa zapisuje znacznik startu (`time.time()`) i swoj `step_id`.
Repozytorium uzytkownika to prawdziwe repo git w tmp_path (`.cockpit/skills`, `workflows`,
`project.yaml`), katalog runow lezy obok niego, nie w srodku.
Szybkie testy regul uzywaja `Szpieg` (zapisuje wywolania, nie uruchamia procesu); testy
rownoleglosci, wyjscia i anulowania - atrapy-procesu przez prawdziwe `wykonaj_krok`.
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from cockpit.services.invariants import Rola, Tier
from cockpit.services.skills import rozstrzygnij_rejestr
from cockpit.services.workflow import ma_bledy, wczytaj_workflow
from cockpit.services.workflow_walidacja import waliduj_workflow
from procesy import czekaj_na_warunek, czy_zyje, zabij_proces
from runner.agent import ProfilBackendu
from runner.bufor import BrakMiejsca
from runner.krok import WynikKroku, wykonaj_krok
from runner.zakres import Zakres

RUN_ID = "run-80"
LIMIT_S = 90

TOZSAMOSC = [
    "-c", "user.name=Test",
    "-c", "user.email=test@example.com",
    "-c", "commit.gpgsign=false",
]

PROJEKT_DOMYSLNY = "stack: [python]\ncapabilities:\n  test: pytest -q\n  lint: ruff check\n"

# Atrapa agenta: znacznik startu i pid zapisane atomowo, trzy linie stdout z wlasnym step_id,
# spanie, znacznik konca, kod wyjscia. Lezy w `-c`, a znaczniki w katalogu POZA repozytorium
# i poza worktree, zeby nie byly zmiana w drzewie.
KOD_ATRAPY = r"""
import json, os, subprocess, sys, time
from pathlib import Path

p = json.loads(sys.argv[1])
znaczniki = Path(p["znaczniki"])
krok = p["step_id"]


def zapisz(nazwa, tekst):
    tmp = znaczniki / (nazwa + ".tmp")
    tmp.write_text(tekst)
    os.replace(tmp, znaczniki / nazwa)


zapisz("start_" + krok, repr(time.time()))
zapisz("pid_" + krok, str(os.getpid()))
if p.get("repo"):
    w = subprocess.run(
        ["git", "-C", p["repo"], "status", "--porcelain", "--untracked-files=all"],
        capture_output=True, text=True,
    )
    zapisz("git_" + krok, f"{w.returncode}|{w.stdout}")
for _ in range(3):
    print(krok, flush=True)
for sciezka in p.get("zapisz", []):
    Path(sciezka).parent.mkdir(parents=True, exist_ok=True)
    Path(sciezka).write_text("wynik")
time.sleep(p.get("spanie", 0))
zapisz("koniec_" + krok, repr(time.time()))
sys.exit(p.get("kod", 0))
"""


def _git(repo: Path, *argumenty: str) -> str:
    return subprocess.run(
        ["git", *TOZSAMOSC, "-C", str(repo), *argumenty],
        check=True, capture_output=True, text=True,
    ).stdout


@dataclass
class K:
    """Krok workflow: id, needs (id krokow), skill (domyslnie `s_<id>`), dopisek YAML."""

    id: str
    needs: tuple[str, ...] = ()
    skill: str | None = None
    extra: str = ""

    @property
    def nazwa_skilla(self) -> str:
        return self.skill or f"s_{self.id}"


def napisz_skill(
    repo: Path, id: str, *, role: str = "author", needs: list[str] | None = None,
    consumes: list[str] | None = None, produces: list[str] | None = None, tresc: str = "Brief.",
) -> Path:
    katalog = repo / ".cockpit" / "skills" / id
    katalog.mkdir(parents=True, exist_ok=True)
    linie = [f"id: {id}", f"name: {id}", f"role: {role}"]
    for pole, wartosci in (("consumes", consumes), ("produces", produces), ("needs", needs)):
        if wartosci:
            linie.append(f"{pole}: [{', '.join(wartosci)}]")
    plik = katalog / "SKILL.md"
    plik.write_text("---\n" + "\n".join(linie) + "\n---\n" + tresc + "\n", encoding="utf-8")
    return plik


def tresc_workflow(kroki: list[K], nazwa: str = "w") -> str:
    linie = [f"name: {nazwa}", "steps:"]
    for k in kroki:
        zaleznosci = f", needs: [{', '.join(k.needs)}]" if k.needs else ""
        linie.append(f"  {k.id}: {{skill: {k.nazwa_skilla}{zaleznosci}{k.extra}}}")
    return "\n".join(linie) + "\n"


def swiat(
    tmp_path: Path,
    kroki: list[K],
    *,
    opcje_skilli: dict[str, dict] | None = None,
    project: str | None = PROJEKT_DOMYSLNY,
    workflow: str | None = "w",
    tresc: str | None = None,
    profil: ProfilBackendu | None = None,
    skille_dodatkowe: dict[str, dict] | None = None,
) -> SimpleNamespace:
    """Repo git z jednym commitem: skille, workflow `w`, project.yaml. `workflow=None` = brak pliku."""
    repo = tmp_path / "repo_uzytkownika"
    dom = tmp_path / "dom"
    root = tmp_path / "runy"
    znaczniki = tmp_path / "znaczniki"
    for katalog in (repo, dom, znaczniki):
        katalog.mkdir()
    _git(repo, "init", "-q")
    opcje_skilli = opcje_skilli or {}
    for k in kroki:
        if k.skill is None:
            napisz_skill(repo, k.nazwa_skilla, **opcje_skilli.get(k.id, {}))
    for id, opcje in (skille_dodatkowe or {}).items():
        napisz_skill(repo, id, **opcje)
    if workflow is not None:
        plik = repo / ".cockpit" / "workflows" / f"{workflow}.yaml"
        plik.parent.mkdir(parents=True, exist_ok=True)
        plik.write_text(tresc if tresc is not None else tresc_workflow(kroki), encoding="utf-8")
    if project is not None:
        (repo / ".cockpit").mkdir(exist_ok=True)
        (repo / ".cockpit" / "project.yaml").write_text(project, encoding="utf-8")
    (repo / "README.md").write_text("repo\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return SimpleNamespace(
        repo=repo, dom=dom, root=root, znaczniki=znaczniki,
        zakres=Zakres([tmp_path]),
        profil=profil or ProfilBackendu(modele={
            Tier.judgment: "m-judgment", Tier.execution: "m-execution",
            Tier.retrieval: "m-retrieval"}),
        polecenie={"type": "run_workflow", "run_id": RUN_ID, "workflow": "w",
                   "sciezka": str(repo)},
        katalog_runu=root / RUN_ID,
        worktree=root / RUN_ID / "worktree",
    )


@dataclass
class Szpieg:
    """Agent bez procesu: zapisuje wywolania i zwraca wynik zadany per krok."""

    wyniki: dict[str, WynikKroku] = field(default_factory=dict)
    opoznienie: float = 0.0
    opoznienia: dict[str, float] = field(default_factory=dict)     # per krok, przed `opoznienie`
    wywolania: list = field(default_factory=list)

    async def __call__(self, spec, polecenie, katalog, emit, *, katalog_runu=None, prompt=None):
        self.wywolania.append(SimpleNamespace(
            spec=spec, polecenie=polecenie, katalog=katalog, katalog_runu=katalog_runu,
            prompt=prompt))
        czekanie = self.opoznienia.get(spec.step_id, self.opoznienie)
        if czekanie:
            await asyncio.sleep(czekanie)
        return self.wyniki.get(spec.step_id, WynikKroku("zakonczony", None, 0))

    @property
    def kroki(self) -> list[str]:
        return [w.spec.step_id for w in self.wywolania]

    def spec(self, step_id: str):
        (wywolanie,) = [w for w in self.wywolania if w.spec.step_id == step_id]
        return wywolanie.spec


@dataclass
class Atrapa:
    """Agent z prawdziwym procesem przez prawdziwe `wykonaj_krok`."""

    znaczniki: Path
    spanie: dict[str, float] = field(default_factory=dict)
    kody: dict[str, int] = field(default_factory=dict)
    repo: Path | None = None
    wywolania: list = field(default_factory=list)

    async def __call__(self, spec, polecenie, katalog, emit, *, katalog_runu=None, prompt=None):
        self.wywolania.append(spec.step_id)
        opis = {
            "znaczniki": str(self.znaczniki), "step_id": spec.step_id,
            "spanie": self.spanie.get(spec.step_id, 0), "kod": self.kody.get(spec.step_id, 0),
            "zapisz": list(spec.wyjscia), "repo": str(self.repo) if self.repo else None,
        }
        argv = [sys.executable, "-I", "-c", KOD_ATRAPY, json.dumps(opis)]
        return await wykonaj_krok(spec, argv, katalog, emit, katalog_runu=katalog_runu)

    def czas(self, rodzaj: str, krok: str) -> float:
        return float((self.znaczniki / f"{rodzaj}_{krok}").read_text())


async def uruchom(s, agent, *, polecenie=None, emit=None, limit: float = LIMIT_S) -> list[dict]:
    from runner import silnik   # import leniwy: brak modulu czerwieni kazdy test osobno

    zdarzenia: list[dict] = []

    async def zapisz(zdarzenie: dict) -> None:
        zdarzenia.append(zdarzenie)

    await asyncio.wait_for(
        silnik.wykonaj_run(
            polecenie if polecenie is not None else s.polecenie,
            emit or zapisz, agent,
            profil=s.profil, dom=s.dom, root=s.root, zakres=s.zakres,
        ),
        limit,
    )
    return zdarzenia


def rodzaje(zdarzenia: list[dict]) -> list[str]:
    return [z["kind"] for z in zdarzenia]


def jedno(zdarzenia: list[dict], rodzaj: str, krok: str) -> dict:
    pasujace = [z for z in zdarzenia if z["kind"] == rodzaj and z["step_id"] == krok]
    assert len(pasujace) == 1, f"oczekiwano jednego {rodzaj} kroku {krok}: {pasujace}"
    return pasujace[0]


def indeks(zdarzenia: list[dict], rodzaj: str, krok: str) -> int:
    return zdarzenia.index(jedno(zdarzenia, rodzaj, krok))


def liczba_worktree(repo: Path) -> int:
    wyjscie = _git(repo, "worktree", "list", "--porcelain")
    return sum(1 for w in wyjscie.splitlines() if w.startswith("worktree "))


def sprawdz_blad_przed_startem(zdarzenia: list[dict]) -> str:
    """D4: dokladnie `step_failed` kroku `walidacja` i `run_finished`, bez startu krokow."""
    assert rodzaje(zdarzenia) == ["step_failed", "run_finished"]
    nieudany, koniec = zdarzenia
    assert nieudany["run_id"] == koniec["run_id"] == RUN_ID
    assert nieudany["step_id"] == koniec["step_id"] == "walidacja"
    assert nieudany["agent_id"] == koniec["agent_id"] == ""
    assert nieudany["text"] == koniec["text"]
    assert nieudany["text"].strip()
    assert koniec["cost_usd"] is None
    assert koniec["cost_source"] == "unavailable"
    return nieudany["text"]


# --- kryterium 1: rownoleglosc wedlug needs ------------------------------------------


async def test_kroki_niezalezne_od_siebie_startuja_w_tej_samej_sekundzie(tmp_path):
    s = swiat(tmp_path, [K("a"), K("b"), K("c", ("a", "b"))])
    agent = Atrapa(s.znaczniki, spanie={"a": 2.0, "b": 2.0})

    await uruchom(s, agent)

    assert abs(agent.czas("start", "a") - agent.czas("start", "b")) < 1.0


async def test_krok_zalezny_od_dwoch_startuje_dopiero_po_zakonczeniu_obu_procesow(tmp_path):
    s = swiat(tmp_path, [K("a"), K("b"), K("c", ("a", "b"))])
    # a konczy sie szybko, b dlugo: start po pierwszym zakonczonym bylby za wczesnie
    agent = Atrapa(s.znaczniki, spanie={"a": 0.1, "b": 1.5})

    await uruchom(s, agent)

    assert agent.czas("start", "c") >= agent.czas("koniec", "a")
    assert agent.czas("start", "c") >= agent.czas("koniec", "b")


async def test_krok_zalezny_jest_wystartowany_po_zdarzeniach_zakonczenia_obu_zaleznosci(tmp_path):
    s = swiat(tmp_path, [K("a"), K("b"), K("c", ("a", "b"))])
    agent = Atrapa(s.znaczniki, spanie={"a": 0.1, "b": 1.0})

    zdarzenia = await uruchom(s, agent)

    start_c = indeks(zdarzenia, "step_started", "c")
    assert start_c > indeks(zdarzenia, "step_finished", "a")
    assert start_c > indeks(zdarzenia, "step_finished", "b")


async def test_wszystkie_kroki_z_jednego_planu_maja_start_zanim_ktorykolwiek_cos_wypisze(tmp_path):
    s = swiat(tmp_path, [K("a"), K("b")])
    agent = Atrapa(s.znaczniki, spanie={"a": 0.3, "b": 0.3})

    zdarzenia = await uruchom(s, agent)

    pierwsze_wyjscie = min(i for i, z in enumerate(zdarzenia) if z["kind"] == "output")
    assert indeks(zdarzenia, "step_started", "a") < pierwsze_wyjscie
    assert indeks(zdarzenia, "step_started", "b") < pierwsze_wyjscie


async def test_lancuch_krokow_wykonuje_sie_po_kolei_a_koniec_runu_wskazuje_ostatni_krok(tmp_path):
    s = swiat(tmp_path, [K("a"), K("b", ("a",)), K("c", ("b",))])
    agent = Atrapa(s.znaczniki, spanie={"a": 0.2, "b": 0.2})

    zdarzenia = await uruchom(s, agent)

    assert agent.czas("start", "b") >= agent.czas("koniec", "a")
    assert agent.czas("start", "c") >= agent.czas("koniec", "b")
    assert rodzaje([z for z in zdarzenia if z["kind"] != "output"]) == [
        "step_started", "step_finished", "step_started", "step_finished",
        "step_started", "step_finished", "run_finished"]
    assert zdarzenia[-1]["step_id"] == "c"


# --- kryterium 2: brak capability -----------------------------------------------------


async def test_krok_z_brakujaca_capability_jest_pominiety_z_powodem_a_niezalezny_sie_wykonuje(tmp_path):
    s = swiat(tmp_path, [K("a"), K("b")], opcje_skilli={"a": {"needs": ["deploy"]}})
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent)

    pominiety = jedno(zdarzenia, "step_skipped", "a")
    assert "brak_capability" in pominiety["text"]
    assert "deploy" in pominiety["text"]
    assert pominiety["agent_id"] == "a"
    assert pominiety["run_id"] == RUN_ID
    jedno(zdarzenia, "step_finished", "b")
    assert agent.kroki == ["b"]


async def test_capability_zadeklarowana_w_projekcie_nie_pomija_kroku(tmp_path):
    s = swiat(tmp_path, [K("a")], opcje_skilli={"a": {"needs": ["test"]}})
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent)

    assert "step_skipped" not in rodzaje(zdarzenia)
    jedno(zdarzenia, "step_finished", "a")


async def test_krok_pominiety_z_braku_capability_nie_zatrzymuje_runu_koncem_ostatnim(tmp_path):
    s = swiat(tmp_path, [K("a")], opcje_skilli={"a": {"needs": ["deploy"]}})

    zdarzenia = await uruchom(s, Szpieg())

    assert rodzaje(zdarzenia) == ["step_skipped", "run_finished"]
    assert zdarzenia[-1]["step_id"] == "run"     # zaden krok nie wystartowal


# --- kryterium 3: wyjscie przypisane do kroku ------------------------------------------


async def test_wyjscie_rownoleglych_krokow_niesie_step_id_kroku_ktory_je_wypisal(tmp_path):
    s = swiat(tmp_path, [K("a"), K("b")])
    agent = Atrapa(s.znaczniki, spanie={"a": 0.5, "b": 0.5})

    zdarzenia = await uruchom(s, agent)

    wyjscia = [z for z in zdarzenia if z["kind"] == "output"]
    assert len(wyjscia) == 6
    for z in wyjscia:
        assert z["text"] == z["step_id"]          # atrapa wypisuje wlasny step_id
        assert z["agent_id"] == z["step_id"]
        assert z["run_id"] == RUN_ID
    assert {z["step_id"] for z in wyjscia} == {"a", "b"}


# --- kryterium 4: walidacja przed jakimkolwiek krokiem ----------------------------------


async def test_workflow_z_bledami_konczy_run_bez_startu_krokow_i_wymienia_problemy(tmp_path):
    s = swiat(tmp_path, [K("a", skill="skill_ktorego_nie_ma"), K("b", ("nie_ma_takiego",))])
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent)

    tekst = sprawdz_blad_przed_startem(zdarzenia)
    linie = tekst.splitlines()
    assert len(linie) == 2
    assert any(w.startswith("a: nieznany_skill: ") for w in linie)
    assert any(w.startswith("b: nieznana_zaleznosc: ") for w in linie)
    assert agent.wywolania == []


async def test_problem_calego_pliku_workflow_ma_w_linii_myslnik_zamiast_kroku(tmp_path):
    s = swiat(tmp_path, [K("a")], tresc="name: w\n")      # brak mapy steps

    tekst = sprawdz_blad_przed_startem(await uruchom(s, Szpieg()))

    assert tekst == "-: brak_krokow: brak mapy steps"


async def test_brak_pliku_workflow_konczy_run_bledem_bez_startu_krokow(tmp_path):
    s = swiat(tmp_path, [K("a")], workflow=None)
    agent = Szpieg()

    tekst = sprawdz_blad_przed_startem(await uruchom(s, agent))

    assert tekst.startswith("-: blad_odczytu: ")
    assert agent.wywolania == []


async def test_ostrzezenie_walidacji_nie_przerywa_runu(tmp_path):
    s = swiat(tmp_path, [K("a", extra=", optional: true")])
    workflow, _ = wczytaj_workflow(s.repo / ".cockpit" / "workflows" / "w.yaml")
    problemy = waliduj_workflow(workflow, rozstrzygnij_rejestr(s.repo, s.dom), s.dom)
    assert problemy and not ma_bledy(problemy)        # wejscie ma ostrzezenie, nie blad
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent)

    assert "step_failed" not in rodzaje(zdarzenia)
    jedno(zdarzenia, "step_finished", "a")
    assert agent.kroki == ["a"]


# --- kryterium 5: repozytorium uzytkownika nietkniete ------------------------------------


async def test_w_trakcie_i_po_runie_repozytorium_uzytkownika_nie_ma_zmian(tmp_path):
    s = swiat(tmp_path, [K("a"), K("b", ("a",))],
              opcje_skilli={"a": {"produces": ["wynik.md"]}, "b": {"consumes": ["wynik.md"]}})
    agent = Atrapa(s.znaczniki, spanie={"a": 0.2}, repo=s.repo)

    await uruchom(s, agent)

    for krok in ("a", "b"):
        kod, _, status = (s.znaczniki / f"git_{krok}").read_text().partition("|")
        assert kod == "0"                         # git zadzialal, pusty wynik to nie awaria
        assert status == ""
    assert _git(s.repo, "status", "--porcelain", "--untracked-files=all") == ""
    assert not (s.repo / ".cockpit" / "_run").exists()
    # praca agenta wyladowala w worktree, nie w repozytorium
    assert (s.worktree / "wynik.md").is_file()
    assert not (s.repo / "wynik.md").exists()


# --- zdarzenia runu: tozsamosc, kolejnosc, pomiar ------------------------------------------


async def test_run_finished_jest_ostatnim_zdarzeniem_i_wystepuje_raz(tmp_path):
    s = swiat(tmp_path, [K("a"), K("b"), K("c", ("a", "b"))])

    zdarzenia = await uruchom(s, Szpieg())

    assert rodzaje(zdarzenia).count("run_finished") == 1
    assert zdarzenia[-1]["kind"] == "run_finished"
    koniec = zdarzenia[-1]
    assert koniec["run_id"] == RUN_ID
    assert koniec["agent_id"] == ""
    assert koniec["text"] == ""
    assert koniec["cost_usd"] is None
    assert koniec["cost_source"] == "unavailable"


async def test_zdarzenia_kroku_niosa_run_id_step_id_i_agent_id_rowny_step_id(tmp_path):
    s = swiat(tmp_path, [K("a"), K("b", ("a",))])

    zdarzenia = await uruchom(s, Szpieg())

    assert {z["run_id"] for z in zdarzenia} == {RUN_ID}
    for krok in ("a", "b"):
        start = jedno(zdarzenia, "step_started", krok)
        koniec = jedno(zdarzenia, "step_finished", krok)
        assert start["agent_id"] == koniec["agent_id"] == krok


async def test_zakonczony_i_nieudany_krok_niosa_zmierzony_czas_trwania(tmp_path):
    s = swiat(tmp_path, [K("a"), K("b")])
    agent = Szpieg(
        opoznienie=0.3, wyniki={"b": WynikKroku("nieudany", "kod wyjścia 1", 1)})

    zdarzenia = await uruchom(s, agent)

    for z in (jedno(zdarzenia, "step_finished", "a"), jedno(zdarzenia, "step_failed", "b")):
        assert 250 <= z["duration_ms"] < 10_000        # zmierzone, nie zero i nie brak


# --- krok nieudany i kroki zalezne ------------------------------------------------------


async def test_krok_z_niezerowym_kodem_wyjscia_jest_nieudany_a_zalezny_pominiety(tmp_path):
    s = swiat(tmp_path, [K("a"), K("b", ("a",)), K("c")])
    agent = Atrapa(s.znaczniki, kody={"a": 1})

    zdarzenia = await uruchom(s, agent)

    assert jedno(zdarzenia, "step_failed", "a")["text"] == "kod wyjścia 1"
    pominiety = jedno(zdarzenia, "step_skipped", "b")
    assert pominiety["text"].startswith("zaleznosc_nieudana: ")
    assert "a" in pominiety["text"].removeprefix("zaleznosc_nieudana: ")
    jedno(zdarzenia, "step_finished", "c")                  # niezalezna galaz idzie dalej
    assert "step_finished" not in [z["kind"] for z in zdarzenia if z["step_id"] == "a"]
    assert sorted(agent.wywolania) == ["a", "c"]
    assert zdarzenia[-1]["kind"] == "run_finished"


# --- approval: bramka (HAR-172; szczegoly w tests/test_bramka_zatwierdzenia.py) -------------


async def test_krok_z_approval_nie_przechodzi_bez_decyzji_czlowieka_i_nie_uruchamia_agenta(tmp_path):
    s = swiat(tmp_path, [K("a", extra=", approval: true"), K("b", ("a",)), K("c")])
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent)

    czekanie = jedno(zdarzenia, "step_waiting", "a")
    # krok na bramce ma wylacznie `step_waiting`: bez startu, bez porazki, bez konca
    assert [z for z in zdarzenia if z["step_id"] == "a"] == [czekanie]
    # krok zalezny od bramki nie rusza i nie jest pominiety: czeka razem z nia
    assert [z for z in zdarzenia if z["step_id"] == "b"] == []
    jedno(zdarzenia, "step_finished", "c")                  # krok niezalezny od bramki konczy sie
    assert agent.kroki == ["c"]                              # a i b nie dotarly do agenta
    # bramka jest ogloszona na koncu: po kroku niezaleznym i jako ostatnie zdarzenie runu
    assert indeks(zdarzenia, "step_finished", "c") < indeks(zdarzenia, "step_waiting", "a")
    assert zdarzenia[-1] == czekanie
    # run zawieszony na bramce nie jest zakonczony: kokpit zrobilby z niego "do odbioru"
    assert "run_finished" not in rodzaje(zdarzenia)
    assert (s.katalog_runu / "checkpoint.json").is_file()


# --- bledy przed startem: nazwa, zakres, project.yaml ---------------------------------------


@pytest.mark.parametrize("nazwa", [".", "..", "../w", "a/b", "", "a" * 65, "w w"])
async def test_niedozwolona_nazwa_workflow_konczy_run_bledem_bez_tworzenia_worktree(tmp_path, nazwa):
    s = swiat(tmp_path, [K("a")])
    agent = Szpieg()
    polecenie = {**s.polecenie, "workflow": nazwa}

    zdarzenia = await uruchom(s, agent, polecenie=polecenie)

    tekst = sprawdz_blad_przed_startem(zdarzenia)
    assert tekst == f"niedozwolona nazwa workflow: {nazwa!r}"
    assert agent.wywolania == []
    assert not s.root.exists()
    assert liczba_worktree(s.repo) == 1


@pytest.mark.parametrize("nazwa", [None, 5, ["w"]])
async def test_nazwa_workflow_ktora_nie_jest_napisem_konczy_run_bledem(tmp_path, nazwa):
    s = swiat(tmp_path, [K("a")])
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent, polecenie={**s.polecenie, "workflow": nazwa})

    tekst = sprawdz_blad_przed_startem(zdarzenia)
    assert tekst == f"niedozwolona nazwa workflow: {nazwa!r}"
    assert agent.wywolania == []
    assert liczba_worktree(s.repo) == 1


async def test_nazwa_workflow_o_najdluzszej_dozwolonej_dlugosci_jest_wykonana(tmp_path):
    nazwa = "a" * 64
    s = swiat(tmp_path, [K("a")], workflow=nazwa, tresc=tresc_workflow([K("a")], nazwa))
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent, polecenie={**s.polecenie, "workflow": nazwa})

    jedno(zdarzenia, "step_finished", "a")
    assert agent.kroki == ["a"]


@pytest.mark.parametrize("sciezka", ["poza", "wzgledna/sciezka"])
async def test_sciezka_poza_zakresem_runnera_konczy_run_bledem_bez_tworzenia_worktree(
    tmp_path, sciezka,
):
    s = swiat(tmp_path, [K("a")])
    agent = Szpieg()
    if sciezka == "poza":
        sciezka = str(s.repo)
        s.zakres = Zakres([tmp_path / "inny_katalog"])      # repo istnieje, ale nie w zakresie

    zdarzenia = await uruchom(s, agent, polecenie={**s.polecenie, "sciezka": sciezka})

    tekst = sprawdz_blad_przed_startem(zdarzenia)
    assert "zakres" in tekst
    assert agent.wywolania == []
    assert not s.root.exists()
    assert liczba_worktree(s.repo) == 1


async def test_wadliwy_project_yaml_konczy_run_bledem_bez_startu_krokow(tmp_path):
    s = swiat(tmp_path, [K("a")], project="capabilities: [niezamkniete\n")
    agent = Szpieg()

    tekst = sprawdz_blad_przed_startem(await uruchom(s, agent))

    assert "niepoprawny_yaml" in tekst
    assert agent.wywolania == []


async def test_brak_pliku_project_yaml_nie_przerywa_runu_ale_nie_daje_capabilities(tmp_path):
    s = swiat(tmp_path, [K("a"), K("b")], project=None, opcje_skilli={"b": {"needs": ["test"]}})
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent)

    jedno(zdarzenia, "step_finished", "a")
    assert "brak_capability" in jedno(zdarzenia, "step_skipped", "b")["text"]
    assert agent.kroki == ["a"]


@pytest.mark.parametrize("run_id", [None, 7, ""])
async def test_polecenie_bez_poprawnego_run_id_nic_nie_wysyla_i_zostawia_slad_na_stderr(
    tmp_path, capsys, run_id,
):
    s = swiat(tmp_path, [K("a")])
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent, polecenie={**s.polecenie, "run_id": run_id})

    assert zdarzenia == []
    assert agent.wywolania == []
    assert capsys.readouterr().err == "polecenie run_workflow bez poprawnego run_id odrzucone\n"
    assert not s.root.exists()


# --- worktree, kopia workflow, skille ---------------------------------------------------------


async def test_run_tworzy_worktree_poza_repo_a_agent_dziala_w_nim_z_katalogiem_runu(tmp_path):
    s = swiat(tmp_path, [K("a")])
    agent = Szpieg()

    await uruchom(s, agent)

    (wywolanie,) = agent.wywolania
    assert Path(wywolanie.katalog).resolve() == s.worktree.resolve()
    assert Path(wywolanie.katalog_runu).resolve() == s.katalog_runu.resolve()
    assert s.worktree.is_dir()                       # silnik nie sprzata (zostaje do odbioru)
    assert liczba_worktree(s.repo) == 2


async def test_kopia_workflow_w_katalogu_runu_ma_tresc_pliku_z_repozytorium(tmp_path):
    s = swiat(tmp_path, [K("a"), K("b", ("a",))])

    await uruchom(s, Szpieg())

    kopia = s.katalog_runu / "workflow.yaml"
    oryginal = s.repo / ".cockpit" / "workflows" / "w.yaml"
    assert kopia.read_bytes() == oryginal.read_bytes()
    # macOS nie rozroznia wielkosci liter w `exists`/`read_bytes`: nazwa z listingu katalogu
    assert "workflow.yaml" in os.listdir(s.katalog_runu)


async def test_skille_sa_zmaterializowane_w_worktree_a_nie_w_repo_uzytkownika(tmp_path):
    s = swiat(tmp_path, [K("a")], skille_dodatkowe={"s_nieuzywany": {}})

    await uruchom(s, Szpieg())

    skill = s.worktree / ".cockpit" / "_run" / "skills" / "s_a" / "SKILL.md"
    assert skill.read_bytes() == (s.repo / ".cockpit/skills/s_a/SKILL.md").read_bytes()
    assert not (s.repo / ".cockpit" / "_run").exists()
    # tylko skille uzyte w workflow
    assert not (s.worktree / ".cockpit" / "_run" / "skills" / "s_nieuzywany").exists()


async def test_niezacommitowana_zmiana_workflow_w_repo_nie_wplywa_na_run(tmp_path):
    s = swiat(tmp_path, [K("a")])
    plik = s.repo / ".cockpit" / "workflows" / "w.yaml"
    zacommitowany = plik.read_bytes()
    # z repozytorium wynikalby blad walidacji (nieznany skill) i dodatkowy krok z
    plik.write_text(tresc_workflow([K("a"), K("z", skill="skill_ktorego_nie_ma")]))
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent)

    assert "walidacja" not in {z["step_id"] for z in zdarzenia}
    assert agent.kroki == ["a"]
    assert (s.katalog_runu / "workflow.yaml").read_bytes() == zacommitowany


async def test_niezacommitowana_zmiana_project_yaml_w_repo_nie_dodaje_capabilities(tmp_path):
    s = swiat(tmp_path, [K("a")], opcje_skilli={"a": {"needs": ["deploy"]}})
    (s.repo / ".cockpit" / "project.yaml").write_text(
        PROJEKT_DOMYSLNY + "  deploy: make deploy\n")        # z repo krok by sie wykonal
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent)

    assert "brak_capability" in jedno(zdarzenia, "step_skipped", "a")["text"]
    assert agent.wywolania == []


async def test_niezacommitowana_zmiana_skilla_w_repo_nie_zmienia_roli_ani_materializacji(tmp_path):
    s = swiat(tmp_path, [K("a")])
    napisz_skill(s.repo, "s_a", role="reviewer", tresc="ZMIANA_NIEZACOMMITOWANA")
    agent = Szpieg()

    await uruchom(s, agent)

    assert agent.spec("a").rola == Rola.author
    skill = s.worktree / ".cockpit" / "_run" / "skills" / "s_a" / "SKILL.md"
    assert "ZMIANA_NIEZACOMMITOWANA" not in skill.read_text()


# --- mapowanie kroku na specyfikacje agenta -------------------------------------------------------


async def test_komendy_capabilities_kroku_trafiaja_do_specyfikacji_agenta(tmp_path):
    s = swiat(
        tmp_path, [K("a")], opcje_skilli={"a": {"needs": ["test", "lint"]}},
        project=PROJEKT_DOMYSLNY + "  build: make\n")
    agent = Szpieg()

    await uruchom(s, agent)

    # tylko to, czego skill potrzebuje; `build` projektu nie wchodzi do specyfikacji
    assert dict(agent.spec("a").komendy) == {"test": "pytest -q", "lint": "ruff check"}


async def test_specyfikacja_kroku_niesie_identyfikatory_role_oraz_wejscia_i_wyjscia_skilla(tmp_path):
    s = swiat(
        tmp_path, [K("a"), K("b", ("a",))],
        opcje_skilli={"a": {"produces": ["plan.md"]},
                      "b": {"consumes": ["plan.md"], "role": "executor"}})
    agent = Szpieg()

    await uruchom(s, agent)

    a, b = agent.spec("a"), agent.spec("b")
    assert (a.run_id, a.step_id, a.agent_id, a.skill_id) == (RUN_ID, "a", "a", "s_a")
    assert (b.run_id, b.step_id, b.agent_id, b.skill_id) == (RUN_ID, "b", "b", "s_b")
    assert a.rola == Rola.author and b.rola == Rola.executor
    assert (a.wejscia, a.wyjscia) == ([], ["plan.md"])
    assert (b.wejscia, b.wyjscia) == (["plan.md"], [])


async def test_polecenie_agenta_niesie_sciezke_skilla_a_model_wynika_z_tieru_roli(tmp_path):
    s = swiat(
        tmp_path, [K("a"), K("b", ("a",))],
        opcje_skilli={"a": {"role": "author"}, "b": {"role": "executor"}})
    agent = Szpieg()

    await uruchom(s, agent)

    argv_a = next(w.polecenie for w in agent.wywolania if w.spec.step_id == "a")
    argv_b = next(w.polecenie for w in agent.wywolania if w.spec.step_id == "b")
    assert argv_a[0] == "claude"
    assert argv_a[argv_a.index("--model") + 1] == "m-judgment"
    assert argv_b[argv_b.index("--model") + 1] == "m-execution"
    prompt = next(w.prompt for w in agent.wywolania if w.spec.step_id == "a")
    assert ".cockpit/_run/skills/s_a/SKILL.md" in prompt
    assert "Brief." not in prompt                        # sciezka, nigdy tresc
    assert not any("SKILL.md" in x for x in argv_a)      # prompt idzie plikiem, nie argv


async def test_znak_sterujacy_w_komendzie_capability_konczy_krok_bledem_bez_uruchomienia_agenta(tmp_path):
    s = swiat(
        tmp_path, [K("a"), K("b")], opcje_skilli={"a": {"needs": ["test"]}},
        project='capabilities:\n  test: "pytest\\nrm -rf"\n')
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent)

    assert indeks(zdarzenia, "step_started", "a") < indeks(zdarzenia, "step_failed", "a")
    assert jedno(zdarzenia, "step_failed", "a")["text"] == (
        "pole komendy[test] zawiera znak sterujący: 'pytest\\nrm -rf'")
    jedno(zdarzenia, "step_finished", "b")
    assert agent.kroki == ["b"]
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_profil_bez_modelu_dla_tieru_roli_konczy_krok_bledem_a_run_idzie_dalej(tmp_path):
    profil = ProfilBackendu(modele={Tier.execution: "m-execution"})   # brak judgment
    s = swiat(
        tmp_path, [K("a"), K("b")], opcje_skilli={"b": {"role": "executor"}}, profil=profil)
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent)

    assert indeks(zdarzenia, "step_started", "a") < indeks(zdarzenia, "step_failed", "a")
    assert jedno(zdarzenia, "step_failed", "a")["text"] == "brak modelu dla tieru judgment"
    jedno(zdarzenia, "step_finished", "b")
    assert agent.kroki == ["b"]
    assert zdarzenia[-1]["kind"] == "run_finished"


# --- blad emit i anulowanie ------------------------------------------------------------------


async def test_blad_zapisu_zdarzenia_nie_jest_polykany_przez_silnik(tmp_path):
    s = swiat(tmp_path, [K("a")])
    agent = Szpieg()

    async def emit(zdarzenie: dict) -> None:
        raise BrakMiejsca("zapis bufora: brak miejsca")

    with pytest.raises(BrakMiejsca):
        await uruchom(s, agent, emit=emit)

    assert agent.wywolania == []


async def test_anulowanie_runu_w_trakcie_krokow_konczy_procesy_wszystkich_krokow(tmp_path):
    from runner import silnik

    s = swiat(tmp_path, [K("a"), K("b")])
    agent = Atrapa(s.znaczniki, spanie={"a": 60, "b": 60})
    zdarzenia: list[dict] = []

    async def emit(zdarzenie: dict) -> None:
        zdarzenia.append(zdarzenie)

    zadanie = asyncio.ensure_future(silnik.wykonaj_run(
        s.polecenie, emit, agent,
        profil=s.profil, dom=s.dom, root=s.root, zakres=s.zakres))
    pidy: list[int] = []
    try:
        await czekaj_na_warunek(
            lambda: all((s.znaczniki / f"pid_{k}").exists() for k in ("a", "b")),
            LIMIT_S, "start obu atrap")
        pidy = [int((s.znaczniki / f"pid_{k}").read_text()) for k in ("a", "b")]
        assert all(czy_zyje(pid) for pid in pidy)

        zadanie.cancel()
        with pytest.raises(asyncio.CancelledError):      # silnik przepuszcza anulowanie
            await asyncio.wait_for(zadanie, 30)

        for pid in pidy:
            await czekaj_na_warunek(
                lambda pid=pid: not czy_zyje(pid), 15, f"koniec procesu atrapy {pid}")
    finally:
        zadanie.cancel()
        await asyncio.wait({zadanie}, timeout=30)
        for pid in pidy:
            zabij_proces(pid)
    assert os.path.isdir(s.worktree)


# --- nazwa workflow: kazdy dozwolony znak i dlugosc ------------------------------------------------


@pytest.mark.parametrize("nazwa", ["a", "w_x", "w.x", "w-x", "AZ", "az", "09"])
async def test_nazwa_workflow_z_dozwolonych_znakow_jest_wykonana(tmp_path, nazwa):
    s = swiat(tmp_path, [K("a")], workflow=nazwa, tresc=tresc_workflow([K("a")], nazwa))
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent, polecenie={**s.polecenie, "workflow": nazwa})

    jedno(zdarzenia, "step_finished", "a")
    assert agent.kroki == ["a"]


# --- zakres z konfiguracji runnera ---------------------------------------------------------------------


async def test_zakres_z_konfiguracji_runnera_jest_uzyty_gdy_wolajacy_go_nie_poda(
    tmp_path, monkeypatch,
):
    s = swiat(tmp_path, [K("a")])
    konfiguracja = tmp_path / "runner.json"
    konfiguracja.write_text(json.dumps({"projekty": [str(tmp_path)]}), encoding="utf-8")
    monkeypatch.setenv("COCKPIT_RUNNER_CONFIG", str(konfiguracja))
    s.zakres = None
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent)

    jedno(zdarzenia, "step_finished", "a")
    assert agent.kroki == ["a"]


async def test_uszkodzona_konfiguracja_zakresu_konczy_run_bledem_a_nie_pustym_zakresem(
    tmp_path, monkeypatch,
):
    s = swiat(tmp_path, [K("a")])
    konfiguracja = tmp_path / "runner.json"
    konfiguracja.write_text("{niepoprawny json", encoding="utf-8")
    monkeypatch.setenv("COCKPIT_RUNNER_CONFIG", str(konfiguracja))
    s.zakres = None
    agent = Szpieg()

    tekst = sprawdz_blad_przed_startem(await uruchom(s, agent))

    # tryb scisly: uszkodzenie to osobny blad, nie zakres pusty z odmowa "poza zakresem"
    assert tekst == "konfiguracja zakresu jest nieczytelna"
    assert agent.wywolania == []
    assert not s.root.exists()


# --- bledy przed startem z dysku i z workflow --------------------------------------------------------


async def test_blad_zapisu_kopii_workflow_konczy_run_bledem_przed_startem_krokow(
    tmp_path, monkeypatch,
):
    def odmow(zrodlo, cel):
        raise PermissionError("brak praw do zapisu kopii workflow")

    monkeypatch.setattr("runner.silnik.shutil", SimpleNamespace(copyfile=odmow))
    s = swiat(tmp_path, [K("a")])
    agent = Szpieg()

    tekst = sprawdz_blad_przed_startem(await uruchom(s, agent))

    assert tekst == "brak praw do zapisu kopii workflow"
    assert agent.wywolania == []


async def test_krok_workflow_ktory_nie_jest_mapa_konczy_run_bledem_choc_reszta_przechodzi_walidacje(
    tmp_path,
):
    s = swiat(tmp_path, [K("a")], tresc="name: w\nsteps:\n  a: {skill: s_a}\n  b: 5\n")
    agent = Szpieg()

    tekst = sprawdz_blad_przed_startem(await uruchom(s, agent))

    assert tekst == "b: blad_pola: krok musi byc mapa pol"
    assert agent.wywolania == []


def test_przygotowany_run_jest_niezmienny(tmp_path):
    from runner import silnik

    s = swiat(tmp_path, [K("a")])
    przygotowany = silnik._przygotuj(s.polecenie, s.dom, s.root, s.zakres, RUN_ID)

    with pytest.raises(FrozenInstanceError):
        przygotowany.workflow = None


# --- rownoleglosc: pierwszy zakonczony, start tylko raz -------------------------------------------------


async def test_krok_zalezny_tylko_od_szybkiego_kroku_startuje_zanim_wolny_niezalezny_sie_skonczy(
    tmp_path,
):
    s = swiat(tmp_path, [K("a"), K("b"), K("c", ("a",))])
    agent = Szpieg(opoznienia={"a": 0.05, "b": 1.0})

    zdarzenia = await uruchom(s, agent)

    assert indeks(zdarzenia, "step_started", "c") < indeks(zdarzenia, "step_finished", "b")
    assert indeks(zdarzenia, "step_started", "c") > indeks(zdarzenia, "step_finished", "a")


async def test_kazdy_krok_startuje_dokladnie_raz_takze_gdy_inny_krok_konczy_sie_wczesniej(tmp_path):
    s = swiat(tmp_path, [K("a"), K("b")])
    agent = Szpieg(opoznienia={"a": 0.05, "b": 0.5})

    zdarzenia = await uruchom(s, agent)

    assert [z["step_id"] for z in zdarzenia if z["kind"] == "step_started"] == ["a", "b"]
    assert sorted(agent.kroki) == ["a", "b"]
    jedno(zdarzenia, "step_finished", "a")
    jedno(zdarzenia, "step_finished", "b")


# --- pomiar i tekst zdarzen kroku -----------------------------------------------------------------------


async def test_czas_trwania_kroku_to_roznica_odczytow_zegara_w_milisekundach(tmp_path, monkeypatch):
    from runner import silnik

    odczyty = iter([10.0, 12.5])        # start kroku, koniec kroku
    monkeypatch.setattr(silnik, "time", SimpleNamespace(monotonic=lambda: next(odczyty)))
    s = swiat(tmp_path, [K("a")])

    zdarzenia = await uruchom(s, Szpieg())

    assert jedno(zdarzenia, "step_finished", "a")["duration_ms"] == 2500


async def test_zdarzenia_kroku_niosa_pusty_tekst_a_koszt_jako_brak_pomiaru(tmp_path):
    s = swiat(tmp_path, [K("a"), K("b")])
    agent = Szpieg(wyniki={"b": WynikKroku("nieudany", "kod wyjścia 1", 1)})

    zdarzenia = await uruchom(s, agent)

    assert jedno(zdarzenia, "step_started", "a")["text"] == ""
    assert jedno(zdarzenia, "step_started", "b")["text"] == ""
    zakonczony = jedno(zdarzenia, "step_finished", "a")
    nieudany = jedno(zdarzenia, "step_failed", "b")
    assert zakonczony["text"] == ""
    assert nieudany["text"] == "kod wyjścia 1"
    for z in (zakonczony, nieudany):
        assert "cost_usd" in z and z["cost_usd"] is None      # brak pomiaru, nie zero
        assert z["cost_source"] == "unavailable"


# --- pominiecie nie blokuje zaleznych ----------------------------------------------------------------------


async def test_krok_zalezny_od_pominietego_z_braku_capability_wykonuje_sie_gdy_nie_zuzywa_jego_wyniku(
    tmp_path,
):
    s = swiat(
        tmp_path, [K("a"), K("b", ("a",))], opcje_skilli={"a": {"needs": ["deploy"]}})
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent)

    assert "brak_capability" in jedno(zdarzenia, "step_skipped", "a")["text"]
    jedno(zdarzenia, "step_finished", "b")
    assert agent.kroki == ["b"]
    assert zdarzenia[-1]["step_id"] == "b"


async def test_krok_zalezny_od_pominietego_i_od_trwajacego_wykonuje_sie_po_zakonczeniu_trwajacego(
    tmp_path,
):
    s = swiat(
        tmp_path, [K("a"), K("x"), K("c", ("a", "x"))],
        opcje_skilli={"a": {"needs": ["deploy"]}})
    agent = Szpieg(opoznienia={"x": 0.1})

    zdarzenia = await uruchom(s, agent)

    assert "brak_capability" in jedno(zdarzenia, "step_skipped", "a")["text"]
    jedno(zdarzenia, "step_started", "c")
    jedno(zdarzenia, "step_finished", "c")
    assert indeks(zdarzenia, "step_started", "c") > indeks(zdarzenia, "step_finished", "x")
    assert sorted(agent.kroki) == ["c", "x"]


# --- domyslny profil i katalog domowy -------------------------------------------------------------------------


async def test_model_ze_srodowiska_runnera_trafia_do_polecenia_agenta_gdy_wolajacy_nie_poda_profilu(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COCKPIT_BACKENDY", str(tmp_path / "brak-profilu.json"))
    monkeypatch.setenv("COCKPIT_MODEL_JUDGMENT", "m-ze-srodowiska")
    s = swiat(tmp_path, [K("a")])
    s.profil = None
    agent = Szpieg()

    await uruchom(s, agent)

    (wywolanie,) = agent.wywolania
    argv = wywolanie.polecenie
    assert argv[argv.index("--model") + 1] == "m-ze-srodowiska"


async def test_skill_z_katalogu_domowego_uzytkownika_jest_widoczny_gdy_wolajacy_nie_poda_katalogu(
    tmp_path, monkeypatch,
):
    s = swiat(tmp_path, [K("a", skill="s_domowy")])
    napisz_skill(s.dom, "s_domowy")
    monkeypatch.setenv("HOME", str(s.dom))
    s.dom = None
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent)

    jedno(zdarzenia, "step_finished", "a")
    assert agent.kroki == ["a"]


async def test_skill_z_podanego_katalogu_domowego_jest_widoczny_mimo_innego_prawdziwego_domu(
    tmp_path,
):
    s = swiat(tmp_path, [K("a", skill="s_tylko_w_podanym_domu")])
    napisz_skill(s.dom, "s_tylko_w_podanym_domu")      # prawdziwy ~ go nie ma
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent)

    jedno(zdarzenia, "step_finished", "a")
    assert agent.kroki == ["a"]


# --- blad zapisu zdarzenia przy zakonczeniu jednego z krokow ---------------------------------------------


async def test_blad_zapisu_przy_koncu_jednego_kroku_konczy_procesy_pozostalych_i_wraca_do_wolajacego(
    tmp_path,
):
    s = swiat(tmp_path, [K("a"), K("b")])
    agent = Atrapa(s.znaczniki, spanie={"a": 0.1, "b": 60})
    blad = BrakMiejsca("zapis bufora: brak miejsca")
    plik_pid_b = s.znaczniki / "pid_b"

    async def emit(zdarzenie: dict) -> None:
        if zdarzenie["kind"] == "step_finished" and zdarzenie["step_id"] == "a":
            # krok b trwa i ma proces, zanim zapis konczacy krok a sie nie powiedzie
            await czekaj_na_warunek(plik_pid_b.exists, LIMIT_S, "start procesu kroku b")
            raise blad

    try:
        with pytest.raises(BrakMiejsca) as info:
            await uruchom(s, agent, emit=emit)
        assert info.value is blad                 # ten wyjatek, nie anulowanie kroku b
        # w chwili powrotu proces kroku b juz nie dziala (nikt tu nie czeka)
        assert not czy_zyje(int(plik_pid_b.read_text()))
    finally:
        if plik_pid_b.exists():
            zabij_proces(int(plik_pid_b.read_text()))
