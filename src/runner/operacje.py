"""Operacje runnera na zapytania kokpitu (HAR-49, HAR-55). Nowa operacja = nowy wpis w `OPERACJE`.

Pliki uzytkownika: kazda sciezka przechodzi przez `Zakres.rozwiaz` (twardy zakres, sekrety, .git
przy zapisie). Runner oddaje surowy plik, bez parsowania (D23). Komunikaty bledow sa stale:
bez `str(OSError)`, sciezek i tresci pliku (moga niesc sekret).
"""
from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import os
import re
import stat
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from cockpit.services.errors import BlednySkill
from cockpit.services.skills import NAZWA_PLIKU, ZRODLA, _podziel
from runner.zakres import PozaZakresem, Zakres

LIMIT_WYNIKOW = 2000
LIMIT_DEFINICJI = 2000   # skille i workflow ze wszystkich projektow lacznie (HAR-67)
DOMYSLNY_LIMIT_ODCZYTU_KB = 512
_ZMIENNA_LIMITU = "COCKPIT_MAX_READ_KB"
_CYFRY_ASCII = re.compile(r"[0-9]{1,12}")   # fullmatch: "$" przepuscilby "5\n"
_TRYB_NOWEGO_PLIKU = 0o644
KOMUNIKAT_POZA_ZAKRESEM = "sciezka poza zakresem runnera"


class BladOperacji(Exception):
    """Blad z kodem dla kokpitu; `komunikat` jest staly i nie niesie tresci pliku."""

    def __init__(self, kod: str, komunikat: str) -> None:
        self.kod = kod
        self.komunikat = komunikat
        super().__init__(komunikat)


def _blad_dysku(e: OSError, *, brak_to_katalog: bool = False) -> BladOperacji:
    """Tlumaczy OSError na kod dziedziny; `str(e)` nigdy nie wychodzi na zewnatrz."""
    if isinstance(e, PermissionError):
        return BladOperacji("brak_uprawnien", "brak uprawnien do pliku lub katalogu")
    if isinstance(e, FileNotFoundError):
        if brak_to_katalog:
            return BladOperacji("brak_katalogu", "katalog nadrzedny nie istnieje")
        return BladOperacji("nie_istnieje", "sciezka nie istnieje")
    if isinstance(e, IsADirectoryError):
        return BladOperacji("to_katalog", "sciezka jest katalogiem")
    if isinstance(e, NotADirectoryError):
        return BladOperacji("nie_katalog", "element sciezki nie jest katalogiem")
    return BladOperacji("blad_dysku", "blad dysku przy operacji na pliku")


def _rozwiaz(zakres: Zakres, sciezka, **opcje) -> Path:
    """`rozwiaz` dotyka dysku (resolve, is_symlink): OSError tlumaczymy, PozaZakresem idzie dalej."""
    try:
        return zakres.rozwiaz(sciezka, **opcje)
    except OSError as e:
        raise _blad_dysku(e) from None


def _tekst(dane: dict, klucz: str) -> str:
    wartosc = dane.get(klucz)
    if not isinstance(wartosc, str):
        raise BladOperacji("zle_dane", f"pole {klucz} musi byc napisem")
    return wartosc


def _limit_odczytu() -> int:
    """Limit w bajtach, czytany przy kazdym wywolaniu. Zla wartosc to blad, nie powrot do domyslnej."""
    surowa = os.environ.get(_ZMIENNA_LIMITU)
    if not surowa:
        return DOMYSLNY_LIMIT_ODCZYTU_KB * 1024
    if not _CYFRY_ASCII.fullmatch(surowa) or int(surowa) == 0:
        raise BladOperacji(
            "zla_konfiguracja", f"{_ZMIENNA_LIMITU} musi byc liczba calkowita > 0 (cyfry ASCII)")
    return int(surowa) * 1024


