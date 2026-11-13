"""Blokady, baza przebiegu i port demo w `tools/testy_pelne.sh` (HAR-151, HAR-236).

Skrypt ogranicza liczbe rownoleglych pelnych przebiegow semaforem `mkdir` (HAR-236: domyslnie dwa
sloty), krotka blokada chroni wspolny zasob, a kazdy przebieg ma wlasna baze i port demo.
Testy uruchamiaja PRAWDZIWY skrypt z falszywymi narzedziami (docker z atrapa serwera PostgreSQL,
npm, python) na poczatku PATH - bez Dockera i Postgresa. Kazdy test ma wlasna sciezke blokady
w `tmp_path` (testy ida rownolegle `-n 4`, a inne worktree maja wlasne przebiegi). Czekanie na
zdarzenie (spotkanie przebiegow, zwolnienie slotu w falszywym `sleep`) zamiast czasu: testy nie
zaleza od obciazenia maszyny.

Skrypt uruchamiamy jawnie przez `/bin/bash`, nie przez shebang (`env bash`): na macOS
`/bin/bash` to bash 3.2 i tylko ta droga wykrywa skladnie niedostepna w starym bashu
(shebang trafilby na bash z Homebrew). Procesy: `tests/procesy.py`, cwd = korzen repo.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import socket
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from procesy import (
    przerwij_sigtermem_sync,
    uruchom_proces_sync,
    zabij_proces,
    zakoncz_proces_sync,
)

KORZEN = Path(__file__).resolve().parent.parent
SKRYPT = KORZEN / "tools" / "testy_pelne.sh"
# Wartosc tylko do podstawienia w falszywym `docker compose port`; nic nie slucha.
PORT = "54329"
LIMIT_PROCESU = 180  # zabezpieczenie przed zawieszeniem, nie oczekiwany czas
# Zabezpieczenie (HAR-226), nie oczekiwany czas: start falszywego przebiegu to fork/exec skryptu
# i kilku powlok, ktore pod obciazeniem rownoleglych sesji trwaja dziesiatki sekund.
LIMIT_STARTU_PRZEBIEGU = 120

FALSZYWY_DOCKER = r"""#!/bin/sh
echo "$*" >> "${FALSZYWY_DZIENNIK_DOCKERA:-/dev/null}"
# Liczba argumentow kazdego wywolania (psql_db ma przekazac SQL jako JEDEN argument).
if [ -n "${FALSZYWY_ARGC_DOCKERA:-}" ]; then echo "$# $*" >> "$FALSZYWY_ARGC_DOCKERA"; fi
# Dziennik stanu wspolnego zasobu ($BLOKADA.zasob) w chwili kazdego wywolania (HAR-236, punkt 7).
if [ -n "${FALSZYWY_ZASOB_LOG:-}" ]; then
  if [ -e "${FALSZYWY_SCIEZKA_ZASOBU:-/nie/ma/takiej/sciezki}" ]; then stan=jest; else stan=brak; fi
  echo "docker $* zasob=$stan" >> "$FALSZYWY_ZASOB_LOG"
fi
if [ "$1" = info ]; then exit "${FALSZYWY_DOCKER_INFO_KOD:-0}"; fi
if [ "$1" = compose ]; then
  case "$2" in
    version) exit 0 ;;
    port) echo "0.0.0.0:${COCKPIT_PG_PORT}"; exit 0 ;;
    up)
      # Pozostalosc po martwym przebiegu o PID-ie tego skryptu ($PPID = skrypt, docker idzie wprost).
      if [ -n "${FALSZYWY_STARA_BAZA_SKROT:-}" ]; then
        : > "$FALSZYWY_STAN_BAZ/cockpit_test_${FALSZYWY_STARA_BAZA_SKROT}_$PPID"
        : > "$FALSZYWY_STAN_BAZ/cockpit_test_${FALSZYWY_STARA_BAZA_SKROT}_${PPID}_gw0"
      fi
      if [ "${FALSZYWY_DOCKER_UP_KOD:-0}" != 0 ]; then
        echo "Bind for 0.0.0.0:${COCKPIT_PG_PORT} failed: port is already allocated" >&2
      fi
      exit "${FALSZYWY_DOCKER_UP_KOD:-0}" ;;
    exec)
      # Atrapa serwera PostgreSQL: bazy to pliki w $FALSZYWY_STAN_BAZ; CREATE/DROP zmieniaja ten stan,
      # a zapytania o pg_database czytaja go (LIKE jak w PostgreSQL: % i _ to wzorce, \_ to znak).
      sql="$*"
      case "$sql" in
        *system_identifier*) echo 7000000000000000001; exit 0 ;;
        *"CREATE DATABASE"*)
          nazwa="$(printf '%s' "$sql" | sed -n 's/.*CREATE DATABASE "*\([^" ]*\).*/\1/p')"
          if [ -e "$FALSZYWY_STAN_BAZ/$nazwa" ]; then
            echo "ERROR:  database \"$nazwa\" already exists" >&2
            exit 1
          fi
          : > "$FALSZYWY_STAN_BAZ/$nazwa"
          # Jak psql: znacznik polecenia idzie na stdout (skrypt ma go odrzucac).
          echo "CREATE DATABASE"
          exit 0 ;;
        *"DROP DATABASE"*)
          nazwa="$(printf '%s' "$sql" | sed -n 's/.*DROP DATABASE IF EXISTS "*\([^" ]*\).*/\1/p')"
          if [ -n "${FALSZYWY_FLAGA_DROP:-}" ] && [ -e "$FALSZYWY_FLAGA_DROP" ]; then
            case "$nazwa" in
              ${FALSZYWY_DROP_PADA_DLA:-*}) echo "ERROR:  drop zablokowany" >&2; exit 1 ;;
            esac
          fi
          rm -f "$FALSZYWY_STAN_BAZ/$nazwa" || true
          echo "DROP DATABASE"
          exit 0 ;;
        *pg_database*)
          case "$sql" in
            *"datname='cockpit'"*)
              if [ "${FALSZYWY_BRAK_COCKPIT:-0}" = 1 ] && [ ! -e "$FALSZYWY_STAN_BAZ/cockpit" ]; then exit 0; fi
              echo 1; exit 0 ;;
            *"LIKE '"*)
              if [ -n "${FALSZYWY_LISTA_PADA:-}" ] && [ -e "${FALSZYWY_FLAGA_DROP:-/nie/ma}" ]; then
                echo "ERROR:  lista zablokowana" >&2
                exit 1
              fi
              wzorzec="$(printf '%s' "$sql" | sed -n "s/.*LIKE '\([^']*\)'.*/\1/p")"
              glob="$(printf '%s' "$wzorzec" | sed -e 's/\\_/@LIT@/g' -e 's/_/?/g' -e 's/%/*/g' -e 's/@LIT@/_/g')"
              for plik in "$FALSZYWY_STAN_BAZ"/*; do
                [ -e "$plik" ] || continue
                nazwa="${plik##*/}"
                case "$nazwa" in $glob) echo "$nazwa" ;; esac
              done
              exit 0 ;;
          esac
          exit 0 ;;
      esac
      exit 0 ;;
  esac
fi
exit 0
"""

FALSZYWY_NPM = r"""#!/bin/sh
if [ -n "${FALSZYWY_ZASOB_LOG:-}" ]; then
  if [ -e "${FALSZYWY_SCIEZKA_ZASOBU:-/nie/ma/takiej/sciezki}" ]; then stan=jest; else stan=brak; fi
  echo "npm $* zasob=$stan" >> "$FALSZYWY_ZASOB_LOG"
fi
exit "${FALSZYWY_NPM_KOD:-0}"
"""

FALSZYWY_PYTHON = r"""#!/bin/sh
zasob() {
  if [ -e "${FALSZYWY_SCIEZKA_ZASOBU:-/nie/ma/takiej/sciezki}" ]; then echo jest; else echo brak; fi
}
case "$1" in
  -c)
    case "$2" in
      # Wolny port demo: jednolinijkowiec skryptu idzie do prawdziwego pythona (prawdziwy bind).
      *socket*)
        if [ "${FALSZYWY_WYNIK_PORTU+x}" = x ]; then printf '%s' "$FALSZYWY_WYNIK_PORTU"; exit 0; fi
        exec "$FALSZYWY_PRAWDZIWY_PYTHON" -c "$2" ;;
      *asyncpg*) echo 7000000000000000001 ;;
    esac
    exit 0 ;;
  -m)
    case "$2" in
      playwright)
        if [ -n "${FALSZYWY_ZASOB_LOG:-}" ]; then echo "playwright zasob=$(zasob)" >> "$FALSZYWY_ZASOB_LOG"; fi
        exit 0 ;;
      pytest)
        shift 2
        kind=e2e
        # HAR-236: przebieg e2e tez idzie z `-n`, wiec `-m e2e` rozpoznajemy przed `-n 4`.
        case "$*" in
          *"-m e2e"*) kind=e2e ;;
          *"-n 4"*) kind=pel ;;
          *"baza and not e2e"*) kind=baza ;;
        esac
        tag="${FALSZYWY_TAG:-x}"
        nazwa_bazy="${COCKPIT_TEST_DATABASE_URL##*/}"
        if [ -n "${FALSZYWY_ARGV_PYTESTA:-}" ]; then echo "$kind $*" >> "$FALSZYWY_ARGV_PYTESTA"; fi
        if [ -n "${FALSZYWY_URL_BAZY:-}" ]; then echo "$COCKPIT_TEST_DATABASE_URL" >> "$FALSZYWY_URL_BAZY"; fi
        if [ -n "${FALSZYWY_ZASOB_LOG:-}" ]; then echo "pytest $kind zasob=$(zasob)" >> "$FALSZYWY_ZASOB_LOG"; fi
        if [ -n "${FALSZYWY_BAZA_ISTNIEJE:-}" ]; then
          if [ -e "$FALSZYWY_STAN_BAZ/$nazwa_bazy" ]; then
            echo "$kind $nazwa_bazy istnieje" >> "$FALSZYWY_BAZA_ISTNIEJE"
          else
            echo "$kind $nazwa_bazy brak" >> "$FALSZYWY_BAZA_ISTNIEJE"
          fi
        fi
        if [ -n "${FALSZYWY_PORT_DEMO:-}" ]; then echo "$kind ${COCKPIT_DEMO_PORT-brak}" >> "$FALSZYWY_PORT_DEMO"; fi
        if [ -n "${FALSZYWY_PID_SLOTU:-}" ] && [ "$kind" = pel ]; then
          for s in "" .2 .3; do
            if [ -f "$COCKPIT_TESTY_BLOKADA$s/pid" ]; then
              echo "slot${s:-.1} $(cat "$COCKPIT_TESTY_BLOKADA$s/pid")" >> "$FALSZYWY_PID_SLOTU"
            fi
          done
        fi
        # Workerzy e2e tworza swoje bazy <baza>_gw<n>; `xgw0` to baza spoza wzorca `<baza>\_gw%`,
        # a `_gw-1` pasuje do wzorca, ale ma znak spoza [a-z0-9_] (skrypt takiej nazwy nie usuwa).
        if [ "$kind" = e2e ] && [ -n "${FALSZYWY_TWORZ_GW:-}" ]; then
          : > "$FALSZYWY_STAN_BAZ/${nazwa_bazy}_gw0"
          : > "$FALSZYWY_STAN_BAZ/${nazwa_bazy}_gw1"
          : > "$FALSZYWY_STAN_BAZ/${nazwa_bazy}xgw0"
          : > "$FALSZYWY_STAN_BAZ/${nazwa_bazy}_gw-1"
        fi
        if [ "$kind" = e2e ] && [ -n "${FALSZYWY_PSUJ_DROP:-}" ]; then : > "$FALSZYWY_FLAGA_DROP"; fi
        if [ -n "${FALSZYWY_DZIENNIK:-}" ]; then echo "start $tag $kind" >> "$FALSZYWY_DZIENNIK"; fi
        # Spotkanie dwoch przebiegow bez zegara: kazdy czeka na start drugiego (do 900 prob).
        if [ -n "${FALSZYWY_SPOTKANIE:-}" ] && [ "$kind" = pel ]; then
          : > "$FALSZYWY_SPOTKANIE/$tag"
          i=0
          while [ ! -e "$FALSZYWY_SPOTKANIE/$FALSZYWY_DRUGI_TAG" ]; do
            sleep 0.1
            i=$((i + 1))
            [ "$i" -le 900 ] || exit 97
          done
        fi
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
    pid: int = 0

    @property
    def calosc(self) -> str:
        return self.stdout + self.stderr


