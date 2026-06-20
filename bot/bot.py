"""
PomRaid Bot v2 — $POM Community Raid Tracker
BNB Chain | @Pom_bsc | Python 3.11

All times in WAT (Africa/Lagos, UTC+1).

Raid week: Monday 00:00 WAT → Sunday 16:00 WAT
Payout + raffle window: Sunday 16:00 → 24:00 WAT
Weekly reset: Monday 00:00 WAT

Weekly Pool: $125
  Top 10: $25/$18/$14/$11/$9/$8/$7/$6/$5/$5 = $108
  Raffle (4 winners): $4 each = $16
  Buffer: $1 → rollover
  Qualification: 70%+ of top scorer's X points

Features:
  - Daily check-in /checkin (TG points, streaks 1→2→3→4→5)
  - Battle royale raffle (4 rounds, owner-triggered)
  - /tip <user> <amount> by admins ($USD or $POM)
  - Relink cooldown (7 days for different handle)
  - Monthly loyalty bonus ($15/$10/$5 to top 3)
  - Smart sync (12:00 & 20:00 WAT batch + Sunday 15:55 final)
  - X API spend tracking + daily DM to owner
  - Freeze window (no new X points Sun 16:00 → Mon 00:00 WAT)
  - HTML parse mode throughout
"""

import asyncio
import difflib
import html
import logging
import os
import random
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import (
    BotCommand, BotCommandScopeChat, BotCommandScopeDefault,
    ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)
from web3 import Web3

from database import (
    add_tg_points_db, add_x_points_db,
    clear_unlink, clear_week_raids, close_db, credit_engagement,
    get_all_users, get_api_spend, get_api_spend_month, get_api_spend_range,
    get_api_spend_week, get_meta, get_monthly_loyalty_top3, get_or_create_user,
    get_or_create_raffle_state, get_period_leaderboard, get_raffle_state,
    get_raids, get_recent_tips, get_streak, get_today_tg_points,
    get_total_x_points_this_week, get_unlink_info,
    handle_taken_by_other, init_db, is_engagement_credited,
    log_tip, record_api_spend, register_raid, reset_period_scores,
    save_raffle_entrants, save_raffle_round, save_user, set_last_unlink,
    set_meta, tweet_already_registered, update_streak, update_tip_status,
)

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────────

BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "")
PAY_WALLET_KEY = os.getenv("PAY_WALLET_KEY", "")

POM_X_HANDLE = "Pom_bsc"
POM_X_ID: str | None = None     # cached on first lookup

# Security
OWNER_ID      = 5228498784
RAID_GROUP_ID = -1002483287072

# Timezone: WAT = UTC+1, no DST
WAT = timezone(timedelta(hours=1))

# BNB Chain / $POM contract
BSC_RPC    = "https://bsc-dataseed.binance.org/"
POM_CA     = Web3.to_checksum_address("0xfbf174090b3cc8ebb9f39b697035a54c5c45b4d6")
BSCSCAN_TX = "https://bscscan.com/tx/"

ERC20_ABI = [
    {"inputs":[{"name":"_to","type":"address"},{"name":"_value","type":"uint256"}],
     "name":"transfer","outputs":[{"name":"","type":"bool"}],
     "stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"_owner","type":"address"}],
     "name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],
     "stateMutability":"view","type":"function"},
]

# Rewards — $125 pool
WEEKLY_POOL_USD     = 125.0
TIER_AMOUNTS        = {1:25, 2:18, 3:14, 4:11, 5:9, 6:8, 7:7, 8:6, 9:5, 10:5}
TOP_N               = 10
QUALIFY_PCT         = 0.70
RAFFLE_WINNER_AMOUNT_USD = 4.0
RAFFLE_WINNERS      = 4
MONTHLY_LOYALTY     = {1: 15, 2: 10, 3: 5}

# X engagement
LINK_COOLDOWN_HOURS = 12
RELINK_COOLDOWN_DAYS = 7
SYNC_COOLDOWN_MINS  = 60
X_POINTS = {"like":5, "repost":10, "comment":15, "quote":20, "post_drop":25}
PERSONAL_POST = {
    "min_words":      50,
    "require_image":  True,
    "min_followers":  50,
    "similarity_limit":0.70,
    "history_count":  5,
}

# Streak — TG points per day of streak
STREAK_BONUS = {1: 1, 2: 2, 3: 3, 4: 4}   # day 1=+1, day 2=+2, etc. Day 5+ = 5
STREAK_CAP   = 5

# TG activity
TG_MIN_WORDS = 8
TG_DAILY_CAP = 20

# Spam protection
SPAM = {"max_per_minute":8, "max_links":3, "mute_minutes":10, "ban_on_third_offense":True}
BANNED_WORDS = [
    "scam","rug pull","double your bnb",
    "giveaway http","dm me for profit","investment guaranteed",
]
LINK_RE   = re.compile(r"https?://\S+", re.IGNORECASE)
X_LINK_RE = re.compile(r"https?://(?:twitter\.com|x\.com)/\S+/status/(\d+)", re.IGNORECASE)

# API cost — pay-per-use estimates
API_COST_USER_LOOKUP  = 0.01
API_COST_TWEET_READ   = 0.005
API_COST_ENGAGEMENT   = 0.005  # per likers/retweeters/replies/quotes call

# Daily spend alert threshold
DAILY_SPEND_ALERT_USD = 2.00

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
#  HTML SAFE
# ─────────────────────────────────────────────────────────────────────────────

def esc(text) -> str:
    return html.escape(str(text), quote=False)

DIV  = "━━━━━━━━━━━━━━━━━━"
DIV2 = "─────────────────"

# ─────────────────────────────────────────────────────────────────────────────
#  TIME HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.utcnow().isoformat()

def now_wat() -> datetime:
    return datetime.now(WAT)

def wat_to_utc(dt: datetime) -> datetime:
    """Convert WAT datetime to UTC for cron jobs."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=WAT)
    return dt.astimezone(timezone.utc)

def is_freeze_window() -> bool:
    """True between Sunday 16:00 WAT and Monday 00:00 WAT."""
    now = now_wat()
    # Sunday in Python: weekday() == 6
    if now.weekday() == 6 and now.hour >= 16:
        return True
    return False

def current_week_start() -> date:
    """Returns the Monday (WAT) of the current raid week as a date."""
    now = now_wat()
    days_since_monday = now.weekday()
    monday = (now - timedelta(days=days_since_monday)).date()
    return monday

# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _blank_bucket() -> dict:
    return {"tg":0, "x":0, "reset_at": _now()}

def _blank_x_data() -> dict:
    return {"last_sync":None, "last_post_drop":None,
            "personal_post_history":[], "credited_engagements":{}}

def display_name(u: dict) -> str:
    return u.get("username") or u.get("first_name") or "Unknown"

def period_total(u: dict, period: str) -> int:
    b = u.get("scores", {}).get(period, _blank_bucket())
    return b.get("tg", 0) + b.get("x", 0)

def x_week_pts(u: dict) -> int:
    return u.get("scores", {}).get("week", {}).get("x", 0)

PERIOD_META = {
    "day":     ("📅", "Today"),
    "week":    ("📆", "This Week"),
    "month":   ("🗓️", "This Month"),
    "alltime": ("🏆", "All-Time"),
}
MEDALS = ["🥇","🥈","🥉"] + ["🔸"] * 47

# ─────────────────────────────────────────────────────────────────────────────
#  PERMISSION GUARDS
# ─────────────────────────────────────────────────────────────────────────────

def is_owner(uid: int) -> bool:
    return uid == OWNER_ID

async def is_group_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    if update.effective_chat.type == "private":
        return False
    try:
        admins = await ctx.bot.get_chat_administrators(update.effective_chat.id)
        return any(a.user.id == update.effective_user.id for a in admins)
    except Exception:
        return False

async def guard_group(update: Update) -> bool:
    chat = update.effective_chat
    if chat.type == "private":
        return True
    if chat.id != RAID_GROUP_ID:
        try:
            await update.effective_message.reply_text(
                "⛔ PomRaid only operates in the official POM Army group."
            )
        except Exception:
            pass
        return False
    return True

async def guard_owner(update: Update) -> bool:
    if not is_owner(update.effective_user.id):
        await update.effective_message.reply_text(
            "🔒 This command is restricted to the bot owner."
        )
        return False
    return True

async def guard_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    if is_owner(update.effective_user.id):
        return True
    if await is_group_admin(update, ctx):
        return True
    await update.effective_message.reply_text(
        "🔒 This command is for group admins only."
    )
    return False

# ─────────────────────────────────────────────────────────────────────────────
#  X API CLIENT
# ─────────────────────────────────────────────────────────────────────────────

async def _track_api(cost: float = API_COST_TWEET_READ) -> None:
    """Record an API call's estimated cost."""
    try:
        await record_api_spend(calls=1, cost_usd=cost)
    except Exception as e:
        logger.warning(f"API spend tracking failed: {e}")


def x_get(url: str, params: dict | None = None) -> dict | None:
    """Synchronous X API GET. Returns parsed JSON or None."""
    if not X_BEARER_TOKEN:
        return None
    try:
        r = httpx.get(
            url,
            headers={"Authorization": f"Bearer {X_BEARER_TOKEN}"},
            params=params or {},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json()
        logger.warning(f"X API {url} returned {r.status_code}: {r.text[:200]}")
        return None
    except Exception as e:
        logger.error(f"X API error for {url}: {e}")
        return None


def get_x_user(handle: str) -> tuple[str | None, int, str]:
    """
    Look up an X user by handle.
    Returns (user_id, followers_count, error_message).
    """
    handle = handle.lstrip("@").strip()
    if not X_BEARER_TOKEN:
        return None, 0, "X_BEARER_TOKEN not set on server"
    try:
        r = httpx.get(
            f"https://api.twitter.com/2/users/by/username/{handle}",
            headers={"Authorization": f"Bearer {X_BEARER_TOKEN}"},
            params={"user.fields": "public_metrics"},
            timeout=15,
        )
        # Track API spend
        asyncio.create_task(_track_api(API_COST_USER_LOOKUP))
        if r.status_code == 401:
            return None, 0, "X API token invalid or expired (HTTP 401)"
        if r.status_code == 403:
            return None, 0, "X API token lacks permission (HTTP 403)"
        if r.status_code == 429:
            return None, 0, "X API rate-limited — wait 15 min and try again"
        if r.status_code == 402:
            return None, 0, "X API requires payment (HTTP 402) — check billing"
        if r.status_code != 200:
            return None, 0, f"X API returned HTTP {r.status_code}"
        data = r.json()
        if "data" not in data:
            err = "User not found on X"
            if "errors" in data and data["errors"]:
                err = data["errors"][0].get("detail", err)
            return None, 0, err
        user = data["data"]
        followers = user.get("public_metrics", {}).get("followers_count", 0)
        return user["id"], followers, ""
    except httpx.TimeoutException:
        return None, 0, "X API timed out"
    except Exception as e:
        logger.error(f"get_x_user error: {e}")
        return None, 0, f"X API error: {type(e).__name__}"


def get_tweet_data(tweet_id: str) -> tuple[str | None, str]:
    data = x_get(
        f"https://api.twitter.com/2/tweets/{tweet_id}",
        {"tweet.fields": "author_id,text"},
    )
    asyncio.create_task(_track_api(API_COST_TWEET_READ))
    if not data or "data" not in data:
        return None, ""
    return data["data"].get("author_id"), data["data"].get("text", "")


def tweet_is_pom_related(text: str) -> bool:
    t = (text or "").lower()
    return "$pom" in t or "@pom_bsc" in t


def get_likers(tweet_id: str) -> list[str]:
    data = x_get(
        f"https://api.twitter.com/2/tweets/{tweet_id}/liking_users",
        {"max_results": 100},
    )
    asyncio.create_task(_track_api(API_COST_ENGAGEMENT))
    return [u["id"] for u in data.get("data", [])] if data else []


def get_retweeters(tweet_id: str) -> list[str]:
    data = x_get(
        f"https://api.twitter.com/2/tweets/{tweet_id}/retweeted_by",
        {"max_results": 100},
    )
    asyncio.create_task(_track_api(API_COST_ENGAGEMENT))
    return [u["id"] for u in data.get("data", [])] if data else []


def get_replies_and_quotes(tweet_id: str) -> tuple[list[str], list[str]]:
    r = x_get(
        "https://api.twitter.com/2/tweets/search/recent",
        {"query": f"conversation_id:{tweet_id} is:reply",
         "max_results": 100, "tweet.fields": "author_id"},
    )
    asyncio.create_task(_track_api(API_COST_ENGAGEMENT))
    q = x_get(
        "https://api.twitter.com/2/tweets/search/recent",
        {"query": f"url:{tweet_id} is:quote",
         "max_results": 100, "tweet.fields": "author_id"},
    )
    asyncio.create_task(_track_api(API_COST_ENGAGEMENT))
    return (
        list({t["author_id"] for t in r.get("data", [])} if r else []),
        list({t["author_id"] for t in q.get("data", [])} if q else []),
    )


def get_pom_price() -> float:
    """Fetch live $POM price from DexScreener (free, doesn't count toward X API spend)."""
    try:
        r = httpx.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{POM_CA}",
            timeout=10,
        )
        if r.status_code == 200:
            pairs = r.json().get("pairs", [])
            if pairs:
                return float(pairs[0].get("priceUsd", 0))
    except Exception as e:
        logger.error(f"DexScreener price fetch error: {e}")
    return 0.0


