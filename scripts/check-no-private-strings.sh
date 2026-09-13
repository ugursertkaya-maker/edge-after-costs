#!/usr/bin/env bash
# Fail if anything that looks like private infrastructure, a personal
# identifier or a credential has been committed.
#
# This is a coarse net on top of gitleaks: gitleaks looks for credential
# *shapes*, this looks for the kinds of string that should never appear in a
# public research repository at all -- a real server address, an ssh command
# against a named host, an absolute path from someone's workstation.
#
#   bash scripts/check-no-private-strings.sh
#
# Project-specific strings (your own domains, hostnames, usernames) do NOT
# belong in this file, because publishing the list publishes the very thing it
# is meant to protect. Put one regex per line in an untracked `.private-strings`
# file at the repository root instead; it is gitignored and read automatically
# when present.
#
# To allow a legitimate match, extend ALLOW rather than weakening a pattern.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

SELF='scripts/check-no-private-strings.sh'

PATTERNS=(
  # IPv4 literals (loopback, RFC1918 and documentation ranges are allowed below)
  '\b(([0-9]{1,3})\.){3}[0-9]{1,3}\b'
  # ssh to a named host
  '\bssh +[a-z_][a-z0-9_-]*@'
  # absolute paths from a developer machine
  '/Users/[a-z]'
  '/home/[a-z]'
  # credential-shaped assignments
  '(api[_-]?key|secret|passwd|password|token)[[:space:]]*[=:][[:space:]]*['"'"'"][^'"'"'"[:space:]]{8,}'
  # private key material
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
  # cloud / provider access key ids
  '\bAKIA[0-9A-Z]{16}\b'
  # provider token prefixes
  '\b(sk|pk|ghp|gho|xox[baprs])[-_][A-Za-z0-9]{16,}'
)

ALLOW='(127\.0\.0\.1|0\.0\.0\.0|localhost|example\.com|example\.invalid|your-domain\.example|your-server|CHANGE_ME|change-me|hunter2|10\.[0-9]+\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|/Users/\[|/home/\[|[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+-|[Vv]ersion|[0-9]\.[0-9]+\.[0-9]+["'"'"'),])'

status=0

for pat in "${PATTERNS[@]}"; do
  hits=$(git grep -nIE "$pat" -- . ":!$SELF" ':!LICENSE' 2>/dev/null | grep -vE "$ALLOW" || true)
  if [[ -n "$hits" ]]; then
    echo "::error::possible private string matching /$pat/"
    echo "$hits"
    status=1
  fi
done

# Optional, untracked, deployment-specific patterns.
if [[ -f .private-strings ]]; then
  while IFS= read -r pat; do
    [[ -z "$pat" || "$pat" == \#* ]] && continue
    hits=$(git grep -nIiE "$pat" -- . ":!$SELF" 2>/dev/null || true)
    if [[ -n "$hits" ]]; then
      echo "::error::matched a pattern from .private-strings"
      echo "$hits"
      status=1
    fi
  done < .private-strings
fi

if [[ $status -eq 0 ]]; then
  echo "OK: no private strings found."
fi
exit $status
