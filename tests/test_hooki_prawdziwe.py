"""Kernel na prawdziwych payloadach hookow Claude Code 2.1.311 (fixture'y).

Pochodzenie i zastrzezenia: tests/fixtures/hooks/README.md.
Wazne: komendy `phase_timer.py` w `fazy-atrapa.ndjson` pochodza od ATRAPY
skryptu, nie od realnego skryptu z palserwis.
"""

from __future__ import annotations

import json
from pathlib import Path

from cockpit.kernel import Event, normalize

FIXTURES = Path(__file__).parent / "fixtures" / "hooks"

KOMENDA_B2_START = "python3 /tmp/har40/phase_timer.py /tmp/har40/run mark B.2 start"
KOMENDA_B2_END = "python3 /tmp/har40/phase_timer.py /tmp/har40/run mark B.2 end"


def _wczytaj(nazwa: str) -> list[tuple[dict, Event]]:
    """Zwraca pary (surowy payload, zdarzenie po normalizacji) w kolejnosci pliku."""
    linie = (FIXTURES / nazwa).read_text(encoding="utf-8").splitlines()
    payloady = [json.loads(linia) for linia in linie if linia.strip()]
    return [(p, normalize(p)) for p in payloady]


def _zdarzenia(nazwa: str, hook: str, **filtr) -> list[tuple[dict, Event]]:
    return [
        (p, ev)
        for p, ev in _wczytaj(nazwa)
        if p["hook_event_name"] == hook
        and all(p.get(k) == v for k, v in filtr.items())
    ]


def test_zapis_pliku_przez_subagenta_zachowuje_jego_tozsamosc_i_narzedzie():
    # Chroni: agent_id=, agent_type=, session_id= oraz ev.tool = tool_name.
    pary = _zdarzenia("subagenty.ndjson", "PreToolUse", tool_name="Write")
    assert len(pary) == 1
    payload, ev = pary[0]

    assert ev.tool == "Write"
    assert ev.agent_id == "agent-1"
    assert ev.agent_type == "general-purpose"
    assert ev.session_id == payload["session_id"] == "session-subagenty-1"
    assert ev.phase is None


def test_edycja_pliku_przez_subagenta_nalezy_do_tego_samego_agenta_co_zapis():
    # Chroni: agent_id= (Edit i Write maja ten sam klucz tozsamosci).
    zapis = _zdarzenia("subagenty.ndjson", "PreToolUse", tool_name="Write")[0][1]
    edycje = _zdarzenia("subagenty.ndjson", "PostToolUse", tool_name="Edit")
    assert len(edycje) == 1
    edycja = edycje[0][1]

    assert edycja.tool == "Edit"
    assert (edycja.session_id, edycja.agent_id) == (zapis.session_id, zapis.agent_id)


def test_rownolegle_subagenty_maja_rozne_tozsamosci_zgodne_z_ich_startem():
    # Chroni: agent_id= i agent_type= - bez nich wszystkie zdarzenia zlewaja
    # sie w watek glowny albo w jednego agenta.
    zdarzenia = [ev for _, ev in _wczytaj("subagenty.ndjson")]
    starty = [e for e in zdarzenia if e.event == "SubagentStart"]

    assert {e.agent_id for e in starty} == {"agent-1", "agent-2"}
    typ_agenta = {e.agent_id: e.agent_type for e in starty}
    assert typ_agenta == {"agent-1": "general-purpose", "agent-2": "Explore"}

    narzedzia_subagentow = [
        e for e in zdarzenia
        if e.event in ("PreToolUse", "PostToolUse") and e.agent_id is not None
    ]
    assert narzedzia_subagentow, "fixture ma zdarzenia narzedzi subagentow"
    for e in narzedzia_subagentow:
        assert e.agent_id in typ_agenta
        assert e.agent_type == typ_agenta[e.agent_id]

    narzedzia_po_agencie: dict[str, set[str]] = {}
    for e in narzedzia_subagentow:
        narzedzia_po_agencie.setdefault(e.agent_id, set()).add(e.tool)
    assert narzedzia_po_agencie == {
        "agent-1": {"Write", "Edit"},
        "agent-2": {"Read", "Bash"},
    }


def test_odrzucony_zapis_subagenta_nadal_jest_przypisany_do_swojego_agenta():
    # Chroni: agent_id= na ciezszym ukladzie - trzy watki (glowny + 2 subagenty),
    # przeplot zdarzen, a Write zostal odmowiony (brak PostToolUse dla Write).
    pary = _zdarzenia("subagenty-odmowa.ndjson", "PreToolUse", tool_name="Write")
    assert len(pary) == 1
    _, zapis = pary[0]
    assert (zapis.agent_id, zapis.agent_type) == ("agent-1", "general-purpose")

    odczyt = _zdarzenia("subagenty-odmowa.ndjson", "PreToolUse", tool_name="Read")[0][1]
    assert (odczyt.agent_id, odczyt.agent_type) == ("agent-2", "Explore")


