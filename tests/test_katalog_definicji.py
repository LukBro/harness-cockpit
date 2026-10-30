"""HAR-67: katalog skilli i workflow w bazie, odswiezany przez runnera na zadanie.

Kokpit pokazuje liste takze wtedy, gdy runner jest offline - z data ostatniego odswiezenia.
W bazie leza tylko metadane; tresc briefu nie opuszcza maszyny uzytkownika (BRO-86).

Warstwy testow (jak w test_operacje_plikowe.py):
 1. operacja runnera `katalog_definicji` na prawdziwych plikach w katalogu testu;
 2. serwis kokpitu (`cockpit.services.katalog`) z wstrzykniegta wysylka; skutek czytany z bazy;
 3. przelot HTTP: prawdziwa aplikacja + prawdziwy `runner.client.Runner` w procesie testu.
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from transport_asgi import TransportASGIStrumieniujacy

from cockpit.api import app as app_mod
from cockpit.api import bus
from cockpit.db import session as db_session
from cockpit.db.models import Base, Definicja, Membership, RolaWZespole, Runner, Tenant, User
from cockpit.services import katalog
from cockpit.services import zapytania_runnera as zapytania
from cockpit.services.errors import RunnerOffline, StanNiemozliwy
from cockpit.settings import settings
from runner import operacje
from runner.client import Runner as RunnerKlient
from runner.zakres import Zakres

ZNACZNIK = "ZNACZNIK-BRIEFU-zxqv-7731"
HASLO = "Sekret-Hasl0-Alfa"
SKILL_RELATYWNIE = ".cockpit/skills/{k}/SKILL.md"


# =================================================================================
# Wspolne: pliki skilli i workflow w katalogu testu
# =================================================================================

@pytest.fixture
def projekt(tmp_path, monkeypatch):
    """Projekt w zakresie runnera i obcy katalog poza nim; HOME w katalogu testu."""
    baza = tmp_path.resolve()
    korzen = baza / "projekt"
    obcy = baza / "obcy"
    dom = baza / "dom"
    for k in (korzen, obcy, dom):
        k.mkdir()
    konfig = baza / "runner.json"
    konfig.write_text(json.dumps({"projekty": [str(korzen)]}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(dom))
    monkeypatch.setenv("COCKPIT_RUNNER_CONFIG", str(konfig))
    monkeypatch.delenv("COCKPIT_MAX_READ_KB", raising=False)
    return SimpleNamespace(korzen=korzen, obcy=obcy, dom=dom, konfig=konfig, baza=baza)


def _pisz(sciezka: Path, tresc: str | bytes) -> Path:
    sciezka.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(tresc, bytes):
        sciezka.write_bytes(tresc)
    else:
        sciezka.write_text(tresc, encoding="utf-8")
    return sciezka


def _plik_skilla(baza: Path, podkatalog: str, katalog_skilla: str, *, role: str = "author",
                 brief: str = f"Brief skilla. {ZNACZNIK}\n") -> Path:
    tresc = f"---\nid: {katalog_skilla}\nname: Skill {katalog_skilla}\nrole: {role}\n---\n{brief}"
    return _pisz(baza / podkatalog / katalog_skilla / "SKILL.md", tresc)


async def _wyk() -> dict:
    return await operacje.wykonaj({"type": "katalog_definicji", "req_id": "r-1"})


async def _projekty() -> list[dict]:
    odp = await _wyk()
    assert odp["ok"] is True, odp
    return odp["wynik"]["projekty"]


def _kat_skilli(wpis: dict) -> list[tuple[str, str, str]]:
    return [(s["zrodlo"], s["podkatalog"], s["katalog"]) for s in wpis["skille"]]


# =================================================================================
# 1. Runner: operacja `katalog_definicji`
# =================================================================================

def test_runner_zna_operacje_katalogu_definicji():
    assert "katalog_definicji" in operacje.OPERACJE


def test_limit_pozycji_katalogu_to_dwa_tysiace():
    assert operacje.LIMIT_DEFINICJI == 2000


async def test_projekt_bez_skilli_i_workflow_jest_na_liscie_z_pustymi_zbiorami(projekt):
    assert await _projekty() == [
        {"projekt": str(projekt.korzen), "skille": [], "workflow": []}]


async def test_skille_ze_wszystkich_czterech_zrodel_sa_w_kolejnosci_priorytetu(projekt):
    _plik_skilla(projekt.korzen, ".agents/skills", "d")
    _plik_skilla(projekt.dom, ".cockpit/skills", "e")
    _plik_skilla(projekt.korzen, ".claude/skills", "c")
    _plik_skilla(projekt.korzen, ".cockpit/skills", "b")
    _plik_skilla(projekt.korzen, ".cockpit/skills", "a")

    [wpis] = await _projekty()

    assert _kat_skilli(wpis) == [
        ("repo", ".cockpit/skills", "a"),
        ("repo", ".cockpit/skills", "b"),
        ("repo", ".claude/skills", "c"),
        ("repo", ".agents/skills", "d"),
        ("dom", ".cockpit/skills", "e"),
    ]
    assert [s["sciezka"] for s in wpis["skille"]] == [
        ".cockpit/skills/a/SKILL.md",
        ".cockpit/skills/b/SKILL.md",
        ".claude/skills/c/SKILL.md",
        ".agents/skills/d/SKILL.md",
        ".cockpit/skills/e/SKILL.md",
    ]


async def test_skill_niesie_frontmatter_a_tresc_briefu_nie_wychodzi_z_runnera(projekt):
    _pisz(projekt.korzen / ".cockpit/skills/x/SKILL.md",
          "---\nid: x\nname: Skill X\nrole: reviewer\n---\n"
          f"Pierwsza linia briefu {ZNACZNIK}\n---\nPo drugiej granicy: {ZNACZNIK}\n")

    odp = await _wyk()

    [skill] = odp["wynik"]["projekty"][0]["skille"]
    assert "id: x" in skill["frontmatter"]
    assert "name: Skill X" in skill["frontmatter"]
    assert "role: reviewer" in skill["frontmatter"]
    assert "Pierwsza linia briefu" not in skill["frontmatter"]
    assert ZNACZNIK not in json.dumps(odp)


async def test_skill_ze_zrodla_domowego_ma_zrodlo_dom_i_sciezke_wzgledem_domu(projekt):
    _plik_skilla(projekt.dom, ".cockpit/skills", "globalny")

    [wpis] = await _projekty()

    [skill] = wpis["skille"]
    assert skill["zrodlo"] == "dom"
    assert skill["podkatalog"] == ".cockpit/skills"
    assert skill["katalog"] == "globalny"
    assert skill["sciezka"] == ".cockpit/skills/globalny/SKILL.md"
    assert str(projekt.dom) not in json.dumps(wpis)


async def test_katalog_bez_pliku_skilla_nie_jest_skillem(projekt):
    (projekt.korzen / ".cockpit/skills/pusty").mkdir(parents=True)
    _plik_skilla(projekt.korzen, ".cockpit/skills", "prawdziwy")

    [wpis] = await _projekty()

    assert _kat_skilli(wpis) == [("repo", ".cockpit/skills", "prawdziwy")]


async def test_workflow_to_tylko_pliki_yaml_posortowane_i_bez_tresci(projekt):
    katalog_wf = projekt.korzen / ".cockpit/workflows"
    _pisz(katalog_wf / "zeta.yaml", f"name: zeta\n# {ZNACZNIK}\n")
    _pisz(katalog_wf / "alfa.yaml", f"name: alfa\n# {ZNACZNIK}\n")
    _pisz(katalog_wf / "inny.yml", f"name: inny\n# {ZNACZNIK}\n")
    _pisz(katalog_wf / "notatka.txt", ZNACZNIK)
    (katalog_wf / "podkatalog.yaml").mkdir()

    odp = await _wyk()

    [wpis] = odp["wynik"]["projekty"]
    assert wpis["workflow"] == [
        {"sciezka": ".cockpit/workflows/alfa.yaml"},
        {"sciezka": ".cockpit/workflows/zeta.yaml"},
    ]
    assert ZNACZNIK not in json.dumps(odp)


async def test_projekt_spoza_konfiguracji_runnera_jest_niewidoczny(projekt):
    _plik_skilla(projekt.korzen, ".cockpit/skills", "swoj")
    _plik_skilla(projekt.obcy, ".cockpit/skills", "cudzy")
    _pisz(projekt.obcy / ".cockpit/workflows/cudzy.yaml", "name: cudzy\n")

    odp = await _wyk()

    assert [w["projekt"] for w in odp["wynik"]["projekty"]] == [str(projekt.korzen)]
    assert "cudzy" not in json.dumps(odp)


async def test_kazdy_projekt_z_konfiguracji_ma_wlasny_wpis_ze_swoimi_skillami(projekt):
    drugi = projekt.baza / "drugi"
    drugi.mkdir()
    projekt.konfig.write_text(
        json.dumps({"projekty": [str(projekt.korzen), str(drugi)]}), encoding="utf-8")
    _plik_skilla(projekt.korzen, ".cockpit/skills", "pierwszy")
    _plik_skilla(drugi, ".cockpit/skills", "drugi")
    _pisz(drugi / ".cockpit/workflows/wf-drugi.yaml", "name: wf\n")

    wpisy = {w["projekt"]: w for w in await _projekty()}

    assert set(wpisy) == {str(projekt.korzen), str(drugi)}
    assert _kat_skilli(wpisy[str(projekt.korzen)]) == [("repo", ".cockpit/skills", "pierwszy")]
    assert _kat_skilli(wpisy[str(drugi)]) == [("repo", ".cockpit/skills", "drugi")]
    assert wpisy[str(projekt.korzen)]["workflow"] == []
    assert wpisy[str(drugi)]["workflow"] == [{"sciezka": ".cockpit/workflows/wf-drugi.yaml"}]


def test_zakres_projektow_nie_zawiera_rejestru_domowego_choc_zakres_plikow_go_ma(projekt):
    zakres = Zakres.z_konfiguracji()

    assert zakres.projekty == (projekt.korzen,)
    # kontrast: rejestr domowy jest w zakresie plikow, tylko nie jest projektem
    assert projekt.dom / ".cockpit" / "skills" in zakres.korzenie


@pytest.mark.parametrize(
    "tresc",
    [
        b"---\nid: x\nname: X\nrole: author\n---\nbrief \xff\xfe " + ZNACZNIK.encode() + b"\n",
        f"Sam brief bez frontmattera {ZNACZNIK}\n".encode(),
        f"---\nid: x\nname: X\nrole: author\n{ZNACZNIK}\n".encode(),
    ],
    ids=["nie_utf8", "bez_frontmattera", "niezamkniety_frontmatter"],
)
async def test_skill_ktorego_nie_da_sie_podzielic_ma_frontmatter_null_i_nie_zdradza_tresci(
    projekt, tresc,
):
    _pisz(projekt.korzen / ".cockpit/skills/x/SKILL.md", tresc)

    odp = await _wyk()

    [skill] = odp["wynik"]["projekty"][0]["skille"]
    assert skill["katalog"] == "x"
    assert skill["frontmatter"] is None
    assert ZNACZNIK not in json.dumps(odp)


async def test_plik_skilla_wiekszy_niz_limit_odczytu_ma_frontmatter_null(projekt, monkeypatch):
    monkeypatch.setenv("COCKPIT_MAX_READ_KB", "1")
    _pisz(projekt.korzen / ".cockpit/skills/duzy/SKILL.md",
          "---\nid: duzy\nname: D\nrole: author\n---\n" + "a" * 2000 + ZNACZNIK)

    odp = await _wyk()

    [skill] = odp["wynik"]["projekty"][0]["skille"]
    assert skill["frontmatter"] is None
    assert ZNACZNIK not in json.dumps(odp)


async def test_plik_skilla_rowny_limitowi_odczytu_jest_czytany_a_o_bajt_wiekszy_nie(
    projekt, monkeypatch,
):
    monkeypatch.setenv("COCKPIT_MAX_READ_KB", "1")
    naglowek = "---\nid: {k}\nname: N\nrole: author\n---\n"
    for nazwa, dopelnienie in (("rowny", 0), ("wiekszy", 1)):
        tresc = naglowek.format(k=nazwa)
        tresc += "a" * (1024 - len(tresc.encode()) + dopelnienie)
        _pisz(projekt.korzen / ".cockpit/skills" / nazwa / "SKILL.md", tresc)

    [wpis] = await _projekty()

    fm = {s["katalog"]: s["frontmatter"] for s in wpis["skille"]}
    assert fm["rowny"] is not None and "id: rowny" in fm["rowny"]
    assert fm["wiekszy"] is None


async def test_skill_przez_dowiazanie_poza_zakres_runnera_jest_pominiety_bez_bledu(projekt):
    cudzy = _pisz(projekt.obcy / "cudzy.md",
                  f"---\nid: wyciek\nname: W\nrole: author\n---\n{ZNACZNIK}\n")
    (projekt.korzen / ".cockpit/skills/wyciek").mkdir(parents=True)
    (projekt.korzen / ".cockpit/skills/wyciek/SKILL.md").symlink_to(cudzy)
    _plik_skilla(projekt.korzen, ".cockpit/skills", "swoj")

    odp = await _wyk()

    assert odp["ok"] is True
    [wpis] = odp["wynik"]["projekty"]
    assert _kat_skilli(wpis) == [("repo", ".cockpit/skills", "swoj")]
    assert "wyciek" not in json.dumps(odp)
    assert ZNACZNIK not in json.dumps(odp)


async def test_dokladnie_tyle_pozycji_co_limit_przechodzi(projekt, monkeypatch):
    monkeypatch.setattr(operacje, "LIMIT_DEFINICJI", 3)
    _plik_skilla(projekt.korzen, ".cockpit/skills", "a")
    _plik_skilla(projekt.korzen, ".claude/skills", "b")
    _pisz(projekt.korzen / ".cockpit/workflows/w.yaml", "name: w\n")

    odp = await _wyk()

    assert odp["ok"] is True, odp
    [wpis] = odp["wynik"]["projekty"]
    assert len(wpis["skille"]) + len(wpis["workflow"]) == 3


async def test_o_jedna_pozycje_wiecej_niz_limit_to_blad_a_nie_obciety_katalog(
    projekt, monkeypatch,
):
    monkeypatch.setattr(operacje, "LIMIT_DEFINICJI", 3)
    _plik_skilla(projekt.korzen, ".cockpit/skills", "a")
    _plik_skilla(projekt.korzen, ".claude/skills", "b")
    _pisz(projekt.korzen / ".cockpit/workflows/w1.yaml", "name: w\n")
    _pisz(projekt.korzen / ".cockpit/workflows/w2.yaml", "name: w\n")

    odp = await _wyk()

    assert odp["ok"] is False
    assert odp["kod"] == "za_duzo"
    assert odp["req_id"] == "r-1"
    assert "wynik" not in odp
    assert isinstance(odp["blad"], str) and odp["blad"]


async def test_limit_pozycji_liczy_laczenie_wszystkie_projekty(projekt, monkeypatch):
    drugi = projekt.baza / "drugi"
    drugi.mkdir()
    projekt.konfig.write_text(
        json.dumps({"projekty": [str(projekt.korzen), str(drugi)]}), encoding="utf-8")
    monkeypatch.setattr(operacje, "LIMIT_DEFINICJI", 3)
    _plik_skilla(projekt.korzen, ".cockpit/skills", "a")
    _plik_skilla(projekt.korzen, ".cockpit/skills", "b")
    _plik_skilla(drugi, ".cockpit/skills", "c")
    _plik_skilla(drugi, ".cockpit/skills", "d")   # po dwie w kazdym projekcie: razem 4 > 3

    odp = await _wyk()

    assert odp["ok"] is False
    assert odp["kod"] == "za_duzo"


# =================================================================================
# 2. Serwis kokpitu: `cockpit.services.katalog`
# =================================================================================

@pytest.fixture
async def zespoly(tmp_path):
    """Dwa zespoly; kazdy z jednym swiezym runnerem. Baza tylko dla serwisu (bez aplikacji)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'serwis.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with maker() as s:
        a, b = Tenant(name="Alfa"), Tenant(name="Beta")
        s.add(a)
        await s.flush()
        s.add(b)
        await s.flush()
        teraz = datetime.now(timezone.utc)
        s.add(Runner(id="runner-a", tenant_id=a.id, name="a", token_hash="x", last_seen_at=teraz))
        s.add(Runner(id="runner-b", tenant_id=b.id, name="b", token_hash="y", last_seen_at=teraz))
        await s.commit()
        ids = SimpleNamespace(a=a.id, b=b.id)
    yield SimpleNamespace(maker=maker, ids=ids)
    await engine.dispose()


