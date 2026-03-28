"""Бэктест — проверка стратегии v2 на исторических данных"""

import asyncio
import aiosqlite
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "polymarket.db"

TRASH_PATTERNS = [
    "will trump say", "will biden say", "will elon say",
    "will mrbeast say", "said during",
    'say "', "say '",
    "during the next episode",
    "during his next video",
    "during the fii", "during the press conference",
    "at the rally",
    "highest temperature", "lowest temperature",
    "weather",
]


def is_trash(question: str) -> bool:
    q = question.lower()
    for p in TRASH_PATTERNS:
        if p in q:
            return True
    return False


async def main():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Все сигналы с ценами
        cursor = await db.execute('''
            SELECT s.*, m.question, m.token_id_yes
            FROM signals s JOIN markets m ON s.market_id = m.id
            ORDER BY s.created_at ASC
        ''')
        all_signals = [dict(r) for r in await cursor.fetchall()]

        bet = 50

        # === Стратегия v1 (старая — всё подряд) ===
        v1_profit = 0
        v1_count = 0
        v1_wins = 0

        for s in all_signals:
            entry = s['probability_at_signal'] or 0
            if entry <= 0 or entry >= 1:
                continue
            cursor2 = await db.execute(
                'SELECT price_yes FROM price_history WHERE market_id=? ORDER BY recorded_at DESC LIMIT 1',
                (s['market_id'],)
            )
            row = await cursor2.fetchone()
            if not row:
                continue
            current = row[0] or 0

            if s['direction'] == 'BUY':
                shares = bet / entry
                pnl = (current - entry) * shares
            else:
                shares = bet / (1 - entry)
                pnl = (entry - current) * shares

            v1_profit += pnl
            v1_count += 1
            if pnl > 0:
                v1_wins += 1

        # === Стратегия v2 (новая — только BUY, 15-70%, без мусора) ===
        v2_profit = 0
        v2_count = 0
        v2_wins = 0
        v2_details = []

        for s in all_signals:
            entry = s['probability_at_signal'] or 0
            change = s['probability_change'] or 0

            # Фильтры v2
            if change <= 0:  # только рост
                continue
            if entry < 0.15 or entry > 0.70:  # только 15-70%
                continue
            potential = (1.0 - entry) / entry
            if potential < 0.30:  # мин 30% потенциал
                continue
            if is_trash(s['question']):  # мусор
                continue
            if (s['confidence'] or 0) < 0.5:  # мин уверенность
                continue

            cursor2 = await db.execute(
                'SELECT price_yes FROM price_history WHERE market_id=? ORDER BY recorded_at DESC LIMIT 1',
                (s['market_id'],)
            )
            row = await cursor2.fetchone()
            if not row:
                continue
            current = row[0] or 0

            shares = bet / entry
            pnl = (current - entry) * shares

            v2_profit += pnl
            v2_count += 1
            if pnl > 0:
                v2_wins += 1
            v2_details.append((pnl, entry, current, s['confidence'], s['question'][:55]))

        # === Результаты ===
        print("=" * 60)
        print("БЭКТЕСТ: Стратегия v1 (старая) vs v2 (новая)")
        print("=" * 60)

        print(f"\n--- v1: ВСЁ ПОДРЯД ---")
        print(f"Сигналов: {v1_count}")
        print(f"Вложено: ${bet * v1_count:,.0f}")
        print(f"P&L: ${v1_profit:+,.2f}")
        v1_wr = v1_wins / v1_count * 100 if v1_count else 0
        print(f"Win rate: {v1_wr:.1f}%")
        v1_roi = v1_profit / (bet * v1_count) * 100 if v1_count else 0
        print(f"ROI: {v1_roi:+.1f}%")

        print(f"\n--- v2: ТОЛЬКО BUY 15-70%, БЕЗ МУСОРА ---")
        print(f"Сигналов: {v2_count}")
        print(f"Вложено: ${bet * v2_count:,.0f}")
        print(f"P&L: ${v2_profit:+,.2f}")
        v2_wr = v2_wins / v2_count * 100 if v2_count else 0
        print(f"Win rate: {v2_wr:.1f}%")
        v2_roi = v2_profit / (bet * v2_count) * 100 if v2_count else 0
        print(f"ROI: {v2_roi:+.1f}%")

        print(f"\n--- РАЗНИЦА ---")
        print(f"Сигналов: {v1_count} → {v2_count} ({v2_count - v1_count:+d})")
        print(f"Win rate: {v1_wr:.1f}% → {v2_wr:.1f}%")
        print(f"ROI: {v1_roi:+.1f}% → {v2_roi:+.1f}%")

        if v2_details:
            v2_details.sort(key=lambda x: x[0], reverse=True)
            print(f"\n=== ТОП-5 ЛУЧШИХ v2 ===")
            for pnl, entry, cur, conf, q in v2_details[:5]:
                print(f"  ${pnl:+.2f} | {entry*100:.0f}%->{cur*100:.0f}% | conf {conf:.2f} | {q}")
            print(f"\n=== ТОП-5 ХУДШИХ v2 ===")
            for pnl, entry, cur, conf, q in v2_details[-5:]:
                print(f"  ${pnl:+.2f} | {entry*100:.0f}%->{cur*100:.0f}% | conf {conf:.2f} | {q}")


asyncio.run(main())
