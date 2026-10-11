import os
import re
import sqlite3
import hashlib
import secrets
import asyncio
from pathlib import Path
from datetime import date
from typing import Optional

import requests
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------
# KONFIGURASYON & SABİT DEĞİŞKENLER
# ---------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "companies.db"
BASE_URL = "https://api.company-information.service.gov.uk"

COMPANIES_HOUSE_API_KEY = os.getenv("COMPANIES_HOUSE_API_KEY", "109c448e-0d41-4f59-8651-2eab715f81fa").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8751754421:AAHN2ta1wD-1LAGKf7Juyao2ggmKVqkwqlI").strip()
SECRET_KEY = os.getenv("SECRET_KEY", "super-secret-uk-company-radar-key-change-in-prod")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

ADMIN_EMAILS = [os.getenv("ADMIN_EMAIL", "ayhan.hakyemez@gmail.com").strip().lower()]

app = FastAPI(title="UK Company Radar")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ---------------------------------------------------------
# VERİTABANI YÖNETİMİ (POSTGRESQL / SQLITE HİBRİT)
# ---------------------------------------------------------
USE_PG = bool(DATABASE_URL)
if USE_PG:
    import psycopg2
    import psycopg2.extras
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

def get_db():
    if USE_PG:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        return conn
    else:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        return conn

