import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import {
  DASH,
  baseAsset,
  formatBps,
  formatCountdownMs,
  formatInt,
  formatMicroMoney,
  formatMoney,
  opportunityStatusLabel,
  opportunityStatusTone,
  rejectReasonLabel,
  strategyLabel,
  toNum,
} from "@/lib/format";
import { opportunityRows } from "@/lib/phase6";
import type { OpportunityBoard as BoardState, ScannerRow } from "@/lib/types";

/**
 * LIVE OPPORTUNITY BOARD — what the allocator is choosing between, right now.
 *
 * This is the trader's primary screen and it is deliberately the first table
 * on the page. Every other panel here explains something that already
 * happened; this one is the only place where the *decision in front of the
 * engine* is visible, ranked, with the refusals kept in the same list rather
 * than swept into a log.
 *
 * Three rules the table encodes:
 *
 *   * A rejected candidate carries its reason on the same row. A board that
 *     shows only what survived cannot tell you why the book is empty.
 *
 *   * A rejected candidate has NO RANK. Rank is a position in the queue the
 *     allocator was choosing from, and printing "7th" beside REJECTED would
 *     claim it was in a queue it never entered.
 *
 *   * The EV column is labelled in bps and the notional it was sized against
 *     sits beside it. "+12 bps" on an unknown position size is not an
 *     expectation anybody can compare.
 */
