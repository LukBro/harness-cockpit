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
from runner.agent import SpecKroku
from runner.krok import WynikKroku, wykonaj_krok

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

    assert wynik == WynikKroku("zakonczony", None, 0)


async def test_krok_bez_deklarowanych_artefaktow_i_z_kodem_zero_jest_zakonczony(run):
    wynik, _ = await uruchom(spec(), atrapa(), run)

    assert wynik == WynikKroku("zakonczony", None, 0)


async def test_brak_drugiego_artefaktu_jest_nazwany_w_powodzie(run):
    wynik, _ = await uruchom(
        spec(wyjscia=["a.md", "b.md"]), atrapa(zapisz={"a.md": "jest"}), run
    )

    assert wynik == WynikKroku("nieudany", "brak artefaktu b.md", 0)


async def test_brak_pierwszego_artefaktu_wygrywa_z_pustym_drugim(run):
    wynik, _ = await uruchom(
        spec(wyjscia=["a.md", "b.md"]), atrapa(zapisz={"b.md": ""}), run
    )

    assert wynik == WynikKroku("nieudany", "brak artefaktu a.md", 0)


async def test_pusty_pierwszy_artefakt_wygrywa_z_brakiem_drugiego(run):
    wynik, _ = await uruchom(
        spec(wyjscia=["a.md", "b.md"]), atrapa(zapisz={"a.md": ""}), run
    )

    assert wynik == WynikKroku(
        "nieudany", f"niezmiennik evidence_required: plik dowodu jest pusty: {run / 'a.md'}", 0
    )


async def test_pusty_artefakt_to_brak_dowodu_a_nie_zielone_przejscie(run):
    wynik, _ = await uruchom(spec(wyjscia=["werdykt.md"]), atrapa(zapisz={"werdykt.md": ""}), run)

    assert wynik == WynikKroku(
        "nieudany",
        f"niezmiennik evidence_required: plik dowodu jest pusty: {run / 'werdykt.md'}",
        0,
    )
    assert (run / "werdykt.md").is_file()


