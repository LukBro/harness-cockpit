"""Pomocnik testu HAR-172 (kryterium 3): silnik runu w osobnym procesie, ktory test zabija.

Uruchamiany przez `tests/test_bramka_zatwierdzenia.py` jako
`python tests/bramka_silnik_proces.py '<json>'` z cwd = korzen repozytorium. Json niesie:
`polecenie` (run_workflow), `dom`, `root`, `zakres` (lista katalogow), `zdarzenia` (plik, do
ktorego proces dopisuje zdarzenia silnika, jedno na linie, z flush i fsync) oraz `agent` (plik,
do ktorego fałszywy agent dopisuje id kazdego uruchomionego kroku) i opcjonalnie `spanie`
(sekundy, ktore fałszywy agent czeka po zapisaniu id kroku; domyslnie 0).

Zmienna COCKPIT_TEST_PAUSE_AFTER_CHECKPOINT dziedziczy z otoczenia procesu. Fałszywy agent nie
startuje zadnego procesu: konczy krok sukcesem od razu (albo po `spanie`).
Silnik ogłasza `step_waiting` dopiero po krokach niezaleznych, wiec okno pauzy
(COCKPIT_TEST_PAUSE_AFTER_CHECKPOINT) jest PO ostatnim zdarzeniu kroku, a PRZED `step_waiting`.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

KORZEN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KORZEN / "src"))

from cockpit.services.invariants import Tier  # noqa: E402
from runner import silnik  # noqa: E402
from runner.agent import ProfilBackendu  # noqa: E402
from runner.krok import WynikKroku  # noqa: E402
from runner.zakres import Zakres  # noqa: E402


def _dopisz(plik: Path, linia: str) -> None:
    with plik.open("a", encoding="utf-8") as f:
        f.write(linia + "\n")
        f.flush()
        os.fsync(f.fileno())


def main() -> None:
    opis = json.loads(sys.argv[1])
    plik_zdarzen = Path(opis["zdarzenia"])
    plik_agenta = Path(opis["agent"])

    async def emit(zdarzenie: dict) -> None:
        _dopisz(plik_zdarzen, json.dumps(zdarzenie))

    async def agent(spec, polecenie, katalog, emit_, *, katalog_runu=None, prompt=None) -> WynikKroku:
        _dopisz(plik_agenta, spec.step_id)
        await asyncio.sleep(opis.get("spanie", 0))
        return WynikKroku(status="zakonczony", powod=None, kod_wyjscia=0)

    profil = ProfilBackendu(modele={
        Tier.judgment: "m-judgment", Tier.execution: "m-execution", Tier.retrieval: "m-retrieval"})
    asyncio.run(silnik.wykonaj_run(
        opis["polecenie"], emit, agent,
        profil=profil, dom=Path(opis["dom"]), root=Path(opis["root"]),
        zakres=Zakres([Path(p) for p in opis["zakres"]]),
    ))


if __name__ == "__main__":
    main()
