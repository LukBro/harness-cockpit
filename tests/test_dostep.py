"""Dostep: macierz czterech rol i ochrona ostatniego wlasciciela (HAR-38, BRO-118).

SQLite w tmp_path. Sprawdzamy reguly dostepu (kto co moze, izolacja zespolow,
ostatni wlasciciel), nie to, ze baza zapisuje wiersze. Oczekiwana macierz jest
zapisana tu jawnie, niezaleznie od `MACIERZ_ROL`.
"""
from __future__ import annotations

from types import MappingProxyType

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.db.models import Base, Membership, RolaWZespole, Tenant, User
from cockpit.services.dostep import (
    MACIERZ_ROL,
    OSTATNI_WLASCICIEL,
    Kontekst,
    Uprawnienie,
    czlonkostwo,
    usun_czlonka,
    wymagaj,
    zmien_role,
)
from cockpit.services.errors import BladDziedziny, NieZnaleziono, StanNiemozliwy, Zabronione

OWNER, ADMIN = RolaWZespole.owner, RolaWZespole.admin
MEMBER, VIEWER = RolaWZespole.member, RolaWZespole.viewer

# Tabela z BRO-118, zapisana jawnie: wiersz = uprawnienie, kolumny = owner admin member viewer.
TABELA_BRO_118 = """
podglad                  T T T T
uruchamianie             T T T N
zatwierdzanie            T T T N
edycja_definicji         T T T N
podlaczanie_projektow    T T T N
zaproszenia_i_role       T T N N
usuniecie_zespolu        T N N N
przeniesienie_wlasnosci  T N N N
"""
KOLUMNY = (OWNER, ADMIN, MEMBER, VIEWER)


def _oczekiwana() -> dict[tuple[str, str], bool]:
    wynik = {}
    for linia in TABELA_BRO_118.strip().splitlines():
        nazwa, *komorki = linia.split()
        assert len(komorki) == 4
        for rola, k in zip(KOLUMNY, komorki):
            wynik[(rola.value, nazwa)] = k == "T"
    return wynik


OCZEKIWANA = _oczekiwana()
PARY = sorted(OCZEKIWANA)


@pytest.fixture
async def maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'dostep.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _zespol(maker, nazwa: str) -> str:
    async with maker() as s:
        t = Tenant(name=nazwa)
        s.add(t)
        await s.commit()
        return t.id


async def _czlonek(maker, tenant_id: str, rola: RolaWZespole, email: str) -> str:
    """Tworzy uzytkownika i jego czlonkostwo w zespole; zwraca user_id."""
    async with maker() as s:
        u = User(email=email, haslo_skrot="argon2-skrot")
        s.add(u)
        await s.flush()
        s.add(Membership(tenant_id=tenant_id, user_id=u.id, rola=rola))
        await s.commit()
        return u.id


async def _rola_w_bazie(maker, tenant_id: str, user_id: str) -> RolaWZespole | None:
    async with maker() as s2:
        return (
            await s2.execute(
                select(Membership.rola).where(
                    Membership.tenant_id == tenant_id, Membership.user_id == user_id
                )
            )
        ).scalar_one_or_none()


def _ctx(tenant_id: str, user_id: str, rola: RolaWZespole) -> Kontekst:
    return Kontekst(user_id=user_id, tenant_id=tenant_id, rola=rola)


async def _zmien(maker, ctx: Kontekst, cel: str, nowa: RolaWZespole):
    async with maker() as s:
        return await zmien_role(s, ctx, cel, nowa)


async def _usun(maker, ctx: Kontekst, cel: str) -> None:
    async with maker() as s:
        await usun_czlonka(s, ctx, cel)


# --- macierz rol ---------------------------------------------------------------------------


