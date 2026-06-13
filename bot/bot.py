"""
PomRaid Bot — $POM Community Raid Tracker & Auto-Payout
BNB Chain | @Pom_bsc | Python 3.11

Permissions:
  OWNER (5228498784)     — /syncx, /distribute, /resetlb, /resetoffenses
  ADMIN (group admins)   — /ban, /mute, /warn, /announce
  MEMBER (everyone else) — /start /help /score /stats /lb /howto /rules /list
                            /linkx /unlinkx /refreshx /wallet /unwallet
                            /rewardstatus /weeklypreview

Group lock: bot only operates in RAID_GROUP_ID; auto-leaves any other group.

All user-facing messages use HTML parse mode (much simpler than MarkdownV2 —
only <, >, & need escaping, no backslash hell).
"""

import asyncio
import difflib
import html
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta

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
    clear_week_raids, close_db, credit_engagement,
    get_all_users, get_meta, get_or_create_user,
    get_period_leaderboard, get_raids, get_today_tg_points,
    get_total_x_points_this_week,
    init_db, is_engagement_credited,
    register_raid, reset_period_scores, save_user, set_meta,
    tweet_already_registered,
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

# Rewards
WEEKLY_POOL_USD = 75.0
TIER_AMOUNTS    = {1:18, 2:13, 3:10, 4:6, 5:6, 6:6, 7:4, 8:4, 9:4, 10:4}
TOP_N           = 10
QUALIFY_PCT     = 0.70

# X engagement
LINK_COOLDOWN_HOURS = 12
SYNC_COOLDOWN_MINS  = 60
X_POINTS = {"like":5, "repost":10, "comment":15, "quote":20, "post_drop":25}
PERSONAL_POST = {
    "min_words":      50,
    "require_image":  True,
    "min_followers":  50,
    "similarity_limit":0.70,
    "history_count":  5,
}

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

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
#  HTML SAFE — far simpler than MarkdownV2
# ─────────────────────────────────────────────────────────────────────────────

def esc(text) -> str:
    """Escape HTML for safe rendering in Telegram messages."""
    return html.escape(str(text), quote=False)

DIV  = "━━━━━━━━━━━━━━━━━━━━━━━━━━"
DIV2 = "─────────────────────────────"

# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.utcnow().isoformat()

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
    """Allow private DMs OR the official POM group. Block everything else."""
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

def x_get(url: str, params: dict | None = None) -> dict | None:
    """Synchronous X API call. Returns parsed JSON or None on failure."""
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


def get_x_user(handle: str) -> tuple[str | None, int]:
    """Look up an X user by handle. Returns (user_id, followers_count) or (None, 0)."""
    handle = handle.lstrip("@").strip()
    data = x_get(
        f"https://api.twitter.com/2/users/by/username/{handle}",
        {"user.fields": "public_metrics"},
    )
    if not data or "data" not in data:
        return None, 0
    user = data["data"]
    followers = user.get("public_metrics", {}).get("followers_count", 0)
    return user["id"], followers


def get_tweet_data(tweet_id: str) -> tuple[str | None, str]:
    """Get a tweet's author_id and text in one call."""
    data = x_get(
        f"https://api.twitter.com/2/tweets/{tweet_id}",
        {"tweet.fields": "author_id,text"},
    )
    if not data or "data" not in data:
        return None, ""
    return data["data"].get("author_id"), data["data"].get("text", "")


def tweet_is_pom_related(text: str) -> bool:
    t = (text or "").lower()
    return "$pom" in t or "@pom_bsc" in t


def get_pom_recent_tweets(pom_x_id: str, max_results: int = 5) -> list:
    data = x_get(
        f"https://api.twitter.com/2/users/{pom_x_id}/tweets",
        {"max_results": max_results,
         "tweet.fields": "created_at",
         "exclude": "retweets,replies"},
    )
    return data.get("data", []) if data else []


def get_likers(tweet_id: str) -> list[str]:
    data = x_get(
        f"https://api.twitter.com/2/tweets/{tweet_id}/liking_users",
        {"max_results": 100},
    )
    return [u["id"] for u in data.get("data", [])] if data else []


def get_retweeters(tweet_id: str) -> list[str]:
    data = x_get(
        f"https://api.twitter.com/2/tweets/{tweet_id}/retweeted_by",
        {"max_results": 100},
    )
    return [u["id"] for u in data.get("data", [])] if data else []


def get_replies_and_quotes(tweet_id: str) -> tuple[list[str], list[str]]:
    r = x_get(
        "https://api.twitter.com/2/tweets/search/recent",
        {"query": f"conversation_id:{tweet_id} is:reply",
         "max_results": 100, "tweet.fields": "author_id"},
    )
    q = x_get(
        "https://api.twitter.com/2/tweets/search/recent",
        {"query": f"url:{tweet_id} is:quote",
         "max_results": 100, "tweet.fields": "author_id"},
    )
    return (
        list({t["author_id"] for t in r.get("data", [])} if r else []),
        list({t["author_id"] for t in q.get("data", [])} if q else []),
    )


