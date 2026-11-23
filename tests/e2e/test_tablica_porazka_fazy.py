"""Tablica faz kolektora: porazka fazy jest ostateczna (HAR-154).

Guardy `if (r.failed) return;` w `src/cockpit/web/index.html` (phase_start i phase_end) sa
sprawdzane na prawdziwym kolektorze (`cockpit.collector.app:app` w procesie) i prawdziwej
stronie. Zdarzenia leca przez POST /hook PRZED otwarciem strony: SSE odtwarza je od poczatku.

Kazdy test konczy sie znacznikiem: zdarzeniem innej fazy (C). Strumien jest uporzadkowany,
wiec gdy wiersz C jest "running", wszystkie wczesniejsze zdarzenia sa juz obsluzone i klasa
wiersza B jest stanem koncowym, nie przejsciowym.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from itertools import count
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect

from procesy import uruchom_proces_sync, zakoncz_proces_sync

pytestmark = [pytest.mark.e2e]

KORZEN = Path(__file__).resolve().parents[2]
LIMIT_STARTU_S = 20
LIMIT_WYNIKU_MS = 5000
LIMIT_ODSWIEZENIA_MS = 8000    # lista runow odswieza sie co 5 s
WIERSZE = "#board tbody tr"


def _wolny_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def kolektor(tmp_path):
    """Prawdziwy kolektor w procesie na wolnym porcie, ze skladem runow w tmp_path."""
    port = _wolny_port()
    adres = f"http://127.0.0.1:{port}"
    env = {k: v for k, v in os.environ.items() if not k.startswith("COCKPIT_")}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(KORZEN / "src"), *filter(None, [env.get("PYTHONPATH")])]
    )
    env["COCKPIT_RUN_STORE"] = str(tmp_path / "store")
    log = tmp_path / "kolektor.log"
    with log.open("wb") as wyjscie:
        proc = uruchom_proces_sync(
            [sys.executable, "-m", "uvicorn", "cockpit.collector.app:app",
             "--host", "127.0.0.1", "--port", str(port)],
            env=env, cwd=KORZEN, stdout=wyjscie, stderr=subprocess.STDOUT,
        )
    try:
        koniec = time.monotonic() + LIMIT_STARTU_S
        while True:
            assert proc.poll() is None, f"kolektor nie wystartowal: {log.read_text()[-2000:]}"
            try:
                if httpx.get(f"{adres}/api/phases", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert time.monotonic() < koniec, f"kolektor nie odpowiada: {log.read_text()[-2000:]}"
            time.sleep(0.1)
        yield adres
    finally:
        zakoncz_proces_sync(proc, nazwa="kolektor e2e", limit_term=10, limit_wait=10)


_ZNACZNIK_CZASU = count(1000)


def _faza(adres, sesja, hook, faza, krawedz, **dodatkowe):
    """Zdarzenie fazy tak, jak wysyla je hook Claude Code po `phase_timer.py ... mark`."""
    tresc = {
        "hook_event_name": hook, "session_id": sesja, "ts": float(next(_ZNACZNIK_CZASU)),
        "tool_name": "Bash",
        "tool_input": {"command": f"python phase_timer.py /tmp/run mark {faza} {krawedz}"},
        **dodatkowe,
    }
    _wyslij(adres, tresc)


def _stop(adres, sesja):
    _wyslij(adres, {"hook_event_name": "Stop", "session_id": sesja,
                    "ts": float(next(_ZNACZNIK_CZASU))})


def _wyslij(adres, tresc):
    odpowiedz = httpx.post(f"{adres}/hook", json=tresc, timeout=5)
    assert odpowiedz.status_code == 200, odpowiedz.text
    assert odpowiedz.json() == {"ok": True}, odpowiedz.text


def _wiersz_fazy(page, faza):
    # regex ^B$, nie has_text="B": B.2 i B.3 tez zawieraja "B"
    return page.locator(WIERSZE).filter(
        has=page.locator("td.ph", has_text=re.compile(rf"^{re.escape(faza)}$"))
    )


def _liczba_zdarzen_runu(adres, sesja) -> int:
    lista = httpx.get(f"{adres}/api/runs", timeout=5).json()
    return next(r["events"] for r in lista if r["session_id"] == sesja)


def test_faza_po_porazce_nie_robi_sie_zielona_gdy_przychodzi_jej_zakonczenie(page, kolektor):
    sesja = "sesja-koniec-po-porazce"
    _faza(kolektor, sesja, "PreToolUse", "B", "start")
    _faza(kolektor, sesja, "PostToolUseFailure", "B", "end", error="kod wyjscia 1")
    _faza(kolektor, sesja, "PostToolUse", "B", "end")
    _faza(kolektor, sesja, "PreToolUse", "C", "start")    # znacznik konca strumienia
    assert _liczba_zdarzen_runu(kolektor, sesja) == 4

    page.goto(kolektor)

    expect(_wiersz_fazy(page, "C")).to_have_class("running", timeout=LIMIT_WYNIKU_MS)
    faza_b = _wiersz_fazy(page, "B")
    expect(faza_b).to_have_count(1)
    expect(faza_b).to_have_class("failed")
    expect(faza_b).not_to_have_class(re.compile(r"\bdone\b"))


def test_faza_po_porazce_nie_wraca_do_w_toku_gdy_przychodzi_jej_ponowny_start(page, kolektor):
    sesja = "sesja-start-po-porazce"
    _faza(kolektor, sesja, "PreToolUse", "B", "start")
    _faza(kolektor, sesja, "PostToolUseFailure", "B", "end", error="kod wyjscia 1")
    _faza(kolektor, sesja, "PreToolUse", "B", "start")
    _faza(kolektor, sesja, "PreToolUse", "C", "start")    # znacznik konca strumienia
    assert _liczba_zdarzen_runu(kolektor, sesja) == 4

    page.goto(kolektor)

    expect(_wiersz_fazy(page, "C")).to_have_class("running", timeout=LIMIT_WYNIKU_MS)
    faza_b = _wiersz_fazy(page, "B")
    expect(faza_b).to_have_count(1)
    expect(faza_b).to_have_class("failed")
    expect(faza_b).not_to_have_class(re.compile(r"\brunning\b"))


def test_faza_bez_porazki_konczy_sie_zielono_a_nie_jako_nieudana(page, kolektor):
    sesja = "sesja-bez-porazki"
    _faza(kolektor, sesja, "PreToolUse", "B", "start")
    _faza(kolektor, sesja, "PostToolUse", "B", "end")
    _faza(kolektor, sesja, "PreToolUse", "C", "start")    # znacznik konca strumienia

    page.goto(kolektor)

    expect(_wiersz_fazy(page, "C")).to_have_class("running", timeout=LIMIT_WYNIKU_MS)
    expect(_wiersz_fazy(page, "B")).to_have_class("done")


def test_run_z_nieudana_faza_jest_na_liscie_runow_oznaczony_krzyzykiem_a_nie_ptaszkiem(
    page, kolektor
):
    porazka = "porazka-faza-1"
    sukces = "sukces-faza-01"
    for sesja, hook in ((porazka, "PostToolUseFailure"), (sukces, "PostToolUse")):
        _faza(kolektor, sesja, "PreToolUse", "B", "start")
        dodatkowe = {"error": "kod wyjscia 1"} if hook == "PostToolUseFailure" else {}
        _faza(kolektor, sesja, hook, "B", "end", **dodatkowe)
        _stop(kolektor, sesja)

    page.goto(kolektor)

    # option niesie pierwsze 12 znakow identyfikatora sesji
    opcja_porazki = page.locator("#runs option").filter(has_text=porazka[:12])
    opcja_sukcesu = page.locator("#runs option").filter(has_text=sukces[:12])
    expect(opcja_porazki).to_have_count(1, timeout=LIMIT_ODSWIEZENIA_MS)
    expect(opcja_porazki).to_contain_text("✗", timeout=LIMIT_ODSWIEZENIA_MS)
    expect(opcja_porazki).not_to_contain_text("✓")
    expect(opcja_sukcesu).to_have_count(1)
    expect(opcja_sukcesu).to_contain_text("✓", timeout=LIMIT_ODSWIEZENIA_MS)
    expect(opcja_sukcesu).not_to_contain_text("✗")
