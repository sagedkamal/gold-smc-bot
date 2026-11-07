# -*- coding: utf-8 -*-
"""
Gold SMC Breakout Channels Bot

Python port of "Smart Money Breakout Channels [AlgoAlpha]" (Pine Script v6).
Original indicator (c) AlgoAlpha, Mozilla Public License 2.0
https://mozilla.org/MPL/2.0/

What is ported: channel detection + breakout signals (the core logic).
What is NOT ported: the volume candles / volume gauge (visual only, and they
need lower-timeframe volume data).

Usage:
    python bot.py          # run the bot (needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    python bot.py test     # print the last signals found in recent data (no Telegram)
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

# ============================================================================
# SETTINGS
# ============================================================================

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8869930845:AAFmPDJiAaHUhi64QkQLt2ZBnguYVPptZ4o")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "7499011694")

SYMBOL = "GC=F"          # Gold futures on Yahoo (spot gold = "XAUUSD=X", lower quality data)
INTERVAL = "15m"
INTERVAL_MIN = 15        # must match INTERVAL
DATA_RANGE = "30d"       # history used to warm up the indicator
POLL_SECONDS = 60

# Indicator inputs (same names/defaults as the Pine script)
OVERLAP = False          # "Nested Channels"
STRONG = True            # "Strong Closes Only"
NORM_LEN = 100           # "Normalization Length"
BOX_LEN = 14             # "Box Detection Length"
MIN_DURATION = 10        # Pine: duration > 10

# Trade levels (my defaults, not part of the original indicator)
# Stop loss = channel midline, targets = multiples of the risk distance.
RR_LEVELS = (1.0, 2.0, 3.0)

TZ_OFFSET_HOURS = 2      # Sudan, UTC+2

# ============================================================================
# DATA
# ============================================================================


def fetch_candles():
    """Download candles from Yahoo and keep only CLOSED candles."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{SYMBOL}"
    r = requests.get(
        url,
        params={"interval": INTERVAL, "range": DATA_RANGE},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame(
        {
            "open": q["open"],
            "high": q["high"],
            "low": q["low"],
            "close": q["close"],
        },
        index=pd.to_datetime(res["timestamp"], unit="s"),
    ).dropna()

    # Yahoo timestamps are candle START times; drop the candle still forming.
    now = datetime.utcnow()
    closed = (df.index + pd.Timedelta(minutes=INTERVAL_MIN)) <= now
    return df[closed]


# ============================================================================
# INDICATOR (port of the Pine logic)
# ============================================================================


def detect_channels(df):
    """
    Run the breakout-channel indicator over df (columns open/high/low/close)
    and return a list of breakout events (oldest first).
    """
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    n = len(df)

    # normalizedPrice = (close - lowest(low, L)) / (highest(high, L) - lowest(low, L))
    ll = pd.Series(l).rolling(NORM_LEN).min()
    hh = pd.Series(h).rolling(NORM_LEN).max()
    norm = (pd.Series(c) - ll) / (hh - ll).replace(0, np.nan)
    # Pine ta.stdev is the population (biased) standard deviation
    vol = norm.rolling(14).std(ddof=0).to_numpy()

    # upper/lower = position (0..1) of the highest/lowest vol inside the last BOX_LEN+1 bars
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    for t in range(BOX_LEN, n):
        w = vol[t - BOX_LEN : t + 1]
        if np.isnan(w).any():
            continue
        rev = w[::-1]  # rev[0] = current bar
        upper[t] = (BOX_LEN - np.argmax(rev)) / BOX_LEN
        lower[t] = (BOX_LEN - np.argmin(rev)) / BOX_LEN

    def crossover(a, b, t):
        if t < 1:
            return False
        vals = (a[t], b[t], a[t - 1], b[t - 1])
        if any(np.isnan(v) for v in vals):
            return False
        return a[t] > b[t] and a[t - 1] <= b[t - 1]

    boxes = []   # active channels
    events = []
    last_lu = None  # last bar where crossover(lower, upper) happened (barssince)

    for t in range(n):
        if crossover(lower, upper, t):
            last_lu = t
        duration = 0 if last_lu is None else t - last_lu
        duration = max(duration, 1)

        # --- new channel formed ---
        if crossover(upper, lower, t) and duration > MIN_DURATION:
            top = h[t - duration + 1 : t + 1].max()
            bottom = l[t - duration + 1 : t + 1].min()
            can_create = OVERLAP or not any(
                (top > b["bottom"]) and (bottom < b["top"]) for b in boxes
            )
            if can_create:
                boxes.append({"top": top, "bottom": bottom, "start": t - duration})

        # --- breakout check on every active channel ---
        price = (c[t] + o[t]) / 2 if STRONG else c[t]
        for b in list(boxes):
            if price > b["top"]:
                events.append(make_event("BUY", t, df.index[t], c[t], b))
                boxes.remove(b)
            elif price < b["bottom"]:
                events.append(make_event("SELL", t, df.index[t], c[t], b))
                boxes.remove(b)

    return events


def make_event(side, t, time_, close, box):
    entry = float(close)
    mid = (box["top"] + box["bottom"]) / 2
    sl = float(mid)
    risk = abs(entry - sl)
    sign = 1 if side == "BUY" else -1
    tps = [entry + sign * risk * r for r in RR_LEVELS]
    return {
        "side": side,
        "index": t,
        "time": time_,
        "entry": entry,
        "sl": sl,
        "tps": tps,
        "box_top": float(box["top"]),
        "box_bottom": float(box["bottom"]),
    }


# ============================================================================
# TELEGRAM
# ============================================================================


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": message}, timeout=20)
        data = r.json()
        if not data.get("ok"):
            print(f"Telegram error: {data}")
        return data
    except Exception as e:
        print(f"Error sending message: {e}")


def build_message(ev):
    local = ev["time"] + timedelta(minutes=INTERVAL_MIN) + timedelta(hours=TZ_OFFSET_HOURS)
    direction = "Bullish" if ev["side"] == "BUY" else "Bearish"
    lines = [
        "👑 SMC BREAKOUT CHANNEL SIGNAL 👑",
        f"🪙 Asset: XAUUSD ({SYMBOL})",
        f"🎯 Concept: {direction} breakout of consolidation channel",
        f"🚀 Action: {ev['side']} @ {ev['entry']:.2f}",
        "",
    ]
    for i, tp in enumerate(ev["tps"], 1):
        lines.append(f"✅ Take Profit {i}: {tp:.2f}")
    lines += [
        f"❌ Stop Loss: {ev['sl']:.2f}",
        "",
        f"📦 Channel: {ev['box_bottom']:.2f} - {ev['box_top']:.2f}",
        f"⏱ Candle close: {local:%Y-%m-%d %H:%M} (UTC+{TZ_OFFSET_HOURS})",
        f"🛡 Timeframe: {INTERVAL}",
    ]
    return "\n".join(lines)


# ============================================================================
# RUN
# ============================================================================


def run_bot():
    if not TOKEN or not CHAT_ID:
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID as environment variables.")
        sys.exit(1)

    print("Bot is running and scanning XAUUSD markets...")
    last_time = None  # time of the last candle already processed

    while True:
        try:
            df = fetch_candles()
            events = detect_channels(df)
            newest = df.index[-1]

            if last_time is None:
                # First run: don't send old history, just remember where we are.
                last_time = newest
                print(f"Started. Last closed candle: {newest} UTC")
            else:
                for ev in events:
                    if ev["time"] > last_time:
                        print(f"Signal: {ev['side']} @ {ev['entry']:.2f}")
                        send_telegram(build_message(ev))
                last_time = newest
        except Exception as e:
            print(f"Loop error: {e}")

        time.sleep(POLL_SECONDS)


def run_test():
    df = fetch_candles()
    events = detect_channels(df)
    print(f"Candles: {len(df)} | signals found: {len(events)}")
    for ev in events[-5:]:
        print("-" * 40)
        print(build_message(ev))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        run_test()
    else:
        run_bot()
