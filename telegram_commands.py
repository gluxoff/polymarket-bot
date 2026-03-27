"""Команды Telegram-бота — юзерские + админские"""

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, CallbackQueryHandler, filters
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

def _main_menu_keyboard(is_connected: bool = False) -> InlineKeyboardMarkup:
    """Главное меню с кнопками"""
    buttons = [
        [InlineKeyboardButton("📊 Рынки", callback_data="menu_markets"),
         InlineKeyboardButton("📋 Сигналы", callback_data="menu_signals")],
    ]
    if is_connected:
        buttons.append([
            InlineKeyboardButton("💼 Портфель", callback_data="menu_portfolio"),
            InlineKeyboardButton("📂 Позиции", callback_data="menu_positions"),
        ])
        buttons.append([InlineKeyboardButton("🤖 Автоставки", callback_data="menu_autotrade")])
        buttons.append([InlineKeyboardButton("🔌 Отключить", callback_data="menu_disconnect")])
    else:
        buttons.append([InlineKeyboardButton("🔗 Подключить Polymarket", callback_data="menu_connect")])
    buttons.append([InlineKeyboardButton("❓ Помощь", callback_data="menu_help")])
    return InlineKeyboardMarkup(buttons)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветствие и регистрация"""
    if not _is_private(update):
        return

    user = update.effective_user
    await db.save_user(user.id, user.username or "")

    db_user = await db.get_user_by_telegram_id(user.id)
    is_connected = bool(db_user and db_user.get("api_key"))

    text = (
        f"👋 Привет, {user.first_name}!\n\n"
        "Я бот для прогнозов на <b>Polymarket</b>.\n\n"
        "📊 Сигналы публикуются в канале автоматически.\n"
        "💰 Чтобы торговать — подключи свой Polymarket."
    )
    keyboard = _main_menu_keyboard(is_connected)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий inline-кнопок"""
    query = update.callback_query
    await query.answer()

    data = query.data
    user = query.from_user

    try:
        if data == "menu_markets" or data.startswith("markets_page_"):
            page = int(data.split("_")[-1]) if data.startswith("markets_page_") else 0
            await _show_markets(query, page)
        elif data == "menu_portfolio":
            await _show_portfolio(query)
        elif data == "menu_signals":
            await _show_signals(query)
        elif data == "menu_help":
            await _show_help(query)
        elif data == "menu_connect":
            await query.edit_message_text(
                "🔑 Для подключения отправь команду /connect",
                parse_mode="HTML",
            )
        elif data == "menu_disconnect":
            await db.delete_user_api_keys(user.id)
            if _client:
                _client.remove_user_client(user.id)
            await query.edit_message_text(
                "✅ Polymarket отключён.\n\nНажми /start для меню.",
            )
        elif data == "menu_autotrade":
            await _show_autotrade(query)
        elif data == "autotrade_toggle":
            await _toggle_autotrade(query)
        elif data.startswith("autotrade_amount_"):
            amount = float(data.split("_")[2])
            await _set_autotrade_amount(query, amount)
        elif data.startswith("autotrade_daily_"):
            daily = float(data.split("_")[2])
            await _set_autotrade_daily(query, daily)
        elif data.startswith("autotrade_conf_"):
            conf = float(data.split("_")[2]) / 100
            await _set_autotrade_confidence(query, conf)
        elif data.startswith("market_"):
            market_id = int(data.split("_")[1])
            await _show_market_detail(query, market_id)
        elif data.startswith("buy_yes_") or data.startswith("buy_no_"):
            parts = data.split("_")
            side = "YES" if parts[1] == "yes" else "NO"
            market_id = int(parts[2])
            await _show_trade_confirm(query, market_id, side)
        elif data.startswith("confirm_trade_"):
            parts = data.split("_")
            side = parts[2].upper()
            market_id = int(parts[3])
            amount = float(parts[4])
            await _execute_trade(query, market_id, side, amount)
        elif data == "menu_positions":
            await _show_positions(query)
        elif data.startswith("close_pos_"):
            trade_id = int(data.split("_")[2])
            await _close_position(query, trade_id)
        elif data == "menu_back":
            db_user = await db.get_user_by_telegram_id(user.id)
            is_connected = bool(db_user and db_user.get("api_key"))
            await query.edit_message_text(
                f"👋 {user.first_name}, выбери действие:",
                parse_mode="HTML",
                reply_markup=_main_menu_keyboard(is_connected),
            )
    except Exception as e:
        logger.error(f"Ошибка callback {data}: {e}")
        await query.edit_message_text(f"❌ Ошибка: {e}\n\nНажми /start")


