"""How many Binance.US pairs could actually carry a trade?

The bot is idle because almost nothing qualifies, and the user's lever --
expand the opportunity universe -- is the right one. But expanding has a price:
pairs outside the Tier-0 fee list pay 10 bps/side instead of ~1, and thinner
books pay more slippage. This measures the trade-off rather than assuming it.
"""
import json, urllib.request

TIER0 = {"BTCUSD","ETHUSD","SOLUSD","BNBUSD","ADAUSD","AVAXUSD","VETUSD","POLUSD",
         "ATOMUSD","ONEUSD","DOTUSD","NEARUSD","FETUSD","SUIUSD","LPTUSD",
         "XTZUSDT","ROSEUSDT","KSMUSDT","KAVAUSDT","TUSDT","ONTUSDT","CELRUSDT","AUDIOUSDT"}

def get(u):
    r = urllib.request.Request(u, headers={"User-Agent":"Mozilla/5.0"})
    return json.load(urllib.request.urlopen(r, timeout=30))

info = get("https://api.binance.us/api/v3/exchangeInfo?showPermissionSets=false")
status = {s["symbol"]: s["status"] for s in info["symbols"]}
quote  = {s["symbol"]: s["quoteAsset"] for s in info["symbols"]}
base   = {s["symbol"]: s["baseAsset"] for s in info["symbols"]}

tick = get("https://api.binance.us/api/v3/ticker/24hr")
rows = []
for t in tick:
    s = t["symbol"]
    if status.get(s) != "TRADING": continue
    if quote.get(s) not in ("USD","USDT"): continue
    if base.get(s) in ("USDT","USDC","BUSD","DAI","TUSD","USDP","PYUSD"): continue
    try:
        qv = float(t["quoteVolume"]); n = int(t["count"])
        hi, lo, wap = float(t["highPrice"]), float(t["lowPrice"]), float(t["weightedAvgPrice"] or 0)
    except Exception:
        continue
    vol_bps = ((hi-lo)/wap*1e4) if wap>0 else 0.0
    rows.append((s, qv, n, vol_bps, s in TIER0))

rows.sort(key=lambda r: -r[1])
print(f"{len(rows)} TRADING USD/USDT pairs\n")
print(f"{'min 24h $vol':>14}{'min trades':>12}{'pairs':>8}{'tier0':>8}{'non-tier0':>11}")
print("-"*55)
for minv, minn in ((2_000_000,5000),(1_000_000,2000),(500_000,1000),
                   (250_000,500),(100_000,200),(50_000,100),(25_000,50)):
    ok = [r for r in rows if r[1]>=minv and r[2]>=minn]
    t0 = sum(1 for r in ok if r[4])
    print(f"{minv:>14,}{minn:>12,}{len(ok):>8}{t0:>8}{len(ok)-t0:>11}")

print("\nTop 25 by 24h quote volume:")
print(f"{'symbol':<12}{'24h $vol':>14}{'trades':>10}{'vol bps':>9}  tier0")
for s,qv,n,vb,t0 in rows[:25]:
    print(f"{s:<12}{qv:>14,.0f}{n:>10,}{vb:>9.0f}  {'YES' if t0 else '-'}")

print("\nTier-0 pairs and where they stand:")
for s,qv,n,vb,t0 in sorted([r for r in rows if r[4]], key=lambda r:-r[1]):
    print(f"  {s:<12}{qv:>14,.0f}{n:>10,}{vb:>9.0f}")
