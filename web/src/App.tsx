import { useEffect, useState } from "react";

type Zdarzenie = { seq: number; kind: string; text: string; step_id: string };

export function App() {
  const [runId, setRunId] = useState<string | null>(null);
  const [zdarzenia, setZdarzenia] = useState<Zdarzenie[]>([]);
  const [blad, setBlad] = useState<string | null>(null);

  useEffect(() => {
    if (!runId) return;
    // Numeracja zdarzen i Last-Event-ID: ponowne polaczenie nie duplikuje historii.
    const es = new EventSource(`/api/runs/${runId}/stream`);
    es.addEventListener("run", (e) =>
      setZdarzenia((p) => [...p, JSON.parse((e as MessageEvent).data)]),
    );
    return () => es.close();
  }, [runId]);

  async function uruchom() {
    setBlad(null);
    setZdarzenia([]);
    const r = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workflow: "szkielet" }),
    });
    if (r.status === 409) {
      // Stan pusty zamiast bledu: mow, co zrobic (BRO-123).
      setBlad("Runner jest offline. Uruchom runnera na swojej maszynie.");
      return;
    }
    setRunId((await r.json()).run_id);
  }

  return (
    <main style={{ fontFamily: "system-ui", maxWidth: 720, margin: "40px auto" }}>
      <h1 style={{ fontSize: 20 }}>Harness Cockpit</h1>
      <button onClick={uruchom}>Uruchom</button>
      {blad && <p style={{ color: "#A5201A" }}>{blad}</p>}
      {runId && <p style={{ color: "#58616D" }}>run {runId}</p>}
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
