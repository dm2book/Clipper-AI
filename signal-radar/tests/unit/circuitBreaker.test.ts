import { describe, expect, it } from 'vitest';
import { CircuitOpenError, NotFoundError, TransientError } from '../../src/core/errors.js';
import { CircuitBreaker } from '../../src/infra/circuitBreaker.js';

describe('CircuitBreaker', () => {
  function setup() {
    let t = 0;
    const states: string[] = [];
    const breaker = new CircuitBreaker({
      name: 'test',
      failureThreshold: 3,
      cooldownMs: 10_000,
      now: () => t,
      onStateChange: (s) => states.push(s),
    });
    return { breaker, states, advance: (ms: number) => (t += ms) };
  }
  const fail = () => Promise.reject(new TransientError('down'));
  const ok = () => Promise.resolve('ok');

  it('opens after consecutive failures and fails fast while open', async () => {
    const { breaker } = setup();
    for (let i = 0; i < 3; i++) await expect(breaker.exec(fail)).rejects.toBeInstanceOf(TransientError);
    expect(breaker.state).toBe('open');
    await expect(breaker.exec(ok)).rejects.toBeInstanceOf(CircuitOpenError);
  });

  it('lets one trial through after the cooldown and closes on success', async () => {
    const { breaker, advance, states } = setup();
    for (let i = 0; i < 3; i++) await breaker.exec(fail).catch(() => undefined);
    advance(10_000);
    await expect(breaker.exec(ok)).resolves.toBe('ok');
    expect(breaker.state).toBe('closed');
    expect(states).toEqual(['open', 'half_open', 'closed']);
  });

  it('re-opens when the trial fails', async () => {
    const { breaker, advance } = setup();
    for (let i = 0; i < 3; i++) await breaker.exec(fail).catch(() => undefined);
    advance(10_000);
    await expect(breaker.exec(fail)).rejects.toBeInstanceOf(TransientError);
    expect(breaker.state).toBe('open');
  });

  it('does not count "not found" as the provider being down', async () => {
    const { breaker } = setup();
    for (let i = 0; i < 5; i++) {
      await expect(breaker.exec(() => Promise.reject(new NotFoundError('no')))).rejects.toBeInstanceOf(NotFoundError);
    }
    expect(breaker.state).toBe('closed');
  });

  it('a success resets the failure count', async () => {
    const { breaker } = setup();
    await breaker.exec(fail).catch(() => undefined);
    await breaker.exec(fail).catch(() => undefined);
    await breaker.exec(ok);
    await breaker.exec(fail).catch(() => undefined);
    expect(breaker.state).toBe('closed');
  });
});
