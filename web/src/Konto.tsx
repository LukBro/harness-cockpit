import { useState, type CSSProperties, type FormEvent } from "react";

export type Ja = { email: string; zespol: string; rola: string; csrf: string };

const KOMUNIKAT_SIEC = "Brak połączenia z kokpitem.";
const KOMUNIKAT_OGOLNY = "Nie udało się. Sprawdź dane i spróbuj ponownie.";

const stylBlad: CSSProperties = { color: "#A5201A" };
const stylSzary: CSSProperties = { color: "#58616D" };
const stylPole: CSSProperties = { display: "block", marginBottom: 12 };

// Token w pamieci modulu: po przeladowaniu strony zawsze bierzemy nowy z "ja".
let tokenCsrf: string | null = null;
let poWylogowaniu: () => void = () => {};
// Wspolna obietnica: rownolegle odswiezenia (start, 403, StrictMode) to jedno zadanie.
let odswiezanie: Promise<Ja | null> | null = null;

export function ustawToken(csrf: string | null) {
  tokenCsrf = csrf;
}

export function ustawPowrotDoLogowania(cb: () => void) {
  poWylogowaniu = cb;
}

function sesjaWygasla() {
  tokenCsrf = null;
  poWylogowaniu();
}

// Zwraca null przy 401 (token czyszczony, callback wywolany); inne bledy to wyjatek.
export function odswiezSesje(): Promise<Ja | null> {
  if (odswiezanie) return odswiezanie;
  const p = (async () => {
    const r = await fetch("/api/konto/ja");
    if (r.status === 401) {
      sesjaWygasla();
      return null;
    }
    if (!r.ok) throw new Error(`ja: ${r.status}`);
    const ja: Ja = await r.json();
    tokenCsrf = ja.csrf;
    return ja;
  })();
  odswiezanie = p;
  const zwolnij = () => {
    if (odswiezanie === p) odswiezanie = null;
  };
  p.then(zwolnij, zwolnij);
  return p;
}

async function bladCsrf(r: Response): Promise<boolean> {
  if (r.status !== 403) return false;
  try {
    const d = (await r.clone().json()).detail;
    return typeof d === "string" && d.includes("CSRF");
  } catch {
    return false;
  }
}

export async function fetchZSesja(url: string, init: RequestInit = {}): Promise<Response> {
  const zmienia = (init.method ?? "GET").toUpperCase() !== "GET";
  const wyslij = () => {
    const headers = new Headers(init.headers);
    if (zmienia && tokenCsrf) headers.set("X-CSRF-Token", tokenCsrf);
    return fetch(url, { ...init, headers });
  };
  let r = await wyslij();
  if (zmienia && (await bladCsrf(r))) {
    // Inna karta mogla uniewaznic token: jedno odswiezenie i jedna ponowna proba.
    if ((await odswiezSesje()) === null) return r;
    r = await wyslij();
  }
  if (r.status === 401) sesjaWygasla();
  return r;
}

async function komunikatBledu(r: Response): Promise<string> {
  try {
    const d = (await r.json()).detail;
    if (typeof d === "string" && d) return d;
  } catch {
    // brak ciala JSON: komunikat ogolny
  }
  return KOMUNIKAT_OGOLNY;
}

