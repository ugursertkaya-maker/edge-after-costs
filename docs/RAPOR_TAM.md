# Binance.US Kripto Micro-Profit Paper Trader — Tam Rapor (Faz 1–6)

**Tarih:** 2026-09-12 · **Mod:** PAPER · **Gerçek emir:** 0 · **Kaldıraç:** yok
**Borsa kimlik bilgisi:** yok · **Testler:** 1.939 geçti, 1 atlandı · **Commit:** 56
**Canlı:** https://your-domain.example/cryptobot

> Bu dosya kendi kendine yeterlidir. Tüm sayılar ölçümdür; tahmin veya hatırlama değildir.
> Üretildikleri araçlar her bölümde adıyla belirtilmiştir.

---

## 0. Tek paragrafta sonuç

Bu hesabın bar tabanlı üç stratejisi (`momentum_scalp`, `breakout_scalp`,
`trend_pullback`) **mid fiyatta, hiçbir maliyet düşülmeden önce para kaybediyor** —
120 günde 366 işlem, ham kâr faktörü **0,791**, üçü de tek tek 1,0'ın altında.
Maliyet bu kaybı beşe katlıyor ama **sebebi değil**. Beş rapor boyunca "maker icrası
bunu çözer" dedim; bu yanlıştı ve düzeltildi: **mid'de kaybeden bir stratejiden
maliyet çıkaramazsınız.**

Projede kendi kıyas ölçüsünü yenen **tek bir şey** var: `flow_impulse`. Piyasanın aynı
tutma aralığında verdiği **0,18 bps**'e karşılık **12,10 bps** kazanıyor — yani
sürüklenme (beta) değil, gerçek bir kenar. Ama örneklem **48 işlem, tek takvim günü,
%93,8 isabet**. Bu bir kanıt değil, bir adaydır. **Terfi ettirilmedi.**

---

## 1. Muhasebe denetimi — rakam doğruydu, etiket yanlıştı

### 1.1 İtiraz ve cevabı

Faz 5 şöyle raporlamıştı:

```
gross −$34,41 · ücret $8,00 · kayma $25,03 · net −$42,40
−34,41 − 8,00 − 25,03 = −67,44 ≠ −42,40
```

Çıkarma doğru, çıkarımı yanlış. `gross` bu kod tabanında **maliyet öncesi kâr değil**.
`Ledger.close` şunu hesaplıyor:

```python
gross = (exit_fill_price - entry_fill_price) * qty   # GERÇEKLEŞEN fiyatlar
net   = gross - entry_fee - exit_fee
slippage_usd = entry_notional * entry_slippage_bps/1e4 \
             + proceeds      * exit_slippage_bps/1e4   # mid'e karşı ölçülen NOT
```

Gerçekleşen fiyatlar spread ve kaymayı zaten içeriyor (simülatör defteri yürüyor).
Yani `−34,4053 − 7,9973 = −42,4026` — tam. **Hiçbir maliyet iki kez sayılmadı.**

Ama buna "gross" demek, bunu söylemeden, gerçek bir raporlama kusuruydu.

### 1.2 Belirsizliği olmayan ayrıştırma

```
RAW_MARKET_PNL   = karar anındaki mid'de kâr (kusursuz icra)
                 = raporlanan gross + spread_ve_kayma
SPREAD+KAYMA     = her iki bacakta mid'den uzaklaşılan her şey
ENTRY_FEE / EXIT_FEE
NET_PNL          = RAW − (SPREAD+KAYMA) − ENTRY_FEE − EXIT_FEE   [≡ gross − ücretler]
```

Spread ve kayma olaydan sonra **ayrıştırılamaz**: simülatör ikisini birlikte, karar
anındaki mid'e karşı ölçer. Bu uydurulmuş iki sayıya bölmek yerine böyle belirtilmiştir.

`api/scripts/pnl_audit.py` her **tek** işlem için NET'i RAW'dan yeniden türetir ve
float hatasından fazla sapma varsa ihlal yazar. **505 işlem, sıfır ihlal.**

### 1.3 İki örneklem

| Örneklem | İşlem | RAW $ | Spread+kayma $ | Ücret $ | NET $ | Ham KF | Net KF |
|---|---|---|---|---|---|---|---|
| Backtest · 120 gün | 366 | −12,42 | 37,52 | 12,01 | −61,95 | **0,791** | 0,303 |
| Canlı · 12,66 saat | 139 | +21,75 | 13,63 | 9,39 | −1,28 | **4,251** | 0,922 |

Backtest strateji kırılımı — **üçü de tek tek 1,0 altında**:

| Strateji | İşlem | RAW | Ham KF |
|---|---|---|---|
| momentum_scalp | 224 | −8,95 | 0,764 |
| breakout_scalp | 122 | −2,28 | 0,876 |
| trend_pullback | 20 | −1,18 | 0,630 |

### 1.4 İlk yorumum yanlıştı

Bunu bir "örneklem çelişkisi" sanıp uzun olanın tarafını tuttum. İkisi çelişmiyordu —
**hiç örtüşmüyorlardı.** Canlı sonucu taşıyan iki strateji 120 günde **sıfır** işlem
yapmış, üstelik tesadüfen değil. Üç günlük sinyal sayımı sebebi gösterdi:

