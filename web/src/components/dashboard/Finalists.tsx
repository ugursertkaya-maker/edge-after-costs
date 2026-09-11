import { Panel } from "@/components/ui/Panel";
import { Skeleton } from "@/components/ui/DataState";
import {
  DASH,
  baseAsset,
  familyLabel,
  formatHorizon,
  formatInt,
  formatMoney,
  formatRate,
  formatSignedMicroMoney,
  gateVerdict,
  regimeLabel,
  toNum,
  tone,
} from "@/lib/format";
import type { FinalistRow, MonteCarloResult, StressResult } from "@/lib/types";

/**
 * The promotion gates, verbatim from the Phase 3 brief.
 *
 * They are rendered whether or not anything passed them, because the list is
 * the standard — and an empty finalists panel only means something if the bar
 * that nothing cleared is visible next to it. They are also the reason this
 * panel exists at all: the gates are not tuned to produce a winner, and a
 * screen that quietly dropped one of them would be indistinguishable from a
 * screen that never had it.
 */
const GATES: { n: number; text: string }[] = [
  { n: 1, text: "Örneklem dışı NET expectancy sıfırın üzerinde" },
  {
    n: 2,
    text: "Profit factor ≥ 1,20 — 1,00 ile 1,20 arası SINIRDA'dır, finalist değildir",
  },
  { n: 3, text: "Max drawdown $500'lık hesaba karşı makul" },
  {
    n: 4,
    text: "Tek işleme bağlı değil — en iyi işlem çıkarıldığında expectancy pozitif kalır",
  },
  {
    n: 5,
    text: "Birden çok test penceresinde pozitif — walk-forward pencerelerinin çoğunluğu",
  },
  {
    n: 6,
    text: "Maliyet stresine dayanır — komisyon +%25 ve +%50, kayma ×2, gecikme ×2, daha kötü makas, rastgele emir gecikmesi",
  },
  { n: 7, text: "Asgari örneklem: en az 30 örneklem dışı işlem" },
];

function GateReference() {
  return (
    <div className="gateref">
      <p className="gateref-t">Yürürlükteki kapılar — örneklem dışı, hepsi birden</p>
      <ol className="gateref-l">
        {GATES.map((g) => (
          <li key={g.n}>
            <span className="gateref-n">{String(g.n).padStart(2, "0")}</span>
            <span>{g.text}</span>
          </li>
        ))}
      </ol>
      <p className="gateref-f">
        Bir kazanan bulmak için bu eşikler indirilmez. İndirilmiş bir kapıdan geçen
        strateji kâr değil, uydurulmuş bir geçmiş üretir.
      </p>
    </div>
  );
}

function Stress({ items }: { items: StressResult[] }) {
  return (
    <div className="stress">
      <p className="fgroup-t">Maliyet stresi</p>
      <div className="stress-list">
        {items.map((s, i) => {
          const survived = s.survived;
          const cls =
            survived === true ? "is-pass" : survived === false ? "is-fail" : "is-unknown";
          return (
            <span
              key={`${s.label ?? "stres"}-${i}`}
              className={`schip ${cls}`}
              title={
                survived === true
                  ? "Bu senaryoda hayatta kaldı"
                  : survived === false
                    ? "Bu senaryoda düştü"
                    : "Bu senaryo için hüküm bildirilmedi"
              }
            >
              <span className="schip-k">{s.label ?? DASH}</span>
              <span className={`schip-v ${tone(s.net_pnl) ?? "muted"}`}>
                {formatSignedMicroMoney(s.net_pnl)}
              </span>
            </span>
          );
        })}
      </div>
    </div>
  );
}

function MonteCarlo({ mc }: { mc: MonteCarloResult }) {
  const prob = toNum(mc.prob_below_start);
  return (
    <div className="mc">
      <p className="fgroup-t">
        Monte Carlo{" "}
        {toNum(mc.samples) !== null && (
          <span className="fgroup-n">{formatInt(mc.samples)} örnekleme</span>
        )}
      </p>
      <dl className="cgrid">
        <div className="citem">
          <dt>Medyan drawdown</dt>
          <dd className={toNum(mc.median_drawdown) === null ? "muted" : "neg"}>
            {toNum(mc.median_drawdown) === null
              ? DASH
              : formatMoney(-Math.abs(toNum(mc.median_drawdown) as number))}
          </dd>
        </div>
        <div className="citem">
          <dt>%95 drawdown</dt>
          <dd className={toNum(mc.p95_drawdown) === null ? "muted" : "neg"}>
            {toNum(mc.p95_drawdown) === null
              ? DASH
              : formatMoney(-Math.abs(toNum(mc.p95_drawdown) as number))}
          </dd>
        </div>
        <div className="citem">
          <dt>En uzun kayıp serisi</dt>
          <dd className={toNum(mc.worst_loss_streak) === null ? "muted" : ""}>
            {toNum(mc.worst_loss_streak) === null
              ? DASH
              : `${formatInt(mc.worst_loss_streak)} işlem`}
          </dd>
        </div>
        <div className="citem">
          <dt>Başlangıcın altında bitme</dt>
          <dd className={prob === null ? "muted" : prob > 0.3 ? "neg" : prob > 0.1 ? "warn" : ""}>
            {formatRate(prob)}
          </dd>
        </div>
      </dl>
    </div>
  );
}

