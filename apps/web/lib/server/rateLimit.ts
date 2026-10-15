// In-memory sliding-window rate limiter, per key (usually client IP). Per server instance only:
// on Vercel each warm instance keeps its own window, which is enough to blunt abuse.

export interface LimitResult {
  ok: boolean;
  remaining: number;
  retryAfterS: number;
}

export class RateLimiter {
  private hits = new Map<string, number[]>();

  constructor(
    readonly max: number,
    readonly windowMs: number,
    private now: () => number = Date.now,
    private maxKeys = 10_000,
  ) {}

  private recent(key: string): number[] {
    const cutoff = this.now() - this.windowMs;
    const list = (this.hits.get(key) ?? []).filter((t) => t > cutoff);
    if (list.length) this.hits.set(key, list);
    else this.hits.delete(key);
    return list;
  }

  private result(list: number[]): LimitResult {
    const remaining = Math.max(0, this.max - list.length);
    const retryAfterS =
      remaining > 0 ? 0 : Math.max(1, Math.ceil((list[0] + this.windowMs - this.now()) / 1000));
    return { ok: remaining > 0, remaining, retryAfterS };
  }

  /** Check without counting. */
  peek(key: string): LimitResult {
    return this.result(this.recent(key));
  }

  /** Count one hit; ok=false once the window is full (the blocked hit is not counted). */
  hit(key: string): LimitResult {
    const list = this.recent(key);
    if (list.length >= this.max) return this.result(list);
    list.push(this.now());
    this.hits.set(key, list);
    if (this.hits.size > this.maxKeys) {
      const oldest = this.hits.keys().next().value;
      if (oldest !== undefined) this.hits.delete(oldest);
    }
    const r = this.result(list);
    return { ...r, ok: true };
  }

  reset(key: string): void {
    this.hits.delete(key);
  }

  size(): number {
    return this.hits.size;
  }
}

// ASSUMED limits: 5 orders per IP per 10 minutes; 5 wrong access codes per IP per 15 minutes.
type Limiters = { orders: RateLimiter; access: RateLimiter };
const g = globalThis as unknown as { __p2pLimiters?: Limiters };
export const limiters: Limiters = (g.__p2pLimiters ??= {
  orders: new RateLimiter(5, 10 * 60_000),
  access: new RateLimiter(5, 15 * 60_000),
});
