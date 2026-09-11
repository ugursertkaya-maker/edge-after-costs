"use client";

import { useEffect, useMemo, useState } from "react";
import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import {
  DASH,
  baseAsset,
  familyLabel,
  formatBpsAbs,
  formatDuration,
  formatFactor,
  formatHorizon,
  formatInt,
  formatMicroMoney,
  formatMoney,
  formatRate,
  formatRelativeMs,
  formatSignedMicroMoney,
  oosStatusLabel,
  oosStatusTone,
  regimeLabel,
  toNum,
  tone,
} from "@/lib/format";
import type { Num, SampleStats, TournamentArm, TournamentSummary } from "@/lib/types";

const PAGE_SIZE = 50;

/**
 * The research record.
 *
 * One rule drives every design decision in this panel: **an in-sample number
 * is not a result.** It is the number the parameters were chosen on, and a
 * strategy search that reports it as a finding is the exact failure this
 * project exists to guard against. So the three readings never share a column
 * group, never share a tone, and never share a heading:
 *
 *   ÖRNEKLEM DIŞI   — the only half a promotion decision may read. Placed
 *                     first, immediately after the candidate's identity, so it
 *                     is what is on screen before any horizontal scrolling.
 *   ÖRNEKLEM İÇİ    — dimmed and hatched, under a heading that says in words
 *                     that it is not a result.
 *   TÜM ÖRNEKLEM    — the raw log of everything the arm did, kept because the
 *                     brief requires every arm tested to appear, losers
 *                     included, and dropped arms are how a search lies.
 */

type Group = "id" | "oos" | "is" | "all";

interface Col {
  key: string;
  label: string;
  group: Group;
  /** Header title — says what the column is, in one line. */
  hint?: string;
  left?: boolean;
  /** Sticky first column, so identity survives a sideways scroll. */
  stick?: boolean;
  sort?: (a: TournamentArm) => number | null;
  render: (a: TournamentArm) => { text: string; cls?: string; title?: string };
}

const GROUPS: { group: Group; label: string; note: string }[] = [
  { group: "id", label: "Aday", note: "aile · coin · rejim · ufuk" },
  {
    group: "oos",
    label: "Örneklem dışı — karar bu sütunlarda",
    note: "parametreler bu veriye bakılmadan donduruldu",
  },
  {
    group: "is",
    label: "Örneklem içi — sonuç değildir",
    note: "parametreler bu veride seçildi; burada iyi görünmek zorundadır",
  },
  {
    group: "all",
    label: "Tüm örneklem — ham araştırma kaydı",
    note: "in + out birlikte; sıralama ölçütü değildir",
  },
];

const IS_TITLE =
  "Örneklem içi: parametreler bu veri üzerinde seçildi. Bu bir sonuç değil, uyumun kendisidir.";
const OOS_TITLE = "Örneklem dışı: parametreler donduruldu, bu veri hiç görülmedi.";

function sampleCell(
  pick: (s: SampleStats) => Num,
  get: (a: TournamentArm) => SampleStats | null | undefined,
): (a: TournamentArm) => Num {
  return (a) => {
    const s = get(a);
    if (!s) return null;
    const v = pick(s);
    return v === undefined ? null : v;
  };
}

const inS = (a: TournamentArm) => a.in_sample;
const outS = (a: TournamentArm) => a.out_of_sample;