- **`book_imbalance` hiç sinyal üretmiyor** (0). Duran defteri okur; harness defteri
  bardan sentezler, dolayısıyla bağlı olduğu özellik hiç hareket etmez.
- **`flow_impulse` bolca üretiyor** — 3 günde 43 sinyal — ve **nitelenen her biri
  `NOT_SUPPORTED_SHORT` ile reddediliyor.** Sentetik tape'e karşı yalnızca short
  gidiyor, bu ise bir spot hesabı. 10 nitelenen, 10 red, 0 işlem.

Yani 120 günlük backtest o iki strateji hakkında **zayıf kanıt değil, kanıtsızlık.**

---

## 2. Alfa mı, yükselen piyasada long olmak mı?

### 2.1 Neden sıfır yanlış kıyas

Canlı örneklemin **139 işleminin hepsi LONG** ve kapsadığı 12,66 saat tek yönlü bir
ralliydi:

| Sembol | Açılış | Kapanış | Hareket | Maks yukarı | Maks aşağı |
|---|---|---|---|---|---|
| BTCUSD | 76.804,99 | 77.681,13 | +114,1 bps | +391,8 | −95,2 |
| ETHUSD | 2.443,84 | 2.562,21 | **+484,4 bps** | +901,6 | −39,6 |
| SOLUSD | 99,15 | 101,59 | +246,1 bps | +647,5 | −122,0 |

Böyle bir bantta long-only bir scalper, bir şey öngörsün ya da öngörmesin yüksek
isabet gösterir. Doğru kıyas: **aynı sembolde, aynı aralıkta, aynı pozisyonu tutmak.**

### 2.2 ALPHA = RAW − PİYASA (`api/scripts/beta_check.py`)

| Strateji | n | Ham bps | Piyasa bps | **Alfa bps** | %95 GA | Tutuş |
|---|---|---|---|---|---|---|
| **flow_impulse** | 48 | 12,10 | **0,18** | **+11,93** | **[+5,43 · +18,42]** | 32 sn |
| momentum_scalp | 67 | 6,33 | 5,48 | +0,85 | [−2,11 · +3,82] | 71 sn |
| book_imbalance | 21 | 2,66 | 2,11 | +0,55 | [−1,89 · +2,99] | 114 sn |
| breakout_scalp | 3 | −1,41 | 0,00 | −1,41 | — | 53 sn |
| **TOPLAM** | 139 | 7,60 | 3,02 | +4,58 | [+1,77 · +7,39] | 64 sn |

**`momentum_scalp` betaymış** — 6,33 bps'in 5,48'ini piyasa vermiş, kalanı sıfırdan
ayırt edilemiyor. 120 günlük testin aynı strateji hakkında söylediği şey, canlı taraftan
ikinci bir yöntemle doğrulandı. `book_imbalance` aynı hikâye.

**`flow_impulse` değil.** 32 saniye tutuyor ve sürüklenme olmayan bir şey yakalıyor.

### 2.3 flow_impulse sağlamlık kontrolleri (48 işlem az olduğu için)

```
t = 3,60                    %95 GA [+5,43 · +18,42]
Bonferroni ×4 GA            [+3,64 · +20,21]      (4 strateji test edildi)
medyan                      +9,76 bps              (tek uç değerden gelmiyor)
pozitif işlem               48'de 36
en iyi 1 işlem çıkarılınca  +10,47 bps
en iyi 3 işlem çıkarılınca  +7,35 bps
ilk yarı / ikinci yarı      +4,59 / +19,26 bps
BTCUSD / ETHUSD / SOLUSD    +8,64 / +9,21 / +18,47 bps  (üçü de pozitif)
```

Ayrıca projedeki **tek net pozitif satır**: +$4,24, net KF 6,649. 2,5 bps'lik Tier-0
gidiş-dönüşünde 12 bps'lik brüt kenar maliyet duvarını rahatça aşıyor.

---

## 3. "BTC baskın" iddiası — ölçüm ne diyor

### 3.1 Baskınlık yok

139 canlı işlemin dağılımı:

| Sembol | İşlem | Pay | Net $ |
|---|---|---|---|
| BTCUSD | 58 | **%41,7** | −3,795 |
| ETHUSD | 51 | %36,7 | −0,018 |
| SOLUSD | 30 | %21,6 | +2,532 |

%70 yoğunlaşma bayrağı **açılmazdı**. Gerçek şikâyet haklıydı ama sebebi başkaydı:
253 işlem gören çiftten yalnızca 3'ü işleniyordu.

### 3.2 Evren, sayılarla (`api/scripts/funnel_audit.py`)

629 listeli çift, 416'sı quote varlığına göre uygun:

| Neden elendi | Çift | Pay | Bizim kontrolümüzde mi? |
|---|---|---|---|
| 24s hacim likidite barının altında | 233 | **%56,0** | hayır |
| Borsa çifti durdurmuş (`status BREAK`) | 160 | **%38,5** | hayır |
| Sabit kodlanmış Tier-0 listesi | 13 | %3,1 | evet |
| Denylist (stablecoin, kaldıraçlı token) | 5 | %1,2 | evet |
| **Seçilen** | **4** | %1,0 | — |

