import { NextResponse } from "next/server";
import { isAuthed } from "@/lib/auth";
import { adminDb } from "@/lib/firebaseAdmin";

export const runtime = "nodejs";

export async function GET(req: Request) {
  if (!(await isAuthed())) return NextResponse.json({ error: "unauthorized" }, { status: 401 });

  const { searchParams } = new URL(req.url);
  const plant = searchParams.get("plant");
  const limit = Math.min(Number(searchParams.get("limit") || 90), 200);

  let query = adminDb().collection("photos").orderBy("timestamp", "desc").limit(limit);
  if (plant) query = query.where("plant", "==", plant);

  const snap = await query.get();
  const photos = snap.docs.map((doc) => ({
    id: doc.id,
    ...(doc.data() as Record<string, unknown>),
  }));
  return NextResponse.json({ photos });
}