class _Wysylka:
    """Wstrzykiwana `wyslij`: zapamietuje wywolania i oddaje zadana odpowiedz."""

    def __init__(self, odpowiedz: dict) -> None:
        self.odpowiedz = odpowiedz
        self.wywolania: list[tuple] = []

    async def __call__(self, runner_id, typ, dane, timeout_s):
        self.wywolania.append((runner_id, typ, dane))
        return self.odpowiedz


POLACZONE_A = frozenset({"runner-a"})
POLACZONE_OBA = frozenset({"runner-a", "runner-b"})


def _fm(id_skilla: str, *, role: str = "author", nazwa: str | None = None) -> str:
    return f"id: {id_skilla}\nname: {nazwa or 'Skill ' + id_skilla}\nrole: {role}\n"


def _skill(katalog_skilla: str, frontmatter: str | None, *, zrodlo: str = "repo",
           podkatalog: str = ".cockpit/skills") -> dict:
    return {"zrodlo": zrodlo, "podkatalog": podkatalog, "katalog": katalog_skilla,
            "sciezka": f"{podkatalog}/{katalog_skilla}/SKILL.md", "frontmatter": frontmatter}


def _wpis(projekt: str, skille: list[dict] | None = None, workflow: list[str] | None = None) -> dict:
    return {"projekt": projekt, "skille": skille or [],
            "workflow": [{"sciezka": f".cockpit/workflows/{n}.yaml"} for n in (workflow or [])]}


def _odp(*wpisy: dict) -> dict:
    return {"ok": True, "wynik": {"projekty": list(wpisy)}}


async def _odswiez(zespoly, tenant_id, odpowiedz, polaczone=POLACZONE_A):
    wysylka = _Wysylka(odpowiedz)
    async with zespoly.maker() as s:
        wynik = await katalog.odswiez_katalog(s, tenant_id, polaczone, wysylka)
    return wynik, wysylka


async def _odswiez_blad(zespoly, tenant_id, odpowiedz, polaczone=POLACZONE_A):
    wysylka = _Wysylka(odpowiedz)
    async with zespoly.maker() as s:
        try:
            await katalog.odswiez_katalog(s, tenant_id, polaczone, wysylka)
        except Exception as e:
            return e, wysylka
    pytest.fail("odswiezenie nie zglosilo bledu")


async def _wiersze(zespoly) -> list[dict]:
    """Wszystkie wiersze `definicja` w bazie (wszystkie zespoly), posortowane deterministycznie."""
    async with zespoly.maker() as s:
        wiersze = (await s.execute(select(Definicja))).scalars().all()
    wynik = [{c: getattr(w, c) for c in (
        "id", "tenant_id", "runner_id", "projekt", "rodzaj", "klucz", "nazwa", "rola",
        "sciezka", "odswiezono_at")} for w in wiersze]
    return sorted(wynik, key=lambda w: (w["tenant_id"], w["runner_id"], w["projekt"],
                                        w["rodzaj"], w["klucz"]))


def _tresc(wiersze: list[dict]) -> list[dict]:
    """Wiersze bez id i daty - to, co mowi katalog, a nie kiedy go zapisano."""
    return [{k: v for k, v in w.items() if k not in ("id", "odswiezono_at")} for w in wiersze]


async def _wstaw(zespoly, tenant_id, runner_id, odswiezono_at, *, projekt="/p", rodzaj="skill",
                 klucz="x", nazwa="Nazwa", rola="author", sciezka=None) -> str:
    id_wiersza = str(uuid.uuid4())
    async with zespoly.maker() as s:
        s.add(Definicja(
            id=id_wiersza, tenant_id=tenant_id, runner_id=runner_id, projekt=projekt,
            rodzaj=rodzaj, klucz=klucz, nazwa=nazwa, rola=rola,
            sciezka=sciezka or f".cockpit/skills/{klucz}/SKILL.md", odswiezono_at=odswiezono_at))
        await s.commit()
    return id_wiersza


async def _dodaj_runnera(zespoly, runner_id: str, tenant_id: str) -> None:
    async with zespoly.maker() as s:
        s.add(Runner(id=runner_id, tenant_id=tenant_id, name=runner_id, token_hash="z",
                     last_seen_at=datetime.now(timezone.utc)))
        await s.commit()


