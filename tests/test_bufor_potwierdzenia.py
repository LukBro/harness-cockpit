"""HAR-203: potwierdzenie zdarzenia nie przepisuje całego bufora.

Potwierdzenia trafiają do dziennika `<run_id>.ack` obok pliku zdarzeń `<run_id>.ndjson`;
plik zdarzeń jest przepisywany dopiero, gdy potwierdzonych jest co najmniej tyle, ile
czekających. Skutek czytamy z plików bufora i z bazy kokpitu, awarie wstrzykujemy na
poziomie `os`, a zabicie procesu to `os._exit` procesu potomnego w ustalonym punkcie.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api.app import app
from cockpit.db import session as db_session
from cockpit.db.models import Base, Event, Run, Runner, RunStatus, Tenant
from procesy import czekaj, uruchom_i_zbierz
from runner.bufor import BladBufora, BrakMiejsca, Bufor
from runner.client import Runner as RunnerKlienta

KORZEN = Path(__file__).resolve().parent.parent
BEZ_ROOTA = pytest.mark.skipif(os.geteuid() == 0, reason="root omija prawa dostepu do plikow")


def _zd(event_id: str, run_id: str = "run-1", text: str = "x") -> dict:
    return {"run_id": run_id, "event_id": event_id, "step_id": "s0", "kind": "output",
            "text": text}


def _linie_zdarzen(*identyfikatory: str, text: str = "x") -> bytes:
    return "".join(json.dumps(_zd(i, text=text)) + "\n" for i in identyfikatory).encode()


def _bufor(*identyfikatory: str) -> Bufor:
    bufor = Bufor()
    for identyfikator in identyfikatory:
        bufor.dopisz(_zd(identyfikator))
    return bufor


def _plik(run_id: str = "run-1") -> Path:
    return Bufor().katalog / f"{run_id}.ndjson"


def _dziennik(run_id: str = "run-1") -> Path:
    return Bufor().katalog / f"{run_id}.ack"


def _czekajace() -> list[str]:
    """Co zobaczylby runner po restarcie: swieza instancja czyta wylacznie dysk."""
    return [z["event_id"] for z in Bufor().niepotwierdzone()]


def _odcisk(plik: Path) -> tuple[int, int, int]:
    s = plik.stat()
    return s.st_ino, s.st_mtime_ns, s.st_size


def _w_pliku(plik: Path) -> list[str]:
    return [json.loads(linia)["event_id"] for linia in plik.read_text().splitlines()]


@contextmanager
def _bez_praw(plik: Path):
    plik.chmod(0o000)
    try:
        yield
    finally:
        plik.chmod(0o600)


# --- koszt potwierdzenia -------------------------------------------------------

def test_potwierdzenie_jednego_z_tysiaca_zdarzen_nie_przepisuje_pliku_z_zaleglosciami():
    identyfikatory = [f"e{n}" for n in range(1000)]
    Bufor().katalog.mkdir(parents=True)
    _plik().write_bytes(_linie_zdarzen(*identyfikatory))
    przed = _odcisk(_plik())

    Bufor().potwierdz("run-1", "e500")

    assert _odcisk(_plik()) == przed
    assert _czekajace() == [i for i in identyfikatory if i != "e500"]


def _bajty_wejscia_wyjscia() -> int:
    pola = dict(linia.split(": ") for linia in Path("/proc/self/io").read_text().splitlines())
    return int(pola["rchar"]) + int(pola["wchar"])


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc/self/io tylko na Linuksie")
def test_czterokrotnie_wieksze_zaleglosci_to_okolo_czterokrotnie_wiecej_odczytu_i_zapisu():
    def koszt(run_id: str, ile: int) -> int:
        identyfikatory = [f"e{n}" for n in range(ile)]
        bufor = Bufor()
        bufor.katalog.mkdir(parents=True, exist_ok=True)
        _plik(run_id).write_text("".join(
            json.dumps(_zd(i, run_id, "x" * 200)) + "\n" for i in identyfikatory))
        przed = _bajty_wejscia_wyjscia()
        for identyfikator in identyfikatory:
            bufor.potwierdz(run_id, identyfikator)
        koszt_bajtow = _bajty_wejscia_wyjscia() - przed
        assert _czekajace() == []
        return koszt_bajtow

    maly = koszt("run-maly", 200)
    duzy = koszt("run-duzy", 800)

    # liniowo: okolo 4x; kwadratowo: okolo 16x
    assert duzy < 8 * maly, (maly, duzy)


# --- prog przepisania pliku ----------------------------------------------------

def test_przy_dwoch_zdarzeniach_pierwsze_potwierdzenie_od_razu_przepisuje_plik_bez_dziennika():
    bufor = _bufor("e1", "e2")

    bufor.potwierdz("run-1", "e1")

    assert _w_pliku(_plik()) == ["e2"]
    assert not _dziennik().exists()


def test_plik_jest_przepisywany_dopiero_gdy_potwierdzonych_jest_tyle_ile_czekajacych():
    bufor = _bufor("e1", "e2", "e3", "e4")
    przed = _odcisk(_plik())

    bufor.potwierdz("run-1", "e1")

    assert _odcisk(_plik()) == przed
    assert _dziennik().exists()
    assert _czekajace() == ["e2", "e3", "e4"]

    bufor.potwierdz("run-1", "e2")

    assert _w_pliku(_plik()) == ["e3", "e4"]
    assert not _dziennik().exists()


def test_potwierdzen_mniej_niz_czekajacych_zostawia_plik_a_przewaga_potwierdzonych_go_przepisuje():
    bufor = _bufor("e1", "e2", "e3", "e4", "e5")
    przed = _odcisk(_plik())

    bufor.potwierdz("run-1", "e1")
    bufor.potwierdz("run-1", "e2")

    assert _odcisk(_plik()) == przed
    assert _czekajace() == ["e3", "e4", "e5"]

    bufor.potwierdz("run-1", "e3")

    assert _w_pliku(_plik()) == ["e4", "e5"]
    assert not _dziennik().exists()


def test_potwierdzenia_sprzed_restartu_runnera_licza_sie_do_progu_przepisania():
    _bufor("e1", "e2", "e3", "e4").potwierdz("run-1", "e1")

    Bufor().potwierdz("run-1", "e2")

    assert _w_pliku(_plik()) == ["e3", "e4"]
    assert not _dziennik().exists()


def test_po_potwierdzeniu_wszystkich_zdarzen_nie_zostaje_ani_plik_ani_dziennik():
    bufor = _bufor("e1", "e2", "e3", "e4", "e5")

    for identyfikator in ("e1", "e2", "e3", "e4", "e5"):
        bufor.potwierdz("run-1", identyfikator)

    assert list(bufor.katalog.iterdir()) == []
    bufor.dopisz(_zd("e1", text="drugi raz"))
    assert _czekajace() == ["e1"]


# --- dziennik potwierdzen ------------------------------------------------------

def test_dziennik_potwierdzen_ma_jedna_linie_z_samym_identyfikatorem_na_potwierdzenie():
    bufor = _bufor("e1", "e2", "e3", "e4", "e5")

    bufor.potwierdz("run-1", "e1")
    assert _dziennik().read_bytes() == b'"e1"\n'

    bufor.potwierdz("run-1", "e3")
    assert _dziennik().read_bytes() == b'"e1"\n"e3"\n'
    assert _czekajace() == ["e2", "e4", "e5"]


def test_identyfikator_ze_znakiem_nowej_linii_i_cudzyslowem_daje_sie_potwierdzic():
    dziwny = 'zdarzenie\n"żółte" '
    bufor = _bufor(dziwny, "e2", "e3")

    bufor.potwierdz("run-1", dziwny)

    assert _czekajace() == ["e2", "e3"]
    assert _dziennik().read_bytes() == (json.dumps(dziwny) + "\n").encode()


def test_potwierdzenie_w_dzienniku_jednego_runu_nie_ukrywa_zdarzenia_innego_runu():
    bufor = _bufor("wspolny", "e2", "e3")
    for identyfikator in ("wspolny", "f2", "f3"):
        bufor.dopisz(_zd(identyfikator, "run-2"))

    bufor.potwierdz("run-1", "wspolny")

    assert [(z["run_id"], z["event_id"]) for z in Bufor().niepotwierdzone()] == [
        ("run-1", "e2"), ("run-1", "e3"),
        ("run-2", "wspolny"), ("run-2", "f2"), ("run-2", "f3")]


@pytest.mark.parametrize("prawa_przed", [None, 0o644], ids=["nowy", "zbyt_otwarty"])
def test_dziennik_potwierdzen_ma_prawa_tylko_dla_wlasciciela(prawa_przed):
    bufor = _bufor("e1", "e2", "e3")
    if prawa_przed is not None:
        _dziennik().write_bytes(b"")
        _dziennik().chmod(prawa_przed)
    stara = os.umask(0o000)
    try:
        bufor.potwierdz("run-1", "e1")
    finally:
        os.umask(stara)

    assert stat.S_IMODE(_dziennik().stat().st_mode) == 0o600


# --- powtorki i nieznane identyfikatory ----------------------------------------

@pytest.mark.parametrize("druga_instancja", [False, True], ids=["ta_sama", "po_restarcie"])
def test_powtorne_potwierdzenie_tego_samego_zdarzenia_nie_liczy_sie_dwa_razy(druga_instancja):
    bufor = _bufor("e1", "e2", "e3")
    przed = _odcisk(_plik())
    bufor.potwierdz("run-1", "e1")

    (Bufor() if druga_instancja else bufor).potwierdz("run-1", "e1")

    assert _odcisk(_plik()) == przed
    assert _dziennik().read_bytes() == b'"e1"\n'
    assert _czekajace() == ["e2", "e3"]


def test_potwierdzenie_nieznanego_zdarzenia_nie_tworzy_dziennika_ani_nie_liczy_sie_do_progu():
    bufor = _bufor("e1", "e2", "e3")
    przed = _odcisk(_plik())

    bufor.potwierdz("run-1", "nie-ma-1")
    assert not _dziennik().exists()

    bufor.potwierdz("run-1", "e1")
    bufor.potwierdz("run-1", "nie-ma-2")

    assert _odcisk(_plik()) == przed
    assert _dziennik().read_bytes() == b'"e1"\n'


def test_nieznane_zdarzenie_przy_zapamietanym_pliku_nie_trafia_do_dziennika():
    # 5 zdarzen: nawet liczone z nieznanym nie dobija do progu, wiec nic dalej go nie odrzuci
    bufor = _bufor("e1", "e2", "e3", "e4", "e5")
    bufor.potwierdz("run-1", "e1")
    przed = _odcisk(_plik())

    bufor.potwierdz("run-1", "nie-ma")

    assert _dziennik().read_bytes() == b'"e1"\n'
    assert _odcisk(_plik()) == przed


def test_potwierdzenie_przy_osieroconym_dzienniku_bez_pliku_zdarzen_nic_nie_robi():
    Bufor().katalog.mkdir(parents=True)
    _dziennik().write_bytes(b'"e1"\n')

    Bufor().potwierdz("run-1", "e1")

    assert _czekajace() == []
    assert not _plik().exists()


def test_osierocone_potwierdzenia_po_przerwanym_sprzataniu_nie_licza_sie_do_progu():
    bufor = _bufor("e3", "e4", "e5", "e6")
    _dziennik().write_bytes(b'"e1"\n"e2"\n')
    przed = _odcisk(_plik())

    bufor.potwierdz("run-1", "e3")

    assert _odcisk(_plik()) == przed
    assert _czekajace() == ["e4", "e5", "e6"]


# --- uszkodzony dziennik --------------------------------------------------------

@pytest.mark.parametrize(
    "uszkodzona",
    [b'{"run_id": "run-1", "event_id": "e1"}', b"42", b"null", b'["e1"]', b"e1", b"",
     b"[" * 100_000, b'"e1'],
    ids=["obiekt", "liczba", "null", "tablica", "bez_cudzyslowow", "pusta",
         "zbyt_gleboka", "obcieta"],
)
def test_uszkodzona_linia_dziennika_jest_pomijana_a_zdarzenie_czeka_na_ponowna_wysylke(uszkodzona):
    _bufor("e1", "e2", "e3")
    # obcieta linia jest ostatnia (po awarii); po kazdej innej czytnik musi czytac dalej
    po_niej = b"" if uszkodzona == b'"e1' else b'\n"e3"\n'
    _dziennik().write_bytes(b'"e2"\n' + uszkodzona + po_niej)

    assert _czekajace() == (["e1", "e3"] if po_niej == b"" else ["e1"])


def test_potwierdzenie_dopisane_po_obcietej_linii_dziennika_nie_zlewa_sie_z_nia():
    bufor = _bufor("e1", "e2", "e3", "e4", "e5")
    _dziennik().write_bytes(b'"e1')

    bufor.potwierdz("run-1", "e2")

    assert _czekajace() == ["e1", "e3", "e4", "e5"]


# --- awaria miedzy krokami sprzatania -------------------------------------------

class _Przerwanie(BaseException):
    """Udaje smierc procesu: nie jest `OSError`, wiec kod bufora go nie obsluguje."""


def _przerwij_po_pierwszym_kroku_sprzatania(monkeypatch) -> None:
    def po_wykonaniu(prawdziwa, *koncowki):
        def podstawka(*args, **kwargs):
            wynik = prawdziwa(*args, **kwargs)
            if str(args[-1]).endswith(koncowki):
                raise _Przerwanie
            return wynik
        return podstawka

    for nazwa in ("replace", "unlink", "remove"):
        monkeypatch.setattr(os, nazwa, po_wykonaniu(getattr(os, nazwa), ".ndjson", ".ack"))


def test_przerwanie_w_polowie_przepisywania_pliku_nie_przywraca_potwierdzonych_zdarzen(
    monkeypatch,
):
    bufor = _bufor("e1", "e2", "e3", "e4")
    bufor.potwierdz("run-1", "e1")
    _przerwij_po_pierwszym_kroku_sprzatania(monkeypatch)

    with pytest.raises(_Przerwanie):
        bufor.potwierdz("run-1", "e2")

    assert _czekajace() == ["e3", "e4"]


def test_przy_komplecie_potwierdzen_dziennik_znika_przed_plikiem_zdarzen(monkeypatch):
    # odwrotna kolejnosc zostawilaby dziennik, ktory polknalby nowe zdarzenie o tym samym id
    bufor = _bufor("e1", "e2")
    _dziennik().write_bytes(b'"e1"\n')
    _przerwij_po_pierwszym_kroku_sprzatania(monkeypatch)

    with pytest.raises(_Przerwanie):
        bufor.potwierdz("run-1", "e2")

    assert not _dziennik().exists()
    assert _plik().exists()
    Bufor().dopisz(_zd("e1", text="nowe"))
    assert "nowe" in [z["text"] for z in Bufor().niepotwierdzone()]


# --- cudza instancja: nieaktualna pamiec niczego nie gubi ------------------------

def test_zdarzenie_dopisane_przez_inna_instancje_przezywa_przepisanie_pliku():
    a = _bufor("e1", "e2", "e3")
    a.potwierdz("run-1", "e1")
    Bufor().dopisz(_zd("e4"))

    a.potwierdz("run-1", "e2")

    assert _plik().read_bytes() == _linie_zdarzen("e3", "e4")
    assert not _dziennik().exists()
    assert _czekajace() == ["e3", "e4"]


def test_zdarzenie_dopisane_przez_inna_instancje_daje_sie_potwierdzic():
    a = _bufor("e1", "e2", "e3", "e4", "e5")
    a.potwierdz("run-1", "e1")
    Bufor().dopisz(_zd("e6"))

    a.potwierdz("run-1", "e6")

    assert _plik().read_bytes() == _linie_zdarzen("e1", "e2", "e3", "e4", "e5", "e6")
    assert _dziennik().read_bytes() == b'"e1"\n"e6"\n'
    assert _czekajace() == ["e2", "e3", "e4", "e5"]


def test_potwierdzenia_innej_instancji_sa_uwzgledniane_przy_przepisaniu_pliku():
    a = _bufor("e1", "e2", "e3", "e4", "e5", "e6")
    a.potwierdz("run-1", "e1")
    Bufor().potwierdz("run-1", "e2")
    a.potwierdz("run-1", "e3")
    assert _dziennik().read_bytes() == b'"e1"\n"e2"\n"e3"\n'

    a.potwierdz("run-1", "e4")                  # tu prog; instancja `a` nie widziala e2

    assert _plik().read_bytes() == _linie_zdarzen("e5", "e6")
    assert not _dziennik().exists()
    assert _czekajace() == ["e5", "e6"]


def test_po_przepisaniu_pliku_przez_inna_instancje_dawne_potwierdzenia_nie_licza_sie_do_progu():
    a = _bufor("e1", "e2", "e3", "e4", "e5", "e6")
    a.potwierdz("run-1", "e1")
    a.potwierdz("run-1", "e2")
    Bufor().potwierdz("run-1", "e3")
    assert _w_pliku(_plik()) == ["e4", "e5", "e6"]
    przed = _odcisk(_plik())

    a.potwierdz("run-1", "e4")

    assert _odcisk(_plik()) == przed
    assert _dziennik().read_bytes() == b'"e4"\n'
    assert _czekajace() == ["e5", "e6"]


def _podmien_plik(tresc: bytes) -> None:
    nowy = _plik().with_name("nowy")
    nowy.write_bytes(tresc)
    os.replace(nowy, _plik())


def test_plik_podmieniony_na_wiekszy_jest_czytany_od_nowa_przy_liczeniu_progu():
    a = _bufor("e1", "e2", "e3", "e4", "e5", "e6")
    a.potwierdz("run-1", "e1")
    rozmiar = _plik().stat().st_size
    _podmien_plik(_linie_zdarzen("e2", "e3", text="x" * 1000))
    assert _plik().stat().st_size > rozmiar

    a.potwierdz("run-1", "e2")

    assert _w_pliku(_plik()) == ["e3"]
    assert not _dziennik().exists()


def test_plik_skrocony_w_miejscu_jest_czytany_od_nowa_przy_liczeniu_progu():
    a = _bufor("e1", "e2", "e3", "e4", "e5", "e6")
    a.potwierdz("run-1", "e1")
    _plik().write_bytes(_linie_zdarzen("e2", "e3"))      # ten sam inode, mniejszy rozmiar

    a.potwierdz("run-1", "e2")

    assert _w_pliku(_plik()) == ["e3"]
    assert not _dziennik().exists()


def test_zdarzenie_nieznane_po_doczytaniu_konca_pliku_jest_szukane_w_calym_pliku():
    a = _bufor("e1", "e2", "e3", "e4")
    a.potwierdz("run-1", "e1")
    # ten sam inode i wiekszy rozmiar: nowe wpisy leza przed miejscem, do ktorego doczytano
    _plik().write_bytes(_linie_zdarzen("n1", "n2", "n3", "n4", "n5", "n6", "n7"))

    a.potwierdz("run-1", "n1")

    assert _plik().read_bytes() == _linie_zdarzen("n1", "n2", "n3", "n4", "n5", "n6", "n7")
    assert _dziennik().read_bytes() == b'"e1"\n"n1"\n'
    assert _czekajace() == ["n2", "n3", "n4", "n5", "n6", "n7"]


def test_nieaktualna_pamiec_nie_przepisuje_pliku_kosztem_nieznanych_jej_zdarzen():
    a = _bufor("e1", "e2", "e3")
    a.potwierdz("run-1", "e1")
    _plik().write_bytes(_linie_zdarzen("e2", "n1", "n2", "n3", text="x" * 500))

    a.potwierdz("run-1", "e2")

    assert _plik().read_bytes() == _linie_zdarzen("e2", "n1", "n2", "n3", text="x" * 500)
    assert _dziennik().read_bytes() == b'"e1"\n"e2"\n'
    assert _czekajace() == ["n1", "n2", "n3"]


def test_nieaktualna_pamiec_nie_usuwa_pliku_z_nieznanym_jej_zdarzeniem():
    a = _bufor("e1")
    a.potwierdz("run-1", "nie-ma")                       # instancja zna tylko e1
    _plik().write_bytes(_linie_zdarzen("e1", "n1", text="x" * 500))

    a.potwierdz("run-1", "e1")

    assert _plik().read_bytes() == _linie_zdarzen("n1", text="x" * 500)
    assert not _dziennik().exists()
    assert _czekajace() == ["n1"]


def _przepisz_niezauwazalnie(tresc: bytes) -> None:
    """Ten sam inode, rozmiar i mtime: `fstat` nie pokaze zmiany, pokaze ja tylko odczyt."""
    przed = _plik().stat()
    assert len(tresc) == przed.st_size
    _plik().write_bytes(tresc)
    os.utime(_plik(), ns=(przed.st_atime_ns, przed.st_mtime_ns))


def test_o_przepisaniu_pliku_decyduje_zawartosc_dysku_a_nie_pamiec_instancji():
    a = _bufor("e1", "e2", "e3")
    a.potwierdz("run-1", "e1")
    _przepisz_niezauwazalnie(_linie_zdarzen("e1", "e2", "n3"))

    a.potwierdz("run-1", "e2")

    assert _plik().read_bytes() == _linie_zdarzen("n3")
    assert not _dziennik().exists()
    assert _czekajace() == ["n3"]


def test_o_usunieciu_pliku_decyduje_zawartosc_dysku_a_nie_pamiec_instancji():
    a = _bufor("e1")
    a.potwierdz("run-1", "nie-ma")                       # instancja zna tylko e1
    _przepisz_niezauwazalnie(_linie_zdarzen("n1"))

    a.potwierdz("run-1", "e1")

    assert _plik().read_bytes() == _linie_zdarzen("n1")
    assert not _dziennik().exists()
    assert _czekajace() == ["n1"]


def test_prog_policzony_ponownie_z_dysku_wstrzymuje_przepisanie_pliku():
    a = _bufor("e1", "e2", "e3")
    a.potwierdz("run-1", "e1")
    _przepisz_niezauwazalnie(_linie_zdarzen("e2", "n1", "n2"))
    przed = _odcisk(_plik())

    a.potwierdz("run-1", "e2")           # wg pamieci 2 z 3, wg dysku 1 z 3

    assert _odcisk(_plik()) == przed
    assert _dziennik().read_bytes() == b'"e1"\n"e2"\n'


def test_zdarzenie_dopisywane_w_kawalkach_daje_sie_potwierdzic_po_dokonczeniu_linii():
    a = _bufor("e1", "e2", "e3", "e4", "e5")
    a.potwierdz("run-1", "e1")
    linia = _linie_zdarzen("e6")
    with _plik().open("ab") as f:
        f.write(linia[:20])
    a.potwierdz("run-1", "e2")
    with _plik().open("ab") as f:
        f.write(linia[20:])

    a.potwierdz("run-1", "e6")

    assert _plik().read_bytes() == _linie_zdarzen("e3", "e4", "e5")
    assert not _dziennik().exists()
    assert _czekajace() == ["e3", "e4", "e5"]


def test_linia_ucieta_w_srodku_polskiej_litery_nie_psuje_potwierdzen_i_daje_sie_dokonczyc():
    a = _bufor("e1", "e2", "e3", "e4", "e5")
    a.potwierdz("run-1", "e1")
    linia = (json.dumps(_zd("e6", text="żółw"), ensure_ascii=False) + "\n").encode("utf-8")
    ciecie = linia.index("ż".encode()) + 1               # miedzy dwoma bajtami litery
    with _plik().open("ab") as f:
        f.write(linia[:ciecie])

    a.potwierdz("run-1", "e2")

    assert _dziennik().read_bytes() == b'"e1"\n"e2"\n'
    with _plik().open("ab") as f:
        f.write(linia[ciecie:])

    a.potwierdz("run-1", "e6")

    assert _plik().read_bytes() == _linie_zdarzen("e3", "e4", "e5")
    assert not _dziennik().exists()


def test_linia_ucieta_przy_pierwszym_odczycie_liczy_sie_po_dokonczeniu_do_progu_przepisania():
    a = _bufor("e1", "e2", "e3", "e4", "e5", "e6")
    linia = _linie_zdarzen("e7")
    with _plik().open("ab") as f:
        f.write(linia[:20])
    a.potwierdz("run-1", "e1")           # pierwszy pelny odczyt widzi ucieta linie
    with _plik().open("ab") as f:
        f.write(linia[20:])
    a.potwierdz("run-1", "e2")
    Bufor().potwierdz("run-1", "e3")

    a.potwierdz("run-1", "e4")           # wg `a` 3 potwierdzone z 7: ponizej progu

    assert _plik().read_bytes() == _linie_zdarzen("e1", "e2", "e3", "e4", "e5", "e6", "e7")
    assert _dziennik().read_bytes() == b'"e1"\n"e2"\n"e3"\n"e4"\n'


# --- ten sam inode wraca po zniknieciu albo przepisaniu pliku ---------------------

def test_po_zniknieciu_pliku_instancja_zapomina_dawne_potwierdzenia():
    a = _bufor("e1", "e2", "e3", "e4", "e5")
    a.potwierdz("run-1", "e1")
    kopia = a.katalog / "kopia"
    os.link(_plik(), kopia)              # twarde dowiazanie trzyma inode
    _plik().unlink()
    _dziennik().unlink()
    a.potwierdz("run-1", "e2")           # pelny odczyt stwierdza brak pliku
    os.link(kopia, _plik())

    a.potwierdz("run-1", "e1")

    assert _dziennik().read_bytes() == b'"e1"\n'
    assert _plik().read_bytes() == _linie_zdarzen("e1", "e2", "e3", "e4", "e5")


def test_po_przepisaniu_pliku_instancja_zapomina_dawne_potwierdzenia():
    a = _bufor("e1", "e2", "e3", "e4", "e5")
    a.potwierdz("run-1", "e1")
    a.potwierdz("run-1", "e2")
    kopia = a.katalog / "kopia"
    os.link(_plik(), kopia)
    a.potwierdz("run-1", "e3")
    assert _w_pliku(_plik()) == ["e4", "e5"]
    os.replace(kopia, _plik())           # wraca plik sprzed przepisania, z tym samym inode

    a.potwierdz("run-1", "e1")

    assert _dziennik().read_bytes() == b'"e1"\n'
    assert _plik().read_bytes() == _linie_zdarzen("e1", "e2", "e3", "e4", "e5")


# --- plik zdarzen znika albo psuje sie pod instancja, ktora go pamieta -----------

def test_potwierdzenie_po_usunieciu_pliku_przez_inna_instancje_konczy_sie_bez_bledu():
    a = _bufor("e1", "e2", "e3")
    a.potwierdz("run-1", "e1")
    Bufor().potwierdz("run-1", "e2")
    Bufor().potwierdz("run-1", "e3")
    assert not _plik().exists()

    a.potwierdz("run-1", "e2")

    assert list(a.katalog.iterdir()) == []


def test_linia_spoza_utf8_dopisana_do_zapamietanego_pliku_to_blad_bufora():
    a = _bufor("e1", "e2", "e3")
    a.potwierdz("run-1", "e1")
    with _plik().open("ab") as f:
        f.write(b"\xff\n")

    with pytest.raises(BladBufora) as wyjatek:
        a.potwierdz("run-1", "e2")

    assert str(wyjatek.value) == "odczyt run-1.ndjson: UnicodeDecodeError"
    assert _dziennik().read_bytes() == b'"e1"\n'


def test_katalog_w_miejscu_zapamietanego_pliku_zdarzen_to_blad_bufora():
    # blad systemu przy doczytywaniu, ktory wystepuje takze dla roota
    a = _bufor("e1", "e2", "e3")
    a.potwierdz("run-1", "e1")
    _plik().unlink()
    _plik().mkdir()

    with pytest.raises(BladBufora) as wyjatek:
        a.potwierdz("run-1", "e2")

    assert str(wyjatek.value) == "odczyt run-1.ndjson: IsADirectoryError"


@pytest.mark.parametrize("rodzaj, nazwa_bledu",
                         [("katalog", "IsADirectoryError"), ("nie_utf8", "UnicodeDecodeError")])
def test_nieczytelny_plik_zdarzen_przy_pierwszym_potwierdzeniu_to_blad_bufora(
    rodzaj, nazwa_bledu,
):
    _bufor("e1", "e2", "e3")
    if rodzaj == "katalog":
        _plik().unlink()
        _plik().mkdir()
    else:
        with _plik().open("ab") as f:
            f.write(b"\xff\n")

    with pytest.raises(BladBufora) as wyjatek:
        Bufor().potwierdz("run-1", "e1")     # po restarcie: pelny odczyt, bez pamieci

    assert str(wyjatek.value) == f"odczyt run-1.ndjson: {nazwa_bledu}"
    assert not _dziennik().exists()


@pytest.mark.parametrize("po_odczycie", [None, ("n1",)], ids=["znika", "bez_tego_zdarzenia"])
def test_zmiana_pliku_tuz_przed_przepisaniem_konczy_potwierdzenie_bez_zapisu(
    monkeypatch, po_odczycie,
):
    _bufor("e1", "e2")
    bufor = Bufor()
    prawdziwy = bufor._wczytaj
    po_zmianie: list = []

    def wczytaj_i_zmien(plik):
        wynik = prawdziwy(plik)
        if not po_zmianie:
            if po_odczycie is None:
                plik.unlink()
                po_zmianie.append(None)
            else:
                _podmien_plik(_linie_zdarzen(*po_odczycie))
                po_zmianie.append(_odcisk(plik))
        return wynik

    monkeypatch.setattr(bufor, "_wczytaj", wczytaj_i_zmien)

    bufor.potwierdz("run-1", "e1")

    assert po_zmianie, "zmiana miedzy odczytami nie zostala wstrzyknieta"
    assert not _dziennik().exists()
    if po_odczycie is None:
        assert list(bufor.katalog.iterdir()) == []
    else:
        assert [_odcisk(_plik())] == po_zmianie
        assert _czekajace() == ["n1"]


@pytest.mark.parametrize(
    "kod, klasa, nazwa_bledu",
    [(errno.ENOSPC, BrakMiejsca, "OSError"), (errno.EACCES, BladBufora, "PermissionError")],
    ids=["brak_miejsca", "brak_praw"],
)
def test_nieudane_sprzatanie_pliku_tymczasowego_nie_zaslania_bledu_przepisania(
    monkeypatch, kod, klasa, nazwa_bledu,
):
    bufor = _bufor("e1", "e2")
    prawdziwy_unlink = os.unlink
    proby_sprzatania: list[str] = []

    def podmien(zrodlo, cel):
        raise OSError(kod, os.strerror(kod))

    def usun(sciezka, *args, **kwargs):
        if str(sciezka).endswith(".tmp"):
            proby_sprzatania.append(str(sciezka))
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        prawdziwy_unlink(sciezka, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(os, "replace", podmien)
        m.setattr(os, "unlink", usun)
        m.setattr(os, "remove", usun)

        with pytest.raises(BladBufora) as wyjatek:
            bufor.potwierdz("run-1", "e1")

    assert len(proby_sprzatania) == 1
    assert type(wyjatek.value) is klasa
    assert str(wyjatek.value) == f"zapis run-1.ndjson: {nazwa_bledu}"
    assert wyjatek.value.__cause__.errno == kod
    assert _czekajace() == ["e1", "e2"]


# --- bledy dysku na sciezce dziennika --------------------------------------------

def _awaria_zapisu_dziennika(monkeypatch, miejsce: str, kod: int) -> list[str]:
    """Awaria dopisania do `*.ack`: `open` (do zapisu), `write` (po polowie danych) albo
    `fsync`. Zwraca liste miejsc, w ktorych awaria faktycznie wystapila."""
    prawdziwy_open, prawdziwy_write, prawdziwy_close = os.open, os.write, os.close
    prawdziwy_fsync = os.fsync
    deskryptory: set[int] = set()
    awarie: list[str] = []

    def padnij():
        awarie.append(miejsce)
        raise OSError(kod, os.strerror(kod))

    def otworz(sciezka, flagi, *args, **kwargs):
        do_zapisu = str(sciezka).endswith(".ack") and flagi & (os.O_WRONLY | os.O_RDWR)
        if do_zapisu and miejsce == "open":
            padnij()
        fd = prawdziwy_open(sciezka, flagi, *args, **kwargs)
        if do_zapisu:
            deskryptory.add(fd)
        return fd

    def zamknij(fd):
        deskryptory.discard(fd)
        prawdziwy_close(fd)

    def zapisz(fd, dane):
        if fd in deskryptory and miejsce == "write":
            prawdziwy_write(fd, dane[: len(dane) // 2])
            padnij()
        return prawdziwy_write(fd, dane)

    def synchronizuj(fd):
        if fd in deskryptory and miejsce == "fsync":
            padnij()
        prawdziwy_fsync(fd)

    monkeypatch.setattr(os, "open", otworz)
    monkeypatch.setattr(os, "close", zamknij)
    monkeypatch.setattr(os, "write", zapisz)
    monkeypatch.setattr(os, "fsync", synchronizuj)
    return awarie


@pytest.mark.parametrize(
    "kod, klasa, nazwa_bledu",
    [(errno.ENOSPC, BrakMiejsca, "OSError"), (errno.EACCES, BladBufora, "PermissionError")],
    ids=["brak_miejsca", "brak_praw"],
)
@pytest.mark.parametrize("miejsce", ["open", "write", "fsync"])
def test_nieudane_zapisanie_potwierdzenia_to_blad_bufora_a_zdarzenie_dalej_czeka(
    monkeypatch, miejsce, kod, klasa, nazwa_bledu,
):
    bufor = _bufor("e1", "e2", "e3", "e4", "e5")
    bufor.potwierdz("run-1", "e1")
    przed = _odcisk(_plik())
    with monkeypatch.context() as m:
        awarie = _awaria_zapisu_dziennika(m, miejsce, kod)

        with pytest.raises(BladBufora) as wyjatek:
            bufor.potwierdz("run-1", "e2")

    assert awarie == [miejsce]
    assert type(wyjatek.value) is klasa
    assert str(wyjatek.value) == f"zapis run-1.ack: {nazwa_bledu}"
    assert wyjatek.value.__cause__.errno == kod
    assert _dziennik().read_bytes() == b'"e1"\n'
    assert _czekajace() == ["e2", "e3", "e4", "e5"]

    bufor.potwierdz("run-1", "e2")              # ponowienie: nieudane nie zostalo w pamieci

    assert _dziennik().read_bytes() == b'"e1"\n"e2"\n'
    assert _odcisk(_plik()) == przed
    assert _czekajace() == ["e3", "e4", "e5"]


def test_nieudane_obciecie_dziennika_nie_zaslania_braku_miejsca_i_nie_psuje_nastepnych(
    monkeypatch,
):
    bufor = _bufor("e1", "e2", "e3", "e4", "e5")
    with monkeypatch.context() as m:
        _awaria_zapisu_dziennika(m, "write", errno.ENOSPC)

        def obetnij(fd, rozmiar):
            raise OSError(errno.EIO, os.strerror(errno.EIO))

        m.setattr(os, "ftruncate", obetnij)

        with pytest.raises(BrakMiejsca) as wyjatek:
            bufor.potwierdz("run-1", "e1")

    assert wyjatek.value.__cause__.errno == errno.ENOSPC
    bufor.potwierdz("run-1", "e1")
    assert _czekajace() == ["e2", "e3", "e4", "e5"]


@pytest.mark.parametrize(
    "kod, klasa, nazwa_bledu",
    [(errno.ENOSPC, BrakMiejsca, "OSError"), (errno.EACCES, BladBufora, "PermissionError")],
    ids=["brak_miejsca", "brak_praw"],
)
def test_nieudane_usuniecie_dziennika_po_przepisaniu_pliku_to_blad_bufora_bez_utraty_zdarzen(
    monkeypatch, kod, klasa, nazwa_bledu,
):
    bufor = _bufor("e1", "e2", "e3", "e4")
    bufor.potwierdz("run-1", "e1")
    prawdziwy = os.unlink

    def usun(sciezka, *args, **kwargs):
        if str(sciezka).endswith(".ack"):
            raise OSError(kod, os.strerror(kod))
        prawdziwy(sciezka, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(os, "unlink", usun)
        m.setattr(os, "remove", usun)

        with pytest.raises(BladBufora) as wyjatek:
            bufor.potwierdz("run-1", "e2")

    assert type(wyjatek.value) is klasa
    assert str(wyjatek.value) == f"zapis run-1.ndjson: {nazwa_bledu}"
    assert _czekajace() == ["e3", "e4"]


@pytest.mark.parametrize(
    "kod, klasa, nazwa_bledu",
    [(errno.ENOSPC, BrakMiejsca, "OSError"), (errno.EACCES, BladBufora, "PermissionError")],
    ids=["brak_miejsca", "brak_praw"],
)
def test_nieudane_usuniecie_pliku_po_komplecie_potwierdzen_to_blad_bufora_a_plik_zostaje(
    monkeypatch, kod, klasa, nazwa_bledu,
):
    bufor = _bufor("e1")
    prawdziwy = os.unlink

    def usun(sciezka, *args, **kwargs):
        if str(sciezka).endswith(".ndjson"):
            raise OSError(kod, os.strerror(kod))
        prawdziwy(sciezka, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(os, "unlink", usun)
        m.setattr(os, "remove", usun)

        with pytest.raises(BladBufora) as wyjatek:
            bufor.potwierdz("run-1", "e1")

    assert type(wyjatek.value) is klasa
    assert str(wyjatek.value) == f"zapis run-1.ndjson: {nazwa_bledu}"
    assert _w_pliku(_plik()) == ["e1"]


def _zepsuj_dziennik(rodzaj: str) -> None:
    if rodzaj == "katalog":
        _dziennik().mkdir()
    else:
        _dziennik().write_bytes(b'\xff\xfe "e1"\n')


ZEPSUTE = [("katalog", "IsADirectoryError"), ("nie_utf8", "UnicodeDecodeError")]


@pytest.mark.parametrize("rodzaj, nazwa_bledu", ZEPSUTE)
@pytest.mark.parametrize("operacja", ["niepotwierdzone", "potwierdz"])
def test_nieczytelny_dziennik_to_blad_bufora_a_nie_wyjatek_biblioteki(
    operacja, rodzaj, nazwa_bledu,
):
    bufor = _bufor("e1", "e2", "e3")
    _zepsuj_dziennik(rodzaj)

    with pytest.raises(BladBufora) as wyjatek:
        if operacja == "potwierdz":
            Bufor().potwierdz("run-1", "e1")     # po restarcie: dziennik trzeba przeczytac
        else:
            bufor.niepotwierdzone()

    assert str(wyjatek.value) == f"odczyt run-1.ack: {nazwa_bledu}"
    assert _w_pliku(_plik()) == ["e1", "e2", "e3"]


@BEZ_ROOTA
@pytest.mark.parametrize("operacja", ["niepotwierdzone", "potwierdz"])
def test_dziennik_bez_praw_odczytu_to_blad_bufora(operacja):
    _bufor("e1", "e2", "e3", "e4").potwierdz("run-1", "e1")
    bufor = Bufor()

    with _bez_praw(_dziennik()), pytest.raises(BladBufora) as wyjatek:
        if operacja == "potwierdz":
            bufor.potwierdz("run-1", "e2")
        else:
            bufor.niepotwierdzone()

    assert str(wyjatek.value) == "odczyt run-1.ack: PermissionError"
    assert _w_pliku(_plik()) == ["e1", "e2", "e3", "e4"]


@BEZ_ROOTA
@pytest.mark.parametrize("sciezka", [_plik, _dziennik], ids=["plik_zdarzen", "dziennik"])
def test_utrata_praw_odczytu_przy_potwierdzeniu_to_blad_bufora_i_nic_nie_ginie(sciezka):
    bufor = _bufor("e1", "e2", "e3", "e4")
    bufor.potwierdz("run-1", "e1")

    with _bez_praw(sciezka()), pytest.raises(BladBufora) as wyjatek:
        bufor.potwierdz("run-1", "e2")

    assert str(wyjatek.value) == f"odczyt {sciezka().name}: PermissionError"
    assert _w_pliku(_plik()) == ["e1", "e2", "e3", "e4"]
    assert _dziennik().read_bytes() == b'"e1"\n'
    assert _czekajace() == ["e2", "e3", "e4"]


# --- zabicie procesu w trakcie wysylki: skutek w bazie kokpitu --------------------

TOKEN = "token-runnera-potwierdzen"
KOD_SMIERCI = 37

# Proces runnera: produkcyjne `dosylaj` do prawdziwej aplikacji na tej samej bazie;
# w ustalonym punkcie wykonuje prawdziwy krok i konczy sie bez sprzatania.
SKRYPT = """
import asyncio, os, sys
import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from cockpit.api.app import app
from cockpit.db import session as db_session
from runner.client import Runner

