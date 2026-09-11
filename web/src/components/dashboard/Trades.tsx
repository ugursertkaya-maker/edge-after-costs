"use client";

import { Fragment, useEffect, useMemo, useState } from "react";
import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import {
  Chevron,
  CostGroup,
  MakerTakerBadge,
  NetEquation,
} from "@/components/dashboard/bits";
import type { CostItem } from "@/components/dashboard/bits";
import {
  DASH,
  baseAsset,
  exitReasonLabel,
  formatCost,
  formatDateTimeMs,
  formatDuration,
  formatMicroMoney,
  formatMoney,
  formatPct,
  formatPrice,
  formatSignedMicroMoney,
  makerTakerLabel,
  strategyLabel,
  toNum,
  tone,
} from "@/lib/format";
import type { TradeRow } from "@/lib/types";

const PAGE_SIZE = 25;
const COLS = 13;

/**
 * Resolve a closed trade's cost stack, including what V1 can still answer.
 *
 * `total_cost_usd` is preferred, but when it is missing the total is not
 * guessed — it is derived from the identity the payload already asserts,
 * `gross − cost = net`, using two figures V1 has always reported. That is
 * arithmetic on measured values, not a substitute for a measurement, and it is
 * the one place the dashboard fills a gap rather than printing a dash.
 */
function read(t: TradeRow) {
  const gross = toNum(t.gross_pnl_usd);
  const net = toNum(t.net_pnl_usd);
  const reportedCost = toNum(t.total_cost_usd);
  const derivedCost = gross !== null && net !== null ? gross - net : null;

  const entryFee = t.entry_fee_usd ?? null;
  const exitFee = t.exit_fee_usd ?? t.expected_exit_fee_usd ?? null;
  const splitKnown = toNum(entryFee) !== null || toNum(exitFee) !== null;

  const hold =
    toNum(t.exit_ms) !== null && toNum(t.entry_ms) !== null
      ? ((toNum(t.exit_ms) as number) - (toNum(t.entry_ms) as number)) / 1000
      : null;

  return {
    gross,
    net,
    cost: reportedCost ?? derivedCost,
    costDerived: reportedCost === null && derivedCost !== null,
    entryFee,
    exitFee,
    exitFeeActual: t.exit_fee_usd !== undefined && t.exit_fee_usd !== null,
    splitKnown,
    hold,
  };
}