async def test_artefakt_z_samych_bialych_znakow_to_brak_dowodu(run):
    wynik, _ = await uruchom(
        spec(wyjscia=["werdykt.md"]), atrapa(zapisz={"werdykt.md": " \n\t \n"}), run
    )

    assert wynik == WynikKroku(
        "nieudany",
        f"niezmiennik evidence_required: plik dowodu jest pusty: {run / 'werdykt.md'}",
        0,
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

    assert wynik == WynikKroku("nieudany", "brak artefaktu sub/a.md", 0)
    assert (katalog / "a.md").read_text() == "tresc"


@pytest.mark.parametrize("kod", [3, 7])
async def test_kod_wyjscia_rozny_od_zera_jest_w_powodzie_porazki(run, kod):
    wynik, _ = await uruchom(spec(), atrapa(kod=kod), run)

    assert wynik == WynikKroku("nieudany", f"kod wyjścia {kod}", kod)


async def test_kod_wyjscia_ma_pierwszenstwo_przed_brakiem_artefaktow(run):
    wynik, _ = await uruchom(spec(wyjscia=["a.md"]), atrapa(kod=3), run)

    assert wynik == WynikKroku("nieudany", "kod wyjścia 3", 3)


# --- reviewer_readonly -------------------------------------------------------


async def test_recenzent_tworzacy_nowy_plik_poza_katalogiem_runu_jest_nieudany(run):
    wynik, _ = await uruchom(
        spec(Rola.reviewer), atrapa(zapisz={"nowy.txt": "samowola"}), run
    )

    assert wynik == WynikKroku("nieudany", NARUSZENIE + "nowe: nowy.txt", 0)
    assert (run / "nowy.txt").read_text() == "samowola"


async def test_recenzent_zmieniajacy_istniejacy_plik_jest_nieudany(run):
    (run / "istniejacy.txt").write_text("stare")

    wynik, _ = await uruchom(
        spec(Rola.reviewer), atrapa(zapisz={"istniejacy.txt": "nowe"}), run
    )

    assert wynik == WynikKroku("nieudany", NARUSZENIE + "zmienione: istniejacy.txt", 0)
    assert (run / "istniejacy.txt").read_text() == "nowe"


async def test_recenzent_usuwajacy_istniejacy_plik_jest_nieudany(run):
    (run / "istniejacy.txt").write_text("stare")

    wynik, _ = await uruchom(spec(Rola.reviewer), atrapa(usun=["istniejacy.txt"]), run)

    assert wynik == WynikKroku("nieudany", NARUSZENIE + "usuniete: istniejacy.txt", 0)
    assert not (run / "istniejacy.txt").exists()


async def test_recenzent_piszacy_tylko_do_katalogu_runu_jest_zakonczony(run):
    (run / "istniejacy.txt").write_text("stare")

    wynik, _ = await uruchom(
        spec(Rola.reviewer, wyjscia=[".cockpit/_run/x.md"]),
        atrapa(zapisz={".cockpit/_run/x.md": "werdykt: ok"}),
        run,
    )

    assert wynik == WynikKroku("zakonczony", None, 0)
    assert (run / ".cockpit" / "_run" / "x.md").read_text() == "werdykt: ok"


async def test_pliki_runu_obecne_przed_krokiem_recenzenta_nie_sa_naruszeniem(run):
    skill = run / ".cockpit" / "_run" / "skills" / "recenzja" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("skill skopiowany przez runner przed krokiem")

    wynik, _ = await uruchom(
        spec(Rola.reviewer, wyjscia=[".cockpit/_run/x.md"]),
        atrapa(zapisz={".cockpit/_run/x.md": "werdykt: ok"}),
        run,
    )

    assert wynik == WynikKroku("zakonczony", None, 0)
    assert skill.read_text() == "skill skopiowany przez runner przed krokiem"
    assert (run / ".cockpit" / "_run" / "x.md").read_text() == "werdykt: ok"


async def test_wyjatek_katalogu_runu_nie_chroni_zapisu_poza_nim(run):
    wynik, _ = await uruchom(
        spec(Rola.reviewer, wyjscia=[".cockpit/_run/x.md"]),
        atrapa(zapisz={".cockpit/_run/x.md": "werdykt", "nowy.txt": "poza"}),
        run,
    )

    assert wynik == WynikKroku("nieudany", NARUSZENIE + "nowe: nowy.txt", 0)


async def test_zapis_obok_katalogu_runu_w_cockpit_jest_naruszeniem(run):
    wynik, _ = await uruchom(
        spec(Rola.reviewer, wyjscia=[".cockpit/_run/x.md"]),
        atrapa(zapisz={".cockpit/_run/x.md": "werdykt", ".cockpit/inny.md": "poza"}),
        run,
    )

    assert wynik == WynikKroku("nieudany", NARUSZENIE + "nowe: .cockpit/inny.md", 0)


async def test_zbieracz_piszacy_poza_katalogiem_runu_jest_nieudany(run):
    wynik, _ = await uruchom(
        spec(Rola.gatherer), atrapa(zapisz={"nowy.txt": "samowola"}), run
    )

    assert wynik == WynikKroku("nieudany", NARUSZENIE + "nowe: nowy.txt", 0)
    assert (run / "nowy.txt").is_file()


async def test_autor_piszacy_poza_katalogiem_runu_jest_zakonczony(run):
    wynik, _ = await uruchom(spec(Rola.author), atrapa(zapisz={"nowy.txt": "praca"}), run)

    assert wynik == WynikKroku("zakonczony", None, 0)
    assert (run / "nowy.txt").read_text() == "praca"


async def test_wykonawca_zmieniajacy_istniejacy_plik_jest_zakonczony(run):
    (run / "istniejacy.txt").write_text("stare")

    wynik, _ = await uruchom(
        spec(Rola.executor), atrapa(zapisz={"istniejacy.txt": "nowe"}), run
    )

    assert wynik == WynikKroku("zakonczony", None, 0)
    assert (run / "istniejacy.txt").read_text() == "nowe"


async def test_recenzent_z_kodem_bledu_dostaje_powod_z_kodem_a_nie_z_naruszenia(run):
    wynik, _ = await uruchom(
        spec(Rola.reviewer), atrapa(zapisz={"nowy.txt": "poza"}, kod=3), run
    )

    assert wynik == WynikKroku("nieudany", "kod wyjścia 3", 3)


async def test_brak_artefaktu_recenzenta_ma_pierwszenstwo_przed_naruszeniem(run):
    wynik, _ = await uruchom(
        spec(Rola.reviewer, wyjscia=[".cockpit/_run/x.md"]),
        atrapa(zapisz={"nowy.txt": "poza"}),
        run,
    )

    assert wynik == WynikKroku("nieudany", "brak artefaktu .cockpit/_run/x.md", 0)


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

    assert wynik == WynikKroku("zakonczony", None, 0)
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

    assert wynik == WynikKroku("zakonczony", None, 0)
    assert zdarzenia == [zdarzenie_wyjscia("�")]


async def test_wyjscie_nieudanego_kroku_tez_trafia_do_zdarzen(run):
    wynik, zdarzenia = await uruchom(spec(), atrapa(linie=[["out", "przed bledem\n"]], kod=3), run)

    assert wynik == WynikKroku("nieudany", "kod wyjścia 3", 3)
    assert zdarzenia == [zdarzenie_wyjscia("przed bledem")]


async def test_linia_dluzsza_niz_64_kib_nie_wywraca_kroku_i_jest_wyemitowana_w_calosci(run):
    dlugosc = 70_000

    wynik, zdarzenia = await uruchom(spec(), atrapa(dluga=dlugosc), run)

    assert wynik == WynikKroku("zakonczony", None, 0)
    assert all(z["kind"] == "output" for z in zdarzenia)
    assert "".join(z["text"] for z in zdarzenia) == "x" * dlugosc


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

    assert wynik == WynikKroku("zakonczony", None, 0)


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
