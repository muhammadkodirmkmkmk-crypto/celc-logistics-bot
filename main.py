import os, json, logging, urllib.parse
from datetime import datetime
from contextlib import contextmanager
from flask import Flask, request
import requests
import pg8000

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)
app = Flask(__name__)

BOT_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
WEBHOOK_URL  = os.environ["WEBHOOK_URL"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_ID     = int(os.environ.get("ADMIN_ID", "0"))

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ─── Регионы и их чат ID ──────────────────────────────────────────────────────
# Заполнить реальными ID когда клиент даст
REGIONS = {
    "Buxoro":           int(os.environ.get("CHAT_BUXORO", "0")),
    "Farg'ona":         int(os.environ.get("CHAT_FARGONA", "0")),
    "Samarqand":        int(os.environ.get("CHAT_SAMARQAND", "0")),
    "Toshkent viloyati":int(os.environ.get("CHAT_TOSHKENT_VIL", "0")),
    "Toshkent shahar":  int(os.environ.get("CHAT_TOSHKENT_SHR", "0")),
    "Namangan":         int(os.environ.get("CHAT_NAMANGAN", "0")),
    "Navoiy":           int(os.environ.get("CHAT_NAVOIY", "0")),
    "Jizzax":           int(os.environ.get("CHAT_JIZZAX", "0")),
    "Qashqadaryo":      int(os.environ.get("CHAT_QASHQA", "0")),
    "Andijon":          int(os.environ.get("CHAT_ANDIJON", "0")),
    "Xorazm":           int(os.environ.get("CHAT_XORAZM", "0")),
    "Sirdaryo":         int(os.environ.get("CHAT_SIRDARYO", "0")),
    "Surxondaryo":      int(os.environ.get("CHAT_SURXON", "0")),
    "Qirg'iziston":     int(os.environ.get("CHAT_KIRGIZ", "0")),
    "Qoraqalpog'iston": int(os.environ.get("CHAT_QORAQALP", "0")),
}

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
        qrun(conn, """CREATE TABLE IF NOT EXISTS user_states (
            user_id     BIGINT PRIMARY KEY,
            state       TEXT DEFAULT '',
            data        TEXT DEFAULT '{}',
            updated_at  TIMESTAMP DEFAULT NOW()
        )""")
        qrun(conn, """CREATE TABLE IF NOT EXISTS counters (
            name  TEXT PRIMARY KEY,
            value INT DEFAULT 0
        )""")
        qrun(conn, "INSERT INTO counters (name, value) VALUES ('order_num', 800) ON CONFLICT DO NOTHING")
    logger.info("[DB] Tables ready")

def next_order_num():
    with get_db() as conn:
        qrun(conn, "UPDATE counters SET value = value + 1 WHERE name = 'order_num'")
        row = qone(conn, "SELECT value FROM counters WHERE name = 'order_num'")
        return row["value"]

# ─── Telegram helpers ─────────────────────────────────────────────────────────
def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    if not chat_id or not text: return None
    payload = {"chat_id": chat_id, "text": str(text)[:4096], "parse_mode": parse_mode}
    if reply_markup: payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(f"{API_BASE}/sendMessage", json=payload, timeout=10)
        return r.json()
    except Exception as e:
        logger.error("[TG] sendMessage %s: %s", chat_id, e)
        return None

def edit_message(chat_id, message_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": str(text)[:4096], "parse_mode": "HTML"}
    if reply_markup: payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(f"{API_BASE}/editMessageText", json=payload, timeout=10)
    except Exception as e:
        logger.error("[TG] editMessage: %s", e)

def answer_callback(cq_id, text=""):
    try:
        requests.post(f"{API_BASE}/answerCallbackQuery",
                      json={"callback_query_id": cq_id, "text": text}, timeout=10)
    except Exception as e:
        logger.error("[TG] answerCB: %s", e)

def get_user_label(u):
    uname = u.get("username")
    return f"@{uname}" if uname else (
        f"{u.get('first_name','')} {u.get('last_name','')}".strip() or str(u.get("id","?")))

# ─── State management ─────────────────────────────────────────────────────────
def set_state(user_id, state, data=None):
    with get_db() as conn:
        qrun(conn, """INSERT INTO user_states (user_id, state, data, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (user_id) DO UPDATE SET state=%s, data=%s, updated_at=NOW()""",
            [user_id, state, json.dumps(data or {}), state, json.dumps(data or {})])

def get_state(user_id):
    with get_db() as conn:
        row = qone(conn, "SELECT state, data FROM user_states WHERE user_id=%s", [user_id])
        if row:
            return row["state"], json.loads(row["data"]) if row["data"] else {}
        return "", {}

def clear_state(user_id):
    with get_db() as conn:
        qrun(conn, "DELETE FROM user_states WHERE user_id=%s", [user_id])

# ─── Форматирование заявки ────────────────────────────────────────────────────
def format_order(order_num, yuk, qayerdan, qayerga, ogirlik, narx, yuklash_san, telefon, status="Yangi"):
    status_emoji = "🟢" if status == "Yangi" else "🔴" if status == "qabul" else "✅"
    return (
        f"📦 <b>Yangi yuk #{order_num}</b>\n\n"
        f"🗂 <b>Yuk:</b> {yuk}\n"
        f"📍 <b>Qayerdan:</b> {qayerdan}\n"
        f"📍 <b>Qayerga:</b> {qayerga}\n"
        f"⚖️ <b>Og'irlik:</b> {ogirlik}\n"
        f"💰 <b>Taklif qilinayotgan narx:</b> {narx}\n"
        f"📅 <b>Yuklash sanasi:</b> {yuklash_san}\n"
        f"{status_emoji} <b>Holati:</b> {status}\n"
        f"📞 <b>Bog'lanish:</b> {telefon}"
    )

def region_keyboard(order_id):
    """Кнопки выбора региона для новой заявки"""
    buttons = []
    row = []
    for i, region in enumerate(REGIONS.keys()):
        row.append({"text": region, "callback_data": f"region|{order_id}|{region}"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return {"inline_keyboard": buttons}

def driver_keyboard(order_id):
    """Кнопка для водителя — принять заявку"""
    return {"inline_keyboard": [[
        {"text": "✅ Qabul qilish", "callback_data": f"accept|{order_id}"}
    ]]}

def confirm_keyboard(order_id):
    """Кнопка подтверждения доставки"""
    return {"inline_keyboard": [[
        {"text": "✅ Yuk yetkazildi", "callback_data": f"delivered|{order_id}"},
        {"text": "❌ Muammo bor", "callback_data": f"problem|{order_id}"}
    ]]}

# ─── Создание заявки — шаги ───────────────────────────────────────────────────
STEPS = ["yuk", "qayerdan", "qayerga", "ogirlik", "narx", "yuklash_san", "telefon"]
STEP_QUESTIONS = {
    "yuk":        "🗂 Yuk nomi nima? (masalan: Komir, Tola, G'isht...)",
    "qayerdan":   "📍 Qayerdan? (shahar/tuman)",
    "qayerga":    "📍 Qayerga? (shahar/tuman)",
    "ogirlik":    "⚖️ Og'irligi? (masalan: 10 t, 5000 kg)",
    "narx":       "💰 Taklif narxi? (masalan: 2,000,000 so'm)",
    "yuklash_san":"📅 Yuklash sanasi? (masalan: 20.06.2026)",
    "telefon":    "📞 Bog'lanish raqami?",
}

# ─── Handlers ─────────────────────────────────────────────────────────────────
def handle_message(msg):
    sender  = msg.get("from", {})
    chat_id = msg.get("chat", {}).get("id") or sender.get("id")
    user_id = sender.get("id")
    text    = (msg.get("text") or "").strip()
    user_label = get_user_label(sender)

    # /start
    if text == "/start":
        clear_state(user_id)
        send_message(chat_id,
            "👋 <b>CELC Logistics botiga xush kelibsiz!</b>\n\n"
            "Quyidagi buyruqlardan foydalaning:\n\n"
            "📦 /yangi_yuk — yangi yuk joylash\n"
            "📋 /mening_yuklar — mening yuklarim\n"
            "🚚 /yuklar — mavjud yuklar (haydovchilar uchun)\n"
            "📊 /statistika — statistika (admin)")
        return

    # /yangi_yuk — начать создание заявки
    if text == "/yangi_yuk":
        clear_state(user_id)
        set_state(user_id, "new_order_yuk", {"answers": {}})
        send_message(chat_id,
            "📦 <b>Yangi yuk joylash</b>\n\n" + STEP_QUESTIONS["yuk"])
        return

    # /yuklar — водитель запрашивает заявки
    if text == "/yuklar":
        with get_db() as conn:
            orders = qall(conn, """SELECT * FROM orders WHERE status='yangi'
                ORDER BY created_at DESC LIMIT 10""")
        if not orders:
            send_message(chat_id, "📭 Hozirda mavjud yuklar yo'q.")
            return
        send_message(chat_id, f"📋 <b>Mavjud yuklar: {len(orders)} ta</b>")
        for o in orders:
            text_order = format_order(
                o["order_num"], o["yuk"], o["qayerdan"], o["qayerga"],
                o["ogirlik"], o["narx"], o["yuklash_san"], o["telefon"])
            send_message(chat_id, text_order, reply_markup=driver_keyboard(o["order_id"]))
        return

    # /mening_yuklar
    if text == "/mening_yuklar":
        with get_db() as conn:
            orders = qall(conn, """SELECT * FROM orders WHERE driver_id=%s
                ORDER BY created_at DESC LIMIT 10""", [user_id])
        if not orders:
            send_message(chat_id, "📭 Sizda hali yuklar yo'q.")
            return
        send_message(chat_id, f"📋 <b>Sizning yuklaringiz: {len(orders)} ta</b>")
        for o in orders:
            status_text = "🟢 Yangi" if o["status"]=="yangi" else "🔴 Qabul qilingan" if o["status"]=="qabul" else "✅ Yetkazildi"
            send_message(chat_id,
                format_order(o["order_num"], o["yuk"], o["qayerdan"], o["qayerga"],
                             o["ogirlik"], o["narx"], o["yuklash_san"], o["telefon"], status_text))
        return

    # /statistika — только для админа
    if text == "/statistika" and chat_id == ADMIN_ID:
        with get_db() as conn:
            total   = qone(conn, "SELECT COUNT(*) as c FROM orders")["c"]
            yangi   = qone(conn, "SELECT COUNT(*) as c FROM orders WHERE status='yangi'")["c"]
            qabul   = qone(conn, "SELECT COUNT(*) as c FROM orders WHERE status='qabul'")["c"]
            done    = qone(conn, "SELECT COUNT(*) as c FROM orders WHERE status='yetkazildi'")["c"]
            today   = qone(conn, "SELECT COUNT(*) as c FROM orders WHERE DATE(created_at)=CURRENT_DATE")["c"]
        send_message(chat_id,
            f"📊 <b>Statistika</b>\n\n"
            f"📅 Bugun: <b>{today}</b>\n"
            f"📦 Jami: <b>{total}</b>\n\n"
            f"🟢 Yangi: <b>{yangi}</b>\n"
            f"🔴 Qabul qilingan: <b>{qabul}</b>\n"
            f"✅ Yetkazildi: <b>{done}</b>")
        return

    # Обработка шагов создания заявки
    state, data = get_state(user_id)
    if state and state.startswith("new_order_"):
        current_step = state.replace("new_order_", "")
        answers = data.get("answers", {})
        answers[current_step] = text

        # Определяем следующий шаг
        idx = STEPS.index(current_step)
        if idx + 1 < len(STEPS):
            next_step = STEPS[idx + 1]
            set_state(user_id, f"new_order_{next_step}", {"answers": answers})
            send_message(chat_id, STEP_QUESTIONS[next_step])
        else:
            # Все шаги пройдены — показываем превью и выбор региона
            order_num = next_order_num()
            with get_db() as conn:
                qrun(conn, """INSERT INTO orders
                    (order_num, yuk, qayerdan, qayerga, ogirlik, narx, yuklash_san, telefon, status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'draft')""",
                    [order_num, answers.get("yuk",""), answers.get("qayerdan",""),
                     answers.get("qayerga",""), answers.get("ogirlik",""),
                     answers.get("narx",""), answers.get("yuklash_san",""),
                     answers.get("telefon","")])
                order = qone(conn, "SELECT order_id FROM orders WHERE order_num=%s", [order_num])

            order_id = order["order_id"]
            preview = format_order(order_num, answers.get("yuk",""), answers.get("qayerdan",""),
                                   answers.get("qayerga",""), answers.get("ogirlik",""),
                                   answers.get("narx",""), answers.get("yuklash_san",""),
                                   answers.get("telefon",""))
            clear_state(user_id)
            send_message(chat_id,
                f"✅ <b>Yuk ma'lumotlari:</b>\n\n{preview}\n\n"
                f"📍 Endi qaysi region chatiga yuborishni tanlang:")
            send_message(chat_id, "👇 Region tanlang:", reply_markup=region_keyboard(order_id))
        return

def handle_callback(cb):
    cb_id      = cb["id"]
    cb_data    = cb.get("data", "")
    chat_id    = cb["message"]["chat"]["id"]
    message_id = cb["message"]["message_id"]
    user       = cb.get("from", {})
    user_id    = user.get("id")
    user_label = get_user_label(user)

    answer_callback(cb_id)

    # Выбор региона
    if cb_data.startswith("region|"):
        _, order_id, region = cb_data.split("|", 2)
        order_id = int(order_id)

        with get_db() as conn:
            qrun(conn, "UPDATE orders SET region=%s, status='yangi', updated_at=NOW() WHERE order_id=%s",
                 [region, order_id])
            order = qone(conn, "SELECT * FROM orders WHERE order_id=%s", [order_id])

        if not order:
            send_message(chat_id, "❌ Xatolik: yuk topilmadi.")
            return

        # Отправляем в региональный чат
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
                # Редактируем сообщение с кнопками регионов
                edit_message(chat_id, message_id, f"✅ <b>Yuk #{order['order_num']} {region} chatiga yuborildi!</b>")
                send_message(chat_id, "📦 Yangi yuk joylash uchun /yangi_yuk")
            else:
                edit_message(chat_id, message_id,
                    f"⚠️ {region} chatiga yuborib bo'lmadi. Chat ID sozlanmagan bo'lishi mumkin.\n\n"
                    f"Yuk #{order['order_num']} saqlanди.")
        else:
            edit_message(chat_id, message_id,
                f"⚠️ {region} chat ID hali sozlanmagan.\n\nYuk #{order['order_num']} saqlanди.")
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
                answer_callback(cb_id, "⚠️ Bu yuk allaqachon qabul qilingan!")
                send_message(chat_id, "⚠️ Bu yuk allaqachon boshqa haydovchi tomonidan qabul qilingan.")
                return
            qrun(conn, """UPDATE orders SET status='qabul', driver_id=%s, driver_name=%s, updated_at=NOW()
                WHERE order_id=%s""", [user_id, user_label, order_id])

        # Редактируем сообщение в региональном чате — убираем кнопку
        region_chat_id = REGIONS.get(order["region"], 0)
        if region_chat_id and order["chat_msg_id"]:
            new_text = format_order(order["order_num"], order["yuk"], order["qayerdan"],
                                    order["qayerga"], order["ogirlik"], order["narx"],
                                    order["yuklash_san"], order["telefon"], "Qabul qilindi 🔴")
            new_text += f"\n\n🚚 <b>Haydovchi:</b> {user_label}"
            edit_message(region_chat_id, order["chat_msg_id"], new_text)

        # Водителю — подтверждение
        send_message(chat_id,
            f"✅ <b>Yuk #{order['order_num']} qabul qilindi!</b>\n\n"
            f"📞 Mijoz: {order['telefon']}\n"
            f"📍 {order['qayerdan']} → {order['qayerga']}\n\n"
            f"Yuk yetkazilgandan so'ng tasdiqlang:",
            reply_markup=confirm_keyboard(order_id))

        # Уведомление в общий чат и создателю
        if ADMIN_ID:
            send_message(ADMIN_ID,
                f"🚚 <b>Yuk #{order['order_num']} qabul qilindi</b>\n"
                f"👤 Haydovchi: {user_label}\n"
                f"📍 {order['qayerdan']} → {order['qayerga']}")
        return

    # Водитель подтверждает доставку
    if cb_data.startswith("delivered|"):
        order_id = int(cb_data.split("|")[1])
        with get_db() as conn:
            order = qone(conn, "SELECT * FROM orders WHERE order_id=%s", [order_id])
            if not order:
                send_message(chat_id, "❌ Yuk topilmadi.")
                return
            qrun(conn, "UPDATE orders SET status='yetkazildi', updated_at=NOW() WHERE order_id=%s", [order_id])

        send_message(chat_id,
            f"✅ <b>Yuk #{order['order_num']} yetkazildi deb belgilandi!</b>\n\n"
            f"Rahmat! 🎉")

        if ADMIN_ID:
            send_message(ADMIN_ID,
                f"✅ <b>Yuk #{order['order_num']} yetkazildi!</b>\n"
                f"🚚 Haydovchi: {order['driver_name']}\n"
                f"📍 {order['qayerdan']} → {order['qayerga']}\n"
                f"💰 Narx: {order['narx']}")
        return

    # Проблема с доставкой
    if cb_data.startswith("problem|"):
        order_id = int(cb_data.split("|")[1])
        with get_db() as conn:
            order = qone(conn, "SELECT * FROM orders WHERE order_id=%s", [order_id])
        send_message(chat_id,
            f"⚠️ <b>Yuk #{order['order_num']} - Muammo</b>\n\n"
            f"Iltimos, muammoni batafsil yozing va dispatcher bilan bog'laning.")
        if ADMIN_ID:
            send_message(ADMIN_ID,
                f"⚠️ <b>MUAMMO! Yuk #{order['order_num']}</b>\n"
                f"🚚 Haydovchi: {user_label}\n"
                f"📍 {order['qayerdan']} → {order['qayerga']}\n"
                f"📞 Mijoz: {order['telefon']}")
        return

# ─── Flask routes ─────────────────────────────────────────────────────────────
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

    # Команды бота
    requests.post(f"{API_BASE}/setMyCommands", json={"commands": [
        {"command": "start",       "description": "Boshlash"},
        {"command": "yangi_yuk",   "description": "📦 Yangi yuk joylash"},
        {"command": "yuklar",      "description": "🚚 Mavjud yuklar (haydovchilar)"},
        {"command": "mening_yuklar","description": "📋 Mening yuklarim"},
        {"command": "statistika",  "description": "📊 Statistika"},
    ]}, timeout=10)

# ─── Startup ──────────────────────────────────────────────────────────────────
try:
    init_db()
    set_webhook()
    logger.info("[Bot] CELC Logistics bot started!")
except Exception as e:
    logger.error("Startup error: %s", e)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
