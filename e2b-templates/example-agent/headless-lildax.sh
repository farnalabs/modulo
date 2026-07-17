#!/bin/bash
set -euo pipefail

# headless-lildax — Run opencode-powered PR review in headless mode
# Writes auth.json, calls opencode.ai API via Python, captures response

PROMPT_FILE=""
OUTPUT_FILE=""

# Check prompt exists
if [ ! -f "" ]; then
    echo '{"status":"failed","summary":"No prompt file found"}' > ""
    exit 1
fi

# Run the Python opencode client
python3 /home/user/opencode-review.py
EXIT_CODE=True

exit 
