"""Bramki lokalne rownolegle (HAR-236): `tools/bramki_lokalne.sh`.

Skrypt uruchamia JEDNOCZESNIE pelne testy i mutanty zmienionych funkcji, zbiera ich logi
w jednym katalogu i konczy sie jednym kodem z obu wynikow. Testy uruchamiaja PRAWDZIWY skrypt
(jawnie przez `/bin/bash`, jak `test_testy_pelne.py`: na macOS to bash 3.2) z falszywymi
programami: skrypt testow (`COCKPIT_BRAMKI_TESTY`) i `python` (`PYTHON`), ktore zapisuja
argumenty, srodowisko i katalog roboczy oraz koncza sie zadanym kodem. Bez Dockera, Postgresa
i mutmuta. Procesy: `tests/procesy.py`, cwd = korzen repo, kazde czekanie ma limit czasu.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from procesy import (
    LimitCzasuTestu,
    czy_zyje,
    uruchom_proces_sync,
    zabij_proces,
    zakoncz_proces_sync,
)

KORZEN = Path(__file__).resolve().parent.parent
SKRYPT = KORZEN / "tools" / "bramki_lokalne.sh"
# Zabezpieczenia przed zawieszeniem (nie czasy, ktorych test oczekuje): start skryptu to fork/exec
# kilku powlok, ktore pod obciazeniem rownoleglych sesji trwaja dziesiatki sekund.
LIMIT_PROCESU = 90
LIMIT_ZDARZENIA = 90

# Wspolna funkcja obu falszywych programow: czeka na plik do 900 prob (>= 90 s), potem zwraca 1.
CZEKAJ_NA = """czekaj_na() {
  i=0
  while [ ! -e "$1" ]; do
    sleep 0.1
    i=$((i + 1))
    [ "$i" -le 900 ] || return 1
  done
}
"""

FALSZYWE_TESTY = (
    """#!/bin/sh
"""
    + CZEKAJ_NA
    + """pwd -P > "$FALSZYWY_KAT/testy.pwd"
env > "$FALSZYWY_KAT/testy.env"
echo "$$" > "$FALSZYWY_KAT/testy.pid"
if [ -n "${FALSZYWY_CZYTAJ_STDIN:-}" ]; then cat > "$FALSZYWY_KAT/testy.stdin"; fi
echo "testy-stdout"
echo "testy-stderr" >&2
case "${FALSZYWY_TRYB:-}" in
  rownolegle)
    : > "$FALSZYWY_KAT/testy_start"
    czekaj_na "$FALSZYWY_KAT/mutanty_start" || exit 97 ;;
  wstrzymaj)
    : > "$FALSZYWY_KAT/testy_start"
    czekaj_na "$FALSZYWY_KAT/puszczaj" || exit 97 ;;
  zawies)
    # Obsluga TERM bez zadnego forka przed dowodem (pod obciazeniem fork trwa dziesiatki sekund,
    # a skrypt dobija grupy KILL po 10 s): dowod `.term_start`, potem - gdy test utworzyl FIFO
    # `.zwolnij` - czekanie na zwolnienie przez test, na koniec dowod `.term`.
    trap ': > "$FALSZYWY_KAT/testy.term_start"; [ ! -p "$FALSZYWY_KAT/testy.zwolnij" ] || read zwolnienie < "$FALSZYWY_KAT/testy.zwolnij"; : > "$FALSZYWY_KAT/testy.term"; exit 0' TERM
    sleep 300 &
    echo "$!" > "$FALSZYWY_KAT/testy.wnuk"
    wait
    # Wnuk ginie od tego samego SIGTERM, a SIGTERM samej powloki moze dojsc po jego SIGCHLD: bez
    # tej petli powloka konczylaby sie sama, zanim obsluzy sygnal, i nie zapisalaby dowodu.
    czekaj_na "$FALSZYWY_KAT/nigdy_nie_powstanie" || exit 99 ;;
  uparty)
    trap ': > "$FALSZYWY_KAT/testy.term"' TERM
    : > "$FALSZYWY_KAT/testy.gotowy"
    # `wait` na tle przerywa sie od razu na sygnale (bash odklada obsluge TERM do konca
    # pierwszoplanowego `sleep 1`, a to pod obciazeniem trwa i zblizalo test do 10 s straznika).
    while :; do sleep 300 & wait $!; done ;;
  oporny_wnuk)
    # Gdy test utworzyl FIFO `.zwolnij`, obsluga TERM czeka na jego zwolnienie (bez forka; dowod
    # `.term_start`): lider zyje, a skrypt stoi w `wait`, wiec test widzi straznika dobijania.
    trap ': > "$FALSZYWY_KAT/testy.term_start"; [ ! -p "$FALSZYWY_KAT/testy.zwolnij" ] || read zwolnienie < "$FALSZYWY_KAT/testy.zwolnij"; exit 0' TERM
    ( trap '' TERM; exec sleep 300 ) &
    echo "$!" > "$FALSZYWY_KAT/testy.wnuk"
    wait ;;
esac
exit "${FALSZYWY_KOD_TESTOW:-0}"
"""
)

# Zapisuje KAZDE wywolanie (argument po argumencie, wywolania rozdzielone `---`); zachowuje sie
# jak mutanty_zmian.py tylko dla wywolania skryptu `tools/mutanty_zmian.py`.
FALSZYWY_PYTHON = (
    """#!/bin/sh
{ for a in "$@"; do printf '%s\\n' "$a"; done; echo '---'; } >> "$FALSZYWY_KAT/python.argv"
case "$1" in
  *tools/mutanty_zmian.py) ;;
  *) exit 0 ;;
esac
"""
    + CZEKAJ_NA
    + """pwd -P > "$FALSZYWY_KAT/mutanty.pwd"
