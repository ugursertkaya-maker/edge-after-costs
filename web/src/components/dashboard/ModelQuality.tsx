import { Panel } from "@/components/ui/Panel";
import { Skeleton } from "@/components/ui/DataState";
import { Stat } from "@/components/dashboard/bits";
import {
  CALIBRATION_MAX_ABS_ERROR,
  DASH,
  calibrationMethodLabel,
  calibrationReading,
  calibrationStatusLabel,
  confidenceTone,
  formatDateTimeMs,
  formatDays,
  formatFactor,
  formatInt,
  formatMoney,
  formatNumber,
  formatPeriod,
  formatProbability,
  formatRate,
  formatScore,
  formatSignedMicroMoney,
  periodDays,
  toNum,
  tone,
} from "@/lib/format";
import type { PeriodMs, PredictiveState } from "@/lib/types";

/**
 * MODEL KALİTESİ — item 32.
 *
 * The three split periods are drawn as one timeline rather than as three
 * rows of dates, because the ordering is the claim being made. "Test comes
 * last and was touched once" is the difference between a measurement and a
 * fitted number, and a reader should not have to compare six dates to check
 * it — the shape on screen either shows train → validate → test left to
 * right, or it shows that the payload does not.
 */

interface Leg {
  key: "train" | "validate" | "test";
  label: string;
  note: string;
  period: PeriodMs | null | undefined;
}

/**
 * Widths are proportional to each leg's real span, with a floor so a short
 * test window is still legible. The floor is the only cosmetic decision here
 * and it only ever makes a small window bigger, never a large one smaller.
 */
const MIN_SHARE = 0.14;

function Timeline({ legs }: { legs: Leg[] }) {
  const spans = legs.map((l) => periodDays(l.period));
  const known = spans.filter((s): s is number => s !== null);
  const total = known.reduce((a, b) => a + b, 0);

  // Chronology is a claim the payload either supports or does not. Reading the
  // ends of each leg is the only way to know, so it is read rather than
  // assumed — a test window that starts before validation ends is a leak, and
  // it is the single most important thing this panel could catch.
  const ends = legs.map((l) => (l.period ? toNum(l.period[1]) : null));
  const starts = legs.map((l) => (l.period ? toNum(l.period[0]) : null));
  const ordered =
    ends[0] !== null && starts[1] !== null && ends[1] !== null && starts[2] !== null
      ? starts[1] >= ends[0] && starts[2] >= ends[1]
      : null;

  return (
    <div className="tline">
      <div className="tline-bar">
        {legs.map((l, i) => {
          const span = spans[i];
          const share =
            span === null || total <= 0
              ? 1 / legs.length
              : Math.max(MIN_SHARE, span / total);
          return (
            <div
              key={l.key}
              className={`tline-seg is-${l.key}${l.period ? "" : " is-empty"}`}
              style={{ flexGrow: share }}
              title={`${l.label} · ${formatPeriod(l.period)}`}
            >
              <span className="tline-k">{l.label}</span>
              <span className="tline-v">{formatPeriod(l.period)}</span>
              <span className="tline-n">
                {span === null ? "süre bildirilmedi" : formatDays(span)}
              </span>
            </div>
          );
        })}
      </div>

      <div className="tline-legend">
        {legs.map((l) => (
          <span key={l.key} className={`tline-dot is-${l.key}`}>
            <i aria-hidden />
            {l.note}
          </span>
        ))}
      </div>

      {ordered === false && (
        <p className="tline-warn">
          Bildirilen pencereler kronolojik değil: doğrulama veya test penceresi
          kendinden öncekinin bitişinden daha erken başlıyor. Etiketler ufka kadar
          ileriye baktığı için bu, sızıntı anlamına gelir ve buradaki örneklem dışı
          rakamlar bu hâliyle bir ölçüm sayılmaz.
        </p>
      )}
      {ordered === null && (
        <p className="tline-note">
          Üç pencerenin tamamı bildirilmeden sıralama doğrulanamaz. Zaman çizgisi
          yalnızca gelen pencereleri çizer; gelmeyeni tahmin etmez.
        </p>
      )}
      {ordered === true && (
        <p className="tline-note">
          Pencereler kronolojik: eğitim, doğrulama ve test bu sırayla ve örtüşmeden
          ilerliyor. Test penceresine bir kez dokunulur — parametreler ona
          bakılmadan dondurulur.
        </p>
      )}
    </div>
  );
}

