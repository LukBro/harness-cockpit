"""Walidator grafu krokow, artefaktow i petli naprawczej workflow (HAR-8).

Zbiera wszystkie problemy naraz i nie przerywa na pierwszym; pusta lista oznacza
poprawny workflow. Graf powstaje wylacznie z `Krok.needs` (`Skill.needs` nie jest
zaleznoscia krokow), a `on_fail.retry` nie bierze udzialu w wykrywaniu cyklu.
Modul nie dotyka bazy.
"""
from __future__ import annotations

import re
from pathlib import Path

from cockpit.services.kroki_systemowe import KROKI_SYSTEMOWE, SYSTEM_CHECK, opis_kroku
from cockpit.services.skills import Rejestr, Skill
from cockpit.services.walidacja import KLUCZ_ROLI, KLUCZE_DEFINICJI, waliduj_definicje
from cockpit.services.workflow import Krok, Poziom, Problem, Workflow

# Kody problemow. Kanwa i testy dopasowuja sie po nich, nie po tresci komunikatow.
NIEZNANA_ZALEZNOSC = "nieznana_zaleznosc"
CYKL = "cykl"
NIEZNANY_SKILL = "nieznany_skill"
BRAK_ARTEFAKTU = "brak_artefaktu"
CONSENSUS_PONAD_FANOUT = "consensus_ponad_fanout"
FOREACH_BEZ_PRODUCENTA = "foreach_bez_producenta"
FOREACH_ZLY_FORMAT = "foreach_zly_format"
RETRY_NIE_WSTECZ = "retry_nie_wstecz"
NIEPRZENOSNY_SKILL = "nieprzenosny_skill"
NIEZNANY_KLUCZ = "nieznany_klucz"
POLE_BEZ_KONSUMENTA = "pole_bez_konsumenta"
ROLA_W_KROKU_IGNOROWANA = "rola_w_kroku_ignorowana"
SKILL_I_SYSTEM = "skill_i_system"
NIEZNANY_KROK_SYSTEMOWY = "nieznany_krok_systemowy"
CHECK_BEZ_CAPABILITY = "check_bez_capability"
ZLA_NAZWA_CAPABILITY = "zla_nazwa_capability"
CAPABILITY_BEZ_CHECK = "capability_bez_check"

# Nazwa capability trafia do nazwy pliku dowodu (`dowod-<capability>.json`).
WZORZEC_CAPABILITY = r"[A-Za-z0-9_-]{1,64}"

_POLA_BEZ_KONSUMENTA = ("optional", "require_distinct_vendors")

_POLA_KROKU = frozenset(Krok.model_fields) - {"id"}


def _przodkowie(start: str, needs: dict[str, list[str]]) -> set[str]:
    """Domkniecie przechodnie `needs` od `start` (bez `start`, chyba ze jest w cyklu).

    Zbior odwiedzonych gwarantuje zakonczenie takze na grafie z cyklem.
    """
    odwiedzeni: set[str] = set()
    stos = [n for n in needs.get(start, []) if n in needs]
    while stos:
        id_kroku = stos.pop()
        if id_kroku in odwiedzeni:
            continue
        odwiedzeni.add(id_kroku)
        stos.extend(n for n in needs[id_kroku] if n in needs)
    return odwiedzeni


def _cykl_przez(start: str, needs: dict[str, list[str]], przodkowie: set[str]) -> list[str] | None:
    """Sciezka `start -> ... -> start` po krawedziach needs albo None, gdy krok nie jest w cyklu.

    DFS ograniczony do przodkow `start` (cykl przez `start` lezy w calosci wsrod nich);
    kolejnosc deterministyczna (kolejnosc `needs` w pliku).
    """
    if start not in przodkowie:
        return None
    odwiedzeni: set[str] = set()
    sciezka = [start]

    def szukaj(id_kroku: str) -> bool:
        for sasiad in needs[id_kroku]:
            if sasiad == start:
                sciezka.append(start)
                return True
            if sasiad in odwiedzeni or sasiad not in przodkowie:
                continue
            odwiedzeni.add(sasiad)
            sciezka.append(sasiad)
            if szukaj(sasiad):
                return True
            sciezka.pop()
        return False

    return sciezka if szukaj(start) else None


def _produkowane(przodkowie: set[str], skille_kroku: dict[str, list[str]]) -> set[str]:
    """Artefakty produkowane przez skille przodkow (nieznany skill nic nie produkuje)."""
    wynik: set[str] = set()
    for id_kroku in przodkowie:
        wynik.update(skille_kroku.get(id_kroku, []))
    return wynik


