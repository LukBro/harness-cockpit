"""HAR-95: konsensus K z N wariantow kroku z `fanout` i zachowanie rozbieznych wariantow (TDD).

Czesc 1: czyste funkcje `runner.konsensus.werdykt` i `rozstrzygnij` na prawdziwych plikach w tmp_path.
Czesc 2: prawdziwy `runner.silnik.wykonaj_run` z krokiem `fanout: 3, consensus: 2`; zaslepiony jest
tylko agent (`claude`): atrapa wariantu zapisuje artefakt, ktorego pierwsza linia to werdykt zadany
per agent_id (trzecia linia to jego agent_id, zeby po tresci poznac, czyj to plik).
Asercje czytaja dane: pliki w worktree i zdarzenia runu.

Przyjety interfejs (zalozenie testow, do potwierdzenia w implementacji):
  WynikWariantu(status: str, artefakty: tuple[Path, ...])      # status "zakonczony" = zdany
  Rozstrzygniecie(zdany: bool, zwyciezca: str | None, opis: str)
  rozstrzygnij(warianty: Mapping[str, WynikWariantu], k: int) -> Rozstrzygniecie
  opis przy braku progu: "<agent_id>: <werdykt>" dla kazdego wariantu, wariant bez werdyktu
  to "<agent_id>: brak werdyktu"; remis grup opisuje slowo "remis".
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cockpit.services.invariants import profil_roli
from runner.krok import wykonaj_krok
from test_silnik_runu import K, rodzaje, swiat, uruchom

LIMIT_S = 60

OKRESLONY = "zakonczony"


def plik(tmp_path: Path, nazwa: str, tresc: str | bytes) -> Path:
    sciezka = tmp_path / nazwa
    if isinstance(tresc, bytes):
        sciezka.write_bytes(tresc)
    else:
        sciezka.write_text(tresc, encoding="utf-8")
    return sciezka


def wariant(tmp_path: Path, agent: str, tresc: str | bytes, status: str = OKRESLONY):
    from runner.konsensus import WynikWariantu

    return WynikWariantu(status=status, artefakty=(plik(tmp_path, f"{agent}.md", tresc),))


# --- werdykt: pierwsza niepusta linia pierwszego artefaktu -----------------------------------


def test_werdykt_to_pierwsza_niepusta_linia_artefaktu(tmp_path):
    from runner.konsensus import werdykt

    assert werdykt([plik(tmp_path, "a.md", "\n  \n\nTAK\ndruga linia\n")]) == "tak"


def test_werdykt_ignoruje_wielkosc_liter_i_biale_znaki_na_brzegach(tmp_path):
    from runner.konsensus import werdykt

    assert werdykt([plik(tmp_path, "a.md", "  ZaTwIeRdZoNo \t\n")]) == "zatwierdzono"


def test_werdykt_porownuje_litery_bez_wzgledu_na_wielkosc_takze_ss_i_eszet(tmp_path):
    from runner.konsensus import werdykt

    assert werdykt([plik(tmp_path, "a.md", "STRASSE\n")]) == werdykt(
        [plik(tmp_path, "b.md", "straße\n")])


def test_werdykt_bierze_tylko_pierwszy_artefakt_kroku(tmp_path):
    from runner.konsensus import werdykt

    pliki = [plik(tmp_path, "a.md", "tak\n"), plik(tmp_path, "b.md", "nie\n")]
    assert werdykt(pliki) == "tak"


def test_werdykt_pustego_artefaktu_to_brak_werdyktu(tmp_path):
    from runner.konsensus import werdykt

    assert werdykt([plik(tmp_path, "a.md", "")]) == ""


def test_werdykt_artefaktu_z_samymi_bialymi_znakami_to_brak_werdyktu(tmp_path):
    from runner.konsensus import werdykt

    assert werdykt([plik(tmp_path, "a.md", " \n\t\n  \n")]) == ""


def test_werdykt_bez_artefaktow_to_brak_werdyktu():
    from runner.konsensus import werdykt

    assert werdykt([]) == ""


def test_werdykt_brakujacego_pliku_to_brak_werdyktu_nie_wyjatek(tmp_path):
    from runner.konsensus import werdykt

    assert werdykt([tmp_path / "nie_ma.md"]) == ""


def test_werdykt_katalogu_zamiast_pliku_to_brak_werdyktu_nie_wyjatek(tmp_path):
    from runner.konsensus import werdykt

    katalog = tmp_path / "plan.md"
    katalog.mkdir()
    assert werdykt([katalog]) == ""


def test_werdykt_pliku_w_zlym_kodowaniu_to_brak_werdyktu_nie_wyjatek(tmp_path):
    from runner.konsensus import werdykt

    assert werdykt([plik(tmp_path, "a.md", b"\xff\xfe\xff\n")]) == ""


# --- rozstrzygnij: glosy, prog K, remis ---------------------------------------------------


def test_dwa_z_trzech_zgodne_przy_progu_dwa_zdaja_krok_a_zwyciezca_jest_pierwszy_z_wiekszosci(
    tmp_path,
):
    from runner.konsensus import rozstrzygnij

    warianty = {
        "a1": wariant(tmp_path, "a1", "NIE\n"),
        "a2": wariant(tmp_path, "a2", "TAK\n"),
        "a3": wariant(tmp_path, "a3", "TAK\n"),
    }

    wynik = rozstrzygnij(warianty, 2)

    assert wynik.zdany is True
    assert wynik.zwyciezca == "a2"                 # a1 nie jest w wiekszosci, mimo ze pierwszy


def test_przy_progu_jeden_wygrywa_pierwszy_z_najwiekszej_grupy_a_nie_pierwszy_glosujacy(tmp_path):
    from runner.konsensus import rozstrzygnij

    warianty = {
        "a1": wariant(tmp_path, "a1", "nie"),
        "a2": wariant(tmp_path, "a2", "tak"),
        "a3": wariant(tmp_path, "a3", "tak"),
    }

    r = rozstrzygnij(warianty, 1)

    assert r.zdany is True
    assert r.zwyciezca == "a2"      # a1 ma werdykt z grupy po jednym glosie, a2 i a3 maja grupe dwoch


def test_dokladnie_k_zgodnych_wystarcza_prog_jest_wlacznie(tmp_path):
    from runner.konsensus import rozstrzygnij

    warianty = {
        "a1": wariant(tmp_path, "a1", "tak\n"),
        "a2": wariant(tmp_path, "a2", "tak\n"),
        "a3": wariant(tmp_path, "a3", "nie\n"),
    }

    assert rozstrzygnij(warianty, 2).zdany is True      # K=2, grupa ma dokladnie 2


def test_wszystkie_trzy_zgodne_przy_progu_trzy_zdaja_krok(tmp_path):
    from runner.konsensus import rozstrzygnij

    warianty = {a: wariant(tmp_path, a, "tak\n") for a in ("a1", "a2", "a3")}

    wynik = rozstrzygnij(warianty, 3)

    assert wynik.zdany is True
    assert wynik.zwyciezca == "a1"


def test_o_jeden_za_malo_zgodnych_nie_zdaje_kroku(tmp_path):
    from runner.konsensus import rozstrzygnij

    warianty = {
        "a1": wariant(tmp_path, "a1", "tak\n"),
        "a2": wariant(tmp_path, "a2", "tak\n"),
        "a3": wariant(tmp_path, "a3", "nie\n"),
    }

    wynik = rozstrzygnij(warianty, 3)

    assert wynik.zdany is False
    assert wynik.zwyciezca is None


def test_trzy_rozne_werdykty_przy_progu_dwa_nie_zdaja_a_opis_wymienia_kazdy_z_agent_id(tmp_path):
    from runner.konsensus import rozstrzygnij

    warianty = {
        "a1": wariant(tmp_path, "a1", "tak\n"),
        "a2": wariant(tmp_path, "a2", "nie\n"),
        "a3": wariant(tmp_path, "a3", "moze\n"),
    }

    wynik = rozstrzygnij(warianty, 2)

    assert wynik.zdany is False
    for fragment in ("a1: tak", "a2: nie", "a3: moze"):
        assert fragment in wynik.opis


def test_opis_porazki_oznacza_wariant_bez_werdyktu_jako_brak_werdyktu(tmp_path):
    from runner.konsensus import rozstrzygnij

    warianty = {
        "a1": wariant(tmp_path, "a1", "tak\n"),
        "a2": wariant(tmp_path, "a2", ""),
        "a3": wariant(tmp_path, "a3", "nie\n"),
    }

    opis = rozstrzygnij(warianty, 2).opis

    assert "a2: brak werdyktu" in opis
    assert "a1: tak" in opis


def test_wariant_nieudany_nie_glosuje_mimo_zgodnego_artefaktu(tmp_path):
    from runner.konsensus import rozstrzygnij

    warianty = {
        "a1": wariant(tmp_path, "a1", "tak\n", status="nieudany"),
        "a2": wariant(tmp_path, "a2", "tak\n"),
        "a3": wariant(tmp_path, "a3", "nie\n"),
    }

    wynik = rozstrzygnij(warianty, 2)

    assert wynik.zdany is False                    # zgodne sa 2 pliki, ale glosuje tylko jeden


def test_wariant_nieudany_nie_moze_byc_zwyciezca_nawet_gdy_jest_pierwszy(tmp_path):
    from runner.konsensus import rozstrzygnij

    warianty = {
        "a1": wariant(tmp_path, "a1", "tak\n", status="nieudany"),
        "a2": wariant(tmp_path, "a2", "tak\n"),
        "a3": wariant(tmp_path, "a3", "tak\n"),
    }

    wynik = rozstrzygnij(warianty, 2)

    assert wynik.zdany is True
    assert wynik.zwyciezca == "a2"


def test_wariant_z_pustym_artefaktem_nie_glosuje(tmp_path):
    from runner.konsensus import rozstrzygnij

    warianty = {
        "a1": wariant(tmp_path, "a1", ""),
        "a2": wariant(tmp_path, "a2", "\n"),
        "a3": wariant(tmp_path, "a3", "tak\n"),
    }

    assert rozstrzygnij(warianty, 2).zdany is False    # puste werdykty nie tworza grupy "brak"


def test_wariant_bez_pliku_artefaktu_nie_glosuje(tmp_path):
    from runner.konsensus import WynikWariantu, rozstrzygnij

    warianty = {
        "a1": WynikWariantu(status=OKRESLONY, artefakty=(tmp_path / "nie_ma_1.md",)),
        "a2": WynikWariantu(status=OKRESLONY, artefakty=(tmp_path / "nie_ma_2.md",)),
        "a3": wariant(tmp_path, "a3", "tak\n"),
    }

    assert rozstrzygnij(warianty, 2).zdany is False


def test_wariant_bez_zadnych_artefaktow_nie_glosuje(tmp_path):
    from runner.konsensus import WynikWariantu, rozstrzygnij

    warianty = {
        "a1": WynikWariantu(status=OKRESLONY, artefakty=()),
        "a2": WynikWariantu(status=OKRESLONY, artefakty=()),
        "a3": wariant(tmp_path, "a3", "tak\n"),
    }

    assert rozstrzygnij(warianty, 2).zdany is False


def test_wariant_z_artefaktem_w_zlym_kodowaniu_nie_glosuje_a_rozstrzygniecie_nie_rzuca(tmp_path):
    from runner.konsensus import rozstrzygnij

    warianty = {
        "a1": wariant(tmp_path, "a1", "tak\n"),
        "a2": wariant(tmp_path, "a2", b"\xff\xfe\xff\n"),
        "a3": wariant(tmp_path, "a3", "tak\n"),
    }

    assert rozstrzygnij(warianty, 3).zdany is False
    assert rozstrzygnij(warianty, 2).zwyciezca == "a1"


def test_wariant_z_katalogiem_zamiast_artefaktu_nie_glosuje_a_rozstrzygniecie_nie_rzuca(tmp_path):
    from runner.konsensus import WynikWariantu, rozstrzygnij

    katalog = tmp_path / "a2_plan.md"
    katalog.mkdir()
    warianty = {
        "a1": wariant(tmp_path, "a1", "tak\n"),
        "a2": WynikWariantu(status=OKRESLONY, artefakty=(katalog,)),
        "a3": wariant(tmp_path, "a3", "tak\n"),
    }

    assert rozstrzygnij(warianty, 3).zdany is False


def test_remis_dwoch_najwiekszych_grup_wygrywa_grupa_pierwszego_agenta_w_kolejnosci(tmp_path):
    from runner.konsensus import rozstrzygnij

    # N=4, K=2: dwie grupy po dwa. Alfabetycznie "nie" < "tak", wiec wybor po tresci werdyktu
    # dalby a2; kolejnosc agentow daje a1.
    warianty = {
        "a1": wariant(tmp_path, "a1", "tak\n"),
        "a2": wariant(tmp_path, "a2", "nie\n"),
        "a3": wariant(tmp_path, "a3", "tak\n"),
        "a4": wariant(tmp_path, "a4", "nie\n"),
    }

    wynik = rozstrzygnij(warianty, 2)

    assert wynik.zdany is True
    assert wynik.zwyciezca == "a1"
    assert "remis" in wynik.opis


def test_remis_rozstrzyga_pierwszy_agent_takze_gdy_jego_grupa_ma_pozniejszy_werdykt_alfabetycznie(
    tmp_path,
):
    from runner.konsensus import rozstrzygnij

    warianty = {
        "a1": wariant(tmp_path, "a1", "nie\n"),
        "a2": wariant(tmp_path, "a2", "tak\n"),
        "a3": wariant(tmp_path, "a3", "tak\n"),
        "a4": wariant(tmp_path, "a4", "nie\n"),
    }

    assert rozstrzygnij(warianty, 2).zwyciezca == "a1"


def test_bez_remisu_opis_zdanego_rozstrzygniecia_nie_mowi_o_remisie(tmp_path):
    from runner.konsensus import rozstrzygnij

    warianty = {
        "a1": wariant(tmp_path, "a1", "tak\n"),
        "a2": wariant(tmp_path, "a2", "tak\n"),
        "a3": wariant(tmp_path, "a3", "nie\n"),
    }

    assert "remis" not in rozstrzygnij(warianty, 2).opis


def test_remis_ponizej_progu_nie_zdaje_kroku(tmp_path):
    from runner.konsensus import rozstrzygnij

    warianty = {
        "a1": wariant(tmp_path, "a1", "tak\n"),
        "a2": wariant(tmp_path, "a2", "nie\n"),
        "a3": wariant(tmp_path, "a3", "tak\n"),
        "a4": wariant(tmp_path, "a4", "nie\n"),
    }

    assert rozstrzygnij(warianty, 3).zdany is False


def test_werdykty_roznia_sie_tylko_wielkoscia_liter_i_bialymi_znakami_licza_sie_jako_zgodne(
    tmp_path,
):
    from runner.konsensus import rozstrzygnij

    warianty = {
        "a1": wariant(tmp_path, "a1", "  TAK  \n"),
        "a2": wariant(tmp_path, "a2", "tak\n"),
        "a3": wariant(tmp_path, "a3", "nie\n"),
    }

    assert rozstrzygnij(warianty, 2).zdany is True


def test_prog_podany_jako_bool_jest_odrzucony(tmp_path):
    from runner.konsensus import rozstrzygnij

    warianty = {"a1": wariant(tmp_path, "a1", "tak\n")}

    with pytest.raises(ValueError):
        rozstrzygnij(warianty, True)


@pytest.mark.parametrize("prog", ["2", 2.0])
def test_prog_niebedacy_liczba_calkowita_jest_odrzucony_bledem_wartosci(tmp_path, prog):
    from runner.konsensus import rozstrzygnij

    warianty = {"a1": wariant(tmp_path, "a1", "tak\n")}

    with pytest.raises(ValueError):          # nie TypeError: wolajacy tlumaczy ValueError
        rozstrzygnij(warianty, prog)


@pytest.mark.parametrize("prog", [0, -1])
def test_prog_mniejszy_niz_jeden_jest_odrzucony(tmp_path, prog):
    from runner.konsensus import rozstrzygnij

    warianty = {"a1": wariant(tmp_path, "a1", "tak\n")}

    with pytest.raises(ValueError):
        rozstrzygnij(warianty, prog)


@pytest.mark.parametrize("prog", [0, "2", True])
def test_odrzucenie_progu_podaje_w_komunikacie_otrzymana_wartosc(tmp_path, prog):
    import re

    from runner.konsensus import rozstrzygnij

    warianty = {"a1": wariant(tmp_path, "a1", "tak\n")}
    komunikat = f"prog konsensusu musi być liczbą całkowitą >= 1, jest {prog!r}"

    with pytest.raises(ValueError, match=f"^{re.escape(komunikat)}$"):
        rozstrzygnij(warianty, prog)


def test_gdy_nikt_nie_glosuje_nawet_przy_progu_jeden_krok_nie_przechodzi_a_opis_nazywa_kazdy_wariant(
    tmp_path,
):
    from runner.konsensus import WynikWariantu, rozstrzygnij

    warianty = {
        "a1": wariant(tmp_path, "a1", "tak\n", status="nieudany"),
        "a2": WynikWariantu(status=OKRESLONY, artefakty=()),
        "a3": wariant(tmp_path, "a3", "nie\n", status="nieudany"),
    }

    wynik = rozstrzygnij(warianty, 1)

    assert wynik.zdany is False
    assert wynik.zwyciezca is None
    assert wynik.opis == "konsensus 1 niespełniony: a1: nieudany; a2: brak werdyktu; a3: nieudany"


def test_opis_porazki_ma_prefiks_progu_i_warianty_rozdzielone_srednikiem(tmp_path):
    from runner.konsensus import rozstrzygnij

    warianty = {
        "a1": wariant(tmp_path, "a1", "tak\n"),
        "a2": wariant(tmp_path, "a2", "tak\n"),
        "a3": wariant(tmp_path, "a3", "nie\n"),
    }

    assert rozstrzygnij(warianty, 3).opis == "konsensus 3 niespełniony: a1: tak; a2: tak; a3: nie"


def test_opis_porazki_z_remisem_grup_konczy_sie_slowami_remis_grup(tmp_path):
    from runner.konsensus import rozstrzygnij

    warianty = {
        "a1": wariant(tmp_path, "a1", "tak\n"),
        "a2": wariant(tmp_path, "a2", "nie\n"),
    }

    assert rozstrzygnij(warianty, 3).opis == "konsensus 3 niespełniony: a1: tak; a2: nie; remis grup"


def test_opis_zdanego_rozstrzygniecia_to_same_wiersze_wariantow_z_remisem_na_koncu(tmp_path):
    from runner.konsensus import rozstrzygnij

    remis = {
        "a1": wariant(tmp_path, "a1", "tak\n"),
        "a2": wariant(tmp_path, "a2", "tak\n"),
        "a3": wariant(tmp_path, "a3", "nie\n"),
        "a4": wariant(tmp_path, "a4", "nie\n"),
    }
    bez_remisu = {
        "a1": wariant(tmp_path, "a1", "tak\n"),
        "a2": wariant(tmp_path, "a2", "tak\n"),
        "a3": wariant(tmp_path, "a3", "nie\n"),
    }

    assert rozstrzygnij(remis, 2).opis == "a1: tak; a2: tak; a3: nie; a4: nie; remis grup"
    assert rozstrzygnij(bez_remisu, 2).opis == "a1: tak; a2: tak; a3: nie"


# --- silnik: krok z `fanout: 3, consensus: 2` --------------------------------------------------

# Atrapa wariantu: artefakt ma w pierwszej linii werdykt zadany per agent_id, w trzeciej agent_id
# (po tresci widac, czyj plik trafil do wyniku kroku). Tresc `bajty` zapisuje surowe bajty.
KOD_WARIANTU = r"""
import json, sys
from pathlib import Path

