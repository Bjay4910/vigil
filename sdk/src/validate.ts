export type TagKey = "client" | "site" | "env" | "service";
export type Tags = Record<string, string>;

export const ALLOWED_TAG_KEYS: ReadonlySet<string> = new Set<TagKey>([
  "client",
  "site",
  "env",
  "service",
]);

const NAME_RE = /^[a-z][a-z0-9_]*$/;
const TS_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;

export function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const proto: unknown = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

export function validateName(name: unknown): string {
  if (typeof name !== "string") {
    throw new Error("name: must be a string");
  }
  if (!NAME_RE.test(name)) {
    throw new Error("name: must match ^[a-z][a-z0-9_]*$");
  }
  return name;
}

export function normalizeValue(value: number | boolean): number {
  if (typeof value === "boolean") {
    return value ? 1 : 0;
  }
  if (typeof value !== "number") {
    throw new Error("value: must be a number");
  }
  if (!Number.isFinite(value)) {
    throw new Error("value: must be a finite number");
  }
  return value === 0 ? 0 : value;
}

export function validateTags(tags: unknown): Tags {
  if (!isPlainObject(tags)) {
    throw new Error("tags: must be a plain object");
  }
  if (Object.getOwnPropertySymbols(tags).length > 0) {
    throw new Error("tags: keys must be strings");
  }

  const out: Tags = {};
  for (const [key, val] of Object.entries(tags)) {
    if (!ALLOWED_TAG_KEYS.has(key)) {
      throw new Error(`tags: unknown tag key '${key}'`);
    }
    if (typeof val !== "string" || val.length === 0) {
      throw new Error(`tags: value for '${key}' must be a non-empty string`);
    }
    if (val !== val.toLowerCase()) {
      throw new Error(`tags: value for '${key}' must be lowercase`);
    }
    out[key] = val;
  }
  return out;
}

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

function formatDateUTC(date: Date): string {
  const year = date.getUTCFullYear();
  if (year < 0 || year > 9999) {
    throw new Error("ts: year must be between 0000 and 9999");
  }
  const y = String(year).padStart(4, "0");
  const m = pad2(date.getUTCMonth() + 1);
  const d = pad2(date.getUTCDate());
  const hh = pad2(date.getUTCHours());
  const mm = pad2(date.getUTCMinutes());
  const ss = pad2(date.getUTCSeconds());
  return `${y}-${m}-${d}T${hh}:${mm}:${ss}Z`;
}

export function formatTs(ts?: Date | string): string {
  if (ts === undefined) {
    return formatDateUTC(new Date());
  }
  if (ts instanceof Date) {
    if (Number.isNaN(ts.getTime())) {
      throw new Error("ts: invalid Date");
    }
    return formatDateUTC(ts);
  }
  if (typeof ts === "string") {
    if (!TS_RE.test(ts)) {
      throw new Error("ts: must match YYYY-MM-DDTHH:MM:SSZ format");
    }
    const parsed = new Date(ts);
    if (Number.isNaN(parsed.getTime()) || formatDateUTC(parsed) !== ts) {
      throw new Error("ts: invalid date/time");
    }
    return ts;
  }
  throw new Error("ts: must be a Date or ISO 8601 UTC string");
}

