"""HAR-142: wspolny helper procesow w testach (`tests/procesy.py`).

Retro M1.3 (HAR-35): sprzatanie `killpg(getpgid(pid))` pod mutantem bez `start_new_session`
zabijalo grupe pytesta/mutmuta/agenta CI. Helper zabija wylacznie grupe, ktora sam
utworzyl, a kazde oczekiwanie ma twardy limit. Testy NIGDY nie wysylaja prawdziwego
sygnalu do grupy testu: tam, gdzie badamy gałąź "wlasna grupa", `os.killpg` jest
podmieniony na rejestrator.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

import pytest

import procesy
from procesy import (
    LimitCzasuTestu,
    WlasnaGrupaProcesow,
    czekaj,
    czekaj_bez_anulowania,
    czekaj_na_warunek,
    czy_zyje,
    grupa_istnieje,
    grupa_umarla,
    przerwij_sigtermem_sync,
    sprzatnij_zapamietane,
    uruchom_proces,
    uruchom_proces_sync,
    zabij_grupe,
    zabij_proces,
    zadanie_tla,
    zakoncz_proces,
    zakoncz_proces_sync,
    zapamietaj_proces,
)

# Krok w Pythonie: handler SIG_IGN, DOPIERO POTEM znacznik gotowosci (trap w sh bywa
# niestabilny), wiec SIGTERM wyslany po znaczniku nigdy nie trafi w domyslny handler.
KROK_IGNORUJACY_SIGTERM = (
    "import signal, sys, time, pathlib\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "pathlib.Path(sys.argv[1]).write_text('gotowy')\n"
    "time.sleep(60)\n"
)
KROK_REAGUJACY_NA_SIGTERM = (
    "import sys, time, pathlib\n"
    "pathlib.Path(sys.argv[1]).write_text('gotowy')\n"
    "time.sleep(60)\n"
)
# Lider konczy sie na SIGTERM, ale jego potomek (ta sama grupa) go ignoruje.
KROK_Z_WNUKIEM_IGNORUJACYM_SIGTERM = (
    "import subprocess, sys, time, pathlib\n"
    f"subprocess.Popen([sys.executable, '-c', {KROK_IGNORUJACY_SIGTERM!r}, sys.argv[1] + '.wnuk'])\n"
    "while not pathlib.Path(sys.argv[1] + '.wnuk').exists():\n"
    "    time.sleep(0.01)\n"
    "pathlib.Path(sys.argv[1]).write_text('gotowy')\n"
    "time.sleep(60)\n"
)
KROK_KONCZACY_SIE_OD_RAZU = "pass"


class RejestratorKillpg:
    """Podmienia `os.killpg`: zapisuje wywolania, opcjonalnie przepuszcza je naprawde."""

    def __init__(self, monkeypatch, *, przepusc: bool):
        self.wywolania: list[tuple[int, int]] = []
        prawdziwy = os.killpg

        def killpg(pgid, sygnal):
            self.wywolania.append((pgid, sygnal))
            if przepusc:
                prawdziwy(pgid, sygnal)

        monkeypatch.setattr(procesy.os, "killpg", killpg)


@pytest.fixture
def killpg_zapisany(monkeypatch):
    return RejestratorKillpg(monkeypatch, przepusc=False)


@pytest.fixture
def killpg_przepuszczony(monkeypatch):
    return RejestratorKillpg(monkeypatch, przepusc=True)


async def _gotowy(plik: Path) -> None:
    await czekaj_na_warunek(plik.exists, 10, f"znacznik gotowosci {plik.name}")


def _uzyj_wolnego_procesu_bez_nowej_sesji() -> subprocess.Popen:
    """Proces w grupie testu: celowo BEZ helpera uruchamiajacego."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def _dobij(proc: subprocess.Popen) -> None:
    proc.kill()
    proc.wait(10)


# --- wlasna grupa testu nie jest zabijana -------------------------------------


def test_zabicie_grupy_testu_jest_odmowione_i_nie_wysyla_zadnego_sygnalu(killpg_zapisany):
    with pytest.raises(WlasnaGrupaProcesow):
        zabij_grupe(os.getpgid(0))

    assert killpg_zapisany.wywolania == []


def test_zabicie_grupy_testu_jest_odmowione_takze_przy_sygnale_sprawdzajacym(killpg_zapisany):
    with pytest.raises(WlasnaGrupaProcesow):
        zabij_grupe(os.getpgid(0), signal.SIGTERM)

    assert killpg_zapisany.wywolania == []


async def test_zakonczenie_procesu_z_grupy_testu_nie_wysyla_sygnalu_do_grupy_ale_dobija_sam_proces(
    killpg_zapisany,
):
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)"
    )
    try:
        with pytest.raises(WlasnaGrupaProcesow, match="atrapa-bez-sesji"):
            await zakoncz_proces(proc, nazwa="atrapa-bez-sesji")

        assert killpg_zapisany.wywolania == []
        await asyncio.wait_for(proc.wait(), 10)
        assert proc.returncode == -signal.SIGKILL
    finally:
        if proc.returncode is None:
            proc.kill()
            await asyncio.wait_for(proc.wait(), 10)


def test_zakonczenie_procesu_z_grupy_testu_w_wariancie_synchronicznym_tez_omija_killpg(
    killpg_zapisany,
):
    proc = _uzyj_wolnego_procesu_bez_nowej_sesji()
    try:
        with pytest.raises(WlasnaGrupaProcesow, match="atrapa-sync"):
            zakoncz_proces_sync(proc, nazwa="atrapa-sync")

        assert killpg_zapisany.wywolania == []
        assert proc.wait(10) == -signal.SIGKILL
    finally:
        if proc.poll() is None:
            _dobij(proc)


async def test_zakonczenie_procesu_z_obcej_grupy_wysyla_sygnal_do_jego_grupy_a_nie_tylko_do_pid(
    killpg_przepuszczony, tmp_path
):
    znacznik = tmp_path / "gotowy"
    proc = await uruchom_proces(sys.executable, "-c", KROK_REAGUJACY_NA_SIGTERM, str(znacznik))
    try:
        await _gotowy(znacznik)
        await zakoncz_proces(proc, nazwa="atrapa-w-nowej-sesji")

        assert (proc.pid, signal.SIGTERM) in killpg_przepuszczony.wywolania
        assert proc.returncode is not None
    finally:
        await zakoncz_proces(proc, nazwa="sprzatanie-atrapy")


async def test_zabicie_obcej_grupy_zabija_proces_ktory_w_niej_zyje(tmp_path):
    znacznik = tmp_path / "gotowy"
    proc = await uruchom_proces(sys.executable, "-c", KROK_REAGUJACY_NA_SIGTERM, str(znacznik))
    try:
        await _gotowy(znacznik)
        zabij_grupe(proc.pid)

        await czekaj(proc.wait(), 10, "koniec zabitego procesu")
        assert proc.returncode == -signal.SIGKILL
    finally:
        await zakoncz_proces(proc, nazwa="sprzatanie-atrapy")


# --- SIGTERM, potem SIGKILL -----------------------------------------------------


async def test_proces_ignorujacy_sigterm_konczy_sie_sigkillem_po_limicie_na_sigterm(tmp_path):
    znacznik = tmp_path / "gotowy"
    proc = await uruchom_proces(sys.executable, "-c", KROK_IGNORUJACY_SIGTERM, str(znacznik))
    try:
        await _gotowy(znacznik)
        await zakoncz_proces(proc, nazwa="uparty-krok", limit_term=0.3)

        assert proc.returncode == -signal.SIGKILL
    finally:
        await zakoncz_proces(proc, nazwa="sprzatanie-atrapy")


async def test_proces_reagujacy_na_sigterm_konczy_sie_sigtermem_bez_eskalacji_do_sigkill(
    tmp_path,
):
    znacznik = tmp_path / "gotowy"
    proc = await uruchom_proces(sys.executable, "-c", KROK_REAGUJACY_NA_SIGTERM, str(znacznik))
    try:
        await _gotowy(znacznik)
        await zakoncz_proces(proc, nazwa="grzeczny-krok", limit_term=5)

        assert proc.returncode == -signal.SIGTERM
    finally:
        await zakoncz_proces(proc, nazwa="sprzatanie-atrapy")


async def test_zakonczenie_procesu_zabija_tez_potomka_ktory_ignoruje_sigterm_i_przezyl_lidera(
    tmp_path,
):
    znacznik = tmp_path / "gotowy"
    proc = await uruchom_proces(
        sys.executable, "-c", KROK_Z_WNUKIEM_IGNORUJACYM_SIGTERM, str(znacznik)
    )
    pgid = proc.pid
    try:
        await _gotowy(znacznik)
        await zakoncz_proces(proc, nazwa="lider-z-wnukiem", limit_term=5)

        assert proc.returncode == -signal.SIGTERM   # lider padl od SIGTERM, wiec nie to zabilo wnuka
        assert await grupa_umarla(pgid, 5)
    finally:
        zabij_grupe(pgid)


async def test_po_zabiciu_sigkillem_proces_ktory_nie_konczy_sie_w_limicie_daje_blad_z_nazwa(
    monkeypatch, tmp_path
):
    znacznik = tmp_path / "gotowy"
    proc = await uruchom_proces(sys.executable, "-c", KROK_REAGUJACY_NA_SIGTERM, str(znacznik))
    try:
        await _gotowy(znacznik)
        monkeypatch.setattr(procesy, "zabij_grupe", lambda pgid, sygnal=signal.SIGKILL: None)

        with pytest.raises(LimitCzasuTestu, match="zawieszony-krok"):
            await zakoncz_proces(proc, nazwa="zawieszony-krok", limit_term=0.1, limit_wait=0.2)
    finally:
        monkeypatch.undo()
        await zakoncz_proces(proc, nazwa="sprzatanie-atrapy")