p = json.loads(sys.argv[1])
agent = p["agent_id"]
for sciezka in p["zapisz"]:
    Path(sciezka).parent.mkdir(parents=True, exist_ok=True)
    if p["bajty"]:
        Path(sciezka).write_bytes(bytes(p["bajty"]))
    else:
        nazwa = Path(sciezka).name + "\n" if p["z_nazwa"] else ""
        Path(sciezka).write_text(p["werdykt"] + "\n\n" + agent + "\n" + nazwa)
tekst = "wariant " + agent
print(json.dumps({"type": "assistant", "message": {
    "content": [{"type": "text", "text": tekst}]}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": tekst}), flush=True)
sys.exit(p["kod"])
"""


@dataclass
class AtrapaZWerdyktami:
    werdykty: dict[str, str]                                  # per agent_id: pierwsza linia artefaktu
    kody: dict[str, int] = field(default_factory=dict)        # per agent_id
    bajty: dict[str, list[int]] = field(default_factory=dict)  # per agent_id: surowa tresc artefaktu
    wywolania: list = field(default_factory=list)             # (step_id, agent_id, wejscia)
    z_nazwa: bool = False                                     # tresc kazdego pliku konczy sie jego nazwa

    async def __call__(
        self, spec, polecenie, katalog, emit, *, katalog_runu=None, prompt=None, **dodatkowe
    ):
        self.wywolania.append((spec.step_id, spec.agent_id, list(spec.wejscia)))
        opis = {
            "agent_id": spec.agent_id,
            "werdykt": self.werdykty.get(spec.agent_id, ""),
            "kod": self.kody.get(spec.agent_id, 0),
            "bajty": self.bajty.get(spec.agent_id, []),
            "z_nazwa": self.z_nazwa,
            "zapisz": list(spec.wyjscia) if profil_roli(spec.rola).zapis else [],
        }
        argv = [sys.executable, "-I", "-c", KOD_WARIANTU, json.dumps(opis)]
        return await wykonaj_krok(
            spec, argv, katalog, emit, katalog_runu=katalog_runu, prompt=prompt, **dodatkowe)


def swiat_konsensusu(tmp_path, *, consensus: int = 2, rola: str = "author"):
    kroki = [K("analiza", extra=f", fanout: 3, consensus: {consensus}")]
    return swiat(tmp_path, kroki, opcje_skilli={"analiza": {"role": rola, "produces": ["plan.md"]}})


def artefakty(korzen: Path) -> Path:
    return korzen / ".cockpit" / "_run" / "artifacts"


def wynik_kroku(korzen: Path) -> Path:
    return artefakty(korzen) / "plan.md"


def podkatalogi(korzen: Path) -> list[str]:
    katalog = artefakty(korzen) / "analiza"
    return sorted(p.name for p in katalog.iterdir()) if katalog.is_dir() else []


def zdarzenia_kroku(zdarzenia: list[dict], rodzaj: str) -> list[dict]:
    return [z for z in zdarzenia if z["kind"] == rodzaj and z["step_id"] == "analiza"]


async def test_dwa_z_trzech_wariantow_zgodne_przy_consensus_2_zdaja_krok_i_wynik_jest_z_wiekszosci(
    tmp_path,
):
    s = swiat_konsensusu(tmp_path)
    agent = AtrapaZWerdyktami({"a1": "NIE", "a2": "TAK", "a3": "tak"})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert wynik_kroku(s.worktree).read_text() == "TAK\n\na2\n"     # a2, nie a1 (a1 w mniejszosci)
    assert zdarzenia_kroku(zdarzenia, "step_failed") == []
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_po_rozstrzygnieciu_zdanym_wszystkie_podkatalogi_wariantow_zostaja_z_trescia(tmp_path):
    s = swiat_konsensusu(tmp_path)
    agent = AtrapaZWerdyktami({"a1": "NIE", "a2": "TAK", "a3": "TAK"})

    await uruchom(s, agent, limit=LIMIT_S)

    assert podkatalogi(s.worktree) == ["agent_a1", "agent_a2", "agent_a3"]
    for agent_id, werdykt in (("a1", "NIE"), ("a2", "TAK"), ("a3", "TAK")):
        tresc = (artefakty(s.worktree) / "analiza" / f"agent_{agent_id}" / "plan.md").read_text()
        assert tresc == f"{werdykt}\n\n{agent_id}\n"


async def test_trzy_rozne_werdykty_przy_consensus_2_koncza_krok_porazka_z_opisem_wariantow(tmp_path):
    s = swiat_konsensusu(tmp_path)
    agent = AtrapaZWerdyktami({"a1": "tak", "a2": "nie", "a3": "moze"})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    nieudane = zdarzenia_kroku(zdarzenia, "step_failed")
    assert nieudane, rodzaje(zdarzenia)
    opisy = "\n".join(z["text"] for z in nieudane)
    for fragment in ("a1: tak", "a2: nie", "a3: moze"):
        assert fragment in opisy
    assert zdarzenia[-1]["kind"] == "run_finished"
    assert not wynik_kroku(s.worktree).exists()          # nie ma zwyciezcy, nie ma wyniku kroku


async def test_po_rozstrzygnieciu_nieudanym_wszystkie_podkatalogi_wariantow_zostaja(tmp_path):
    s = swiat_konsensusu(tmp_path)
    agent = AtrapaZWerdyktami({"a1": "tak", "a2": "nie", "a3": "moze"})

    await uruchom(s, agent, limit=LIMIT_S)

    assert podkatalogi(s.worktree) == ["agent_a1", "agent_a2", "agent_a3"]
    for agent_id, werdykt in (("a1", "tak"), ("a2", "nie"), ("a3", "moze")):
        tresc = (artefakty(s.worktree) / "analiza" / f"agent_{agent_id}" / "plan.md").read_text()
        assert tresc == f"{werdykt}\n\n{agent_id}\n"


async def test_nieudany_wariant_nie_glosuje_ale_krok_przechodzi_gdy_zgodnych_zdanych_jest_dwa(
    tmp_path,
):
    s = swiat_konsensusu(tmp_path)
    # a1 zgodny z pozostalymi, ale proces konczy sie bledem: nie liczy sie, wygrywa a2
    agent = AtrapaZWerdyktami(
        {"a1": "tak", "a2": "tak", "a3": "tak"}, kody={"a1": 1})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert wynik_kroku(s.worktree).read_text() == "tak\n\na2\n"
    assert {z["agent_id"] for z in zdarzenia_kroku(zdarzenia, "step_failed")} == {"a1"}
    assert {z["agent_id"] for z in zdarzenia_kroku(zdarzenia, "step_finished")} == {"a2", "a3"}
    assert zdarzenia[-1]["kind"] == "run_finished"
    assert podkatalogi(s.worktree) == ["agent_a1", "agent_a2", "agent_a3"]


async def test_nieudany_wariant_odbiera_wiekszosc_gdy_zgodnych_zdanych_jest_za_malo(tmp_path):
    s = swiat_konsensusu(tmp_path, consensus=3)
    # trzy pliki zgodne, ale a1 padl: glosuja dwa, a prog to trzy (przy progu 2 krok by przeszedl)
    agent = AtrapaZWerdyktami(
        {"a1": "tak", "a2": "tak", "a3": "tak"}, kody={"a1": 1})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert "a1" in {z["agent_id"] for z in zdarzenia_kroku(zdarzenia, "step_failed")}
    assert not wynik_kroku(s.worktree).exists()


async def test_wariant_z_nieczytelnym_artefaktem_nie_jest_zgodny_i_krok_przy_progu_trzy_pada(
    tmp_path,
):
    s = swiat_konsensusu(tmp_path, consensus=3)
    agent = AtrapaZWerdyktami(
        {"a1": "tak", "a2": "tak", "a3": "tak"}, bajty={"a2": [0xFF, 0xFE, 0xFF, 0x0A]})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert zdarzenia_kroku(zdarzenia, "step_failed") != []
    assert "a2: brak werdyktu" in "\n".join(z["text"] for z in zdarzenia_kroku(zdarzenia, "step_failed"))
    assert zdarzenia[-1]["kind"] == "run_finished"
    assert not wynik_kroku(s.worktree).exists()


async def test_wariant_z_nieczytelnym_artefaktem_nie_psuje_kroku_gdy_dwa_pozostale_sa_zgodne(
    tmp_path,
):
    s = swiat_konsensusu(tmp_path)
    agent = AtrapaZWerdyktami(
        {"a1": "tak", "a2": "tak", "a3": "tak"}, bajty={"a1": [0xFF, 0xFE, 0xFF, 0x0A]})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert wynik_kroku(s.worktree).read_text() == "tak\n\na2\n"
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_wariant_z_pustym_artefaktem_nie_jest_zgodny_i_krok_przy_progu_trzy_pada(tmp_path):
    s = swiat_konsensusu(tmp_path, consensus=3)
    agent = AtrapaZWerdyktami({"a1": "tak", "a2": "", "a3": "tak"})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert zdarzenia_kroku(zdarzenia, "step_failed") != []
    assert not wynik_kroku(s.worktree).exists()


async def test_rola_bez_zapisu_z_konsensusem_tez_rozstrzyga_wedlug_artefaktow_w_katalogu_runu(
    tmp_path,
):
    s = swiat_konsensusu(tmp_path, rola="reviewer")
    # rola bez zapisu: runner zapisuje stdout agenta jako artefakt, wiec werdykt to "wariant <id>"
    # i trzy warianty sa rozne; krok pada, a podkatalogi w katalogu runu zostaja
    agent = AtrapaZWerdyktami({"a1": "tak", "a2": "tak", "a3": "tak"})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert zdarzenia_kroku(zdarzenia, "step_failed") != []
    for agent_id in ("a1", "a2", "a3"):
        assert (s.katalog_runu / "analiza" / f"agent_{agent_id}" / "plan.md").is_file()


async def test_rola_bez_zapisu_z_konsensusem_kopiuje_artefakt_zwyciezcy_do_artefaktow_runu_w_worktree(
    tmp_path,
):
    s = swiat_konsensusu(tmp_path, consensus=1, rola="reviewer")
    # rola bez zapisu: artefakty wariantow leza w katalogu runu poza repozytorium; przy progu 1
    # kazdy wariant ma grupe po jednym glosie, wiec remis rozstrzyga kolejnosc agentow (a1)
    agent = AtrapaZWerdyktami({})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    zrodlo = s.katalog_runu / "analiza" / "agent_a1" / "plan.md"
    assert zdarzenia_kroku(zdarzenia, "step_failed") == []
    assert "wariant a1" in zrodlo.read_text()
    assert wynik_kroku(s.worktree).read_text() == zrodlo.read_text()


async def test_blad_kopii_wyniku_zwyciezcy_konczy_krok_porazka_i_run_zdarzeniem_koncowym(
    tmp_path, monkeypatch,
):
    s = swiat_konsensusu(tmp_path)
    agent = AtrapaZWerdyktami({"a1": "NIE", "a2": "TAK", "a3": "TAK"})
    prawdziwa = shutil.copyfile

    def copyfile(zrodlo, cel, *argumenty, **opcje):
        if Path(cel) == wynik_kroku(s.worktree):        # tylko kopia wyniku kroku, nie materializacja
            raise OSError("pelny dysk")
        return prawdziwa(zrodlo, cel, *argumenty, **opcje)

    monkeypatch.setattr(shutil, "copyfile", copyfile)

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    opisy = "\n".join(z["text"] for z in zdarzenia_kroku(zdarzenia, "step_failed"))
    assert "nie można skopiować wyniku wariantu a2" in opisy
    assert "pelny dysk" in opisy
    assert zdarzenia[-1]["kind"] == "run_finished"
    assert not wynik_kroku(s.worktree).exists()


async def test_krok_zalezny_od_producenta_z_konsensusem_dostaje_jedna_sciezke_kopii_zwyciezcy(
    tmp_path,
):
    s = swiat(
        tmp_path,
        [K("analiza", extra=", fanout: 3, consensus: 2"), K("synteza", ("analiza",))],
        opcje_skilli={
            "analiza": {"role": "author", "produces": ["plan.md"]},
            "synteza": {"consumes": ["plan.md"]},
        })
    agent = AtrapaZWerdyktami({"a1": "NIE", "a2": "TAK", "a3": "TAK"})

    await uruchom(s, agent, limit=LIMIT_S)

    (wejscia,) = [w for krok, _, w in agent.wywolania if krok == "synteza"]
    assert wejscia == [".cockpit/_run/artifacts/plan.md"]      # nie trzy sciezki wariantow


def teksty_porazek(zdarzenia: list[dict]) -> dict[str, str]:
    return {z["agent_id"]: z["text"] for z in zdarzenia_kroku(zdarzenia, "step_failed")}


async def test_niespelniony_konsensus_daje_pierwszemu_wariantowi_opis_a_pozostalym_powod_konsensusu(
    tmp_path,
):
    s = swiat_konsensusu(tmp_path)
    agent = AtrapaZWerdyktami({"a1": "tak", "a2": "nie", "a3": "moze"})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert teksty_porazek(zdarzenia) == {
        "a1": "konsensus 2 niespełniony: a1: tak; a2: nie; a3: moze; remis grup",
        "a2": "wariant a2: konsensus niespełniony",
        "a3": "wariant a3: konsensus niespełniony",
    }


async def test_wariant_nieudany_z_wlasnej_przyczyny_zachowuje_ja_po_porazce_konsensusu(tmp_path):
    s = swiat_konsensusu(tmp_path, consensus=3)
    # a2 pada z kodem 1 (nie glosuje); a1 i a3 sa zgodne, ale prog 3 nie jest spelniony
    agent = AtrapaZWerdyktami({"a1": "tak", "a2": "tak", "a3": "tak"}, kody={"a2": 1})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert teksty_porazek(zdarzenia) == {
        "a1": "konsensus 3 niespełniony: a1: tak; a2: nieudany; a3: tak",
        "a2": "wariant a2: kod wyjścia 1",
        "a3": "wariant a3: konsensus niespełniony",
    }


async def test_blad_kopii_wyniku_zwyciezcy_ma_ten_sam_pelny_komunikat_w_zdarzeniu_kazdego_wariantu(
    tmp_path, monkeypatch,
):
    s = swiat_konsensusu(tmp_path)
    agent = AtrapaZWerdyktami({"a1": "NIE", "a2": "TAK", "a3": "TAK"})
    prawdziwa = shutil.copyfile

    def copyfile(zrodlo, cel, *argumenty, **opcje):
        if Path(cel) == wynik_kroku(s.worktree):
            raise OSError("pelny dysk")
        return prawdziwa(zrodlo, cel, *argumenty, **opcje)

    monkeypatch.setattr(shutil, "copyfile", copyfile)

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    komunikat = "nie można skopiować wyniku wariantu a2: pelny dysk"
    assert teksty_porazek(zdarzenia) == {"a1": komunikat, "a2": komunikat, "a3": komunikat}


async def test_konsument_producenta_z_jednym_wariantem_i_consensus_1_dostaje_zwykla_nazwe_artefaktu(
    tmp_path,
):
    s = swiat(
        tmp_path,
        [K("analiza", extra=", fanout: 1, consensus: 1"), K("synteza", ("analiza",))],
        opcje_skilli={
            "analiza": {"role": "author", "produces": ["plan.md"]},
            "synteza": {"consumes": ["plan.md"]},
        })
    agent = AtrapaZWerdyktami({"analiza": "TAK"})

    await uruchom(s, agent, limit=LIMIT_S)

    (wejscia,) = [w for krok, _, w in agent.wywolania if krok == "synteza"]
    assert wejscia == ["plan.md"]      # jeden wariant: bez kopii zwyciezcy w artifacts/


async def test_przy_consensus_1_z_trzech_wariantow_wynik_kroku_jest_z_pierwszego_z_wiekszej_grupy(
    tmp_path,
):
    s = swiat_konsensusu(tmp_path, consensus=1)
    agent = AtrapaZWerdyktami({"a1": "NIE", "a2": "TAK", "a3": "TAK"})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert zdarzenia_kroku(zdarzenia, "step_failed") == []
    assert wynik_kroku(s.worktree).read_text() == "TAK\n\na2\n"     # a2, nie pierwszy glosujacy a1
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_wynik_kroku_z_dwoma_artefaktami_kopiuje_oba_pliki_zwyciezcy_kazdy_z_wlasna_trescia(
    tmp_path,
):
    s = swiat(
        tmp_path,
        [K("analiza", extra=", fanout: 3, consensus: 2")],
        opcje_skilli={"analiza": {"role": "author", "produces": ["plan.md", "uwagi.md"]}})
    agent = AtrapaZWerdyktami({"a1": "NIE", "a2": "TAK", "a3": "TAK"}, z_nazwa=True)

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert zdarzenia_kroku(zdarzenia, "step_failed") == []
    assert (artefakty(s.worktree) / "plan.md").read_text() == "TAK\n\na2\nplan.md\n"
    assert (artefakty(s.worktree) / "uwagi.md").read_text() == "TAK\n\na2\nuwagi.md\n"


def swiat_jednego_wariantu(tmp_path, rola: str):
    kroki = [K("analiza", extra=", fanout: 1, consensus: 1")]
    return swiat(tmp_path, kroki, opcje_skilli={"analiza": {"role": rola, "produces": ["plan.md"]}})


async def test_jeden_wariant_roli_z_zapisem_z_werdyktem_zdaje_krok_bez_kopii_i_bez_podkatalogu(
    tmp_path,
):
    s = swiat_jednego_wariantu(tmp_path, "author")
    agent = AtrapaZWerdyktami({"analiza": "TAK"})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert zdarzenia_kroku(zdarzenia, "step_failed") == []
    assert [z["agent_id"] for z in zdarzenia_kroku(zdarzenia, "step_finished")] == ["analiza"]
    assert (s.worktree / "plan.md").read_text() == "TAK\n\nanaliza\n"
    assert not wynik_kroku(s.worktree).exists()
    assert podkatalogi(s.worktree) == []
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_jeden_wariant_roli_bez_zapisu_z_werdyktem_zdaje_krok_wedlug_artefaktu_z_katalogu_runu(
    tmp_path,
):
    s = swiat_jednego_wariantu(tmp_path, "reviewer")
    agent = AtrapaZWerdyktami({})     # rola bez zapisu: artefakt to stdout agenta ("wariant analiza")

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert zdarzenia_kroku(zdarzenia, "step_failed") == []
    assert [z["agent_id"] for z in zdarzenia_kroku(zdarzenia, "step_finished")] == ["analiza"]
    assert "wariant analiza" in (s.katalog_runu / "analiza" / "plan.md").read_text()
    assert not wynik_kroku(s.worktree).exists()
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_jeden_wariant_roli_z_zapisem_z_artefaktem_spoza_utf8_konczy_krok_porazka_brak_werdyktu(
    tmp_path,
):
    s = swiat_jednego_wariantu(tmp_path, "author")
    # dowod z pliku czyta z zamiana znakow (niepusty), werdykt czyta scisle: wariant nie glosuje
    agent = AtrapaZWerdyktami({}, bajty={"analiza": [0xFF, 0xFE, 0x0A]})

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert [z["text"] for z in zdarzenia_kroku(zdarzenia, "step_failed")] == [
        "konsensus 1 niespełniony: analiza: brak werdyktu"]
    assert zdarzenia_kroku(zdarzenia, "step_finished") == []
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_jeden_wariant_roli_z_zapisem_z_pustym_artefaktem_konczy_krok_porazka_brak_dowodu(
    tmp_path,
):
    s = swiat_jednego_wariantu(tmp_path, "author")
    agent = AtrapaZWerdyktami({}, bajty={"analiza": [0x0A]})     # plik z samym znakiem nowej linii

    zdarzenia = await uruchom(s, agent, limit=LIMIT_S)

    assert [z["text"] for z in zdarzenia_kroku(zdarzenia, "step_failed")] == [
        f"niezmiennik evidence_required: plik dowodu jest pusty: {s.worktree / 'plan.md'}"]
    assert zdarzenia_kroku(zdarzenia, "step_finished") == []
    assert zdarzenia[-1]["kind"] == "run_finished"
