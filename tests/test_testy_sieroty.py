"""`tools/testy.py` odbiera osierocone procesy w trakcie przebiegu (HAR-262).

W kontenerze, w ktorym PID 1 nie odbiera sierot, zakonczony wnuk zostaje w tablicy procesow i nadal
odpowiada na `kill(pid, 0)`. `testy.py` uruchomiony jako program staje sie ich subreaperem i odbiera je
sam, ale NIGDY nie odbiera wlasnych dzieci (kod pytesta musi dojsc bez zmian).

Wszystko, co wlacza subreapera albo odbiera procesy naprawde, dzieje sie w osobnym interpreterze:
proces pytesta nie moze zostac subreaperem ani zbierac dzieci innych testow. Tylko Linux (`prctl`).
"""
import ctypes
import errno
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from procesy import czy_zyje, uruchom_i_zbierz, zabij_proces

KORZEN = Path(__file__).resolve().parents[1]
NARZEDZIA = KORZEN / "tools"
sys.path.insert(0, str(NARZEDZIA))
import testy as testy_py  # noqa: E402

TESTY_PY = NARZEDZIA / "testy.py"
LIMIT_SKRYPTU = 120.0
KOD_BEZ_NADRZEDNEGO_SUBREAPERA = 99
ZMIENNE_PYTESTA = ("PYTEST_ADDOPTS", "PYTEST_CURRENT_TEST", "PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT")


def _interpreter(kod: str, *argumenty: str) -> subprocess.CompletedProcess:
    """Kod w osobnym interpreterze (wlasna grupa procesow, twardy limit czasu)."""
    srodowisko = {k: v for k, v in os.environ.items() if k not in ZMIENNE_PYTESTA}
    srodowisko["PYTHONDONTWRITEBYTECODE"] = "1"
    return uruchom_i_zbierz(
        [sys.executable, "-c", kod, *argumenty],
        cwd=KORZEN,
        env=srodowisko,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=LIMIT_SKRYPTU,
    )


def _plik_testow(tmp_path: Path, tresc: str) -> str:
    plik = tmp_path / "test_przyklad.py"
    plik.write_text(tresc, encoding="utf-8")
    return str(plik)


# --- sierota znika w trakcie przebiegu -------------------------------------------------------------

# Nadrzedny subreaper, ktory NIGDY nie odbiera sierot (czeka tylko na wlasny proces). Zastepuje PID 1
# z kontenera: bez mechanizmu w testy.py zombie zostaje takze na hoscie, gdzie init odbiera sieroty.
NADRZEDNY_BEZ_ODBIERANIA = f"""
import ctypes, subprocess, sys

libc = ctypes.CDLL(None, use_errno=True)
flaga = ctypes.c_int(0)
if libc.prctl(36, 1, 0, 0, 0) != 0 or libc.prctl(37, ctypes.byref(flaga), 0, 0, 0) != 0 or flaga.value != 1:
    sys.exit({KOD_BEZ_NADRZEDNEGO_SUBREAPERA})
proc = subprocess.Popen(sys.argv[1:])
sys.exit(proc.wait(100))
"""

# `sh` konczy sie od razu, wiec `sleep` jest osieroconym wnukiem pytesta. Dwie kolejne sieroty: odbieranie
# dziala przez caly przebieg, nie jeden raz.
PLIK_Z_SIEROTAMI = """
import os
import subprocess
import time


def test_osierocone_wnuki_znikaja_z_tablicy_procesow():
    for numer in (1, 2):
        wynik = subprocess.run(
            ["sh", "-c", "sleep 0.3 >/dev/null 2>&1 & echo $!"], capture_output=True, text=True, timeout=30, check=True
        )
        wnuk = int(wynik.stdout)
        koniec = time.monotonic() + 10
        while True:
            try:
                os.kill(wnuk, 0)
            except ProcessLookupError:
                break
            assert time.monotonic() < koniec, f"sierota {numer} nadal w tablicy procesow"
            time.sleep(0.05)
"""


