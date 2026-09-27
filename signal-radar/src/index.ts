/**
 * Entry point: `npm start` (built) or `npm run dev` (TypeScript directly).
 * Signal handling: SIGINT/SIGTERM trigger a graceful stop bounded by
 * SHUTDOWN_TIMEOUT_MS; a second signal or the timeout forces an exit.
 */
import { createApp, type App } from './app.js';
import { ConfigError, loadConfig } from './config/env.js';

async function main(): Promise<void> {
  let config;
  try {
    config = loadConfig();
  } catch (err) {
    if (err instanceof ConfigError) {
      console.error(err.message);
      console.error('See .env.example for every variable.');
      process.exit(1);
    }
    throw err;
  }

  let app: App;
  try {
    app = await createApp(config);
  } catch (err) {
    console.error(`startup failed: ${err instanceof Error ? err.message : String(err)}`);
    process.exit(1);
  }
  const { logger } = app;

  let shuttingDown = false;
  const shutdown = (reason: string, exitCode: number) => {
    if (shuttingDown) {
      logger.warn({ reason }, 'second shutdown request: exiting immediately');
      process.exit(1);
    }
    shuttingDown = true;
    logger.info({ reason }, 'shutdown requested');
    const force = setTimeout(() => {
      logger.error('graceful shutdown timed out; exiting');
      process.exit(1);
    }, config.shutdownTimeoutMs);
    force.unref();
    app
      .stop()
      .catch((err: unknown) => logger.error({ err: String(err) }, 'error during shutdown'))
      .finally(() => {
        clearTimeout(force);
        process.exit(exitCode);
      });
  };

  process.on('SIGINT', () => shutdown('SIGINT', 0));
  process.on('SIGTERM', () => shutdown('SIGTERM', 0));
  process.on('unhandledRejection', (err) => {
    logger.fatal({ err: String(err) }, 'unhandled promise rejection');
    shutdown('unhandledRejection', 1);
  });
  process.on('uncaughtException', (err) => {
    logger.fatal({ err: err.message, stack: err.stack }, 'uncaught exception');
    shutdown('uncaughtException', 1);
  });

  try {
    await app.start();
  } catch (err) {
    logger.fatal({ err: err instanceof Error ? err.message : String(err) }, 'start failed');
    shutdown('start failed', 1);
  }
}

void main();