export function Detail({ t }: { t: TradeRow }) {
  const d = read(t);

  const costItems: CostItem[] = [
    {
      k: "Giriş komisyonu",
      v: formatCost(d.entryFee),
      cls: toNum(d.entryFee) === null ? "muted" : "neg",
    },
    {
      k: d.exitFeeActual ? "Gerçekleşen çıkış komisyonu" : "Beklenen çıkış komisyonu",
      v: formatCost(d.exitFee),
      cls: toNum(d.exitFee) === null ? "muted" : "neg",
    },
    {
      k: "Makas maliyeti",
      v: formatCost(t.spread_cost_usd ?? null),
      cls: toNum(t.spread_cost_usd ?? null) === null ? "muted" : "neg",
    },
    { k: "Kayma", v: formatCost(t.slippage_usd), cls: toNum(t.slippage_usd) === null ? "muted" : "neg" },
    {
      k: "Toplam işlem maliyeti",
      v: formatCost(d.cost),
      cls: d.cost === null ? "muted" : "neg is-total",
      title: d.costDerived
        ? "Brüt − NET kimliğinden türetildi; worker ayrı bir toplam maliyet alanı raporlamıyor"
        : undefined,
    },
  ];

  // Until the entry/exit split lands, V1's single fee figure is still a real
  // measurement and is shown as what it is rather than being dropped.
  if (!d.splitKnown) {
    costItems.splice(2, 0, {
      k: "Toplam komisyon (V1)",
      v: formatCost(t.fees_usd),
      cls: toNum(t.fees_usd) === null ? "muted" : "neg",
      title: "Worker giriş/çıkış ayrımını henüz raporlamıyor",
    });
  }

  return (
    <tr className="xdetail">
      <td colSpan={COLS}>
        <div className="xbody">
          <NetEquation
            gross={formatSignedMicroMoney(d.gross)}
            cost={formatCost(d.cost)}
            net={formatSignedMicroMoney(d.net)}
            grossCls={tone(d.gross) ?? "muted"}
            netCls={tone(d.net) ?? "muted"}
          />

          <div className="xcols">
            <CostGroup title="Maliyet kalemleri" items={costItems} />

            <CostGroup
              title="Fiyat seviyeleri"
              items={[
                { k: "Giriş", v: formatPrice(t.entry_price) },
                { k: "Çıkış", v: formatPrice(t.exit_price), cls: "sym" },
                {
                  k: "Başabaş fiyat",
                  v: formatPrice(t.break_even_price ?? null),
                  cls: toNum(t.break_even_price ?? null) === null ? "muted" : "warn",
                  title: "Bu fiyatta işlem maliyetini tam olarak geri kazanıyordu",
                },
                {
                  k: "NET TP fiyat",
                  v: formatPrice(t.net_tp_price ?? null),
                  cls: toNum(t.net_tp_price ?? null) === null ? "muted" : "pos",
                },
                {
                  k: "NET TP %",
                  v: formatPct(t.net_tp_pct ?? null, 2),
                  cls: toNum(t.net_tp_pct ?? null) === null ? "muted" : "pos",
                },
                {
                  k: "Stop",
                  v: formatPrice(t.stop_loss ?? null),
                  cls: toNum(t.stop_loss ?? null) === null ? "muted" : "neg",
                },
              ]}
            />

            <CostGroup
              title="Pozisyon ve risk"
              items={[
                {
                  k: "Pozisyon büyüklüğü",
                  v: formatMoney(t.notional_usd ?? null),
                  cls: toNum(t.notional_usd ?? null) === null ? "muted" : "",
                },
                {
                  k: "Hesap riski",
                  v:
                    toNum(t.risk_usd ?? null) === null
                      ? DASH
                      : formatMoney(-Math.abs(toNum(t.risk_usd) as number)),
                  cls: toNum(t.risk_usd ?? null) === null ? "muted" : "neg",
                  title: "Pozisyon büyüklüğü değil — stop dolsaydı kaybedilecek tutar",
                },
                {
                  k: "Dolum tipi",
                  v: makerTakerLabel(t.maker_or_taker ? String(t.maker_or_taker) : null),
                  cls: t.maker_or_taker ? "" : "muted",
                },
                {
                  k: "Strateji",
                  v: t.strategy ? strategyLabel(t.strategy) : DASH,
                  cls: t.strategy ? "" : "muted",
                },
                {
                  k: "Çıkış nedeni",
                  v: exitReasonLabel(t.exit_reason),
                  cls: t.exit_reason ? "" : "muted",
                  title: t.exit_reason ?? undefined,
                },
              ]}
            />

            <CostGroup
              title="Zaman"
              items={[
                { k: "Giriş zamanı", v: formatDateTimeMs(t.entry_ms), cls: "dim" },
                { k: "Çıkış zamanı", v: formatDateTimeMs(t.exit_ms), cls: "dim" },
                {
                  k: "Tutma süresi",
                  v: formatDuration(d.hold),
                  cls: d.hold === null ? "muted" : "",
                  title: "Hedef pencere 25–60 dk",
                },
              ]}
            />
          </div>

          <p className="xnote">
            {d.cost === null
              ? `Brüt ${formatSignedMicroMoney(d.gross)} — maliyet dökümü raporlanmadı.`
              : `Brüt ${formatSignedMicroMoney(d.gross)}, ${formatMicroMoney(
                  Math.abs(d.cost),
                )} maliyetten sonra ${formatSignedMicroMoney(d.net)} nettir.`}
          </p>
        </div>
      </td>
    </tr>
  );
}

/**
 * Closed round trips, newest first, with every cost itemised.
 *
 * Brüt, Toplam maliyet and NET are three separate columns and the expanded row
 * shows what made up the middle one — entry fee, exit fee, spread and
 * slippage. The thesis of this bot is that a gross-positive trade can be a net
 * loss, so folding the costs into one number would hide exactly the thing the
 * dashboard exists to measure.
 */
