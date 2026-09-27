import { readdir, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import pg from 'pg';
import type { Logger } from './logger.js';

export type Pool = pg.Pool;
export type PoolClient = pg.PoolClient;
/** Anything that can run a query: the pool or a checked-out client. */
export type Queryable = Pick<pg.Pool, 'query'> | Pick<pg.PoolClient, 'query'>;

/** Arbitrary constants: one for migrations, one for "only one radar per DB". */
const MIGRATION_LOCK_KEY = 0x5169_0001;
const INSTANCE_LOCK_KEY = 0x5169_0002;

export const DEFAULT_MIGRATIONS_DIR = fileURLToPath(new URL('../../migrations/', import.meta.url));

export function createPool(url: string, max: number, logger: Logger): pg.Pool {
  const pool = new pg.Pool({
    connectionString: url,
    max,
    application_name: 'signal-radar',
    connectionTimeoutMillis: 10_000,
    idleTimeoutMillis: 30_000,
    // Guard against a runaway query holding a worker forever.
    statement_timeout: 30_000,
  });
  // Without a listener, an idle client error crashes the process.
  pool.on('error', (err) => logger.error({ err }, 'idle postgres client error'));
  return pool;
}

export async function migrate(pool: pg.Pool, logger: Logger, dir = DEFAULT_MIGRATIONS_DIR): Promise<string[]> {
  const files = (await readdir(dir)).filter((f) => /^\d{4}_.+\.sql$/.test(f)).sort();
  const client = await pool.connect();
  const applied: string[] = [];
  try {
    // Serialises concurrent starts (e.g. two containers booting at once).
    await client.query('SELECT pg_advisory_lock($1)', [MIGRATION_LOCK_KEY]);
    await client.query(`CREATE TABLE IF NOT EXISTS schema_migrations (
      name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())`);
    const done = new Set(
      (await client.query<{ name: string }>('SELECT name FROM schema_migrations')).rows.map((r) => r.name),
    );
    for (const file of files) {
      if (done.has(file)) continue;
      const sql = await readFile(join(dir, file), 'utf8');
      await client.query('BEGIN');
      try {
        await client.query(sql);
        await client.query('INSERT INTO schema_migrations (name) VALUES ($1)', [file]);
        await client.query('COMMIT');
      } catch (err) {
        await client.query('ROLLBACK');
        throw new Error(`migration ${file} failed: ${(err as Error).message}`, { cause: err });
      }
      applied.push(file);
      logger.info({ migration: file }, 'migration applied');
    }
  } finally {
    await client.query('SELECT pg_advisory_unlock($1)', [MIGRATION_LOCK_KEY]).catch(() => undefined);
    client.release();
  }
  return applied;
}

/**
 * Session-level advisory lock on a dedicated connection, held for the life of
 * the process. Rate limits and the discovery WebSocket are per process, so two
 * radars on one database would double every provider budget.
 * Returns the client holding the lock (release it on shutdown) or null.
 */
export async function acquireInstanceLock(pool: pg.Pool): Promise<pg.PoolClient | null> {
  const client = await pool.connect();
  const { rows } = await client.query<{ locked: boolean }>('SELECT pg_try_advisory_lock($1) AS locked', [
    INSTANCE_LOCK_KEY,
  ]);
  if (rows[0]?.locked) return client;
  client.release();
  return null;
}

export async function releaseInstanceLock(client: pg.PoolClient): Promise<void> {
  try {
    await client.query('SELECT pg_advisory_unlock($1)', [INSTANCE_LOCK_KEY]);
  } finally {
    client.release();
  }
}

export async function ping(pool: Queryable, timeoutMs = 2_000): Promise<boolean> {
  let timer: NodeJS.Timeout | undefined;
  try {
    const result = await Promise.race([
      pool.query('SELECT 1 AS ok'),
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error('db ping timeout')), timeoutMs);
      }),
    ]);
    return result.rows[0]?.ok === 1;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

// --- value conversion helpers -------------------------------------------------
// pg returns numeric/bigint as strings to avoid precision loss. USD amounts and
// ratios are converted to numbers explicitly; raw token amounts stay strings.

export function num(v: unknown): number | null {
  if (v === null || v === undefined) return null;
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

export function int(v: unknown): number | null {
  const n = num(v);
  return n === null ? null : Math.trunc(n);
}

export function date(v: unknown): Date | null {
  if (v === null || v === undefined) return null;
  return v instanceof Date ? v : new Date(String(v));
}
