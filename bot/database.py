"""
PomRaid — PostgreSQL Database Layer
Replaces the flat JSON file with a proper async PostgreSQL backend.
Uses asyncpg for high-performance async queries.
"""

import asyncpg
import os
import json
import logging
from datetime import datetime, timedelta, date
from typing import Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
#  CONNECTION POOL  (single global pool shared across all handlers)
# ─────────────────────────────────────────────────────────────────────────────

_pool: Optional[asyncpg.Pool] = None


async def _set_codecs(conn: asyncpg.Connection) -> None:
    """Register JSONB codec so we get dicts/lists back, not strings."""
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "json",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


async def init_db() -> None:
    """
    Create the connection pool and initialise all tables.
    Call once at bot startup.
    """
    global _pool

    database_url = os.getenv("DATABASE_URL", "")
    if not database_url:
        raise ValueError("DATABASE_URL environment variable not set.")

    # Railway appends ?sslmode=require — asyncpg needs ssl="require" kwarg
    if "sslmode=require" in database_url:
        database_url = database_url.replace("?sslmode=require", "").replace("&sslmode=require", "")
        _pool = await asyncpg.create_pool(
            database_url, ssl="require",
            min_size=2, max_size=10,
            init=_set_codecs,
        )
    else:
        _pool = await asyncpg.create_pool(
            database_url,
            min_size=2, max_size=10,
            init=_set_codecs,
        )

    await _create_tables()
    logger.info("PostgreSQL connected and tables ready.")


async def close_db() -> None:
    global _pool
    if _pool:
        await _pool.close()


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    return _pool


# ─────────────────────────────────────────────────────────────────────────────
#  SCHEMA
# ─────────────────────────────────────────────────────────────────────────────

