import os, json, logging, urllib.parse, re
from datetime import datetime
from contextlib import contextmanager
from flask import Flask, request
import requests
import pg8000

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)
app = Flask(__name__)

BOT_TOKEN        = os.environ["TELEGRAM_BOT_TOKEN"]
WEBHOOK_URL      = os.environ["WEBHOOK_URL"]
DATABASE_URL     = os.environ["DATABASE_URL"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
ADMIN_ID         = int(os.environ.get("ADMIN_ID", "0"))

API_BASE     = f"https://api.telegram.org/bot{BOT_TOKEN}"
CLAUDE_URL   = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# Авторизованные пользователи (водители) — заполняется через /driver_add
# Загружается из БД динамически
ADMIN_IDS = [ADMIN_ID]  # только список adminов

REGIONS = {
    "Buxoro":            int(os.environ.get("CHAT_BUXORO", "-5274572946")),
    "Farg'ona":          int(os.environ.get("CHAT_FARGONA", "0")),
    "Samarqand":         int(os.environ.get("CHAT_SAMARQAND", "-5171165315")),
    "Toshkent viloyati": int(os.environ.get("CHAT_TOSHKENT_VIL", "0")),
    "Toshkent shahar":   int(os.environ.get("CHAT_TOSHKENT_SHR", "-5277916866")),
    "Namangan":          int(os.environ.get("CHAT_NAMANGAN", "0")),
    "Navoiy":            int(os.environ.get("CHAT_NAVOIY", "-5275311328")),
    "Jizzax":            int(os.environ.get("CHAT_JIZZAX", "0")),
    "Qashqadaryo":       int(os.environ.get("CHAT_QASHQA", "0")),
    "Andijon":           int(os.environ.get("CHAT_ANDIJON", "0")),
    "Xorazm":            int(os.environ.get("CHAT_XORAZM", "0")),
    "Sirdaryo":          int(os.environ.get("CHAT_SIRDARYO", "0")),
    "Surxondaryo":       int(os.environ.get("CHAT_SURXON", "0")),
    "Qirg'iziston":      int(os.environ.get("CHAT_KIRGIZ", "0")),
    "Qoraqalpog'iston":  int(os.environ.get("CHAT_QORAQALP", "0")),
}
REGION_NAMES = list(REGIONS.keys())

# ─── DB ───────────────────────────────────────────────────────────────────────
def parse_db_url(url):
    r = urllib.parse.urlparse(url)
    return dict(host=r.hostname, port=r.port or 5432,
                database=r.path.lstrip("/"), user=r.username, password=r.password)

@contextmanager
def get_db():
    p = parse_db_url(DATABASE_URL)
    conn = pg8000.connect(host=p["host"], port=p["port"], database=p["database"],
                          user=p["user"], password=p["password"], ssl_context=None)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def qall(conn, sql, params=None):
    cur = conn.cursor()
    cur.execute(sql, params or [])
    if cur.description:
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    return []

def qone(conn, sql, params=None):
    rows = qall(conn, sql, params)
    return rows[0] if rows else None

def qrun(conn, sql, params=None):
    cur = conn.cursor()
    cur.execute(sql, params or [])
    return cur.rowcount

def init_db():
    with get_db() as conn:
        qrun(conn, """CREATE TABLE IF NOT EXISTS orders (
            order_id    SERIAL PRIMARY KEY,
            order_num   INT NOT NULL,
            yuk         TEXT DEFAULT '',
            qayerdan    TEXT DEFAULT '',
            qayerga     TEXT DEFAULT '',
            ogirlik     TEXT DEFAULT '',
            narx        TEXT DEFAULT '',
            yuklash_san TEXT DEFAULT '',
            telefon     TEXT DEFAULT '',
            mashina     TEXT DEFAULT '',
            region      TEXT DEFAULT '',
            status      TEXT DEFAULT 'yangi',
            driver_id   BIGINT DEFAULT 0,
            driver_name TEXT DEFAULT '',
            chat_msg_id BIGINT DEFAULT 0,
            created_at  TIMESTAMP DEFAULT NOW(),
            updated_at  TIMESTAMP DEFAULT NOW()
        )""")
        # Add mashina column if not exists (for existing DBs)
        try:
            qrun(conn, "ALTER TABLE orders ADD COLUMN IF NOT EXISTS mashina TEXT DEFAULT ''")
        except: pass
        qrun(conn, """CREATE TABLE IF NOT EXISTS conversations (
            user_id     BIGINT PRIMARY KEY,
            role        TEXT DEFAULT 'client',
            history     TEXT DEFAULT '[]',
            order_data  TEXT DEFAULT '{}',
            updated_at  TIMESTAMP DEFAULT NOW()
        )""")
        qrun(conn, """CREATE TABLE IF NOT EXISTS counters (
            name  TEXT PRIMARY KEY,
            value INT DEFAULT 0
        )""")
        qrun(conn, "INSERT INTO counters (name,value) VALUES ('order_num',800) ON CONFLICT DO NOTHING")
        qrun(conn, """CREATE TABLE IF NOT EXISTS drivers (
            user_id     BIGINT PRIMARY KEY,
            user_label  TEXT DEFAULT '',
            region      TEXT DEFAULT '',
            status      TEXT DEFAULT 'active',
            created_at  TIMESTAMP DEFAULT NOW()
        )""")
    logger.info("[DB] Tables ready")

def next_order_num():
    with get_db() as conn:
        qrun(conn, "UPDATE counters SET value=value+1 WHERE name='order_num'")
        return qone(conn, "SELECT value FROM counters WHERE name='order_num'")["value"]

def get_state(user_id):
    with get_db() as conn:
        row = qone(conn, "SELECT state, data FROM user_states WHERE user_id=%s", [user_id])
        if row:
            return row["state"], json.loads(row["data"]) if row["data"] else {}
        return "", {}

def set_state(user_id, state, data=None):
    with get_db() as conn:
        qrun(conn, """INSERT INTO user_states (user_id, state, data, updated_at)
            VALUES (%s,%s,%s,NOW()) ON CONFLICT (user_id) DO UPDATE
            SET state=%s, data=%s, updated_at=NOW()""",
            [user_id, state, json.dumps(data or {}), state, json.dumps(data or {})])

def clear_state(user_id):
    with get_db() as conn:
        qrun(conn, "DELETE FROM user_states WHERE user_id=%s", [user_id])

# ─── Driver management ───────────────────────────────────────────────────────
def is_driver(user_id):
    """Проверяет зарегистрирован ли пользователь как водитель"""
    with get_db() as conn:
        row = qone(conn, "SELECT user_id FROM drivers WHERE user_id=%s AND status='active'", [user_id])
        return row is not None

def register_driver(user_id, user_label, region=""):
    with get_db() as conn:
        qrun(conn, """INSERT INTO drivers (user_id, user_label, region)
            VALUES (%s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET
            user_label=%s, region=%s, status='active'""",
            [user_id, user_label, region, user_label, region])

def get_all_drivers():
    with get_db() as conn:
        return qall(conn, "SELECT * FROM drivers WHERE status='active' ORDER BY created_at DESC")

# ─── Conversation ─────────────────────────────────────────────────────────────
def get_conv(user_id):
    with get_db() as conn:
        row = qone(conn, "SELECT role, history, order_data FROM conversations WHERE user_id=%s", [user_id])
        if row:
            return row["role"], json.loads(row["history"]), json.loads(row["order_data"])
        return "", [], {}

def save_conv(user_id, role, history, order_data):
    if len(history) > 20:
        history = history[-20:]
    with get_db() as conn:
        qrun(conn, """INSERT INTO conversations (user_id, role, history, order_data, updated_at)
            VALUES (%s,%s,%s,%s,NOW())
            ON CONFLICT (user_id) DO UPDATE SET
            role=%s, history=%s, order_data=%s, updated_at=NOW()""",
            [user_id, role, json.dumps(history), json.dumps(order_data),
             role, json.dumps(history), json.dumps(order_data)])

def clear_conv(user_id):
    with get_db() as conn:
        qrun(conn, "DELETE FROM conversations WHERE user_id=%s", [user_id])

# ─── Claude AI ────────────────────────────────────────────────────────────────
def ask_claude(system_prompt, messages, max_tokens=600):
    try:
        resp = requests.post(CLAUDE_URL, headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }, json={
            "model": CLAUDE_MODEL,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": messages
        }, timeout=30)
        data = resp.json()
        if data.get("content"):
            return data["content"][0]["text"]
        logger.error("[Claude] Error: %s", data)
        return None
    except Exception as e:
        logger.error("[Claude] Exception: %s", e)
        return None