async def test_zakonczony_i_odebrany_proces_nie_dotyka_grup_procesow(monkeypatch):
    proc = await uruchom_proces(sys.executable, "-c", KROK_KONCZACY_SIE_OD_RAZU)
    await czekaj(proc.wait(), 10, "koniec krotkiego procesu")
    wywolania: list[str] = []
    monkeypatch.setattr(procesy.os, "killpg", lambda *a: wywolania.append("killpg"))
    monkeypatch.setattr(procesy.os, "getpgid", lambda *a: wywolania.append("getpgid") or 0)

    await zakoncz_proces(proc, nazwa="juz-zakonczony")

    assert wywolania == []


class AtrapaProcesuBezGrupy:
    """Proces, ktory juz zniknal z systemu, ale obiekt jeszcze nie ma kodu wyjscia
    (zombie): `getpgid` rzuca `ProcessLookupError`."""

    def __init__(self, pid: int):
        self.pid = pid
        self.returncode = None

    async def wait(self):
        self.returncode = 0

    def poll(self):
        return self.returncode


def _pid_nieistniejacego_procesu() -> int:
    proc = subprocess.Popen([sys.executable, "-c", KROK_KONCZACY_SIE_OD_RAZU])
    proc.wait(10)
    return proc.pid


async def test_proces_ktorego_grupy_nie_da_sie_ustalic_jest_tylko_odbierany_bez_sygnalow(
    killpg_zapisany,
):
    atrapa = AtrapaProcesuBezGrupy(_pid_nieistniejacego_procesu())

    await zakoncz_proces(atrapa, nazwa="zombie")

    assert atrapa.returncode == 0
    assert killpg_zapisany.wywolania == []


def test_proces_ignorujacy_sigterm_w_wariancie_synchronicznym_konczy_sie_sigkillem(tmp_path):
    znacznik = tmp_path / "gotowy"
    proc = uruchom_proces_sync([sys.executable, "-c", KROK_IGNORUJACY_SIGTERM, str(znacznik)])
    try:
        _czekaj_synchronicznie_na_plik(znacznik)
        zakoncz_proces_sync(proc, nazwa="uparty-krok-sync", limit_term=0.3)

        assert proc.returncode == -signal.SIGKILL
    finally:
        zakoncz_proces_sync(proc, nazwa="sprzatanie-atrapy")


def test_proces_reagujacy_na_sigterm_w_wariancie_synchronicznym_nie_dostaje_sigkill(tmp_path):
    znacznik = tmp_path / "gotowy"
    proc = uruchom_proces_sync([sys.executable, "-c", KROK_REAGUJACY_NA_SIGTERM, str(znacznik)])
    try:
        _czekaj_synchronicznie_na_plik(znacznik)
        zakoncz_proces_sync(proc, nazwa="grzeczny-krok-sync", limit_term=5)

        assert proc.returncode == -signal.SIGTERM
    finally:
        zakoncz_proces_sync(proc, nazwa="sprzatanie-atrapy")


def test_wariant_synchroniczny_zabija_potomka_ktory_przezyl_lidera(tmp_path):
    znacznik = tmp_path / "gotowy"
    proc = uruchom_proces_sync(
        [sys.executable, "-c", KROK_Z_WNUKIEM_IGNORUJACYM_SIGTERM, str(znacznik)]
    )
    pgid = proc.pid
    try:
        _czekaj_synchronicznie_na_plik(znacznik)
        zakoncz_proces_sync(proc, nazwa="lider-z-wnukiem-sync", limit_term=5)

        assert proc.returncode == -signal.SIGTERM
        assert asyncio.run(grupa_umarla(pgid, 5))
    finally:
        zabij_grupe(pgid)


def test_wariant_synchroniczny_po_sigkillu_bez_konca_procesu_daje_blad_z_nazwa(
    monkeypatch, tmp_path
):
    znacznik = tmp_path / "gotowy"
    proc = uruchom_proces_sync([sys.executable, "-c", KROK_REAGUJACY_NA_SIGTERM, str(znacznik)])
    try:
        _czekaj_synchronicznie_na_plik(znacznik)
        monkeypatch.setattr(procesy, "zabij_grupe", lambda pgid, sygnal=signal.SIGKILL: None)

        with pytest.raises(LimitCzasuTestu, match="zawieszony-krok-sync"):
            zakoncz_proces_sync(proc, nazwa="zawieszony-krok-sync", limit_term=0.1, limit_wait=0.2)
    finally:
        monkeypatch.undo()
        zakoncz_proces_sync(proc, nazwa="sprzatanie-atrapy")


def test_zakonczony_proces_w_wariancie_synchronicznym_nie_dotyka_grup_procesow(monkeypatch):
    proc = uruchom_proces_sync([sys.executable, "-c", KROK_KONCZACY_SIE_OD_RAZU])
    proc.wait(10)
    wywolania: list[str] = []
    monkeypatch.setattr(procesy.os, "killpg", lambda *a: wywolania.append("killpg"))
    monkeypatch.setattr(procesy.os, "getpgid", lambda *a: wywolania.append("getpgid") or 0)

    zakoncz_proces_sync(proc, nazwa="juz-zakonczony-sync")

    assert wywolania == []


def test_wariant_synchroniczny_bez_ustalonej_grupy_tylko_odbiera_proces(killpg_zapisany):
    class Zombie(AtrapaProcesuBezGrupy):
        def wait(self, timeout=None):
            self.returncode = 0

    atrapa = Zombie(_pid_nieistniejacego_procesu())

    zakoncz_proces_sync(atrapa, nazwa="zombie-sync")

    assert atrapa.returncode == 0
    assert killpg_zapisany.wywolania == []


def _czekaj_synchronicznie_na_plik(plik: Path) -> None:
    asyncio.run(czekaj_na_warunek(plik.exists, 10, f"znacznik gotowosci {plik.name}"))


# --- twarde limity oczekiwania --------------------------------------------------


async def test_przekroczenie_limitu_czekania_daje_blad_z_nazwa_operacji_i_limitem():
    with pytest.raises(LimitCzasuTestu) as blad:
        await czekaj(asyncio.Event().wait(), 0.2, "odpowiedz kokpitu")

    assert str(blad.value) == "odpowiedz kokpitu: przekroczono limit 0.2 s"


async def test_czekanie_zwraca_wynik_operacji_ktora_zdazyla_w_limicie():
    async def odpowiedz():
        await asyncio.sleep(0.01)
        return 42

    assert await czekaj(odpowiedz(), 5, "szybka operacja") == 42


async def test_czekanie_przepuszcza_wyjatek_operacji_bez_podmiany_na_blad_limitu():
    async def pada():
        raise ValueError("blad operacji")

    with pytest.raises(ValueError, match="blad operacji"):
        await czekaj(pada(), 5, "operacja z bledem")


async def test_czekanie_bez_anulowania_po_limicie_daje_blad_z_nazwa_i_zostawia_zadanie_zywe():
    zwolnienie = asyncio.Event()
    zadanie = asyncio.create_task(zwolnienie.wait())
    try:
        with pytest.raises(LimitCzasuTestu) as blad:
            await czekaj_bez_anulowania(zadanie, 0.2, "koniec nasluchu")

        assert str(blad.value) == "koniec nasluchu: przekroczono limit 0.2 s"
        assert not zadanie.done()
        assert not zadanie.cancelled()
    finally:
        zwolnienie.set()
        await czekaj_bez_anulowania(zadanie, 5, "sprzatanie zadania")


async def test_czekanie_bez_anulowania_zwraca_wynik_zadania_ktore_zdazylo():
    async def praca():
        await asyncio.sleep(0.01)
        return "gotowe"

    zadanie = asyncio.create_task(praca())

    assert await czekaj_bez_anulowania(zadanie, 5, "szybkie zadanie") == "gotowe"


async def test_czekanie_bez_anulowania_przepuszcza_wyjatek_zadania():
    async def pada():
        raise ValueError("blad zadania")

    zadanie = asyncio.create_task(pada())

    with pytest.raises(ValueError, match="blad zadania"):
        await czekaj_bez_anulowania(zadanie, 5, "zadanie z bledem")


async def test_czekanie_na_warunek_konczy_sie_gdy_warunek_sie_spelni():
    stan = {"licznik": 0}

    def warunek():
        stan["licznik"] += 1
        return stan["licznik"] >= 3

    await czekaj_na_warunek(warunek, 5, "trzecie sprawdzenie")

    assert stan["licznik"] == 3


async def test_czekanie_na_warunek_ktory_sie_nie_spelni_daje_blad_z_nazwa_i_limitem():
    with pytest.raises(LimitCzasuTestu) as blad:
        await czekaj_na_warunek(lambda: False, 0.1, "plik pid")

    assert str(blad.value) == "plik pid: przekroczono limit 0.1 s"


# --- zadania tla ----------------------------------------------------------------


async def test_zadanie_tla_jest_anulowane_i_zakonczone_przy_wyjsciu_z_bloku():
    async with zadanie_tla(asyncio.Event().wait(), "nasluch") as zadanie:
        assert not zadanie.done()

    assert zadanie.cancelled()


