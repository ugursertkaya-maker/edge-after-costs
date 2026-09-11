"use client";

import { Fragment, useState } from "react";
import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import {
  CheckpointBadge,
  Chevron,
  CostGroup,
  HorizonMeter,
  MakerTakerBadge,
  NetEquation,
  StrengthBar,
} from "@/components/dashboard/bits";
import {
  DASH,
  baseAsset,
  checkpointLabel,
  formatClockMs,
  formatCost,
  formatCountdownMs,
  formatDateTimeMs,
  formatDuration,
  formatHoldWindow,
  formatMicroMoney,
  formatMoney,
  formatPct,
  formatPrice,
  formatSignedMicroMoney,
  makerTakerLabel,
  sideLabel,
  strategyLabel,
  strengthPct,
  toNum,
  tone,
} from "@/lib/format";
import type { Num, PositionRow } from "@/lib/types";

const COLS = 15;

/**
 * Everything a row needs, with the V1 fallbacks already resolved.
 *
 * V1 reports `unrealized_usd` as price move × qty — exactly the gross term —
 * and `fees_usd` as the entry fee alone, so both fold into the V2 stack
 * without being relabelled into something they are not. The cost total and the
 * NET figure have no V1 equivalent and stay null: an unmeasured cost is not a
 * zero cost, and the dashboard will not invent one.
 */
function read(r: PositionRow, generatedMs?: Num) {
  const age = r.age_seconds ?? r.holding_seconds;
  const ageNum = toNum(age);
  const gen = toNum(generatedMs);
  return {
    gross: r.gross_pnl_usd ?? r.unrealized_usd,
    entryFee: r.entry_fee_usd ?? r.fees_usd,
    cost: r.total_cost_usd ?? null,
    net: r.net_pnl_usd ?? null,
    age,
    accountRisk: r.account_risk_usd ?? r.risk_usd,
    entryMs:
      toNum(r.entry_ms) ?? (ageNum !== null && gen !== null ? gen - ageNum * 1000 : null),
    derivedEntry: toNum(r.entry_ms) === null,
  };
}

