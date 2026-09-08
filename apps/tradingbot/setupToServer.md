# setupToServer — развёртывание бота на сервере (Docker / Kubernetes)

> **В ЭТОМ репозитории бот уже развёрнут иначе — читай сначала раздел 9 в конце.**
> Ниже описан самостоятельный вариант (свой namespace `tradingbot`, свой ingress),
> он остаётся верным для отдельного сервера, но в кластере из этого репозитория
> применяются не манифесты из `k8s/tradingbot.yaml`, а Helm-чарт
> `infrastructure/k8s/base/tradingbot-chart`, и панель открывается по `/tb`.

Коротко: да, всё это делается штатно. Бот и веб-панель живут **в одном контейнере**
и в одном процессе, обмениваются через каталог `/data`. Никакой базы, брокера
сообщений и второго сервиса не нужно.

---

## 1. Как это устроено

```
                    ┌─────────────────────── под tradingbot ───────────────────────┐
   api.bybit.com    │                                                              │
   (публичный REST) │   run.py --web                                               │
        │           │     ├── engine  ── сканер → стратегия → демо-сделки          │
        └──────────►│     │             пишет  ──►  /data/trades.csv               │
    исходящий 443   │     │                        /data/signals.csv               │
                    │     │                        /data/state.json                │
                    │     │                        /data/bot.log                   │
                    │     └── aiohttp-сервер :8080 ──  читает те же файлы          │
                    │                          │                                   │
                    └──────────────────────────┼───────────────────────────────────┘
                                               │
                                       Service :80 → Ingress / port-forward
                                               │
                                          браузер: таблица сделок
```

Входящие соединения боту не нужны — только **исходящий HTTPS на `api.bybit.com`**.
API-ключи не используются: режим демо, ордера никуда не отправляются.

---

## 2. Откуда веб-страница берёт данные

Единственный источник — каталог `BOT_DATA_DIR` (в контейнере `/data`, на PVC).
Сервер `bot/web.py` читает файлы на каждый запрос и отдаёт JSON:

| Эндпоинт | Файл-источник | Что внутри |
|----------|---------------|------------|
| `GET /` | — | сама страница (HTML+JS, без сборки и CDN, обновляется раз в 5 сек) |
| `GET /api/state` | `state.json` | открытые позиции, статистика, список наблюдаемых монет |
| `GET /api/trades?limit=200` | `trades.csv` | закрытые сделки, свежие сверху |
| `GET /api/signals?limit=100` | `signals.csv` | все сработавшие сетапы, включая пропущенные |
| `GET /api/log?lines=200` | `bot.log` | хвост лога |
| `GET /trades.csv` | `trades.csv` | скачать журнал как файл (открывается в Excel) |
| `GET /healthz` | — | проба для Kubernetes (`{"status":"ok"}`) |

Поля сделки в `/api/trades`: `num, date, symbol, side, timeframe, open_time,
close_time, duration_min, entry_price, exit_price, qty, notional_usd, margin_usd,
leverage, stop_price, take_price, gross_pnl_usd, fees_usd, **profit_usd**,
profit_pct_margin, max_profit_usd, max_loss_usd, exit_reason, entry_score, entry_reason`.

`state.json` перезаписывается раз в 60 секунд (`Engine.report_loop`), сделка в
`trades.csv` дописывается в момент закрытия. Если захочешь свой фронт или Grafana —
бери эти же эндпоинты, менять в боте ничего не нужно.

---

## 3. Переменные окружения

| Переменная | По умолчанию | Зачем |
|------------|--------------|-------|
| `BOT_DATA_DIR` | `data` | куда писать журналы (в контейнере `/data`) |
| `BOT_WEB` | `0` | `1` — поднять веб-панель |
| `BOT_WEB_HOST` | `0.0.0.0` | адрес прослушивания |
| `BOT_WEB_PORT` | `8080` | порт панели |
| `TZ` | `Asia/Yekaterinburg` (UTC+5) | часовой пояс во времени сделок |

Параметры самой ТС (маржа, плечо, стоп, тейк, фильтры) — в `bot/config.py`
или ключами запуска: `args: ["run", "--web", "--margin", "20", "--leverage", "20"]`.

---

## 4. Вариант A: просто Docker (самый быстрый путь)

```bash
# на сервере, в каталоге проекта
docker build -t tradingbot:1.0.0 .
docker compose up -d          # поднимет бота + панель на :8080
docker compose logs -f bot
```

Панель: `http://<IP сервера>:8080`. Данные лежат в томе `bot-data`, пересоздание
контейнера их не трогает.

Без compose:

```bash
docker volume create bot-data
docker run -d --name tradingbot --restart unless-stopped \
  -p 8080:8080 -v bot-data:/data tradingbot:1.0.0 run --web
```

---

## 5. Вариант B: Kubernetes

### 5.1 Собрать образ и доставить его в кластер

Выбери одно:

```bash
# (а) есть registry (Docker Hub, GHCR, Harbor, registry облака)
docker build -t <registry>/tradingbot:1.0.0 .
docker push <registry>/tradingbot:1.0.0
# затем в k8s/tradingbot.yaml подставь этот image

# (б) k3s / containerd без registry — импорт образа прямо в узел
docker save tradingbot:1.0.0 | sudo k3s ctr images import -

# (в) kind / minikube
kind load docker-image tradingbot:1.0.0
minikube image load tradingbot:1.0.0
```

Если образ тянется из приватного registry — добавь `imagePullSecrets` в Deployment.

### 5.2 Применить манифесты

```bash
kubectl apply -f k8s/tradingbot.yaml
kubectl -n tradingbot get pods,pvc,svc
kubectl -n tradingbot logs -f deploy/tradingbot
```

В `k8s/tradingbot.yaml` создаются: namespace `tradingbot`, PVC на 2Gi под журналы,
ConfigMap с переменными, Deployment (1 реплика, `strategy: Recreate`), Service и
Ingress.

**Почему одна реплика:** второй под писал бы те же сделки в тот же файл, и статистика
превратилась бы в кашу. Плюс PVC у нас `ReadWriteOnce` — двум подам его не отдадут.
Масштабировать здесь нечего: узкое место — не CPU, а лимиты API биржи.

### 5.3 Открыть панель

```bash
# 1) быстро, без ingress — проброс на свою машину
kubectl -n tradingbot port-forward svc/tradingbot 8080:80
#    → http://localhost:8080

# 2) NodePort — открыть порт на самом сервере
kubectl -n tradingbot patch svc tradingbot -p '{"spec":{"type":"NodePort"}}'
kubectl -n tradingbot get svc tradingbot     # смотри порт 3xxxx
#    → http://<IP сервера>:3xxxx

# 3) Ingress — если в кластере есть ingress-контроллер
#    в манифесте замени host: bot.example.com на свой домен и примени заново
```

---

## 6. Эксплуатация

```bash
# логи бота
kubectl -n tradingbot logs -f deploy/tradingbot

# сводка по сделкам прямо в поде
kubectl -n tradingbot exec deploy/tradingbot -- python run.py report

# забрать журнал себе
kubectl -n tradingbot cp $(kubectl -n tradingbot get pod -l app=tradingbot \
  -o jsonpath='{.items[0].metadata.name}'):/data/trades.csv ./trades.csv
# или просто скачать: http://<адрес панели>/trades.csv

# поменять параметры ТС на лету (маржа/плечо/фильтры) — через args в Deployment
kubectl -n tradingbot edit deploy tradingbot

# обновить версию
docker build -t <registry>/tradingbot:1.0.1 . && docker push <registry>/tradingbot:1.0.1
kubectl -n tradingbot set image deploy/tradingbot bot=<registry>/tradingbot:1.0.1

# проверка логики без сети (полезно после правок стратегии)
kubectl -n tradingbot exec deploy/tradingbot -- python run.py selftest
```

---

## 7. О чём стоит знать заранее

- **Панель ничем не защищена.** Она только читает файлы, но список твоих сделок
  увидит любой, кто дотянется до порта. Не публикуй её в интернет без basic-auth
  на ingress (`nginx.ingress.kubernetes.io/auth-type: basic`) или хотя бы без
  ограничения по IP. Через `port-forward` этот вопрос не встаёт вовсе.
- **Перезапуск пода теряет открытые демо-позиции.** Закрытые сделки в `trades.csv`
  никуда не денутся, а позиции живут в памяти процесса — после рестарта бот начнёт
  искать сетапы заново. Для демо это некритично; если понадобится — состояние
  восстанавливается из `state.json`, но такой загрузки сейчас нет.
- **Гео-блокировки.** Bybit отдаёт публичный рынок не из всех стран/дата-центров.
  Проверь до деплоя: `curl -s "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT"`.
  Если приходит ошибка — нужен выходной прокси/другой регион узла.
- **Время сервера.** Логика выходов считает минуты по системным часам; убедись,
  что на узле работает NTP, и задай `TZ`, иначе времена в журнале будут в UTC.
- **Нагрузка.** ~1-3 запроса в секунду к бирже, встроенный лимитер держит ≤8 rps.
  Хватает 100m CPU и 128Mi памяти, в лимитах стоит 500m/512Mi.
- **Диск.** `trades.csv` растёт медленно, а вот `bot.log` — заметно. 2Gi хватит
  надолго, но ротацию логов при желании настрой отдельно.
- **Это симулятор.** Демо-сделки считаются по последней цене без проскальзывания;
  на реальных деньгах результат будет хуже, чем в журнале.

---

## 8. Файлы, которые за это отвечают