env > "$FALSZYWY_KAT/mutanty.env"
echo "$$" > "$FALSZYWY_KAT/mutanty.pid"
if [ -n "${FALSZYWY_CZYTAJ_STDIN:-}" ]; then cat > "$FALSZYWY_KAT/mutanty.stdin"; fi
echo "mutanty-stdout"
echo "mutanty-stderr" >&2
case "${FALSZYWY_TRYB:-}" in
  rownolegle)
    : > "$FALSZYWY_KAT/mutanty_start"
    czekaj_na "$FALSZYWY_KAT/testy_start" || exit 97 ;;
  wstrzymaj)
    : > "$FALSZYWY_KAT/mutanty_start"
    czekaj_na "$FALSZYWY_KAT/puszczaj" || exit 97 ;;
  zawies)
    # Obsluga TERM bez zadnego forka przed dowodem (pod obciazeniem fork trwa dziesiatki sekund,
    # a skrypt dobija grupy KILL po 10 s): dowod `.term_start`, potem - gdy test utworzyl FIFO
    # `.zwolnij` - czekanie na zwolnienie przez test, na koniec dowod `.term`.
    trap ': > "$FALSZYWY_KAT/mutanty.term_start"; [ ! -p "$FALSZYWY_KAT/mutanty.zwolnij" ] || read zwolnienie < "$FALSZYWY_KAT/mutanty.zwolnij"; : > "$FALSZYWY_KAT/mutanty.term"; exit 0' TERM
    sleep 300 &
    echo "$!" > "$FALSZYWY_KAT/mutanty.wnuk"
    wait
    # Wnuk ginie od tego samego SIGTERM, a SIGTERM samej powloki moze dojsc po jego SIGCHLD: bez
    # tej petli powloka konczylaby sie sama, zanim obsluzy sygnal, i nie zapisalaby dowodu.
    czekaj_na "$FALSZYWY_KAT/nigdy_nie_powstanie" || exit 99 ;;
  uparty)
    trap ': > "$FALSZYWY_KAT/mutanty.term"' TERM
    : > "$FALSZYWY_KAT/mutanty.gotowy"
    # `wait` na tle przerywa sie od razu na sygnale (bash odklada obsluge TERM do konca
    # pierwszoplanowego `sleep 1`, a to pod obciazeniem trwa i zblizalo test do 10 s straznika).
    while :; do sleep 300 & wait $!; done ;;
  oporny_wnuk)
    trap ': > "$FALSZYWY_KAT/mutanty.term_start"; [ ! -p "$FALSZYWY_KAT/mutanty.zwolnij" ] || read zwolnienie < "$FALSZYWY_KAT/mutanty.zwolnij"; exit 0' TERM
    ( trap '' TERM; exec sleep 300 ) &
    echo "$!" > "$FALSZYWY_KAT/mutanty.wnuk"
    wait ;;
