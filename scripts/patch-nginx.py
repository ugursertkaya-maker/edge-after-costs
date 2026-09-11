#!/usr/bin/env python3
"""Additively patch the shared nginx vhost for your-domain.example.

Two changes, both reversible and both confined to this one server block:

1.  Add ``/cryptobot`` and ``/cryptobot/api/`` locations, placed ABOVE the
    existing ``/api/`` and ``/`` blocks so the longer prefix wins.
2.  Stamp an "ARŞİV — DURDURULDU" banner onto the archived ``/other-project`` pages
    via ``sub_filter``, so nobody mistakes a frozen dashboard for a live one.
    This needs no rebuild of the another project web container.

Run on the server:  sudo python3 patch-nginx.py /etc/nginx/conf.d/site.conf
Revert:             restore the .bak written next to the file.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

CRYPTOBOT_BLOCK = """    # ---- Crypto Micro-Profit Trader (PAPER) — served under /cryptobot ----
    # Additive only. Must sit ABOVE `location /api/` and `location /`.
    location /cryptobot/api/ {
        proxy_pass http://cryptobot-api:8000;      # path preserved
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 60s;
    }
    location /cryptobot {
        proxy_pass http://cryptobot-web:3000;      # Next.js basePath=/cryptobot
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
    # ---- end /cryptobot ----

"""

ARCHIVE_BANNER = (
    "        # Archived 2026-09-10: the another project worker is stopped. Stamp a\n"
    "        # status banner on the served HTML so the page cannot be mistaken\n"
    "        # for a live one. Removing these four lines fully reverts it.\n"
    '        proxy_set_header Accept-Encoding "";\n'
    "        sub_filter_once on;\n"
    "        sub_filter '</body>' '<div style=\"position:fixed;left:0;right:0;bottom:0;"
    "z-index:99999;background:#7a1f1f;color:#fff;font:600 12px/1.6 ui-monospace,monospace;"
    "text-align:center;padding:6px 10px;letter-spacing:.08em\">"
    "ARCHIVED PROJECT - STOPPED"
    "(10 Eyl&#252;l 2026). Veri dondurulmu&#351;tur, tarama &#231;al&#305;&#351;m&#305;yor."
    "</div></body>';\n"
)


def patch(path: Path) -> int:
    text = path.read_text()
    changed = False

    if "/cryptobot" not in text:
        anchor = "    location /api/ {"
        if anchor not in text:
            print("ERROR: could not find `location /api/` anchor", file=sys.stderr)
            return 2
        text = text.replace(anchor, CRYPTOBOT_BLOCK + anchor, 1)
        changed = True
        print("added /cryptobot locations")
    else:
        print("/cryptobot already present, skipping")

    if "sub_filter" not in text and "location /other-project {" in text:
        marker = 'proxy_pass http://other-project-web:3000;      # Next.js basePath=/other-project expects the prefix\n'
        if marker in text:
            text = text.replace(marker, marker + ARCHIVE_BANNER, 1)
            changed = True
            print("added /other-project archive banner")
        else:
            print("WARN: /other-project proxy_pass line not matched, banner skipped")
    else:
        print("archive banner already present or /other-project absent, skipping")

    if not changed:
        return 0

    backup = path.with_suffix(path.suffix + ".bak")
    if not backup.exists():
        shutil.copy2(path, backup)
        print(f"backup written: {backup}")
    path.write_text(text)
    print(f"patched: {path}")
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "/etc/nginx/conf.d/site.conf")
    raise SystemExit(patch(target))
