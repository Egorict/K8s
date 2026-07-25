const express = require('express');
const { initDB } = require('./db');
const routes = require('./routes');

const app = express();
const PORT = process.env.PORT || 8080;

// CORS не нужен: сервер это ClusterIP, снаружи недоступен. Браузер обращается
// к API через nginx клиента на том же origin (/api), поэтому запросы same-origin.
// Единственный клиент сервера — nginx (server-to-server), где CORS неприменим.

app.use(express.json());
app.use(routes);

app.get('/health', (req, res) => {
  res.send('OK 1');
});

const start = async () => {
  await initDB();
  app.listen(PORT, () => {
    console.log(`🚀 Server running on port ${PORT}`);
  });
};

start();