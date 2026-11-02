import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// One config for both jobs: `vite build` and `vitest`.
//
// This file is .mjs on purpose. package.json has no "type": "module", so a .js
// config would be loaded with require() — and @vitejs/plugin-react is ESM-only,
// failing with "resolved to an ESM file. ESM file cannot be loaded by require".

// Dev server proxy, replacing CRA's setupProxy.js. The backend lives on :8000;
// override with PROXY_TARGET=http://localhost:8001 npm start.
const target = process.env.PROXY_TARGET || 'http://localhost:8000';

export default defineConfig({
  plugins: [react()],

  // The backend serves build/index.html, and start.sh checks for it, so the
  // output directory stays `build` rather than Vite's default `dist`.
  build: {
    outDir: 'build',
    emptyOutDir: true,
  },

  // public/ holds the standalone viewers the backend serves directly
  // (livekit.html, qa_reports.html, recordings.html) plus vendor/. Vite copies
  // them to the build root, which is what the FileResponse routes expect.
  publicDir: 'public',

  server: {
    port: 3000,
    strictPort: true,
    proxy: {
      '/api': { target, changeOrigin: true },
      '/ws': { target, changeOrigin: true, ws: true },
    },
  },

  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/setupTests.js'],
    include: ['src/**/*.test.{js,jsx}'],
    restoreMocks: true,
  },
});
