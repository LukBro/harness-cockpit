"""Dostep: macierz czterech rol i ochrona ostatniego wlasciciela (HAR-38, BRO-118).

Jedno miejsce rozstrzyga, czy czlonek zespolu moze wykonac operacje. Brak
czlonkostwa to `NieZnaleziono` o tej samej tresci dla zespolu istniejacego
i nieistniejacego - nie zdradzamy, czy zasob jest.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.models import Membership, RolaWZespole
from cockpit.services.errors import NieZnaleziono, StanNiemozliwy, Zabronione


class Uprawnienie(StrEnum):
    podglad = "podglad"
    uruchamianie = "uruchamianie"
    zatwierdzanie = "zatwierdzanie"
    edycja_definicji = "edycja_definicji"
    podlaczanie_projektow = "podlaczanie_projektow"
    zaproszenia_i_role = "zaproszenia_i_role"
    usuniecie_zespolu = "usuniecie_zespolu"
    przeniesienie_wlasnosci = "przeniesienie_wlasnosci"


_U = Uprawnienie
_MEMBER = frozenset({_U.podglad, _U.uruchamianie, _U.zatwierdzanie, _U.edycja_definicji, _U.podlaczanie_projektow})

MACIERZ_ROL = MappingProxyType({
    RolaWZespole.owner: frozenset(Uprawnienie),
    RolaWZespole.admin: _MEMBER | {_U.zaproszenia_i_role},
    RolaWZespole.member: _MEMBER,
    RolaWZespole.viewer: frozenset({_U.podglad}),
})

OSTATNI_WLASCICIEL = "nie można usunąć ani zdegradować ostatniego właściciela zespołu"

_CZYNNOSCI = {
    _U.podglad: "przeglądać runów i definicji",
    _U.uruchamianie: "uruchamiać runów",
    _U.zatwierdzanie: "zatwierdzać bramek",
    _U.edycja_definicji: "edytować skilli i workflow",
    _U.podlaczanie_projektow: "podłączać projektów",
    _U.zaproszenia_i_role: "zapraszać członków ani zmieniać ról",
    _U.usuniecie_zespolu: "usunąć zespołu",
    _U.przeniesienie_wlasnosci: "przenosić własności zespołu",
}


@dataclass(frozen=True)
class Kontekst:
    user_id: str
    tenant_id: str
    rola: RolaWZespole


async def czlonkostwo(s: AsyncSession, user_id: str, tenant_id: str) -> Membership:
    m = (
        await s.execute(
            select(Membership).where(Membership.user_id == user_id, Membership.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if m is None:
        raise NieZnaleziono("zespół nieznany")
    return m


def wymagaj(kontekst: Kontekst, uprawnienie: Uprawnienie) -> None:
    if uprawnienie not in MACIERZ_ROL[kontekst.rola]:
        raise Zabronione(f"rola {kontekst.rola.value} nie może {_CZYNNOSCI[uprawnienie]}")


async def _cel(s: AsyncSession, kontekst: Kontekst, user_id: str) -> Membership:
    m = (
        await s.execute(
            select(Membership).where(
                Membership.user_id == user_id, Membership.tenant_id == kontekst.tenant_id
            )
        )
    ).scalar_one_or_none()
    if m is None:
        raise NieZnaleziono("członek nieznany")
    return m


async def _sprawdz_wlasnosc(s: AsyncSession, kontekst: Kontekst, cel: Membership, nowa_rola: RolaWZespole | None) -> None:
    # Wszystkie odmowy przed zapisem: po rollback obiekty sesji async wygasaja.
    if cel.rola == RolaWZespole.owner or nowa_rola == RolaWZespole.owner:
        wymagaj(kontekst, Uprawnienie.przeniesienie_wlasnosci)
    traci_wlasciciela = cel.rola == RolaWZespole.owner and nowa_rola != RolaWZespole.owner
    if traci_wlasciciela:
        wlasciciele = (
            await s.execute(
                select(func.count())
                .select_from(Membership)
                .where(Membership.tenant_id == kontekst.tenant_id, Membership.rola == RolaWZespole.owner)
            )
        ).scalar_one()
        if wlasciciele == 1:
            raise StanNiemozliwy(OSTATNI_WLASCICIEL)


async def zmien_role(s: AsyncSession, kontekst: Kontekst, user_id: str, nowa_rola: RolaWZespole) -> Membership:
    wymagaj(kontekst, Uprawnienie.zaproszenia_i_role)
    cel = await _cel(s, kontekst, user_id)
    await _sprawdz_wlasnosc(s, kontekst, cel, nowa_rola)
    cel.rola = nowa_rola
    await s.commit()
    return cel


async def usun_czlonka(s: AsyncSession, kontekst: Kontekst, user_id: str) -> None:
    wymagaj(kontekst, Uprawnienie.zaproszenia_i_role)
    cel = await _cel(s, kontekst, user_id)
    await _sprawdz_wlasnosc(s, kontekst, cel, None)
    await s.delete(cel)
    await s.commit()
