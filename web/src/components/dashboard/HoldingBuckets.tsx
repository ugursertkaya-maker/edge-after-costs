import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import {
  DASH,
  formatDuration,
  formatInt,
  formatRate,
  formatSignedMicroMoney,
  strategyLabel,
  toNum,
  tone,
} from "@/lib/format";
import {
  HOLDING_BUCKETS,
  deriveHoldingBuckets,
  medianHoldByStrategy,
  normalizeBucketKey,
} from "@/lib/phase6";
import type { BucketCell, BucketGrid } from "@/lib/phase6";
import type { HoldingBucketState, TradeRow } from "@/lib/types";

/**
 * TUTMA SÜRESİ KOVALARI — §20.
 *
 * Net performance by holding bucket, one row per STRATEGY, never pooled.
 *
 * Pooling is the failure mode this panel exists to prevent. `flow_impulse` has
 * a ~32 s median hold and `momentum_scalp` a ~71 s one; a single "30–60 sn"
 * row would average an intentionally fast strategy against the tail of a slow
 * one and call the average a result. Worse, it would make a strategy-specific
 * holding policy look wrong: a 30-second `flow_impulse` trade is CORRECT when
 * the impulse is spent, and only a per-strategy row can say so.
 *
 * The second rule is the empty cell. A strategy that has never held for 2–5
 * minutes renders BLANK, not "$0.00". Zero net P&L over four trades and no
 * trades at all are different statements, and a grid that draws them the same
 * way invents a flat performance reading out of an absence of data.
 */

/** Below this a cell is shown but not read as a performance figure. */
const THIN_SAMPLE = 5;

function cellOf(grid: BucketGrid, strategy: string, bucket: string): BucketCell | undefined {
  return grid.cells[strategy]?.[bucket];
}

/**
 * Build the grid from the worker's rows when it serves them.
 *
 * Backend rows win over derivation, but they are folded into the same shape so
 * the table has exactly one rendering path — two code paths for the same grid
 * is how the served and derived versions drift into disagreeing.
 */
function gridFromRows(state: HoldingBucketState): BucketGrid {
  const grid: BucketGrid = { strategies: [], cells: {}, totals: {}, sample: 0, undated: 0 };
  for (const row of state.rows ?? []) {
    const strategy = row.strategy ?? "—";
    const bucket = normalizeBucketKey(row.bucket);
    const trades = toNum(row.trades);
    if (!bucket || trades === null) continue;
    const net = toNum(row.net_pnl_usd);
    const winRate = toNum(row.win_rate);
    const cell: BucketCell = {
      trades,
      net: net ?? 0,
      gross: toNum(row.gross_pnl_usd),
      cost: toNum(row.total_cost_usd),
      wins: winRate === null ? 0 : Math.round(winRate * trades),
      unmeasured: net === null ? trades : 0,
    };
    (grid.cells[strategy] ??= {})[bucket] = cell;

    const total = (grid.totals[strategy] ??= {
      trades: 0, net: 0, gross: null, cost: null, wins: 0, unmeasured: 0,
    });
    total.trades += cell.trades;
    total.net += cell.net;
    total.wins += cell.wins;
    total.unmeasured += cell.unmeasured;
    if (cell.gross !== null) total.gross = (total.gross ?? 0) + cell.gross;
    if (cell.cost !== null) total.cost = (total.cost ?? 0) + cell.cost;
    grid.sample += cell.trades;
  }
  grid.strategies = Object.keys(grid.cells).sort(
    (a, b) => (grid.totals[b]?.trades ?? 0) - (grid.totals[a]?.trades ?? 0),
  );
  return grid;
}

/** One cell: net P&L over its own trade count. The count is the point — a
    net figure without its denominator is the lie this project names by name. */
function Cell({ cell }: { cell: BucketCell | undefined }) {
  if (!cell || cell.trades === 0) {
    // Deliberately empty. See the panel docstring: this is "never held here",
    // which is a different statement from "held here and made nothing".
    return (
      <td className="hb-cell is-none" title="Bu strateji bu kovada hiç işlem kapatmadı">
        <span className="hb-empty" aria-label="işlem yok">
          ·
        </span>
      </td>
    );
  }
  const measured = cell.trades - cell.unmeasured;
  const net = measured > 0 ? cell.net : null;
  const thin = cell.trades < THIN_SAMPLE;
  return (
    <td
      className={`hb-cell${thin ? " is-thin" : ""}`}
      title={
        `${cell.trades} işlem` +
        (cell.unmeasured > 0 ? ` (${cell.unmeasured} tanesinin net P&L'i bildirilmedi)` : "") +
        (measured > 0 ? ` · win rate ${formatRate(cell.wins / measured, 0)}` : "") +
        (thin ? " · örneklem bir performans okuması için ince" : "")
      }
    >
      <span className={`hb-net ${tone(net) ?? "muted"}`}>
        {net === null ? DASH : formatSignedMicroMoney(net)}
      </span>
      <span className="hb-n">{formatInt(cell.trades)} işlem</span>
    </td>
  );
}

