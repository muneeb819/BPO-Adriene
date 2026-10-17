// Dev-server proxy for API calls (CRA serves this app on :3000, the FastAPI
// backend lives on :8055 by default — HTTPS with a self-signed dev cert). Override
// the target when the backend runs elsewhere, e.g.:
//   PROXY_TARGET=https://localhost:9000 npm start
const { createProxyMiddleware } = require('http-proxy-middleware');

const target = process.env.PROXY_TARGET || 'https://localhost:8055';

module.exports = function (app) {
  app.use('/api', createProxyMiddleware({ target, changeOrigin: true, secure: false }));
  app.use('/ws', createProxyMiddleware({ target, changeOrigin: true, ws: true, secure: false }));
};