url, punkt = sys.argv[1], sys.argv[2]

def smierc():
    os._exit(37)

class Kokpit(httpx.ASGITransport):
    przyjete = 0

    async def handle_async_request(self, request):
        odpowiedz = await super().handle_async_request(request)
        assert odpowiedz.status_code == 200, odpowiedz.status_code
        Kokpit.przyjete += 1
        if punkt == "po_przyjeciu" and Kokpit.przyjete == 2:
            smierc()
        return odpowiedz

def po_wykonaniu(prawdziwa, warunek):
    def podstawka(*args, **kwargs):
        wynik = prawdziwa(*args, **kwargs)
        if warunek(args):
            smierc()
        return wynik
    return podstawka

dzienniki = set()
prawdziwy_open = os.open

def otworz(sciezka, *args, **kwargs):
    fd = prawdziwy_open(sciezka, *args, **kwargs)
    dzienniki.discard(fd)
    if str(sciezka).endswith(".ack"):
        dzienniki.add(fd)
    return fd

async def main():
    engine = create_async_engine(url)
    db_session.engine = engine
    db_session.SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    if punkt == "po_dopisaniu_potwierdzenia":
        os.open = otworz
        os.fsync = po_wykonaniu(os.fsync, lambda a: a[0] in dzienniki)
    elif punkt == "po_podmianie_pliku":
        os.replace = po_wykonaniu(os.replace, lambda a: str(a[1]).endswith(".ndjson"))
    elif punkt == "po_usunieciu_dziennika":
        os.unlink = po_wykonaniu(os.unlink, lambda a: str(a[0]).endswith(".ack"))
        os.remove = os.unlink
    try:
        async with httpx.AsyncClient(transport=Kokpit(app=app)) as klient:
            runner = Runner("http://test", os.environ["TOKEN_TESTU"])
            await asyncio.wait_for(runner.dosylaj(klient), 30)
    finally:
        await engine.dispose()

