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
# Two traps this script is written around, both of which make a pattern match
# nothing at all rather than fail loudly:
#
#   * `git grep -E` is POSIX ERE, where `\b` is NOT a word boundary. A pattern
#     written with `\b` is inert, so explicit `(^|[^...])` guards are used.
#   * a pattern beginning with `-` is read as an option unless it is passed
#     after `-e`.
#
# The self-test at the bottom exists because of those two: it proves every
# expression still fires against a known-bad sample before a pass is trusted.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

SELF='scripts/check-no-private-strings.sh'

# --- IPv4 -------------------------------------------------------------------
# Checked separately, with its own allowlist. It must not share the general
# ALLOW: a rule permitting a version string like "1.2.3", would also permit the
# tail of a real address written "198.51.100.7", which is the exact hole this
# script exists to close.
IPV4='(^|[^0-9.])([0-9]{1,3}\.){3}[0-9]{1,3}([^0-9.]|$)'
IPV4_ALLOW='^(127\.0\.0\.1|0\.0\.0\.0|255\.255\.255\.(0|255)|10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|169\.254\.|192\.0\.2\.|198\.51\.100\.|203\.0\.113\.)'

# --- everything else --------------------------------------------------------
PATTERNS=(
  # ssh to a named host
  '(^|[^a-z])ssh +[a-z_][a-z0-9_-]*@'
  # absolute paths from a developer machine
  '/Users/[a-z]'
  '/home/[a-z]'
  # credential-shaped assignments
  '(api[_-]?key|secret|passwd|password|token)[[:space:]]*[=:][[:space:]]*['"'"'"][^'"'"'"[:space:]]{8,}'
  # private key material
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
  # cloud / provider access key ids
  'AKIA[0-9A-Z]{16}'
  # provider token prefixes
  '(^|[^A-Za-z0-9])(sk|pk|ghp|gho|xox[baprs])[-_][A-Za-z0-9]{16,}'
)

# Lines that are legitimately allowed to match one of the PATTERNS above.
ALLOW='(localhost|example\.com|example\.invalid|your-domain\.example|your-server|CHANGE_ME|change-me|hunter2|/Users/\[|/home/\[)'

status=0

# --- IPv4 scan --------------------------------------------------------------
ip_hits=$(git grep -hIE -e "$IPV4" -- . ":!$SELF" ':!LICENSE' 2>/dev/null \
          | grep -oE -e '([0-9]{1,3}\.){3}[0-9]{1,3}' \
          | sort -u \
          | grep -vE -e "$IPV4_ALLOW" || true)
if [[ -n "$ip_hits" ]]; then
  echo "::error::IPv4 literal outside the loopback / private / documentation ranges"
  for addr in $ip_hits; do
    git grep -nIF -e "$addr" -- . ":!$SELF" ':!LICENSE' 2>/dev/null || true
  done
  status=1
fi

# --- pattern scan -----------------------------------------------------------
for pat in "${PATTERNS[@]}"; do
  hits=$(git grep -nIE -e "$pat" -- . ":!$SELF" ':!LICENSE' 2>/dev/null \
         | grep -vE -e "$ALLOW" || true)
  if [[ -n "$hits" ]]; then
    echo "::error::possible private string matching /$pat/"
    echo "$hits"
    status=1
  fi
done

# --- optional, untracked, deployment-specific patterns ----------------------
if [[ -f .private-strings ]]; then
  while IFS= read -r pat; do
    [[ -z "$pat" || "$pat" == \#* ]] && continue
    hits=$(git grep -nIiE -e "$pat" -- . ":!$SELF" 2>/dev/null || true)
    if [[ -n "$hits" ]]; then
      echo "::error::matched a pattern from .private-strings"
      echo "$hits"
      status=1
    fi
  done < .private-strings
fi

# --- self-test --------------------------------------------------------------
# A silently non-matching pattern is worse than no pattern at all.
probe() {
  # -e is required: a pattern starting with "-" is otherwise read as an option.
  if ! printf '%s\n' "$2" | grep -qE -e "$1"; then
    echo "::error::self-test failed, pattern does not match its sample: /$1/"
    return 1
  fi
}
# Every sample below is synthetic by construction: the addresses are RFC 5737
# documentation ranges, which can never route, and the key-shaped strings spell
# EXAMPLE. The private-key line is a header with no key material.
selftest=0
probe "$IPV4"        'resolved to 203.0.113.7, somewhere'       || selftest=1
probe "$IPV4"        'host 192.0.2.4'                           || selftest=1
probe "${PATTERNS[0]}" 'run: ssh deployer@build-host uptime'    || selftest=1
probe "${PATTERNS[1]}" '/Users/someone/project'                 || selftest=1
probe "${PATTERNS[2]}" '/home/someone/project'                  || selftest=1
probe "${PATTERNS[3]}" 'password = "correcthorsebattery"'       || selftest=1
probe "${PATTERNS[4]}" '-----BEGIN RSA PRIVATE KEY-----'        || selftest=1
probe "${PATTERNS[5]}" 'AKIAEXAMPLEEXAMPLE12'                   || selftest=1
probe "${PATTERNS[6]}" 'token ghp_EXAMPLEEXAMPLEEXAMPLE'        || selftest=1
if [[ $selftest -ne 0 ]]; then
  echo "::error::the scanner cannot be trusted; fix the patterns before relying on a pass"
  exit 2
fi

if [[ $status -eq 0 ]]; then
  echo "OK: no private strings found (7 patterns + IPv4, all self-tested)."
fi
exit $status
