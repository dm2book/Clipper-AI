import { describe, expect, it } from 'vitest';
import { redactUrl } from '../../src/infra/redact.js';

describe('redactUrl', () => {
  it('masks credential query parameters', () => {
    const out = redactUrl('https://mainnet.helius-rpc.com/?api-key=abc123&cluster=main');
    expect(out).not.toContain('abc123');
    expect(out).toContain('cluster=main');
  });

  it('masks the Discord webhook token', () => {
    const out = redactUrl('https://discord.com/api/webhooks/123/very-secret-token?wait=true');
    expect(out).not.toContain('very-secret-token');
    expect(out).toContain('/webhooks/123/');
  });

  it('masks userinfo and survives garbage', () => {
    expect(redactUrl('postgres://user:pw@host/db')).not.toContain('pw');
    expect(redactUrl('not a url')).toBe('[invalid-url]');
  });
});