def test_uprawnien_jest_osiem_o_podanych_nazwach():
    assert {u.value for u in Uprawnienie} == {
        "podglad", "uruchamianie", "zatwierdzanie", "edycja_definicji",
        "podlaczanie_projektow", "zaproszenia_i_role", "usuniecie_zespolu",
        "przeniesienie_wlasnosci",
    }
    assert len(Uprawnienie) == 8
    assert all(u.value == u.name for u in Uprawnienie)


def test_macierz_ma_dokladnie_cztery_role():
    assert set(MACIERZ_ROL) == {OWNER, ADMIN, MEMBER, VIEWER}
    assert len(MACIERZ_ROL) == 4


def test_macierz_nie_daje_sie_zmienic_w_locie():
    assert isinstance(MACIERZ_ROL, MappingProxyType)
    with pytest.raises(TypeError):
        MACIERZ_ROL[VIEWER] = frozenset(Uprawnienie)  # type: ignore[index]


def test_uprawnienia_roli_to_zbiory_niemutowalne():
    assert all(type(v) is frozenset for v in MACIERZ_ROL.values())


@pytest.mark.parametrize(("rola", "uprawnienie"), PARY)
def test_macierz_zawiera_dokladnie_te_komorki_co_tabela_bro_118(rola, uprawnienie):
    oczekiwane = OCZEKIWANA[(rola, uprawnienie)]
    assert (Uprawnienie(uprawnienie) in MACIERZ_ROL[RolaWZespole(rola)]) is oczekiwane


@pytest.mark.parametrize(("rola", "uprawnienie"), PARY)
def test_wymagaj_przepuszcza_i_odmawia_zgodnie_z_tabela_bro_118(rola, uprawnienie):
    ctx = _ctx("t", "u", RolaWZespole(rola))
    if OCZEKIWANA[(rola, uprawnienie)]:
        assert wymagaj(ctx, Uprawnienie(uprawnienie)) is None
    else:
        with pytest.raises(Zabronione):
            wymagaj(ctx, Uprawnienie(uprawnienie))


def test_zabronione_jest_bledem_dziedziny():
    assert issubclass(Zabronione, BladDziedziny)


def test_viewer_nie_moze_uruchamiac_runow():
    with pytest.raises(Zabronione) as e:
        wymagaj(_ctx("t", "u", VIEWER), Uprawnienie.uruchamianie)
    assert str(e.value) == "rola viewer nie może uruchamiać runów"


def test_viewer_nie_moze_zatwierdzac_bramek():
    with pytest.raises(Zabronione) as e:
        wymagaj(_ctx("t", "u", VIEWER), Uprawnienie.zatwierdzanie)
    assert str(e.value) == "rola viewer nie może zatwierdzać bramek"


def test_komunikat_odmowy_nazywa_role_odmowionego():
    with pytest.raises(Zabronione) as e:
        wymagaj(_ctx("t", "u", MEMBER), Uprawnienie.zaproszenia_i_role)
    assert str(e.value) == "rola member nie może zapraszać członków ani zmieniać ról"


def test_komunikat_ostatniego_wlasciciela_jest_dokladnie_ustalony():
    assert OSTATNI_WLASCICIEL == "nie można usunąć ani zdegradować ostatniego właściciela zespołu"


# --- czlonkostwo i izolacja zespolow ---------------------------------------------------------


async def test_czlonkostwo_zwraca_wiersz_z_rola_z_wlasciwego_zespolu(maker):
    a = await _zespol(maker, "A")
    b = await _zespol(maker, "B")
    jan = await _czlonek(maker, a, ADMIN, "jan@przyklad.pl")
    await _czlonek(maker, b, OWNER, "ewa@przyklad.pl")
    async with maker() as s:
        m = await czlonkostwo(s, jan, a)
    assert (m.user_id, m.tenant_id, m.rola) == (jan, a, ADMIN)


async def test_uzytkownik_bez_czlonkostwa_w_zespole_b_nie_znajduje_go_mimo_czlonkostwa_w_a(maker):
    a = await _zespol(maker, "A")
    b = await _zespol(maker, "B")
    jan = await _czlonek(maker, a, OWNER, "jan@przyklad.pl")
    await _czlonek(maker, b, MEMBER, "ewa@przyklad.pl")
    async with maker() as s:
        with pytest.raises(NieZnaleziono):
            await czlonkostwo(s, jan, b)


