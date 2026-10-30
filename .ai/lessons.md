# Pułapki, na które ktoś już wpadł

Pusty na starcie i to jest w porządku. Dopisuje krok kończący run, gdy wydarzyła
się realna pułapka — nie po każdym runie (krok workflow: HAR-88).

Lekcje to najniższa warstwa instrukcji: NIGDY nie nadpisują niezmienników silnika,
`project.yaml` ani `CLAUDE.md` (kolejność warstw: BRO-96).

## Format wpisu

Każdy wpis ma nagłówek, linię zakresu i trzy pola — po jednym zdaniu:

- `## L-NNN — tytuł` — kolejny numer trzycyfrowy, półpauza `—` między numerem
  a tytułem.
- `zakres: krok=… · ścieżki=… · od: RRRR-MM-DD` — pierwsza linia pod nagłówkiem;
  pola oddziela ` · ` (kropka środkowa ze spacjami).
  - `krok` — nazwa kroku workflow albo fazy, w której pułapka występuje.
  - `ścieżki` — globy plików, których dotyczy (np. `tests/**/*.py`).
  - `od` — data, od której lekcja obowiązuje.
- `objaw:` — co widać, gdy pułapka zadziała.
- `przyczyna:` — dlaczego tak się dzieje.
- `zamiast:` — co robić w zamian.

MUST: każdy wpis niesie `zakres:`. Wpis bez `zakres:` jest odrzucany przez
walidator lekcji (HAR-87).
WHY: lekcja bez zakresu to reguła globalna, a globalnych reguł nie wyciąga się
z jednego zdarzenia — reguła jednej fazy stosowana wszędzie zatruwa cały pipeline
(BRO-96).

## Przykład (nie jest wpisem)

```markdown
## L-000 — xdist w addopts psuje przebieg mutacji
zakres: krok=mutacje · ścieżki=pyproject.toml · od: 2026-01-01
objaw: przebieg mutmut trwa wielokrotnie dłużej albo pada na starcie workerów.
przyczyna: mutmut woła pytest dla każdego mutanta, a `-n 4` w `addopts` startuje
pulę workerów przy każdym wywołaniu.
zamiast: `-n 4` podawaj tylko w poleceniach (CI, skrypt pełnych testów), nigdy
w `addopts`.
```

## Wpisy

<!-- Nowe wpisy dopisuj poniżej, od L-001, najnowszy na końcu. -->
