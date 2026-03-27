"""Генератор графиков вероятностей — современный тёмный дизайн"""

import asyncio
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
        history = await db.get_price_history(market_id, hours=hours)
        if len(history) < 3:
            return None

        market = await db.get_market_by_id(market_id)
        if not market:
            return None

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._render_chart, history, market["question"],
            market.get("category", ""), market_id,
        )

    def _render_chart(self, history, question, category, market_id) -> str | None:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            from matplotlib.ticker import FuncFormatter
            from datetime import datetime
            import numpy as np

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
                    prices.append(h["price_yes"] * 100)
                except (ValueError, KeyError):
                    continue

            if len(times) < 3:
                return None

            current = prices[-1]

            # ── Стиль ──────────────────────────────────────────
            BG = "#0D1117"
            CARD_BG = "#161B22"
            GRID = "#21262D"
            TEXT = "#C9D1D9"
            TEXT_DIM = "#8B949E"
            GREEN = "#3FB950"
            RED = "#F85149"
            BLUE = "#58A6FF"
            BLUE_FILL = "#58A6FF"

            # Определяем цвет по тренду
            change = prices[-1] - prices[0]
            line_color = GREEN if change >= 0 else RED
            fill_color = GREEN if change >= 0 else RED

            fig, ax = plt.subplots(figsize=(12, 6), facecolor=BG)
            ax.set_facecolor(CARD_BG)

            # Градиентная заливка под линией
            ax.fill_between(times, prices, min(prices) - 2, alpha=0.15, color=fill_color)

            # Основная линия
            ax.plot(times, prices, color=line_color, linewidth=2.5, solid_capstyle="round")

            # Точка на конце
            ax.scatter([times[-1]], [current], color=line_color, s=60, zorder=5, edgecolors="white", linewidths=1.5)

            # Горизонтальная линия 50%
            ax.axhline(y=50, color=TEXT_DIM, linestyle="--", linewidth=0.6, alpha=0.4)

            # Аннотация текущей цены
            change_str = f"+{change:.1f}%" if change >= 0 else f"{change:.1f}%"
            ax.annotate(
                f" {current:.1f}%  ({change_str})",
                xy=(times[-1], current),
                xytext=(15, 0),
                textcoords="offset points",
                fontsize=14,
                fontweight="bold",
                color=line_color,
                va="center",
                bbox=dict(
                    boxstyle="round,pad=0.4",
                    facecolor=BG,
                    edgecolor=line_color,
                    alpha=0.9,
                    linewidth=1.5,
                ),
            )

            # Оси
            ax.set_ylabel("Probability", fontsize=12, color=TEXT_DIM, labelpad=10)
            ax.yaxis.set_major_formatter(FuncFormatter(lambda y, _: f"{y:.0f}%"))

            # Y лимиты с отступом
            y_min = max(0, min(prices) - 5)
            y_max = min(100, max(prices) + 5)
            ax.set_ylim(y_min, y_max)

            # X ось
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
            ax.xaxis.set_major_locator(mdates.AutoDateLocator())

            # Сетка
            ax.grid(True, color=GRID, linewidth=0.5, alpha=0.5)
            ax.tick_params(colors=TEXT_DIM, labelsize=10)

            # Рамка
            for spine in ax.spines.values():
                spine.set_color(GRID)
                spine.set_linewidth(0.5)

            # Заголовок
            title = question if len(question) <= 65 else question[:62] + "..."
            cat_str = f"  [{category.upper()}]" if category else ""
            ax.set_title(
                f"{title}{cat_str}",
                fontsize=13, fontweight="bold", color=TEXT,
                pad=15, loc="left",
            )

            # Подпись
            fig.text(
                0.99, 0.01, "polymarket.com",
                fontsize=8, color=TEXT_DIM, alpha=0.4,
                ha="right", va="bottom",
            )

            plt.tight_layout()

            # Сохранение
            filename = f"chart_{market_id}_{int(time.time())}.png"
            filepath = str(self.charts_dir / filename)
            fig.savefig(filepath, dpi=150, bbox_inches="tight", facecolor=BG)
            plt.close(fig)

            return filepath

        except Exception as e:
            logger.error(f"Ошибка рендеринга графика: {e}")
            return None

    def cleanup_old_charts(self, max_age_hours: int = 24):
        cutoff = time.time() - max_age_hours * 3600
        count = 0
        for f in self.charts_dir.iterdir():
            if f.suffix == ".png" and f.stat().st_mtime < cutoff:
                f.unlink()
                count += 1
        if count:
            logger.info(f"Удалено {count} старых графиков")