def test_osierocony_wnuk_po_zakonczeniu_znika_z_tablicy_procesow_jeszcze_w_trakcie_przebiegu(tmp_path):
    plik = _plik_testow(tmp_path, PLIK_Z_SIEROTAMI)
    wynik = _interpreter(NADRZEDNY_BEZ_ODBIERANIA, sys.executable, str(TESTY_PY), "-q", plik)
    assert wynik.returncode != KOD_BEZ_NADRZEDNEGO_SUBREAPERA, "nie udalo sie przygotowac nadrzednego subreapera"
    assert wynik.returncode == 0, wynik.stdout + wynik.stderr
    assert "1 passed" in wynik.stdout


# --- kod pytesta przechodzi bez zmian ----------------------------------------------------------------

_SIEROTY_PRZED_KONCEM = """
import subprocess
import time

import pytest


def _zostaw_sieroty():
    for _ in range(3):
        subprocess.run(["sh", "-c", "sleep 0.1 >/dev/null 2>&1 &"], timeout=30, check=True)
    time.sleep(0.6)


def test_zostawia_sieroty_i_konczy_przebieg():
    _zostaw_sieroty()
"""
PRZEBIEGI_Z_NIEZEROWYM_KODEM = [
    pytest.param(_SIEROTY_PRZED_KONCEM + "    assert 1 == 2\n", 1, "1 failed", id="czerwony-test"),
    pytest.param(
        _SIEROTY_PRZED_KONCEM + "    pytest.exit('koniec', returncode=7)\n", 7, "koniec", id="wlasny-kod-pytesta"
    ),
]

# Wymusza kolejnosc, ktora w zwyklym przebiegu zdarza sie rzadko: pytest jest juz zombie, a `Popen` jeszcze
# go nie odebral (trzy kolejne `poll()` zwracaja None). Odbieranie, ktore ruszy wlasne dziecko, zabiera wtedy
# kod pytesta, a `Popen` zglasza 0. Skrypt rusza jak program (`run_name="__main__"`).
SPOZNIONY_ODBIOR_PYTESTA = """
import os, runpy, subprocess, sys

wstrzymane = []


class SpoznionyPopen(subprocess.Popen):
    def poll(self):
        if self.returncode is None and len(wstrzymane) < 3:
            try:
                stan = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                stan = None
            if stan is not None:
                wstrzymane.append(self.pid)
                return None
        return super().poll()


subprocess.Popen = SpoznionyPopen
sys.argv = sys.argv[1:]
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
finally:
    print(f"WSTRZYMANO: {len(wstrzymane)}", file=sys.stderr, flush=True)
"""


@pytest.mark.parametrize(("tresc", "kod", "slad"), PRZEBIEGI_Z_NIEZEROWYM_KODEM)
def test_niezerowy_kod_pytesta_przechodzi_bez_zmian_takze_gdy_przebieg_zostawia_sieroty(tmp_path, tresc, kod, slad):
    wynik = _interpreter(SPOZNIONY_ODBIOR_PYTESTA, str(TESTY_PY), "-q", _plik_testow(tmp_path, tresc))
    assert wynik.returncode == kod, wynik.stdout + wynik.stderr
    assert slad in wynik.stdout
    assert "WSTRZYMANO: 3" in wynik.stderr  # dowod, ze pytest naprawde czekal jako nieodebrany


# --- odbieranie: tylko obce procesy, bez zawieszenia ------------------------------------------------

WSTEP_ODBIERANIA = """
import os, subprocess, sys

sys.path.insert(0, sys.argv[1])
import testy


def obcy_zakonczony():
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
    return pid


def wlasne_zakonczone():
    # Wolac PO `obcy_zakonczony()`: jadro podaje zombie od najstarszego, a odbieranie konczy sie na wlasnym.
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(7)"])
    os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
    return proc


def istnieje(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


"""


def _odbieranie(scenariusz: str) -> str:
    wynik = _interpreter(WSTEP_ODBIERANIA + scenariusz, str(NARZEDZIA))
    assert wynik.returncode == 0, wynik.stderr
    return wynik.stdout.strip()


def test_odbieranie_nie_wisi_gdy_jedynym_zakonczonym_jest_wlasne_dziecko_i_zostawia_mu_kod_wyjscia():
    scenariusz = """
proc = wlasne_zakonczone()
testy.odbierz_sieroty({proc.pid})
print(proc.wait(10))
"""
    assert _odbieranie(scenariusz) == "7"


