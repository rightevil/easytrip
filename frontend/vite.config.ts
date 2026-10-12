import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import type { ProxyOptions } from 'vite';

const localProxy: ProxyOptions = {
  target: 'http://127.0.0.1:8000',
  configure(proxy) {
    proxy.on('proxyReq', request => request.setHeader('Origin', 'http://127.0.0.1:8000'));
  },
};

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    cors: false,
    proxy: {
      '/api': localProxy,
      '/p0': localProxy,
      '/_AMapService': localProxy,
    },
  },
  test: { environment: 'jsdom', restoreMocks: true },
});