export function HoldingBuckets({
  buckets,
  trades,
  loading,
}: {
  buckets: HoldingBucketState | null | undefined;
  trades: TradeRow[] | null;
  loading: boolean;
}) {
  const served = buckets?.rows && buckets.rows.length > 0 ? buckets : null;
  const grid = served ? gridFromRows(served) : deriveHoldingBuckets(trades);
  const derived = !served;

  const medians: Record<string, number> = served
    ? Object.fromEntries(
        Object.entries(buckets?.median_hold_seconds ?? {})
          .map(([k, v]) => [k, toNum(v)] as const)
          .filter((e): e is readonly [string, number] => e[1] !== null),
      )
    : medianHoldByStrategy(trades);

  const sample = toNum(served?.sample) ?? grid.sample;

  const meta = (
    <>
      <span className="pill is-off" title="Kovaların hesaplandığı kapanmış işlem sayısı">
        {formatInt(sample)} işlem
      </span>
      <span className="pill is-off" title="Satır sayısı — hiçbir satır diğeriyle toplanmaz">
        {formatInt(grid.strategies.length)} strateji
      </span>
    </>
  );

  return (
    <Panel
      idx="20"
      title="Tutma süresi kovaları"
      note="maliyet sonrası net performans — strateji strateji, asla havuzlanmadan"
      meta={meta}
      flush
      footer={
        "Satırlar ASLA toplanmaz. flow_impulse medyan ~32 saniye, momentum_scalp ~71 saniye tutar; tek bir «30–60 sn» satırı, kasıtlı olarak hızlı bir stratejiyi yavaş bir stratejinin kuyruğuyla ortalar ve ortalamayı sonuç diye sunar. 30 saniyelik bir flow_impulse turu, impuls tükendiyse DOĞRU turdur. " +
        "Boş bir hücre (·) o stratejinin o kovada hiç tur kapatmadığı anlamına gelir; sıfır dolar anlamına gelmez — bu ikisi burada aynı şekilde çizilmez. Her hücre kendi işlem sayısını taşır: denominatörü olmayan bir net rakam bir performans okuması değildir, beş işlemlik bir hücre de öyle. " +
        (derived
          ? "KAYNAK: kapanmış işlem listesi (API son 200 turu yayınlar). Worker kendi kova bloğunu (`holding_buckets.rows`) yayınlamaya başladığında bu tablo ondan beslenir."
          : "KAYNAK: worker'ın kendi kova bloğu.")
      }
    >
      {loading && grid.strategies.length === 0 ? (
        <Skeleton rows={5} />
      ) : grid.strategies.length === 0 ? (
        <DataState
          title="Kova dağılımı yok"
          text="Hiçbir strateji henüz kapanmış tur üretmedi, bu yüzden hiçbir kovada ölçüm yok. Boş bir tablo sıfır performans değildir: ölçülecek bir tur olmadığı okumasıdır."
        />
      ) : (
        <>
          <div className="tbl-wrap">
            <table className="tbl hb">
              <thead>
                <tr>
                  <th className="l">Strateji</th>
                  <th className="l" title="Bu stratejinin medyan tutma süresi — satırların neden ayrı olduğunun kanıtı">
                    Medyan tutma
                  </th>
                  {HOLDING_BUCKETS.map((b) => (
                    <th key={b.key} className="c">
                      {b.label}
                    </th>
                  ))}
                  <th title="Satır toplamı — yalnızca bu stratejinin kendi kovaları">Toplam net</th>
                  <th title="Bu stratejinin kapanmış tur sayısı">İşlem</th>
                </tr>
              </thead>
              <tbody>
                {grid.strategies.map((s) => {
                  const total = grid.totals[s];
                  const measured = total ? total.trades - total.unmeasured : 0;
                  return (
                    <tr key={s}>
                      <td className="l sym" title={s}>
                        {strategyLabel(s)}
                      </td>
                      <td className="l dim" title="medyan, saniye">
                        {medians[s] === undefined ? DASH : formatDuration(medians[s])}
                      </td>
                      {HOLDING_BUCKETS.map((b) => (
                        <Cell key={b.key} cell={cellOf(grid, s, b.key)} />
                      ))}
                      <td className={`net ${measured > 0 ? (tone(total?.net ?? null) ?? "muted") : "muted"}`}>
                        {measured > 0 ? formatSignedMicroMoney(total?.net ?? null) : DASH}
                      </td>
                      <td className="dim">{formatInt(total?.trades ?? null)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div className="hb-foot">
            <span className="hb-legend">
              <span className="hb-empty" aria-hidden>
                ·
              </span>{" "}
              bu kovada hiç işlem yok — sıfır değil, ölçüm yok
            </span>
            <span className="hb-legend is-thin">
              soluk hücre: {THIN_SAMPLE} işlemin altında, performans okuması sayılmaz
            </span>
            {grid.undated > 0 && (
              <span className="hb-legend is-warn">
                {formatInt(grid.undated)} tur tutma süresini bildirmedi ve hiçbir kovaya
                yazılmadı — hiçbir satıra sessizce eklenmediler
              </span>
            )}
          </div>
        </>
      )}
    </Panel>
  );
}
