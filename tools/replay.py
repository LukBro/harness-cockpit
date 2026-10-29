#!/usr/bin/env python3
"""Syntetyczny run: wysyla zdarzenia na /hook, zeby zobaczyc tablice bez pipeline'u.

    python3 tools/replay.py [--url http://127.0.0.1:8765/hook] [--speed 1.0]
"""
import argparse
import json
import time
import urllib.request
import uuid

PHASES = [("0", 2, 0), ("A", 6, 5), ("B", 4, 1), ("B.2", 5, 3), ("C", 8, 2), ("G", 2, 0)]


def post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8765/hook")
    ap.add_argument("--speed", type=float, default=1.0)
    a = ap.parse_args()
    sid = "replay-" + uuid.uuid4().hex[:8]
    tick = 0.25 / a.speed

    post(a.url, {"hook_event_name": "SessionStart", "session_id": sid})
    for phase, dur, fanout in PHASES:
        cmd = f"python3 phase_timer.py /run mark {phase} %s"
        post(a.url, {"hook_event_name": "PreToolUse", "session_id": sid,
                     "tool_name": "Bash", "tool_input": {"command": cmd % "start"}})
        ids = [uuid.uuid4().hex for _ in range(fanout)]
        kind = "Explore" if phase in ("A", "B.2") else "general-purpose"
        for i in ids:
            post(a.url, {"hook_event_name": "SubagentStart", "session_id": sid,
                         "agent_id": i, "agent_type": kind})
            time.sleep(tick / 3)
        time.sleep(tick * dur)
        for i in ids:
            post(a.url, {"hook_event_name": "SubagentStop", "session_id": sid,
                         "agent_id": i, "agent_type": kind})
        # Koniec fazy to PostToolUse (po wykonaniu komendy), jak w prawdziwych hookach.
        post(a.url, {"hook_event_name": "PostToolUse", "session_id": sid,
                     "tool_name": "Bash", "tool_input": {"command": cmd % "end"},
                     "tool_response": {"stdout": "", "stderr": "", "interrupted": False,
                                       "isImage": False, "noOutputExpected": False}})
    post(a.url, {"hook_event_name": "SessionEnd", "session_id": sid})
    print(sid)


if __name__ == "__main__":
    main()
