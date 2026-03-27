"""Генератор сигналов — создание торговых сигналов на основе аналитики"""

import aiohttp
from loguru import logger

import config
import db
from analytics_engine import AnalyticsEngine


class SignalGenerator:
    def __init__(self, analytics: AnalyticsEngine):
        self.analytics = analytics

    async def generate_signals(self) -> list[dict]:
        """
        Основной pipeline генерации сигналов:
        1. Обнаружение значимых движений
        2. Оценка уверенности
        3. Опциональный GPT-анализ
        4. Сохранение в БД
        """
        movements = await self.analytics.detect_significant_movements()
        if not movements:
            return []

        signals = []
        for item in movements:
            market = item["market"]
            analysis = item["analysis"]
            reasons = item["reasons"]

            # Определяем направление
            change_1h = analysis["change_1h"]
            direction = "BUY" if change_1h > 0 else "SELL"

            # Определяем тип сигнала (приоритет)
            if "probability_shift" in reasons:
                signal_type = "probability_shift"
            elif "volume_spike" in reasons:
                signal_type = "volume_spike"
            else:
                signal_type = "high_momentum"

            # Считаем уверенность
            confidence = self._calculate_confidence(analysis, reasons)

            # Пропускаем слабые сигналы
            if confidence < 0.3:
                continue

            # GPT-анализ (опционально)
            reasoning = self._build_reasoning(analysis, reasons)
            if config.OPENAI_API_KEY:
                gpt_reasoning = await self._gpt_analyze(market, analysis)
                if gpt_reasoning:
                    reasoning = gpt_reasoning
                    signal_type = "gpt_analysis"

            # Сохраняем сигнал
            signal_id = await db.save_signal(
                market_id=market["id"],
                signal_type=signal_type,
                direction=direction,
                confidence=confidence,
                probability_at_signal=analysis["current_price"],
                probability_change=change_1h,
                reasoning=reasoning,
            )

            signal_data = {
                "id": signal_id,
                "market_id": market["id"],
                "question": market["question"],
                "category": market.get("category", ""),
                "polymarket_url": market.get("polymarket_url", ""),
                "signal_type": signal_type,
                "direction": direction,
                "confidence": confidence,
                "probability_at_signal": analysis["current_price"],
                "probability_change": change_1h,
                "reasoning": reasoning,
                "token_id_yes": market.get("token_id_yes", ""),
                "token_id_no": market.get("token_id_no", ""),
            }
            signals.append(signal_data)
            logger.info(
                f"Сигнал: {direction} '{market['question'][:50]}...' "
                f"(conf={confidence:.2f}, type={signal_type})"
            )

        return signals

    def _calculate_confidence(self, analysis: dict, reasons: list[str]) -> float:
        """Расчёт уверенности 0-1 на основе анализа"""
        score = 0.0

        # Сила сдвига вероятности (основной фактор)
        shift = abs(analysis["change_1h"])
        if shift >= 0.10:
            score += 0.4
        elif shift >= 0.05:
            score += 0.25
        elif shift >= 0.03:
            score += 0.1

        # Согласованность трендов (1ч и 6ч в одном направлении)
        if analysis["change_1h"] * analysis["change_6h"] > 0:
            score += 0.15

        # Моментум
        if abs(analysis["momentum"]) >= 0.02:
            score += 0.15
        elif abs(analysis["momentum"]) >= 0.01:
            score += 0.08

        # Объём
        if analysis["volume_ratio"] >= 3.0:
            score += 0.15
        elif analysis["volume_ratio"] >= 2.0:
            score += 0.1

        # Количество причин
        if len(reasons) >= 3:
            score += 0.15
        elif len(reasons) >= 2:
            score += 0.1

        return min(score, 1.0)

    def _build_reasoning(self, analysis: dict, reasons: list[str]) -> str:
        """Сформировать текстовое обоснование сигнала"""
        parts = []

        change_1h = analysis["change_1h"] * 100
        change_6h = analysis["change_6h"] * 100

        if "probability_shift" in reasons:
            parts.append(f"Probability shifted {change_1h:+.1f}% in 1h")

        if "volume_spike" in reasons:
            parts.append(f"Volume {analysis['volume_ratio']:.1f}x above average")

        if "high_momentum" in reasons:
            parts.append(f"Strong momentum detected")

        if abs(change_6h) > abs(change_1h):
            parts.append(f"6h trend: {change_6h:+.1f}%")

        return ". ".join(parts)

    async def _gpt_analyze(self, market: dict, analysis: dict) -> str | None:
        """GPT-анализ контекста рынка"""
        if not config.OPENAI_API_KEY:
            return None

        question = market.get("question", "")
        current = analysis["current_price"] * 100
        change_1h = analysis["change_1h"] * 100
        change_24h = analysis["change_24h"] * 100

        prompt = (
            f"You are a prediction market analyst. Briefly analyze this Polymarket event "
            f"(max 2 sentences):\n\n"
            f"Question: {question}\n"
            f"Current probability: {current:.0f}%\n"
            f"Change 1h: {change_1h:+.1f}%\n"
            f"Change 24h: {change_24h:+.1f}%\n\n"
            f"Why might the probability be moving? Is this a good entry point?"
        )

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {config.OPENAI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "gpt-4o-mini",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 150,
                        "temperature": 0.7,
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"].strip()
                    logger.warning(f"GPT API: HTTP {resp.status}")
                    return None
        except Exception as e:
            logger.warning(f"GPT анализ недоступен: {e}")
            return None
