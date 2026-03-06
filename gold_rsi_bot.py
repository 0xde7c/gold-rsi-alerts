#!/usr/bin/env python3
"""
Gold RSI Alert Bot — XAU/USD multi-TF RSI with simple OB/OS alerts.

RSI(7) on 1m, 5m, 15m, 30m, 1H. RSI(14) on 4H.
Levels: 25 (oversold) / 75 (overbought).
Hysteresis: must recover 5 points past threshold before re-alerting.
"""

import time, json, logging, urllib.request, os
from datetime import datetime, timezone, timedelta

# ── LOGGING ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("gold_rsi.log")],
)
log = logging.getLogger("gold_rsi")

# ── CONFIG ───────────────────────────────────────────────────────────────
METAAPI_TOKEN = os.environ.get("METAAPI_TOKEN", "")
ACCOUNT_ID = os.environ.get("METAAPI_ACCOUNT_ID", "29e4f4af-fa3a-4e66-9116-98a2d9db91b2")

SYMBOL = "XAUUSD"

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "5583279698")

RSI_OVERSOLD = 25
RSI_OVERBOUGHT = 75
RSI_OS_RESET = 30         # must recover above 30 before OS can re-fire
RSI_OB_RESET = 70         # must drop below 70 before OB can re-fire

TIMEFRAMES = {
    "4H":  {"metaapi": "4h",  "poll": 900,  "candles": 200, "rsi_period": 14},
    "1H":  {"metaapi": "1h",  "poll": 300,  "candles": 200, "rsi_period": 7},
    "30m": {"metaapi": "30m", "poll": 180,  "candles": 200, "rsi_period": 7},
    "15m": {"metaapi": "15m", "poll": 120,  "candles": 200, "rsi_period": 7},
    "5m":  {"metaapi": "5m",  "poll": 60,   "candles": 200, "rsi_period": 7},
    "1m":  {"metaapi": "1m",  "poll": 60,   "candles": 200, "rsi_period": 7},
}

TF_ORDER = ["4H", "1H", "30m", "15m", "5m", "1m"]

METAAPI_BASE = "https://mt-market-data-client-api-v1.london.agiliumtrade.ai"

# ── STATE ────────────────────────────────────────────────────────────────
rsi_state = {}              # tf -> latest RSI
last_poll = {}              # tf -> last poll timestamp
last_price = None
prev_rsi = {}               # tf -> previous RSI
in_oversold = {}            # tf -> bool (currently in OS zone)
in_overbought = {}          # tf -> bool (currently in OB zone)


# ── SESSION TAG ──────────────────────────────────────────────────────────
def get_session_tag():
    now = datetime.now(timezone.utc)
    h, m = now.hour, now.minute
    mins = h * 60 + m
    if 300 <= mins < 660:
        return "🟢 PRIME SESSION"
    elif 810 <= mins < 1200:
        return "🗽 NY SESSION"
    else:
        return "⚪ OFF HOURS"


# ── TELEGRAM ─────────────────────────────────────────────────────────────
MAIN_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "📊 RSI", "callback_data": "gold"},
            {"text": "📰 Today", "callback_data": "today"},
        ],
        [
            {"text": "📅 Week", "callback_data": "week"},
            {"text": "🔴 High Impact", "callback_data": "high"},
        ],
    ]
}

def tg(msg, keyboard=None):
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = {"chat_id": TG_CHAT, "text": msg}
        if keyboard:
            payload["reply_markup"] = keyboard
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log.warning(f"TG send failed: {e}")


