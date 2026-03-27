"""База данных — SQLite хранилище рынков, сигналов и сделок"""

import aiosqlite
from datetime import datetime, timedelta
from loguru import logger

import config


async def init_db():
    """Создание таблиц если не существуют"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS markets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_id TEXT UNIQUE NOT NULL,
                token_id_yes TEXT,
                token_id_no TEXT,
                event_slug TEXT,
                question TEXT NOT NULL,
                category TEXT,
                end_date TEXT,
                polymarket_url TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id INTEGER REFERENCES markets(id),
                price_yes REAL,
                price_no REAL,
                volume REAL DEFAULT 0,
                recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_price_history_market
                ON price_history(market_id, recorded_at);

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id INTEGER REFERENCES markets(id),
                signal_type TEXT NOT NULL,
                direction TEXT NOT NULL,
                confidence REAL DEFAULT 0,
                probability_at_signal REAL,
                probability_change REAL,
                reasoning TEXT,
                published INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id),
                signal_id INTEGER REFERENCES signals(id),
                market_id INTEGER REFERENCES markets(id),
                token_id TEXT NOT NULL,
                side TEXT NOT NULL,
                size_usdc REAL NOT NULL,
                price REAL NOT NULL,
                order_id TEXT,
                status TEXT DEFAULT 'pending',
                pnl REAL DEFAULT 0,
                closed_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                api_key TEXT,
                api_secret TEXT,
                api_passphrase TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS daily_pnl (
                date TEXT PRIMARY KEY,
                total_pnl REAL DEFAULT 0,
                trades_count INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0
            );
        """)
        await db.commit()
    logger.info("БД инициализирована")


# ── Users ────────────────────────────────────────────────────

async def save_user(telegram_id: int, username: str = "") -> int:
    """Зарегистрировать пользователя, вернуть user_id"""
    async with aiosqlite.connect(config.DB_PATH) as conn:
        cursor = await conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cursor.fetchone()
        if row:
            return row[0]
        cursor = await conn.execute(
            "INSERT INTO users (telegram_id, username) VALUES (?, ?)",
            (telegram_id, username),
        )
        await conn.commit()
        return cursor.lastrowid


