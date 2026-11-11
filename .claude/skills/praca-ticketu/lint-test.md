# Lint i testy - gate przed review

Procedura wspolna dla flow pracy nad ticketem. Krytyk ocenia kod, ktory sie
kompiluje i przechodzi testy - tani gate przed drogim review. Ticket nie trafia
do `in_review` z czerwonymi testami.

Ten plik opisuje SAMA procedure: skad komendy, wybor wariantu, tryb pelnego
przebiegu (`lokalnie` albo `ci`), zawezanie testow w trybie `ci`, wykonanie,
tryb defektu, decyzje po wyniku i warunek domkniecia. Moment jej uruchomienia,
wysokosc limitu poprawek i miejsca raportowania naleza do skilla, ktory go
czyta.

---

## Skad komendy

Ze strony wiki `toolchain` projektu - z sekcji "Toolchain" raportu Researchera
albo z tresci strony pobranej wprost przez skill.

**Brak strony `toolchain`** - poinformuj uzytkownika:

> Projekt nie ma strony wiki `toolchain`, wiec nie znam komend lint/test.
> Uruchom `/monolynx:project-toolchain` zeby ja utworzyc (jednorazowo).
>
> Kontynuowac bez lintu i testow? (tak / podam komendy recznie)

Poczekaj na odpowiedz. Bez potwierdzenia nie zmieniaj statusu ticketu.

---

## Wybor wariantu: worktree czy glowny checkout

Strona `toolchain` moze miec dwa komplety komend. Zanim cokolwiek uruchomisz,
musisz wiedziec, gdzie pracujesz. Sprawdzasz to NA HOSCIE (kontener nie ma
binarki `git`, a `.git` w worktree to plik ze sciezka hosta) DWIEMA zwyklymi
komendami, porownujac wyniki sam - jednolinijkowiec z
`[ "$(...)" != "$(...)" ]` zostanie odrzucony przez guard izolacji worktree:

```bash
git rev-parse --git-dir
```

```bash
git rev-parse --git-common-dir
```

- **Wyniki rozne** (np. `.../.git/worktrees/MON-166` wobec `.../.git`) -
  pracujesz w git worktree: komendy z sekcji `## Worktree`, pola `lint:`
  i `test:`. Wartosc `jak wyzej` w ktorymkolwiek polu odsyla do `## Lint` /
  `## Test`.
- **Wyniki identyczne** - glowny checkout: komendy z `## Lint` i `## Test`,
  jak dotad.

Skill, ktory ustalil srodowisko wczesniej, tej detekcji nie powtarza - bierze
zapamietana wartosc.

**Podstawienia w komendzie worktree**: sekcja `## Worktree` zapisuje komende
w formie przenosnej, z `"$PWD"`. Przed uruchomieniem podstaw za nia DOSLOWNY
wynik `pwd` (sciezka absolutna). W komendzie `docker`, ktora faktycznie
uruchamiasz, nie moze zostac zaden `$VAR` ani `$(...)` - sesja pracujaca
w worktree odrzuca komendy z wartoscia obliczana w locie. Forma
`sh -c "a && b && c"` z literalna sciezka przechodzi.

**Praca idzie w worktree, a strona nie ma sekcji `## Worktree`** - poinformuj
uzytkownika:

> Strona `toolchain` nie ma sekcji `## Worktree`, a praca idzie w git worktree.
> Komendy z `## Lint` i `## Test` (`make`, `docker compose exec`) sprawdzilyby
> glowny checkout, nie ten kod.
> Uruchom `/monolynx:project-toolchain`, zeby ta sekcje dodac.
>
> Kontynuowac bez lintu i testow? (tak / podam komendy recznie)

Poczekaj na odpowiedz. Bez potwierdzenia nie zmieniaj statusu ticketu. Brak
sekcji `## Worktree` POZA worktree to stan normalny - zadnego komunikatu,
pracujesz komendami z `## Lint` i `## Test`.

---

## Pelny przebieg: lokalnie albo w CI

Pole `pełny przebieg:` w sekcji `## Test` strony `toolchain` (zapis
`pelny przebieg:` liczy sie tak samo, wielkosc liter etykiety nie ma znaczenia)
mowi, gdzie idzie pelny zestaw testow. To samo pole w innej sekcji
(`## Worktree`, `## Uwagi`) jest ignorowane.

