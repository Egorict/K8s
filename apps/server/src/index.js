const express = require('express');
const dotenv = require('dotenv');
const { initDB } = require('./db');
const routes = require('./routes');

dotenv.config();

const app = express();
const PORT = process.env.PORT || 8080;

app.use(express.json());

app.use(routes);

app.get('/health', (req, res) => {
  res.send('OK');
});

const start = async () => {
  await initDB();
  app.listen(PORT, () => {
    console.log(`🚀 Server running on port ${PORT}`);
  });
};

start();