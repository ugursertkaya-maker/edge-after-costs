import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import {
  CONCENTRATION_VERDICT_LABELS,
  CONCENTRATION_VERDICT_NOTES,
  DASH,
  baseAsset,
  concentrationTone,
  formatInt,
  formatMoney,
  formatPct,
  formatSignedMicroMoney,
  funnelStageLabel,
  toNum,
  tone,
} from "@/lib/format";
import {
  CONCENTRATION_MIN_SAMPLE,
  CONCENTRATION_THRESHOLD_PCT,
  applyFunnel,
  concentrationReading,
  concentrationRows,
  deriveConcentration,
} from "@/lib/phase6";
import type {
  ConcentrationRow,
  ConcentrationState,
  FunnelReport,
  TradeRow,
} from "@/lib/types";

/**
 * BTC YOĞUNLAŞMA İZLEYİCİSİ — §21.
 *
 * The brief opens with an accusation ("the trader is BTC-only") and this panel
 * exists to answer it with counts instead of argument. Two headline figures do
 * that: what share of the TRADES went to BTC, and what share of the CAPITAL
 * did. They routinely disagree — a coin can be 40% of the trade count and 60%
 * of the money — and the disagreement is itself the reading, so they are never
 * collapsed into one "BTC dominance" number.
 *
 * The flag is a DIAGNOSTIC and the panel says so in its own words. Nothing in
 * this codebase suppresses, penalises or re-ranks BTC because it fired; the
 * spec is explicit that if BTC wins on net EV, BTC trades. What the flag asks
 * for is the funnel: concentration in the trade count and concentration in
 * what is *allowed to be a candidate* are different faults with the same
 * symptom, and only the per-symbol funnel separates them.
 *
 * The resting state is designed as carefully as the firing one. Today BTC is
 * 41.7% of 139 trades, so the flag renders inactive and it has to look like a
 * finished measurement — a threshold marked on a real track with a real
 * sample beside it — rather than like a panel whose data never arrived.
 */

interface Headline {
  symbol: string | null;
  sharePct: number | null;
  sample: number | null;
}

/**
 * The threshold meter.
 *
 * The 70% line is drawn ON the track, not in a caption: a threshold that lives
 * in a legend is a threshold the eye never compares against. The distance
 * between the fill and the line is the whole reading, in both directions.
 */
function ShareMeter({
  label,
  symbol,
  pct,
  sample,
  sampleUnit,
  kind,
  note,
}: {
  label: string;
  symbol: string | null;
  pct: number | null;
  sample: number | null;
  sampleUnit: string;
  kind: string;
  note: string;
}) {
  const fill = pct === null ? 0 : Math.max(0, Math.min(100, pct));
  return (
    <div className={`cshare ${concentrationTone(kind)}`}>
      <div className="cshare-head">
        <span className="cshare-k">{label}</span>
        <span className={`cshare-v${pct === null ? " is-empty" : ""}`}>
          {pct === null ? DASH : formatPct(pct)}
        </span>
        <span className="cshare-sym">{symbol ? baseAsset(symbol) : DASH}</span>
      </div>
      <div
        className="cshare-track"
        role="img"
        aria-label={
          pct === null
            ? `${label} ölçülmedi`
            : `${label} ${formatPct(pct)}, eşik %${CONCENTRATION_THRESHOLD_PCT}`
        }
      >
        <span className={`cshare-fill${pct === null ? " is-unknown" : ""}`} style={{ width: `${fill}%` }} />
        <span
          className="cshare-limit"
          style={{ left: `${CONCENTRATION_THRESHOLD_PCT}%` }}
          title={`BTC_CONCENTRATION eşiği: %${CONCENTRATION_THRESHOLD_PCT}`}
        />
      </div>
      <div className="cshare-foot">
        <span className="cshare-n">
          {sample === null ? "örneklem bildirilmedi" : `${formatInt(sample)} ${sampleUnit}`}
        </span>
        <span className="cshare-lim">eşik %{CONCENTRATION_THRESHOLD_PCT}</span>
      </div>
      <p className="cshare-x">{note}</p>
    </div>
  );
}

/**
 * The flag, in whichever of its four states the data licenses.
 *
 * `clear` is not the absence of a reading, it is a reading: measured, over a
 * stated sample, below the line. It gets the same frame, the same eyebrow and
 * the same explanatory line as `flagged` — only the tone changes. A resting
 * diagnostic that looks like an empty box teaches the operator to distrust it
 * the day it fires.
 */
