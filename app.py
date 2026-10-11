import sqlite3
import requests
import hashlib
import secrets
from datetime import date
from pathlib import Path
from fastapi import FastAPI, Request, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer

CH_API_KEY = "109c448e-0d41-4f59-8651-2eab715f81fa"
TELEGRAM_TOKEN = "8751754421:AAHN2ta1wD-1LAGKf7Juyao2ggmKVqkwqlI"
SECRET_KEY = "super-secret-uk-company-radar-key-change-in-prod"

BASE_URL = "https://api.company-information.service.gov.uk"
BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "companies.db"

GUEST_SEARCH_LIMIT = 3
REGISTERED_SEARCH_LIMIT = 5
MAX_FREE_WATCH_LIMIT = 3

serializer = URLSafeTimedSerializer(SECRET_KEY)

app = FastAPI(title="UK Company Radar")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# --- ŞİFRELEME (PBKDF2-SHA256) ---
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000)
    return f"{salt}\${key.hex()}"

def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt, key_hex = stored_hash.split("\$")
        check_key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000)
        return secrets.compare_digest(check_key.hex(), key_hex)
    except Exception:
        return False

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            telegram_chat_id TEXT,
            search_count INTEGER DEFAULT 0,
            last_search_date TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("PRAGMA table_info(users)")
    columns = [col["name"] for col in cursor.fetchall()]
    if "search_count" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN search_count INTEGER DEFAULT 0")
    if "last_search_date" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN last_search_date TEXT")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS watched_companies (
            company_number TEXT PRIMARY KEY,
            company_name TEXT,
            last_status TEXT,
            last_filing_date TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_watchlist (
            user_id INTEGER,
            company_number TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, company_number),
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (company_number) REFERENCES watched_companies (company_number)
        )
    """)
    conn.commit()
    conn.close()

init_db()

def get_current_user(request: Request):
    token = request.cookies.get("session_token")
    if not token:
        return None
    try:
        user_id = serializer.loads(token, max_age=86400 * 7)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id, email, telegram_chat_id, search_count, last_search_date FROM users WHERE id = ?", (user_id,))
        user = cursor.fetchone()
        conn.close()
        return user
    except Exception:
        return None

def send_telegram_alert(chat_id: str, message: str):
    if not chat_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Telegram error: {e}")

# ================= AUTH ENDPOINTS =================

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if get_current_user(request):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request=request, name="auth.html", context={"mode": "login", "error": None})

@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, password_hash FROM users WHERE email = ?", (email.strip().lower(),))
    user = cursor.fetchone()
    conn.close()

    if not user or not verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(
            request=request, 
            name="auth.html", 
            context={"mode": "login", "error": "Invalid email or password."}
        )

    token = serializer.dumps(user["id"])
    res = RedirectResponse(url="/", status_code=303)
    res.set_cookie(key="session_token", value=token, httponly=True, max_age=86400 * 7)
    return res

@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    if get_current_user(request):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request=request, name="auth.html", context={"mode": "register", "error": None})

@app.post("/register")
def register(request: Request, email: str = Form(...), password: str = Form(...), telegram_chat_id: str = Form(None)):
    email_clean = email.strip().lower()
    hashed = hash_password(password)
    tg_id = telegram_chat_id.strip() if telegram_chat_id else None

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO users (email, password_hash, telegram_chat_id) VALUES (?, ?, ?)", 
                       (email_clean, hashed, tg_id))
        user_id = cursor.lastrowid
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return templates.TemplateResponse(
            request=request, 
            name="auth.html", 
            context={"mode": "register", "error": "An account with this email already exists."}
        )
    conn.close()

    token = serializer.dumps(user_id)
    res = RedirectResponse(url="/", status_code=303)
    res.set_cookie(key="session_token", value=token, httponly=True, max_age=86400 * 7)
    return res

@app.get("/logout")
def logout():
    res = RedirectResponse(url="/", status_code=303)
    res.delete_cookie("session_token")
    return res

# ================= DASHBOARD ENDPOINTS =================

@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = None, limit_reached: bool = False):
    user = get_current_user(request)
    today_str = str(date.today())
    search_results = []
    search_limit_reached = False
    new_guest_cookie_val = None

    if user:
        # KAYITLI KULLANICI MANTIĞI (Max: 5 sorgu)
        daily_limit = REGISTERED_SEARCH_LIMIT
        conn = get_db()
        cursor = conn.cursor()

        user_search_count = user["search_count"] or 0
        if user["last_search_date"] != today_str:
            user_search_count = 0
            cursor.execute("UPDATE users SET search_count = 0, last_search_date = ? WHERE id = ?", (today_str, user["id"]))
            conn.commit()

        cursor.execute("""
            SELECT c.company_number, c.company_name, c.last_status, c.last_filing_date 
            FROM watched_companies c
            INNER JOIN user_watchlist uw ON c.company_number = uw.company_number
            WHERE uw.user_id = ?
        """, (user["id"],))
        companies = cursor.fetchall()

        if q and q.strip():
            if user_search_count >= daily_limit:
                search_limit_reached = True
            else:
                search_url = f"{BASE_URL}/search/companies?q={requests.utils.quote(q.strip())}&items_per_page=5"
                res = requests.get(search_url, auth=(CH_API_KEY, ""), timeout=10)
                if res.status_code == 200:
                    for item in res.json().get("items", []):
                        search_results.append({
                            "name": item.get("title", "Unknown"),
                            "number": item.get("company_number", ""),
                            "status": item.get("company_status", "unknown"),
                            "address": item.get("address_snippet", "")
                        })
                    user_search_count += 1
                    cursor.execute("UPDATE users SET search_count = ?, last_search_date = ? WHERE id = ?", 
                                   (user_search_count, today_str, user["id"]))
                    conn.commit()

        remaining_searches = max(0, daily_limit - user_search_count)
        conn.close()

    else:
        # KAYITSIZ ZİYARETÇİ MANTIĞI (Max: 3 sorgu - Çerez ile takip)
        daily_limit = GUEST_SEARCH_LIMIT
        companies = []
        guest_data = request.cookies.get("guest_searches", f"{today_str}:0")
        
        try:
            cookie_date, count_str = guest_data.split(":")
            guest_count = int(count_str) if cookie_date == today_str else 0
        except Exception:
            guest_count = 0

        if q and q.strip():
            if guest_count >= daily_limit:
                search_limit_reached = True
            else:
                search_url = f"{BASE_URL}/search/companies?q={requests.utils.quote(q.strip())}&items_per_page=5"
                res = requests.get(search_url, auth=(CH_API_KEY, ""), timeout=10)
                if res.status_code == 200:
                    for item in res.json().get("items", []):
                        search_results.append({
                            "name": item.get("title", "Unknown"),
                            "number": item.get("company_number", ""),
                            "status": item.get("company_status", "unknown"),
                            "address": item.get("address_snippet", "")
                        })
                    guest_count += 1
                    new_guest_cookie_val = f"{today_str}:{guest_count}"

        remaining_searches = max(0, daily_limit - guest_count)

    response = templates.TemplateResponse(
        request=request, 
        name="index.html", 
        context={
            "user": user,
            "companies": companies,
            "search_results": search_results,
            "query": q or "",
            "limit_reached": limit_reached,
            "search_limit_reached": search_limit_reached,
            "remaining_searches": remaining_searches,
            "daily_limit": daily_limit
        }
    )

    if new_guest_cookie_val:
        response.set_cookie(key="guest_searches", value=new_guest_cookie_val, max_age=86400)

    return response

@app.post("/track")
def track(request: Request, company_number: str = Form(...)):
    user = get_current_user(request)
    if not user:
        # Kayıtsız kullanıcı şirket takip etmeye çalışırsa doğrudan kayıt olmaya yönlendirilir
        return RedirectResponse(url="/register", status_code=303)

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) as count FROM user_watchlist WHERE user_id = ?", (user["id"],))
    count_row = cursor.fetchone()
    current_count = count_row["count"] if count_row else 0

    if current_count >= MAX_FREE_WATCH_LIMIT:
        conn.close()
        return RedirectResponse(url="/?limit_reached=true", status_code=303)

    comp_no = company_number.strip().upper()
    url = f"{BASE_URL}/company/{comp_no}"
    res = requests.get(url, auth=(CH_API_KEY, ""), timeout=10)

    if res.status_code == 200:
        data = res.json()
        name = data.get("company_name", "Unknown")
        status = data.get("company_status", "unknown")

        cursor.execute("""
            INSERT OR REPLACE INTO watched_companies (company_number, company_name, last_status, last_filing_date)
            VALUES (?, ?, ?, NULL)
        """, (comp_no, name, status))
        cursor.execute("""
            INSERT OR IGNORE INTO user_watchlist (user_id, company_number)
            VALUES (?, ?)
        """, (user["id"], comp_no))
        conn.commit()

    conn.close()
    return RedirectResponse(url="/", status_code=303)

@app.get("/untrack/{company_number}")
def untrack(request: Request, company_number: str):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM user_watchlist WHERE user_id = ? AND company_number = ?", 
                   (user["id"], company_number))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)

@app.get("/scan")
def scan(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT c.company_number, c.last_status, c.last_filing_date 
        FROM watched_companies c
        INNER JOIN user_watchlist uw ON c.company_number = uw.company_number
        WHERE uw.user_id = ?
    """, (user["id"],))
    companies = cursor.fetchall()

    risk_statuses = ["dissolved", "liquidation", "administration", "receivership"]

    for comp in companies:
        comp_no = comp["company_number"]
        url = f"{BASE_URL}/company/{comp_no}"
        res = requests.get(url, auth=(CH_API_KEY, ""), timeout=10)
        if res.status_code != 200:
            continue

        data = res.json()
        name = data.get("company_name", "Unknown")
        current_status = data.get("company_status", "unknown")
        has_insolvency = data.get("has_insolvency_history", False)

        f_res = requests.get(f"{BASE_URL}/company/{comp_no}/filing-history?items_per_page=1", auth=(CH_API_KEY, ""), timeout=10)
        latest_date = None
        latest_desc = "None"
        if f_res.status_code == 200:
            items = f_res.json().get("items", [])
            if items:
                latest_date = items[0].get("date")
                latest_desc = items[0].get("description", "")

        is_risky = current_status in risk_statuses
        status_changed = comp["last_status"] is not None and comp["last_status"] != current_status

        if (is_risky or has_insolvency or status_changed) and user["telegram_chat_id"]:
            alert = (
                f"🚨 *CRITICAL RISK ALERT (PORTFOLIO AUDIT)*\n\n"
                f"🏢 *Entity:* {name}\n"
                f"🔢 *Reg No:* `{comp_no}`\n"
                f"⚠️ *Status:* *{current_status.upper()}*\n"
                f"📋 *Notice:* _{latest_desc}_\n\n"
                f"🔗 [Companies House Profile](https://find-and-update.company-information.service.gov.uk/company/{comp_no})"
            )
            send_telegram_alert(user["telegram_chat_id"], alert)

        cursor.execute("""
            UPDATE watched_companies
            SET company_name = ?, last_status = ?, last_filing_date = ?
            WHERE company_number = ?
        """, (name, current_status, latest_date, comp_no))

    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)