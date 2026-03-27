"""Web Admin Panel — aiohttp панель управления"""

import json
from aiohttp import web
from loguru import logger

import config
import db


async def start_web_admin(application):
    """Запустить веб-сервер на event loop бота"""
    app = web.Application(middlewares=[auth_middleware])
    app["bot_app"] = application

    # API endpoints
    app.router.add_get("/api/settings", api_get_settings)
    app.router.add_post("/api/settings", api_save_settings)
    app.router.add_get("/api/markets", api_get_markets)
    app.router.add_get("/api/signals", api_get_signals)
    app.router.add_get("/api/trades", api_get_trades)
    app.router.add_get("/api/portfolio", api_get_portfolio)
    app.router.add_get("/api/users", api_get_users)
    app.router.add_post("/api/trade/close/{trade_id}", api_close_trade)
    app.router.add_get("/health", health_check)
    app.router.add_get("/", index_page)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.WEB_ADMIN_PORT)
    await site.start()


@web.middleware
async def auth_middleware(request, handler):
    """Token-based auth"""
    # Health check без авторизации
    if request.path == "/health":
        return await handler(request)

    token = (
        request.query.get("token")
        or request.headers.get("X-Admin-Token")
        or request.cookies.get("admin_token")
    )

    if not token or token != config.WEB_ADMIN_TOKEN:
        return web.json_response({"error": "Unauthorized"}, status=401)

    response = await handler(request)

    # Ставим cookie
    if isinstance(response, web.Response) and token:
        response.set_cookie("admin_token", token, max_age=86400 * 30)

    return response


async def health_check(request):
    return web.json_response({"status": "ok"})


async def api_get_settings(request):
    return web.json_response(config.get_all_settings())


