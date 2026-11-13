"""Narzedzia na powtarzalne momenty sesji ticketu (HAR-240).

`tools/testy.py` (pytest z limitem bez postepu), `tools/po_merge_main.sh` (moment po merge origin/main),
`tools/liczby_raportu.py` (liczby do raportu z pliku i diffu), `tools/oddaj_pr.sh` (moment oddania).

Testy uruchamiaja PRAWDZIWE skrypty. Pytest, alembic i git pracuja wylacznie w mini-repozytoriach w
`tmp_path`; zaslepki w PATH (`gh` i `git`) sluza tylko do obserwacji, czy skrypt wypchnal galaz i
wolal `gh pr create`. Procesy ida przez `tests/procesy.py`.
"""
import os
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
    assert [w for w in wyjscie.splitlines() if w.startswith("zawieszenie:")] == [
        "zawieszenie: test_przyklad.py::test_wisi"
    ]


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


# ============================ tools/po_merge_main.sh ============================

TABELA_DOBRA = """[[mutant]]
plik = "src/modul.py"
szukaj = "WARTOSC = 1"
zamien = "WARTOSC = 2"
test = "tests/test_ok.py::test_ok"
"""
REWIZJA = 'revision = "{numer}"\ndown_revision = {poprzednia}\n'


def _mini_repo_po_merge(repo: Path) -> None:
    repo.mkdir()
    _init_repo(repo)
    for plik in (PO_MERGE, TESTY_PY, NARZEDZIA / "_testy_zdarzenia.py", NARZEDZIA / "mutanty_reczne.py"):
        _zapisz(repo, f"tools/{plik.name}", plik.read_text(encoding="utf-8"))
    (repo / "tools" / "po_merge_main.sh").chmod(0o755)
    _zapisz(repo, "alembic.ini", "[alembic]\nscript_location = alembic\n")
    _zapisz(repo, "alembic/versions/0001_pierwsza.py", REWIZJA.format(numer="0001", poprzednia="None"))
    _zapisz(repo, "src/modul.py", "WARTOSC = 1\n")
    _zapisz(repo, "tests/test_ok.py", "def test_ok():\n    assert True\n")
    _zapisz(repo, "tests/mutanty_reczne/X.toml", TABELA_DOBRA)
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
    for etap in ("konflikt", "alembic", "mutanty", "testy"):
        assert f"PO-MERGE etap {etap}: ok" in wyjscie


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


def test_po_merge_wiersz_tabeli_nietrafiajacy_w_kod_daje_blad_etapu_mutanty_kod_2(repo_po_merge):
    _zapisz(repo_po_merge, "tests/mutanty_reczne/X.toml", TABELA_DOBRA.replace("WARTOSC = 1", "WARTOSC = 100"))
    _git(repo_po_merge, "add", "-A")
    kod, ostatnia, _, bledy = _po_merge(repo_po_merge)
    assert kod == 2
    assert ostatnia == "PO-MERGE: blad mutanty"
    assert "tests/mutanty_reczne/X.toml #1" in bledy


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
    _zapisz(repo_po_merge, "tests/mutanty_reczne/X.toml", TABELA_DOBRA.replace("WARTOSC = 1", "WARTOSC = 100"))  # kod 2
    _git(repo_po_merge, "add", "-A")
    kod, ostatnia, _, _ = _po_merge(repo_po_merge)
    assert kod == 2
    assert ostatnia == "PO-MERGE: blad mutanty"


def test_po_merge_wszystkie_etapy_wykonuja_sie_mimo_wczesniejszego_bledu(repo_po_merge):
    _zapisz(repo_po_merge, "src/inny.py", f"{ZNACZNIK_START} HEAD\n")
    _git(repo_po_merge, "add", "-A")
    kod, _, wyjscie, bledy = _po_merge(repo_po_merge)
    assert kod == 1
    assert "PO-MERGE etap alembic: ok" in wyjscie
    assert "PO-MERGE etap mutanty: ok" in wyjscie
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

