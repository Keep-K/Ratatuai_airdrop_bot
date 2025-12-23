import os
import re
import json
import sqlite3
import secrets
import hashlib
import time
import httpx
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
    MenuButtonCommands,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# -------------------------
# Env / Config
# -------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
RECIPE_AI_BASE_URL = os.getenv("RECIPE_AI_BASE_URL", "http://127.0.0.1:8000").strip().rstrip("/")
RECIPE_AI_TIMEOUT = 30
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip()

DB_PATH = os.getenv("DB_PATH", "db.sqlite3")
CONFIG_PATH = os.getenv("CONFIG_PATH", "config.json")

ASSETS_DIR = Path(__file__).parent / "assets"
START_IMAGE = ASSETS_DIR / "start.jpg"
TERMS_IMAGE = ASSETS_DIR / "terms.jpg"
BONUS_IMAGE = ASSETS_DIR / "bonus.jpg"
CONNECT_WALLET_IMAGE = ASSETS_DIR / "connect_wallet.jpg"
BALANCE_IMAGE = ASSETS_DIR / "balance.jpg"
CHAT_IMAGE = ASSETS_DIR / "chat.jpg"


def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise SystemExit(f"Missing {CONFIG_PATH}. Create it first.")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


CONFIG = load_config()

MENU = CONFIG.get("ui", {}).get("menu", {})
TERMS_TEXT = CONFIG.get("terms_text", "📄 Terms")
POINTS = CONFIG.get("points", {})
MISSIONS = CONFIG.get("missions", [])

TZ = ZoneInfo(CONFIG.get("timezone", "Asia/Seoul"))

POINT_WALLET_REGISTER = int(POINTS.get("wallet_register", 50))
POINT_REFERRAL_QUALIFIED = int(POINTS.get("referral_qualified", 100))
POINT_DAILY_CHECKIN = int(POINTS.get("daily_checkin", 10))
RAW_STREAK_BONUSES = POINTS.get("streak_bonuses", [])
STREAK_BONUSES = []
if isinstance(RAW_STREAK_BONUSES, list):
    for r in RAW_STREAK_BONUSES:
        try:
            d = int(r.get("days"))
            p = int(r.get("points"))
            if d > 0 and p != 0:
                STREAK_BONUSES.append({"days": d, "points": p})
        except Exception:
            pass
STREAK_BONUSES.sort(key=lambda x: x["days"])

# Validators
WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
# X profile link only (no @handle)
X_PROFILE_URL_RE = re.compile(r"^https?://(www\.)?(x\.com|twitter\.com)/[A-Za-z0-9_]{1,15}/?$")

# Telegram username must start with @
TG_USERNAME_RE = re.compile(r"^@[A-Za-z0-9_]{5,32}$")