def test_odbieranie_usuwa_wszystkie_obce_zakonczone_procesy_a_wlasnemu_dziecku_zostawia_kod_wyjscia():
    scenariusz = """
obce = [obcy_zakonczony() for _ in range(3)]
proc = wlasne_zakonczone()
testy.odbierz_sieroty({proc.pid})
print([istnieje(pid) for pid in obce], proc.wait(10))
"""
    assert _odbieranie(scenariusz) == "[False, False, False] 7"


def test_odbieranie_nie_czeka_na_procesy_ktore_jeszcze_dzialaja():
    scenariusz = """
proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
try:
    testy.odbierz_sieroty(set())
    print(proc.poll())
finally:
    proc.kill()
    proc.wait(10)
"""
    wynik = _interpreter(WSTEP_ODBIERANIA + scenariusz, str(NARZEDZIA))
    assert (wynik.returncode, wynik.stdout.strip()) == (0, "None"), wynik.stderr


def test_odbieranie_bez_zadnego_procesu_potomnego_nie_jest_bledem():
    assert _odbieranie("testy.odbierz_sieroty(set())\nprint('ok')\n") == "ok"


@pytest.mark.parametrize(
    ("zakonczone", "odebrane"),
    [
        pytest.param([333], [], id="tylko-wlasne"),
        pytest.param([111, 222, 333], [111, 222], id="obce-przed-wlasnym"),
        pytest.param([111, None], [111], id="obcy-i-nic-wiecej"),
    ],
)
def test_odbierany_jest_wylacznie_wskazany_obcy_proces_nigdy_dowolne_dziecko(monkeypatch, zakonczone, odebrane):
    kolejka = list(zakonczone)
    wywolania: list[int] = []

    class _Zakonczony:
        def __init__(self, pid: int) -> None:
            self.si_pid = pid

    def podejrzyj(*_argumenty):
        # druga prosba po wlasnym dziecku albo po pustej odpowiedzi to petla bez konca
        assert kolejka, "odbieranie nie przerwalo petli"
        pid = kolejka.pop(0)
        return None if pid is None else _Zakonczony(pid)

    def odbierz(pid, _opcje):
        wywolania.append(pid)
        return pid, 0

    monkeypatch.setattr(testy_py.os, "waitid", podejrzyj)
    monkeypatch.setattr(testy_py.os, "waitpid", odbierz)
    testy_py.odbierz_sieroty({333})
    assert wywolania == odebrane


# --- blad systemu przy odbieraniu --------------------------------------------------------------------

PREFIKS_BLEDU_ODBIERANIA = "nie mozna odebrac osieroconych procesow: "


def test_odmowa_systemu_przy_podgladzie_zakonczonych_procesow_jest_bledem_narzedzia(monkeypatch):
    odmowa = PermissionError(errno.EPERM, "brak zgody na podglad")

    def pada(*_argumenty):
        raise odmowa

    monkeypatch.setattr(testy_py.os, "waitid", pada)
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        testy_py.odbierz_sieroty(set())
    assert str(blad.value) == f"{PREFIKS_BLEDU_ODBIERANIA}{odmowa}"


# `ChildProcessError` konczy odbieranie po cichu tylko przy podgladzie ("nie ma dzieci"); gdy proces byl
# widziany, a nie da sie go odebrac, to blad.
@pytest.mark.parametrize(
    "odmowa",
    [
        pytest.param(PermissionError(errno.EPERM, "brak zgody na odebranie"), id="brak-zgody"),
        pytest.param(ChildProcessError(errno.ECHILD, "proces zniknal"), id="proces-zniknal"),
    ],
)
def test_nieudane_odebranie_zauwazonego_procesu_jest_bledem_narzedzia(monkeypatch, odmowa):
    kolejka = [SimpleNamespace(si_pid=111)]

    def pada(*_argumenty):
        raise odmowa

    monkeypatch.setattr(testy_py.os, "waitid", lambda *_argumenty: kolejka.pop() if kolejka else None)
    monkeypatch.setattr(testy_py.os, "waitpid", pada)
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        testy_py.odbierz_sieroty(set())
    assert str(blad.value) == f"{PREFIKS_BLEDU_ODBIERANIA}{odmowa}"


