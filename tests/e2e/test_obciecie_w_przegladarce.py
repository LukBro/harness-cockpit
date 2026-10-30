"""Oznaczenie obcietego tekstu zdarzenia na ekranie runu w prawdziwej przegladarce (HAR-59)."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.e2e, pytest.mark.baza]

LIMIT_WYNIKU_MS = 3000
# dwie rozne dlugosci: kazda sciezka (historia, strumien) ma wlasna asercje
DLUGOSC_ZDARZENIA_Z_HISTORII = 123456
DLUGOSC_ZDARZENIA_ZE_STRUMIENIA = 7654321


def _id_runu(page) -> str:
    return parse_qs(urlparse(page.url).query)["run"][0]


def _wyslij_zdarzenie(adres, token, run_id, event_id, text, text_full_len=None):
    """Zdarzenie od runnera tak, jak wysyla je prawdziwy runner (Bearer + POST /api/events)."""
    odpowiedz = httpx.post(
        f"{adres}/api/events",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "run_id": run_id,
            "event_id": event_id,
            "step_id": "s0",
            "kind": "output",
            "text": text,
            "text_full_len": text_full_len,
        },
        timeout=5,
    )
    assert odpowiedz.status_code == 200, odpowiedz.text


def test_obciety_tekst_zdarzenia_ma_na_ekranie_oznaczenie_z_pelna_dlugoscia(
    runner_online, zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    page.get_by_role("button", name="Uruchom").click()
    expect(page.get_by_text("status: to_accept")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    run_id = _id_runu(page)

    # zdarzenia dla historii: obciete, nieobciete bez pelnej dlugosci i z pelna dlugoscia rowna tekstowi
    _wyslij_zdarzenie(adres_kokpitu, token_runnera, run_id, "hist-obciete",
                      "tekst-historii-obciety", DLUGOSC_ZDARZENIA_Z_HISTORII)
    _wyslij_zdarzenie(adres_kokpitu, token_runnera, run_id, "hist-cale-bez-dlugosci",
                      "tekst-calosci-bez-dlugosci")
    _wyslij_zdarzenie(adres_kokpitu, token_runnera, run_id, "hist-cale-z-dlugoscia",
                      "tekst-calosci-z-dlugoscia", len("tekst-calosci-z-dlugoscia"))

    # strona otwarta od nowa: te zdarzenia przychodza wylacznie z GET (historia)
    page.goto(f"{adres_kokpitu}/?run={run_id}")
    obciete_z_historii = page.locator("li", has_text="tekst-historii-obciety")
    expect(obciete_z_historii).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(page.get_by_text("status: to_accept")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(obciete_z_historii).to_contain_text(
        f"obcięte, pełna długość {DLUGOSC_ZDARZENIA_Z_HISTORII} znaków"
    )
    expect(page.locator("li", has_text="tekst-calosci-bez-dlugosci")).to_be_visible()
    expect(page.locator("li", has_text="tekst-calosci-z-dlugoscia")).to_be_visible()

    # od teraz GET runu oddaje zamrozona historie bez nowych zdarzen (trasa nie lapie /stream),
    # wiec zdarzenie wyslane ponizej moze dotrzec do ekranu wylacznie strumieniem
    historia = page.request.get(f"{adres_kokpitu}/api/runs/{run_id}").json()
    page.route(
        re.compile(rf"/api/runs/{re.escape(run_id)}(\?.*)?$"),
        lambda route: route.fulfill(json=historia),
    )

    # zdarzenie wyslane dopiero po pokazaniu historii przychodzi strumieniem
    _wyslij_zdarzenie(adres_kokpitu, token_runnera, run_id, "strumien-obciete",
                      "tekst-strumienia-obciety", DLUGOSC_ZDARZENIA_ZE_STRUMIENIA)
    _wyslij_zdarzenie(adres_kokpitu, token_runnera, run_id, "strumien-cale",
                      "tekst-strumienia-caly")

    obciete_ze_strumienia = page.locator("li", has_text="tekst-strumienia-obciety")
    expect(obciete_ze_strumienia).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(obciete_ze_strumienia).to_contain_text(
        f"obcięte, pełna długość {DLUGOSC_ZDARZENIA_ZE_STRUMIENIA} znaków"
    )
    expect(page.locator("li", has_text="tekst-strumienia-caly")).to_be_visible(
        timeout=LIMIT_WYNIKU_MS
    )

    # zdarzenia nieobciete (z historii i ze strumienia) nie niosa oznaczenia
    for fragment in ("tekst-calosci-bez-dlugosci", "tekst-calosci-z-dlugoscia",
                     "tekst-strumienia-caly"):
        expect(page.locator("li", has_text=fragment)).not_to_contain_text("obcięte")
    # oznaczenie dokladnie przy dwoch obcietych zdarzeniach, nigdzie indziej
    expect(page.locator("li", has_text="obcięte")).to_have_count(2)


def test_obciete_zdarzenie_bez_pomiaru_dlugosci_pokazuje_dlugosc_nieznana_a_nie_zero(
    runner_online, zalogowana_strona, adres_kokpitu, token_runnera
):
    page = zalogowana_strona
    page.get_by_role("button", name="Uruchom").click()
    expect(page.get_by_text("status: to_accept")).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    run_id = _id_runu(page)
    _wyslij_zdarzenie(adres_kokpitu, token_runnera, run_id, "bez-pomiaru",
                      "tekst-bez-pomiaru", DLUGOSC_ZDARZENIA_Z_HISTORII)

    # API samo nie wytwarza obcietego zdarzenia bez dlugosci, wiec stan podstawiamy w odpowiedzi
    # historii; trasa lapie kazdy GET tego runu (takze ten z onopen strumienia), ale nie /stream
    podmienione = []

    def podmien_dlugosc(route):
        odpowiedz = route.fetch()
        dane = odpowiedz.json()
        for zdarzenie in dane["events"]:
            if zdarzenie["text"] == "tekst-bez-pomiaru":
                assert zdarzenie["text_truncated"] is True
                zdarzenie["text_full_len"] = None
                podmienione.append(zdarzenie["seq"])
        route.fulfill(response=odpowiedz, json=dane)

    page.route(re.compile(rf"/api/runs/{re.escape(run_id)}(\?.*)?$"), podmien_dlugosc)
    page.goto(f"{adres_kokpitu}/?run={run_id}")

    zdarzenie_li = page.locator("li", has_text="tekst-bez-pomiaru")
    expect(zdarzenie_li).to_be_visible(timeout=LIMIT_WYNIKU_MS)
    expect(zdarzenie_li).to_contain_text("obcięte, pełna długość nieznana")
    expect(zdarzenie_li).not_to_contain_text("0 znaków")
    expect(zdarzenie_li).not_to_contain_text("null")
    # dowod, ze podmiana naprawde zadzialala (inaczej test sprawdzalby zwykla dlugosc)
    assert podmienione
