import { useEffect, useRef, useState } from "react";
import { fetchZSesja } from "./Konto";
import type { DecyzjaIn, DecyzjaPrzyjeta, KrokRunu, RunSzczegoly as Run, RunStatus, RunUtworzony, ZdarzenieRunu as Zdarzenie } from "./api";
import type { PokrycieRunu } from "./api";
import { Odbior } from "./Odbior";
import { OdbiorDowody } from "./OdbiorDowody";

// "nieznany": brak odpowiedzi albo blad pobrania. Nie blokujemy akcji, bo backend i tak broni sie 409.
export type StanRunnera = "nieznany" | "online" | "offline";

export const PODPOWIEDZ_OFFLINE_ID = "podpowiedz-runner-offline";
export const PODPOWIEDZ_OFFLINE = "Wymaga runnera online. Uruchom runnera na swojej maszynie.";
export const KOMUNIKAT_OFFLINE = "Runner jest offline. Uruchom runnera na swojej maszynie.";
export const KOMUNIKAT_SIEC = "Brak połączenia z kokpitem.";
const KOMUNIKAT_STRUMIEN = "Brak połączenia ze strumieniem zdarzeń. Ponawiam; historia poniżej może być niepełna.";

// Record po enumie z OpenAPI: nowa wartosc po stronie backendu psuje tsc.
const STAN_KROKU: Record<KrokRunu["status"], string> = {
  running: "w toku",
  finished: "zakończony",
  failed: "nieudany",
  skipped: "pominięty",
  waiting: "czeka",
};

// Nigdy "gotowe": o przyjeciu wyniku decyduje czlowiek (RunStatus.to_accept).
const STATUS_RUNU: Record<RunStatus, string> = {
  pending: "oczekuje",
  running: "w toku",
  waiting: "czeka",
  to_accept: "do odbioru",
  failed: "nieudany",
  skipped: "pominięty",
  aborted: "przerwany",
  rejected: "odrzucony",
};

// Nieznana wartosc w runtime (nowszy backend): pokazujemy surowa, nigdy jako sukces.
function napisStatusuRunu(status: string): string {
  return Object.hasOwn(STATUS_RUNU, status) ? STATUS_RUNU[status as RunStatus] : status;
}
function napisStanuKroku(status: string): string {
  return Object.hasOwn(STAN_KROKU, status) ? STAN_KROKU[status as KrokRunu["status"]] : status;
}

// Zdarzenia, po ktorych stan kroku sie zmienil: wtedy dociagamy GET (steps wylicza backend).
const KINDY_KROKU = new Set(["step_started", "step_skipped", "step_waiting", "step_aborted"]);

// Atrybuty wspolne dla przyciskow wymagajacych runnera.
export function opisOffline(offline: boolean) {
  return offline ? { "aria-describedby": PODPOWIEDZ_OFFLINE_ID, title: PODPOWIEDZ_OFFLINE } : {};
}

function runZAdresu(): string | null {
  return new URLSearchParams(window.location.search).get("run");
}

// Scalanie po seq: historia z GET i strumien zbiegaja sie bez duplikatow.
function scal(stare: Zdarzenie[], nowe: Zdarzenie[]): Zdarzenie[] {
  const mapa = new Map<number, Zdarzenie>();
  for (const z of stare) mapa.set(z.seq, z);
  for (const z of nowe) mapa.set(z.seq, z);
  return [...mapa.values()].sort((a, b) => a.seq - b.seq);
}

// Powod kroku: niepusty steps[].reason; dla pominietego, gdy backend go nie ma,
// tekst ostatniego (po seq) zdarzenia step_skipped tego kroku.
function powodKroku(krok: KrokRunu, zdarzenia: Zdarzenie[]): string | null {
  if (krok.reason) return krok.reason;
  if (krok.status !== "skipped") return null;
  let ostatnie: Zdarzenie | null = null;
  for (const z of zdarzenia) {
    if (z.kind === "step_skipped" && z.step_id === krok.step_id && (ostatnie === null || z.seq > ostatnie.seq)) {
      ostatnie = z;
    }
  }
  return ostatnie && ostatnie.text ? ostatnie.text : null;
}

