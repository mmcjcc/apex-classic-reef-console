import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// The app lives at /reef/; the API is beside it at /reef/api.
export default defineConfig({
  base: '/reef/',
  plugins: [react()],
  build: { outDir: 'dist', sourcemap: false },
  // `npm run dev` against reef-web elsewhere: VITE_API=https://reef.example.lan npm run dev
  server: { proxy: { '/reef/api': { target: process.env.VITE_API ?? 'http://localhost:8001', changeOrigin: true, secure: false } } },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
  },
});