async def _create_tables() -> None:
    async with get_pool().acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                tg_uid             BIGINT      PRIMARY KEY,
                username           TEXT        DEFAULT '',
                first_name         TEXT        DEFAULT '',
                x_handle           TEXT,
                x_user_id          TEXT,
                x_followers        INT         DEFAULT 0,
                wallet             TEXT,
                offenses           INT         DEFAULT 0,
                joined             TIMESTAMPTZ DEFAULT NOW(),
                last_active        TIMESTAMPTZ DEFAULT NOW(),
                last_post_drop     TIMESTAMPTZ,
                post_history       JSONB       DEFAULT '[]',
                x_credits          JSONB       DEFAULT '{}',
                streak_days        INT         DEFAULT 0,
                last_checkin_date  DATE,
                last_unlink_at     TIMESTAMPTZ,
                last_x_handle      TEXT
            );

            CREATE TABLE IF NOT EXISTS scores (
                tg_uid      BIGINT  NOT NULL,
                period      TEXT    NOT NULL,
                tg_pts      INT     DEFAULT 0,
                x_pts       INT     DEFAULT 0,
                reset_at    TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (tg_uid, period),
                FOREIGN KEY (tg_uid) REFERENCES users(tg_uid) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS raids (
                tweet_id         TEXT    PRIMARY KEY,
                dropper_uid      BIGINT,
                dropper_name     TEXT,
                dropped_at       TIMESTAMPTZ DEFAULT NOW(),
                is_pom_official  BOOLEAN DEFAULT FALSE,
                is_own_post      BOOLEAN DEFAULT FALSE,
                tweet_author     TEXT
            );

            CREATE TABLE IF NOT EXISTS engagements (
                tweet_id    TEXT    NOT NULL,
                tg_uid      BIGINT  NOT NULL,
                action      TEXT    NOT NULL,
                credited_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (tweet_id, tg_uid, action)
            );

            CREATE TABLE IF NOT EXISTS meta (
                key     TEXT PRIMARY KEY,
                value   JSONB
            );

            CREATE TABLE IF NOT EXISTS tips_log (
                id              SERIAL PRIMARY KEY,
                from_uid        BIGINT NOT NULL,
                from_username   TEXT,
                to_uid          BIGINT NOT NULL,
                to_username     TEXT,
                amount_usd      NUMERIC(10,2),
                amount_pom      NUMERIC(20,2),
                tx_hash         TEXT,
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                status          TEXT DEFAULT 'pending'
            );

            CREATE TABLE IF NOT EXISTS api_spend (
                day             DATE PRIMARY KEY,
                calls           INT  DEFAULT 0,
                spent_usd       NUMERIC(10,4) DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS raffle_state (
                week_start      DATE PRIMARY KEY,
                entrants        JSONB DEFAULT '[]',
                round1_winner   BIGINT,
                round1_tx       TEXT,
                round1_done     BOOLEAN DEFAULT FALSE,
                round2_winner   BIGINT,
                round2_tx       TEXT,
                round2_done     BOOLEAN DEFAULT FALSE,
                round3_winner   BIGINT,
                round3_tx       TEXT,
                round3_done     BOOLEAN DEFAULT FALSE,
                round4_winner   BIGINT,
                round4_tx       TEXT,
                round4_done     BOOLEAN DEFAULT FALSE,
                pool_usd        NUMERIC(10,2) DEFAULT 16.00,
                created_at      TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_scores_period ON scores(period);
            CREATE INDEX IF NOT EXISTS idx_engagements_tweet ON engagements(tweet_id);
            CREATE INDEX IF NOT EXISTS idx_engagements_user  ON engagements(tg_uid);
            CREATE INDEX IF NOT EXISTS idx_tips_from ON tips_log(from_uid);
            CREATE INDEX IF NOT EXISTS idx_tips_to ON tips_log(to_uid);
        """)

        # Migrate existing users table — add new columns if missing
        # (asyncpg doesn't error on IF NOT EXISTS, but ADD COLUMN does — use safe pattern)
        for col, ddl in [
            ("streak_days",       "INT DEFAULT 0"),
            ("last_checkin_date", "DATE"),
            ("last_unlink_at",    "TIMESTAMPTZ"),
            ("last_x_handle",     "TEXT"),
        ]:
            try:
                await conn.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {ddl}")
            except Exception as e:
                logger.warning(f"Migration ALTER users ADD {col}: {e}")

        # Migrate raffle_state — add round4 columns for existing deployments
        for col, ddl in [
            ("round4_winner", "BIGINT"),
            ("round4_tx",     "TEXT"),
            ("round4_done",   "BOOLEAN DEFAULT FALSE"),
        ]:
            try:
                await conn.execute(f"ALTER TABLE raffle_state ADD COLUMN IF NOT EXISTS {col} {ddl}")
            except Exception as e:
                logger.warning(f"Migration ALTER raffle_state ADD {col}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  META HELPERS  (replaces db["meta"])
# ─────────────────────────────────────────────────────────────────────────────

async def get_meta(key: str, default=None):
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM meta WHERE key = $1", key)
        return row["value"] if row else default


async def set_meta(key: str, value) -> None:
    async with get_pool().acquire() as conn:
        await conn.execute("""
            INSERT INTO meta (key, value)
            VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, key, value)


# ─────────────────────────────────────────────────────────────────────────────
#  USER HELPERS
# ─────────────────────────────────────────────────────────────────────────────

async def get_or_create_user(tg_uid: int, username: str = "", first_name: str = "") -> dict:
    """
    Fetch a user dict from DB. Creates the user + score rows if they don't exist.
    Returns a dict matching the old JSON structure so bot logic needs no changes.
    """
    async with get_pool().acquire() as conn:
        # Upsert user row
        await conn.execute("""
            INSERT INTO users (tg_uid, username, first_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (tg_uid) DO UPDATE SET
                username   = COALESCE(NULLIF($2, ''), users.username),
                first_name = COALESCE(NULLIF($3, ''), users.first_name)
        """, tg_uid, username, first_name)

        # Ensure score rows exist for all periods
        for period in ("alltime", "month", "week", "day"):
            await conn.execute("""
                INSERT INTO scores (tg_uid, period)
                VALUES ($1, $2)
                ON CONFLICT DO NOTHING
            """, tg_uid, period)

        return await _fetch_user_dict(conn, tg_uid)


async def save_user(user_dict: dict) -> None:
    """
    Persist changes made to a user dict back to the database.
    Called after any modification (same pattern as save_db in JSON version).
    """
    uid = user_dict["tg_uid"]
    async with get_pool().acquire() as conn:
        await conn.execute("""
            UPDATE users SET
                username        = $2,
                first_name      = $3,
                x_handle        = $4,
                x_user_id       = $5,
                x_followers     = $6,
                wallet          = $7,
                offenses        = $8,
                last_active     = $9,
                last_post_drop  = $10,
                post_history    = $11,
                x_credits       = $12
            WHERE tg_uid = $1
        """,
            uid,
            user_dict.get("username", ""),
            user_dict.get("first_name", ""),
            user_dict.get("x_handle"),
            user_dict.get("x_user_id"),
            user_dict.get("x_followers", 0),
            user_dict.get("wallet"),
            user_dict.get("offenses", 0),
            datetime.utcnow(),
            _parse_dt(user_dict.get("x_data", {}).get("last_post_drop")),
            list(user_dict.get("x_data", {}).get("personal_post_history", [])),
            dict(user_dict.get("x_data", {}).get("credited_engagements", {})),
        )

        # Update scores
        for period, bucket in user_dict.get("scores", {}).items():
            await conn.execute("""
                INSERT INTO scores (tg_uid, period, tg_pts, x_pts)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (tg_uid, period) DO UPDATE SET
                    tg_pts = $3,
                    x_pts  = $4
            """, uid, period, bucket.get("tg", 0), bucket.get("x", 0))


async def get_all_users() -> list[dict]:
    """Returns all users as a list of dicts (for leaderboard/reward computation)."""
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT tg_uid FROM users")
        users = []
        for row in rows:
            users.append(await _fetch_user_dict(conn, row["tg_uid"]))
        return users


async def _fetch_user_dict(conn, tg_uid: int) -> dict:
    """Build the full user dict from DB rows — matches JSON schema exactly."""
    u = await conn.fetchrow("SELECT * FROM users WHERE tg_uid = $1", tg_uid)
    if not u:
        return {}

    scores_rows = await conn.fetch("SELECT * FROM scores WHERE tg_uid = $1", tg_uid)
    scores = {}
    for s in scores_rows:
        scores[s["period"]] = {
            "tg":       s["tg_pts"],
            "x":        s["x_pts"],
            "reset_at": s["reset_at"].isoformat() if s["reset_at"] else None,
        }
    # Ensure all periods present
    for p in ("alltime", "month", "week", "day"):
        scores.setdefault(p, {"tg": 0, "x": 0, "reset_at": datetime.utcnow().isoformat()})

    post_history = u["post_history"] if u["post_history"] else []
    x_credits    = u["x_credits"]    if u["x_credits"]    else {}

    return {
        "tg_uid":      tg_uid,
        "username":    u["username"]   or "",
        "first_name":  u["first_name"] or "",
        "x_handle":    u["x_handle"],
        "x_user_id":   u["x_user_id"],
        "x_followers": u["x_followers"] or 0,
        "wallet":      u["wallet"],
        "offenses":    u["offenses"]   or 0,
        "joined":      u["joined"].isoformat()      if u["joined"]      else datetime.utcnow().isoformat(),
        "last_active": u["last_active"].isoformat() if u["last_active"] else datetime.utcnow().isoformat(),
        "scores":      scores,
        "x_data": {
            "last_post_drop":        u["last_post_drop"].isoformat() if u["last_post_drop"] else None,
            "personal_post_history": list(post_history),
            "credited_engagements":  dict(x_credits),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
#  RAID HELPERS
# ─────────────────────────────────────────────────────────────────────────────

async def get_raids() -> dict:
    """Returns {tweet_id: raid_info} — matches old registered_raids structure."""
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT * FROM raids")
        return {
            r["tweet_id"]: {
                "dropper_uid":     r["dropper_uid"],
                "dropper_name":    r["dropper_name"],
                "dropped_at":      r["dropped_at"].isoformat() if r["dropped_at"] else None,
                "is_pom_official": r["is_pom_official"],
                "is_own_post":     r["is_own_post"],
                "tweet_author":    r["tweet_author"],
            }
            for r in rows
        }


async def register_raid(tweet_id: str, dropper_uid: int, dropper_name: str,
                         is_pom_official: bool, is_own_post: bool,
                         tweet_author: str = None) -> None:
    async with get_pool().acquire() as conn:
        await conn.execute("""
            INSERT INTO raids
                (tweet_id, dropper_uid, dropper_name, is_pom_official, is_own_post, tweet_author)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT DO NOTHING
        """, tweet_id, dropper_uid, dropper_name, is_pom_official, is_own_post, tweet_author)


async def tweet_already_registered(tweet_id: str) -> bool:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM raids WHERE tweet_id = $1", tweet_id)
        return row is not None


async def is_engagement_credited(tweet_id: str, tg_uid: int, action: str) -> bool:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM engagements WHERE tweet_id=$1 AND tg_uid=$2 AND action=$3",
            tweet_id, tg_uid, action
        )
        return row is not None


async def credit_engagement(tweet_id: str, tg_uid: int, action: str) -> None:
    async with get_pool().acquire() as conn:
        await conn.execute("""
            INSERT INTO engagements (tweet_id, tg_uid, action)
            VALUES ($1, $2, $3)
            ON CONFLICT DO NOTHING
        """, tweet_id, tg_uid, action)


async def clear_week_raids() -> None:
    """Called on weekly reset — clears all raid and engagement records."""
    async with get_pool().acquire() as conn:
        await conn.execute("DELETE FROM engagements")
        await conn.execute("DELETE FROM raids")


# ─────────────────────────────────────────────────────────────────────────────
#  SCORE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

async def add_tg_points_db(tg_uid: int, pts: int) -> None:
    """Add TG points to all 4 periods atomically."""
    async with get_pool().acquire() as conn:
        await conn.execute("""
            UPDATE scores SET tg_pts = tg_pts + $2
            WHERE tg_uid = $1
        """, tg_uid, pts)


async def add_x_points_db(tg_uid: int, pts: int) -> None:
    """Add X points to all 4 periods atomically."""
    async with get_pool().acquire() as conn:
        await conn.execute("""
            UPDATE scores SET x_pts = x_pts + $2
            WHERE tg_uid = $1
        """, tg_uid, pts)


async def get_today_tg_points(tg_uid: int) -> int:
    """Get today's TG points for the daily cap check."""
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT tg_pts FROM scores WHERE tg_uid=$1 AND period='day'", tg_uid
        )
        return row["tg_pts"] if row else 0


async def reset_period_scores(period: str) -> int:
    """Reset scores for a given period. Returns count of users affected."""
    async with get_pool().acquire() as conn:
        if period == "all":
            result = await conn.execute(
                "UPDATE scores SET tg_pts=0, x_pts=0, reset_at=NOW()"
            )
        else:
            result = await conn.execute(
                "UPDATE scores SET tg_pts=0, x_pts=0, reset_at=NOW() WHERE period=$1",
                period
            )
        # result is like "UPDATE 42"
        return int(result.split()[-1])


async def get_weekly_leaderboard(limit: int = 50) -> list[dict]:
    """
    Returns top N users by combined week score, with all their data.
    Much faster than loading all users then sorting in Python.
    """
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("""
            SELECT u.tg_uid, u.username, u.first_name, u.x_handle, u.x_followers,
                   u.wallet, s.tg_pts, s.x_pts, (s.tg_pts + s.x_pts) AS total
            FROM scores s
            JOIN users u ON u.tg_uid = s.tg_uid
            WHERE s.period = 'week' AND (s.tg_pts + s.x_pts) > 0
            ORDER BY total DESC
            LIMIT $1
        """, limit)
        return [dict(r) for r in rows]


async def get_period_leaderboard(period: str, limit: int = 50) -> list[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("""
            SELECT u.tg_uid, u.username, u.first_name, u.x_handle, u.x_followers,
                   u.wallet, s.tg_pts, s.x_pts, (s.tg_pts + s.x_pts) AS total
            FROM scores s
            JOIN users u ON u.tg_uid = s.tg_uid
            WHERE s.period = $1 AND (s.tg_pts + s.x_pts) > 0
            ORDER BY total DESC
            LIMIT $2
        """, period, limit)
        return [dict(r) for r in rows]


async def get_total_x_points_this_week() -> int:
    """Sum of all users' X points this week — used for 70% threshold."""
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COALESCE(SUM(x_pts), 0) AS total FROM scores WHERE period='week'"
        )
        return row["total"]


# ─────────────────────────────────────────────────────────────────────────────
#  STREAK HELPERS
# ─────────────────────────────────────────────────────────────────────────────

async def get_streak(tg_uid: int) -> tuple[int, Optional[date]]:
    """Returns (streak_days, last_checkin_date)."""
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT streak_days, last_checkin_date FROM users WHERE tg_uid=$1",
            tg_uid,
        )
        if not row:
            return 0, None
        return row["streak_days"] or 0, row["last_checkin_date"]


async def update_streak(tg_uid: int, new_streak: int, checkin_date: date) -> None:
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE users SET streak_days=$2, last_checkin_date=$3 WHERE tg_uid=$1",
            tg_uid, new_streak, checkin_date,
        )