esac
exit "${FALSZYWY_KOD_MUTANTOW:-0}"
"""
)

# Python z domyslnymi dyspozycjami sygnalow (SIGINT bywa odziedziczony jako ignorowany po
# zadaniu w tle) i exec basha: skrypt dostaje sygnaly tak jak z terminala.
START_Z_DOMYSLNYMI_SYGNALAMI = (
    "import os, signal, sys; "
    "signal.signal(signal.SIGINT, signal.SIG_DFL); signal.signal(signal.SIGTERM, signal.SIG_DFL); "
    "os.execv('/bin/bash', ['/bin/bash', *sys.argv[1:]])"
)


@dataclass
class Wynik:
    kod: int
    stdout: str
    stderr: str

    @property
    def calosc(self) -> str:
        return self.stdout + self.stderr


def _zapisz_program(sciezka: Path, tresc: str) -> None:
    sciezka.parent.mkdir(parents=True, exist_ok=True)
    sciezka.write_text(tresc)
    sciezka.chmod(0o755)


def _srodowisko(tmp_path: Path, **dodatkowe: str | None) -> dict[str, str]:
    """Srodowisko skryptu: falszywe programy, wlasny katalog logow i TMPDIR w `tmp_path`, bez
    cudzych zmiennych COCKPIT_*. Wartosc `None` usuwa zmienna."""
    falszywe = tmp_path / "falszywe"
    _zapisz_program(falszywe / "testy.sh", FALSZYWE_TESTY)
    _zapisz_program(falszywe / "python.sh", FALSZYWY_PYTHON)
    (tmp_path / "tmp").mkdir(exist_ok=True)
    env = {
        k: v for k, v in os.environ.items() if not k.startswith("COCKPIT_") and k != "PYTHON"
    }
    env["TMPDIR"] = str(tmp_path / "tmp")
    env["PYTHON"] = str(falszywe / "python.sh")
    env["COCKPIT_BRAMKI_TESTY"] = str(falszywe / "testy.sh")
    env["COCKPIT_BRAMKI_KATALOG"] = str(_kat(tmp_path))
    env["FALSZYWY_KAT"] = str(falszywe)
    for nazwa, wartosc in dodatkowe.items():
        if wartosc is None:
            env.pop(nazwa, None)
        else:
            env[nazwa] = wartosc
    return env


def _kat(tmp_path: Path) -> Path:
    """Katalog logow bramek (`COCKPIT_BRAMKI_KATALOG`)."""
    return tmp_path / "kat"


def _falszywe(tmp_path: Path) -> Path:
    """Katalog, do ktorego falszywe programy zapisuja dowody swojego uruchomienia."""
    return tmp_path / "falszywe"


def _start(
    env: dict[str, str], *argv: str, cwd: Path = KORZEN, skrypt: Path = SKRYPT
) -> subprocess.Popen:
    return uruchom_proces_sync(
        ["/bin/bash", str(skrypt), *argv],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _koniec(proc: subprocess.Popen) -> Wynik:
    try:
        out, err = proc.communicate(timeout=LIMIT_PROCESU)
        return Wynik(proc.returncode, out, err)
    finally:
        zakoncz_proces_sync(proc, nazwa="bramki_lokalne.sh")


def _uruchom(env: dict[str, str], *argv: str, **kw) -> Wynik:
    return _koniec(_start(env, *argv, **kw))


def _czekaj_na(warunek: Callable[[], object], nazwa: str) -> None:
    koniec = time.monotonic() + LIMIT_ZDARZENIA
    while not warunek():
        if time.monotonic() >= koniec:
            raise LimitCzasuTestu(f"{nazwa}: przekroczono limit {LIMIT_ZDARZENIA} s")
        time.sleep(0.05)


def _podsumowanie(kat: Path, testy: int, mutanty: int, bramki: int) -> list[str]:
    return [
        f"testy: kod {testy}, log {kat}/testy.log",
        f"mutanty: kod {mutanty}, stdout {kat}/mutanty.stdout, stderr {kat}/mutanty.stderr",
        f"bramki: kod {bramki}",
    ]


def _wywolania_pythona(tmp_path: Path) -> list[list[str]]:
    """Wywolania falszywego pythona (lista argumentow, bez nazwy programu), w kolejnosci zapisu."""
    plik = _falszywe(tmp_path) / "python.argv"
    if not plik.exists():
        return []
    wywolania: list[list[str]] = [[]]
    for linia in plik.read_text().splitlines():
        if linia == "---":
            wywolania.append([])
        else:
            wywolania[-1].append(linia)
    return [w for w in wywolania if w]


def _wywolanie_mutantow(tmp_path: Path) -> list[str]:
    wywolania = [w for w in _wywolania_pythona(tmp_path) if w[0].endswith("tools/mutanty_zmian.py")]
    assert len(wywolania) == 1, wywolania
    return wywolania[0]


def _zmienne(plik: Path) -> dict[str, str]:
    """Zmienne z zapisu `env` falszywego programu (wiersze `NAZWA=wartosc`)."""
    wynik: dict[str, str] = {}
    for linia in plik.read_text(errors="replace").splitlines():
        nazwa, rowna, wartosc = linia.partition("=")
        if rowna and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", nazwa):
            wynik[nazwa] = wartosc
    return wynik


# --- kod wyjscia: z kodu testow i kodu mutantow ---------------------------------------------


def test_oba_wyniki_zielone_daja_kod_0_i_trzy_linie_podsumowania_na_koncu_stdout(tmp_path):
    wynik = _uruchom(_srodowisko(tmp_path))

    assert wynik.kod == 0, wynik.calosc
    assert wynik.stdout.splitlines()[-3:] == _podsumowanie(_kat(tmp_path), 0, 0, 0)


def test_padniete_same_testy_daja_kod_niezerowy_gdy_mutanty_sa_czyste(tmp_path):
    wynik = _uruchom(_srodowisko(tmp_path, FALSZYWY_KOD_TESTOW="1", FALSZYWY_KOD_MUTANTOW="0"))

    assert wynik.kod == 1, wynik.calosc
    assert wynik.stdout.splitlines()[-3:] == _podsumowanie(_kat(tmp_path), 1, 0, 1)


def test_przezywajace_mutanty_daja_kod_1_gdy_testy_sa_zielone(tmp_path):
    wynik = _uruchom(_srodowisko(tmp_path, FALSZYWY_KOD_TESTOW="0", FALSZYWY_KOD_MUTANTOW="1"))

    assert wynik.kod == 1, wynik.calosc
    assert wynik.stdout.splitlines()[-3:] == _podsumowanie(_kat(tmp_path), 0, 1, 1)


@pytest.mark.parametrize(
    ("kod_testow", "kod_mutantow", "oczekiwany"),
    [
        pytest.param(0, 3, 3, id="zmiany_poza_zasiegiem_mutmuta_to_kod_3"),
        pytest.param(0, 2, 2, id="blad_narzedzia_mutantow_to_kod_2"),
        pytest.param(0, 4, 2, id="nieznany_kod_mutantow_to_kod_2"),
        pytest.param(0, 127, 2, id="brak_programu_mutantow_to_kod_2"),
        pytest.param(1, 3, 1, id="testy_czerwone_wygrywaja_z_kodem_3_mutantow"),
        pytest.param(1, 2, 2, id="blad_narzedzia_mutantow_wygrywa_z_czerwonymi_testami"),
        pytest.param(7, 0, 1, id="kazdy_niezerowy_kod_testow_to_kod_1"),
        pytest.param(1, 1, 1, id="testy_i_mutanty_czerwone_to_kod_1"),
    ],
)
def test_kod_bramek_wynika_z_kodow_testow_i_mutantow(
    tmp_path, kod_testow, kod_mutantow, oczekiwany
):
    env = _srodowisko(
        tmp_path, FALSZYWY_KOD_TESTOW=str(kod_testow), FALSZYWY_KOD_MUTANTOW=str(kod_mutantow)
    )

    wynik = _uruchom(env)

    assert wynik.kod == oczekiwany, wynik.calosc
    assert wynik.stdout.splitlines()[-3:] == _podsumowanie(
        _kat(tmp_path), kod_testow, kod_mutantow, oczekiwany
    )


# --- rownolegle uruchomienie obu bramek ---------------------------------------------------------


def test_testy_i_mutanty_biegna_rownolegle_a_nie_jedne_po_drugich(tmp_path):
    # Kazdy falszywy program czeka na znacznik startu drugiego (do ~20 s). Przy uruchomieniu
    # szeregowym pierwszy z nich nie doczeka sie drugiego i konczy sie kodem 97.
    wynik = _uruchom(_srodowisko(tmp_path, FALSZYWY_TRYB="rownolegle"))

    assert wynik.kod == 0, wynik.calosc
    assert wynik.stdout.splitlines()[-3:] == _podsumowanie(_kat(tmp_path), 0, 0, 0)
    assert (_falszywe(tmp_path) / "testy_start").exists()
    assert (_falszywe(tmp_path) / "mutanty_start").exists()


def test_sciezki_logow_sa_na_stdout_zanim_bramki_skoncza_prace(tmp_path):
    env = _srodowisko(tmp_path, FALSZYWY_TRYB="wstrzymaj")
    wyjscie = tmp_path / "stdout.txt"
    with wyjscie.open("w") as plik:
        proc = uruchom_proces_sync(
            ["/bin/bash", str(SKRYPT)], cwd=KORZEN, env=env, stdout=plik, stderr=subprocess.PIPE
        )
        try:
            kat = _kat(tmp_path)
            # Oba falszywe programy stoja i czekaja na `puszczaj`: bramki jeszcze trwaja.
            _czekaj_na(
                lambda: (_falszywe(tmp_path) / "testy_start").exists()
                and (_falszywe(tmp_path) / "mutanty_start").exists(),
                "start obu bramek",
            )
            _czekaj_na(
                lambda: all(
                    s in wyjscie.read_text()
                    for s in (f"{kat}/testy.log", f"{kat}/mutanty.stdout", f"{kat}/mutanty.stderr")
                ),
                "sciezki logow na stdout",
            )
            assert "bramki: kod" not in wyjscie.read_text()
            (_falszywe(tmp_path) / "puszczaj").write_text("")
            proc.communicate(timeout=LIMIT_PROCESU)
        finally:
            zakoncz_proces_sync(proc, nazwa="bramki_lokalne.sh")
    assert proc.returncode == 0
    assert wyjscie.read_text().splitlines()[-3:] == _podsumowanie(kat, 0, 0, 0)


# --- logi -------------------------------------------------------------------------------------------


def test_logi_testow_zbieraja_stdout_i_stderr_a_logi_mutantow_trzymaja_je_osobno(tmp_path):
    wynik = _uruchom(_srodowisko(tmp_path))

    assert wynik.kod == 0, wynik.calosc
    kat = _kat(tmp_path)
    log_testow = (kat / "testy.log").read_text()
    assert "testy-stdout\n" in log_testow
    assert "testy-stderr\n" in log_testow
    assert (kat / "mutanty.stdout").read_text() == "mutanty-stdout\n"
    assert (kat / "mutanty.stderr").read_text() == "mutanty-stderr\n"


def test_bez_katalogu_logow_w_zmiennej_skrypt_tworzy_katalog_tymczasowy_i_podaje_sciezki(
    tmp_path,
):
    wynik = _uruchom(_srodowisko(tmp_path, COCKPIT_BRAMKI_KATALOG=None))

    assert wynik.kod == 0, wynik.calosc
    log = re.fullmatch(r"testy: kod 0, log (.+)", wynik.stdout.splitlines()[-3])
    assert log is not None, wynik.stdout
    log_testow = Path(log.group(1))
    assert log_testow.name == "testy.log"
    assert log_testow.parent.parent == tmp_path / "tmp"  # mktemp -d pod TMPDIR
    assert "testy-stdout" in log_testow.read_text()
    assert wynik.stdout.splitlines()[-2] == (
        f"mutanty: kod 0, stdout {log_testow.parent}/mutanty.stdout, "
        f"stderr {log_testow.parent}/mutanty.stderr"
    )


def test_nieistniejacy_katalog_logow_jest_tworzony(tmp_path):
    kat = tmp_path / "jeszcze-nie-ma"
    assert not kat.exists()

    wynik = _uruchom(_srodowisko(tmp_path, COCKPIT_BRAMKI_KATALOG=str(kat)))

    assert wynik.kod == 0, wynik.calosc
    assert (kat / "testy.log").is_file()
    assert wynik.stdout.splitlines()[-3:] == _podsumowanie(kat, 0, 0, 0)


def test_katalog_logow_ktorego_nie_da_sie_utworzyc_to_kod_2_i_nic_nie_startuje(tmp_path):
    plik = tmp_path / "zwykly-plik"
    plik.write_text("to nie jest katalog")

    wynik = _uruchom(_srodowisko(tmp_path, COCKPIT_BRAMKI_KATALOG=str(plik / "kat")))

    assert wynik.kod == 2, wynik.calosc
    assert wynik.calosc.strip() != ""
    assert "Traceback" not in wynik.calosc
    assert not (_falszywe(tmp_path) / "testy.pid").exists()
    assert _wywolania_pythona(tmp_path) == []


# --- srodowisko i argumenty mutantow ------------------------------------------------------------


def test_mutanty_dzialaja_bez_adresu_bazy_i_wymogu_bazy_a_testy_je_dostaja(tmp_path):
    adres = "postgresql+asyncpg://u:p@localhost:5432/baza_rodzica"
    env = _srodowisko(
        tmp_path, COCKPIT_TEST_DATABASE_URL=adres, COCKPIT_WYMAGAJ_BAZY="1"
    )

    wynik = _uruchom(env)

    assert wynik.kod == 0, wynik.calosc
    zmienne_mutantow = _zmienne(_falszywe(tmp_path) / "mutanty.env")
    zmienne_testow = _zmienne(_falszywe(tmp_path) / "testy.env")
    assert "COCKPIT_TEST_DATABASE_URL" not in zmienne_mutantow
    assert "COCKPIT_WYMAGAJ_BAZY" not in zmienne_mutantow
    assert zmienne_mutantow["FALSZYWY_KAT"] == str(_falszywe(tmp_path))  # reszta srodowiska zostaje
    assert zmienne_testow["COCKPIT_TEST_DATABASE_URL"] == adres
    assert zmienne_testow["COCKPIT_WYMAGAJ_BAZY"] == "1"


def test_argumenty_skryptu_trafiaja_do_mutanty_zmian_po_max_children_bez_zmiany(tmp_path):
    wynik = _uruchom(
        _srodowisko(tmp_path), "--baza", "origin/develop", "--korzen", "katalog ze spacja"
    )

    assert wynik.kod == 0, wynik.calosc
    wywolanie = _wywolanie_mutantow(tmp_path)
    assert wywolanie[1:] == [
        "--max-children",
        "8",
        "--baza",
        "origin/develop",
        "--korzen",
        "katalog ze spacja",
    ]


@pytest.mark.parametrize(
    ("zmienna", "oczekiwane"),
    [
        pytest.param(None, "8", id="domyslnie_8"),
        pytest.param("3", "3", id="ze_zmiennej_3"),
        pytest.param("1", "1", id="najmniejsza_dozwolona_1"),
        pytest.param("64", "64", id="duza_liczba_64"),
    ],
)
def test_liczba_dzieci_mutmuta_to_osiem_albo_wartosc_ze_zmiennej(tmp_path, zmienna, oczekiwane):
    wynik = _uruchom(_srodowisko(tmp_path, COCKPIT_MUTMUT_DZIECI=zmienna))

    assert wynik.kod == 0, wynik.calosc
    assert _wywolanie_mutantow(tmp_path)[1:3] == ["--max-children", oczekiwane]


@pytest.mark.parametrize("wartosc", ["0", "abc", "-2", "1.5", "8x"])
def test_zla_liczba_dzieci_mutmuta_to_kod_2_z_komunikatem_i_nic_nie_startuje(tmp_path, wartosc):
    wynik = _uruchom(_srodowisko(tmp_path, COCKPIT_MUTMUT_DZIECI=wartosc))

    assert wynik.kod == 2, wynik.calosc
    assert "COCKPIT_MUTMUT_DZIECI" in wynik.calosc
    assert "Traceback" not in wynik.calosc
    assert not (_falszywe(tmp_path) / "testy.pid").exists()
    assert _wywolania_pythona(tmp_path) == []


def test_skrypt_dziala_z_korzenia_repo_niezaleznie_od_biezacego_katalogu_i_domyslnie_wola_testy_pelne(
    tmp_path,
):
    korzen = (tmp_path / "inny-korzen").resolve()
    _zapisz_program(korzen / "tools" / "testy_pelne.sh", FALSZYWE_TESTY)
    shutil.copy(SKRYPT, korzen / "tools" / "bramki_lokalne.sh")
    inny_katalog = tmp_path / "gdzie-indziej"
    inny_katalog.mkdir()
    env = _srodowisko(tmp_path, COCKPIT_BRAMKI_TESTY=None)

    wynik = _uruchom(env, cwd=inny_katalog, skrypt=korzen / "tools" / "bramki_lokalne.sh")

    assert wynik.kod == 0, wynik.calosc
    assert (_falszywe(tmp_path) / "testy.pwd").read_text().strip() == str(korzen)
    assert (_falszywe(tmp_path) / "mutanty.pwd").read_text().strip() == str(korzen)
    assert _wywolanie_mutantow(tmp_path)[0] == "tools/mutanty_zmian.py"


# --- przerwanie: zero sierot -----------------------------------------------------------------------


def _zyje_i_nie_zombie(pid: int) -> bool:
    if not czy_zyje(pid):
        return False
    ps = uruchom_proces_sync(
        ["ps", "-o", "stat=", "-p", str(pid)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        stan, _ = ps.communicate(timeout=30)
    finally:
        zakoncz_proces_sync(ps, nazwa="ps")
    return stan.strip() != "" and not stan.strip().startswith("Z")


def _pid_z_pliku(plik: Path) -> int | None:
    try:
        tekst = plik.read_text().strip()
    except OSError:
        return None
    return int(tekst) if tekst.isdigit() else None


def _zwolnij_wstrzymana_obsluge(fifo: Path, lider: int) -> None:
    """Zwalnia obsluge TERM wstrzymana na FIFO: zapis konczy `read` w falszywym programie.
    Otwarcie do zapisu bez blokowania udaje sie dopiero, gdy czytnik juz otworzyl FIFO (ENXIO
    wczesniej); gdy lider nie zyje, nie ma komu zwalniac - brak dowodu `.term` oblewa test."""
    koniec = time.monotonic() + LIMIT_ZDARZENIA
    while time.monotonic() < koniec:
        try:
            fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            if not _zyje_i_nie_zombie(lider):
                return
            time.sleep(0.02)
            continue
        try:
            os.write(fd, b"zwolnij\n")
        finally:
            os.close(fd)
        return
    raise LimitCzasuTestu(f"zwolnienie obslugi sygnalu {fifo.name}: limit {LIMIT_ZDARZENIA} s")


@pytest.mark.parametrize(
    ("sygnal", "kod"),
    [
        pytest.param(signal.SIGTERM, 143, id="sigterm"),
        pytest.param(signal.SIGINT, 130, id="sigint"),
    ],
)
@pytest.mark.parametrize(
    "wstrzymane",
    [
        pytest.param((), id="obie_bramki_koncza_od_razu"),
        # Obsluga sygnalu jednej bramki wisi na FIFO, dopoki test jej nie zwolni, a dowod `.term`
        # powstaje dopiero po zwolnieniu: skrypt musi poczekac na OBIE, takze na te wstrzymana
        # (usuniecie `wait` jednej z nich zabilaby ja przed zapisem). Zdarzenia, nie czas:
        # obciazona maszyna nie zmienia wyniku (HAR-236, runda 3).
        pytest.param(("testy",), id="wolniejsze_testy"),
        pytest.param(("mutanty",), id="wolniejsze_mutanty"),
    ],
)
def test_przerwanie_bramek_zabija_oba_procesy_i_ich_dzieci_i_konczy_sie_kodem_sygnalu(
    tmp_path, sygnal, kod, wstrzymane
):
    env = _srodowisko(tmp_path, FALSZYWY_TRYB="zawies")
    falszywe = _falszywe(tmp_path)
    for nazwa in wstrzymane:
        os.mkfifo(falszywe / f"{nazwa}.zwolnij")
    proc = uruchom_proces_sync(
        [sys.executable, "-c", START_Z_DOMYSLNYMI_SYGNALAMI, str(SKRYPT)],
        cwd=KORZEN,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pliki_pid = ("testy.pid", "testy.wnuk", "mutanty.pid", "mutanty.wnuk")
    pidy: list[int] = []
    try:
        _czekaj_na(
            lambda: all(_pid_z_pliku(falszywe / p) for p in pliki_pid),
            "start procesow bramek i ich dzieci",
        )
        pidy = [_pid_z_pliku(falszywe / p) for p in pliki_pid]
        assert all(_zyje_i_nie_zombie(p) for p in pidy)
        # Sygnal tylko do samego skryptu (nie do jego grupy): potomkow musi zabic jego obsluga sygnalu.
        zabij_proces(proc.pid, sygnal)
        # Obie grupy dostaly SIGTERM; bramki niewstrzymane koncza same, wstrzymane czekaja na test.
        _czekaj_na(
            lambda: all((falszywe / f"{n}.term_start").exists() for n in ("testy", "mutanty")),
            "SIGTERM dla obu grup",
        )
        _czekaj_na(
            lambda: all(
                (falszywe / f"{n}.term").exists() for n in ("testy", "mutanty") if n not in wstrzymane
            ),
            "obsluga SIGTERM w niewstrzymanych bramkach",
        )
        for nazwa in wstrzymane:
            _zwolnij_wstrzymana_obsluge(
                falszywe / f"{nazwa}.zwolnij", pidy[0] if nazwa == "testy" else pidy[2]
            )
        proc.wait(timeout=LIMIT_PROCESU)
        assert proc.returncode == kod
        # Dowod, ze skrypt poczekal na obsluge sygnalu w OBU bramkach.
        assert (falszywe / "testy.term").exists()
        assert (falszywe / "mutanty.term").exists()
        _czekaj_na(lambda: not any(_zyje_i_nie_zombie(p) for p in pidy), "koniec potomkow")
    finally:
        for pid in pidy:
            if _zyje_i_nie_zombie(pid):
                zabij_proces(pid, signal.SIGKILL)
        zakoncz_proces_sync(proc, nazwa="bramki_lokalne.sh")
    assert not [p for p in pidy if _zyje_i_nie_zombie(p)], "po przerwaniu zostaly sieroty"


def _start_przerwania(
    tmp_path: Path, tryb: str, *, path_przed: Path | None = None
) -> subprocess.Popen:
    env = _srodowisko(tmp_path, FALSZYWY_TRYB=tryb)
    if path_przed is not None:
        env["PATH"] = f"{path_przed}:{env['PATH']}"
    return uruchom_proces_sync(
        [sys.executable, "-c", START_Z_DOMYSLNYMI_SYGNALAMI, str(SKRYPT)],
        cwd=KORZEN,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _dobij(pidy: list[int]) -> None:
    for pid in pidy:
        if _zyje_i_nie_zombie(pid):
            zabij_proces(pid, signal.SIGKILL)


@pytest.mark.parametrize(
    ("pierwszy", "drugi", "kod"),
    [
        pytest.param(signal.SIGTERM, signal.SIGINT, 143, id="term_potem_int"),
        pytest.param(signal.SIGINT, signal.SIGTERM, 130, id="int_potem_term"),
    ],
)
def test_drugi_sygnal_w_trakcie_sprzatania_nie_zmienia_kodu_wyjscia_ani_nie_skraca_czekania(
    tmp_path, pierwszy, drugi, kod
):
    # Bramki ignoruja SIGTERM (zapisuja dowod i dzialaja dalej): skrypt czeka na nie do KILL po
    # 10 s. W tym czasie dostaje drugi sygnal, ktory musi zostac zignorowany.
    proc = _start_przerwania(tmp_path, "uparty")
    falszywe = _falszywe(tmp_path)
    pidy: list[int] = []
    try:
        _czekaj_na(
            lambda: all(
                (falszywe / p).exists() for p in ("testy.gotowy", "mutanty.gotowy")
            )
            and _pid_z_pliku(falszywe / "testy.pid")
            and _pid_z_pliku(falszywe / "mutanty.pid"),
            "start obu bramek",
        )
        pidy = [_pid_z_pliku(falszywe / "testy.pid"), _pid_z_pliku(falszywe / "mutanty.pid")]
        zabij_proces(proc.pid, pierwszy)
        _czekaj_na(
            lambda: (falszywe / "testy.term").exists() and (falszywe / "mutanty.term").exists(),
            "SIGTERM dla obu grup",
        )
        assert proc.poll() is None  # bramki ignoruja SIGTERM, wiec skrypt jeszcze czeka
        zabij_proces(proc.pid, drugi)
        proc.wait(timeout=LIMIT_PROCESU)
        assert proc.returncode == kod
        _czekaj_na(lambda: not any(_zyje_i_nie_zombie(p) for p in pidy), "koniec bramek po KILL")
    finally:
        _dobij(pidy)
        zakoncz_proces_sync(proc, nazwa="bramki_lokalne.sh")


def test_potomek_ignorujacy_sigterm_jest_dobity_po_przerwaniu_a_nie_zostaje_sierota(tmp_path):
    proc = _start_przerwania(tmp_path, "oporny_wnuk")
    falszywe = _falszywe(tmp_path)
    pliki_pid = ("testy.pid", "testy.wnuk", "mutanty.pid", "mutanty.wnuk")
    pidy: list[int] = []
    try:
        _czekaj_na(
            lambda: all(_pid_z_pliku(falszywe / p) for p in pliki_pid), "start bramek i ich wnukow"
        )
        pidy = [_pid_z_pliku(falszywe / p) for p in pliki_pid]
        wnuki = [pidy[1], pidy[3]]
        assert all(_zyje_i_nie_zombie(p) for p in wnuki)
        zabij_proces(proc.pid, signal.SIGTERM)
        proc.wait(timeout=LIMIT_PROCESU)
        assert proc.returncode == 143
        # Liderzy koncza na SIGTERM, a wnuki go ignoruja: zabija je tylko KILL do calej grupy.
        _czekaj_na(lambda: not any(_zyje_i_nie_zombie(p) for p in wnuki), "koniec wnukow")
    finally:
        _dobij(pidy)
        zakoncz_proces_sync(proc, nazwa="bramki_lokalne.sh")


def _potomkowie(rodzic: int) -> list[int]:
    """PID-y zywych (nie zombie) dzieci procesu `rodzic` wg `ps -A`."""
    ps = uruchom_proces_sync(
        ["ps", "-A", "-o", "pid=,ppid=,stat="],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        tekst, _ = ps.communicate(timeout=30)
    finally:
        zakoncz_proces_sync(ps, nazwa="ps")
    wynik = []
    for linia in tekst.splitlines():
        pola = linia.split()
        if len(pola) == 3 and pola[1] == str(rodzic) and not pola[2].startswith("Z"):
            wynik.append(int(pola[0]))
    return wynik


def test_po_przerwaniu_wnuki_ignorujace_sigterm_i_straznik_dobijania_nie_zyja_juz_po_wyjsciu_skryptu(
    tmp_path,
):
    """Liderzy bramek stoja w obsludze SIGTERM (FIFO), wiec skrypt jest w `wait`, a jego straznik
    (`sleep 10` w swojej grupie) istnieje. Dopiero po zwolnieniu liderow skrypt wychodzi: do tego
    momentu wnuki ignorujace SIGTERM i straznik musza byc dobite przez sam skrypt, nie przez
    zegar straznika ani przez sprzatanie wolajacego. Falszywy `sleep 10` straznika trwa 300 s,
    wiec zegar straznika w tym tescie nigdy nie dobija wnukow ani sam sie nie konczy: skrypt, ktory
    nie zabija grupy straznika, wisi na `wait` (zdarzenie, nie czas). Sprawdzenie po wyjsciu ma 3 s
    zapasu tylko na asynchroniczne umieranie procesu po SIGKILL."""
    falszywe = _falszywe(tmp_path)
    bin_dir = tmp_path / "bin"
    _zapisz_program(
        bin_dir / "sleep",
        "#!/bin/sh\n"
        "dosc=/bin/sleep\n"
        '[ -x "$dosc" ] || dosc=/usr/bin/sleep\n'
        '[ "$1" != 10 ] || exec "$dosc" 300\n'
        'exec "$dosc" "$@"\n',
    )
    proc = _start_przerwania(  # tworzy tez katalog falszywych programow
        tmp_path, "oporny_wnuk", path_przed=bin_dir
    )
    os.mkfifo(falszywe / "testy.zwolnij")  # obsluga TERM sprawdza FIFO dopiero po sygnale
    pliki_pid = ("testy.pid", "testy.wnuk", "mutanty.pid", "mutanty.wnuk")
    pidy: list[int] = []
    try:
        _czekaj_na(
            lambda: all(_pid_z_pliku(falszywe / p) for p in pliki_pid), "start bramek i ich wnukow"
        )
        pidy = [_pid_z_pliku(falszywe / p) for p in pliki_pid]
        testy, _, mutanty, _ = pidy
        zabij_proces(proc.pid, signal.SIGTERM)
        # Straznik = jedyne zywe dziecko skryptu poza liderami bramek; jego dziecko = `sleep 10`.
        straznik: list[int] = []
        sleep_straznika: list[int] = []

        def straznik_i_jego_sleep() -> bool:
            straznik[:] = [p for p in _potomkowie(proc.pid) if p not in (testy, mutanty)]
            sleep_straznika[:] = _potomkowie(straznik[0]) if len(straznik) == 1 else []
            return len(straznik) == 1 and len(sleep_straznika) == 1

        _czekaj_na(straznik_i_jego_sleep, "straznik dobijania i jego sleep")
        potomkowie = [*pidy, straznik[0], sleep_straznika[0]]
        pidy = potomkowie  # takze straznik trafi do sprzatania w `finally`, gdy skrypt go nie zabije
        assert all(_zyje_i_nie_zombie(p) for p in (pidy[1], pidy[3], *straznik, *sleep_straznika))
        _zwolnij_wstrzymana_obsluge(falszywe / "testy.zwolnij", testy)
        proc.wait(timeout=LIMIT_PROCESU)
        assert proc.returncode == 143
        _czekaj_na_krotko(
            lambda: not any(_zyje_i_nie_zombie(p) for p in potomkowie),
            3,
        )
        zywe = [p for p in potomkowie if _zyje_i_nie_zombie(p)]
        assert zywe == [], f"po wyjsciu skryptu zyja jeszcze: {zywe}"
    finally:
        _dobij(pidy)
        zakoncz_proces_sync(proc, nazwa="bramki_lokalne.sh")


def _czekaj_na_krotko(warunek: Callable[[], object], sekundy: float) -> None:
    koniec = time.monotonic() + sekundy
    while not warunek() and time.monotonic() < koniec:
        time.sleep(0.05)


# --- standardowe wejscie, komunikaty bledow i katalog logow -------------------------------------


def test_bramki_nie_dziedzicza_standardowego_wejscia_skryptu(tmp_path):
    env = _srodowisko(tmp_path, FALSZYWY_CZYTAJ_STDIN="1")
    proc = uruchom_proces_sync(
        ["/bin/bash", str(SKRYPT)],
        cwd=KORZEN,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out, err = proc.communicate(input="dane-z-terminala\n", timeout=LIMIT_PROCESU)
    finally:
        zakoncz_proces_sync(proc, nazwa="bramki_lokalne.sh")

    assert proc.returncode == 0, out + err
    assert (_falszywe(tmp_path) / "testy.stdin").read_text() == ""
    assert (_falszywe(tmp_path) / "mutanty.stdin").read_text() == ""


def test_bez_zmiennej_python_mutanty_ida_przez_python_z_path(tmp_path):
    bin_dir = tmp_path / "bin"
    _zapisz_program(bin_dir / "python", FALSZYWY_PYTHON)
    env = _srodowisko(tmp_path, PYTHON=None)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    wynik = _uruchom(env)

    assert wynik.kod == 0, wynik.calosc
    assert _wywolanie_mutantow(tmp_path)[0] == "tools/mutanty_zmian.py"


@pytest.mark.parametrize("wartosc", ["0", "abc", "-2", "1.5", "8x"])
def test_zla_liczba_dzieci_mutmuta_to_na_stderr_dokladnie_jeden_komunikat_bez_bledu_powloki(
    tmp_path, wartosc
):
    wynik = _uruchom(_srodowisko(tmp_path, COCKPIT_MUTMUT_DZIECI=wartosc))

    assert wynik.kod == 2
    assert wynik.stdout == ""
    assert wynik.stderr == (
        f"BLAD: COCKPIT_MUTMUT_DZIECI musi byc liczba calkowita >= 1 (jest: '{wartosc}')\n"
    )


def test_katalog_logow_ktorego_nie_da_sie_utworzyc_ma_w_komunikacie_sciezke(tmp_path):
    plik = tmp_path / "zwykly-plik"
    plik.write_text("to nie jest katalog")

    wynik = _uruchom(_srodowisko(tmp_path, COCKPIT_BRAMKI_KATALOG=str(plik / "kat")))

    assert wynik.kod == 2, wynik.calosc
    assert wynik.stdout == ""
    assert f"BLAD: nie udalo sie utworzyc katalogu logow '{plik}/kat'\n" in wynik.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root zapisuje do katalogu bez prawa zapisu")
def test_istniejacy_katalog_logow_bez_prawa_zapisu_to_kod_2_i_nic_nie_startuje(tmp_path):
    kat = tmp_path / "tylko-do-odczytu"
    kat.mkdir()
    kat.chmod(0o555)
    try:
        wynik = _uruchom(_srodowisko(tmp_path, COCKPIT_BRAMKI_KATALOG=str(kat)))
    finally:
        kat.chmod(0o755)

    assert wynik.kod == 2, wynik.calosc
    assert wynik.stdout == ""
    assert wynik.stderr == f"BLAD: katalog logow '{kat}' nie jest katalogiem do zapisu\n"
    assert _wywolania_pythona(tmp_path) == []


def test_nieistniejacy_tmpdir_bez_katalogu_w_zmiennej_to_kod_2_z_komunikatem(tmp_path):
    brak = tmp_path / "nie-ma-tmpdir"

    wynik = _uruchom(_srodowisko(tmp_path, COCKPIT_BRAMKI_KATALOG=None, TMPDIR=str(brak)))

    assert wynik.kod == 2, wynik.calosc
    assert wynik.stdout == ""
    assert f"BLAD: nie udalo sie utworzyc katalogu tymczasowego w '{brak}'\n" in wynik.stderr
    assert _wywolania_pythona(tmp_path) == []


def test_nieudany_mktemp_bez_tmpdir_ma_w_komunikacie_katalog_tmp_a_nic_nie_startuje(tmp_path):
    bin_dir = tmp_path / "bin"
    _zapisz_program(bin_dir / "mktemp", "#!/bin/sh\nexit 1\n")
    env = _srodowisko(tmp_path, COCKPIT_BRAMKI_KATALOG=None, TMPDIR=None)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    wynik = _uruchom(env)

    assert wynik.kod == 2, wynik.calosc
    assert wynik.stdout == ""
    assert wynik.stderr == "BLAD: nie udalo sie utworzyc katalogu tymczasowego w '/tmp'\n"
    assert _wywolania_pythona(tmp_path) == []


def test_bez_tmpdir_i_bez_katalogu_w_zmiennej_katalog_logow_powstaje_w_tmp(tmp_path):
    wynik = _uruchom(_srodowisko(tmp_path, COCKPIT_BRAMKI_KATALOG=None, TMPDIR=None))

    log = re.fullmatch(r"testy: kod 0, log (.+)", wynik.stdout.splitlines()[-3])
    try:
        assert wynik.kod == 0, wynik.calosc
        assert log is not None, wynik.stdout
        katalog = Path(log.group(1)).parent
        assert re.fullmatch(r"/tmp/bramki_lokalne\.[A-Za-z0-9]{6}", str(katalog)), katalog
    finally:
        if log is not None:
            shutil.rmtree(Path(log.group(1)).parent, ignore_errors=True)


def test_wzgledny_katalog_logow_jest_liczony_od_biezacego_katalogu_a_nie_od_korzenia_repo(
    tmp_path,
):
    wywolanie = (tmp_path / "wywolanie").resolve()
    wywolanie.mkdir()

    wynik = _uruchom(
        _srodowisko(tmp_path, COCKPIT_BRAMKI_KATALOG="logi-wzgledne"), cwd=wywolanie
    )

    assert wynik.kod == 0, wynik.calosc
    kat = wywolanie / "logi-wzgledne"
    assert (kat / "testy.log").is_file()
    assert not (KORZEN / "logi-wzgledne").exists()
    assert wynik.stdout.splitlines()[-3:] == _podsumowanie(kat, 0, 0, 0)


def test_na_starcie_stdout_podaje_sciezki_logow_testow_i_mutantow_przed_podsumowaniem(tmp_path):
    wynik = _uruchom(_srodowisko(tmp_path))

    kat = _kat(tmp_path)
    assert wynik.kod == 0, wynik.calosc
    assert wynik.stdout.splitlines()[:2] == [
        f"testy: log {kat}/testy.log",
        f"mutanty: stdout {kat}/mutanty.stdout, stderr {kat}/mutanty.stderr",
    ]


def test_ponowne_uruchomienie_w_tym_samym_katalogu_nadpisuje_logi_zamiast_je_dopisywac(tmp_path):
    kat = _kat(tmp_path)
    kat.mkdir()
    for nazwa in ("testy.log", "mutanty.stdout", "mutanty.stderr"):
        (kat / nazwa).write_text("stara-tresc-z-poprzedniego-przebiegu\n")

    wynik = _uruchom(_srodowisko(tmp_path))

    assert wynik.kod == 0, wynik.calosc
    assert (kat / "mutanty.stdout").read_text() == "mutanty-stdout\n"
    assert (kat / "mutanty.stderr").read_text() == "mutanty-stderr\n"
    assert "stara-tresc" not in (kat / "testy.log").read_text()


def test_trapy_przerwania_sa_w_pliku_przed_startem_obu_bramek():
    # Sygnal miedzy startem bramki a ustawieniem trapu zabilby skrypt domyslna akcja i zostawil
    # sieroty; okno jest za krotkie na deterministyczny test czasowy, wiec sprawdzamy kolejnosc
    # w pliku: oba trapy przed pierwszym startem bramki w tle.
    linie = SKRYPT.read_text(encoding="utf-8").splitlines()

    def numer(poczatek: str) -> int:
        trafione = [i for i, linia in enumerate(linie) if linia.startswith(poczatek)]
        assert len(trafione) == 1, (poczatek, trafione)
        return trafione[0]

    start_testow = numer('"$TESTY" < /dev/null')
    start_mutantow = numer("env -u COCKPIT_TEST_DATABASE_URL")
    assert numer("trap 'przerwij 143' TERM") < start_testow < start_mutantow
    assert numer("trap 'przerwij 130' INT") < start_testow
    # Grupy procesow (set -m) przed trapami: przerwanie zaraz po ich ustawieniu startuje straznika
    # w jego wlasnej grupie, ktora koniec skryptu zabija jednym KILL (HAR-236, review).
    assert numer("set -m") < numer("trap 'przerwij 143' TERM")
    assert numer("set -m") < numer("trap 'przerwij 130' INT")