# Odmowa pojawia sie dopiero, gdy test juz trwa (istnieje plik z PID-em pytesta), wiec jest co posprzatac.
PROGRAM_Z_ODMOWA_ODBIERANIA = """
import errno, os, runpy, sys

znacznik = sys.argv[1]


def waitid(*_argumenty):
    if os.path.exists(znacznik):
        raise PermissionError(errno.EPERM, "brak zgody na podglad")
    return None


os.waitid = waitid
sys.argv = sys.argv[2:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def test_blad_odbierania_w_trakcie_przebiegu_konczy_go_kodem_2_z_komunikatem_i_zabija_pytesta(tmp_path):
    znacznik = tmp_path / "pytest.pid"
    tresc = (
        "import os\nimport pathlib\nimport time\n\n\ndef test_trwa():\n"
        f"    gotowy = pathlib.Path({str(tmp_path / 'pid.tmp')!r})\n"
        "    gotowy.write_text(str(os.getpid()))\n"
        f"    gotowy.rename({str(znacznik)!r})\n"
        "    time.sleep(60)\n"
    )
    plik = _plik_testow(tmp_path, tresc)
    try:
        wynik = _interpreter(PROGRAM_Z_ODMOWA_ODBIERANIA, str(znacznik), str(TESTY_PY), "-q", plik)
        assert wynik.returncode == 2, wynik.stdout + wynik.stderr
        odmowa = PermissionError(errno.EPERM, "brak zgody na podglad")
        assert wynik.stderr == f"BLAD: {PREFIKS_BLEDU_ODBIERANIA}{odmowa}\n"
        assert not czy_zyje(int(znacznik.read_text()))
    finally:
        if znacznik.exists() and czy_zyje(int(znacznik.read_text())):
            zabij_proces(int(znacznik.read_text()))


# --- wywolanie z innego programu nie zmienia jego procesu --------------------------------------------

_FLAGA_SUBREAPERA = """
import ctypes


def flaga_subreapera():
    flaga = ctypes.c_int(-1)
    assert ctypes.CDLL(None, use_errno=True).prctl(37, ctypes.byref(flaga), 0, 0, 0) == 0
    return flaga.value


"""


@pytest.mark.parametrize(
    "wywolanie",
    [
        pytest.param("testy.uruchom(['-q', sys.argv[2]], 60.0, testy.KORZEN)", id="uruchom"),
        pytest.param("testy.main(['-q', sys.argv[2]])", id="main"),
    ],
)
def test_funkcja_narzedzia_wolana_z_innego_programu_nie_robi_z_niego_subreapera_ani_nie_odbiera_mu_dzieci(
    tmp_path, wywolanie
):
    scenariusz = f"""