export function WidokRunu({
  poWygasnieciuSesji,
  stanRunnera,
}: {
  poWygasnieciuSesji: () => void;
  stanRunnera: StanRunnera;
}) {
  const offline = stanRunnera === "offline";
  const [runId, setRunId] = useState<string | null>(runZAdresu);
  const [zdarzenia, setZdarzenia] = useState<Zdarzenie[]>([]);
  const [kroki, setKroki] = useState<KrokRunu[]>([]);
  const [status, setStatus] = useState<string | null>(null);
  const [reason, setReason] = useState<string | null>(null);
  const [pokrycie, setPokrycie] = useState<PokrycieRunu | null>(null);
  const [blad, setBlad] = useState<string | null>(null);
  // Osobno od bledu historii: udany GET nie moze zgasic komunikatu o strumieniu.
  const [bladStrumienia, setBladStrumienia] = useState<string | null>(null);
  // Blad samego uruchomienia (np. runner offline); nie nalezy do zadnego runu.
  const [bladUruchomienia, setBladUruchomienia] = useState<string | null>(null);
  const [uruchamiam, setUruchamiam] = useState(false);
  const [ticket, setTicket] = useState("");
  const [ostrzezenie, setOstrzezenie] = useState<{ aktywnyRunId: string } | null>(null);
  const [powod, setPowod] = useState("");
  const [bladDecyzji, setBladDecyzji] = useState<string | null>(null);
  const [wysylamDecyzje, setWysylamDecyzje] = useState(false);
  // dociagnij() zyje w efekcie (zalezy od runId); decyzja odswieza widok przez ten uchwyt.
  const dociagnijRef = useRef<(() => Promise<void>) | null>(null);
  const runIdRef = useRef<string | null>(runId);
  runIdRef.current = runId;

  useEffect(() => {
    // Nowy runId (lub brak): nic z poprzedniego runu nie moze zostac na ekranie.
    setZdarzenia([]);
    setKroki([]);
    setStatus(null);
    setReason(null);
    setPokrycie(null);
    setBlad(null);
    setBladStrumienia(null);
    setPowod("");
    setBladDecyzji(null);
    setWysylamDecyzje(false);
    if (!runId) return;
    let anulowano = false;

    async function dociagnij() {
      try {
        const r = await fetch(`/api/runs/${encodeURIComponent(runId!)}`);
        if (anulowano) return;
        if (r.status === 404) {
          setBlad("Nie znaleziono runu o tym identyfikatorze.");
          return;
        }
        if (!r.ok) {
          setBlad(KOMUNIKAT_SIEC);
          return;
        }
        const run: Run = await r.json();
        if (anulowano) return;
        setBlad(null);
        setStatus(run.status);
        setReason(run.reason);
        setKroki(run.steps);
        setPokrycie(run.pokrycie ?? null);
        setZdarzenia((p) => scal(p, run.events));
      } catch {
        if (!anulowano) setBlad(KOMUNIKAT_SIEC);
      }
    }

    // Serwer wznawia strumien od Last-Event-ID; GET i onopen zostaja jako obrona
    // w glab (steps/status/pokrycie). Scalanie po seq.
    dociagnijRef.current = dociagnij;
    const es = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events`);
    void dociagnij();
    es.onopen = () => {
      if (anulowano) return;
      setBladStrumienia(null);
      void dociagnij();
    };
    es.onerror = () => {
      if (!anulowano) setBladStrumienia(KOMUNIKAT_STRUMIEN);
    };
    // Serwer zamyka strumien po tym zdarzeniu; close() przed onerror, zeby nie bylo ponawiania.
    // Dane zdarzenia nie sa potrzebne (stały tekst UI), wiec nie ma parsowania.
    es.addEventListener("sesja_wygasla", () => {
      es.close();
      if (anulowano) return;
      poWygasnieciuSesji();
    });
    es.addEventListener("run", (e) => {
      if (anulowano) return;
      const z: Zdarzenie = JSON.parse((e as MessageEvent).data);
      setZdarzenia((p) => scal(p, [z]));
      if (z.kind === "step_finished" || z.kind === "step_failed" || z.kind === "run_finished" || KINDY_KROKU.has(z.kind)) void dociagnij();
    });
    return () => {
      anulowano = true;
      dociagnijRef.current = null;
      es.close();
    };
  }, [runId]);

  async function wyslijStart(potwierdz: boolean) {
    setUruchamiam(true);
    setBladUruchomienia(null);
    setOstrzezenie(null);
    // Kazda porazka ma ten sam skutek: zadnego "starego" runu na ekranie ani w adresie.
    // runId=null domyka stary strumien w cleanupie efektu.
    const porzucRun = () => {
      window.history.replaceState(null, "", window.location.pathname);
      setRunId(null);
    };
    try {
      const r = await fetchZSesja("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // Pusty ticket: klucz pomijamy, bo API odrzuca pusty napis (min_length=1).
        body: JSON.stringify({
          workflow: "szkielet",
          ...(ticket.trim() ? { ticket_id: ticket.trim() } : {}),
          ...(potwierdz ? { potwierdz_rownolegly: true } : {}),
        }),
      });
      if (r.status === 409) {
        // Pole aktywny_run_id odroznia ostrzezenie o rownoleglym runie od "runner offline".
        // Nieczytelne ciało traktujemy jak offline. Biezacego widoku nie zrywamy: decyzja jest u człowieka.
        const aktywny = await r.json().then(
          (j) => (typeof j?.aktywny_run_id === "string" ? (j.aktywny_run_id as string) : null),
          () => null,
        );
        if (aktywny !== null) {
          setOstrzezenie({ aktywnyRunId: aktywny });
          return;
        }
        // Stan pusty zamiast bledu: mow, co zrobic (BRO-123).
        porzucRun();
        setBladUruchomienia(KOMUNIKAT_OFFLINE);
        return;
      }
      if (!r.ok) {
        porzucRun();
        setBladUruchomienia(`Nie udało się uruchomić (błąd ${r.status}).`);
        return;
      }
      const utworzony: RunUtworzony = await r.json();
      const id = utworzony.run_id;
      window.history.pushState(null, "", `?run=${encodeURIComponent(id)}`);
      setRunId(id);
    } catch {
      porzucRun();
      setBladUruchomienia(KOMUNIKAT_SIEC);
    } finally {
      setUruchamiam(false);
    }
  }

  async function wyslijDecyzje(tresc: DecyzjaIn) {
    const id = runId;
    // Podwojnego zadania broni disabled={wysylamDecyzje} na obu przyciskach: React nie wola
    // onClick przycisku wylaczonego w propsach, wiec handler z wysylamDecyzje=true nie startuje.
    if (!id) return;
    setWysylamDecyzje(true);
    setBladDecyzji(null);
    const nieaktualny = () => runIdRef.current !== id;
    try {
      const r = await fetchZSesja(`/api/runs/${encodeURIComponent(id)}/decision`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(tresc),
      });
      // Zmiane runu sprawdzamy dopiero po ostatnim await galezi (po r.json()): spozniona
      // odpowiedz runu A nie moze dotknac widoku runu B takze w oknie parsowania ciala.
      if (!r.ok) {
        // detail z JSON, a gdy go brak albo nie jest napisem: kod HTTP. Nigdy cisza.
        const detail = await r.json().then(
          (j) => (typeof j?.detail === "string" && j.detail ? (j.detail as string) : null),
          () => null,
        );
        if (nieaktualny()) return;
        setBladDecyzji(`Decyzja nieprzyjęta: ${detail ?? `błąd ${r.status}`}`);
        // Stan na ekranie mogl sie rozjechac z kokpitem (np. 409): pokaz prawde.
        void dociagnijRef.current?.();
        return;
      }
      const przyjeta: DecyzjaPrzyjeta = await r.json();
      if (nieaktualny()) return;
      setStatus(przyjeta.status);
      setPowod("");
      void dociagnijRef.current?.();
    } catch {
      if (!nieaktualny()) setBladDecyzji(`Decyzja nieprzyjęta: ${KOMUNIKAT_SIEC}`);
    } finally {
      if (!nieaktualny()) setWysylamDecyzje(false);
    }
  }

  const odswiezRun = () => {
    void dociagnijRef.current?.();
  };
  const krokBramki = kroki.find((k) => k.status === "waiting")?.step_id;

  return (
    <main style={{ fontFamily: "system-ui", maxWidth: 720, margin: "40px auto" }}>
      <h1 style={{ fontSize: 20 }}>Harness Cockpit</h1>
      <label>
        Ticket{" "}
        <input
          type="text"
          value={ticket}
          onChange={(e) => {
            setTicket(e.target.value);
            // Ostrzezenie dotyczylo starej wartosci pola.
            setOstrzezenie(null);
          }}
        />
      </label>{" "}
      <button onClick={() => void wyslijStart(false)} disabled={uruchamiam || offline} {...opisOffline(offline)}>
        {uruchamiam ? "Uruchamiam…" : "Uruchom"}
      </button>
      {ostrzezenie && (
        <div role="alert" style={{ color: "#8A5A00" }}>
          <p>
            Na tym tickecie trwa już run{" "}
            <a href={`?run=${encodeURIComponent(ostrzezenie.aktywnyRunId)}`}>{ostrzezenie.aktywnyRunId}</a>. Uruchomić drugi?
          </p>
          <button onClick={() => void wyslijStart(true)} disabled={uruchamiam || offline} {...opisOffline(offline)}>
            Uruchom mimo to
          </button>{" "}
          <button onClick={() => setOstrzezenie(null)} disabled={uruchamiam}>
            Anuluj
          </button>
        </div>
      )}
      {bladUruchomienia && <p style={{ color: "#A5201A" }}>{bladUruchomienia}</p>}
      {blad && <p style={{ color: "#A5201A" }}>{blad}</p>}
      {bladStrumienia && <p style={{ color: "#A5201A" }}>{bladStrumienia}</p>}
      {runId && (
        <p style={{ color: "#58616D" }}>
          run {runId}
          {status && <> · status: {status}</>}
        </p>
      )}
      {runId && status && <p>Stan runu: {napisStatusuRunu(status)}</p>}
      {runId && status === "waiting" && (
        <div>
          <p>czeka na decyzję: krok {krokBramki ?? "—"}</p>
          <button onClick={() => void wyslijDecyzje({ decyzja: "approve" })} disabled={wysylamDecyzje}>
            Zatwierdź
          </button>{" "}
          <label>
            Powód odrzucenia{" "}
            <input type="text" value={powod} onChange={(e) => setPowod(e.target.value)} />
          </label>{" "}
          <button
            onClick={() => void wyslijDecyzje({ decyzja: "reject", powod: powod.trim() })}
            disabled={wysylamDecyzje || powod.trim() === ""}
          >
            Odrzuć
          </button>
        </div>
      )}
      {runId && bladDecyzji && (
        <p role="alert" style={{ color: "#A5201A" }}>
          {bladDecyzji}
        </p>
      )}
      {runId && status === "rejected" && <p style={{ color: "#A5201A" }}>odrzucony{reason ? `: ${reason}` : ""}</p>}
      {runId && status !== "rejected" && reason && <p style={{ color: "#A5201A" }}>{reason}</p>}
      {runId && status === "to_accept" && (
        <Odbior key={runId} runId={runId} zdarzenia={zdarzenia} odswiez={() => void dociagnijRef.current?.()} />
      )}
      {status === "to_accept" && runId ? (
        <OdbiorDowody key={`dowody-${runId}`} runId={runId} odswiez={odswiezRun} />
      ) : null}
      {runId && pokrycie !== null && (
        <div>
          {pokrycie.stan === "niesprawdzony" ? (
            <p role="alert" style={{ color: "#A5201A" }}>
              {`Nic tego nie sprawdziło: pominięto ${pokrycie.pominiete} z ${pokrycie.zaplanowane} bramek`}
            </p>
          ) : (
            <p>{`Sprawdzono ${pokrycie.wykonane} z ${pokrycie.zaplanowane} bramek`}</p>
          )}
          {Object.keys(pokrycie.powody).length > 0 && (
            <ul>
              {Object.entries(pokrycie.powody).map(([krok, kod]) => (
                <li key={krok}>{`${krok}: ${kod}`}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      {runId && kroki.length > 0 && (
        <table aria-label="Kroki runu" style={{ borderCollapse: "collapse", width: "100%", textAlign: "left" }}>
          <thead>
            <tr>
              <th>Krok</th>
              <th>Agent</th>
              <th>Próba</th>
              <th>Stan</th>
              <th>Powód</th>
            </tr>
          </thead>
          <tbody>
            {kroki.map((k) => (
              <tr key={`${k.step_id}:${k.agent_id}:${k.attempt}`}>
                <td>{k.step_id}</td>
                <td>{k.agent_id || "—"}</td>
                <td>próba {k.attempt}</td>
                <td>{napisStanuKroku(k.status)}</td>
                <td>{powodKroku(k, zdarzenia) ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <ul>
        {zdarzenia.map((z) => (
          <li key={z.seq}>
            <b>{z.kind}</b> {z.text}
            {z.text_truncated && (
              // === null, nie truthiness: brak pomiaru to "nieznana", nigdy 0 (measured_or_null).
              <span style={{ color: "#58616D" }}>
                {" "}
                obcięte, pełna długość {z.text_full_len === null ? "nieznana" : `${z.text_full_len} znaków`}
              </span>
            )}
          </li>
        ))}
      </ul>
    </main>
  );
}
