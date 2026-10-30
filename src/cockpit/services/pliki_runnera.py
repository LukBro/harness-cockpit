"""Pliki uzytkownika wylacznie przez runnera (HAR-55): kokpit nie widzi dysku, wiec kazda
operacja to zapytanie `zapytaj_runnera_zespolu`. Zakres i sekrety egzekwuje runner (D14)."""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .zapytania_runnera import Wyslij, zapytaj_runnera_zespolu


async def czytaj(
    s: AsyncSession, tenant_id: str, sciezka: str, polaczone: frozenset[str], wyslij: Wyslij,
) -> Any:
    return await zapytaj_runnera_zespolu(
        s, tenant_id, "read_file", {"sciezka": sciezka}, polaczone, wyslij)


async def listuj(
    s: AsyncSession, tenant_id: str, sciezka: str, wzorzec: str,
    polaczone: frozenset[str], wyslij: Wyslij,
) -> Any:
    return await zapytaj_runnera_zespolu(
        s, tenant_id, "list_files", {"sciezka": sciezka, "wzorzec": wzorzec}, polaczone, wyslij)


async def zapisz(
    s: AsyncSession, tenant_id: str, sciezka: str, tresc: str,
    polaczone: frozenset[str], wyslij: Wyslij,
) -> Any:
    return await zapytaj_runnera_zespolu(
        s, tenant_id, "write_file", {"sciezka": sciezka, "tresc": tresc}, polaczone, wyslij)