def search_organic_pom_posts(since_hours: int = 4) -> list:
    since = (datetime.utcnow() - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = x_get("https://api.twitter.com/2/tweets/search/recent", {
        "query": "($POM OR @Pom_bsc) -is:retweet -is:reply lang:en",
        "max_results": 100,
        "start_time": since,
        "tweet.fields": "author_id,text,attachments,created_at",
        "expansions": "attachments.media_keys",
        "media.fields": "type",
    })
    if not data or "data" not in data:
        return []
    media_ids = {m["media_key"] for m in data.get("includes", {}).get("media", [])
                 if m.get("type") in ("photo","video")}
    for tweet in data["data"]:
        keys = tweet.get("attachments", {}).get("media_keys", [])
        tweet["has_image"] = any(k in media_ids for k in keys)
    return data["data"]


def is_original(text: str, history: list[str]) -> bool:
    return all(
        difflib.SequenceMatcher(None, text.lower(), p.lower()).ratio() < PERSONAL_POST["similarity_limit"]
        for p in history
    )


def get_pom_price() -> float:
    """Fetch live $POM price from DexScreener."""
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
        logger.error("PAY_WALLET_KEY not set — cannot send payout")
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
#  SYNC ENGINE — pulls X engagement, awards points
# ─────────────────────────────────────────────────────────────────────────────

async def sync_x_engagement() -> dict:
    """Sync all registered raids: check who engaged, award points, scan organic posts."""
    global POM_X_ID
    summary = {"tweets":0, "engagements":0, "organic":0, "errors":[]}

    if not X_BEARER_TOKEN:
        summary["errors"].append("X_BEARER_TOKEN not set")
        return summary

    if not POM_X_ID:
        POM_X_ID, _ = get_x_user(POM_X_HANDLE)
        if not POM_X_ID:
            summary["errors"].append(f"Cannot resolve @{POM_X_HANDLE}")
            return summary

    # Build X user_id → user_dict map; resolve any missing x_user_ids
    all_users = await get_all_users()
    xid_map: dict[str, dict] = {}
    for u in all_users:
        if u.get("x_handle") and not u.get("x_user_id"):
            xid, fl = get_x_user(u["x_handle"])
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
                # Don't credit engagement on your own post
                if dropper_xid and xid == dropper_xid:
                    continue
                tg_uid = u["tg_uid"]
                if await is_engagement_credited(tweet_id, tg_uid, action):
                    continue
                await add_x_points_db(tg_uid, X_POINTS[action])
                await credit_engagement(tweet_id, tg_uid, action)
                summary["engagements"] += 1

    # Organic $POM posts (people posting independently)
    now = datetime.utcnow()
    for tweet in search_organic_pom_posts(since_hours=4):
        u = xid_map.get(tweet.get("author_id"))
        if not u:
            continue
        text = tweet.get("text", "")

        if u.get("x_followers", 0) < PERSONAL_POST["min_followers"]:
            continue
        if PERSONAL_POST["require_image"] and not tweet.get("has_image"):
            continue
        if len(text.split()) < PERSONAL_POST["min_words"]:
            continue

        xd = u.get("x_data", {})
        last_drop = xd.get("last_post_drop")
        if last_drop:
            try:
                if (now - datetime.fromisoformat(last_drop)) < timedelta(hours=LINK_COOLDOWN_HOURS):
                    continue
            except Exception:
                pass

        history = xd.get("personal_post_history", [])
        if not is_original(text, history):
            continue

        await add_x_points_db(u["tg_uid"], X_POINTS["post_drop"])
        u["x_data"]["last_post_drop"] = now.isoformat()
        history.append(text)
        u["x_data"]["personal_post_history"] = history[-PERSONAL_POST["history_count"]:]
        await save_user(u)
        summary["organic"] += 1

    await set_meta("last_x_sync", _now())
    return summary

# ─────────────────────────────────────────────────────────────────────────────
#  REWARD ENGINE
# ─────────────────────────────────────────────────────────────────────────────

async def compute_weekly_rewards() -> dict:
    """Compute the top-10 reward distribution based on weekly X points."""
    total_x = await get_total_x_points_this_week()
    if not total_x:
        return {}

    threshold = total_x * QUALIFY_PCT
    lb        = await get_period_leaderboard("week", limit=TOP_N * 3)

    qualifiers = [r for r in lb if r["x_pts"] >= threshold]
    if not qualifiers:
        return {}
    qualifiers = qualifiers[:TOP_N]

    price    = get_pom_price()
    rollover = float(await get_meta("rollover_usd", 0.0) or 0.0)
    pool     = WEEKLY_POOL_USD + rollover

    results = {}
    for rank, r in enumerate(qualifiers, 1):
        uid = str(r["tg_uid"])
        usd = TIER_AMOUNTS.get(rank, 4)
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

    paid_out_total = sum(TIER_AMOUNTS.get(r+1, 4) for r in range(len(qualifiers)))
    new_rollover   = max(0.0, pool - paid_out_total)
    await set_meta("rollover_usd", new_rollover)
    return results

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
#  KEYBOARDS
# ─────────────────────────────────────────────────────────────────────────────

def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 My Score",       callback_data="score"),
         InlineKeyboardButton("📋 My Stats",       callback_data="stats")],
        [InlineKeyboardButton("🏆 Leaderboard",    callback_data="lb_alltime"),
         InlineKeyboardButton("❓ How to Earn",    callback_data="howto")],
        [InlineKeyboardButton("💰 Reward Status",  callback_data="rewardstatus"),
         InlineKeyboardButton("📊 Weekly Preview", callback_data="weeklypreview")],
        [InlineKeyboardButton("🐦 Link X",         callback_data="linkx_prompt"),
         InlineKeyboardButton("🔄 Refresh X",      callback_data="refreshx")],
        [InlineKeyboardButton("👛 Set Wallet",     callback_data="wallet_info"),
         InlineKeyboardButton("🗑️ Remove Wallet", callback_data="unwallet")],
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

def kb_confirm(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes", callback_data=f"{action}_confirm"),
         InlineKeyboardButton("❌ Cancel", callback_data="back_main")],
    ])

# ─────────────────────────────────────────────────────────────────────────────
#  RENDER FUNCTIONS — all use HTML (no escape headaches)
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
        "3. How to earn → /howto\n"
        "4. Leaderboard → /lb\n"
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
        "/refreshx — Refresh X follower count",
        "",
        DIV2,
        "💰 <b>Rewards</b>",
        "/wallet 0x… — Set BNB payout wallet",
        "/unwallet — Remove your wallet",
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
        ]
    if is_own:
        lines += [
            "",
            DIV2,
            "👑 <b>Owner Only</b>",
            "/syncx — Sync X engagement scores",
            "/distribute — Run weekly payout",
            "/resetlb day|week|month|all — Reset board",
            "/resetoffenses — Clear user warnings",
        ]
    lines += ["", DIV]
    return "\n".join(lines)


