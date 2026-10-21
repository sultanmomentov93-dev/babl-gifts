import os, json, sqlite3, time, hmac, hashlib
from urllib.parse import parse_qsl
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).parent
DB_PATH = os.getenv("DATABASE_PATH", str(BASE / "babl_gifts.db"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ALLOW_DEMO_AUTH = os.getenv("ALLOW_DEMO_AUTH", "true").lower() == "true"

app = FastAPI(title="Babl Gifts MVP")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

ITEMS = {
    "blue_cap": {"name": "Голубая кепка", "emoji": "🧢", "rarity": "Обычный"},
    "bubble_hoodie": {"name": "Пузырьковая худи", "emoji": "🧥", "rarity": "Редкий"},
    "golden_crown": {"name": "Корона Бабла", "emoji": "👑", "rarity": "Эпический"},
    "star_sneakers": {"name": "Звёздные кроссовки", "emoji": "👟", "rarity": "Редкий"},
}

def connect():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS players (
            user_id TEXT PRIMARY KEY, username TEXT NOT NULL DEFAULT '',
            coins INTEGER NOT NULL DEFAULT 100, last_daily INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS inventory (
            user_id TEXT NOT NULL, item_id TEXT NOT NULL, quantity INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(user_id,item_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            kind TEXT NOT NULL, amount INTEGER NOT NULL, created_at INTEGER NOT NULL)""")
init_db()

def validate_init_data(init_data: str):
    if not BOT_TOKEN:
        raise HTTPException(503, "BOT_TOKEN не настроен: серверная проверка Telegram пока недоступна")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "Нет hash в Telegram initData")
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(401, "Неверная подпись Telegram initData")
    try:
        user = json.loads(pairs["user"])
        if not user.get("id"):
            raise ValueError()
        return str(user["id"]), user.get("username") or user.get("first_name") or "Игрок"
    except Exception:
        raise HTTPException(401, "Не удалось прочитать пользователя Telegram")

def get_player(authorization: Optional[str]):
    if authorization and authorization.startswith("tma "):
        init_data = authorization[4:]
        uid, username = validate_init_data(init_data)
    elif ALLOW_DEMO_AUTH:
        uid, username = "demo-android-user", "Игрок"
    else:
        raise HTTPException(401, "Открой игру через Telegram")
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO players(user_id, username, coins, created_at) VALUES(?,?,100,?)",
                  (uid, username, int(time.time())))
        c.execute("UPDATE players SET username=? WHERE user_id=?", (username, uid))
        row = c.execute("SELECT * FROM players WHERE user_id=?", (uid,)).fetchone()
    return dict(row)

@app.get("/", response_class=HTMLResponse)
def home():
    return (BASE / "static" / "index.html").read_text(encoding="utf-8")

@app.get("/api/state")
def state(authorization: Optional[str] = Header(default=None)):
    p = get_player(authorization)
    with connect() as c:
        rows = c.execute("SELECT item_id, quantity FROM inventory WHERE user_id=? ORDER BY item_id", (p["user_id"],)).fetchall()
    inventory = [{"id": r["item_id"], **ITEMS[r["item_id"]], "quantity": r["quantity"]} for r in rows if r["item_id"] in ITEMS]
    now = int(time.time())
    return {"player": {"name": p["username"], "coins": p["coins"]}, "inventory": inventory,
            "daily_available": now - p["last_daily"] >= 86400, "items": ITEMS}

@app.post("/api/daily")
def daily(authorization: Optional[str] = Header(default=None)):
    p = get_player(authorization)
    now = int(time.time())
    with connect() as c:
        row = c.execute("SELECT last_daily FROM players WHERE user_id=?", (p["user_id"],)).fetchone()
        if now - row["last_daily"] < 86400:
            raise HTTPException(400, "Бонус уже получен. Возвращайся завтра!")
        c.execute("UPDATE players SET coins=coins+50, last_daily=? WHERE user_id=?", (now, p["user_id"]))
        c.execute("INSERT INTO transactions(user_id,kind,amount,created_at) VALUES(?,?,?,?)",
                  (p["user_id"], "daily_bonus", 50, now))
    return {"ok": True, "message": "Бабл дарит тебе +50 монет!"}

@app.post("/api/open-box")
def open_box(authorization: Optional[str] = Header(default=None)):
    p = get_player(authorization)
    price = 30
    # A secure, server-side weighted roll; this is only for virtual items.
    import secrets
    roll = secrets.randbelow(100)
    item_id = "blue_cap" if roll < 55 else "star_sneakers" if roll < 82 else "bubble_hoodie" if roll < 97 else "golden_crown"
    now = int(time.time())
    with connect() as c:
        row = c.execute("SELECT coins FROM players WHERE user_id=?", (p["user_id"],)).fetchone()
        if row["coins"] < price:
            raise HTTPException(400, "Не хватает монет. Забери ежедневный бонус!")
        c.execute("UPDATE players SET coins=coins-? WHERE user_id=?", (price, p["user_id"]))
        c.execute("""INSERT INTO inventory(user_id,item_id,quantity) VALUES(?,?,1)
                     ON CONFLICT(user_id,item_id) DO UPDATE SET quantity=quantity+1""", (p["user_id"], item_id))
        c.execute("INSERT INTO transactions(user_id,kind,amount,created_at) VALUES(?,?,?,?)",
                  (p["user_id"], "open_box", -price, now))
    return {"ok": True, "item": {"id": item_id, **ITEMS[item_id]}}

@app.get("/api/health")
def health():
    return {"ok": True, "app": "Babl Gifts"}