export function Konto({ onZalogowano }: { onZalogowano: () => void }) {
  const [rejestracja, setRejestracja] = useState(false);
  const [email, setEmail] = useState("");
  const [haslo, setHaslo] = useState("");
  const [zespol, setZespol] = useState("");
  const [blad, setBlad] = useState<string | null>(null);
  const [wysylam, setWysylam] = useState(false);

  async function wyslij(e: FormEvent) {
    e.preventDefault();
    setWysylam(true);
    setBlad(null);
    try {
      const body: Record<string, string> = { email, haslo };
      if (rejestracja && zespol.trim()) body.nazwa_zespolu = zespol.trim();
      // Bez fetchZSesja: przed sesja nie ma tokenu, a 401 to zle dane, nie wygasniecie.
      const r = await fetch(rejestracja ? "/api/konto/rejestracja" : "/api/konto/logowanie", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        setBlad(await komunikatBledu(r));
        return;
      }
      tokenCsrf = (await r.json()).csrf;
      onZalogowano();
    } catch {
      setBlad(KOMUNIKAT_SIEC);
    } finally {
      setWysylam(false);
    }
  }

  return (
    <main style={{ fontFamily: "system-ui", maxWidth: 360, margin: "40px auto" }}>
      <h1 style={{ fontSize: 20 }}>Harness Cockpit</h1>
      <h2 style={{ fontSize: 16 }}>{rejestracja ? "Rejestracja" : "Logowanie"}</h2>
      <form onSubmit={wyslij}>
        <label htmlFor="konto-email" style={stylPole}>
          E-mail
          <input
            id="konto-email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            style={{ display: "block", width: "100%" }}
          />
        </label>
        <label htmlFor="konto-haslo" style={stylPole}>
          Hasło
          <input
            id="konto-haslo"
            type="password"
            value={haslo}
            onChange={(e) => setHaslo(e.target.value)}
            required
            style={{ display: "block", width: "100%" }}
          />
        </label>
        {rejestracja && (
          <label htmlFor="konto-zespol" style={stylPole}>
            Nazwa zespołu
            <input
              id="konto-zespol"
              type="text"
              value={zespol}
              onChange={(e) => setZespol(e.target.value)}
              style={{ display: "block", width: "100%" }}
            />
          </label>
        )}
        {blad && (
          <p role="alert" style={stylBlad}>
            {blad}
          </p>
        )}
        <button type="submit" disabled={wysylam}>
          {rejestracja ? "Zarejestruj" : "Zaloguj"}
        </button>{" "}
        <button
          type="button"
          onClick={() => {
            setRejestracja(!rejestracja);
            setBlad(null);
          }}
        >
          {rejestracja ? "Mam już konto" : "Załóż konto"}
        </button>
      </form>
    </main>
  );
}

export function PanelKonta({ email }: { email: string }) {
  const [stare, setStare] = useState("");
  const [nowe, setNowe] = useState("");
  const [blad, setBlad] = useState<string | null>(null);
  const [zmieniono, setZmieniono] = useState(false);
  const [zajety, setZajety] = useState(false);

  async function wyloguj(url: string) {
    setBlad(null);
    try {
      const r = await fetchZSesja(url, { method: "POST" });
      // 401 wywolalo juz powrot do logowania; sukces to 204.
      if (r.status === 204) sesjaWygasla();
      else if (r.status !== 401) setBlad(await komunikatBledu(r));
    } catch {
      setBlad(KOMUNIKAT_SIEC);
    }
  }

  async function zmienHaslo(e: FormEvent) {
    e.preventDefault();
    setZajety(true);
    setBlad(null);
    setZmieniono(false);
    try {
      const r = await fetchZSesja("/api/konto/haslo", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stare, nowe }),
      });
      if (r.status !== 204) {
        // 401 wywolalo juz powrot do logowania; 422 (np. zle haslo) to blad formularza.
        if (r.status !== 401) setBlad(await komunikatBledu(r));
        return;
      }
      setStare("");
      setNowe("");
      setZmieniono(true);
    } catch {
      setBlad(KOMUNIKAT_SIEC);
    } finally {
      setZajety(false);
    }
  }

  return (
    <aside style={{ fontFamily: "system-ui", maxWidth: 720, margin: "40px auto 0" }}>
      <p style={stylSzary}>
        {email}{" "}
        <button type="button" onClick={() => void wyloguj("/api/konto/wyloguj")}>
          Wyloguj
        </button>{" "}
        <button type="button" onClick={() => void wyloguj("/api/konto/wyloguj-wszedzie")}>
          Wyloguj ze wszystkich urządzeń
        </button>
      </p>
      <form onSubmit={zmienHaslo}>
        <label htmlFor="konto-stare" style={{ marginRight: 12 }}>
          Obecne hasło{" "}
          <input
            id="konto-stare"
            type="password"
            value={stare}
            onChange={(e) => setStare(e.target.value)}
            required
          />
        </label>
        <label htmlFor="konto-nowe" style={{ marginRight: 12 }}>
          Nowe hasło{" "}
          <input
            id="konto-nowe"
            type="password"
            value={nowe}
            onChange={(e) => setNowe(e.target.value)}
            required
          />
        </label>
        <button type="submit" disabled={zajety}>
          Zmień hasło
        </button>
      </form>
      {blad && (
        <p role="alert" style={stylBlad}>
          {blad}
        </p>
      )}
      {zmieniono && (
        <p role="status" style={stylSzary}>
          Hasło zmienione.
        </p>
      )}
    </aside>
  );
}
