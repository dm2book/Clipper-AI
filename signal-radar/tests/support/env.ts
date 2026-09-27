/** Minimal valid environment for tests. The URLs point nowhere on purpose. */
export function baseEnv(overrides: Record<string, string | undefined> = {}): NodeJS.ProcessEnv {
  return {
    DATABASE_URL: 'postgres://radar@127.0.0.1:5432/radar_test',
    SOLANA_RPC_HTTP_URL: 'http://127.0.0.1:9/rpc?api-key=test-key',
    SOLANA_RPC_WS_URL: 'ws://127.0.0.1:9/ws?api-key=test-key',
    ...overrides,
  };
}