async def test_zadanie_tla_ktore_polyka_anulowanie_i_nie_konczy_sie_daje_blad_z_nazwa():
    zwolnienie = asyncio.Event()

    async def uparte():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await zwolnienie.wait()

    zadanie = None
    try:
        with pytest.raises(LimitCzasuTestu, match="uparty nasluch"):
            async with zadanie_tla(uparte(), "uparty nasluch", limit_konca=0.2) as zadanie:
                await asyncio.sleep(0)
    finally:
        zwolnienie.set()
        if zadanie is not None:
            await czekaj_bez_anulowania(zadanie, 5, "sprzatanie zadania")


async def test_zadanie_tla_nie_maskuje_wyjatku_z_bloku():
    with pytest.raises(KeyError, match="z-bloku"):
        async with zadanie_tla(asyncio.Event().wait(), "nasluch"):
            raise KeyError("z-bloku")


# --- sondy i brak procesu -------------------------------------------------------


async def test_sondy_odroznia_proces_zywy_od_zakonczonego_i_grupe_istniejaca_od_nieistniejacej():
    proc = await uruchom_proces(sys.executable, "-c", "import time; time.sleep(60)")
    try:
        assert czy_zyje(proc.pid)
        assert grupa_istnieje(proc.pid)
    finally:
        await zakoncz_proces(proc, nazwa="sprzatanie-atrapy")

    assert not czy_zyje(proc.pid)
    assert not grupa_istnieje(proc.pid)


def test_proces_i_grupa_cudze_ale_istniejace_sa_uznane_za_zywe(monkeypatch):
    def brak_uprawnien(*argumenty):
        raise PermissionError

    monkeypatch.setattr(procesy.os, "kill", brak_uprawnien)
    monkeypatch.setattr(procesy.os, "killpg", brak_uprawnien)

    assert czy_zyje(1)
    assert grupa_istnieje(1)


async def test_grupa_ktora_nie_umiera_w_limicie_nie_jest_uznana_za_martwa():
    proc = await uruchom_proces(sys.executable, "-c", "import time; time.sleep(60)")
    try:
        assert not await grupa_umarla(proc.pid, 0.1)
    finally:
        await zakoncz_proces(proc, nazwa="sprzatanie-atrapy")

    assert await grupa_umarla(proc.pid, 5)


async def test_zabicie_nieistniejacej_grupy_i_nieistniejacego_procesu_nie_rzuca_wyjatku():
    proc = await uruchom_proces(sys.executable, "-c", KROK_KONCZACY_SIE_OD_RAZU)
    await czekaj(proc.wait(), 10, "koniec krotkiego procesu")

    zabij_grupe(proc.pid)
    zabij_proces(proc.pid)


# --- rejestr procesow testu -----------------------------------------------------


async def test_sprzatanie_rejestru_zabija_grupe_procesu_uruchomionego_w_tescie(tmp_path):
    znacznik = tmp_path / "gotowy"
    proc = await uruchom_proces(sys.executable, "-c", KROK_IGNORUJACY_SIGTERM, str(znacznik))
    await _gotowy(znacznik)

    sprzatnij_zapamietane()

    await czekaj(proc.wait(), 10, "koniec zabitego procesu")
    assert proc.returncode == -signal.SIGKILL


async def test_sprzatanie_rejestru_pomija_proces_ktorego_numer_przejal_inny_proces(
    killpg_zapisany,
):
    """Anty-recykling: pid zapamietany z inna grupa niz obecna to juz nie nasz proces."""
    proc = await uruchom_proces(sys.executable, "-c", "import time; time.sleep(60)")
    try:
        procesy._PROCESY[proc.pid] = proc.pid + 1      # rzekoma, dawna grupa tego pid

        sprzatnij_zapamietane()

        assert killpg_zapisany.wywolania == []
        assert czy_zyje(proc.pid)
    finally:
        procesy._PROCESY.pop(proc.pid, None)
        # killpg jest tu podmieniony na rejestrator, wiec grupa nie zginie; dobijamy sam pid.
        zabij_proces(proc.pid)
        await czekaj(proc.wait(), 10, "koniec atrapy")


async def test_sprzatanie_rejestru_wysyla_sigkill_do_grupy_zgodnej_z_zapamietana(
    killpg_przepuszczony,
):
    proc = await uruchom_proces(sys.executable, "-c", "import time; time.sleep(60)")
    try:
        sprzatnij_zapamietane()

        assert killpg_przepuszczony.wywolania == [(proc.pid, signal.SIGKILL)]
        await czekaj(proc.wait(), 10, "koniec zabitego procesu")
    finally:
        await zakoncz_proces(proc, nazwa="sprzatanie-atrapy")


def test_sprzatanie_rejestru_dla_procesu_z_grupy_testu_dobija_tylko_pid_bez_killpg(
    killpg_zapisany,
):
    proc = _uzyj_wolnego_procesu_bez_nowej_sesji()
    try:
        zapamietaj_proces(proc.pid)

        sprzatnij_zapamietane()

        assert killpg_zapisany.wywolania == []
        assert proc.wait(10) == -signal.SIGKILL
    finally:
        if proc.poll() is None:
            _dobij(proc)


def test_sprzatanie_rejestru_pomija_proces_ktory_juz_nie_istnieje(killpg_zapisany):
    proc = subprocess.Popen([sys.executable, "-c", KROK_KONCZACY_SIE_OD_RAZU], start_new_session=True)
    zapamietaj_proces(proc.pid)
    proc.wait(10)

    sprzatnij_zapamietane()

    assert killpg_zapisany.wywolania == []


def test_sprzatanie_rejestru_czysci_rejestr(killpg_zapisany):
    procesy._PROCESY[999_999_999] = 999_999_999

    sprzatnij_zapamietane()

    assert procesy._PROCESY == {}


def test_zapamietanie_nieistniejacego_procesu_jest_pomijane_bez_wyjatku():
    proc = subprocess.Popen([sys.executable, "-c", KROK_KONCZACY_SIE_OD_RAZU], start_new_session=True)
    proc.wait(10)

    zapamietaj_proces(proc.pid)

    assert proc.pid not in procesy._PROCESY


def test_uruchomiony_helperem_proces_jest_zapamietany_z_wlasna_grupa():
    proc = uruchom_proces_sync([sys.executable, "-c", KROK_KONCZACY_SIE_OD_RAZU])
    try:
        assert procesy._PROCESY[proc.pid] == proc.pid
        assert os.getpgid(proc.pid) == proc.pid != os.getpgid(0)
    finally:
        zakoncz_proces_sync(proc, nazwa="sprzatanie-atrapy")


# --- straznik: reszta testow nie sygnalizuje grup procesow sama ------------------

TESTY = Path(__file__).resolve().parent

# Jawna i minimalna lista plikow wolnych od straznika: sam helper i ten plik.
WYJATKI_STRAZNIKA = {TESTY / "procesy.py", Path(__file__).resolve()}

# Kazdy element wzorcow ma w testach nizej przypadek, w ktorym tylko on decyduje o wykryciu.
# Klasy znakow: `[^)]` i `[^\n]` maja po jednym znaku (zastapione dowolnym znakiem gina na
# "from os import (getpid)\nkill = 1"). W `[^,()]` kazdy z `,` i `(` ma wlasny przypadek
# (`os.kill(a, b, 0)`, `os.kill(f(pid, 0), 9)`). Usuniecie samego `)` jest rownowazne:
# niesparowany `)` zaraz po `os.kill(` zamyka wywolanie z jednym argumentem, ktore nie
# wysyla zadnego sygnalu, wiec nie ma naruszenia, ktore tylko ten znak wykrywa.
WZORZEC_KILLPG = re.compile(r"\bkillpg\s*\(")
# Galaz `\([^)]*` obejmuje import wielowierszowy w nawiasach; `[^\n]*` import w jednej linii.
WZORZEC_IMPORTU_KILL = re.compile(
    r"from\s+os\s+import\s+(?:\([^)]*|[^\n]*)\bkill(?:pg)?\b"
)
WZORZEC_ALIASU_OS = re.compile(r"import\s+os\s+as\s+(\w+)")


def _wzorzec_os_kill(nazwa: str) -> re.Pattern:
    """`<nazwa>.kill(` dozwolone tylko jako sonda (sygnal 0) albo do WLASNEGO pid."""
    n = re.escape(nazwa)
    return re.compile(
        rf"\b{n}\s*\.\s*kill\s*\((?!\s*{n}\s*\.\s*getpid\(\)\s*,)(?![^,()]+,\s*0\s*\))"
    )


def znajdz_naruszenia(zrodlo: str) -> list[int]:
    """Numery linii z sygnalem do grupy albo do cudzego pid-u poza helperem."""
    nazwy_os = {"os", *WZORZEC_ALIASU_OS.findall(zrodlo)}
    wzorce = [WZORZEC_KILLPG, WZORZEC_IMPORTU_KILL]
    wzorce += [_wzorzec_os_kill(n) for n in sorted(nazwy_os)]
    numery = []
    for wzorzec in wzorce:
        numery += [zrodlo.count("\n", 0, m.start()) + 1 for m in wzorzec.finditer(zrodlo)]
    return sorted(set(numery))