STARA_DATA = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


# ---- zapis metadanych ---------------------------------------------------------------

async def test_odswiezenie_zapisuje_w_bazie_skille_i_workflow_bez_tresci_briefu(zespoly):
    odp = _odp(_wpis("/p/alfa",
                     skille=[_skill("x", _fm("x", role="reviewer", nazwa="Recenzent X"))],
                     workflow=["wdrozenie"]))

    wynik, wysylka = await _odswiez(zespoly, zespoly.ids.a, odp)

    assert wysylka.wywolania == [("runner-a", "katalog_definicji", {})]
    assert _tresc(await _wiersze(zespoly)) == [
        {"tenant_id": zespoly.ids.a, "runner_id": "runner-a", "projekt": "/p/alfa",
         "rodzaj": "skill", "klucz": "x", "nazwa": "Recenzent X", "rola": "reviewer",
         "sciezka": ".cockpit/skills/x/SKILL.md"},
        {"tenant_id": zespoly.ids.a, "runner_id": "runner-a", "projekt": "/p/alfa",
         "rodzaj": "workflow", "klucz": "wdrozenie", "nazwa": "wdrozenie", "rola": None,
         "sciezka": ".cockpit/workflows/wdrozenie.yaml"},
    ]
    assert wynik["pominiete"] == 0


async def test_odswiezenie_oddaje_ten_sam_katalog_co_odczyt_plus_liczbe_pominietych(zespoly):
    odp = _odp(_wpis("/p/alfa", skille=[_skill("x", _fm("x")), _skill("zly", "to nie yaml: [")]))

    wynik, _ = await _odswiez(zespoly, zespoly.ids.a, odp)

    async with zespoly.maker() as s:
        odczyt = await katalog.katalog(s, zespoly.ids.a)
    assert wynik == {**odczyt, "pominiete": 1}
    assert [d["klucz"] for d in wynik["definicje"]] == ["x"]


async def test_wszystkie_wiersze_jednego_odswiezenia_maja_te_sama_swieza_date(zespoly):
    przed = datetime.now(timezone.utc) - timedelta(seconds=1)
    odp = _odp(_wpis("/p/alfa", skille=[_skill("x", _fm("x"))], workflow=["w"]),
               _wpis("/p/beta", skille=[_skill("y", _fm("y"))]))

    wynik, _ = await _odswiez(zespoly, zespoly.ids.a, odp)

    po = datetime.now(timezone.utc) + timedelta(seconds=1)
    daty = {d["odswiezono_at"] for d in wynik["definicje"]}
    assert len(wynik["definicje"]) == 3
    assert len(daty) == 1
    assert daty == {wynik["odswiezono_at"]}
    odswiezono = datetime.fromisoformat(wynik["odswiezono_at"])
    assert przed <= odswiezono <= po


async def test_data_odswiezenia_jest_iso_z_przesunieciem_utc(zespoly):
    wynik, _ = await _odswiez(zespoly, zespoly.ids.a,
                              _odp(_wpis("/p", skille=[_skill("x", _fm("x"))])))

    assert wynik["odswiezono_at"].endswith("+00:00")
    assert wynik["definicje"][0]["odswiezono_at"].endswith("+00:00")


async def test_tresc_briefu_nie_trafia_do_zadnej_kolumny(zespoly):
    # runner wysyla tylko frontmatter; nieznany klucz z briefem nie moze sie przedostac do bazy
    fm = _fm("x") + f"opis: {ZNACZNIK}\n"
    await _odswiez(zespoly, zespoly.ids.a, _odp(_wpis("/p", skille=[_skill("x", fm)])))

    wiersze = await _wiersze(zespoly)
    assert len(wiersze) == 1
    assert ZNACZNIK not in json.dumps(wiersze, default=str)


# ---- przeslanianie ------------------------------------------------------------------

async def test_ten_sam_skill_w_repo_i_w_domu_to_jeden_wiersz_z_repo(zespoly):
    odp = _odp(_wpis("/p", skille=[
        _skill("x", _fm("x", nazwa="Z repo")),
        _skill("x", _fm("x", nazwa="Z domu"), zrodlo="dom"),
    ]))

    await _odswiez(zespoly, zespoly.ids.a, odp)

    assert _tresc(await _wiersze(zespoly)) == [
        {"tenant_id": zespoly.ids.a, "runner_id": "runner-a", "projekt": "/p",
         "rodzaj": "skill", "klucz": "x", "nazwa": "Z repo", "rola": "author",
         "sciezka": ".cockpit/skills/x/SKILL.md"}]


async def test_skill_ze_zrodla_konwencyjnego_przeslania_ten_z_domu(zespoly):
    odp = _odp(_wpis("/p", skille=[
        _skill("x", "role: reviewer\nname: Z claude\n", podkatalog=".claude/skills"),
        _skill("x", _fm("x", nazwa="Z domu"), zrodlo="dom"),
    ]))

    await _odswiez(zespoly, zespoly.ids.a, odp)

    [w] = _tresc(await _wiersze(zespoly))
    assert (w["nazwa"], w["rola"], w["sciezka"]) == (
        "Z claude", "reviewer", ".claude/skills/x/SKILL.md")


async def test_skill_tylko_w_domu_ma_sciezke_zaczynajaca_sie_od_tyldy(zespoly):
    odp = _odp(_wpis("/p", skille=[_skill("g", _fm("g"), zrodlo="dom")]))

    await _odswiez(zespoly, zespoly.ids.a, odp)

    [w] = await _wiersze(zespoly)
    assert w["sciezka"] == "~/.cockpit/skills/g/SKILL.md"


async def test_skill_z_repo_nie_dostaje_tyldy_w_sciezce(zespoly):
    odp = _odp(_wpis("/p", skille=[_skill("r", _fm("r"))]))

    await _odswiez(zespoly, zespoly.ids.a, odp)

    [w] = await _wiersze(zespoly)
    assert w["sciezka"] == ".cockpit/skills/r/SKILL.md"


async def test_przeslanianie_dziala_w_obrebie_projektu_a_skill_globalny_wchodzi_do_kazdego(zespoly):
    odp = _odp(
        _wpis("/p/pierwszy", skille=[
            _skill("x", _fm("x", nazwa="Z repo pierwszego")),
            _skill("x", _fm("x", nazwa="Z domu"), zrodlo="dom")]),
        _wpis("/p/drugi", skille=[_skill("x", _fm("x", nazwa="Z domu"), zrodlo="dom")]),
    )

    await _odswiez(zespoly, zespoly.ids.a, odp)

    wiersze = {w["projekt"]: w for w in await _wiersze(zespoly)}
    assert set(wiersze) == {"/p/pierwszy", "/p/drugi"}
    assert wiersze["/p/pierwszy"]["sciezka"] == ".cockpit/skills/x/SKILL.md"
    assert wiersze["/p/pierwszy"]["nazwa"] == "Z repo pierwszego"
    assert wiersze["/p/drugi"]["sciezka"] == "~/.cockpit/skills/x/SKILL.md"
    assert wiersze["/p/drugi"]["nazwa"] == "Z domu"


# ---- skille zepsute nie przerywaja odswiezenia ---------------------------------------

async def test_skill_niescisly_bez_roli_to_nie_skill_kokpitu_i_jest_pominiety(zespoly):
    odp = _odp(_wpis("/p", skille=[
        _skill("cudzy", "name: Cudzy skill innego narzedzia\n", podkatalog=".claude/skills"),
        _skill("nasz", _fm("nasz")),
    ]))

    wynik, _ = await _odswiez(zespoly, zespoly.ids.a, odp)

    assert [w["klucz"] for w in await _wiersze(zespoly)] == ["nasz"]
    assert wynik["pominiete"] == 1


async def test_skill_niescisly_z_rola_dostaje_id_i_nazwe_z_nazwy_katalogu(zespoly):
    odp = _odp(_wpis("/p", skille=[
        _skill("konwencja", "role: executor\n", podkatalog=".agents/skills")]))

    wynik, _ = await _odswiez(zespoly, zespoly.ids.a, odp)

    assert _tresc(await _wiersze(zespoly)) == [
        {"tenant_id": zespoly.ids.a, "runner_id": "runner-a", "projekt": "/p",
         "rodzaj": "skill", "klucz": "konwencja", "nazwa": "konwencja", "rola": "executor",
         "sciezka": ".agents/skills/konwencja/SKILL.md"}]
    assert wynik["pominiete"] == 0


ZEPSUTE_FRONTMATTERY = {
    "nieznana_rola": _fm("zly", role="czarodziej"),
    "brak_roli": "id: zly\nname: Zly\n",
    "brak_nazwy": "id: zly\nrole: author\n",
    "brak_id": "name: Zly\nrole: author\n",
    "id_rozne_od_katalogu": _fm("inny-id"),
    "zly_yaml": "id: zly\nname: [niezamkniete\nrole: author\n",
    "frontmatter_nie_jest_mapa": "- id\n- name\n",
    "nazwa_dluzsza_niz_kolumna": _fm("zly", nazwa="N" * 201),
    "brak_frontmattera_u_runnera": None,
    "niepoprawna_data_w_frontmatterze": _fm("zly") + "utworzono: 2024-02-30\n",
    "data_poza_zakresem_w_frontmatterze": (
        _fm("zly") + "utworzono: 9999-12-31T23:59:59.999999999-23:59\n"),
    "klucz_zlozony_w_frontmatterze": _fm("zly") + "? [a, {b: c}]\n: 1\n",
}


@pytest.mark.parametrize("frontmatter", ZEPSUTE_FRONTMATTERY.values(),
                         ids=ZEPSUTE_FRONTMATTERY.keys())
async def test_zepsuty_skill_scisly_jest_pomijany_a_reszta_katalogu_zapisana(
    zespoly, frontmatter,
):
    odp = _odp(_wpis("/p", skille=[_skill("zly", frontmatter), _skill("dobry", _fm("dobry"))]))

    wynik, _ = await _odswiez(zespoly, zespoly.ids.a, odp)

    assert [w["klucz"] for w in await _wiersze(zespoly)] == ["dobry"]
    assert wynik["pominiete"] == 1


async def test_nazwa_skilla_rowna_dlugosci_kolumny_jest_zapisana(zespoly):
    odp = _odp(_wpis("/p", skille=[_skill("x", _fm("x", nazwa="N" * 200))]))

    wynik, _ = await _odswiez(zespoly, zespoly.ids.a, odp)

    [w] = await _wiersze(zespoly)
    assert w["nazwa"] == "N" * 200
    assert wynik["pominiete"] == 0


