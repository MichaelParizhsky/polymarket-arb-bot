import httpx

print("Testing Polymarket API...")
try:
    r = httpx.get("https://gamma-api.polymarket.com/markets?limit=5&active=true", timeout=10)
    print(f"Status: {r.status_code}")
    data = r.json()
    print(f"Markets returned: {len(data)}")
    if data:
        print(f"First market: {data[0].get('question', 'N/A')}")
except Exception as e:
    print(f"Error: {e}")