def tg_answer_callback(callback_id):
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/answerCallbackQuery"
        data = json.dumps({"callback_query_id": callback_id}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except:
        pass


last_update_id = 0

def tg_check_commands():
    global last_update_id
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?offset={last_update_id + 1}&limit=10&timeout=0"
        resp = json.loads(urllib.request.urlopen(url, timeout=10).read().decode())
        if not resp.get("ok"):
            return
        for upd in resp.get("result", []):
            last_update_id = upd["update_id"]

            cb = upd.get("callback_query")
            if cb:
                chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                if chat_id != TG_CHAT:
                    continue
                tg_answer_callback(cb["id"])
                handle_action(cb.get("data", ""))
                continue

            msg = upd.get("message", {})
            text = msg.get("text", "").strip().lower()
            chat_id = str(msg.get("chat", {}).get("id", ""))
            if chat_id != TG_CHAT:
                continue

            if text in ("/gold", "/xau"):
                handle_action("gold")
            elif text in ("/today", "/news"):
                handle_action("today")
            elif text == "/week":
                handle_action("week")
            elif text == "/high":
                handle_action("high")
            elif text == "/menu":
                tg("🥇 Gold RSI Bot", keyboard=MAIN_KEYBOARD)
    except:
        pass


def format_rsi_snapshot():
    lines = ["📊 XAU/USD RSI Snapshot\n"]
    for tf in TF_ORDER:
        rsi = rsi_state.get(tf)
        period = TIMEFRAMES[tf]["rsi_period"]
        if rsi is not None:
            tag = ""
            if rsi <= RSI_OVERSOLD:
                tag = " 🟢 OS"
            elif rsi >= RSI_OVERBOUGHT:
                tag = " 🔴 OB"
            lines.append(f"  {tf:>3}  RSI({period}): {rsi:5.1f}{tag}")
        else:
            lines.append(f"  {tf:>3}  RSI({period}):    --")

    if last_price:
        lines.append(f"\n💰 ${last_price:,.2f}")
    lines.append(get_session_tag())
    return "\n".join(lines)


def handle_action(action):
    if action == "gold":
        tg(format_rsi_snapshot(), keyboard=MAIN_KEYBOARD)
    elif action in ("today", "week", "high"):
        handle_news_command(action)
    else:
        tg("Use /gold or /menu", keyboard=MAIN_KEYBOARD)


# ── MACRO NEWS (ForexFactory) ────────────────────────────────────────────
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_CACHE = {"data": None, "fetched": 0}
FF_CACHE_TTL = 600

GOLD_RELEVANT = {"USD", "EUR", "GBP", "CHF", "JPY", "CNY", "AUD", "CAD"}

def ff_fetch():
    now = time.time()
    if FF_CACHE["data"] and now - FF_CACHE["fetched"] < FF_CACHE_TTL:
        return FF_CACHE["data"]
    try:
        req = urllib.request.Request(FF_URL, headers={"User-Agent": "GoldBot/1.0"})
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode())
        FF_CACHE["data"] = data
        FF_CACHE["fetched"] = now
        return data
    except Exception as e:
        log.error(f"FF fetch failed: {e}")
        return FF_CACHE["data"]


def ff_filter_events(data, start_date, end_date, gold_only=True):
    events = []
    for e in data:
        edate = e.get("date", "")[:10]
        if edate < start_date or edate > end_date:
            continue
        country = e.get("country", "")
        impact = e.get("impact", "")
        if gold_only:
            if impact == "High":
                events.append(e)
            elif country in GOLD_RELEVANT and impact in ("Medium", "High"):
                events.append(e)
        else:
            if impact in ("Medium", "High"):
                events.append(e)
    return events


def ff_format_events(events, title, show_date=False):
    if not events:
        return f"{title}\n\nNo major events scheduled."

    icon = {"High": "🔴", "Medium": "🟡", "Low": "⚪"}
    lines = [title, ""]

    current_date = ""
    for e in events:
        edate = e.get("date", "")[:10]
        etime = e.get("date", "")[11:16]
        if show_date and edate != current_date:
            try:
                dt = datetime.strptime(edate, "%Y-%m-%d")
                lines.append(f"── {dt.strftime('%a %b %d')} ──")
            except:
                lines.append(f"── {edate} ──")
            current_date = edate

        imp = icon.get(e.get("impact", ""), "⚪")
        country = e.get("country", "?")
        title_str = e.get("title", "?")
        forecast = e.get("forecast", "")
        previous = e.get("previous", "")
        extra = ""
        if forecast:
            extra += f" F:{forecast}"
        if previous:
            extra += f" P:{previous}"
        lines.append(f" {etime} {imp} {country} {title_str}{extra}")

    lines.append("\n⏰ Times EST (UTC-5)")
    if last_price:
        lines.append(f"💰 XAU ${last_price:,.2f}")
    return "\n".join(lines)


