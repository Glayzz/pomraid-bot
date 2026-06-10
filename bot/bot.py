"""
PomRaid Bot — $POM Community Raid Tracker & Auto-Payout
BNB Chain | @Pom_bsc | Python 3.11

Permission tiers:
  OWNER (5228498784) — all commands including distribute, resetlb, syncx
  ADMIN (group admins) — ban, mute, warn, announce
  MEMBER — score, stats, lb, linkx, wallet, howto, rewardstatus
  GROUP LOCK — bot only works in official POM group (-1002483287072)
"""

import logging
import json
import os
import re
import difflib
from datetime import datetime, timedelta
from collections import defaultdict

from dotenv import load_dotenv
load_dotenv()

import httpx
import asyncpg
from web3 import Web3
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from database import (
    init_db, close_db, get_or_create_user, save_user,
    get_all_users, get_raids, register_raid,
    tweet_already_registered, is_engagement_credited,
    credit_engagement, clear_week_raids,
    add_tg_points_db, add_x_points_db,
    get_today_tg_points, reset_period_scores,
    get_period_leaderboard, get_weekly_leaderboard,
    get_total_x_points_this_week,
    get_meta, set_meta,
)

from telegram import (
    Update, ChatPermissions,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────────

BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "")
PAY_WALLET_KEY = os.getenv("PAY_WALLET_KEY", "")

POM_X_HANDLE   = "Pom_bsc"
POM_X_ID       = None

# ── Security ──────────────────────────────────────────────────────────────────
OWNER_ID       = 5228498784          # only this user can run critical commands
RAID_GROUP_ID  = -1002483287072      # only group the bot operates in

# ── BNB Chain ─────────────────────────────────────────────────────────────────
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
    {"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],
     "stateMutability":"view","type":"function"},
]

# ── Rewards ───────────────────────────────────────────────────────────────────
WEEKLY_POOL_USD = 75.0
TIER_AMOUNTS    = {1:18, 2:13, 3:10, 4:6, 5:6, 6:6, 7:4, 8:4, 9:4, 10:4}
TOP_N           = 10
QUALIFY_PCT     = 0.70

# ── X engagement ──────────────────────────────────────────────────────────────
LINK_COOLDOWN_HOURS = 12
SYNC_COOLDOWN_MINS  = 60
X_POINTS = {"like":5, "repost":10, "comment":15, "quote":20, "post_drop":25}
PERSONAL_POST = {
    "min_words":50, "require_image":True,
    "min_followers":50, "similarity_limit":0.70, "history_count":5,
}

# ── Spam ──────────────────────────────────────────────────────────────────────
SPAM = {"max_per_minute":8, "max_links":3, "mute_minutes":10, "ban_on_third_offense":True}
BANNED_WORDS = ["scam","rug pull","double your bnb","giveaway http","dm me for profit","investment guaranteed"]
LINK_RE   = re.compile(r"https?://\S+", re.IGNORECASE)
X_LINK_RE = re.compile(r"https?://(?:twitter\.com|x\.com)/\S+/status/(\d+)", re.IGNORECASE)

# TG point rules — spam protection
TG_MIN_WORDS  = 8    # messages under this get 0 TG points
TG_DAILY_CAP  = 20   # max TG points earned per day per user

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
#  MARKDOWN V2 SAFETY
# ─────────────────────────────────────────────────────────────────────────────

_SPECIAL = r"\_*[]()~`>#+-=|{}.!"

def safe(text: str) -> str:
    """Escape all MarkdownV2 special characters."""
    out = []
    for ch in str(text):
        if ch in _SPECIAL:
            out.append(f"\\{ch}")
        else:
            out.append(ch)
    return "".join(out)

def md_link(label: str, url: str) -> str:
    """Create a MarkdownV2 hyperlink."""
    return f"[{label}]({url})"

# ─────────────────────────────────────────────────────────────────────────────
#  PERMISSION GUARDS
# ─────────────────────────────────────────────────────────────────────────────

def is_owner(uid: int) -> bool:
    return uid == OWNER_ID

async def is_group_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        admins = await ctx.bot.get_chat_administrators(update.effective_chat.id)
        return any(a.user.id == update.effective_user.id for a in admins)
    except Exception:
        return False

async def is_official_group(update: Update) -> bool:
    """Returns True if message is from the official POM group or a private chat."""
    chat = update.effective_chat
    # Allow private messages (DMs) for member commands
    if chat.type == "private":
        return True
    return chat.id == RAID_GROUP_ID

