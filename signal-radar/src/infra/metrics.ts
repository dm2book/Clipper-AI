import { Counter, Gauge, Histogram, Registry, collectDefaultMetrics } from 'prom-client';

/**
 * Prometheus metrics (docs/ARCHITECTURE.md §K). One registry per instance so
 * tests can create as many as they like without global state.
 */
export class Metrics {
  readonly registry = new Registry();

  readonly providerRequests = new Counter({
    name: 'radar_provider_requests_total',
    help: 'Requests to external providers by outcome',
    labelNames: ['provider', 'endpoint', 'outcome'] as const,
    registers: [this.registry],
  });
  readonly providerLatency = new Histogram({
    name: 'radar_provider_request_duration_seconds',
    help: 'Latency of external provider requests',
    labelNames: ['provider', 'endpoint'] as const,
    buckets: [0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30],
    registers: [this.registry],
  });
  readonly rateLimitWaits = new Counter({
    name: 'radar_rate_limited_total',
    help: '429 responses received, by provider',
    labelNames: ['provider'] as const,
    registers: [this.registry],
  });
  readonly circuitState = new Gauge({
    name: 'radar_circuit_open',
    help: '1 when the circuit breaker for a provider is open',
    labelNames: ['provider'] as const,
    registers: [this.registry],
  });
  readonly schemaErrors = new Counter({
    name: 'radar_schema_errors_total',
    help: 'Provider responses that failed validation',
    labelNames: ['provider', 'endpoint'] as const,
    registers: [this.registry],
  });
  readonly tokensDiscovered = new Counter({
    name: 'radar_tokens_discovered_total',
    help: 'New tokens stored, by discovery source',
    labelNames: ['source'] as const,
    registers: [this.registry],
  });
  readonly detectionLatency = new Histogram({
    name: 'radar_detection_latency_seconds',
    help: 'Time from on-chain creation to detection',
    buckets: [1, 2, 5, 10, 20, 30, 60, 120, 300],
    registers: [this.registry],
  });
  readonly discoveryDropped = new Counter({
    name: 'radar_discovery_dropped_total',
    help: 'Launch notifications dropped because the resolver queue was full',
    registers: [this.registry],
  });
  readonly snapshots = new Counter({
    name: 'radar_snapshots_total',
    help: 'Market snapshot attempts by result',
    labelNames: ['result'] as const,
    registers: [this.registry],
  });
  readonly enrichments = new Counter({
    name: 'radar_enrichment_checks_total',
    help: 'Safety/holder checks by provider and result',
    labelNames: ['provider', 'result'] as const,
    registers: [this.registry],
  });
  readonly alerts = new Counter({
    name: 'radar_alerts_total',
    help: 'Alerts created, by type and initial status',
    labelNames: ['type', 'status'] as const,
    registers: [this.registry],
  });
  readonly notifications = new Counter({
    name: 'radar_notifications_total',
    help: 'Notification delivery attempts by outcome',
    labelNames: ['outcome'] as const,
    registers: [this.registry],
  });
  readonly tokensTracked = new Gauge({
    name: 'radar_tokens_tracked',
    help: 'Tokens per monitoring tier',
    labelNames: ['tier'] as const,
    registers: [this.registry],
  });
  readonly jobRuns = new Counter({
    name: 'radar_job_runs_total',
    help: 'Scheduler job runs by outcome',
    labelNames: ['job', 'outcome'] as const,
    registers: [this.registry],
  });

  constructor(opts: { defaultMetrics?: boolean } = {}) {
    if (opts.defaultMetrics) collectDefaultMetrics({ register: this.registry });
  }
}
