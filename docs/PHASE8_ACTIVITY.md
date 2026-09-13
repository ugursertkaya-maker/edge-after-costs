# Aktivite araştırması — hangi koşul kurulumları baskılıyor?

Tarih: 2026-09-13 · Mod: **PAPER** · Gerçek emir: **0**

## 1. Giriş hunisi — ilk bağlayıcı koşul

`momentum_scalp`, 8.054 değerlendirme, **51 sinyal (%0,633)**:

| koşul | kez | pay | eşik | medyan | p75 | p90 | p95 |
|---|---|---|---|---|---|---|---|
| REGIME_NOT_ALLOWED | 3.656 | **45,4%** | — | — | — | — | — |
| **ret_1m** | 3.013 | **37,4%** | 0,150 | **0,000** | −0,000 | −0,000 | 0,000 |
| volume_ratio | 709 | 8,8% | 1,150 | 1,000 | 1,000 | 1,000 | 1,000 |
| ema_slope | 548 | 6,8% | 0,060 | 0,006 | 0,036 | 0,051 | 0,056 |
| ret_5m_agree | 53 | 0,7% | 0,000 | −0,945 | −0,377 | −0,155 | −0,120 |
| trade_flow | 24 | 0,3% | 0,050 | −0,157 | −0,000 | 0,000 | 0,026 |

Diğer stratejiler: `breakout_scalp` %0,050 sinyal (rejim %83,0), `trend_pullback`
%0,037 (dist_to_ema %43,6), `flow_impulse` %0,521, `book_imbalance` %0
(imbalance %71,4), `mean_reversion` %0 (not_stretched %31,5).

## 2. Rejim kapısı kusur değil

| rejim | pay | | strateji | kapsadığı pazar |
|---|---|---|---|---|
| TRENDING | 44,1% | | momentum_scalp | %55,5 |
| RANGING | 20,2% | | book_imbalance | %81,7 |
| LOW_VOLATILITY | 17,5% | | flow_impulse | %55,6 |
| HIGH_VOLATILITY | 11,5% | | trend_pullback | %44,1 |
| ABNORMAL | 6,7% | | mean_reversion | %37,7 |
| BREAKOUT | 0,1% | | breakout_scalp | %17,6 |

`REGIME_NOT_ALLOWED` %45,4, momentum'un %55,5 kapsamının tam tümleyeni.
Tasarım, kusur değil.

## 3. `ret_1m` gevşetilemez

Eşik 0,150; **gözlenen medyan 0,000, p90 −0,000**. Dağılım sıfırda dejenere.
Eşiği 0,01'e indirmek, fiyatın hiç hareket etmediği dakikaları kabul etmektir —
yakalanacak hareket yok, ödenecek ~8 bps var. Bu bir eşik sorunu değil, ETHUSD
barlarının **%80,4'ünde yüksek = düşük** olmasının sonucudur.

## 4. Kapasite kapağı — bulunan en büyük tek kusur

`capacity_max_usd` Faz 6'dan beri vardı ve yalnızca **evrene girme** kararına
bağlıydı; emir boyutunu hiç sınırlamıyordu.

| profil | ort. pozisyon | | kayma (bps) | | NET $ | |
|---|---|---|---|---|---|---|
| | öncesi | sonrası | öncesi | sonrası | öncesi | sonrası |
| CURRENT | $1.352 | **$55** | 6,72 | 6,10 | −2.012 | **−43,91** |
| MODERATE | $1.167 | $55 | 6,51 | 6,08 | −2.698 | −65,06 |
| ACTIVE | $1.149 | $55 | 6,50 | 6,08 | −2.760 | −72,39 |
| HIGH_ACTIVITY | $1.144 | $55 | 6,48 | 6,09 | −2.786 | −77,53 |

ETHUSD dakikada ~$22 emiyor; $1.352'lik emir defteri yürüyor ve simülatör bunu
dürüstçe ücretlendiriyordu. Kelepçe `RiskEngine._apply_clamps`'e kondu — hem
canlı tahsisçinin hem backtest'in geçtiği tek kapı.

## 5. Aktivite / kalite tablosu (120 gün, 3 sembol, kapasiteye uygun boyut)

| Profil | Kurulum/gün | İşlem/gün | Kullanım | Devir/gün | NET $ | PF | Beklenti | Maks DD |
|---|---|---|---|---|---|---|---|---|
| CURRENT | 74,4 | 7,12 | %1,10 | 0,078 | −43,91 | 0,21 | −0,0520 | 43,91 |
| MODERATE | 159,8 | 9,81 | %1,10 | 0,108 | −65,06 | 0,20 | −0,0559 | 65,06 |
| ACTIVE | 199,7 | 11,18 | %1,10 | 0,123 | −72,39 | 0,20 | −0,0546 | 72,39 |
| HIGH_ACTIVITY | 248,8 | 11,75 | %1,10 | 0,130 | −77,53 | 0,19 | −0,0556 | 77,53 |

**Terfi eden profil yok.** Hiçbirinde pozitif NET beklenti yok.

## 6. Cevaplar

**1. Hangi koşul kurulumları baskılıyor?**
Sırayla: `REGIME_NOT_ALLOWED` (%45,4, tasarım) ve `ret_1m` (%37,4). İkincisi
bağlayıcı olan ama gevşetilemeyen — medyanı sıfır.

**2. Ne kadar güvenle gevşetilebilir?**
Gevşetme **çalışıyor**: arz 74 → 249 kurulum/gün, işlem 7,1 → 11,8/gün.
Ve işlem başına beklenti **bozulmuyor** (−0,0520 → −0,0556). Yani mevcut
sıkılık kalite satın almıyordu; sadece aktiviteyi kısıyordu. Ama hepsi
negatif olduğu için hiçbiri güvenli değil — gevşetme kaybı büyütüyor.

**3. En yüksek sürdürülebilir aktivite hangi profilde?**
**Hiçbiri.** Kural pozitif OOS beklenti ve PF > 1 istiyor; en iyisi PF 0,21.

**4. Gerçekçi işlem/gün?**
3 sembolde **7–12 işlem/gün**. 20–30+/gün bu evrende ancak eşikleri sıfıra
indirerek olur ve beklenti negatif kalır.

**5. Anlamlı sermaye çalıştırabilir mi?**
**Hayır.** Kapasiteye uygun boyutta kullanım **%1,10** — $5.000'in $55'i.
Bu bir politika seçimi değil, borsanın derinliğidir: ETHUSD dakikada ~$22,
BTCUSD ~$12, SOLUSD ~$17 emiyor. $4.000 aktif sermaye bu borsada ulaşılabilir
değil; zorlamak kaymayı 25 kat artırıp kaybı −$43'ten −$2.012'ye çıkarıyor —
tam olarak bu araştırmada ölçülen şey.
