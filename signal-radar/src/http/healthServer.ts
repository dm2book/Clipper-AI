import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import type { Registry } from 'prom-client';
import type { Logger } from '../infra/logger.js';

export interface Readiness {
  ready: boolean;
  checks: Record<string, unknown>;
}

export interface HealthServer {
  port: number;
  close(): Promise<void>;
}

/**
 * GET /health   liveness: the process is up
 * GET /ready    readiness: database reachable, discovery connected, not stopping
 * GET /metrics  Prometheus text format
 */
export async function startHealthServer(opts: {
  host: string;
  port: number;
  logger: Logger;
  registry: Registry;
  readiness: () => Promise<Readiness>;
}): Promise<HealthServer> {
  const server: Server = createServer((req, res) => {
    const send = (status: number, body: string, type = 'application/json') => {
      res.writeHead(status, { 'content-type': type, 'cache-control': 'no-store' });
      res.end(body);
    };
    if (req.method !== 'GET') return send(405, JSON.stringify({ error: 'method not allowed' }));
    const path = (req.url ?? '/').split('?')[0];
    if (path === '/health') return send(200, JSON.stringify({ status: 'ok' }));
    if (path === '/ready') {
      opts
        .readiness()
        .then((r) => send(r.ready ? 200 : 503, JSON.stringify(r)))
        .catch((err: unknown) => send(503, JSON.stringify({ ready: false, error: String(err) })));
      return;
    }
    if (path === '/metrics') {
      opts.registry
        .metrics()
        .then((text) => send(200, text, opts.registry.contentType))
        .catch(() => send(500, JSON.stringify({ error: 'metrics unavailable' })));
      return;
    }
    send(404, JSON.stringify({ error: 'not found' }));
  });
  server.keepAliveTimeout = 5_000;

  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(opts.port, opts.host, () => {
      server.off('error', reject);
      resolve();
    });
  });
  const port = (server.address() as AddressInfo).port;
  opts.logger.info({ host: opts.host, port }, 'health server listening');
  return {
    port,
    close: () =>
      new Promise<void>((resolve) => {
        server.close(() => resolve());
        server.closeAllConnections();
      }),
  };
}
