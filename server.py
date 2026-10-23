import os, json, sqlite3, time, hmac, hashlib, secrets
from urllib.parse import parse_qsl
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import httpx

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

# Призы рулетки. Подарки сохраняются в pending_gifts («На вывод»),
# а пузырьки начисляются на баланс внутри одной транзакции SQLite.
ROULETTE_PRIZES = [
    {"id": "cake", "name": "Торт", "emoji": "🎂", "kind": "gift", "weight": 4},
    {"id": "bouquet", "name": "Букет", "emoji": "💐", "kind": "gift", "weight": 5},
    {"id": "rocket", "name": "Ракета", "emoji": "🚀", "kind": "gift", "weight": 1},
    {"id": "champagne", "name": "Шампанское", "emoji": "🍾", "kind": "gift", "weight": 2},
    {"id": "gift", "name": "Подарок", "emoji": "🎁", "kind": "gift", "weight": 8},
    {"id": "rose", "name": "Роза", "emoji": "🌹", "kind": "gift", "weight": 10},
    {"id": "heart", "name": "Сердце", "emoji": "💝", "kind": "gift", "weight": 8},
    {"id": "teddy_bear", "name": "Мишка", "emoji": "🧸", "kind": "gift", "weight": 5},
    {"id": "bubbles_250", "name": "+250 Пузырьков", "emoji": "🫧", "kind": "coins", "amount": 250, "weight": 1},
    {"id": "bubbles_100", "name": "+100 Пузырьков", "emoji": "🫧", "kind": "coins", "amount": 100, "weight": 4},
    {"id": "bubbles_50", "name": "+50 Пузырьков", "emoji": "🫧", "kind": "coins", "amount": 50, "weight": 9},
    {"id": "bubbles_25", "name": "+25 Пузырьков", "emoji": "🫧", "kind": "coins", "amount": 25, "weight": 15},
    {"id": "bubbles_10", "name": "+10 Пузырьков", "emoji": "🫧", "kind": "coins", "amount": 10, "weight": 20},
    {"id": "empty", "name": "Пусто", "emoji": "➖", "kind": "empty", "weight": 8},
]
PRIZE_BY_ID = {p["id"]: p for p in ROULETTE_PRIZES}

def connect():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=15000")
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
        c.execute("""CREATE TABLE IF NOT EXISTS pending_gifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL, prize_id TEXT NOT NULL, name TEXT NOT NULL,
            emoji TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL)""")
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
        uid, username = validate_init_data(authorization[4:])
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
        gifts = c.execute("""SELECT id, prize_id, name, emoji, status, created_at
                             FROM pending_gifts WHERE user_id=? ORDER BY id DESC""", (p["user_id"],)).fetchall()
    inventory = [{"id": r["item_id"], **ITEMS[r["item_id"]], "quantity": r["quantity"]} for r in rows if r["item_id"] in ITEMS]
    pending_gifts = [dict(r) for r in gifts]
    now = int(time.time())
    return {"player": {"name": p["username"], "coins": p["coins"]}, "inventory": inventory,
            "pending_gifts": pending_gifts, "daily_available": now - p["last_daily"] >= 86400,
            "items": ITEMS}

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

class RouletteSpin(BaseModel):
    mode: str = "free"

