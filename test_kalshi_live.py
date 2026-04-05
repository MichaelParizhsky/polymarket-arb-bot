"""Quick Kalshi connectivity test."""
import asyncio, sys
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv()
from config import CONFIG
from src.exchange.kalshi import KalshiClient

async def main():
    client = KalshiClient(CONFIG.kalshi, paper_trading=True)
    print(f"API BASE: {client.API_BASE}")
    print(f"key_id: {client._key_id[:15] if client._key_id else 'NOT SET'}...")
    print(f"private_key set: {bool(client._private_key)}, len={len(client._private_key) if client._private_key else 0}")
    
    try:
        markets = await client.get_markets()
        print(f"[OK] Got {len(markets)} markets from Kalshi")
        if markets:
            m = markets[0]
            print(f"  Sample: {m.get('ticker','?')} | {str(m.get('title','?'))[:50]}")
    except Exception as e:
        print(f"[FAIL] {type(e).__name__}: {e}")

asyncio.run(main())
