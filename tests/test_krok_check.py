"""HAR-177, czesc 1: krok systemowy `system: check` - dowod silnika zamiast slowa agenta (TDD).

Silnik bez LLM uruchamia komende capability z `.cockpit/project.yaml` w worktree i zapisuje
w katalogu runu `<krok>/dowod-<capability>.json`: `source: engine`, kod wyjscia, sha256 PELNEGO
wyjscia, ogon (ostatnie `LIMIT_OGONA` bajtow) i czasy. Recenzent dostaje ten plik przez `consumes`.

Prawdziwy kod: `wykonaj_check` na prawdziwych procesach (komendy `python -c ...` przez
`sys.executable`, bez npm) oraz `wykonaj_run` z wstrzyknietym agentem (`Szpieg` z
`test_silnik_runu`). Oczekiwane skroty i ogony wyliczone w tescie z bajtow, nie funkcja pod
testem. Kazde czekanie ma limit czasu; testy nie wolaja `os.kill`/`os.killpg` - proces zabija
sam `wykonaj_check`, test sprawdza skutek (znacznik zapisany przez potomka po czasie).
Nazwy testow w jezyku dziedziny (CLAUDE.md).
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import shlex
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from procesy import czekaj, czekaj_na_warunek, uruchom_proces, zabij_proces, zakoncz_proces
from runner.dowody import (
    LIMIT_CHECKU_S,
    LIMIT_DRENAZU_S,
    LIMIT_OGONA,
    OPCJE_JSON,
    ZRODLO,
    _czytaj,
    _Wyjscie,
    wykonaj_check,
)
from runner.krok import ROZMIAR_KAWALKA, WynikKroku
from test_silnik_runu import K, RUN_ID, Szpieg, indeks, jedno, rodzaje, swiat, uruchom

LIMIT_TESTU_S = 60.0
KORZEN_REPO = Path(__file__).resolve().parents[1]
KLUCZE_PLIKU = {
    "capability", "argv", "exit_code", "stdout_tail", "sha256", "started_at", "finished_at",
    "source",
}
KLUCZE_ZDARZENIA = {
    "capability", "exit_code", "sha256", "started_at", "finished_at", "source", "plik",
}
DOWOD_TYPECHECK = "dowod-typecheck.json"
ZASTEPCZY = chr(0xFFFD)   # znak zastepczy dekodera UTF-8
EVIDENCE_PUSTY = (
    "check typecheck: niezmiennik evidence_required: komenda nie zwrocila zadnego wyniku")


def komenda(kod: str, *argumenty: str) -> str:
    """Komenda capability: ten sam interpreter, kod w `-c`, cytowanie jak w project.yaml."""
    argumenty = [shlex.quote(a) for a in argumenty]
    return " ".join([shlex.quote(sys.executable), "-c", shlex.quote(kod), *argumenty])


def sha(dane: bytes) -> str:
    return hashlib.sha256(dane).hexdigest()


def pliki_pod(korzen: Path) -> list[str]:
    """Wszystkie wpisy pod katalogiem (takze pliki tymczasowe); brak katalogu = pusto."""
    if not korzen.exists():
        return []
    return sorted(str(p.relative_to(korzen)) for p in korzen.rglob("*"))


def wczytaj_dowod(katalog: Path, capability: str = "typecheck") -> dict:
    surowy = (katalog / f"dowod-{capability}.json").read_bytes()
    return json.loads(surowy.decode("utf-8"))


@dataclass
class Swiat:
    worktree: Path
    katalog: Path        # katalog kroku w katalogu runu, poza repozytorium


@pytest.fixture
def sw(tmp_path: Path) -> Swiat:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    return Swiat(worktree=worktree, katalog=tmp_path / "run" / "typecheck")


async def sprawdz(
    sw: Swiat, polecenie: str, *, capability: str = "typecheck", limit_s: float | None = None,
    katalog: Path | None = None, bezpiecznik_s: float = LIMIT_TESTU_S,
) -> WynikKroku:
    opcje = {} if limit_s is None else {"limit_s": limit_s}
    return await czekaj(
        wykonaj_check(sw.worktree, capability, polecenie, katalog or sw.katalog, **opcje),
        bezpiecznik_s, "wykonaj_check")


# --- stale i podpis --------------------------------------------------------------------------


def test_stale_dowodu_silnika_maja_ustalone_wartosci():
    assert LIMIT_OGONA == 4000
    assert LIMIT_CHECKU_S == 600.0
    assert ZRODLO == "engine"
    assert LIMIT_DRENAZU_S == 5.0
    assert OPCJE_JSON == {"ensure_ascii": False, "indent": 2}


def test_domyslny_limit_czasu_checku_to_stala_a_nie_liczba_wpisana_w_podpisie():
    parametr = inspect.signature(wykonaj_check).parameters["limit_s"]

    assert parametr.default == LIMIT_CHECKU_S
    assert parametr.kind is inspect.Parameter.KEYWORD_ONLY


# --- zielony check ---------------------------------------------------------------------------


async def test_zielony_check_konczy_krok_sukcesem_a_plik_dowodu_ma_dokladnie_ustalone_klucze(sw):
    kod = "print('typy ok')"
    polecenie = komenda(kod)

    wynik = await sprawdz(sw, polecenie)

    assert (wynik.status, wynik.powod, wynik.kod_wyjscia) == ("zakonczony", None, 0)
    dowod = wczytaj_dowod(sw.katalog)
    assert set(dowod) == KLUCZE_PLIKU
    assert dowod["capability"] == "typecheck"
    assert dowod["argv"] == [sys.executable, "-c", kod]
    assert dowod["exit_code"] == 0
    assert dowod["stdout_tail"] == "typy ok\n"
    assert dowod["sha256"] == sha(b"typy ok\n")
    assert dowod["source"] == "engine"


async def test_dowod_check_nazywa_plik_wedlug_capability_a_nie_wedlug_kroku(sw):
    wynik = await sprawdz(sw, komenda("print('k')"), capability="contract")

    assert wynik.status == "zakonczony"
    assert pliki_pod(sw.katalog) == ["dowod-contract.json"]
    assert wczytaj_dowod(sw.katalog, "contract")["capability"] == "contract"


async def test_dwie_capability_w_jednym_katalogu_maja_osobne_pliki_dowodu(sw):
    await sprawdz(sw, komenda("print('typy')"), capability="typecheck")
    await sprawdz(sw, komenda("print('kontrakt')"), capability="contract")

    assert pliki_pod(sw.katalog) == ["dowod-contract.json", "dowod-typecheck.json"]
    assert wczytaj_dowod(sw.katalog, "typecheck")["stdout_tail"] == "typy\n"
    assert wczytaj_dowod(sw.katalog, "contract")["stdout_tail"] == "kontrakt\n"


async def test_dowod_zapisany_w_katalogu_kroku_nie_zostawia_plikow_tymczasowych(sw):
    await sprawdz(sw, komenda("print('ok')"))

    assert pliki_pod(sw.katalog) == [DOWOD_TYPECHECK]


async def test_metadane_dowodu_w_wyniku_kroku_zgadzaja_sie_z_plikiem_i_nie_niosa_wyjscia_ani_argv(sw):
    wynik = await sprawdz(sw, komenda("print('tajne wyjscie')", "SEKRET-W-ARGV"))

    assert wynik.dowod is not None
    metadane = dict(wynik.dowod)
    assert set(metadane) == KLUCZE_ZDARZENIA
    plik = wczytaj_dowod(sw.katalog)
    for klucz in ("capability", "exit_code", "sha256", "started_at", "finished_at", "source"):
        assert metadane[klucz] == plik[klucz]
    assert metadane["plik"] == DOWOD_TYPECHECK
    assert "stdout_tail" not in metadane and "argv" not in metadane
    assert "tajne wyjscie" not in json.dumps(metadane)
    assert "SEKRET-W-ARGV" not in json.dumps(metadane)


async def test_czasy_dowodu_to_iso_utc_a_komenda_miesci_sie_miedzy_nimi(sw):
    przed = datetime.now(timezone.utc)

    await sprawdz(sw, komenda("import time; time.sleep(0.4); print('ok')"))

    po = datetime.now(timezone.utc)
    dowod = wczytaj_dowod(sw.katalog)
    start = datetime.fromisoformat(dowod["started_at"])
    koniec = datetime.fromisoformat(dowod["finished_at"])
    assert start.utcoffset() == timedelta(0) and koniec.utcoffset() == timedelta(0)
    assert przed <= start <= koniec <= po
    assert (koniec - start).total_seconds() >= 0.35


async def test_komenda_checku_dziala_w_katalogu_worktree(sw):
    await sprawdz(sw, komenda("import os; print(os.getcwd())"))

    katalog_komendy = wczytaj_dowod(sw.katalog)["stdout_tail"].strip()
    assert Path(katalog_komendy).resolve() == sw.worktree.resolve()


# --- ogon i skrot ----------------------------------------------------------------------------

KOD_DUZEGO_WYJSCIA = (
    "import sys\n"
    "for i in range(40000):\n"
    "    print('linia', i)\n"
    "sys.stdout.flush()\n"
    "sys.stderr.write('koniec na stderr\\n')\n"
)


async def test_skrot_obejmuje_cale_wyjscie_a_ogon_tylko_ostatnie_bajty_dluzszego_wyjscia(sw):
    pelne = ("".join(f"linia {i}\n" for i in range(40000)) + "koniec na stderr\n").encode()
    assert len(pelne) > 4 * 65536 and len(pelne) > LIMIT_OGONA

    await sprawdz(sw, komenda(KOD_DUZEGO_WYJSCIA))

    dowod = wczytaj_dowod(sw.katalog)
    assert dowod["sha256"] == sha(pelne)
    assert dowod["sha256"] != sha(pelne[-LIMIT_OGONA:])
    assert dowod["stdout_tail"] == pelne[-LIMIT_OGONA:].decode("utf-8")
    assert len(dowod["stdout_tail"].encode("utf-8")) == LIMIT_OGONA


async def test_wyjscie_dokladnie_rowne_limitowi_ogona_trafia_do_ogona_w_calosci(sw):
    kod = f"import sys; sys.stdout.write('a' * {LIMIT_OGONA - 1} + 'z')"

    await sprawdz(sw, komenda(kod))

    dowod = wczytaj_dowod(sw.katalog)
    assert dowod["stdout_tail"] == "a" * (LIMIT_OGONA - 1) + "z"


async def test_wyjscie_o_bajt_dluzsze_od_limitu_ogona_traci_w_ogonie_pierwszy_bajt(sw):
    kod = f"import sys; sys.stdout.write('p' + 'a' * {LIMIT_OGONA - 1} + 'z')"

    await sprawdz(sw, komenda(kod))

    dowod = wczytaj_dowod(sw.katalog)
    assert dowod["stdout_tail"] == "a" * (LIMIT_OGONA - 1) + "z"
    assert dowod["sha256"] == sha(("p" + "a" * (LIMIT_OGONA - 1) + "z").encode())


async def test_krotkie_wyjscie_trafia_do_ogona_w_calosci_razem_z_stderr(sw):
    kod = (
        "import sys; sys.stdout.write('na stdout\\n'); sys.stdout.flush(); "
        "sys.stderr.write('na stderr\\n')"
    )

    await sprawdz(sw, komenda(kod))

    dowod = wczytaj_dowod(sw.katalog)
    assert dowod["stdout_tail"] == "na stdout\nna stderr\n"
    assert dowod["sha256"] == sha(b"na stdout\nna stderr\n")


async def test_ogon_zastepuje_bajty_niepoprawne_w_utf8_a_skrot_liczy_surowe_bajty(sw):
    kod = "import sys; sys.stdout.buffer.write(b'przed \\xff po\\n')"

    await sprawdz(sw, komenda(kod))

    dowod = wczytaj_dowod(sw.katalog)
    assert dowod["stdout_tail"] == f"przed {ZASTEPCZY} po\n"
    assert dowod["sha256"] == sha(b"przed \xff po\n")


async def test_ogon_uciety_w_srodku_znaku_utf8_zaczyna_sie_od_znaku_zastepczego(sw):
    kod = "import sys; sys.stdout.buffer.write(('\\u017c' * 2500 + '\\n').encode('utf-8'))"
    surowe = ("ż" * 2500 + "\n").encode("utf-8")
    assert len(surowe) == 5001 and (len(surowe) - LIMIT_OGONA) % 2 == 1   # cecha w polowie znaku

    await sprawdz(sw, komenda(kod))

    dowod = wczytaj_dowod(sw.katalog)
    assert dowod["stdout_tail"] == surowe[-LIMIT_OGONA:].decode("utf-8", errors="replace")
    assert dowod["stdout_tail"].startswith(ZASTEPCZY)


# --- czerwony check i brak dowodu ------------------------------------------------------------


async def test_czerwony_check_konczy_krok_porazka_z_kodem_wyjscia_a_dowod_zapisuje_ten_kod(sw):
    wynik = await sprawdz(sw, komenda("import sys; print('blad typow'); sys.exit(3)"))

    assert (wynik.status, wynik.powod, wynik.kod_wyjscia) == (
        "nieudany", "check typecheck: kod wyjścia 3", 3)
    assert wynik.dowod is not None and wynik.dowod["exit_code"] == 3
    dowod = wczytaj_dowod(sw.katalog)
    assert dowod["exit_code"] == 3
    assert dowod["stdout_tail"] == "blad typow\n"
    assert dowod["sha256"] == sha(b"blad typow\n")
    assert dowod["source"] == "engine"


async def test_czerwony_check_bez_wyjscia_to_porazka_kodu_wyjscia_a_nie_brak_wyniku(sw):
    wynik = await sprawdz(sw, komenda("import sys; sys.exit(1)"))

    assert (wynik.status, wynik.powod, wynik.kod_wyjscia) == (
        "nieudany", "check typecheck: kod wyjścia 1", 1)
    assert wczytaj_dowod(sw.katalog)["exit_code"] == 1
    assert wczytaj_dowod(sw.katalog)["sha256"] == sha(b"")


async def test_kod_zero_z_pustym_wyjsciem_to_brak_dowodu_evidence_required_a_dowod_jest_zapisany(sw):
    wynik = await sprawdz(sw, komenda("pass"))

    assert (wynik.status, wynik.powod, wynik.kod_wyjscia) == ("nieudany", EVIDENCE_PUSTY, 0)
    assert wynik.dowod is not None and wynik.dowod["exit_code"] == 0
    dowod = wczytaj_dowod(sw.katalog)
    assert (dowod["exit_code"], dowod["stdout_tail"], dowod["sha256"]) == (0, "", sha(b""))


@pytest.mark.parametrize("biale", ["   ", "\n\n", "\t \n"])
async def test_kod_zero_z_samymi_bialymi_znakami_to_brak_dowodu_evidence_required(sw, biale):
    wynik = await sprawdz(sw, komenda(f"import sys; sys.stdout.write({biale!r})"))

    assert (wynik.status, wynik.powod, wynik.kod_wyjscia) == ("nieudany", EVIDENCE_PUSTY, 0)
    assert wczytaj_dowod(sw.katalog)["sha256"] == sha(biale.encode())


async def test_wyjscie_z_trescia_tylko_przed_ogonem_bialych_znakow_nadal_jest_dowodem(sw):
    # ogon (ostatnie LIMIT_OGONA bajtow) to same spacje, ale komenda wypisala znak niebialy
    kod = f"import sys; sys.stdout.write('x' + ' ' * {LIMIT_OGONA + 1000})"

    wynik = await sprawdz(sw, komenda(kod))

    assert (wynik.status, wynik.powod, wynik.kod_wyjscia) == ("zakonczony", None, 0)
    assert wczytaj_dowod(sw.katalog)["stdout_tail"] == " " * LIMIT_OGONA


@pytest.mark.parametrize(
    "kod",
    [
        "import sys; sys.stdout.write(' ' * 200000 + 'ok')",     # tresc dopiero w pozniejszym kawalku
        "import sys; sys.stdout.write('ok' + ' ' * 200000)",    # biale znaki po tresci w innym kawalku
    ],
)
async def test_tresc_wyjscia_w_dowolnym_kawalku_strumienia_wystarcza_za_dowod(sw, kod):
    wynik = await sprawdz(sw, komenda(kod))

    assert (wynik.status, wynik.powod, wynik.kod_wyjscia) == ("zakonczony", None, 0)


# Stala lista bialych znakow Unicode spoza ASCII - niezalezna od kodu pod testem.
BIALE_UNICODE = [" ", " ", "　", "\u0085"]


def kod_z_bajtami(surowe: bytes) -> str:
    """Kod komendy zapisujacej dokladnie te bajty na stdout, bez zaleznosci od kodowania."""
    return f"import sys; sys.stdout.buffer.write(bytes.fromhex({surowe.hex()!r}))"


@pytest.mark.parametrize("znak", BIALE_UNICODE)
async def test_kod_zero_z_samymi_bialymi_znakami_unicode_to_brak_dowodu_evidence_required(sw, znak):
    surowe = (znak * 3 + "\n").encode("utf-8")

    wynik = await sprawdz(sw, komenda(kod_z_bajtami(surowe)))

    assert (wynik.status, wynik.powod, wynik.kod_wyjscia) == ("nieudany", EVIDENCE_PUSTY, 0)
    assert wczytaj_dowod(sw.katalog)["sha256"] == sha(surowe)


async def test_kod_zero_z_jedna_spacja_nielamliwa_i_nowa_linia_to_brak_dowodu(sw):
    surowe = " \n".encode("utf-8")

    wynik = await sprawdz(sw, komenda(kod_z_bajtami(surowe)))

    assert (wynik.status, wynik.powod, wynik.kod_wyjscia) == ("nieudany", EVIDENCE_PUSTY, 0)


async def test_kod_zero_ze_zwyklymi_spacjami_i_nowa_linia_nadal_jest_brakiem_dowodu(sw):
    surowe = b"  \n"

    wynik = await sprawdz(sw, komenda(kod_z_bajtami(surowe)))

    assert (wynik.status, wynik.powod, wynik.kod_wyjscia) == ("nieudany", EVIDENCE_PUSTY, 0)
    assert wczytaj_dowod(sw.katalog)["sha256"] == sha(surowe)


@pytest.mark.parametrize("znak", BIALE_UNICODE)
async def test_jedyna_tresc_otoczona_bialymi_znakami_unicode_nadal_jest_dowodem(sw, znak):
    surowe = (znak * 5 + "x" + znak * 5).encode("utf-8")

    wynik = await sprawdz(sw, komenda(kod_z_bajtami(surowe)))

    assert (wynik.status, wynik.powod, wynik.kod_wyjscia) == ("zakonczony", None, 0)
    assert wczytaj_dowod(sw.katalog)["sha256"] == sha(surowe)


async def test_bajty_niebedace_utf8_przy_kodzie_zero_sa_trescia_a_nie_pustka(sw):
    wynik = await sprawdz(sw, komenda(kod_z_bajtami(b"\xff")))

    assert (wynik.status, wynik.powod, wynik.kod_wyjscia) == ("zakonczony", None, 0)
    assert wczytaj_dowod(sw.katalog)["stdout_tail"] == ZASTEPCZY


async def test_bajty_niebedace_utf8_otoczone_spacjami_nielamliwymi_sa_trescia(sw):
    surowe = " ".encode("utf-8") + b"\xff" + " \n".encode("utf-8")

    wynik = await sprawdz(sw, komenda(kod_z_bajtami(surowe)))

    assert (wynik.status, wynik.powod, wynik.kod_wyjscia) == ("zakonczony", None, 0)


# --- granice kawalkow: znak wielobajtowy rozciety miedzy kawalkami ---------------------------


async def czytaj_kawalkami(*dane: bytes) -> _Wyjscie:
    """Prawdziwy `_czytaj`; wszystko zasilone jest juz w buforze, wiec `read(ROZMIAR_KAWALKA)`
    tnie dokladnie co ROZMIAR_KAWALKA bajtow."""
    strumien = asyncio.StreamReader(limit=4 * ROZMIAR_KAWALKA)
    for kawalek in dane:
        strumien.feed_data(kawalek)
    strumien.feed_eof()
    wyjscie = _Wyjscie()
    await czekaj(_czytaj(strumien, wyjscie), LIMIT_TESTU_S, "_czytaj")
    return wyjscie


async def test_spacja_nielamliwa_rozcieta_na_granicy_kawalkow_jest_nadal_pustka():
    # pierwszy kawalek konczy sie bajtem 0xC2, drugi zaczyna od 0xA0
    pierwszy = b" " * (ROZMIAR_KAWALKA - 1) + b"\xc2"
    wyjscie = await czytaj_kawalkami(pierwszy, b"\xa0\n")

    assert wyjscie.ma_tresc is False
    assert wyjscie.skrot.hexdigest() == sha(pierwszy + b"\xa0\n")


async def test_spacje_nielamliwe_rozciete_na_granicy_kawalkow_w_srodku_wyjscia_sa_pustka():
    pierwszy = b" " + " ".encode("utf-8") * (ROZMIAR_KAWALKA // 2 - 1) + b"\xc2"
    assert len(pierwszy) == ROZMIAR_KAWALKA
    wyjscie = await czytaj_kawalkami(pierwszy, b"\xa0" + " \n".encode("utf-8"))

    assert wyjscie.ma_tresc is False


async def test_tresc_po_znaku_rozcietym_na_granicy_kawalkow_nadal_jest_dowodem():
    pierwszy = b" " * (ROZMIAR_KAWALKA - 1) + b"\xc2"
    wyjscie = await czytaj_kawalkami(pierwszy, b"\xa0x")

    assert wyjscie.ma_tresc is True


async def test_niepelny_znak_na_samym_koncu_wyjscia_po_spacji_nielamliwej_jest_trescia():
    # po EOF dekoder oddaje U+FFFD zamiast cichej utraty ostatniego bajtu
    wyjscie = await czytaj_kawalkami(" ".encode("utf-8") + b"\xc2")

    assert wyjscie.ma_tresc is True


async def test_kolejne_wyjscie_nie_dziedziczy_niedokonczonego_znaku_po_poprzednim():
    # pierwsze wyjscie ma tresc i konczy sie poczatkiem znaku (0xC2); drugie zaczyna sie od
    # samotnego 0xA0 - bez poczatku znaku to bajt niepoprawny, czyli tresc
    pierwsze = await czytaj_kawalkami(b"x\xc2")
    drugie = await czytaj_kawalkami(b"\xa0\n")

    assert pierwsze.ma_tresc is True
    assert drugie.ma_tresc is True


async def test_niepelny_znak_w_ostatnim_kawalku_po_pelnym_kawalku_spacji_jest_trescia():
    wyjscie = await czytaj_kawalkami(b" " * ROZMIAR_KAWALKA, b"\xc2")

    assert wyjscie.ma_tresc is True


# --- limit czasu i anulowanie ----------------------------------------------------------------

# Komenda uruchamia potomka (ta sama grupa procesow), ktory po `opoznienie` s zapisuje znacznik.
# Zywy potomek po koncu `wykonaj_check` zostawilby znacznik; `gotowy` zapisuje sama komenda po
# starcie potomka.
KOD_Z_POTOMKIEM = """\
import subprocess, sys, time
from pathlib import Path

