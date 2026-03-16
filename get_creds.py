from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

key = input("Paste your private key: ").strip()
client = ClobClient(host="https://clob.polymarket.com", chain_id=POLYGON, key=key)
creds = client.create_or_derive_api_creds()
print(f"POLYMARKET_API_KEY={creds.api_key}")
print(f"POLYMARKET_API_SECRET={creds.api_secret}")
print(f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")