export function OpportunityBoard({
  board,
  scanner,
  loading,
  connected,
  now,
}: {
  board: BoardState | null | undefined;
  scanner: ScannerRow[] | null;
  loading: boolean;
  connected: boolean;
  now: number;
}) {
  const { rows, derived } = opportunityRows(board?.candidates ?? board?.entries, scanner);

  const queued = rows.filter((r) => String(r.status ?? "").toUpperCase() === "QUEUED").length;
  const selected = rows.filter((r) => String(r.status ?? "").toUpperCase() === "SELECTED").length;
  const rejected = rows.filter((r) => String(r.status ?? "").toUpperCase() === "REJECT").length;
  const evaluated = toNum(board?.evaluated) ?? toNum(board?.size) ?? rows.length;

  // Why entries left the board. An open record, like every other tally on this
  // screen: a reason the worker adds later still reaches the operator.
  const dropped = Object.entries(board?.dropped ?? {})
    .map(([k, v]) => [k, toNum(v)] as const)
    .filter((e): e is readonly [string, number] => e[1] !== null && e[1] > 0)
    .sort((a, b) => b[1] - a[1]);

  const meta = (
    <>
      <span className="pill is-off" title="Symbols evaluated in this cycle">
        {formatInt(evaluated)} candidates
      </span>
      <span className="pill is-pos" title="Every row is a simulation; there is no real order.">
        PAPER
      </span>
    </>
  );

  return (
    <Panel
      idx="17"
      title="Live opportunity board"
      note="the queue the allocator chose from — ordered by expected NET EV"
      meta={meta}
      flush
      footer={
        derived
          ? "SOURCE: scanner rows. The worker is not publishing its own queue (`opportunities.entries`) yet, so this ordering is DERIVED from the scanner's net-edge reading — it is not the allocator's real queue, and where the two diverge is exactly where a selection defect would live. The EV column is in BASIS POINTS (1 bps = 0.01%), not dollars; a dollar figure cannot be computed until position size arrives. A rejected candidate has no rank: showing one would place it in a queue it never entered. Every row is on paper; the real order count is zero."
          : "The EV column is in BASIS POINTS (1 bps = 0.01%). A dollar figure is shown only when the candidate's notional is reported too — an expectancy without a denominator cannot be compared across candidates. A rejected candidate has no rank. Every row is on paper; the real order count is zero."
      }
    >
      {loading && rows.length === 0 ? (
        <Skeleton rows={6} />
      ) : rows.length === 0 ? (
        <DataState
          scanning={connected}
          title="No candidates on the board"
          text={
            connected
              ? "The scanner is running but no symbol has produced a candidate yet. This is not an empty table, it is a reading: right now there is nothing in front of the allocator to choose."
              : "The exchange connection is down. Candidates are not ranked on a stale book, so this board is deliberately empty."
          }
        />
      ) : (
        <>
          <div className="obar">
            <span className="obar-k">Bu turda</span>
            <span className="obar-i is-selected">
              <b>{formatInt(selected)}</b> selected
            </span>
            <span className="obar-i is-queued">
              <b>{formatInt(queued)}</b> queued
            </span>
            <span className="obar-i is-reject">
              <b>{formatInt(rejected)}</b> reddedildi
            </span>
            <span className="obar-n">
              {formatInt(evaluated)} candidates evaluated
              {derived ? " · derived from the scanner" : ""}
              {toNum(board?.max_size) === null
                ? ""
                : ` · pano kapasitesi ${formatInt(board?.max_size ?? null)}`}
            </span>
          </div>

          {dropped.length > 0 && (
            <p className="obar-drop">
              Candidates dropped from the board:{" "}
              {dropped.map(([k, v]) => `${k} ${formatInt(v)}`).join(" · ")}. Being
              dropped is not being rejected — a candidate also leaves the board when it
              expires, is beaten by a better one, or is no longer valid.
            </p>
          )}

          <div className="tbl-wrap">
            <table className="tbl">
              <thead>
                <tr>
                  <th className="c" title="The allocator's rank. A rejected candidate has none.">
                    Rank
                  </th>
                  <th className="l">Coin</th>
                  <th className="l">Strategy</th>
                  <th title="Expected NET expected value, in basis points">Expected net EV</th>
                  <th title="The candidate's notional — the denominator of EV">Notional</th>
                  <th title="EV in dollars, only when the worker reports it together with size">
                    Net EV ($)
                  </th>
                  <th className="l" title="Episode id — the same impulse cannot produce a second entry">
                    Episode
                  </th>
                  <th title="How long a queued candidate stays valid">Validity</th>
                  <th className="c">Status</th>
                  <th className="l">Rejection reason</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => {
                  const status = String(r.status ?? "").toUpperCase();
                  const isReject = status === "REJECT";
                  const ev = toNum(r.net_ev_bps);
                  const evCls = ev === null ? "muted" : ev > 0 ? "pos" : ev < 0 ? "neg" : "dim";
                  const rank = toNum(r.rank);
                  return (
                    <tr key={`${r.symbol ?? "?"}-${r.episode_id ?? i}`}>
                      <td className="c rank">
                        {rank === null ? <span className="muted">{DASH}</span> : formatInt(rank)}
                      </td>
                      <td className="l sym" title={r.symbol ?? undefined}>
                        {r.symbol ? baseAsset(r.symbol) : DASH}
                      </td>
                      <td className="l dim" title={r.strategy ?? undefined}>
                        {r.strategy ? strategyLabel(r.strategy) : DASH}
                      </td>
                      <td className={evCls} title="bps, net">
                        {formatBps(ev)}
                      </td>
                      <td className={r.notional_usd ? "dim" : "muted"}>
                        {formatMoney(r.notional_usd ?? null)}
                      </td>
                      <td className={toNum(r.net_ev_usd) === null ? "muted" : evCls}>
                        {toNum(r.net_ev_usd) === null
                          ? DASH
                          : formatMicroMoney(r.net_ev_usd ?? null)}
                      </td>
                      <td className="l dim ep" title={r.episode_id ?? "Episode id not reported"}>
                        {r.episode_id ? r.episode_id.slice(0, 8) : DASH}
                      </td>
                      <td className={toNum(r.expires_ms) === null ? "muted" : "dim"}>
                        {toNum(r.expires_ms) === null
                          ? DASH
                          : formatCountdownMs(r.expires_ms ?? null, now)}
                      </td>
                      <td className="c">
                        <span className={`ostat ${opportunityStatusTone(r.status)}`}>
                          {opportunityStatusLabel(r.status)}
                        </span>
                      </td>
                      <td
                        className={`l ${isReject ? "warn" : "muted"}`}
                        title={r.reject_reason ?? undefined}
                      >
                        {r.reject_reason ? rejectReasonLabel(r.reject_reason) : DASH}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Panel>
  );
}
