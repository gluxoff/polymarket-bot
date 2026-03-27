"""Риск-менеджмент — лимиты, стоп-лоссы, размер позиций"""

from loguru import logger

import config
import db


class RiskManager:
    async def can_trade(self, size_usdc: float) -> tuple[bool, str]:
        """Проверить все лимиты. Возвращает (разрешено, причина)."""
        # Проверка дневного лимита потерь
        ok, remaining = await self.check_daily_loss_limit()
        if not ok:
            return False, f"Daily loss limit reached (${config.MAX_DAILY_LOSS_USDC})"

        if size_usdc > remaining:
            return False, f"Trade size ${size_usdc} exceeds remaining budget ${remaining:.2f}"

        # Проверка количества позиций
        ok, current = await self.check_position_count()
        if not ok:
            return False, f"Max positions ({config.MAX_POSITIONS}) reached"

        # Проверка размера ставки
        if size_usdc > config.MAX_BET_SIZE_USDC:
            return False, f"Trade size ${size_usdc} exceeds max ${config.MAX_BET_SIZE_USDC}"

        if size_usdc <= 0:
            return False, "Trade size must be positive"

        return True, "OK"

    async def calculate_position_size(self, confidence: float) -> float:
        """
        Расчёт размера позиции на основе уверенности.
        Упрощённый Kelly criterion: size = max_bet * confidence * 0.5
        """
        # Базовый размер: пропорционален уверенности
        base_size = config.MAX_BET_SIZE_USDC * confidence * 0.5

        # Минимум $1
        size = max(base_size, 1.0)

        # Ограничение сверху
        size = min(size, config.MAX_BET_SIZE_USDC)

        # Проверяем оставшийся бюджет
        _, remaining = await self.check_daily_loss_limit()
        size = min(size, remaining)

        return round(size, 2)

    async def check_daily_loss_limit(self) -> tuple[bool, float]:
        """Проверка дневного лимита. Возвращает (в_пределах, остаток)."""
        today_pnl = await db.get_today_pnl()
        total_loss = abs(min(today_pnl.get("total_pnl", 0), 0))
        remaining = config.MAX_DAILY_LOSS_USDC - total_loss
        return remaining > 0, max(remaining, 0)

    async def check_position_count(self) -> tuple[bool, int]:
        """Проверка количества открытых позиций."""
        open_trades = await db.get_open_trades()
        current = len(open_trades)
        return current < config.MAX_POSITIONS, current

    def check_stop_loss(self, entry_price: float, current_price: float, side: str) -> bool:
        """
        Проверить стоп-лосс.
        Возвращает True если стоп-лосс сработал.
        """
        if side.upper() == "BUY":
            # Купили YES — потеря если цена упала
            loss_pct = (entry_price - current_price) / entry_price if entry_price > 0 else 0
        else:
            # Купили NO — потеря если цена выросла (для YES-токена)
            loss_pct = (current_price - entry_price) / (1 - entry_price) if entry_price < 1 else 0

        return loss_pct >= config.STOP_LOSS_PERCENT
