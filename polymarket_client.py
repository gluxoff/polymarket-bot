"""Async обёртка для Polymarket API (Gamma + CLOB)"""

import asyncio
import time
from functools import partial

import aiohttp
from loguru import logger

import config


class RateLimiter:
    """Token bucket rate limiter"""

    def __init__(self, max_tokens: int, refill_period: float):
        self._max = max_tokens
        self._tokens = max_tokens
        self._refill_period = refill_period
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            if elapsed >= self._refill_period:
                self._tokens = self._max
                self._last_refill = now
            if self._tokens <= 0:
                wait = self._refill_period - elapsed
                await asyncio.sleep(wait)
                self._tokens = self._max
                self._last_refill = time.monotonic()
            self._tokens -= 1


class PolymarketClient:
    """Клиент для Polymarket Gamma API и CLOB API"""

    def __init__(self):
        self._http: aiohttp.ClientSession | None = None
        self._clob_client = None  # py-clob-client (sync)
        self._price_limiter = RateLimiter(max_tokens=100, refill_period=1.0)
        self._order_limiter = RateLimiter(max_tokens=50, refill_period=1.0)

    async def init(self):
        """Инициализация HTTP-сессии и CLOB клиента"""
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
        )

        # Инициализация py-clob-client если есть приватный ключ
        if config.POLYMARKET_PRIVATE_KEY:
            try:
                loop = asyncio.get_event_loop()
                self._clob_client = await loop.run_in_executor(
                    None, self._init_clob_client
                )
                logger.info("CLOB клиент инициализирован (торговля доступна)")
            except Exception as e:
                logger.warning(f"CLOB клиент не инициализирован: {e}")
                logger.info("Бот будет работать в режиме только чтение")
        else:
            logger.info("POLYMARKET_PRIVATE_KEY не задан — режим только чтение")

    def _init_clob_client(self):
        """Инициализация py-clob-client (синхронно, вызывается в executor)"""
        from py_clob_client.client import ClobClient

        client = ClobClient(
            config.CLOB_API_URL,
            key=config.POLYMARKET_PRIVATE_KEY,
            chain_id=config.POLYMARKET_CHAIN_ID,
        )
        # Получение/создание L2 API credentials
        client.set_api_creds(client.create_or_derive_api_creds())
        return client

    async def close(self):
        """Закрыть HTTP-сессию"""
        if self._http and not self._http.closed:
            await self._http.close()

    # ── Gamma API (чтение рынков, без авторизации) ─────────────

    async def get_events(
        self,
        active: bool = True,
        closed: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Получить список событий с Gamma API"""
        params = {"limit": limit, "offset": offset}
        if active:
            params["active"] = "true"
        if not closed:
            params["closed"] = "false"

        try:
            async with self._http.get(
                f"{config.GAMMA_API_URL}/events", params=params
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.error(f"Gamma /events: HTTP {resp.status}")
                return []
        except Exception as e:
            logger.error(f"Gamma /events ошибка: {e}")
            return []

    async def get_markets(
        self, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        """Получить список рынков с Gamma API"""
        params = {"limit": limit, "offset": offset}
        try:
            async with self._http.get(
                f"{config.GAMMA_API_URL}/markets", params=params
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.error(f"Gamma /markets: HTTP {resp.status}")
                return []
        except Exception as e:
            logger.error(f"Gamma /markets ошибка: {e}")
            return []

    async def get_event_by_id(self, event_id: str) -> dict | None:
        """Получить событие по ID"""
        try:
            async with self._http.get(
                f"{config.GAMMA_API_URL}/events/{event_id}"
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        except Exception as e:
            logger.error(f"Gamma /events/{event_id} ошибка: {e}")
            return None

    # ── CLOB API (чтение цен, без авторизации) ─────────────────

    async def get_price(self, token_id: str) -> float | None:
        """Получить текущую цену (вероятность 0-1) для токена"""
        await self._price_limiter.acquire()
        try:
            async with self._http.get(
                f"{config.CLOB_API_URL}/price",
                params={"token_id": token_id},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data.get("price", 0))
                return None
        except Exception as e:
            logger.error(f"CLOB /price ошибка: {e}")
            return None

    async def get_midpoint(self, token_id: str) -> float | None:
        """Получить midpoint цену для токена"""
        await self._price_limiter.acquire()
        try:
            async with self._http.get(
                f"{config.CLOB_API_URL}/midpoint",
                params={"token_id": token_id},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data.get("mid", 0))
                return None
        except Exception as e:
            logger.error(f"CLOB /midpoint ошибка: {e}")
            return None

    async def get_order_book(self, token_id: str) -> dict | None:
        """Получить книгу ордеров"""
        await self._price_limiter.acquire()
        try:
            async with self._http.get(
                f"{config.CLOB_API_URL}/book",
                params={"token_id": token_id},
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        except Exception as e:
            logger.error(f"CLOB /book ошибка: {e}")
            return None

    async def get_prices_batch(self, token_ids: list[str]) -> dict[str, float]:
        """Получить цены для нескольких токенов параллельно"""
        results = {}
        tasks = []
        for tid in token_ids:
            tasks.append(self._get_price_safe(tid))

        prices = await asyncio.gather(*tasks)
        for tid, price in zip(token_ids, prices):
            if price is not None:
                results[tid] = price
        return results

    async def _get_price_safe(self, token_id: str) -> float | None:
        """Безопасное получение цены (не бросает исключений)"""
        try:
            return await self.get_price(token_id)
        except Exception:
            return None

    # ── CLOB API (торговля, требует авторизации) ───────────────

    @property
    def can_trade(self) -> bool:
        """Доступна ли торговля"""
        return self._clob_client is not None

    async def place_order(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
    ) -> dict | None:
        """Разместить ордер на CLOB. side='BUY' или 'SELL'"""
        if not self._clob_client:
            logger.error("Торговля недоступна — CLOB клиент не инициализирован")
            return None

        await self._order_limiter.acquire()
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                partial(
                    self._place_order_sync,
                    token_id=token_id,
                    side=side,
                    size=size,
                    price=price,
                ),
            )
            logger.info(f"Ордер размещён: {side} {size} @ {price} (token={token_id[:16]}...)")
            return result
        except Exception as e:
            logger.error(f"Ошибка размещения ордера: {e}")
            return None

    def _place_order_sync(self, token_id: str, side: str, size: float, price: float) -> dict:
        """Синхронное размещение ордера через py-clob-client"""
        from py_clob_client.order_builder.constants import BUY, SELL

        order_side = BUY if side.upper() == "BUY" else SELL
        order = self._clob_client.create_order(
            token_id=token_id,
            price=price,
            size=size,
            side=order_side,
        )
        return self._clob_client.post_order(order)

    async def cancel_order(self, order_id: str) -> dict | None:
        """Отменить ордер"""
        if not self._clob_client:
            return None

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                partial(self._clob_client.cancel, order_id=order_id),
            )
            logger.info(f"Ордер отменён: {order_id}")
            return result
        except Exception as e:
            logger.error(f"Ошибка отмены ордера {order_id}: {e}")
            return None

    async def get_open_orders(self) -> list[dict]:
        """Получить открытые ордера пользователя"""
        if not self._clob_client:
            return []

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, self._clob_client.get_orders)
            return result if isinstance(result, list) else []
        except Exception as e:
            logger.error(f"Ошибка получения ордеров: {e}")
            return []