def format_query(sql: str) -> str:
    if USE_PG:
        return sql.replace("?", "%s")
    return sql

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    if USE_PG:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                telegram_chat_id TEXT,
                is_pro INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS watchlist (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                company_number TEXT NOT NULL,
                company_name TEXT NOT NULL,
                status TEXT NOT NULL,
                type TEXT,
                jurisdiction TEXT,
                last_filing_date TEXT,
                last_filing_description TEXT,
                last_filing_category TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, company_number)
            );
            CREATE TABLE IF NOT EXISTS search_logs (
                id SERIAL PRIMARY KEY,
                identifier TEXT NOT NULL,
                search_date DATE NOT NULL,
                count INTEGER DEFAULT 1,
                UNIQUE(identifier, search_date)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
    else:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                telegram_chat_id TEXT,
                is_pro INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                company_number TEXT NOT NULL,
                company_name TEXT NOT NULL,
                status TEXT NOT NULL,
                type TEXT,
                jurisdiction TEXT,
                last_filing_date TEXT,
                last_filing_description TEXT,
                last_filing_category TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id),
                UNIQUE(user_id, company_number)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS search_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identifier TEXT NOT NULL,
                search_date DATE NOT NULL,
                count INTEGER DEFAULT 1,
                UNIQUE(identifier, search_date)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        """)
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_watchlist_user_company ON watchlist(user_id, company_number)")

    conn.commit()
    conn.close()

# ---------------------------------------------------------
# ŞİFRELEME
# ---------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000)
    return f"{salt}${key.hex()}"

def verify_password(stored_hash: str, password: str) -> bool:
    try:
        salt, key_hex = stored_hash.split("$")
        key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000)
        return key.hex() == key_hex
    except Exception:
        return False

# ---------------------------------------------------------
# TELEGRAM BİLDİRİM
# ---------------------------------------------------------
def send_telegram_sync(chat_id: str, message: str):
    if not chat_id or not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": str(chat_id).strip(),
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[Telegram Alert Error]: {e}")

# ---------------------------------------------------------
# YARDIMCI METODLAR & KULLANICI
# ---------------------------------------------------------
def normalize_company_number(value: str) -> str:
    return (value or "").strip().upper().replace(" ", "")

def is_valid_telegram_chat_id(chat_id: str) -> bool:
    if chat_id is None:
        return False
    return bool(re.fullmatch(r"-?\d{6,20}", str(chat_id).strip()))

def get_current_user(request: Request):
    session_id = request.cookies.get("session_id")
    if not session_id:
        return None
    conn = get_db()
    cursor = conn.cursor()
    query = format_query("""
        SELECT u.id, u.email, u.telegram_chat_id, u.is_pro 
        FROM users u 
        JOIN sessions s ON u.id = s.user_id 
        WHERE s.session_id = ?
    """)
    cursor.execute(query, (session_id,))
    user = cursor.fetchone()
    conn.close()
    
    if not user:
        return None
        
    user_dict = dict(user)
    if user_dict["email"].lower() in ADMIN_EMAILS:
        user_dict["is_pro"] = 1
        user_dict["is_admin"] = True
    else:
        user_dict["is_admin"] = False
        
    return user_dict

def get_search_count(identifier: str) -> int:
    today = date.today().isoformat()
    conn = get_db()
    cursor = conn.cursor()
    query = format_query("SELECT count FROM search_logs WHERE identifier = ? AND search_date = ?")
    cursor.execute(query, (identifier, today))
    row = cursor.fetchone()
    conn.close()
    return row["count"] if row else 0

def increment_search_count(identifier: str):
    today = date.today().isoformat()
    conn = get_db()
    cursor = conn.cursor()
    query = format_query("""
        INSERT INTO search_logs (identifier, search_date, count) 
        VALUES (?, ?, 1)
        ON CONFLICT(identifier, search_date) DO UPDATE SET count = search_logs.count + 1
    """)
    cursor.execute(query, (identifier, today))
    conn.commit()
    conn.close()

# ---------------------------------------------------------
# COMPANIES HOUSE API
# ---------------------------------------------------------
def search_companies_house(query: str):
    if not COMPANIES_HOUSE_API_KEY:
        print("[Companies House Error]: Key missing!")
        return []
    cleaned_query = (query or "").strip()
    if not cleaned_query:
        return []
    url = f"{BASE_URL}/search/companies"
    try:
        res = requests.get(url, params={"q": cleaned_query}, auth=(COMPANIES_HOUSE_API_KEY, ""), timeout=8)
        if res.status_code == 200:
            return res.json().get("items", [])
    except Exception as e:
        print(f"[Companies House Exception]: {e}")
    return []

def get_company_profile(company_number: str):
    if not COMPANIES_HOUSE_API_KEY:
        return None
    normalized_number = normalize_company_number(company_number)
    if not normalized_number:
        return None
    url = f"{BASE_URL}/company/{normalized_number}"
    try:
        res = requests.get(url, auth=(COMPANIES_HOUSE_API_KEY, ""), timeout=8)
        if res.status_code == 200:
            return res.json()
    except Exception as e:
        print(f"[Companies House Profile Exception]: {e}")
    return None

def get_latest_filing_update(company_number: str):
    if not COMPANIES_HOUSE_API_KEY:
        return None, None, None
    normalized_number = normalize_company_number(company_number)
    if not normalized_number:
        return None, None, None
    url = f"{BASE_URL}/company/{normalized_number}/filing-history?items_per_page=1"
    try:
        res = requests.get(url, auth=(COMPANIES_HOUSE_API_KEY, ""), timeout=8)
        if res.status_code != 200:
            return None, None, None
        items = res.json().get("items", [])
        if not items:
            return None, None, None
        item = items[0]
        return (
            item.get("date"),
            item.get("description", ""),
            item.get("category", "")
        )
    except Exception as e:
        print(f"[Companies House Filing Exception]: {e}")
        return None, None, None

# ---------------------------------------------------------
# ARKA PLAN GÖREVİ
# ---------------------------------------------------------
async def automated_background_monitor():
    while True:
        try:
            await asyncio.sleep(3600)
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT w.id, w.user_id, w.company_number, w.company_name, w.status, w.last_filing_date,
                       w.last_filing_description, w.last_filing_category, u.telegram_chat_id
                FROM watchlist w
                JOIN users u ON w.user_id = u.id
            """)
            items = cursor.fetchall()

            for item in items:
                live_profile = get_company_profile(item["company_number"])
                if not live_profile:
                    continue

                new_status = (live_profile.get("company_status") or "unknown").lower()
                old_status = (item["status"] or "unknown").lower()
                latest_filing_date, latest_filing_description, latest_filing_category = get_latest_filing_update(item["company_number"])

                status_changed = old_status != new_status
                filing_changed = (
                    latest_filing_date is not None
                    and latest_filing_date != item["last_filing_date"]
                    and (
                        "insolvency" in (latest_filing_category or "").lower()
                        or "gazette" in (latest_filing_category or "").lower()
                        or "liquidation" in (latest_filing_description or "").lower()
                    )
                )

                if status_changed or filing_changed:
                    if status_changed:
                        q_update = format_query("""
                            UPDATE watchlist 
                            SET status = ?, 
                                last_filing_date = COALESCE(?, last_filing_date), 
                                last_filing_description = COALESCE(?, last_filing_description), 
                                last_filing_category = COALESCE(?, last_filing_category) 
                            WHERE id = ?
                        """)
                        cursor.execute(
                            q_update,
                            (live_profile.get("company_status", "unknown"), latest_filing_date, latest_filing_description, latest_filing_category, item["id"]),
                        )
                        if item["telegram_chat_id"]:
                            alert_msg = (
                                f"🚨 <b>CRITICAL RISK ALERT: STATUS CHANGE DETECTED</b>\n\n"
                                f"<b>Company:</b> {item['company_name']} ({item['company_number']})\n"
                                f"<b>Previous Status:</b> {old_status.upper()}\n"
                                f"<b>New Status:</b> ⚠️ <b>{new_status.upper()}</b>\n\n"
                                f"<i>Immediate attention recommended. Check official Companies House filing.</i>"
                            )
                            send_telegram_sync(item["telegram_chat_id"], alert_msg)
                    if filing_changed and item["telegram_chat_id"]:
                        filing_msg = (
                            f"🚨 <b>INSOLVENCY / GAZETTE ALERT</b>\n\n"
                            f"<b>Company:</b> {item['company_name']} ({item['company_number']})\n"
                            f"<b>Latest filing:</b> {latest_filing_description or 'No description'}\n"
                            f"<b>Category:</b> {latest_filing_category or 'Unknown'}\n"
                            f"<b>Date:</b> {latest_filing_date}\n\n"
                            f"<i>New official notice detected in Companies House filing history.</i>"
                        )
                        send_telegram_sync(item["telegram_chat_id"], filing_msg)

                q_sync = format_query("""
                    UPDATE watchlist 
                    SET company_name = ?, 
                        status = ?, 
                        last_filing_date = COALESCE(?, last_filing_date), 
                        last_filing_description = COALESCE(?, last_filing_description), 
                        last_filing_category = COALESCE(?, last_filing_category) 
                    WHERE id = ?
                """)
                cursor.execute(
                    q_sync,
                    (
                        live_profile.get("company_name", item["company_name"]),
                        live_profile.get("company_status", "unknown"),
                        latest_filing_date,
                        latest_filing_description,
                        latest_filing_category,
                        item["id"],
                    ),
                )

            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[Background Monitor Error]: {e}")

