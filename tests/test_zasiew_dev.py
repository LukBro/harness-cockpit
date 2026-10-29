"""Zasiew wlasciciela zespolu w srodowisku deweloperskim (HAR-50).

Nazwy w jezyku dziedziny, nie technologii (BRO-95). Zmienne COCKPIT_DEV_* sa
czytane z os.environ (nie z cockpit.settings, ktory czyta .env), wiec testy
ustawiaja je przez monkeypatch.setenv.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cockpit.db.models import Base, Membership, Runner, RolaWZespole, Tenant, User
from cockpit.settings import settings
from runner import secrets

EMAIL = "dev@example.com"
HASLO = "haslo-z-env-123"
KORZEN = Path(__file__).resolve().parent.parent


def _zaladuj_seed_dev():
    sciezka = KORZEN / "tools" / "seed_dev.py"
    spec = importlib.util.spec_from_file_location("seed_dev", sciezka)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


@pytest.fixture
async def baza_do_zasiewu(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets, "CONFIG", tmp_path / "cfg" / "credentials.json")
    monkeypatch.delenv("COCKPIT_RUNNER_TOKEN", raising=False)
    monkeypatch.delenv("COCKPIT_DEV_EMAIL", raising=False)
    monkeypatch.delenv("COCKPIT_DEV_PASSWORD", raising=False)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'seed.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _ustaw_wlasciciela(monkeypatch, email=EMAIL, haslo=HASLO):
    if email is None:
        monkeypatch.delenv("COCKPIT_DEV_EMAIL", raising=False)
    else:
        monkeypatch.setenv("COCKPIT_DEV_EMAIL", email)
    if haslo is None:
        monkeypatch.delenv("COCKPIT_DEV_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("COCKPIT_DEV_PASSWORD", haslo)


async def _uzytkownicy(maker) -> list[User]:
    async with maker() as s:
        return list((await s.execute(select(User))).scalars().all())


async def _czlonkostwa(maker) -> list[Membership]:
    async with maker() as s:
        return list((await s.execute(select(Membership))).scalars().all())


def _haslo_pasuje(skrot: str, haslo: str) -> bool:
    try:
        return PasswordHasher().verify(skrot, haslo)
    except VerifyMismatchError:
        return False


async def test_zasiew_zaklada_wlasciciela_w_zespole_runnera_z_haslem_z_env(
    baza_do_zasiewu, monkeypatch
):
    _ustaw_wlasciciela(monkeypatch)

    await _zaladuj_seed_dev().zasiej(baza_do_zasiewu)

    async with baza_do_zasiewu() as s:
        runner = (await s.execute(select(Runner))).scalars().one()
    uzytkownicy = await _uzytkownicy(baza_do_zasiewu)
    czlonkostwa = await _czlonkostwa(baza_do_zasiewu)
    assert len(uzytkownicy) == 1
    assert uzytkownicy[0].email == EMAIL
    assert len(czlonkostwa) == 1
    assert czlonkostwa[0].user_id == uzytkownicy[0].id
    assert czlonkostwa[0].rola == RolaWZespole.owner
    assert czlonkostwa[0].tenant_id == runner.tenant_id


async def test_haslo_wlasciciela_w_bazie_to_skrot_ktory_weryfikuje_haslo_z_env(
    baza_do_zasiewu, monkeypatch
):
    _ustaw_wlasciciela(monkeypatch)

    await _zaladuj_seed_dev().zasiej(baza_do_zasiewu)

    uzytkownik = (await _uzytkownicy(baza_do_zasiewu))[0]
    assert uzytkownik.haslo_skrot != HASLO
    assert HASLO not in uzytkownik.haslo_skrot
    assert PasswordHasher().verify(uzytkownik.haslo_skrot, HASLO)
    assert not _haslo_pasuje(uzytkownik.haslo_skrot, HASLO + "x")


async def test_zasiew_bez_hasla_w_env_nie_zaklada_uzytkownika(baza_do_zasiewu, monkeypatch):
    _ustaw_wlasciciela(monkeypatch, email=EMAIL, haslo=None)

    await _zaladuj_seed_dev().zasiej(baza_do_zasiewu)

    assert await _uzytkownicy(baza_do_zasiewu) == []
    assert await _czlonkostwa(baza_do_zasiewu) == []


async def test_zasiew_bez_adresu_w_env_nie_zaklada_uzytkownika(baza_do_zasiewu, monkeypatch):
    _ustaw_wlasciciela(monkeypatch, email=None, haslo=HASLO)

    await _zaladuj_seed_dev().zasiej(baza_do_zasiewu)

    assert await _uzytkownicy(baza_do_zasiewu) == []
    assert await _czlonkostwa(baza_do_zasiewu) == []


async def test_zasiew_z_pustymi_zmiennymi_nie_zaklada_uzytkownika(baza_do_zasiewu, monkeypatch):
    _ustaw_wlasciciela(monkeypatch, email="", haslo="")

    await _zaladuj_seed_dev().zasiej(baza_do_zasiewu)

    assert await _uzytkownicy(baza_do_zasiewu) == []
    assert await _czlonkostwa(baza_do_zasiewu) == []


async def test_zasiew_z_pustym_haslem_i_adresem_nie_zaklada_uzytkownika(
    baza_do_zasiewu, monkeypatch
):
    _ustaw_wlasciciela(monkeypatch, email=EMAIL, haslo="")

    await _zaladuj_seed_dev().zasiej(baza_do_zasiewu)

    assert await _uzytkownicy(baza_do_zasiewu) == []


async def test_zasiew_z_pustym_adresem_i_haslem_nie_zaklada_uzytkownika(
    baza_do_zasiewu, monkeypatch
):
    _ustaw_wlasciciela(monkeypatch, email="", haslo=HASLO)

    await _zaladuj_seed_dev().zasiej(baza_do_zasiewu)

    assert await _uzytkownicy(baza_do_zasiewu) == []


async def test_zasiew_bez_wlasciciela_nadal_zaklada_zespol_i_runnera(
    baza_do_zasiewu, monkeypatch
):
    _ustaw_wlasciciela(monkeypatch, email=None, haslo=None)

    sciezka = await _zaladuj_seed_dev().zasiej(baza_do_zasiewu)

    assert sciezka == secrets.CONFIG
    async with baza_do_zasiewu() as s:
        assert (await s.execute(select(func.count()).select_from(Tenant))).scalar_one() == 1
        assert (await s.execute(select(func.count()).select_from(Runner))).scalar_one() == 1


async def test_wlasciciel_trafia_do_zespolu_runnera_a_nie_do_innego_zespolu(
    baza_do_zasiewu, monkeypatch
):
    # Zespol A (z runnerem) jest starszy, zespol B nowszy: czlonkostwo ma trafic do A.
    teraz = datetime.now(timezone.utc)
    async with baza_do_zasiewu() as s:
        a = Tenant(name="A", created_at=teraz - timedelta(days=2))
        b = Tenant(name="B", created_at=teraz - timedelta(days=1))
        s.add_all([a, b])
        await s.flush()
        s.add(Runner(tenant_id=a.id, name="runner-a", token_hash="x"))
        await s.commit()
        id_a, id_b = a.id, b.id
    _ustaw_wlasciciela(monkeypatch)

    await _zaladuj_seed_dev().zasiej(baza_do_zasiewu)

    czlonkostwa = await _czlonkostwa(baza_do_zasiewu)
    assert [c.tenant_id for c in czlonkostwa] == [id_a]
    assert id_b not in [c.tenant_id for c in czlonkostwa]
    async with baza_do_zasiewu() as s:
        assert (await s.execute(select(func.count()).select_from(Tenant))).scalar_one() == 2


async def test_powtorny_zasiew_nie_dubluje_uzytkownika_ani_czlonkostwa(
    baza_do_zasiewu, monkeypatch
):
    _ustaw_wlasciciela(monkeypatch)
    seed = _zaladuj_seed_dev()

    await seed.zasiej(baza_do_zasiewu)
    await seed.zasiej(baza_do_zasiewu)

    assert len(await _uzytkownicy(baza_do_zasiewu)) == 1
    assert len(await _czlonkostwa(baza_do_zasiewu)) == 1


async def test_powtorny_zasiew_z_innym_haslem_nie_nadpisuje_hasla_istniejacego_uzytkownika(
    baza_do_zasiewu, monkeypatch
):
    seed = _zaladuj_seed_dev()
    _ustaw_wlasciciela(monkeypatch, haslo=HASLO)
    await seed.zasiej(baza_do_zasiewu)
    _ustaw_wlasciciela(monkeypatch, haslo="zupelnie-inne-haslo-456")

    await seed.zasiej(baza_do_zasiewu)

    uzytkownik = (await _uzytkownicy(baza_do_zasiewu))[0]
    assert PasswordHasher().verify(uzytkownik.haslo_skrot, HASLO)
    assert not _haslo_pasuje(uzytkownik.haslo_skrot, "zupelnie-inne-haslo-456")


async def test_istniejacy_uzytkownik_z_innego_zespolu_dostaje_czlonkostwo_wlasciciela_w_zespole_runnera(
    baza_do_zasiewu, monkeypatch
):
    teraz = datetime.now(timezone.utc)
    stary_skrot = PasswordHasher().hash("stare-haslo-789")
    async with baza_do_zasiewu() as s:
        a = Tenant(name="A", created_at=teraz - timedelta(days=2))
        b = Tenant(name="B", created_at=teraz - timedelta(days=1))
        s.add_all([a, b])
        await s.flush()
        s.add(Runner(tenant_id=a.id, name="runner-a", token_hash="x"))
        u = User(email=EMAIL, haslo_skrot=stary_skrot)
        s.add(u)
        await s.flush()
        s.add(Membership(tenant_id=b.id, user_id=u.id, rola=RolaWZespole.member))
        await s.commit()
        id_a, id_b, id_u = a.id, b.id, u.id
    _ustaw_wlasciciela(monkeypatch)

    await _zaladuj_seed_dev().zasiej(baza_do_zasiewu)

    uzytkownicy = await _uzytkownicy(baza_do_zasiewu)
    assert [u.id for u in uzytkownicy] == [id_u]
    assert uzytkownicy[0].haslo_skrot == stary_skrot
    czlonkostwa = {c.tenant_id: c for c in await _czlonkostwa(baza_do_zasiewu)}
    assert set(czlonkostwa) == {id_a, id_b}
    assert czlonkostwa[id_a].rola == RolaWZespole.owner
    assert czlonkostwa[id_a].user_id == id_u
    assert czlonkostwa[id_b].rola == RolaWZespole.member   # cudze czlonkostwo bez zmian


async def test_istniejace_czlonkostwo_nie_traci_roli_przy_ponownym_zasiewie(
    baza_do_zasiewu, monkeypatch
):
    # Uzytkownik jest juz adminem w zespole runnera: zasiew nie dubluje ani nie degraduje.
    async with baza_do_zasiewu() as s:
        t = Tenant(name="A")
        s.add(t)
        await s.flush()
        s.add(Runner(tenant_id=t.id, name="runner-a", token_hash="x"))
        u = User(email=EMAIL, haslo_skrot=PasswordHasher().hash("stare-haslo-789"))
        s.add(u)
        await s.flush()
        s.add(Membership(tenant_id=t.id, user_id=u.id, rola=RolaWZespole.admin))
        await s.commit()
    _ustaw_wlasciciela(monkeypatch)

    await _zaladuj_seed_dev().zasiej(baza_do_zasiewu)

    czlonkostwa = await _czlonkostwa(baza_do_zasiewu)
    assert len(czlonkostwa) == 1
    assert czlonkostwa[0].rola == RolaWZespole.admin


async def test_adres_z_wielkimi_literami_i_spacjami_jest_normalizowany_przy_zasiewie(
    baza_do_zasiewu, monkeypatch
):
    seed = _zaladuj_seed_dev()
    _ustaw_wlasciciela(monkeypatch, email="  Dev@Example.COM ")
    await seed.zasiej(baza_do_zasiewu)
    assert [u.email for u in await _uzytkownicy(baza_do_zasiewu)] == [EMAIL]

    _ustaw_wlasciciela(monkeypatch, email=EMAIL)
    await seed.zasiej(baza_do_zasiewu)

    assert len(await _uzytkownicy(baza_do_zasiewu)) == 1
    assert len(await _czlonkostwa(baza_do_zasiewu)) == 1


async def test_adres_z_wielkimi_literami_nie_dubluje_istniejacego_uzytkownika(
    baza_do_zasiewu, monkeypatch
):
    seed = _zaladuj_seed_dev()
    _ustaw_wlasciciela(monkeypatch, email=EMAIL)
    await seed.zasiej(baza_do_zasiewu)

    _ustaw_wlasciciela(monkeypatch, email="  DEV@example.com ")
    await seed.zasiej(baza_do_zasiewu)

    assert len(await _uzytkownicy(baza_do_zasiewu)) == 1
    assert len(await _czlonkostwa(baza_do_zasiewu)) == 1


def test_dane_wlasciciela_zwraca_znormalizowany_adres_i_haslo_z_env(monkeypatch):
    _ustaw_wlasciciela(monkeypatch, email="  Dev@Example.COM ", haslo=HASLO)

    assert _zaladuj_seed_dev().dane_wlasciciela() == (EMAIL, HASLO)


@pytest.mark.parametrize(
    "email, haslo",
    [(EMAIL, None), (None, HASLO), ("", HASLO), (EMAIL, ""), (None, None), ("", "")],
)
def test_dane_wlasciciela_bez_adresu_lub_hasla_to_brak(monkeypatch, email, haslo):
    _ustaw_wlasciciela(monkeypatch, email=email, haslo=haslo)

    assert _zaladuj_seed_dev().dane_wlasciciela() is None


def test_dane_wlasciciela_zmieniaja_sie_razem_ze_srodowiskiem_procesu(monkeypatch):
    # Zrodlem jest os.environ czytany przy kazdym wywolaniu (nie migawka z importu).
    seed = _zaladuj_seed_dev()
    _ustaw_wlasciciela(monkeypatch, email=None, haslo=None)
    assert seed.dane_wlasciciela() is None

    monkeypatch.setenv("COCKPIT_DEV_EMAIL", EMAIL)
    monkeypatch.setenv("COCKPIT_DEV_PASSWORD", HASLO)

    assert seed.dane_wlasciciela() == (EMAIL, HASLO)


def _przygotuj_main(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(secrets, "CONFIG", tmp_path / "cfg" / "credentials.json")
    monkeypatch.delenv("COCKPIT_RUNNER_TOKEN", raising=False)
    url = f"sqlite+aiosqlite:///{tmp_path/'main.db'}"

    async def schemat():
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(schemat())
    monkeypatch.setattr(settings, "database_url", url)


def test_uruchomienie_zasiewu_z_wlascicielem_wypisuje_adres_ale_nie_haslo(
    tmp_path, monkeypatch, capsys
):
    _przygotuj_main(tmp_path, monkeypatch)
    _ustaw_wlasciciela(monkeypatch)

    _zaladuj_seed_dev().main()

    wyjscie = capsys.readouterr()
    assert f"Wlasciciel: {EMAIL}" in wyjscie.out
    assert HASLO not in wyjscie.out
    assert HASLO not in wyjscie.err


def test_uruchomienie_zasiewu_bez_zmiennych_mowi_ze_zasiew_jest_bez_uzytkownika(
    tmp_path, monkeypatch, capsys
):
    _przygotuj_main(tmp_path, monkeypatch)
    _ustaw_wlasciciela(monkeypatch, email=None, haslo=None)

    _zaladuj_seed_dev().main()

    wyjscie = capsys.readouterr()
    assert (
        "Brak COCKPIT_DEV_EMAIL lub COCKPIT_DEV_PASSWORD - zasiew bez uzytkownika"
        in wyjscie.out
    )
    assert "Wlasciciel:" not in wyjscie.out


def test_uruchomienie_zasiewu_z_samym_adresem_mowi_ze_zasiew_jest_bez_uzytkownika(
    tmp_path, monkeypatch, capsys
):
    _przygotuj_main(tmp_path, monkeypatch)
    _ustaw_wlasciciela(monkeypatch, email=EMAIL, haslo=None)

    _zaladuj_seed_dev().main()

    wyjscie = capsys.readouterr()
    assert "zasiew bez uzytkownika" in wyjscie.out
    assert "Wlasciciel:" not in wyjscie.out


def test_zasiew_jako_osobny_proces_nie_wypisuje_hasla_i_nie_przyjmuje_go_w_argumentach(
    tmp_path,
):
    url = f"sqlite+aiosqlite:///{tmp_path/'proces.db'}"

    async def schemat():
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(schemat())
    # Podproces importuje kod z KORZEN/src; pod mutmutem jest on zmutowany z
    # trampolina. MUTANT_UNDER_TEST ("stats" przy zbieraniu statystyk) kazalaby
    # trampolinie szukac konfiguracji mutmuta w cwd podprocesu (tmp_path) i padlaby.
    # Bez tej zmiennej podproces wola oryginalne funkcje - to sprawdzamy.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("COCKPIT_") and k != "MUTANT_UNDER_TEST"
    }
    env["PYTHONPATH"] = os.pathsep.join(
        [str(KORZEN / "src"), *filter(None, [env.get("PYTHONPATH")])]
    )
    env.update(
        COCKPIT_CONFIG=str(tmp_path / "credentials.json"),
        COCKPIT_DATABASE_URL=url,
        COCKPIT_DEV_EMAIL=EMAIL,
        COCKPIT_DEV_PASSWORD=HASLO,
    )

    wynik = subprocess.run(
        [sys.executable, str(KORZEN / "tools" / "seed_dev.py")],   # bez zadnych argumentow
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )

    assert wynik.returncode == 0, wynik.stderr[-1500:]
    assert f"Wlasciciel: {EMAIL}" in wynik.stdout
    assert HASLO not in wynik.stdout
    assert HASLO not in wynik.stderr


async def test_zasiew_zaklada_nowego_wlasciciela_obok_istniejacego_uzytkownika_o_innym_adresie(
    baza_do_zasiewu, monkeypatch
):
    stary_skrot = PasswordHasher().hash("stare-haslo-789")
    async with baza_do_zasiewu() as s:
        t = Tenant(name="A")
        s.add(t)
        await s.flush()
        s.add(Runner(tenant_id=t.id, name="runner-a", token_hash="x"))
        stary = User(email="ktos-inny@example.com", haslo_skrot=stary_skrot)
        s.add(stary)
        await s.flush()
        s.add(Membership(tenant_id=t.id, user_id=stary.id, rola=RolaWZespole.member))
        await s.commit()
        id_t, id_starego = t.id, stary.id
    _ustaw_wlasciciela(monkeypatch)

    await _zaladuj_seed_dev().zasiej(baza_do_zasiewu)

    uzytkownicy = {u.email: u for u in await _uzytkownicy(baza_do_zasiewu)}
    assert set(uzytkownicy) == {"ktos-inny@example.com", EMAIL}
    nowy = uzytkownicy[EMAIL]
    assert PasswordHasher().verify(nowy.haslo_skrot, HASLO)
    assert uzytkownicy["ktos-inny@example.com"].haslo_skrot == stary_skrot
    czlonkostwa = {c.user_id: c for c in await _czlonkostwa(baza_do_zasiewu)}
    assert czlonkostwa[nowy.id].rola == RolaWZespole.owner
    assert czlonkostwa[nowy.id].tenant_id == id_t
    assert czlonkostwa[id_starego].rola == RolaWZespole.member


async def test_wlasciciel_dostaje_wlasne_czlonkostwo_gdy_zespol_ma_juz_czlonka_innego_uzytkownika(
    baza_do_zasiewu, monkeypatch
):
    async with baza_do_zasiewu() as s:
        t = Tenant(name="A")
        s.add(t)
        await s.flush()
        s.add(Runner(tenant_id=t.id, name="runner-a", token_hash="x"))
        inny = User(email="ktos-inny@example.com", haslo_skrot=PasswordHasher().hash("x-haslo-123"))
        s.add(inny)
        await s.flush()
        s.add(Membership(tenant_id=t.id, user_id=inny.id, rola=RolaWZespole.admin))
        await s.commit()
        id_t, id_innego = t.id, inny.id
    _ustaw_wlasciciela(monkeypatch)

    await _zaladuj_seed_dev().zasiej(baza_do_zasiewu)

    async with baza_do_zasiewu() as s:
        wlasciciel = (await s.execute(select(User).where(User.email == EMAIL))).scalar_one()
        jego = (await s.execute(
            select(Membership).where(Membership.user_id == wlasciciel.id)
        )).scalars().all()
    assert [(c.tenant_id, c.rola) for c in jego] == [(id_t, RolaWZespole.owner)]
    czlonkostwa = await _czlonkostwa(baza_do_zasiewu)
    assert len(czlonkostwa) == 2
    assert next(c for c in czlonkostwa if c.user_id == id_innego).rola == RolaWZespole.admin
