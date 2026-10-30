"""Konta: rejestracja z zespolem, logowanie argon2id, sesje w bazie (HAR-44).

SQLite w tmp_path. Sprawdzamy reguly uslugi (atomowosc rejestracji, jeden komunikat
logowania, uniewaznianie sesji, CSRF), nie to, ze baza zapisuje wiersze. Kazda asercja
o skutku czyta dane w NOWEJ sesji `maker()`.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

import pytest
from argon2 import PasswordHasher
from pydantic import ValidationError
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.db.models import Base, Membership, RolaWZespole, Sesja, Tenant, User
from cockpit.services import konta, sesje
from cockpit.services.dostep import Kontekst
from cockpit.services.errors import (
    BladDziedziny,
    BrakDostepu,
    NieprawidloweObecneHaslo,
    StanNiemozliwy,
)
from cockpit.settings import Settings, settings

KOMUNIKAT_SESJI = "sesja wygasła, zaloguj się"
KOMUNIKAT_LOGOWANIA = "nieprawidłowy adres lub hasło"
HASLO = "Sekret-Hasl0-Alfa"
JAN = "jan@przyklad.pl"
EWA = "ewa@przyklad.pl"
IP = "203.0.113.7"


@pytest.fixture
async def maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'konta_usluga.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _sha(napis: str) -> str:
    return hashlib.sha256(napis.encode("utf-8")).hexdigest()


def _utc(dt: datetime) -> datetime:
    # SQLite oddaje naive datetime z kolumn DateTime(timezone=True)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _teraz() -> datetime:
    return datetime.now(timezone.utc)


async def _rejestruj(maker, email=JAN, haslo=HASLO, nazwa=None) -> User:
    async with maker() as s:
        return await konta.zarejestruj(s, email, haslo, nazwa)


async def _nowa_sesja(maker, user: User) -> tuple[str, str]:
    async with maker() as s:
        return await sesje.utworz(s, user)


async def _uzytkownik_z_sesja(maker, email=JAN, haslo=HASLO):
    user = await _rejestruj(maker, email, haslo)
    ident, csrf = await _nowa_sesja(maker, user)
    return user, ident, csrf


async def _kontekst(maker, ident: str) -> Kontekst:
    async with maker() as s:
        return await sesje.rozstrzygnij(s, ident)


async def _wiersze(maker, model) -> list:
    async with maker() as s:
        return list((await s.execute(select(model))).scalars())


async def _sesja_w_bazie(maker, ident: str) -> Sesja:
    async with maker() as s:
        return (await s.execute(select(Sesja).where(Sesja.skrot == _sha(ident)))).scalar_one()


async def _user_w_bazie(maker, user_id: str) -> User:
    async with maker() as s:
        return (await s.execute(select(User).where(User.id == user_id))).scalar_one()


async def _ustaw_sesje(maker, ident: str, **pola) -> None:
    async with maker() as s:
        await s.execute(update(Sesja).where(Sesja.skrot == _sha(ident)).values(**pola))
        await s.commit()


async def _dodaj_zespol(maker, user_id: str, rola=RolaWZespole.member, created_at=None) -> str:
    async with maker() as s:
        t = Tenant(name="Drugi zespol")
        s.add(t)
        await s.flush()
        m = Membership(tenant_id=t.id, user_id=user_id, rola=rola)
        if created_at is not None:
            m.created_at = created_at
        s.add(m)
        await s.commit()
        return t.id


async def _wszystkie_wartosci(maker) -> list[str]:
    """Kazda wartosc kazdej kolumny wszystkich tabel jako napis."""
    wartosci: list[str] = []
    async with maker() as s:
        for tabela in Base.metadata.sorted_tables:
            for wiersz in (await s.execute(select(tabela))).all():
                wartosci.extend(str(v) for v in wiersz)
    return wartosci


async def _odmowa_sesji(maker, ident: str) -> None:
    async with maker() as s:
        with pytest.raises(BrakDostepu) as e:
            await sesje.rozstrzygnij(s, ident)
    assert str(e.value) == KOMUNIKAT_SESJI


# --- rejestracja -----------------------------------------------------------------------------


async def test_rejestracja_tworzy_zespol_uzytkownika_i_czlonkostwo_owner_w_jednej_transakcji(maker):
    user = await _rejestruj(maker)

    tenanty = await _wiersze(maker, Tenant)
    uzytkownicy = await _wiersze(maker, User)
    czlonkostwa = await _wiersze(maker, Membership)
    assert (len(tenanty), len(uzytkownicy), len(czlonkostwa)) == (1, 1, 1)
    assert uzytkownicy[0].id == user.id
    assert czlonkostwa[0].rola == RolaWZespole.owner
    assert czlonkostwa[0].user_id == uzytkownicy[0].id
    assert czlonkostwa[0].tenant_id == tenanty[0].id


async def test_adres_przy_rejestracji_jest_zapisany_bez_spacji_i_malymi_literami(maker):
    await _rejestruj(maker, email="  Ala@Example.COM ")
    assert [u.email for u in await _wiersze(maker, User)] == ["ala@example.com"]


async def test_podana_nazwa_zespolu_zostaje_zapisana_bez_otaczajacych_spacji(maker):
    await _rejestruj(maker, nazwa="  Browarek  ")
    assert [t.name for t in await _wiersze(maker, Tenant)] == ["Browarek"]


async def test_bez_nazwy_zespol_nazywa_sie_jak_znormalizowany_adres(maker):
    await _rejestruj(maker, email="  Ala@Example.COM ")
    assert [t.name for t in await _wiersze(maker, Tenant)] == ["ala@example.com"]


async def test_nazwa_zespolu_z_samych_spacji_jest_traktowana_jak_brak_nazwy(maker):
    await _rejestruj(maker, email=JAN, nazwa="    ")
    assert [t.name for t in await _wiersze(maker, Tenant)] == [JAN]


async def test_nazwa_zespolu_dluzsza_niz_200_znakow_jest_obcieta_do_200(maker):
    await _rejestruj(maker, nazwa="x" * 250)
    nazwy = [t.name for t in await _wiersze(maker, Tenant)]
    assert nazwy == ["x" * 200]


async def test_awaria_przy_tworzeniu_czlonkostwa_nie_zostawia_zespolu_ani_uzytkownika(
    maker, monkeypatch
):
    class Awaria(Exception):
        pass

    def membership_rzucajace(*args, **kwargs):
        raise Awaria("awaria przy czlonkostwie")

    monkeypatch.setattr(konta, "Membership", membership_rzucajace)

    async with maker() as s:
        with pytest.raises(Awaria):
            await konta.zarejestruj(s, JAN, HASLO)

    assert await _wiersze(maker, Tenant) == []
    assert await _wiersze(maker, User) == []
    assert await _wiersze(maker, Membership) == []


@pytest.mark.parametrize("haslo", ["1234567", "zażółć1"])
async def test_haslo_krotsze_niz_osiem_znakow_jest_odrzucone_i_nic_nie_powstaje(maker, haslo):
    # "zażółć1" ma 7 znakow, ale 10 bajtow: liczymy znaki, nie bajty
    async with maker() as s:
        with pytest.raises(StanNiemozliwy) as e:
            await konta.zarejestruj(s, JAN, haslo)
    assert str(e.value) == "hasło musi mieć co najmniej 8 znaków"
    assert haslo not in str(e.value)
    assert await _wiersze(maker, Tenant) == []
    assert await _wiersze(maker, User) == []
    assert await _wiersze(maker, Membership) == []


async def test_haslo_dokladnie_osmioznakowe_jest_przyjete(maker):
    await _rejestruj(maker, haslo="12345678")
    assert len(await _wiersze(maker, User)) == 1


@pytest.mark.parametrize("drugi", [JAN, "  JAN@Przyklad.PL "])
async def test_zajety_adres_jest_odrzucony_takze_w_innym_zapisie_i_nie_tworzy_drugiego_zespolu(
    maker, drugi
):
    await _rejestruj(maker)
    async with maker() as s:
        with pytest.raises(StanNiemozliwy) as e:
            await konta.zarejestruj(s, drugi, "Inne-Haslo-Beta")
    assert str(e.value) == "adres jest już zajęty"
    assert "Inne-Haslo-Beta" not in str(e.value)
    assert len(await _wiersze(maker, Tenant)) == 1
    assert len(await _wiersze(maker, User)) == 1
    assert len(await _wiersze(maker, Membership)) == 1


async def test_haslo_jest_zapisane_jako_skrot_argon2id(maker):
    user = await _rejestruj(maker)
    zapisany = await _user_w_bazie(maker, user.id)
    assert zapisany.haslo_skrot.startswith("$argon2id$")
    assert zapisany.haslo_skrot != HASLO


# --- logowanie -------------------------------------------------------------------------------


async def test_poprawne_haslo_loguje_i_zwraca_uzytkownika(maker):
    user = await _rejestruj(maker)
    async with maker() as s:
        zalogowany = await konta.zaloguj(s, JAN, HASLO, IP)
    assert zalogowany.id == user.id


async def test_logowanie_dziala_dla_adresu_w_innej_wielkosci_liter_i_ze_spacjami(maker):
    user = await _rejestruj(maker)
    async with maker() as s:
        zalogowany = await konta.zaloguj(s, "  JAN@Przyklad.PL ", HASLO, IP)
    assert zalogowany.id == user.id


async def test_zle_haslo_i_nieistniejacy_adres_daja_ten_sam_komunikat(maker):
    await _rejestruj(maker)
    async with maker() as s:
        with pytest.raises(BrakDostepu) as zle_haslo:
            await konta.zaloguj(s, JAN, "Zle-Haslo-Gamma", IP)
    async with maker() as s:
        with pytest.raises(BrakDostepu) as brak_konta:
            await konta.zaloguj(s, EWA, HASLO, IP)
    assert str(zle_haslo.value) == str(brak_konta.value) == KOMUNIKAT_LOGOWANIA
    assert "Zle-Haslo-Gamma" not in str(zle_haslo.value)


class _SzpiegHashera:
    """Zapamietuje wywolania verify i deleguje do prawdziwego hashera."""

    def __init__(self, prawdziwy):
        self._prawdziwy = prawdziwy
        self.weryfikacje: list[tuple[str, str]] = []

    def verify(self, skrot, haslo):
        self.weryfikacje.append((skrot, haslo))
        return self._prawdziwy.verify(skrot, haslo)

    def __getattr__(self, nazwa):
        return getattr(self._prawdziwy, nazwa)


async def test_nieistniejacy_adres_tez_kosztuje_weryfikacje_skrotu_atrapy(maker, monkeypatch):
    user = await _rejestruj(maker)
    szpieg = _SzpiegHashera(konta._HASHER)
    monkeypatch.setattr(konta, "_HASHER", szpieg)

    async with maker() as s:
        with pytest.raises(BrakDostepu):
            await konta.zaloguj(s, EWA, "Cokolwiek-Delta-1", IP)

    assert len(szpieg.weryfikacje) == 1
    skrot_atrapy, sprawdzane = szpieg.weryfikacje[0]
    assert sprawdzane == "Cokolwiek-Delta-1"
    assert skrot_atrapy.startswith("$argon2id$")
    assert skrot_atrapy != (await _user_w_bazie(maker, user.id)).haslo_skrot


async def test_skrot_hasla_z_slabszymi_parametrami_jest_przeliczony_przy_logowaniu(maker):
    stary = PasswordHasher(time_cost=1).hash(HASLO)
    async with maker() as s:
        u = User(email=JAN, haslo_skrot=stary)
        s.add(u)
        await s.commit()
        user_id = u.id

    async with maker() as s:
        zalogowany = await konta.zaloguj(s, JAN, HASLO, IP)
    assert zalogowany.id == user_id

    nowy = (await _user_w_bazie(maker, user_id)).haslo_skrot
    assert nowy != stary
    assert nowy.startswith("$argon2id$")
    PasswordHasher().verify(nowy, HASLO)  # rzuci, gdyby skrot nie pasowal do hasla


async def test_skrot_z_domyslnymi_parametrami_zostaje_bez_zmian_po_logowaniu(maker):
    user = await _rejestruj(maker)
    przed = (await _user_w_bazie(maker, user.id)).haslo_skrot
    async with maker() as s:
        await konta.zaloguj(s, JAN, HASLO, IP)
    assert (await _user_w_bazie(maker, user.id)).haslo_skrot == przed


# --- sesje: utworzenie -----------------------------------------------------------------------


async def test_utworzona_sesja_trzyma_w_bazie_tylko_skroty_identyfikatora_i_tokenu_csrf(maker):
    user = await _rejestruj(maker)
    ident, csrf = await _nowa_sesja(maker, user)

    assert ident != csrf
    wiersz = (await _wiersze(maker, Sesja))[0]
    assert wiersz.skrot == _sha(ident)
    assert wiersz.csrf_skrot == _sha(csrf)
    wartosci = await _wszystkie_wartosci(maker)
    assert ident not in wartosci
    assert csrf not in wartosci


async def test_kazda_sesja_dostaje_inny_identyfikator_i_inny_token(maker):
    user = await _rejestruj(maker)
    pierwsza = await _nowa_sesja(maker, user)
    druga = await _nowa_sesja(maker, user)
    assert pierwsza[0] != druga[0]
    assert pierwsza[1] != druga[1]


async def test_sesja_dotyczy_zespolu_najstarszego_czlonkostwa_a_nie_najpierw_dodanego(maker):
    user = await _rejestruj(maker)  # czlonkostwo "teraz"
    najstarszy = await _dodaj_zespol(
        maker, user.id, created_at=_teraz() - timedelta(days=1)  # dodane pozniej, ale starsze
    )
    ident, _ = await _nowa_sesja(maker, user)
    assert (await _sesja_w_bazie(maker, ident)).tenant_id == najstarszy


async def test_sesja_wygasa_po_tylu_dniach_ile_wskazuja_ustawienia(maker, monkeypatch):
    monkeypatch.setattr(settings, "sesja_wazna_dni", 2)
    user = await _rejestruj(maker)
    przed = _teraz()
    ident, _ = await _nowa_sesja(maker, user)
    po = _teraz()

    wygasa = _utc((await _sesja_w_bazie(maker, ident)).wygasa_at)
    assert przed + timedelta(days=2) <= wygasa <= po + timedelta(days=2)


async def test_uzytkownik_bez_zespolu_nie_dostaje_sesji(maker):
    async with maker() as s:
        u = User(email=JAN, haslo_skrot="x")
        s.add(u)
        await s.commit()
    async with maker() as s:
        with pytest.raises(StanNiemozliwy) as e:
            await sesje.utworz(s, u)
    assert str(e.value) == "użytkownik nie należy do żadnego zespołu"
    assert await _wiersze(maker, Sesja) == []


# --- sesje: rozstrzyganie --------------------------------------------------------------------


async def test_rozstrzygniecie_sesji_zwraca_zespol_i_role_z_czlonkostwa(maker):
    user, ident, _ = await _uzytkownik_z_sesja(maker)
    tenant_id = (await _sesja_w_bazie(maker, ident)).tenant_id
    # rola z bazy, nie domyslne owner z rejestracji
    async with maker() as s:
        await s.execute(
            update(Membership)
            .where(Membership.user_id == user.id)
            .values(rola=RolaWZespole.viewer)
        )
        await s.commit()

    assert await _kontekst(maker, ident) == Kontekst(
        user_id=user.id, tenant_id=tenant_id, rola=RolaWZespole.viewer
    )


async def test_rozstrzygniecie_sesji_odswieza_czas_ostatniej_aktywnosci(maker):
    _, ident, _ = await _uzytkownik_z_sesja(maker)
    stary = datetime(2020, 1, 1, tzinfo=timezone.utc)
    await _ustaw_sesje(maker, ident, ostatnio_at=stary)

    przed = _teraz()
    await _kontekst(maker, ident)
    assert _utc((await _sesja_w_bazie(maker, ident)).ostatnio_at) >= przed - timedelta(seconds=1)


async def test_nieznany_identyfikator_sesji_jest_odrzucony(maker):
    await _uzytkownik_z_sesja(maker)
    await _odmowa_sesji(maker, "nie-ma-takiej-sesji")


async def test_sesja_po_wylogowaniu_jest_odrzucona(maker):
    _, ident, _ = await _uzytkownik_z_sesja(maker)
    await _kontekst(maker, ident)  # przed wylogowaniem przechodzi
    async with maker() as s:
        await sesje.wyloguj(s, ident)
    assert (await _sesja_w_bazie(maker, ident)).uniewazniona_at is not None
    await _odmowa_sesji(maker, ident)


async def test_sesja_po_wylogowaniu_wszedzie_jest_odrzucona(maker):
    user, ident, _ = await _uzytkownik_z_sesja(maker)
    await _kontekst(maker, ident)
    async with maker() as s:
        await sesje.wyloguj_wszedzie(s, user.id)
    await _odmowa_sesji(maker, ident)


async def test_sesja_po_terminie_waznosci_jest_odrzucona(maker):
    _, ident, _ = await _uzytkownik_z_sesja(maker)
    await _kontekst(maker, ident)  # przed terminem przechodzi
    await _ustaw_sesje(maker, ident, wygasa_at=_teraz() - timedelta(minutes=1))
    await _odmowa_sesji(maker, ident)


async def test_sesja_zespolu_z_ktorego_usunieto_uzytkownika_jest_odrzucona(maker):
    user, ident, _ = await _uzytkownik_z_sesja(maker)
    tenant_id = (await _sesja_w_bazie(maker, ident)).tenant_id
    # czlonkostwo w innym zespole zostaje: liczy sie zespol sesji
    await _dodaj_zespol(maker, user.id, created_at=_teraz() + timedelta(days=1))
    await _kontekst(maker, ident)
    async with maker() as s:
        await s.execute(
            delete(Membership).where(
                Membership.user_id == user.id, Membership.tenant_id == tenant_id
            )
        )
        await s.commit()
    await _odmowa_sesji(maker, ident)


# --- sesje: wylogowanie ----------------------------------------------------------------------


async def test_wylogowanie_wszedzie_uniewaznia_wszystkie_sesje_uzytkownika_a_cudze_dziala(maker):
    jan, jan1, _ = await _uzytkownik_z_sesja(maker, JAN)
    jan2, _ = await _nowa_sesja(maker, jan)
    ewa, ewa1, _ = await _uzytkownik_z_sesja(maker, EWA)

    async with maker() as s:
        await sesje.wyloguj_wszedzie(s, jan.id)

    await _odmowa_sesji(maker, jan1)
    await _odmowa_sesji(maker, jan2)
    assert (await _kontekst(maker, ewa1)).user_id == ewa.id
    assert (await _sesja_w_bazie(maker, ewa1)).uniewazniona_at is None


async def test_wylogowanie_nieznanej_sesji_nie_rzuca_i_nie_rusza_innych(maker):
    user, ident, _ = await _uzytkownik_z_sesja(maker)
    async with maker() as s:
        await sesje.wyloguj(s, "nie-ma-takiej-sesji")
    assert (await _kontekst(maker, ident)).user_id == user.id


async def test_wylogowanie_jednej_sesji_nie_rusza_drugiej_sesji_tego_samego_uzytkownika(maker):
    user, pierwsza, _ = await _uzytkownik_z_sesja(maker)
    druga, _ = await _nowa_sesja(maker, user)

    async with maker() as s:
        await sesje.wyloguj(s, pierwsza)

    await _odmowa_sesji(maker, pierwsza)
    assert (await _kontekst(maker, druga)).user_id == user.id


# --- zmiana hasla ----------------------------------------------------------------------------


async def test_zmiana_hasla_uniewaznia_pozostale_sesje_a_biezaca_dziala(maker):
    jan, biezaca, _ = await _uzytkownik_z_sesja(maker, JAN)
    inna, _ = await _nowa_sesja(maker, jan)
    ewa, cudza, _ = await _uzytkownik_z_sesja(maker, EWA)

    async with maker() as s:
        await konta.zmien_haslo(s, jan, HASLO, "Nowe-Haslo-Omega", biezaca_sesja_id=biezaca)

    assert (await _kontekst(maker, biezaca)).user_id == jan.id
    await _odmowa_sesji(maker, inna)
    assert (await _kontekst(maker, cudza)).user_id == ewa.id
    assert (await _sesja_w_bazie(maker, cudza)).uniewazniona_at is None


async def test_po_zmianie_hasla_dziala_nowe_a_stare_nie_i_zmienia_sie_znacznik_czasu(maker):
    jan, biezaca, _ = await _uzytkownik_z_sesja(maker, JAN)
    async with maker() as s:
        await s.execute(
            update(User)
            .where(User.id == jan.id)
            .values(haslo_zmienione_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
        )
        await s.commit()

    async with maker() as s:
        await konta.zmien_haslo(s, jan, HASLO, "Nowe-Haslo-Omega", biezaca_sesja_id=biezaca)

    async with maker() as s:
        assert (await konta.zaloguj(s, JAN, "Nowe-Haslo-Omega", IP)).id == jan.id
    async with maker() as s:
        with pytest.raises(BrakDostepu) as e:
            await konta.zaloguj(s, JAN, HASLO, IP)
    assert str(e.value) == KOMUNIKAT_LOGOWANIA
    zmienione = (await _user_w_bazie(maker, jan.id)).haslo_zmienione_at
    assert zmienione is not None
    assert _utc(zmienione) > datetime(2021, 1, 1, tzinfo=timezone.utc)


async def test_zle_stare_haslo_odmawia_i_niczego_nie_zmienia(maker):
    jan, biezaca, _ = await _uzytkownik_z_sesja(maker, JAN)
    inna, _ = await _nowa_sesja(maker, jan)
    przed = await _user_w_bazie(maker, jan.id)

    async with maker() as s:
        with pytest.raises(NieprawidloweObecneHaslo) as e:
            await konta.zmien_haslo(
                s, jan, "Zle-Stare-Haslo-1", "Nowe-Haslo-Omega", biezaca_sesja_id=biezaca
            )

    assert str(e.value) == "nieprawidłowe hasło"
    assert "Zle-Stare-Haslo-1" not in str(e.value)
    assert "Nowe-Haslo-Omega" not in str(e.value)
    po = await _user_w_bazie(maker, jan.id)
    assert po.haslo_skrot == przed.haslo_skrot
    assert po.haslo_zmienione_at == przed.haslo_zmienione_at
    assert (await _kontekst(maker, inna)).user_id == jan.id


def test_zle_obecne_haslo_to_blad_formularza_a_nie_brak_dostepu_jak_wygasla_sesja():
    """Decyzja właściciela (HAR-145): dziedziczy wprost z BladDziedziny, nie z BrakDostepu."""
    assert issubclass(NieprawidloweObecneHaslo, BladDziedziny)
    assert not issubclass(NieprawidloweObecneHaslo, BrakDostepu)


async def test_za_krotkie_nowe_haslo_odmawia_i_niczego_nie_zmienia(maker):
    jan, biezaca, _ = await _uzytkownik_z_sesja(maker, JAN)
    inna, _ = await _nowa_sesja(maker, jan)
    przed = await _user_w_bazie(maker, jan.id)

    async with maker() as s:
        with pytest.raises(StanNiemozliwy) as e:
            await konta.zmien_haslo(s, jan, HASLO, "1234567", biezaca_sesja_id=biezaca)

    assert str(e.value) == "hasło musi mieć co najmniej 8 znaków"
    po = await _user_w_bazie(maker, jan.id)
    assert po.haslo_skrot == przed.haslo_skrot
    assert po.haslo_zmienione_at == przed.haslo_zmienione_at
    assert (await _kontekst(maker, inna)).user_id == jan.id


# --- haslo nie wycieka -----------------------------------------------------------------------


async def test_jawne_haslo_nie_trafia_do_bazy_ani_do_logow(maker, caplog):
    caplog.set_level(logging.DEBUG)
    haslo, zle, nowe = "Tajne-Haslo-Epsilon", "Zle-Haslo-Zeta-9", "Nowe-Haslo-Eta-77"

    jan = await _rejestruj(maker, JAN, haslo)
    async with maker() as s:
        await konta.zaloguj(s, JAN, haslo, IP)
    async with maker() as s:
        with pytest.raises(BrakDostepu):
            await konta.zaloguj(s, JAN, zle, IP)
    async with maker() as s:
        with pytest.raises(BrakDostepu):
            await konta.zaloguj(s, EWA, zle, IP)
    ident, _ = await _nowa_sesja(maker, jan)
    async with maker() as s:
        await konta.zmien_haslo(s, jan, haslo, nowe, biezaca_sesja_id=ident)

    wartosci = await _wszystkie_wartosci(maker)
    for jawne in (haslo, zle, nowe):
        assert not [w for w in wartosci if jawne in w]
        assert jawne not in caplog.text
    assert ident not in caplog.text


# --- CSRF ------------------------------------------------------------------------------------


async def test_token_csrf_z_utworzenia_sesji_przechodzi_sprawdzenie(maker):
    _, ident, csrf = await _uzytkownik_z_sesja(maker)
    async with maker() as s:
        assert await sesje.sprawdz_csrf(s, ident, csrf) is None


async def test_zly_token_csrf_jest_odrzucony(maker):
    _, ident, _ = await _uzytkownik_z_sesja(maker)
    async with maker() as s:
        with pytest.raises(BrakDostepu) as e:
            await sesje.sprawdz_csrf(s, ident, "to-nie-jest-token")
    assert str(e.value) == "nieprawidłowy token CSRF"


async def test_token_csrf_innej_sesji_nie_przechodzi(maker):
    user, pierwsza, _ = await _uzytkownik_z_sesja(maker)
    _, csrf_drugiej = await _nowa_sesja(maker, user)
    async with maker() as s:
        with pytest.raises(BrakDostepu) as e:
            await sesje.sprawdz_csrf(s, pierwsza, csrf_drugiej)
    assert str(e.value) == "nieprawidłowy token CSRF"


async def test_odnowiony_token_csrf_przechodzi_a_poprzedni_juz_nie(maker):
    _, ident, stary = await _uzytkownik_z_sesja(maker)
    async with maker() as s:
        nowy = await sesje.odnow_csrf(s, ident)

    assert nowy != stary
    assert (await _sesja_w_bazie(maker, ident)).csrf_skrot == _sha(nowy)
    async with maker() as s:
        await sesje.sprawdz_csrf(s, ident, nowy)
    async with maker() as s:
        with pytest.raises(BrakDostepu) as e:
            await sesje.sprawdz_csrf(s, ident, stary)
    assert str(e.value) == "nieprawidłowy token CSRF"


async def _uniewaznij_wylogowaniem(maker, user, ident):
    async with maker() as s:
        await sesje.wyloguj(s, ident)


async def _uniewaznij_terminem(maker, user, ident):
    await _ustaw_sesje(maker, ident, wygasa_at=_teraz() - timedelta(minutes=1))


@pytest.mark.parametrize("uniewaznij", [_uniewaznij_wylogowaniem, _uniewaznij_terminem])
async def test_sprawdzenie_csrf_dla_sesji_wylogowanej_lub_wygaslej_daje_odmowe_sesji(
    maker, uniewaznij
):
    user, ident, csrf = await _uzytkownik_z_sesja(maker)
    await uniewaznij(maker, user, ident)  # token pasuje, decyduje tylko stan sesji
    async with maker() as s:
        with pytest.raises(BrakDostepu) as e:
            await sesje.sprawdz_csrf(s, ident, csrf)
    assert str(e.value) == KOMUNIKAT_SESJI


@pytest.mark.parametrize("uniewaznij", [_uniewaznij_wylogowaniem, _uniewaznij_terminem])
async def test_odnowienie_csrf_dla_sesji_wylogowanej_lub_wygaslej_daje_odmowe_sesji(
    maker, uniewaznij
):
    user, ident, csrf = await _uzytkownik_z_sesja(maker)
    await uniewaznij(maker, user, ident)
    async with maker() as s:
        with pytest.raises(BrakDostepu) as e:
            await sesje.odnow_csrf(s, ident)
    assert str(e.value) == KOMUNIKAT_SESJI
    assert (await _sesja_w_bazie(maker, ident)).csrf_skrot == _sha(csrf)


async def test_odnowienie_csrf_dla_nieznanej_sesji_daje_odmowe_sesji(maker):
    async with maker() as s:
        with pytest.raises(BrakDostepu) as e:
            await sesje.odnow_csrf(s, "nie-ma-takiej-sesji")
    assert str(e.value) == KOMUNIKAT_SESJI


# --- poprawki po mutantach (HAR-44, iteracja 1) ----------------------------------------------

STARA_DATA = datetime(2020, 1, 1, tzinfo=timezone.utc)


async def _dodaj_czlonka(maker, tenant_id: str, user_id: str, rola: RolaWZespole) -> None:
    async with maker() as s:
        s.add(Membership(tenant_id=tenant_id, user_id=user_id, rola=rola))
        await s.commit()


async def _uzytkownik_z_czlonkostwami(maker, czlonkostwa: list[tuple[str, datetime]]):
    """Uzytkownik bez rejestracji; kazde czlonkostwo (jawne id, created_at) w osobnym zespole,
    wstawiane w podanej kolejnosci. Zwraca (user, {id czlonkostwa: id zespolu})."""
    zespoly: dict[str, str] = {}
    async with maker() as s:
        user = User(email=JAN, haslo_skrot="x")
        s.add(user)
        await s.flush()
        for id_czlonkostwa, _ in czlonkostwa:
            t = Tenant(name=f"Zespol {id_czlonkostwa[:2]}")
            s.add(t)
            await s.flush()
            zespoly[id_czlonkostwa] = t.id
        for id_czlonkostwa, utworzono in czlonkostwa:
            s.add(
                Membership(
                    id=id_czlonkostwa,
                    tenant_id=zespoly[id_czlonkostwa],
                    user_id=user.id,
                    rola=RolaWZespole.member,
                    created_at=utworzono,
                )
            )
        await s.commit()
    return user, zespoly


async def test_blad_bazy_niezwiazany_z_adresem_przy_rejestracji_nie_jest_zajetym_adresem(
    maker, monkeypatch
):
    prawdziwe = Membership
    # czlonkostwo bez zespolu: NOT NULL na membership.tenant_id, a nie kolizja adresu
    monkeypatch.setattr(
        konta, "Membership", lambda **kw: prawdziwe(**{**kw, "tenant_id": None})
    )

    async with maker() as s:
        with pytest.raises(IntegrityError):
            await konta.zarejestruj(s, JAN, HASLO)

    assert await _wiersze(maker, Tenant) == []
    assert await _wiersze(maker, User) == []
    assert await _wiersze(maker, Membership) == []


def test_komunikat_postgresql_o_unikalnym_adresie_jest_rozpoznany_a_innego_ograniczenia_nie():
    adres = IntegrityError(
        "stmt", {}, Exception('duplicate key value violates unique constraint "uq_user_email"')
    )
    inne = IntegrityError(
        "stmt",
        {},
        Exception('duplicate key value violates unique constraint "uq_membership_tenant_user"'),
    )
    assert konta._dotyczy_adresu(adres) is True
    assert konta._dotyczy_adresu(inne) is False


async def test_nowe_haslo_dokladnie_osmioznakowe_jest_przyjete_przy_zmianie_hasla(maker):
    jan, biezaca, _ = await _uzytkownik_z_sesja(maker, JAN)

    async with maker() as s:
        await konta.zmien_haslo(s, jan, HASLO, "12345678", biezaca_sesja_id=biezaca)

    async with maker() as s:
        assert (await konta.zaloguj(s, JAN, "12345678", IP)).id == jan.id


async def test_zmiana_hasla_nie_nadpisuje_czasu_wczesniejszego_uniewaznienia_sesji(maker):
    jan, biezaca, _ = await _uzytkownik_z_sesja(maker, JAN)
    wylogowana, _ = await _nowa_sesja(maker, jan)
    await _ustaw_sesje(maker, wylogowana, uniewazniona_at=STARA_DATA)

    async with maker() as s:
        await konta.zmien_haslo(s, jan, HASLO, "Nowe-Haslo-Omega", biezaca_sesja_id=biezaca)

    po = await _sesja_w_bazie(maker, wylogowana)
    assert _utc(po.uniewazniona_at) == STARA_DATA


async def test_ponowne_wylogowanie_nie_nadpisuje_czasu_wczesniejszego_uniewaznienia(maker):
    _, ident, _ = await _uzytkownik_z_sesja(maker)
    await _ustaw_sesje(maker, ident, uniewazniona_at=STARA_DATA)

    async with maker() as s:
        await sesje.wyloguj(s, ident)

    assert _utc((await _sesja_w_bazie(maker, ident)).uniewazniona_at) == STARA_DATA


async def test_wylogowanie_wszedzie_nie_nadpisuje_czasu_wczesniejszego_uniewaznienia(maker):
    user, ident, _ = await _uzytkownik_z_sesja(maker)
    await _ustaw_sesje(maker, ident, uniewazniona_at=STARA_DATA)

    async with maker() as s:
        await sesje.wyloguj_wszedzie(s, user.id)

    assert _utc((await _sesja_w_bazie(maker, ident)).uniewazniona_at) == STARA_DATA


async def test_sesja_wygasajaca_dokladnie_teraz_jest_juz_wygasla(maker, monkeypatch):
    _, ident, _ = await _uzytkownik_z_sesja(maker)
    chwila = datetime(2030, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
    await _ustaw_sesje(maker, ident, wygasa_at=chwila)
    monkeypatch.setattr(sesje, "teraz", lambda: chwila)

    await _odmowa_sesji(maker, ident)


def test_czas_z_innej_strefy_zostaje_bez_zmian_a_czas_bez_strefy_dostaje_utc():
    plus_dwa = datetime(2024, 3, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    bez_strefy = datetime(2024, 3, 1, 12, 0)

    assert sesje._utc(plus_dwa) is plus_dwa
    assert sesje._utc(plus_dwa).utcoffset() == timedelta(hours=2)
    assert sesje._utc(bez_strefy) == datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc)
    assert sesje._utc(bez_strefy).utcoffset() == timedelta(0)


async def test_identyfikator_i_token_csrf_nowej_sesji_maja_po_32_bajty_losowosci(maker):
    # 32 bajty w base64url bez dopelnienia to 43 znaki
    _, ident, csrf = await _uzytkownik_z_sesja(maker)
    assert len(ident) == 43
    assert len(csrf) == 43


async def test_odnowiony_token_csrf_ma_32_bajty_losowosci(maker):
    _, ident, _ = await _uzytkownik_z_sesja(maker)
    async with maker() as s:
        nowy = await sesje.odnow_csrf(s, ident)
    assert len(nowy) == 43


async def test_czlonek_usuniety_z_zespolu_sesji_dostaje_odmowe_choc_zespol_ma_innego_czlonka(maker):
    jan, ident, _ = await _uzytkownik_z_sesja(maker, JAN)
    ewa = await _rejestruj(maker, EWA)
    tenant_id = (await _sesja_w_bazie(maker, ident)).tenant_id
    await _dodaj_czlonka(maker, tenant_id, ewa.id, RolaWZespole.owner)
    async with maker() as s:
        await s.execute(
            delete(Membership).where(
                Membership.user_id == jan.id, Membership.tenant_id == tenant_id
            )
        )
        await s.commit()

    await _odmowa_sesji(maker, ident)


async def test_w_zespole_z_dwoma_czlonkami_sesja_dostaje_role_swojego_wlasciciela(maker):
    jan, ident, _ = await _uzytkownik_z_sesja(maker, JAN)
    ewa = await _rejestruj(maker, EWA)
    tenant_id = (await _sesja_w_bazie(maker, ident)).tenant_id
    async with maker() as s:
        await s.execute(
            update(Membership)
            .where(Membership.user_id == jan.id)
            .values(rola=RolaWZespole.viewer)
        )
        await s.commit()
    await _dodaj_czlonka(maker, tenant_id, ewa.id, RolaWZespole.owner)

    assert await _kontekst(maker, ident) == Kontekst(
        user_id=jan.id, tenant_id=tenant_id, rola=RolaWZespole.viewer
    )


async def test_sesja_dotyczy_starszego_czlonkostwa_nawet_gdy_ma_ono_wieksze_id(maker):
    starsze = "ffffffff-0000-0000-0000-000000000001"
    nowsze = "00000000-0000-0000-0000-000000000002"
    user, zespoly = await _uzytkownik_z_czlonkostwami(
        maker,
        [
            (nowsze, datetime(2024, 6, 1, tzinfo=timezone.utc)),
            (starsze, datetime(2023, 6, 1, tzinfo=timezone.utc)),
        ],
    )

    ident, _ = await _nowa_sesja(maker, user)

    assert (await _sesja_w_bazie(maker, ident)).tenant_id == zespoly[starsze]


async def test_przy_rownym_czasie_czlonkostw_sesja_dotyczy_zespolu_o_mniejszym_id(maker):
    wieksze = "ffffffff-0000-0000-0000-000000000001"
    mniejsze = "00000000-0000-0000-0000-000000000002"
    ten_sam_czas = datetime(2024, 6, 1, tzinfo=timezone.utc)
    user, zespoly = await _uzytkownik_z_czlonkostwami(
        maker, [(wieksze, ten_sam_czas), (mniejsze, ten_sam_czas)]
    )

    ident, _ = await _nowa_sesja(maker, user)

    assert (await _sesja_w_bazie(maker, ident)).tenant_id == zespoly[mniejsze]


# --- poprawki po mutantach (HAR-44, iteracja 2) ----------------------------------------------


async def test_po_awarii_rejestracji_ta_sama_sesja_zapisuje_kolejna_rejestracje_bez_sladu_pierwszej(
    maker, monkeypatch
):
    class Awaria(Exception):
        pass

    prawdziwe = Membership
    wywolania = []

    def membership_psujace_pierwsze_wywolanie(*args, **kwargs):
        wywolania.append(kwargs)
        if len(wywolania) == 1:
            raise Awaria("awaria przy czlonkostwie")
        return prawdziwe(*args, **kwargs)

    monkeypatch.setattr(konta, "Membership", membership_psujace_pierwsze_wywolanie)

    async with maker() as s:
        with pytest.raises(Awaria):
            await konta.zarejestruj(s, JAN, HASLO)
        # ta sama sesja: bez rollbacku commit drugiej rejestracji zapisalby tez pierwsza
        druga = await konta.zarejestruj(s, EWA, HASLO)

    tenanty = await _wiersze(maker, Tenant)
    uzytkownicy = await _wiersze(maker, User)
    assert [t.name for t in tenanty] == [EWA]
    assert [u.email for u in uzytkownicy] == [EWA]
    assert uzytkownicy[0].id == druga.id
    assert len(await _wiersze(maker, Membership)) == 1


async def test_po_odrzuceniu_zajetego_adresu_ta_sama_sesja_przyjmuje_rejestracje_innego_adresu(maker):
    pierwszy = await _rejestruj(maker, JAN)

    async with maker() as s:
        with pytest.raises(StanNiemozliwy):
            await konta.zarejestruj(s, JAN, "Inne-Haslo-Beta")
        # ta sama sesja: bez rollbacku jest w stanie wymagajacym wycofania i druga operacja pada
        druga = await konta.zarejestruj(s, EWA, HASLO)

    uzytkownicy = await _wiersze(maker, User)
    assert {u.email for u in uzytkownicy} == {JAN, EWA}
    assert {u.id for u in uzytkownicy} == {pierwszy.id, druga.id}
    assert len(await _wiersze(maker, Tenant)) == 2
    assert len(await _wiersze(maker, Membership)) == 2


async def test_uzytkownik_z_uszkodzonym_skrotem_hasla_w_bazie_dostaje_ten_sam_komunikat_logowania(
    maker,
):
    async with maker() as s:
        s.add(User(email=JAN, haslo_skrot="x"))
        await s.commit()

    async with maker() as s:
        with pytest.raises(BrakDostepu) as e:
            await konta.zaloguj(s, JAN, HASLO, IP)
    assert str(e.value) == KOMUNIKAT_LOGOWANIA


def test_sesja_domyslnie_jest_wazna_30_dni(monkeypatch):
    monkeypatch.delenv("COCKPIT_SESJA_WAZNA_DNI", raising=False)
    assert Settings(_env_file=None).sesja_wazna_dni == 30


@pytest.mark.parametrize("wartosc", [0, -1])
def test_sesja_wazna_zero_albo_ujemna_liczba_dni_jest_odrzucona(monkeypatch, wartosc):
    monkeypatch.delenv("COCKPIT_SESJA_WAZNA_DNI", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, sesja_wazna_dni=wartosc)
