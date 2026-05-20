"""
Simulate live trading activity on the Securities & Trading Firm Postgres database.

This is what we expect to see in their real prod environment - their OMS
inserting executed trades into Oracle, plus the market-data feed
updating LTP every few seconds.

RDI will pick these up via CDC and propagate them to Redis Enterprise
within ~1 second so the portfolio dashboard reflects them live.
"""
from __future__ import annotations

import argparse
import os
import random
import time
from datetime import datetime

import psycopg2

DB = dict(
    host=os.getenv("PG_HOST", "localhost"),
    port=int(os.getenv("PG_PORT", "5432")),
    dbname=os.getenv("PG_DB", "sectrade"),
    user=os.getenv("PG_USER", "postgres"),
    password=os.getenv("PG_PASS", "postgres"),
)


def fetch_universe(cur):
    cur.execute("SELECT customer_id FROM portfolio.customer "
                "WHERE kyc_status='VERIFIED' ORDER BY customer_id")
    customers = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT security_id FROM portfolio.security_master "
                "WHERE is_active = true")
    securities = [r[0] for r in cur.fetchall()]
    return customers, securities


def update_price(cur, security_id: int) -> None:
    """Tick the LTP up or down by a small percentage."""
    cur.execute("SELECT ltp, day_high, day_low FROM portfolio.market_price "
                "WHERE security_id=%s", (security_id,))
    row = cur.fetchone()
    if not row:
        return
    ltp, hi, lo = (float(x) for x in row)
    move = ltp * random.uniform(-0.004, 0.004)   # +/- 0.4%
    new = round(max(0.05, ltp + move), 2)
    hi = max(hi, new); lo = min(lo, new)
    cur.execute("""
        UPDATE portfolio.market_price
        SET ltp=%s, day_high=%s, day_low=%s,
            volume = volume + %s, updated_at=now()
        WHERE security_id=%s
    """, (new, hi, lo, random.randint(50, 5000), security_id))


def insert_trade(cur, customer_id: int, security_id: int) -> None:
    cur.execute("SELECT ltp FROM portfolio.market_price WHERE security_id=%s",
                (security_id,))
    row = cur.fetchone()
    if not row:
        return
    ltp = float(row[0])
    side = random.choices(["BUY", "SELL"], weights=[6, 4])[0]
    qty = random.choice([1, 2, 5, 10, 25, 50, 100])
    # slight slippage
    px = round(ltp * random.uniform(0.998, 1.002), 2)
    val = round(qty * px, 2)
    brokerage = round(val * 0.0005, 2)
    order_id = f"ORD-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{random.randint(100,999)}"
    cur.execute("""
        INSERT INTO portfolio.trade
          (trade_id, customer_id, security_id, side, quantity, price,
           trade_value, brokerage, order_id, exchange, executed_at)
        VALUES (nextval('portfolio.trade_id_seq'), %s, %s, %s, %s, %s, %s, %s, %s, 'NSE', now())
    """, (customer_id, security_id, side, qty, px, val, brokerage, order_id))

    # Maintain holdings (real OMS would do this in the same transaction)
    cur.execute("""
        SELECT holding_id, quantity, avg_buy_price
        FROM portfolio.holding WHERE customer_id=%s AND security_id=%s
    """, (customer_id, security_id))
    h = cur.fetchone()
    if h:
        hid, cur_qty, cur_avg = h[0], float(h[1]), float(h[2])
        if side == "BUY":
            new_qty = cur_qty + qty
            new_avg = ((cur_qty * cur_avg) + (qty * px)) / new_qty
        else:
            new_qty = max(0, cur_qty - qty)
            new_avg = cur_avg
        cur.execute("""
            UPDATE portfolio.holding
            SET quantity=%s, avg_buy_price=%s,
                invested_value=%s, last_trade_date=current_date, updated_at=now()
            WHERE holding_id=%s
        """, (new_qty, round(new_avg, 4), round(new_qty * new_avg, 4), hid))
    elif side == "BUY":
        cur.execute("""
            INSERT INTO portfolio.holding
              (holding_id, customer_id, security_id, quantity,
               avg_buy_price, invested_value, last_trade_date)
            VALUES (nextval('portfolio.holding_id_seq'), %s, %s, %s, %s, %s, current_date)
        """, (customer_id, security_id, qty, px, round(qty * px, 4)))

    print(f"  trade  {side} {qty:>4} @ {px:>10.2f}  cust={customer_id} sec={security_id}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades-per-sec", type=float, default=2.0,
                    help="approx number of trades inserted per second")
    ap.add_argument("--price-updates-per-sec", type=float, default=8.0,
                    help="approx number of price ticks per second")
    ap.add_argument("--duration", type=int, default=0,
                    help="run for N seconds, 0 = forever")
    args = ap.parse_args()

    conn = psycopg2.connect(**DB); conn.autocommit = True
    cur = conn.cursor()
    customers, securities = fetch_universe(cur)
    print(f"[load] {len(customers)} customers, {len(securities)} securities")
    print(f"[load] generating ~{args.trades_per_sec} trades/s and "
          f"~{args.price_updates_per_sec} price ticks/s")

    started = time.time()
    next_trade  = time.time()
    next_price  = time.time()
    trade_int   = 1.0 / args.trades_per_sec
    price_int   = 1.0 / args.price_updates_per_sec

    while True:
        now = time.time()
        if now >= next_trade:
            insert_trade(cur, random.choice(customers), random.choice(securities))
            next_trade = now + trade_int * random.uniform(0.5, 1.5)
        if now >= next_price:
            update_price(cur, random.choice(securities))
            next_price = now + price_int * random.uniform(0.5, 1.5)
        if args.duration and (now - started) > args.duration:
            break
        time.sleep(0.05)


if __name__ == "__main__":
    main()