**Evrenin %94,5'i, bizim hiç etkilemediğimiz sebeplerle yok.** Briefin şüphelendiği
Tier-0 listesi 13 çifte mal oluyor.

### 3.3 İki gerçek kusur — ikisi de sıralamanın yukarısında

**Kusur 1 — worker yanlış metodu çağırıyordu.** Faz 5, tüm borsayı HOT/WARM/COLD
katmanlayan `refresh_tiers()`'i yazdı, test etti ve **hiç bağlamadı**. Canlı yol hâlâ
`refresh()`'teydi: likidite öncelikli sıralama, `max_symbols`'da kesme, kapasite
kontrolü yok. Bu projede **dördüncü kez** doğru yazılmış ve test edilmiş bir bileşenin
üretimden erişilemez olduğu ortaya çıktı. Bağlandı; eski yol yedek olarak duruyor
(depth probe hatası evreni boşaltamasın diye).

**Kusur 2 — hiçbir yer bir çiftin BİZİM büyüklüğümüzü emip ememeyeceğini ölçmüyordu.**
24 saatlik hacme göre sıralamak BTC'yi kalıcı kazandırır ve $25'lik emrin piyasayı
oynatıp oynatmadığı hakkında hiçbir şey söylemez. Doğru ölçü: emrin **bir dakikalık**
akışa oranı (32 saniyelik bir scalp, 24 saatin toplamıyla değil o pencerede gelen
akışla yarışır).

| Çift | 24s hacim | Dakikada emebildiği | $25 emir | $50 emir |
|---|---|---|---|---|
| ETHUSD | $1.778.425 | $61,75 | %2,0 | %4,0 |
| BTCUSD | $1.190.091 | $41,32 | %3,0 | %6,0 |
| SOLUSD | $301.108 | $10,46 | %12,0 | %23,9 |
| **ADAUSD** | $161.688 | **$5,61** | %22,3 | %44,5 |
| BNBUSD | $56.091 | $1,95 | %64,2 | %128,4 |
| AVAXUSD | $9.493 | $0,33 | %379,2 | %758,5 |

**ADAUSD'nin açıklaması:** tüm Faz 5 boyunca HOT katmanında oturdu ve bir kez bile işlem
görmedi. Akışı dakikada $5,61 emiyor, minimum emrimiz $10. Hiçbir zaman işlenebilir
değildi ve sistemde bunu söyleyebilecek hiçbir şey yoktu.

Kapasite **boyutlandırır, listeden çıkarmaz** — ince bir çiftin doğru cevabı $10'lık
emirdir, çiftin silinmesi değil. ("İzin verilen en büyük boyut"a göre kapı koymak,
Faz 5'in risk kapısının yaptığı ve baskın atıl sebebi yanlış teşhis etmesine yol açan
hatanın ta kendisi; kasıtlı olarak tekrarlanmadı.)

### 3.4 Likidite barı kısıt değil — kapasite kısıt

| Min 24s hacim | Min işlem | Sadece Tier-0 | İşlenebilir çift | Medyan gidiş-dönüş |
|---|---|---|---|---|
| $100.000 | 200 | evet | 3 | 2,5 bps |
| $50.000 | 100 | evet | 3 | 2,5 bps |
| $10.000 | 20 | evet | 3 | 2,5 bps |
| $1.000 | 5 | evet | 3 | 2,5 bps |
| $100.000 | 200 | **hayır** | 7 | **20,7 bps** |

Kapasite kapısı aktifken likidite barını $100.000'den $1.000'e indirmek işlenebilir
evreni **tam olarak sıfır çift** değiştiriyor.

### 3.5 Tier-0 kısıtı haklı mı? Gerçekten tutulan vadelerde

Beklenen hareket `√zaman` ile ölçeklenir, dolayısıyla 60 dakikalık kenar oranı
32 saniyelik bir stratejiyi kayırır. Kenar oranı = beklenen hareket ÷ gidiş-dönüş:

| Çift | Gidiş-dönüş | 60 dk | flow_impulse (32 sn) | momentum (71 sn) |
|---|---|---|---|---|
| ETHUSD | 2,5 bps | 19,52 | **1,84** | 2,74 |
| BTCUSD | 2,5 bps | 10,55 | 0,99 | 1,48 |
| SOLUSD | 4,0 bps | 10,26 | 0,97 | 1,44 |
| ZECUSDT | 28,4 bps | 2,22 | 0,21 | 0,31 |
| XRPUSDT | 20,7 bps | 2,20 | 0,21 | 0,31 |
| DOGEUSDT | 24,7 bps | 1,52 | 0,14 | 0,21 |

`flow_impulse`'un gerçek vadesinde Tier-0 dışı bir çift, aynı sinyalde başabaş olmak
için BTC'den **yaklaşık 8 kat büyük** bir hareket ister.

