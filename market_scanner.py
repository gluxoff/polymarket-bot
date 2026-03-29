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
        Цены берутся сразу из Gamma API (outcomePrices).
        Возвращает количество новых/обновлённых рынков.
        """
        logger.info("Запуск сканирования рынков...")
        count = 0
        prices_saved = 0
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

                        # Token IDs (может быть JSON строкой или списком)
                        import json as _json
                        clob_token_ids = market.get("clobTokenIds", [])
                        if isinstance(clob_token_ids, str):
                            try:
                                clob_token_ids = _json.loads(clob_token_ids)
                            except (ValueError, TypeError):
                                clob_token_ids = []
                        outcomes = market.get("outcomes", [])
                        if isinstance(outcomes, str):
                            try:
                                outcomes = _json.loads(outcomes)
                            except (ValueError, TypeError):
                                outcomes = []

                        token_yes = ""
                        token_no = ""
                        if len(clob_token_ids) >= 2 and len(outcomes) >= 2:
                            for i, outcome in enumerate(outcomes):
                                if isinstance(outcome, str) and outcome.lower() in ("yes", "да"):
                                    token_yes = clob_token_ids[i]
                                elif isinstance(outcome, str) and outcome.lower() in ("no", "нет"):
                                    token_no = clob_token_ids[i]
                            if not token_yes:
                                token_yes = clob_token_ids[0]
                            if not token_no and len(clob_token_ids) >= 2:
                                token_no = clob_token_ids[1]

                        question = market.get("question", event.get("title", "Unknown"))
                        event_slug = event.get("slug", "")
                        end_date = market.get("endDate") or market.get("end_date_iso", "")

                        polymarket_url = ""
                        if event_slug:
                            polymarket_url = f"https://polymarket.com/event/{event_slug}"

                        market_id = await db.upsert_market(
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

                        # Цены из Gamma API (outcomePrices или outcomePrices)
                        price_yes = self._extract_price(market, 0)
                        price_no = self._extract_price(market, 1)

                        if price_yes is not None:
                            if price_no is None:
                                price_no = 1.0 - price_yes
                            volume = float(market.get("volume", 0) or 0)
                            await db.save_price(market_id, price_yes, price_no, volume)
                            prices_saved += 1

                    except Exception as e:
                        logger.error(f"Ошибка обработки рынка: {e}")

            if len(events) < limit:
                break
            offset += limit

        logger.info(f"Сканирование завершено: {count} рынков, {prices_saved} цен сохранено")
        return count

    def _extract_price(self, market: dict, index: int) -> float | None:
        """Извлечь цену из Gamma API ответа"""
        # outcomePrices — строка JSON или список
        outcome_prices = market.get("outcomePrices")
        if outcome_prices:
            if isinstance(outcome_prices, str):
                try:
                    import json
                    outcome_prices = json.loads(outcome_prices)
                except (json.JSONDecodeError, ValueError):
                    pass
            if isinstance(outcome_prices, list) and len(outcome_prices) > index:
                try:
                    return float(outcome_prices[index])
                except (ValueError, TypeError):
                    pass

        # bestAsk / bestBid
        if index == 0:
            for key in ("bestAsk", "bestBid", "lastTradePrice"):
                val = market.get(key)
                if val:
                    try:
                        return float(val)
                    except (ValueError, TypeError):
                        pass

        return None

    async def update_prices(self, max_markets: int = 200):
        """Обновить цены для активных рынков через CLOB API (фоллбэк)"""
        markets = await db.get_active_markets()
        if not markets:
            return

        # Только рынки без цен
        markets_no_price = []
        for m in markets[:max_markets]:
            latest = await db.get_latest_price(m["id"])
            if not latest:
                markets_no_price.append(m)

        if not markets_no_price:
            logger.info("Все рынки имеют цены из Gamma API")
            return

        token_ids = []
        market_map = {}
        for m in markets_no_price[:100]:
            if m["token_id_yes"]:
                token_ids.append(m["token_id_yes"])
                market_map[m["token_id_yes"]] = m

        if not token_ids:
            return

        logger.info(f"CLOB фоллбэк: обновление цен для {len(token_ids)} рынков...")
        prices = await self.client.get_prices_batch(token_ids)

        saved = 0
        for token_id, price_yes in prices.items():
            market = market_map.get(token_id)
            if market:
                await db.save_price(market["id"], price_yes, 1.0 - price_yes)
                saved += 1

        logger.info(f"CLOB фоллбэк: {saved}/{len(token_ids)} цен обновлено")

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
