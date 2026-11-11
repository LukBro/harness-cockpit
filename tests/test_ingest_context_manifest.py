"""HAR-169: przyjecie zdarzenia `context_manifest` od runnera (TDD).

Kontrakt: `POST /api/events` z rodzajem `context_manifest` niesie pole `manifest` (lista wpisow
warstwa, sciezka, sha256, rozmiar, id - same hashe, bez tresci plikow); trafia do `Event.dane`
jako `{"manifest": [...]}`. `dane` wolno przy `context_manifest`, a przy innym rodzaju nie
koncowym nadal jest `NiespojneDane` (422). Skutki czytamy z bazy (nowa sesja), na prawdziwej
aplikacji; baza ma dwa zespoly (Browarek z runem, Piekarnia z obcym runnerem).
"""
# ruff: noqa: F811  (fixture `swiat` importowana z test_odbior_dowody i uzywana jako argument testu)
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from cockpit.db.models import Event
from test_odbior_dowody import (  # noqa: F401  (swiat to fixture)
    dane_zdarzen,
    liczba_zdarzen,
    odrzucone_bez_zapisu,
    swiat,
    wyslij,
    wyslij_ok,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
INT4_MAX = 2_147_483_647


def wpis(**zmiany) -> dict:
    return {"warstwa": "claude_md_korzenia", "sciezka": "CLAUDE.md", "sha256": SHA_A,
            "rozmiar": 1234, "id": None, **zmiany}


MANIFEST = [
    {"warstwa": "niezmienniki_silnika", "sciezka": None, "sha256": None, "rozmiar": None,
     "id": None},
    wpis(warstwa="project_yaml", sciezka=".cockpit/project.yaml", sha256=SHA_B, rozmiar=0),
    wpis(),
    wpis(warstwa="lekcje", sciezka=".ai/lessons.md", rozmiar=INT4_MAX, id="L-012"),
]


async def test_manifest_kroku_trafia_do_danych_zdarzenia_w_bazie_jako_same_hashe(swiat):
    await wyslij_ok(swiat, "context_manifest", manifest=MANIFEST, attempt=1)

    assert await dane_zdarzen(swiat, swiat.run_id) == [{"manifest": MANIFEST}]


async def test_brak_pomiaru_we_wpisie_manifestu_zostaje_w_bazie_brakiem_a_nie_zerem(swiat):
    await wyslij_ok(swiat, "context_manifest", manifest=MANIFEST[:1])

    ((zapisany,),) = [d["manifest"] for d in await dane_zdarzen(swiat, swiat.run_id)]
    assert zapisany["sha256"] is None
    assert zapisany["rozmiar"] is None
    assert zapisany["sciezka"] is None


async def test_zdarzenie_manifestu_nalezy_do_zespolu_runu_i_ma_rodzaj_context_manifest(swiat):
    await wyslij_ok(swiat, "context_manifest", manifest=MANIFEST, step_id="krok-7")

    async with swiat.maker() as s:
        (zdarzenie,) = (await s.execute(select(Event))).scalars().all()
    assert zdarzenie.kind == "context_manifest"
    assert zdarzenie.step_id == "krok-7"
    assert zdarzenie.tenant_id == swiat.tenant_id
    assert zdarzenie.tenant_id != swiat.obcy_tenant_id


async def test_runner_obcego_zespolu_nie_zapisze_manifestu_w_runie_browarka(swiat):
    r = await swiat.klient.post(
        "/api/events",
        json={"run_id": swiat.run_id, "kind": "context_manifest", "step_id": "s0",
              "event_id": str(uuid.uuid4()), "manifest": MANIFEST},
        headers={"Authorization": "Bearer token-piekarni"})

    assert r.status_code == 404, r.text
    assert await liczba_zdarzen(swiat) == 0


@pytest.mark.parametrize("rodzaj", ["output", "step_started", "step_waiting", "step_skipped"])
async def test_manifest_przy_innym_rodzaju_zdarzenia_niz_context_manifest_jest_odrzucony(
    swiat, rodzaj,
):
    await odrzucone_bez_zapisu(swiat, rodzaj, manifest=MANIFEST)


async def test_zdarzenie_context_manifest_bez_manifestu_nie_ma_danych(swiat):
    await wyslij_ok(swiat, "context_manifest")

    assert await dane_zdarzen(swiat, swiat.run_id) == [None]


ZLE_WPISY = [
    pytest.param(wpis(sha256="A" * 64), id="sha256-wielkie-litery"),
    pytest.param(wpis(sha256="a" * 63), id="sha256-za-krotki"),
    pytest.param(wpis(sha256="a" * 65), id="sha256-za-dlugi"),
    pytest.param(wpis(sha256="g" * 64), id="sha256-nie-hex"),
    pytest.param(wpis(sha256="a" * 63 + "\n"), id="sha256-z-nowa-linia-na-koncu"),
    pytest.param(wpis(rozmiar=-1), id="rozmiar-ujemny"),
    pytest.param(wpis(rozmiar=INT4_MAX + 1), id="rozmiar-ponad-int4"),
    pytest.param(wpis(warstwa="w" * 10_000), id="warstwa-bez-limitu-dlugosci"),
    pytest.param(wpis(sciezka="s" * 50_000), id="sciezka-bez-limitu-dlugosci"),
    pytest.param(wpis(id="L" * 50_000), id="id-bez-limitu-dlugosci"),
    pytest.param({k: v for k, v in wpis().items() if k != "warstwa"}, id="brak-warstwy"),
]


@pytest.mark.parametrize("zly", ZLE_WPISY)
async def test_wpis_manifestu_z_zla_wartoscia_jest_odrzucony_bez_zapisu(swiat, zly):
    await odrzucone_bez_zapisu(swiat, "context_manifest", manifest=[wpis(), zly])


async def test_lista_manifestu_bez_limitu_dlugosci_jest_odrzucona_bez_zapisu(swiat):
    await odrzucone_bez_zapisu(
        swiat, "context_manifest", manifest=[{"warstwa": "lekcje"}] * 20_000)


async def test_wpis_na_granicach_limitow_jest_przyjety_i_zapisany_bez_zmian(swiat):
    graniczny = wpis(sha256="0" * 64, rozmiar=INT4_MAX, sciezka="s" * 255, id="L-999")

    await wyslij_ok(swiat, "context_manifest", manifest=[graniczny])

    assert await dane_zdarzen(swiat, swiat.run_id) == [{"manifest": [graniczny]}]


async def test_odrzucony_manifest_nie_zostawia_zdarzenia_a_kolejny_poprawny_jest_przyjety(swiat):
    r = await wyslij(swiat, "context_manifest", manifest=[wpis(sha256="x")])
    assert r.status_code == 422
    assert await liczba_zdarzen(swiat) == 0

    await wyslij_ok(swiat, "context_manifest", manifest=MANIFEST)

    assert await liczba_zdarzen(swiat) == 1
