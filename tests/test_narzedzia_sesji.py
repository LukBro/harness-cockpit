"""Narzedzia na powtarzalne momenty sesji ticketu (HAR-240).

`tools/testy.py` (pytest z limitem bez postepu), `tools/po_merge_main.sh` (moment po merge origin/main),
`tools/liczby_raportu.py` (liczby do raportu z pliku i diffu), `tools/oddaj_pr.sh` (moment oddania).

Testy uruchamiaja PRAWDZIWE skrypty. Pytest, alembic i git pracuja wylacznie w mini-repozytoriach w
`tmp_path`; zaslepki w PATH (`gh` i `git`) sluza tylko do obserwacji, czy skrypt wypchnal galaz i
wolal `gh pr create`. Procesy ida przez `tests/procesy.py`.
"""
import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from procesy import (
    LimitCzasuTestu,
    czy_zyje,
    przerwij_sigtermem_sync,
    uruchom_proces_sync,
    sprzatnij_zapamietane,
    zabij_proces,
    zakoncz_proces_sync,
    zapamietaj_proces,
)

KORZEN = Path(__file__).resolve().parents[1]
NARZEDZIA = KORZEN / "tools"
sys.path.insert(0, str(NARZEDZIA))
import _testy_zdarzenia as zdarzenia_py  # noqa: E402
import liczby_raportu as liczby_py  # noqa: E402
import testy as testy_py  # noqa: E402
TESTY_PY = NARZEDZIA / "testy.py"
PO_MERGE = NARZEDZIA / "po_merge_main.sh"
LICZBY = NARZEDZIA / "liczby_raportu.py"
ODDAJ = NARZEDZIA / "oddaj_pr.sh"
LIMIT_SKRYPTU = 240.0
# Znaczniki konfliktu skladane w kodzie: literal na poczatku linii zepsulby etap `konflikt` tego repozytorium.
ZNACZNIK_START = "<" * 7
ZNACZNIK_KONIEC = ">" * 7

ZMIENNE_PYTESTA = ("PYTEST_ADDOPTS", "PYTEST_CURRENT_TEST", "PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT")


def _srodowisko(dodatkowe: dict[str, str] | None = None) -> dict[str, str]:
    srodowisko = {k: v for k, v in os.environ.items() if k not in ZMIENNE_PYTESTA}
    srodowisko.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    srodowisko.update(dodatkowe or {})
    return srodowisko


def _uruchom(
    argv: list[str], cwd: Path, srodowisko: dict[str, str] | None = None, limit: float = LIMIT_SKRYPTU
) -> tuple[int, str, str]:
    nazwa = Path(argv[0]).name
    proc = uruchom_proces_sync(
        argv,
        cwd=cwd,
        env=_srodowisko(srodowisko),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wyjscie, bledy = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        zakoncz_proces_sync(proc, nazwa=nazwa)
        raise LimitCzasuTestu(f"{nazwa}: przekroczono limit {limit} s") from None
    finally:
        zakoncz_proces_sync(proc, nazwa=nazwa)
    return proc.returncode, wyjscie, bledy


def _git(repo: Path, *argumenty: str) -> str:
    kod, wyjscie, bledy = _uruchom(["git", *argumenty], repo, limit=60.0)
    assert kod == 0, f"git {' '.join(argumenty)}: {bledy}"
    return wyjscie


def _poczekaj(warunek, limit: float, nazwa: str) -> None:
    koniec = time.monotonic() + limit
    while not warunek():
        if time.monotonic() >= koniec:
            raise LimitCzasuTestu(f"{nazwa}: przekroczono limit {limit} s")
        time.sleep(0.05)


def _zapisz(repo: Path, sciezka: str, tresc: str) -> None:
    plik = repo / sciezka
    plik.parent.mkdir(parents=True, exist_ok=True)
    plik.write_text(tresc, encoding="utf-8")


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "lukaszbrowar@gmail.com")
    _git(repo, "config", "user.name", "LukBro")