async def test_brak_czlonkostwa_nie_zdradza_czy_zespol_istnieje(maker):
    a = await _zespol(maker, "A")
    jan = await _czlonek(maker, a, OWNER, "jan@przyklad.pl")
    obcy = await _czlonek(maker, await _zespol(maker, "B"), OWNER, "ewa@przyklad.pl")
    async with maker() as s:
        with pytest.raises(NieZnaleziono) as istniejacy:
            await czlonkostwo(s, obcy, a)
    async with maker() as s:
        with pytest.raises(NieZnaleziono) as nieistniejacy:
            await czlonkostwo(s, jan, "nie-ma-takiego-zespolu")
    assert str(istniejacy.value) == str(nieistniejacy.value) == "zespół nieznany"


# --- zmiana roli: uprawnienie, cel, wlasnosc -------------------------------------------------


async def test_member_nie_moze_zmieniac_rol_innych(maker):
    t = await _zespol(maker, "A")
    await _czlonek(maker, t, OWNER, "o@przyklad.pl")
    ja = await _czlonek(maker, t, MEMBER, "m@przyklad.pl")
    cel = await _czlonek(maker, t, MEMBER, "c@przyklad.pl")
    with pytest.raises(Zabronione):
        await _zmien(maker, _ctx(t, ja, MEMBER), cel, VIEWER)
    assert await _rola_w_bazie(maker, t, cel) == MEMBER


async def test_member_nie_moze_usuwac_czlonkow(maker):
    t = await _zespol(maker, "A")
    await _czlonek(maker, t, OWNER, "o@przyklad.pl")
    ja = await _czlonek(maker, t, MEMBER, "m@przyklad.pl")
    cel = await _czlonek(maker, t, MEMBER, "c@przyklad.pl")
    with pytest.raises(Zabronione):
        await _usun(maker, _ctx(t, ja, MEMBER), cel)
    assert await _rola_w_bazie(maker, t, cel) == MEMBER


async def test_uprawnienie_jest_sprawdzane_przed_szukaniem_celu(maker):
    t = await _zespol(maker, "A")
    ja = await _czlonek(maker, t, MEMBER, "m@przyklad.pl")
    with pytest.raises(Zabronione):
        await _zmien(maker, _ctx(t, ja, MEMBER), "nie-ma-takiego-uzytkownika", VIEWER)
    with pytest.raises(Zabronione):
        await _usun(maker, _ctx(t, ja, MEMBER), "nie-ma-takiego-uzytkownika")


async def test_admin_wobec_nieistniejacego_celu_dostaje_nieznaleziono(maker):
    t = await _zespol(maker, "A")
    ja = await _czlonek(maker, t, ADMIN, "a@przyklad.pl")
    with pytest.raises(NieZnaleziono) as e:
        await _zmien(maker, _ctx(t, ja, ADMIN), "nie-ma-takiego-uzytkownika", VIEWER)
    assert str(e.value) == "członek nieznany"
    with pytest.raises(NieZnaleziono):
        await _usun(maker, _ctx(t, ja, ADMIN), "nie-ma-takiego-uzytkownika")


async def test_admin_nie_degraduje_wlasciciela_nawet_gdy_jest_ich_dwoch(maker):
    t = await _zespol(maker, "A")
    o1 = await _czlonek(maker, t, OWNER, "o1@przyklad.pl")
    await _czlonek(maker, t, OWNER, "o2@przyklad.pl")
    ja = await _czlonek(maker, t, ADMIN, "a@przyklad.pl")
    with pytest.raises(Zabronione):
        await _zmien(maker, _ctx(t, ja, ADMIN), o1, MEMBER)
    assert await _rola_w_bazie(maker, t, o1) == OWNER


