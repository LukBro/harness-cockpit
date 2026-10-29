"""Spłaszczenie ADF z Jiry do czytelnego tekstu (HAR-61, część 3/9 BRO-111)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from runner.tickety.adf import adf_na_tekst
from runner.tickety.bledy import BladIntegracji

FIXTURE = Path(__file__).parent / "fixtures" / "tickety" / "jira_adf_opis.json"


# --- pomocnicy -------------------------------------------------------------


def _tekst(napis, *znaczniki):
    wezel = {"type": "text", "text": napis}
    if znaczniki:
        wezel["marks"] = list(znaczniki)
    return wezel


def _akapit(*dzieci):
    return {"type": "paragraph", "content": list(dzieci)}


def _akapit_z(napis):
    return _akapit(_tekst(napis))


def _dok(*bloki):
    return {"version": 1, "type": "doc", "content": list(bloki)}


def _pozycja(*bloki):
    return {"type": "listItem", "content": list(bloki)}


def _punktowana(*pozycje):
    return {"type": "bulletList", "content": list(pozycje)}


def _numerowana(*pozycje, order=None):
    wezel = {"type": "orderedList", "content": list(pozycje)}
    if order is not None:
        wezel["attrs"] = {"order": order}
    return wezel


def _znacznik_kodu():
    return {"type": "code"}


def _link(href):
    return {"type": "link", "attrs": {"href": href}}


def _wynik_akapitu(*dzieci):
    return adf_na_tekst(_dok(_akapit(*dzieci)))


# --- fixture z opisem zgłoszenia -------------------------------------------


def test_opis_jira_z_lista_kodem_i_linkiem_daje_czytelny_tekst_z_zachowana_struktura():
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))

    oczekiwany = (
        "## Logowanie nie działa\n"
        "\n"
        "Użytkownik `token` nie jest odświeżany, zob. dokumentację "
        "(https://example.com/docs/auth). Zgłosił @Jan Kowalski :warning:\n"
        "\n"
        "Kroki do wykonania:\n"
        "\n"
        "- Otwórz stronę logowania\n"
        "- Sprawdź sesję\n"
        "  1. Odczytaj ciasteczko\n"
        "     - flaga httpOnly\n"
        "  2. Porównaj z bazą\n"
        "- Zgłoś wynik\n"
        "\n"
        "```python\n"
        "def odswiez(token):\n"
        "    return token\n"
        "```\n"
        "\n"
        "Dziękujemy za pomoc."
    )
    assert adf_na_tekst(doc) == oczekiwany


def test_opis_jira_po_splaszczeniu_nie_zawiera_surowego_json():
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))

    wynik = adf_na_tekst(doc)

    assert isinstance(wynik, str)
    for fragment in ("{", "}", '"type"', "attrs", "listItem"):
        assert fragment not in wynik


# --- wejście nie będące dokumentem ADF -------------------------------------


def test_opis_jako_napis_wiki_markup_data_center_wraca_bez_zmian():
    wejscie = "h1. Tytul\n* punkt\n{code}x = 1{code}"

    assert adf_na_tekst(wejscie) is wejscie


def test_brak_opisu_none_daje_pusty_napis():
    assert adf_na_tekst(None) == ""


@pytest.mark.parametrize("wejscie", [[{"type": "doc", "content": []}], 5, 2.5, True])
def test_wejscie_inne_niz_slownik_napis_albo_none_daje_pusty_napis(wejscie):
    assert adf_na_tekst(wejscie) == ""


def test_slownik_bez_typu_jest_nieznanym_wezlem_i_daje_tekst_dzieci():
    wejscie = {"content": [_akapit_z("a"), _akapit_z("b")]}

    assert adf_na_tekst(wejscie) == "a\n\nb"


def test_samotny_akapit_bez_dokumentu_daje_swoj_tekst():
    assert adf_na_tekst(_akapit_z("a")) == "a"


def test_samotny_naglowek_bez_dokumentu_daje_naglowek():
    wejscie = {"type": "heading", "attrs": {"level": 3}, "content": [_tekst("T")]}

    assert adf_na_tekst(wejscie) == "### T"


# --- glebokosc -------------------------------------------------------------


def test_zlosliwie_gleboki_adf_konczy_sie_bledem_dziedziny_jira_bez_json():
    wezel = _akapit_z("x")
    for _ in range(100_000):
        wezel = {"type": "panel", "content": [wezel]}

    with pytest.raises(BladIntegracji) as wyjatek:
        adf_na_tekst(_dok(wezel))

    assert wyjatek.value.kod == "blad_dostawcy"
    assert wyjatek.value.dostawca == "jira"
    assert wyjatek.value.komunikat == (
        "jira: opis zgłoszenia w ADF jest zbyt głęboko zagnieżdżony, "
        "nie da się go spłaszczyć."
    )
    assert wyjatek.value.__cause__ is None
    assert wyjatek.value.__suppress_context__ is True


def test_lista_zagniezdzona_na_dwudziestu_poziomach_daje_wciecie_kazdego_poziomu():
    wezel = _punktowana(_pozycja(_akapit_z("p19")))
    for i in range(18, -1, -1):
        wezel = _punktowana(_pozycja(_akapit_z(f"p{i}"), wezel))

    oczekiwany = "\n".join(" " * (2 * i) + f"- p{i}" for i in range(20))
    assert adf_na_tekst(_dok(wezel)) == oczekiwany


# --- szkielet dokumentu ----------------------------------------------------


def test_dokument_bez_tresci_daje_pusty_napis():
    assert adf_na_tekst({"version": 1, "type": "doc"}) == ""


def test_dokument_z_pusta_lista_tresci_daje_pusty_napis():
    assert adf_na_tekst(_dok()) == ""


def test_bloki_najwyzszego_poziomu_sa_rozdzielone_pusta_linia_bez_koncowego_nowego_wiersza():
    assert adf_na_tekst(_dok(_akapit_z("a"), _akapit_z("b"))) == "a\n\nb"


def test_pusty_akapit_miedzy_akapitami_nie_robi_podwojnej_pustej_linii():
    doc = _dok(_akapit_z("a"), _akapit(), _akapit_z("b"))

    assert adf_na_tekst(doc) == "a\n\nb"


def test_akapit_z_pustym_tekstem_jest_pomijany_jak_pusty_blok():
    doc = _dok(_akapit_z("a"), _akapit(_tekst("")), _akapit_z("b"))

    assert adf_na_tekst(doc) == "a\n\nb"


def test_dokument_z_samymi_pustymi_blokami_daje_pusty_napis():
    assert adf_na_tekst(_dok(_akapit(), _akapit())) == ""


def _panel_z_tekstem_i(blok):
    return {"type": "panel", "content": [_tekst("a"), blok]}


def test_nieznany_wezel_z_naglowkiem_wsrod_dzieci_laczy_dzieci_pusta_linia():
    naglowek = {"type": "heading", "attrs": {"level": 1}, "content": [_tekst("B")]}

    assert adf_na_tekst(_dok(_panel_z_tekstem_i(naglowek))) == "a\n\n# B"


def test_nieznany_wezel_z_dwoma_naglowkami_laczy_je_pusta_linia():
    def naglowek(t):
        return {"type": "heading", "attrs": {"level": 1}, "content": [_tekst(t)]}

    panel = {"type": "panel", "content": [naglowek("A"), naglowek("B")]}

    assert adf_na_tekst(_dok(panel)) == "# A\n\n# B"


def test_nieznany_wezel_z_blokiem_kodu_wsrod_dzieci_laczy_dzieci_pusta_linia():
    kod = {"type": "codeBlock", "content": [_tekst("x")]}

    assert adf_na_tekst(_dok(_panel_z_tekstem_i(kod))) == "a\n\n```\nx\n```"


def test_nieznany_wezel_z_linia_pozioma_wsrod_dzieci_laczy_dzieci_pusta_linia():
    assert adf_na_tekst(_dok(_panel_z_tekstem_i({"type": "rule"}))) == "a\n\n---"


def test_nieznany_wezel_z_lista_punktowana_wsrod_dzieci_laczy_dzieci_pusta_linia():
    lista = _punktowana(_pozycja(_tekst("b")))

    assert adf_na_tekst(_dok(_panel_z_tekstem_i(lista))) == "a\n\n- b"


def test_nieznany_wezel_z_lista_numerowana_wsrod_dzieci_laczy_dzieci_pusta_linia():
    lista = _numerowana(_pozycja(_tekst("b")))

    assert adf_na_tekst(_dok(_panel_z_tekstem_i(lista))) == "a\n\n1. b"


def test_akapit_skleja_dzieci_inline_nawet_gdy_dziecko_jest_blokiem():
    akapit = {"type": "paragraph", "content": [_akapit_z("a"), _akapit_z("b")]}

    assert adf_na_tekst(_dok(akapit)) == "ab"


def test_akapit_sklada_tekst_dzieci_inline_bez_separatora():
    assert _wynik_akapitu(_tekst("ala "), _tekst("ma"), _tekst(" kota")) == "ala ma kota"


# --- nagłówki --------------------------------------------------------------


@pytest.mark.parametrize("poziom", [1, 2, 3, 4, 5, 6])
def test_naglowek_ma_tyle_krzyzykow_ile_wynosi_jego_poziom(poziom):
    doc = _dok(
        {"type": "heading", "attrs": {"level": poziom}, "content": [_tekst("Tytuł")]}
    )

    assert adf_na_tekst(doc) == "#" * poziom + " Tytuł"


@pytest.mark.parametrize(
    "poziom",
    [0, 7, -1, 100, "2", 2.5, None, True],
    ids=["zero", "siedem", "ujemny", "sto", "napis", "ulamek", "none", "bool"],
)
def test_naglowek_o_poziomie_spoza_zakresu_albo_zlego_typu_ma_jeden_krzyzyk(poziom):
    doc = _dok(
        {"type": "heading", "attrs": {"level": poziom}, "content": [_tekst("Tytuł")]}
    )

    assert adf_na_tekst(doc) == "# Tytuł"


def test_naglowek_bez_atrybutow_ma_jeden_krzyzyk():
    doc = _dok({"type": "heading", "content": [_tekst("Tytuł")]})

    assert adf_na_tekst(doc) == "# Tytuł"


# --- inline: znaczniki tekstu ----------------------------------------------


def test_fragment_kodu_jest_ujety_w_odwrotne_apostrofy():
    assert _wynik_akapitu(_tekst("x", _znacznik_kodu())) == "`x`"


def test_link_dopisuje_adres_w_nawiasie_po_tekscie():
    assert _wynik_akapitu(_tekst("dok", _link("https://a"))) == "dok (https://a)"


def test_fragment_kodu_z_linkiem_ma_kod_wewnatrz_a_adres_na_zewnatrz_niezaleznie_od_kolejnosci_znacznikow():
    kod_potem_link = _wynik_akapitu(_tekst("x", _znacznik_kodu(), _link("https://a")))
    link_potem_kod = _wynik_akapitu(_tekst("x", _link("https://a"), _znacznik_kodu()))

    assert kod_potem_link == "`x` (https://a)"
    assert link_potem_kod == "`x` (https://a)"


def test_link_bez_adresu_zostawia_sam_tekst():
    assert _wynik_akapitu(_tekst("dok", {"type": "link", "attrs": {}})) == "dok"
    assert _wynik_akapitu(_tekst("dok", {"type": "link"})) == "dok"


def test_nieznany_znacznik_tekstu_jest_ignorowany():
    wynik = _wynik_akapitu(
        _tekst("x", {"type": "strong"}, {"type": "em"}, {"type": "textColor", "attrs": {"color": "#f00"}})
    )

    assert wynik == "x"


def test_nieznany_znacznik_obok_znacznika_kodu_nie_zmienia_wyniku_kodu():
    wynik = _wynik_akapitu(_tekst("x", {"type": "strong"}, _znacznik_kodu()))

    assert wynik == "`x`"


def test_tekst_bez_pola_text_albo_z_nienapisem_daje_pusty_napis():
    assert _wynik_akapitu({"type": "text"}) == ""
    assert _wynik_akapitu({"type": "text", "text": 5}) == ""
    assert _wynik_akapitu({"type": "text", "text": None}, _tekst("b")) == "b"


# --- inline: mention, emoji, inlineCard, hardBreak -------------------------


def test_wzmianka_z_malpa_w_tekscie_ma_jedna_malpe():
    wynik = _wynik_akapitu(
        {"type": "mention", "attrs": {"id": "1", "text": "@Jan Kowalski"}}
    )

    assert wynik == "@Jan Kowalski"


def test_wzmianka_bez_malpy_w_tekscie_dostaje_malpe():
    wynik = _wynik_akapitu({"type": "mention", "attrs": {"id": "1", "text": "Jan"}})

    assert wynik == "@Jan"


def test_wzmianka_bez_nazwy_nie_daje_samotnej_malpy():
    assert _wynik_akapitu({"type": "mention", "attrs": {"id": "1"}}) == ""
    assert _wynik_akapitu({"type": "mention"}) == ""
    assert _wynik_akapitu({"type": "mention", "attrs": {"text": "@"}}) == ""
    assert _wynik_akapitu({"type": "mention", "attrs": {"text": ""}}) == ""


def test_emoji_daje_krotka_nazwe_gdy_jest_podana():
    wynik = _wynik_akapitu(
        {"type": "emoji", "attrs": {"shortName": ":smile:", "text": "😄"}}
    )

    assert wynik == ":smile:"


def test_emoji_bez_krotkiej_nazwy_daje_znak_z_pola_text():
    assert _wynik_akapitu({"type": "emoji", "attrs": {"text": "😄"}}) == "😄"


def test_emoji_bez_nazwy_i_znaku_daje_pusty_napis():
    assert _wynik_akapitu({"type": "emoji", "attrs": {"id": "1f604"}}) == ""
    assert _wynik_akapitu({"type": "emoji"}) == ""


def test_karta_linku_daje_adres_z_atrybutow():
    wynik = _wynik_akapitu(
        _tekst("zob. "), {"type": "inlineCard", "attrs": {"url": "https://x.example/PRJ-1"}}
    )

    assert wynik == "zob. https://x.example/PRJ-1"


def test_karta_linku_bez_adresu_daje_pusty_napis():
    assert _wynik_akapitu(_tekst("a"), {"type": "inlineCard", "attrs": {}}, _tekst("b")) == "ab"
    assert _wynik_akapitu(_tekst("a"), {"type": "inlineCard"}, _tekst("b")) == "ab"


def test_twarde_zlamanie_wiersza_w_akapicie_daje_nowy_wiersz():
    assert _wynik_akapitu(_tekst("a"), {"type": "hardBreak"}, _tekst("b")) == "a\nb"


def test_linia_pozioma_jest_trzema_myslnikami_otoczonymi_pustymi_liniami():
    doc = _dok(_akapit_z("a"), {"type": "rule"}, _akapit_z("b"))

    assert adf_na_tekst(doc) == "a\n\n---\n\nb"


# --- blok kodu -------------------------------------------------------------


def test_blok_kodu_z_jezykiem_ma_jezyk_w_linii_otwierajacej():
    doc = _dok(
        {
            "type": "codeBlock",
            "attrs": {"language": "python"},
            "content": [_tekst("x = 1")],
        }
    )

    assert adf_na_tekst(doc) == "```python\nx = 1\n```"


def test_blok_kodu_bez_atrybutow_ma_sama_otwierajaca_linie_z_trzema_apostrofami():
    doc = _dok({"type": "codeBlock", "content": [_tekst("x = 1")]})

    assert adf_na_tekst(doc) == "```\nx = 1\n```"


def test_blok_kodu_z_pustym_jezykiem_albo_jezykiem_nie_napisem_nie_dopisuje_jezyka():
    pusty = _dok({"type": "codeBlock", "attrs": {"language": ""}, "content": [_tekst("x")]})
    liczba = _dok({"type": "codeBlock", "attrs": {"language": 5}, "content": [_tekst("x")]})

    assert adf_na_tekst(pusty) == "```\nx\n```"
    assert adf_na_tekst(liczba) == "```\nx\n```"


def test_blok_kodu_ignoruje_znaczniki_wewnatrz_i_skleja_kawalki_tekstu():
    doc = _dok(
        {
            "type": "codeBlock",
            "attrs": {"language": "sh"},
            "content": [
                _tekst("echo ", _znacznik_kodu()),
                _tekst("hej", _link("https://a"), {"type": "strong"}),
            ],
        }
    )

    assert adf_na_tekst(doc) == "```sh\necho hej\n```"


def test_blok_kodu_zachowuje_wielolinijkowy_tekst_ze_wcieciami():
    doc = _dok(
        {
            "type": "codeBlock",
            "attrs": {"language": "python"},
            "content": [_tekst("def f():\n    return 1")],
        }
    )

    assert adf_na_tekst(doc) == "```python\ndef f():\n    return 1\n```"


# --- listy i wcięcia -------------------------------------------------------


def test_lista_punktowa_ma_pozycje_jedna_pod_druga_bez_pustych_linii():
    doc = _dok(_punktowana(_pozycja(_akapit_z("a")), _pozycja(_akapit_z("b"))))

    assert adf_na_tekst(doc) == "- a\n- b"


def test_lista_numerowana_bez_atrybutow_liczy_od_jedynki():
    doc = _dok(_numerowana(_pozycja(_akapit_z("a")), _pozycja(_akapit_z("b")), _pozycja(_akapit_z("c"))))

    assert adf_na_tekst(doc) == "1. a\n2. b\n3. c"


def test_lista_numerowana_zaczyna_od_wartosci_order_z_atrybutow():
    doc = _dok(_numerowana(_pozycja(_akapit_z("a")), _pozycja(_akapit_z("b")), order=5))

    assert adf_na_tekst(doc) == "5. a\n6. b"


@pytest.mark.parametrize(
    "order",
    [0, -3, "5", 2.5, None, False],
    ids=["zero", "ujemny", "napis", "ulamek", "none", "false"],
)
def test_lista_numerowana_o_zlym_order_liczy_od_jedynki(order):
    doc = _dok(_numerowana(_pozycja(_akapit_z("a")), _pozycja(_akapit_z("b")), order=order))

    assert adf_na_tekst(doc) == "1. a\n2. b"


def test_lista_numerowana_z_order_bool_nie_traktuje_go_jak_liczby():
    # Bez wykluczenia bool numer zostalby wypisany jako "True", wiec ten test
    # zabija implementacje, ktora przyjmuje True jako liczbe calkowita.
    doc = _dok(_numerowana(_pozycja(_akapit_z("a")), _pozycja(_akapit_z("b")), order=True))

    assert adf_na_tekst(doc) == "1. a\n2. b"


def test_lista_numerowana_z_atrybutami_none_liczy_od_jedynki():
    lista = _numerowana(_pozycja(_akapit_z("a")))
    lista["attrs"] = None

    assert adf_na_tekst(_dok(lista)) == "1. a"


def test_kolejny_akapit_w_pozycji_listy_punktowanej_jest_wciety_o_szerokosc_znacznika():
    doc = _dok(_punktowana(_pozycja(_akapit_z("a"), _akapit_z("b")), _pozycja(_akapit_z("c"))))

    assert adf_na_tekst(doc) == "- a\n  b\n- c"


def test_kolejny_akapit_w_pozycji_listy_numerowanej_jest_wciety_o_trzy_spacje():
    doc = _dok(_numerowana(_pozycja(_akapit_z("a"), _akapit_z("b")), _pozycja(_akapit_z("c"))))

    assert adf_na_tekst(doc) == "1. a\n   b\n2. c"


def test_zagniezdzona_lista_numerowana_ma_wciecie_szerokosci_znacznika_rodzica():
    doc = _dok(
        _punktowana(
            _pozycja(_akapit_z("a")),
            _pozycja(
                _akapit_z("b"),
                _numerowana(_pozycja(_akapit_z("b1")), _pozycja(_akapit_z("b2"))),
            ),
            _pozycja(_akapit_z("c")),
        )
    )

    assert adf_na_tekst(doc) == "- a\n- b\n  1. b1\n  2. b2\n- c"


def test_trzeci_poziom_zagniezdzenia_sumuje_wciecia_rodzicow():
    doc = _dok(
        _punktowana(
            _pozycja(
                _akapit_z("a"),
                _numerowana(
                    _pozycja(
                        _akapit_z("a1"),
                        _punktowana(_pozycja(_akapit_z("a1x"))),
                    )
                ),
            )
        )
    )

    assert adf_na_tekst(doc) == "- a\n  1. a1\n     - a1x"


def test_zagniezdzenie_pod_pozycja_dwucyfrowa_ma_wciecie_czterech_spacji():
    doc = _dok(
        _numerowana(
            _pozycja(_akapit_z("a"), _punktowana(_pozycja(_akapit_z("x")))),
            _pozycja(
                _akapit_z("b"),
                _punktowana(_pozycja(_akapit_z("y"))),
                _akapit_z("c"),
            ),
            order=9,
        )
    )

    assert adf_na_tekst(doc) == "9. a\n   - x\n10. b\n    - y\n    c"


def test_zagniezdzenie_pod_pozycja_jednocyfrowa_ma_wciecie_trzech_spacji():
    doc = _dok(_numerowana(_pozycja(_akapit_z("a"), _punktowana(_pozycja(_akapit_z("x"))))))

    assert adf_na_tekst(doc) == "1. a\n   - x"


def test_pusta_linia_wewnatrz_pozycji_listy_zostaje_pusta_bez_spacji_wciecia():
    panel = {"type": "panel", "content": [_akapit_z("x"), _akapit_z("y")]}
    doc = _dok(_punktowana(_pozycja(_akapit_z("a"), panel)))

    assert adf_na_tekst(doc) == "- a\n  x\n\n  y"


def test_pusta_pozycja_listy_jest_pomijana_ale_numeracja_liczy_ja_dalej():
    doc = _dok(
        _numerowana(
            _pozycja(),
            _pozycja(_akapit()),
            _pozycja(_akapit_z("a")),
            _pozycja(_akapit_z("b")),
        )
    )

    assert adf_na_tekst(doc) == "3. a\n4. b"


def test_pusta_pozycja_listy_punktowanej_nie_zostawia_pustego_znacznika():
    doc = _dok(_punktowana(_pozycja(), _pozycja(_akapit_z("a")), _pozycja(_akapit())))

    assert adf_na_tekst(doc) == "- a"


def test_lista_z_samymi_pustymi_pozycjami_jest_pomijana_jak_pusty_blok():
    doc = _dok(_akapit_z("a"), _numerowana(_pozycja(), _pozycja(_akapit())), _akapit_z("b"))

    assert adf_na_tekst(doc) == "a\n\nb"


def test_pusta_lista_nie_zostawia_sladu_w_dokumencie():
    doc = _dok(_akapit_z("a"), _punktowana(), _akapit_z("b"))

    assert adf_na_tekst(doc) == "a\n\nb"


# --- nieznane typy węzłów --------------------------------------------------


def test_nieznany_wezel_z_dwoma_akapitami_laczy_je_pusta_linia():
    panel = {"type": "panel", "attrs": {"panelType": "info"}, "content": [_akapit_z("a"), _akapit_z("b")]}

    assert adf_na_tekst(_dok(panel)) == "a\n\nb"


def test_nieznany_wezel_z_dziecmi_inline_skleja_je_bez_separatora():
    wezel = {"type": "xyz", "content": [_tekst("a"), _tekst("b")]}

    assert adf_na_tekst(_dok(wezel)) == "ab"


def test_nieznany_wezel_z_dzieckiem_blokowym_traktuje_inline_obok_jak_osobny_blok():
    wezel = {"type": "xyz", "content": [_tekst("a"), _akapit_z("b")]}

    assert adf_na_tekst(_dok(wezel)) == "a\n\nb"


def test_zagniezdzony_nieznany_wezel_z_blokiem_w_srodku_jest_blokowy():
    expand = {
        "type": "expand",
        "attrs": {"title": "Szczegóły"},
        "content": [
            {"type": "panel", "content": [_akapit_z("a")]},
            {"type": "panel", "content": [_akapit_z("b")]},
        ],
    }

    assert adf_na_tekst(_dok(expand)) == "a\n\nb"


def test_nieznany_wezel_ze_slownikiem_w_atrybutach_nie_wypuszcza_json_do_wyniku():
    status = {
        "type": "status",
        "attrs": {"color": "red", "meta": {"klucz": "wartosc"}},
        "content": [_tekst("Gotowe")],
    }

    wynik = adf_na_tekst(_dok(status))

    assert wynik == "Gotowe"
    for znak in ("{", "}", '"'):
        assert znak not in wynik


def test_nieznany_wezel_bez_dzieci_ze_slownikiem_w_atrybutach_daje_pusty_napis():
    wezel = {"type": "mediaSingle", "attrs": {"layout": "center", "width": {"a": 1}}}

    assert adf_na_tekst(_dok(wezel)) == ""
    assert adf_na_tekst(_dok(_akapit_z("a"), wezel, _akapit_z("b"))) == "a\n\nb"


def test_nieznany_wezel_wewnatrz_akapitu_daje_swoj_tekst_w_biegu_zdania():
    status = {"type": "status", "attrs": {"text": "x"}, "content": [_tekst("b")]}

    assert _wynik_akapitu(_tekst("a"), status, _tekst("c")) == "abc"


def test_nieznany_wezel_w_pozycji_listy_jest_wciety_razem_z_pozycja():
    panel = {"type": "panel", "content": [_akapit_z("x")]}
    doc = _dok(_punktowana(_pozycja(_akapit_z("a"), panel)))

    assert adf_na_tekst(doc) == "- a\n  x"


# --- zniekształcone drzewo -------------------------------------------------


def test_tresc_dokumentu_jako_napis_nie_rzuca_i_daje_pusty_napis():
    assert adf_na_tekst({"type": "doc", "content": "abc"}) == ""


def test_tresc_dokumentu_jako_slownik_nie_rzuca_i_daje_pusty_napis():
    assert adf_na_tekst({"type": "doc", "content": {"type": "paragraph"}}) == ""


def test_elementy_tresci_nie_bedace_slownikami_sa_pomijane():
    doc = {"type": "doc", "content": [1, "a", None, [], _akapit_z("ok")]}

    assert adf_na_tekst(doc) == "ok"


def test_tresc_dokumentu_jako_liczba_nie_rzuca_i_daje_pusty_napis():
    assert adf_na_tekst({"type": "doc", "content": 5}) == ""


def test_tresc_akapitu_jako_liczba_nie_rzuca_i_daje_pusty_blok():
    doc = _dok({"type": "paragraph", "content": 5}, _akapit_z("b"))

    assert adf_na_tekst(doc) == "b"


def test_znaczniki_tekstu_jako_liczba_sa_ignorowane():
    tekst = {"type": "text", "text": "a", "marks": 5}

    assert _wynik_akapitu(tekst) == "a"


def test_tresc_akapitu_jako_napis_nie_rzuca_i_daje_pusty_blok():
    doc = _dok({"type": "paragraph", "content": "abc"}, _akapit_z("b"))

    assert adf_na_tekst(doc) == "b"


def test_dzieci_akapitu_nie_bedace_slownikami_sa_pomijane():
    assert _wynik_akapitu("a", None, 5, _tekst("b")) == "b"


def test_atrybuty_none_albo_nie_slownik_sa_traktowane_jak_brak():
    for atrybuty in (None, "napis", 5, []):
        naglowek = _dok({"type": "heading", "attrs": atrybuty, "content": [_tekst("T")]})
        kod = _dok({"type": "codeBlock", "attrs": atrybuty, "content": [_tekst("x")]})
        wzmianka = _wynik_akapitu({"type": "mention", "attrs": atrybuty})
        emoji = _wynik_akapitu({"type": "emoji", "attrs": atrybuty})
        karta = _wynik_akapitu({"type": "inlineCard", "attrs": atrybuty})
        nieznany = adf_na_tekst(_dok({"type": "xyz", "attrs": atrybuty, "content": [_tekst("t")]}))

        assert adf_na_tekst(naglowek) == "# T"
        assert adf_na_tekst(kod) == "```\nx\n```"
        assert wzmianka == ""
        assert emoji == ""
        assert karta == ""
        assert nieznany == "t"


def test_znacznik_linku_z_atrybutami_none_zostawia_sam_tekst():
    wynik = _wynik_akapitu(_tekst("dok", {"type": "link", "attrs": None}))

    assert wynik == "dok"


def test_znaczniki_nie_bedace_lista_albo_slownikami_sa_ignorowane():
    jako_napis = {"type": "text", "text": "a", "marks": "code"}
    ze_smieciami = {"type": "text", "text": "b", "marks": [None, "code", 5]}

    assert _wynik_akapitu(jako_napis) == "a"
    assert _wynik_akapitu(ze_smieciami) == "b"


def test_pozycja_listy_ze_smieciowymi_dziecmi_nie_rzuca():
    smieciowa = {"type": "listItem", "content": ["x", None, _akapit_z("a")]}
    doc = _dok(_punktowana(smieciowa, "smiec"))

    assert adf_na_tekst(doc) == "- a"