async def guard_owner(update: Update) -> bool:
    """
    Silently ignore if not owner.
    Returns True if allowed, False if blocked.
    """
    if not is_owner(update.effective_user.id):
        await update.message.reply_text(
            "🔒 This command is restricted to the bot owner\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return False
    return True

async def guard_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if user is owner OR group admin."""
    if is_owner(update.effective_user.id):
        return True
    if await is_group_admin(update, ctx):
        return True
    await update.message.reply_text(
        "🔒 This command is for group admins only\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return False

async def guard_group(update: Update) -> bool:
    """
    Block all interaction from groups other than the official POM group.
    Returns True if allowed.
    """
    chat = update.effective_chat
    if chat.type == "private":
        return True
    if chat.id != RAID_GROUP_ID:
        try:
            await update.message.reply_text(
                "⛔ PomRaid only operates in the official POM Army group\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            await ctx_bot_leave(update)
        except Exception:
            pass
        return False
    return True

# We need a reference to the app bot for leaving groups
_app_ref = None

async def ctx_bot_leave(update: Update):
    try:
        if _app_ref:
            await _app_ref.bot.leave_chat(update.effective_chat.id)
    except Exception as e:
        logger.warning(f"Could not leave chat: {e}")

# ─────────────────────────────────────────────────────────────────────────────
#  DATABASE  — thin async wrappers that keep bot logic unchanged
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.utcnow().isoformat()

def _blank_bucket() -> dict:
    return {"tg": 0, "x": 0, "reset_at": _now()}

def _blank_x_data() -> dict:
    return {
        "last_sync": None, "last_post_drop": None,
        "personal_post_history": [], "credited_engagements": {},
    }

# x_week_pts helper (used in reward computation)
def x_week_pts(user_dict: dict) -> int:
    return user_dict.get("scores", {}).get("week", {}).get("x", 0)

def period_total(user_dict: dict, period: str) -> int:
    b = user_dict.get("scores", {}).get(period, _blank_bucket())
    return b.get("tg", 0) + b.get("x", 0)

def display_name(user_dict: dict) -> str:
    return user_dict.get("username") or user_dict.get("first_name") or "Unknown"

# ── Async DB wrappers — keep all bot logic unchanged ──────────────────────────

async def get_user_async(uid: int, username: str = "", first_name: str = "") -> dict:
    return await get_or_create_user(uid, username, first_name)

async def save_user_async(d: dict) -> None:
    await save_user(d)

async def load_all_users() -> list:
    """Load all users for leaderboard/reward computation."""
    return await get_all_users()

async def get_db_meta(key: str, default=None):
    return await get_meta(key, default)

async def set_db_meta(key: str, value) -> None:
    await set_meta(key, value)


# ─────────────────────────────────────────────────────────────────────────────
#  MARKDOWN V2 SAFETY
# ─────────────────────────────────────────────────────────────────────────────

_SPECIAL = r"\_*[]()~`>#+-=|{}.!"

def safe(text: str) -> str:
    """Escape all MarkdownV2 special characters."""
    out = []
    for ch in str(text):
        if ch in _SPECIAL:
            out.append(f"\\{ch}")
        else:
            out.append(ch)
    return "".join(out)

def md_link(label: str, url: str) -> str:
    """Create a MarkdownV2 hyperlink."""
    return f"[{label}]({url})"

# ─────────────────────────────────────────────────────────────────────────────
#  PERMISSION GUARDS
# ─────────────────────────────────────────────────────────────────────────────

def is_owner(uid: int) -> bool:
    return uid == OWNER_ID

async def is_group_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        admins = await ctx.bot.get_chat_administrators(update.effective_chat.id)
        return any(a.user.id == update.effective_user.id for a in admins)
    except Exception:
        return False

async def is_official_group(update: Update) -> bool:
    """Returns True if message is from the official POM group or a private chat."""
    chat = update.effective_chat
    # Allow private messages (DMs) for member commands
    if chat.type == "private":
        return True
    return chat.id == RAID_GROUP_ID

async def guard_owner(update: Update) -> bool:
    """
    Silently ignore if not owner.
    Returns True if allowed, False if blocked.
    """
    if not is_owner(update.effective_user.id):
        await update.message.reply_text(
            "🔒 This command is restricted to the bot owner\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return False
    return True

async def guard_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if user is owner OR group admin."""
    if is_owner(update.effective_user.id):
        return True
    if await is_group_admin(update, ctx):
        return True
    await update.message.reply_text(
        "🔒 This command is for group admins only\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return False

async def guard_group(update: Update) -> bool:
    """
    Block all interaction from groups other than the official POM group.
    Returns True if allowed.
    """
    chat = update.effective_chat
    if chat.type == "private":
        return True
    if chat.id != RAID_GROUP_ID:
        try:
            await update.message.reply_text(
                "⛔ PomRaid only operates in the official POM Army group\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            await ctx_bot_leave(update)
        except Exception:
            pass
        return False
    return True

# We need a reference to the app bot for leaving groups
_app_ref = None

async def ctx_bot_leave(update: Update):
    try:
        if _app_ref:
            await _app_ref.bot.leave_chat(update.effective_chat.id)
    except Exception as e:
        logger.warning(f"Could not leave chat: {e}")

# ─────────────────────────────────────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.utcnow().isoformat()

def _blank_bucket() -> dict:
    return {"tg":0, "x":0, "reset_at":_now()}

def _blank_x_data() -> dict:
    return {
        "last_sync":None, "last_post_drop":None,
        "personal_post_history":[], "credited_engagements":{},
    }

def load_db() -> dict:
    os.makedirs("data", exist_ok=True)
    if os.path.exists(DB_FILE):
        with open(DB_FILE,"r",encoding="utf-8") as f:
            db = json.load(f)
    else:
        db = {}
    db.setdefault("users",{})
    db.setdefault("meta",{
        "last_x_sync":None, "known_pom_tweets":[],
        "registered_raids":{}, "week_start":None,
        "rollover_usd":0.0, "last_week_rewards":{},
        "raid_group_id":RAID_GROUP_ID,
    })
    return db

def save_db(db: dict):
    with open(DB_FILE,"w",encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

def get_user(db: dict, uid: int, username:str="", first_name:str="") -> dict:
    key = str(uid)
    if key not in db["users"]:
        db["users"][key] = {
            "username":username, "first_name":first_name,
            "x_handle":None, "x_user_id":None, "x_followers":0,
            "wallet":None, "offenses":0, "tg_uid":uid,
            "joined":_now(), "last_active":_now(),
            "scores":{k:_blank_bucket() for k in ("alltime","month","week","day")},
            "x_data":_blank_x_data(),
        }
    u = db["users"][key]
    if username:   u["username"]   = username
    if first_name: u["first_name"] = first_name
    for f,d in [("x_data",_blank_x_data()),("x_user_id",None),("x_followers",0),("wallet",None),("tg_uid",uid)]:
        u.setdefault(f,d)
    u.setdefault("scores",{k:_blank_bucket() for k in ("alltime","month","week","day")})
    return u

def add_x_pts(u,pts):
    for b in u["scores"].values(): b["x"]+=pts

def add_tg_pts(u,pts):
    for b in u["scores"].values(): b["tg"]+=pts

def period_total(u,p):
    b=u["scores"].get(p,_blank_bucket()); return b["tg"]+b["x"]

def display_name(u):
    return u.get("username") or u.get("first_name") or "Unknown"

PERIOD_META = {
    "day":("📅","Today"), "week":("📆","This Week"),
    "month":("🗓️","This Month"), "alltime":("🏆","All-Time"),
}
MEDALS = ["🥇","🥈","🥉"]+["🔸"]*47

# ─────────────────────────────────────────────────────────────────────────────
#  X API
# ─────────────────────────────────────────────────────────────────────────────

def x_get(url, params=None):
    try:
        r = httpx.get(url, headers={"Authorization":f"Bearer {X_BEARER_TOKEN}"},
                      params=params or {}, timeout=15)
        return r.json() if r.status_code==200 else None
    except Exception as e:
        logger.error(f"X API: {e}"); return None

def get_x_user(handle):
    d = x_get(f"https://api.twitter.com/2/users/by/username/{handle}",
               {"user.fields":"public_metrics"})
    if not d or "data" not in d: return None,0
    return d["data"]["id"], d["data"].get("public_metrics",{}).get("followers_count",0)

def get_tweet_data(tweet_id):
    """
    Fetch author_id + text for a tweet in one API call.
    Returns (author_id, text) or (None, None) on failure.
    """
    d = x_get(
        f"https://api.twitter.com/2/tweets/{tweet_id}",
        {"tweet.fields": "author_id,text"},
    )
    if not d or "data" not in d:
        return None, None
    return d["data"].get("author_id"), d["data"].get("text", "")

def tweet_is_pom_related(text: str) -> bool:
    """Return True if tweet text mentions $POM or @Pom_bsc (case-insensitive)."""
    t = text.lower()
    return "$pom" in t or "@pom_bsc" in t

def get_pom_tweets(pom_id, n=5):
    d = x_get(f"https://api.twitter.com/2/users/{pom_id}/tweets",
               {"max_results":n,"tweet.fields":"created_at","exclude":"retweets,replies"})
    return d.get("data",[]) if d else []

def get_likers(tid):
    d=x_get(f"https://api.twitter.com/2/tweets/{tid}/liking_users",{"max_results":100})
    return [u["id"] for u in d.get("data",[])] if d else []

def get_retweeters(tid):
    d=x_get(f"https://api.twitter.com/2/tweets/{tid}/retweeted_by",{"max_results":100})
    return [u["id"] for u in d.get("data",[])] if d else []

def get_replies_quotes(tid):
    r=x_get("https://api.twitter.com/2/tweets/search/recent",
             {"query":f"conversation_id:{tid} is:reply","max_results":100,"tweet.fields":"author_id"})
    q=x_get("https://api.twitter.com/2/tweets/search/recent",
             {"query":f"url:{tid} is:quote","max_results":100,"tweet.fields":"author_id"})
    return (
        list({t["author_id"] for t in r.get("data",[])} if r else []),
        list({t["author_id"] for t in q.get("data",[])} if q else []),
    )

def search_organic_pom(since_hours=4):
    since=(datetime.utcnow()-timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    d=x_get("https://api.twitter.com/2/tweets/search/recent",{
        "query":"($POM OR @Pom_bsc) -is:retweet -is:reply lang:en",
        "max_results":100,"start_time":since,
        "tweet.fields":"author_id,text,attachments,created_at",
        "expansions":"attachments.media_keys","media.fields":"type",
    })
    if not d or "data" not in d: return []
    media_ids={m["media_key"] for m in d.get("includes",{}).get("media",[])
               if m.get("type") in ("photo","video")}
    for t in d["data"]:
        t["has_image"]=any(k in media_ids for k in t.get("attachments",{}).get("media_keys",[]))
    return d["data"]

def is_original(text, history):
    return all(
        difflib.SequenceMatcher(None,text.lower(),p.lower()).ratio()
        < PERSONAL_POST["similarity_limit"] for p in history
    )

# ─────────────────────────────────────────────────────────────────────────────
#  SYNC ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def sync_raids(db):
    global POM_X_ID
    summary={"tweets":0,"engagements":0,"organic":0,"errors":[]}
    if not X_BEARER_TOKEN:
        summary["errors"].append("X_BEARER_TOKEN not set"); return summary
    if not POM_X_ID:
        POM_X_ID,_=get_x_user(POM_X_HANDLE)
        if not POM_X_ID:
            summary["errors"].append(f"Cannot resolve @{POM_X_HANDLE}"); return summary

    xid_map={}
    for ud in db["users"].values():
        if ud.get("x_handle") and not ud.get("x_user_id"):
            xid,fl=get_x_user(ud["x_handle"])
            if xid: ud["x_user_id"]=xid; ud["x_followers"]=fl
        if ud.get("x_user_id"): xid_map[ud["x_user_id"]]=ud

    for tid, info in db["meta"].get("registered_raids",{}).items():
        summary["tweets"]+=1
        dropper_xid = info.get("tweet_author")
        for action,ids in [("like",get_likers(tid)),("repost",get_retweeters(tid))]:
            for xid in ids:
                ud=xid_map.get(xid)
                if not ud or xid==dropper_xid: continue
                cr=ud["x_data"]["credited_engagements"].setdefault(tid,[])
                if action not in cr:
                    add_x_pts(ud,X_POINTS[action]); cr.append(action); summary["engagements"]+=1
        reps,quotes=get_replies_quotes(tid)
        for action,ids in [("comment",reps),("quote",quotes)]:
            for xid in ids:
                ud=xid_map.get(xid)
                if not ud or xid==dropper_xid: continue
                cr=ud["x_data"]["credited_engagements"].setdefault(tid,[])
                if action not in cr:
                    add_x_pts(ud,X_POINTS[action]); cr.append(action); summary["engagements"]+=1

    now=datetime.utcnow()
    for tweet in search_organic_pom(since_hours=4):
        ud=xid_map.get(tweet.get("author_id"))
        if not ud: continue
        text=tweet.get("text",""); xd=ud["x_data"]
        if ud.get("x_followers",0)<PERSONAL_POST["min_followers"]: continue
        if PERSONAL_POST["require_image"] and not tweet.get("has_image"): continue
        if len(text.split())<PERSONAL_POST["min_words"]: continue
        lp=xd.get("last_post_drop")
        if lp and (now-datetime.fromisoformat(lp))<timedelta(hours=LINK_COOLDOWN_HOURS): continue
        if not is_original(text,xd.get("personal_post_history",[])): continue
        add_x_pts(ud,X_POINTS["post_drop"]); xd["last_post_drop"]=now.isoformat()
        h=xd.get("personal_post_history",[]); h.append(text)
        xd["personal_post_history"]=h[-PERSONAL_POST["history_count"]:]
        summary["organic"]+=1

    db["meta"]["last_x_sync"]=_now()
    return summary

# ─────────────────────────────────────────────────────────────────────────────
#  REWARD ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def get_pom_price():
    try:
        r=httpx.get(f"https://api.dexscreener.com/latest/dex/tokens/{POM_CA}",timeout=10)
        if r.status_code==200:
            pairs=r.json().get("pairs",[])
            if pairs: return float(pairs[0].get("priceUsd",0))
    except Exception as e:
        logger.error(f"Price fetch: {e}")
    return 0.0

def x_week_pts(u):
    """X-only weekly points — used for reward threshold."""
    return u["scores"].get("week", {}).get("x", 0)

def compute_rewards(db):
    # Reward threshold uses X points ONLY — TG points excluded
    total_x_pts = sum(x_week_pts(u) for u in db["users"].values())
    if not total_x_pts: return {}
    threshold = total_x_pts * QUALIFY_PCT
    # Qualify based on X points, rank by total (TG + X)
    qs = [(uid, ud, period_total(ud,"week"), x_week_pts(ud))
          for uid, ud in db["users"].items()
          if x_week_pts(ud) >= threshold]
    if not qs: return {}
    qs.sort(key=lambda x: x[2], reverse=True); qs = qs[:TOP_N]
    price   = get_pom_price()
    rollover = db["meta"].get("rollover_usd", 0.0)
    pool    = WEEKLY_POOL_USD + rollover
    results = {}
    for rank, (uid, ud, pts, x_pts) in enumerate(qs, 1):
        usd=TIER_AMOUNTS.get(rank,4)
        results[uid]={"rank":rank,"username":display_name(ud),"points":pts,
                      "usd_amount":usd,"token_amount":(usd/price) if price>0 else 0,
                      "wallet":ud.get("wallet"),"paid":False,"tx_hash":None}
    paid_out=sum(TIER_AMOUNTS.get(r+1,4) for r in range(len(qs)))
    db["meta"]["rollover_usd"]=max(0.0,pool-paid_out)
    return results

def send_pom(to_address, token_amount):
    if not PAY_WALLET_KEY: logger.error("PAY_WALLET_KEY not set"); return None
    try:
        w3=Web3(Web3.HTTPProvider(BSC_RPC))
        contract=w3.eth.contract(address=POM_CA,abi=ERC20_ABI)
        dec=contract.functions.decimals().call()
        amt=int(token_amount*(10**dec))
        acc=w3.eth.account.from_key(PAY_WALLET_KEY)
        tx=contract.functions.transfer(Web3.to_checksum_address(to_address),amt).build_transaction({
            "from":acc.address,"nonce":w3.eth.get_transaction_count(acc.address),
            "gas":200_000,"gasPrice":w3.eth.gas_price,"chainId":56,
        })
        signed=w3.eth.account.sign_transaction(tx,PAY_WALLET_KEY)
        h=w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt=w3.eth.wait_for_transaction_receipt(h,timeout=120)
        return h.hex() if receipt.status==1 else None
    except Exception as e:
        logger.error(f"Token send: {e}"); return None

# ─────────────────────────────────────────────────────────────────────────────
#  RATE LIMITER
# ─────────────────────────────────────────────────────────────────────────────

_msg_times: dict = defaultdict(list)

def is_flooding(uid):
    now=datetime.utcnow(); cutoff=now-timedelta(minutes=1)
    times=[t for t in _msg_times[uid] if t>cutoff]; times.append(now)
    _msg_times[uid]=times; return len(times)>SPAM["max_per_minute"]

# ─────────────────────────────────────────────────────────────────────────────
#  KEYBOARDS
# ─────────────────────────────────────────────────────────────────────────────

def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 My Score",      callback_data="score"),
         InlineKeyboardButton("📋 My Stats",      callback_data="stats")],
        [InlineKeyboardButton("🏆 Leaderboard",   callback_data="lb_alltime"),
         InlineKeyboardButton("❓ How to Earn",   callback_data="howto")],
        [InlineKeyboardButton("💰 Reward Status",  callback_data="rewardstatus"),
         InlineKeyboardButton("📊 Weekly Preview", callback_data="weeklypreview")],
        [InlineKeyboardButton("🐦 Link X Account", callback_data="linkx_prompt"),
         InlineKeyboardButton("🔗 Unlink X",        callback_data="unlinkx")],
        [InlineKeyboardButton("👛 Set Wallet",       callback_data="wallet_info"),
         InlineKeyboardButton("🗑️ Remove Wallet",   callback_data="unwallet")],
    ])

def kb_lb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏆 All-Time",callback_data="lb_alltime"),
         InlineKeyboardButton("🗓️ Month",  callback_data="lb_month")],
        [InlineKeyboardButton("📆 Week",   callback_data="lb_week"),
         InlineKeyboardButton("📅 Today",  callback_data="lb_day")],
        [InlineKeyboardButton("🔙 Back",   callback_data="back_main")],
    ])