asyncio.run(main())
"""


@dataclass
class Swiat:
    url: str
    run_id: str


@pytest.fixture
async def swiat(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 't.db'}"
    engine = create_async_engine(url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with maker() as s:
        zespol = Tenant(name="Browarek")
        s.add(zespol)
        await s.flush()
        wlasciciel = Runner(
            tenant_id=zespol.id, name="wlasciciel", last_seen_at=datetime.now(timezone.utc),
            token_hash=hashlib.sha256(TOKEN.encode()).hexdigest())
        s.add(wlasciciel)
        await s.flush()
        run = Run(tenant_id=zespol.id, runner_id=wlasciciel.id, status=RunStatus.running)
        s.add(run)
        await s.commit()
        run_id = run.id
    try:
        yield Swiat(url, run_id)
    finally:
        await engine.dispose()


async def _identyfikatory_w_bazie(run_id: str) -> list[str]:
    async with db_session.SessionLocal() as s:
        return list((await s.execute(
            select(Event.event_id).where(Event.run_id == run_id).order_by(Event.seq)
        )).scalars().all())


class _KokpitZapisujacy(ASGITransport):
    """Prawdziwa aplikacja; pamieta identyfikatory wyslane przez runnera po restarcie."""

    def __init__(self) -> None:
        super().__init__(app=app)
        self.wyslane: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.wyslane.append(json.loads(request.content)["event_id"])
        return await super().handle_async_request(request)


@pytest.mark.parametrize(
    "punkt, pierwsze_ponowione",
    [("po_przyjeciu", 2), ("po_dopisaniu_potwierdzenia", 2), ("po_podmianie_pliku", 4),
     ("po_usunieciu_dziennika", 4)],
)
async def test_po_zabiciu_runnera_w_trakcie_wysylki_kazde_zdarzenie_jest_w_kokpicie_dokladnie_raz(
    swiat, punkt, pierwsze_ponowione,
):
    identyfikatory = [f"e{n}" for n in range(1, 7)]
    for identyfikator in identyfikatory:
        Bufor().dopisz(_zd(identyfikator, swiat.run_id))

    potomek = uruchom_i_zbierz(
        [sys.executable, "-c", SKRYPT, swiat.url, punkt], cwd=KORZEN, capture_output=True,
        env={**os.environ, "PYTHONPATH": str(KORZEN / "src"), "TOKEN_TESTU": TOKEN},
        timeout=60)
    assert potomek.returncode == KOD_SMIERCI, potomek.stderr.decode(errors="replace")

    kokpit = _KokpitZapisujacy()
    async with httpx.AsyncClient(transport=kokpit) as klient:
        await czekaj(RunnerKlienta("http://test", TOKEN).dosylaj(klient), 20, "dosylka")

    assert kokpit.wyslane == identyfikatory[pierwsze_ponowione - 1:]
    assert await _identyfikatory_w_bazie(swiat.run_id) == identyfikatory
    assert Bufor().niepotwierdzone() == []
