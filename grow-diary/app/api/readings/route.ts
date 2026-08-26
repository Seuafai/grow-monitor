import { NextResponse } from "next/server";
import { isAuthed } from "@/lib/auth";
import { adminDb } from "@/lib/firebaseAdmin";

export const runtime = "nodejs";

export async function GET(req: Request) {
  if (!(await isAuthed())) return NextResponse.json({ error: "unauthorized" }, { status: 401 });

  const snap = await adminDb().collection("readings").orderBy("timestamp", "desc").limit(300).get();
  const readings = snap.docs.map((doc) => doc.data() as Record<string, unknown>);
  return NextResponse.json({ readings });
}
