"""Генератор сигналов v2 — только качественные BUY сигналы"""

import aiohttp
from loguru import logger

import config
import db
from analytics_engine import AnalyticsEngine

# Мусорные паттерны — рынки которые не имеют смысла для торговли
TRASH_PATTERNS = [
    "will trump say", "will biden say", "will elon say",
    "will mrbeast say", "said during",
    "say \"", "say '",
    "during the next episode",
    "during his next video",
    "during the fii", "during the press conference",
    "at the rally",
    "highest temperature", "lowest temperature",
    "weather",
]

MAX_SIGNALS_PER_CYCLE = 5


class SignalGenerator:
    def __init__(self, analytics: AnalyticsEngine):
        self.analytics = analytics

    async def generate_signals(self) -> list[dict]:
        """
        Стратегия v2:
        - Только BUY (покупаем дешёво, получаем $1 при исполнении)
        - Цена 15-70% (есть потенциал роста 30%+)
        - Фильтр мусорных рынков
        - Макс 5 лучших за цикл
        """
        movements = await self.analytics.detect_significant_movements()
        if not movements:
            return []

        candidates = []
        for item in movements:
            market = item["market"]
            analysis = item["analysis"]
            reasons = item["reasons"]

            question = market.get("question", "").lower()
            price = analysis["current_price"]
            change_1h = analysis["change_1h"]

            # === ФИЛЬТРЫ ===

            # Только растущие (BUY)
            if change_1h <= 0:
                continue

            # Цена 15-70% — есть куда расти
            if price < 0.15 or price > 0.70:
                continue

            # Потенциальный профит минимум 30%
            potential_profit = (1.0 - price) / price
            if potential_profit < 0.30:
                continue

            # Фильтр мусора
            if self._is_trash_market(question):
                continue

            # === SCORING ===
            confidence = self._calculate_confidence(analysis, reasons, potential_profit)

            if confidence < 0.5:
                continue

            reasoning = self._build_reasoning(analysis, reasons, potential_profit)

            # GPT (опционально)
            if config.OPENAI_API_KEY:
                gpt = await self._gpt_analyze(market, analysis)
                if gpt:
                    reasoning = gpt

            signal_type = "probability_shift"
            if "volume_spike" in reasons:
                signal_type = "volume_spike"

            candidates.append({
                "market": market,
                "analysis": analysis,
                "confidence": confidence,
                "signal_type": signal_type,
                "reasoning": reasoning,
                "potential_profit": potential_profit,
            })

        # Сортируем: сначала по потенциальному профиту * уверенность
        candidates.sort(key=lambda c: c["confidence"] * c["potential_profit"], reverse=True)
        candidates = candidates[:MAX_SIGNALS_PER_CYCLE]

        # Сохраняем в БД
        signals = []
        for c in candidates:
            market = c["market"]
            analysis = c["analysis"]

            signal_id = await db.save_signal(
                market_id=market["id"],
                signal_type=c["signal_type"],
                direction="BUY",
                confidence=c["confidence"],
                probability_at_signal=analysis["current_price"],
                probability_change=analysis["change_1h"],
                reasoning=c["reasoning"],
            )

            signals.append({
                "id": signal_id,
                "market_id": market["id"],
                "question": market["question"],
                "category": market.get("category", ""),
                "polymarket_url": market.get("polymarket_url", ""),
                "signal_type": c["signal_type"],
                "direction": "BUY",
                "confidence": c["confidence"],
                "probability_at_signal": analysis["current_price"],
                "probability_change": analysis["change_1h"],
                "reasoning": c["reasoning"],
                "token_id_yes": market.get("token_id_yes", ""),
                "token_id_no": market.get("token_id_no", ""),
            })

            profit_pct = c["potential_profit"] * 100
            logger.info(
                f"Сигнал: BUY '{market['question'][:50]}...' "
                f"@ {analysis['current_price']*100:.0f}% "
                f"(conf={c['confidence']:.2f}, profit={profit_pct:.0f}%)"
            )

        return signals

    def _is_trash_market(self, question: str) -> bool:
        """Проверить является ли рынок мусорным"""
        for pattern in TRASH_PATTERNS:
            if pattern in question:
                return True
        return False

    def _calculate_confidence(self, analysis: dict, reasons: list[str],
                               potential_profit: float) -> float:
        """Расчёт уверенности с учётом потенциала"""
        score = 0.0

        # Сила сдвига (растёт — хорошо)
        shift = analysis["change_1h"]
        if shift >= 0.15:
            score += 0.30
        elif shift >= 0.10:
            score += 0.25
        elif shift >= 0.08:
            score += 0.15

        # Согласованность: 1ч и 6ч оба вверх
        if analysis["change_1h"] > 0 and analysis["change_6h"] > 0:
            score += 0.15

        # 24ч тренд тоже вверх — сильный сигнал
        if analysis["change_24h"] > 0 and analysis["change_1h"] > 0:
            score += 0.10

        # Моментум (ускорение)
        if analysis["momentum"] >= 0.02:
            score += 0.15
        elif analysis["momentum"] >= 0.01:
            score += 0.08

        # Объём
        if analysis["volume_ratio"] >= 3.0:
            score += 0.15
        elif analysis["volume_ratio"] >= 2.0:
            score += 0.10

        # Потенциальный профит бонус
        if potential_profit >= 2.0:  # 200%+
            score += 0.15
        elif potential_profit >= 1.0:  # 100%+
            score += 0.10
        elif potential_profit >= 0.5:  # 50%+
            score += 0.05

        return min(score, 1.0)

    def _build_reasoning(self, analysis: dict, reasons: list[str],
                          potential_profit: float) -> str:
        parts = []

        change_1h = analysis["change_1h"] * 100
        parts.append(f"+{change_1h:.1f}% in 1h")

        if "volume_spike" in reasons:
            parts.append(f"volume {analysis['volume_ratio']:.1f}x avg")

        if analysis["change_6h"] > 0:
            parts.append(f"6h trend +{analysis['change_6h']*100:.1f}%")

        parts.append(f"potential profit {potential_profit*100:.0f}%")

        return " | ".join(parts)

    async def _gpt_analyze(self, market: dict, analysis: dict) -> str | None:
        if not config.OPENAI_API_KEY:
            return None

        question = market.get("question", "")
        current = analysis["current_price"] * 100
        change_1h = analysis["change_1h"] * 100
        profit = ((1.0 - analysis["current_price"]) / analysis["current_price"]) * 100

        prompt = (
            f"Prediction market analyst. 1-2 sentences max.\n\n"
            f"Market: {question}\n"
            f"Price: {current:.0f}% (up {change_1h:.0f}% in 1h)\n"
            f"Potential profit if YES: {profit:.0f}%\n\n"
            f"Is this a good BUY opportunity? Why is probability rising?"
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
                        "max_tokens": 100,
                        "temperature": 0.5,
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"].strip()
                    return None
        except Exception as e:
            logger.warning(f"GPT недоступен: {e}")
            return None
