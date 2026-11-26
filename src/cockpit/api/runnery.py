"""Trasy tokenow runnera: wydanie, lista, odwolanie (HAR-119). Reguly zyja w services/tokeny.py."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from cockpit.db.session import get_session
from cockpit.services import dostep, tokeny
from cockpit.services.dostep import Kontekst, Uprawnienie

from .schematy import NowyTokenRunnera, TokenRunnera, TokenRunneraWydany, odpowiedzi_bledow
from .zaleznosci import kontekst_sesji

router = APIRouter(prefix="/api/runnery")


def _wiersz(runner) -> TokenRunnera:
    return TokenRunnera(
        runner_id=runner.id, nazwa=runner.name, token_prefiks=runner.token_prefiks,
        token_utworzony_at=tokeny.na_iso(runner.token_utworzony_at),
        token_uzyty_at=tokeny.na_iso(runner.token_uzyty_at),
        token_wazny_do=tokeny.na_iso(runner.token_wazny_do),
        odwolany_at=tokeny.na_iso(runner.odwolany_at),
    )


@router.post("", response_model=TokenRunneraWydany, responses=odpowiedzi_bledow(401, 403, 422))
async def wydaj_token(
    dane: NowyTokenRunnera,
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> TokenRunneraWydany:
    runner, token = await tokeny.wydaj_token_runnera(s, k, dane.nazwa, dane.wazny_do)
    return TokenRunneraWydany(runner_id=runner.id, token=token)


@router.get("", response_model=list[TokenRunnera], responses=odpowiedzi_bledow(401, 403))
async def lista_tokenow(
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> list[TokenRunnera]:
    dostep.wymagaj(k, Uprawnienie.podglad)
    return [_wiersz(r) for r in await tokeny.tokeny_runnerow(s, k)]


async def odwolaj_token(
    runner_id: str,
    k: Kontekst = Depends(kontekst_sesji),
    s: AsyncSession = Depends(get_session),
) -> TokenRunnera:
    return _wiersz(await tokeny.odwolaj(s, k, runner_id))


# `add_api_route` zamiast `@router.delete`: check_layers traktuje kazde `.delete(` w api/
# jako zapytanie do bazy, a to jest metoda HTTP (jak przy usuwaniu skilla w app.py)
router.add_api_route(
    "/{runner_id}", odwolaj_token, methods=["DELETE"], response_model=TokenRunnera,
    responses=odpowiedzi_bledow(401, 403, 404, 422))
