#!/usr/bin/env bash
# Securely store Binance.US API credentials on the server.
#
#   sudo /srv/netedge/scripts/configure-exchange.sh
#
# IMPORTANT — read before running:
#   * V1 of this bot is PAPER ONLY and does not use these credentials at all.
#     There is no signing code in the application; the paper-only guard in
#     app/safety.py fails the build if any appears. Running this script does
#     NOT enable live trading, and nothing else in this repo turns it on.
#   * Never paste an API secret into a chat window, a git commit, or a ticket.
#     This script is the only place it should ever be typed.
#   * The key you create on Binance.US must have WITHDRAWALS DISABLED.
#
set -euo pipefail

SECRETS_FILE="/srv/netedge/.exchange-credentials"
ENV_FILE="/srv/netedge/.env"

if [[ $EUID -ne 0 ]]; then
  echo "Bu script root olarak çalıştırılmalı: sudo $0" >&2
  exit 1
fi

echo "=============================================================="
echo " Binance.US API anahtarı yapılandırması"
echo "=============================================================="
echo
echo "UYARI: Bu bot şu anda PAPER modunda ve bu anahtarları KULLANMIYOR."
echo "Anahtarı şimdi kaydetmek canlı işlemi AÇMAZ."
echo
read -r -p "Devam edilsin mi? [e/H] " confirm
[[ "${confirm:-}" =~ ^[eE]$ ]] || { echo "İptal edildi."; exit 0; }

read -r -p "API Key: " api_key
read -r -s -p "API Secret: " api_secret
echo

if [[ -z "$api_key" || -z "$api_secret" ]]; then
  echo "Boş değer kabul edilmiyor." >&2
  exit 1
fi

umask 077
cat > "$SECRETS_FILE" <<EOF
# Binance.US credentials. chmod 600, root-owned, never committed, never logged.
# Written $(date -u +%Y-%m-%dT%H:%M:%SZ) by configure-exchange.sh
BINANCEUS_API_KEY=$api_key
BINANCEUS_API_SECRET=$api_secret
EOF
chmod 600 "$SECRETS_FILE"
chown root:root "$SECRETS_FILE"

# Deliberately NOT added to .env: the application must not be able to read these
# until a future, separately reviewed phase explicitly wires them in.
if grep -q "BINANCEUS_API" "$ENV_FILE" 2>/dev/null; then
  echo "UYARI: .env içinde exchange anahtarı referansı var — kontrol edin." >&2
fi

echo
echo "Kaydedildi: $SECRETS_FILE (chmod 600, root)"
echo "Uygulama bu dosyayı okumuyor. Canlı işlem hâlâ KAPALI."
echo
echo "Kontrol listesi:"
echo "  [ ] Anahtarda WITHDRAWAL izni KAPALI"
echo "  [ ] IP kısıtlaması: your-server"
echo "  [ ] Sadece 'Enable Reading' açık (Spot Trading bile şimdilik kapalı)"
