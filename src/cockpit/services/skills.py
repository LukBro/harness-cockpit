"""Skill jako byt w kodzie: odczyt/zapis SKILL.md, rejestr i materializacja (HAR-5).

Modul nie dotyka bazy - rejestr zyje na plikach. python-frontmatter sluzy
wylacznie do podzialu na frontmatter i brief (YAMLHandler.split); frontmatter
parsuje ruamel.yaml w trybie round-trip, zeby przetrwaly komentarze i kolejnosc.
"""
from __future__ import annotations

import io
import re
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from frontmatter.default_handlers import YAMLHandler
from pydantic import BaseModel, ConfigDict, PrivateAttr, ValidationError
from ruamel.yaml import YAML, YAMLError
from ruamel.yaml.comments import CommentedMap

from cockpit.services.errors import BlednySkill, NieZnaleziono
from cockpit.services.invariants import Rola, profil_roli
from cockpit.services.walidacja import (
    PREFIKS_WERDYKTU,
    KontraktWerdyktu,
    waliduj_definicje,
)

# Zrodla od najwyzszego priorytetu (wygrywa pierwsze, w ktorym jest id).
# (baza, podkatalog, scisle): scisle = model kokpitu; nie scisle = format innych narzedzi.
ZRODLA: tuple[tuple[str, str, bool], ...] = (
    ("repo", ".cockpit/skills", True),
    ("repo", ".claude/skills", False),
    ("repo", ".agents/skills", False),
    ("dom", ".cockpit/skills", True),
)

NAZWA_PLIKU = "SKILL.md"
KATALOG_RUNU = Path(".cockpit") / "_run" / "skills"
_GRANICA = re.compile(r"^-{3,}\s*$")
_POLA_LISTOWE = ("consumes", "produces", "needs")
_POLA = ("id", "name", "role", *_POLA_LISTOWE)
KLUCZ_WERDYKTU = "verdict"
# klucze mapy `verdict` we frontmatterze (`pass` w YAML to `pass_` w modelu)
_KLUCZE_WERDYKTU = frozenset({"line", "prefix", "pass", "fail"})


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    return y


