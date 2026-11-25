"""Reset hasla jednorazowym tokenem z listu (HAR-105).

Testy przed implementacja: `cockpit.services.reset_hasla`, `TokenResetu` i `konta.ustaw_haslo`
jeszcze nie istnieja. SQLite w tmp_path, produkcyjna aplikacja `cockpit.api.app.app`,
wysylka tylko atrapa (wstrzykniete `wyslij` albo podmieniona wysylka SMTP) - zadnej sieci.
Kazda asercja o skutku czyta dane w NOWEJ sesji `maker()`. Zegar: `sesje.teraz`
(konwencja z test_konta.py).

Zalozenia kontraktu HTTP, ktorych ticket nie ustala (backend ma sie ich trzymac albo
poprawic test): ciaglo POST /api/konto/reset/potwierdz to {"token", "nowe_haslo"}.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from httpx import ASGITransport
from pydantic import ValidationError
from sqlalchemy import event, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api.app import app
from cockpit.db.models import Base, Sesja, TokenResetu, User
from cockpit.db.session import get_session
from cockpit.services import konta, poczta, reset_hasla, sesje
from cockpit.services.errors import BladPoczty, BrakDostepu, StanNiemozliwy
from cockpit.settings import Settings, settings

HASLO = "Sekret-Hasl0-Alfa"
NOWE_HASLO = "Nowe-Hasl0-Delta"
INNE_NOWE_HASLO = "Inne-Hasl0-Sigma"
JAN = "jan@przyklad.pl"
EWA = "ewa@przyklad.pl"
NIKT = "nikt@przyklad.pl"
IP = "203.0.113.7"
ADRES_PUBLICZNY = "https://kokpit.przyklad.pl"
T0 = datetime(2030, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
GODZINA = 3600
KOMUNIKAT_ZA_KROTKIE = "hasło musi mieć co najmniej 8 znaków"
STARA_DATA = datetime(2020, 1, 1, tzinfo=timezone.utc)


# --- swiat testu -------------------------------------------------------------------------------


@pytest.fixture
async def maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reset_hasla.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class Zegar:
    def __init__(self) -> None:
        self.teraz = T0

    def przesun(self, sekundy: int) -> None:
        self.teraz = self.teraz + timedelta(seconds=sekundy)


@pytest.fixture(autouse=True)
def zegar(monkeypatch) -> Zegar:
    z = Zegar()
    monkeypatch.setattr(sesje, "teraz", lambda: z.teraz)
    return z


@pytest.fixture(autouse=True)
def _poczta_wlaczona(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.przyklad.pl")
    monkeypatch.setattr(settings, "smtp_nadawca", "kokpit@przyklad.pl")
    monkeypatch.setattr(settings, "smtp_uzytkownik", None)
    monkeypatch.setattr(settings, "smtp_haslo", None)
    monkeypatch.setattr(settings, "adres_publiczny", ADRES_PUBLICZNY)
    monkeypatch.setattr(settings, "ciasteczko_secure", True)


class Skrzynka:
    """Atrapa `wyslij`: zapamietuje listy zamiast je wysylac."""

    def __init__(self) -> None:
        self.listy: list[tuple[str, str, str]] = []

    async def __call__(self, do: str, temat: str, tresc: str) -> None:
        self.listy.append((do, temat, tresc))

    def token_z_ostatniego_listu(self) -> str:
        return _token_z_tresci(self.listy[-1][2])


@pytest.fixture
def skrzynka() -> Skrzynka:
    return Skrzynka()


TEMAT_LISTU = "Reset hasła w Harness Cockpit"
KOMUNIKAT_TOKENU = "link do zmiany hasła jest nieprawidłowy albo wygasł"
KOMUNIKAT_RESETU = "jeśli adres ma konto, wysłaliśmy na niego link do zmiany hasła"
KOMUNIKAT_BRAK_ADRESU = "reset hasła wyłączony: ustaw zmienną COCKPIT_ADRES_PUBLICZNY"
KOMUNIKAT_BRAK_POCZTY = "poczta wyłączona: ustaw zmienną COCKPIT_SMTP_HOST"
KOMUNIKAT_NIEKODOWALNE = "hasło zawiera znaki, których nie da się zapisać"
KOMUNIKAT_AWARII_POCZTY = "wysylka listu resetu hasla nie powiodla sie"
SUROGAT = "\ud800abcdefgh"   # samotny surogat: 9 znaków, ale nie da się go zapisać w UTF-8


def _tresc_listu(link: str, minuty: int) -> str:
    return (
        "Ktoś poprosił o zmianę hasła do Harness Cockpit.\n\n"
        f"Ustaw nowe hasło tym linkiem (jednorazowy, ważny {minuty} min):\n{link}\n\n"
        "Jeśli to nie Ty, zignoruj ten list: hasło się nie zmieni.\n"
    )


def _link_z_tresci(tresc: str) -> str:
    linie = [linia for linia in tresc.splitlines() if linia.startswith("http")]
    assert len(linie) == 1, tresc
    return linie[0]


def _token_z_tresci(tresc: str) -> str:
    trafienia = re.findall(re.escape(ADRES_PUBLICZNY) + r"/#reset=([A-Za-z0-9_-]+)$", tresc, re.M)
    assert len(trafienia) == 1, tresc
    return trafienia[0]


def _sha(napis: str) -> str:
    return hashlib.sha256(napis.encode("utf-8")).hexdigest()


def _utc(dt: datetime) -> datetime:
    # SQLite oddaje naive datetime z kolumn DateTime(timezone=True)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


async def _rejestruj(maker, email=JAN, haslo=HASLO) -> User:
    async with maker() as s:
        return await konta.zarejestruj(s, email, haslo)


async def _nowa_sesja(maker, user: User) -> str:
    async with maker() as s:
        ident, _ = await sesje.utworz(s, user)
    return ident


async def _popros(maker, skrzynka: Skrzynka, email: str = JAN):
    async with maker() as s:
        return await reset_hasla.popros(s, email, wyslij=skrzynka)


async def _popros_i_odbierz(maker, skrzynka: Skrzynka, email: str = JAN) -> str:
    await _popros(maker, skrzynka, email)
    return skrzynka.token_z_ostatniego_listu()


async def _uzyj(maker, token: str, haslo: str = NOWE_HASLO):
    async with maker() as s:
        return await reset_hasla.uzyj(s, token, haslo)


async def _tokeny(maker) -> list[TokenResetu]:
    async with maker() as s:
        return list((await s.execute(select(TokenResetu))).scalars())


async def _sesje_uzytkownika(maker, user_id: str) -> list[Sesja]:
    async with maker() as s:
        return list((await s.execute(select(Sesja).where(Sesja.user_id == user_id))).scalars())


async def _wszystkie_wartosci(maker) -> list[str]:
    """Kazda wartosc kazdej kolumny wszystkich tabel jako napis."""
    wartosci: list[str] = []
    async with maker() as s:
        for tabela in Base.metadata.sorted_tables:
            for wiersz in (await s.execute(select(tabela))).all():
                wartosci.extend(str(v) for v in wiersz)
    return wartosci


async def _haslo_dziala(maker, email: str, haslo: str) -> bool:
    async with maker() as s:
        try:
            await konta.zaloguj(s, email, haslo, IP)
        except BrakDostepu:
            return False
    return True


async def _komunikat_odmowy_uzycia(maker, token: str) -> str:
    async with maker() as s:
        with pytest.raises(BrakDostepu) as e:
            await reset_hasla.uzyj(s, token, NOWE_HASLO)
    return str(e.value)


# --- prosba o reset: wiadomosc i zapis ----------------------------------------------------------


async def test_prosba_istniejacego_adresu_wysyla_jeden_list_z_linkiem_na_adres_publiczny(
    maker, skrzynka
):
    await _rejestruj(maker)

    await _popros(maker, skrzynka)

    assert len(skrzynka.listy) == 1
    do, temat, tresc = skrzynka.listy[0]
    assert do == JAN
    assert temat == TEMAT_LISTU
    token = _token_z_tresci(tresc)
    assert tresc == _tresc_listu(f"{ADRES_PUBLICZNY}/#reset={token}", 60)


async def test_link_w_liscie_trzyma_token_we_fragmencie_a_nie_w_zapytaniu(maker, skrzynka):
    await _rejestruj(maker)
    token = await _popros_i_odbierz(maker, skrzynka)

    link = _link_z_tresci(skrzynka.listy[0][2])

    # fragment nie trafia do serwera ani do logu dostepu; "?reset=" by go tam wyslalo
    assert link == f"{ADRES_PUBLICZNY}/#reset={token}"
    assert "?" not in link


@pytest.mark.parametrize("adres", [ADRES_PUBLICZNY + "/", "  " + ADRES_PUBLICZNY + "/  "])
async def test_adres_publiczny_z_koncowym_ukosnikiem_albo_spacjami_daje_link_bez_podwojnego_ukosnika(
    maker, skrzynka, monkeypatch, adres
):
    await _rejestruj(maker)
    monkeypatch.setattr(settings, "adres_publiczny", adres)

    token = await _popros_i_odbierz(maker, skrzynka)

    assert _link_z_tresci(skrzynka.listy[0][2]) == f"{ADRES_PUBLICZNY}/#reset={token}"


@pytest.mark.parametrize(("sekundy", "minuty"), [(3600, 60), (120, 2), (119, 1), (59, 1), (1, 1)])
async def test_list_podaje_waznosc_linku_w_pelnych_minutach_ale_nie_mniej_niz_jedna(
    maker, skrzynka, monkeypatch, sekundy, minuty
):
    await _rejestruj(maker)
    monkeypatch.setattr(settings, "reset_wazny_s", sekundy)

    token = await _popros_i_odbierz(maker, skrzynka)

    assert skrzynka.listy[0][2] == _tresc_listu(f"{ADRES_PUBLICZNY}/#reset={token}", minuty)


@pytest.mark.parametrize(("wartosc", "przyjeta"), [(0, False), (1, True), (86400, True), (86401, False)])
def test_waznosc_linku_resetu_miesci_sie_miedzy_sekunda_a_doba(wartosc, przyjeta):
    if przyjeta:
        assert Settings(_env_file=None, reset_wazny_s=wartosc).reset_wazny_s == wartosc
    else:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, reset_wazny_s=wartosc)


async def test_token_resetu_ma_32_bajty_losowosci_i_kazda_prosba_dostaje_inny(maker, skrzynka):
    await _rejestruj(maker)
    pierwszy = await _popros_i_odbierz(maker, skrzynka)
    drugi = await _popros_i_odbierz(maker, skrzynka)

    # 32 bajty w base64url bez dopelnienia to 43 znaki
    assert len(pierwszy) == len(drugi) == 43
    assert pierwszy != drugi


async def test_token_z_wiadomosci_nie_wystepuje_w_bazie_a_zapisany_jest_tylko_jego_skrot(
    maker, skrzynka
):
    user = await _rejestruj(maker)
    token = await _popros_i_odbierz(maker, skrzynka)

    wiersze = await _tokeny(maker)
    assert [(w.user_id, w.skrot) for w in wiersze] == [(user.id, _sha(token))]
    wartosci = await _wszystkie_wartosci(maker)
    assert wartosci  # dowod, ze cos przeczytano
    assert not [w for w in wartosci if token in w]


async def test_token_jest_wazny_tyle_sekund_ile_wskazuja_ustawienia_domyslnie_godzine(
    maker, skrzynka
):
    assert Settings(_env_file=None).reset_wazny_s == GODZINA
    await _rejestruj(maker)
    await _popros(maker, skrzynka)

    wiersz = (await _tokeny(maker))[0]
    assert _utc(wiersz.wygasa_at) == T0 + timedelta(seconds=GODZINA)
    assert wiersz.uzyty_at is None


async def test_okres_waznosci_tokenu_pochodzi_z_ustawien_a_nie_ze_stalej(
    maker, skrzynka, zegar, monkeypatch
):
    monkeypatch.setattr(settings, "reset_wazny_s", 120)
    await _rejestruj(maker)
    token = await _popros_i_odbierz(maker, skrzynka)

    assert _utc((await _tokeny(maker))[0].wygasa_at) == T0 + timedelta(seconds=120)
    zegar.przesun(119)
    await _uzyj(maker, token)  # w oknie ustawien (a poza oknem 3600 s nie jest potrzebne)


@pytest.mark.parametrize("adres", ["  jan@przyklad.pl ", "JAN@PRZYKLAD.PL"])
async def test_adres_w_prosbie_jest_normalizowany_osobno_przez_strip_i_przez_lower(
    maker, skrzynka, adres
):
    user = await _rejestruj(maker)

    await _popros(maker, skrzynka, adres)

    assert [wyslany[0] for wyslany in skrzynka.listy] == [JAN]
    assert [w.user_id for w in await _tokeny(maker)] == [user.id]


async def test_prosba_nieistniejacego_adresu_nie_wysyla_listu_i_nie_zapisuje_tokenu(
    maker, skrzynka
):
    await _rejestruj(maker)

    await _popros(maker, skrzynka, NIKT)

    assert skrzynka.listy == []
    assert await _tokeny(maker) == []


async def test_prosba_o_reset_daje_ten_sam_wynik_dla_istniejacego_i_nieistniejacego_adresu(
    maker, skrzynka
):
    await _rejestruj(maker)

    istniejacy = await _popros(maker, skrzynka, JAN)
    nieistniejacy = await _popros(maker, skrzynka, NIKT)

    assert istniejacy == nieistniejacy


async def test_prosba_nie_zmienia_hasla_ani_sesji_uzytkownika(maker, skrzynka):
    user = await _rejestruj(maker)
    ident = await _nowa_sesja(maker, user)

    await _popros(maker, skrzynka)

    assert await _haslo_dziala(maker, JAN, HASLO)
    async with maker() as s:
        assert (await sesje.rozstrzygnij(s, ident)).user_id == user.id


# --- reset wylaczony ----------------------------------------------------------------------------


BRAKI_KONFIGURACJI = [
    ("smtp_host", None, KOMUNIKAT_BRAK_POCZTY),
    ("adres_publiczny", None, KOMUNIKAT_BRAK_ADRESU),
    ("adres_publiczny", "", KOMUNIKAT_BRAK_ADRESU),
    ("adres_publiczny", "   ", KOMUNIKAT_BRAK_ADRESU),
    ("adres_publiczny", "/", KOMUNIKAT_BRAK_ADRESU),
]


@pytest.mark.parametrize(("pole", "wartosc", "komunikat"), BRAKI_KONFIGURACJI)
async def test_bez_poczty_albo_adresu_publicznego_reset_jest_wylaczony_i_nic_nie_powstaje(
    maker, skrzynka, monkeypatch, pole, wartosc, komunikat
):
    await _rejestruj(maker)
    monkeypatch.setattr(settings, pole, wartosc)

    for adres in (JAN, NIKT):
        async with maker() as s:
            with pytest.raises(StanNiemozliwy) as e:
                await reset_hasla.popros(s, adres, wyslij=skrzynka)
        assert str(e.value) == komunikat

    assert skrzynka.listy == []
    assert await _tokeny(maker) == []


async def test_reset_wylaczony_gdy_poczta_ma_nieprawidlowego_nadawce(
    maker, skrzynka, monkeypatch
):
    await _rejestruj(maker)
    monkeypatch.setattr(settings, "smtp_nadawca", "to nie jest adres")
    assert poczta.stan().skonfigurowana is False

    async with maker() as s:
        with pytest.raises(StanNiemozliwy):
            await reset_hasla.popros(s, JAN, wyslij=skrzynka)

    assert skrzynka.listy == []
    assert await _tokeny(maker) == []


# --- awaria wysylki -----------------------------------------------------------------------------


async def test_blad_poczty_nie_przerywa_prosby_a_token_nie_trafia_do_logow(maker, caplog):
    await _rejestruj(maker)
    widziane: list[str] = []

    async def zepsuta(do: str, temat: str, tresc: str) -> None:
        widziane.append(_token_z_tresci(tresc))
        raise BladPoczty("serwer poczty odrzucił wysyłkę listu")

    caplog.set_level(logging.DEBUG)
    async with maker() as s:
        await reset_hasla.popros(s, JAN, wyslij=zepsuta)  # nie rzuca: wynik taki jak przy sukcesie

    assert len(widziane) == 1
    ostrzezenia = [
        (r.levelno, r.getMessage())
        for r in caplog.records
        if r.name == "cockpit.services.reset_hasla"
    ]
    assert ostrzezenia == [(logging.WARNING, KOMUNIKAT_AWARII_POCZTY)]
    assert widziane[0] not in caplog.text


async def test_awaria_inna_niz_blad_poczty_przerywa_prosbe(maker):
    await _rejestruj(maker)

    class Awaria(Exception):
        pass

    async def zepsuta(do: str, temat: str, tresc: str) -> None:
        raise Awaria("nie z poczty")

    async with maker() as s:
        with pytest.raises(Awaria):
            await reset_hasla.popros(s, JAN, wyslij=zepsuta)


async def test_token_z_udanej_wysylki_nie_trafia_do_logow(maker, skrzynka, caplog):
    await _rejestruj(maker)
    caplog.set_level(logging.DEBUG)

    token = await _popros_i_odbierz(maker, skrzynka)
    await _uzyj(maker, token)

    assert token not in caplog.text
    assert NOWE_HASLO not in caplog.text


# --- uzycie tokenu ------------------------------------------------------------------------------


async def test_poprawny_token_ustawia_nowe_haslo_a_stare_przestaje_dzialac(maker, skrzynka):
    user = await _rejestruj(maker)
    async with maker() as s:
        await s.execute(update(User).where(User.id == user.id).values(haslo_zmienione_at=STARA_DATA))
        await s.commit()
    token = await _popros_i_odbierz(maker, skrzynka)

    await _uzyj(maker, token)

    assert await _haslo_dziala(maker, JAN, NOWE_HASLO)
    assert not await _haslo_dziala(maker, JAN, HASLO)
    async with maker() as s:
        zapisany = await s.get(User, user.id)
    assert zapisany.haslo_skrot.startswith("$argon2id$")
    assert _utc(zapisany.haslo_zmienione_at) == T0


async def test_uzyty_token_ma_w_bazie_czas_uzycia(maker, skrzynka):
    await _rejestruj(maker)
    token = await _popros_i_odbierz(maker, skrzynka)

    await _uzyj(maker, token)

    assert _utc((await _tokeny(maker))[0].uzyty_at) == T0


async def test_link_dziala_tylko_raz_drugie_uzycie_nie_zmienia_hasla(maker, skrzynka):
    await _rejestruj(maker)
    token = await _popros_i_odbierz(maker, skrzynka)
    await _uzyj(maker, token, NOWE_HASLO)

    async with maker() as s:
        with pytest.raises(BrakDostepu):
            await reset_hasla.uzyj(s, token, INNE_NOWE_HASLO)

    assert await _haslo_dziala(maker, JAN, NOWE_HASLO)
    assert not await _haslo_dziala(maker, JAN, INNE_NOWE_HASLO)


async def test_token_wazny_jeszcze_sekunde_przed_koncem_godziny_dziala(maker, skrzynka, zegar):
    await _rejestruj(maker)
    token = await _popros_i_odbierz(maker, skrzynka)
    zegar.przesun(GODZINA - 1)

    await _uzyj(maker, token)

    assert await _haslo_dziala(maker, JAN, NOWE_HASLO)


@pytest.mark.parametrize("po_sekundach", [GODZINA, GODZINA + 1, 24 * GODZINA])
async def test_token_wygasa_dokladnie_po_godzinie_a_wygasniety_nie_zmienia_niczego(
    maker, skrzynka, zegar, po_sekundach
):
    user = await _rejestruj(maker)
    await _nowa_sesja(maker, user)
    token = await _popros_i_odbierz(maker, skrzynka)
    zegar.przesun(po_sekundach)  # GODZINA to dokladnie wygasa_at == teraz

    async with maker() as s:
        with pytest.raises(BrakDostepu):
            await reset_hasla.uzyj(s, token, NOWE_HASLO)

    assert await _haslo_dziala(maker, JAN, HASLO)
    assert not await _haslo_dziala(maker, JAN, NOWE_HASLO)
    assert [x.uniewazniona_at for x in await _sesje_uzytkownika(maker, user.id)] == [None]
    assert (await _tokeny(maker))[0].uzyty_at is None


async def test_nieznany_token_jest_odrzucony_i_niczego_nie_zmienia(maker, skrzynka):
    user = await _rejestruj(maker)
    await _nowa_sesja(maker, user)
    await _popros_i_odbierz(maker, skrzynka)

    async with maker() as s:
        with pytest.raises(BrakDostepu):
            await reset_hasla.uzyj(s, "to-nie-jest-token", NOWE_HASLO)

    assert await _haslo_dziala(maker, JAN, HASLO)
    assert [x.uniewazniona_at for x in await _sesje_uzytkownika(maker, user.id)] == [None]
    assert (await _tokeny(maker))[0].uzyty_at is None


async def test_odmowa_ma_ten_sam_komunikat_dla_nieznanego_zuzytego_i_wygaslego_tokenu(
    maker, skrzynka, zegar
):
    await _rejestruj(maker)
    zuzyty = await _popros_i_odbierz(maker, skrzynka)
    await _uzyj(maker, zuzyty)
    wygasly = await _popros_i_odbierz(maker, skrzynka)
    zegar.przesun(GODZINA)

    komunikaty = [
        await _komunikat_odmowy_uzycia(maker, "to-nie-jest-token"),
        await _komunikat_odmowy_uzycia(maker, zuzyty),
        await _komunikat_odmowy_uzycia(maker, wygasly),
    ]

    assert komunikaty == [KOMUNIKAT_TOKENU] * 3


async def test_token_zmienia_haslo_wlasciciela_tokenu_a_nie_innego_uzytkownika(maker, skrzynka):
    await _rejestruj(maker, JAN)
    await _rejestruj(maker, EWA)
    token_jana = await _popros_i_odbierz(maker, skrzynka, JAN)

    await _uzyj(maker, token_jana)

    assert await _haslo_dziala(maker, JAN, NOWE_HASLO)
    assert await _haslo_dziala(maker, EWA, HASLO)
    assert not await _haslo_dziala(maker, EWA, NOWE_HASLO)


async def test_za_krotkie_nowe_haslo_odmawia_a_token_zostaje_nieuzyty_i_sesje_zywe(
    maker, skrzynka
):
    user = await _rejestruj(maker)
    await _nowa_sesja(maker, user)
    token = await _popros_i_odbierz(maker, skrzynka)

    async with maker() as s:
        with pytest.raises(StanNiemozliwy) as e:
            await reset_hasla.uzyj(s, token, "1234567")

    assert str(e.value) == KOMUNIKAT_ZA_KROTKIE
    assert (await _tokeny(maker))[0].uzyty_at is None
    assert [x.uniewazniona_at for x in await _sesje_uzytkownika(maker, user.id)] == [None]
    assert await _haslo_dziala(maker, JAN, HASLO)
    await _uzyj(maker, token, "12345678")  # ten sam token dziala z poprawnym haslem
    assert await _haslo_dziala(maker, JAN, "12345678")


@pytest.mark.parametrize("haslo", ["1234567", SUROGAT])
async def test_odrzucone_haslo_nie_zuzywa_tokenu_nawet_gdy_wolajacy_zatwierdzi_te_sama_sesje(
    maker, skrzynka, haslo
):
    user = await _rejestruj(maker)
    await _nowa_sesja(maker, user)
    token = await _popros_i_odbierz(maker, skrzynka)

    async with maker() as s:
        with pytest.raises(StanNiemozliwy):
            await reset_hasla.uzyj(s, token, haslo)
        # bez wycofania w `uzyj` ten commit zatwierdzilby oznaczenie tokenu jako uzytego
        await s.commit()

    wiersz = (await _tokeny(maker))[0]   # NOWA sesja: zamkniecie starej niczego nie dowodzi
    assert wiersz.uzyty_at is None
    assert [x.uniewazniona_at for x in await _sesje_uzytkownika(maker, user.id)] == [None]
    assert await _haslo_dziala(maker, JAN, HASLO)
    await _uzyj(maker, token, NOWE_HASLO)   # ten sam token dziala z poprawnym haslem
    assert await _haslo_dziala(maker, JAN, NOWE_HASLO)


async def test_haslo_ze_znakiem_ktorego_nie_da_sie_zapisac_daje_blad_dziedziny_z_pelnym_komunikatem(
    maker, skrzynka
):
    await _rejestruj(maker)
    token = await _popros_i_odbierz(maker, skrzynka)

    async with maker() as s:
        with pytest.raises(StanNiemozliwy) as e:
            await reset_hasla.uzyj(s, token, SUROGAT)

    assert str(e.value) == KOMUNIKAT_NIEKODOWALNE


# --- uniewaznienie sesji i pozostalych tokenow --------------------------------------------------


async def test_reset_uniewaznia_wszystkie_sesje_uzytkownika_a_sesje_innego_zostaja(
    maker, skrzynka
):
    jan = await _rejestruj(maker, JAN)
    ewa = await _rejestruj(maker, EWA)
    sesje_jana = [await _nowa_sesja(maker, jan), await _nowa_sesja(maker, jan)]
    sesja_ewy = await _nowa_sesja(maker, ewa)
    token = await _popros_i_odbierz(maker, skrzynka, JAN)

    await _uzyj(maker, token)

    wiersze_jana = await _sesje_uzytkownika(maker, jan.id)
    assert len(wiersze_jana) == 2
    assert all(_utc(w.uniewazniona_at) == T0 for w in wiersze_jana)
    wiersze_ewy = await _sesje_uzytkownika(maker, ewa.id)
    assert [w.uniewazniona_at for w in wiersze_ewy] == [None]
    for ident in sesje_jana:
        async with maker() as s:
            with pytest.raises(BrakDostepu):
                await sesje.rozstrzygnij(s, ident)
    async with maker() as s:
        assert (await sesje.rozstrzygnij(s, sesja_ewy)).user_id == ewa.id


async def test_reset_nie_nadpisuje_czasu_wczesniejszego_uniewaznienia_sesji(maker, skrzynka):
    user = await _rejestruj(maker)
    ident = await _nowa_sesja(maker, user)
    async with maker() as s:
        await s.execute(
            update(Sesja).where(Sesja.skrot == _sha(ident)).values(uniewazniona_at=STARA_DATA)
        )
        await s.commit()
    token = await _popros_i_odbierz(maker, skrzynka)

    await _uzyj(maker, token)

    assert _utc((await _sesje_uzytkownika(maker, user.id))[0].uniewazniona_at) == STARA_DATA


async def test_po_resecie_pozostale_tokeny_uzytkownika_przestaja_dzialac_a_cudze_dzialaja(
    maker, skrzynka
):
    await _rejestruj(maker, JAN)
    await _rejestruj(maker, EWA)
    starszy = await _popros_i_odbierz(maker, skrzynka, JAN)
    nowszy = await _popros_i_odbierz(maker, skrzynka, JAN)
    token_ewy = await _popros_i_odbierz(maker, skrzynka, EWA)

    await _uzyj(maker, nowszy, NOWE_HASLO)

    async with maker() as s:
        with pytest.raises(BrakDostepu):
            await reset_hasla.uzyj(s, starszy, INNE_NOWE_HASLO)
    assert await _haslo_dziala(maker, JAN, NOWE_HASLO)
    await _uzyj(maker, token_ewy, INNE_NOWE_HASLO)
    assert await _haslo_dziala(maker, EWA, INNE_NOWE_HASLO)


async def test_reset_nie_zmienia_czasu_uzycia_tokenu_zuzytego_wczesniej(maker, skrzynka, zegar):
    await _rejestruj(maker)
    token_a = await _popros_i_odbierz(maker, skrzynka)
    await _uzyj(maker, token_a, NOWE_HASLO)
    zegar.przesun(100)
    token_b = await _popros_i_odbierz(maker, skrzynka)

    await _uzyj(maker, token_b, INNE_NOWE_HASLO)

    uzyte = {w.skrot: _utc(w.uzyty_at) for w in await _tokeny(maker)}
    assert uzyte == {_sha(token_a): T0, _sha(token_b): T0 + timedelta(seconds=100)}


# --- blokada konta ------------------------------------------------------------------------------


async def test_resety_jednego_konta_ida_po_kolei_bo_konto_jest_zablokowane_przed_zuzyciem_tokenu(
    maker, skrzynka
):
    await _rejestruj(maker)
    token = await _popros_i_odbierz(maker, skrzynka)
    zapytania: list = []

    def _zapamietaj(stan):
        zapytania.append(stan.statement)

    event.listen(Session, "do_orm_execute", _zapamietaj)
    try:
        await _uzyj(maker, token)
    finally:
        event.remove(Session, "do_orm_execute", _zapamietaj)

    # SQLite pomija FOR UPDATE, wiec blokade widac dopiero w SQL dla PostgreSQL
    sql = [str(z.compile(dialect=postgresql.dialect())) for z in zapytania]
    blokady = [q for q in sql if q.startswith("SELECT") and 'FROM "user"' in q]
    assert len(blokady) == 1
    assert blokady[0].endswith("FOR UPDATE")
    # kolejnosc: lista `sql` jest w kolejnosci wykonania; blokada konta przed pierwszym
    # zuzyciem tokenu (UPDATE), inaczej drugi reset zdazylby zuzyc token przed czekaniem na konto
    przy_blokadzie = sql.index(blokady[0])
    pierwsze_zuzycie = next(i for i, q in enumerate(sql) if q.startswith("UPDATE token_resetu"))
    assert przy_blokadzie < pierwsze_zuzycie


async def test_dwa_rownolegle_resety_tego_samego_konta_roznymi_tokenami_daja_jeden_sukces(
    maker, skrzynka
):
    # SQLite nie ma blokad wierszy (FOR UPDATE to no-op), wiec ten test dowodzi niezmiennika
    # "jedna zmiana hasla na raz" w danych, a samej blokady dowodzi test kompilacji powyzej.
    user = await _rejestruj(maker)
    await _nowa_sesja(maker, user)
    pierwszy = await _popros_i_odbierz(maker, skrzynka)
    drugi = await _popros_i_odbierz(maker, skrzynka)

    wyniki = await asyncio.wait_for(
        asyncio.gather(
            _uzyj(maker, pierwszy, NOWE_HASLO),
            _uzyj(maker, drugi, INNE_NOWE_HASLO),
            return_exceptions=True,
        ),
        timeout=60,
    )

    bledy = [w for w in wyniki if isinstance(w, BaseException)]
    assert len(bledy) == 1 and isinstance(bledy[0], BrakDostepu), wyniki
    assert sorted(
        [
            await _haslo_dziala(maker, JAN, NOWE_HASLO),
            await _haslo_dziala(maker, JAN, INNE_NOWE_HASLO),
        ]
    ) == [False, True]
    assert all(w.uzyty_at is not None for w in await _tokeny(maker))
    assert all(w.uniewazniona_at is not None for w in await _sesje_uzytkownika(maker, user.id))


# --- wyscig -------------------------------------------------------------------------------------


async def test_dwa_rownolegle_uzycia_tego_samego_tokenu_daja_dokladnie_jeden_sukces(
    maker, skrzynka
):
    user = await _rejestruj(maker)
    await _nowa_sesja(maker, user)
    token = await _popros_i_odbierz(maker, skrzynka)

    wyniki = await asyncio.wait_for(
        asyncio.gather(
            _uzyj(maker, token, NOWE_HASLO),
            _uzyj(maker, token, INNE_NOWE_HASLO),
            return_exceptions=True,
        ),
        timeout=60,
    )

    bledy = [w for w in wyniki if isinstance(w, BaseException)]
    assert len(bledy) == 1 and isinstance(bledy[0], BrakDostepu), wyniki
    dziala = [
        await _haslo_dziala(maker, JAN, NOWE_HASLO),
        await _haslo_dziala(maker, JAN, INNE_NOWE_HASLO),
    ]
    assert sorted(dziala) == [False, True]  # haslo jednego zwyciezcy, nie mieszanka
    assert not await _haslo_dziala(maker, JAN, HASLO)
    assert all(w.uniewazniona_at is not None for w in await _sesje_uzytkownika(maker, user.id))


# --- ustaw_haslo i model ------------------------------------------------------------------------


async def test_ustaw_haslo_niczego_nie_zapisuje_dopoki_wolajacy_nie_zatwierdzi(maker):
    user = await _rejestruj(maker)

    async with maker() as s:
        zapisany = await s.get(User, user.id)
        await konta.ustaw_haslo(s, zapisany, NOWE_HASLO)
        await s.rollback()

    assert await _haslo_dziala(maker, JAN, HASLO)
    assert not await _haslo_dziala(maker, JAN, NOWE_HASLO)


async def test_ustaw_haslo_po_zatwierdzeniu_zmienia_skrot_i_znacznik_czasu(maker):
    user = await _rejestruj(maker)

    async with maker() as s:
        zapisany = await s.get(User, user.id)
        await konta.ustaw_haslo(s, zapisany, NOWE_HASLO)
        await s.commit()

    assert await _haslo_dziala(maker, JAN, NOWE_HASLO)
    assert not await _haslo_dziala(maker, JAN, HASLO)
    async with maker() as s:
        assert _utc((await s.get(User, user.id)).haslo_zmienione_at) == T0


async def test_ustaw_haslo_odrzuca_za_krotkie_a_osmioznakowe_przyjmuje(maker):
    user = await _rejestruj(maker)

    async with maker() as s:
        zapisany = await s.get(User, user.id)
        with pytest.raises(StanNiemozliwy) as e:
            await konta.ustaw_haslo(s, zapisany, "1234567")
        assert str(e.value) == KOMUNIKAT_ZA_KROTKIE
        await konta.ustaw_haslo(s, zapisany, "12345678")
        await s.commit()

    assert await _haslo_dziala(maker, JAN, "12345678")


async def test_ustaw_haslo_odrzuca_haslo_ze_znakiem_ktorego_nie_da_sie_zapisac_a_krotkie_z_takim_znakiem_jako_za_krotkie(
    maker,
):
    user = await _rejestruj(maker)

    async with maker() as s:
        zapisany = await s.get(User, user.id)
        with pytest.raises(StanNiemozliwy) as niekodowalne:
            await konta.ustaw_haslo(s, zapisany, SUROGAT)
        with pytest.raises(StanNiemozliwy) as krotkie:
            await konta.ustaw_haslo(s, zapisany, "\ud800abc")

    assert str(niekodowalne.value) == KOMUNIKAT_NIEKODOWALNE
    assert str(krotkie.value) == KOMUNIKAT_ZA_KROTKIE
    assert await _haslo_dziala(maker, JAN, HASLO)


async def test_dwa_tokeny_o_tym_samym_skrocie_nie_moga_istniec_obok_siebie(maker):
    user = await _rejestruj(maker)
    skrot = _sha("jakis-token")

    async with maker() as s:
        s.add(TokenResetu(user_id=user.id, skrot=skrot, wygasa_at=T0 + timedelta(hours=1)))
        s.add(TokenResetu(user_id=user.id, skrot=skrot, wygasa_at=T0 + timedelta(hours=1)))
        with pytest.raises(IntegrityError):
            await s.commit()


# --- HTTP ---------------------------------------------------------------------------------------


@pytest.fixture
async def http(maker):
    async def _sesja_testowa():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _sesja_testowa
    yield lambda: httpx.AsyncClient(transport=ASGITransport(app=app), base_url="https://test")
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture
def listy_smtp(monkeypatch) -> list:
    """Podmienia dopiero ostatni krok wysylki (SMTP): produkcyjna `poczta.wyslij` i jej
    walidacje zostaja, nic nie wychodzi do sieci."""
    wyslane: list = []

    def _atrapa(wiadomosc, host, do):
        wyslane.append((wiadomosc, host, do))

    monkeypatch.setattr(poczta, "_wyslij_blokujaco", _atrapa)
    return wyslane


def _token_z_listu(wiadomosc) -> str:
    return _token_z_tresci(wiadomosc.get_content())


async def _api_prosba(klient, email: str):
    return await klient.post("/api/konto/reset", json={"email": email})


async def _api_potwierdz(klient, token: str, haslo: str = NOWE_HASLO):
    return await klient.post(
        "/api/konto/reset/potwierdz", json={"token": token, "nowe_haslo": haslo}
    )


async def test_odpowiedz_na_prosbe_o_reset_jest_taka_sama_dla_istniejacego_i_nieistniejacego_adresu(
    maker, http, listy_smtp
):
    await _rejestruj(maker)
    async with http() as klient:
        istniejacy = await _api_prosba(klient, JAN)
        nieistniejacy = await _api_prosba(klient, NIKT)

    assert istniejacy.status_code == nieistniejacy.status_code == 202
    assert istniejacy.json() == {"komunikat": KOMUNIKAT_RESETU}
    assert istniejacy.json() == nieistniejacy.json()
    assert istniejacy.text == nieistniejacy.text
    assert JAN not in istniejacy.text
    # skutek rozni sie tylko w danych: jeden list i jeden token, tylko dla istniejacego
    assert [wyslany[2] for wyslany in listy_smtp] == [JAN]
    assert len(await _tokeny(maker)) == 1


async def test_odpowiedz_na_prosbe_o_reset_nie_zawiera_tokenu(maker, http, listy_smtp):
    await _rejestruj(maker)
    async with http() as klient:
        odp = await _api_prosba(klient, JAN)

    token = _token_z_listu(listy_smtp[0][0])
    assert token not in odp.text
    assert _sha(token) not in odp.text


async def test_prosba_i_potwierdzenie_dzialaja_bez_sesji_a_link_z_listu_dziala_raz(
    maker, http, listy_smtp
):
    await _rejestruj(maker)
    async with http() as klient:
        assert (await _api_prosba(klient, JAN)).status_code == 202
        token = _token_z_listu(listy_smtp[0][0])

        pierwsze = await _api_potwierdz(klient, token, NOWE_HASLO)
        drugie = await _api_potwierdz(klient, token, INNE_NOWE_HASLO)

    assert pierwsze.is_success
    assert drugie.status_code == 401
    assert drugie.json() == {"detail": KOMUNIKAT_TOKENU}
    assert await _haslo_dziala(maker, JAN, NOWE_HASLO)
    assert not await _haslo_dziala(maker, JAN, INNE_NOWE_HASLO)
    assert not await _haslo_dziala(maker, JAN, HASLO)


async def test_token_z_wyslanego_listu_nie_wystepuje_w_bazie(maker, http, listy_smtp):
    await _rejestruj(maker)
    async with http() as klient:
        await _api_prosba(klient, JAN)

    token = _token_z_listu(listy_smtp[0][0])
    assert [w.skrot for w in await _tokeny(maker)] == [_sha(token)]
    assert not [w for w in await _wszystkie_wartosci(maker) if token in w]


async def test_potwierdzenie_nieznanym_tokenem_daje_401_i_nie_zmienia_hasla(maker, http, listy_smtp):
    await _rejestruj(maker)
    async with http() as klient:
        odp = await _api_potwierdz(klient, "to-nie-jest-token")

    assert odp.status_code == 401
    assert odp.json() == {"detail": KOMUNIKAT_TOKENU}
    assert await _haslo_dziala(maker, JAN, HASLO)
    assert not await _haslo_dziala(maker, JAN, NOWE_HASLO)


async def test_potwierdzenie_za_krotkim_haslem_daje_409_z_powodem_i_nie_zuzywa_tokenu(
    maker, http, listy_smtp
):
    await _rejestruj(maker)
    async with http() as klient:
        await _api_prosba(klient, JAN)
        token = _token_z_listu(listy_smtp[0][0])

        odp = await _api_potwierdz(klient, token, "1234567")

    assert odp.status_code == 409
    assert odp.json() == {"detail": KOMUNIKAT_ZA_KROTKIE}
    assert (await _tokeny(maker))[0].uzyty_at is None
    assert await _haslo_dziala(maker, JAN, HASLO)


async def test_po_resecie_stara_sesja_przegladarki_traci_dostep_a_cudza_zostaje(
    maker, http, listy_smtp
):
    async with http() as przegladarka_jana, http() as przegladarka_ewy, http() as bez_sesji:
        assert (
            await przegladarka_jana.post(
                "/api/konto/rejestracja", json={"email": JAN, "haslo": HASLO}
            )
        ).status_code == 201
        assert (
            await przegladarka_ewy.post(
                "/api/konto/rejestracja", json={"email": EWA, "haslo": HASLO}
            )
        ).status_code == 201
        assert (await przegladarka_jana.get("/api/konto/ja")).status_code == 200

        await _api_prosba(bez_sesji, JAN)
        token = _token_z_listu(listy_smtp[0][0])
        assert (await _api_potwierdz(bez_sesji, token)).is_success

        assert (await przegladarka_jana.get("/api/konto/ja")).status_code == 401
        assert (await przegladarka_ewy.get("/api/konto/ja")).status_code == 200

    async with maker() as s:
        jan_id = (await s.execute(select(User.id).where(User.email == JAN))).scalar_one()
        ewa_id = (await s.execute(select(User.id).where(User.email == EWA))).scalar_one()
    assert all(w.uniewazniona_at is not None for w in await _sesje_uzytkownika(maker, jan_id))
    assert all(w.uniewazniona_at is None for w in await _sesje_uzytkownika(maker, ewa_id))


async def test_dwa_rownolegle_potwierdzenia_tego_samego_tokenu_daja_jeden_sukces(
    maker, http, listy_smtp
):
    await _rejestruj(maker)
    async with http() as a, http() as b:
        await _api_prosba(a, JAN)
        token = _token_z_listu(listy_smtp[0][0])

        odpowiedzi = await asyncio.wait_for(
            asyncio.gather(
                _api_potwierdz(a, token, NOWE_HASLO), _api_potwierdz(b, token, INNE_NOWE_HASLO)
            ),
            timeout=60,
        )

    assert sorted(o.status_code // 100 for o in odpowiedzi) == [2, 4]
    assert [o.status_code for o in odpowiedzi if not o.is_success] == [401]
    assert sorted(
        [
            await _haslo_dziala(maker, JAN, NOWE_HASLO),
            await _haslo_dziala(maker, JAN, INNE_NOWE_HASLO),
        ]
    ) == [False, True]


async def test_potwierdzenie_haslem_ze_znakiem_ktorego_nie_da_sie_zapisac_daje_409_i_nie_zuzywa_tokenu(
    maker, http, listy_smtp
):
    await _rejestruj(maker)
    async with http() as klient:
        await _api_prosba(klient, JAN)
        token = _token_z_listu(listy_smtp[0][0])

        # surowe ciało: klient httpx nie zakoduje samotnego surogatu, a serwer ma go dostać
        odp = await klient.post(
            "/api/konto/reset/potwierdz",
            content=('{"token": "%s", "nowe_haslo": "\\ud800abcdefgh"}' % token).encode(),
            headers={"content-type": "application/json"},
        )

    assert odp.status_code == 409
    assert odp.json() == {"detail": KOMUNIKAT_NIEKODOWALNE}
    assert (await _tokeny(maker))[0].uzyty_at is None
    assert await _haslo_dziala(maker, JAN, HASLO)


@pytest.mark.parametrize(("pole", "wartosc", "komunikat"), BRAKI_KONFIGURACJI)
async def test_prosba_przy_wylaczonym_resecie_daje_409_z_powodem_i_nic_nie_wysyla(
    maker, http, listy_smtp, monkeypatch, pole, wartosc, komunikat
):
    await _rejestruj(maker)
    monkeypatch.setattr(settings, pole, wartosc)
    async with http() as klient:
        istniejacy = await _api_prosba(klient, JAN)
        nieistniejacy = await _api_prosba(klient, NIKT)

    for odp in (istniejacy, nieistniejacy):
        assert odp.status_code == 409
        assert odp.json() == {"detail": komunikat}
    assert listy_smtp == []
    assert await _tokeny(maker) == []


# --- granice dlugosci pol ------------------------------------------------------------------------


@pytest.mark.parametrize(("dlugosc", "kod"), [(320, 202), (321, 422)])
async def test_adres_w_prosbie_o_reset_moze_miec_320_znakow_a_321_jest_odrzucony(
    maker, http, listy_smtp, dlugosc, kod
):
    adres = "a" * (dlugosc - len("@x.pl")) + "@x.pl"
    assert len(adres) == dlugosc
    async with http() as klient:
        odp = await _api_prosba(klient, adres)

    assert odp.status_code == kod
    assert listy_smtp == []


@pytest.mark.parametrize(("dlugosc", "kod"), [(200, 401), (201, 422)])
async def test_token_w_potwierdzeniu_moze_miec_200_znakow_a_201_jest_odrzucony(
    maker, http, listy_smtp, dlugosc, kod
):
    await _rejestruj(maker)
    async with http() as klient:
        odp = await _api_potwierdz(klient, "t" * dlugosc)

    assert odp.status_code == kod
    assert await _haslo_dziala(maker, JAN, HASLO)


async def test_nowe_haslo_o_256_znakach_jest_przyjete_a_257_odrzucone_bez_zuzycia_tokenu(
    maker, http, listy_smtp
):
    await _rejestruj(maker)
    async with http() as klient:
        await _api_prosba(klient, JAN)
        token = _token_z_listu(listy_smtp[0][0])

        za_dlugie = await _api_potwierdz(klient, token, "h" * 257)
        assert za_dlugie.status_code == 422
        assert (await _tokeny(maker))[0].uzyty_at is None

        graniczne = await _api_potwierdz(klient, token, "h" * 256)

    assert graniczne.is_success
    assert await _haslo_dziala(maker, JAN, "h" * 256)


async def test_konfiguracja_podaje_ze_reset_jest_wlaczony_gdy_jest_poczta_i_adres(
    http,
):
    async with http() as klient:
        odp = await klient.get("/api/konto/konfiguracja")

    assert odp.status_code == 200
    assert odp.json() == {"reset_hasla": True}


@pytest.mark.parametrize(("pole", "wartosc", "komunikat"), BRAKI_KONFIGURACJI)
async def test_konfiguracja_podaje_ze_reset_jest_wylaczony_bez_poczty_albo_adresu(
    http, monkeypatch, pole, wartosc, komunikat
):
    monkeypatch.setattr(settings, pole, wartosc)
    async with http() as klient:
        odp = await klient.get("/api/konto/konfiguracja")

    assert odp.status_code == 200
    assert odp.json() == {"reset_hasla": False}
