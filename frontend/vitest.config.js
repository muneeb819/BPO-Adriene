import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// Vitest runs alongside react-scripts, not instead of it: `npm run build` still
// goes through CRA and this config is only read by vitest.
//
// The plugin is needed for the include pattern alone. CRA silently accepts JSX
// inside .js files; Vite does not, so src/App.js has to be named explicitly or
// every test importing it fails to parse at the first JSX tag.
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
