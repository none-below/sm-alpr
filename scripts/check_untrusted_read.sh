#!/bin/bash
# PreToolUse hook: blocks Claude from reading raw scraped HTML/TXT files
# that could contain prompt injection from Flock Safety's transparency portal.
# Only parsed .json and archived .pdf files are safe to read directly.

file_path=$(jq -r '.tool_input.file_path // empty')

if echo "$file_path" | grep -qE 'assets/transparency\.flocksafety\.com/.*\.(html|txt)$'; then
  cat <<'EOF'
{"decision":"block","reason":"BLOCKED: Raw scraped .html/.txt files may contain prompt injection. Use the parsed .json file instead, or ask the user for explicit permission to proceed."}
EOF
  exit 2
fi

exit 0
