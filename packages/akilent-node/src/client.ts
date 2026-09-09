import { APIConnectionError, errorFromResponse } from "./errors.js";

const DEFAULT_BASE_URL = "https://akilent.com";
const USER_AGENT = "akilent-node/0.1.0";
const RETRY_STATUSES = new Set([429, 500, 502, 503, 504]);

export interface AkilentOptions {
  apiKey: string;
  baseUrl?: string;
  timeoutMs?: number;
  maxRetries?: number;
  fetch?: typeof fetch;
}

export interface RequestOptions {
  params?: Record<string, string | number | undefined>;
  body?: unknown;
  idempotencyKey?: string | null;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const backoff = (attempt: number) => Math.min(500 * 2 ** (attempt - 1), 8000);

export class Transport {
  private readonly apiKey: string;
  private readonly baseUrl: string;
  private readonly timeoutMs: number;
  private readonly maxRetries: number;
  private readonly fetchImpl: typeof fetch;

  constructor(opts: AkilentOptions) {
    if (!opts.apiKey) throw new Error("apiKey is required");
    this.apiKey = opts.apiKey;
    this.baseUrl = (opts.baseUrl ?? DEFAULT_BASE_URL).replace(/\/$/, "");
    this.timeoutMs = opts.timeoutMs ?? 30_000;
    this.maxRetries = opts.maxRetries ?? 2;
    this.fetchImpl = opts.fetch ?? globalThis.fetch;
    if (!this.fetchImpl) throw new Error("global fetch unavailable; pass options.fetch");
  }

  async request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
    const url = new URL(this.baseUrl + path);
    for (const [k, v] of Object.entries(opts.params ?? {})) {
      if (v !== undefined) url.searchParams.set(k, String(v));
    }

    const headers: Record<string, string> = {
      authorization: `Bearer ${this.apiKey}`,
      "user-agent": USER_AGENT,
      accept: "application/json",
    };
    let idem = opts.idempotencyKey;
    if (method === "POST" && idem === undefined) idem = crypto.randomUUID();
    if (idem) headers["idempotency-key"] = idem;
    if (opts.body !== undefined) headers["content-type"] = "application/json";

    let attempt = 0;
    for (;;) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), this.timeoutMs);
      let res: Response;
      try {
        res = await this.fetchImpl(url, {
          method,
          headers,
          body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
          signal: controller.signal,
        });
      } catch (err) {
        clearTimeout(timer);
        if (attempt < this.maxRetries) {
          attempt += 1;
          await sleep(backoff(attempt));
          continue;
        }
        throw new APIConnectionError((err as Error).message);
      }
      clearTimeout(timer);

      if (RETRY_STATUSES.has(res.status) && attempt < this.maxRetries) {
        attempt += 1;
        const ra = res.headers.get("retry-after");
        await sleep(ra && /^\d+$/.test(ra) ? Number(ra) * 1000 : backoff(attempt));
        continue;
      }

      const text = await res.text();
      const parsed = text ? safeJson(text) : undefined;
      if (res.status >= 400) throw errorFromResponse(res.status, parsed, res.headers);
      return parsed as T;
    }
  }
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}
