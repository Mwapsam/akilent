import { call } from "./api.mjs";
import { configPath, loadConfig, resolveAuth, saveConfig } from "./config.mjs";
import { listen as webhookListen } from "./webhooks.mjs";

const HELP = `akilent — command-line interface for the Akilent email API

Usage:
  akilent login --key <ak_...> [--base-url <url>]
  akilent whoami
  akilent send --from <addr> --to <addr> [--subject <s>] [--text <t>] [--html <h>] [--template <slug>]
  akilent messages [--status <s>] [--to <addr>] [--limit <n>]
  akilent events <message_id>
  akilent logs tail [--interval <seconds>]
  akilent templates pull <slug> [--out <file.json>]
  akilent templates push <slug> --in <file.json>
  akilent webhooks test --event <type> [--data <json>]
  akilent webhooks listen [--port <n>] [--secret <whsec_...>] [--forward-to <url>]

Auth resolution order: --key flag, $AKILENT_API_KEY, ${"~/.akilent/config.json"}.
`;

export function parseArgs(argv) {
  const positional = [];
  const flags = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith("--")) {
      const key = a.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith("--")) {
        flags[key] = true;
      } else {
        flags[key] = next;
        i++;
      }
    } else {
      positional.push(a);
    }
  }
  return { positional, flags };
}

async function main(argv, { out = console.log, err = console.error } = {}) {
  const { positional, flags } = parseArgs(argv);
  const cmd = positional[0];

  if (!cmd || flags.help || cmd === "help") {
    out(HELP);
    return 0;
  }

  if (cmd === "login") {
    if (!flags.key) {
      err("login: --key is required");
      return 1;
    }
    const patch = { apiKey: flags.key };
    if (flags["base-url"]) patch.baseUrl = flags["base-url"];
    const path = saveConfig(patch);
    out(`Saved credentials to ${path}`);
    return 0;
  }

  const { apiKey, baseUrl } = resolveAuth(flags);
  if (!apiKey && cmd !== "config") {
    err("No API key. Run `akilent login --key ak_...` or set $AKILENT_API_KEY.");
    return 1;
  }

  try {
    switch (cmd) {
      case "whoami": {
        const cfg = loadConfig();
        out(JSON.stringify({ baseUrl, keySuffix: apiKey ? apiKey.slice(-4) : null, config: configPath() }, null, 2));
        return 0;
      }
      case "send": {
        for (const req of ["from", "to"]) {
          if (!flags[req]) {
            err(`send: --${req} is required`);
            return 1;
          }
        }
        const body = { from: flags.from, to: flags.to };
        for (const k of ["subject", "text", "html", "template"]) {
          if (flags[k] && flags[k] !== true) body[k] = flags[k];
        }
        const r = await call(baseUrl, apiKey, "POST", "/api/v1/messages", { body });
        out(JSON.stringify(r, null, 2));
        return 0;
      }
      case "messages": {
        const r = await call(baseUrl, apiKey, "GET", "/api/v1/messages", {
          params: { status: flags.status, to: flags.to, limit: flags.limit || 20 },
        });
        for (const m of r.data || []) out(`${m.id}\t${m.status}\t${m.to}\t${m.subject || ""}`);
        return 0;
      }
      case "events": {
        const id = positional[1];
        if (!id) {
          err("events: message id is required");
          return 1;
        }
        const r = await call(baseUrl, apiKey, "GET", `/api/v1/messages/${id}/events`);
        for (const e of r.data || []) out(`${e.occurred_at}\t${e.type}\t${e.source}`);
        return 0;
      }
      case "logs": {
        if (positional[1] !== "tail") {
          err("usage: akilent logs tail [--interval <seconds>]");
          return 1;
        }
        const interval = Number(flags.interval || 5) * 1000;
        const seen = new Set();
        out(`Tailing ${baseUrl}/api/v1/messages every ${interval / 1000}s — Ctrl-C to stop`);
        for (;;) {
          const r = await call(baseUrl, apiKey, "GET", "/api/v1/messages", { params: { limit: 20 } });
          for (const m of (r.data || []).slice().reverse()) {
            if (seen.has(m.id)) continue;
            seen.add(m.id);
            out(`${m.created_at}\t${m.id}\t${m.status}\t${m.to}`);
          }
          await new Promise((res) => setTimeout(res, interval));
        }
      }
      case "templates": {
        const sub = positional[1];
        const slug = positional[2];
        if (!slug) {
          err("usage: akilent templates <pull|push> <slug> ...");
          return 1;
        }
        if (sub === "pull") {
          const t = await call(baseUrl, apiKey, "GET", `/api/v1/templates/${slug}`);
          const json = JSON.stringify(t, null, 2);
          if (flags.out && flags.out !== true) {
            const { writeFileSync } = await import("node:fs");
            writeFileSync(flags.out, json + "\n");
            out(`Wrote ${flags.out}`);
          } else {
            out(json);
          }
          return 0;
        }
        if (sub === "push") {
          if (!flags.in || flags.in === true) {
            err("templates push: --in <file.json> is required");
            return 1;
          }
          const { readFileSync } = await import("node:fs");
          const local = JSON.parse(readFileSync(flags.in, "utf8"));
          const body = {};
          for (const k of ["name", "subject", "text", "html", "content_blocks", "builder_mode"]) {
            if (local[k] !== undefined) body[k] = local[k];
          }
          const r = await call(baseUrl, apiKey, "PATCH", `/api/v1/templates/${slug}`, { body });
          out(JSON.stringify(r, null, 2));
          return 0;
        }
        err("templates: expected `pull` or `push`");
        return 1;
      }
      case "webhooks": {
        const sub = positional[1];
        if (sub === "test") {
          if (!flags.event || flags.event === true) {
            err("webhooks test: --event <type> is required");
            return 1;
          }
          let data = {};
          if (flags.data && flags.data !== true) {
            try {
              data = JSON.parse(flags.data);
            } catch {
              err("webhooks test: --data must be valid JSON");
              return 1;
            }
          }
          const r = await call(baseUrl, apiKey, "POST", "/api/v1/webhooks/test", {
            body: { event: flags.event, data },
          });
          out(JSON.stringify(r, null, 2));
          return 0;
        }
        if (sub === "listen") {
          const port = Number(flags.port || 4318);
          const secret =
            (flags.secret && flags.secret !== true && flags.secret) ||
            loadConfig().webhookSecret ||
            null;
          const forwardTo = flags["forward-to"] && flags["forward-to"] !== true ? flags["forward-to"] : null;
          const { port: bound } = await webhookListen({ port, secret, forwardTo, out });
          out(`Listening for webhooks on http://localhost:${bound}/`);
          out(secret ? "Signatures will be verified." : "No --secret given — signatures will not be verified.");
          if (forwardTo) out(`Forwarding each request to ${forwardTo}`);
          out("Point a webhook endpoint (via your own tunnel) at this URL. Ctrl-C to stop.");
          await new Promise(() => {}); // run until killed
          return 0;
        }
        err("usage: akilent webhooks <test|listen> ...");
        return 1;
      }
      default:
        err(`Unknown command: ${cmd}`);
        out(HELP);
        return 1;
    }
  } catch (e) {
    err(`Error: ${e.message}${e.code ? ` (${e.code})` : ""}${e.requestId ? ` [request ${e.requestId}]` : ""}`);
    return 1;
  }
}

export { main };
