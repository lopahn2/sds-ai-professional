import { defineConfig, loadEnv, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';

function mockGatewayDocument(): Plugin {
  return {
    name: 'loopback-mock-gateway',
    configurePreviewServer(server) {
      server.middlewares.use((req, res, next) => {
        // The browser project uses Vite's minimal Node compatibility declarations.
        const { method, headers } = req as typeof req & {
          method?: string; headers: { accept?: string; host?: string };
        };
        if (method === 'GET' && headers.accept?.includes('text/html') &&
            /^(localhost|127\.0\.0\.1):\d+$/.test(headers.host ?? '')) {
          const challenge = 'a'.repeat(64) + '.' + 'b'.repeat(64);
          res.setHeader('Set-Cookie', 'lp_operator_csrf=' + challenge + '; Path=/; SameSite=Strict');
        }
        next();
      });
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'LIVEPROBE_');
  const mock = env.LIVEPROBE_MOCK_API;
  const mockPreview = env.LIVEPROBE_MOCK === '1' && mock && /^http:\/\/127\.0\.0\.1:\d+$/.test(mock);
  return {
    plugins: [react(), ...(mockPreview ? [mockGatewayDocument()] : [])],
    server: { proxy: { '/api': mock || 'http://localhost:8000' } },
    preview: { proxy: mock ? { '/api': mock } : undefined },
  };
});