@app.on_event("startup")
async def startup_event():
    init_db()
    asyncio.create_task(automated_background_monitor())

# ---------------------------------------------------------
# ROTALAR
# ---------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request, q: Optional[str] = None):
    user = get_current_user(request)
    client_ip = request.client.host if request.client else "127.0.0.1"
    
    is_pro = bool(user and user.get("is_pro"))
    identifier = f"user_{user['id']}" if user else f"guest_{client_ip}"
    search_count = get_search_count(identifier)
    
    if is_pro:
        search_limit = 999999
        search_limit_reached = False
        remaining_searches = "Unlimited"
    elif user:
        search_limit = 5
        search_limit_reached = search_count >= search_limit
        remaining_searches = max(0, search_limit - search_count)
    else:
        search_limit = 3
        search_limit_reached = search_count >= search_limit
        remaining_searches = max(0, search_limit - search_count)
        
    search_results = []
    if q and not search_limit_reached:
        search_results = search_companies_house(q.strip())
        increment_search_count(identifier)
        if not is_pro:
            search_count += 1
            remaining_searches = max(0, search_limit - search_count)
            if search_count >= search_limit:
                search_limit_reached = True

    watchlist = []
    watchlist_limit_reached = False
    if user:
        conn = get_db()
        cursor = conn.cursor()
        query = format_query("SELECT * FROM watchlist WHERE user_id = ? ORDER BY created_at DESC")
        cursor.execute(query, (user["id"],))
        watchlist = cursor.fetchall()
        conn.close()
        
        if not is_pro and len(watchlist) >= 3:
            watchlist_limit_reached = True

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "user": user,
            "query": q,
            "results": search_results,
            "watchlist": watchlist,
            "search_count": search_count,
            "search_limit": search_limit,
            "search_limit_reached": search_limit_reached,
            "remaining_searches": remaining_searches,
            "watchlist_limit_reached": watchlist_limit_reached,
            "is_pro": is_pro
        }
    )