# -------------------------
# DB helpers
# -------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        ref_code TEXT UNIQUE,
        referred_by INTEGER,
        wallet_address TEXT,
        wallet_pending TEXT,
        wallet_change_count INTEGER DEFAULT 0,
        state TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        unique_key TEXT NOT NULL,
        type TEXT NOT NULL,
        amount INTEGER NOT NULL,
        meta TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        UNIQUE(user_id, unique_key)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS submissions (
        user_id INTEGER NOT NULL,
        mission_id TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (user_id, mission_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS recipe_fingerprints (
        mission_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (mission_id, fingerprint)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS pending_rewards (
        user_id INTEGER NOT NULL,
        mission_id TEXT NOT NULL,
        unique_key TEXT NOT NULL,
        amount INTEGER NOT NULL,
        available_at_ts INTEGER NOT NULL,
        created_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (user_id, mission_id)
    )
    """)

    conn.commit()

    # Global wallet uniqueness
    try:
        cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_wallet_address
        ON users(wallet_address)
        WHERE wallet_address IS NOT NULL
        """)
        conn.commit()
    except Exception as e:
        print("[WARN] Could not create UNIQUE index for wallet_address.")
        print("       Resolve duplicate wallet_address rows in DB, then restart.")
        print(f"       Error: {e}")

    conn.close()


def ensure_user(user_id: int, username: str | None):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()

    if row:
        cur.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
        conn.commit()
        conn.close()
        return

    ref_code = secrets.token_urlsafe(8).replace("-", "").replace("_", "")
    cur.execute(
        "INSERT INTO users (user_id, username, ref_code) VALUES (?, ?, ?)",
        (user_id, username, ref_code),
    )
    conn.commit()
    conn.close()


def get_user(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def find_user_by_ref_code(ref_code: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE ref_code = ?", (ref_code,))
    row = cur.fetchone()
    conn.close()
    return row


def set_referred_by_if_empty(user_id: int, referrer_id: int):
    if user_id == referrer_id:
        return
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT referred_by FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row and row["referred_by"] is None:
        cur.execute("UPDATE users SET referred_by = ? WHERE user_id = ?", (referrer_id, user_id))
        conn.commit()
    conn.close()


def set_state(user_id: int, state: str | None):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET state = ? WHERE user_id = ?", (state, user_id))
    conn.commit()
    conn.close()


def add_points_once(user_id: int, unique_key: str, typ: str, amount: int, meta: dict | None = None) -> bool:
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO ledger (user_id, unique_key, type, amount, meta) VALUES (?, ?, ?, ?, ?)",
            (user_id, unique_key, typ, amount, json.dumps(meta or {}, ensure_ascii=False)),
        )
        conn.commit()
        ok = True
    except sqlite3.IntegrityError:
        ok = False
    conn.close()
    return ok

def add_pending_reward(user_id: int, mission_id: str, unique_key: str, amount: int, delay_minutes: int) -> tuple[bool, int]:
    available_at_ts = int(time.time()) + int(delay_minutes) * 60
    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO pending_rewards (user_id, mission_id, unique_key, amount, available_at_ts) VALUES (?, ?, ?, ?, ?)",
            (user_id, mission_id, unique_key, int(amount), int(available_at_ts))
        )
        conn.commit()
        ok = True
    except sqlite3.IntegrityError:
        ok = False
    conn.close()
    return ok, available_at_ts


def process_due_rewards(user_id: int) -> list[tuple[str, int]]:
    """Return list of (mission_id, amount) that were credited now."""
    now_ts = int(time.time())
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT mission_id, unique_key, amount FROM pending_rewards WHERE user_id=? AND available_at_ts<=?",
        (user_id, now_ts),
    )
    rows = cur.fetchall()

    credited = []
    for r in rows:
        mission_id = r["mission_id"]
        unique_key = r["unique_key"]
        amount = int(r["amount"])

        # ledger에 실제 적립 (idempotent)
        ok = add_points_once(user_id, unique_key, f"mission:{mission_id}", amount, meta={"delayed": True})
        # 적립되었든(OK) 이미 적립되어 있든(False) pending은 제거
        cur.execute("DELETE FROM pending_rewards WHERE user_id=? AND mission_id=?", (user_id, mission_id))
        conn.commit()

        if ok:
            credited.append((mission_id, amount))

    conn.close()
    return credited


def get_points(user_id: int) -> int:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(SUM(amount), 0) AS s FROM ledger WHERE user_id = ?", (user_id,))
    s = cur.fetchone()["s"]
    conn.close()
    return int(s)


def get_referral_counts(referrer_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE referred_by = ?", (referrer_id,))
    total = int(cur.fetchone()["c"])
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE referred_by = ? AND wallet_address IS NOT NULL", (referrer_id,))
    qualified = int(cur.fetchone()["c"])
    conn.close()
    return total, qualified


def has_wallet(user_id: int) -> bool:
    row = get_user(user_id)
    return bool(row and row["wallet_address"])

import httpx

async def call_recipe_ai(user_id: int, text: str) -> str:
    url = f"{RECIPE_AI_BASE_URL}/chat"

    payload = {
        "message": text,
        "user_id": str(user_id),
        "spiciness": "normal",
        "saltiness": "normal"
    }

    try:
        async with httpx.AsyncClient(timeout=RECIPE_AI_TIMEOUT) as client:
            r = await client.post(url, json=payload)

        # JSON이 아닌 텍스트 응답도 방어
        ctype = (r.headers.get("content-type") or "").lower()
        if "application/json" not in ctype:
            if r.status_code >= 400:
                return f"Recipe AI error: {r.status_code}\n{r.text[:300]}"
            return r.text

        r.raise_for_status()
        data = r.json()

        # ✅ 영어로 이미 만들어진 최종 답변을 반드시 우선 반환
        answer = data.get("markdown_message") or data.get("message")
        if answer:
            # (선택) suggestions 붙이기
            suggestions = data.get("suggestions")
            if isinstance(suggestions, list) and suggestions:
                answer += "\n\nSuggestions:\n- " + "\n- ".join(map(str, suggestions[:5]))
            return answer

        # ✅ 여기서부터는 "서버 응답 형식이 예상과 다름" 케이스
        # str(data)를 통째로 찍으면 recipes.title(한국어)가 섞여 보일 수 있으니 제한적으로 출력
        keys = ", ".join(list(data.keys())[:20]) if isinstance(data, dict) else "non-dict"
        return f"Recipe AI returned an unexpected response format. keys={keys}"

    except httpx.ConnectError:
        return "Recipe AI server is not reachable. Please check that the server is running."
    except httpx.HTTPStatusError as e:
        # status + body 일부까지 같이 보여주면 디버깅 쉬움
        return f"Recipe AI error: {e.response.status_code}\n{e.response.text[:300]}"
    except Exception as e:
        return f"Unexpected error: {e}"


# -------------------------
# UI helpers
# -------------------------
def main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            [MENU.get("terms", "📄 Terms"), MENU.get("chat", "🤖 Chat")],
            [MENU.get("bonus", "💰 Bonus"), MENU.get("balance", "🏆 Balance")],
            [MENU.get("connect_wallet", "🦊 Connect Wallet")],
        ],
        resize_keyboard=True
    )


