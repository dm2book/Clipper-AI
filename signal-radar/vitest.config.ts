import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['tests/**/*.test.ts'],
    environment: 'node',
    // Integration tests share one PostgreSQL database and the instance lock.
    fileParallelism: false,
    testTimeout: 15_000,
  },
});