def _back_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="menu_back")]])


MARKETS_PER_PAGE = 10


async def _show_markets(query, page: int = 0):
    """Показать рынки с кнопками и пагинацией"""
    markets = await db.get_active_markets()
    if not markets:
        await query.edit_message_text("Нет рынков.", reply_markup=_back_button())
        return

    total = len(markets)
    start = page * MARKETS_PER_PAGE
    end = start + MARKETS_PER_PAGE
    page_markets = markets[start:end]

    lines = [f"📊 <b>Рынки</b> ({start+1}-{min(end, total)} из {total})\n"]
    for m in page_markets:
        latest = await db.get_latest_price(m["id"])
        price = f"{latest['price_yes'] * 100:.0f}%" if latest and latest["price_yes"] else "—"
        cat = f"[{m['category']}]" if m.get("category") else ""
        q = m["question"][:50] + ("..." if len(m["question"]) > 50 else "")
        lines.append(f"<b>#{m['id']}</b> {cat} {q}\n   YES: {price}")

    text = "\n".join(lines)

    # Кнопки рынков (для деталей)
    buttons = []
    row = []
    for m in page_markets:
        row.append(InlineKeyboardButton(f"#{m['id']}", callback_data=f"market_{m['id']}"))
        if len(row) == 5:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    # Пагинация
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Пред", callback_data=f"markets_page_{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton("След ▶️", callback_data=f"markets_page_{page+1}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("◀️ Меню", callback_data="menu_back")])

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def _show_market_detail(query, market_id: int):
    """Детали рынка с кнопками Buy YES / Buy NO"""
    market = await db.get_market_by_id(market_id)
    if not market:
        await query.edit_message_text("Рынок не найден.", reply_markup=_back_button())
        return

    latest = await db.get_latest_price(market_id)
    price_yes = latest["price_yes"] if latest and latest["price_yes"] else None
    price_no = latest["price_no"] if latest and latest["price_no"] else None

    price_yes_str = f"{price_yes * 100:.1f}%" if price_yes else "N/A"
    price_no_str = f"{price_no * 100:.1f}%" if price_no else "N/A"

    cat = f"#{market['category']}" if market.get("category") else ""
    url = market.get("polymarket_url", "")

    text = (
        f"📊 <b>Рынок #{market_id}</b>\n\n"
        f"❓ {market['question']}\n\n"
        f"✅ YES: <b>{price_yes_str}</b>\n"
        f"❌ NO: <b>{price_no_str}</b>\n"
    )
    if cat:
        text += f"\n🏷 {cat}"
    if url:
        text += f'\n🔗 <a href="{url}">Polymarket</a>'

    buttons = [
        [InlineKeyboardButton(f"🟢 Buy YES ({price_yes_str})", callback_data=f"buy_yes_{market_id}"),
         InlineKeyboardButton(f"🔴 Buy NO ({price_no_str})", callback_data=f"buy_no_{market_id}")],
        [InlineKeyboardButton("◀️ К рынкам", callback_data="menu_markets")],
    ]

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def _show_trade_confirm(query, market_id: int, side: str):
    """Выбор суммы ставки"""
    market = await db.get_market_by_id(market_id)
    if not market:
        return

    user = await db.get_user_by_telegram_id(query.from_user.id)
    if not user or not user.get("api_key"):
        await query.edit_message_text(
            "❌ Сначала подключи Polymarket через /connect",
            reply_markup=_back_button(),
        )
        return

    q = market["question"][:60]
    text = (
        f"💰 <b>Ставка: {side}</b>\n\n"
        f"❓ {q}\n\n"
        f"Выбери сумму:"
    )

    buttons = [
        [InlineKeyboardButton("$1", callback_data=f"confirm_trade_{side.lower()}_{market_id}_1"),
         InlineKeyboardButton("$2", callback_data=f"confirm_trade_{side.lower()}_{market_id}_2"),
         InlineKeyboardButton("$5", callback_data=f"confirm_trade_{side.lower()}_{market_id}_5")],
        [InlineKeyboardButton("$10", callback_data=f"confirm_trade_{side.lower()}_{market_id}_10"),
         InlineKeyboardButton("$25", callback_data=f"confirm_trade_{side.lower()}_{market_id}_25"),
         InlineKeyboardButton("$50", callback_data=f"confirm_trade_{side.lower()}_{market_id}_50")],
        [InlineKeyboardButton("◀️ Назад", callback_data=f"market_{market_id}")],
    ]

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def _execute_trade(query, market_id: int, side: str, amount: float):
    """Выполнить ставку"""
    user_data = await db.get_user_by_telegram_id(query.from_user.id)
    if not user_data or not user_data.get("api_key"):
        await query.edit_message_text("❌ Polymarket не подключён.", reply_markup=_back_button())
        return

    market = await db.get_market_by_id(market_id)
    if not market:
        await query.edit_message_text("❌ Рынок не найден.", reply_markup=_back_button())
        return

    token_id = market["token_id_yes"] if side == "YES" else market["token_id_no"]
    if not token_id:
        await query.edit_message_text("❌ Нет токена.", reply_markup=_back_button())
        return

    user_clob = await _client.get_user_client(
        query.from_user.id,
        user_data["api_key"], user_data["api_secret"], user_data["api_passphrase"],
    )
    if not user_clob:
        await query.edit_message_text("❌ Ошибка подключения. Переподключись: /connect", reply_markup=_back_button())
        return

    price = await _client.get_midpoint(token_id)
    if not price or price <= 0 or price >= 1:
        await query.edit_message_text("❌ Не удалось получить цену.", reply_markup=_back_button())
        return

    shares = amount / price

    await query.edit_message_text(f"⏳ Размещаю {side} ${amount:.0f}...")

    result = await user_clob.place_order(token_id, "BUY", shares, price)
    if not result:
        await query.edit_message_text(
            "❌ Ордер не размещён. Проверь баланс USDC.",
            reply_markup=_back_button(),
        )
        return

    order_id = result.get("orderID", result.get("id", ""))
    trade_id = await db.save_trade(
        user_id=user_data["id"],
        signal_id=None,
        market_id=market_id,
        token_id=token_id,
        side="BUY",
        size_usdc=amount,
        price=price,
        order_id=order_id,
        status="filled",
    )

    q = market["question"][:50]
    await query.edit_message_text(
        f"✅ <b>Ставка размещена!</b>\n\n"
        f"🎯 {side} ${amount:.0f} @ {price:.4f}\n"
        f"📋 {q}\n"
        f"🆔 Trade #{trade_id}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💼 Портфель", callback_data="menu_portfolio"),
             InlineKeyboardButton("◀️ Меню", callback_data="menu_back")],
        ]),
    )


