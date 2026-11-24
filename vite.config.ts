import path from 'path';
import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '');

  // Backend URL - default to localhost:8000 (FastAPI trading bot)
  const apiPort = env.VITE_API_PORT || '8000';
  const apiHost = env.VITE_API_HOST || '127.0.0.1';
  const apiTarget = `http://${apiHost}:${apiPort}`;

  console.log(`[Vite] Proxy target: ${apiTarget}`);

  return {
    server: {
      port: 3000,
      host: '0.0.0.0',
      strictPort: true,
      proxy: {
        // WebSocket proxy (must be before /api)
        '/ws': {
          target: apiTarget,
          ws: true,
          changeOrigin: true,
          secure: false,
          rewrite: (path) => path,
          configure: (proxy, _options) => {
            proxy.on('error', (err, _req, _res) => {
              console.error('[Vite Proxy WS] Error:', err.message);
            });
            proxy.on('proxyReqWs', (_proxyReq, req, _socket, _options, _head) => {
              console.log('[Vite Proxy WS] Upgrading:', req.url);
            });
          },
        },
        // REST API proxy → trading backend
        '/api': {
          target: apiTarget,
          changeOrigin: true,
          secure: false,
          configure: (proxy, _options) => {
            proxy.on('error', (err, _req, _res) => {
              console.error('[Vite Proxy API] Error:', err.message);
              console.error('[Vite Proxy API] Is backend running? Start with: python run.py');
            });
            proxy.on('proxyReq', (_proxyReq, req, _res) => {
              console.log('[Vite Proxy API]', req.method, req.url, '->', apiTarget);
            });
          },
        },
      },
    },
    plugins: [react()],
    define: {
      'process.env.API_KEY': JSON.stringify(env.GEMINI_API_KEY),
      'process.env.GEMINI_API_KEY': JSON.stringify(env.GEMINI_API_KEY),
    },
    resolve: {
      alias: {
        '@': path.resolve(__dirname, '.'),
      }
    }
  };
});
