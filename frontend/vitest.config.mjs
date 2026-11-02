import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// Vitest runs alongside react-scripts, not instead of it: `npm run build` still
// goes through CRA and this config is only read by vitest.
//
// This file is .mjs on purpose. package.json has no "type": "module" (CRA's
// setupProxy.js is CommonJS and needs it that way), so a .js config would be
// loaded with require() — and @vitejs/plugin-react is ESM-only, which fails
// with "resolved to an ESM file. ESM file cannot be loaded by require".
//
// The plugin is needed for the include pattern: JSX lives in .jsx here, and
// the plugin is what teaches Vite to compile it before the browser would.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/setupTests.js'],
    include: ['src/**/*.test.{js,jsx}'],
    restoreMocks: true,
  },
});
