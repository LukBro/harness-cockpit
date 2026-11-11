# Brak dowodu to nie zielony wynik

Każda ścieżka kończąca się wynikiem „czysto / nic nie znaleziono / przeszło” odróżnia
**„sprawdziłem i jest czysto”** od **„nie sprawdziłem”**. Pusty wynik komendy, zero zbadanych
elementów, wyjątek albo brak pliku to błąd, nigdy sukces (niezmiennik `evidence_required`).
WHY: fałszywa zieleń wygląda jak sukces i nikt jej nie ogląda — CI mutacji było zielone przez
cały sprint, mierząc 0 z 2366 mutantów (HAR-132).

## Kształty

- **CI i skrypty**: `continue-on-error`, `|| true`, `exit 0` tylko dla nazwanej przyczyny
  i konkretnego kodu, nie całego wywołania. Narzędzie zepsute (zero zabitych mutantów, zero zebranych
  testów, zero zbadanych elementów) oblewa krok (HAR-132). Cache przywracany po prefiksie klucza (`restore-keys`)
  daje wynik ze starego stanu — klucz obejmuje wszystko, od czego zależy werdykt.
- **Wyjście innego programu**: wymuś format flagami (`diff.noprefix` zmienia `git diff`).
  Kod wyjścia czytaj razem z danymi — u gita kod 1 to „różni się” albo „błąd”, kod 127 z `ps`
  to „brak narzędzia”, nie „proces nie żyje”. Strażnik, który nie umie ocenić stanu, traktuje go
  jak zajęty, nigdy jak zgodę na akcję destrukcyjną.
- **Parsery i filtry**: element wyglądający na objęty regułą, ale niezgodny z formatem
  (`## L-12` zamiast `## L-012`), to błąd walidatora; gałąź „nie umiem ocenić” (`except …: return False`,
  ścieżka spoza `rootdir`) nie przepuszcza elementu jako poprawnego.
- **Zasięg zewnętrznego narzędzia**: liczony mechanizmem narzędzia (`PragmaVisitor` mutmuta),
  nie własnym parserem; czego narzędzie nie sprawdza, idzie do „sprawdź ręcznie”, nie do kodu 0.
- **Zdarzenie zamykające**: `run_finished` dopiero po potwierdzeniu, że zdarzenie kroku, z którego
  kokpit liczy wynik, zostało przyjęte.
- **Kryterium akceptacji ma wynik, przy którym zawodzi** — „zmierzone”, „poniżej 5% albo komentarz”
  nie mogą zawieść. Wypchnięcie sprawdza `git reflog show origin/<branch>`, nie reflog HEAD,
  który wypchnięcia nie rejestruje (BRO-108).

## Co sprawdzić w review

- Ścieżka kończąca się sukcesem bez dowodu, że coś zostało sprawdzone, albo dowolny kształt
  z listy wyżej = **-25**. Każda gałąź „brak dowodu → błąd” ma test, w którym tylko ona decyduje
  (zob. [test-odroznia-regule](test-odroznia-regule.md)).
- Odhaczone kryterium bez wskazania wyniku, przy którym by zawiodło = **-25**.
- Kryterium o wypchnięciu sprawdzane przez `git reflog` HEAD = **-25**.
- Brak ostrzeżenia, pominięta bramka albo pusty plik dowodu raportowane jako zielone — jedno
  odjęcie **-25**, nie sumuje się z punktami o kryterium (jedna wada = jedno odjęcie).