def _zapisz_narzedzie(sciezka: Path, tresc: str) -> None:
    sciezka.write_text(tresc)
    sciezka.chmod(0o755)


def _srodowisko(
    tmp_path: Path, *, blokada: Path | None | bool = True, **dodatkowe: str | None
) -> dict[str, str]:
    """Srodowisko skryptu: falszywe narzedzia, wlasna blokada w tmp_path, bez COCKPIT_TESTY_*.
    `blokada=False` nie ustawia COCKPIT_TESTY_BLOKADA (test sciezki domyslnej)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _zapisz_narzedzie(bin_dir / "docker", FALSZYWY_DOCKER)
    _zapisz_narzedzie(bin_dir / "npm", FALSZYWY_NPM)
    _zapisz_narzedzie(bin_dir / "fake_python", FALSZYWY_PYTHON)
    (tmp_path / "stan_baz").mkdir(exist_ok=True)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("COCKPIT_TESTY_")
        and k not in ("COCKPIT_TEST_DATABASE_URL", "COCKPIT_E2E_PROCESY", "COCKPIT_DEMO_PORT")
    }
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["PYTHON"] = str(bin_dir / "fake_python")
    env["COCKPIT_PG_PORT"] = PORT
    env["FALSZYWY_DZIENNIK_DOCKERA"] = str(tmp_path / "docker.log")
    env["FALSZYWY_ZASOB_LOG"] = str(tmp_path / "zasob.log")
    env["FALSZYWY_STAN_BAZ"] = str(tmp_path / "stan_baz")
    env["FALSZYWY_FLAGA_DROP"] = str(tmp_path / "flaga_drop")
    env["FALSZYWY_PRAWDZIWY_PYTHON"] = sys.executable
    # Jeden slot: dotychczasowe testy blokady opisuja serializacje (slot 1 = $BLOKADA). Semafor
    # z domyslna liczba slotow testuja osobne testy (HAR-236, punkt 7), ktore ustawiaja zmienna.
    env["COCKPIT_TESTY_SLOTY"] = "1"
    if blokada is True:
        env["COCKPIT_TESTY_BLOKADA"] = str(tmp_path / "blokada")
    elif isinstance(blokada, Path):
        env["COCKPIT_TESTY_BLOKADA"] = str(blokada)
    if "COCKPIT_TESTY_BLOKADA" in env:
        env["FALSZYWY_SCIEZKA_ZASOBU"] = f"{env['COCKPIT_TESTY_BLOKADA']}.zasob"
    for nazwa, wartosc in dodatkowe.items():
        if wartosc is None:
            env.pop(nazwa, None)
        else:
            env[nazwa] = wartosc
    return env


def _start(
    env: dict[str, str], skrypt: Path = SKRYPT, cwd: Path = KORZEN
) -> tuple[subprocess.Popen, float]:
    proc = uruchom_proces_sync(
        ["/bin/bash", str(skrypt)],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc, time.monotonic()


def _koniec(proc: subprocess.Popen, start: float, nazwa: str = "testy_pelne.sh") -> Wynik:
    try:
        out, err = proc.communicate(timeout=LIMIT_PROCESU)
        return Wynik(proc.returncode, out, err, time.monotonic() - start, proc.pid)
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


def _dwa_rownolegle(
    tmp_path: Path, blokada_a: Path, blokada_b: Path, *, spotkanie: bool = False, **dodatkowe: str
) -> tuple[Wynik, Wynik]:
    """Dwa przebiegi naraz. `spotkanie=True`: kazdy pierwszy przebieg pytest czeka na start drugiego
    (zdarzenie, nie czas), wiec przebiegi MUSZA sie nakladac, a gdy skrypt je serializuje, spotkanie
    sie nie odbedzie (kod 97 falszywego pytesta)."""
    wspolne = {"FALSZYWY_DZIENNIK": str(tmp_path / "pytest.log"), **dodatkowe}
    if spotkanie:
        (tmp_path / "spotkanie").mkdir(exist_ok=True)
        wspolne["FALSZYWY_SPOTKANIE"] = str(tmp_path / "spotkanie")
    else:
        wspolne["FALSZYWY_PYTEST_SEN"] = "0.6"
    drugi = {"A": "B", "B": "A"}
    env_a = _srodowisko(
        tmp_path, blokada=blokada_a, FALSZYWY_TAG="A", FALSZYWY_DRUGI_TAG=drugi["A"], **wspolne
    )
    env_b = _srodowisko(
        tmp_path, blokada=blokada_b, FALSZYWY_TAG="B", FALSZYWY_DRUGI_TAG=drugi["B"], **wspolne
    )
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
    asercja z testu powyzej moze oblac. Nakladanie wymusza spotkanie, nie czas."""
    a, b = _dwa_rownolegle(
        tmp_path, tmp_path / "blokada_a", tmp_path / "blokada_b", spotkanie=True
    )
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
        "inne przebiegi testow trwaja, ponow pozniej albo zwieksz COCKPIT_TESTY_LIMIT_BLOKADY"
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
    assert not _zasob(tmp_path / "blokada").exists()


def test_po_przebiegu_z_porazka_testow_blokada_jest_zwolniona(tmp_path):
    wynik = _uruchom(_srodowisko(tmp_path, FALSZYWY_PYTEST_PADA_W="baza"))
    assert wynik.kod == 1, wynik.stderr
    assert not (tmp_path / "blokada").exists()


def test_po_nieudanym_starcie_bazy_blokada_i_zasob_sa_zwolnione(tmp_path):
    # Skrypt konczy sie w trakcie operacji na wspolnym zasobie: zwolnic je musi trap.
    wynik = _uruchom(_srodowisko(tmp_path, FALSZYWY_DOCKER_UP_KOD="17"))
    assert wynik.kod == 1, wynik.stderr
    assert not (tmp_path / "blokada").exists()
    assert not _zasob(tmp_path / "blokada").exists()


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
    # Przebieg, ktory nie zdazyl utworzyc bazy, nie sprzata cudzej: zadnego zapytania do serwera.
    assert not [p for p in polecenia if p.startswith("compose exec")], polecenia


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

# Zapisuje kazde wywolanie. Gdy ustawiono FALSZYWY_ZWOLNIJ_PO_SLEEPIE=<n>, po n-tym wywolaniu usuwa
# katalog FALSZYWY_ZWOLNIJ_KATALOG: odtwarza zwolnienie blokady przez inny przebieg w trakcie czekania
# bez zegara (n-ta sekunda czekania jest zdarzeniem, nie uplywem czasu).
FALSZYWY_SLEEP = """#!/bin/sh
echo "$*" >> "$FALSZYWY_DZIENNIK_SLEEP"
if [ -n "${FALSZYWY_ZWOLNIJ_PO_SLEEPIE:-}" ]; then
  n="$(grep -c '' "$FALSZYWY_DZIENNIK_SLEEP")"
  if [ "$n" -eq "$FALSZYWY_ZWOLNIJ_PO_SLEEPIE" ]; then rm -rf "$FALSZYWY_ZWOLNIJ_KATALOG"; fi
fi
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


def _zasob(blokada: Path) -> Path:
    """Katalog krotkiej blokady wspolnego zasobu (HAR-236)."""
    return Path(f"{blokada}.zasob")


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
    # Nieudane `mkdir` (blokada i przejecie istnieja) jest cicha probe: bez bledu powloki na stderr.
    assert "mkdir" not in wynik.stderr
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
    # martwy, zywy (ponowne) i koniec: kod 3 ("ponow od razu") tylko po faktycznym usunieciu blokady,
    # inaczej trzecie sprawdzenie (ps) bylo by po zbednym ponowieniu bez usuniecia czegokolwiek.
    assert licznik == ["x"] * 2
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
        "lsof", "env", "sh", "cksum", "cut", "awk", "sed", "basename", "ls", "head", "wc",
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
        # Start przebiegu to zdarzenie (wpis w dzienniku), nie upyw czasu: limit jest tylko
        # zabezpieczeniem przed zawieszeniem, wiec obciazona maszyna nie oblewa testu.
        koniec = time.monotonic() + LIMIT_STARTU_PRZEBIEGU
        while not (dziennik.exists() and "start" in dziennik.read_text()):
            assert time.monotonic() < koniec, (
                f"przebieg testow nie ruszyl w limicie {LIMIT_STARTU_PRZEBIEGU} s"
            )
            time.sleep(0.05)
        assert (blokada / "pid").read_text().strip() == str(proc.pid)  # przebieg trzyma blokade
        # Sam SIGTERM, bez eskalacji do SIGKILL; po koncu procesu trap EXIT juz zwolnil blokade.
        przerwij_sigtermem_sync(proc, nazwa="przerywany przebieg")
        proc.communicate(timeout=LIMIT_STARTU_PRZEBIEGU)
    finally:
        zakoncz_proces_sync(proc, nazwa="przerywany przebieg")
    assert not blokada.exists()
    assert proc.returncode == 143  # trap TERM -> exit 143 -> sprzatnij, nie smierc od sygnalu


# --- runda 3, HAR-151: galaz sprzatania przejecia i krok licznika limitu -----------------

# Falszywe `rm` zglasza start (plik) i czeka na zwolnienie przez test (plik): skrypt stoi w usuwaniu
# martwej blokady, czyli miedzy utworzeniem katalogu przejecia a jego zwolnieniem w galezi glownej.
# Nie usuwa niczego (kod 1). Zdarzenia, nie czas; 900 prob po 0,1 s to tylko zabezpieczenie.
FALSZYWY_RM_WISI = """#!/bin/sh
: > "$FALSZYWY_RM_START"
i=0
while [ ! -e "$FALSZYWY_RM_DALEJ" ]; do
  sleep 0.1
  i=$((i + 1))
  [ "$i" -le 900 ] || exit 97
