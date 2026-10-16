// Dev-server proxy for API calls (CRA serves this app on :3000, the FastAPI
// backend lives on :8000 by default). Override the target when the backend runs
// elsewhere, e.g.:
//   PROXY_TARGET=http://localhost:8001 npm start
const { createProxyMiddleware } = require('http-proxy-middleware');

const target = process.env.PROXY_TARGET || 'http://localhost:8000';

module.exports = function (app) {
  app.use('/api', createProxyMiddleware({ target, changeOrigin: true }));
  app.use('/ws', createProxyMiddleware({ target, changeOrigin: true, ws: true }));
};
