"""HAR-55: kokpit czyta, listuje i zapisuje pliki uzytkownika wylacznie przez runnera,
a runner robi to tylko w twardym zakresie sciezek (`runner.zakres.Zakres`).

Warstwy testow:
 1. operacje runnera (`runner.operacje.wykonaj`) na prawdziwych plikach w katalogu testu,
    bez kokpitu - kazdy kod bledu i kazda granica limitu;
 2. serwis kokpitu (`zapytaj_runnera_zespolu`, `pliki_runnera`) z wstrzykniegta wysylka;
 3. przelot HTTP: prawdziwa aplikacja (`cockpit.api.app.app`) + prawdziwy `runner.client.Runner`
    w procesie testu, a skutek sprawdzany na DYSKU (plik, prawa, mtime), nie na odpowiedzi.
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from transport_asgi import TransportASGIStrumieniujacy

from cockpit.api import app as app_mod
from cockpit.api import bus
from cockpit.db import session as db_session
from cockpit.db.models import Base, Membership, RolaWZespole, Runner, Tenant, User
from cockpit.services import pliki_runnera
from cockpit.services import zapytania_runnera as zapytania
from cockpit.services.errors import PlikPozaZakresem, RunnerOffline, StanNiemozliwy, Zabronione
from cockpit.settings import settings
from runner import operacje
from runner.client import Runner as RunnerKlient
from runner.zakres import PozaZakresem

SEKRET = "zxqv7-SEKRET-token-482913"
TRESC_SEKRETNA = "wnetrze-pliku-nie-wychodzi-w-bledzie-5521"
KOMUNIKAT_POZA_ZAKRESEM = "sciezka poza zakresem runnera"
KOMUNIKAT_OFFLINE = "runner offline — tryb tylko do odczytu"
HASLO = "Sekret-Hasl0-Alfa"
JEST_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


# =================================================================================
# Wspolne: zakres runnera w katalogu testu
# =================================================================================

@pytest.fixture
def projekt(tmp_path, monkeypatch):
    """Projekt w zakresie runnera i obcy katalog poza nim; HOME w katalogu testu.
    Sciezki rozwiazane (macOS: /tmp to dowiazanie do /private/tmp)."""
    baza = tmp_path.resolve()
    korzen = baza / "projekt"
    obcy = baza / "obcy"
    dom = baza / "dom"
    for k in (korzen, obcy, dom):
        k.mkdir()
    konfig = baza / "runner.json"
    konfig.write_text(json.dumps({"projekty": [str(korzen)]}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(dom))
    monkeypatch.setenv("COCKPIT_RUNNER_CONFIG", str(konfig))
    monkeypatch.delenv("COCKPIT_MAX_READ_KB", raising=False)
    return SimpleNamespace(korzen=korzen, obcy=obcy, dom=dom, konfig=konfig, baza=baza)


async def _wyk(typ: str, **dane) -> dict:
    return await operacje.wykonaj({"type": typ, "req_id": "r-1", **dane})


def _blad(odp: dict, kod: str) -> None:
    assert odp["ok"] is False, odp
    assert odp["kod"] == kod, odp
    assert odp["req_id"] == "r-1"
    assert isinstance(odp["blad"], str) and odp["blad"]


def _nazwy(katalog: Path) -> list[str]:
    return sorted(p.name for p in katalog.iterdir())


# =================================================================================
# 1. Operacje runnera: kontrakt
# =================================================================================

def test_runner_zna_trzy_operacje_plikowe_obok_ping():
    assert {"ping", "read_file", "list_files", "write_file"} <= set(operacje.OPERACJE)


def test_limit_wynikow_listy_plikow_to_dwa_tysiace():
    assert operacje.LIMIT_WYNIKOW == 2000


# ---- read_file --------------------------------------------------------------------

async def test_odczyt_pliku_w_projekcie_oddaje_tresc_sciezke_i_rozmiar_w_bajtach(projekt):
    plik = projekt.korzen / "notatka.txt"
    tresc = "zażółć gęślą jaźń\nlinia 2\n"
    plik.write_bytes(tresc.encode("utf-8"))

    odp = await _wyk("read_file", sciezka=str(plik))

    assert odp == {"req_id": "r-1", "ok": True, "wynik": {
        "sciezka": str(plik), "tresc": tresc, "rozmiar": len(tresc.encode("utf-8"))}}


async def test_odczyt_oddaje_rozmiar_w_bajtach_a_nie_w_znakach(projekt):
    plik = projekt.korzen / "pl.txt"
    plik.write_text("ąąąą", encoding="utf-8")

    odp = await _wyk("read_file", sciezka=str(plik))

    assert odp["wynik"]["rozmiar"] == 8


async def test_odczyt_oddaje_surowy_plik_z_koncami_linii_bez_tlumaczenia(projekt):
    plik = projekt.korzen / "crlf.txt"
    plik.write_bytes(b"a\r\nb\rc\n")

    odp = await _wyk("read_file", sciezka=str(plik))

    assert odp["wynik"]["tresc"] == "a\r\nb\rc\n"


async def test_odczyt_pliku_ktorego_nie_ma_daje_kod_nie_istnieje(projekt):
    odp = await _wyk("read_file", sciezka=str(projekt.korzen / "brak.txt"))
    _blad(odp, "nie_istnieje")


async def test_odczyt_katalogu_daje_kod_to_katalog(projekt):
    katalog = projekt.korzen / "podkatalog"
    katalog.mkdir()
    odp = await _wyk("read_file", sciezka=str(katalog))
    _blad(odp, "to_katalog")


async def test_plik_z_bajtami_niebedacymi_utf8_jest_binarny_i_nie_wychodzi_wyjatkiem(projekt):
    plik = projekt.korzen / "obraz.bin"
    plik.write_bytes(TRESC_SEKRETNA.encode() + b"\xff\xfe\x80")

    odp = await _wyk("read_file", sciezka=str(plik))

    _blad(odp, "binarny")
    assert TRESC_SEKRETNA not in odp["blad"]


async def test_plik_z_bajtem_zerowym_w_poprawnym_utf8_tez_jest_binarny(projekt):
    plik = projekt.korzen / "nul.dat"
    plik.write_bytes(b"ab\x00cd")
    assert plik.read_bytes().decode("utf-8") == "ab\x00cd"   # poprawny UTF-8: tylko NUL rozstrzyga

    odp = await _wyk("read_file", sciezka=str(plik))

    _blad(odp, "binarny")


async def test_plik_wiekszy_niz_limit_odczytu_daje_za_duzy_bez_tresci(projekt, monkeypatch):
    monkeypatch.setenv("COCKPIT_MAX_READ_KB", "1")
    plik = projekt.korzen / "duzy.txt"
    plik.write_text(TRESC_SEKRETNA + "a" * 2000, encoding="utf-8")

    odp = await _wyk("read_file", sciezka=str(plik))

    _blad(odp, "za_duzy")
    assert TRESC_SEKRETNA not in odp["blad"]


async def test_plik_rowny_limitowi_odczytu_przechodzi_a_o_bajt_wiekszy_jest_za_duzy(
    projekt, monkeypatch,
):
    monkeypatch.setenv("COCKPIT_MAX_READ_KB", "1")
    rowny = projekt.korzen / "rowny.txt"
    rowny.write_text("a" * 1024, encoding="utf-8")
    wiekszy = projekt.korzen / "wiekszy.txt"
    wiekszy.write_text("a" * 1025, encoding="utf-8")

    odp_rowny = await _wyk("read_file", sciezka=str(rowny))
    odp_wiekszy = await _wyk("read_file", sciezka=str(wiekszy))

    assert odp_rowny["ok"] is True
    assert odp_rowny["wynik"]["rozmiar"] == 1024
    _blad(odp_wiekszy, "za_duzy")


async def test_domyslny_limit_odczytu_to_512_kb(projekt):
    rowny = projekt.korzen / "rowny.txt"
    rowny.write_bytes(b"a" * (512 * 1024))
    wiekszy = projekt.korzen / "wiekszy.txt"
    wiekszy.write_bytes(b"a" * (512 * 1024 + 1))

    odp_rowny = await _wyk("read_file", sciezka=str(rowny))
    odp_wiekszy = await _wyk("read_file", sciezka=str(wiekszy))

    assert odp_rowny["ok"] is True
    assert odp_rowny["wynik"]["rozmiar"] == 512 * 1024
    _blad(odp_wiekszy, "za_duzy")


async def test_limit_odczytu_jest_czytany_przy_kazdym_wywolaniu_a_nie_zapamietany(
    projekt, monkeypatch,
):
    plik = projekt.korzen / "srednio.txt"
    plik.write_text("a" * 2000, encoding="utf-8")

    monkeypatch.setenv("COCKPIT_MAX_READ_KB", "1")
    za_malo = await _wyk("read_file", sciezka=str(plik))
    monkeypatch.setenv("COCKPIT_MAX_READ_KB", "4")
    wystarczy = await _wyk("read_file", sciezka=str(plik))

    _blad(za_malo, "za_duzy")
    assert wystarczy["ok"] is True


@pytest.mark.parametrize("zla", ["abc", "0", "-3", "1.5", "٥"],
                         ids=["napis", "zero", "ujemna", "ulamek", "cyfra_nie_ascii"])
async def test_zla_wartosc_limitu_odczytu_to_blad_konfiguracji_a_nie_cichy_powrot_do_domyslnej(
    projekt, monkeypatch, zla,
):
    plik = projekt.korzen / "maly.txt"
    plik.write_text("a", encoding="utf-8")
    monkeypatch.setenv("COCKPIT_MAX_READ_KB", zla)

    odp = await _wyk("read_file", sciezka=str(plik))

    _blad(odp, "zla_konfiguracja")


@pytest.mark.skipif(JEST_ROOT, reason="root czyta pliki bez wzgledu na prawa")
async def test_odczyt_pliku_bez_praw_daje_brak_uprawnien_bez_tekstu_wyjatku_systemu(projekt):
    plik = projekt.korzen / "zamkniety.txt"
    plik.write_text(TRESC_SEKRETNA, encoding="utf-8")
    plik.chmod(0o000)
    try:
        odp = await _wyk("read_file", sciezka=str(plik))
    finally:
        plik.chmod(0o600)

    _blad(odp, "brak_uprawnien")
    assert "Errno" not in odp["blad"]
    assert "Permission denied" not in odp["blad"]
    assert TRESC_SEKRETNA not in odp["blad"]


@pytest.mark.parametrize("dane", [{"sciezka": 5}, {"sciezka": None}, {}, {"sciezka": ["a"]}],
                         ids=["liczba", "none", "brak_pola", "lista"])
async def test_odczyt_ze_zla_sciezka_w_zapytaniu_daje_zle_dane(projekt, dane):
    odp = await _wyk("read_file", **dane)
    _blad(odp, "zle_dane")


# ---- poza zakresem -------------------------------------------------------------------

async def test_odczyt_pliku_poza_zakresem_daje_poza_zakresem_i_nie_oddaje_tresci(projekt):
    plik = projekt.obcy / "cudzy.txt"
    plik.write_text(TRESC_SEKRETNA, encoding="utf-8")

    odp = await _wyk("read_file", sciezka=str(plik))

    _blad(odp, "poza_zakresem")
    assert odp["blad"] == KOMUNIKAT_POZA_ZAKRESEM
    assert TRESC_SEKRETNA not in json.dumps(odp)


@pytest.mark.parametrize("nazwa", [".env", ".env.local", "klucz.pem", "serwer.key", "id_rsa",
                                   "id_ed25519.pub"])
async def test_odczyt_sekretu_w_projekcie_jest_odmowiony(projekt, nazwa):
    plik = projekt.korzen / nazwa
    plik.write_text(TRESC_SEKRETNA, encoding="utf-8")

    odp = await _wyk("read_file", sciezka=str(plik))

    _blad(odp, "poza_zakresem")
    assert TRESC_SEKRETNA not in json.dumps(odp)


async def test_odczyt_wzglednej_sciezki_jest_odmowiony(projekt):
    odp = await _wyk("read_file", sciezka="projekt/notatka.txt")
    _blad(odp, "poza_zakresem")


async def test_zakres_runnera_jest_czytany_z_konfiguracji_przy_kazdej_operacji(projekt):
    plik = projekt.korzen / "notatka.txt"
    plik.write_text("a", encoding="utf-8")
    przed = await _wyk("read_file", sciezka=str(plik))

    projekt.konfig.write_text(json.dumps({"projekty": [str(projekt.obcy)]}), encoding="utf-8")
    po = await _wyk("read_file", sciezka=str(plik))

    assert przed["ok"] is True
    _blad(po, "poza_zakresem")


async def test_wyjatek_zakresu_jest_tlumaczony_na_poza_zakresem_bez_powodu_i_sciezki(
    projekt, monkeypatch,
):
    async def odmawia(dane):
        raise PozaZakresem(f"/cos/{SEKRET}", "sekret")

    monkeypatch.setitem(operacje.OPERACJE, "odmawia", odmawia)

    odp = await _wyk("odmawia")

    assert odp == {"req_id": "r-1", "ok": False, "blad": KOMUNIKAT_POZA_ZAKRESEM,
                   "kod": "poza_zakresem"}


# ---- list_files ----------------------------------------------------------------------

async def test_lista_katalogu_oddaje_wpisy_posortowane_z_rozroznieniem_katalogow(projekt):
    for nazwa in ("b.txt", "a.txt", "C.txt"):
        (projekt.korzen / nazwa).write_text("x", encoding="utf-8")
    (projekt.korzen / "src").mkdir()

    odp = await _wyk("list_files", sciezka=str(projekt.korzen))

    assert odp["ok"] is True
    pliki = odp["wynik"]["pliki"]
    assert [p["sciezka"] for p in pliki] == sorted(
        str(projekt.korzen / n) for n in ("b.txt", "a.txt", "C.txt", "src"))
    assert {Path(p["sciezka"]).name: p["katalog"] for p in pliki} == {
        "a.txt": False, "b.txt": False, "C.txt": False, "src": True}
    assert odp["wynik"]["obcieto"] is False


async def test_lista_bez_wzorca_pokazuje_wszystko_a_wzorzec_zaweza_wynik(projekt):
    (projekt.korzen / "a.txt").write_text("x", encoding="utf-8")
    (projekt.korzen / "b.py").write_text("x", encoding="utf-8")

    bez = await _wyk("list_files", sciezka=str(projekt.korzen))
    z_wzorcem = await _wyk("list_files", sciezka=str(projekt.korzen), wzorzec="*.py")

    assert [Path(p["sciezka"]).name for p in bez["wynik"]["pliki"]] == ["a.txt", "b.py"]
    assert [Path(p["sciezka"]).name for p in z_wzorcem["wynik"]["pliki"]] == ["b.py"]


async def test_wzorzec_z_podkatalogiem_zagladac_do_podkatalogu(projekt):
    (projekt.korzen / "src").mkdir()
    (projekt.korzen / "src" / "x.py").write_text("x", encoding="utf-8")
    (projekt.korzen / "y.py").write_text("x", encoding="utf-8")

    odp = await _wyk("list_files", sciezka=str(projekt.korzen), wzorzec="src/*.py")

    assert [p["sciezka"] for p in odp["wynik"]["pliki"]] == [str(projekt.korzen / "src" / "x.py")]


async def test_lista_pomija_sekrety_po_cichu(projekt):
    (projekt.korzen / "readme.md").write_text("x", encoding="utf-8")
    for sekret in (".env", ".env.prod", "tls.pem", "api.key", "id_rsa", "id_ed25519"):
        (projekt.korzen / sekret).write_text(TRESC_SEKRETNA, encoding="utf-8")

    odp = await _wyk("list_files", sciezka=str(projekt.korzen))

    assert [Path(p["sciezka"]).name for p in odp["wynik"]["pliki"]] == ["readme.md"]
    assert odp["wynik"]["obcieto"] is False


async def test_lista_pomija_dowiazania_wychodzace_poza_zakres(projekt):
    cudzy = projekt.obcy / "cudzy.txt"
    cudzy.write_text(TRESC_SEKRETNA, encoding="utf-8")
    (projekt.korzen / "swoj.txt").write_text("x", encoding="utf-8")
    (projekt.korzen / "wyjscie.txt").symlink_to(cudzy)
    (projekt.korzen / "wyjscie_katalog").symlink_to(projekt.obcy, target_is_directory=True)

    odp = await _wyk("list_files", sciezka=str(projekt.korzen))

    assert [Path(p["sciezka"]).name for p in odp["wynik"]["pliki"]] == ["swoj.txt"]
    assert str(projekt.obcy) not in json.dumps(odp)


async def test_lista_katalogu_poza_zakresem_daje_poza_zakresem(projekt):
    (projekt.obcy / "cudzy.txt").write_text("x", encoding="utf-8")
    odp = await _wyk("list_files", sciezka=str(projekt.obcy))
    _blad(odp, "poza_zakresem")


async def test_lista_nieistniejacego_katalogu_daje_nie_istnieje(projekt):
    odp = await _wyk("list_files", sciezka=str(projekt.korzen / "brak"))
    _blad(odp, "nie_istnieje")


async def test_lista_na_zwyklym_pliku_daje_nie_katalog(projekt):
    plik = projekt.korzen / "a.txt"
    plik.write_text("x", encoding="utf-8")
    odp = await _wyk("list_files", sciezka=str(plik))
    _blad(odp, "nie_katalog")


@pytest.mark.parametrize("wzorzec", ["", "/etc/*", "../*", "a/../*", "a**"],
                         ids=["pusty", "bezwzgledny", "z_kropkami", "kropki_w_srodku",
                              "gwiazdki_w_nazwie"])
async def test_zly_wzorzec_listy_daje_zly_wzorzec(projekt, wzorzec):
    (projekt.korzen / "a.txt").write_text("x", encoding="utf-8")
    odp = await _wyk("list_files", sciezka=str(projekt.korzen), wzorzec=wzorzec)
    _blad(odp, "zly_wzorzec")


@pytest.mark.parametrize("dane", [{"sciezka": 5}, {}, {"sciezka": "/x", "wzorzec": 5}],
                         ids=["sciezka_liczba", "brak_sciezki", "wzorzec_liczba"])
async def test_lista_ze_zlymi_danymi_zapytania_daje_zle_dane(projekt, dane):
    if dane.get("wzorzec") is not None:
        dane = {**dane, "sciezka": str(projekt.korzen)}
    odp = await _wyk("list_files", **dane)
    _blad(odp, "zle_dane")


def _utworz_puste(katalog: Path, ile: int) -> None:
    for i in range(ile):
        (katalog / f"p{i:05d}.txt").touch()


async def test_lista_dokladnie_dwoch_tysiecy_wpisow_nie_jest_obcieta(projekt):
    _utworz_puste(projekt.korzen, 2000)

    odp = await _wyk("list_files", sciezka=str(projekt.korzen))

    assert len(odp["wynik"]["pliki"]) == 2000
    assert odp["wynik"]["obcieto"] is False


async def test_lista_ponad_dwa_tysiace_wpisow_zwraca_pierwsze_dwa_tysiace_i_zaznacza_obciecie(
    projekt,
):
    _utworz_puste(projekt.korzen, 2001)

    odp = await _wyk("list_files", sciezka=str(projekt.korzen))

    pliki = odp["wynik"]["pliki"]
    assert len(pliki) == 2000
    assert odp["wynik"]["obcieto"] is True
    assert pliki[0]["sciezka"] == str(projekt.korzen / "p00000.txt")
    assert pliki[-1]["sciezka"] == str(projekt.korzen / "p01999.txt")   # pierwsze po sortowaniu


async def test_pominiete_sekrety_nie_wliczaja_sie_do_limitu_wynikow(projekt):
    _utworz_puste(projekt.korzen, 2000)
    for i in range(5):
        (projekt.korzen / f".env.{i}").write_text("x", encoding="utf-8")

    odp = await _wyk("list_files", sciezka=str(projekt.korzen))

    assert len(odp["wynik"]["pliki"]) == 2000
    assert odp["wynik"]["obcieto"] is False


# ---- write_file ----------------------------------------------------------------------

async def test_zapis_nowego_pliku_tworzy_go_z_trescia_i_prawami_0644_mimo_ostrego_umask(projekt):
    plik = projekt.korzen / "nowy.txt"
    stary_umask = os.umask(0o077)
    try:
        odp = await _wyk("write_file", sciezka=str(plik), tresc="zażółć\n")
    finally:
        os.umask(stary_umask)

    assert odp == {"req_id": "r-1", "ok": True, "wynik": {
        "sciezka": str(plik), "rozmiar": len("zażółć\n".encode("utf-8"))}}
    assert plik.read_bytes() == "zażółć\n".encode("utf-8")
    assert stat.S_IMODE(plik.stat().st_mode) == 0o644
    assert _nazwy(projekt.korzen) == ["nowy.txt"]


async def test_zapis_nadpisuje_istniejacy_plik_i_zachowuje_jego_prawa(projekt):
    plik = projekt.korzen / "skrypt.sh"
    plik.write_text("stare", encoding="utf-8")
    plik.chmod(0o640)

    odp = await _wyk("write_file", sciezka=str(plik), tresc="nowe")

    assert odp["ok"] is True
    assert plik.read_text(encoding="utf-8") == "nowe"
    assert stat.S_IMODE(plik.stat().st_mode) == 0o640


async def test_zapis_nie_tlumaczy_koncow_linii(projekt):
    plik = projekt.korzen / "crlf.txt"

    odp = await _wyk("write_file", sciezka=str(plik), tresc="a\r\nb\rc\n")

    assert odp["wynik"]["rozmiar"] == 7
    assert plik.read_bytes() == b"a\r\nb\rc\n"


async def test_po_udanym_zapisie_w_katalogu_nie_zostaje_plik_tymczasowy(projekt):
    plik = projekt.korzen / "a.txt"
    plik.write_text("stare", encoding="utf-8")

    await _wyk("write_file", sciezka=str(plik), tresc="nowe")

    assert _nazwy(projekt.korzen) == ["a.txt"]


async def test_nieudany_zapis_zostawia_stary_plik_nietkniety_i_nie_zostawia_smieci(
    projekt, monkeypatch,
):
    plik = projekt.korzen / "a.txt"
    plik.write_text("stare", encoding="utf-8")

    def zepsute_replace(*a, **kw):
        raise OSError(5, f"dysk padl {SEKRET}")

    monkeypatch.setattr(os, "replace", zepsute_replace)

    odp = await _wyk("write_file", sciezka=str(plik), tresc="nowe-bardzo-dlugie")

    _blad(odp, "blad_dysku")
    assert SEKRET not in json.dumps(odp)
    assert "Errno" not in odp["blad"]
    assert plik.read_text(encoding="utf-8") == "stare"
    assert _nazwy(projekt.korzen) == ["a.txt"]


async def test_nieudany_zapis_z_odmowa_praw_daje_brak_uprawnien_i_stary_plik_zostaje(
    projekt, monkeypatch,
):
    plik = projekt.korzen / "a.txt"
    plik.write_text("stare", encoding="utf-8")

    def odmowa(*a, **kw):
        raise PermissionError(13, f"odmowa {SEKRET}")

    monkeypatch.setattr(os, "replace", odmowa)

    odp = await _wyk("write_file", sciezka=str(plik), tresc="nowe")

    _blad(odp, "brak_uprawnien")
    assert SEKRET not in json.dumps(odp)
    assert plik.read_text(encoding="utf-8") == "stare"
    assert _nazwy(projekt.korzen) == ["a.txt"]


@pytest.mark.skipif(JEST_ROOT, reason="root pisze do katalogow bez wzgledu na prawa")
async def test_zapis_do_katalogu_bez_prawa_zapisu_daje_brak_uprawnien(projekt):
    zamkniety = projekt.korzen / "zamkniety"
    zamkniety.mkdir()
    zamkniety.chmod(0o500)
    try:
        odp = await _wyk("write_file", sciezka=str(zamkniety / "a.txt"), tresc="x")
    finally:
        zamkniety.chmod(0o700)

    _blad(odp, "brak_uprawnien")
    assert _nazwy(zamkniety) == []


async def test_zapis_gdy_nie_ma_katalogu_nadrzednego_daje_brak_katalogu_i_go_nie_tworzy(projekt):
    odp = await _wyk("write_file", sciezka=str(projekt.korzen / "nowy_kat" / "a.txt"), tresc="x")

    _blad(odp, "brak_katalogu")
    assert not (projekt.korzen / "nowy_kat").exists()


async def test_zapis_na_katalogu_daje_to_katalog(projekt):
    katalog = projekt.korzen / "podkatalog"
    katalog.mkdir()

    odp = await _wyk("write_file", sciezka=str(katalog), tresc="x")

    _blad(odp, "to_katalog")
    assert katalog.is_dir()
    assert _nazwy(projekt.korzen) == ["podkatalog"]


async def test_zapis_poza_zakresem_jest_odmowiony_a_plik_poza_zakresem_zostaje_nietkniety(projekt):
    cudzy = projekt.obcy / "cudzy.txt"
    cudzy.write_text("oryginal", encoding="utf-8")
    mtime = cudzy.stat().st_mtime_ns

    odp = await _wyk("write_file", sciezka=str(cudzy), tresc="podmiana")

    _blad(odp, "poza_zakresem")
    assert cudzy.read_text(encoding="utf-8") == "oryginal"
    assert cudzy.stat().st_mtime_ns == mtime
    assert _nazwy(projekt.obcy) == ["cudzy.txt"]


async def test_zapis_do_katalogu_git_w_projekcie_jest_odmowiony(projekt):
    (projekt.korzen / ".git").mkdir()
    cel = projekt.korzen / ".git" / "config"

    odp = await _wyk("write_file", sciezka=str(cel), tresc="[core]")

    _blad(odp, "poza_zakresem")
    assert not cel.exists()
    assert _nazwy(projekt.korzen / ".git") == []


async def test_zapis_przez_dowiazanie_wychodzace_poza_zakres_nie_zmienia_celu(projekt):
    cudzy = projekt.obcy / "cudzy.txt"
    cudzy.write_text("oryginal", encoding="utf-8")
    (projekt.korzen / "wyjscie.txt").symlink_to(cudzy)

    odp = await _wyk("write_file", sciezka=str(projekt.korzen / "wyjscie.txt"), tresc="podmiana")

    _blad(odp, "poza_zakresem")
    assert cudzy.read_text(encoding="utf-8") == "oryginal"


async def test_zapis_sekretu_w_projekcie_jest_odmowiony(projekt):
    cel = projekt.korzen / ".env"

    odp = await _wyk("write_file", sciezka=str(cel), tresc="X=1")

    _blad(odp, "poza_zakresem")
    assert not cel.exists()


@pytest.mark.parametrize("dane", [{"tresc": 5}, {"tresc": None}, {}],
                         ids=["tresc_liczba", "tresc_none", "brak_tresci"])
async def test_zapis_ze_zla_trescia_w_zapytaniu_daje_zle_dane_i_nie_tworzy_pliku(projekt, dane):
    cel = projekt.korzen / "a.txt"

    odp = await _wyk("write_file", sciezka=str(cel), **dane)

    _blad(odp, "zle_dane")
    assert not cel.exists()


async def test_zapis_ze_zla_sciezka_w_zapytaniu_daje_zle_dane(projekt):
    odp = await _wyk("write_file", sciezka=5, tresc="x")
    _blad(odp, "zle_dane")


# ---- regresja: dotychczasowe zachowanie wykonaj() -----------------------------------

async def test_ping_nadal_odpowiada_pong():
    assert await _wyk("ping") == {"req_id": "r-1", "ok": True, "wynik": {"pong": True}}


async def test_nieznana_operacja_nadal_daje_kod_nieznana_operacja():
    odp = await _wyk("teleport")
    assert odp == {"req_id": "r-1", "ok": False, "blad": "nieznana operacja: teleport",
                   "kod": "nieznana_operacja"}


async def test_nieznany_wyjatek_operacji_nadal_daje_blad_operacji_bez_tresci_wyjatku(monkeypatch):
    async def psuje(dane):
        raise RuntimeError(f"w srodku jest {SEKRET}")

    monkeypatch.setitem(operacje.OPERACJE, "psuje", psuje)

    odp = await _wyk("psuje")

    assert odp["ok"] is False
    assert odp["kod"] == "blad_operacji"
    assert SEKRET not in json.dumps(odp)
    assert "RuntimeError" in odp["blad"]


# ---- dokladne kody i komunikaty bledow (mutanty zmienionych funkcji) ---------------------

KOMUNIKAT_ZLY_WZORZEC = "wzorzec jest pusty, bezwzgledny albo niedozwolony"


@pytest.mark.parametrize(
    ("wyjatek", "brak_to_katalog", "kod", "komunikat"),
    [
        (PermissionError(errno.EACCES, "x"), False, "brak_uprawnien",
         "brak uprawnien do pliku lub katalogu"),
        (FileNotFoundError(errno.ENOENT, "x"), False, "nie_istnieje", "sciezka nie istnieje"),
        (FileNotFoundError(errno.ENOENT, "x"), True, "brak_katalogu",
         "katalog nadrzedny nie istnieje"),
        (IsADirectoryError(errno.EISDIR, "x"), False, "to_katalog", "sciezka jest katalogiem"),
        (NotADirectoryError(errno.ENOTDIR, "x"), False, "nie_katalog",
         "element sciezki nie jest katalogiem"),
        (OSError(errno.ENOSPC, "x"), False, "blad_dysku", "blad dysku przy operacji na pliku"),
        (OSError(errno.EIO, "x"), True, "blad_dysku", "blad dysku przy operacji na pliku"),
    ],
    ids=["brak_praw", "brak_pliku", "brak_katalogu_nadrzednego", "katalog_zamiast_pliku",
         "plik_zamiast_katalogu", "brak_miejsca_na_dysku", "inny_blad_dysku"],
)
def test_kazdy_blad_dysku_ma_staly_kod_i_staly_komunikat_bez_tekstu_systemu(
    wyjatek, brak_to_katalog, kod, komunikat,
):
    blad = operacje._blad_dysku(wyjatek, brak_to_katalog=brak_to_katalog)

    assert type(blad) is operacje.BladOperacji
    assert blad.kod == kod
    assert blad.komunikat == komunikat
    assert str(blad) == komunikat


def test_blad_operacji_niesie_komunikat_jako_swoj_tekst():
    blad = operacje.BladOperacji("jakis_kod", "komunikat dla czlowieka")

    assert str(blad) == "komunikat dla czlowieka"
    assert blad.args == ("komunikat dla czlowieka",)


async def test_odczyt_katalogu_mowi_ze_sciezka_jest_katalogiem(projekt):
    katalog = projekt.korzen / "podkatalog"
    katalog.mkdir()

    odp = await _wyk("read_file", sciezka=str(katalog))

    assert odp["blad"] == "sciezka jest katalogiem"


async def test_odczyt_kolejki_fifo_jest_odmowiony_bez_zawieszenia_jako_blad_dysku(projekt):
    kolejka = projekt.korzen / "kolejka"
    os.mkfifo(kolejka)

    odp = await asyncio.wait_for(_wyk("read_file", sciezka=str(kolejka)), 10)

    _blad(odp, "blad_dysku")
    assert odp["blad"] == "sciezka nie jest zwyklym plikiem"


async def test_odczyt_czyta_z_pliku_najwyzej_limit_plus_jeden_bajt_a_nie_caly_plik(
    projekt, monkeypatch,
):
    monkeypatch.setenv("COCKPIT_MAX_READ_KB", "1")
    plik = projekt.korzen / "ogromny.txt"
    plik.write_bytes(b"a" * 5000)
    odczyty: list = []

    class Sledzacy:
        def __init__(self, f):
            self._f = f

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._f.close()

        def read(self, n=-1):
            odczyty.append(n)
            return self._f.read(n)

    monkeypatch.setattr(operacje, "open", lambda *a, **kw: Sledzacy(open(*a, **kw)),
                        raising=False)

    odp = await _wyk("read_file", sciezka=str(plik))

    _blad(odp, "za_duzy")
    assert odczyty == [1024 + 1]


async def test_komunikat_za_duzego_pliku_podaje_limit_w_pelnych_kilobajtach(projekt, monkeypatch):
    monkeypatch.setenv("COCKPIT_MAX_READ_KB", "1")
    plik = projekt.korzen / "duzy.txt"
    plik.write_bytes(b"a" * 1025)

    odp = await _wyk("read_file", sciezka=str(plik))

    _blad(odp, "za_duzy")
    assert odp["blad"] == "plik jest wiekszy niz limit 1 KB"


async def test_komunikat_binarnego_pliku_mowi_ze_to_nie_tekst_utf8(projekt):
    plik = projekt.korzen / "obraz.bin"
    plik.write_bytes(b"\xff\xfe\x80")

    odp = await _wyk("read_file", sciezka=str(plik))

    _blad(odp, "binarny")
    assert odp["blad"] == "plik nie jest tekstem UTF-8"


async def test_komunikat_pliku_z_bajtem_zerowym_wskazuje_bajt_nul(projekt):
    plik = projekt.korzen / "nul.dat"
    plik.write_bytes(b"ab\x00cd")

    odp = await _wyk("read_file", sciezka=str(plik))

    _blad(odp, "binarny")
    assert odp["blad"] == "plik nie jest tekstem (zawiera bajt NUL)"


async def test_lista_na_zwyklym_pliku_mowi_ze_sciezka_nie_jest_katalogiem(projekt):
    plik = projekt.korzen / "a.txt"
    plik.write_text("x", encoding="utf-8")

    odp = await _wyk("list_files", sciezka=str(plik))

    assert odp["blad"] == "sciezka nie jest katalogiem"


@pytest.mark.skipif(JEST_ROOT, reason="root listuje katalogi bez wzgledu na prawa")
async def test_lista_katalogu_bez_prawa_odczytu_daje_brak_uprawnien_a_nie_pusta_liste(projekt):
    zamkniety = projekt.korzen / "zamkniety"
    zamkniety.mkdir()
    (zamkniety / "a.txt").write_text("x", encoding="utf-8")
    zamkniety.chmod(0o000)
    try:
        odp = await _wyk("list_files", sciezka=str(zamkniety))
    finally:
        zamkniety.chmod(0o700)

    _blad(odp, "brak_uprawnien")


@pytest.mark.skipif(JEST_ROOT, reason="root listuje katalogi bez wzgledu na prawa")
async def test_lista_ze_wzorcem_przez_zamkniety_podkatalog_daje_brak_uprawnien_a_nie_pusta_liste(
    projekt,
):
    sub = projekt.korzen / "sub"
    sub.mkdir()
    (sub / "a.txt").write_text("x", encoding="utf-8")
    sub.chmod(0o000)
    try:
        odp = await _wyk("list_files", sciezka=str(projekt.korzen), wzorzec="sub/*")
    finally:
        sub.chmod(0o700)

    _blad(odp, "brak_uprawnien")
    assert "Errno" not in odp["blad"]
    assert "Permission denied" not in odp["blad"]


@pytest.mark.skipif(JEST_ROOT, reason="root listuje katalogi bez wzgledu na prawa")
async def test_rekurencyjna_lista_z_zamknietym_podkatalogiem_w_glebi_daje_brak_uprawnien(projekt):
    (projekt.korzen / "a" / "b").mkdir(parents=True)
    (projekt.korzen / "a" / "b" / "y.txt").write_text("x", encoding="utf-8")
    zamkniety = projekt.korzen / "a" / "sub"
    zamkniety.mkdir()
    (zamkniety / "x.txt").write_text("x", encoding="utf-8")
    zamkniety.chmod(0o000)
    try:
        odp = await _wyk("list_files", sciezka=str(projekt.korzen), wzorzec="**/*.txt")
    finally:
        zamkniety.chmod(0o700)

    _blad(odp, "brak_uprawnien")
    assert "Errno" not in odp["blad"]
    assert "Permission denied" not in odp["blad"]


@pytest.mark.skipif(JEST_ROOT, reason="root listuje katalogi bez wzgledu na prawa")
async def test_zamkniety_katalog_ktorego_wzorzec_nie_przeszukuje_nie_psuje_listy(projekt):
    zablokowany = projekt.korzen / "zablokowany"
    zablokowany.mkdir()
    (zablokowany / "a.txt").write_text("x", encoding="utf-8")
    (projekt.korzen / "plik.txt").write_text("x", encoding="utf-8")
    zablokowany.chmod(0o000)
    try:
        odp_txt = await _wyk("list_files", sciezka=str(projekt.korzen), wzorzec="*.txt")
        odp_wszystko = await _wyk("list_files", sciezka=str(projekt.korzen), wzorzec="*")
    finally:
        zablokowany.chmod(0o700)

    assert odp_txt["ok"] is True, odp_txt
    assert [Path(p["sciezka"]).name for p in odp_txt["wynik"]["pliki"]] == ["plik.txt"]
    assert odp_wszystko["ok"] is True, odp_wszystko
    wpisy = {Path(p["sciezka"]).name: p["katalog"] for p in odp_wszystko["wynik"]["pliki"]}
    assert wpisy == {"zablokowany": True, "plik.txt": False}


async def test_blad_systemu_przy_przeszukiwaniu_katalogu_jest_tlumaczony_na_kod_dziedziny(
    projekt, monkeypatch,
):
    sub = projekt.korzen / "sub"
    sub.mkdir()
    (sub / "a.txt").write_text("x", encoding="utf-8")
    prawdziwy_scandir = os.scandir

    def scandir_z_odmowa(sciezka="."):
        if str(sciezka) == str(sub):
            raise PermissionError(errno.EACCES, f"odmowa {SEKRET}")
        return prawdziwy_scandir(sciezka)

    monkeypatch.setattr(os, "scandir", scandir_z_odmowa)

    odp = await _wyk("list_files", sciezka=str(projekt.korzen), wzorzec="sub/*")

    _blad(odp, "brak_uprawnien")
    assert SEKRET not in json.dumps(odp)


async def test_blad_systemu_przy_odczycie_wpisu_listy_jest_tlumaczony_na_kod_dziedziny(
    projekt, monkeypatch,
):
    plik = projekt.korzen / "a.txt"
    plik.write_text("x", encoding="utf-8")
    prawdziwy_stat = os.stat

    def stat_z_odmowa(sciezka, *a, **kw):
        if str(sciezka) == str(plik):
            raise PermissionError(errno.EACCES, f"odmowa {SEKRET}")
        return prawdziwy_stat(sciezka, *a, **kw)

    monkeypatch.setattr(os, "stat", stat_z_odmowa)

    odp = await _wyk("list_files", sciezka=str(projekt.korzen))

    _blad(odp, "brak_uprawnien")
    assert SEKRET not in json.dumps(odp)


async def test_wiszace_dowiazanie_na_liscie_jest_pomijane_a_pozostale_wpisy_zostaja(projekt):
    (projekt.korzen / "a_wisi").symlink_to(projekt.korzen / "nie_ma")
    (projekt.korzen / "dobry.txt").write_text("x", encoding="utf-8")
    (projekt.korzen / "drugi.txt").write_text("x", encoding="utf-8")

    odp = await _wyk("list_files", sciezka=str(projekt.korzen))

    assert odp["ok"] is True
    assert [Path(p["sciezka"]).name for p in odp["wynik"]["pliki"]] == ["dobry.txt", "drugi.txt"]


@pytest.mark.parametrize("wzorzec", ["", "a\0b"], ids=["pusty", "bajt_zerowy"])
async def test_pusty_wzorzec_i_wzorzec_z_bajtem_zerowym_odrzuca_walidacja_wzorca(
    projekt, wzorzec,
):
    odp = await _wyk("list_files", sciezka=str(projekt.korzen), wzorzec=wzorzec)

    _blad(odp, "zly_wzorzec")
    assert odp["blad"] == KOMUNIKAT_ZLY_WZORZEC


@pytest.mark.parametrize("wzorzec", ["a/../*", "../*", "a**"],
                         ids=["kropki_w_srodku", "kropki_na_poczatku", "gwiazdki_w_nazwie"])
async def test_wzorzec_z_kropkami_i_gwiazdkami_w_nazwie_ma_komunikat_walidacji(projekt, wzorzec):
    odp = await _wyk("list_files", sciezka=str(projekt.korzen), wzorzec=wzorzec)

    _blad(odp, "zly_wzorzec")
    assert odp["blad"] == KOMUNIKAT_ZLY_WZORZEC


async def test_wzorzec_podwojna_gwiazdka_jako_caly_skladnik_szuka_rekurencyjnie(projekt):
    (projekt.korzen / "a" / "b").mkdir(parents=True)
    (projekt.korzen / "a" / "b" / "gleboki.txt").write_text("x", encoding="utf-8")
    (projekt.korzen / "plytki.txt").write_text("x", encoding="utf-8")

    odp = await _wyk("list_files", sciezka=str(projekt.korzen), wzorzec="**/*.txt")

    assert odp["ok"] is True
    assert sorted(Path(p["sciezka"]).name for p in odp["wynik"]["pliki"]) == [
        "gleboki.txt", "plytki.txt"]


def test_wzorzec_zaczynajacy_sie_od_ukosnika_jest_odrzucany_takze_gdy_isabs_go_nie_widzi(
    monkeypatch,
):
    monkeypatch.setattr(os.path, "isabs", lambda p: False)

    with pytest.raises(operacje.BladOperacji) as wyjatek:
        operacje._sprawdz_wzorzec("/etc/*")

    assert wyjatek.value.kod == "zly_wzorzec"
    assert wyjatek.value.komunikat == KOMUNIKAT_ZLY_WZORZEC


def test_wzorzec_uznany_przez_isabs_za_bezwzgledny_jest_odrzucany_mimo_braku_ukosnika(
    monkeypatch,
):
    monkeypatch.setattr(os.path, "isabs", lambda p: True)

    with pytest.raises(operacje.BladOperacji) as wyjatek:
        operacje._sprawdz_wzorzec("docs/*")

    assert wyjatek.value.kod == "zly_wzorzec"


async def test_zapis_na_katalogu_mowi_ze_sciezka_jest_katalogiem(projekt):
    katalog = projekt.korzen / "podkatalog"
    katalog.mkdir()

    odp = await _wyk("write_file", sciezka=str(katalog), tresc="x")

    assert odp["blad"] == "sciezka jest katalogiem"


async def test_zapis_tresci_z_niekodowalnym_znakiem_daje_zle_dane_i_nie_tworzy_pliku(projekt):
    plik = projekt.korzen / "a.txt"

    odp = await _wyk("write_file", sciezka=str(plik), tresc="ok\ud800")

    _blad(odp, "zle_dane")
    assert odp["blad"] == "tresc nie jest poprawnym tekstem UTF-8"
    assert _nazwy(projekt.korzen) == []


async def test_plik_tymczasowy_zapisu_lezy_w_katalogu_celu_z_nazwa_kropka_cel_i_koncowka_tmp(
    projekt, monkeypatch,
):
    plik = projekt.korzen / "a.txt"
    plik.write_text("stare", encoding="utf-8")
    prawdziwy_fsync = os.fsync
    migawki: list[list[str]] = []

    def fsync_ze_migawka(fd):
        migawki.append(_nazwy(projekt.korzen))
        return prawdziwy_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync_ze_migawka)

    odp = await _wyk("write_file", sciezka=str(plik), tresc="nowe")

    assert odp["ok"] is True
    assert len(migawki) == 1
    tymczasowe = [n for n in migawki[0] if n != "a.txt"]
    assert len(tymczasowe) == 1
    assert tymczasowe[0].startswith(".a.txt.")
    assert tymczasowe[0].endswith(".tmp")
    assert _nazwy(projekt.korzen) == ["a.txt"]


async def test_udany_zapis_nie_probuje_usuwac_pliku_tymczasowego_ktory_juz_zastapil_cel(
    projekt, monkeypatch,
):
    plik = projekt.korzen / "a.txt"
    usuniete: list[str] = []
    prawdziwy_unlink = os.unlink

    def unlink_ze_sladem(sciezka, *a, **kw):
        usuniete.append(str(sciezka))
        return prawdziwy_unlink(sciezka, *a, **kw)

    monkeypatch.setattr(os, "unlink", unlink_ze_sladem)

    odp = await _wyk("write_file", sciezka=str(plik), tresc="nowe")

    assert odp["ok"] is True
    assert plik.read_text(encoding="utf-8") == "nowe"
    assert usuniete == []


async def test_brak_miejsca_na_dysku_przy_zapisie_daje_blad_dysku_a_stary_plik_i_katalog_zostaja(
    projekt, monkeypatch,
):
    plik = projekt.korzen / "a.txt"
    plik.write_text("stare", encoding="utf-8")

    def brak_miejsca(fd):
        raise OSError(errno.ENOSPC, f"brak miejsca {SEKRET}")

    monkeypatch.setattr(os, "fsync", brak_miejsca)

    odp = await _wyk("write_file", sciezka=str(plik), tresc="nowe")

    _blad(odp, "blad_dysku")
    assert odp["blad"] == "blad dysku przy operacji na pliku"
    assert SEKRET not in json.dumps(odp)
    assert plik.read_text(encoding="utf-8") == "stare"
    assert _nazwy(projekt.korzen) == ["a.txt"]


async def test_zapis_ktorego_nie_da_sie_posprzatac_nadal_zglasza_pierwotny_blad_dysku(
    projekt, monkeypatch,
):
    plik = projekt.korzen / "a.txt"
    plik.write_text("stare", encoding="utf-8")

    def zepsute_replace(*a, **kw):
        raise PermissionError(errno.EACCES, "odmowa przy replace")

    def zepsute_unlink(*a, **kw):
        raise OSError(errno.EIO, "odmowa przy unlink")

    monkeypatch.setattr(os, "replace", zepsute_replace)
    monkeypatch.setattr(os, "unlink", zepsute_unlink)

    odp = await _wyk("write_file", sciezka=str(plik), tresc="nowe")

    _blad(odp, "brak_uprawnien")
    assert plik.read_text(encoding="utf-8") == "stare"


# =================================================================================
# 2. Serwis kokpitu: tlumaczenie odpowiedzi runnera na bledy dziedziny
# =================================================================================

@pytest.fixture
async def zespoly(tmp_path):
    """Dwa zespoly; kazdy z jednym swiezym runnerem. Baza tylko dla serwisu (bez aplikacji)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'serwis.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with maker() as s:
        a, b = Tenant(name="Alfa"), Tenant(name="Beta")
        s.add(a)
        await s.flush()
        s.add(b)
        await s.flush()
        teraz = datetime.now(timezone.utc)
        s.add(Runner(id="runner-a", tenant_id=a.id, name="a", token_hash="x", last_seen_at=teraz))
        s.add(Runner(id="runner-b", tenant_id=b.id, name="b", token_hash="y", last_seen_at=teraz))
        await s.commit()
        ids = SimpleNamespace(a=a.id, b=b.id)
    yield SimpleNamespace(maker=maker, ids=ids)
    await engine.dispose()