done
exit 1
"""


@pytest.mark.parametrize(
    "przejecie_zniknelo",
    [
        pytest.param(False, id="katalog_przejecia_istnieje"),
        # Katalog przejecia zniknal w miedzyczasie (konkurent): `rmdir` w trapie zawodzi, a zawiedzenie
        # nie moze ani zmienic kodu 143, ani wypisac bledu rmdir.
        pytest.param(True, id="katalog_przejecia_zniknal"),
    ],
)
def test_przebieg_przerwany_w_trakcie_przejmowania_zwalnia_blokade_przejecia(
    tmp_path, przejecie_zniknelo
):
    """SIGTERM w srodku usuwania martwej blokady: powloka obsluguje go zaraz po zakonczeniu `rm`,
    zanim galaz glowna zwolni przejecie, wiec katalog `.przejecie` usuwa tylko `sprzatnij` (trap
    EXIT). Sygnal idzie tylko do skryptu (jak `kill <pid>`): sygnal do calej grupy zabilby tez `rm`,
    a powloka pomija wtedy obsluge sygnalu i dokancza galaz glowna."""
    blokada = tmp_path / "blokada"
    blokada.mkdir()
    martwy = _martwy_pid()
    (blokada / "pid").write_text(f"{martwy}\n")
    start_rm = tmp_path / "rm_start"
    dalej_rm = tmp_path / "rm_dalej"
    env = _srodowisko(
        tmp_path, FALSZYWY_RM_START=str(start_rm), FALSZYWY_RM_DALEJ=str(dalej_rm)
    )
    _zapisz_narzedzie(tmp_path / "bin" / "rm", FALSZYWY_RM_WISI)
    proc, _ = _start(env)
    try:
        koniec = time.monotonic() + LIMIT_STARTU_PRZEBIEGU
        while not start_rm.exists():
            assert time.monotonic() < koniec, (
                f"usuwanie martwej blokady nie ruszylo w limicie {LIMIT_STARTU_PRZEBIEGU} s"
            )
            time.sleep(0.05)
        assert _przejecie(blokada).is_dir()  # skrypt jest w srodku przejmowania
        if przejecie_zniknelo:
            _przejecie(blokada).rmdir()
        zabij_proces(proc.pid, signal.SIGTERM)  # tylko skrypt; `rm` czeka dalej na test
        dalej_rm.write_text("")
        _, stderr = proc.communicate(timeout=LIMIT_STARTU_PRZEBIEGU)
    finally:
        zakoncz_proces_sync(proc, nazwa="przerywane przejmowanie")
    assert proc.returncode == 143
    assert f"Przejmuje blokade martwego procesu PID {martwy}: {blokada}" in stderr
    assert "rmdir" not in stderr
    assert (blokada / "pid").is_file()  # usuniecie nie dobieglo konca
    assert not _przejecie(blokada).exists()


# Falszywe `rm` usuwa to, o co poproszono, i zabiera tez katalog przejecia: konkurent konczy
# swoje przejecie, zanim skrypt zwolni je w galezi glownej.
FALSZYWY_RM_ZABIERA_PRZEJECIE = """#!/bin/sh
/bin/rm "$@"
kod=$?
/bin/rmdir "$2.przejecie" 2>/dev/null
exit $kod
"""


def test_katalog_przejecia_ktory_zniknal_w_trakcie_przejmowania_nie_wypisuje_bledu_rmdir(tmp_path):
    blokada = tmp_path / "blokada"
    blokada.mkdir()
    martwy = _martwy_pid()
    (blokada / "pid").write_text(f"{martwy}\n")
    env = _srodowisko(tmp_path)
    _zapisz_narzedzie(tmp_path / "bin" / "rm", FALSZYWY_RM_ZABIERA_PRZEJECIE)
    wynik = _uruchom(env)
    assert wynik.kod == 0, wynik.stderr
    assert f"Przejmuje blokade martwego procesu PID {martwy}: {blokada}\n" in wynik.stderr
    assert "rmdir" not in wynik.stderr
    assert not blokada.exists() and not _przejecie(blokada).exists()


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


# --- HAR-186: osobna baza testowa na worktree ---------------------------------------------


def _korzen_z_kopia_skryptu(tmp_path: Path, nazwa: str) -> Path:
    """Udaje inny worktree: kopia skryptu w `<korzen>/tools/` i pusty katalog `web/`."""
    korzen = (tmp_path / nazwa).resolve()
    (korzen / "tools").mkdir(parents=True)
    (korzen / "web").mkdir()
    shutil.copy(SKRYPT, korzen / "tools" / "testy_pelne.sh")
    return korzen


def _przebieg_w_korzeniu(
    tmp_path: Path, korzen: Path, etykieta: str, *, dostep: Path | None = None, **dodatkowe: str
) -> tuple[Wynik, list[str], list[str]]:
    """Pelny przebieg skryptu z `korzen`; zwraca wynik, URL-e przekazane pytestowi (po
    jednym na przebieg) i polecenia dockera. Dzienniki sa osobne dla kazdej etykiety.
    `dostep` = sciezka do tego samego korzenia (np. dowiazanie), z ktorej startuje skrypt."""
    url_log = tmp_path / f"url_{etykieta}.log"
    docker_log = tmp_path / f"docker_{etykieta}.log"
    env = _srodowisko(
        tmp_path,
        blokada=tmp_path / f"blokada_{etykieta}",
        FALSZYWY_URL_BAZY=str(url_log),
        FALSZYWY_DZIENNIK_DOCKERA=str(docker_log),
        **dodatkowe,
    )
    wejscie = dostep if dostep is not None else korzen
    proc, start = _start(env, wejscie / "tools" / "testy_pelne.sh", wejscie)
    wynik = _koniec(proc, start)
    urle = url_log.read_text().splitlines() if url_log.exists() else []
    docker = docker_log.read_text().splitlines() if docker_log.exists() else []
    return wynik, urle, docker


def _nazwa_bazy(url: str) -> str:
    return url.rsplit("/", 1)[-1]


def _skrot_sciezki(korzen: Path) -> str:
    """Pierwsze pole `cksum` ze sciezki korzenia (bez konca linii) - jak w skrypcie."""
    proc = uruchom_proces_sync(
        ["cksum"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out, _ = proc.communicate(str(korzen), timeout=LIMIT_PROCESU)
    finally:
        zakoncz_proces_sync(proc, nazwa="cksum")
    return out.split()[0]


def _stan_baz(tmp_path: Path) -> set[str]:
    """Bazy na atrapie serwera PostgreSQL (pliki w `stan_baz`)."""
    return {p.name for p in (tmp_path / "stan_baz").iterdir()}


def _dodaj_baze(tmp_path: Path, nazwa: str) -> None:
    # Katalog stanu powstaje zwykle w `_srodowisko`, ale test zasiewa bazy przed jego wywolaniem.
    (tmp_path / "stan_baz").mkdir(exist_ok=True)
    (tmp_path / "stan_baz" / nazwa).write_text("")


def _nazwa_bazy_przebiegu(korzen: Path, pid: int) -> str:
    return f"cockpit_test_{_skrot_sciezki(korzen)}_{pid}"


def _polecenia_sql(docker: list[str], rodzaj: str) -> list[str]:
    """Polecenia `compose exec ... -c <SQL>` z dziennika dockera, w ktorych SQL zaczyna sie od `rodzaj`."""
    return [p for p in docker if p.startswith("compose exec") and f"-c {rodzaj}" in p]


def test_dwa_worktree_o_roznych_sciezkach_dostaja_rozne_bazy_testowe(tmp_path):
    a = _korzen_z_kopia_skryptu(tmp_path, "a")
    b = _korzen_z_kopia_skryptu(tmp_path, "b")
    wynik_a, urle_a, _ = _przebieg_w_korzeniu(tmp_path, a, "a")
    wynik_b, urle_b, _ = _przebieg_w_korzeniu(tmp_path, b, "b")
    assert (wynik_a.kod, wynik_b.kod) == (0, 0), (wynik_a.stderr, wynik_b.stderr)
    assert len(urle_a) == 3 and len(urle_b) == 3
    assert len(set(urle_a)) == 1 and len(set(urle_b)) == 1
    nazwa_a, nazwa_b = _nazwa_bazy(urle_a[0]), _nazwa_bazy(urle_b[0])
    assert re.fullmatch(r"cockpit_test_[0-9]+_[0-9]+", nazwa_a), nazwa_a
    assert re.fullmatch(r"cockpit_test_[0-9]+_[0-9]+", nazwa_b), nazwa_b
    # Czesc ze skrotu sciezki jest rozna (pid tez, wiec to osobna asercja: skrot to worktree).
    assert nazwa_a.split("_")[2] != nazwa_b.split("_")[2]


def test_ten_sam_worktree_dostaje_przy_kazdym_przebiegu_inna_baze_o_tym_samym_skrocie(tmp_path):
    korzen = _korzen_z_kopia_skryptu(tmp_path, "a")
    pierwszy, urle_1, _ = _przebieg_w_korzeniu(tmp_path, korzen, "1")
    drugi, urle_2, _ = _przebieg_w_korzeniu(tmp_path, korzen, "2")
    assert (pierwszy.kod, drugi.kod) == (0, 0), (pierwszy.stderr, drugi.stderr)
    nazwa_1, nazwa_2 = _nazwa_bazy(urle_1[0]), _nazwa_bazy(urle_2[0])
    assert nazwa_1 == _nazwa_bazy_przebiegu(korzen, pierwszy.pid)
    assert nazwa_2 == _nazwa_bazy_przebiegu(korzen, drugi.pid)
    assert nazwa_1 != nazwa_2


def test_nazwa_bazy_przebiegu_to_skrot_cksum_sciezki_korzenia_i_pid_skryptu(tmp_path):
    korzen = _korzen_z_kopia_skryptu(tmp_path, "a")
    wynik, urle, _ = _przebieg_w_korzeniu(tmp_path, korzen, "a")
    assert wynik.kod == 0, wynik.stderr
    nazwa = _nazwa_bazy_przebiegu(korzen, wynik.pid)
    assert urle == [f"postgresql+asyncpg://cockpit:cockpit@localhost:{PORT}/{nazwa}"] * 3
    assert f"Baza testowa tego przebiegu: {nazwa} (usuwana na koniec przebiegu)\n" in wynik.stderr


def test_jawny_url_bazy_ma_pierwszenstwo_a_skrypt_nie_tworzy_ani_nie_usuwa_baz(tmp_path):
    korzen = _korzen_z_kopia_skryptu(tmp_path, "a")
    jawny = "postgresql+asyncpg://u:p@localhost:54329/moja_baza"
    # Pozostalosc po martwym przebiegu tego worktree: przy jawnym URL skrypt jej nie sprzata.
    stara = f"{_nazwa_bazy_przebiegu(korzen, _martwy_pid())}"
    _dodaj_baze(tmp_path, stara)
    wynik, urle, docker = _przebieg_w_korzeniu(
        tmp_path, korzen, "a", COCKPIT_TEST_DATABASE_URL=jawny, FALSZYWY_TWORZ_GW="1"
    )
    assert wynik.kod == 0, wynik.stderr
    assert urle == [jawny] * 3
    assert "Baza testowa tego przebiegu" not in wynik.stderr
    assert not [p for p in docker if "CREATE DATABASE cockpit_test" in p], docker
    assert not [p for p in docker if "CREATE DATABASE \"" in p], docker
    assert not [p for p in docker if "DROP DATABASE" in p], docker
    assert not [p for p in docker if "datname LIKE" in p], docker
    # Bazy workerow e2e `moja_baza_gw*` nie sa bazami skryptu: zostaja.
    assert _stan_baz(tmp_path) == {
        stara,
        "moja_baza_gw0",
        "moja_baza_gw1",
        "moja_bazaxgw0",
        "moja_baza_gw-1",
    }


def test_baza_przebiegu_jest_tworzona_przed_testami_i_usunieta_po_zielonym_przebiegu(tmp_path):
    korzen = _korzen_z_kopia_skryptu(tmp_path, "a")
    nazwa_log = tmp_path / "baza_istnieje.log"
    wynik, _, docker = _przebieg_w_korzeniu(
        tmp_path, korzen, "a", FALSZYWY_BAZA_ISTNIEJE=str(nazwa_log)
    )
    assert wynik.kod == 0, wynik.stderr
    nazwa = _nazwa_bazy_przebiegu(korzen, wynik.pid)
    # Kazdy z trzech przebiegow pytest zastaje baze na serwerze...
    assert nazwa_log.read_text().splitlines() == [
        f"pel {nazwa} istnieje",
        f"baza {nazwa} istnieje",
        f"e2e {nazwa} istnieje",
    ]
    tworzenie = _polecenia_sql(docker, "CREATE DATABASE \"cockpit_test")
    assert len(tworzenie) == 1, docker
    assert tworzenie[0].endswith(f"-c CREATE DATABASE \"{nazwa}\" OWNER cockpit"), tworzenie
    # ... a po przebiegu jej juz nie ma.
    assert nazwa not in _stan_baz(tmp_path)


@pytest.mark.parametrize("przebieg", ["pel", "baza", "e2e"])
def test_po_czerwonych_testach_baza_przebiegu_jest_usunieta_a_kod_zostaje_1(tmp_path, przebieg):
    korzen = _korzen_z_kopia_skryptu(tmp_path, "a")
    wynik, _, docker = _przebieg_w_korzeniu(
        tmp_path, korzen, "a", FALSZYWY_PYTEST_PADA_W=przebieg
    )
    assert wynik.kod == 1, wynik.stderr
    nazwa = _nazwa_bazy_przebiegu(korzen, wynik.pid)
    assert nazwa not in _stan_baz(tmp_path)
    assert [p for p in docker if p.endswith(f'-c DROP DATABASE IF EXISTS "{nazwa}" WITH (FORCE)')]


def test_po_bledzie_budowy_frontu_baza_przebiegu_jest_usunieta(tmp_path):
    # Baza istnieje juz po starcie, a skrypt pada dalej (npm): usuwa ja trap EXIT.
    korzen = _korzen_z_kopia_skryptu(tmp_path, "a")
    wynik, urle, _ = _przebieg_w_korzeniu(tmp_path, korzen, "a", FALSZYWY_NPM_KOD="1")
    assert wynik.kod == 1, wynik.stderr
    assert urle == []  # testy nie ruszyly
    assert _stan_baz(tmp_path) == set()


def test_po_sigterm_baza_przebiegu_jest_usunieta_a_slot_innego_przebiegu_zostaje(tmp_path):
    korzen = _korzen_z_kopia_skryptu(tmp_path, "a")
    blokada = tmp_path / "blokada_a"
    dziennik = tmp_path / "pytest.log"
    # Domyslne dwa sloty: slot 1 trzyma inny przebieg, ten bierze slot 2.
    env = _srodowisko(
        tmp_path,
        blokada=blokada,
        COCKPIT_TESTY_SLOTY=None,
        FALSZYWY_DZIENNIK=str(dziennik),
        FALSZYWY_PYTEST_SEN="30",
        FALSZYWY_TAG="S",
    )
    with _zywy_wlasciciel(blokada) as inny:
        proc, _ = _start(env, korzen / "tools" / "testy_pelne.sh", korzen)
        try:
            koniec = time.monotonic() + LIMIT_STARTU_PRZEBIEGU
            while not (dziennik.exists() and "start" in dziennik.read_text()):
                assert time.monotonic() < koniec, "przebieg testow nie ruszyl w limicie"
                time.sleep(0.05)
            nazwa = _nazwa_bazy_przebiegu(korzen, proc.pid)
            assert nazwa in _stan_baz(tmp_path)  # baza istnieje, gdy testy trwaja
            assert (Path(f"{blokada}.2") / "pid").read_text().strip() == str(proc.pid)
            przerwij_sigtermem_sync(proc, nazwa="przerywany przebieg")
            proc.communicate(timeout=LIMIT_STARTU_PRZEBIEGU)
        finally:
            zakoncz_proces_sync(proc, nazwa="przerywany przebieg")
        assert proc.returncode == 143
        assert nazwa not in _stan_baz(tmp_path)
        assert not Path(f"{blokada}.2").exists()  # wlasny slot zwolniony
        assert (blokada / "pid").read_text().strip() == str(inny)  # cudzy slot nietkniety


def test_bazy_workerow_e2e_sa_usuwane_razem_z_baza_przebiegu_ale_nie_bazy_spoza_wzorca(tmp_path):
    korzen = _korzen_z_kopia_skryptu(tmp_path, "a")
    obca = "cockpit_test_inny_worktree_1"
    _dodaj_baze(tmp_path, obca)
    wynik, _, docker = _przebieg_w_korzeniu(
        tmp_path, korzen, "a", FALSZYWY_TWORZ_GW="1", COCKPIT_E2E_PROCESY="2"
    )
    assert wynik.kod == 0, wynik.stderr
    nazwa = _nazwa_bazy_przebiegu(korzen, wynik.pid)
    for usuwana in (nazwa, f"{nazwa}_gw0", f"{nazwa}_gw1"):
        assert f'-c DROP DATABASE IF EXISTS "{usuwana}" WITH (FORCE)' in "\n".join(
            p for p in docker if p.startswith("compose exec")
        ), (usuwana, docker)
    # `<baza>xgw0` pasowalaby do `<baza>_gw%` tylko gdyby `_` w LIKE nie byl znakiem (bez `\_`);
    # `<baza>_gw-1` pasuje do wzorca, ale nazwa ze znakiem spoza [a-z0-9_] nie jest usuwana.
    assert _stan_baz(tmp_path) == {obca, f"{nazwa}xgw0", f"{nazwa}_gw-1"}


@pytest.mark.parametrize(
    ("pada_w", "oczekiwany_kod"),
    [
        pytest.param(None, 0, id="zielony_przebieg_zostaje_zielony"),
        pytest.param("baza", 1, id="czerwony_przebieg_zostaje_czerwony"),
    ],
)
def test_nieudane_usuniecie_bazy_przebiegu_jest_zglaszane_ale_nie_zmienia_kodu_wyjscia(
    tmp_path, pada_w, oczekiwany_kod
):
    korzen = _korzen_z_kopia_skryptu(tmp_path, "a")
    wynik, _, _ = _przebieg_w_korzeniu(
        tmp_path,
        korzen,
        "a",
        FALSZYWY_PSUJ_DROP="1",
        FALSZYWY_DROP_PADA_DLA="cockpit_test_*",
        FALSZYWY_PYTEST_PADA_W=pada_w,
    )
    nazwa = _nazwa_bazy_przebiegu(korzen, wynik.pid)
    assert wynik.kod == oczekiwany_kod, wynik.stderr
    assert (
        f'BLAD: nie udalo sie usunac bazy przebiegu {nazwa} (usun ja recznie: DROP DATABASE "{nazwa}")\n'
    ) in wynik.stderr
    assert nazwa in _stan_baz(tmp_path)  # drop naprawde sie nie udal


@pytest.mark.parametrize(
    "psucie",
    [
        pytest.param({"FALSZYWY_DROP_PADA_DLA": "*_gw0"}, id="drop_bazy_workera"),
        # Samo zawiedzenie listy: usuniecia (takze bazy przebiegu) udaja sie, wiec baze przebiegu
        # zostawia tylko przerwanie sprzatania po bledzie listy.
        pytest.param(
            {"FALSZYWY_LISTA_PADA": "1", "FALSZYWY_DROP_PADA_DLA": "nie_ma_takiej_bazy"},
            id="lista_baz_workerow",
        ),
    ],
)
def test_gdy_nie_da_sie_usunac_baz_workerow_baza_przebiegu_nie_jest_usuwana_i_jest_blad(
    tmp_path, psucie
):
    # Usuniecie bazy przebiegu przy zywych bazach workerow wymaga wczesniejszego usuniecia workerow;
    # blad na tym etapie konczy sprzatanie (i zostawia baze do recznego usuniecia).
    korzen = _korzen_z_kopia_skryptu(tmp_path, "a")
    wynik, _, _ = _przebieg_w_korzeniu(
        tmp_path,
        korzen,
        "a",
        FALSZYWY_PSUJ_DROP="1",
        FALSZYWY_TWORZ_GW="1",
        COCKPIT_E2E_PROCESY="1",
        **psucie,
    )
    nazwa = _nazwa_bazy_przebiegu(korzen, wynik.pid)
    assert wynik.kod == 0, wynik.stderr
    assert f"BLAD: nie udalo sie usunac bazy przebiegu {nazwa} " in wynik.stderr
    assert nazwa in _stan_baz(tmp_path)


def test_sprzatanie_usuwa_bazy_martwych_przebiegow_tego_worktree_i_tylko_je(tmp_path):
    korzen = _korzen_z_kopia_skryptu(tmp_path, "a")
    skrot = _skrot_sciezki(korzen)
    martwy_1, martwy_2 = _martwy_pid(), _martwy_pid()
    with _zywy_wlasciciel(tmp_path / "cudzy_zywy") as zywy:
        do_usuniecia = {
            f"cockpit_test_{skrot}_{martwy_1}",
            f"cockpit_test_{skrot}_{martwy_2}_gw0",
            f"cockpit_test_{skrot}_{martwy_2}_gw0_x",  # sufiks <PID>_gw* z dalszym podkreslnikiem
        }
        zostaja = {
            f"cockpit_test_{skrot}_{zywy}",  # PID zyje (inny przebieg tego worktree)
            f"cockpit_test_{skrot}_{zywy}_gw1",
            f"cockpit_test_{skrot}_{martwy_1}_gw-1",  # pasuje do <PID>_gw*, ale ma znak spoza [a-z0-9_]
            f"cockpit_test_{skrot}_{martwy_1}_inne",  # sufiks inny niz <PID> i <PID>_gw*
            f"cockpit_test_{skrot}_abc",  # sufiks bez PID
            f"cockpit_test_{skrot}_",  # prefiks ze znakiem `_` i pusty sufiks (pusty PID)
            f"cockpit_test_{skrot}",  # baza starego typu, bez sufiksu
            f"cockpit_test_{skrot}9_{martwy_1}",  # skrot innego worktree zaczynajacy sie tak samo
            f"cockpit_test_123_{martwy_1}",  # inny worktree
            "cockpit_test",
            "cockpit",
        }
        for nazwa in do_usuniecia | zostaja:
            _dodaj_baze(tmp_path, nazwa)
        wynik, _, _ = _przebieg_w_korzeniu(tmp_path, korzen, "a")
    assert wynik.kod == 0, wynik.stderr
    assert _stan_baz(tmp_path) == zostaja
    assert f"Usuwam baze po martwym przebiegu PID {martwy_1}: cockpit_test_{skrot}_{martwy_1}\n" in wynik.stderr
    assert (
        f"Usuwam baze po martwym przebiegu PID {martwy_2}: cockpit_test_{skrot}_{martwy_2}_gw0\n"
        in wynik.stderr
    )
    assert (
        f"Usuwam baze po martwym przebiegu PID {martwy_2}: cockpit_test_{skrot}_{martwy_2}_gw0_x\n"
        in wynik.stderr
    )
    assert wynik.stderr.count("Usuwam baze po martwym przebiegu") == 3


def test_baza_po_martwym_przebiegu_o_tym_samym_pid_jest_usunieta_przed_utworzeniem_nowej(tmp_path):
    # PID wrocil do obiegu: baza `<prefiks>_<PID>` (i jej worker) istnieje, gdy skrypt tworzy swoja.
    # Atrapa serwera odrzuca CREATE istniejacej bazy, wiec skrypt musi najpierw usunac stara.
    korzen = _korzen_z_kopia_skryptu(tmp_path, "a")
    wynik, _, _ = _przebieg_w_korzeniu(
        tmp_path, korzen, "a", FALSZYWY_STARA_BAZA_SKROT=_skrot_sciezki(korzen)
    )
    assert wynik.kod == 0, wynik.stderr
    assert "already exists" not in wynik.stderr
    assert _stan_baz(tmp_path) == set()  # takze stary worker `_gw0` zniknal


@pytest.mark.parametrize("wartosc", ["", "abc 12"], ids=["pusty", "nieliczbowy"])
def test_skrot_sciezki_pusty_albo_nieliczbowy_to_blad_bez_tworzenia_bazy_i_testow(
    tmp_path, wartosc
):
    korzen = _korzen_z_kopia_skryptu(tmp_path, "a")
    (tmp_path / "bin").mkdir()
    _zapisz_narzedzie(
        tmp_path / "bin" / "cksum",
        f"#!/bin/sh\ncat >/dev/null\nprintf '%s' '{wartosc}'\n",
    )
    wynik, urle, docker = _przebieg_w_korzeniu(tmp_path, korzen, "a")
    assert wynik.kod == 1, wynik.stderr
    # Skrot to pierwsze pole wyniku `cksum`: z "abc 12" zostaje "abc".
    assert (
        f"BLAD: nie udalo sie wyliczyc skrotu sciezki korzenia repo ('{wartosc.split(' ')[0]}')\n"
    ) in wynik.stderr
    assert urle == []
    assert not [p for p in docker if "CREATE DATABASE \"cockpit_test" in p], docker
    assert not [p for p in docker if "DROP DATABASE" in p], docker
    assert not _zasob(tmp_path / "blokada_a").exists()  # trap zwolnil zasob
    assert "Traceback" not in wynik.calosc


def test_worktree_dostepny_przez_dowiazanie_dostaje_baze_ze_sciezki_fizycznej(tmp_path):
    korzen = _korzen_z_kopia_skryptu(tmp_path, "prawdziwy")
    dowiazanie = tmp_path / "skrot_do_worktree"
    dowiazanie.symlink_to(korzen, target_is_directory=True)
    assert _skrot_sciezki(dowiazanie) != _skrot_sciezki(korzen)  # test nie jest pusty
    wynik, urle, _ = _przebieg_w_korzeniu(tmp_path, korzen, "a", dostep=dowiazanie)
    assert wynik.kod == 0, wynik.stderr
    assert urle == [
        f"postgresql+asyncpg://cockpit:cockpit@localhost:{PORT}/"
        f"{_nazwa_bazy_przebiegu(korzen, wynik.pid)}"
    ] * 3


def test_pusty_jawny_url_bazy_jest_traktowany_jak_brak_i_dostaje_nazwe_wyliczona(tmp_path):
    korzen = _korzen_z_kopia_skryptu(tmp_path, "a")
    wynik, urle, _ = _przebieg_w_korzeniu(tmp_path, korzen, "a", COCKPIT_TEST_DATABASE_URL="")
    assert wynik.kod == 0, wynik.stderr
    assert urle == [
        f"postgresql+asyncpg://cockpit:cockpit@localhost:{PORT}/"
        f"{_nazwa_bazy_przebiegu(korzen, wynik.pid)}"
    ] * 3


# --- HAR-236: przebieg e2e na kilku procesach ------------------------------------------------


def _polecenia_przebiegow(tmp_path: Path) -> list[str]:
    """Wiersze `<rodzaj> <argumenty>` zapisane przez falszywy python, w kolejnosci wywolan."""
    sciezka = tmp_path / "argv_przebiegow.log"
    return sciezka.read_text().splitlines() if sciezka.exists() else []


def _env_z_dziennikiem_argv(tmp_path: Path, **dodatkowe: str) -> dict[str, str]:
    return _srodowisko(
        tmp_path, FALSZYWY_ARGV_PYTESTA=str(tmp_path / "argv_przebiegow.log"), **dodatkowe
    )


def test_przebieg_e2e_pelnego_zestawu_idzie_domyslnie_na_czterech_procesach(tmp_path):
    wynik = _uruchom(_env_z_dziennikiem_argv(tmp_path))
    assert wynik.kod == 0, wynik.stderr
    assert _polecenia_przebiegow(tmp_path) == [
        "pel -q -rs -n 4 -m not baza and not e2e",
        "baza -q -rs -m baza and not e2e",
        "e2e -m e2e -rs -n 4",
    ]


@pytest.mark.parametrize("procesy", ["1", "2", "7", "16"])
def test_liczba_procesow_przebiegu_e2e_pochodzi_ze_zmiennej_i_nie_zmienia_pozostalych_przebiegow(
    tmp_path, procesy
):
    wynik = _uruchom(_env_z_dziennikiem_argv(tmp_path, COCKPIT_E2E_PROCESY=procesy))
    assert wynik.kod == 0, wynik.stderr
    assert _polecenia_przebiegow(tmp_path) == [
        "pel -q -rs -n 4 -m not baza and not e2e",
        "baza -q -rs -m baza and not e2e",
        f"e2e -m e2e -rs -n {procesy}",
    ]


def test_pusta_zmienna_procesow_e2e_znaczy_wartosc_domyslna_cztery(tmp_path):
    wynik = _uruchom(_env_z_dziennikiem_argv(tmp_path, COCKPIT_E2E_PROCESY=""))
    assert wynik.kod == 0, wynik.stderr
    assert _polecenia_przebiegow(tmp_path)[-1] == "e2e -m e2e -rs -n 4"


@pytest.mark.parametrize("wartosc", ["0", "abc", "-1", "1.5", "4x"])
def test_zla_liczba_procesow_e2e_konczy_przebieg_kodem_1_zanim_cokolwiek_wystartuje(
    tmp_path, wartosc
):
    wynik = _uruchom(_env_z_dziennikiem_argv(tmp_path, COCKPIT_E2E_PROCESY=wartosc))
    assert wynik.kod == 1, wynik.stderr
    # Dokladnie jeden komunikat: bez dopisku `[: ...: integer expression expected` z powloki,
    # ktory pojawilby sie, gdyby napis nie-liczbowy doszedl do porownania `-ge`.
    assert wynik.stderr == (
        f"BLAD: COCKPIT_E2E_PROCESY musi byc liczba calkowita >= 1 (jest: '{wartosc}')\n"
    )
    assert "Traceback" not in wynik.calosc
    assert not (tmp_path / "docker.log").exists()  # nawet `docker compose version` nie ruszyl
    assert _polecenia_przebiegow(tmp_path) == []
    assert not (tmp_path / "blokada").exists()


# --- HAR-236, punkt 7: semafor pelnych przebiegow, krotka blokada zasobu, port demo ----------------


def _slot(blokada: Path, numer: int) -> Path:
    """Slot 1 = `$BLOKADA`, slot i > 1 = `$BLOKADA.<i>`."""
    return blokada if numer == 1 else Path(f"{blokada}.{numer}")


def _komunikat_zajetych(opis: str, pidy: list[int], katalogi: list[Path], limit: int) -> str:
    return (
        f"BLAD: blokada {opis} zajeta przez PID {', '.join(map(str, pidy))} "
        f"({', '.join(map(str, katalogi))}) - limit {limit} s minal; inne przebiegi testow trwaja, "
        "ponow pozniej albo zwieksz COCKPIT_TESTY_LIMIT_BLOKADY"
    )


def _dziennik_sleep(tmp_path: Path) -> Path:
    return tmp_path / "sleep.log"


def _srodowisko_bez_czekania(tmp_path: Path, **dodatkowe: str | None) -> dict[str, str]:
    """Srodowisko z falszywym `sleep` (zapis do dziennika zamiast czekania)."""
    env = _srodowisko(tmp_path, FALSZYWY_DZIENNIK_SLEEP=str(_dziennik_sleep(tmp_path)), **dodatkowe)
    _sleep_bez_czekania(tmp_path)
    return env


@pytest.mark.parametrize("sloty", [None, ""], ids=["nieustawione", "puste"])
def test_domyslnie_sa_dwa_sloty_drugi_przebieg_bierze_slot_2_i_nie_czeka(tmp_path, sloty):
    blokada = tmp_path / "blokada"
    env = _srodowisko_bez_czekania(
        tmp_path,
        COCKPIT_TESTY_SLOTY=sloty,
        COCKPIT_TESTY_LIMIT_BLOKADY="0",
        FALSZYWY_PID_SLOTU=str(tmp_path / "sloty.log"),
    )
    with _zywy_wlasciciel(blokada) as inny:
        wynik = _uruchom(env)
        assert wynik.kod == 0, wynik.stderr
        assert f"Blokada pelnego przebiegu zdobyta: {blokada}.2 (PID {wynik.pid})\n" in wynik.stderr
        # Podczas testow oba sloty maja wlascicieli: cudzy w slocie 1, ten przebieg w slocie 2.
        assert (tmp_path / "sloty.log").read_text().splitlines() == [
            f"slot.1 {inny}",
            f"slot.2 {wynik.pid}",
        ]
        assert "Czekam na blokade" not in wynik.stderr
        assert not _dziennik_sleep(tmp_path).exists()
        # Trap zwalnia tylko wlasny slot.
        assert not _slot(blokada, 2).exists()
        assert (blokada / "pid").read_text().strip() == str(inny)


def test_przy_jednym_slocie_drugi_przebieg_czeka_i_po_limicie_konczy_sie_kodem_75(tmp_path):
    blokada = tmp_path / "blokada"
    env = _srodowisko_bez_czekania(
        tmp_path, COCKPIT_TESTY_SLOTY="1", COCKPIT_TESTY_LIMIT_BLOKADY="1"
    )
    with _zywy_wlasciciel(blokada) as pid:
        wynik = _uruchom(env)
    assert wynik.kod == 75, wynik.stderr
    assert _dziennik_sleep(tmp_path).read_text().split() == ["1"]
    assert _komunikat_zajetych("pelnego przebiegu", [pid], [blokada], 1) + "\n" in wynik.stderr
    assert not _slot(blokada, 2).exists()  # przy jednym slocie nie powstaje drugi


def test_przy_dwoch_zajetych_slotach_przebieg_konczy_sie_kodem_75_z_pidami_obu_wlascicieli(tmp_path):
    blokada = tmp_path / "blokada"
    env = _srodowisko(tmp_path, COCKPIT_TESTY_SLOTY="2", COCKPIT_TESTY_LIMIT_BLOKADY="0")
    with _zywy_wlasciciel(blokada) as pierwszy, _zywy_wlasciciel(_slot(blokada, 2)) as drugi:
        wynik = _uruchom(env)
        assert wynik.kod == 75, wynik.stderr
        assert (
            _komunikat_zajetych(
                "pelnego przebiegu", [pierwszy, drugi], [blokada, _slot(blokada, 2)], 0
            )
            + "\n"
        ) in wynik.stderr
        assert not _slot(blokada, 3).exists()  # slotow jest tyle, ile w COCKPIT_TESTY_SLOTY
        assert (blokada / "pid").read_text().strip() == str(pierwszy)
        assert (_slot(blokada, 2) / "pid").read_text().strip() == str(drugi)
    polecenia = (tmp_path / "docker.log").read_text().splitlines()
    assert not [p for p in polecenia if p.startswith(("compose up", "compose port"))], polecenia


def test_przy_trzech_slotach_i_dwoch_zajetych_przebieg_bierze_slot_3(tmp_path):
    blokada = tmp_path / "blokada"
    env = _srodowisko(
        tmp_path,
        COCKPIT_TESTY_SLOTY="3",
        COCKPIT_TESTY_LIMIT_BLOKADY="0",
        FALSZYWY_PID_SLOTU=str(tmp_path / "sloty.log"),
    )
    with _zywy_wlasciciel(blokada) as pierwszy, _zywy_wlasciciel(_slot(blokada, 2)) as drugi:
        wynik = _uruchom(env)
    assert wynik.kod == 0, wynik.stderr
    assert (tmp_path / "sloty.log").read_text().splitlines() == [
        f"slot.1 {pierwszy}",
        f"slot.2 {drugi}",
        f"slot.3 {wynik.pid}",
    ]
    assert not _slot(blokada, 3).exists()


def test_wolne_sloty_sa_zajmowane_po_kolei_od_pierwszego(tmp_path):
    env = _srodowisko(
        tmp_path, COCKPIT_TESTY_SLOTY="3", FALSZYWY_PID_SLOTU=str(tmp_path / "sloty.log")
    )
    wynik = _uruchom(env)
    assert wynik.kod == 0, wynik.stderr
    assert (tmp_path / "sloty.log").read_text().splitlines() == [f"slot.1 {wynik.pid}"]


def test_w_kazdej_sekundzie_czekania_skrypt_probuje_wszystkie_sloty(tmp_path):
    # Oba sloty zajete; po pierwszej sekundzie czekania zwalnia sie slot 2 (nie slot 1): przebieg,
    # ktory po sekundzie probowalby tylko slotu 1, czekalby do limitu.
    blokada = tmp_path / "blokada"
    env = _srodowisko_bez_czekania(
        tmp_path,
        COCKPIT_TESTY_SLOTY="2",
        COCKPIT_TESTY_LIMIT_BLOKADY="5",
        FALSZYWY_ZWOLNIJ_PO_SLEEPIE="1",
        FALSZYWY_ZWOLNIJ_KATALOG=str(_slot(blokada, 2)),
        FALSZYWY_PID_SLOTU=str(tmp_path / "sloty.log"),
    )
    with _zywy_wlasciciel(blokada) as pierwszy, _zywy_wlasciciel(_slot(blokada, 2)) as drugi:
        wynik = _uruchom(env)
        assert wynik.kod == 0, wynik.stderr
        assert (blokada / "pid").read_text().strip() == str(pierwszy)
    assert _dziennik_sleep(tmp_path).read_text().split() == ["1"]
    assert (
        f"Czekam na blokade pelnego przebiegu (trzyma PID {pierwszy}, {drugi}, limit 5 s)\n"
        in wynik.stderr
    )
    assert f"Blokada pelnego przebiegu zdobyta: {_slot(blokada, 2)} (PID {wynik.pid})\n" in (
        wynik.stderr
    )
    assert (tmp_path / "sloty.log").read_text().splitlines() == [
        f"slot.1 {pierwszy}",
        f"slot.2 {wynik.pid}",
    ]


def test_slot_martwego_procesu_jest_przejety_gdy_slot_1_trzyma_zywy_przebieg(tmp_path):
    blokada = tmp_path / "blokada"
    slot_2 = _slot(blokada, 2)
    slot_2.mkdir()
    martwy = _martwy_pid()
    (slot_2 / "pid").write_text(f"{martwy}\n")
    env = _srodowisko_bez_czekania(
        tmp_path,
        COCKPIT_TESTY_SLOTY="2",
        COCKPIT_TESTY_LIMIT_BLOKADY="0",
        FALSZYWY_PID_SLOTU=str(tmp_path / "sloty.log"),
    )
    with _zywy_wlasciciel(blokada) as inny:
        wynik = _uruchom(env)
        assert wynik.kod == 0, wynik.stderr
        assert (blokada / "pid").read_text().strip() == str(inny)
    assert f"Przejmuje blokade martwego procesu PID {martwy}: {slot_2}\n" in wynik.stderr
    assert f"Blokada pelnego przebiegu zdobyta: {slot_2} (PID {wynik.pid})\n" in wynik.stderr
    assert not _dziennik_sleep(tmp_path).exists()  # przejecie nie czeka
    # Podczas testow slot 2 ma PID tego przebiegu: po przejeciu skrypt ponowil probe zdobycia
    # (`mkdir` + zapis pid), a nie uznal slotu za zdobyty bez zapisu.
    assert (tmp_path / "sloty.log").read_text().splitlines() == [
        f"slot.1 {inny}",
        f"slot.2 {wynik.pid}",
    ]
    assert not slot_2.exists() and not _przejecie(slot_2).exists()


def test_martwy_slot_1_jest_przejety_i_zajety_zanim_przebieg_siegnie_po_slot_2(tmp_path):
    # Slot 2 jest wolny: przebieg, ktory po przejeciu martwego slotu 1 szedlby dalej do slotu 2
    # zamiast ponowic probe od slotu 1, zajalby slot 2.
    blokada = tmp_path / "blokada"
    blokada.mkdir()
    martwy = _martwy_pid()
    (blokada / "pid").write_text(f"{martwy}\n")
    env = _srodowisko_bez_czekania(
        tmp_path,
        COCKPIT_TESTY_SLOTY="2",
        COCKPIT_TESTY_LIMIT_BLOKADY="0",
        FALSZYWY_PID_SLOTU=str(tmp_path / "sloty.log"),
    )
    wynik = _uruchom(env)
    assert wynik.kod == 0, wynik.stderr
    assert f"Przejmuje blokade martwego procesu PID {martwy}: {blokada}\n" in wynik.stderr
    assert f"Blokada pelnego przebiegu zdobyta: {blokada} (PID {wynik.pid})\n" in wynik.stderr
    assert (tmp_path / "sloty.log").read_text().splitlines() == [f"slot.1 {wynik.pid}"]
    assert not _slot(blokada, 2).exists()
    assert not _dziennik_sleep(tmp_path).exists()


def test_slot_bez_pidu_po_slocie_z_zajetym_przejeciem_nie_dziedziczy_cudzego_wlasciciela(tmp_path):
    # Slot 1: martwy wlasciciel, a przejecie trzyma inny przebieg (kod 2, WLASCICIEL = martwy PID).
    # Slot 2: katalog bez pliku pid (wlasciciel dopiero zapisuje) - ma byc zajety, nie martwy.
    blokada = tmp_path / "blokada"
    slot_2 = _slot(blokada, 2)
    blokada.mkdir()
    (blokada / "pid").write_text(f"{_martwy_pid()}\n")
    _przejecie(blokada).mkdir()
    slot_2.mkdir()
    env = _srodowisko_bez_czekania(
        tmp_path, COCKPIT_TESTY_SLOTY="2", COCKPIT_TESTY_LIMIT_BLOKADY="0"
    )
    wynik = _uruchom(env)
    assert wynik.kod == 75, wynik.stderr
    assert "Przejmuje blokade" not in wynik.stderr
    assert slot_2.is_dir() and not _przejecie(slot_2).exists()
    assert not _dziennik_sleep(tmp_path).exists()


def test_przy_kilku_slotach_z_zajetym_przejeciem_komunikat_wskazuje_pierwszy_z_nich(tmp_path):
    blokada = tmp_path / "blokada"
    for numer in (1, 2):
        slot = _slot(blokada, numer)
        slot.mkdir()
        (slot / "pid").write_text(f"{_martwy_pid()}\n")
        _przejecie(slot).mkdir()
    env = _srodowisko_bez_czekania(
        tmp_path, COCKPIT_TESTY_SLOTY="2", COCKPIT_TESTY_LIMIT_BLOKADY="0"
    )
    wynik = _uruchom(env)
    assert wynik.kod == 75, wynik.stderr
    assert (
        f"BLAD: przejecie blokady pelnego przebiegu zajete ({blokada}.przejecie) - limit 0 s "
        "minal; jesli zaden przebieg testow nie trwa, usun ten katalog recznie\n"
    ) in wynik.stderr
    assert f"{_slot(blokada, 2)}.przejecie" not in wynik.stderr


def test_domyslnie_dwa_zajete_sloty_daja_kod_75_i_nie_powstaje_trzeci_slot(tmp_path):
    blokada = tmp_path / "blokada"
    env = _srodowisko(tmp_path, COCKPIT_TESTY_SLOTY=None, COCKPIT_TESTY_LIMIT_BLOKADY="0")
    with _zywy_wlasciciel(blokada) as pierwszy, _zywy_wlasciciel(_slot(blokada, 2)) as drugi:
        wynik = _uruchom(env)
    assert wynik.kod == 75, wynik.stderr
    assert (
        _komunikat_zajetych("pelnego przebiegu", [pierwszy, drugi], [blokada, _slot(blokada, 2)], 0)
        + "\n"
    ) in wynik.stderr
    assert not _slot(blokada, 3).exists()


@pytest.mark.parametrize("wartosc", ["0", "abc", "-1", "1.5", "2x"])
def test_zla_liczba_slotow_konczy_przebieg_kodem_1_zanim_cokolwiek_wystartuje(tmp_path, wartosc):
    wynik = _uruchom(_srodowisko(tmp_path, COCKPIT_TESTY_SLOTY=wartosc))
    assert wynik.kod == 1, wynik.stderr
    # Dokladnie jeden komunikat, bez dopisku `[: ...: integer expression expected` z powloki.
    assert wynik.stderr == (
        f"BLAD: COCKPIT_TESTY_SLOTY musi byc liczba calkowita >= 1 (jest: '{wartosc}')\n"
    )
    assert "Traceback" not in wynik.calosc
    assert not (tmp_path / "docker.log").exists()  # nawet `docker compose version` nie ruszyl
    assert not (tmp_path / "blokada").exists()


# --- krotka blokada wspolnego zasobu ------------------------------------------------------------------


def _dziennik_zasobu(tmp_path: Path) -> list[str]:
    sciezka = tmp_path / "zasob.log"
    return sciezka.read_text().splitlines() if sciezka.exists() else []


def _stan_zasobu(linia: str) -> str:
    return linia.rsplit(" zasob=", 1)[1]


def test_wspolny_zasob_jest_trzymany_tylko_wokol_operacji_na_serwerze_i_przegladarce(tmp_path):
    korzen = _korzen_z_kopia_skryptu(tmp_path, "a")
    stara = f"cockpit_test_{_skrot_sciezki(korzen)}_{_martwy_pid()}"
    _dodaj_baze(tmp_path, stara)
    wynik, _, _ = _przebieg_w_korzeniu(tmp_path, korzen, "a", FALSZYWY_BRAK_COCKPIT="1")
    assert wynik.kod == 0, wynik.stderr
    nowa = _nazwa_bazy_przebiegu(korzen, wynik.pid)
    dziennik = _dziennik_zasobu(tmp_path)
    pierwszy_npm = next(i for i, linia in enumerate(dziennik) if linia.startswith("npm "))
    pod_zasobem, poza_zasobem = dziennik[:pierwszy_npm], dziennik[pierwszy_npm:]
    operacje = [
        linia
        for linia in pod_zasobem
        if linia.startswith(
            ("docker compose port", "docker compose up", "docker compose exec", "playwright")
        )
    ]
    # Kazda operacja na wspolnym zasobie idzie pod blokada ...
    assert all(_stan_zasobu(linia) == "jest" for linia in operacje), operacje
    # ... i sa to dokladnie te operacje (test nie przechodzi na pustej liscie).
    oczekiwane = [
        "docker compose port",
        "docker compose up",
        "system_identifier",
        "-c CREATE DATABASE cockpit OWNER cockpit",
        "-c SELECT datname FROM pg_database WHERE datname LIKE",
        f'-c DROP DATABASE IF EXISTS "{stara}" WITH (FORCE)',
        f'-c CREATE DATABASE "{nowa}" OWNER cockpit',
        "playwright",
    ]
    for fragment in oczekiwane:
        assert any(fragment in linia for linia in operacje), (fragment, operacje)
    # Build frontu, testy i sprzatanie w trapie ida poza blokada.
    assert [linia.split(" zasob=")[0] for linia in poza_zasobem if linia.startswith("pytest")] == [
        "pytest pel",
        "pytest baza",
        "pytest e2e",
    ]
    assert all(_stan_zasobu(linia) == "brak" for linia in poza_zasobem), poza_zasobem
    assert any(linia.startswith("docker compose exec") for linia in poza_zasobem)  # drop w trapie
    assert not _zasob(tmp_path / "blokada_a").exists()


def test_zajety_zasob_zywego_procesu_daje_kod_75_po_limicie_i_nie_rusza_serwera(tmp_path):
    blokada = tmp_path / "blokada"
    zasob = _zasob(blokada)
    env = _srodowisko_bez_czekania(tmp_path, COCKPIT_TESTY_LIMIT_BLOKADY="1")
    with _zywy_wlasciciel(zasob) as pid:
        wynik = _uruchom(env)
        assert wynik.kod == 75, wynik.stderr
        assert (zasob / "pid").read_text().strip() == str(pid)  # trap nie zwolnil cudzego zasobu
    assert _komunikat_zajetych("wspolnego zasobu", [pid], [zasob], 1) + "\n" in wynik.stderr
    assert wynik.stderr.count("Czekam na blokade wspolnego zasobu") == 1
    assert f"Czekam na blokade wspolnego zasobu (trzyma PID {pid}, limit 1 s)\n" in wynik.stderr
    assert _dziennik_sleep(tmp_path).read_text().split() == ["1"]
    polecenia = (tmp_path / "docker.log").read_text().splitlines()
    assert not [p for p in polecenia if p.startswith(("compose port", "compose up"))], polecenia
    assert not blokada.exists()  # slot zdobyty przed zasobem jest zwolniony


def test_zasob_martwego_procesu_jest_przejety_bez_czekania_i_zwolniony_po_przebiegu(tmp_path):
    blokada = tmp_path / "blokada"
    zasob = _zasob(blokada)
    zasob.mkdir()
    martwy = _martwy_pid()
    (zasob / "pid").write_text(f"{martwy}\n")
    env = _srodowisko_bez_czekania(tmp_path, COCKPIT_TESTY_LIMIT_BLOKADY="40")
    wynik = _uruchom(env)
    assert wynik.kod == 0, wynik.stderr
    assert f"Przejmuje blokade martwego procesu PID {martwy}: {zasob}\n" in wynik.stderr
    assert f"Blokada wspolnego zasobu zdobyta: {zasob} (PID {wynik.pid})\n" in wynik.stderr
    assert not _dziennik_sleep(tmp_path).exists()
    assert not zasob.exists() and not _przejecie(zasob).exists()


def test_oczekiwanie_na_slot_i_na_zasob_liczy_sie_do_jednego_limitu(tmp_path):
    # Slot zwalnia sie po 1. sekundzie czekania, zasob trzyma zywy proces: lacznie 3 sekundy
    # (1 + 2), nie 4 (1 + limit liczony od nowa dla zasobu).
    blokada = tmp_path / "blokada"
    zasob = _zasob(blokada)
    env = _srodowisko_bez_czekania(
        tmp_path,
        COCKPIT_TESTY_SLOTY="1",
        COCKPIT_TESTY_LIMIT_BLOKADY="3",
        FALSZYWY_ZWOLNIJ_PO_SLEEPIE="1",
        FALSZYWY_ZWOLNIJ_KATALOG=str(blokada),
    )
    with _zywy_wlasciciel(blokada) as pid_slotu, _zywy_wlasciciel(zasob) as pid_zasobu:
        wynik = _uruchom(env)
    assert wynik.kod == 75, wynik.stderr
    assert _dziennik_sleep(tmp_path).read_text().split() == ["1", "1", "1"]
    assert wynik.stderr.count("Czekam na blokade pelnego przebiegu") == 1
    assert (
        f"Czekam na blokade pelnego przebiegu (trzyma PID {pid_slotu}, limit 3 s)\n" in wynik.stderr
    )
    assert (
        f"Czekam na blokade wspolnego zasobu (trzyma PID {pid_zasobu}, limit 3 s)\n" in wynik.stderr
    )
    assert _komunikat_zajetych("wspolnego zasobu", [pid_zasobu], [zasob], 3) + "\n" in wynik.stderr
    assert not blokada.exists()  # slot zdobyty w trakcie czekania zwolnil trap


# --- port przelotu demo --------------------------------------------------------------------------------


def test_port_demo_trafia_do_wszystkich_przebiegow_pytest_jako_wolny_port(tmp_path):
    dziennik = tmp_path / "port_demo.log"
    wynik = _uruchom(_srodowisko(tmp_path, FALSZYWY_PORT_DEMO=str(dziennik)))
    assert wynik.kod == 0, wynik.stderr
    linie = [linia.split() for linia in dziennik.read_text().splitlines()]
    assert [rodzaj for rodzaj, _ in linie] == ["pel", "baza", "e2e"]
    porty = {port for _, port in linie}
    assert len(porty) == 1, linie  # jeden port na caly przebieg
    napis = porty.pop()
    assert re.fullmatch(r"[0-9]+", napis), napis
    port = int(napis)
    assert 1 <= port <= 65535
    assert port != 8799  # to nie stala z CI: system przydziela wolny port z zakresu tymczasowego
    # Port dalo sie zwiazac, wiec byl wolny (skrypt wybiera go przez bind na porcie 0).
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as gniazdo:
        gniazdo.bind(("127.0.0.1", port))


@pytest.mark.parametrize("wynik_portu", ["", "abc"], ids=["pusty", "nieliczbowy"])
def test_brak_wolnego_portu_demo_to_blad_przed_testami_a_baza_przebiegu_jest_usunieta(
    tmp_path, wynik_portu
):
    korzen = _korzen_z_kopia_skryptu(tmp_path, "a")
    wynik, urle, _ = _przebieg_w_korzeniu(
        tmp_path, korzen, "a", FALSZYWY_WYNIK_PORTU=wynik_portu
    )
    assert wynik.kod == 1, wynik.stderr
    assert f"BLAD: brak wolnego portu demo (wynik: '{wynik_portu}')\n" in wynik.stderr
    assert urle == []
    assert _stan_baz(tmp_path) == set()
    assert "Traceback" not in wynik.calosc


# --- kryterium punktu 7: dwa pelne przebiegi naraz ---------------------------------------------------


def test_dwa_pelne_przebiegi_przy_domyslnych_slotach_ida_naraz_na_osobnych_bazach(tmp_path):
    korzen = _korzen_z_kopia_skryptu(tmp_path, "a")
    blokada = tmp_path / "wspolna_blokada"
    (tmp_path / "spotkanie").mkdir()
    env = {}
    for tag, drugi in (("A", "B"), ("B", "A")):
        env[tag] = _srodowisko(
            tmp_path,
            blokada=blokada,
            COCKPIT_TESTY_SLOTY=None,  # domyslnie dwa sloty
            FALSZYWY_TAG=tag,
            FALSZYWY_DRUGI_TAG=drugi,
            FALSZYWY_SPOTKANIE=str(tmp_path / "spotkanie"),
            FALSZYWY_URL_BAZY=str(tmp_path / f"url_{tag}.log"),
            FALSZYWY_PID_SLOTU=str(tmp_path / f"sloty_{tag}.log"),
        )
    skrypt = korzen / "tools" / "testy_pelne.sh"
    proc_a, start_a = _start(env["A"], skrypt, korzen)
    proc_b, start_b = _start(env["B"], skrypt, korzen)
    try:
        a, b = _koniec(proc_a, start_a, "przebieg A"), _koniec(proc_b, start_b, "przebieg B")
    finally:
        zakoncz_proces_sync(proc_a, nazwa="przebieg A")
        zakoncz_proces_sync(proc_b, nazwa="przebieg B")
    # Kod 0 w obu: kazdy pierwszy pytest doczekal sie startu drugiego (inaczej kod 97 -> 1).
    assert (a.kod, b.kod) == (0, 0), (a.stderr, b.stderr)
    urle = {
        tag: (tmp_path / f"url_{tag}.log").read_text().splitlines() for tag in ("A", "B")
    }
    assert urle["A"] == [
        f"postgresql+asyncpg://cockpit:cockpit@localhost:{PORT}/"
        f"{_nazwa_bazy_przebiegu(korzen, a.pid)}"
    ] * 3
    assert urle["B"] == [
        f"postgresql+asyncpg://cockpit:cockpit@localhost:{PORT}/"
        f"{_nazwa_bazy_przebiegu(korzen, b.pid)}"
    ] * 3
    assert urle["A"][0] != urle["B"][0]  # ten sam worktree, dwie bazy
    # Sloty: jeden przebieg w slocie 1, drugi w slocie 2.
    sloty = {
        tag: (tmp_path / f"sloty_{tag}.log").read_text().splitlines() for tag in ("A", "B")
    }
    wlasne = sorted(
        linia.split()[0]
        for tag, pid in (("A", a.pid), ("B", b.pid))
        for linia in sloty[tag]
        if linia.split()[1] == str(pid)
    )
    assert wlasne == ["slot.1", "slot.2"]
    # Po przebiegach nie zostaje ani baza, ani blokada.
    assert _stan_baz(tmp_path) == set()
    assert not blokada.exists() and not _slot(blokada, 2).exists() and not _zasob(blokada).exists()


# --- HAR-236, runda 4: elementy sprzatania, zapytan do serwera i portu demo -----------------------

PREFIKS_PSQL = "compose exec -T db psql -U cockpit -d cockpit_test -tA -c "


def test_kazde_zapytanie_do_serwera_idzie_przez_psql_na_baze_administracyjna_jednym_argumentem_sql(
    tmp_path,
):
    korzen = _korzen_z_kopia_skryptu(tmp_path, "a")
    argc = tmp_path / "argc_dockera.log"
    wynik, _, _ = _przebieg_w_korzeniu(
        tmp_path, korzen, "a", FALSZYWY_ARGC_DOCKERA=str(argc), FALSZYWY_BRAK_COCKPIT="1"
    )
    assert wynik.kod == 0, wynik.stderr
    zapytania = []
    for linia in argc.read_text().splitlines():
        liczba, reszta = linia.split(" ", 1)
        if reszta.startswith("compose exec"):
            zapytania.append((liczba, reszta))
    # Tozsamosc serwera, istnienie i tworzenie `cockpit`, lista, usuniecie i utworzenie bazy przebiegu,
    # usuniecie w trapie.
    assert len(zapytania) >= 7, zapytania
    for liczba, reszta in zapytania:
        assert reszta.startswith(PREFIKS_PSQL), reszta
        # 11 slow polecenia + CALE zapytanie SQL jako jeden argument.
        assert liczba == "12", (liczba, reszta)


def test_polecenia_sql_skryptu_nie_wypisuja_znacznikow_polecen_na_stdout(tmp_path):
    korzen = _korzen_z_kopia_skryptu(tmp_path, "a")
    stara = f"cockpit_test_{_skrot_sciezki(korzen)}_{_martwy_pid()}"
    _dodaj_baze(tmp_path, stara)
    wynik, _, _ = _przebieg_w_korzeniu(
        tmp_path, korzen, "a", FALSZYWY_BRAK_COCKPIT="1", FALSZYWY_TWORZ_GW="1"
    )
    assert wynik.kod == 0, wynik.stderr
    nazwa = _nazwa_bazy_przebiegu(korzen, wynik.pid)
    # Wszystkie rodzaje polecen faktycznie poszly: utworzenie `cockpit`, usuniecie starej bazy,
    # utworzenie bazy przebiegu, usuniecie baz workerow i bazy przebiegu.
    assert "Usuwam baze po martwym przebiegu PID" in wynik.stderr
    assert _stan_baz(tmp_path) == {"cockpit", f"{nazwa}xgw0", f"{nazwa}_gw-1"}
    assert "CREATE DATABASE" not in wynik.stdout
    assert "DROP DATABASE" not in wynik.stdout


def test_zielony_przebieg_bez_workerow_usuwa_baze_przebiegu_dwa_razy_bez_polecenia_z_pusta_nazwa(
    tmp_path,
):
    # Raz przed utworzeniem (PID mogl wrocic do obiegu), raz w trapie; za kazdym razem po liscie
    # baz workerow `<baza>\_gw%`. Pusta lista nie daje polecenia usuniecia bazy o pustej nazwie.
    korzen = _korzen_z_kopia_skryptu(tmp_path, "a")
    wynik, _, docker = _przebieg_w_korzeniu(tmp_path, korzen, "a")
    assert wynik.kod == 0, wynik.stderr
    nazwa = _nazwa_bazy_przebiegu(korzen, wynik.pid)
    polecenia = [p for p in docker if p.startswith("compose exec")]
    assert [p for p in polecenia if "-c DROP DATABASE" in p] == [
        f'{PREFIKS_PSQL}DROP DATABASE IF EXISTS "{nazwa}" WITH (FORCE)'
    ] * 2
    assert [p for p in polecenia if p.endswith("\\_gw%'")] == [
        f"{PREFIKS_PSQL}SELECT datname FROM pg_database WHERE datname LIKE '{nazwa}\\_gw%'"
    ] * 2


# Falszywe `mktemp` (BSD z macOS ignoruje TMPDIR): pliki tymczasowe skryptu laduja w katalogu testu.
FALSZYWY_MKTEMP = """#!/bin/sh
exec /usr/bin/mktemp "$FALSZYWY_KATALOG_TMP/log.XXXXXX"
"""


def test_po_przebiegu_nie_zostaja_pliki_tymczasowe_z_logami_testow(tmp_path):
    katalog = tmp_path / "tmpdir"
    katalog.mkdir()
    env = _srodowisko(tmp_path, FALSZYWY_KATALOG_TMP=str(katalog))
    _zapisz_narzedzie(tmp_path / "bin" / "mktemp", FALSZYWY_MKTEMP)
    wynik = _uruchom(env)
    assert wynik.kod == 0, wynik.stderr
    assert list(katalog.iterdir()) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root czyta kazdy plik, wiec pid nie bedzie nieczytelny")
def test_nieczytelny_plik_pid_zajetego_slotu_nie_zmienia_kodu_75_ani_nie_wypisuje_bledu_cat(
    tmp_path,
):
    blokada = tmp_path / "blokada"
    env = _srodowisko(tmp_path, COCKPIT_TESTY_LIMIT_BLOKADY="0")
    with _zywy_wlasciciel(blokada):
        plik_pid = blokada / "pid"
        plik_pid.chmod(0)
        try:
            wynik = _uruchom(env)
        finally:
            plik_pid.chmod(0o644)
    # Sprzatanie w trapie czyta pid slotu, ktorego nie zdobyto: nieczytelny plik to nie wlasciciel,
    # a blad `cat` nie moze zmienic kodu wyjscia ani trafic na stderr.
    assert wynik.kod == 75, wynik.stderr
    assert f"zajeta przez PID ? ({blokada}) - limit 0 s minal" in wynik.stderr
    assert "cat:" not in wynik.stderr


# Falszywe `rmdir`: po pierwszym zwolnieniu przejecia slotu `FALSZYWY_KATALOG_PODMIANY` konkurent
# odtwarza slot z martwym wlascicielem i zajmuje jego przejecie (zdarzenie, nie czas).
FALSZYWY_RMDIR_ODTWARZA_PRZEJECIE = """#!/bin/sh
/bin/rmdir "$@"
kod=$?
if [ "$1" = "${FALSZYWY_KATALOG_PODMIANY:-}.przejecie" ] && [ ! -e "$FALSZYWY_ZNACZNIK_PODMIANY" ]; then
  : > "$FALSZYWY_ZNACZNIK_PODMIANY"
  /bin/mkdir "${1%.przejecie}" "$1"
  echo "$FALSZYWY_MARTWY_PID" > "${1%.przejecie}/pid"
