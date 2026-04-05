"""
Run this once to generate Polymarket API credentials from your private key.
Usage: python setup_creds.py
"""
import os
from py_clob_client.client import ClobClient

private_key = input("Paste your MetaMask private key: ").strip()
funder_address = input("Paste your MetaMask wallet address (0x...): ").strip()

client = ClobClient(
    host="https://clob.polymarket.com",
    key=private_key,
    chain_id=137,
    funder=funder_address,
)

creds = client.create_or_derive_api_creds()

print("\n=== Copy these into Railway ===")
print(f"POLYMARKET_PRIVATE_KEY={private_key}")
print(f"POLYMARKET_FUNDER_ADDRESS={funder_address}")
print(f"POLYMARKET_API_KEY={creds.api_key}")
print(f"POLYMARKET_API_SECRET={creds.api_secret}")
print(f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")
print("\nRun:")
print(f'railway variables set POLYMARKET_PRIVATE_KEY="{private_key}"')
print(f'railway variables set POLYMARKET_FUNDER_ADDRESS="{funder_address}"')
print(f'railway variables set POLYMARKET_API_KEY="{creds.api_key}"')
print(f'railway variables set POLYMARKET_API_SECRET="{creds.api_secret}"')
print(f'railway variables set POLYMARKET_API_PASSPHRASE="{creds.api_passphrase}"')
