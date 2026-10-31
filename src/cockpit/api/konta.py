"""Trasy kont: rejestracja, logowanie, wylogowanie, profil, zmiana hasla (HAR-48).

Ciasteczko sesji: httpOnly, SameSite=Lax, Secure poza lokalnym developmentem
(`COCKPIT_CIASTECZKO_SECURE`), trwale. Reguly zyja w services/konta.py i sesje.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.session import get_session
from cockpit.services import konta as uslugi
from cockpit.services import sesje
from cockpit.services.dostep import Kontekst
from cockpit.settings import settings

from .schematy import Profil, TokenCsrf, odpowiedzi_bledow
from .zaleznosci import CIASTECZKO_SESJI, identyfikator_sesji, kontekst_sesji, trasa_publiczna

router = APIRouter(prefix="/api/konto")

BRAK_ADRESU_KLIENTA = "nieznany"
MAX_HASLO = 256   # argon2 hashuje wszystko; limit chroni przed ogromnym cialem zadania


class DaneRejestracji(BaseModel):
    email: str = Field(max_length=320)
    haslo: SecretStr = Field(max_length=MAX_HASLO)
    nazwa_zespolu: str | None = Field(default=None, max_length=200)   # Tenant.name: String(200)


class DaneLogowania(BaseModel):
    email: str = Field(max_length=320)
    haslo: SecretStr = Field(max_length=MAX_HASLO)


class ZmianaHasla(BaseModel):
    stare: SecretStr = Field(max_length=MAX_HASLO)
    nowe: SecretStr = Field(max_length=MAX_HASLO)


def _ustaw_ciasteczko(odp: Response, identyfikator: str) -> None:
    odp.set_cookie(
        CIASTECZKO_SESJI,
        identyfikator,
        max_age=settings.sesja_wazna_dni * 86400,
        path="/",
        secure=settings.ciasteczko_secure,
        httponly=True,
        samesite="lax",
    )


def _skasuj_ciasteczko(odp: Response) -> None:
    # te same path/secure/httponly/samesite co przy ustawieniu, inaczej przegladarka nie skasuje
    odp.delete_cookie(
        CIASTECZKO_SESJI,
        path="/",
        secure=settings.ciasteczko_secure,
        httponly=True,
        samesite="lax",
    )


@router.post("/rejestracja", status_code=201, dependencies=[Depends(trasa_publiczna)],
             response_model=TokenCsrf, responses=odpowiedzi_bledow(409, 422))
async def rejestracja(
    dane: DaneRejestracji, odp: Response, s: AsyncSession = Depends(get_session)
) -> TokenCsrf:
    user = await uslugi.zarejestruj(
        s, dane.email, dane.haslo.get_secret_value(), dane.nazwa_zespolu
    )
    identyfikator, csrf = await sesje.utworz(s, user)
    _ustaw_ciasteczko(odp, identyfikator)
    return TokenCsrf(csrf=csrf)


@router.post("/logowanie", dependencies=[Depends(trasa_publiczna)], response_model=TokenCsrf,
             responses=odpowiedzi_bledow(401, 422, 429))
async def logowanie(
    dane: DaneLogowania,
    request: Request,
    odp: Response,
    s: AsyncSession = Depends(get_session),
) -> TokenCsrf:
    ip = request.client.host if request.client else BRAK_ADRESU_KLIENTA
    user = await uslugi.zaloguj(s, dane.email, dane.haslo.get_secret_value(), ip)
    identyfikator, csrf = await sesje.utworz(s, user)
    _ustaw_ciasteczko(odp, identyfikator)
    return TokenCsrf(csrf=csrf)


@router.get("/ja", response_model=Profil, responses=odpowiedzi_bledow(401))
async def ja(
    kontekst: Kontekst = Depends(kontekst_sesji),
    identyfikator: str = Depends(identyfikator_sesji),
    s: AsyncSession = Depends(get_session),
) -> Profil:
    profil = await uslugi.profil(s, kontekst)
    csrf = await sesje.odnow_csrf(s, identyfikator)
    return Profil(email=profil.email, zespol=profil.zespol, rola=profil.rola.value, csrf=csrf)


@router.post("/wyloguj", status_code=204, response_model=None,
             responses=odpowiedzi_bledow(401, 403))
async def wyloguj(
    _: Kontekst = Depends(kontekst_sesji),
    identyfikator: str = Depends(identyfikator_sesji),
    s: AsyncSession = Depends(get_session),
) -> Response:
    await sesje.wyloguj(s, identyfikator)
    odp = Response(status_code=204)
    _skasuj_ciasteczko(odp)
    return odp


@router.post("/wyloguj-wszedzie", status_code=204, response_model=None,
             responses=odpowiedzi_bledow(401, 403))
async def wyloguj_wszedzie(
    kontekst: Kontekst = Depends(kontekst_sesji), s: AsyncSession = Depends(get_session)
) -> Response:
    await sesje.wyloguj_wszedzie(s, kontekst.user_id)
    odp = Response(status_code=204)
    _skasuj_ciasteczko(odp)
    return odp


@router.post("/haslo", status_code=204, response_model=None,
             responses=odpowiedzi_bledow(401, 403, 409, 422))
async def zmiana_hasla(
    dane: ZmianaHasla,
    kontekst: Kontekst = Depends(kontekst_sesji),
    identyfikator: str = Depends(identyfikator_sesji),
    s: AsyncSession = Depends(get_session),
) -> Response:
    await uslugi.zmien_haslo_uzytkownika(
        s,
        kontekst.user_id,
        dane.stare.get_secret_value(),
        dane.nowe.get_secret_value(),
        identyfikator,
    )
    return Response(status_code=204)