/** Trades / PF / expectancy / net, for one half of the walk-forward split. */
function splitColumns(group: Group, get: (a: TournamentArm) => SampleStats | null | undefined): Col[] {
  const title = group === "is" ? IS_TITLE : OOS_TITLE;
  const trades = sampleCell((s) => s.trades ?? null, get);
  const pf = sampleCell((s) => s.profit_factor ?? null, get);
  const exp = sampleCell((s) => s.expectancy ?? null, get);
  const net = sampleCell((s) => s.net_pnl ?? null, get);
  const dim = group === "is";

  return [
    {
      key: `${group}.trades`,
      label: "İşlem",
      group,
      hint: title,
      sort: trades,
      render: (a) => ({ text: formatInt(trades(a)), cls: dim ? "dim" : "", title }),
    },
    {
      key: `${group}.pf`,
      label: "PF",
      group,
      hint: `Profit factor · ${title}`,
      sort: pf,
      render: (a) => {
        const v = toNum(pf(a));
        return {
          text: formatFactor(pf(a)),
          // The 1.20 bar is a gate, so out-of-sample it is drawn as one. In
          // sample it is drawn as nothing, because it decides nothing.
          cls: dim ? "dim" : v === null ? "muted" : v >= 1.2 ? "pos" : v >= 1 ? "warn" : "neg",
          title,
        };
      },
    },
    {
      key: `${group}.expectancy`,
      label: "Expectancy",
      group,
      hint: `İşlem başına NET beklenti · ${title}`,
      sort: exp,
      render: (a) => ({
        text: formatSignedMicroMoney(exp(a)),
        cls: dim ? "dim" : (tone(exp(a)) ?? "muted"),
        title,
      }),
    },
    {
      key: `${group}.net`,
      label: "NET P&L",
      group,
      hint: `Maliyet sonrası net · ${title}`,
      sort: net,
      render: (a) => ({
        text: formatSignedMicroMoney(net(a)),
        cls: dim ? "dim" : `net ${tone(net(a)) ?? "muted"}`,
        title,
      }),
    },
  ];
}

const COLUMNS: Col[] = [
  {
    key: "family",
    label: "Strateji",
    group: "id",
    left: true,
    stick: true,
    render: (a) => ({
      text: a.family ? familyLabel(a.family) : DASH,
      cls: a.family ? "sym" : "muted",
      title: [
        a.key ? `anahtar: ${a.key}` : null,
        a.exit_profile ? `çıkış profili: ${a.exit_profile}` : null,
        a.stop_profile ? `stop profili: ${a.stop_profile}` : null,
      ]
        .filter(Boolean)
        .join(" · "),
    }),
  },
  {
    key: "symbol",
    label: "Coin",
    group: "id",
    left: true,
    render: (a) => ({
      text: a.symbol ? baseAsset(a.symbol) : DASH,
      cls: a.symbol ? "sym" : "muted",
      title: a.symbol ?? undefined,
    }),
  },
  {
    key: "regime",
    label: "Rejim",
    group: "id",
    left: true,
    render: (a) => ({
      text: a.regime ? regimeLabel(a.regime) : "TÜM REJİMLER",
      cls: a.regime ? "dim" : "muted",
      title: a.regime ?? "Rejime göre ayrılmamış aday",
    }),
  },
  {
    key: "horizon",
    label: "Tutma ufku",
    group: "id",
    hint: "Pozisyonun hedeflenen tutma süresi",
    sort: (a) => toNum(a.horizon_seconds),
    render: (a) => ({ text: formatHorizon(a.horizon_seconds), cls: "dim" }),
  },

  ...splitColumns("oos", outS),
  {
    key: "oos_status",
    label: "OOS durumu",
    group: "oos",
    hint: "Örneklem dışı terfi kapılarına göre verilen hüküm",
    render: () => ({ text: "" }),
  },

  ...splitColumns("is", inS),

  {
    key: "trades",
    label: "İşlem",
    group: "all",
    sort: (a) => toNum(a.trades),
    render: (a) => ({ text: formatInt(a.trades) }),
  },
  {
    key: "win_rate",
    label: "Win %",
    group: "all",
    sort: (a) => toNum(a.win_rate),
    render: (a) => ({ text: formatRate(a.win_rate), cls: "dim" }),
  },
  {
    key: "avg_win",
    label: "Ort. kazanç",
    group: "all",
    sort: (a) => toNum(a.avg_win),
    render: (a) => ({
      text: formatMicroMoney(a.avg_win),
      cls: toNum(a.avg_win) ? "pos" : "muted",
    }),
  },
  {
    key: "avg_loss",
    label: "Ort. kayıp",
    group: "all",
    sort: (a) => toNum(a.avg_loss),
    render: (a) => {
      const v = toNum(a.avg_loss);
      return {
        // Stored as a magnitude; printed unsigned next to a win column it
        // would read as a gain.
        text: v === null ? DASH : formatMicroMoney(-Math.abs(v)),
        cls: v ? "neg" : "muted",
      };
    },
  },
  {
    key: "profit_factor",
    label: "PF",
    group: "all",
    sort: (a) => toNum(a.profit_factor),
    render: (a) => ({ text: formatFactor(a.profit_factor), cls: "dim" }),
  },
  {
    key: "expectancy",
    label: "Expectancy",
    group: "all",
    sort: (a) => toNum(a.expectancy),
    render: (a) => ({
      text: formatSignedMicroMoney(a.expectancy),
      cls: tone(a.expectancy) ?? "muted",
    }),
  },
  {
    key: "fees",
    label: "Komisyon",
    group: "all",
    sort: (a) => toNum(a.fees),
    render: (a) => ({ text: formatMicroMoney(a.fees), cls: "dim" }),
  },
  {
    key: "slippage",
    label: "Kayma",
    group: "all",
    hint: "V1'de her kaybı domine eden kalem komisyon değil kaymaydı",
    sort: (a) => toNum(a.slippage),
    render: (a) => ({ text: formatMicroMoney(a.slippage), cls: "dim" }),
  },
  {
    key: "net_pnl",
    label: "NET P&L",
    group: "all",
    sort: (a) => toNum(a.net_pnl),
    render: (a) => ({
      text: formatSignedMicroMoney(a.net_pnl),
      cls: tone(a.net_pnl) ?? "muted",
    }),
  },
  {
    key: "max_drawdown",
    label: "Max DD",
    group: "all",
    sort: (a) => toNum(a.max_drawdown),
    render: (a) => {
      const v = toNum(a.max_drawdown);
      return {
        text: v === null ? DASH : formatMoney(-Math.abs(v)),
        cls: v ? "neg" : "muted",
      };
    },
  },
];

