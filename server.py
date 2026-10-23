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

BUBBLE_PACKS = {
    "100": {"coins": 100, "stars": 10},
    "500": {"coins": 500, "stars": 50},
    "1000": {"coins": 1000, "stars": 100},
    "2500": {"coins": 2500, "stars": 250},
    "5000": {"coins": 5000, "stars": 500},
    "10000": {"coins": 10000, "stars": 1000},
}
class BubblePurchase(BaseModel):
    pack_id: str

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
        c.execute("""CREATE TABLE IF NOT EXISTS star_payments (
            telegram_payment_charge_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            amount INTEGER NOT NULL,
            coins INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        )""")
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




@app.post("/api/buy-bubbles")
async def buy_bubbles(
    body: BubblePurchase,
    authorization: Optional[str] = Header(default=None)
):
    p = get_player(authorization)

    if p["user_id"] == "demo-android-user":
        raise HTTPException(
            400,
            "Открой игру через Telegram, чтобы покупать Пузырьки."
        )

    if not BOT_TOKEN:
        raise HTTPException(503, "BOT_TOKEN не настроен")

    pack = BUBBLE_PACKS.get(body.pack_id)

    if not pack:
        raise HTTPException(400, "Неизвестный пакет Пузырьков")

    payload = f"bubbles_{body.pack_id}:{p['user_id']}"

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/createInvoiceLink",
            json={
                "title": f"{pack['coins']} Пузырьков",
                "description": f"Пакет из {pack['coins']} игровых Пузырьков",
                "payload": payload,
                "provider_token": "",
                "currency": "XTR",
                "prices": [
                    {
                        "label": f"{pack['coins']} Пузырьков",
                        "amount": pack["stars"]
                    }
                ]
            }
        )
        data = response.json()

    if not response.is_success or not data.get("ok"):
        raise HTTPException(
            502,
            data.get("description", "Не удалось создать счёт Telegram")
        )

    return {
        "ok": True,
        "invoice_link": data["result"],
        "coins": pack["coins"],
        "stars": pack["stars"]
    }
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

@app.get("/api/admin/stars-balance")
async def admin_stars_balance(
    authorization: Optional[str] = Header(default=None)
):
    admin_id = os.getenv("ADMIN_ID", "").strip()

    if not admin_id:
        raise HTTPException(503, "ADMIN_ID не настроен")

    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(401, "Открой игру через Telegram")

    uid, _ = validate_init_data(authorization[4:])

    if uid != admin_id:
        raise HTTPException(403, "Доступ только для администратора")

    if not BOT_TOKEN:
        raise HTTPException(503, "BOT_TOKEN не настроен")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getMyStarBalance"
            )
            data = response.json()

        if not response.is_success or not data.get("ok"):
            raise HTTPException(
                502,
                data.get("description", "Не удалось проверить баланс Stars")
            )

        return {
            "ok": True,
            "balance": data["result"]
        }

    except httpx.HTTPError:
        raise HTTPException(502, "Ошибка связи с Telegram")
 
@app.get("/api/telegram/gifts")
async def telegram_available_gifts(
    authorization: Optional[str] = Header(default=None)
):
    admin_id = os.getenv("ADMIN_ID", "").strip()

    if not admin_id:
        raise HTTPException(503, "ADMIN_ID не настроен")

    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(401, "Открой игру через Telegram")

    # Проверяем подлинность данных Telegram.
    uid, _ = validate_init_data(authorization[4:])

    if uid != admin_id:
        raise HTTPException(403, "Доступ только для администратора")

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getAvailableGifts"

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(url)
            response.raise_for_status()
            result = response.json()
    except httpx.HTTPError:
        raise HTTPException(502, "Ошибка связи с Telegram")

    if not result.get("ok"):
        raise HTTPException(502, "Telegram не смог вернуть каталог подарков")

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
    
class GiftStatusUpdate(BaseModel):
    gift_id: int
    status: str


class SendTelegramGift(BaseModel):
    gift_id: int
    telegram_gift_id: str