| Wartosc | Znaczenie |
|---|---|
| `lokalnie` | pelny zestaw idzie lokalnie, w tej sesji. Wartosc domyslna |
| `ci` | lokalnie ida tylko testy zawezone do zmienionego obszaru, pelny zestaw weryfikuje pipeline merge requesta |

**Ustalenie trybu:**

- **Brak linii pola w `## Test`** -> tryb `lokalnie`, bez ostrzezenia i bez
  wolania skryptu.
- **Linia jest** -> wartosc normalizuje skrypt z katalogu tego skilla. Wartosc
  bedaca jednym slowem z samych liter podajesz wprost:

  ```bash
  python3 <katalog skilla>/scripts/toolchain_fields.py --value <wartosc pola>
  ```

  W kazdym innym przypadku (wartosc pusta, kilka slow, cyfry, znaki
  interpunkcyjne) zapisz tresc strony `toolchain` narzedziem Write do pliku
  roboczego POZA repozytorium i podaj jego sciezke:

  ```bash
  python3 <katalog skilla>/scripts/toolchain_fields.py --file <sciezka do pliku z trescia strony>
  ```

  Dokladnie jedna z dwoch opcji naraz. `<katalog skilla>` i sciezka pliku to
  sciezki absolutne wpisane DOSLOWNIE, jak przy `test_lock.py` - bez `$VAR`, `~`
  i `$(...)`, bez pipe i przekierowan. Calej tresci strony nie podawaj
  w argumencie: zawiera nazwy narzedzi lint i testow, ktore hook ocenia jak
  komende.

**Wynik skryptu**: ostatnia (jedyna) linia stdout to jeden obiekt JSON
z kluczami `pelny_przebieg` (`lokalnie` albo `ci`), `zrodlo` (`pole`,
`domyslna`, `nieznana`) i `ostrzezenia` (lista napisow). Tryb bierzesz
z `pelny_przebieg`.

| Co widzisz | Znaczenie | Co robisz |
|---|---|---|
| kod 0, `zrodlo` = `pole` | wartosc rozpoznana | pracujesz w trybie z `pelny_przebieg` |
| kod 0, `zrodlo` = `domyslna` | plik nie ma sekcji `## Test` albo linii pola | tryb `lokalnie` |
| kod 0, `zrodlo` = `nieznana` | wartosc pusta albo inna niz `lokalnie` / `ci` | tryb `lokalnie`; ostrzezenie przepisujesz uzytkownikowi |
| `toolchain_fields: ostrzezenie: <tresc>` na stderr | to samo ostrzezenie co w `ostrzezenia` (nieznana wartosc, duplikat pola - liczy sie pierwsza linia) | przepisujesz uzytkownikowi, pracujesz dalej |
| kod 2 i `toolchain_fields: <powod>` na stderr, nic na stdout | blad wejscia `--file`: pliku nie ma, to katalog, plik nieczytelny, nie UTF-8 albo pusty | tryb `lokalnie`; komunikat przepisujesz uzytkownikowi |
| kod 64, nic na stdout | blad uzycia: brak opcji, obie naraz, nieznana flaga | popraw komende i uruchom ponownie; nie da sie -> tryb `lokalnie` |

Wartosc nieznana i blad skryptu daja `lokalnie`, nigdy `ci`: pelny przebieg
lokalny kosztuje czas, a pominiety pelny przebieg bylby niewidoczny.

Skill, ktory ustalil tryb wczesniej, tego nie powtarza - bierze zapamietana
wartosc.

**Tryb `lokalnie`**: sekcje tego pliku o trybie `ci` pomijasz w calosci.
Procedura jest taka sama jak bez pola.

### Tryb `ci`: czy merge request jest mozliwy

Pelny zestaw weryfikuje pipeline merge requesta, wiec tryb `ci` ma sens tylko
wtedy, gdy merge request powstanie. Sprawdzasz to ZARAZ po ustaleniu trybu,
przed testami i przed review. Wymagane jednoczesnie:

- `MONOLYNX_AUTOCOMMIT=true`, `MONOLYNX_AUTOPUSH=true` i `MONOLYNX_AUTOMR=true`
  (z linii konfiguracji skilla),
- dzialajace `glab`: komenda `glab --version` konczy sie kodem 0.

Ktorys warunek niespelniony:

- **Sesja w tle** (`MONOLYNX_SPRINT_RUN=1`) - koniec tury wg reguly trybu sesji
  w tle skilla, ktory czyta ten plik, z powodem `pełny przebieg ci bez MR`.
  Ostatnia linia odpowiedzi: `SPRINT-RUN STOP: pełny przebieg ci bez MR`.