class _Wysylka:
    """Wstrzykiwana `wyslij`: zapamietuje wywolania i oddaje zadana odpowiedz."""

    def __init__(self, odpowiedz: dict) -> None:
        self.odpowiedz = odpowiedz
        self.wywolania: list[tuple] = []

    async def __call__(self, runner_id, typ, dane, timeout_s):
        self.wywolania.append((runner_id, typ, dane))
        return self.odpowiedz


async def _zapytaj(zespoly, tenant_id, odpowiedz, polaczone=frozenset({"runner-a", "runner-b"})):
    wysylka = _Wysylka(odpowiedz)
    async with zespoly.maker() as s:
        try:
            wynik = await zapytania.zapytaj_runnera_zespolu(
                s, tenant_id, "read_file", {"sciezka": "/x"}, polaczone, wysylka)
        except Exception as e:
            return e, wysylka
    return wynik, wysylka


async def test_odmowa_runnera_z_kodem_poza_zakresem_staje_sie_bledem_pliku_poza_zakresem(zespoly):
    wynik, _ = await _zapytaj(zespoly, zespoly.ids.a, {
        "ok": False, "blad": KOMUNIKAT_POZA_ZAKRESEM, "kod": "poza_zakresem"})

    assert type(wynik) is PlikPozaZakresem
    assert isinstance(wynik, Zabronione)
    assert str(wynik) == KOMUNIKAT_POZA_ZAKRESEM


