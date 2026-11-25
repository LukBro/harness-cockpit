"""Poczta kokpitu (HAR-91): stan konfiguracji SMTP, wysylka testowa do zalogowanego i tlumaczenie bledow.

Prawdziwy SMTP nigdy: `smtplib.SMTP` w module `cockpit.services.poczta` zastepuje atrapa, ktora
zapisuje, co kod z nia zrobil (polaczenie, starttls, login, wiadomosc). Skutki czytamy z atrapy,
nie tylko z kodu odpowiedzi HTTP. Hasla SMTP uzywamy jako znacznika: ma trafic do `login`, a nigdzie
indziej (ani do `detail`, ani do logow, takze gdy sama biblioteka wpisze je w tresc wyjatku).
"""
from __future__ import annotations

import logging
import smtplib
import socket
import ssl
from email.utils import getaddresses
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.api.app import app
from cockpit.api.ustawienia import TEMAT_TESTOWY, TRESC_TESTOWA
from cockpit.api.zaleznosci import NAGLOWEK_CSRF
from cockpit.db import session as db_session
from cockpit.db.models import Base, RolaWZespole, User
from cockpit.services import poczta
from cockpit.services.errors import BladPoczty
from cockpit.settings import Settings, settings
from sesja_testowa import zaloguj_klienta, zaloz_czlonka, zaloz_wlasciciela

HASLO_SMTP = "tajne-haslo-smtp"
HOST = "smtp.example.test"
NADAWCA = "kokpit@example.test"
LOGIN_SMTP = "ukryty-uzytkownik"
ADRES_TESTOWEJ = "/api/ustawienia/poczta/test"
ADRES_STANU = "/api/ustawienia/poczta"
ZDARZENIA_KLUCZOWE = ("polaczenie", "starttls", "login", "wysylka")
POWOD_BRAK_HOSTA = "poczta wyłączona: ustaw zmienną COCKPIT_SMTP_HOST"
POWOD_BRAK_NADAWCY = "poczta wyłączona: ustaw zmienną COCKPIT_SMTP_NADAWCA"
POWOD_BRAK_HASLA = "poczta wyłączona: ustaw zmienną COCKPIT_SMTP_HASLO"


# --- atrapa smtplib.SMTP -------------------------------------------------------------------------


class _Atrapa:
    """Rejestr wywolan atrapy; `rzuc` = {etap: wyjatek} wyzwala blad na danym etapie."""

    def __init__(self) -> None:
        self.polaczenia: list[SimpleNamespace] = []
        self.rzuc: dict[str, BaseException] = {}

    @property
    def liczba_polaczen(self) -> int:
        return len(self.polaczenia)

    @property
    def wiadomosci(self) -> list:
        return [w for p in self.polaczenia for w in p.wiadomosci]


@pytest.fixture
def atrapa_smtp(monkeypatch) -> _Atrapa:
    rejestr = _Atrapa()

    class AtrapaSMTP:
        def __init__(self, host="", port=0, local_hostname=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                     source_address=None):
            self.wpis = SimpleNamespace(
                host=host, port=port, timeout=timeout, zdarzenia=[], login=None,
                wiadomosci=[], zamkniete=False, koperty=[], kontekst=None)
            rejestr.polaczenia.append(self.wpis)
            self.wpis.zdarzenia.append("polaczenie")
            self._rzuc_jesli("polaczenie")
            host.encode("idna")   # jak create_connection: pusta etykieta (`a..b`) to UnicodeError

        def _rzuc_jesli(self, etap: str) -> None:
            if etap in rejestr.rzuc:
                raise rejestr.rzuc[etap]

        def __enter__(self):
            return self

        def __exit__(self, *wyjatek):
            self.wpis.zamkniete = True
            return False

        def ehlo(self, *a, **k):
            return (250, b"ok")

        def ehlo_or_helo_if_needed(self, *a, **k):
            return None

        def starttls(self, *a, context=None, **k):
            self.wpis.zdarzenia.append("starttls")
            self.wpis.kontekst = context
            self._rzuc_jesli("starttls")
            return (220, b"ok")

        def login(self, uzytkownik, haslo, *a, **k):
            self.wpis.zdarzenia.append("login")
            self.wpis.login = (uzytkownik, haslo)
            self._rzuc_jesli("login")
            f"{uzytkownik}\0{haslo}".encode("ascii")   # jak AUTH w smtplib: nie-ASCII to UnicodeEncodeError
            return (235, b"ok")

        def send_message(self, wiadomosc, from_addr=None, to_addrs=None, *a, **k):
            self.wpis.zdarzenia.append("wysylka")
            self.wpis.koperty.append(to_addrs)
            self._rzuc_jesli("wysylka")
            self.wpis.wiadomosci.append(wiadomosc)
            return {}

        def quit(self):
            self.wpis.zamkniete = True
            return (221, b"bye")

        def close(self):
            self.wpis.zamkniete = True

    monkeypatch.setattr("cockpit.services.poczta.smtplib.SMTP", AtrapaSMTP)
    return rejestr


@pytest.fixture
def poczta_skonfigurowana(monkeypatch) -> None:
    monkeypatch.setattr(settings, "smtp_host", HOST)
    monkeypatch.setattr(settings, "smtp_port", 2525)
    monkeypatch.setattr(settings, "smtp_uzytkownik", LOGIN_SMTP)
    monkeypatch.setattr(settings, "smtp_haslo", SecretStr(HASLO_SMTP))
    monkeypatch.setattr(settings, "smtp_nadawca", NADAWCA)
    monkeypatch.setattr(settings, "smtp_starttls", True)
    monkeypatch.setattr(settings, "smtp_timeout_s", 7)


