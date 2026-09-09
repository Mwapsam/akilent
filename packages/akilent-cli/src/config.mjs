import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

/** Resolved per call so tests (and $AKILENT_CONFIG) can redirect it. */
export function configPath() {
  return process.env.AKILENT_CONFIG || join(homedir(), ".akilent", "config.json");
}

export function loadConfig() {
  try {
    return JSON.parse(readFileSync(configPath(), "utf8"));
  } catch {
    return {};
  }
}

export function saveConfig(patch) {
  const path = configPath();
  const next = { ...loadConfig(), ...patch };
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(next, null, 2) + "\n", { mode: 0o600 });
  return path;
}

export function resolveAuth(flags) {
  const cfg = loadConfig();
  const apiKey = flags.key || process.env.AKILENT_API_KEY || cfg.apiKey;
  const baseUrl =
    flags["base-url"] ||
    process.env.AKILENT_BASE_URL ||
    cfg.baseUrl ||
    "https://akilent.com";
  return { apiKey, baseUrl };
}
