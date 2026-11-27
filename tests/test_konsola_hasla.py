"""Ustawianie hasla z konsoli: `cockpit ustaw-haslo <email>` (HAR-111).

Sprawdzamy reguly: skad pochodzi haslo, kiedy odmawiamy i jakim kodem, ze po zmianie
wszystkie sesje uzytkownika przestaja dzialac i ze haslo nigdy nie trafia do wyjscia.
Skutki czytamy z bazy (SQLite w tmp_path) w NOWEJ sesji. Testy podkomendy sa synchroniczne:
`main` sam wola `asyncio.run`, wiec w testach `async def` padloby na dzialajacej petli.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from procesy import uruchom_i_zbierz
from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import cockpit.__main__ as kokpit_main
from cockpit.db.models import Base, Sesja, User
from cockpit.services import konsola_hasla, konta, sesje
from cockpit.services.errors import StanNiemozliwy
from cockpit.settings import settings
from runner import maskowanie

STARE = "Stare-Hasl0-Alfa"
NOWE = "Nowe-Hasl0-Beta-77"
INNE = "Inne-Hasl0-Gamma-88"
JAN = "jan@przyklad.pl"
EWA = "ewa@przyklad.pl"
ZMIENNA = "COCKPIT_NOWE_HASLO"
_HASHER = PasswordHasher()
KORZEN = Path(__file__).resolve().parents[1]

KOMUNIKAT_SUKCES = "hasło ustawione, wszystkie sesje użytkownika unieważnione\n"
KOMUNIKAT_ROZNE = "hasła się różnią; nic nie zmieniono\n"
KOMUNIKAT_KROTKIE = "hasło odrzucone: hasło musi mieć co najmniej 8 znaków\n"
KOMUNIKAT_BRAK_ZRODLA = "brak terminala: podaj nowe hasło w zmiennej COCKPIT_NOWE_HASLO\n"
KOMUNIKAT_NIEZNANY = "nie ma użytkownika o takim adresie\n"
KOMUNIKAT_BAZA = (
    "nie można połączyć się z bazą albo zapytanie się nie powiodło; nic nie zmieniono\n"
)
KOMUNIKAT_PRZERWANO = "przerwano, nic nie zmieniono\n"
KOMUNIKAT_ZNAKI = "hasło zawiera znaki spoza kodowania terminala; nic nie zmieniono\n"
KOMUNIKAT_ARGUMENTY = (
    "nieznane lub błędne argumenty; hasła nie podaje się w poleceniu: "
    "terminal albo COCKPIT_NOWE_HASLO\n"
)
KOMUNIKAT_ADRES = "niepoprawny adres bazy w COCKPIT_DATABASE_URL; nic nie zmieniono\n"


class _Terminal:
    def __init__(self, jest: bool) -> None:
        self._jest = jest

    def isatty(self) -> bool:
        return self._jest


@pytest.fixture
def maker(tmp_path):
    # NullPool: kazde asyncio.run (test i main) otwiera wlasne polaczenie na swojej petli
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'konsola.db'}", poolclass=NullPool
    )

    async def schemat():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(schemat())
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _czyste_srodowisko(monkeypatch):
    monkeypatch.delenv(ZMIENNA, raising=False)
    # rejestr sekretow jest globalny: test nie moze zostawic hasla dla innych testow
    monkeypatch.setattr(maskowanie, "_SEKRETY", set())
    monkeypatch.setattr(
        kokpit_main.uvicorn, "run", lambda *a, **k: pytest.fail("podkomenda wystartowala serwer")
    )
    # po przekroczeniu limitu polecenie podmienia globalny hook watkow: test go przywraca
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)


def _terminal(monkeypatch, jest: bool) -> None:
    monkeypatch.setattr(sys, "stdin", _Terminal(jest))


def _getpass(monkeypatch, *odpowiedzi: str) -> list[str]:
    zapytania: list[str] = []
    kolejka = list(odpowiedzi)

    def falszywy(prompt: str = "Password: ", *a, **k) -> str:
        zapytania.append(prompt)
        if not kolejka:
            raise AssertionError("getpass zapytany wiecej razy niz przewidziano")
        return kolejka.pop(0)

    monkeypatch.setattr(kokpit_main.getpass, "getpass", falszywy)
    return zapytania


def _bez_getpass(monkeypatch) -> None:
    def zabronione(*a, **k):
        raise AssertionError("getpass nie powinien byc wolany")

    monkeypatch.setattr(kokpit_main.getpass, "getpass", zabronione)


def _zaloz(maker, email=JAN, haslo=STARE, sesji=1):
    async def praca():
        async with maker() as s:
            user = await konta.zarejestruj(s, email, haslo)
        for _ in range(sesji):
            async with maker() as s:
                await sesje.utworz(s, user)
        return user.id

    return asyncio.run(praca())


def _stan(maker, user_id: str):
    async def praca():
        async with maker() as s:
            user = await s.get(User, user_id)
            wiersze = (
                await s.execute(select(Sesja).where(Sesja.user_id == user_id))
            ).scalars().all()
            return user.haslo_skrot, user.haslo_zmienione_at, [w.uniewazniona_at for w in wiersze]

    return asyncio.run(praca())


def _pasuje(skrot: str, haslo: str) -> bool:
    try:
        return _HASHER.verify(skrot, haslo)
    except VerifyMismatchError:
        return False


def _uruchom(maker, *argumenty: str) -> int:
    return kokpit_main.main(["ustaw-haslo", *argumenty], maker=maker)


# --- usluga: services/konsola_hasla.ustaw_haslo_z_konsoli -------------------------------------


async def test_usluga_dla_nieznanego_adresu_zglasza_brak_uzytkownika_i_nie_zmienia_cudzego_hasla(
    maker,
):
    async with maker() as s:
        user = await konta.zarejestruj(s, JAN, STARE)
    async with maker() as s:
        wynik = await konsola_hasla.ustaw_haslo_z_konsoli(s, EWA, NOWE)
    async with maker() as s:
        zapisany = await s.get(User, user.id)
    assert wynik is False
    assert _pasuje(zapisany.haslo_skrot, STARE)


async def test_usluga_adres_z_uszkodzonym_znakiem_to_nieznany_uzytkownik(maker):
    async with maker() as s:
        wynik = await konsola_hasla.ustaw_haslo_z_konsoli(s, "ja\ud800n@przyklad.pl", NOWE)
    assert wynik is False


async def test_usluga_zmienia_haslo_i_zapisuje_czas_zmiany(maker):
    async with maker() as s:
        user = await konta.zarejestruj(s, JAN, STARE)
    async with maker() as s:
        przed = (await s.get(User, user.id)).haslo_zmienione_at
    async with maker() as s:
        wynik = await konsola_hasla.ustaw_haslo_z_konsoli(s, JAN, NOWE)
    async with maker() as s:
        zapisany = await s.get(User, user.id)
    assert wynik is True
    assert _pasuje(zapisany.haslo_skrot, NOWE)
    assert not _pasuje(zapisany.haslo_skrot, STARE)
    assert sesje.na_utc(zapisany.haslo_zmienione_at) > sesje.na_utc(przed)


@pytest.mark.parametrize("zapis", ["  JAN@Przyklad.PL ", "JAN@PRZYKLAD.PL", " jan@przyklad.pl"])
async def test_usluga_adres_jest_normalizowany_jak_przy_logowaniu(maker, zapis):
    async with maker() as s:
        user = await konta.zarejestruj(s, JAN, STARE)
    async with maker() as s:
        wynik = await konsola_hasla.ustaw_haslo_z_konsoli(s, zapis, NOWE)
    async with maker() as s:
        zapisany = await s.get(User, user.id)
    assert wynik is True
    assert _pasuje(zapisany.haslo_skrot, NOWE)


async def test_usluga_uniewaznia_wszystkie_sesje_uzytkownika_a_cudzych_nie_rusza(maker):
    async with maker() as s:
        jan = await konta.zarejestruj(s, JAN, STARE)
        ewa = await konta.zarejestruj(s, EWA, STARE)
    for user in (jan, jan, ewa):
        async with maker() as s:
            await sesje.utworz(s, user)
    async with maker() as s:
        await konsola_hasla.ustaw_haslo_z_konsoli(s, JAN, NOWE)
    async with maker() as s:
        wiersze = (await s.execute(select(Sesja))).scalars().all()
    janowe = [w.uniewazniona_at for w in wiersze if w.user_id == jan.id]
    ewy = [w.uniewazniona_at for w in wiersze if w.user_id == ewa.id]
    assert len(janowe) == 2
    assert all(t is not None for t in janowe)
    assert len(ewy) == 1
    assert all(t is None for t in ewy)


async def test_usluga_za_krotkie_haslo_to_stan_niemozliwy_i_nic_sie_nie_zmienia(maker):
    async with maker() as s:
        user = await konta.zarejestruj(s, JAN, STARE)
    async with maker() as s:
        await sesje.utworz(s, user)
    async with maker() as s:
        with pytest.raises(StanNiemozliwy) as blad:
            await konsola_hasla.ustaw_haslo_z_konsoli(s, JAN, "krotkie")
    assert type(blad.value) is StanNiemozliwy
    async with maker() as s:
        zapisany = await s.get(User, user.id)
        sesja = (await s.execute(select(Sesja))).scalar_one()
    assert _pasuje(zapisany.haslo_skrot, STARE)
    assert sesja.uniewazniona_at is None


async def test_usluga_zmiana_hasla_i_wylogowanie_to_jedna_transakcja(maker, monkeypatch):
    async with maker() as s:
        user = await konta.zarejestruj(s, JAN, STARE)
    async with maker() as s:
        await sesje.utworz(s, user)

    async def wywrotka(s, user_id):
        raise RuntimeError("awaria przy wylogowaniu")

    monkeypatch.setattr(sesje, "wyloguj_wszedzie", wywrotka)
    async with maker() as s:
        with pytest.raises(RuntimeError):
            await konsola_hasla.ustaw_haslo_z_konsoli(s, JAN, NOWE)
    async with maker() as s:
        zapisany = await s.get(User, user.id)
        sesja = (await s.execute(select(Sesja))).scalar_one()
    # haslo zmienione bez wylogowania zostawiloby stare sesje przy nowym hasle
    assert _pasuje(zapisany.haslo_skrot, STARE)
    assert sesja.uniewazniona_at is None


async def test_usluga_blokuje_wiersz_uzytkownika_na_czas_zmiany(maker):
    async with maker() as s:
        await konta.zarejestruj(s, JAN, STARE)
    zapytania: list = []

    def zapamietaj(stan):
        zapytania.append(stan.statement)

    async with maker() as s:
        event.listen(s.sync_session, "do_orm_execute", zapamietaj)
        try:
            await konsola_hasla.ustaw_haslo_z_konsoli(s, JAN, NOWE)
        finally:
            event.remove(s.sync_session, "do_orm_execute", zapamietaj)

    # bez przechwyconych zapytan nie ma dowodu na blokade
    assert zapytania
    # SQLite pomija FOR UPDATE, wiec blokade widac dopiero w SQL dla PostgreSQL
    sql = [str(z.compile(dialect=postgresql.dialect())) for z in zapytania]
    odczyty = [q for q in sql if q.startswith("SELECT") and 'FROM "user"' in q]
    assert len(odczyty) == 1
    # koncowka wyklucza FOR SHARE, SKIP LOCKED i NOWAIT
    assert odczyty[0].endswith("FOR UPDATE")


# --- podkomenda: ustaw-haslo, zrodlo hasla ----------------------------------------------------


def test_bez_terminala_haslo_pochodzi_ze_zmiennej_srodowiska(maker, monkeypatch):
    user_id = _zaloz(maker)
    _terminal(monkeypatch, False)
    _bez_getpass(monkeypatch)
    monkeypatch.setenv(ZMIENNA, NOWE)
    assert _uruchom(maker, JAN) == 0
    skrot, _, _ = _stan(maker, user_id)
    assert _pasuje(skrot, NOWE)


def test_na_terminalu_haslo_pytane_dwa_razy_i_zapisane(maker, monkeypatch):
    user_id = _zaloz(maker)
    _terminal(monkeypatch, True)
    zapytania = _getpass(monkeypatch, NOWE, NOWE)
    assert _uruchom(maker, JAN) == 0
    skrot, _, _ = _stan(maker, user_id)
    assert len(zapytania) == 2
    assert _pasuje(skrot, NOWE)


def test_udane_ustawienie_hasla_ze_zmiennej_pokazuje_tylko_potwierdzenie(
    maker, monkeypatch, capsys
):
    _zaloz(maker)
    _terminal(monkeypatch, False)
    monkeypatch.setenv(ZMIENNA, NOWE)
    assert _uruchom(maker, JAN) == 0
    wyjscie = capsys.readouterr()
    assert wyjscie.out == KOMUNIKAT_SUKCES
    assert wyjscie.err == ""


def test_udane_ustawienie_hasla_z_terminala_pokazuje_tylko_potwierdzenie(
    maker, monkeypatch, capsys
):
    _zaloz(maker)
    _terminal(monkeypatch, True)
    _getpass(monkeypatch, NOWE, NOWE)
    assert _uruchom(maker, JAN) == 0
    wyjscie = capsys.readouterr()
    assert wyjscie.out == KOMUNIKAT_SUKCES
    assert wyjscie.err == ""


def test_na_terminalu_zmienna_srodowiska_jest_ignorowana(maker, monkeypatch):
    user_id = _zaloz(maker)
    _terminal(monkeypatch, True)
    _getpass(monkeypatch, NOWE, NOWE)
    monkeypatch.setenv(ZMIENNA, INNE)
    assert _uruchom(maker, JAN) == 0
    skrot, _, _ = _stan(maker, user_id)
    assert _pasuje(skrot, NOWE)
    assert not _pasuje(skrot, INNE)


def test_dwa_rozne_hasla_z_terminala_koncza_sie_kodem_1_bez_zmiany(maker, monkeypatch, capsys):
    user_id = _zaloz(maker)
    _terminal(monkeypatch, True)
    _getpass(monkeypatch, NOWE, INNE)
    assert _uruchom(maker, JAN) == 1
    skrot, _, sesje_stan = _stan(maker, user_id)
    assert _pasuje(skrot, STARE)
    assert all(t is None for t in sesje_stan)
    wyjscie = capsys.readouterr()
    assert wyjscie.err == KOMUNIKAT_ROZNE
    assert wyjscie.out == ""


def test_za_krotkie_haslo_ze_zmiennej_to_kod_1_bez_zmiany(maker, monkeypatch, capsys):
    user_id = _zaloz(maker)
    _terminal(monkeypatch, False)
    monkeypatch.setenv(ZMIENNA, "krotkie")
    assert _uruchom(maker, JAN) == 1
    skrot, _, sesje_stan = _stan(maker, user_id)
    assert _pasuje(skrot, STARE)
    assert all(t is None for t in sesje_stan)
    wyjscie = capsys.readouterr()
    assert wyjscie.err == KOMUNIKAT_KROTKIE
    assert wyjscie.out == ""


def test_za_krotkie_haslo_z_terminala_to_kod_1_bez_zmiany(maker, monkeypatch, capsys):
    user_id = _zaloz(maker)
    _terminal(monkeypatch, True)
    _getpass(monkeypatch, "krotkie", "krotkie")
    assert _uruchom(maker, JAN) == 1
    skrot, _, _ = _stan(maker, user_id)
    assert _pasuje(skrot, STARE)
    wyjscie = capsys.readouterr()
    assert wyjscie.err == KOMUNIKAT_KROTKIE
    assert wyjscie.out == ""


def test_bez_terminala_i_bez_zmiennej_kod_2_z_komunikatem_i_bez_zmiany(
    maker, monkeypatch, capsys
):
    user_id = _zaloz(maker)
    _terminal(monkeypatch, False)
    _bez_getpass(monkeypatch)
    assert _uruchom(maker, JAN) == 2
    skrot, _, sesje_stan = _stan(maker, user_id)
    assert _pasuje(skrot, STARE)
    assert all(t is None for t in sesje_stan)
    wyjscie = capsys.readouterr()
    assert wyjscie.err == KOMUNIKAT_BRAK_ZRODLA
    assert wyjscie.out == ""


def test_pusta_zmienna_srodowiska_bez_terminala_to_kod_2_i_baza_bez_zmian(
    maker, monkeypatch, capsys
):
    user_id = _zaloz(maker)
    _terminal(monkeypatch, False)
    _bez_getpass(monkeypatch)
    monkeypatch.setenv(ZMIENNA, "")
    assert _uruchom(maker, JAN) == 2
    skrot, _, sesje_stan = _stan(maker, user_id)
    assert _pasuje(skrot, STARE)
    assert all(t is None for t in sesje_stan)
    wyjscie = capsys.readouterr()
    assert wyjscie.err == KOMUNIKAT_BRAK_ZRODLA
    assert wyjscie.out == ""


@pytest.mark.parametrize("terminal", [True, False], ids=["terminal", "bez_terminala"])
def test_nieznany_adres_to_kod_1_z_komunikatem_i_nikt_nie_zmienia_hasla(
    maker, monkeypatch, capsys, terminal
):
    user_id = _zaloz(maker, JAN)
    _terminal(monkeypatch, terminal)
    if terminal:
        _getpass(monkeypatch, NOWE, NOWE)
    else:
        monkeypatch.setenv(ZMIENNA, NOWE)
    assert _uruchom(maker, EWA) == 1
    skrot, _, sesje_stan = _stan(maker, user_id)
    assert _pasuje(skrot, STARE)
    assert all(t is None for t in sesje_stan)
    wyjscie = capsys.readouterr()
    assert wyjscie.err == KOMUNIKAT_NIEZNANY
    assert wyjscie.out == ""


# --- blad bazy i przerwanie: kod 1, stala tresc, bez tresci wyjatku ------------------------------


class _BazaPada:
    """Fabryka sesji, ktorej sesja nie otwiera sie: baza niedostepna."""

    def __init__(self, wyjatek: BaseException) -> None:
        self._wyjatek = wyjatek

    def __call__(self):
        return self

    async def __aenter__(self):
        raise self._wyjatek

    async def __aexit__(self, *a):
        return False


@pytest.mark.parametrize(
    "wyjatek",
    [
        OperationalError("SELECT tajny-skrot-w-zapytaniu", {"p": "tajny-parametr"}, Exception("x")),
        OSError("tajny-host-bazy: brak polaczenia"),
    ],
    ids=["blad_sterownika_bazy", "blad_systemu_plikow_lub_sieci"],
)
def test_awaria_bazy_to_kod_1_ze_stalym_komunikatem_bez_tresci_wyjatku(
    monkeypatch, capsys, wyjatek
):
    _terminal(monkeypatch, False)
    monkeypatch.setenv(ZMIENNA, NOWE)
    assert kokpit_main.main(["ustaw-haslo", JAN], maker=_BazaPada(wyjatek)) == 1
    wyjscie = capsys.readouterr()
    assert wyjscie.err == KOMUNIKAT_BAZA
    assert wyjscie.out == ""
    assert "tajny" not in wyjscie.err


@pytest.mark.parametrize("ktore", [1, 2], ids=["przy_pierwszym_pytaniu", "przy_powtorzeniu"])
def test_przerwanie_wpisywania_hasla_to_kod_1_baza_bez_zmian(maker, monkeypatch, capsys, ktore):
    user_id = _zaloz(maker)
    _terminal(monkeypatch, True)
    zapytania: list[str] = []

    def przerwany(prompt: str = "Password: ", *a, **k) -> str:
        zapytania.append(prompt)
        if len(zapytania) == ktore:
            raise EOFError
        return NOWE

    monkeypatch.setattr(kokpit_main.getpass, "getpass", przerwany)
    assert _uruchom(maker, JAN) == 1
    skrot, _, sesje_stan = _stan(maker, user_id)
    assert _pasuje(skrot, STARE)
    assert all(t is None for t in sesje_stan)
    wyjscie = capsys.readouterr()
    assert wyjscie.err == KOMUNIKAT_PRZERWANO
    assert wyjscie.out == ""


@pytest.mark.parametrize("ktore", [1, 2], ids=["przy_pierwszym_pytaniu", "przy_powtorzeniu"])
def test_haslo_ze_znakami_spoza_kodowania_terminala_to_kod_1_stala_tresc_baza_bez_zmian(
    maker, monkeypatch, capsys, ktore
):
    user_id = _zaloz(maker)
    _terminal(monkeypatch, True)
    zapytania: list[str] = []

    def zle_kodowanie(prompt: str = "Password: ", *a, **k) -> str:
        zapytania.append(prompt)
        if len(zapytania) == ktore:
            raise UnicodeDecodeError("utf-8", b"\xfftajne", 0, 1, "invalid start byte")
        return NOWE

    monkeypatch.setattr(kokpit_main.getpass, "getpass", zle_kodowanie)
    assert _uruchom(maker, JAN) == 1
    assert len(zapytania) == ktore
    skrot, _, sesje_stan = _stan(maker, user_id)
    assert _pasuje(skrot, STARE)
    assert all(t is None for t in sesje_stan)
    wyjscie = capsys.readouterr()
    assert wyjscie.err == KOMUNIKAT_ZNAKI
    assert wyjscie.out == ""
    assert "tajne" not in wyjscie.err
    assert "\\xff" not in wyjscie.err
    assert "xff" not in wyjscie.err


def test_po_ustawieniu_hasla_wszystkie_sesje_uzytkownika_sa_uniewaznione(maker, monkeypatch):
    user_id = _zaloz(maker, JAN, sesji=3)
    inny_id = _zaloz(maker, EWA, sesji=1)
    _terminal(monkeypatch, False)
    monkeypatch.setenv(ZMIENNA, NOWE)
    assert _uruchom(maker, JAN) == 0
    _, _, sesje_jana = _stan(maker, user_id)
    _, _, sesje_ewy = _stan(maker, inny_id)
    assert len(sesje_jana) == 3
    assert all(t is not None for t in sesje_jana)
    assert all(t is None for t in sesje_ewy)


def test_adres_w_podkomendzie_jest_normalizowany(maker, monkeypatch):
    user_id = _zaloz(maker)
    _terminal(monkeypatch, False)
    monkeypatch.setenv(ZMIENNA, NOWE)
    assert _uruchom(maker, "  JAN@Przyklad.PL ") == 0
    skrot, _, _ = _stan(maker, user_id)
    assert _pasuje(skrot, NOWE)


# --- haslo nie wycieka -----------------------------------------------------------------------


def _scenariusze(maker, monkeypatch):
    """Zwraca krotke (sekret, slownik nazwa -> (kod oczekiwany, uruchomienie)) dla sciezek
    udanych (kod 0) i blednych (kod 1)."""
    _zaloz(maker, JAN)
    sekret = "Tajne-Hasl0-Wyciek-99"

    def udane():
        _terminal(monkeypatch, False)
        monkeypatch.setenv(ZMIENNA, sekret)
        return _uruchom(maker, JAN)

    def nieznany():
        _terminal(monkeypatch, False)
        monkeypatch.setenv(ZMIENNA, sekret)
        return _uruchom(maker, EWA)

    def rozne():
        _terminal(monkeypatch, True)
        _getpass(monkeypatch, sekret, sekret + "x")
        return _uruchom(maker, JAN)

    def terminal_udane():
        _terminal(monkeypatch, True)
        _getpass(monkeypatch, sekret, sekret)
        return _uruchom(maker, JAN)

    return sekret, {
        "udane": (0, udane),
        "nieznany": (1, nieznany),
        "rozne": (1, rozne),
        "terminal": (0, terminal_udane),
    }


@pytest.mark.parametrize("nazwa", ["udane", "nieznany", "rozne", "terminal"])
def test_haslo_nie_trafia_na_ekran_ani_do_logow(maker, monkeypatch, capsys, caplog, nazwa):
    sekret, scenariusze = _scenariusze(maker, monkeypatch)
    with caplog.at_level(logging.DEBUG):
        oczekiwany, uruchom = scenariusze[nazwa]
        assert uruchom() == oczekiwany
    wyjscie = capsys.readouterr()
    assert sekret not in wyjscie.out
    assert sekret not in wyjscie.err
    assert sekret not in caplog.text


def test_za_krotkie_haslo_nie_trafia_do_komunikatu_bledu(maker, monkeypatch, capsys, caplog):
    _zaloz(maker)
    _terminal(monkeypatch, False)
    monkeypatch.setenv(ZMIENNA, "tajne7z")
    with caplog.at_level(logging.DEBUG):
        assert _uruchom(maker, JAN) == 1
    wyjscie = capsys.readouterr()
    assert "tajne7z" not in wyjscie.out + wyjscie.err + caplog.text


def test_ustawione_haslo_jest_zarejestrowane_do_maskowania(maker, monkeypatch):
    _zaloz(maker)
    _terminal(monkeypatch, False)
    monkeypatch.setenv(ZMIENNA, NOWE)
    assert _uruchom(maker, JAN) == 0
    assert NOWE in maskowanie._SEKRETY


def test_haslo_z_terminala_jest_zarejestrowane_do_maskowania(maker, monkeypatch):
    _zaloz(maker)
    _terminal(monkeypatch, True)
    _getpass(monkeypatch, NOWE, NOWE)
    assert _uruchom(maker, JAN) == 0
    assert NOWE in maskowanie._SEKRETY


def test_pierwsze_haslo_z_terminala_jest_maskowane_nawet_gdy_powtorzenie_sie_rozni(
    maker, monkeypatch
):
    _zaloz(maker)
    _terminal(monkeypatch, True)
    _getpass(monkeypatch, NOWE, INNE)
    assert _uruchom(maker, JAN) == 1
    assert NOWE in maskowanie._SEKRETY


def test_powtorzone_haslo_z_terminala_jest_maskowane_nawet_gdy_rozni_sie_od_pierwszego(
    maker, monkeypatch
):
    _zaloz(maker)
    _terminal(monkeypatch, True)
    _getpass(monkeypatch, NOWE, INNE)
    assert _uruchom(maker, JAN) == 1
    assert INNE in maskowanie._SEKRETY


def test_za_krotkie_haslo_ze_zmiennej_tez_jest_zarejestrowane_do_maskowania(
    maker, monkeypatch
):
    _zaloz(maker)
    _terminal(monkeypatch, False)
    monkeypatch.setenv(ZMIENNA, "krotkie")
    assert _uruchom(maker, JAN) == 1
    assert "krotkie" in maskowanie._SEKRETY


def test_podkomenda_instaluje_maskowanie_logow(maker, monkeypatch):
    _zaloz(maker)
    _terminal(monkeypatch, False)
    monkeypatch.setenv(ZMIENNA, NOWE)
    wywolania: list[int] = []
    monkeypatch.setattr(kokpit_main, "zainstaluj_maskowanie", lambda: wywolania.append(1))
    assert _uruchom(maker, JAN) == 0
    assert wywolania == [1]


# --- parser: haslo nigdy w argv ---------------------------------------------------------------


@pytest.mark.parametrize(
    "dodatkowe",
    [
        ["--password", INNE],
        ["--haslo", INNE],
        [f"--password={INNE}"],
        [INNE],
    ],
    ids=["opcja_password", "opcja_haslo", "opcja_z_rownosci", "nadmiarowy_argument"],
)
def test_haslo_w_argumentach_polecenia_jest_odrzucane_kodem_2_bez_echa(
    maker, monkeypatch, capsys, dodatkowe
):
    user_id = _zaloz(maker)
    _terminal(monkeypatch, False)
    monkeypatch.setenv(ZMIENNA, NOWE)
    with pytest.raises(SystemExit) as wyjscie:
        _uruchom(maker, JAN, *dodatkowe)
    assert wyjscie.value.code == 2
    skrot, _, _ = _stan(maker, user_id)
    assert _pasuje(skrot, STARE)
    ekran = capsys.readouterr()
    assert ekran.err == KOMUNIKAT_ARGUMENTY
    assert ekran.out == ""
    assert INNE not in ekran.err


def test_podkomenda_bez_adresu_jest_odrzucana_kodem_2(maker, monkeypatch, capsys):
    _terminal(monkeypatch, False)
    monkeypatch.setenv(ZMIENNA, NOWE)
    with pytest.raises(SystemExit) as wyjscie:
        kokpit_main.main(["ustaw-haslo"], maker=maker)
    assert wyjscie.value.code == 2
    ekran = capsys.readouterr()
    assert ekran.err == KOMUNIKAT_ARGUMENTY
    assert ekran.out == ""


# --- zamkniete wejscie ---------------------------------------------------------------------------


def test_przy_zamknietym_wejsciu_haslo_pochodzi_ze_zmiennej_srodowiska(maker, monkeypatch):
    user_id = _zaloz(maker)
    monkeypatch.setattr(sys, "stdin", None)
    _bez_getpass(monkeypatch)
    monkeypatch.setenv(ZMIENNA, NOWE)
    assert _uruchom(maker, JAN) == 0
    skrot, _, _ = _stan(maker, user_id)
    assert _pasuje(skrot, NOWE)


def test_przy_zamknietym_wejsciu_i_bez_zmiennej_kod_2_i_baza_bez_zmian(
    maker, monkeypatch, capsys
):
    user_id = _zaloz(maker)
    monkeypatch.setattr(sys, "stdin", None)
    _bez_getpass(monkeypatch)
    assert _uruchom(maker, JAN) == 2
    skrot, _, sesje_stan = _stan(maker, user_id)
    assert _pasuje(skrot, STARE)
    assert all(t is None for t in sesje_stan)
    wyjscie = capsys.readouterr()
    assert wyjscie.err == KOMUNIKAT_BRAK_ZRODLA
    assert wyjscie.out == ""


# --- baza kokpitu wskazana w ustawieniach (polecenie bez podanej bazy) ---------------------------


def _adres_bazy_testowej(tmp_path) -> str:
    """Adres tego samego pliku, ktorego uzywa fixture `maker`."""
    return f"sqlite+aiosqlite:///{tmp_path / 'konsola.db'}"


def _polecenie_z_baza_z_ustawien(monkeypatch, adres: str) -> int:
    monkeypatch.setattr(kokpit_main.settings, "database_url", adres)
    _terminal(monkeypatch, False)
    monkeypatch.setenv(ZMIENNA, NOWE)
    return kokpit_main.main(["ustaw-haslo", JAN])


def _tylko_komunikat_o_adresie(capsys) -> None:
    wyjscie = capsys.readouterr()
    assert wyjscie.err == KOMUNIKAT_ADRES
    assert wyjscie.out == ""
    assert NOWE not in wyjscie.err
    assert "tajne" not in wyjscie.err
    assert "Traceback" not in wyjscie.err


def test_polecenie_bez_wskazanej_bazy_zapisuje_haslo_w_bazie_kokpitu(
    maker, monkeypatch, tmp_path
):
    user_id = _zaloz(maker)
    assert _polecenie_z_baza_z_ustawien(monkeypatch, _adres_bazy_testowej(tmp_path)) == 0
    skrot, _, sesje_stan = _stan(maker, user_id)
    assert _pasuje(skrot, NOWE)
    assert all(t is not None for t in sesje_stan)


def test_brak_innej_czesci_instalacji_nie_udaje_zlego_adresu_bazy(monkeypatch, capsys):
    def brak_modulu(*a, **k):
        raise ModuleNotFoundError("No module named 'greenlet'", name="greenlet")

    monkeypatch.setattr(kokpit_main, "create_async_engine", brak_modulu)
    with pytest.raises(ModuleNotFoundError) as blad:
        _polecenie_z_baza_z_ustawien(monkeypatch, "sqlite+aiosqlite://")
    assert blad.value.name == "greenlet"
    assert capsys.readouterr().err == ""


# port 1 jest zamkniety: zaden adres z tych tabel nie dochodzi do dzialajacego serwera bazy
ADRES_PG = "postgresql+asyncpg://u:tajne@127.0.0.1:1/db"


@pytest.mark.parametrize(
    "wzor",
    [
        "nonsense",
        "postgresql+asyncpg://u:tajne@127.0.0.1:abc/db",
        "postgresql+pg8000://u:tajne@127.0.0.1:1/db",
        "mssql://u:tajne@127.0.0.1:1/db",
        "sqlite:///{katalog}/konsola.db",
        ADRES_PG + "?loop=x",
        ADRES_PG + "?loop=abc",
        "sqlite+aiosqlite:///{katalog}/konsola.db?loop=x",
        ADRES_PG + "?port=abc",
        ADRES_PG + "?prepared_statement_cache_size=1&prepared_statement_cache_size=2",
        ADRES_PG + "?prepared_statement_cache_size=abc",
        # tylko zamkniety port: bez odmowy ujemnego rozmiaru dzialajaca baza zawiesza polecenie
        ADRES_PG + "?prepared_statement_cache_size=-1",
        ADRES_PG + "?sslmode=require",
        ADRES_PG + "?command_timeout=abc",
        "postgresql+asyncpg://u:tajne@127.0.0.1:99999/db",
        "sqlite+aiosqlite://u:tajne@127.0.0.1/{katalog}/konsola.db",
    ],
    ids=[
        "to_nie_jest_adres",
        "port_nie_jest_liczba",
        "sterownik_postgresa_spoza_obslugiwanych",
        "rodzaj_bazy_spoza_obslugiwanych",
        "sqlite_bez_obslugi_asynchronicznej",
        "petla_zdarzen_w_adresie_postgresa",
        "petla_zdarzen_o_innej_wartosci",
        "petla_zdarzen_w_adresie_sqlite",
        "port_w_parametrze_nie_jest_liczba",
        "rozmiar_pamieci_zapytan_podany_dwa_razy",
        "rozmiar_pamieci_zapytan_nie_jest_liczba",
        "rozmiar_pamieci_zapytan_ujemny",
        "nieznany_parametr",
        "limit_czasu_nie_jest_liczba",
        "port_poza_zakresem",
        "sqlite_z_uzytkownikiem_i_hostem",
    ],
)
def test_zly_adres_bazy_to_komunikat_o_adresie_i_haslo_uzytkownika_bez_zmian(
    maker, monkeypatch, capsys, tmp_path, wzor
):
    user_id = _zaloz(maker)
    assert _polecenie_z_baza_z_ustawien(monkeypatch, wzor.format(katalog=tmp_path)) == 1
    _tylko_komunikat_o_adresie(capsys)
    skrot, _, sesje_stan = _stan(maker, user_id)
    assert _pasuje(skrot, STARE)
    assert all(t is None for t in sesje_stan)


@pytest.mark.parametrize(
    "adres",
    [ADRES_PG, ADRES_PG + "?prepared_statement_cache_size=0"],
    ids=["obslugiwany_adres_postgresa", "rozmiar_pamieci_zapytan_zero"],
)
def test_poprawny_adres_nieosiagalnej_bazy_to_komunikat_o_bazie_a_nie_o_adresie(
    maker, monkeypatch, capsys, adres
):
    user_id = _zaloz(maker)
    assert _polecenie_z_baza_z_ustawien(monkeypatch, adres) == 1
    wyjscie = capsys.readouterr()
    assert wyjscie.err == KOMUNIKAT_BAZA
    assert wyjscie.out == ""
    assert "tajne" not in wyjscie.err
    assert "Traceback" not in wyjscie.err
    skrot, _, sesje_stan = _stan(maker, user_id)
    assert _pasuje(skrot, STARE)
    assert all(t is None for t in sesje_stan)


@pytest.mark.parametrize(
    "wyjatek",
    [
        OperationalError("SELECT tajne-zapytanie", {}, Exception("x")),
        OSError("tajny-host-bazy: brak polaczenia"),
    ],
    ids=["baza_odmawia", "brak_sieci"],
)
def test_niedostepna_baza_przy_laczeniu_to_komunikat_o_bazie_a_nie_o_adresie(
    maker, monkeypatch, capsys, wyjatek
):
    user_id = _zaloz(maker)

    async def nie_laczy(self, *a, **k):
        raise wyjatek

    monkeypatch.setattr(AsyncSession, "connection", nie_laczy)
    _terminal(monkeypatch, False)
    monkeypatch.setenv(ZMIENNA, NOWE)
    assert _uruchom(maker, JAN) == 1
    wyjscie = capsys.readouterr()
    assert wyjscie.err == KOMUNIKAT_BAZA
    assert wyjscie.out == ""
    skrot, _, _ = _stan(maker, user_id)
    assert _pasuje(skrot, STARE)


@pytest.mark.parametrize(
    "typ", [TypeError, ValueError, OverflowError], ids=["zly_typ", "zla_wartosc", "przepelnienie"]
)
def test_blad_programu_po_polaczeniu_z_baza_nie_udaje_zlego_adresu(
    maker, monkeypatch, capsys, typ
):
    async def zepsuta(s, email, haslo):
        raise typ("blad w programie")

    monkeypatch.setattr(konsola_hasla, "ustaw_haslo_z_konsoli", zepsuta)
    _terminal(monkeypatch, False)
    monkeypatch.setenv(ZMIENNA, NOWE)
    with pytest.raises(typ):
        _uruchom(maker, JAN)
    assert capsys.readouterr().err == ""


def _podglad_polaczen_z_baza(monkeypatch):
    """Szpieg wokol prawdziwych funkcji: zapisuje wywolania i oddaje prawdziwy wynik."""
    budowy: list[tuple] = []
    silniki: list[AsyncEngine] = []
    fabryki: list[tuple] = []
    zamkniete: list[AsyncEngine] = []
    buduj = kokpit_main.create_async_engine
    fabryka = kokpit_main.async_sessionmaker
    zamknij = AsyncEngine.dispose

    def podglad_budowy(*a, **k):
        budowy.append((a, k))
        silniki.append(buduj(*a, **k))
        return silniki[-1]

    def podglad_fabryki(*a, **k):
        fabryki.append((a, k))
        return fabryka(*a, **k)

    async def podglad_zamkniecia(self, *a, **k):
        zamkniete.append(self)
        await zamknij(self, *a, **k)

    monkeypatch.setattr(kokpit_main, "create_async_engine", podglad_budowy)
    monkeypatch.setattr(kokpit_main, "async_sessionmaker", podglad_fabryki)
    monkeypatch.setattr(AsyncEngine, "dispose", podglad_zamkniecia)
    return budowy, silniki, fabryki, zamkniete


def test_polaczenie_z_baza_kokpitu_jest_zamykane_dokladnie_raz_po_udanej_zmianie(
    maker, monkeypatch, tmp_path
):
    _zaloz(maker)
    _, silniki, _, zamkniete = _podglad_polaczen_z_baza(monkeypatch)
    assert _polecenie_z_baza_z_ustawien(monkeypatch, _adres_bazy_testowej(tmp_path)) == 0
    assert len(silniki) == 1
    assert zamkniete == silniki


def test_polaczenie_z_baza_kokpitu_jest_zamykane_dokladnie_raz_po_nieudanym_laczeniu(
    monkeypatch,
):
    _, silniki, _, zamkniete = _podglad_polaczen_z_baza(monkeypatch)
    adres = "postgresql+asyncpg://u:p@127.0.0.1:1/db?sslmode=require"
    assert _polecenie_z_baza_z_ustawien(monkeypatch, adres) == 1
    assert len(silniki) == 1
    assert zamkniete == silniki


def test_baza_kokpitu_jest_otwierana_z_adresu_z_ustawien_a_dane_sa_czytelne_po_zapisie(
    maker, monkeypatch, tmp_path
):
    _zaloz(maker)
    budowy, silniki, fabryki, _ = _podglad_polaczen_z_baza(monkeypatch)
    adres = _adres_bazy_testowej(tmp_path)
    assert _polecenie_z_baza_z_ustawien(monkeypatch, adres) == 0
    assert budowy == [((adres,), {"future": True})]
    assert fabryki == [((silniki[0],), {"expire_on_commit": False})]


# --- sterownik bazy konczy prace przed koncem polecenia ------------------------------------------

KOMUNIKAT_STEROWNIK = (
    "sterownik bazy nie zakończył pracy w czasie; sprawdź, czy hasło zostało zmienione\n"
)
# bezpiecznik: dluzej zaden watek atrapy nie czeka, nawet gdy sprzatanie testu zawiedzie
_BEZPIECZNIK_S = 30.0


class _WatkiSterownika:
    """Atrapy watkow sterownika bazy. Czas pracy kazdej liczy sie od zamkniecia polaczenia
    z baza, wiec nie zalezy od tego, jak dlugo trwa sama zmiana hasla."""

    def __init__(self, monkeypatch) -> None:
        self._monkeypatch = monkeypatch
        self._zamkniete = threading.Event()
        self._zwolnij = threading.Event()
        self.watki: list[threading.Thread] = []
        zamknij = AsyncEngine.dispose

        async def zamknij_i_zglos(silnik, *a, **k):
            await zamknij(silnik, *a, **k)
            self._zamkniete.set()

        monkeypatch.setattr(AsyncEngine, "dispose", zamknij_i_zglos)

    def uruchom(
        self, czas_pracy: float, *, demon: bool = True, blad: Exception | None = None
    ) -> None:
        def praca():
            self._zamkniete.wait(timeout=_BEZPIECZNIK_S)
            self._zwolnij.wait(timeout=czas_pracy)
            if blad is not None:
                raise blad

        watek = threading.Thread(target=praca, daemon=demon)
        watek.start()
        self.watki.append(watek)

    def przy_otwieraniu_bazy(
        self, *czasy_pracy: float, demon: bool = True, blad: Exception | None = None
    ) -> None:
        """Watki startuja w podanej kolejnosci w trakcie polecenia, razem z silnikiem bazy."""
        buduj = kokpit_main.create_async_engine

        def buduj_z_watkami(*a, **k):
            for czas_pracy in czasy_pracy:
                self.uruchom(czas_pracy, demon=demon, blad=blad)
            return buduj(*a, **k)

        self._monkeypatch.setattr(kokpit_main, "create_async_engine", buduj_z_watkami)

    def przy_zamykaniu_polaczenia_rzuc(self, wyjatek: BaseException) -> None:
        """Polaczenie zostaje zamkniete (watki zaczynaja liczyc swoj czas pracy), po czym
        zamkniecie konczy sie podanym wyjatkiem."""
        zamknij_i_zglos = AsyncEngine.dispose

        async def zamknij_i_rzuc(silnik, *a, **k):
            await zamknij_i_zglos(silnik, *a, **k)
            raise wyjatek

        self._monkeypatch.setattr(AsyncEngine, "dispose", zamknij_i_rzuc)

    def posprzataj(self) -> None:
        self._zwolnij.set()
        self._zamkniete.set()
        for watek in self.watki:
            watek.join(timeout=5)
        assert not any(watek.is_alive() for watek in self.watki)


@pytest.fixture
def sterownik(monkeypatch):
    atrapy = _WatkiSterownika(monkeypatch)
    yield atrapy
    atrapy.posprzataj()


def _limit_czekania(monkeypatch, sekundy: float) -> None:
    monkeypatch.setattr(kokpit_main, "_LIMIT_CZEKANIA_NA_STEROWNIK_S", sekundy)


@pytest.mark.parametrize(
    ("adres", "komunikat"),
    [(ADRES_PG, KOMUNIKAT_BAZA), (ADRES_PG + "?sslmode=require", KOMUNIKAT_ADRES)],
    ids=["baza_nieosiagalna", "adres_odrzucony_przy_laczeniu"],
)
def test_po_nieudanym_polaczeniu_polecenie_czeka_az_sterownik_bazy_skonczy_prace(
    maker, monkeypatch, capsys, sterownik, adres, komunikat
):
    user_id = _zaloz(maker)
    sterownik.przy_otwieraniu_bazy(0.5)
    kod = _polecenie_z_baza_z_ustawien(monkeypatch, adres)
    pracuje_po_poleceniu = [watek.is_alive() for watek in sterownik.watki]
    assert pracuje_po_poleceniu == [False]
    assert kod == 1
    wyjscie = capsys.readouterr()
    assert wyjscie.err == komunikat
    assert wyjscie.out == ""
    assert "Traceback" not in wyjscie.err
    skrot, _, sesje_stan = _stan(maker, user_id)
    assert _pasuje(skrot, STARE)
    assert all(t is None for t in sesje_stan)


def test_po_udanej_zmianie_hasla_polecenie_czeka_az_sterownik_bazy_skonczy_prace(
    maker, monkeypatch, capsys, tmp_path, sterownik
):
    user_id = _zaloz(maker)
    sterownik.przy_otwieraniu_bazy(0.5)
    kod = _polecenie_z_baza_z_ustawien(monkeypatch, _adres_bazy_testowej(tmp_path))
    pracuje_po_poleceniu = [watek.is_alive() for watek in sterownik.watki]
    assert pracuje_po_poleceniu == [False]
    assert kod == 0
    wyjscie = capsys.readouterr()
    assert wyjscie.out == KOMUNIKAT_SUKCES
    assert wyjscie.err == ""
    skrot, _, _ = _stan(maker, user_id)
    assert _pasuje(skrot, NOWE)


def test_sterownik_pracujacy_dluzej_niz_limit_po_zapisie_kaze_sprawdzic_czy_haslo_zmienione(
    maker, monkeypatch, capsys, tmp_path, sterownik
):
    user_id = _zaloz(maker)
    _limit_czekania(monkeypatch, 0.2)
    sterownik.przy_otwieraniu_bazy(_BEZPIECZNIK_S)
    kod = _polecenie_z_baza_z_ustawien(monkeypatch, _adres_bazy_testowej(tmp_path))
    pracuje_po_poleceniu = [watek.is_alive() for watek in sterownik.watki]
    assert pracuje_po_poleceniu == [True]
    assert kod == 1
    wyjscie = capsys.readouterr()
    assert wyjscie.err == KOMUNIKAT_STEROWNIK
    assert wyjscie.out == ""
    assert "nic nie zmieniono" not in wyjscie.err
    assert "Traceback" not in wyjscie.err
    skrot, _, sesje_stan = _stan(maker, user_id)
    assert _pasuje(skrot, NOWE)
    assert all(t is not None for t in sesje_stan)


def test_sterownik_pracujacy_dluzej_niz_limit_zastepuje_komunikat_o_nieosiagalnej_bazie(
    monkeypatch, capsys, sterownik
):
    _limit_czekania(monkeypatch, 0.2)
    sterownik.przy_otwieraniu_bazy(_BEZPIECZNIK_S)
    kod = _polecenie_z_baza_z_ustawien(monkeypatch, ADRES_PG)
    pracuje_po_poleceniu = [watek.is_alive() for watek in sterownik.watki]
    assert pracuje_po_poleceniu == [True]
    assert kod == 1
    wyjscie = capsys.readouterr()
    assert wyjscie.err == KOMUNIKAT_STEROWNIK
    assert wyjscie.out == ""
    assert "nic nie zmieniono" not in wyjscie.err
    assert "Traceback" not in wyjscie.err


def test_watek_dzialajacy_w_tle_juz_przed_poleceniem_nie_opoznia_jego_wyniku(
    maker, monkeypatch, capsys, tmp_path, sterownik
):
    _zaloz(maker)
    _limit_czekania(monkeypatch, 1.0)
    sterownik.uruchom(_BEZPIECZNIK_S)
    kod = _polecenie_z_baza_z_ustawien(monkeypatch, _adres_bazy_testowej(tmp_path))
    pracuje_po_poleceniu = [watek.is_alive() for watek in sterownik.watki]
    assert pracuje_po_poleceniu == [True]
    assert kod == 0
    assert capsys.readouterr().err == ""


def test_nowy_watek_ktory_nie_jest_watkiem_sterownika_bazy_nie_opoznia_wyniku(
    maker, monkeypatch, capsys, tmp_path, sterownik
):
    # sterownik bazy pracuje w watkach demonicznych; pozostale polecenie pomija
    _zaloz(maker)
    _limit_czekania(monkeypatch, 1.0)
    sterownik.przy_otwieraniu_bazy(_BEZPIECZNIK_S, demon=False)
    kod = _polecenie_z_baza_z_ustawien(monkeypatch, _adres_bazy_testowej(tmp_path))
    pracuje_po_poleceniu = [watek.is_alive() for watek in sterownik.watki]
    assert pracuje_po_poleceniu == [True]
    assert kod == 0
    assert capsys.readouterr().err == ""


def test_limit_czekania_na_sterownik_bazy_jest_wspolny_dla_wszystkich_jego_watkow(
    maker, monkeypatch, capsys, tmp_path, sterownik
):
    _zaloz(maker)
    limit = 1.0
    _limit_czekania(monkeypatch, limit)
    # krotszy watek musi wystartowac pierwszy: czekanie idzie w kolejnosci startu, a przy
    # odwrotnej takze osobny limit na kazdy watek konczylby sie przekroczeniem
    sterownik.przy_otwieraniu_bazy(0.9 * limit, 1.5 * limit)
    assert _polecenie_z_baza_z_ustawien(monkeypatch, _adres_bazy_testowej(tmp_path)) == 1
    wyjscie = capsys.readouterr()
    assert wyjscie.err == KOMUNIKAT_STEROWNIK
    assert wyjscie.out == ""


def test_na_sterownik_bazy_polecenie_czeka_dopiero_po_zamknieciu_polaczenia(
    maker, monkeypatch, capsys, tmp_path, sterownik
):
    # watek konczy prace dopiero po zamknieciu polaczenia: czekanie na niego wczesniej
    # zawsze przekraczaloby limit
    user_id = _zaloz(maker)
    _limit_czekania(monkeypatch, 1.0)
    sterownik.przy_otwieraniu_bazy(0)
    assert _polecenie_z_baza_z_ustawien(monkeypatch, _adres_bazy_testowej(tmp_path)) == 0
    assert capsys.readouterr().err == ""
    skrot, _, _ = _stan(maker, user_id)
    assert _pasuje(skrot, NOWE)


def test_przerwanie_przy_zamykaniu_polaczenia_dociera_dopiero_gdy_sterownik_bazy_skonczyl_prace(
    maker, monkeypatch, tmp_path, sterownik
):
    user_id = _zaloz(maker)
    sterownik.przy_otwieraniu_bazy(2.0)
    # z prawdziwego zamkniecia polaczenia wychodza tylko wyjatki spoza Exception
    przerwanie = KeyboardInterrupt("przerwane przy zamykaniu polaczenia")
    sterownik.przy_zamykaniu_polaczenia_rzuc(przerwanie)
    with pytest.raises(KeyboardInterrupt) as blad:
        _polecenie_z_baza_z_ustawien(monkeypatch, _adres_bazy_testowej(tmp_path))
    pracuje_po_poleceniu = [watek.is_alive() for watek in sterownik.watki]
    assert pracuje_po_poleceniu == [False]
    assert blad.value is przerwanie
    skrot, _, _ = _stan(maker, user_id)
    assert _pasuje(skrot, NOWE)


# --- spozniony blad porzuconego watku sterownika ---------------------------------------------------


def test_blad_innego_watku_po_przekroczeniu_limitu_jest_nadal_zglaszany_a_porzuconego_pomijany(
    monkeypatch, capsys, sterownik
):
    zgloszone: list[threading.Thread] = []
    # przed poleceniem: to ten hook polecenie zastaje jako poprzedni
    monkeypatch.setattr(threading, "excepthook", lambda args: zgloszone.append(args.thread))
    _limit_czekania(monkeypatch, 0.2)
    sterownik.przy_otwieraniu_bazy(_BEZPIECZNIK_S, blad=RuntimeError("spozniony blad"))
    assert _polecenie_z_baza_z_ustawien(monkeypatch, ADRES_PG) == 1
    assert capsys.readouterr().err == KOMUNIKAT_STEROWNIK
    [porzucony] = sterownik.watki
    assert porzucony.is_alive()
    # zwalnia porzucony watek, ktory rzuca, i czeka na jego koniec
    sterownik.posprzataj()

    def awaria():
        raise RuntimeError("blad innego watku")

    imiennik = threading.Thread(target=awaria, name=porzucony.name, daemon=True)
    obcy = threading.Thread(target=awaria, name="inny-watek", daemon=True)
    for watek in (imiennik, obcy):
        watek.start()
        watek.join(timeout=5)
        assert not watek.is_alive()
    assert zgloszone == [imiennik, obcy]


def test_blad_porzuconego_watku_sterownika_jeszcze_przed_wynikiem_polecenia_nie_trafia_na_ekran(
    maker, monkeypatch, capsys, tmp_path, sterownik
):
    user_id = _zaloz(maker)
    zgloszone: list[threading.Thread] = []

    def poprzedni(args):
        zgloszone.append(args.thread)
        # domyslne zglaszanie: przepuszczony blad zostawia slad na stderr
        threading.__excepthook__(args)

    monkeypatch.setattr(threading, "excepthook", poprzedni)
    _limit_czekania(monkeypatch, 0.2)
    blad = RuntimeError("spozniony blad")
    sterownik.przy_otwieraniu_bazy(_BEZPIECZNIK_S, blad=blad)
    uruchom_petle = asyncio.run
    # (zyl po zamknieciu petli, rzucil juz wtedy, rzucil przed powrotem, zyje przed powrotem)
    przebieg: list[tuple[bool, bool, bool, bool]] = []

    def uruchom_i_zwolnij_watek(*a, **k):
        try:
            return uruchom_petle(*a, **k)
        finally:
            # petla zdarzen jest juz zamknieta, a polecenie jeszcze nie zwrocilo wyniku
            [porzucony] = sterownik.watki
            zyl = porzucony.is_alive()
            rzucil_za_wczesnie = blad.__traceback__ is not None
            sterownik.posprzataj()
            przebieg.append(
                (zyl, rzucil_za_wczesnie, blad.__traceback__ is not None, porzucony.is_alive())
            )

    # tylko na czas polecenia: odczyt z bazy po nim tez korzysta z asyncio.run
    with monkeypatch.context() as tylko_polecenie:
        tylko_polecenie.setattr(asyncio, "run", uruchom_i_zwolnij_watek)
        kod = _polecenie_z_baza_z_ustawien(monkeypatch, _adres_bazy_testowej(tmp_path))
    # bez tego test nie dowodzilby niczego: watek rzucil i skonczyl w wymuszonym momencie
    assert przebieg == [(True, False, True, False)]
    assert kod == 1
    wyjscie = capsys.readouterr()
    assert "Exception in thread" not in wyjscie.err
    assert "Traceback" not in wyjscie.err
    assert wyjscie.err == KOMUNIKAT_STEROWNIK
    assert wyjscie.out == ""
    assert zgloszone == []
    skrot, _, sesje_stan = _stan(maker, user_id)
    assert _pasuje(skrot, NOWE)
    assert all(t is not None for t in sesje_stan)


@pytest.mark.parametrize(
    ("polaczenie", "kod", "komunikat"),
    [("udane", 0, ""), ("nieudane", 1, KOMUNIKAT_BAZA)],
    ids=["udana_zmiana_hasla", "blad_polaczenia"],
)
def test_gdy_sterownik_konczy_prace_w_limicie_zglaszanie_bledow_watkow_zostaje_bez_zmian(
    maker, monkeypatch, capsys, tmp_path, sterownik, polaczenie, kod, komunikat
):
    _zaloz(maker)
    adres = _adres_bazy_testowej(tmp_path) if polaczenie == "udane" else ADRES_PG
    # nowy watek sterownika, ktory konczy prace w limicie, istnieje w obu przypadkach
    sterownik.przy_otwieraniu_bazy(0.1)
    przed = threading.excepthook
    assert _polecenie_z_baza_z_ustawien(monkeypatch, adres) == kod
    assert threading.excepthook is przed
    assert capsys.readouterr().err == komunikat
    assert [watek.is_alive() for watek in sterownik.watki] == [False]


# --- wyjatek w trakcie zmiany hasla, gdy sterownik nie konczy pracy --------------------------------


def _zmiana_hasla_rzuca(monkeypatch, wyjatek: BaseException) -> None:
    """Wyjatek leci po polaczeniu z baza, a przed zapisem nowego hasla."""

    async def zawodzi(s, email, haslo):
        raise wyjatek

    monkeypatch.setattr(konsola_hasla, "ustaw_haslo_z_konsoli", zawodzi)


def _sterownik_nie_konczy_pracy(monkeypatch, sterownik) -> None:
    _limit_czekania(monkeypatch, 0.2)
    sterownik.przy_otwieraniu_bazy(_BEZPIECZNIK_S)


@pytest.mark.parametrize(
    "wyjatek",
    [TypeError("blad w programie"), KeyboardInterrupt("przerwane przez czlowieka")],
    ids=["blad_programu", "przerwanie_przez_czlowieka"],
)
def test_blad_programu_i_przerwanie_przy_niekonczacym_pracy_sterowniku_dochodza_bez_zmian(
    maker, monkeypatch, capsys, tmp_path, sterownik, wyjatek
):
    user_id = _zaloz(maker)
    _sterownik_nie_konczy_pracy(monkeypatch, sterownik)
    _zmiana_hasla_rzuca(monkeypatch, wyjatek)
    with pytest.raises(type(wyjatek)) as blad:
        _polecenie_z_baza_z_ustawien(monkeypatch, _adres_bazy_testowej(tmp_path))
    assert blad.value is wyjatek
    # bez zywego watku test nie dowodzilby niczego
    assert [watek.is_alive() for watek in sterownik.watki] == [True]
    wyjscie = capsys.readouterr()
    assert wyjscie.err == ""
    assert wyjscie.out == ""
    skrot, _, sesje_stan = _stan(maker, user_id)
    assert _pasuje(skrot, STARE)
    assert all(t is None for t in sesje_stan)


# kazdy blad, ktory polecenie samo tlumaczy na komunikat; fabryki, bo wyjatek rzucony raz
# zachowuje slad i przyczyne
_BLEDY_Z_WLASNYM_KOMUNIKATEM = [
    pytest.param(lambda: StanNiemozliwy("odmowa"), id="haslo_odrzucone"),
    pytest.param(lambda: kokpit_main._NiepoprawnyAdresBazy(), id="niepoprawny_adres_bazy"),
    pytest.param(
        lambda: OperationalError("SELECT 1", {}, Exception("x")), id="baza_odmawia_zapytania"
    ),
    pytest.param(lambda: OSError("brak polaczenia"), id="brak_sieci"),
]


@pytest.mark.parametrize("fabryka", _BLEDY_Z_WLASNYM_KOMUNIKATEM)
def test_przy_niekonczacym_pracy_sterowniku_blad_polecenia_ustepuje_prosbie_o_sprawdzenie_hasla(
    maker, monkeypatch, capsys, tmp_path, sterownik, fabryka
):
    user_id = _zaloz(maker)
    _sterownik_nie_konczy_pracy(monkeypatch, sterownik)
    _zmiana_hasla_rzuca(monkeypatch, fabryka())
    kod = _polecenie_z_baza_z_ustawien(monkeypatch, _adres_bazy_testowej(tmp_path))
    assert [watek.is_alive() for watek in sterownik.watki] == [True]
    assert kod == 1
    wyjscie = capsys.readouterr()
    assert wyjscie.err == KOMUNIKAT_STEROWNIK
    assert wyjscie.out == ""
    skrot, _, sesje_stan = _stan(maker, user_id)
    assert _pasuje(skrot, STARE)
    assert all(t is None for t in sesje_stan)


@pytest.mark.parametrize("fabryka", _BLEDY_Z_WLASNYM_KOMUNIKATEM)
def test_wynik_niepewny_z_powodu_sterownika_zachowuje_pierwotny_blad_jako_przyczyne(
    maker, monkeypatch, tmp_path, sterownik, fabryka
):
    user_id = _zaloz(maker)
    _sterownik_nie_konczy_pracy(monkeypatch, sterownik)
    pierwotny = fabryka()
    _zmiana_hasla_rzuca(monkeypatch, pierwotny)
    monkeypatch.setattr(kokpit_main.settings, "database_url", _adres_bazy_testowej(tmp_path))
    with pytest.raises(kokpit_main._SterownikNieZakonczyl) as blad:
        asyncio.run(kokpit_main._zapisz(None, JAN, NOWE))
    assert blad.value.__cause__ is pierwotny
    assert [watek.is_alive() for watek in sterownik.watki] == [True]
    skrot, _, _ = _stan(maker, user_id)
    assert _pasuje(skrot, STARE)


# --- prawdziwy proces ----------------------------------------------------------------------------

# watek sterownika zyje dluzej niz limit i rzuca dopiero po komunikacie polecenia; skrypt
# czeka na jego koniec, wiec to, co watek wypisalby na stderr, na pewno tam trafi
_SKRYPT_SPOZNIONEGO_WATKU = """
import sys
import threading

