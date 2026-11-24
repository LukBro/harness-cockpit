"""Rozdzielacz bramki testow (2026-11-24 wlasciciel): testy ida na najluzniejsza zdrowa maszyne puli."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

KORZEN = Path(__file__).resolve().parents[1]
SKRYPTY = KORZEN / ".claude" / "skills" / "praca-ticketu" / "scripts"
SZABLON = KORZEN / ".claude" / "skills" / "praca-ticketu" / "bramki-szablon.json"
BROWAR = {"ssh": "browar", "katalog": "~/h", "obraz": "obraz:1", "shardy": 2, "sloty": 2}


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location("rozdzielacz_test", SKRYPTY / "rozdzielacz.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _pomiar(rdzenie, obciazenie, pamiec_gb=16.0):
    return {"rdzenie": rdzenie, "obciazenie": obciazenie, "pamiec_gb": pamiec_gb}


@pytest.fixture
def swiat(mod, tmp_path, monkeypatch):
    """Atrapy pomiarow i uruchomien; `stan` steruje wynikiem, `wolane` zapisuje, co sie wykonalo."""
    stan = {"lokalne": _pomiar(8, 6.0), "zdalne": {"browar": _pomiar(8, 0.4)}, "pula": [BROWAR],
            "kod_zdalny": 0, "kod_lokalny": 0}
    wolane = []
    dziennik = tmp_path / "rozdzielacz.log"
    monkeypatch.setattr(mod, "wczytaj_pule", lambda: stan["pula"])
    monkeypatch.setattr(mod, "plik_dziennika", lambda: dziennik)
    monkeypatch.setattr(mod, "obciazenie_lokalne", lambda: stan["lokalne"])

    def zdalne(m):
        w = stan["zdalne"][m["ssh"]]
        if isinstance(w, Exception):
            raise w
        return w
    monkeypatch.setattr(mod, "obciazenie_zdalne", zdalne)
    monkeypatch.setattr(mod, "uruchom_zdalnie", lambda m, argv: wolane.append(("zdalnie", m["ssh"], argv))
                        or stan["kod_zdalny"])
    monkeypatch.setattr(mod, "uruchom_lokalnie", lambda sloty, timeout, argv: wolane.append(("lokalnie", sloty, argv))
                        or stan["kod_lokalny"])
    return stan, wolane, dziennik


KOMENDA = ["--sloty", "2", "--", "python", "tools/testy.py", "-q", "-n", "4"]


def test_przeciazona_maszyna_sesji_wysyla_testy_na_luzniejsza_maszyne_puli(mod, swiat):
    stan, wolane, dziennik = swiat
    assert mod.main(KOMENDA) == 0
    assert wolane == [("zdalnie", "browar", ["python", "tools/testy.py", "-q", "-n", "4"])]
    assert "-> browar" in dziennik.read_text()


def test_kod_testow_z_maszyny_zdalnej_wraca_bez_zmian_i_bez_ponowki_lokalnej(mod, swiat):
    stan, wolane, _ = swiat
    stan["kod_zdalny"] = 1
    assert mod.main(KOMENDA) == 1
    assert [w[0] for w in wolane] == ["zdalnie"]


def test_niewielka_roznica_obciazenia_zostawia_testy_na_maszynie_sesji(mod, swiat):
    stan, wolane, _ = swiat
    stan["lokalne"], stan["zdalne"]["browar"] = _pomiar(8, 2.4), _pomiar(8, 1.6)  # 0,30 wobec 0,20 na rdzen
    assert mod.main(KOMENDA) == 0
    assert wolane == [("lokalnie", 2, ["python", "tools/testy.py", "-q", "-n", "4"])]


def test_maszyna_zdalna_z_mala_pamiecia_nie_dostaje_testow(mod, swiat):
    stan, wolane, _ = swiat
    stan["zdalne"]["browar"] = _pomiar(8, 0.1, pamiec_gb=3.0)
    mod.main(KOMENDA)
    assert [w[0] for w in wolane] == ["lokalnie"]


def test_nieudany_pomiar_maszyny_zdalnej_to_wpis_w_dzienniku_i_testy_lokalnie(mod, swiat):
    stan, wolane, dziennik = swiat
    stan["zdalne"]["browar"] = mod.BladMaszyny("ssh: timeout")
    assert mod.main(KOMENDA) == 0
    assert [w[0] for w in wolane] == ["lokalnie"]
    assert "ROZDZIELACZ ZAWIODL: browar pomiar" in dziennik.read_text()


def test_awaria_maszyny_zdalnej_przy_testach_to_wpis_w_dzienniku_i_ponowka_lokalna(mod, swiat):
    stan, wolane, dziennik = swiat
    stan["kod_zdalny"], stan["kod_lokalny"] = mod.KOD_INFRA, 1
    assert mod.main(KOMENDA) == 1
    assert [w[0] for w in wolane] == ["zdalnie", "lokalnie"]
    assert f"ROZDZIELACZ ZAWIODL: browar kod {mod.KOD_INFRA}" in dziennik.read_text()


def test_zerwane_polaczenie_ssh_tez_jest_awaria_maszyny_a_nie_wynikiem_testow(mod, swiat):
    stan, wolane, _ = swiat
    stan["kod_zdalny"] = 255
    mod.main(KOMENDA)
    assert [w[0] for w in wolane] == ["zdalnie", "lokalnie"]


def test_bez_maszyn_puli_testy_ida_lokalnie_bez_pomiarow_zdalnych(mod, swiat):
    stan, wolane, _ = swiat
    stan["pula"] = []
    stan["zdalne"] = {}
    assert mod.main(KOMENDA) == 0
    assert [w[0] for w in wolane] == ["lokalnie"]


def test_wybierana_jest_najluzniejsza_z_kilku_maszyn(mod):
    a, b = {**BROWAR, "ssh": "a"}, {**BROWAR, "ssh": "b"}
    wybor = mod.wybierz(_pomiar(8, 7.0), [(a, _pomiar(8, 3.0)), (b, _pomiar(8, 0.8))])
    assert wybor["ssh"] == "b"


@pytest.mark.parametrize("argv", [[], ["python"], ["--sloty", "0", "--", "x"], ["--sloty", "x", "--", "x"],
                                  ["--", ], ["--nieznana", "--", "x"]])
def test_zle_uzycie_konczy_sie_kodem_64_bez_uruchamiania(mod, swiat, argv):
    _, wolane, _ = swiat
    assert mod.main(argv) == 64
    assert wolane == []


def test_skrypt_zdalny_odtwarza_kopie_i_puszcza_komende_w_kontenerze_ze_slotem(mod):
    s = mod.skrypt_zdalny(BROWAR, "praca-1", "abc123", ["python", "tools/testy.py", "-q"])
    assert "docker run --rm" in s and "obraz:1" in s
    assert "python tools/testy.py -q" in s
    assert "checkout -q --detach abc123" in s
    assert "flock -n -E" in s
    assert f"exit {mod.KOD_INFRA}" in s


def test_szablon_bramki_testow_idzie_przez_rozdzielacz_z_dwoma_slotami():
    testy = next(b for b in json.loads(SZABLON.read_text(encoding="utf-8")) if b["nazwa"] == "testy")
    argv = testy["argv"]
    assert argv[1].endswith("rozdzielacz.py")
    assert argv[argv.index("--sloty") + 1] == "2"
    assert argv[argv.index("--") + 1:][:2] == ["python", "tools/testy.py"]


def test_lokalny_przebieg_idzie_przez_test_lock_z_tymi_samymi_slotami_i_limitem(mod, monkeypatch):
    wolane = []
    monkeypatch.setattr(mod.subprocess, "run", lambda argv, **k: wolane.append(argv) or type("R", (), {"returncode": 3})())
    assert mod.uruchom_lokalnie(2, 3600, ["python", "tools/testy.py"]) == 3
    assert wolane[0][1].endswith("test_lock.py")
    assert wolane[0][2:] == ["--sloty", "2", "--timeout", "3600", "--", "python", "tools/testy.py"]
