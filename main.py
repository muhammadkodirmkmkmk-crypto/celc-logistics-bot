import os, json, logging, urllib.parse, re
from datetime import datetime
from contextlib import contextmanager
from flask import Flask, request
import requests
import pg8000

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)
app = Flask(__name__)

def format_phone(phone):
    """Форматирует телефон как +998XXXXXXXXX чтобы был кликабельным в Telegram"""
    if not phone: return phone
    digits = re.sub(r'\D', '', str(phone))
    if len(digits) == 9:
        return f"+998{digits}"
    elif len(digits) == 12 and digits.startswith("998"):
        return f"+{digits}"
    elif len(digits) == 11 and digits.startswith("998"):
        return f"+{digits}"
    elif str(phone).startswith("+"):
        return phone
    return f"+{digits}" if digits else phone

def format_price(price):
    """Форматирует цену как 3,500,000 so'm"""
    if not price: return price
    price_str = str(price).strip()
    # Извлекаем только цифры
    digits = re.sub(r'[^\d]', '', price_str)
    if not digits: return price_str
    try:
        num = int(digits)
        formatted = f"{num:,}".replace(",", " ")
        if "so'm" in price_str.lower() or "сум" in price_str.lower():
            return f"{formatted} so'm"
        elif "mln" in price_str.lower() or "mlн" in price_str.lower():
            return f"{formatted} so'm"
        return f"{formatted} so'm"
    except:
        return price_str

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

# Forum group - одна группа с топиками
FORUM_CHAT_ID = int(os.environ.get("FORUM_CHAT_ID", "-1003910403192"))