async def test_kazdy_zepsuty_skill_jest_liczony_w_pominietych(zespoly):
    odp = _odp(_wpis("/p", skille=[
        _skill("a", None), _skill("b", _fm("b", role="nieznana")), _skill("c", _fm("c"))]))

    wynik, _ = await _odswiez(zespoly, zespoly.ids.a, odp)

    assert wynik["pominiete"] == 2


# ---- odswiezenie zastepuje stary stan -------------------------------------------------

async def test_drugie_odswiezenie_po_usunieciu_skilla_usuwa_jego_wiersz_z_bazy(zespoly):
    pelny = _odp(_wpis("/p", skille=[_skill("x", _fm("x")), _skill("y", _fm("y"))],
                       workflow=["w"]))
    ubogi = _odp(_wpis("/p", skille=[_skill("x", _fm("x"))]))
    await _odswiez(zespoly, zespoly.ids.a, pelny)
    assert {w["klucz"] for w in await _wiersze(zespoly)} == {"x", "y", "w"}   # kontrast

    await _odswiez(zespoly, zespoly.ids.a, ubogi)

    assert [(w["rodzaj"], w["klucz"]) for w in await _wiersze(zespoly)] == [("skill", "x")]


async def test_odswiezenie_nie_dubluje_wierszy_przy_niezmienionych_plikach(zespoly):
    odp = _odp(_wpis("/p", skille=[_skill("x", _fm("x"))], workflow=["w"]))

    await _odswiez(zespoly, zespoly.ids.a, odp)
    await _odswiez(zespoly, zespoly.ids.a, odp)

    assert len(await _wiersze(zespoly)) == 2


async def test_odswiezenie_przesuwa_date_ostatniego_odswiezenia(zespoly):
    await _wstaw(zespoly, zespoly.ids.a, "runner-a", STARA_DATA)

    wynik, _ = await _odswiez(zespoly, zespoly.ids.a,
                              _odp(_wpis("/p", skille=[_skill("x", _fm("x"))])))

    assert datetime.fromisoformat(wynik["odswiezono_at"]) > STARA_DATA


async def test_odswiezenie_do_pustego_wyniku_runnera_oproznia_katalog_gdy_runner_naprawde_nic_nie_ma(
    zespoly,
):
    await _wstaw(zespoly, zespoly.ids.a, "runner-a", STARA_DATA)

    wynik, _ = await _odswiez(zespoly, zespoly.ids.a, _odp(_wpis("/p")))

    assert await _wiersze(zespoly) == []
    assert wynik["definicje"] == []
    assert wynik["odswiezono_at"] is None   # brak wierszy = brak daty, nie "teraz"


# ---- izolacja zespolow i runnerow -------------------------------------------------------

async def test_odswiezenie_zespolu_a_nie_dotyka_wierszy_zespolu_b(zespoly):
    wspolny_projekt = "/p"
    await _odswiez(zespoly, zespoly.ids.b, _odp(_wpis(wspolny_projekt, skille=[
        _skill("x", _fm("x", nazwa="Beta X"))])), polaczone=POLACZONE_OBA)
    przed_b = [w for w in await _wiersze(zespoly) if w["tenant_id"] == zespoly.ids.b]
    assert len(przed_b) == 1   # kontrast: B ma wiersz o tym samym projekcie i kluczu

    await _odswiez(zespoly, zespoly.ids.a, _odp(_wpis(wspolny_projekt, skille=[
        _skill("x", _fm("x", nazwa="Alfa X"))])), polaczone=POLACZONE_OBA)
    await _odswiez(zespoly, zespoly.ids.a, _odp(_wpis(wspolny_projekt)), polaczone=POLACZONE_OBA)

    po = await _wiersze(zespoly)
    assert po == przed_b   # A odswiezone dwa razy (w tym do pustego), B bez jednej zmiany
    assert po[0]["nazwa"] == "Beta X"


async def test_odswiezenie_zapisuje_wiersze_zespolu_i_runnera_ktory_odpowiedzial(zespoly):
    await _odswiez(zespoly, zespoly.ids.b, _odp(_wpis("/p", skille=[_skill("x", _fm("x"))])),
                   polaczone=POLACZONE_OBA)

    [w] = await _wiersze(zespoly)
    assert (w["tenant_id"], w["runner_id"]) == (zespoly.ids.b, "runner-b")


async def test_odswiezenie_nie_kasuje_wierszy_innego_runnera_tego_samego_zespolu(zespoly):
    await _dodaj_runnera(zespoly, "runner-a2", zespoly.ids.a)
    obcy_wiersz = await _wstaw(zespoly, zespoly.ids.a, "runner-a2", STARA_DATA, klucz="x")

    await _odswiez(zespoly, zespoly.ids.a, _odp(_wpis("/p")), polaczone=POLACZONE_A)

    wiersze = await _wiersze(zespoly)
    assert [w["id"] for w in wiersze] == [obcy_wiersz]
    assert wiersze[0]["runner_id"] == "runner-a2"


async def test_katalog_zespolu_a_nie_zawiera_wpisow_zespolu_b(zespoly):
    teraz = datetime.now(timezone.utc)
    await _wstaw(zespoly, zespoly.ids.a, "runner-a", teraz, klucz="alfa-skill")
    await _wstaw(zespoly, zespoly.ids.b, "runner-b", teraz - timedelta(days=30), klucz="beta-skill")

    async with zespoly.maker() as s:
        a = await katalog.katalog(s, zespoly.ids.a)
        b = await katalog.katalog(s, zespoly.ids.b)

    assert [d["klucz"] for d in a["definicje"]] == ["alfa-skill"]
    assert [d["klucz"] for d in b["definicje"]] == ["beta-skill"]
    # data zespolu A pochodzi z jego wierszy, nie z (starszego) wiersza zespolu B
    assert datetime.fromisoformat(a["odswiezono_at"]) > teraz - timedelta(days=1)


# ---- runner offline / zla odpowiedz ------------------------------------------------------

async def test_runner_offline_daje_runner_offline_a_wczesniejszy_katalog_zostaje(zespoly):
    await _wstaw(zespoly, zespoly.ids.a, "runner-a", STARA_DATA)
    przed = await _wiersze(zespoly)

    blad, wysylka = await _odswiez_blad(
        zespoly, zespoly.ids.a, _odp(_wpis("/p")), polaczone=frozenset())

    assert type(blad) is RunnerOffline
    assert wysylka.wywolania == []
    assert await _wiersze(zespoly) == przed


async def test_runner_innego_zespolu_nie_odswieza_katalogu_zespolu_a(zespoly):
    await _wstaw(zespoly, zespoly.ids.a, "runner-a", STARA_DATA)
    przed = await _wiersze(zespoly)

    blad, wysylka = await _odswiez_blad(
        zespoly, zespoly.ids.a, _odp(_wpis("/p")), polaczone=frozenset({"runner-b"}))

    assert type(blad) is RunnerOffline
    assert wysylka.wywolania == []
    assert await _wiersze(zespoly) == przed


ZLE_ODPOWIEDZI = {
    "wynik_pusty_slownik": {"ok": True, "wynik": {}},
    "wynik_none": {"ok": True, "wynik": None},
    "wynik_nie_slownik": {"ok": True, "wynik": []},
    "projekty_slownik": {"ok": True, "wynik": {"projekty": {}}},
    "projekty_napis": {"ok": True, "wynik": {"projekty": "brak"}},
    "pozycja_nie_slownik": {"ok": True, "wynik": {"projekty": ["napis"]}},
    "projekt_bez_nazwy": {"ok": True, "wynik": {"projekty": [
        {"skille": [], "workflow": []}]}},
    "projekt_nie_jest_napisem": {"ok": True, "wynik": {"projekty": [
        {"projekt": 5, "skille": [], "workflow": []}]}},
    "skille_nie_lista": {"ok": True, "wynik": {"projekty": [
        {"projekt": "/p", "skille": {}, "workflow": []}]}},
    "workflow_nie_lista": {"ok": True, "wynik": {"projekty": [
        {"projekt": "/p", "skille": [], "workflow": "wf"}]}},
    "skill_nie_slownik": {"ok": True, "wynik": {"projekty": [
        {"projekt": "/p", "skille": ["x"], "workflow": []}]}},
    "frontmatter_nie_jest_napisem": {"ok": True, "wynik": {"projekty": [
        {"projekt": "/p", "skille": [_skill("x", 5)], "workflow": []}]}},
    "workflow_nie_slownik": {"ok": True, "wynik": {"projekty": [
        {"projekt": "/p", "skille": [], "workflow": ["wf.yaml"]}]}},
    "drugi_projekt_zepsuty_po_poprawnym_pierwszym": {"ok": True, "wynik": {"projekty": [
        _wpis("/p/dobry", skille=[_skill("nowy", _fm("nowy"))]), "zepsuty"]}},
    "runner_zglosil_blad": {"ok": False, "blad": "za duzo pozycji", "kod": "za_duzo"},
    "runner_zglosil_blad_bez_kodu": {"ok": False},
    "skill_bez_klucza_frontmatter": {"ok": True, "wynik": {"projekty": [
        {"projekt": "/p", "workflow": [], "skille": [
            {k: v for k, v in _skill("x", _fm("x")).items() if k != "frontmatter"}]}]}},
}


@pytest.mark.parametrize("odpowiedz", ZLE_ODPOWIEDZI.values(), ids=ZLE_ODPOWIEDZI.keys())
async def test_zla_odpowiedz_runnera_to_stan_niemozliwy_a_stary_katalog_zostaje(
    zespoly, odpowiedz,
):
    await _wstaw(zespoly, zespoly.ids.a, "runner-a", STARA_DATA)
    przed = await _wiersze(zespoly)
    assert len(przed) == 1

    blad, _ = await _odswiez_blad(zespoly, zespoly.ids.a, odpowiedz)

    assert type(blad) is StanNiemozliwy   # dokladnie ten typ, nie RunnerOffline ani inny
    assert await _wiersze(zespoly) == przed   # nigdy pusty ani polowiczny katalog


# ---- odczyt bez runnera ---------------------------------------------------------------------

async def test_pusty_katalog_nie_ma_daty_odswiezenia(zespoly):
    async with zespoly.maker() as s:
        wynik = await katalog.katalog(s, zespoly.ids.a)

    assert wynik == {"odswiezono_at": None, "definicje": []}


