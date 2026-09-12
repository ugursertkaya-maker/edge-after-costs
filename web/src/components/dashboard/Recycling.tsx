import { Panel } from "@/components/ui/Panel";
import { Skeleton } from "@/components/ui/DataState";
import { Stat } from "@/components/dashboard/bits";
import {
  DASH,
  RECYCLE_BAD_MS,
  RECYCLE_FAST_MS,
  RECYCLE_SLOW_MS,
  baseAsset,
  formatClockMs,
  formatDuration,
  formatExactMs,
  formatInt,
  formatMoney,
  formatSeconds,
  recycleTone,
  strategyLabel,
  toNum,
} from "@/lib/format";
import type { CapitalState, PortfolioState, RecycleEvent, RecyclingState } from "@/lib/types";

/**
 * SERMAYE DEVİR ZİNCİRİ — §22.
 *
 * One question: when a position closes, how long before that money is working
 * again? For a strategy whose median hold is ~32 seconds, a 40-second gap
 * between the close and the next entry halves the account's turnover without
 * taking one extra unit of risk — the delay costs more than most of the
 * trades earn. So the chain is drawn as a chain, in order, with the two
 * intervals the brief names printed at the precision they are measured at:
 * close→rescan in MILLISECONDS, close→next-entry in SECONDS.
 *
 * The one reading this panel refuses to blur: `qualified_candidate_existed`.
 * A long gap with no qualified candidate is NO_QUALIFIED_SIGNAL — the market
 * had nothing, and the recycler is innocent. A long gap WITH a qualified
 * candidate waiting is a recycling defect. They produce the same idle balance
 * and they are opposite findings, so the chain states which one it is rather
 * than printing one duration and letting the reader guess.
 *
 * The idle-capital reasons are NOT repeated here. There is exactly one
 * attribution on this screen and it lives in §02; this panel links to it and
 * shows the balance, because the same idle dollar explained twice in two
 * places is how a dashboard starts disagreeing with itself.
 */

const STEPS: {
  key: keyof RecycleEvent;
  label: string;
  note: string;
}[] = [
  { key: "closed_ms", label: "Pozisyon kapandı", note: "Turun bittiği an" },
  {
    key: "capital_released_ms",
    label: "Sermaye serbest",
    note: "Muhasebenin parayı yeniden kullanılabilir saydığı an",
  },
  {
    key: "rescan_triggered_ms",
    label: "Yeniden tarama",
    note: "Tarayıcının yeni aday araması için tetiklendiği an",
  },
  { key: "next_entry_ms", label: "Sonraki giriş", note: "Paranın yeniden piyasaya girdiği an" },
];

/** One interval, with its own tone band. The bands are set against the hold,
    not against a round number — see `recycleTone` in `lib/format.ts`. */
function Interval({
  label,
  value,
  text,
  hint,
}: {
  label: string;
  value: number | null;
  text: string;
  hint: string;
}) {
  const cls = recycleTone(value);
  return (
    <div className={`rcy-int ${cls}`} title={hint}>
      <span className="rcy-int-k">{label}</span>
      <span className={`rcy-int-v${value === null ? " is-empty" : ""}`}>{text}</span>
      <span className="rcy-int-n">{hint}</span>
    </div>
  );
}

function Chain({ ev, now }: { ev: RecycleEvent; now: number }) {
  const closed = toNum(ev.closed_ms);
  const marks = STEPS.map((s) => ({ ...s, ms: toNum(ev[s.key] as number | null | undefined) }));

  return (
    <ol className="rcy-chain">
      {marks.map((m, i) => {
        const prev = i === 0 ? null : marks[i - 1].ms;
        const delta = m.ms !== null && prev !== null ? m.ms - prev : null;
        const pending = m.ms === null;
        return (
          <li key={String(m.key)} className={`rcy-step${pending ? " is-pending" : ""}`}>
            <span className="rcy-dot" aria-hidden />
            <span className="rcy-k">{m.label}</span>
            <span className={`rcy-v${pending ? " is-empty" : ""}`}>
              {pending ? DASH : formatClockMs(m.ms)}
            </span>
            <span className="rcy-d">
              {delta === null ? (
                i === 0 ? (
                  ""
                ) : (
                  <span className="muted">{DASH}</span>
                )
              ) : (
                `+${formatExactMs(delta)}`
              )}
            </span>
            <span className="rcy-n">{m.note}</span>
          </li>
        );
      })}
      {toNum(ev.next_entry_ms) === null && closed !== null && (
        <li className="rcy-open">
          Bu zincir henüz kapanmadı: kapanıştan bu yana{" "}
          <b>{formatDuration((now - closed) / 1000)}</b> geçti ve yeni bir giriş olmadı.{" "}
          {ev.qualified_candidate_existed === true
            ? "Sermaye serbest kaldığında nitelikli bir aday VARDI — bu durumda gecikme piyasanın değil, devir mekanizmasının hanesine yazılır."
            : ev.qualified_candidate_existed === false
              ? "Sermaye serbest kaldığında nitelikli aday YOKTU — gecikme burada NO_QUALIFIED_SIGNAL'dır, bir devir kusuru değildir."
              : "Nitelikli aday olup olmadığı bildirilmedi; bu bilgi olmadan gecikmenin piyasadan mı yoksa mekanizmadan mı geldiği söylenemez."}
        </li>
      )}
    </ol>
  );
}