znacznik, gotowy, opoznienie = sys.argv[1], sys.argv[2], sys.argv[3]
potomek = (
    "import sys, time\\n"
    "from pathlib import Path\\n"
    "time.sleep(float(sys.argv[2]))\\n"
    "Path(sys.argv[1]).write_text('potomek zyl')\\n"
)
subprocess.Popen([sys.executable, "-c", potomek, znacznik, opoznienie])
print("start", flush=True)
Path(gotowy).write_text("tak")
time.sleep(20)
"""


async def test_przekroczony_limit_czasu_konczy_krok_porazka_zabija_komende_i_potomka_a_dowod_ma_kod_null(
    tmp_path, sw
):
    znacznik, gotowy = tmp_path / "znacznik", tmp_path / "gotowy"
    polecenie = komenda(KOD_Z_POTOMKIEM, str(znacznik), str(gotowy), "3.0")

    wynik = await sprawdz(sw, polecenie, limit_s=1.5)

    assert (wynik.status, wynik.powod, wynik.kod_wyjscia) == (
        "nieudany", "check typecheck: przekroczony limit czasu 1.5 s", None)
    assert wynik.dowod is not None and wynik.dowod["exit_code"] is None
    dowod = wczytaj_dowod(sw.katalog)
    assert dowod["exit_code"] is None
    assert dowod["stdout_tail"] == "start\n"
    assert dowod["sha256"] == sha(b"start\n")
    assert dowod["source"] == "engine"
    assert gotowy.exists()
    # potomek (znacznik po 3 s od startu) nie moze przezyc zabicia grupy procesow
    await asyncio.sleep(2.2)
    assert not znacznik.exists()


# Potomek, ktory uciekl z grupy procesow komendy (nowa sesja): zabicie grupy go nie dosiega.
# Zapisuje swoj pid (sprzata go test), potem wykonuje reszte kodu (`%s`).
POTOMEK_POZA_GRUPA = (
    "import os, sys, time\n"
    "from pathlib import Path\n"
    "os.setsid()\n"
    "rodzic = os.getppid()\n"
    "Path(sys.argv[1]).write_text(str(os.getpid()))\n"
    "%s"
)

# Potomek zbierania: czeka az komenda zginie (zmiana rodzica), po pauzie wypisuje swoja linie na
# wspolny potok i zamyka fd 1 i 2 (EOF). Pauza: gdy drenazu nie ma, czytnik jest juz anulowany.
POTOMEK_PISZE_PO_ZABICIU = POTOMEK_POZA_GRUPA % (
    "koniec = time.monotonic() + 120\n"
    "while os.getppid() == rodzic and time.monotonic() < koniec:\n"
    "    time.sleep(0.02)\n"
    "time.sleep(0.3)\n"
    "os.write(1, b'po zabiciu grupy\\n')\n"
    "os.close(1)\n"
    "os.close(2)\n"
)

# Potomek limitu: trzyma potok (nie zamyka go) i zyje dlugo - EOF nie nadchodzi.
POTOMEK_TRZYMA_POTOK = POTOMEK_POZA_GRUPA % "time.sleep(120)\n"

# Komenda odpala potomka, czeka az ten zapisze pid (jest juz poza grupa) i drukuje `start`.
# Argument 3: `zyj` = zyje do zabicia grupy (potomek poznaje zabicie po zmianie rodzica - komenda,
# ktora konczy sie sama, zmienilaby rodzica przed limitem i test niczego by nie dowodzil),
# `koncz` = konczy sie od razu.
KOD_KOMENDY_Z_POTOMKIEM_POZA_GRUPA = """\
import subprocess, sys, time
from pathlib import Path