async def reply_photo_or_text(msg_obj, image_path: Path, caption: str, reply_markup=None):
    # Telegram caption limit ~1024 chars. If too long, send photo then text separately.
    if image_path.exists():
        try:
            with image_path.open("rb") as f:
                if len(caption) <= 900:
                    await msg_obj.reply_photo(photo=f, caption=caption, reply_markup=reply_markup)
                else:
                    await msg_obj.reply_photo(photo=f, reply_markup=reply_markup)
                    await msg_obj.reply_text(caption)
            return
        except Exception:
            pass

    await msg_obj.reply_text(caption, reply_markup=reply_markup)


def get_mission(mission_id: str):
    for m in MISSIONS:
        if m.get("id") == mission_id:
            return m
    return None


def validate(validator: str, text: str) -> tuple[bool, str, str]:
    if validator == "x_profile":
        if X_PROFILE_URL_RE.match(text):
            return True, "", text
        return False, "Please send your X profile link like https://x.com/username", text

    if validator == "tg_username":
        if TG_USERNAME_RE.match(text):
            return True, "", text
        return False, "Please send your Telegram username starting with @ (e.g., @myname).", text

    if validator == "recipe_text":
        t = text.strip()

        # 1) 너무 짧으면 컷 (대충 복붙/스팸 방지)
        if len(t) < 200:
            return False, "Please send a longer recipe (min 200 characters). Include Ingredients + Steps.", t

        # 2) 링크 금지 (홍보/스팸 방지)
        if re.search(r"https?://|www\.", t, re.IGNORECASE):
            return False, "Please do not include links in your recipe.", t

        # 3) 최소 구조 요구 (Ingredients/Steps 또는 한국어 재료/방법)
        has_en = re.search(r"\bingredients\b", t, re.IGNORECASE) and re.search(r"\bsteps\b|\bdirections\b|\bmethod\b", t, re.IGNORECASE)
        if not has_en:
            return False, "Please include sections: Ingredients + Steps.", t

        return True, "", t

    return True, "", text


