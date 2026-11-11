# Komentarze w tickecie i rozliczenie czasu

Czytasz w kroku 2 (plan) i w kroku 7 (zamknięcie). Komentarze są dla ludzi i dla `/monolynx:retro`;
źródłem prawdy dla bramek jest stan.json. Długą treść zapisujesz narzędziem zapisu do pliku
w katalogu stanu i podajesz wrapperowi jako `--content @<plik>`.

Język: polski z pełnymi znakami diakrytycznymi.

## Plan pracy (krok 2) - źródło wznowienia dla ludzi

```
**Team Manager - Plan pracy**

**Tryb:** zmiana | defekt
**Raport Researchera (streszczenie):** zakres, ryzyka, podejście; Granice: ...
**Agenci i przydział:**
- <agent> - <uzasadnienie> - pliki: <lista>
- <krytyk> - obowiązkowy quality gate
**Kontrakt (zaakceptowany | auto):** stop przed: <lista>; zdjęte przez człowieka: <lista|brak>
**Baseline:** SHA <sha>; brudne przed startem: <lista|brak>; rozjazd z bazą: <z start.py>
**Krytyk:** <linia CRITIC z start.py>
**Stan:** <ścieżka stan.json>
**Plan:** 1. ... 2. ...
```

## Podsumowanie agenta (krok 7, każdy agent i krytyk)

```
**<agent> - Podsumowanie pracy**
Co zrobiono: - <zmiana - pliki>
Ocena krytyka: <X>/100 (<APPROVED|NEEDS WORK> -> <n> iteracji)
Czas pracy: <X> min
```

Krytyk: zamiast "Co zrobiono" wklej dosłownie ostatni blok **Code Review** (linie odjęć, REGULA),
potem "Rola: quality gate (ocenił N agentów, M iteracji)". Powód: retro czyta komentarze, nie logi.

## Podsumowanie zadania (krok 7)

```
**Team Manager - Podsumowanie zadania**
Zrealizowane: ...
Zespół i oceny: <agent>: <X>/100 (<min> min) ...
Krytyk: <kto wydał werdykt> (<min> min)
Rekoncyliacja: <REKONCYLIACJA z rekoncyliacja.py>
Bramki: <linie BRAMKA ... z ostatniego przebiegu>; skrót diffu <skrot>
Kryteria: <odhaczone>/<wszystkie>; niezrealizowane: <id - powód>
Reguły krytyka: <REGULA ... | brak>; powtórzona -> zaznacz
Commit/PR: <sha, URL | powód braku>
Czas: zespół <suma> min, Team Manager <CZAS-TM> min
```

## BLOKADA ZEWNĘTRZNA (werdykt.py kod 4)

```
**BLOKADA ZEWNĘTRZNA**
Agent: <typ> (<score>/100)
Blokady: - [-N] <plik:linia> <wada> - powód: <zdanie>
Opcje: (a) usuń blokadę i wznów skill; (b) zaakceptuj mimo progu (zamkniecie_wyjatek);
(c) zmień zakres albo wydziel blokadę do nowego ticketu.
```

## SPRINT-RUN STOP (tylko MONOLYNX_SPRINT_RUN=1)

```
**SPRINT-RUN STOP**
Powód: <powód z tabeli STOP w SKILL.md>
Etap: <krok>
Co odblokuje: <jedno zdanie>
```

Ostatnia linia odpowiedzi: `SPRINT-RUN STOP: <powód>`. Statusu na `in_review` nie zmieniasz.

## Czas (log_time)

- `date_logged` = data dnia wpisu (`date +%F`).
- Researcher: `stan.py znacznik` przed i po (TM czeka synchronicznie, więc stoper = jego praca).
- Agenci: wartość z ich linii `CZAS PRACY`, nie stoper TM; brak linii -> szacunek z adnotacją
  `(czas szacowany)`. Krytyk z kilkoma rundami: suma w jednym wpisie.
- Team Manager: `stan.py czas <KEY>` (formuła: wall-clock minus okno pracy zespołu, minimum 1).
- Błąd `add_comment`/`log_time`: jeden ponowny zapis, potem odnotuj i pracuj dalej - ceremonia nie
  zatrzymuje pracy. Błąd `update_ticket` i `update_acceptance_criterion` zgłoś człowiekowi wprost.