export function Detail({
  r,
  now,
  generatedMs,
}: {
  r: PositionRow;
  now: number;
  generatedMs?: Num;
}) {
  const d = read(r, generatedMs);

  return (
    <tr className="xdetail">
      <td colSpan={COLS}>
        <div className="xbody">
          <NetEquation
            gross={formatSignedMicroMoney(d.gross)}
            cost={formatCost(d.cost)}
            net={formatSignedMicroMoney(d.net)}
            grossCls={tone(d.gross) ?? "muted"}
            netCls={d.net === null ? "muted" : (tone(d.net) ?? "muted")}
          />

          <div className="xcols">
            <CostGroup
              title="Maliyet kalemleri"
              items={[
                {
                  k: "Giriş komisyonu",
                  v: formatCost(d.entryFee),
                  cls: d.entryFee === null ? "muted" : "neg",
                },
                {
                  k: "Beklenen çıkış komisyonu",
                  v: formatCost(r.expected_exit_fee_usd ?? null),
                  cls: (r.expected_exit_fee_usd ?? null) === null ? "muted" : "neg",
                },
                {
                  k: "Makas maliyeti",
                  v: formatCost(r.spread_cost_usd ?? null),
                  cls: (r.spread_cost_usd ?? null) === null ? "muted" : "neg",
                },
                {
                  k: "Kayma",
                  v: formatCost(r.slippage_usd ?? null),
                  cls: (r.slippage_usd ?? null) === null ? "muted" : "neg",
                },
                {
                  k: "Toplam işlem maliyeti",
                  v: formatCost(d.cost),
                  cls: d.cost === null ? "muted" : "neg is-total",
                },
              ]}
            />

            <CostGroup
              title="Fiyat seviyeleri"
              items={[
                { k: "Giriş", v: formatPrice(r.entry_price) },
                { k: "Güncel", v: formatPrice(r.current_price), cls: "sym" },
                {
                  k: "Başabaş fiyat",
                  v: formatPrice(r.break_even_price ?? null),
                  cls: (r.break_even_price ?? null) === null ? "muted" : "warn",
                  title: "Komisyon, makas ve kayma bu fiyatta geri kazanılır",
                },
                {
                  k: "NET TP fiyat",
                  v: formatPrice(r.net_tp_price ?? r.take_profit),
                  cls: (r.net_tp_price ?? r.take_profit) === null ? "muted" : "pos",
                },
                {
                  k: "NET TP %",
                  v: formatPct(r.net_tp_pct ?? null, 2),
                  cls: (r.net_tp_pct ?? null) === null ? "muted" : "pos",
                },
                { k: "Stop", v: formatPrice(r.stop_loss), cls: "neg" },
              ]}
            />

            <CostGroup
              title="Pozisyon ve risk"
              items={[
                { k: "Pozisyon büyüklüğü", v: formatMoney(r.notional_usd) },
                {
                  k: "Hesap riski",
                  v:
                    d.accountRisk === null || d.accountRisk === undefined
                      ? DASH
                      : formatMoney(-Math.abs(d.accountRisk)),
                  cls: d.accountRisk ? "neg" : "muted",
                  title:
                    "Pozisyon büyüklüğü değil — stop dolarsa hesaptan gidecek modellenmiş tutar",
                },
                {
                  k: "Dolum tipi",
                  v: makerTakerLabel(r.maker_or_taker ? String(r.maker_or_taker) : null),
                  cls: r.maker_or_taker ? "" : "muted",
                },
                {
                  k: "Sinyal gücü",
                  v:
                    strengthPct(r.signal_strength ?? null) === null
                      ? DASH
                      : (strengthPct(r.signal_strength ?? null) as number).toFixed(0),
                  cls: (r.signal_strength ?? null) === null ? "muted" : "",
                },
                {
                  k: "Strateji",
                  v: r.strategy ? strategyLabel(r.strategy) : DASH,
                  cls: r.strategy ? "" : "muted",
                },
              ]}
            />

            <CostGroup
              title="Zaman ufku"
              items={[
                {
                  k: "Giriş zamanı",
                  v: formatDateTimeMs(d.entryMs),
                  cls: d.entryMs === null ? "muted" : "dim",
                  title: d.derivedEntry
                    ? "Yaştan türetildi — worker giriş zamanını ayrıca raporlamıyor"
                    : undefined,
                },
                { k: "Yaş", v: formatDuration(d.age) },
                {
                  k: "Beklenen tutma penceresi",
                  v: formatHoldWindow(r.expected_hold_window),
                  cls:
                    r.expected_hold_window === null || r.expected_hold_window === undefined
                      ? "muted"
                      : "",
                },
                {
                  k: "25-dk kontrol durumu",
                  v: checkpointLabel(
                    r.checkpoint_status ? String(r.checkpoint_status) : null,
                  ),
                  cls: r.checkpoint_status ? "" : "muted",
                },
                {
                  k: "60-dk zaman stopu",
                  v:
                    toNum(r.time_stop_ms) === null
                      ? DASH
                      : `${formatClockMs(r.time_stop_ms)} · ${formatCountdownMs(
                          r.time_stop_ms,
                          now,
                        )}`,
                  cls: toNum(r.time_stop_ms) === null ? "muted" : "warn",
                },
              ]}
            />
          </div>

          <p className="xnote">
            {d.cost === null
              ? `Brüt ${formatSignedMicroMoney(d.gross)} — maliyet dökümü henüz raporlanmadığı için bu hareketin net karşılığı bilinmiyor.`
              : `Brüt ${formatSignedMicroMoney(d.gross)}, ${formatMicroMoney(
                  Math.abs(toNum(d.cost) ?? 0),
                )} maliyetten sonra ${formatSignedMicroMoney(d.net)} nettir.`}
          </p>
        </div>
      </td>
    </tr>
  );
}

/**
 * Open paper positions.
 *
 * The row is deliberately split in two. The compact line carries what an
 * operator scans — size, account risk, the two prices, the three P&L terms,
 * how the entry filled, and where the position sits in its 25–60 minute
 * window. Clicking it opens the full cost stack, the three price levels V2
 * gives every position (break-even, NET TP, stop) and the horizon state.
 * Fifteen cost columns in one table would be a horizontal scrollbar pretending
 * to be a report, with the figures that decide anything parked off-screen.
 *
 * Brüt, Maliyet and NET are three separate columns and are never collapsed:
 * the entire point of V2 is that the first can be positive while the third is
 * negative.
 */
