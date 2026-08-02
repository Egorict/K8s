const client = require('prom-client');

const register = new client.Registry();

// Стандартные метрики процесса Node: CPU, heap, event loop lag, GC, дескрипторы.
// Именно они показывают утечки памяти и залипание event loop.
client.collectDefaultMetrics({ register });

// Латентность и количество HTTP-запросов. Бакеты подобраны под быстрый API:
// от 5 мс до 5 с — этого хватает, чтобы видеть и норму, и деградацию.
const httpRequestDuration = new client.Histogram({
  name: 'http_request_duration_seconds',
  help: 'Duration of HTTP requests in seconds',
  labelNames: ['method', 'route', 'status_code'],
  buckets: [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5],
  registers: [register],
});

// Middleware вешается ДО маршрутов, а замер закрывается на событии finish —
// так учитываются в том числе ответы с ошибками.
const metricsMiddleware = (req, res, next) => {
  const end = httpRequestDuration.startTimer();
  res.on('finish', () => {
    // req.route.path вместо req.path: иначе /api/data/1, /api/data/2 ... создали
    // бы бесконечное число серий (классический взрыв кардинальности).
    const route = req.route ? req.route.path : req.path;
    end({ method: req.method, route, status_code: res.statusCode });
  });
  next();
};

const metricsHandler = async (req, res) => {
  res.set('Content-Type', register.contentType);
  res.end(await register.metrics());
};

module.exports = { metricsMiddleware, metricsHandler };