def send_pom_tokens(to_address: str, token_amount: float) -> str | None:
    """Sign and broadcast $POM transfer. Returns tx hash or None."""
    if not PAY_WALLET_KEY:
        logger.error("PAY_WALLET_KEY not set")
        return None
    try:
        w3       = Web3(Web3.HTTPProvider(BSC_RPC))
        contract = w3.eth.contract(address=POM_CA, abi=ERC20_ABI)
        decimals = contract.functions.decimals().call()
        amount   = int(token_amount * (10 ** decimals))
        account  = w3.eth.account.from_key(PAY_WALLET_KEY)

        tx = contract.functions.transfer(
            Web3.to_checksum_address(to_address), amount
        ).build_transaction({
            "from":     account.address,
            "nonce":    w3.eth.get_transaction_count(account.address),
            "gas":      200_000,
            "gasPrice": w3.eth.gas_price,
            "chainId":  56,
        })
        signed  = w3.eth.account.sign_transaction(tx, PAY_WALLET_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        return tx_hash.hex() if receipt.status == 1 else None
    except Exception as e:
        logger.error(f"POM send error: {e}")
        return None

# ─────────────────────────────────────────────────────────────────────────────
#  SYNC ENGINE
# ─────────────────────────────────────────────────────────────────────────────

async def sync_x_engagement() -> dict:
    """Sync registered raids: check engagement, award points."""
    global POM_X_ID
    summary = {"tweets":0, "engagements":0, "errors":[]}

    if not X_BEARER_TOKEN:
        summary["errors"].append("X_BEARER_TOKEN not set")
        return summary

    if not POM_X_ID:
        POM_X_ID, _, err = get_x_user(POM_X_HANDLE)
        if not POM_X_ID:
            summary["errors"].append(f"Cannot resolve @{POM_X_HANDLE}: {err}")
            return summary

    # Build x_user_id → user_dict map; resolve missing x_user_ids
    all_users = await get_all_users()
    xid_map: dict[str, dict] = {}
    for u in all_users:
        if u.get("x_handle") and not u.get("x_user_id"):
            xid, fl, _err = get_x_user(u["x_handle"])
            if xid:
                u["x_user_id"]   = xid
                u["x_followers"] = fl
                await save_user(u)
        if u.get("x_user_id"):
            xid_map[u["x_user_id"]] = u

    raids = await get_raids()
    for tweet_id, info in raids.items():
        summary["tweets"] += 1
        dropper_xid = info.get("tweet_author")

        likers     = get_likers(tweet_id)
        retweeters = get_retweeters(tweet_id)
        repliers, quoters = get_replies_and_quotes(tweet_id)

        for action, xid_list in [
            ("like",    likers),
            ("repost",  retweeters),
            ("comment", repliers),
            ("quote",   quoters),
        ]:
            for xid in xid_list:
                u = xid_map.get(xid)
                if not u:
                    continue
                if dropper_xid and xid == dropper_xid:
                    continue  # don't credit self-engagement
                tg_uid = u["tg_uid"]
                if await is_engagement_credited(tweet_id, tg_uid, action):
                    continue
                await add_x_points_db(tg_uid, X_POINTS[action])
                await credit_engagement(tweet_id, tg_uid, action)
                summary["engagements"] += 1

    await set_meta("last_x_sync", _now())
    return summary

# ─────────────────────────────────────────────────────────────────────────────
#  REWARD ENGINE
# ─────────────────────────────────────────────────────────────────────────────

async def compute_weekly_rewards() -> tuple[dict, list[dict]]:
    """
    Returns (top10_dict, raffle_eligible_list).
    top10_dict: {uid: reward_info, ...} for top 10 qualifiers
    raffle_eligible_list: [{...}, ...] for users who hit threshold but didn't make top 10

    Threshold: a user's X points must be ≥ 70% of the top scorer's X points to qualify.
    """
    lb = await get_period_leaderboard("week", limit=100)
    if not lb:
        return {}, []

    top_x_pts = max((r["x_pts"] for r in lb), default=0)
    if top_x_pts <= 0:
        return {}, []

    threshold = top_x_pts * QUALIFY_PCT
    qualifiers = [r for r in lb if r["x_pts"] >= threshold]

    top_10     = qualifiers[:TOP_N]
    raffle_pool = qualifiers[TOP_N:]

    if not top_10:
        return {}, []

    price    = get_pom_price()
    rollover = float(await get_meta("rollover_usd", 0.0) or 0.0)

    results = {}
    for rank, r in enumerate(top_10, 1):
        uid = str(r["tg_uid"])
        usd = TIER_AMOUNTS.get(rank, 5)
        results[uid] = {
            "rank":         rank,
            "username":     r.get("username") or r.get("first_name") or "Unknown",
            "tg_uid":       r["tg_uid"],
            "points":       r["total"],
            "x_pts":        r["x_pts"],
            "usd_amount":   usd,
            "token_amount": (usd / price) if price > 0 else 0,
            "wallet":       r.get("wallet"),
            "paid":         False,
            "tx_hash":      None,
        }

    # Roll unpaid amounts forward — for now we always pay exactly $88 for top 10
    # Any leftover (e.g. no top10 winners or someone without wallet) rolls
    paid_out_total = sum(TIER_AMOUNTS.get(r+1, 5) for r in range(len(top_10)))
    pool           = WEEKLY_POOL_USD - 12.0  # subtract raffle pool first
    new_rollover   = max(0.0, pool - paid_out_total)
    await set_meta("rollover_usd", new_rollover + rollover)

    return results, raffle_pool

# ─────────────────────────────────────────────────────────────────────────────
#  BATTLE ROYALE DEATH LINES
# ─────────────────────────────────────────────────────────────────────────────

DEATH_LINES = [
    "💀 @{user} tripped over their own tail and died.",
    "💀 @{user} got bonked by a flying bone. Out cold.",
    "💀 @{user} was distracted by treats. Eliminated.",
    "💀 @{user} barked too loud and lost their voice.",
    "💀 @{user} chased their tail one too many times. Dizzy. Gone.",
    "💀 @{user} fell asleep on the battlefield. Snoring.",
    "💀 @{user} stepped on a squeaky toy and gave away their position.",
    "💀 @{user} mistook a cat for a friend. Big mistake.",
    "💀 @{user} ran into a wall chasing a butterfly.",
    "💀 @{user} ate too much kibble pre-fight. Sluggish. Eliminated.",
    "💀 @{user} was distracted by the mailman.",
    "💀 @{user} forgot to charge their POM energy. Powered down.",
    "💀 @{user} tried to high-five with paws. It didn't end well.",
    "💀 @{user} thought the battlefield was for fetch. Bad call.",
    "💀 @{user} barked at their own shadow and tripped.",
    "💀 @{user} sniffed the wrong tree at the wrong time.",
    "💀 @{user} tried to dab. Fell over.",
    "💀 @{user} got tackled by @{killer}.",
    "💀 @{user} got out-fluffed by @{killer}.",
    "💀 @{user} got rugged by @{killer}.",
    "💀 @{user} got out-memed by @{killer}.",
    "💀 @{user} got out-raided by @{killer} in the final stretch.",
    "💀 @{user} got bonked by @{killer} mid-bark.",
    "💀 @{user} sneezed at the worst possible moment.",
    "💀 @{user} made eye contact with the abyss. Couldn't look away.",
    "💀 @{user} got carried away by the wind. Too fluffy.",
    "💀 @{user} took a nap at the wrong time.",
    "💀 @{user} got distracted by their reflection.",
    "💀 @{user} forgot they were in a battle and went to find snacks.",
    "💀 @{user} got out-paw'd by @{killer}.",
]

def pick_death_line(victim: str, killer: str = None) -> str:
    """Return a random death line. If killer needed but not given, retry."""
    for _ in range(5):
        line = random.choice(DEATH_LINES)
        if "{killer}" in line and not killer:
            continue
        if killer:
            return line.format(user=victim, killer=killer)
        return line.format(user=victim)
    # Fallback
    return f"💀 @{victim} fell in battle."

# ─────────────────────────────────────────────────────────────────────────────
#  BATTLE ROYALE EXECUTION
# ─────────────────────────────────────────────────────────────────────────────

async def run_battle_round(app: Application, fighters: list[dict],
                            round_num: int, total_rounds: int = 3,
                            duration_sec: int = 60) -> dict:
    """
    Run a single battle royale round.
    fighters: list of {uid, username, tickets} dicts
    Returns: the winner dict {uid, username, tickets}

    Posts and edits a single message through the whole battle (~60 sec),
    then deletes it. Caller posts the final reveal separately.
    """
    if not fighters:
        return None
    if len(fighters) == 1:
        return fighters[0]

    # Build weighted ticket pool
    ticket_pool = []
    for f in fighters:
        ticket_pool.extend([f] * max(1, f.get("tickets", 1)))

    # The actual winner — picked NOW, drama is just window dressing
    winner = random.choice(ticket_pool)

    # Everyone else dies in random order
    survivors = [f for f in fighters if f["uid"] != winner["uid"]]
    death_order = list(survivors)
    random.shuffle(death_order)

    n_deaths = len(death_order)

    # Send initial battle message
    intro_text = (
        f"🐺 <b>ROUND {round_num} — BATTLE COMMENCES</b>\n"
        f"{DIV}\n\n"
        f"{len(fighters)} fighters enter the arena...\n"
        f"The crowd holds its breath."
    )
    try:
        msg = await app.bot.send_message(
            chat_id=RAID_GROUP_ID, text=intro_text, parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"battle intro failed: {e}")
        return winner

    await asyncio.sleep(4)

    # Pace eliminations across ~ duration_sec - 8 seconds (4s intro, 4s final)
    available_time = max(20, duration_sec - 8)
    per_kill_delay = max(2.0, available_time / max(1, n_deaths))

    fighters_alive = list(fighters)
    for i, victim in enumerate(death_order):
        # Pick a random killer from still-alive fighters (excluding the victim)
        killer_options = [f["username"] for f in fighters_alive
                          if f["uid"] != victim["uid"]]
        killer = random.choice(killer_options) if killer_options else None

        line = pick_death_line(victim["username"], killer)
        fighters_alive = [f for f in fighters_alive if f["uid"] != victim["uid"]]

        remaining_text = f"\n\n{len(fighters_alive)} fighters remain."
        if len(fighters_alive) == 2:
            remaining_text = "\n\n🔥 <b>FINAL 2 REMAIN</b> 🔥"

        battle_text = (
            f"🐺 <b>ROUND {round_num} — IN COMBAT</b>\n"
            f"{DIV}\n\n"
            f"{line}"
            f"{remaining_text}"
        )

        try:
            await app.bot.edit_message_text(
                chat_id=msg.chat.id, message_id=msg.message_id,
                text=battle_text, parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning(f"battle edit failed: {e}")

        # Final 2 → pause longer for drama
        delay = per_kill_delay
        if len(fighters_alive) == 2:
            delay = min(delay + 3, 6)
        await asyncio.sleep(delay)

    # Delete the battle message — only winner reveal stays
    try:
        await app.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
    except Exception as e:
        logger.warning(f"battle delete failed: {e}")

    return winner


# ─────────────────────────────────────────────────────────────────────────────
#  KEYBOARDS
# ─────────────────────────────────────────────────────────────────────────────

def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 My Score",       callback_data="score"),
         InlineKeyboardButton("📋 My Stats",       callback_data="stats")],
        [InlineKeyboardButton("🏆 Leaderboard",    callback_data="lb_alltime"),
         InlineKeyboardButton("❓ How to Earn",    callback_data="howto")],
        [InlineKeyboardButton("💰 Reward Status",  callback_data="rewardstatus"),
         InlineKeyboardButton("🔥 My Streak",      callback_data="streak")],
        [InlineKeyboardButton("🐦 X Account",      callback_data="menu_x"),
         InlineKeyboardButton("👛 Wallet",         callback_data="menu_wallet")],
    ])

def kb_x_menu(d: dict) -> InlineKeyboardMarkup:
    if d.get("x_handle"):
        link_label = "🔄 Re-link X"
    else:
        link_label = "🔗 Link X Account"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(link_label,         callback_data="linkx_prompt")],
        [InlineKeyboardButton("🔄 Refresh X Data", callback_data="refreshx")],
        [InlineKeyboardButton("❌ Unlink X",       callback_data="unlinkx")],
        [InlineKeyboardButton("🔙 Back to Menu",   callback_data="back_main")],
    ])

def kb_wallet_menu(d: dict) -> InlineKeyboardMarkup:
    if d.get("wallet"):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("👛 View / Update Wallet", callback_data="wallet_info")],
            [InlineKeyboardButton("🗑️ Remove Wallet",       callback_data="unwallet")],
            [InlineKeyboardButton("🔙 Back to Menu",         callback_data="back_main")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👛 Set Wallet",      callback_data="wallet_info")],
        [InlineKeyboardButton("🔙 Back to Menu",    callback_data="back_main")],
    ])

def kb_lb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏆 All-Time", callback_data="lb_alltime"),
         InlineKeyboardButton("🗓️ Month",   callback_data="lb_month")],
        [InlineKeyboardButton("📆 Week",    callback_data="lb_week"),
         InlineKeyboardButton("📅 Today",   callback_data="lb_day")],
        [InlineKeyboardButton("🔙 Back",    callback_data="back_main")],
    ])

def kb_score() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="lb_alltime"),
         InlineKeyboardButton("📋 Full Stats",  callback_data="stats")],
        [InlineKeyboardButton("💰 Rewards",     callback_data="rewardstatus"),
         InlineKeyboardButton("🔙 Back",        callback_data="back_main")],
    ])

def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_main")]])

def kb_x_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to X Menu", callback_data="menu_x")]])

def kb_wallet_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Wallet Menu", callback_data="menu_wallet")]])

def kb_confirm(action: str, return_to: str = "back_main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes", callback_data=f"{action}_confirm"),
         InlineKeyboardButton("❌ Cancel", callback_data=return_to)],
    ])

# ─────────────────────────────────────────────────────────────────────────────
#  RATE LIMITER
# ─────────────────────────────────────────────────────────────────────────────

_msg_times: dict[int, list[datetime]] = defaultdict(list)

def is_flooding(uid: int) -> bool:
    now    = datetime.utcnow()
    cutoff = now - timedelta(minutes=1)
    times  = [t for t in _msg_times[uid] if t > cutoff]
    times.append(now)
    _msg_times[uid] = times
    return len(times) > SPAM["max_per_minute"]


# ─────────────────────────────────────────────────────────────────────────────
#  RENDER FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def render_start(first_name: str) -> str:
    return (
        f"🐶 <b>Welcome to PomRaid, {esc(first_name)}!</b>\n"
        f"{DIV}\n\n"
        "The official activity &amp; raid tracker for the\n"
        "<b>$POM Army</b> on BNB Chain 🚀\n\n"
        "Earn points by staying active here and raiding on X.\n"
        "Top raiders get rewarded in <b>$POM tokens</b> every Sunday.\n\n"
        f"{DIV2}\n"
        "🚀 <b>Quick Start:</b>\n"
        "1. Link your X → /linkx\n"
        "2. Set payout wallet → /wallet\n"
        "3. Check in daily → /checkin\n"
        "4. How to earn → /howto\n\n"
        f"{DIV}\n"
        "<i>Stay loud. Stay based. POM Army never sleeps.</i> 🔥"
    )


