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
from cockpit.services import reset_hasla, sesje
from cockpit.services.dostep import Kontekst
from cockpit.settings import settings

from .schematy import (
    KonfiguracjaKonta, Profil, ResetPrzyjety, TokenCsrf, odpowiedzi_bledow,
)
from .zaleznosci import CIASTECZKO_SESJI, identyfikator_sesji, kontekst_sesji, trasa_publiczna

router = APIRouter(prefix="/api/konto")

BRAK_ADRESU_KLIENTA = "nieznany"
KOMUNIKAT_RESETU = "jeśli adres ma konto, wysłaliśmy na niego link do zmiany hasła"
MAX_HASLO = 256   # argon2 hashuje wszystko; limit chroni przed ogromnym cialem zadania
MAX_TOKEN = 200   # token resetu ma 43 znaki; limit chroni przed ogromnym cialem zadania


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


class ProsbaOReset(BaseModel):
    email: str = Field(max_length=320)


class PotwierdzenieResetu(BaseModel):
    token: str = Field(max_length=MAX_TOKEN)
    nowe_haslo: SecretStr = Field(max_length=MAX_HASLO)


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


@router.post("/reset", status_code=202, dependencies=[Depends(trasa_publiczna)],
             response_model=ResetPrzyjety, responses=odpowiedzi_bledow(409, 422))
async def prosba_o_reset(
    dane: ProsbaOReset, s: AsyncSession = Depends(get_session)
) -> ResetPrzyjety:
    await reset_hasla.popros(s, dane.email)
    return ResetPrzyjety(komunikat=KOMUNIKAT_RESETU)


@router.post("/reset/potwierdz", status_code=204, dependencies=[Depends(trasa_publiczna)],
             response_model=None, responses=odpowiedzi_bledow(401, 409, 422))
async def potwierdzenie_resetu(
    dane: PotwierdzenieResetu, s: AsyncSession = Depends(get_session)
) -> Response:
    await reset_hasla.uzyj(s, dane.token, dane.nowe_haslo.get_secret_value())
    return Response(status_code=204)


@router.get("/konfiguracja", dependencies=[Depends(trasa_publiczna)],
            response_model=KonfiguracjaKonta, responses=odpowiedzi_bledow())
async def konfiguracja() -> KonfiguracjaKonta:
    return KonfiguracjaKonta(reset_hasla=reset_hasla.wlaczony())


@router.get("/ja", response_model=Profil, responses=odpowiedzi_bledow(401))
async def ja(
    kontekst: Kontekst = Depends(kontekst_sesji),
    identyfikator: str = Depends(identyfikator_sesji),
    s: AsyncSession = Depends(get_session),
) -> Profil:
    profil = await uslugi.profil(s, kontekst)
    csrf = await sesje.odnow_csrf(s, identyfikator)
    return Profil(email=profil.email, zespol=profil.zespol, rola=profil.rola, csrf=csrf)


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