async def _show_portfolio(query):
    """Показать портфель через кнопку"""
    user = await db.get_user_by_telegram_id(query.from_user.id)
    if not user or not user.get("api_key"):
        await query.edit_message_text(
            "💼 Polymarket не подключён.\nНажми /connect чтобы подключить.",
            reply_markup=_back_button(),
        )
        return

    stats = await db.get_user_portfolio_stats(user["id"])
    open_trades = await db.get_open_trades(user_id=user["id"])
    pnl_emoji = "🟢" if stats["realized_pnl"] >= 0 else "🔴"

    text = (
        f"💼 <b>Мой портфель</b>\n\n"
        f"📂 Открытых: {stats['open_positions']} (${stats['total_invested']:.2f})\n"
        f"{pnl_emoji} P&L: <b>${stats['realized_pnl']:+.2f}</b>\n"
        f"📊 Win rate: {stats['win_rate']:.0f}% ({stats['wins']}W / {stats['losses']}L)"
    )

    if open_trades:
        text += "\n\n<b>Позиции:</b>"
        for t in open_trades[:8]:
            q = t["question"][:35] + ("..." if len(t["question"]) > 35 else "")
            text += f"\n  #{t['id']} {t['side']} ${t['size_usdc']:.2f} — {q}"

    buttons = []
    if open_trades:
        buttons.append([InlineKeyboardButton("📂 Мои позиции", callback_data="menu_positions")])
    buttons.append([InlineKeyboardButton("◀️ Меню", callback_data="menu_back")])

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def _show_positions(query):
    """Показать открытые позиции с кнопками закрытия"""
    user = await db.get_user_by_telegram_id(query.from_user.id)
    if not user:
        await query.edit_message_text("Нажми /start", reply_markup=_back_button())
        return

    open_trades = await db.get_open_trades(user_id=user["id"])
    if not open_trades:
        await query.edit_message_text("📂 Нет открытых позиций.", reply_markup=_back_button())
        return

    lines = ["📂 <b>Открытые позиции</b>\n"]
    buttons = []
    for t in open_trades[:10]:
        q = t["question"][:40] + ("..." if len(t["question"]) > 40 else "")
        lines.append(f"<b>#{t['id']}</b> {t['side']} ${t['size_usdc']:.2f} @ {t['price']:.4f}\n   {q}")
        buttons.append([InlineKeyboardButton(f"❌ Закрыть #{t['id']}", callback_data=f"close_pos_{t['id']}")])

    buttons.append([InlineKeyboardButton("💼 Портфель", callback_data="menu_portfolio")])
    buttons.append([InlineKeyboardButton("◀️ Меню", callback_data="menu_back")])

    text = "\n".join(lines)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def _close_position(query, trade_id: int):
    """Закрыть позицию через кнопку"""
    user = await db.get_user_by_telegram_id(query.from_user.id)
    if not user or not user.get("api_key"):
        await query.edit_message_text("❌ Polymarket не подключён.", reply_markup=_back_button())
        return

    open_trades = await db.get_open_trades(user_id=user["id"])
    trade = next((t for t in open_trades if t["id"] == trade_id), None)
    if not trade:
        await query.edit_message_text(f"❌ Позиция #{trade_id} не найдена.", reply_markup=_back_button())
        return

    user_clob = await _client.get_user_client(
        query.from_user.id, user["api_key"], user["api_secret"], user["api_passphrase"],
    )
    if not user_clob:
        await query.edit_message_text("❌ Ошибка подключения.", reply_markup=_back_button())
        return

    await query.edit_message_text(f"⏳ Закрываю позицию #{trade_id}...")

    current_price = await _client.get_midpoint(trade["token_id"])
    if not current_price:
        await query.edit_message_text("❌ Не удалось получить цену.", reply_markup=_back_button())
        return

    shares = trade["size_usdc"] / trade["price"] if trade["price"] > 0 else 0
    result = await user_clob.place_order(trade["token_id"], "SELL", shares, current_price)

    if not result:
        await query.edit_message_text("❌ Не удалось закрыть.", reply_markup=_back_button())
        return

    pnl = (current_price - trade["price"]) * shares
    await db.update_trade_status(trade_id, status="closed", pnl=pnl)
    pnl_emoji = "🟢" if pnl >= 0 else "🔴"

    await query.edit_message_text(
        f"✅ Позиция #{trade_id} закрыта\n{pnl_emoji} P&L: ${pnl:+.2f}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💼 Портфель", callback_data="menu_portfolio"),
             InlineKeyboardButton("◀️ Меню", callback_data="menu_back")],
        ]),
    )