export function Positions({
  rows,
  loading,
  now,
  generatedMs,
}: {
  rows: PositionRow[] | null;
  loading: boolean;
  /** The page's single clock, so every age and countdown ticks in step. */
  now: number;
  /** When the payload was built — the reference for a derived entry time. */
  generatedMs?: Num;
}) {
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const toggle = (id: string) => setOpen((prev) => ({ ...prev, [id]: !prev[id] }));

  const exposure = rows?.reduce((acc, r) => acc + (r.notional_usd ?? 0), 0) ?? null;
  const risk =
    rows?.reduce((acc, r) => acc + (r.account_risk_usd ?? r.risk_usd ?? 0), 0) ?? null;

  return (
    <Panel
      idx="04"
      title="Açık pozisyonlar"
      note="anlık fiyatla değerlenmiş · satıra tıklayın, maliyet dökümü açılır"
      meta={
        rows && rows.length > 0 ? (
          <span className="panel-note">
            toplam tutar {formatMoney(exposure)} · hesap riski {formatMoney(risk)}
          </span>
        ) : null
      }
      flush
      footer="Pozisyon büyüklüğü risk değildir. Brüt P&L fiyat hareketinin kazandırdığıdır; komisyon, makas ve kayma düşülmeden kâr sayılmaz. NET sütunu boşsa worker o pozisyonun maliyet dökümünü henüz raporlamamıştır — sıfır maliyet demek değildir."
    >
      {loading && !rows ? (
        <Skeleton rows={3} />
      ) : !rows || rows.length === 0 ? (
        <DataState
          title="Açık pozisyon yok"
          text="Sermayenin tamamı nakitte. Bu bir hata değil: net edge eşiğini geçen bir kurulum bulunana kadar bot kasıtlı olarak beklemede kalır."
        />
      ) : (
        <div className="tbl-wrap">
          <table className="tbl tbl-x">
            <thead>
              <tr>
                <th className="c x" aria-label="Detay" />
                <th className="l">Coin</th>
                <th className="c">Yön</th>
                <th>Pozisyon $</th>
                <th>Hesap riski $</th>
                <th>Giriş</th>
                <th>Güncel</th>
                <th>Brüt P&amp;L</th>
                <th>Maliyet</th>
                <th>NET P&amp;L</th>
                <th className="c">Dolum</th>
                <th className="l">Yaş · 25/60 dk</th>
                <th className="c">25-dk kontrol</th>
                <th className="l">Sinyal</th>
                <th className="l">Strateji</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const isOpen = !!open[r.id];
                const d = read(r, generatedMs);
                return (
                  <Fragment key={r.id}>
                    <tr
                      className={`xrow${isOpen ? " is-open" : ""}`}
                      role="button"
                      tabIndex={0}
                      aria-expanded={isOpen}
                      onClick={() => toggle(r.id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          toggle(r.id);
                        }
                      }}
                    >
                      <td className="c x">
                        <Chevron open={isOpen} />
                      </td>
                      <td className="l sym" title={r.symbol}>
                        {baseAsset(r.symbol)}
                      </td>
                      <td className="c">
                        <span
                          className={`side ${r.side === "SHORT" ? "short" : "long"}`}
                          title={String(r.side)}
                        >
                          {sideLabel(String(r.side))}
                        </span>
                      </td>
                      <td>{formatMoney(r.notional_usd)}</td>
                      <td className="dim" title="Stop dolarsa modellenen kayıp">
                        {formatMoney(d.accountRisk)}
                      </td>
                      <td>{formatPrice(r.entry_price)}</td>
                      <td className="sym">{formatPrice(r.current_price)}</td>
                      <td
                        className={tone(d.gross) ?? "muted"}
                        title="Fiyat hareketinin kazandırdığı — maliyet düşülmemiştir"
                      >
                        {formatSignedMicroMoney(d.gross)}
                      </td>
                      <td className={d.cost === null ? "muted" : "neg"}>
                        {formatCost(d.cost)}
                      </td>
                      <td
                        className={`net ${d.net === null ? "muted" : (tone(d.net) ?? "muted")}`}
                      >
                        {formatSignedMicroMoney(d.net)}
                      </td>
                      <td className="c">
                        <MakerTakerBadge value={r.maker_or_taker} />
                      </td>
                      <td className="l">
                        <HorizonMeter ageSeconds={d.age} />
                      </td>
                      <td className="c">
                        <CheckpointBadge status={r.checkpoint_status} />
                      </td>
                      <td className="l">
                        <StrengthBar value={r.signal_strength ?? null} />
                      </td>
                      <td className="l dim" title={r.strategy ?? undefined}>
                        {r.strategy ? strategyLabel(r.strategy) : DASH}
                      </td>
                    </tr>
                    {isOpen && <Detail r={r} now={now} generatedMs={generatedMs} />}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
