# Brak dowodu to nie zielony wynik

Źródło: retro sprintu M1.2.1 — HAR-132 (`mutation.yml`), HAR-133 (`tools/mutanty_zmian.py`),
HAR-134 (strażnik bazy w `tests/conftest.py`). Niezmiennik `evidence_required` z CLAUDE.md.

## Reguła

Każda ścieżka, która kończy się wynikiem „czysto / nic nie znaleziono / przeszło”,
musi odróżniać **„sprawdziłem i jest czysto”** od **„nie sprawdziłem”**. Brak dowodu
(pusty wynik komendy, zero zbadanych elementów, wyjątek, brak pliku) jest błędem,
nigdy sukcesem. Dotyczy to w szczególności:

- **CI**: `continue-on-error`, `|| true` i `exit 0` wolno stosować tylko dla jawnie
  nazwanej przyczyny (np. brak narzędzia na runnerze), nie dla całego kroku;
  narzędzie zepsute (zero zabitych mutantów, zero zebranych testów) oblewa krok,
- **wyjścia narzędzi**: wynik parsowany z innego programu (`git diff`, raport testów)
  nie zależy od konfiguracji użytkownika, która zmienia format (np. `diff.noprefix`,
  `diff.mnemonicPrefix` — wymuś format flagami), a niezrozumiany wynik to błąd,
- **strażnicy i filtry**: gałąź „nie umiem ocenić” (`except …: return False`,
  ścieżka spoza `rootdir`) nie przepuszcza elementu jako poprawnego.

## Pułapka

Fałszywie zielony wynik wygląda jak sukces i nikt go nie ogląda: CI mutacji było
zielone przez cały sprint, mierząc 0 z 2366 mutantów (HAR-132), a skrypt mutantów
zmian zgłaszałby „brak zmienionych funkcji” na maszynie z `diff.noprefix` (HAR-133).

## Co sprawdzić w review

- Ścieżka kończąca się sukcesem bez dowodu, że coś zostało sprawdzone = **-25**.
- Każda gałąź „brak dowodu → błąd” ma test, w którym tylko ona decyduje o wyniku
  (zob. [test-odroznia-regule](test-odroznia-regule.md)).