async def test_odmowa_poza_zakresem_bez_komunikatu_dostaje_komunikat_domyslny(zespoly):
    wynik, _ = await _zapytaj(zespoly, zespoly.ids.a, {"ok": False, "kod": "poza_zakresem"})

    assert type(wynik) is PlikPozaZakresem
    assert str(wynik) == KOMUNIKAT_POZA_ZAKRESEM


async def test_odpowiedz_bez_pola_ok_z_kodem_poza_zakresem_tez_jest_odmowa(zespoly):
    wynik, _ = await _zapytaj(zespoly, zespoly.ids.a, {"blad": "nie wolno", "kod": "poza_zakresem"})

    assert type(wynik) is PlikPozaZakresem
    assert str(wynik) == "nie wolno"


@pytest.mark.parametrize("kod", ["binarny", "za_duzy", "nie_istnieje", "blad_operacji", None])
async def test_inny_kod_bledu_runnera_to_zwykly_stan_niemozliwy_z_komunikatem_runnera(zespoly, kod):
    odpowiedz = {"ok": False, "blad": "plik jest binarny"}
    if kod is not None:
        odpowiedz["kod"] = kod

    wynik, _ = await _zapytaj(zespoly, zespoly.ids.a, odpowiedz)

    assert type(wynik) is StanNiemozliwy
    assert str(wynik) == "plik jest binarny"


