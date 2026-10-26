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
from cockpit.services.invariants import Rola

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


def _podziel(tekst: str, plik: Path) -> tuple[str, str]:
    """Zwraca (frontmatter, brief). Brief to wszystko po linii zamykajacej, bajt w bajt.

    Konwencja: brief NIE zawiera konca linii zamykajacego delimitera; zaczyna sie
    od pierwszego znaku nastepnej linii. split() z python-frontmatter zjada biale
    znaki za delimiterem (\\s*), wiec briefu nie bierzemy z niego - tylko frontmatter.
    """
    handler = YAMLHandler()
    if not handler.detect(tekst):
        raise BlednySkill(f"brak frontmattera w {plik}")
    linie = tekst.splitlines(keepends=True)
    for i in range(1, len(linie)):
        if _GRANICA.match(linie[i].rstrip("\r\n")):
            # zamkniecie potwierdzone - dopiero teraz split() nie rzuci ValueError
            fm, _ = handler.split(tekst)
            return fm, "".join(linie[i + 1 :])
    raise BlednySkill(f"niezamkniety frontmatter w {plik}")


def _rozbierz(plik: Path) -> tuple[bytes, CommentedMap, str]:
    """Bajty, mapa frontmattera i brief; BlednySkill, gdy plik nie jest parsowalnym skillem."""
    surowy = plik.read_bytes()
    try:
        tekst = surowy.decode("utf-8")
    except UnicodeDecodeError as e:
        raise BlednySkill(f"SKILL.md nie jest w UTF-8: {plik}") from e
    fm, brief = _podziel(tekst, plik)
    try:
        mapa = _yaml().load(fm)
    except YAMLError as e:
        raise BlednySkill(f"niepoprawny frontmatter w {plik}: {e}") from e
    if mapa is None:
        mapa = CommentedMap()
    if not isinstance(mapa, CommentedMap):
        raise BlednySkill(f"frontmatter w {plik} nie jest mapa")
    return surowy, mapa, brief


def _wczytaj(plik: Path, *, scisle: bool) -> Skill | Pominiety:
    # pliki zrodel konwencyjnych pochodza z innych narzedzi: nieparsowalny plik
    # to dla nas "nie nasz skill", nie blad calego rejestru
    try:
        surowy, mapa, brief = _rozbierz(plik)
    except BlednySkill as e:
        if scisle:
            raise
        return Pominiety(plik=plik, powod=f"{e} - to nie skill kokpitu")

    katalog = plik.parent
    dane: dict[str, Any] = {k: mapa[k] for k in _POLA if k in mapa}
    if not scisle:
        if "role" not in dane:
            return Pominiety(plik=plik, powod="brak pola role - to nie skill kokpitu")
        dane.setdefault("id", katalog.name)
        dane.setdefault("name", katalog.name)
    for pole in ("id", "name", "role"):
        if pole not in dane:
            raise BlednySkill(f"brak pola '{pole}' w {plik}")
    dozwolone = ", ".join(r.value for r in Rola)
    if str(dane["role"]) not in {r.value for r in Rola}:
        raise BlednySkill(f"nieznana rola '{dane['role']}' w {plik} (dozwolone: {dozwolone})")
    if str(dane["id"]) != katalog.name:
        raise BlednySkill(
            f"id '{dane['id']}' rozni sie od nazwy katalogu '{katalog.name}' w {plik}"
        )
    try:
        skill = Skill(
            **{
                k: ([str(x) for x in v] if k in _POLA_LISTOWE and isinstance(v, list) else v)
                for k, v in dane.items()
            },
            brief=brief,
            katalog=katalog,
        )
    except (ValidationError, TypeError) as e:
        raise BlednySkill(f"niepoprawny skill w {plik}: {e}") from e
    skill._surowy = surowy
    skill._mapa = mapa
    skill._migawka = skill._stan()
    return skill


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
