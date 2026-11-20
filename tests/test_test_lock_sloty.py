"""Semafor test_lock.py --sloty (audyt procesu 2026-11-19, krok 7): najwyzej N ciezkich komend naraz."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from procesy import uruchom_i_zbierz, uruchom_proces_sync, zakoncz_proces_sync

KORZEN = Path(__file__).resolve().parents[1]
TEST_LOCK = KORZEN / ".claude" / "skills" / "praca-ticketu" / "scripts" / "test_lock.py"
# komenda trzymajaca slot: zaklada plik startu i czeka na plik konca
TRZYMAJ = ("import pathlib, sys, time\n"
           "pathlib.Path(sys.argv[1]).touch()\n"
           "while not pathlib.Path(sys.argv[2]).exists():\n"
           "    time.sleep(0.05)\n")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    uruchom_i_zbierz(["git", "init", "-q"], cwd=r, check=True, timeout=30)
    return r


def _czekaj_na(sciezka: Path, limit: float = 30.0) -> None:
    koniec = time.monotonic() + limit
    while not sciezka.exists():
        assert time.monotonic() < koniec, f"brak {sciezka} po {limit} s"
        time.sleep(0.05)


def _trzymaj(repo: Path, nazwa: str, *opcje: str) -> tuple[subprocess.Popen, Path]:
    start, stop = repo.parent / f"{nazwa}.start", repo.parent / f"{nazwa}.stop"
    proc = uruchom_proces_sync([sys.executable, str(TEST_LOCK), *opcje, "--", sys.executable, "-c", TRZYMAJ,
                                str(start), str(stop)], cwd=repo, stderr=subprocess.PIPE, text=True)
    _czekaj_na(start)
    return proc, stop


def _zwolnij(proc: subprocess.Popen, stop: Path) -> str:
    stop.touch()
    try:
        _, err = proc.communicate(timeout=30)
    finally:
        zakoncz_proces_sync(proc, nazwa="trzymajacy slot")
    assert proc.returncode == 0
    return err


def _lock(repo: Path, *argv: str, limit: float = 60.0) -> subprocess.CompletedProcess:
    return uruchom_i_zbierz([sys.executable, str(TEST_LOCK), *argv], cwd=repo, capture_output=True, text=True,
                            timeout=limit)


def test_dwa_przebiegi_dostaja_rozne_sloty_a_trzeci_czeka(repo):
    a, stop_a = _trzymaj(repo, "a", "--sloty", "2")
    b, stop_b = _trzymaj(repo, "b", "--sloty", "2")
    try:
        trzeci = _lock(repo, "--sloty", "2", "--timeout", "1", "--", "true")
        assert trzeci.returncode == 75
        czekanie = [w for w in trzeci.stderr.splitlines() if w.startswith("TEST-LOCK: waiting holder=")]
        assert len(czekanie) == 1  # raz, nie przy kazdej probie co 0,5 s
        holderzy = czekanie[0].removeprefix("TEST-LOCK: waiting holder=").split(",")
        # kazdy slot ma wpis swojego holdera (pid/katalog), nie "unknown"
        assert sorted(holderzy) == sorted(f"{p.pid}/repo" for p in (a, b))
        assert trzeci.stderr.strip().splitlines()[-1] == "TEST-LOCK: timeout"
    finally:
        err_a, err_b = _zwolnij(a, stop_a), _zwolnij(b, stop_b)
    assert {err_a.splitlines()[0], err_b.splitlines()[0]} == {"TEST-LOCK: acquired slot=0",
                                                              "TEST-LOCK: acquired slot=1"}


def test_oczekujacy_dostaje_slot_po_zwolnieniu(repo):
    a, stop_a = _trzymaj(repo, "a", "--sloty", "2")
    b, stop_b = _trzymaj(repo, "b", "--sloty", "2")
    c = uruchom_proces_sync([sys.executable, str(TEST_LOCK), "--sloty", "2", "--timeout", "60", "--", "true"],
                            cwd=repo, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(1.0)  # trzeci wisi na zajetych slotach
        assert c.poll() is None
        err_a = _zwolnij(a, stop_a)
        _, err_c = c.communicate(timeout=30)
    finally:
        zakoncz_proces_sync(c, nazwa="oczekujacy")
        _zwolnij(b, stop_b)
    assert c.returncode == 0
    assert err_c.strip().splitlines()[-1] == err_a.splitlines()[0]  # dostal zwolniony slot


def test_timeout_zero_przy_zajetych_slotach_nie_czeka_i_nie_uruchamia_komendy(repo):
    a, stop_a = _trzymaj(repo, "a", "--sloty", "2")
    b, stop_b = _trzymaj(repo, "b", "--sloty", "2")
    znacznik = repo.parent / "uruchomiona"
    try:
        r = _lock(repo, "--sloty", "2", "--timeout", "0", "--", "touch", str(znacznik))
    finally:
        _zwolnij(a, stop_a)
        _zwolnij(b, stop_b)
    assert r.returncode == 75
    assert r.stderr.strip().splitlines() == ["TEST-LOCK: timeout"]
    assert not znacznik.exists()


def test_jeden_slot_to_dotychczasowa_blokada_na_pliku_test_lock(repo):
    r = _lock(repo, "--", "true")
    assert r.returncode == 0
    assert r.stderr.strip().splitlines() == ["TEST-LOCK: acquired"]
    katalog = repo / ".git" / "monolynx"
    assert sorted(p.name for p in katalog.iterdir()) == ["test.lock"]


def test_jawny_jeden_slot_blokuje_drugi_przebieg(repo):
    a, stop_a = _trzymaj(repo, "a", "--sloty", "1")
    try:
        r = _lock(repo, "--sloty", "1", "--timeout", "0", "--", "true")
    finally:
        _zwolnij(a, stop_a)
    assert r.returncode == 75


def test_sloty_to_osobne_pliki_blokady(repo):
    r = _lock(repo, "--sloty", "3", "--", "true")
    assert r.returncode == 0
    katalog = repo / ".git" / "monolynx"
    assert sorted(p.name for p in katalog.iterdir()) == ["test.lock.0", "test.lock.1", "test.lock.2"]


def test_kod_komendy_przechodzi_przez_semafor(repo):
    assert _lock(repo, "--sloty", "2", "--", sys.executable, "-c", "raise SystemExit(7)").returncode == 7


@pytest.mark.parametrize("wartosc", ["0", "65", "-1", "x", "1.5"])
def test_liczba_slotow_poza_zakresem_to_blad_uzycia(repo, wartosc):
    r = _lock(repo, "--sloty", wartosc, "--", "true")
    assert r.returncode == 64
    assert r.stderr.startswith("test_lock: --sloty")
    assert not (repo / ".git" / "monolynx").exists()


def test_najwieksza_liczba_slotow_jest_dozwolona(repo):
    assert _lock(repo, "--sloty", "64", "--", "true").returncode == 0


def test_sloty_bez_wartosci_to_blad_uzycia(repo):
    r = _lock(repo, "--sloty", "--", "true")
    assert r.returncode == 64


def test_timeout_nadal_ograniczony_do_doby_takze_ze_slotami(repo):
    r = _lock(repo, "--sloty", "2", "--timeout", "86401", "--", "true")
    assert r.returncode == 64
    assert "86400" in r.stderr
