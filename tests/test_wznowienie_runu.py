"""HAR-173 (czesc 1): wznowienie runu po decyzji czlowieka z checkpointu na dysku (TDD).

Bramka stoi PRZED krokiem z `approval: true` (rozstrzygniecie D2). Po `approve` krok za
bramka startuje od poczatku, a decyzja (z powodem) jest jego artefaktem wejsciowym; `reject`
konczy run bez startu kroku. Wznowienie czyta WYLACZNIE checkpoint z dysku: kazdy test
wznawia swiezym agentem po zakonczeniu pierwszego przebiegu.

Prawdziwy kod: `runner.silnik.wykonaj_run` i `runner.silnik.wznow_run` na prawdziwym repo git
z `tests/test_silnik_runu.py` (swiat, K, Szpieg, uruchom). Zaslepiony jest tylko agent.
Nazwy testow w jezyku dziedziny, nie technologii (CLAUDE.md).
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from runner.checkpoint import (
    Checkpoint,
    StanKrokuCheckpointu,
    wczytaj_checkpoint,
    zapisz_checkpoint,
)
from test_silnik_runu import RUN_ID, K, Szpieg, indeks, jedno, rodzaje, swiat, uruchom

BRAMKA = ", approval: true"
LIMIT_S = 30
SCIEZKA_DECYZJI_B = ".cockpit/_run/artifacts/decyzja_b.json"
TEKST_PRZERWANIA = "przerwany przy śmierci runnera — nie wznawiamy"
TEKST_BRAK_OCZEKIWANIA = "run nie czeka na decyzję (polecenie powtórzone albo run już zakończony)"


def ignorowane(krok: str, tekst: str) -> dict:
    """Dokladne zdarzenie `resume_ignored`: wszystkie pola, pelny tekst."""
    return {"kind": "resume_ignored", "run_id": RUN_ID, "step_id": krok, "agent_id": krok,
            "text": tekst}


async def wznow(
    s, agent, *, krok="b", decyzja="approve", powod=None, emit=None, run_id=RUN_ID,
) -> list[dict]:
    """Prawdziwe `silnik.wznow_run` na swiezym agencie; zwraca zdarzenia wznowienia."""
    from runner import silnik   # import leniwy: brak funkcji czerwieni kazdy test osobno

    zdarzenia: list[dict] = []

    async def zapisz(zdarzenie: dict) -> None:
        zdarzenia.append(zdarzenie)

    await asyncio.wait_for(
        silnik.wznow_run(
            run_id, krok, decyzja, emit or zapisz, agent,
            powod=powod, profil=s.profil, dom=s.dom, root=s.root),
        LIMIT_S)
    return zdarzenia


async def run_zatrzymany_na_bramce(tmp_path, kroki=None, **opcje):
    """Pierwszy przebieg: a -> b(approval) -> c; zwraca (swiat, zdarzenia pierwszego przebiegu)."""
    kroki = kroki or [K("a"), K("b", ("a",), extra=BRAMKA), K("c", ("b",))]
    s = swiat(tmp_path, kroki, **opcje)
    zdarzenia = await uruchom(s, Szpieg())
    assert zdarzenia[-1]["kind"] == "step_waiting"
    return s, zdarzenia


def artefakt_decyzji(s, krok: str = "b") -> Path:
    return s.worktree / ".cockpit" / "_run" / "artifacts" / f"decyzja_{krok}.json"


class SzpiegCzytajacyStan(Szpieg):
    """Agent-szpieg: w chwili wywolania kroku czyta checkpoint i sprawdza artefakt decyzji."""

    def __init__(self, s, **opcje):
        super().__init__(**opcje)
        self.s = s
        self.waiting_on: dict[str, object] = {}
        self.artefakt_istnieje: dict[str, bool] = {}

    async def __call__(self, spec, polecenie, katalog, emit, *, katalog_runu=None):
        cp = wczytaj_checkpoint(self.s.katalog_runu)
        self.waiting_on[spec.step_id] = cp.waiting_on
        self.artefakt_istnieje[spec.step_id] = artefakt_decyzji(self.s, spec.step_id).is_file()
        return await super().__call__(spec, polecenie, katalog, emit, katalog_runu=katalog_runu)


# =====================================================================================
# 1. Zatwierdzenie: od kroku za bramka, bez powtarzania zakonczonych
# =====================================================================================


async def test_po_zatwierdzeniu_uruchamiaja_sie_krok_za_bramka_i_jego_nastepnik_ale_nie_zakonczony_poprzednik(
    tmp_path,
):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    nowy = Szpieg()

    zdarzenia = await wznow(s, nowy)

    assert nowy.kroki == ["b", "c"]                 # `a` byl zakonczony w pierwszym przebiegu
    assert "a" not in [z["step_id"] for z in zdarzenia if z["kind"] == "step_started"]
    jedno(zdarzenia, "step_started", "b")
    jedno(zdarzenia, "step_finished", "b")
    jedno(zdarzenia, "step_finished", "c")


async def test_wznowiony_run_konczy_sie_dokladnie_jednym_run_finished(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)

    zdarzenia = await wznow(s, Szpieg())

    assert zdarzenia[-1]["kind"] == "run_finished"
    assert rodzaje(zdarzenia).count("run_finished") == 1
    assert zdarzenia[-1]["run_id"] == RUN_ID
    assert zdarzenia[-1]["cost_usd"] is None
    assert zdarzenia[-1]["cost_source"] == "unavailable"


async def test_krok_za_bramka_dostaje_start_pod_swoim_step_id_i_agent_id(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)

    zdarzenia = await wznow(s, Szpieg())

    start = jedno(zdarzenia, "step_started", "b")
    assert start["run_id"] == RUN_ID
    assert start["agent_id"] == "b"
    assert indeks(zdarzenia, "step_started", "b") < indeks(zdarzenia, "step_finished", "b")
    assert indeks(zdarzenia, "step_finished", "b") < indeks(zdarzenia, "step_started", "c")


async def test_zatwierdzony_krok_nie_zatrzymuje_sie_drugi_raz_na_swojej_bramce(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)

    zdarzenia = await wznow(s, Szpieg())

    assert "step_waiting" not in rodzaje(zdarzenia)
    assert wczytaj_checkpoint(s.katalog_runu).waiting_on is None


# =====================================================================================
# 2. Wznowienie wylacznie z checkpointu na dysku
# =====================================================================================


async def test_wznowienie_czyta_workflow_z_kopii_w_katalogu_runu_a_nie_z_repozytorium(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    inny = "name: w\nsteps:\n  z: {skill: s_a}\n"
    for plik in (s.repo / ".cockpit" / "workflows" / "w.yaml",
                 s.worktree / ".cockpit" / "workflows" / "w.yaml"):
        plik.write_text(inny, encoding="utf-8")        # tylko kopia w katalogu runu jest wierna
    nowy = Szpieg()

    await wznow(s, nowy)

    assert nowy.kroki == ["b", "c"]


async def test_wznowiony_krok_dziala_w_worktree_i_katalogu_runu_zapisanych_w_checkpoincie(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    nowy = Szpieg()

    await wznow(s, nowy)

    for wywolanie in nowy.wywolania:
        assert Path(wywolanie.katalog).resolve() == s.worktree.resolve()
        assert Path(wywolanie.katalog_runu).resolve() == s.katalog_runu.resolve()


@pytest.mark.parametrize("jak", ["usuniety", "uszkodzony"])
async def test_brak_albo_uszkodzenie_checkpointu_to_porazka_kroku_i_koniec_runu_bez_startu_agenta(
    tmp_path, jak,
):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    plik = s.katalog_runu / "checkpoint.json"
    if jak == "usuniety":
        plik.unlink()
    else:
        plik.write_text("{nie json", encoding="utf-8")
    nowy = Szpieg()

    zdarzenia = await wznow(s, nowy)        # wyjatek z wznow_run oblalby test tutaj

    assert nowy.wywolania == []
    assert "step_started" not in rodzaje(zdarzenia)
    porazka = jedno(zdarzenia, "step_failed", "b")
    assert porazka["run_id"] == RUN_ID
    assert porazka["agent_id"] == "b"
    prefiks = (f"odczyt checkpointu {plik}: " if jak == "usuniety"
               else f"checkpoint {plik} nie jest poprawnym JSON: ")
    assert porazka["text"].startswith(prefiks)
    assert len(porazka["text"]) > len(prefiks)      # za prefiksem jest powod z dysku albo parsera
    assert zdarzenia[-1]["kind"] == "run_finished"
    assert zdarzenia[-1]["step_id"] == "b"
    assert zdarzenia[-1]["text"] == porazka["text"]
    assert rodzaje(zdarzenia).count("run_finished") == 1


async def test_brak_worktree_runu_to_porazka_kroku_i_koniec_runu_bez_startu_agenta(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    cp = wczytaj_checkpoint(s.katalog_runu)
    zapisz_checkpoint(s.katalog_runu, Checkpoint(
        run_id=cp.run_id, repozytorium=cp.repozytorium, workflow=cp.workflow,
        base_commit=cp.base_commit, worktree=str(tmp_path / "nie_ma_takiego_worktree"),
        kroki=cp.kroki, waiting_on=cp.waiting_on))
    nowy = Szpieg()

    zdarzenia = await wznow(s, nowy)

    assert nowy.wywolania == []
    porazka = jedno(zdarzenia, "step_failed", "b")
    assert porazka["text"] == f"brak worktree runu: {tmp_path / 'nie_ma_takiego_worktree'}"
    assert porazka["agent_id"] == "b"
    assert zdarzenia[-1]["kind"] == "run_finished"
    assert zdarzenia[-1]["text"] == porazka["text"]


async def test_brak_kopii_workflow_w_katalogu_runu_to_porazka_kroku_i_koniec_runu(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    (s.katalog_runu / "workflow.yaml").unlink()
    nowy = Szpieg()

    zdarzenia = await wznow(s, nowy)

    assert nowy.wywolania == []
    porazka = jedno(zdarzenia, "step_failed", "b")
    assert porazka["text"].startswith("-: ")                       # problem calego pliku, bez kroku
    assert "nie mozna odczytac pliku" in porazka["text"]
    assert zdarzenia[-1]["kind"] == "run_finished"
    assert zdarzenia[-1]["text"] == porazka["text"]


async def test_nieudany_zapis_artefaktu_decyzji_to_porazka_kroku_i_koniec_runu_bez_startu_agenta(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    katalog_run = s.worktree / ".cockpit" / "_run"
    katalog_run.mkdir(parents=True, exist_ok=True)
    (katalog_run / "artifacts").write_text("plik zamiast katalogu", encoding="utf-8")
    nowy = Szpieg()

    zdarzenia = await wznow(s, nowy)

    assert nowy.wywolania == []
    assert "step_started" not in rodzaje(zdarzenia)
    porazka = jedno(zdarzenia, "step_failed", "b")
    assert porazka["text"].startswith("zapis artefaktu decyzji dla kroku b: ")
    assert len(porazka["text"]) > len("zapis artefaktu decyzji dla kroku b: ")
    assert zdarzenia[-1]["kind"] == "run_finished"
    assert zdarzenia[-1]["text"] == porazka["text"]
    # artefakt nie powstal, wiec checkpoint nie moze byc juz zapisany bez oczekiwania
    assert wczytaj_checkpoint(s.katalog_runu).waiting_on.step_id == "b"


# =====================================================================================
# 3. Kolejnosc: artefakt decyzji, checkpoint bez oczekiwania, dopiero potem start kroku
# =====================================================================================


async def test_w_chwili_wywolania_agenta_kroku_za_bramka_checkpoint_nie_czeka_juz_na_decyzje(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    cp_przed = wczytaj_checkpoint(s.katalog_runu)
    assert cp_przed.waiting_on is not None and cp_przed.waiting_on.step_id == "b"
    agent = SzpiegCzytajacyStan(s)

    await wznow(s, agent)

    assert agent.waiting_on["b"] is None
    assert agent.waiting_on["c"] is None


async def test_w_chwili_wywolania_agenta_kroku_za_bramka_artefakt_decyzji_juz_istnieje(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    assert not artefakt_decyzji(s).exists()          # przed decyzja nie ma artefaktu
    agent = SzpiegCzytajacyStan(s)

    await wznow(s, agent)

    assert agent.artefakt_istnieje["b"] is True


async def test_w_chwili_ogloszenia_startu_kroku_checkpoint_i_artefakt_decyzji_sa_juz_na_dysku(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    widziane: list[tuple[object, bool]] = []

    async def emit(zdarzenie: dict) -> None:
        if zdarzenie["kind"] == "step_started" and zdarzenie["step_id"] == "b":
            widziane.append((wczytaj_checkpoint(s.katalog_runu).waiting_on,
                             artefakt_decyzji(s).is_file()))

    await wznow(s, Szpieg(), emit=emit)

    assert widziane == [(None, True)]


# =====================================================================================
# 4. Idempotencja i wyscig: powtorzone polecenie niczego nie uruchamia drugi raz
# =====================================================================================


async def test_powtorzone_polecenie_wznowienia_jest_ignorowane_jednym_zdarzeniem_i_nic_nie_startuje(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    pierwszy, drugi = Szpieg(), Szpieg()

    await wznow(s, pierwszy)
    zdarzenia = await wznow(s, drugi)

    assert pierwszy.kroki == ["b", "c"]
    assert drugi.wywolania == []                       # b nie ruszyl drugi raz
    assert rodzaje(zdarzenia) == ["resume_ignored"]
    assert zdarzenia == [ignorowane("b", TEKST_BRAK_OCZEKIWANIA)]


async def test_dwa_wspolbiezne_polecenia_wznowienia_uruchamiaja_krok_za_bramka_dokladnie_raz(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    agent = Szpieg(opoznienie=0.2)
    pierwsze: list[dict] = []
    drugie: list[dict] = []

    async def emit_1(z: dict) -> None:
        pierwsze.append(z)

    async def emit_2(z: dict) -> None:
        drugie.append(z)

    from runner import silnik
    await asyncio.wait_for(asyncio.gather(
        silnik.wznow_run(RUN_ID, "b", "approve", emit_1, agent,
                         profil=s.profil, dom=s.dom, root=s.root),
        silnik.wznow_run(RUN_ID, "b", "approve", emit_2, agent,
                         profil=s.profil, dom=s.dom, root=s.root),
    ), LIMIT_S)

    assert agent.kroki.count("b") == 1
    assert agent.kroki.count("c") == 1
    wszystkie = pierwsze + drugie
    assert rodzaje(wszystkie).count("step_started") == 2        # b i c, po jednym
    assert rodzaje(wszystkie).count("resume_ignored") == 1
    assert rodzaje(wszystkie).count("run_finished") == 1


async def test_zatwierdzenie_po_odrzuceniu_jest_ignorowane_bo_run_juz_sie_skonczyl(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    await wznow(s, Szpieg(), decyzja="reject", powod="nie teraz")
    nowy = Szpieg()

    zdarzenia = await wznow(s, nowy)

    assert nowy.wywolania == []
    assert zdarzenia == [ignorowane("b", TEKST_BRAK_OCZEKIWANIA)]


# =====================================================================================
# 5. Polecenie nie pasujace do oczekiwania: ignorowane, checkpoint bez zmian
# =====================================================================================


async def test_polecenie_dla_innego_kroku_niz_czekajacy_jest_ignorowane_a_checkpoint_zostaje(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    przed = (s.katalog_runu / "checkpoint.json").read_bytes()
    nowy = Szpieg()

    zdarzenia = await wznow(s, nowy, krok="c")

    assert nowy.wywolania == []
    # step_id i agent_id z polecenia ("c"), nie z checkpointu ("b")
    assert zdarzenia == [ignorowane("c", "run czeka na decyzję dla kroku b, nie c")]
    assert (s.katalog_runu / "checkpoint.json").read_bytes() == przed
    assert wczytaj_checkpoint(s.katalog_runu).waiting_on.step_id == "b"
    assert not artefakt_decyzji(s, "c").exists()


async def test_nieznana_decyzja_jest_ignorowana_a_run_dalej_czeka_na_bramce(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    przed = (s.katalog_runu / "checkpoint.json").read_bytes()
    nowy = Szpieg()

    zdarzenia = await wznow(s, nowy, decyzja="maybe")

    assert nowy.wywolania == []
    assert zdarzenia == [ignorowane("b", "nieznana decyzja: 'maybe'")]
    assert (s.katalog_runu / "checkpoint.json").read_bytes() == przed
    assert wczytaj_checkpoint(s.katalog_runu).waiting_on.step_id == "b"
    assert not artefakt_decyzji(s).exists()


# =====================================================================================
# 6. Odrzucenie
# =====================================================================================


async def test_odrzucenie_konczy_run_bez_startu_kroku_i_bez_wywolania_agenta(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    nowy = Szpieg()

    zdarzenia = await wznow(s, nowy, decyzja="reject", powod="za ryzykowne")

    assert nowy.wywolania == []
    assert "step_started" not in rodzaje(zdarzenia)
    assert zdarzenia[-1]["kind"] == "run_finished"
    assert rodzaje(zdarzenia).count("run_finished") == 1
    assert zdarzenia == [{
        "kind": "run_finished", "run_id": RUN_ID, "step_id": "b", "agent_id": "",
        "text": "za ryzykowne", "cost_usd": None, "cost_source": "unavailable"}]


async def test_odrzucenie_bez_powodu_konczy_run_z_pustym_tekstem_a_nie_napisem_none(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)

    zdarzenia = await wznow(s, Szpieg(), decyzja="reject", powod=None)

    assert [z["text"] for z in zdarzenia] == [""]
    assert rodzaje(zdarzenia) == ["run_finished"]


async def test_odrzucenie_zapisuje_checkpoint_bez_oczekiwania_i_nie_dotyka_stanow_krokow(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    przed = wczytaj_checkpoint(s.katalog_runu)

    await wznow(s, Szpieg(), decyzja="reject", powod="za ryzykowne")

    po = wczytaj_checkpoint(s.katalog_runu)
    assert po.waiting_on is None
    assert po.kroki == przed.kroki                     # `a` zakonczony, `b` i `c` oczekuja
    assert po.kroki["a"] == StanKrokuCheckpointu("zakonczony", 1)


# =====================================================================================
# 7. Artefakt decyzji jako wejscie kroku
# =====================================================================================


async def test_artefakt_decyzji_niesie_run_krok_decyzje_powod_i_czas_w_utc(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)

    await wznow(s, Szpieg(), powod="wszystko sprawdzone")

    tresc = json.loads(artefakt_decyzji(s).read_text(encoding="utf-8"))
    assert set(tresc) == {"run_id", "step_id", "decyzja", "powod", "czas"}
    assert tresc["run_id"] == RUN_ID
    assert tresc["step_id"] == "b"
    assert tresc["decyzja"] == "approve"
    assert tresc["powod"] == "wszystko sprawdzone"
    czas = datetime.fromisoformat(tresc["czas"])
    assert czas.utcoffset() == timedelta(0)


async def test_zatwierdzenie_bez_powodu_zapisuje_w_artefakcie_powod_null(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)

    await wznow(s, Szpieg(), powod=None)

    tresc = json.loads(artefakt_decyzji(s).read_text(encoding="utf-8"))
    assert "powod" in tresc and tresc["powod"] is None


async def test_artefakt_decyzji_jest_ostatnim_wejsciem_kroku_za_bramka_po_wejsciach_ze_skilla(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(
        tmp_path,
        kroki=[K("a"), K("b", ("a",), extra=BRAMKA), K("c", ("b",))],
        opcje_skilli={"a": {"produces": ["wynik.md"]}, "b": {"consumes": ["wynik.md"]}})
    nowy = Szpieg()

    await wznow(s, nowy)

    assert nowy.spec("b").wejscia == ["wynik.md", SCIEZKA_DECYZJI_B]
    assert nowy.spec("b").wejscia[-1] == SCIEZKA_DECYZJI_B


async def test_krok_bez_bramki_nie_dostaje_artefaktu_decyzji_wsrod_wejsc(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    nowy = Szpieg()

    await wznow(s, nowy)

    assert SCIEZKA_DECYZJI_B not in nowy.spec("c").wejscia
    assert not any("decyzja_" in w for w in nowy.spec("c").wejscia)
    assert not artefakt_decyzji(s, "c").exists()


async def test_sciezka_artefaktu_w_wejsciach_wskazuje_plik_ktory_istnieje_w_worktree(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    nowy = Szpieg()

    await wznow(s, nowy, powod="ok")

    sciezka = nowy.spec("b").wejscia[-1]
    assert (s.worktree / sciezka).is_file()
    assert (s.worktree / sciezka).resolve() == artefakt_decyzji(s).resolve()


# =====================================================================================
# 8. Druga bramka
# =====================================================================================


async def test_po_wznowieniu_run_z_druga_bramka_znowu_czeka_i_nie_uruchamia_kroku_za_nia(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(
        tmp_path, kroki=[K("a"), K("b", ("a",), extra=BRAMKA), K("c", ("b",), extra=BRAMKA)])
    nowy = Szpieg()

    zdarzenia = await wznow(s, nowy)

    assert nowy.kroki == ["b"]                          # c stoi za druga bramka
    assert zdarzenia[-1]["kind"] == "step_waiting"
    assert zdarzenia[-1]["step_id"] == "c"
    assert "run_finished" not in rodzaje(zdarzenia)
    assert "c" not in [z["step_id"] for z in zdarzenia if z["kind"] == "step_started"]
    cp = wczytaj_checkpoint(s.katalog_runu)
    assert cp.waiting_on is not None and cp.waiting_on.step_id == "c"
    assert cp.kroki["a"] == StanKrokuCheckpointu("zakonczony", 1)       # liczba startow przezyla wznowienie
    assert cp.kroki["b"] == StanKrokuCheckpointu("zakonczony", 1)
    assert cp.kroki["c"] == StanKrokuCheckpointu("oczekuje", 0)


async def test_zatwierdzenie_drugiej_bramki_uruchamia_krok_za_nia_i_konczy_run(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(
        tmp_path, kroki=[K("a"), K("b", ("a",), extra=BRAMKA), K("c", ("b",), extra=BRAMKA)])
    await wznow(s, Szpieg())
    trzeci = Szpieg()

    zdarzenia = await wznow(s, trzeci, krok="c")

    assert trzeci.kroki == ["c"]                        # ani a, ani b drugi raz
    assert zdarzenia[-1]["kind"] == "run_finished"
    assert artefakt_decyzji(s, "c").is_file()
    assert trzeci.spec("c").wejscia[-1] == ".cockpit/_run/artifacts/decyzja_c.json"


# =====================================================================================
# 9. Krok przerwany smiercia runnera nie jest wznawiany
# =====================================================================================


async def _checkpoint_z_krokiem_w_toku(tmp_path):
    """Run: b(approval), x niezalezny, y po x. Checkpoint przerobiony: x w toku, y czeka."""
    s, _ = await run_zatrzymany_na_bramce(
        tmp_path, kroki=[K("b", extra=BRAMKA), K("x"), K("y", ("x",))])
    cp = wczytaj_checkpoint(s.katalog_runu)
    assert cp.kroki["x"] == StanKrokuCheckpointu("zakonczony", 1)
    kroki = dict(cp.kroki)
    kroki["x"] = StanKrokuCheckpointu("w_toku", 1)
    kroki["y"] = StanKrokuCheckpointu("oczekuje", 0)
    zapisz_checkpoint(s.katalog_runu, Checkpoint(
        run_id=cp.run_id, repozytorium=cp.repozytorium, workflow=cp.workflow,
        base_commit=cp.base_commit, worktree=cp.worktree, kroki=kroki, waiting_on=cp.waiting_on))
    return s


async def test_krok_przerwany_przy_smierci_runnera_dostaje_porazke_z_powodem_zamiast_wznowienia(tmp_path):
    s = await _checkpoint_z_krokiem_w_toku(tmp_path)
    nowy = Szpieg()

    zdarzenia = await wznow(s, nowy)

    assert "x" not in nowy.kroki                        # ani drugi raz, ani w ogole
    porazka = jedno(zdarzenia, "step_failed", "x")
    assert porazka["text"] == TEKST_PRZERWANIA
    assert porazka["run_id"] == RUN_ID
    assert porazka["agent_id"] == "x"
    assert "step_finished" not in [z["kind"] for z in zdarzenia if z["step_id"] == "x"]


async def test_porazka_kroku_przerwanego_jest_ogloszona_zanim_ruszy_krok_za_bramka(tmp_path):
    s = await _checkpoint_z_krokiem_w_toku(tmp_path)

    zdarzenia = await wznow(s, Szpieg())

    assert indeks(zdarzenia, "step_failed", "x") < indeks(zdarzenia, "step_started", "b")


async def test_krok_zalezny_od_przerwanego_jest_pominiety_a_run_konczy_sie_run_finished(tmp_path):
    s = await _checkpoint_z_krokiem_w_toku(tmp_path)
    nowy = Szpieg()

    zdarzenia = await wznow(s, nowy)

    assert nowy.kroki == ["b"]                          # y zalezy od nieudanego x
    jedno(zdarzenia, "step_skipped", "y")
    assert zdarzenia[-1]["kind"] == "run_finished"
    assert rodzaje(zdarzenia).count("run_finished") == 1


# =====================================================================================
# 10. Checkpoint z nieznanym stanem kroku
# =====================================================================================


async def test_checkpoint_z_nieznanym_stanem_kroku_to_porazka_kroku_i_koniec_runu_bez_startu_agenta(tmp_path):
    from runner.graf import StanKroku

    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    cp = wczytaj_checkpoint(s.katalog_runu)
    kroki = dict(cp.kroki)
    kroki["a"] = StanKrokuCheckpointu("zupelnie_nowy_stan", 1)
    zapisz_checkpoint(s.katalog_runu, Checkpoint(
        run_id=cp.run_id, repozytorium=cp.repozytorium, workflow=cp.workflow,
        base_commit=cp.base_commit, worktree=cp.worktree, kroki=kroki, waiting_on=cp.waiting_on))
    try:
        StanKroku("zupelnie_nowy_stan")
    except ValueError as e:
        powod_biblioteki = str(e)
    nowy = Szpieg()

    zdarzenia = await wznow(s, nowy)        # ValueError z wznow_run oblalby test tutaj

    assert nowy.wywolania == []
    assert "step_started" not in rodzaje(zdarzenia)
    porazka = jedno(zdarzenia, "step_failed", "b")
    assert porazka["text"] == f"checkpoint: nieznany stan kroku: {powod_biblioteki}"
    assert porazka["agent_id"] == "b"
    assert zdarzenia[-1]["kind"] == "run_finished"
    assert zdarzenia[-1]["text"] == porazka["text"]
    assert not artefakt_decyzji(s).exists()             # nic nie zapisano przed odrzuceniem checkpointu


# =====================================================================================
# 11. Dokladna tresc zdarzen porazki, zrodla danych wznowienia, wartosci domyslne, blokada
# =====================================================================================


def _zepsuj_worktree_w_checkpoincie(s, nieistniejacy: Path) -> None:
    cp = wczytaj_checkpoint(s.katalog_runu)
    zapisz_checkpoint(s.katalog_runu, Checkpoint(
        run_id=cp.run_id, repozytorium=cp.repozytorium, workflow=cp.workflow,
        base_commit=cp.base_commit, worktree=str(nieistniejacy),
        kroki=cp.kroki, waiting_on=cp.waiting_on))


async def test_porazka_wznowienia_to_dokladnie_dwa_zdarzenia_step_failed_i_run_finished_ze_wszystkimi_polami(
    tmp_path,
):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    brak = tmp_path / "nie_ma_takiego_worktree"
    _zepsuj_worktree_w_checkpoincie(s, brak)
    tekst = f"brak worktree runu: {brak}"

    zdarzenia = await wznow(s, Szpieg())

    assert zdarzenia == [
        {"kind": "step_failed", "run_id": RUN_ID, "step_id": "b", "agent_id": "b", "text": tekst},
        {"kind": "run_finished", "run_id": RUN_ID, "step_id": "b", "agent_id": "", "text": tekst,
         "cost_usd": None, "cost_source": "unavailable"},
    ]


async def test_wznowienie_wczytuje_workflow_z_pliku_workflow_yaml_dokladnie_w_katalogu_runu(
    tmp_path, monkeypatch,
):
    from runner import silnik

    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    odczytane: list[Path] = []
    prawdziwa = silnik.wczytaj_workflow

    def szpieg(plik):
        odczytane.append(plik)
        return prawdziwa(plik)

    monkeypatch.setattr(silnik, "wczytaj_workflow", szpieg)

    await wznow(s, Szpieg())

    assert odczytane == [s.katalog_runu / "workflow.yaml"]     # wielkosc liter nazwy ma znaczenie


async def test_kopia_workflow_z_bledem_pola_to_porazka_kroku_mimo_wczytanego_workflow_bez_startu_agenta(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    (s.katalog_runu / "workflow.yaml").write_text(
        "name: w\nsteps:\n  a: {skill: s_a}\n  b: {skill: s_b, needs: [a], approval: true}\n"
        "  c: {skill: s_c, needs: [b], fanout: 0}\n", encoding="utf-8")
    nowy = Szpieg()

    zdarzenia = await wznow(s, nowy)

    assert nowy.wywolania == []                         # workflow sie wczytal, ale ma blad: nie startujemy
    porazka = jedno(zdarzenia, "step_failed", "b")
    assert porazka["text"].startswith("c: ")
    assert "fanout" in porazka["text"]
    assert zdarzenia[-1]["kind"] == "run_finished"


async def test_zepsuty_project_yaml_w_worktree_to_porazka_kroku_z_dokladnym_tekstem_problemow(tmp_path):
    from cockpit.services.projekt import wczytaj_projekt

    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    (s.worktree / ".cockpit" / "project.yaml").write_text("- to nie jest mapa\n", encoding="utf-8")
    projekt, problemy = wczytaj_projekt(s.worktree)
    assert projekt is None and problemy
    oczekiwany = "\n".join(
        f"{p.krok if p.krok is not None else '-'}: {p.kod}: {p.komunikat}" for p in problemy)
    nowy = Szpieg()

    zdarzenia = await wznow(s, nowy)

    assert nowy.wywolania == []
    assert jedno(zdarzenia, "step_failed", "b")["text"] == oczekiwany
    assert oczekiwany.endswith("project.yaml musi byc mapa")
    assert zdarzenia[-1]["text"] == oczekiwany


async def test_wznowienie_zachowuje_w_checkpoincie_sciezke_repozytorium_uzytkownika(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    przed = wczytaj_checkpoint(s.katalog_runu)
    assert Path(przed.repozytorium).resolve() == s.repo.resolve()

    await wznow(s, Szpieg())

    assert wczytaj_checkpoint(s.katalog_runu).repozytorium == przed.repozytorium


async def test_artefakt_decyzji_zapisuje_polskie_znaki_powodu_doslownie_w_utf8(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)

    await wznow(s, Szpieg(), powod="zażółć gęślą jaźń")

    surowe = artefakt_decyzji(s).read_bytes()
    assert "zażółć gęślą jaźń".encode("utf-8") in surowe
    assert b"\\u" not in surowe


async def test_wznowienie_bez_profilu_bierze_modele_z_profilu_srodowiska(tmp_path, monkeypatch):
    from cockpit.services.invariants import Tier
    from runner import silnik
    from runner.agent import ProfilBackendu

    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    z_env = ProfilBackendu(modele={t: "model-z-env" for t in (Tier.judgment, Tier.execution, Tier.retrieval)})
    monkeypatch.setattr(ProfilBackendu, "z_srodowiska", classmethod(lambda cls: z_env))
    nowy = Szpieg()

    async def emit(zdarzenie: dict) -> None:
        pass

    await asyncio.wait_for(
        silnik.wznow_run(RUN_ID, "b", "approve", emit, nowy, dom=s.dom, root=s.root), LIMIT_S)

    assert nowy.kroki == ["b", "c"]
    for wywolanie in nowy.wywolania:
        assert wywolanie.polecenie[wywolanie.polecenie.index("--model") + 1] == "model-z-env"


async def test_wznowienie_z_podanym_profilem_nie_siega_do_profilu_srodowiska(tmp_path, monkeypatch):
    from cockpit.services.invariants import Tier
    from runner.agent import ProfilBackendu

    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    z_env = ProfilBackendu(modele={t: "model-z-env" for t in (Tier.judgment, Tier.execution, Tier.retrieval)})
    monkeypatch.setattr(ProfilBackendu, "z_srodowiska", classmethod(lambda cls: z_env))
    nowy = Szpieg()

    await wznow(s, nowy)                                  # `wznow` podaje s.profil

    for wywolanie in nowy.wywolania:
        assert wywolanie.polecenie[wywolanie.polecenie.index("--model") + 1] == "m-judgment"


async def test_wznowienie_bez_dom_buduje_rejestr_z_katalogu_domowego(tmp_path, monkeypatch):
    from runner import silnik

    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    dom_uzytkownika = tmp_path / "dom_z_home"
    dom_uzytkownika.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: dom_uzytkownika))
    uzyte: list[Path] = []
    prawdziwa = silnik.rozstrzygnij_rejestr

    def szpieg(worktree, dom):
        uzyte.append(dom)
        return prawdziwa(worktree, dom)

    monkeypatch.setattr(silnik, "rozstrzygnij_rejestr", szpieg)

    async def emit(zdarzenie: dict) -> None:
        pass

    await asyncio.wait_for(
        silnik.wznow_run(RUN_ID, "b", "approve", emit, Szpieg(), profil=s.profil, root=s.root),
        LIMIT_S)

    assert uzyte == [dom_uzytkownika]


async def test_wznowienie_z_podanym_dom_przekazuje_go_do_rejestru_a_nie_katalog_domowy(tmp_path, monkeypatch):
    from runner import silnik

    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    dom_uzytkownika = tmp_path / "dom_z_home"
    dom_uzytkownika.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: dom_uzytkownika))
    uzyte: list[Path] = []
    prawdziwa = silnik.rozstrzygnij_rejestr

    def szpieg(worktree, dom):
        uzyte.append(dom)
        return prawdziwa(worktree, dom)

    monkeypatch.setattr(silnik, "rozstrzygnij_rejestr", szpieg)

    await wznow(s, Szpieg())                              # podaje dom=s.dom

    assert uzyte == [s.dom]


async def test_blokada_wznowienia_jest_osobna_dla_kazdego_runu_a_nie_wspolna_dla_wszystkich(tmp_path, monkeypatch):
    import threading

    from runner import silnik

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    s_a = swiat(tmp_path / "a", [K("a"), K("b", ("a",), extra=BRAMKA), K("c", ("b",))])
    s_b = swiat(tmp_path / "b", [K("a"), K("b", ("a",), extra=BRAMKA), K("c", ("b",))])
    pol_b = {**s_b.polecenie, "run_id": "run-173-b"}
    zdarzenia_a = await uruchom(s_a, Szpieg())
    zdarzenia_b = await uruchom(s_b, Szpieg(), polecenie=pol_b)
    assert zdarzenia_a[-1]["kind"] == "step_waiting" and zdarzenia_b[-1]["kind"] == "step_waiting"
    w_srodku = threading.Event()
    zwolnij = threading.Event()
    prawdziwy = silnik.wczytaj_checkpoint

    def wczytaj(katalog):
        if Path(katalog).name == RUN_ID:                  # run A zatrzymany w sekcji krytycznej
            w_srodku.set()
            zwolnij.wait(timeout=LIMIT_S)
        return prawdziwy(katalog)

    monkeypatch.setattr(silnik, "wczytaj_checkpoint", wczytaj)
    zadanie_a = asyncio.create_task(wznow(s_a, Szpieg()))
    try:
        for _ in range(int(LIMIT_S / 0.05)):
            if w_srodku.is_set():
                break
            await asyncio.sleep(0.05)
        assert w_srodku.is_set(), "wznowienie runu A nie weszlo do sekcji krytycznej"

        nowy_b = Szpieg()
        # wspolna blokada zablokowalaby run B do konca runu A: wait_for rzuci TimeoutError
        await asyncio.wait_for(wznow(s_b, nowy_b, run_id="run-173-b"), 10)
    finally:
        zwolnij.set()
        await asyncio.wait_for(zadanie_a, LIMIT_S)

    assert nowy_b.kroki == ["b", "c"]


async def test_blokady_runow_nie_zostaja_w_pamieci_po_zakonczeniu_wznowienia(tmp_path):
    from runner import silnik

    s, _ = await run_zatrzymany_na_bramce(tmp_path)

    await wznow(s, Szpieg())

    assert len(silnik._BLOKADY) == 0


@pytest.mark.skipif(os.geteuid() == 0, reason="root czyta katalog bez praw odczytu")
async def test_surowy_oserror_z_dysku_przy_wznowieniu_to_porazka_kroku_i_koniec_runu_bez_wyjatku(tmp_path):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    skille = s.worktree / ".cockpit" / "skills"
    przed = (s.katalog_runu / "checkpoint.json").read_bytes()
    nowy = Szpieg()
    skille.chmod(0o000)                     # iterdir w rozstrzygnij_rejestr rzuca PermissionError
    try:
        zdarzenia = await wznow(s, nowy)    # wyjatek z wznow_run oblalby test tutaj
    finally:
        skille.chmod(0o755)

    assert nowy.wywolania == []
    assert rodzaje(zdarzenia) == ["step_failed", "run_finished"]
    tekst = zdarzenia[0]["text"]
    assert tekst.startswith("[Errno 13] Permission denied")
    assert ".cockpit/skills" in tekst
    assert zdarzenia == [
        {"kind": "step_failed", "run_id": RUN_ID, "step_id": "b", "agent_id": "b", "text": tekst},
        {"kind": "run_finished", "run_id": RUN_ID, "step_id": "b", "agent_id": "", "text": tekst,
         "cost_usd": None, "cost_source": "unavailable"},
    ]
    assert (s.katalog_runu / "checkpoint.json").read_bytes() == przed     # nic nie zapisano
    assert not artefakt_decyzji(s).exists()


async def test_skill_usuniety_miedzy_bramka_a_decyzja_to_porazka_walidacji_bez_zapisu_i_bez_startu_agenta(tmp_path):
    from cockpit.services.skills import rozstrzygnij_rejestr
    from cockpit.services.workflow import ma_bledy, wczytaj_workflow
    from cockpit.services.workflow_walidacja import waliduj_workflow

    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    (s.worktree / ".cockpit" / "skills" / "s_c" / "SKILL.md").unlink()
    workflow, problemy = wczytaj_workflow(s.katalog_runu / "workflow.yaml")
    problemy += waliduj_workflow(workflow, rozstrzygnij_rejestr(s.worktree, s.dom), s.dom)
    bledy = [p for p in problemy if ma_bledy([p])]
    assert bledy, "usuniecie skilla powinno dac blad walidacji"
    oczekiwany = "\n".join(f"{p.krok if p.krok is not None else '-'}: {p.kod}: {p.komunikat}" for p in bledy)
    przed = (s.katalog_runu / "checkpoint.json").read_bytes()
    nowy = Szpieg()

    zdarzenia = await wznow(s, nowy)        # KeyError z wznow_run oblalby test tutaj

    assert nowy.wywolania == []
    assert "step_started" not in rodzaje(zdarzenia)
    porazka = jedno(zdarzenia, "step_failed", "b")
    assert porazka["text"] == oczekiwany
    assert oczekiwany.startswith("c: ")
    assert zdarzenia[-1]["kind"] == "run_finished"
    assert zdarzenia[-1]["text"] == porazka["text"]
    assert rodzaje(zdarzenia).count("run_finished") == 1
    assert (s.katalog_runu / "checkpoint.json").read_bytes() == przed
    assert wczytaj_checkpoint(s.katalog_runu).waiting_on.step_id == "b"
    assert not artefakt_decyzji(s).exists()


def _checkpoint_z_zestawem_krokow(s, *, dodaj: str | None = None, usun: str | None = None) -> None:
    cp = wczytaj_checkpoint(s.katalog_runu)
    kroki = dict(cp.kroki)
    if dodaj is not None:
        kroki[dodaj] = StanKrokuCheckpointu("oczekuje", 0)
    if usun is not None:
        del kroki[usun]
    zapisz_checkpoint(s.katalog_runu, Checkpoint(
        run_id=cp.run_id, repozytorium=cp.repozytorium, workflow=cp.workflow,
        base_commit=cp.base_commit, worktree=cp.worktree, kroki=kroki, waiting_on=cp.waiting_on))


@pytest.mark.parametrize("zmiana", [{"dodaj": "z"}, {"usun": "c"}], ids=["krok_nadmiarowy", "krok_brakujacy"])
async def test_checkpoint_z_innym_zestawem_krokow_niz_kopia_workflow_to_porazka_kroku_bez_zapisu_i_startu(
    tmp_path, zmiana,
):
    s, _ = await run_zatrzymany_na_bramce(tmp_path)
    _checkpoint_z_zestawem_krokow(s, **zmiana)
    przed = (s.katalog_runu / "checkpoint.json").read_bytes()
    nowy = Szpieg()
    tekst = "checkpoint nie pasuje do kopii workflow: inne kroki"

    zdarzenia = await wznow(s, nowy)

    assert nowy.wywolania == []
    assert zdarzenia == [
        {"kind": "step_failed", "run_id": RUN_ID, "step_id": "b", "agent_id": "b", "text": tekst},
        {"kind": "run_finished", "run_id": RUN_ID, "step_id": "b", "agent_id": "", "text": tekst,
         "cost_usd": None, "cost_source": "unavailable"},
    ]
    assert (s.katalog_runu / "checkpoint.json").read_bytes() == przed
    assert not artefakt_decyzji(s).exists()


async def test_blad_wznowienia_jest_bledem_dziedziny():
    from cockpit.services.errors import BladDziedziny
    from runner import silnik

    assert issubclass(silnik.BladWznowienia, BladDziedziny)
