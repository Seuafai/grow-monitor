import { adminDb } from "./firebaseAdmin";

export interface Settings {
  lightsOn: string;
  lightsOff: string;
  photosPerDay: number;
  plants: string[];
  passwordHash?: string;
}

export const DEFAULT_SETTINGS: Settings = {
  lightsOn: "18:00",
  lightsOff: "12:00",
  photosPerDay: 4,
  plants: ["Plant A"],
};

export async function getSettings(): Promise<Settings> {
  const doc = await adminDb().collection("settings").doc("app").get();
  if (!doc.exists) return { ...DEFAULT_SETTINGS };
  return { ...DEFAULT_SETTINGS, ...doc.data() } as Settings;
}

export async function saveSettings(patch: Partial<Settings>) {
  await adminDb().collection("settings").doc("app").set(patch, { merge: true });
}

export function settingsForClient(settings: Settings) {
  const { passwordHash: _pw, ...rest } = settings;
  return rest;
}