export function ModelQuality({
  predictive,
  loading,
}: {
  predictive: PredictiveState | null | undefined;
  loading: boolean;
}) {
  const p = predictive ?? null;
  const cal = calibrationReading(p?.calibration);
  const c = p?.calibration ?? null;
  const oos = p?.oos ?? null;

  const err = toNum(c?.max_abs_error);
  const legs: Leg[] = [
    {
      key: "train",
      label: "EĞİTİM",
      note: "eğitim — model bu veriyi gördü ve ona uydu",
      period: p?.train_period,
    },
    {
      key: "validate",
      label: "DOĞRULAMA",
      note: "doğrulama — model ve eşikler burada seçildi",
      period: p?.validate_period,
    },
    {
      key: "test",
      label: "TEST",
      note: "test — en sonda, bir kez dokunuldu",
      period: p?.test_period,
    },
  ];

  const meta = p ? (
    <>
      {p.model_version && <span className="pill">{p.model_version}</span>}
      <span className={`pill ${cal.usable ? "is-pos" : "is-warn"}`} title={cal.note}>
        {c?.status ? calibrationStatusLabel(c.status) : cal.text}
      </span>
      {toNum(p.trained_to_ms) !== null && (
        <span className="panel-note" title="Modelin görmesine izin verilen son bar">
          {formatDateTimeMs(p.trained_to_ms)} itibarıyla eğitildi
        </span>
      )}
    </>
  ) : null;

  return (
    <Panel
      idx="14"
      title="Model kalitesi"
      note="eğitim/doğrulama/test pencereleri, kalibrasyon ve örneklem dışı karne"
      meta={meta}
      footer="Kalibrasyon bir ayrıntı değil, bu panelin var oluş sebebidir: %70 diyen bir model işlemlerin yaklaşık %70'ini kazanmıyorsa çıktısı bir olasılık değil, bir sayıdır ve üzerine kurulan beklenen değer hesabı da öyledir. Örneklem dışı sütunlar donmuş parametrelerin hiç görmediği veriden gelir; örneklem içi performans hiçbir şeyi terfi ettirmez."
    >
      {loading && !p ? (
        <Skeleton rows={4} />
      ) : !p ? (
        <div className="pred-empty">
          <div className="fin-empty-head">
            <span className="state-mark" aria-hidden />
            <p className="fin-empty-t">Ölçülecek bir model yok</p>
          </div>
          <p className="fin-empty-x">
            Eğitilmiş ve dondurulmuş bir çıkarım paketi bildirilmedi, dolayısıyla
            ölçülecek bir Brier skoru, kalibrasyon eğrisi veya örneklem dışı karne de
            yok. Bu bir arıza değil, bu fazın çıkış noktasıdır: Phase 3 sıfır
            şampiyonla kapandı. Bir model eğitildiğinde üç pencere — eğitim, doğrulama
            ve test — burada zaman çizgisi olarak, kalibrasyonu ise geçtiği ya da
            geçemediği sınırla birlikte görünecek. Ölçülmemiş bir kalibrasyon, bu
            ekranda hiçbir zaman geçmiş bir kalibrasyon gibi gösterilmez.
          </p>
        </div>
      ) : (
        <div className="mq">
          <Timeline legs={legs} />

          <div className="mq-cols">
            <div className="fgroup">
              <p className="fgroup-t">
                Kimlik <span className="fgroup-n">donmuş çıkarım paketi</span>
              </p>
              <dl className="cgrid">
                <div className="citem">
                  <dt>Model sürümü</dt>
                  <dd className={p.model_version ? "" : "muted"}>
                    {p.model_version ?? DASH}
                  </dd>
                </div>
                <div className="citem">
                  <dt>Model</dt>
                  <dd className={p.model_name ? "" : "muted"}>{p.model_name ?? DASH}</dd>
                </div>
                <div className="citem">
                  <dt>Öznitelik sürümü</dt>
                  <dd className={p.feature_version ? "dim" : "muted"}>
                    {p.feature_version ?? DASH}
                  </dd>
                </div>
                <div className="citem">
                  <dt>Etiket anahtarı</dt>
                  <dd className={p.spec_key ? "dim" : "muted"} title={p.spec_key ?? undefined}>
                    {p.spec_key ?? DASH}
                  </dd>
                </div>
                <div className="citem">
                  <dt>Eğitim sonu</dt>
                  <dd className={toNum(p.trained_to_ms) === null ? "muted" : "dim"}>
                    {formatDateTimeMs(p.trained_to_ms)}
                  </dd>
                </div>
                <div className="citem">
                  <dt>Olasılık eşiği</dt>
                  <dd className={toNum(p.probability_threshold) === null ? "muted" : ""}>
                    {formatProbability(p.probability_threshold)}
                  </dd>
                </div>
                <div className="citem">
                  <dt>EV eşiği</dt>
                  <dd className={toNum(p.ev_threshold_usd) === null ? "muted" : ""}>
                    {toNum(p.ev_threshold_usd) === null
                      ? DASH
                      : formatMoney(p.ev_threshold_usd, 4)}
                  </dd>
                </div>
              </dl>
            </div>

            <div className="fgroup">
              <p className="fgroup-t">
                Kalibrasyon{" "}
                <span className="fgroup-n">
                  {c?.method ? calibrationMethodLabel(c.method) : "yöntem bildirilmedi"}
                </span>
              </p>

              <div className={`calverdict ${confidenceTone(cal.kind)}`}>
                <span className="calverdict-v">
                  {c?.status ? calibrationStatusLabel(c.status) : cal.text}
                </span>
                <span className="calverdict-x">{cal.note}</span>
              </div>

              <dl className="cgrid">
                <div className="citem">
                  <dt>Brier</dt>
                  <dd
                    className={toNum(c?.brier) === null ? "muted" : ""}
                    title="Olasılık tahmininin karesel hatası — düşük iyidir, 0,25 yazı tura demektir"
                  >
                    {formatScore(c?.brier)}
                  </dd>
                </div>
                <div className="citem">
                  <dt>Log loss</dt>
                  <dd className={toNum(c?.log_loss) === null ? "muted" : "dim"}>
                    {formatScore(c?.log_loss)}
                  </dd>
                </div>
                <div className="citem">
                  <dt>AUC</dt>
                  <dd className={toNum(c?.auc) === null ? "muted" : "dim"}>
                    {formatNumber(c?.auc, 3)}
                  </dd>
                </div>
                <div className="citem">
                  <dt>PR-AUC</dt>
                  <dd className={toNum(c?.pr_auc) === null ? "muted" : "dim"}>
                    {formatNumber(c?.pr_auc, 3)}
                  </dd>
                </div>
                <div className="citem is-wide">
                  <dt>Azami güvenilirlik hatası</dt>
                  <dd
                    className={
                      err === null ? "muted" : err > CALIBRATION_MAX_ABS_ERROR ? "neg" : "pos"
                    }
                    title={`Terfi kapısı: ≤ ${CALIBRATION_MAX_ABS_ERROR}`}
                  >
                    {err === null
                      ? DASH
                      : `${formatRate(err, 1)} / sınır ${formatRate(
                          CALIBRATION_MAX_ABS_ERROR,
                          0,
                        )}`}
                  </dd>
                </div>
              </dl>
            </div>
          </div>

          <div className="fgroup">
            <p className="fgroup-t">
              Örneklem dışı karne{" "}
              <span className="fgroup-n">
                donmuş parametreler · test penceresi · karar bu satırda
              </span>
            </p>
            {!oos ? (
              <p className="xnote">
                Örneklem dışı karne bildirilmedi. Karnesiz bir model terfi edemez; bu
                satırların boş olması, sıfır sonuç değil, ölçülmemiş sonuç demektir.
              </p>
            ) : (
              <div className="pfolio-grid">
                <Stat label="İşlem" value={formatInt(oos.trades)} note="örneklem dışı tur" />
                <Stat
                  label="Kazanma oranı"
                  value={formatRate(oos.win_rate)}
                  note="tek başına bir sonuç değil"
                />
                <Stat
                  label="Profit factor"
                  value={formatFactor(oos.profit_factor)}
                  cls={
                    toNum(oos.profit_factor) === null
                      ? ""
                      : (toNum(oos.profit_factor) as number) >= 1.2
                        ? "pos"
                        : (toNum(oos.profit_factor) as number) >= 1
                          ? "warn"
                          : "neg"
                  }
                  note="terfi barajı 1,20"
                />
                <Stat
                  label="Expectancy"
                  value={formatSignedMicroMoney(oos.expectancy)}
                  cls={tone(oos.expectancy) ?? ""}
                  note="işlem başına NET"
                />
                <Stat
                  label="NET P&L"
                  value={formatSignedMicroMoney(oos.net_pnl)}
                  cls={tone(oos.net_pnl) ?? ""}
                  note="maliyet sonrası"
                />
                <Stat
                  label="Max drawdown"
                  value={
                    toNum(oos.max_drawdown) === null
                      ? DASH
                      : formatMoney(-Math.abs(toNum(oos.max_drawdown) as number))
                  }
                  cls={toNum(oos.max_drawdown) ? "neg" : ""}
                  note="tepe noktasından"
                />
              </div>
            )}
          </div>
        </div>
      )}
    </Panel>
  );
}