async def get_user_by_telegram_id(telegram_id: int) -> dict | None:
    """Получить пользователя по Telegram ID"""
    async with aiosqlite.connect(config.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def save_user_api_keys(telegram_id: int, api_key: str, api_secret: str, api_passphrase: str):
    """Сохранить API ключи пользователя"""
    async with aiosqlite.connect(config.DB_PATH) as conn:
        await conn.execute(
            """UPDATE users SET api_key=?, api_secret=?, api_passphrase=?, is_active=1
               WHERE telegram_id=?""",
            (api_key, api_secret, api_passphrase, telegram_id),
        )
        await conn.commit()


async def delete_user_api_keys(telegram_id: int):
    """Удалить API ключи пользователя"""
    async with aiosqlite.connect(config.DB_PATH) as conn:
        await conn.execute(
            "UPDATE users SET api_key=NULL, api_secret=NULL, api_passphrase=NULL WHERE telegram_id=?",
            (telegram_id,),
        )
        await conn.commit()


async def get_connected_users() -> list[dict]:
    """Все пользователи с подключёнными API ключами"""
    async with aiosqlite.connect(config.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM users WHERE api_key IS NOT NULL AND is_active = 1"
        )
        return [dict(row) for row in await cursor.fetchall()]


# ── Markets ──────────────────────────────────────────────────

async def upsert_market(
    condition_id: str,
    token_id_yes: str,
    token_id_no: str,
    event_slug: str,
    question: str,
    category: str,
    end_date: str | None = None,
    polymarket_url: str | None = None,
) -> int:
    """Добавить или обновить рынок. Возвращает market_id."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        # Проверяем существование
        cursor = await db.execute(
            "SELECT id FROM markets WHERE condition_id = ?", (condition_id,)
        )
        row = await cursor.fetchone()
        if row:
            await db.execute(
                """UPDATE markets SET token_id_yes=?, token_id_no=?, event_slug=?,
                   question=?, category=?, end_date=?, polymarket_url=?,
                   is_active=1, updated_at=CURRENT_TIMESTAMP
                   WHERE condition_id=?""",
                (token_id_yes, token_id_no, event_slug, question, category,
                 end_date, polymarket_url, condition_id),
            )
            await db.commit()
            return row[0]
        else:
            cursor = await db.execute(
                """INSERT INTO markets
                   (condition_id, token_id_yes, token_id_no, event_slug,
                    question, category, end_date, polymarket_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (condition_id, token_id_yes, token_id_no, event_slug,
                 question, category, end_date, polymarket_url),
            )
            await db.commit()
            return cursor.lastrowid


async def get_active_markets() -> list[dict]:
    """Получить все активные рынки"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM markets WHERE is_active = 1 ORDER BY updated_at DESC"
        )
        return [dict(row) for row in await cursor.fetchall()]


async def get_market_by_id(market_id: int) -> dict | None:
    """Получить рынок по ID"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM markets WHERE id = ?", (market_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def deactivate_market(market_id: int):
    """Деактивировать рынок (закрыт/разрешён)"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE markets SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (market_id,),
        )
        await db.commit()


# ── Price History ────────────────────────────────────────────

async def save_price(market_id: int, price_yes: float, price_no: float, volume: float = 0):
    """Сохранить снимок цены"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT INTO price_history (market_id, price_yes, price_no, volume) VALUES (?, ?, ?, ?)",
            (market_id, price_yes, price_no, volume),
        )
        await db.commit()


async def get_price_history(market_id: int, hours: int = 24) -> list[dict]:
    """Получить историю цен за N часов"""
    since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM price_history
               WHERE market_id = ? AND recorded_at >= ?
               ORDER BY recorded_at ASC""",
            (market_id, since),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def get_latest_price(market_id: int) -> dict | None:
    """Последняя цена для рынка"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM price_history WHERE market_id = ? ORDER BY recorded_at DESC LIMIT 1",
            (market_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


# ── Signals ──────────────────────────────────────────────────

async def save_signal(
    market_id: int,
    signal_type: str,
    direction: str,
    confidence: float,
    probability_at_signal: float,
    probability_change: float,
    reasoning: str = "",
) -> int:
    """Сохранить сигнал, вернуть ID"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO signals
               (market_id, signal_type, direction, confidence,
                probability_at_signal, probability_change, reasoning)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (market_id, signal_type, direction, confidence,
             probability_at_signal, probability_change, reasoning),
        )
        await db.commit()
        return cursor.lastrowid


async def mark_signal_published(signal_id: int):
    """Отметить сигнал как опубликованный"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "UPDATE signals SET published = 1 WHERE id = ?", (signal_id,)
        )
        await db.commit()


async def get_recent_signals(limit: int = 20) -> list[dict]:
    """Последние сигналы"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT s.*, m.question, m.category
               FROM signals s JOIN markets m ON s.market_id = m.id
               ORDER BY s.created_at DESC LIMIT ?""",
            (limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def get_unpublished_signals() -> list[dict]:
    """Неопубликованные сигналы"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT s.*, m.question, m.category, m.token_id_yes, m.token_id_no,
                      m.polymarket_url
               FROM signals s JOIN markets m ON s.market_id = m.id
               WHERE s.published = 0
               ORDER BY s.created_at ASC"""
        )
        return [dict(row) for row in await cursor.fetchall()]


# ── Trades ───────────────────────────────────────────────────

async def save_trade(
    user_id: int,
    signal_id: int | None,
    market_id: int,
    token_id: str,
    side: str,
    size_usdc: float,
    price: float,
    order_id: str = "",
    status: str = "pending",
) -> int:
    """Сохранить сделку"""
    async with aiosqlite.connect(config.DB_PATH) as conn:
        cursor = await conn.execute(
            """INSERT INTO trades
               (user_id, signal_id, market_id, token_id, side, size_usdc, price, order_id, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, signal_id, market_id, token_id, side, size_usdc, price, order_id, status),
        )
        await conn.commit()
        return cursor.lastrowid


async def update_trade_status(trade_id: int, status: str, pnl: float = 0):
    """Обновить статус сделки"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        if status in ("closed", "resolved"):
            await db.execute(
                "UPDATE trades SET status=?, pnl=?, closed_at=CURRENT_TIMESTAMP WHERE id=?",
                (status, pnl, trade_id),
            )
        else:
            await db.execute(
                "UPDATE trades SET status=?, pnl=? WHERE id=?",
                (status, pnl, trade_id),
            )
        await db.commit()


async def get_open_trades(user_id: int | None = None) -> list[dict]:
    """Открытые сделки (все или по user_id)"""
    async with aiosqlite.connect(config.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        if user_id:
            cursor = await conn.execute(
                """SELECT t.*, m.question, m.category
                   FROM trades t JOIN markets m ON t.market_id = m.id
                   WHERE t.status IN ('pending', 'filled') AND t.user_id = ?
                   ORDER BY t.created_at DESC""",
                (user_id,),
            )
        else:
            cursor = await conn.execute(
                """SELECT t.*, m.question, m.category
                   FROM trades t JOIN markets m ON t.market_id = m.id
                   WHERE t.status IN ('pending', 'filled')
                   ORDER BY t.created_at DESC"""
            )
        return [dict(row) for row in await cursor.fetchall()]


async def get_trade_history(limit: int = 50, user_id: int | None = None) -> list[dict]:
    """История сделок (все или по user_id)"""
    async with aiosqlite.connect(config.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        if user_id:
            cursor = await conn.execute(
                """SELECT t.*, m.question, m.category
                   FROM trades t JOIN markets m ON t.market_id = m.id
                   WHERE t.user_id = ?
                   ORDER BY t.created_at DESC LIMIT ?""",
                (user_id, limit),
            )
        else:
            cursor = await conn.execute(
                """SELECT t.*, m.question, m.category
                   FROM trades t JOIN markets m ON t.market_id = m.id
                   ORDER BY t.created_at DESC LIMIT ?""",
                (limit,),
            )
        return [dict(row) for row in await cursor.fetchall()]


async def get_user_portfolio_stats(user_id: int) -> dict:
    """Статистика портфеля для конкретного юзера"""
    async with aiosqlite.connect(config.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row

        cursor = await conn.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(size_usdc), 0) as total_invested "
            "FROM trades WHERE status IN ('pending', 'filled') AND user_id = ?",
            (user_id,),
        )
        open_row = await cursor.fetchone()

        cursor = await conn.execute(
            """SELECT COUNT(*) as cnt,
                      COALESCE(SUM(pnl), 0) as realized_pnl,
                      SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                      SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses
               FROM trades WHERE status IN ('closed', 'resolved') AND user_id = ?""",
            (user_id,),
        )
        closed_row = await cursor.fetchone()

        open_positions = open_row["cnt"] if open_row else 0
        total_invested = open_row["total_invested"] if open_row else 0
        total_closed = closed_row["cnt"] if closed_row else 0
        realized_pnl = closed_row["realized_pnl"] if closed_row else 0
        wins = closed_row["wins"] or 0 if closed_row else 0
        losses = closed_row["losses"] or 0 if closed_row else 0
        win_rate = (wins / total_closed * 100) if total_closed > 0 else 0

        return {
            "open_positions": open_positions,
            "total_invested": total_invested,
            "total_closed": total_closed,
            "realized_pnl": realized_pnl,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
        }


# ── Daily P&L ────────────────────────────────────────────────

async def record_daily_pnl(date_str: str):
    """Записать дневной P&L"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT
                   COALESCE(SUM(pnl), 0) as total_pnl,
                   COUNT(*) as trades_count,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses
               FROM trades
               WHERE DATE(created_at) = ? AND status IN ('closed', 'resolved')""",
            (date_str,),
        )
        row = await cursor.fetchone()
        if row:
            await db.execute(
                """INSERT OR REPLACE INTO daily_pnl (date, total_pnl, trades_count, wins, losses)
                   VALUES (?, ?, ?, ?, ?)""",
                (date_str, row["total_pnl"], row["trades_count"],
                 row["wins"] or 0, row["losses"] or 0),
            )
            await db.commit()


async def get_today_pnl() -> dict:
    """P&L за сегодня"""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT
                   COALESCE(SUM(pnl), 0) as total_pnl,
                   COUNT(*) as trades_count,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN pnl <= 0 AND status IN ('closed','resolved') THEN 1 ELSE 0 END) as losses
               FROM trades
               WHERE DATE(created_at) = ?""",
            (today,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else {"total_pnl": 0, "trades_count": 0, "wins": 0, "losses": 0}


async def get_portfolio_stats() -> dict:
    """Общая статистика портфеля"""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Открытые позиции
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(size_usdc), 0) as total_invested "
            "FROM trades WHERE status IN ('pending', 'filled')"
        )
        open_row = await cursor.fetchone()

        # Закрытые
        cursor = await db.execute(
            """SELECT COUNT(*) as cnt,
                      COALESCE(SUM(pnl), 0) as realized_pnl,
                      SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                      SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses
               FROM trades WHERE status IN ('closed', 'resolved')"""
        )
        closed_row = await cursor.fetchone()

        open_positions = open_row["cnt"] if open_row else 0
        total_invested = open_row["total_invested"] if open_row else 0
        total_closed = closed_row["cnt"] if closed_row else 0
        realized_pnl = closed_row["realized_pnl"] if closed_row else 0
        wins = closed_row["wins"] or 0 if closed_row else 0
        losses = closed_row["losses"] or 0 if closed_row else 0
        win_rate = (wins / total_closed * 100) if total_closed > 0 else 0

        return {
            "open_positions": open_positions,
            "total_invested": total_invested,
            "total_closed": total_closed,
            "realized_pnl": realized_pnl,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
        }