def render_list(is_adm: bool, is_own: bool) -> str:
    """Compact command list — same content as /help but always shows full layout."""
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
        f"  Comment ........ +{X_POINTS['comment']} pts (1 per post)\n"
        f"  Quote Tweet .... +{X_POINTS['quote']} pts\n\n"
        f"{DIV2}\n"
        "🔗 <b>DROP YOUR OWN POST</b>\n"
        "Paste your X link in the group.\n"
        "Must contain $POM or @Pom_bsc.\n\n"
        f"  ✅ Verified as yours → +{X_POINTS['post_drop']} pts\n"
        "  ✅ 12hr cooldown starts\n"
        "  ✅ Others raid it &amp; earn points too\n\n"
        f"{DIV2}\n"
        "💬 <b>TELEGRAM ACTIVITY</b>\n"
        f"Messages of {TG_MIN_WORDS}+ words earn TG points.\n"
        f"Daily cap: <b>{TG_DAILY_CAP} pts max</b>\n"
        "<i>TG points do not count toward reward threshold</i>\n\n"
        f"{DIV2}\n"
        "💰 <b>WEEKLY REWARDS</b>\n"
        f"Every Sunday — <b>${WEEKLY_POOL_USD:.0f}</b> pool in $POM.\n"
        f"Top {TOP_N} raiders with 70%+ of weekly X points.\n\n"
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
        "Bot auto-detects new POM posts and alerts the group.\n"
        "Go engage on X to earn points:\n\n"
        "<pre>"
        "Action         Points    Limit\n"
        "─────────────────────────────────\n"
        f"Like             +{X_POINTS['like']}      Once per post\n"
        f"Repost           +{X_POINTS['repost']}     Once per post\n"
        f"Comment          +{X_POINTS['comment']}     Once per post\n"
        f"Quote Tweet      +{X_POINTS['quote']}     Once per post"
        "</pre>\n\n"
        f"{DIV2}\n"
        "🔗 <b>DROPPING YOUR OWN POST</b>\n"
        "Paste your X link in the group.\n\n"
        "Requirements:\n"
        "  ✅ Must mention $POM or @Pom_bsc\n"
        "  ✅ Verified as YOUR linked X account\n"
        f"  ✅ Min {PERSONAL_POST['min_words']} words\n"
        "  ✅ Must include image or video\n"
        f"  ✅ Min {PERSONAL_POST['min_followers']} followers on X\n"
        "  ✅ Original content (no copy-paste)\n"
        "  ✅ 12hr cooldown between drops\n\n"
        f"Reward: +{X_POINTS['post_drop']} pts to you\n"
        "Others raid it → they earn engagement points\n\n"
        f"{DIV2}\n"
        "🌐 <b>COMMUNITY RAID LINKS</b>\n"
        "Anyone can drop a POM-related X link that isn't theirs:\n"
        "  • 0 pts to the dropper\n"
        "  • Everyone who raids earns engagement points\n"
        "  • No cooldown affected for dropper\n\n"
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
        "⚠️ TG points do NOT count toward the 70% reward\n"
        "qualification threshold. Only X points qualify.\n\n"
        f"{DIV2}\n"
        "💰 <b>WEEKLY REWARDS</b> (Every Sunday)\n"
        f"Pool: <b>${WEEKLY_POOL_USD:.0f}/week</b> in <b>$POM tokens</b>\n\n"
        "Qualification:\n"
        "  • Score 70%+ of total weekly X points\n"
        "  • Only X points count toward this\n"
        f"  • Top {TOP_N} qualifiers get paid\n\n"
        "<pre>"
        "Payout Tiers\n"
        "──────────────────────\n"
        "🥇 1st             $18\n"
        "🥈 2nd             $13\n"
        "🥉 3rd             $10\n"
        "4th – 6th        $6 each\n"
        "7th – 10th       $4 each\n"
        "──────────────────────\n"
        "Total             $75"
        "</pre>\n\n"
        "Unclaimed rewards roll over to next week.\n\n"
        f"{DIV2}\n"
        "📋 <b>SETUP CHECKLIST</b>\n"
        "  ☐ /linkx — connect your X account\n"
        "  ☐ /wallet — set your BNB wallet\n"
        "  ☐ /rewardstatus — check your standing\n"
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
        f"{cd_line}\n\n"
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

    return (
        f"📋 <b>Full Profile — {esc(first_name)}</b>\n"
        f"{DIV}\n\n"
        f"👤 Handle: @{esc(display_name(d))}\n"
        f"🐦 X Account: {x_info}\n"
        f"💳 Wallet: {wall}\n"
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
        "<i>Use /rewardstatus to check reward eligibility</i>"
    )


