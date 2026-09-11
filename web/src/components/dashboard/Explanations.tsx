import { Panel } from "@/components/ui/Panel";
import { Skeleton } from "@/components/ui/DataState";
import {
  DASH,
  REJECT_FAMILY_LABELS,
  REJECT_FAMILY_NOTES,
  type RejectFamily,
  calibrationReading,
  decisionLabel,
  formatInt,
  formatMicroMoney,
  formatProbability,
  formatRate,
  formatRelativeMs,
  formatSignedMicroMoney,
  liquidityLabel,
  makerTakerLabel,
  predictRejectLabel,
  rejectFamily,
  regimeLabel,
  sideLabel,
  spreadLabel,
  toNum,
  tone,
} from "@/lib/format";
import type { PredictiveExplanation, PredictiveState, RejectionTally } from "@/lib/types";

/**
 * NEDEN İŞLEM AÇTI / NEDEN REDDETTİ — items 33 and 34.
 *
 * Two readings of the same decision stream, in one panel because they are one
 * question. The cards say what the model and the risk layer actually held
 * when a trade was opened; the distribution says what stopped every candidate
 * that was not.
 *
 * **Nothing on this panel is composed prose.** Every token in a card is one
 * stored field rendered by one formatter, and a field the worker did not send
 * prints an em dash in place rather than being quietly dropped — a sentence
 * that silently shortens when data is missing reads as a complete explanation
 * and is not one.
 *
 * The distribution exists because "found no edge" and "refuses everything"
 * produce an identical flat P&L and are completely different defects. Grouping
 * the nine reasons into model / venue / scope is what makes them
 * distinguishable at a glance instead of on a calculator.
 */

const FAMILY_ORDER: RejectFamily[] = ["model", "venue", "scope"];

const KNOWN_REASONS = [
  "LOW_PROBABILITY",
  "NEGATIVE_EV",
  "COST_TOO_HIGH",
  "MODEL_UNCERTAIN",
  "CORRELATED_RISK",
  "SPREAD_TOO_WIDE",
  "NO_LIQUIDITY",
  "STALE_DATA",
  "REGIME_NOT_SUPPORTED",
];

function decisionClass(code: string | null | undefined): string {
  const k = code ? String(code).toUpperCase() : "";
  if (k === "BUY" || k === "ENTER" || k === "OPEN" || k === "ACCEPT") return "buy";
  if (k === "SELL") return "sell";
  if (k === "WATCH") return "watch";
  return "none";
}

/** One stored field. The key is always drawn, so an absent value is visible. */
function Tok({
  k,
  v,
  cls,
  title,
}: {
  k: string;
  v: string;
  cls?: string;
  title?: string;
}) {
  return (
    <span className="xtok" title={title}>
      <span className="xtok-k">{k}</span>
      <span className={`xtok-v ${cls ?? ""}`}>{v}</span>
    </span>
  );
}

function ExplanationCard({
  e,
  usableProbabilities,
  probNote,
  now,
}: {
  e: PredictiveExplanation;
  usableProbabilities: boolean;
  probNote: string;
  now: number;
}) {
  const loss = toNum(e.expected_net_loss);

  return (
    <article className={`xcard ${decisionClass(e.decision)}`}>
      <header className="xcard-head">
        <span className="xcard-sym" title={e.symbol ?? undefined}>
          {e.symbol ?? DASH}
        </span>
        <span className="xcard-side">{e.side ? sideLabel(String(e.side)) : DASH}</span>
        <span className={`decision ${decisionClass(e.decision)}`} title={e.decision ?? undefined}>
          {decisionLabel(e.decision)}
        </span>
        <span className="xcard-meta">
          {e.trade_id ? `#${e.trade_id}` : DASH}
          {toNum(e.ts_ms) !== null && ` · ${formatRelativeMs(e.ts_ms, now)}`}
        </span>
      </header>

      <div className="xcard-lines">
        <p className="xline">
          <Tok
            k="P(kazanç)"
            v={formatProbability(e.p_win)}
            cls={usableProbabilities ? "" : "is-uncal"}
            title={probNote}
          />
          <Tok
            k="Beklenen net kazanç"
            v={formatSignedMicroMoney(e.expected_net_win)}
            cls={toNum(e.expected_net_win) === null ? "muted" : "pos"}
          />
          <Tok
            k="Beklenen kayıp"
            v={loss === null ? DASH : formatMicroMoney(-Math.abs(loss))}
            cls={loss === null ? "muted" : "neg"}
          />
        </p>
        <p className="xline">
          <Tok
            k="Beklenen değer"
            v={formatSignedMicroMoney(e.expected_value)}
            cls={tone(e.expected_value) ?? "muted"}
          />
          <Tok
            k="Rejim"
            v={e.regime ? regimeLabel(e.regime) : DASH}
            cls={e.regime ? "" : "muted"}
            title={e.regime ?? undefined}
          />
          <Tok
            k="Makas"
            v={spreadLabel(e.spread_ok)}
            cls={e.spread_ok === true ? "pos" : e.spread_ok === false ? "warn" : "muted"}
          />
          <Tok
            k="Likidite"
            v={liquidityLabel(e.liquidity)}
            cls={
              e.liquidity === null || e.liquidity === undefined || e.liquidity === ""
                ? "muted"
                : ""
            }
          />
        </p>
        <p className="xline">
          <Tok
            k="İcra"
            v={e.execution_style ? makerTakerLabel(String(e.execution_style)) : DASH}
            cls={
              String(e.execution_style ?? "").toUpperCase() === "MAKER"
                ? "pos"
                : e.execution_style
                  ? "dim"
                  : "muted"
            }
          />
          <Tok
            k="Model"
            v={e.model_version ?? DASH}
            cls={e.model_version ? "dim" : "muted"}
          />
        </p>
      </div>
    </article>
  );
}

