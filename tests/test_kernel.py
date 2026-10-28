import json
import time
from pathlib import Path

from cockpit.kernel import Event, RunStore, normalize, parse_phase_mark


def test_phase_mark_parsed():
    cmd = "python3 /home/ubuntu/.claude/skills/x/references/phase_timer.py /run/dir mark B.2 start"
    assert parse_phase_mark(cmd) == ("B.2", "start")


def test_non_phase_bash_ignored():
    assert parse_phase_mark("bundle exec rspec spec/") == (None, None)


def _bash(hook: str, komenda: str, **pola) -> Event:
    return normalize({
        "hook_event_name": hook,
        "session_id": "s1",
        "tool_name": "Bash",
        "tool_input": {"command": komenda},
        **pola,
    })


KONIEC_C = "python3 phase_timer.py /d mark C end"
POCZATEK_C = "python3 phase_timer.py /d mark C start"


def test_normalize_phase_event():
    # Koniec fazy pochodzi ze zdarzenia PO komendzie, nie sprzed niej.
    ev = _bash("PreToolUse", KONIEC_C)
    assert (ev.event, ev.phase, ev.tool) == ("PreToolUse", None, "Bash")


def test_ta_sama_komenda_konca_fazy_daje_inny_wynik_zaleznie_od_hooka():
    wyniki = {
        "PreToolUse": _bash("PreToolUse", KONIEC_C),
        "PostToolUse": _bash("PostToolUse", KONIEC_C),
        "PostToolUseFailure": _bash("PostToolUseFailure", KONIEC_C, error="Exit code 1"),
    }

    assert (wyniki["PreToolUse"].event, wyniki["PreToolUse"].phase) == ("PreToolUse", None)
    assert (wyniki["PostToolUse"].event, wyniki["PostToolUse"].phase) == ("phase_end", "C")
    assert (wyniki["PostToolUseFailure"].event, wyniki["PostToolUseFailure"].phase) == (
        "phase_failed", "C",
    )
    assert wyniki["PostToolUse"].tool is None
    assert wyniki["PostToolUseFailure"].tool is None


def test_poczatek_fazy_liczy_sie_tylko_przed_wykonaniem_komendy():
    pre = _bash("PreToolUse", POCZATEK_C)
    assert (pre.event, pre.phase, pre.tool) == ("phase_start", "C", None)

    for hook in ("PostToolUse", "PostToolUseFailure"):
        ev = _bash(hook, POCZATEK_C)
        assert (ev.event, ev.phase, ev.tool) == (hook, None, "Bash"), hook


def test_porazka_komendy_ze_znacznikiem_konca_przenosi_tresc_bledu_bez_statusu():
    ev = _bash("PostToolUseFailure", KONIEC_C, error="Exit code 2")

    assert ev.event == "phase_failed"
    assert ev.extra["error"] == "Exit code 2"
    assert ev.status is None
    d = ev.to_dict()
    assert "status" not in d
    assert d["extra"] == {"error": "Exit code 2"}


def test_porazka_komendy_bez_tresci_bledu_nie_wymysla_pola_error():
    ev = _bash("PostToolUseFailure", KONIEC_C)

    assert ev.event == "phase_failed"
    assert "error" not in ev.extra
    assert ev.status is None
    assert "extra" not in ev.to_dict()
    assert "status" not in ev.to_dict()


def test_pole_error_w_payloadzie_nie_trafia_do_zdarzen_innych_niz_porazka_fazy():
    # Chroni czlon `phase_event == "phase_failed"` w warunku przeniesienia `error`.
    for hook, komenda, oczekiwane in (
        ("PostToolUse", KONIEC_C, "phase_end"),
        ("PreToolUse", POCZATEK_C, "phase_start"),
    ):
        ev = _bash(hook, komenda, error="x")
        assert ev.event == oczekiwane, hook
        assert "error" not in ev.extra, hook
        assert "extra" not in ev.to_dict(), hook


