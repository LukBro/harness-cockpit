"""Blokada logowania: licznik prob w bazie, ta sama dla kazdego adresu (HAR-46).

SQLite w tmp_path. Zegar podmieniamy przez `sesje.teraz`, ustawienia przez obiekt
`cockpit.settings.settings`. Kazda asercja o skutku czyta dane w NOWEJ sesji `maker()`.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cockpit.api.bledy import KODY_BLEDOW, zarejestruj_obsluge_bledow
from cockpit.db.models import Base, BlokadaLogowania, User
from cockpit.services import konta, sesje
from cockpit.services.errors import BladDziedziny, BrakDostepu, ZbytWieleProb
from cockpit.settings import Settings, settings

KOMUNIKAT_LOGOWANIA = "nieprawidłowy adres lub hasło"
HASLO = "Sekret-Hasl0-Alfa"
ZLE_HASLO = "Zle-Haslo-Gamma"
JAN = "jan@przyklad.pl"
EWA = "ewa@przyklad.pl"
NIKT = "nikt@przyklad.pl"
IP1 = "203.0.113.7"
IP2 = "198.51.100.23"
IP3 = "192.0.2.99"
IP4 = "192.0.2.100"
T0 = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
async def maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'blokada_logowania.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class Zegar:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def zegar(monkeypatch):
    z = Zegar()
    monkeypatch.setattr(sesje, "teraz", z)
    return z


def _sha(napis: str) -> str:
    return hashlib.sha256(napis.encode("utf-8")).hexdigest()


def _utc(dt: datetime) -> datetime:
    # SQLite oddaje naive datetime z kolumn DateTime(timezone=True)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


async def _rejestruj(maker, email=JAN, haslo=HASLO) -> User:
    async with maker() as s:
        return await konta.zarejestruj(s, email, haslo, None)


async def _zla(maker, email=JAN, ip=IP1) -> None:
    """Zla proba: nadal zwykla odmowa z jednym komunikatem."""
    async with maker() as s:
        with pytest.raises(BrakDostepu) as e:
            await konta.zaloguj(s, email, ZLE_HASLO, ip)
    assert str(e.value) == KOMUNIKAT_LOGOWANIA


async def _zablokowana(maker, email=JAN, ip=IP1, haslo=HASLO) -> str:
    async with maker() as s:
        with pytest.raises(ZbytWieleProb) as e:
            await konta.zaloguj(s, email, haslo, ip)
    return str(e.value)


async def _poprawna(maker, email=JAN, ip=IP1, haslo=HASLO) -> User:
    async with maker() as s:
        return await konta.zaloguj(s, email, haslo, ip)


async def _licznik(maker, klucz: str) -> BlokadaLogowania | None:
    async with maker() as s:
        return (
            await s.execute(select(BlokadaLogowania).where(BlokadaLogowania.klucz == klucz))
        ).scalar_one_or_none()


async def _wszystkie_liczniki(maker) -> list[BlokadaLogowania]:
    async with maker() as s:
        return list((await s.execute(select(BlokadaLogowania))).scalars())


def _komunikat(minuty: int) -> str:
    return konta.KOMUNIKAT_BLOKADY.format(minuty=minuty)


async def _zablokuj_pare(maker, email=JAN, ip=IP1) -> None:
    for _ in range(5):
        await _zla(maker, email, ip)


# --- granica limitu pary -------------------------------------------------------------------------


async def test_szosta_proba_z_poprawnym_haslem_po_pieciu_zlych_jest_wstrzymana_z_komunikatem(
    maker, zegar
):
    await _rejestruj(maker)
    for _ in range(5):
        await _zla(maker)  # takze piata dostaje zwykla odmowe, nie blokade

    assert await _zablokowana(maker) == "zbyt wiele nieudanych prób, spróbuj za 15 min"
    assert await _zablokowana(maker) == _komunikat(15)


async def test_po_czterech_zlych_probach_poprawne_haslo_jeszcze_przechodzi(maker, zegar):
    jan = await _rejestruj(maker)
    for _ in range(4):
        await _zla(maker)

    assert (await _poprawna(maker)).id == jan.id


async def test_piata_zla_proba_ustawia_blokade_na_czas_blokady_od_teraz(maker, zegar):
    await _rejestruj(maker)
    for _ in range(4):
        await _zla(maker)
    assert (await _licznik(maker, konta.klucz_pary(JAN, IP1))).zablokowane_do is None

    await _zla(maker)

    wiersz = await _licznik(maker, konta.klucz_pary(JAN, IP1))
    assert wiersz.nieudane == 5
    assert _utc(wiersz.zablokowane_do) == T0 + timedelta(seconds=900)


async def test_blokada_trwa_tyle_ile_wynosi_czas_blokady_z_ustawien(maker, zegar, monkeypatch):
    monkeypatch.setattr(settings, "logowanie_blokada_s", 120)
    await _rejestruj(maker)
    await _zablokuj_pare(maker)

    wiersz = await _licznik(maker, konta.klucz_pary(JAN, IP1))
    assert _utc(wiersz.zablokowane_do) == T0 + timedelta(seconds=120)
    assert await _zablokowana(maker) == _komunikat(2)


async def test_limit_prob_pary_pochodzi_z_ustawien(maker, zegar, monkeypatch):
    monkeypatch.setattr(settings, "logowanie_limit_prob", 2)
    await _rejestruj(maker)
    await _zla(maker)
    await _zla(maker)

    assert await _zablokowana(maker) == _komunikat(15)


# --- koniec blokady i komunikat ------------------------------------------------------------------


async def test_dokladnie_w_chwili_konca_blokady_poprawne_haslo_przechodzi_a_sekunde_wczesniej_nie(
    maker, zegar
):
    jan = await _rejestruj(maker)
    await _zablokuj_pare(maker)

    zegar.t = T0 + timedelta(seconds=899)
    assert await _zablokowana(maker) == _komunikat(1)

    zegar.t = T0 + timedelta(seconds=900)
    assert (await _poprawna(maker)).id == jan.id


async def test_po_koncu_blokady_zla_proba_dostaje_zwykla_odmowe_nie_blokade(maker, zegar):
    await _rejestruj(maker)
    await _zablokuj_pare(maker)

    zegar.t = T0 + timedelta(seconds=900)
    await _zla(maker)


@pytest.mark.parametrize(
    ("zostalo_s", "minuty"),
    [(0.4, 1), (1, 1), (60, 1), (61, 2), (120, 2), (121, 3), (899, 15), (900, 15)],
)
async def test_komunikat_podaje_pozostale_minuty_zaokraglone_w_gore(
    maker, zegar, zostalo_s, minuty
):
    await _rejestruj(maker)
    await _zablokuj_pare(maker)

    zegar.t = T0 + timedelta(seconds=900) - timedelta(seconds=zostalo_s)

    assert await _zablokowana(maker) == _komunikat(minuty)


async def test_po_wygasnieciu_blokady_pierwsza_zla_proba_liczy_od_nowa(maker, zegar, monkeypatch):
    # okna dluzsze niz blokada: o nowym liczeniu decyduje wylacznie wygasla blokada
    monkeypatch.setattr(settings, "logowanie_okno_s", 100_000)
    monkeypatch.setattr(settings, "logowanie_okno_globalne_s", 100_000)
    monkeypatch.setattr(settings, "logowanie_limit_globalny", 5)
    await _rejestruj(maker)
    await _zablokuj_pare(maker)
    assert (await _licznik(maker, konta.klucz_adresu(JAN))).zablokowane_do is not None

    zegar.t = T0 + timedelta(seconds=900)
    await _zla(maker)

    for klucz in (konta.klucz_pary(JAN, IP1), konta.klucz_adresu(JAN)):
        wiersz = await _licznik(maker, klucz)
        assert wiersz.nieudane == 1
        assert wiersz.zablokowane_do is None
        assert _utc(wiersz.pierwsza_at) == T0 + timedelta(seconds=900)


async def test_po_wygasnieciu_blokady_pary_licznik_adresu_bez_blokady_dalej_rosnie(
    maker, zegar, monkeypatch
):
    monkeypatch.setattr(settings, "logowanie_okno_s", 100_000)
    monkeypatch.setattr(settings, "logowanie_okno_globalne_s", 100_000)
    await _rejestruj(maker)
    await _zablokuj_pare(maker)  # adres: 5 z limitu 50, bez blokady

    zegar.t = T0 + timedelta(seconds=900)
    await _zla(maker)

    adres = await _licznik(maker, konta.klucz_adresu(JAN))
    assert adres.nieudane == 6
    assert _utc(adres.pierwsza_at) == T0


@pytest.mark.parametrize("pierwsza_blokada_s", [100, 900])
async def test_komunikat_liczy_z_pozniejszego_konca_ze_wszystkich_aktywnych_blokad(
    maker, zegar, monkeypatch, pierwsza_blokada_s
):
    # para (JAN, IP1) i limit adresu koncza sie o roznych porach; raz dluzsza jest jedna, raz druga
    drugi_blokada_s = 1000 - pierwsza_blokada_s
    monkeypatch.setattr(settings, "logowanie_limit_prob", 2)
    monkeypatch.setattr(settings, "logowanie_limit_globalny", 3)
    await _rejestruj(maker)
    monkeypatch.setattr(settings, "logowanie_blokada_s", pierwsza_blokada_s)
    await _zla(maker, ip=IP1)
    await _zla(maker, ip=IP1)  # para (JAN, IP1) blokowana na pierwsza_blokada_s
    monkeypatch.setattr(settings, "logowanie_blokada_s", drugi_blokada_s)
    await _zla(maker, ip=IP2)  # trzecia nieudana adresu: limit globalny na drugi_blokada_s

    dluzsza_s = max(pierwsza_blokada_s, drugi_blokada_s)
    oczekiwane = (dluzsza_s + 59) // 60
    assert await _zablokowana(maker, ip=IP1) == _komunikat(oczekiwane)


# --- adres bez konta -----------------------------------------------------------------------------


async def test_nieistniejacy_adres_po_pieciu_probach_dostaje_taka_sama_blokade_jak_istniejacy(
    maker, zegar
):
    await _rejestruj(maker, JAN)
    await _zablokuj_pare(maker, JAN)
    for _ in range(5):
        await _zla(maker, NIKT)  # piata takze zwykla odmowa, nie blokada

    zegar.t = T0 + timedelta(seconds=300)
    istniejacy = await _zablokowana(maker, JAN)
    nieistniejacy = await _zablokowana(maker, NIKT, haslo=ZLE_HASLO)

    assert nieistniejacy == istniejacy == _komunikat(10)


async def test_nieistniejacy_adres_ma_liczniki_w_bazie_tak_jak_istniejacy(maker, zegar):
    for _ in range(3):
        await _zla(maker, NIKT)

    para = await _licznik(maker, konta.klucz_pary(NIKT, IP1))
    adres = await _licznik(maker, konta.klucz_adresu(NIKT))
    assert (para.nieudane, adres.nieudane) == (3, 3)


# --- zerowanie po sukcesie -----------------------------------------------------------------------


async def test_udane_logowanie_po_czterech_zlych_zeruje_licznik_pary_ale_nie_adresu(maker, zegar):
    await _rejestruj(maker)
    for _ in range(4):
        await _zla(maker)

    await _poprawna(maker)

    assert await _licznik(maker, konta.klucz_pary(JAN, IP1)) is None
    adres = await _licznik(maker, konta.klucz_adresu(JAN))
    assert adres.nieudane == 4


async def test_po_udanym_logowaniu_kolejne_cztery_zle_nie_zlewaja_sie_z_poprzednimi(maker, zegar):
    jan = await _rejestruj(maker)
    for _ in range(4):
        await _zla(maker)
    await _poprawna(maker)
    for _ in range(4):
        await _zla(maker)

    assert (await _poprawna(maker)).id == jan.id


async def test_udane_logowanie_z_jednego_ip_nie_zeruje_licznika_pary_z_innego_ip(maker, zegar):
    await _rejestruj(maker)
    await _zla(maker, ip=IP1)
    await _zla(maker, ip=IP2)
    await _zla(maker, ip=IP2)

    await _poprawna(maker, ip=IP1)

    assert await _licznik(maker, konta.klucz_pary(JAN, IP1)) is None
    assert (await _licznik(maker, konta.klucz_pary(JAN, IP2))).nieudane == 2


async def test_udane_logowanie_nie_zeruje_limitu_globalnego_adresu(maker, zegar, monkeypatch):
    monkeypatch.setattr(settings, "logowanie_limit_globalny", 3)
    await _rejestruj(maker)
    await _zla(maker, ip=IP1)
    await _zla(maker, ip=IP1)
    await _poprawna(maker, ip=IP1)  # nie omija limitu globalnego
    await _zla(maker, ip=IP2)  # trzecia nieudana adresu

    assert await _zablokowana(maker, ip=IP3) == _komunikat(15)


# --- izolacja par i adresow ----------------------------------------------------------------------


async def test_blokada_jednego_adresu_nie_blokuje_innego_adresu(maker, zegar):
    await _rejestruj(maker, JAN)
    ewa = await _rejestruj(maker, EWA)
    await _zablokuj_pare(maker, JAN)
    await _zablokowana(maker, JAN)

    assert (await _poprawna(maker, EWA)).id == ewa.id


async def test_blokada_pary_nie_blokuje_tego_samego_adresu_z_innego_ip(maker, zegar):
    jan = await _rejestruj(maker)
    await _zablokuj_pare(maker, JAN, IP1)
    await _zablokowana(maker, JAN, IP1)

    assert (await _poprawna(maker, JAN, IP2)).id == jan.id


async def test_blokada_pary_nie_blokuje_innego_adresu_z_tego_samego_ip(maker, zegar):
    await _rejestruj(maker, JAN)
    ewa = await _rejestruj(maker, EWA)
    await _zablokuj_pare(maker, JAN, IP1)

    assert (await _poprawna(maker, EWA, IP1)).id == ewa.id


# --- limit globalny adresu -----------------------------------------------------------------------


async def test_limit_globalny_blokuje_adres_takze_dla_nowego_ip(maker, zegar, monkeypatch):
    monkeypatch.setattr(settings, "logowanie_limit_globalny", 3)
    await _rejestruj(maker)
    await _zla(maker, ip=IP1)
    await _zla(maker, ip=IP2)
    await _zla(maker, ip=IP3)  # trzecia nieudana z roznych IP: zwykla odmowa

    assert await _zablokowana(maker, ip=IP4) == _komunikat(15)
    assert await _licznik(maker, konta.klucz_pary(JAN, IP4)) is None


async def test_przy_dwoch_nieudanych_z_roznych_ip_limit_globalny_trzech_jeszcze_nie_blokuje(
    maker, zegar, monkeypatch
):
    monkeypatch.setattr(settings, "logowanie_limit_globalny", 3)
    jan = await _rejestruj(maker)
    await _zla(maker, ip=IP1)
    await _zla(maker, ip=IP2)

    assert (await _poprawna(maker, ip=IP3)).id == jan.id


async def test_limit_globalny_blokuje_na_czas_blokady_i_potem_puszcza(maker, zegar, monkeypatch):
    monkeypatch.setattr(settings, "logowanie_limit_globalny", 3)
    monkeypatch.setattr(settings, "logowanie_blokada_s", 600)
    jan = await _rejestruj(maker)
    for ip in (IP1, IP2, IP3):
        await _zla(maker, ip=ip)

    adres = await _licznik(maker, konta.klucz_adresu(JAN))
    assert _utc(adres.zablokowane_do) == T0 + timedelta(seconds=600)
    zegar.t = T0 + timedelta(seconds=599)
    assert await _zablokowana(maker, ip=IP4) == _komunikat(1)
    zegar.t = T0 + timedelta(seconds=600)
    assert (await _poprawna(maker, ip=IP4)).id == jan.id


async def test_limit_globalny_jednego_adresu_nie_blokuje_innego_adresu(maker, zegar, monkeypatch):
    monkeypatch.setattr(settings, "logowanie_limit_globalny", 3)
    await _rejestruj(maker, JAN)
    ewa = await _rejestruj(maker, EWA)
    for ip in (IP1, IP2, IP3):
        await _zla(maker, JAN, ip)

    assert (await _poprawna(maker, EWA, IP4)).id == ewa.id


async def test_domyslny_limit_globalny_to_piecdziesiat_nieudanych_prob_z_roznych_ip(
    maker, zegar
):
    jan = await _rejestruj(maker)
    for i in range(49):
        await _zla(maker, ip=f"10.0.{i // 200}.{i % 200}")
    assert (await _poprawna(maker, ip="10.9.9.9")).id == jan.id

    await _zla(maker, ip="10.9.9.10")  # piecdziesiata nieudana adresu
    assert await _zablokowana(maker, ip="10.9.9.11") == _komunikat(15)


# --- okna liczenia -------------------------------------------------------------------------------


async def test_proba_po_uplywie_okna_pary_liczy_od_nowa_a_sekunde_wczesniej_kontynuuje(
    maker, zegar, monkeypatch
):
    monkeypatch.setattr(settings, "logowanie_okno_s", 100)
    monkeypatch.setattr(settings, "logowanie_okno_globalne_s", 1000)
    await _rejestruj(maker)
    for _ in range(3):
        await _zla(maker)

    zegar.t = T0 + timedelta(seconds=99)
    await _zla(maker)
    para = await _licznik(maker, konta.klucz_pary(JAN, IP1))
    assert para.nieudane == 4
    assert _utc(para.pierwsza_at) == T0

    zegar.t = T0 + timedelta(seconds=100)
    await _zla(maker)
    para = await _licznik(maker, konta.klucz_pary(JAN, IP1))
    assert para.nieudane == 1
    assert _utc(para.pierwsza_at) == T0 + timedelta(seconds=100)


async def test_okno_pary_nie_resetuje_licznika_globalnego_adresu(maker, zegar, monkeypatch):
    monkeypatch.setattr(settings, "logowanie_okno_s", 100)
    monkeypatch.setattr(settings, "logowanie_okno_globalne_s", 1000)
    await _rejestruj(maker)
    for _ in range(3):
        await _zla(maker)

    zegar.t = T0 + timedelta(seconds=100)
    await _zla(maker)

    adres = await _licznik(maker, konta.klucz_adresu(JAN))
    assert adres.nieudane == 4
    assert _utc(adres.pierwsza_at) == T0


async def test_proba_po_uplywie_okna_globalnego_liczy_adres_od_nowa_a_sekunde_wczesniej_nie(
    maker, zegar, monkeypatch
):
    monkeypatch.setattr(settings, "logowanie_okno_s", 100)
    monkeypatch.setattr(settings, "logowanie_okno_globalne_s", 1000)
    await _rejestruj(maker)
    for _ in range(3):
        await _zla(maker)

    zegar.t = T0 + timedelta(seconds=999)
    await _zla(maker)
    adres = await _licznik(maker, konta.klucz_adresu(JAN))
    assert adres.nieudane == 4
    assert _utc(adres.pierwsza_at) == T0

    zegar.t = T0 + timedelta(seconds=1000)
    await _zla(maker)
    adres = await _licznik(maker, konta.klucz_adresu(JAN))
    assert adres.nieudane == 1
    assert _utc(adres.pierwsza_at) == T0 + timedelta(seconds=1000)


async def test_okno_globalne_odlicza_sie_od_pierwszej_proby_a_nie_od_ostatniej(
    maker, zegar, monkeypatch
):
    monkeypatch.setattr(settings, "logowanie_okno_globalne_s", 1000)
    monkeypatch.setattr(settings, "logowanie_limit_globalny", 3)
    monkeypatch.setattr(settings, "logowanie_okno_s", 100_000)
    await _rejestruj(maker)
    await _zla(maker, ip=IP1)
    zegar.t = T0 + timedelta(seconds=600)
    await _zla(maker, ip=IP2)
    zegar.t = T0 + timedelta(seconds=1200)  # ponad 1000 s od pierwszej, mniej niz od ostatniej
    await _zla(maker, ip=IP3)

    adres = await _licznik(maker, konta.klucz_adresu(JAN))
    assert adres.nieudane == 1
    assert adres.zablokowane_do is None


async def test_zle_proby_rozlozone_poza_oknem_pary_nie_prowadza_do_blokady(
    maker, zegar, monkeypatch
):
    monkeypatch.setattr(settings, "logowanie_okno_s", 100)
    jan = await _rejestruj(maker)
    for i in range(5):
        zegar.t = T0 + timedelta(seconds=100 * i)
        await _zla(maker)

    assert (await _poprawna(maker)).id == jan.id


# --- zablokowane wywolanie -----------------------------------------------------------------------


async def test_zablokowane_wywolanie_nie_zmienia_licznikow_w_bazie(maker, zegar):
    await _rejestruj(maker)
    await _zablokuj_pare(maker)

    def migawka(wiersze):
        return sorted(
            (w.klucz, w.nieudane, _utc(w.pierwsza_at), w.zablokowane_do and _utc(w.zablokowane_do))
            for w in wiersze
        )

    przed = migawka(await _wszystkie_liczniki(maker))
    zegar.t = T0 + timedelta(seconds=100)
    await _zablokowana(maker, haslo=HASLO)
    await _zablokowana(maker, haslo=ZLE_HASLO)

    assert migawka(await _wszystkie_liczniki(maker)) == przed


async def test_zablokowane_poprawne_haslo_nie_zeruje_licznika_pary(maker, zegar):
    await _rejestruj(maker)
    await _zablokuj_pare(maker)

    await _zablokowana(maker, haslo=HASLO)

    assert (await _licznik(maker, konta.klucz_pary(JAN, IP1))).nieudane == 5


# --- normalizacja i klucze -----------------------------------------------------------------------


async def test_adres_w_innej_wielkosci_liter_i_ze_spacjami_dzieli_licznik_z_normalnym(maker, zegar):
    await _rejestruj(maker)
    for _ in range(3):
        await _zla(maker, "  JAN@Przyklad.PL ")
    for _ in range(2):
        await _zla(maker, JAN)

    assert await _zablokowana(maker, "JAN@Przyklad.PL ") == _komunikat(15)
    assert await _zablokowana(maker, JAN) == _komunikat(15)


async def test_klucze_licznikow_to_sha256_z_przestrzenia_nazw_adresu_i_pary(maker, zegar):
    assert konta.klucz_pary("  JAN@Przyklad.PL ", IP1) == _sha(f"para\0{JAN}\0{IP1}")
    assert konta.klucz_adresu("  JAN@Przyklad.PL ") == _sha(f"adres\0{JAN}")
    assert konta.klucz_pary(JAN, IP1) != konta.klucz_adresu(JAN)
    assert konta.klucz_pary(JAN, IP1) != konta.klucz_pary(JAN, IP2)

    await _rejestruj(maker)
    await _zla(maker, "  JAN@Przyklad.PL ", IP1)
    klucze = {w.klucz for w in await _wszystkie_liczniki(maker)}
    assert klucze == {_sha(f"para\0{JAN}\0{IP1}"), _sha(f"adres\0{JAN}")}


async def test_w_bazie_nie_ma_jawnego_adresu_ani_ip_tylko_skroty(maker, zegar):
    await _rejestruj(maker)
    await _zla(maker, JAN, IP1)
    await _zla(maker, NIKT, IP2)

    wiersze = await _wszystkie_liczniki(maker)
    assert len(wiersze) == 4
    for w in wiersze:
        assert len(w.klucz) == 64
        assert set(w.klucz) <= set("0123456789abcdef")
        napis = f"{w.klucz}|{w.nieudane}|{w.pierwsza_at}|{w.zablokowane_do}"
        for jawne in ("jan", "nikt", "przyklad", IP1, IP2, "203.0", "198.51"):
            assert jawne not in napis


# --- licznik trwaly ------------------------------------------------------------------------------


async def test_licznik_przezywa_wyjatek_i_zamkniecie_sesji_wolajacego(maker, zegar):
    await _rejestruj(maker)
    async with maker() as s:
        with pytest.raises(BrakDostepu):
            await konta.zaloguj(s, JAN, ZLE_HASLO, IP1)
        await s.rollback()  # wolajacy cofa swoja transakcje po bledzie

    assert (await _licznik(maker, konta.klucz_pary(JAN, IP1))).nieudane == 1
    assert (await _licznik(maker, konta.klucz_adresu(JAN))).nieudane == 1


async def test_blokada_obowiazuje_nowa_sesje_bo_licznik_jest_w_bazie_nie_w_pamieci(maker, zegar):
    await _rejestruj(maker)
    await _zablokuj_pare(maker)  # kazda proba w osobnej sesji bazy

    assert await _zablokowana(maker) == _komunikat(15)


# --- ustawienia i typ bledu ----------------------------------------------------------------------

USTAWIENIA_LOGOWANIA = [
    ("logowanie_limit_prob", 5),
    ("logowanie_okno_s", 900),
    ("logowanie_blokada_s", 900),
    ("logowanie_limit_globalny", 50),
    ("logowanie_okno_globalne_s", 3600),
]


@pytest.mark.parametrize(("nazwa", "domyslna"), USTAWIENIA_LOGOWANIA)
def test_ustawienie_blokady_logowania_ma_domyslna_wartosc_z_decyzji_wlasciciela(
    monkeypatch, nazwa, domyslna
):
    monkeypatch.delenv(f"COCKPIT_{nazwa.upper()}", raising=False)
    assert getattr(Settings(_env_file=None), nazwa) == domyslna


@pytest.mark.parametrize(("nazwa", "_"), USTAWIENIA_LOGOWANIA)
@pytest.mark.parametrize("wartosc", [0, -1])
def test_ustawienie_blokady_logowania_zero_albo_ujemne_jest_odrzucone(
    monkeypatch, nazwa, _, wartosc
):
    monkeypatch.delenv(f"COCKPIT_{nazwa.upper()}", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{nazwa: wartosc})


@pytest.mark.parametrize(("nazwa", "_"), USTAWIENIA_LOGOWANIA)
def test_ustawienie_blokady_logowania_jeden_jest_dozwolony(monkeypatch, nazwa, _):
    monkeypatch.delenv(f"COCKPIT_{nazwa.upper()}", raising=False)
    assert getattr(Settings(_env_file=None, **{nazwa: 1}), nazwa) == 1


def test_zbyt_wiele_prob_jest_bledem_dziedziny_ale_nie_zwykla_odmowa_dostepu():
    assert issubclass(ZbytWieleProb, BladDziedziny)
    assert not issubclass(ZbytWieleProb, BrakDostepu)


def test_komunikat_blokady_ma_ustalone_brzmienie():
    assert konta.KOMUNIKAT_BLOKADY == "zbyt wiele nieudanych prób, spróbuj za {minuty} min"


def test_tabela_kodow_http_przypisuje_blokadzie_logowania_429():
    assert KODY_BLEDOW[ZbytWieleProb] == 429


async def test_zablokowane_logowanie_przez_prawdziwa_trase_daje_429_z_komunikatem_blokady(
    maker, zegar
):
    await _rejestruj(maker)
    await _zablokuj_pare(maker)
    app = FastAPI()
    zarejestruj_obsluge_bledow(app)

    @app.get("/zaloguj")
    async def zaloguj_trasa():
        async with maker() as s:
            await konta.zaloguj(s, JAN, HASLO, IP1)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as c:
        r = await c.get("/zaloguj")

    assert r.status_code == 429
    assert r.json() == {"detail": _komunikat(15)}


# --- blokada wiersza licznika --------------------------------------------------------------------


class SesjaZeSzpiegiem(AsyncSession):
    """Prawdziwa sesja; zapamietuje kazde zapytanie przekazane do `execute`."""

    zapytania: list = []

    async def execute(self, statement, *args, **kwargs):
        self.zapytania.append(statement)
        return await super().execute(statement, *args, **kwargs)


async def test_odczyt_licznikow_pary_i_adresu_blokuje_wiersze_for_update_w_postgresql(
    maker, zegar
):
    # SQLite ignoruje FOR UPDATE, wiec sprawdzamy zapytanie skompilowane dla PostgreSQL
    await _rejestruj(maker)
    SesjaZeSzpiegiem.zapytania = []
    szpieg = async_sessionmaker(
        maker.kw["bind"], class_=SesjaZeSzpiegiem, expire_on_commit=False
    )
    async with szpieg() as s:
        with pytest.raises(BrakDostepu):
            await konta.zaloguj(s, JAN, ZLE_HASLO, IP1)

    sql = [str(z.compile(dialect=postgresql.dialect())) for z in SesjaZeSzpiegiem.zapytania]
    liczniki = [q for q in sql if "FROM blokada_logowania" in q]
    assert len(liczniki) == 2  # para i adres
    for q in liczniki:
        assert q.rstrip().endswith("FOR UPDATE"), q
