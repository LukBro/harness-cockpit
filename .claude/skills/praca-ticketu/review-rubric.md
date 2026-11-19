# Rubryka oceny krytyka

Ocena NIE jest wyczuciem. Start 100 punktow, twarde odjecia z tabeli ponizej.
Krytyk MUSI wypisac kazde odjecie z lokalizacja `plik:linia` (wady w kodzie)
albo ze wskazaniem zrodla - raportu/kryterium (wady poza repo, np. brak wpisu
wiki). Ocena bez uzasadnienia jest niewazna.

Prog zaliczenia: **82**. Odjecia ida co 5, wiec prog lezy celowo MIEDZY skokami -
decyzja jest zawsze jednoznaczna (85 zalicza, 80 nie).

---

## Tabela odjec

| Naruszenie | Odjecie | Uwagi |
|---|---|---|
| Naruszenie reguly z `.claude/rules/*` | -25 za regule | reguly czytaj na starcie review |
| Brak testu dla nowego kodu | -25 | tylko gdy projekt ma testy - wg toolchainu z Twojego promptu, patrz zasady projektu ponizej (pkt 4) |
| Kryterium akceptacji niezrealizowane | -15 za kryterium | liste kryteriow masz w prompcie; zrealizowane = widoczne w git diff LUB potwierdzone w raporcie developera albo Team Managera (kryteria poza repo: wiki, konfiguracja, "testy przechodza"). Stan odhaczen w systemie IGNORUJ (developerzy odhaczaja swoje, resztki domyka Team Manager po review) |
| Brak obslugi bledu na granicy systemu | -10 | user input, zewnetrzne API, storage |
| Zmiana pliku poza przydzialem agenta | UWAGA, 0 pkt | przydzial masz w prompcie: lista zespolu z zakresem per agent (kilku developerow) ALBO `ZAKRES PRACY DEV` jednego developera - uzyj tego, ktore dostales. Wykryj przez `git diff --stat <BASELINE_SHA>` + `git status --porcelain` (nowe pliki sa untracked). Nie da sie przypisac agentowi -> NIE odejmuj nikomu, odnotuj jako naruszenie w raporcie - decyzje podejmie Team Manager. Wpisz jako `UWAGA (plik poza przydzialem, bez blokady)` - nie obniza oceny ani nie blokuje APPROVED (audyt procesu 2026-11-19: formalnosci raportu to 15 z 48 odjec i 4 rundy bez wady w kodzie) |
| Naruszenie STOP CONDITIONS | UWAGA, 0 pkt | liste STOP CONDITIONS masz w prompcie (kontrakt autonomii). Agent zrobil cos z tej listy (np. migracja, nowa zaleznosc, plik z "Nie ruszac") BEZ zgloszenia `STOP:` w raporcie -> wpisz `UWAGA (STOP bez zgloszenia, bez blokady)`; Team Manager decyduje, czy zmiana wchodzi. Zglosil i dostal zgode Team Managera -> bez uwagi. Jedna operacja pasujaca tez do "plik poza przydzialem" -> jedna uwaga |
| Over-engineering | -10 | abstrakcja bez 3 realnych uzyc, przedwczesny feature flag |
| Niezgodnosc z konwencja sasiedniego pliku | -5 | nazewnictwo, gestosc komentarzy, idiom |
| Komentarz opisujacy CO robi kod | -5 | komentarze tylko o WHY |
| Test przechodzi na mutancie chronionego kodu (usunięty ORDER BY/filtr, odwrócona wartość domyślna, asercja na echo wejścia) | -25 | krytyk podaje mutanta, który przeżył; patrz `.claude/rules/test-odroznia-regule.md` |


### Wiersze TYLKO gdy TRYB = defekt

| Naruszenie | Odjecie | Uwagi |
|---|---|---|
| Fix bez nazwanego mechanizmu | -20 | raport developera nie wskazuje przyczyny jako plik:linia + jedno zdanie "dlaczego to daje ten symptom", albo fix jest w innym miejscu niz dowod z researchu (o ile research byl i masz go w prompcie) bez wyjasnienia. Symptom zniknal != przyczyna usunieta |
| Brak testu regresyjnego dla defektu | -25 | test ma przechodzic przez sciezke kodu wskazana jako przyczyna, nie tylko sprawdzac symptom. W trybie defektu ZASTEPUJE ogolny wiersz "brak testu dla nowego kodu" - nie sumuj obu. Projekt bez testow -> zamiast testu wymagane sa zapisane kroki reprodukcji wykonane przed i po fixie (raport Team Managera) |
| Klasa bledu niesprawdzona | -5 | research w prompcie (raport Researchera albo mini-research Team Managera) wskazal inne wystapienia tego samego wzorca, developer ich nie sprawdzil ani nie odnotowal, ze sa poza zakresem. Zadnego researchu w prompcie -> pomin ten wiersz |

