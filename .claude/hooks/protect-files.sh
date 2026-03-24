#!/usr/bin/env bash
# Blocks Claude from writing to sensitive files in the trading bot project.
INPUT=$(cat)
FILE=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null)

if [ -z "$FILE" ]; then exit 0; fi

PROTECTED=(".env" ".env.local" ".env.production" "secrets" "railway.toml" "get_creds.py")
for pattern in "${PROTECTED[@]}"; do
  if [[ "$FILE" == *"$pattern"* ]]; then
    echo "BLOCKED: $FILE is a protected file. Edit manually if needed." >&2
    exit 2
  fi
done
exit 0