import cockpit.__main__ as kokpit_main

po_komunikacie = threading.Event()
rzuca = threading.Event()
watki = []


def praca():
    if po_komunikacie.wait(timeout=30):
        rzuca.set()
        raise RuntimeError("spozniony-blad-sterownika")


buduj = kokpit_main.create_async_engine


def buduj_z_watkiem(*a, **k):
    watek = threading.Thread(target=praca, daemon=True)
    watek.start()
    watki.append(watek)
    return buduj(*a, **k)


kokpit_main.create_async_engine = buduj_z_watkiem
kokpit_main._LIMIT_CZEKANIA_NA_STEROWNIK_S = 0.2
kod = kokpit_main.main(["ustaw-haslo", sys.argv[1]])
po_komunikacie.set()
watki[0].join(timeout=10)
if rzuca.is_set() and not watki[0].is_alive():
    print("watek rzucil i skonczyl")
sys.exit(kod)
"""


def test_blad_porzuconego_watku_sterownika_nie_dopisuje_nic_do_komunikatu_polecenia():
    srodowisko = {
        **os.environ,
        "PYTHONPATH": str(KORZEN / "src"),
        "COCKPIT_DATABASE_URL": ADRES_PG,
        ZMIENNA: NOWE,
    }
    wynik = uruchom_i_zbierz(
        [sys.executable, "-c", _SKRYPT_SPOZNIONEGO_WATKU, JAN],
        capture_output=True,
        text=True,
        cwd=KORZEN,
        env=srodowisko,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    # bez tej linii test nie dowodzilby niczego: watek musial naprawde rzucic wyjatek
    assert wynik.stdout == "watek rzucil i skonczyl\n", wynik.stderr
    assert "Exception in thread" not in wynik.stderr
    assert "Traceback" not in wynik.stderr
    assert wynik.stderr == KOMUNIKAT_STEROWNIK
    assert wynik.returncode == 1


def test_polecenie_w_procesie_ze_zlym_adresem_bazy_konczy_sie_komunikatem_o_adresie():
    srodowisko = {
        **os.environ,
        "PYTHONPATH": str(KORZEN / "src"),
        "COCKPIT_DATABASE_URL": "nonsense",
        ZMIENNA: NOWE,
    }
    wynik = uruchom_i_zbierz(
        [sys.executable, "-m", "cockpit", "ustaw-haslo", JAN],
        capture_output=True,
        text=True,
        cwd=KORZEN,
        env=srodowisko,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    assert wynik.returncode == 1, wynik.stderr
    assert wynik.stderr == KOMUNIKAT_ADRES
    assert wynik.stdout == ""
    assert NOWE not in wynik.stderr


def test_polecenie_w_procesie_bez_terminala_i_bez_zmiennej_konczy_sie_kodem_2():
    srodowisko = {k: v for k, v in os.environ.items() if k != ZMIENNA}
    srodowisko["PYTHONPATH"] = str(KORZEN / "src")
    wynik = uruchom_i_zbierz(
        [sys.executable, "-m", "cockpit", "ustaw-haslo", JAN],
        capture_output=True,
        text=True,
        cwd=KORZEN,
        env=srodowisko,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    assert wynik.returncode == 2, wynik.stderr
    assert wynik.stderr == KOMUNIKAT_BRAK_ZRODLA
    assert wynik.stdout == ""


# --- bez podkomendy serwer startuje jak dotad ---------------------------------------------------


def test_bez_podkomendy_startuje_serwer_na_adresie_z_ustawien_i_zwraca_0(monkeypatch):
    wywolania: list[tuple] = []
    monkeypatch.setattr(
        kokpit_main.uvicorn, "run", lambda *a, **k: wywolania.append((a, k))
    )
    assert kokpit_main.main([]) == 0
    assert wywolania == [
        (
            ("cockpit.api.app:app",),
            {
                "host": settings.host,
                "port": settings.port,
                "proxy_headers": settings.proxy_headers,
                "forwarded_allow_ips": settings.forwarded_allow_ips,
            },
        )
    ]


def test_bez_podkomendy_serwer_dostaje_maskowanie_logow(monkeypatch):
    kolejnosc: list[str] = []
    monkeypatch.setattr(kokpit_main, "zainstaluj_maskowanie", lambda: kolejnosc.append("maskowanie"))
    monkeypatch.setattr(kokpit_main.uvicorn, "run", lambda *a, **k: kolejnosc.append("serwer"))
    kokpit_main.main([])
    assert kolejnosc == ["maskowanie", "serwer"]


def test_polecenie_z_wiersza_polecen_bez_podkomendy_startuje_serwer(monkeypatch):
    wywolania: list[int] = []
    monkeypatch.setattr(sys, "argv", ["cockpit"])
    monkeypatch.setattr(kokpit_main.uvicorn, "run", lambda *a, **k: wywolania.append(1))
    assert kokpit_main.main() == 0
    assert wywolania == [1]


def test_polecenie_z_wiersza_polecen_ustaw_haslo_zmienia_haslo_uzytkownika(maker, monkeypatch):
    user_id = _zaloz(maker)
    _terminal(monkeypatch, False)
    monkeypatch.setenv(ZMIENNA, NOWE)
    monkeypatch.setattr(sys, "argv", ["cockpit", "ustaw-haslo", JAN])
    assert kokpit_main.main(maker=maker) == 0
    skrot, _, _ = _stan(maker, user_id)
    assert _pasuje(skrot, NOWE)
