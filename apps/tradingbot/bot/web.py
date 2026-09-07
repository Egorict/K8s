"""Веб-панель: отдаёт данные демо-торговли по HTTP.

Поднимается в том же процессе и том же event loop, что и бот, поэтому
никакой отдельной базы не нужно - источник данных ровно один, каталог data/:

    data/state.json    -> GET /api/state    (открытые позиции + статистика)
    data/trades.csv    -> GET /api/trades   (закрытые сделки)
    data/signals.csv   -> GET /api/signals  (сработавшие сетапы)
    data/bot.log       -> GET /api/log      (хвост лога)
                          GET /            (страница, которая всё это рисует)
                          GET /healthz     (проба для Kubernetes)
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
from typing import Dict, List

from aiohttp import web

from .config import Config

log = logging.getLogger("web")


def _read_csv(path: str, limit: int) -> List[Dict[str, str]]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter=";"))
    return rows[-limit:][::-1]  # свежие сверху


def _read_json(path: str) -> Dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _tail(path: str, lines: int) -> List[str]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return [l.rstrip("\n") for l in fh.readlines()[-lines:]]


def build_app(cfg: Config) -> web.Application:
    app = web.Application()

    async def index(_request: web.Request) -> web.Response:
        return web.Response(text=DASHBOARD_HTML, content_type="text/html", charset="utf-8")

    async def api_state(_request: web.Request) -> web.Response:
        return web.json_response(_read_json(cfg.state_json))

    async def api_trades(request: web.Request) -> web.Response:
        limit = min(int(request.query.get("limit", 200)), 2000)
        return web.json_response(_read_csv(cfg.trades_csv, limit))

    async def api_signals(request: web.Request) -> web.Response:
        limit = min(int(request.query.get("limit", 100)), 1000)
        return web.json_response(_read_csv(cfg.signals_csv, limit))

    async def api_log(request: web.Request) -> web.Response:
        lines = min(int(request.query.get("lines", 200)), 2000)
        return web.json_response(_tail(cfg.log_file, lines))

    async def api_trades_csv(_request: web.Request) -> web.Response:
        """Выгрузка журнала как есть - открыть в Excel."""
        if not os.path.exists(cfg.trades_csv):
            raise web.HTTPNotFound()
        return web.FileResponse(cfg.trades_csv, headers={
            "Content-Disposition": 'attachment; filename="trades.csv"'
        })

    async def healthz(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "mode": cfg.mode})

    app.add_routes([
        web.get("/", index),
        web.get("/api/state", api_state),
        web.get("/api/trades", api_trades),
        web.get("/api/signals", api_signals),
        web.get("/api/log", api_log),
        web.get("/trades.csv", api_trades_csv),
        web.get("/healthz", healthz),
    ])
    return app


async def start_web(cfg: Config) -> web.AppRunner:
    runner = web.AppRunner(build_app(cfg), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, cfg.web_host, cfg.web_port)
    await site.start()
    log.info("Веб-панель: http://%s:%d", cfg.web_host, cfg.web_port)
    return runner


DASHBOARD_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Демо-бот · журнал торговли</title>
<style>
  :root {
    --bg: #0f1216; --card: #171b21; --line: #262c35;
    --fg: #e6e9ee; --dim: #8a93a0; --green: #3fbf7f; --red: #e5555a; --accent: #5b8def;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--fg);
         font: 14px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif; }
  header { padding: 18px 24px; border-bottom: 1px solid var(--line);
           display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap; }
  h1 { font-size: 17px; margin: 0; font-weight: 600; }
  .upd { color: var(--dim); font-size: 12px; }
  main { padding: 20px 24px 60px; max-width: 1400px; }
  .cards { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
           margin-bottom: 24px; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; }
  .card .k { color: var(--dim); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
  .card .v { font-size: 22px; font-weight: 600; margin-top: 4px; font-variant-numeric: tabular-nums; }
  h2 { font-size: 14px; text-transform: uppercase; letter-spacing: .05em;
       color: var(--dim); margin: 28px 0 10px; font-weight: 600; }
  .scroll { overflow-x: auto; border: 1px solid var(--line); border-radius: 10px; background: var(--card); }
  table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
  th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--line); white-space: nowrap; }
  th { color: var(--dim); font-weight: 600; font-size: 12px; position: sticky; top: 0; background: var(--card); }
  tr:last-child td { border-bottom: none; }
  .pos { color: var(--green); } .neg { color: var(--red); }
  .sym { font-weight: 600; }
  .muted { color: var(--dim); white-space: normal; max-width: 420px; }
  .empty { padding: 24px; color: var(--dim); }
  a { color: var(--accent); }
  pre { margin: 0; padding: 14px; background: var(--card); border: 1px solid var(--line);
        border-radius: 10px; overflow-x: auto; font-size: 12px; color: var(--dim); max-height: 320px; }
</style>
</head>
<body>
<header>
  <h1>Демо-бот · шорт истощения импульса</h1>
  <span class="upd" id="upd">загрузка…</span>
  <span class="upd"><a href="/trades.csv">скачать trades.csv</a></span>
</header>
<main>
  <div class="cards" id="cards"></div>

  <h2>Открытые позиции</h2>
  <div class="scroll"><div id="open"></div></div>

  <h2>Сделки</h2>
  <div class="scroll"><div id="trades"></div></div>

  <h2>Наблюдение</h2>
  <div id="watch" class="muted"></div>

  <h2>Лог</h2>
  <pre id="log"></pre>
</main>
<script>
const num = (v, d = 2) => Number(v).toFixed(d);
const sign = v => (Number(v) >= 0 ? 'pos' : 'neg');
const esc = s => String(s ?? '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

function table(cols, rows, render) {
  if (!rows.length) return '<div class="empty">пока пусто</div>';
  const head = cols.map(c => `<th>${c}</th>`).join('');
  const body = rows.map(r => `<tr>${render(r)}</tr>`).join('');
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

async function refresh() {
  const [state, trades, log] = await Promise.all([
    fetch('/api/state').then(r => r.json()).catch(() => ({})),
    fetch('/api/trades?limit=200').then(r => r.json()).catch(() => []),
    fetch('/api/log?lines=60').then(r => r.json()).catch(() => []),
  ]);

  const s = state.stats || {};
  document.getElementById('upd').textContent = 'обновлено ' + (state.updated || '—');

  document.getElementById('cards').innerHTML = [
    ['Итог, $', `<span class="${sign(s.net_pnl_usd || 0)}">${num(s.net_pnl_usd || 0)}</span>`],
    ['Сделок', s.trades ?? 0],
    ['Winrate', num(s.winrate || 0, 1) + '%'],
    ['Профит-фактор', Number.isFinite(s.profit_factor) ? num(s.profit_factor || 0) : '∞'],
    ['Открыто', s.open ?? 0],
    ['В наблюдении', (state.watchlist || []).length],
  ].map(([k, v]) => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('');

  document.getElementById('open').innerHTML = table(
    ['Монета', 'ТФ', 'Открыта', 'Вход', 'Сейчас', 'Стоп', 'Тейк', 'Лучший PnL', 'Мин'],
    state.open_positions || [],
    p => `<td class="sym">${esc(p.symbol)}</td><td>${esc(p.timeframe)}</td><td>${esc(p.opened)}</td>
          <td>${p.entry_price}</td><td>${p.last_price}</td><td>${p.stop_price}</td><td>${p.take_price}</td>
          <td class="${sign(p.best_pnl_usd)}">${num(p.best_pnl_usd)}$</td><td>${p.age_min}</td>`);

  document.getElementById('trades').innerHTML = table(
    ['#', 'Монета', 'ТФ', 'Вход', 'Выход', 'Цена входа', 'Цена выхода', 'Профит, $', '% к марже', 'Мин', 'Причина'],
    trades,
    t => `<td>${esc(t.num)}</td><td class="sym">${esc(t.symbol)}</td><td>${esc(t.timeframe)}</td>
          <td>${esc(t.open_time)}</td><td>${esc(t.close_time)}</td>
          <td>${esc(t.entry_price)}</td><td>${esc(t.exit_price)}</td>
          <td class="${sign(t.profit_usd)}">${num(t.profit_usd)}</td>
          <td class="${sign(t.profit_pct_margin)}">${num(t.profit_pct_margin, 1)}%</td>
          <td>${esc(t.duration_min)}</td><td class="muted">${esc(t.exit_reason)}</td>`);

  document.getElementById('watch').textContent = (state.watchlist || []).join(', ') || 'пусто';
  document.getElementById('log').textContent = log.join('\\n');
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