**Ama kısıt bir mekanizma olarak kaldırıldı.** Tier-0 dışı çiftler artık taranıyor,
puanlanıyor ve net beklenen değere göre sıralanıyor; fırsat panosunda kaybetme
sebepleriyle görünüyorlar. Yalnızca alternatifi hiç denenmediği için ayakta kalan bir
iddia bulgu değildir.

### 3.6 Kümülatif huni — canlı kanıt (1.807 değerlendirme)

| Sembol | Katman | Aday | Sıralandı | İşlem | Darboğaz |
|---|---|---|---|---|---|
| BTCUSD | HOT | 139 | 139 | 0 | selected |
| SOLUSD | HOT | 120 | 120 | 0 | selected |
| XRPUSDT | WARM | 8 | 8 | 0 | selected |
| DOGEUSDT | WARM | 3 | 3 | 0 | selected |
| ETHUSD | HOT | 0 | 0 | 0 | candidate |

WARM çiftler gerçekten aday üretiyor ve sıralanıyor — yasaklı değiller.

---

## 4. Tutma süresi: endişe ters yöndeymiş

**Bu projede hiçbir işlem 293 saniyeyi geçmedi.** 5–10 dk, 10–25 dk, 25–60 dk ve 60 dk
üstü kovalarının hepsi boş; V2'de yazılan 25 dakikalık kontrol noktası ve 60 dakikalık
zaman durdurucu **bir kez bile çalışmadı.**

| Strateji | n | Medyan | Ortalama | Maks | Net $ | Çıkış sebepleri |
|---|---|---|---|---|---|---|
| momentum_scalp | 67 | **27 sn** | 71 sn | 293 sn | +1,17 | 33 TP · 18 SL · 16 trail |
| flow_impulse | 48 | 24 sn | 32 sn | 122 sn | +4,24 | 18 TP · 3 SL · 27 trail |
| book_imbalance | 21 | 89 sn | 114 sn | 275 sn | −5,57 | 5 TP · 10 SL · 6 trail |
| breakout_scalp | 3 | 53 sn | 53 sn | 53 sn | −1,12 | 3 SL |

Asıl sorun tersi: **`momentum_scalp` medyan 27 saniyede çıkıyor.** Yarım dakikadan kısa
sürede kapanan bir momentum stratejisi momentum işlemiyor.

### 4.1 Kova başına net K/Z — asla havuzlanmadan

| Strateji | <30 sn | 30–60 sn | 1–2 dk | 2–5 dk | 5 dk+ |
|---|---|---|---|---|---|
| flow_impulse | +1,85 (30) | +1,47 (12) | +0,36 (3) | +0,57 (3) | — |
| momentum_scalp | +0,80 (39) | +0,60 (4) | +0,47 (3) | **−0,69 (21)** | — |
| book_imbalance | −1,03 (2) | −1,84 (6) | −1,87 (5) | −0,83 (8) | — |
| breakout_scalp | — | −1,12 (3) | — | — | — |

Boş kova **boş** görünür, `$0,00` değil: "burada işlem yok" ile "bu işlemler sıfır
getirdi" farklı olgulardır.

### 4.2 Uygulanan tutma profilleri

| Strateji | Min | Tipik | Maks | Sönüm çıkışı | Giriş skoru yeniden değerlendirilir mi? |
|---|---|---|---|---|---|
| flow_impulse | **0 sn** | 30 sn | 180 sn | evet · 20 sn yarı ömür | evet |
| book_imbalance | **0 sn** | 120 sn | 600 sn | evet · 90 sn yarı ömür | evet |
| momentum_scalp | 60 sn | 600 sn | 2400 sn | hayır | **hayır** |
| breakout_scalp | 60 sn | 900 sn | 3600 sn | hayır | **hayır** |
| trend_pullback | 120 sn | 1500 sn | 3600 sn | hayır | **hayır** |
| mean_reversion | 60 sn | 600 sn | 2400 sn | kendi esneme hedefleri | **hayır** |

**Sönüm ölçülür, zamanlanmaz:**

```
sönüm    = 1 − clamp(tetik_şimdi / tetik_girişte, 0, 1)
tolerans = 0,5 ^ (tutulan_süre / yarı_ömür)
çıkış    ⟺ sönüm > tolerans
```

Girişte tolerans 1,0'dır → hiçbir şey giriş anında sönümden çıkamaz. **Bozulmamış bir
impuls her yaşta her toleransı geçer.** 30 saniyelik bir `flow_impulse` işlemi impuls
tükendiyse doğrudur; tükenmediyse 120 saniyelik olan doğrudur. Evrensel minimum tutma
**yok**; `min_hold_seconds` her iki akış stratejisi için sıfır.

Yavaş stratejilerde giriş skoru ile pozisyon yönetimi **ayrı** işlerdir
(`reeval_entry_score=False`): kayan bir giriş skoru bir momentum işlemini kapatamaz.

---

## 5. Sermaye devri ve epizot kimliği

`Ledger.close` nakdi zaten senkron geri veriyordu — boşluk orada değildi. Boşluk bir
katman yukarıdaydı: **portföy görünümü** (korelasyonlu risk, yönsel maruziyet, nakit
rezervi, `DUPLICATE_SYMBOL`) yalnızca bir sonraki durum inşasında tazeleniyordu; yani
kapanan bir sembol bir tick boyunca kullanılamaz kalıyordu.

