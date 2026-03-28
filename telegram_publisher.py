"""Публикация сообщений и графиков в Telegram-канал"""

from telegram import Bot, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger

import config


class TelegramPublisher:
    def __init__(self, bot: Bot, channel_id: str | None = None):
        self.bot = bot
        self.channel_id = channel_id or config.TELEGRAM_CHANNEL_ID

    async def send_text(self, text: str, reply_to: int | None = None) -> int | None:
        """Отправить текст в канал"""
        try:
            msg = await self.bot.send_message(
                chat_id=self.channel_id,
                text=text,
                parse_mode="HTML",
                reply_to_message_id=reply_to,
                disable_web_page_preview=True,
            )
            return msg.message_id
        except Exception as e:
            logger.error(f"Ошибка отправки текста: {e}")
            return None

    async def send_photo(self, photo_path: str, caption: str = "",
                         reply_to: int | None = None) -> int | None:
        """Отправить фото с подписью"""
        try:
            with open(photo_path, "rb") as f:
                msg = await self.bot.send_photo(
                    chat_id=self.channel_id,
                    photo=InputFile(f),
                    caption=caption,
                    parse_mode="HTML" if caption else None,
                    reply_to_message_id=reply_to,
                )
            return msg.message_id
        except Exception as e:
            logger.error(f"Ошибка отправки фото {photo_path}: {e}")
            return None

    async def send_text_with_button(self, text: str, button_text: str, button_url: str,
                                    reply_to: int | None = None) -> int | None:
        """Отправить текст с inline-кнопкой"""
        try:
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton(button_text, url=button_url)]]
            )
            msg = await self.bot.send_message(
                chat_id=self.channel_id,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
                reply_to_message_id=reply_to,
                disable_web_page_preview=True,
            )
            return msg.message_id
        except Exception as e:
            logger.error(f"Ошибка отправки текста с кнопкой: {e}")
            return None

    async def notify_admin(self, text: str):
        """Отправить уведомление админу"""
        if not config.ADMIN_TELEGRAM_ID:
            return
        try:
            await self.bot.send_message(
                chat_id=config.ADMIN_TELEGRAM_ID,
                text=text,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Ошибка уведомления админу: {e}")

    async def send_signal(self, signal: dict, chart_path: str | None = None) -> int | None:
        """Опубликовать сигнал Polymarket с графиком"""
        direction = signal["direction"]
        confidence = signal.get("confidence", 0)
        prob = signal.get("probability_at_signal", 0)
        change = signal.get("probability_change", 0)
        question = signal.get("question", "Unknown")
        category = signal.get("category", "")
        reasoning = signal.get("reasoning", "")
        url = signal.get("polymarket_url", "")

        # Уровень уверенности
        if confidence >= 0.8:
            conf_text = "HIGH"
        elif confidence >= 0.5:
            conf_text = "MEDIUM"
        else:
            conf_text = "LOW"

        # Направление изменения
        change_pct = change * 100
        prob_pct = prob * 100
        prev_pct = prob_pct - change_pct

        if change > 0:
            trend = f"📈 YES: {prev_pct:.0f}% → {prob_pct:.0f}% (+{change_pct:.1f}%)"
        else:
            trend = f"📉 YES: {prev_pct:.0f}% → {prob_pct:.0f}% ({change_pct:.1f}%)"

        signal_type = signal.get("signal_type", "")
        type_labels = {
            "probability_shift": "Probability Shift",
            "volume_spike": "Volume Spike",
            "value_bet": "Value Bet",
            "gpt_analysis": "AI Analysis",
            "contrarian_dip": "Dip Buy",
            "contrarian_volume": "Dip Buy + Volume",
        }
        type_text = type_labels.get(signal_type, signal_type)

        # Потенциальный профит
        potential = ((1.0 - prob) / prob * 100) if prob > 0 else 0

        text = (
            f"📊 <b>Polymarket Signal</b>\n\n"
            f"❓ <b>{question}</b>\n"
            f"{trend}\n\n"
            f"💡 <b>BUY YES</b> @ {prob:.2f}\n"
            f"💰 Potential profit: <b>+{potential:.0f}%</b>\n"
            f"🎯 Confidence: <b>{conf_text}</b> ({confidence:.2f})\n"
            f"📋 Type: {type_text}\n"
        )

        if reasoning:
            text += f"\n💬 {reasoning}\n"

        if category:
            cat_emoji = "🏛" if category == "politics" else "📊"
            text += f"\n{cat_emoji} #{category}"

        # Отправка с графиком или без
        msg_id = None
        if chart_path:
            if url:
                msg_id = await self.send_photo_with_button(
                    chart_path, text, "Open on Polymarket", url
                )
            else:
                msg_id = await self.send_photo(chart_path, caption=text)
        else:
            if url:
                msg_id = await self.send_text_with_button(text, "Open on Polymarket", url)
            else:
                msg_id = await self.send_text(text)

        return msg_id

    async def send_photo_with_button(self, photo_path: str, caption: str,
                                     button_text: str, button_url: str) -> int | None:
        """Отправить фото с кнопкой"""
        try:
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton(button_text, url=button_url)]]
            )
            with open(photo_path, "rb") as f:
                msg = await self.bot.send_photo(
                    chat_id=self.channel_id,
                    photo=InputFile(f),
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            return msg.message_id
        except Exception as e:
            logger.error(f"Ошибка отправки фото с кнопкой: {e}")
            return None

    async def send_daily_summary(self, summary: dict) -> int | None:
        """Дневной отчёт"""
        markets = summary.get("tracked_markets", 0)
        signals_today = summary.get("signals_today", 0)
        trades_today = summary.get("trades_today", 0)
        pnl = summary.get("pnl", 0)
        wins = summary.get("wins", 0)
        losses = summary.get("losses", 0)
        open_pos = summary.get("open_positions", 0)
        win_rate = summary.get("win_rate", 0)

        pnl_emoji = "🟢" if pnl >= 0 else "🔴"

        text = (
            f"📊 <b>Daily Summary</b>\n\n"
            f"📡 Markets tracked: {markets}\n"
            f"📋 Signals generated: {signals_today}\n"
            f"💰 Trades executed: {trades_today}\n\n"
            f"{pnl_emoji} <b>P&L: ${pnl:+.2f}</b>\n"
            f"✅ Wins: {wins} | ❌ Losses: {losses}\n"
            f"📊 Win rate: {win_rate:.0f}%\n"
            f"📂 Open positions: {open_pos}\n"
        )
        return await self.send_text(text)

    async def send_portfolio_update(self, portfolio: dict) -> int | None:
        """Обновление портфеля"""
        realized = portfolio.get("realized_pnl", 0)
        open_pos = portfolio.get("open_positions", 0)
        invested = portfolio.get("total_invested", 0)
        wins = portfolio.get("wins", 0)
        losses = portfolio.get("losses", 0)
        win_rate = portfolio.get("win_rate", 0)

        pnl_emoji = "🟢" if realized >= 0 else "🔴"

        text = (
            f"💼 <b>Portfolio Update</b>\n\n"
            f"{pnl_emoji} Realized P&L: <b>${realized:+.2f}</b>\n"
            f"📂 Open: {open_pos} (${invested:.2f} invested)\n"
            f"✅ {wins}W / ❌ {losses}L ({win_rate:.0f}%)\n"
        )
        return await self.send_text(text)

    async def check_bot_is_admin(self) -> bool:
        """Проверить что бот — админ канала"""
        try:
            bot_info = await self.bot.get_me()
            member = await self.bot.get_chat_member(self.channel_id, bot_info.id)
            is_admin = member.status in ("administrator", "creator")
            if not is_admin:
                logger.error(f"Бот НЕ является админом канала {self.channel_id}!")
            return is_admin
        except Exception as e:
            logger.error(f"Не удалось проверить права бота: {e}")
            return False