def get_checkin_dates(user_id: int) -> set:
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT unique_key FROM ledger "
        "WHERE user_id=? AND type='daily_checkin' AND unique_key LIKE 'daily_checkin:%'",
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()

    dates = set()
    for r in rows:
        uk = r["unique_key"]
        try:
            ds = uk.split(":", 1)[1]  # YYYY-MM-DD
            dates.add(datetime.strptime(ds, "%Y-%m-%d").date())
        except Exception:
            continue
    return dates


def compute_streak(user_id: int, today_date) -> int:
    dates = get_checkin_dates(user_id)
    streak = 0
    d = today_date
    while d in dates:
        streak += 1
        d = d - timedelta(days=1)
    return streak

def normalize_recipe_text(text: str) -> str:
    t = text.strip().lower()
    # collapse whitespace
    t = re.sub(r"\s+", " ", t)
    return t


def claim_recipe_fingerprint(mission_id: str, user_id: int, text: str) -> bool:
    norm = normalize_recipe_text(text)
    fp = hashlib.sha256(norm.encode("utf-8")).hexdigest()

    conn = db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO recipe_fingerprints (mission_id, fingerprint, user_id) VALUES (?, ?, ?)",
            (mission_id, fp, user_id)
        )
        conn.commit()
        ok = True
    except sqlite3.IntegrityError:
        ok = False
    conn.close()
    return ok


# -------------------------
# Handlers
# -------------------------
async def ai_chat_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id, update.effective_user.username)

    set_state(user_id, "AI_CHAT")

    caption = (
        "🤖 Recipe AI Chat\n\n"
        "Send me a message about recipes (ingredients, steps, substitutions, etc.).\n"
        "To exit, press another menu button."
    )

    await reply_photo_or_text(update.message, CHAT_IMAGE, caption, reply_markup=main_menu_keyboard())


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username)

    # referral payload
    if context.args:
        ref_code = context.args[0].strip()
        ref_user = find_user_by_ref_code(ref_code)
        if ref_user:
            set_referred_by_if_empty(user.id, ref_user["user_id"])

    caption = (
        "Welcome! Use the menu below.\n\n"
        "• Connect Wallet: register your BSC wallet\n"
        "• Bonus: do missions to earn points\n"
        "• Balance: check your points & invite friends"
    )

    await reply_photo_or_text(update.message, START_IMAGE, caption, reply_markup=main_menu_keyboard())


async def terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_photo_or_text(update.message, TERMS_IMAGE, TERMS_TEXT, reply_markup=main_menu_keyboard())


async def bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id, update.effective_user.username)

    if not has_wallet(user_id):
        await update.message.reply_text("Please connect your wallet first!", reply_markup=main_menu_keyboard())
        return

    lines = [
        "🎁 Bonus Missions\n"
        "Complete missions and earn points.\n"
        "Points are granted once per mission and may be reviewed for abuse.\n"
    ]
    kb_rows = [
        [InlineKeyboardButton(f"✅ Daily Check-in (+{POINT_DAILY_CHECKIN})", callback_data="daily_checkin")]
    ]

    lines.append(
        "🗓️ Daily Check-in\n"
        f"• Tap once per day (UTC) to claim +{POINT_DAILY_CHECKIN} points.\n"
        "• Keep your streak going to unlock extra bonuses!\n"
    )

    if STREAK_BONUSES:
        streak_text = ", ".join([f"{r['days']} days → +{r['points']}" for r in STREAK_BONUSES])
        lines.append("🔥 Streak Bonuses\n" f"• {streak_text}\n")

    lines.append("🚀 Social Missions (Submit after you complete the task)")
    for m in MISSIONS:
        title = m.get("button_text", m.get("id"))
        pts = int(m.get("points", 0))
        lines.append(f"• {title} → +{pts} points")
        kb_rows.append([InlineKeyboardButton(title, callback_data=f"mission:{m.get('id')}")])

    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(kb_rows)

    await reply_photo_or_text(update.message, BONUS_IMAGE, text, reply_markup=kb)


