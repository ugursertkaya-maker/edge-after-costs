"use client";

import { useEffect, useMemo, useState } from "react";
import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import {
  DASH,
  baseAsset,
  exitReasonLabel,
  formatDateTimeMs,
  formatMicroMoney,
  formatPrice,
  formatSignedMicroMoney,
  strategyLabel,
  tone,
} from "@/lib/format";
import type { TradeRow } from "@/lib/types";

const PAGE_SIZE = 25;

/**
 * Closed round trips, newest first, with every cost itemised. Gross, komisyon
 * and slipaj are separate columns on purpose: the whole thesis of this bot is
 * that a gross-positive trade can be a net loss, and folding the costs into
 * one number would hide exactly the thing we are trying to measure.
 */
export function Trades({
  rows,
  loading,
}: {
  rows: TradeRow[] | null;
  loading: boolean;
}) {
  const [page, setPage] = useState(0);

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

  return (
    <Panel
      idx="05"
      title="İşlem geçmişi"
      note="kapanmış turlar · en yeni üstte"
      flush
      footer={
        total > 0
          ? "Brüt P&L komisyon ve kaymadan önceki tutardır. Karar verici sütun NET P&L'dir."
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
            <table className="tbl">
              <thead>
                <tr>
                  <th className="l">Coin</th>
                  <th className="l">Strateji</th>
                  <th>Giriş zamanı</th>
                  <th>Çıkış zamanı</th>
                  <th>Giriş</th>
                  <th>Çıkış</th>
                  <th>Brüt P&amp;L</th>
                  <th>Komisyon</th>
                  <th>Slipaj</th>
                  <th>NET P&amp;L</th>
                  <th className="l">Çıkış nedeni</th>
                </tr>
              </thead>
              <tbody>
                {slice.map((r) => (
                  <tr key={r.id}>
                    <td className="l sym" title={r.symbol}>
                      {baseAsset(r.symbol)}
                    </td>
                    <td className="l dim" title={r.strategy ?? undefined}>
                      {r.strategy ? strategyLabel(r.strategy) : DASH}
                    </td>
                    <td className="dim">{formatDateTimeMs(r.entry_ms)}</td>
                    <td className="dim">{formatDateTimeMs(r.exit_ms)}</td>
                    <td>{formatPrice(r.entry_price)}</td>
                    <td>{formatPrice(r.exit_price)}</td>
                    <td className={tone(r.gross_pnl_usd) ?? "muted"}>
                      {formatSignedMicroMoney(r.gross_pnl_usd)}
                    </td>
                    <td className="dim">{formatMicroMoney(r.fees_usd)}</td>
                    <td className="dim">{formatMicroMoney(r.slippage_usd)}</td>
                    <td className={`net ${tone(r.net_pnl_usd) ?? "muted"}`}>
                      {formatSignedMicroMoney(r.net_pnl_usd)}
                    </td>
                    <td className="l" title={r.exit_reason ?? undefined}>
                      <span
                        className={
                          r.exit_reason === "STOP_LOSS" || r.exit_reason === "RISK_EXIT"
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
                ))}
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
