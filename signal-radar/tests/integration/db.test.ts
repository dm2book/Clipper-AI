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
    expect(rows.map((r) => r.name)).toEqual(['0001_init.sql', '0002_momentum_engine.sql', '0003_wallet_intelligence.sql']);
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
      'tracked_wallets',
      'trades',
      'wallet_events',
      'wallets',
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

  it('dedupes wallet events and accepts the wallet alert types', async () => {
    const ins = `INSERT INTO wallet_events (chain, signature, ix_index, wallet, token_address, kind, status, amount_raw, block_time, source)
                 VALUES ('solana', 's', 0, 'W', 'T', 'buy', 'success', 1, now(), 'test')`;
    await pool.query(ins);
    await expect(pool.query(ins)).rejects.toThrow(/duplicate key/i);
    await expect(pool.query(ins.replace("'buy'", "'swap'").replace("'s', 0", "'s2', 0"))).rejects.toThrow(/check/i);
    for (const type of ['WHALE', 'TRACKED_WALLET', 'TRACKED_CLUSTER']) {
      await pool.query(
        `INSERT INTO alerts (chain, token_address, type, dedupe_key, status, payload, wallet)
         VALUES ('solana', 'A', $1, $2, 'PENDING', '{}', 'W')`,
        [type, `k-${type}`],
      );
    }
    await expect(
      pool.query(`INSERT INTO alerts (chain, token_address, type, dedupe_key, status, payload)
                  VALUES ('solana', 'A', 'SMART_MONEY', 'k-x', 'PENDING', '{}')`),
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
