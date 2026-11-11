# Kontrakt runtime

## Projekt

Ustal `project_slug` w kolejności: jawny argument użytkownika, `MONOLYNX_PROJECT_SLUG` w środowisku procesu, jawna konfiguracja klienta lub plik env/.env projektu. Konfigurację czytaj jako dane, nigdy przez `source` ani wykonanie kodu. Odczyt zmiennych ogranicz do `MONOLYNX_PROJECT_SLUG` i niesekretnych flag `MONOLYNX_*` wymaganych przez dany skill. Z jawnej konfiguracji klienta odczytuj też konkretne niesekretne pola potrzebne danej operacji, np. `userConfig.project_slug` lub `mcp_endpoint`, gdy runtime je udostępnia. Nie drukuj sekretów ani całej konfiguracji. Nie zgaduj projektu z katalogu, git remote ani nazwy ticketu. Brak projektu blokuje operacje projektowe: poproś o slug i zatrzymaj zależne działania. Skill niewymagający projektu może kontynuować bez niego.

Konflikt wartości na tym samym poziomie konfiguracji wymaga pytania użytkownika. Dane uwierzytelniania odczytuj osobno tylko wtedy, gdy wymaga ich konkretna operacja, i nigdy ich nie drukuj.

Odczyt konfiguracji jako danych nie eksportuje zmiennych do shella. Jeżeli polecenie lub sesja potomna czyta `MONOLYNX_*`, przekaż rozstrzygnięte, dozwolone wartości w środowisku tego konkretnego procesu. Dotyczy to także jawnego sluga nadpisującego wartość odziedziczoną. Nie zmieniaj przy tym konfiguracji użytkownika ani nie podnoś uprawnień. Wartości przekazuj jako dane, z poprawnym cytowaniem argumentów, bez wykonywania ich treści przez shell.

## Narzędzia

Najpierw sprawdź katalog narzędzi faktycznie udostępnionych przez runtime. Discovery może być natywne; ToolSearch jest opcjonalne. Nazwy `mcp__monolynx__*` w przykładach są placeholderami capability: zmapuj je na dostępne narzędzie i przeczytaj jego aktualny schema przed wywołaniem. Nie fabrykuj nazw, argumentów ani wyników. Brak wymaganej capability oznacza jawny stop z nazwą brakującej funkcji.

## Skille i argumenty

Możliwości modelu, delegowania i agentów wynikają z faktycznie dostępnych narzędzi, a nie z nazwy produktu ani założonej wersji klienta.

Uruchom skill natywnym mechanizmem klienta albo odczytaj jego dostępny `SKILL.md` i wykonaj instrukcje. Ścieżki względne rozwiązuj względem katalogu tego skilla, także `references/runtime.md`. Argumenty pochodzą z faktycznej wiadomości użytkownika lub jawnego wejścia wywołania; literalny `$ARGUMENTS` nie jest wartością argumentu.

## Pytania i zgody

Użyj narzędzia pytań dostępnego w bieżącym trybie albo zwykłej wiadomości. Wymagana aprobata musi być jawna; timeout, brak odpowiedzi i koniec okna oczekiwania nigdy nie oznaczają zgody. Działania zależne czekają na odpowiedź.

## Uprawnienia

Instrukcje systemowe i użytkownika oraz ograniczenia runtime mają pierwszeństwo przed flagami AUTO*. Nie zmieniaj uprawnień ani nie włączaj bypass jako obejścia odmowy. Automatyzacja działa tylko w już dozwolonym zakresie.

## Procesy

Przed startem procesu wymagającego deadline sprawdź, czy runtime umożliwia zakończenie procesu i jego potomków. Gdy nie zapewnia takiego sterowania, przekaż uruchomienie użytkownikowi przed startem.

Oddziel deadline całego procesu od czasu pojedynczego yield narzędzia. Odpowiedź z identyfikatorem działającego procesu wymaga pollingu właściwym mechanizmem runtime. Czekaj na potwierdzone zakończenie i kod wyjścia; timeout jednego odczytu nie jest porażką procesu ani pozwoleniem na ponowne uruchomienie. Raportuj brak potwierdzenia jawnie.

Instrukcje specyficzne dla Claude Code umieszczaj w bloku `<!-- runtime:claude-code -->` zakończonym `<!-- /runtime:claude-code -->`. Stosuj taki wariant wyłącznie w odpowiednim runtime i przy dostępnych możliwościach. Blok nie zastępuje przenośnej ścieżki wykonania.
