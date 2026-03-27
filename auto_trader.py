"""Автоторговля — управление позициями для всех юзеров"""

from loguru import logger

import config
import db
from polymarket_client import PolymarketClient
from risk_manager import RiskManager


class AutoTrader:
    def __init__(self, client: PolymarketClient, risk_manager: RiskManager):
        self.client = client
        self.risk = risk_manager

    async def check_open_positions(self):
        """Проверить стоп-лоссы для всех открытых позиций всех юзеров"""
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

                if self.risk.check_stop_loss(entry_price, current_price, trade["side"]):
                    logger.warning(
                        f"Stop-loss trade #{trade['id']}: "
                        f"entry={entry_price:.4f}, current={current_price:.4f}"
                    )
                    await self._close_trade_for_user(trade, current_price, reason="stop_loss")

            except Exception as e:
                logger.error(f"Ошибка проверки позиции #{trade['id']}: {e}")

    async def _close_trade_for_user(self, trade: dict, current_price: float, reason: str):
        """Закрыть позицию от имени юзера"""
        # Получаем юзера
        user_id = trade.get("user_id")
        if not user_id:
            return

        # Ищем юзера в БД для получения API ключей
        from db import get_connected_users
        users = await get_connected_users()
        user = next((u for u in users if u["id"] == user_id), None)
        if not user:
            logger.error(f"Юзер #{user_id} не найден для закрытия trade #{trade['id']}")
            return

        user_clob = await self.client.get_user_client(
            user["telegram_id"], user["api_key"], user["api_secret"], user["api_passphrase"],
        )
        if not user_clob:
            logger.error(f"CLOB клиент недоступен для юзера #{user_id}")
            return

        shares = trade["size_usdc"] / trade["price"] if trade["price"] > 0 else 0
        result = await user_clob.place_order(trade["token_id"], "SELL", shares, current_price)

        if result:
            pnl = (current_price - trade["price"]) * shares
            await db.update_trade_status(trade["id"], status="closed", pnl=pnl)
            logger.info(f"Trade #{trade['id']} закрыт ({reason}): P&L=${pnl:+.2f}")
        else:
            logger.error(f"Не удалось закрыть trade #{trade['id']}")