def kb_score():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏆 Leaderboard",callback_data="lb_alltime"),
         InlineKeyboardButton("📋 Full Stats", callback_data="stats")],
        [InlineKeyboardButton("💰 Rewards",    callback_data="rewardstatus"),
         InlineKeyboardButton("🔙 Back",       callback_data="back_main")],
    ])

def kb_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Menu",callback_data="back_main")]])

def kb_confirm_unlink():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, unlink",callback_data="unlinkx_confirm"),
         InlineKeyboardButton("❌ Cancel",      callback_data="back_main")],
    ])


# ─────────────────────────────────────────────────────────────────────────────
#  RENDER FUNCTIONS  — all text uses MarkdownV2 with proper escaping
#  Rule: every special char in MarkdownV2 needs \\ in the Python string
#  Special chars: _ * [ ] ( ) ~ ` > # + - = | { } . !
# ─────────────────────────────────────────────────────────────────────────────

DIV  = "━━━━━━━━━━━━━━━━━━━━━━━━━━"
DIV2 = "─────────────────────────────"

def render_list(is_adm: bool = False, is_own: bool = False) -> str:
    lines = [
        "\U0001F4CB *Commands*",
        DIV,
        "",
        "\U0001F680 *General*",
        "/start \\- Open main menu",
        "/help \\- Get help \\& support",
        "/list \\- Show all commands",
        "/score \\- View your score card",
        "/stats \\- View your full profile",
        "/howto \\- How to earn points \\(quick\\)",
        "/rules \\- Full rules \\& point breakdown",
        "",
        DIV2,
        "\U0001F3C6 *Leaderboard*",
        "/lb \\- All\\-time leaderboard",
        "/lb week \\- This week's rankings",
        "/lb month \\- This month's rankings",
        "/lb day \\- Today's rankings",
        "",
        DIV2,
        "\U0001F426 *X Account*",
        "/linkx @handle \\- Link your X account",
        "/unlinkx \\- Unlink your X account",
        "/refreshx \\- Refresh X followers \\& data",
        "",
        DIV2,
        "\U0001F4B0 *Rewards*",
        "/wallet 0x\\.\\.\\. \\- Set your payout wallet",
        "/unwallet \\- Remove your payout wallet",
        "/rewardstatus \\- Check weekly eligibility",
        "/weeklypreview \\- See full weekly standings \\& top 10",
    ]
    if is_adm or is_own:
        lines += [
            "",
            DIV2,
            "\U0001F6E1\uFE0F *Admin Only*",
            "/ban \\- Ban a user \\(reply to message\\)",
            "/mute \\[mins\\] \\- Mute a user \\(reply\\)",
            "/warn \\- Warn a user \\(reply\\)",
            "/announce \\- Post \\& pin announcement",
        ]
    if is_own:
        lines += [
            "",
            DIV2,
            "\U0001F451 *Owner Only*",
            "/syncx \\- Sync X engagement scores",
            "/distribute \\- Run weekly payout",
            "/resetlb day\\|week\\|month\\|all \\- Reset board",
            "/resetoffenses \\- Clear user warnings",
        ]
    lines += ["", DIV]
    return "\n".join(lines)


def render_start(first_name: str) -> str:
    name = safe(first_name)
    return "\n".join([
        f"\U0001F436 *Welcome to PomRaid, {name}\\!*",
        DIV,
        "",
        "The official activity \\& raid tracker for the",
        "*\\$POM Army* on BNB Chain \U0001F680",
        "",
        "Earn points by staying active here and raiding on X\\.",
        "Top raiders get rewarded in *\\$POM tokens* every Sunday\\.",
        "",
        DIV2,
        "\U0001F680 *Quick Start:*",
        "1\\. Link your X account \u2192 /linkx",
        "2\\. Set payout wallet \u2192 /wallet",
        "3\\. How to earn \u2192 /howto",
        "4\\. Leaderboard \u2192 /lb",
        DIV,
        "_Stay loud\\. Stay based\\. POM Army never sleeps\\._ \U0001F525",
    ])


def render_help_member() -> str:
    return "\n".join([
        "\U0001F4D6 *PomRaid \u2014 Member Commands*",
        DIV,
        "",
        "\U0001F3E0 *Getting Started*",
        "/start \u2014 Show main menu",
        "/howto \u2014 How to earn points \\& rewards",
        "/rules \u2014 Full rules breakdown",
        "",
        DIV2,
        "\U0001F4CA *Your Stats*",
        "/score \u2014 View your points across all periods",
        "/stats \u2014 Full profile with X account \\& wallet",
        "/rewardstatus \u2014 Check reward eligibility",
        "/weeklypreview \u2014 See full weekly standings",
        "",
        DIV2,
        "\U0001F3C6 *Leaderboard*",
        "/lb \u2014 All\\-time top 50",
        "/lb week \u2014 This week's rankings",
        "/lb month \u2014 This month's rankings",
        "/lb day \u2014 Today's rankings",
        "",
        DIV2,
        "\U0001F426 *X Account*",
        "/linkx \u2014 Link your X account",
        "/unlinkx \u2014 Unlink your X account",
        "/refreshx \u2014 Refresh X follower count",
        "",
        DIV2,
        "\U0001F4B0 *Rewards*",
        "/wallet \u2014 Set your BNB wallet for \\$POM payouts",
        "/unwallet \u2014 Remove your wallet",
        "/rewardstatus \u2014 See your weekly reward eligibility",
        "",
        DIV,
        "_Tap any command above to use it directly_",
    ])


def render_help_admin() -> str:
    return "\n".join([
        "\U0001F6E1\uFE0F *PomRaid \u2014 Admin Commands*",
        DIV,
        "",
        "\U0001F46E *Moderation* \\(reply to a user's message\\)",
        "/ban \u2014 Permanently ban a user",
        "/mute \u2014 Mute a user \\(default: 10 min\\)",
        "/mute 30 \u2014 Mute for custom duration",
        "/warn \u2014 Issue a warning \\(3 warnings \\= auto\\-ban\\)",
        "",
        DIV2,
        "\U0001F4E2 *Announcements*",
        "/announce \u2014 Post and pin a message to the group",
        "",
        DIV,
        "\U0001F451 *Owner\\-Only Commands*",
        "/syncx \u2014 Sync all X engagement scores",
        "/distribute \u2014 Run weekly reward distribution",
        "/resetlb \u2014 Reset leaderboard period",
        "/resetoffenses \u2014 Clear a user's warnings",
        "",
        DIV,
        "_All moderation commands require replying to the target user's message_",
    ])


def render_howto() -> str:
    return "\n".join([
        "\u2753 *How to Earn PomRaid Points*",
        DIV,
        "",
        "\U0001F4E3 *RAID @Pom\\_bsc POSTS*",
        "Bot alerts the group when POM posts on X\\.",
        "Go raid it and earn points:",
        "",
        f"  Like \\.\\.\\.\\.\\.\\.\\.\\.\\.\\.\\.\\. \\+{X_POINTS['like']} pts",
        f"  Repost \\.\\.\\.\\.\\.\\.\\.\\.\\. \\+{X_POINTS['repost']} pts",
        f"  Comment \\.\\.\\.\\.\\.\\.\\. \\+{X_POINTS['comment']} pts \\(1 per post\\)",
        f"  Quote Tweet \\.\\.\\. \\+{X_POINTS['quote']} pts",
        "",
        DIV2,
        "\U0001F517 *DROP YOUR OWN POST*",
        "Paste your X link in the group\\.",
        "Must contain \\$POM or @Pom\\_bsc\\.",
        "",
        f"  \u2705 Verified as yours \u2192 \\+{X_POINTS['post_drop']} pts",
        "  \u2705 12hr cooldown starts",
        "  \u2705 Others raid it \\& earn points too",
        "",
        DIV2,
        "\U0001F4AC *TELEGRAM ACTIVITY*",
        f"Messages of {TG_MIN_WORDS}\\+ words earn TG points\\.",
        f"Daily cap: *{TG_DAILY_CAP} pts max*",
        "_TG points do not count toward reward threshold_",
        "",
        DIV2,
        "\U0001F4B0 *WEEKLY REWARDS*",
        f"Every Sunday \u2014 *\\${WEEKLY_POOL_USD:.0f}* pool in *\\$POM*\\.",
        f"Top {TOP_N} raiders with 70\\%\\+ of weekly X points\\.",
        "",
        "  \U0001F449 /wallet \u2014 set your BNB address",
        "  \U0001F449 /rewardstatus \u2014 check your standing",
        "  \U0001F449 /rules \u2014 full breakdown",
        DIV,
    ])


