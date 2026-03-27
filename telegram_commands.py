"""Команды Telegram-бота — админские команды"""

from telegram import Update
from telegram.ext import ContextTypes
from loguru import logger

import config
import db

# Глобальные ссылки (устанавливаются из main.py)
_publisher = None
_scheduler = None
_scanner = None
_trader = None
_chart_gen = None


def set_components(publisher, scheduler, scanner, trader=None, chart_gen=None):
    """Установить ссылки на компоненты"""
    global _publisher, _scheduler, _scanner, _trader, _chart_gen
    _publisher = publisher
    _scheduler = scheduler
    _scanner = scanner
    _trader = trader
    _chart_gen = chart_gen


def _is_admin(update: Update) -> bool:
    """Проверить что пользователь — админ"""
    return update.effective_user and update.effective_user.id == config.ADMIN_TELEGRAM_ID


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статус бота"""
    if not _is_admin(update):
        return

    markets = await db.get_active_markets()
    portfolio = await db.get_portfolio_stats()
    today_pnl = await db.get_today_pnl()

    next_runs = _scheduler.get_next_run_times() if _scheduler else {}
    next_scan = next_runs.get("market_scan", "N/A")
    next_analysis = next_runs.get("deep_analysis", "N/A")

    paused = "⏸ PAUSED" if (_scheduler and _scheduler.is_paused) else "▶️ Active"

    text = (
        f"📊 <b>Polymarket Bot Status</b>\n\n"
        f"Status: {paused}\n"
        f"Auto-trade: {'✅' if config.AUTO_TRADE_ENABLED else '❌'}\n\n"
        f"📡 Markets tracked: {len(markets)}\n"
        f"📂 Open positions: {portfolio['open_positions']}\n"
        f"💰 Invested: ${portfolio['total_invested']:.2f}\n\n"
        f"Today P&L: ${today_pnl.get('total_pnl', 0):+.2f}\n"
        f"Total P&L: ${portfolio['realized_pnl']:+.2f}\n"
        f"Win rate: {portfolio['win_rate']:.0f}%\n\n"
        f"⏰ Next scan: {next_scan}\n"
        f"⏰ Next analysis: {next_analysis}\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_markets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список отслеживаемых рынков"""
    if not _is_admin(update):
        return

    markets = await db.get_active_markets()
    if not markets:
        await update.message.reply_text("Нет отслеживаемых рынков")
        return

    lines = ["📊 <b>Tracked Markets</b>\n"]
    for i, m in enumerate(markets[:20], 1):
        latest = await db.get_latest_price(m["id"])
        price = f"{latest['price_yes'] * 100:.0f}%" if latest else "N/A"
        cat = f"[{m['category']}]" if m.get("category") else ""
        q = m["question"][:60] + ("..." if len(m["question"]) > 60 else "")
        lines.append(f"{i}. {cat} {q}\n   YES: {price}")

    text = "\n".join(lines)
    # Telegram лимит 4096 символов
    if len(text) > 4000:
        text = text[:4000] + "\n..."

    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Последние сигналы"""
    if not _is_admin(update):
        return

    signals = await db.get_recent_signals(10)
    if not signals:
        await update.message.reply_text("Нет сигналов")
        return

    lines = ["📋 <b>Recent Signals</b>\n"]
    for s in signals:
        conf = s["confidence"]
        direction = s["direction"]
        q = s["question"][:50] + ("..." if len(s["question"]) > 50 else "")
        change = s.get("probability_change", 0) * 100
        emoji = "🟢" if direction == "BUY" else "🔴"
        lines.append(
            f"{emoji} {direction} | {q}\n"
            f"   Conf: {conf:.2f} | Change: {change:+.1f}% | {s['signal_type']}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Портфель"""
    if not _is_admin(update):
        return

    portfolio = await db.get_portfolio_stats()
    open_trades = await db.get_open_trades()

    text = (
        f"💼 <b>Portfolio</b>\n\n"
        f"Open: {portfolio['open_positions']} (${portfolio['total_invested']:.2f})\n"
        f"Realized P&L: ${portfolio['realized_pnl']:+.2f}\n"
        f"Win rate: {portfolio['win_rate']:.0f}% ({portfolio['wins']}W / {portfolio['losses']}L)\n"
    )

    if open_trades:
        text += "\n<b>Open Positions:</b>\n"
        for t in open_trades[:10]:
            q = t["question"][:40] + ("..." if len(t["question"]) > 40 else "")
            text += f"  • {t['side']} ${t['size_usdc']:.2f} @ {t['price']:.2f} — {q}\n"

    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принудительное сканирование"""
    if not _is_admin(update):
        return

    await update.message.reply_text("🔍 Запуск сканирования...")

    if _scanner:
        count = await _scanner.scan_markets()
        await _scanner.update_prices()
        await update.message.reply_text(f"✅ Сканирование завершено: {count} рынков")
    else:
        await update.message.reply_text("❌ Scanner не инициализирован")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Поставить на паузу"""
    if not _is_admin(update):
        return
    if _scheduler:
        _scheduler.is_paused = True
        await update.message.reply_text("⏸ Бот поставлен на паузу")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Снять с паузы"""
    if not _is_admin(update):
        return
    if _scheduler:
        _scheduler.is_paused = False
        await update.message.reply_text("▶️ Бот возобновлён")


async def cmd_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручная ставка: /trade <market_id> <YES/NO> <amount>"""
    if not _is_admin(update):
        return

    if not _trader:
        await update.message.reply_text("❌ Автоторговля не инициализирована")
        return

    args = context.args
    if not args or len(args) < 3:
        await update.message.reply_text("Использование: /trade <market_id> <YES/NO> <amount>")
        return

    try:
        market_id = int(args[0])
        side = args[1].upper()
        amount = float(args[2])
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Неверные аргументы")
        return

    if side not in ("YES", "NO"):
        await update.message.reply_text("❌ side должен быть YES или NO")
        return

    market = await db.get_market_by_id(market_id)
    if not market:
        await update.message.reply_text(f"❌ Рынок #{market_id} не найден")
        return

    await update.message.reply_text(
        f"📤 Размещение ставки: {side} ${amount:.2f} на '{market['question'][:50]}...'"
    )

    result = await _trader.manual_trade(market, side, amount)
    if result:
        await update.message.reply_text(f"✅ Ставка размещена (trade #{result})")
    else:
        await update.message.reply_text("❌ Ошибка размещения ставки")


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Закрыть позицию: /close <trade_id>"""
    if not _is_admin(update):
        return

    if not _trader:
        await update.message.reply_text("❌ Автоторговля не инициализирована")
        return

    args = context.args
    if not args:
        await update.message.reply_text("Использование: /close <trade_id>")
        return

    try:
        trade_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный trade_id")
        return

    result = await _trader.close_position(trade_id, reason="manual")
    if result:
        await update.message.reply_text(f"✅ Позиция #{trade_id} закрыта")
    else:
        await update.message.reply_text(f"❌ Не удалось закрыть позицию #{trade_id}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Помощь"""
    if not _is_admin(update):
        return

    text = (
        "📊 <b>Polymarket Bot Commands</b>\n\n"
        "/status — Статус бота\n"
        "/markets — Отслеживаемые рынки\n"
        "/signals — Последние сигналы\n"
        "/portfolio — Портфель\n"
        "/scan — Запустить сканирование\n"
        "/pause — Пауза\n"
        "/resume — Возобновить\n"
        "/trade &lt;id&gt; &lt;YES/NO&gt; &lt;$&gt; — Ручная ставка\n"
        "/close &lt;trade_id&gt; — Закрыть позицию\n"
        "/help — Эта справка\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")
