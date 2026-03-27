"""Сканер рынков Polymarket — обнаружение и трекинг цен"""

from loguru import logger

import config
import db
from polymarket_client import PolymarketClient


class MarketScanner:
    def __init__(self, client: PolymarketClient):
        self.client = client

    async def scan_markets(self) -> int:
        """
        Сканировать Gamma API, найти рынки по категориям, сохранить в БД.
        Возвращает количество новых/обновлённых рынков.
        """
        logger.info("Запуск сканирования рынков...")
        count = 0
        offset = 0
        limit = 100

        while True:
            events = await self.client.get_events(
                active=True, closed=False, limit=limit, offset=offset
            )
            if not events:
                break

            for event in events:
                category = self._detect_category(event)
                if not category:
                    continue

                markets = event.get("markets", [])
                if not markets:
                    continue

                for market in markets:
                    try:
                        condition_id = market.get("conditionId") or market.get("condition_id", "")
                        if not condition_id:
                            continue

                        # Получение token IDs для YES/NO
                        clob_token_ids = market.get("clobTokenIds", [])
                        outcomes = market.get("outcomes", [])

                        token_yes = ""
                        token_no = ""
                        if len(clob_token_ids) >= 2 and len(outcomes) >= 2:
                            for i, outcome in enumerate(outcomes):
                                if outcome.lower() in ("yes", "да"):
                                    token_yes = clob_token_ids[i]
                                elif outcome.lower() in ("no", "нет"):
                                    token_no = clob_token_ids[i]
                            # Фоллбэк: первый = YES, второй = NO
                            if not token_yes and len(clob_token_ids) >= 1:
                                token_yes = clob_token_ids[0]
                            if not token_no and len(clob_token_ids) >= 2:
                                token_no = clob_token_ids[1]

                        question = market.get("question", event.get("title", "Unknown"))
                        event_slug = event.get("slug", "")
                        end_date = market.get("endDate") or market.get("end_date_iso", "")

                        # URL на Polymarket
                        polymarket_url = ""
                        if event_slug:
                            polymarket_url = f"https://polymarket.com/event/{event_slug}"

                        await db.upsert_market(
                            condition_id=condition_id,
                            token_id_yes=token_yes,
                            token_id_no=token_no,
                            event_slug=event_slug,
                            question=question,
                            category=category,
                            end_date=end_date,
                            polymarket_url=polymarket_url,
                        )
                        count += 1

                    except Exception as e:
                        logger.error(f"Ошибка обработки рынка: {e}")

            if len(events) < limit:
                break
            offset += limit

        logger.info(f"Сканирование завершено: {count} рынков обработано")
        return count

    async def update_prices(self):
        """Обновить цены для всех активных рынков"""
        markets = await db.get_active_markets()
        if not markets:
            return

        # Собираем все token_id_yes для batch запроса
        token_ids = []
        market_map = {}  # token_id -> market
        for m in markets:
            if m["token_id_yes"]:
                token_ids.append(m["token_id_yes"])
                market_map[m["token_id_yes"]] = m

        if not token_ids:
            return

        logger.info(f"Обновление цен для {len(token_ids)} рынков...")
        prices = await self.client.get_prices_batch(token_ids)

        saved = 0
        for token_id, price_yes in prices.items():
            market = market_map.get(token_id)
            if market:
                price_no = 1.0 - price_yes if price_yes else 0
                await db.save_price(
                    market_id=market["id"],
                    price_yes=price_yes,
                    price_no=price_no,
                )
                saved += 1

        logger.info(f"Цены обновлены: {saved}/{len(token_ids)}")

    async def get_tracked_markets(self) -> list[dict]:
        """Получить все отслеживаемые рынки с последними ценами"""
        markets = await db.get_active_markets()
        result = []
        for m in markets:
            latest = await db.get_latest_price(m["id"])
            m["latest_price_yes"] = latest["price_yes"] if latest else None
            m["latest_price_no"] = latest["price_no"] if latest else None
            m["price_updated_at"] = latest["recorded_at"] if latest else None
            result.append(m)
        return result

    def _detect_category(self, event: dict) -> str | None:
        """Определить категорию события по ключевым словам"""
        # Собираем текст для анализа
        title = (event.get("title") or "").lower()
        description = (event.get("description") or "").lower()
        slug = (event.get("slug") or "").lower()
        raw_tags = event.get("tags") or []
        tag_parts = []
        for t in raw_tags:
            if isinstance(t, str):
                tag_parts.append(t.lower())
            elif isinstance(t, dict):
                tag_parts.append((t.get("label") or t.get("name") or t.get("slug") or "").lower())
        tags = " ".join(tag_parts)

        text = f"{title} {description} {slug} {tags}"

        for category in config.CATEGORIES:
            keywords = config.CATEGORY_KEYWORDS.get(category, [])
            for keyword in keywords:
                if keyword in text:
                    return category

        return None