async def test_katalog_bez_runnera_oddaje_ostatni_stan_ze_wszystkimi_polami(zespoly):
    await _wstaw(zespoly, zespoly.ids.a, "runner-a", STARA_DATA, projekt="/p/alfa",
                 rodzaj="skill", klucz="x", nazwa="Skill X", rola="gatherer",
                 sciezka=".cockpit/skills/x/SKILL.md")
    await _wstaw(zespoly, zespoly.ids.a, "runner-a", STARA_DATA, projekt="/p/alfa",
                 rodzaj="workflow", klucz="w", nazwa="w", rola=None,
                 sciezka=".cockpit/workflows/w.yaml")

    async with zespoly.maker() as s:
        wynik = await katalog.katalog(s, zespoly.ids.a)

    data = "2024-01-02T03:04:05+00:00"
    assert wynik == {"odswiezono_at": data, "definicje": [
        {"runner_id": "runner-a", "projekt": "/p/alfa", "rodzaj": "skill", "klucz": "x",
         "nazwa": "Skill X", "rola": "gatherer", "sciezka": ".cockpit/skills/x/SKILL.md",
         "odswiezono_at": data},
        {"runner_id": "runner-a", "projekt": "/p/alfa", "rodzaj": "workflow", "klucz": "w",
         "nazwa": "w", "rola": None, "sciezka": ".cockpit/workflows/w.yaml",
         "odswiezono_at": data},
    ]}


async def test_katalog_zespolu_z_dwoma_runnerami_podaje_najstarsze_odswiezenie(zespoly):
    await _dodaj_runnera(zespoly, "runner-a2", zespoly.ids.a)
    starsza = datetime(2024, 3, 1, 8, 0, 0, tzinfo=timezone.utc)
    nowsza = datetime(2024, 6, 1, 9, 30, 0, tzinfo=timezone.utc)
    await _wstaw(zespoly, zespoly.ids.a, "runner-a", nowsza, klucz="nowy")
    await _wstaw(zespoly, zespoly.ids.a, "runner-a2", starsza, klucz="stary")

    async with zespoly.maker() as s:
        wynik = await katalog.katalog(s, zespoly.ids.a)

    assert wynik["odswiezono_at"] == "2024-03-01T08:00:00+00:00"
    assert {d["klucz"]: d["odswiezono_at"] for d in wynik["definicje"]} == {
        "stary": "2024-03-01T08:00:00+00:00", "nowy": "2024-06-01T09:30:00+00:00"}


async def test_katalog_jest_posortowany_po_projekcie_rodzaju_i_kluczu(zespoly):
    teraz = datetime.now(timezone.utc)
    for projekt, rodzaj, klucz in (
        ("/p/b", "skill", "a"), ("/p/a", "workflow", "a"), ("/p/a", "skill", "z"),
        ("/p/a", "skill", "b"), ("/p/b", "workflow", "a"),
    ):
        await _wstaw(zespoly, zespoly.ids.a, "runner-a", teraz, projekt=projekt, rodzaj=rodzaj,
                     klucz=klucz)

    async with zespoly.maker() as s:
        wynik = await katalog.katalog(s, zespoly.ids.a)

    assert [(d["projekt"], d["rodzaj"], d["klucz"]) for d in wynik["definicje"]] == [
        ("/p/a", "skill", "b"), ("/p/a", "skill", "z"), ("/p/a", "workflow", "a"),
        ("/p/b", "skill", "a"), ("/p/b", "workflow", "a")]


async def test_katalog_czyta_tylko_baze_bez_pytania_runnera(zespoly, monkeypatch):
    await _wstaw(zespoly, zespoly.ids.a, "runner-a", STARA_DATA)

    async def zakaz(*a, **kw):
        raise AssertionError("odczyt katalogu nie moze pytac runnera")

    monkeypatch.setattr(zapytania, "zapytaj_runnera_online", zakaz)
    async with zespoly.maker() as s:
        wynik = await katalog.katalog(s, zespoly.ids.a)

    assert len(wynik["definicje"]) == 1


# ---- wybor runnera online -------------------------------------------------------------------

async def test_zapytanie_do_runnera_online_zwraca_jego_id_i_wynik_pomijajac_offline_o_mniejszym_id(
    zespoly,
):
    await _dodaj_runnera(zespoly, "runner-0", zespoly.ids.a)   # id mniejsze niz "runner-a"
    wysylka = _Wysylka({"ok": True, "wynik": {"x": 1}})

    async with zespoly.maker() as s:
        runner_id, wynik = await zapytania.zapytaj_runnera_online(
            s, zespoly.ids.a, "katalog_definicji", {}, POLACZONE_A, wysylka)

    assert (runner_id, wynik) == ("runner-a", {"x": 1})
    assert wysylka.wywolania == [("runner-a", "katalog_definicji", {})]


async def test_zapytanie_do_runnera_online_nie_wybiera_runnera_obcego_zespolu(zespoly):
    wysylka = _Wysylka({"ok": True, "wynik": {}})

    async with zespoly.maker() as s:
        with pytest.raises(RunnerOffline):
            await zapytania.zapytaj_runnera_online(
                s, zespoly.ids.a, "katalog_definicji", {}, frozenset({"runner-b"}), wysylka)
    assert wysylka.wywolania == []


# =================================================================================
# 3. Przelot HTTP: prawdziwa aplikacja + prawdziwy runner w procesie
# =================================================================================

@pytest.fixture(autouse=True)
def _czysta_szyna():
    """Kolejki, oczekujace zapytania i ewidencja polaczen to stan procesu."""
    slowniki = ("_oczekujace", "_runner_queues", "_polaczone", "_rozlaczone")
    zapisane = {n: dict(getattr(bus, n)) for n in slowniki if hasattr(bus, n)}
    yield
    for nazwa, przed in zapisane.items():
        biezacy = getattr(bus, nazwa)
        biezacy.clear()
        biezacy.update(przed)


class _Kokpit:
    """Aplikacja na bazie w katalogu testu: konta, runnery w procesie i klienci HTTP."""

    def __init__(self, maker) -> None:
        self.maker = maker
        self.klienci: list[AsyncClient] = []
        self.runnery: list[tuple[RunnerKlient, asyncio.Task]] = []

    def klient(self) -> AsyncClient:
        k = AsyncClient(transport=ASGITransport(app=app_mod.app), base_url="https://test")
        self.klienci.append(k)
        return k

    async def konto(self, email: str, zespol: str) -> SimpleNamespace:
        k = self.klient()
        odp = await k.post("/api/konto/rejestracja",
                           json={"email": email, "haslo": HASLO, "nazwa_zespolu": zespol})
        assert odp.status_code == 201, odp.text
        async with self.maker() as s:
            tenant_id = (await s.execute(select(Tenant.id).where(Tenant.name == zespol))).scalar_one()
        return SimpleNamespace(http=k, csrf={"X-CSRF-Token": odp.json()["csrf"]}, tenant_id=tenant_id)

    async def viewer(self, email: str, tenant_id: str) -> SimpleNamespace:
        """Konto z rola viewer w ISTNIEJACYM zespole, potem swieze logowanie."""
        wstepne = await self.konto(email, f"zespol-{email}")
        async with self.maker() as s:
            user_id = (await s.execute(select(User.id).where(User.email == email))).scalar_one()
            await s.execute(update(Membership).where(Membership.user_id == user_id).values(
                tenant_id=tenant_id, rola=RolaWZespole.viewer))
            await s.commit()
        k = self.klient()
        odp = await k.post("/api/konto/logowanie", json={"email": email, "haslo": HASLO})
        assert odp.status_code == 200, odp.text
        assert wstepne.tenant_id != tenant_id
        return SimpleNamespace(http=k, csrf={"X-CSRF-Token": odp.json()["csrf"]}, tenant_id=tenant_id)

    async def dodaj_runnera(self, runner_id: str, tenant_id: str) -> None:
        async with self.maker() as s:
            s.add(Runner(id=runner_id, tenant_id=tenant_id, name=runner_id,
                         token_hash=hashlib.sha256(f"tok-{runner_id}".encode()).hexdigest(),
                         last_seen_at=datetime.now(timezone.utc)))
            await s.commit()

    async def uruchom_runnera(self, runner_id: str) -> None:
        runner = RunnerKlient("http://kokpit.test", f"tok-{runner_id}",
                              transport=TransportASGIStrumieniujacy(app_mod.app))
        nasluch = asyncio.create_task(runner.listen())
        self.runnery.append((runner, nasluch))
        koniec = time.monotonic() + 10
        while runner_id not in bus.connected_runner_ids():
            assert time.monotonic() < koniec, "runner nie polaczyl sie z kokpitem"
            await asyncio.sleep(0.01)

    async def zamknij(self) -> None:
        for runner, nasluch in reversed(self.runnery):
            await asyncio.wait_for(runner.stop(), 10)
            gotowe, _ = await asyncio.wait({nasluch}, timeout=10)
            if nasluch not in gotowe:
                nasluch.cancel()
                await asyncio.wait({nasluch}, timeout=5)
        for k in self.klienci:
            await k.aclose()


@pytest.fixture
async def kokpit(tmp_path, monkeypatch, projekt):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'kokpit.db'}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "engine", engine)
    monkeypatch.setattr(db_session, "SessionLocal", maker)
    monkeypatch.setattr(settings, "ciasteczko_secure", True)
    monkeypatch.setattr(settings, "runner_zapytanie_timeout_s", 20.0)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    k = _Kokpit(maker)
    try:
        yield k
    finally:
        await k.zamknij()
        await engine.dispose()


@pytest.fixture
async def zespol(kokpit):
    """Zespol Alfa z wlascicielem i jednym zywym runnerem; zakres = `projekt`."""
    konto = await kokpit.konto("ala@przyklad.pl", "Alfa")
    await kokpit.dodaj_runnera("runner-alfa", konto.tenant_id)
    await kokpit.uruchom_runnera("runner-alfa")
    return konto


@pytest.fixture
async def zespol_offline(kokpit):
    """Zespol Alfa z runnerem w bazie, ktory sie nie polaczyl."""
    konto = await kokpit.konto("ala@przyklad.pl", "Alfa")
    await kokpit.dodaj_runnera("runner-alfa", konto.tenant_id)
    return konto


async def _wiersze_http(kokpit) -> list[dict]:
    async with kokpit.maker() as s:
        wiersze = (await s.execute(select(Definicja))).scalars().all()
    wynik = [{c: getattr(w, c) for c in (
        "id", "tenant_id", "runner_id", "projekt", "rodzaj", "klucz", "nazwa", "rola",
        "sciezka", "odswiezono_at")} for w in wiersze]
    return sorted(wynik, key=lambda w: (w["tenant_id"], w["projekt"], w["rodzaj"], w["klucz"]))