@app.post("/track")
async def track_company(
    request: Request, 
    company_number: str = Form(...), 
    company_name: str = Form(...), 
    status: str = Form(...),
    type: str = Form(None),
    jurisdiction: str = Form(None)
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth", status_code=303)

    company_number = normalize_company_number(company_number)
    if not company_number or not re.fullmatch(r"[A-Z0-9]{6,10}", company_number):
        return RedirectResponse(url="/?invalid_company=1", status_code=303)

    conn = get_db()
    cursor = conn.cursor()
    q_check = format_query("SELECT id FROM watchlist WHERE user_id = ? AND company_number = ?")
    cursor.execute(q_check, (user["id"], company_number))
    existing = cursor.fetchone()
    if existing:
        conn.close()
        return RedirectResponse(url="/?already_tracked=1", status_code=303)

    q_count = format_query("SELECT COUNT(*) as count FROM watchlist WHERE user_id = ?")
    cursor.execute(q_count, (user["id"],))
    count = cursor.fetchone()["count"]
    
    if not user.get("is_pro") and count >= 3:
        conn.close()
        return RedirectResponse(url="/?limit_reached=1", status_code=303)
        
    q_insert = format_query("""
        INSERT INTO watchlist (user_id, company_number, company_name, status, type, jurisdiction, last_filing_date, last_filing_description, last_filing_category)
        VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
    """)
    cursor.execute(q_insert, (user["id"], company_number, company_name.strip(), status.strip(), type, jurisdiction))
    conn.commit()
    conn.close()
    
    if user.get("telegram_chat_id"):
        welcome_alert = (
            f"🛡️ <b>UK Company Radar | Monitoring Active</b>\n\n"
            f"<b>Company:</b> {company_name.strip()} ({company_number})\n"
            f"<b>Current Legal Status:</b> {status.strip().upper()}\n\n"
            f"✅ <i>24/7 automated insolvency monitoring is now active. You will receive an instant notification here if this entity enters liquidation, administration, or strike-off.</i>"
        )
        send_telegram_sync(user["telegram_chat_id"], welcome_alert)

    return RedirectResponse(url="/", status_code=303)

@app.post("/untrack")
async def untrack_company(request: Request, company_id: int = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth", status_code=303)
        
    conn = get_db()
    cursor = conn.cursor()
    query = format_query("DELETE FROM watchlist WHERE id = ? AND user_id = ?")
    cursor.execute(query, (company_id, user["id"]))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=303)

# ---------------------------------------------------------
# AUTH ROTALARI
# ---------------------------------------------------------
@app.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="auth.html",
        context={"error": None}
    )

