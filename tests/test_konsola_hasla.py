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
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from procesy import uruchom_i_zbierz
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.selectable import Select

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
KOMUNIKAT_ARGUMENTY = (
    "nieznane lub błędne argumenty; hasła nie podaje się w poleceniu: "
    "terminal albo COCKPIT_NOWE_HASLO\n"
)


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


async def test_usluga_nieznany_adres_zwraca_falsz_i_nie_zmienia_cudzego_hasla(maker):
    async with maker() as s:
        user = await konta.zarejestruj(s, JAN, STARE)
    async with maker() as s:
        wynik = await konsola_hasla.ustaw_haslo_z_konsoli(s, EWA, NOWE)
    async with maker() as s:
        zapisany = await s.get(User, user.id)
    assert wynik is False
    assert _pasuje(zapisany.haslo_skrot, STARE)


async def test_usluga_adres_z_samotnym_surogatem_to_nieznany_adres_a_nie_wyjatek(maker):
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


async def test_usluga_blokuje_wiersz_uzytkownika_na_czas_zmiany(maker, monkeypatch):
    async with maker() as s:
        await konta.zarejestruj(s, JAN, STARE)
    blokady: list[object] = []
    oryginal = Select.with_for_update

    def podglad(self, *a, **k):
        blokady.append(self)
        return oryginal(self, *a, **k)

    monkeypatch.setattr(Select, "with_for_update", podglad)
    async with maker() as s:
        await konsola_hasla.ustaw_haslo_z_konsoli(s, JAN, NOWE)
    assert len(blokady) == 1
    assert "user" in str(blokady[0]).lower()


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


def test_udane_ustawienie_hasla_ze_zmiennej_wypisuje_tylko_potwierdzenie_na_stdout(
    maker, monkeypatch, capsys
):
    _zaloz(maker)
    _terminal(monkeypatch, False)
    monkeypatch.setenv(ZMIENNA, NOWE)
    assert _uruchom(maker, JAN) == 0
    wyjscie = capsys.readouterr()
    assert wyjscie.out == KOMUNIKAT_SUKCES
    assert wyjscie.err == ""


def test_udane_ustawienie_hasla_z_terminala_wypisuje_tylko_potwierdzenie_na_stdout(
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
    """Zwraca liste (nazwa, kod oczekiwany, uruchomienie) dla sciezek udanych i blednych."""
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

    return sekret, {"udane": udane, "nieznany": nieznany, "rozne": rozne, "terminal": terminal_udane}


@pytest.mark.parametrize("nazwa", ["udane", "nieznany", "rozne", "terminal"])
def test_haslo_nie_trafia_na_ekran_ani_do_logow(maker, monkeypatch, capsys, caplog, nazwa):
    sekret, scenariusze = _scenariusze(maker, monkeypatch)
    with caplog.at_level(logging.DEBUG):
        scenariusze[nazwa]()
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


# --- domyslna sesja bazy i prawdziwy proces ------------------------------------------------------


def test_bez_podanej_fabryki_sesji_podkomenda_uzywa_sesji_aplikacji(maker, monkeypatch):
    user_id = _zaloz(maker)
    monkeypatch.setattr(kokpit_main, "SessionLocal", maker)
    _terminal(monkeypatch, False)
    monkeypatch.setenv(ZMIENNA, NOWE)
    assert kokpit_main.main(["ustaw-haslo", JAN]) == 0
    skrot, _, sesje_stan = _stan(maker, user_id)
    assert _pasuje(skrot, NOWE)
    assert all(t is not None for t in sesje_stan)


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
        (("cockpit.api.app:app",), {"host": settings.host, "port": settings.port})
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