`PortfolioEngine.release()` artık `_commit`'in tam tersi ve aynı çağrıda çalışıyor;
`CapitalRecycler.on_close` yeniden taramayı **o çağrının içinde** yapıyor
(`rescan = evaluate_once`). Yalnızca base defteri tetikler: karşılaştırma defterleri
aynı kararları yansıtır, bir karar için üç tarama üç kat iştir.

**Epizot kimliği:** `sha1(sembol | strateji | rejim | kova | sayaç)` — kova ve rejim
**epizot açıldığı anda dondurulur.** Her gözlemde yeniden hesaplamak, kayan bir
özelliğin impuls ortasında kova sınırını geçip aynı fikre ikinci bir kimlik üretmesine
izin verirdi: temiz sicille yeniden giriş. Sayaç yalnızca stratejinin tetiği
yanlış→doğru geçince ilerler. Aynı coin'de gerçekten yeni bir impuls serbestçe işlem
görür — **BTC dahil**.

**Küresel bekleme yok:** hesap geneli tek cooldown zarar serisinde çalışır, kârlı
kapanışta asla. SOL'u kapatmak ETH'yi açmayı engelleyemez.

**Atıl sermaye** 10 sebep koduna ayrılır ve atıl bakiyeye **tam olarak** toplanır;
artık kova yoktur. Yeni kovalar atıl toplamın **içinden** pay alır, üstüne eklenmez;
`CAPITAL_STATE_BUG` rezervden önce yazılır ki bir muhasebe kusuru politika gibi
aklanamasın.

---

## 6. TAKER · MAKER · HYBRID — iki çalışma, iki soru

### 6.1 Soru A — flow_impulse maker kullanabilir mi?

48 canlı `flow_impulse` sinyali, kaydedilmiş L2 + tape üzerinde yeniden oynatıldı.
Bekleme pencereleri medyan tutuşu 32 saniye olan bir sinyale göre seçildi: 30 saniyelik
alfada maker dolumu için 20 saniye beklemek icrayı değil, **alfanın sönmesini** ölçer.

| Bekleme | Maker dolum | Hybrid geçiş | TAKER net | MAKER net | MAKER − fırsat | HYBRID net | Markout |
|---|---|---|---|---|---|---|---|
| 50 ms | **%0,0** | %100 | $4,2410 | $0,0000 | −$4,2410 | $4,3525 | — |
| 100 ms | **%0,0** | %100 | $4,2410 | $0,0000 | −$4,2410 | $4,5227 | — |
| 250 ms | **%0,0** | %100 | $4,2410 | $0,0000 | −$4,2410 | $4,0214 | — |
| 500 ms | **%0,0** | %100 | $4,2410 | $0,0000 | −$4,2410 | $4,2532 | — |
| 1.000 ms | %6,2 | %93,8 | $4,2410 | $0,1038 | −$4,0803 | $3,8838 | +0,54 bps |
| 2.000 ms | %12,5 | %87,5 | $4,2410 | $0,1694 | −$3,6538 | $3,0473 | +0,62 bps |

**Maker icrası bu zaman ölçeğinde mevcut değil.** Dokunuşta ya da bir tick gerisinde
post-only kotasyon, yarım saniye içinde **%0** doluyor. Sabır dolum satın alır
(1 sn'de %6,2, 2 sn'de %12,5) ve aldığından fazlasına mal olur: hybrid net
$4,52 → $3,88 → $3,05.

**Hybrid, taker'dan ayırt edilemiyor.** Tüm kollar $3,88–$4,52 arasında ($0,64 yayılım);
taker toplamının **kendi standart hatası $0,80**. Yani tüm bekleme pencerelerindeki tüm
fark, **tek bir standart hatanın 0,8'i**. 100 ms satırını "%6,6 iyileşme" diye
raporlamak gürültü okumak olurdu ve öyle raporlamıyorum.

**Saf maker en kötü kol:** doldurabildiği $0,17'ye karşılık hiç dolduramadığı
sinyallerin kaçırdığı $3,65 → net **−$3,49**. Zararlıları atlamak, kazançlıları da
atlamadıysanız bir başarıdır.

### 6.2 Soru B — borsa dinlenmeyi ödüllendiriyor mu?

Farklı bir soru, ve cevabı **sembole göre değişiyor**. 33,94 saatlik tape ile, aracın
kendi 24 saat eşiğini `sufficient=True` ile geçerek:

| Sembol | Dolum 5 sn | Dolum 15 sn | Dolum 30 sn | Markout 1 sn | Markout 5 sn | Hüküm |
|---|---|---|---|---|---|---|
| **BTCUSD** | %6,0 | **%22,0** | **%33,0** | −0,18 bps | **−0,06 bps** | **DÜZ** |
| ETHUSD | %1,0 | %3,0 | %5,0 | −1,39 bps | −2,07 bps | **TERS SEÇİLİM** |
| SOLUSD | %0,0 | %2,0 | %4,0 | −1,00 bps | −1,75 bps | **TERS SEÇİLİM** |

