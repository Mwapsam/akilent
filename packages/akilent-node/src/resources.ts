import type { Transport } from "./client.js";

export interface SendParams {
  from: string;
  to: string;
  subject?: string;
  text?: string;
  html?: string;
  template?: string;
  variables?: Record<string, unknown>;
  locale?: string;
  attachments?: Array<{ filename: string; content_b64: string; content_type?: string }>;
  idempotencyKey?: string;
}

export interface Message {
  id: string;
  status: string;
  from: string;
  to: string;
  subject: string;
  template: string | null;
  campaign_id: number | null;
  created_at: string;
  sent_at: string | null;
}

export interface MessageEvent {
  id: string;
  type: string;
  source: string;
  occurred_at: string;
  data: Record<string, unknown>;
  request_id: string | null;
}

interface Page<T> {
  data: T[];
  total: number;
  limit: number;
  offset: number;
}

export class Messages {
  constructor(private readonly t: Transport) {}

  send(p: SendParams): Promise<{ id: number; public_id: string; status: string }> {
    const { idempotencyKey, variables, ...rest } = p;
    const body: Record<string, unknown> = { ...rest };
    if (variables !== undefined) body.template_variables = variables;
    return this.t.request("POST", "/api/v1/messages", { body, idempotencyKey });
  }

  list(filters: Record<string, string | number> = {}): Promise<Page<Message>> {
    return this.t.request("GET", "/api/v1/messages", { params: filters });
  }

  retrieve(id: string): Promise<Message & { events: MessageEvent[] }> {
    return this.t.request("GET", `/api/v1/messages/${id}`);
  }

  async events(id: string): Promise<MessageEvent[]> {
    const r = await this.t.request<{ data: MessageEvent[] }>(
      "GET",
      `/api/v1/messages/${id}/events`,
    );
    return r.data;
  }

  async *iterate(
    filters: Record<string, string | number> = {},
    pageSize = 100,
  ): AsyncGenerator<Message> {
    let offset = 0;
    for (;;) {
      const page = await this.list({ ...filters, limit: pageSize, offset });
      for (const m of page.data) yield m;
      offset += page.data.length;
      if (page.data.length === 0 || offset >= page.total) return;
    }
  }
}

export class Templates {
  constructor(private readonly t: Transport) {}
  list(): Promise<unknown[]> {
    return this.t.request("GET", "/api/v1/templates");
  }
  create(body: Record<string, unknown>): Promise<unknown> {
    return this.t.request("POST", "/api/v1/templates", { body });
  }
  retrieve(slug: string): Promise<unknown> {
    return this.t.request("GET", `/api/v1/templates/${slug}`);
  }
  update(slug: string, body: Record<string, unknown>): Promise<unknown> {
    return this.t.request("PATCH", `/api/v1/templates/${slug}`, { body });
  }
  remove(slug: string): Promise<void> {
    return this.t.request("DELETE", `/api/v1/templates/${slug}`);
  }
  preview(slug: string, variables: Record<string, unknown> = {}): Promise<{
    subject: string;
    html: string;
    text: string;
    missing_variables: string[];
  }> {
    return this.t.request("POST", `/api/v1/templates/${slug}/preview`, {
      body: { variables },
    });
  }
  clone(slug: string): Promise<unknown> {
    return this.t.request("POST", `/api/v1/templates/${slug}/clone`, { body: {} });
  }
}

export class Campaigns {
  constructor(private readonly t: Transport) {}
  create(body: Record<string, unknown>): Promise<unknown> {
    return this.t.request("POST", "/api/v1/campaigns", { body });
  }
  retrieve(id: number): Promise<unknown> {
    return this.t.request("GET", `/api/v1/campaigns/${id}`);
  }
}

export class RequestLogs {
  constructor(private readonly t: Transport) {}
  list(filters: Record<string, string | number> = {}): Promise<unknown> {
    return this.t.request("GET", "/api/v1/request-logs", { params: filters });
  }
  retrieve(requestId: string): Promise<unknown> {
    return this.t.request("GET", `/api/v1/request-logs/${requestId}`);
  }
}