def render_rules() -> str:
    return "\n".join([
        "\U0001F4DC *PomRaid \u2014 Full Rules \\& Points*",
        DIV,
        "",
        "\U0001F4E3 *RAIDING @Pom\\_bsc OFFICIAL POSTS*",
        "Bot auto\\-detects new posts \\& alerts the group\\.",
        "Go engage on X:",
        "",
        "```",
        "Action         Points    Limit",
        "────────────────────────────────",
        f"Like             +{X_POINTS['like']}      Once per post",
        f"Repost           +{X_POINTS['repost']}     Once per post",
        f"Comment          +{X_POINTS['comment']}     Once per post",
        f"Quote Tweet      +{X_POINTS['quote']}     Once per post",
        "```",
        "",
        DIV2,
        "\U0001F517 *DROPPING YOUR OWN POST*",
        "Paste your X link in the group\\.",
        "",
        "Requirements:",
        "  \u2705 Must mention \\$POM or @Pom\\_bsc",
        "  \u2705 Verified as YOUR linked X account",
        f"  \u2705 Min {PERSONAL_POST['min_words']} words",
        "  \u2705 Must include image or video",
        f"  \u2705 Min {PERSONAL_POST['min_followers']} followers on X",
        "  \u2705 Original content \\(no copy\\-paste\\)",
        "  \u2705 12hr cooldown between drops",
        "",
        f"Reward: \\+{X_POINTS['post_drop']} pts to you | Others raid it \u2192 they earn points",
        "",
        DIV2,
        "\U0001F310 *COMMUNITY RAID LINKS*",
        "Anyone can drop a POM\\-related X link\\.",
        "If it's NOT your post:",
        "  \u2022 0 pts to dropper",
        "  \u2022 Everyone who raids earns points",
        "  \u2022 No cooldown affected",
        "",
        DIV2,
        "\U0001F4AC *TELEGRAM ACTIVITY*",
        "",
        "```",
        f"Words per message    Points",
        f"──────────────────────────",
        f"Under {TG_MIN_WORDS} words       0 pts",
        f"{TG_MIN_WORDS}-14 words          1 pt",
        f"15-29 words          2 pts",
        f"30+ words            3 pts",
        f"Daily cap            {TG_DAILY_CAP} pts",
        "```",
        "",
        "\u26A0\uFE0F TG points do NOT count toward",
        "the 70\\% reward qualification threshold\\.",
        "Only X engagement points qualify you\\.",
        "",
        DIV2,
        "\U0001F4B0 *WEEKLY REWARDS \\(Every Sunday\\)*",
        f"Pool: *\\${WEEKLY_POOL_USD:.0f}/week* in *\\$POM tokens*",
        "",
        "Qualification:",
        "  \u2022 Score 70\\%\\+ of total weekly X points",
        "  \u2022 Only X points count toward this",
        f"  \u2022 Top {TOP_N} qualifiers get paid",
        "",
        "```",
        "Payout Tiers",
        "────────────────────",
        "1st place       $18",
        "2nd place       $13",
        "3rd place       $10",
        "4th-6th place    $6 each",
        "7th-10th place   $4 each",
        "────────────────────",
        "Total           $75",
        "```",
        "",
        "Unclaimed rewards roll to next week\\.",
        "",
        DIV2,
        "\U0001F4CB *SETUP CHECKLIST*",
        "  \u25A1 /linkx \u2014 connect your X account",
        "  \u25A1 /wallet \u2014 set your BNB wallet",
        "  \u25A1 /rewardstatus \u2014 check your standing",
        DIV,
    ])


def render_score(d: dict) -> str:
    s    = d["scores"]
    xd   = d["x_data"]
    name = safe(display_name(d))

    if d["x_handle"]:
        x_line = f"\U0001F426 *@{safe(d['x_handle'])}*  \u2022  {d.get('x_followers',0):,} followers"
    else:
        x_line = "\U0001F426 X Account: _not linked_ \u2014 tap Link X Account"

    last_drop = xd.get("last_post_drop")
    if last_drop:
        elapsed   = datetime.utcnow() - datetime.fromisoformat(last_drop)
        remaining = timedelta(hours=LINK_COOLDOWN_HOURS) - elapsed
        if remaining.total_seconds() > 0:
            h = int(remaining.total_seconds() // 3600)
            m = int((remaining.total_seconds() % 3600) // 60)
            cd_line = f"\u23F3 Post cooldown: *{h}h {m}m* remaining"
        else:
            cd_line = "\u2705 Post cooldown: *Ready to drop\\!*"
    else:
        cd_line = "\u2705 Post cooldown: *Ready to drop\\!*"

    rows = []
    for key, icon, label in [
        ("day",     "\U0001F4C5", "Today     "),
        ("week",    "\U0001F4C6", "This Week "),
        ("month",   "\U0001F5D3", "This Month"),
        ("alltime", "\U0001F3C6", "All-Time  "),
    ]:
        b     = s[key]
        total = b["tg"] + b["x"]
        rows.append(f"{icon} {label}  {b['tg']:>5}  {b['x']:>5}  {total:>7}")

    table = "```\nPeriod           TG      X    Total\n─────────────────────────────────────\n" + "\n".join(rows) + "\n```"

    return "\n".join([
        f"\U0001F4CA *Score Card \u2014 @{name}*",
        DIV,
        "",
        x_line,
        cd_line,
        "",
        table,
        DIV,
        "_Use /stats for full profile_",
    ])


def render_stats(d: dict, first_name: str) -> str:
    s     = d["scores"]
    xd    = d["x_data"]
    total = s["alltime"]["tg"] + s["alltime"]["x"]
    wk    = s["week"]["tg"] + s["week"]["x"]

    if d["x_handle"]:
        x_info = f"@{safe(d['x_handle'])}  \u2022  {d.get('x_followers',0):,} followers"
    else:
        x_info = "_not linked_"

    eng  = sum(len(v) for v in xd.get("credited_engagements", {}).values())
    wall = f"`{safe(d['wallet'][:20])}\\.\\.\\.[set]`" if d.get("wallet") else "_not set \u2014 use /wallet_"

    return "\n".join([
        f"\U0001F4CB *Full Profile \u2014 {safe(first_name)}*",
        DIV,
        "",
        f"\U0001F464 Handle: @{safe(display_name(d))}",
        f"\U0001F426 X Account: {x_info}",
        f"\U0001F4B3 Wallet: {wall}",
        f"\U0001F4C5 Member since: {safe(d['joined'][:10])}",
        "",
        DIV2,
        "\U0001F3C6 *Points Summary*",
        f"  All\\-time total: *{total:,} pts*",
        f"  This week: *{wk:,} pts*",
        f"  TG \\(all\\-time\\): {s['alltime']['tg']:,} pts",
        f"  X \\(all\\-time\\): {s['alltime']['x']:,} pts",
        "",
        DIV2,
        "\U0001F4E3 *Raid Activity*",
        f"  Engagements credited: *{eng}*",
        f"  Personal posts: *{len(xd.get('personal_post_history', []))}*",
        "",
        DIV2,
        f"\u26A0\uFE0F Warnings: *{d['offenses']}/3*",
        DIV,
        "_Use /rewardstatus to check reward eligibility_",
    ])


def render_reward_status(db: dict, uid: str) -> str:
    ud       = db["users"].get(uid, {})
    wk_pts   = period_total(ud, "week")
    wk_x_pts = x_week_pts(ud)
    total_x  = sum(x_week_pts(u) for u in db["users"].values())
    threshold = total_x * QUALIFY_PCT if total_x > 0 else 0
    pct       = (wk_x_pts / total_x * 100) if total_x > 0 else 0
    qualifies = wk_x_pts >= threshold and threshold > 0

    ranked = sorted(db["users"].values(), key=lambda u: period_total(u, "week"), reverse=True)
    rank   = next((i+1 for i, u in enumerate(ranked) if u is ud), "?")

    if qualifies:
        est_usd     = TIER_AMOUNTS.get(rank if isinstance(rank, int) else 11, 4)
        status_box  = "\u2705 *QUALIFYING THIS WEEK\\!*"
        reward_line = [
            f"  Estimated reward: *\\${est_usd}* in \\$POM",
            f"  Current rank: *\\#{rank}*",
        ]
    else:
        needed     = max(0, int(threshold) - wk_x_pts)
        status_box = "\u274C *Not qualifying yet*"
        reward_line = [f"  X points needed: *{needed:,} more*"]

    rollover = db["meta"].get("rollover_usd", 0.0)
    pool     = WEEKLY_POOL_USD + rollover

    if ud.get("wallet"):
        wall_line = f"  \u2705 `{safe(ud['wallet'][:10])}\\.\\.\\.[set]`"
    else:
        wall_line = "  \u26A0\uFE0F _Not set\\!_ Use /wallet before Sunday"

    rollover_line = [f"  Rollover: *\\${rollover:.2f}*"] if rollover > 0 else []

    return "\n".join([
        "\U0001F4B0 *Weekly Reward Status*",
        DIV,
        "",
        status_box,
        "",
        DIV2,
        "\U0001F4CA *Your Position*",
        f"  This week total: *{wk_pts:,} pts*",
        f"  X points \\(counts for rewards\\): *{wk_x_pts:,}*",
        f"  Community X total: *{total_x:,}*",
        f"  Qualify threshold \\(70%\\): *{int(threshold):,}*",
        f"  Your share of X pool: *{pct:.1f}%*",
    ] + reward_line + [
        "",
        DIV2,
        "\U0001F4B5 *This Week's Pool*",
        f"  Base pool: *\\${WEEKLY_POOL_USD:.0f}*",
    ] + rollover_line + [
        f"  Total: *\\${pool:.2f}*",
        f"  Top {TOP_N} qualifiers share this pool",
        "",
        DIV2,
        "\U0001F4B3 *Payout Wallet*",
        wall_line,
        DIV,
        "_Rewards distributed every Sunday automatically_",
    ])


def render_leaderboard(db: dict, period: str) -> str:
    icon, label = PERIOD_META[period]
    ranked = sorted(db["users"].items(), key=lambda kv: period_total(kv[1], period), reverse=True)
    ranked = [(uid, u) for uid, u in ranked if period_total(u, period) > 0][:50]

    if not ranked:
        return "\n".join([
            f"{icon} *{safe(label)} Leaderboard*",
            DIV,
            "",
            "_No activity recorded yet for this period\\._",
            "",
            "Be the first to raid and claim the top spot\\! \U0001F525",
        ])

    total_pts = sum(period_total(u, period) for _, u in ranked)
    lines = [
        f"{icon} *{safe(label)} \u2014 POM Army Leaderboard*",
        DIV,
        f"\U0001F465 Active raiders: *{len(ranked)}* | \U0001F3AF Total pts: *{total_pts:,}*",
        DIV2,
        "",
    ]
    for i, (_, u) in enumerate(ranked):
        name  = safe(display_name(u))
        b     = u["scores"].get(period, _blank_bucket())
        total = b["tg"] + b["x"]
        x_tag = f" \U0001F426{b['x']}" if u.get("x_handle") and b["x"] > 0 else ""
        pct   = f"{total/total_pts*100:.0f}%" if total_pts > 0 else "0%"
        lines.append(f"{MEDALS[i]} `{name}` \u2014 *{total:,} pts* \\({pct}\\)")
        lines.append(f"    TG: {b['tg']} | X: {b['x']}{x_tag}")
    lines += ["", "", DIV, "_Updated on /syncx | Resets every Sunday_"]
    return "\n".join(lines)


def render_wallet_info(d: dict) -> str:
    if d.get("wallet"):
        wall   = f"`{safe(d['wallet'])}`"
        status = "\u2705 Wallet is set\\. You're eligible for automatic payouts\\."
    else:
        wall   = "_Not set_"
        status = "\u26A0\uFE0F No wallet set\\. You won't receive rewards until you add one\\."

    return "\n".join([
        "\U0001F4B3 *Your Payout Wallet*",
        DIV,
        "",
        f"BNB Chain Address:\n{wall}",
        "",
        status,
        "",
        DIV2,
        "To set or update your wallet:",
        "`/wallet 0xYourBNBWalletAddress`",
        "",
        DIV2,
        "\u26A0\uFE0F *Important:*",
        "\u2022 Must be a BNB Chain \\(BSC\\) address",
        "\u2022 Must start with `0x`",
        "\u2022 Double\\-check your address \u2014 payments are irreversible",
        "\u2022 You can update anytime before Sunday payout",
        "\u2022 Use /unwallet to remove it",
        DIV,
        "_Rewards are paid automatically every Sunday in \\$POM_",
    ])


# ─────────────────────────────────────────────────────────────────────────────
#  KEYBOARDS
# ─────────────────────────────────────────────────────────────────────────────

def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 My Score",       callback_data="score"),
         InlineKeyboardButton("📋 My Stats",       callback_data="stats")],
        [InlineKeyboardButton("🏆 Leaderboard",    callback_data="lb_alltime"),
         InlineKeyboardButton("❓ How to Earn",    callback_data="howto")],
        [InlineKeyboardButton("💰 Reward Status",  callback_data="rewardstatus"),
         InlineKeyboardButton("📊 Weekly Preview", callback_data="weeklypreview")],
        [InlineKeyboardButton("🐦 Link X Account", callback_data="linkx_prompt"),
         InlineKeyboardButton("🔗 Unlink X",       callback_data="unlinkx")],
        [InlineKeyboardButton("👛 Set Wallet",      callback_data="wallet_info"),
         InlineKeyboardButton("🗑️ Remove Wallet",  callback_data="unwallet")],
    ])