async def api_save_settings(request):
    try:
        data = await request.json()
        config.save_settings(data)
        config.reload_dynamic()

        # Пересоздать расписание
        bot_app = request.app["bot_app"]
        scheduler = bot_app.bot_data.get("scheduler")
        if scheduler:
            scheduler.reschedule()

        return web.json_response({"status": "ok"})
    except Exception as e:
        logger.error(f"Ошибка сохранения настроек: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def api_get_markets(request):
    markets = await db.get_active_markets()
    # Добавить последние цены
    result = []
    for m in markets:
        latest = await db.get_latest_price(m["id"])
        m["latest_price"] = latest["price_yes"] if latest else None
        m["price_updated"] = latest["recorded_at"] if latest else None
        result.append(m)
    return web.json_response(result)


async def api_get_signals(request):
    limit = int(request.query.get("limit", 50))
    signals = await db.get_recent_signals(limit)
    return web.json_response(signals)


async def api_get_trades(request):
    limit = int(request.query.get("limit", 50))
    trades = await db.get_trade_history(limit)
    return web.json_response(trades)


async def api_get_portfolio(request):
    stats = await db.get_portfolio_stats()
    today = await db.get_today_pnl()
    return web.json_response({"portfolio": stats, "today": today})


async def api_get_users(request):
    users = await db.get_connected_users()
    # Не отдаём секреты
    safe = []
    for u in users:
        safe.append({
            "id": u["id"],
            "telegram_id": u["telegram_id"],
            "username": u.get("username", ""),
            "connected": bool(u.get("api_key")),
            "created_at": u.get("created_at", ""),
        })
    return web.json_response(safe)


async def api_close_trade(request):
    trade_id = int(request.match_info["trade_id"])
    bot_app = request.app["bot_app"]
    trader = bot_app.bot_data.get("trader")

    if not trader:
        return web.json_response({"error": "Trader not initialized"}, status=400)

    result = await trader.close_position(trade_id, reason="web_admin")
    if result:
        return web.json_response({"status": "closed", "trade_id": trade_id})
    return web.json_response({"error": "Failed to close"}, status=500)


async def index_page(request):
    """SPA админ-панель"""
    token = request.query.get("token", "")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Bot Admin</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        background: #0f1923; color: #e1e8ed; padding: 20px; }}
.container {{ max-width: 1200px; margin: 0 auto; }}
h1 {{ color: #4A90D9; margin-bottom: 20px; }}
h2 {{ color: #8899a6; margin: 20px 0 10px; font-size: 16px; text-transform: uppercase; }}
.card {{ background: #1a2734; border-radius: 12px; padding: 20px; margin-bottom: 16px;
         border: 1px solid #253341; }}
.stat {{ display: inline-block; margin-right: 30px; }}
.stat-value {{ font-size: 24px; font-weight: bold; color: #fff; }}
.stat-label {{ font-size: 12px; color: #8899a6; }}
.green {{ color: #17bf63; }}
.red {{ color: #e0245e; }}
.btn {{ background: #4A90D9; color: #fff; border: none; padding: 8px 16px;
        border-radius: 6px; cursor: pointer; font-size: 14px; margin: 4px; }}
.btn:hover {{ background: #357abd; }}
.btn-danger {{ background: #e0245e; }}
.btn-danger:hover {{ background: #c5203f; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #253341; }}
th {{ color: #8899a6; font-size: 12px; text-transform: uppercase; }}
td {{ font-size: 14px; }}
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; }}
.badge-buy {{ background: #17bf6333; color: #17bf63; }}
.badge-sell {{ background: #e0245e33; color: #e0245e; }}
#loading {{ text-align: center; padding: 40px; color: #8899a6; }}
</style>
</head>
<body>
<div class="container">
<h1>📊 Polymarket Bot</h1>
<div id="loading">Loading...</div>
<div id="app" style="display:none">

<div class="card" id="portfolio-card"></div>

<div style="margin-bottom:16px">
    <button class="btn" onclick="refresh()">🔄 Refresh</button>
    <button class="btn" id="trade-toggle" onclick="toggleTrading()">Toggle Trading</button>
</div>

<h2>Markets</h2>
<div class="card"><table id="markets-table"><thead>
    <tr><th>#</th><th>Question</th><th>Category</th><th>YES Price</th><th>Updated</th></tr>
</thead><tbody></tbody></table></div>

<h2>Recent Signals</h2>
<div class="card"><table id="signals-table"><thead>
    <tr><th>Time</th><th>Question</th><th>Direction</th><th>Confidence</th><th>Type</th></tr>
</thead><tbody></tbody></table></div>

<h2>Trades</h2>
<div class="card"><table id="trades-table"><thead>
    <tr><th>Time</th><th>Question</th><th>Side</th><th>Size</th><th>Price</th><th>P&L</th><th>Status</th><th></th></tr>
</thead><tbody></tbody></table></div>

</div>
</div>

<script>
const TOKEN = '{token}';
const API = (path) => path + (path.includes('?') ? '&' : '?') + 'token=' + TOKEN;

async function fetchJSON(path) {{
    const r = await fetch(API(path));
    return r.json();
}}

async function refresh() {{
    const [portfolio, markets, signals, trades] = await Promise.all([
        fetchJSON('/api/portfolio'),
        fetchJSON('/api/markets'),
        fetchJSON('/api/signals?limit=20'),
        fetchJSON('/api/trades?limit=20'),
    ]);
    renderPortfolio(portfolio);
    renderMarkets(markets);
    renderSignals(signals);
    renderTrades(trades);
    document.getElementById('loading').style.display = 'none';
    document.getElementById('app').style.display = 'block';
}}

function renderPortfolio(data) {{
    const p = data.portfolio || {{}};
    const t = data.today || {{}};
    const pnlClass = (p.realized_pnl || 0) >= 0 ? 'green' : 'red';
    const todayClass = (t.total_pnl || 0) >= 0 ? 'green' : 'red';
    document.getElementById('portfolio-card').innerHTML = `
        <div class="stat"><div class="stat-value">${{p.open_positions || 0}}</div><div class="stat-label">Open Positions</div></div>
        <div class="stat"><div class="stat-value">${{(p.total_invested || 0).toFixed(2)}}</div><div class="stat-label">Invested ($)</div></div>
        <div class="stat"><div class="stat-value ${{pnlClass}}">${{(p.realized_pnl || 0) >= 0 ? '+' : ''}}${{(p.realized_pnl || 0).toFixed(2)}}</div><div class="stat-label">Realized P&L</div></div>
        <div class="stat"><div class="stat-value ${{todayClass}}">${{(t.total_pnl || 0) >= 0 ? '+' : ''}}${{(t.total_pnl || 0).toFixed(2)}}</div><div class="stat-label">Today P&L</div></div>
        <div class="stat"><div class="stat-value">${{(p.win_rate || 0).toFixed(0)}}%</div><div class="stat-label">Win Rate</div></div>
        <div class="stat"><div class="stat-value">${{p.wins || 0}}W / ${{p.losses || 0}}L</div><div class="stat-label">Record</div></div>
    `;
}}

function renderMarkets(markets) {{
    const tbody = document.querySelector('#markets-table tbody');
    tbody.innerHTML = markets.slice(0, 30).map((m, i) => `
        <tr>
            <td>${{i + 1}}</td>
            <td>${{m.question?.substring(0, 60) || 'N/A'}}</td>
            <td>${{m.category || ''}}</td>
            <td>${{m.latest_price ? (m.latest_price * 100).toFixed(0) + '%' : 'N/A'}}</td>
            <td>${{m.price_updated?.substring(11, 16) || ''}}</td>
        </tr>
    `).join('');
}}

function renderSignals(signals) {{
    const tbody = document.querySelector('#signals-table tbody');
    tbody.innerHTML = signals.map(s => `
        <tr>
            <td>${{s.created_at?.substring(11, 16) || ''}}</td>
            <td>${{s.question?.substring(0, 50) || 'N/A'}}</td>
            <td><span class="badge badge-${{s.direction?.toLowerCase()}}">${{s.direction}}</span></td>
            <td>${{(s.confidence || 0).toFixed(2)}}</td>
            <td>${{s.signal_type || ''}}</td>
        </tr>
    `).join('');
}}

function renderTrades(trades) {{
    const tbody = document.querySelector('#trades-table tbody');
    tbody.innerHTML = trades.map(t => {{
        const pnlClass = (t.pnl || 0) >= 0 ? 'green' : 'red';
        const closeBtn = ['pending', 'filled'].includes(t.status)
            ? `<button class="btn btn-danger" onclick="closeTrade(${{t.id}})">Close</button>` : '';
        return `
        <tr>
            <td>${{t.created_at?.substring(11, 16) || ''}}</td>
            <td>${{t.question?.substring(0, 40) || 'N/A'}}</td>
            <td>${{t.side}}</td>
            <td>$${{(t.size_usdc || 0).toFixed(2)}}</td>
            <td>${{(t.price || 0).toFixed(4)}}</td>
            <td class="${{pnlClass}}">$${{(t.pnl || 0).toFixed(2)}}</td>
            <td>${{t.status}}</td>
            <td>${{closeBtn}}</td>
        </tr>`;
    }}).join('');
}}

async function toggleTrading() {{
    await fetch(API('/api/trading/toggle'), {{ method: 'POST' }});
    refresh();
}}

async function closeTrade(id) {{
    if (!confirm('Close this trade?')) return;
    await fetch(API('/api/trade/close/' + id), {{ method: 'POST' }});
    refresh();
}}

refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")