async def _wstaw_http(kokpit, tenant_id, runner_id, klucz="stary", data=STARA_DATA) -> None:
    async with kokpit.maker() as s:
        s.add(Definicja(
            id=str(uuid.uuid4()), tenant_id=tenant_id, runner_id=runner_id, projekt="/p/stary",
            rodzaj="skill", klucz=klucz, nazwa=f"Nazwa {klucz}", rola="author",
            sciezka=f".cockpit/skills/{klucz}/SKILL.md", odswiezono_at=data))
        await s.commit()


async def test_odczyt_katalogu_bez_sesji_daje_401(kokpit):
    odp = await kokpit.klient().get("/api/katalog")

    assert odp.status_code == 401


async def test_odswiezenie_katalogu_bez_sesji_daje_401_i_nie_zmienia_bazy(kokpit, zespol):
    await _wstaw_http(kokpit, zespol.tenant_id, "runner-alfa")
    przed = await _wiersze_http(kokpit)

    odp = await kokpit.klient().post("/api/katalog/odswiez")

    assert odp.status_code == 401
    assert await _wiersze_http(kokpit) == przed


async def test_gdy_runner_jest_offline_odczyt_oddaje_ostatni_stan_z_data_odswiezenia(
    kokpit, zespol_offline,
):
    await _wstaw_http(kokpit, zespol_offline.tenant_id, "runner-alfa", klucz="stary")

    odp = await zespol_offline.http.get("/api/katalog")

    assert odp.status_code == 200, odp.text
    assert odp.json() == {"odswiezono_at": "2024-01-02T03:04:05+00:00", "definicje": [
        {"runner_id": "runner-alfa", "projekt": "/p/stary", "rodzaj": "skill", "klucz": "stary",
         "nazwa": "Nazwa stary", "rola": "author", "sciezka": ".cockpit/skills/stary/SKILL.md",
         "odswiezono_at": "2024-01-02T03:04:05+00:00"}]}


async def test_odczyt_pustego_katalogu_nie_ma_daty_i_nie_jest_bledem(kokpit, zespol_offline):
    odp = await zespol_offline.http.get("/api/katalog")

    assert odp.status_code == 200
    assert odp.json() == {"odswiezono_at": None, "definicje": []}


async def test_odswiezenie_przy_runnerze_offline_daje_409_a_katalog_w_bazie_bez_zmian(
    kokpit, zespol_offline,
):
    await _wstaw_http(kokpit, zespol_offline.tenant_id, "runner-alfa")
    przed = await _wiersze_http(kokpit)
    assert len(przed) == 1

    odp = await zespol_offline.http.post("/api/katalog/odswiez", headers=zespol_offline.csrf)

    assert odp.status_code == 409
    assert odp.json()["detail"]
    assert await _wiersze_http(kokpit) == przed


async def test_odczyt_katalogu_nie_pokazuje_wpisow_innego_zespolu(kokpit, zespol_offline):
    inny = await kokpit.konto("bartek@przyklad.pl", "Beta")
    await kokpit.dodaj_runnera("runner-beta", inny.tenant_id)
    await _wstaw_http(kokpit, zespol_offline.tenant_id, "runner-alfa", klucz="alfa-skill")
    await _wstaw_http(kokpit, inny.tenant_id, "runner-beta", klucz="beta-skill")

    odp_a = await zespol_offline.http.get("/api/katalog")
    odp_b = await inny.http.get("/api/katalog")

    assert [d["klucz"] for d in odp_a.json()["definicje"]] == ["alfa-skill"]
    assert [d["klucz"] for d in odp_b.json()["definicje"]] == ["beta-skill"]


async def test_odswiezenie_z_zywym_runnerem_zapisuje_katalog_a_odczyt_go_pokazuje(
    kokpit, zespol, projekt,
):
    _pisz(projekt.korzen / ".cockpit/skills/szkic/SKILL.md",
          f"---\nid: szkic\nname: Szkic tekstu\nrole: author\n---\nBrief {ZNACZNIK}\n---\n{ZNACZNIK}\n")
    _pisz(projekt.korzen / ".cockpit/workflows/wydanie.yaml", f"name: wydanie\n# {ZNACZNIK}\n")

    odswiezenie = await zespol.http.post("/api/katalog/odswiez", headers=zespol.csrf)
    odczyt = await zespol.http.get("/api/katalog")

    assert odswiezenie.status_code == 200, odswiezenie.text
    assert odswiezenie.json()["pominiete"] == 0
    assert odczyt.status_code == 200
    definicje = {(d["rodzaj"], d["klucz"]): d for d in odczyt.json()["definicje"]}
    assert set(definicje) == {("skill", "szkic"), ("workflow", "wydanie")}
    assert definicje[("skill", "szkic")]["nazwa"] == "Szkic tekstu"
    assert definicje[("skill", "szkic")]["rola"] == "author"
    assert definicje[("skill", "szkic")]["projekt"] == str(projekt.korzen)
    assert definicje[("workflow", "wydanie")]["sciezka"] == ".cockpit/workflows/wydanie.yaml"
    assert odczyt.json()["odswiezono_at"] is not None
    # skutek w bazie: wiersze zespolu i runnera, a tresc briefu nie wyszla z maszyny
    wiersze = await _wiersze_http(kokpit)
    assert {(w["rodzaj"], w["klucz"]) for w in wiersze} == set(definicje)
    assert {(w["tenant_id"], w["runner_id"]) for w in wiersze} == {
        (zespol.tenant_id, "runner-alfa")}
    assert ZNACZNIK not in json.dumps(wiersze, default=str)
    assert ZNACZNIK not in odswiezenie.text
    assert ZNACZNIK not in odczyt.text


async def test_po_usunieciu_skilla_z_dysku_kolejne_odswiezenie_usuwa_go_z_katalogu(
    kokpit, zespol, projekt,
):
    plik = _plik_skilla(projekt.korzen, ".cockpit/skills", "tymczasowy")
    _plik_skilla(projekt.korzen, ".cockpit/skills", "zostaje")
    await zespol.http.post("/api/katalog/odswiez", headers=zespol.csrf)
    assert {w["klucz"] for w in await _wiersze_http(kokpit)} == {"tymczasowy", "zostaje"}

    plik.unlink()
    odp = await zespol.http.post("/api/katalog/odswiez", headers=zespol.csrf)

    assert odp.status_code == 200
    assert [w["klucz"] for w in await _wiersze_http(kokpit)] == ["zostaje"]


async def test_obserwator_zespolu_odswieza_i_czyta_katalog(kokpit, zespol, projekt):
    _plik_skilla(projekt.korzen, ".cockpit/skills", "x")
    obserwator = await kokpit.viewer("obserwator@przyklad.pl", zespol.tenant_id)

    odswiezenie = await obserwator.http.post("/api/katalog/odswiez", headers=obserwator.csrf)
    odczyt = await obserwator.http.get("/api/katalog")

    assert odswiezenie.status_code == 200, odswiezenie.text
    assert odczyt.status_code == 200
    assert [w["klucz"] for w in await _wiersze_http(kokpit)] == ["x"]


async def test_za_duzo_pozycji_u_runnera_daje_409_a_stary_katalog_zostaje(
    kokpit, zespol, projekt, monkeypatch,
):
    await _wstaw_http(kokpit, zespol.tenant_id, "runner-alfa")
    przed = await _wiersze_http(kokpit)
    monkeypatch.setattr(operacje, "LIMIT_DEFINICJI", 1)
    _plik_skilla(projekt.korzen, ".cockpit/skills", "a")
    _plik_skilla(projekt.korzen, ".cockpit/skills", "b")

    odp = await zespol.http.post("/api/katalog/odswiez", headers=zespol.csrf)

    assert odp.status_code == 409
    assert await _wiersze_http(kokpit) == przed   # nie obciety katalog podany jako pelny


# =================================================================================
# 4. Mutanty zmienionych funkcji (HAR-67, poprawka 2): kazdy test na wejsciu, ktore
#    odrzuca tylko jeden element reguly
# =================================================================================

JEST_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


# ---- runner: lista workflow ----------------------------------------------------------

async def test_plik_workflow_o_nazwie_samego_rozszerzenia_nie_jest_workflow(projekt):
    katalog_wf = projekt.korzen / ".cockpit/workflows"
    _pisz(katalog_wf / ".yaml", "name: bez-nazwy\n")
    _pisz(katalog_wf / "dobry.yaml", "name: dobry\n")

    [wpis] = await _projekty()

    assert wpis["workflow"] == [{"sciezka": ".cockpit/workflows/dobry.yaml"}]


async def test_workflow_przez_dowiazanie_poza_zakres_i_wiszace_dowiazanie_nie_ucinaja_listy(
    projekt,
):
    katalog_wf = projekt.korzen / ".cockpit/workflows"
    cudzy = _pisz(projekt.obcy / "cudzy.yaml", f"name: cudzy # {ZNACZNIK}\n")
    katalog_wf.mkdir(parents=True)
    (katalog_wf / "a1.yaml").symlink_to(cudzy)                       # poza zakresem
    (katalog_wf / "a2.yaml").symlink_to(projekt.korzen / "nie_ma")   # wiszace, w zakresie
    _pisz(katalog_wf / "b.yaml", "name: b\n")

    odp = await _wyk()

    assert odp["wynik"]["projekty"][0]["workflow"] == [{"sciezka": ".cockpit/workflows/b.yaml"}]
    assert ZNACZNIK not in json.dumps(odp)


# ---- runner: pliki skilli, ktorych nie da sie odczytac ----------------------------------

@pytest.mark.parametrize("rodzaj", ["katalog", "kolejka_fifo"])
async def test_skill_ktorego_plik_nie_jest_zwyklym_plikiem_nie_jest_skillem(projekt, rodzaj):
    zly = projekt.korzen / ".cockpit/skills/zly"
    zly.mkdir(parents=True)
    if rodzaj == "katalog":
        (zly / "SKILL.md").mkdir()
    else:
        os.mkfifo(zly / "SKILL.md")
    _plik_skilla(projekt.korzen, ".cockpit/skills", "dobry")

    odp = await asyncio.wait_for(_wyk(), 10)

    assert _kat_skilli(odp["wynik"]["projekty"][0]) == [("repo", ".cockpit/skills", "dobry")]