- **Tryb interaktywny** - jedno pytanie do uzytkownika:

  > Strona `toolchain` ma `pełny przebieg: ci`, ale ta sesja nie zalozy merge
  > requesta ([ktorego warunku brakuje]), wiec pelnego zestawu testow nikt by
  > nie uruchomil.
  >
  > (a) wracam do lokalnego pelnego przebiegu w tej sesji
  > (b) zatrzymuje sie

  (a) -> do konca sesji pracujesz jak w trybie `lokalnie`. (b) -> zatrzymaj
  sie; statusu ticketu nie zmieniaj.

Tryb `ci` NIGDY nie konczy sie po cichu bez pelnego przebiegu: albo pipeline
merge requesta, albo lokalny pelny przebieg, albo zatrzymanie.

### Tryb `ci`: testy zawezone do zmienionego obszaru

W trybie `ci` komendy testow z pelnym zestawem NIE uruchamiasz lokalnie.
Uruchamiasz te sama komende `test:` wybranego wariantu ze sciezkami testow
zmienionego obszaru. Lint idzie bez zmian, w calosci.

**Wejscie** - lista plikow zmienionych wzgledem `BASELINE_SHA` (SHA zapamietany
przez skill na starcie pracy, wpisany doslownie), z dwoch osobnych komend:

```bash
git diff --name-only <BASELINE_SHA>
```

```bash
git status --porcelain --untracked-files=all
```

Z drugiej bierzesz nowe pliki (linie `??`), ktorych pierwsza nie pokazuje.
`--untracked-files=all` jest konieczne: bez niego nowy katalog przychodzi jako
jedna linia `?? katalog/`, bez plikow w srodku. Pliki usuniete pomijasz.

**Wyjscie** - posortowana lista sciezek testow bez powtorzen, zlozona z dwoch
grup:

1. zmienione i nowe pliki testow z listy wejsciowej,
2. dla kazdego zmienionego pliku zrodlowego: pliki testow, ktore mu
   odpowiadaja wg konwencji projektu (framework z pola `framework:`, katalog
   testow z domyslnej sciezki w komendzie `test:`; np. dla `src/pkg/foo.py`
   pliki `test_foo.py` w katalogu testow). Bierzesz tylko pliki, ktore
   ISTNIEJA - sprawdzasz narzedziem do wyszukiwania plikow, nie zgadujesz
   nazwy.

**Podstawienie** - sciezki z listy wpisujesz w komende `test:` W MIEJSCE
domyslnej sciezki zestawu (np. `tests/unit`), nie obok niej. Komenda bez
sciezki zestawu, ktorej runner przyjmuje sciezki jako argumenty, dostaje je na
koncu.

Trzy wyniki, ktorych nie wolno pomylic:

| Sytuacja | Co to jest | Co robisz |
|---|---|---|
| lista niepusta, runner przyjmuje sciezki | zwykly przebieg zawezony | uruchamiasz go wg sekcji "Wykonanie" |
| lista PUSTA: zaden zmieniony plik nie jest testem i nie ma pasujacego testu (np. sama dokumentacja) | brak testow do uruchomienia - to NIE jest blad | lokalnie idzie tylko lint; zapisz "testy zawezone: brak pasujacych testow" do raportu |
| lista niepusta, ale komenda `test:` nie daje sie zawezic (np. `make test` albo skrypt ze stalym zestawem, bez miejsca na sciezki) | blokada | postepowanie nizej |

Komenda nie daje sie zawezic:

- **Sesja w tle** (`MONOLYNX_SPRINT_RUN=1`) - koniec tury wg reguly trybu sesji
  w tle skilla, ktory czyta ten plik, z powodem
  `pełny przebieg ci - testów nie da się zawęzić`. Ostatnia linia odpowiedzi:
  `SPRINT-RUN STOP: pełny przebieg ci - testów nie da się zawęzić`.
- **Tryb interaktywny** - to samo pytanie co przy braku merge requesta:
  (a) lokalny pelny przebieg w tej sesji, (b) zatrzymanie.

Przebiegi zawezone ida przez blokade i podlegaja wylacznikowi
`izolacja testów: tak` tak samo jak pelne (sekcja "Wykonanie").

---

## Wykonanie

