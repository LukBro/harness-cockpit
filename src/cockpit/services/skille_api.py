"""Skille projektu dla API (HAR-180): lista z cache katalogu i szczegoly z pliku przez runnera.

Lista czyta wylacznie tabele `definicja` (BRO-93, HAR-67), wiec dziala takze offline, z data
ostatniego odswiezenia. Szczegoly czytaja biezacy SKILL.md przez runnera PROJEKTU i oddaja
frontmatter i brief bajt w bajt; parsowanie robi `skills.py` (D23), runner oddaje surowy plik.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from sqlalchemy import Select, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.models import Definicja, Project, Runner

from runner.operacje import DOMYSLNY_LIMIT_ODCZYTU_KB

from . import projekty, zapytania_runnera
from .errors import BlednySkill, NiespojneDane, NieZnaleziono, PlikPozaZakresem, StanNiemozliwy
from .katalog import _iso, _sprawdz_ksztalt, _wiersze
from .skills import NAZWA_PLIKU, ZRODLA, Skill, podziel_skill, skill_z_tekstu
from .zapytania_runnera import Wyslij

logger = logging.getLogger(__name__)

# Nazwa katalogu skilla / pliku workflow trafia do sciezki na dysku uzytkownika: bez ukosnikow,
# kropek na poczatku, spacji i znakow sterujacych (HAR-181).
WZORZEC_BEZPIECZNEJ_NAZWY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
LIMIT_PLIKU_SKILLA_B = DOMYSLNY_LIMIT_ODCZYTU_KB * 1024   # limit odczytu runnera, w bajtach
KOMUNIKAT_BRAK_PLIKU = "sciezka nie istnieje"   # stala tresc odmowy runnera (nie_istnieje)
# stale tresci odmow runnera (`runner.operacje._blad_dysku`, `_czytaj_plik`) dotyczace samego pliku
_ODMOWY_DOTYCZACE_PLIKU = frozenset({
    "sciezka jest katalogiem", "plik nie jest tekstem UTF-8",
    "plik nie jest tekstem (zawiera bajt NUL)", "brak uprawnien do pliku lub katalogu",
    "sciezka nie jest zwyklym plikiem", "element sciezki nie jest katalogiem",
    "blad dysku przy operacji na pliku",
})
_PREFIKS_ODMOWY_ZA_DUZY = "plik jest wiekszy niz limit "   # dalej "<N> KB" z konfiguracji runnera
# zakres zapisu -> podkatalog skilli; `~/` to katalog domowy runnera (rozwija go runner)
_PODKATALOG_ZAPISU = {"project": ".cockpit/skills", "global": "~/.cockpit/skills"}
_ZAKRES_BEZ_KATALOGU = {"project": "lokalny", "global": "globalny"}
# Wyjatki, ktore ruamel naprawde rzuca przy konstrukcji wartosci z zlym tagiem i ktorych parser
# skilli (`skills._mapa_frontmattera`) nie tlumaczy: `!!bool x` (KeyError), `!!set x`
# (AttributeError), `!!int 0b` (IndexError); ValueError tlumaczy sam parser.
_BLEDY_KONSTRUKCJI_YAML = (KeyError, AttributeError, IndexError)


def _zapytanie_skilli(projekt: Project) -> Select:
    """Wiersze skilli tego projektu: tego zespolu, jego runnera i jego katalogu.

    Wiersz bez zakresu pochodzi sprzed migracji 0015 (przed odswiezeniem katalogu) - nie jest
    jeszcze skillem w nowym formacie, wiec go tu nie ma."""
    return select(Definicja).where(
        Definicja.tenant_id == projekt.tenant_id, Definicja.runner_id == projekt.runner_id,
        Definicja.projekt == projekt.path, Definicja.rodzaj == "skill",
        Definicja.zakres.is_not(None))


def _zrodlo_scisle(sciezka: str) -> bool:
    """Czy sciezka z cache (repo wzgledna, dom z `~/`) lezy w zrodle w modelu scislym kokpitu."""
    wzgledna = sciezka.removeprefix("~/")
    return any(scisle and wzgledna.startswith(f"{podkatalog}/") for _, podkatalog, scisle in ZRODLA)


async def wiersze_skilli(s: AsyncSession, projekt: Project) -> list[Definicja]:
    """Wiersze skilli projektu z cache katalogu (dla walidatora workflow, HAR-181)."""
    wynik = await s.execute(_zapytanie_skilli(projekt))
    return list(wynik.scalars().all())


async def rejestr_projektu(s: AsyncSession, tenant_id: str, project_id: str) -> dict:
    """Rozstrzygniety rejestr skilli projektu z cache, posortowany po id; tylko baza."""
    projekt = await projekty.projekt_zespolu(s, tenant_id, project_id)
    wiersze = sorted(
        (await s.execute(_zapytanie_skilli(projekt))).scalars().all(), key=lambda w: w.klucz)
    najstarszy = min((w.odswiezono_at for w in wiersze), default=None)
    return {
        "odswiezono_at": None if najstarszy is None else _iso(najstarszy),
        "skille": [{
            "id": w.klucz, "name": w.nazwa, "role": w.rola, "consumes": w.consumes,
            "produces": w.produces, "zakres": w.zakres, "problem": w.problem,
        } for w in wiersze],
    }


async def skill_projektu(
    s: AsyncSession, tenant_id: str, project_id: str, skill_id: str,
    polaczone: frozenset[str], wyslij: Wyslij,
) -> dict:
    """Szczegoly skilla: pola z biezacego pliku, zakres i sciezka z cache.

    Skill spoza cache projektu to NieZnaleziono (bez pytania runnera); plik, ktory po odswiezeniu
    przestal byc poprawnym skillem, to BlednySkill z komunikatem parsera."""
    projekt = await projekty.projekt_zespolu(s, tenant_id, project_id)
    wiersz = (await s.execute(
        _zapytanie_skilli(projekt).where(Definicja.klucz == skill_id))).scalar_one_or_none()
    if wiersz is None:
        raise NieZnaleziono("skill nieznany")
    tresc = await projekty.czytaj_plik_projektu(
        s, tenant_id, projekt, wiersz.sciezka, polaczone, wyslij)
    skill = skill_z_tekstu(tresc, wiersz.klucz, scisle=_zrodlo_scisle(wiersz.sciezka))
    if skill is None:
        raise BlednySkill(
            f"plik skilla '{wiersz.klucz}' nie jest skillem kokpitu: "
            "brak frontmattera albo pola role")
    # skill_z_tekstu wyzej juz podzielil ten tekst bez bledu, wiec nazwa katalogu (tylko do
    # komunikatu BlednySkill) nie ma tu skutku: mutant argumentu rownowazny
    frontmatter, brief = podziel_skill(tresc, wiersz.klucz)  # pragma: no mutate
    return {
        "id": skill.id, "name": skill.name, "role": skill.role.value,
        "consumes": list(skill.consumes), "produces": list(skill.produces),
        "zakres": wiersz.zakres, "sciezka": wiersz.sciezka,
        "frontmatter": frontmatter, "brief": brief,
    }


# ---- zapis i usuniecie skilla z UI (HAR-181) -------------------------------------------------

def sprawdz_bezpieczna_nazwe(nazwa: str) -> None:
    """Nazwa katalogu skilla trafia do sciezki na dysku uzytkownika: wzorzec, nie czarna lista."""
    if not WZORZEC_BEZPIECZNEJ_NAZWY.fullmatch(nazwa):
        raise BlednySkill(
            "id skilla musi byc nazwa katalogu: litery, cyfry, _ - . (do 64 znakow, "
            "pierwszy znak litera lub cyfra)")


def _plik_skilla(skill_id: str, frontmatter: str, brief: str) -> tuple[str, Skill]:
    """Tekst SKILL.md zlozony bajt w bajt jak w `SkillSzczegoly` i skill z jego walidacji
    (reguly sciesle jak przy wczytaniu): zly plik to BlednySkill PRZED zapisem."""
    tekst = "---\n" + frontmatter + "---\n" + brief
    try:
        bajty = tekst.encode()
    except UnicodeEncodeError:
        raise BlednySkill("tresc skilla nie jest poprawnym tekstem UTF-8") from None
    if len(bajty) > LIMIT_PLIKU_SKILLA_B:   # runner nie odczytalby takiego pliku (`za_duzy`)
        raise NiespojneDane(
            f"plik skilla ma {len(bajty)} bajtow; limit odczytu runnera to "
            f"{LIMIT_PLIKU_SKILLA_B // 1024} KB")
    if "\0" in tekst:   # runner odrzuca taki plik przy odczycie (`binarny`, operacje.py)
        raise NiespojneDane("plik skilla zawiera bajt NUL; runner nie odczyta takiego pliku")
    try:
        # zrodlo scisle zawsze oddaje skill albo rzuca BlednySkill
        return tekst, skill_z_tekstu(tekst, skill_id, scisle=True)
    except _BLEDY_KONSTRUKCJI_YAML:
        raise BlednySkill("niepoprawny frontmatter: nieprawidlowa wartosc") from None


def _sciezka_skilla(zakres: str, skill_id: str) -> str:
    return f"{_PODKATALOG_ZAPISU[zakres]}/{skill_id}/{NAZWA_PLIKU}"


async def _plik_istnieje(
    s: AsyncSession, tenant_id: str, projekt: Project, wzgledna: str,
    polaczone: frozenset[str], wyslij: Wyslij,
) -> bool:
    """Istnienie pliku sprawdza dysk (przez runnera projektu), nie cache katalogu. Runner nie
    ma osobnego typu bledu dla braku pliku, wiec brak rozpoznajemy po jego stalym komunikacie;
    kazda inna odmowa (offline, zakres, limit czasu, zly ksztalt) idzie dalej."""
    try:
        await projekty.czytaj_plik_projektu(s, tenant_id, projekt, wzgledna, polaczone, wyslij)
    except StanNiemozliwy as e:
        if str(e) == KOMUNIKAT_BRAK_PLIKU:
            return False
        raise
    return True


async def _zablokuj_runnera(s: AsyncSession, tenant_id: str, runner_id: str) -> None:
    """Blokada wiersza Runner (`FOR UPDATE`) do konca transakcji; brak wiersza to StanNiemozliwy."""
    blokada = (await s.execute(
        select(Runner).where(Runner.id == runner_id, Runner.tenant_id == tenant_id)
        .with_for_update())).scalar_one_or_none()
    if blokada is None:
        raise StanNiemozliwy("runner zniknal w trakcie odswiezania katalogu")


async def _projekt_do_zapisu(
    s: AsyncSession, tenant_id: str, project_id: str, zakres: str,
) -> Project:
    """Projekt zablokowany do commitu. Skill globalny lezy w katalogu domowym runnera, wspolnym
    dla wszystkich jego projektow, wiec blokada projektu go nie szereguje: zapisy `global`
    szereguje dodatkowo wiersz Runner (zajetosc id i zapis w jednej kolejce)."""
    projekt = await projekty.projekt_zespolu(s, tenant_id, project_id, blokuj=True)
    if zakres == "global":
        await _zablokuj_runnera(s, tenant_id, projekt.runner_id)
    return projekt


async def _odswiez_katalog_projektu(
    s: AsyncSession, tenant_id: str, projekt: Project, polaczone: frozenset[str], wyslij: Wyslij,
) -> None:
    """Jak `katalog.odswiez_katalog`, ale pyta runnera TEGO projektu, nie pierwszego online
    (HAR-181): inaczej katalog innego runnera zespolu przykrylby skutek zapisu. Walidacja
    odpowiedzi przed zmiana w bazie, wymiana wierszy runnera w jednej transakcji."""
    runner_id = projekt.runner_id
    _, wynik = await zapytania_runnera.zapytaj_runnera_online(
        s, tenant_id, "katalog_definicji", {}, polaczone, wyslij, runner_id=runner_id)
    ksztalt = _sprawdz_ksztalt(wynik)
    try:
        wiersze, _ = _wiersze(ksztalt, tenant_id, runner_id, datetime.now(timezone.utc))
    except _BLEDY_KONSTRUKCJI_YAML:
        # plik z zlym tagiem YAML (jakikolwiek skill runnera) nie moze zamienic zapisu, ktory
        # sie udal, w 500; `_odswiez_po_zapisie` zostawia cache do recznego odswiezenia
        raise StanNiemozliwy("katalog runnera zawiera plik z niepoprawnym YAML") from None
    await _zablokuj_runnera(s, tenant_id, runner_id)
    await s.execute(delete(Definicja).where(
        Definicja.tenant_id == tenant_id, Definicja.runner_id == runner_id))
    s.add_all(wiersze)
    await s.commit()


async def _odswiez_po_zapisie(
    s: AsyncSession, tenant_id: str, projekt: Project, polaczone: frozenset[str], wyslij: Wyslij,
) -> bool:
    """Plik jest juz zapisany albo usuniety. Gdy katalog nie da sie odswiezyc (runner odpadl,
    zly ksztalt), skutek na dysku zostaje, a cache czeka na reczne odswiezenie: blad zapisu
    byloby tu klamstwem. Transakcja konczy sie, wiec blokada projektu nie wisi.
    Zwraca, czy katalog zostal odswiezony."""
    try:
        await _odswiez_katalog_projektu(s, tenant_id, projekt, polaczone, wyslij)
    except StanNiemozliwy:
        logger.warning("katalog projektu nie odswiezony po zmianie skilla", exc_info=True)
        await s.commit()
        return False
    return True


async def _zakres_z_katalogu(s: AsyncSession, projekt: Project, skill_id: str) -> str | None:
    """Zakres z wiersza `definicja` tego projektu i id - zrodla listy i GET; None, gdy katalog
    nie ma takiego wiersza (np. nazwa skilla dluzsza niz kolumna, katalog.py go odrzuca)."""
    return (await s.execute(
        _zapytanie_skilli(projekt).where(Definicja.klucz == skill_id)
        .with_only_columns(Definicja.zakres))).scalar_one_or_none()


def _odmowa_dotyczy_pliku(komunikat: str) -> bool:
    """Czy komunikat odmowy runnera mowi o samym pliku (stale tresci z `runner.operacje`):
    katalog zamiast pliku, za duzy, nie UTF-8, NUL, brak uprawnien, nie zwykly plik, element
    sciezki nie jest katalogiem, blad dysku. Reszta (offline, limit czasu, za duza odpowiedz,
    zly ksztalt, zla konfiguracja runnera) to awaria runnera, nie stan pliku."""
    return (komunikat in _ODMOWY_DOTYCZACE_PLIKU
            or komunikat.startswith(_PREFIKS_ODMOWY_ZA_DUZY))


async def _jest_skill_globalny(
    s: AsyncSession, tenant_id: str, projekt: Project, skill_id: str,
    polaczone: frozenset[str], wyslij: Wyslij,
) -> bool:
    """Czy w domu runnera lezy POPRAWNY skill o tym id - tak jak uznaje go katalog (katalog.py:
    skill globalny to plik bez `problem`). Brak, nieczytelny (katalog, >512 KB, NUL) albo
    niepoprawny plik domowy to `nie globalny`: zapis lokalny ma przejsc, wiec zadnej odmowy."""
    wzgledna = _sciezka_skilla("global", skill_id)
    try:
        tresc = await projekty.czytaj_plik_projektu(
            s, tenant_id, projekt, wzgledna, polaczone, wyslij)
    except PlikPozaZakresem as e:
        # runner nie pozwala czytac tego pliku (np. dom poza zakresem): skilla globalnego nie ma
        # w tym projekcie, a zapis lokalny ma sie udac
        logger.warning("globalny skill '%s' poza zakresem, traktuje jak brak: %s", skill_id, e)
        return False
    except StanNiemozliwy as e:
        komunikat = str(e)
        if komunikat == KOMUNIKAT_BRAK_PLIKU:
            return False
        if _odmowa_dotyczy_pliku(komunikat):
            logger.warning(
                "globalny skill '%s' nieczytelny, traktuje jak brak: %s", skill_id, komunikat)
            return False
        raise   # offline, limit czasu, za duza odpowiedz, zly ksztalt, inna awaria: fail-closed
    try:
        return skill_z_tekstu(tresc, skill_id, scisle=True) is not None
    except (BlednySkill, *_BLEDY_KONSTRUKCJI_YAML) as e:
        logger.warning("globalny skill '%s' niepoprawny, traktuje jak brak: %s", skill_id, e)
        return False


def _zakres_w_katalogu(zakres_zapisu: str, jest_globalny: bool) -> str:
    """Zakres zapisanego skilla wynika z tego, co zapisano, a nie z cache katalogu (ten mogl sie
    nie odswiezyc): `global` to `globalny`; `project` to `nadpisany`, gdy w domu runnera jest
    tez poprawny globalny skill o tym id (`_jest_skill_globalny`), inaczej `lokalny`."""
    if zakres_zapisu == "global":
        return _ZAKRES_BEZ_KATALOGU["global"]
    return "nadpisany" if jest_globalny else _ZAKRES_BEZ_KATALOGU["project"]


async def _zapisz_skill(
    s: AsyncSession, tenant_id: str, project_id: str, skill_id: str, zakres: str,
    frontmatter: str, brief: str, polaczone: frozenset[str], wyslij: Wyslij, *, nowy: bool,
) -> dict:
    sprawdz_bezpieczna_nazwe(skill_id)
    tekst, skill = _plik_skilla(skill_id, frontmatter, brief)
    # blokada trzymana do commitu: zakres `project` szereguje wiersz projektu, zakres `global`
    # wiersz Runner (katalog domowy dzieli wiele projektow); dwa rownolegle dodania tego samego
    # id ustawiaja sie w kolejce, wiec sprawdzenie istnienia i zapis sa atomowe
    projekt = await _projekt_do_zapisu(s, tenant_id, project_id, zakres)
    wzgledna = _sciezka_skilla(zakres, skill_id)
    istnieje = await _plik_istnieje(s, tenant_id, projekt, wzgledna, polaczone, wyslij)
    if nowy is True and istnieje:
        raise StanNiemozliwy(f"skill '{skill_id}' juz istnieje w tym zakresie")
    if nowy is False and not istnieje:
        raise NieZnaleziono("skill nieznany")
    # skill globalny o tym id czyta dysk runnera przed zapisem, pod ta sama blokada projektu
    jest_globalny = zakres == "project" and await _jest_skill_globalny(
        s, tenant_id, projekt, skill_id, polaczone, wyslij)
    await projekty.zapisz_plik_projektu(s, tenant_id, projekt, wzgledna, tekst, polaczone, wyslij)
    odswiezono = await _odswiez_po_zapisie(s, tenant_id, projekt, polaczone, wyslij)
    # zakres jak w liscie i GET (wiersz katalogu); wynik z dysku tylko, gdy katalog sie nie
    # odswiezyl albo nie ma wiersza dla tego id
    zakres_katalogu = await _zakres_z_katalogu(s, projekt, skill_id) if odswiezono else None
    if zakres_katalogu is None:
        zakres_katalogu = _zakres_w_katalogu(zakres, jest_globalny)
    # _plik_skilla wyzej juz podzielil ten tekst bez bledu (skill_z_tekstu scisle=True), wiec
    # nazwa katalogu (tylko do komunikatu BlednySkill) nie ma tu skutku: mutant argumentu rownowazny
    frontmatter_pliku, brief_pliku = podziel_skill(tekst, skill_id)  # pragma: no mutate
    return {
        "id": skill.id, "name": skill.name, "role": skill.role.value,
        "consumes": list(skill.consumes), "produces": list(skill.produces),
        "zakres": zakres_katalogu, "sciezka": wzgledna,
        "frontmatter": frontmatter_pliku, "brief": brief_pliku,
    }


async def zapisz_skill(
    s: AsyncSession, tenant_id: str, project_id: str, skill_id: str, zakres: str,
    frontmatter: str, brief: str, polaczone: frozenset[str], wyslij: Wyslij,
) -> dict:
    """Nadpisuje istniejacy SKILL.md w zakresie (`project` albo `global`); brak pliku to 404."""
    return await _zapisz_skill(
        s, tenant_id, project_id, skill_id, zakres, frontmatter, brief, polaczone, wyslij,
        nowy=False)


async def dodaj_skill(
    s: AsyncSession, tenant_id: str, project_id: str, skill_id: str, zakres: str,
    frontmatter: str, brief: str, polaczone: frozenset[str], wyslij: Wyslij,
) -> dict:
    """Tworzy SKILL.md (z katalogami); zajete id w zakresie (na dysku) to StanNiemozliwy."""
    return await _zapisz_skill(
        s, tenant_id, project_id, skill_id, zakres, frontmatter, brief, polaczone, wyslij,
        nowy=True)


async def usun_skill(
    s: AsyncSession, tenant_id: str, project_id: str, skill_id: str, zakres: str,
    polaczone: frozenset[str], wyslij: Wyslij,
) -> None:
    """Usuwa sam plik SKILL.md w zakresie; plik, ktorego nie ma, to odmowa runnera (409)."""
    sprawdz_bezpieczna_nazwe(skill_id)
    projekt = await _projekt_do_zapisu(s, tenant_id, project_id, zakres)
    await projekty.usun_plik_projektu(
        s, tenant_id, projekt, _sciezka_skilla(zakres, skill_id), polaczone, wyslij)
    await _odswiez_po_zapisie(s, tenant_id, projekt, polaczone, wyslij)
