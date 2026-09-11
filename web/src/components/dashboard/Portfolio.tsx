import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import { Stat } from "@/components/dashboard/bits";
import {
  DASH,
  allocationModeLabel,
  formatDuration,
  formatInt,
  formatMicroMoney,
  formatMoney,
  formatPct,
  formatRatio,
  netTpProfileLabel,
  toNum,
  tone,
} from "@/lib/format";
import type { Num, PortfolioState } from "@/lib/types";

/** Where correlated exposure stops being diversification and starts being one
    bet wearing eight coins. The bands are read from the V2 brief's own
    argument, not tuned: below 40% the book is genuinely spread, 40–70% it is
    concentrating, above 70% the "many small positions" are a single trade. */
function correlationBand(pct: number): { cls: string; text: string } {
  if (pct >= 70)
    return {
      cls: "is-crit",
      text: "Açık pozisyonlar tek bir yönlü bahis gibi davranıyor. Sayı çokluğu dağıtım değildir.",
    };
  if (pct >= 40)
    return {
      cls: "is-warn",
      text: "Yoğunlaşma artıyor — pozisyonların önemli bölümü aynı hareketle kazanıp aynı hareketle kaybeder.",
    };
  return {
    cls: "is-ok",
    text: "Açık pozisyonlar büyük ölçüde birbirinden bağımsız hareket ediyor.",
  };
}

/** The 25 / 60 minute buckets as one bar. Three counts in three cells read as
    trivia; as a distribution they say whether the book is turning over or
    silently ageing into positions the horizon never intended. */
function Distribution({
  under25,
  mid,
  over60,
}: {
  under25: Num;
  mid: Num;
  over60: Num;
}) {
  const a = toNum(under25);
  const b = toNum(mid);
  const c = toNum(over60);
  const known = [a, b, c].filter((v): v is number => v !== null);
  const total = known.reduce((acc, v) => acc + v, 0);

  const seg = (v: number | null) =>
    total > 0 && v !== null ? `${(v / total) * 100}%` : "0%";

  return (
    <div className="dist">
      <div className="dist-head">
        <span className="dist-t">Yaş dağılımı</span>
        <span className="dist-n">
          {known.length === 0
            ? "ölçüm yok"
            : total === 0
              ? "açık pozisyon yok"
              : `${formatInt(total)} açık pozisyon`}
        </span>
      </div>
      <div className="dist-bar" aria-hidden>
        <span className="dist-seg is-early" style={{ width: seg(a) }} />
        <span className="dist-seg is-mid" style={{ width: seg(b) }} />
        <span className="dist-seg is-over" style={{ width: seg(c) }} />
      </div>
      <div className="dist-legend">
        <span className="dist-key is-early">
          &lt;25 dk <b>{formatInt(under25)}</b>
        </span>
        <span className="dist-key is-mid">
          25–60 dk <b>{formatInt(mid)}</b>
        </span>
        <span className="dist-key is-over">
          &gt;60 dk <b>{formatInt(over60)}</b>
        </span>
      </div>
    </div>
  );
}

/**
 * The portfolio layer.
 *
 * V2 removed the 1–3 concurrent-position cap, so the question stopped being
 * "how many are open" and became "how much of the account is at work, at what
 * modelled risk, and is that risk actually one position". Those are different
 * readings and they are shown as different readings — position size is not
 * risk, and this panel never conflates them.
 */