# Регионы → thread_id топика (CELC yuklar group)
REGIONS = {
    "Buxoro":            int(os.environ.get("THREAD_BUXORO", "7")),
    "Farg'ona":          int(os.environ.get("THREAD_FARGONA", "4")),
    "Samarqand":         int(os.environ.get("THREAD_SAMARQAND", "9")),
    "Toshkent viloyati": int(os.environ.get("THREAD_TOSHKENT_VIL", "14")),
    "Toshkent shahar":   int(os.environ.get("THREAD_TOSHKENT_SHR", "15")),
    "Namangan":          int(os.environ.get("THREAD_NAMANGAN", "3")),
    "Navoiy":            int(os.environ.get("THREAD_NAVOIY", "8")),
    "Jizzax":            int(os.environ.get("THREAD_JIZZAX", "12")),
    "Qashqadaryo":       int(os.environ.get("THREAD_QASHQA", "11")),
    "Andijon":           int(os.environ.get("THREAD_ANDIJON", "2")),
    "Xorazm":            int(os.environ.get("THREAD_XORAZM", "6")),
    "Sirdaryo":          int(os.environ.get("THREAD_SIRDARYO", "13")),
    "Surxondaryo":       int(os.environ.get("THREAD_SURXON", "10")),
    "Qirg'iziston":      int(os.environ.get("THREAD_KIRGIZ", "1142")),
    "Qoraqalpog'iston":  int(os.environ.get("THREAD_QORAQALP", "5")),
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
        qrun(conn, """CREATE TABLE IF NOT EXISTS user_states (
            user_id     BIGINT PRIMARY KEY,
            state       TEXT DEFAULT '',
            data        TEXT DEFAULT '{}',
            updated_at  TIMESTAMP DEFAULT NOW()
        )""")
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
CLIENT_SYSTEM = """CELC dispetcheri. Qisqa gaplash.

Kerak: yuk nomi, qayerdan, qayerga, ogirlik, narx, sana, telefon.

MUHIM:
1. Berilgan narsani qabul qil
2. FAQAT BITTA yetishmagan narsani so'ra - HECH QACHON ro'yxat yozma
3. "aka" de
4. To'g'ri yoz: Assalomu alaykum, rahmat
5. Hammasi to'liq - JSON qaytar, boshqa hech narsa yozma:
{"DONE":true,"yuk":"","qayerdan":"","qayerga":"","ogirlik":"","mashina":"","narx":"","yuklash_san":"","telefon":""}

MISOL to'g'ri javob:
User: Gisht Toshkentdan Samarqandga 3 tonna 2mln bugun
Bot: Telefon raqamingiz aka?

MISOL NOTO'G'RI javob (BUNDAY QILMA):
Bot: - Yuk: Gisht
- Qayerdan: Toshkent
Mashina turini aniqlaymiz..."""
DRIVER_SYSTEM = """Sen CELC dispetcherisan. O'zbek tilida qisqa gaplash.

Haydovchi marshrut aytsa JSON qaytar:
{{"SEARCH":true,"qayerdan":"","qayerga":"","max_og":null,"min_og":null}}

- "aka" de, 1 jumla max
- "barcha", "hamma" = qayerga="" qoldir
- tonnagacha = max_og
- Boshqa savol = 1 jumlada javob"""

# ─── Telegram helpers ─────────────────────────────────────────────────────────
def send_message(chat_id, text, reply_markup=None, thread_id=None):
    if not chat_id or not text: return None
    # Убираем markdown ** из текста
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', str(text))
    payload = {"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML"}
    if reply_markup: payload["reply_markup"] = json.dumps(reply_markup)
    if thread_id: payload["message_thread_id"] = thread_id
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
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
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
    formatted_phone = format_phone(telefon)
    formatted_price = format_price(narx)
    phone_line = f"📞 <b>Bog'lanish:</b> {formatted_phone}" if show_phone else "📞 <b>Bog'lanish:</b> <i>Qabul qilgandan so'ng ko'rinadi</i>"
    return (
        f"📦 <b>Yangi yuk #{order_num}</b>\n\n"
        f"🗂 <b>Yuk:</b> {yuk}\n"
        f"📍 <b>Qayerdan:</b> {qayerdan}\n"
        f"📍 <b>Qayerga:</b> {qayerga}\n"
        f"⚖️ <b>Og'irlik:</b> {ogirlik}\n"
        f"💰 <b>Taklif qilinayotgan narx:</b> {formatted_price}\n"
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
    return {"inline_keyboard": [
        [{"text": "📦 Yuk beruvchi (mijoz)", "callback_data": "role|client"}],
        [{"text": "🚚 Haydovchi",            "callback_data": "role|driver"}]
    ]}

FORUM_INVITE_LINK = os.environ.get("FORUM_INVITE_LINK", "https://t.me/celcyuklar")

def get_group_links():
    """Возвращает ссылку на форум группу"""
    return FORUM_INVITE_LINK

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
    thread_id = REGIONS.get(region, 0)
    # В топике телефон скрыт
    order_text_no_phone = format_order(
        order["order_num"], order["yuk"], order["qayerdan"],
        order["qayerga"], order["ogirlik"], order.get("mashina",""),
        order["narx"], order["yuklash_san"], order["telefon"], show_phone=False)

    if thread_id:
        result = send_message(FORUM_CHAT_ID, order_text_no_phone,
                             reply_markup=driver_keyboard(order_id),
                             thread_id=thread_id)
    else:
        result = send_message(FORUM_CHAT_ID, order_text_no_phone,
                             reply_markup=driver_keyboard(order_id))

    logger.info("[Send] FORUM=%s thread=%s result=%s", FORUM_CHAT_ID, thread_id, result)

    if result and result.get("ok"):
        msg_id = result["result"]["message_id"]
        with get_db() as conn:
            qrun(conn, "UPDATE orders SET chat_msg_id=%s WHERE order_id=%s", [msg_id, order_id])
        return True
    else:
        logger.error("[Send] Failed: %s", result)
    return False

# ─── Find orders for driver ───────────────────────────────────────────────────
def find_orders_for_driver(qayerdan, qayerga, max_og=None, min_og=None):
    """
    Водитель едет из qayerdan в qayerga.
    Логика: водитель берёт груз ГДЕ ОН НАХОДИТСЯ (qayerdan водителя = qayerdan заявки)
    Или везёт В нужный город (qayerga водителя = qayerga заявки)
    """
    with get_db() as conn:
        orders = qall(conn, "SELECT * FROM orders WHERE status='yangi' ORDER BY created_at DESC LIMIT 50")
    if not orders:
        return []

    city_map = {
        "toshkent": ["toshkent", "toshkentdan", "toshkentga", "тошкент", "ташкент"],
        "samarqand": ["samarqand", "samarqanddan", "samarqandga", "самарканд"],
        "buxoro": ["buxoro", "buxorodan", "buxoroga", "бухоро", "бухара"],
        "fargona": ["farg'ona", "fargona", "farg'onadan", "farg'onaga", "фаргона", "фергана"],
        "namangan": ["namangan", "namanganга", "наманган"],
        "andijon": ["andijon", "андижан"],
        "navoiy": ["navoiy", "navoiydan", "navoiyga", "навои"],
        "jizzax": ["jizzax", "джизак"],
        "qashqa": ["qashqa", "qarshi", "карши"],
        "xorazm": ["xorazm", "urganch", "хорезм"],
        "surxon": ["surxon", "termiz", "термез"],
        "sirdaryo": ["sirdaryo", "guliston"],
        "qoraqalp": ["qoraqalp", "nukus"],
    }

    def get_city_key(name):
        if not name: return ""
        n = name.lower().strip()
        for key, variants in city_map.items():
            for v in variants:
                if v in n:
                    return key
        return n.split()[0] if n else ""

    driver_from = get_city_key(qayerdan)
    driver_to = get_city_key(qayerga)

    matched = []
    for o in orders:
        order_from = get_city_key(o["qayerdan"])
        order_to   = get_city_key(o["qayerga"])
        match = False

        if driver_from and driver_to:
            match = (driver_from == order_from and driver_to == order_to)
        elif driver_from and order_from and driver_from == order_from:
            match = True
        elif driver_to and order_to and driver_to == order_to:
            match = True
        elif not driver_from and not driver_to:
            match = True  # Нет фильтра по городу — показываем все

        # Фильтр по весу — max_og это грузоподъёмность машины водителя
        # Показываем заявки где вес <= грузоподъёмности водителя
        if match and (max_og is not None or min_og is not None):
            try:
                og_str = re.sub(r"[^\d.]", "", str(o["ogirlik"] or "0"))
                og_val = float(og_str) if og_str else 0
                if max_og is not None and og_val > max_og:
                    match = False
                if min_og is not None and og_val < min_og:
                    match = False
            except:
                pass

        # Если фильтр не задан но есть только вес — показываем все
        if not driver_from and not driver_to and max_og is None and min_og is None:
            match = True

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

                # Отправляем в региональный чат автоматически
                sent = send_order_to_region(order_id, order)
                # Сбрасываем историю но оставляем роль client для следующей заявки
                save_conv(user_id, "client", [], {})

                if sent:
                    send_message(chat_id,
                        f"🎉 <b>Yuk muvaffaqiyatli joylashtirildi!</b>\n\n"
                        f"{preview}\n\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"📍 <b>{region}</b> chatiga yuborildi\n"
                        f"🚚 Haydovchilar ko'rmoqda...\n\n"
                        f"➕ Yangi yuk → /yangi_yuk")
                else:
                    send_message(chat_id,
                        f"✅ <b>Yuk bazaga saqlandi!</b>\n\n"
                        f"{preview}\n\n"
                        f"📍 Region: <b>{region}</b>\n"
                        f"➕ Yangi yuk → /yangi_yuk")

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
                max_og   = data.get("max_og", None)
                min_og   = data.get("min_og", None)
                orders   = find_orders_for_driver(qayerdan, qayerga, max_og, min_og)
                history.append({"role": "assistant", "content": reply})
                save_conv(user_id, "driver", history, {})
                if not orders:
                    route = f"{qayerdan} → {qayerga}" if qayerdan else (qayerga or "barcha yo'nalishlar")
                    weight_info = ""
                    if max_og: weight_info = f" ({max_og}t gacha)"
                    if min_og: weight_info = f" ({min_og}t dan ko'p)"
                    send_message(chat_id,
                        f"Hozirda {route}{weight_info} uchun yuklar yo'q aka.\n"
                        f"Yangi yuklar kelganda /yuklar yozing.")
                    return
                route = f"{qayerdan} → {qayerga}" if qayerdan else (qayerga or "barcha yo'nalishlar")
                weight_info = ""
                if max_og: weight_info = f" ({max_og}t gacha)"
                if min_og: weight_info = f" ({min_og}t dan ko'p)"
                send_message(chat_id, f"📋 {route}{weight_info} — {len(orders)} ta yuk topildi:")
                for o in orders:
                    send_message(chat_id,
                        format_order(o["order_num"], o["yuk"], o["qayerdan"], o["qayerga"],
                                     o["ogirlik"], o.get("mashina",""), o["narx"],
                                     o["yuklash_san"], o["telefon"], show_phone=False),
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
        thread_id = msg.get("message_thread_id", None)
        if thread_id:
            send_message(chat_id, f"Chat ID: <code>{chat_id}</code>\nThread ID: <code>{thread_id}</code>", reply_markup=None)
        else:
            send_message(chat_id, f"Chat ID: <code>{chat_id}</code>")
        return

    # Блокируем боты в неавторизованных группах
    if msg.get("chat", {}).get("type") in ("group", "supergroup"):
        if text and text.startswith("/chatid"):
            send_message(chat_id, f"Chat ID: <code>{chat_id}</code>")
        return  # В группах бот не отвечает на обычные сообщения

    if text == "/start":
        clear_conv(user_id)
        if user_id == ADMIN_ID:
            send_message(chat_id,
                "👑 <b>Admin panel</b>\n\n"
                "━━━━━━━━━━━━━━━━\n"
                "📦 /yangi_yuk — <b>yuk joylash</b>\n"
                "🚚 /yuklar — <b>yuklar qidirish</b>\n"
                "📊 /statistika — <b>statistika</b>\n"
                "👥 /haydovchilar — <b>haydovchilar</b>\n"
                "━━━━━━━━━━━━━━━━")
        else:
            send_message(chat_id,
                "👋 <b>CELC Logistics botiga xush kelibsiz!</b>\n\n"
                "Siz kim sifatida foydalanasiz?",
                reply_markup=role_keyboard())
        return

    if text == "/register":
        clear_conv(user_id)
        send_message(chat_id,
            "🚚 <b>Haydovchi sifatida ro'yxatdan o'tish</b>\n\n"
            "Quyida o'z regioningizni tanlang 👇",
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
            "📦 <b>Yangi yuk joylash</b>\n\n"
            "Yukingiz haqida gapirib bering.\n\n"
            "📝 <b>Namuna:</b>\n"
            "<i>Samarqanddan Toshkentga 10 tonna g'isht, bugun, 3 mln, 998901234567</i>")
        return

    if text == "/yuklar":
        clear_conv(user_id)
        save_conv(user_id, "driver", [], {})
        send_message(chat_id,
            "🚚 <b>Yuk qidirish</b>\n\n"
            "Qayerdan qayerga ketayotganingizni yozing.\n\n"
            "📝 <b>Namuna:</b>\n"
            "<i>Toshkentdan Farg'onaga ketyapman</i>\n"
            "<i>15 tonnagacha Samarqandga yuk bormi?</i>\n\n"
            f"👥 <b>Guruhda ham ko'ring:</b>\n"
            f"👉 <a href='{FORUM_INVITE_LINK}'>CELC Yuklar guruhi</a>")
        return

        send_message(chat_id, f"⏳ {len(lines)} ta yuk qo'shilmoqda...")
        success = 0
        failed = 0

        for line in lines:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 4:
                failed += 1
                continue
            try:
                yuk        = parts[0] if len(parts) > 0 else ""
                route      = parts[1] if len(parts) > 1 else ""
                ogirlik    = parts[2] if len(parts) > 2 else ""
                mashina    = parts[3] if len(parts) > 3 else ""
                narx       = parts[4] if len(parts) > 4 else ""
                yuklash    = parts[5] if len(parts) > 5 else "10.06.2026"
                telefon    = parts[6] if len(parts) > 6 else "998900000000"

                # Парсим маршрут
                qayerdan, qayerga = "", ""
                route_lower = route.lower()
                if "dan " in route_lower:
                    idx = route_lower.index("dan ")
                    qayerdan = route[:idx+3].strip()
                    qayerga  = route[idx+4:].strip()
                elif "→" in route:
                    parts2 = route.split("→")
                    qayerdan = parts2[0].strip()
                    qayerga  = parts2[1].strip() if len(parts2) > 1 else ""
                else:
                    qayerdan = route
                    qayerga  = ""

                region = detect_region(qayerdan, qayerga)
                order_num = next_order_num()

                with get_db() as conn:
                    qrun(conn, """INSERT INTO orders
                        (order_num,yuk,qayerdan,qayerga,ogirlik,mashina,narx,yuklash_san,telefon,region,status)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'yangi')""",
                        [order_num, yuk, qayerdan, qayerga, ogirlik,
                         mashina, narx, yuklash, telefon, region])
                    order = qone(conn, "SELECT * FROM orders WHERE order_num=%s", [order_num])

                send_order_to_region(order["order_id"], order)
                success += 1
            except Exception as e:
                logger.error("[Bulk] %s: %s", line, e)
                failed += 1

        msg = f"✅ <b>{success} ta yuk muvaffaqiyatli qo'shildi!</b>"
        if failed:
            msg += f"\n❌ {failed} ta qo'shilmadi (format xato)"
        send_message(chat_id, msg)
        if ADMIN_ID and chat_id != ADMIN_ID:
            send_message(ADMIN_ID, f"📦 Bulk: {success} ta yangi yuk qo'shildi")
        return

        send_message(chat_id, f"⏳ {len(lines)} ta yuk qo\'shilmoqda...")
        success = 0
        failed = 0

        for line in lines:
            parts2 = [p.strip() for p in line.split(",")]
            if len(parts2) < 4:
                failed += 1
                continue
            try:
                yuk     = parts2[0] if len(parts2) > 0 else ""
                route   = parts2[1] if len(parts2) > 1 else ""
                ogirlik = parts2[2] if len(parts2) > 2 else ""
                mashina = parts2[3] if len(parts2) > 3 else ""
                narx    = parts2[4] if len(parts2) > 4 else "0"
                yuklash = parts2[5] if len(parts2) > 5 else "10.06.2026"
                telefon = parts2[6] if len(parts2) > 6 else "998900000000"

                qayerdan, qayerga = "", ""
                r = route.lower()
                if "dan " in r:
                    idx = r.index("dan ")
                    qayerdan = route[:idx+3].strip()
                    qayerga  = route[idx+4:].strip()
                elif "-" in route:
                    sp = route.split("-", 1)
                    qayerdan = sp[0].strip()
                    qayerga  = sp[1].strip()
                else:
                    qayerdan = route

                region = detect_region(qayerdan, qayerga)
                order_num = next_order_num()

                with get_db() as conn:
                    qrun(conn, """INSERT INTO orders
                        (order_num,yuk,qayerdan,qayerga,ogirlik,mashina,narx,yuklash_san,telefon,region,status)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'yangi')""",
                        [order_num, yuk, qayerdan, qayerga, ogirlik,
                         mashina, narx, yuklash, telefon, region])
                    order = qone(conn, "SELECT * FROM orders WHERE order_num=%s", [order_num])

                send_order_to_region(order["order_id"], order)
                success += 1
            except Exception as e:
                logger.error("[Bulk] %s: %s", line, e)
                failed += 1

        msg = f"✅ <b>{success} ta yuk muvaffaqiyatli qo\'shildi!</b>"
        if failed:
            msg += f"\n❌ {failed} ta qo\'shilmadi"
        send_message(chat_id, msg)
        return

    # /bulk bulk order
    if text and text.startswith("/bulk"):
        raw = text[5:].strip()
        lines = [l.strip() for l in raw.split(chr(10)) if l.strip()]
        if not lines:
            send_message(chat_id,
                "📦 /bulk — ommaviy yuk qo'shish\n\n"
                "Namuna:\n"
                "/bulk\n"
                "G'isht, Samarqand-Toshkent, 20t, Tent 6, 3500000, 10.06.2026, 998901111\n"
                "Mebel, Toshkent-Fargona, 5t, Ref, 2000000, 11.06.2026, 998902222")
            return
        send_message(chat_id, "⏳ " + str(len(lines)) + " ta yuk qo'shilmoqda...")
        ok = 0
        fail = 0
        for line in lines:
            cols = [c.strip() for c in line.split(",")]
            if len(cols) < 4:
                fail += 1
                continue
            try:
                yuk     = cols[0] if len(cols) > 0 else ""
                route   = cols[1] if len(cols) > 1 else ""
                ogirlik = cols[2] if len(cols) > 2 else ""
                mashina = cols[3] if len(cols) > 3 else ""
                narx    = cols[4] if len(cols) > 4 else "0"
                yuklash = cols[5] if len(cols) > 5 else "10.06.2026"
                telefon = cols[6] if len(cols) > 6 else "998900000000"
                qd, qg = "", ""
                rl = route.lower()
                if "dan " in rl:
                    i = rl.index("dan ")
                    qd = route[:i+3].strip()
                    qg = route[i+4:].strip()
                elif "-" in route:
                    sp = route.split("-", 1)
                    qd = sp[0].strip()
                    qg = sp[1].strip()
                else:
                    qd = route
                region = detect_region(qd, qg)
                onum = next_order_num()
                with get_db() as conn:
                    qrun(conn, "INSERT INTO orders (order_num,yuk,qayerdan,qayerga,ogirlik,mashina,narx,yuklash_san,telefon,region,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'yangi')",
                        [onum, yuk, qd, qg, ogirlik, mashina, narx, yuklash, telefon, region])
                    o = qone(conn, "SELECT * FROM orders WHERE order_num=%s", [onum])
                send_order_to_region(o["order_id"], o)
                ok += 1
            except Exception as e:
                logger.error("[Bulk] %s: %s", line, e)
                fail += 1
        res = "✅ " + str(ok) + " ta yuk qo'shildi!"
        if fail:
            res += " ❌ " + str(fail) + " ta xato"
        send_message(chat_id, res)
        clear_conv(user_id)  # Сбрасываем состояние после bulk
        return

    if text == "/statistika" and chat_id == ADMIN_ID:
        with get_db() as conn:
            total = qone(conn, "SELECT COUNT(*) as c FROM orders WHERE status != 'draft'")["c"]
            yangi = qone(conn, "SELECT COUNT(*) as c FROM orders WHERE status='yangi'")["c"]
            qabul = qone(conn, "SELECT COUNT(*) as c FROM orders WHERE status='qabul'")["c"]
            done  = qone(conn, "SELECT COUNT(*) as c FROM orders WHERE status='yetkazildi'")["c"]
            today = qone(conn, "SELECT COUNT(*) as c FROM orders WHERE DATE(created_at)=CURRENT_DATE AND status!='draft'")["c"]

            # Общая сумма всех заявок
            sum_all = qone(conn, """SELECT SUM(CAST(REGEXP_REPLACE(narx, '[^0-9]', '', 'g') AS BIGINT)) as s
                FROM orders WHERE status != 'draft' AND narx ~ '[0-9]'""")
            total_sum = sum_all["s"] if sum_all and sum_all["s"] else 0

            # Топ 5 водителей
            top_drivers = qall(conn, """SELECT driver_name, COUNT(*) as cnt,
                SUM(CAST(REGEXP_REPLACE(narx, '[^0-9]', '', 'g') AS BIGINT)) as total_sum
                FROM orders WHERE status IN ('qabul','yetkazildi') AND driver_name != ''
                GROUP BY driver_name ORDER BY cnt DESC LIMIT 5""")

            # Топ 5 регионов
            top_regions = qall(conn, """SELECT region, COUNT(*) as cnt
                FROM orders WHERE status != 'draft' AND region != ''
                GROUP BY region ORDER BY cnt DESC LIMIT 5""")

            # Последние 5 заявок
            last_orders = qall(conn, """SELECT order_num, yuk, qayerdan, qayerga,
                narx, status, driver_name FROM orders
                WHERE status != 'draft' ORDER BY created_at DESC LIMIT 5""")

        # Форматируем сумму
        def fmt_sum(s):
            try:
                return f"{int(s):,}".replace(",", " ") + " so'm"
            except:
                return "—"

        msg = (
            f"📊 <b>To'liq hisobot</b>\n"
            f"━━━━━━━━━━━━━━━━\n\n"
            f"📅 <b>Bugun:</b> {today} ta\n"
            f"📦 <b>Jami zaявkalar:</b> {total} ta\n"
            f"💰 <b>Umumiy summa:</b> {fmt_sum(total_sum)}\n\n"
            f"🟢 <b>Yangi:</b> {yangi} ta\n"
            f"🔴 <b>Qabul qilingan:</b> {qabul} ta\n"
            f"✅ <b>Yetkazildi:</b> {done} ta\n\n"
        )

        if top_drivers:
            msg += "🏆 <b>Top haydovchilar:</b>\n"
            medals = ["🥇","🥈","🥉","4️⃣","5️⃣"]
            for i, d in enumerate(top_drivers):
                s = fmt_sum(d["total_sum"]) if d["total_sum"] else "—"
                msg += f"{medals[i]} {d['driver_name']} — {d['cnt']} ta | {s}\n"
            msg += "\n"

        if top_regions:
            msg += "📍 <b>Top regionlar:</b>\n"
            for r in top_regions:
                msg += f"  • {r['region']}: {r['cnt']} ta\n"
            msg += "\n"

        if last_orders:
            msg += "🕐 <b>Oxirgi 5 zaявka:</b>\n"
            status_map = {"yangi": "🟢", "qabul": "🔴", "yetkazildi": "✅", "draft": "⚪"}
            for o in last_orders:
                st = status_map.get(o["status"], "⚪")
                driver = f" → {o['driver_name']}" if o["driver_name"] else ""
                narx_fmt = fmt_sum(re.sub(r"[^0-9]", "", str(o["narx"]))) if o["narx"] else "—"
                msg += f"{st} #{o['order_num']} {o['yuk']} | {o['qayerdan']}→{o['qayerga']} | {narx_fmt}{driver}\n"

        send_message(chat_id, msg)
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

    # Каждое сообщение — умное определение намерения
    text_lower = text.lower()
    has_phone = bool(re.search(r'9[0-9]{8,11}', text))
    has_price = bool(re.search(r'[0-9]{5,}', text))
    multiline = len([l for l in text.strip().split(chr(10)) if l.strip()]) >= 3

    driver_search_kw = [
        "yuk bor","yuklar bor","yuklar yo","ketyapman","boraman","ketaman",
        "bormi","borme","yuklar qidirish","yuk topib","topib ber",
        "ko'rsat","korsating","ko'rsating","barcha yuklar","hammasi",
        "haydovchi","hayduvchi","tonnagacha","tonnadan",
        "men hozir","hozir toshkent","hozir samarqand","hozir fargona",
        "men toshkent","men samarqand","men buxoro","men fargona",
        # Кирилица
        "борам","кетам","йук борми","юк борми","хайдувчи","хайдовчи",
        "топиб бер","юк топ","йук топ","тонагача","тонадан",
        "корсат","кўрсат","барча юклар","ҳаммаси","либой","либо",
        "йук кер","юк кер","менга юк","менга йук",
        "хозр тошкент","хозир тошкент","хозр самарқанд","хозир бухоро",
        "ман тошкент","ман самарқанд","ман фарғона","ман бухоро",
        "да ман","га ман","дан ман",
    ]
    # Жёсткое правило: город + кер/kerak/bor = водитель
    cities = ["тошкент","самарқанд","самарканд","бухоро","фарғона","фергана",
              "наманган","андижан","навои","жиззах","қашқа","хоразм","сурхон",
              "toshkent","samarqand","buxoro","fargona","namangan","andijon",
              "navoiy","jizzax","xorazm","surxon"]
    need_words = ["кер","kerak","bor","bormi","ko'rsat","корсат","кўрсат",
                  "топиб","topib","қидир","qidir","йук бер","yuk ber"]
    
    has_city = any(c in text_lower for c in cities)
    has_need = any(n in text_lower for n in need_words)
    
    if has_city and has_need:
        is_search = True
    
    # Если просто название города без другого контекста — водитель
    if has_city and not has_phone and len(text.strip()) < 50:
        is_search = True
    is_search = any(kw in text_lower for kw in driver_search_kw)

    # Кириллица + многострочный = клиент добавляет заявку
    has_cyrillic = bool(re.search(r'[а-яёА-ЯЁЀ-ӿ]', text))
    is_order_attempt = (multiline and (has_price or has_cyrillic)) or has_phone

    if is_search:
        # Водитель ищет — всегда приоритет, даже если был диалог клиента
        save_conv(user_id, "driver", [], {})
        handle_driver_message(chat_id, user_id, text, user_label)
    elif is_order_attempt:
        # Клиент добавляет заявку
        if role == "client" and history:
            # Продолжаем диалог
            handle_client_message(chat_id, user_id, text, user_label)
        else:
            save_conv(user_id, "client", [], {})
            handle_client_message(chat_id, user_id, text, user_label)
    elif role == "driver":
        handle_driver_message(chat_id, user_id, text, user_label)
    else:
        if role != "client":
            save_conv(user_id, "client", [], {})
        handle_client_message(chat_id, user_id, text, user_label)

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
                f"🎉 <b>Muvaffaqiyatli ro'yxatdan o'tdingiz!</b>\n\n"
                f"🌍 <b>Region:</b> Barcha regionlar\n\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"🚚 Yuklar qidirish → /yuklar\n"
                f"━━━━━━━━━━━━━━━━")
        else:
            edit_message(chat_id, message_id,
                f"🎉 <b>Muvaffaqiyatli ro'yxatdan o'tdingiz!</b>\n\n"
                f"📍 <b>Regioningiz:</b> {region_text}\n\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"🚚 Yuklar qidirish → /yuklar\n"
                f"━━━━━━━━━━━━━━━━")

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
            # Водитель — отправляем ссылку на форум группу
            forum_link = get_group_links()
            edit_message(chat_id, message_id,
                "🚚 <b>Haydovchi rejimi</b>\n\n"
                "Barcha yuklarni ko'rish uchun guruhga a'zo bo'ling:\n\n"
                f"👉 <a href='{forum_link}'>CELC Yuklar guruhiga kirish</a>\n\n"
                "━━━━━━━━━━━━━━━━\n"
                "A'zo bo'lgandan so'ng /yuklar yozing\n"
                "Men sizga mos yuklar topib beraman! 🎯")
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

        if order["chat_msg_id"]:
            # Обновляем в форуме — убираем кнопку у всех водителей
            new_text = format_order(
                order["order_num"], order["yuk"], order["qayerdan"], order["qayerga"],
                order["ogirlik"], order.get("mashina",""), order["narx"],
                order["yuklash_san"], order["telefon"],
                "Qabul qilindi 🔴", show_phone=False)
            new_text += f"\n\n🚚 Haydovchi: {user_label}"
            # Передаём пустой markup — убираем кнопку "Qabul qilish"
            edit_message(FORUM_CHAT_ID, order["chat_msg_id"], new_text, reply_markup={"inline_keyboard": []})

        # Телефон показываем ТОЛЬКО в личке водителю, не в группе
        send_message(user_id,
            f"✅ <b>Yuk #{order['order_num']} qabul qilindi!</b>\n\n"
            f"📞 <b>Mijoz telefoni:</b> {format_phone(order['telefon'])}\n"
            f"📍 <b>Yo'nalish:</b> {order['qayerdan']} → {order['qayerga']}\n"
            f"🗂 <b>Yuk:</b> {order['yuk']} | {order['ogirlik']}\n"
            f"💰 <b>Narx:</b> {format_price(order['narx'])}\n\n"
            f"⚠️ Yuk yetkazilgandan so'ng tasdiqlang 👇",
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
