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

## Regresja (retro M1.6)

Źródło: HAR-149 (-25 `|| true` na zapadce, -15 fallback cache), HAR-144 (-25 zasięg mutmuta).

- **`|| true` na wywołaniu, którego niezerowy kod oznacza błąd wejścia** — zepsute wejście
  przechodzi na zielono; nazwana przyczyna musi dotyczyć konkretnego kodu, nie całego
  wywołania (HAR-149, `mutation.yml` zapadka).
- **Cache przywracany po samym prefiksie klucza** (`restore-keys`) przywraca stan z innymi
  testami albo z plikami usuniętymi w PR — wynik bramki pochodzi wtedy ze starego stanu.
  Klucz obejmuje wszystko, od czego zależy werdykt (testy, konfiguracja) (HAR-149).
- **Model zasięgu zewnętrznego narzędzia** liczony według składni, a nie według faktycznych
  reguł pomijania w zainstalowanej wersji (mutmut 3.8.0: dekoratory, `__new__`,
  `# pragma: no mutate`) — kod, którego narzędzie nie sprawdza, idzie do „sprawdź ręcznie”,
  nigdy do kodu 0 (HAR-144).

Co sprawdzić w review: każdy z powyższych kształtów = **-25**.

## Regresja (retro M1.7)

Źródło: 5 ticketów M1.7 z -25 u backend-python (HAR-78, HAR-87, HAR-89, HAR-151, HAR-152 —
ten ostatni w czterech rundach z rzędu, limit poprawek wyczerpany). Nowe kształty:

- **Kod wyjścia o kilku znaczeniach** — u gita kod 1 znaczy i „różni się”, i „błąd”
  (`diff --no-index`), kod 127 z `ps` znaczy „brak narzędzia”, nie „proces nie żyje”. Kod
  sprawdzasz razem z danymi wyjścia: pusty albo niezrozumiany wynik przy kodzie „różni się”
  to błąd, a strażnik, który nie może ocenić stanu, traktuje go jak zajęty, nigdy jak zgodę
  na akcję destrukcyjną (HAR-89, HAR-151).
- **Parser pomija element niezgodny z formatem** — element wyglądający na objęty regułą
  (`## L-12` zamiast `## L-012`, niedomknięty blok kodu) jest błędem walidatora, nie zwykłą
  sekcją przepuszczaną z kodem 0 (HAR-87).
- **Zdarzenie zamykające po utraconym dowodzie** — `run_finished` (werdykt, koniec całości)
  wysyłasz dopiero po potwierdzeniu, że zdarzenie kroku, z którego kokpit wylicza wynik,
  zostało przyjęte; utracony dowód nigdy nie kończy się stanem sukcesu (HAR-78).
- **Model zasięgu zewnętrznego narzędzia** (powtórka z M1.6, HAR-144) — przybliżenie przez
  AST zamiast mechanizmu narzędzia: pragma `end` na końcu linii z kodem, `end` w miejscu,
  którego `PragmaVisitor` nie czyta, nazwy z `NEVER_MUTATE_FUNCTION_NAMES` tylko na najwyższym
  poziomie. Gdy narzędzie ma własny parser, wołasz jego kod (`PragmaVisitor` zainstalowanego
  mutmuta), a nie piszesz drugi (HAR-152).

Co sprawdzić w review: każdy z powyższych kształtów = **-25**.