pid_plik, potomek, zyj = sys.argv[1], sys.argv[2], sys.argv[3] == "zyj"
subprocess.Popen([sys.executable, "-c", potomek, pid_plik])
koniec = time.monotonic() + 30
while not Path(pid_plik).exists() and time.monotonic() < koniec:
    time.sleep(0.02)
print("start", flush=True)
if zyj:
    time.sleep(600)
"""


def zabij_zapisanego_potomka(pid_plik: Path) -> None:
    try:
        tekst = pid_plik.read_text()
    except FileNotFoundError:
        return
    if tekst.strip():
        zabij_proces(int(tekst))


async def test_wyjscie_potomka_spoza_grupy_wypisane_po_zabiciu_grupy_trafia_do_dowodu(
    tmp_path, sw, monkeypatch
):
    # bezpiecznik: drenaz konczy sie na EOF (potomek zamyka potok), nie na uplywie tego limitu
    monkeypatch.setattr("runner.dowody.LIMIT_DRENAZU_S", 30.0)
    pid_plik = tmp_path / "pid-potomka"
    # limit komendy z zapasem: pod obciazeniem interpreter komendy i potomka musza wystartowac
    # przed zabiciem grupy; komenda czeka na pid potomka, wiec `start` oznacza potomka gotowego
    polecenie = komenda(
        KOD_KOMENDY_Z_POTOMKIEM_POZA_GRUPA, str(pid_plik), POTOMEK_PISZE_PO_ZABICIU, "zyj")

    try:
        wynik = await sprawdz(sw, polecenie, limit_s=5.0, bezpiecznik_s=40.0)
    finally:
        zabij_zapisanego_potomka(pid_plik)

    assert (wynik.status, wynik.powod, wynik.kod_wyjscia) == (
        "nieudany", "check typecheck: przekroczony limit czasu 5 s", None)
    dowod = wczytaj_dowod(sw.katalog)
    assert dowod["exit_code"] is None
    assert dowod["stdout_tail"] == "start\npo zabiciu grupy\n"
    assert dowod["sha256"] == sha(b"start\npo zabiciu grupy\n")


async def test_potomek_ktory_uciekl_z_grupy_i_trzyma_potok_nie_wiesza_kroku_ponad_limit_drenazu(
    tmp_path, sw, monkeypatch
):
    monkeypatch.setattr("runner.dowody.LIMIT_DRENAZU_S", 1.0)
    pid_plik = tmp_path / "pid-potomka"
    polecenie = komenda(
        KOD_KOMENDY_Z_POTOMKIEM_POZA_GRUPA, str(pid_plik), POTOMEK_TRZYMA_POTOK, "koncz")

    # bez limitu drenazu krok czekalby do konca zycia potomka (120 s); bezpiecznik testu (20 s)
    # rzuca wtedy `LimitCzasuTestu` - test oblewa na mechanizmie, nie na oknie czasu
    try:
        wynik = await sprawdz(sw, polecenie, limit_s=2.0, bezpiecznik_s=20.0)
    finally:
        zabij_zapisanego_potomka(pid_plik)

    assert (wynik.status, wynik.powod, wynik.kod_wyjscia) == (
        "nieudany", "check typecheck: przekroczony limit czasu 2 s", None)
    dowod = wczytaj_dowod(sw.katalog)
    assert dowod["exit_code"] is None
    assert dowod["stdout_tail"] == "start\n"
    assert dowod["sha256"] == sha(b"start\n")


async def test_plik_dowodu_ma_znaki_spoza_ascii_doslownie_w_utf8_i_wciecie_dwoch_spacji(sw):
    # bajty 0xC4 0x85 to znak a z ogonkiem w UTF-8; komenda pisze surowe bajty, bez locale
    kod = "import sys; sys.stdout.buffer.write(bytes([0xC4, 0x85, 10]))"

    wynik = await sprawdz(sw, komenda(kod))

    assert wynik.status == "zakonczony"
    surowy = (sw.katalog / DOWOD_TYPECHECK).read_bytes()
    assert b'"stdout_tail": "\xc4\x85\\n"' in surowy
    assert b"\\u0105" not in surowy
    assert surowy.startswith(b'{\n  "capability": "typecheck",\n  "argv": [\n    ')


async def test_komenda_checku_startuje_z_dev_null_na_stdin_wlasna_sesja_i_stderr_w_stdout(
    sw, monkeypatch
):
    prawdziwa = asyncio.create_subprocess_exec
    wywolania: list[dict] = []

    async def atrapa(*argv, **kwargs):
        wywolania.append(kwargs)
        return await prawdziwa(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", atrapa)

    wynik = await sprawdz(sw, komenda("print('ok')"))

    assert wynik.status == "zakonczony"
    assert len(wywolania) == 1
    opcje = wywolania[0]
    assert opcje["stdin"] is asyncio.subprocess.DEVNULL
    assert opcje["stdout"] is asyncio.subprocess.PIPE
    assert opcje["stderr"] is asyncio.subprocess.STDOUT
    assert opcje["start_new_session"] is True
    assert opcje["cwd"] == sw.worktree


KOD_STDIN_NIEDOSTEPNY = """\
import asyncio, json, sys
sys.path[:0] = json.loads(sys.argv[1])
from pathlib import Path
from runner.dowody import wykonaj_check

