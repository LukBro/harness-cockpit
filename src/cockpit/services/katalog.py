"""Katalog skilli i workflow w bazie, odswiezany przez runnera na zadanie (HAR-67).

W bazie leza wylacznie metadane (id, nazwa, rola, sciezka). Runner oddaje sam frontmatter
SKILL.md, wiec tresc briefu nie opuszcza maszyny uzytkownika (BRO-86). Odczyt katalogu nie
pyta runnera - dziala takze offline, z data ostatniego odswiezenia.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.models import Definicja, Runner

from . import zapytania_runnera
from .errors import BlednySkill, StanNiemozliwy
from .skills import ZRODLA, metadane_skilla
from .zapytania_runnera import Wyslij

# Dlugosci kolumn `definicja`: dluzsza wartosc to zepsuta pozycja, nie blad calego odswiezenia.
_MAX_KLUCZ = 200
_MAX_SCIEZKA = 1024
_MAX_PROJEKT = 1024
_PRIORYTET = {(zrodlo, podkatalog): i for i, (zrodlo, podkatalog, _) in enumerate(ZRODLA)}
_SCISLE = {(zrodlo, podkatalog): scisle for zrodlo, podkatalog, scisle in ZRODLA}
_ROZSZERZENIE_WORKFLOW = ".yaml"


def _zly_ksztalt(powod: str) -> StanNiemozliwy:
    return StanNiemozliwy(f"odpowiedz runnera ma zly ksztalt ({powod})")


def _napis(wartosc: Any, pole: str) -> str:
    if not isinstance(wartosc, str):
        raise _zly_ksztalt(f"pole {pole} nie jest napisem")
    return wartosc


def _lista_slownikow(wartosc: Any, pole: str) -> list[dict]:
    if not isinstance(wartosc, list) or not all(isinstance(p, dict) for p in wartosc):
        raise _zly_ksztalt(f"pole {pole} nie jest lista slownikow")
    return wartosc


def _sprawdz_ksztalt(wynik: Any) -> list[dict]:
    """Walidacja calej odpowiedzi PRZED zmiana w bazie: zly ksztalt to nigdy pusty katalog."""
    if not isinstance(wynik, dict):
        raise _zly_ksztalt("wynik nie jest slownikiem")
    projekty = _lista_slownikow(wynik.get("projekty"), "projekty")
    widziane: set[str] = set()
    for p in projekty:
        nazwa = _napis(p.get("projekt"), "projekt")
        if nazwa in widziane:
            raise _zly_ksztalt("projekt wystepuje dwa razy")
        widziane.add(nazwa)
        for skill in _lista_slownikow(p.get("skille"), "skille"):
            zrodlo = _napis(skill.get("zrodlo"), "zrodlo")
            podkatalog = _napis(skill.get("podkatalog"), "podkatalog")
            if (zrodlo, podkatalog) not in _PRIORYTET:
                raise _zly_ksztalt("nieznane zrodlo skilli")
            _napis(skill.get("katalog"), "katalog")
            _napis(skill.get("sciezka"), "sciezka")
            if "frontmatter" not in skill or (
                    skill["frontmatter"] is not None and not isinstance(skill["frontmatter"], str)):
                raise _zly_ksztalt("pole frontmatter nie jest napisem ani null")
        for wf in _lista_slownikow(p.get("workflow"), "workflow"):
            _napis(wf.get("sciezka"), "sciezka")
    return projekty


def _skille_projektu(skille: list[dict]) -> tuple[dict[str, tuple[str, str, str]], int]:
    """({id: (nazwa, rola, sciezka)}, liczba pominietych). Wygrywa zrodlo o wyzszym priorytecie."""
    wybrane: dict[str, tuple[str, str, str]] = {}
    pominiete = 0
    for skill in sorted(skille, key=lambda s: _PRIORYTET[(s["zrodlo"], s["podkatalog"])]):
        klucz_zrodla = (skill["zrodlo"], skill["podkatalog"])
        if skill["frontmatter"] is None:
            pominiete += 1
            continue
        try:
            meta = metadane_skilla(
                skill["frontmatter"], skill["katalog"], scisle=_SCISLE[klucz_zrodla])
        except BlednySkill:
            pominiete += 1
            continue
        if meta is None:   # zrodlo niescisle, brak `role`: nie skill kokpitu
            pominiete += 1
            continue
        id_skilla, nazwa, rola = meta
        sciezka = skill["sciezka"] if skill["zrodlo"] == "repo" else f"~/{skill['sciezka']}"
        if (len(id_skilla) > _MAX_KLUCZ or len(nazwa) > _MAX_KLUCZ
                or len(sciezka) > _MAX_SCIEZKA):
            pominiete += 1
            continue
        wybrane.setdefault(id_skilla, (nazwa, rola, sciezka))
    return wybrane, pominiete


def _workflow_projektu(workflow: list[dict]) -> tuple[dict[str, str], int]:
    """({nazwa: sciezka}, liczba pominietych)."""
    wybrane: dict[str, str] = {}
    pominiete = 0
    for wf in workflow:
        sciezka = wf["sciezka"]
        plik = PurePosixPath(sciezka).name
        nazwa = plik.removesuffix(_ROZSZERZENIE_WORKFLOW)
        if (not plik.endswith(_ROZSZERZENIE_WORKFLOW) or not nazwa
                or len(nazwa) > _MAX_KLUCZ or len(sciezka) > _MAX_SCIEZKA):
            pominiete += 1
            continue
        wybrane.setdefault(nazwa, sciezka)
    return wybrane, pominiete


def _wiersze(
    projekty: list[dict], tenant_id: str, runner_id: str, teraz: datetime,
) -> tuple[list[Definicja], int]:
    wiersze: list[Definicja] = []
    pominiete = 0
    for p in projekty:
        if len(p["projekt"]) > _MAX_PROJEKT:
            pominiete += len(p["skille"]) + len(p["workflow"])
            continue
        skille, pominiete_skille = _skille_projektu(p["skille"])
        workflow, pominiete_wf = _workflow_projektu(p["workflow"])
        pominiete += pominiete_skille + pominiete_wf
        wspolne = {"tenant_id": tenant_id, "runner_id": runner_id, "projekt": p["projekt"],
                   "odswiezono_at": teraz}
        wiersze += [
            Definicja(**wspolne, rodzaj="skill", klucz=id_skilla, nazwa=nazwa, rola=rola,
                      sciezka=sciezka)
            for id_skilla, (nazwa, rola, sciezka) in skille.items()]
        wiersze += [
            Definicja(**wspolne, rodzaj="workflow", klucz=nazwa, nazwa=nazwa, rola=None,
                      sciezka=sciezka)
            for nazwa, sciezka in workflow.items()]
    return wiersze, pominiete


def _iso(moment: datetime) -> str:
    """ISO 8601 z +00:00; SQLite oddaje daty bez strefy - to zawsze UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc).isoformat()
    return moment.astimezone(timezone.utc).isoformat()