@app.get("/api/admin/telegram-gifts")
async def admin_telegram_gifts(
    authorization: Optional[str] = Header(default=None)
):
    admin_id = os.getenv("ADMIN_ID", "").strip()

    if not admin_id:
        raise HTTPException(503, "ADMIN_ID не настроен")

    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(401, "Открой игру через Telegram")

    uid, _ = validate_init_data(authorization[4:])

    if str(uid) != admin_id:
        raise HTTPException(403, "Доступ только для администратора")

    if not BOT_TOKEN:
        raise HTTPException(503, "BOT_TOKEN не настроен")

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getAvailableGifts"
        )
        data = response.json()

    if not response.is_success or not data.get("ok"):
        raise HTTPException(
            502,
            "Не удалось загрузить каталог подарков Telegram"
        )

    gifts = []
    for gift in data.get("result", {}).get("gifts", []):
        sticker = gift.get("sticker", {})
        gifts.append({
            "id": str(gift.get("id", "")),
            "star_count": gift.get("star_count", 0),
            "sticker_file_id": sticker.get("file_id", ""),
            "emoji": sticker.get("emoji", "🎁"),
            "remaining_count": gift.get("remaining_count"),
            "total_count": gift.get("total_count")
        })

    return {"ok": True, "gifts": gifts}


@app.post("/api/admin/gifts/send")
async def send_telegram_gift(
    body: SendTelegramGift,
    authorization: Optional[str] = Header(default=None)
):
    admin_id = os.getenv("ADMIN_ID", "").strip()

    if not admin_id:
        raise HTTPException(503, "ADMIN_ID не настроен")

    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(401, "Открой игру через Telegram")

    uid, _ = validate_init_data(authorization[4:])

    if str(uid) != admin_id:
        raise HTTPException(403, "Доступ только для администратора")

    if not BOT_TOKEN:
        raise HTTPException(503, "BOT_TOKEN не настроен")

    telegram_gift_id = body.telegram_gift_id.strip()
    if not telegram_gift_id:
        raise HTTPException(400, "Не выбран подарок Telegram")

    # Проверяем заявку и получателя.
    with connect() as c:
        gift = c.execute("""
            SELECT id, user_id, name, status
            FROM pending_gifts
            WHERE id=?
        """, (body.gift_id,)).fetchone()

        if not gift:
            raise HTTPException(404, "Заявка не найдена")

        if gift["status"] != "approved":
            raise HTTPException(
                400,
                "Сначала одобри заявку. Отправлять можно только одобренные подарки."
            )

        player_id = gift["user_id"]
        gift_name = gift["name"]

    # Telegram user_id должен быть числовым.
    try:
        telegram_user_id = int(player_id)
    except (TypeError, ValueError):
        raise HTTPException(
            400,
            "У этой заявки нет настоящего Telegram ID. "
            "Демо-аккаунту отправить подарок нельзя."
        )

    # Проверяем, что выбранный ID действительно есть в каталоге Telegram.
    async with httpx.AsyncClient(timeout=20) as client:
        catalog_response = await client.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getAvailableGifts"
        )

        if not catalog_response.is_success:
            raise HTTPException(502, "Не удалось проверить каталог Telegram")

        catalog = catalog_response.json()
        if not catalog.get("ok"):
            raise HTTPException(502, "Telegram не вернул каталог подарков")

        available_ids = {
            str(item.get("id"))
            for item in catalog.get("result", {}).get("gifts", [])
        }

        if telegram_gift_id not in available_ids:
            raise HTTPException(
                400,
                "Такого подарка сейчас нет в доступном каталоге Telegram"
            )

        # Отправляем настоящий подарок.
        response = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendGift",
            json={
                "user_id": telegram_user_id,
                "gift_id": telegram_gift_id
            }
        )

        result = response.json()

    if not response.is_success or result.get("ok") is not True:
        # Заявка остаётся одобренной, чтобы можно было разобраться с ошибкой.
        raise HTTPException(
            502,
            "Telegram не подтвердил отправку: "
            + str(result.get("description", "неизвестная ошибка"))
        )

    # Меняем статус только после подтверждения Telegram.
    with connect() as c:
        current = c.execute(
            "SELECT status FROM pending_gifts WHERE id=?",
            (body.gift_id,)
        ).fetchone()

        if not current or current["status"] != "approved":
            raise HTTPException(
                409,
                "Подарок отправлен Telegram, но статус заявки изменился. "
                "Проверь заявку вручную."
            )

        c.execute(
            "UPDATE pending_gifts SET status='withdrawn' "
            "WHERE id=? AND status='approved'",
            (body.gift_id,)
        )

    # Уведомляем игрока после подтверждённой отправки.
    notification_sent = False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            notice = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": telegram_user_id,
                    "text": (
                        f"🎁 Подарок «{gift_name}» отправлен на твой аккаунт Telegram!\n\n"
                        "Проверь свои подарки в Telegram."
                    )
                }
            )
            notice_data = notice.json()
            notification_sent = (
                notice.is_success and notice_data.get("ok") is True
            )
    except Exception as exc:
        print("Ошибка уведомления игрока:", exc)

    return {
        "ok": True,
        "gift_id": body.gift_id,
        "status": "withdrawn",
        "telegram_gift_id": telegram_gift_id,
        "notification_sent": notification_sent,
        "message": "Telegram подтвердил отправку подарка"
    }
