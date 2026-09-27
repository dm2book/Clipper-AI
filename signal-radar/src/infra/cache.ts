import { Redis } from 'ioredis';
import type { Logger } from './logger.js';

/**
 * Cache abstraction (docs/ARCHITECTURE.md §H). The cache is an optimisation:
 * every read may miss, and a broken Redis must degrade to "slower", never to
 * "wrong" or "down". Values must be JSON-serialisable.
 */
export interface Cache {
  readonly kind: 'memory' | 'redis';
  get<T>(key: string): Promise<T | undefined>;
  set(key: string, value: unknown, ttlMs: number): Promise<void>;
  delete(key: string): Promise<void>;
  close(): Promise<void>;
}

export class MemoryCache implements Cache {
  readonly kind = 'memory' as const;
  private readonly entries = new Map<string, { value: string; expiresAt: number }>();

  constructor(
    private readonly maxEntries = 10_000,
    private readonly now: () => number = Date.now,
  ) {}

  async get<T>(key: string): Promise<T | undefined> {
    const hit = this.entries.get(key);
    if (!hit) return undefined;
    if (hit.expiresAt <= this.now()) {
      this.entries.delete(key);
      return undefined;
    }
    // LRU: re-insert to mark as most recently used.
    this.entries.delete(key);
    this.entries.set(key, hit);
    return JSON.parse(hit.value) as T;
  }

  async set(key: string, value: unknown, ttlMs: number): Promise<void> {
    this.entries.delete(key);
    // Serialise so callers can never mutate a cached object in place.
    this.entries.set(key, { value: JSON.stringify(value), expiresAt: this.now() + ttlMs });
    while (this.entries.size > this.maxEntries) {
      const oldest = this.entries.keys().next().value;
      if (oldest === undefined) break;
      this.entries.delete(oldest);
    }
  }

  async delete(key: string): Promise<void> {
    this.entries.delete(key);
  }

  async close(): Promise<void> {
    this.entries.clear();
  }
}

export class RedisCache implements Cache {
  readonly kind = 'redis' as const;
  private warnedAt = 0;

  constructor(
    private readonly redis: Redis,
    private readonly logger: Logger,
    private readonly prefix = 'radar:',
  ) {}

  /**
   * Connects and waits (bounded) until Redis is ready. Commands are not queued
   * while disconnected, so without this wait early reads would all miss. If
   * Redis is not reachable in time the cache starts degraded and ioredis keeps
   * reconnecting in the background.
   */
  static async connect(url: string, logger: Logger, readyTimeoutMs = 3_000): Promise<RedisCache> {
    const redis = new Redis(url, {
      // Fail fast instead of queueing: a cache miss is cheaper than a stall.
      enableOfflineQueue: false,
      maxRetriesPerRequest: 1,
      connectTimeout: 5_000,
      retryStrategy: (times) => Math.min(times * 500, 10_000),
    });
    redis.on('error', (err: Error) => logger.debug({ err: err.message }, 'redis connection error'));
    await new Promise<void>((resolve) => {
      if (redis.status === 'ready') return resolve();
      const timer = setTimeout(() => {
        logger.warn('redis not ready at startup; continuing without cache until it is');
        resolve();
      }, readyTimeoutMs);
      redis.once('ready', () => {
        clearTimeout(timer);
        resolve();
      });
    });
    return new RedisCache(redis, logger);
  }

  async ping(): Promise<boolean> {
    try {
      return (await this.redis.ping()) === 'PONG';
    } catch {
      return false;
    }
  }

  async get<T>(key: string): Promise<T | undefined> {
    try {
      const raw = await this.redis.get(this.prefix + key);
      return raw === null ? undefined : (JSON.parse(raw) as T);
    } catch (err) {
      this.degraded(err);
      return undefined;
    }
  }

  async set(key: string, value: unknown, ttlMs: number): Promise<void> {
    try {
      await this.redis.set(this.prefix + key, JSON.stringify(value), 'PX', Math.max(1, Math.round(ttlMs)));
    } catch (err) {
      this.degraded(err);
    }
  }

  async delete(key: string): Promise<void> {
    try {
      await this.redis.del(this.prefix + key);
    } catch (err) {
      this.degraded(err);
    }
  }

  async close(): Promise<void> {
    try {
      await this.redis.quit();
    } catch {
      this.redis.disconnect();
    }
  }

  private degraded(err: unknown): void {
    // Log at most once a minute; the cache being down is not an emergency.
    if (Date.now() - this.warnedAt > 60_000) {
      this.warnedAt = Date.now();
      this.logger.warn({ err: (err as Error).message }, 'redis unavailable, continuing without cache');
    }
  }
}

/** Deduplicates concurrent loads of the same key within this process. */
export class SingleFlight {
  private readonly inflight = new Map<string, Promise<unknown>>();

  run<T>(key: string, fn: () => Promise<T>): Promise<T> {
    const existing = this.inflight.get(key);
    if (existing) return existing as Promise<T>;
    const p = fn().finally(() => this.inflight.delete(key));
    this.inflight.set(key, p);
    return p;
  }
}

/** Read-through cache with single-flight loading. */
export async function cached<T>(
  cache: Cache,
  flight: SingleFlight,
  key: string,
  ttlMs: number,
  load: () => Promise<T>,
): Promise<T> {
  const hit = await cache.get<T>(key);
  if (hit !== undefined) return hit;
  return flight.run(key, async () => {
    const value = await load();
    await cache.set(key, value, ttlMs);
    return value;
  });
}
