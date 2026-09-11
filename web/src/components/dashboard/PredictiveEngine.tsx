import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import {
  CALIBRATION_MAX_ABS_ERROR,
  DASH,
  baseAsset,
  calibrationReading,
  confidenceReading,
  confidenceTone,
  decisionLabel,
  formatMicroMoney,
  formatMoney,
  formatInt,
  formatProbability,
  formatSignedMicroMoney,
  predictRejectLabel,
  regimeLabel,
  toNum,
  tone,
} from "@/lib/format";
import type { ConfidenceReading } from "@/lib/format";
import type {
  PredictiveCalibration,
  PredictiveCandidate,
  PredictiveState,
} from "@/lib/types";

/**
 * TAHMİNLİ GİRİŞ MOTORU — item 31.
 *
 * The panel exists to answer one question per candidate: given this market
 * state, what is the expected value after costs, and is it positive enough to
 * act on. Everything is laid out around the BEKLENEN DEĞER column for that
 * reason — the probabilities to its left are inputs, the cost to its right is
 * the toll, and the decision on the far right is the consequence.
 *
 * The rule that shapes the whole design: **an uncalibrated model's output is
 * a number, not a probability.** When the calibration block has not been
 * measured, or was measured and failed, the probability columns are hatched
 * exactly as the in-sample columns are in the tournament, the confidence cell
 * prints the reason instead of a figure, and a strip above the table says so
 * in words. A percentage is the most authoritative-looking thing this screen
 * can print, and it is not printed on trust.
 */

type Group = "pid" | "prob" | "ev" | "dec";

const GROUPS: { group: Group; label: string; note: string }[] = [
  { group: "pid", label: "Aday", note: "coin · rejim · modeli üreten sürüm" },
  {
    group: "prob",
    label: "Model olasılıkları",
    note: "üç sonucun olasılığı — yalnızca kalibrasyon ölçülüp geçtiyse olasılık sayılır",
  },
  {
    group: "ev",
    label: "Maliyet sonrası beklenti",
    note: "kazanç ve kayıp dalları, beklenen değer ve icra maliyeti — hepsi NET",
  },
  { group: "dec", label: "Hüküm", note: "güven ve karar" },
];

function cellTone(v: number | null): string {
  if (v === null) return "muted";
  return tone(v) ?? "flat";
}

/**
 * The calibration strip.
 *
 * It is drawn above the table rather than inside a tooltip because it governs
 * how every figure below it should be read. When the model is trusted the
 * strip still appears — a screen that only warns when something is wrong
 * gives the operator no way to tell "fine" from "not checked".
 */
function CalibrationStrip({ reading }: { reading: ConfidenceReading }) {
  return (
    <div className={`calstrip ${confidenceTone(reading.kind)}`} role="status">
      <span className="calstrip-tag">{reading.text}</span>
      <span className="calstrip-x">{reading.note}</span>
    </div>
  );
}

/**
 * The engine's own decision rule, printed whenever there is nothing to judge.
 *
 * Same job as the gate list in the FİNALİSTLER panel: an empty engine panel
 * only means something if the bar nothing cleared is visible next to it. The
 * two thresholds are read from the payload when the worker sends them and
 * said to be unreported when it does not — they are never assumed.
 */
function EngineRule({ p }: { p: PredictiveState | null | undefined }) {
  const pt = toNum(p?.probability_threshold);
  const ev = toNum(p?.ev_threshold_usd);

  const rules: { n: number; text: string }[] = [
    {
      n: 1,
      text:
        pt === null
          ? "Kazanç olasılığı, bildirilmemiş bir olasılık eşiğinin üzerinde olmalı"
          : `Kazanç olasılığı ≥ ${formatProbability(pt)} — modelin donmuş olasılık eşiği`,
    },
    {
      n: 2,
      text:
        ev === null
          ? "Beklenen değer, bildirilmemiş bir USD eşiğinin üzerinde olmalı"
          : `Beklenen değer > ${formatMicroMoney(ev)} — maliyet düşülmüş, işlem başına`,
    },
    {
      n: 3,
      text: `Kalibrasyon ölçülmüş ve azami mutlak güvenilirlik hatası ≤ ${CALIBRATION_MAX_ABS_ERROR.toLocaleString(
        "en-US",
        { minimumFractionDigits: 2 },
      )} olmalı`,
    },
    {
      n: 4,
      text: "İcra maliyeti kazanç dalının içinden çıkmalı — brüt hedef değil, NET hedef",
    },
    {
      n: 5,
      text: "Rejim modelin eğitildiği kapsamda, makas ve derinlik uygun, veri taze olmalı",
    },
  ];

  return (
    <div className="gateref">
      <p className="gateref-t">Bir adayın AL olabilmesi için — hepsi birden</p>
      <ol className="gateref-l">
        {rules.map((r) => (
          <li key={r.n}>
            <span className="gateref-n">{String(r.n).padStart(2, "0")}</span>
            <span>{r.text}</span>
          </li>
        ))}
      </ol>
      <p className="gateref-f">
        Kazanma oranı değil beklenen değer karar verir: yüksek isabetli ama ödemesi
        kötü bir kurulum kaybeden kurulumdur. Kalibre edilmemiş bir modelin ürettiği
        yüzde ise bir olasılık değil, bir sayıdır — bu panel onu olasılık gibi
        göstermez.
      </p>
    </div>
  );
}

