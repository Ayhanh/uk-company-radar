import sqlite3
import requests

CH_API_KEY = "109c448e-0d41-4f59-8651-2eab715f81fa"
TELEGRAM_TOKEN = "8751754421:AAHN2ta1wD-1LAGKf7Juyao2ggmKVqkwqlI"
CHAT_ID = "8829892073"

BASE_URL = "https://api.company-information.service.gov.uk"
DB_FILE = "companies.db"

# İlk etapta takip edilecek örnek havuz
INITIAL_WATCHLIST = [
    ("NI068869", "ORACLE (N.I.) LIMITED"),
    ("00445790", "TESCO PLC")
]

def init_db():
    """Veritabanını ve takip tablosunu oluşturur."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS watched_companies (
            company_number TEXT PRIMARY KEY,
            company_name TEXT,
            last_status TEXT,
            last_filing_date TEXT
        )
    """)
    
    # Başlangıç şirketlerini tabloya ekle (zaten varsa dokunmaz)
    for comp_no, name in INITIAL_WATCHLIST:
        cursor.execute("""
            INSERT OR IGNORE INTO watched_companies (company_number, company_name, last_status, last_filing_date)
            VALUES (?, ?, NULL, NULL)
        """, (comp_no, name))
    
    conn.commit()
    conn.close()

def send_telegram_alert(message):
    """Telegram üzerinden formatlanmış Markdown mesajı atar."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram gönderim hatası: {e}")

def check_company(cursor, company_number, saved_status, saved_filing_date):
    url = f"{BASE_URL}/company/{company_number}"
    res = requests.get(url, auth=(CH_API_KEY, ""), timeout=10)
    
    if res.status_code != 200:
        print(f"⚠️ Bilgi çekilemedi: {company_number}")
        return

    data = res.json()
    name = data.get("company_name", "Bilinmiyor")
    current_status = data.get("company_status", "unknown")
    has_insolvency = data.get("has_insolvency_history", False)
    
    # Son bildirimleri çekelim
    filing_url = f"{BASE_URL}/company/{company_number}/filing-history?items_per_page=1"
    f_res = requests.get(filing_url, auth=(CH_API_KEY, ""), timeout=10)
    
    latest_filing_date = None
    latest_filing_desc = "Kayıt yok"
    latest_filing_cat = ""
    
    if f_res.status_code == 200:
        items = f_res.json().get("items", [])
        if items:
            latest_filing_date = items[0].get("date")
            latest_filing_desc = items[0].get("description", "")
            latest_filing_cat = items[0].get("category", "")

    # Değişim veya risk kontrolü:
    # 1. İlk defa taranıyorsa ve riskliyse bildir.
    # 2. Statü değişmişse bildir.
    # 3. Yeni bir riskli bildirim (insolvency/gazette) gelmişse bildir.
    is_risky_status = current_status in ["dissolved", "liquidation", "administration", "receivership"]
    status_changed = saved_status is not None and saved_status != current_status
    new_filing_alert = latest_filing_date != saved_filing_date and ("insolvency" in latest_filing_cat or "gazette" in latest_filing_cat)

    should_alert = False
    alert_reason = ""

    if saved_status is None and (is_risky_status or has_insolvency):
        should_alert = True
        alert_reason = "İlk Tarama: Mevcut Riskli Durum Tespiti"
    elif status_changed:
        should_alert = True
        alert_reason = f"Statü Değişikliği: {saved_status} ➡️ {current_status.upper()}"
    elif new_filing_alert:
        should_alert = True
        alert_reason = f"Yeni Resmi Bildirim Düşürüldü: {latest_filing_cat.upper()}"

    if should_alert:
        msg = (
            f"🚨 *UK COMPANY RADAR - UYARI*\n\n"
            f"📌 *Neden:* {alert_reason}\n"
            f"🏢 *Firma:* {name}\n"
            f"🔢 *Sicil No:* `{company_number}`\n"
            f"⚠️ *Statü:* *{current_status.upper()}*\n"
            f"⚖️ *İflas Kaydı:* {'EVET' if has_insolvency else 'HAYIR'}\n\n"
            f"📋 *Son Bildirim ({latest_filing_date}):*\n_{latest_filing_desc}_\n\n"
            f"🔗 [Profili Aç](https://find-and-update.company-information.service.gov.uk/company/{company_number})"
        )
        send_telegram_alert(msg)
        print(f"📲 ALARM GÖNDERİLDİ: {name}")
    else:
        print(f"✅ Değişiklik yok: {name} (Statü: {current_status})")

    # Veritabanındaki kaydı güncelle
    cursor.execute("""
        UPDATE watched_companies
        SET company_name = ?, last_status = ?, last_filing_date = ?
        WHERE company_number = ?
    """, (name, current_status, latest_filing_date, company_number))

def run_scanner():
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("SELECT company_number, last_status, last_filing_date FROM watched_companies")
    companies = cursor.fetchall()
    
    print(f"🔎 {len(companies)} şirket taranıyor...\n")
    for comp_no, last_stat, last_filing in companies:
        check_company(cursor, comp_no, last_stat, last_filing)
    
    conn.commit()
    conn.close()
    print("\n🏁 Tarama tamamlandı.")

if __name__ == "__main__":
    run_scanner()