"""Точка входа — запуск Polymarket бота"""

import asyncio
import sys

from loguru import logger
from telegram.ext import Application, CommandHandler, CallbackQueryHandler

import config
import db
import telegram_commands
from polymarket_client import PolymarketClient
from market_scanner import MarketScanner
from analytics_engine import AnalyticsEngine
from signal_generator import SignalGenerator
from chart_generator import ChartGenerator
from risk_manager import RiskManager
from auto_trader import AutoTrader
from portfolio_tracker import PortfolioTracker
from telegram_publisher import TelegramPublisher
from scheduler import PolymarketScheduler

# Логирование
logger.remove()
logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}")
logger.add(
    config.DATA_DIR / "bot.log",
    level="DEBUG",
    rotation="10 MB",
    retention="7 days",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level:<7} | {message}",
)


async def post_init(application: Application):
    """Инициализация после запуска бота"""
    bot = application.bot

    # БД
    await db.init_db()

    # Polymarket клиент (чтение рынков/цен)
    client = PolymarketClient()
    await client.init()

    # Publisher (канал для сигналов)
    publisher = TelegramPublisher(bot)

    if config.TELEGRAM_CHANNEL_ID:
        is_admin = await publisher.check_bot_is_admin()
        if not is_admin:
            logger.warning(f"Бот не админ канала {config.TELEGRAM_CHANNEL_ID} — сигналы не будут публиковаться")

    # Компоненты
    scanner = MarketScanner(client)
    analytics = AnalyticsEngine()
    signal_gen = SignalGenerator(analytics)
    chart_gen = ChartGenerator()
    risk_mgr = RiskManager()
    trader = AutoTrader(client, risk_mgr)
    portfolio = PortfolioTracker(client)

    # Планировщик
    scheduler = PolymarketScheduler(
        scanner=scanner,
        analytics_engine=analytics,
        signal_generator=signal_gen,
        auto_trader=trader,
        portfolio_tracker=portfolio,
        publisher=publisher,
        chart_generator=chart_gen,
    )

    # Привязываем компоненты к командам
    telegram_commands.set_components(publisher, scheduler, scanner, client, chart_gen)

    # Запуск планировщика
    scheduler.start()

    # Сохраняем ссылки
    application.bot_data["client"] = client
    application.bot_data["publisher"] = publisher
    application.bot_data["scanner"] = scanner
    application.bot_data["scheduler"] = scheduler
    application.bot_data["trader"] = trader
    application.bot_data["portfolio"] = portfolio

    # Web Admin
    if config.WEB_ADMIN_PORT:
        from web_admin import start_web_admin
        await start_web_admin(application)
        logger.info(f"Web Admin: http://0.0.0.0:{config.WEB_ADMIN_PORT}")

    # Первое сканирование — в фоне, не блокируя бот
    async def _initial_scan():
        logger.info("Первое сканирование рынков (фон)...")
        await scanner.scan_markets()
        await scanner.update_prices()
        markets = await db.get_active_markets()
        logger.info(f"Сканирование завершено: {len(markets)} рынков")

    asyncio.create_task(_initial_scan())

    users = await db.get_connected_users()
    logger.info("Бот запущен и готов к работе")
    logger.info(f"  Канал: {config.TELEGRAM_CHANNEL_ID or 'не задан'}")
    logger.info(f"  Подключённых юзеров: {len(users)}")
    logger.info(f"  Категории: {', '.join(config.CATEGORIES)}")
    logger.info(f"  Скан: каждые {config.SCAN_INTERVAL_MINUTES} мин")


async def run_setup_mode():
    """Режим настройки — только веб-панель, без Telegram-бота"""
    await db.init_db()

    from web_admin import start_setup_web_admin
    await start_setup_web_admin()

    port = config.WEB_ADMIN_PORT or 8081
    token = config.WEB_ADMIN_TOKEN or "setup"
    logger.info("=" * 50)
    logger.info("  РЕЖИМ НАСТРОЙКИ")
    logger.info(f"  Открой: http://IP:{port}/?token={token}")
    logger.info("  Заполни настройки и перезапусти бот")
    logger.info("=" * 50)

    # Бесконечный цикл — ждём пока настроят и перезапустят
    while True:
        await asyncio.sleep(3600)


def main():
    """Запуск бота"""
    # Если нет токена — режим настройки (только веб-панель)
    if not config.TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN не задан — запуск в режиме настройки")
        asyncio.run(run_setup_mode())
        return

    if config.ADMIN_TELEGRAM_ID == 0:
        logger.warning("ADMIN_TELEGRAM_ID не задан — запуск в режиме настройки")
        asyncio.run(run_setup_mode())
        return

    logger.info("Запуск Polymarket Bot...")

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # ConversationHandler для /connect (должен быть перед обычными handlers)
    app.add_handler(telegram_commands.get_connect_handler())

    # Callback handler для inline-кнопок
    app.add_handler(CallbackQueryHandler(telegram_commands.callback_handler))

    # Юзерские команды (личка)
    app.add_handler(CommandHandler("start", telegram_commands.cmd_start))
    app.add_handler(CommandHandler("help", telegram_commands.cmd_help))
    app.add_handler(CommandHandler("disconnect", telegram_commands.cmd_disconnect))
    app.add_handler(CommandHandler("portfolio", telegram_commands.cmd_portfolio))
    app.add_handler(CommandHandler("trade", telegram_commands.cmd_trade))
    app.add_handler(CommandHandler("close", telegram_commands.cmd_close))
    app.add_handler(CommandHandler("markets", telegram_commands.cmd_markets))

    # Админские команды
    app.add_handler(CommandHandler("status", telegram_commands.cmd_status))
    app.add_handler(CommandHandler("signals", telegram_commands.cmd_signals))
    app.add_handler(CommandHandler("scan", telegram_commands.cmd_scan))
    app.add_handler(CommandHandler("pause", telegram_commands.cmd_pause))
    app.add_handler(CommandHandler("resume", telegram_commands.cmd_resume))

    logger.info("Бот запущен, ожидание команд...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