async def test_admin_nie_usuwa_wlasciciela_nawet_gdy_jest_ich_dwoch(maker):
    t = await _zespol(maker, "A")
    o1 = await _czlonek(maker, t, OWNER, "o1@przyklad.pl")
    await _czlonek(maker, t, OWNER, "o2@przyklad.pl")
    ja = await _czlonek(maker, t, ADMIN, "a@przyklad.pl")
    with pytest.raises(Zabronione):
        await _usun(maker, _ctx(t, ja, ADMIN), o1)
    assert await _rola_w_bazie(maker, t, o1) == OWNER


async def test_admin_wobec_jedynego_wlasciciela_dostaje_zabronione_nie_ostatni_wlasciciel(maker):
    t = await _zespol(maker, "A")
    o = await _czlonek(maker, t, OWNER, "o@przyklad.pl")
    ja = await _czlonek(maker, t, ADMIN, "a@przyklad.pl")
    with pytest.raises(Zabronione):
        await _zmien(maker, _ctx(t, ja, ADMIN), o, MEMBER)
    assert await _rola_w_bazie(maker, t, o) == OWNER


async def test_admin_nie_nadaje_roli_wlasciciela(maker):
    t = await _zespol(maker, "A")
    await _czlonek(maker, t, OWNER, "o@przyklad.pl")
    ja = await _czlonek(maker, t, ADMIN, "a@przyklad.pl")
    cel = await _czlonek(maker, t, MEMBER, "c@przyklad.pl")
    with pytest.raises(Zabronione):
        await _zmien(maker, _ctx(t, ja, ADMIN), cel, OWNER)
    assert await _rola_w_bazie(maker, t, cel) == MEMBER


async def test_admin_zmienia_membera_na_viewera(maker):
    t = await _zespol(maker, "A")
    await _czlonek(maker, t, OWNER, "o@przyklad.pl")
    ja = await _czlonek(maker, t, ADMIN, "a@przyklad.pl")
    cel = await _czlonek(maker, t, MEMBER, "c@przyklad.pl")
    await _zmien(maker, _ctx(t, ja, ADMIN), cel, VIEWER)
    assert await _rola_w_bazie(maker, t, cel) == VIEWER


async def test_admin_usuwa_membera_z_zespolu(maker):
    t = await _zespol(maker, "A")
    await _czlonek(maker, t, OWNER, "o@przyklad.pl")
    ja = await _czlonek(maker, t, ADMIN, "a@przyklad.pl")
    cel = await _czlonek(maker, t, MEMBER, "c@przyklad.pl")
    await _usun(maker, _ctx(t, ja, ADMIN), cel)
    assert await _rola_w_bazie(maker, t, cel) is None
    assert await _rola_w_bazie(maker, t, ja) == ADMIN


async def test_zmien_role_zwraca_zaktualizowane_czlonkostwo(maker):
    t = await _zespol(maker, "A")
    await _czlonek(maker, t, OWNER, "o@przyklad.pl")
    ja = await _czlonek(maker, t, ADMIN, "a@przyklad.pl")
    cel = await _czlonek(maker, t, VIEWER, "c@przyklad.pl")
    m = await _zmien(maker, _ctx(t, ja, ADMIN), cel, MEMBER)
    assert isinstance(m, Membership)
    assert (m.user_id, m.tenant_id, m.rola) == (cel, t, MEMBER)


async def test_wlasciciel_nadaje_role_wlasciciela_memberowi(maker):
    t = await _zespol(maker, "A")
    ja = await _czlonek(maker, t, OWNER, "o@przyklad.pl")
    cel = await _czlonek(maker, t, MEMBER, "c@przyklad.pl")
    await _zmien(maker, _ctx(t, ja, OWNER), cel, OWNER)
    assert await _rola_w_bazie(maker, t, cel) == OWNER


# --- ostatni wlasciciel ----------------------------------------------------------------------


