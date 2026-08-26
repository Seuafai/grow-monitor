"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

type Tab = "timeline" | "sensors" | "ai" | "settings";

interface Photo { id: string; timestamp: string; plant: string; base64: string; bytes?: number; }
interface Entry { id: string; timestamp: string; plant: string; ph?: string; ec?: string; nutrients?: string; notes?: string; }
interface Reading { timestamp: string; temperature?: number; humidity?: number; vpd?: number; unit?: string; }
interface Settings { lightsOn: string; lightsOff: string; photosPerDay: number; plants: string[]; }
interface ChatMsg { role: string; text: string; timestamp: string; }

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

function Sparkline({ values, color }: { values: number[]; color: string }) {
  const w = 600;
  const h = 80;
  if (!values.length) return <div className="muted">no data</div>;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pts = values
    .map((v, i) => `${(i / (values.length - 1)) * w},${h - ((v - min) / span) * (h - 8) - 4}`)
    .join(" ");
  return (
    <svg className="spark" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
      <polyline points={pts} fill="none" stroke={color} strokeWidth="2" />
    </svg>
  );
}

export default function Dashboard() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [password, setPassword] = useState("");
  const [tab, setTab] = useState<Tab>("timeline");
  const [settings, setSettings] = useState<Settings | null>(null);
  const [photos, setPhotos] = useState<Photo[]>([]);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [readings, setReadings] = useState<Reading[]>([]);
  const [plant, setPlant] = useState<string>("");
  const [toast, setToast] = useState("");
  const [busy, setBusy] = useState(false);

  const [ph, setPh] = useState("");
  const [ec, setEc] = useState("");
  const [nutrients, setNutrients] = useState("");
  const [notes, setNotes] = useState("");

  const [analysis, setAnalysis] = useState("");
  const [analysisBusy, setAnalysisBusy] = useState(false);
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [chatInput, setChatInput] = useState("");
  const chatEnd = useRef<HTMLDivElement>(null);

  const [form, setForm] = useState({ lightsOn: "18:00", lightsOff: "12:00", photosPerDay: 4, plants: "", newPassword: "" });

  const showToast = useCallback((msg: string) => {
    setToast(msg);
    setTimeout(() => setToast(""), 3500);
  }, []);

  const refresh = useCallback(async () => {
    const [p, e, r, s] = await Promise.all([
      getJson<{ photos: Photo[] }>("/api/photos"),
      getJson<{ entries: Entry[] }>("/api/entries"),
      getJson<{ readings: Reading[] }>("/api/readings"),
      getJson<{ settings: Settings }>("/api/settings"),
    ]);
    setPhotos(p.photos);
    setEntries(e.entries);
    setReadings(r.readings);
    setSettings(s.settings);
    setPlant((prev) => prev || (s.settings.plants[0] ?? ""));
    setForm({
      lightsOn: s.settings.lightsOn,
      lightsOff: s.settings.lightsOff,
      photosPerDay: s.settings.photosPerDay,
      plants: s.settings.plants.join(", "),
      newPassword: "",
    });
  }, []);

  useEffect(() => {
    getJson<{ authed: boolean }>("/api/auth")
      .then((a) => {
        setAuthed(a.authed);
        if (a.authed) refresh();
      })
      .catch(() => setAuthed(false));
  }, [refresh]);

  async function login(e: React.FormEvent) {
    e.preventDefault();
    const res = await fetch("/api/auth", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    if (res.ok) {
      setAuthed(true);
      await refresh();
      showToast("Welcome back");
    } else {
      showToast("Wrong password");
    }
  }

  async function logout() {
    await fetch("/api/logout", { method: "POST" });
    window.location.reload();
  }

  async function logEntry(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    const res = await fetch("/api/entries", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        plant,
        ph: ph ? Number(ph) : undefined,
        ec: ec ? Number(ec) : undefined,
        nutrients,
        notes,
      }),
    });
    setBusy(false);
    if (res.ok) {
      setPh(""); setEc(""); setNutrients(""); setNotes("");
      await refresh();
      showToast("Entry logged");
    } else {
      const data = await res.json().catch(() => ({}));
      showToast(data.error || "Could not log entry");
    }
  }

  async function saveSettings(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    const res = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        lightsOn: form.lightsOn,
        lightsOff: form.lightsOff,
        photosPerDay: Number(form.photosPerDay),
        plants: form.plants.split(",").map((p) => p.trim()).filter(Boolean),
        newPassword: form.newPassword || undefined,
      }),
    });
    setBusy(false);
    if (res.ok) {
      await refresh();
      showToast("Settings saved — the Pi picks it up within 5 minutes");
    } else {
      showToast("Could not save settings");
    }
  }

  async function runAnalysis() {
    setAnalysisBusy(true);
    setAnalysis("");
    try {
      const res = await fetch("/api/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plant }),
      });
      const data = await res.json();
      if (data.ok) {
        setAnalysis(data.result as string);
      } else {
        showToast(data.error || "Analysis failed");
      }
    } catch {
      showToast("Analysis failed");
    } finally {
      setAnalysisBusy(false);
    }
  }

  async function sendChat(e: React.FormEvent) {
    e.preventDefault();
    if (!chatInput.trim()) return;
    const msg = chatInput;
    setChatInput("");
    setMessages((m) => [...m, { role: "user", text: msg, timestamp: new Date().toISOString() }]);
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plant, message: msg }),
    });
    const data = await res.json().catch(() => ({ ok: false }));
    setMessages((m) => [
      ...m,
      data.ok
        ? { role: "assistant", text: data.reply as string, timestamp: new Date().toISOString() }
        : { role: "assistant", text: `Error: ${data.error || "unknown"}`, timestamp: new Date().toISOString() },
    ]);
  }

  useEffect(() => {
    if (chatEnd.current) chatEnd.current.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function loadChat() {
    try {
      const data = await getJson<{ messages: ChatMsg[] }>(`/api/chat?plant=${encodeURIComponent(plant)}`);
      setMessages(data.messages);
    } catch {
      setMessages([]);
    }
  }

  useEffect(() => {
    if (tab === "ai" && plant) loadChat();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, plant]);

  const timeline = useMemo(() => {
    interface TimelineItem {
      ts: string;
      kind: "photo" | "entry";
      photo?: Photo;
      entry?: Entry;
    }
    const items: TimelineItem[] = [
      ...photos.map((p) => ({ ts: p.timestamp, kind: "photo" as const, photo: p })),
      ...entries.map((en) => ({ ts: en.timestamp, kind: "entry" as const, entry: en })),
    ];
    const filtered = items.filter(
      (it) =>
        plant === "all" ||
        (it.photo && it.photo.plant === plant) ||
        (it.entry && it.entry.plant === plant),
    );
    filtered.sort((a, b) => b.ts.localeCompare(a.ts));
    return filtered;
  }, [photos, entries, plant]);

  const days = useMemo(() => {
    const map = new Map<string, typeof timeline>();
    for (const it of timeline) {
      const day = it.ts.slice(0, 10);
      if (!map.has(day)) map.set(day, []);
      map.get(day)!.push(it);
    }
    return Array.from(map.entries());
  }, [timeline]);

  const latest = readings[0];
  const tempSeries = useMemo(() => readings.slice(0, 60).reverse().map((r) => r.temperature ?? 0), [readings]);
  const humSeries = useMemo(() => readings.slice(0, 60).reverse().map((r) => r.humidity ?? 0), [readings]);

  if (authed === null) return <div className="container">Loading…</div>;

  if (!authed) {
    return (
      <div className="login-wrap">
        <h2>Grow Diary</h2>
        <form onSubmit={login}>
          <label>Password</label>
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoFocus />
          <div style={{ marginTop: 14 }}>
            <button type="submit">Open diary</button>
          </div>
        </form>
      </div>
    );
  }

  return (
    <div>
      <header className="topbar">
        <h1>🌿 Grow Diary</h1>
        <select value={plant} onChange={(e) => setPlant(e.target.value)} style={{ maxWidth: 180 }}>
          {plant !== "all" && !(settings?.plants ?? []).includes(plant) && (
            <option value={plant}>{plant}</option>
          )}
          <option value="all">All plants</option>
          {(settings?.plants ?? []).map((p) => (
            <option key={p} value={p}>{p}</option>
          ))}
        </select>
        <button className="secondary" onClick={logout}>Log out</button>
      </header>

      <div className="container">
        <div className="tabs">
          {(["timeline", "sensors", "ai", "settings"] as Tab[]).map((t) => (
            <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>
              {t}
            </button>
          ))}
        </div>

        {tab === "timeline" && (
          <>
            <div className="card">
              <h3>Log an entry</h3>
              <form onSubmit={logEntry}>
                <div className="row">
                  <div>
                    <label>pH</label>
                    <input type="number" step="0.1" min="0" max="14" value={ph} onChange={(e) => setPh(e.target.value)} />
                  </div>
                  <div>
                    <label>EC (mS/cm)</label>
                    <input type="number" step="0.1" min="0.2" max="3.6" value={ec} onChange={(e) => setEc(e.target.value)} />
                  </div>
                </div>
                <label>Nutrients used</label>
                <input value={nutrients} onChange={(e) => setNutrients(e.target.value)} placeholder="e.g. BioBizz Grow 2ml/L + calmag 0.5ml/L" />
                <label>Notes</label>
                <textarea rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} />
                <div style={{ marginTop: 10 }}>
                  <button type="submit" disabled={busy}>Log entry</button>
                </div>
              </form>
            </div>

            {days.length === 0 && <div className="card">No photos or entries yet. Once the Pi takes its first photo it appears here.</div>}

            {days.map(([day, items]) => (
              <div key={day}>
                <div className="day-header">{day}</div>
                {items.map((it, i) =>
                  it.photo ? (
                    <div className="card" key={`p${it.photo!.id}`}>
                      <img src={`data:image/jpeg;base64,${it.photo!.base64}`} alt={`photo ${it.photo!.timestamp}`} style={{ maxWidth: 480, borderRadius: 8 }} />
                      <div className="muted" style={{ color: "var(--muted)", fontSize: 12, marginTop: 6 }}>
                        {it.photo!.plant} · {new Date(it.photo!.timestamp).toLocaleTimeString()}
                      </div>
                    </div>
                  ) : (
                    <div className="card entry-card" key={`e${it.entry!.id}-${i}`}>
                      <div className="muted" style={{ color: "var(--muted)", fontSize: 12 }}>
                        {it.entry!.plant} · {new Date(it.entry!.timestamp).toLocaleString()} · {it.entry!.ph ? `pH ${it.entry!.ph}` : ""} {it.entry!.ec ? `· EC ${it.entry!.ec}` : ""}
                      </div>
                      {it.entry!.nutrients && <div style={{ marginTop: 4 }}>{it.entry!.nutrients}</div>}
                      {it.entry!.notes && <div style={{ marginTop: 4, color: "var(--muted)" }}>{it.entry!.notes}</div>}
                    </div>
                  ),
                )}
              </div>
            ))}
          </>
        )}

        {tab === "sensors" && (
          <>
            <div className="card">
              <h3>Latest climate</h3>
              {latest ? (
                <div className="reading-big">
                  <div><div className="val">{latest.temperature}°{latest.unit || "C"}</div><div className="lbl">Temperature</div></div>
                  <div><div className="val">{latest.humidity}%</div><div className="lbl">Humidity</div></div>
                  <div><div className="val">{latest.vpd ?? "–"}</div><div className="lbl">VPD (kPa)</div></div>
                </div>
              ) : (
                <div>No readings yet — the Pi logs them every few minutes once the AC Infinity controller is in range.</div>
              )}
            </div>
            <div className="card">
              <h3>Temperature (°C)</h3>
              <Sparkline values={tempSeries} color="var(--accent)" />
              <h3>Humidity (%)</h3>
              <Sparkline values={humSeries} color="#6bc3ff" />
            </div>
          </>
        )}

        {tab === "ai" && (
          <>
            <div className="card">
              <h3>Full history analysis — {plant}</h3>
              <p style={{ color: "var(--muted)", fontSize: 13 }}>
                Sends all photos, diary entries and climate data to Gemini for a progress report.
              </p>
              <button disabled={analysisBusy} onClick={() => { void runAnalysis(); }}>
                {analysisBusy ? "Analyzing…" : "Analyze progress"}
              </button>
            </div>
            {analysis && (
              <div className="card">
                <h3>Report</h3>
                <div className="ai-result">{analysis}</div>
              </div>
            )}

            <div className="card">
              <h3>Ask the AI</h3>
              <div className="chat-box">
                {messages.length === 0 && <div style={{ color: "var(--muted)" }}>Ask anything about this plant's grow.</div>}
                {messages.map((m, i) => (
                  <div key={i} className={`msg ${m.role}`}>
                    <div className="bubble">{m.text}</div>
                  </div>
                ))}
                <div ref={chatEnd} />
              </div>
              <form onSubmit={sendChat} style={{ marginTop: 10 }}>
                <div className="row">
                  <div>
                    <input
                      value={chatInput}
                      onChange={(e) => setChatInput(e.target.value)}
                      placeholder="e.g. should I raise the EC to 1.8?"
                    />
                  </div>
                  <button type="submit">Send</button>
                </div>
              </form>
            </div>
          </>
        )}

        {tab === "settings" && (
          <form onSubmit={saveSettings}>
            <div className="card">
              <h3>Light schedule</h3>
              <div className="row">
                <div>
                  <label>Lights on (HH:MM)</label>
                  <input value={form.lightsOn} onChange={(e) => setForm({ ...form, lightsOn: e.target.value })} placeholder="18:00" />
                </div>
                <div>
                  <label>Lights off (HH:MM)</label>
                  <input value={form.lightsOff} onChange={(e) => setForm({ ...form, lightsOff: e.target.value })} placeholder="12:00" />
                </div>
                <div>
                  <label>Photos per day</label>
                  <input type="number" min={1} max={24} value={form.photosPerDay} onChange={(e) => setForm({ ...form, photosPerDay: Number(e.target.value) })} />
                </div>
              </div>
              <div style={{ color: "var(--muted)", fontSize: 12, marginTop: 8 }}>
                Photos are spread evenly across the lights-on window — never during lights-off.
              </div>
            </div>
            <div className="card">
              <h3>Plants</h3>
              <label>Comma-separated names</label>
              <input value={form.plants} onChange={(e) => setForm({ ...form, plants: e.target.value })} placeholder="Plant A, Plant B" />
            </div>
            <div className="card">
              <h3>Change password</h3>
              <label>New password (leave blank to keep current)</label>
              <input type="password" value={form.newPassword} onChange={(e) => setForm({ ...form, newPassword: e.target.value })} />
            </div>
            <button type="submit" disabled={busy}>Save settings</button>
          </form>
        )}
      </div>

      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}