# ─────────────────────────────────────────────────────────────────────────────
#  RELINK COOLDOWN HELPERS
# ─────────────────────────────────────────────────────────────────────────────

async def set_last_unlink(tg_uid: int, handle: str, when: Optional[datetime] = None) -> None:
    """Mark when a user unlinked and which handle they had."""
    when = when or datetime.utcnow()
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE users SET last_unlink_at=$2, last_x_handle=$3 WHERE tg_uid=$1",
            tg_uid, when, handle,
        )


async def get_unlink_info(tg_uid: int) -> tuple[Optional[datetime], Optional[str]]:
    """Returns (last_unlink_at, last_x_handle) or (None, None) if never unlinked."""
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT last_unlink_at, last_x_handle FROM users WHERE tg_uid=$1",
            tg_uid,
        )
        if not row:
            return None, None
        return row["last_unlink_at"], row["last_x_handle"]


async def clear_unlink(tg_uid: int) -> None:
    """Owner override — reset cooldown for a user."""
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE users SET last_unlink_at=NULL, last_x_handle=NULL WHERE tg_uid=$1",
            tg_uid,
        )


async def handle_taken_by_other(handle: str, excluding_uid: int) -> Optional[int]:
    """Returns the TG UID currently using this X handle, or None if free."""
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT tg_uid FROM users WHERE LOWER(x_handle) = LOWER($1) AND tg_uid != $2",
            handle, excluding_uid,
        )
        return row["tg_uid"] if row else None


