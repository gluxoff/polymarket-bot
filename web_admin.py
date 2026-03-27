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


async def start_setup_web_admin():
    """Запустить только веб-панель для первичной настройки (без Telegram-бота)"""
    app = web.Application(middlewares=[auth_middleware])
    app["bot_app"] = None

    app.router.add_get("/api/settings", api_get_settings)
    app.router.add_post("/api/settings", api_save_settings)
    app.router.add_get("/health", health_check)
    app.router.add_get("/", setup_page)

    runner = web.AppRunner(app)
    await runner.setup()
    port = config.WEB_ADMIN_PORT or 8081
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


@web.middleware
async def auth_middleware(request, handler):
    if request.path == "/health":
        return await handler(request)

    token = (
        request.query.get("token")
        or request.headers.get("X-Admin-Token")
        or request.cookies.get("admin_token")
    )

    admin_token = config.WEB_ADMIN_TOKEN or "setup"
    if not token or token != admin_token:
        return web.json_response({"error": "Unauthorized"}, status=401)

    response = await handler(request)
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

        bot_app = request.app.get("bot_app")
        if bot_app:
            scheduler = bot_app.bot_data.get("scheduler")
            if scheduler:
                scheduler.reschedule()

        return web.json_response({"status": "ok"})
    except Exception as e:
        logger.error(f"Ошибка сохранения настроек: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def api_get_markets(request):
    markets = await db.get_active_markets()
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
    bot_app = request.app.get("bot_app")
    if not bot_app:
        return web.json_response({"error": "Bot not running"}, status=400)
    trader = bot_app.bot_data.get("trader")
    if not trader:
        return web.json_response({"error": "Trader not initialized"}, status=400)
    result = await trader.close_position(trade_id, reason="web_admin")
    if result:
        return web.json_response({"status": "closed", "trade_id": trade_id})
    return web.json_response({"error": "Failed to close"}, status=500)


# ── Страница первичной настройки ─────────────────────────────

async def setup_page(request):
    token = request.query.get("token", "")
    settings = config.get_all_settings()
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Bot — Setup</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        background: #0f1923; color: #e1e8ed; display: flex; justify-content: center; padding: 40px 20px; }}
.setup {{ max-width: 600px; width: 100%; }}
h1 {{ color: #4A90D9; margin-bottom: 8px; }}
.subtitle {{ color: #8899a6; margin-bottom: 30px; }}
.card {{ background: #1a2734; border-radius: 12px; padding: 24px; margin-bottom: 16px;
         border: 1px solid #253341; }}
h2 {{ color: #8899a6; font-size: 14px; text-transform: uppercase; margin-bottom: 16px; }}
label {{ display: block; color: #8899a6; font-size: 13px; margin-bottom: 4px; }}
input {{ width: 100%; padding: 10px 12px; background: #0f1923; border: 1px solid #253341;
         border-radius: 6px; color: #e1e8ed; font-size: 14px; margin-bottom: 16px; }}
input:focus {{ outline: none; border-color: #4A90D9; }}
.hint {{ color: #657786; font-size: 12px; margin-top: -12px; margin-bottom: 16px; }}
.btn {{ background: #4A90D9; color: #fff; border: none; padding: 12px 24px;
        border-radius: 8px; cursor: pointer; font-size: 16px; width: 100%; }}
.btn:hover {{ background: #357abd; }}
.success {{ background: #17bf6333; color: #17bf63; padding: 12px; border-radius: 8px;
            text-align: center; margin-top: 16px; display: none; }}
.error {{ background: #e0245e33; color: #e0245e; padding: 12px; border-radius: 8px;
          text-align: center; margin-top: 16px; display: none; }}
</style>
</head>
<body>
<div class="setup">
<h1>📊 Polymarket Bot</h1>
<p class="subtitle">Первичная настройка. Заполни поля и перезапусти бота.</p>

<div class="card">
<h2>Telegram</h2>
<label>Bot Token</label>
<input id="bot_token" type="text" placeholder="123456:ABC-DEF..." value="{settings.get('telegram_bot_token', '')}">
<div class="hint">Получи у @BotFather в Telegram</div>

<label>Channel ID</label>
<input id="channel_id" type="text" placeholder="-100xxxxxxxxxx" value="{settings.get('telegram_channel_id', '')}">
<div class="hint">ID канала для публикации сигналов (опционально)</div>

<label>Admin Telegram ID</label>
<input id="admin_id" type="text" placeholder="123456789" value="{settings.get('admin_telegram_id', 0) or ''}">
<div class="hint">Твой ID. Узнай у @userinfobot</div>
</div>

<div class="card">
<h2>Параметры</h2>
<label>Scan Interval (min)</label>
<input id="scan_interval" type="number" value="{settings.get('scan_interval_minutes', 10)}">

<label>Analysis Interval (min)</label>
<input id="analysis_interval" type="number" value="{settings.get('deep_analysis_interval_minutes', 60)}">

<label>Max Bet Size (USDC)</label>
<input id="max_bet" type="number" step="0.01" value="{settings.get('max_bet_size_usdc', 10)}">

<label>Max Daily Loss (USDC)</label>
<input id="max_loss" type="number" step="0.01" value="{settings.get('max_daily_loss_usdc', 50)}">
</div>

<button class="btn" onclick="saveSettings()">💾 Сохранить настройки</button>
<div class="success" id="success-msg">✅ Сохранено! Перезапусти бота: <code>systemctl restart polymarket-bot</code></div>
<div class="error" id="error-msg"></div>
</div>

<script>
const TOKEN = '{token}';
const API = (path) => path + '?token=' + TOKEN;

async function saveSettings() {{
    const data = {{
        telegram_bot_token: document.getElementById('bot_token').value.trim(),
        telegram_channel_id: document.getElementById('channel_id').value.trim(),
        admin_telegram_id: parseInt(document.getElementById('admin_id').value) || 0,
        scan_interval_minutes: parseInt(document.getElementById('scan_interval').value) || 10,
        deep_analysis_interval_minutes: parseInt(document.getElementById('analysis_interval').value) || 60,
        max_bet_size_usdc: parseFloat(document.getElementById('max_bet').value) || 10,
        max_daily_loss_usdc: parseFloat(document.getElementById('max_loss').value) || 50,
    }};
    try {{
        const r = await fetch(API('/api/settings'), {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(data),
        }});
        if (r.ok) {{
            document.getElementById('success-msg').style.display = 'block';
            document.getElementById('error-msg').style.display = 'none';
        }} else {{
            throw new Error('HTTP ' + r.status);
        }}
    }} catch(e) {{
        document.getElementById('error-msg').textContent = 'Ошибка: ' + e.message;
        document.getElementById('error-msg').style.display = 'block';
        document.getElementById('success-msg').style.display = 'none';
    }}
}}
</script>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


# ── Основная админ-панель (когда бот работает) ────────────────

async def index_page(request):
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
.tabs {{ display: flex; gap: 4px; margin-bottom: 16px; }}
.tab {{ padding: 8px 16px; border-radius: 6px; cursor: pointer; background: #1a2734; color: #8899a6; border: 1px solid #253341; }}
.tab.active {{ background: #4A90D9; color: #fff; border-color: #4A90D9; }}
.panel {{ display: none; }}
.panel.active {{ display: block; }}
#loading {{ text-align: center; padding: 40px; color: #8899a6; }}
label {{ display: block; color: #8899a6; font-size: 13px; margin-bottom: 4px; }}
input {{ width: 100%; padding: 8px 10px; background: #0f1923; border: 1px solid #253341;
         border-radius: 6px; color: #e1e8ed; font-size: 14px; margin-bottom: 12px; }}
input:focus {{ outline: none; border-color: #4A90D9; }}
.save-msg {{ color: #17bf63; font-size: 13px; margin-top: 8px; display: none; }}
</style>
</head>
<body>
<div class="container">
<h1>📊 Polymarket Bot</h1>

<div class="tabs">
    <div class="tab active" onclick="showTab('dashboard')">Dashboard</div>
    <div class="tab" onclick="showTab('settings')">Settings</div>
</div>

<div id="loading">Loading...</div>

<!-- Dashboard -->
<div id="tab-dashboard" class="panel active">
<div class="card" id="portfolio-card"></div>
<div style="margin-bottom:16px">
    <button class="btn" onclick="refresh()">🔄 Refresh</button>
</div>
<h2>Markets</h2>
<div class="card"><table id="markets-table"><thead>
    <tr><th>#</th><th>Question</th><th>Category</th><th>YES</th><th>Updated</th></tr>
</thead><tbody></tbody></table></div>
<h2>Signals</h2>
<div class="card"><table id="signals-table"><thead>
    <tr><th>Time</th><th>Question</th><th>Dir</th><th>Conf</th><th>Type</th></tr>
</thead><tbody></tbody></table></div>
<h2>Trades</h2>
<div class="card"><table id="trades-table"><thead>
    <tr><th>Time</th><th>Question</th><th>Side</th><th>Size</th><th>Price</th><th>P&L</th><th>Status</th><th></th></tr>
</thead><tbody></tbody></table></div>
</div>

<!-- Settings -->
<div id="tab-settings" class="panel">
<div class="card">
<h2>Telegram</h2>
<label>Bot Token</label>
<input id="s_bot_token" type="text" placeholder="123456:ABC-DEF...">
<label>Channel ID</label>
<input id="s_channel_id" type="text" placeholder="-100xxxxxxxxxx">
<label>Admin Telegram ID</label>
<input id="s_admin_id" type="text" placeholder="123456789">
</div>
<div class="card">
<h2>Parameters</h2>
<label>Scan Interval (min)</label>
<input id="s_scan" type="number">
<label>Analysis Interval (min)</label>
<input id="s_analysis" type="number">
<label>Max Bet Size (USDC)</label>
<input id="s_max_bet" type="number" step="0.01">
<label>Max Daily Loss (USDC)</label>
<input id="s_max_loss" type="number" step="0.01">
<label>Probability Shift Threshold</label>
<input id="s_prob_shift" type="number" step="0.01">
<label>Stop Loss %</label>
<input id="s_stop_loss" type="number" step="0.01">
</div>
<button class="btn" onclick="saveSettings()">💾 Save Settings</button>
<div class="save-msg" id="save-msg">✅ Saved! Restart bot to apply token/channel changes.</div>
</div>

</div>

<script>
const TOKEN = '{token}';
const API = (path) => path + (path.includes('?') ? '&' : '?') + 'token=' + TOKEN;

function showTab(name) {{
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.getElementById('tab-' + name).classList.add('active');
    event.target.classList.add('active');
    if (name === 'settings') loadSettings();
}}

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
}}

function renderPortfolio(data) {{
    const p = data.portfolio || {{}};
    const t = data.today || {{}};
    const pnlClass = (p.realized_pnl||0) >= 0 ? 'green' : 'red';
    document.getElementById('portfolio-card').innerHTML = `
        <div class="stat"><div class="stat-value">${{p.open_positions||0}}</div><div class="stat-label">Open Positions</div></div>
        <div class="stat"><div class="stat-value">${{(p.total_invested||0).toFixed(2)}}</div><div class="stat-label">Invested ($)</div></div>
        <div class="stat"><div class="stat-value ${{pnlClass}}">${{(p.realized_pnl||0)>=0?'+':''}}${{(p.realized_pnl||0).toFixed(2)}}</div><div class="stat-label">Realized P&L</div></div>
        <div class="stat"><div class="stat-value">${{(p.win_rate||0).toFixed(0)}}%</div><div class="stat-label">Win Rate</div></div>
        <div class="stat"><div class="stat-value">${{p.wins||0}}W / ${{p.losses||0}}L</div><div class="stat-label">Record</div></div>`;
}}
function renderMarkets(m) {{
    document.querySelector('#markets-table tbody').innerHTML = m.slice(0,30).map((x,i) => `<tr><td>${{i+1}}</td><td>${{x.question?.substring(0,55)||''}}</td><td>${{x.category||''}}</td><td>${{x.latest_price?(x.latest_price*100).toFixed(0)+'%':'N/A'}}</td><td>${{x.price_updated?.substring(11,16)||''}}</td></tr>`).join('');
}}
function renderSignals(s) {{
    document.querySelector('#signals-table tbody').innerHTML = s.map(x => `<tr><td>${{x.created_at?.substring(11,16)||''}}</td><td>${{x.question?.substring(0,50)||''}}</td><td><span class="badge badge-${{x.direction?.toLowerCase()}}">${{x.direction}}</span></td><td>${{(x.confidence||0).toFixed(2)}}</td><td>${{x.signal_type||''}}</td></tr>`).join('');
}}
function renderTrades(t) {{
    document.querySelector('#trades-table tbody').innerHTML = t.map(x => {{
        const cls = (x.pnl||0)>=0?'green':'red';
        const btn = ['pending','filled'].includes(x.status)?`<button class="btn btn-danger" onclick="closeTrade(${{x.id}})">Close</button>`:'';
        return `<tr><td>${{x.created_at?.substring(11,16)||''}}</td><td>${{x.question?.substring(0,40)||''}}</td><td>${{x.side}}</td><td>$${{(x.size_usdc||0).toFixed(2)}}</td><td>${{(x.price||0).toFixed(4)}}</td><td class="${{cls}}">$${{(x.pnl||0).toFixed(2)}}</td><td>${{x.status}}</td><td>${{btn}}</td></tr>`;
    }}).join('');
}}
async function closeTrade(id) {{
    if (!confirm('Close?')) return;
    await fetch(API('/api/trade/close/'+id), {{method:'POST'}});
    refresh();
}}

async function loadSettings() {{
    const s = await fetchJSON('/api/settings');
    document.getElementById('s_bot_token').value = s.telegram_bot_token || '';
    document.getElementById('s_channel_id').value = s.telegram_channel_id || '';
    document.getElementById('s_admin_id').value = s.admin_telegram_id || '';
    document.getElementById('s_scan').value = s.scan_interval_minutes || 10;
    document.getElementById('s_analysis').value = s.deep_analysis_interval_minutes || 60;
    document.getElementById('s_max_bet').value = s.max_bet_size_usdc || 10;
    document.getElementById('s_max_loss').value = s.max_daily_loss_usdc || 50;
    document.getElementById('s_prob_shift').value = s.probability_shift_threshold || 0.05;
    document.getElementById('s_stop_loss').value = s.stop_loss_percent || 0.20;
}}

async function saveSettings() {{
    const data = {{
        telegram_bot_token: document.getElementById('s_bot_token').value.trim(),
        telegram_channel_id: document.getElementById('s_channel_id').value.trim(),
        admin_telegram_id: parseInt(document.getElementById('s_admin_id').value) || 0,
        scan_interval_minutes: parseInt(document.getElementById('s_scan').value) || 10,
        deep_analysis_interval_minutes: parseInt(document.getElementById('s_analysis').value) || 60,
        max_bet_size_usdc: parseFloat(document.getElementById('s_max_bet').value) || 10,
        max_daily_loss_usdc: parseFloat(document.getElementById('s_max_loss').value) || 50,
        probability_shift_threshold: parseFloat(document.getElementById('s_prob_shift').value) || 0.05,
        stop_loss_percent: parseFloat(document.getElementById('s_stop_loss').value) || 0.20,
    }};
    const r = await fetch(API('/api/settings'), {{
        method: 'POST', headers: {{'Content-Type':'application/json'}}, body: JSON.stringify(data),
    }});
    if (r.ok) {{
        document.getElementById('save-msg').style.display = 'block';
        setTimeout(() => document.getElementById('save-msg').style.display = 'none', 5000);
    }}
}}

refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")