async def test_zwykly_plik_zamiast_katalogu_skilla_nie_jest_skillem(projekt):
    _pisz(projekt.korzen / ".cockpit/skills/plik", "to nie katalog skilla")
    _plik_skilla(projekt.korzen, ".cockpit/skills", "dobry")

    [wpis] = await _projekty()

    assert _kat_skilli(wpis) == [("repo", ".cockpit/skills", "dobry")]


@pytest.mark.skipif(JEST_ROOT, reason="root czyta pliki bez wzgledu na prawa")
async def test_skill_z_plikiem_bez_praw_odczytu_jest_na_liscie_z_frontmatterem_null(projekt):
    plik = _plik_skilla(projekt.korzen, ".cockpit/skills", "zamkniety")
    plik.chmod(0o000)
    try:
        odp = await _wyk()
    finally:
        plik.chmod(0o600)

    [skill] = odp["wynik"]["projekty"][0]["skille"]
    assert skill["katalog"] == "zamkniety"
    assert skill["frontmatter"] is None
    assert ZNACZNIK not in json.dumps(odp)


@pytest.mark.skipif(JEST_ROOT, reason="root omija prawa katalogow")
async def test_skill_w_katalogu_bez_prawa_przeszukiwania_jest_na_liscie_z_frontmatterem_null(
    projekt,
):
    plik = _plik_skilla(projekt.korzen, ".cockpit/skills", "zamkniety")
    plik.parent.chmod(0o600)   # bez bitu x: stat pliku w srodku konczy sie PermissionError
    try:
        odp = await _wyk()
    finally:
        plik.parent.chmod(0o700)

    [skill] = odp["wynik"]["projekty"][0]["skille"]
    assert skill["katalog"] == "zamkniety"
    assert skill["frontmatter"] is None


async def test_skill_ktorego_sciezke_zakres_odrzuca_bledem_dysku_jest_na_liscie_z_null(
    projekt, monkeypatch,
):
    _plik_skilla(projekt.korzen, ".cockpit/skills", "zly")
    _plik_skilla(projekt.korzen, ".cockpit/skills", "dobry")
    prawdziwe = Zakres.rozwiaz

    def rozwiaz_z_awaria(self, sciezka, **kw):
        if Path(sciezka).parent.name == "zly":
            raise PermissionError(13, "odmowa przy rozwiazywaniu sciezki")
        return prawdziwe(self, sciezka, **kw)

    monkeypatch.setattr(Zakres, "rozwiaz", rozwiaz_z_awaria)

    [wpis] = await _projekty()

    fm = {s["katalog"]: s["frontmatter"] for s in wpis["skille"]}
    assert set(fm) == {"zly", "dobry"}
    assert fm["zly"] is None
    assert "id: dobry" in fm["dobry"]


async def test_plik_skilla_z_bajtem_zerowym_w_poprawnym_utf8_ma_frontmatter_null(projekt):
    _pisz(projekt.korzen / ".cockpit/skills/nul/SKILL.md",
          f"---\nid: nul\nname: Nul\nrole: author\n---\nbrief \0 {ZNACZNIK}\n")

    odp = await _wyk()

    [skill] = odp["wynik"]["projekty"][0]["skille"]
    assert skill["katalog"] == "nul"
    assert skill["frontmatter"] is None
    assert ZNACZNIK not in json.dumps(odp)


async def test_runner_czyta_z_pliku_skilla_najwyzej_limit_plus_jeden_bajt(projekt, monkeypatch):
    monkeypatch.setenv("COCKPIT_MAX_READ_KB", "1")
    _pisz(projekt.korzen / ".cockpit/skills/ogromny/SKILL.md", b"a" * 5000)
    odczyty: list = []

    class Sledzacy:
        def __init__(self, f):
            self._f = f

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._f.close()

        def read(self, n=-1):
            odczyty.append(n)
            return self._f.read(n)

    monkeypatch.setattr(operacje, "open", lambda *a, **kw: Sledzacy(open(*a, **kw)),
                        raising=False)

    odp = await _wyk()

    assert odp["wynik"]["projekty"][0]["skille"][0]["frontmatter"] is None
    assert odczyty == [1024 + 1]


@pytest.mark.skipif(JEST_ROOT, reason="root listuje katalogi bez wzgledu na prawa")
async def test_katalog_skilli_bez_prawa_odczytu_to_blad_brak_uprawnien_a_nie_pusta_lista(projekt):
    zrodlo = projekt.korzen / ".cockpit/skills"
    _plik_skilla(projekt.korzen, ".cockpit/skills", "x")
    zrodlo.chmod(0o000)
    try:
        odp = await _wyk()
    finally:
        zrodlo.chmod(0o700)

    assert odp["ok"] is False
    assert odp["kod"] == "brak_uprawnien"
    assert odp["req_id"] == "r-1"


# ---- serwis: dokladne komunikaty zlego ksztaltu odpowiedzi -------------------------------

def _projekt_ze_skillem(**zmiany) -> dict:
    return {"ok": True, "wynik": {"projekty": [{
        "projekt": "/p", "skille": [{**_skill("x", _fm("x")), **zmiany}], "workflow": []}]}}


ZLY_KSZTALT_Z_POWODEM = {
    "wynik_nie_slownik": ({"ok": True, "wynik": []}, "wynik nie jest slownikiem"),
    "projekty_nie_lista": (
        {"ok": True, "wynik": {"projekty": {}}}, "pole projekty nie jest lista slownikow"),
    "projekt_nie_napis": (
        {"ok": True, "wynik": {"projekty": [{"projekt": 5, "skille": [], "workflow": []}]}},
        "pole projekt nie jest napisem"),
    "ten_sam_projekt_dwa_razy": (_odp(_wpis("/p"), _wpis("/p")), "projekt wystepuje dwa razy"),
    "skille_nie_lista": (
        {"ok": True, "wynik": {"projekty": [{"projekt": "/p", "skille": "x", "workflow": []}]}},
        "pole skille nie jest lista slownikow"),
    "zrodlo_nie_napis": (_projekt_ze_skillem(zrodlo=1), "pole zrodlo nie jest napisem"),
    "podkatalog_nie_napis": (_projekt_ze_skillem(podkatalog=1), "pole podkatalog nie jest napisem"),
    "nieznany_podkatalog": (_projekt_ze_skillem(podkatalog=".nieznany"), "nieznane zrodlo skilli"),
    "zrodlo_dom_z_podkatalogiem_tylko_repo": (
        _projekt_ze_skillem(zrodlo="dom", podkatalog=".claude/skills"), "nieznane zrodlo skilli"),
    "katalog_nie_napis": (_projekt_ze_skillem(katalog=1), "pole katalog nie jest napisem"),
    "sciezka_skilla_nie_napis": (_projekt_ze_skillem(sciezka=1), "pole sciezka nie jest napisem"),
    "frontmatter_nie_napis": (
        _projekt_ze_skillem(frontmatter=5), "pole frontmatter nie jest napisem ani null"),
    "frontmatter_bez_klucza": (
        {"ok": True, "wynik": {"projekty": [{"projekt": "/p", "workflow": [], "skille": [
            {k: v for k, v in _skill("x", _fm("x")).items() if k != "frontmatter"}]}]}},
        "pole frontmatter nie jest napisem ani null"),
    "workflow_nie_lista": (
        {"ok": True, "wynik": {"projekty": [{"projekt": "/p", "skille": [], "workflow": "wf"}]}},
        "pole workflow nie jest lista slownikow"),
    "sciezka_workflow_nie_napis": (
        {"ok": True, "wynik": {"projekty": [
            {"projekt": "/p", "skille": [], "workflow": [{"sciezka": 1}]}]}},
        "pole sciezka nie jest napisem"),
}


@pytest.mark.parametrize(("odpowiedz", "powod"), ZLY_KSZTALT_Z_POWODEM.values(),
                         ids=ZLY_KSZTALT_Z_POWODEM.keys())
async def test_zly_ksztalt_odpowiedzi_runnera_wskazuje_w_komunikacie_co_jest_zle(
    zespoly, odpowiedz, powod,
):
    blad, _ = await _odswiez_blad(zespoly, zespoly.ids.a, odpowiedz)

    assert type(blad) is StanNiemozliwy
    assert str(blad) == f"odpowiedz runnera ma zly ksztalt ({powod})"
    assert await _wiersze(zespoly) == []


# ---- serwis: runner znika w trakcie odswiezania ---------------------------------------------

class _WysylkaZHakiem(_Wysylka):
    """Po odpowiedzi wykonuje zmiane w bazie - jak wyscig z innym zadaniem."""

    def __init__(self, odpowiedz: dict, hak) -> None:
        super().__init__(odpowiedz)
        self.hak = hak

    async def __call__(self, runner_id, typ, dane, timeout_s):
        wynik = await super().__call__(runner_id, typ, dane, timeout_s)
        await self.hak()
        return wynik


async def _odswiez_z_hakiem(zespoly, hak):
    wysylka = _WysylkaZHakiem(_odp(_wpis("/p", skille=[_skill("x", _fm("x"))])), hak)
    async with zespoly.maker() as s:
        try:
            await katalog.odswiez_katalog(s, zespoly.ids.a, POLACZONE_A, wysylka)
        except Exception as e:
            return e
    pytest.fail("odswiezenie nie zglosilo bledu")


async def test_runner_usuniety_w_trakcie_odswiezania_to_stan_niemozliwy_i_katalog_bez_zapisu(
    zespoly,
):
    async def usun_runnera():
        async with zespoly.maker() as s:
            await s.execute(delete(Runner).where(Runner.id == "runner-a"))
            await s.commit()

    blad = await _odswiez_z_hakiem(zespoly, usun_runnera)

    assert type(blad) is StanNiemozliwy
    assert str(blad) == "runner zniknal w trakcie odswiezania katalogu"
    assert await _wiersze(zespoly) == []


async def test_runner_przeniesiony_w_trakcie_odswiezania_do_innego_zespolu_nie_zapisuje_katalogu(
    zespoly,
):
    async def przenies_runnera():
        async with zespoly.maker() as s:
            await s.execute(update(Runner).where(Runner.id == "runner-a").values(
                tenant_id=zespoly.ids.b))
            await s.commit()

    blad = await _odswiez_z_hakiem(zespoly, przenies_runnera)

    assert type(blad) is StanNiemozliwy
    assert str(blad) == "runner zniknal w trakcie odswiezania katalogu"
    assert await _wiersze(zespoly) == []


