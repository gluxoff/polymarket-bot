"""Трекинг портфеля — позиции, P&L, статистика"""

from loguru import logger

import db
from polymarket_client import PolymarketClient


class PortfolioTracker:
    def __init__(self, client: PolymarketClient):
        self.client = client

    async def get_portfolio_summary(self) -> dict:
        """Полная сводка портфеля"""
        stats = await db.get_portfolio_stats()
        open_trades = await db.get_open_trades()

        # Подсчёт нереализованного P&L
        unrealized_pnl = 0.0
        for trade in open_trades:
            current_price = await self.client.get_midpoint(trade["token_id"])
            if current_price is not None:
                shares = trade["size_usdc"] / trade["price"]
                unrealized_pnl += (current_price - trade["price"]) * shares

        stats["unrealized_pnl"] = unrealized_pnl
        stats["total_pnl"] = stats["realized_pnl"] + unrealized_pnl
        return stats

    async def get_open_positions_detailed(self) -> list[dict]:
        """Открытые позиции с текущими ценами"""
        open_trades = await db.get_open_trades()
        result = []

        for trade in open_trades:
            current_price = await self.client.get_midpoint(trade["token_id"])
            shares = trade["size_usdc"] / trade["price"] if trade["price"] > 0 else 0

            if current_price is not None:
                unrealized_pnl = (current_price - trade["price"]) * shares
                pnl_pct = ((current_price / trade["price"]) - 1) * 100 if trade["price"] > 0 else 0
            else:
                unrealized_pnl = 0
                pnl_pct = 0
                current_price = trade["price"]

            result.append({
                **trade,
                "current_price": current_price,
                "shares": shares,
                "unrealized_pnl": unrealized_pnl,
                "pnl_pct": pnl_pct,
            })

        return result

    async def update_all_prices(self):
        """Обновить цены всех открытых позиций (для отображения)"""
        positions = await self.get_open_positions_detailed()
        logger.info(f"Обновлены цены для {len(positions)} открытых позиций")
        return positions