def kb_lb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏆 All-Time", callback_data="lb_alltime"),
         InlineKeyboardButton("🗓️ Month",   callback_data="lb_month")],
        [InlineKeyboardButton("📆 Week",    callback_data="lb_week"),
         InlineKeyboardButton("📅 Today",   callback_data="lb_day")],
        [InlineKeyboardButton("🔙 Back",    callback_data="back_main")],
    ])

def kb_score():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏆 Leaderboard", callback_data="lb_alltime"),
         InlineKeyboardButton("📋 Full Stats",  callback_data="stats")],
        [InlineKeyboardButton("💰 Rewards",     callback_data="rewardstatus"),
         InlineKeyboardButton("🔙 Back",        callback_data="back_main")],
    ])

def kb_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_main")]])

def kb_confirm_unlink():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, unlink", callback_data="unlinkx_confirm"),
         InlineKeyboardButton("❌ Cancel",       callback_data="back_main")],
    ])

def kb_confirm_unwallet():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, remove", callback_data="unwallet_confirm"),
         InlineKeyboardButton("❌ Cancel",       callback_data="back_main")],
    ])


# ─────────────────────────────────────────────────────────────────────────────
#  USER COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u = update.effective_user
    await get_user_async(u.id, u.username or "", u.first_name)
    await save_user_async(d)
    await update.message.reply_text(
        render_start(u.first_name),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb_main(),
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u = update.effective_user
    if is_owner(u.id) or await is_group_admin(update, ctx):
        text = render_help_admin()
    else:
        text = render_help_member()
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_main())

async def cmd_howto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    await update.message.reply_text(render_howto(), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_back())

async def cmd_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    await update.message.reply_text(render_rules(), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_back())

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u = update.effective_user
    own = is_owner(u.id)
    adm = own or await is_group_admin(update, ctx)
    await update.message.reply_text(
        render_list(is_adm=adm, is_own=own),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=kb_back(),
    )

async def cmd_score(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u = update.effective_user
    user = update.effective_user
    user_data = await get_user_async(user.id, user.username or "", user.first_name or "")
    d = get_user(db, u.id, u.username or "", u.first_name)
    await update.message.reply_text(render_score(d), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_score())

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u = update.effective_user
    db = load_db()
    d = get_user(db, u.id, u.username or "", u.first_name)
    await update.message.reply_text(render_stats(d, u.first_name), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_back())

async def cmd_rewardstatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u = update.effective_user
    await get_user_async(u.id, u.username or "", u.first_name)
    await update.message.reply_text(
        render_reward_status(db, str(u.id)),
        parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_back(),
    )

async def cmd_weeklypreview(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u = update.effective_user
    own = is_owner(u.id)
    db = load_db()
    uid_str = str(u.id)

    total_x   = sum(x_week_pts(ud) for ud in db["users"].values())
    threshold = total_x * QUALIFY_PCT if total_x > 0 else 0

    all_u = [(uid2, ud2, period_total(ud2, "week"), x_week_pts(ud2))
             for uid2, ud2 in db["users"].items() if period_total(ud2, "week") > 0]
    all_u.sort(key=lambda x: x[2], reverse=True)

    qualifying = [(uid2, ud2, tot, xp) for uid2, ud2, tot, xp in all_u if xp >= threshold]
    not_yet    = [(uid2, ud2, tot, xp) for uid2, ud2, tot, xp in all_u if xp < threshold]

    c_rank     = next((i+1 for i, (uid2,_,_,_) in enumerate(all_u) if uid2 == uid_str), None)
    caller_d   = db["users"].get(uid_str, {})
    caller_xp  = x_week_pts(caller_d)
    qualifies  = caller_xp >= threshold and threshold > 0

    if c_rank and qualifies:
        est = TIER_AMOUNTS.get(c_rank, 4)
        your_line = f"\u2705 *You: Rank \\#{c_rank} \u2014 Qualifying \u2014 Est\\. \\${est}*"
    elif c_rank:
        needed = max(0, int(threshold) - caller_xp)
        your_line = f"\u23F3 *You: Rank \\#{c_rank} \u2014 Need {needed:,} more X pts*"
    else:
        your_line = "_No activity yet this week_"

    lines = [
        "\U0001F4CA *Weekly Standings Preview*",
        DIV,
        f"\U0001F310 Community X points: *{total_x:,}*",
        f"\U0001F4CF Qualify threshold \\(70%\\): *{int(threshold):,}*",
        f"\U0001F4C5 Resets: *Sunday midnight UTC*",
        DIV2,
        your_line,
        DIV2,
        f"\U0001F3C6 *Top {min(len(qualifying), TOP_N)} Qualifying*" if qualifying else "_No qualifiers yet_",
    ]

    for i, (uid2, ud2, tot, xp) in enumerate(qualifying[:TOP_N], 1):
        name = safe(display_name(ud2))
        est  = TIER_AMOUNTS.get(i, 4)
        wall = " \U0001F4B3" if ud2.get("wallet") else " \u26A0\uFE0F" if own else ""
        lines.append(f"{MEDALS[i-1]} @{name} \u2014 *{xp:,} X pts* \u2014 Est\\. *\\${est}*{wall}")

    if not_yet:
        lines += ["", DIV2, "\u274C *Not Qualifying Yet*"]
        for uid2, ud2, tot, xp in not_yet[:8]:
            needed = max(0, int(threshold) - xp)
            lines.append(f"  \u2022 @{safe(display_name(ud2))} \u2014 {xp:,} pts \\(need {needed:,} more\\)")
        if len(not_yet) > 8:
            lines.append(f"  _\\.\\.\\. and {len(not_yet)-8} others_")

    if own and qualifying:
        no_wall = [safe(display_name(ud2)) for _, ud2, _, xp in qualifying[:TOP_N] if not ud2.get("wallet")]
        if no_wall:
            lines += ["", DIV2, "\u26A0\uFE0F *Missing Wallets \\(won't receive payout\\):*"]
            for name in no_wall:
                lines.append(f"  \u2022 @{name}")
            lines.append("_They need to run /wallet before Sunday_")

    lines += [
        "",
        DIV,
        "_Run /distribute on Sunday to pay everyone automatically_" if own else "_Keep raiding\\! Payouts every Sunday\\._",
    ]

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3950] + "\n_\\.\\.\\.use /rewardstatus for your personal status_"

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_back())