def _nieznany_skill(krok: Krok, rejestr: Rejestr) -> Problem:
    for pominiety in rejestr.pominiete:
        if pominiety.plik.parent.name == krok.skill:
            return Problem(
                krok.id,
                NIEZNANY_SKILL,
                f"skill pominiety przy wczytaniu: {krok.skill} ({pominiety.powod})",
            )
    return Problem(krok.id, NIEZNANY_SKILL, f"nieznany skill: {krok.skill}")


def _niezmienniki_kroku(krok: Krok, skill: Skill | None, workflow: Workflow) -> list[Problem]:
    """Naruszenia niezmiennikow (HAR-6) i nieznane klucze surowej mapy kroku.

    Rola pochodzi z rejestru i nadpisuje `role` wpisane w kroku. Krok z nieznanym
    skillem wolamy BEZ `role` (takze tej z pliku): bez roli `reviewer_readonly` i zgodnosc
    tieru nie odpalaja, wiec nie ma falszywego naruszenia, a klucze zabronione dla kazdej
    roli (`write`, `model`) nadal sa zglaszane. Do walidatora trafiaja tylko klucze-napisy.
    """
    surowy = workflow.surowy_krok(krok.id)
    definicja = {k: v for k, v in surowy.items() if isinstance(k, str)}
    definicja.pop(KLUCZ_ROLI, None)
    if skill is not None:
        definicja[KLUCZ_ROLI] = skill.role.value
    problemy = [
        Problem(krok.id, n.niezmiennik.value, str(n)) for n in waliduj_definicje(definicja)
    ]
    for klucz in surowy:
        if klucz not in _POLA_KROKU and klucz not in KLUCZE_DEFINICJI:
            problemy.append(Problem(krok.id, NIEZNANY_KLUCZ, f"nieznany klucz kroku: {klucz!r}"))
    return problemy


def _ostrzezenia_kroku(krok: Krok, skill: Skill | None, workflow: Workflow) -> list[Problem]:
    """Ostrzezenia o polach kroku bez skutku (HAR-16); nie zmieniaja `ma_bledy()`.

    Pole bez konsumenta: wlaczone `optional` / `require_distinct_vendors` (po jednym
    ostrzezeniu na pole). `role` w surowym kroku jest zawsze ignorowana - rola pochodzi
    z rejestru - wiec ostrzegamy za kazdym razem, gdy klucz jest w pliku.
    """
    problemy = [
        Problem(
            krok.id,
            POLE_BEZ_KONSUMENTA,
            f"pole {pole} nie ma jeszcze skutku: silnik go nie odczytuje",
            Poziom.ostrzezenie,
        )
        for pole in _POLA_BEZ_KONSUMENTA
        if getattr(krok, pole)
    ]
    if KLUCZ_ROLI in workflow.surowy_krok(krok.id):
        komunikat = (
            f"role w kroku jest ignorowane: krok pojdzie z rola {skill.role.value}"
            if skill is not None
            else "role w kroku jest ignorowane: rola zostanie ustalona z rejestru"
        )
        problemy.append(Problem(krok.id, ROLA_W_KROKU_IGNOROWANA, komunikat, Poziom.ostrzezenie))
    return problemy


def _problemy_capability(krok: Krok) -> list[Problem]:
    """Pole `capability` kroku: wymagane przy `system: check`, zabronione przy kazdym innym."""
    if krok.system == SYSTEM_CHECK and krok.skill is None:
        if not krok.capability:
            return [Problem(
                krok.id, CHECK_BEZ_CAPABILITY, "krok system: check wymaga pola capability")]
        if not re.fullmatch(WZORZEC_CAPABILITY, krok.capability):
            return [Problem(
                krok.id, ZLA_NAZWA_CAPABILITY,
                f"nazwa capability {krok.capability!r} spoza wzorca {WZORZEC_CAPABILITY}")]
        return []
    if krok.capability is not None:
        return [Problem(
            krok.id, CAPABILITY_BEZ_CHECK, "pole capability dotyczy tylko kroku system: check")]
    return []


def _w_katalogu(sciezka: Path, katalog: Path) -> bool:
    try:
        sciezka.resolve().relative_to(katalog.resolve())
    except ValueError:
        return False
    return True


