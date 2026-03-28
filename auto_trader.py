"""Автоторговля — умное управление позициями

Логика:
- Тейк-профит +15%: закрыть, зафиксировать прибыль (безубыток)
- Рост +25%: оставить до разрешения (высокий потенциал), включить трейлинг-стоп
- Трейлинг-стоп: запомнить макс цену, если откат -10% от макс → закрыть
- Стоп-лосс -20%: закрыть, зафиксировать убыток
"""

from loguru import logger

import config
import db
from polymarket_client import PolymarketClient
from risk_manager import RiskManager

# Пороги (в долях от цены входа)
TAKE_PROFIT_PCT = 0.15       # +15% → закрыть
HOLD_THRESHOLD_PCT = 0.25    # +25% → оставить с трейлинг-стопом
TRAILING_STOP_PCT = 0.10     # откат 10% от макс → закрыть
STOP_LOSS_PCT = 0.20         # -20% → закрыть


class AutoTrader:
    def __init__(self, client: PolymarketClient, risk_manager: RiskManager):
        self.client = client
        self.risk = risk_manager

    async def check_open_positions(self):
        """Проверить все открытые позиции — тейк-профит, трейлинг-стоп, стоп-лосс"""
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
                max_price = trade.get("max_price") or entry_price

                if entry_price <= 0:
                    continue

                # Обновляем макс цену
                if current_price > max_price:
                    max_price = current_price
                    await db.update_trade_max_price(trade["id"], max_price)

                # Считаем % изменения от входа
                gain_pct = (current_price - entry_price) / entry_price

                # === СТОП-ЛОСС: -20% от входа ===
                if gain_pct <= -STOP_LOSS_PCT:
                    logger.warning(
                        f"STOP-LOSS #{trade['id']}: {entry_price:.4f} → {current_price:.4f} ({gain_pct*100:+.1f}%)"
                    )
                    await self._close_trade_for_user(trade, current_price, "stop_loss")
                    continue

                # === ТРЕЙЛИНГ-СТОП: рост был >25%, откат 10% от макс ===
                max_gain = (max_price - entry_price) / entry_price
                if max_gain >= HOLD_THRESHOLD_PCT:
                    # Позиция в режиме "hold" — проверяем откат от макс
                    drawdown = (max_price - current_price) / max_price if max_price > 0 else 0
                    if drawdown >= TRAILING_STOP_PCT:
                        pnl_pct = gain_pct * 100
                        logger.info(
                            f"TRAILING-STOP #{trade['id']}: peak {max_price:.4f}, "
                            f"now {current_price:.4f} (drawdown {drawdown*100:.1f}%), P&L {pnl_pct:+.1f}%"
                        )
                        await self._close_trade_for_user(trade, current_price, "trailing_stop")
                        continue
                    # Иначе — держим, не трогаем
                    continue

                # === ТЕЙК-ПРОФИТ: +15% но ещё не +25% → фиксируем ===
                if gain_pct >= TAKE_PROFIT_PCT:
                    logger.info(
                        f"TAKE-PROFIT #{trade['id']}: {entry_price:.4f} → {current_price:.4f} ({gain_pct*100:+.1f}%)"
                    )
                    await self._close_trade_for_user(trade, current_price, "take_profit")
                    continue

            except Exception as e:
                logger.error(f"Ошибка проверки позиции #{trade['id']}: {e}")

    async def _close_trade_for_user(self, trade: dict, current_price: float, reason: str):
        """Закрыть позицию от имени юзера"""
        user_id = trade.get("user_id")
        if not user_id:
            # Просто обновляем статус в БД (нет реального ордера)
            shares = trade["size_usdc"] / trade["price"] if trade["price"] > 0 else 0
            pnl = (current_price - trade["price"]) * shares
            await db.update_trade_status(trade["id"], status="closed", pnl=pnl)
            logger.info(f"Trade #{trade['id']} закрыт ({reason}): P&L=${pnl:+.2f}")
            return

        users = await db.get_connected_users()
        user = next((u for u in users if u["id"] == user_id), None)
        if not user:
            logger.error(f"Юзер #{user_id} не найден")
            return

        if user.get("private_key"):
            user_clob = await self.client.get_user_client(user["telegram_id"], private_key=user["private_key"])
        else:
            user_clob = await self.client.get_user_client(
                user["telegram_id"], api_key=user["api_key"],
                api_secret=user["api_secret"], api_passphrase=user["api_passphrase"],
            )
        if not user_clob:
            logger.error(f"CLOB недоступен для юзера #{user_id}")
            return

        shares = trade["size_usdc"] / trade["price"] if trade["price"] > 0 else 0
        result = await user_clob.place_order(trade["token_id"], "SELL", shares, current_price)

        pnl = (current_price - trade["price"]) * shares

        if result:
            await db.update_trade_status(trade["id"], status="closed", pnl=pnl)
            logger.info(f"Trade #{trade['id']} закрыт ({reason}): P&L=${pnl:+.2f}")

            # Уведомляем юзера
            try:
                from telegram import Bot
                bot = Bot(token=config.TELEGRAM_BOT_TOKEN)

                reason_labels = {
                    "take_profit": "✅ Тейк-профит +15%",
                    "trailing_stop": "📊 Трейлинг-стоп",
                    "stop_loss": "🛑 Стоп-лосс",
                }
                reason_text = reason_labels.get(reason, reason)
                pnl_emoji = "🟢" if pnl >= 0 else "🔴"

                q = trade.get("question", "")[:50]
                text = (
                    f"{reason_text}\n\n"
                    f"📋 {q}\n"
                    f"💰 Вход: {trade['price']:.4f} → Выход: {current_price:.4f}\n"
                    f"{pnl_emoji} P&L: <b>${pnl:+.2f}</b>"
                )
                await bot.send_message(
                    chat_id=user["telegram_id"], text=text, parse_mode="HTML",
                )
            except Exception:
                pass
        else:
            logger.error(f"Не удалось закрыть trade #{trade['id']}")