# ─────────────────────────────────────────────────────────────────────────────
#  TIP LOG
# ─────────────────────────────────────────────────────────────────────────────

async def log_tip(from_uid: int, from_username: str, to_uid: int, to_username: str,
                  amount_usd: Optional[float], amount_pom: Optional[float],
                  tx_hash: Optional[str], status: str = 'pending') -> int:
    """Insert tip record; returns the ID."""
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO tips_log (from_uid, from_username, to_uid, to_username,
                                  amount_usd, amount_pom, tx_hash, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id
        """, from_uid, from_username, to_uid, to_username,
            amount_usd, amount_pom, tx_hash, status)
        return row["id"]


async def update_tip_status(tip_id: int, status: str, tx_hash: Optional[str] = None) -> None:
    async with get_pool().acquire() as conn:
        if tx_hash:
            await conn.execute(
                "UPDATE tips_log SET status=$2, tx_hash=$3 WHERE id=$1",
                tip_id, status, tx_hash,
            )
        else:
            await conn.execute(
                "UPDATE tips_log SET status=$2 WHERE id=$1",
                tip_id, status,
            )


async def get_recent_tips(limit: int = 20) -> list[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM tips_log ORDER BY created_at DESC LIMIT $1",
            limit,
        )
        return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
#  API SPEND TRACKING
# ─────────────────────────────────────────────────────────────────────────────

async def record_api_spend(calls: int = 1, cost_usd: float = 0.005) -> None:
    """Increment today's API spend counter."""
    today = datetime.utcnow().date()
    async with get_pool().acquire() as conn:
        await conn.execute("""
            INSERT INTO api_spend (day, calls, spent_usd)
            VALUES ($1, $2, $3)
            ON CONFLICT (day) DO UPDATE SET
                calls     = api_spend.calls + $2,
                spent_usd = api_spend.spent_usd + $3
        """, today, calls, cost_usd)


async def get_api_spend(day: Optional[date] = None) -> dict:
    """Get spend for a specific day, or today if None."""
    if day is None:
        day = datetime.utcnow().date()
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT calls, spent_usd FROM api_spend WHERE day=$1", day,
        )
        if not row:
            return {"day": day, "calls": 0, "spent_usd": 0.0}
        return {"day": day, "calls": row["calls"], "spent_usd": float(row["spent_usd"])}