def _czytaj_plik(sciezka: str, limit: int) -> dict:
    cel = _rozwiaz(Zakres.z_konfiguracji(), sciezka)
    try:
        tryb = os.stat(cel).st_mode
        if stat.S_ISDIR(tryb):
            raise BladOperacji("to_katalog", "sciezka jest katalogiem")
        if not stat.S_ISREG(tryb):   # FIFO czy urzadzenie: open() moglby zawisnac
            raise BladOperacji("blad_dysku", "sciezka nie jest zwyklym plikiem")
        with open(cel, "rb") as f:
            surowe = f.read(limit + 1)
    except OSError as e:
        raise _blad_dysku(e) from None
    if len(surowe) > limit:
        raise BladOperacji("za_duzy", f"plik jest wiekszy niz limit {limit // 1024} KB")
    try:
        tresc = surowe.decode("utf-8")
    except UnicodeDecodeError:
        raise BladOperacji("binarny", "plik nie jest tekstem UTF-8") from None
    if "\0" in tresc:
        raise BladOperacji("binarny", "plik nie jest tekstem (zawiera bajt NUL)")
    return {"sciezka": str(cel), "tresc": tresc, "rozmiar": len(surowe)}


def _sprawdz_wzorzec(wzorzec: str) -> None:
    skladniki = wzorzec.split("/")
    zly = (
        not wzorzec
        or "\0" in wzorzec
        or wzorzec.startswith("/")
        or os.path.isabs(wzorzec)
        or ".." in skladniki
        # "**" tylko jako caly skladnik; "a**" rozni sie miedzy wersjami Pythona
        or any("**" in s and s != "**" for s in skladniki)
    )
    if zly:
        raise BladOperacji("zly_wzorzec", "wzorzec jest pusty, bezwzgledny albo niedozwolony")


def _podkatalogi_w_glab(korzen: Path, widziane: set[Path]) -> list[Path]:
    """Korzen i podkatalogi, bez dowiazan do katalogow; blad dowolnego katalogu to wyjatek.
    `widziane` wspolne dla calego skladnika: poddrzewo przejrzane raz nie jest przegladane drugi."""
    wynik: list[Path] = []
    do_przejrzenia = [korzen]
    while do_przejrzenia:
        katalog = do_przejrzenia.pop()
        if katalog in widziane:
            continue
        widziane.add(katalog)
        wynik.append(katalog)
        with os.scandir(katalog) as wpisy:
            for wpis in wpisy:
                if wpis.is_dir(follow_symlinks=False):
                    do_przejrzenia.append(katalog / wpis.name)
    return wynik


def _w_zakresie(zakres: Zakres, sciezka: Path) -> bool:
    try:
        _rozwiaz(zakres, sciezka)
    except PozaZakresem:
        return False
    return True


def _dopasuj(zakres: Zakres, baza: Path, wzorzec: str) -> list[Path]:
    """Wlasny glob: `Path.glob` polyka OSError w podkatalogach, a brak praw to nie pusta lista.
    Zakres sprawdzamy przed wejsciem do katalogu, a koszt wzorca ograniczamy: kolejne `**`
    to jedno, a duplikaty znikaja po kazdym skladniku."""
    skladniki: list[str] = []
    for s in wzorzec.split("/"):
        if s in ("", ".") or (s == "**" and skladniki[-1:] == ["**"]):
            continue
        skladniki.append(s)
    biezace = [baza]
    for i, skladnik in enumerate(skladniki):
        ostatni = i == len(skladniki) - 1
        nastepne: list[Path] = []
        widziane: set[Path] = set()
        try:
            for katalog in biezace:
                if skladnik == "**":
                    nastepne.extend(_podkatalogi_w_glab(katalog, widziane))
                    continue
                with os.scandir(katalog) as wpisy:
                    for wpis in wpisy:
                        if not fnmatch.fnmatchcase(wpis.name, skladnik):
                            continue
                        sciezka = katalog / wpis.name
                        # skladnik posredni: tylko katalogi i tylko w zakresie
                        if ostatni or (_w_zakresie(zakres, sciezka) and wpis.is_dir()):
                            nastepne.append(sciezka)
        except OSError as e:
            raise _blad_dysku(e) from None
        biezace = list(dict.fromkeys(nastepne))
    return biezace


def _listuj_pliki(sciezka: str, wzorzec: str) -> dict:
    zakres = Zakres.z_konfiguracji()
    baza = _rozwiaz(zakres, sciezka)
    try:
        if not stat.S_ISDIR(os.stat(baza).st_mode):
            raise BladOperacji("nie_katalog", "sciezka nie jest katalogiem")
    except OSError as e:
        raise _blad_dysku(e) from None
    wpisy = _dopasuj(zakres, baza, wzorzec)
    pliki: dict[str, bool] = {}
    for wpis in wpisy:
        try:
            cel = _rozwiaz(zakres, wpis)
        except PozaZakresem:
            continue   # sekret albo dowiazanie poza zakres: pomijamy po cichu
        try:
            pliki[str(cel)] = stat.S_ISDIR(os.stat(cel).st_mode)
        except FileNotFoundError:
            continue   # zniknal w trakcie albo wiszace dowiazanie
        except OSError as e:
            raise _blad_dysku(e) from None
    posortowane = sorted(pliki)
    return {
        "pliki": [{"sciezka": p, "katalog": pliki[p]} for p in posortowane[:LIMIT_WYNIKOW]],
        "obcieto": len(posortowane) > LIMIT_WYNIKOW,
    }


