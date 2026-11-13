"""HAR-172 (czesc 1): bramka w silniku runu i checkpoint na dysku (TDD).

Krok z `approval: true` zawiesza run BEZ procesu agenta; checkpoint z `waiting_on` lezy na
dysku, zanim silnik ogłosi `step_waiting` (zapis nr 1 w chwili trafienia na bramke, nr 2 po
petli); kroki niezalezne od bramki sie koncza PRZED `step_waiting` (inaczej ich `step_started`
cofnalby w kokpicie `waiting` do `running`), zalezne nie startuja i nie sa pomijane; run
zawieszony nie dostaje `run_finished`; blad zapisu checkpointu to `step_failed` kroku bramki.

Prawdziwy kod: `runner.silnik.wykonaj_run` i `runner.checkpoint` na prawdziwym repo git z
`tests/test_silnik_runu.py` (swiat, K, Szpieg, Atrapa, uruchom). Zaslepiony jest tylko agent
(Szpieg zapisuje wywolania, Atrapa uruchamia prawdziwy proces). Okno po zapisie checkpointu
(kryterium 3) sprawdza prawdziwy proces silnika zabity SIGKILL (`tests/bramka_silnik_proces.py`).
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import signal
import sys
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cockpit.services.errors import BladDziedziny
from procesy import czekaj, czekaj_na_warunek, uruchom_proces, zabij_proces, zakoncz_proces
from runner.checkpoint import (
    BladCheckpointu,
    Checkpoint,
    Oczekiwanie,
    StanKrokuCheckpointu,
    wczytaj_checkpoint,
    zapisz_checkpoint,
)
from runner.krok import WynikKroku
from test_silnik_runu import RUN_ID, Atrapa, K, Szpieg, _git, indeks, jedno, rodzaje, swiat, uruchom

KORZEN = Path(__file__).resolve().parent.parent
POMOCNIK = Path(__file__).resolve().parent / "bramka_silnik_proces.py"
BRAMKA = ", approval: true"
PLIK = "checkpoint.json"
ZMIENNA = "COCKPIT_TEST_PAUSE_AFTER_CHECKPOINT"
LIMIT_PROCESU_S = 60
TEKST_CZEKANIA = "czeka na decyzję człowieka (approval)"


def _checkpoint(**zmiany) -> Checkpoint:
    pola = {
        "run_id": "run-1",
        "repozytorium": "/repo",
        "workflow": "w",
        "base_commit": "a" * 40,
        "worktree": "/runy/run-1/worktree",
        "kroki": {
            "a": StanKrokuCheckpointu("oczekuje", 0),
            "c": StanKrokuCheckpointu("zakonczony", 1),
        },
        "waiting_on": Oczekiwanie("a", "2026-01-01T10:00:00+00:00"),
    }
    pola.update(zmiany)
    return Checkpoint(**pola)


def _surowy_json(katalog: Path) -> dict:
    return json.loads((katalog / PLIK).read_text(encoding="utf-8"))


def _podmien_sleep(monkeypatch, przy_wywolaniu=None) -> list[float]:
    """`asyncio.sleep` zapisuje argument; prawdziwe czekanie jest zastapione oddaniem petli."""
    prawdziwy = asyncio.sleep
    wywolania: list[float] = []

    async def sleep(sekundy, *argumenty, **opcje):
        wywolania.append(sekundy)
        if przy_wywolaniu is not None:
            przy_wywolaniu(sekundy)
        await prawdziwy(0)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    return wywolania


def _zlicz_podmiany_checkpointu(monkeypatch) -> list[Path]:
    prawdziwy = os.replace
    cele: list[Path] = []

    def replace(src, dst, *argumenty, **opcje):
        if Path(dst).name == PLIK:
            cele.append(Path(dst))
        return prawdziwy(src, dst, *argumenty, **opcje)

    monkeypatch.setattr(os, "replace", replace)
    return cele


# =====================================================================================
# 1. Silnik: bramka zawiesza run bez procesu agenta (kryteria 1 i 4)
# =====================================================================================


async def test_run_z_bramka_zatrzymuje_sie_na_niej_i_nie_ma_zadnego_procesu_agenta_tego_kroku(tmp_path):
    s = swiat(tmp_path, [K("a", extra=BRAMKA), K("c")])
    agent = Atrapa(s.znaczniki)
    zadania_przed = asyncio.all_tasks()

    zdarzenia = await uruchom(s, agent)

    jedno(zdarzenia, "step_waiting", "a")
    assert agent.wywolania == ["c"]                         # krok z bramka nie dotarl do agenta
    assert not (s.znaczniki / "start_a").exists()           # ani jeden proces agenta kroku a
    assert not (s.znaczniki / "pid_a").exists()
    assert (s.znaczniki / "pid_c").exists()                 # kontrast: krok niezalezny mial proces
    assert asyncio.all_tasks() - zadania_przed == set()     # po zawieszeniu nie zostaje zadanie kroku


async def test_krok_na_bramce_ma_tylko_step_waiting_bez_startu_porazki_i_pominiecia(tmp_path):
    s = swiat(tmp_path, [K("a", extra=BRAMKA)])

    zdarzenia = await uruchom(s, Szpieg())

    assert rodzaje(zdarzenia) == ["step_waiting"]


async def test_step_waiting_niesie_run_krok_agenta_i_informacje_na_co_czeka(tmp_path):
    s = swiat(tmp_path, [K("a", extra=BRAMKA)])

    zdarzenia = await uruchom(s, Szpieg())

    assert jedno(zdarzenia, "step_waiting", "a") == {
        "kind": "step_waiting", "run_id": RUN_ID, "step_id": "a", "agent_id": "a",
        "text": TEKST_CZEKANIA, "attempt": 1,
    }


async def test_run_zawieszony_na_bramce_nie_dostaje_run_finished(tmp_path):
    s = swiat(tmp_path, [K("a", extra=BRAMKA), K("c")])

    zdarzenia = await uruchom(s, Szpieg())

    assert "run_finished" not in rodzaje(zdarzenia)


async def test_krok_zalezny_od_bramki_nie_startuje_i_nie_jest_pominiety(tmp_path):
    s = swiat(tmp_path, [K("a", extra=BRAMKA), K("b", ("a",)), K("c")])
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent)

    assert [z for z in zdarzenia if z["step_id"] == "b"] == []     # ani start, ani pominiecie
    assert "b" not in agent.kroki


async def test_krok_zalezny_od_bramki_posrednio_tez_nie_startuje(tmp_path):
    s = swiat(tmp_path, [K("a", extra=BRAMKA), K("b", ("a",)), K("d", ("b",))])
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent)

    assert [z for z in zdarzenia if z["step_id"] in ("b", "d")] == []
    assert agent.wywolania == []


async def test_kroki_niezalezne_od_bramki_koncza_sie_mimo_zawieszenia_takze_lancuch_po_niej(tmp_path):
    s = swiat(tmp_path, [K("a", extra=BRAMKA), K("c"), K("d", ("c",))])
    agent = Szpieg(opoznienia={"c": 0.3})

    zdarzenia = await uruchom(s, agent)

    jedno(zdarzenia, "step_finished", "c")
    jedno(zdarzenia, "step_finished", "d")
    assert agent.kroki == ["c", "d"]


# Silnik ogłasza bramkę dopiero po krokach niezależnych: `step_started` kroku niezależnego
# zmienia w kokpicie run `waiting` z powrotem na `running` (runs.nastepny_status), a wtedy
# decyzja dostałaby 409. `step_waiting` musi więc być ostatnim zdarzeniem runu.


async def test_step_waiting_jest_po_zakonczeniu_kroku_niezaleznego_od_bramki(tmp_path):
    s = swiat(tmp_path, [K("a", extra=BRAMKA), K("c")])

    zdarzenia = await uruchom(s, Szpieg(opoznienia={"c": 0.2}))

    assert indeks(zdarzenia, "step_started", "c") < indeks(zdarzenia, "step_finished", "c")
    assert indeks(zdarzenia, "step_finished", "c") < indeks(zdarzenia, "step_waiting", "a")
    assert zdarzenia[-1]["kind"] == "step_waiting"


async def test_step_waiting_jest_po_porazce_kroku_niezaleznego_od_bramki(tmp_path):
    s = swiat(tmp_path, [K("a", extra=BRAMKA), K("c")])
    agent = Szpieg(wyniki={"c": WynikKroku("nieudany", "kod wyjścia 1", 1)})

    zdarzenia = await uruchom(s, agent)

    assert indeks(zdarzenia, "step_failed", "c") < indeks(zdarzenia, "step_waiting", "a")
    assert zdarzenia[-1]["kind"] == "step_waiting"


async def test_step_waiting_jest_po_calym_lancuchu_krokow_niezaleznych_od_bramki(tmp_path):
    s = swiat(tmp_path, [K("a", extra=BRAMKA), K("c"), K("d", ("c",))])

    zdarzenia = await uruchom(s, Szpieg(opoznienie=0.1))

    assert indeks(zdarzenia, "step_finished", "d") < indeks(zdarzenia, "step_waiting", "a")
    assert zdarzenia[-1]["kind"] == "step_waiting"
    assert rodzaje(zdarzenia).count("step_waiting") == 1


async def test_bramka_nie_powtarza_sie_w_kolejnych_przebiegach_petli(tmp_path):
    s = swiat(tmp_path, [K("a", extra=BRAMKA), K("c"), K("d", ("c",)), K("e", ("d",))])
    agent = Szpieg(opoznienie=0.1)

    zdarzenia = await uruchom(s, agent)

    assert rodzaje(zdarzenia).count("step_waiting") == 1
    assert agent.kroki == ["c", "d", "e"]


async def test_dwie_niezalezne_bramki_daja_dokladnie_jedno_oczekiwanie(tmp_path):
    s = swiat(tmp_path, [K("a", extra=BRAMKA), K("b", extra=BRAMKA)])
    agent = Szpieg()

    zdarzenia = await uruchom(s, agent)

    (czekanie,) = [z for z in zdarzenia if z["kind"] == "step_waiting"]
    assert rodzaje(zdarzenia) == ["step_waiting"]           # druga bramka bez zadnego zdarzenia
    assert agent.wywolania == []
    cp = wczytaj_checkpoint(s.katalog_runu)
    assert cp.waiting_on.step_id == czekanie["step_id"]     # checkpoint wskazuje te sama bramke
    drugi = ({"a", "b"} - {czekanie["step_id"]}).pop()
    assert cp.kroki[drugi] == StanKrokuCheckpointu("oczekuje", 0)


async def test_run_bez_bramki_nadal_konczy_sie_run_finished(tmp_path):
    s = swiat(tmp_path, [K("a"), K("b", ("a",))])

    zdarzenia = await uruchom(s, Szpieg())

    assert zdarzenia[-1]["kind"] == "run_finished"
    assert "step_waiting" not in rodzaje(zdarzenia)


# =====================================================================================
# 2. Silnik: checkpoint na dysku zanim zostanie wyslane step_waiting (kryterium 2)
# =====================================================================================


async def test_checkpoint_z_waiting_on_istnieje_na_dysku_zanim_zostanie_wyslane_step_waiting(tmp_path):
    s = swiat(tmp_path, [K("a", extra=BRAMKA), K("b", ("a",)), K("c")])
    odczyty: list[dict] = []

    async def emit(zdarzenie: dict) -> None:
        if zdarzenie["kind"] == "step_waiting":
            # brak pliku w tej chwili = FileNotFoundError i czerwony test
            odczyty.append(_surowy_json(s.katalog_runu))

    await uruchom(s, Szpieg(), emit=emit)

    (cp,) = odczyty
    assert cp["run_id"] == RUN_ID
    assert cp["waiting_on"]["step_id"] == "a"
    assert set(cp["kroki"]) == {"a", "b", "c"}
    assert cp["kroki"]["a"] == {"stan": "oczekuje", "proby": 0}   # bramka nie dostala startu
    assert cp["kroki"]["b"] == {"stan": "oczekuje", "proby": 0}   # zalezny czeka na bramke


async def test_znacznik_czasu_oczekiwania_to_iso_8601_w_utc_z_chwili_zawieszenia(tmp_path):
    s = swiat(tmp_path, [K("a", extra=BRAMKA)])

    await uruchom(s, Szpieg())

    od = datetime.fromisoformat(wczytaj_checkpoint(s.katalog_runu).waiting_on.od)
    assert od.utcoffset() == timedelta(0)
    assert abs((datetime.now(timezone.utc) - od).total_seconds()) < 120


async def test_checkpoint_opisuje_run_repozytorium_workflow_commit_i_worktree(tmp_path):
    s = swiat(tmp_path, [K("a", extra=BRAMKA)])

    await uruchom(s, Szpieg())

    cp = wczytaj_checkpoint(s.katalog_runu)
    assert cp.run_id == RUN_ID
    assert Path(cp.repozytorium).resolve() == s.repo.resolve()
    assert cp.workflow == "w"
    assert cp.base_commit == _git(s.repo, "rev-parse", "HEAD").strip()
    assert Path(cp.worktree).resolve() == s.worktree.resolve()


class SzpiegCzytajacyCheckpoint(Szpieg):
    """Agent-szpieg: w chwili, gdy silnik uruchamia jego krok, czyta checkpoint z dysku."""

    def __init__(self, **opcje):
        super().__init__(**opcje)
        self.checkpointy: dict[str, dict | None] = {}

    async def __call__(self, spec, polecenie, katalog, emit, *, katalog_runu=None, prompt=None):
        plik = katalog_runu / PLIK
        self.checkpointy[spec.step_id] = (
            json.loads(plik.read_text(encoding="utf-8")) if plik.is_file() else None)
        return await super().__call__(
            spec, polecenie, katalog, emit, katalog_runu=katalog_runu, prompt=prompt)


async def test_checkpoint_z_waiting_on_lezy_na_dysku_zanim_krok_niezalezny_od_bramki_sie_skonczy(tmp_path):
    s = swiat(tmp_path, [K("a", extra=BRAMKA), K("c"), K("d", ("c",))])
    agent = SzpiegCzytajacyCheckpoint(opoznienie=0.1)

    await uruchom(s, agent)

    # to zapis nr 1: silnik trafil na bramke, a krok `c` dopiero rusza (nie ma jeszcze zapisu nr 2)
    widziany_przez_c = agent.checkpointy["c"]
    assert widziany_przez_c is not None, "przy starcie kroku niezaleznego nie bylo checkpointu"
    assert widziany_przez_c["run_id"] == RUN_ID
    assert widziany_przez_c["workflow"] == "w"             # nazwa workflow już w zapisie nr 1
    assert widziany_przez_c["waiting_on"]["step_id"] == "a"
    assert widziany_przez_c["kroki"]["a"] == {"stan": "oczekuje", "proby": 0}
    assert widziany_przez_c["kroki"]["c"]["stan"] != "zakonczony"
    # krok `d` startuje po zakonczeniu `c`, ale nadal przed zapisem nr 2 (po petli)
    widziany_przez_d = agent.checkpointy["d"]
    assert widziany_przez_d is not None
    assert widziany_przez_d["waiting_on"] == widziany_przez_c["waiting_on"]
    assert widziany_przez_d["kroki"]["d"]["stan"] != "zakonczony"


async def test_po_zakonczeniu_petli_checkpoint_ma_aktualne_stany_krokow_i_to_samo_waiting_on(tmp_path):
    s = swiat(tmp_path, [K("a", extra=BRAMKA), K("b", ("a",)), K("c")])
    agent = SzpiegCzytajacyCheckpoint(opoznienia={"c": 0.2})

    await uruchom(s, agent)

    koncowy = wczytaj_checkpoint(s.katalog_runu)
    assert koncowy.kroki == {
        "a": StanKrokuCheckpointu("oczekuje", 0),
        "b": StanKrokuCheckpointu("oczekuje", 0),
        "c": StanKrokuCheckpointu("zakonczony", 1),         # niezalezny krok skonczony, 1 start
    }
    assert koncowy.waiting_on is not None
    assert koncowy.waiting_on.step_id == "a"
    pierwszy = agent.checkpointy["c"]                        # zapis nr 1, sprzed pracy kroku `c`
    assert pierwszy is not None
    assert koncowy.waiting_on.od == pierwszy["waiting_on"]["od"]     # to samo oczekiwanie
    assert pierwszy["kroki"]["c"]["stan"] != "zakonczony"
    assert pierwszy["kroki"] != _surowy_json(s.katalog_runu)["kroki"]   # zapis nr 2 jest nowszy


async def test_nieudany_krok_niezalezny_trafia_do_checkpointu_jako_nieudany_z_jedna_proba(tmp_path):
    s = swiat(tmp_path, [K("a", extra=BRAMKA), K("c")])
    agent = Szpieg(wyniki={"c": WynikKroku("nieudany", "kod wyjścia 1", 1)})

    await uruchom(s, agent)

    assert wczytaj_checkpoint(s.katalog_runu).kroki["c"] == StanKrokuCheckpointu("nieudany", 1)


async def test_checkpoint_zapisany_dokladnie_dwa_razy_przy_bramce_na_poczatku_i_po_petli(tmp_path, monkeypatch):
    s = swiat(tmp_path, [K("a", extra=BRAMKA), K("c"), K("d", ("c",)), K("e", ("d",))])
    cele = _zlicz_podmiany_checkpointu(monkeypatch)

    await uruchom(s, Szpieg(opoznienie=0.05))

    assert len(cele) == 2                      # przed zawieszeniem i po zakonczeniu petli


# =====================================================================================
# 3. Silnik: pauza testowa miedzy zapisem checkpointu a step_waiting (kryterium 3, czesc 1)
# =====================================================================================


async def test_zmienna_pauzy_wstrzymuje_silnik_po_zapisie_checkpointu_i_przed_step_waiting(tmp_path, monkeypatch):
    monkeypatch.setenv(ZMIENNA, "7")
    s = swiat(tmp_path, [K("a", extra=BRAMKA)])
    zapisane: list[dict] = []
    w_oknie: list[tuple[bool, list[str]]] = []

    def przy_pauzie(sekundy: float) -> None:
        if sekundy == 7:
            plik = s.katalog_runu / PLIK
            w_oknie.append((plik.is_file() and _surowy_json(s.katalog_runu)["waiting_on"] is not None,
                            rodzaje(zapisane)))

    pauzy = _podmien_sleep(monkeypatch, przy_pauzie)

    async def emit(zdarzenie: dict) -> None:
        zapisane.append(zdarzenie)

    await uruchom(s, Szpieg(), emit=emit)

    assert pauzy == [7]
    assert w_oknie == [(True, [])]            # checkpoint kompletny, step_waiting jeszcze nie wyslane
    assert rodzaje(zapisane) == ["step_waiting"]


async def test_pauza_przyjmuje_ulamek_sekundy(tmp_path, monkeypatch):
    monkeypatch.setenv(ZMIENNA, "0.25")
    s = swiat(tmp_path, [K("a", extra=BRAMKA)])
    pauzy = _podmien_sleep(monkeypatch)

    await uruchom(s, Szpieg())

    assert pauzy == [0.25]


@pytest.mark.parametrize("wartosc", ["0", "-3", "abc", ""])
async def test_zmienna_pauzy_zerowa_ujemna_albo_nieliczbowa_nie_wstrzymuje_silnika(
    tmp_path, monkeypatch, wartosc,
):
    monkeypatch.setenv(ZMIENNA, wartosc)
    s = swiat(tmp_path, [K("a", extra=BRAMKA)])
    pauzy = _podmien_sleep(monkeypatch)

    zdarzenia = await uruchom(s, Szpieg())

    assert pauzy == []
    jedno(zdarzenia, "step_waiting", "a")            # bramka dziala mimo zlej wartosci


async def test_nieliczbowa_zmienna_pauzy_zostawia_komunikat_na_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(ZMIENNA, "abc")
    s = swiat(tmp_path, [K("a", extra=BRAMKA)])
    capsys.readouterr()

    await uruchom(s, Szpieg())

    assert f"{ZMIENNA}='abc' nie jest liczba, pauza pominieta" in capsys.readouterr().err


async def test_brak_zmiennej_pauzy_nie_wstrzymuje_silnika(tmp_path, monkeypatch):
    monkeypatch.delenv(ZMIENNA, raising=False)
    s = swiat(tmp_path, [K("a", extra=BRAMKA)])
    pauzy = _podmien_sleep(monkeypatch)

    zdarzenia = await uruchom(s, Szpieg())

    assert pauzy == []
    jedno(zdarzenia, "step_waiting", "a")


async def test_pauza_dotyczy_tylko_bramki_run_bez_bramki_nie_czeka(tmp_path, monkeypatch):
    monkeypatch.setenv(ZMIENNA, "7")
    s = swiat(tmp_path, [K("a"), K("b", ("a",))])
    pauzy = _podmien_sleep(monkeypatch)

    zdarzenia = await uruchom(s, Szpieg())

    assert pauzy == []
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_pauza_jest_jedna_przy_jednej_bramce_mimo_wielu_przebiegow_petli(tmp_path, monkeypatch):
    monkeypatch.setenv(ZMIENNA, "7")
    s = swiat(tmp_path, [K("a", extra=BRAMKA), K("c"), K("d", ("c",))])
    pauzy = _podmien_sleep(monkeypatch)

    await uruchom(s, Szpieg())

    assert pauzy == [7]


async def test_pauza_jest_po_krokach_niezaleznych_a_przed_step_waiting_z_zapisanym_koncowym_checkpointem(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv(ZMIENNA, "7")
    s = swiat(tmp_path, [K("a", extra=BRAMKA), K("c")])
    zapisane: list[dict] = []
    w_oknie: list[tuple[list[str], dict]] = []

    def przy_pauzie(sekundy: float) -> None:
        if sekundy == 7:
            w_oknie.append((rodzaje(zapisane), _surowy_json(s.katalog_runu)))

    pauzy = _podmien_sleep(monkeypatch, przy_pauzie)

    async def emit(zdarzenie: dict) -> None:
        zapisane.append(zdarzenie)

    await uruchom(s, Szpieg(), emit=emit)

    assert pauzy == [7]
    ((rodzaje_w_oknie, cp_w_oknie),) = w_oknie
    assert rodzaje_w_oknie == ["step_started", "step_finished"]      # krok `c` skonczony, bramka jeszcze nie
    assert cp_w_oknie["kroki"]["c"] == {"stan": "zakonczony", "proby": 1}   # zapis nr 2 przed pauza
    assert cp_w_oknie["waiting_on"]["step_id"] == "a"
    assert rodzaje(zapisane)[-1] == "step_waiting"


def test_zmienna_pauzy_i_plik_checkpointu_maja_nazwy_z_kontraktu():
    from runner import checkpoint

    assert checkpoint.ZMIENNA_PAUZY == ZMIENNA
    assert checkpoint.PLIK_CHECKPOINTU == PLIK


# =====================================================================================
# 3b. Silnik: blad zapisu checkpointu nie wywraca silnika (sekcja G kontraktu)
# =====================================================================================


PREFIKS_BLEDU_ZAPISU = "zapis checkpointu nieudany:"


def _zepsuj_zapis_checkpointu(monkeypatch, od_zapisu: int) -> list[Path]:
    """`os.replace` na checkpoint.json rzuca `OSError` od zapisu o numerze `od_zapisu` (1 = pierwszy)."""
    prawdziwy = os.replace
    proby: list[Path] = []

    def replace(src, dst, *argumenty, **opcje):
        if Path(dst).name == PLIK:
            proby.append(Path(dst))
            if len(proby) >= od_zapisu:
                raise OSError("dysk pelny")
        return prawdziwy(src, dst, *argumenty, **opcje)

    monkeypatch.setattr(os, "replace", replace)
    return proby


async def test_blad_pierwszego_zapisu_checkpointu_to_porazka_kroku_bramki_a_nie_wyjatek_silnika(
    tmp_path, monkeypatch,
):
    s = swiat(tmp_path, [K("a", extra=BRAMKA), K("c")])
    proby = _zepsuj_zapis_checkpointu(monkeypatch, od_zapisu=1)

    zdarzenia = await uruchom(s, Szpieg())          # wyjatek z silnika oblalby test tutaj

    assert len(proby) == 1                          # po bledzie nr 1 silnik nie ponawia zapisu
    porazka = jedno(zdarzenia, "step_failed", "a")
    assert porazka["run_id"] == RUN_ID              # zdarzenie przypisane do runu i do agenta kroku
    assert porazka["agent_id"] == "a"
    tekst = porazka["text"]
    assert tekst.startswith(PREFIKS_BLEDU_ZAPISU)
    assert "dysk pelny" in tekst                    # powod z dysku nie ginie
    assert "step_waiting" not in rodzaje(zdarzenia)
    assert zdarzenia[-1]["kind"] == "run_finished"
    assert rodzaje(zdarzenia).count("run_finished") == 1


async def test_blad_pierwszego_zapisu_checkpointu_nie_uruchamia_krokow_zaleznych_ani_pozniejszych(
    tmp_path, monkeypatch,
):
    s = swiat(tmp_path, [K("a", extra=BRAMKA), K("b", ("a",)), K("c"), K("d", ("c",))])
    _zepsuj_zapis_checkpointu(monkeypatch, od_zapisu=1)
    agent = Szpieg(opoznienia={"c": 0.2})

    zdarzenia = await uruchom(s, agent)

    assert "b" not in agent.kroki                   # zalezny od bramki
    assert "d" not in agent.kroki                   # nastepny w lancuchu po `c`: petla sie konczy
    assert [z for z in zdarzenia if z["step_id"] in ("b", "d")] == []
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_blad_drugiego_zapisu_checkpointu_to_porazka_kroku_bramki_po_krokach_niezaleznych(
    tmp_path, monkeypatch,
):
    s = swiat(tmp_path, [K("a", extra=BRAMKA), K("c"), K("d", ("c",))])
    proby = _zepsuj_zapis_checkpointu(monkeypatch, od_zapisu=2)

    zdarzenia = await uruchom(s, Szpieg())

    assert len(proby) == 2                          # zapis nr 1 przeszedl, nr 2 padl
    jedno(zdarzenia, "step_finished", "c")
    jedno(zdarzenia, "step_finished", "d")          # bledu nr 2 nie ma przed koncem krokow niezaleznych
    nieudany = jedno(zdarzenia, "step_failed", "a")
    assert nieudany["text"].startswith(PREFIKS_BLEDU_ZAPISU)
    assert nieudany["run_id"] == RUN_ID
    assert nieudany["agent_id"] == "a"
    assert indeks(zdarzenia, "step_finished", "d") < zdarzenia.index(nieudany)
    assert "step_waiting" not in rodzaje(zdarzenia)
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_blad_drugiego_zapisu_checkpointu_nie_uruchamia_pauzy_bo_bramka_nie_zostala_zapisana(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv(ZMIENNA, "7")
    s = swiat(tmp_path, [K("a", extra=BRAMKA)])
    _zepsuj_zapis_checkpointu(monkeypatch, od_zapisu=2)
    pauzy = _podmien_sleep(monkeypatch)

    zdarzenia = await uruchom(s, Szpieg())

    assert pauzy == []
    assert "step_waiting" not in rodzaje(zdarzenia)
    jedno(zdarzenia, "step_failed", "a")


@pytest.mark.skipif(os.geteuid() == 0, reason="root pisze do katalogu bez praw zapisu")
async def test_katalog_runu_odebrany_do_zapisu_po_krokach_niezaleznych_to_porazka_kroku_bramki(tmp_path):
    s = swiat(tmp_path, [K("a", extra=BRAMKA), K("c")])
    odebrane: list[dict] = []

    async def emit(zdarzenie: dict) -> None:
        odebrane.append(zdarzenie)
        if zdarzenie["kind"] == "step_finished":
            s.katalog_runu.chmod(0o500)             # zapis nr 1 byl wczesniej, nr 2 trafi na odczyt

    try:
        await uruchom(s, Szpieg(), emit=emit)
    finally:
        s.katalog_runu.chmod(0o700)

    assert jedno(odebrane, "step_failed", "a")["text"].startswith(PREFIKS_BLEDU_ZAPISU)
    assert "step_waiting" not in rodzaje(odebrane)
    assert odebrane[-1]["kind"] == "run_finished"


# =====================================================================================
# 4. Prawdziwy proces silnika zabity SIGKILL w oknie pauzy (kryterium 3, czesc 2)
# =====================================================================================


def _rodzaje_z_pliku(plik: Path) -> list[str]:
    if not plik.exists():
        return []
    return [json.loads(linia)["kind"] for linia in plik.read_text(encoding="utf-8").splitlines()]


async def _silnik_w_osobnym_procesie(s, tmp_path, *, pauza: str, spanie_agenta: float = 0.0):
    """Startuje prawdziwy proces silnika; zwraca (proces, plik zdarzen, plik agenta, plik bledow)."""
    plik_zdarzen = tmp_path / "zdarzenia.jsonl"
    plik_agenta = tmp_path / "agent.log"
    plik_bledow = tmp_path / "stderr.txt"
    opis = {
        "polecenie": s.polecenie, "dom": str(s.dom), "root": str(s.root),
        "zakres": [str(tmp_path)], "zdarzenia": str(plik_zdarzen), "agent": str(plik_agenta),
        "spanie": spanie_agenta,
    }
    srodowisko = {**os.environ, "PYTHONPATH": str(KORZEN / "src"), ZMIENNA: pauza}
    with plik_bledow.open("wb") as bledy:
        proc = await uruchom_proces(
            sys.executable, str(POMOCNIK), json.dumps(opis),
            cwd=KORZEN, env=srodowisko, stdout=asyncio.subprocess.DEVNULL, stderr=bledy)
    return proc, plik_zdarzen, plik_agenta, plik_bledow


async def test_zabicie_silnika_w_oknie_po_checkpoincie_zostawia_kompletny_checkpoint_z_waiting_on(tmp_path):
    s = swiat(tmp_path, [K("a", extra=BRAMKA), K("c")])
    proc, plik_zdarzen, plik_agenta, plik_bledow = await _silnik_w_osobnym_procesie(
        s, tmp_path, pauza="5")
    plik_cp = s.katalog_runu / PLIK
    try:
        # okno pauzy otwiera sie po ostatnim zdarzeniu kroku niezaleznego (`step_finished c`)
        await czekaj_na_warunek(
            lambda: "step_finished" in _rodzaje_z_pliku(plik_zdarzen) or proc.returncode is not None,
            LIMIT_PROCESU_S, "koniec kroku niezaleznego albo koniec procesu")
        # srodek okna pauzy (5 s): proces zyje, wiec nic go nie zakonczylo przed zabiciem
        await asyncio.sleep(1.0)
        komunikat = plik_bledow.read_text(encoding="utf-8", errors="replace")
        assert proc.returncode is None, f"silnik nie czeka w oknie pauzy: {komunikat}"
        zabij_proces(proc.pid, signal.SIGKILL)
        await czekaj(proc.wait(), 10, "koniec zabitego silnika")
    finally:
        await zakoncz_proces(proc, nazwa="silnik z bramka")

    assert proc.returncode == -signal.SIGKILL
    tekst = plik_cp.read_text(encoding="utf-8")
    surowy = json.loads(tekst)                                   # kompletny JSON, nie ucięty
    assert surowy["waiting_on"]["step_id"] == "a"
    # zapis nr 2 (po petli) lezy na dysku w oknie: krok niezalezny jest w nim zakonczony
    assert surowy["kroki"]["c"] == {"stan": "zakonczony", "proby": 1}
    assert wczytaj_checkpoint(s.katalog_runu).waiting_on.step_id == "a"
    assert [p.name for p in s.katalog_runu.iterdir() if "checkpoint" in p.name] == [PLIK]
    # zabity przed ogloszeniem czekania: jedyne zdarzenia to start i koniec kroku niezaleznego
    assert _rodzaje_z_pliku(plik_zdarzen) == ["step_started", "step_finished"]
    assert plik_agenta.read_text().split() == ["c"]             # agent bramki nigdy nie ruszyl


async def test_zabicie_silnika_w_trakcie_kroku_niezaleznego_zostawia_checkpoint_z_waiting_on(tmp_path):
    s = swiat(tmp_path, [K("a", extra=BRAMKA), K("c")])
    # pauza dluga, ale nieosiagalna: krok `c` trwa 60 s, wiec petla nie dochodzi do zapisu nr 2
    proc, plik_zdarzen, plik_agenta, plik_bledow = await _silnik_w_osobnym_procesie(
        s, tmp_path, pauza="5", spanie_agenta=60.0)
    try:
        await czekaj_na_warunek(
            lambda: (plik_agenta.exists() and plik_agenta.read_text().split() == ["c"])
            or proc.returncode is not None,
            LIMIT_PROCESU_S, "start kroku niezaleznego albo koniec procesu")
        komunikat = plik_bledow.read_text(encoding="utf-8", errors="replace")
        assert proc.returncode is None, f"silnik skonczyl przed zabiciem: {komunikat}"
        zabij_proces(proc.pid, signal.SIGKILL)
        await czekaj(proc.wait(), 10, "koniec zabitego silnika")
    finally:
        await zakoncz_proces(proc, nazwa="silnik z bramka i dlugim krokiem")

    assert proc.returncode == -signal.SIGKILL
    # zapis nr 1 (w chwili trafienia na bramke) przezyl smierc procesu w trakcie kroku `c`
    surowy = json.loads((s.katalog_runu / PLIK).read_text(encoding="utf-8"))
    assert surowy["waiting_on"]["step_id"] == "a"
    assert surowy["kroki"]["a"] == {"stan": "oczekuje", "proby": 0}
    assert _rodzaje_z_pliku(plik_zdarzen) == ["step_started"]      # ani step_waiting, ani koniec `c`


# =====================================================================================
# 5. Moduł checkpointu: zapis atomowy i odczyt
# =====================================================================================


def test_blad_checkpointu_jest_bledem_dziedziny():
    assert issubclass(BladCheckpointu, BladDziedziny)


@pytest.mark.parametrize("cp", [
    _checkpoint(),
    _checkpoint(waiting_on=None),
], ids=["z_oczekiwaniem", "bez_oczekiwania"])
def test_zapisany_checkpoint_wraca_z_dysku_taki_sam(tmp_path, cp):
    zapisz_checkpoint(tmp_path, cp)

    assert wczytaj_checkpoint(tmp_path) == cp


def test_zapis_checkpointu_zwraca_sciezke_pliku_checkpointu(tmp_path):
    assert zapisz_checkpoint(tmp_path, _checkpoint()) == tmp_path / PLIK


def test_checkpoint_na_dysku_ma_dokladnie_klucze_z_kontraktu(tmp_path):
    zapisz_checkpoint(tmp_path, _checkpoint())

    assert json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8")) == {
        "run_id": "run-1", "repozytorium": "/repo", "workflow": "w", "base_commit": "a" * 40,
        "worktree": "/runy/run-1/worktree",
        "kroki": {"a": {"stan": "oczekuje", "proby": 0}, "c": {"stan": "zakonczony", "proby": 1}},
        "waiting_on": {"step_id": "a", "od": "2026-01-01T10:00:00+00:00"},
    }


def test_checkpoint_bez_oczekiwania_ma_na_dysku_waiting_on_null(tmp_path):
    zapisz_checkpoint(tmp_path, _checkpoint(waiting_on=None))

    surowy = _surowy_json(tmp_path)
    assert "waiting_on" in surowy and surowy["waiting_on"] is None


def test_zapis_checkpointu_nie_zostawia_plikow_tymczasowych(tmp_path):
    zapisz_checkpoint(tmp_path, _checkpoint())
    zapisz_checkpoint(tmp_path, _checkpoint(waiting_on=None))       # drugi zapis nadpisuje

    assert [p.name for p in tmp_path.iterdir()] == [PLIK]


def test_zapis_checkpointu_nadpisuje_poprzedni_stan(tmp_path):
    zapisz_checkpoint(tmp_path, _checkpoint(workflow="stary"))
    zapisz_checkpoint(tmp_path, _checkpoint(workflow="nowy"))

    assert wczytaj_checkpoint(tmp_path).workflow == "nowy"


def test_plik_tymczasowy_checkpointu_lezy_w_tym_samym_katalogu_co_docelowy(tmp_path, monkeypatch):
    prawdziwy = os.replace
    podmiany: list[tuple[Path, Path]] = []

    def replace(src, dst, *argumenty, **opcje):
        podmiany.append((Path(src), Path(dst)))
        return prawdziwy(src, dst, *argumenty, **opcje)

    monkeypatch.setattr(os, "replace", replace)

    zapisz_checkpoint(tmp_path, _checkpoint())

    ((zrodlo, cel),) = podmiany
    assert cel == tmp_path / PLIK
    assert zrodlo != cel                          # zapis szedl przez plik tymczasowy
    assert zrodlo.parent == tmp_path              # inny katalog = rename miedzy systemami plikow
    # nazwa pliku tymczasowego ma stały prefiks i sufiks (łatwo go rozpoznać po awarii)
    assert zrodlo.name.startswith(".tmp-")
    assert zrodlo.name.endswith(".json")


def test_nieudane_utworzenie_pliku_tymczasowego_nie_probuje_niczego_usuwac(tmp_path, monkeypatch):
    usuniete: list[object] = []

    def unlink(sciezka, *argumenty, **opcje):
        usuniete.append(sciezka)

    monkeypatch.setattr(os, "unlink", unlink)

    with pytest.raises(BladCheckpointu, match="zapis checkpointu"):
        zapisz_checkpoint(tmp_path / "nie_ma_takiego_katalogu", _checkpoint())

    assert usuniete == []                         # nie powstał żaden plik tymczasowy do sprzątania


def test_checkpoint_na_dysku_trzyma_znaki_spoza_ascii_doslownie_w_utf8(tmp_path):
    zapisz_checkpoint(tmp_path, _checkpoint(workflow="zażółć"))

    surowe = (tmp_path / PLIK).read_bytes()
    assert "zażółć".encode("utf-8") in surowe     # nie sekwencja ż
    assert b"\\u" not in surowe


POLECENIE_KODOWANIA = """
import locale, sys
from pathlib import Path
from runner.checkpoint import Checkpoint, Oczekiwanie, StanKrokuCheckpointu
from runner.checkpoint import wczytaj_checkpoint, zapisz_checkpoint