class Skill(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    id: str
    name: str
    role: Rola
    consumes: list[str] = []
    produces: list[str] = []
    needs: list[str] = []
    verdict: KontraktWerdyktu | None = None
    brief: str
    katalog: Path

    # surowy tekst pliku, mapa frontmatteru i migawka pol z chwili wczytania
    _surowy: bytes = PrivateAttr(default=b"")
    _mapa: Any = PrivateAttr(default=None)
    _migawka: dict[str, Any] = PrivateAttr(default_factory=dict)

    def _stan(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role.value,
            "consumes": list(self.consumes),
            "produces": list(self.produces),
            "needs": list(self.needs),
            "brief": self.brief,
        }


class Pominiety(BaseModel):
    plik: Path
    powod: str


class Rejestr(BaseModel):
    skille: dict[str, Skill] = {}
    pominiete: list[Pominiety] = []


def _podziel(tekst: str, plik: object) -> tuple[str, str]:
    """Zwraca (frontmatter, brief). Brief to wszystko po linii zamykajacej, bajt w bajt.

    Konwencja: brief NIE zawiera konca linii zamykajacego delimitera; zaczyna sie
    od pierwszego znaku nastepnej linii. split() z python-frontmatter zjada biale
    znaki za delimiterem (\\s*), wiec briefu nie bierzemy z niego - tylko frontmatter.
    """
    frontmatter, _, brief = _podziel_na_linie(tekst, plik)
    return frontmatter, brief


def _podziel_na_linie(tekst: str, plik: object) -> tuple[str, str, str]:
    """(frontmatter z split(), frontmatter dokladnie jak w tekscie, brief); wspolne dla `_podziel`
    i `podziel_skill`. Drugi element to linie miedzy granicami z oryginalnymi koncami linii."""
    handler = YAMLHandler()
    if not handler.detect(tekst):
        raise BlednySkill(f"brak frontmattera w {plik}")
    linie = tekst.splitlines(keepends=True)
    for i in range(1, len(linie)):
        if _GRANICA.match(linie[i]):
            # zamkniecie potwierdzone - dopiero teraz split() nie rzuci ValueError
            fm, _ = handler.split(tekst)
            return fm, "".join(linie[1:i]), "".join(linie[i + 1 :])
    raise BlednySkill(f"niezamkniety frontmatter w {plik}")


def podziel_skill(tekst: str, katalog: str) -> tuple[str, str]:
    """(frontmatter, brief) calego SKILL.md bajt w bajt (HAR-180): frontmatter to tekst miedzy
    liniami granicznymi bez nich, brief to wszystko po zamykajacej. BlednySkill bez frontmattera."""
    _, frontmatter, brief = _podziel_na_linie(tekst, f"'{katalog}'")
    return frontmatter, brief


def _rozbierz(plik: Path) -> tuple[bytes, CommentedMap, str]:
    """Bajty, mapa frontmattera i brief; BlednySkill, gdy plik nie jest parsowalnym skillem."""
    try:
        surowy = plik.read_bytes()
    except OSError as e:
        raise BlednySkill(f"nie mozna odczytac SKILL.md: {plik}") from e
    try:
        tekst = surowy.decode("utf-8")
    except UnicodeDecodeError as e:
        raise BlednySkill(f"SKILL.md nie jest w UTF-8: {plik}") from e
    fm, brief = _podziel(tekst, plik)
    return surowy, _mapa_frontmattera(fm, plik), brief


def _mapa_frontmattera(fm: str, opis: object) -> CommentedMap:
    """Mapa z tekstu frontmattera; BlednySkill, gdy to nie poprawna mapa YAML."""
    try:
        mapa = _yaml().load(fm)
    except (YAMLError, RecursionError, ValueError, TypeError, OverflowError) as e:
        # ValueError/OverflowError: konstrukcja wartosci (np. data 2024-02-30), nie skladnia
        raise BlednySkill(f"niepoprawny frontmatter w {opis}: {e}") from e
    if mapa is None:
        mapa = CommentedMap()
    if not isinstance(mapa, CommentedMap):
        raise BlednySkill(f"frontmatter w {opis} nie jest mapa")
    return mapa


def _pola_skilla(mapa: CommentedMap, katalog: str, *, scisle: bool, opis: object) -> dict | None:
    """Wspolna walidacja pol frontmattera. None: zrodlo niescisle, a brak `role` - to nie skill
    kokpitu. Reszta bledow to BlednySkill."""
    dane: dict[str, Any] = {k: mapa[k] for k in _POLA if k in mapa}
    if not scisle:
        if "role" not in dane:
            return None
        dane.setdefault("id", katalog)
        dane.setdefault("name", katalog)
    for pole in ("id", "name", "role"):
        if pole not in dane:
            raise BlednySkill(f"brak pola '{pole}' w {opis}")
    dozwolone = ", ".join(r.value for r in Rola)
    if str(dane["role"]) not in {r.value for r in Rola}:
        raise BlednySkill(f"nieznana rola '{dane['role']}' w {opis} (dozwolone: {dozwolone})")
    if str(dane["id"]) != katalog:
        raise BlednySkill(f"id '{dane['id']}' rozni sie od nazwy katalogu '{katalog}' w {opis}")
    return dane


def metadane_skilla(frontmatter: str, katalog: str, *, scisle: bool) -> tuple[str, str, str] | None:
    """(id, name, role) z samego frontmattera - bez briefu (HAR-67, BRO-86).

    None: zrodlo niescisle i brak `role` (nie skill kokpitu). BlednySkill: skill zepsuty."""
    opis = f"'{katalog}'"
    dane = _pola_skilla(_mapa_frontmattera(frontmatter, opis), katalog, scisle=scisle, opis=opis)
    if dane is None:
        return None
    id_skilla, nazwa, rola = dane["id"], dane["name"], dane["role"]
    if not isinstance(id_skilla, str) or not isinstance(nazwa, str):
        raise BlednySkill(f"id i name skilla {opis} musza byc napisami")
    return id_skilla, nazwa, str(rola)


def _lista_napisow(wartosc: Any) -> tuple[str, ...] | None:
    """Krotka niepustych napisow z listy YAML; None, gdy to nie lista takich napisow."""
    if not isinstance(wartosc, list):
        return None
    if not all(isinstance(x, str) and x for x in wartosc):
        return None
    return tuple(str(x) for x in wartosc)


def _kontrakt_werdyktu(surowy: Any) -> KontraktWerdyktu | str:
    """Kontrakt z mapy `verdict` frontmattera albo powod odrzucenia (napis)."""
    if not isinstance(surowy, CommentedMap):
        return "verdict musi być mapą"
    for klucz in surowy:
        if klucz not in _KLUCZE_WERDYKTU:
            return f"nieznany klucz {klucz}"
    przechodzi = _lista_napisow(surowy.get("pass"))
    if not przechodzi:
        return "pass musi być niepustą listą napisów"
    oblewa = _lista_napisow(surowy.get("fail", []))
    if oblewa is None:
        return "fail musi być listą napisów"
    linia = surowy.get("line", 1)
    if not isinstance(linia, int) or isinstance(linia, bool) or linia < 1:
        return "line musi być liczbą całkowitą >= 1"
    prefiks = surowy.get("prefix", PREFIKS_WERDYKTU)
    if not isinstance(prefiks, str) or not prefiks:
        return "prefix musi być niepustym napisem"
    zablokowane = {x.casefold() for x in oblewa}
    for wartosc in przechodzi:
        if wartosc.casefold() in zablokowane:
            return f"pass i fail mają wspólną wartość: {wartosc}"
    return KontraktWerdyktu(pass_=przechodzi, fail=oblewa, line=int(linia), prefix=str(prefiks))


def _zbuduj_skill(
    mapa: CommentedMap, brief: str, katalog: Path, *, scisle: bool, opis: object,
) -> Skill | None:
    """Wspolne reguly skilla dla pliku i tekstu (HAR-180). None: zrodlo niescisle, brak `role`.
    `opis` trafia do komunikatow: sciezka pliku albo nazwa katalogu."""
    dane = _pola_skilla(mapa, katalog.name, scisle=scisle, opis=opis)
    if dane is None:
        return None
    # HAR-175: surowa mapa frontmattera (nie `dane` z _POLA, ktore gubi write/model/tier/command);
    # klucze niebedace napisami (`1: x`, `true: x`) nie sa kluczami definicji
    naruszenia = waliduj_definicje({k: v for k, v in mapa.items() if isinstance(k, str)})
    if naruszenia:
        raise BlednySkill(f"{opis}: " + "; ".join(str(n) for n in naruszenia))
    verdict = None
    if KLUCZ_WERDYKTU in mapa:
        wynik = _kontrakt_werdyktu(mapa[KLUCZ_WERDYKTU])
        if isinstance(wynik, str):
            raise BlednySkill(f"niepoprawny verdict w {opis}: {wynik}")
        verdict = wynik
    try:
        skill = Skill(
            **{
                k: ([str(x) for x in v] if k in _POLA_LISTOWE and isinstance(v, list) else v)
                for k, v in dane.items()
            },
            verdict=verdict,
            brief=brief,
            katalog=katalog,
        )
    except (ValidationError, TypeError) as e:
        raise BlednySkill(f"niepoprawny skill w {opis}: {e}") from e
    # rola bez zapisu oddaje artefakt jako wynik agenta (HAR-160): wynik jest jeden
    if not profil_roli(skill.role).zapis and len(skill.produces) > 1:
        raise BlednySkill(
            f"skill {skill.id}: rola {skill.role.value} oddaje jeden artefakt, "
            f"produces ma {len(skill.produces)}: {', '.join(skill.produces)}"
        )
    if skill.verdict is not None and not skill.produces:
        raise BlednySkill(f"niepoprawny verdict w {opis}: verdict wymaga produces")
    skill._mapa = mapa
    skill._migawka = skill._stan()
    return skill


def _wczytaj(plik: Path, *, scisle: bool) -> Skill | Pominiety:
    # pliki zrodel konwencyjnych pochodza z innych narzedzi: nieparsowalny plik
    # to dla nas "nie nasz skill", nie blad calego rejestru
    try:
        surowy, mapa, brief = _rozbierz(plik)
    except BlednySkill as e:
        if scisle:
            raise
        return Pominiety(plik=plik, powod=f"{e} - to nie skill kokpitu")
    skill = _zbuduj_skill(mapa, brief, plik.parent, scisle=scisle, opis=plik)
    if skill is None:
        return Pominiety(plik=plik, powod="brak pola role - to nie skill kokpitu")
    skill._surowy = surowy
    return skill


def skill_z_tekstu(tekst: str, katalog: str, *, scisle: bool) -> Skill | None:
    """Skill z calego tekstu SKILL.md, bez dysku (HAR-180), tymi samymi regulami co z pliku.

    None: zrodlo niescisle, a brak `role` albo tekst nieparsowalny - nie skill kokpitu.
    Reszta bledow to BlednySkill; komunikaty wskazuja nazwe katalogu."""
    opis = f"'{katalog}'"
    try:
        frontmatter, brief = _podziel(tekst, opis)
        mapa = _mapa_frontmattera(frontmatter, opis)
    except BlednySkill:
        if scisle:
            raise
        return None
    return _zbuduj_skill(mapa, brief, Path(katalog), scisle=scisle, opis=opis)


def wczytaj_skill(plik: Path) -> Skill:
    """Wczytuje skill w modelu scislym kokpitu (id, name, role wymagane)."""
    wynik = _wczytaj(plik, scisle=True)
    assert isinstance(wynik, Skill)
    return wynik


def zapisz_skill(skill: Skill, plik: Path) -> None:
    """Bez zmian w polach zapisuje surowy tekst oryginalu (bajt w bajt).

    Po zmianie przenosi zmienione pola do oryginalnej mapy ruamel i sklada
    `---\\n` + dump + `---\\n` + brief. Nieznane klucze, komentarze i kolejnosc
    zostaja; wciecia sa ujednolicone do stylu modulu.
    """
    stan = skill._stan()
    if stan == skill._migawka and skill._surowy:
        plik.write_bytes(skill._surowy)
        return
    mapa = skill._mapa if skill._mapa is not None else CommentedMap()
    for pole in _POLA:
        if stan[pole] != skill._migawka.get(pole):
            mapa[pole] = stan[pole]
    wyjscie = io.StringIO()
    _yaml().dump(mapa, wyjscie)
    plik.write_bytes(("---\n" + wyjscie.getvalue() + "---\n" + skill.brief).encode("utf-8"))


def rozstrzygnij_rejestr(repo: Path, dom: Path) -> Rejestr:
    """Wczytuje wszystkie zrodla (kokpitowe rzucaja takze gdy skill jest przesloniety),
    potem przeslania: wygrywa pierwsze zrodlo z ZRODLA, w ktorym jest id."""
    bazy = {"repo": repo, "dom": dom}
    rejestr = Rejestr()
    for baza, podkatalog, scisle in ZRODLA:
        zrodlo = bazy[baza] / podkatalog
        if not zrodlo.is_dir():
            continue
        for katalog in sorted(p for p in zrodlo.iterdir() if p.is_dir()):
            plik = katalog / NAZWA_PLIKU
            if not plik.is_file():
                continue
            wynik = _wczytaj(plik, scisle=scisle)
            if isinstance(wynik, Pominiety):
                rejestr.pominiete.append(wynik)
            else:
                rejestr.skille.setdefault(wynik.id, wynik)
    return rejestr


def materializuj(ids: Iterable[str], rejestr: Rejestr, katalog: Path) -> Path:
    """Kopiuje skille z `ids` do <katalog>/.cockpit/_run/skills/<id>/ i zwraca ten katalog.

    Kopia jest prawdziwa (shutil.copytree, symlinks=False): edycja skilla w rejestrze
    po materializacji nie zmienia tego, co czyta agent.

    Powtorna materializacja CZYSCI: kasujemy wylacznie <katalog>/.cockpit/_run/skills
    i kopiujemy zestaw od nowa, wiec stary i nowy zestaw sie nie mieszaja. Wszystkie
    id sprawdzamy przed jakimkolwiek kopiowaniem; id spoza rejestru to NieZnaleziono.

    Swiadomy wyjatek od niezmiennika run_dir_outside_repo (uzasadnienie BRO-100):
    kopia laduje w cwd agenta, pod .cockpit/_run/ ignorowanym przez git, bo daje
    przenosnosc miedzy backendami. Artefakty runu (logi, wyniki) dalej leza poza repo.
    """
    wybrane = list(dict.fromkeys(ids))
    for id in wybrane:
        if id not in rejestr.skille:
            raise NieZnaleziono(f"skill '{id}' nie istnieje w rejestrze")
    cel = katalog / KATALOG_RUNU
    if cel.exists():
        shutil.rmtree(cel)
    cel.mkdir(parents=True)
    for id in wybrane:
        shutil.copytree(rejestr.skille[id].katalog, cel / id, symlinks=False)
    return cel