**Dolum oranından ÖNCE markout'a bakın.** Yüksek dolum + negatif markout, düşük dolum +
düz markout'tan **daha kötüdür**: %0 maker ücretinin fiyattan geri ödendiği anlamına gelir.

**Naif maker backtest'i %33 abartırdı:** dokunulan 220 kotasyon hücresinin 54'ü
dokunuldu ama dolmadı (%24,5). Her dokunuşu dolum sayan bir model 166 yerine 220 dolum
raporlardı. Bu, çalışmanın neden fiyatı değil **kuyruğu** modellediğinin cevabıdır.

### 6.3 Sentez

"Maker burada işe yaramıyor"dan daha dar bir sonuç: **BTCUSD'de 15–30 saniyelik vadede
gerçek bir maker fırsatı var** — ve bu projede o vadede kenarı olan hiçbir strateji yok.
`flow_impulse` beklemek için fazla hızlı; `momentum_scalp` yeterince yavaş ve beta.
Kapatılacak bir kapı değil, adı konacak bir boşluk. Mevcut kitabı kurtarmıyor.

---

## 7. flow_impulse terfi kapıları — HAYIR

| Kapı | Değer | Sonuç |
|---|---|---|
| Örneklem ≥ 100 işlem | **48** | **KALDI** |
| Net kâr faktörü > 1,30 | 6,649 | geçti |
| Net beklenti > 0 | +$0,0884 | geçti |
| Alfa GA sıfırdan uzak | +5,43 bps | geçti |
| Coin yoğunlaşması < %60 | %37,5 (ETH) | geçti |
| **Gün yoğunlaşması < %60** | **%100,0** | **KALDI** |
| Rejim yoğunlaşması < %80 | %75,0 | geçti |
| Maks düşüş < net'in %25'i | $0,75 | geçti |