async def test_jedyny_wlasciciel_nie_moze_zdegradowac_siebie(maker):
    t = await _zespol(maker, "A")
    ja = await _czlonek(maker, t, OWNER, "o@przyklad.pl")
    with pytest.raises(StanNiemozliwy) as e:
        await _zmien(maker, _ctx(t, ja, OWNER), ja, ADMIN)
    assert str(e.value) == OSTATNI_WLASCICIEL
    assert await _rola_w_bazie(maker, t, ja) == OWNER


async def test_jedyny_wlasciciel_nie_moze_usunac_siebie(maker):
    t = await _zespol(maker, "A")
    ja = await _czlonek(maker, t, OWNER, "o@przyklad.pl")
    with pytest.raises(StanNiemozliwy) as e:
        await _usun(maker, _ctx(t, ja, OWNER), ja)
    assert str(e.value) == OSTATNI_WLASCICIEL
    assert await _rola_w_bazie(maker, t, ja) == OWNER


async def test_przy_dwoch_wlascicielach_jednego_mozna_zdegradowac(maker):
    t = await _zespol(maker, "A")
    ja = await _czlonek(maker, t, OWNER, "o1@przyklad.pl")
    drugi = await _czlonek(maker, t, OWNER, "o2@przyklad.pl")
    await _zmien(maker, _ctx(t, ja, OWNER), drugi, ADMIN)
    assert await _rola_w_bazie(maker, t, drugi) == ADMIN
    assert await _rola_w_bazie(maker, t, ja) == OWNER


async def test_przy_dwoch_wlascicielach_jednego_mozna_usunac(maker):
    t = await _zespol(maker, "A")
    ja = await _czlonek(maker, t, OWNER, "o1@przyklad.pl")
    drugi = await _czlonek(maker, t, OWNER, "o2@przyklad.pl")
    await _usun(maker, _ctx(t, ja, OWNER), drugi)
    assert await _rola_w_bazie(maker, t, drugi) is None
    assert await _rola_w_bazie(maker, t, ja) == OWNER


async def test_potwierdzenie_roli_wlasciciela_u_jedynego_wlasciciela_nie_jest_degradacja(maker):
    t = await _zespol(maker, "A")
    ja = await _czlonek(maker, t, OWNER, "o@przyklad.pl")
    m = await _zmien(maker, _ctx(t, ja, OWNER), ja, OWNER)
    assert m.rola == OWNER
    assert await _rola_w_bazie(maker, t, ja) == OWNER


async def test_inni_czlonkowie_nie_licza_sie_jako_wlasciciele_przy_degradacji(maker):
    t = await _zespol(maker, "A")
    ja = await _czlonek(maker, t, OWNER, "o@przyklad.pl")
    admin = await _czlonek(maker, t, ADMIN, "a@przyklad.pl")
    member = await _czlonek(maker, t, MEMBER, "m@przyklad.pl")
    viewer = await _czlonek(maker, t, VIEWER, "v@przyklad.pl")
    with pytest.raises(StanNiemozliwy) as e:
        await _zmien(maker, _ctx(t, ja, OWNER), ja, ADMIN)
    assert str(e.value) == OSTATNI_WLASCICIEL
    assert await _rola_w_bazie(maker, t, ja) == OWNER
    assert await _rola_w_bazie(maker, t, admin) == ADMIN
    assert await _rola_w_bazie(maker, t, member) == MEMBER
    assert await _rola_w_bazie(maker, t, viewer) == VIEWER


async def test_inni_czlonkowie_nie_licza_sie_jako_wlasciciele_przy_usuwaniu(maker):
    t = await _zespol(maker, "A")
    ja = await _czlonek(maker, t, OWNER, "o@przyklad.pl")
    await _czlonek(maker, t, ADMIN, "a@przyklad.pl")
    await _czlonek(maker, t, MEMBER, "m@przyklad.pl")
    with pytest.raises(StanNiemozliwy) as e:
        await _usun(maker, _ctx(t, ja, OWNER), ja)
    assert str(e.value) == OSTATNI_WLASCICIEL
    assert await _rola_w_bazie(maker, t, ja) == OWNER


