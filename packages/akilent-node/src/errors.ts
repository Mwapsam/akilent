export interface ErrorEnvelope {
  code?: string;
  message?: string;
  request_id?: string;
  docs_url?: string;
}

export class AkilentError extends Error {
  constructor(message: string) {
    super(message);
    this.name = new.target.name;
  }
}

export class APIConnectionError extends AkilentError {}

export class APIStatusError extends AkilentError {
  readonly statusCode: number;
  readonly code?: string;
  readonly requestId?: string;
  readonly docsUrl?: string;
  readonly body: unknown;
  retryAfter?: number;

  constructor(
    message: string,
    opts: {
      statusCode: number;
      code?: string;
      requestId?: string;
      docsUrl?: string;
      body?: unknown;
      retryAfter?: number;
    },
  ) {
    super(message);
    this.statusCode = opts.statusCode;
    this.code = opts.code;
    this.requestId = opts.requestId;
    this.docsUrl = opts.docsUrl;
    this.body = opts.body;
    this.retryAfter = opts.retryAfter;
  }
}

export class ValidationError extends APIStatusError {}
export class AuthenticationError extends APIStatusError {}
export class PermissionDeniedError extends APIStatusError {}
export class NotFoundError extends APIStatusError {}
export class ConflictError extends APIStatusError {}
export class RateLimitError extends APIStatusError {}
export class ServerError extends APIStatusError {}

const BY_STATUS: Record<number, typeof APIStatusError> = {
  400: ValidationError,
  401: AuthenticationError,
  403: PermissionDeniedError,
  404: NotFoundError,
  409: ConflictError,
  429: RateLimitError,
};

export function errorFromResponse(
  status: number,
  body: unknown,
  headers: Headers,
): APIStatusError {
  const env: ErrorEnvelope =
    body && typeof body === "object" && "error" in body
      ? ((body as { error: ErrorEnvelope }).error ?? {})
      : {};
  const Cls =
    BY_STATUS[status] ?? (status >= 500 ? ServerError : APIStatusError);
  const retryHeader = headers.get("retry-after");
  return new Cls(env.message ?? `HTTP ${status}`, {
    statusCode: status,
    code: env.code,
    requestId: env.request_id ?? headers.get("x-request-id") ?? undefined,
    docsUrl: env.docs_url,
    body,
    retryAfter:
      retryHeader && /^\d+$/.test(retryHeader) ? Number(retryHeader) : undefined,
  });
}