interface ReasonRow {
  code: string;
  count: number;
  family: RejectFamily | null;
}

function Rejections({ tally }: { tally: RejectionTally | null | undefined }) {
  if (!tally) {
    return (
      <p className="xnote">
        Ret sayacı bildirilmedi. Bu, hiçbir adayın reddedilmediği anlamına gelmez —
        sayacın hiç üretilmediği anlamına gelir, ve kenar bulamayan bir motor ile her
        şeyi reddeden bir motor tam olarak bu sayaç olmadan ayırt edilemez.
      </p>
    );
  }

  const rows: ReasonRow[] = Object.entries(tally)
    .map(([code, raw]) => ({ code, count: toNum(raw) ?? 0, family: rejectFamily(code) }))
    .filter((r) => toNum(tally[r.code]) !== null)
    .sort((a, b) => b.count - a.count);

  const total = rows.reduce((a, r) => a + r.count, 0);
  const missing = KNOWN_REASONS.filter((k) => toNum(tally[k]) === null);

  if (rows.length === 0) {
    return (
      <p className="xnote">
        Ret sayacı geldi ama içinde tek bir sebep yok. Bu bir dağılım değil, boş bir
        kayıttır.
      </p>
    );
  }

  const families = FAMILY_ORDER.map((f) => ({
    family: f,
    count: rows.filter((r) => r.family === f).reduce((a, r) => a + r.count, 0),
  }));
  const unclassified = rows.filter((r) => r.family === null).reduce((a, r) => a + r.count, 0);
  const top = [...families].sort((a, b) => b.count - a.count)[0];

  return (
    <div className="rej">
      <div className="rej-head">
        <span className="rej-total">{formatInt(total)}</span>
        <span className="rej-total-k">reddedilen aday</span>
      </div>

      {total > 0 && (
        <>
          <div className="rej-stack" role="img" aria-label="Ret sebeplerinin aile dağılımı">
            {families.map(
              (f) =>
                f.count > 0 && (
                  <span
                    key={f.family}
                    className={`rej-seg is-${f.family}`}
                    style={{ flexGrow: f.count }}
                    title={`${REJECT_FAMILY_LABELS[f.family]} · ${formatInt(f.count)} · ${formatRate(
                      f.count / total,
                      0,
                    )}`}
                  >
                    {formatRate(f.count / total, 0)}
                  </span>
                ),
            )}
            {unclassified > 0 && (
              <span
                className="rej-seg is-other"
                style={{ flexGrow: unclassified }}
                title={`Sınıflandırılmamış sebepler · ${formatInt(unclassified)}`}
              >
                {formatRate(unclassified / total, 0)}
              </span>
            )}
          </div>

          <div className="rej-legend">
            {families.map((f) => (
              <span key={f.family} className={`rej-dot is-${f.family}`} title={REJECT_FAMILY_NOTES[f.family]}>
                <i aria-hidden />
                {REJECT_FAMILY_LABELS[f.family]}
                <b>{formatInt(f.count)}</b>
              </span>
            ))}
            {unclassified > 0 && (
              <span className="rej-dot is-other">
                <i aria-hidden />
                Sınıflandırılmamış
                <b>{formatInt(unclassified)}</b>
              </span>
            )}
          </div>

          {top && top.count > 0 && (
            <p className="rej-read">
              Retlerin {formatRate(top.count / total, 0)} kadarı{" "}
              <strong>{REJECT_FAMILY_LABELS[top.family].toLocaleLowerCase("tr-TR")}</strong>{" "}
              grubundan geliyor. {REJECT_FAMILY_NOTES[top.family]}
            </p>
          )}
        </>
      )}

      <ul className="rej-list">
        {rows.map((r) => (
          <li key={r.code} className={`rej-row is-${r.family ?? "other"}`}>
            <span className="rej-k" title={r.code}>
              {predictRejectLabel(r.code)}
            </span>
            <span className="rej-bar">
              <span
                className={`rej-fill is-${r.family ?? "other"}`}
                style={{ width: total > 0 ? `${(r.count / total) * 100}%` : "0%" }}
              />
            </span>
            <span className="rej-n">{formatInt(r.count)}</span>
            <span className="rej-p">{total > 0 ? formatRate(r.count / total, 0) : DASH}</span>
          </li>
        ))}
      </ul>

      {missing.length > 0 && (
        <p className="rej-missing">
          Bildirilmeyen sebepler ({missing.length}):{" "}
          {missing.map((m) => predictRejectLabel(m)).join(" · ")}. Bildirilmemiş bir
          sebep sıfır kez tetiklenmiş sayılmaz; sayaçta hiç görünmemiştir.
        </p>
      )}
    </div>
  );
}