def test_zdarzenia_sesji_glownej_nie_naleza_do_zadnego_subagenta():
    # Chroni: agent_id=payload.get(...) nie moze wymyslac wartosci dla watku
    # glownego (Agent to narzedzie sesji glownej, nie subagenta).
    wywolania = _zdarzenia("subagenty.ndjson", "PreToolUse", tool_name="Agent")
    assert len(wywolania) == 2
    for payload, ev in wywolania:
        assert "agent_id" not in payload
        assert ev.tool == "Agent"
        assert ev.agent_id is None
        assert ev.agent_type is None

    for nazwa in ("subagenty.ndjson", "subagenty-odmowa.ndjson", "fazy-atrapa.ndjson"):
        for payload, ev in _zdarzenia(nazwa, "Stop"):
            assert ev.agent_id is None, nazwa


def test_znacznik_poczatku_fazy_b2_z_prawdziwego_pretooluse_daje_phase_start():
    """Komenda pochodzi od ATRAPY phase_timer.py (format niepotwierdzony w palserwis)."""
    # Chroni: parse_phase_mark/_PHASE_MARK (kropka w nazwie fazy, krawedz start),
    # ev.event = f"phase_{edge}", ev.phase oraz ev.tool = None.
    pary = [
        (p, ev) for p, ev in _zdarzenia("fazy-atrapa.ndjson", "PreToolUse")
        if p["tool_input"]["command"] == KOMENDA_B2_START
    ]
    assert len(pary) == 1
    _, ev = pary[0]

    assert ev.event == "phase_start"
    assert ev.phase == "B.2"
    assert ev.tool is None


def test_znacznik_konca_fazy_b2_z_prawdziwego_pretooluse_daje_phase_end():
    """Komenda pochodzi od ATRAPY phase_timer.py (format niepotwierdzony w palserwis)."""
    # Chroni: rozroznienie krawedzi start/end w _PHASE_MARK i w nazwie zdarzenia.
    pary = [
        (p, ev) for p, ev in _zdarzenia("fazy-atrapa.ndjson", "PreToolUse")
        if p["tool_input"]["command"] == KOMENDA_B2_END
    ]
    assert len(pary) == 1
    _, ev = pary[0]

    assert ev.event == "phase_end"
    assert ev.phase == "B.2"
    assert ev.tool is None


def test_znacznik_fazy_zakonczony_sukcesem_daje_to_samo_po_posttooluse():
    """Komenda pochodzi od ATRAPY phase_timer.py (format niepotwierdzony w palserwis)."""
    # Chroni: obecnosc "PostToolUse" w krotce hookow, ktore niosa znacznik fazy.
    pary = [
        (p, ev) for p, ev in _zdarzenia("fazy-atrapa.ndjson", "PostToolUse")
        if p["tool_input"]["command"] == KOMENDA_B2_START
    ]
    assert len(pary) == 1
    _, ev = pary[0]
    assert (ev.event, ev.phase) == ("phase_start", "B.2")


def test_zwykle_polecenie_basha_subagenta_nie_jest_znacznikiem_fazy():
    # Chroni: warunek `if phase` - `ls` zostaje narzedziem Bash, bez fazy.
    pary = _zdarzenia("subagenty.ndjson", "PreToolUse", tool_name="Bash")
    assert len(pary) == 1
    _, ev = pary[0]

    assert ev.event == "PreToolUse"
    assert ev.tool == "Bash"
    assert ev.phase is None
    assert ev.agent_id == "agent-2"


def test_zakonczenie_subagenta_ma_status_completed_i_zachowuje_tozsamosc():
    # Chroni: `if name == "SubagentStop": ev.status = "completed"` oraz agent_id.
    stopy = _zdarzenia("subagenty.ndjson", "SubagentStop")
    assert {ev.agent_id for _, ev in stopy} == {"agent-1", "agent-2"}
    for _, ev in stopy:
        assert ev.event == "SubagentStop"
        assert ev.status == "completed"


def test_tylko_zakonczenie_subagenta_dostaje_status_completed():
    # Chroni: zawezenie statusu do SubagentStop (gdyby status dostawal tez start
    # subagenta albo Stop sesji, ten test oblewa).
    for nazwa in ("subagenty.ndjson", "fazy-atrapa.ndjson"):
        for payload, ev in _wczytaj(nazwa):
            if payload["hook_event_name"] != "SubagentStop":
                assert ev.status is None, (nazwa, payload["hook_event_name"])
