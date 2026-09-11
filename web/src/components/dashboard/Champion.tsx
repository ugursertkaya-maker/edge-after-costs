import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import { Stat } from "@/components/dashboard/bits";
import {
  DASH,
  formatDateTimeMs,
  formatDuration,
  formatFactor,
  formatInt,
  formatMoney,
  formatRelativeMs,
  formatSignedMicroMoney,
  strategyLabel,
  toNum,
  tone,
} from "@/lib/format";
import type { ChampionState } from "@/lib/types";

/**
 * How far apart two profit factors have to be before the gap is worth a word.
 * Below this the two measurements are saying the same thing with different
 * samples; above it they are disagreeing, and the disagreement is information.
 */
const PF_GAP = 0.1;

/**
 * The out-of-sample and live-paper profit factors, side by side.
 *
 * They are not two versions of one number and the panel never averages, picks
 * or reconciles them. The out-of-sample figure is what frozen parameters did
 * on recorded history; the live figure is what the same strategy does against
 * a real book, real latency and real queue position. They will disagree, and
 * the size of the disagreement is the only honest read on how much of the
 * backtest was an artefact of the backtest.
 */
function ProfitFactorGap({ oos, live }: { oos: number | null; live: number | null }) {
  const gap = oos !== null && live !== null ? live - oos : null;
  const gapCls =
    gap === null ? "is-empty" : gap < -PF_GAP ? "neg" : gap > PF_GAP ? "pos" : "flat";

  const verdict =
    gap === null
      ? "İki ölçüm birden gelmeden fark okunamaz. Tek bir profit factor, hangi koşulda ölçüldüğü söylenmeden bir şey ifade etmez."
      : gap < -PF_GAP
        ? "Canlı kâğıt, araştırmanın gerisinde. Aradaki fark, backtest'in modellemediği bir maliyetin — kuyruk sırası, gecikme, gerçek makas — bedelidir ve araştırma sonucundan düşülmesi gerekir."
        : gap > PF_GAP
          ? "Canlı kâğıt, araştırmanın önünde. Bu iyi haber değil, bir uyarıdır: örneklem küçükken bu fark çoğunlukla şanstır ve iki ölçümün yakınsaması beklenir."
          : "İki ölçüm birbirine yakın. Bu, stratejinin gerçek akışta da araştırmadaki gibi davrandığını gösteren tek doğrudan kanıttır.";

  return (
    <div className="vs">
      <div className="vs-cell">
        <span className="vs-k">Örneklem dışı PF</span>
        <span className={`vs-v ${oos === null ? "is-empty" : ""}`}>
          {formatFactor(oos)}
        </span>
        <span className="vs-n">donmuş parametreler · kayıtlı geçmiş</span>
      </div>

      <div className="vs-gap">
        <span className="vs-op" aria-hidden>
          ↔
        </span>
        <span className={`vs-d ${gapCls}`}>
          {gap === null
            ? DASH
            : `${gap > 0 ? "+" : ""}${gap.toLocaleString("en-US", {
                minimumFractionDigits: 2,
                maximumFractionDigits: 2,
              })}`}
        </span>
        <span className="vs-dk">fark</span>
      </div>

      <div className="vs-cell is-live">
        <span className="vs-k">Canlı kâğıt PF</span>
        <span className={`vs-v ${live === null ? "is-empty" : ""}`}>
          {formatFactor(live)}
        </span>
        <span className="vs-n">gerçek akış · gerçek gecikme</span>
      </div>

      <p className="vs-note">{verdict}</p>
    </div>
  );
}

/**
 * What is actually running in live paper.
 *
 * `null` is a legitimate and, given the gates, a likely answer — so it is said
 * in a sentence rather than left as an empty panel. Nothing here enables live
 * trading: this is the paper ledger's own champion.
 */
export function Champion({
  champion,
  loading,
  now,
}: {
  champion: ChampionState | null | undefined;
  loading: boolean;
  now: number;
}) {
  const c = champion ?? null;

  const oos = toNum(c?.oos_profit_factor);
  const live = toNum(c?.live_profit_factor);

  const meta = c ? (
    <>
      {c.version && (
        <span className="pill" title="Şampiyonun sürümü — terfi edilen strateji sürümlenir">
          {c.version}
        </span>
      )}
      {toNum(c.started_ms) !== null && (
        <span className="panel-note" title={formatDateTimeMs(c.started_ms)}>
          {formatRelativeMs(c.started_ms, now)} başladı
        </span>
      )}
    </>
  ) : null;

  return (
    <Panel
      idx="12"
      title="Mevcut paper şampiyonu"
      note="canlı kâğıt motorunda çalışan strateji"
      meta={meta}
      footer="Şampiyon kâğıt üzerinde çalışır; bu panelde gerçek emir yoktur. Örneklem dışı PF araştırmanın, canlı kâğıt PF ise gerçek akışın ölçümüdür — ikisinin ayrışması beklenen bir durumdur ve yan yana durmalarının sebebi de budur."
    >
      {loading && !c ? (
        <Skeleton rows={3} />
      ) : !c ? (
        <DataState
          title="Kâğıt şampiyonu yok"
          text="Canlı kâğıt motorunda araştırmadan terfi etmiş bir strateji çalışmıyor. Kapıların tamamını geçen bir aday olmadan terfi yapılmaz; bir strateji terfi ettiğinde sürümü, başlangıç anı ve her iki profit factor'ü burada görünecek."
        />
      ) : (
        <div className="champ">
          <div className="champ-id">
            <span className="champ-k">Strateji</span>
            <span className="champ-v">
              {c.strategy ? strategyLabel(c.strategy) : DASH}
            </span>
            <span className="champ-n">
              {toNum(c.started_ms) !== null
                ? `${formatDateTimeMs(c.started_ms)} itibarıyla · ${formatDuration(
                    Math.max(0, (now - (toNum(c.started_ms) as number)) / 1000),
                  )} çalışıyor`
                : "başlangıç anı bildirilmedi"}
            </span>
          </div>

          <ProfitFactorGap oos={oos} live={live} />

          <div className="pfolio-grid">
            <Stat
              label="Kâğıt sermaye"
              value={formatMoney(c.paper_equity)}
              note="şampiyonun defteri"
            />
            <Stat
              label="Expectancy"
              value={formatSignedMicroMoney(c.expectancy)}
              cls={tone(c.expectancy) ?? ""}
              note="işlem başına NET"
            />
            <Stat
              label="Max drawdown"
              value={
                toNum(c.max_drawdown) === null
                  ? DASH
                  : formatMoney(-Math.abs(toNum(c.max_drawdown) as number))
              }
              cls={toNum(c.max_drawdown) ? "neg" : ""}
              note="tepe noktasından"
            />
            <Stat label="İşlem" value={formatInt(c.trades)} note="kapanmış tur" />
            <Stat
              label="Sürüm"
              value={c.version ?? DASH}
              note="terfi edilen strateji sürümü"
            />
          </div>
        </div>
      )}
    </Panel>
  );
}
