"""Генератор графиков вероятностей — matplotlib"""

import asyncio
import os
import time
from pathlib import Path

from loguru import logger

import config
import db


class ChartGenerator:
    def __init__(self):
        self.charts_dir = config.CHARTS_DIR

    async def generate_probability_chart(
        self, market_id: int, hours: int = 24
    ) -> str | None:
        """
        Генерировать график вероятности YES за период.
        Возвращает путь к PNG или None.
        """
        history = await db.get_price_history(market_id, hours=hours)
        if len(history) < 3:
            return None

        market = await db.get_market_by_id(market_id)
        if not market:
            return None

        question = market["question"]
        category = market.get("category", "")

        # matplotlib не async — рендерим в executor
        loop = asyncio.get_event_loop()
        chart_path = await loop.run_in_executor(
            None,
            self._render_chart,
            history,
            question,
            category,
            market_id,
        )
        return chart_path

    def _render_chart(
        self,
        history: list[dict],
        question: str,
        category: str,
        market_id: int,
    ) -> str | None:
        """Рендеринг графика (синхронно, вызывается в executor)"""
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            from datetime import datetime

            # Данные
            times = []
            prices = []
            for h in history:
                try:
                    ts = h["recorded_at"]
                    if isinstance(ts, str):
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    else:
                        dt = ts
                    times.append(dt)
                    prices.append(h["price_yes"] * 100)  # в процентах
                except (ValueError, KeyError):
                    continue

            if len(times) < 3:
                return None

            # Создание графика
            fig, ax = plt.subplots(figsize=(10, 5))

            # Основная линия
            ax.plot(times, prices, color="#4A90D9", linewidth=2.0, label="YES probability")

            # Заливка под линией
            ax.fill_between(times, prices, alpha=0.15, color="#4A90D9")

            # Горизонтальная линия 50%
            ax.axhline(y=50, color="#888888", linestyle="--", linewidth=0.8, alpha=0.5)

            # Оси
            ax.set_ylabel("Probability (%)", fontsize=12)
            ax.set_ylim(0, 100)
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
            ax.xaxis.set_major_locator(mdates.AutoDateLocator())
            plt.xticks(rotation=45, fontsize=9)
            plt.yticks(fontsize=10)

            # Заголовок (обрезанный вопрос)
            title = question if len(question) <= 60 else question[:57] + "..."
            cat_label = f" [{category.upper()}]" if category else ""
            ax.set_title(f"{title}{cat_label}", fontsize=11, fontweight="bold", pad=12)

            # Текущая вероятность — аннотация
            current_price = prices[-1]
            ax.annotate(
                f"{current_price:.0f}%",
                xy=(times[-1], current_price),
                xytext=(10, 10),
                textcoords="offset points",
                fontsize=12,
                fontweight="bold",
                color="#4A90D9",
                bbox=dict(boxstyle="round,pad=0.3", facecolor="white", edgecolor="#4A90D9", alpha=0.8),
            )

            # Сетка
            ax.grid(True, alpha=0.3)
            ax.set_facecolor("#FAFAFA")
            fig.patch.set_facecolor("white")

            # Watermark
            fig.text(
                0.99, 0.01, "Polymarket Bot",
                fontsize=8, color="#CCCCCC",
                ha="right", va="bottom",
                alpha=0.7,
            )

            plt.tight_layout()

            # Сохранение
            filename = f"chart_{market_id}_{int(time.time())}.png"
            filepath = str(self.charts_dir / filename)
            fig.savefig(filepath, dpi=120, bbox_inches="tight")
            plt.close(fig)

            return filepath

        except Exception as e:
            logger.error(f"Ошибка рендеринга графика: {e}")
            return None

    async def generate_portfolio_chart(self) -> str | None:
        """График P&L портфеля"""
        # TODO: реализовать когда будет достаточно данных
        return None

    def cleanup_old_charts(self, max_age_hours: int = 24):
        """Удалить графики старше N часов"""
        cutoff = time.time() - max_age_hours * 3600
        count = 0
        for f in self.charts_dir.iterdir():
            if f.suffix == ".png" and f.stat().st_mtime < cutoff:
                f.unlink()
                count += 1
        if count:
            logger.info(f"Удалено {count} старых графиков")
