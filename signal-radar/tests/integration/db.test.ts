import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { acquireInstanceLock, createPool, migrate, ping, releaseInstanceLock, type Pool } from '../../src/infra/db.js';
import { silentLogger } from '../../src/infra/logger.js';
import { TEST_DATABASE_URL, freshDatabase, hasDatabase } from '../support/db.js';

describe.skipIf(!hasDatabase)('database schema and migrations', () => {
  let pool: Pool;
  beforeAll(async () => {
    pool = await freshDatabase();
  });
  afterAll(async () => {
    await pool?.end();
  });

  it('is idempotent', async () => {
    expect(await migrate(pool, silentLogger())).toEqual([]);
    const { rows } = await pool.query('SELECT name FROM schema_migrations');
    expect(rows.map((r) => r.name)).toEqual(['0001_init.sql', '0002_momentum_engine.sql']);
  });

  it('creates every table the services use', async () => {
    const { rows } = await pool.query<{ table_name: string }>(
      `SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY 1`,
    );
    expect(rows.map((r) => r.table_name)).toEqual([
      'alerts',
      'holder_snapshots',
      'market_snapshots',
      'momentum_signals',
      'pools',
      'provider_errors',
      'safety_reports',
      'schema_migrations',
      'system_state',
      'tokens',
      'trades',
    ]);
  });

  it('enforces the invariants in the schema', async () => {
    await pool.query(`INSERT INTO tokens (chain, address, detection_source) VALUES ('solana', 'A', 'test')`);
    // ARCHIVED requires archived_at
    await expect(pool.query(`UPDATE tokens SET tier = 'ARCHIVED' WHERE address = 'A'`)).rejects.toThrow(/check/i);
    // unknown tier
    await expect(pool.query(`UPDATE tokens SET tier = 'LUKEWARM' WHERE address = 'A'`)).rejects.toThrow(/check/i);
    // snapshots must reference a known token
    await expect(
      pool.query(
        `INSERT INTO market_snapshots (chain, token_address, source, observed_at) VALUES ('solana', 'nope', 'x', now())`,
      ),
    ).rejects.toThrow(/foreign key/i);
    // alert dedupe
    const insertAlert = `INSERT INTO alerts (chain, token_address, type, dedupe_key, status, payload)
                         VALUES ('solana', 'A', 'NEW_TOKEN', 'k1', 'PENDING', '{}')`;
    await pool.query(insertAlert);
    await expect(pool.query(insertAlert)).rejects.toThrow(/duplicate key/i);
    // SUPPRESSED needs a reason
    await expect(
      pool.query(`INSERT INTO alerts (chain, token_address, type, dedupe_key, status, payload)
                  VALUES ('solana', 'A', 'MOMENTUM', 'k2', 'SUPPRESSED', '{}')`),
    ).rejects.toThrow(/check/i);
  });

  it('allows only one instance per database', async () => {
    const first = await acquireInstanceLock(pool);
    expect(first).not.toBeNull();
    const other = createPool(TEST_DATABASE_URL!, 2, silentLogger());
    try {
      expect(await acquireInstanceLock(other)).toBeNull();
      await releaseInstanceLock(first!);
      const second = await acquireInstanceLock(other);
      expect(second).not.toBeNull();
      await releaseInstanceLock(second!);
    } finally {
      await other.end();
    }
  });

  it('pings', async () => {
    expect(await ping(pool)).toBe(true);
  });
});
