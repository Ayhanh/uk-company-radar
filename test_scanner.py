import requests
import json

API_KEY = "109c448e-0d41-4f59-8651-2eab715f81fa"
BASE_URL = "https://api.company-information.service.gov.uk"

# 1. Glens of Antrim Potatoes (NI068869)
# 2. Tesco PLC (00445790)
# 3. İflas / Tasfiye sürecinde örnek bir firma
WATCHLIST = ["NI068869", "00445790"]

def analyze_company(company_number):
    url = f"{BASE_URL}/company/{company_number}"
    response = requests.get(url, auth=(API_KEY, ""))
    
    if response.status_code != 200:
        print(f"⚠️ Hata ({response.status_code}): {company_number}")
        return

    data = response.json()
    name = data.get("company_name")
    status = data.get("company_status")
    has_insolvency = data.get("has_insolvency_history", False)
    
    print(f"\n==========================================")
    print(f"🏢 {name} [{company_number}]")
    print(f"Durum: {status.upper()} | İflas Kaydı: {has_insolvency}")

    # 1. Statü Kontrolü
    if status in ["administration", "liquidation", "receivership", "dissolved"]:
        print(f"🚨 ALARM - ŞİRKET BATIYOR / KAPANDI: Statü -> {status.upper()}")
    elif status == "active":
        print("🟢 Şirket faal.")

    # 2. Dosyalama Geçmişi (Filing History) Kontrolü (Erken Uyarı Mekanizması)
    # İflas veya zorunlu kapanma kararları genellikle statü değişmeden önce buraya düşer!
    filing_url = f"{BASE_URL}/company/{company_number}/filing-history?items_per_page=5"
    f_res = requests.get(filing_url, auth=(API_KEY, ""))
    
    if f_res.status_code == 200:
        filings = f_res.json().get("items", [])
        print("\n📋 Son Resmi Bildirimler:")
        for item in filings:
            category = item.get("category", "")
            date = item.get("date", "")
            desc = item.get("description", "")
            
            # Riskli kategoriler: insolvency, gazette
            is_risk = category in ["insolvency", "liquidation"] or "gazette" in category
            prefix = "⚠️ [RİSKLİ]" if is_risk else "ℹ️"
            print(f"  {prefix} {date} | {category.upper()} -> {desc}")

if __name__ == "__main__":
    for comp in WATCHLIST:
        analyze_company(comp)