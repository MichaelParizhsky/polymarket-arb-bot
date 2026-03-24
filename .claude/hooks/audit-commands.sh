#!/usr/bin/env bash
# Appends every bash command Claude runs to an audit log.
INPUT=$(cat)
CMD=$(echo "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)
if [ -n "$CMD" ]; then
  LOG_DIR="$(dirname "$0")/../logs"
  mkdir -p "$LOG_DIR"
  echo "$(date +%Y-%m-%dT%H:%M:%S) | $CMD" >> "$LOG_DIR/claude-audit.log"
fi
exit 0
