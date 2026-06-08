import os, json, logging, urllib.parse
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

REGIONS = {
    "Buxoro":            int(os.environ.get("CHAT_BUXORO", "0")),
    "Farg'ona":          int(os.environ.get("CHAT_FARGONA", "0")),
    "Samarqand":         int(os.environ.get("CHAT_SAMARQAND", "0")),
    "Toshkent viloyati": int(os.environ.get("CHAT_TOSHKENT_VIL", "0")),
    "Toshkent shahar":   int(os.environ.get("CHAT_TOSHKENT_SHR", "0")),
    "Namangan":          int(os.environ.get("CHAT_NAMANGAN", "0")),
    "Navoiy":            int(os.environ.get("CHAT_NAVOIY", "0")),
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
                          user=p["user"], password=p["password"], ssl_context=True)
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
            region      TEXT DEFAULT '',
            status      TEXT DEFAULT 'yangi',
            driver_id   BIGINT DEFAULT 0,
            driver_name TEXT DEFAULT '',
            chat_msg_id BIGINT DEFAULT 0,
            created_at  TIMESTAMP DEFAULT NOW(),
            updated_at  TIMESTAMP DEFAULT NOW()
        )""")
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
    logger.info("[DB] Tables ready")

def next_order_num():
    with get_db() as conn:
        qrun(conn, "UPDATE counters SET value=value+1 WHERE name='order_num'")
        return qone(conn, "SELECT value FROM counters WHERE name='order_num'")["value"]

# ─── Conversation history ─────────────────────────────────────────────────────
def get_conv(user_id):
    with get_db() as conn:
        row = qone(conn, "SELECT role, history, order_data FROM conversations WHERE user_id=%s", [user_id])
        if row:
            return row["role"], json.loads(row["history"]), json.loads(row["order_data"])
        return "client", [], {}

def save_conv(user_id, role, history, order_data):
    # Храним только последние 20 сообщений чтобы не раздувать токены
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
def ask_claude(system_prompt, messages, max_tokens=500):
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
Mijoz bilan o'zbek tilida suhbatlashasan va yuk haqida ma'lumot yig'asan.

Yig'ish kerak bo'lgan ma'lumotlar:
1. Yuk nomi (nima tashiladi)
2. Qayerdan (jo'natish joyi)
3. Qayerga (yetkazish joyi)  
4. Og'irligi (tonna yoki kg)
5. Taklif narxi (so'mda)
6. Yuklash sanasi
7. Bog'lanish telefoni

Qoidalar:
- Har bir savolni alohida, do'stona va tabiiy so'ra
- Mijoz yozgan ma'lumotlardan avtomatik tushun (masalan "10 tonna g'isht Toshkentdan Samarqandga" desa - bir savol bilan 3 ta maydonni to'ldir)
- Barcha ma'lumot to'liq bo'lganda JSON formatda qaytар (boshqa hech narsa yozma):
{"DONE": true, "yuk": "...", "qayerdan": "...", "qayerga": "...", "ogirlik": "...", "narx": "...", "yuklash_san": "...", "telefon": "..."}
- Agar mijoz noaniq yozsa, aniqlashtir
- Qisqa va do'stona javob ber"""

DRIVER_SYSTEM = """Sen CELC Logistics kompaniyasining aqlli dispetcherisan.
Haydovchi bilan o'zbek tilida suhbatlashasan.

Mavjud regionlar: {regions}

Haydovchi o'z marshruti yoki manzilini aytganda:
1. Uning yo'nalishini tushun (qayerdan qayerga ketayotgani)
2. Mos keladigan yuklar borligini tekshir
3. JSON formatda qaytар (boshqa hech narsa yozma):
{{"SEARCH": true, "qayerdan": "...", "qayerga": "..."}}

Agar haydovchi boshqa narsa so'rasa (holat, yordam va h.k.) - oddiy javob ber.
Qisqa va do'stona javob ber."""