export function Trades({
  rows,
  loading,
}: {
  rows: TradeRow[] | null;
  loading: boolean;
}) {
  const [page, setPage] = useState(0);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const toggle = (id: string) => setOpen((prev) => ({ ...prev, [id]: !prev[id] }));

  const sorted = useMemo(() => {
    if (!rows) return null;
    return [...rows].sort((a, b) => (b.exit_ms ?? 0) - (a.exit_ms ?? 0));
  }, [rows]);

  const total = sorted?.length ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  // A poll that shrinks the history (ledger switch, retention trim) must not
  // strand the operator on a page that no longer exists.
  useEffect(() => {
    if (page > pages - 1) setPage(0);
  }, [page, pages]);

  const slice = sorted?.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE) ?? [];

  // The three terms for the page in view, so the equation above the table and
  // the rows below it are talking about the same set of trades.
  const pageGross = slice.reduce((acc, t) => acc + (toNum(t.gross_pnl_usd) ?? 0), 0);
  const pageNet = slice.reduce((acc, t) => acc + (toNum(t.net_pnl_usd) ?? 0), 0);

  return (
    <Panel
      idx="05"
      title="İşlem geçmişi"
      note="kapanmış turlar · en yeni üstte · satıra tıklayın, maliyet dökümü açılır"
      meta={
        total > 0 ? (
          <span className="panel-note">
            bu sayfa · brüt {formatSignedMicroMoney(pageGross)} · net{" "}
            {formatSignedMicroMoney(pageNet)}
          </span>
        ) : null
      }
      flush
      footer={
        total > 0
          ? "Brüt P&L komisyon, makas ve kaymadan önceki tutardır ve kâr değildir. Karar verici sütun NET P&L'dir; aradaki fark satır açıldığında kalem kalem görünür."
          : undefined
      }
    >
      {loading && !sorted ? (
        <Skeleton rows={6} />
      ) : total === 0 ? (
        <DataState
          title="Henüz işlem yok"
          text="Kapanmış tur bulunmuyor. Bot bir kurulumu ancak net beklenen edge eşiği aştığında açar; ilk tur kapandığında bu tablo dolmaya başlar."
        />
      ) : (
        <>
          <div className="tbl-wrap">
            <table className="tbl tbl-x">
              <thead>
                <tr>
                  <th className="c x" aria-label="Detay" />
                  <th className="l">Coin</th>
                  <th className="l">Strateji</th>
                  <th>Çıkış zamanı</th>
                  <th>Pozisyon $</th>
                  <th>Giriş</th>
                  <th>Çıkış</th>
                  <th>Brüt P&amp;L</th>
                  <th>Toplam maliyet</th>
                  <th>NET P&amp;L</th>
                  <th className="c">Dolum</th>
                  <th>Tutma</th>
                  <th className="l">Çıkış nedeni</th>
                </tr>
              </thead>
              <tbody>
                {slice.map((r) => {
                  const isOpen = !!open[r.id];
                  const d = read(r);
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
                        <td className="l dim" title={r.strategy ?? undefined}>
                          {r.strategy ? strategyLabel(r.strategy) : DASH}
                        </td>
                        <td className="dim">{formatDateTimeMs(r.exit_ms)}</td>
                        <td className={toNum(r.notional_usd ?? null) === null ? "muted" : ""}>
                          {formatMoney(r.notional_usd ?? null)}
                        </td>
                        <td>{formatPrice(r.entry_price)}</td>
                        <td>{formatPrice(r.exit_price)}</td>
                        <td
                          className={tone(d.gross) ?? "muted"}
                          title="Fiyat hareketinin kazandırdığı — maliyet düşülmemiştir"
                        >
                          {formatSignedMicroMoney(d.gross)}
                        </td>
                        <td
                          className={d.cost === null ? "muted" : "neg"}
                          title={
                            d.costDerived
                              ? "Brüt − NET kimliğinden türetildi"
                              : "Komisyon + makas + kayma"
                          }
                        >
                          {formatCost(d.cost)}
                        </td>
                        <td className={`net ${tone(d.net) ?? "muted"}`}>
                          {formatSignedMicroMoney(d.net)}
                        </td>
                        <td className="c">
                          <MakerTakerBadge value={r.maker_or_taker} />
                        </td>
                        <td className={d.hold === null ? "muted" : "dim"}>
                          {formatDuration(d.hold)}
                        </td>
                        <td className="l" title={r.exit_reason ?? undefined}>
                          <span
                            className={
                              r.exit_reason === "STOP_LOSS" ||
                              r.exit_reason === "RISK_EXIT" ||
                              r.exit_reason === "EXIT_WEAK"
                                ? "neg"
                                : r.exit_reason === "TAKE_PROFIT" ||
                                    r.exit_reason === "TRAILING_PROFIT"
                                  ? "pos"
                                  : "tag"
                            }
                          >
                            {exitReasonLabel(r.exit_reason)}
                          </span>
                        </td>
                      </tr>
                      {isOpen && <Detail t={r} />}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="pager">
            <button
              type="button"
              className="btn"
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              disabled={page === 0}
            >
              ← Önceki
            </button>
            <button
              type="button"
              className="btn"
              onClick={() => setPage((p) => Math.min(pages - 1, p + 1))}
              disabled={page >= pages - 1}
            >
              Sonraki →
            </button>
            <span className="pager-info">
              {page * PAGE_SIZE + 1}–{Math.min(total, (page + 1) * PAGE_SIZE)} / {total}{" "}
              işlem · sayfa {page + 1}/{pages}
            </span>
          </div>
        </>
      )}
    </Panel>
  );
}