async def connect_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id, update.effective_user.username)
    row = get_user(user_id)

    if row["wallet_address"]:
        caption = (
            "🦊 Connect Wallet\n\n"
            "✅ Wallet already registered:\n"
            f"{row['wallet_address']}\n\n"
            "Need to change it?"
        )

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔁 Change wallet", callback_data="wallet_change")],
        ])

        await reply_photo_or_text(update.message, CONNECT_WALLET_IMAGE, caption, reply_markup=kb)
        return

    set_state(user_id, "AWAIT_WALLET")

    caption = (
        "🦊 Connect Wallet\n\n"
        "Connect a non-custodial wallet that supports EVM networks "
        "(like Metamask, Rabby, Trust Wallet, etc.).\n\n"
        "Enter your BSC wallet address:"
    )

    await reply_photo_or_text(update.message, CONNECT_WALLET_IMAGE, caption, reply_markup=main_menu_keyboard())


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id, update.effective_user.username)

    if not has_wallet(user_id):
        await update.message.reply_text("Please connect your wallet first!", reply_markup=main_menu_keyboard())
        return

    points = get_points(user_id)
    total_ref, qualified_ref = get_referral_counts(user_id)

    caption = (
        f"Balance: {points} points 🎯\n"
        f"Total referrals: {total_ref}\n"
        f"Qualified referrals (wallet connected): {qualified_ref}\n\n"
        "Invite friends and earn more points.\n"
        "Share your referral link below — you’ll get bonus points when they connect a wallet."
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Invite friend", callback_data="invite_friend")],
    ])

    await reply_photo_or_text(update.message, BALANCE_IMAGE, caption, reply_markup=kb)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    ensure_user(user_id, q.from_user.username)

    data = q.data or ""
    user_row = get_user(user_id)

    # credit any due delayed rewards
    newly = process_due_rewards(user_id)
    if newly:
        total = sum(a for _, a in newly)
        await q.message.reply_text(f"🎉 Your delayed rewards are now credited: +{total} points!")

    # Daily check-in (once per day)
    if data == "daily_checkin":
        if not user_row["wallet_address"]:
            await q.message.reply_text("Please connect your wallet first!", reply_markup=main_menu_keyboard())
            return

        today_date = datetime.now(TZ).date()
        today_str = today_date.strftime("%Y-%m-%d")
        unique_key = f"daily_checkin:{today_str}"

        awarded = add_points_once(
            user_id,
            unique_key,
            "daily_checkin",
            POINT_DAILY_CHECKIN,
            meta={"date": today_str}
        )

        # streak 계산(오늘 포함)
        streak = compute_streak(user_id, today_date)

        if not awarded:
            await q.message.reply_text(
                f"✅ You already checked in today.\nCurrent streak: {streak} day(s).",
                reply_markup=main_menu_keyboard()
            )
            return

        bonus_lines = []
        for rule in STREAK_BONUSES:
            if streak == rule["days"]:
                bkey = f"streak_bonus:{rule['days']}:{today_str}"
                ok = add_points_once(
                    user_id,
                    bkey,
                    "streak_bonus",
                    int(rule["points"]),
                    meta={"days": int(rule["days"]), "date": today_str}
                )
                if ok:
                    bonus_lines.append(f"🔥 Streak bonus ({rule['days']} days): +{rule['points']} points!")

        msg = (
            f"✅ Daily check-in complete! +{POINT_DAILY_CHECKIN} points.\n"
            f"Current streak: {streak} day(s)."
        )
        if bonus_lines:
            msg += "\n\n" + "\n".join(bonus_lines)

        await q.message.reply_text(msg, reply_markup=main_menu_keyboard())
        return

    if data == "invite_friend":
        if not user_row["wallet_address"]:
            await q.message.reply_text("Please connect your wallet first!", reply_markup=main_menu_keyboard())
            return

        ref_link = f"https://t.me/{BOT_USERNAME}?start={user_row['ref_code']}"
        await q.message.reply_text(
            "👥 Invite friend\n\n"
            f"Your referral link:\n{ref_link}\n\n"
            f"When your friend connects a wallet, you get +{POINT_REFERRAL_QUALIFIED} points.",
            reply_markup=main_menu_keyboard()
        )
        return

    if data.startswith("mission:"):
        if not user_row["wallet_address"]:
            await q.message.reply_text("Please connect your wallet first!", reply_markup=main_menu_keyboard())
            return

        mission_id = data.split(":", 1)[1]
        m = get_mission(mission_id)
        if not m:
            await q.message.reply_text("Mission not found in config.json.")
            return

        set_state(user_id, f"MISSION:{mission_id}")

        url = (m.get("url") or "").strip()
        prompt = m.get("prompt", "Send your submission:")
        intro = (m.get("intro") or "").strip()

        msg_parts = []
        if intro:
            msg_parts.append(intro)

        # url이 있으면 링크도 보여주고 버튼도 제공
        kb = None
        if url:
            msg_parts.append(url)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Open link", url=url)]])

    # 항상 prompt는 마지막에 붙이기
        msg_parts.append(prompt)

        msg = "\n\n".join(msg_parts)
        await q.message.reply_text(msg, reply_markup=kb)
        return
    
    if data == "wallet_change":
        # 지갑 변경은 연결된 유저만
        if not user_row["wallet_address"]:
            await q.message.reply_text("Please connect your wallet first!", reply_markup=main_menu_keyboard())
            return

        # 변경 횟수 제한(권장: 1회)
        try:
            cnt = int(user_row["wallet_change_count"] or 0)
        except Exception:
            cnt = 0

        if cnt >= 1:
            await q.message.reply_text(
                "⚠️ Wallet change is limited.\nPlease contact support if you really need to update it.",
                reply_markup=main_menu_keyboard()
            )
            return

        # 변경 모드 진입
        conn = db()
        cur = conn.cursor()
        cur.execute("UPDATE users SET state = 'AWAIT_WALLET_CHANGE', wallet_pending = NULL WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()

        await q.message.reply_text(
            "🔁 Change Wallet\n\nPlease enter your NEW BSC wallet address (0x...):",
            reply_markup=main_menu_keyboard()
        )
        return

    if data == "wallet_confirm":
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT wallet_address, wallet_pending, referred_by FROM users WHERE user_id = ?", (user_id,))
        dbrow = cur.fetchone()

        if not dbrow:
            conn.close()
            return

        if dbrow["wallet_address"]:
            conn.close()
            await q.message.reply_text("✅ Wallet already registered.", reply_markup=main_menu_keyboard())
            return

        pending = dbrow["wallet_pending"]
        if not pending:
            conn.close()
            await q.message.reply_text("No pending wallet found. Please try Connect Wallet again.")
            return

        # global duplication check
        cur.execute("SELECT user_id FROM users WHERE wallet_address = ? AND user_id != ?", (pending, user_id))
        dup = cur.fetchone()
        if dup:
            conn.close()
            await q.message.reply_text(
                "❌ This wallet address is already registered by another user.\n"
                "Please use a different wallet."
            )
            return

        try:
            cur.execute(
                "UPDATE users SET wallet_address = ?, wallet_pending = NULL, state = NULL WHERE user_id = ?",
                (pending, user_id)
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            await q.message.reply_text(
                "❌ This wallet address is already registered.\n"
                "Please use a different wallet."
            )
            return
        conn.close()

        add_points_once(user_id, "wallet_register", "wallet_register", POINT_WALLET_REGISTER)

        referred_by = dbrow["referred_by"]
        if referred_by:
            add_points_once(
                int(referred_by),
                f"referral:{user_id}",
                "referral_wallet_connected",
                POINT_REFERRAL_QUALIFIED,
                meta={"referred_user_id": user_id}
            )

        await q.message.reply_text(
            f"✅ You have successfully registered your wallet:\n{pending}",
            reply_markup=main_menu_keyboard()
        )
        return

    if data == "wallet_retry":
        conn = db()
        cur = conn.cursor()
        cur.execute("UPDATE users SET wallet_pending = NULL, state = 'AWAIT_WALLET' WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        await q.message.reply_text(
            "Enter your BSC wallet address again:",
            reply_markup=main_menu_keyboard()
        )
        return
    
    if data == "wallet_change_confirm":
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT wallet_address, wallet_pending, wallet_change_count FROM users WHERE user_id = ?", (user_id,))
        dbrow = cur.fetchone()

        if not dbrow:
            conn.close()
            return

        old_wallet = dbrow["wallet_address"]
        pending = dbrow["wallet_pending"]

        if not old_wallet:
            conn.close()
            await q.message.reply_text("No existing wallet found. Use Connect Wallet first.", reply_markup=main_menu_keyboard())
            return

        if not pending:
            conn.close()
            await q.message.reply_text("No pending wallet found. Please try again.", reply_markup=main_menu_keyboard())
            return

        # 같은 지갑으로 변경 시도 방지(선택)
        if pending == old_wallet:
            conn.close()
            await q.message.reply_text("✅ This is the same wallet as current. No changes made.", reply_markup=main_menu_keyboard())
            return

        # 전역 중복 체크
        cur.execute("SELECT user_id FROM users WHERE wallet_address = ? AND user_id != ?", (pending, user_id))
        dup = cur.fetchone()
        if dup:
            conn.close()
            await q.message.reply_text(
                "❌ This wallet address is already registered by another user.\nPlease use a different wallet.",
                reply_markup=main_menu_keyboard()
            )
            return

        # 변경 횟수 증가 + 주소 변경 (포인트/레퍼럴 추가 지급 없음)
        cur.execute(
            "UPDATE users SET wallet_address = ?, wallet_pending = NULL, state = NULL, wallet_change_count = COALESCE(wallet_change_count,0)+1 WHERE user_id = ?",
            (pending, user_id)
        )
        conn.commit()
        conn.close()

        await q.message.reply_text(
            f"✅ Wallet updated successfully.\n\nOld: {old_wallet}\nNew: {pending}",
            reply_markup=main_menu_keyboard()
        )
        return
    
    if data == "wallet_change_retry":
        conn = db()
        cur = conn.cursor()
        cur.execute("UPDATE users SET wallet_pending = NULL, state = 'AWAIT_WALLET_CHANGE' WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()

        await q.message.reply_text(
            "Enter your NEW BSC wallet address again:",
            reply_markup=main_menu_keyboard()
        )
        return


async def on_any_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id, update.effective_user.username)
    row = get_user(user_id)

    # credit any due delayed rewards
    newly = process_due_rewards(user_id)
    if newly:
        total = sum(a for _, a in newly)
        await update.message.reply_text(f"🎉 Your delayed rewards are now credited: +{total} points!", reply_markup=main_menu_keyboard())

    text = (update.message.text or "").strip()

    # menu routing
    if text == MENU.get("terms", "📄 Terms") or text in ["Terms", "📄 Terms"]:
        return await terms(update, context)
    if text == MENU.get("bonus", "💰 Bonus") or text in ["Bonus", "💰 Bonus"]:
        return await bonus(update, context)
    if text == MENU.get("connect_wallet", "🦊 Connect Wallet") or text in ["Connect Wallet", "🦊 Connect Wallet"]:
        return await connect_wallet(update, context)
    if text == MENU.get("balance", "🏆 Balance") or text in ["Balance", "🏆 Balance"]:
        return await balance(update, context)
    if text == MENU.get("chat", "🤖 Chat"):
        return await ai_chat_start(update, context)

    state = (row["state"] or "").strip()

    if state == "AI_CHAT":
        answer = await call_recipe_ai(user_id, text)
        await update.message.reply_text(answer, reply_markup=main_menu_keyboard())
        return

    # wallet input
    if state == "AWAIT_WALLET":
        if not WALLET_RE.match(text):
            await update.message.reply_text("The wallet address is not correct. Check it out!")
            return

        conn = db()
        cur = conn.cursor()
        cur.execute("UPDATE users SET wallet_pending = ? WHERE user_id = ?", (text, user_id))
        conn.commit()
        conn.close()

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm", callback_data="wallet_confirm"),
             InlineKeyboardButton("✏️ Re-enter", callback_data="wallet_retry")]
        ])
        await update.message.reply_text(
            f"Please confirm your wallet address:\n{text}\n\nIs it correct?",
            reply_markup=kb
        )
        return

    if state == "AWAIT_WALLET_CHANGE":
        if not WALLET_RE.match(text):
            await update.message.reply_text("The wallet address is not correct. Check it out!")
            return

        conn = db()
        cur = conn.cursor()
        cur.execute("UPDATE users SET wallet_pending = ? WHERE user_id = ?", (text, user_id))
        conn.commit()
        conn.close()

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm", callback_data="wallet_change_confirm"),
             InlineKeyboardButton("✏️ Re-enter", callback_data="wallet_change_retry")]
        ])

        await update.message.reply_text(
            f"Please confirm your NEW wallet address:\n{text}\n\nIs it correct?",
            reply_markup=kb
        )
        return

    # mission submission input
    if state.startswith("MISSION:"):
        mission_id = state.split(":", 1)[1]
        m = get_mission(mission_id)
        if not m:
            set_state(user_id, None)
            await update.message.reply_text("Mission is not configured. Please try again.")
            return

        ok, err, normalized = validate(m.get("validator", ""), text)
        if not ok:
            await update.message.reply_text(err)
            return

        # recipe: global duplicate protection (keep state so user can resubmit)
        if mission_id == "recipe_submit":
            if not claim_recipe_fingerprint(mission_id, user_id, normalized):
                await update.message.reply_text(
                    "⚠️ This recipe looks already submitted by someone else.\n"
                    "Please submit an original recipe (different content)."
                )
                return

        # store submission once per mission
        conn = db()
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO submissions (user_id, mission_id, payload) VALUES (?, ?, ?)",
                (user_id, mission_id, normalized)
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass
        conn.close()

        points = int(m.get("points", 0))
        delay_minutes = int(m.get("delay_minutes", 0) or 0)

        # 지연 지급이면 pending_rewards에 넣고, 30분 뒤(다음 상호작용 시) 적립
        if delay_minutes > 0:
            ok, ts = add_pending_reward(
                user_id,
                mission_id,
                f"mission:{mission_id}",  # unique_key (mission 1회 제한 유지)
                points,
                delay_minutes
            )
            set_state(user_id, None)

            if ok:
                await update.message.reply_text(
                    f"✅ Saved.\nYour points will be credited after verification. Estimated time: {delay_minutes} minutes.",
                    reply_markup=main_menu_keyboard()
                )
            else:
                await update.message.reply_text(
                    "✅ Saved. (reward already scheduled)",
                    reply_markup=main_menu_keyboard()
                )
            return

        # 기존: 즉시 지급
        awarded = add_points_once(
            user_id,
            f"mission:{mission_id}",
            f"mission:{mission_id}",
            points,
            meta={"payload": normalized}
        )

        set_state(user_id, None)

        if awarded:
            await update.message.reply_text(f"✅ Saved. +{points} points!", reply_markup=main_menu_keyboard())
        else:
            await update.message.reply_text("✅ Saved. (points already granted)", reply_markup=main_menu_keyboard())
        return

        set_state(user_id, None)

        if awarded:
            await update.message.reply_text(f"✅ Saved. +{points} points!", reply_markup=main_menu_keyboard())
        else:
            await update.message.reply_text("✅ Saved. (points already granted)", reply_markup=main_menu_keyboard())
        return

    await update.message.reply_text("Use the menu buttons below.", reply_markup=main_menu_keyboard())

async def post_init(application: Application):
    # Telegram "Menu" button shows bot commands (like /start)
    await application.bot.set_my_commands([
        BotCommand("start", "Open main menu"),
    ])
    # Force the menu button to show command list
    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())

def run():
    if not BOT_TOKEN or not BOT_USERNAME:
        raise SystemExit("BOT_TOKEN / BOT_USERNAME is missing in .env")

    init_db()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_any_text))

    print("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    run()