fi
exit $kod
"""


def test_zwolnione_przejecie_nie_jest_pamietane_wiec_wyjscie_nie_usuwa_cudzego_przejecia(tmp_path):
    blokada = tmp_path / "blokada"
    blokada.mkdir()
    martwy = _martwy_pid()
    (blokada / "pid").write_text(f"{martwy}\n")
    env = _srodowisko_bez_czekania(
        tmp_path,
        COCKPIT_TESTY_LIMIT_BLOKADY="0",
        FALSZYWY_KATALOG_PODMIANY=str(blokada),
        FALSZYWY_ZNACZNIK_PODMIANY=str(tmp_path / "podmiana"),
        FALSZYWY_MARTWY_PID=str(martwy),
    )
    _zapisz_narzedzie(tmp_path / "bin" / "rmdir", FALSZYWY_RMDIR_ODTWARZA_PRZEJECIE)
    wynik = _uruchom(env)
    # Pierwsze przejecie zakonczone; slot odtworzony przez konkurenta, ktory trzyma jego przejecie.
    assert wynik.kod == 75, wynik.stderr
    assert wynik.stderr.count("Przejmuje blokade martwego procesu") == 1
    assert (
        f"BLAD: przejecie blokady pelnego przebiegu zajete ({blokada}.przejecie) - limit 0 s minal"
    ) in wynik.stderr
    # Skrypt sprzata tylko przejecie, ktore sam trzyma: cudze (odtworzone) zostaje.
    assert _przejecie(blokada).is_dir()
    assert (blokada / "pid").read_text().strip() == str(martwy)


def test_wolny_port_demo_z_bialymi_znakami_wokol_liczby_jest_czyszczony_przed_eksportem(tmp_path):
    dziennik = tmp_path / "port_demo.log"
    wynik = _uruchom(
        _srodowisko(
            tmp_path, FALSZYWY_PORT_DEMO=str(dziennik), FALSZYWY_WYNIK_PORTU=" 54321\t"
        )
    )
    assert wynik.kod == 0, wynik.stderr
    assert dziennik.read_text().splitlines() == ["pel 54321", "baza 54321", "e2e 54321"]


# --- HAR-236, poprawka po review: bledy blokady to komunikaty, nie petla bez konca --------------

# Falszywe `rm` odmawia usuniecia dokladnie katalogu z `FALSZYWY_RM_ODMAWIA` (wywolanie z flagami
# `-rf`), pozostale wywolania przekazuje do prawdziwego `rm`.
FALSZYWY_RM_ODMAWIA_KATALOGU = """#!/bin/sh
if [ "$*" = "-rf ${FALSZYWY_RM_ODMAWIA:-brak}" ]; then exit 1; fi
exec /bin/rm "$@"
"""

# Falszywe `mkdir`: po utworzeniu katalogu `FALSZYWY_MKDIR_BEZ_ZAPISU` odbiera mu prawo zapisu, wiec
# zapis pliku pid w nim zawodzi mimo udanego `mkdir`.
FALSZYWY_MKDIR_BEZ_ZAPISU = """#!/bin/sh
/bin/mkdir "$@" || exit $?
if [ "$1" = "${FALSZYWY_MKDIR_BEZ_ZAPISU:-brak}" ]; then /bin/chmod 555 "$1"; fi
exit 0
"""


@pytest.mark.skipif(os.geteuid() == 0, reason="root usuwa katalog bez prawa zapisu")
def test_martwa_blokada_ktorej_nie_da_sie_usunac_to_kod_1_z_komunikatem_a_nie_petla_bez_konca(
    tmp_path,
):
    blokada = tmp_path / "blokada"
    blokada.mkdir()
    (blokada / "pid").write_text("abc\n")  # PID nieliczbowy = blokada martwego procesu
    blokada.chmod(0o555)
    env = _srodowisko_bez_czekania(tmp_path, COCKPIT_TESTY_LIMIT_BLOKADY="1")
    try:
        wynik = _uruchom(env)
    finally:
        blokada.chmod(0o755)
    assert wynik.kod == 1, wynik.stderr
    assert f"Przejmuje blokade martwego procesu PID abc: {blokada}\n" in wynik.stderr
    assert (
        f"BLAD: nie da sie usunac martwej blokady {blokada} "
        "(sprawdz prawa do katalogu i usun go recznie)\n"
    ) in wynik.stderr
    assert (blokada / "pid").read_text().strip() == "abc"  # blokada nietknieta
    # Katalog przejecia zwolniony w galezi bledu: nie zostaje po przebiegu.
    assert not _przejecie(blokada).exists()


def test_po_nieudanym_usunieciu_martwej_blokady_cudze_przejecie_odtworzone_po_zwolnieniu_zostaje(
    tmp_path,
):
    blokada = tmp_path / "blokada"
    blokada.mkdir()
    martwy = _martwy_pid()
    (blokada / "pid").write_text(f"{martwy}\n")
    env = _srodowisko_bez_czekania(
        tmp_path,
        COCKPIT_TESTY_LIMIT_BLOKADY="1",
        FALSZYWY_RM_ODMAWIA=str(blokada),
        FALSZYWY_KATALOG_PODMIANY=str(blokada),
        FALSZYWY_ZNACZNIK_PODMIANY=str(tmp_path / "podmiana"),
        FALSZYWY_MARTWY_PID=str(martwy),
    )
    _zapisz_narzedzie(tmp_path / "bin" / "rm", FALSZYWY_RM_ODMAWIA_KATALOGU)
    _zapisz_narzedzie(tmp_path / "bin" / "rmdir", FALSZYWY_RMDIR_ODTWARZA_PRZEJECIE)
    wynik = _uruchom(env)
    assert wynik.kod == 1, wynik.stderr
    assert (
        f"BLAD: nie da sie usunac martwej blokady {blokada} "
        "(sprawdz prawa do katalogu i usun go recznie)\n"
    ) in wynik.stderr
    assert (tmp_path / "podmiana").exists()  # galaz bledu zwolnila WLASNE przejecie, konkurent odtworzyl
    # Przejecie odtworzone przez konkurenta po zwolnieniu: trap nie pamieta zwolnionego, wiec go nie rusza.
    assert _przejecie(blokada).is_dir()


@pytest.mark.skipif(os.geteuid() == 0, reason="root zapisuje do katalogu bez prawa zapisu")
def test_blokada_w_ktorej_nie_da_sie_zapisac_pid_to_kod_1_z_komunikatem_i_zwolniony_katalog(
    tmp_path,
):
    blokada = tmp_path / "blokada"
    env = _srodowisko_bez_czekania(
        tmp_path, COCKPIT_TESTY_LIMIT_BLOKADY="1", FALSZYWY_MKDIR_BEZ_ZAPISU=str(blokada)
    )
    _zapisz_narzedzie(tmp_path / "bin" / "mkdir", FALSZYWY_MKDIR_BEZ_ZAPISU)
    try:
        wynik = _uruchom(env)
    finally:
        if blokada.exists():
            blokada.chmod(0o755)
    assert wynik.kod == 1, wynik.stderr
    assert f"BLAD: nie da sie zapisac PID w blokadzie {blokada}\n" in wynik.stderr
    # Pusty katalog bez pid nie jest zwalniany przez trap (brak wlasciciela): usuwa go sama galaz bledu.
    assert not blokada.exists()


FALSZYWY_RMDIR_NIE_DZIALA = """#!/bin/sh
echo "rmdir: nie dziala" >&2
exit 1
"""


@pytest.mark.skipif(os.geteuid() == 0, reason="root zapisuje do katalogu bez prawa zapisu")
def test_blad_rmdir_po_nieudanym_zapisie_pid_nie_trafia_na_stderr_i_nie_zmienia_komunikatu(
    tmp_path,
):
    blokada = tmp_path / "blokada"
    env = _srodowisko_bez_czekania(
        tmp_path, COCKPIT_TESTY_LIMIT_BLOKADY="1", FALSZYWY_MKDIR_BEZ_ZAPISU=str(blokada)
    )
    _zapisz_narzedzie(tmp_path / "bin" / "mkdir", FALSZYWY_MKDIR_BEZ_ZAPISU)
    _zapisz_narzedzie(tmp_path / "bin" / "rmdir", FALSZYWY_RMDIR_NIE_DZIALA)
    try:
        wynik = _uruchom(env)
    finally:
        blokada.chmod(0o755)
    assert wynik.kod == 1, wynik.stderr
    assert f"BLAD: nie da sie zapisac PID w blokadzie {blokada}\n" in wynik.stderr
    assert "rmdir: nie dziala" not in wynik.stderr


def test_nieudane_zwolnienie_wlasnej_blokady_na_koncu_przebiegu_jest_zgloszone_a_kod_zostaje_0(
    tmp_path,
):
    blokada = tmp_path / "blokada"
    env = _srodowisko(tmp_path, FALSZYWY_RM_ODMAWIA=str(blokada))
    _zapisz_narzedzie(tmp_path / "bin" / "rm", FALSZYWY_RM_ODMAWIA_KATALOGU)
    wynik = _uruchom(env)
    assert wynik.kod == 0, wynik.stderr
    assert (
        f"BLAD: nie udalo sie zwolnic wlasnej blokady {blokada} (usun ja recznie)\n"
    ) in wynik.stderr
    assert (blokada / "pid").read_text().strip() == str(wynik.pid)  # katalog zostal (rm odmowil)