def _commit(repo: Path, komunikat: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", komunikat)


# =============================== tools/testy.py ===============================

PLIK_WISI = """
import os
import pathlib
import threading


def test_poprzedni_przechodzi():
    pass


def test_wisi():
    znacznik = os.environ.get("ZNACZNIK_PID")
    if znacznik:
        pathlib.Path(znacznik).write_text(str(os.getpid()))
    threading.Event().wait()
"""
PLIK_IGNORUJE_SIGTERM = """
import signal
import threading


def test_wisi_i_ignoruje_sigterm():
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    threading.Event().wait()
"""
PLIK_BEZ_ZAWIESZENIA = """
def test_przechodzi():
    pass


def test_drugi_przechodzi():
    pass
"""
LIMIT_BEZ_POSTEPU = 10.0


def _testy(
    tmp_path: Path,
    tresc: str,
    *argumenty: str,
    srodowisko: dict[str, str] | None = None,
    limit_testu: float = LIMIT_SKRYPTU,
):
    plik = tmp_path / "test_przyklad.py"
    plik.write_text(tresc, encoding="utf-8")
    return _uruchom([sys.executable, str(TESTY_PY), *argumenty, str(plik)], KORZEN, srodowisko, limit_testu)


def test_wiszacy_test_konczy_przebieg_kodem_2_w_limicie_i_podaje_swoj_node_id(tmp_path):
    start = time.monotonic()
    kod, wyjscie, _ = _testy(tmp_path, PLIK_WISI, "--bez-postepu", str(LIMIT_BEZ_POSTEPU), "-q")
    czas = time.monotonic() - start
    assert kod == 2
    assert czas < LIMIT_BEZ_POSTEPU + 30
    linie = wyjscie.splitlines()
    assert any(w.endswith("testy.py: brak postepu przez 10 s - przerywam przebieg") for w in linie)
    assert any(wiersz.startswith("zawieszenie: ") and wiersz.endswith("test_przyklad.py::test_wisi") for wiersz in linie)
    assert [wiersz for wiersz in linie if wiersz.startswith("zawieszenie:")] == [wiersz for wiersz in linie if wiersz.endswith("::test_wisi")]


def test_wiszacy_test_ignorujacy_sigterm_jest_dobijany_sigkillem_a_przebieg_konczy_sie_kodem_2(tmp_path):
    kod, wyjscie, _ = _testy(
        tmp_path, PLIK_IGNORUJE_SIGTERM, "--bez-postepu", str(LIMIT_BEZ_POSTEPU), "-q", limit_testu=90.0
    )
    assert kod == 2
    assert any(wiersz.endswith("::test_wisi_i_ignoruje_sigterm") for wiersz in wyjscie.splitlines())


def test_bez_jawnego_limitu_test_trwajacy_kilka_sekund_bez_wyjscia_nie_jest_zawieszeniem(tmp_path):
    kod, wyjscie, _ = _testy(tmp_path, "import time\n\n\ndef test_wolny():\n    time.sleep(4)\n", "-q")
    assert kod == 0
    assert "1 passed" in wyjscie


def test_przebieg_z_samym_wyjsciem_testu_i_bez_nowych_testow_nie_jest_zawieszeniem(tmp_path):
    tresc = (
        "import time\n\n\ndef test_gadatliwy():\n    for i in range(8):\n"
        "        print('linia', i, flush=True)\n        time.sleep(1)\n"
    )
    kod, wyjscie, _ = _testy(tmp_path, tresc, "--bez-postepu", "4", "-q", "-s")
    assert kod == 0
    assert "linia 7" in wyjscie


PLIK_Z_POTOMKIEM = """
import os
import pathlib
import subprocess


def test_zostawia_potomka():
    potomek = subprocess.Popen(["sleep", "600"])
    pathlib.Path(os.environ["ZNACZNIK_PID"]).write_text(str(potomek.pid))
"""


def test_wiszacy_test_pod_xdist_tez_konczy_przebieg_kodem_2_z_node_id(tmp_path):
    kod, wyjscie, _ = _testy(tmp_path, PLIK_WISI, "--bez-postepu", "75", "-q", "-n", "2", limit_testu=200.0)
    assert kod == 2
    zawieszenia = [w for w in wyjscie.splitlines() if w.startswith("zawieszenie:")]
    # node id jest wzgledem wspolnego przodka rootdir i tmp_path: pod /private/tmp (worktree sesji,
    # mutmut) dostaje prefiks katalogu, wiec sprawdzamy jedna linie z nazwa pliku i testu na koncu
    assert len(zawieszenia) == 1
    assert re.fullmatch(r"zawieszenie: (\S+/)?test_przyklad\.py::test_wisi", zawieszenia[0])


def test_wtyczka_zdarzen_dopisuje_start_i_koniec_testu_do_pliku_z_zmiennej(monkeypatch, tmp_path):
    plik = tmp_path / "z.log"
    monkeypatch.setenv(zdarzenia_py.ZMIENNA, str(plik))
    zdarzenia_py.pytest_runtest_logstart("a::x", None)
    zdarzenia_py.pytest_runtest_logfinish("a::x", None)
    zdarzenia_py.pytest_runtest_logstart("a::y", None)
    assert plik.read_text(encoding="utf-8").splitlines() == ["S a::x", "K a::x", "S a::y"]


def test_wtyczka_zdarzen_bez_zmiennej_niczego_nie_zapisuje(monkeypatch, tmp_path):
    monkeypatch.delenv(zdarzenia_py.ZMIENNA, raising=False)
    monkeypatch.chdir(tmp_path)
    zdarzenia_py.pytest_runtest_logstart("a::x", None)
    zdarzenia_py.pytest_runtest_logfinish("a::x", None)
    assert list(tmp_path.iterdir()) == []


def test_wtyczka_zdarzen_pusta_zmienna_to_brak_pliku_zdarzen(monkeypatch, tmp_path):
    monkeypatch.setenv(zdarzenia_py.ZMIENNA, "")
    monkeypatch.chdir(tmp_path)
    zdarzenia_py.pytest_runtest_logstart("a::x", None)
    assert list(tmp_path.iterdir()) == []


def test_potomek_ktory_przezyl_pytesta_nie_zawiesza_skryptu_i_jest_zabity(tmp_path):
    znacznik = tmp_path / "potomek.pid"
    try:
        kod, wyjscie, _ = _testy(
            tmp_path, PLIK_Z_POTOMKIEM, "-q", "-s", srodowisko={"ZNACZNIK_PID": str(znacznik)}, limit_testu=60.0
        )
        assert kod == 0
        assert "1 passed" in wyjscie
        pid = int(znacznik.read_text())
        _poczekaj(lambda: not czy_zyje(pid), 15.0, "koniec potomka pytesta")
    finally:
        if znacznik.exists() and czy_zyje(int(znacznik.read_text())):
            zabij_proces(int(znacznik.read_text()))


PLIK_POTOMEK_W_OSOBNEJ_SESJI = """
import os
import pathlib
import subprocess


def test_zostawia_potomka_w_osobnej_sesji():
    potomek = subprocess.Popen(["sleep", "600"], start_new_session=True)
    pathlib.Path(os.environ["ZNACZNIK_PID"]).write_text(str(potomek.pid))
"""


def test_potomek_w_osobnej_sesji_trzymajacy_potok_nie_blokuje_skryptu_ponad_limit_czekania(tmp_path):
    znacznik = tmp_path / "potomek.pid"
    try:
        start = time.monotonic()
        kod, wyjscie, _ = _testy(
            tmp_path,
            PLIK_POTOMEK_W_OSOBNEJ_SESJI,
            "-q",
            "-s",
            srodowisko={"ZNACZNIK_PID": str(znacznik)},
            limit_testu=60.0,
        )
        assert kod == 0
        assert "1 passed" in wyjscie
        assert time.monotonic() - start < 45
    finally:
        if znacznik.exists() and znacznik.read_text() != "" and czy_zyje(int(znacznik.read_text())):
            zabij_proces(int(znacznik.read_text()))


def test_blad_przy_uruchamianiu_czytnika_zabija_juz_wystartowany_pytest(monkeypatch, tmp_path):
    plik = tmp_path / "test_wisi.py"
    plik.write_text("import threading\n\n\ndef test_wisi():\n    threading.Event().wait()\n", encoding="utf-8")
    uruchomione = []
    prawdziwy_popen = subprocess.Popen

    def popen_z_zapisem(*argumenty, **nazwane):
        proc = prawdziwy_popen(*argumenty, **nazwane)
        uruchomione.append(proc)
        zapamietaj_proces(proc.pid)
        return proc

    def start_pada(_self):
        raise RuntimeError("nie ma watkow")

    sprzatane = []
    prawdziwe_zakonczenie = testy_py._zakoncz_grupe

    def zakonczenie_z_zapisem(proc):
        sprzatane.append(proc)
        prawdziwe_zakonczenie(proc)

    monkeypatch.setattr(testy_py.subprocess, "Popen", popen_z_zapisem)
    monkeypatch.setattr(testy_py.threading.Thread, "start", start_pada)
    monkeypatch.setattr(testy_py, "_zakoncz_grupe", zakonczenie_z_zapisem)
    try:
        with pytest.raises(RuntimeError, match="nie ma watkow"):
            testy_py.uruchom(["-q", str(plik)], 60.0, KORZEN)
        assert sprzatane == uruchomione
        assert uruchomione[0].poll() is not None
    finally:
        sprzatnij_zapamietane()


def test_skrocona_nazwa_opcji_limitu_nie_jest_rozpoznawana_i_idzie_do_pytesta(tmp_path):
    kod, wyjscie, _ = _testy(tmp_path, PLIK_BEZ_ZAWIESZENIA, "--bez-postep", "5", "-q")
    assert kod == 4
    assert "passed" not in wyjscie


def test_blad_pliku_przy_uruchomieniu_to_blad_narzedzia_kod_2_a_nie_traceback(monkeypatch, capsys):
    def pada(*_argumenty, **_nazwane):
        raise PermissionError("brak praw do katalogu tymczasowego")

    monkeypatch.setattr(testy_py.tempfile, "TemporaryDirectory", pada)
    stary = signal.getsignal(signal.SIGTERM)
    try:
        kod = testy_py.main(["-q"])
    finally:
        signal.signal(signal.SIGTERM, stary)
    assert kod == 2
    assert capsys.readouterr().err == "BLAD: operacja na pliku nie powiodla sie: brak praw do katalogu tymczasowego\n"


def test_nieuruchamialny_pytest_to_blad_dziedziny_z_komunikatem(monkeypatch):
    def pada(*_argumenty, **_nazwane):
        raise FileNotFoundError("brak interpretera")

    monkeypatch.setattr(testy_py.subprocess, "Popen", pada)
    with pytest.raises(testy_py.BladNarzedzia, match="nie udalo sie uruchomic pytesta: brak interpretera"):
        testy_py.uruchom(["-q"], 5.0, KORZEN)


def test_srodowisko_pytesta_zachowuje_stary_pythonpath_za_katalogiem_narzedzi(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHONPATH", "/stary/katalog")
    srodowisko = testy_py._srodowisko(tmp_path / "z.log")
    assert srodowisko["PYTHONPATH"] == os.pathsep.join([str(NARZEDZIA), "/stary/katalog"])
    assert srodowisko[testy_py.ZMIENNA_ZDARZEN] == str(tmp_path / "z.log")


def test_srodowisko_pytesta_bez_starego_pythonpath_ma_tylko_katalog_narzedzi(monkeypatch, tmp_path):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    assert testy_py._srodowisko(tmp_path / "z.log")["PYTHONPATH"] == str(NARZEDZIA)
    monkeypatch.setenv("PYTHONPATH", "")
    assert testy_py._srodowisko(tmp_path / "z.log")["PYTHONPATH"] == str(NARZEDZIA)


def test_pomoc_podaje_domyslny_limit_bez_postepu_1200_sekund():
    kod, wyjscie, _ = _uruchom([sys.executable, str(TESTY_PY), "--help"], KORZEN, limit=60.0)
    assert kod == 0
    assert "(domyslnie 1200)" in wyjscie


def _stan_po_zdarzeniach(tmp_path: Path, tresc: bytes, przesuniecie: int = 0):
    plik = tmp_path / "zdarzenia.log"
    plik.write_bytes(tresc)
    stan = testy_py._Stan()
    return testy_py._wczytaj_zdarzenia(plik, przesuniecie, stan), stan.testy_w_toku()


def test_czytnik_zdarzen_pomija_niedokonczona_linie_i_nie_przesuwa_sie_za_nia(tmp_path):
    przesuniecie, w_toku = _stan_po_zdarzeniach(tmp_path, b"S a::pierwszy\nS b::drugi")
    assert (przesuniecie, w_toku) == (len(b"S a::pierwszy\n"), ["a::pierwszy"])


def test_czytnik_zdarzen_bez_ani_jednego_konca_linii_nie_czyta_nic(tmp_path):
    assert _stan_po_zdarzeniach(tmp_path, b"S a::pierwszy") == (0, [])


def test_czytnik_zdarzen_sam_znak_nowej_linii_jest_przeczytana_linia(tmp_path):
    assert _stan_po_zdarzeniach(tmp_path, b"\n") == (1, [])


def test_czytnik_zdarzen_dopisana_koncowka_linii_dopelnia_stan_od_przesuniecia(tmp_path):
    przesuniecie, _ = _stan_po_zdarzeniach(tmp_path, b"S a::pierwszy\nS b::dru")
    przesuniecie, w_toku = _stan_po_zdarzeniach(tmp_path, b"S a::pierwszy\nS b::drugi\n", przesuniecie)
    # Nowy stan czyta tylko to, co lezy za przesunieciem: pierwsza linia nie wraca.
    assert (przesuniecie, w_toku) == (len(b"S a::pierwszy\nS b::drugi\n"), ["b::drugi"])


def test_czytnik_zdarzen_koniec_testu_zdejmuje_go_z_listy_w_toku(tmp_path):
    _, w_toku = _stan_po_zdarzeniach(tmp_path, b"S a::x\nS a::y\nK a::x\n")
    assert w_toku == ["a::y"]


def test_czytnik_zdarzen_koniec_testu_bez_startu_nie_jest_bledem(tmp_path):
    assert _stan_po_zdarzeniach(tmp_path, b"K a::x\n") == (len(b"K a::x\n"), [])


def test_czytnik_zdarzen_pomija_linie_z_nieznanym_znakiem_i_bez_node_id(tmp_path):
    _, w_toku = _stan_po_zdarzeniach(tmp_path, b"S a::x\nX a::x\nS \nS a::swoj\n")
    assert w_toku == ["a::x", "a::swoj"]


def test_ten_sam_plik_bez_zawieszenia_zwraca_kod_pytesta_0(tmp_path):
    kod, wyjscie, _ = _testy(tmp_path, PLIK_BEZ_ZAWIESZENIA, "-q")
    assert kod == 0
    assert "2 passed" in wyjscie
    assert "zawieszenie" not in wyjscie


def test_czerwony_test_zwraca_kod_pytesta_1_a_nie_kod_zawieszenia(tmp_path):
    kod, wyjscie, _ = _testy(tmp_path, "def test_czerwony():\n    assert 1 == 2\n", "-q")
    assert kod == 1
    assert "1 failed" in wyjscie


def test_argumenty_pytesta_ida_do_pytesta_bez_zmian(tmp_path):
    kod, wyjscie, _ = _testy(tmp_path, PLIK_BEZ_ZAWIESZENIA, "-q", "-k", "test_drugi")
    assert kod == 0
    assert "1 passed, 1 deselected" in wyjscie


def test_przebieg_z_postepem_dluzszy_niz_limit_nie_jest_przerywany(tmp_path):
    tresc = "import time\n\n" + "".join(
        f"def test_krok_{i}():\n    time.sleep(2.5)\n\n" for i in range(4)
    )
    kod, wyjscie, _ = _testy(tmp_path, tresc, "--bez-postepu", "6", "-q")
    assert kod == 0
    assert "4 passed" in wyjscie


def test_zawieszenie_przy_zbieraniu_testow_mowi_ze_zaden_test_nie_byl_w_toku(tmp_path):
    kod, wyjscie, _ = _testy(
        tmp_path, "import threading\nthreading.Event().wait()\n", "--bez-postepu", str(LIMIT_BEZ_POSTEPU), "-q"
    )
    assert kod == 2
    assert "zawieszenie: brak testu w toku" in wyjscie


def test_pytest_zabity_sygnalem_to_blad_narzedzia_a_nie_zielony_wynik(tmp_path):
    kod, _, bledy = _testy(
        tmp_path, "import signal\n\n\ndef test_ginie():\n    signal.raise_signal(signal.SIGKILL)\n", "-q"
    )
    assert kod == 2
    assert "BLAD: pytest zabity sygnalem 9" in bledy


@pytest.mark.parametrize("limit", ["abc", "0", "-3", "inf", "nan"])
def test_limit_bez_postepu_ktory_nie_jest_skonczona_liczba_dodatnia_jest_odrzucony(tmp_path, limit):
    kod, wyjscie, bledy = _testy(tmp_path, PLIK_BEZ_ZAWIESZENIA, f"--bez-postepu={limit}", "-q")
    assert kod == 2
    assert "--bez-postepu musi byc" in bledy
    assert "passed" not in wyjscie


def test_sigterm_do_skryptu_zabija_grupe_pytesta_i_konczy_kodem_143(tmp_path):
    plik = tmp_path / "test_przyklad.py"
    plik.write_text(PLIK_WISI, encoding="utf-8")
    znacznik = tmp_path / "pid.txt"
    proc = uruchom_proces_sync(
        [sys.executable, str(TESTY_PY), "-q", str(plik)],
        cwd=KORZEN,
        env=_srodowisko({"ZNACZNIK_PID": str(znacznik)}),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _poczekaj(lambda: znacznik.exists() and znacznik.read_text() != "", 120.0, "start wiszacego testu")
        pid_pytesta = int(znacznik.read_text())
        assert czy_zyje(pid_pytesta)
        przerwij_sigtermem_sync(proc, nazwa="testy.py", limit=60.0)
        assert proc.returncode == 143
        _poczekaj(lambda: not czy_zyje(pid_pytesta), 30.0, "koniec pytesta po SIGTERM do skryptu")
    finally:
        zakoncz_proces_sync(proc, nazwa="testy.py")
        if znacznik.exists() and znacznik.read_text() != "" and czy_zyje(int(znacznik.read_text())):
            zabij_proces(int(znacznik.read_text()))  # pytest, ktory przezyl skrypt (tylko gdy test oblal)


# ----------------------- tools/testy.py --dotkniete (HAR-256) -----------------------
# Mini-projekt zyje w tmp_path z wlasnym repo git; `testy.KORZEN` podmienia maly skrypt-opakowanie,
# bo narzedzie uruchamia pytesta w swoim korzeniu. Baza testmona (TESTMON_DATAFILE) lezy poza repo.

MINI_KOD = """
def dodaj(a, b):
    return a + b


def odejmij(a, b):
    return a - b


def pomnoz(a, b):
    return a * b
"""
MINI_TESTY = """
from mini import dodaj, odejmij, pomnoz


def test_dodawanie_liczb_dodatnich():
    assert dodaj(1, 2) == 3


def test_dodawanie_liczb_ujemnych():
    assert dodaj(-1, -2) == -3


def test_odejmowanie_liczb_dodatnich():
    assert odejmij(3, 1) == 2


def test_odejmowanie_do_ujemnej():
    assert odejmij(1, 3) == -2


def test_mnozenie_liczb_dodatnich():
    assert pomnoz(2, 3) == 6


def test_mnozenie_przez_zero():
    assert pomnoz(5, 0) == 0


def test_niezalezny_od_kodu():
    assert True


def test_drugi_niezalezny_od_kodu():
    assert 1 == 1
"""
OPAKOWANIE = """
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import testy

testy.KORZEN = Path(sys.argv[2])
sys.exit(testy.main(sys.argv[3:]))
"""


def _mini_projekt(tmp_path: Path, testy: str = MINI_TESTY) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _zapisz(repo, "mini.py", MINI_KOD)
    _zapisz(repo, "test_mini.py", testy)
    _zapisz(repo, "pytest.ini", "[pytest]\npythonpath = .\n")
    _commit(repo, "start")
    return repo


def _testy_dotkniete(
    repo: Path,
    *argumenty: str,
    baza: Path | None,
    limit_testu: float = LIMIT_SKRYPTU,
    dom: Path | None = None,
    srodowisko: dict[str, str] | None = None,
):
    opakowanie = repo.parent / "opakowanie.py"
    opakowanie.write_text(OPAKOWANIE, encoding="utf-8")
    # `_uruchom` dokleja srodowisko procesu testow, wiec samo usuniecie zmiennej by jej nie wylaczylo: pusta wartosc
    # to "brak" dla narzedzia (`or domyslna`), a podana jawnie baza ma pierwszenstwo.
    env = {"TESTMON_DATAFILE": str(baza) if baza is not None else ""}
    if dom is not None:
        env["HOME"] = str(dom)
    env.update(srodowisko or {})
    return _uruchom(
        [sys.executable, str(opakowanie), str(NARZEDZIA), str(repo), "--dotkniete", *argumenty],
        KORZEN,
        env,
        limit_testu,
    )


def _liczba(wyjscie: str, etykieta: str) -> int:
    trafienia = re.findall(rf"{etykieta}: (\d+)", wyjscie)
    assert len(trafienia) == 1, wyjscie
    return int(trafienia[0])


def test_po_zmianie_jednej_funkcji_dotkniete_uruchamiaja_tylko_pokrywajace_ja_testy_i_podaja_liczby(tmp_path):
    repo = _mini_projekt(tmp_path)
    baza = tmp_path / "baza.db"
    kod, wyjscie, _ = _testy_dotkniete(repo, "-q", baza=baza)
    assert kod == 0, wyjscie
    assert baza.exists()
    _zapisz(repo, "mini.py", MINI_KOD.replace("return a + b", "return b + a"))
    kod, wyjscie, _ = _testy_dotkniete(repo, "-q", baza=baza)
    assert kod == 0, wyjscie
    wybrane, pominiete = _liczba(wyjscie, "wybrane"), _liczba(wyjscie, "pominiete")
    assert wybrane == 2  # tylko dwa testy dodawania pokrywaja `dodaj`
    assert pominiete == 6
    assert wybrane < (wybrane + pominiete) / 2


def test_dotkniete_uruchamiaja_test_pokrywajacy_zmieniona_funkcje_i_pomijaja_reszte(tmp_path):
    repo = _mini_projekt(tmp_path)
    baza = tmp_path / "baza.db"
    _testy_dotkniete(repo, "-q", baza=baza)
    _zapisz(repo, "mini.py", MINI_KOD.replace("return a * b", "return b * a"))
    kod, wyjscie, _ = _testy_dotkniete(repo, "-v", baza=baza)
    assert kod == 0, wyjscie
    assert "test_mnozenie_przez_zero" in wyjscie
    assert "test_dodawanie_liczb_dodatnich" not in wyjscie


def test_baza_testmona_domyslnie_nie_laduje_w_repozytorium_uzytkownika(tmp_path):
    repo = _mini_projekt(tmp_path)
    dom = tmp_path / "dom"
    kod, wyjscie, _ = _testy_dotkniete(repo, "-q", baza=None, dom=dom)
    assert kod == 0, wyjscie
    assert [p.name for p in repo.iterdir() if p.name.startswith(".testmondata")] == []
    skrot = hashlib.sha1(str(repo).encode()).hexdigest()[:12]
    baza = dom / ".cache" / "cockpit-testy" / skrot / ".testmondata"
    assert baza.is_file()
    assert testy_py.plik_odcisku(baza).is_file()


# --- wyzwalacze pelnego przebiegu: czysta funkcja `powod_pelnego_przebiegu` ---------------------
# Kontrakt: powod_pelnego_przebiegu(korzen, baza, *, wersje=None) -> str | None (powod albo None),
# zapisz_odcisk(korzen, baza, *, wersje=None) zapisuje odcisk plikow-wyzwalaczy w plik_odcisku(baza),
# obok bazy. `wersje` to mapa nazwa -> wersja zaleznosci (domyslnie z importlib.metadata).

WERSJE = {"pytest": "8.4.0", "ruamel.yaml": "0.18.0"}


def _zdrowy_stan(tmp_path: Path) -> tuple[Path, Path]:
    import sqlite3

    repo = tmp_path / "repo"
    _zapisz(repo, "conftest.py", "# a\n")
    _zapisz(repo, "tests/conftest.py", "# a\n")
    _zapisz(repo, "tests/test_x.py", "# a\n")
    _zapisz(repo, "pyproject.toml", "[project]\nname='x'\n")
    _zapisz(repo, "alembic/versions/0001.py", "# a\n")
    _zapisz(repo, ".cockpit/project.yaml", "a: 1\n")
    _zapisz(repo, "deploy/compose.yaml", "a: 1\n")
    _zapisz(repo, "web/openapi.json", "{}\n")
    _zapisz(repo, "src/modul.py", "x = 1\n")
    baza = tmp_path / "baza" / "testmon.db"
    baza.parent.mkdir()
    polaczenie = sqlite3.connect(baza)
    try:
        with polaczenie:
            polaczenie.execute("create table node (id integer primary key)")
    finally:
        polaczenie.close()
    testy_py.zapisz_odcisk(repo, baza, wersje=WERSJE)
    return repo, baza


def test_niezmienione_wyzwalacze_i_zdrowa_baza_nie_wymuszaja_pelnego_przebiegu(tmp_path):
    repo, baza = _zdrowy_stan(tmp_path)
    assert testy_py.powod_pelnego_przebiegu(repo, baza, wersje=WERSJE) is None


def test_zmiana_zwyklego_pliku_zrodlowego_nie_wymusza_pelnego_przebiegu(tmp_path):
    repo, baza = _zdrowy_stan(tmp_path)
    _zapisz(repo, "src/modul.py", "x = 2\n")
    assert testy_py.powod_pelnego_przebiegu(repo, baza, wersje=WERSJE) is None


def test_odcisk_wyzwalaczy_lezy_obok_bazy_testmona_poza_repozytorium(tmp_path):
    repo, baza = _zdrowy_stan(tmp_path)
    odcisk = testy_py.plik_odcisku(baza)
    assert odcisk.parent == baza.parent
    assert odcisk.is_file()
    assert repo not in odcisk.parents


def test_brak_bazy_testmona_wymusza_pelny_przebieg(tmp_path):
    repo, baza = _zdrowy_stan(tmp_path)
    baza.unlink()
    assert "brak bazy" in testy_py.powod_pelnego_przebiegu(repo, baza, wersje=WERSJE)


def test_uszkodzona_baza_testmona_wymusza_pelny_przebieg(tmp_path):
    repo, baza = _zdrowy_stan(tmp_path)
    baza.write_bytes(b"to nie jest sqlite" * 50)
    assert "uszkodzona" in testy_py.powod_pelnego_przebiegu(repo, baza, wersje=WERSJE)


def test_brak_odcisku_wyzwalaczy_wymusza_pelny_przebieg(tmp_path):
    repo, baza = _zdrowy_stan(tmp_path)
    testy_py.plik_odcisku(baza).unlink()
    assert "odcisk" in testy_py.powod_pelnego_przebiegu(repo, baza, wersje=WERSJE)


def test_zmiana_conftest_wymusza_pelny_przebieg(tmp_path):
    repo, baza = _zdrowy_stan(tmp_path)
    _zapisz(repo, "tests/conftest.py", "# b\n")
    assert "conftest.py" in testy_py.powod_pelnego_przebiegu(repo, baza, wersje=WERSJE)


def test_zmiana_conftest_w_korzeniu_wymusza_pelny_przebieg(tmp_path):
    repo, baza = _zdrowy_stan(tmp_path)
    _zapisz(repo, "conftest.py", "# b\n")
    assert "conftest.py" in testy_py.powod_pelnego_przebiegu(repo, baza, wersje=WERSJE)


def test_zmiana_zwyklego_testu_w_katalogu_tests_nie_wymusza_pelnego_przebiegu(tmp_path):
    repo, baza = _zdrowy_stan(tmp_path)
    _zapisz(repo, "tests/test_x.py", "# b\n")
    assert testy_py.powod_pelnego_przebiegu(repo, baza, wersje=WERSJE) is None


def test_baza_testmona_bez_ani_jednej_tabeli_jest_uznana_za_uszkodzona(tmp_path):
    repo, baza = _zdrowy_stan(tmp_path)
    baza.write_bytes(b"")  # zerobajtowy plik to poprawna, pusta baza sqlite: 0 tabel
    assert "uszkodzona" in testy_py.powod_pelnego_przebiegu(repo, baza, wersje=WERSJE)


def test_zmiana_pyproject_wymusza_pelny_przebieg(tmp_path):
    repo, baza = _zdrowy_stan(tmp_path)
    _zapisz(repo, "pyproject.toml", "[project]\nname='y'\n")
    assert "pyproject.toml" in testy_py.powod_pelnego_przebiegu(repo, baza, wersje=WERSJE)


def test_zmiana_migracji_alembic_wymusza_pelny_przebieg(tmp_path):
    repo, baza = _zdrowy_stan(tmp_path)
    _zapisz(repo, "alembic/versions/0001.py", "# b\n")
    assert "alembic" in testy_py.powod_pelnego_przebiegu(repo, baza, wersje=WERSJE)


def test_nowa_migracja_alembic_wymusza_pelny_przebieg(tmp_path):
    repo, baza = _zdrowy_stan(tmp_path)
    _zapisz(repo, "alembic/versions/0002.py", "# nowy\n")
    assert "alembic" in testy_py.powod_pelnego_przebiegu(repo, baza, wersje=WERSJE)


def test_zmiana_konfiguracji_cockpit_wymusza_pelny_przebieg(tmp_path):
    repo, baza = _zdrowy_stan(tmp_path)
    _zapisz(repo, ".cockpit/project.yaml", "a: 2\n")
    assert ".cockpit" in testy_py.powod_pelnego_przebiegu(repo, baza, wersje=WERSJE)


def test_zmiana_plikow_deploy_wymusza_pelny_przebieg(tmp_path):
    repo, baza = _zdrowy_stan(tmp_path)
    _zapisz(repo, "deploy/compose.yaml", "a: 2\n")
    assert "deploy" in testy_py.powod_pelnego_przebiegu(repo, baza, wersje=WERSJE)


def test_zmiana_openapi_frontu_wymusza_pelny_przebieg(tmp_path):
    repo, baza = _zdrowy_stan(tmp_path)
    _zapisz(repo, "web/openapi.json", '{"a": 1}\n')
    assert "web/openapi.json" in testy_py.powod_pelnego_przebiegu(repo, baza, wersje=WERSJE)


def test_zmiana_wersji_zaleznosci_wymusza_pelny_przebieg(tmp_path):
    repo, baza = _zdrowy_stan(tmp_path)
    nowe = {**WERSJE, "pytest": "8.5.0"}
    assert "zaleznosci" in testy_py.powod_pelnego_przebiegu(repo, baza, wersje=nowe)


def test_dodanie_zaleznosci_wymusza_pelny_przebieg(tmp_path):
    repo, baza = _zdrowy_stan(tmp_path)
    nowe = {**WERSJE, "nowa-biblioteka": "1.0"}
    assert "zaleznosci" in testy_py.powod_pelnego_przebiegu(repo, baza, wersje=nowe)


# --- brak dowodu -----------------------------------------------------------------------------


def test_zero_wybranych_testow_przy_niepustym_diffie_py_to_blad_brak_dowodu_a_nie_sukces(tmp_path):
    repo = _mini_projekt(tmp_path)
    baza = tmp_path / "baza.db"
    kod, wyjscie, _ = _testy_dotkniete(repo, "-q", baza=baza)
    assert kod == 0, wyjscie
    # plik .py w diffie, ktorego zaden test nie importuje: testmon nie wybiera niczego (kod 5 pytesta)
    _zapisz(repo, "nikt_nie_uzywa.py", "x = 1\n")
    _git(repo, "add", "-N", "nikt_nie_uzywa.py")
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-q", baza=baza)
    assert kod == testy_py.KOD_BLEDU, wyjscie
    assert kod not in (0, 5)
    assert "brak dowodu" in wyjscie + bledy


def test_zero_wybranych_testow_przy_pustym_diffie_py_nie_jest_bledem_brak_dowodu(tmp_path):
    repo = _mini_projekt(tmp_path)
    baza = tmp_path / "baza.db"
    _testy_dotkniete(repo, "-q", baza=baza)
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-q", baza=baza)
    assert "brak dowodu" not in wyjscie + bledy
    assert kod == 0, wyjscie


def test_projekt_bez_zadnych_testow_i_bez_diffu_py_konczy_dotkniete_kodem_0_a_nie_5_pytesta(tmp_path):
    repo = _mini_projekt(tmp_path)
    baza = tmp_path / "baza.db"
    kod, wyjscie, _ = _testy_dotkniete(repo, "-q", baza=baza)
    assert kod == 0, wyjscie
    (repo / "test_mini.py").unlink()
    _commit(repo, "usuniecie testow")
    assert _git(repo, "diff", "--name-only", "HEAD") == ""
    # baza i odcisk sa zdrowe: o wyniku decyduje wylacznie gałąź pustego diffu, nie pelny przebieg
    assert testy_py.powod_pelnego_przebiegu(repo, baza) is None
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-q", baza=baza)
    assert "brak dowodu" not in wyjscie + bledy
    assert kod == 0, wyjscie


def test_wiszacy_test_w_trybie_dotkniete_pod_xdist_n_4_konczy_przebieg_kodem_2_z_node_id(tmp_path):
    repo = _mini_projekt(tmp_path, PLIK_WISI)
    baza = tmp_path / "baza.db"
    kod, wyjscie, _ = _testy_dotkniete(repo, "--bez-postepu", "75", "-q", "-n", "4", baza=baza, limit_testu=200.0)
    assert kod == 2
    zawieszenia = [w for w in wyjscie.splitlines() if w.startswith("zawieszenie:")]
    assert len(zawieszenia) == 1
    assert re.fullmatch(r"zawieszenie: (\S+/)?test_mini\.py::test_wisi", zawieszenia[0])


# --- HAR-256, runda 2: kazda galaz tryba --dotkniete ma test, w ktorym tylko ona decyduje ---------

TESTY_CZERWONE = MINI_TESTY + "\n\ndef test_czerwony():\n    assert False\n"
BEZ_ROOTA = pytest.mark.skipif(os.geteuid() == 0, reason="root ignoruje prawa dostepu do plikow")


def test_diff_w_repozytorium_bez_commita_to_blad_brak_dowodu_a_nie_pusta_lista_zmian(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    with pytest.raises(testy_py.BladNarzedzia, match=r"brak dowodu: git diff zakonczyl sie kodem [1-9]\d*: .*HEAD"):
        testy_py._zmienione_py(repo)


def test_zmienione_pliki_to_tylko_te_z_rozszerzeniem_py(tmp_path):
    repo = _mini_projekt(tmp_path)
    _zapisz(repo, "notatki.txt", "a\n")
    _commit(repo, "notatki")
    _zapisz(repo, "notatki.txt", "b\n")
    _zapisz(repo, "mini.py", MINI_KOD + "\n# zmiana\n")
    assert testy_py._zmienione_py(repo) == ["mini.py"]


def test_git_ktorego_nie_da_sie_uruchomic_to_blad_brak_dowodu_przy_odczycie_diffu(monkeypatch, tmp_path):
    def pada(*_argumenty, **_nazwane):
        raise FileNotFoundError("brak gita")

    monkeypatch.setattr(testy_py.subprocess, "run", pada)
    with pytest.raises(testy_py.BladNarzedzia, match="brak dowodu: nie udalo sie odczytac diffu: brak gita"):
        testy_py._zmienione_py(tmp_path)


def test_git_diff_przekraczajacy_limit_czasu_to_blad_brak_dowodu_przy_odczycie_diffu(monkeypatch, tmp_path):
    def pada(*_argumenty, **_nazwane):
        raise subprocess.TimeoutExpired("git", 60)

    monkeypatch.setattr(testy_py.subprocess, "run", pada)
    with pytest.raises(testy_py.BladNarzedzia, match="brak dowodu: nie udalo sie odczytac diffu: .*60"):
        testy_py._zmienione_py(tmp_path)


def test_pelny_przebieg_bez_podsumowania_z_liczba_testow_to_blad_brak_dowodu_a_nie_zielony_wynik(tmp_path):
    repo = _mini_projekt(tmp_path)
    baza = tmp_path / "baza.db"
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-qq", baza=baza)
    assert kod == testy_py.KOD_BLEDU, wyjscie + bledy
    assert "brak dowodu: pelny przebieg bez podsumowania z liczba testow" in bledy
    assert not testy_py.plik_odcisku(baza).exists()  # brak dowodu nie zapisuje odcisku


def test_czerwony_przebieg_bez_podsumowania_zwraca_kod_pytesta_a_nie_blad_brak_dowodu(tmp_path):
    repo = _mini_projekt(tmp_path, TESTY_CZERWONE)
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-qq", baza=tmp_path / "baza.db")
    assert kod == 1, wyjscie + bledy
    assert "brak dowodu" not in wyjscie + bledy


def test_baza_testmona_wewnatrz_repozytorium_to_blad_narzedzia_a_nie_przebieg(tmp_path):
    repo = _mini_projekt(tmp_path)
    baza = repo / "wewnatrz" / "baza.db"
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-q", baza=baza)
    assert kod == testy_py.KOD_BLEDU, wyjscie + bledy
    assert "baza testmona nie moze lezec w repozytorium" in bledy
    assert not baza.parent.exists()  # nic nie zostalo utworzone w repozytorium


def test_zmiana_pyproject_po_zbudowaniu_bazy_uruchamia_wszystkie_testy_a_nie_zero(tmp_path):
    repo = _mini_projekt(tmp_path)
    _zapisz(repo, "pyproject.toml", "[project]\nname='a'\n")
    baza = tmp_path / "baza.db"
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-q", baza=baza)
    assert kod == 0, wyjscie + bledy
    _zapisz(repo, "pyproject.toml", "[project]\nname='b'\n")
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-q", baza=baza)
    assert kod == 0, wyjscie + bledy
    assert "pelny przebieg - zmiana: pyproject.toml" in wyjscie
    assert (_liczba(wyjscie, "wybrane"), _liczba(wyjscie, "pominiete")) == (8, 0)


def test_uszkodzona_baza_jest_usuwana_i_odbudowana_a_przebieg_pelny_konczy_sie_kodem_0(tmp_path):
    repo = _mini_projekt(tmp_path)
    baza = tmp_path / "baza.db"
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-q", baza=baza)
    assert kod == 0, wyjscie + bledy
    baza.write_bytes(b"to nie jest sqlite" * 50)
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-q", baza=baza)
    assert kod == 0, wyjscie + bledy
    assert "pelny przebieg - baza testmona uszkodzona" in wyjscie
    assert _liczba(wyjscie, "wybrane") == 8
    assert testy_py._baza_zdrowa(baza)


def test_przebieg_z_czerwonym_testem_nie_zapisuje_odcisku_wiec_nastepny_przebieg_jest_pelny(tmp_path):
    repo = _mini_projekt(tmp_path, TESTY_CZERWONE)
    baza = tmp_path / "baza.db"
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-q", baza=baza)
    assert kod == 1, wyjscie + bledy
    assert baza.is_file()
    assert not testy_py.plik_odcisku(baza).exists()
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-q", baza=baza)
    assert kod == 1, wyjscie + bledy
    assert "pelny przebieg - brak odcisku plikow-wyzwalaczy" in wyjscie


def test_katalog_bazy_testmona_ktorego_nie_da_sie_utworzyc_to_blad_narzedzia_z_nazwa_katalogu(tmp_path):
    repo = _mini_projekt(tmp_path)
    plik = tmp_path / "zwykly_plik"
    plik.write_text("to nie katalog", encoding="utf-8")
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-q", baza=plik / "baza.db")
    assert kod == testy_py.KOD_BLEDU, wyjscie + bledy
    katalog = plik.resolve()
    assert f"BLAD: nie mozna utworzyc katalogu bazy testmona {katalog}: [Errno 17] File exists: '{katalog}'" in bledy


@BEZ_ROOTA
def test_uszkodzonej_bazy_testmona_ktorej_nie_da_sie_usunac_to_blad_narzedzia_a_baza_zostaje(tmp_path):
    repo = _mini_projekt(tmp_path)
    katalog = tmp_path / "bazy"
    katalog.mkdir()
    baza = katalog / "baza.db"
    baza.write_bytes(b"to nie jest sqlite" * 50)
    katalog.chmod(0o555)
    try:
        kod, wyjscie, bledy = _testy_dotkniete(repo, "-q", baza=baza)
    finally:
        katalog.chmod(0o755)
    assert kod == testy_py.KOD_BLEDU, wyjscie + bledy
    pelna = baza.resolve()
    assert f"BLAD: nie mozna usunac uszkodzonej bazy testmona {pelna}: [Errno 13] Permission denied: '{pelna}'" in bledy
    assert baza.read_bytes() == b"to nie jest sqlite" * 50


@pytest.mark.parametrize("tresc", [b"{nie json", b"", b"\xff\xfe\xfd"])
def test_odcisk_ktorego_nie_da_sie_odczytac_jako_json_wymusza_pelny_przebieg_a_nie_traceback(tmp_path, tresc):
    repo, baza = _zdrowy_stan(tmp_path)
    testy_py.plik_odcisku(baza).write_bytes(tresc)
    assert testy_py.powod_pelnego_przebiegu(repo, baza, wersje=WERSJE) == "brak odcisku plikow-wyzwalaczy"


@pytest.mark.parametrize("tresc", [b"[]", b'"x"', b"null", b"1"])
def test_odcisk_ktory_nie_jest_slownikiem_wymusza_pelny_przebieg_a_nie_traceback(tmp_path, tresc):
    repo, baza = _zdrowy_stan(tmp_path)
    testy_py.plik_odcisku(baza).write_bytes(tresc)
    assert testy_py.powod_pelnego_przebiegu(repo, baza, wersje=WERSJE) == "brak odcisku plikow-wyzwalaczy"


def test_podsumowanie_z_jednym_ostrzezeniem_i_pominietymi_to_zero_wybranych_i_szesc_pominietych():
    assert testy_py._liczby_z_podsumowania(["6 deselected, 1 warning in 0.1s\n"]) == (0, 6)


def test_podsumowanie_z_ostrzezeniami_w_liczbie_mnogiej_nie_liczy_ich_jako_wybranych():
    assert testy_py._liczby_z_podsumowania(["2 passed, 3 warnings in 0.1s\n"]) == (2, None)


def test_podsumowanie_sumuje_rodzaje_wybranych_osobno_od_pominietych_i_ignoruje_ozdobniki():
    ogon = ["=== 2 passed, 1 failed, 4 deselected in 61.25s (0:01:01) ===\n", "inna linia\n"]
    assert testy_py._liczby_z_podsumowania(ogon) == (3, 4)


def test_podsumowanie_bierze_ostatnia_linie_z_liczbami_a_ogon_bez_podsumowania_to_brak_wyniku():
    assert testy_py._liczby_z_podsumowania(["1 passed in 0.1s\n", "5 failed in 0.2s\n"]) == (5, None)
    assert testy_py._liczby_z_podsumowania(["nic tu nie ma\n"]) is None


def test_podsumowanie_z_nieznanym_rodzajem_to_blad_brak_dowodu_a_nie_pominiecie_liczby():
    with pytest.raises(testy_py.BladNarzedzia, match="brak dowodu: nieznany rodzaj w podsumowaniu pytesta: 'dziwne'"):
        testy_py._liczby_z_podsumowania(["2 passed, 3 dziwne in 0.1s\n"])


@pytest.mark.parametrize("rodzaj", sorted({"passed", "failed", "error", "errors", "skipped", "xfailed", "xpassed"}))
def test_podsumowanie_liczy_kazdy_znany_rodzaj_wyniku_jako_wybrane(rodzaj):
    assert testy_py._liczby_z_podsumowania([f"3 {rodzaj} in 0.1s\n"]) == (3, None)


@pytest.mark.parametrize("rodzaj", ["warning", "warnings", "rerun", "reruns"])
def test_podsumowanie_nie_liczy_ostrzezen_i_ponowien_jako_wybranych(rodzaj):
    assert testy_py._liczby_z_podsumowania([f"2 passed, 3 {rodzaj} in 0.1s\n"]) == (2, None)


def test_plik_w_pycache_migracji_nie_zmienia_odcisku_a_zwykly_plik_migracji_tak(tmp_path):
    repo, baza = _zdrowy_stan(tmp_path)
    _zapisz(repo, "alembic/versions/__pycache__/0001.cpython-314.pyc", "bajty")
    assert testy_py.powod_pelnego_przebiegu(repo, baza, wersje=WERSJE) is None
    _zapisz(repo, "alembic/versions/0002.py", "# nowy\n")
    assert "alembic" in testy_py.powod_pelnego_przebiegu(repo, baza, wersje=WERSJE)


@BEZ_ROOTA
def test_nieczytelny_katalog_migracji_to_blad_narzedzia_a_nie_pusty_odcisk(tmp_path):
    repo, baza = _zdrowy_stan(tmp_path)
    (repo / "alembic").chmod(0o000)
    try:
        with pytest.raises(testy_py.BladNarzedzia) as blad:
            testy_py.powod_pelnego_przebiegu(repo, baza, wersje=WERSJE)
        katalog = repo / "alembic"
        assert str(blad.value) == f"nie mozna przejrzec katalogu wyzwalaczy: [Errno 13] Permission denied: '{katalog}'"
    finally:
        (repo / "alembic").chmod(0o755)


def test_wyzwalacz_ktorego_nie_da_sie_sprawdzic_to_blad_narzedzia_z_nazwa_sciezki(monkeypatch, tmp_path):
    def pada(self, *_argumenty, **_nazwane):
        raise PermissionError("brak praw")

    with monkeypatch.context() as m:
        m.setattr(Path, "is_dir", pada)
        with pytest.raises(testy_py.BladNarzedzia) as blad:
            testy_py._pliki(tmp_path, "alembic", ("alembic",))
    assert str(blad.value) == f"nie mozna sprawdzic wyzwalacza {tmp_path / 'alembic'}: brak praw"


@BEZ_ROOTA
def test_nieczytelny_plik_wyzwalacza_to_blad_narzedzia_z_nazwa_pliku(tmp_path):
    repo, baza = _zdrowy_stan(tmp_path)
    (repo / "pyproject.toml").chmod(0o000)
    try:
        with pytest.raises(testy_py.BladNarzedzia) as blad:
            testy_py.powod_pelnego_przebiegu(repo, baza, wersje=WERSJE)
        plik = repo / "pyproject.toml"
        assert str(blad.value) == f"nie mozna odczytac wyzwalacza {plik}: [Errno 13] Permission denied: '{plik}'"
    finally:
        (repo / "pyproject.toml").chmod(0o644)


def test_odcisku_nie_da_sie_zapisac_gdy_katalog_bazy_nie_istnieje_to_blad_narzedzia(tmp_path):
    repo, _ = _zdrowy_stan(tmp_path)
    baza = tmp_path / "brak" / "baza.db"
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        testy_py.zapisz_odcisk(repo, baza, wersje=WERSJE)
    odcisk = testy_py.plik_odcisku(baza)
    assert str(blad.value) == f"nie mozna zapisac odcisku wyzwalaczy: [Errno 2] No such file or directory: '{odcisk}'"


def test_przeniesiona_migracja_o_tej_samej_tresci_zmienia_odcisk_bo_nazwa_pliku_wchodzi_do_skrotu(tmp_path):
    repo, baza = _zdrowy_stan(tmp_path)
    (repo / "alembic/versions/0001.py").rename(repo / "alembic/versions/0003.py")
    assert "alembic" in testy_py.powod_pelnego_przebiegu(repo, baza, wersje=WERSJE)


def test_kolejnosc_przegladania_katalogow_nie_zmienia_odcisku_wyzwalaczy(monkeypatch, tmp_path):
    repo, baza = _zdrowy_stan(tmp_path)
    _zapisz(repo, "alembic/versions/0002.py", "# b\n")
    _zapisz(repo, "alembic/versions/0003.py", "# c\n")
    _zapisz(repo, "alembic/inne/0004.py", "# d\n")
    oryginal = os.walk

    def walk_w_kolejnosci(odwrotnie):
        def przegladaj(*argumenty, **nazwane):
            for katalog, podkatalogi, pliki in oryginal(*argumenty, **nazwane):
                podkatalogi.sort(reverse=odwrotnie)
                pliki.sort(reverse=odwrotnie)
                yield katalog, podkatalogi, pliki

        return przegladaj

    with monkeypatch.context() as m:
        m.setattr(testy_py.os, "walk", walk_w_kolejnosci(False))
        testy_py.zapisz_odcisk(repo, baza, wersje=WERSJE)
        m.setattr(testy_py.os, "walk", walk_w_kolejnosci(True))
        assert testy_py.powod_pelnego_przebiegu(repo, baza, wersje=WERSJE) is None


def test_zdrowa_baza_testmona_przezywa_pelny_przebieg_po_zmianie_wyzwalacza_i_nie_jest_usuwana(tmp_path):
    import sqlite3

    repo = _mini_projekt(tmp_path)
    _zapisz(repo, "pyproject.toml", "[project]\nname='a'\n")
    baza = tmp_path / "baza.db"
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-q", baza=baza)
    assert kod == 0, wyjscie + bledy
    polaczenie = sqlite3.connect(baza)
    try:
        with polaczenie:
            polaczenie.execute("create table znacznik_testu (id integer)")
    finally:
        polaczenie.close()
    _zapisz(repo, "pyproject.toml", "[project]\nname='b'\n")
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-q", baza=baza)
    assert kod == 0, wyjscie + bledy
    assert "pelny przebieg - zmiana: pyproject.toml" in wyjscie
    polaczenie = sqlite3.connect(baza)
    try:
        tabele = {w[0] for w in polaczenie.execute("select name from sqlite_master")}
    finally:
        polaczenie.close()
    assert "znacznik_testu" in tabele  # baza byla zdrowa, wiec nie zostala usunieta


# --- addopts projektu, e2e i opcje wylaczajace selekcje testmona ------------------------------------

def _repo_z_prawdziwym_pyproject(tmp_path: Path) -> Path:
    """Mini-repo z kopia pyproject.toml projektu (addopts = "-m 'not e2e'"); kod w src/, testy w tests/."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    tresc = (KORZEN / "pyproject.toml").read_text(encoding="utf-8")
    assert "addopts = \"-m 'not e2e'\"" in tresc  # bez tego test nie odtwarza defektu
    _zapisz(repo, "pyproject.toml", tresc)
    _zapisz(repo, "src/mini.py", MINI_KOD)
    _zapisz(repo, "tests/test_mini.py", MINI_TESTY)
    _commit(repo, "start")
    return repo


def test_addopts_projektu_z_wyborem_znacznikow_nie_wylacza_selekcji_dotknietych_testow(tmp_path):
    repo = _repo_z_prawdziwym_pyproject(tmp_path)
    baza = tmp_path / "baza.db"
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-q", baza=baza)
    assert kod == 0, wyjscie + bledy
    _zapisz(repo, "src/mini.py", MINI_KOD.replace("return a + b", "return b + a"))
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-q", baza=baza)
    assert kod == 0, wyjscie + bledy
    wybrane, pominiete = _liczba(wyjscie, "wybrane"), _liczba(wyjscie, "pominiete")
    assert (wybrane, pominiete) == (2, 6)


def test_katalog_e2e_jest_pomijany_przy_pelnym_przebiegu_dotknietych(tmp_path):
    repo = _mini_projekt(tmp_path)
    _zapisz(repo, "tests/e2e/test_x.py", "def test_e2e_ktory_padlby():\n    assert False\n")
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-q", baza=tmp_path / "baza.db")
    assert kod == 0, wyjscie + bledy
    assert _liczba(wyjscie, "wybrane") == 8
    assert "test_e2e_ktory_padlby" not in wyjscie


def _repo_ze_zdrowa_baza(tmp_path: Path) -> tuple[Path, Path]:
    import sqlite3

    repo = tmp_path / "repo"
    repo.mkdir()
    baza = tmp_path / "baza" / "testmon.db"
    baza.parent.mkdir()
    polaczenie = sqlite3.connect(baza)
    try:
        with polaczenie:
            polaczenie.execute("create table node (id integer primary key)")
    finally:
        polaczenie.close()
    testy_py.zapisz_odcisk(repo, baza)
    return repo, baza


def _nie_uruchamiaj(*_argumenty, **_nazwane):
    raise AssertionError("pytest nie powinien byc uruchomiony")


# Powody podaje sonda (parser pytesta + `_get_noselect_reasons` testmona), nie dopasowanie tekstu argumentow.
@pytest.mark.parametrize(
    ("argumenty", "powod"),
    [
        (["-k", "dodawanie"], "-k was used"),
        (["-m", "e2e"], "-m was used"),
        (["-kdodawanie"], "-k was used"),
        (["-mslow"], "-m was used"),
        (["-qkdodawanie"], "-k was used"),
        (["-vm", "e2e"], "-m was used"),
        (["-q", "-k", "x"], "-k was used"),
        (["--lf"], "--lf was used"),
        (["--last-failed"], "--lf was used"),
        (["tests/test_mini.py::test_x"], "you selected tests manually"),
    ],
)
def test_argument_wylaczajacy_selekcje_testmona_to_blad_brak_dowodu_zanim_ruszy_pytest(
    monkeypatch, tmp_path, argumenty, powod
):
    repo, baza = _repo_ze_zdrowa_baza(tmp_path)
    monkeypatch.setenv("TESTMON_DATAFILE", str(baza))
    monkeypatch.setattr(testy_py, "uruchom", _nie_uruchamiaj)
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        testy_py.uruchom_dotkniete(argumenty, 5.0, repo)
    assert str(blad.value) == f"brak dowodu: testmon wylaczyl selekcje ({powod}) - uruchom bez tych argumentow"


@pytest.mark.parametrize(
    "argumenty",
    [["-q"], ["-v"], ["-qv"], ["-x"], ["-s"], ["-rA"], ["-W", "ignore"], ["-n", "4"], ["tests/a.py"]],
)
def test_argumenty_niewylaczajace_selekcji_nie_sa_bledem_w_trybie_z_selekcja(monkeypatch, tmp_path, argumenty):
    repo, baza = _repo_ze_zdrowa_baza(tmp_path)
    monkeypatch.setenv("TESTMON_DATAFILE", str(baza))
    monkeypatch.setattr(testy_py, "uruchom", _atrapa_pytesta_z_wyjsciem(["8 passed, 4 deselected in 0.1s\n"]))
    assert testy_py.uruchom_dotkniete(argumenty, 5.0, repo) == 0


def test_te_same_argumenty_w_pelnym_przebiegu_sa_dozwolone_bo_nie_ma_selekcji(tmp_path):
    repo = _mini_projekt(tmp_path)
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-q", "-k", "dodawanie", baza=tmp_path / "baza.db")
    assert kod == 0, wyjscie + bledy
    assert "brak dowodu" not in wyjscie + bledy
    assert (_liczba(wyjscie, "wybrane"), _liczba(wyjscie, "pominiete")) == (2, 6)


def _odtworz_wyjscie(linie: list[str], ogon, dezaktywacje) -> None:
    """To, co dla kazdej linii robi prawdziwy `_czytaj_wyjscie`: ogon i pierwsza linia o wylaczeniu selekcji."""
    for linia in linie:
        ogon.append(linia)
        del ogon[:-40]
        trafienie = testy_py.DEZAKTYWACJA_TESTMONA.search(linia)
        if trafienie and dezaktywacje is not None and not dezaktywacje:
            dezaktywacje.append(trafienie.group(0))


def _atrapa_pytesta_z_wyjsciem(linie: list[str]):
    def atrapa(_argumenty, _limit, _cwd, ogon=None, dezaktywacje=None):
        _odtworz_wyjscie(linie, ogon, dezaktywacje)
        return 0

    return atrapa


@pytest.mark.parametrize(
    ("komunikat", "w_bledzie"),
    [
        ("selection automatically deactivated because -m was used\n", "selection automatically deactivated because -m was used"),
        ("collection automatically deactivated because -x was used\n", "collection automatically deactivated because -x was used"),
        ("selection automatically deactivated because -m was used, \n", "selection automatically deactivated because -m was used"),
        ("testmon: selection automatically deactivated because -m was used\n", "selection automatically deactivated because -m was used"),
    ],
)
def test_testmon_ktory_sam_wylaczyl_selekcje_to_blad_brak_dowodu_z_jego_powodem(monkeypatch, tmp_path, komunikat, w_bledzie):
    repo, baza = _repo_ze_zdrowa_baza(tmp_path)
    monkeypatch.setenv("TESTMON_DATAFILE", str(baza))
    monkeypatch.setattr(testy_py, "uruchom", _atrapa_pytesta_z_wyjsciem([komunikat, "8 passed in 0.1s\n"]))
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        testy_py.uruchom_dotkniete(["-q"], 5.0, repo)
    assert str(blad.value) == f"brak dowodu: testmon wylaczyl selekcje ({w_bledzie})"


def test_wyjscie_bez_komunikatu_o_wylaczeniu_selekcji_nie_jest_bledem(monkeypatch, tmp_path):
    repo, baza = _repo_ze_zdrowa_baza(tmp_path)
    monkeypatch.setenv("TESTMON_DATAFILE", str(baza))
    monkeypatch.setattr(testy_py, "uruchom", _atrapa_pytesta_z_wyjsciem(["8 passed, 4 deselected in 0.1s\n"]))
    assert testy_py.uruchom_dotkniete(["-q"], 5.0, repo) == 0


def test_komunikat_o_wylaczeniu_selekcji_w_pelnym_przebiegu_nie_jest_bledem_bo_noselect_robi_to_celowo(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("TESTMON_DATAFILE", str(tmp_path / "baza" / "testmon.db"))
    atrapa = _atrapa_pytesta_z_wyjsciem(["selection automatically deactivated because --testmon-noselect\n", "8 passed in 0.1s\n"])
    monkeypatch.setattr(testy_py, "uruchom", atrapa)
    assert testy_py.uruchom_dotkniete(["-q"], 5.0, repo) == 0


# --- liczba pominietych: brak pomiaru to nie zero (measured_or_null) ---------------------------------


def test_podsumowanie_z_zerem_pominietych_to_zero_a_bez_wzmianki_o_pominietych_to_brak_pomiaru():
    assert testy_py._liczby_z_podsumowania(["2 passed, 0 deselected in 0.1s\n"]) == (2, 0)
    assert testy_py._liczby_z_podsumowania(["2 passed in 0.1s\n"]) == (2, None)


def _atrapa_z_przebiegami(glowny: list[str], collect: list[str], kod_glowny: int = 0, kod_collect: int = 0):
    """Atrapa `uruchom`: osobne wyjscie dla przebiegu glownego i dla `--collect-only`; zapisuje argumenty wywolan."""
    wywolania: list[list[str]] = []

    def atrapa(argumenty, _limit, _cwd, ogon=None, dezaktywacje=None):
        wywolania.append(list(argumenty))
        if "--collect-only" in argumenty:
            _odtworz_wyjscie(collect, ogon, dezaktywacje)
            return kod_collect
        _odtworz_wyjscie(glowny, ogon, dezaktywacje)
        return kod_glowny

    atrapa.wywolania = wywolania
    return atrapa


def test_brak_deselected_w_podsumowaniu_uruchamia_collect_only_a_pominiete_to_calosc_minus_wybrane(
    monkeypatch, tmp_path, capsys
):
    repo, baza = _repo_ze_zdrowa_baza(tmp_path)
    monkeypatch.setenv("TESTMON_DATAFILE", str(baza))
    atrapa = _atrapa_z_przebiegami(["5 passed in 0.1s\n"], ["12 tests collected in 0.1s\n"])
    monkeypatch.setattr(testy_py, "uruchom", atrapa)
    assert testy_py.uruchom_dotkniete(["-q", "-n", "4", "tests/x.py"], 5.0, repo) == 0
    assert capsys.readouterr().out == "wybrane: 5\npominiete: 7\n"
    assert atrapa.wywolania == [
        ["--testmon", "-o", "addopts=", "-q", "-n", "4", "tests/x.py"],
        ["--testmon-noselect", "--collect-only", "-q", "-o", "addopts=", "tests/x.py"],
    ]


def test_deselected_w_podsumowaniu_to_zmierzone_pominiete_bez_dodatkowego_przebiegu(monkeypatch, tmp_path, capsys):
    repo, baza = _repo_ze_zdrowa_baza(tmp_path)
    monkeypatch.setenv("TESTMON_DATAFILE", str(baza))
    atrapa = _atrapa_z_przebiegami(["5 passed, 7 deselected in 0.1s\n"], ["999 tests collected\n"])
    monkeypatch.setattr(testy_py, "uruchom", atrapa)
    assert testy_py.uruchom_dotkniete(["-q"], 5.0, repo) == 0
    assert capsys.readouterr().out == "wybrane: 5\npominiete: 7\n"
    assert len(atrapa.wywolania) == 1


def test_pelny_przebieg_nic_nie_pomija_wiec_nie_liczy_pominietych_osobnym_przebiegiem(monkeypatch, tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("TESTMON_DATAFILE", str(tmp_path / "baza" / "testmon.db"))
    atrapa = _atrapa_z_przebiegami(["5 passed in 0.1s\n"], ["999 tests collected\n"])
    monkeypatch.setattr(testy_py, "uruchom", atrapa)
    assert testy_py.uruchom_dotkniete(["-q"], 5.0, repo) == 0
    assert "wybrane: 5\npominiete: 0\n" in capsys.readouterr().out
    assert len(atrapa.wywolania) == 1


@pytest.mark.parametrize("kod_glowny", [0, 5])
def test_przebieg_bez_wybranych_testow_i_bez_zmian_py_liczy_wszystkie_testy_jako_pominiete(
    monkeypatch, tmp_path, capsys, kod_glowny
):
    repo, baza = _repo_ze_zdrowa_baza(tmp_path)
    _init_repo(repo)
    _zapisz(repo, "notatki.txt", "a\n")
    _commit(repo, "start")
    monkeypatch.setenv("TESTMON_DATAFILE", str(baza))
    atrapa = _atrapa_z_przebiegami(["no tests ran in 0.00s\n"], ["8 tests collected in 0.1s\n"], kod_glowny=kod_glowny)
    monkeypatch.setattr(testy_py, "uruchom", atrapa)
    assert testy_py.uruchom_dotkniete(["-q"], 5.0, repo) == 0
    assert capsys.readouterr().out == "wybrane: 0\npominiete: 8\n"


@pytest.mark.parametrize(
    ("collect", "wszystkie_w_bledzie"),
    [(["nic\n"], "None"), (["to nie jest 12 tests collected\n"], "None")],
)
def test_collect_only_bez_linii_z_liczba_testow_to_blad_brak_dowodu_a_nie_zero_pominietych(
    monkeypatch, tmp_path, collect, wszystkie_w_bledzie
):
    repo, baza = _repo_ze_zdrowa_baza(tmp_path)
    monkeypatch.setenv("TESTMON_DATAFILE", str(baza))
    monkeypatch.setattr(testy_py, "uruchom", _atrapa_z_przebiegami(["5 passed in 0.1s\n"], collect))
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        testy_py.uruchom_dotkniete(["-q"], 5.0, repo)
    assert str(blad.value) == (
        f"brak dowodu: nie udalo sie ustalic liczby pominietych (kod 0, wszystkie: {wszystkie_w_bledzie}, wybrane: 5)"
    )


def test_collect_only_z_kodem_bledu_to_blad_brak_dowodu_mimo_poprawnej_linii(monkeypatch, tmp_path):
    repo, baza = _repo_ze_zdrowa_baza(tmp_path)
    monkeypatch.setenv("TESTMON_DATAFILE", str(baza))
    atrapa = _atrapa_z_przebiegami(["5 passed in 0.1s\n"], ["12 tests collected in 0.1s\n"], kod_collect=2)
    monkeypatch.setattr(testy_py, "uruchom", atrapa)
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        testy_py.uruchom_dotkniete(["-q"], 5.0, repo)
    assert str(blad.value) == "brak dowodu: nie udalo sie ustalic liczby pominietych (kod 2, wszystkie: 12, wybrane: 5)"


def test_collect_only_z_mniejsza_liczba_testow_niz_wybrane_to_blad_brak_dowodu(monkeypatch, tmp_path):
    repo, baza = _repo_ze_zdrowa_baza(tmp_path)
    monkeypatch.setenv("TESTMON_DATAFILE", str(baza))
    atrapa = _atrapa_z_przebiegami(["5 passed in 0.1s\n"], ["4 tests collected in 0.1s\n"])
    monkeypatch.setattr(testy_py, "uruchom", atrapa)
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        testy_py.uruchom_dotkniete(["-q"], 5.0, repo)
    assert str(blad.value) == "brak dowodu: nie udalo sie ustalic liczby pominietych (kod 0, wszystkie: 4, wybrane: 5)"


@pytest.mark.parametrize(
    ("collect", "pominiete"),
    [
        (["5 tests collected in 0.1s\n"], 0),
        (["12 tests collected in 0.1s\n"], 7),
        (["12 tests collected\n"], 7),
        (["=== 12 tests collected in 0.1s ===\n"], 7),
        (["99 tests collected\n", "12 tests collected in 0.1s\n", "inna linia\n"], 7),
    ],
)
def test_collect_only_liczba_testow_jest_brana_z_ostatniej_linii_w_kazdym_formacie(
    monkeypatch, tmp_path, capsys, collect, pominiete
):
    repo, baza = _repo_ze_zdrowa_baza(tmp_path)
    monkeypatch.setenv("TESTMON_DATAFILE", str(baza))
    monkeypatch.setattr(testy_py, "uruchom", _atrapa_z_przebiegami(["5 passed in 0.1s\n"], collect))
    assert testy_py.uruchom_dotkniete(["-q"], 5.0, repo) == 0
    assert capsys.readouterr().out == f"wybrane: 5\npominiete: {pominiete}\n"


def test_collect_only_z_jednym_testem_w_liczbie_pojedynczej_jest_liczony(monkeypatch, tmp_path, capsys):
    repo, baza = _repo_ze_zdrowa_baza(tmp_path)
    monkeypatch.setenv("TESTMON_DATAFILE", str(baza))
    monkeypatch.setattr(testy_py, "uruchom", _atrapa_z_przebiegami(["1 passed in 0.1s\n"], ["1 test collected in 0.1s\n"]))
    assert testy_py.uruchom_dotkniete(["-q"], 5.0, repo) == 0
    assert capsys.readouterr().out == "wybrane: 1\npominiete: 0\n"


@pytest.mark.parametrize(
    ("argumenty", "zostaje"),
    [
        (["-n", "4"], []),
        (["-n4"], []),
        (["-nauto"], []),
        (["--numprocesses", "4"], []),
        (["--numprocesses=4"], []),
        (["-p", "no:cacheprovider"], []),
        (["-pno:cacheprovider"], []),
        (["-q"], []),
        (["-qq"], []),
        (["-v"], []),
        (["-vv"], []),
        (["tests/a.py"], ["tests/a.py"]),
        (["-x"], ["-x"]),
        (["-n", "4", "tests/a.py"], ["tests/a.py"]),
        (["-p", "x", "-x"], ["-x"]),
    ],
)
def test_do_zliczania_pominietych_nie_trafiaja_opcje_rownoleglosci_wtyczek_i_gadatliwosci(argumenty, zostaje):
    assert testy_py._argumenty_do_zliczania(argumenty) == zostaje


def _liczba_pominietych_z_atrapa(monkeypatch, tmp_path, wybrane, collect, kod_collect):
    monkeypatch.setattr(
        testy_py, "uruchom", _atrapa_z_przebiegami([], collect, kod_collect=kod_collect)
    )
    return testy_py._liczba_pominietych(wybrane, [], [], 5.0, tmp_path)


@pytest.mark.parametrize(
    "linia",
    ["no tests collected in 0.01s\n", "no tests collected\n", "=== no tests collected in 0.01s ===\n"],
)
def test_projekt_bez_testow_collect_only_z_kodem_5_to_zero_wszystkich_i_zero_pominietych(monkeypatch, tmp_path, linia):
    assert _liczba_pominietych_z_atrapa(monkeypatch, tmp_path, 0, [linia], 5) == 0


def test_kod_5_collect_only_bez_linii_no_tests_collected_to_blad_brak_dowodu(monkeypatch, tmp_path):
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        _liczba_pominietych_z_atrapa(monkeypatch, tmp_path, 0, ["nic\n"], 5)
    assert str(blad.value) == "brak dowodu: nie udalo sie ustalic liczby pominietych (kod 5, wszystkie: None, wybrane: 0)"


def test_kod_5_collect_only_z_liczba_testow_zamiast_no_tests_collected_to_blad_brak_dowodu(monkeypatch, tmp_path):
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        _liczba_pominietych_z_atrapa(monkeypatch, tmp_path, 5, ["12 tests collected in 0.1s\n"], 5)
    assert str(blad.value) == "brak dowodu: nie udalo sie ustalic liczby pominietych (kod 5, wszystkie: 12, wybrane: 5)"


def test_no_tests_collected_z_kodem_0_to_blad_brak_dowodu_bo_pytest_konczy_wtedy_kodem_5(monkeypatch, tmp_path):
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        _liczba_pominietych_z_atrapa(monkeypatch, tmp_path, 0, ["no tests collected in 0.01s\n"], 0)
    assert str(blad.value) == "brak dowodu: nie udalo sie ustalic liczby pominietych (kod 0, wszystkie: 0, wybrane: 0)"


def test_no_tests_collected_z_innym_kodem_bledu_to_blad_brak_dowodu(monkeypatch, tmp_path):
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        _liczba_pominietych_z_atrapa(monkeypatch, tmp_path, 0, ["no tests collected in 0.01s\n"], 2)
    assert str(blad.value) == "brak dowodu: nie udalo sie ustalic liczby pominietych (kod 2, wszystkie: 0, wybrane: 0)"


def test_no_tests_collected_przy_wybranych_testach_to_blad_brak_dowodu_a_nie_ujemne_pominiete(monkeypatch, tmp_path):
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        _liczba_pominietych_z_atrapa(monkeypatch, tmp_path, 3, ["no tests collected in 0.01s\n"], 5)
    assert str(blad.value) == "brak dowodu: nie udalo sie ustalic liczby pominietych (kod 5, wszystkie: 0, wybrane: 3)"


def test_projekt_bez_testow_i_bez_zmian_py_konczy_dotkniete_kodem_0_z_zerem_pominietych(monkeypatch, tmp_path, capsys):
    repo, baza = _repo_ze_zdrowa_baza(tmp_path)
    _init_repo(repo)
    _zapisz(repo, "notatki.txt", "a\n")
    _commit(repo, "start")
    monkeypatch.setenv("TESTMON_DATAFILE", str(baza))
    atrapa = _atrapa_z_przebiegami(
        ["no tests ran in 0.00s\n"], ["no tests collected in 0.01s\n"], kod_glowny=5, kod_collect=5
    )
    monkeypatch.setattr(testy_py, "uruchom", atrapa)
    assert testy_py.uruchom_dotkniete(["-q"], 5.0, repo) == 0
    assert capsys.readouterr().out == "wybrane: 0\npominiete: 0\n"


def test_plik_py_ze_znakiem_spoza_ascii_w_diffie_jest_zmiana_a_nie_ukryta_przez_cytowanie_sciezki(tmp_path):
    repo = _mini_projekt(tmp_path)
    _zapisz(repo, "moduł.py", "x = 1\n")
    _commit(repo, "modul")
    _zapisz(repo, "moduł.py", "x = 2\n")
    assert testy_py._zmienione_py(repo) == ["moduł.py"]


def test_zero_wybranych_testow_mimo_zmiany_pliku_py_ze_znakiem_spoza_ascii_to_blad_brak_dowodu(
    monkeypatch, tmp_path
):
    repo, baza = _repo_ze_zdrowa_baza(tmp_path)
    _init_repo(repo)
    _zapisz(repo, "moduł.py", "x = 1\n")
    _commit(repo, "start")
    _zapisz(repo, "moduł.py", "x = 2\n")
    monkeypatch.setenv("TESTMON_DATAFILE", str(baza))
    atrapa = _atrapa_z_przebiegami(["no tests ran in 0.00s\n"], ["8 tests collected\n"], kod_glowny=5)
    monkeypatch.setattr(testy_py, "uruchom", atrapa)
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        testy_py.uruchom_dotkniete(["-q"], 5.0, repo)
    assert str(blad.value) == "brak dowodu: zero wybranych testow mimo zmian w .py (moduł.py)"


def test_komunikat_o_wylaczeniu_selekcji_sprzed_ostatnich_40_linii_jest_zapamietany_w_locie():
    pierwszy = "testmon: selection automatically deactivated because -m was used"
    linie = [pierwszy + "\n"] + [f"linia {i}\n" for i in range(60)]
    linie.append("collection automatically deactivated because inny\n")
    ogon: list[str] = []
    dezaktywacje: list[str] = []
    testy_py._czytaj_wyjscie(io.BytesIO("".join(linie).encode()), testy_py._Stan(), ogon, dezaktywacje)
    assert ogon == linie[-40:]
    assert all("deactivated" not in wiersz for wiersz in ogon[:-1])
    assert dezaktywacje == ["selection automatically deactivated because -m was used"]


def test_komunikat_o_wylaczeniu_selekcji_sprzed_ostatnich_40_linii_wyjscia_daje_blad_brak_dowodu(
    monkeypatch, tmp_path
):
    repo, baza = _repo_ze_zdrowa_baza(tmp_path)
    monkeypatch.setenv("TESTMON_DATAFILE", str(baza))
    linie = ["selection automatically deactivated because -m was used\n"] + [f"linia {i}\n" for i in range(60)]
    linie.append("8 passed in 0.1s\n")
    monkeypatch.setattr(testy_py, "uruchom", _atrapa_pytesta_z_wyjsciem(linie))
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        testy_py.uruchom_dotkniete(["-q"], 5.0, repo)
    assert str(blad.value) == "brak dowodu: testmon wylaczyl selekcje (selection automatically deactivated because -m was used)"


def test_wyjscie_pytesta_spoza_utf8_nie_przerywa_czytnika_a_podsumowanie_jest_odczytane():
    ogon: list[str] = []
    testy_py._czytaj_wyjscie(io.BytesIO(b"\xff\xfe zly bajt\n3 passed in 0.1s\n"), testy_py._Stan(), ogon, [])
    assert ogon[0] == "\ufffd\ufffd zly bajt\n"
    assert testy_py._liczby_z_podsumowania(ogon) == (3, None)


def test_wzgledna_sciezka_bazy_testmona_jest_rozwiazywana_i_wykrywana_jako_lezaca_w_repozytorium(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("TESTMON_DATAFILE", "baza.db")
    monkeypatch.setattr(testy_py, "uruchom", _nie_uruchamiaj)
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        testy_py.uruchom_dotkniete(["-q"], 5.0, repo)
    assert str(blad.value) == f"baza testmona nie moze lezec w repozytorium ({repo.resolve() / 'baza.db'})"


def test_korzen_podany_przez_dowiazanie_symboliczne_nie_ukrywa_bazy_lezacej_w_repozytorium(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    dowiazanie = tmp_path / "dowiazanie"
    dowiazanie.symlink_to(repo, target_is_directory=True)
    monkeypatch.setenv("TESTMON_DATAFILE", str(repo / "baza.db"))
    monkeypatch.setattr(testy_py, "uruchom", _nie_uruchamiaj)
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        testy_py.uruchom_dotkniete(["-q"], 5.0, dowiazanie)
    assert str(blad.value) == f"baza testmona nie moze lezec w repozytorium ({repo.resolve() / 'baza.db'})"


def test_dystrybucja_bez_nazwy_w_metadanych_jest_pomijana_w_odcisku_zaleznosci_a_nie_bledem(monkeypatch, tmp_path):
    from email.message import Message
    from types import SimpleNamespace

    nazwana = Message()
    nazwana["Name"] = "pakiet"
    dystrybucje = [SimpleNamespace(metadata=Message(), version="1.0"), SimpleNamespace(metadata=nazwana, version="2")]
    with monkeypatch.context() as m:
        m.setattr(testy_py.importlib.metadata, "distributions", lambda: dystrybucje)
        odcisk = testy_py._odcisk(tmp_path, None)
    assert odcisk["zaleznosci"] == testy_py._odcisk(tmp_path, {"pakiet": "2"})["zaleznosci"]


def test_czytnik_wyjscia_bez_listy_dezaktywacji_nie_pada_na_komunikacie_testmona():
    ogon: list[str] = []
    testy_py._czytaj_wyjscie(
        io.BytesIO(b"selection automatically deactivated because -m was used\n"), testy_py._Stan(), ogon, None
    )
    assert ogon == ["selection automatically deactivated because -m was used\n"]


def test_komunikat_o_wylaczeniu_selekcji_z_prawdziwego_przebiegu_trafia_do_bledu_brak_dowodu(tmp_path):
    repo = _mini_projekt(tmp_path)
    _zapisz(
        repo,
        "conftest.py",
        "def pytest_report_header(config):\n    return 'selection automatically deactivated because test'\n",
    )
    baza = tmp_path / "baza.db"
    kod, wyjscie, bledy = _testy_dotkniete(repo, baza=baza)  # bez -q naglowek jest w wyjsciu
    assert kod == 0, wyjscie + bledy  # pelny przebieg: komunikat jest oczekiwany
    kod, wyjscie, bledy = _testy_dotkniete(repo, baza=baza)
    assert kod == testy_py.KOD_BLEDU, wyjscie + bledy
    assert "BLAD: brak dowodu: testmon wylaczyl selekcje (selection automatically deactivated because test)" in bledy


def test_pytest_addopts_z_otoczenia_nie_wylacza_selekcji_dotknietych_testow(tmp_path):
    repo = _mini_projekt(tmp_path)
    baza = tmp_path / "baza.db"
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-q", baza=baza)
    assert kod == 0, wyjscie + bledy
    _zapisz(repo, "mini.py", MINI_KOD.replace("return a - b", "return -b + a"))
    kod, wyjscie, bledy = _testy_dotkniete(
        repo, "-q", baza=baza, srodowisko={"PYTEST_ADDOPTS": "-k nieistniejace_slowo_xyz"}
    )
    # bez usuniecia zmiennej `-k` odrzuca wszystkie testy: wybrane 0 mimo zmiany w mini.py => "brak dowodu", kod 2
    assert kod == 0, wyjscie + bledy
    assert "brak dowodu" not in wyjscie + bledy
    assert (_liczba(wyjscie, "wybrane"), _liczba(wyjscie, "pominiete")) == (2, 6)


def _repo_z_plikiem_o_nazwie_z_bajtem_ff(tmp_path: Path) -> tuple[Path, str]:
    """Plik tylko w indeksie gita (na dysku nie da sie go utworzyc na kazdym systemie plikow), potem usuniety z indeksu."""
    repo = _mini_projekt(tmp_path)
    tresc = tmp_path / "tresc.txt"
    tresc.write_text("x = 1\n", encoding="utf-8")
    sha = _git(repo, "hash-object", "-w", str(tresc)).strip()
    sciezka = "plik\udcff.py"  # surrogateescape: w argv trafia do gita jako bajt 0xff
    _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{sha},{sciezka}")
    _git(repo, "commit", "-q", "-m", "plik z bajtem ff")
    _git(repo, "rm", "-q", "--cached", "--", sciezka)
    return repo, sciezka


def test_nazwa_zmienionego_pliku_z_bajtem_spoza_utf8_nie_przerywa_odczytu_diffu(tmp_path):
    repo, _ = _repo_z_plikiem_o_nazwie_z_bajtem_ff(tmp_path)
    assert testy_py._zmienione_py(repo) == ["plik\ufffd.py"]


def test_nazwa_zmienionego_pliku_py_ze_znakiem_spoza_ascii_jest_czytana_jako_utf8_niezaleznie_od_locale(tmp_path):
    repo = _mini_projekt(tmp_path)
    _zapisz(repo, "moduł.py", "x = 1\n")
    _commit(repo, "modul")
    _zapisz(repo, "moduł.py", "x = 2\n")
    kod = (
        "import sys\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\nimport testy\n"
        "print(ascii(testy._zmienione_py(Path(sys.argv[2]))))\n"
    )
    srodowisko = {"LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
    kod_wyjscia, wyjscie, bledy = _uruchom([sys.executable, "-c", kod, str(NARZEDZIA), str(repo)], KORZEN, srodowisko)
    assert kod_wyjscia == 0, bledy
    assert wyjscie.strip() == ascii(["moduł.py"])


@pytest.mark.parametrize(
    "argumenty",
    [
        ["-oaddopts=-kfoo"],
        ["--override-ini=addopts=-kfoo"],
        ["-o", "addopts=-kfoo"],
        ["--override-ini", "addopts=-kfoo"],
        ["-q", "-o", "addopts=-kfoo"],
    ],
)
def test_argument_ustawiajacy_addopts_z_opcja_k_to_blad_brak_dowodu_zanim_ruszy_pytest(monkeypatch, tmp_path, argumenty):
    repo, baza = _repo_ze_zdrowa_baza(tmp_path)
    monkeypatch.setenv("TESTMON_DATAFILE", str(baza))
    monkeypatch.setattr(testy_py, "uruchom", _nie_uruchamiaj)
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        testy_py.uruchom_dotkniete(argumenty, 5.0, repo)
    assert str(blad.value) == "brak dowodu: testmon wylaczyl selekcje (-k was used) - uruchom bez tych argumentow"


@pytest.mark.parametrize(
    "argumenty",
    [
        ["-o", "cache_dir=x"],
        ["--override-ini", "cache_dir=x"],
        ["addopts=-kfoo"],
        ["-o", "x=1", "addopts=-kfoo"],
        ["-oaddoptsx=1"],
    ],
)
def test_argumenty_niezwiazane_z_addopts_nie_sa_bledem_w_trybie_z_selekcja(monkeypatch, tmp_path, argumenty):
    repo, baza = _repo_ze_zdrowa_baza(tmp_path)
    monkeypatch.setenv("TESTMON_DATAFILE", str(baza))
    monkeypatch.setattr(testy_py, "uruchom", _atrapa_pytesta_z_wyjsciem(["8 passed, 4 deselected in 0.1s\n"]))
    assert testy_py.uruchom_dotkniete(argumenty, 5.0, repo) == 0


@pytest.mark.parametrize("argumenty", [["-o", "addopts=-kfoo"], ["-oaddopts=-kfoo"]])
def test_addopts_w_argumencie_w_pelnym_przebiegu_nie_jest_bledem_bo_nie_ma_selekcji(monkeypatch, tmp_path, argumenty):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("TESTMON_DATAFILE", str(tmp_path / "baza" / "testmon.db"))
    monkeypatch.setattr(testy_py, "uruchom", _atrapa_pytesta_z_wyjsciem(["5 passed in 0.1s\n"]))
    assert testy_py.uruchom_dotkniete(argumenty, 5.0, repo) == 0


def test_addopts_z_linii_polecen_w_prawdziwym_przebiegu_dotknietych_to_blad_brak_dowodu_kod_2(tmp_path):
    repo = _mini_projekt(tmp_path)
    baza = tmp_path / "baza.db"
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-q", baza=baza)
    assert kod == 0, wyjscie + bledy
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-q", "-o", "addopts=-kfoo", baza=baza)
    assert kod == testy_py.KOD_BLEDU, wyjscie + bledy
    assert "BLAD: brak dowodu: testmon wylaczyl selekcje (-k was used) - uruchom bez tych argumentow" in bledy


# --- HAR-257: forma opcji nie ma znaczenia, decyduje to, co pytest naprawde zrobi z argumentami -------
# Testy ida przez prawdziwy pytest i prawdziwy testmon w mini-projekcie. Po zmianie TYLKO `pomnoz` poprawna selekcja to
# dwa testy mnozenia (wybrane 2, pominiete 6). Wylaczona selekcja (np. `-k odejmowanie`) pokazalaby inne testy z takimi
# samymi liczbami - dlatego sprawdzamy, KTORE testy ruszyly, a nie same liczby.

def _po_zmianie_pomnoz(tmp_path: Path, *argumenty: str, srodowisko: dict[str, str] | None = None):
    repo = _mini_projekt(tmp_path)
    baza = tmp_path / "baza.db"
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-q", baza=baza)
    assert kod == 0, wyjscie + bledy
    _zapisz(repo, "mini.py", MINI_KOD.replace("return a * b", "return b * a"))
    return _testy_dotkniete(repo, *argumenty, baza=baza, srodowisko=srodowisko)


def _nie_jest_zielone_z_wylaczona_selekcja(kod: int, wyjscie: str, bledy: str) -> None:
    """Dozwolone: blad uzycia / brak dowodu (kod 2) albo jawny pelny przebieg. Zabronione: kod 0 z liczbami selekcji."""
    if kod == testy_py.KOD_BLEDU:
        assert "BLAD: brak dowodu:" in bledy, wyjscie + bledy
    else:
        assert kod == 0 and "testy.py: pelny przebieg" in wyjscie, (
            f"kod {kod} bez pelnego przebiegu, a selekcje wylaczyl argument:\n{wyjscie}{bledy}"
        )


@pytest.mark.parametrize(
    "argumenty",
    [
        ["-q", "-o=addopts=-kodejmowanie"],
        ["-qo", "addopts=-kodejmowanie"],
        ["-vo", "addopts=-kodejmowanie"],
        ["-xo", "addopts=-kodejmowanie"],
        ["-so=addopts=-kodejmowanie"],
        ["-q", "--override-ini", "addopts=-kodejmowanie"],
    ],
)
def test_forma_argumentu_wylaczajacego_selekcje_dotknietych_nie_daje_zielonego_wyniku_z_liczbami_selekcji(tmp_path, argumenty):
    kod, wyjscie, bledy = _po_zmianie_pomnoz(tmp_path, *argumenty)
    _nie_jest_zielone_z_wylaczona_selekcja(kod, wyjscie, bledy)


@pytest.mark.parametrize("linie", [["-o", "addopts=-kodejmowanie"], ["-k", "odejmowanie"], ["-qo", "addopts=-kodejmowanie"]])
def test_argumenty_wczytane_z_pliku_wylaczajace_selekcje_dotknietych_nie_daja_zielonego_wyniku(tmp_path, linie):
    plik = tmp_path / "argumenty.txt"
    plik.write_text("".join(f"{linia}\n" for linia in linie), encoding="utf-8")
    kod, wyjscie, bledy = _po_zmianie_pomnoz(tmp_path, "-q", f"@{plik}")
    _nie_jest_zielone_z_wylaczona_selekcja(kod, wyjscie, bledy)


def test_pytest_addopts_z_otoczenia_z_pasujacym_slowem_kluczowym_nie_podmienia_wybranych_testow(tmp_path):
    kod, wyjscie, bledy = _po_zmianie_pomnoz(
        tmp_path, "-v", srodowisko={"PYTEST_ADDOPTS": "-k odejmowanie"}
    )
    assert kod == 0, wyjscie + bledy
    assert "test_mnozenie_przez_zero" in wyjscie
    assert "test_odejmowanie_do_ujemnej" not in wyjscie
    assert (_liczba(wyjscie, "wybrane"), _liczba(wyjscie, "pominiete")) == (2, 6)


def test_klucz_ini_tylko_podobny_do_addopts_nie_wylacza_selekcji_dotknietych_testow(tmp_path):
    kod, wyjscie, bledy = _po_zmianie_pomnoz(tmp_path, "-v", "-o", "addoptsx=-kodejmowanie")
    assert kod == 0, wyjscie + bledy
    assert "brak dowodu" not in wyjscie + bledy
    assert "test_mnozenie_przez_zero" in wyjscie
    assert "test_odejmowanie_do_ujemnej" not in wyjscie
    assert (_liczba(wyjscie, "wybrane"), _liczba(wyjscie, "pominiete")) == (2, 6)


@pytest.mark.parametrize(
    "argumenty",
    [
        ["-q", "--nieistniejaca-opcja-har257"],
        ["-q", "--markex", "not wolny"],  # pytest nie przyjmuje skrotow opcji dlugich: UsageError parsera
    ],
)
def test_opcja_nieznana_pytestowi_to_brak_dowodu_kod_2_a_nie_kod_uzycia_pytesta(tmp_path, argumenty):
    kod, wyjscie, bledy = _po_zmianie_pomnoz(tmp_path, *argumenty)
    assert kod == testy_py.KOD_BLEDU, wyjscie + bledy
    assert "BLAD: brak dowodu:" in bledy


def test_brak_testmona_przy_sprawdzaniu_selekcji_to_brak_dowodu_kod_2(tmp_path):
    # `testmon` podmieniony pakietem, ktory nie daje sie zaimportowac: sonda nie ma z czego ocenic selekcji
    atrapa = tmp_path / "atrapa" / "testmon"
    atrapa.mkdir(parents=True)
    (atrapa / "__init__.py").write_text("raise ImportError('brak testmona w tescie HAR-257')\n", encoding="utf-8")
    kod, wyjscie, bledy = _po_zmianie_pomnoz(
        tmp_path, "-q", srodowisko={"PYTHONPATH": str(atrapa.parent)}
    )
    assert kod == testy_py.KOD_BLEDU, wyjscie + bledy
    assert "BLAD: brak dowodu:" in bledy


# --- HAR-257: sonda parsera (`_powody_bez_selekcji`) - kazda galaz "brak dowodu" i kontrakt wywolania ---


def _atrapa_sondy(*, kod: int = 0, stdout: str = "", stderr: str = "", wyjatek: Exception | None = None):
    """Atrapa `subprocess.run` widzianego przez sonde; zapisuje wywolania (argumenty i parametry nazwane)."""
    wywolania: list[tuple[list[str], dict]] = []

    def atrapa(argv, **nazwane):
        wywolania.append((list(argv), nazwane))
        if wyjatek is not None:
            raise wyjatek
        return subprocess.CompletedProcess(argv, kod, stdout=stdout, stderr=stderr)

    atrapa.wywolania = wywolania
    return atrapa


def _sonda_z_atrapa(monkeypatch, tmp_path, atrapa, argumenty=("--testmon",)):
    monkeypatch.setattr(testy_py.subprocess, "run", atrapa)
    return testy_py._powody_bez_selekcji(list(argumenty), tmp_path)


# Odpowiedz sondy: obiekt z kompletem kluczy. Test zmienia jedno pole naraz, wiec o wyniku decyduje tylko ono.
WERDYKT_BEZ_POWODOW = {
    "noselect": [],
    "notestmon": None,
    "forceselect": False,
    "keyword": None,
    "markexpr": None,
    "lf": False,
    "recznie": [],
}


def _odpowiedz_sondy(**nadpisania) -> str:
    return json.dumps({**WERDYKT_BEZ_POWODOW, **nadpisania})


def _stdout_sondy(**nadpisania) -> str:
    return f"{testy_py.ZNACZNIK_SONDY}{_odpowiedz_sondy(**nadpisania)}\n"


def test_sonda_podaje_powody_wylaczenia_selekcji_z_prawdziwego_parsera_pytesta_i_testmona(tmp_path):
    assert testy_py._powody_bez_selekcji(["--testmon", "-k", "x"], tmp_path) == ["-k was used"]
    assert testy_py._powody_bez_selekcji(["--testmon", "-q"], tmp_path) == []


def test_jawne_testmon_noselect_w_argumentach_sondy_to_powod_o_nazwie_opcji_a_nie_pusty_tekst(tmp_path):
    assert testy_py._powody_bez_selekcji(["--testmon", "--testmon-noselect"], tmp_path) == ["--testmon-noselect"]


def test_kolejnosc_argumentow_sondy_decyduje_ostatnie_ustawienie_addopts_wygrywa(tmp_path):
    zle_na_koncu = ["--testmon", "-o", "addopts=", "-o", "addopts=-kfoo"]
    dobre_na_koncu = ["--testmon", "-o", "addopts=-kfoo", "-o", "addopts="]
    assert testy_py._powody_bez_selekcji(zle_na_koncu, tmp_path) == ["-k was used"]
    assert testy_py._powody_bez_selekcji(dobre_na_koncu, tmp_path) == []


def test_sonda_startuje_w_podanym_katalogu_z_limitem_czasu_bez_stdin_i_z_argumentami_po_skrypcie(monkeypatch, tmp_path):
    atrapa = _atrapa_sondy(stdout=_stdout_sondy())
    assert _sonda_z_atrapa(monkeypatch, tmp_path, atrapa, ("--testmon", "-q")) == []
    (argv, nazwane), = atrapa.wywolania
    assert argv == [sys.executable, "-c", testy_py._SKRYPT_SONDY, "--testmon", "-q"]
    assert nazwane["cwd"] == tmp_path
    assert nazwane["timeout"] == testy_py.LIMIT_SONDY
    assert nazwane["stdin"] == subprocess.DEVNULL
    assert nazwane["env"] == dict(os.environ)
    assert nazwane["capture_output"] is True
    assert nazwane["text"] is True
    assert nazwane["encoding"] == "utf-8"
    assert nazwane["errors"] == "replace"


def test_przekroczenie_limitu_czasu_sondy_to_brak_dowodu_a_nie_selekcja_ktora_dziala(monkeypatch, tmp_path):
    wyjatek = subprocess.TimeoutExpired(["sonda"], 120.0)
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        _sonda_z_atrapa(monkeypatch, tmp_path, _atrapa_sondy(wyjatek=wyjatek))
    assert str(blad.value) == f"brak dowodu: sonda parsera pytesta przekroczyla limit 120 s: {wyjatek}"
    assert blad.value.__cause__ is wyjatek


def test_niemozliwy_start_sondy_to_brak_dowodu_z_przyczyna_systemu(monkeypatch, tmp_path):
    wyjatek = OSError(2, "brak interpretera")
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        _sonda_z_atrapa(monkeypatch, tmp_path, _atrapa_sondy(wyjatek=wyjatek))
    assert str(blad.value) == "brak dowodu: nie udalo sie uruchomic sondy parsera pytesta: [Errno 2] brak interpretera"
    assert blad.value.__cause__ is wyjatek


def test_sonda_ktora_padla_to_brak_dowodu_z_ostatnia_linia_bledu_nawet_gdy_wypisala_odpowiedz(monkeypatch, tmp_path):
    atrapa = _atrapa_sondy(kod=3, stdout=_stdout_sondy(), stderr="Traceback\n  pierwsza\nValueError: zla\n")
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        _sonda_z_atrapa(monkeypatch, tmp_path, atrapa)
    assert str(blad.value) == "brak dowodu: sonda parsera pytesta zakonczyla sie kodem 3: ValueError: zla"


def test_sonda_ktora_padla_bez_komunikatu_to_brak_dowodu_z_adnotacja_o_braku_komunikatu(monkeypatch, tmp_path):
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        _sonda_z_atrapa(monkeypatch, tmp_path, _atrapa_sondy(kod=1, stderr="  \n"))
    assert str(blad.value) == "brak dowodu: sonda parsera pytesta zakonczyla sie kodem 1: brak komunikatu"


def test_sonda_zabita_sygnalem_to_brak_dowodu_z_ujemnym_kodem_nawet_gdy_wypisala_odpowiedz(monkeypatch, tmp_path):
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        _sonda_z_atrapa(monkeypatch, tmp_path, _atrapa_sondy(kod=-9, stdout=_stdout_sondy()))
    assert str(blad.value) == "brak dowodu: sonda parsera pytesta zakonczyla sie kodem -9: brak komunikatu"


@pytest.mark.parametrize(
    ("stdout", "ile"),
    [
        ("", 0),
        ("jakis szum\n", 0),
        (f" {_stdout_sondy()}", 0),
        (_stdout_sondy() * 2, 2),
    ],
)
def test_sonda_bez_dokladnie_jednej_odpowiedzi_to_brak_dowodu_z_liczba_odpowiedzi(monkeypatch, tmp_path, stdout, ile):
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        _sonda_z_atrapa(monkeypatch, tmp_path, _atrapa_sondy(stdout=stdout))
    assert str(blad.value) == f"brak dowodu: sonda parsera pytesta nie podala powodow (odpowiedzi: {ile})"


def _bez_klucza(klucz: str) -> str:
    return json.dumps({k: v for k, v in WERDYKT_BEZ_POWODOW.items() if k != klucz})


# Kazdy wiersz psuje dokladnie jedno: skladnie JSON, ksztalt calosci, brak jednego z siedmiu kluczy albo typ jednego pola
ZLE_ODPOWIEDZI_SONDY = [
    pytest.param("nie json", id="nie-json"),
    pytest.param("", id="pusta"),
    pytest.param("{", id="urwany-json"),
    pytest.param("[]", id="lista-zamiast-obiektu"),
    pytest.param('["noselect"]', id="lista-tekstow-zamiast-obiektu"),
    pytest.param("5", id="liczba-zamiast-obiektu"),
    pytest.param("null", id="null-zamiast-obiektu"),
    pytest.param('"noselect"', id="tekst-zamiast-obiektu"),
    pytest.param("{}", id="pusty-obiekt"),
    *[pytest.param(_bez_klucza(klucz), id=f"brak-klucza-{klucz}") for klucz in WERDYKT_BEZ_POWODOW],
    pytest.param(_odpowiedz_sondy(noselect="-k was used"), id="noselect-tekst-zamiast-listy"),
    pytest.param(_odpowiedz_sondy(noselect=None), id="noselect-null-zamiast-listy"),
    pytest.param(_odpowiedz_sondy(noselect={"a": "b"}), id="noselect-obiekt-zamiast-listy"),
    pytest.param(_odpowiedz_sondy(noselect=[1]), id="noselect-liczba-w-liscie"),
    pytest.param(_odpowiedz_sondy(noselect=["a", 2]), id="noselect-liczba-po-tekscie"),
    pytest.param(_odpowiedz_sondy(noselect=[[]]), id="noselect-lista-w-liscie"),
    pytest.param(_odpowiedz_sondy(notestmon=5), id="notestmon-liczba"),
    pytest.param(_odpowiedz_sondy(notestmon=0), id="notestmon-zero-nie-jest-brakiem"),
    pytest.param(_odpowiedz_sondy(notestmon=False), id="notestmon-false-nie-jest-brakiem"),
    pytest.param(_odpowiedz_sondy(notestmon=["x"]), id="notestmon-lista"),
    pytest.param(_odpowiedz_sondy(forceselect="tak"), id="forceselect-tekst"),
    pytest.param(_odpowiedz_sondy(forceselect=1), id="forceselect-jedynka"),
    pytest.param(_odpowiedz_sondy(forceselect=0), id="forceselect-zero"),
    pytest.param(_odpowiedz_sondy(forceselect=None), id="forceselect-null"),
    pytest.param(_odpowiedz_sondy(recznie=5), id="recznie-liczba"),
    pytest.param(_odpowiedz_sondy(recznie=None), id="recznie-null"),
    pytest.param(_odpowiedz_sondy(recznie=[1]), id="recznie-liczba-w-liscie"),
    pytest.param(_odpowiedz_sondy(recznie=["a.py::t", 1]), id="recznie-liczba-po-tekscie"),
    pytest.param(_odpowiedz_sondy(recznie=[None]), id="recznie-null-w-liscie"),
    pytest.param(_odpowiedz_sondy(recznie=[["a.py::t"]]), id="recznie-lista-w-liscie"),
]


@pytest.mark.parametrize("odpowiedz", ZLE_ODPOWIEDZI_SONDY)
def test_odpowiedz_sondy_niezgodna_z_kontraktem_to_brak_dowodu_a_nie_selekcja_ktora_dziala(monkeypatch, tmp_path, odpowiedz):
    atrapa = _atrapa_sondy(stdout=f"{testy_py.ZNACZNIK_SONDY}{odpowiedz}\n")
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        _sonda_z_atrapa(monkeypatch, tmp_path, atrapa)
    assert str(blad.value) == f"brak dowodu: niezrozumiala odpowiedz sondy parsera pytesta: {odpowiedz!r}"


# Pola selektorow o zlym typie: kazde sprawdzamy z forceselect i bez niego. Bez kontroli typu wartosc "prawie dobra"
# (pusty tekst, "ab", 1) przechodzilaby jako brak selektora albo jako selektor, wiec wynik byloby inny niz blad.
ZLE_POLA_SELEKTOROW = [
    ("recznie-pusty-tekst", {"recznie": ""}),
    ("recznie-tekst", {"recznie": "ab"}),
    ("recznie-null", {"recznie": None}),
    ("recznie-liczba-w-liscie", {"recznie": [1]}),
    ("keyword-liczba", {"keyword": 5}),
    ("keyword-pusta-lista", {"keyword": []}),
    ("markexpr-liczba", {"markexpr": 5}),
    ("markexpr-pusta-lista", {"markexpr": []}),
    ("lf-jedynka", {"lf": 1}),
    ("lf-tekst", {"lf": "x"}),
]


@pytest.mark.parametrize(
    ("forceselect", "pole"),
    [
        pytest.param(forceselect, pole, id=f"{nazwa}-{'z' if forceselect else 'bez'}-forceselect")
        for nazwa, pole in ZLE_POLA_SELEKTOROW
        for forceselect in (True, False)
    ],
)
def test_pole_selektora_zlego_typu_w_odpowiedzi_sondy_to_brak_dowodu_z_forceselect_i_bez(monkeypatch, tmp_path, forceselect, pole):
    odpowiedz = _odpowiedz_sondy(forceselect=forceselect, **pole)
    atrapa = _atrapa_sondy(stdout=f"{testy_py.ZNACZNIK_SONDY}{odpowiedz}\n")
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        _sonda_z_atrapa(monkeypatch, tmp_path, atrapa)
    assert str(blad.value) == f"brak dowodu: niezrozumiala odpowiedz sondy parsera pytesta: {odpowiedz!r}"


def test_odpowiedz_sondy_bez_klucza_zachowuje_przyczyne_klucza_a_nie_jest_osobnym_wyjatkiem(monkeypatch, tmp_path):
    atrapa = _atrapa_sondy(stdout=f"{testy_py.ZNACZNIK_SONDY}{_bez_klucza('lf')}\n")
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        _sonda_z_atrapa(monkeypatch, tmp_path, atrapa)
    assert isinstance(blad.value.__cause__, KeyError)


def test_odpowiedz_sondy_niebedaca_obiektem_zachowuje_przyczyne_bledu_typu(monkeypatch, tmp_path):
    atrapa = _atrapa_sondy(stdout=f"{testy_py.ZNACZNIK_SONDY}[]\n")
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        _sonda_z_atrapa(monkeypatch, tmp_path, atrapa)
    assert isinstance(blad.value.__cause__, TypeError)


def test_sonda_bez_zadnego_powodu_zwraca_pusta_liste_a_powody_noselect_w_kolejnosci(monkeypatch, tmp_path):
    assert _sonda_z_atrapa(monkeypatch, tmp_path, _atrapa_sondy(stdout=_stdout_sondy())) == []
    atrapa = _atrapa_sondy(stdout=_stdout_sondy(noselect=["b", "a"]))
    assert _sonda_z_atrapa(monkeypatch, tmp_path, atrapa) == ["b", "a"]


def test_testmon_wylaczony_w_odpowiedzi_sondy_to_brak_dowodu_z_powodem_i_pierwszenstwem_przed_reszta(monkeypatch, tmp_path):
    atrapa = _atrapa_sondy(
        stdout=_stdout_sondy(notestmon="deactivated through --no-testmon", forceselect=True, keyword="x", noselect=["-k was used"])
    )
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        _sonda_z_atrapa(monkeypatch, tmp_path, atrapa)
    assert str(blad.value) == "brak dowodu: testmon wylaczony (deactivated through --no-testmon) - uruchom bez tych argumentow"


def test_pusty_powod_wylaczenia_testmona_w_odpowiedzi_sondy_nie_jest_wylaczeniem(monkeypatch, tmp_path):
    atrapa = _atrapa_sondy(stdout=_stdout_sondy(notestmon="", noselect=["-k was used"]))
    assert _sonda_z_atrapa(monkeypatch, tmp_path, atrapa) == ["-k was used"]


@pytest.mark.parametrize(
    ("pole", "wartosc", "opis"),
    [
        ("keyword", "szybkie", "-k szybkie"),
        ("markexpr", "not wolny", "-m not wolny"),
        ("lf", True, "--lf"),
        ("recznie", ["tests/a.py::test_a"], "tests/a.py::test_a"),
    ],
    ids=["keyword", "markexpr", "lf", "recznie"],
)
def test_forceselect_z_kazdym_selektorem_pytesta_z_osobna_to_brak_dowodu_z_nazwa_selektora(monkeypatch, tmp_path, pole, wartosc, opis):
    atrapa = _atrapa_sondy(stdout=_stdout_sondy(forceselect=True, **{pole: wartosc}))
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        _sonda_z_atrapa(monkeypatch, tmp_path, atrapa)
    assert str(blad.value) == f"brak dowodu: --testmon-forceselect z selektorem pytesta ({opis}) - uruchom bez tych argumentow"


def test_forceselect_z_kilkoma_selektorami_wymienia_je_wszystkie_w_stalej_kolejnosci(monkeypatch, tmp_path):
    atrapa = _atrapa_sondy(
        stdout=_stdout_sondy(
            forceselect=True, keyword="a", markexpr="b", lf=True, recznie=["x.py::t1", "y.py::t2"]
        )
    )
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        _sonda_z_atrapa(monkeypatch, tmp_path, atrapa)
    assert str(blad.value) == (
        "brak dowodu: --testmon-forceselect z selektorem pytesta (-k a, -m b, --lf, x.py::t1, y.py::t2) - uruchom bez tych argumentow"
    )


@pytest.mark.parametrize(
    "nadpisania",
    [
        {"forceselect": True},
        {"forceselect": True, "keyword": "", "markexpr": "", "lf": False, "recznie": []},
        {"forceselect": False, "keyword": "x"},
        {"forceselect": False, "markexpr": "x"},
        {"forceselect": False, "lf": True},
        {"forceselect": False, "recznie": ["a.py::t"]},
    ],
    ids=["forceselect-bez-selektora", "forceselect-z-pustymi-selektorami", "k-bez-forceselect", "m-bez-forceselect", "lf-bez-forceselect", "plik-bez-forceselect"],
)
def test_bez_pary_forceselect_i_selektor_odpowiedz_sondy_nie_jest_bledem_selektora(monkeypatch, tmp_path, nadpisania):
    atrapa = _atrapa_sondy(stdout=_stdout_sondy(noselect=["-k was used"], **nadpisania))
    assert _sonda_z_atrapa(monkeypatch, tmp_path, atrapa) == ["-k was used"]


# Prawdziwy parser pytesta i testmon (sonda w podprocesie): forma, w jakiej selektor albo wylaczenie trafia do argumentow,
# nie ma znaczenia - `-o addopts=...` i `@plik` rozwija ten sam parser, ktory zobaczy realny przebieg.

def _argumenty_z_pliku(tmp_path: Path, *linie: str) -> list[str]:
    plik = tmp_path / "argumenty.txt"
    plik.write_text("".join(f"{linia}\n" for linia in linie), encoding="utf-8")
    return [f"@{plik}"]


def _sonda_na_prawdziwym_parserze_odrzuca(argumenty: list[str], tmp_path: Path) -> str:
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        testy_py._powody_bez_selekcji(["--testmon", *argumenty], tmp_path)
    return str(blad.value)


@pytest.mark.parametrize(
    ("argumenty", "selektor"),
    [
        (["--testmon-forceselect", "-k", "x"], "-k x"),
        (["--testmon-forceselect", "-m", "x"], "-m x"),
        (["--testmon-forceselect", "--lf"], "--lf"),
        (["--testmon-forceselect", "plik.py::t"], "plik.py::t"),
        (["--testmon-forceselect", "-o", "addopts=-k x"], "-k x"),
        (["--testmon-forceselect", "-o", "addopts=-m x"], "-m x"),
        (["--testmon-forceselect", "-o", "addopts=--lf"], "--lf"),
        (["--testmon-forceselect", "-o", "addopts=plik.py::t"], "plik.py::t"),
        (["-o", "addopts=--testmon-forceselect -k x"], "-k x"),
    ],
    ids=["k", "m", "lf", "plik-test", "addopts-k", "addopts-m", "addopts-lf", "addopts-plik-test", "addopts-z-forceselect"],
)
def test_forceselect_z_selektorem_pytesta_to_brak_dowodu_na_prawdziwym_parserze_w_kazdej_formie(tmp_path, argumenty, selektor):
    komunikat = _sonda_na_prawdziwym_parserze_odrzuca(argumenty, tmp_path)
    assert komunikat == f"brak dowodu: --testmon-forceselect z selektorem pytesta ({selektor}) - uruchom bez tych argumentow"


@pytest.mark.parametrize(
    ("linie", "selektor"),
    [
        (("--testmon-forceselect", "-k", "x"), "-k x"),
        (("--testmon-forceselect", "-m", "x"), "-m x"),
        (("--testmon-forceselect", "--lf"), "--lf"),
        (("--testmon-forceselect", "plik.py::t"), "plik.py::t"),
    ],
    ids=["k", "m", "lf", "plik-test"],
)
def test_forceselect_z_selektorem_z_pliku_argumentow_to_brak_dowodu_na_prawdziwym_parserze(tmp_path, linie, selektor):
    komunikat = _sonda_na_prawdziwym_parserze_odrzuca(_argumenty_z_pliku(tmp_path, *linie), tmp_path)
    assert komunikat == f"brak dowodu: --testmon-forceselect z selektorem pytesta ({selektor}) - uruchom bez tych argumentow"


def test_forceselect_rozdzielony_miedzy_argumenty_i_plik_z_selektorem_to_brak_dowodu(tmp_path):
    argumenty = ["--testmon-forceselect", *_argumenty_z_pliku(tmp_path, "-k", "x")]
    komunikat = _sonda_na_prawdziwym_parserze_odrzuca(argumenty, tmp_path)
    assert komunikat == "brak dowodu: --testmon-forceselect z selektorem pytesta (-k x) - uruchom bez tych argumentow"


@pytest.mark.parametrize(
    "argumenty",
    [["--testmon-forceselect"], ["-o", "addopts=--testmon-forceselect"], ["--testmon-forceselect", "plik.py"]],
    ids=["argument", "addopts", "sam-plik-bez-nazwy-testu"],
)
def test_forceselect_bez_selektora_na_prawdziwym_parserze_nie_jest_bledem_selektora(tmp_path, argumenty):
    assert testy_py._powody_bez_selekcji(["--testmon", *argumenty], tmp_path) == []


def test_forceselect_bez_selektora_z_pliku_argumentow_na_prawdziwym_parserze_nie_jest_bledem_selektora(tmp_path):
    argumenty = _argumenty_z_pliku(tmp_path, "--testmon-forceselect")
    assert testy_py._powody_bez_selekcji(["--testmon", *argumenty], tmp_path) == []


def test_forceselect_z_addopts_repozytorium_tylko_po_skasowaniu_ich_przez_wlasne_nie_ma_selektora():
    # korzen repo ma `addopts = -m 'not e2e'`: bez wlasnego `-o addopts=` to selektor, z nim - brak
    komunikat = _sonda_na_prawdziwym_parserze_odrzuca(["--testmon-forceselect"], KORZEN)
    assert komunikat == "brak dowodu: --testmon-forceselect z selektorem pytesta (-m not e2e) - uruchom bez tych argumentow"
    assert testy_py._powody_bez_selekcji(["--testmon", "-o", "addopts=", "--testmon-forceselect"], KORZEN) == []


@pytest.mark.parametrize(
    "argumenty",
    [["--no-testmon"], ["-o", "addopts=--no-testmon"], ["-q", "--no-testmon", "-v"]],
    ids=["argument", "addopts", "miedzy-innymi"],
)
def test_wylaczenie_testmona_argumentem_na_prawdziwym_parserze_to_brak_dowodu_z_powodem_testmona(tmp_path, argumenty):
    komunikat = _sonda_na_prawdziwym_parserze_odrzuca(argumenty, tmp_path)
    assert komunikat == "brak dowodu: testmon wylaczony (deactivated through --no-testmon) - uruchom bez tych argumentow"


def test_wylaczenie_testmona_z_pliku_argumentow_na_prawdziwym_parserze_to_brak_dowodu(tmp_path):
    komunikat = _sonda_na_prawdziwym_parserze_odrzuca(_argumenty_z_pliku(tmp_path, "--no-testmon"), tmp_path)
    assert komunikat == "brak dowodu: testmon wylaczony (deactivated through --no-testmon) - uruchom bez tych argumentow"


# Pelna sciezka: `uruchom_dotkniete` w mini-projekcie z prawdziwym pytestem i testmonem; sprawdzamy, KTORE testy ruszyly.

@pytest.mark.parametrize(
    ("argumenty", "komunikat"),
    [
        (["-q", "--testmon-forceselect", "-k", "odejmowanie"], "--testmon-forceselect z selektorem pytesta (-k odejmowanie)"),
        (["-q", "--testmon-forceselect", "-m", "wolny"], "--testmon-forceselect z selektorem pytesta (-m wolny)"),
        (["-q", "--testmon-forceselect", "--lf"], "--testmon-forceselect z selektorem pytesta (--lf)"),
        (["-q", "--no-testmon"], "testmon wylaczony (deactivated through --no-testmon)"),
    ],
    ids=["forceselect-k", "forceselect-m", "forceselect-lf", "no-testmon"],
)
def test_dotkniete_z_forceselect_i_selektorem_albo_z_wylaczonym_testmonem_to_brak_dowodu_kod_2(tmp_path, argumenty, komunikat):
    kod, wyjscie, bledy = _po_zmianie_pomnoz(tmp_path, *argumenty)
    assert kod == testy_py.KOD_BLEDU, wyjscie + bledy
    assert f"BLAD: brak dowodu: {komunikat} - uruchom bez tych argumentow" in bledy
    assert "testy.py: pelny przebieg" not in wyjscie


def test_dotkniete_z_forceselect_bez_selektora_uruchamia_tylko_testy_dotknietej_funkcji(tmp_path):
    kod, wyjscie, bledy = _po_zmianie_pomnoz(tmp_path, "-v", "--testmon-forceselect")
    assert kod == 0, wyjscie + bledy
    assert "test_mnozenie_przez_zero" in wyjscie
    assert "test_odejmowanie_do_ujemnej" not in wyjscie
    assert (_liczba(wyjscie, "wybrane"), _liczba(wyjscie, "pominiete")) == (2, 6)


def test_odpowiedz_sondy_niebedaca_json_zachowuje_przyczyne_bledu_parsowania(monkeypatch, tmp_path):
    atrapa = _atrapa_sondy(stdout=f"{testy_py.ZNACZNIK_SONDY}nie json\n")
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        _sonda_z_atrapa(monkeypatch, tmp_path, atrapa)
    assert isinstance(blad.value.__cause__, ValueError)


def test_pierwszy_argument_sondy_trafia_do_parsera_pytesta_a_nie_jest_pomijany(tmp_path):
    # `--testmon` na koncu (bez niego testmon jest wylaczony): argument zerowy sondy to opcja `-k`, a nie tylko wypelniacz
    assert testy_py._powody_bez_selekcji(["-k", "x", "--testmon"], tmp_path) == ["-k was used"]


def test_odpowiedz_sondy_wsrod_innych_linii_stdout_jest_czytana_a_powody_zachowuja_kolejnosc(monkeypatch, tmp_path):
    atrapa = _atrapa_sondy(stdout=f'szum\n{testy_py.ZNACZNIK_SONDY}{_odpowiedz_sondy(noselect=["b", None, "a"])}\nszum 2\n')
    assert _sonda_z_atrapa(monkeypatch, tmp_path, atrapa) == ["b", "--testmon-noselect", "a"]


def test_odpowiedz_sondy_z_literalnym_znacznikiem_jest_czytana_niezaleznie_od_stalej_w_kodzie(monkeypatch, tmp_path):
    # znacznik wpisany tekstem, nie ze stalej: zmiana wartosci stalej w kodzie nie moze przejsc niezauwazona
    atrapa = _atrapa_sondy(stdout=f'szum SONDA-POWODY: nie to\nSONDA-POWODY:{_odpowiedz_sondy(noselect=["a"])}\n')
    assert _sonda_z_atrapa(monkeypatch, tmp_path, atrapa) == ["a"]


def test_atrapa_zamiast_testmona_w_sondzie_to_brak_dowodu_z_ostatnia_linia_bledu_importu(monkeypatch, tmp_path):
    atrapa = tmp_path / "atrapa" / "testmon"
    atrapa.mkdir(parents=True)
    (atrapa / "__init__.py").write_text("raise ImportError('brak testmona w tescie HAR-257')\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("PYTHONPATH", str(atrapa.parent))
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        testy_py._powody_bez_selekcji(["--testmon"], repo)
    assert str(blad.value).startswith("brak dowodu: sonda parsera pytesta zakonczyla sie kodem 1: ImportError")
    assert "brak testmona w tescie HAR-257" in str(blad.value)


def _zapisujaca_sonda(monkeypatch, powody: list[str]):
    wywolania: list[tuple[list[str], Path]] = []

    def sonda(argumenty, cwd):
        wywolania.append((list(argumenty), cwd))
        return powody

    monkeypatch.setattr(testy_py, "_powody_bez_selekcji", sonda)
    return wywolania


def test_przebieg_z_selekcja_pyta_sonde_o_te_same_argumenty_w_tej_samej_kolejnosci_i_w_katalogu_repo(monkeypatch, tmp_path):
    repo, baza = _repo_ze_zdrowa_baza(tmp_path)
    (repo / "tests" / "e2e").mkdir(parents=True)
    monkeypatch.setenv("TESTMON_DATAFILE", str(baza))
    wywolania = _zapisujaca_sonda(monkeypatch, [])
    uruchomione = _atrapa_z_przebiegami(["8 passed, 4 deselected in 0.1s\n"], [])
    monkeypatch.setattr(testy_py, "uruchom", uruchomione)
    assert testy_py.uruchom_dotkniete(["-q", "tests/a.py"], 5.0, repo) == 0
    oczekiwane = ["--testmon", "-o", "addopts=", "--ignore=tests/e2e", "-q", "tests/a.py"]
    assert wywolania == [(oczekiwane, repo)]
    assert uruchomione.wywolania == [oczekiwane]


def test_powod_od_sondy_przerywa_dotkniete_przed_uruchomieniem_pytesta_i_podaje_tylko_pierwszy_powod(monkeypatch, tmp_path):
    repo, baza = _repo_ze_zdrowa_baza(tmp_path)
    monkeypatch.setenv("TESTMON_DATAFILE", str(baza))
    _zapisujaca_sonda(monkeypatch, ["pierwszy", "drugi"])
    monkeypatch.setattr(testy_py, "uruchom", _nie_uruchamiaj)
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        testy_py.uruchom_dotkniete(["-q"], 5.0, repo)
    assert str(blad.value) == "brak dowodu: testmon wylaczyl selekcje (pierwszy) - uruchom bez tych argumentow"


def test_pelny_przebieg_dotknietych_nie_uruchamia_sondy_bo_nic_nie_selekcjonuje(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("TESTMON_DATAFILE", str(tmp_path / "baza" / "testmon.db"))
    wywolania = _zapisujaca_sonda(monkeypatch, ["-k was used"])
    monkeypatch.setattr(testy_py, "uruchom", _atrapa_pytesta_z_wyjsciem(["5 passed in 0.1s\n"]))
    assert testy_py.uruchom_dotkniete(["-q", "-k", "x"], 5.0, repo) == 0
    assert wywolania == []


def test_blad_sondy_w_dotknietych_to_blad_narzedzia_a_nie_uruchomienie_pytesta(monkeypatch, tmp_path):
    repo, baza = _repo_ze_zdrowa_baza(tmp_path)
    monkeypatch.setenv("TESTMON_DATAFILE", str(baza))
    monkeypatch.setattr(testy_py.subprocess, "run", _atrapa_sondy(kod=1, stderr="UsageError: zle\n"))
    monkeypatch.setattr(testy_py, "uruchom", _nie_uruchamiaj)
    with pytest.raises(testy_py.BladNarzedzia) as blad:
        testy_py.uruchom_dotkniete(["-q"], 5.0, repo)
    assert str(blad.value) == "brak dowodu: sonda parsera pytesta zakonczyla sie kodem 1: UsageError: zle"


def test_pytest_addopts_z_otoczenia_nie_trafia_do_sondy_bo_zmienna_jest_czyszczona_przed_nia(monkeypatch, tmp_path):
    repo, baza = _repo_ze_zdrowa_baza(tmp_path)
    monkeypatch.setenv("TESTMON_DATAFILE", str(baza))
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k cokolwiek")
    widziane: list[str | None] = []

    def sonda(_argumenty, _cwd):
        widziane.append(os.environ.get("PYTEST_ADDOPTS"))
        return []

    monkeypatch.setattr(testy_py, "_powody_bez_selekcji", sonda)
    monkeypatch.setattr(testy_py, "uruchom", _atrapa_pytesta_z_wyjsciem(["8 passed, 4 deselected in 0.1s\n"]))
    assert testy_py.uruchom_dotkniete(["-q"], 5.0, repo) == 0
    assert widziane == [None]


def test_pominiete_pod_xdist_sa_zmierzone_a_nie_zerem_dla_prawdziwego_projektu_z_addopts(tmp_path):
    repo = _repo_z_prawdziwym_pyproject(tmp_path)
    baza = tmp_path / "baza.db"
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-q", "-n", "2", baza=baza)
    assert kod == 0, wyjscie + bledy
    _zapisz(repo, "src/mini.py", MINI_KOD.replace("return a + b", "return b + a"))
    kod, wyjscie, bledy = _testy_dotkniete(repo, "-q", "-n", "2", baza=baza)
    assert kod == 0, wyjscie + bledy
    assert (_liczba(wyjscie, "wybrane"), _liczba(wyjscie, "pominiete")) == (2, 6)


# ============================ tools/po_merge_main.sh ============================

REWIZJA = 'revision = "{numer}"\ndown_revision = {poprzednia}\n'


def _mini_repo_po_merge(repo: Path) -> None:
    repo.mkdir()
    _init_repo(repo)
    for plik in (PO_MERGE, TESTY_PY, NARZEDZIA / "_testy_zdarzenia.py"):
        _zapisz(repo, f"tools/{plik.name}", plik.read_text(encoding="utf-8"))
    (repo / "tools" / "po_merge_main.sh").chmod(0o755)
    _zapisz(repo, "alembic.ini", "[alembic]\nscript_location = alembic\n")
    _zapisz(repo, "alembic/versions/0001_pierwsza.py", REWIZJA.format(numer="0001", poprzednia="None"))
    _zapisz(repo, "src/modul.py", "WARTOSC = 1\n")
    _zapisz(repo, "tests/test_ok.py", "def test_ok():\n    assert True\n")
    _commit(repo, "stan poczatkowy")


@pytest.fixture
def repo_po_merge(tmp_path):
    repo = tmp_path / "repo"
    _mini_repo_po_merge(repo)
    return repo


def _po_merge(repo: Path, *, testy_args: str | None = "-q", srodowisko: dict[str, str] | None = None):
    zmienne = {"PYTHON": sys.executable, **(srodowisko or {})}
    if testy_args is not None:
        zmienne["PO_MERGE_TESTY_ARGS"] = testy_args
    kod, wyjscie, bledy = _uruchom(["bash", str(repo / "tools" / "po_merge_main.sh")], KORZEN, zmienne)
    return kod, wyjscie.splitlines()[-1], wyjscie, bledy


def test_po_merge_wszystko_poprawne_daje_ok_kod_0_i_uruchamia_testy_na_czterech_workerach(repo_po_merge):
    kod, ostatnia, wyjscie, _ = _po_merge(repo_po_merge, testy_args=None)
    assert kod == 0
    assert ostatnia == "PO-MERGE: ok"
    assert "bringing up nodes" in wyjscie
    for etap in ("konflikt", "alembic", "testy"):
        assert f"PO-MERGE etap {etap}: ok" in wyjscie
    # 2026-11-24 wlasciciel: tabele mutantow recznych nie sa juz bramka
    assert "etap mutanty" not in wyjscie


@pytest.mark.parametrize(
    "tresc",
    [
        pytest.param(f"{ZNACZNIK_START} HEAD\nx = 1\n{ZNACZNIK_KONIEC} origin/main\n", id="oba_znaczniki"),
        pytest.param(f"x = 1\n{ZNACZNIK_START} HEAD\n", id="tylko_poczatek"),
        pytest.param(f"x = 1\n{ZNACZNIK_KONIEC} origin/main\n", id="tylko_koniec"),
        pytest.param(f"x = 1\n{ZNACZNIK_START}\n", id="znacznik_bez_tekstu_na_koncu_linii"),
        pytest.param(f"x = 1\n{ZNACZNIK_KONIEC}\n", id="znacznik_koncowy_bez_tekstu"),
    ],
)
def test_po_merge_znacznik_konfliktu_w_sledzonym_pliku_daje_blad_etapu_konflikt(repo_po_merge, tresc):
    _zapisz(repo_po_merge, "src/inny.py", tresc)
    _git(repo_po_merge, "add", "-A")
    kod, ostatnia, _, bledy = _po_merge(repo_po_merge)
    assert kod == 1
    assert ostatnia == "PO-MERGE: blad konflikt"
    assert "src/inny.py:2:" in bledy or "src/inny.py:1:" in bledy


@pytest.mark.parametrize(
    "tresc",
    [
        pytest.param(f"{ZNACZNIK_START[:6]} HEAD\n", id="szesc_znakow"),
        pytest.param(f" {ZNACZNIK_START} HEAD\n", id="wciety_znacznik"),
        pytest.param(f"{ZNACZNIK_START}HEAD\n", id="bez_spacji_po_znaczniku"),
        pytest.param(f"{ZNACZNIK_KONIEC[:6]} x\n", id="szesc_znakow_koncowych"),
    ],
)
def test_po_merge_tekst_podobny_do_znacznika_konfliktu_nie_jest_konfliktem(repo_po_merge, tresc):
    _zapisz(repo_po_merge, "src/inny.py", tresc)
    _git(repo_po_merge, "add", "-A")
    kod, ostatnia, _, _ = _po_merge(repo_po_merge)
    assert (kod, ostatnia) == (0, "PO-MERGE: ok")


def test_po_merge_wpis_niescalony_w_indeksie_bez_znacznikow_tez_jest_konfliktem(repo_po_merge):
    repo = repo_po_merge
    _git(repo, "checkout", "-q", "-b", "boczna")
    _zapisz(repo, "src/modul.py", "WARTOSC = 1\nINNA = 'boczna'\n")
    _commit(repo, "boczna")
    _git(repo, "checkout", "-q", "main")
    _zapisz(repo, "src/modul.py", "WARTOSC = 1\nINNA = 'main'\n")
    _commit(repo, "main")
    kod, _, _ = _uruchom(["git", "merge", "boczna"], repo, limit=60.0)
    assert kod == 1  # konflikt, merge zatrzymany
    _zapisz(repo, "src/modul.py", "WARTOSC = 1\n")  # znaczniki usuniete, indeks nadal niescalony
    kod, ostatnia, _, bledy = _po_merge(repo)
    assert kod == 1
    assert ostatnia == "PO-MERGE: blad konflikt"
    assert "src/modul.py" in bledy


@pytest.mark.parametrize(
    "dodatkowe_rewizje",
    [
        pytest.param({"0002_druga.py": ("0002", "None")}, id="dwie_glowy"),
        pytest.param({"0002_druga.py": ("0002", '"0001"'), "0003_trzecia.py": ("0003", '"0001"')}, id="rozgalezienie"),
    ],
)
def test_po_merge_wiecej_niz_jedna_glowa_alembica_daje_blad_etapu_alembic(repo_po_merge, dodatkowe_rewizje):
    for nazwa, (numer, poprzednia) in dodatkowe_rewizje.items():
        _zapisz(
            repo_po_merge,
            f"alembic/versions/{nazwa}",
            REWIZJA.format(numer=numer, poprzednia=poprzednia),
        )
    _git(repo_po_merge, "add", "-A")
    kod, ostatnia, _, bledy = _po_merge(repo_po_merge)
    assert kod == 1
    assert ostatnia == "PO-MERGE: blad alembic"
    assert "oczekiwano jednej glowy, jest 2" in bledy


def test_po_merge_brak_migracji_to_brak_glowy_i_blad_etapu_alembic(repo_po_merge):
    _git(repo_po_merge, "rm", "-q", "alembic/versions/0001_pierwsza.py")
    (repo_po_merge / "alembic" / "versions").mkdir(parents=True, exist_ok=True)  # git rm usunal puste katalogi
    kod, ostatnia, _, bledy = _po_merge(repo_po_merge)
    assert kod == 1
    assert ostatnia == "PO-MERGE: blad alembic"
    assert "oczekiwano jednej glowy, jest 0" in bledy


def test_po_merge_alembic_ktory_sam_pada_to_blad_narzedzia_kod_2(repo_po_merge):
    _git(repo_po_merge, "rm", "-q", "alembic.ini")
    kod, ostatnia, _, bledy = _po_merge(repo_po_merge)
    assert kod == 2
    assert ostatnia == "PO-MERGE: blad alembic"
    assert "alembic heads zakonczyl sie kodem" in bledy


def test_po_merge_czerwony_test_daje_blad_etapu_testy_kod_1(repo_po_merge):
    _zapisz(repo_po_merge, "tests/test_ok.py", "def test_ok():\n    assert 1 == 2\n")
    kod, ostatnia, _, _ = _po_merge(repo_po_merge)
    assert kod == 1
    assert ostatnia == "PO-MERGE: blad testy"


def test_po_merge_zaden_test_nie_zebrany_to_brak_dowodu_kod_2_a_nie_czerwony_test(repo_po_merge):
    kod, ostatnia, _, _ = _po_merge(repo_po_merge, testy_args="-q -k nie_ma_takiego_testu")
    assert kod == 2
    assert ostatnia == "PO-MERGE: blad testy"


def test_po_merge_zawieszony_test_daje_blad_etapu_testy_kod_2(repo_po_merge):
    _zapisz(repo_po_merge, "tests/test_ok.py", "import threading\n\n\ndef test_ok():\n    threading.Event().wait()\n")
    kod, ostatnia, wyjscie, _ = _po_merge(repo_po_merge, testy_args="--bez-postepu 12 -q")
    assert kod == 2
    assert ostatnia == "PO-MERGE: blad testy"
    assert "zawieszenie: tests/test_ok.py::test_ok" in wyjscie


def test_po_merge_poza_repozytorium_git_etap_konflikt_zglasza_blad_narzedzia_kod_2(tmp_path):
    repo = tmp_path / "repo"
    _mini_repo_po_merge(repo)
    shutil.move(str(repo / ".git"), str(tmp_path / "git_schowany"))
    kod, ostatnia, _, bledy = _po_merge(repo)
    assert kod == 2
    assert ostatnia == "PO-MERGE: blad konflikt"
    assert "git grep zakonczyl sie kodem" in bledy


def test_po_merge_blad_odczytu_indeksu_to_blad_narzedzia_a_nie_brak_konfliktu(repo_po_merge, tmp_path):
    atrapa = _atrapa_git(tmp_path, pada="ls-files")
    kod, ostatnia, _, bledy = _po_merge(repo_po_merge, srodowisko=atrapa)
    assert kod == 2
    assert ostatnia == "PO-MERGE: blad konflikt"
    assert "git ls-files -u zakonczyl sie kodem 1" in bledy


def test_po_merge_przy_dwoch_bledach_o_tym_samym_kodzie_nazywa_pierwszy_etap(repo_po_merge):
    _zapisz(repo_po_merge, "src/inny.py", f"{ZNACZNIK_START} HEAD\n")  # konflikt, kod 1
    _zapisz(repo_po_merge, "tests/test_ok.py", "def test_ok():\n    assert 1 == 2\n")  # testy, kod 1
    _git(repo_po_merge, "add", "-A")
    kod, ostatnia, _, _ = _po_merge(repo_po_merge)
    assert kod == 1
    assert ostatnia == "PO-MERGE: blad konflikt"


def test_po_merge_blad_narzedzia_wygrywa_z_bledem_w_kodzie_niezaleznie_od_kolejnosci_etapow(repo_po_merge):
    _zapisz(repo_po_merge, "src/inny.py", f"{ZNACZNIK_START} HEAD\n")  # konflikt, kod 1 - pierwszy etap
    _git(repo_po_merge, "add", "-A")
    kod, ostatnia, _, _ = _po_merge(repo_po_merge, testy_args="-q -k nie_ma_takiego_testu")  # testy: kod 2
    assert kod == 2
    assert ostatnia == "PO-MERGE: blad testy"


def test_po_merge_wszystkie_etapy_wykonuja_sie_mimo_wczesniejszego_bledu(repo_po_merge):
    _zapisz(repo_po_merge, "src/inny.py", f"{ZNACZNIK_START} HEAD\n")
    _git(repo_po_merge, "add", "-A")
    kod, _, wyjscie, bledy = _po_merge(repo_po_merge)
    assert kod == 1
    assert "PO-MERGE etap alembic: ok" in wyjscie
    assert "PO-MERGE etap testy: ok" in wyjscie
    assert "PO-MERGE etap konflikt: blad (kod 1)" in bledy


def _atrapa_git(tmp_path: Path, *, pada: str = "") -> dict[str, str]:
    """`git` w PATH: dopisuje argumenty do dziennika i deleguje do prawdziwego gita, a podkomenda
    `pada` konczy sie kodem 1 bez wolania prawdziwego gita. Zwraca zmienne srodowiska."""
    katalog = tmp_path / "atrapy_git"
    katalog.mkdir(exist_ok=True)
    dziennik = katalog / "git.log"
    dziennik.touch()
    skrypt = katalog / "git"
    skrypt.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$ATRAPA_DZIENNIK_GIT"\n'
        'for a in "$@"; do\n'
        '  if [ -n "$ATRAPA_GIT_PADA" ] && [ "$a" = "$ATRAPA_GIT_PADA" ]; then echo "atrapa: awaria $a" >&2; exit 1; fi\n'
        'done\n'
        'exec "$PRAWDZIWY_GIT" "$@"\n',
        encoding="utf-8",
    )
    skrypt.chmod(0o755)
    return {
        "PATH": f"{katalog}{os.pathsep}{os.environ['PATH']}",
        "PRAWDZIWY_GIT": shutil.which("git") or "git",
        "ATRAPA_DZIENNIK_GIT": str(dziennik),
        "ATRAPA_GIT_PADA": pada,
    }


# ============================ tools/liczby_raportu.py ============================

TEST_A_PRZED = """def test_stary_jeden():
    pass


def test_stary_dwa():
    pass


class TestGrupa:
    def test_w_klasie(self):
        pass
"""
TEST_A_PO = """def test_stary_jeden():
    pass


class TestGrupa:
    def test_w_klasie(self):
        pass
"""
TEST_B = """def test_nowy_jeden():
    pass


async def test_nowy_asynchroniczny():
    pass


def pomocnik_testu():
    pass


class TestNowa:
    def test_metoda(self):
        pass

    def pomocnicza(self):
        pass


class Pomocnicza:
    def test_nie_zbierany_przez_pytest(self):
        pass
"""


# Sciezka skladana z czesci: pelny literal lamie kryterium `git grep` HAR-260.
DAWNNA_TABELA_TICKETU = "tests/mutanty_" + "reczne/HAR-7.toml"


@pytest.fixture
def repo_liczb(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _zapisz(repo, "tests/test_a.py", TEST_A_PRZED)
    _zapisz(repo, "tests/test_c.py", "def test_c():\n    pass\n")
    _zapisz(repo, "tests/dane/HAR-2.toml", "# plik w katalogu bez znaczenia\n")
    _zapisz(repo, "src/x.py", "X = 1\n")
    _commit(repo, "main")
    _git(repo, "checkout", "-q", "-b", "feat-HAR-7")
    _zapisz(repo, "tests/test_a.py", TEST_A_PO)  # zmiana skomitowana
    _git(repo, "rm", "-q", "tests/test_c.py")
    _commit(repo, "zmiany na galezi")
    _zapisz(repo, "tests/test_b.py", TEST_B)  # nowe pliki i zmiany niezacommitowane
    _zapisz(repo, "tests/dane/HAR-2.toml", "# plik zmieniony\n")
    _zapisz(repo, DAWNNA_TABELA_TICKETU, "# dawna tabela ticketu\n")
    _zapisz(repo, "inne/HAR-9.toml", "nie ta sciezka\n")
    _zapisz(repo, "src/x.py", "X = 5\n")
    _zapisz(repo, "docs/notatka.md", "notatka\n")
    return repo


def _liczby(repo: Path, *argumenty: str):
    return _uruchom([sys.executable, str(LICZBY), "--korzen", str(repo), "--baza", "main", *argumenty], KORZEN)


def _sekcja(wyjscie: str, naglowek: str) -> list[str]:
    """Linie listy zaraz po `**naglowek:** N` (bez samego naglowka)."""
    linie = wyjscie.splitlines()
    start = next(i for i, wiersz in enumerate(linie) if wiersz.startswith(f"**{naglowek}:**"))
    wynik = []
    for linia in linie[start + 1 :]:
        if not linia.startswith("- "):
            break
        wynik.append(linia)
    return wynik


def test_liczby_raportu_wypisuja_nowe_i_usuniete_testy_jako_node_id_z_drzewa_skladni(repo_liczb):
    kod, wyjscie, _ = _liczby(repo_liczb)
    assert kod == 0
    assert "**Nowe testy:** 3" in wyjscie.splitlines()
    assert _sekcja(wyjscie, "Nowe testy") == [
        "- `tests/test_b.py::TestNowa::test_metoda`",
        "- `tests/test_b.py::test_nowy_asynchroniczny`",
        "- `tests/test_b.py::test_nowy_jeden`",
    ]
    assert "**Usuniete testy:** 2" in wyjscie.splitlines()
    assert _sekcja(wyjscie, "Usuniete testy") == [
        "- `tests/test_a.py::test_stary_dwa`",
        "- `tests/test_c.py::test_c`",
    ]


def test_liczby_raportu_bez_listy_plikow_nie_wypisuja_sekcji_spoza_przydzialu(repo_liczb):
    kod, wyjscie, _ = _liczby(repo_liczb)
    assert kod == 0
    assert "spoza listy" not in wyjscie


def test_liczby_raportu_pliki_spoza_listy_pliki_dotykane_maja_dopisek_wymaga_stop(repo_liczb):
    kod, wyjscie, _ = _liczby(repo_liczb, "--pliki", "tests/test_a.py,tests/test_b.py src/ docs/*.md")
    assert kod == 0
    assert "**Zmienione pliki spoza listy Pliki dotykane:** 4" in wyjscie.splitlines()
    assert _sekcja(wyjscie, "Zmienione pliki spoza listy Pliki dotykane") == [
        "- `inne/HAR-9.toml` - wymaga STOP:",
        "- `tests/dane/HAR-2.toml` - wymaga STOP:",
        f"- `{DAWNNA_TABELA_TICKETU}` - wymaga STOP:",
        "- `tests/test_c.py` - wymaga STOP:",
    ]


def test_liczby_raportu_plik_o_nazwie_ticketu_poza_lista_nie_ma_juz_wyjatku_i_wymaga_stop(repo_liczb):
    kod, wyjscie, _ = _liczby(repo_liczb, "--pliki", "tests/test_a.py")
    assert kod == 0
    assert f"- `{DAWNNA_TABELA_TICKETU}` - wymaga STOP:" in wyjscie.splitlines()


def test_liczby_raportu_nie_wypisuja_wierszy_tabel_ani_sekcji_cudzych_tabel(repo_liczb):
    kod, wyjscie, _ = _liczby(repo_liczb, "--pliki", "tests/test_a.py")
    assert kod == 0
    assert "[[mutant]]" not in wyjscie
    assert "[[rownowazny]]" not in wyjscie
    assert "Zmienione tabele innych ticketow" not in wyjscie


def test_liczby_raportu_biora_ticket_z_nazwy_galezi_a_jawny_argument_go_nadpisuje(repo_liczb):
    kod, wyjscie, _ = _liczby(repo_liczb)
    assert kod == 0
    assert wyjscie.splitlines()[0] == "## Liczby z pliku i diffu (HAR-7, baza main)"
    kod, wyjscie, _ = _liczby(repo_liczb, "--ticket", "HAR-8")
    assert kod == 0
    assert wyjscie.splitlines()[0] == "## Liczby z pliku i diffu (HAR-8, baza main)"


def test_liczby_raportu_nieznana_baza_to_blad_kod_2_bez_liczb(repo_liczb):
    kod, wyjscie, bledy = _uruchom(
        [sys.executable, str(LICZBY), "--korzen", str(repo_liczb), "--baza", "nie-ma-takiego-refa"], KORZEN
    )
    assert kod == 2
    assert wyjscie == ""
    assert "BLAD: git merge-base nie-ma-takiego-refa HEAD zakonczyl sie kodem" in bledy


def test_liczby_raportu_ticket_bez_zadnego_pliku_z_tabela_nie_jest_bledem(repo_liczb):
    kod, wyjscie, bledy = _liczby(repo_liczb, "--ticket", "HAR-99")
    assert kod == 0, bledy
    assert wyjscie.splitlines()[0] == "## Liczby z pliku i diffu (HAR-99, baza main)"


def test_liczby_raportu_galaz_bez_klucza_ticketu_wymaga_jawnego_argumentu(repo_liczb):
    _git(repo_liczb, "checkout", "-q", "-b", "bez-klucza")
    kod, _, bledy = _liczby(repo_liczb)
    assert kod == 2
    assert "nie ustalono ticketu z galezi 'bez-klucza'" in bledy


@pytest.mark.parametrize("klucz", ["7", "HAR-", "har-7", "HAR-7x", "../HAR-7"])
def test_liczby_raportu_zly_klucz_ticketu_to_blad_kod_2(repo_liczb, klucz):
    kod, _, bledy = _liczby(repo_liczb, "--ticket", klucz)
    assert kod == 2
    assert f"zly klucz ticketu {klucz!r}" in bledy


def test_liczby_raportu_pusta_lista_plikow_to_blad_kod_2(repo_liczb):
    kod, wyjscie, bledy = _liczby(repo_liczb, "--pliki", " , ")
    assert kod == 2
    assert wyjscie == ""
    assert "--pliki: lista jest pusta" in bledy


def test_liczby_raportu_plik_testowy_ktorego_nie_da_sie_sparsowac_to_blad_kod_2(repo_liczb):
    _zapisz(repo_liczb, "tests/test_zepsuty.py", "def test_x(:\n")
    kod, _, bledy = _liczby(repo_liczb)
    assert kod == 2
    assert "tests/test_zepsuty.py: nie da sie sparsowac pliku testowego" in bledy


def test_liczby_raportu_zmiana_pliku_nietestowego_i_testu_poza_tests_nie_liczy_sie_jako_test(repo_liczb):
    _zapisz(repo_liczb, "src/test_pomocnik.py", "def test_poza_katalogiem_tests():\n    pass\n")
    _zapisz(repo_liczb, "tests/pomocnik.py", "def test_w_pliku_bez_prefiksu():\n    pass\n")
    _zapisz(repo_liczb, "tests/test_notatka.txt", "to nie jest kod pythona (\n")
    kod, wyjscie, _ = _liczby(repo_liczb)
    assert kod == 0
    assert "**Nowe testy:** 3" in wyjscie.splitlines()


def test_liczby_raportu_zamiana_pliku_na_dowiazanie_symboliczne_to_zmiana_pliku(repo_liczb):
    (repo_liczb / "src" / "x.py").unlink()
    os.symlink("../docs/notatka.md", repo_liczb / "src" / "x.py")
    kod, wyjscie, _ = _liczby(repo_liczb, "--pliki", "tests/test_a.py")
    assert kod == 0
    assert "- `src/x.py` - wymaga STOP:" in wyjscie.splitlines()


def test_liczby_raportu_plik_testowy_ktorego_nie_da_sie_odczytac_to_blad_kod_2(repo_liczb):
    os.symlink("/nie/ma/takiego/pliku.py", repo_liczb / "tests" / "test_wiszacy.py")
    kod, wyjscie, bledy = _liczby(repo_liczb)
    assert kod == 2
    assert wyjscie == ""
    assert "BLAD: nieczytelny plik:" in bledy


def test_liczby_raportu_plik_testowy_w_zlym_kodowaniu_to_blad_kod_2(repo_liczb):
    (repo_liczb / "tests" / "test_zly_kod.py").write_bytes(b"\xff\xfe\x00")
    kod, wyjscie, bledy = _liczby(repo_liczb)
    assert kod == 2
    assert wyjscie == ""
    assert "BLAD: nieczytelny plik:" in bledy


def test_liczby_raportu_brak_gita_w_srodowisku_to_blad_kod_2_a_nie_pusty_diff(repo_liczb, tmp_path):
    pusty = tmp_path / "pusty_path"
    pusty.mkdir()
    kod, wyjscie, bledy = _uruchom(
        [sys.executable, str(LICZBY), "--korzen", str(repo_liczb), "--baza", "main", "--ticket", "HAR-7"],
        KORZEN,
        {"PATH": str(pusty)},
    )
    assert kod == 2
    assert wyjscie == ""
    assert "BLAD: git merge-base main HEAD:" in bledy


@pytest.mark.parametrize(
    "tresc",
    [
        pytest.param("a" + ".b" * 200000, id="zbyt_gleboka_rekurencja"),
        pytest.param("-" * 100000 + "1", id="przepelniony_stos_parsera"),
    ],
)
def test_liczby_raportu_zlosliwy_plik_testowy_to_blad_dziedziny_a_nie_traceback(repo_liczb, tresc):
    _zapisz(repo_liczb, "tests/test_zlosliwy.py", tresc + "\n")
    kod, wyjscie, bledy = _liczby(repo_liczb)
    assert kod == 2
    assert wyjscie == ""
    assert "tests/test_zlosliwy.py: nie da sie sparsowac pliku testowego" in bledy


def test_liczby_raportu_przekroczony_czas_gita_to_blad_dziedziny(monkeypatch, tmp_path):
    def za_dlugo(*_argumenty, **_nazwane):
        raise subprocess.TimeoutExpired("git", 120)

    monkeypatch.setattr(liczby_py.subprocess, "run", za_dlugo)
    with pytest.raises(liczby_py.BladNarzedzia, match="git branch --show-current: Command 'git' timed out"):
        liczby_py._git(["branch", "--show-current"], tmp_path)


def test_liczby_raportu_nieznany_status_zmiany_to_blad_dziedziny_a_nie_pominiecie(monkeypatch, tmp_path):
    odpowiedzi = iter([b"abc123\n", b"U\0a.txt\0"])
    monkeypatch.setattr(liczby_py, "_git", lambda _argumenty, _korzen: next(odpowiedzi))
    with pytest.raises(liczby_py.BladNarzedzia, match="nieznany status zmiany 'U' pliku a.txt"):
        liczby_py.zmienione_pliki(tmp_path, "main")


@pytest.fixture
def repo_przeniesienia(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _zapisz(repo, "tests/test_r.py", "def test_r():\n    pass\n")
    _zapisz(repo, ".gitignore", "tests/test_ignorowany.py\n")
    _commit(repo, "start")
    _git(repo, "checkout", "-q", "-b", "feat-HAR-7")
    return repo


def test_liczby_raportu_przeniesiony_plik_testowy_to_usuniety_i_nowy_a_nie_rename(repo_przeniesienia):
    _git(repo_przeniesienia, "mv", "tests/test_r.py", "tests/test_s.py")
    kod, wyjscie, _ = _liczby(repo_przeniesienia)
    assert kod == 0
    assert _sekcja(wyjscie, "Nowe testy") == ["- `tests/test_s.py::test_r`"]
    assert _sekcja(wyjscie, "Usuniete testy") == ["- `tests/test_r.py::test_r`"]


def test_liczby_raportu_plik_ignorowany_przez_gitignore_nie_liczy_sie(repo_przeniesienia):
    _zapisz(repo_przeniesienia, "tests/test_ignorowany.py", "def test_ignorowany():\n    pass\n")
    kod, wyjscie, _ = _liczby(repo_przeniesienia)
    assert kod == 0
    assert "**Nowe testy:** 0" in wyjscie.splitlines()


def test_liczby_raportu_blad_gita_przy_sprawdzaniu_pliku_w_bazie_to_blad_a_nie_nowe_testy(monkeypatch, tmp_path):
    def pada(_argumenty, _korzen):
        raise liczby_py.BladNarzedzia("git ls-tree: uszkodzony obiekt")

    monkeypatch.setattr(liczby_py, "_git", pada)
    with pytest.raises(liczby_py.BladNarzedzia, match="uszkodzony obiekt"):
        liczby_py._czy_w_bazie(tmp_path, "abc", "tests/test_a.py")


def test_liczby_raportu_merge_base_bez_commita_to_blad_dziedziny(monkeypatch, tmp_path):
    monkeypatch.setattr(liczby_py, "_git", lambda _argumenty, _korzen: b"\n")
    with pytest.raises(liczby_py.BladNarzedzia, match="git merge-base main HEAD nie zwrocil commita"):
        liczby_py.zmienione_pliki(tmp_path, "main")


def test_liczby_raportu_nieparzysta_liczba_pol_diffu_to_blad_dziedziny(monkeypatch, tmp_path):
    odpowiedzi = iter([b"abc123\n", b"M\0"])
    monkeypatch.setattr(liczby_py, "_git", lambda _argumenty, _korzen: next(odpowiedzi))
    with pytest.raises(liczby_py.BladNarzedzia, match="niezrozumiale wyjscie git diff --name-status -z"):
        liczby_py.zmienione_pliki(tmp_path, "main")


# ================================ tools/oddaj_pr.sh ================================

ADRES_PR = "https://github.com/LukBro/proba/pull/1"


def _atrapa_gh(tmp_path: Path) -> dict[str, str]:
    katalog = tmp_path / "atrapy_gh"
    katalog.mkdir(exist_ok=True)
    dziennik = katalog / "gh.log"
    dziennik.touch()
    skrypt = katalog / "gh"
    skrypt.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$ATRAPA_DZIENNIK_GH"\n'
        'if [ -n "$ATRAPA_GH_PADA" ]; then echo "atrapa gh: awaria" >&2; exit 1; fi\n'
        f'echo "{ADRES_PR}"\n',
        encoding="utf-8",
    )
    skrypt.chmod(0o755)
    return {"ATRAPA_DZIENNIK_GH": str(dziennik), "PATH_GH": str(katalog)}


class Oddanie:
    """Mini-repozytorium z galezia robocza, zdalnym `origin` (repo bare) i atrapami `git` oraz `gh`."""

    def __init__(self, tmp_path: Path, *, pada_git: str = "", pada_gh: bool = False):
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        zdalne = tmp_path / "origin.git"
        zdalne.mkdir()
        _git(zdalne, "init", "-q", "--bare", "-b", "main")
        _init_repo(self.repo)
        _zapisz(self.repo, "tools/oddaj_pr.sh", ODDAJ.read_text(encoding="utf-8"))
        (self.repo / "tools" / "oddaj_pr.sh").chmod(0o755)
        _zapisz(self.repo, "README.md", "start\n")
        _commit(self.repo, "start")
        _git(self.repo, "remote", "add", "origin", str(zdalne))
        _git(self.repo, "push", "-q", "-u", "origin", "main")
        _git(self.repo, "checkout", "-q", "-b", "feat-HAR-1")
        _zapisz(self.repo, "src/zmiana.py", "x = 1\n")
        _commit(self.repo, "zmiana")
        self.tresc = tmp_path / "tresc.md"
        self.tresc.write_text("Opis PR\n", encoding="utf-8")
        atrapa_git = _atrapa_git(tmp_path, pada=pada_git)
        atrapa_gh = _atrapa_gh(tmp_path)
        self.dziennik_git = Path(atrapa_git["ATRAPA_DZIENNIK_GIT"])
        self.dziennik_gh = Path(atrapa_gh["ATRAPA_DZIENNIK_GH"])
        self.srodowisko = {
            **atrapa_git,
            "PATH": f"{atrapa_gh['PATH_GH']}{os.pathsep}{atrapa_git['PATH']}",
            "ATRAPA_DZIENNIK_GH": atrapa_gh["ATRAPA_DZIENNIK_GH"],
            "ATRAPA_GH_PADA": "1" if pada_gh else "",
        }
        # Dziennik zaczyna sie PO przygotowaniu repozytorium: zawiera tylko to, co robi skrypt.
        self.dziennik_git.write_text("", encoding="utf-8")

    def oddaj(self, *argumenty: str, domyslne: bool = True):
        polecenie = ["bash", str(self.repo / "tools" / "oddaj_pr.sh")]
        if domyslne:
            polecenie += ["--tytul", "HAR-1: proba", "--tresc", str(self.tresc)]
        kod, wyjscie, bledy = _uruchom([*polecenie, *argumenty], KORZEN, self.srodowisko)
        return kod, wyjscie, bledy

    def wolania_git(self, podkomenda: str) -> list[str]:
        return [
            wiersz for wiersz in self.dziennik_git.read_text(encoding="utf-8").splitlines() if wiersz.split(" ")[0] == podkomenda
        ]

    def wolania_gh(self) -> list[str]:
        return self.dziennik_gh.read_text(encoding="utf-8").splitlines()

    def nic_nie_wypchniete(self) -> bool:
        return not self.wolania_git("push") and not self.wolania_gh()


@pytest.fixture
def oddanie(tmp_path):
    return Oddanie(tmp_path)


def test_oddanie_poprawnej_pracy_wypycha_galaz_i_tworzy_pr_z_trescia_z_pliku(oddanie):
    kod, wyjscie, _ = oddanie.oddaj()
    assert kod == 0
    assert wyjscie.splitlines()[-1] == f"ODDAJ-PR: ok {ADRES_PR}"
    assert oddanie.wolania_git("push") == ["push -u origin feat-HAR-1"]
    assert oddanie.wolania_gh() == [
        f"pr create --base main --head feat-HAR-1 --title HAR-1: proba --body-file {oddanie.tresc}"
    ]
    assert "merge reczny" not in wyjscie
    zdalna_galaz = _git(oddanie.repo, "ls-remote", "--heads", "origin", "feat-HAR-1")
    assert zdalna_galaz.split()[0] == _git(oddanie.repo, "rev-parse", "HEAD").strip()


def test_oddanie_ze_zlym_adresem_autora_odmawia_i_nic_nie_wypycha(oddanie):
    _git(oddanie.repo, "config", "user.email", "ktos@firma.example")
    kod, wyjscie, bledy = oddanie.oddaj()
    assert kod == 1
    assert "git config user.email to 'ktos@firma.example'" in bledy
    assert wyjscie.splitlines()[-1].startswith("ODDAJ-PR: odmowa git config user.email")
    assert oddanie.nic_nie_wypchniete()


def test_oddanie_ze_zla_nazwa_autora_odmawia_i_nic_nie_wypycha(oddanie):
    _git(oddanie.repo, "config", "user.name", "Ktos Inny")
    kod, _, bledy = oddanie.oddaj()
    assert kod == 1
    assert "git config user.name to 'Ktos Inny'" in bledy
    assert oddanie.nic_nie_wypchniete()


@pytest.mark.parametrize("linia", ["Co-Authored-By: Ktos <ktos@example.com>", "co-authored-by: ktos <k@example.com>"])
def test_oddanie_commitu_ze_wspolautorem_odmawia_i_nic_nie_wypycha(oddanie, linia):
    _zapisz(oddanie.repo, "src/drugi.py", "y = 2\n")
    _git(oddanie.repo, "add", "-A")
    _git(oddanie.repo, "commit", "-q", "-m", f"drugi commit\n\n{linia}")
    kod, wyjscie, bledy = oddanie.oddaj()
    assert kod == 1
    assert "Co-Authored-By" in bledy
    assert wyjscie.splitlines()[-1].startswith("ODDAJ-PR: odmowa commit galezi ma linie Co-Authored-By")
    assert oddanie.nic_nie_wypchniete()


def test_oddanie_wspolautor_w_srodku_linii_a_nie_na_jej_poczatku_nie_blokuje(oddanie):
    _zapisz(oddanie.repo, "src/drugi.py", "y = 2\n")
    _git(oddanie.repo, "add", "-A")
    _git(oddanie.repo, "commit", "-q", "-m", "opis wspomina Co-Authored-By: w srodku zdania")
    kod, _, _ = oddanie.oddaj()
    assert kod == 0


def test_oddanie_wspolautor_w_commicie_spoza_galezi_nie_blokuje(tmp_path):
    oddanie = Oddanie(tmp_path)
    _git(oddanie.repo, "checkout", "-q", "main")
    _zapisz(oddanie.repo, "src/stary.py", "z = 3\n")
    _git(oddanie.repo, "add", "-A")
    _git(oddanie.repo, "commit", "-q", "-m", "stary\n\nCo-Authored-By: Ktos <k@example.com>")
    _git(oddanie.repo, "push", "-q", "origin", "main")
    _git(oddanie.repo, "checkout", "-q", "feat-HAR-1")
    _git(oddanie.repo, "rebase", "-q", "main")
    kod, _, _ = oddanie.oddaj()
    assert kod == 0


@pytest.mark.parametrize(
    "przygotuj",
    [
        pytest.param(lambda repo: _zapisz(repo, "README.md", "zmieniony\n"), id="zmieniony_sledzony"),
        pytest.param(lambda repo: _zapisz(repo, "nowy.txt", "nowy\n"), id="nowy_plik"),
        pytest.param(lambda repo: _zapisz(repo, "katalog/glebiej/nowy.txt", "nowy\n"), id="nowy_plik_w_katalogu"),
    ],
)
def test_oddanie_przy_brudnym_drzewie_odmawia_i_nic_nie_wypycha(oddanie, przygotuj):
    przygotuj(oddanie.repo)
    kod, wyjscie, bledy = oddanie.oddaj()
    assert kod == 1
    assert "drzewo robocze nie jest czyste, brudne:" in bledy
    assert wyjscie.splitlines()[-1].startswith("ODDAJ-PR: odmowa drzewo robocze nie jest czyste")
    assert oddanie.nic_nie_wypchniete()


def test_oddanie_brudny_plik_z_listy_dozwolonych_nie_blokuje_ale_inny_brudny_plik_tak(oddanie):
    _zapisz(oddanie.repo, "notatki/sesja.md", "robocze\n")
    kod, _, _ = oddanie.oddaj("--dozwolone", "notatki/sesja.md")
    assert kod == 0
    _zapisz(oddanie.repo, "inny.txt", "cos\n")
    kod, _, bledy = oddanie.oddaj("--dozwolone", "notatki/sesja.md")
    assert kod == 1
    assert "brudne: inny.txt" in bledy
    assert "notatki/sesja.md" not in bledy


@pytest.mark.parametrize(
    "sciezka",
    [
        ".github/workflows/ci.yml",
        "pyproject.toml",
        "alembic/versions/0099_nowa.py",
        "CLAUDE.md",
        ".claude/settings.json",
        ".claude/hooks/straznik.py",
        "src/runner/secrets.py",
        "alembic.ini",
    ],
)
def test_oddanie_diff_ze_sciezka_wrazliwa_tworzy_pr_i_wypisuje_merge_reczny(oddanie, sciezka):
    _zapisz(oddanie.repo, sciezka, "zmiana\n")
    _commit(oddanie.repo, "wrazliwa")
    kod, wyjscie, _ = oddanie.oddaj()
    assert kod == 0
    assert f"merge reczny: diff dotyka sciezek wrazliwych: {sciezka}" in wyjscie.splitlines()
    assert wyjscie.splitlines()[-1] == f"ODDAJ-PR: ok {ADRES_PR}"
    assert len(oddanie.wolania_gh()) == 1
    assert oddanie.wolania_git("push") == ["push -u origin feat-HAR-1"]


@pytest.mark.parametrize(
    "sciezka",
    ["src/runner/secrets_inne.py", "src/cockpit/pyproject.txt", "docs/CLAUDE.md", "alembic/env.py", "src/.claude/hooks/x.py"],
)
def test_oddanie_sciezka_podobna_do_wrazliwej_ale_inna_nie_daje_merge_recznego(oddanie, sciezka):
    _zapisz(oddanie.repo, sciezka, "zmiana\n")
    _commit(oddanie.repo, "podobna")
    kod, wyjscie, _ = oddanie.oddaj()
    assert kod == 0
    assert "merge reczny" not in wyjscie


def test_oddanie_przy_wielu_sciezkach_wrazliwych_wypisuje_wszystkie(oddanie):
    _zapisz(oddanie.repo, "pyproject.toml", "a\n")
    _zapisz(oddanie.repo, ".github/workflows/ci.yml", "b\n")
    _commit(oddanie.repo, "dwie")
    _, wyjscie, _ = oddanie.oddaj()
    assert "merge reczny: diff dotyka sciezek wrazliwych: .github/workflows/ci.yml pyproject.toml" in wyjscie.splitlines()


def test_oddanie_z_galezi_main_odmawia_i_nic_nie_wypycha(oddanie):
    _git(oddanie.repo, "checkout", "-q", "main")
    kod, wyjscie, _ = oddanie.oddaj()
    assert kod == 1
    assert wyjscie.splitlines()[-1] == "ODDAJ-PR: odmowa galaz main - nigdy nie pushuje na main ani master"
    assert oddanie.nic_nie_wypchniete()


def test_oddanie_z_galezi_master_odmawia_i_nic_nie_wypycha(oddanie):
    _git(oddanie.repo, "checkout", "-q", "-b", "master")
    kod, wyjscie, _ = oddanie.oddaj()
    assert kod == 1
    assert wyjscie.splitlines()[-1] == "ODDAJ-PR: odmowa galaz master - nigdy nie pushuje na main ani master"
    assert oddanie.nic_nie_wypchniete()


def test_oddanie_z_odlaczonym_head_odmawia_i_nic_nie_wypycha(oddanie):
    _git(oddanie.repo, "checkout", "-q", "--detach")
    kod, wyjscie, _ = oddanie.oddaj()
    assert kod == 1
    assert wyjscie.splitlines()[-1] == "ODDAJ-PR: odmowa detached HEAD - nie ma galezi do wypchniecia"
    assert oddanie.nic_nie_wypchniete()


@pytest.mark.parametrize(
    ("argumenty", "domyslne", "fragment"),
    [
        pytest.param(["--tresc", "x"], False, "brak --tytul", id="brak_tytulu"),
        pytest.param(["--tytul", "T"], False, "brak --tresc", id="brak_tresci"),
        pytest.param(["--tytul", "T", "--tresc", "/nie/ma/pliku.md"], False, "nie istnieje albo jest pusty", id="brak_pliku"),
        pytest.param(["--nieznany"], True, "nieznany argument: --nieznany", id="nieznany_argument"),
        pytest.param(["--baza"], True, "--baza wymaga wartosci", id="baza_bez_wartosci"),
        pytest.param(["--tytul"], True, "--tytul wymaga wartosci", id="tytul_bez_wartosci"),
        pytest.param(["--tresc"], True, "--tresc wymaga wartosci", id="tresc_bez_wartosci"),
        pytest.param(["--dozwolone"], True, "--dozwolone wymaga wartosci", id="dozwolone_bez_wartosci"),
    ],
)
def test_oddanie_zle_uzycie_to_blad_kod_2_i_nic_nie_wypycha(oddanie, argumenty, domyslne, fragment):
    kod, wyjscie, bledy = oddanie.oddaj(*argumenty, domyslne=domyslne)
    assert kod == 2
    assert fragment in bledy
    assert "ODDAJ-PR" not in wyjscie
    assert oddanie.nic_nie_wypchniete()


def test_oddanie_z_pustym_plikiem_tresci_to_blad_kod_2(oddanie):
    oddanie.tresc.write_text("", encoding="utf-8")
    kod, _, bledy = oddanie.oddaj()
    assert kod == 2
    assert "nie istnieje albo jest pusty" in bledy
    assert oddanie.nic_nie_wypchniete()


def test_oddanie_wartosc_flagi_z_myslnikiem_na_poczatku_jest_wartoscia_a_nie_flaga(oddanie):
    kod, _, _ = oddanie.oddaj("--tytul", "--HAR-1 proba")
    assert kod == 0
    assert "--title --HAR-1 proba" in oddanie.wolania_gh()[0]


def test_oddanie_nieznana_baza_to_blad_narzedzia_kod_2_a_nie_brak_wspolautora(oddanie):
    kod, _, bledy = oddanie.oddaj("--baza", "origin/nie-ma-takiej")
    assert kod == 2
    assert "git log origin/nie-ma-takiej..HEAD zakonczyl sie bledem" in bledy
    assert oddanie.nic_nie_wypchniete()


@pytest.mark.parametrize(
    ("podkomenda", "fragment"),
    [
        ("log", "git log origin/main..HEAD zakonczyl sie bledem"),
        ("status", "git status zakonczyl sie bledem"),
        ("diff", "git diff origin/main...HEAD zakonczyl sie bledem"),
        ("branch", "git branch --show-current zakonczyl sie bledem"),
    ],
)
def test_oddanie_awaria_gita_przy_sprawdzeniu_to_kod_2_i_nic_nie_wypycha(tmp_path, podkomenda, fragment):
    oddanie = Oddanie(tmp_path, pada_git=podkomenda)
    kod, wyjscie, bledy = oddanie.oddaj()
    assert kod == 2
    assert fragment in bledy
    assert "ODDAJ-PR" not in wyjscie
    assert oddanie.nic_nie_wypchniete()


def test_oddanie_nieudany_push_to_kod_2_i_gh_nie_jest_wolany(tmp_path):
    oddanie = Oddanie(tmp_path, pada_git="push")
    kod, wyjscie, bledy = oddanie.oddaj()
    assert kod == 2
    assert "git push -u origin feat-HAR-1 nie powiodl sie" in bledy
    assert "ODDAJ-PR" not in wyjscie
    assert oddanie.wolania_gh() == []


def test_oddanie_nieudane_gh_pr_create_to_kod_2_po_wypchnieciu_galezi(tmp_path):
    oddanie = Oddanie(tmp_path, pada_gh=True)
    kod, wyjscie, bledy = oddanie.oddaj()
    assert kod == 2
    assert "gh pr create nie powiodl sie (galaz feat-HAR-1 jest juz wypchnieta)" in bledy
    assert "ODDAJ-PR: ok" not in wyjscie
    assert oddanie.wolania_git("push") == ["push -u origin feat-HAR-1"]


def test_oddanie_nigdy_nie_uzywa_force_ani_amend_i_nie_dotyka_commitow(oddanie):
    przed = _git(oddanie.repo, "rev-parse", "HEAD")
    kod, _, _ = oddanie.oddaj()
    assert kod == 0
    dziennik = oddanie.dziennik_git.read_text(encoding="utf-8")
    assert "--force" not in dziennik
    assert "--amend" not in dziennik
    assert "-f " not in dziennik
    assert "commit" not in [wiersz.split(" ")[0] for wiersz in dziennik.splitlines()]
    assert _git(oddanie.repo, "rev-parse", "HEAD") == przed


def test_oddanie_commitu_innego_autora_odmawia_mimo_poprawnego_git_config(oddanie):
    _zapisz(oddanie.repo, "src/obcy.py", "z = 1\n")
    _git(oddanie.repo, "add", "-A")
    _git(oddanie.repo, "commit", "-q", "-m", "obcy", "--author", "Ktos <ktos@firma.example>")
    kod, wyjscie, bledy = oddanie.oddaj()
    assert kod == 1
    assert "commit galezi ma autora 'ktos@firma.example' albo committera 'lukaszbrowar@gmail.com'" in bledy
    assert wyjscie.splitlines()[-1].startswith("ODDAJ-PR: odmowa commit galezi ma autora")
    assert oddanie.nic_nie_wypchniete()


def test_oddanie_commitu_innego_committera_odmawia_mimo_poprawnego_git_config(oddanie):
    _zapisz(oddanie.repo, "src/obcy.py", "z = 1\n")
    _git(oddanie.repo, "add", "-A")
    kod, _, _ = _uruchom(
        ["git", "commit", "-q", "-m", "obcy"], oddanie.repo, {"GIT_COMMITTER_EMAIL": "ktos@firma.example"}, 60.0
    )
    assert kod == 0
    kod, _, bledy = oddanie.oddaj()
    assert kod == 1
    assert "albo committera 'ktos@firma.example'" in bledy
    assert oddanie.nic_nie_wypchniete()


@pytest.mark.parametrize(
    "zmienne",
    [
        pytest.param({"GIT_AUTHOR_NAME": "Inna Nazwa"}, id="nazwa_autora"),
        pytest.param({"GIT_COMMITTER_NAME": "Inna Nazwa"}, id="nazwa_committera"),
    ],
)
def test_oddanie_commitu_z_inna_nazwa_przy_poprawnym_adresie_odmawia(oddanie, zmienne):
    _zapisz(oddanie.repo, "src/obcy.py", "z = 1\n")
    _git(oddanie.repo, "add", "-A")
    kod, _, _ = _uruchom(["git", "commit", "-q", "-m", "obcy"], oddanie.repo, zmienne, 60.0)
    assert kod == 0
    kod, _, bledy = oddanie.oddaj()
    assert kod == 1
    assert "commit galezi ma nazwe autora" in bledy
    assert oddanie.nic_nie_wypchniete()


def test_oddanie_zmiany_na_bazie_po_rozwidleniu_nie_wchodza_do_oceny_galezi(oddanie):
    _git(oddanie.repo, "checkout", "-q", "main")
    _zapisz(oddanie.repo, "pyproject.toml", "zmiana na bazie\n")
    _git(oddanie.repo, "add", "-A")
    _git(oddanie.repo, "commit", "-q", "-m", "baza\n\nCo-Authored-By: Ktos <k@example.com>")
    _git(oddanie.repo, "push", "-q", "origin", "main")
    _git(oddanie.repo, "checkout", "-q", "feat-HAR-1")
    kod, wyjscie, _ = oddanie.oddaj()
    assert kod == 0
    assert "merge reczny" not in wyjscie


def test_oddanie_przeniesiona_sciezka_wrazliwa_jest_wykryta_po_starej_nazwie(oddanie):
    _git(oddanie.repo, "checkout", "-q", "main")
    _zapisz(oddanie.repo, "CLAUDE.md", "instrukcje\n" * 20)
    _commit(oddanie.repo, "claude na bazie")
    _git(oddanie.repo, "push", "-q", "origin", "main")
    _git(oddanie.repo, "checkout", "-q", "feat-HAR-1")
    _git(oddanie.repo, "rebase", "-q", "main")
    _git(oddanie.repo, "mv", "CLAUDE.md", "docs-kopia.md")
    _git(oddanie.repo, "commit", "-q", "-m", "przeniesienie")
    kod, wyjscie, _ = oddanie.oddaj()
    assert kod == 0
    assert "merge reczny: diff dotyka sciezek wrazliwych: CLAUDE.md" in wyjscie.splitlines()


def test_oddanie_galezi_bez_nowych_commitow_nie_odmawia_z_powodu_autorow(tmp_path):
    oddanie = Oddanie(tmp_path)
    _git(oddanie.repo, "reset", "-q", "--hard", "origin/main")
    kod, _, bledy = oddanie.oddaj()
    assert kod == 0, bledy


def test_oddanie_sciezka_wrazliwa_z_nie_ascii_w_nazwie_tez_daje_merge_reczny(oddanie):
    sciezka = "alembic/versions/0099_zażółć.py"
    _zapisz(oddanie.repo, sciezka, "zmiana\n")
    _commit(oddanie.repo, "wrazliwa z polskimi znakami")
    kod, wyjscie, _ = oddanie.oddaj()
    assert kod == 0
    assert f"merge reczny: diff dotyka sciezek wrazliwych: {sciezka}" in wyjscie.splitlines()


def test_oddanie_baza_inna_niz_origin_main_wyznacza_galaz_docelowa_pr(oddanie):
    _git(oddanie.repo, "push", "-q", "origin", "main:rozwoj")
    _git(oddanie.repo, "fetch", "-q", "origin")
    kod, _, _ = oddanie.oddaj("--baza", "origin/rozwoj")
    assert kod == 0
    assert oddanie.wolania_gh()[0].startswith("pr create --base rozwoj --head feat-HAR-1 ")