def test_koniec_fazy_po_udanej_komendzie_nie_dostaje_pola_error_ani_statusu():
    ev = _bash("PostToolUse", KONIEC_C, tool_response={"stdout": "", "stderr": ""})

    assert ev.event == "phase_end"
    assert "error" not in ev.extra
    assert ev.status is None
    assert "extra" not in ev.to_dict()


def test_hooki_przykladowe_rejestruja_znacznik_fazy_przed_i_po_wykonaniu_komendy():
    plik = Path(__file__).resolve().parent.parent / "deploy" / "hooks.example.json"
    hooki = json.loads(plik.read_text(encoding="utf-8"))["hooks"]

    for nazwa in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        wpisy = hooki.get(nazwa)
        assert wpisy, f"brak rejestracji phase_timer dla {nazwa}"
        assert len(wpisy) == 1, nazwa
        assert wpisy[0]["matcher"] == "Bash", nazwa
        assert len(wpisy[0]["hooks"]) == 1, nazwa
        assert wpisy[0]["hooks"][0]["if"] == "Bash(*phase_timer*)", nazwa
    # Nowe wpisy maja ten sam ksztalt co istniejacy PreToolUse.
    assert hooki["PostToolUse"] == hooki["PreToolUse"]
    assert hooki["PostToolUseFailure"] == hooki["PreToolUse"]


def test_zdarzenie_bez_nazwy_hooka_ma_nazwe_nieznana():
    ev = normalize({"session_id": "s1"})
    assert ev.event == "unknown"


def test_zdarzenie_bez_identyfikatora_sesji_ma_sesje_nieznana():
    ev = normalize({"hook_event_name": "Stop"})
    assert ev.session_id == "unknown"


def test_identyfikator_sesji_z_hooka_zostaje_zachowany():
    ev = normalize({"hook_event_name": "Stop", "session_id": "sesja-42"})
    assert ev.session_id == "sesja-42"


def test_czas_zdarzenia_pochodzi_z_hooka_gdy_jest():
    ev = normalize({"hook_event_name": "Stop", "session_id": "s1", "ts": 123.5})
    assert ev.ts == 123.5


def test_czas_zdarzenia_bez_czasu_w_hooku_to_chwila_przyjecia():
    przed = time.time()
    ev = normalize({"hook_event_name": "Stop", "session_id": "s1"})
    po = time.time()
    assert isinstance(ev.ts, float)
    assert przed <= ev.ts <= po


def test_zakonczenie_subagenta_nie_ma_statusu_bez_zrodla():
    # Payload SubagentStop niesie tylko agent_id i agent_type, nic o wyniku.
    # Brak informacji zapisujemy jako brak (measured_or_null), nie jako powodzenie.
    ev = normalize({
        "hook_event_name": "SubagentStop",
        "session_id": "s1",
        "agent_id": "a7",
        "agent_type": "Explore",
    })
    assert ev.agent_id == "a7"
    assert ev.agent_type == "Explore"
    assert ev.status is None
    # Skutek w danych trafiajacych do NDJSON: pole statusu w ogole nie powstaje.
    assert "status" not in ev.to_dict()


def test_unknown_event_survives():
    ev = normalize({"hook_event_name": "Whatever", "session_id": "s1"})
    assert ev.event == "Whatever"


def test_store_roundtrip(tmp_path):
    store = RunStore(tmp_path)
    store.append(Event(ts=1.0, session_id="s1", event="SessionStart"))
    store.append(Event(ts=2.0, session_id="s1", event="Stop"))
    assert [r["event"] for r in store.read("s1")] == ["SessionStart", "Stop"]
    assert store.runs() == ["s1"]


def test_store_rejects_path_traversal(tmp_path):
    store = RunStore(tmp_path)
    try:
        store.run_dir("../escape")
    except ValueError:
        return
    raise AssertionError("powinno rzucic ValueError")
