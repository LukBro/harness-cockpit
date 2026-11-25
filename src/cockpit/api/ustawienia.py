"""Trasy ustawien kokpitu: poczta SMTP (HAR-91). Reguly zyja w services/poczta.py."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.session import get_session
from cockpit.services import dostep, poczta
from cockpit.services import konta as uslugi_kont
from cockpit.services.dostep import Kontekst, Uprawnienie

from .schematy import PocztaTestowaWyslana, UstawieniaPoczty, odpowiedzi_bledow
from .zaleznosci import kontekst_sesji

router = APIRouter(prefix="/api/ustawienia")

TEMAT_TESTOWY = "Harness Cockpit: wiadomość testowa"
TRESC_TESTOWA = "Poczta kokpitu działa: ta wiadomość została wysłana z ustawień."


@router.get("/poczta", response_model=UstawieniaPoczty, responses=odpowiedzi_bledow(401))
async def stan_poczty(_: Kontekst = Depends(kontekst_sesji)) -> UstawieniaPoczty:
    stan = poczta.stan()
    return UstawieniaPoczty(skonfigurowana=stan.skonfigurowana, powod=stan.powod)


@router.post("/poczta/test", response_model=PocztaTestowaWyslana,
             responses=odpowiedzi_bledow(401, 403, 502))
async def wyslij_poczte_testowa(
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> PocztaTestowaWyslana:
    dostep.wymagaj(k, Uprawnienie.zaproszenia_i_role)   # przed wysylka: czlonek nie uruchamia SMTP
    profil = await uslugi_kont.profil(s, k)   # adres zalogowanego, nie "pierwszego w bazie"
    await poczta.wyslij(profil.email, TEMAT_TESTOWY, TRESC_TESTOWA)
    return PocztaTestowaWyslana(do=profil.email)
