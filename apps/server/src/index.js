const express = require('express');
const { initDB, ping, closePool } = require('./db');
const routes = require('./routes');
const { metricsMiddleware, metricsHandler } = require('./metrics');

const app = express();
const PORT = process.env.PORT || 8080;
// Пауза между "начали выключаться" и "закрыли соединения". Нужна, чтобы
// Kubernetes успел увидеть проваленный readiness и убрать под из Endpoints
// Service — иначе трафик продолжит приходить в уже закрывающийся процесс.
const SHUTDOWN_GRACE_MS = parseInt(process.env.SHUTDOWN_GRACE_MS || '5000', 10);

let schemaReady = false;
let shuttingDown = false;

// CORS не нужен: сервер это ClusterIP, снаружи недоступен. Браузер обращается
// к API через nginx клиента на том же origin (/api), поэтому запросы same-origin.
// Единственный клиент сервера — nginx (server-to-server), где CORS неприменим.

// Замер должен стоять ДО маршрутов, чтобы охватить их все.
app.use(metricsMiddleware);
app.use(express.json());
app.use(routes);

// Метрики для Prometheus. Наружу не торчат: сервер это ClusterIP, а NetworkPolicy
// пускает сюда только под клиента и скрейп из неймспейса monitoring.
app.get('/metrics', metricsHandler);

// LIVENESS — "процесс жив, его не надо перезапускать".
// Базу здесь НЕ проверяем намеренно: рестарт пода не починит упавший Postgres,
// а превратит недоступность базы в бесконечный CrashLoopBackOff.
app.get('/health', (req, res) => {
  res.json({ status: 'ok' });
});

// READINESS — "можно ли слать мне трафик".
// Вот здесь база проверяется реально: если она недоступна, под убирается из
// Endpoints Service, но НЕ перезапускается.
app.get('/ready', async (req, res) => {
  if (shuttingDown) {
    return res.status(503).json({ status: 'shutting down' });
  }
  if (!schemaReady) {
    return res.status(503).json({ status: 'initializing schema' });
  }
  try {
    await ping();
    res.json({ status: 'ready' });
  } catch (err) {
    res.status(503).json({ status: 'database unavailable', error: err.message });
  }
});

// Инициализация схемы с экспоненциальным backoff вместо падения процесса.
// Работает в фоне: сервер уже слушает порт, поэтому пробы отвечают и
// Kubernetes не убивает под, пока база поднимается.
const initSchemaWithRetry = async () => {
  let delayMs = 1000;
  while (!shuttingDown && !schemaReady) {
    try {
      await initDB();
      schemaReady = true;
      return;
    } catch (err) {
      console.error(`❌ Schema init failed: ${err.message}. Retry in ${delayMs}ms`);
      await new Promise((resolve) => setTimeout(resolve, delayMs));
      delayMs = Math.min(delayMs * 2, 30000);
    }
  }
};

const start = () => {
  // Слушаем СРАЗУ, до инициализации БД. Раньше здесь стоял `await initDB()`,
  // из-за которого недоступная на старте база убивала процесс -> CrashLoopBackOff.
  const server = app.listen(PORT, () => {
    console.log(`🚀 Server running on port ${PORT}`);
  });

  initSchemaWithRetry();

  const shutdown = (signal) => {
    if (shuttingDown) return;
    shuttingDown = true;
    console.log(`${signal} received — graceful shutdown`);

    // Страховка: если что-то повиснет, выходим принудительно. unref(), чтобы
    // сам таймер не держал процесс живым.
    const forceExit = setTimeout(() => {
      console.error('⚠️ Forced exit after shutdown timeout');
      process.exit(1);
    }, SHUTDOWN_GRACE_MS + 15000);
    forceExit.unref();

    // 1. readiness уже отдаёт 503 — даём Kubernetes убрать нас из Service.
    setTimeout(() => {
      // 2. Перестаём принимать новые соединения и дожидаемся активных запросов.
      server.close(async () => {
        try {
          await closePool();
        } catch (err) {
          console.error('Error closing pool:', err.message);
        }
        console.log('✅ Shutdown complete');
        process.exit(0);
      });
    }, SHUTDOWN_GRACE_MS);
  };

  // SIGTERM присылает Kubernetes при удалении пода, SIGINT — Ctrl+C локально.
  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('SIGINT', () => shutdown('SIGINT'));
};

start();
