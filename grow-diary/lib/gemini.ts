import { GoogleGenerativeAI } from "@google/generative-ai";

const MODEL = process.env.GEMINI_MODEL || "gemini-2.0-flash";
const MAX_HISTORY_PHOTOS = 40;
const MAX_CHAT_PHOTOS = 5;

export interface HistoryPhoto {
  base64: string;
  timestamp: string;
}

export interface HistoryEntry {
  timestamp: string;
  ph?: string;
  ec?: string;
  nutrients?: string;
  notes?: string;
}

export interface HistoryReading {
  timestamp: string;
  temperature: number;
  humidity: number;
  vpd: number;
}

function model() {
  const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY || "");
  return genAI.getGenerativeModel({ model: MODEL, generationConfig: { temperature: 0.3 } });
}

function formatEntries(entries: HistoryEntry[]): string {
  if (!entries.length) return "No manual diary entries logged.";
  return entries
    .map(
      (e) =>
        `- ${e.timestamp}: pH=${e.ph || "-"}  EC=${e.ec || "-"}  nutrients="${e.nutrients || "-"}"  notes="${e.notes || "-"}"`,
    )
    .join("\n");
}

function formatClimate(readings: HistoryReading[]): string {
  if (!readings.length) return "No climate readings recorded yet.";
  const temps = readings.map((r) => r.temperature);
  const hums = readings.map((r) => r.humidity);
  const vpds = readings.map((r) => r.vpd).filter((v) => v != null);
  const avg = (xs: number[]) => (xs.reduce((a, b) => a + b, 0) / xs.length).toFixed(1);
  const min = (xs: number[]) => Math.min(...xs);
  const max = (xs: number[]) => Math.max(...xs);
  return [
    `${readings.length} readings across the grow.`,
    `Temperature: min ${min(temps).toFixed(1)}, avg ${avg(temps)}, max ${max(temps).toFixed(1)}°C.`,
    `Humidity: min ${min(hums).toFixed(1)}, avg ${avg(hums)}, max ${max(hums).toFixed(1)}% RH.`,
    vpds.length
      ? `VPD: min ${min(vpds).toFixed(2)}, avg ${avg(vpds)}, max ${max(vpds).toFixed(2)} kPa.`
      : "",
  ]
    .filter(Boolean)
    .join("\n");
}

function imageParts(photos: HistoryPhoto[]) {
  return photos.map((p) => ({
    inlineData: { mimeType: "image/jpeg", data: p.base64 },
  }));
}

export async function analyzeFullHistory(
  plant: string,
  photos: HistoryPhoto[],
  entries: HistoryEntry[],
  readings: HistoryReading[],
): Promise<string> {
  const prompt = [
    `You are an expert plant grow advisor reviewing the ENTIRE grow diary for the plant "${plant}".`,
    "",
    "Attached photos run OLDEST -> NEWEST and show the plant over time.",
    "",
    "CLIMATE HISTORY:",
    formatClimate(readings),
    "",
    "DIARY ENTRIES (manual logs, chronological):",
    formatEntries(entries),
    "",
    "Produce a full progress report covering:",
    "1. Overall trend since the first photo (improving / stable / declining) and the evidence for it.",
    "2. Every issue you can see (disease, pest damage, nutrient deficiency, watering/climate stress) with its severity, roughly when it started, and the most likely cause — using the climate and feeding data to support your reasoning.",
    "3. Whether the feeding program (nutrients + EC) and climate correlate with how the plant looks over time.",
    "4. Concrete, actionable next steps: feeding changes, watering, climate targets, and any treatment.",
    "5. A one-line 'growth stage' assessment (e.g. healthy early veg / stretching / beginning to fade).",
  ].join("\n");

  const result = await model().generateContent({
    contents: [{ role: "user", parts: [{ text: prompt }, ...imageParts(photos.slice(-MAX_HISTORY_PHOTOS))] }],
  });
  return result.response.text();
}

export async function chatReply(
  plant: string,
  photos: HistoryPhoto[],
  entries: HistoryEntry[],
  readings: HistoryReading[],
  conversation: { role: string; text: string }[],
  message: string,
): Promise<string> {
  const recent = conversation.slice(-8).map((m) => `${m.role.toUpperCase()}: ${m.text}`).join("\n");
  const prompt = [
    `You are a knowledgeable plant grow advisor. You have access to this grow's data for "${plant}".`,
    "PHOTOS:",
    photos.length ? `${photos.length} recent photos attached (oldest -> newest).` : "None available.",
    "CLIMATE:",
    formatClimate(readings),
    "DIARY:",
    formatEntries(entries),
    "",
    "Previous conversation:",
    recent || "(none)",
    "",
    `Now answer this question about the grow: ${message}`,
  ].join("\n");

  const result = await model().generateContent({
    contents: [{ role: "user", parts: [{ text: prompt }, ...imageParts(photos.slice(-MAX_CHAT_PHOTOS))] }],
  });
  return result.response.text();
}