async def render_reward_status(tg_uid: int) -> str:
    d        = await get_or_create_user(tg_uid)
    wk_pts   = period_total(d, "week")
    wk_x_pts = x_week_pts(d)
    total_x  = await get_total_x_points_this_week()

    threshold = total_x * QUALIFY_PCT if total_x > 0 else 0
    pct       = (wk_x_pts / total_x * 100) if total_x > 0 else 0
    qualifies = wk_x_pts >= threshold and threshold > 0

    lb   = await get_period_leaderboard("week", limit=100)
    rank = next((i+1 for i, row in enumerate(lb) if row["tg_uid"] == tg_uid), "?")

    if qualifies and isinstance(rank, int):
        est        = TIER_AMOUNTS.get(rank, 4)
        status_box = "✅ <b>QUALIFYING THIS WEEK!</b>"
        reward_lines = [
            f"  Estimated reward: <b>${est}</b> in $POM",
            f"  Current rank: <b>#{rank}</b>",
        ]
    else:
        needed       = max(0, int(threshold) - wk_x_pts)
        status_box   = "❌ <b>Not qualifying yet</b>"
        reward_lines = [f"  X points needed: <b>{needed:,} more</b>"]

    rollover = float(await get_meta("rollover_usd", 0.0) or 0.0)
    pool     = WEEKLY_POOL_USD + rollover
    rollover_lines = [f"  Rollover from last week: <b>${rollover:.2f}</b>"] if rollover > 0 else []

    if d.get("wallet"):
        wall_line = f"  ✅ <code>{esc(d['wallet'][:8])}…{esc(d['wallet'][-6:])}</code>"
    else:
        wall_line = "  ⚠️ <i>Not set!</i> Use /wallet before Sunday"

    lines = [
        "💰 <b>Weekly Reward Status</b>",
        DIV, "",
        status_box, "",
        DIV2,
        "📊 <b>Your Position</b>",
        f"  This week total: <b>{wk_pts:,} pts</b>",
        f"  X points (count for rewards): <b>{wk_x_pts:,}</b>",
        f"  Community X total: <b>{total_x:,}</b>",
        f"  Qualify threshold (70%): <b>{int(threshold):,}</b>",
        f"  Your share of X pool: <b>{pct:.1f}%</b>",
    ] + reward_lines + [
        "",
        DIV2,
        "💵 <b>This Week's Pool</b>",
        f"  Base pool: <b>${WEEKLY_POOL_USD:.0f}</b>",
    ] + rollover_lines + [
        f"  Total available: <b>${pool:.2f}</b>",
        f"  Top {TOP_N} qualifiers share this pool",
        "",
        DIV2,
        "💳 <b>Payout Wallet</b>",
        wall_line,
        DIV,
        "<i>Rewards distributed every Sunday automatically</i>",
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
    lines += ["", "", DIV, "<i>Updated on /syncx | Resets every Sunday</i>"]
    return "\n".join(lines)


async def render_weeklypreview(tg_uid: int, is_owner_view: bool) -> str:
    total_x   = await get_total_x_points_this_week()
    threshold = total_x * QUALIFY_PCT if total_x > 0 else 0

    rows = await get_period_leaderboard("week", limit=100)
    enriched = [(str(r["tg_uid"]), r, r["total"], r["x_pts"]) for r in rows]

    qualifying = [t for t in enriched if t[3] >= threshold]
    not_yet    = [t for t in enriched if t[3] <  threshold]

    c_rank = next((i+1 for i,(uid,_,_,_) in enumerate(enriched) if uid==str(tg_uid)), None)
    c_xp   = next((xp for uid,_,_,xp in enriched if uid==str(tg_uid)), 0)

    if c_rank and c_xp >= threshold and threshold > 0:
        est = TIER_AMOUNTS.get(c_rank, 4)
        your_line = f"✅ <b>You: Rank #{c_rank} — Qualifying — Est. ${est}</b>"
    elif c_rank:
        needed = max(0, int(threshold) - c_xp)
        your_line = f"⏳ <b>You: Rank #{c_rank} — Need {needed:,} more X pts</b>"
    else:
        your_line = "<i>No activity yet this week</i>"

    lines = [
        "📊 <b>Weekly Standings Preview</b>",
        DIV,
        f"🌐 Community X points: <b>{total_x:,}</b>",
        f"📏 Qualify threshold (70%): <b>{int(threshold):,}</b>",
        "📅 Resets: <b>Sunday midnight UTC</b>",
        DIV2,
        your_line,
        DIV2,
        (f"🏆 <b>Top {min(len(qualifying), TOP_N)} Qualifying</b>"
         if qualifying else "<i>No qualifiers yet</i>"),
    ]

    for i, (uid, r, tot, xp) in enumerate(qualifying[:TOP_N], 1):
        name = esc(r.get("username") or r.get("first_name") or "Unknown")
        est  = TIER_AMOUNTS.get(i, 4)
        wall = " 💳" if r.get("wallet") else (" ⚠️" if is_owner_view else "")
        lines.append(f"{MEDALS[i-1]} @{name} — <b>{xp:,} X pts</b> — Est. <b>${est}</b>{wall}")

    if not_yet:
        lines += ["", DIV2, "❌ <b>Not Qualifying Yet</b>"]
        for uid, r, tot, xp in not_yet[:8]:
            name   = esc(r.get("username") or r.get("first_name") or "Unknown")
            needed = max(0, int(threshold) - xp)
            lines.append(f"  • @{name} — {xp:,} pts (need {needed:,} more)")
        if len(not_yet) > 8:
            lines.append(f"  <i>… and {len(not_yet)-8} others</i>")

    if is_owner_view and qualifying:
        no_wall = [esc(r.get("username") or r.get("first_name") or "Unknown")
                   for _, r, _, _ in qualifying[:TOP_N] if not r.get("wallet")]
        if no_wall:
            lines += ["", DIV2, "⚠️ <b>Missing Wallets (won't receive payout):</b>"]
            for n in no_wall:
                lines.append(f"  • @{n}")
            lines.append("<i>They need to run /wallet before Sunday</i>")

    lines += [
        "", DIV,
        ("<i>Run /distribute on Sunday to pay everyone automatically</i>"
         if is_owner_view else "<i>Keep raiding! Payouts every Sunday.</i>"),
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
        f"{DIV}\n"
        "<i>Rewards are paid automatically every Sunday in $POM</i>"
    )

# ─────────────────────────────────────────────────────────────────────────────
#  USER COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

HTML = ParseMode.HTML

async def _reply(update: Update, text: str, kb=None, preview=False):
    """Helper to send a reply with HTML parse mode."""
    await update.effective_message.reply_text(
        text, parse_mode=HTML, reply_markup=kb,
        disable_web_page_preview=not preview,
    )


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u = update.effective_user
    await get_or_create_user(u.id, u.username or "", u.first_name or "")
    await _reply(update, render_start(u.first_name or "friend"), kb_main())


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


async def cmd_linkx(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Link an X account. FIXED: was returning 'not found' due to caching/API errors."""
    if not await guard_group(update): return
    u = update.effective_user

    if not ctx.args:
        await _reply(update,
            "🐦 <b>Link Your X Account</b>\n\n"
            "Usage: <code>/linkx @yourhandle</code>\n\n"
            "<i>Example: /linkx @Glayzz_4T9ne_BK</i>\n\n"
            "Your X account is used to track raid engagement and award points.")
        return

    handle = ctx.args[0].lstrip("@").strip()
    if not re.match(r"^[A-Za-z0-9_]{1,15}$", handle):
        await _reply(update,
            f"❌ <b>Invalid Handle</b>\n\n"
            f"<code>{esc(handle)}</code> doesn't look like a valid X handle.\n"
            "Should be 1–15 characters, letters/numbers/underscore only.")
        return

    msg = await update.effective_message.reply_text(
        f"🔄 Verifying <b>@{esc(handle)}</b> on X…",
        parse_mode=HTML,
    )

    if not X_BEARER_TOKEN:
        # Skip verification if no API token — just save the handle
        d = await get_or_create_user(u.id, u.username or "", u.first_name or "")
        d["x_handle"]    = handle
        d["x_user_id"]   = None
        d["x_followers"] = 0
        await save_user(d)
        await msg.edit_text(
            f"⚠️ <b>X Linked (Unverified)</b>\n\n"
            f"🐦 @{esc(handle)}\n\n"
            "<i>X_BEARER_TOKEN not set on server — handle saved but not verified.\n"
            "Admin will verify on next /syncx.</i>",
            parse_mode=HTML, reply_markup=kb_back(),
        )
        return

    xid, followers = get_x_user(handle)

    if not xid:
        await msg.edit_text(
            f"❌ <b>Account Not Found</b>\n\n"
            f"Could not find <b>@{esc(handle)}</b> on X.\n\n"
            "Possible reasons:\n"
            "• Handle spelled wrong\n"
            "• Account is suspended or private\n"
            "• X API temporarily rate-limited\n\n"
            "<i>Try again in a minute, or double-check your handle.</i>",
            parse_mode=HTML, reply_markup=kb_back(),
        )
        return

    if followers < PERSONAL_POST["min_followers"]:
        await msg.edit_text(
            f"❌ <b>Insufficient Followers</b>\n\n"
            f"@{esc(handle)} has <b>{followers}</b> followers.\n"
            f"Minimum required: <b>{PERSONAL_POST['min_followers']}</b>\n\n"
            "<i>Keep building your X presence and try again!</i>",
            parse_mode=HTML, reply_markup=kb_back(),
        )
        return

    d = await get_or_create_user(u.id, u.username or "", u.first_name or "")
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
        "• Dropping your X posts in the group\n\n"
        "<i>Points sync on the next /syncx</i>",
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
        "This will:\n"
        "• Remove your X account from PomRaid\n"
        "• Reset your X score to <b>0</b>\n"
        "• Clear your raid engagement history\n\n"
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
            "❌ X_BEARER_TOKEN not set on server. Cannot fetch live data.",
            parse_mode=HTML, reply_markup=kb_back())
        return

    xid, followers = get_x_user(handle)
    if not xid:
        await msg.edit_text(
            f"❌ Could not reach X API for <b>@{esc(handle)}</b>.\n"
            "Try again later.",
            parse_mode=HTML, reply_markup=kb_back())
        return

    d["x_user_id"]   = xid
    d["x_followers"] = followers
    await save_user(d)

    await msg.edit_text(
        f"✅ <b>X Data Refreshed!</b>\n\n"
        f"🐦 @{esc(handle)}\n"
        f"👥 Followers: <b>{followers:,}</b>\n\n"
        "Your X account is up to date.",
        parse_mode=HTML, reply_markup=kb_back(),
    )


async def cmd_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u = update.effective_user

    if not ctx.args:
        await _reply(update,
            "💳 <b>Set Your Payout Wallet</b>\n\n"
            "Usage: <code>/wallet 0xYourBNBWalletAddress</code>\n\n"
            "<i>Example: /wallet 0xAbCd1234…</i>")
        return

    address = ctx.args[0].strip()
    if not re.match(r"^0x[0-9a-fA-F]{40}$", address):
        await _reply(update,
            "❌ <b>Invalid Address</b>\n\n"
            "Must be a valid BNB Chain address starting with 0x.\n"
            "(0x followed by 40 hex characters.)")
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
#  CALLBACK HANDLER (button presses)
# ─────────────────────────────────────────────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q     = update.callback_query
    data  = q.data
    u     = q.from_user
    await q.answer()

    d = await get_or_create_user(u.id, u.username or "", u.first_name or "")

    async def edit(text: str, kb=None):
        try:
            await q.edit_message_text(text, parse_mode=HTML, reply_markup=kb,
                                      disable_web_page_preview=True)
        except Exception as e:
            logger.warning(f"edit_message_text failed: {e}")

    if data == "back_main":
        await edit(render_start(u.first_name or "friend"), kb_main())
    elif data == "score":
        await edit(render_score(d), kb_score())
    elif data == "stats":
        await edit(render_stats(d, u.first_name or "friend"), kb_back())
    elif data == "howto":
        await edit(render_howto(), kb_back())
    elif data == "rewardstatus":
        await edit(await render_reward_status(u.id), kb_back())
    elif data == "weeklypreview":
        text = await render_weeklypreview(u.id, is_owner(u.id))
        if len(text) > 4000:
            text = text[:3950] + "\n<i>… message truncated</i>"
        await edit(text, kb_back())
    elif data == "wallet_info":
        await edit(render_wallet_info(d), kb_back())
    elif data == "unwallet":
        if not d.get("wallet"):
            await edit(
                "ℹ️ You don't have a wallet set.\n\nUse /wallet to add one.",
                kb_back())
        else:
            await edit(
                f"🗑️ <b>Remove Wallet?</b>\n\n"
                f"Address: <code>{esc(d['wallet'][:8])}…{esc(d['wallet'][-6:])}</code>\n\n"
                "You won't receive rewards without a wallet.\n"
                "<i>This cannot be undone.</i>",
                kb_confirm("unwallet"))
    elif data == "unwallet_confirm":
        old = d.get("wallet", "")
        d["wallet"] = None
        await save_user(d)
        await edit(
            f"✅ <b>Wallet Removed</b>\n\n"
            f"<code>{esc(old[:8])}…</code> has been unlinked.\n\n"
            "Use /wallet anytime to set a new address.",
            kb_main())
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
            "Send the command:\n<code>/linkx @YourXHandle</code>\n\n"
            "<i>Example: /linkx @Glayzz_4T9ne_BK</i>",
            kb_back())
    elif data == "refreshx":
        if not d.get("x_handle"):
            await edit(
                "ℹ️ You don't have an X account linked.\n\nUse /linkx to connect one.",
                kb_back())
        else:
            handle = d["x_handle"]
            if not X_BEARER_TOKEN:
                await edit(
                    "❌ X_BEARER_TOKEN not set on server.",
                    kb_back())
                return
            xid, followers = get_x_user(handle)
            if not xid:
                await edit(
                    f"❌ Could not reach X API for <b>@{esc(handle)}</b>.\n"
                    "Try again later.",
                    kb_back())
                return
            d["x_user_id"]   = xid
            d["x_followers"] = followers
            await save_user(d)
            await edit(
                f"✅ <b>X Data Refreshed!</b>\n\n"
                f"🐦 @{esc(handle)}\n"
                f"👥 Followers: <b>{followers:,}</b>",
                kb_back())
    elif data == "unlinkx":
        if not d.get("x_handle"):
            await edit(
                "ℹ️ No X account linked.\n\nUse /linkx to connect one.",
                kb_back())
        else:
            await edit(
                f"⚠️ <b>Confirm Unlink</b>\n\n"
                f"Unlink <b>@{esc(d['x_handle'])}</b>?\n\n"
                "Your X score will be reset to <b>0</b> and all raid history cleared.\n"
                "<i>This cannot be undone.</i>",
                kb_confirm("unlinkx"))
    elif data == "unlinkx_confirm":
        old = d.get("x_handle", "")
        d["x_handle"]    = None
        d["x_user_id"]   = None
        d["x_followers"] = 0
        d["x_data"]      = _blank_x_data()
        # Zero out X scores
        for p in d.get("scores", {}).values():
            p["x"] = 0
        await save_user(d)
        await edit(
            f"✅ <b>Unlinked Successfully</b>\n\n"
            f"@{esc(old)} has been removed from your profile.\n"
            "Your X score has been reset to 0.\n\n"
            "Use /linkx anytime to connect a new account.",
            kb_main())

# ─────────────────────────────────────────────────────────────────────────────
#  ADMIN COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update, ctx): return
    if not update.message.reply_to_message:
        await _reply(update, "↩️ Reply to the user's message to ban them.")
        return
    target = update.message.reply_to_message.from_user
    await ctx.bot.ban_chat_member(update.effective_chat.id, target.id)
    await _reply(update,
        f"🚫 <b>{esc(target.first_name)}</b> has been banned from the POM Army.")


async def cmd_mute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update, ctx): return
    if not update.message.reply_to_message:
        await _reply(update, "↩️ Reply to the user's message to mute them.")
        return
    target  = update.message.reply_to_message.from_user
    minutes = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 10
    until   = datetime.utcnow() + timedelta(minutes=minutes)
    await ctx.bot.restrict_chat_member(
        update.effective_chat.id, target.id,
        permissions=ChatPermissions(can_send_messages=False), until_date=until,
    )
    await _reply(update,
        f"🔇 <b>{esc(target.first_name)}</b> muted for <b>{minutes} minutes</b>.")


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
        await ctx.bot.ban_chat_member(update.effective_chat.id, target.id)
        await _reply(update,
            f"🚫 <b>{esc(target.first_name)}</b> hit 3 warnings — permanently banned.")
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

# ─────────────────────────────────────────────────────────────────────────────
#  OWNER-ONLY COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_syncx(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_owner(update): return
    if not X_BEARER_TOKEN:
        await _reply(update, "❌ X_BEARER_TOKEN not set in .env.")
        return

    last_sync = await get_meta("last_x_sync")
    if last_sync:
        try:
            elapsed = datetime.utcnow() - datetime.fromisoformat(last_sync)
            if elapsed < timedelta(minutes=SYNC_COOLDOWN_MINS):
                rem = int((timedelta(minutes=SYNC_COOLDOWN_MINS) - elapsed).total_seconds() // 60)
                await _reply(update,
                    f"⏳ Sync on cooldown. Next sync available in <b>{rem} min</b>.")
                return
        except Exception:
            pass

    status = await update.effective_message.reply_text(
        "🔄 <b>Syncing X Engagement…</b>\n\n"
        "Checking all registered raid tweets.\n"
        "<i>This may take up to 30 seconds.</i>",
        parse_mode=HTML,
    )

    summary = await sync_x_engagement()

    err_line = (f"\n\n⚠️ Errors: {esc(', '.join(summary['errors']))}"
                if summary["errors"] else "")
    await status.edit_text(
        f"✅ <b>X Sync Complete</b>\n{DIV}\n\n"
        f"📊 Raid tweets checked: <b>{summary['tweets']}</b>\n"
        f"📣 Engagements credited: <b>{summary['engagements']}</b>\n"
        f"✍️ Organic posts credited: <b>{summary['organic']}</b>{err_line}",
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
    await _reply(update, f"✅ All warnings cleared for <b>{esc(target.first_name)}</b>.")


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


async def cmd_distribute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_owner(update): return

    status = await update.effective_message.reply_text(
        "💰 <b>Computing Weekly Rewards…</b>\n\n"
        "Calculating 70% threshold, ranking qualifiers, fetching $POM price…",
        parse_mode=HTML,
    )

    rewards = await compute_weekly_rewards()
    if not rewards:
        await status.edit_text(
            "⚠️ <b>No Qualifying Members</b>\n\n"
            "Nobody scored 70%+ of the total weekly X points pool.\n"
            "The full pool has been rolled over to next week.",
            parse_mode=HTML,
        )
        return

    price = get_pom_price()
    lines = [
        "🏆 <b>POM Army Weekly Rewards</b>",
        DIV,
        f"💵 Pool: <b>${WEEKLY_POOL_USD:.0f}</b> | $POM Price: <code>${price:.6f}</code>",
        f"👥 Winners: <b>{len(rewards)}</b>",
        DIV2, "",
    ]
    no_wallet = []

    for uid, r in rewards.items():
        name   = esc(r["username"])
        wallet = r["wallet"]
        if not wallet:
            no_wallet.append(name)
            lines.append(f"{MEDALS[r['rank']-1]} @{name} — ${r['usd_amount']} — ⚠️ <i>No wallet set</i>")
            continue

        await status.edit_text(
            f"💸 Sending to @{name} (Rank #{r['rank']})…",
            parse_mode=HTML,
        )
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

    # Save reward distribution to meta
    await set_meta("last_week_rewards", {
        "distributed_at": _now(),
        "total_pool":     WEEKLY_POOL_USD,
        "winners":        list(rewards.values()),
    })

    if no_wallet:
        nw = ", ".join(f"@{n}" for n in no_wallet)
        lines += ["", DIV2,
                  f"⚠️ No wallet: {nw}",
                  "<i>Their rewards rolled over to next week</i>"]

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

    await status.edit_text(
        "✅ Distribution complete! Results posted in the group.",
        parse_mode=HTML,
    )

# ─────────────────────────────────────────────────────────────────────────────
#  MESSAGE HANDLER
# ─────────────────────────────────────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    chat = update.effective_chat
    if chat.type != "private" and chat.id != RAID_GROUP_ID:
        return  # ignore other groups

    u    = update.effective_user
    msg  = update.message
    text = msg.text or ""

    # In private chat, don't run flood/spam/raid logic — just register the user
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

    # X link drop handling
    x_match = X_LINK_RE.search(text)
    if x_match:
        tweet_id = x_match.group(1)

        if not await tweet_already_registered(tweet_id):
            # Fetch author + text from X
            tweet_author_xid = None
            tweet_text       = ""
            if X_BEARER_TOKEN:
                tweet_author_xid, tweet_text = get_tweet_data(tweet_id)

            # Check if POM-related (must mention $POM or @Pom_bsc)
            known_pom_raw = await get_meta("known_pom_tweets", [])
            known_pom     = set(known_pom_raw) if isinstance(known_pom_raw, list) else set()
            is_pom_official = tweet_id in known_pom
            pom_related     = is_pom_official or tweet_is_pom_related(tweet_text)

            if pom_related:
                user_x_id = d.get("x_user_id")
                is_own    = bool(user_x_id and tweet_author_xid
                                 and tweet_author_xid == user_x_id)

                await register_raid(
                    tweet_id, u.id, display_name(d),
                    is_pom_official, is_own, tweet_author_xid,
                )

                if is_own:
                    # Award personal post points + start cooldown
                    await add_x_points_db(u.id, X_POINTS["post_drop"])
                    d["x_data"]["last_post_drop"] = _now()
                    history = d["x_data"].get("personal_post_history", [])
                    history.append(tweet_text or text)
                    d["x_data"]["personal_post_history"] = history[-PERSONAL_POST["history_count"]:]
                    await save_user(d)
                    await msg.reply_text(
                        f"🔗 <b>Personal Raid Post Registered!</b>\n"
                        f"{DIV2}\n\n"
                        "✅ Verified as <b>your</b> post\n"
                        "✅ Contains <b>$POM</b> mention\n"
                        f"🎯 <b>+{X_POINTS['post_drop']} pts</b> awarded\n"
                        "⏳ <b>12-hour cooldown</b> started\n\n"
                        "🐶 <b>POM Army — go raid it!</b> 🔥\n"
                        "<i>Everyone who engages earns points</i>",
                        parse_mode=HTML, disable_web_page_preview=True,
                    )
                else:
                    if user_x_id and tweet_author_xid and tweet_author_xid != user_x_id:
                        note = "<i>(Not your post — no personal points)</i>\n"
                    elif not user_x_id:
                        note = "<i>(Link your X with /linkx to earn personal post points)</i>\n"
                    else:
                        note = ""
                    await msg.reply_text(
                        f"🔗 <b>Community Raid Target Registered!</b>\n"
                        f"{DIV2}\n\n"
                        "✅ Post contains <b>$POM</b> mention\n"
                        "🎯 0 pts to dropper\n"
                        f"{note}\n"
                        "🐶 <b>POM Army — go raid this post!</b> 🔥\n"
                        "<i>Like | Repost | Comment | Quote for points</i>",
                        parse_mode=HTML, disable_web_page_preview=True,
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

    # TG activity points (min words + daily cap)
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
        # Bot was added to a non-official group — leave
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
                "You've just joined one of the most active communities in crypto.\n\n"
                "📌 <b>Get started in 3 steps:</b>\n"
                "1. Register → /start\n"
                "2. Link your X → /linkx\n"
                "3. Learn how to earn → /howto\n\n"
                f"{DIV2}\n"
                "<i>Raid hard. Earn $POM. Never stop.</i> 🔥",
                parse_mode=HTML,
            )
        except Exception as e:
            logger.warning(f"Welcome failed: {e}")

# ─────────────────────────────────────────────────────────────────────────────
#  SCHEDULED TASKS
# ─────────────────────────────────────────────────────────────────────────────

async def poll_pom_tweets(app: Application):
    """Every 15 min: check @Pom_bsc for new tweets, register them, alert group."""
    global POM_X_ID
    if not X_BEARER_TOKEN:
        return
    if not POM_X_ID:
        POM_X_ID, _ = get_x_user(POM_X_HANDLE)
        if not POM_X_ID:
            return

    tweets    = get_pom_recent_tweets(POM_X_ID, max_results=5)
    known_raw = await get_meta("known_pom_tweets", [])
    known     = set(known_raw) if isinstance(known_raw, list) else set()
    new_count = 0

    for tweet in tweets:
        tid = tweet["id"]
        if tid in known:
            continue
        known.add(tid)

        if not await tweet_already_registered(tid):
            await register_raid(tid, None, f"@{POM_X_HANDLE}", True, False, None)

        url = f"https://x.com/{POM_X_HANDLE}/status/{tid}"
        try:
            await app.bot.send_message(
                chat_id=RAID_GROUP_ID,
                text=(
                    f"🚨 <b>NEW @Pom_bsc POST — RAID NOW!</b> 🐶\n"
                    f"{DIV}\n\n"
                    f"🔗 {url}\n\n"
                    "Engage for points:\n"
                    f"👍 Like <b>+{X_POINTS['like']}</b> | "
                    f"🔁 Repost <b>+{X_POINTS['repost']}</b> | "
                    f"💬 Comment <b>+{X_POINTS['comment']}</b> | "
                    f"🗨️ Quote <b>+{X_POINTS['quote']}</b>\n\n"
                    f"{DIV2}\n"
                    "<i>Points sync on next /syncx</i>"
                ),
                parse_mode=HTML, disable_web_page_preview=False,
            )
            new_count += 1
        except Exception as e:
            logger.error(f"Group post failed: {e}")

    await set_meta("known_pom_tweets", list(known)[-200:])
    if new_count:
        logger.info(f"Posted {new_count} new POM tweet(s)")


async def weekly_reset(app: Application):
    """Every Sunday midnight UTC: reset weekly scores and raid registry."""
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
                "Weekly scores have been reset.\n"
                "A fresh week means a fresh shot at the top 10.\n\n"
                "📌 <b>Reminder:</b>\n"
                "• Raid every <b>@Pom_bsc</b> post\n"
                "• Drop your own posts for bonus points\n"
                "• Set your wallet with /wallet to get paid\n\n"
                f"{DIV}\n"
                "<i>$POM Army — Let's run it up!</i> 🚀"
            ),
            parse_mode=HTML,
        )
    except Exception as e:
        logger.error(f"Weekly reset announcement failed: {e}")


async def daily_reset(app: Application):
    """Every day midnight UTC: reset 'day' scores."""
    await reset_period_scores("day")
    logger.info("Daily reset complete")


async def monthly_reset(app: Application):
    """First day of month midnight UTC: reset monthly scores."""
    if datetime.utcnow().day == 1:
        await reset_period_scores("month")
        logger.info("Monthly reset complete")

# ─────────────────────────────────────────────────────────────────────────────
#  STARTUP / MAIN
# ─────────────────────────────────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    """Run after PTB initialisation: DB connect + register BotFather commands."""
    await init_db()
    logger.info("PostgreSQL connected.")

    member_cmds = [
        BotCommand("start",         "Open main menu"),
        BotCommand("score",         "View your score card"),
        BotCommand("stats",         "View your full profile"),
        BotCommand("lb",            "Leaderboard (add: week/month/day)"),
        BotCommand("howto",         "How to earn points"),
        BotCommand("rules",         "Full rules & point breakdown"),
        BotCommand("list",          "Show all commands"),
        BotCommand("linkx",         "Link your X account"),
        BotCommand("unlinkx",       "Unlink your X account"),
        BotCommand("refreshx",      "Refresh X follower data"),
        BotCommand("wallet",        "Set your payout wallet"),
        BotCommand("unwallet",      "Remove your payout wallet"),
        BotCommand("rewardstatus",  "Check weekly reward eligibility"),
        BotCommand("weeklypreview", "See full weekly standings"),
        BotCommand("help",          "Get help & support"),
    ]
    admin_cmds = member_cmds + [
        BotCommand("ban",      "Ban a user (reply)"),
        BotCommand("mute",     "Mute a user (reply)"),
        BotCommand("warn",     "Warn a user (reply)"),
        BotCommand("announce", "Post & pin announcement"),
    ]
    owner_cmds = admin_cmds + [
        BotCommand("syncx",         "Sync X engagement scores"),
        BotCommand("distribute",    "Run weekly payout"),
        BotCommand("resetlb",       "Reset leaderboard period"),
        BotCommand("resetoffenses", "Clear user warnings"),
    ]
    try:
        await app.bot.set_my_commands(member_cmds, scope=BotCommandScopeDefault())
        await app.bot.set_my_commands(
            owner_cmds, scope=BotCommandScopeChat(chat_id=OWNER_ID))
        logger.info("BotFather commands registered.")
    except Exception as e:
        logger.warning(f"Could not register BotFather commands: {e}")

    # Start scheduler
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(poll_pom_tweets, "interval", minutes=15, args=[app])
    scheduler.add_job(weekly_reset, "cron", day_of_week="sun", hour=0, minute=0, args=[app])
    scheduler.add_job(daily_reset,  "cron", hour=0, minute=0, args=[app])
    scheduler.add_job(monthly_reset, "cron", day=1, hour=0, minute=5, args=[app])
    scheduler.start()
    logger.info("Scheduler started.")


async def post_shutdown(app: Application) -> None:
    await close_db()


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set in environment.")

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

    # Owner-only commands
    app.add_handler(CommandHandler("syncx",         cmd_syncx))
    app.add_handler(CommandHandler("distribute",    cmd_distribute))
    app.add_handler(CommandHandler("resetlb",       cmd_resetlb))
    app.add_handler(CommandHandler("resetoffenses", cmd_resetoffenses))

    # Callbacks + messages
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🐶 PomRaid Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()