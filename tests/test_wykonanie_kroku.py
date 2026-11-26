"""Wykonanie kroku u runnera: status z kodu wyjscia i artefaktow produces (HAR-42).

Prawdziwe procesy: atrapa agenta to maly skrypt Pythona sterowany jednym argumentem
JSON. Lezy poza katalogiem roboczym runu (jest w `-c`), bo inaczej migawka
recenzenta widzialaby ja jako zmiane.
"""
import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path

import pytest

from cockpit.services.invariants import Rola
from procesy import (
    WlasnaGrupaProcesow,
    czekaj,
    czekaj_na_warunek,
    grupa_umarla,
    zabij_grupe,
    zabij_proces,
)
from cockpit.settings import settings
from runner.agent import SpecKroku
from cockpit.services.walidacja import KATALOG_SKILLI_RUNU, migawka
from runner import krok as modul_kroku
from runner.krok import RAPORTY_NAPRAWY_SILNIKA, WYJATKI_MIGAWKI, WynikKroku, wykonaj_krok
from runner.silnik import _sciezka_raportu
from test_pomiar_kroku import _agent_z_atrapa, result
from test_silnik_runu import K, jedno, rodzaje, swiat as swiat_silnika, uruchom as uruchom_run

# Kolejnosc: wnuk (zeby pidfile istnial zanim cokolwiek zostanie wyemitowane),
# linie, pliki, spanie, kod. Kazda linia jest flushowana, zeby kolejnosc stdout/stderr
# w scalonym strumieniu byla deterministyczna.
KOD_ATRAPY = r"""
import json, os, subprocess, sys, time
from pathlib import Path

p = json.loads(sys.argv[1])
if "wnuk" in p:
    w = subprocess.Popen(["sleep", "60"])
    tmp = p["wnuk"] + ".tmp"
    Path(tmp).write_text(f"{os.getpgid(0)} {w.pid}")
    os.replace(tmp, p["wnuk"])
for kanal, tekst in p.get("linie", []):
    strumien = sys.stderr if kanal == "err" else sys.stdout
    strumien.write(tekst)
    strumien.flush()
if "bajty" in p:
    sys.stdout.buffer.write(bytes.fromhex(p["bajty"]))
    sys.stdout.buffer.flush()
if "dluga" in p:
    sys.stdout.write("x" * p["dluga"] + "\n")
    sys.stdout.flush()
for sciezka, tresc in p.get("zapisz", {}).items():
    f = Path(sciezka)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(tresc)
for sciezka in p.get("usun", []):
    Path(sciezka).unlink()
for sciezka, tryb in p.get("chmod", {}).items():
    os.chmod(sciezka, tryb)
if "wnuk" in p:
    time.sleep(60)
sys.exit(p.get("kod", 0))
"""


def atrapa(**opis) -> list[str]:
    return [sys.executable, "-I", "-c", KOD_ATRAPY, json.dumps(opis)]


def spec(rola: Rola = Rola.author, wyjscia: list[str] | None = None) -> SpecKroku:
    # Wartosci identyfikatorow sa rozne, zeby pomylone pole bylo widoczne w zdarzeniu.
    return SpecKroku(
        run_id="run-17",
        step_id="krok-23",
        agent_id="agent-31",
        skill_id="pisanie",
        rola=rola,
        wejscia=[],
        wyjscia=list(wyjscia or []),
        komendy={},
    )


@pytest.fixture
def run(tmp_path: Path) -> Path:
    katalog = tmp_path / "run"
    katalog.mkdir()
    return katalog


async def uruchom(
    krok: SpecKroku, polecenie: list[str], katalog: Path
) -> tuple[WynikKroku, list[dict]]:
    zdarzenia: list[dict] = []

    async def emit(zdarzenie: dict) -> None:
        zdarzenia.append(zdarzenie)

    wynik = await asyncio.wait_for(wykonaj_krok(krok, polecenie, katalog, emit), 30)
    return wynik, zdarzenia


def zdarzenie_wyjscia(tekst: str) -> dict:
    return {
        "kind": "output",
        "run_id": "run-17",
        "step_id": "krok-23",
        "agent_id": "agent-31",
        "text": tekst,
    }


NARUSZENIE = "niezmiennik reviewer_readonly: "


# --- status z kodu wyjscia i artefaktow -------------------------------------


async def test_krok_z_kodem_zero_i_wszystkimi_artefaktami_jest_zakonczony(run):
    wynik, _ = await uruchom(
        spec(wyjscia=["a.md", "dir/b.md"]),
        atrapa(zapisz={"a.md": "pierwszy", "dir/b.md": "drugi"}),
        run,
    )

    assert wynik == WynikKroku(status="zakonczony", powod=None, kod_wyjscia=0)


