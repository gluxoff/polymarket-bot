"""Аналитика рынков — анализ трендов вероятностей"""

from datetime import datetime, timedelta
from loguru import logger

import config
import db


class AnalyticsEngine:
    """Анализ трендов вероятностей на рынках Polymarket"""

    async def analyze_market(self, market_id: int) -> dict | None:
        """
        Полный анализ одного рынка.
        Возвращает dict с метриками или None если данных мало.
        """
        # Получаем историю за 24ч
        history = await db.get_price_history(market_id, hours=24)
        if len(history) < 2:
            return None

        prices = [(h["price_yes"], h["recorded_at"]) for h in history]
        current_price = prices[-1][0]

        # Изменения за разные периоды
        change_1h = self._price_change(prices, hours=1)
        change_6h = self._price_change(prices, hours=6)
        change_24h = self._price_change(prices, hours=24)

        # Моментум (скорость изменения)
        momentum = self._calculate_momentum(prices)

        # Волатильность
        volatility = self._calculate_volatility(prices)

        # Объём (из последней записи)
        latest_volume = history[-1].get("volume", 0)
        avg_volume = sum(h.get("volume", 0) for h in history) / len(history) if history else 0
        volume_ratio = latest_volume / avg_volume if avg_volume > 0 else 1.0

        return {
            "market_id": market_id,
            "current_price": current_price,
            "change_1h": change_1h,
            "change_6h": change_6h,
            "change_24h": change_24h,
            "momentum": momentum,
            "volatility": volatility,
            "volume_ratio": volume_ratio,
            "data_points": len(prices),
        }

    async def detect_significant_movements(self) -> list[dict]:
        """
        Найти рынки со значимыми движениями:
        - Сдвиг вероятности > порога за 1ч
        - Всплеск объёма > множителя
        - Высокий моментум
        """
        markets = await db.get_active_markets()
        significant = []

        for market in markets:
            analysis = await self.analyze_market(market["id"])
            if not analysis:
                continue

            reasons = []

            # Значимый сдвиг вероятности
            if abs(analysis["change_1h"]) >= config.PROBABILITY_SHIFT_THRESHOLD:
                reasons.append("probability_shift")

            # Всплеск объёма
            if analysis["volume_ratio"] >= config.VOLUME_SPIKE_MULTIPLIER:
                reasons.append("volume_spike")

            # Сильный моментум (быстрое изменение)
            if abs(analysis["momentum"]) >= config.PROBABILITY_SHIFT_THRESHOLD * 0.5:
                reasons.append("high_momentum")

            if reasons:
                significant.append({
                    "market": market,
                    "analysis": analysis,
                    "reasons": reasons,
                })

        if significant:
            logger.info(f"Обнаружено {len(significant)} значимых движений")

        return significant

    def _price_change(self, prices: list[tuple], hours: int) -> float:
        """Изменение цены за последние N часов"""
        if not prices:
            return 0.0

        current = prices[-1][0]
        cutoff = datetime.utcnow() - timedelta(hours=hours)

        # Находим ближайшую точку к cutoff
        closest = None
        for price, timestamp in prices:
            try:
                ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                ts = datetime.fromisoformat(timestamp)

            if ts.replace(tzinfo=None) <= cutoff:
                closest = price

        if closest is None:
            # Берём самую старую доступную
            closest = prices[0][0]

        return current - closest

    def _calculate_momentum(self, prices: list[tuple]) -> float:
        """Скорость изменения — наклон линии за последние 6 точек"""
        if len(prices) < 3:
            return 0.0

        # Берём последние 6 точек (или все, если меньше)
        recent = [p[0] for p in prices[-6:]]

        # Простая линейная регрессия: наклон
        n = len(recent)
        x_mean = (n - 1) / 2.0
        y_mean = sum(recent) / n

        numerator = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(recent))
        denominator = sum((i - x_mean) ** 2 for i in range(n))

        if denominator == 0:
            return 0.0

        return numerator / denominator

    def _calculate_volatility(self, prices: list[tuple]) -> float:
        """Стандартное отклонение изменений цены"""
        if len(prices) < 3:
            return 0.0

        values = [p[0] for p in prices]
        changes = [values[i] - values[i - 1] for i in range(1, len(values))]

        if not changes:
            return 0.0

        mean = sum(changes) / len(changes)
        variance = sum((c - mean) ** 2 for c in changes) / len(changes)
        return variance ** 0.5
