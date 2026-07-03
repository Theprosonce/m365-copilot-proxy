#!/bin/bash
# Idempotent update/removal of ~/.config/opencode/opencode.json
# Usage:
#   ./opencode.sh          -> merge new settings (preserve existing)
#   ./opencode.sh --remove -> remove the m365-copilot-proxy provider

set -euo pipefail

CONFIG_FILE="$HOME/.config/opencode/opencode.json"
NEW_CONTENT='{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "m365-copilot-proxy": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "M365 Copilot Proxy",
      "options": {
        "baseURL": "http://127.0.0.1:8000/v1",
        "apiKey": "dummy"
      },
      "models": {
        "m365-auto": {},
        "m365-copilot": {},
        "m365-copilot:persist": {},
        "m365-opus": {},
        "m365-opus:persist": {},
        "m365-claude": {},
        "m365-claude:persist": {},
        "m365-gpt-quick": {},
        "m365-gpt-quick:persist": {},
        "m365-gpt-think": {},
        "m365-gpt-think:persist": {}
      }
    }
  }
}'

# Check for jq (required)
if ! command -v jq &> /dev/null; then
    echo "Error: jq is not installed. Please install it (e.g., sudo apt install jq)." >&2
    exit 1
fi

# Determine action
ACTION="merge"
if [ $# -gt 0 ] && [ "$1" = "--remove" ]; then
    ACTION="remove"
fi

mkdir -p "$(dirname "$CONFIG_FILE")"

# Function to perform merge (as before)
do_merge() {
    if [ ! -f "$CONFIG_FILE" ]; then
        echo "$NEW_CONTENT" > "$CONFIG_FILE"
        echo "Created new configuration file."
    else
        NEW_PROVIDER=$(echo "$NEW_CONTENT" | jq '.provider')
        TMP_FILE="${CONFIG_FILE}.tmp"
        # Use // {} to handle missing/null provider
        jq --argjson newprov "$NEW_PROVIDER" \
            '.provider = ((.provider // {}) * $newprov)' \
            "$CONFIG_FILE" > "$TMP_FILE"
        if ! cmp -s "$CONFIG_FILE" "$TMP_FILE"; then
            mv "$TMP_FILE" "$CONFIG_FILE"
            echo "Setup completed!"
        else
            rm "$TMP_FILE"
            echo "Configuration already up-to-date."
        fi
    fi
}

# Function to remove the provider
do_remove() {
    if [ ! -f "$CONFIG_FILE" ]; then
        echo "Configuration file does not exist. Nothing removed."
        return 0
    fi

    TMP_FILE="${CONFIG_FILE}.tmp"

    # Remove the specific provider from the .provider object
    # Then delete .provider if it becomes empty
    jq 'del(.provider."m365-copilot-proxy") | if .provider == {} then del(.provider) else . end' \
        "$CONFIG_FILE" > "$TMP_FILE"

    if ! cmp -s "$CONFIG_FILE" "$TMP_FILE"; then
        mv "$TMP_FILE" "$CONFIG_FILE"
        echo "Removed 'm365-copilot-proxy' provider from configuration."
    else
        rm "$TMP_FILE"
        echo "No changes. Provider was already absent."
    fi
}

# Execute the chosen action
case "$ACTION" in
    merge)   do_merge ;;
    remove)  do_remove ;;
    *)       echo "Unknown action"; exit 1 ;;
esac