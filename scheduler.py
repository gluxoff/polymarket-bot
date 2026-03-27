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

        # Проверка открытых позиций
        if config.AUTO_TRADE_ENABLED:
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
        """Сканирование + обновление цен"""
        if self.is_paused:
            return
        try:
            await self.scanner.scan_markets()
            await self.scanner.update_prices()
        except Exception as e:
            logger.error(f"Ошибка сканирования: {e}")

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

            # Публикация
            for signal in new_signals:
                # Генерация графика
                chart_path = None
                if self.chart_gen:
                    chart_path = await self.chart_gen.generate_probability_chart(
                        signal["market_id"]
                    )

                # Публикация в Telegram
                await self.publisher.send_signal(signal, chart_path)
                await db.mark_signal_published(signal["id"])

                # Автоторговля
                if config.AUTO_TRADE_ENABLED and self.trader:
                    await self.trader.execute_signal(signal)

                # Пауза между сообщениями
                await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"Ошибка анализа/публикации: {e}")

    async def _run_position_check(self):
        """Проверка открытых позиций"""
        if self.is_paused or not self.trader:
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

            text = (
                f"📊 Статус бота\n\n"
                f"Рынков: {len(markets)}\n"
                f"Открытых позиций: {portfolio['open_positions']}\n"
                f"P&L: ${portfolio['realized_pnl']:+.2f}\n"
                f"Win rate: {portfolio['win_rate']:.0f}%\n"
                f"Автоторговля: {'✅' if config.AUTO_TRADE_ENABLED else '❌'}\n"
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