/** Default order: out-of-sample NET expectancy per trade — the objective
    function named in the Phase 3 brief. Profit factor and drawdown are gates
    in the FİNALİSTLER panel, not terms in this ranking. */
const DEFAULT_SORT = "oos.expectancy";

const ALL = "__all__";

function options(arms: TournamentArm[], pick: (a: TournamentArm) => string | null | undefined) {
  const seen = new Set<string>();
  for (const a of arms) {
    const v = pick(a);
    if (v) seen.add(v);
  }
  return Array.from(seen).sort();
}

function Select({
  label,
  value,
  onChange,
  items,
  render,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  items: string[];
  render: (v: string) => string;
}) {
  return (
    <label className="sel">
      <span className="sel-k">{label}</span>
      <select
        className="sel-input"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={items.length === 0}
      >
        <option value={ALL}>{items.length === 0 ? "—" : "hepsi"}</option>
        {items.map((it) => (
          <option key={it} value={it}>
            {render(it)}
          </option>
        ))}
      </select>
    </label>
  );
}

function StatusBadge({ status }: { status: string | null | undefined }) {
  if (!status) return <span className="muted">{DASH}</span>;
  const key = String(status).toUpperCase();
  return (
    <span className={`oos ${oosStatusTone(key)}`} title={key}>
      {oosStatusLabel(key)}
    </span>
  );
}