```
Dockerfile               образ (python:3.12-slim,non-root, healthcheck на /healthz)
.dockerignore            что не тащить в образ
docker-compose.yml       локальный/серверный запуск без Kubernetes
k8s/tradingbot.yaml      namespace, PVC, ConfigMap, Deployment, Service, Ingress
bot/web.py               HTTP-сервер и сама страница
bot/journal.py           то, что пишет данные, которые страница показывает
```

---

## 9. Как боты развёрнуты в этом репозитории (маршрут `/tb`)

Здесь уже есть кластер, ArgoCD и nginx-клиент, поэтому отдельный namespace и
собственный ingress ботам не нужны — они въезжают в общую схему как ещё
несколько сервисов в неймспейсе `app`, а их данные показывает страница `/tb` на
том же адресе, что и остальной сайт (NodePort клиента,
`http://<IP сервера>:30081/tb`).

**Один под = одна ТС = свой том.** Общий журнал смешал бы статистику разных
систем, а сравнить их — весь смысл затеи. Образ у всех подов один, различается
ключ `--strategy`. Сейчас ТС четыре: `impulse`, `density`, `breakout`, `btc`.

```
браузер :30081/tb
      │
      ├── /tb                     ─► nginx отдаёт apps/client/html/tb.html (статика из образа)
      ├── /tb/api/impulse/state   ─► Service tradingbot-impulse:8080  ─► /api/state
      ├── /tb/api/density/state   ─► Service tradingbot-density:8080  ─► /api/state
      ├── /tb/api/breakout/state  ─► Service tradingbot-breakout:8080 ─► /api/state
      └── /tb/api/btc/state       ─► Service tradingbot-btc:8080      ─► /api/state
                                         └─ у каждого свой под, свой PVC, api.bybit.com
```

На странице боты переключаются вкладками, и в самих вкладках сразу видны итог,
число сделок и winrate каждой ТС — сравнение не требует переключения.
Ссылка на конкретного бота: `/tb#density`.

Что где лежит:

| Файл | Роль |
|------|------|
| `apps/tradingbot/**` | код обеих ТС; сборка образа `ermakov880/devops-project-tradingbot` |
| `apps/client/html/tb.html` | страница: вкладки ботов, карточки, кривая, лимитки, таблицы, лог |
| `apps/client/nginx.conf` | маршруты `/tb`, `/tb/api/<бот>/`, `/tb/<бот>/trades.csv` |
| `infrastructure/k8s/base/tradingbot-chart/` | чарт: по Deployment, Service и PVC на каждого бота из `values.bots` |
| `infrastructure/k8s/argocd/tradingbot-app.yaml` | ArgoCD-приложение, namespace `app` |
| `.github/workflows/deploy.yaml` | selftest → сборка образа → trivy → обновление тега в values |

Отличия от варианта из раздела 5:

- **Namespace `app`, а не `tradingbot`.** В `app` действует `default-deny-all`, и
  NetworkPolicy ботов пускает к ним только под с меткой `app: myapp` (nginx).
  Снаружи кластера порты ботов не открыты вовсе — панель доступна лишь через `/tb`.
- **Своя страница бота (`GET /` в поде) наружу не отдаётся.** Она ходит за данными
  в абсолютный `/api/state`, а этот префикс у клиента занят `server-app`.
  Поэтому страница живёт в образе клиента и читает `/tb/api/<бот>/...`.
- **Деплой через ArgoCD.** `kubectl apply` руками не нужен: пуш в master →
  CI собирает образ и подставляет git SHA в `values.yaml` → ArgoCD синхронизирует.
  Параметры ТС меняются в `values.yaml` (`bots[].args`), а не `kubectl edit deploy`.
- **Ресурсы урезаны** под квоту неймспейса `app`: 100m/128Mi в запросах,
  300m/256Mi в лимитах — на каждого бота. С четырьмя ботами суммарно выходит
  850m/1152Mi запросов при квоте 1500m/1.5Gi: место ещё есть, но добавляя
  пятую ТС, сверьтесь с `infrastructure/k8s/argocd/namespace.yaml`.

### Как добавить ещё одну ТС

Три места, все рядом:

1. `apps/tradingbot/bot/<своя>.py` + ветка в `run.py: _make_engine()` и значение
   в `--strategy`;
2. элемент в `bots[]` в `infrastructure/k8s/base/tradingbot-chart/values.yaml`
   (появятся Deployment, Service и PVC) и роут в `apps/client/nginx.conf`;
3. элемент в `BOTS` в `apps/client/html/tb.html` — вкладка появится сама.

Полезное:

```bash
kubectl -n app logs -f deploy/tradingbot-density
kubectl -n app exec deploy/tradingbot-density -- python run.py report      # сводка по сделкам
kubectl -n app exec deploy/tradingbot-density -- python run.py selftest    # логика без сети
kubectl -n app port-forward deploy/tradingbot-density 8080:8080            # родная панель бота
```

Предупреждение из раздела 7 остаётся в силе: панель не защищена авторизацией.
Сейчас она открыта всем, кто дотянется до NodePort клиента, — как и остальной сайт.