async def test_brak_runnera_online_w_zespole_to_runner_offline_z_komunikatem_trybu_do_odczytu(
    zespoly,
):
    wynik, wysylka = await _zapytaj(zespoly, zespoly.ids.a, {"ok": True, "wynik": {}},
                                    polaczone=frozenset())

    assert type(wynik) is RunnerOffline
    assert isinstance(wynik, StanNiemozliwy)
    assert str(wynik) == KOMUNIKAT_OFFLINE
    assert wysylka.wywolania == []


async def test_runner_z_nieaktualnym_heartbeatem_to_runner_offline(zespoly):
    async with zespoly.maker() as s:
        await s.execute(update(Runner).where(Runner.id == "runner-a").values(
            last_seen_at=datetime(2020, 1, 1, tzinfo=timezone.utc)))
        await s.commit()

    wynik, wysylka = await _zapytaj(zespoly, zespoly.ids.a, {"ok": True, "wynik": {}})

    assert type(wynik) is RunnerOffline
    assert wysylka.wywolania == []


async def test_zapytanie_zespolu_trafia_tylko_do_runnera_tego_zespolu(zespoly):
    _, wysylka_a = await _zapytaj(zespoly, zespoly.ids.a, {"ok": True, "wynik": {"x": 1}})
    _, wysylka_b = await _zapytaj(zespoly, zespoly.ids.b, {"ok": True, "wynik": {"x": 1}})

    assert [w[0] for w in wysylka_a.wywolania] == ["runner-a"]
    assert [w[0] for w in wysylka_b.wywolania] == ["runner-b"]


