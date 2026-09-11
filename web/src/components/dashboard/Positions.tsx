import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import {
  DASH,
  baseAsset,
  formatDuration,
  formatMicroMoney,
  formatMoney,
  formatPrice,
  formatSignedMicroMoney,
  sideLabel,
  strategyLabel,
  tone,
} from "@/lib/format";
import type { PositionRow } from "@/lib/types";

/**
 * Open paper positions. Every row carries its stop: a position without a
 * defined exit is a defect in this system, so SL is a first-class column and
 * never folded into a detail view.
 */
export function Positions({
  rows,
  loading,
}: {
  rows: PositionRow[] | null;
  loading: boolean;
}) {
  const exposure = rows?.reduce((acc, r) => acc + (r.notional_usd ?? 0), 0) ?? null;
  const risk = rows?.reduce((acc, r) => acc + (r.risk_usd ?? 0), 0) ?? null;

  return (
    <Panel
      idx="04"
      title="Açık pozisyonlar"
      note="anlık fiyatla değerlenmiş"
      meta={
        rows && rows.length > 0 ? (
          <span className="panel-note">
            toplam tutar {formatMoney(exposure)} · risk {formatMoney(risk)}
          </span>
        ) : null
      }
      flush
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
          <table className="tbl">
            <thead>
              <tr>
                <th className="l">Coin</th>
                <th className="c">Yön</th>
                <th>Giriş</th>
                <th>Güncel</th>
                <th>Pozisyon $</th>
                <th>Risk $</th>
                <th>TP</th>
                <th>SL</th>
                <th>Gerçekleşmemiş P&amp;L</th>
                <th>Komisyon</th>
                <th>Tutma süresi</th>
                <th className="l">Strateji</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
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
                  <td>{formatPrice(r.entry_price)}</td>
                  <td className="sym">{formatPrice(r.current_price)}</td>
                  <td>{formatMoney(r.notional_usd)}</td>
                  <td className="dim">{formatMoney(r.risk_usd)}</td>
                  <td className="pos">{formatPrice(r.take_profit)}</td>
                  <td className="neg">{formatPrice(r.stop_loss)}</td>
                  <td className={`net ${tone(r.unrealized_usd) ?? "muted"}`}>
                    {formatSignedMicroMoney(r.unrealized_usd)}
                  </td>
                  <td className="dim">{formatMicroMoney(r.fees_usd)}</td>
                  <td className="dim">{formatDuration(r.holding_seconds)}</td>
                  <td className="l dim" title={r.strategy ?? undefined}>
                    {r.strategy ? strategyLabel(r.strategy) : DASH}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
