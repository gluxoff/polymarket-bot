"""Автоторговля — управление позициями, проверка статусов, уведомления"""

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
        """Проверить все открытые позиции — статус ордеров, тейк-профит, стоп-лосс"""
        open_trades = await db.get_open_trades()
        if not open_trades:
            return

        for trade in open_trades:
            try:
                order_id = trade.get("order_id", "")
                token_id = trade["token_id"]
                user_id = trade.get("user_id")

                # Проверяем статус ордера на Polymarket
                if order_id and trade["status"] == "pending":
                    await self._check_order_status(trade)
                    continue

                # Для исполненных ордеров — проверяем тейк-профит/стоп-лосс
                if trade["status"] == "filled":
                    current_price = await self.client.get_midpoint(token_id)
                    if not current_price:
                        current_price = await self.client.get_price(token_id)
                    if not current_price:
                        latest = await db.get_latest_price(trade["market_id"])
                        if latest:
                            current_price = latest["price_yes"]
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

                    gain_pct = (current_price - entry_price) / entry_price

                    # СТОП-ЛОСС
                    if gain_pct <= -STOP_LOSS_PCT:
                        await self._close_and_notify(trade, current_price, "stop_loss", gain_pct)
                        continue

                    # ТРЕЙЛИНГ-СТОП
                    max_gain = (max_price - entry_price) / entry_price
                    if max_gain >= HOLD_THRESHOLD_PCT:
                        drawdown = (max_price - current_price) / max_price if max_price > 0 else 0
                        if drawdown >= TRAILING_STOP_PCT:
                            await self._close_and_notify(trade, current_price, "trailing_stop", gain_pct)
                            continue
                        continue

                    # ТЕЙК-ПРОФИТ
                    if gain_pct >= TAKE_PROFIT_PCT:
                        await self._close_and_notify(trade, current_price, "take_profit", gain_pct)
                        continue

            except Exception as e:
                logger.error(f"Ошибка проверки позиции #{trade['id']}: {e}")

    async def _check_order_status(self, trade: dict):
        """Проверить статус ордера на Polymarket и обновить в БД"""
        order_id = trade["order_id"]
        user_id = trade.get("user_id")

        if not user_id:
            return

        # Получаем CLOB клиент
        user_clob = await self._get_user_clob(user_id)
        if not user_clob:
            return

        order_info = await user_clob.get_order(order_id)
        if not order_info:
            return

        status = order_info.get("status", "").lower()
        size_matched = float(order_info.get("size_matched", 0) or 0)

        if status == "matched" or size_matched > 0:
            # Ордер исполнен
            await db.update_trade_status(trade["id"], "filled")
            await self._notify_user(user_id, trade,
                f"✅ <b>Ордер исполнен</b>\n\n"
                f"📋 {trade.get('question', '')[:50]}\n"
                f"💰 BUY @ {trade['price']:.2f} | ${trade['size_usdc']:.2f}"
            )
            logger.info(f"Ордер #{trade['id']} исполнен: {order_id[:20]}...")

        elif status in ("cancelled", "expired", "dead"):
            # Ордер отменён/истёк
            await db.update_trade_status(trade["id"], "cancelled")
            await self._notify_user(user_id, trade,
                f"⏳ <b>Ордер истёк</b>\n\n"
                f"📋 {trade.get('question', '')[:50]}\n"
                f"💰 ${trade['size_usdc']:.2f} — деньги вернулись"
            )
            logger.info(f"Ордер #{trade['id']} отменён: {status}")

    async def _close_and_notify(self, trade: dict, current_price: float, reason: str, gain_pct: float):
        """Закрыть позицию и уведомить юзера"""
        user_id = trade.get("user_id")
        shares = trade["size_usdc"] / trade["price"] if trade["price"] > 0 else 0
        pnl = (current_price - trade["price"]) * shares

        # Пробуем продать на Polymarket
        if user_id:
            user_clob = await self._get_user_clob(user_id)
            if user_clob:
                sell_price = round(current_price, 2)
                if sell_price > 0 and shares >= 5:
                    try:
                        result = await user_clob.place_order(trade["token_id"], "SELL", shares, sell_price)
                        if result:
                            logger.info(f"Sell order placed for #{trade['id']}")
                    except Exception as e:
                        logger.error(f"Sell error: {e}")

        await db.update_trade_status(trade["id"], "closed", pnl=pnl)

        reason_labels = {
            "take_profit": "✅ Тейк-профит",
            "trailing_stop": "📊 Трейлинг-стоп",
            "stop_loss": "🛑 Стоп-лосс",
        }
        reason_text = reason_labels.get(reason, reason)
        pnl_emoji = "🟢" if pnl >= 0 else "🔴"

        msg = (
            f"{reason_text}\n\n"
            f"📋 {trade.get('question', '')[:50]}\n"
            f"💰 Вход: {trade['price']:.2f} → Выход: {current_price:.2f} ({gain_pct*100:+.1f}%)\n"
            f"{pnl_emoji} P&L: <b>${pnl:+.2f}</b>"
        )

        if user_id:
            await self._notify_user(user_id, trade, msg)

        logger.info(f"Trade #{trade['id']} закрыт ({reason}): P&L=${pnl:+.2f}")

    async def _get_user_clob(self, user_id: int):
        """Получить CLOB клиент для юзера"""
        users = await db.get_connected_users()
        user = next((u for u in users if u["id"] == user_id), None)
        if not user:
            return None

        if user.get("private_key"):
            return await self.client.get_user_client(user["telegram_id"], private_key=user["private_key"])
        elif user.get("api_key"):
            return await self.client.get_user_client(
                user["telegram_id"], api_key=user["api_key"],
                api_secret=user["api_secret"], api_passphrase=user["api_passphrase"],
            )
        return None

    async def _notify_user(self, user_id: int, trade: dict, text: str):
        """Отправить уведомление юзеру в Telegram"""
        users = await db.get_connected_users()
        user = next((u for u in users if u["id"] == user_id), None)
        if not user:
            return

        try:
            from telegram import Bot
            bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
            await bot.send_message(
                chat_id=user["telegram_id"],
                text=text,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Уведомление не отправлено: {e}")