async def test_zespol_bez_wlasnego_runnera_online_nie_dostaje_runnera_obcego_zespolu(zespoly):
    wynik, wysylka = await _zapytaj(zespoly, zespoly.ids.a, {"ok": True, "wynik": {}},
                                    polaczone=frozenset({"runner-b"}))

    assert type(wynik) is RunnerOffline
    assert wysylka.wywolania == []


async def test_serwis_plikow_czyta_przez_operacje_read_file_z_sciezka(zespoly):
    wysylka = _Wysylka({"ok": True, "wynik": {"tresc": "x"}})
    async with zespoly.maker() as s:
        wynik = await pliki_runnera.czytaj(
            s, zespoly.ids.a, "/p/a.txt", frozenset({"runner-a"}), wysylka)

    assert wynik == {"tresc": "x"}
    assert wysylka.wywolania == [("runner-a", "read_file", {"sciezka": "/p/a.txt"})]


async def test_serwis_plikow_listuje_przez_operacje_list_files_ze_wzorcem(zespoly):
    wysylka = _Wysylka({"ok": True, "wynik": {"pliki": [], "obcieto": False}})
    async with zespoly.maker() as s:
        wynik = await pliki_runnera.listuj(
            s, zespoly.ids.a, "/p", "*.py", frozenset({"runner-a"}), wysylka)

    assert wynik == {"pliki": [], "obcieto": False}
    assert wysylka.wywolania == [("runner-a", "list_files", {"sciezka": "/p", "wzorzec": "*.py"})]


async def test_serwis_plikow_zapisuje_przez_operacje_write_file_z_trescia(zespoly):
    wysylka = _Wysylka({"ok": True, "wynik": {"sciezka": "/p/a.txt", "rozmiar": 3}})
    async with zespoly.maker() as s:
        wynik = await pliki_runnera.zapisz(
            s, zespoly.ids.a, "/p/a.txt", "abc", frozenset({"runner-a"}), wysylka)

    assert wynik == {"sciezka": "/p/a.txt", "rozmiar": 3}
    assert wysylka.wywolania == [
        ("runner-a", "write_file", {"sciezka": "/p/a.txt", "tresc": "abc"})]


async def test_serwis_plikow_przy_braku_runnera_zespolu_zglasza_runner_offline(zespoly):
    wysylka = _Wysylka({"ok": True, "wynik": {}})
    async with zespoly.maker() as s:
        with pytest.raises(RunnerOffline):
            await pliki_runnera.zapisz(
                s, zespoly.ids.a, "/p/a.txt", "abc", frozenset({"runner-b"}), wysylka)
    assert wysylka.wywolania == []


# =================================================================================
# 3. Przelot HTTP: prawdziwa aplikacja + prawdziwy runner w procesie
# =================================================================================

@pytest.fixture(autouse=True)
def _czysta_szyna():
    """Kolejki, oczekujace zapytania i ewidencja polaczen to stan procesu."""
    slowniki = ("_oczekujace", "_runner_queues", "_polaczone", "_rozlaczone")
    zapisane = {n: dict(getattr(bus, n)) for n in slowniki if hasattr(bus, n)}
    yield
    for nazwa, przed in zapisane.items():
        biezacy = getattr(bus, nazwa)
        biezacy.clear()
        biezacy.update(przed)


