"""Przejście na instancję publiczną to zmiana zmiennych środowiskowych, nie kodu (HAR-113)."""
import os

import cockpit.__main__ as kokpit_main
from cockpit.settings import Settings


def _uruchom_serwer(monkeypatch) -> list[tuple]:
    """Startuje produkcyjne main() z konfiguracją z bieżącego środowiska, bez pliku .env."""
    monkeypatch.setattr(kokpit_main, "settings", Settings(_env_file=None))
    wywolania: list[tuple] = []
    monkeypatch.setattr(
        kokpit_main.uvicorn, "run", lambda *a, **k: wywolania.append((a, k))
    )
    assert kokpit_main.main([]) == 0
    return wywolania


def test_serwer_za_proxy_dostaje_adres_port_i_zaufane_proxy_ze_zmiennych_srodowiskowych(
    monkeypatch,
):
    monkeypatch.setenv("COCKPIT_HOST", "0.0.0.0")
    monkeypatch.setenv("COCKPIT_PORT", "9123")
    monkeypatch.setenv("COCKPIT_PROXY_HEADERS", "0")
    monkeypatch.setenv("COCKPIT_FORWARDED_ALLOW_IPS", "10.0.0.5")

    assert _uruchom_serwer(monkeypatch) == [
        (
            ("cockpit.api.app:app",),
            {
                "host": "0.0.0.0",
                "port": 9123,
                "proxy_headers": False,
                "forwarded_allow_ips": "10.0.0.5",
            },
        )
    ]


def test_bez_konfiguracji_serwer_ufa_naglowkom_proxy_tylko_z_adresu_127_0_0_1(monkeypatch):
    for nazwa in list(os.environ):
        if nazwa.upper().startswith("COCKPIT_"):
            monkeypatch.delenv(nazwa)

    # pełny słownik: forwarded_allow_ips trzeba przekazać, bo domyślnie
    # uvicorn bierze zmienną FORWARDED_ALLOW_IPS albo "127.0.0.1,::1"
    assert _uruchom_serwer(monkeypatch) == [
        (
            ("cockpit.api.app:app",),
            {
                "host": "127.0.0.1",
                "port": 8765,
                "proxy_headers": True,
                "forwarded_allow_ips": "127.0.0.1",
            },
        )
    ]
