"""Sekrety u runnera: zmienna srodowiskowa przed plikiem 0600 (BRO-98).

Zero dodatkowych bibliotek. Bez wlasnej kryptografii - szyfrowanie kluczem
lezacym obok pliku to teatr; ochrona to prawa 0600 i to, ze plik nie lezy w repo.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG = Path(
    os.environ.get("COCKPIT_CONFIG", Path.home() / ".config" / "cockpit" / "credentials.json")
)


def get_token(name: str) -> str | None:
    env = os.environ.get(f"COCKPIT_{name.upper()}_TOKEN")
    if env:
        return env
    if not CONFIG.is_file():
        return None
    return json.loads(CONFIG.read_text()).get(name)


def set_token(name: str, value: str) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(CONFIG.read_text()) if CONFIG.is_file() else {}
    data[name] = value
    CONFIG.write_text(json.dumps(data, indent=2))
    CONFIG.chmod(0o600)   # po zapisie, nie przed


def mask(token: str) -> str:
    return f"{token[:8]}{'*' * 8}" if len(token) > 8 else "*" * 8
