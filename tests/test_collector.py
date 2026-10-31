"""Tablica runow w kolektorze: znacznik ukonczenia i wynik porazki fazy (HAR-23)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from cockpit.collector import app as modul_kolektora
from cockpit.kernel import Event, RunStore


@pytest.fixture
def store(tmp_path, monkeypatch) -> RunStore:
    """Kolektor czyta z izolowanego magazynu, nie z katalogu uzytkownika."""
    s = RunStore(tmp_path)
    monkeypatch.setattr(modul_kolektora, "store", s)
    return s


def _zapisz(store: RunStore, sid: str, nazwy: list[tuple[str, str | None]]) -> None:
    """Dopisuje do logu runu zdarzenia (nazwa, faza) w podanej kolejnosci."""
    for i, (nazwa, faza) in enumerate(nazwy):
        store.append(Event(ts=1000.0 + i, session_id=sid, event=nazwa, phase=faza))


async def _wpis(sid: str) -> dict[str, Any]:
    transport = httpx.ASGITransport(app=modul_kolektora.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/api/runs")
    assert r.status_code == 200
    wpisy = [w for w in r.json() if w["session_id"] == sid]
    assert len(wpisy) == 1
    return wpisy[0]


async def test_run_ktory_padl_na_fazie_ma_wynik_porazki_mimo_zakonczenia(store):
    _zapisz(store, "s1", [("phase_start", "B"), ("phase_failed", "B"), ("Stop", None)])

    wpis = await _wpis("s1")

    assert wpis["outcome"] == "failed"
    assert wpis["finished"] is True


async def test_zakonczenie_runu_nie_znika_po_kolejnych_zdarzeniach(store):
    _zapisz(
        store,
        "s1",
        [("phase_start", "A"), ("Stop", None)] + [("PostToolUse", None)] * 5,
    )

    wpis = await _wpis("s1")

    assert wpis["finished"] is True
    assert wpis["outcome"] is None


async def test_koniec_sesji_tez_oznacza_run_jako_zakonczony(store):
    # SessionEnd daleko od konca logu: sama krotka z "Stop" by go nie znalazla.
    _zapisz(
        store,
        "s1",
        [("phase_start", "A"), ("SessionEnd", None)] + [("PostToolUse", None)] * 4,
    )

    wpis = await _wpis("s1")

    assert wpis["finished"] is True


async def test_run_bez_zakonczenia_nie_jest_oznaczony_jako_zakonczony(store):
    _zapisz(store, "s1", [("phase_start", "A"), ("PostToolUse", None), ("phase_end", "A")])

    wpis = await _wpis("s1")

    assert wpis["finished"] is False
    assert wpis["outcome"] is None


async def test_run_zakonczony_bez_porazki_nie_dostaje_wyniku_sukcesu(store):
    # Brak dowodu sukcesu to brak (measured_or_null), nie "success".
    _zapisz(store, "s1", [("phase_start", "A"), ("phase_end", "A"), ("Stop", None)])

    wpis = await _wpis("s1")

    assert wpis["finished"] is True
    assert wpis["outcome"] is None


async def test_porazka_fazy_z_poczatku_logu_nadal_decyduje_o_wyniku_runu(store):
    _zapisz(
        store,
        "s1",
        [
            ("phase_start", "B"),
            ("phase_failed", "B"),
            ("phase_start", "C"),
            ("phase_end", "C"),
            ("PostToolUse", None),
            ("PostToolUse", None),
            ("Stop", None),
        ],
    )

    wpis = await _wpis("s1")

    assert wpis["outcome"] == "failed"


async def test_porazka_jednego_runu_nie_przecieka_do_innego_runu(store):
    _zapisz(store, "padl", [("phase_start", "B"), ("phase_failed", "B"), ("Stop", None)])
    _zapisz(store, "dobry", [("phase_start", "B"), ("phase_end", "B"), ("Stop", None)])

    padl = await _wpis("padl")
    dobry = await _wpis("dobry")

    assert padl["outcome"] == "failed"
    assert dobry["outcome"] is None
