import sqlite3
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

CH_API_KEY = "109c448e-0d41-4f59-8651-2eab715f81fa"
TELEGRAM_TOKEN = "8751754421:AAHN2ta1wD-1LAGKf7Juyao2ggmKVqkwqlI"
AUTHORIZED_CHAT_ID = 8829892073

BASE_URL = "https://api.company-information.service.gov.uk"
DB_FILE = "companies.db"

def init_db():
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
    conn.commit()
    conn.close()

def is_authorized(update: Update) -> bool:
    return update.effective_chat.id == AUTHORIZED_CHAT_ID

# /start & /help
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    text = (
        "🛡️ *UK Company Radar Monitor Active*\n\n"
        "Available Commands:\n"
        "🔹 `/track <company_number>` - Add a UK company to your watchlist\n"
        "🔹 `/untrack <company_number>` - Remove a company from your watchlist\n"
        "🔹 `/watchlist` - View all tracked entities and live statuses\n"
        "🔹 `/scan` - Run an immediate risk and insolvency audit"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# /track <number>
async def track_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if not context.args:
        await update.message.reply_text("⚠️ Please provide a company number.\nExample: `/track 00445790`", parse_mode="Markdown")
        return

    company_number = context.args[0].strip().upper()
    
    url = f"{BASE_URL}/company/{company_number}"
    res = requests.get(url, auth=(CH_API_KEY, ""), timeout=10)

    if res.status_code == 404:
        await update.message.reply_text(f"❌ Company number `{company_number}` was not found on Companies House.", parse_mode="Markdown")
        return
    elif res.status_code != 200:
        await update.message.reply_text(f"⚠️ Companies House API error (HTTP {res.status_code}).")
        return

    data = res.json()
    name = data.get("company_name", "Unknown")
    status = data.get("company_status", "unknown")

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO watched_companies (company_number, company_name, last_status, last_filing_date)
        VALUES (?, ?, ?, NULL)
    """, (company_number, name, status))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ *Company Added to Watchlist*\n\n"
        f"🏢 *Entity:* {name}\n"
        f"🔢 *Reg No:* `{company_number}`\n"
        f"📊 *Current Status:* {status.upper()}",
        parse_mode="Markdown"
    )

# /watchlist
async def list_companies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT company_number, company_name, last_status FROM watched_companies")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("Your watchlist is currently empty.")
        return

    msg = "📋 *Tracked Companies Watchlist:*\n\n"
    for no, name, status in rows:
        stat = status.upper() if status else "UNKNOWN"
        icon = "🟢" if stat == "ACTIVE" else "🔴"
        msg += f"{icon} *{name}*\n   └ Reg No: `{no}` | Status: {stat}\n"

    await update.message.reply_text(msg, parse_mode="Markdown")

# /untrack <number>
async def untrack_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    if not context.args:
        await update.message.reply_text("⚠️ Please specify a company number to untrack.\nExample: `/untrack 00445790`", parse_mode="Markdown")
        return

    company_number = context.args[0].strip().upper()

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM watched_companies WHERE company_number = ?", (company_number,))
    affected = cursor.rowcount
    conn.commit()
    conn.close()

    if affected > 0:
        await update.message.reply_text(f"🗑️ `{company_number}` has been removed from your watchlist.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"⚠️ Company `{company_number}` was not found in your watchlist.", parse_mode="Markdown")

# Tarama Çekirdeği (Hem /scan hem otomatik arka plan görevi kullanır)
def perform_audit():
    alerts = []
    risk_statuses = ["dissolved", "liquidation", "administration", "receivership"]

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT company_number, last_status, last_filing_date FROM watched_companies")
    companies = cursor.fetchall()

    for comp_no, saved_status, saved_filing_date in companies:
        url = f"{BASE_URL}/company/{comp_no}"
        res = requests.get(url, auth=(CH_API_KEY, ""), timeout=10)
        if res.status_code != 200:
            continue

        data = res.json()
        name = data.get("company_name", "Unknown Entity")
        current_status = data.get("company_status", "unknown")
        has_insolvency = data.get("has_insolvency_history", False)

        filing_url = f"{BASE_URL}/company/{comp_no}/filing-history?items_per_page=1"
        f_res = requests.get(filing_url, auth=(CH_API_KEY, ""), timeout=10)
        latest_filing_date = None
        latest_filing_desc = "None"
        latest_filing_cat = ""
        if f_res.status_code == 200:
            items = f_res.json().get("items", [])
            if items:
                latest_filing_date = items[0].get("date")
                latest_filing_desc = items[0].get("description", "")
                latest_filing_cat = items[0].get("category", "")

        is_risky_status = current_status in risk_statuses
        status_changed = saved_status is not None and saved_status != current_status
        new_risk_filing = latest_filing_date != saved_filing_date and ("insolvency" in latest_filing_cat or "gazette" in latest_filing_cat)

        if is_risky_status or has_insolvency or status_changed or new_risk_filing:
            alert_msg = (
                f"🚨 *CRITICAL ALERT - STATUS / INSOLVENCY RISK*\n\n"
                f"🏢 *Entity:* {name}\n"
                f"🔢 *Reg No:* `{comp_no}`\n"
                f"⚠️ *Status:* *{current_status.upper()}*\n"
                f"⚖️ *Insolvency History:* {'YES' if has_insolvency else 'NO'}\n"
                f"📋 *Latest Gazette / Filing:*\n_{latest_filing_desc}_\n\n"
                f"🔗 [Companies House Profile](https://find-and-update.company-information.service.gov.uk/company/{comp_no})"
            )
            alerts.append(alert_msg)

        cursor.execute("""
            UPDATE watched_companies
            SET company_name = ?, last_status = ?, last_filing_date = ?
            WHERE company_number = ?
        """, (name, current_status, latest_filing_date, comp_no))

    conn.commit()
    conn.close()
    return len(companies), alerts

# /scan Komutu
async def manual_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    await update.message.reply_text("🔍 Audit running... Querying Companies House registry.")
    total_checked, alerts = perform_audit()

    for alert in alerts:
        await update.message.reply_text(alert, parse_mode="Markdown")

    await update.message.reply_text(f"🏁 Audit completed. Checked: {total_checked} | Risk notices: {len(alerts)}")

# Arka Plan Zamanlanmış Görev (Her 6 saatte bir otomatik tarar)
async def scheduled_audit(context: ContextTypes.DEFAULT_TYPE):
    _, alerts = perform_audit()
    for alert in alerts:
        await context.bot.send_message(chat_id=AUTHORIZED_CHAT_ID, text=alert, parse_mode="Markdown")

def main():
    init_db()
    print("🛡️ UK Company Radar service started. Monitoring queue active...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("track", track_company))
    app.add_handler(CommandHandler("untrack", untrack_company))
    app.add_handler(CommandHandler("watchlist", list_companies))
    app.add_handler(CommandHandler("scan", manual_scan))

    # Her 21600 saniyede bir (6 saat) arka planda otomatik çalışır
    if app.job_queue:
        app.job_queue.run_repeating(scheduled_audit, interval=21600, first=60)

    app.run_polling()

if __name__ == "__main__":
    main()