**Sekiz kapının altısı geçti; kalan ikisi, diğer altısının anlamlı olup olmadığına karar
veren ikisi.** Bütün işlemler tek takvim gününde oldu ve isabet **%93,8** (48'de 45).
Bu, gerçek bir kenarın ürettiği bir sayı değil; bir rallinin ürettiği bir sayıdır.

Strateji PAPER'da kalıyor. Rapor noktaları: **100, 200, 500 işlem.**

---

## 8. Olay (2026-09-12, çözüldü 15:47 UTC)

### 8.1 Bildirilen ve gerçek

"$5.000 paper sermaye değişikliğinden sonra bot durdu." **Bot durmamıştı** — worker
10 saattir ayakta, **0 yeniden başlatma**, 34.969 değerlendirme, kalp atışı güncel,
kill switch boş. Ve **$5.000'lık bir değişiklik hiç uygulanmamıştı**:
`STARTING_EQUITY=500`, `.env` 04:46 UTC'deki düzenlemeden beri dokunulmamış.
`l5000`, en baştan beri var olan üç karşılaştırma defterinden biri ($500 / $1.000 /
$5.000 aynı kararlarla paralel işler). Paper sermayeyi değiştirmek botu zaten
duduramaz.

Ama dışarıdan görülen gerçekti: **sıfır işlem, sıfır pozisyon, düz özkaynak** — durmuş
bir bota birebir benzer.

### 8.2 Kök neden: üç kusur, üçü de benim `UNIVERSE_RESTRICT_TIER0=false` değişikliğimden

| # | Kusur | Belirti | Düzeltme |
|---|---|---|---|
| 1 | COLD çiftler **değerlendiriliyor** ama **abone edilmiyordu** | `refresh_universe` her katmanı `self.universe`'e koyuyordu, `set_universe` yalnız hot+warm'a abone oluyordu → kitaplar 600.000 ms bayat → ABNORMAL rejim → anlamsız `ABNORMAL_VOLATILITY` reddi seli (scanner, huni ve tüm sayaçları kirletiyordu) | COLD artık izleme listesi: sıralanır, raporlanır, **asla değerlendirilmez**. Boş değerlendirilebilir küme ERROR seviyesinde loglanır |
| 2 | Kapasite kapısı hacim düşüşünde **listeden çıkarıyordu** | Hafta sonu durgunluğu SOLUSD'yi $10,46 → $6,54/dk indirince evrenden tamamen kayboldu — abonelikten çıktı, ölçülemez oldu, bir sonraki yenilemede geri geldi | Liste dışı bırakma **borsa minimumuna** ($1) göre; bizim $10 tabanımız ayrı bir `sizable` özelliği, tahsiste reddedilir, veri akmaya devam eder |
| 3 | USD/USDT tekilleştirmesi **salınıyordu** | SOLUSD 0,2982 · SOLUSDT 0,2941 → %1,4 fark; tik gürültüsü her yenilemede hangi bacağın abone olacağına karar veriyor, her çevirme sıcak kitabı düşürüp yerine bayatını koyuyordu (loglarda evren 4→5→4) | %5 ölü bant içindeki beraberlikler **ucuz gidiş-dönüşe** göre çözülür — hem kararlı hem doğru |

Tier-0 kısıtı tam olarak pahalı bacağı dışarıda tutmak için vardı; kapatmak kod
yorumlarının zaten belgelediği bir tuzağı yeniden açtı.

### 8.3 Düzeltme sonrası canlı kanıt (15:48 UTC)

| Ölçüm | Olaydan önce | Düzeltmeden sonra |
|---|---|---|
| Değerlendirilen sembol | 5 (2'si abone) | **3 (hepsi abone)** |
| HOT | ETHUSD, BTCUSD | **ETHUSD, BTCUSD, SOLUSD** |
| En eski kitap yaşı | 600.000 ms | **1.315 ms** |
| Bayat sembol | `['BTCUSD']` | **`[]`** |
| Rejimler | 4/5 ABNORMAL | **RANGING · HIGH_VOLATILITY** |
| Son değerlendirme | — | **0,6 sn önce** |
| Defterler | $500 / $1.000 / $5.000 | değişmedi |
| Veritabanındaki tarihsel işlem | 139 | **139 (sıfırlanmadı)** |

**Şu an neden yeni işlem açılmıyor:** atıl $500'ün tamamı `NO_QUALIFIED_SIGNAL $400` +
`CASH_RESERVE $100` olarak açıklanıyor; açıklanmamış tek dolar yok. Tarama saniyede
devam ediyor — nitelikli işlem sıfır olsa bile bot taramayı sürdürür.

---

## 9. 31 teslimat kalemi

| # | Kalem | Sonuç |
|---|---|---|
| 1 | BTC baskınlığı kök nedeni | BTC baskın değildi (%41,7); evren küçüktü çünkü borsanın %94,5'i durdurulmuş/işlemsiz |
| 2 | Düzeltilen hatalar | katman yolu bağlı değildi · kapasite ölçümü yoktu · 3 olay kusuru · huni monotonluğu · hibrit ihracı |
| 3 | HOT semboller | ETHUSD · BTCUSD · SOLUSD |
| 4 | WARM semboller | ADAUSD (`sizable=false`); COLD izleme: XRPUSDT · ZECUSDT · DOGEUSDT · HYPEUSD |
| 5 | Aktif puanlanan sembol | 3 değerlendirilen + 3 izleme (416 uygun, 629 listeli) |
| 6 | Sembol başına aday | BTCUSD 139 · SOLUSD 120 · XRPUSDT 8 · DOGEUSDT 3 |
| 7 | Sembol başına nitelenen | aynı sayılar — aday üretenlerin hepsi sıralandı |
| 8 | Sembol başına işlem | BTCUSD 58 · ETHUSD 51 · SOLUSD 30 |
| 9 | Sembol başına K/Z | BTCUSD −$3,80 · ETHUSD −$0,02 · SOLUSD +$2,53 |
| 10 | BTC işlem payı | **%41,7** — bayrak açılmıyor |
| 11 | Ort. eşzamanlı farklı varlık | 0,01 — kısıt politika değil, arz |
| 12 | Stratejiye göre tutma (medyan) | flow 24 sn · momentum 27 sn · imbalance 89 sn · breakout 53 sn |
| 13 | <1 dk işlemler | flow 42/48 · momentum 43/67 · imbalance 8/21 · breakout 3/3 |
| 14 | 10–25 dk işlemler | **sıfır** — her stratejide |
| 15 | 25–60 dk işlemler | **sıfır** — kontrol noktası ve zaman durdurucu hiç çalışmadı |
| 16 | Kapanış→yeniden tarama | kapanış çağrısının içinde; `payload.recycling`'de ölçülüyor |
| 17 | Kapanış→sonraki giriş | enstrümante edildi; canlı örneklem bekliyor |
| 18 | Atıl sermaye sebepleri | 10 kod, atıl bakiyeye tam toplanıyor, artık kova yok |
| 19 | flow_impulse işlem | 48 |
| 20 | flow_impulse net KF | 6,649 |
| 21 | flow_impulse beklenti | +$0,0884 / işlem |
| 22 | flow_impulse K/Z | +$4,24 — projedeki tek net pozitif satır |
| 23 | flow_impulse coin dağılımı | ETH 18 · SOL 15 · BTC 15 (alfa +9,21 / +18,47 / +8,64 bps) |
| 24 | TAKER | $4,2410 net — referans |
| 25 | MAKER | $0,17 kazanıldı; fırsat maliyeti sonrası **−$3,49** |
| 26 | HYBRID | $3,88–$4,52 — TAKER'ın tek standart hatası içinde |
| 27 | **Terfiye hazır mı?** | **HAYIR** — 48 işlem, tek takvim günü |
| 28 | Testler | 1.939 geçti, 1 atlandı |
| 29 | CPU / RAM | worker 0,8 CPU / 640 MB sınırı — değişmedi |
| 30 | PAPER | **EVET** |
| 31 | Gerçek emir | **0** |

---

## 10. Kendi hatalarım

Bu fazda sekiz kez kendi çalışmamı düzelttim. Hepsi **canlı veriye bakmaktan** çıktı,
yeşil test paketine güvenmekten değil — ve her seferinde kod doğruydu, *raporlama*
sessizce yanlıştı.

| # | Ne yanlıştı | Nasıl yakalandı |
|---|---|---|
| 1 | Canlı/backtest ayrımını "örneklem çelişkisi" sanıp uzun olanın tarafını tuttum | strateji bileşimini saydığımda ikisinin örtüşmediği çıktı |
| 2 | Beş rapor boyunca "maker icrası çözer" dedim | ham KF 0,791 — mid'de kaybeden stratejiden maliyet çıkarılamaz |
| 3 | Hibrit %0 geçiş oranı | dışa aktarmamda `expected_net_edge_bps` eksikti — bulgu değil, hatamdı |
| 4 | Huni, şampiyon modeli olmayan aşamayı darboğaz sanıyordu | canlı payload'da her sembolün darboğazı aynı çıktı |
| 5 | "Hiçbir şey tetiklenmedi" ile "hiç bakmadık" aynı kelimelerle yazılıyordu | canlı payload |
| 6 | Huni kendi monotonluk kuralını ihlal ediyordu | SOLUSD sıralandığında görünür oldu; boş ihlal listesi "hiçbir şey sıralanmadı" demekmiş |
| 7 | Huni anlık durumu gösteriyordu, sayım istenen yerde | worker'ın kendi sayaçları huninin attığı 18 reddi gösteriyordu |
| 8 | `best_net_edge_bps` 0,0 raporluyordu — hiçbir gözlemin görmediği bir değer | ilk canlı short örnekleminde |

Ayrıca bugünkü olayın **üç kusuru da** benim Faz 6 değişikliğimden geldi (bkz. §8).

Bunları saymamın sebebi alçakgönüllülük değil: **enstrümantasyonun kendi hatalarını
yakalayıp yakalamadığı, ürettiği negatif sonuçlara ne kadar güvenebileceğimizi
belirler.** Artık yakalıyor — bu yüzden negatif sonuçlara güveniyorum ve tek pozitif
sonuca hâlâ güvenmiyorum.

---

## 11. İddia etmeyeceğim şeyler

- **`flow_impulse` kanıtlanmış değil.** Tek gün, tek rejim, 48 işlem, %93,8 isabet.
  Projede ölçülmüş en iyi şey ve hâlâ bir örneklem.
- **100 ms hibrit satırı %6,6 iyileşme değil.** Gürültünün içinde.
- **Tier-0 kısıtını kaldırmanın yardım etmesi beklenmiyor.** Dört WARM çift bizim
  vadelerimizde kenar oranında 5–9 kat geride. İddia çürütülebilir kalsın diye
  taranıyorlar, şüphe duyulduğu için değil.
- **Buradaki hiçbir şey hesabın para kazanacağını söylemiyor.** Faz 3–5 kazanmayacağını
  söyledi; buna katılmayan tek strateji on iki saattir işlem yapıyor.

**Sıradaki adım giriş-modeli araştırması.** Faz 4'ün sınıflandırıcısı örneklem dışında
~5 puan isabet ekliyor (purge ve embargo zorunlu, kronolojik doğrulama) ve bu, projede
kör örneklemde yazı-turayı yenmiş ikinci şey. %67,5'e ulaştı, gereken %70,0'dı; aradaki
2,5 puan **ölçülmüş bir büyüklük üzerinde bir modelleme problemi**, borsa problemi değil.

**Hiçbir eşiği bir sonucu iyileştirmek için oynatmadım.** Kapılar tam olarak bunu
reddetmek için var.

---

## Ek A — Üreten araçlar

| Araç | Ne üretir |
|---|---|
| `api/scripts/pnl_audit.py` | RAW/NET ayrıştırması, işlem başına ihlal kontrolü, CASE A/B hükmü |
| `api/scripts/beta_check.py` | ALPHA = RAW − PİYASA, işlem başına, güven aralığıyla |
| `api/scripts/funnel_audit.py` | Tüm borsa için sembol başına huni sayımları |
| `api/scripts/universe_sensitivity.py` | Likidite barı / Tier-0 kısıtı duyarlılığı |
| `api/scripts/holding_buckets.py` | Strateji başına tutma kovası performansı |
| `api/scripts/flow_execution_study.py` | flow_impulse TAKER/MAKER/HYBRID, ms ölçeğinde |
| `api/scripts/run_execution_study.py` | Borsa geneli kotasyon ızgarası, markout, kuyruk modeli |
| `api/scripts/flow_gates.py` | Terfi kapıları — raporlar, asla terfi ettirmez |
| `api/scripts/liveconfig_backtest.py` | Dağıtılmış konfigürasyonla 120 günlük tekrar oynatma |

## Ek B — Faz dokümanları

`docs/` altında: `EXCHANGE_DECISION.md`, `ECONOMICS.md`, `RUNBOOK.md`,
`FINAL_REPORT.md`, `V2_SPEC.md`, `V2_REPORT.md`, `PHASE3_SPEC.md`, `PHASE3_REPORT.md`,
`PHASE4_SPEC.md`, `PHASE4_REPORT.md`, `PHASE5_SPEC.md`, `PHASE5_REPORT.md`,
`PNL_AUDIT.md`, `PHASE6_SPEC.md`, `PHASE6_REPORT.md`, `RAPOR_TAM.md` (bu dosya).