def waliduj_workflow(workflow: Workflow, rejestr: Rejestr, dom: Path) -> list[Problem]:
    """Zwraca wszystkie problemy workflow; kolejnosc: kroki z pliku, potem kolejnosc sprawdzen.

    Po sprawdzeniu skilla krok przechodzi przez niezmienniki i nieznane klucze
    (`_niezmienniki_kroku`).

    Kroki z `workflow.niepoprawne` traktujemy jako istniejace (mozna na nie wskazac
    w `needs`), ale ich pol nie sprawdzamy i nic nie produkuja.

    Ograniczenie: czesci `<pole>` z `foreach: <artefakt>#<pole>` nie da sie sprawdzic
    statycznie, bo artefakty nie maja schematu. Sprawdzamy tylko, ze `<artefakt>`
    produkuje ktorys przodek kroku.
    """
    problemy: list[Problem] = []
    istniejace = {k.id for k in workflow.kroki} | set(workflow.niepoprawne)
    needs: dict[str, list[str]] = {id_kroku: [] for id_kroku in istniejace}
    for krok in workflow.kroki:
        needs[krok.id] = [n for n in krok.needs if n in istniejace]
    kroki_po_id = {k.id: k for k in workflow.kroki}
    produkcja = {
        id_kroku: list(rejestr.skille[krok.skill].produces)
        for id_kroku, krok in kroki_po_id.items()
        if krok.skill in rejestr.skille
    }
    # krok systemowy jest producentem artefaktow ze swojego opisu; nieznany nic nie produkuje
    produkcja.update({
        id_kroku: list(opis_kroku(krok).produces)
        for id_kroku, krok in kroki_po_id.items()
        if krok.skill is None and krok.system is not None
    })
    skille_repo = dom / ".cockpit" / "skills"

    for krok in workflow.kroki:
        for cel in dict.fromkeys(krok.needs):
            if cel not in istniejace:
                problemy.append(
                    Problem(krok.id, NIEZNANA_ZALEZNOSC, f"krok zalezy od nieznanego kroku: {cel}")
                )

        przodkowie = _przodkowie(krok.id, needs)
        cykl = _cykl_przez(krok.id, needs, przodkowie)
        if cykl is not None:
            problemy.append(
                Problem(krok.id, CYKL, "cykl w needs: " + " → ".join(cykl))
            )

        if krok.skill is not None and krok.system is not None:
            problemy.append(
                Problem(krok.id, SKILL_I_SYSTEM, "krok ma naraz pole skill i system")
            )
        skill = rejestr.skille.get(krok.skill)
        if krok.skill is None and krok.system is not None:
            # krok systemowy: bez skilla, wiec bez `nieznany_skill` i bez roli
            if krok.system not in KROKI_SYSTEMOWE:
                problemy.append(
                    Problem(
                        krok.id,
                        NIEZNANY_KROK_SYSTEMOWY,
                        f"nieznany krok systemowy: {krok.system}",
                    )
                )
            konsumowane = opis_kroku(krok).consumes
        elif skill is None:
            problemy.append(_nieznany_skill(krok, rejestr))
            konsumowane = ()
        else:
            konsumowane = tuple(skill.consumes)
        problemy.extend(_problemy_capability(krok))
        problemy.extend(_niezmienniki_kroku(krok, skill, workflow))
        dostepne = _produkowane(przodkowie, produkcja)
        for artefakt in konsumowane:
            if artefakt not in dostepne:
                problemy.append(
                    Problem(
                        krok.id,
                        BRAK_ARTEFAKTU,
                        f"artefakt {artefakt} nie jest produkowany przez zadnego przodka kroku",
                    )
                )

        if krok.consensus is not None and krok.consensus > krok.fanout:
            problemy.append(
                Problem(
                    krok.id,
                    CONSENSUS_PONAD_FANOUT,
                    f"consensus {krok.consensus} jest wiekszy niz fanout {krok.fanout}",
                )
            )

        if krok.foreach is not None:
            artefakt, separator, pole = krok.foreach.partition("#")
            if not separator or not artefakt or not pole:
                problemy.append(
                    Problem(
                        krok.id,
                        FOREACH_ZLY_FORMAT,
                        f"foreach musi miec postac <artefakt>#<pole>, jest: {krok.foreach}",
                    )
                )
            elif artefakt not in _produkowane(przodkowie, produkcja):
                problemy.append(
                    Problem(
                        krok.id,
                        FOREACH_BEZ_PRODUCENTA,
                        f"foreach: artefakt {artefakt} nie jest produkowany przez zadnego przodka kroku",
                    )
                )

        if krok.on_fail is not None:
            cel = krok.on_fail.retry
            if cel == krok.id or cel not in przodkowie:
                problemy.append(
                    Problem(
                        krok.id,
                        RETRY_NIE_WSTECZ,
                        f"retry z kroku {krok.id} do {cel}: cel musi byc przodkiem w needs",
                    )
                )

        if skill is not None and _w_katalogu(skill.katalog, skille_repo):
            problemy.append(
                Problem(
                    krok.id,
                    NIEPRZENOSNY_SKILL,
                    f"nieprzenośny — skill spoza repozytorium: {skill.id}",
                    Poziom.ostrzezenie,
                )
            )

        problemy.extend(_ostrzezenia_kroku(krok, skill, workflow))

    return problemy