export function Explanations({
  predictive,
  loading,
  now,
}: {
  predictive: PredictiveState | null | undefined;
  loading: boolean;
  now: number;
}) {
  const p = predictive ?? null;
  const cal = calibrationReading(p?.calibration);
  const rows = p?.explanations ?? null;
  const tally = p?.rejections ?? null;

  const rejected = tally
    ? Object.values(tally).reduce<number>((a, v) => a + (toNum(v) ?? 0), 0)
    : null;
  const opened = rows ? rows.filter((e) => decisionClass(e.decision) === "buy").length : null;

  const meta = p ? (
    <>
      {opened !== null && (
        <span
          className="pill is-pos"
          title="Gerekçesi saklanmış, işlem açan karar sayısı — kapanmış tur sayısı değil"
        >
          {formatInt(opened)} açılış kaydı
        </span>
      )}
      {rejected !== null && (
        <span className="pill is-off" title="Ret sayacındaki toplam">
          {formatInt(rejected)} ret
        </span>
      )}
      {!cal.usable && (
        <span className="pill is-warn" title={cal.note}>
          {cal.text}
        </span>
      )}
    </>
  ) : null;

  return (
    <Panel
      idx="15"
      title="Neden işlem açtı / neden reddetti"
      note="karar anında saklanan model ve risk değerleri"
      meta={meta}
      footer="Buradaki her satır saklanmış alanlardan çizilir; hiçbiri sonradan üretilmiş bir açıklama değildir. Gelmeyen bir alan yerinde tire olarak görünür, cümleden sessizce düşmez. Ret dağılımı ise tek başına bir teşhistir: kenar bulamayan bir motor ile her şeyi reddeden bir motor kâr/zarar tablosunda birebir aynı görünür, sebep dağılımında ise apayrı."
    >
      {loading && !p ? (
        <Skeleton rows={5} />
      ) : !p ? (
        <div className="pred-empty">
          <div className="fin-empty-head">
            <span className="state-mark" aria-hidden />
            <p className="fin-empty-t">Gerekçe kaydı yok</p>
          </div>
          <p className="fin-empty-x">
            Tahminli motor bildirilmediği için ne açılmış bir işlemin gerekçesi ne de
            bir ret sayacı var. Bu, motorun her şeyi reddettiği anlamına gelmez —
            henüz karar veren bir motor olmadığı anlamına gelir. Motor devreye
            girdiğinde her kâğıt işlem, kararı üreten olasılık, beklenen değer, rejim,
            makas, likidite ve icra biçimiyle birlikte burada saklanacak; reddedilen
            her aday da sebebiyle sayılacak.
          </p>
        </div>
      ) : (
        <div className="xwrap">
          <div className="xside">
            <p className="fgroup-t">
              Neden reddetti{" "}
              <span className="fgroup-n">sebebe göre dağılım</span>
            </p>
            <Rejections tally={tally} />
          </div>

          <div className="xmain">
            <p className="fgroup-t">
              Neden işlem açtı{" "}
              <span className="fgroup-n">
                {rows ? `${formatInt(rows.length)} kayıt` : "kayıt bildirilmedi"}
              </span>
            </p>
            {!rows ? (
              <p className="xnote">
                Gerekçe listesi bildirilmedi. Gerekçesi saklanmamış bir işlem, sonradan
                açıklanamaz — bu yüzden burada bir tahmin üretilmez.
              </p>
            ) : rows.length === 0 ? (
              <p className="xnote">
                Motor çalıştı ve hiçbir işlem açmadı. Tek başına bu bir arıza değildir:
                maliyetini çıkaramayan bir piyasada hiçbir şey açmamak doğru
                davranıştır. Bunun bir kusur mu yoksa doğru karar mı olduğu soldaki ret
                dağılımından okunur — kenar bulunamadığı için mi, yoksa adaylar model
                kararına hiç ulaşamadığı için mi reddedildiklerine bakın.
              </p>
            ) : (
              <div className="xlist">
                {rows.map((e, i) => (
                  <ExplanationCard
                    key={e.trade_id ?? `${e.symbol ?? "?"}-${i}`}
                    e={e}
                    usableProbabilities={cal.usable}
                    probNote={cal.note}
                    now={now}
                  />
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </Panel>
  );
}
