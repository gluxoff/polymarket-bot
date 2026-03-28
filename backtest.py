"""Бэктест — сравнение разных стратегий"""

import asyncio
import aiosqlite
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "polymarket.db"

TRASH_PATTERNS = [
    "will trump say", "will biden say", "will elon say",
    "will mrbeast say", "said during",
    'say "', "say '",
    "during the next episode", "during his next video",
    "during the fii", "during the press conference",
    "at the rally",
    "highest temperature", "lowest temperature", "weather",
]


def is_trash(q: str) -> bool:
    q = q.lower()
    return any(p in q for p in TRASH_PATTERNS)


async def main():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute('''
            SELECT s.*, m.question, m.token_id_yes
            FROM signals s JOIN markets m ON s.market_id = m.id
            ORDER BY s.created_at ASC
        ''')
        all_signals = [dict(r) for r in await cursor.fetchall()]

        bet = 50

        async def get_current_price(market_id):
            c = await db.execute(
                'SELECT price_yes FROM price_history WHERE market_id=? ORDER BY recorded_at DESC LIMIT 1',
                (market_id,))
            r = await c.fetchone()
            return r[0] if r else None

        # === V1: Всё подряд ===
        v1_pnl, v1_n, v1_w = 0, 0, 0
        for s in all_signals:
            entry = s['probability_at_signal'] or 0
            if entry <= 0 or entry >= 1: continue
            cur = await get_current_price(s['market_id'])
            if cur is None: continue
            if s['direction'] == 'BUY':
                pnl = (cur - entry) * (bet / entry)
            else:
                pnl = (entry - cur) * (bet / (1 - entry))
            v1_pnl += pnl; v1_n += 1
            if pnl > 0: v1_w += 1

        # === V2: BUY моментум 15-70% ===
        v2_pnl, v2_n, v2_w = 0, 0, 0
        for s in all_signals:
            entry = s['probability_at_signal'] or 0
            change = s['probability_change'] or 0
            if change <= 0 or entry < 0.15 or entry > 0.70: continue
            if (1 - entry) / entry < 0.30: continue
            if is_trash(s['question']): continue
            if (s['confidence'] or 0) < 0.5: continue
            cur = await get_current_price(s['market_id'])
            if cur is None: continue
            pnl = (cur - entry) * (bet / entry)
            v2_pnl += pnl; v2_n += 1
            if pnl > 0: v2_w += 1

        # === V3: КОНТРАРИАНСКАЯ — покупай после падения ===
        # Логика: цена упала >8%, но всё ещё 20-65% — покупаем отскок
        v3_pnl, v3_n, v3_w = 0, 0, 0
        v3_details = []
        for s in all_signals:
            entry = s['probability_at_signal'] or 0
            change = s['probability_change'] or 0
            if change >= 0: continue  # только падающие
            if abs(change) < 0.08: continue  # минимум -8%
            if entry < 0.20 or entry > 0.65: continue
            if is_trash(s['question']): continue
            cur = await get_current_price(s['market_id'])
            if cur is None: continue
            pnl = (cur - entry) * (bet / entry)
            v3_pnl += pnl; v3_n += 1
            if pnl > 0: v3_w += 1
            v3_details.append((pnl, entry, cur, s['question'][:55]))

        # === V4: КОМБО — BUY при высоком объёме + средняя цена ===
        # Логика: любое направление, но объём 3x+, цена 25-60%
        v4_pnl, v4_n, v4_w = 0, 0, 0
        v4_details = []
        for s in all_signals:
            entry = s['probability_at_signal'] or 0
            change = s['probability_change'] or 0
            if entry < 0.25 or entry > 0.60: continue
            if is_trash(s['question']): continue
            if (s['confidence'] or 0) < 0.7: continue
            cur = await get_current_price(s['market_id'])
            if cur is None: continue
            # Всегда BUY (покупаем YES по средней цене)
            pnl = (cur - entry) * (bet / entry)
            v4_pnl += pnl; v4_n += 1
            if pnl > 0: v4_w += 1
            v4_details.append((pnl, entry, cur, s['question'][:55]))

        # === V5: SELL дорогое — продавай когда >75% и падает ===
        v5_pnl, v5_n, v5_w = 0, 0, 0
        v5_details = []
        for s in all_signals:
            entry = s['probability_at_signal'] or 0
            change = s['probability_change'] or 0
            if change >= 0: continue  # только падающие
            if entry < 0.75: continue  # дорогие рынки
            if is_trash(s['question']): continue
            cur = await get_current_price(s['market_id'])
            if cur is None: continue
            # SELL = покупаем NO, профит если цена падает
            pnl = (entry - cur) * (bet / (1 - entry))
            v5_pnl += pnl; v5_n += 1
            if pnl > 0: v5_w += 1
            v5_details.append((pnl, entry, cur, s['question'][:55]))

        # === Вывод ===
        def show(name, pnl, n, w, details=None):
            if n == 0:
                print(f"\n--- {name} ---\nСигналов: 0\n")
                return
            wr = w / n * 100
            roi = pnl / (bet * n) * 100
            print(f"\n--- {name} ---")
            print(f"Сигналов: {n}")
            print(f"Вложено: ${bet * n:,.0f}")
            print(f"P&L: ${pnl:+,.2f}")
            print(f"Win rate: {wr:.1f}%")
            print(f"ROI: {roi:+.1f}%")
            if details:
                details.sort(key=lambda x: x[0], reverse=True)
                print(f"  Лучшая: ${details[0][0]:+.2f} | {details[0][1]*100:.0f}%->{details[0][2]*100:.0f}% | {details[0][3]}")
                print(f"  Худшая: ${details[-1][0]:+.2f} | {details[-1][1]*100:.0f}%->{details[-1][2]*100:.0f}% | {details[-1][3]}")

        print("=" * 60)
        print("БЭКТЕСТ: 5 стратегий")
        print("=" * 60)
        show("V1: Всё подряд (старая)", v1_pnl, v1_n, v1_w)
        show("V2: BUY моментум 15-70%", v2_pnl, v2_n, v2_w)
        show("V3: КОНТРАРИАНСКАЯ (покупай после падения 20-65%)", v3_pnl, v3_n, v3_w, v3_details)
        show("V4: BUY при высокой уверенности 25-60%", v4_pnl, v4_n, v4_w, v4_details)
        show("V5: SELL дорогое (>75% и падает)", v5_pnl, v5_n, v5_w, v5_details)


asyncio.run(main())