export function Portfolio({
  portfolio,
  loading,
}: {
  portfolio: PortfolioState | null | undefined;
  loading: boolean;
}) {
  const p = portfolio ?? null;

  const corr = toNum(p?.correlated_exposure_pct);
  const band = corr === null ? null : correlationBand(corr);
  const barWidth = corr === null ? 0 : Math.max(0, Math.min(100, corr));

  const meta =
    p && (p.allocation_mode || p.net_tp_profile) ? (
      <>
        {p.allocation_mode && (
          <span className="pill" title="Sermaye dağıtım modu">
            {allocationModeLabel(p.allocation_mode)}
          </span>
        )}
        {p.net_tp_profile && (
          <span className="pill" title="Aktif NET kâr-al profili">
            {netTpProfileLabel(p.net_tp_profile)}
          </span>
        )}
      </>
    ) : null;

  return (
    <Panel
      idx="09"
      title="Portföy"
      note="sermaye, risk ve yoğunlaşma"
      meta={meta}
      footer="Pozisyon büyüklüğü risk değildir: %1 stop taşıyan $20'lık bir pozisyon $20 değil, yaklaşık $0,20 artı maliyet riske eder. Toplam açık risk bu modellenmiş kayıpların toplamıdır."
    >
      {loading && !p ? (
        <Skeleton rows={4} />
      ) : !p ? (
        <DataState
          title="Portföy katmanı henüz raporlamıyor"
          text="Worker V2 portföy alanlarını üretmeye başladığında dağıtılan sermaye, açık risk, korelasyonlu maruziyet ve yaş dağılımı burada görünecek. Boş görünmesi bir hata değil; ölçülmemiş bir değeri sıfır olarak göstermemek kasıtlıdır."
        />
      ) : (
        <div className="pfolio">
          <div className={`corr${band ? ` ${band.cls}` : " is-empty"}`}>
            <div className="corr-head">
              <span className="corr-k">Korelasyonlu maruziyet</span>
              <span className="corr-v">{corr === null ? DASH : formatPct(corr)}</span>
            </div>
            <div className="corr-track" aria-hidden>
              <span className="corr-fill" style={{ width: `${barWidth}%` }} />
              <span className="corr-mark" style={{ left: "40%" }} />
              <span className="corr-mark is-hard" style={{ left: "70%" }} />
            </div>
            <p className="corr-note">
              {band
                ? band.text
                : "Ölçüm yok. Sekiz eşzamanlı kripto long'u tek bir büyük kripto long'udur; bu değer o riski yakalamak içindir."}
            </p>
          </div>

          <div className="pfolio-grid">
            <Stat
              label="Açık pozisyon"
              value={formatInt(p.open_positions)}
              note="adet"
            />
            <Stat
              label="Kullanılan sermaye"
              value={formatMoney(p.capital_deployed)}
              note="piyasada çalışan tutar"
            />
            <Stat
              label="Kullanılabilir nakit"
              value={formatMoney(p.available_cash)}
              note="yeni pozisyon açılabilir"
            />
            <Stat
              label="Nakit rezervi"
              value={formatMoney(p.cash_reserve)}
              note="kasıtlı olarak kullanılmaz"
            />
            <Stat
              label="Toplam açık risk"
              value={
                p.open_risk_usd === null || p.open_risk_usd === undefined
                  ? DASH
                  : formatMoney(-Math.abs(p.open_risk_usd))
              }
              cls={p.open_risk_usd ? "neg" : ""}
              note="stoplar dolarsa modellenen kayıp"
            />
            <Stat
              label="Açık risk / sermaye"
              value={formatPct(p.open_risk_pct)}
              cls={
                toNum(p.open_risk_pct) !== null && (toNum(p.open_risk_pct) as number) > 3
                  ? "warn"
                  : ""
              }
              note="bütçe %3"
            />
            <Stat
              label="Ort. pozisyon"
              value={formatMoney(p.avg_position_usd)}
              note="tutar"
            />
            <Stat
              label="Medyan pozisyon"
              value={formatMoney(p.median_position_usd)}
              note="ortalamayı bozan uç değerler için"
            />
            <Stat
              label="Ort. tutma süresi"
              value={formatDuration(p.avg_holding_seconds)}
              note="hedef 25–60 dk"
            />
            <Stat
              label="Günlük sermaye devri"
              value={formatRatio(p.capital_turnover_per_day)}
              note="işlem hacmi / sermaye"
            />
            <Stat
              label="$1 başına NET kâr"
              value={formatMicroMoney(p.net_profit_per_dollar_deployed)}
              cls={tone(p.net_profit_per_dollar_deployed) ?? ""}
              note="dağıtılan sermaye başına"
            />
          </div>

          <Distribution
            under25={p.positions_under_25m}
            mid={p.positions_25_60m}
            over60={p.positions_over_60m}
          />
        </div>
      )}
    </Panel>
  );
}