wynik = asyncio.run(wykonaj_check(
    Path(sys.argv[4]), "typecheck", sys.argv[2], Path(sys.argv[3]), limit_s=5.0))
print(json.dumps({"status": wynik.status, "powod": wynik.powod}))
"""


async def test_komenda_checku_nie_dziedziczy_stdin_procesu_silnika_i_czyta_koniec_pliku(tmp_path):
    # stan rodzica rozny od oczekiwanego u potomka: stdin silnika to otwarty potok, do ktorego
    # nikt nie pisze; komenda, ktora go odziedziczy, wisi do limitu czasu
    worktree = tmp_path / "repo"
    worktree.mkdir()
    katalog = tmp_path / "run" / "typecheck"
    polecenie = komenda("import sys; print('przeczytano', len(sys.stdin.read()))")
    proc = await uruchom_proces(
        sys.executable, "-c", KOD_STDIN_NIEDOSTEPNY, json.dumps(sys.path), polecenie,
        str(katalog), str(worktree), cwd=KORZEN_REPO,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    try:
        wyjscie = await czekaj(proc.stdout.read(), 40, "wynik silnika w procesie potomnym")
        await czekaj(proc.wait(), 20, "koniec procesu silnika")
    finally:
        await zakoncz_proces(proc, nazwa="silnik z otwartym stdin")

    assert json.loads(wyjscie.decode("utf-8")) == {"status": "zakonczony", "powod": None}
    assert wczytaj_dowod(katalog)["stdout_tail"] == "przeczytano 0\n"


async def test_limit_czasu_w_komunikacie_jest_liczba_bez_zbednego_zera(sw):
    wynik = await sprawdz(sw, komenda("import time; time.sleep(20)"), limit_s=1)

    assert wynik.powod == "check typecheck: przekroczony limit czasu 1 s"
    assert wynik.kod_wyjscia is None


async def test_komenda_ktora_konczy_sie_w_limicie_nie_jest_przerywana(sw):
    wynik = await sprawdz(sw, komenda("import time; time.sleep(0.3); print('ok')"), limit_s=20)

    assert (wynik.status, wynik.kod_wyjscia) == ("zakonczony", 0)


async def test_anulowanie_zadania_checku_zabija_komende_i_potomka_i_przepuszcza_anulowanie(
    tmp_path, sw
):
    znacznik, gotowy = tmp_path / "znacznik", tmp_path / "gotowy"
    polecenie = komenda(KOD_Z_POTOMKIEM, str(znacznik), str(gotowy), "1.5")
    zadanie = asyncio.ensure_future(wykonaj_check(sw.worktree, "typecheck", polecenie, sw.katalog))
    try:
        await czekaj_na_warunek(gotowy.exists, 30, "start potomka komendy checku")

        zadanie.cancel()
        with pytest.raises(asyncio.CancelledError):
            await czekaj(zadanie, 30, "koniec anulowanego checku")
    finally:
        if not zadanie.done():
            zadanie.cancel()
    await asyncio.sleep(2.5)
    assert not znacznik.exists()


# --- brak dowodu: komenda nieczytelna, brak programu, blad zapisu -----------------------------


@pytest.mark.parametrize("zla", ["a 'b", "a 'sekret", "", "   "])
async def test_komenda_nieczytelna_konczy_krok_porazka_bez_tresci_komendy_i_bez_pliku_dowodu(sw, zla):
    wynik = await sprawdz(sw, zla)

    assert wynik == WynikKroku(status="nieudany", powod="check typecheck: komenda nieczytelna", kod_wyjscia=None)
    assert wynik.dowod is None
    assert pliki_pod(sw.katalog) == []


async def test_brak_programu_konczy_krok_porazka_z_nazwa_wyjatku_bez_pliku_dowodu(tmp_path, sw):
    wynik = await sprawdz(sw, str(tmp_path / "nie-ma-takiego-programu") + " --x")

    assert wynik == WynikKroku(
        status="nieudany", powod="check typecheck: nie uruchomiono komendy (FileNotFoundError)", kod_wyjscia=None)
    assert pliki_pod(sw.katalog) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root uruchamia plik bez prawa wykonania")
async def test_program_bez_prawa_wykonania_konczy_krok_porazka_z_nazwa_wyjatku_bez_pliku_dowodu(
    tmp_path, sw
):
    program = tmp_path / "nie-wykonywalny"
    program.write_text("#!/bin/sh\necho x\n", encoding="utf-8")
    program.chmod(0o644)

    wynik = await sprawdz(sw, str(program))

    assert wynik == WynikKroku(
        status="nieudany", powod="check typecheck: nie uruchomiono komendy (PermissionError)", kod_wyjscia=None)
    assert pliki_pod(sw.katalog) == []


async def test_katalog_kroku_w_repozytorium_konczy_krok_naruszeniem_run_dir_outside_repo_bez_procesu(
    tmp_path, sw
):
    katalog = sw.worktree / "wyniki" / "typecheck"
    start = tmp_path / "komenda-ruszyla"
    kod = f"from pathlib import Path; Path({str(start)!r}).write_text('tak'); print('ok')"

    wynik = await sprawdz(sw, komenda(kod), katalog=katalog)

    assert wynik == WynikKroku(
        status="nieudany",
        powod=f"niezmiennik run_dir_outside_repo: katalog runu {katalog} lezy w repozytorium {sw.worktree}",
        kod_wyjscia=None,
    )
    assert not start.exists()
    assert pliki_pod(sw.worktree / "wyniki") == []


@pytest.mark.parametrize("kod_komendy", [0, 4])
async def test_blad_zapisu_dowodu_konczy_krok_porazka_z_nazwa_wyjatku_i_kodem_komendy(
    tmp_path, sw, kod_komendy
):
    blokada = tmp_path / "blokada"
    blokada.write_text("to jest plik, nie katalog", encoding="utf-8")
    kod = f"import sys; print('wynik'); sys.exit({kod_komendy})"

    wynik = await sprawdz(sw, komenda(kod), katalog=blokada / "typecheck")

    assert wynik.status == "nieudany"
    assert wynik.kod_wyjscia == kod_komendy
    assert wynik.powod in (
        "check typecheck: nie zapisano dowodu (NotADirectoryError)",
        "check typecheck: nie zapisano dowodu (FileExistsError)",
    )
    assert wynik.dowod is None
    assert blokada.read_text(encoding="utf-8") == "to jest plik, nie katalog"


# --- silnik: krok check w runie --------------------------------------------------------------

KOD_ZIELONY = "print('typy ok')"
KOD_CZERWONY = "import sys; print('blad typow w a.py'); sys.exit(3)"
KOD_PUSTY = "pass"


def yaml_napis(tekst: str) -> str:
    return "'" + tekst.replace("'", "''") + "'"


def projekt_z(**komendy: str) -> str:
    linie = ["stack: [python]"]
    if komendy:
        linie.append("capabilities:")
        linie.extend(f"  {nazwa}: {yaml_napis(k)}" for nazwa, k in komendy.items())
    return "\n".join(linie) + "\n"


WORKFLOW_Z_CHECKIEM = """\
name: w
steps:
  autor: {skill: s_autor}
  typecheck: {system: check, capability: typecheck, needs: [autor]}
  rec: {skill: s_rec, needs: [typecheck]}
