import { createHmac, timingSafeEqual } from "node:crypto";

const TOLERANCE_SECONDS = 300;

export class SignatureVerificationError extends Error {}

/**
 * Verify an inbound Akilent webhook signature.
 *
 * @param payload  Raw request body (string or Buffer) — must be the exact bytes received.
 * @param header   The `X-Akilent-Signature` header value, `t=<unix>,v1=<hex>`.
 * @param secret   The endpoint's signing secret (`whsec_…`).
 */
export function verify(
  payload: string | Buffer,
  header: string,
  secret: string,
  toleranceSeconds = TOLERANCE_SECONDS,
): true {
  const parts = Object.fromEntries(
    header.split(",").map((p) => {
      const i = p.indexOf("=");
      return [p.slice(0, i), p.slice(i + 1)];
    }),
  );
  const timestamp = Number(parts.t);
  const received = parts.v1;
  if (!Number.isFinite(timestamp) || !received) {
    throw new SignatureVerificationError("malformed signature header");
  }
  if (Math.abs(Date.now() / 1000 - timestamp) > toleranceSeconds) {
    throw new SignatureVerificationError("timestamp outside tolerance window");
  }
  const body = typeof payload === "string" ? Buffer.from(payload) : payload;
  const signed = Buffer.concat([Buffer.from(`${timestamp}.`), body]);
  const expected = createHmac("sha256", secret).update(signed).digest("hex");
  const a = Buffer.from(expected);
  const b = Buffer.from(received);
  if (a.length !== b.length || !timingSafeEqual(a, b)) {
    throw new SignatureVerificationError("signature mismatch");
  }
  return true;
}