@pytest.mark.parametrize(
    "zrodlo",
    [
        "os.killpg(pgid, signal.SIGKILL)",                  # killpg po kropce
        "killpg(pgid, 9)",                                  # killpg bez os.
        "os.killpg (pgid, 9)",                              # KILLPG: \s* przed nawiasem
        "_os.killpg(pgid, 9)",                              # KILLPG przez alias modulu
        "from os import killpg",                            # import: (?:pg)?
        "from os import getpid, kill",                      # import: kill w liscie
        "from  os  import  kill",                           # import: \s+ (wiele spacji)
        "from os import (\n    getpid,\n    kill,\n)",      # import: galaz w nawiasach
        "os.kill(pid, signal.SIGKILL)",                     # sygnal inny niz 0
        "os.kill(os.getpid() + 1, signal.SIGKILL)",         # nie WLASNY pid (\s*, po getpid)
        "os .kill(pid, 9)",                                 # OS_KILL: \s* przed kropka
        "os. kill(pid, 9)",                                 # OS_KILL: \s* po kropce
        "os.kill (pid, 9)",                                 # OS_KILL: \s* przed nawiasem
        "os.kill(a, b, 0)",                                 # sonda: pierwszy argument bez przecinka
        "os.kill(int(x), 0)",                               # sonda: pierwszy argument bez nawiasow
        "os.kill(f(pid, 0), 9)",                            # sonda: `(` w klasie; bez niej `f(pid, 0` to sonda
        "os.kill(pid, 0 | 9)",                             # sonda: po 0 musi byc zamkniecie
        "os.kill(pid, 01)",                                 # sonda: dokladnie 0, nie 01
        "import os as _os\n_os.kill(-pgid, 9)",             # alias modulu
        "import  os  as  _os\n_os.kill(-pgid, 9)",         # alias: \s+ (wiele spacji)
        "import os as _os\nos.kill(pid, 9)",                # alias nie zastepuje samego os
        "import os as _os\n_os.kill(os.getpid(), 2)",       # getpid z innego modulu to nie WLASNY pid
    ],
)
def test_straznik_wykrywa_sygnal_do_grupy_lub_cudzego_pid_poza_helperem(zrodlo):
    assert znajdz_naruszenia(zrodlo) != []


@pytest.mark.parametrize(
    ("zrodlo", "linie"),
    [
        ("os.killpg(pgid, 9)", [1]),
        ("a = 1\nos.killpg(1, 9)\n", [2]),
        ("from os import (\n    kill,\n)", [1]),
    ],
)
def test_straznik_podaje_numer_linii_naruszenia(zrodlo, linie):
    assert znajdz_naruszenia(zrodlo) == linie


@pytest.mark.parametrize(
    "zrodlo",
    [
        "os.kill(pid, 0)",                                  # sonda
        "os.kill(pid,0)",                                   # sonda bez spacji po przecinku
        "os.kill(pid, 0 )",                                 # sonda ze spacja przed nawiasem
        "os.kill(os.getpid(), signal.SIGINT)",              # sygnal do samego siebie
        "os.kill( os.getpid(), 2)",                         # \s* po nawiasie
        "os.kill(os.getpid() , 2)",                         # \s* przed przecinkiem
        "os.kill(os . getpid(), 2)",                        # \s* wokol kropki w getpid
        "import os as _os\n_os.kill(_os.getpid(), 2)",      # wlasny pid przez alias
        "proc.kill()",                                      # metoda Popen/Process
        "xkillpg(1, 9)",                                    # KILLPG: \b przed killpg
        "pos.kill(pid, 9)",                                 # OS_KILL: \b przed nazwa modulu
        "from os import unkill",                            # import: \b przed kill
        "from os import killer",                            # import: \b po kill
        "from os import kill_all",                          # import: \b po kill (znak slowa)
        "from os import getpid\nkill = 1",                  # import: tylko ta sama linia
        "from os import (getpid)\nkill = 1",                # import: nawias konczy liste
        "import os as _os\nproc.kill()",                    # alias bez wywolania os.kill
    ],
)
def test_straznik_przepuszcza_sonde_sygnal_do_wlasnego_pid_i_metody_procesu(zrodlo):
    assert znajdz_naruszenia(zrodlo) == []


def test_straznik_przeglada_pliki_testow_i_znajduje_ich_wiecej_niz_sam_siebie():
    pliki = [p for p in TESTY.rglob("*.py") if p not in WYJATKI_STRAZNIKA]

    assert len(pliki) > 5      # zero przejrzanych plikow to blad, nie czysto
    assert TESTY / "conftest.py" in pliki


def test_testy_nie_wysylaja_sygnalow_do_grup_procesow_poza_wspolnym_helperem():
    naruszenia = {}
    for plik in sorted(TESTY.rglob("*.py")):
        if plik in WYJATKI_STRAZNIKA:
            continue
        numery = znajdz_naruszenia(plik.read_text(encoding="utf-8"))
        if numery:
            naruszenia[str(plik.relative_to(TESTY))] = numery

    assert naruszenia == {}, "uzyj tests/procesy.py zamiast os.kill/os.killpg"


# --- sam SIGTERM bez eskalacji (HAR-226) -----------------------------------------


def test_przerwanie_sigtermem_konczy_proces_reagujacy_na_sigterm_jego_sygnalem(tmp_path):
    znacznik = tmp_path / "gotowy"
    proc = uruchom_proces_sync([sys.executable, "-c", KROK_REAGUJACY_NA_SIGTERM, str(znacznik)])
    try:
        _czekaj_synchronicznie_na_plik(znacznik)
        przerwij_sigtermem_sync(proc, nazwa="grzeczny-krok", limit=10)

        assert proc.returncode == -signal.SIGTERM
    finally:
        zakoncz_proces_sync(proc, nazwa="sprzatanie-atrapy")


def test_przerwanie_sigtermem_procesu_ignorujacego_sigterm_daje_blad_z_nazwa_i_nie_dobija_go(
    tmp_path,
):
    znacznik = tmp_path / "gotowy"
    proc = uruchom_proces_sync([sys.executable, "-c", KROK_IGNORUJACY_SIGTERM, str(znacznik)])
    try:
        _czekaj_synchronicznie_na_plik(znacznik)

        with pytest.raises(LimitCzasuTestu, match="uparty-krok"):
            przerwij_sigtermem_sync(proc, nazwa="uparty-krok", limit=0.3)

        assert proc.poll() is None  # zadnej eskalacji do SIGKILL
    finally:
        zakoncz_proces_sync(proc, nazwa="sprzatanie-atrapy")


def test_przerwanie_sigtermem_procesu_z_grupy_testu_nie_wysyla_sygnalu_do_grupy_ale_dobija_pid(
    killpg_zapisany,
):
    proc = _uzyj_wolnego_procesu_bez_nowej_sesji()
    try:
        with pytest.raises(WlasnaGrupaProcesow, match="atrapa-sigterm"):
            przerwij_sigtermem_sync(proc, nazwa="atrapa-sigterm")

        assert killpg_zapisany.wywolania == []
        assert proc.wait(10) == -signal.SIGKILL
    finally:
        if proc.poll() is None:
            _dobij(proc)


def test_przerwanie_sigtermem_zakonczonego_procesu_nie_dotyka_grup_procesow(monkeypatch):
    proc = uruchom_proces_sync([sys.executable, "-c", KROK_KONCZACY_SIE_OD_RAZU])
    proc.wait(10)
    wywolania: list[str] = []
    monkeypatch.setattr(procesy.os, "killpg", lambda *a: wywolania.append("killpg"))
    monkeypatch.setattr(procesy.os, "getpgid", lambda *a: wywolania.append("getpgid") or 0)

    przerwij_sigtermem_sync(proc, nazwa="juz-zakonczony")

    assert wywolania == []


def test_przerwanie_sigtermem_procesu_bez_ustalonej_grupy_tylko_go_odbiera(killpg_zapisany):
    class Zombie(AtrapaProcesuBezGrupy):
        def wait(self, timeout=None):
            self.returncode = 0

    atrapa = Zombie(_pid_nieistniejacego_procesu())

    przerwij_sigtermem_sync(atrapa, nazwa="zombie-sigterm")

    assert atrapa.returncode == 0
    assert killpg_zapisany.wywolania == []


# --- straznik: procesy i czekanie w testach (HAR-242) ----------------------------------------
# Regula `.claude/rules/testy-podprocesow.md`: proces w tescie startuje przez `procesy.py`,
# podproces z kodem projektu ma `cwd` = korzen repo, kazde czekanie ma limit czasu. Skaner
# idzie po AST (komentarz i napis nie sa wywolaniem). Granice skanera: argv podany przez
# zmienna (nie literal) nie jest oceniany pod katem `tools/` i `-m runner`; `**opcje` w
# wywolaniu uznajemy za mogace niesc `cwd`.

