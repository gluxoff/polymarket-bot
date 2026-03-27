"""Команды Telegram-бота — юзерские + админские"""

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters
from loguru import logger

import config
import db

# Глобальные ссылки (устанавливаются из main.py)
_publisher = None
_scheduler = None
_scanner = None
_client = None  # PolymarketClient
_chart_gen = None

# Состояния ConversationHandler для /connect
WAITING_API_KEY, WAITING_API_SECRET, WAITING_API_PASSPHRASE = range(3)


def set_components(publisher, scheduler, scanner, client=None, chart_gen=None):
    global _publisher, _scheduler, _scanner, _client, _chart_gen
    _publisher = publisher
    _scheduler = scheduler
    _scanner = scanner
    _client = client
    _chart_gen = chart_gen


def _is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == config.ADMIN_TELEGRAM_ID


def _is_private(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.type == "private"


# ── Юзерские команды (личка) ─────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветствие и регистрация"""
    if not _is_private(update):
        return

    user = update.effective_user
    await db.save_user(user.id, user.username or "")

    text = (
        f"👋 Привет, {user.first_name}!\n\n"
        "Я бот для прогнозов на <b>Polymarket</b>.\n\n"
        "📊 Сигналы публикуются в канале автоматически.\n"
        "💰 Чтобы торговать через меня — подключи свой Polymarket.\n\n"
        "<b>Команды:</b>\n"
        "/connect — Подключить Polymarket (API ключи)\n"
        "/disconnect — Отключить аккаунт\n"
        "/portfolio — Мой портфель\n"
        "/trade — Сделать ставку\n"
        "/markets — Активные рынки\n"
        "/help — Справка"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Справка"""
    if not _is_private(update):
        return

    is_admin = _is_admin(update)
    text = (
        "📊 <b>Polymarket Bot</b>\n\n"
        "<b>Для всех:</b>\n"
        "/connect — Подключить Polymarket API\n"
        "/disconnect — Отключить аккаунт\n"
        "/portfolio — Мой портфель и P&L\n"
        "/trade &lt;market_id&gt; &lt;YES/NO&gt; &lt;$сумма&gt; — Ставка\n"
        "/close &lt;trade_id&gt; — Закрыть позицию\n"
        "/markets — Активные рынки\n"
    )
    if is_admin:
        text += (
            "\n<b>Админ:</b>\n"
            "/status — Статус бота\n"
            "/signals — Последние сигналы\n"
            "/scan — Принудительное сканирование\n"
            "/pause — Пауза\n"
            "/resume — Возобновить\n"
        )
    await update.message.reply_text(text, parse_mode="HTML")


# ── /connect — пошаговый ввод API ключей ─────────────────────

async def connect_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало подключения — запрос API Key"""
    if not _is_private(update):
        return ConversationHandler.END

    user = update.effective_user
    await db.save_user(user.id, user.username or "")

    text = (
        "🔑 <b>Подключение Polymarket</b>\n\n"
        "Тебе нужны L2 API ключи от Polymarket.\n"
        "Их можно получить на polymarket.com → Settings → API Keys.\n\n"
        "Шаг 1/3: Отправь <b>API Key</b>:"
    )
    await update.message.reply_text(text, parse_mode="HTML")
    return WAITING_API_KEY


async def connect_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получен API Key, запрос Secret"""
    context.user_data["pm_api_key"] = update.message.text.strip()
    # Удаляем сообщение с ключом для безопасности
    try:
        await update.message.delete()
    except Exception:
        pass
    await update.message.reply_text(
        "✅ API Key получен.\n\nШаг 2/3: Отправь <b>API Secret</b>:",
        parse_mode="HTML",
    )
    return WAITING_API_SECRET


async def connect_api_secret(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получен Secret, запрос Passphrase"""
    context.user_data["pm_api_secret"] = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    await update.message.reply_text(
        "✅ API Secret получен.\n\nШаг 3/3: Отправь <b>API Passphrase</b>:",
        parse_mode="HTML",
    )
    return WAITING_API_PASSPHRASE


async def connect_passphrase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получен Passphrase — проверка и сохранение"""
    passphrase = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    api_key = context.user_data.pop("pm_api_key", "")
    api_secret = context.user_data.pop("pm_api_secret", "")
    telegram_id = update.effective_user.id

    # Проверяем ключи — пробуем создать клиент
    if _client:
        user_clob = await _client.get_user_client(telegram_id, api_key, api_secret, passphrase)
        if not user_clob:
            await update.message.reply_text(
                "❌ Не удалось подключиться. Проверь ключи и попробуй снова: /connect"
            )
            return ConversationHandler.END

    # Сохраняем в БД
    await db.save_user_api_keys(telegram_id, api_key, api_secret, passphrase)

    await update.message.reply_text(
        "✅ <b>Polymarket подключён!</b>\n\n"
        "Теперь ты можешь торговать:\n"
        "/trade &lt;market_id&gt; &lt;YES/NO&gt; &lt;$сумма&gt;\n"
        "/portfolio — твой портфель",
        parse_mode="HTML",
    )
    logger.info(f"Юзер {telegram_id} подключил Polymarket")
    return ConversationHandler.END


async def connect_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена подключения"""
    context.user_data.pop("pm_api_key", None)
    context.user_data.pop("pm_api_secret", None)
    await update.message.reply_text("❌ Подключение отменено.")
    return ConversationHandler.END


async def cmd_disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отключить Polymarket"""
    if not _is_private(update):
        return

    telegram_id = update.effective_user.id
    await db.delete_user_api_keys(telegram_id)
    if _client:
        _client.remove_user_client(telegram_id)

    await update.message.reply_text("✅ Polymarket отключён. Ключи удалены.")


# ── /portfolio — портфель юзера ──────────────────────────────

async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Портфель текущего юзера"""
    if not _is_private(update):
        return

    user = await db.get_user_by_telegram_id(update.effective_user.id)
    if not user:
        await update.message.reply_text("Сначала нажми /start")
        return

    if not user.get("api_key"):
        await update.message.reply_text(
            "💼 Polymarket не подключён.\n"
            "Подключи через /connect чтобы видеть портфель."
        )
        return

    stats = await db.get_user_portfolio_stats(user["id"])
    open_trades = await db.get_open_trades(user_id=user["id"])

    pnl_emoji = "🟢" if stats["realized_pnl"] >= 0 else "🔴"

    text = (
        f"💼 <b>Мой портфель</b>\n\n"
        f"📂 Открытых: {stats['open_positions']} (${stats['total_invested']:.2f})\n"
        f"{pnl_emoji} P&L: <b>${stats['realized_pnl']:+.2f}</b>\n"
        f"📊 Win rate: {stats['win_rate']:.0f}% ({stats['wins']}W / {stats['losses']}L)\n"
    )

    if open_trades:
        text += "\n<b>Позиции:</b>\n"
        for t in open_trades[:10]:
            q = t["question"][:40] + ("..." if len(t["question"]) > 40 else "")
            text += f"  #{t['id']} {t['side']} ${t['size_usdc']:.2f} @ {t['price']:.2f} — {q}\n"

    await update.message.reply_text(text, parse_mode="HTML")


# ── /trade — ставка юзера ────────────────────────────────────

async def cmd_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ставка: /trade <market_id> <YES/NO> <amount>"""
    if not _is_private(update):
        return

    user = await db.get_user_by_telegram_id(update.effective_user.id)
    if not user or not user.get("api_key"):
        await update.message.reply_text("❌ Сначала подключи Polymarket: /connect")
        return

    args = context.args
    if not args or len(args) < 3:
        await update.message.reply_text(
            "Использование: /trade &lt;market_id&gt; &lt;YES/NO&gt; &lt;$сумма&gt;\n\n"
            "Пример: /trade 42 YES 5",
            parse_mode="HTML",
        )
        return

    try:
        market_id = int(args[0])
        side = args[1].upper()
        amount = float(args[2])
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Неверные аргументы")
        return

    if side not in ("YES", "NO"):
        await update.message.reply_text("❌ Укажи YES или NO")
        return

    if amount <= 0 or amount > config.MAX_BET_SIZE_USDC:
        await update.message.reply_text(f"❌ Сумма от $0.01 до ${config.MAX_BET_SIZE_USDC}")
        return

    market = await db.get_market_by_id(market_id)
    if not market:
        await update.message.reply_text(f"❌ Рынок #{market_id} не найден. Смотри /markets")
        return

    # Получаем CLOB клиент юзера
    user_clob = await _client.get_user_client(
        update.effective_user.id,
        user["api_key"], user["api_secret"], user["api_passphrase"],
    )
    if not user_clob:
        await update.message.reply_text("❌ Ошибка подключения к Polymarket. Переподключись: /connect")
        return

    # Определяем токен
    token_id = market["token_id_yes"] if side == "YES" else market["token_id_no"]
    if not token_id:
        await update.message.reply_text("❌ Нет данных о токене для этого рынка")
        return

    # Получаем цену
    price = await _client.get_midpoint(token_id)
    if not price or price <= 0 or price >= 1:
        await update.message.reply_text("❌ Не удалось получить цену")
        return

    shares = amount / price

    await update.message.reply_text(
        f"📤 Размещаю: <b>{side}</b> ${amount:.2f} @ {price:.4f}\n"
        f"📋 {market['question'][:60]}",
        parse_mode="HTML",
    )

    result = await user_clob.place_order(token_id, "BUY", shares, price)
    if not result:
        await update.message.reply_text("❌ Ордер не размещён. Проверь баланс USDC на Polymarket.")
        return

    order_id = result.get("orderID", result.get("id", ""))
    trade_id = await db.save_trade(
        user_id=user["id"],
        signal_id=None,
        market_id=market_id,
        token_id=token_id,
        side="BUY",
        size_usdc=amount,
        price=price,
        order_id=order_id,
        status="filled",
    )

    await update.message.reply_text(f"✅ Ставка размещена! (trade #{trade_id})")


# ── /close — закрыть позицию ─────────────────────────────────

async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Закрыть позицию: /close <trade_id>"""
    if not _is_private(update):
        return

    user = await db.get_user_by_telegram_id(update.effective_user.id)
    if not user or not user.get("api_key"):
        await update.message.reply_text("❌ Сначала подключи Polymarket: /connect")
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

    # Проверяем что это сделка этого юзера
    open_trades = await db.get_open_trades(user_id=user["id"])
    trade = next((t for t in open_trades if t["id"] == trade_id), None)
    if not trade:
        await update.message.reply_text(f"❌ Позиция #{trade_id} не найдена")
        return

    user_clob = await _client.get_user_client(
        update.effective_user.id,
        user["api_key"], user["api_secret"], user["api_passphrase"],
    )
    if not user_clob:
        await update.message.reply_text("❌ Ошибка подключения")
        return

    current_price = await _client.get_midpoint(trade["token_id"])
    if not current_price:
        await update.message.reply_text("❌ Не удалось получить цену")
        return

    shares = trade["size_usdc"] / trade["price"]
    result = await user_clob.place_order(trade["token_id"], "SELL", shares, current_price)

    if not result:
        await update.message.reply_text("❌ Не удалось закрыть позицию")
        return

    pnl = (current_price - trade["price"]) * shares
    await db.update_trade_status(trade_id, status="closed", pnl=pnl)

    pnl_emoji = "🟢" if pnl >= 0 else "🔴"
    await update.message.reply_text(
        f"✅ Позиция #{trade_id} закрыта\n"
        f"{pnl_emoji} P&L: ${pnl:+.2f}"
    )


# ── /markets — список рынков (для всех) ──────────────────────

async def cmd_markets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_private(update):
        return

    markets = await db.get_active_markets()
    if not markets:
        await update.message.reply_text("Нет отслеживаемых рынков. Подожди первого сканирования.")
        return

    lines = ["📊 <b>Активные рынки</b>\n"]
    for i, m in enumerate(markets[:20], 1):
        latest = await db.get_latest_price(m["id"])
        price = f"{latest['price_yes'] * 100:.0f}%" if latest else "N/A"
        cat = f"[{m['category']}]" if m.get("category") else ""
        q = m["question"][:55] + ("..." if len(m["question"]) > 55 else "")
        lines.append(f"<b>#{m['id']}</b> {cat} {q}\n   YES: {price}")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n..."

    await update.message.reply_text(text, parse_mode="HTML")


# ── Админские команды ────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return

    markets = await db.get_active_markets()
    portfolio = await db.get_portfolio_stats()
    users = await db.get_connected_users()

    next_runs = _scheduler.get_next_run_times() if _scheduler else {}
    next_scan = next_runs.get("market_scan", "N/A")
    paused = "⏸ PAUSED" if (_scheduler and _scheduler.is_paused) else "▶️ Active"

    text = (
        f"📊 <b>Bot Status</b>\n\n"
        f"Status: {paused}\n"
        f"👥 Users connected: {len(users)}\n"
        f"📡 Markets: {len(markets)}\n"
        f"📂 Open trades (all): {portfolio['open_positions']}\n"
        f"💰 Total P&L: ${portfolio['realized_pnl']:+.2f}\n"
        f"⏰ Next scan: {next_scan}\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return

    signals = await db.get_recent_signals(10)
    if not signals:
        await update.message.reply_text("Нет сигналов")
        return

    lines = ["📋 <b>Recent Signals</b>\n"]
    for s in signals:
        q = s["question"][:50] + ("..." if len(s["question"]) > 50 else "")
        change = s.get("probability_change", 0) * 100
        emoji = "🟢" if s["direction"] == "BUY" else "🔴"
        lines.append(
            f"{emoji} {s['direction']} | {q}\n"
            f"   Conf: {s['confidence']:.2f} | {change:+.1f}% | {s['signal_type']}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    await update.message.reply_text("🔍 Сканирование...")
    if _scanner:
        count = await _scanner.scan_markets()
        await _scanner.update_prices()
        await update.message.reply_text(f"✅ Готово: {count} рынков")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if _scheduler:
        _scheduler.is_paused = True
        await update.message.reply_text("⏸ Бот на паузе")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if _scheduler:
        _scheduler.is_paused = False
        await update.message.reply_text("▶️ Бот возобновлён")


# ── ConversationHandler для /connect ─────────────────────────

def get_connect_handler() -> ConversationHandler:
    """Возвращает ConversationHandler для пошагового подключения API"""
    return ConversationHandler(
        entry_points=[CommandHandler("connect", connect_start)],
        states={
            WAITING_API_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, connect_api_key)],
            WAITING_API_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, connect_api_secret)],
            WAITING_API_PASSPHRASE: [MessageHandler(filters.TEXT & ~filters.COMMAND, connect_passphrase)],
        },
        fallbacks=[CommandHandler("cancel", connect_cancel)],
    )
