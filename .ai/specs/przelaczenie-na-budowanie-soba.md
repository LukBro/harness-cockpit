# Przełączenie na budowanie sobą

## Decyzja

- **Do przełączenia:** pierwsze kamienie milowe budujemy istniejącym skillem
  `implementacja-lukaszka`, bo harness-cockpit jeszcze nie działa.
- **Warunek przełączenia:** M1 i M2 działają.
- **Po przełączeniu:** harness-cockpit budujemy przy pomocy harness-cockpit —
  kolejne kamienie milowe idą przez workflow składane i uruchamiane w kokpicie.

## Uzasadnienie

Z BRO-95:

> Punkt przełączenia jest wyraźny: gdy M1 i M2 działają, zaczynasz budować
> harness-cockpit przy pomocy harness-cockpit. To jednocześnie najlepszy test
> produktu dostępny bez obcych użytkowników — jeśli nie da się nim wygodnie
> budować jego samego, nie da się nim budować niczego.

## Moment przełączenia

| Pole | Wartość |
| -- | -- |
| Data faktycznego przełączenia | do uzupełnienia |
| Pierwszy ticket zbudowany kokpitem | do uzupełnienia |
| Kto potwierdził, że M1 i M2 działają | do uzupełnienia |

Uzupełnia człowiek w dniu przełączenia — tym samym PR, który odnotowuje pierwszy
run zbudowany kokpitem. Do tego czasu tabela zostaje z „do uzupełnienia”;
NEVER: nie wpisuj daty planowanej jako faktycznej.

## Źródło

BRO-95, sekcja „Bootstrap: czym budować, zanim narzędzie istnieje”; kryterium
akceptacji „Zapisany moment przełączenia na budowanie sobą”.