async def get_api_spend_range(days: int = 30) -> dict:
    """Get total spend over the last N days."""
    cutoff = datetime.utcnow().date() - timedelta(days=days)
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow("""
            SELECT COALESCE(SUM(calls), 0) AS calls,
                   COALESCE(SUM(spent_usd), 0) AS spent_usd
            FROM api_spend WHERE day >= $1
        """, cutoff)
        return {
            "days":  days,
            "calls": row["calls"] or 0,
            "spent_usd": float(row["spent_usd"] or 0),
        }


async def get_api_spend_week() -> float:
    """Total spend Monday → today."""
    today  = datetime.utcnow().date()
    monday = today - timedelta(days=today.weekday())
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COALESCE(SUM(spent_usd), 0) AS total FROM api_spend WHERE day >= $1",
            monday,
        )
        return float(row["total"] or 0)


async def get_api_spend_month() -> float:
    """Total spend this month."""
    today      = datetime.utcnow().date()
    month_start = today.replace(day=1)
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COALESCE(SUM(spent_usd), 0) AS total FROM api_spend WHERE day >= $1",
            month_start,
        )
        return float(row["total"] or 0)


# ─────────────────────────────────────────────────────────────────────────────
#  RAFFLE STATE
# ─────────────────────────────────────────────────────────────────────────────