proc = wlasne_zakonczone()
kod = {wywolanie}
print(f"WYNIK: kod={{kod}} subreaper={{flaga_subreapera()}} dziecko={{proc.wait(10)}}")
"""
    plik = _plik_testow(tmp_path, "def test_przechodzi():\n    pass\n")
    wynik = _interpreter(WSTEP_ODBIERANIA + _FLAGA_SUBREAPERA + scenariusz, str(NARZEDZIA), plik)
    assert wynik.returncode == 0, wynik.stderr
    assert wynik.stdout.splitlines()[-1] == "WYNIK: kod=0 subreaper=0 dziecko=7"


def test_wlaczenie_odbierania_sierot_na_linuksie_czyni_proces_subreaperem():
    scenariusz = "print(flaga_subreapera(), testy.wlacz_odbieranie_sierot(), flaga_subreapera())\n"
    assert _odbieranie(_FLAGA_SUBREAPERA + scenariusz) == "0 True 1"


# --- blad prctl i system inny niz Linux ---------------------------------------------------------------

PREFIKS_BLEDU = "nie mozna wlaczyc odbierania sierot: "
BLAD_PRCTL = f"{PREFIKS_BLEDU}prctl PR_SET_CHILD_SUBREAPER, errno {errno.EPERM} ({os.strerror(errno.EPERM)})"


class _LibcOdmawiajaca:
    def __init__(self) -> None:
        def prctl(*_argumenty):
            ctypes.set_errno(errno.EPERM)
            return -1

        self.prctl = prctl


def test_odmowa_systemu_przy_wlaczaniu_odbierania_sierot_jest_bledem_narzedzia_z_numerem_bledu(monkeypatch):
    monkeypatch.setattr(testy_py.sys, "platform", "linux")
    monkeypatch.setattr(testy_py, "_libc", _LibcOdmawiajaca)
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        testy_py.wlacz_odbieranie_sierot()
    assert str(blad.value) == BLAD_PRCTL


def test_niedostepna_biblioteka_systemowa_jest_bledem_narzedzia_a_nie_cichym_pominieciem(monkeypatch):
    def pada():
        raise OSError("brak libc")

    monkeypatch.setattr(testy_py.sys, "platform", "linux")
    monkeypatch.setattr(testy_py, "_libc", pada)
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        testy_py.wlacz_odbieranie_sierot()
    assert str(blad.value) == f"{PREFIKS_BLEDU}nie udalo sie zaladowac libc: brak libc"


def test_biblioteka_systemowa_jest_ladowana_tak_by_numer_bledu_byl_do_odczytania(monkeypatch):
    wywolania = []
    monkeypatch.setattr(testy_py.ctypes, "CDLL", lambda *a, **k: wywolania.append((a, k)) or "libc")
    assert testy_py._libc() == "libc"
    assert wywolania == [((None,), {"use_errno": True})]


def test_poza_linuksem_odbieranie_sierot_nie_jest_wlaczane_i_biblioteka_nie_jest_ladowana(monkeypatch):
    def zakazane():
        raise AssertionError("poza Linuksem nie wolno wolac prctl")

    monkeypatch.setattr(testy_py.sys, "platform", "darwin")
    monkeypatch.setattr(testy_py, "_libc", zakazane)
    assert testy_py.wlacz_odbieranie_sierot() is False


# `del os.waitid` odtwarza system bez tego wywolania: petla przebiegu nie moze wtedy probowac odbierac.
PROGRAM_POZA_LINUKSEM = """
import ctypes, os, runpy, sys
import argparse, hashlib, importlib.metadata, signal, sqlite3, subprocess, tempfile  # przed podmiana platformy


def zakazane(*_argumenty, **_nazwane):
    raise AssertionError("poza Linuksem nie wolno ladowac libc")


ctypes.CDLL = zakazane
sys.platform = "darwin"
del os.waitid
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def test_poza_linuksem_przebieg_dziala_bez_odbierania_sierot_i_zwraca_kod_pytesta(tmp_path):
    tresc = (
        "import time\n\nimport pytest\n\n\ndef test_konczy_wlasnym_kodem():\n"
        "    time.sleep(0.5)\n    pytest.exit('koniec', returncode=7)\n"
    )
    wynik = _interpreter(PROGRAM_POZA_LINUKSEM, str(TESTY_PY), "-q", _plik_testow(tmp_path, tresc))
    assert wynik.returncode == 7, wynik.stdout + wynik.stderr


PROGRAM_Z_ODMOWA_PRCTL = """
import ctypes, errno, runpy, sys
from types import SimpleNamespace


def prctl(*_argumenty):
    ctypes.set_errno(errno.EPERM)
    return -1


ctypes.CDLL = lambda *_argumenty, **_nazwane: SimpleNamespace(prctl=prctl)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def test_program_ktoremu_system_odmawia_odbierania_sierot_konczy_kodem_2_z_komunikatem_i_nie_uruchamia_testow(tmp_path):
    plik = _plik_testow(tmp_path, "def test_przechodzi():\n    pass\n")
    wynik = _interpreter(PROGRAM_Z_ODMOWA_PRCTL, str(TESTY_PY), "-q", plik)
    assert wynik.returncode == 2, wynik.stdout + wynik.stderr
    assert wynik.stderr == f"BLAD: {BLAD_PRCTL}\n"
    assert "passed" not in wynik.stdout