def render_help(is_adm: bool, is_own: bool) -> str:
    lines = [
        "📖 <b>PomRaid Help</b>",
        DIV, "",
        "🏠 <b>Getting Started</b>",
        "/start — Open main menu",
        "/howto — How to earn (quick)",
        "/rules — Full rules &amp; point breakdown",
        "/list — Show all commands",
        "",
        DIV2,
        "📊 <b>Your Stats</b>",
        "/score — Your score card",
        "/stats — Full profile",
        "/streak — Your daily check-in streak",
        "/rewardstatus — Weekly reward eligibility",
        "/weeklypreview — Full weekly standings",
        "",
        DIV2,
        "🏆 <b>Leaderboard</b>",
        "/lb — All-time top 50",
        "/lb week | month | day — Period boards",
        "",
        DIV2,
        "🐦 <b>X Account</b>",
        "/linkx @handle — Link your X account",
        "/unlinkx — Unlink your X account",
        "/refreshx — Refresh follower count",
        "",
        DIV2,
        "💰 <b>Rewards</b>",
        "/wallet 0x… — Set BNB payout wallet",
        "/unwallet — Remove your wallet",
        "",
        DIV2,
        "🔥 <b>Daily Check-in</b>",
        "/checkin — Check in for the day, build your streak",
    ]
    if is_adm or is_own:
        lines += [
            "",
            DIV2,
            "🛡️ <b>Admin</b> (reply to user's message)",
            "/ban — Ban a user",
            "/mute [mins] — Mute (default 10 min)",
            "/warn — Warn user (3 = auto-ban)",
            "/announce — Post &amp; pin announcement",
            "/tip @user &lt;amount&gt; — Tip in $POM or USD",
        ]
    if is_own:
        lines += [
            "",
            DIV2,
            "👑 <b>Owner Only</b>",
            "/syncx — Force sync X engagement",
            "/distribute — Run weekly top-10 payout",
            "/raffle round1|round2|round3|round4|status — Run raffle rounds",
            "/monthlydistribute — Run monthly loyalty payout",
            "/resetlb day|week|month|all — Reset leaderboard",
            "/resetoffenses — Clear user warnings (reply)",
            "/clearcooldown @user — Reset relink cooldown",
            "/spend — Show current X API spend",
            "/tiplog — Show recent tips",
        ]
    lines += ["", DIV]
    return "\n".join(lines)


def render_list(is_adm: bool, is_own: bool) -> str:
    return render_help(is_adm, is_own).replace("📖 <b>PomRaid Help</b>", "📋 <b>Commands</b>")


def render_howto() -> str:
    return (
        "❓ <b>How to Earn PomRaid Points</b>\n"
        f"{DIV}\n\n"
        "📣 <b>RAID @Pom_bsc POSTS</b>\n"
        "Bot alerts the group when POM posts on X.\n"
        "Go raid it and earn points:\n\n"
        f"  Like ............ +{X_POINTS['like']} pts\n"
        f"  Repost ......... +{X_POINTS['repost']} pts\n"
        f"  Comment ........ +{X_POINTS['comment']} pts\n"
        f"  Quote Tweet .... +{X_POINTS['quote']} pts\n\n"
        f"{DIV2}\n"
        "🔗 <b>DROP YOUR OWN POST</b>\n"
        "Paste your X link in the group.\n"
        "Must contain $POM or @Pom_bsc.\n\n"
        f"  ✅ Verified as yours → +{X_POINTS['post_drop']} pts\n"
        "  ✅ 12hr cooldown starts\n\n"
        f"{DIV2}\n"
        "💬 <b>TELEGRAM ACTIVITY</b>\n"
        f"Messages of {TG_MIN_WORDS}+ words earn TG points.\n"
        f"Daily cap: <b>{TG_DAILY_CAP} pts max</b>\n\n"
        f"{DIV2}\n"
        "🔥 <b>DAILY CHECK-IN</b>\n"
        "Type /checkin once per day to build your streak.\n"
        "Day 1 = +1 pt, Day 2 = +2, ... Day 5+ = +5 (cap)\n"
        "Streak resets if you miss a day.\n\n"
        f"{DIV2}\n"
        "💰 <b>WEEKLY REWARDS</b>\n"
        f"Every Sunday — <b>${int(WEEKLY_POOL_USD)}</b> pool in $POM.\n"
        f"Top {TOP_N} raiders + {RAFFLE_WINNERS} raffle winners.\n"
        "Must hit 70%+ of community X points to qualify.\n\n"
        "  👉 /wallet — set your BNB address\n"
        "  👉 /rewardstatus — check your standing\n"
        "  👉 /rules — full breakdown\n"
        f"{DIV}"
    )


def render_rules() -> str:
    return (
        "📜 <b>PomRaid — Full Rules &amp; Points</b>\n"
        f"{DIV}\n\n"
        "📣 <b>RAIDING @Pom_bsc OFFICIAL POSTS</b>\n"
        "Engage on X to earn points:\n\n"
        "<pre>"
        "Action         Points    Limit\n"
        "─────────────────────────────────\n"
        f"Like             +{X_POINTS['like']}      Once per post\n"
        f"Repost           +{X_POINTS['repost']}     Once per post\n"
        f"Comment          +{X_POINTS['comment']}     Once per post\n"
        f"Quote Tweet      +{X_POINTS['quote']}     Once per post"
        "</pre>\n\n"
        f"{DIV2}\n"
        "🔗 <b>PERSONAL X POSTS</b>\n"
        "Drop your X link in the group.\n\n"
        "Requirements:\n"
        "  ✅ Must mention $POM or @Pom_bsc\n"
        "  ✅ Verified as YOUR linked X account\n"
        f"  ✅ Min {PERSONAL_POST['min_words']} words\n"
        "  ✅ Must include image or video\n"
        f"  ✅ Min {PERSONAL_POST['min_followers']} followers on X\n"
        "  ✅ Original content\n"
        "  ✅ 12hr cooldown between drops\n\n"
        f"Reward: +{X_POINTS['post_drop']} pts to you\n\n"
        f"{DIV2}\n"
        "💬 <b>TELEGRAM ACTIVITY</b>\n"
        "<pre>"
        f"Words per message    Points\n"
        f"──────────────────────────\n"
        f"Under {TG_MIN_WORDS} words       0 pts\n"
        f"{TG_MIN_WORDS}–14 words          1 pt\n"
        f"15–29 words          2 pts\n"
        f"30+ words            3 pts\n"
        f"Daily cap            {TG_DAILY_CAP} pts"
        "</pre>\n\n"
        "TG points do NOT count toward the 70% reward threshold.\n\n"
        f"{DIV2}\n"
        "🔥 <b>DAILY CHECK-IN STREAK</b>\n"
        "<pre>"
        "Streak    Bonus\n"
        "───────────────\n"
        "Day 1       +1\n"
        "Day 2       +2\n"
        "Day 3       +3\n"
        "Day 4       +4\n"
        "Day 5+      +5"
        "</pre>\n"
        "Awarded as TG points. Resets if you miss a day.\n\n"
        f"{DIV2}\n"
        "💰 <b>WEEKLY REWARDS</b> (Sunday 16:00 WAT)\n"
        f"Pool: <b>${int(WEEKLY_POOL_USD)}/week</b> in $POM\n\n"
        "Qualification: 70%+ of top scorer's X points.\n\n"
        "<pre>"
        "Top 10 Payouts\n"
        "──────────────────────\n"
        "🥇 1st             $25\n"
        "🥈 2nd             $18\n"
        "🥉 3rd             $14\n"
        "   4th             $11\n"
        "   5th             $9\n"
        "   6th             $8\n"
        "   7th             $7\n"
        "   8th             $6\n"
        "   9–10            $5 each\n"
        "──────────────────────\n"
        "Raffle (4 winners)  $4 each\n"
        "──────────────────────\n"
        "Total              $125"
        "</pre>\n\n"
        "Raffle = battle royale among qualifiers ranked 11+.\n\n"
        f"{DIV2}\n"
        "📅 <b>SCHEDULE (WAT)</b>\n"
        "  • Raid week: Mon 00:00 → Sun 16:00\n"
        "  • Payout window: Sun 16:00 → 24:00\n"
        "  • New week: Mon 00:00\n\n"
        f"{DIV2}\n"
        "🏅 <b>MONTHLY LOYALTY</b>\n"
        "Top 3 by 30-day points on the 1st of each month:\n"
        "  🏅 MVP            $15\n"
        "  🎖️ Runner-up      $10\n"
        "  🏵️ 3rd            $5\n"
        f"{DIV}"
    )


