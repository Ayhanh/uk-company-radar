async def search_companies_house(query: str):
    if not COMPANIES_HOUSE_API_KEY:
        print("[Companies House Error]: API key is missing in environment variables!")
        return []
    url = f"https://api.company-information.service.gov.uk/search/companies?q={query}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(url, auth=httpx.BasicAuth(COMPANIES_HOUSE_API_KEY.strip(), ""))
            print(f"[Companies House Status]: {res.status_code}")
            if res.status_code == 200:
                data = res.json()
                return data.get("items", [])
            else:
                print(f"[Companies House Body]: {res.text}")
    except Exception as e:
        print(f"[Companies House Exception]: {e}")
    return []