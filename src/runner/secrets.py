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
    """Zapis atomowy: plik tymczasowy 0600 od utworzenia, potem os.replace."""
    cel = CONFIG
    cel.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = json.loads(cel.read_text()) if cel.is_file() else {}
    data[name] = value
    tmp = cel.with_name(f".{cel.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, cel)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def mask(token: str) -> str:
    return f"{token[:8]}{'*' * 8}" if len(token) > 8 else "*" * 8
