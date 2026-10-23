import { useEffect, useState } from "react";

type Zdarzenie = { seq: number; kind: string; text: string; step_id: string };
type Run = {
  id: string;
  workflow: string;
  status: string;
  runner_kind: string;
  events: Zdarzenie[];
};

const KOMUNIKAT_OFFLINE = "Runner jest offline. Uruchom runnera na swojej maszynie.";
const KOMUNIKAT_SIEC = "Brak połączenia z kokpitem.";

// Scalanie po seq: historia z GET i strumień zbiegają się bez duplikatów.
function scal(stare: Zdarzenie[], nowe: Zdarzenie[]): Zdarzenie[] {
  const mapa = new Map<number, Zdarzenie>();
  for (const z of stare) mapa.set(z.seq, z);
  for (const z of nowe) mapa.set(z.seq, z);
  return [...mapa.values()].sort((a, b) => a.seq - b.seq);
}

function runZAdresu(): string | null {
  return new URLSearchParams(window.location.search).get("run");
}

export function App() {
  const [runId, setRunId] = useState<string | null>(runZAdresu);
  const [zdarzenia, setZdarzenia] = useState<Zdarzenie[]>([]);
  const [status, setStatus] = useState<string | null>(null);
  const [blad, setBlad] = useState<string | null>(null);
  const [uruchamiam, setUruchamiam] = useState(false);

  useEffect(() => {
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
        setZdarzenia((p) => scal(p, run.events));
      } catch {
        if (!anulowano) setBlad(KOMUNIKAT_SIEC);
      }
    }

    // Najpierw strumien (zdarzenia scalane od razu), po onopen historia z GET:
    // strumien nie odtwarza historii, a samo "historia, potem strumien" zostawia luke.
    const es = new EventSource(`/api/runs/${encodeURIComponent(runId)}/stream`);
    es.onopen = () => void dociagnij();
    es.addEventListener("run", (e) => {
      const z: Zdarzenie = JSON.parse((e as MessageEvent).data);
      setZdarzenia((p) => scal(p, [z]));
      if (z.kind === "step_finished" || z.kind === "step_failed") void dociagnij();
    });
    return () => {
      anulowano = true;
      es.close();
    };
  }, [runId]);

  async function uruchom() {
    setUruchamiam(true);
    setBlad(null);
    setZdarzenia([]);
    setStatus(null);
    try {
      const r = await fetch("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workflow: "szkielet" }),
      });
      if (r.status === 409) {
        // Stan pusty zamiast bledu: mow, co zrobic (BRO-123).
        setBlad(KOMUNIKAT_OFFLINE);
        return;
      }
      if (!r.ok) {
        setBlad(`Nie udało się uruchomić (błąd ${r.status}).`);
        return;
      }
      const id: string = (await r.json()).run_id;
      window.history.pushState(null, "", `?run=${encodeURIComponent(id)}`);
      setRunId(id);
    } catch {
      setBlad(KOMUNIKAT_SIEC);
    } finally {
      setUruchamiam(false);
    }
  }

  return (
    <main style={{ fontFamily: "system-ui", maxWidth: 720, margin: "40px auto" }}>
      <h1 style={{ fontSize: 20 }}>Harness Cockpit</h1>
      <button onClick={uruchom} disabled={uruchamiam}>
        {uruchamiam ? "Uruchamiam…" : "Uruchom"}
      </button>
      {blad && <p style={{ color: "#A5201A" }}>{blad}</p>}
      {runId && (
        <p style={{ color: "#58616D" }}>
          run {runId}
          {status && <> · status: {status}</>}
        </p>
      )}
      <ul>
        {zdarzenia.map((z) => (
          <li key={z.seq}>
            <b>{z.kind}</b> {z.text}
          </li>
        ))}
      </ul>
    </main>
  );
}