# ─── System prompts ───────────────────────────────────────────────────────────
CLIENT_SYSTEM = """Sen CELC Logistics kompaniyasining aqlli dispetcherisan.
Mijoz bilan o'zbek tilida oddiy va do'stona suhbat olib borasan.

Yig'ish kerak bo'lgan ma'lumotlar:
1. Yuk nomi (nima tashiladi)
2. Qayerdan (jo'natish joyi - shahar/tuman)
3. Qayerga (yetkazish joyi - shahar/tuman)
4. Og'irligi (tonna yoki kg)
5. Mashina turi - quyidagilardan birini so'ra:
   - Ref (sovutgichli, maks 24t)
   - Tent 5 o'qli (maks 24t)
   - Tent 6 o'qli (maks 25t)
   - Konteyner
   - Plashchatka
6. Taklif narxi (so'mda)
7. Yuklash sanasi
8. Bog'lanish telefoni

MUHIM QOIDALAR:
- Faqat oddiy matn yoz, hech qanday ** yoki markdown ishlatma
- Har bir savolni qisqa va do'stona so'ra
- Mashina turi so'raganda variantlarni ko'rsat
- Mijoz bir nechta ma'lumot birga bersa - barchasini qabul qil va faqat qolganlarini so'ra
- Barcha ma'lumot to'liq bo'lganda FAQAT JSON qaytар, boshqa hech narsa yozma:
{"DONE": true, "yuk": "...", "qayerdan": "...", "qayerga": "...", "ogirlik": "...", "mashina": "...", "narx": "...", "yuklash_san": "...", "telefon": "..."}
- Qisqa javob ber, 1-2 jumla yetarli"""