assert locale.getencoding().lower().replace("-", "") not in ("utf8", "cp65001"), locale.getencoding()
wejscie, wyjscie = Path(sys.argv[1]), Path(sys.argv[2])
wczytany = wczytaj_checkpoint(wejscie)
assert wczytany.workflow == "za\\u017c\\u00f3\\u0142\\u0107", wczytany.workflow
zapisz_checkpoint(wyjscie, wczytany)
"""


async def test_checkpoint_ze_znakami_spoza_ascii_dziala_gdy_domyslne_kodowanie_systemu_to_nie_utf8(tmp_path):
    wejscie, wyjscie = tmp_path / "wejscie", tmp_path / "wyjscie"
    wejscie.mkdir()
    wyjscie.mkdir()
    zapisz_checkpoint(wejscie, _checkpoint(workflow="zażółć"))
    srodowisko = {
        **os.environ, "PYTHONPATH": str(KORZEN / "src"),
        "LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0",
    }
    srodowisko.pop("PYTHONIOENCODING", None)
    proc = await uruchom_proces(
        sys.executable, "-c", POLECENIE_KODOWANIA, str(wejscie), str(wyjscie),
        cwd=KORZEN, env=srodowisko, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        _, bledy = await czekaj(proc.communicate(), LIMIT_PROCESU_S, "proces czytajacy i piszacy checkpoint")
    finally:
        await zakoncz_proces(proc, nazwa="proces kodowania checkpointu")

    assert proc.returncode == 0, bledy.decode("utf-8", errors="replace")
    assert "zażółć".encode("utf-8") in (wyjscie / PLIK).read_bytes()


def test_checkpoint_jest_zsynchronizowany_na_dysk_przed_podmiana_pliku(tmp_path, monkeypatch):
    kolejnosc: list[str] = []
    fsync, replace = os.fsync, os.replace

    def podmieniony_fsync(fd):
        kolejnosc.append("fsync")
        return fsync(fd)

    def podmieniony_replace(src, dst, *argumenty, **opcje):
        kolejnosc.append("replace")
        return replace(src, dst, *argumenty, **opcje)

    monkeypatch.setattr(os, "fsync", podmieniony_fsync)
    monkeypatch.setattr(os, "replace", podmieniony_replace)

    zapisz_checkpoint(tmp_path, _checkpoint())

    assert "fsync" in kolejnosc and "replace" in kolejnosc
    assert kolejnosc.index("fsync") < kolejnosc.index("replace")


def test_nieudana_podmiana_nie_psuje_poprzedniego_checkpointu_i_jest_bledem_checkpointu(tmp_path, monkeypatch):
    stary = _checkpoint(workflow="stary")
    zapisz_checkpoint(tmp_path, stary)

    def replace(src, dst, *argumenty, **opcje):
        raise OSError("dysk pelny")

    monkeypatch.setattr(os, "replace", replace)

    with pytest.raises(BladCheckpointu):
        zapisz_checkpoint(tmp_path, _checkpoint(workflow="nowy"))

    monkeypatch.undo()
    assert wczytaj_checkpoint(tmp_path) == stary      # polowiczny zapis nie wyladowal w pliku docelowym


def test_nieudana_podmiana_nie_zostawia_pliku_tymczasowego_w_katalogu_runu(tmp_path, monkeypatch):
    zapisz_checkpoint(tmp_path, _checkpoint(workflow="stary"))

    def replace(src, dst, *argumenty, **opcje):
        raise OSError("dysk pelny")

    monkeypatch.setattr(os, "replace", replace)

    with pytest.raises(BladCheckpointu):
        zapisz_checkpoint(tmp_path, _checkpoint(workflow="nowy"))

    monkeypatch.undo()
    assert [p.name for p in tmp_path.iterdir()] == [PLIK]


def test_nieudana_synchronizacja_na_dysk_nie_zostawia_pliku_tymczasowego_w_katalogu_runu(
    tmp_path, monkeypatch,
):
    def fsync(fd):
        raise OSError("blad dysku")

    monkeypatch.setattr(os, "fsync", fsync)

    with pytest.raises(BladCheckpointu):
        zapisz_checkpoint(tmp_path, _checkpoint())

    monkeypatch.undo()
    assert list(tmp_path.iterdir()) == []


def test_blad_sprzatania_pliku_tymczasowego_nie_zastepuje_bledu_zapisu_checkpointu(
    tmp_path, monkeypatch,
):
    def replace(src, dst, *argumenty, **opcje):
        raise OSError("dysk pelny")

    def unlink(sciezka, *argumenty, **opcje):
        raise OSError("nie da sie usunac")

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "unlink", unlink)

    with pytest.raises(BladCheckpointu, match="dysk pelny"):
        zapisz_checkpoint(tmp_path, _checkpoint())


def test_zapis_do_nieistniejacego_katalogu_to_blad_checkpointu(tmp_path):
    with pytest.raises(BladCheckpointu):
        zapisz_checkpoint(tmp_path / "nie_ma_takiego_katalogu", _checkpoint())


@pytest.mark.skipif(os.geteuid() == 0, reason="root pisze do katalogu bez praw zapisu")
def test_zapis_do_katalogu_bez_praw_zapisu_to_blad_checkpointu(tmp_path):
    katalog = tmp_path / "tylko_do_odczytu"
    katalog.mkdir()
    katalog.chmod(0o500)
    try:
        with pytest.raises(BladCheckpointu):
            zapisz_checkpoint(katalog, _checkpoint())
    finally:
        katalog.chmod(0o700)


def test_brak_pliku_checkpointu_to_blad_checkpointu(tmp_path):
    with pytest.raises(BladCheckpointu, match="odczyt checkpointu"):
        wczytaj_checkpoint(tmp_path)


def test_katalog_zamiast_pliku_checkpointu_to_blad_checkpointu(tmp_path):
    (tmp_path / PLIK).mkdir()

    with pytest.raises(BladCheckpointu, match="odczyt checkpointu"):
        wczytaj_checkpoint(tmp_path)


@pytest.mark.skipif(os.geteuid() == 0, reason="root czyta plik bez praw odczytu")
def test_checkpoint_bez_praw_odczytu_to_blad_checkpointu(tmp_path):
    zapisz_checkpoint(tmp_path, _checkpoint())
    (tmp_path / PLIK).chmod(0o000)
    try:
        with pytest.raises(BladCheckpointu):
            wczytaj_checkpoint(tmp_path)
    finally:
        (tmp_path / PLIK).chmod(0o600)


@pytest.mark.parametrize("zawartosc,komunikat", [
    (b"", "nie jest poprawnym JSON"),
    (b"{nie json", "nie jest poprawnym JSON"),
    (b"\xff\xfe\x00{", "odczyt checkpointu"),
    (b"[" * 200_000, "nie jest poprawnym JSON"),
], ids=["pusty_plik", "nie_json", "nie_utf8", "zlosliwe_zagniezdzenie"])
def test_nieczytelna_zawartosc_checkpointu_to_blad_checkpointu(tmp_path, zawartosc, komunikat):
    (tmp_path / PLIK).write_bytes(zawartosc)

    with pytest.raises(BladCheckpointu, match=komunikat):
        wczytaj_checkpoint(tmp_path)


def _bez(*sciezka: str):
    def zmien(dane: dict) -> dict:
        wskaznik = dane
        for klucz in sciezka[:-1]:
            wskaznik = wskaznik[klucz]
        del wskaznik[sciezka[-1]]
        return dane
    return zmien


def _ustaw(*sciezka_i_wartosc):
    *sciezka, wartosc = sciezka_i_wartosc

    def zmien(dane: dict) -> dict:
        wskaznik = dane
        for klucz in sciezka[:-1]:
            wskaznik = wskaznik[klucz]
        wskaznik[sciezka[-1]] = wartosc
        return dane
    return zmien


PROBY_BLAD = "checkpoint: proby kroku a nie sa liczba calkowita"


USZKODZENIA = {
    "brak_run_id": (_bez("run_id"), "checkpoint: run_id nie jest napisem"),
    "brak_repozytorium": (_bez("repozytorium"), "checkpoint: repozytorium nie jest napisem"),
    "brak_workflow": (_bez("workflow"), "checkpoint: workflow nie jest napisem"),
    "brak_base_commit": (_bez("base_commit"), "checkpoint: base_commit nie jest napisem"),
    "brak_worktree": (_bez("worktree"), "checkpoint: worktree nie jest napisem"),
    "brak_kroki": (_bez("kroki"), "checkpoint: kroki nie jest obiektem"),
    "brak_waiting_on": (_bez("waiting_on"), "checkpoint: brak klucza waiting_on"),
    "brak_step_id_w_waiting_on": (
        _bez("waiting_on", "step_id"), "checkpoint: waiting_on.step_id nie jest napisem"),
    "brak_od_w_waiting_on": (
        _bez("waiting_on", "od"), "checkpoint: waiting_on.od nie jest napisem"),
    "brak_stanu_kroku": (
        _bez("kroki", "a", "stan"), "checkpoint: stan kroku a nie jest napisem"),
    "brak_prob_kroku": (_bez("kroki", "a", "proby"), PROBY_BLAD),
    "proby_jako_napis": (_ustaw("kroki", "a", "proby", "1"), PROBY_BLAD),
    "proby_null": (_ustaw("kroki", "a", "proby", None), PROBY_BLAD),
    # bool jest int-em w Pythonie
    "proby_jako_prawda": (_ustaw("kroki", "a", "proby", True), PROBY_BLAD),
    "proby_jako_falsz": (_ustaw("kroki", "a", "proby", False), PROBY_BLAD),
    "proby_jako_ulamek": (_ustaw("kroki", "a", "proby", 1.5), PROBY_BLAD),
    "stan_jako_liczba": (
        _ustaw("kroki", "a", "stan", 5), "checkpoint: stan kroku a nie jest napisem"),
    "krok_jako_napis": (
        _ustaw("kroki", "a", "oczekuje"), "checkpoint: krok a nie jest obiektem"),
    "kroki_jako_lista": (_ustaw("kroki", []), "checkpoint: kroki nie jest obiektem"),
    "run_id_jako_liczba": (_ustaw("run_id", 5), "checkpoint: run_id nie jest napisem"),
    "repozytorium_null": (
        _ustaw("repozytorium", None), "checkpoint: repozytorium nie jest napisem"),
    "waiting_on_jako_napis": (
        _ustaw("waiting_on", "a"), "checkpoint: waiting_on nie jest obiektem"),
    "step_id_oczekiwania_jako_liczba": (
        _ustaw("waiting_on", "step_id", 5), "checkpoint: waiting_on.step_id nie jest napisem"),
    "od_oczekiwania_jako_liczba": (
        _ustaw("waiting_on", "od", 5), "checkpoint: waiting_on.od nie jest napisem"),
}


@pytest.mark.parametrize("uszkodz,komunikat", list(USZKODZENIA.values()), ids=list(USZKODZENIA))
def test_checkpoint_o_zlym_ksztalcie_to_blad_checkpointu_ze_wskazaniem_pola(
    tmp_path, uszkodz, komunikat,
):
    zapisz_checkpoint(tmp_path, _checkpoint())
    dane = uszkodz(_surowy_json(tmp_path))
    (tmp_path / PLIK).write_text(json.dumps(dane), encoding="utf-8")

    # cały komunikat (kotwice): sam podciąg przepuściłby dopisany przedrostek albo przyrostek
    with pytest.raises(BladCheckpointu, match=f"^{re.escape(komunikat)}$"):
        wczytaj_checkpoint(tmp_path)


@pytest.mark.parametrize("tresc", ["[]", "null", '"napis"', "5"],
                         ids=["lista", "null", "napis", "liczba"])
def test_checkpoint_ktory_nie_jest_obiektem_json_to_blad_checkpointu(tmp_path, tresc):
    (tmp_path / PLIK).write_text(tresc, encoding="utf-8")

    with pytest.raises(BladCheckpointu, match=r"^checkpoint: korzen nie jest obiektem$"):
        wczytaj_checkpoint(tmp_path)


@pytest.mark.parametrize("obiekt", [
    Oczekiwanie("a", "x"),
    StanKrokuCheckpointu("oczekuje", 0),
    _checkpoint(),
], ids=["oczekiwanie", "stan_kroku", "checkpoint"])
def test_dane_checkpointu_sa_niezmienne(obiekt):
    pole = next(iter(obiekt.__dataclass_fields__))

    with pytest.raises(FrozenInstanceError):
        setattr(obiekt, pole, "inne")


# =====================================================================================
# 6. Migracja 0011: plik opisuje downgrade (kryterium 10, czesc niezalezna od PostgreSQL)
# =====================================================================================


def test_plik_migracji_run_odrzucony_opisuje_downgrade_i_laczy_sie_z_poprzednia_migracja():
    pliki = list((KORZEN / "alembic" / "versions").glob("0011_*.py"))
    assert len(pliki) == 1, pliki
    tresc = pliki[0].read_text(encoding="utf-8")

    opis = ast.get_docstring(ast.parse(tresc)) or ""
    assert "downgrade" in opis.lower()
    assert "przebudow" in opis.lower()              # typ enum nie traci wartosci inaczej niz przebudowa
    assert "failed" in opis                         # co dzieje sie z runami `rejected`
    assert 'revision: str = "0011"' in tresc or "revision = '0011'" in tresc or 'revision = "0011"' in tresc
    assert '"0010"' in tresc or "'0010'" in tresc


def test_migracje_alembica_tworza_jeden_lancuch_z_0011_po_0010():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(KORZEN / "alembic.ini"))
    cfg.set_main_option("script_location", str(KORZEN / "alembic"))
    skrypty = ScriptDirectory.from_config(cfg)

    assert len(skrypty.get_heads()) == 1
    assert skrypty.get_revision("0011").down_revision == "0010"