@app.get("/api/admin/gifts")
def admin_list_gifts(
    authorization: Optional[str] = Header(default=None)
):
    admin_id = os.getenv("ADMIN_ID", "").strip()

    if not admin_id:
        raise HTTPException(503, "ADMIN_ID не настроен")

    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(401, "Открой игру через Telegram")

    uid, _ = validate_init_data(authorization[4:])

    if uid != admin_id:
        raise HTTPException(403, "Доступ только для администратора")

    with connect() as c:
        rows = c.execute("""
            SELECT id, user_id, prize_id, name, emoji, status, created_at
            FROM pending_gifts
            ORDER BY id DESC
        """).fetchall()

    return {"ok": True, "gifts": [dict(row) for row in rows]}



@app.post("/api/admin/gifts/status")
def admin_update_gift_status(
    body: GiftStatusUpdate,
    authorization: Optional[str] = Header(default=None)
):
    admin_id = os.getenv("ADMIN_ID", "").strip()

    if not admin_id:
        raise HTTPException(503, "ADMIN_ID не настроен")

    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(401, "Открой игру через Telegram")

    uid, _ = validate_init_data(authorization[4:])

    if uid != admin_id:
        raise HTTPException(403, "Доступ только для администратора")
        
    if body.status != "approved":
        raise HTTPException(
            400,
            "Изменять статус можно только на «Одобрен». "
            "Для отправки подарка используй отдельную функцию отправки."
       )

    # Проверяем заявку и сохраняем новый статус.
    with connect() as c:
        gift = c.execute("""
            SELECT id, user_id, name, status
            FROM pending_gifts
            WHERE id=?
        """, (body.gift_id,)).fetchone()

        if not gift:
            raise HTTPException(404, "Подарок не найден")

        if body.status == "approved" and gift["status"] != "pending":
            raise HTTPException(
                400,
                "Одобрить можно только ожидающий обработки подарок"
            )

        if body.status == "withdrawn" and gift["status"] != "approved":
            raise HTTPException(
                400,
                "Отметить выведенным можно только одобренный подарок"
            )

        c.execute(
            "UPDATE pending_gifts SET status=? WHERE id=?",
            (body.status, body.gift_id)
        )

        player_id = gift["user_id"]
        gift_name = gift["name"]

    # Готовим сообщение игроку.
    if body.status == "approved":
        message = (
            f"✅ Твой подарок «{gift_name}» одобрен администратором!\n\n"
            "Следи за обновлениями в игре «Бабл Подарки»."
        )
    else:
        message = (
            f"🎁 Подарок «{gift_name}» отмечен администратором как отправленный.\n\n"
            "Проверь свой аккаунт Telegram!"
        )

    # Отправляем уведомление. Ошибка Telegram не отменяет статус.
    notification_sent = False
    bot_token = BOT_TOKEN.strip()

    if bot_token:
        try:
            with httpx.Client(timeout=10) as client:
                response = client.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={
                        "chat_id": player_id,
                        "text": message
                    }
                )
                result = response.json()
                notification_sent = (
                    response.is_success and result.get("ok") is True
                )
        except Exception as exc:
            print("Ошибка уведомления игрока:", exc)

    if not notification_sent:
        print(
            f"Не удалось отправить уведомление игроку {player_id} "
            f"о подарке {body.gift_id}"
        )

    return {
        "ok": True,
        "gift_id": body.gift_id,
        "status": body.status,
        "notification_sent": notification_sent
    }


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()

    async with httpx.AsyncClient(timeout=15) as client:

        
        # 1. Проверяем платёж перед подтверждением.
        query = update.get("pre_checkout_query")

        if query:
            payload = query.get("invoice_payload", "")
            user_id = str(query.get("from", {}).get("id", ""))

            try:
                pack_part, payload_user_id = payload.split(":", 1)
                pack_id = pack_part.removeprefix("bubbles_")
                pack = BUBBLE_PACKS.get(pack_id)
            except ValueError:
                pack = None
                payload_user_id = ""

            valid = (
                pack is not None
                and payload_user_id == user_id
                and query.get("currency") == "XTR"
                and query.get("total_amount") == pack["stars"]
            )

            response = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/answerPreCheckoutQuery",
                json={
                    "pre_checkout_query_id": query["id"],
                    "ok": valid,
                    **(
                        {}
                        if valid
                        else {
                            "error_message":
                                "Счёт недействителен. Создай новый."
                        }
                    )
                }
            )
            response.raise_for_status()
            return {"ok": True}

        
        # 2. Telegram подтвердил успешную оплату.
        message = update.get("message", {})
        payment = message.get("successful_payment")

        if payment:
            telegram_user = message.get("from", {})
            user_id = str(telegram_user.get("id", ""))
            payload = payment.get("invoice_payload", "")
            charge_id = payment.get("telegram_payment_charge_id", "")
            username = (
                telegram_user.get("username")
                or telegram_user.get("first_name")
                or "Игрок"
            )
            now = int(time.time())

            try:
                pack_part, payload_user_id = payload.split(":", 1)
                pack_id = pack_part.removeprefix("bubbles_")
                pack = BUBBLE_PACKS.get(pack_id)
            except ValueError:
                pack = None
                payload_user_id = ""

            # Проверяем пакет, пользователя, валюту и цену.
            if (
                pack is None
                or payload_user_id != user_id
                or payment.get("currency") != "XTR"
                or payment.get("total_amount") != pack["stars"]
                or not charge_id
            ):
                return {"ok": True}

            # Начисляем пакет строго один раз.
            with connect() as c:
                c.execute("BEGIN IMMEDIATE")

                existing = c.execute(
                    """
                    SELECT 1 FROM star_payments
                    WHERE telegram_payment_charge_id = ?
                    """,
                    (charge_id,)
                ).fetchone()

                if not existing:
                    player = c.execute(
                        "SELECT user_id FROM players WHERE user_id = ?",
                        (user_id,)
                    ).fetchone()

                    if not player:
                        c.execute(
                            """
                            INSERT INTO players
                                (user_id, username, coins, last_daily, created_at)
                            VALUES (?, ?, 100, 0, ?)
                            """,
                            (user_id, username, now)
                        )

                    c.execute(
                        """
                        INSERT INTO star_payments
                            (telegram_payment_charge_id, user_id,
                             amount, coins, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (charge_id, user_id, pack["stars"], pack["coins"], now)
                    )

                    c.execute(
                        "UPDATE players SET coins = coins + ? WHERE user_id = ?",
                        (pack["coins"], user_id)
                    )

                    c.execute(
                        """
                        INSERT INTO transactions
                            (user_id, kind, amount, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            user_id,
                            "stars_purchase",
                            pack["coins"],
                            now
                        )
                    )

            # Сообщаем игроку о покупке.
            chat_id = message.get("chat", {}).get("id")
            if chat_id:
                await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": (
                            f"🎉 Оплата получена! Начислено "
                            f"{pack['coins']:,} Пузырьков за "
                            f"{pack['stars']:,} Stars."
                            .replace(",", " ")
                            + "\nВозвращайся в «Бабл Подарки»!"
                        )
                    }
                )

            return {"ok": True}
            
        # 3. Обычная команда /start — оставляем как раньше.
        text = message.get("text", "")
        chat = message.get("chat", {})
        chat_id = chat.get("id")

        if text == "/start" and chat_id:
            keyboard = {
                "inline_keyboard": [[
                    {
                        "text": "🎁 Открыть Бабл Подарки",
                        "web_app": {
                            "url": "https://babl-gifts.onrender.com"
                        }
                    }
                ]]
            }

            response = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text":
                        "Привет! 👋 Добро пожаловать в «Бабл Подарки»!\n\n"
                        "Открывай виртуальные коробки, собирай подарки "
                        "и забирай ежедневные монеты. 🎁",
                    "reply_markup": keyboard
                }
            )
            response.raise_for_status()

    return {"ok": True}

@app.on_event("startup")

async def register_telegram_webhook():
    if not BOT_TOKEN:
        print("BOT_TOKEN не настроен — Telegram webhook не подключён")
        return

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                "https://api.telegram.org/bot" + BOT_TOKEN + "/setWebhook",
                json={
                    "url": "https://babl-gifts.onrender.com/telegram/webhook",
                    "allowed_updates": ["message", "pre_checkout_query"]
                }
            )

            print("Результат подключения Telegram:", response.text)
            response.raise_for_status()

    except Exception as exc:
        print("Не удалось подключить Telegram webhook:", exc)
