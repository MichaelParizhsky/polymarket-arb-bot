import httpx

r = httpx.get("https://gamma-api.polymarket.com/markets?limit=3&active=true", timeout=10)
markets = r.json()
for m in markets:
    print("Keys:", list(m.keys()))
    print("condition_id:", m.get("condition_id"))
    print("tokens:", m.get("tokens"))
    print("clobTokenIds:", m.get("clobTokenIds"))
    print("---")