async def katalog(s: AsyncSession, tenant_id: str) -> dict:
    """Ostatni zapisany katalog zespolu; tylko baza, bez pytania runnera."""
    wiersze = (await s.execute(
        select(Definicja).where(Definicja.tenant_id == tenant_id)
    )).scalars().all()
    wiersze = sorted(wiersze, key=lambda w: (w.projekt, w.rodzaj, w.klucz, w.runner_id))
    # runnery odswiezane osobno: najstarsza data mowi, jak stary jest najstarszy fragment
    najstarszy = min((_iso(w.odswiezono_at) for w in wiersze), default=None,
                     key=lambda d: datetime.fromisoformat(d))
    return {
        "odswiezono_at": najstarszy,
        "definicje": [{
            "runner_id": w.runner_id, "projekt": w.projekt, "rodzaj": w.rodzaj, "klucz": w.klucz,
            "nazwa": w.nazwa, "rola": w.rola, "sciezka": w.sciezka,
            "odswiezono_at": _iso(w.odswiezono_at)} for w in wiersze],
    }


async def odswiez_katalog(
    s: AsyncSession, tenant_id: str, polaczone: frozenset[str], wyslij: Wyslij,
) -> dict:
    """Pyta runnera online, a dopiero potem w jednej transakcji wymienia wiersze jego katalogu.

    Brak runnera (RunnerOffline), blad runnera i zly ksztalt odpowiedzi (StanNiemozliwy) nie
    zmieniaja bazy: stary katalog zostaje, nigdy pusty ani polowiczny."""
    runner_id, wynik = await zapytania_runnera.zapytaj_runnera_online(
        s, tenant_id, "katalog_definicji", {}, polaczone, wyslij)
    projekty = _sprawdz_ksztalt(wynik)
    wiersze, pominiete = _wiersze(projekty, tenant_id, runner_id, datetime.now(timezone.utc))
    blokada = (await s.execute(
        select(Runner).where(Runner.id == runner_id, Runner.tenant_id == tenant_id)
        .with_for_update()
    )).scalar_one_or_none()
    if blokada is None:
        raise StanNiemozliwy("runner zniknal w trakcie odswiezania katalogu")
    await s.execute(delete(Definicja).where(
        Definicja.tenant_id == tenant_id, Definicja.runner_id == runner_id))
    s.add_all(wiersze)
    await s.commit()
    return {**await katalog(s, tenant_id), "pominiete": pominiete}