def render_score(d: dict) -> str:
    s    = d.get("scores", {})
    xd   = d.get("x_data", {})
    name = esc(display_name(d))

    if d.get("x_handle"):
        x_line = f"🐦 <b>@{esc(d['x_handle'])}</b>  •  {d.get('x_followers',0):,} followers"
    else:
        x_line = "🐦 X Account: <i>not linked</i> — use /linkx"

    last_drop = xd.get("last_post_drop")
    cd_line   = "✅ Post cooldown: <b>Ready to drop!</b>"
    if last_drop:
        try:
            elapsed   = datetime.utcnow() - datetime.fromisoformat(last_drop)
            remaining = timedelta(hours=LINK_COOLDOWN_HOURS) - elapsed
            if remaining.total_seconds() > 0:
                h = int(remaining.total_seconds() // 3600)
                m = int((remaining.total_seconds() % 3600) // 60)
                cd_line = f"⏳ Post cooldown: <b>{h}h {m}m</b> remaining"
        except Exception:
            pass

    streak = d.get("streak_days", 0)
    streak_line = f"🔥 Check-in streak: <b>{streak} days</b>" if streak else "🔥 Check-in streak: <i>none — type /checkin</i>"

    rows = []
    for key, (icon, label) in [
        ("day",     ("📅", "Today     ")),
        ("week",    ("📆", "This Week ")),
        ("month",   ("🗓", "This Month")),
        ("alltime", ("🏆", "All-Time  ")),
    ]:
        b     = s.get(key, _blank_bucket())
        total = b.get("tg",0) + b.get("x",0)
        rows.append(f"{icon} {label}  {b.get('tg',0):>5}  {b.get('x',0):>5}  {total:>7}")

    table = (
        "<pre>"
        "Period           TG      X    Total\n"
        "─────────────────────────────────────\n"
        + "\n".join(rows) +
        "</pre>"
    )

    return (
        f"📊 <b>Score Card — @{name}</b>\n"
        f"{DIV}\n\n"
        f"{x_line}\n"
        f"{cd_line}\n"
        f"{streak_line}\n\n"
        f"{table}\n"
        f"{DIV}\n"
        "<i>Use /stats for full profile</i>"
    )


def render_stats(d: dict, first_name: str) -> str:
    s     = d.get("scores", {})
    xd    = d.get("x_data", {})
    alltime = s.get("alltime", _blank_bucket())
    week    = s.get("week", _blank_bucket())
    total = alltime.get("tg",0) + alltime.get("x",0)
    wk    = week.get("tg",0) + week.get("x",0)

    if d.get("x_handle"):
        x_info = f"@{esc(d['x_handle'])} • {d.get('x_followers',0):,} followers"
    else:
        x_info = "<i>not linked</i>"

    eng = sum(len(v) for v in xd.get("credited_engagements", {}).values())
    if d.get("wallet"):
        wall = f"<code>{esc(d['wallet'][:8])}…{esc(d['wallet'][-6:])}</code>"
    else:
        wall = "<i>not set — use /wallet</i>"

    joined = d.get("joined", _now())[:10]
    streak = d.get("streak_days", 0)

    return (
        f"📋 <b>Full Profile — {esc(first_name)}</b>\n"
        f"{DIV}\n\n"
        f"👤 Handle: @{esc(display_name(d))}\n"
        f"🐦 X Account: {x_info}\n"
        f"💳 Wallet: {wall}\n"
        f"🔥 Streak: <b>{streak} days</b>\n"
        f"📅 Member since: {esc(joined)}\n\n"
        f"{DIV2}\n"
        "🏆 <b>Points Summary</b>\n"
        f"  All-time total: <b>{total:,} pts</b>\n"
        f"  This week: <b>{wk:,} pts</b>\n"
        f"  TG (all-time): {alltime.get('tg',0):,} pts\n"
        f"  X (all-time): {alltime.get('x',0):,} pts\n\n"
        f"{DIV2}\n"
        "📣 <b>Raid Activity</b>\n"
        f"  Engagements credited: <b>{eng}</b>\n"
        f"  Personal posts: <b>{len(xd.get('personal_post_history', []))}</b>\n\n"
        f"{DIV2}\n"
        f"⚠️ Warnings: <b>{d.get('offenses',0)}/3</b>\n"
        f"{DIV}\n"
        "<i>Use /rewardstatus to check eligibility</i>"
    )


def render_streak(d: dict) -> str:
    streak = d.get("streak_days", 0)
    last   = d.get("last_checkin_date")

    if streak == 0:
        return (
            "🔥 <b>Your Check-in Streak</b>\n"
            f"{DIV}\n\n"
            "Current streak: <b>0 days</b>\n\n"
            "<i>Type /checkin to start your streak!</i>\n\n"
            f"{DIV2}\n"
            "<b>How it works:</b>\n"
            "Day 1: +1 TG point\n"
            "Day 2: +2 TG points\n"
            "Day 3: +3 TG points\n"
            "Day 4: +4 TG points\n"
            "Day 5+: +5 TG points (max)\n\n"
            "<i>Miss a day → streak resets to 0.</i>"
        )

    today_str = "today" if last == datetime.utcnow().date() else f"last on {last}"
    bonus_today = STREAK_BONUS.get(streak, STREAK_CAP)

    next_bonus = STREAK_BONUS.get(streak + 1, STREAK_CAP)
    can_checkin = last != datetime.utcnow().date()

    next_line = (
        f"\n\n<i>Come back tomorrow for +{next_bonus} pts</i>"
        if not can_checkin else
        f"\n\n<i>Type /checkin to keep your streak alive!</i>"
    )

    return (
        "🔥 <b>Your Check-in Streak</b>\n"
        f"{DIV}\n\n"
        f"Current streak: <b>{streak} days</b>\n"
        f"Last check-in: {esc(str(last))}\n"
        f"Today's bonus: <b>+{bonus_today} TG points</b>"
        + next_line
    )


async def render_reward_status(tg_uid: int) -> str:
    d        = await get_or_create_user(tg_uid)
    wk_pts   = period_total(d, "week")
    wk_x_pts = x_week_pts(d)
    total_x  = await get_total_x_points_this_week()

    lb   = await get_period_leaderboard("week", limit=100)
    rank = next((i+1 for i, row in enumerate(lb) if row["tg_uid"] == tg_uid), "?")
    top_x_pts = max((r["x_pts"] for r in lb), default=0)

    threshold = top_x_pts * QUALIFY_PCT if top_x_pts > 0 else 0
    pct       = (wk_x_pts / top_x_pts * 100) if top_x_pts > 0 else 0
    qualifies = wk_x_pts >= threshold and threshold > 0

    if qualifies and isinstance(rank, int):
        if rank <= TOP_N:
            est        = TIER_AMOUNTS.get(rank, 5)
            status_box = f"✅ <b>QUALIFYING — Top {rank}</b>"
            reward_lines = [f"  Estimated reward: <b>${est}</b> in $POM"]
        else:
            status_box   = "🎰 <b>QUALIFIED FOR RAFFLE</b>"
            reward_lines = [
                f"  Current rank: <b>#{rank}</b>",
                f"  Eligible for raffle ({RAFFLE_WINNERS} winners × ${RAFFLE_WINNER_AMOUNT_USD:.0f})",
            ]
    else:
        needed       = max(0, int(threshold) - wk_x_pts)
        status_box   = "❌ <b>Not qualifying yet</b>"
        reward_lines = [f"  X points needed: <b>{needed:,} more</b>"]

    rollover = float(await get_meta("rollover_usd", 0.0) or 0.0)
    rollover_lines = [f"  Rollover from last week: <b>${rollover:.2f}</b>"] if rollover > 0 else []

    if d.get("wallet"):
        wall_line = f"  ✅ <code>{esc(d['wallet'][:8])}…{esc(d['wallet'][-6:])}</code>"
    else:
        wall_line = "  ⚠️ <i>Not set!</i> Use /wallet before Sunday"

    top10_total  = sum(TIER_AMOUNTS.values())                          # $108
    raffle_total = RAFFLE_WINNERS * RAFFLE_WINNER_AMOUNT_USD            # $16
    lines = [
        "💰 <b>Weekly Reward Status</b>",
        DIV, "",
        status_box, "",
        DIV2,
        "📊 <b>Your Position</b>",
        f"  This week total: <b>{wk_pts:,} pts</b>",
        f"  X points: <b>{wk_x_pts:,}</b>",
        f"  Top scorer's X: <b>{top_x_pts:,}</b>",
        f"  Qualify threshold (70% of top): <b>{int(threshold):,}</b>",
        f"  Your % of top scorer: <b>{pct:.1f}%</b>",
    ] + reward_lines + [
        "",
        DIV2,
        "💵 <b>This Week's Pool</b>",
        f"  Top 10: <b>${top10_total:.0f}</b>",
        f"  Raffle ({RAFFLE_WINNERS} winners): <b>${raffle_total:.0f}</b>",
    ] + rollover_lines + [
        f"  Total: <b>${WEEKLY_POOL_USD + rollover:.2f}</b>",
        "",
        DIV2,
        "💳 <b>Payout Wallet</b>",
        wall_line,
        DIV,
        "<i>Rewards distributed every Sunday after 16:00 WAT</i>",
    ]
    return "\n".join(lines)


async def render_leaderboard(period: str) -> str:
    icon, label = PERIOD_META[period]
    rows = await get_period_leaderboard(period, limit=50)

    if not rows:
        return (
            f"{icon} <b>{esc(label)} Leaderboard</b>\n"
            f"{DIV}\n\n"
            "<i>No activity recorded yet for this period.</i>\n\n"
            "Be the first to raid and claim the top spot! 🔥"
        )

    total_pts = sum(r["total"] for r in rows)
    lines = [
        f"{icon} <b>{esc(label)} — POM Army Leaderboard</b>",
        DIV,
        f"👥 Active raiders: <b>{len(rows)}</b> | 🎯 Total pts: <b>{total_pts:,}</b>",
        DIV2, "",
    ]
    for i, r in enumerate(rows):
        name  = esc(r.get("username") or r.get("first_name") or "Unknown")
        total = r["total"]
        tg    = r["tg_pts"]
        x     = r["x_pts"]
        pct   = f"{total/total_pts*100:.0f}%" if total_pts > 0 else "0%"
        x_tag = f" 🐦{x}" if r.get("x_handle") and x > 0 else ""
        lines.append(f"{MEDALS[i]} <code>{name}</code> — <b>{total:,} pts</b> ({pct})")
        lines.append(f"    TG: {tg} | X: {x}{x_tag}")
    lines += ["", "", DIV, "<i>Resets every Sunday 16:00 WAT</i>"]
    return "\n".join(lines)


async def render_weeklypreview(tg_uid: int, is_owner_view: bool) -> str:
    rows = await get_period_leaderboard("week", limit=100)
    top_x_pts = max((r["x_pts"] for r in rows), default=0)
    threshold = top_x_pts * QUALIFY_PCT if top_x_pts > 0 else 0
    total_x = sum(r["x_pts"] for r in rows)

    enriched = [(str(r["tg_uid"]), r, r["total"], r["x_pts"]) for r in rows]

    qualifying = [t for t in enriched if t[3] >= threshold]
    top10      = qualifying[:TOP_N]
    raffle     = qualifying[TOP_N:]
    not_yet    = [t for t in enriched if t[3] <  threshold]

    c_rank = next((i+1 for i,(uid,_,_,_) in enumerate(enriched) if uid==str(tg_uid)), None)
    c_xp   = next((xp for uid,_,_,xp in enriched if uid==str(tg_uid)), 0)

    if c_rank and c_xp >= threshold and threshold > 0:
        if c_rank <= TOP_N:
            est = TIER_AMOUNTS.get(c_rank, 5)
            your_line = f"✅ <b>You: Rank #{c_rank} — Est. ${est}</b>"
        else:
            your_line = f"🎰 <b>You: Rank #{c_rank} — In Raffle</b>"
    elif c_rank:
        needed = max(0, int(threshold) - c_xp)
        your_line = f"⏳ <b>You: Rank #{c_rank} — Need {needed:,} more X pts</b>"
    else:
        your_line = "<i>No activity yet this week</i>"

    lines = [
        "📊 <b>Weekly Standings Preview</b>",
        DIV,
        f"🌐 Community X points: <b>{total_x:,}</b>",
        f"👑 Top scorer's X: <b>{top_x_pts:,}</b>",
        f"📏 Qualify threshold (70% of top): <b>{int(threshold):,}</b>",
        "📅 Raid ends: <b>Sunday 16:00 WAT</b>",
        DIV2,
        your_line,
        DIV2,
        f"🏆 <b>Top {min(len(top10), TOP_N)} (Payout)</b>" if top10 else "<i>No qualifiers yet</i>",
    ]

    for i, (uid, r, tot, xp) in enumerate(top10, 1):
        name = esc(r.get("username") or r.get("first_name") or "Unknown")
        est  = TIER_AMOUNTS.get(i, 5)
        wall = " 💳" if r.get("wallet") else (" ⚠️" if is_owner_view else "")
        lines.append(f"{MEDALS[i-1]} @{name} — <b>{xp:,} X</b> — <b>${est}</b>{wall}")

    if raffle:
        lines += ["", DIV2, f"🎰 <b>Raffle Pool ({len(raffle)} entrants)</b>"]
        for uid, r, tot, xp in raffle[:10]:
            name = esc(r.get("username") or r.get("first_name") or "Unknown")
            lines.append(f"  🎫 @{name} — {xp:,} X pts")
        if len(raffle) > 10:
            lines.append(f"  <i>… and {len(raffle)-10} others</i>")

    if not_yet and is_owner_view:
        lines += ["", DIV2, "❌ <b>Not Qualifying Yet</b>"]
        for uid, r, tot, xp in not_yet[:5]:
            name   = esc(r.get("username") or r.get("first_name") or "Unknown")
            needed = max(0, int(threshold) - xp)
            lines.append(f"  • @{name} — {xp:,} pts (need {needed:,} more)")
        if len(not_yet) > 5:
            lines.append(f"  <i>… and {len(not_yet)-5} others</i>")

    lines += [
        "", DIV,
        ("<i>Run /distribute Sunday 16:00 WAT</i>" if is_owner_view
         else "<i>Keep raiding! Payouts every Sunday.</i>"),
    ]
    return "\n".join(lines)


def render_wallet_info(d: dict) -> str:
    if d.get("wallet"):
        wall   = f"<code>{esc(d['wallet'])}</code>"
        status = "✅ Wallet is set. You're eligible for automatic payouts."
    else:
        wall   = "<i>Not set</i>"
        status = "⚠️ No wallet set. You won't receive rewards until you add one."

    return (
        "💳 <b>Your Payout Wallet</b>\n"
        f"{DIV}\n\n"
        f"BNB Chain Address:\n{wall}\n\n"
        f"{status}\n\n"
        f"{DIV2}\n"
        "To set or update your wallet:\n"
        "<code>/wallet 0xYourBNBWalletAddress</code>\n\n"
        f"{DIV2}\n"
        "⚠️ <b>Important:</b>\n"
        "• Must be a BNB Chain (BSC) address\n"
        "• Must start with 0x\n"
        "• Double-check — payments are irreversible\n"
        "• Use /unwallet to remove it\n"
        f"{DIV}"
    )


def render_x_menu(d: dict) -> str:
    if d.get("x_handle"):
        status_lines = [
            f"✅ <b>Linked:</b> @{esc(d['x_handle'])}",
            f"👥 <b>Followers:</b> {d.get('x_followers', 0):,}",
        ]
        action_hint = "Choose an action below:"
    else:
        status_lines = [
            "❌ <b>No X account linked</b>",
            "<i>You need to link X to earn raid points.</i>",
        ]
        action_hint = "Tap <b>Link X Account</b> to get started:"

    return (
        "🐦 <b>X Account</b>\n"
        f"{DIV}\n\n"
        + "\n".join(status_lines)
        + f"\n\n{DIV2}\n"
        f"{action_hint}\n\n"
        "🔗 <b>Link X</b> — connect your X handle\n"
        "🔄 <b>Refresh X Data</b> — update follower count\n"
        "❌ <b>Unlink X</b> — remove your X account\n"
        f"{DIV}"
    )


def render_wallet_menu(d: dict) -> str:
    if d.get("wallet"):
        addr = d["wallet"]
        status_lines = [
            "✅ <b>Wallet Set</b>",
            f"<code>{esc(addr[:8])}…{esc(addr[-6:])}</code>",
            "",
            "<i>You're eligible to receive $POM payouts.</i>",
        ]
        action_hint = "Choose an action below:"
    else:
        status_lines = [
            "⚠️ <b>No wallet set</b>",
            "",
            "<i>You won't receive rewards until you add one.</i>",
        ]
        action_hint = "Tap <b>Set Wallet</b> to add one:"

    return (
        "👛 <b>Wallet</b>\n"
        f"{DIV}\n\n"
        + "\n".join(status_lines)
        + f"\n\n{DIV2}\n"
        f"{action_hint}\n\n"
        "👛 <b>Set / View Wallet</b> — add or update BNB address\n"
        "🗑️ <b>Remove Wallet</b> — unlink your address\n"
        f"{DIV}"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  USER COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

HTML = ParseMode.HTML
BANNER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pomraid_banner.png")


async def _reply(update: Update, text: str, kb=None, preview=False):
    await update.effective_message.reply_text(
        text, parse_mode=HTML, reply_markup=kb,
        disable_web_page_preview=not preview,
    )


async def _reply_and_delete(update: Update, text: str, delay: int = 60):
    """Reply and auto-delete after N seconds. For checkin etc."""
    try:
        msg = await update.effective_message.reply_text(
            text, parse_mode=HTML, disable_web_page_preview=True,
        )
        async def delete_later():
            await asyncio.sleep(delay)
            try:
                await msg.delete()
            except Exception:
                pass
        asyncio.create_task(delete_later())
    except Exception as e:
        logger.warning(f"_reply_and_delete failed: {e}")


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u = update.effective_user
    await get_or_create_user(u.id, u.username or "", u.first_name or "")
    caption = render_start(u.first_name or "friend")
    try:
        with open(BANNER_PATH, "rb") as banner:
            await update.effective_message.reply_photo(
                photo=banner, caption=caption, parse_mode=HTML,
                reply_markup=kb_main(),
            )
    except FileNotFoundError:
        await _reply(update, caption, kb_main())
    except Exception as e:
        logger.warning(f"Banner send failed: {e}")
        await _reply(update, caption, kb_main())


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u   = update.effective_user
    own = is_owner(u.id)
    adm = own or await is_group_admin(update, ctx)
    await _reply(update, render_help(adm, own), kb_main())


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u   = update.effective_user
    own = is_owner(u.id)
    adm = own or await is_group_admin(update, ctx)
    await _reply(update, render_list(adm, own), kb_back())


async def cmd_howto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    await _reply(update, render_howto(), kb_back())


async def cmd_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    await _reply(update, render_rules(), kb_back())


async def cmd_score(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u = update.effective_user
    d = await get_or_create_user(u.id, u.username or "", u.first_name or "")
    await _reply(update, render_score(d), kb_score())


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u = update.effective_user
    d = await get_or_create_user(u.id, u.username or "", u.first_name or "")
    await _reply(update, render_stats(d, u.first_name or "friend"), kb_back())


async def cmd_streak(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u = update.effective_user
    d = await get_or_create_user(u.id, u.username or "", u.first_name or "")
    await _reply(update, render_streak(d), kb_back())


async def cmd_rewardstatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u = update.effective_user
    await get_or_create_user(u.id, u.username or "", u.first_name or "")
    text = await render_reward_status(u.id)
    await _reply(update, text, kb_back())


async def cmd_weeklypreview(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u = update.effective_user
    await get_or_create_user(u.id, u.username or "", u.first_name or "")
    text = await render_weeklypreview(u.id, is_owner(u.id))
    if len(text) > 4000:
        text = text[:3950] + "\n<i>… message truncated</i>"
    await _reply(update, text, kb_back())


async def cmd_leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    raw    = ctx.args[0].lower() if ctx.args else "alltime"
    period = {"day":"day", "week":"week", "month":"month"}.get(raw, "alltime")
    text   = await render_leaderboard(period)
    if len(text) > 4000:
        text = text[:3950] + "\n<i>… use /lb for more</i>"
    await _reply(update, text, kb_lb())


async def cmd_checkin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Daily check-in. Group only, auto-deletes response after 60s."""
    # Must be in the group, not DM
    if update.effective_chat.type == "private":
        await _reply(update, "ℹ️ Check in from the POM Army group, not DM.", None)
        return
    if not await guard_group(update): return

    u = update.effective_user
    d = await get_or_create_user(u.id, u.username or "", u.first_name or "")

    today = datetime.utcnow().date()
    last  = d.get("last_checkin_date")
    current_streak = d.get("streak_days", 0) or 0

    # Already checked in today?
    if last == today:
        bonus = STREAK_BONUS.get(current_streak, STREAK_CAP) if current_streak > 0 else 1
        await _reply_and_delete(update,
            f"ℹ️ @{esc(display_name(d))} you already checked in today.\n"
            f"🔥 Streak: <b>{current_streak} days</b>",
        )
        return

    # Compute new streak: yesterday → +1; otherwise → reset to 1
    yesterday = today - timedelta(days=1)
    if last == yesterday:
        new_streak = current_streak + 1
    else:
        new_streak = 1

    # Capped bonus
    bonus = STREAK_BONUS.get(new_streak, STREAK_CAP)

    # Award TG points (these don't count toward 70% threshold)
    await add_tg_points_db(u.id, bonus)
    await update_streak(u.id, new_streak, today)

    # Build response
    if last and last != yesterday and current_streak > 0:
        # Streak reset
        msg = (
            f"✅ @{esc(display_name(d))} checked in!\n"
            f"🔥 Streak: <b>{new_streak} day</b> (reset from {current_streak})\n"
            f"🎯 +{bonus} TG point"
        )
    elif new_streak == 1:
        msg = (
            f"✅ @{esc(display_name(d))} checked in!\n"
            f"🔥 Streak: <b>1 day</b>\n"
            f"🎯 +{bonus} TG point"
        )
    else:
        plural = "s" if bonus != 1 else ""
        msg = (
            f"✅ @{esc(display_name(d))} checked in!\n"
            f"🔥 Streak: <b>{new_streak} days</b>\n"
            f"🎯 +{bonus} TG point{plural}"
        )

    await _reply_and_delete(update, msg)


async def cmd_linkx(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u = update.effective_user

    if not ctx.args:
        await _reply(update,
            "🐦 <b>Link Your X Account</b>\n\n"
            "Usage: <code>/linkx @yourhandle</code>\n\n"
            "<i>Example: /linkx @Glayzz_4T9ne_BK</i>\n\n"
            "Your X account is used to track raid engagement.")
        return

    handle = ctx.args[0].lstrip("@").strip()
    if not re.match(r"^[A-Za-z0-9_]{1,15}$", handle):
        await _reply(update,
            f"❌ <b>Invalid Handle</b>\n\n"
            f"<code>{esc(handle)}</code> doesn't look like a valid X handle.\n"
            "Letters, numbers, underscore. Max 15 chars.")
        return

    # Load user data — check cooldown rules
    d = await get_or_create_user(u.id, u.username or "", u.first_name or "")
    current_handle = d.get("x_handle")
    unlink_at, last_handle = await get_unlink_info(u.id)

    # If user is linking the SAME handle they had before, allow (no cooldown)
    is_same_as_previous = last_handle and last_handle.lower() == handle.lower()
    is_same_as_current  = current_handle and current_handle.lower() == handle.lower()

    if not is_same_as_previous and not is_same_as_current:
        # Check cooldown — only if they're linking a DIFFERENT handle
        if unlink_at:
            elapsed = datetime.utcnow() - unlink_at.replace(tzinfo=None)
            cooldown_remaining = timedelta(days=RELINK_COOLDOWN_DAYS) - elapsed
            if cooldown_remaining.total_seconds() > 0:
                hours = int(cooldown_remaining.total_seconds() // 3600)
                days = hours // 24
                hrs  = hours % 24
                await _reply(update,
                    f"⏳ <b>Relink Cooldown Active</b>\n\n"
                    f"You unlinked recently. You can only link the same handle "
                    f"(<code>@{esc(last_handle)}</code>) immediately.\n\n"
                    f"To link a different handle, wait <b>{days}d {hrs}h</b>.\n\n"
                    f"<i>Owner can clear with /clearcooldown if needed.</i>")
                return

    # Check handle is not taken by someone else
    other = await handle_taken_by_other(handle, u.id)
    if other:
        await _reply(update,
            f"❌ <b>Handle Already Linked</b>\n\n"
            f"<code>@{esc(handle)}</code> is linked to another POM Army member.\n\n"
            "<i>If this is your account and someone else took it, contact admin.</i>")
        return

    msg = await update.effective_message.reply_text(
        f"🔄 Verifying <b>@{esc(handle)}</b> on X…",
        parse_mode=HTML,
    )

    if not X_BEARER_TOKEN:
        # Skip verification — just save the handle unverified
        d["x_handle"]    = handle
        d["x_user_id"]   = None
        d["x_followers"] = 0
        await save_user(d)
        await msg.edit_text(
            f"⚠️ <b>X Linked (Unverified)</b>\n\n"
            f"🐦 @{esc(handle)}\n\n"
            "<i>X API not configured — handle saved but not verified.</i>",
            parse_mode=HTML, reply_markup=kb_back(),
        )
        return

    xid, followers, err = get_x_user(handle)

    if not xid:
        await msg.edit_text(
            f"❌ <b>Could Not Link @{esc(handle)}</b>\n\n"
            f"<b>Reason:</b> {esc(err)}\n\n"
            "<i>Check the handle and try again, or contact admin.</i>",
            parse_mode=HTML, reply_markup=kb_back(),
        )
        return

    if followers < PERSONAL_POST["min_followers"]:
        await msg.edit_text(
            f"❌ <b>Insufficient Followers</b>\n\n"
            f"@{esc(handle)} has <b>{followers}</b> followers.\n"
            f"Minimum required: <b>{PERSONAL_POST['min_followers']}</b>",
            parse_mode=HTML, reply_markup=kb_back(),
        )
        return

    d["x_handle"]    = handle
    d["x_user_id"]   = xid
    d["x_followers"] = followers
    await save_user(d)

    await msg.edit_text(
        f"✅ <b>X Account Linked!</b>\n\n"
        f"🐦 @{esc(handle)}\n"
        f"👥 Followers: <b>{followers:,}</b>\n\n"
        "You'll now earn points for:\n"
        "• Engaging with <b>@Pom_bsc</b> posts\n"
        "• Dropping your X posts in the group",
        parse_mode=HTML, reply_markup=kb_back(),
    )


async def cmd_unlinkx(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u = update.effective_user
    d = await get_or_create_user(u.id, u.username or "", u.first_name or "")
    if not d.get("x_handle"):
        await _reply(update,
            "ℹ️ You don't have an X account linked.\n\nUse /linkx to connect one.",
            kb_back())
        return
    await _reply(update,
        f"⚠️ <b>Confirm Unlink</b>\n\n"
        f"Unlink <b>@{esc(d['x_handle'])}</b>?\n\n"
        "Your points stay safe. But:\n"
        "• You can't earn X points while unlinked\n"
        f"• {RELINK_COOLDOWN_DAYS}-day cooldown to link a different handle\n"
        "• Re-linking the same handle = instant (no cooldown)\n\n"
        "<i>This cannot be undone.</i>",
        kb_confirm("unlinkx"))


async def cmd_refreshx(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u = update.effective_user
    d = await get_or_create_user(u.id, u.username or "", u.first_name or "")

    if not d.get("x_handle"):
        await _reply(update,
            "ℹ️ You don't have an X account linked.\n\nUse /linkx to connect one.",
            kb_back())
        return

    handle = d["x_handle"]
    msg = await update.effective_message.reply_text(
        f"🔄 Refreshing <b>@{esc(handle)}</b> data…", parse_mode=HTML)

    if not X_BEARER_TOKEN:
        await msg.edit_text(
            "❌ X API not configured on server.",
            parse_mode=HTML, reply_markup=kb_back())
        return

    xid, followers, err = get_x_user(handle)
    if not xid:
        await msg.edit_text(
            f"❌ Could not refresh <b>@{esc(handle)}</b>\n\n"
            f"<b>Reason:</b> {esc(err)}",
            parse_mode=HTML, reply_markup=kb_back())
        return

    d["x_user_id"]   = xid
    d["x_followers"] = followers
    await save_user(d)

    await msg.edit_text(
        f"✅ <b>X Data Refreshed!</b>\n\n"
        f"🐦 @{esc(handle)}\n"
        f"👥 Followers: <b>{followers:,}</b>",
        parse_mode=HTML, reply_markup=kb_back(),
    )


async def cmd_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u = update.effective_user

    if not ctx.args:
        await _reply(update,
            "💳 <b>Set Your Payout Wallet</b>\n\n"
            "Usage: <code>/wallet 0xYourBNBWalletAddress</code>")
        return

    address = ctx.args[0].strip()
    if not re.match(r"^0x[0-9a-fA-F]{40}$", address):
        await _reply(update,
            "❌ <b>Invalid Address</b>\n\n"
            "Must be a BNB Chain address starting with 0x (40 hex chars).")
        return

    d = await get_or_create_user(u.id, u.username or "", u.first_name or "")
    d["wallet"] = address
    await save_user(d)

    await _reply(update,
        f"✅ <b>Wallet Saved!</b>\n\n"
        f"Address: <code>{esc(address)}</code>\n\n"
        "You're all set to receive <b>$POM</b> rewards every Sunday.",
        kb_back())


async def cmd_unwallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u = update.effective_user
    d = await get_or_create_user(u.id, u.username or "", u.first_name or "")

    if not d.get("wallet"):
        await _reply(update,
            "ℹ️ You don't have a wallet set.\n\nUse /wallet to add one.",
            kb_back())
        return

    await _reply(update,
        f"🗑️ <b>Remove Wallet?</b>\n\n"
        f"Address: <code>{esc(d['wallet'][:8])}…{esc(d['wallet'][-6:])}</code>\n\n"
        "You won't receive rewards without a wallet.\n"
        "<i>This cannot be undone.</i>",
        kb_confirm("unwallet"))


# ─────────────────────────────────────────────────────────────────────────────
#  ADMIN COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update, ctx): return
    if not update.message.reply_to_message:
        await _reply(update, "↩️ Reply to the user's message to ban them.")
        return
    target = update.message.reply_to_message.from_user
    try:
        await ctx.bot.ban_chat_member(update.effective_chat.id, target.id)
        await _reply(update,
            f"🚫 <b>{esc(target.first_name)}</b> has been banned from the POM Army.")
    except Exception as e:
        await _reply(update, f"❌ Ban failed: {esc(str(e))}")


async def cmd_mute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update, ctx): return
    if not update.message.reply_to_message:
        await _reply(update, "↩️ Reply to the user's message to mute them.")
        return
    target  = update.message.reply_to_message.from_user
    minutes = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 10
    until   = datetime.utcnow() + timedelta(minutes=minutes)
    try:
        await ctx.bot.restrict_chat_member(
            update.effective_chat.id, target.id,
            permissions=ChatPermissions(can_send_messages=False), until_date=until,
        )
        await _reply(update,
            f"🔇 <b>{esc(target.first_name)}</b> muted for <b>{minutes} minutes</b>.")
    except Exception as e:
        await _reply(update, f"❌ Mute failed: {esc(str(e))}")


async def cmd_warn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update, ctx): return
    if not update.message.reply_to_message:
        await _reply(update, "↩️ Reply to the user's message to warn them.")
        return
    target = update.message.reply_to_message.from_user
    d = await get_or_create_user(target.id, target.username or "", target.first_name or "")
    d["offenses"] = d.get("offenses", 0) + 1
    offenses = d["offenses"]
    await save_user(d)
    if offenses >= 3 and SPAM["ban_on_third_offense"]:
        try:
            await ctx.bot.ban_chat_member(update.effective_chat.id, target.id)
            await _reply(update,
                f"🚫 <b>{esc(target.first_name)}</b> hit 3 warnings — permanently banned.")
        except Exception as e:
            await _reply(update, f"⚠️ Hit 3 warnings but ban failed: {esc(str(e))}")
    else:
        bar = "🟥" * offenses + "⬜" * (3 - offenses)
        almost = " — <b>One more = ban.</b>" if offenses == 2 else ""
        await _reply(update,
            f"⚠️ <b>Warning Issued — {esc(target.first_name)}</b>\n\n"
            f"Warnings: {bar} <b>{offenses}/3</b>{almost}")


async def cmd_announce(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update, ctx): return
    if not ctx.args:
        await _reply(update, "Usage: <code>/announce Your message here</code>")
        return
    text = " ".join(ctx.args)
    msg  = await update.effective_message.reply_text(
        f"📢 <b>POM ARMY ANNOUNCEMENT</b>\n{DIV}\n\n{esc(text)}\n\n{DIV}",
        parse_mode=HTML,
    )
    try:
        await ctx.bot.pin_chat_message(update.effective_chat.id, msg.message_id)
    except Exception as e:
        logger.warning(f"Pin failed: {e}")


async def cmd_tip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /tip @user 1000 $POM
    /tip @user $5
    Admin/owner only. Pays on-chain in $POM.
    Recipient must have wallet set.
    """
    if not await guard_admin(update, ctx): return

    args = ctx.args or []
    # Parse: target should be @username, amount and currency in remaining args
    if len(args) < 2:
        await _reply(update,
            "💸 <b>Tip Command</b>\n\n"
            "Usage:\n"
            "  <code>/tip @user 1000 $POM</code> — tip exact $POM amount\n"
            "  <code>/tip @user $5</code> — tip USD value (converts to $POM)\n\n"
            "<i>Admin/owner only. Recipient needs a wallet set.</i>")
        return

    target_username = args[0].lstrip("@").lower()

    # Detect if it's a "$5" style or a "1000 $POM" style
    amount_str = args[1].strip()
    amount_usd = None
    amount_pom = None

    if amount_str.startswith("$"):
        # USD mode: /tip @user $5
        try:
            amount_usd = float(amount_str.lstrip("$"))
            if amount_usd <= 0:
                raise ValueError()
        except ValueError:
            await _reply(update, "❌ Invalid USD amount. Use format: <code>$5</code>")
            return
    else:
        # POM mode: /tip @user 1000 $POM
        try:
            amount_pom = float(amount_str.replace(",", ""))
            if amount_pom <= 0:
                raise ValueError()
        except ValueError:
            await _reply(update, "❌ Invalid amount. Use <code>1000 $POM</code> or <code>$5</code>")
            return

    # Find recipient
    all_users = await get_all_users()
    target = None
    for u in all_users:
        if (u.get("username") or "").lower() == target_username:
            target = u
            break

    if not target:
        await _reply(update,
            f"❌ User <b>@{esc(target_username)}</b> not found.\n"
            "They need to have used the bot at least once.")
        return

    if not target.get("wallet"):
        await _reply(update,
            f"❌ <b>@{esc(target_username)}</b> has no wallet set.\n"
            "They need to run /wallet first to receive tips.")
        return

    # Resolve amounts
    if amount_usd is not None:
        price = get_pom_price()
        if not price:
            await _reply(update,
                "❌ Couldn't fetch $POM price right now.\n"
                "Try again or use a fixed amount like <code>1000 $POM</code>.")
            return
        amount_pom = amount_usd / price
        label = f"${amount_usd:.2f} (≈ {amount_pom:,.0f} $POM)"
    else:
        label = f"{amount_pom:,.0f} $POM"

    # Log tip as pending
    sender = update.effective_user
    tip_id = await log_tip(
        from_uid=sender.id, from_username=sender.username or "",
        to_uid=target["tg_uid"], to_username=target.get("username", ""),
        amount_usd=amount_usd, amount_pom=amount_pom,
        tx_hash=None, status="pending",
    )

    status_msg = await update.effective_message.reply_text(
        f"💸 Sending tip to <b>@{esc(target_username)}</b>…\n"
        f"Amount: <b>{label}</b>",
        parse_mode=HTML,
    )

    # Send tokens
    tx_hash = send_pom_tokens(target["wallet"], amount_pom)

    if tx_hash:
        await update_tip_status(tip_id, "paid", tx_hash=tx_hash)
        await status_msg.edit_text(
            f"🎁 @{esc(sender.username or sender.first_name)} tipped "
            f"@{esc(target_username)} <b>{label}</b>\n"
            f"🔗 <a href='{BSCSCAN_TX}{tx_hash}'>TX</a>",
            parse_mode=HTML, disable_web_page_preview=True,
        )
    else:
        await update_tip_status(tip_id, "failed")
        await status_msg.edit_text(
            f"❌ Tip failed. Try again or check team wallet BNB balance for gas.",
            parse_mode=HTML,
        )


# ─────────────────────────────────────────────────────────────────────────────
#  OWNER-ONLY COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_syncx(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_owner(update): return
    if not X_BEARER_TOKEN:
        await _reply(update, "❌ X_BEARER_TOKEN not set.")
        return

    # Block during freeze window
    if is_freeze_window():
        await _reply(update,
            "⏳ Raid period has ended. Sync resumes Monday 00:00 WAT.")
        return

    last_sync = await get_meta("last_x_sync")
    if last_sync:
        try:
            elapsed = datetime.utcnow() - datetime.fromisoformat(last_sync)
            if elapsed < timedelta(minutes=SYNC_COOLDOWN_MINS):
                rem = int((timedelta(minutes=SYNC_COOLDOWN_MINS) - elapsed).total_seconds() // 60)
                await _reply(update,
                    f"⏳ Sync on cooldown. Next available in <b>{rem} min</b>.")
                return
        except Exception:
            pass

    status = await update.effective_message.reply_text(
        "🔄 <b>Syncing X Engagement…</b>",
        parse_mode=HTML,
    )

    summary = await sync_x_engagement()

    err_line = (f"\n\n⚠️ Errors: {esc(', '.join(summary['errors']))}"
                if summary["errors"] else "")
    await status.edit_text(
        f"✅ <b>X Sync Complete</b>\n{DIV}\n\n"
        f"📊 Raid tweets checked: <b>{summary['tweets']}</b>\n"
        f"📣 Engagements credited: <b>{summary['engagements']}</b>{err_line}",
        parse_mode=HTML,
    )


async def cmd_resetoffenses(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_owner(update): return
    if not update.message.reply_to_message:
        await _reply(update, "↩️ Reply to the user's message.")
        return
    target = update.message.reply_to_message.from_user
    d = await get_or_create_user(target.id, target.username or "", target.first_name or "")
    d["offenses"] = 0
    await save_user(d)
    await _reply(update, f"✅ Warnings cleared for <b>{esc(target.first_name)}</b>.")


async def cmd_clearcooldown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_owner(update): return

    target_id = None
    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
        target_name = update.message.reply_to_message.from_user.first_name
    elif ctx.args:
        # /clearcooldown @username
        target_username = ctx.args[0].lstrip("@").lower()
        all_users = await get_all_users()
        for u in all_users:
            if (u.get("username") or "").lower() == target_username:
                target_id = u["tg_uid"]
                target_name = u.get("first_name") or target_username
                break
    if not target_id:
        await _reply(update,
            "Usage: <code>/clearcooldown @user</code> or reply to their message.")
        return

    await clear_unlink(target_id)
    await _reply(update,
        f"✅ Cleared relink cooldown for <b>{esc(target_name)}</b>.")


async def cmd_resetlb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_owner(update): return
    if not ctx.args or ctx.args[0].lower() not in {"day","week","month","all"}:
        await _reply(update, "Usage: <code>/resetlb day|week|month|all</code>")
        return
    arg = ctx.args[0].lower()
    periods = ["day","week","month","alltime"] if arg == "all" else [arg]
    count = 0
    for p in periods:
        n = await reset_period_scores(p)
        count = max(count, n)
    if arg in ("week", "all"):
        await clear_week_raids()
    label = "ALL periods" if arg == "all" else f"the <b>{esc(arg)}</b> leaderboard"
    await _reply(update, f"♻️ Reset {label} for <b>{count}</b> members.")


async def cmd_spend(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show current X API spending. Owner only, DM preferred."""
    if not await guard_owner(update): return
    today = await get_api_spend()
    week = await get_api_spend_week()
    month = await get_api_spend_month()
    month_proj = await get_api_spend_range(days=30)

    text = (
        "📊 <b>X API Spend</b>\n"
        f"{DIV}\n\n"
        f"<b>Today</b> ({today['day']}):\n"
        f"  Calls: {today['calls']}\n"
        f"  Spent: <b>${today['spent_usd']:.4f}</b>\n\n"
        f"<b>Week to date:</b> <b>${week:.4f}</b>\n"
        f"<b>Month to date:</b> <b>${month:.4f}</b>\n"
        f"<b>Last 30 days:</b> ${month_proj['spent_usd']:.4f} ({month_proj['calls']} calls)\n\n"
        f"{DIV}"
    )

    # Always reply in DM to owner only
    try:
        await ctx.bot.send_message(chat_id=OWNER_ID, text=text, parse_mode=HTML)
        # If they ran it in group, delete the request silently
        if update.effective_chat.id != OWNER_ID:
            try:
                await update.message.delete()
            except Exception:
                pass
    except Exception:
        await _reply(update, text)


async def cmd_tiplog(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show recent tips. Owner only."""
    if not await guard_owner(update): return
    tips = await get_recent_tips(limit=20)
    if not tips:
        await _reply(update, "ℹ️ No tips logged yet.")
        return
    lines = ["📋 <b>Recent Tips</b>", DIV, ""]
    for t in tips:
        sender = f"@{t['from_username']}" if t['from_username'] else f"uid:{t['from_uid']}"
        recv   = f"@{t['to_username']}" if t['to_username'] else f"uid:{t['to_uid']}"
        amt = ""
        if t['amount_usd']:
            amt = f"${float(t['amount_usd']):.2f}"
        if t['amount_pom']:
            amt = f"{float(t['amount_pom']):,.0f} $POM" if not amt else f"{amt} ({float(t['amount_pom']):,.0f} $POM)"
        status = t['status']
        when = t['created_at'].strftime("%m-%d %H:%M") if t.get('created_at') else "?"
        lines.append(f"• {when} | {esc(sender)} → {esc(recv)} | {esc(amt)} | <i>{esc(status)}</i>")
    lines += ["", DIV]
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3950] + "\n<i>… truncated</i>"
    await _reply(update, text)


# ─────────────────────────────────────────────────────────────────────────────
#  /distribute — pay top 10 + set up raffle
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_distribute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_owner(update): return

    status = await update.effective_message.reply_text(
        "💰 <b>Computing Weekly Rewards…</b>",
        parse_mode=HTML,
    )

    top10, raffle_pool = await compute_weekly_rewards()
    if not top10:
        await status.edit_text(
            "⚠️ <b>No Qualifying Members</b>\n\n"
            "Nobody scored 70%+ of weekly X points.\n"
            "Pool rolled over to next week.",
            parse_mode=HTML,
        )
        return

    price = get_pom_price()
    lines = [
        "🏆 <b>POM Army Weekly Top 10</b>",
        DIV,
        f"💵 Pool: <b>${WEEKLY_POOL_USD:.0f}</b>  •  $POM Price: <code>${price:.6f}</code>",
        f"👥 Top 10 Winners: <b>{len(top10)}</b>",
        DIV2, "",
    ]
    no_wallet = []

    for uid, r in top10.items():
        name   = esc(r["username"])
        wallet = r["wallet"]
        if not wallet:
            no_wallet.append(name)
            lines.append(f"{MEDALS[r['rank']-1]} @{name} — ${r['usd_amount']} — ⚠️ <i>no wallet</i>")
            continue

        try:
            await status.edit_text(
                f"💸 Sending to @{name} (Rank #{r['rank']})…",
                parse_mode=HTML,
            )
        except Exception:
            pass
        tx = send_pom_tokens(wallet, r["token_amount"])
        if tx:
            r["paid"]    = True
            r["tx_hash"] = tx
            verify = f"<a href='{BSCSCAN_TX}{tx}'>Verify</a>"
            lines.append(
                f"{MEDALS[r['rank']-1]} @{name}\n"
                f"    💰 <b>{r['token_amount']:,.0f} $POM</b> (${r['usd_amount']}) — {verify}"
            )
        else:
            lines.append(f"{MEDALS[r['rank']-1]} @{name} — ${r['usd_amount']} — ❌ <i>TX failed</i>")

    # Save snapshot
    await set_meta("last_week_rewards", {
        "distributed_at": _now(),
        "total_pool":     WEEKLY_POOL_USD,
        "winners":        list(top10.values()),
    })

    if no_wallet:
        nw = ", ".join(f"@{n}" for n in no_wallet)
        lines += ["", DIV2,
                  f"⚠️ No wallet: {nw}",
                  "<i>Their rewards rolled over</i>"]

    lines += ["", DIV,
              f"<i>Distributed {esc(datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'))}</i>"]

    summary_text = "\n".join(lines)

    try:
        await ctx.bot.send_message(
            chat_id=RAID_GROUP_ID, text=summary_text,
            parse_mode=HTML, disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Group post failed: {e}")

    # Now set up the raffle if there are eligible entrants
    if raffle_pool:
        await setup_raffle(ctx, raffle_pool)
        await status.edit_text(
            "✅ Top 10 paid. Raffle setup posted.\n"
            "Run <code>/raffle round1</code> when ready.",
            parse_mode=HTML,
        )
    else:
        await status.edit_text(
            "✅ Top 10 paid. No raffle entrants this week (≤10 qualifiers).",
            parse_mode=HTML,
        )


async def setup_raffle(ctx: ContextTypes.DEFAULT_TYPE, raffle_pool: list[dict]) -> None:
    """Create raffle state in DB and post the pinned setup message."""
    week_start = current_week_start()
    state = await get_or_create_raffle_state(week_start)

    # Build entrant list with tickets (tickets = x_pts / 70, min 1)
    entrants = []
    for r in raffle_pool:
        tickets = max(1, r["x_pts"] // 70)
        entrants.append({
            "uid":      r["tg_uid"],
            "username": r.get("username") or r.get("first_name") or "Unknown",
            "tickets":  int(tickets),
            "x_pts":    r["x_pts"],
            "wallet":   r.get("wallet"),
        })
    await save_raffle_entrants(week_start, entrants)

    # Build setup message
    total_tickets = sum(e["tickets"] for e in entrants)
    lines = [
        "🎰 <b>POM RAFFLE — BATTLE ROYALE</b>",
        DIV, "",
        f"<b>{len(entrants)} raiders qualified for the raffle.</b>",
        f"{RAFFLE_WINNERS} rounds. {RAFFLE_WINNERS} separate battles. {RAFFLE_WINNERS} winners.",
        "",
        f"💰 Each winner takes home <b>${RAFFLE_WINNER_AMOUNT_USD:.0f} in $POM</b>",
        "",
        DIV2,
        "🎟️ <b>THE FIGHTERS</b>",
        "",
    ]
    for e in entrants[:50]:
        ticket_label = f"{e['tickets']} ticket{'s' if e['tickets'] != 1 else ''}"
        lines.append(f"  @{esc(e['username'])} — {e['x_pts']:,} X ({ticket_label})")
    if len(entrants) > 50:
        lines.append(f"  <i>… and {len(entrants)-50} others</i>")

    lines += [
        "",
        f"<i>Total tickets in pot: <b>{total_tickets}</b></i>",
        "",
        DIV2,
        "Admin will start the battles shortly.",
        "Sharpen your bones, POM Army. 🐶🔪",
        DIV,
    ]
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3950] + "\n<i>… truncated</i>"

    try:
        msg = await ctx.bot.send_message(
            chat_id=RAID_GROUP_ID, text=text, parse_mode=HTML,
            disable_web_page_preview=True,
        )
        try:
            await ctx.bot.pin_chat_message(RAID_GROUP_ID, msg.message_id)
        except Exception as e:
            logger.warning(f"Pin failed: {e}")
    except Exception as e:
        logger.error(f"Raffle setup post failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  /raffle commands
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_raffle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_owner(update): return

    args = ctx.args or []
    if not args:
        await _reply(update,
            "🎰 <b>Raffle Commands</b>\n\n"
            "<code>/raffle round1</code> — Run Round 1\n"
            "<code>/raffle round2</code> — Run Round 2\n"
            "<code>/raffle round3</code> — Run Round 3\n"
            "<code>/raffle round4</code> — Run Round 4\n"
            "<code>/raffle status</code> — Show current state")
        return

    sub = args[0].lower()
    week_start = current_week_start()
    state = await get_raffle_state(week_start)

    if not state or not state.get("entrants"):
        await _reply(update,
            "ℹ️ No raffle set up for this week.\n"
            "Run /distribute first to set up the raffle.")
        return

    if sub == "status":
        await cmd_raffle_status(update, state)
        return

    round_map = {"round1": 1, "round2": 2, "round3": 3, "round4": 4}
    if sub not in round_map:
        await _reply(update, "Unknown raffle command. Use round1, round2, round3, round4, or status.")
        return

    round_num = round_map[sub]

    # Check order — must complete previous rounds first
    if round_num > 1 and not state.get(f"round{round_num - 1}_done"):
        await _reply(update,
            f"⚠️ Run Round {round_num - 1} first before Round {round_num}.")
        return

    # Already done?
    if state.get(f"round{round_num}_done"):
        winner_uid = state.get(f"round{round_num}_winner")
        await _reply(update,
            f"ℹ️ Round {round_num} already complete. Winner: uid {winner_uid}")
        return

    # Build fighters list — exclude previous round winners
    all_entrants = list(state.get("entrants", []))
    excluded_uids = set()
    for r in (1, 2, 3, 4):
        wuid = state.get(f"round{r}_winner")
        if wuid:
            excluded_uids.add(wuid)
    fighters = [e for e in all_entrants if e["uid"] not in excluded_uids]

    if not fighters:
        await _reply(update, "⚠️ No fighters remaining for this round.")
        return

    # Run the battle
    await _reply(update, f"⚔️ Starting Round {round_num}…")
    winner = await run_battle_round(
        ctx.application, fighters, round_num,
        total_rounds=3, duration_sec=60,
    )

    if not winner:
        await _reply(update, "⚠️ No winner produced.")
        return

    # Pay the winner
    price = get_pom_price()
    token_amount = (RAFFLE_WINNER_AMOUNT_USD / price) if price > 0 else 0
    tx_hash = None

    if winner.get("wallet") and token_amount > 0:
        tx_hash = send_pom_tokens(winner["wallet"], token_amount)

    await save_raffle_round(week_start, round_num, winner["uid"], tx_hash, done=True)

    # Post the reveal
    total_tickets = sum(e["tickets"] for e in fighters)
    win_tickets   = winner.get("tickets", 1)
    win_pct       = (win_tickets / total_tickets * 100) if total_tickets > 0 else 0

    reveal_lines = [
        f"🎉  <b>ROUND {round_num} WINNER</b>  🎉",
        DIV, "",
        f"🏆 <b>@{esc(winner['username'])}</b>", "",
        f"🎟️ Held <b>{win_tickets}</b> tickets out of {total_tickets} ({win_pct:.1f}% chance)",
    ]
    if tx_hash:
        reveal_lines.append(
            f"💰 ${RAFFLE_WINNER_AMOUNT_USD:.0f} in $POM "
            f"(≈ {token_amount:,.0f} $POM)"
        )
        reveal_lines.append(f"🔗 <a href='{BSCSCAN_TX}{tx_hash}'>Verify on BSCScan</a>")
    elif not winner.get("wallet"):
        reveal_lines.append("⚠️ <i>No wallet set — payout pending</i>")
    else:
        reveal_lines.append("❌ <i>Payout failed — admin will retry</i>")

    fighters_left = len(fighters) - 1
    reveal_lines += [
        "",
        DIV,
        f"<i>{fighters_left} fighters left for next round.</i>" if round_num < 4 else
        "<i>Raffle complete. GG POM Army 🔥🐶</i>",
    ]

    try:
        await ctx.bot.send_message(
            chat_id=RAID_GROUP_ID, text="\n".join(reveal_lines),
            parse_mode=HTML, disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Reveal post failed: {e}")

    # DM the winner
    try:
        await ctx.bot.send_message(
            chat_id=winner["uid"],
            text=(
                f"🎉 <b>YOU WON THE POM RAFFLE!</b>\n\n"
                f"Round {round_num} of the weekly raffle is yours.\n"
                f"💰 <b>${RAFFLE_WINNER_AMOUNT_USD:.0f} in $POM</b>"
                + (f"\n🔗 <a href='{BSCSCAN_TX}{tx_hash}'>Verify</a>" if tx_hash else "")
            ),
            parse_mode=HTML, disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning(f"Winner DM failed: {e}")

    # Final wrap-up if this was the last round
    if round_num == 4:
        await post_raffle_wrap(ctx, week_start)


async def cmd_raffle_status(update: Update, state: dict) -> None:
    lines = [
        "🎰 <b>Raffle Status</b>",
        DIV, "",
        f"Week start: {esc(str(state.get('week_start')))}",
        f"Entrants: <b>{len(state.get('entrants', []))}</b>",
        "",
    ]
    for r in (1, 2, 3, 4):
        done = state.get(f"round{r}_done")
        winner = state.get(f"round{r}_winner")
        if done and winner:
            lines.append(f"  Round {r}: ✅ Winner uid {winner}")
        elif done:
            lines.append(f"  Round {r}: ✅ Complete")
        else:
            lines.append(f"  Round {r}: ⏳ Pending")
    lines += ["", DIV]
    await _reply(update, "\n".join(lines))


async def post_raffle_wrap(ctx: ContextTypes.DEFAULT_TYPE, week_start: date) -> None:
    state = await get_raffle_state(week_start)
    if not state:
        return

    entrants_by_uid = {e["uid"]: e for e in state.get("entrants", [])}
    winners = []
    for r in (1, 2, 3, 4):
        wuid = state.get(f"round{r}_winner")
        if wuid:
            w = entrants_by_uid.get(wuid, {"username": f"uid:{wuid}"})
            winners.append((r, w))

    medal = {1: "🥇", 2: "🥈", 3: "🥉", 4: "🏆"}
    lines = [
        "🏁  <b>RAFFLE COMPLETE</b>  🏁",
        DIV, "",
        f"{len(winners)} POMs stand among the fallen.",
        "",
    ]
    for r, w in winners:
        lines.append(f"{medal[r]} Round {r}: <b>@{esc(w['username'])}</b>  •  ${RAFFLE_WINNER_AMOUNT_USD:.0f} $POM")
    lines += [
        "",
        DIV,
        "<i>GG POM ARMY. 🔥🐶</i>",
        "<i>See you next week.</i>",
    ]
    try:
        await ctx.bot.send_message(
            chat_id=RAID_GROUP_ID, text="\n".join(lines),
            parse_mode=HTML, disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Wrap post failed: {e}")


async def cmd_monthlydistribute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Pay out the monthly loyalty bonus (top 3 by month period)."""
    if not await guard_owner(update): return

    top3 = await get_monthly_loyalty_top3()
    if not top3:
        await _reply(update, "ℹ️ No qualifying users for monthly loyalty.")
        return

    status = await update.effective_message.reply_text(
        "🏅 <b>Paying Monthly Loyalty Bonus…</b>",
        parse_mode=HTML,
    )

    price = get_pom_price()
    medal = {1: "🏅", 2: "🎖️", 3: "🏵️"}
    lines = ["🏅 <b>POM Army Monthly Loyalty</b>", DIV, ""]
    paid_count = 0

    for i, r in enumerate(top3[:3], 1):
        usd = MONTHLY_LOYALTY.get(i, 5)
        name = esc(r.get("username") or r.get("first_name") or "Unknown")
        wallet = r.get("wallet")
        if not wallet:
            lines.append(f"{medal[i]} @{name} — ${usd} — ⚠️ <i>no wallet</i>")
            continue

        token_amount = (usd / price) if price > 0 else 0
        try:
            await status.edit_text(
                f"💸 Sending to @{name} (Rank #{i})…",
                parse_mode=HTML,
            )
        except Exception:
            pass

        tx = send_pom_tokens(wallet, token_amount)
        if tx:
            paid_count += 1
            verify = f"<a href='{BSCSCAN_TX}{tx}'>Verify</a>"
            lines.append(
                f"{medal[i]} @{name}\n"
                f"    💰 {token_amount:,.0f} $POM (${usd}) — {verify}"
            )
        else:
            lines.append(f"{medal[i]} @{name} — ${usd} — ❌ <i>TX failed</i>")

    lines += ["", DIV, "<i>Thank you for being loyal POM Army.</i>"]

    summary = "\n".join(lines)
    try:
        msg = await ctx.bot.send_message(
            chat_id=RAID_GROUP_ID, text=summary,
            parse_mode=HTML, disable_web_page_preview=True,
        )
        try:
            await ctx.bot.pin_chat_message(RAID_GROUP_ID, msg.message_id)
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Monthly post failed: {e}")

    await status.edit_text(
        f"✅ Monthly loyalty paid: {paid_count}/3 winners.",
        parse_mode=HTML,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  CALLBACK HANDLER
# ─────────────────────────────────────────────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q     = update.callback_query
    data  = q.data
    u     = q.from_user
    await q.answer()

    d = await get_or_create_user(u.id, u.username or "", u.first_name or "")

    async def edit(text: str, kb=None):
        try:
            if q.message and q.message.photo:
                await q.edit_message_caption(
                    caption=text, parse_mode=HTML, reply_markup=kb,
                )
            else:
                await q.edit_message_text(
                    text, parse_mode=HTML, reply_markup=kb,
                    disable_web_page_preview=True,
                )
        except Exception as e:
            logger.warning(f"edit failed: {e}")
            try:
                await ctx.bot.send_message(
                    chat_id=q.message.chat.id,
                    text=text, parse_mode=HTML, reply_markup=kb,
                    disable_web_page_preview=True,
                )
            except Exception as e2:
                logger.error(f"send fallback failed: {e2}")

    if data == "back_main":
        await edit(render_start(u.first_name or "friend"), kb_main())
    elif data == "menu_x":
        await edit(render_x_menu(d), kb_x_menu(d))
    elif data == "menu_wallet":
        await edit(render_wallet_menu(d), kb_wallet_menu(d))
    elif data == "score":
        await edit(render_score(d), kb_score())
    elif data == "stats":
        await edit(render_stats(d, u.first_name or "friend"), kb_back())
    elif data == "streak":
        await edit(render_streak(d), kb_back())
    elif data == "howto":
        await edit(render_howto(), kb_back())
    elif data == "rewardstatus":
        await edit(await render_reward_status(u.id), kb_back())
    elif data == "weeklypreview":
        text = await render_weeklypreview(u.id, is_owner(u.id))
        if len(text) > 4000:
            text = text[:3950] + "\n<i>… truncated</i>"
        await edit(text, kb_back())
    elif data == "wallet_info":
        await edit(render_wallet_info(d), kb_wallet_back())
    elif data == "unwallet":
        if not d.get("wallet"):
            await edit("ℹ️ You don't have a wallet set.\n\nUse /wallet to add one.",
                       kb_wallet_back())
        else:
            await edit(
                f"🗑️ <b>Remove Wallet?</b>\n\n"
                f"Address: <code>{esc(d['wallet'][:8])}…{esc(d['wallet'][-6:])}</code>\n\n"
                "You won't receive rewards without a wallet.\n"
                "<i>This cannot be undone.</i>",
                kb_confirm("unwallet", return_to="menu_wallet"))
    elif data == "unwallet_confirm":
        old = d.get("wallet", "")
        d["wallet"] = None
        await save_user(d)
        d = await get_or_create_user(u.id, u.username or "", u.first_name or "")
        await edit(
            f"✅ <b>Wallet Removed</b>\n\n"
            f"<code>{esc(old[:8])}…</code> has been unlinked.\n\n"
            "Use /wallet anytime to set a new address.",
            kb_wallet_menu(d))
    elif data.startswith("lb_"):
        period = data[3:]
        text = await render_leaderboard(period)
        if len(text) > 4000:
            text = text[:3950] + "\n<i>… use /lb for more</i>"
        await edit(text, kb_lb())
    elif data == "linkx_prompt":
        cur = f"Currently linked: <b>@{esc(d['x_handle'])}</b>\n\n" if d.get("x_handle") else ""
        await edit(
            f"🐦 <b>Link Your X Account</b>\n\n{cur}"
            "Send the command:\n<code>/linkx @YourXHandle</code>",
            kb_x_back())
    elif data == "refreshx":
        if not d.get("x_handle"):
            await edit("ℹ️ You don't have an X account linked.\n\nUse /linkx to connect one.",
                       kb_x_back())
        else:
            handle = d["x_handle"]
            if not X_BEARER_TOKEN:
                await edit("❌ X API not configured.", kb_x_back())
                return
            xid, followers, err = get_x_user(handle)
            if not xid:
                await edit(
                    f"❌ Could not refresh <b>@{esc(handle)}</b>\n\n"
                    f"<b>Reason:</b> {esc(err)}",
                    kb_x_back())
                return
            d["x_user_id"]   = xid
            d["x_followers"] = followers
            await save_user(d)
            await edit(
                f"✅ <b>X Data Refreshed!</b>\n\n"
                f"🐦 @{esc(handle)}\n"
                f"👥 Followers: <b>{followers:,}</b>",
                kb_x_back())
    elif data == "unlinkx":
        if not d.get("x_handle"):
            await edit("ℹ️ No X account linked.\n\nUse /linkx to connect one.",
                       kb_x_back())
        else:
            await edit(
                f"⚠️ <b>Confirm Unlink</b>\n\n"
                f"Unlink <b>@{esc(d['x_handle'])}</b>?\n\n"
                "Your points stay safe.\n"
                f"<i>{RELINK_COOLDOWN_DAYS}-day cooldown for different handle.</i>",
                kb_confirm("unlinkx", return_to="menu_x"))
    elif data == "unlinkx_confirm":
        old = d.get("x_handle", "")
        d["x_handle"]    = None
        d["x_user_id"]   = None
        d["x_followers"] = 0
        d["x_data"]      = _blank_x_data()
        await save_user(d)
        # Record unlink timestamp for cooldown
        await set_last_unlink(u.id, old)
        d = await get_or_create_user(u.id, u.username or "", u.first_name or "")
        await edit(
            f"✅ <b>Unlinked Successfully</b>\n\n"
            f"@{esc(old)} has been removed.\n"
            f"Points preserved. {RELINK_COOLDOWN_DAYS}-day cooldown for different handle.\n\n"
            "<i>Same handle = instant relink.</i>",
            kb_x_menu(d))

# ─────────────────────────────────────────────────────────────────────────────
#  MESSAGE HANDLER
# ─────────────────────────────────────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    chat = update.effective_chat
    if chat.type != "private" and chat.id != RAID_GROUP_ID:
        return

    u    = update.effective_user
    msg  = update.message
    text = msg.text or ""

    # In DM, only register user, don't process spam/raid
    if chat.type == "private":
        await get_or_create_user(u.id, u.username or "", u.first_name or "")
        return

    d = await get_or_create_user(u.id, u.username or "", u.first_name or "")

    # Flood protection
    if is_flooding(u.id):
        try:
            await msg.delete()
            until = datetime.utcnow() + timedelta(minutes=SPAM["mute_minutes"])
            await ctx.bot.restrict_chat_member(
                msg.chat.id, u.id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until,
            )
            d["offenses"] = d.get("offenses", 0) + 1
            await save_user(d)
            await ctx.bot.send_message(
                msg.chat.id,
                f"⚡ @{esc(u.username or u.first_name or 'user')} — slow down! "
                f"Auto-muted for <b>{SPAM['mute_minutes']} minutes</b>.",
                parse_mode=HTML,
            )
        except Exception as e:
            logger.warning(f"Flood mute failed: {e}")
        return

    # Banned words
    if any(w in text.lower() for w in BANNED_WORDS):
        try:
            await msg.delete()
            d["offenses"] = d.get("offenses", 0) + 1
            await save_user(d)
            await ctx.bot.send_message(
                msg.chat.id, "🚫 Message removed — violates community rules.")
        except Exception:
            pass
        return

    # X link handling
    x_match = X_LINK_RE.search(text)
    if x_match:
        tweet_id = x_match.group(1)

        # Freeze window — no raid registration
        if is_freeze_window():
            await msg.reply_text(
                "⏳ Raid week ended at 16:00 WAT.\n"
                "New week starts Monday 00:00 WAT.",
                parse_mode=HTML,
            )
            return

        if not await tweet_already_registered(tweet_id):
            tweet_author_xid = None
            tweet_text       = ""
            if X_BEARER_TOKEN:
                tweet_author_xid, tweet_text = get_tweet_data(tweet_id)

            # Resolve POM_X_ID if not set
            global POM_X_ID
            if not POM_X_ID and X_BEARER_TOKEN:
                POM_X_ID, _, _ = get_x_user(POM_X_HANDLE)

            # Determine validity:
            # - @Pom_bsc tweets: always valid (any content)
            # - Other tweets: must mention $POM or @Pom_bsc
            is_pom_official = (tweet_author_xid and POM_X_ID
                               and tweet_author_xid == POM_X_ID)
            pom_related     = is_pom_official or tweet_is_pom_related(tweet_text)

            if not pom_related:
                # Not a valid raid target — silent ignore
                return

            # Check if it's the user's own post
            user_x_id = d.get("x_user_id")
            is_own    = bool(user_x_id and tweet_author_xid
                             and tweet_author_xid == user_x_id)

            await register_raid(
                tweet_id, u.id, display_name(d),
                is_pom_official, is_own, tweet_author_xid,
            )

            if is_own:
                # Personal raid - award +25 immediately + cooldown
                # Check cooldown first
                last_drop = d.get("x_data", {}).get("last_post_drop")
                on_cooldown = False
                if last_drop:
                    try:
                        elapsed = datetime.utcnow() - datetime.fromisoformat(last_drop)
                        if elapsed < timedelta(hours=LINK_COOLDOWN_HOURS):
                            on_cooldown = True
                    except Exception:
                        pass

                if on_cooldown:
                    await msg.reply_text(
                        "⏳ Personal post on cooldown (12hr). Try again later.",
                        parse_mode=HTML,
                    )
                    return

                await add_x_points_db(u.id, X_POINTS["post_drop"])
                d["x_data"]["last_post_drop"] = _now()
                history = d["x_data"].get("personal_post_history", [])
                history.append(tweet_text or text)
                d["x_data"]["personal_post_history"] = history[-PERSONAL_POST["history_count"]:]
                await save_user(d)
                await msg.reply_text(
                    "✅ Personal raid registered\n"
                    "⏳ 12hr cooldown started\n\n"
                    "POM ARMY — go raid! 🐶🔥",
                    parse_mode=HTML,
                )
            elif is_pom_official:
                await msg.reply_text(
                    "✅ Official raid registered\n\n"
                    "POM ARMY — go raid! 🐶🔥",
                    parse_mode=HTML,
                )
            else:
                await msg.reply_text(
                    "✅ Community raid registered\n\n"
                    "POM ARMY — go raid! 🐶🔥",
                    parse_mode=HTML,
                )
        return  # don't count X links as TG activity

    # Excessive links
    if len(LINK_RE.findall(text)) > SPAM["max_links"]:
        try:
            await msg.delete()
            d["offenses"] = d.get("offenses", 0) + 1
            await save_user(d)
            await ctx.bot.send_message(msg.chat.id, "🚫 Too many links — message removed.")
        except Exception:
            pass
        return

    # TG activity points (only during active raid week — not during freeze)
    if is_freeze_window():
        return  # don't award TG points during freeze either

    words = len(text.split())
    if words >= TG_MIN_WORDS:
        today_tg = await get_today_tg_points(u.id)
        if today_tg < TG_DAILY_CAP:
            pts = 1 if words < 15 else 2 if words < 30 else 3
            pts = min(pts, TG_DAILY_CAP - today_tg)
            await add_tg_points_db(u.id, pts)


async def handle_new_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.id != RAID_GROUP_ID:
        # Bot was added to a non-official group
        try:
            await ctx.bot.send_message(
                chat_id=chat.id,
                text="⛔ PomRaid only operates in the official POM Army group.",
            )
            await ctx.bot.leave_chat(chat.id)
        except Exception:
            pass
        return

    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        await get_or_create_user(member.id, member.username or "", member.first_name or "")
        try:
            await update.message.reply_text(
                f"🐶 <b>Welcome to the POM Army, {esc(member.first_name or 'friend')}!</b>\n"
                f"{DIV}\n\n"
                "📌 <b>Get started in 3 steps:</b>\n"
                "1. Register → /start\n"
                "2. Link your X → /linkx\n"
                "3. Daily check-in → /checkin\n\n"
                f"{DIV2}\n"
                "<i>Raid hard. Earn $POM. Never stop.</i> 🔥",
                parse_mode=HTML,
            )
        except Exception as e:
            logger.warning(f"Welcome failed: {e}")

# ─────────────────────────────────────────────────────────────────────────────
#  SCHEDULED TASKS
# ─────────────────────────────────────────────────────────────────────────────

async def scheduled_sync(app: Application):
    """Run scheduled X engagement sync. Skips during freeze window."""
    if is_freeze_window():
        logger.info("Skipped scheduled sync — freeze window")
        return
    if not X_BEARER_TOKEN:
        logger.info("Skipped scheduled sync — no X_BEARER_TOKEN")
        return
    try:
        summary = await sync_x_engagement()
        logger.info(f"Scheduled sync done: tweets={summary['tweets']} "
                    f"engagements={summary['engagements']}")
    except Exception as e:
        logger.error(f"Scheduled sync failed: {e}")


async def final_sync_before_freeze(app: Application):
    """Sunday 15:55 WAT — final sync before raid week ends."""
    if not X_BEARER_TOKEN:
        return
    try:
        summary = await sync_x_engagement()
        logger.info(f"Final sync done: tweets={summary['tweets']} "
                    f"engagements={summary['engagements']}")
        # Notify group that raid week is ending
        try:
            await app.bot.send_message(
                chat_id=RAID_GROUP_ID,
                text=(
                    "⏰ <b>RAID WEEK ENDING IN 5 MINUTES</b>\n"
                    f"{DIV}\n\n"
                    "Last sync complete.\n"
                    "Get final raids in before 16:00 WAT.\n\n"
                    "Payouts coming shortly after. 🔥"
                ),
                parse_mode=HTML,
            )
        except Exception as e:
            logger.warning(f"Final sync announce failed: {e}")
    except Exception as e:
        logger.error(f"Final sync failed: {e}")


async def freeze_announcement(app: Application):
    """Sunday 16:00 WAT — announce raid week ended."""
    try:
        await app.bot.send_message(
            chat_id=RAID_GROUP_ID,
            text=(
                "⏰ <b>RAID WEEK ENDED</b>\n"
                f"{DIV}\n\n"
                "Leaderboard is frozen.\n"
                "No new X points until Monday 00:00 WAT.\n\n"
                "🏆 Top 10 + raffle payouts soon.\n"
                "Stay tuned, POM Army. 🔥🐶"
            ),
            parse_mode=HTML,
        )
    except Exception as e:
        logger.error(f"Freeze announce failed: {e}")
    # Also DM owner
    try:
        await app.bot.send_message(
            chat_id=OWNER_ID,
            text=(
                "⏰ <b>Raid week ended.</b>\n\n"
                "Run <code>/distribute</code> when ready to pay out."
            ),
            parse_mode=HTML,
        )
    except Exception as e:
        logger.warning(f"Owner freeze DM failed: {e}")


async def weekly_reset(app: Application):
    """Monday 00:00 WAT (23:00 UTC Sun): reset weekly scores and raids."""
    await reset_period_scores("week")
    await clear_week_raids()
    await set_meta("week_start", _now())
    logger.info("Weekly reset complete")
    try:
        await app.bot.send_message(
            chat_id=RAID_GROUP_ID,
            text=(
                "🔄 <b>New Raid Week Has Begun!</b> 🐶\n"
                f"{DIV}\n\n"
                "Weekly scores reset.\n"
                "Fresh shot at the top 10.\n\n"
                "📌 <b>Reminder:</b>\n"
                "• Raid every <b>@Pom_bsc</b> post\n"
                "• Drop your own posts for bonus points\n"
                "• Daily /checkin to build streak\n"
                "• /wallet to receive payouts\n\n"
                f"{DIV}\n"
                "<i>$POM Army — Let's run it up!</i> 🚀"
            ),
            parse_mode=HTML,
        )
    except Exception as e:
        logger.error(f"Weekly reset announce failed: {e}")


async def daily_reset(app: Application):
    """Every day 00:00 WAT (23:00 UTC): reset 'day' scores."""
    await reset_period_scores("day")
    logger.info("Daily reset complete")


async def monthly_reset(app: Application):
    """1st of month at 00:00 WAT: reset 'month' scores.
    Loyalty bonus is owner-triggered via /monthlydistribute."""
    if datetime.now(WAT).day == 1:
        await reset_period_scores("month")
        logger.info("Monthly reset complete")


async def daily_spend_report(app: Application):
    """Send the daily X API spend report to owner DM."""
    today = await get_api_spend()
    week  = await get_api_spend_week()
    month = await get_api_spend_month()

    alert = ""
    if today['spent_usd'] >= DAILY_SPEND_ALERT_USD:
        alert = "\n⚠️ <b>HIGH SPEND TODAY</b>\n"

    text = (
        f"📊 <b>X API Spend Report</b>{alert}\n"
        f"{DIV}\n\n"
        f"<b>Today</b> ({today['day']}):\n"
        f"  Calls: {today['calls']}\n"
        f"  Spent: <b>${today['spent_usd']:.4f}</b>\n\n"
        f"<b>Week to date:</b> <b>${week:.4f}</b>\n"
        f"<b>Month to date:</b> <b>${month:.4f}</b>\n\n"
        f"<i>Estimated monthly: ~${month * 30 / max(1, datetime.now(WAT).day):.2f}</i>\n"
        f"{DIV}"
    )
    try:
        await app.bot.send_message(chat_id=OWNER_ID, text=text, parse_mode=HTML)
    except Exception as e:
        logger.error(f"Daily spend DM failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN / STARTUP
# ─────────────────────────────────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    await init_db()
    logger.info("PostgreSQL connected.")

    member_cmds = [
        BotCommand("start",         "Open main menu"),
        BotCommand("score",         "View your score card"),
        BotCommand("stats",         "View your full profile"),
        BotCommand("streak",        "Check your check-in streak"),
        BotCommand("checkin",       "Daily check-in (group only)"),
        BotCommand("lb",            "Leaderboard (week/month/day)"),
        BotCommand("howto",         "How to earn points"),
        BotCommand("rules",         "Full rules & point breakdown"),
        BotCommand("list",          "Show all commands"),
        BotCommand("linkx",         "Link your X account"),
        BotCommand("unlinkx",       "Unlink your X account"),
        BotCommand("refreshx",      "Refresh X follower data"),
        BotCommand("wallet",        "Set your payout wallet"),
        BotCommand("unwallet",      "Remove your payout wallet"),
        BotCommand("rewardstatus",  "Check reward eligibility"),
        BotCommand("weeklypreview", "See weekly standings"),
        BotCommand("help",          "Get help"),
    ]
    owner_cmds = member_cmds + [
        BotCommand("ban",               "Ban a user"),
        BotCommand("mute",              "Mute a user"),
        BotCommand("warn",              "Warn a user"),
        BotCommand("announce",          "Post & pin announcement"),
        BotCommand("tip",               "Tip user in $POM"),
        BotCommand("syncx",             "Force sync X engagement"),
        BotCommand("distribute",        "Run weekly top-10 payout"),
        BotCommand("raffle",            "Run raffle (round1/2/3/status)"),
        BotCommand("monthlydistribute", "Run monthly loyalty payout"),
        BotCommand("resetlb",           "Reset leaderboard period"),
        BotCommand("resetoffenses",     "Clear user warnings"),
        BotCommand("clearcooldown",     "Reset relink cooldown"),
        BotCommand("spend",             "Show X API spend"),
        BotCommand("tiplog",            "Show recent tips"),
    ]
    try:
        await app.bot.set_my_commands(member_cmds, scope=BotCommandScopeDefault())
        await app.bot.set_my_commands(owner_cmds, scope=BotCommandScopeChat(chat_id=OWNER_ID))
        logger.info("BotFather commands registered.")
    except Exception as e:
        logger.warning(f"Could not register BotFather commands: {e}")

    # Scheduler — all times in UTC
    scheduler = AsyncIOScheduler(timezone="UTC")

    # Sync jobs — 12:00 WAT (= 11:00 UTC), 20:00 WAT (= 19:00 UTC)
    scheduler.add_job(scheduled_sync,            "cron", hour=11, minute=0, args=[app])
    scheduler.add_job(scheduled_sync,            "cron", hour=19, minute=0, args=[app])

    # Final sync at Sunday 15:55 WAT = Sunday 14:55 UTC
    scheduler.add_job(final_sync_before_freeze,  "cron", day_of_week="sun",
                      hour=14, minute=55, args=[app])

    # Freeze announcement: Sunday 16:00 WAT = Sunday 15:00 UTC
    scheduler.add_job(freeze_announcement,       "cron", day_of_week="sun",
                      hour=15, minute=0, args=[app])

    # Weekly reset: Monday 00:00 WAT = Sunday 23:00 UTC
    scheduler.add_job(weekly_reset,              "cron", day_of_week="sun",
                      hour=23, minute=0, args=[app])

    # Daily reset: 00:00 WAT = 23:00 UTC every day
    scheduler.add_job(daily_reset,               "cron", hour=23, minute=0, args=[app])

    # Monthly reset: 1st of month at 00:00 WAT = 23:00 UTC on last day of month
    # Use day=1 hour=23 UTC of the previous month — simpler: just check day=1 at 23:05 UTC
    scheduler.add_job(monthly_reset,             "cron", day=1, hour=23, minute=5, args=[app])

    # Daily spend report: 23:00 WAT = 22:00 UTC
    scheduler.add_job(daily_spend_report,        "cron", hour=22, minute=0, args=[app])

    scheduler.start()
    logger.info("Scheduler started.")


async def post_shutdown(app: Application) -> None:
    await close_db()


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set.")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Member commands
    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("help",          cmd_help))
    app.add_handler(CommandHandler("list",          cmd_list))
    app.add_handler(CommandHandler("howto",         cmd_howto))
    app.add_handler(CommandHandler("rules",         cmd_rules))
    app.add_handler(CommandHandler("score",         cmd_score))
    app.add_handler(CommandHandler("stats",         cmd_stats))
    app.add_handler(CommandHandler("streak",        cmd_streak))
    app.add_handler(CommandHandler("checkin",       cmd_checkin))
    app.add_handler(CommandHandler("rewardstatus",  cmd_rewardstatus))
    app.add_handler(CommandHandler("weeklypreview", cmd_weeklypreview))
    app.add_handler(CommandHandler("leaderboard",   cmd_leaderboard))
    app.add_handler(CommandHandler("lb",            cmd_leaderboard))
    app.add_handler(CommandHandler("linkx",         cmd_linkx))
    app.add_handler(CommandHandler("unlinkx",       cmd_unlinkx))
    app.add_handler(CommandHandler("refreshx",      cmd_refreshx))
    app.add_handler(CommandHandler("wallet",        cmd_wallet))
    app.add_handler(CommandHandler("unwallet",      cmd_unwallet))

    # Admin commands
    app.add_handler(CommandHandler("ban",      cmd_ban))
    app.add_handler(CommandHandler("mute",     cmd_mute))
    app.add_handler(CommandHandler("warn",     cmd_warn))
    app.add_handler(CommandHandler("announce", cmd_announce))
    app.add_handler(CommandHandler("tip",      cmd_tip))

    # Owner-only commands
    app.add_handler(CommandHandler("syncx",             cmd_syncx))
    app.add_handler(CommandHandler("distribute",       cmd_distribute))
    app.add_handler(CommandHandler("raffle",            cmd_raffle))
    app.add_handler(CommandHandler("monthlydistribute", cmd_monthlydistribute))
    app.add_handler(CommandHandler("resetlb",           cmd_resetlb))
    app.add_handler(CommandHandler("resetoffenses",     cmd_resetoffenses))
    app.add_handler(CommandHandler("clearcooldown",     cmd_clearcooldown))
    app.add_handler(CommandHandler("spend",             cmd_spend))
    app.add_handler(CommandHandler("tiplog",            cmd_tiplog))

    # Callbacks + messages
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🐶 PomRaid Bot v2 starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()