function decisionClass(code: string | null | undefined): string {
  switch (code) {
    case "BUY":
    case "ENTER":
    case "OPEN":
    case "ACCEPT":
      return "buy";
    case "SELL":
      return "sell";
    case "WATCH":
      return "watch";
    default:
      return "none";
  }
}

function Row({
  c,
  calibration,
}: {
  c: PredictiveCandidate;
  calibration: PredictiveCalibration | null | undefined;
}) {
  // `confidenceReading` consults the model's calibration first and only then
  // the candidate's own figure, so an uncalibrated model can never reach the
  // branch that prints a percentage.
  const reading: ConfidenceReading = confidenceReading(
    c.confidence,
    c.confidence_known,
    calibration,
  );

  const evValue = toNum(c.expected_value);
  const loss = toNum(c.expected_net_loss);
  const cost = toNum(c.execution_cost);

  return (
    <tr>
      <td className={`l stick c-pid ${c.symbol ? "sym" : "muted"}`} title={c.symbol ?? undefined}>
        {c.symbol ? baseAsset(c.symbol) : DASH}
      </td>
      <td className="l c-pid">
        <span className="tag" title={c.regime ?? undefined}>
          {regimeLabel(c.regime)}
        </span>
      </td>
      <td className="l c-pid dim" title={c.model ?? undefined}>
        {c.model ?? DASH}
      </td>

      <td className="c-prob" title="P(önce kâr al bariyeri)">
        {formatProbability(c.p_tp)}
      </td>
      <td className="c-prob" title="P(önce stop bariyeri)">
        {formatProbability(c.p_sl)}
      </td>
      <td className="c-prob" title="P(ufuk içinde iki bariyer de değmedi)">
        {formatProbability(c.p_timeout)}
      </td>

      <td
        className={`c-ev ${toNum(c.expected_net_win) === null ? "muted" : "pos"}`}
        title="Kazanç dalının modellenen NET getirisi"
      >
        {formatMicroMoney(c.expected_net_win)}
      </td>
      <td
        className={`c-ev ${loss === null ? "muted" : "neg"}`}
        title="Kayıp dalının modellenen NET bedeli"
      >
        {loss === null ? DASH : formatMicroMoney(-Math.abs(loss))}
      </td>
      <td
        className={`c-ev is-head ${cellTone(evValue)}`}
        title="Beklenen değer: olasılık ağırlıklı kazanç − kayıp − maliyet"
      >
        {formatSignedMicroMoney(evValue)}
      </td>
      <td
        className={`c-ev ${cost === null ? "muted" : "neg"}`}
        title="Komisyon, makas ve kaymanın adaya düşen payı"
      >
        {cost === null ? DASH : formatMicroMoney(-Math.abs(cost))}
      </td>

      <td className="c-dec">
        <span className={`conf ${confidenceTone(reading.kind)}`} title={reading.note}>
          {reading.text}
        </span>
      </td>
      <td className="c-dec l">
        <span
          className={`decision ${decisionClass(c.decision)}`}
          title={c.decision ?? undefined}
        >
          {decisionLabel(c.decision)}
        </span>
        {c.reject_reason && (
          <span className="pred-why" title={String(c.reject_reason)}>
            {predictRejectLabel(c.reject_reason)}
          </span>
        )}
      </td>
    </tr>
  );
}