def _zapisz_plik(sciezka: str, dane: bytes) -> dict:
    cel = _rozwiaz(Zakres.z_konfiguracji(), sciezka, do_zapisu=True)
    try:
        try:
            istniejacy = os.stat(cel)
        except FileNotFoundError:
            tryb = _TRYB_NOWEGO_PLIKU
        else:
            if stat.S_ISDIR(istniejacy.st_mode):
                raise BladOperacji("to_katalog", "sciezka jest katalogiem")
            tryb = stat.S_IMODE(istniejacy.st_mode)
        fd, tymczasowy = tempfile.mkstemp(dir=cel.parent, prefix=f".{cel.name}.", suffix=".tmp")
        zastapiony = False
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(dane)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tymczasowy, tryb)
            os.replace(tymczasowy, cel)
            zastapiony = True
        finally:
            if not zastapiony:
                with contextlib.suppress(OSError):
                    os.unlink(tymczasowy)
    except OSError as e:
        raise _blad_dysku(e, brak_to_katalog=True) from None
    return {"sciezka": str(cel), "rozmiar": len(dane)}


def _nazwy_podkatalogow(katalog: Path) -> list[str]:
    """Posortowane nazwy wpisow katalogu; brak katalogu to pusta lista, inny blad dysku to wyjatek."""
    try:
        with os.scandir(katalog) as wpisy:
            return sorted(w.name for w in wpisy)
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as e:
        raise _blad_dysku(e) from None


def _frontmatter_skilla(zakres: Zakres, plik: Path, limit: int) -> tuple[bool, str | None]:
    """(jest_skillem, frontmatter). Tresc po frontmatterze nigdy nie wychodzi z tej funkcji;
    plik nieczytelny, za duzy, nie-UTF-8 albo bez frontmattera daje (True, None)."""
    try:
        cel = _rozwiaz(zakres, plik)
    except PozaZakresem:
        return False, None   # sekret albo dowiazanie poza zakres: pomijamy bez bledu
    except BladOperacji:
        return True, None
    try:
        tryb = os.stat(cel).st_mode
    except (FileNotFoundError, NotADirectoryError):
        return False, None
    except OSError:
        return True, None
    if not stat.S_ISREG(tryb):   # katalog albo FIFO: open() moglby zawisnac
        return False, None
    try:
        with open(cel, "rb") as f:
            surowe = f.read(limit + 1)
    except OSError:
        return True, None
    if len(surowe) > limit:
        return True, None
    try:
        tekst = surowe.decode("utf-8")
    except UnicodeDecodeError:
        return True, None
    if "\0" in tekst:
        return True, None
    try:
        frontmatter, _ = _podziel(tekst, plik)
    except BlednySkill:
        return True, None
    return True, frontmatter


def _skille_ze_zrodla(zakres: Zakres, baza: Path, podkatalog: str, zrodlo: str,
                      limit: int) -> list[dict]:
    wynik: list[dict] = []
    for nazwa in _nazwy_podkatalogow(baza / podkatalog):
        plik = baza / podkatalog / nazwa / NAZWA_PLIKU
        jest_skillem, frontmatter = _frontmatter_skilla(zakres, plik, limit)
        if jest_skillem:
            wynik.append({
                "zrodlo": zrodlo, "podkatalog": podkatalog, "katalog": nazwa,
                "sciezka": f"{podkatalog}/{nazwa}/{NAZWA_PLIKU}", "frontmatter": frontmatter})
    return wynik


