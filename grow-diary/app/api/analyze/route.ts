import { NextResponse } from "next/server";
import { isAuthed } from "@/lib/auth";
import { adminDb } from "@/lib/firebaseAdmin";
import { analyzeFullHistory } from "@/lib/gemini";

export const runtime = "nodejs";
export const maxDuration = 120;

export async function POST(req: Request) {
  if (!(await isAuthed())) return NextResponse.json({ error: "unauthorized" }, { status: 401 });

  const body = await req.json().catch(() => ({}));
  const plant = typeof body.plant === "string" && body.plant.trim() ? body.plant.trim() : "Plant A";

  try {
    const photosSnap = await adminDb()
      .collection("photos")
      .where("plant", "==", plant)
      .orderBy("timestamp", "asc")
      .limit(50)
      .get();
    const entriesSnap = await adminDb()
      .collection("entries")
      .where("plant", "==", plant)
      .orderBy("timestamp", "asc")
      .limit(300)
      .get();
    const readingsSnap = await adminDb().collection("readings").orderBy("timestamp", "desc").limit(500).get();

    const photos = photosSnap.docs.map((doc) => {
      const d = doc.data() as { base64?: string; timestamp?: string };
      return { base64: d.base64 || "", timestamp: d.timestamp || "" };
    });
    const entries = entriesSnap.docs.map((doc) => doc.data() as Record<string, unknown>);
    const readings = readingsSnap.docs
      .map((doc) => doc.data() as Record<string, unknown>)
      .reverse();

    const result = await analyzeFullHistory(
      plant,
      photos,
      entries as never,
      readings as never,
    );

    await adminDb().collection("analyses").add({
      plant,
      timestamp: new Date().toISOString(),
      result,
    });

    return NextResponse.json({ ok: true, result });
  } catch (err) {
    return NextResponse.json(
      { ok: false, error: err instanceof Error ? err.message : "analysis failed" },
      { status: 502 },
    );
  }
}
