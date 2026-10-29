# Praca w sesji w tle bez czekania na człowieka

Źródło: retro sprintu M1.2 — sesje HAR-13, HAR-24, HAR-40 i HAR-43 stały łącznie
kilkadziesiąt godzin na prośbach o zgodę, bo komendy usuwały pliki albo łączyły wiele
operacji w jednym wywołaniu.

## Reguła

- **Nie usuwaj plików ani katalogów** (`rm -rf`, `rm -r`, `shutil.rmtree`) w komendach
  narzędzi. Kopia robocza, mutant albo eksperyment idzie do **nowego** katalogu pod
  katalogiem tymczasowym zadania (`~/.claude/jobs/<id>/tmp/<nazwa>-<n>`) — ten katalog
  sprząta środowisko.
- **Jedna prosta komenda na wywołanie.** Bez pętli łączących `cp`, `sed -i`, `git`
  i testy w jednej linii; złożoną procedurę zapisz jako skrypt w katalogu tymczasowym
  i uruchom go osobno.
- **Bez `sed -i`** na plikach repozytorium — zmiany w kodzie przez edycję pliku;
  `sed -i -E` zostawia artefakty `*-E` (HAR-14).
- **Zapis tylko w swoim worktree** i w katalogu tymczasowym zadania — nie w pamięci
  sesji głównej ani w katalogach użytkownika.

## Pułapka

W trybie `auto` każda z tych operacji kończy się prośbą o zgodę. Sesja w tle nie ma
okna, więc stoi do rana, a pętla sprintu nie startuje nowych ticketów, gdy dwie sesje
czekają na człowieka (`MONOLYNX_SPRINT_MAX_WAITING`).

## Co sprawdzić w review

- Raport agenta wymienia komendę z `rm -rf` albo `sed -i` na pliku repozytorium = **-10**.
- Artefakty narzędzi (`*-E`, `*.orig`, katalogi `mut*`) w diffie = **-25**.

## Reguły z review — komentarz ticketu, nie `.claude/` (retro M1.3)

Źródło: HAR-35, HAR-38 — sesje stały na prośbie o zgodę przy zapisie do
`.claude/agent-memory/*/MEMORY.md`; HAR-35 zostawił niezacommitowaną zmianę tego pliku.

- Sesja w tle **nie edytuje plików w `.claude/`** (reguły, pamięć agentów, ustawienia).
  W trybie `auto` każdy taki zapis czeka na człowieka.
- Regułę z review zapisuje w komentarzu podsumowania ticketu jako linię `REGULA: …` —
  retro zbiera ją stamtąd i przenosi do `.claude/rules/` przez osobny PR.
- Komendy bez `cd … &&` i bez przeszukiwania cudzych worktree — każda taka komenda
  także czeka na zgodę (HAR-45, HAR-52).