function Flag({
  verdict,
  sharePct,
  sample,
  symbol,
  cause,
  causeKnown,
}: {
  verdict: string;
  sharePct: number | null;
  sample: number | null;
  symbol: string | null;
  cause: string | null;
  causeKnown: boolean;
}) {
  const label = CONCENTRATION_VERDICT_LABELS[verdict] ?? verdict;
  const note = CONCENTRATION_VERDICT_NOTES[verdict] ?? "";

  return (
    <div className={`cflag ${concentrationTone(verdict)}`}>
      <div className="cflag-head">
        <span className="cflag-badge">{label}</span>
        <span className="cflag-read">
          {symbol ? baseAsset(symbol) : DASH} {sharePct === null ? DASH : formatPct(sharePct)}
          <span className="cflag-vs">
            {" "}
            / eşik %{CONCENTRATION_THRESHOLD_PCT} ·{" "}
            {sample === null ? "örneklem yok" : `${formatInt(sample)} işlem`}
          </span>
        </span>
      </div>
      <p className="cflag-x">{note}</p>

      <div className="cflag-cause">
        <span className="cflag-cause-k">Sebep</span>
        {causeKnown ? (
          <span className="cflag-cause-v">{cause}</span>
        ) : (
          <span className="cflag-cause-v is-empty">
            {DASH} — sebep verisi bu ekrana ulaşmıyor. Yoğunlaşmanın sebebi sıralamada mı
            yoksa neyin aday olmasına izin verildiğinde mi olduğunu yalnızca sembol bazlı
            huni söyleyebilir; o gelene kadar burada bir sebep uydurulmaz.
          </span>
        )}
      </div>

      <p className="cflag-guard">
        Bu bayrak yalnızca teşhistir. BTC hiçbir yerde bastırılmaz, cezalandırılmaz veya
        yeniden sıralanmaz; net EV'de BTC kazanıyorsa BTC işlem görür. Bayrak bir
        kısıt değil, huniye bakma çağrısıdır.
      </p>
    </div>
  );
}

