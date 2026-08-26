import { NextResponse } from "next/server";
import { isAuthed } from "@/lib/auth";
import { adminDb } from "@/lib/firebaseAdmin";
import { chatReply } from "@/lib/gemini";

export const runtime = "nodejs";
export const maxDuration = 120;

export async function GET(req: Request) {
  if (!(await isAuthed())) return NextResponse.json({ error: "unauthorized" }, { status: 401 });

  const { searchParams } = new URL(req.url);
  const plant = searchParams.get("plant") || "Plant A";

  const snap = await adminDb()
    .collection("chat")
    .where("plant", "==", plant)
    .orderBy("timestamp", "asc")
    .limit(200)
    .get();
  const messages = snap.docs.map((doc) => doc.data());
  return NextResponse.json({ messages });
}

export async function POST(req: Request) {
  if (!(await isAuthed())) return NextResponse.json({ error: "unauthorized" }, { status: 401 });

  const body = await req.json().catch(() => ({}));
  const plant = typeof body.plant === "string" && body.plant.trim() ? body.plant.trim() : "Plant A";
  const message = typeof body.message === "string" ? body.message.trim() : "";
  if (!message) return NextResponse.json({ ok: false, error: "empty message" }, { status: 400 });

  try {
    const historySnap = await adminDb()
      .collection("chat")
      .where("plant", "==", plant)
      .orderBy("timestamp", "asc")
      .limit(200)
      .get();
    const conversation = historySnap.docs.map((doc) => doc.data() as { role: string; text: string });

    const photosSnap = await adminDb()
      .collection("photos")
      .where("plant", "==", plant)
      .orderBy("timestamp", "asc")
      .limit(10)
      .get();
    const photos = photosSnap.docs.map((doc) => {
      const d = doc.data() as { base64?: string; timestamp?: string };
      return { base64: d.base64 || "", timestamp: d.timestamp || "" };
    });

    const entriesSnap = await adminDb().collection("entries").where("plant", "==", plant).get();
    const entries = entriesSnap.docs.map((doc) => doc.data() as Record<string, unknown>);

    const readingsSnap = await adminDb().collection("readings").orderBy("timestamp", "desc").limit(200).get();
    const readings = readingsSnap.docs.map((doc) => doc.data() as Record<string, unknown>);

    await adminDb().collection("chat").add({
      plant,
      role: "user",
      text: message,
      timestamp: new Date().toISOString(),
    });

    const reply = await chatReply(
      plant,
      photos,
      entries as never,
      readings as never,
      [...conversation, { role: "user", text: message }],
      message,
    );

    await adminDb().collection("chat").add({
      plant,
      role: "assistant",
      text: reply,
      timestamp: new Date().toISOString(),
    });

    return NextResponse.json({ ok: true, reply });
  } catch (err) {
    return NextResponse.json(
      { ok: false, error: err instanceof Error ? err.message : "chat failed" },
      { status: 502 },
    );
  }
}
