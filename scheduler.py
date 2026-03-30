"""Планировщик задач — сканирование, анализ, отчёты"""

import asyncio
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

import config
import db


class PolymarketScheduler:
    def __init__(
        self,
        scanner,
        analytics_engine,
        signal_generator,
        auto_trader,
        portfolio_tracker,
        publisher,
        chart_generator,
    ):
        self.scanner = scanner
        self.analytics = analytics_engine
        self.signal_gen = signal_generator
        self.trader = auto_trader
        self.portfolio = portfolio_tracker
        self.publisher = publisher
        self.chart_gen = chart_generator
        self.scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)
        self._job_ids: list[str] = []
        self.is_paused = False

    def start(self):
        """Запустить планировщик"""
        self._schedule_all()
        self.scheduler.start()
        logger.info("Планировщик запущен")

    def stop(self):
        """Остановить планировщик"""
        self.scheduler.shutdown(wait=False)
        logger.info("Планировщик остановлен")

    def reschedule(self):
        """Пересоздать расписание"""
        for job_id in self._job_ids:
            try:
                self.scheduler.remove_job(job_id)
            except Exception:
                pass
        self._job_ids.clear()
        self._schedule_all()
        logger.info("Расписание пересоздано")

    def _schedule_all(self):
        """Настроить все задачи"""
        # Сканирование рынков
        self._add_job(
            "market_scan",
            self._run_scan,
            IntervalTrigger(minutes=config.SCAN_INTERVAL_MINUTES),
        )

        # Глубокий анализ + генерация сигналов
        self._add_job(
            "deep_analysis",
            self._run_analysis,
            IntervalTrigger(minutes=config.DEEP_ANALYSIS_INTERVAL_MINUTES),
        )

        # Проверка открытых позиций (стоп-лоссы для всех юзеров)
        self._add_job(
            "position_check",
            self._run_position_check,
            IntervalTrigger(minutes=5),
        )

        # Дневной отчёт
        self._add_job(
            "daily_summary",
            self._run_daily_summary,
            CronTrigger(
                hour=config.DAILY_SUMMARY_HOUR,
                minute=0,
                timezone=config.TIMEZONE,
            ),
        )

        # Снимок дневного P&L
        self._add_job(
            "daily_pnl",
            self._run_daily_pnl,
            CronTrigger(hour=23, minute=59, timezone=config.TIMEZONE),
        )

        # Очистка старых графиков
        self._add_job(
            "chart_cleanup",
            self._run_chart_cleanup,
            CronTrigger(hour=4, minute=0, timezone=config.TIMEZONE),
        )

        # Лог-отчёт админу
        for hour in (0, 12):
            self._add_job(
                f"log_report_{hour:02d}",
                self._run_log_report,
                CronTrigger(hour=hour, minute=0, timezone=config.TIMEZONE),
            )

    def _add_job(self, job_id: str, func, trigger):
        """Добавить задачу в планировщик"""
        self.scheduler.add_job(
            func, trigger, id=job_id, replace_existing=True
        )
        self._job_ids.append(job_id)

    # ── Задачи ───────────────────────────────────────────────

    async def _run_scan(self):
        """Сканирование + обновление цен + мгновенные сигналы при сильных просадках"""
        if self.is_paused:
            return
        try:
            await self.scanner.scan_markets()
            await self.scanner.update_prices()

            # Проверяем на горячие сигналы (сильная просадка >15%)
            await self._check_hot_signals()
        except Exception as e:
            logger.error(f"Ошибка сканирования: {e}")

    async def _check_hot_signals(self):
        """Мгновенные сигналы — если найдена сильная просадка, не ждём часового анализа"""
        try:
            from signal_generator import SignalGenerator
            from analytics_engine import AnalyticsEngine

            # Ищем движения с повышенным порогом
            movements = await self.analytics.detect_significant_movements()
            if not movements:
                return

            hot_signals = []
            for item in movements:
                analysis = item["analysis"]
                market = item["market"]
                change = analysis["change_1h"]

                # Только сильные просадки: >20% падение, цена 20-65%
                if change >= 0:
                    continue
                if abs(change) < 0.20:
                    continue
                price = analysis["current_price"]
                if price < 0.20 or price > 0.65:
                    continue

                # Проверяем что не мусор
                from signal_generator import TRASH_PATTERNS
                q = market.get("question", "").lower()
                if any(p in q for p in TRASH_PATTERNS):
                    continue

                potential = (1.0 - price) / price
                hot_signals.append({
                    "market": market,
                    "analysis": analysis,
                    "drop": abs(change),
                    "potential": potential,
                })

            if not hot_signals:
                return

            # Берём топ-2 самых жирных
            hot_signals.sort(key=lambda s: s["drop"] * s["potential"], reverse=True)
            hot_signals = hot_signals[:2]

            logger.info(f"🔥 Найдено {len(hot_signals)} горячих сигналов!")

            hot_signal_data = []
            for hs in hot_signals:
                market = hs["market"]
                analysis = hs["analysis"]

                signal_id = await db.save_signal(
                    market_id=market["id"],
                    signal_type="hot_dip",
                    direction="BUY",
                    confidence=0.85,
                    probability_at_signal=analysis["current_price"],
                    probability_change=analysis["change_1h"],
                    reasoning=f"🔥 HOT: dropped {hs['drop']*100:.0f}% in 1h | potential +{hs['potential']*100:.0f}%",
                )

                sig = {
                    "id": signal_id,
                    "market_id": market["id"],
                    "question": market["question"],
                    "category": market.get("category", ""),
                    "polymarket_url": market.get("polymarket_url", ""),
                    "signal_type": "hot_dip",
                    "direction": "BUY",
                    "confidence": 0.85,
                    "probability_at_signal": analysis["current_price"],
                    "probability_change": analysis["change_1h"],
                    "reasoning": f"🔥 HOT: dropped {hs['drop']*100:.0f}% in 1h | potential +{hs['potential']*100:.0f}%",
                    "token_id_yes": market.get("token_id_yes", ""),
                    "token_id_no": market.get("token_id_no", ""),
                }
                hot_signal_data.append(sig)

                # Публикация в канал
                chart_path = None
                if self.chart_gen:
                    chart_path = await self.chart_gen.generate_probability_chart(market["id"])
                await self.publisher.send_signal(sig, chart_path)
                await db.mark_signal_published(signal_id)

                logger.info(f"🔥 HOT сигнал: '{market['question'][:50]}' drop {hs['drop']*100:.0f}%")
                await asyncio.sleep(2)

            # Автоставки по горячим сигналам
            if hot_signal_data:
                await self._run_auto_trades(hot_signal_data)

        except Exception as e:
            logger.error(f"Ошибка горячих сигналов: {e}")

    async def _run_analysis(self):
        """Анализ + генерация сигналов + публикация"""
        if self.is_paused:
            return
        try:
            # Генерация сигналов
            new_signals = await self.signal_gen.generate_signals()
            if not new_signals:
                return

            logger.info(f"Сгенерировано {len(new_signals)} сигналов")

            # Публикация в канал
            for signal in new_signals:
                chart_path = None
                if self.chart_gen:
                    chart_path = await self.chart_gen.generate_probability_chart(
                        signal["market_id"]
                    )

                await self.publisher.send_signal(signal, chart_path)
                await db.mark_signal_published(signal["id"])
                await asyncio.sleep(2)

            # Автоставки для подписанных юзеров
            await self._run_auto_trades(new_signals)

        except Exception as e:
            logger.error(f"Ошибка анализа/публикации: {e}")

    async def _run_auto_trades(self, signals: list[dict]):
        """Выполнить автоставки для юзеров с включённым auto_trade"""
        auto_users = await db.get_auto_trade_users()
        if not auto_users:
            logger.info("Автоставки: нет юзеров с auto_trade=1")
            return

        logger.info(f"Автоставки: {len(auto_users)} юзеров, {len(signals)} сигналов")

        from polymarket_client import PolymarketClient
        client: PolymarketClient = self.scanner.client

        for user in auto_users:
            try:
                amount = user.get("auto_amount", 0.5)
                max_daily = user.get("auto_max_daily", 10.0)
                min_conf = user.get("auto_min_confidence", 0.7)

                # Считаем сколько уже потрачено сегодня
                today_trades = await db.get_trade_history(limit=100, user_id=user["id"])
                from datetime import datetime
                today_str = datetime.utcnow().strftime("%Y-%m-%d")
                today_spent = sum(
                    t["size_usdc"] for t in today_trades
                    if t.get("created_at", "").startswith(today_str)
                )

                logger.info(f"Автоставки: юзер {user['telegram_id']} — потрачено сегодня ${today_spent:.2f}/{max_daily:.0f}, ставка ${amount:.2f}, мин.увер. {min_conf}")

                trades_placed = 0
                for signal in signals:
                    if signal["confidence"] < min_conf:
                        logger.info(f"Автоставки: пропуск — уверенность {signal['confidence']:.2f} < {min_conf}")
                        continue
                    if today_spent + amount > max_daily:
                        logger.info(f"Автоставки: дневной лимит ${max_daily} достигнут")
                        break

                    # Получаем CLOB клиент юзера
                    if user.get("private_key"):
                        user_clob = await client.get_user_client(user["telegram_id"], private_key=user["private_key"])
                    else:
                        user_clob = await client.get_user_client(
                            user["telegram_id"],
                            api_key=user["api_key"], api_secret=user["api_secret"],
                            api_passphrase=user["api_passphrase"],
                        )
                    if not user_clob:
                        logger.error(f"Автоставки: CLOB недоступен для {user['telegram_id']}")
                        break

                    # Определяем токен
                    token_id = signal.get("token_id_yes") if signal["direction"] == "BUY" else signal.get("token_id_no")
                    if not token_id:
                        logger.warning(f"Автоставки: нет token_id для сигнала")
                        continue

                    # Цена: сначала CLOB midpoint, потом CLOB price, потом из БД
                    price = await client.get_midpoint(token_id)
                    if not price or price <= 0 or price >= 1:
                        price = await client.get_price(token_id)
                    if not price or price <= 0 or price >= 1:
                        # Берём из БД (Gamma API цена)
                        latest = await db.get_latest_price(signal["market_id"])
                        if latest:
                            price = latest["price_yes"]
                    logger.info(f"Автоставки: цена для {token_id[:16]}... = {price}")
                    if not price or price <= 0 or price >= 1:
                        logger.warning(f"Автоставки: невалидная цена {price}")
                        continue

                    shares = amount / price
                    # Polymarket минимум 5 shares
                    if shares < 5:
                        shares = 5.0
                        amount = shares * price
                    # Округляем цену до 0.01
                    price = round(price, 2)
                    logger.info(f"Автоставки: размещаю ордер BUY {shares:.2f} shares @ {price:.2f} (${amount:.2f})")
                    result = await user_clob.place_order(token_id, "BUY", shares, price)
                    logger.info(f"Автоставки: результат ордера = {result}")

                    if result:
                        order_id = result.get("orderID", result.get("id", ""))
                        await db.save_trade(
                            user_id=user["id"],
                            signal_id=signal.get("id"),
                            market_id=signal["market_id"],
                            token_id=token_id,
                            side="BUY",
                            size_usdc=amount,
                            price=price,
                            order_id=order_id,
                            status="filled",
                        )
                        trades_placed += 1
                        today_spent += amount
                        logger.info(
                            f"Автоставка: юзер {user['telegram_id']} — "
                            f"{signal['direction']} ${amount} на '{signal['question'][:40]}'"
                        )
                    await asyncio.sleep(1)

                if trades_placed:
                    # Уведомляем юзера
                    try:
                        from telegram import Bot
                        bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
                        await bot.send_message(
                            chat_id=user["telegram_id"],
                            text=f"🤖 Автоставки: размещено <b>{trades_placed}</b> ставок по ${amount:.0f}",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass

            except Exception as e:
                logger.error(f"Автоставки ошибка для {user['telegram_id']}: {e}")

    async def _run_position_check(self):
        """Проверка открытых позиций"""
        if self.is_paused:
            return
        try:
            await self.trader.check_open_positions()
        except Exception as e:
            logger.error(f"Ошибка проверки позиций: {e}")

    async def _run_daily_summary(self):
        """Ежедневный отчёт"""
        try:
            markets = await db.get_active_markets()
            today_pnl = await db.get_today_pnl()
            portfolio_stats = await db.get_portfolio_stats()

            summary = {
                "tracked_markets": len(markets),
                "signals_today": today_pnl.get("trades_count", 0),
                "trades_today": today_pnl.get("trades_count", 0),
                "pnl": today_pnl.get("total_pnl", 0),
                "wins": today_pnl.get("wins", 0),
                "losses": today_pnl.get("losses", 0),
                "open_positions": portfolio_stats.get("open_positions", 0),
                "win_rate": portfolio_stats.get("win_rate", 0),
            }
            await self.publisher.send_daily_summary(summary)
            logger.info("Дневной отчёт отправлен")
        except Exception as e:
            logger.error(f"Ошибка дневного отчёта: {e}")

    async def _run_daily_pnl(self):
        """Снимок P&L"""
        try:
            today = datetime.utcnow().strftime("%Y-%m-%d")
            await db.record_daily_pnl(today)
        except Exception as e:
            logger.error(f"Ошибка снимка P&L: {e}")

    async def _run_chart_cleanup(self):
        """Очистка старых графиков"""
        try:
            if self.chart_gen:
                self.chart_gen.cleanup_old_charts(max_age_hours=24)
        except Exception as e:
            logger.error(f"Ошибка очистки графиков: {e}")

    async def _run_log_report(self):
        """Лог-отчёт админу"""
        try:
            markets = await db.get_active_markets()
            portfolio = await db.get_portfolio_stats()

            users = await db.get_connected_users()
            text = (
                f"📊 Статус бота\n\n"
                f"Рынков: {len(markets)}\n"
                f"Юзеров: {len(users)}\n"
                f"Открытых позиций: {portfolio['open_positions']}\n"
                f"P&L: ${portfolio['realized_pnl']:+.2f}\n"
                f"Win rate: {portfolio['win_rate']:.0f}%\n"
            )
            await self.publisher.notify_admin(text)
        except Exception as e:
            logger.error(f"Ошибка лог-отчёта: {e}")

    def get_next_run_times(self) -> dict[str, str]:
        """Получить время следующего запуска для каждой задачи"""
        result = {}
        for job in self.scheduler.get_jobs():
            if job.next_run_time:
                result[job.id] = job.next_run_time.strftime("%H:%M:%S (%d.%m)")
        return result