function CoinTable({ rows, hasFunnel }: { rows: ConcentrationRow[]; hasFunnel: boolean }) {
  return (
    <div className="tbl-wrap">
      <table className="tbl">
        <thead>
          <tr>
            <th className="l">Coin</th>
            <th>İşlem</th>
            <th title="İşlem sayısının payı">İşlem payı</th>
            <th title="Maliyet sonrası net P&L">Net P&amp;L</th>
            <th title="Kapanmış işlemlerin nominal toplamı — anlık dağıtılmış sermaye değil">
              Tahsis edilen sermaye
            </th>
            <th title="Tahsis edilen sermayenin payı">Sermaye payı</th>
            <th title="Huni: strategy_evaluated">Sinyal</th>
            <th title="Huni: ranked — tüm kapıları geçen sinyal">Nitelikli</th>
            <th title="Sinyal − nitelikli">Reddedilen</th>
            <th className="l" title="Bu sembolün öldüğü aşama ve orada en sık görülen gerekçe">
              Darboğaz
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const trades = toNum(r.trades);
            return (
              <tr key={r.symbol}>
                <td className="l sym" title={r.symbol}>
                  {baseAsset(r.symbol)}
                </td>
                <td className={trades ? "" : "dim"}>{formatInt(trades)}</td>
                <td className={toNum(r.trade_share_pct) === null ? "muted" : ""}>
                  {toNum(r.trade_share_pct) === null ? DASH : formatPct(r.trade_share_pct ?? null)}
                </td>
                <td className={`net ${tone(r.net_pnl_usd ?? null) ?? "muted"}`}>
                  {toNum(r.net_pnl_usd) === null
                    ? DASH
                    : formatSignedMicroMoney(r.net_pnl_usd ?? null)}
                </td>
                <td className={toNum(r.capital_allocated_usd) === null ? "muted" : "dim"}>
                  {formatMoney(r.capital_allocated_usd ?? null)}
                </td>
                <td className={toNum(r.capital_share_pct) === null ? "muted" : ""}>
                  {toNum(r.capital_share_pct) === null
                    ? DASH
                    : formatPct(r.capital_share_pct ?? null)}
                </td>
                <td className={toNum(r.signals) === null ? "muted" : "dim"}>
                  {formatInt(r.signals ?? null)}
                </td>
                <td className={toNum(r.qualified_signals) === null ? "muted" : ""}>
                  {formatInt(r.qualified_signals ?? null)}
                </td>
                <td className={toNum(r.rejected_signals) === null ? "muted" : "dim"}>
                  {formatInt(r.rejected_signals ?? null)}
                </td>
                <td className="l dim" title={r.bottleneck_reason ?? undefined}>
                  {r.bottleneck_stage ? (
                    <>
                      {funnelStageLabel(r.bottleneck_stage)}
                      {r.bottleneck_reason ? (
                        <span className="cbn"> · {r.bottleneck_reason}</span>
                      ) : null}
                    </>
                  ) : (
                    <span className="muted">{hasFunnel ? DASH : "huni yok"}</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function Concentration({
  concentration,
  funnel,
  trades,
  loading,
}: {
  concentration: ConcentrationState | null | undefined;
  funnel: FunnelReport | null | undefined;
  trades: TradeRow[] | null;
  loading: boolean;
}) {
  const served = concentration ?? null;
  const { rows: baseRows, derived, sample } = concentrationRows(served, trades);
  const rows = applyFunnel(baseRows, funnel?.symbols ?? null).sort(
    (a, b) => (toNum(b.trades) ?? 0) - (toNum(a.trades) ?? 0),
  );

  // The capital denominator only exists when at least one trade reported its
  // notional, so it is read from the derivation rather than assumed.
  const derivation = deriveConcentration(trades);

  const top = rows[0] ?? null;
  const tradeHead: Headline = {
    symbol: served?.top_symbol ?? top?.symbol ?? null,
    sharePct: toNum(served?.top_trade_share_pct) ?? toNum(top?.trade_share_pct ?? null),
    sample: sample ?? null,
  };
  const capTop = [...rows].sort(
    (a, b) => (toNum(b.capital_share_pct) ?? -1) - (toNum(a.capital_share_pct) ?? -1),
  )[0];
  // The capital share has its own denominator and it is smaller than the trade
  // one whenever a round trip did not report its size. Reporting the trade
  // sample beside a capital percentage would overstate what was counted.
  const capSample = derived
    ? derivation.notionalTotal === null
      ? null
      : derivation.tradeSample - derivation.notionalMissing
    : toNum(served?.sample_notional_usd) === null
      ? null
      : toNum(served?.sample_trades);
  const capHead: Headline = {
    symbol: served?.top_symbol ?? capTop?.symbol ?? null,
    sharePct: toNum(served?.top_capital_share_pct) ?? toNum(capTop?.capital_share_pct ?? null),
    sample: capSample,
  };

  const reading = concentrationReading(tradeHead.sharePct, tradeHead.sample);
  const verdict = served?.flag === true ? "flagged" : reading.verdict;

  const cause = served?.cause ?? causeFromFunnel(funnel);
  const meta = (
    <>
      <span
        className="pill is-off"
        title="Payların hesaplandığı kapanmış işlem sayısı — her yüzdenin denominatörü"
      >
        {sample === null ? "örneklem yok" : `${formatInt(sample)} işlem`}
      </span>
      <span
        className={`pill ${verdict === "flagged" ? "is-warn" : "is-off"}`}
        title={CONCENTRATION_VERDICT_NOTES[verdict] ?? ""}
      >
        {verdict === "flagged" ? "BAYRAK ETKİN" : "BAYRAK ETKİN DEĞİL"}
      </span>
    </>
  );

  return (
    <Panel
      idx="18"
      title="BTC yoğunlaşma izleyicisi"
      note="coin başına işlem, sermaye ve huni sayımları"
      meta={meta}
      footer={
        "TAHSİS EDİLEN SERMAYE, kapanmış işlemlerin nominal toplamıdır: on kez devreden 50 dolarlık bir pozisyon bu sütuna 500 dolar yazar. Bu, §09'daki anlık DAĞITILMIŞ SERMAYE ile aynı büyüklük değildir ve iki sütun asla toplanmaz. " +
        (derived
          ? "İşlem ve sermaye payları kapanmış işlem listesinden TÜRETİLMİŞTİR (API son 200 turu yayınlar), sinyal/nitelikli/reddedilen sütunları ise yalnızca sembol bazlı huniden gelir. "
          : "") +
        `Bayrak eşiği %${CONCENTRATION_THRESHOLD_PCT}, konuşmaya başlaması için gereken en küçük örneklem ${CONCENTRATION_MIN_SAMPLE} işlemdir; az sayıda işlemde yüksek pay, yoğunlaşma değil az sayıda işlemdir. Bayrak hiçbir coini bastırmaz.`
      }
    >
      {loading && rows.length === 0 ? (
        <Skeleton rows={5} />
      ) : rows.length === 0 ? (
        <DataState
          title="Coin bazlı sayım yok"
          text="Ne kapanmış işlem ne de sembol bazlı huni bu ekrana ulaştı. Yoğunlaşma ölçülmedi — ve ölçülmemiş bir yoğunlaşma, olmayan bir yoğunlaşma değildir."
        />
      ) : (
        <div className="conc">
          <div className="conc-heads">
            <ShareMeter
              label="BTC işlem payı"
              symbol={tradeHead.symbol}
              pct={tradeHead.sharePct}
              sample={tradeHead.sample}
              sampleUnit="kapanmış işlem"
              kind={verdict}
              note="Kaç turun bu coine gittiği. Sıralamanın çıktısıdır; kota değildir."
            />
            <ShareMeter
              label="BTC sermaye payı"
              symbol={capHead.symbol}
              pct={capHead.sharePct}
              sample={capHead.sample}
              sampleUnit="nominali bildirilmiş işlem"
              kind={capHead.sharePct !== null && capHead.sharePct > CONCENTRATION_THRESHOLD_PCT ? "flagged" : verdict}
              note="Paranın ne kadarının bu coinden geçtiği. İşlem payından farklı olması bir kusur değil, ayrı bir okumadır: az sayıda büyük tur, çok sayıda küçük turdan farklı bir maruziyettir."
            />
          </div>

          <Flag
            verdict={verdict}
            sharePct={tradeHead.sharePct}
            sample={tradeHead.sample}
            symbol={tradeHead.symbol}
            cause={cause}
            causeKnown={cause !== null}
          />

          {derivation.notionalMissing > 0 && (
            <p className="xnote">
              {formatInt(derivation.notionalMissing)} işlem nominal büyüklüğünü bildirmedi;
              sermaye payı yalnızca bildirenler üzerinden hesaplanmıştır. Bildirilmemiş bir
              nominal sıfır dolar sayılmaz, denominatörden çıkarılır.
            </p>
          )}

          <CoinTable rows={rows} hasFunnel={!!funnel?.symbols} />
        </div>
      )}
    </Panel>
  );
}

/**
 * The cause, read off the funnel's own wall counts.
 *
 * The wording is assembled from counts, not composed: "251 sembol
 * `discovered` aşamasında duruyor" is the funnel speaking. When no funnel is
 * served there is no cause, and the flag prints that instead of a theory.
 */
function causeFromFunnel(funnel: FunnelReport | null | undefined): string | null {
  const wall = funnel?.wall ?? null;
  if (!wall) return null;
  const entries = Object.entries(wall)
    .map(([stage, n]) => [stage, toNum(n)] as const)
    .filter((e): e is readonly [string, number] => e[1] !== null && e[1] > 0)
    .sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) return null;
  const [stage, n] = entries[0];
  const total = entries.reduce((s, [, v]) => s + v, 0);
  const reason = topReasonAt(funnel, stage);
  return (
    `${formatInt(n)} / ${formatInt(total)} sembol "${funnelStageLabel(stage)}" aşamasında duruyor` +
    (reason ? ` — orada en sık kaydedilen gerekçe: ${reason}.` : ".")
  );
}

function topReasonAt(funnel: FunnelReport | null | undefined, stage: string): string | null {
  const tally: Record<string, number> = {};
  for (const f of Object.values(funnel?.symbols ?? {})) {
    const bucket = f.drop_reasons?.[stage];
    if (!bucket) continue;
    for (const [reason, n] of Object.entries(bucket)) {
      const v = toNum(n);
      if (v === null) continue;
      tally[reason] = (tally[reason] ?? 0) + v;
    }
  }
  const top = Object.entries(tally).sort((a, b) => b[1] - a[1])[0];
  return top ? top[0] : null;
}
