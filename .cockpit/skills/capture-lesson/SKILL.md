---
id: capture-lesson
name: Dopisz lekcję po runie z realną pułapką
role: author
---
# Dopisz lekcję po runie z realną pułapką

Dopisz wpis do `.ai/lessons.md` TYLKO wtedy, gdy w tym runie wydarzyła się realna
pułapka: coś, na co ktoś wpadł i wpadnie znowu. Nie dopisuj po każdym runie. Jeśli
pułapki nie było, nie zmieniaj pliku i napisz to w odpowiedzi jednym zdaniem.

## Przeczytaj przed pracą

- `.ai/lessons.md` — format wpisu i istniejące wpisy (nie powtarzaj lekcji, która
  już tam jest),
- `CLAUDE.md` w korzeniu, sekcja „Pierwszeństwo warstw”.

## Format wpisu

Wpis ma dokładnie taki kształt jak w `.ai/lessons.md`:

```markdown
## L-NNN — tytuł
zakres: krok=… · ścieżki=… · od: RRRR-MM-DD
objaw: …
przyczyna: …
zamiast: …
```

- `L-NNN` to kolejny numer trzycyfrowy po ostatnim wpisie, a między numerem
  i tytułem stoi półpauza `—`.
- Pierwsza linia pod nagłówkiem to `zakres:`; pola `krok=`, `ścieżki=` i `od:`
  oddziela ` · ` (kropka środkowa ze spacjami).
- `objaw:`, `przyczyna:`, `zamiast:` — po jednym zdaniu.

Wpis bez `zakres:` jest niedopuszczalny. Zakres ma być tak wąski, jak pułapka:
krok i ścieżki, których naprawdę dotyczy.

## Granice

Dopisuj na końcu sekcji „Wpisy”. Nie zmieniaj ani nie usuwaj istniejących wpisów.

Lekcja nigdy nie nadpisuje warstw wyższych: niezmienników silnika,
`.cockpit/project.yaml` ani `CLAUDE.md`. Lekcja z nimi sprzeczna jest do
poprawki, nie do zapisania. Nie commitujesz i nie pushujesz; zmiana zostaje w
kopii roboczej do odbioru przez człowieka.
