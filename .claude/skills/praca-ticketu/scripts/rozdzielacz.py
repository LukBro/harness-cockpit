#!/usr/bin/env python3
"""Rozdzielacz bramki testow (2026-11-24 wlasciciel: wolne zasoby i praca do zrobienia -> praca idzie tam).

Przed kazdym przebiegiem mierzy obciazenie na rdzen i wolna pamiec maszyny sesji oraz maszyn z
<git-common-dir>/monolynx/pula.json. Komenda idzie na najluzniejsza zdrowa maszyne: zdalna dostaje
kopie kodu (jak pula mutantow) i biegnie w kontenerze, lokalna przez test_lock.py (ten sam semafor).
Zdalna tylko, gdy jest luzniejsza o co najmniej ROZNICA na rdzen i ma >= 4 GB wolnej pamieci.

Awaria maszyny zdalnej (pomiar, ssh, przygotowanie kopii) nie jest wynikiem testow: komenda biegnie
lokalnie, a linia `ROZDZIELACZ ZAWIODL: ...` trafia do <git-common-dir>/monolynx/rozdzielacz.log,
z ktorego watcher robi alarm (zadnego cichego przejscia).

Wywolanie: rozdzielacz.py [--sloty N] [--timeout S] -- <komenda...>
Kod wyjscia: kod komendy; 64 - blad uzycia albo pula.json.
Ograniczenie: komenda zwracajaca sama 97 albo 255 na maszynie zdalnej jest brana za awarie maszyny.
"""
from __future__ import annotations

import io
import os
import shlex
import subprocess
import sys
import tarfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pula_mutantow  # noqa: E402

ROZNICA = 0.25
MIN_PAMIEC_GB = 4.0
KOD_INFRA = 97  # przygotowanie kopii na maszynie zdalnej nie wyszlo
KOD_SSH = 255
LIMIT_S = 9000
TEST_LOCK = Path(__file__).with_name("test_lock.py")


class BladMaszyny(Exception):
    pass


def _git_common() -> Path:
    r = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise pula_mutantow.BladPuli(f"nie repozytorium git: {r.stderr.strip()[:200]}")
    return Path(r.stdout.strip())


def wczytaj_pule() -> list[dict]:
    return pula_mutantow.wczytaj_pule(_git_common() / "monolynx" / "pula.json")


def plik_dziennika() -> Path:
    return _git_common() / "monolynx" / "rozdzielacz.log"


def dziennik(linia: str) -> None:
    print(linia, file=sys.stderr, flush=True)
    try:
        with open(plik_dziennika(), "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {linia}\n")
    except (OSError, pula_mutantow.BladPuli) as e:
        print(f"ROZDZIELACZ: dziennik niezapisany ({e})", file=sys.stderr, flush=True)


def obciazenie_lokalne() -> dict:
    pamiec = 0.0
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            pamiec = next(int(x.split()[1]) * 1024 for x in f if x.startswith("MemAvailable"))
    except (OSError, StopIteration, ValueError):
        pass  # Mac: brak /proc; lokalnie pamiec nie decyduje
    return {"rdzenie": os.cpu_count() or 1, "obciazenie": os.getloadavg()[0], "pamiec_gb": pamiec / 1024 ** 3}


def obciazenie_zdalne(m: dict) -> dict:
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", m["ssh"],
                            "echo $(nproc) $(cut -d' ' -f1 /proc/loadavg) "
                            "$(awk '/MemAvailable/{print $2*1024}' /proc/meminfo)"],
                           capture_output=True, text=True, timeout=30)
        rdzenie, obciazenie, pamiec = r.stdout.split()
        return {"rdzenie": int(rdzenie), "obciazenie": float(obciazenie), "pamiec_gb": float(pamiec) / 1024 ** 3}
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        raise BladMaszyny(str(e)[:200]) from e


def _na_rdzen(p: dict) -> float:
    return p["obciazenie"] / max(p["rdzenie"], 1)


def wybierz(lokalne: dict, zdalne: list[tuple[dict, dict]]) -> dict | None:
    """Najluzniejsza zdrowa maszyna zdalna albo None (lokalnie)."""
    kandydaci = [(_na_rdzen(p), m) for m, p in zdalne if p["pamiec_gb"] >= MIN_PAMIEC_GB]
    if not kandydaci:
        return None
    najluzniejsza, m = min(kandydaci, key=lambda x: x[0])
    return m if _na_rdzen(lokalne) - najluzniejsza >= ROZNICA else None