TABELA_TICKETU = """# tabela ticketu
[[mutant]]
plik = "src/x.py"
szukaj = "X = 1"
zamien = "X = 2"
test = "tests/test_a.py::test_stary_jeden"

[[mutant]]
plik = "src/x.py"
szukaj = "X = 1"
zamien = "X = 3"
test = "tests/test_a.py::test_stary_jeden"

[[mutant]]
plik = "src/x.py"
szukaj = "X = 1"
zamien = "X = 4"
test = "tests/test_a.py::test_stary_jeden"

[[rownowazny]]
plik = "src/x.py"
linia_tekst = "X = 1"
uzasadnienie = "zadne wejscie nie odroznia wersji"
"""
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


@pytest.fixture
def repo_liczb(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _zapisz(repo, "tests/test_a.py", TEST_A_PRZED)
    _zapisz(repo, "tests/test_c.py", "def test_c():\n    pass\n")
    _zapisz(repo, "tests/mutanty_reczne/HAR-2.toml", "# cudza tabela\n")
    _zapisz(repo, "tests/mutanty_reczne/HAR-3.toml", "# druga cudza tabela\n")
    _zapisz(repo, "src/x.py", "X = 1\n")
    _commit(repo, "main")
    _git(repo, "checkout", "-q", "-b", "feat-HAR-7")
    _zapisz(repo, "tests/test_a.py", TEST_A_PO)  # zmiana skomitowana
    _git(repo, "rm", "-q", "tests/test_c.py")
    _commit(repo, "zmiany na galezi")
    _zapisz(repo, "tests/test_b.py", TEST_B)  # nowe pliki i zmiany niezacommitowane
    _zapisz(repo, "tests/mutanty_reczne/HAR-7.toml", TABELA_TICKETU)
    _zapisz(repo, "tests/mutanty_reczne/HAR-2.toml", "# cudza tabela zmieniona\n")
    (repo / "tests/mutanty_reczne/HAR-3.toml").unlink()
    _zapisz(repo, "tests/mutanty_reczne/NOTATKA.md", "nie tabela\n")
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


def test_liczby_raportu_podaja_wiersze_tabeli_ticketu_dokladnie_3_mutanty_i_1_rownowazny(repo_liczb):
    kod, wyjscie, _ = _liczby(repo_liczb)
    assert kod == 0
    linie = wyjscie.splitlines()
    assert linie[0] == "## Liczby z pliku i diffu (HAR-7, baza main)"
    assert "- wiersze `[[mutant]]` w `tests/mutanty_reczne/HAR-7.toml`: 3" in linie
    assert "- wiersze `[[rownowazny]]` w tej tabeli: 1" in linie


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


def test_liczby_raportu_tabele_innych_ticketow_maja_dopisek_wymaga_stop_a_wlasna_nie(repo_liczb):
    kod, wyjscie, _ = _liczby(repo_liczb)
    assert kod == 0
    assert "**Zmienione tabele innych ticketow:** 2" in wyjscie.splitlines()
    assert _sekcja(wyjscie, "Zmienione tabele innych ticketow") == [
        "- `tests/mutanty_reczne/HAR-2.toml` - wymaga STOP:",
        "- `tests/mutanty_reczne/HAR-3.toml` - wymaga STOP:",
    ]


def test_liczby_raportu_bez_listy_plikow_nie_wypisuja_sekcji_spoza_przydzialu(repo_liczb):
    kod, wyjscie, _ = _liczby(repo_liczb)
    assert kod == 0
    assert "spoza listy" not in wyjscie


def test_liczby_raportu_pliki_spoza_listy_pliki_dotykane_maja_dopisek_wymaga_stop(repo_liczb):
    kod, wyjscie, _ = _liczby(
        repo_liczb, "--pliki", "tests/test_a.py,tests/test_b.py src/ docs/*.md tests/mutanty_reczne/HAR-7.toml"
    )
    assert kod == 0
    assert "**Zmienione pliki spoza listy Pliki dotykane:** 5" in wyjscie.splitlines()
    assert _sekcja(wyjscie, "Zmienione pliki spoza listy Pliki dotykane") == [
        "- `inne/HAR-9.toml` - wymaga STOP:",
        "- `tests/mutanty_reczne/HAR-2.toml` - wymaga STOP:",
        "- `tests/mutanty_reczne/HAR-3.toml` - wymaga STOP:",
        "- `tests/mutanty_reczne/NOTATKA.md` - wymaga STOP:",
        "- `tests/test_c.py` - wymaga STOP:",
    ]


def test_liczby_raportu_wlasna_tabela_ticketu_jest_dozwolona_bez_wpisu_na_liscie(repo_liczb):
    _, wyjscie, _ = _liczby(repo_liczb, "--pliki", "tests/test_a.py")
    assert "tests/mutanty_reczne/HAR-7.toml` - wymaga STOP:" not in wyjscie


def test_liczby_raportu_biora_ticket_z_nazwy_galezi_a_jawny_argument_go_nadpisuje(repo_liczb):
    _zapisz(repo_liczb, "tests/mutanty_reczne/HAR-8.toml", "[[mutant]]\nplik = 'a'\n")
    kod, wyjscie, _ = _liczby(repo_liczb, "--ticket", "HAR-8")
    assert kod == 0
    assert "- wiersze `[[mutant]]` w `tests/mutanty_reczne/HAR-8.toml`: 1" in wyjscie.splitlines()
    assert "- wiersze `[[rownowazny]]` w tej tabeli: 0" in wyjscie.splitlines()


def test_liczby_raportu_nieznana_baza_to_blad_kod_2_bez_liczb(repo_liczb):
    kod, wyjscie, bledy = _uruchom(
        [sys.executable, str(LICZBY), "--korzen", str(repo_liczb), "--baza", "nie-ma-takiego-refa"], KORZEN
    )
    assert kod == 2
    assert wyjscie == ""
    assert "BLAD: git merge-base nie-ma-takiego-refa HEAD zakonczyl sie kodem" in bledy


def test_liczby_raportu_brak_tabeli_ticketu_to_blad_kod_2_a_nie_zero_wierszy(repo_liczb):
    kod, wyjscie, bledy = _liczby(repo_liczb, "--ticket", "HAR-99")
    assert kod == 2
    assert wyjscie == ""
    assert "brak albo nieczytelna tabela tests/mutanty_reczne/HAR-99.toml" in bledy


def test_liczby_raportu_zepsuta_tabela_to_blad_kod_2(repo_liczb):
    _zapisz(repo_liczb, "tests/mutanty_reczne/HAR-7.toml", "[[mutant\n")
    kod, _, bledy = _liczby(repo_liczb)
    assert kod == 2
    assert "brak albo nieczytelna tabela tests/mutanty_reczne/HAR-7.toml" in bledy


def test_liczby_raportu_tabela_w_ktorej_mutant_nie_jest_lista_to_blad_kod_2(repo_liczb):
    _zapisz(repo_liczb, "tests/mutanty_reczne/HAR-7.toml", "[mutant]\nplik = 'a'\n")
    kod, _, bledy = _liczby(repo_liczb)
    assert kod == 2
    assert "HAR-7.toml: [mutant] musi byc lista tabel" in bledy


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


def test_liczby_raportu_tabela_ticketu_w_zlym_kodowaniu_to_blad_tabeli_a_nie_ogolny_blad_pliku(repo_liczb):
    (repo_liczb / "tests" / "mutanty_reczne" / "HAR-7.toml").write_bytes(b"\xff\xfe\x00")
    kod, _, bledy = _liczby(repo_liczb)
    assert kod == 2
    assert "brak albo nieczytelna tabela tests/mutanty_reczne/HAR-7.toml" in bledy


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
    _zapisz(repo, "tests/mutanty_reczne/HAR-7.toml", TABELA_TICKETU)
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
