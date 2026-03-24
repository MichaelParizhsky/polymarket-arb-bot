#!/usr/bin/env python3
"""
Run this once to find your Telegram chat ID.

1. Message your bot on Telegram first (send /start or any message)
2. Run: TELEGRAM_BOT_TOKEN=your_token python monitor/get_chat_id.py
"""
import os
import urllib.request
import json

token = os.getenv("TELEGRAM_BOT_TOKEN", "")
if not token:
    print("Set TELEGRAM_BOT_TOKEN env var first.")
    raise SystemExit(1)

url = f"https://api.telegram.org/bot{token}/getUpdates"
with urllib.request.urlopen(url) as r:
    data = json.loads(r.read())

if not data.get("result"):
    print("No messages found. Send your bot a message on Telegram first, then re-run.")
    raise SystemExit(1)

for update in data["result"]:
    chat = update.get("message", {}).get("chat", {})
    print(f"Chat ID: {chat.get('id')}  |  Name: {chat.get('first_name', '')} {chat.get('username', '')}")