async def test_krok_bez_deklarowanych_artefaktow_i_z_kodem_zero_jest_zakonczony(run):
    wynik, _ = await uruchom(spec(), atrapa(), run)

    assert wynik == WynikKroku(status="zakonczony", powod=None, kod_wyjscia=0)


async def test_brak_drugiego_artefaktu_jest_nazwany_w_powodzie(run):
    wynik, _ = await uruchom(
        spec(wyjscia=["a.md", "b.md"]), atrapa(zapisz={"a.md": "jest"}), run
    )

    assert wynik == WynikKroku(status="nieudany", powod="brak artefaktu b.md", kod_wyjscia=0)


async def test_brak_pierwszego_artefaktu_wygrywa_z_pustym_drugim(run):
    wynik, _ = await uruchom(
        spec(wyjscia=["a.md", "b.md"]), atrapa(zapisz={"b.md": ""}), run
    )

    assert wynik == WynikKroku(status="nieudany", powod="brak artefaktu a.md", kod_wyjscia=0)


async def test_pusty_pierwszy_artefakt_wygrywa_z_brakiem_drugiego(run):
    wynik, _ = await uruchom(
        spec(wyjscia=["a.md", "b.md"]), atrapa(zapisz={"a.md": ""}), run
    )

    assert wynik == WynikKroku(
        status="nieudany", powod=f"niezmiennik evidence_required: plik dowodu jest pusty: {run / 'a.md'}", kod_wyjscia=0
    )


async def test_pusty_artefakt_to_brak_dowodu_a_nie_zielone_przejscie(run):
    wynik, _ = await uruchom(spec(wyjscia=["werdykt.md"]), atrapa(zapisz={"werdykt.md": ""}), run)

    assert wynik == WynikKroku(
        status="nieudany",
        powod=f"niezmiennik evidence_required: plik dowodu jest pusty: {run / 'werdykt.md'}",
        kod_wyjscia=0,
    )
    assert (run / "werdykt.md").is_file()