@app.post("/api/roulette/spin")
def roulette_spin(body: RouletteSpin, authorization: Optional[str] = Header(default=None)):
    p = get_player(authorization)
    if body.mode not in ("free", "paid"):
        raise HTTPException(400, "Неизвестный режим рулетки")

    now = int(time.time())
    with connect() as c:
        # BEGIN IMMEDIATE предотвращает параллельные списания/начисления.
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT coins FROM players WHERE user_id=?", (p["user_id"],)).fetchone()
        if body.mode == "paid" and row["coins"] < 100:
            raise HTTPException(400, "Недостаточно Пузырьков. Нужно 100 🫧")

        if body.mode == "paid":
            c.execute("UPDATE players SET coins=coins-100 WHERE user_id=?", (p["user_id"],))
            c.execute("INSERT INTO transactions(user_id,kind,amount,created_at) VALUES(?,?,?,?)",
                      (p["user_id"], "roulette_paid", -100, now))

        prize = secrets.choice(
            [prize for prize in ROULETTE_PRIZES for _ in range(prize["weight"])]
        )
        if prize["kind"] == "coins":
            amount = prize["amount"]
            c.execute("UPDATE players SET coins=coins+? WHERE user_id=?", (amount, p["user_id"]))
            c.execute("INSERT INTO transactions(user_id,kind,amount,created_at) VALUES(?,?,?,?)",
                      (p["user_id"], "roulette_prize:" + prize["id"], amount, now))
        elif prize["kind"] == "gift":
            gift_cursor = c.execute("""INSERT INTO pending_gifts(user_id,prize_id,name,emoji,status,created_at)
                                        VALUES(?,?,?,?, 'pending', ?)""",
                                     (p["user_id"], prize["id"], prize["name"], prize["emoji"], now))
            gift_id = gift_cursor.lastrowid
            c.execute("INSERT INTO transactions(user_id,kind,amount,created_at) VALUES(?,?,0,?)",
                      (p["user_id"], "roulette_gift:" + prize["id"], now))
        else:
            c.execute("INSERT INTO transactions(user_id,kind,amount,created_at) VALUES(?,?,0,?)",
                      (p["user_id"], "roulette_empty", now))

        updated = c.execute("SELECT coins FROM players WHERE user_id=?", (p["user_id"],)).fetchone()
        if prize["kind"] != "gift":
            gift_id = None

    return {
        "ok": True,
        "prize": {"id": prize["id"], "name": prize["name"], "emoji": prize["emoji"],
                  "kind": prize["kind"], "amount": prize.get("amount", 0), "gift_id": gift_id},
        "coins": updated["coins"],
        "message": ("Начислено +" + str(prize["amount"]) + " Пузырьков!" if prize["kind"] == "coins"
                    else "Подарок добавлен в раздел «На вывод»!" if prize["kind"] == "gift"
                    else "В этот раз пусто. Попробуй ещё раз!")
    }

@app.get("/api/withdrawals")
def withdrawals(authorization: Optional[str] = Header(default=None)):
    p = get_player(authorization)
    with connect() as c:
        rows = c.execute("""SELECT id, prize_id, name, emoji, status, created_at
                            FROM pending_gifts WHERE user_id=? ORDER BY id DESC""", (p["user_id"],)).fetchall()
    return {"ok": True, "gifts": [dict(r) for r in rows]}

@app.get("/api/health")
def health():
    return {"ok": True, "app": "Babl Gifts"}
    
@app.get("/api/telegram/gifts")
async def telegram_available_gifts(
    authorization: Optional[str] = Header(default=None)
):
    # Не разрешаем демонстрационному пользователю обращаться к каталогу.
    p = get_player(authorization)

    # Проверяем, что запрос действительно пришёл от администратора.
    admin_id = os.getenv("ADMIN_ID", "")
    if not admin_id or p["user_id"] != admin_id:
        raise HTTPException(403, "Доступ только для администратора")

    if not BOT_TOKEN:
        raise HTTPException(503, "BOT_TOKEN не настроен")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getAvailableGifts"

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url)
            response.raise_for_status()
            result = response.json()
    except httpx.HTTPError:
        raise HTTPException(502, "Не удалось связаться с Telegram")

    if not result.get("ok"):
        raise HTTPException(502, "Telegram не вернул список подарков")

    gifts = result.get("result", {}).get("gifts", [])

    return {
        "ok": True,
        "count": len(gifts),
        "gifts": [
            {
                "id": gift.get("id"),
                "star_count": gift.get("star_count"),
                "remaining_count": gift.get("remaining_count"),
                "total_count": gift.get("total_count"),
            }
            for gift in gifts
        ],
    }

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    message = update.get("message", {})
    text = message.get("text", "")
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    if text == "/start" and chat_id:
        keyboard = {"inline_keyboard": [[{"text": "🎁 Открыть Бабл Подарки",
                                          "web_app": {"url": "https://babl-gifts.onrender.com"}}]]}
        payload = {"chat_id": chat_id,
                   "text": "Привет! 👋 Добро пожаловать в «Бабл Подарки»!\n\nОткрывай виртуальные коробки, собирай подарки и забирай ежедневные монеты. 🎁",
                   "reply_markup": keyboard}
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload)
            response.raise_for_status()
    return {"ok": True}

@app.on_event("startup")
async def register_telegram_webhook():
    if not BOT_TOKEN:
        print("BOT_TOKEN не настроен — Telegram webhook не подключён")
        return
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post("https://api.telegram.org/bot" + BOT_TOKEN + "/setWebhook",
                                         json={"url": "https://babl-gifts.onrender.com/telegram/webhook"})
            print("Результат подключения Telegram:", response.text)
            response.raise_for_status()
    except Exception as exc:
        print("Не удалось подключить Telegram webhook:", exc)
