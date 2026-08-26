import { NextResponse } from "next/server";
import { isAuthed } from "@/lib/auth";
import { adminDb } from "@/lib/firebaseAdmin";

export const runtime = "nodejs";

export async function GET(req: Request) {
  if (!(await isAuthed())) return NextResponse.json({ error: "unauthorized" }, { status: 401 });

  const { searchParams } = new URL(req.url);
  const plant = searchParams.get("plant");

  let query = adminDb().collection("entries").orderBy("timestamp", "desc").limit(500);
  if (plant) query = query.where("plant", "==", plant);

  const snap = await query.get();
  const entries = snap.docs.map((doc) => ({ id: doc.id, ...doc.data() }));
  return NextResponse.json({ entries });
}

export async function POST(req: Request) {
  if (!(await isAuthed())) return NextResponse.json({ error: "unauthorized" }, { status: 401 });

  const body = await req.json().catch(() => ({}));
  const plant = typeof body.plant === "string" && body.plant.trim() ? body.plant.trim() : "Plant A";

  const entry: Record<string, unknown> = {
    timestamp: new Date().toISOString(),
    plant,
  };

  if (typeof body.ph === "number" && body.ph >= 0 && body.ph <= 14) entry.ph = String(body.ph);
  if (typeof body.ec === "number" && body.ec >= 0.2 && body.ec <= 3.6) entry.ec = String(body.ec);
  if (typeof body.nutrients === "string") entry.nutrients = body.nutrients.trim();
  if (typeof body.notes === "string") entry.notes = body.notes.trim();

  const doc = await adminDb().collection("entries").add(entry);
  return NextResponse.json({ ok: true, id: doc.id });
}