export function PredictiveEngine({
  predictive,
  loading,
}: {
  predictive: PredictiveState | null | undefined;
  loading: boolean;
}) {
  const p = predictive ?? null;
  const cal = calibrationReading(p?.calibration);
  const rows = p?.candidates ?? null;

  const buys = rows?.filter((c) => decisionClass(c.decision) === "buy").length ?? 0;

  const meta = p ? (
    <>
      {p.model_version && (
        <span className="pill" title="Çıkarım paketinin sürümü">
          {p.model_version}
        </span>
      )}
      <span className={`pill ${cal.usable ? "is-pos" : "is-warn"}`} title={cal.note}>
        {cal.text}
      </span>
      {toNum(p.probability_threshold) !== null && (
        <span className="panel-note" title="Donmuş olasılık eşiği">
          olasılık eşiği {formatProbability(p.probability_threshold)}
        </span>
      )}
      {toNum(p.ev_threshold_usd) !== null && (
        <span className="panel-note" title="İşlem başına asgari beklenen değer">
          EV eşiği {formatMicroMoney(p.ev_threshold_usd)}
        </span>
      )}
      {rows && (
        <span className="panel-note">
          {formatInt(rows.length)} aday · {formatInt(buys)} AL
        </span>
      )}
    </>
  ) : null;

  return (
    <Panel
      idx="13"
      title="Tahminli giriş motoru"
      note="aday başına kazanma olasılığı, maliyet sonrası beklenen değer ve karar"
      meta={meta}
      flush
      footer="Karar beklenen değere göre verilir, kazanma oranına göre değil: yüksek isabetli ama ödemesi kötü bir kurulum kaybeden kurulumdur. Olasılık sütunları yalnızca kalibrasyon ölçülüp sınırı geçtiğinde düz çizilir; ölçülmemiş veya sınırı geçememiş bir modelde taranarak gösterilir ve güven hücresi yüzde yerine sebebi yazar."
    >
      {loading && !p ? (
        <Skeleton rows={6} />
      ) : !p ? (
        <div className="pred-empty">
          <div className="fin-empty-head">
            <span className="state-mark" aria-hidden />
            <p className="fin-empty-t">Tahminli motor henüz devrede değil</p>
          </div>
          <p className="fin-empty-x">
            Worker bu ekrana bir model bildirmiyor. Bu, &ldquo;model kenar
            bulamadı&rdquo; demek değildir — henüz terfi etmiş bir model olmadığı
            anlamına gelir, ve bu iki ifade bu panelde birbirinin yerine kullanılmaz.
            Phase 3 sıfır şampiyonla kapandı; dolayısıyla burada hiçbir zaman bir
            model olmaması da olası ve kabul edilebilir bir sonuçtur. Bir çıkarım
            paketi eğitilip donduğunda her aday aşağıdaki koşullardan tek tek
            geçirilecek ve sonucu bu tabloda görünecek.
          </p>
          <EngineRule p={null} />
        </div>
      ) : !rows ? (
        <DataState
          title="Aday listesi bildirilmedi"
          text="Model kaydı geldi ancak içinde değerlendirilmiş aday alanı yok. Bu bir eksik çıktıdır, sıfır aday değildir — ölçülmemiş bir sonuç bu ekranda sıfır olarak gösterilmez."
        />
      ) : rows.length === 0 ? (
        <div className="pred-empty">
          <div className="fin-empty-head">
            <span className="fin-empty-mark" aria-hidden />
            <p className="fin-empty-t">Bu turda hiçbir aday üretilmedi</p>
          </div>
          <p className="fin-empty-x">
            Model çalıştı ve değerlendirilecek tek bir aday bulamadı. Boş bir aday
            listesi ile hepsi reddedilmiş bir aday listesi farklı okumalardır: ilki
            evrende hiçbir kurulumun ön elemeyi geçmediğini, ikincisi kurulumların
            geçip beklenen değerde elendiğini söyler. Hangisinin yaşandığı
            §15&apos;teki ret dağılımında görünür.
          </p>
          <EngineRule p={p} />
        </div>
      ) : (
        <>
          <CalibrationStrip reading={cal} />
          <div className="tbl-wrap">
            <table className={`tbl tbl-pred${cal.usable ? "" : " is-uncal"}`}>
              <thead>
                <tr className="grp">
                  {GROUPS.map((g) => {
                    const span =
                      g.group === "pid" ? 3 : g.group === "prob" ? 3 : g.group === "ev" ? 4 : 2;
                    return (
                      <th key={g.group} className={`gh g-${g.group}`} colSpan={span} title={g.note}>
                        {g.label}
                        {g.group === "prob" && !cal.usable && (
                          <span className="gh-warn" title={cal.note}>
                            {" "}
                            · {cal.text} — olasılık değil, sayı
                          </span>
                        )}
                      </th>
                    );
                  })}
                </tr>
                <tr className="cols">
                  <th className="l stick c-pid">Coin</th>
                  <th className="l c-pid">Rejim</th>
                  <th className="l c-pid">Model</th>
                  <th className="c-prob" title="Önce kâr al bariyerine değme olasılığı">
                    P(TP önce)
                  </th>
                  <th className="c-prob" title="Önce stop bariyerine değme olasılığı">
                    P(SL önce)
                  </th>
                  <th className="c-prob" title="Ufuk içinde hiçbir bariyere değmeme olasılığı">
                    P(zaman aşımı)
                  </th>
                  <th className="c-ev">Beklenen net kazanç</th>
                  <th className="c-ev">Beklenen net kayıp</th>
                  <th className="c-ev is-head">Beklenen değer</th>
                  <th className="c-ev">İcra maliyeti</th>
                  <th className="c-dec">Güven</th>
                  <th className="c-dec l">Karar</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((c, i) => (
                  <Row
                    key={`${c.symbol ?? "?"}-${c.model ?? "?"}-${i}`}
                    c={c}
                    calibration={p.calibration}
                  />
                ))}
              </tbody>
            </table>
          </div>
          {toNum(p.ev_threshold_usd) !== null && (
            <p className="pred-foot">
              Yürürlükteki eşikler: olasılık {formatProbability(p.probability_threshold)} ·
              beklenen değer {formatMoney(p.ev_threshold_usd, 4)} · azami güvenilirlik hatası{" "}
              {CALIBRATION_MAX_ABS_ERROR.toLocaleString("en-US", { minimumFractionDigits: 2 })}
            </p>
          )}
        </>
      )}
    </Panel>
  );
}
