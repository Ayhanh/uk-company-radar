import os
import sqlite3
import hashlib
import secrets
import asyncio
from pathlib import Path
from datetime import date
from typing import Optional

import requests
from fastapi import FastAPI, Request, Form, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------
# KONFIGURASYON & SABİT DEĞİŞKENLER
# ---------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "companies.db"
BASE_URL = "https://api.company-information.service.gov.uk"

# Ortam değişkeni yoksa doğrudan sizin anahtarlarınızı kullanır:
COMPANIES_HOUSE_API_KEY = os.getenv("COMPANIES_HOUSE_API_KEY", "109c448e-0d41-4f59-8651-2eab715f81fa").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8751754421:AAHN2ta1wD-1LAGKf7Juyao2ggmKVqkwqlI").strip()
SECRET_KEY = os.getenv("SECRET_KEY", "super-secret-uk-company-radar-key-change-in-prod")

ADMIN_EMAILS = [os.getenv("ADMIN_EMAIL", "ayhan.hakyemez@gmail.com").strip().lower()]

app = FastAPI(title="UK Company Radar")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ---------------------------------------------------------
# VERİTABANI YÖNETİMİ
# ---------------------------------------------------------
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
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
# KULLANICI & KOTA
# ---------------------------------------------------------
def get_current_user(request: Request):
    session_id = request.cookies.get("session_id")
    if not session_id:
        return None
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT u.id, u.email, u.telegram_chat_id, u.is_pro 
        FROM users u 
        JOIN sessions s ON u.id = s.user_id 
        WHERE s.session_id = ?
    """, (session_id,))
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
    cursor.execute("SELECT count FROM search_logs WHERE identifier = ? AND search_date = ?", (identifier, today))
    row = cursor.fetchone()
    conn.close()
    return row["count"] if row else 0

def increment_search_count(identifier: str):
    today = date.today().isoformat()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO search_logs (identifier, search_date, count) 
        VALUES (?, ?, 1)
        ON CONFLICT(identifier, search_date) DO UPDATE SET count = count + 1
    """, (identifier, today))
    conn.commit()
    conn.close()

# ---------------------------------------------------------
# COMPANIES HOUSE API
# ---------------------------------------------------------
def search_companies_house(query: str):
    if not COMPANIES_HOUSE_API_KEY:
        print("[Companies House Error]: Key missing!")
        return []
    url = f"{BASE_URL}/search/companies?q={query}"
    try:
        res = requests.get(url, auth=(COMPANIES_HOUSE_API_KEY, ""), timeout=8)
        if res.status_code == 200:
            return res.json().get("items", [])
        else:
            print(f"[Companies House Status]: {res.status_code}")
    except Exception as e:
        print(f"[Companies House Exception]: {e}")
    return []