def _zdarzenia_kluczowe(polaczenie) -> list[str]:
    return [z for z in polaczenie.zdarzenia if z in ZDARZENIA_KLUCZOWE]


# --- swiat: dwa zespoly, "pierwszy w bazie" to ktos z innego zespolu ------------------------------


@pytest.fixture
async def swiat(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'poczta.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    pierwszy, _ = await zaloz_wlasciciela(maker, "pierwszy@example.com", nazwa_zespolu="Piekarnia")
    wlasciciel, tenant = await zaloz_wlasciciela(maker, "wlasciciel@example.com", nazwa_zespolu="Browarek")
    administrator = await zaloz_czlonka(maker, tenant, RolaWZespole.admin, "admin@example.com")
    czlonek = await zaloz_czlonka(maker, tenant, RolaWZespole.member, "czlonek@example.com")
    obserwator = await zaloz_czlonka(maker, tenant, RolaWZespole.viewer, "obserwator@example.com")

    klienci: list[AsyncClient] = []

    async def klient_dla(user, csrf: bool = True) -> AsyncClient:
        k = AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1")
        klienci.append(k)
        await zaloguj_klienta(k, maker, user)
        if not csrf:
            del k.headers[NAGLOWEK_CSRF]
        return k

    def klient_bez_sesji() -> AsyncClient:
        k = AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1")
        klienci.append(k)
        return k

    yield SimpleNamespace(
        maker=maker, tenant=tenant, klient_dla=klient_dla, klient_bez_sesji=klient_bez_sesji, pierwszy=pierwszy,
        wlasciciel=wlasciciel, administrator=administrator, czlonek=czlonek, obserwator=obserwator)
    for k in klienci:
        await k.aclose()
    await engine.dispose()


# =================================================================================================
# 1. Pola konfiguracji
# =================================================================================================


def test_poczta_ma_domyslnie_port_587_starttls_i_limit_czasu_10_sekund_a_reszta_pusta():
    ustawienia = Settings(_env_file=None)

    assert (ustawienia.smtp_host, ustawienia.smtp_uzytkownik, ustawienia.smtp_haslo,
            ustawienia.smtp_nadawca, ustawienia.adres_publiczny) == (None, None, None, None, None)
    assert ustawienia.smtp_port == 587
    assert ustawienia.smtp_starttls is True
    assert ustawienia.smtp_timeout_s == 10


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_port_smtp_poza_zakresem_1_do_65535_jest_odrzucony(port):
    with pytest.raises(ValidationError, match="smtp_port"):
        Settings(_env_file=None, smtp_port=port)


@pytest.mark.parametrize("port", [1, 65535])
def test_port_smtp_na_granicach_zakresu_jest_przyjety(port):
    assert Settings(_env_file=None, smtp_port=port).smtp_port == port


def test_limit_czasu_smtp_ponizej_sekundy_jest_odrzucony_a_sekunda_przyjeta():
    with pytest.raises(ValidationError, match="smtp_timeout_s"):
        Settings(_env_file=None, smtp_timeout_s=0)
    assert Settings(_env_file=None, smtp_timeout_s=1).smtp_timeout_s == 1


def test_haslo_smtp_jest_sekretem_ukrytym_w_tekstowym_zapisie_ustawien():
    ustawienia = Settings(_env_file=None, smtp_haslo=HASLO_SMTP)

    assert isinstance(ustawienia.smtp_haslo, SecretStr)
    assert ustawienia.smtp_haslo.get_secret_value() == HASLO_SMTP
    assert HASLO_SMTP not in repr(ustawienia)
    assert HASLO_SMTP not in str(ustawienia)


def test_ustawienia_poczty_czytaja_sie_ze_zmiennych_srodowiska_z_prefiksem_cockpit(monkeypatch):
    monkeypatch.setenv("COCKPIT_SMTP_HOST", "smtp.srodowisko.test")
    monkeypatch.setenv("COCKPIT_SMTP_PORT", "2525")
    monkeypatch.setenv("COCKPIT_SMTP_HASLO", HASLO_SMTP)
    monkeypatch.setenv("COCKPIT_SMTP_NADAWCA", NADAWCA)
    monkeypatch.setenv("COCKPIT_ADRES_PUBLICZNY", "https://kokpit.example.test")

    ustawienia = Settings(_env_file=None)

    assert (ustawienia.smtp_host, ustawienia.smtp_port) == ("smtp.srodowisko.test", 2525)
    assert ustawienia.smtp_haslo.get_secret_value() == HASLO_SMTP
    assert ustawienia.smtp_nadawca == NADAWCA
    assert ustawienia.adres_publiczny == "https://kokpit.example.test"


# =================================================================================================
# 2. stan() i wyslij() na prawdziwym module
# =================================================================================================


def test_bez_hosta_poczta_nie_jest_skonfigurowana_a_powod_wskazuje_zmienna(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", None)

    stan = poczta.stan()

    assert stan.skonfigurowana is False
    assert stan.powod == POWOD_BRAK_HOSTA


def test_pusty_host_to_brak_konfiguracji_nie_skonfigurowana_poczta(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "")

    stan = poczta.stan()

    assert stan.skonfigurowana is False
    assert stan.powod == POWOD_BRAK_HOSTA


def test_host_bez_nadawcy_to_poczta_niedzialajaca_z_powodem_o_nadawcy(
    poczta_skonfigurowana, monkeypatch,
):
    monkeypatch.setattr(settings, "smtp_nadawca", None)

    stan = poczta.stan()

    assert stan.skonfigurowana is False
    assert stan.powod == POWOD_BRAK_NADAWCY


def test_uzytkownik_smtp_bez_hasla_to_poczta_niedzialajaca_z_powodem_o_hasle(
    poczta_skonfigurowana, monkeypatch,
):
    monkeypatch.setattr(settings, "smtp_haslo", None)

    stan = poczta.stan()

    assert stan.skonfigurowana is False
    assert stan.powod == POWOD_BRAK_HASLA


def test_haslo_smtp_bez_uzytkownika_nie_przeszkadza_poczta_jest_skonfigurowana(
    poczta_skonfigurowana, monkeypatch,
):
    monkeypatch.setattr(settings, "smtp_uzytkownik", None)

    assert poczta.stan() == poczta.StanPoczty(True, None)


@pytest.mark.parametrize(("pole", "powod"), [
    ("smtp_nadawca", POWOD_BRAK_NADAWCY), ("smtp_haslo", POWOD_BRAK_HASLA)],
    ids=["brak_nadawcy", "brak_hasla"])
async def test_wyslij_w_stanie_niedzialajacym_rzuca_powod_stanu_i_nie_laczy_sie_z_serwerem(
    poczta_skonfigurowana, atrapa_smtp, monkeypatch, pole, powod,
):
    monkeypatch.setattr(settings, pole, None)

    with pytest.raises(BladPoczty) as wyjatek:
        await poczta.wyslij("odbiorca@example.com", "T", "X")

    assert str(wyjatek.value) == powod
    assert atrapa_smtp.liczba_polaczen == 0


def test_z_hostem_i_nadawca_poczta_jest_skonfigurowana_i_nie_ma_powodu(poczta_skonfigurowana):
    stan = poczta.stan()

    assert stan.skonfigurowana is True
    assert stan.powod is None


async def test_wyslij_bez_konfiguracji_rzuca_blad_poczty_i_nie_laczy_sie_z_serwerem(
    monkeypatch, atrapa_smtp,
):
    monkeypatch.setattr(settings, "smtp_host", None)

    with pytest.raises(BladPoczty) as wyjatek:
        await poczta.wyslij("ktos@example.com", "Temat", "Tresc")

    assert str(wyjatek.value) == POWOD_BRAK_HOSTA
    assert atrapa_smtp.liczba_polaczen == 0


async def test_wyslij_buduje_wiadomosc_od_nadawcy_do_odbiorcy_z_tematem_i_trescia(
    poczta_skonfigurowana, atrapa_smtp,
):
    await poczta.wyslij("odbiorca@example.com", "Zażółć gęślą jaźń", "Treść listu")

    (wiadomosc,) = atrapa_smtp.wiadomosci
    assert wiadomosc["From"] == NADAWCA
    assert wiadomosc["To"] == "odbiorca@example.com"
    assert str(wiadomosc["Subject"]) == "Zażółć gęślą jaźń"
    assert "Treść listu" in wiadomosc.get_content()


async def test_wyslij_laczy_sie_z_hostem_portem_i_limitem_czasu_z_ustawien(
    poczta_skonfigurowana, atrapa_smtp,
):
    await poczta.wyslij("odbiorca@example.com", "T", "X")

    (polaczenie,) = atrapa_smtp.polaczenia
    assert (polaczenie.host, polaczenie.port, polaczenie.timeout) == (HOST, 2525, 7)


async def test_wyslij_z_starttls_szyfruje_polaczenie_przed_logowaniem_i_wysylka(
    poczta_skonfigurowana, atrapa_smtp,
):
    await poczta.wyslij("odbiorca@example.com", "T", "X")

    (polaczenie,) = atrapa_smtp.polaczenia
    assert _zdarzenia_kluczowe(polaczenie) == ["polaczenie", "starttls", "login", "wysylka"]


async def test_wyslij_bez_starttls_nie_wywoluje_starttls(
    poczta_skonfigurowana, atrapa_smtp, monkeypatch,
):
    monkeypatch.setattr(settings, "smtp_starttls", False)

    await poczta.wyslij("odbiorca@example.com", "T", "X")

    (polaczenie,) = atrapa_smtp.polaczenia
    assert _zdarzenia_kluczowe(polaczenie) == ["polaczenie", "login", "wysylka"]


async def test_wyslij_loguje_sie_uzytkownikiem_i_jawnym_haslem_z_sekretu(
    poczta_skonfigurowana, atrapa_smtp,
):
    await poczta.wyslij("odbiorca@example.com", "T", "X")

    (polaczenie,) = atrapa_smtp.polaczenia
    assert polaczenie.login == (LOGIN_SMTP, HASLO_SMTP)
    assert isinstance(polaczenie.login[1], str)


async def test_wyslij_bez_uzytkownika_smtp_wysyla_bez_logowania(
    poczta_skonfigurowana, atrapa_smtp, monkeypatch,
):
    monkeypatch.setattr(settings, "smtp_uzytkownik", None)
    monkeypatch.setattr(settings, "smtp_haslo", None)

    await poczta.wyslij("odbiorca@example.com", "T", "X")

    (polaczenie,) = atrapa_smtp.polaczenia
    assert _zdarzenia_kluczowe(polaczenie) == ["polaczenie", "starttls", "wysylka"]
    assert polaczenie.login is None
    assert len(atrapa_smtp.wiadomosci) == 1


async def test_wyslij_zamyka_polaczenie_takze_gdy_wysylka_sie_nie_udala(
    poczta_skonfigurowana, atrapa_smtp,
):
    atrapa_smtp.rzuc["wysylka"] = smtplib.SMTPException("serwer odmowil")

    with pytest.raises(BladPoczty):
        await poczta.wyslij("odbiorca@example.com", "T", "X")

    (polaczenie,) = atrapa_smtp.polaczenia
    assert polaczenie.zamkniete is True


async def test_wyslij_z_haslem_bez_uzytkownika_wysyla_list_bez_logowania(
    poczta_skonfigurowana, atrapa_smtp, monkeypatch,
):
    monkeypatch.setattr(settings, "smtp_uzytkownik", None)

    await poczta.wyslij("odbiorca@example.com", "T", "X")

    (polaczenie,) = atrapa_smtp.polaczenia
    assert polaczenie.login is None
    assert _zdarzenia_kluczowe(polaczenie) == ["polaczenie", "starttls", "wysylka"]
    assert len(atrapa_smtp.wiadomosci) == 1


async def test_koperta_listu_to_dokladnie_adres_odbiorcy_a_nie_naglowki(
    poczta_skonfigurowana, atrapa_smtp,
):
    await poczta.wyslij("odbiorca@example.com", "T", "X")

    (polaczenie,) = atrapa_smtp.polaczenia
    assert polaczenie.koperty == [["odbiorca@example.com"]]


async def test_starttls_wymaga_zweryfikowanego_certyfikatu_i_nazwy_hosta(
    poczta_skonfigurowana, atrapa_smtp,
):
    await poczta.wyslij("odbiorca@example.com", "T", "X")

    (polaczenie,) = atrapa_smtp.polaczenia
    assert polaczenie.kontekst.verify_mode == ssl.CERT_REQUIRED
    assert polaczenie.kontekst.check_hostname is True


# --- adresy i dane, ktore biblioteka odrzuca: osobny komunikat, bez listu ---------------------------

KOMUNIKAT_ODBIORCA = "adres odbiorcy listu jest nieprawidłowy"
KOMUNIKAT_NADAWCA = "adres nadawcy jest nieprawidłowy: sprawdź COCKPIT_SMTP_NADAWCA"
KOMUNIKAT_HOST = "adres serwera poczty jest nieprawidłowy: sprawdź COCKPIT_SMTP_HOST"
KOMUNIKAT_ASCII = (
    "dane logowania do poczty muszą być w ASCII: sprawdź COCKPIT_SMTP_UZYTKOWNIK i COCKPIT_SMTP_HASLO")
ADRES_Z_PRZECINKIEM = "a@example.com,b@example.com"
ADRES_Z_NOWA_LINIA = "a@example.com\nBcc: c@example.com"


@pytest.mark.parametrize("adres", [ADRES_Z_PRZECINKIEM, ADRES_Z_NOWA_LINIA],
                         ids=["przecinek", "nowa_linia"])
async def test_wyslij_na_nieprawidlowy_adres_odbiorcy_jest_odrzucone_przed_polaczeniem(
    poczta_skonfigurowana, atrapa_smtp, adres,
):
    with pytest.raises(BladPoczty) as wyjatek:
        await poczta.wyslij(adres, "T", "X")

    assert str(wyjatek.value) == KOMUNIKAT_ODBIORCA
    assert atrapa_smtp.liczba_polaczen == 0
    assert atrapa_smtp.wiadomosci == []


async def test_wyslij_z_nadawca_z_nowa_linia_jest_odrzucone_przed_polaczeniem(
    poczta_skonfigurowana, atrapa_smtp, monkeypatch,
):
    monkeypatch.setattr(settings, "smtp_nadawca", "kokpit@example.test\nBcc: x@example.test")

    with pytest.raises(BladPoczty) as wyjatek:
        await poczta.wyslij("odbiorca@example.com", "T", "X")

    assert str(wyjatek.value) == KOMUNIKAT_NADAWCA
    assert atrapa_smtp.liczba_polaczen == 0


async def test_wyslij_na_host_z_pusta_etykieta_daje_komunikat_o_hoscie_i_nie_wysyla_listu(
    poczta_skonfigurowana, atrapa_smtp, monkeypatch,
):
    monkeypatch.setattr(settings, "smtp_host", "smtp..example.test")

    with pytest.raises(BladPoczty) as wyjatek:
        await poczta.wyslij("odbiorca@example.com", "T", "X")

    assert str(wyjatek.value) == KOMUNIKAT_HOST
    assert atrapa_smtp.wiadomosci == []


async def test_wyslij_z_haslem_spoza_ascii_daje_komunikat_o_ascii_i_nie_wysyla_listu(
    poczta_skonfigurowana, atrapa_smtp, monkeypatch,
):
    monkeypatch.setattr(settings, "smtp_haslo", SecretStr("zażółć"))

    with pytest.raises(BladPoczty) as wyjatek:
        await poczta.wyslij("odbiorca@example.com", "T", "X")

    assert str(wyjatek.value) == KOMUNIKAT_ASCII
    (polaczenie,) = atrapa_smtp.polaczenia
    assert polaczenie.zamkniete is True
    assert atrapa_smtp.wiadomosci == []


# =================================================================================================
# 3. GET /api/ustawienia/poczta
# =================================================================================================


async def test_wlasciciel_widzi_ze_poczta_nie_jest_skonfigurowana_z_powodem(swiat, monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", None)
    klient = await swiat.klient_dla(swiat.wlasciciel)

    odp = await klient.get(ADRES_STANU)

    assert odp.status_code == 200
    assert odp.json()["skonfigurowana"] is False
    assert odp.json()["powod"] == POWOD_BRAK_HOSTA


async def test_wlasciciel_widzi_ze_poczta_jest_skonfigurowana_bez_powodu_i_bez_hasla(
    swiat, poczta_skonfigurowana,
):
    klient = await swiat.klient_dla(swiat.wlasciciel)

    odp = await klient.get(ADRES_STANU)

    assert odp.status_code == 200
    assert odp.json() == {"skonfigurowana": True, "powod": None}
    assert HASLO_SMTP not in odp.text


async def test_odczyt_stanu_poczty_nie_laczy_sie_z_serwerem_smtp(
    swiat, poczta_skonfigurowana, atrapa_smtp,
):
    klient = await swiat.klient_dla(swiat.wlasciciel)

    await klient.get(ADRES_STANU)

    assert atrapa_smtp.liczba_polaczen == 0


async def test_bez_sesji_stan_poczty_dostaje_401_takze_na_localhoscie(swiat):
    klient = swiat.klient_bez_sesji()
    assert klient.base_url.host == "127.0.0.1"

    assert (await klient.get(ADRES_STANU)).status_code == 401


# =================================================================================================
# 4. POST /api/ustawienia/poczta/test: adres, uprawnienia, CSRF
# =================================================================================================


async def test_wysylka_testowa_trafia_na_adres_zalogowanego_a_nie_pierwszego_uzytkownika_w_bazie(
    swiat, poczta_skonfigurowana, atrapa_smtp,
):
    klient = await swiat.klient_dla(swiat.wlasciciel)

    odp = await klient.post(ADRES_TESTOWEJ)

    assert odp.status_code == 200
    assert odp.json() == {"do": "wlasciciel@example.com"}
    (wiadomosc,) = atrapa_smtp.wiadomosci
    assert wiadomosc["To"] == "wlasciciel@example.com"
    assert swiat.pierwszy.email == "pierwszy@example.com"
    assert "pierwszy@example.com" not in str(wiadomosc)


async def test_administrator_wysyla_wiadomosc_testowa_na_swoj_adres(
    swiat, poczta_skonfigurowana, atrapa_smtp,
):
    klient = await swiat.klient_dla(swiat.administrator)

    odp = await klient.post(ADRES_TESTOWEJ)

    assert odp.status_code == 200
    assert [w["To"] for w in atrapa_smtp.wiadomosci] == ["admin@example.com"]


async def test_wiadomosc_testowa_ma_nadawce_z_ustawien_oraz_temat_i_tresc_testowe(
    swiat, poczta_skonfigurowana, atrapa_smtp,
):
    klient = await swiat.klient_dla(swiat.wlasciciel)

    await klient.post(ADRES_TESTOWEJ)

    (wiadomosc,) = atrapa_smtp.wiadomosci
    assert wiadomosc["From"] == NADAWCA
    assert str(wiadomosc["Subject"]) == TEMAT_TESTOWY
    assert wiadomosc.get_content().rstrip("\n") == TRESC_TESTOWA


async def test_koperta_wiadomosci_testowej_to_tylko_adres_zalogowanego(
    swiat, poczta_skonfigurowana, atrapa_smtp,
):
    klient = await swiat.klient_dla(swiat.wlasciciel)

    await klient.post(ADRES_TESTOWEJ)

    (polaczenie,) = atrapa_smtp.polaczenia
    assert polaczenie.koperty == [["wlasciciel@example.com"]]


@pytest.mark.parametrize("rola", ["czlonek", "obserwator"])
async def test_czlonek_i_obserwator_nie_wysylaja_wiadomosci_testowej_i_nic_nie_wychodzi(
    swiat, poczta_skonfigurowana, atrapa_smtp, rola,
):
    klient = await swiat.klient_dla(getattr(swiat, rola))

    odp = await klient.post(ADRES_TESTOWEJ)

    assert odp.status_code == 403
    assert atrapa_smtp.liczba_polaczen == 0
    assert atrapa_smtp.wiadomosci == []


async def test_bez_sesji_wysylka_testowa_dostaje_401_i_nic_nie_wychodzi(
    swiat, poczta_skonfigurowana, atrapa_smtp,
):
    klient = swiat.klient_bez_sesji()
    assert klient.base_url.host == "127.0.0.1"

    odp = await klient.post(ADRES_TESTOWEJ)

    assert odp.status_code == 401
    assert atrapa_smtp.liczba_polaczen == 0


@pytest.mark.parametrize(("naglowek", "komunikat"), [
    (None, "brak tokenu CSRF"), ("zly-token", "nieprawidłowy token CSRF")],
    ids=["brak_tokenu", "zly_token"])
async def test_wysylka_testowa_bez_poprawnego_tokenu_csrf_dostaje_403_i_nic_nie_wychodzi(
    swiat, poczta_skonfigurowana, atrapa_smtp, naglowek, komunikat,
):
    klient = await swiat.klient_dla(swiat.wlasciciel, csrf=naglowek is not None)
    if naglowek is not None:
        klient.headers[NAGLOWEK_CSRF] = naglowek

    odp = await klient.post(ADRES_TESTOWEJ)

    assert odp.status_code == 403
    assert odp.json() == {"detail": komunikat}
    assert atrapa_smtp.liczba_polaczen == 0


async def test_wysylka_testowa_bez_konfiguracji_odpowiada_bledem_a_serwer_nie_jest_wolany(
    swiat, atrapa_smtp, monkeypatch,
):
    monkeypatch.setattr(settings, "smtp_host", None)
    klient = await swiat.klient_dla(swiat.wlasciciel)

    odp = await klient.post(ADRES_TESTOWEJ)

    assert odp.status_code == 502
    assert odp.json()["detail"] == POWOD_BRAK_HOSTA
    assert atrapa_smtp.liczba_polaczen == 0


# =================================================================================================
# 5. Tlumaczenie bledow SMTP na 502: kazda galaz osobno, bez Traceback i bez hasla
# =================================================================================================

LOGGER_POCZTY = "cockpit.services.poczta"

# nazwa -> (etap awarii, wyjatek z haslem w tresci, dokladny `detail`, typ w logu)
AWARIE = {
    "autoryzacja": (
        "login", smtplib.SMTPAuthenticationError(535, f"odrzucone haslo {HASLO_SMTP}".encode()),
        "serwer poczty odrzucił logowanie: sprawdź COCKPIT_SMTP_UZYTKOWNIK i COCKPIT_SMTP_HASLO",
        "SMTPAuthenticationError"),
    "odmowa_polaczenia": (
        "polaczenie", ConnectionRefusedError(f"odmowa, haslo {HASLO_SMTP}"),
        "serwer poczty odmówił połączenia: sprawdź COCKPIT_SMTP_HOST i COCKPIT_SMTP_PORT",
        "ConnectionRefusedError"),
    "przekroczony_czas": (
        "polaczenie", TimeoutError(f"timed out, haslo {HASLO_SMTP}"),
        "serwer poczty nie odpowiedział w limicie czasu (COCKPIT_SMTP_TIMEOUT_S)",
        "TimeoutError"),
    "ogolny_blad_smtp": (
        "wysylka", smtplib.SMTPException(f"blad serwera, haslo {HASLO_SMTP}"),
        "serwer poczty odrzucił wysyłkę listu", "SMTPException"),
    "nieznany_host": (
        "polaczenie", socket.gaierror(-2, f"nieznany host, haslo {HASLO_SMTP}"),
        "nie udało się połączyć z serwerem poczty", "gaierror"),
    "ogolny_blad_sieci": (
        "polaczenie", OSError(f"siec padla, haslo {HASLO_SMTP}"),
        "nie udało się połączyć z serwerem poczty", "OSError"),
}


def _ostrzezenia_poczty(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == LOGGER_POCZTY and r.levelno == logging.WARNING]


def _sprawdz_brak_hasla_i_traceback(odp, caplog) -> None:
    assert HASLO_SMTP not in odp.text
    assert HASLO_SMTP not in caplog.text
    assert "Traceback" not in odp.text
    assert "Traceback" not in caplog.text


@pytest.mark.parametrize("nazwa", list(AWARIE))
async def test_awaria_smtp_daje_502_z_komunikatem_swojej_galezi_i_ostrzezeniem_z_nazwa_typu(
    swiat, poczta_skonfigurowana, atrapa_smtp, caplog, nazwa,
):
    etap, wyjatek, oczekiwany_detail, typ = AWARIE[nazwa]
    atrapa_smtp.rzuc[etap] = wyjatek
    caplog.set_level(logging.DEBUG)
    klient = await swiat.klient_dla(swiat.wlasciciel)

    odp = await klient.post(ADRES_TESTOWEJ)

    assert odp.status_code == 502
    assert odp.json() == {"detail": oczekiwany_detail}
    assert _ostrzezenia_poczty(caplog) == [f"wysylka poczty nie powiodla sie: {typ}"]
    _sprawdz_brak_hasla_i_traceback(odp, caplog)
    assert atrapa_smtp.liczba_polaczen == 1      # blad pochodzi z proby, nie z braku konfiguracji
    assert atrapa_smtp.wiadomosci == []


async def test_blad_poczty_nie_zostawia_otwartego_polaczenia_takze_przy_awarii_logowania(
    swiat, poczta_skonfigurowana, atrapa_smtp,
):
    atrapa_smtp.rzuc["login"] = AWARIE["autoryzacja"][1]
    klient = await swiat.klient_dla(swiat.wlasciciel)

    await klient.post(ADRES_TESTOWEJ)

    (polaczenie,) = atrapa_smtp.polaczenia
    assert polaczenie.zamkniete is True


# --- te same galezie, ktore biblioteka odrzuca sama, przez trase -----------------------------------


async def _post_z_kontem_o_adresie(swiat, adres: str):
    konto = await zaloz_czlonka(swiat.maker, swiat.tenant, RolaWZespole.admin, adres)
    klient = await swiat.klient_dla(konto)
    return await klient.post(ADRES_TESTOWEJ)


@pytest.mark.parametrize("adres", [ADRES_Z_PRZECINKIEM, ADRES_Z_NOWA_LINIA],
                         ids=["przecinek", "nowa_linia"])
async def test_konto_z_nieprawidlowym_adresem_dostaje_502_a_list_nie_wychodzi(
    swiat, poczta_skonfigurowana, atrapa_smtp, caplog, adres,
):
    odp = await _post_z_kontem_o_adresie(swiat, adres)

    assert odp.status_code == 502
    assert odp.json() == {"detail": KOMUNIKAT_ODBIORCA}
    assert atrapa_smtp.liczba_polaczen == 0
    assert atrapa_smtp.wiadomosci == []
    assert _ostrzezenia_poczty(caplog) == []


async def test_nadawca_z_nowa_linia_daje_przez_trase_502_bez_polaczenia(
    swiat, poczta_skonfigurowana, atrapa_smtp, monkeypatch,
):
    monkeypatch.setattr(settings, "smtp_nadawca", "kokpit@example.test\nBcc: x@example.test")
    klient = await swiat.klient_dla(swiat.wlasciciel)

    odp = await klient.post(ADRES_TESTOWEJ)

    assert odp.status_code == 502
    assert odp.json() == {"detail": KOMUNIKAT_NADAWCA}
    assert atrapa_smtp.liczba_polaczen == 0


async def test_host_z_pusta_etykieta_daje_przez_trase_502_z_komunikatem_o_hoscie(
    swiat, poczta_skonfigurowana, atrapa_smtp, monkeypatch, caplog,
):
    monkeypatch.setattr(settings, "smtp_host", "smtp..example.test")
    klient = await swiat.klient_dla(swiat.wlasciciel)

    odp = await klient.post(ADRES_TESTOWEJ)

    assert odp.status_code == 502
    assert odp.json() == {"detail": KOMUNIKAT_HOST}
    assert atrapa_smtp.wiadomosci == []
    assert _ostrzezenia_poczty(caplog) == []


async def test_haslo_spoza_ascii_daje_przez_trase_502_z_komunikatem_o_ascii_bez_hasla(
    swiat, poczta_skonfigurowana, atrapa_smtp, monkeypatch, caplog,
):
    monkeypatch.setattr(settings, "smtp_haslo", SecretStr("zażółć"))
    klient = await swiat.klient_dla(swiat.wlasciciel)

    odp = await klient.post(ADRES_TESTOWEJ)

    assert odp.status_code == 502
    assert odp.json() == {"detail": KOMUNIKAT_ASCII}
    assert "zażółć" not in odp.text
    assert "zażółć" not in caplog.text
    assert atrapa_smtp.wiadomosci == []


# --- walidator adresu zalogowanego: kazdy czlon warunku ma wejscie, na ktorym decyduje sam ---------
# Wejscia sprawdzone w REPL: kazde przechodzi pozostale czlony walidatora (opis przy kazdej grupie).

# Rozne separatory linii bez zadnej innej wady: strip() ich w srodku nie rusza, getaddresses oddaje
# tekst bez zmian. Decyduje tylko `len(do.splitlines()) != 1`. Uwaga: bez tego czlonu adres odrzuci
# jeszcze EmailMessage (ValueError przy `To`) z tym samym komunikatem, wiec odroznia go tylko
# bezposrednie wolanie walidatora (test nizej); wyslij() i trasa pilnuja skutku (brak polaczenia).
ADRESY_Z_SEPARATOREM_LINII = {
    "tabulacja_pionowa": "a\x0b@example.com",
    "wysuw_strony": "a\x0c@example.com",
    "separator_pliku": "a\x1c@example.com",
    "separator_grupy": "a\x1d@example.com",
    "separator_rekordu": "a\x1e@example.com",
    "nastepna_linia": "a\x85@example.com",
    "separator_linii_unicode": "a\u2028@example.com",
    "separator_akapitu_unicode": "a\u2029@example.com",
    "nowa_linia_w_cudzyslowie": '"a\nb"@example.com',
}
# Jeden wpis getaddresses, ale nie "czysty adres": nazwa wyswietlana, nawiasy, lista, grupa.
# Decyduje tylko `getaddresses([do]) == [("", do)]`: jedna linia, brak bialych znakow na brzegach,
# a `EmailMessage` przyjmuje je w `To` bez bledu (czyli bez tego czlonu list by wyszedl).
ADRESY_NIE_JEDEN_CZYSTY_ADRES = {
    "nazwa_wyswietlana": "Jan <ofiara@inny.test>",
    "adres_w_nawiasach": "<a@example.com>",
    "dwa_adresy": "a@b.test, c@d.test",
    "grupa": "grupa: b@inny.test;",
}
# Separator linii na koncu: jedna linia wg splitlines, getaddresses oddaje tekst bez zmian,
# `EmailMessage` go przyjmuje. Decyduje tylko `do != do.strip()`.
ADRESY_Z_BIALYM_ZNAKIEM_NA_BRZEGU = {
    "tabulacja_pionowa_na_koncu": "u@example.test\x0b",
    "nastepna_linia_na_koncu": "u@example.test\x85",
}
# Spacje na brzegu i sama spacja odrzucaja dwa czlony naraz (strip i getaddresses), wiec nie
# odroznia zadnego z nich; zostaja jako dodatkowa ochrona skutku, nie jako dowod na czlon.
ADRESY_ZE_SPACJA_NA_BRZEGU = {
    "spacja_na_koncu": "u@example.test ",
    "spacja_na_poczatku": " u@example.test",
    "sama_spacja": " ",
}
# Pusty adres: zero linii wg splitlines, a pozostale czlony go przepuszczaja
# (`"" == "".strip()`, `getaddresses([""]) == [("", "")]`). Decyduje tylko `!= 1` (nie `> 1`).
ADRESY_PUSTE = {"pusty": ""}
WSZYSTKIE_ODRZUCANE_ADRESY = {
    **ADRESY_PUSTE, **ADRESY_Z_SEPARATOREM_LINII, **ADRESY_NIE_JEDEN_CZYSTY_ADRES,
    **ADRESY_Z_BIALYM_ZNAKIEM_NA_BRZEGU, **ADRESY_ZE_SPACJA_NA_BRZEGU,
}


@pytest.mark.parametrize("adres", list(ADRESY_Z_SEPARATOREM_LINII.values()),
                         ids=list(ADRESY_Z_SEPARATOREM_LINII))
def test_adres_z_separatorem_linii_bez_innej_wady_nie_jest_poprawnym_adresem_odbiorcy(adres):
    assert len(adres.splitlines()) == 2                    # wejscie rzeczywiscie ma dwie linie
    assert adres == adres.strip()                          # i nie ma brzegowych bialych znakow
    assert getaddresses([adres]) == [("", adres)]          # getaddresses go przepuszcza

    assert poczta._adres_zalogowanego_poprawny(adres) is False


@pytest.mark.parametrize("adres", list(ADRESY_NIE_JEDEN_CZYSTY_ADRES.values()),
                         ids=list(ADRESY_NIE_JEDEN_CZYSTY_ADRES))
def test_adres_z_nazwa_nawiasami_lista_albo_grupa_nie_jest_poprawnym_adresem_odbiorcy(adres):
    assert len(adres.splitlines()) == 1 and adres == adres.strip()   # pozostale czlony przepuszczaja

    assert poczta._adres_zalogowanego_poprawny(adres) is False


@pytest.mark.parametrize("adres", list(ADRESY_Z_BIALYM_ZNAKIEM_NA_BRZEGU.values()),
                         ids=list(ADRESY_Z_BIALYM_ZNAKIEM_NA_BRZEGU))
def test_adres_z_separatorem_linii_na_brzegu_nie_jest_poprawnym_adresem_odbiorcy(adres):
    assert len(adres.splitlines()) == 1                    # splitlines go przepuszcza
    assert getaddresses([adres]) == [("", adres)]          # getaddresses tez

    assert poczta._adres_zalogowanego_poprawny(adres) is False


def test_pusty_adres_nie_jest_poprawnym_adresem_odbiorcy():
    assert "" == "".strip()                                # strip go przepuszcza
    assert getaddresses([""]) == [("", "")]                # getaddresses tez
    assert "".splitlines() == []                           # zero linii, nie wiele

    assert poczta._adres_zalogowanego_poprawny("") is False


def test_zwykly_adres_jest_poprawnym_adresem_odbiorcy():
    assert poczta._adres_zalogowanego_poprawny("u@example.test") is True


@pytest.mark.parametrize("adres", list(WSZYSTKIE_ODRZUCANE_ADRESY.values()),
                         ids=list(WSZYSTKIE_ODRZUCANE_ADRESY))
async def test_wyslij_na_adres_odrzucany_przez_walidator_nie_laczy_sie_z_serwerem_i_nie_wysyla_listu(
    poczta_skonfigurowana, atrapa_smtp, adres,
):
    with pytest.raises(BladPoczty) as wyjatek:
        await poczta.wyslij(adres, "T", "X")

    assert str(wyjatek.value) == KOMUNIKAT_ODBIORCA
    assert atrapa_smtp.liczba_polaczen == 0
    assert atrapa_smtp.wiadomosci == []


@pytest.mark.parametrize("adres", list(WSZYSTKIE_ODRZUCANE_ADRESY.values()),
                         ids=list(WSZYSTKIE_ODRZUCANE_ADRESY))
async def test_konto_z_adresem_odrzucanym_przez_walidator_dostaje_502_bez_listu_i_bez_ostrzezenia(
    swiat, poczta_skonfigurowana, atrapa_smtp, caplog, adres,
):
    caplog.set_level(logging.DEBUG)
    konto = await zaloz_czlonka(swiat.maker, swiat.tenant, RolaWZespole.admin, "konto@example.test")
    # Model User obcina biale znaki przy zapisie przez ORM (`@validates`), wiec adres z bialym znakiem
    # na brzegu trafia do bazy tylko zapisem SQL wprost - tak jak stare dane sprzed normalizacji.
    async with swiat.maker() as sesja:
        await sesja.execute(update(User).where(User.id == konto.id).values(email=adres))
        await sesja.commit()
    async with swiat.maker() as sesja:
        assert (await sesja.get(User, konto.id)).email == adres      # baza ma dokladnie ten adres
    klient = await swiat.klient_dla(konto)

    odp = await klient.post(ADRES_TESTOWEJ)

    assert odp.status_code == 502
    assert odp.json() == {"detail": KOMUNIKAT_ODBIORCA}
    assert atrapa_smtp.liczba_polaczen == 0
    assert atrapa_smtp.wiadomosci == []
    assert _ostrzezenia_poczty(caplog) == []


# --- adres nadawcy, ktory biblioteka odrzuca innym wyjatkiem niz ValueError -------------------------
# `kokpit@[example.test` i `Kokpit <kokpit@[example.test>` daja AttributeError (InvalidHeaderDefect
# bez 'all_defects'), ` :^+(`{` daje IndexError (pusty token). Test z nowa linia (ValueError) jest wyzej.

NADAWCY_ODRZUCANI_BIBLIOTEKA = {
    "nieszczelny_nawias_w_domenie": "kokpit@[example.test",
    "nazwa_i_nieszczelny_nawias": "Kokpit <kokpit@[example.test>",
    "same_znaki_specjalne": " :^+(`{",
}


@pytest.mark.parametrize("nadawca", list(NADAWCY_ODRZUCANI_BIBLIOTEKA.values()),
                         ids=list(NADAWCY_ODRZUCANI_BIBLIOTEKA))
async def test_wyslij_z_nadawca_odrzucanym_przez_biblioteke_daje_komunikat_o_nadawcy_bez_polaczenia(
    poczta_skonfigurowana, atrapa_smtp, monkeypatch, nadawca,
):
    monkeypatch.setattr(settings, "smtp_nadawca", nadawca)

    with pytest.raises(BladPoczty) as wyjatek:
        await poczta.wyslij("odbiorca@example.com", "T", "X")

    assert str(wyjatek.value) == KOMUNIKAT_NADAWCA
    assert atrapa_smtp.liczba_polaczen == 0
    assert atrapa_smtp.wiadomosci == []


@pytest.mark.parametrize("nadawca", list(NADAWCY_ODRZUCANI_BIBLIOTEKA.values()),
                         ids=list(NADAWCY_ODRZUCANI_BIBLIOTEKA))
async def test_nadawca_odrzucany_przez_biblioteke_daje_przez_trase_502_bez_listu_i_bez_ostrzezenia(
    swiat, poczta_skonfigurowana, atrapa_smtp, monkeypatch, caplog, nadawca,
):
    monkeypatch.setattr(settings, "smtp_nadawca", nadawca)
    caplog.set_level(logging.DEBUG)
    klient = await swiat.klient_dla(swiat.wlasciciel)

    odp = await klient.post(ADRES_TESTOWEJ)

    assert odp.status_code == 502
    assert odp.json() == {"detail": KOMUNIKAT_NADAWCA}
    assert atrapa_smtp.liczba_polaczen == 0
    assert atrapa_smtp.wiadomosci == []
    assert _ostrzezenia_poczty(caplog) == []