def handle_news_command(period):
    data = ff_fetch()
    if not data:
        tg("📰 Failed to fetch calendar data.")
        return

    now_utc = datetime.now(timezone.utc)

    if period == "today":
        today = now_utc.strftime("%Y-%m-%d")
        events = ff_filter_events(data, today, today)
        msg = ff_format_events(events, f"📰 Today's Macro — {today}")
    elif period == "week":
        today = now_utc.strftime("%Y-%m-%d")
        end = (now_utc + timedelta(days=7)).strftime("%Y-%m-%d")
        events = ff_filter_events(data, today, end)
        msg = ff_format_events(events, "📰 This Week's Macro", show_date=True)
    elif period == "high":
        today = now_utc.strftime("%Y-%m-%d")
        end = (now_utc + timedelta(days=7)).strftime("%Y-%m-%d")
        high_events = [e for e in data
                       if e.get("date", "")[:10] >= today
                       and e.get("date", "")[:10] <= end
                       and e.get("impact") == "High"]
        msg = ff_format_events(high_events, "🔴 High Impact Events This Week", show_date=True)
    else:
        msg = "Unknown period."

    tg(msg, keyboard=MAIN_KEYBOARD)


# ── RSI CALCULATION (Wilder's smoothing) ─────────────────────────────────
def compute_rsi(closes, period):
    if len(closes) < period + 1:
        return None

    gains = []
    losses = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))

    if len(gains) < period:
        return None

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ── FETCH CANDLES FROM METAAPI ───────────────────────────────────────────
def fetch_candles(timeframe_key):
    tf_config = TIMEFRAMES[timeframe_key]
    metaapi_tf = tf_config["metaapi"]
    limit = tf_config["candles"]

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    url = (
        f"{METAAPI_BASE}/users/current/accounts/{ACCOUNT_ID}"
        f"/historical-market-data/symbols/{SYMBOL}/timeframes/{metaapi_tf}"
        f"/candles?startTime={now}&limit={limit}"
    )

    try:
        req = urllib.request.Request(url, headers={
            "auth-token": METAAPI_TOKEN,
            "Content-Type": "application/json"
        })
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read().decode())

        if not data or not isinstance(data, list):
            log.warning(f"No candle data for {timeframe_key}: {data}")
            return None

        candles = []
        for c in data:
            candles.append({
                "time": c.get("time", ""),
                "close": float(c.get("close", 0)),
            })

        candles.sort(key=lambda x: x["time"])
        return candles

    except Exception as e:
        log.error(f"fetch_candles({timeframe_key}) failed: {e}")
        return None


# ── CONFLUENCE COUNT ─────────────────────────────────────────────────────
def count_confluence(zone):
    """Count how many TFs are currently in the given zone ('os' or 'ob')."""
    tfs = []
    for tf in TF_ORDER:
        rsi = rsi_state.get(tf)
        if rsi is None:
            continue
        if zone == "os" and rsi <= RSI_OVERSOLD:
            tfs.append(tf)
        elif zone == "ob" and rsi >= RSI_OVERBOUGHT:
            tfs.append(tf)
    return tfs


# ── SIGNAL LOGIC ─────────────────────────────────────────────────────────
def check_signals(tf, rsi, price):
    """Check for OB/OS zone entry/exit with hysteresis."""
    period = TIMEFRAMES[tf]["rsi_period"]
    was_os = in_oversold.get(tf, False)
    was_ob = in_overbought.get(tf, False)

    # ── OVERSOLD ──
    if not was_os and rsi <= RSI_OVERSOLD:
        # Entered oversold zone
        in_oversold[tf] = True
        aligned = count_confluence("os")
        count = len(aligned)

        msg = f"🟢 XAU/USD RSI Oversold — {tf}\n"
        msg += f"RSI({period}): {rsi:.1f}\n"
        msg += f"Price: ${price:,.2f}\n"
        if count > 1:
            msg += f"[{count}/6] {', '.join(aligned)}"

        tg(msg)
        log.info(f"OVERSOLD {tf} RSI({period})={rsi:.1f} confluences={count} ({', '.join(aligned)})")

    elif was_os and rsi > RSI_OS_RESET:
        # Left oversold zone (with hysteresis)
        in_oversold[tf] = False

        msg = f"⬆️ XAU/USD RSI Left Oversold — {tf}\n"
        msg += f"RSI({period}): {rsi:.1f}\n"
        msg += f"Price: ${price:,.2f}"

        tg(msg)
        log.info(f"LEFT OS {tf} RSI({period})={rsi:.1f}")

    # ── OVERBOUGHT ──
    if not was_ob and rsi >= RSI_OVERBOUGHT:
        # Entered overbought zone
        in_overbought[tf] = True
        aligned = count_confluence("ob")
        count = len(aligned)

        msg = f"🔴 XAU/USD RSI Overbought — {tf}\n"
        msg += f"RSI({period}): {rsi:.1f}\n"
        msg += f"Price: ${price:,.2f}\n"
        if count > 1:
            msg += f"[{count}/6] {', '.join(aligned)}"

        tg(msg)
        log.info(f"OVERBOUGHT {tf} RSI({period})={rsi:.1f} confluences={count} ({', '.join(aligned)})")

    elif was_ob and rsi < RSI_OB_RESET:
        # Left overbought zone (with hysteresis)
        in_overbought[tf] = False

        msg = f"⬇️ XAU/USD RSI Left Overbought — {tf}\n"
        msg += f"RSI({period}): {rsi:.1f}\n"
        msg += f"Price: ${price:,.2f}"

        tg(msg)
        log.info(f"LEFT OB {tf} RSI({period})={rsi:.1f}")


