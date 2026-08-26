import { NextResponse } from "next/server";
import { AUTH_COOKIE, signToken, verifyPassword, isAuthed } from "@/lib/auth";
import { getSettings } from "@/lib/db";

export const runtime = "nodejs";

export async function GET() {
  return NextResponse.json({ authed: await isAuthed() });
}

export async function POST(req: Request) {
  const { password } = await req.json().catch(() => ({}));
  if (typeof password !== "string") {
    return NextResponse.json({ ok: false }, { status: 400 });
  }

  const settings = await getSettings();
  const stored = settings.passwordHash;
  const fallback = process.env.APP_PASSWORD;

  const matches =
    (stored && verifyPassword(password, stored)) || (!stored && fallback && password === fallback);
  if (!matches) {
    return NextResponse.json({ ok: false }, { status: 401 });
  }

  const res = NextResponse.json({ ok: true });
  res.cookies.set(AUTH_COOKIE, signToken(process.env.APP_SECRET || ""), {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: 60 * 60 * 24 * 30,
  });
  return res;
}