Uruchamiasz komendy WYBRANEGO wariantu. W trybie `ci` komenda testow to komenda
zawezona z sekcji "Tryb `ci`: testy zawezone do zmienionego obszaru"; komenda
z pelnym zestawem nie idzie lokalnie w ogole.

**`MONOLYNX_AUTOTEST=false`** (domyslnie) - wypisz komendy i poczekaj na wynik:

> Uruchom i wklej wynik:
> ```
> [komenda lint wybranego wariantu]
> python3 [katalog skilla]/scripts/test_lock.py -- [komenda test wybranego wariantu]
> ```

**`MONOLYNX_AUTOTEST=true`** - odpal komendy sam.

Wariant worktree wymaga podniesionego stacku glownego checkoutu - siec, obraz
i baza pochodza stamtad. Stack nie stoi -> to brakujace srodowisko, nie blad
kodu: podnies go albo powiedz o tym uzytkownikowi zamiast raportowac czerwony
wynik.

### Testy ida przez blokade

Rownolegle sesje w kilku worktree dziela jedno srodowisko testow (baza, porty,
kontenery glownego checkoutu). KAZDA komende testow - pelny przebieg, przebieg
zawezony, oba przebiegi trybu defektu i pelny przebieg po ostatniej zmianie -
uruchamiasz przez skrypt z katalogu tego skilla:

```bash
python3 <katalog skilla>/scripts/test_lock.py -- <komenda testow>
```

Skrypt bierze blokade na pliku `<git-common-dir>/monolynx/test.lock`, wspolnym
dla glownego checkoutu i wszystkich worktree, i dopiero potem uruchamia komende.
Zajeta blokada to kolejka: skrypt czeka, az poprzedni przebieg sie skonczy.
Lint NIE idzie przez blokade.

- **`<katalog skilla>`** to absolutna sciezka wpisana DOSLOWNIE - bez `$VAR`,
  `~` i `$(...)`. To samo ograniczenie co `"$PWD"` w komendzie worktree: sesja
  w worktree odrzuca wartosci liczone w locie.
- **Komenda po `--` jest uruchamiana bez powloki** (jako lista argumentow),
  wiec nie moze zawierac skladni powloki: `|`, `;`, `&&`, przekierowan,
  zmiennych. Komenda testow w formie `docker compose ... run ... pytest ...`
  to spelnia. Komenda, ktora powloki wymaga, dostaje jawne `sh -c "..."` po
  `--`.
- **Hook ocenia owinieta komende tak samo jak nieowinieta**: prosta forma
  wrappera jest rozpakowywana i podlega regulom samej komendy testow (pytanie
  o zgode bez `MONOLYNX_AUTOTEST=true`). Wrapper niczego nie odblokowuje. Przy
  skladni powloki w komendzie (takze w `sh -c "a && b"`) hook wrappera nie
  rozpakowuje i ocenia caly napis starymi regulami.
- **`--timeout <s>`** (liczba calkowita 0..86400, czyli do 24 h, przed `--`;
  wartosc spoza zakresu to kod 64) zmienia limit czekania
  na blokade; domyslnie 3600 s (60 min), `0` to jedna proba bez czekania.
  Bez powodu go nie podajesz.
- **`MONOLYNX_AUTOTEST=false`** - uzytkownikowi wypisujesz komende testow JUZ
  owinieta, jak w bloku wyzej.

**Wylacznik**: pole `izolacja testów: tak` w sekcji `## Test` strony `toolchain`
(zapis `izolacja testow: tak` liczy sie tak samo) oznacza, ze kazdy przebieg ma
wlasne srodowisko - testy ida wtedy BEZ blokady, sama komenda. Brak pola albo
kazda inna wartosc = `brak` = blokada wlaczona.

### Linie `TEST-LOCK:` i kody wyjscia

Skrypt pisze na stderr linie `TEST-LOCK:` i zwraca kod komendy testow.

| Co widzisz | Znaczenie | Co robisz |
|---|---|---|
| `TEST-LOCK: acquired` | blokada zdobyta, komenda ruszyla | czytasz wynik testow jak zwykle |
| `TEST-LOCK: waiting holder=<pid>/<worktree>` albo `holder=unknown` | blokade trzyma inny przebieg, czekasz w kolejce | nic - to nie blad |
| `TEST-LOCK: timeout` i kod 75 | limit czekania minal, komenda NIE zostala uruchomiona | postepowanie nizej |
| `test_lock: ...` i kod 64 | wrapper nie mogl ruszyc, komenda NIE zostala uruchomiona: blad skladni (brak `--`, zla flaga, pusta komenda), `--timeout` poza zakresem 0..86400, katalog poza repozytorium git albo blad gita, blad otwarcia pliku blokady | przeczytaj komunikat `test_lock: ...` i zdecyduj wg niego: blad skladni albo zakresu -> popraw komende i uruchom ponownie; git albo plik blokady -> to brakujace srodowisko, poprawianie komendy nie pomoze |