def skrypt_zdalny(m: dict, praca: str, sha: str, argv: list[str]) -> str:
    """Kopia kodu na maszynie zdalnej, slot przez flock (wspolny z pula), komenda w kontenerze."""
    k, q = m["katalog"], shlex.quote
    komenda = f"pip install -q --no-deps -e . >/dev/null 2>&1; {shlex.join(argv)}; k=$?; chown -R $(stat -c %u:%g .) .; exit $k"
    docker = f"docker run --rm -v $D/repo:/repo -w /repo {q(m['obraz'])} bash -c {q(komenda)}"
    return "\n".join([
        "set -u",
        f"D={k}/praca/{praca}; mkdir -p $D && tar -x -C $D -f $D.tar || exit {KOD_INFRA}",
        f"flock {k}/repo.lock git -C {k}/repo fetch -q origin || exit {KOD_INFRA}",
        f"[ -s $D/zmiany.bundle ] && {{ flock {k}/repo.lock git -C {k}/repo fetch -q $D/zmiany.bundle || exit {KOD_INFRA}; }}",
        f"git clone -q --no-checkout {k}/repo $D/repo && git -C $D/repo checkout -q --detach {sha} || exit {KOD_INFRA}",
        f"[ -s $D/lata.diff ] && {{ git -C $D/repo apply --binary $D/lata.diff || exit {KOD_INFRA}; }}",
        "[ -d $D/nowe ] && cp -a $D/nowe/. $D/repo/",
        f"while :; do for s in $(seq 0 {m['sloty'] - 1}); do",
        f"  flock -n -E {pula_mutantow.KOD_ZAJETY} /tmp/harness-pula-$s.lock {docker}; k=$?",
        f"  [ $k != {pula_mutantow.KOD_ZAJETY} ] && break 2",
        "done; sleep 5; done",
        "rm -rf $D $D.tar; exit $k",
    ])


def uruchom_zdalnie(m: dict, argv: list[str]) -> int:
    """Kod komendy z maszyny zdalnej; KOD_INFRA/KOD_SSH = awaria maszyny. Wyjscie idzie 1:1 na nasze."""
    try:
        korzen = Path(pula_mutantow._git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
        sha = pula_mutantow._git(korzen, "rev-parse", "HEAD").strip()
        dane = pula_mutantow.paczka(korzen)
        praca = f"testy-{sha[:12]}-{os.getpid()}"
        bundle = b""
        if subprocess.run(["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"], cwd=korzen).returncode != 0:
            bundle = pula_mutantow._git(korzen, "bundle", "create", "-", "HEAD", "--not", "origin/main", binary=True)
        bufor = io.BytesIO(dane)
        with tarfile.open(fileobj=bufor, mode="a") as tar:
            info = tarfile.TarInfo("zmiany.bundle")
            info.size = len(bundle)
            tar.addfile(info, io.BytesIO(bundle))
        ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", m["ssh"]]
        subprocess.run([*ssh, f"mkdir -p {m['katalog']}/praca && cat > {m['katalog']}/praca/{praca}.tar"],
                       input=bufor.getvalue(), check=True, timeout=600, capture_output=True)
        return subprocess.run([*ssh, skrypt_zdalny(m, praca, sha, argv)], timeout=LIMIT_S).returncode
    except (OSError, subprocess.SubprocessError, RuntimeError) as e:
        print(f"ROZDZIELACZ: {m['ssh']}: {e}", file=sys.stderr, flush=True)
        return KOD_INFRA


def uruchom_lokalnie(sloty: int, timeout: int, argv: list[str]) -> int:
    return subprocess.run([sys.executable, str(TEST_LOCK), "--sloty", str(sloty), "--timeout", str(timeout),
                           "--", *argv]).returncode


def _parsuj(argv: list[str]) -> tuple[int, int, list[str]]:
    sloty, timeout = 1, 3600
    if "--" not in argv:
        raise ValueError("brak --")
    i = argv.index("--")
    opcje, komenda = argv[:i], argv[i + 1:]
    if not komenda:
        raise ValueError("pusta komenda")
    while opcje:
        nazwa, wartosc, opcje = opcje[0], (opcje[1] if len(opcje) > 1 else ""), opcje[2:]
        if nazwa not in ("--sloty", "--timeout") or not wartosc.isdigit():
            raise ValueError(f"zla opcja {nazwa} {wartosc}")
        if nazwa == "--sloty":
            sloty = int(wartosc)
        else:
            timeout = int(wartosc)
    if not 1 <= sloty <= 64:
        raise ValueError("--sloty poza 1..64")
    return sloty, timeout, komenda


def main(argv: list[str]) -> int:
    try:
        sloty, timeout, komenda = _parsuj(argv)
        pula = wczytaj_pule()
    except (ValueError, pula_mutantow.BladPuli) as e:
        print(f"rozdzielacz: {e}", file=sys.stderr)
        return 64
    zdalne = []
    for m in pula:
        try:
            zdalne.append((m, obciazenie_zdalne(m)))
        except BladMaszyny as e:
            dziennik(f"ROZDZIELACZ ZAWIODL: {m['ssh']} pomiar ({e}) - testy lokalnie")
    lokalne = obciazenie_lokalne()
    cel = wybierz(lokalne, zdalne) if zdalne else None
    if cel is not None:
        dziennik(f"ROZDZIELACZ: testy -> {cel['ssh']} (lokalnie {_na_rdzen(lokalne):.2f}/rdzen)")
        kod = uruchom_zdalnie(cel, komenda)
        if kod not in (KOD_INFRA, KOD_SSH):
            return kod
        dziennik(f"ROZDZIELACZ ZAWIODL: {cel['ssh']} kod {kod} - testy lokalnie")
    return uruchom_lokalnie(sloty, timeout, komenda)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
