"""HAR-142: wspolny helper procesow w testach (`tests/procesy.py`).

Retro M1.3 (HAR-35): sprzatanie `killpg(getpgid(pid))` pod mutantem bez `start_new_session`
zabijalo grupe pytesta/mutmuta/agenta CI. Helper zabija wylacznie grupe, ktora sam
utworzyl, a kazde oczekiwanie ma twardy limit. Testy NIGDY nie wysylaja prawdziwego
sygnalu do grupy testu: tam, gdzie badamy gałąź "wlasna grupa", `os.killpg` jest
podmieniony na rejestrator.
"""
from __future__ import annotations

import asyncio
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