export function Tournament({
  tournament,
  loading,
  now,
}: {
  tournament: TournamentSummary | null | undefined;
  loading: boolean;
  now: number;
}) {
  const [sortKey, setSortKey] = useState<string>(DEFAULT_SORT);
  const [coin, setCoin] = useState(ALL);
  const [family, setFamily] = useState(ALL);
  const [regime, setRegime] = useState(ALL);
  const [status, setStatus] = useState(ALL);
  const [page, setPage] = useState(0);

  const arms = useMemo(() => tournament?.arms ?? null, [tournament]);

  const coins = useMemo(() => (arms ? options(arms, (a) => a.symbol) : []), [arms]);
  const families = useMemo(() => (arms ? options(arms, (a) => a.family) : []), [arms]);
  const regimes = useMemo(() => (arms ? options(arms, (a) => a.regime) : []), [arms]);
  const statuses = useMemo(() => (arms ? options(arms, (a) => a.oos_status) : []), [arms]);

  const filtered = useMemo(() => {
    if (!arms) return null;
    return arms.filter(
      (a) =>
        (coin === ALL || a.symbol === coin) &&
        (family === ALL || a.family === family) &&
        (regime === ALL || a.regime === regime) &&
        (status === ALL || a.oos_status === status),
    );
  }, [arms, coin, family, regime, status]);

  const sorted = useMemo(() => {
    if (!filtered) return null;
    const col = COLUMNS.find((c) => c.key === sortKey);
    const read = col?.sort;
    if (!read) return filtered;
    return [...filtered].sort((a, b) => {
      const av = read(a);
      const bv = read(b);
      // An unmeasured arm is not "the worst" — it is not yet comparable, so it
      // sinks regardless of direction rather than being ranked as a loser.
      if (av === null || av === undefined) return 1;
      if (bv === null || bv === undefined) return -1;
      return bv - av;
    });
  }, [filtered, sortKey]);

  const total = sorted?.length ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  useEffect(() => {
    if (page > pages - 1) setPage(0);
  }, [page, pages]);
  const slice = sorted?.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE) ?? [];

  const tally = useMemo(() => {
    const t: Record<string, number> = {};
    for (const a of filtered ?? []) {
      const k = a.oos_status ? String(a.oos_status) : "UNKNOWN";
      t[k] = (t[k] ?? 0) + 1;
    }
    return t;
  }, [filtered]);

  const filtersOn = coin !== ALL || family !== ALL || regime !== ALL || status !== ALL;
  const reset = () => {
    setCoin(ALL);
    setFamily(ALL);
    setRegime(ALL);
    setStatus(ALL);
  };

  const meta = tournament ? (
    <>
      {toNum(tournament.candidates_evaluated) !== null && (
        <span className="pill" title="Değerlendirilen aday sayısı">
          {formatInt(tournament.candidates_evaluated)} aday
        </span>
      )}
      {tournament.period && (
        <span className="panel-note" title="Aramanın koştuğu veri penceresi">
          {tournament.period}
        </span>
      )}
      {toNum(tournament.fee_bps) !== null && (
        <span className="panel-note" title="Tüm arama bu komisyon varsayımıyla maliyetlendirildi">
          komisyon {formatBpsAbs(tournament.fee_bps)} bps
        </span>
      )}
      {toNum(tournament.wall_clock_seconds) !== null && (
        <span className="panel-note">{formatDuration(tournament.wall_clock_seconds)} arama</span>
      )}
      {toNum(tournament.generated_ms) !== null && (
        <span className="panel-note">{formatRelativeMs(tournament.generated_ms, now)}</span>
      )}
    </>
  ) : null;

  return (
    <Panel
      idx="10"
      title="Strateji turnuvası"
      note="her denenen aday · kaybedenler dahil"
      meta={meta}
      flush
      footer="Sıralama varsayılan olarak ÖRNEKLEM DIŞI expectancy'ye göre yapılır; Phase 3'ün amaç fonksiyonu budur. Örneklem içi sütunlar parametrelerin seçildiği veriden gelir ve orada iyi görünmek zorundadırlar — bu yüzden sönük çizilir ve hiçbir kararın dayanağı değildir. Kaybeden adaylar tablodan çıkarılmaz: bir aramanın yalan söylemesinin en kolay yolu, denediklerini göstermemesidir."
    >
      {loading && !arms ? (
        <Skeleton rows={6} />
      ) : !tournament ? (
        <DataState
          title="Turnuva henüz çalışmadı"
          text="Arama, sunucuya yük bindirmemek için önbelleğe alınmış veri üzerinde ayrıca koşar; sonuç üretildiğinde denenen her aday burada örneklem içi ve örneklem dışı karnesiyle listelenecek. Panelin boş olması bir hata değildir — ölçülmemiş bir sonuç sıfır olarak gösterilmez."
        />
      ) : !arms || arms.length === 0 ? (
        <DataState
          title="Değerlendirilmiş aday yok"
          text={
            toNum(tournament.candidates_evaluated) !== null
              ? `Turnuva kaydı ${formatInt(tournament.candidates_evaluated)} aday bildiriyor ancak tabloya hiçbir satır gelmedi. Bu bir veri eksiği olarak gösterilir, boş bir sonuç olarak değil.`
              : "Turnuva kaydı var ama içinde hiçbir aday yok. Bu bir arama sonucu değil, eksik bir çıktıdır."
          }
        />
      ) : (
        <>
          <div className="tools">
            <Select
              label="Coin"
              value={coin}
              onChange={(v) => {
                setCoin(v);
                setPage(0);
              }}
              items={coins}
              render={baseAsset}
            />
            <Select
              label="Aile"
              value={family}
              onChange={(v) => {
                setFamily(v);
                setPage(0);
              }}
              items={families}
              render={familyLabel}
            />
            <Select
              label="Rejim"
              value={regime}
              onChange={(v) => {
                setRegime(v);
                setPage(0);
              }}
              items={regimes}
              render={regimeLabel}
            />
            <Select
              label="OOS durumu"
              value={status}
              onChange={(v) => {
                setStatus(v);
                setPage(0);
              }}
              items={statuses}
              render={oosStatusLabel}
            />
            {filtersOn && (
              <button type="button" className="btn" onClick={reset}>
                Filtreleri temizle
              </button>
            )}
            <span className="tools-spacer" />
            <span className="tally">
              {["FINALIST", "MARGINAL", "FAILED", "INSUFFICIENT_SAMPLE"].map((k) =>
                tally[k] ? (
                  <span key={k} className={`oos ${oosStatusTone(k)}`}>
                    {oosStatusLabel(k)} {tally[k]}
                  </span>
                ) : null,
              )}
              <span className="tally-n">
                {formatInt(total)} / {formatInt(arms.length)} aday
              </span>
            </span>
          </div>

          {total === 0 ? (
            <DataState
              title="Filtreye uyan aday yok"
              text="Seçili coin, aile, rejim ve durum birleşiminde denenmiş bir aday bulunmuyor. Bu, hiçbir adayın olmadığı anlamına gelmez — filtreleri temizleyin."
            />
          ) : (
            <>
              <div className="tbl-wrap">
                <table className="tbl tbl-tour">
                  <thead>
                    <tr className="grp">
                      {GROUPS.map((g) => {
                        const span = COLUMNS.filter((c) => c.group === g.group).length;
                        if (span === 0) return null;
                        return (
                          <th
                            key={g.group}
                            className={`gh g-${g.group}`}
                            colSpan={span}
                            title={g.note}
                          >
                            {g.label}
                          </th>
                        );
                      })}
                    </tr>
                    <tr className="cols">
                      {COLUMNS.map((c) => (
                        <th
                          key={c.key}
                          className={[
                            c.left ? "l" : "",
                            c.stick ? "stick" : "",
                            `c-${c.group}`,
                            c.sort ? "sortable" : "",
                          ]
                            .filter(Boolean)
                            .join(" ")}
                          title={c.hint ?? (c.sort ? `${c.label} sütununa göre sırala` : undefined)}
                          onClick={c.sort ? () => setSortKey(c.key) : undefined}
                        >
                          {c.label}
                          {sortKey === c.key && <span className="caret">▾</span>}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {slice.map((a, i) => (
                      <tr key={a.key ?? `${a.family}-${a.symbol}-${a.regime}-${i}`}>
                        {COLUMNS.map((c) => {
                          if (c.key === "oos_status") {
                            return (
                              <td key={c.key} className="c c-oos">
                                <StatusBadge status={a.oos_status} />
                              </td>
                            );
                          }
                          const { text, cls, title } = c.render(a);
                          return (
                            <td
                              key={c.key}
                              className={[
                                c.left ? "l" : "",
                                c.stick ? "stick" : "",
                                `c-${c.group}`,
                                cls ?? "",
                              ]
                                .filter(Boolean)
                                .join(" ")}
                              title={title || undefined}
                            >
                              {text}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {pages > 1 && (
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
                    {page * PAGE_SIZE + 1}–{Math.min(total, (page + 1) * PAGE_SIZE)} /{" "}
                    {formatInt(total)} aday · sayfa {page + 1}/{pages}
                  </span>
                </div>
              )}
            </>
          )}
        </>
      )}
    </Panel>
  );
}
