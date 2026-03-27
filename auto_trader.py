"""Автоторговля — размещение и управление ставками на Polymarket"""

from loguru import logger

import config
import db
from polymarket_client import PolymarketClient
from risk_manager import RiskManager


class AutoTrader:
    def __init__(self, client: PolymarketClient, risk_manager: RiskManager):
        self.client = client
        self.risk = risk_manager

    async def execute_signal(self, signal: dict) -> int | None:
        """
        Выполнить сделку на основе сигнала.
        Возвращает trade_id или None.
        """
        if not config.AUTO_TRADE_ENABLED:
            return None

        if not self.client.can_trade:
            logger.warning("Торговля недоступна — CLOB клиент не инициализирован")
            return None

        direction = signal["direction"]
        confidence = signal.get("confidence", 0)
        market_id = signal["market_id"]

        # Определяем токен
        if direction == "BUY":
            token_id = signal.get("token_id_yes", "")
        else:
            token_id = signal.get("token_id_no", "")

        if not token_id:
            logger.error(f"Нет token_id для {direction}")
            return None

        # Размер позиции
        size = await self.risk.calculate_position_size(confidence)

        # Проверка рисков
        can, reason = await self.risk.can_trade(size)
        if not can:
            logger.info(f"Сделка отклонена: {reason}")
            return None

        # Получаем текущую цену
        price = await self.client.get_midpoint(token_id)
        if not price or price <= 0 or price >= 1:
            logger.error(f"Невалидная цена: {price}")
            return None

        # Количество акций
        shares = size / price

        # Размещаем ордер
        logger.info(
            f"Размещение: BUY {shares:.2f} shares @ {price:.4f} "
            f"(${size:.2f}) [{signal.get('question', '')[:40]}]"
        )

        result = await self.client.place_order(
            token_id=token_id,
            side="BUY",
            size=shares,
            price=price,
        )

        if not result:
            logger.error("Ордер не размещён")
            return None

        # Сохраняем в БД
        order_id = result.get("orderID", result.get("id", ""))
        trade_id = await db.save_trade(
            signal_id=signal.get("id"),
            market_id=market_id,
            token_id=token_id,
            side="BUY",
            size_usdc=size,
            price=price,
            order_id=order_id,
            status="filled",
        )

        logger.info(f"Сделка #{trade_id} сохранена")
        return trade_id

    async def manual_trade(self, market: dict, side: str, amount: float) -> int | None:
        """Ручная ставка через команду /trade"""
        if not self.client.can_trade:
            return None

        token_id = market["token_id_yes"] if side == "YES" else market["token_id_no"]
        if not token_id:
            return None

        price = await self.client.get_midpoint(token_id)
        if not price or price <= 0 or price >= 1:
            return None

        shares = amount / price

        result = await self.client.place_order(
            token_id=token_id,
            side="BUY",
            size=shares,
            price=price,
        )

        if not result:
            return None

        order_id = result.get("orderID", result.get("id", ""))
        trade_id = await db.save_trade(
            signal_id=None,
            market_id=market["id"],
            token_id=token_id,
            side="BUY",
            size_usdc=amount,
            price=price,
            order_id=order_id,
            status="filled",
        )
        return trade_id

    async def check_open_positions(self):
        """Проверить открытые позиции — стоп-лосс и обновление цен"""
        open_trades = await db.get_open_trades()
        if not open_trades:
            return

        for trade in open_trades:
            try:
                token_id = trade["token_id"]
                current_price = await self.client.get_midpoint(token_id)
                if current_price is None:
                    continue

                entry_price = trade["price"]

                # Проверка стоп-лосса
                if self.risk.check_stop_loss(entry_price, current_price, trade["side"]):
                    logger.warning(
                        f"Stop-loss на позиции #{trade['id']}: "
                        f"entry={entry_price:.4f}, current={current_price:.4f}"
                    )
                    await self.close_position(trade["id"], reason="stop_loss")

            except Exception as e:
                logger.error(f"Ошибка проверки позиции #{trade['id']}: {e}")

    async def close_position(self, trade_id: int, reason: str = "manual") -> bool:
        """Закрыть позицию — продать токены обратно"""
        trades = await db.get_open_trades()
        trade = next((t for t in trades if t["id"] == trade_id), None)
        if not trade:
            logger.error(f"Позиция #{trade_id} не найдена")
            return False

        token_id = trade["token_id"]
        current_price = await self.client.get_midpoint(token_id)
        if current_price is None:
            logger.error(f"Не удалось получить цену для закрытия #{trade_id}")
            return False

        # Размер в акциях (приблизительно)
        shares = trade["size_usdc"] / trade["price"]

        result = await self.client.place_order(
            token_id=token_id,
            side="SELL",
            size=shares,
            price=current_price,
        )

        if not result:
            logger.error(f"Не удалось закрыть позицию #{trade_id}")
            return False

        # P&L
        pnl = (current_price - trade["price"]) * shares
        await db.update_trade_status(trade_id, status="closed", pnl=pnl)

        logger.info(
            f"Позиция #{trade_id} закрыта ({reason}): "
            f"P&L=${pnl:+.2f}"
        )
        return True