async def cmd_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u = update.effective_user
    if not ctx.args:
        await update.message.reply_text(
            "\U0001F4B3 *Set Your Payout Wallet*\n\n"
            "Usage: `/wallet 0xYourBNBWalletAddress`\n\n"
            "_Example:_\n`/wallet 0xAbCd1234\\.\\.\\.`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    address = ctx.args[0].strip()
    if not re.match(r"^0x[0-9a-fA-F]{40}$", address):
        await update.message.reply_text(
            "\u274C *Invalid Address*\n\n"
            "Must be a valid BNB Chain address starting with `0x`\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    d = await get_user_async(u.id, u.username or "", u.first_name)
    d["wallet"] = address
    await save_user_async(d)
    await update.message.reply_text(
        "\u2705 *Wallet Saved\\!*\n\n"
        f"Address: `{safe(address)}`\n\n"
        "You're all set to receive *\\$POM* rewards every Sunday\\.",
        parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_back(),
    )

async def cmd_unwallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u  = update.effective_user
    d = await get_user_async(u.id, u.username or "", u.first_name)
    if not d.get("wallet"):
        await update.message.reply_text(
            "\u2139\uFE0F You don't have a wallet set\\.\n\nUse /wallet to add one\\.",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_back(),
        )
        return
    await update.message.reply_text(
        "\U0001F5D1\uFE0F *Remove Wallet?*\n\n"
        f"Address: `{safe(d['wallet'][:20])}\\.\\.\\.[wallet]`\n\n"
        "You won't receive rewards without a wallet\\.\n"
        "_This cannot be undone\\._",
        parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_confirm_unwallet(),
    )

async def cmd_leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    raw    = ctx.args[0].lower() if ctx.args else "alltime"
    period = {"day": "day", "week": "week", "month": "month"}.get(raw, "alltime")
    db     = load_db()
    text   = render_leaderboard(db, period)
    if len(text) > 4000:
        text = text[:3950] + "\n_\\.\\.\\.use /lb for more_"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_lb())

async def cmd_linkx(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u = update.effective_user
    if not ctx.args:
        await update.message.reply_text(
            "\U0001F426 *Link Your X Account*\n\n"
            "Usage: `/linkx @yourhandle`\n\n"
            "_Example: /linkx @Glayzz\\_4T9ne\\_BK_\n\n"
            "Your X account is used to track raid engagement and award points\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    handle = ctx.args[0].lstrip("@").strip()
    msg = await update.message.reply_text(
        f"\U0001F504 Verifying *@{safe(handle)}* on X\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    if X_BEARER_TOKEN:
        xid, followers = get_x_user(handle)
        if not xid:
            await msg.edit_text(
                f"\u274C *Account Not Found*\n\nCould not find *@{safe(handle)}* on X\\.\nCheck the handle and try again\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        if followers < PERSONAL_POST["min_followers"]:
            await msg.edit_text(
                f"\u274C *Insufficient Followers*\n\n@{safe(handle)} has *{followers}* followers\\.\n"
                f"Minimum required: *{PERSONAL_POST['min_followers']}*\n\n"
                "Keep building your X presence and try again\\!",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
    else:
        xid, followers = None, 0
    d = await get_user_async(u.id, u.username or "", u.first_name)
    d["x_handle"]    = handle
    d["x_user_id"]   = xid
    d["x_followers"] = followers
    d["tg_uid"]      = u.id
    await save_user_async(d)
    await msg.edit_text(
        f"\u2705 *X Account Linked\\!*\n\n"
        f"\U0001F426 @{safe(handle)}\n"
        f"\U0001F465 Followers: *{followers:,}*\n\n"
        "You'll now earn points for:\n"
        "\u2022 Engaging with *@Pom\\_bsc* posts\n"
        "\u2022 Dropping your X posts in the group\n\n"
        "_Points are synced by admins using /syncx_",
        parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_back(),
    )

async def cmd_unlinkx(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u  = update.effective_user
    d = await get_user_async(u.id, u.username or "", u.first_name)
    if not d.get("x_handle"):
        await update.message.reply_text(
            "\u2139\uFE0F You don't have an X account linked\\.\n\nUse /linkx to connect one\\.",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_back(),
        )
        return
    await update.message.reply_text(
        f"\u26A0\uFE0F *Confirm Unlink*\n\n"
        f"Are you sure you want to unlink *@{safe(d['x_handle'])}*?\n\n"
        "This will:\n"
        "\u2022 Remove your X account from PomRaid\n"
        "\u2022 Reset your X score to *0*\n"
        "\u2022 Clear your raid engagement history\n\n"
        "_This action cannot be undone\\._",
        parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_confirm_unlink(),
    )

async def cmd_refreshx(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_group(update): return
    u  = update.effective_user
    d = await get_user_async(u.id, u.username or "", u.first_name)
    if not d.get("x_handle"):
        await update.message.reply_text(
            "\u2139\uFE0F You don't have an X account linked\\.\n\nUse /linkx to connect one\\.",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_back(),
        )
        return
    handle = d["x_handle"]
    msg = await update.message.reply_text(
        f"\U0001F504 Refreshing *@{safe(handle)}* data\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    if not X_BEARER_TOKEN:
        await msg.edit_text(
            "\u274C X\\_BEARER\\_TOKEN not set\\. Cannot fetch live data\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    xid, followers = get_x_user(handle)
    if not xid:
        await msg.edit_text(
            f"\u274C Could not reach X API for *@{safe(handle)}*\\.\nTry again later\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    d["x_user_id"]   = xid
    d["x_followers"] = followers
    await save_user_async(d)
    await msg.edit_text(
        f"\u2705 *X Data Refreshed\\!*\n\n"
        f"\U0001F426 @{safe(handle)}\n"
        f"\U0001F465 Followers: *{followers:,}*\n\n"
        "Your X account is up to date\\.",
        parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_back(),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  CALLBACK HANDLER
# ─────────────────────────────────────────────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    u    = q.from_user
    await q.answer()
    d = await get_user_async(u.id, u.username or "", u.first_name)

    if data == "back_main":
        await q.edit_message_text(render_start(u.first_name), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_main())
    elif data == "score":
        await q.edit_message_text(render_score(d), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_score())
    elif data == "stats":
        await q.edit_message_text(render_stats(d, u.first_name), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_back())
    elif data == "howto":
        await q.edit_message_text(render_howto(), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_back())
    elif data == "rewardstatus":
        await q.edit_message_text(render_reward_status(db, str(u.id)), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_back())
    elif data == "weeklypreview":
        total_x   = sum(x_week_pts(ud) for ud in db["users"].values())
        threshold = total_x * QUALIFY_PCT if total_x > 0 else 0
        all_u = [(uid2, ud2, period_total(ud2,"week"), x_week_pts(ud2))
                 for uid2, ud2 in db["users"].items() if period_total(ud2,"week") > 0]
        all_u.sort(key=lambda x: x[2], reverse=True)
        qualifying = [(uid2,ud2,tot,xp) for uid2,ud2,tot,xp in all_u if xp >= threshold]
        c_rank = next((i+1 for i,(uid2,_,_,_) in enumerate(all_u) if uid2==str(u.id)), None)
        caller_xp = x_week_pts(d)
        qualifies = caller_xp >= threshold and threshold > 0
        if c_rank and qualifies:
            your_line = f"\u2705 *You: Rank \\#{c_rank} \u2014 Est\\. \\${TIER_AMOUNTS.get(c_rank,4)}*"
        elif c_rank:
            your_line = f"\u23F3 *You: Rank \\#{c_rank} \u2014 Need {max(0,int(threshold)-caller_xp):,} more X pts*"
        else:
            your_line = "_No activity yet_"
        out = [
            "\U0001F4CA *Weekly Standings Preview*", DIV,
            f"\U0001F310 Community X pts: *{total_x:,}*",
            f"\U0001F4CF Threshold \\(70%\\): *{int(threshold):,}*",
            DIV2, your_line, DIV2,
            f"\U0001F3C6 *Top {min(len(qualifying),TOP_N)} Qualifying*" if qualifying else "_No qualifiers yet_",
        ]
        for i,(uid2,ud2,tot,xp) in enumerate(qualifying[:TOP_N],1):
            out.append(f"{MEDALS[i-1]} @{safe(display_name(ud2))} \u2014 *{xp:,} X pts* \u2014 Est\\. *\\${TIER_AMOUNTS.get(i,4)}*")
        out += [DIV, "_Payouts every Sunday_"]
        preview = "\n".join(out)
        if len(preview) > 4000:
            preview = preview[:3950] + "\n_\\.\\.\\.use /weeklypreview_"
        await q.edit_message_text(preview, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_back())
    elif data == "wallet_info":
        await q.edit_message_text(render_wallet_info(d), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_back())
    elif data == "unwallet":
        if not d.get("wallet"):
            await q.edit_message_text(
                "\u2139\uFE0F You don't have a wallet set\\.\n\nUse /wallet to add one\\.",
                parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_back(),
            )
        else:
            await q.edit_message_text(
                "\U0001F5D1\uFE0F *Remove Wallet?*\n\n"
                f"Address: `{safe(d['wallet'][:20])}\\.\\.\\.`\n\n"
                "You won't receive rewards without a wallet\\.\n"
                "_This cannot be undone\\._",
                parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_confirm_unwallet(),
            )
    elif data == "unwallet_confirm":
        old_w = d.get("wallet", "")
        d["wallet"] = None
        await save_user_async(d)
        await q.edit_message_text(
            "\u2705 *Wallet Removed*\n\n"
            f"`{safe(old_w[:20])}\\.\\.\\.[removed]`\n\n"
            "Use /wallet anytime to set a new address\\.",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_main(),
        )
    elif data.startswith("lb_"):
        period = data[3:]
        text   = render_leaderboard(db, period)
        if len(text) > 4000:
            text = text[:3950] + "\n_\\.\\.\\.use /lb for full list_"
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_lb())
    elif data == "linkx_prompt":
        cur = f"Currently linked: *@{safe(d['x_handle'])}*\n\n" if d.get("x_handle") else ""
        await q.edit_message_text(
            f"\U0001F426 *Link Your X Account*\n\n{cur}"
            "Send the command:\n`/linkx @YourXHandle`\n\n"
            "_Example: /linkx @Glayzz\\_4T9ne\\_BK_",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_back(),
        )
    elif data == "unlinkx":
        if not d.get("x_handle"):
            await q.edit_message_text(
                "\u2139\uFE0F No X account linked\\.\n\nUse /linkx to connect one\\.",
                parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_back(),
            )
        else:
            await q.edit_message_text(
                f"\u26A0\uFE0F *Confirm Unlink*\n\n"
                f"Unlink *@{safe(d['x_handle'])}*?\n\n"
                "Your X score will be reset to *0* and all raid history cleared\\.\n"
                "_This cannot be undone\\._",
                parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_confirm_unlink(),
            )
    elif data == "unlinkx_confirm":
        old = d.get("x_handle", "")
        d["x_handle"] = None
        d["x_user_id"] = None
        d["x_followers"] = 0
        d["x_data"] = _blank_x_data()
        for b in d["scores"].values():
            b["x"] = 0
        await save_user_async(d)
        await q.edit_message_text(
            f"\u2705 *Unlinked Successfully*\n\n"
            f"@{safe(old)} has been removed from your profile\\.\n"
            "Your X score has been reset to 0\\.\n\n"
            "Use /linkx anytime to connect a new account\\.",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb_main(),
        )


# ─────────────────────────────────────────────────────────────────────────────
#  ADMIN COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update, ctx): return
    if not update.message.reply_to_message:
        await update.message.reply_text("\u21A9\uFE0F Reply to the user's message to ban them\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    target = update.message.reply_to_message.from_user
    await ctx.bot.ban_chat_member(update.effective_chat.id, target.id)
    await update.message.reply_text(
        f"\U0001F6AB *{safe(target.first_name)}* has been banned from the POM Army\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

async def cmd_mute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update, ctx): return
    if not update.message.reply_to_message:
        await update.message.reply_text("\u21A9\uFE0F Reply to the user's message to mute them\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    target  = update.message.reply_to_message.from_user
    minutes = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 10
    until   = datetime.utcnow() + timedelta(minutes=minutes)
    await ctx.bot.restrict_chat_member(
        update.effective_chat.id, target.id,
        permissions=ChatPermissions(can_send_messages=False), until_date=until,
    )
    await update.message.reply_text(
        f"\U0001F507 *{safe(target.first_name)}* muted for *{minutes} minutes*\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

async def cmd_warn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update, ctx): return
    if not update.message.reply_to_message:
        await update.message.reply_text("\u21A9\uFE0F Reply to the user's message to warn them\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    target = update.message.reply_to_message.from_user
    db     = load_db()
    d      = get_user(db, target.id, target.username or "", target.first_name)
    d["offenses"] += 1
    offenses = d["offenses"]
    await save_user_async(d)
    if offenses >= 3 and SPAM["ban_on_third_offense"]:
        await ctx.bot.ban_chat_member(update.effective_chat.id, target.id)
        await update.message.reply_text(
            f"\U0001F6AB *{safe(target.first_name)}* hit 3 warnings \u2014 permanently banned\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        bar = "\U0001F7E5" * offenses + "\u2B1C" * (3 - offenses)
        almost = " \u2014 *One more \\= ban\\.*" if offenses == 2 else ""
        await update.message.reply_text(
            f"\u26A0\uFE0F *Warning Issued \u2014 {safe(target.first_name)}*\n\n"
            f"Warnings: {bar} *{offenses}/3*{almost}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

async def cmd_announce(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_admin(update, ctx): return
    if not ctx.args:
        await update.message.reply_text("Usage: `/announce Your message here`", parse_mode=ParseMode.MARKDOWN_V2)
        return
    text = " ".join(ctx.args)
    msg  = await update.message.reply_text(
        f"\U0001F4E2 *POM ARMY ANNOUNCEMENT*\n{DIV}\n\n{safe(text)}\n\n{DIV}",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    await ctx.bot.pin_chat_message(update.effective_chat.id, msg.message_id)


# ─────────────────────────────────────────────────────────────────────────────
#  OWNER-ONLY COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_syncx(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_owner(update): return
    if not X_BEARER_TOKEN:
        await update.message.reply_text("\u274C X\\_BEARER\\_TOKEN not set in \\.env\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    db = load_db()
    last_sync = db["meta"].get("last_x_sync")
    if last_sync:
        elapsed = datetime.utcnow() - datetime.fromisoformat(last_sync)
        if elapsed < timedelta(minutes=SYNC_COOLDOWN_MINS):
            rem = int((timedelta(minutes=SYNC_COOLDOWN_MINS) - elapsed).total_seconds() // 60)
            await update.message.reply_text(
                f"\u23F3 Sync on cooldown\\. Next sync in *{rem} minutes*\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
    status = await update.message.reply_text(
        "\U0001F504 *Syncing X Engagement\\.\\.\\.*\n\n"
        "Checking all registered raid tweets\\.\n"
        "_This may take up to 30 seconds\\._",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    summary = sync_raids(db)
    await save_user_async(d)
    err = f"\n\n\u26A0\uFE0F Errors: {safe(', '.join(summary['errors']))}" if summary["errors"] else ""
    await status.edit_text(
        f"\u2705 *X Sync Complete*\n{DIV}\n\n"
        f"\U0001F4CA Raid tweets checked: *{summary['tweets']}*\n"
        f"\U0001F4E3 Engagements credited: *{summary['engagements']}*\n"
        f"\u270D\uFE0F Organic posts: *{summary['organic']}*{err}",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

async def cmd_resetoffenses(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_owner(update): return
    if not update.message.reply_to_message:
        await update.message.reply_text("\u21A9\uFE0F Reply to the user's message\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    target = update.message.reply_to_message.from_user
    db     = load_db()
    d      = get_user(db, target.id)
    d["offenses"] = 0
    await save_user_async(d)
    await update.message.reply_text(
        f"\u2705 All warnings cleared for *{safe(target.first_name)}*\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

async def cmd_resetlb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_owner(update): return
    if not ctx.args or ctx.args[0].lower() not in {"day","week","month","all"}:
        await update.message.reply_text("Usage: `/resetlb day|week|month|all`", parse_mode=ParseMode.MARKDOWN_V2)
        return
    arg     = ctx.args[0].lower()
    periods = ["day","week","month","alltime"] if arg == "all" else [arg]
    db      = load_db()
    count   = 0
    for ud in db["users"].values():
        for p in periods:
            if p in ud.get("scores", {}):
                ud["scores"][p] = _blank_bucket()
        count += 1
    if arg in ("week", "all"):
        db["meta"]["registered_raids"] = {}
    await save_user_async(d)
    label = "ALL periods" if arg == "all" else f"the *{arg}* leaderboard"
    await update.message.reply_text(
        f"\u267B\uFE0F Reset {label} for *{count}* members\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

async def cmd_distribute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await guard_owner(update): return
    db     = load_db()
    status = await update.message.reply_text(
        "\U0001F4B0 *Computing Weekly Rewards\\.\\.\\.*\n\n"
        "Calculating 70% threshold, ranking qualifiers, fetching \\$POM price\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    rewards = compute_rewards(db)
    if not rewards:
        await status.edit_text(
            "\u26A0\uFE0F *No Qualifying Members*\n\n"
            "Nobody scored 70%\\+ of the total weekly X points pool\\.\n"
            "The full pool has been rolled over to next week\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        await save_user_async(d)
        return

    price = get_pom_price()
    lines = [
        "\U0001F3C6 *POM Army Weekly Rewards*",
        DIV,
        f"\U0001F4B5 Pool: *\\${WEEKLY_POOL_USD}* | \\$POM Price: `${price:.6f}`",
        f"\U0001F465 Winners: *{len(rewards)}*",
        DIV2, "",
    ]
    no_wallet = []

    for uid, r in rewards.items():
        ud     = db["users"].get(uid, {})
        wallet = ud.get("wallet")
        name   = safe(r["username"])
        if not wallet:
            no_wallet.append(name)
            lines.append(f"{MEDALS[r['rank']-1]} @{name} \u2014 \\${r['usd_amount']} \u2014 \u26A0\uFE0F _No wallet set_")
            continue
        await status.edit_text(
            f"\U0001F4B8 Sending to @{name} \\(Rank \\#{r['rank']}\\)\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        tx = send_pom(wallet, r["token_amount"])
        if tx:
            rewards[uid]["paid"]    = True
            rewards[uid]["tx_hash"] = tx
            verify = f"[Verify on BSCScan]({BSCSCAN_TX}{tx})"
            lines.append(
                f"{MEDALS[r['rank']-1]} @{name}\n"
                f"    \U0001F4B0 *{r['token_amount']:,.0f} \\$POM* \\(\\${r['usd_amount']}\\) \u2014 {verify}"
            )
        else:
            lines.append(f"{MEDALS[r['rank']-1]} @{name} \u2014 \\${r['usd_amount']} \u2014 \u274C _TX failed_")

    db["meta"]["last_week_rewards"] = {
        "week_start":    db["meta"].get("week_start"),
        "total_pool":    WEEKLY_POOL_USD,
        "rollover":      db["meta"].get("rollover_usd", 0),
        "distributions": list(rewards.values()),
    }
    await save_user_async(d)

    if no_wallet:
        nw = ", ".join(f"@{n}" for n in no_wallet)
        lines += ["", DIV2, f"\u26A0\uFE0F No wallet: {safe(nw)}", "_Their rewards rolled over_"]

    lines += ["", DIV, f"_Distributed \u2014 {safe(datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC'))}_"]

    summary_text = "\n".join(lines)
    try:
        gid = db["meta"].get("raid_group_id", RAID_GROUP_ID)
        await ctx.bot.send_message(
            chat_id=gid, text=summary_text,
            parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Group post failed: {e}")

    await status.edit_text(
        "\u2705 Distribution complete\\! Results posted in the group\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  MESSAGE HANDLER
# ─────────────────────────────────────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user: return
    chat = update.effective_chat
    if chat.type != "private" and chat.id != RAID_GROUP_ID: return

    u    = update.effective_user
    msg  = update.message
    text = msg.text or ""
    user = update.effective_user
    user_data = await get_user_async(user.id, user.username or "", user.first_name or "")
    d    = get_user(db, u.id, u.username or "", u.first_name)

    # Flood check
    if is_flooding(u.id):
        try:
            await msg.delete()
            until = datetime.utcnow() + timedelta(minutes=SPAM["mute_minutes"])
            await ctx.bot.restrict_chat_member(
                msg.chat.id, u.id,
                permissions=ChatPermissions(can_send_messages=False), until_date=until,
            )
            d["offenses"] += 1
            await save_user_async(d)
            await ctx.bot.send_message(
                msg.chat.id,
                f"\u26A1 @{safe(u.username or u.first_name)} slow down\\! Auto\\-muted for *{SPAM['mute_minutes']} minutes*\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception as e:
            logger.warning(f"Mute failed: {e}")
        return

    # Banned words
    if any(w in text.lower() for w in BANNED_WORDS):
        try:
            await msg.delete()
            d["offenses"] += 1
            await save_user_async(d)
            await ctx.bot.send_message(msg.chat.id, "\U0001F6AB Message removed \u2014 violates community rules\\.", parse_mode=ParseMode.MARKDOWN_V2)
        except Exception: pass
        return

    # X link detection
    x_match = X_LINK_RE.search(text)
    if x_match:
        tweet_id = x_match.group(1)
        raids    = db["meta"].setdefault("registered_raids", {})

        if tweet_id not in raids:
            known_pom       = db["meta"].get("known_pom_tweets", [])
            is_pom_official = tweet_id in known_pom
            tweet_author_xid = None
            tweet_text       = ""

            if X_BEARER_TOKEN:
                tweet_author_xid, tweet_text = get_tweet_data(tweet_id)

            pom_related = is_pom_official or tweet_is_pom_related(tweet_text)

            if pom_related:
                user_x_id = d.get("x_user_id")
                is_own    = bool(user_x_id and tweet_author_xid and tweet_author_xid == user_x_id)

                raids[tweet_id] = {
                    "dropper_uid":     str(u.id),
                    "dropper_name":    display_name(d),
                    "dropped_at":      _now(),
                    "is_pom_official": is_pom_official,
                    "is_own_post":     is_own,
                    "tweet_author":    tweet_author_xid,
                }

                if is_own:
                    add_x_pts(d, X_POINTS["post_drop"])
                    d["x_data"]["last_post_drop"] = _now()
                    h = d["x_data"].get("personal_post_history", [])
                    h.append(tweet_text or text)
                    d["x_data"]["personal_post_history"] = h[-PERSONAL_POST["history_count"]:]
                    await save_user_async(d)
                    await msg.reply_text(
                        "\U0001F517 *Personal Raid Post Registered\\!*\n"
                        f"{DIV2}\n\n"
                        "\u2705 Verified as *your* post\n"
                        "\u2705 Contains *\\$POM* mention\n"
                        f"\U0001F3AF *\\+{X_POINTS['post_drop']} pts* awarded to you\n"
                        "\u23F3 *12\\-hour cooldown* started\n\n"
                        "\U0001F436 *POM Army \u2014 go raid it\\!* \U0001F525\n"
                        "_Everyone who engages earns points_",
                        parse_mode=ParseMode.MARKDOWN_V2,
                        disable_web_page_preview=True,
                    )
                else:
                    if user_x_id and tweet_author_xid and tweet_author_xid != user_x_id:
                        note = "_\\(Not your post \u2014 no personal points awarded\\)_\n"
                    elif not user_x_id:
                        note = "_\\(Link your X with /linkx to earn personal post points\\)_\n"
                    else:
                        note = ""
                    await save_user_async(d)
                    await msg.reply_text(
                        "\U0001F517 *Community Raid Target Registered\\!*\n"
                        f"{DIV2}\n\n"
                        "\u2705 Post contains *\\$POM* mention\n"
                        f"\U0001F3AF 0 pts to dropper\n"
                        f"{note}\n"
                        "\U0001F436 *POM Army \u2014 go raid this post\\!* \U0001F525\n"
                        "_Like | Repost | Comment | Quote for points_",
                        parse_mode=ParseMode.MARKDOWN_V2,
                        disable_web_page_preview=True,
                    )
        return

    # Link flood
    if len(LINK_RE.findall(text)) > SPAM["max_links"]:
        try:
            await msg.delete()
            d["offenses"] += 1
            await save_user_async(d)
            await ctx.bot.send_message(msg.chat.id, "\U0001F6AB Too many links \u2014 message removed\\.", parse_mode=ParseMode.MARKDOWN_V2)
        except Exception: pass
        return

    # TG points with daily cap and min words
    words = len(text.split())
    if words >= TG_MIN_WORDS:
        today_tg = d["scores"]["day"]["tg"]
        if today_tg < TG_DAILY_CAP:
            pts = 1 if words < 15 else 2 if words < 30 else 3
            pts = min(pts, TG_DAILY_CAP - today_tg)
            add_tg_pts(d, pts)
    d["last_active"] = _now()
    await save_user_async(d)


async def handle_new_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != RAID_GROUP_ID: return
    for member in update.message.new_chat_members:
        if member.is_bot: continue
        db = load_db()
        get_user(db, member.id, member.username or "", member.first_name)
        await save_user_async(d)
        await update.message.reply_text(
            f"\U0001F436 *Welcome to the POM Army, {safe(member.first_name)}\\!*\n"
            f"{DIV}\n\n"
            "You've just joined one of the most active communities in crypto\\.\n\n"
            "\U0001F4CC *Get started in 3 steps:*\n"
            "1\\. Register \u2192 /start\n"
            "2\\. Link your X \u2192 /linkx\n"
            "3\\. Learn how to earn \u2192 /howto\n\n"
            f"{DIV2}\n"
            "_Raid hard\\. Earn \\$POM\\. Never stop\\._ \U0001F525",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

async def handle_new_group(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != RAID_GROUP_ID:
        try:
            await update.message.reply_text(
                "\u26D4 PomRaid only operates in the official POM Army group\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            await ctx.bot.leave_chat(update.effective_chat.id)
        except Exception as e:
            logger.warning(f"Could not leave unauthorized group: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  SCHEDULED TASKS
# ─────────────────────────────────────────────────────────────────────────────

async def poll_pom_tweets(app: Application):
    global POM_X_ID
    if not X_BEARER_TOKEN: return
    db=load_db()
    if not POM_X_ID:
        POM_X_ID,_=get_x_user(POM_X_HANDLE)
        if not POM_X_ID: return
    tweets=get_pom_tweets(POM_X_ID,n=5)
    known=set(db["meta"].get("known_pom_tweets",[])); raids=db["meta"].setdefault("registered_raids",{})
    group_id=db["meta"].get("raid_group_id",RAID_GROUP_ID); new=0
    for tweet in tweets:
        tid=tweet["id"]
        if tid in known: continue
        known.add(tid)
        url=f"https://x\\.com/{POM_X_HANDLE}/status/{tid}"
        if tid not in raids:
            raids[tid]={"dropper_uid":None,"dropper_name":f"@{POM_X_HANDLE}",
                        "dropped_at":_now(),"is_pom_official":True,"is_own_post":False}
        try:
            await app.bot.send_message(
                chat_id=group_id,
                text=(
                    f"🚨 *NEW @Pom\\_bsc POST — RAID NOW\\!* 🐶\n"
                    f"{DIV}\n\n"
                    f"🔗 {url}\n\n"
                    f"Engage for points:\n"
                    f"👍 Like *\\+{X_POINTS['like']}* \\| "
                    f"🔁 Repost *\\+{X_POINTS['repost']}* \\| "
                    f"💬 Comment *\\+{X_POINTS['comment']}* \\| "
                    f"🗨️ Quote *\\+{X_POINTS['quote']}*\n\n"
                    f"{DIV2}\n"
                    "_Points sync on next /syncx_"
                ),
                parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=False,
            )
            new+=1
        except Exception as e:
            logger.error(f"Group post failed: {e}")
    db["meta"]["known_pom_tweets"]=list(known)[-200:]; save_db(db)
    if new: logger.info(f"Posted {new} new POM tweet(s)")

async def weekly_reset(app: Application):
    db=load_db()
    for ud in db["users"].values():
        if "week" in ud.get("scores",{}): ud["scores"]["week"]=_blank_bucket()
    db["meta"]["registered_raids"]={}; db["meta"]["week_start"]=_now(); save_db(db)
    logger.info("Weekly reset complete")
    try:
        gid=db["meta"].get("raid_group_id",RAID_GROUP_ID)
        await app.bot.send_message(
            chat_id=gid,
            text=(
                f"🔄 *New Raid Week Has Begun\\!* 🐶\n"
                f"{DIV}\n\n"
                "Weekly scores have been reset\\.\n"
                "A fresh week means a fresh shot at the top 10\\.\n\n"
                "📌 *Reminder:*\n"
                "• Raid every *@Pom\\_bsc* post\n"
                "• Drop your own posts for bonus points\n"
                "• Set your wallet with /wallet to get paid\n\n"
                f"{DIV}\n"
                "_\\$POM Army — Let's run it up\\!_ 🚀"
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as e:
        logger.error(f"Weekly reset announcement failed: {e}")

# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    """Called by PTB after the app is initialised — perfect place for DB setup."""
    await init_db()
    logger.info("Database initialised.")

async def post_shutdown(app: Application) -> None:
    await close_db()

def main():
    global _app_ref
    if not BOT_TOKEN: raise ValueError("BOT_TOKEN not set in .env")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    _app_ref = app

    # Member commands
    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("help",         cmd_help))
    app.add_handler(CommandHandler("howto",        cmd_howto))
    app.add_handler(CommandHandler("score",        cmd_score))
    app.add_handler(CommandHandler("stats",        cmd_stats))
    app.add_handler(CommandHandler("leaderboard",  cmd_leaderboard))
    app.add_handler(CommandHandler("lb",           cmd_leaderboard))
    app.add_handler(CommandHandler("linkx",        cmd_linkx))
    app.add_handler(CommandHandler("unlinkx",      cmd_unlinkx))
    app.add_handler(CommandHandler("wallet",       cmd_wallet))
    app.add_handler(CommandHandler("unwallet",      cmd_unwallet))
    app.add_handler(CommandHandler("rewardstatus",   cmd_rewardstatus))
    app.add_handler(CommandHandler("weeklypreview",  cmd_weeklypreview))
    app.add_handler(CommandHandler("list",         cmd_list))
    app.add_handler(CommandHandler("rules",        cmd_rules))
    app.add_handler(CommandHandler("refreshx",     cmd_refreshx))

    # Admin commands
    app.add_handler(CommandHandler("ban",          cmd_ban))
    app.add_handler(CommandHandler("mute",         cmd_mute))
    app.add_handler(CommandHandler("warn",         cmd_warn))
    app.add_handler(CommandHandler("announce",     cmd_announce))

    # Owner-only commands
    app.add_handler(CommandHandler("syncx",        cmd_syncx))
    app.add_handler(CommandHandler("distribute",   cmd_distribute))
    app.add_handler(CommandHandler("resetlb",      cmd_resetlb))
    app.add_handler(CommandHandler("resetoffenses",cmd_resetoffenses))

    # Callbacks + messages
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_group))

    # Scheduler
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(poll_pom_tweets, "interval", minutes=15, args=[app])
    scheduler.add_job(weekly_reset,    "cron", day_of_week="sun", hour=0, minute=0, args=[app])
    scheduler.start()

    # Register BotFather command menu (visible in bottom-left Menu button)
    import asyncio

    async def set_commands():
        from telegram import BotCommand, BotCommandScopeDefault, BotCommandScopeChat
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
            BotCommand("refreshx",      "Refresh X followers & data"),
            BotCommand("wallet",        "Set your payout wallet"),
            BotCommand("rewardstatus",  "Check weekly reward eligibility"),
            BotCommand("weeklypreview", "See weekly standings and top 10"),
            BotCommand("unwallet",      "Remove your payout wallet"),
            BotCommand("rules",         "Full rules and point breakdown"),
            BotCommand("list",          "Show all commands"),
            BotCommand("refreshx",      "Refresh X followers and data"),
            BotCommand("weeklypreview", "See full weekly standings & top 10"),
            BotCommand("help",          "Get help & support"),
        ]
        owner_cmds = member_cmds + [
            BotCommand("syncx",         "Sync X engagement scores"),
            BotCommand("distribute",    "Run weekly payout"),
            BotCommand("resetlb",       "Reset leaderboard period"),
            BotCommand("resetoffenses", "Clear user warnings"),
            BotCommand("ban",           "Ban a user (reply to message)"),
            BotCommand("mute",          "Mute a user (reply to message)"),
            BotCommand("warn",          "Warn a user (reply to message)"),
            BotCommand("announce",      "Post and pin announcement"),
        ]
        try:
            await app.bot.set_my_commands(member_cmds, scope=BotCommandScopeDefault())
            await app.bot.set_my_commands(owner_cmds,  scope=BotCommandScopeChat(chat_id=OWNER_ID))
            logger.info("BotFather command menu registered")
        except Exception as e:
            logger.warning(f"Could not register commands: {e}")

    asyncio.get_event_loop().run_until_complete(set_commands())

    logger.info("🐶 PomRaid Bot is live.")
    app.run_polling()

if __name__ == "__main__":
    main()