"""

SKILLE_Z_CHECKIEM = {
    "autor": {},
    "rec": {
        "role": "reviewer", "consumes": [DOWOD_TYPECHECK], "produces": ["zastrzezenia.md"]},
}


@dataclass
class SzpiegZmieniajacyDrzewo(Szpieg):
    """Agent bez procesu; autor zostawia w worktree nowy plik (jak prawdziwy autor)."""

    async def __call__(self, spec, polecenie, katalog, emit, *, katalog_runu=None, **dodatkowe):
        if spec.step_id == "autor":
            (katalog / "nowy.txt").write_text("a\nb\n", encoding="utf-8")
        return await super().__call__(
            spec, polecenie, katalog, emit, katalog_runu=katalog_runu, **dodatkowe)


def swiat_z_checkiem(tmp_path: Path, *, typecheck: str | None = komenda(KOD_ZIELONY)):
    komendy = {} if typecheck is None else {"typecheck": typecheck}
    return swiat(
        tmp_path, [K("autor"), K("rec")], tresc=WORKFLOW_Z_CHECKIEM,
        opcje_skilli=SKILLE_Z_CHECKIEM, project=projekt_z(**komendy))


async def test_krok_check_nie_uruchamia_agenta_a_dowod_silnika_lezy_w_katalogu_runu_pod_id_kroku(
    tmp_path,
):
    s = swiat_z_checkiem(tmp_path)
    agent = SzpiegZmieniajacyDrzewo()

    zdarzenia = await uruchom(s, agent)

    assert agent.kroki == ["autor", "rec"]
    katalog = s.katalog_runu / "typecheck"
    assert pliki_pod(katalog) == [DOWOD_TYPECHECK]
    dowod = wczytaj_dowod(katalog)
    assert (dowod["source"], dowod["exit_code"], dowod["capability"]) == ("engine", 0, "typecheck")
    assert dowod["sha256"] == sha(b"typy ok\n")
    assert dowod["argv"] == [sys.executable, "-c", KOD_ZIELONY]
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_step_finished_kroku_check_niesie_metadane_dowodu_bez_wyjscia_i_argv(tmp_path):
    s = swiat_z_checkiem(tmp_path)

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())

    start = jedno(zdarzenia, "step_started", "typecheck")
    koniec = jedno(zdarzenia, "step_finished", "typecheck")
    assert (start["run_id"], start["agent_id"], koniec["agent_id"]) == (
        RUN_ID, "typecheck", "typecheck")
    assert koniec["text"] == ""
    plik = wczytaj_dowod(s.katalog_runu / "typecheck")
    assert koniec["dowod"] == {
        "capability": "typecheck", "exit_code": 0, "sha256": sha(b"typy ok\n"),
        "started_at": plik["started_at"], "finished_at": plik["finished_at"],
        "source": "engine", "plik": DOWOD_TYPECHECK,
    }
    assert indeks(zdarzenia, "step_finished", "autor") < indeks(zdarzenia, "step_started", "typecheck")
    assert indeks(zdarzenia, "step_finished", "typecheck") < indeks(zdarzenia, "step_started", "rec")


async def test_zdarzenie_kroku_skillowego_nie_niesie_pola_dowod(tmp_path):
    s = swiat_z_checkiem(tmp_path)

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())

    assert "dowod" not in jedno(zdarzenia, "step_finished", "autor")
    assert "dowod" not in jedno(zdarzenia, "step_finished", "rec")


async def test_check_dziala_w_worktree_po_pracy_autora_i_widzi_jego_zmiane(tmp_path):
    kod = "import os; print(os.path.exists('nowy.txt'))"
    s = swiat_z_checkiem(tmp_path, typecheck=komenda(kod))

    await uruchom(s, SzpiegZmieniajacyDrzewo())

    assert wczytaj_dowod(s.katalog_runu / "typecheck")["stdout_tail"] == "True\n"


async def test_recenzent_dostaje_dowod_jako_sciezke_bezwzgledna_w_katalogu_runu(tmp_path):
    s = swiat_z_checkiem(tmp_path)
    agent = SzpiegZmieniajacyDrzewo()

    await uruchom(s, agent)

    oczekiwana = s.katalog_runu / "typecheck" / DOWOD_TYPECHECK
    assert agent.spec("rec").wejscia == [str(oczekiwana)]
    assert Path(agent.spec("rec").wejscia[0]).is_absolute()
    assert Path(agent.spec("rec").wejscia[0]).is_file()


async def test_czerwony_check_konczy_krok_porazka_z_dowodem_a_recenzent_jest_pominiety_jako_zaleznosc_nieudana(
    tmp_path,
):
    s = swiat_z_checkiem(tmp_path, typecheck=komenda(KOD_CZERWONY))
    agent = SzpiegZmieniajacyDrzewo()

    zdarzenia = await uruchom(s, agent)

    nieudany = jedno(zdarzenia, "step_failed", "typecheck")
    assert nieudany["text"] == "check typecheck: kod wyjścia 3"
    assert nieudany["dowod"]["exit_code"] == 3
    assert nieudany["dowod"]["plik"] == DOWOD_TYPECHECK
    assert "step_finished" not in [z["kind"] for z in zdarzenia if z["step_id"] == "typecheck"]
    assert wczytaj_dowod(s.katalog_runu / "typecheck")["exit_code"] == 3
    pominiety = jedno(zdarzenia, "step_skipped", "rec")
    assert pominiety["text"] == "zaleznosc_nieudana: zależność nieudana: typecheck"
    assert agent.kroki == ["autor"]
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_check_z_kodem_zero_i_pustym_wyjsciem_konczy_krok_bledem_evidence_required(tmp_path):
    s = swiat_z_checkiem(tmp_path, typecheck=komenda(KOD_PUSTY))
    agent = SzpiegZmieniajacyDrzewo()

    zdarzenia = await uruchom(s, agent)

    assert jedno(zdarzenia, "step_failed", "typecheck")["text"] == EVIDENCE_PUSTY
    assert wczytaj_dowod(s.katalog_runu / "typecheck")["exit_code"] == 0
    assert jedno(zdarzenia, "step_skipped", "rec")["text"] == (
        "zaleznosc_nieudana: zależność nieudana: typecheck")
    assert agent.kroki == ["autor"]


async def test_check_z_kodem_zero_i_samymi_spacjami_nielamliwymi_konczy_krok_bledem_evidence_required(
    tmp_path,
):
    surowe = "  \n".encode("utf-8")
    s = swiat_z_checkiem(tmp_path, typecheck=komenda(kod_z_bajtami(surowe)))
    agent = SzpiegZmieniajacyDrzewo()

    zdarzenia = await uruchom(s, agent)

    assert jedno(zdarzenia, "step_failed", "typecheck")["text"] == EVIDENCE_PUSTY
    assert "step_finished" not in [z["kind"] for z in zdarzenia if z["step_id"] == "typecheck"]
    assert wczytaj_dowod(s.katalog_runu / "typecheck")["sha256"] == sha(surowe)
    assert jedno(zdarzenia, "step_skipped", "rec")["text"] == (
        "zaleznosc_nieudana: zależność nieudana: typecheck")
    assert agent.kroki == ["autor"]


async def test_brak_capability_w_projekcie_pomija_krok_check_a_recenzent_traci_wejscie(tmp_path):
    s = swiat_z_checkiem(tmp_path, typecheck=None)
    agent = SzpiegZmieniajacyDrzewo()

    zdarzenia = await uruchom(s, agent)

    assert jedno(zdarzenia, "step_skipped", "typecheck")["text"] == (
        "brak_capability: brak capability typecheck")
    assert jedno(zdarzenia, "step_skipped", "rec")["text"] == (
        "brak_wejscia: brak wejścia: dowod-typecheck.json z kroku typecheck")
    assert "step_started" not in [z["kind"] for z in zdarzenia if z["step_id"] == "typecheck"]
    assert pliki_pod(s.katalog_runu / "typecheck") == []
    assert agent.kroki == ["autor"]
    assert zdarzenia[-1]["kind"] == "run_finished"


WORKFLOW_Z_DWOMA_CHECKAMI = """\
name: w
steps:
  autor: {skill: s_autor}
  diff: {system: diff, needs: [autor]}
  sprawdz-typy: {system: check, capability: typecheck, needs: [diff]}
  sprawdz-kontrakt: {system: check, capability: contract, needs: [diff]}
  cc: {skill: s_cc, needs: [diff, sprawdz-typy, sprawdz-kontrakt]}