export function Recycling({
  recycling,
  capital,
  portfolio,
  loading,
  now,
}: {
  recycling: RecyclingState | null | undefined;
  capital: CapitalState | null | undefined;
  portfolio: PortfolioState | null | undefined;
  loading: boolean;
  now: number;
}) {
  const r = recycling ?? null;
  const last = r?.last ?? r?.events?.[0] ?? null;

  const toRescan = toNum(last?.close_to_rescan_ms) ?? deltaOf(last?.closed_ms, last?.rescan_triggered_ms);
  const toEntryMs = toNum(last?.close_to_entry_ms) ?? deltaOf(last?.closed_ms, last?.next_entry_ms);

  // Cash is read from the recycling block when it serves one and from the
  // capital / portfolio layers when it does not. All three name the same
  // dollars, so preferring one and saying which is better than showing three.
  const cash =
    toNum(r?.available_cash_usd) ??
    toNum(capital?.idle_usd) ??
    toNum(portfolio?.available_cash) ??
    null;
  const cashSource = toNum(r?.available_cash_usd) !== null
    ? "devir bloğu"
    : toNum(capital?.idle_usd) !== null
      ? "§02 boşta bakiye"
      : toNum(portfolio?.available_cash) !== null
        ? "§09 kullanılabilir nakit"
        : null;

  const idleSeconds =
    toNum(r?.idle_seconds) ??
    (toNum(last?.capital_released_ms) !== null && toNum(last?.next_entry_ms) === null
      ? (now - (toNum(last?.capital_released_ms) as number)) / 1000
      : null);

  const qualified = toNum(r?.qualified_opportunities) ?? toNum(capital?.qualified_opportunities);
  const sample = toNum(r?.closes) ?? toNum(r?.sample) ?? (r?.events?.length ?? null);
  // Closes where a qualified candidate was waiting and the capital still sat.
  // This is the only count that separates "the market had nothing" from "we
  // failed to redeploy", so it is a headline, not a table column.
  const notTaken = toNum(r?.qualified_but_not_taken);
  const stateBug = toNum(last?.state_bug_usd);

  // `RecycleEvent.as_dict()` names these `symbol` and `next_entry_symbol`;
  // both spellings are accepted so the panel does not depend on which one the
  // worker ends up projecting.
  const closedSymbol = last?.closed_symbol ?? last?.symbol ?? null;
  const nextSymbol = last?.next_symbol ?? last?.next_entry_symbol ?? null;

  const meta = (
    <>
      <span className="pill is-off" title="Medyanların hesaplandığı kapanış sayısı">
        {sample === null ? "örneklem yok" : `${formatInt(sample)} kapanış`}
      </span>
      <span className="pill is-pos" title="Tüm devir zinciri simülasyondur.">
        PAPER
      </span>
    </>
  );

  return (
    <Panel
      idx="19"
      title="Sermaye devir zinciri"
      note="kapanış → serbest kalma → yeniden tarama → sonraki giriş"
      meta={meta}
      footer={
        "KAPANIŞ→TARAMA milisaniye, KAPANIŞ→GİRİŞ saniye cinsindendir; medyan sıralamaları da eşlik eden kapanış sayısıyla birlikte okunur, tek bir zincir bir dağılım değildir. Boşta sermayenin SEBEP dağılımı bu panelde tekrarlanmaz: bu ekranda tek bir ayrıştırma vardır ve §02'dedir — aynı doları iki yerde ayrı ayrı açıklamak, panonun kendisiyle çelişmesinin yoludur. " +
        `Eşikler tutma süresine göredir: ${formatExactMs(RECYCLE_FAST_MS)} altı anlık, ${formatExactMs(RECYCLE_SLOW_MS)} altı kabul edilebilir, ${formatExactMs(RECYCLE_BAD_MS)} üstü medyan tutma süresinden uzun bir gecikmedir ve çoğu turun kazancından fazlasına mal olur.`
      }
    >
      {loading && !r ? (
        <Skeleton rows={5} />
      ) : (
        <div className="rcy">
          <div className="rcy-ints">
            <Interval
              label="Kapanış → yeniden tarama"
              value={toRescan}
              text={formatExactMs(toRescan)}
              hint="Para serbest kaldıktan sonra tarayıcının sorulma süresi"
            />
            <Interval
              label="Kapanış → sonraki giriş"
              value={toEntryMs}
              text={toEntryMs === null ? DASH : formatSeconds(toEntryMs / 1000)}
              hint="Paranın yeniden piyasada olması için geçen süre"
            />
            <Interval
              label="Medyan kapanış → tarama"
              value={toNum(r?.median_close_to_rescan_ms)}
              text={formatExactMs(r?.median_close_to_rescan_ms ?? null)}
              hint={sample === null ? "örneklem bildirilmedi" : `${formatInt(sample)} kapanış üzerinden`}
            />
            <Interval
              label="Medyan kapanış → giriş"
              value={toNum(r?.median_close_to_entry_ms)}
              text={
                toNum(r?.median_close_to_entry_ms) === null
                  ? DASH
                  : formatSeconds((toNum(r?.median_close_to_entry_ms) as number) / 1000)
              }
              hint={sample === null ? "örneklem bildirilmedi" : `${formatInt(sample)} kapanış üzerinden`}
            />
          </div>

          <div className="pfolio-grid">
            <Stat
              label="Kullanılabilir nakit"
              value={formatMoney(cash)}
              note={cashSource ? `kaynak: ${cashSource}` : "bildirilmedi"}
              cls={cash === null ? "flat" : undefined}
              title="Şu anda yeni bir pozisyona girebilecek nakit"
            />
            <Stat
              label="Boşta kalma süresi"
              value={idleSeconds === null ? DASH : formatDuration(idleSeconds)}
              note={idleSeconds === null ? "ölçülmedi" : "son serbest kalmadan bu yana"}
              cls={idleSeconds === null ? "flat" : idleSeconds > 60 ? "warn" : undefined}
            />
            <Stat
              label="Nitelikli fırsat"
              value={formatInt(qualified)}
              note={qualified === null ? "bildirilmedi" : "bu turda kapıları geçen aday"}
              cls={qualified === null ? "flat" : qualified > 0 ? "pos" : undefined}
              title="Sermaye serbestken kaç aday tüm kapıları geçmişti"
            />
            <Stat
              label="Kapanan son tur"
              value={closedSymbol ? baseAsset(closedSymbol) : DASH}
              note={
                last?.closed_trade_id
                  ? `tur ${last.closed_trade_id.slice(0, 8)}`
                  : "kapanış kaydı yok"
              }
              cls={closedSymbol ? undefined : "flat"}
            />
            <Stat
              label="Sonraki nitelikli aday"
              value={
                nextSymbol
                  ? baseAsset(nextSymbol)
                  : last?.qualified_candidate_existed === false
                    ? "YOKTU"
                    : DASH
              }
              note={
                last?.next_strategy
                  ? strategyLabel(last.next_strategy)
                  : last?.qualified_candidate_existed === false
                    ? "serbest kalma anında aday yoktu"
                    : "bildirilmedi"
              }
              cls={nextSymbol ? undefined : "flat"}
            />
            <Stat
              label="Nitelikli ama alınmadı"
              value={formatInt(notTaken)}
              note={
                notTaken === null
                  ? "bildirilmedi"
                  : notTaken > 0
                    ? "kapanışta aday vardı, sermaye yine de bekledi"
                    : "böyle bir kapanış olmadı"
              }
              cls={notTaken === null ? "flat" : notTaken > 0 ? "neg" : "pos"}
              title="Bu sayı sıfırdan büyükse gecikme piyasanın değil, devir mekanizmasının hanesindedir"
            />
            <Stat
              label="Mutabakatsız nakit"
              value={stateBug === null ? DASH : formatMoney(stateBug)}
              note={
                stateBug === null
                  ? "bildirilmedi"
                  : stateBug > 0
                    ? "CAPITAL_STATE_BUG — §02'de de görünür"
                    : "nakit, maruziyet ve özkaynak uyuşuyor"
              }
              cls={stateBug === null ? "flat" : stateBug > 0 ? "neg" : undefined}
              title="Nakit, maruziyet ve özkaynağın uyuşmadığı tutar. Bir kusurdur, piyasa okuması değildir."
            />
            <Stat
              label="Aday var mıydı"
              value={
                last?.qualified_candidate_existed === true
                  ? "VARDI"
                  : last?.qualified_candidate_existed === false
                    ? "YOKTU"
                    : DASH
              }
              note="uzun gecikmenin kusur mu piyasa mı olduğunu ayıran tek alan"
              cls={
                last?.qualified_candidate_existed === true && toEntryMs !== null && toEntryMs > RECYCLE_BAD_MS
                  ? "neg"
                  : last?.qualified_candidate_existed === undefined ||
                      last?.qualified_candidate_existed === null
                    ? "flat"
                    : undefined
              }
            />
          </div>

          {last ? (
            <Chain ev={last} now={now} />
          ) : (
            <div className="fin-empty">
              <div className="fin-empty-head">
                <span className="state-mark" aria-hidden />
                <p className="fin-empty-t">Devir zinciri henüz ölçülmüyor</p>
              </div>
              <p className="fin-empty-x">
                Portföy katmanı bu ekrana tek bir kapanış zinciri bildirmiyor. Bu, sermayenin
                anında devrettiği anlamına gelmez — kapanış ile sonraki giriş arasındaki sürenin
                hiç ölçülmediği anlamına gelir, ve bu iki ifade burada birbirinin yerine
                kullanılmaz. Yukarıdaki nakit ve boşta süre değerleri, ölçülebilen yerlerden
                okunmuştur; zincirin kendisi gelene kadar kapanış→tarama gecikmesi bilinmiyor.
              </p>
              <p className="xnote">
                Beklenen alanlar: <code>recycling.last</code> ve <code>recycling.events[]</code>{" "}
                — her biri <code>closed_trade_id</code>, <code>closed_ms</code>,{" "}
                <code>capital_released_ms</code>, <code>rescan_triggered_ms</code>,{" "}
                <code>next_entry_ms</code>, <code>qualified_candidate_existed</code>,{" "}
                <code>close_to_rescan_ms</code>, <code>close_to_entry_ms</code>.
              </p>
            </div>
          )}

          {r?.events && r.events.length > 1 && (
            <div className="tbl-wrap">
              <table className="tbl">
                <thead>
                  <tr>
                    <th className="l">Kapanan tur</th>
                    <th>Kapanış</th>
                    <th title="milisaniye">Kapanış → tarama</th>
                    <th title="saniye">Kapanış → giriş</th>
                    <th className="c">Aday var mıydı</th>
                    <th className="l">Sonraki giriş</th>
                  </tr>
                </thead>
                <tbody>
                  {r.events.slice(0, 25).map((ev, i) => {
                    const a = toNum(ev.close_to_rescan_ms) ?? deltaOf(ev.closed_ms, ev.rescan_triggered_ms);
                    const b = toNum(ev.close_to_entry_ms) ?? deltaOf(ev.closed_ms, ev.next_entry_ms);
                    return (
                      <tr key={ev.closed_trade_id ?? `${ev.closed_ms}-${i}`}>
                        <td className="l sym" title={ev.closed_trade_id ?? undefined}>
                          {ev.closed_symbol ?? ev.symbol
                            ? baseAsset((ev.closed_symbol ?? ev.symbol) as string)
                            : DASH}
                        </td>
                        <td className="dim">{formatClockMs(ev.closed_ms ?? null)}</td>
                        <td className={a === null ? "muted" : ""}>{formatExactMs(a)}</td>
                        <td className={b === null ? "muted" : ""}>
                          {b === null ? DASH : formatSeconds(b / 1000)}
                        </td>
                        <td className="c">
                          {ev.qualified_candidate_existed === true ? (
                            <span className="warn">VARDI</span>
                          ) : ev.qualified_candidate_existed === false ? (
                            <span className="dim">YOKTU</span>
                          ) : (
                            <span className="muted">{DASH}</span>
                          )}
                        </td>
                        <td className="l dim">
                          {ev.next_symbol ?? ev.next_entry_symbol
                            ? baseAsset((ev.next_symbol ?? ev.next_entry_symbol) as string)
                            : DASH}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}

function deltaOf(a: number | null | undefined, b: number | null | undefined): number | null {
  const x = toNum(a);
  const y = toNum(b);
  if (x === null || y === null || y < x) return null;
  return y - x;
}