async def test_artefakt_z_samych_bialych_znakow_to_brak_dowodu(run):
    wynik, _ = await uruchom(
        spec(wyjscia=["werdykt.md"]), atrapa(zapisz={"werdykt.md": " \n\t \n"}), run
    )

    assert wynik == WynikKroku(
        status="nieudany",
        powod=f"niezmiennik evidence_required: plik dowodu jest pusty: {run / 'werdykt.md'}",
        kod_wyjscia=0,
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root czyta pliki bez wzgledu na prawa")
async def test_artefakt_ktorego_nie_da_sie_odczytac_to_porazka_kroku_z_nazwa_pliku(run):
    try:
        wynik, _ = await uruchom(
            spec(wyjscia=["tajny.md"]),
            atrapa(zapisz={"tajny.md": "tresc"}, chmod={"tajny.md": 0}),
            run,
        )
    finally:
        (run / "tajny.md").chmod(0o644)

    assert wynik.status == "nieudany"
    assert wynik.kod_wyjscia == 0
    assert wynik.powod is not None
    assert wynik.powod.startswith("nie można odczytać artefaktu tajny.md: ")


@pytest.mark.skipif(os.geteuid() == 0, reason="root przeszukuje katalogi bez wzgledu na prawa")
async def test_artefakt_w_katalogu_bez_prawa_przeszukiwania_to_brak_artefaktu(run):
    katalog = run / "sub"
    try:
        wynik, _ = await uruchom(
            spec(Rola.author, wyjscia=["sub/a.md"]),
            atrapa(zapisz={"sub/a.md": "tresc"}, chmod={"sub": 0o600}),
            run,
        )
    finally:
        katalog.chmod(0o755)

    assert wynik == WynikKroku(status="nieudany", powod="brak artefaktu sub/a.md", kod_wyjscia=0)
    assert (katalog / "a.md").read_text() == "tresc"


@pytest.mark.parametrize("kod", [3, 7])
async def test_kod_wyjscia_rozny_od_zera_jest_w_powodzie_porazki(run, kod):
    wynik, _ = await uruchom(spec(), atrapa(kod=kod), run)

    assert wynik == WynikKroku(status="nieudany", powod=f"kod wyjścia {kod}", kod_wyjscia=kod)


async def test_kod_wyjscia_ma_pierwszenstwo_przed_brakiem_artefaktow(run):
    wynik, _ = await uruchom(spec(wyjscia=["a.md"]), atrapa(kod=3), run)

    assert wynik == WynikKroku(status="nieudany", powod="kod wyjścia 3", kod_wyjscia=3)


# --- reviewer_readonly -------------------------------------------------------


async def test_recenzent_tworzacy_nowy_plik_poza_katalogiem_runu_jest_nieudany(run):
    wynik, _ = await uruchom(
        spec(Rola.reviewer), atrapa(zapisz={"nowy.txt": "samowola"}), run
    )

    assert wynik == WynikKroku(status="nieudany", powod=NARUSZENIE + "nowe: nowy.txt", kod_wyjscia=0)
    assert (run / "nowy.txt").read_text() == "samowola"


async def test_recenzent_zmieniajacy_istniejacy_plik_jest_nieudany(run):
    (run / "istniejacy.txt").write_text("stare")

    wynik, _ = await uruchom(
        spec(Rola.reviewer), atrapa(zapisz={"istniejacy.txt": "nowe"}), run
    )

    assert wynik == WynikKroku(status="nieudany", powod=NARUSZENIE + "zmienione: istniejacy.txt", kod_wyjscia=0)
    assert (run / "istniejacy.txt").read_text() == "nowe"


async def test_recenzent_usuwajacy_istniejacy_plik_jest_nieudany(run):
    (run / "istniejacy.txt").write_text("stare")

    wynik, _ = await uruchom(spec(Rola.reviewer), atrapa(usun=["istniejacy.txt"]), run)

    assert wynik == WynikKroku(status="nieudany", powod=NARUSZENIE + "usuniete: istniejacy.txt", kod_wyjscia=0)
    assert not (run / "istniejacy.txt").exists()


async def test_recenzent_piszacy_tylko_do_katalogu_skilli_runu_jest_zakonczony(run):
    (run / "istniejacy.txt").write_text("stare")

    wynik, _ = await uruchom(
        spec(Rola.reviewer),
        atrapa(zapisz={".cockpit/_run/skills/x.md": "werdykt: ok"}),
        run,
    )

    assert wynik == WynikKroku(status="zakonczony", powod=None, kod_wyjscia=0)
    assert (run / ".cockpit" / "_run" / "skills" / "x.md").read_text() == "werdykt: ok"


async def test_recenzent_piszacy_do_katalogu_runu_poza_skillami_jest_nieudany(run):
    wynik, _ = await uruchom(
        spec(Rola.reviewer),
        atrapa(zapisz={".cockpit/_run/x": "samowola"}),
        run,
    )

    assert wynik == WynikKroku(
        status="nieudany", powod=NARUSZENIE + "nowe: .cockpit/_run/x", kod_wyjscia=0)
    assert (run / ".cockpit" / "_run" / "x").read_text() == "samowola"


async def test_zbieracz_piszacy_do_katalogu_runu_poza_skillami_jest_nieudany(run):
    wynik, _ = await uruchom(
        spec(Rola.gatherer),
        atrapa(zapisz={".cockpit/_run/x": "samowola"}),
        run,
    )

    assert wynik == WynikKroku(
        status="nieudany", powod=NARUSZENIE + "nowe: .cockpit/_run/x", kod_wyjscia=0)


async def test_recenzent_zmieniajacy_istniejacy_plik_w_katalogu_runu_poza_skillami_jest_nieudany(run):
    ruch = run / ".cockpit" / "_run" / "x"
    ruch.parent.mkdir(parents=True)
    ruch.write_text("stare")

    wynik, _ = await uruchom(
        spec(Rola.reviewer), atrapa(zapisz={".cockpit/_run/x": "nowe"}), run)

    assert wynik == WynikKroku(
        status="nieudany", powod=NARUSZENIE + "zmienione: .cockpit/_run/x", kod_wyjscia=0)


async def test_pliki_skilli_runu_obecne_przed_krokiem_recenzenta_nie_sa_naruszeniem(run):
    skill = run / ".cockpit" / "_run" / "skills" / "recenzja" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("skill skopiowany przez runner przed krokiem")

    wynik, _ = await uruchom(
        spec(Rola.reviewer),
        atrapa(zapisz={".cockpit/_run/skills/x.md": "werdykt: ok"}),
        run,
    )

    assert wynik == WynikKroku(status="zakonczony", powod=None, kod_wyjscia=0)
    assert skill.read_text() == "skill skopiowany przez runner przed krokiem"
    assert (run / ".cockpit" / "_run" / "skills" / "x.md").read_text() == "werdykt: ok"


async def test_artefakty_silnika_zapisane_przed_krokiem_recenzenta_nie_sa_naruszeniem(run):
    """Decyzje czlowieka z `.cockpit/_run/artifacts` istnieja przed krokiem i sa w migawce `przed`;
    raport naprawy nie zawsze (silnik zapisuje go tez w trakcie rownoleglego kroku, zob. test silnika)."""
    decyzja = run / ".cockpit" / "_run" / "artifacts" / "decyzja_a.json"
    decyzja.parent.mkdir(parents=True)
    decyzja.write_text('{"decyzja": "zatwierdz"}')

    wynik, _ = await uruchom(spec(Rola.reviewer), atrapa(), run)

    assert wynik == WynikKroku(status="zakonczony", powod=None, kod_wyjscia=0)
    assert decyzja.read_text() == '{"decyzja": "zatwierdz"}'


async def test_wyjatek_katalogu_skilli_runu_nie_chroni_zapisu_poza_nim(run):
    wynik, _ = await uruchom(
        spec(Rola.reviewer),
        atrapa(zapisz={".cockpit/_run/skills/x.md": "werdykt", "nowy.txt": "poza"}),
        run,
    )

    assert wynik == WynikKroku(status="nieudany", powod=NARUSZENIE + "nowe: nowy.txt", kod_wyjscia=0)


async def test_zapis_obok_katalogu_skilli_runu_w_cockpit_jest_naruszeniem(run):
    wynik, _ = await uruchom(
        spec(Rola.reviewer),
        atrapa(zapisz={".cockpit/_run/skills/x.md": "werdykt", ".cockpit/inny.md": "poza"}),
        run,
    )

    assert wynik == WynikKroku(status="nieudany", powod=NARUSZENIE + "nowe: .cockpit/inny.md", kod_wyjscia=0)


async def test_recenzent_przy_nieczytelnym_katalogu_repozytorium_nie_startuje_agenta(
    run, tmp_path, monkeypatch
):
    """Root ignoruje prawa do katalogow, wiec nieczytelny podkatalog symuluje `os.walk` z `onerror`."""
    (run / "a.txt").write_text("tresc")
    znacznik = tmp_path / "agent-wystartowal"

    def walk_z_nieczytelnym_podkatalogiem(top, topdown=True, onerror=None, followlinks=False):
        yield (str(top), ["zamkniety"], ["a.txt"])
        if onerror is not None:
            onerror(PermissionError(13, "Brak dostępu", str(Path(top) / "zamkniety")))

    monkeypatch.setattr("cockpit.services.walidacja.os.walk", walk_z_nieczytelnym_podkatalogiem)

    wynik, _ = await uruchom(
        spec(Rola.reviewer), atrapa(zapisz={str(znacznik): "x"}), run)

    assert wynik.status == "nieudany"
    assert wynik.powod is not None
    assert wynik.powod.startswith("nie można wykonać migawki katalogu: ")
    assert wynik.kod_wyjscia is None
    assert not znacznik.exists()


# --- evidence_required: agent z kodem 0 i bez wyniku nie jest zielony -------------------------

KOD_AGENTA_PUSTY_WYNIK = r"""
import json
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": ""}))
"""


async def test_autor_z_kodem_zero_i_pustym_wynikiem_nie_oddajacy_artefaktu_jest_nieudany(run, tmp_path):
    katalog_runu = tmp_path / "katalog-runu"
    katalog_runu.mkdir()
    zdarzenia: list[dict] = []

    async def emit(zdarzenie: dict) -> None:
        zdarzenia.append(zdarzenie)

    wynik = await asyncio.wait_for(
        wykonaj_krok(
            spec(Rola.author, wyjscia=["plan.md"]),
            [sys.executable, "-I", "-c", KOD_AGENTA_PUSTY_WYNIK],
            run, emit, katalog_runu=katalog_runu, prompt="prompt kroku"),
        30)

    assert wynik.status == "nieudany"
    assert wynik.kod_wyjscia == 0
    assert wynik.powod == "brak artefaktu plan.md"
    assert not (run / "plan.md").exists()


async def test_krok_agenta_z_kodem_zero_i_pustym_wynikiem_konczy_sie_zdarzeniem_step_failed(tmp_path):
    s = swiat_silnika(
        tmp_path, [K("a")], opcje_skilli={"a": {"role": "reviewer", "produces": ["plan.md"]}})
    agent = _agent_z_atrapa(tmp_path, a=[result(result="")])

    zdarzenia = await uruchom_run(s, agent)

    assert jedno(zdarzenia, "step_failed", "a")["text"] == (
        "niezmiennik evidence_required: pusty wynik agenta, brak artefaktu plan.md")
    assert "step_finished" not in rodzaje(zdarzenia)


async def test_zbieracz_piszacy_poza_katalogiem_runu_jest_nieudany(run):
    wynik, _ = await uruchom(
        spec(Rola.gatherer), atrapa(zapisz={"nowy.txt": "samowola"}), run
    )

    assert wynik == WynikKroku(status="nieudany", powod=NARUSZENIE + "nowe: nowy.txt", kod_wyjscia=0)
    assert (run / "nowy.txt").is_file()


async def test_autor_piszacy_poza_katalogiem_runu_jest_zakonczony(run):
    wynik, _ = await uruchom(spec(Rola.author), atrapa(zapisz={"nowy.txt": "praca"}), run)

    assert wynik == WynikKroku(status="zakonczony", powod=None, kod_wyjscia=0)
    assert (run / "nowy.txt").read_text() == "praca"


async def test_wykonawca_zmieniajacy_istniejacy_plik_jest_zakonczony(run):
    (run / "istniejacy.txt").write_text("stare")

    wynik, _ = await uruchom(
        spec(Rola.executor), atrapa(zapisz={"istniejacy.txt": "nowe"}), run
    )

    assert wynik == WynikKroku(status="zakonczony", powod=None, kod_wyjscia=0)
    assert (run / "istniejacy.txt").read_text() == "nowe"


async def test_recenzent_z_kodem_bledu_dostaje_powod_z_kodem_a_nie_z_naruszenia(run):
    wynik, _ = await uruchom(
        spec(Rola.reviewer), atrapa(zapisz={"nowy.txt": "poza"}, kod=3), run
    )

    assert wynik == WynikKroku(status="nieudany", powod="kod wyjścia 3", kod_wyjscia=3)


# --- zdarzenia wyjscia -------------------------------------------------------


async def test_kazda_linia_wyjscia_trafia_do_zdarzen_z_identyfikatorami_kroku_po_kolei(run):
    wynik, zdarzenia = await uruchom(
        spec(),
        atrapa(
            linie=[
                ["out", "pierwsza\n"],
                ["err", "druga\n"],
                ["out", "trzecia\n"],
                ["out", "bez konca"],
            ]
        ),
        run,
    )

    assert wynik == WynikKroku(status="zakonczony", powod=None, kod_wyjscia=0)
    assert zdarzenia == [
        zdarzenie_wyjscia("pierwsza"),
        zdarzenie_wyjscia("druga"),
        zdarzenie_wyjscia("trzecia"),
        zdarzenie_wyjscia("bez konca"),
    ]


async def test_linia_tylko_ze_stderr_trafia_do_zdarzen(run):
    _, zdarzenia = await uruchom(spec(), atrapa(linie=[["err", "blad agenta\n"]]), run)

    assert zdarzenia == [zdarzenie_wyjscia("blad agenta")]


async def test_zakonczenie_linii_crlf_nie_zostaje_w_tekscie_zdarzenia(run):
    _, zdarzenia = await uruchom(spec(), atrapa(linie=[["out", "windowsowa\r\n"]]), run)

    assert zdarzenia == [zdarzenie_wyjscia("windowsowa")]


async def test_bajty_spoza_utf8_nie_wywracaja_kroku_i_sa_zastapione(run):
    wynik, zdarzenia = await uruchom(spec(), atrapa(bajty="ff0a"), run)

    assert wynik == WynikKroku(status="zakonczony", powod=None, kod_wyjscia=0)
    assert zdarzenia == [zdarzenie_wyjscia("�")]


async def test_wyjscie_nieudanego_kroku_tez_trafia_do_zdarzen(run):
    wynik, zdarzenia = await uruchom(spec(), atrapa(linie=[["out", "przed bledem\n"]], kod=3), run)

    assert wynik == WynikKroku(status="nieudany", powod="kod wyjścia 3", kod_wyjscia=3)
    assert zdarzenia == [zdarzenie_wyjscia("przed bledem")]


async def test_linia_dluzsza_niz_limit_zdarzenia_nie_wywraca_kroku_i_jest_wyemitowana_obcieta_z_pelna_dlugoscia(run):
    dlugosc = 70_000
    limit = settings.limit_tekstu_zdarzenia

    wynik, zdarzenia = await uruchom(spec(), atrapa(dluga=dlugosc), run)

    assert wynik == WynikKroku(status="zakonczony", powod=None, kod_wyjscia=0)
    assert all(z["kind"] == "output" for z in zdarzenia)
    assert dlugosc > limit
    assert all(len(z["text"]) <= limit for z in zdarzenia)
    assert [(len(z["text"]), z["text_full_len"]) for z in zdarzenia] == [(limit, dlugosc)]


# --- blad startu -------------------------------------------------------------


async def test_nieistniejace_polecenie_to_nieudany_krok_bez_kodu_wyjscia(run):
    wynik, zdarzenia = await uruchom(spec(), ["/nie/ma/takiego/polecenia"], run)

    assert wynik.status == "nieudany"
    assert wynik.kod_wyjscia is None
    assert wynik.powod is not None
    assert wynik.powod.startswith("nie można uruchomić polecenia: ")
    assert zdarzenia == []


async def test_nieistniejacy_katalog_roboczy_to_nieudany_krok_bez_kodu_wyjscia(tmp_path):
    wynik, _ = await uruchom(spec(), atrapa(), tmp_path / "nie-ma-takiego-katalogu")

    assert wynik.status == "nieudany"
    assert wynik.kod_wyjscia is None
    assert wynik.powod is not None
    assert wynik.powod.startswith("nie można uruchomić polecenia: ")


# --- nieczytelne pliki w katalogu runu ----------------------------------------

BEZ_PRAW = pytest.mark.skipif(os.geteuid() == 0, reason="root czyta pliki bez wzgledu na prawa")


@BEZ_PRAW
async def test_recenzent_z_nieczytelnym_plikiem_w_katalogu_runu_nie_startuje_a_krok_jest_nieudany(
    run, tmp_path
):
    nieczytelny = run / "tajny.txt"
    nieczytelny.write_text("tresc")
    nieczytelny.chmod(0)
    znacznik = tmp_path / "znacznik-startu.txt"
    try:
        wynik, zdarzenia = await uruchom(
            spec(Rola.reviewer),
            atrapa(zapisz={str(znacznik): "proces wystartowal"}, linie=[["out", "x\n"]]),
            run,
        )
    finally:
        nieczytelny.chmod(0o644)

    assert wynik.status == "nieudany"
    assert wynik.kod_wyjscia is None
    assert wynik.powod is not None
    assert wynik.powod.startswith("nie można wykonać migawki katalogu: ")
    assert zdarzenia == []
    assert not znacznik.exists()


@BEZ_PRAW
async def test_recenzent_tworzacy_nieczytelny_plik_ma_nieudany_krok_z_kodem_zero(run):
    nowy = run / "nowy.txt"
    try:
        wynik, _ = await uruchom(
            spec(Rola.reviewer),
            atrapa(zapisz={"nowy.txt": "samowola"}, chmod={"nowy.txt": 0}),
            run,
        )
        istnieje = nowy.exists()
    finally:
        if nowy.exists():
            nowy.chmod(0o644)

    assert wynik.status == "nieudany"
    assert wynik.kod_wyjscia == 0
    assert wynik.powod is not None
    assert wynik.powod.startswith("nie można sprawdzić niezmiennika reviewer_readonly: ")
    assert istnieje


@BEZ_PRAW
async def test_autor_z_nieczytelnym_plikiem_w_katalogu_runu_jest_zakonczony(run):
    nieczytelny = run / "tajny.txt"
    nieczytelny.write_text("tresc")
    nieczytelny.chmod(0)
    try:
        wynik, _ = await uruchom(spec(Rola.author), atrapa(), run)
    finally:
        nieczytelny.chmod(0o644)

    assert wynik == WynikKroku(status="zakonczony", powod=None, kod_wyjscia=0)


# --- sprzatanie procesow -----------------------------------------------------


async def czekaj_na_pidfile(plik: Path, zadanie: asyncio.Task) -> tuple[int, int]:
    try:
        await czekaj_na_warunek(plik.is_file, 10, "pidfile atrapy")
    except BaseException:
        zadanie.cancel()      # krok nie moze zostac w tle, gdy atrapa nie zapisala pidfile
        raise
    pgid, wnuk = plik.read_text().split()
    return int(pgid), int(wnuk)


def sprzatnij_atrape(pgid: int, wnuk: int) -> None:
    """Zabezpieczenie testu: wnuk nie moze przezyc nawet przy zlej implementacji.
    Pod mutantem bez start_new_session atrapa dzieli grupe z pytestem: sygnal do grupy
    nie idzie, a test pada na `grupa_umarla`, nie na bledzie sprzatania w `finally`."""
    zabij_proces(wnuk)
    with contextlib.suppress(WlasnaGrupaProcesow):
        zabij_grupe(pgid)


async def test_anulowanie_kroku_zabija_cala_grupe_procesow_wraz_z_wnukiem(run, tmp_path):
    pidfile = tmp_path / "pid.txt"
    zdarzenia: list[dict] = []

    async def emit(zdarzenie: dict) -> None:
        zdarzenia.append(zdarzenie)

    zadanie = asyncio.create_task(
        wykonaj_krok(spec(), atrapa(wnuk=str(pidfile)), run, emit)
    )
    pgid, wnuk = await czekaj_na_pidfile(pidfile, zadanie)
    try:
        zadanie.cancel()
        with pytest.raises(asyncio.CancelledError):
            await czekaj(zadanie, 10, "koniec kroku")
        assert await grupa_umarla(pgid)
    finally:
        sprzatnij_atrape(pgid, wnuk)


async def test_wyjatek_z_emit_wychodzi_z_kroku_i_grupa_procesow_jest_martwa(run, tmp_path):
    pidfile = tmp_path / "pid.txt"

    class BladEmit(Exception):
        pass

    async def emit(zdarzenie: dict) -> None:
        raise BladEmit("odbiorca zdarzen padl")

    zadanie = asyncio.create_task(
        wykonaj_krok(
            spec(), atrapa(wnuk=str(pidfile), linie=[["out", "linia\n"]]), run, emit
        )
    )
    pgid, wnuk = await czekaj_na_pidfile(pidfile, zadanie)
    try:
        with pytest.raises(BladEmit, match="odbiorca zdarzen padl"):
            await czekaj(zadanie, 10, "koniec kroku")
        assert await grupa_umarla(pgid)
    finally:
        sprzatnij_atrape(pgid, wnuk)


def test_wynik_kroku_nie_przyjmuje_pol_pozycyjnie():
    # HAR-242 (G6): nowe pole w srodku listy po merge dwoch ticketow nie moze cicho
    # wpasc w zla pozycje - konstruktor wymaga argumentow nazwanych.
    with pytest.raises(TypeError):
        WynikKroku("zakonczony", None, 0)  # type: ignore[misc]


# --- HAR-209: raport naprawy silnika to jedyny wyjatek migawki poza skillami ---------------

SCIEZKA_RAPORTU_W_MIGAWCE = [
    ("implementuj", 1),
    ("implementuj", 12),
    ("cel-z-myslnikiem_2", 3),
]


@pytest.mark.parametrize(("cel", "nr"), SCIEZKA_RAPORTU_W_MIGAWCE)
def test_raport_naprawy_zapisany_przez_silnik_nie_jest_widoczny_w_migawce_recenzenta(
    tmp_path, cel, nr
):
    """Wzorzec `RAPORTY_NAPRAWY_SILNIKA` musi obejmowac sciezke, ktora silnik naprawde produkuje."""
    raport = tmp_path / _sciezka_raportu(cel, nr)
    raport.parent.mkdir(parents=True)
    raport.write_text("raport porazki")

    assert migawka(tmp_path, WYJATKI_MIGAWKI) == {}


@pytest.mark.parametrize(("cel", "nr"), SCIEZKA_RAPORTU_W_MIGAWCE)
def test_wzorzec_raportu_naprawy_sam_obejmuje_sciezke_produkowana_przez_silnik(tmp_path, cel, nr):
    raport = tmp_path / _sciezka_raportu(cel, nr)
    raport.parent.mkdir(parents=True)
    raport.write_text("raport porazki")

    assert migawka(tmp_path, [RAPORTY_NAPRAWY_SILNIKA]) == {}


@pytest.mark.parametrize("sciezka", [
    ".cockpit/_run/artifacts/implementuj/_naprawa/inny.md",       # inna nazwa pliku
    ".cockpit/_run/artifacts/implementuj/_naprawa/proba_1.txt",   # inne rozszerzenie
    ".cockpit/_run/artifacts/implementuj/proba_1.md",             # bez katalogu _naprawa
    ".cockpit/_run/artifacts/implementuj/inny/proba_1.md",        # inny katalog zamiast _naprawa
    ".cockpit/_run/artifacts/a/b/_naprawa/proba_1.md",            # o jeden poziom za gleboko
    ".cockpit/_run/artifacts/decyzja_implementuj.json",           # decyzja czlowieka
    ".cockpit/_run/inny/implementuj/_naprawa/proba_1.md",         # nie katalog artifacts
    ".cockpit/_run/artifacts/implementuj/_naprawa/proba_1.md/x",  # katalog zamiast pliku raportu
])
async def test_recenzent_zapisujacy_inny_plik_w_artefaktach_runu_jest_nieudany(run, sciezka):
    wynik, _ = await uruchom(spec(Rola.reviewer), atrapa(zapisz={sciezka: "samowola"}), run)

    assert wynik == WynikKroku(status="nieudany", powod=NARUSZENIE + f"nowe: {sciezka}", kod_wyjscia=0)


# Recenzent czeka (w prawdziwym `wykonaj_krok`, z prawdziwa migawka), az silnik zapisze raport
# porazki rownoleglego kroku; dopiero wtedy konczy sie z kodem 0.
KOD_RECENZENTA_CZEKAJACEGO_NA_RAPORT = r"""
import glob, sys, time
open(sys.argv[1], "w").close()
koniec = time.monotonic() + 25
while time.monotonic() < koniec:
    if glob.glob(".cockpit/_run/artifacts/*/_naprawa/proba_*.md"):
        sys.exit(0)
    time.sleep(0.05)
sys.exit(3)
"""


async def test_recenzent_startujacy_gdy_raport_naprawy_juz_lezy_w_worktree_nie_ma_naruszenia(run):
    """Raport zapisany PRZED krokiem (ponowienie, kolejny krok) jest w migawce `przed`; migawka `po`
    go pomija, wiec wyjatek musi dzialac w obu migawkach, inaczej fałszywe `usuniete`."""
    raport = run / _sciezka_raportu("implementuj", 1)
    raport.parent.mkdir(parents=True)
    raport.write_text("raport porazki")

    wynik, _ = await uruchom(spec(Rola.reviewer), atrapa(), run)

    assert wynik == WynikKroku(status="zakonczony", powod=None, kod_wyjscia=0)
    assert raport.read_text() == "raport porazki"


async def test_wyjatek_raportu_naprawy_tylko_w_migawce_po_kroku_daje_falszywe_usuniecie(
    run, monkeypatch
):
    """Odtwarza wada: migawka `przed` bez wyjatku raportu (jak po zamianie argumentu w `wykonaj_krok`)."""
    raport = run / _sciezka_raportu("implementuj", 1)
    raport.parent.mkdir(parents=True)
    raport.write_text("raport porazki")
    prawdziwa = modul_kroku.migawka

    def migawka_przed_bez_raportu(katalog, wyjatki=()):
        return prawdziwa(katalog, [KATALOG_SKILLI_RUNU])

    monkeypatch.setattr(modul_kroku, "migawka", migawka_przed_bez_raportu)

    wynik, _ = await uruchom(spec(Rola.reviewer), atrapa(), run)

    assert wynik == WynikKroku(
        status="nieudany",
        powod=NARUSZENIE + "usuniete: " + _sciezka_raportu("implementuj", 1).as_posix(),
        kod_wyjscia=0)


async def _run_z_rownoleglym_recenzentem(tmp_path) -> tuple[list[dict], object]:
    s = swiat_silnika(
        tmp_path,
        [K("implementuj"),
         K("testuj", ("implementuj",), extra=", on_fail: {retry: implementuj, max: 1}"),
         K("recenzja")],
        opcje_skilli={"recenzja": {"role": "reviewer"}})
    wystartowal = tmp_path / "recenzent-wystartowal"
    proby_testuj: list[int] = []

    async def agent(spec_kroku, polecenie, katalog, emit, *, katalog_runu=None, prompt=None, **reszta):
        if spec_kroku.step_id == "recenzja":
            return await wykonaj_krok(
                spec_kroku,
                [sys.executable, "-I", "-c", KOD_RECENZENTA_CZEKAJACEGO_NA_RAPORT, str(wystartowal)],
                katalog, emit, katalog_runu=katalog_runu)
        if spec_kroku.step_id == "testuj":
            proby_testuj.append(1)
            if len(proby_testuj) == 1:
                # porazka dopiero, gdy recenzent ma juz migawke `przed` (proces wystartowal po niej)
                for _ in range(400):
                    if wystartowal.exists():
                        break
                    await asyncio.sleep(0.05)
                return WynikKroku(status="nieudany", powod="testy padly", kod_wyjscia=1)
        return WynikKroku(status="zakonczony", powod=None, kod_wyjscia=0)

    zdarzenia = await uruchom_run(s, agent, limit=60)
    assert wystartowal.exists()
    return zdarzenia, s


def _kroki_o_rodzaju(zdarzenia: list[dict], rodzaj: str, krok: str) -> list[dict]:
    return [z for z in zdarzenia if z["kind"] == rodzaj and z["step_id"] == krok]


async def test_recenzent_trwajacy_gdy_rownolegly_krok_zapisuje_raport_naprawy_konczy_sie_sukcesem(
    tmp_path
):
    zdarzenia, s = await _run_z_rownoleglym_recenzentem(tmp_path)

    assert len(_kroki_o_rodzaju(zdarzenia, "step_finished", "recenzja")) == 1
    assert _kroki_o_rodzaju(zdarzenia, "step_failed", "recenzja") == []
    assert (s.worktree / _sciezka_raportu("implementuj", 1)).is_file()


async def test_bez_wyjatku_dla_raportu_naprawy_recenzent_rownoleglego_kroku_bylby_nieudany(
    tmp_path, monkeypatch
):
    """Dowod, ze test powyzej rozstrzyga o wyjatku, a nie o czasie: ten sam run bez wyjatku oblewa recenzenta."""
    monkeypatch.setattr(modul_kroku, "WYJATKI_MIGAWKI", [KATALOG_SKILLI_RUNU])

    zdarzenia, _ = await _run_z_rownoleglym_recenzentem(tmp_path)

    (porazka,) = _kroki_o_rodzaju(zdarzenia, "step_failed", "recenzja")
    assert porazka["text"] == (
        NARUSZENIE + "nowe: " + _sciezka_raportu("implementuj", 1).as_posix())
    assert _kroki_o_rodzaju(zdarzenia, "step_finished", "recenzja") == []