"""

SKILLE_Z_DWOMA_CHECKAMI = {
    "autor": {},
    "cc": {
        "role": "reviewer",
        "consumes": ["zmiana.diff", DOWOD_TYPECHECK, "dowod-contract.json"],
        "produces": ["rozjazdy-kontraktu.md"],
    },
}


def swiat_z_dwoma_checkami(tmp_path: Path, **komendy: str):
    return swiat(
        tmp_path, [K("autor"), K("cc")], tresc=WORKFLOW_Z_DWOMA_CHECKAMI,
        opcje_skilli=SKILLE_Z_DWOMA_CHECKAMI, project=projekt_z(**komendy))


async def test_recenzent_kontraktu_dostaje_diff_i_oba_dowody_gdy_oba_checki_przeszly(tmp_path):
    s = swiat_z_dwoma_checkami(
        tmp_path, typecheck=komenda("print('typy-ok')"), contract=komenda("print('kontrakt-ok')"))
    agent = SzpiegZmieniajacyDrzewo()

    zdarzenia = await uruchom(s, agent)

    assert agent.kroki.count("cc") == 1
    assert agent.spec("cc").wejscia == [
        str(s.katalog_runu / "diff" / "zmiana.diff"),
        str(s.katalog_runu / "sprawdz-typy" / DOWOD_TYPECHECK),
        str(s.katalog_runu / "sprawdz-kontrakt" / "dowod-contract.json"),
    ]
    assert "step_skipped" not in rodzaje(zdarzenia)
    assert "step_failed" not in rodzaje(zdarzenia)
    jedno(zdarzenia, "step_finished", "cc")
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_kazdy_krok_check_uruchamia_komende_swojej_capability_a_nie_cudzej(tmp_path):
    s = swiat_z_dwoma_checkami(
        tmp_path, typecheck=komenda("print('typy-ok')"), contract=komenda("print('kontrakt-ok')"))

    await uruchom(s, SzpiegZmieniajacyDrzewo())

    typy = wczytaj_dowod(s.katalog_runu / "sprawdz-typy")
    kontrakt = wczytaj_dowod(s.katalog_runu / "sprawdz-kontrakt", "contract")
    assert (typy["capability"], typy["stdout_tail"]) == ("typecheck", "typy-ok\n")
    assert (kontrakt["capability"], kontrakt["stdout_tail"]) == ("contract", "kontrakt-ok\n")
    assert pliki_pod(s.katalog_runu / "sprawdz-typy") == [DOWOD_TYPECHECK]
    assert pliki_pod(s.katalog_runu / "sprawdz-kontrakt") == ["dowod-contract.json"]


async def test_czerwony_jeden_z_dwoch_checkow_pomija_recenzenta_a_drugi_check_dobiega_konca(tmp_path):
    s = swiat_z_dwoma_checkami(
        tmp_path, typecheck=komenda("print('typy-ok')"), contract=komenda(KOD_CZERWONY))
    agent = SzpiegZmieniajacyDrzewo()

    zdarzenia = await uruchom(s, agent)

    jedno(zdarzenia, "step_finished", "sprawdz-typy")
    assert jedno(zdarzenia, "step_failed", "sprawdz-kontrakt")["dowod"]["exit_code"] == 3
    assert jedno(zdarzenia, "step_skipped", "cc")["text"] == (
        "zaleznosc_nieudana: zależność nieudana: sprawdz-kontrakt")
    assert "cc" not in agent.kroki


async def test_brak_jednej_z_dwoch_capability_pomija_tylko_jej_krok_a_recenzent_traci_jej_wejscie(
    tmp_path,
):
    s = swiat_z_dwoma_checkami(tmp_path, typecheck=komenda("print('typy-ok')"))
    agent = SzpiegZmieniajacyDrzewo()

    zdarzenia = await uruchom(s, agent)

    jedno(zdarzenia, "step_finished", "sprawdz-typy")
    assert jedno(zdarzenia, "step_skipped", "sprawdz-kontrakt")["text"] == (
        "brak_capability: brak capability contract")
    assert jedno(zdarzenia, "step_skipped", "cc")["text"] == (
        "brak_wejscia: brak wejścia: dowod-contract.json z kroku sprawdz-kontrakt")
    assert "cc" not in agent.kroki


async def test_krok_check_jest_krokiem_weryfikujacym_w_koncu_runu(tmp_path):
    s = swiat_z_checkiem(tmp_path)

    zdarzenia = await uruchom(s, SzpiegZmieniajacyDrzewo())

    assert zdarzenia[-1]["kroki_weryfikujace"] == ["typecheck", "rec"]


def test_rejestr_wykonawcow_krokow_systemowych_zna_diff_i_check():
    from runner.silnik import WYKONAWCY_SYSTEMOWI

    assert set(WYKONAWCY_SYSTEMOWI) == {"diff", "check"}


async def test_krok_check_bez_capability_w_projekcie_zglasza_porazke_kroku_z_nazwa_capability(
    tmp_path,
):
    from runner.silnik import PorazkaKroku, _krok_systemowy

    krok = SimpleNamespace(id="typecheck", system="check", capability="typecheck")
    p = SimpleNamespace(
        projekt=SimpleNamespace(capabilities={"inna": "x"}),
        worktree=SimpleNamespace(
            sciezka=tmp_path / "repo", katalog_runu=tmp_path / "run", base_commit="0" * 40),
    )

    with pytest.raises(PorazkaKroku, match=r"^brak capability typecheck$"):
        await czekaj(_krok_systemowy(krok, p), 30, "_krok_systemowy bez capability")
    assert pliki_pod(tmp_path / "run") == []