def _workflow_projektu(zakres: Zakres, projekt: Path) -> list[dict]:
    katalog = projekt / ".cockpit" / "workflows"
    wynik: list[dict] = []
    for nazwa in _nazwy_podkatalogow(katalog):
        if not nazwa.endswith(".yaml") or nazwa == ".yaml":
            continue
        plik = katalog / nazwa
        try:
            cel = _rozwiaz(zakres, plik)
        except PozaZakresem:
            continue
        try:
            if not stat.S_ISREG(os.stat(cel).st_mode):
                continue
        except OSError:
            continue   # zniknal w trakcie albo wiszace dowiazanie
        wynik.append({"sciezka": f".cockpit/workflows/{nazwa}"})
    return wynik


def _katalog_definicji() -> dict:
    zakres = Zakres.z_konfiguracji()
    limit = _limit_odczytu()
    dom = Path.home()
    z_domu = [
        (zrodlo, podkatalog, _skille_ze_zrodla(zakres, dom, podkatalog, zrodlo, limit))
        for zrodlo, podkatalog, _ in ZRODLA if zrodlo == "dom"
    ]
    razem = 0
    projekty: list[dict] = []
    for projekt in zakres.projekty:
        skille: list[dict] = []
        for zrodlo, podkatalog, _ in ZRODLA:
            if zrodlo == "repo":
                skille += _skille_ze_zrodla(zakres, projekt, podkatalog, zrodlo, limit)
            else:
                skille += next(s for z, p, s in z_domu if (z, p) == (zrodlo, podkatalog))
        workflow = _workflow_projektu(zakres, projekt)
        razem += len(skille) + len(workflow)
        if razem > LIMIT_DEFINICJI:   # nigdy obciety katalog podany jako pelny
            raise BladOperacji(
                "za_duzo", f"za duzo skilli i workflow w projektach (limit {LIMIT_DEFINICJI})")
        projekty.append({"projekt": str(projekt), "skille": skille, "workflow": workflow})
    return {"projekty": projekty}


async def ping(dane: dict) -> dict:
    return {"pong": True}


async def read_file(dane: dict) -> dict:
    sciezka = _tekst(dane, "sciezka")
    limit = _limit_odczytu()
    return await asyncio.to_thread(_czytaj_plik, sciezka, limit)


async def list_files(dane: dict) -> dict:
    sciezka = _tekst(dane, "sciezka")
    wzorzec = _tekst({"wzorzec": "*", **dane}, "wzorzec")
    _sprawdz_wzorzec(wzorzec)
    return await asyncio.to_thread(_listuj_pliki, sciezka, wzorzec)


async def write_file(dane: dict) -> dict:
    sciezka = _tekst(dane, "sciezka")
    tresc = _tekst(dane, "tresc")
    try:
        surowe = tresc.encode("utf-8")
    except UnicodeEncodeError:
        raise BladOperacji("zle_dane", "tresc nie jest poprawnym tekstem UTF-8") from None
    return await asyncio.to_thread(_zapisz_plik, sciezka, surowe)


async def katalog_definicji(dane: dict) -> dict:
    return await asyncio.to_thread(_katalog_definicji)


OPERACJE: dict[str, Callable[[dict], Awaitable[Any]]] = {
    "ping": ping,
    "read_file": read_file,
    "list_files": list_files,
    "write_file": write_file,
    "katalog_definicji": katalog_definicji,
}


async def wykonaj(polecenie: dict) -> dict:
    """Zawsze zwraca odpowiedz z `req_id`; blad operacji nie wychodzi wyjatkiem."""
    req_id = polecenie.get("req_id")
    typ = polecenie.get("type")
    operacja = OPERACJE.get(typ)
    if operacja is None:
        return {"req_id": req_id, "ok": False, "blad": f"nieznana operacja: {typ}",
                "kod": "nieznana_operacja"}
    dane = {k: v for k, v in polecenie.items() if k not in ("type", "req_id")}
    try:
        wynik = await operacja(dane)
    except BladOperacji as e:
        return {"req_id": req_id, "ok": False, "blad": e.komunikat, "kod": e.kod}
    except PozaZakresem:
        # bez powodu i sciezki z wyjatku: zdradzilyby uklad dysku
        return {"req_id": req_id, "ok": False, "blad": KOMUNIKAT_POZA_ZAKRESEM,
                "kod": "poza_zakresem"}
    except Exception as e:
        # bez str(e) i sladu stosu: tresc wyjatku moze niesc sekret
        return {"req_id": req_id, "ok": False,
                "blad": f"operacja {typ} nie powiodla sie ({type(e).__name__})",
                "kod": "blad_operacji"}
    return {"req_id": req_id, "ok": True, "wynik": wynik}
