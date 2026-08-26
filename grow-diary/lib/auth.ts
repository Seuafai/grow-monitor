import { createHmac, randomBytes, scryptSync, timingSafeEqual } from "crypto";
import { cookies } from "next/headers";

export function hashPassword(password: string): string {
  const salt = randomBytes(16).toString("hex");
  const hash = scryptSync(password, salt, 64).toString("hex");
  return `${salt}:${hash}`;
}

export function verifyPassword(password: string, stored: string): boolean {
  const [salt, hash] = stored.split(":");
  if (!salt || !hash) return false;
  const candidate = scryptSync(password, salt, 64);
  const expected = Buffer.from(hash, "hex");
  return candidate.length === expected.length && timingSafeEqual(candidate, expected);
}

export function signToken(secret: string): string {
  const payload = "grow-diary-admin";
  const sig = createHmac("sha256", secret).update(payload).digest("hex");
  return `${payload}.${sig}`;
}

export function validToken(token: string | undefined, secret: string): boolean {
  if (!token || !secret) return false;
  const [payload, sig] = token.split(".");
  const expected = createHmac("sha256", secret).update(payload).digest("hex");
  return !!sig && sig === expected;
}

export const AUTH_COOKIE = "grow_auth";

export async function getAuthCookie() {
  return (await cookies()).get(AUTH_COOKIE)?.value;
}

export async function isAuthed(): Promise<boolean> {
  return validToken(await getAuthCookie(), process.env.APP_SECRET || "");
}
