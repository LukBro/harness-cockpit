"""Blokada pelnych przebiegow testow w `tools/testy_pelne.sh` (HAR-151).

Skrypt serializuje pelne przebiegi (wspolna baza `cockpit_test`) blokada `mkdir`.
Testy uruchamiaja PRAWDZIWY skrypt z falszywymi narzedziami (docker, npm, python)
na poczatku PATH - bez Dockera i Postgresa. Kazdy test ma wlasna sciezke blokady
w `tmp_path` (testy ida rownolegle `-n 4`, a inne worktree maja wlasne przebiegi).

Skrypt uruchamiamy jawnie przez `/bin/bash`, nie przez shebang (`env bash`): na macOS
`/bin/bash` to bash 3.2 i tylko ta droga wykrywa skladnie niedostepna w starym bashu
(shebang trafilby na bash z Homebrew). Procesy: `tests/procesy.py`, cwd = korzen repo.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from procesy import uruchom_proces_sync, zakoncz_proces_sync

KORZEN = Path(__file__).resolve().parent.parent
SKRYPT = KORZEN / "tools" / "testy_pelne.sh"
# Wartosc tylko do podstawienia w falszywym `docker compose port`; nic nie slucha.
PORT = "54329"
LIMIT_PROCESU = 60

FALSZYWY_DOCKER = """#!/bin/sh
echo "$*" >> "${FALSZYWY_DZIENNIK_DOCKERA:-/dev/null}"
if [ "$1" = info ]; then exit "${FALSZYWY_DOCKER_INFO_KOD:-0}"; fi
if [ "$1" = compose ]; then
  case "$2" in
    version) exit 0 ;;
    port) echo "0.0.0.0:${COCKPIT_PG_PORT}"; exit 0 ;;
    up)
      if [ "${FALSZYWY_DOCKER_UP_KOD:-0}" != 0 ]; then
        echo "Bind for 0.0.0.0:${COCKPIT_PG_PORT} failed: port is already allocated" >&2
      fi
      exit "${FALSZYWY_DOCKER_UP_KOD:-0}" ;;
    exec)
      case "$*" in
        *system_identifier*) echo 7000000000000000001 ;;
        *pg_database*) echo 1 ;;
      esac
      exit 0 ;;
  esac
fi
exit 0
"""

FALSZYWY_NPM = """#!/bin/sh
exit 0
"""

FALSZYWY_PYTHON = """#!/bin/sh
case "$1" in
  -c)
    case "$2" in *asyncpg*) echo 7000000000000000001 ;; esac
    exit 0 ;;
  -m)
    case "$2" in
      playwright) exit 0 ;;
      pytest)
        shift 2
        kind=e2e
        case "$*" in
          *"-n 4"*) kind=pel ;;
          *"baza and not e2e"*) kind=baza ;;
        esac
        tag="${FALSZYWY_TAG:-x}"
        if [ -n "${FALSZYWY_DZIENNIK:-}" ]; then echo "start $tag $kind" >> "$FALSZYWY_DZIENNIK"; fi
        if [ -n "${FALSZYWY_PID_BLOKADY:-}" ]; then
          cat "$COCKPIT_TESTY_BLOKADA/pid" >> "$FALSZYWY_PID_BLOKADY" 2>&1
        fi
        if [ -n "${FALSZYWY_PYTEST_SEN:-}" ]; then sleep "$FALSZYWY_PYTEST_SEN"; fi
        if [ "${FALSZYWY_PYTEST_PADA_W:-}" = "$kind" ]; then
          echo "1 failed, 2 passed"; rc=1
        else
          echo "3 passed"; rc=0
        fi
        if [ -n "${FALSZYWY_DZIENNIK:-}" ]; then echo "koniec $tag $kind" >> "$FALSZYWY_DZIENNIK"; fi
        exit $rc ;;
    esac ;;
esac
exit 0
"""


@dataclass
class Wynik:
    kod: int
    stdout: str
    stderr: str
    czas: float

    @property
    def calosc(self) -> str:
        return self.stdout + self.stderr


def _zapisz_narzedzie(sciezka: Path, tresc: str) -> None:
    sciezka.write_text(tresc)
    sciezka.chmod(0o755)


def _srodowisko(
    tmp_path: Path, *, blokada: Path | None | bool = True, **dodatkowe: str
) -> dict[str, str]:
    """Srodowisko skryptu: falszywe narzedzia, wlasna blokada w tmp_path, bez COCKPIT_TESTY_*.
    `blokada=False` nie ustawia COCKPIT_TESTY_BLOKADA (test sciezki domyslnej)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _zapisz_narzedzie(bin_dir / "docker", FALSZYWY_DOCKER)
    _zapisz_narzedzie(bin_dir / "npm", FALSZYWY_NPM)
    _zapisz_narzedzie(bin_dir / "fake_python", FALSZYWY_PYTHON)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("COCKPIT_TESTY_") and k != "COCKPIT_TEST_DATABASE_URL"
    }
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["PYTHON"] = str(bin_dir / "fake_python")
    env["COCKPIT_PG_PORT"] = PORT
    env["FALSZYWY_DZIENNIK_DOCKERA"] = str(tmp_path / "docker.log")
    if blokada is True:
        env["COCKPIT_TESTY_BLOKADA"] = str(tmp_path / "blokada")
    elif isinstance(blokada, Path):
        env["COCKPIT_TESTY_BLOKADA"] = str(blokada)
    env.update(dodatkowe)
    return env