async def test_wlasciciel_degraduje_innego_wlasciciela_gdy_zespol_ma_tez_innych_czlonkow(maker):
    t = await _zespol(maker, "A")
    ja = await _czlonek(maker, t, OWNER, "o1@przyklad.pl")
    drugi = await _czlonek(maker, t, OWNER, "o2@przyklad.pl")
    await _czlonek(maker, t, MEMBER, "m@przyklad.pl")
    await _zmien(maker, _ctx(t, ja, OWNER), drugi, ADMIN)
    assert await _rola_w_bazie(maker, t, drugi) == ADMIN
    assert await _rola_w_bazie(maker, t, ja) == OWNER


async def test_wlasciciel_innego_zespolu_nie_liczy_sie_do_ochrony_ostatniego_wlasciciela(maker):
    a = await _zespol(maker, "A")
    b = await _zespol(maker, "B")
    ja = await _czlonek(maker, a, OWNER, "o-a@przyklad.pl")
    inny = await _czlonek(maker, b, OWNER, "o-b@przyklad.pl")
    with pytest.raises(StanNiemozliwy):
        await _zmien(maker, _ctx(a, ja, OWNER), ja, MEMBER)
    assert await _rola_w_bazie(maker, a, ja) == OWNER
    with pytest.raises(StanNiemozliwy):
        await _usun(maker, _ctx(a, ja, OWNER), ja)
    assert await _rola_w_bazie(maker, a, ja) == OWNER
    assert await _rola_w_bazie(maker, b, inny) == OWNER


# --- cel z innego zespolu --------------------------------------------------------------------


async def test_admin_nie_zmieni_roli_czlonka_innego_zespolu(maker):
    a = await _zespol(maker, "A")
    b = await _zespol(maker, "B")
    await _czlonek(maker, a, OWNER, "o-a@przyklad.pl")
    ja = await _czlonek(maker, a, ADMIN, "a@przyklad.pl")
    await _czlonek(maker, b, OWNER, "o-b@przyklad.pl")
    obcy = await _czlonek(maker, b, MEMBER, "obcy@przyklad.pl")
    with pytest.raises(NieZnaleziono):
        await _zmien(maker, _ctx(a, ja, ADMIN), obcy, VIEWER)
    assert await _rola_w_bazie(maker, b, obcy) == MEMBER
    assert await _rola_w_bazie(maker, a, obcy) is None


async def test_admin_nie_usunie_czlonka_innego_zespolu(maker):
    a = await _zespol(maker, "A")
    b = await _zespol(maker, "B")
    await _czlonek(maker, a, OWNER, "o-a@przyklad.pl")
    ja = await _czlonek(maker, a, ADMIN, "a@przyklad.pl")
    await _czlonek(maker, b, OWNER, "o-b@przyklad.pl")
    obcy = await _czlonek(maker, b, MEMBER, "obcy@przyklad.pl")
    with pytest.raises(NieZnaleziono):
        await _usun(maker, _ctx(a, ja, ADMIN), obcy)
    assert await _rola_w_bazie(maker, b, obcy) == MEMBER


async def test_wlasciciel_nie_zmieni_roli_czlonka_innego_zespolu(maker):
    a = await _zespol(maker, "A")
    b = await _zespol(maker, "B")
    ja = await _czlonek(maker, a, OWNER, "o-a@przyklad.pl")
    await _czlonek(maker, b, OWNER, "o-b@przyklad.pl")
    obcy = await _czlonek(maker, b, VIEWER, "obcy@przyklad.pl")
    with pytest.raises(NieZnaleziono):
        await _zmien(maker, _ctx(a, ja, OWNER), obcy, MEMBER)
    assert await _rola_w_bazie(maker, b, obcy) == VIEWER
