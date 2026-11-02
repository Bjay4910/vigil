import {
  formatTs,
  isPlainObject,
  normalizeValue,
  type Tags,
  validateName,
  validateTags,
} from "./validate.js";

export interface Point {
  name: string;
  value: number;
  ts: string;
  tags: Tags;
}

export interface VigilOptions {
  url?: string;
  apiKey?: string;
  defaultTags?: Tags;
  batchSize?: number;
  flushIntervalMs?: number;
  maxQueue?: number;
  timeoutMs?: number;
  onError?: (message: string) => void;
  fetch?: typeof fetch;
}

export interface VigilStats {
  queued: number;
  sent: number;
  dropped: number;
  failed: number;
}

export class Vigil {
  private readonly url: string;
  private readonly apiKey: string | undefined;
  private readonly defaultTags: Tags | undefined;
  private readonly batchSize: number;
  private readonly flushIntervalMs: number;
  private readonly maxQueue: number;
  private readonly timeoutMs: number;
  private readonly onError: ((message: string) => void) | undefined;
  private readonly customFetch: typeof fetch | undefined;

  private readonly queue: Point[] = [];
  private sentCount = 0;
  private droppedCount = 0;
  private failedCount = 0;

  private stopTimerFn: (() => void) | null = null;
  private inFlightFlush: Promise<void> | null = null;

  constructor(options: VigilOptions = {}) {
    const rawUrl = options.url || process.env.VIGIL_STORE_URL || "http://localhost:8000";
    this.url = rawUrl.replace(/\/+$/, "");

    const rawKey = options.apiKey !== undefined ? options.apiKey : process.env.VIGIL_API_KEY;
    this.apiKey = rawKey && rawKey.length > 0 ? rawKey : undefined;

    this.defaultTags = isPlainObject(options.defaultTags)
      ? { ...options.defaultTags }
      : options.defaultTags;

    this.batchSize = Math.max(1, Math.floor(options.batchSize ?? 100));
    this.flushIntervalMs = options.flushIntervalMs ?? 5000;
    this.maxQueue = Math.max(0, Math.floor(options.maxQueue ?? 1000));
    this.timeoutMs = options.timeoutMs ?? 5000;
    this.onError = options.onError;
    this.customFetch = options.fetch;
  }

  record(
    name: string,
    value: number | boolean,
    tags?: Tags,
    ts?: Date | string,
  ): boolean {
    let point: Point;
    try {
      const validName = validateName(name);
      const validValue = normalizeValue(value);
      const validTs = formatTs(ts);

      if (this.defaultTags !== undefined && !isPlainObject(this.defaultTags)) {
        throw new Error("tags: must be a plain object");
      }
      if (tags !== undefined && !isPlainObject(tags)) {
        throw new Error("tags: must be a plain object");
      }
      const validTags = validateTags({ ...this.defaultTags, ...tags });

      point = {
        name: validName,
        value: validValue,
        ts: validTs,
        tags: validTags,
      };
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      this.reportError(message);
      return false;
    }

    this.queue.push(point);

    while (this.queue.length > this.maxQueue) {
      this.queue.shift();
      this.droppedCount += 1;
      this.reportError("queue full: dropped oldest point");
    }

    if (this.stopTimerFn === null && this.flushIntervalMs > 0) {
      const handle = setInterval(() => {
        void this.flush();
      }, this.flushIntervalMs);
      (handle as unknown as { unref?: () => void }).unref?.();
      this.stopTimerFn = () => {
        clearInterval(handle);
      };
    }

    if (this.queue.length >= this.batchSize) {
      void this.flush();
    }

    return true;
  }

  flush(): Promise<void> {
    if (this.inFlightFlush !== null) {
      return this.inFlightFlush;
    }
    if (this.queue.length === 0) {
      return Promise.resolve();
    }
    const promise = this.runFlush().finally(() => {
      this.inFlightFlush = null;
    });
    this.inFlightFlush = promise;
    return promise;
  }

  async close(): Promise<void> {
    if (this.stopTimerFn !== null) {
      this.stopTimerFn();
      this.stopTimerFn = null;
    }
    await this.flush();
  }

  stats(): VigilStats {
    return {
      queued: this.queue.length,
      sent: this.sentCount,
      dropped: this.droppedCount,
      failed: this.failedCount,
    };
  }

  private requeueFront(batch: Point[]): void {
    this.queue.unshift(...batch);
    while (this.queue.length > this.maxQueue) {
      this.queue.shift();
      this.droppedCount += 1;
    }
  }

  private reportError(message: string): void {
    if (!this.onError) {
      return;
    }
    try {
      this.onError(message);
    } catch {
      // Never allow an onError callback to throw out of record() or flush().
    }
  }

  private async runFlush(): Promise<void> {
    try {
      const fetchFn = this.customFetch ?? globalThis.fetch;
      const headers: Record<string, string> = {
        "Content-Type": "application/json",
      };
      if (this.apiKey) {
        headers["X-API-Key"] = this.apiKey;
      }

      while (this.queue.length > 0) {
        const batch = this.queue.splice(0, this.batchSize);
        const controller = new AbortController();
        const timeoutHandle = setTimeout(() => {
          controller.abort();
        }, this.timeoutMs);
        (timeoutHandle as unknown as { unref?: () => void }).unref?.();

        let res: Response;
        try {
          res = await fetchFn(`${this.url}/ingest`, {
            method: "POST",
            headers,
            body: JSON.stringify({ points: batch }),
            signal: controller.signal,
          });
        } catch (err) {
          clearTimeout(timeoutHandle);
          this.requeueFront(batch);
          const message = controller.signal.aborted
            ? `ingest request timed out after ${this.timeoutMs}ms`
            : err instanceof Error
              ? err.message
              : String(err);
          this.reportError(message);
          break;
        }
        clearTimeout(timeoutHandle);

        if (res.status >= 200 && res.status < 300) {
          if (typeof res.text === "function") {
            await res.text().catch(() => {});
          }
          this.sentCount += batch.length;
          continue;
        }

        if (res.status === 400) {
          let errorMsg = "ingest rejected batch with status 400";
          try {
            if (typeof res.json === "function") {
              const body: unknown = await res.json();
              if (
                typeof body === "object" &&
                body !== null &&
                "error" in body &&
                typeof (body as { error: unknown }).error === "string" &&
                (body as { error: string }).error.length > 0
              ) {
                errorMsg = (body as { error: string }).error;
              }
            }
          } catch {
            // Ignore JSON parse errors and fall back to default status message.
          }
          this.failedCount += batch.length;
          this.reportError(errorMsg);
          continue;
        }

        if (typeof res.text === "function") {
          await res.text().catch(() => {});
        }
        this.requeueFront(batch);
        this.reportError(`ingest failed with status ${res.status}`);
        break;
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      this.reportError(message);
    }
  }
}

