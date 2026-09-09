import { Transport, type AkilentOptions } from "./client.js";
import { Campaigns, Messages, RequestLogs, Templates } from "./resources.js";

export * from "./errors.js";
export * as webhooks from "./webhooks.js";
export type { SendParams, Message, MessageEvent } from "./resources.js";
export type { AkilentOptions } from "./client.js";

/**
 * Akilent API client.
 *
 * ```ts
 * import { Akilent } from "akilent";
 * const client = new Akilent({ apiKey: process.env.AKILENT_API_KEY! });
 * const msg = await client.messages.send({
 *   from: "billing@acme.com",
 *   to: "customer@example.com",
 *   subject: "Your receipt",
 *   text: "Thanks!",
 * });
 * ```
 */
export class Akilent {
  readonly messages: Messages;
  readonly templates: Templates;
  readonly campaigns: Campaigns;
  readonly requestLogs: RequestLogs;

  constructor(options: AkilentOptions | string) {
    const opts = typeof options === "string" ? { apiKey: options } : options;
    const transport = new Transport(opts);
    this.messages = new Messages(transport);
    this.templates = new Templates(transport);
    this.campaigns = new Campaigns(transport);
    this.requestLogs = new RequestLogs(transport);
  }
}

export default Akilent;