# ── PROCESS TIMEFRAME ────────────────────────────────────────────────────
def process_timeframe(tf, bootstrap=False):
    """Fetch candles, compute RSI, check signals."""
    global last_price

    candles = fetch_candles(tf)
    if not candles or len(candles) < 20:
        log.warning(f"{tf}: Not enough candles ({len(candles) if candles else 0})")
        return

    closes = [c["close"] for c in candles]
    price = closes[-1]
    if tf in ("1m", "5m"):
        last_price = price

    period = TIMEFRAMES[tf]["rsi_period"]
    rsi = compute_rsi(closes, period)
    if rsi is None:
        return

    rsi_state[tf] = rsi

    if bootstrap:
        # Set initial zone state without firing alerts
        in_oversold[tf] = rsi <= RSI_OVERSOLD
        in_overbought[tf] = rsi >= RSI_OVERBOUGHT
        zone = ""
        if in_oversold[tf]:
            zone = " [OS]"
        elif in_overbought[tf]:
            zone = " [OB]"
        log.info(f"{tf}: RSI({period})={rsi:.1f}{zone} (init)")
        return

    log.info(f"{tf}: RSI({period})={rsi:.1f} price=${price:,.2f}")
    check_signals(tf, rsi, price)


# ── MAIN LOOP ────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("Gold RSI Alert Bot — XAU/USD")
    for tf in TF_ORDER:
        p = TIMEFRAMES[tf]["rsi_period"]
        log.info(f"  {tf}: RSI({p}) poll={TIMEFRAMES[tf]['poll']}s")
    log.info(f"  OB={RSI_OVERBOUGHT} OS={RSI_OVERSOLD} hysteresis={RSI_OB_RESET}/{RSI_OS_RESET}")
    log.info("=" * 60)

    # Register Telegram commands
    try:
        cmd_url = f"https://api.telegram.org/bot{TG_TOKEN}/setMyCommands"
        commands = [
            {"command": "gold", "description": "RSI Snapshot"},
            {"command": "today", "description": "Today's macro calendar"},
            {"command": "week", "description": "This week's macro calendar"},
            {"command": "high", "description": "High impact events only"},
            {"command": "menu", "description": "Show button menu"},
        ]
        cmd_data = json.dumps({"commands": commands}).encode()
        req = urllib.request.Request(cmd_url, data=cmd_data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log.warning(f"Failed to set TG menu: {e}")

    tg("🥇 XAU/USD RSI Bot started\n"
       "1m/5m/15m/30m/1H: RSI(7)\n"
       "4H: RSI(14)\n"
       "Levels: 25/75 (hysteresis 30/70)\n"
       "Alerts on every OB/OS entry + exit",
       keyboard=MAIN_KEYBOARD)

    # Bootstrap — set initial zone state without alerts
    log.info("Bootstrap: fetching all timeframes...")
    for tf in TF_ORDER:
        try:
            process_timeframe(tf, bootstrap=True)
            last_poll[tf] = time.time()
            time.sleep(1)
        except Exception as e:
            log.error(f"Bootstrap {tf} failed: {e}")
            last_poll[tf] = 0

    tg(format_rsi_snapshot(), keyboard=MAIN_KEYBOARD)
    log.info("Bootstrap complete. Entering main loop...")

    while True:
        try:
            now = time.time()

            for tf, config in TIMEFRAMES.items():
                elapsed = now - last_poll.get(tf, 0)
                if elapsed >= config["poll"]:
                    try:
                        process_timeframe(tf)
                        last_poll[tf] = now
                    except Exception as e:
                        log.error(f"Process {tf} failed: {e}")

            tg_check_commands()
            time.sleep(30)

        except KeyboardInterrupt:
            log.info("Shutting down...")
            break
        except Exception as e:
            log.error(f"Main loop error: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