Kod 75 z linia `TEST-LOCK: timeout` to brakujace srodowisko, nie czerwony test:
NIE uruchamiasz poprawki agenta i NIE zuzywasz limitu poprawek.

- **Tryb interaktywny** - powiedz uzytkownikowi, kto trzyma blokade (`holder`
  z linii `waiting`), i zapytaj, czy ponowic.
- **Sesja w tle** (`MONOLYNX_SPRINT_RUN=1`) - koniec tury wg reguly trybu sesji
  w tle skilla, ktory czyta ten plik, z powodem `kolizja testów`. Ostatnia
  linia odpowiedzi: `SPRINT-RUN STOP: kolizja testów`.

Kod 75 albo 64 BEZ linii `TEST-LOCK: timeout` / `test_lock:` to kod samej
komendy testow - traktujesz go jak kazdy inny wynik testow. Pozostale kody
skryptu: 127 (komendy nie ma), 126 (brak uprawnien do jej uruchomienia),
128+N (komenda zabita sygnalem N) - to tez nie sa czerwone testy, tylko
komenda, ktora nie zadzialala.

---

## TRYB = defekt - najpierw czerwony, potem zielony

Developer nie mogl uruchomic testu (hook blokuje testy subagentom), wiec test
regresyjny nigdy nie byl czerwony i sam w sobie niczego nie dowodzi. Zanim
pojdzie pelny przebieg (w trybie `ci`: przebieg zawezony), wykonaj
(AUTOTEST=false -> wypisz uzytkownikowi z prosba o wklejenie OBU wynikow):

```bash
git stash -- <pliki fixu, BEZ pliku testu>
<komenda testu z toolchain zawezona do testu regresyjnego>
git stash pop
<ta sama komenda>
```

Oba przebiegi testu ida przez blokade, jak kazda komenda testow (sekcja
"Wykonanie"); `git stash` i `git stash pop` sa poza nia. Tryb `ci` niczego tu
nie zmienia: oba przebiegi zostaja lokalne, pipeline merge requesta ich nie
zastepuje.

Oczekiwane: pierwszy przebieg CZERWONY, drugi ZIELONY. Test zielony takze bez
fixu -> nie przechodzi przez mechanizm: re-run developera z ta informacja
(konsumuje limit poprawek). Projekt bez testow -> zamiast komendy testu kroki
reprodukcji z raportu developera, przed `stash pop` i po nim. Oba wyniki
doslownie do logu instrumentacji lintu i testow oraz do komentarza
podsumowujacego. Uzyj `git stash --`, nie `git stash push` - hook czyta `push`
jak git push i pyta niepotrzebnie.

---

## Decyzja po wyniku

- **Wszystko zielone** -> dalej, do review krytyka.
- **Cokolwiek czerwone** -> najpierw sprawdz, czy to nie kolizja srodowiska
  (sekcja "Kolizja srodowiska testow") ani timeout blokady (kod 75 z linia
  `TEST-LOCK: timeout`) - oba przypadki maja wlasne postepowanie i nie sa
  czerwonym wynikiem. W kazdym innym razie uruchom poprawke agenta
  odpowiedzialnego za wadliwy obszar z trescia bledu (mechanika re-run wg tego
  flow). Po poprawce powtorz lint i testy. Kazda poprawka konsumuje limit
  poprawek obowiazujacy w tym flow - po jego wyczerpaniu zapytaj uzytkownika.
- **Uzytkownik swiadomie pominal** -> odnotuj w podsumowaniu koncowym jako
  "lint/testy pominiete" i przekaz te informacje krytykowi. Krytyk lintu
  i testow i tak nie punktuje (to gate, nie kategoria rubryki), ale odjecie
  "-20 brak testu dla nowego kodu" STOSUJE nadal - pominiecie URUCHOMIENIA
  testow nie znosi obowiazku ich NAPISANIA.