function Card({ f, index }: { f: FinalistRow; index: number }) {
  const reasons = f.reasons ?? [];
  const verdicts = reasons.map((r) => gateVerdict(r));
  const passed = verdicts.filter((v) => v === "pass").length;
  const failed = verdicts.filter((v) => v === "fail").length;
  const unknown = verdicts.filter((v) => v === "unknown").length;

  const overall =
    f.passed === true ? "is-pass" : f.passed === false ? "is-fail" : "is-unknown";
  const overallText =
    f.passed === true
      ? "TÜM KAPILAR GEÇİLDİ"
      : f.passed === false
        ? "KAPILARDAN GEÇEMEDİ"
        : "HÜKÜM BİLDİRİLMEDİ";

  const identity = [
    f.symbol ? baseAsset(f.symbol) : null,
    f.regime ? regimeLabel(f.regime) : null,
    toNum(f.horizon_seconds) !== null ? formatHorizon(f.horizon_seconds) : null,
  ].filter(Boolean);

  return (
    <article className={`fin ${overall}`}>
      <header className="fin-head">
        <span className="fin-name">{f.family ? familyLabel(f.family) : DASH}</span>
        <span className="fin-sub">
          {identity.length > 0 ? identity.join(" · ") : "kimlik bildirilmedi"}
        </span>
        <span className={`fin-verdict ${overall}`}>{overallText}</span>
      </header>

      <div className="fin-body">
        <div className="fgroup">
          <p className="fgroup-t">
            Kapı raporu{" "}
            <span className="fgroup-n">
              {formatInt(reasons.length)} satır · {passed} geçti · {failed} kaldı
              {unknown > 0 ? ` · ${unknown} belirsiz` : ""}
            </span>
          </p>
          {reasons.length === 0 ? (
            <p className="xnote">
              Bu aday için kapı gerekçesi bildirilmedi. Gerekçesiz bir geçiş, geçiş
              sayılmaz — aşağıdaki kapı listesi hâlâ yürürlüktedir.
            </p>
          ) : (
            <ul className="gates">
              {reasons.map((r, i) => {
                const v = verdicts[i];
                return (
                  <li key={`${index}-${i}`} className={`gate is-${v}`}>
                    <span className="gate-m" aria-hidden>
                      {v === "pass" ? "✓" : v === "fail" ? "✕" : "·"}
                    </span>
                    <span className="gate-t">{r}</span>
                  </li>
                );
              })}
            </ul>
          )}
          {unknown > 0 && reasons.length > 0 && (
            <p className="xnote">
              Hükmü kendi metninde yazmayan satırlar nötr çizilir. Panel, kendisine
              söylenmemiş bir geçişi yeşile boyamaz.
            </p>
          )}
        </div>

        {f.stress && f.stress.length > 0 && <Stress items={f.stress} />}
        {f.monte_carlo && <MonteCarlo mc={f.monte_carlo} />}
      </div>
    </article>
  );
}

/**
 * What survived the gates.
 *
 * The empty case is the expected case and is the most important sentence on
 * this page. It is written as a finding — "nothing passed" — and never as a
 * loading state, an empty table or a blank panel, because those three all read
 * as "the screen is broken" and this one has to read as "the search answered".
 */
export function Finalists({
  finalists,
  loading,
}: {
  finalists: FinalistRow[] | null | undefined;
  loading: boolean;
}) {
  const rows = finalists ?? null;
  const passedCount = rows?.filter((f) => f.passed !== false).length ?? 0;

  const meta =
    rows && rows.length > 0 ? (
      <span className={`pill ${passedCount > 0 ? "is-pos" : "is-off"}`}>
        {formatInt(passedCount)} finalist
      </span>
    ) : rows ? (
      <span className="pill is-off">finalist yok</span>
    ) : null;

  return (
    <Panel
      idx="11"
      title="Finalistler"
      note="sağlamlık kapılarının tamamını geçen adaylar"
      meta={meta}
      footer="Finalist listesinin boş olması bir arıza değil, bir ölçüm sonucudur: denenen adayların hiçbiri maliyet sonrası ve örneklem dışı olarak kapıların tamamını geçemedi. Hiçbir şeyin çalışmadığı bir arama da bir sonuçtur ve bu panel onu böyle bildirir."
    >
      {loading && rows === null ? (
        <Skeleton rows={4} />
      ) : rows === null ? (
        <div className="fin-empty">
          <div className="fin-empty-head">
            <span className="state-mark" aria-hidden />
            <p className="fin-empty-t">Sağlamlık değerlendirmesi henüz çalışmadı</p>
          </div>
          <p className="fin-empty-x">
            Kapı raporu üretilmedi. Bu, &ldquo;hiçbir strateji geçemedi&rdquo; demek
            değildir — henüz kimsenin sınava girmediği anlamına gelir. İkisi farklı
            ifadelerdir ve bu panel onları birbirinin yerine kullanmaz. Turnuva
            sonuçları geldiğinde her aday aşağıdaki kapılardan tek tek geçirilecek.
          </p>
          <GateReference />
        </div>
      ) : rows.length === 0 ? (
        <div className="fin-empty is-none">
          <div className="fin-empty-head">
            <span className="fin-empty-mark" aria-hidden />
            <p className="fin-empty-t">Hiçbir strateji kapıları geçemedi</p>
          </div>
          <p className="fin-empty-x">
            Beklenen sonuç budur ve panelin boş olması değil, aramanın cevabıdır.
            Denenen adayların hiçbiri maliyet sonrası, örneklem dışı ve stres altında
            aşağıdaki yedi kapının tamamını geçemedi; dolayısıyla finalist yoktur ve
            canlı kâğıda terfi edecek bir strateji de yoktur.
          </p>
          <GateReference />
        </div>
      ) : (
        <div className="fin-list">
          {rows.map((f, i) => (
            <Card key={f.key ?? `${f.family}-${f.symbol}-${i}`} f={f} index={i} />
          ))}
          <GateReference />
        </div>
      )}
    </Panel>
  );
}