# ─── Telegram helpers ─────────────────────────────────────────────────────────
def send_message(chat_id, text, reply_markup=None):
    if not chat_id or not text: return None
    payload = {"chat_id": chat_id, "text": str(text)[:4096], "parse_mode": "HTML"}
    if reply_markup: payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(f"{API_BASE}/sendMessage", json=payload, timeout=10)
        return r.json()
    except Exception as e:
        logger.error("[TG] %s: %s", chat_id, e)
        return None

def edit_message(chat_id, message_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id,
               "text": str(text)[:4096], "parse_mode": "HTML"}
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
def format_order(order_num, yuk, qayerdan, qayerga, ogirlik, narx, yuklash_san, telefon, holat="Yangi"):
    emoji = "🟢" if holat == "Yangi" else "🔴" if "qabul" in holat.lower() else "✅"
    return (
        f"📦 <b>Yangi yuk #{order_num}</b>\n\n"
        f"🗂 <b>Yuk:</b> {yuk}\n"
        f"📍 <b>Qayerdan:</b> {qayerdan}\n"
        f"📍 <b>Qayerga:</b> {qayerga}\n"
        f"⚖️ <b>Og'irlik:</b> {ogirlik}\n"
        f"💰 <b>Taklif qilinayotgan narx:</b> {narx}\n"
        f"📅 <b>Yuklash sanasi:</b> {yuklash_san}\n"
        f"{emoji} <b>Holati:</b> {holat}\n"
        f"📞 <b>Bog'lanish:</b> {telefon}"
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

def region_keyboard(order_id):
    buttons = []
    row = []
    for i, region in enumerate(REGION_NAMES):
        row.append({"text": region, "callback_data": f"region|{order_id}|{region}"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row: buttons.append(row)
    return {"inline_keyboard": buttons}

def role_keyboard():
    return {"inline_keyboard": [[
        {"text": "📦 Yuk beruvchi (mijoz)", "callback_data": "role|client"},
        {"text": "🚚 Haydovchi",            "callback_data": "role|driver"}
    ]]}

# ─── Detect region from text ──────────────────────────────────────────────────
def detect_region(text):
    text_lower = text.lower()
    mapping = {
        "Buxoro":            ["buxoro", "бухара", "buxara"],
        "Farg'ona":          ["farg'ona", "fargona", "fergana", "фергана"],
        "Samarqand":         ["samarqand", "самарканд"],
        "Toshkent viloyati": ["toshkent vil", "toshkent region", "ташкентская область"],
        "Toshkent shahar":   ["toshkent", "ташкент"],
        "Namangan":          ["namangan", "наманган"],
        "Navoiy":            ["navoiy", "навои"],
        "Jizzax":            ["jizzax", "джизак"],
        "Qashqadaryo":       ["qashqa", "kashka", "қашқа"],
        "Andijon":           ["andijon", "андижан"],
        "Xorazm":            ["xorazm", "xorezm", "хорезм"],
        "Sirdaryo":          ["sirdaryo", "сырдарья"],
        "Surxondaryo":       ["surxon", "сурхан"],
        "Qirg'iziston":      ["qirg'iz", "киргиз", "kyrgyz"],
        "Qoraqalpog'iston":  ["qoraqalp", "каракалп"],
    }
    for region, keywords in mapping.items():
        for kw in keywords:
            if kw in text_lower:
                return region
    return None

# ─── Search orders for driver ─────────────────────────────────────────────────
def find_orders_for_driver(qayerdan, qayerga):
    with get_db() as conn:
        # Ищем заявки где qayerdan совпадает с началом маршрута водителя
        orders = qall(conn, """SELECT * FROM orders WHERE status='yangi'
            ORDER BY created_at DESC LIMIT 20""")

    if not orders:
        return []

    matched = []
    qd_lower = qayerdan.lower() if qayerdan else ""
    qg_lower = qayerga.lower() if qayerga else ""

    for o in orders:
        o_qd = (o["qayerdan"] or "").lower()
        o_qg = (o["qayerga"] or "").lower()
        # Проверяем совпадение по ключевым словам
        match_from = any(w in o_qd for w in qd_lower.split()) if qd_lower else True
        match_to   = any(w in o_qg for w in qg_lower.split()) if qg_lower else True
        if match_from or match_to:
            matched.append(o)

    return matched[:5]  # Максимум 5 заявок

# ─── Handle client AI conversation ───────────────────────────────────────────
def handle_client_message(chat_id, user_id, text, user_label):
    role, history, order_data = get_conv(user_id)

    # Добавляем сообщение пользователя в историю
    history.append({"role": "user", "content": text})

    # Спрашиваем Claude
    reply = ask_claude(CLIENT_SYSTEM, history)

    if not reply:
        send_message(chat_id, "Uzr, texnik xatolik. Qaytadan urinib ko'ring.")
        return

    # Проверяем — вернул ли Claude JSON с DONE
    try:
        # Ищем JSON в ответе
        import re
        json_match = re.search(r'\{.*"DONE".*\}', reply, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            if data.get("DONE"):
                # Все данные собраны — сохраняем заявку как черновик
                order_num = next_order_num()
                with get_db() as conn:
                    qrun(conn, """INSERT INTO orders
                        (order_num,yuk,qayerdan,qayerga,ogirlik,narx,yuklash_san,telefon,status)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'draft')""",
                        [order_num, data.get("yuk",""), data.get("qayerdan",""),
                         data.get("qayerga",""), data.get("ogirlik",""),
                         data.get("narx",""), data.get("yuklash_san",""),
                         data.get("telefon","")])
                    order = qone(conn, "SELECT order_id FROM orders WHERE order_num=%s", [order_num])

                order_id = order["order_id"]
                preview = format_order(order_num, data.get("yuk",""), data.get("qayerdan",""),
                                       data.get("qayerga",""), data.get("ogirlik",""),
                                       data.get("narx",""), data.get("yuklash_san",""),
                                       data.get("telefon",""))

                clear_conv(user_id)
                send_message(chat_id,
                    f"✅ <b>Zo'r! Ma'lumotlar to'liq yig'ildi:</b>\n\n{preview}\n\n"
                    f"Qaysi region chatiga yuborishni tanlang 👇",
                    reply_markup=region_keyboard(order_id))
                return
    except Exception as e:
        logger.error("[Parse] %s", e)

    # Обычный ответ Claude — продолжаем разговор
    history.append({"role": "assistant", "content": reply})
    save_conv(user_id, "client", history, order_data)
    send_message(chat_id, reply)

# ─── Handle driver AI conversation ───────────────────────────────────────────
def handle_driver_message(chat_id, user_id, text, user_label):
    role, history, order_data = get_conv(user_id)

    history.append({"role": "user", "content": text})

    system = DRIVER_SYSTEM.format(regions=", ".join(REGION_NAMES))
    reply = ask_claude(system, history)

    if not reply:
        send_message(chat_id, "Uzr, texnik xatolik. Qaytadan urinib ko'ring.")
        return

    try:
        import re
        json_match = re.search(r'\{.*"SEARCH".*\}', reply, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            if data.get("SEARCH"):
                qayerdan = data.get("qayerdan", "")
                qayerga  = data.get("qayerga", "")

                orders = find_orders_for_driver(qayerdan, qayerga)

                history.append({"role": "assistant", "content": reply})
                save_conv(user_id, "driver", history, {})

                if not orders:
                    send_message(chat_id,
                        f"📭 <b>Hozirda {qayerdan} → {qayerga} yo'nalishi uchun yuklar yo'q.</b>\n\n"
                        f"Yangi yuklar kelganda xabar beraman! 🔔")
                    return

                send_message(chat_id,
                    f"📋 <b>{qayerdan} → {qayerga} yo'nalishi bo'yicha {len(orders)} ta yuk topildi:</b>")
                for o in orders:
                    send_message(chat_id,
                        format_order(o["order_num"], o["yuk"], o["qayerdan"], o["qayerga"],
                                     o["ogirlik"], o["narx"], o["yuklash_san"], o["telefon"]),
                        reply_markup=driver_keyboard(o["order_id"]))
                return
    except Exception as e:
        logger.error("[DriverParse] %s", e)

    history.append({"role": "assistant", "content": reply})
    save_conv(user_id, "driver", history, {})
    send_message(chat_id, reply)

# ─── Main message handler ─────────────────────────────────────────────────────
def handle_message(msg):
    sender  = msg.get("from", {})
    chat_id = msg.get("chat", {}).get("id") or sender.get("id")
    user_id = sender.get("id")
    text    = (msg.get("text") or "").strip()
    user_label = get_user_label(sender)

    if not text: return

    # /start — выбор роли
    if text == "/start":
        clear_conv(user_id)
        send_message(chat_id,
            "👋 <b>CELC Logistics botiga xush kelibsiz!</b>\n\n"
            "Siz kim sifatida kiryapsiz?",
            reply_markup=role_keyboard())
        return

    # /yangi_yuk — быстрый старт для клиента
    if text == "/yangi_yuk":
        clear_conv(user_id)
        save_conv(user_id, "client", [], {})
        send_message(chat_id,
            "📦 <b>Yangi yuk joylash</b>\n\n"
            "Yukingiz haqida gapirib bering. Masalan:\n"
            "<i>\"Toshkentdan Samarqandga 10 tonna g'isht\"</i>")
        return

    # /yuklar — быстрый старт для водителя
    if text == "/yuklar":
        clear_conv(user_id)
        save_conv(user_id, "driver", [], {})
        send_message(chat_id,
            "🚚 <b>Haydovchi rejimi</b>\n\n"
            "Qayerdan qayerga ketayotganingizni yozing. Masalan:\n"
            "<i>\"Men Toshkentdan Farg'onaga ketyapman\"</i>")
        return

    # /statistika
    if text == "/statistika" and chat_id == ADMIN_ID:
        with get_db() as conn:
            total = qone(conn, "SELECT COUNT(*) as c FROM orders WHERE status != 'draft'")["c"]
            yangi = qone(conn, "SELECT COUNT(*) as c FROM orders WHERE status='yangi'")["c"]
            qabul = qone(conn, "SELECT COUNT(*) as c FROM orders WHERE status='qabul'")["c"]
            done  = qone(conn, "SELECT COUNT(*) as c FROM orders WHERE status='yetkazildi'")["c"]
            today = qone(conn, "SELECT COUNT(*) as c FROM orders WHERE DATE(created_at)=CURRENT_DATE AND status!='draft'")["c"]
        send_message(chat_id,
            f"📊 <b>Statistika</b>\n\n"
            f"📅 Bugun: <b>{today}</b>\n"
            f"📦 Jami: <b>{total}</b>\n\n"
            f"🟢 Yangi: <b>{yangi}</b>\n"
            f"🔴 Qabul qilingan: <b>{qabul}</b>\n"
            f"✅ Yetkazildi: <b>{done}</b>")
        return

    # Определяем роль пользователя и направляем
    role, history, order_data = get_conv(user_id)

    if role == "client":
        handle_client_message(chat_id, user_id, text, user_label)
    elif role == "driver":
        handle_driver_message(chat_id, user_id, text, user_label)
    else:
        # Роль не выбрана
        send_message(chat_id,
            "Davom etish uchun /start ni bosing 👇")

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

    # Выбор роли
    if cb_data.startswith("role|"):
        role = cb_data.split("|")[1]
        clear_conv(user_id)
        save_conv(user_id, role, [], {})

        if role == "client":
            edit_message(chat_id, message_id,
                "📦 <b>Yuk beruvchi rejimi</b>\n\n"
                "Yukingiz haqida gapirib bering. Masalan:\n"
                "<i>\"Toshkentdan Samarqandga 10 tonna g'isht\"</i>\n\n"
                "Yoki to'liqroq:\n"
                "<i>\"Menda 15 tonna temir bor, Andijondan Toshkentga, narx 3 million\"</i>")
        else:
            edit_message(chat_id, message_id,
                "🚚 <b>Haydovchi rejimi</b>\n\n"
                "Qayerdan qayerga ketayotganingizni yozing. Masalan:\n"
                "<i>\"Men Toshkentdan Farg'onaga ketyapman\"</i>\n\n"
                "Yoki shunchaki:\n"
                "<i>\"Navoiyga ketaman, yuk bormi?\"</i>")
        return

    # Выбор региона для отправки заявки
    if cb_data.startswith("region|"):
        parts = cb_data.split("|", 2)
        order_id = int(parts[1])
        region   = parts[2]

        with get_db() as conn:
            qrun(conn, "UPDATE orders SET region=%s, status='yangi', updated_at=NOW() WHERE order_id=%s",
                 [region, order_id])
            order = qone(conn, "SELECT * FROM orders WHERE order_id=%s", [order_id])

        if not order:
            send_message(chat_id, "❌ Xatolik.")
            return

        region_chat_id = REGIONS.get(region, 0)
        order_text = format_order(order["order_num"], order["yuk"], order["qayerdan"],
                                  order["qayerga"], order["ogirlik"], order["narx"],
                                  order["yuklash_san"], order["telefon"])

        if region_chat_id:
            result = send_message(region_chat_id, order_text, reply_markup=driver_keyboard(order_id))
            if result and result.get("ok"):
                msg_id = result["result"]["message_id"]
                with get_db() as conn:
                    qrun(conn, "UPDATE orders SET chat_msg_id=%s WHERE order_id=%s", [msg_id, order_id])
                edit_message(chat_id, message_id,
                    f"✅ <b>Yuk #{order['order_num']} muvaffaqiyatli yuborildi!</b>\n\n"
                    f"📍 Region: <b>{region}</b>\n"
                    f"🚚 Haydovchilar ko'rmoqda...\n\n"
                    f"Yangi yuk joylash uchun /yangi_yuk")
            else:
                edit_message(chat_id, message_id,
                    f"⚠️ {region} chatiga yuborib bo'lmadi. Chat ID sozlanmagan.\n"
                    f"Yuk #{order['order_num']} saqlanди.")
        else:
            edit_message(chat_id, message_id,
                f"⚠️ {region} chat ID hali sozlanmagan.\n"
                f"Yuk #{order['order_num']} bazaga saqlandi.")

        if ADMIN_ID:
            send_message(ADMIN_ID,
                f"📦 <b>Yangi yuk #{order['order_num']}</b>\n"
                f"📍 {order['qayerdan']} → {order['qayerga']}\n"
                f"🗂 {order['yuk']} | {order['ogirlik']}\n"
                f"🌍 Region: {region}")
        return

    # Водитель принимает заявку
    if cb_data.startswith("accept|"):
        order_id = int(cb_data.split("|")[1])
        with get_db() as conn:
            order = qone(conn, "SELECT * FROM orders WHERE order_id=%s", [order_id])
            if not order:
                send_message(chat_id, "❌ Yuk topilmadi.")
                return
            if order["status"] != "yangi":
                send_message(chat_id, "⚠️ Bu yuk allaqachon qabul qilingan!")
                return
            qrun(conn, """UPDATE orders SET status='qabul', driver_id=%s, driver_name=%s, updated_at=NOW()
                WHERE order_id=%s AND status='yangi'""", [user_id, user_label, order_id])
            # Перепроверяем — вдруг другой водитель успел раньше
            updated = qone(conn, "SELECT driver_id FROM orders WHERE order_id=%s", [order_id])

        if updated["driver_id"] != user_id:
            send_message(chat_id, "⚠️ Bu yuk boshqa haydovchi tomonidan qabul qilindi!")
            return

        # Обновляем сообщение в региональном чате
        region_chat_id = REGIONS.get(order["region"], 0)
        if region_chat_id and order["chat_msg_id"]:
            new_text = format_order(order["order_num"], order["yuk"], order["qayerdan"],
                                    order["qayerga"], order["ogirlik"], order["narx"],
                                    order["yuklash_san"], order["telefon"], "Qabul qilindi 🔴")
            new_text += f"\n\n🚚 <b>Haydovchi:</b> {user_label}"
            edit_message(region_chat_id, order["chat_msg_id"], new_text)

        send_message(chat_id,
            f"✅ <b>Yuk #{order['order_num']} qabul qilindi!</b>\n\n"
            f"📞 Mijoz telefoni: <b>{order['telefon']}</b>\n"
            f"📍 <b>{order['qayerdan']} → {order['qayerga']}</b>\n"
            f"🗂 Yuk: {order['yuk']} | {order['ogirlik']}\n"
            f"💰 Narx: {order['narx']}\n\n"
            f"Yuk yetkazilgandan so'ng tasdiqlang 👇",
            reply_markup=confirm_keyboard(order_id))

        if ADMIN_ID:
            send_message(ADMIN_ID,
                f"🚚 <b>Yuk #{order['order_num']} qabul qilindi</b>\n"
                f"👤 Haydovchi: {user_label}\n"
                f"📍 {order['qayerdan']} → {order['qayerga']}")
        return

    # Доставлено
    if cb_data.startswith("delivered|"):
        order_id = int(cb_data.split("|")[1])
        with get_db() as conn:
            order = qone(conn, "SELECT * FROM orders WHERE order_id=%s", [order_id])
            if not order: return
            qrun(conn, "UPDATE orders SET status='yetkazildi', updated_at=NOW() WHERE order_id=%s", [order_id])

        send_message(chat_id, f"✅ <b>Rahmat! Yuk #{order['order_num']} yetkazildi deb belgilandi!</b> 🎉")

        if ADMIN_ID:
            send_message(ADMIN_ID,
                f"✅ <b>Yuk #{order['order_num']} yetkazildi!</b>\n"
                f"🚚 Haydovchi: {order['driver_name']}\n"
                f"📍 {order['qayerdan']} → {order['qayerga']}\n"
                f"💰 Narx: {order['narx']}")
        return

    # Проблема
    if cb_data.startswith("problem|"):
        order_id = int(cb_data.split("|")[1])
        with get_db() as conn:
            order = qone(conn, "SELECT * FROM orders WHERE order_id=%s", [order_id])
        send_message(chat_id,
            f"⚠️ <b>Yuk #{order['order_num']} - Muammo haqida xabar berildi</b>\n\n"
            f"Dispatcher siz bilan bog'lanadi. Muammoni batafsil yozing.")
        if ADMIN_ID:
            send_message(ADMIN_ID,
                f"🚨 <b>MUAMMO! Yuk #{order['order_num']}</b>\n"
                f"🚚 Haydovchi: {user_label}\n"
                f"📍 {order['qayerdan']} → {order['qayerga']}\n"
                f"📞 Mijoz: {order['telefon']}")
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
        json={"url": endpoint, "allowed_updates": ["message", "callback_query"]}, timeout=10)
    logger.info("Webhook: %s -> %s", endpoint, resp.json().get("ok"))
    requests.post(f"{API_BASE}/setMyCommands", json={"commands": [
        {"command": "start",        "description": "Boshlash"},
        {"command": "yangi_yuk",    "description": "📦 Yangi yuk joylash"},
        {"command": "yuklar",       "description": "🚚 Yuklar qidirish (haydovchi)"},
        {"command": "statistika",   "description": "📊 Statistika (admin)"},
    ]}, timeout=10)

try:
    init_db()
    set_webhook()
    logger.info("[Bot] CELC AI Logistics bot started!")
except Exception as e:
    logger.error("Startup error: %s", e)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
