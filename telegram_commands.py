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
WAITING_CONNECT_METHOD, WAITING_PRIVATE_KEY, WAITING_API_KEY, WAITING_API_SECRET, WAITING_API_PASSPHRASE = range(5)


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


async def _get_clob_for_user(telegram_id: int):
    """Получить CLOB клиент для юзера (по private_key или API keys)"""
    user = await db.get_user_by_telegram_id(telegram_id)
    if not user or not _client:
        return None
    if user.get("private_key"):
        return await _client.get_user_client(telegram_id, private_key=user["private_key"])
    elif user.get("api_key"):
        return await _client.get_user_client(
            telegram_id, api_key=user["api_key"],
            api_secret=user["api_secret"], api_passphrase=user["api_passphrase"],
        )
    return None


# ── Юзерские команды (личка) ─────────────────────────────────

STRATEGIES = {
    "contrarian": {"name": "Контрарианская", "desc": "Покупка на просадках. Ищет панику и overreaction."},
    "momentum": {"name": "Моментум", "desc": "Покупка растущих. Следует за трендом."},
    "conservative": {"name": "Консервативная", "desc": "Только сильные сигналы с уверенностью 80%+."},
}


async def _build_main_menu(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Собрать главное меню с текущим статусом"""
    db_user = await db.get_user_by_telegram_id(user_id)
    is_connected = bool(db_user and (db_user.get("api_key") or db_user.get("private_key")))

    if not is_connected:
        text = (
            "📊 <b>Polymarket Bot</b>\n\n"
            "Подключи аккаунт чтобы начать торговлю.\n"
            "Сигналы публикуются в канале автоматически."
        )
        buttons = [
            [InlineKeyboardButton("🔗 Подключить Polymarket", callback_data="menu_connect")],
            [InlineKeyboardButton("❓ Как это работает", callback_data="menu_help")],
        ]
        return text, InlineKeyboardMarkup(buttons)

    # Подключён — показываем статус
    enabled = db_user.get("auto_trade", 0)
    amount = db_user.get("auto_amount", 0.5)
    max_daily = db_user.get("auto_max_daily", 5.0)
    strategy = db_user.get("strategy", "contrarian")
    strategy_name = STRATEGIES.get(strategy, {}).get("name", strategy)

    stats = await db.get_user_portfolio_stats(db_user["id"])
    open_pos = stats["open_positions"]
    pnl = stats["realized_pnl"]
    pnl_emoji = "🟢" if pnl >= 0 else "🔴"
    wr = stats["win_rate"]

    status_icon = "🟢" if enabled else "⏸"
    status_text = "Активен" if enabled else "На паузе"

    text = (
        f"📊 <b>Polymarket Bot</b>\n\n"
        f"{status_icon} Автоторговля: <b>{status_text}</b>\n"
        f"📋 Стратегия: <b>{strategy_name}</b>\n"
        f"💰 Ставка: <b>${amount:.2f}</b> | Лимит: <b>${max_daily:.0f}/день</b>\n\n"
        f"📂 Позиций: <b>{open_pos}</b>\n"
        f"{pnl_emoji} P&L: <b>${pnl:+.2f}</b> | Win rate: <b>{wr:.0f}%</b>"
    )

    toggle_text = "⏸ Остановить" if enabled else "▶️ Запустить"

    buttons = [
        [InlineKeyboardButton(toggle_text, callback_data="autotrade_toggle")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings"),
         InlineKeyboardButton("📂 Позиции", callback_data="menu_positions")],
        [InlineKeyboardButton("🔌 Отключить аккаунт", callback_data="menu_disconnect")],
    ]

    return text, InlineKeyboardMarkup(buttons)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Приветствие и регистрация"""
    if not _is_private(update):
        return

    user = update.effective_user

    # Проверка доступа
    allowed = await db.is_user_allowed(user.id)
    if not allowed:
        await update.message.reply_text(
            "🔒 Доступ закрыт.\n\nОбратись к администратору для получения доступа.\n"
            f"Твой ID: <code>{user.id}</code>",
            parse_mode="HTML",
        )
        return

    await db.save_user(user.id, user.username or "")

    text, keyboard = await _build_main_menu(user.id)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий inline-кнопок"""
    query = update.callback_query
    await query.answer()

    data = query.data
    user = query.from_user

    try:
        if data == "menu_back":
            text, kb = await _build_main_menu(user.id)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        elif data == "menu_settings":
            await _show_settings(query)
        elif data == "menu_positions":
            await _show_positions(query)
        elif data == "menu_help":
            await _show_help(query)
        elif data == "menu_connect":
            await query.edit_message_text(
                "🔑 Отправь команду /connect для подключения",
                parse_mode="HTML",
            )
        elif data == "menu_disconnect":
            await db.delete_user_api_keys(user.id)
            if _client:
                _client.remove_user_client(user.id)
            text, kb = await _build_main_menu(user.id)
            await query.edit_message_text("✅ Аккаунт отключён.\n\n" + text, parse_mode="HTML", reply_markup=kb)
        elif data == "autotrade_toggle":
            await _toggle_autotrade(query)
        elif data == "set_amount_custom":
            await query.edit_message_text(
                "✏️ Отправь сумму ставки в долларах (например: <b>3.5</b>):",
                parse_mode="HTML",
            )
            context.user_data["waiting_for"] = "custom_amount"
        elif data == "set_daily_custom":
            await query.edit_message_text(
                "✏️ Отправь дневной лимит в долларах (например: <b>15</b>):",
                parse_mode="HTML",
            )
            context.user_data["waiting_for"] = "custom_daily"
        elif data.startswith("set_amount_"):
            amount = float(data.split("_")[2])
            await db.set_auto_trade_settings(query.from_user.id, amount=amount)
            await _show_settings(query)
        elif data.startswith("set_daily_"):
            daily = float(data.split("_")[2])
            await db.set_auto_trade_settings(query.from_user.id, max_daily=daily)
            await _show_settings(query)
        elif data.startswith("set_strategy_"):
            strategy = data.split("_")[2]
            await db.set_user_strategy(query.from_user.id, strategy)
            await _show_settings(query)
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
        elif data.startswith("close_pos_"):
            trade_id = int(data.split("_")[2])
            await _close_position(query, trade_id)
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
    if not user or not (user.get("api_key") or user.get("private_key")):
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

    user_clob = await _get_clob_for_user(query.from_user.id)
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
    if not user or not (user.get("api_key") or user.get("private_key")):
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
    if not user or not (user.get("api_key") or user.get("private_key")):
        await query.edit_message_text("❌ Polymarket не подключён.", reply_markup=_back_button())
        return

    open_trades = await db.get_open_trades(user_id=user["id"])
    trade = next((t for t in open_trades if t["id"] == trade_id), None)
    if not trade:
        await query.edit_message_text(f"❌ Позиция #{trade_id} не найдена.", reply_markup=_back_button())
        return

    user_clob = await _get_clob_for_user(query.from_user.id)
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


async def _show_settings(query):
    """Настройки: стратегия, сумма, лимит"""
    user = await db.get_user_by_telegram_id(query.from_user.id)
    if not user or not (user.get("api_key") or user.get("private_key")):
        await query.edit_message_text("❌ Сначала подключи Polymarket.", reply_markup=_back_button())
        return

    amount = user.get("auto_amount", 0.5)
    max_daily = user.get("auto_max_daily", 5.0)
    strategy = user.get("strategy", "contrarian")
    strategy_info = STRATEGIES.get(strategy, {})

    text = (
        f"⚙️ <b>Настройки</b>\n\n"
        f"<b>Стратегия:</b> {strategy_info.get('name', strategy)}\n"
        f"<i>{strategy_info.get('desc', '')}</i>\n\n"
        f"<b>Сумма ставки:</b> ${amount:.2f}\n"
        f"<b>Лимит в день:</b> ${max_daily:.0f}"
    )

    # Кнопки стратегий
    strat_buttons = []
    for key, info in STRATEGIES.items():
        mark = " ✓" if key == strategy else ""
        strat_buttons.append(InlineKeyboardButton(
            f"{info['name']}{mark}", callback_data=f"set_strategy_{key}"
        ))

    buttons = [
        strat_buttons,
        [InlineKeyboardButton("$1", callback_data="set_amount_1"),
         InlineKeyboardButton("$2", callback_data="set_amount_2"),
         InlineKeyboardButton("$5", callback_data="set_amount_5"),
         InlineKeyboardButton("$10", callback_data="set_amount_10"),
         InlineKeyboardButton("✏️", callback_data="set_amount_custom")],
        [InlineKeyboardButton("Лимит $5", callback_data="set_daily_5"),
         InlineKeyboardButton("$10", callback_data="set_daily_10"),
         InlineKeyboardButton("$25", callback_data="set_daily_25"),
         InlineKeyboardButton("$50", callback_data="set_daily_50"),
         InlineKeyboardButton("✏️", callback_data="set_daily_custom")],
        [InlineKeyboardButton("◀️ Назад", callback_data="menu_back")],
    ]

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def _toggle_autotrade(query):
    user = await db.get_user_by_telegram_id(query.from_user.id)
    if not user:
        return
    new_state = not bool(user.get("auto_trade", 0))
    await db.set_auto_trade(query.from_user.id, new_state)
    text, kb = await _build_main_menu(query.from_user.id)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстового ввода (кастомные суммы)"""
    if not _is_private(update):
        return

    waiting = context.user_data.pop("waiting_for", None)
    if not waiting:
        return

    text = update.message.text.strip()

    try:
        value = float(text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("❌ Введи число. Попробуй /start")
        return

    if waiting == "custom_amount":
        if value < 1:
            await update.message.reply_text("❌ Минимум $1 (требование Polymarket)")
            return
        await db.set_auto_trade_settings(update.effective_user.id, amount=value)
        await update.message.reply_text(f"✅ Ставка: <b>${value:.2f}</b>\n\nНажми /start", parse_mode="HTML")
    elif waiting == "custom_daily":
        if value < 1:
            await update.message.reply_text("❌ Минимум $1")
            return
        await db.set_auto_trade_settings(update.effective_user.id, max_daily=value)
        await update.message.reply_text(f"✅ Лимит: <b>${value:.0f}/день</b>\n\nНажми /start", parse_mode="HTML")


async def _show_help(query):
    """Справка"""
    text = (
        "❓ <b>Как это работает</b>\n\n"
        "1️⃣ Подключи Polymarket аккаунт через /connect\n"
        "2️⃣ Выбери стратегию и сумму ставки\n"
        "3️⃣ Нажми ▶️ Запустить\n\n"
        "Бот каждый час анализирует рынки и автоматически "
        "размещает ставки по выбранной стратегии.\n\n"
        "<b>Стратегии:</b>\n"
        "• <b>Контрарианская</b> — покупка на просадках\n"
        "• <b>Моментум</b> — покупка растущих\n"
        "• <b>Консервативная</b> — только сильные сигналы\n\n"
        "<b>Управление рисками:</b>\n"
        "• Тейк-профит +15%\n"
        "• Трейлинг-стоп при росте +25%\n"
        "• Стоп-лосс -20%"
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
    """Начало подключения — выбор метода"""
    if not _is_private(update):
        return ConversationHandler.END

    user = update.effective_user
    await db.save_user(user.id, user.username or "")

    text = (
        "🔑 <b>Подключение Polymarket</b>\n\n"
        "Выбери способ подключения:\n\n"
        "1️⃣ Отправь <b>приватный ключ</b> кошелька (начинается с 0x)\n"
        "   <i>Проще всего — бот сам настроит API</i>\n\n"
        "2️⃣ Отправь <b>API Key</b> (если уже есть API ключи)\n\n"
        "Или /cancel для отмены"
    )
    await update.message.reply_text(text, parse_mode="HTML")
    return WAITING_CONNECT_METHOD


async def connect_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Определяем метод по содержимому — приватный ключ или API key"""
    text = update.message.text.strip()

    try:
        await update.message.delete()
    except Exception:
        pass

    # Если начинается с 0x и длинный — это приватный ключ
    if text.startswith("0x") and len(text) >= 60:
        telegram_id = update.effective_user.id

        await update.message.reply_text("⏳ Проверяю ключ...")

        if _client:
            user_clob = await _client.get_user_client(telegram_id, private_key=text)
            if not user_clob:
                await update.message.reply_text(
                    "❌ Неверный ключ. Проверь и попробуй снова: /connect"
                )
                return ConversationHandler.END

        await db.save_user_private_key(telegram_id, text)

        await update.message.reply_text(
            "✅ <b>Polymarket подключён!</b>\n\nНажми /start для настройки.",
            parse_mode="HTML",
        )
        logger.info(f"Юзер {telegram_id} подключил Polymarket (private key)")
        return ConversationHandler.END
    else:
        # Это API Key — переходим к вводу Secret
        context.user_data["pm_api_key"] = text
        await update.message.reply_text(
            "✅ API Key получен.\n\nОтправь <b>API Secret</b>:",
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
        "✅ API Secret получен.\n\nОтправь <b>API Passphrase</b>:",
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

    if _client:
        user_clob = await _client.get_user_client(
            telegram_id, api_key=api_key, api_secret=api_secret, api_passphrase=passphrase
        )
        if not user_clob:
            await update.message.reply_text(
                "❌ Не удалось подключиться. Проверь ключи: /connect"
            )
            return ConversationHandler.END

    await db.save_user_api_keys(telegram_id, api_key, api_secret, passphrase)

    await update.message.reply_text(
        "✅ <b>Polymarket подключён!</b>\n\nНажми /start для настройки.",
        parse_mode="HTML",
    )
    logger.info(f"Юзер {telegram_id} подключил Polymarket (API keys)")
    return ConversationHandler.END


async def connect_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("pm_api_key", None)
    context.user_data.pop("pm_api_secret", None)
    await update.message.reply_text("❌ Отменено.")
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

    if not (user.get("api_key") or user.get("private_key")):
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
    if not user or not (user.get("api_key") or user.get("private_key")):
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
    user_clob = await _get_clob_for_user(update.effective_user.id)
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
    if not user or not (user.get("api_key") or user.get("private_key")):
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

    user_clob = await _get_clob_for_user(update.effective_user.id)
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
    paused = "⏸ ПАУЗА" if (_scheduler and _scheduler.is_paused) else "▶️ Активен"

    text = (
        f"📊 <b>Статус бота</b>\n\n"
        f"Статус: {paused}\n"
        f"👥 Подключённых юзеров: {len(users)}\n"
        f"📡 Рынков: {len(markets)}\n"
        f"📂 Открытых сделок (все): {portfolio['open_positions']}\n"
        f"💰 Общий P&L: ${portfolio['realized_pnl']:+.2f}\n"
        f"⏰ Следующее сканирование: {next_scan}\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return

    signals = await db.get_recent_signals(10)
    if not signals:
        await update.message.reply_text("Нет сигналов.")
        return

    lines = ["📋 <b>Последние сигналы</b>\n"]
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


# ── Управление доступом (админ) ──────────────────────────────

async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/adduser <telegram_id> — дать доступ юзеру"""
    if not _is_admin(update):
        return
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /adduser <telegram_id>")
        return
    try:
        tid = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный ID")
        return
    await db.add_allowed_user(tid)
    await update.message.reply_text(f"✅ Юзер <code>{tid}</code> добавлен", parse_mode="HTML")


async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/removeuser <telegram_id> — убрать доступ"""
    if not _is_admin(update):
        return
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /removeuser <telegram_id>")
        return
    try:
        tid = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный ID")
        return
    await db.remove_allowed_user(tid)
    await update.message.reply_text(f"✅ Юзер <code>{tid}</code> удалён", parse_mode="HTML")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/users — список допущенных"""
    if not _is_admin(update):
        return
    allowed = await db.get_allowed_users()
    connected = await db.get_connected_users()
    connected_ids = {u["telegram_id"] for u in connected}

    if not allowed:
        await update.message.reply_text("Список пуст. Добавь: /adduser <id>")
        return

    lines = ["👥 <b>Допущенные юзеры</b>\n"]
    for tid in allowed:
        status = "🟢 подключён" if tid in connected_ids else "⚪ не подключён"
        lines.append(f"<code>{tid}</code> — {status}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ── ConversationHandler для /connect ─────────────────────────

def get_connect_handler() -> ConversationHandler:
    """ConversationHandler для подключения (приватный ключ или API ключи)"""
    return ConversationHandler(
        entry_points=[CommandHandler("connect", connect_start)],
        states={
            WAITING_CONNECT_METHOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, connect_method)],
            WAITING_API_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, connect_api_secret)],
            WAITING_API_PASSPHRASE: [MessageHandler(filters.TEXT & ~filters.COMMAND, connect_passphrase)],
        },
        fallbacks=[CommandHandler("cancel", connect_cancel)],
    )
