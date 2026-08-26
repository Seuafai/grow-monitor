import { cert, getApps, initializeApp } from "firebase-admin/app";
import { getFirestore, type Firestore } from "firebase-admin/firestore";

let _db: Firestore | null = null;

export function adminDb(): Firestore {
  if (!_db) {
    const projectId = process.env.FIREBASE_PROJECT_ID;
    const clientEmail = process.env.FIREBASE_CLIENT_EMAIL;
    const privateKey = (process.env.FIREBASE_PRIVATE_KEY || "").replace(/\\n/g, "\n");
    if (!projectId || !clientEmail || !privateKey) {
      throw new Error(
        "Firebase credentials not configured (FIREBASE_PROJECT_ID, FIREBASE_CLIENT_EMAIL, FIREBASE_PRIVATE_KEY)",
      );
    }
    const app =
      getApps()[0] || initializeApp({ credential: cert({ projectId, clientEmail, privateKey }) });
    _db = getFirestore(app);
  }
  return _db;
}