def get_company_profile(company_number: str):
    if not COMPANIES_HOUSE_API_KEY:
        return None
    url = f"{BASE_URL}/company/{company_number}"
    try:
        res = requests.get(url, auth=(COMPANIES_HOUSE_API_KEY, ""), timeout=8)
        if res.status_code == 200:
            return res.json()
    except Exception as e:
        print(f"[Companies House Profile Exception]: {e}")
    return None

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
                SELECT w.id, w.company_number, w.company_name, w.status, u.telegram_chat_id 
                FROM watchlist w 
                JOIN users u ON w.user_id = u.id
            """)
            items = cursor.fetchall()
            
            for item in items:
                live_profile = get_company_profile(item["company_number"])
                if live_profile:
                    new_status = live_profile.get("company_status", "unknown")
                    old_status = item["status"]
                    
                    if new_status.lower() != old_status.lower():
                        cursor.execute("UPDATE watchlist SET status = ? WHERE id = ?", (new_status, item["id"]))
                        conn.commit()
                        
                        if item["telegram_chat_id"]:
                            alert_msg = (
                                f"🚨 <b>CRITICAL RISK ALERT: STATUS CHANGE DETECTED</b>\n\n"
                                f"<b>Company:</b> {item['company_name']} ({item['company_number']})\n"
                                f"<b>Previous Status:</b> {old_status.upper()}\n"
                                f"<b>New Status:</b> ⚠️ <b>{new_status.upper()}</b>\n\n"
                                f"<i>Immediate attention recommended. Check official Companies House filing.</i>"
                            )
                            send_telegram_sync(item["telegram_chat_id"], alert_msg)
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
        cursor.execute("SELECT * FROM watchlist WHERE user_id = ? ORDER BY created_at DESC", (user["id"],))
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
        return RedirectResponse(url="/auth", status_code=status.HTTP_303_SEE_OTHER)
        
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as count FROM watchlist WHERE user_id = ?", (user["id"],))
    count = cursor.fetchone()["count"]
    
    if not user.get("is_pro") and count >= 3:
        conn.close()
        return RedirectResponse(url="/?limit_reached=1", status_code=status.HTTP_303_SEE_OTHER)
        
    cursor.execute("""
        INSERT INTO watchlist (user_id, company_number, company_name, status, type, jurisdiction)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user["id"], company_number, company_name, status, type, jurisdiction))
    conn.commit()
    conn.close()
    
    # Telegram Bildirimi Gönder
    if user.get("telegram_chat_id"):
        welcome_alert = (
            f"🛡️ <b>UK Company Radar | Monitoring Active</b>\n\n"
            f"<b>Company:</b> {company_name} ({company_number})\n"
            f"<b>Current Legal Status:</b> {status.upper()}\n\n"
            f"✅ <i>24/7 automated insolvency monitoring is now active. You will receive an instant notification here if this entity enters liquidation, administration, or strike-off.</i>"
        )
        send_telegram_sync(user["telegram_chat_id"], welcome_alert)

    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/untrack")
async def untrack_company(request: Request, company_id: int = Form(...)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth", status_code=status.HTTP_303_SEE_OTHER)
        
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM watchlist WHERE id = ? AND user_id = ?", (company_id, user["id"]))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

# ---------------------------------------------------------
# AUTH ROTALARI
# ---------------------------------------------------------
@app.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
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
    
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT id FROM users WHERE lower(email) = ?", (email_clean,))
    if cursor.fetchone():
        conn.close()
        return templates.TemplateResponse(
            request=request,
            name="auth.html",
            context={"error": "This email is already registered."}
        )
        
    if chat_id_clean and (email_clean not in ADMIN_EMAILS):
        cursor.execute("SELECT id FROM users WHERE telegram_chat_id = ?", (chat_id_clean,))
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
    
    cursor.execute("""
        INSERT INTO users (email, password_hash, telegram_chat_id, is_pro) 
        VALUES (?, ?, ?, ?)
    """, (email_clean, pwd_hash, chat_id_clean, is_pro_val))
    user_id = cursor.lastrowid
    
    session_id = secrets.token_hex(24)
    cursor.execute("INSERT INTO sessions (session_id, user_id) VALUES (?, ?)", (session_id, user_id))
    conn.commit()
    conn.close()
    
    if chat_id_clean:
        reg_msg = (
            f"👋 <b>Welcome to UK Company Radar!</b>\n\n"
            f"Your Telegram Chat ID has been linked successfully.\n"
            f"Automated monitoring will dispatch alerts directly here."
        )
        send_telegram_sync(chat_id_clean, reg_msg)
    
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie("session_id", session_id, max_age=30*86400, httponly=True)
    return response

@app.post("/auth/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    email_clean = email.strip().lower()
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE lower(email) = ?", (email_clean,))
    user = cursor.fetchone()
    
    if not user or not verify_password(user["password_hash"], password):
        conn.close()
        return templates.TemplateResponse(
            request=request,
            name="auth.html",
            context={"error": "Invalid email or password."}
        )
        
    session_id = secrets.token_hex(24)
    cursor.execute("INSERT INTO sessions (session_id, user_id) VALUES (?, ?)", (session_id, user["id"]))
    conn.commit()
    conn.close()
    
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie("session_id", session_id, max_age=30*86400, httponly=True)
    return response

@app.get("/auth/logout")
async def logout(request: Request):
    session_id = request.cookies.get("session_id")
    if session_id:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        conn.commit()
        conn.close()
        
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("session_id")
    return response