DRIVER_SYSTEM = """Sen CELC Logistics kompaniyasining aqlli dispetcherisan.
Haydovchi bilan o'zbek tilida oddiy suhbat olib borasan.

Mavjud regionlar: {regions}

MUHIM QOIDALAR:
- Faqat oddiy matn yoz, hech qanday ** yoki markdown ishlatma
- Haydovchi marshrut aytganda FAQAT JSON qaytar:
{{"SEARCH": true, "qayerdan": "...", "qayerga": "..."}}
- Agar haydovchi faqat bir joy aytsa (masalan "Farg'onaga"), qayerdan=bo'sh qoldir
- Boshqa savolga oddiy javob ber"""

# ─── Telegram helpers ─────────────────────────────────────────────────────────
def send_message(chat_id, text, reply_markup=None):
    if not chat_id or not text: return None
    # Убираем markdown ** из текста
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', str(text))
    payload = {"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML"}
    if reply_markup: payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(f"{API_BASE}/sendMessage", json=payload, timeout=10)
        return r.json()
    except Exception as e:
        logger.error("[TG] %s: %s", chat_id, e)
        return None

def edit_message(chat_id, message_id, text, reply_markup=None):
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', str(text))
    payload = {"chat_id": chat_id, "message_id": message_id,
               "text": text[:4096], "parse_mode": "HTML"}
    if reply_markup: payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(f"{API_BASE}/editMessageText", json=payload, timeout=10)
    except Exception as e:
        logger.error("[TG] edit: %s", e)

def answer_callback(cq_id, text=""):
    try:
        requests.post(f"{API_BASE}/answerCallbackQuery",
                      json={"callback_query_id": cq_id, "text": text}, timeout=10)
    except: pass

def get_user_label(u):
    uname = u.get("username")
    return f"@{uname}" if uname else (
        f"{u.get('first_name','')} {u.get('last_name','')}".strip() or str(u.get("id","?")))

# ─── Format order ─────────────────────────────────────────────────────────────
def format_order(order_num, yuk, qayerdan, qayerga, ogirlik, mashina, narx, yuklash_san, telefon, holat="Yangi", show_phone=True):
    emoji = "🟢" if holat == "Yangi" else "🔴" if "qabul" in holat.lower() else "✅"
    phone_line = f"📞 <b>Bog'lanish:</b> {telefon}" if show_phone else "📞 <b>Bog'lanish:</b> <i>Qabul qilgandan so'ng ko'rinadi</i>"
    mashina_line = f"🚛 <b>Mashina turi:</b> {mashina}\n" if mashina else ""
    return (
        f"📦 <b>Yangi yuk #{order_num}</b>\n\n"
        f"🗂 <b>Yuk:</b> {yuk}\n"
        f"📍 <b>Qayerdan:</b> {qayerdan}\n"
        f"📍 <b>Qayerga:</b> {qayerga}\n"
        f"⚖️ <b>Og'irlik:</b> {ogirlik}\n"
        f"{mashina_line}"
        f"💰 <b>Taklif qilinayotgan narx:</b> {narx}\n"
        f"📅 <b>Yuklash sanasi:</b> {yuklash_san}\n"
        f"{emoji} <b>Holati:</b> {holat}\n"
        f"{phone_line}"
    )

