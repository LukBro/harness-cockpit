from cockpit.kernel import Event, RunStore, normalize, parse_phase_mark


def test_phase_mark_parsed():
    cmd = "python3 /home/ubuntu/.claude/skills/x/references/phase_timer.py /run/dir mark B.2 start"
    assert parse_phase_mark(cmd) == ("B.2", "start")


def test_non_phase_bash_ignored():
    assert parse_phase_mark("bundle exec rspec spec/") == (None, None)


def test_normalize_phase_event():
    ev = normalize({
        "hook_event_name": "PreToolUse",
        "session_id": "s1",
        "tool_name": "Bash",
        "tool_input": {"command": "python3 phase_timer.py /d mark C end"},
    })
    assert (ev.event, ev.phase, ev.tool) == ("phase_end", "C", None)


def test_normalize_subagent():
    ev = normalize({
        "hook_event_name": "SubagentStop",
        "session_id": "s1",
        "agent_id": "a7",
        "agent_type": "Explore",
    })
    assert (ev.agent_id, ev.agent_type, ev.status) == ("a7", "Explore", "completed")


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
