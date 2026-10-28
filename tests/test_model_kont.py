"""Model kont: uzytkownik, czlonkostwo z rola w zespole i sesja (HAR-36).

SQLite w tmp_path - sprawdzamy reguly modelu (unikalnosc, normalizacja, brak
jawnego sekretu sesji), nie to, ze baza zapisuje wiersze.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import String, Text, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.db.models import Base, Membership, RolaWZespole, Sesja, Tenant, User


@pytest.fixture
async def maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'konta.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _user(email: str) -> User:
    return User(email=email, haslo_skrot="argon2-skrot")


async def _zespol_i_user(maker, nazwa="Browarek", email="jan@przyklad.pl"):
    async with maker() as s:
        t = Tenant(name=nazwa)
        u = _user(email)
        s.add_all([t, u])
        await s.commit()
        return t.id, u.id


async def _czlonkostwa(maker) -> list[Membership]:
    async with maker() as s:
        return list((await s.execute(select(Membership))).scalars())


async def _adresy(maker) -> list[str]:
    async with maker() as s:
        return list((await s.execute(select(User.email))).scalars())


async def test_ten_sam_uzytkownik_ma_inna_role_w_kazdym_zespole(maker):
    async with maker() as s:
        a, b, u = Tenant(name="A"), Tenant(name="B"), _user("jan@przyklad.pl")
        s.add_all([a, b, u])
        await s.flush()
        s.add_all([
            Membership(tenant_id=a.id, user_id=u.id, rola=RolaWZespole.owner),
            Membership(tenant_id=b.id, user_id=u.id, rola=RolaWZespole.viewer),
        ])
        await s.commit()
        id_a, id_b, id_u = a.id, b.id, u.id

    wiersze = await _czlonkostwa(maker)  # nowa sesja: dane z bazy
    assert {w.user_id for w in wiersze} == {id_u}
    assert {w.tenant_id: w.rola for w in wiersze} == {
        id_a: RolaWZespole.owner,
        id_b: RolaWZespole.viewer,
    }


async def test_uzytkownik_nie_moze_miec_dwoch_czlonkostw_w_tym_samym_zespole(maker):
    tenant_id, user_id = await _zespol_i_user(maker)
    async with maker() as s:
        s.add(Membership(tenant_id=tenant_id, user_id=user_id, rola=RolaWZespole.member))
        await s.commit()

    async with maker() as s:
        s.add(Membership(tenant_id=tenant_id, user_id=user_id, rola=RolaWZespole.admin))
        with pytest.raises(IntegrityError, match=r"membership\.tenant_id"):
            await s.commit()
        await s.rollback()

    wiersze = await _czlonkostwa(maker)
    assert [(w.tenant_id, w.user_id, w.rola) for w in wiersze] == [
        (tenant_id, user_id, RolaWZespole.member)
    ]


async def test_ten_sam_uzytkownik_moze_nalezec_do_dwoch_zespolow(maker):
    # kontrprzyklad: unikalnosc samego user_id odrzucilaby drugi wiersz
    t1, u = await _zespol_i_user(maker)
    async with maker() as s:
        t2 = Tenant(name="Drugi")
        s.add(t2)
        await s.flush()
        s.add_all([
            Membership(tenant_id=t1, user_id=u, rola=RolaWZespole.member),
            Membership(tenant_id=t2.id, user_id=u, rola=RolaWZespole.member),
        ])
        await s.commit()
    assert len(await _czlonkostwa(maker)) == 2


async def test_zespol_moze_miec_wielu_uzytkownikow(maker):
    # kontrprzyklad: unikalnosc samego tenant_id odrzucilaby drugi wiersz
    t, u1 = await _zespol_i_user(maker)
    async with maker() as s:
        u2 = _user("ewa@przyklad.pl")
        s.add(u2)
        await s.flush()
        s.add_all([
            Membership(tenant_id=t, user_id=u1, rola=RolaWZespole.owner),
            Membership(tenant_id=t, user_id=u2.id, rola=RolaWZespole.viewer),
        ])
        await s.commit()
    assert len(await _czlonkostwa(maker)) == 2


async def test_drugi_uzytkownik_z_tym_samym_adresem_jest_odrzucony(maker):
    async with maker() as s:
        s.add(_user("jan@przyklad.pl"))
        await s.commit()

    async with maker() as s:
        s.add(_user("jan@przyklad.pl"))
        with pytest.raises(IntegrityError, match=r"user\.email"):
            await s.commit()
        await s.rollback()

    assert await _adresy(maker) == ["jan@przyklad.pl"]


@pytest.mark.parametrize(
    "drugi",
    [
        "  jan@przyklad.pl ",  # tylko biale znaki: bez strip adresy sa rozne
        "JAN@PRZYKLAD.PL",  # tylko wielkosc liter: bez lower adresy sa rozne
        "  Jan@Przyklad.PL ",
    ],
)
async def test_adres_rozniacy_sie_tylko_zapisem_to_ten_sam_uzytkownik(maker, drugi):
    async with maker() as s:
        s.add(_user("jan@przyklad.pl"))
        await s.commit()

    async with maker() as s:
        s.add(_user(drugi))
        with pytest.raises(IntegrityError, match=r"user\.email"):
            await s.commit()
        await s.rollback()

    assert await _adresy(maker) == ["jan@przyklad.pl"]


async def test_adres_uzytkownika_jest_zapisany_bez_spacji_i_malymi_literami(maker):
    async with maker() as s:
        s.add(_user("  Jan@Przyklad.PL "))
        await s.commit()
    assert await _adresy(maker) == ["jan@przyklad.pl"]


async def test_rozne_adresy_to_rozni_uzytkownicy(maker):
    # kontrprzyklad: normalizacja nie moze zlewac roznych adresow
    async with maker() as s:
        s.add_all([_user("jan@przyklad.pl"), _user("ewa@przyklad.pl")])
        await s.commit()
    assert sorted(await _adresy(maker)) == ["ewa@przyklad.pl", "jan@przyklad.pl"]


def test_role_w_zespole_to_wlasciciel_administrator_czlonek_i_obserwator():
    assert {r.value for r in RolaWZespole} == {"owner", "admin", "member", "viewer"}


def test_sesja_trzyma_tylko_skroty_a_nie_jawny_identyfikator_ani_token_csrf():
    kolumny = Sesja.__table__.columns
    tekstowe = {k.name for k in kolumny if isinstance(k.type, (String, Text))}
    assert tekstowe == {"id", "skrot", "user_id", "tenant_id", "csrf_skrot"}
    assert not [k.name for k in kolumny if "token" in k.name.lower()]
    assert kolumny["skrot"].unique or any(
        c.name == "uq_sesja_skrot" for c in Sesja.__table__.constraints
    )


async def test_sesja_zapisana_w_bazie_ma_skroty_i_jest_aktywna(maker):
    t, u = await _zespol_i_user(maker)
    teraz = datetime.now(timezone.utc)
    async with maker() as s:
        s.add(
            Sesja(
                skrot="a" * 64,
                user_id=u,
                tenant_id=t,
                csrf_skrot="b" * 64,
                wygasa_at=teraz + timedelta(hours=1),
                ostatnio_at=teraz,
            )
        )
        await s.commit()

    async with maker() as s:
        sesja = (await s.execute(select(Sesja))).scalars().one()
    assert (sesja.skrot, sesja.csrf_skrot) == ("a" * 64, "b" * 64)
    assert (sesja.user_id, sesja.tenant_id) == (u, t)
    assert sesja.uniewazniona_at is None


async def test_dwie_sesje_o_tym_samym_skrocie_sa_odrzucone(maker):
    t, u = await _zespol_i_user(maker)
    teraz = datetime.now(timezone.utc)

    def nowa(csrf: str) -> Sesja:
        return Sesja(
            skrot="c" * 64,
            user_id=u,
            tenant_id=t,
            csrf_skrot=csrf,
            wygasa_at=teraz,
            ostatnio_at=teraz,
        )

    async with maker() as s:
        s.add(nowa("d" * 64))
        await s.commit()
    async with maker() as s:
        s.add(nowa("e" * 64))
        with pytest.raises(IntegrityError, match=r"sesja\.skrot"):
            await s.commit()
        await s.rollback()
    async with maker() as s:
        assert len((await s.execute(select(Sesja))).scalars().all()) == 1