class _Kokpit:
    """Aplikacja na bazie w katalogu testu: konta, runnery w procesie i klienci HTTP."""

    def __init__(self, maker) -> None:
        self.maker = maker
        self.klienci: list[AsyncClient] = []
        self.runnery: list[tuple[RunnerKlient, asyncio.Task]] = []

    def klient(self) -> AsyncClient:
        k = AsyncClient(transport=ASGITransport(app=app_mod.app), base_url="https://test")
        self.klienci.append(k)
        return k

    async def konto(self, email: str, zespol: str) -> SimpleNamespace:
        """Rejestruje wlasciciela nowego zespolu; zwraca klienta z sesja, csrf i id zespolu."""
        k = self.klient()
        odp = await k.post("/api/konto/rejestracja",
                           json={"email": email, "haslo": HASLO, "nazwa_zespolu": zespol})
        assert odp.status_code == 201, odp.text
        async with self.maker() as s:
            tenant_id = (await s.execute(select(Tenant.id).where(Tenant.name == zespol))).scalar_one()
        return SimpleNamespace(http=k, csrf={"X-CSRF-Token": odp.json()["csrf"]}, tenant_id=tenant_id)

    async def viewer(self, email: str, tenant_id: str) -> SimpleNamespace:
        """Konto z rola viewer w ISTNIEJACYM zespole (rejestracja zaklada wlasny zespol,
        wiec czlonkostwo jest przepisane w bazie), potem swieze logowanie."""
        wstepne = await self.konto(email, f"zespol-{email}")
        async with self.maker() as s:
            user_id = (await s.execute(select(User.id).where(User.email == email))).scalar_one()
            await s.execute(update(Membership).where(Membership.user_id == user_id).values(
                tenant_id=tenant_id, rola=RolaWZespole.viewer))
            await s.commit()
        k = self.klient()
        odp = await k.post("/api/konto/logowanie", json={"email": email, "haslo": HASLO})
        assert odp.status_code == 200, odp.text
        assert wstepne.tenant_id != tenant_id
        return SimpleNamespace(http=k, csrf={"X-CSRF-Token": odp.json()["csrf"]}, tenant_id=tenant_id)

    async def dodaj_runnera(self, runner_id: str, tenant_id: str) -> None:
        async with self.maker() as s:
            s.add(Runner(id=runner_id, tenant_id=tenant_id, name=runner_id,
                         token_hash=hashlib.sha256(f"tok-{runner_id}".encode()).hexdigest(),
                         last_seen_at=datetime.now(timezone.utc)))
            await s.commit()

    async def uruchom_runnera(self, runner_id: str) -> None:
        runner = RunnerKlient("http://kokpit.test", f"tok-{runner_id}",
                              transport=TransportASGIStrumieniujacy(app_mod.app))
        nasluch = asyncio.create_task(runner.listen())
        self.runnery.append((runner, nasluch))
        koniec = time.monotonic() + 10
        while runner_id not in bus.connected_runner_ids():
            assert time.monotonic() < koniec, "runner nie polaczyl sie z kokpitem"
            await asyncio.sleep(0.01)

    async def zamknij(self) -> None:
        for runner, nasluch in reversed(self.runnery):
            await asyncio.wait_for(runner.stop(), 10)
            gotowe, _ = await asyncio.wait({nasluch}, timeout=10)
            if nasluch not in gotowe:
                nasluch.cancel()
                await asyncio.wait({nasluch}, timeout=5)
        for k in self.klienci:
            await k.aclose()


@pytest.fixture
async def kokpit(tmp_path, monkeypatch, projekt):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'kokpit.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    monkeypatch.setattr(settings, "ciasteczko_secure", True)
    monkeypatch.setattr(settings, "runner_zapytanie_timeout_s", 20.0)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    k = _Kokpit(maker)
    try:
        yield k
    finally:
        await k.zamknij()
        await engine.dispose()


@pytest.fixture
async def zespol(kokpit):
    """Zespol Alfa z wlascicielem i jednym zywym runnerem; zakres = `projekt`."""
    konto = await kokpit.konto("ala@przyklad.pl", "Alfa")
    await kokpit.dodaj_runnera("runner-alfa", konto.tenant_id)
    await kokpit.uruchom_runnera("runner-alfa")
    return konto


async def _get(konto, sciezka: str):
    return await konto.http.get("/api/pliki", params={"sciezka": sciezka})


async def _put(konto, sciezka: str, tresc: str):
    return await konto.http.put("/api/pliki", json={"sciezka": sciezka, "tresc": tresc},
                                headers=konto.csrf)


async def test_odczyt_pliku_projektu_przez_kokpit_i_runnera_oddaje_tresc_z_dysku(
    zespol, projekt,
):
    plik = projekt.korzen / "README.md"
    plik.write_text("# Projekt\nzażółć\n", encoding="utf-8")

    odp = await _get(zespol, str(plik))

    assert odp.status_code == 200, odp.text
    assert odp.json() == {"sciezka": str(plik), "tresc": plik.read_text(encoding="utf-8"),
                          "rozmiar": plik.stat().st_size}


async def test_zapis_przez_kokpit_i_runnera_zmienia_plik_na_dysku_i_zachowuje_jego_prawa(
    zespol, projekt,
):
    plik = projekt.korzen / "skrypt.sh"
    plik.write_text("stare\n", encoding="utf-8")
    plik.chmod(0o640)

    odp = await _put(zespol, str(plik), "nowe\nlinie\n")

    assert odp.status_code == 200, odp.text
    assert odp.json() == {"sciezka": str(plik), "rozmiar": len(b"nowe\nlinie\n")}
    assert plik.read_bytes() == b"nowe\nlinie\n"
    assert stat.S_IMODE(plik.stat().st_mode) == 0o640
    assert _nazwy(projekt.korzen) == ["skrypt.sh"]


async def test_zapis_nowego_pliku_przez_kokpit_tworzy_go_z_prawami_0644(zespol, projekt):
    plik = projekt.korzen / "nowy.txt"
    stary_umask = os.umask(0o077)
    try:
        odp = await _put(zespol, str(plik), "zawartosc")
    finally:
        os.umask(stary_umask)

    assert odp.status_code == 200, odp.text
    assert plik.read_text(encoding="utf-8") == "zawartosc"
    assert stat.S_IMODE(plik.stat().st_mode) == 0o644
    assert _nazwy(projekt.korzen) == ["nowy.txt"]


async def test_zapisany_przez_kokpit_plik_mozna_odczytac_z_powrotem_bez_zmian(zespol, projekt):
    plik = projekt.korzen / "obieg.txt"
    tresc = "a\r\nb\n zażółć \n"

    zapis = await _put(zespol, str(plik), tresc)
    odczyt = await _get(zespol, str(plik))

    assert zapis.status_code == 200
    assert odczyt.json()["tresc"] == tresc
    assert plik.read_bytes() == tresc.encode("utf-8")


async def test_lista_plikow_przez_kokpit_pomija_sekrety_i_dowiazania_poza_zakres(
    zespol, projekt,
):
    cudzy = projekt.obcy / "cudzy.txt"
    cudzy.write_text(TRESC_SEKRETNA, encoding="utf-8")
    (projekt.korzen / "b.txt").write_text("x", encoding="utf-8")
    (projekt.korzen / "a.txt").write_text("x", encoding="utf-8")
    (projekt.korzen / ".env").write_text(TRESC_SEKRETNA, encoding="utf-8")
    (projekt.korzen / "wyjscie.txt").symlink_to(cudzy)

    odp = await zespol.http.get("/api/pliki/lista", params={"sciezka": str(projekt.korzen)})

    assert odp.status_code == 200, odp.text
    assert odp.json() == {
        "pliki": [{"sciezka": str(projekt.korzen / "a.txt"), "katalog": False},
                  {"sciezka": str(projekt.korzen / "b.txt"), "katalog": False}],
        "obcieto": False}


async def test_lista_plikow_przez_kokpit_stosuje_wzorzec_z_zapytania(zespol, projekt):
    (projekt.korzen / "a.txt").write_text("x", encoding="utf-8")
    (projekt.korzen / "b.py").write_text("x", encoding="utf-8")

    odp = await zespol.http.get("/api/pliki/lista",
                                params={"sciezka": str(projekt.korzen), "wzorzec": "*.py"})

    assert [p["sciezka"] for p in odp.json()["pliki"]] == [str(projekt.korzen / "b.py")]


async def test_zly_wzorzec_listy_przez_kokpit_to_409_z_czytelnym_komunikatem(zespol, projekt):
    odp = await zespol.http.get("/api/pliki/lista",
                                params={"sciezka": str(projekt.korzen), "wzorzec": "../*"})

    assert odp.status_code == 409
    assert odp.json()["detail"]


# ---- zakres: 403 i dowod na dysku --------------------------------------------------------

async def test_odczyt_pliku_poza_projektem_daje_403_bez_tresci_pliku(zespol, projekt):
    cudzy = projekt.obcy / "cudzy.txt"
    cudzy.write_text(TRESC_SEKRETNA, encoding="utf-8")

    odp = await _get(zespol, str(cudzy))

    assert odp.status_code == 403
    assert TRESC_SEKRETNA not in odp.text
    assert cudzy.read_text(encoding="utf-8") == TRESC_SEKRETNA


async def test_zapis_poza_projektem_jest_odrzucony_a_plik_poza_projektem_zostaje_nietkniety(
    zespol, projekt,
):
    cudzy = projekt.obcy / "cudzy.txt"
    cudzy.write_text("oryginal", encoding="utf-8")
    przed = cudzy.stat()

    odp = await _put(zespol, str(cudzy), "podmiana")

    assert odp.status_code == 403
    assert cudzy.read_text(encoding="utf-8") == "oryginal"
    assert cudzy.stat().st_mtime_ns == przed.st_mtime_ns
    assert _nazwy(projekt.obcy) == ["cudzy.txt"]


async def test_zapis_nowego_pliku_poza_projektem_jest_odrzucony_i_plik_nie_powstaje(
    zespol, projekt,
):
    cel = projekt.obcy / "nowy.txt"

    odp = await _put(zespol, str(cel), "x")

    assert odp.status_code == 403
    assert not cel.exists()
    assert _nazwy(projekt.obcy) == []


async def test_zapis_do_katalogu_git_projektu_daje_403_i_plik_nie_powstaje(zespol, projekt):
    (projekt.korzen / ".git").mkdir()
    cel = projekt.korzen / ".git" / "hooks_pre-commit"

    odp = await _put(zespol, str(cel), "#!/bin/sh\n")

    assert odp.status_code == 403
    assert not cel.exists()
    assert _nazwy(projekt.korzen / ".git") == []


async def test_odczyt_sekretu_z_projektu_daje_403_bez_tresci(zespol, projekt):
    (projekt.korzen / ".env").write_text(TRESC_SEKRETNA, encoding="utf-8")

    odp = await _get(zespol, str(projekt.korzen / ".env"))

    assert odp.status_code == 403
    assert TRESC_SEKRETNA not in odp.text


async def test_lista_katalogu_poza_projektem_daje_403(zespol, projekt):
    (projekt.obcy / "cudzy.txt").write_text("x", encoding="utf-8")

    odp = await zespol.http.get("/api/pliki/lista", params={"sciezka": str(projekt.obcy)})

    assert odp.status_code == 403
    assert "cudzy.txt" not in odp.text


async def test_sciezka_z_kropkami_wychodzaca_z_projektu_daje_403_i_nie_zmienia_pliku(
    zespol, projekt,
):
    cudzy = projekt.obcy / "cudzy.txt"
    cudzy.write_text("oryginal", encoding="utf-8")
    zakamuflowana = f"{projekt.korzen}/../obcy/cudzy.txt"

    odczyt = await _get(zespol, zakamuflowana)
    zapis = await _put(zespol, zakamuflowana, "podmiana")

    assert odczyt.status_code == 403
    assert zapis.status_code == 403
    assert cudzy.read_text(encoding="utf-8") == "oryginal"


