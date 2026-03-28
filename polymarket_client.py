"""Async обёртка для Polymarket API (Gamma + CLOB) — multi-user"""

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


class UserClobClient:
    """CLOB клиент для конкретного юзера (по приватному ключу или API ключам)"""

    def __init__(self, api_key: str = "", api_secret: str = "", api_passphrase: str = "",
                 private_key: str = ""):
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self.private_key = private_key
        self._client = None

    async def init(self) -> bool:
        try:
            loop = asyncio.get_event_loop()
            self._client = await loop.run_in_executor(None, self._init_sync)
            return True
        except Exception as e:
            logger.error(f"Ошибка инициализации CLOB клиента: {e}")
            return False

    def _init_sync(self):
        from py_clob_client.client import ClobClient

        if self.private_key:
            # Подключение по приватному ключу — автогенерация API credentials
            client = ClobClient(
                config.CLOB_API_URL,
                key=self.private_key,
                chain_id=config.POLYMARKET_CHAIN_ID,
            )
            client.set_api_creds(client.create_or_derive_api_creds())
            return client
        else:
            # Подключение по готовым API ключам
            from py_clob_client.clob_types import ApiCreds
            client = ClobClient(
                config.CLOB_API_URL,
                chain_id=config.POLYMARKET_CHAIN_ID,
            )
            creds = ApiCreds(
                api_key=self.api_key,
                api_secret=self.api_secret,
                api_passphrase=self.api_passphrase,
            )
            client.set_api_creds(creds)
            return client

    @property
    def is_ready(self) -> bool:
        return self._client is not None

    async def place_order(self, token_id: str, side: str, size: float, price: float) -> dict | None:
        if not self._client:
            return None
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None,
                partial(self._place_order_sync, token_id, side, size, price),
            )
        except Exception as e:
            logger.error(f"Ошибка ордера: {e}")
            return None

    def _place_order_sync(self, token_id: str, side: str, size: float, price: float) -> dict:
        from py_clob_client.order_builder.constants import BUY, SELL
        order_side = BUY if side.upper() == "BUY" else SELL
        order = self._client.create_order(
            token_id=token_id, price=price, size=size, side=order_side,
        )
        return self._client.post_order(order)

    async def cancel_order(self, order_id: str) -> dict | None:
        if not self._client:
            return None
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, partial(self._client.cancel, order_id=order_id),
            )
        except Exception as e:
            logger.error(f"Ошибка отмены ордера: {e}")
            return None


class PolymarketClient:
    """Основной клиент — чтение рынков/цен (без авторизации)"""

    def __init__(self):
        self._http: aiohttp.ClientSession | None = None
        self._price_limiter = RateLimiter(max_tokens=100, refill_period=1.0)
        self._order_limiter = RateLimiter(max_tokens=50, refill_period=1.0)
        # Кеш per-user CLOB клиентов: telegram_id -> UserClobClient
        self._user_clients: dict[int, UserClobClient] = {}

    async def init(self):
        """Инициализация HTTP-сессии"""
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
        )
        logger.info("Polymarket клиент инициализирован (режим чтения)")

    async def close(self):
        if self._http and not self._http.closed:
            await self._http.close()

    # ── Per-user CLOB клиенты ────────────────────────────────

    async def get_user_client(self, telegram_id: int, api_key: str = "", api_secret: str = "",
                              api_passphrase: str = "", private_key: str = "") -> UserClobClient | None:
        """Получить или создать CLOB клиент для юзера"""
        if telegram_id in self._user_clients:
            client = self._user_clients[telegram_id]
            if client.is_ready:
                return client

        client = UserClobClient(
            api_key=api_key, api_secret=api_secret,
            api_passphrase=api_passphrase, private_key=private_key,
        )
        ok = await client.init()
        if ok:
            self._user_clients[telegram_id] = client
            logger.info(f"CLOB клиент создан для юзера {telegram_id}")
            return client
        return None

    def remove_user_client(self, telegram_id: int):
        """Удалить кеш CLOB клиента юзера"""
        self._user_clients.pop(telegram_id, None)

    # ── Gamma API (чтение рынков, без авторизации) ─────────────

    async def get_events(self, active: bool = True, closed: bool = False,
                         limit: int = 100, offset: int = 0) -> list[dict]:
        params = {"limit": limit, "offset": offset}
        if active:
            params["active"] = "true"
        if not closed:
            params["closed"] = "false"
        try:
            async with self._http.get(f"{config.GAMMA_API_URL}/events", params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.error(f"Gamma /events: HTTP {resp.status}")
                return []
        except Exception as e:
            logger.error(f"Gamma /events ошибка: {e}")
            return []

    async def get_markets(self, limit: int = 100, offset: int = 0) -> list[dict]:
        params = {"limit": limit, "offset": offset}
        try:
            async with self._http.get(f"{config.GAMMA_API_URL}/markets", params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.error(f"Gamma /markets: HTTP {resp.status}")
                return []
        except Exception as e:
            logger.error(f"Gamma /markets ошибка: {e}")
            return []

    async def get_event_by_id(self, event_id: str) -> dict | None:
        try:
            async with self._http.get(f"{config.GAMMA_API_URL}/events/{event_id}") as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        except Exception as e:
            logger.error(f"Gamma /events/{event_id} ошибка: {e}")
            return None

    # ── CLOB API (чтение цен, без авторизации) ─────────────────

    async def get_price(self, token_id: str) -> float | None:
        await self._price_limiter.acquire()
        try:
            async with self._http.get(
                f"{config.CLOB_API_URL}/price", params={"token_id": token_id},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data.get("price", 0))
                return None
        except Exception as e:
            logger.error(f"CLOB /price ошибка: {e}")
            return None

    async def get_midpoint(self, token_id: str) -> float | None:
        await self._price_limiter.acquire()
        try:
            async with self._http.get(
                f"{config.CLOB_API_URL}/midpoint", params={"token_id": token_id},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data.get("mid", 0))
                return None
        except Exception as e:
            logger.error(f"CLOB /midpoint ошибка: {e}")
            return None

    async def get_order_book(self, token_id: str) -> dict | None:
        await self._price_limiter.acquire()
        try:
            async with self._http.get(
                f"{config.CLOB_API_URL}/book", params={"token_id": token_id},
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        except Exception as e:
            logger.error(f"CLOB /book ошибка: {e}")
            return None

    async def get_prices_batch(self, token_ids: list[str], batch_size: int = 20) -> dict[str, float]:
        """Получить цены батчами по batch_size параллельных запросов"""
        results = {}
        for i in range(0, len(token_ids), batch_size):
            batch = token_ids[i:i + batch_size]
            tasks = [self._get_price_safe(tid) for tid in batch]
            prices = await asyncio.gather(*tasks)
            for tid, p in zip(batch, prices):
                if p is not None:
                    results[tid] = p
        return results

    async def _get_price_safe(self, token_id: str) -> float | None:
        try:
            return await self.get_price(token_id)
        except Exception:
            return None
