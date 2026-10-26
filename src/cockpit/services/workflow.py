"""Model pliku workflow (.cockpit/workflows/<nazwa>.yaml) i zapis round-trip.

Plik jest zapisywany z kanwy, wiec komentarze, kolejnosc kluczy i cudzyslowy
musza przetrwac zapis. Dlatego sama `CommentedMap` z ruamel.yaml zostaje przy
modelu, a zmiany modelu nanosimy na nia w miejscu. Tylko ruamel.yaml, nigdy pyyaml.

Wczytanie nigdy nie rzuca wyjatku na bledne dane: zwraca liste `Problem`.
`Problem` jest wspolnym typem wyniku dla kanwy, ktora pokazuje wszystkie bledy naraz.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.constructor import DuplicateKeyError
from ruamel.yaml.error import YAMLError

# Kody problemow. Kanwa i testy dopasowuja sie po nich, nie po tresci komunikatow.
NIEPOPRAWNY_YAML = "niepoprawny_yaml"
ZDUPLIKOWANY_KLUCZ = "zduplikowany_klucz"
BRAK_KROKOW = "brak_krokow"
BLAD_POLA = "blad_pola"
BRAK_SKILLA = "brak_skilla"
BLAD_ODCZYTU = "blad_odczytu"

_BRAK = object()


class Poziom(StrEnum):
    blad = "blad"
    ostrzezenie = "ostrzezenie"


@dataclass(frozen=True)
class Problem:
    """Jeden problem znaleziony w pliku. `krok=None` oznacza problem calego pliku."""

    krok: str | None
    kod: str
    komunikat: str
    poziom: Poziom = Poziom.blad


def ma_bledy(problemy: list[Problem]) -> bool:
    """Prawda, gdy na liscie jest choc jeden problem poziomu `blad`."""
    return any(p.poziom == Poziom.blad for p in problemy)


class OnFail(BaseModel):
    retry: str
    max: int = Field(ge=1, strict=True)


class Krok(BaseModel):
    """Krok workflow. Nieznane klucze ignorujemy tutaj; odrzuca je walidator (HAR-9).

    Typy `int` i `bool` sa scisle: `fanout: true` ani `fanout: "3"` nie sa liczba,
    a w YAML-u latwo je wpisac przez przypadek ("yes", "1" przeszloby jako bool).
    """

    id: str
    skill: str
    needs: list[str] = []
    fanout: int = Field(default=1, ge=1, strict=True)
    consensus: int | None = Field(default=None, ge=1, strict=True)
    foreach: str | None = None
    approval: bool = Field(default=False, strict=True)
    optional: bool = Field(default=False, strict=True)
    require_distinct_vendors: bool = Field(default=False, strict=True)
    on_fail: OnFail | None = None


_POLA_KROKU = [nazwa for nazwa in Krok.model_fields if nazwa != "id"]


class Workflow(BaseModel):
    nazwa: str
    kroki: list[Krok]
    # id krokow, ktorych pola nie przeszly walidacji typow (HAR-8 traktuje je jako istniejace)
    niepoprawne: list[str] = []

    _mapa: CommentedMap = PrivateAttr()
    _bajty: bytes = PrivateAttr()
    _migawka: dict[str, Any] = PrivateAttr()
    _plik: Path = PrivateAttr()

    def surowy_krok(self, id_kroku: str) -> CommentedMap:
        """Surowa mapa kroku z pliku (z nieznanymi kluczami) dla walidatora."""
        for klucz, wartosc in self._mapa["steps"].items():
            if str(klucz) == id_kroku:
                return wartosc
        raise KeyError(id_kroku)

    def _zmieniony(self) -> bool:
        return self.model_dump() != self._migawka


def _nowy_yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096
    return yaml


def _lokalizacja(exc: YAMLError) -> str:
    znacznik = getattr(exc, "problem_mark", None)
    return f" (linia {znacznik.line + 1})" if znacznik is not None else ""


def _problemy_kroku(id_kroku: str, exc: ValidationError) -> list[Problem]:
    problemy = []
    for blad in exc.errors():
        sciezka = ".".join(str(c) for c in blad["loc"])
        if blad["type"] == "missing" and blad["loc"] == ("skill",):
            problemy.append(Problem(id_kroku, BRAK_SKILLA, "krok nie ma pola skill"))
        else:
            problemy.append(Problem(id_kroku, BLAD_POLA, f"{sciezka}: {blad['msg']}"))
    return problemy


def _zbuduj_kroki(
    kroki_surowe: CommentedMap,
) -> tuple[list[Krok], list[str], list[Problem]] | Problem:
    """Zwraca kroki i problemy pol albo `Problem` calego pliku (kolizja id po rzutowaniu na str)."""
    problemy: list[Problem] = []
    kroki: list[Krok] = []
    niepoprawne: list[str] = []
    widziane: set[str] = set()
    for klucz, surowy in kroki_surowe.items():
        id_kroku = str(klucz)
        if id_kroku in widziane:
            return Problem(None, ZDUPLIKOWANY_KLUCZ, f"zduplikowany klucz kroku: {id_kroku}")
        widziane.add(id_kroku)
        if not isinstance(surowy, CommentedMap):
            problemy.append(Problem(id_kroku, BLAD_POLA, "krok musi byc mapa pol"))
            niepoprawne.append(id_kroku)
            continue
        # klucze nie-napisy zostaja tylko w surowej mapie (HAR-9); nie sa kwargs ani problemem
        pola = {k: v for k, v in surowy.items() if isinstance(k, str)}
        try:
            kroki.append(Krok.model_validate({**pola, "id": id_kroku}))
        except ValidationError as exc:
            problemy.extend(_problemy_kroku(id_kroku, exc))
            niepoprawne.append(id_kroku)
    return kroki, niepoprawne, problemy


def wczytaj_workflow(plik: Path) -> tuple[Workflow | None, list[Problem]]:
    """Wczytuje workflow. `None` tylko gdy plik jest nieczytelny albo nie ma mapy `steps`."""
    try:
        bajty = plik.read_bytes()
        tekst = bajty.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, [Problem(None, BLAD_ODCZYTU, f"nie mozna odczytac pliku: {exc}")]

    try:
        mapa = _nowy_yaml().load(tekst)
    except RecursionError:
        return None, [Problem(None, NIEPOPRAWNY_YAML, "niepoprawny YAML: zbyt gleboko zagniezdzona struktura")]
    except DuplicateKeyError as exc:
        return None, [Problem(None, ZDUPLIKOWANY_KLUCZ, f"zduplikowany klucz{_lokalizacja(exc)}")]
    except YAMLError as exc:
        return None, [Problem(None, NIEPOPRAWNY_YAML, f"niepoprawny YAML{_lokalizacja(exc)}")]

    kroki_surowe = mapa.get("steps") if isinstance(mapa, CommentedMap) else None
    if not isinstance(kroki_surowe, CommentedMap):
        return None, [Problem(None, BRAK_KROKOW, "brak mapy steps")]

    try:
        wynik = _zbuduj_kroki(kroki_surowe)
    except RecursionError:
        return None, [Problem(None, NIEPOPRAWNY_YAML, "niepoprawny YAML: zbyt gleboko zagniezdzona struktura")]
    if isinstance(wynik, Problem):
        return None, [wynik]
    kroki, niepoprawne, problemy = wynik

    nazwa = mapa.get("name")
    workflow = Workflow(
        nazwa=str(nazwa) if nazwa is not None else plik.stem,
        kroki=kroki,
        niepoprawne=niepoprawne,
    )
    workflow._mapa = mapa
    workflow._bajty = bajty
    workflow._migawka = workflow.model_dump()
    workflow._plik = plik
    return workflow, problemy


def _wartosc_pola(krok: Krok, pole: str) -> Any:
    wartosc = getattr(krok, pole)
    if pole == "on_fail":
        return None if wartosc is None else {"retry": wartosc.retry, "max": wartosc.max}
    return wartosc


def _domyslna(pole: str) -> Any:
    return Krok.model_fields[pole].get_default(call_default_factory=True)


def _ustaw_w_miejscu(mapa: CommentedMap, pole: str, wartosc: Any) -> None:
    """Zmienia wartosc zachowujac istniejacy kontener (styl flow, komentarze)."""
    obecna = mapa.get(pole, _BRAK)
    if isinstance(wartosc, list) and isinstance(obecna, list):
        obecna[:] = wartosc
    elif isinstance(wartosc, dict) and isinstance(obecna, CommentedMap):
        for k in [k for k in obecna if k not in wartosc]:
            del obecna[k]
        for k, v in wartosc.items():
            obecna[k] = v
    elif isinstance(wartosc, dict):
        nowa = CommentedMap(wartosc)
        nowa.fa.set_flow_style()
        mapa[pole] = nowa
    else:
        mapa[pole] = wartosc


def _nanies_krok(mapa: CommentedMap, krok: Krok) -> None:
    for pole in _POLA_KROKU:
        wartosc = _wartosc_pola(krok, pole)
        obecna = mapa.get(pole, _BRAK)
        if obecna is _BRAK:
            # pol domyslnych nieobecnych w pliku nie dopisujemy; skill zawsze
            if wartosc == _domyslna(pole) and pole != "skill":
                continue
        elif obecna == wartosc and isinstance(obecna, bool) == isinstance(wartosc, bool):
            continue
        if wartosc is None:
            if obecna is not _BRAK:
                del mapa[pole]
            continue
        _ustaw_w_miejscu(mapa, pole, wartosc)


def _przestaw_kroki(kroki_mapa: CommentedMap, kolejnosc: list[str]) -> None:
    """Ustawia kolejnosc kluczy `steps` w miejscu, przenoszac te same wezly.

    Kroki poprawne zajmuja pozycje, ktore juz zajmowaly poprawne klucze, w kolejnosci
    `workflow.kroki`; kroki niepoprawne (poza `kroki`) zostaja na swoich pozycjach.
    Komentarze doczepione do kluczy (`ca.items`) przenosza sie razem z kluczem.
    """
    klucze = {str(k): k for k in kroki_mapa}
    poprzednia = list(klucze)
    docelowa = list(poprzednia)
    pozycje = [i for i, id_kroku in enumerate(poprzednia) if id_kroku in kolejnosc]
    for i, id_kroku in zip(pozycje, kolejnosc, strict=True):
        docelowa[i] = id_kroku
    if docelowa == poprzednia:
        return
    wezly = {k: kroki_mapa[k] for k in klucze.values()}
    komentarze = {k: kroki_mapa.ca.items[k] for k in klucze.values() if k in kroki_mapa.ca.items}
    for k in klucze.values():
        del kroki_mapa[k]
        kroki_mapa.ca.items.pop(k, None)
    for id_kroku in docelowa:
        klucz = klucze[id_kroku]
        kroki_mapa[klucz] = wezly[klucz]
        if klucz in komentarze:
            kroki_mapa.ca.items[klucz] = komentarze[klucz]


def _nanies_zmiany(workflow: Workflow) -> None:
    mapa = workflow._mapa
    kroki_mapa: CommentedMap = mapa["steps"]

    nazwa_w_pliku = mapa.get("name", workflow._plik.stem)
    if workflow.nazwa != str(nazwa_w_pliku):
        if "name" in mapa:
            mapa["name"] = workflow.nazwa
        else:
            mapa.insert(0, "name", workflow.nazwa)

    istniejace = {str(k): k for k in kroki_mapa}
    aktualne = {k.id for k in workflow.kroki}
    # kroki niepoprawne zostaja w surowej mapie nietkniete
    for id_kroku, klucz in istniejace.items():
        if id_kroku not in aktualne and id_kroku not in workflow.niepoprawne:
            del kroki_mapa[klucz]

    for krok in workflow.kroki:
        if krok.id in istniejace:
            _nanies_krok(kroki_mapa[istniejace[krok.id]], krok)
        else:
            nowy = CommentedMap()
            nowy["skill"] = krok.skill
            kroki_mapa[krok.id] = nowy
            _nanies_krok(nowy, krok)

    _przestaw_kroki(kroki_mapa, [k.id for k in workflow.kroki])


def zapisz_workflow(workflow: Workflow, plik: Path) -> None:
    """Zapisuje workflow. Bez zmian modelu plik jest identyczny bajt w bajt z oryginalem."""
    if not workflow._zmieniony():
        plik.write_bytes(workflow._bajty)
        return

    _nanies_zmiany(workflow)
    wyjscie = io.StringIO()
    _nowy_yaml().dump(workflow._mapa, wyjscie)
    bajty = wyjscie.getvalue().encode("utf-8")
    plik.write_bytes(bajty)
    workflow._bajty = bajty
    workflow._migawka = workflow.model_dump()
