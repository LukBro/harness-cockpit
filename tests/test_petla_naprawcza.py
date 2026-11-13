"""HAR-109: petla naprawcza `on_fail: {retry, max}` w silniku runu (TDD).

Krok nieudany z `on_fail: {retry: <cel>, max: M}` wraca do celu (wszystkie kroki na sciezce
`needs` od celu do kroku nieudanego), z raportem porazki jako wejsciem celu, co najwyzej M razy;
po wyczerpaniu limitu run konczy sie jawnie (`step_failed` z powodem wyczerpania i
`run_finished`), nie w ciszy i nie na bramce. Kazde zdarzenie kroku niesie numer proby
(`attempt`). Rozstrzygniecie D28.

Prawdziwy kod: `runner.silnik.wykonaj_run` i `wznow_run` na prawdziwym repo git z
`tests/test_silnik_runu.py` (swiat, K, uruchom). Zaslepiony jest tylko agent: `AgentNaprawczy`
zlicza wywolania per krok, pada zadana liczbe razy, emituje linie `output` i zapisuje prompty.
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import asyncio
import json
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from cockpit.services.invariants import Tier
from cockpit.services.skills import rozstrzygnij_rejestr
from cockpit.services.workflow import wczytaj_workflow
from cockpit.services.workflow_walidacja import waliduj_workflow
from runner.agent import ProfilBackendu
from runner.checkpoint import (
    Checkpoint, StanKrokuCheckpointu, wczytaj_checkpoint, zapisz_checkpoint)
from runner.krok import WynikKroku
from test_bramka_zatwierdzenia import PREFIKS_BLEDU_ZAPISU, _zepsuj_zapis_checkpointu
from test_silnik_runu import RUN_ID, K, rodzaje, swiat, tresc_workflow, uruchom

LIMIT_S = 30
KOD_PORAZKI = 42
BRAMKA = ", approval: true"
TEKST_CZEKANIA = "czeka na decyzję człowieka (approval)"
BEZ_WYJSCIA = ("step_started", "step_finished", "step_failed", "step_skipped", "step_waiting")

CEL = "implementuj"
NIEUDANY = "testuj"


def powod_porazki(nr: int) -> str:
    return f"padły testy jednostkowe w próbie {nr}"


def tekst_wyczerpania(max: int, krok: str = NIEUDANY) -> str:
    return f"wyczerpany limit {max} ponowień kroku {krok}"


# --- atrapa agenta -------------------------------------------------------------------------


@dataclass
class AgentNaprawczy:
    """Agent bez procesu: licznik wywolan per krok, `padaja[krok]` pierwszych wywolan nieudanych."""

    padaja: dict[str, int] = field(default_factory=dict)
    werdykty: dict[str, str] = field(default_factory=dict)
    linie_przed: dict[str, int] = field(default_factory=dict)    # dodatkowe linie `linia-NN` per krok
    bez_kodu: frozenset[str] = frozenset()                       # kroki nieudane bez kodu wyjscia
    bez_tekstu: frozenset[str] = frozenset()                     # kroki z `output` bez klucza `text`
    padaja_w: dict[str, frozenset[int]] = field(default_factory=dict)   # krok -> numery wywolan
    wywolania: list = field(default_factory=list)

    async def __call__(
        self, spec, polecenie, katalog, emit, *, katalog_runu=None, prompt=None, **dodatkowe
    ):
        krok = spec.step_id
        nr = self.starty(krok) + 1
        self.wywolania.append(SimpleNamespace(krok=krok, nr=nr, prompt=prompt))
        pada = nr <= self.padaja.get(krok, 0) or nr in self.padaja_w.get(krok, frozenset())
        if krok in self.bez_tekstu:
            zdarzenie = self._output(spec, "")
            del zdarzenie["text"]
            await emit(zdarzenie)
        for i in range(1, self.linie_przed.get(krok, 0) + 1):
            await emit(self._output(spec, f"linia-{i:02d}"))
        await emit(self._output(
            spec, f"{krok}: wynik wywołania {nr}: {'czerwone' if pada else 'zielone'}"))
        if pada:
            return WynikKroku(
                status="nieudany", powod=powod_porazki(nr),
                kod_wyjscia=None if krok in self.bez_kodu else KOD_PORAZKI,
                werdykt=self.werdykty.get(krok))
        return WynikKroku(status="zakonczony", powod=None, kod_wyjscia=0)

    @staticmethod
    def _output(spec, tekst: str) -> dict:
        return {
            "kind": "output", "run_id": spec.run_id, "step_id": spec.step_id,
            "agent_id": spec.agent_id, "text": tekst,
        }

    def starty(self, krok: str) -> int:
        return sum(1 for w in self.wywolania if w.krok == krok)

    def prompt(self, krok: str, nr: int) -> str:
        (wywolanie,) = [w for w in self.wywolania if w.krok == krok and w.nr == nr]
        return wywolanie.prompt


# --- swiaty --------------------------------------------------------------------------------


def swiat_naprawy(tmp_path: Path, max: int = 3, **opcje):
    """plan -> implementuj -> przeglad -> testuj (retry: implementuj) -> wdroz; dokumentacja osobno."""
    return swiat(tmp_path, [
        K("plan"),
        K(CEL, ("plan",)),
        K("przeglad", (CEL,)),
        K(NIEUDANY, ("przeglad",), extra=f", on_fail: {{retry: {CEL}, max: {max}}}"),
        K("wdroz", (NIEUDANY,)),
        K("dokumentacja"),
    ], **opcje)


def swiat_z_bramka(tmp_path: Path, max: int = 3):
    """implementuj -> zatwierdz (approval) -> testuj (retry: implementuj) -> wdroz."""
    return swiat(tmp_path, [
        K(CEL),
        K("zatwierdz", (CEL,), extra=BRAMKA),
        K(NIEUDANY, ("zatwierdz",), extra=f", on_fail: {{retry: {CEL}, max: {max}}}"),
        K("wdroz", (NIEUDANY,)),
    ])


async def wznow(s, agent, krok: str = "zatwierdz", decyzja: str = "approve") -> list[dict]:
    from runner import silnik   # import leniwy: brak funkcji czerwieni kazdy test osobno

    zdarzenia: list[dict] = []

    async def zapisz(zdarzenie: dict) -> None:
        zdarzenia.append(zdarzenie)

    await asyncio.wait_for(
        silnik.wznow_run(
            RUN_ID, krok, decyzja, zapisz, agent, profil=s.profil, dom=s.dom, root=s.root),
        LIMIT_S)
    return zdarzenia


async def uruchom_swiat(s, agent) -> list[dict]:
    return await uruchom(s, agent, limit=LIMIT_S)


def raport(s, cel: str, nr: int) -> Path:
    return s.worktree / ".cockpit" / "_run" / "artifacts" / cel / "_naprawa" / f"proba_{nr}.md"


def sciezka_raportu(cel: str, nr: int) -> str:
    return f".cockpit/_run/artifacts/{cel}/_naprawa/proba_{nr}.md"


def przebieg(zdarzenia: list[dict], krok: str) -> list[tuple[str, int]]:
    """(rodzaj, attempt) zdarzen kroku bez `output`, w kolejnosci."""
    return [
        (z["kind"], z["attempt"]) for z in zdarzenia
        if z["step_id"] == krok and z["kind"] in BEZ_WYJSCIA]


def linie_wejscia(prompt: str) -> list[str]:
    linie = prompt.splitlines()
    return linie[linie.index("wejście:") + 1: linie.index("wyjście:")]


def tekst_raportu(s, cel: str, nr: int) -> str:
    plik = raport(s, cel, nr)
    assert plik.is_file(), f"brak raportu porazki {plik}"
    return plik.read_text(encoding="utf-8")


# --- liczba prob: `max` to liczba PONOWIEN -----------------------------------------------------


async def test_testy_padajace_dwa_razy_przechodza_w_trzeciej_probie(tmp_path):
    s = swiat_naprawy(tmp_path, max=3)
    agent = AgentNaprawczy(padaja={NIEUDANY: 2})

    zdarzenia = await uruchom_swiat(s, agent)

    assert [agent.starty(k) for k in (CEL, "przeglad", NIEUDANY)] == [3, 3, 3]
    assert przebieg(zdarzenia, NIEUDANY)[-1] == ("step_finished", 3)
    assert zdarzenia[-1]["kind"] == "run_finished"
    assert agent.starty("wdroz") == 1


async def test_krok_zalezny_od_nieudanego_nie_jest_pomijany_gdy_ponowienie_jeszcze_jest_mozliwe(tmp_path):
    s = swiat_naprawy(tmp_path, max=3)

    zdarzenia = await uruchom_swiat(s, AgentNaprawczy(padaja={NIEUDANY: 2}))

    assert "step_skipped" not in rodzaje(zdarzenia)
    assert przebieg(zdarzenia, "wdroz") == [("step_started", 1), ("step_finished", 1)]


async def test_porazki_posrednie_maja_tekst_powodu_a_nie_wyczerpania(tmp_path):
    s = swiat_naprawy(tmp_path, max=3)

    zdarzenia = await uruchom_swiat(s, AgentNaprawczy(padaja={NIEUDANY: 2}))

    porazki = [z["text"] for z in zdarzenia if z["kind"] == "step_failed"]
    assert porazki == [powod_porazki(1), powod_porazki(2)]


async def test_testy_padajace_dokladnie_tyle_razy_ile_wynosi_limit_przechodza_w_ostatniej_probie(tmp_path):
    s = swiat_naprawy(tmp_path, max=2)
    agent = AgentNaprawczy(padaja={NIEUDANY: 2})

    zdarzenia = await uruchom_swiat(s, agent)

    assert agent.starty(NIEUDANY) == 3
    assert przebieg(zdarzenia, NIEUDANY)[-1] == ("step_finished", 3)
    assert all(tekst_wyczerpania(2) != z["text"] for z in zdarzenia)
    assert zdarzenia[-1]["kind"] == "run_finished"


@pytest.mark.parametrize("max", [1, 2, 3])
async def test_krok_startuje_najwyzej_max_plus_jeden_razy_gdy_testy_padaja_zawsze(tmp_path, max):
    s = swiat_naprawy(tmp_path, max=max)
    agent = AgentNaprawczy(padaja={NIEUDANY: 99})

    await uruchom_swiat(s, agent)

    assert agent.starty(NIEUDANY) == max + 1
    assert agent.starty(CEL) == max + 1
    assert agent.starty("przeglad") == max + 1


@pytest.mark.parametrize("max", [1, 2, 3])
async def test_wyczerpanie_limitu_konczy_run_porazka_kroku_z_powodem_wyczerpania(tmp_path, max):
    s = swiat_naprawy(tmp_path, max=max)

    zdarzenia = await uruchom_swiat(s, AgentNaprawczy(padaja={NIEUDANY: 99}))

    ostatnia_porazka = [
        z for z in zdarzenia if z["kind"] == "step_failed" and z["step_id"] == NIEUDANY][-1]
    assert ostatnia_porazka["text"] == tekst_wyczerpania(max)
    assert ostatnia_porazka["attempt"] == max + 1
    # ostatnie zdarzenie kroku to porazka: kokpit nie ma jak policzyc go jako zakonczonego
    assert [k for k, _ in przebieg(zdarzenia, NIEUDANY)][-1] == "step_failed"


async def test_wyczerpanie_limitu_konczy_run_zdarzeniem_run_finished_a_nie_zawieszeniem(tmp_path):
    s = swiat_naprawy(tmp_path, max=2)

    zdarzenia = await uruchom_swiat(s, AgentNaprawczy(padaja={NIEUDANY: 99}))

    assert zdarzenia[-1]["kind"] == "run_finished"
    assert rodzaje(zdarzenia).count("run_finished") == 1
    assert "step_waiting" not in rodzaje(zdarzenia)


async def test_po_wyczerpaniu_limitu_krok_zalezny_od_nieudanego_jest_pominiety_z_numerem_proby(tmp_path):
    s = swiat_naprawy(tmp_path, max=2)
    agent = AgentNaprawczy(padaja={NIEUDANY: 99})

    zdarzenia = await uruchom_swiat(s, agent)

    pominiete = [z for z in zdarzenia if z["kind"] == "step_skipped"]
    assert [z["step_id"] for z in pominiete] == ["wdroz"]
    assert pominiete[0]["text"].startswith("zaleznosc_nieudana: ")
    assert pominiete[0]["attempt"] == 1       # wdroz nie startowal: nastepna proba to 1
    assert agent.starty("wdroz") == 0


# --- zakres powrotu: sciezka needs od celu do kroku nieudanego ------------------------------------


async def test_wracaja_tylko_kroki_ze_sciezki_od_celu_do_nieudanego_a_niezalezne_i_wczesniejsze_nie(tmp_path):
    s = swiat_naprawy(tmp_path, max=3)
    agent = AgentNaprawczy(padaja={NIEUDANY: 2})

    await uruchom_swiat(s, agent)

    assert agent.starty("plan") == 1            # przodek celu, poza sciezka
    assert agent.starty("dokumentacja") == 1    # krok niezalezny
    assert agent.starty(CEL) == 3


async def test_ponowny_start_kroku_ze_sciezki_dostaje_kolejny_numer_proby(tmp_path):
    s = swiat_naprawy(tmp_path, max=3)

    zdarzenia = await uruchom_swiat(s, AgentNaprawczy(padaja={NIEUDANY: 2}))

    assert przebieg(zdarzenia, CEL) == [
        ("step_started", 1), ("step_finished", 1), ("step_started", 2), ("step_finished", 2),
        ("step_started", 3), ("step_finished", 3)]
    assert przebieg(zdarzenia, NIEUDANY) == [
        ("step_started", 1), ("step_failed", 1), ("step_started", 2), ("step_failed", 2),
        ("step_started", 3), ("step_finished", 3)]


async def test_cel_startuje_ponownie_dopiero_po_porazce_kroku_nieudanego(tmp_path):
    s = swiat_naprawy(tmp_path, max=3)

    zdarzenia = await uruchom_swiat(s, AgentNaprawczy(padaja={NIEUDANY: 1}))

    opis = [(z["kind"], z["step_id"]) for z in zdarzenia if z["kind"] in BEZ_WYJSCIA]
    porazka = opis.index(("step_failed", NIEUDANY))
    drugi_start_celu = [i for i, o in enumerate(opis) if o == ("step_started", CEL)][1]
    assert porazka < drugi_start_celu


# --- numer proby w zdarzeniach --------------------------------------------------------------------


async def test_zdarzenia_output_niosa_numer_biezacej_proby_kroku(tmp_path):
    s = swiat_naprawy(tmp_path, max=3)

    zdarzenia = await uruchom_swiat(s, AgentNaprawczy(padaja={NIEUDANY: 2}))

    wyjscia = [(z["text"], z["attempt"]) for z in zdarzenia
               if z["kind"] == "output" and z["step_id"] == NIEUDANY]
    assert wyjscia == [
        (f"{NIEUDANY}: wynik wywołania 1: czerwone", 1),
        (f"{NIEUDANY}: wynik wywołania 2: czerwone", 2),
        (f"{NIEUDANY}: wynik wywołania 3: zielone", 3)]


async def test_krok_bez_ponowienia_niesie_numer_proby_jeden_w_kazdym_zdarzeniu(tmp_path):
    s = swiat(tmp_path, [K("plan"), K("zrob", ("plan",))])

    zdarzenia = await uruchom_swiat(s, AgentNaprawczy())

    for krok in ("plan", "zrob"):
        assert przebieg(zdarzenia, krok) == [("step_started", 1), ("step_finished", 1)]
        assert [z["attempt"] for z in zdarzenia if z["kind"] == "output" and z["step_id"] == krok] == [1]


async def test_krok_pominiety_z_braku_capability_niesie_numer_proby_jeden(tmp_path):
    s = swiat(tmp_path, [K("a")], opcje_skilli={"a": {"needs": ["deploy"]}})

    zdarzenia = await uruchom_swiat(s, AgentNaprawczy())

    assert przebieg(zdarzenia, "a") == [("step_skipped", 1)]


# --- raport porazki jako artefakt -------------------------------------------------------------------


async def test_pierwsza_porazka_zapisuje_raport_proba_1_w_katalogu_celu_w_worktree(tmp_path):
    s = swiat_naprawy(tmp_path, max=3)

    await uruchom_swiat(s, AgentNaprawczy(padaja={NIEUDANY: 1}))

    assert raport(s, CEL, 1).is_file()
    assert not raport(s, CEL, 2).exists()
    # raport lezy w worktree runu, nie w repozytorium uzytkownika
    assert not (s.repo / ".cockpit" / "_run").exists()


async def test_kazda_porazka_dostaje_osobny_raport_z_numerem_proby_celu(tmp_path):
    s = swiat_naprawy(tmp_path, max=3)

    await uruchom_swiat(s, AgentNaprawczy(padaja={NIEUDANY: 2}))

    assert raport(s, CEL, 1).is_file()
    assert raport(s, CEL, 2).is_file()
    assert not raport(s, CEL, 3).exists()       # trzecia proba sie powiodla


async def test_raport_porazki_zawiera_id_kroku_kod_wyjscia_i_powod(tmp_path):
    s = swiat_naprawy(tmp_path, max=3)

    await uruchom_swiat(s, AgentNaprawczy(padaja={NIEUDANY: 1}))

    tekst = tekst_raportu(s, CEL, 1)
    assert NIEUDANY in tekst
    assert str(KOD_PORAZKI) in tekst
    assert powod_porazki(1) in tekst


async def test_raport_porazki_zawiera_ogon_logu_kroku_nieudanego_z_tej_proby(tmp_path):
    s = swiat_naprawy(tmp_path, max=3)

    await uruchom_swiat(s, AgentNaprawczy(padaja={NIEUDANY: 2}))

    pierwszy, drugi = tekst_raportu(s, CEL, 1), tekst_raportu(s, CEL, 2)
    assert f"{NIEUDANY}: wynik wywołania 1: czerwone" in pierwszy
    assert "wynik wywołania 2" not in pierwszy
    assert f"{NIEUDANY}: wynik wywołania 2: czerwone" in drugi
    assert "wynik wywołania 1" not in drugi


async def test_raport_porazki_nie_zawiera_logu_innych_krokow(tmp_path):
    s = swiat_naprawy(tmp_path, max=3)

    await uruchom_swiat(s, AgentNaprawczy(padaja={NIEUDANY: 1}))

    tekst = tekst_raportu(s, CEL, 1)
    assert f"{CEL}: wynik wywołania" not in tekst
    assert "przeglad: wynik wywołania" not in tekst


async def test_raport_porazki_bez_werdyktu_mowi_brak(tmp_path):
    s = swiat_naprawy(tmp_path, max=3)

    await uruchom_swiat(s, AgentNaprawczy(padaja={NIEUDANY: 1}))

    wszystkie = tekst_raportu(s, CEL, 1).splitlines()
    linie = [linia.lower() for linia in wszystkie if "werdykt" in linia.lower()]
    assert linie, "raport nie ma linii o werdykcie"
    assert any("brak" in linia for linia in linie)


async def test_raport_porazki_niesie_werdykt_kroku_nieudanego(tmp_path):
    s = swiat_naprawy(tmp_path, max=3)
    agent = AgentNaprawczy(padaja={NIEUDANY: 1}, werdykty={NIEUDANY: "ODRZUCONO_PRZEZ_TESTY"})

    await uruchom_swiat(s, agent)

    tekst = tekst_raportu(s, CEL, 1)
    assert "ODRZUCONO_PRZEZ_TESTY" in tekst


async def test_raport_ostatniej_porazki_powstaje_takze_przy_wyczerpaniu_limitu(tmp_path):
    s = swiat_naprawy(tmp_path, max=2)

    await uruchom_swiat(s, AgentNaprawczy(padaja={NIEUDANY: 99}))

    assert [raport(s, CEL, n).is_file() for n in (1, 2, 3)] == [True, True, True]
    assert not raport(s, CEL, 4).exists()
    ostatni = tekst_raportu(s, CEL, 3)
    assert powod_porazki(3) in ostatni
    assert f"{NIEUDANY}: wynik wywołania 3: czerwone" in ostatni
    assert str(KOD_PORAZKI) in ostatni


# --- raport porazki w prompcie celu -------------------------------------------------------------------


async def test_pierwsza_proba_celu_nie_ma_w_prompcie_sciezki_raportu_naprawy(tmp_path):
    s = swiat_naprawy(tmp_path, max=3)
    agent = AgentNaprawczy(padaja={NIEUDANY: 2})

    await uruchom_swiat(s, agent)

    assert "_naprawa" not in agent.prompt(CEL, 1)


async def test_druga_proba_celu_dostaje_w_wejsciu_sciezke_raportu_pierwszej_porazki(tmp_path):
    s = swiat_naprawy(tmp_path, max=3)
    agent = AgentNaprawczy(padaja={NIEUDANY: 2})

    await uruchom_swiat(s, agent)

    assert f"- {sciezka_raportu(CEL, 1)}" in linie_wejscia(agent.prompt(CEL, 2))


async def test_trzecia_proba_celu_dostaje_ostatni_raport_a_nie_pierwszy(tmp_path):
    s = swiat_naprawy(tmp_path, max=3)
    agent = AgentNaprawczy(padaja={NIEUDANY: 2})

    await uruchom_swiat(s, agent)

    wejscia = linie_wejscia(agent.prompt(CEL, 3))
    assert f"- {sciezka_raportu(CEL, 2)}" in wejscia
    assert not any("proba_1" in linia for linia in wejscia)


async def test_prompt_niesie_sciezke_raportu_a_nie_jego_tresc(tmp_path):
    s = swiat_naprawy(tmp_path, max=3)
    agent = AgentNaprawczy(padaja={NIEUDANY: 1})

    await uruchom_swiat(s, agent)

    prompt = agent.prompt(CEL, 2)
    assert powod_porazki(1) not in prompt
    assert str(KOD_PORAZKI) not in prompt


async def test_krok_posredni_na_sciezce_nie_dostaje_raportu_w_prompcie(tmp_path):
    s = swiat_naprawy(tmp_path, max=3)
    agent = AgentNaprawczy(padaja={NIEUDANY: 2})

    await uruchom_swiat(s, agent)

    for krok in ("przeglad", NIEUDANY):
        for nr in (1, 2, 3):
            assert "_naprawa" not in agent.prompt(krok, nr)


# --- krok bez on_fail -----------------------------------------------------------------------------------


async def test_krok_bez_on_fail_po_porazce_nie_jest_ponawiany_i_nie_ma_raportu_naprawy(tmp_path):
    s = swiat(tmp_path, [K("plan"), K(CEL, ("plan",)), K(NIEUDANY, (CEL,)), K("wdroz", (NIEUDANY,))])
    agent = AgentNaprawczy(padaja={NIEUDANY: 1})

    zdarzenia = await uruchom_swiat(s, agent)

    assert [agent.starty(k) for k in ("plan", CEL, NIEUDANY, "wdroz")] == [1, 1, 1, 0]
    assert [z["text"] for z in zdarzenia if z["kind"] == "step_failed"] == [powod_porazki(1)]
    assert przebieg(zdarzenia, "wdroz") == [("step_skipped", 1)]
    assert zdarzenia[-1]["kind"] == "run_finished"
    assert list(s.worktree.glob(".cockpit/_run/artifacts/**/_naprawa")) == []


# --- bramka approval na sciezce powtorzenia --------------------------------------------------------------


async def test_pierwsze_zatrzymanie_na_bramce_niesie_numer_proby_jeden(tmp_path):
    s = swiat_z_bramka(tmp_path)

    zdarzenia = await uruchom_swiat(s, AgentNaprawczy())

    assert zdarzenia[-1]["kind"] == "step_waiting"
    assert zdarzenia[-1]["step_id"] == "zatwierdz"
    assert zdarzenia[-1]["attempt"] == 1


async def test_powrot_przez_krok_z_bramka_zatrzymuje_run_na_oczekiwaniu_z_numerem_nastepnej_proby(tmp_path):
    s = swiat_z_bramka(tmp_path)
    agent = AgentNaprawczy(padaja={NIEUDANY: 1})
    await uruchom_swiat(s, agent)

    zdarzenia = await wznow(s, agent)

    ostatnie = zdarzenia[-1]
    assert (ostatnie["kind"], ostatnie["step_id"], ostatnie["attempt"]) == ("step_waiting", "zatwierdz", 2)
    assert ostatnie["text"] == TEKST_CZEKANIA
    assert "run_finished" not in rodzaje(zdarzenia)
    assert "step_skipped" not in rodzaje(zdarzenia)


async def test_powrot_przez_bramke_nie_startuje_ponownie_kroku_z_bramka_bez_nowej_decyzji(tmp_path):
    s = swiat_z_bramka(tmp_path)
    agent = AgentNaprawczy(padaja={NIEUDANY: 1})
    await uruchom_swiat(s, agent)

    zdarzenia = await wznow(s, agent)

    assert przebieg(zdarzenia, "zatwierdz") == [
        ("step_started", 1), ("step_finished", 1), ("step_waiting", 2)]
    assert agent.starty("zatwierdz") == 1
    assert agent.starty(NIEUDANY) == 1


async def test_powrot_przez_bramke_cofa_krok_celu_przed_zatrzymaniem_na_bramce(tmp_path):
    s = swiat_z_bramka(tmp_path)
    agent = AgentNaprawczy(padaja={NIEUDANY: 1})
    await uruchom_swiat(s, agent)

    zdarzenia = await wznow(s, agent)

    opis = [(z["kind"], z["step_id"], z["attempt"]) for z in zdarzenia if z["kind"] in BEZ_WYJSCIA]
    porazka = opis.index(("step_failed", NIEUDANY, 1))
    start_celu = opis.index(("step_started", CEL, 2))
    assert porazka < start_celu < opis.index(("step_waiting", "zatwierdz", 2))
    assert f"- {sciezka_raportu(CEL, 1)}" in linie_wejscia(agent.prompt(CEL, 2))


async def test_po_nowej_decyzji_krok_z_bramka_startuje_z_kolejnym_numerem_proby_a_licznik_nie_wraca_do_jedynki(tmp_path):
    s = swiat_z_bramka(tmp_path)
    agent = AgentNaprawczy(padaja={NIEUDANY: 1})
    await uruchom_swiat(s, agent)
    await wznow(s, agent)

    zdarzenia = await wznow(s, agent)

    assert przebieg(zdarzenia, "zatwierdz")[0] == ("step_started", 2)
    assert przebieg(zdarzenia, NIEUDANY) == [("step_started", 2), ("step_finished", 2)]
    assert przebieg(zdarzenia, "wdroz") == [("step_started", 1), ("step_finished", 1)]
    assert zdarzenia[-1]["kind"] == "run_finished"
    assert agent.starty(CEL) == 2               # cel wrocil raz, przed pierwszym zatrzymaniem


async def test_checkpoint_zatrzymanego_na_bramce_runu_zachowuje_liczbe_prob_kazdego_kroku(tmp_path):
    s = swiat_z_bramka(tmp_path)
    agent = AgentNaprawczy(padaja={NIEUDANY: 1})
    await uruchom_swiat(s, agent)
    await wznow(s, agent)

    cp = wczytaj_checkpoint(s.katalog_runu)

    assert {k: v.proby for k, v in cp.kroki.items()} == {
        CEL: 2, "zatwierdz": 1, NIEUDANY: 1, "wdroz": 0}
    assert {k: v.stan for k, v in cp.kroki.items()} == {
        CEL: "zakonczony", "zatwierdz": "oczekuje", NIEUDANY: "oczekuje", "wdroz": "oczekuje"}
    assert cp.waiting_on is not None and cp.waiting_on.step_id == "zatwierdz"


async def test_wyczerpanie_limitu_na_sciezce_z_bramka_konczy_run_a_nie_zawiesza_go_na_bramce(tmp_path):
    s = swiat_z_bramka(tmp_path, max=1)
    agent = AgentNaprawczy(padaja={NIEUDANY: 99})
    await uruchom_swiat(s, agent)
    pierwsze = await wznow(s, agent)
    assert pierwsze[-1]["kind"] == "step_waiting"     # limit jeszcze nie wyczerpany

    zdarzenia = await wznow(s, agent)

    assert zdarzenia[-1]["kind"] == "run_finished"
    assert "step_waiting" not in rodzaje(zdarzenia)
    ostatnia = [z for z in zdarzenia if z["kind"] == "step_failed" and z["step_id"] == NIEUDANY][-1]
    assert ostatnia["text"] == tekst_wyczerpania(1)
    assert agent.starty(NIEUDANY) == 2
    assert raport(s, CEL, 2).is_file()


# --- walidator: retry tylko wstecz (istniejaca regula RETRY_NIE_WSTECZ) -----------------------------------


def _problemy_workflow(tmp_path, kroki: list[K]):
    s = swiat(tmp_path, kroki, tresc=tresc_workflow(kroki))
    workflow, problemy = wczytaj_workflow(s.repo / ".cockpit" / "workflows" / "w.yaml")
    assert workflow is not None and problemy == []
    return waliduj_workflow(workflow, rozstrzygnij_rejestr(s.repo, s.dom), s.dom)


def test_workflow_ponawiajacy_implementacje_po_testach_jest_poprawny(tmp_path):
    problemy = _problemy_workflow(tmp_path, [
        K("implementuj"),
        K("testuj", ("implementuj",), extra=", on_fail: {retry: implementuj, max: 2}")])

    assert [p.kod for p in problemy if p.kod == "retry_nie_wstecz"] == []


def test_powrot_do_kroku_pozniejszego_w_petli_naprawczej_jest_odrzucony_jako_retry_nie_wstecz(tmp_path):
    problemy = _problemy_workflow(tmp_path, [
        K("implementuj", extra=", on_fail: {retry: testuj, max: 2}"),
        K("testuj", ("implementuj",))])

    assert [(p.krok, p.kod) for p in problemy if p.kod == "retry_nie_wstecz"] == [
        ("implementuj", "retry_nie_wstecz")]


def test_powrot_kroku_do_samego_siebie_w_petli_naprawczej_jest_odrzucony_jako_retry_nie_wstecz(tmp_path):
    problemy = _problemy_workflow(tmp_path, [
        K("implementuj"),
        K("testuj", ("implementuj",), extra=", on_fail: {retry: testuj, max: 2}")])

    assert [(p.krok, p.kod) for p in problemy if p.kod == "retry_nie_wstecz"] == [
        ("testuj", "retry_nie_wstecz")]


# --- gałęzie błędów pętli naprawczej (na prawdziwym kodzie silnika) -------------------------------------


def _koniec_runu_bez_ponowienia(zdarzenia: list[dict], agent: AgentNaprawczy) -> None:
    assert zdarzenia[-1]["kind"] == "run_finished"
    assert agent.starty(NIEUDANY) == 1
    assert agent.starty(CEL) == 1


async def test_cel_ponowienia_ktory_nie_jest_przodkiem_konczy_porazka_z_komunikatem_bez_ponowienia(
    tmp_path, monkeypatch,
):
    def zly_cel(workflow, cel, krok):
        raise ValueError("cel nie leży na ścieżce")

    monkeypatch.setattr("runner.silnik.kroki_do_powtorzenia", zly_cel)
    s = swiat_naprawy(tmp_path, max=3)
    agent = AgentNaprawczy(padaja={NIEUDANY: 99})

    zdarzenia = await uruchom_swiat(s, agent)

    porazki = [z for z in zdarzenia if z["kind"] == "step_failed"]
    assert [z["text"] for z in porazki] == [f"nie można ponowić kroku {NIEUDANY}: cel nie leży na ścieżce"]
    _koniec_runu_bez_ponowienia(zdarzenia, agent)
    assert list(s.worktree.glob(".cockpit/_run/artifacts/**/_naprawa")) == []


@pytest.mark.parametrize("typ", [PermissionError, IsADirectoryError])
async def test_blad_zapisu_raportu_porazki_konczy_porazka_z_nazwa_typu_bez_ponowienia(
    tmp_path, monkeypatch, typ,
):
    from runner import silnik

    oryginal = silnik._zapisz_atomowo

    def zapis(cel, tresc):
        if "_naprawa" in str(cel):
            raise typ("brak zapisu")
        oryginal(cel, tresc)

    monkeypatch.setattr("runner.silnik._zapisz_atomowo", zapis)
    s = swiat_naprawy(tmp_path, max=3)
    agent = AgentNaprawczy(padaja={NIEUDANY: 99})

    zdarzenia = await uruchom_swiat(s, agent)

    porazki = [z for z in zdarzenia if z["kind"] == "step_failed"]
    assert [z["text"] for z in porazki] == [f"nie zapisano raportu porażki ({typ.__name__})"]
    _koniec_runu_bez_ponowienia(zdarzenia, agent)


async def test_nieczytelny_raport_poprzedniej_proby_konczy_porazka_celu_zamiast_startu_agenta(
    tmp_path, monkeypatch,
):
    oryginal = Path.stat

    def stat(self, *argumenty, **opcje):
        if "_naprawa" in str(self):
            raise PermissionError("brak praw")
        return oryginal(self, *argumenty, **opcje)

    monkeypatch.setattr(Path, "stat", stat)
    s = swiat_naprawy(tmp_path, max=3)
    agent = AgentNaprawczy(padaja={NIEUDANY: 99})

    zdarzenia = await uruchom_swiat(s, agent)

    porazka_celu = [z for z in zdarzenia if z["kind"] == "step_failed" and z["step_id"] == CEL]
    assert [(z["text"], z["attempt"]) for z in porazka_celu] == [
        ("nie sprawdzono raportu porażki (PermissionError)", 2)]
    assert agent.starty(CEL) == 1            # druga proba celu nie dotarla do agenta
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_brak_raportu_poprzedniej_proby_nie_blokuje_celu_a_prompt_nie_ma_sciezki(
    tmp_path, monkeypatch,
):
    oryginal = Path.stat

    def stat(self, *argumenty, **opcje):
        if "_naprawa" in str(self):
            raise FileNotFoundError("raport zniknął")
        return oryginal(self, *argumenty, **opcje)

    monkeypatch.setattr(Path, "stat", stat)
    s = swiat_naprawy(tmp_path, max=3)
    agent = AgentNaprawczy(padaja={NIEUDANY: 1})

    zdarzenia = await uruchom_swiat(s, agent)

    assert agent.starty(CEL) == 2
    assert "_naprawa" not in agent.prompt(CEL, 2)
    assert "step_failed" not in [z["kind"] for z in zdarzenia if z["step_id"] == CEL]


# --- ogon wyjścia komendy dla kroku check ----------------------------------------------------------------------

PROJEKT_Z_CHECKIEM = "stack: [python]\ncapabilities:\n  typecheck: {komenda}\n"


def _komenda(kod: str) -> str:
    return " ".join([shlex.quote(sys.executable), "-c", shlex.quote(kod)])


def swiat_z_checkiem(tmp_path: Path):
    """implementuj -> sprawdz (system: check, capability typecheck, retry: implementuj, max 1)."""
    komenda = _komenda("print('ostatnia-linia-komendy'); raise SystemExit(1)")
    tresc = (
        "name: w\nsteps:\n"
        f"  {CEL}: {{skill: s_{CEL}}}\n"
        f"  sprawdz: {{system: check, capability: typecheck, needs: [{CEL}], "
        f"on_fail: {{retry: {CEL}, max: 1}}}}\n")
    return swiat(
        tmp_path, [K(CEL)], tresc=tresc,
        project=PROJEKT_Z_CHECKIEM.format(komenda=json.dumps(komenda)))


async def test_raport_porazki_kroku_check_zawiera_ogon_wyjscia_komendy_z_pliku_dowodu(tmp_path):
    s = swiat_z_checkiem(tmp_path)

    await uruchom_swiat(s, AgentNaprawczy())

    tekst = tekst_raportu(s, CEL, 1)
    assert "## Ogon wyjścia komendy\n\nostatnia-linia-komendy\n" in tekst
    assert "- krok: sprawdz" in tekst.splitlines()
    assert "- kod wyjścia: 1" in tekst.splitlines()


async def test_raport_porazki_kroku_bez_dowodu_nie_ma_sekcji_ogona_komendy(tmp_path):
    s = swiat_naprawy(tmp_path, max=3)

    await uruchom_swiat(s, AgentNaprawczy(padaja={NIEUDANY: 1}))

    assert "## Ogon wyjścia komendy" not in tekst_raportu(s, CEL, 1)


def _przygotowany_z_dowodem(tmp_path: Path, tresc: bytes | None):
    katalog = tmp_path / "runy" / "run" / "sprawdz"
    katalog.mkdir(parents=True)
    if tresc is not None:
        (katalog / "dowod-typecheck.json").write_bytes(tresc)
    return SimpleNamespace(worktree=SimpleNamespace(katalog_runu=tmp_path / "runy" / "run"))


@pytest.mark.parametrize("tresc, typ", [
    (None, "FileNotFoundError"),                                    # OSError: brak pliku
    (b"{to nie jest json", "JSONDecodeError"),                      # ValueError: nie-JSON
    (b"\xff\xfe", "UnicodeDecodeError"),                            # ValueError: zle kodowanie
    (b"{}", "KeyError"),                                            # brak stdout_tail
    (b"[]", "TypeError"),                                           # JSON bedacy lista
    (b"[" * 200_000, "RecursionError"),                             # zbyt gleboki JSON
])
def test_nieczytelny_plik_dowodu_daje_linie_ogon_niedostepny_z_nazwa_typu_bledu(tmp_path, tresc, typ):
    from runner.silnik import _ogon_dowodu

    p = _przygotowany_z_dowodem(tmp_path, tresc)

    assert _ogon_dowodu(p, "sprawdz", {"plik": "dowod-typecheck.json"}) == f"ogon niedostępny ({typ})"


def test_czytelny_plik_dowodu_daje_stdout_tail_doslownie(tmp_path):
    from runner.silnik import _ogon_dowodu

    p = _przygotowany_z_dowodem(tmp_path, json.dumps({"stdout_tail": "linia 1\nlinia 2\n"}).encode())

    assert _ogon_dowodu(p, "sprawdz", {"plik": "dowod-typecheck.json"}) == "linia 1\nlinia 2\n"


# --- ogon logu i linie raportu ---------------------------------------------------------------------------------------


async def test_raport_porazki_ma_dokladnie_dwadziescia_ostatnich_linii_logu_kroku(tmp_path):
    s = swiat_naprawy(tmp_path, max=3)
    agent = AgentNaprawczy(padaja={NIEUDANY: 1}, linie_przed={NIEUDANY: 24})   # 25 linii w sumie

    await uruchom_swiat(s, agent)

    tekst = tekst_raportu(s, CEL, 1)
    ogon = tekst.split("## Ogon logu\n\n", 1)[1].splitlines()
    assert ogon == [f"linia-{i:02d}" for i in range(6, 25)] + [f"{NIEUDANY}: wynik wywołania 1: czerwone"]


async def test_raport_porazki_ma_dokladne_linie_kroku_proby_kodu_powodu_i_werdyktu(tmp_path):
    s = swiat_naprawy(tmp_path, max=3)

    await uruchom_swiat(s, AgentNaprawczy(padaja={NIEUDANY: 2}, werdykty={NIEUDANY: "ODRZUCONO"}))

    pierwszy = tekst_raportu(s, CEL, 1).splitlines()
    assert pierwszy[0] == f"# Raport porażki kroku {NIEUDANY}"
    for linia in (
        f"- krok: {NIEUDANY}", "- próba kroku: 1", f"- kod wyjścia: {KOD_PORAZKI}",
        f"- powód: {powod_porazki(1)}", "- werdykt: ODRZUCONO",
    ):
        assert linia in pierwszy
    assert "- próba kroku: 2" in tekst_raportu(s, CEL, 2).splitlines()


async def test_raport_porazki_bez_kodu_wyjscia_mowi_kod_wyjscia_brak(tmp_path):
    s = swiat_naprawy(tmp_path, max=3)

    await uruchom_swiat(s, AgentNaprawczy(padaja={NIEUDANY: 1}, bez_kodu=frozenset({NIEUDANY})))

    linie = tekst_raportu(s, CEL, 1).splitlines()
    assert "- kod wyjścia: brak" in linie
    assert "- werdykt: brak" in linie


async def test_raport_porazki_kroku_ktory_nie_ruszyl_ma_powod_silnika_i_brak_kodu(tmp_path):
    profil = ProfilBackendu(modele={Tier.execution: "m-execution"})     # brak modelu dla judgment
    s = swiat(
        tmp_path,
        [K(CEL), K(NIEUDANY, (CEL,), extra=f", on_fail: {{retry: {CEL}, max: 1}}")],
        opcje_skilli={CEL: {"role": "executor"}, NIEUDANY: {"role": "reviewer"}}, profil=profil)

    zdarzenia = await uruchom_swiat(s, AgentNaprawczy())

    linie = tekst_raportu(s, CEL, 1).splitlines()
    assert "- kod wyjścia: brak" in linie
    assert "- powód: brak modelu dla tieru judgment" in linie
    assert "- werdykt: brak" in linie
    assert zdarzenia[-1]["kind"] == "run_finished"


# --- wznowienie: krok przerwany ma numer proby z checkpointu -----------------------------------------------------------


async def test_krok_przerwany_przy_smierci_runnera_dostaje_porazke_z_numerem_proby_z_checkpointu(tmp_path):
    s = swiat(tmp_path, [K("b", extra=BRAMKA), K("x"), K("y", ("x",))])
    agent = AgentNaprawczy()
    await uruchom_swiat(s, agent)
    cp = wczytaj_checkpoint(s.katalog_runu)
    kroki = dict(cp.kroki)
    kroki["x"] = StanKrokuCheckpointu("w_toku", 3)
    kroki["y"] = StanKrokuCheckpointu("oczekuje", 0)
    zapisz_checkpoint(s.katalog_runu, Checkpoint(
        run_id=cp.run_id, repozytorium=cp.repozytorium, workflow=cp.workflow,
        base_commit=cp.base_commit, worktree=cp.worktree, kroki=kroki, waiting_on=cp.waiting_on))

    zdarzenia = await wznow(s, agent, krok="b")

    (porazka,) = [z for z in zdarzenia if z["kind"] == "step_failed" and z["step_id"] == "x"]
    assert porazka["text"] == "przerwany przy śmierci runnera — nie wznawiamy"
    assert porazka["attempt"] == 3
    assert agent.starty("x") == 1             # tylko pierwszy przebieg, nie wznowienie


# --- ogon bez tekstu, numer proby pominiecia, blad zapisu checkpointu przy bramce ------------------------------------


async def test_zdarzenie_output_bez_tekstu_daje_w_ogonie_raportu_pusta_linie(tmp_path):
    s = swiat_naprawy(tmp_path, max=3)

    await uruchom_swiat(s, AgentNaprawczy(padaja={NIEUDANY: 1}, bez_tekstu=frozenset({NIEUDANY})))

    ogon = tekst_raportu(s, CEL, 1).split("## Ogon logu\n\n", 1)[1].splitlines()
    assert ogon == ["", f"{NIEUDANY}: wynik wywołania 1: czerwone"]


async def test_krok_pominiety_po_porazce_celu_w_drugiej_probie_niesie_numer_nastepnej_proby(tmp_path):
    s = swiat(tmp_path, [
        K(CEL), K("przeglad", (CEL,)),
        K(NIEUDANY, ("przeglad",), extra=f", on_fail: {{retry: {CEL}, max: 1}}"),
    ])
    agent = AgentNaprawczy(padaja={NIEUDANY: 1}, padaja_w={CEL: frozenset({2})})

    zdarzenia = await uruchom_swiat(s, agent)

    pominiete = [z for z in zdarzenia if z["kind"] == "step_skipped"]
    assert [(z["step_id"], z["attempt"]) for z in pominiete] == [("przeglad", 2), (NIEUDANY, 2)]
    assert przebieg(zdarzenia, CEL)[-1] == ("step_failed", 2)
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_blad_zapisu_checkpointu_przy_pierwszej_bramce_niesie_numer_proby_jeden(tmp_path, monkeypatch):
    s = swiat_z_bramka(tmp_path)
    _zepsuj_zapis_checkpointu(monkeypatch, od_zapisu=1)

    zdarzenia = await uruchom_swiat(s, AgentNaprawczy())

    (porazka,) = [z for z in zdarzenia if z["kind"] == "step_failed"]
    assert porazka["step_id"] == "zatwierdz"
    assert porazka["text"].startswith(PREFIKS_BLEDU_ZAPISU)
    assert porazka["attempt"] == 1
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_blad_zapisu_checkpointu_przy_bramce_po_powrocie_petli_niesie_numer_nastepnej_proby(
    tmp_path, monkeypatch,
):
    s = swiat_z_bramka(tmp_path)
    agent = AgentNaprawczy(padaja={NIEUDANY: 1})
    await uruchom_swiat(s, agent)
    _zepsuj_zapis_checkpointu(monkeypatch, od_zapisu=2)   # zapis 1: decyzja, zapis 2: bramka po powrocie

    zdarzenia = await wznow(s, agent)

    (porazka,) = [z for z in zdarzenia if z["kind"] == "step_failed" and z["step_id"] == "zatwierdz"]
    assert porazka["text"].startswith(PREFIKS_BLEDU_ZAPISU)
    assert porazka["attempt"] == 2             # zatwierdz wystartowal juz raz
    assert zdarzenia[-1]["kind"] == "run_finished"


# --- `_zapisz_raport_porazki` wprost: identyfikator celu, pelna tresc ---------------------------------------------------


def _p_raportu(tmp_path: Path):
    return SimpleNamespace(worktree=SimpleNamespace(
        sciezka=tmp_path / "wt", katalog_runu=tmp_path / "run"))


def test_cel_z_niedozwolonym_identyfikatorem_daje_komunikat_i_nie_zapisuje_zadnego_pliku(tmp_path):
    from runner.silnik import _zapisz_raport_porazki

    wynik = _zapisz_raport_porazki(
        _p_raportu(tmp_path), "testuj", 1, "../x", 1,
        WynikKroku(status="nieudany", powod="p", kod_wyjscia=1), "p", [])

    assert wynik == "niedozwolony identyfikator kroku: '../x'"
    assert list(tmp_path.rglob("*")) == []


def test_raport_porazki_ma_dokladna_tresc_z_ogonem_logu(tmp_path):
    from runner.silnik import _zapisz_raport_porazki

    p = _p_raportu(tmp_path)
    wynik = WynikKroku(status="nieudany", powod="pow", kod_wyjscia=3, werdykt="W")

    assert _zapisz_raport_porazki(p, "krok", 1, "cel", 2, wynik, "pow", ["l1", "l2"]) is None

    plik = p.worktree.sciezka / ".cockpit/_run/artifacts/cel/_naprawa/proba_2.md"
    assert plik.read_text(encoding="utf-8") == (
        "# Raport porażki kroku krok\n\n- krok: krok\n- próba kroku: 1\n- kod wyjścia: 3\n"
        "- powód: pow\n- werdykt: W\n\n## Ogon logu\n\nl1\nl2\n")


def test_raport_porazki_kroku_check_ma_dokladna_tresc_z_ogonem_wyjscia_komendy(tmp_path):
    from runner.silnik import _zapisz_raport_porazki

    p = _p_raportu(tmp_path)
    (tmp_path / "run" / "sprawdz").mkdir(parents=True)
    (tmp_path / "run" / "sprawdz" / "dowod-typecheck.json").write_text(
        json.dumps({"stdout_tail": "wyjscie\n"}), encoding="utf-8")
    wynik = WynikKroku(
        status="nieudany", powod="pow", kod_wyjscia=None, dowod={"plik": "dowod-typecheck.json"})

    assert _zapisz_raport_porazki(p, "sprawdz", 1, "cel", 1, wynik, "pow", ["l1"]) is None

    plik = p.worktree.sciezka / ".cockpit/_run/artifacts/cel/_naprawa/proba_1.md"
    assert plik.read_text(encoding="utf-8") == (
        "# Raport porażki kroku sprawdz\n\n- krok: sprawdz\n- próba kroku: 1\n"
        "- kod wyjścia: brak\n- powód: pow\n- werdykt: brak\n\n## Ogon logu\n\nl1\n\n"
        "## Ogon wyjścia komendy\n\nwyjscie\n\n")


def test_raport_porazki_bez_powodu_mowi_powod_brak(tmp_path):
    from runner.silnik import _zapisz_raport_porazki

    p = _p_raportu(tmp_path)
    wynik = WynikKroku(status="nieudany", powod=None, kod_wyjscia=1)

    assert _zapisz_raport_porazki(p, "krok", 1, "cel", 1, wynik, None, []) is None

    plik = p.worktree.sciezka / ".cockpit/_run/artifacts/cel/_naprawa/proba_1.md"
    assert "- powód: brak" in plik.read_text(encoding="utf-8").splitlines()


# --- cel ponowienia pominiety (nie wystartowal), a krok nieudany i tak startuje ---------------------------------------

OPCJE_CEL_BEZ_CAPABILITY = {CEL: {"needs": ["deploy"]}}     # projekt nie ma `deploy`: cel pominiety
RETRY_NA_CEL = f", on_fail: {{retry: {CEL}, max: 2}}"


def _sprawdz_brak_ponowienia_celu_ktory_nie_wystartowal(s, agent, zdarzenia) -> None:
    assert agent.starty(NIEUDANY) == 1
    assert agent.starty(CEL) == 0
    (porazka,) = [z for z in zdarzenia if z["kind"] == "step_failed" and z["step_id"] == NIEUDANY]
    assert porazka["text"] == f"nie można ponowić kroku {NIEUDANY}: cel {CEL} nie wystartował"
    assert porazka["attempt"] == 1
    assert zdarzenia[-1]["kind"] == "run_finished"
    assert list(s.worktree.glob(".cockpit/_run/artifacts/**/_naprawa")) == []


async def test_cel_pominiety_z_braku_capability_nie_jest_ponawiany_a_krok_nieudany_konczy_run_jawnie(tmp_path):
    s = swiat(
        tmp_path, [K(CEL), K(NIEUDANY, (CEL,), extra=RETRY_NA_CEL)],
        opcje_skilli=OPCJE_CEL_BEZ_CAPABILITY)
    agent = AgentNaprawczy(padaja={NIEUDANY: 99})

    zdarzenia = await uruchom_swiat(s, agent)

    assert "brak_capability" in [z for z in zdarzenia if z["kind"] == "step_skipped"][0]["text"]
    _sprawdz_brak_ponowienia_celu_ktory_nie_wystartowal(s, agent, zdarzenia)


async def test_cel_pominiety_nie_jest_ponawiany_takze_po_wznowieniu_runu_z_bramki(tmp_path):
    s = swiat(
        tmp_path,
        [K(CEL), K("zatwierdz", (CEL,), extra=BRAMKA), K(NIEUDANY, ("zatwierdz",), extra=RETRY_NA_CEL)],
        opcje_skilli=OPCJE_CEL_BEZ_CAPABILITY)
    agent = AgentNaprawczy(padaja={NIEUDANY: 99})
    pierwsze = await uruchom_swiat(s, agent)
    assert pierwsze[-1]["kind"] == "step_waiting"

    zdarzenia = await wznow(s, agent)

    _sprawdz_brak_ponowienia_celu_ktory_nie_wystartowal(s, agent, zdarzenia)
    assert agent.starty("zatwierdz") == 1
