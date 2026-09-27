/** `npm run migrate`: apply pending migrations and exit. */
import { ConfigError, loadConfig } from '../config/env.js';
import { createPool, migrate } from '../infra/db.js';
import { createLogger } from '../infra/logger.js';

async function main(): Promise<void> {
  const config = loadConfig();
  const logger = createLogger(config.logLevel);
  const pool = createPool(config.database.url, 2, logger);
  try {
    const applied = await migrate(pool, logger);
    logger.info({ applied }, applied.length ? 'migrations applied' : 'database already up to date');
  } finally {
    await pool.end();
  }
}

main().catch((err: unknown) => {
  if (err instanceof ConfigError) {
    console.error(err.message);
  } else {
    console.error(err);
  }
  process.exit(1);
});