def _start(env: dict[str, str]) -> tuple[subprocess.Popen, float]:
    proc = uruchom_proces_sync(
        ["/bin/bash", str(SKRYPT)],
        cwd=KORZEN,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc, time.monotonic()


def _koniec(proc: subprocess.Popen, start: float, nazwa: str = "testy_pelne.sh") -> Wynik:
    try:
        out, err = proc.communicate(timeout=LIMIT_PROCESU)
        return Wynik(proc.returncode, out, err, time.monotonic() - start)
    finally:
        zakoncz_proces_sync(proc, nazwa=nazwa)


def _uruchom(env: dict[str, str]) -> Wynik:
    proc, start = _start(env)
    return _koniec(proc, start)


@contextlib.contextmanager
def _zywy_wlasciciel(blokada: Path) -> Iterator[int]:
    """Blokada zajeta przez zyjacy proces (`sleep 60`) - jak drugi pelny przebieg."""
    proces = uruchom_proces_sync(["sleep", "60"], stdout=subprocess.DEVNULL)
    try:
        blokada.mkdir()
        (blokada / "pid").write_text(f"{proces.pid}\n")
        yield proces.pid
    finally:
        zakoncz_proces_sync(proces, nazwa="wlasciciel blokady")


def _martwy_pid() -> int:
    proces = uruchom_proces_sync(["sleep", "0"], stdout=subprocess.DEVNULL)
    proces.wait(timeout=10)
    return proces.pid


def _linie_dziennika(tmp_path: Path) -> list[list[str]]:
    sciezka = tmp_path / "pytest.log"
    if not sciezka.exists():
        return []
    return [linia.split() for linia in sciezka.read_text().splitlines()]


def _pozycje(linie: list[list[str]], tag: str) -> list[int]:
    return [i for i, (_, t, _) in enumerate(linie) if t == tag]


def _przedzialy_rozlaczne(linie: list[list[str]]) -> bool:
    a, b = _pozycje(linie, "A"), _pozycje(linie, "B")
    return max(a) < min(b) or max(b) < min(a)


def _dwa_rownolegle(tmp_path: Path, blokada_a: Path, blokada_b: Path) -> tuple[Wynik, Wynik]:
    wspolne = {
        "FALSZYWY_DZIENNIK": str(tmp_path / "pytest.log"),
        "FALSZYWY_PYTEST_SEN": "0.6",
    }
    env_a = _srodowisko(tmp_path, blokada=blokada_a, FALSZYWY_TAG="A", **wspolne)
    env_b = _srodowisko(tmp_path, blokada=blokada_b, FALSZYWY_TAG="B", **wspolne)
    proc_a, start_a = _start(env_a)
    proc_b, start_b = _start(env_b)
    try:
        return _koniec(proc_a, start_a, "przebieg A"), _koniec(proc_b, start_b, "przebieg B")
    finally:
        zakoncz_proces_sync(proc_a, nazwa="przebieg A")
        zakoncz_proces_sync(proc_b, nazwa="przebieg B")


# --- kryterium: dwa rownolegle uruchomienia sie nie nakladaja -----------------------


def test_drugi_pelny_przebieg_czeka_na_koniec_pierwszego(tmp_path):
    wspolna = tmp_path / "wspolna_blokada"
    a, b = _dwa_rownolegle(tmp_path, wspolna, wspolna)
    assert (a.kod, b.kod) == (0, 0), (a.stderr, b.stderr)
    linie = _linie_dziennika(tmp_path)
    assert len(_pozycje(linie, "A")) == 6 and len(_pozycje(linie, "B")) == 6
    assert _przedzialy_rozlaczne(linie), linie


def test_bez_wspolnej_blokady_przebiegi_nakladaja_sie_w_dzienniku(tmp_path):
    """Kontrola detektora: bez wspolnej blokady przedzialy sie przecinaja, wiec
    asercja z testu powyzej moze oblac."""
    a, b = _dwa_rownolegle(tmp_path, tmp_path / "blokada_a", tmp_path / "blokada_b")
    assert (a.kod, b.kod) == (0, 0), (a.stderr, b.stderr)
    assert not _przedzialy_rozlaczne(_linie_dziennika(tmp_path))


def test_oczekujacy_przebieg_pisze_o_czekaniu_tylko_raz(tmp_path):
    blokada = tmp_path / "blokada"
    with _zywy_wlasciciel(blokada) as pid:
        wynik = _uruchom(_srodowisko(tmp_path, COCKPIT_TESTY_LIMIT_BLOKADY="3"))
    assert wynik.kod == 75
    assert wynik.stderr.count("Czekam na blokade pelnego przebiegu") == 1
    assert f"Czekam na blokade pelnego przebiegu (trzyma PID {pid}, limit 3 s)" in wynik.stderr


# --- kryterium: blokada martwego procesu przejeta bez czekania -------------------------


def test_blokada_martwego_procesu_jest_przejeta_bez_czekania_do_limitu(tmp_path):
    blokada = tmp_path / "blokada"
    blokada.mkdir()
    martwy = _martwy_pid()
    (blokada / "pid").write_text(f"{martwy}\n")
    wynik = _uruchom(_srodowisko(tmp_path, COCKPIT_TESTY_LIMIT_BLOKADY="40"))
    assert wynik.kod == 0, wynik.stderr
    assert f"Przejmuje blokade martwego procesu PID {martwy}: {blokada}" in wynik.stderr
    assert wynik.czas < 20  # limit to 40 s; czekanie do limitu przekroczyloby ten prog


def test_pid_nieliczbowy_w_pliku_blokady_oznacza_blokade_martwego_procesu(tmp_path):
    blokada = tmp_path / "blokada"
    blokada.mkdir()
    (blokada / "pid").write_text("nie-liczba\n")
    wynik = _uruchom(_srodowisko(tmp_path, COCKPIT_TESTY_LIMIT_BLOKADY="40"))
    assert wynik.kod == 0, wynik.stderr
    assert f"Przejmuje blokade martwego procesu PID nie-liczba: {blokada}" in wynik.stderr
    assert wynik.czas < 20


def test_blokada_bez_pliku_pid_jest_traktowana_jak_zajeta_nie_martwa(tmp_path):
    blokada = tmp_path / "blokada"
    blokada.mkdir()  # wlasciciel dopiero zapisuje PID
    wynik = _uruchom(_srodowisko(tmp_path, COCKPIT_TESTY_LIMIT_BLOKADY="1"))
    assert wynik.kod == 75, wynik.stderr
    assert "Przejmuje blokade" not in wynik.stderr
    assert blokada.is_dir()


# --- kryterium: oczekiwanie dluzsze niz limit ----------------------------------------


def test_zajeta_blokada_po_limicie_konczy_sie_kodem_75(tmp_path):
    with _zywy_wlasciciel(tmp_path / "blokada"):
        wynik = _uruchom(_srodowisko(tmp_path, COCKPIT_TESTY_LIMIT_BLOKADY="1"))
    assert wynik.kod == 75, wynik.stderr


def test_zajeta_blokada_po_limicie_podaje_pid_wlasciciela_i_sciezke(tmp_path):
    blokada = tmp_path / "blokada"
    with _zywy_wlasciciel(blokada) as pid:
        wynik = _uruchom(_srodowisko(tmp_path, COCKPIT_TESTY_LIMIT_BLOKADY="1"))
    assert (
        f"BLAD: blokada pelnego przebiegu zajeta przez PID {pid} ({blokada}) - limit 1 s minal; "
        "inny przebieg testow trwa, ponow pozniej albo zwieksz COCKPIT_TESTY_LIMIT_BLOKADY"
    ) in wynik.stderr
    assert "Traceback" not in wynik.calosc


def test_zajeta_blokada_czeka_przynajmniej_do_limitu_przed_poddaniem_sie(tmp_path):
    with _zywy_wlasciciel(tmp_path / "blokada"):
        wynik = _uruchom(_srodowisko(tmp_path, COCKPIT_TESTY_LIMIT_BLOKADY="2"))
    assert wynik.kod == 75
    assert wynik.czas >= 2


def test_limit_zero_z_zywym_wlascicielem_konczy_od_razu_kodem_75(tmp_path):
    with _zywy_wlasciciel(tmp_path / "blokada"):
        wynik = _uruchom(_srodowisko(tmp_path, COCKPIT_TESTY_LIMIT_BLOKADY="0"))
    assert wynik.kod == 75, wynik.stderr
    assert wynik.czas < 10


def test_domyslny_limit_oczekiwania_nie_jest_zerem(tmp_path):
    """Bez COCKPIT_TESTY_LIMIT_BLOKADY przebieg czeka na zwolnienie blokady, a nie
    poddaje sie od razu: wlasciciel konczy sie po ~2 s, wtedy przebieg dostaje blokade."""
    blokada = tmp_path / "blokada"
    wlasciciel = uruchom_proces_sync(["sleep", "2"], stdout=subprocess.DEVNULL)
    try:
        blokada.mkdir()
        (blokada / "pid").write_text(f"{wlasciciel.pid}\n")
        # Status konczacego sie wlasciciela odbiera watek - inaczej zostaje zombie, a
        # `ps -p` w skrypcie widzi go jako zywy proces (w uzyciu odbiera go rodzic basha).
        odbior = threading.Thread(target=lambda: wlasciciel.wait(timeout=20), daemon=True)
        odbior.start()
        wynik = _uruchom(_srodowisko(tmp_path))
        odbior.join(timeout=20)
    finally:
        zakoncz_proces_sync(wlasciciel, nazwa="wlasciciel blokady")
    assert wynik.kod == 0, wynik.stderr
    assert wynik.czas >= 1


@pytest.mark.parametrize("wartosc", ["abc", "-5", "1.5"])
def test_nieliczbowy_limit_blokady_to_blad_bez_czekania_i_bez_blokady(tmp_path, wartosc):
    blokada = tmp_path / "blokada"
    wynik = _uruchom(_srodowisko(tmp_path, COCKPIT_TESTY_LIMIT_BLOKADY=wartosc))
    assert wynik.kod == 1, wynik.stderr
    assert (
        f"BLAD: COCKPIT_TESTY_LIMIT_BLOKADY musi byc liczba sekund (jest: '{wartosc}')"
        in wynik.stderr
    )
    assert not blokada.exists()
    assert "Traceback" not in wynik.calosc


# --- zdobycie i zwolnienie blokady ----------------------------------------------------


def test_w_czasie_przebiegu_blokada_nalezy_do_pid_skryptu(tmp_path):
    env = _srodowisko(tmp_path, FALSZYWY_PID_BLOKADY=str(tmp_path / "pid_w_trakcie.txt"))
    proc, start = _start(env)
    wynik = _koniec(proc, start)
    assert wynik.kod == 0, wynik.stderr
    zapisane = (tmp_path / "pid_w_trakcie.txt").read_text().split()
    assert zapisane == [str(proc.pid)] * 3  # trzy przebiegi pytest, wszystkie pod blokada


def test_zdobycie_blokady_jest_zgloszone_na_stderr(tmp_path):
    blokada = tmp_path / "blokada"
    proc, start = _start(_srodowisko(tmp_path))
    wynik = _koniec(proc, start)
    assert f"Blokada pelnego przebiegu zdobyta: {blokada} (PID {proc.pid})" in wynik.stderr


def test_domyslna_blokada_lezy_w_katalogu_tymczasowym_bez_podwojnego_ukosnika(tmp_path):
    env = _srodowisko(tmp_path, blokada=False, TMPDIR=f"{tmp_path}/")
    wynik = _uruchom(env)
    assert wynik.kod == 0, wynik.stderr
    assert f"Blokada pelnego przebiegu zdobyta: {tmp_path}/cockpit_testy_pelne.lock (PID" in (
        wynik.stderr
    )


def test_po_udanym_przebiegu_blokada_jest_zwolniona(tmp_path):
    wynik = _uruchom(_srodowisko(tmp_path))
    assert wynik.kod == 0, wynik.stderr
    assert not (tmp_path / "blokada").exists()


def test_po_przebiegu_z_porazka_testow_blokada_jest_zwolniona(tmp_path):
    wynik = _uruchom(_srodowisko(tmp_path, FALSZYWY_PYTEST_PADA_W="baza"))
    assert wynik.kod == 1, wynik.stderr
    assert not (tmp_path / "blokada").exists()


def test_po_nieudanym_starcie_bazy_blokada_jest_zwolniona(tmp_path):
    wynik = _uruchom(_srodowisko(tmp_path, FALSZYWY_DOCKER_UP_KOD="17"))
    assert wynik.kod == 1, wynik.stderr
    assert not (tmp_path / "blokada").exists()


def test_po_przejeciu_blokady_martwego_procesu_blokada_jest_zwolniona(tmp_path):
    blokada = tmp_path / "blokada"
    blokada.mkdir()
    (blokada / "pid").write_text(f"{_martwy_pid()}\n")
    wynik = _uruchom(_srodowisko(tmp_path))
    assert wynik.kod == 0, wynik.stderr
    assert not blokada.exists()


def test_przebieg_ktory_poddal_sie_na_limicie_nie_usuwa_cudzej_blokady(tmp_path):
    blokada = tmp_path / "blokada"
    with _zywy_wlasciciel(blokada) as pid:
        wynik = _uruchom(_srodowisko(tmp_path, COCKPIT_TESTY_LIMIT_BLOKADY="0"))
        assert wynik.kod == 75, wynik.stderr
        assert blokada.is_dir()
        assert (blokada / "pid").read_text().strip() == str(pid)


# --- kolejnosc: kontrole narzedzi przed blokada, blokada przed baza ---------------------


def test_zajeta_blokada_konczy_przebieg_zanim_ruszy_baza(tmp_path):
    with _zywy_wlasciciel(tmp_path / "blokada"):
        wynik = _uruchom(
            _srodowisko(tmp_path, COCKPIT_TESTY_LIMIT_BLOKADY="0", FALSZYWY_DOCKER_UP_KOD="17")
        )
    assert wynik.kod == 75, wynik.stderr
    polecenia = (tmp_path / "docker.log").read_text().splitlines()
    assert not [p for p in polecenia if p.startswith("compose up")], polecenia
    assert not [p for p in polecenia if p.startswith("compose port")], polecenia


# --- kryterium: wylaczony demon Dockera ------------------------------------------------


def test_wylaczony_demon_dockera_daje_komunikat_dziedziny_bez_tracebacku(tmp_path):
    wynik = _uruchom(_srodowisko(tmp_path, FALSZYWY_DOCKER_INFO_KOD="1"))
    assert wynik.kod == 1, wynik.stderr
    assert "BLAD: demon Dockera nie dziala - uruchom Docker i ponow" in wynik.stderr
    assert "Traceback" not in wynik.calosc
    assert not (tmp_path / "blokada").exists()


def test_kontrola_demona_dockera_jest_przed_zajeciem_blokady(tmp_path):
    """Przy zajetej blokadzie i limicie 0 kontrola demona wciaz musi byc pierwsza:
    kod 1 z komunikatem Dockera, nie 75."""
    with _zywy_wlasciciel(tmp_path / "blokada"):
        wynik = _uruchom(
            _srodowisko(tmp_path, COCKPIT_TESTY_LIMIT_BLOKADY="0", FALSZYWY_DOCKER_INFO_KOD="1")
        )
    assert wynik.kod == 1, wynik.stderr
    assert "demon Dockera nie dziala" in wynik.stderr
    assert "blokada pelnego przebiegu zajeta" not in wynik.stderr


# --- zajety port bazy / nieudany start bazy ---------------------------------------------


def test_nieudany_start_bazy_daje_komunikat_dziedziny_i_kod_1_bez_tracebacku(tmp_path):
    wynik = _uruchom(_srodowisko(tmp_path, FALSZYWY_DOCKER_UP_KOD="17"))
    assert wynik.kod == 1, wynik.stderr
    assert (
        f"BLAD: nie udalo sie uruchomic bazy (docker compose up db) - sprawdz, czy port {PORT} "
        "jest wolny i Docker dziala; inny port: COCKPIT_PG_PORT=5433 "
    ) in wynik.stderr
    assert "Traceback" not in wynik.calosc


def test_nieudany_start_bazy_zostawia_wyjscie_dockera_na_stderr(tmp_path):
    wynik = _uruchom(_srodowisko(tmp_path, FALSZYWY_DOCKER_UP_KOD="17"))
    assert "port is already allocated" in wynik.stderr


def test_nieudany_start_bazy_nie_uruchamia_zadnego_przebiegu_testow(tmp_path):
    dziennik = str(tmp_path / "pytest.log")
    _uruchom(_srodowisko(tmp_path, FALSZYWY_DOCKER_UP_KOD="17", FALSZYWY_DZIENNIK=dziennik))
    assert _linie_dziennika(tmp_path) == []


# --- kryterium: bez konkurencji ten sam wynik testow co przed zmiana -------------------


def test_bez_konkurencji_trzy_zielone_przebiegi_daja_kod_0_i_komunikat_ok(tmp_path):
    wynik = _uruchom(_srodowisko(tmp_path, FALSZYWY_DZIENNIK=str(tmp_path / "pytest.log")))
    assert wynik.kod == 0, wynik.stderr
    assert "OK: pelny zestaw testow zielony" in wynik.stdout
    assert wynik.stdout.count("3 passed") == 3
    assert "failed" not in wynik.calosc
    starty = [linia[2] for linia in _linie_dziennika(tmp_path) if linia[0] == "start"]
    assert starty == ["pel", "baza", "e2e"]


@pytest.mark.parametrize(
    ("przebieg", "komunikat"),
    [
        ("pel", "BLAD: pytest -n 4 (bez bazy i e2e) zakonczyl sie kodem 1"),
        ("baza", "BLAD: pytest -m baza zakonczyl sie kodem 1"),
        ("e2e", "BLAD: pytest -m e2e zakonczyl sie kodem 1"),
    ],
)
def test_porazka_jednego_przebiegu_daje_kod_1_a_pozostale_przebiegi_i_tak_ida(
    tmp_path, przebieg, komunikat
):
    wynik = _uruchom(
        _srodowisko(
            tmp_path,
            FALSZYWY_PYTEST_PADA_W=przebieg,
            FALSZYWY_DZIENNIK=str(tmp_path / "pytest.log"),
        )
    )
    assert wynik.kod == 1, wynik.stderr
    assert komunikat in wynik.stderr
    assert "OK: pelny zestaw testow zielony" not in wynik.stdout
    starty = [linia[2] for linia in _linie_dziennika(tmp_path) if linia[0] == "start"]
    assert starty == ["pel", "baza", "e2e"]


# --- wylacznosc przejecia blokady, limit i sygnaly (runda 2, HAR-151) -------------------

FALSZYWY_SLEEP = """#!/bin/sh
echo "$*" >> "$FALSZYWY_DZIENNIK_SLEEP"
exit 0
"""

# Pierwsze wywolanie `ps` = proces martwy (kod 1), kazde kolejne = zywy (kod 0): odtwarza
# wyscig, w ktorym inny przebieg przejmuje blokade miedzy pierwszym a drugim sprawdzeniem.
FALSZYWY_PS = """#!/bin/sh
if [ ! -e "$FALSZYWY_LICZNIK_PS" ]; then
  echo x > "$FALSZYWY_LICZNIK_PS"
  exit 1
fi
echo x >> "$FALSZYWY_LICZNIK_PS"
exit 0
"""


def _sleep_bez_czekania(tmp_path: Path) -> Path:
    """Podmienia `sleep` na zapis do dziennika (bez czekania); zwraca sciezke dziennika.
    Liczba wywolan = liczba sekund, ktore skrypt chcialby przeczekac."""
    _zapisz_narzedzie(tmp_path / "bin" / "sleep", FALSZYWY_SLEEP)
    return tmp_path / "sleep.log"


def _przejecie(blokada: Path) -> Path:
    return Path(f"{blokada}.przejecie")


def test_przejecie_blokady_przez_inny_przebieg_zatrzymuje_przejecie_i_konczy_sie_na_limicie(
    tmp_path,
):
    """Martwy wlasciciel, ale `<blokada>.przejecie` trzyma inny przebieg: nie przejmujemy,
    blokada zostaje, a przy limicie 0 nie ma ani sekundy czekania."""
    blokada = tmp_path / "blokada"
    blokada.mkdir()
    (blokada / "pid").write_text(f"{_martwy_pid()}\n")
    _przejecie(blokada).mkdir()
    env = _srodowisko(
        tmp_path,
        COCKPIT_TESTY_LIMIT_BLOKADY="0",
        FALSZYWY_DZIENNIK_SLEEP=str(tmp_path / "sleep.log"),
    )
    dziennik = _sleep_bez_czekania(tmp_path)
    wynik = _uruchom(env)
    assert wynik.kod == 75, wynik.stderr
    assert (
        f"BLAD: przejecie blokady pelnego przebiegu zajete ({blokada}.przejecie) - limit 0 s "
        "minal; jesli zaden przebieg testow nie trwa, usun ten katalog recznie"
    ) in wynik.stderr
    assert "Przejmuje blokade" not in wynik.stderr
    assert (blokada / "pid").is_file()  # blokada nietknieta
    assert _przejecie(blokada).is_dir()  # cudze przejecie nie zostalo usuniete
    assert not dziennik.exists()  # limit 0 = zadnego sleep


def test_osierocone_przejecie_blokady_liczy_sie_do_limitu_czekania(tmp_path):
    blokada = tmp_path / "blokada"
    blokada.mkdir()
    (blokada / "pid").write_text(f"{_martwy_pid()}\n")
    _przejecie(blokada).mkdir()
    wynik = _uruchom(_srodowisko(tmp_path, COCKPIT_TESTY_LIMIT_BLOKADY="1"))
    assert wynik.kod == 75, wynik.stderr
    assert (
        f"BLAD: przejecie blokady pelnego przebiegu zajete ({blokada}.przejecie) - limit 1 s "
        "minal; jesli zaden przebieg testow nie trwa, usun ten katalog recznie"
    ) in wynik.stderr
    assert wynik.czas >= 1  # czekanie na przejecie nie omija limitu, ale go tez nie skraca
    assert _przejecie(blokada).is_dir()


def test_blokada_przejeta_przez_inny_przebieg_miedzy_sprawdzeniami_nie_jest_usuwana(tmp_path):
    """Pierwsze sprawdzenie widzi martwego wlasciciela, ponowne (pod blokada pomocnicza)
    zywego - jak po przejeciu przez konkurenta. Blokada musi zostac."""
    blokada = tmp_path / "blokada"
    blokada.mkdir()
    (blokada / "pid").write_text("4242424\n")  # tresc bez znaczenia: decyduje falszywy ps
    env = _srodowisko(
        tmp_path, COCKPIT_TESTY_LIMIT_BLOKADY="0", FALSZYWY_LICZNIK_PS=str(tmp_path / "ps.log")
    )
    _zapisz_narzedzie(tmp_path / "bin" / "ps", FALSZYWY_PS)
    wynik = _uruchom(env)
    assert wynik.kod == 75, wynik.stderr
    licznik = (tmp_path / "ps.log").read_text().split()
    assert licznik == ["x"] * 3  # martwy, zywy (ponowne), zywy (po continue)
    assert "Przejmuje blokade" not in wynik.stderr
    assert (blokada / "pid").read_text().strip() == "4242424"
    assert not _przejecie(blokada).exists()  # pomocnicza blokada zwolniona mimo braku przejecia


def test_po_przejeciu_blokady_martwego_procesu_katalog_przejecia_jest_zwolniony(tmp_path):
    blokada = tmp_path / "blokada"
    blokada.mkdir()
    (blokada / "pid").write_text(f"{_martwy_pid()}\n")
    wynik = _uruchom(_srodowisko(tmp_path))
    assert wynik.kod == 0, wynik.stderr
    assert "Przejmuje blokade martwego procesu" in wynik.stderr  # przejecie faktycznie bylo
    assert not _przejecie(blokada).exists()


def test_limit_zero_z_zywym_wlascicielem_nie_czeka_ani_sekundy_i_nie_zapowiada_czekania(
    tmp_path,
):
    """Granica limitu: przy limicie 0 pierwsza proba jest ostatnia. Bez czekania nie ma
    ani wywolania sleep, ani komunikatu `Czekam na blokade`."""
    env = _srodowisko(
        tmp_path,
        COCKPIT_TESTY_LIMIT_BLOKADY="0",
        FALSZYWY_DZIENNIK_SLEEP=str(tmp_path / "sleep.log"),
    )
    dziennik = _sleep_bez_czekania(tmp_path)
    with _zywy_wlasciciel(tmp_path / "blokada"):
        wynik = _uruchom(env)
    assert wynik.kod == 75, wynik.stderr
    assert "Czekam na blokade" not in wynik.stderr
    assert not dziennik.exists()


def test_limit_jeden_z_zywym_wlascicielem_czeka_dokladnie_jedna_sekunde(tmp_path):
    env = _srodowisko(
        tmp_path,
        COCKPIT_TESTY_LIMIT_BLOKADY="1",
        FALSZYWY_DZIENNIK_SLEEP=str(tmp_path / "sleep.log"),
    )
    dziennik = _sleep_bez_czekania(tmp_path)
    with _zywy_wlasciciel(tmp_path / "blokada"):
        wynik = _uruchom(env)
    assert wynik.kod == 75, wynik.stderr
    assert dziennik.read_text().split() == ["1"]


def test_pid_wlasciciela_z_bialymi_znakami_jest_rozpoznany_jako_zywy_proces(tmp_path):
    blokada = tmp_path / "blokada"
    blokada.mkdir()
    wlasciciel = uruchom_proces_sync(["sleep", "60"], stdout=subprocess.DEVNULL)
    try:
        (blokada / "pid").write_text(f" {wlasciciel.pid}\r\n")
        wynik = _uruchom(_srodowisko(tmp_path, COCKPIT_TESTY_LIMIT_BLOKADY="0"))
    finally:
        zakoncz_proces_sync(wlasciciel, nazwa="wlasciciel blokady")
    assert wynik.kod == 75, wynik.stderr
    assert f"zajeta przez PID {wlasciciel.pid} ({blokada})" in wynik.stderr
    assert "Przejmuje blokade" not in wynik.stderr
    assert (blokada / "pid").is_file()


def test_blokada_bez_pliku_pid_po_limicie_mowi_ze_pid_jest_nieznany(tmp_path):
    blokada = tmp_path / "blokada"
    blokada.mkdir()
    wynik = _uruchom(_srodowisko(tmp_path, COCKPIT_TESTY_LIMIT_BLOKADY="0"))
    assert wynik.kod == 75, wynik.stderr
    assert f"zajeta przez PID ? ({blokada}) - limit 0 s minal" in wynik.stderr
    assert blokada.is_dir()


def _srodowisko_bez_ps(tmp_path: Path, **dodatkowe: str) -> dict[str, str]:
    """PATH = falszywe narzedzia + dowiazania do narzedzi z systemu, ale bez `ps`."""
    env = _srodowisko(tmp_path, **dodatkowe)
    narzedzia = tmp_path / "narzedzia"
    narzedzia.mkdir()
    for nazwa in (
        "dirname", "mkdir", "rmdir", "rm", "cat", "tr", "sleep", "mktemp", "tee", "grep",
        "lsof", "env", "sh",
    ):
        sciezka = shutil.which(nazwa)
        if sciezka is not None:
            os.symlink(sciezka, narzedzia / nazwa)
    assert (narzedzia / "dirname").exists() and not (narzedzia / "ps").exists()
    env["PATH"] = f"{tmp_path / 'bin'}:{narzedzia}"
    return env


def test_brak_ps_to_blad_narzedzia_a_nie_przejecie_blokady_zywego_wlasciciela(tmp_path):
    blokada = tmp_path / "blokada"
    with _zywy_wlasciciel(blokada) as pid:
        wynik = _uruchom(_srodowisko_bez_ps(tmp_path, COCKPIT_TESTY_LIMIT_BLOKADY="0"))
        assert wynik.kod == 1, wynik.stderr
        assert (
            "BLAD: brak ps - potrzebny do sprawdzenia, czy wlasciciel blokady zyje"
        ) in wynik.stderr
        assert "Przejmuje blokade" not in wynik.stderr
        assert (blokada / "pid").read_text().strip() == str(pid)  # wlasciciel nietkniety


def test_brak_ps_nie_zajmuje_blokady(tmp_path):
    wynik = _uruchom(_srodowisko_bez_ps(tmp_path))
    assert wynik.kod == 1, wynik.stderr
    assert "brak ps" in wynik.stderr
    assert not (tmp_path / "blokada").exists()
    assert "Traceback" not in wynik.calosc


def test_przebieg_przerwany_sygnalem_zwalnia_swoja_blokade(tmp_path):
    blokada = tmp_path / "blokada"
    dziennik = tmp_path / "pytest.log"
    env = _srodowisko(
        tmp_path, FALSZYWY_DZIENNIK=str(dziennik), FALSZYWY_PYTEST_SEN="30", FALSZYWY_TAG="S"
    )
    proc, _ = _start(env)
    try:
        koniec = time.monotonic() + 20
        while not (dziennik.exists() and "start" in dziennik.read_text()):
            assert time.monotonic() < koniec, "przebieg testow nie ruszyl w limicie 20 s"
            time.sleep(0.05)
        assert (blokada / "pid").read_text().strip() == str(proc.pid)  # przebieg trzyma blokade
        zakoncz_proces_sync(proc, nazwa="przerywany przebieg")
        koniec = time.monotonic() + 5
        while blokada.exists() and time.monotonic() < koniec:
            time.sleep(0.05)
        proc.communicate(timeout=10)
    finally:
        zakoncz_proces_sync(proc, nazwa="przerywany przebieg")
    assert not blokada.exists()
    assert proc.returncode == 143  # trap TERM -> exit 143 -> sprzatnij, nie smierc od sygnalu


# --- runda 3, HAR-151: galaz sprzatania przejecia i krok licznika limitu -----------------

FALSZYWY_RM_PADA = """#!/bin/sh
exit 1
"""


def test_przebieg_przerwany_w_trakcie_przejmowania_zwalnia_blokade_przejecia(tmp_path):
    """Martwy wlasciciel, a usuniecie blokady pada: `set -e` konczy skrypt przy
    PRZEJECIE=1, zanim galaz glowna zwolni przejecie. Tylko `sprzatnij` usuwa katalog."""
    blokada = tmp_path / "blokada"
    blokada.mkdir()
    martwy = _martwy_pid()
    (blokada / "pid").write_text(f"{martwy}\n")
    env = _srodowisko(tmp_path)
    _zapisz_narzedzie(tmp_path / "bin" / "rm", FALSZYWY_RM_PADA)
    wynik = _uruchom(env)
    assert wynik.kod == 1, wynik.stderr  # kod nieudanego rm, nie 0 ani 75
    assert f"Przejmuje blokade martwego procesu PID {martwy}: {blokada}" in wynik.stderr
    assert (blokada / "pid").is_file()  # usuniecie faktycznie nie zadzialalo
    assert not _przejecie(blokada).exists()


def test_osierocone_przejecie_przy_limicie_dwa_czeka_dokladnie_dwie_sekundy(tmp_path):
    blokada = tmp_path / "blokada"
    blokada.mkdir()
    (blokada / "pid").write_text(f"{_martwy_pid()}\n")
    _przejecie(blokada).mkdir()
    env = _srodowisko(
        tmp_path,
        COCKPIT_TESTY_LIMIT_BLOKADY="2",
        FALSZYWY_DZIENNIK_SLEEP=str(tmp_path / "sleep.log"),
    )
    dziennik = _sleep_bez_czekania(tmp_path)
    wynik = _uruchom(env)
    assert wynik.kod == 75, wynik.stderr
    assert dziennik.read_text().split() == ["1", "1"]
    assert (
        f"BLAD: przejecie blokady pelnego przebiegu zajete ({blokada}.przejecie) - limit 2 s "
        "minal"
    ) in wynik.stderr


def test_limit_dwa_z_zywym_wlascicielem_czeka_dokladnie_dwie_sekundy(tmp_path):
    env = _srodowisko(
        tmp_path,
        COCKPIT_TESTY_LIMIT_BLOKADY="2",
        FALSZYWY_DZIENNIK_SLEEP=str(tmp_path / "sleep.log"),
    )
    dziennik = _sleep_bez_czekania(tmp_path)
    with _zywy_wlasciciel(tmp_path / "blokada"):
        wynik = _uruchom(env)
    assert wynik.kod == 75, wynik.stderr
    assert dziennik.read_text().split() == ["1", "1"]
    assert "limit 2 s minal" in wynik.stderr
