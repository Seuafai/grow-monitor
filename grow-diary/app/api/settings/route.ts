import { NextResponse } from "next/server";
import { isAuthed } from "@/lib/auth";
import { getSettings, saveSettings, settingsForClient } from "@/lib/db";
import { hashPassword } from "@/lib/auth";

export const runtime = "nodejs";

export async function GET() {
  if (!(await isAuthed())) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  const settings = await getSettings();
  return NextResponse.json({ settings: settingsForClient(settings) });
}

export async function PUT(req: Request) {
  if (!(await isAuthed())) return NextResponse.json({ error: "unauthorized" }, { status: 401 });

  const body = await req.json().catch(() => ({}));
  const patch: Record<string, unknown> = {};

  if (typeof body.lightsOn === "string" && /^([01]\d|2[0-3]):[0-5]\d$/.test(body.lightsOn)) {
    patch.lightsOn = body.lightsOn;
  }
  if (typeof body.lightsOff === "string" && /^([01]\d|2[0-3]):[0-5]\d$/.test(body.lightsOff)) {
    patch.lightsOff = body.lightsOff;
  }
  if (Number.isInteger(body.photosPerDay) && body.photosPerDay >= 1 && body.photosPerDay <= 24) {
    patch.photosPerDay = body.photosPerDay;
  }
  if (Array.isArray(body.plants)) {
    const plants = body.plants
      .map((p: unknown) => (typeof p === "string" ? p.trim() : ""))
      .filter(Boolean);
    if (plants.length) patch.plants = plants;
  }
  if (typeof body.newPassword === "string" && body.newPassword.length >= 4) {
    patch.passwordHash = hashPassword(body.newPassword);
  }

  if (Object.keys(patch).length) {
    await saveSettings(patch);
  }
  const settings = await getSettings();
  return NextResponse.json({ ok: true, settings: settingsForClient(settings) });
}