**Kto poprawia, gdy blad przecina przydzialy kilku agentow**: poprawia agent,
ktorego zmiana bezposrednio wywolala blad (czytaj traceback/komunikat);
niejednoznaczne -> agent z wiekszym udzialem w diffie wadliwego obszaru. Runda
konsumuje licznik TYLKO poprawianego agenta. Flow z jednym developerem ma tu
zawsze jednego adresata.

---

## Kolizja srodowiska testow

Blokada szereguje przebiegi, ktore przez nia ida. Przebieg spoza niej (reczny,
z innego narzedzia, ze starszej wersji skilla) nadal moze wejsc na wspoldzielony
stan: baze, port, katalog.

**Rozpoznanie** - oba warunki naraz:

1. Pelny przebieg konczy sie masowymi `errors` w setupie: bledy fixture,
   polaczenia albo bazy w wielu niezwiazanych ze soba testach. Nie `failed`
   z asercji.
2. Testy zawezone do zmienionego obszaru sa ZIELONE.

To kolizja srodowiska, nie wada kodu: NIE uruchamiasz poprawki agenta i NIE
zuzywasz limitu poprawek. Zawezone testy czerwone -> to zwykly czerwony wynik,
nie kolizja; obowiazuje "Decyzja po wyniku".

W trybie `ci` pelny przebieg lokalnie nie idzie, wiec warunku 1 nie da sie
spelnic i tego rozpoznania nie stosujesz: czerwony przebieg zawezony to zwykly
czerwony wynik. Kolizje pelnego zestawu widzi wtedy pipeline merge requesta.

**Postepowanie** - JEDNA powtorka pelnego przebiegu przez blokade:

- **Zielono** -> dalej, jak po kazdym zielonym przebiegu.
- **Dalej czerwono w ten sam sposob**:
  - sesja w tle (`MONOLYNX_SPRINT_RUN=1`) - koniec tury wg reguly trybu sesji
    w tle skilla, ktory czyta ten plik (komentarz do ticketu, domkniecie
    instrumentacji), z powodem `kolizja testów`. Ostatnia linia odpowiedzi:
    `SPRINT-RUN STOP: kolizja testów`;
  - tryb interaktywny - jedno pytanie do uzytkownika, z opisem objawu
    i propozycja ponowienia, gdy srodowisko bedzie wolne.

NIGDY petla czekania (np. na znikniecie cudzych kontenerow) ani kolejne
powtorki. Kolejke obsluguje blokada, nie ty.

---

## Pelny przebieg po OSTATNIEJ zmianie

Pelny przebieg to wszystkie komendy lint i test WYBRANEGO wariantu, bez
zawezania; testy ida przez blokade (sekcja "Wykonanie"). Zielony wynik z iteracji zawezonej do dotknietego obszaru NIE
wystarcza: jesli od ostatniego PELNEGO przebiegu zmienil sie w repo choc jeden
plik, powtorz pelny przebieg teraz (mechanika jak wyzej; AUTOTEST=false ->
uruchamia uzytkownik i wkleja). Czerwony -> poprawka jak kazda inna, limit
obowiazuje. Dopiero zielony pelny przebieg, po ktorym nikt niczego nie zmienil,
domyka gate.

Bez iteracji poprawkowych warunek spelnia pierwszy pelny przebieg. Zapisz,
ktory przebieg byl ostatni i czy byl pelny. Oba akapity powyzej opisuja tryb
`lokalnie`.

**Tryb `ci`** (sekcja "Pelny przebieg: lokalnie albo w CI"): lokalnie nie ma
pelnego przebiegu testow, wiec warunek brzmi inaczej. Gate domyka zielony lint
w calosci i zielony przebieg ZAWEZONY, po ktorych nikt niczego nie zmienil.
Zmienil sie w repo choc jeden plik -> wyprowadz liste sciezek OD NOWA (zmiana
mogla dodac pliki) i powtorz lint oraz przebieg zawezony. Czerwony -> poprawka
jak kazda inna, limit obowiazuje. Pusta lista sciezek ("brak pasujacych
testow") spelnia warunek samym zielonym lintem.

Pelny zestaw weryfikuje pipeline merge requesta - to on jest dowodem pelnego
przebiegu, a odczytuje go procedura z `commit-mr.md`. Zapisz, ze ostatni
przebieg byl zawezony lokalnie, i liste uruchomionych sciezek; skill dopisuje
do tego pipeline merge requesta, gdy merge request juz istnieje. Ticket
w trybie `ci` nie trafia do `in_review` bez wystawionego merge requesta - ten
warunek egzekwuje skill, ktory czyta ten plik.