async def _show_signals(query):
    """Показать последние сигналы через кнопку"""
    signals = await db.get_recent_signals(10)
    if not signals:
        await query.edit_message_text("Нет сигналов пока.", reply_markup=_back_button())
        return

    lines = ["📋 <b>Последние сигналы</b>\n"]
    for s in signals:
        q = s["question"][:45] + ("..." if len(s["question"]) > 45 else "")
        change = s.get("probability_change", 0) * 100
        emoji = "🟢" if s["direction"] == "BUY" else "🔴"
        lines.append(f"{emoji} {s['direction']} | {q}\n   {change:+.1f}% | {s['signal_type']}")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n..."
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=_back_button())


async def _show_autotrade(query):
    """Настройки автоставок"""
    user = await db.get_user_by_telegram_id(query.from_user.id)
    if not user or not user.get("api_key"):
        await query.edit_message_text("❌ Сначала подключи Polymarket.", reply_markup=_back_button())
        return

    enabled = user.get("auto_trade", 0)
    amount = user.get("auto_amount", 5.0)
    max_daily = user.get("auto_max_daily", 50.0)
    min_conf = user.get("auto_min_confidence", 0.6)

    status = "🟢 ВКЛЮЧЕНЫ" if enabled else "🔴 ВЫКЛЮЧЕНЫ"

    text = (
        f"🤖 <b>Автоставки</b>\n\n"
        f"Статус: <b>{status}</b>\n"
        f"💰 Сумма за ставку: <b>${amount:.0f}</b>\n"
        f"📊 Лимит в день: <b>${max_daily:.0f}</b>\n"
        f"🎯 Мин. уверенность: <b>{min_conf*100:.0f}%</b>\n\n"
        f"Когда бот находит сигнал с уверенностью ≥ {min_conf*100:.0f}%, "
        f"он автоматически ставит ${amount:.0f} от твоего имени."
    )

    toggle_text = "🔴 Выключить" if enabled else "🟢 Включить"

    buttons = [
        [InlineKeyboardButton(toggle_text, callback_data="autotrade_toggle")],
        [InlineKeyboardButton("$1", callback_data="autotrade_amount_1"),
         InlineKeyboardButton("$2", callback_data="autotrade_amount_2"),
         InlineKeyboardButton("$5", callback_data="autotrade_amount_5"),
         InlineKeyboardButton("$10", callback_data="autotrade_amount_10")],
        [InlineKeyboardButton("Daily $25", callback_data="autotrade_daily_25"),
         InlineKeyboardButton("$50", callback_data="autotrade_daily_50"),
         InlineKeyboardButton("$100", callback_data="autotrade_daily_100")],
        [InlineKeyboardButton("Conf 60%", callback_data="autotrade_conf_60"),
         InlineKeyboardButton("70%", callback_data="autotrade_conf_70"),
         InlineKeyboardButton("80%", callback_data="autotrade_conf_80")],
        [InlineKeyboardButton("◀️ Меню", callback_data="menu_back")],
    ]

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def _toggle_autotrade(query):
    user = await db.get_user_by_telegram_id(query.from_user.id)
    if not user:
        return
    new_state = not bool(user.get("auto_trade", 0))
    await db.set_auto_trade(query.from_user.id, new_state)
    await _show_autotrade(query)


async def _set_autotrade_amount(query, amount: float):
    await db.set_auto_trade_settings(query.from_user.id, amount=amount)
    await _show_autotrade(query)


async def _set_autotrade_daily(query, daily: float):
    await db.set_auto_trade_settings(query.from_user.id, max_daily=daily)
    await _show_autotrade(query)


async def _set_autotrade_confidence(query, conf: float):
    await db.set_auto_trade_settings(query.from_user.id, min_confidence=conf)
    await _show_autotrade(query)


async def _show_help(query):
    """Справка через кнопку"""
    text = (
        "📊 <b>Polymarket Bot</b>\n\n"
        "<b>Команды:</b>\n"
        "/connect — Подключить Polymarket API\n"
        "/disconnect — Отключить аккаунт\n"
        "/trade &lt;id&gt; &lt;YES/NO&gt; &lt;$&gt; — Ставка\n"
        "/close &lt;trade_id&gt; — Закрыть позицию\n\n"
        "Кнопки меню доступны через /start"
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=_back_button())


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