async def get_or_create_raffle_state(week_start: date) -> dict:
    """Fetch the raffle state for the given week; create empty record if missing."""
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM raffle_state WHERE week_start=$1", week_start,
        )
        if row:
            return dict(row)
        await conn.execute(
            "INSERT INTO raffle_state (week_start, entrants) VALUES ($1, $2)",
            week_start, [],
        )
        row = await conn.fetchrow(
            "SELECT * FROM raffle_state WHERE week_start=$1", week_start,
        )
        return dict(row)


async def save_raffle_entrants(week_start: date, entrants: list) -> None:
    """Save the list of qualified raffle entrants (with their tickets)."""
    async with get_pool().acquire() as conn:
        await conn.execute(
            "UPDATE raffle_state SET entrants=$2 WHERE week_start=$1",
            week_start, entrants,
        )


async def save_raffle_round(week_start: date, round_num: int,
                             winner_uid: Optional[int], tx_hash: Optional[str],
                             done: bool = True) -> None:
    """Record the outcome of a raffle round."""
    if round_num not in (1, 2, 3, 4):
        return
    async with get_pool().acquire() as conn:
        await conn.execute(f"""
            UPDATE raffle_state SET
                round{round_num}_winner = $2,
                round{round_num}_tx     = $3,
                round{round_num}_done   = $4
            WHERE week_start = $1
        """, week_start, winner_uid, tx_hash, done)


async def get_raffle_state(week_start: date) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM raffle_state WHERE week_start=$1", week_start,
        )
        return dict(row) if row else None


# ─────────────────────────────────────────────────────────────────────────────
#  30-DAY ROLLING TOTALS (for monthly loyalty)
# ─────────────────────────────────────────────────────────────────────────────

async def get_monthly_loyalty_top3() -> list[dict]:
    """
    Returns the top 3 users by their stored 'month' period totals.
    This approximates 30-day rolling totals well enough — the 'month' bucket
    is reset on the 1st, so on the 1st of next month it represents the prior month.
    """
    return await get_period_leaderboard("month", limit=3)


# ─────────────────────────────────────────────────────────────────────────────
#  UTILITY
# ─────────────────────────────────────────────────────────────────────────────

def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None