def driver_keyboard(order_id):
    return {"inline_keyboard": [[
        {"text": "✅ Qabul qilish", "callback_data": f"accept|{order_id}"}
    ]]}

def confirm_keyboard(order_id):
    return {"inline_keyboard": [[
        {"text": "✅ Yuk yetkazildi", "callback_data": f"delivered|{order_id}"},
        {"text": "⚠️ Muammo bor",     "callback_data": f"problem|{order_id}"}
    ]]}

def role_keyboard():
    return {"inline_keyboard": [[
        {"text": "📦 Yuk beruvchi (mijoz)", "callback_data": "role|client"},
        {"text": "🚚 Haydovchi",            "callback_data": "role|driver"}
    ]]}

def region_register_keyboard():
    """Клавиатура выбора региона для регистрации водителя"""
    buttons = []
    row = []
    for i, region in enumerate(REGION_NAMES):
        row.append({"text": region, "callback_data": f"reg_region|{region}"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row: buttons.append(row)
    buttons.append([{"text": "🌍 Barcha regionlar", "callback_data": "reg_region|all"}])
    return {"inline_keyboard": buttons}

# ─── Auto detect region from qayerga ──────────────────────────────────────────
def detect_region(qayerdan, qayerga):
    """Определяет регион по qayerdan (откуда забрать груз)"""
    mapping = {
        "Buxoro":            ["buxoro", "buxara"],
        "Farg'ona":          ["farg'ona", "fargona", "fergana"],
        "Samarqand":         ["samarqand", "samarkand"],
        "Toshkent viloyati": ["toshkent viloyat", "chirchiq", "angren", "olmaliq", "bekobod"],
        "Toshkent shahar":   ["toshkent"],
        "Namangan":          ["namangan"],
        "Navoiy":            ["navoiy", "navoi", "karmana"],
        "Jizzax":            ["jizzax", "jizzak"],
        "Qashqadaryo":       ["qashqa", "qarshi", "shahrisabz", "kitob"],
        "Andijon":           ["andijon", "andijan"],
        "Xorazm":            ["xorazm", "urganch", "xiva"],
        "Sirdaryo":          ["sirdaryo", "guliston", "yangiyer"],
        "Surxondaryo":       ["surxon", "termiz", "denov", "boysun"],
        "Qirg'iziston":      ["qirg'iz", "kyrgyz", "bishkek", "osh"],
        "Qoraqalpog'iston":  ["qoraqalp", "nukus", "mo'ynoq"],
    }
    # Определяем по qayerdan (откуда везут)
    qd = qayerdan.lower().strip()
    for region, keywords in mapping.items():
        for kw in keywords:
            if kw in qd:
                return region
    return "Toshkent shahar"

# ─── Send order to region chat ────────────────────────────────────────────────
def send_order_to_region(order_id, order):
    region = order["region"]
    region_chat_id = REGIONS.get(region, 0)
    # В группе телефон скрыт
    order_text_no_phone = format_order(
        order["order_num"], order["yuk"], order["qayerdan"],
        order["qayerga"], order["ogirlik"], order.get("mashina",""),
        order["narx"], order["yuklash_san"], order["telefon"], show_phone=False)

    if region_chat_id:
        result = send_message(region_chat_id, order_text_no_phone, reply_markup=driver_keyboard(order_id))
        if result and result.get("ok"):
            msg_id = result["result"]["message_id"]
            with get_db() as conn:
                qrun(conn, "UPDATE orders SET chat_msg_id=%s WHERE order_id=%s", [msg_id, order_id])
            return True
    return False

# ─── Find orders for driver ───────────────────────────────────────────────────
def find_orders_for_driver(qayerdan, qayerga):
    with get_db() as conn:
        orders = qall(conn, "SELECT * FROM orders WHERE status='yangi' ORDER BY created_at DESC LIMIT 20")
    if not orders:
        return []
    matched = []
    qd = qayerdan.lower() if qayerdan else ""
    qg = qayerga.lower() if qayerga else ""
    for o in orders:
        o_qd = (o["qayerdan"] or "").lower()
        o_qg = (o["qayerga"] or "").lower()
        match = False
        if qg:
            match = any(w in o_qg or w in o_qd for w in qg.split() if len(w) > 2)
        if not match and qd:
            match = any(w in o_qd for w in qd.split() if len(w) > 2)
        if match:
            matched.append(o)
    return matched[:5]

# ─── Handle client AI ─────────────────────────────────────────────────────────
def handle_client_message(chat_id, user_id, text, user_label):
    role, history, order_data = get_conv(user_id)
    history.append({"role": "user", "content": text})
    reply = ask_claude(CLIENT_SYSTEM, history)
    if not reply:
        send_message(chat_id, "Uzr, texnik xatolik. Qaytadan urinib ko'ring.")
        return

    try:
        json_match = re.search(r'\{[^{}]*"DONE"[^{}]*\}', reply, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            if data.get("DONE"):
                order_num = next_order_num()
                # Автоматически определяем регион
                region = detect_region(data.get("qayerdan",""), data.get("qayerga",""))

                with get_db() as conn:
                    qrun(conn, """INSERT INTO orders
                        (order_num,yuk,qayerdan,qayerga,ogirlik,mashina,narx,yuklash_san,telefon,region,status)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'yangi')""",
                        [order_num, data.get("yuk",""), data.get("qayerdan",""),
                         data.get("qayerga",""), data.get("ogirlik",""),
                         data.get("mashina",""), data.get("narx",""),
                         data.get("yuklash_san",""), data.get("telefon",""), region])
                    order = qone(conn, "SELECT * FROM orders WHERE order_num=%s", [order_num])

                order_id = order["order_id"]
                preview = format_order(
                    order_num, data.get("yuk",""), data.get("qayerdan",""),
                    data.get("qayerga",""), data.get("ogirlik",""),
                    data.get("mashina",""), data.get("narx",""),
                    data.get("yuklash_san",""), data.get("telefon",""))

                clear_conv(user_id)

                # Отправляем в региональный чат автоматически
                sent = send_order_to_region(order_id, order)

                if sent:
                    send_message(chat_id,
                        f"✅ Yuk muvaffaqiyatli joylashtirildi!\n\n{preview}\n\n"
                        f"📍 {region} chatiga yuborildi. Haydovchilar ko'rmoqda...")
                else:
                    send_message(chat_id,
                        f"✅ Yuk bazaga saqlandi!\n\n{preview}\n\n"
                        f"📍 Region: {region}\n\n"
                        f"Yangi yuk joylash uchun /yangi_yuk")

                if ADMIN_ID:
                    send_message(ADMIN_ID,
                        f"📦 Yangi yuk #{order_num}\n"
                        f"📍 {data.get('qayerdan','')} → {data.get('qayerga','')}\n"
                        f"🗂 {data.get('yuk','')} | {data.get('ogirlik','')}\n"
                        f"🌍 Region: {region}")
                return
    except Exception as e:
        logger.error("[Parse] %s", e)

    history.append({"role": "assistant", "content": reply})
    save_conv(user_id, "client", history, order_data)
    send_message(chat_id, reply)

# ─── Handle driver AI ─────────────────────────────────────────────────────────
def handle_driver_message(chat_id, user_id, text, user_label):
    role, history, order_data = get_conv(user_id)
    history.append({"role": "user", "content": text})
    system = DRIVER_SYSTEM.format(regions=", ".join(REGION_NAMES))
    reply = ask_claude(system, history)
    if not reply:
        send_message(chat_id, "Uzr, texnik xatolik. Qaytadan urinib ko'ring.")
        return

    try:
        json_match = re.search(r'\{[^{}]*"SEARCH"[^{}]*\}', reply, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            if data.get("SEARCH"):
                qayerdan = data.get("qayerdan", "")
                qayerga  = data.get("qayerga", "")
                orders   = find_orders_for_driver(qayerdan, qayerga)
                history.append({"role": "assistant", "content": reply})
                save_conv(user_id, "driver", history, {})
                if not orders:
                    route = f"{qayerdan} → {qayerga}" if qayerdan else qayerga
                    send_message(chat_id,
                        f"Hozirda {route} yo'nalishi uchun yuklar yo'q.\n"
                        f"Yangi yuklar kelganda ko'rish uchun /yuklar buyrug'ini yuboring.")
                    return
                route = f"{qayerdan} → {qayerga}" if qayerdan else qayerga
                send_message(chat_id, f"📋 {route} yo'nalishi bo'yicha {len(orders)} ta yuk topildi:")
                for o in orders:
                    send_message(chat_id,
                        format_order(o["order_num"], o["yuk"], o["qayerdan"], o["qayerga"],
                                     o["ogirlik"], o.get("mashina",""), o["narx"],
                                     o["yuklash_san"], o["telefon"]),
                        reply_markup=driver_keyboard(o["order_id"]))
                return
    except Exception as e:
        logger.error("[DriverParse] %s", e)

    history.append({"role": "assistant", "content": reply})
    save_conv(user_id, "driver", history, {})
    send_message(chat_id, reply)

# ─── Main handler ─────────────────────────────────────────────────────────────
def handle_message(msg):
    sender  = msg.get("from", {})
    chat_id = msg.get("chat", {}).get("id") or sender.get("id")
    user_id = sender.get("id")
    text    = (msg.get("text") or "").strip()
    user_label = get_user_label(sender)
    if not text: return

    if text == "/chatid":
        send_message(chat_id, f"Chat ID: <code>{chat_id}</code>")
        return

    # Блокируем боты в неавторизованных группах
    if msg.get("chat", {}).get("type") in ("group", "supergroup"):
        if text and text.startswith("/chatid"):
            send_message(chat_id, f"Chat ID: <code>{chat_id}</code>")
        return  # В группах бот не отвечает на обычные сообщения

    if text == "/start":
        clear_conv(user_id)
        driver = is_driver(user_id)
        if user_id == ADMIN_ID:
            send_message(chat_id,
                "👋 Admin paneliga xush kelibsiz!\n\n"
                "📦 /yangi_yuk — yangi yuk joylash\n"
                "🚚 /yuklar — yuklar qidirish\n"
                "📊 /statistika — statistika\n"
                "👥 /haydovchilar — haydovchilar royxati\n"
                "➕ /haydovchi_add — haydovchi qo'shish")
        elif driver:
            send_message(chat_id,
                "👋 Xush kelibsiz!\n\nSiz kim sifatida kiryapsiz?",
                reply_markup=role_keyboard())
        else:
            # Новый пользователь — предлагаем зарегистрироваться
            send_message(chat_id,
                "👋 CELC Logistics botiga xush kelibsiz!\n\n"
                "Siz hali ro'yxatdan o'tmagansiz.\n\n"
                "Yuk joylash uchun /yangi_yuk\n"
                "Haydovchi sifatida ro'yxatdan o'tish uchun /register")
        return

    if text == "/register":
        clear_conv(user_id)
        set_state(user_id, "register", {})
        # Get region keyboard for driver registration  
        send_message(chat_id,
            "🚚 Haydovchi sifatida ro'yxatdan o'tish\n\n"
            "Qaysi regionda ishlaysiz?",
            reply_markup=region_register_keyboard())
        return

    if text == "/haydovchilar" and user_id == ADMIN_ID:
        drivers = get_all_drivers()
        if not drivers:
            send_message(chat_id, "👥 Hozirda haydovchilar yo'q.")
            return
        text_out = f"👥 <b>Haydovchilar: {len(drivers)} ta</b>\n\n"
        for d in drivers:
            text_out += f"• {d['user_label']} | {d.get('region','—')}\n"
        send_message(chat_id, text_out)
        return

    if text == "/haydovchi_add" and user_id == ADMIN_ID:
        send_message(chat_id,
            "➕ Haydovchi qo'shish uchun uning Telegram ID sini yuboring.\n"
            "Masalan: <code>123456789</code>\n\n"
            "Haydovchi /register orqali ham ro'yxatdan o'ta oladi.")
        set_state(user_id, "add_driver", {})
        return

    if text == "/yangi_yuk":
        clear_conv(user_id)
        save_conv(user_id, "client", [], {})
        send_message(chat_id,
            "📦 Yangi yuk joylash\n\n"
            "Yukingiz haqida gapirib bering. Masalan:\n"
            "Toshkentdan Samarqandga 10 tonna g'isht")
        return

    if text == "/yuklar":
        clear_conv(user_id)
        save_conv(user_id, "driver", [], {})
        send_message(chat_id,
            "🚚 Haydovchi rejimi\n\n"
            "Qayerdan qayerga ketayotganingizni yozing. Masalan:\n"
            "Men Toshkentdan Farg'onaga ketyapman")
        return

    if text == "/statistika" and chat_id == ADMIN_ID:
        with get_db() as conn:
            total = qone(conn, "SELECT COUNT(*) as c FROM orders WHERE status != 'draft'")["c"]
            yangi = qone(conn, "SELECT COUNT(*) as c FROM orders WHERE status='yangi'")["c"]
            qabul = qone(conn, "SELECT COUNT(*) as c FROM orders WHERE status='qabul'")["c"]
            done  = qone(conn, "SELECT COUNT(*) as c FROM orders WHERE status='yetkazildi'")["c"]
            today = qone(conn, "SELECT COUNT(*) as c FROM orders WHERE DATE(created_at)=CURRENT_DATE AND status!='draft'")["c"]
        send_message(chat_id,
            f"📊 Statistika\n\n"
            f"📅 Bugun: {today}\n"
            f"📦 Jami: {total}\n\n"
            f"🟢 Yangi: {yangi}\n"
            f"🔴 Qabul qilingan: {qabul}\n"
            f"✅ Yetkazildi: {done}")
        return

    # Обработка добавления водителя админом
    state, state_data = get_state(user_id)
    if state == "add_driver" and user_id == ADMIN_ID:
        try:
            new_driver_id = int(text.strip())
            register_driver(new_driver_id, f"ID:{new_driver_id}", "")
            clear_state(user_id)
            send_message(chat_id,
                f"✅ Haydovchi qo'shildi!\n"
                f"🆔 ID: <code>{new_driver_id}</code>\n\n"
                f"Haydovchi /start yozib regionini tanlashi kerak.")
        except ValueError:
            send_message(chat_id, "❌ Noto'g'ri ID. Faqat raqam yuboring.")
        return

    role, history, order_data = get_conv(user_id)

    if role == "client":
        handle_client_message(chat_id, user_id, text, user_label)
    elif role == "driver":
        handle_driver_message(chat_id, user_id, text, user_label)
    else:
        send_message(chat_id,
            "Davom etish uchun /start ni bosing.",
            reply_markup=role_keyboard())

# ─── Callback handler ─────────────────────────────────────────────────────────
def handle_callback(cb):
    cb_id      = cb["id"]
    cb_data    = cb.get("data", "")
    chat_id    = cb["message"]["chat"]["id"]
    message_id = cb["message"]["message_id"]
    user       = cb.get("from", {})
    user_id    = user.get("id")
    user_label = get_user_label(user)
    answer_callback(cb_id)

    # Регистрация водителя — выбор региона
    if cb_data.startswith("reg_region|"):
        region = cb_data.split("|", 1)[1]
        register_driver(user_id, user_label, region)
        region_text = "Barcha regionlar" if region == "all" else region

        # Отправляем ссылки на региональные группы
        if region == "all":
            edit_message(chat_id, message_id,
                f"✅ Ro'yxatdan o'tdingiz! Region: {region_text}\n\n"
                f"Endi /yuklar buyrug'i orqali yuklar qidirishingiz mumkin.")
        else:
            edit_message(chat_id, message_id,
                f"✅ Ro'yxatdan o'tdingiz!\n"
                f"📍 Regioningiz: {region_text}\n\n"
                f"Yuklar qidirish uchun /yuklar yozing.")

        # Уведомляем админа
        if ADMIN_ID:
            send_message(ADMIN_ID,
                f"🚚 Yangi haydovchi ro'yxatdan o'tdi!\n"
                f"👤 {user_label}\n"
                f"📍 Region: {region_text}\n"
                f"🆔 ID: <code>{user_id}</code>")
        return

    if cb_data.startswith("role|"):
        role = cb_data.split("|")[1]
        clear_conv(user_id)
        save_conv(user_id, role, [], {})
        if role == "client":
            edit_message(chat_id, message_id,
                "📦 Yuk beruvchi rejimi\n\n"
                "Yukingiz haqida gapirib bering. Masalan:\n"
                "Toshkentdan Samarqandga 10 tonna g'isht")
        else:
            edit_message(chat_id, message_id,
                "🚚 Haydovchi rejimi\n\n"
                "Qayerdan qayerga ketayotganingizni yozing. Masalan:\n"
                "Men Toshkentdan Farg'onaga ketyapman")
        return

    if cb_data.startswith("accept|"):
        order_id = int(cb_data.split("|")[1])
        with get_db() as conn:
            order = qone(conn, "SELECT * FROM orders WHERE order_id=%s", [order_id])
            if not order:
                send_message(chat_id, "Yuk topilmadi."); return
            if order["status"] != "yangi":
                send_message(chat_id, "Bu yuk allaqachon qabul qilingan!"); return
            qrun(conn, """UPDATE orders SET status='qabul', driver_id=%s, driver_name=%s, updated_at=NOW()
                WHERE order_id=%s AND status='yangi'""", [user_id, user_label, order_id])
            updated = qone(conn, "SELECT driver_id FROM orders WHERE order_id=%s", [order_id])

        if updated["driver_id"] != user_id:
            send_message(chat_id, "Bu yuk boshqa haydovchi tomonidan qabul qilindi!"); return

        region_chat_id = REGIONS.get(order["region"], 0)
        if region_chat_id and order["chat_msg_id"]:
            # В группе телефон НЕ показываем даже после принятия
            new_text = format_order(
                order["order_num"], order["yuk"], order["qayerdan"], order["qayerga"],
                order["ogirlik"], order.get("mashina",""), order["narx"],
                order["yuklash_san"], order["telefon"],
                "Qabul qilindi 🔴", show_phone=False)
            new_text += f"\n\n🚚 Haydovchi: {user_label}"
            edit_message(region_chat_id, order["chat_msg_id"], new_text)

        # Телефон показываем ТОЛЬКО в личке водителю, не в группе
        mashina_info = f"\n🚛 {order.get('mashina','')}" if order.get('mashina') else ""
        send_message(user_id,
            f"✅ Yuk #{order['order_num']} qabul qilindi!\n\n"
            f"📞 Mijoz telefoni: {order['telefon']}\n"
            f"📍 {order['qayerdan']} → {order['qayerga']}\n"
            f"🗂 {order['yuk']} | {order['ogirlik']}{mashina_info}\n"
            f"💰 {order['narx']}\n\n"
            f"Yuk yetkazilgandan so'ng tasdiqlang:",
            reply_markup=confirm_keyboard(order_id))

        if ADMIN_ID:
            send_message(ADMIN_ID,
                f"🚚 Yuk #{order['order_num']} qabul qilindi\n"
                f"👤 Haydovchi: {user_label}\n"
                f"📍 {order['qayerdan']} → {order['qayerga']}")
        return

    if cb_data.startswith("delivered|"):
        order_id = int(cb_data.split("|")[1])
        with get_db() as conn:
            order = qone(conn, "SELECT * FROM orders WHERE order_id=%s", [order_id])
            if not order: return
            qrun(conn, "UPDATE orders SET status='yetkazildi', updated_at=NOW() WHERE order_id=%s", [order_id])
        send_message(chat_id, f"✅ Rahmat! Yuk #{order['order_num']} yetkazildi deb belgilandi! 🎉")
        if ADMIN_ID:
            send_message(ADMIN_ID,
                f"✅ Yuk #{order['order_num']} yetkazildi!\n"
                f"🚚 {order['driver_name']}\n"
                f"📍 {order['qayerdan']} → {order['qayerga']}\n"
                f"💰 {order['narx']}")
        return

    if cb_data.startswith("problem|"):
        order_id = int(cb_data.split("|")[1])
        with get_db() as conn:
            order = qone(conn, "SELECT * FROM orders WHERE order_id=%s", [order_id])
        send_message(chat_id, f"Yuk #{order['order_num']} bo'yicha muammo haqida dispatcher bilan bog'laning.")
        if ADMIN_ID:
            send_message(ADMIN_ID,
                f"🚨 MUAMMO! Yuk #{order['order_num']}\n"
                f"🚚 {user_label}\n"
                f"📍 {order['qayerdan']} → {order['qayerga']}\n"
                f"📞 {order['telefon']}")
        return

# ─── Flask ────────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        if not data: return "ok", 200
        if "callback_query" in data: handle_callback(data["callback_query"])
        elif "message" in data: handle_message(data["message"])
    except Exception as e:
        logger.error("Webhook error: %s", e, exc_info=True)
    return "ok", 200

@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200

def set_webhook():
    endpoint = f"{WEBHOOK_URL.rstrip('/')}/webhook"
    resp = requests.post(f"{API_BASE}/setWebhook",
        json={"url": endpoint, "allowed_updates": ["message", "callback_query", "my_chat_member"]}, timeout=10)
    logger.info("Webhook: %s -> %s", endpoint, resp.json().get("ok"))

    # Для обычных пользователей — только /start
    requests.post(f"{API_BASE}/setMyCommands", json={
        "commands": [{"command": "start", "description": "Boshlash"}]
    }, timeout=10)

    # Для админа — полное меню
    requests.post(f"{API_BASE}/setMyCommands", json={
        "commands": [
            {"command": "start",      "description": "Boshlash"},
            {"command": "yangi_yuk",  "description": "📦 Yangi yuk joylash"},
            {"command": "yuklar",     "description": "🚚 Yuklar qidirish"},
            {"command": "statistika", "description": "📊 Statistika"},
        ],
        "scope": {"type": "chat", "chat_id": ADMIN_ID}
    }, timeout=10)

try:
    init_db()
    set_webhook()
    logger.info("[Bot] CELC AI Logistics bot started!")
except Exception as e:
    logger.error("Startup error: %s", e)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