W trybie `zmiana` te trzy wiersze pomijasz w calosci. TRYB masz w prompcie.

Odjecia sumuja sie MIEDZY roznymi wadami. Jedna wada punktowana RAZ: gdy pasuje
do kilku wierszy (np. przezywajacy mutant to takze naruszenie reguly
`.claude/rules/test-odroznia-regule.md`), zastosuj tylko najwyzsze odjecie. Ocena moze spasc ponizej zera - wtedy raportuj `0/100`.

Lintu i testow NIE punktujesz - to osobny gate przed review, dostajesz kod PO
zielonym wyniku. Wynik "pominiete za zgoda uzytkownika" -> ocen normalnie i zaznacz
pominiecie w raporcie. Wynik czerwony (nie powinien do Ciebie trafic) ->
przerwij review i zwroc NEEDS WORK bez punktacji.

---

## Blokada zewnetrzna

Znacznik `BLOKADA-ZEWNĘTRZNA` dopisujesz do odjecia, ktorego zaden agent nie
usunie z poziomu repo i swoich narzedzi. Trzy klasy:

- **srodowisko** lokalne - brak narzedzia, uslugi albo danych na maszynie
  (np. brak runnera, bazy, pliku spoza repo),
- **uprawnienia** - odmowa klasyfikatora uprawnien albo hooka, brak dostepu
  do systemu zewnetrznego,
- **decyzja czlowieka** - wybor wariantu, sekret albo token, zmiana poza
  kontraktem ticketu.

Przy znaczniku obowiazkowy jest jednozdaniowy powod, ktory wskazuje zrodlo
blokady (czego brakuje i kto to dostarcza). Znacznik bez powodu jest niewazny,
odjecie traktujesz jak zwykle.

Odjecie z blokada liczy sie do score jak kazde inne: prog 82 bez zmian,
znacznik NIE podnosi oceny. Mowi tylko, ze kolejna iteracja developera nie
zmieni wyniku. Nie oznaczasz nim wady, ktora agent moze naprawic
w przydzielonych plikach - ani brakujacego testu, ktory da sie napisac bez
zewnetrznej uslugi. Format zapisu - sekcja "Format odpowiedzi".

---

## Zasady projektu - skad je brac

Trwale reguly projektu zyja W REPOZYTORIUM, jako zwykle pliki Markdown -
nie w prywatnej pamieci ktoregokolwiek klienta. Czyta je kazdy klient
wykonujacy te instrukcje, wersjonuje je git, a nazwa katalogu `.claude/` jest
HISTORYCZNA (katalog jest wspolny dla klientow, nie zastrzezony dla jednego).
Sciezki liczysz od KORZENIA REPOZYTORIUM.

Przed ocena przeczytaj:

1. `.claude/rules/*.md` w korzeniu repo (jesli istnieje) - twarde reguly projektowe,
   kazde naruszenie to -25
2. `.claude/agent-memory/<typ-ocenianego-agenta>/MEMORY.md`, sekcja
   `## Reguly z review krytyka` (jesli istnieje) - reguly z poprzednich review
   tej roli. Regula stamtad zlamana ponownie to ta sama kategoria co `.claude/rules/`
   (-25) i zaznacz w raporcie, ze korekta WRACA - to materia dla `/monolynx:retro`
3. `CLAUDE.md` / `AGENTS.md` - konwencje architektoniczne
4. Toolchain projektu z Twojego promptu - sekcja "Toolchain" raportu Researchera
   ALBO tresc strony wiki `toolchain` wklejona wprost przez Team Managera
   (dostajesz jedno z dwoch, zaleznie od trybu pracy; research bywa opcjonalny) -
   czy projekt ma testy, jakim frameworkiem. NIE pobieraj strony wiki sam.
   Nie masz zadnego z tych zrodel -> nie zakladaj, ze projekt ma testy: pomin
   wiersz "brak testu dla nowego kodu" i odnotuj ten brak w raporcie

Nie zakladaj regul, ktorych nie ma w tych zrodlach. Jesli plik regul nie istnieje -
pomijasz kategorie "-25 za regule".

---

## Format odpowiedzi

