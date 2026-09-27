import { errorMessage } from '../core/errors.js';
import type { Logger } from '../infra/logger.js';
import type { Metrics } from '../infra/metrics.js';
import { sleep } from '../infra/retry.js';

export interface Job {
  name: string;
  intervalMs: number;
  /** Return true when more work is waiting: the job then runs again right away. */
  run(signal: AbortSignal): Promise<boolean | void>;
}

export interface JobStatus {
  lastSuccessAt: Date | null;
  consecutiveFailures: number;
}

/**
 * Runs each job in its own loop: never overlapping with itself, isolated from
 * the others (a failing job backs off; the rest carry on), and stoppable.
 * `stop()` aborts the shared signal and waits for in-flight runs to finish.
 */
export class Scheduler {
  private readonly jobs: Job[] = [];
  private readonly loops: Promise<void>[] = [];
  private readonly status = new Map<string, JobStatus>();
  private controller = new AbortController();
  private started = false;

  constructor(
    private readonly logger: Logger,
    private readonly metrics?: Metrics,
  ) {}

  add(job: Job): this {
    if (this.started) throw new Error('cannot add jobs after start');
    this.jobs.push(job);
    this.status.set(job.name, { lastSuccessAt: null, consecutiveFailures: 0 });
    return this;
  }

  start(): void {
    if (this.started) return;
    this.started = true;
    for (const job of this.jobs) this.loops.push(this.loop(job));
  }

  /** Resolves true when every job stopped within the timeout. */
  async stop(timeoutMs: number): Promise<boolean> {
    this.controller.abort(new Error('scheduler stopping'));
    let timer: NodeJS.Timeout | undefined;
    const finished = await Promise.race([
      Promise.allSettled(this.loops).then(() => true),
      new Promise<boolean>((resolve) => {
        timer = setTimeout(() => resolve(false), timeoutMs);
      }),
    ]);
    clearTimeout(timer);
    return finished;
  }

  jobStatus(): Record<string, JobStatus> {
    return Object.fromEntries(this.status);
  }

  private async loop(job: Job): Promise<void> {
    const signal = this.controller.signal;
    const state = this.status.get(job.name)!;
    while (!signal.aborted) {
      let more = false;
      try {
        more = (await job.run(signal)) === true;
        state.lastSuccessAt = new Date();
        state.consecutiveFailures = 0;
        this.metrics?.jobRuns.inc({ job: job.name, outcome: 'ok' });
      } catch (err) {
        if (signal.aborted) break;
        state.consecutiveFailures++;
        this.metrics?.jobRuns.inc({ job: job.name, outcome: 'error' });
        this.logger.error({ job: job.name, failures: state.consecutiveFailures, err: errorMessage(err) }, 'job run failed');
      }
      const delay = more
        ? 0
        : state.consecutiveFailures
          ? Math.min(job.intervalMs * 2 ** state.consecutiveFailures, 60_000)
          : job.intervalMs;
      try {
        // Even with more work waiting, yield to the event loop between runs.
        await sleep(delay, signal);
      } catch {
        break;
      }
    }
  }
}