# ---- bledy plikow: czytelne, nie wyjatek -------------------------------------------------

async def test_plik_binarny_daje_409_z_czytelnym_komunikatem_a_nie_blad_serwera(zespol, projekt):
    plik = projekt.korzen / "obraz.png"
    plik.write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff\xfe")

    odp = await _get(zespol, str(plik))

    assert odp.status_code == 409
    assert isinstance(odp.json()["detail"], str) and odp.json()["detail"]
    assert "Traceback" not in odp.text
    assert "PNG" not in odp.text


async def test_plik_wiekszy_niz_limit_odczytu_daje_409_z_czytelnym_komunikatem(
    zespol, projekt, monkeypatch,
):
    monkeypatch.setenv("COCKPIT_MAX_READ_KB", "1")
    plik = projekt.korzen / "duzy.txt"
    plik.write_text("a" * 2048, encoding="utf-8")

    odp = await _get(zespol, str(plik))

    assert odp.status_code == 409
    assert isinstance(odp.json()["detail"], str) and odp.json()["detail"]
    assert "aaaa" not in odp.text


async def test_odczyt_nieistniejacego_pliku_daje_409_a_nie_blad_serwera(zespol, projekt):
    odp = await _get(zespol, str(projekt.korzen / "brak.txt"))

    assert odp.status_code == 409
    assert odp.json()["detail"]


async def test_zapis_gdy_brakuje_katalogu_nadrzednego_daje_409_i_nie_tworzy_katalogu(
    zespol, projekt,
):
    odp = await _put(zespol, str(projekt.korzen / "nowy_kat" / "a.txt"), "x")

    assert odp.status_code == 409
    assert not (projekt.korzen / "nowy_kat").exists()


# ---- runner offline ----------------------------------------------------------------------

async def test_bez_runnera_online_odczyt_daje_409_z_komunikatem_trybu_tylko_do_odczytu(
    kokpit, projekt,
):
    konto = await kokpit.konto("ala@przyklad.pl", "Alfa")
    plik = projekt.korzen / "a.txt"
    plik.write_text("x", encoding="utf-8")

    odp = await _get(konto, str(plik))

    assert odp.status_code == 409
    assert odp.json() == {"detail": KOMUNIKAT_OFFLINE}


async def test_bez_runnera_online_zapis_daje_409_i_plik_zostaje_nietkniety(kokpit, projekt):
    konto = await kokpit.konto("ala@przyklad.pl", "Alfa")
    plik = projekt.korzen / "a.txt"
    plik.write_text("stare", encoding="utf-8")

    odp = await _put(konto, str(plik), "nowe")

    assert odp.status_code == 409
    assert odp.json() == {"detail": KOMUNIKAT_OFFLINE}
    assert plik.read_text(encoding="utf-8") == "stare"


async def test_bez_runnera_online_lista_daje_409_z_komunikatem_trybu_tylko_do_odczytu(
    kokpit, projekt,
):
    konto = await kokpit.konto("ala@przyklad.pl", "Alfa")

    odp = await konto.http.get("/api/pliki/lista", params={"sciezka": str(projekt.korzen)})

    assert odp.status_code == 409
    assert odp.json() == {"detail": KOMUNIKAT_OFFLINE}


# ---- uprawnienia i sesja -----------------------------------------------------------------

async def test_widz_nie_moze_zapisac_pliku_a_runner_nie_jest_nawet_pytany(
    kokpit, zespol, projekt, monkeypatch,
):
    plik = projekt.korzen / "a.txt"
    plik.write_text("stare", encoding="utf-8")
    widz = await kokpit.viewer("widz@przyklad.pl", zespol.tenant_id)
    zapytania_do_runnera: list[str] = []
    prawdziwa = bus.zapytaj

    async def liczaca(runner_id, typ, dane, timeout_s):
        zapytania_do_runnera.append(typ)
        return await prawdziwa(runner_id, typ, dane, timeout_s)

    monkeypatch.setattr(bus, "zapytaj", liczaca)

    odp = await _put(widz, str(plik), "podmiana")

    assert odp.status_code == 403
    assert plik.read_text(encoding="utf-8") == "stare"
    assert zapytania_do_runnera == []


async def test_widz_moze_czytac_i_listowac_pliki_projektu(kokpit, zespol, projekt):
    plik = projekt.korzen / "a.txt"
    plik.write_text("tresc", encoding="utf-8")
    widz = await kokpit.viewer("widz@przyklad.pl", zespol.tenant_id)

    odczyt = await _get(widz, str(plik))
    lista = await widz.http.get("/api/pliki/lista", params={"sciezka": str(projekt.korzen)})

    assert odczyt.status_code == 200
    assert odczyt.json()["tresc"] == "tresc"
    assert lista.status_code == 200
    assert [p["sciezka"] for p in lista.json()["pliki"]] == [str(plik)]


async def test_zapis_bez_tokenu_csrf_jest_odrzucony_a_plik_zostaje_nietkniety(zespol, projekt):
    plik = projekt.korzen / "a.txt"
    plik.write_text("stare", encoding="utf-8")

    odp = await zespol.http.put("/api/pliki", json={"sciezka": str(plik), "tresc": "nowe"})

    assert odp.status_code == 403
    assert plik.read_text(encoding="utf-8") == "stare"


async def test_bez_sesji_wszystkie_trasy_plikow_daja_401_a_plik_zostaje_nietkniety(
    kokpit, zespol, projekt,
):
    plik = projekt.korzen / "a.txt"
    plik.write_text("stare", encoding="utf-8")
    anonim = kokpit.klient()

    odczyt = await anonim.get("/api/pliki", params={"sciezka": str(plik)})
    lista = await anonim.get("/api/pliki/lista", params={"sciezka": str(projekt.korzen)})
    zapis = await anonim.put("/api/pliki", json={"sciezka": str(plik), "tresc": "nowe"})

    assert (odczyt.status_code, lista.status_code, zapis.status_code) == (401, 401, 401)
    assert "stare" not in odczyt.text
    assert plik.read_text(encoding="utf-8") == "stare"


# ---- wejscie ------------------------------------------------------------------------------

async def test_zbyt_dluga_sciezka_daje_422_a_nie_blad_serwera(zespol):
    odp = await _get(zespol, "/" + "a" * 4096)
    assert odp.status_code == 422


async def test_zapis_z_pusta_sciezka_daje_422(zespol):
    odp = await _put(zespol, "", "x")
    assert odp.status_code == 422


async def test_zapis_tresci_dluzszej_niz_3_mln_znakow_daje_422_i_nie_zmienia_pliku(
    zespol, projekt,
):
    plik = projekt.korzen / "a.txt"
    plik.write_text("stare", encoding="utf-8")

    odp = await _put(zespol, str(plik), "a" * (3 * 1024 * 1024 + 1))

    assert odp.status_code == 422
    assert plik.read_text(encoding="utf-8") == "stare"


async def test_zbyt_dlugi_wzorzec_listy_daje_422(zespol, projekt):
    odp = await zespol.http.get("/api/pliki/lista",
                                params={"sciezka": str(projekt.korzen), "wzorzec": "a" * 513})
    assert odp.status_code == 422


# ---- izolacja zespolow (tenant_id) ------------------------------------------------------------

async def test_zespol_bez_wlasnego_runnera_nie_czyta_plikow_przez_runnera_innego_zespolu(
    kokpit, projekt,
):
    alfa = await kokpit.konto("ala@przyklad.pl", "Alfa")
    beta = await kokpit.konto("bartek@przyklad.pl", "Beta")
    await kokpit.dodaj_runnera("runner-beta", beta.tenant_id)
    await kokpit.uruchom_runnera("runner-beta")      # online jest TYLKO runner zespolu Beta
    plik = projekt.korzen / "a.txt"
    plik.write_text("tresc", encoding="utf-8")

    odp_alfa = await _get(alfa, str(plik))
    odp_beta = await _get(beta, str(plik))

    assert odp_alfa.status_code == 409
    assert odp_alfa.json() == {"detail": KOMUNIKAT_OFFLINE}
    assert "tresc" not in odp_alfa.text
    assert odp_beta.status_code == 200
    assert odp_beta.json()["tresc"] == "tresc"


async def test_zespol_bez_wlasnego_runnera_nie_zapisuje_plikow_przez_runnera_innego_zespolu(
    kokpit, projekt,
):
    alfa = await kokpit.konto("ala@przyklad.pl", "Alfa")
    beta = await kokpit.konto("bartek@przyklad.pl", "Beta")
    await kokpit.dodaj_runnera("runner-beta", beta.tenant_id)
    await kokpit.uruchom_runnera("runner-beta")
    plik = projekt.korzen / "a.txt"
    plik.write_text("stare", encoding="utf-8")

    odp = await _put(alfa, str(plik), "podmiana")

    assert odp.status_code == 409
    assert plik.read_text(encoding="utf-8") == "stare"


# =================================================================================
# 4. Dysk odmawia dostepu w trakcie ustalania sciezki (`_rozwiaz`)
# =================================================================================

def _dysk_odmawia_dla(monkeypatch, metoda: str, nazwa: str, blad: OSError) -> None:
    """`Zakres.rozwiaz` dotyka dysku (`resolve`, `is_symlink`); odmowa tylko dla elementu o danej nazwie."""
    oryginal = getattr(Path, metoda)

    def podmiana(self, *a, **k):
        if self.name == nazwa:
            raise blad
        return oryginal(self, *a, **k)

    monkeypatch.setattr(Path, metoda, podmiana)


def _odmowa_dysku() -> PermissionError:
    return PermissionError(errno.EACCES, f"odmowa {SEKRET}")


def _bez_wycieku(odp: dict) -> None:
    assert SEKRET not in odp["blad"]
    assert "Errno" not in odp["blad"]


def _sciezki(odp: dict) -> list[str]:
    assert odp["ok"] is True, odp
    return [p["sciezka"] for p in odp["wynik"]["pliki"]]


@pytest.mark.skipif(JEST_ROOT, reason="root czyta katalogi bez wzgledu na prawa")
@pytest.mark.parametrize("wzorzec", ["**/*.txt", "*/*", "*/*.txt"])
async def test_lista_nie_wchodzi_w_dowiazanie_do_katalogu_poza_projektem(projekt, wzorzec):
    """Zamkniety katalog za dowiazaniem poza zakresem: ani `**`, ani skladnik posredni nie wchodzi."""
    (projekt.korzen / "a").mkdir()
    (projekt.korzen / "a" / "x.txt").write_text("x", encoding="utf-8")
    zamkniety = projekt.obcy / "zamkniety"
    zamkniety.mkdir()
    (projekt.korzen / "wyjscie").symlink_to(projekt.obcy, target_is_directory=True)
    zamkniety.chmod(0o000)
    try:
        odp = await _wyk("list_files", sciezka=str(projekt.korzen), wzorzec=wzorzec)
    finally:
        zamkniety.chmod(0o755)

    assert _sciezki(odp) == [str((projekt.korzen / "a" / "x.txt").resolve())]