Sztywny, jedna linia na agenta:

```
**Code Review**

<agent-1>: 100 -25 (services/runs.py:142 odczyt statusu i zapis bez blokady wiersza) -25 (brak testu dla zakoncz_run) = 50/100
  UWAGA (tools/x.py poza przydzialem, bez blokady)
  -> NEEDS WORK: warunek w UPDATE ... WHERE status, dopisz test dwoch przeplecionych zapisow
  REGULA: Odczyt stanu i zapis zalezny od niego ida w jednej transakcji z blokada albo warunkiem w zapisie.

<agent-2>: 100 -5 (templates/board.html:88 niezgodnosc z konwencja sasiednich plikow) = 95/100
  -> APPROVED

Ogolna ocena: 50/100 (minimum z agentow)
Status: NEEDS WORK (agent-1 ponizej progu 82)
```

**Ogolna ocena = MINIMUM z ocen agentow** - decyzja i tak zapada per agent
(kazdy musi miec >= 82), minimum pokazuje najslabsze ogniwo. Oceniasz jednego
developera -> jedna linia i ogolna ocena rowna jego ocenie; format zostaje ten sam.

Bez odjec -> `100/100, APPROVED`. Nie zanizaj "dla zasady".

**Ponowna ocena po poprawce** (prompt z `ZAMROŻONY ZAKRES`): odejmujesz tylko za
nienaprawione odjecie z poprzedniego werdyktu, wade w kodzie dopisanym albo
zmienionym od poprzedniego diffu i naruszenie przydzialu. Wada w kodzie bez zmian,
ktorej poprzedni werdykt nie wskazal, to linia pod ocena agenta, bez odjecia:

```
<agent-1>: 100 = 100/100
  -> APPROVED
  UWAGA-PO-RUNDZIE: tests/e2e/conftest.py:236 element krotki nie trafia na sciezce produkcyjnej
```

**Odjecie z blokada zewnetrzna** - znacznik i powod w nawiasie odjecia, po
lokalizacji i wadzie, oddzielone srednikiem:

```
<agent-3>: 100 -15 (.gitlab-ci.yml:40 brak joba bump-homebrew; BLOKADA-ZEWNĘTRZNA: job wymaga tokenu Homebrew, ktory ustawia czlowiek w zmiennych CI) = 85/100
  -> APPROVED

<agent-4>: 100 -15 (plugin/formula.rb:12 brak bloku resource; BLOKADA-ZEWNĘTRZNA: sumy SHA wymagaja opublikowanych archiwow, ktorych nie ma przed wydaniem) -10 (docs/install.md:5 niedzialajaca komenda) = 75/100
  -> NEEDS WORK: popraw komende w docs/install.md
  REGULA: Kazda komenda w dokumentacji jest sprawdzona wobec --help narzedzia.
```

**Linia `REGULA:`** - obowiazkowa przy kazdym NEEDS WORK, jedna na agenta:
jedno zdanie w trybie oznajmujacym, ktore developer tego typu ma zapamietac,
zeby ta sama wada nie wrocila (Team Manager DOPISUJE je do
`.claude/agent-memory/<typ>/MEMORY.md` w sekcji `## Reguly z review krytyka` -
zapis addytywny, nigdy nadpisanie pliku, zeby wczesniejsze reguly zostaly
i wrocily w kolejnym przebiegu dowolnego klienta). Regula opisuje WZORZEC, nie ten
konkretny plik ("kazdy zapis konczy sie commitem", nie "dodaj commit
w pipelines.py:142"). Kilka odjec -> regula dla najwyzszego. Odjecia
z `BLOKADA-ZEWNĘTRZNA` nie sa wzorcem bledu developera: regule wyprowadzasz
tylko z odjec bez znacznika, a NEEDS WORK z samymi blokadami nie ma linii
`REGULA:`.

---

## Czego krytyk NIE robi

- **Nie pisze kodu.** Ocenia i opisuje poprawke slownie. Poprawia autor.
- **Nie odpala testow ani lintera.** Wynik dostaje od Team Managera - w prompcie
  albo w sygnale startu review - lint i testy ida PRZED review i nie podlegaja
  punktacji (patrz uwaga pod tabela).
- **Nie ocenia decyzji produktowych** z ticketu. Ocenia realizacje, nie zasadnosc zadania.
- **Nie decyduje o zatrzymaniu pracy.** Znacznik `BLOKADA-ZEWNĘTRZNA` jest
  informacja dla Team Managera; kolejna iteracja albo stop to jego decyzja.
