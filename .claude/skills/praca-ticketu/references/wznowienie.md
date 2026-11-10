# Wznowienie przerwanej pracy

Czytasz, gdy `start.py` zastał istniejący stan.json albo ticket ma status `in_progress` i komentarz
"Team Manager - Plan pracy". Nie zaczynasz od zera i nie mierzysz baseline ponownie: brudne pliki są
teraz pracą zespołu, a ponowny pomiar wykluczyłby je z oceny krytyka.

`python3 <SKILL_DIR>/scripts/stan.py pokaz <KEY>` - etap ustalasz z pól stanu, w tej kolejności:

| Stan | Ostatni domknięty etap | Wznów od |
|---|---|---|
| brak `przydzial` | start / research | krok 1 (Researcher) |
| `przydzial`, brak `znaczniki.koniec_zespolu` | plan | krok 3 (zespół) |
| `koniec_zespolu`, brak `bramki` dla bieżącego skrótu | developerzy | krok 4 (rekoncyliacja) |
| `bramki` zielone, ostatni werdykt brak albo kod != 0 | bramki | krok 6 (krytyk) |
| werdykt kod 0, `bramka_zamkniecia.py` != 0 | review | krok 7 (kryteria i bramka zamknięcia) |
| `bramka_zamkniecia.py` = 0, status `in_progress` | zamknięcie przerwane | krok 7: commit wg flag, status |

Bramki same powiedzą, czy wynik dotyczy bieżącego diffu (skrót) - nie musisz tego oceniać.

Sprzeczne ślady (komentarz podsumowania jest, a stan mówi "bramki niezielone") -> ufasz bramkom,
w odpowiedzi wypisujesz obie wersje. Bez stanu (sesja sprzed tej wersji skilla): komentarz planu ->
`stan.py ustaw` z jego pól (przydział, baseline), potem tabela wyżej. Pełny obraz bez podejmowania pracy
daje `/monolynx:resume <KEY>`.