def _licz_scandir(monkeypatch) -> list[str]:
    """Rejestr katalogow, do ktorych runner wszedl (`os.scandir`)."""
    wejscia: list[str] = []
    oryginal = os.scandir

    def podmiana(sciezka=".", *a, **k):
        wejscia.append(str(sciezka))
        return oryginal(sciezka, *a, **k)

    monkeypatch.setattr(os, "scandir", podmiana)
    return wejscia


async def test_skladnik_posredni_nie_wchodzi_do_czytelnego_katalogu_poza_zakresem(projekt, monkeypatch):
    """Zakres sprawdzany PRZED wejsciem do katalogu, nie tylko na wynikach."""
    (projekt.obcy / "tajne.txt").write_text("x", encoding="utf-8")
    (projekt.korzen / "wyjscie").symlink_to(projekt.obcy, target_is_directory=True)
    wejscia = _licz_scandir(monkeypatch)

    odp = await _wyk("list_files", sciezka=str(projekt.korzen), wzorzec="wyjscie/*")

    assert _sciezki(odp) == []
    # runner wchodzi sciezka dowiazania (`.../wyjscie`), nie celem - sprawdzamy po nazwie dowiazania
    assert not [w for w in wejscia if "wyjscie" in Path(w).parts]


async def test_skladnik_posredni_przeszukuje_dowiazanie_do_katalogu_w_projekcie(projekt):
    (projekt.korzen / "src").mkdir()
    (projekt.korzen / "src" / "m.py").write_text("x", encoding="utf-8")
    (projekt.korzen / "link").symlink_to(projekt.korzen / "src", target_is_directory=True)

    odp = await _wyk("list_files", sciezka=str(projekt.korzen), wzorzec="link/*.py")

    assert _sciezki(odp) == [str((projekt.korzen / "src" / "m.py").resolve())]


async def test_kolejne_gwiazdki_rekurencyjne_nie_mnoza_przegladanych_katalogow(projekt, monkeypatch):
    katalog = projekt.korzen
    for i in range(10):
        katalog = katalog / f"d{i}"
        katalog.mkdir()
    (katalog / "x").write_text("x", encoding="utf-8")
    wejscia = _licz_scandir(monkeypatch)

    odp = await _wyk("list_files", sciezka=str(projekt.korzen), wzorzec="**/" * 20 + "x")

    assert _sciezki(odp) == [str((katalog / "x").resolve())]
    assert len(wejscia) <= 2 * 11   # 11 katalogow: raz dla `**`, raz dla `x`


async def test_kolejne_gwiazdki_po_innym_skladniku_tez_sa_zwijane(projekt, monkeypatch):
    katalog = projekt.korzen
    for i in range(10):
        katalog = katalog / f"d{i}"
        katalog.mkdir()
    (katalog / "x").write_text("x", encoding="utf-8")
    wejscia = _licz_scandir(monkeypatch)

    odp = await _wyk("list_files", sciezka=str(projekt.korzen), wzorzec="d0/" + "**/" * 20 + "x")

    assert _sciezki(odp) == [str((katalog / "x").resolve())]
    assert len(wejscia) <= 1 + 2 * 10   # korzen dla `d0`, potem 10 katalogow dla `**` i dla `x`


async def test_plik_pasujacy_do_skladnika_posredniego_nie_jest_przeszukiwany_jak_katalog(projekt):
    (projekt.korzen / "plik.txt").write_text("x", encoding="utf-8")
    (projekt.korzen / "a").mkdir()
    (projekt.korzen / "a" / "x.txt").write_text("x", encoding="utf-8")

    odp = await _wyk("list_files", sciezka=str(projekt.korzen), wzorzec="*/*")

    assert _sciezki(odp) == [str((projekt.korzen / "a" / "x.txt").resolve())]


async def test_katalog_osiagniety_kilkoma_drogami_jest_przegladany_raz_na_skladnik(projekt, monkeypatch):
    katalog = projekt.korzen
    for _ in range(3):
        katalog = katalog / "a"
        katalog.mkdir()
    (katalog / "x").write_text("x", encoding="utf-8")
    wejscia = _licz_scandir(monkeypatch)

    odp = await _wyk("list_files", sciezka=str(projekt.korzen), wzorzec="**/a/**/x")

    assert _sciezki(odp) == [str((katalog / "x").resolve())]
    # 4 katalogi: `**` (4) + `a` (4) + `**` z 3 trafien (3) + `x` (3); bez deduplikacji 20
    assert len(wejscia) <= 14


async def test_rekurencyjna_lista_przeszukuje_kazdy_katalog_dopasowany_wczesniej(projekt):
    for katalog in ("a", "b"):
        (projekt.korzen / katalog / "g").mkdir(parents=True)
        (projekt.korzen / katalog / "g" / f"{katalog}.txt").write_text("x", encoding="utf-8")

    odp = await _wyk("list_files", sciezka=str(projekt.korzen), wzorzec="*/**/*.txt")

    assert _sciezki(odp) == [
        str((projekt.korzen / "a" / "g" / "a.txt").resolve()),
        str((projekt.korzen / "b" / "g" / "b.txt").resolve()),
    ]


@pytest.mark.parametrize("wzorzec", ["./*.txt", "src//*.txt", "src/./*.txt"])
async def test_pusty_skladnik_i_kropka_we_wzorcu_nie_zmieniaja_dopasowania(projekt, wzorzec):
    (projekt.korzen / "src").mkdir()
    (projekt.korzen / "a.txt").write_text("x", encoding="utf-8")
    (projekt.korzen / "src" / "b.txt").write_text("x", encoding="utf-8")
    oczekiwany = "a.txt" if wzorzec.startswith("./") else "src/b.txt"

    odp = await _wyk("list_files", sciezka=str(projekt.korzen), wzorzec=wzorzec)

    assert _sciezki(odp) == [str((projekt.korzen / oczekiwany).resolve())]


# `Zakres.rozwiaz` (HAR-29) sam zamienia blad `resolve()` na odmowe zakresu (fail-closed),
# wiec do `_rozwiaz` dociera tylko blad `is_symlink()` przy zapisie.

async def test_odczyt_gdy_dysk_odmawia_przy_ustalaniu_sciezki_jest_odmowa_zakresu_bez_wycieku(
    projekt, monkeypatch,
):
    plik = projekt.korzen / "zamkniete.txt"
    plik.write_text("tresc", encoding="utf-8")
    _dysk_odmawia_dla(monkeypatch, "resolve", "zamkniete.txt", _odmowa_dysku())

    odp = await _wyk("read_file", sciezka=str(plik))

    _blad(odp, "poza_zakresem")
    _bez_wycieku(odp)
    assert "tresc" not in json.dumps(odp)


async def test_zapis_gdy_dysk_odmawia_przy_ustalaniu_celu_jest_odmowa_zakresu_i_plik_bez_zmian(
    projekt, monkeypatch,
):
    plik = projekt.korzen / "cel.txt"
    plik.write_text("stare", encoding="utf-8")
    _dysk_odmawia_dla(monkeypatch, "resolve", "cel.txt", _odmowa_dysku())

    odp = await _wyk("write_file", sciezka=str(plik), tresc="nowe")

    _blad(odp, "poza_zakresem")
    _bez_wycieku(odp)
    assert plik.read_text(encoding="utf-8") == "stare"
    assert _nazwy(projekt.korzen) == ["cel.txt"]


async def test_zapis_gdy_dysk_odmawia_przy_sprawdzaniu_dowiazania_to_brak_uprawnien_i_plik_bez_zmian(
    projekt, monkeypatch,
):
    plik = projekt.korzen / "cel.txt"
    plik.write_text("stare", encoding="utf-8")
    _dysk_odmawia_dla(monkeypatch, "is_symlink", "cel.txt", _odmowa_dysku())

    odp = await _wyk("write_file", sciezka=str(plik), tresc="nowe")

    _blad(odp, "brak_uprawnien")
    _bez_wycieku(odp)
    assert plik.read_text(encoding="utf-8") == "stare"
    assert _nazwy(projekt.korzen) == ["cel.txt"]


async def test_inny_blad_dysku_przy_sprawdzaniu_dowiazania_to_blad_dysku_a_nie_brak_uprawnien(
    projekt, monkeypatch,
):
    plik = projekt.korzen / "cel.txt"
    plik.write_text("stare", encoding="utf-8")
    blad_io = OSError(errno.EIO, f"blad wejscia-wyjscia {SEKRET}")
    _dysk_odmawia_dla(monkeypatch, "is_symlink", "cel.txt", blad_io)

    odp = await _wyk("write_file", sciezka=str(plik), tresc="nowe")

    _blad(odp, "blad_dysku")
    _bez_wycieku(odp)
    assert plik.read_text(encoding="utf-8") == "stare"


async def test_lista_gdy_dysk_odmawia_przy_ustalaniu_katalogu_bazowego_jest_odmowa_zakresu(
    projekt, monkeypatch,
):
    katalog = projekt.korzen / "zamkniety_katalog"
    katalog.mkdir()
    (katalog / "a.txt").write_text("x", encoding="utf-8")
    _dysk_odmawia_dla(monkeypatch, "resolve", "zamkniety_katalog", _odmowa_dysku())

    odp = await _wyk("list_files", sciezka=str(katalog))

    _blad(odp, "poza_zakresem")
    _bez_wycieku(odp)


# =================================================================================
# 5. Wzorce listy plikow: koncowe `**` i wielkosc liter
# =================================================================================

async def test_wzorzec_konczacy_sie_na_gwiazdki_gwiazdki_zwraca_katalog_bazowy_i_podkatalogi_bez_plikow(
    projekt,
):
    """Regula: `**` na koncu wzorca oznacza katalogi (baza i wszystkie podkatalogi), nigdy pliki
    (jak `Path.glob` do Pythona 3.12). Baza jest wpisem listy, bo `**` obejmuje zero poziomow."""
    (projekt.korzen / "a" / "b").mkdir(parents=True)
    (projekt.korzen / "a" / "b" / "plik.txt").write_text("x", encoding="utf-8")
    (projekt.korzen / "a" / "inny.txt").write_text("x", encoding="utf-8")

    odp = await _wyk("list_files", sciezka=str(projekt.korzen), wzorzec="**")

    assert odp["ok"] is True, odp
    assert odp["wynik"] == {
        "pliki": [
            {"sciezka": str(projekt.korzen), "katalog": True},
            {"sciezka": str(projekt.korzen / "a"), "katalog": True},
            {"sciezka": str(projekt.korzen / "a" / "b"), "katalog": True},
        ],
        "obcieto": False,
    }


async def test_wzorzec_rozroznia_wielkosc_liter_w_nazwach_plikow(projekt):
    """Regula implementacji: `*.TXT` nie obejmuje `a.txt` (fnmatchcase, nie fnmatch zalezny od systemu)."""
    (projekt.korzen / "a.txt").write_text("x", encoding="utf-8")
    (projekt.korzen / "b.TXT").write_text("x", encoding="utf-8")

    odp = await _wyk("list_files", sciezka=str(projekt.korzen), wzorzec="*.TXT")

    assert odp["ok"] is True, odp
    assert [p["sciezka"] for p in odp["wynik"]["pliki"]] == [str(projekt.korzen / "b.TXT")]