POLECENIA_PODPROCESU = frozenset(
    {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"})
HELPERY_PODPROCESU = frozenset({"uruchom_proces", "uruchom_proces_sync", "uruchom_i_zbierz"})
PODPROCES_POZA_HELPEREM = "podproces_poza_helperem"
PODPROCES_BEZ_CWD = "podproces_projektu_bez_cwd"
CZEKANIE_BEZ_LIMITU = "czekanie_bez_limitu"


# Wyjatki od reguly "czekanie ma limit": (plik wzgledem tests/, funkcja) -> uzasadnienie. Dotycza
# wylacznie funkcji ATRAP (serwer, strumien, brama), ktorych zadaniem jest czekac do zwolnienia
# przez test albo do anulowania przez zatrzymywany kod pod testem; czekanie samego testu
# (`czekaj`, `wait_for`) limit ma. Lista niezalezna od skanera: usuniecie wpisu oblewa
# `test_testy_nie_czekaja_bez_limitu_poza_jawnymi_wyjatkami`, a wpis bez naruszenia -
# `test_kazdy_wyjatek_od_czekania_bez_limitu_nadal_jest_potrzebny`.
_ATRAPA_ZAWIESZA = "atrapa serwera zawiesza zadanie celowo; konczy je anulowanie przy stop() kodu pod testem"
_ATRAPA_STRUMIENIA = "strumien atrapy czeka na kolejne dane albo koniec; konczy go sentinel albo anulowanie"
_ATRAPA_BRAMY = "brama atrapy zwalniana przez test (`set()`); test, ktory ja otwiera, ma wlasny limit"
WYJATKI_CZEKANIA: dict[tuple[str, str], str] = {
    ("test_heartbeat_runnera.py", "handle_async_request"): _ATRAPA_ZAWIESZA,
    ("test_heartbeat_runnera.py", "__aiter__"): _ATRAPA_STRUMIENIA,
    ("test_heartbeat_runnera.py", "exec_z_bramka"): _ATRAPA_BRAMY,
    ("test_heartbeat_runnera.py", "stop"): _ATRAPA_BRAMY,
    ("test_polecenie_run_workflow.py", "__aiter__"): _ATRAPA_STRUMIENIA,
    ("test_protokol_bez_argv.py", "__aiter__"): _ATRAPA_STRUMIENIA,
    ("test_zapytania_runnera.py", "wolna"): _ATRAPA_ZAWIESZA,
    ("transport_asgi.py", "__aiter__"): _ATRAPA_STRUMIENIA,
    ("transport_asgi.py", "receive"): _ATRAPA_ZAWIESZA,
    ("transport_asgi.py", "handle_async_request"): _ATRAPA_STRUMIENIA,
}


def _aliasy_importow(drzewo: ast.AST) -> tuple[dict[str, str], dict[str, str]]:
    """(alias modulu -> modul, nazwa lokalna -> `modul.nazwa`) dla `subprocess` i `asyncio`."""
    moduly: dict[str, str] = {}
    nazwy: dict[str, str] = {}
    for w in ast.walk(drzewo):
        if isinstance(w, ast.Import):
            for a in w.names:
                if a.name in ("subprocess", "asyncio"):
                    moduly[a.asname or a.name] = a.name
        elif isinstance(w, ast.ImportFrom) and w.module in ("subprocess", "asyncio"):
            for a in w.names:
                nazwy[a.asname or a.name] = f"{w.module}.{a.name}"
    return moduly, nazwy


def _pelna_nazwa(func: ast.expr, moduly: dict[str, str], nazwy: dict[str, str]) -> str:
    """`subprocess.run` dla `subprocess.run`, `sp.run` (alias) i `run` (import z subprocess);
    metoda cudzego obiektu to `?.<metoda>`; inne wyrazenie to pusty napis."""
    if isinstance(func, ast.Name):
        return nazwy.get(func.id, func.id)
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name) and func.value.id in moduly:
            return f"{moduly[func.value.id]}.{func.attr}"
        return f"?.{func.attr}"
    return ""


def _jest_podprocesem(pelna: str) -> bool:
    modul, _, nazwa = pelna.partition(".")
    return (modul == "subprocess" and nazwa in POLECENIA_PODPROCESU) or (
        modul == "asyncio" and nazwa.startswith("create_subprocess_"))


def _jest_helperem(pelna: str) -> bool:
    return pelna.rpartition(".")[2] in HELPERY_PODPROCESU


def _ma_slowo_kluczowe(w: ast.Call, nazwa: str) -> bool:
    """Slowo kluczowe `nazwa=` z wartoscia inna niz `None` (`timeout=None` to brak limitu)."""
    return any(k.arg == nazwa and not _jest_none(k.value) for k in w.keywords)


def _jest_none(w: ast.AST) -> bool:
    return isinstance(w, ast.Constant) and w.value is None


def _ma_cwd(w: ast.Call) -> bool:
    for k in w.keywords:
        if k.arg is None:
            return True
        if k.arg == "cwd":
            return not (isinstance(k.value, ast.Constant) and k.value.value is None)
    return False


def _tekst(w: ast.AST) -> str | None:
    return w.value if isinstance(w, ast.Constant) and isinstance(w.value, str) else None


def _kod_projektu_w_argv(w: ast.Call) -> bool:
    """Literal `tools`/`tools/...` w argv albo `-m` tuz przed modulem `runner...`."""
    wyrazenia = [*w.args, *(k.value for k in w.keywords if k.arg == "args")]
    for wyrazenie in wyrazenia:
        for n in ast.walk(wyrazenie):
            t = _tekst(n)
            if t is not None and (t == "tools" or t.startswith("tools/") or "/tools/" in t):
                return True
    ciagi = [w.args] + [
        n.elts for wyrazenie in wyrazenia for n in ast.walk(wyrazenie)
        if isinstance(n, (ast.List, ast.Tuple))]
    for ciag in ciagi:
        for a, b in zip(ciag, ciag[1:]):
            if _tekst(a) == "-m" and (_tekst(b) or "").startswith("runner"):
                return True
    return False


def _ma_limit_wait_for(w: ast.Call) -> bool:
    drugi = w.args[1] if len(w.args) > 1 else next(
        (k.value for k in w.keywords if k.arg == "timeout"), None)
    return drugi is not None and not (isinstance(drugi, ast.Constant) and drugi.value is None)


def _w_ograniczonym_czekaniu(w: ast.AST, rodzice: dict[ast.AST, ast.AST], moduly, nazwy) -> bool:
    """Wywolanie jest argumentem `czekaj*`, `wait_for` z limitem, `asyncio.wait(timeout=)`
    albo stoi w bloku `async with asyncio.timeout(...)`."""
    obecny = w
    while obecny in rodzice:
        obecny = rodzice[obecny]
        if isinstance(obecny, ast.Call):
            nazwa = _pelna_nazwa(obecny.func, moduly, nazwy).rpartition(".")[2]
            if nazwa.startswith("czekaj"):
                return True
            if nazwa == "wait_for" and _ma_limit_wait_for(obecny):
                return True
            if nazwa == "wait" and _ma_slowo_kluczowe(obecny, "timeout"):
                return True
        if isinstance(obecny, (ast.With, ast.AsyncWith)):
            for pozycja in obecny.items:
                if isinstance(pozycja.context_expr, ast.Call) and _pelna_nazwa(
                    pozycja.context_expr.func, moduly, nazwy
                ).rpartition(".")[2] in ("timeout", "timeout_at"):
                    return True
    return False


def _jest_submit(w: ast.AST) -> bool:
    return isinstance(w, ast.Call) and isinstance(w.func, ast.Attribute) and w.func.attr == "submit"


def _nazwy_z_submit(drzewo: ast.AST) -> set[str]:
    """Nazwy przypisane z wyniku `<pula>.submit(...)` (wynik to `concurrent.futures.Future`)."""
    nazwy: set[str] = set()
    for w in ast.walk(drzewo):
        if isinstance(w, ast.Assign) and _jest_submit(w.value):
            nazwy.update(t.id for t in w.targets if isinstance(t, ast.Name))
    return nazwy


def _wait_blokuje(w: ast.Call) -> bool:
    """`.wait()` bez limitu: bez argumentow albo z `None`/`timeout=None`."""
    if any(k.arg != "timeout" for k in w.keywords) or len(w.args) > 1:
        return False
    if w.args and not _jest_none(w.args[0]):
        return False
    return not _ma_slowo_kluczowe(w, "timeout")


def _get_blokuje(w: ast.Call) -> bool:
    """`.get()` kolejki bez limitu: bez argumentow, `True`, `block=True`, `timeout=None`.
    `slownik.get('k')` i `get(False)` (nieblokujace) nie sa zgłaszane."""
    if any(k.arg not in ("block", "timeout") for k in w.keywords):
        return False
    if _ma_slowo_kluczowe(w, "timeout"):
        return False
    if any(k.arg == "block" and isinstance(k.value, ast.Constant) and k.value.value is False
           for k in w.keywords):
        return False
    if not w.args:
        return True
    pierwszy = w.args[0]
    if not (isinstance(pierwszy, ast.Constant) and pierwszy.value is True):
        return False
    return len(w.args) < 2 or _jest_none(w.args[1])


def _czeka_bez_limitu(w: ast.Call, pelna: str, z_submit: set[str]) -> bool:
    nazwa = pelna.rpartition(".")[2]
    if pelna.startswith("?.") and nazwa == "wait":
        return _wait_blokuje(w)
    if pelna.startswith("?.") and nazwa == "get":
        return _get_blokuje(w)
    if pelna == "asyncio.wait" and not _ma_slowo_kluczowe(w, "timeout"):
        return True
    if nazwa == "wait_for" and not pelna.startswith("?.") and not _ma_limit_wait_for(w):
        return True
    return (
        pelna == "?.result" and not w.args and not w.keywords and isinstance(w.func, ast.Attribute)
        and (_jest_submit(w.func.value)
             or (isinstance(w.func.value, ast.Name) and w.func.value.id in z_submit))
    )


def _funkcja_obejmujaca(w: ast.AST, rodzice: dict[ast.AST, ast.AST]) -> str:
    """Nazwa najblizszej funkcji obejmujacej wezel; pusty napis na poziomie modulu."""
    while w in rodzice:
        w = rodzice[w]
        if isinstance(w, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return w.name
    return ""


def _naruszenia_surowe(zrodlo: str) -> list[tuple[int, str, str]]:
    """(linia, kod, funkcja obejmujaca) bez zadnych wyjatkow."""
    drzewo = ast.parse(zrodlo)
    moduly, nazwy = _aliasy_importow(drzewo)
    rodzice = {dziecko: w for w in ast.walk(drzewo) for dziecko in ast.iter_child_nodes(w)}
    z_submit = _nazwy_z_submit(drzewo)
    wynik: set[tuple[int, str, str]] = set()
    for w in ast.walk(drzewo):
        if not isinstance(w, ast.Call):
            continue
        pelna = _pelna_nazwa(w.func, moduly, nazwy)
        funkcja = _funkcja_obejmujaca(w, rodzice)
        if _jest_podprocesem(pelna):
            wynik.add((w.lineno, PODPROCES_POZA_HELPEREM, funkcja))
        if (_jest_podprocesem(pelna) or _jest_helperem(pelna)) and _kod_projektu_w_argv(w) \
                and not _ma_cwd(w):
            wynik.add((w.lineno, PODPROCES_BEZ_CWD, funkcja))
        if _czeka_bez_limitu(w, pelna, z_submit) and not _w_ograniczonym_czekaniu(
            w, rodzice, moduly, nazwy
        ):
            wynik.add((w.lineno, CZEKANIE_BEZ_LIMITU, funkcja))
    return sorted(wynik)


def znajdz_naruszenia_procesow(
    zrodlo: str, nazwa_pliku: str, wyjatki: dict[tuple[str, str], str] = WYJATKI_CZEKANIA,
) -> list[tuple[int, str]]:
    """(linia, kod) naruszen reguly procesow w tescie. Pliki z `WYJATKI_STRAZNIKA` sa wolne;
    czekanie bez limitu w funkcji z `wyjatki` (klucz: plik wzgledem tests/, funkcja) tez."""
    if TESTY / nazwa_pliku in WYJATKI_STRAZNIKA:
        return []
    wzgledna = (TESTY / nazwa_pliku).relative_to(TESTY).as_posix()
    return sorted({
        (linia, kod) for linia, kod, funkcja in _naruszenia_surowe(zrodlo)
        if not (kod == CZEKANIE_BEZ_LIMITU and (wzgledna, funkcja) in wyjatki)
    })


# --- straznik procesow: skaner na atrapach, kazdy element osobno --------------------------------


def _naruszenia(zrodlo: str, nazwa: str = "test_atrapa.py", **opcje) -> list[tuple[int, str]]:
    return znajdz_naruszenia_procesow(zrodlo, nazwa, **opcje)


POZA = PODPROCES_POZA_HELPEREM
BEZ_CWD = PODPROCES_BEZ_CWD
BEZ_LIMITU = CZEKANIE_BEZ_LIMITU


@pytest.mark.parametrize("polecenie", ["run", "Popen", "call", "check_call", "check_output",
                                       "getoutput", "getstatusoutput"])
def test_straznik_wykrywa_kazde_polecenie_modulu_subprocess_poza_helperem(polecenie):
    assert _naruszenia(f"import subprocess\nsubprocess.{polecenie}(['ls'])\n") == [(2, POZA)]


@pytest.mark.parametrize("polecenie", ["create_subprocess_exec", "create_subprocess_shell"])
def test_straznik_wykrywa_kazde_create_subprocess_poza_helperem(polecenie):
    assert _naruszenia(f"import asyncio\nasyncio.{polecenie}('ls')\n") == [(2, POZA)]


@pytest.mark.parametrize(
    "zrodlo",
    [
        "import subprocess as sp\nsp.run(['ls'])\n",                          # alias modulu
        "import asyncio as aio\naio.create_subprocess_exec('ls')\n",          # alias asyncio
        "from subprocess import run\nrun(['ls'])\n",                          # import nazwy
        "from subprocess import Popen as P\nP(['ls'])\n",                     # import z aliasem
        "from asyncio import create_subprocess_exec\ncreate_subprocess_exec('ls')\n",
    ],
)
def test_straznik_wykrywa_podproces_przez_alias_modulu_i_import_nazwy(zrodlo):
    assert _naruszenia(zrodlo) == [(2, POZA)]


@pytest.mark.parametrize(
    "zrodlo",
    [
        "import subprocess\nsubprocess.CompletedProcess(['ls'], 0)\n",        # typ wyniku
        "import subprocess\nx = subprocess.PIPE\n",                           # odwolanie do stalej
        "import subprocess\nx: subprocess.Popen\n",                           # adnotacja
        "import subprocess\nraise subprocess.TimeoutExpired('ls', 1)\n",       # wyjatek
        "import subprocess\nsubprocess.create_subprocess_exec('ls')\n",       # zly modul
        "import asyncio\nasyncio.run(f())\n",                                 # run w asyncio
        "import asyncio\nasyncio.Popen('ls')\n",                              # Popen nie z asyncio
        "import asyncio\nasyncio.create_task(f())\n",                         # create_task
        "import asyncio\nasyncio.sleep(1)\n",
        "from procesy import uruchom_proces_sync\nuruchom_proces_sync(['ls'])\n",   # helper
        "def run(x):\n    return x\nrun(1)\n",                                # lokalna `run`
        "import other\nother.run(['ls'])\n",                                  # inny modul
        "obiekt.run(['ls'])\n",                                               # metoda obiektu
        "x = 'subprocess.run(1)'\n# subprocess.run(2)\n",                     # napis i komentarz
    ],
)
def test_straznik_przepuszcza_typy_stale_helpery_i_inne_moduly(zrodlo):
    assert _naruszenia(zrodlo) == []


@pytest.mark.parametrize(
    "zrodlo",
    [
        "uruchom_proces_sync([sys.executable, '-m', 'runner', 'x'])\n",       # `-m runner`
        "uruchom_proces_sync([sys.executable, '-m', 'runner.__main__'])\n",   # modul runnera
        "await uruchom_proces(sys.executable, '-m', 'runner', 'x')\n",        # argv pozycyjnie
        "uruchom_proces_sync([sys.executable, 'tools/check_x.py'])\n",        # `tools/` na poczatku
        "uruchom_proces_sync([sys.executable, '/r/tools/x.py'])\n",           # `/tools/` w srodku
        "uruchom_proces_sync([sys.executable, str(K / 'tools' / 'x.py')])\n",   # skladnik `tools`
        "procesy.uruchom_proces_sync(['python', 'tools/x.py'])\n",            # helper jako atrybut
        "procesy.uruchom_proces('python', 'tools/x.py')\n",                   # wariant asynchroniczny
        "uruchom_proces_sync(['python', 'tools/x.py'], cwd=None)\n",          # `cwd=None` to brak
        "uruchom_proces_sync(args=['python', 'tools/x.py'])\n",               # argv jako `args=`
    ],
)
def test_straznik_wykrywa_podproces_z_kodem_projektu_bez_cwd(zrodlo):
    assert _naruszenia(zrodlo) == [(1, BEZ_CWD)]


def test_straznik_wykrywa_polecenie_subprocess_z_kodem_projektu_dwoma_naruszeniami():
    zrodlo = "import subprocess\nsubprocess.run(['python', 'tools/x.py'])\n"

    assert _naruszenia(zrodlo) == [(2, POZA), (2, BEZ_CWD)]


def test_straznik_wykrywa_create_subprocess_z_runnerem_bez_cwd():
    zrodlo = "import asyncio\nasyncio.create_subprocess_exec('python', '-m', 'runner')\n"

    assert _naruszenia(zrodlo) == [(2, POZA), (2, BEZ_CWD)]


@pytest.mark.parametrize(
    "zrodlo",
    [
        "uruchom_proces_sync([sys.executable, '-m', 'runner'], cwd=KORZEN)\n",   # jest `cwd`
        "uruchom_proces_sync([sys.executable, 'tools/x.py'], cwd=KORZEN)\n",
        "uruchom_proces_sync([sys.executable, 'tools/x.py'], **opcje)\n",        # `**` moze nosic cwd
        "uruchom_proces_sync([sys.executable, '-m', 'pytest'])\n",               # `-m` innego modulu
        "uruchom_proces_sync([sys.executable, 'runner'])\n",                    # bez `-m`
        "uruchom_proces_sync([sys.executable, '-m', 'x', 'runner'])\n",         # `-m` nie tuz przed
        "uruchom_proces_sync([sys.executable, 'toolsbox/x.py'])\n",             # inny katalog
        "uruchom_proces_sync([sys.executable, 'mytools/x.py'])\n",
        "uruchom_proces_sync([sys.executable, '-c', 'pass'])\n",                # bez kodu projektu
        "uruchom_proces_sync(argv)\n",                                          # argv ze zmiennej
        "inna(['python', 'tools/x.py'])\n",                                     # nie podproces
    ],
)
def test_straznik_przepuszcza_podproces_z_cwd_albo_bez_kodu_projektu(zrodlo):
    assert _naruszenia(zrodlo) == []


@pytest.mark.parametrize(
    "zrodlo",
    [
        "proc.wait()\n",
        "await asyncio.Event().wait()\n",
        "await zdarzenie.wait()\n",
        "kolejka.get()\n",
        "await kolejka.get()\n",
        "import asyncio\nasyncio.wait_for(f())\n",
        "import asyncio\nasyncio.wait_for(f(), None)\n",
        "import asyncio\nasyncio.wait_for(f(), timeout=None)\n",
        "from asyncio import wait_for\nwait_for(f())\n",
        "pula.submit(f).result()\n",
        "przyszlosc = pula.submit(f)\nprzyszlosc.result()\n",
    ],
)
def test_straznik_wykrywa_czekanie_bez_limitu(zrodlo):
    wyniki = _naruszenia(zrodlo)

    assert [kod for _, kod in wyniki] == [BEZ_LIMITU]
    assert wyniki[0][0] == zrodlo.count("\n")       # ostatnia linia zrodla


@pytest.mark.parametrize(
    "zrodlo",
    [
        "proc.wait(timeout=5)\n",                      # limit slowem kluczowym
        "proc.wait(5)\n",                              # limit pozycyjnie
        "kolejka.get(timeout=3)\n",
        "kolejka.get(False)\n",                        # nieblokujace
        "slownik.get('k')\n",                          # dict.get
        "import asyncio\nasyncio.wait_for(f(), 5)\n",
        "import asyncio\nasyncio.wait_for(f(), timeout=5)\n",
        "obiekt.wait_for(f())\n",                      # metoda obcego obiektu, nie asyncio
        "pula.submit(f).result(timeout=5)\n",
        "pula.submit(f).result(5)\n",
        "zadanie.result()\n",                          # zadanie asyncio: nie blokuje
        "przyszlosc = pula.map(f)\nprzyszlosc.result()\n",   # nie z `submit`
        "pula.submit(f)\n",                            # sam `submit` nie czeka
        "def result():\n    return 1\nresult()\n",      # zwykla funkcja o nazwie result
        "wynik = result()\n",
    ],
)
def test_straznik_przepuszcza_czekanie_z_limitem_i_wywolania_ktore_nie_czekaja(zrodlo):
    assert _naruszenia(zrodlo) == []


@pytest.mark.parametrize(
    "zrodlo",
    [
        "await czekaj(proc.wait(), 5, 'koniec')\n",
        "await czekaj_bez_anulowania(zdarzenie.wait(), 5, 'x')\n",
        "await czekaj_na_warunek(lambda: kolejka.get(), 5, 'x')\n",
        "await czekaj(asyncio.create_task(zdarzenie.wait()), 5, 'x')\n",    # zagniezdzone glebiej
        "await czekaj(f(g(zdarzenie.wait())), 5, 'x')\n",
        "import asyncio\nawait asyncio.wait_for(proc.wait(), 5)\n",
        "import asyncio\nawait asyncio.wait_for(proc.wait(), timeout=5)\n",
        "import asyncio\nawait asyncio.wait([zdarzenie.wait()], timeout=3)\n",
        "import asyncio\nasync with asyncio.timeout(5):\n    await zdarzenie.wait()\n",
        "import asyncio\nasync with asyncio.timeout_at(5):\n    await zdarzenie.wait()\n",
        "from asyncio import timeout\nasync with timeout(5):\n    await zdarzenie.wait()\n",
        "import asyncio\nwith asyncio.timeout(5):\n    zdarzenie.wait()\n",
    ],
)
def test_straznik_przepuszcza_czekanie_bedace_argumentem_limitowanego_wywolania_albo_w_bloku_limitu(
    zrodlo,
):
    assert _naruszenia(zrodlo) == []


@pytest.mark.parametrize(
    "zrodlo",
    [
        "foo(zdarzenie.wait())\n",                                   # zwykle wywolanie nie limituje
        "import asyncio\nasyncio.wait([zdarzenie.wait()])\n",         # `asyncio.wait` bez timeout
        "import asyncio\nasyncio.wait_for(zdarzenie.wait(), None)\n",  # `wait_for` bez limitu
        "import asyncio\nasync with inny(5):\n    await zdarzenie.wait()\n",  # inny menedzer
        "async with blokada:\n    await zdarzenie.wait()\n",           # menedzer bez wywolania
        "await czaj(zdarzenie.wait(), 5)\n",                           # prefiks `czekaj`, nie `cz`
    ],
)
def test_straznik_zglasza_czekanie_w_wywolaniu_ktore_nie_daje_limitu(zrodlo):
    wyniki = _naruszenia(zrodlo)

    assert BEZ_LIMITU in [kod for _, kod in wyniki]
    assert wyniki[-1][0] == zrodlo.count("\n")


def test_straznik_zglasza_wait_for_bez_limitu_razem_z_czekaniem_w_jego_argumencie():
    zrodlo = "import asyncio\nawait asyncio.wait_for(\n    proc.wait(), None)\n"

    assert _naruszenia(zrodlo) == [(2, BEZ_LIMITU), (3, BEZ_LIMITU)]


def test_straznik_podaje_numer_linii_kazdego_naruszenia_w_kolejnosci():
    zrodlo = (
        "import subprocess\n"
        "proc.wait()\n"
        "subprocess.run(['ls'])\n"
        "kolejka.get()\n"
    )

    assert _naruszenia(zrodlo) == [(2, BEZ_LIMITU), (3, POZA), (4, BEZ_LIMITU)]


@pytest.mark.parametrize("nazwa", ["procesy.py", "test_procesy_testowe.py"])
def test_straznik_procesow_pomija_helper_i_wlasny_plik(nazwa):
    zrodlo = "import subprocess\nsubprocess.run(['ls'])\nproc.wait()\n"

    assert _naruszenia(zrodlo, nazwa) == []


def test_straznik_procesow_pomija_wyjatek_takze_gdy_nazwa_pliku_jest_bezwzgledna():
    zrodlo = "import subprocess\nsubprocess.run(['ls'])\n"

    assert _naruszenia(zrodlo, str(TESTY / "procesy.py")) == []


@pytest.mark.parametrize("nazwa", ["test_inny.py", "e2e/procesy.py", "test_procesy_testowe2.py"])
def test_straznik_procesow_nie_pomija_plikow_spoza_listy_wyjatkow(nazwa):
    assert _naruszenia("import subprocess\nsubprocess.run(['ls'])\n", nazwa) == [(2, POZA)]


ZRODLO_ATRAPY_Z_CZEKANIEM = (
    "class Atrapa:\n"
    "    async def zawies(self):\n"
    "        await asyncio.Event().wait()\n"
)


def test_wyjatek_od_czekania_dziala_dla_pliku_i_funkcji_z_listy():
    wyjatki = {("test_a.py", "zawies"): "atrapa"}

    assert _naruszenia(ZRODLO_ATRAPY_Z_CZEKANIEM, "test_a.py", wyjatki=wyjatki) == []


def test_wyjatek_od_czekania_nie_obejmuje_innej_funkcji_tego_samego_pliku():
    wyjatki = {("test_a.py", "inna"): "atrapa"}

    assert _naruszenia(ZRODLO_ATRAPY_Z_CZEKANIEM, "test_a.py", wyjatki=wyjatki) == [(3, BEZ_LIMITU)]


def test_wyjatek_od_czekania_nie_obejmuje_tej_samej_funkcji_w_innym_pliku():
    wyjatki = {("test_a.py", "zawies"): "atrapa"}

    assert _naruszenia(ZRODLO_ATRAPY_Z_CZEKANIEM, "test_b.py", wyjatki=wyjatki) == [(3, BEZ_LIMITU)]


def test_wyjatek_od_czekania_liczy_sie_wzgledem_katalogu_testow_takze_dla_sciezki_bezwzglednej():
    wyjatki = {("e2e/test_a.py", "zawies"): "atrapa"}

    assert _naruszenia(
        ZRODLO_ATRAPY_Z_CZEKANIEM, str(TESTY / "e2e" / "test_a.py"), wyjatki=wyjatki) == []


def test_wyjatek_od_czekania_nie_zwalnia_podprocesu_w_tej_samej_funkcji():
    zrodlo = (
        "import subprocess\n"
        "def zawies():\n"
        "    subprocess.run(['ls'])\n"
        "    proc.wait()\n"
    )

    assert _naruszenia(zrodlo, "test_a.py", wyjatki={("test_a.py", "zawies"): "atrapa"}) == [
        (3, POZA)]


def test_wyjatek_od_czekania_wskazuje_najblizsza_funkcje_obejmujaca():
    zrodlo = (
        "def zewnetrzna():\n"
        "    def wewnetrzna():\n"
        "        proc.wait()\n"
    )

    assert _naruszenia(zrodlo, "test_a.py", wyjatki={("test_a.py", "wewnetrzna"): "x"}) == []
    assert _naruszenia(zrodlo, "test_a.py", wyjatki={("test_a.py", "zewnetrzna"): "x"}) == [
        (3, BEZ_LIMITU)]


def test_czekanie_na_poziomie_modulu_nie_jest_zwolnione_wyjatkiem_dla_funkcji():
    assert _naruszenia("proc.wait()\n", "test_a.py", wyjatki={("test_a.py", "f"): "x"}) == [
        (1, BEZ_LIMITU)]


def test_funkcja_asynchroniczna_i_zwykla_sa_funkcjami_obejmujacymi():
    zwykla = "def f():\n    proc.wait()\n"
    asynchroniczna = "async def f():\n    proc.wait()\n"
    wyjatki = {("test_a.py", "f"): "x"}

    assert _naruszenia(zwykla, "test_a.py", wyjatki=wyjatki) == []
    assert _naruszenia(asynchroniczna, "test_a.py", wyjatki=wyjatki) == []


# --- straznik procesow: prawdziwe pliki testow ---------------------------------------------------


def _wszystkie_naruszenia_procesow() -> dict[str, list[tuple[int, str]]]:
    wynik = {}
    for plik in sorted(TESTY.rglob("*.py")):
        if plik in WYJATKI_STRAZNIKA:
            continue
        naruszenia = znajdz_naruszenia_procesow(plik.read_text(encoding="utf-8"), str(plik))
        if naruszenia:
            wynik[plik.relative_to(TESTY).as_posix()] = naruszenia
    return wynik


def _tylko(kod: str) -> dict[str, list[int]]:
    return {
        plik: [linia for linia, k in naruszenia if k == kod]
        for plik, naruszenia in _wszystkie_naruszenia_procesow().items()
        if any(k == kod for _, k in naruszenia)
    }


def test_straznik_procesow_przeglada_wiecej_niz_piec_plikow_w_tym_podkatalog_e2e():
    pliki = [p for p in TESTY.rglob("*.py") if p not in WYJATKI_STRAZNIKA]

    assert len(pliki) > 5      # zero przejrzanych plikow to blad, nie czysto
    assert TESTY / "conftest.py" in pliki
    assert TESTY / "e2e" / "conftest.py" in pliki


def test_testy_uruchamiaja_procesy_wylacznie_przez_helper_z_procesy_py():
    assert _tylko(PODPROCES_POZA_HELPEREM) == {}, "uzyj tests/procesy.py zamiast subprocess"


def test_testy_uruchamiaja_podprocesy_z_kodem_projektu_z_cwd():
    assert _tylko(PODPROCES_BEZ_CWD) == {}, "podproces z tools/ albo -m runner ma cwd=KORZEN"


def test_testy_nie_czekaja_bez_limitu_poza_jawnymi_wyjatkami():
    assert _tylko(CZEKANIE_BEZ_LIMITU) == {}, "kazde czekanie w tescie ma limit czasu"


def test_kazdy_wyjatek_od_czekania_bez_limitu_nadal_jest_potrzebny():
    zbedne = []
    for plik, funkcja in WYJATKI_CZEKANIA:
        surowe = _naruszenia_surowe((TESTY / plik).read_text(encoding="utf-8"))
        if (CZEKANIE_BEZ_LIMITU, funkcja) not in [(k, f) for _, k, f in surowe]:
            zbedne.append((plik, funkcja))

    assert zbedne == []
    assert all(WYJATKI_CZEKANIA.values())


# --- pomocnik `uruchom_i_zbierz` (zamiast subprocess.run) ----------------------------------------


def _python(kod: str, *argumenty: str) -> list[str]:
    return [sys.executable, "-c", kod, *argumenty]


def test_uruchom_i_zbierz_zwraca_kod_wyjscia_i_oba_strumienie_gdy_zbiera_wyjscie():
    wynik = procesy.uruchom_i_zbierz(
        _python("import sys; print('a'); print('b', file=sys.stderr); sys.exit(3)"),
        capture_output=True, text=True)

    assert (wynik.returncode, wynik.stdout, wynik.stderr) == (3, "a\n", "b\n")
    assert wynik.args == _python("import sys; print('a'); print('b', file=sys.stderr); sys.exit(3)")


def test_uruchom_i_zbierz_bez_zbierania_wyjscia_nie_ma_stdout_ani_stderr_w_wyniku():
    wynik = procesy.uruchom_i_zbierz(_python("pass"))

    assert (wynik.returncode, wynik.stdout, wynik.stderr) == (0, None, None)


def test_uruchom_i_zbierz_z_check_i_kodem_niezerowym_rzuca_blad_z_wyjsciem():
    argv = _python("import sys; print('o'); print('e', file=sys.stderr); sys.exit(4)")

    with pytest.raises(subprocess.CalledProcessError) as wyjatek:
        procesy.uruchom_i_zbierz(argv, check=True, capture_output=True, text=True)

    assert wyjatek.value.returncode == 4
    assert wyjatek.value.cmd == argv
    assert (wyjatek.value.stdout, wyjatek.value.stderr) == ("o\n", "e\n")


def test_uruchom_i_zbierz_z_check_i_kodem_zero_zwraca_wynik():
    assert procesy.uruchom_i_zbierz(_python("pass"), check=True).returncode == 0


def test_uruchom_i_zbierz_bez_check_nie_rzuca_przy_kodzie_niezerowym():
    assert procesy.uruchom_i_zbierz(_python("import sys; sys.exit(7)")).returncode == 7


def test_uruchom_i_zbierz_przekazuje_wejscie_na_stdin():
    wynik = procesy.uruchom_i_zbierz(
        _python("import sys; print(sys.stdin.read().upper())"),
        input="abc", capture_output=True, text=True)

    assert wynik.stdout == "ABC\n"


def test_uruchom_i_zbierz_przekazuje_cwd_i_srodowisko(tmp_path):
    wynik = procesy.uruchom_i_zbierz(
        _python("import os; print(os.getcwd()); print(os.environ['HAR242'])"),
        cwd=tmp_path, env={**os.environ, "HAR242": "tak"}, capture_output=True, text=True)

    assert wynik.stdout.split("\n")[:2] == [str(tmp_path.resolve()), "tak"]


def test_uruchom_i_zbierz_uruchamia_proces_w_nowej_grupie_procesow():
    wynik = procesy.uruchom_i_zbierz(
        _python("import os; print(os.getpgid(0))"), capture_output=True, text=True)

    assert int(wynik.stdout) != os.getpgid(0)


def test_uruchom_i_zbierz_po_przekroczeniu_limitu_rzuca_blad_i_zabija_proces(tmp_path):
    pidfile = tmp_path / "pid"
    kod = "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)"

    with pytest.raises(subprocess.TimeoutExpired):
        procesy.uruchom_i_zbierz(_python(kod, str(pidfile)), timeout=3)

    assert not czy_zyje(int(pidfile.read_text()))


def test_uruchom_i_zbierz_ma_domyslny_skonczony_limit_czasu():
    domyslny = inspect.signature(procesy.uruchom_i_zbierz).parameters["timeout"].default

    assert domyslny == procesy.LIMIT_DOMYSLNY_S == 120.0


# --- straznik procesow: limity jawnie wylaczone i helpery (iteracja poprawkowa HAR-242) -----------


@pytest.mark.parametrize(
    "zrodlo",
    [
        "uruchom_i_zbierz([sys.executable, 'tools/x.py'])\n",
        "uruchom_i_zbierz([sys.executable, '-m', 'runner'], capture_output=True)\n",
        "uruchom_i_zbierz([sys.executable, 'tools/x.py'], cwd=None)\n",
    ],
)
def test_straznik_wykrywa_uruchom_i_zbierz_z_kodem_projektu_bez_cwd(zrodlo):
    assert _naruszenia(zrodlo) == [(1, BEZ_CWD)]


def test_straznik_przepuszcza_uruchom_i_zbierz_z_cwd_albo_bez_kodu_projektu():
    assert _naruszenia("uruchom_i_zbierz([sys.executable, 'tools/x.py'], cwd=KORZEN)\n") == []
    assert _naruszenia("uruchom_i_zbierz(['git', 'status'])\n") == []


@pytest.mark.parametrize(
    "zrodlo",
    [
        "proc.wait(timeout=None)\n",
        "proc.wait(None)\n",
        "kolejka.get(timeout=None)\n",
        "kolejka.get(True)\n",
        "kolejka.get(block=True)\n",
        "kolejka.get(True, None)\n",
        "kolejka.get(block=True, timeout=None)\n",
        "import asyncio\nawait asyncio.wait(zadania)\n",
        "import asyncio\nawait asyncio.wait(zadania, timeout=None)\n",
        "import asyncio\nawait asyncio.wait(zadania, return_when=asyncio.FIRST_COMPLETED)\n",
        "from asyncio import wait\nawait wait(zadania)\n",
    ],
)
def test_straznik_wykrywa_czekanie_z_limitem_jawnie_wylaczonym_i_asyncio_wait_bez_timeout(zrodlo):
    wyniki = _naruszenia(zrodlo)

    assert [kod for _, kod in wyniki] == [BEZ_LIMITU]
    assert wyniki[0][0] == zrodlo.count("\n")


@pytest.mark.parametrize(
    "zrodlo",
    [
        "kolejka.get(block=False)\n",
        "kolejka.get(True, 5)\n",
        "kolejka.get(block=True, timeout=3)\n",
        "kolejka.get(False, None)\n",
        "slownik.get('k', 1)\n",
        "slownik.get(default=1)\n",
        "proc.wait(timeout=3)\n",
        "proc.wait(3)\n",
        "import asyncio\nawait asyncio.wait(zadania, timeout=3)\n",
        "import asyncio\nawait czekaj(asyncio.wait(zadania), 5, 'x')\n",
        "import asyncio\nawait asyncio.wait_for(asyncio.wait(zadania), 5)\n",
    ],
)
def test_straznik_przepuszcza_czekanie_z_prawdziwym_limitem_i_wywolania_nie_blokujace(zrodlo):
    assert _naruszenia(zrodlo) == []


def test_straznik_zglasza_czekanie_w_asyncio_wait_z_timeout_none():
    zrodlo = "import asyncio\nawait asyncio.wait([zdarzenie.wait()], timeout=None)\n"

    assert _naruszenia(zrodlo) == [(2, BEZ_LIMITU)]
