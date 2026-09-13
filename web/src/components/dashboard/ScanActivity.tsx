import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import { formatMoney, toNum } from "@/lib/format";
import type { ScanState, WatchlistState } from "@/lib/types";

/** A scan that has not run in this long is a different fact from a quiet market. */
const STALL_SECONDS = 30;
/** Within this much of the WARM bar is a near miss worth highlighting. */
const NEAR_MISS = 0.3;

/**
 * SCAN AND BENCH — the panel that answers "is this thing alive".
 *
 * Added after the terminal was reported dead three times while the worker was
 * evaluating about once a second and the API was serving payloads a second
 * old. Every figure on the screen was a legitimate zero — no trades, no
 * positions, no candidates — and zero activity and zero EVIDENCE of activity
 * render identically.
 *
 * So this reports the two things a static screen cannot: the scan's own pulse,
 * and the pairs the bot ranked and benched. "Nothing is scanning" and "six
 * pairs ranked, five benched by a number you can read" are different claims,
 * and only the second was ever true.
 */
export function ScanActivity({
  scan,
  watchlist,
  loading,
}: {
  scan: ScanState | null | undefined;
  watchlist: WatchlistState | null | undefined;
  loading: boolean;
}) {
  const rate = toNum(scan?.evaluations_per_second);
  const since = toNum(scan?.seconds_since_last);
  const evals = toNum(scan?.evaluations);
  const benched = watchlist?.benched ?? [];
  const evaluated = watchlist?.evaluated ?? [];

  const stalled = since !== null && since > STALL_SECONDS;
  const alive = rate !== null && rate > 0 && !stalled;
  const warmBar = toNum(watchlist?.warm_threshold);

  return (
    <Panel
      idx="07"
      title="SCAN AND BENCH"
      note="is the engine running, how many pairs are being evaluated, why the rest are benched"
      meta={
        <span className={`pill ${alive ? "is-pos" : stalled ? "is-neg" : "is-warn"}`}>
          {alive ? "SCAN ACTIVE" : stalled ? "SCAN STALLED" : "WAITING"}
        </span>
      }
    >
      {loading && !scan ? (
        <Skeleton rows={3} />
      ) : !scan ? (
        <DataState
          title="Scan data has not been published yet"
          text="The worker is not publishing this block. That does not mean scanning has stopped — only that the screen has no field to see it in, which is exactly the problem this panel exists to fix."
          scanning
        />
      ) : (
        <>
            <div className="metrics">
              <Metric
                k="Evaluations"
                v={evals === null ? "—" : evals.toLocaleString("tr-TR")}
                note="since process start"
              />
              <Metric
                k="Scan rate"
                v={rate === null ? "—" : `${rate.toFixed(2)}/sn`}
                note="evaluations per second"
                tone={alive ? "pos" : undefined}
              />
              <Metric
                k="Last scan"
                v={since === null ? "—" : `${since.toFixed(1)} sn`}
                note={stalled ? `over ${STALL_SECONDS}s — stalled` : "ago"}
                tone={stalled ? "neg" : undefined}
              />
              <Metric
                k="Evaluated"
                v={String(evaluated.length)}
                note={evaluated.join(", ") || "—"}
              />
              <Metric
                k="Bankta"
                v={String(benched.length)}
                note="ranked, did not clear the threshold"
              />
            </div>

            {benched.length > 0 && (
              <div className="tbl-wrap">
                <table className="tbl">
                  <thead>
                    <tr>
                      <th>Sembol</th>
                      <th className="num">Edge ratio</th>
                      <th className="num">Threshold</th>
                      <th className="num">Eksik</th>
                      <th className="num">Round trip</th>
                      <th className="num">Beklenen hareket</th>
                      <th className="num">Kapasite</th>
                    </tr>
                  </thead>
                  <tbody>
                    {benched.map((b) => {
                      const shortBy = toNum(b.short_of_warm);
                      const near =
                        shortBy !== null && shortBy > 0 && shortBy <= NEAR_MISS;
                      return (
                        <tr key={b.symbol}>
                          <td>{b.symbol}</td>
                          <td className={`num${near ? " pos" : ""}`}>
                            {toNum(b.edge_ratio)?.toFixed(2) ?? "—"}
                          </td>
                          <td className="num">
                            {toNum(b.warm_threshold)?.toFixed(2) ?? "—"}
                          </td>
                          <td className={`num${near ? " pos" : ""}`}>
                            {shortBy === null || shortBy <= 0
                              ? "—"
                              : shortBy.toFixed(2)}
                          </td>
                          <td className="num">
                            {toNum(b.round_trip_bps)?.toFixed(2) ?? "—"} bps
                          </td>
                          <td className="num">
                            {toNum(b.expected_move_bps)?.toFixed(1) ?? "—"} bps
                          </td>
                          <td className="num">
                            {formatMoney(toNum(b.capacity_max_usd))}/dk
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}

            <p className="panel-note">
              Edge ratio = the pair's expected move ÷ its round-trip cost.{" "}
              {warmBar === null ? "1.50" : warmBar.toFixed(2)} — a pair below that
              cannot pay for its own execution, so it is not scanned. That is a
              measurement, not a fault. Rows that miss the threshold by
              {" "}{NEAR_MISS.toFixed(2)} or less are highlighted.
            </p>
        </>
      )}
    </Panel>
  );
}

function Metric({
  k,
  v,
  note,
  tone,
}: {
  k: string;
  v: string;
  note?: string;
  tone?: "pos" | "neg";
}) {
  return (
    <div className="metric">
      <span className="metric-k">{k}</span>
      <span className={`metric-v${tone ? ` ${tone}` : ""}`}>{v}</span>
      {note && <span className="metric-note">{note}</span>}
    </div>
  );
}
