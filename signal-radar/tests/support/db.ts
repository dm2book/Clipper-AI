import { createPool, migrate, type Pool } from '../../src/infra/db.js';
import { silentLogger } from '../../src/infra/logger.js';

/** Integration tests run only when a disposable database is provided. */
export const TEST_DATABASE_URL = process.env.TEST_DATABASE_URL;
export const hasDatabase = Boolean(TEST_DATABASE_URL);

/** A migrated, empty database. Everything in `public` is dropped first. */
export async function freshDatabase(): Promise<Pool> {
  if (!TEST_DATABASE_URL) throw new Error('TEST_DATABASE_URL not set');
  const pool = createPool(TEST_DATABASE_URL, 6, silentLogger());
  await pool.query('DROP SCHEMA public CASCADE; CREATE SCHEMA public;');
  await migrate(pool, silentLogger());
  return pool;
}