async def test_odswiezenie_nie_kasuje_wiersza_innego_zespolu_nawet_z_tym_samym_runner_id(zespoly):
    cudzy = await _wstaw(zespoly, zespoly.ids.b, "runner-a", STARA_DATA, klucz="obcy")

    await _odswiez(zespoly, zespoly.ids.a, _odp(_wpis("/p")))

    assert [w["id"] for w in await _wiersze(zespoly)] == [cudzy]


# ---- serwis: licznik pominietych i granice pol -----------------------------------------------

async def test_dwa_skille_bez_frontmattera_to_dwie_pominiete(zespoly):
    odp = _odp(_wpis("/p", skille=[_skill("a", None), _skill("b", None)]))

    wynik, _ = await _odswiez(zespoly, zespoly.ids.a, odp)

    assert wynik["pominiete"] == 2


async def test_dwa_skille_konwencyjne_bez_roli_to_dwie_pominiete(zespoly):
    odp = _odp(_wpis("/p", skille=[
        _skill("a", "name: A\n", podkatalog=".claude/skills"),
        _skill("b", "name: B\n", podkatalog=".agents/skills")]))

    wynik, _ = await _odswiez(zespoly, zespoly.ids.a, odp)

    assert wynik["pominiete"] == 2


async def test_skill_konwencyjny_bez_roli_nie_ucina_kolejnych_skilli_tego_samego_zrodla(zespoly):
    odp = _odp(_wpis("/p", skille=[
        _skill("cudzy", "name: Cudzy\n", podkatalog=".claude/skills"),
        _skill("nasz", "role: author\n", podkatalog=".claude/skills"),
        _skill("trzeci", "role: author\n", podkatalog=".agents/skills")]))

    wynik, _ = await _odswiez(zespoly, zespoly.ids.a, odp)

    assert [w["klucz"] for w in await _wiersze(zespoly)] == ["nasz", "trzeci"]
    assert wynik["pominiete"] == 1


async def test_id_skilla_dlugosci_kolumny_jest_zapisane_a_o_znak_dluzsze_pominiete(zespoly):
    rowny, dluzszy = "k" * 200, "d" * 201
    odp = _odp(_wpis("/p", skille=[
        _skill(rowny, _fm(rowny, nazwa="N")), _skill(dluzszy, _fm(dluzszy, nazwa="N"))]))

    wynik, _ = await _odswiez(zespoly, zespoly.ids.a, odp)

    assert [w["klucz"] for w in await _wiersze(zespoly)] == [rowny]
    assert wynik["pominiete"] == 1


@pytest.mark.parametrize(("zrodlo", "dlugosc_z_runnera", "zapisany"), [
    ("repo", 1024, True), ("repo", 1025, False),
    ("dom", 1022, True), ("dom", 1023, False),   # "~/" doklada dwa znaki do sciezki w bazie
])
async def test_sciezka_skilla_dluzsza_niz_kolumna_jest_pominieta_liczona_z_tylda(
    zespoly, zrodlo, dlugosc_z_runnera, zapisany,
):
    skill = {**_skill("x", _fm("x"), zrodlo=zrodlo), "sciezka": "a" * dlugosc_z_runnera}

    wynik, _ = await _odswiez(zespoly, zespoly.ids.a, _odp(_wpis("/p", skille=[skill])))

    assert len(await _wiersze(zespoly)) == (1 if zapisany else 0)
    assert wynik["pominiete"] == (0 if zapisany else 1)


async def test_dwa_skille_z_za_dluga_sciezka_to_dwie_pominiete(zespoly):
    odp = _odp(_wpis("/p", skille=[
        {**_skill("a", _fm("a")), "sciezka": "s" * 1025},
        {**_skill("b", _fm("b")), "sciezka": "s" * 1025}]))

    wynik, _ = await _odswiez(zespoly, zespoly.ids.a, odp)

    assert wynik["pominiete"] == 2
    assert await _wiersze(zespoly) == []


def _workflow_ze_sciezkami(projekt: str, *sciezki: str) -> dict:
    return {"projekt": projekt, "skille": [], "workflow": [{"sciezka": s} for s in sciezki]}


async def test_workflow_z_nazwa_dlugosci_kolumny_jest_zapisany_a_o_znak_dluzszy_pominiety(zespoly):
    rowny = f".cockpit/workflows/{'w' * 200}.yaml"
    dluzszy = f".cockpit/workflows/{'d' * 201}.yaml"

    wynik, _ = await _odswiez(zespoly, zespoly.ids.a,
                              _odp(_workflow_ze_sciezkami("/p", rowny, dluzszy)))

    assert [w["klucz"] for w in await _wiersze(zespoly)] == ["w" * 200]
    assert wynik["pominiete"] == 1


@pytest.mark.parametrize(("dlugosc", "zapisany"), [(1024, True), (1025, False)])
async def test_workflow_z_sciezka_dluzsza_niz_kolumna_jest_pominiety_a_rowna_zapisana(
    zespoly, dlugosc, zapisany,
):
    sciezka = "a" * (dlugosc - len("/w.yaml")) + "/w.yaml"
    assert len(sciezka) == dlugosc

    wynik, _ = await _odswiez(zespoly, zespoly.ids.a,
                              _odp(_workflow_ze_sciezkami("/p", sciezka)))

    assert len(await _wiersze(zespoly)) == (1 if zapisany else 0)
    assert wynik["pominiete"] == (0 if zapisany else 1)


@pytest.mark.parametrize("sciezka", [
    ".cockpit/workflows/.yaml", ".cockpit/workflows/x.txt", ".cockpit/workflows/x.yml",
], ids=["sama_koncowka", "inne_rozszerzenie_txt", "inne_rozszerzenie_yml"])
async def test_pozycja_workflow_bez_nazwy_albo_ze_zlym_rozszerzeniem_jest_pominieta(
    zespoly, sciezka,
):
    wynik, _ = await _odswiez(zespoly, zespoly.ids.a,
                              _odp(_workflow_ze_sciezkami("/p", sciezka)))

    assert await _wiersze(zespoly) == []
    assert wynik["pominiete"] == 1


async def test_dwie_zle_pozycje_workflow_to_dwie_pominiete_a_dobra_po_nich_zapisana(zespoly):
    wynik, _ = await _odswiez(zespoly, zespoly.ids.a, _odp(_workflow_ze_sciezkami(
        "/p", ".cockpit/workflows/.yaml", ".cockpit/workflows/x.txt",
        ".cockpit/workflows/dobry.yaml")))

    assert [w["klucz"] for w in await _wiersze(zespoly)] == ["dobry"]
    assert wynik["pominiete"] == 2


async def test_projekt_o_nazwie_dlugosci_kolumny_jest_zapisany(zespoly):
    projekt = "/" + "p" * 1023
    odp = _odp(_wpis(projekt, skille=[_skill("x", _fm("x"))]))

    wynik, _ = await _odswiez(zespoly, zespoly.ids.a, odp)

    assert [w["projekt"] for w in await _wiersze(zespoly)] == [projekt]
    assert wynik["pominiete"] == 0


async def test_projekty_z_za_dluga_nazwa_licza_wszystkie_pozycje_i_nie_ucinaja_kolejnych(zespoly):
    pierwszy = "/" + "a" * 1024
    drugi = "/" + "b" * 1024
    odp = _odp(
        _wpis(pierwszy, skille=[_skill("a", _fm("a")), _skill("b", _fm("b"))], workflow=["w"]),
        _wpis(drugi, skille=[_skill("c", _fm("c"))]),
        _wpis("/p/normalny", skille=[_skill("n", _fm("n"))]),
    )

    wynik, _ = await _odswiez(zespoly, zespoly.ids.a, odp)

    assert [(w["projekt"], w["klucz"]) for w in await _wiersze(zespoly)] == [("/p/normalny", "n")]
    assert wynik["pominiete"] == 4   # 2 skille + 1 workflow + 1 skill


async def test_pominiete_z_kilku_projektow_i_z_skilli_oraz_workflow_sumuja_sie(zespoly):
    odp = _odp(
        {"projekt": "/p/1", "skille": [_skill("zly", None)],
         "workflow": [{"sciezka": ".cockpit/workflows/x.txt"}]},
        {"projekt": "/p/2", "skille": [_skill("zly", None)], "workflow": []},
    )

    wynik, _ = await _odswiez(zespoly, zespoly.ids.a, odp)

    assert wynik["pominiete"] == 3


# ---- poprawka 3: blokada wiersza runnera i niepoprawna data we frontmatterze -----------------

class _SesjaZeSzpiegiem(AsyncSession):
    """Prawdziwa sesja; zapamietuje kazde zapytanie przekazane do `execute`."""

    zapytania: list = []

    async def execute(self, statement, *args, **kwargs):
        self.zapytania.append(statement)
        return await super().execute(statement, *args, **kwargs)


async def test_odswiezenie_blokuje_wiersz_runnera_zespolu_for_update_w_postgresql(zespoly):
    # SQLite ignoruje FOR UPDATE, wiec sprawdzamy zapytanie skompilowane dla PostgreSQL
    _SesjaZeSzpiegiem.zapytania = []
    szpieg = async_sessionmaker(
        zespoly.maker.kw["bind"], class_=_SesjaZeSzpiegiem, expire_on_commit=False)
    wysylka = _Wysylka(_odp(_wpis("/p", skille=[_skill("x", _fm("x"))])))

    async with szpieg() as s:
        await katalog.odswiez_katalog(s, zespoly.ids.a, POLACZONE_A, wysylka)

    sql = [str(z.compile(dialect=postgresql.dialect())) for z in _SesjaZeSzpiegiem.zapytania]
    zablokowane = [q for q in sql if q.rstrip().endswith("FOR UPDATE")]
    assert len(zablokowane) == 1, sql
    assert "FROM runner" in zablokowane[0]
    warunki = zablokowane[0].split("WHERE", 1)[1]
    assert "runner.id =" in warunki
    assert "runner.tenant_id =" in warunki


async def test_skill_z_niepoprawna_data_we_frontmatterze_jest_pominiety_a_reszta_zapisana(zespoly):
    odp = _odp(_wpis("/p", skille=[
        _skill("zly", _fm("zly") + "utworzono: 2024-02-30\n"), _skill("dobry", _fm("dobry"))]))

    wynik, _ = await _odswiez(zespoly, zespoly.ids.a, odp)

    assert [w["klucz"] for w in await _wiersze(zespoly)] == ["dobry"]
    assert wynik["pominiete"] == 1
