import { randomUUID } from "node:crypto";

/** Minimal API caller — the CLI stays dependency-free rather than bundling the SDK. */
export async function call(baseUrl, apiKey, method, path, { params, body } = {}) {
  const url = new URL(baseUrl.replace(/\/$/, "") + path);
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
  }
  const headers = {
    authorization: `Bearer ${apiKey}`,
    accept: "application/json",
    "user-agent": "akilent-cli/0.1.0",
  };
  if (body !== undefined) {
    headers["content-type"] = "application/json";
    if (method === "POST") headers["idempotency-key"] = randomUUID();
  }
  const res = await fetch(url, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let parsed;
  try {
    parsed = text ? JSON.parse(text) : undefined;
  } catch {
    parsed = text;
  }
  if (!res.ok) {
    const err = parsed && parsed.error ? parsed.error : {};
    const e = new Error(err.message || `HTTP ${res.status}`);
    e.status = res.status;
    e.code = err.code;
    e.requestId = err.request_id || res.headers.get("x-request-id");
    throw e;
  }
  return parsed;
}
