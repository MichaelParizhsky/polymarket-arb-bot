#!/usr/bin/env python3
"""
Kalshi credential diagnostic.
Run locally or on Railway (railway run python diagnose_kalshi.py).

Checks every step of RSA auth and prints exactly why 401 occurs.
"""
import asyncio
import base64
import os
import sys
import time

# ── 1. Check env vars ─────────────────────────────────────────────────────────

key_id     = os.getenv("KALSHI_API_KEY_ID", "")
priv_key   = os.getenv("KALSHI_PRIVATE_KEY", "")
demo_mode  = os.getenv("KALSHI_DEMO", "").lower() in ("1", "true", "yes")

BASE = "https://demo-api.kalshi.co/trade-api/v2" if demo_mode else "https://trading-api.kalshi.com/trade-api/v2"

print("=" * 60)
print("KALSHI CREDENTIAL DIAGNOSTIC")
print("=" * 60)
print(f"  Environment : {'DEMO (demo-api.kalshi.co)' if demo_mode else 'PRODUCTION (trading-api.kalshi.com)'}")
print(f"  KALSHI_API_KEY_ID set : {bool(key_id)}")
if key_id:
    print(f"    value  : {key_id}")
print(f"  KALSHI_PRIVATE_KEY set: {bool(priv_key)}")
if priv_key:
    print(f"    length : {len(priv_key)} chars")
    print(f"    first 40: {priv_key[:40]!r}")

if not key_id:
    print("\n[FAIL] KALSHI_API_KEY_ID is not set")
    sys.exit(1)
if not priv_key:
    print("\n[FAIL] KALSHI_PRIVATE_KEY is not set")
    sys.exit(1)

# ── 2. Parse PEM key ──────────────────────────────────────────────────────────

print("\n── PEM key parsing ──")
# Handle both real newlines and escaped \n stored in env vars
pem_normalized = priv_key.replace("\\n", "\n")
print(f"  Lines after normalising \\\\n → \\n: {len(pem_normalized.splitlines())}")
print(f"  First line : {pem_normalized.splitlines()[0] if pem_normalized.splitlines() else 'EMPTY'}")
print(f"  Last  line : {pem_normalized.splitlines()[-1] if pem_normalized.splitlines() else 'EMPTY'}")

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding as crypto_padding
    private_key = serialization.load_pem_private_key(pem_normalized.encode(), password=None)
    print(f"  [OK] PEM parsed — key type: {type(private_key).__name__}")
    key_size = private_key.key_size
    print(f"  [OK] Key size: {key_size} bits")
except Exception as e:
    print(f"  [FAIL] PEM parse error: {e}")
    print()
    print("  Fix: ensure the private key in Railway env vars has proper line breaks.")
    print("  The value must look like:")
    print("    -----BEGIN RSA PRIVATE KEY-----")
    print("    MIIEowIBAAKCAQEA...")
    print("    -----END RSA PRIVATE KEY-----")
    print()
    print("  If stored as one line, use literal \\n between lines (backslash + n):")
    print("    -----BEGIN RSA PRIVATE KEY-----\\nMIIEow...\\n-----END RSA PRIVATE KEY-----")
    sys.exit(1)

# ── 3. Test signing ───────────────────────────────────────────────────────────

print("\n── Signature generation ──")
ts_ms = int(time.time() * 1000)
method = "GET"
path = "/trade-api/v2/markets"
message = f"{ts_ms}{method}{path}".encode()
print(f"  Message to sign: {message[:60]!r}...")

try:
    signature = private_key.sign(
        message,
        crypto_padding.PSS(
            mgf=crypto_padding.MGF1(hashes.SHA256()),
            salt_length=crypto_padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    sig_b64 = base64.b64encode(signature).decode()
    print(f"  [OK] Signature generated ({len(sig_b64)} chars b64)")
    print(f"       First 40: {sig_b64[:40]}...")
except Exception as e:
    print(f"  [FAIL] Signing error: {e}")
    sys.exit(1)

# ── 4. Make real API call ─────────────────────────────────────────────────────

print(f"\n── Live API call → {BASE}/markets ──")

async def test_api():
    import httpx
    headers = {
        "Kalshi-Access-Key": key_id,
        "Kalshi-Access-Timestamp": str(ts_ms),
        "Kalshi-Access-Signature": sig_b64,
        "User-Agent": "kalshi-diag/1.0",
    }
    print(f"  Headers sent:")
    for k, v in headers.items():
        display = v if len(v) < 40 else v[:37] + "..."
        print(f"    {k}: {display}")

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{BASE}/markets",
            params={"limit": 5, "status": "open"},
            headers=headers,
        )
    print(f"\n  HTTP status : {r.status_code} {r.reason_phrase}")
    print(f"  Response body (first 500 chars):")
    print(f"    {r.text[:500]}")

    if r.status_code == 200:
        data = r.json()
        n = len(data.get("markets", []))
        print(f"\n  [OK] SUCCESS — {n} markets returned")
        print("\n  Your Kalshi credentials are working correctly.")
    elif r.status_code == 401:
        print("\n  [FAIL] 401 Unauthorized")
        print()
        print("  Most likely causes:")
        print("  1. Your API key was created on the DEMO environment.")
        print("     → Set KALSHI_DEMO=true in Railway env vars and re-run.")
        print("  2. KALSHI_API_KEY_ID does not match the private key.")
        print("     → Regenerate the key pair on kalshi.com and update both vars.")
        print("  3. Timestamp drift (clock skew) — unlikely in containers.")
    else:
        print(f"\n  [FAIL] Unexpected status {r.status_code}")

asyncio.run(test_api())
print("=" * 60)