@app.post("/auth/register")
async def register(
    request: Request, 
    email: str = Form(...), 
    password: str = Form(...), 
    telegram_chat_id: str = Form(None)
):
    email_clean = email.strip().lower()
    chat_id_clean = telegram_chat_id.strip() if telegram_chat_id else None

    if chat_id_clean and not is_valid_telegram_chat_id(chat_id_clean):
        return templates.TemplateResponse(
            request=request,
            name="auth.html",
            context={"error": "Telegram Chat ID must be a valid numeric ID."}
        )

    conn = get_db()
    cursor = conn.cursor()
    
    q_email = format_query("SELECT id FROM users WHERE lower(email) = ?")
    cursor.execute(q_email, (email_clean,))
    if cursor.fetchone():
        conn.close()
        return templates.TemplateResponse(
            request=request,
            name="auth.html",
            context={"error": "This email is already registered."}
        )
        
    if chat_id_clean and (email_clean not in ADMIN_EMAILS):
        q_tg = format_query("SELECT id FROM users WHERE telegram_chat_id = ?")
        cursor.execute(q_tg, (chat_id_clean,))
        existing_tg_user = cursor.fetchone()
        if existing_tg_user:
            conn.close()
            return templates.TemplateResponse(
                request=request,
                name="auth.html",
                context={"error": "This Telegram Chat ID is already registered to another account."}
            )
        
    pwd_hash = hash_password(password)
    is_pro_val = 1 if email_clean in ADMIN_EMAILS else 0
    
    if USE_PG:
        cursor.execute("""
            INSERT INTO users (email, password_hash, telegram_chat_id, is_pro) 
            VALUES (%s, %s, %s, %s) RETURNING id
        """, (email_clean, pwd_hash, chat_id_clean, is_pro_val))
        user_id = cursor.fetchone()["id"]
    else:
        cursor.execute("""
            INSERT INTO users (email, password_hash, telegram_chat_id, is_pro) 
            VALUES (?, ?, ?, ?)
        """, (email_clean, pwd_hash, chat_id_clean, is_pro_val))
        user_id = cursor.lastrowid
    
    session_id = secrets.token_hex(24)
    q_session = format_query("INSERT INTO sessions (session_id, user_id) VALUES (?, ?)")
    cursor.execute(q_session, (session_id, user_id))
    conn.commit()
    conn.close()
    
    if chat_id_clean:
        reg_msg = (
            f"👋 <b>Welcome to UK Company Radar!</b>\n\n"
            f"Your Telegram Chat ID has been linked successfully.\n"
            f"Automated monitoring will dispatch alerts directly here."
        )
        send_telegram_sync(chat_id_clean, reg_msg)
    
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie("session_id", session_id, max_age=30 * 86400, httponly=True, secure=True, samesite="lax")
    return response

@app.post("/auth/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    email_clean = email.strip().lower()
    conn = get_db()
    cursor = conn.cursor()
    query = format_query("SELECT * FROM users WHERE lower(email) = ?")
    cursor.execute(query, (email_clean,))
    user = cursor.fetchone()
    
    if not user or not verify_password(user["password_hash"], password):
        conn.close()
        return templates.TemplateResponse(
            request=request,
            name="auth.html",
            context={"error": "Invalid email or password."}
        )
        
    session_id = secrets.token_hex(24)
    q_session = format_query("INSERT INTO sessions (session_id, user_id) VALUES (?, ?)")
    cursor.execute(q_session, (session_id, user["id"]))
    conn.commit()
    conn.close()
    
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie("session_id", session_id, max_age=30 * 86400, httponly=True, secure=True, samesite="lax")
    return response

@app.get("/auth/logout")
async def logout(request: Request):
    session_id = request.cookies.get("session_id")
    if session_id:
        conn = get_db()
        cursor = conn.cursor()
        query = format_query("DELETE FROM sessions WHERE session_id = ?")
        cursor.execute(query, (session_id,))
        conn.commit()
        conn.close()
        
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("session_id", path="/", samesite="lax")
    return response