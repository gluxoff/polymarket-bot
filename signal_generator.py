"""Генератор сигналов v3 — контрарианская стратегия (покупай после падения)"""

import aiohttp
from loguru import logger

import config
import db
from analytics_engine import AnalyticsEngine

TRASH_PATTERNS = [
    "will trump say", "will biden say", "will elon say",
    "will mrbeast say", "said during",
    'say "', "say '",
    "during the next episode", "during his next video",
    "during the fii", "during the press conference",
    "at the rally",
    "highest temperature", "lowest temperature", "weather",
]

MAX_SIGNALS_PER_CYCLE = 5


class SignalGenerator:
    def __init__(self, analytics: AnalyticsEngine):
        self.analytics = analytics

    async def generate_signals(self) -> list[dict]:
        """
        Контрарианская стратегия:
        - Цена резко упала (>8% за час) — паника/overreaction
        - Цена в диапазоне 20-65% — есть потенциал отскока
        - Покупаем YES дёшево, ждём восстановления
        - Без мусорных рынков
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

            question = market.get("question", "")
            price = analysis["current_price"]
            change_1h = analysis["change_1h"]

            # === ФИЛЬТРЫ ===

            # Только ПАДАЮЩИЕ — контрарианская логика
            if change_1h >= 0:
                continue

            # Падение минимум 8%
            if abs(change_1h) < 0.08:
                continue

            # Цена 20-65% — не слишком дешёвая (мусор) и не дорогая
            if price < 0.20 or price > 0.65:
                continue

            # Фильтр мусора
            if self._is_trash_market(question.lower()):
                continue

            # Потенциальный профит
            potential_profit = (1.0 - price) / price

            # === SCORING ===
            confidence = self._calculate_confidence(analysis, reasons, potential_profit)

            if confidence < 0.45:
                continue

            reasoning = self._build_reasoning(analysis, potential_profit)

            # GPT (опционально)
            if config.OPENAI_API_KEY:
                gpt = await self._gpt_analyze(market, analysis)
                if gpt:
                    reasoning = gpt

            signal_type = "contrarian_dip"
            if "volume_spike" in reasons:
                signal_type = "contrarian_volume"

            candidates.append({
                "market": market,
                "analysis": analysis,
                "confidence": confidence,
                "signal_type": signal_type,
                "reasoning": reasoning,
                "potential_profit": potential_profit,
                "drop_size": abs(change_1h),
            })

        # Сортируем: сильнейшее падение * потенциал * уверенность
        candidates.sort(
            key=lambda c: c["drop_size"] * c["potential_profit"] * c["confidence"],
            reverse=True,
        )
        candidates = candidates[:MAX_SIGNALS_PER_CYCLE]

        # Сохраняем
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

            logger.info(
                f"Сигнал: BUY '{market['question'][:50]}...' "
                f"@ {analysis['current_price']*100:.0f}% "
                f"(drop {c['drop_size']*100:.0f}%, profit pot. {c['potential_profit']*100:.0f}%)"
            )

        return signals

    def _is_trash_market(self, question: str) -> bool:
        return any(p in question for p in TRASH_PATTERNS)

    def _calculate_confidence(self, analysis: dict, reasons: list[str],
                               potential_profit: float) -> float:
        score = 0.0

        # Сила падения (чем больше упало — тем вероятнее отскок)
        drop = abs(analysis["change_1h"])
        if drop >= 0.20:
            score += 0.30
        elif drop >= 0.15:
            score += 0.25
        elif drop >= 0.10:
            score += 0.20
        elif drop >= 0.08:
            score += 0.10

        # 6ч тренд был ВВЕРХ до этого (значит падение — откат, не тренд)
        if analysis["change_6h"] > 0 and analysis["change_1h"] < 0:
            score += 0.20  # Был рост, потом резкое падение — хороший знак

        # 24ч тренд вверх — ещё лучше
        if analysis["change_24h"] > 0:
            score += 0.10

        # Объём (высокий объём при падении = паника = отскок вероятнее)
        if analysis["volume_ratio"] >= 3.0:
            score += 0.15
        elif analysis["volume_ratio"] >= 2.0:
            score += 0.10

        # Потенциальный профит
        if potential_profit >= 2.0:
            score += 0.15
        elif potential_profit >= 1.0:
            score += 0.10
        elif potential_profit >= 0.5:
            score += 0.05

        # Несколько причин
        if len(reasons) >= 2:
            score += 0.10

        return min(score, 1.0)

    def _build_reasoning(self, analysis: dict, potential_profit: float) -> str:
        parts = []

        drop = abs(analysis["change_1h"]) * 100
        parts.append(f"Dropped {drop:.0f}% in 1h — potential overreaction")

        if analysis["change_6h"] > 0:
            parts.append(f"was rising before (+{analysis['change_6h']*100:.0f}% in 6h)")

        if analysis["volume_ratio"] >= 2.0:
            parts.append(f"volume {analysis['volume_ratio']:.1f}x avg")

        parts.append(f"potential profit {potential_profit*100:.0f}%")

        return " | ".join(parts)

    async def _gpt_analyze(self, market: dict, analysis: dict) -> str | None:
        if not config.OPENAI_API_KEY:
            return None

        question = market.get("question", "")
        price = analysis["current_price"] * 100
        drop = abs(analysis["change_1h"]) * 100
        profit = ((1.0 - analysis["current_price"]) / analysis["current_price"]) * 100

        prompt = (
            f"Prediction market. 1-2 sentences.\n\n"
            f"Market: {question}\n"
            f"Price dropped to {price:.0f}% (down {drop:.0f}% in 1h)\n"
            f"Potential profit if YES: {profit:.0f}%\n\n"
            f"Is this an overreaction? Good dip-buy opportunity?"
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
        except Exception:
            return None
