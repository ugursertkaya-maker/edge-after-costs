import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import { Stat } from "@/components/dashboard/bits";
import {
  DASH,
  MISSED_ALARM_BPS,
  MISSED_NOISE_BPS,
  formatBps,
  formatInt,
  formatRate,
  missedVerdict,
  missedVerdictTone,
  rejectReasonLabel,
  toNum,
} from "@/lib/format";
import type { MissedAudit } from "@/lib/types";

/**
 * KAÇIRILAN FIRSATLAR — was the refusal right?
 *
 * Every rejection the risk layer makes is a claim: *this candidate was not
 * worth taking.* This panel is the only place that claim is ever checked, by
 * replaying what the refused move went on to do. Two outcomes, and they are
 * opposite readings of the same gate:
 *
 *   MISSED_WINNER     — the refusal gave up money that was there.
 *   CORRECT_REJECTION — the refusal avoided a loss and earned its keep.
 *
 * The aggregate `missed_net_bps` is the calibration reading and its SIGN is
 * the whole answer: negative means the refusals saved money, strongly positive
 * means the gate is throwing money away. A dashboard that printed the
 * magnitude without the direction would be unreadable, so the verdict is
 * written in words next to the figure and never left to the reader's sign
 * arithmetic.
 *
 * This is POST-HOC analysis computed with future data. It cannot and must not
 * reach an entry decision — any path where it could is a leak and a defect —
 * and the strip at the top of the panel says so, in one line, so nobody reads
 * a row here as a live signal.
 */

interface ReasonRow {
  code: string;
  missed: number;
  correct: number;
}

function pairOf(value: unknown): [number | null, number | null] {
  if (!Array.isArray(value)) return [null, null];
  return [toNum(value[0] as number), toNum(value[1] as number)];
}

/** The one line that keeps this panel from being mistaken for a live signal. */
function PostHocStrip() {
  return (
    <div className="phoc" role="note">
      <span className="phoc-tag">GEÇMİŞE DÖNÜK</span>
      <span className="phoc-x">
        Bu panel, reddedilen adayların sonradan ne yaptığını gelecekteki veriyle ölçer.
        Bir denetimdir, bir sinyal değildir: buradaki hiçbir satır canlı karara girmez,
        girebildiği bir kod yolu varsa o bir sızıntı ve kusurdur.
      </span>
    </div>
  );
}

function Split({
  missed,
  correct,
  unlabelled,
}: {
  missed: number;
  correct: number;
  unlabelled: number;
}) {
  const total = missed + correct + unlabelled;
  if (total <= 0) return null;
  return (
    <div className="mo-split" role="img" aria-label="Doğru retler ile kaçırılan kazançların oranı">
      {correct > 0 && (
        <span
          className="mo-seg is-correct"
          style={{ flexGrow: correct }}
          title={`Doğru ret · ${formatInt(correct)} · ${formatRate(correct / total, 0)}`}
        >
          {formatRate(correct / total, 0)}
        </span>
      )}
      {missed > 0 && (
        <span
          className="mo-seg is-missed"
          style={{ flexGrow: missed }}
          title={`Kaçırılan kazanç · ${formatInt(missed)} · ${formatRate(missed / total, 0)}`}
        >
          {formatRate(missed / total, 0)}
        </span>
      )}
      {unlabelled > 0 && (
        <span
          className="mo-seg is-other"
          style={{ flexGrow: unlabelled }}
          title={`Etiketlenmemiş ret · ${formatInt(unlabelled)}`}
        >
          {formatRate(unlabelled / total, 0)}
        </span>
      )}
    </div>
  );
}

export function MissedOpportunities({
  missed,
  loading,
}: {
  missed: MissedAudit | null | undefined;
  loading: boolean;
}) {
  const m = missed ?? null;

  const bps = toNum(m?.missed_net_bps);
  const verdict = missedVerdict(bps);
  const vtone = missedVerdictTone(verdict.kind);

  const winners = toNum(m?.missed_winners);
  const correct = toNum(m?.correct_rejections);
  const examined = toNum(m?.rejections_examined);

  const judged = (winners ?? 0) + (correct ?? 0);
  const unlabelled =
    examined !== null && winners !== null && correct !== null
      ? Math.max(0, examined - judged)
      : 0;
  const correctShare =
    winners !== null && correct !== null && judged > 0 ? correct / judged : null;

  const byReason = m?.by_reason ?? null;
  const rows: ReasonRow[] = byReason
    ? Object.keys(byReason)
        .map((code) => {
          const [a, b] = pairOf(byReason[code]);
          return { code, missed: a ?? 0, correct: b ?? 0, known: a !== null || b !== null };
        })
        .filter((r) => r.known)
        .map(({ code, missed: ms, correct: cr }) => ({ code, missed: ms, correct: cr }))
        .sort((x, y) => y.missed - x.missed || y.correct - x.correct)
    : [];

  const rowMissed = rows.reduce((a, r) => a + r.missed, 0);
  const rowCorrect = rows.reduce((a, r) => a + r.correct, 0);
  const totalsDisagree =
    rows.length > 0 &&
    winners !== null &&
    correct !== null &&
    (rowMissed !== winners || rowCorrect !== correct);
  const rowMax = rows.reduce((a, r) => Math.max(a, r.missed + r.correct), 0);

  const meta = m ? (
    <>
      <span className={`pill ${verdict.kind === "saved" ? "is-pos" : verdict.kind === "noise" ? "is-off" : "is-warn"}`} title={verdict.note}>
        {verdict.text}
      </span>
      {toNum(m.windows) !== null && (
        <span className="panel-note" title="İncelenen geçmiş pencere sayısı">
          {formatInt(m.windows)} pencere
        </span>
      )}
      {examined !== null && (
        <span className="panel-note">{formatInt(examined)} ret incelendi</span>
      )}
    </>
  ) : null;

  return (
    <Panel
      idx="16"
      title="Kaçırılan fırsatlar"
      note="reddedilen adaylar sonradan ne yaptı — geçmişe dönük denetim"
      meta={meta}
      footer={`Toplamın işareti hükümdür: negatif bir toplam, reddedilen hareketlerin maliyet sonrası eksiye gittiğini — yani kapının para kazandırdığını — söyler; güçlü pozitif bir toplam, kapının gerçekten orada olan kenarı geri çevirdiğini söyler. Sıfıra yakın değerler (±${MISSED_NOISE_BPS} bps, bir Tier-0 gidiş-dönüşü) kovalanacak bir fark değildir; ${MISSED_ALARM_BPS} bps, Tier-0 dışı bir gidiş-dönüşün tamamıdır. Bu denetim yalnızca kapının kalibrasyonunu ölçer; eşikleri gevşetmek için bir gerekçe değildir, çünkü her gevşetme aynı anda hem kaçırılanı hem de zararı artırır.`}
    >
      {loading && !m ? (
        <Skeleton rows={4} />
      ) : !m ? (
        <div className="fin-empty">
          <div className="fin-empty-head">
            <span className="state-mark" aria-hidden />
            <p className="fin-empty-t">Ret denetimi henüz çalışmadı</p>
          </div>
          <p className="fin-empty-x">
            Reddedilen adayların sonradan ne yaptığı ölçülmedi. Bu, &ldquo;hiçbir fırsat
            kaçırılmadı&rdquo; demek değildir — kimsenin bakmadığı anlamına gelir, ve bu
            ekranda ölçülmemiş bir denetim temiz bir denetim gibi gösterilmez. Denetim
            çalıştığında her ret iki kutudan birine düşecek: parayı kurtaran DOĞRU RET ve
            parayı bırakan KAÇIRILAN KAZANÇ.
          </p>
          <PostHocStrip />
        </div>
      ) : (
        <div className="mo">
          <PostHocStrip />

          <div className={`mo-head ${vtone}`}>
            <div className="mo-figure">
              <span className="mo-k">Toplam kaçırılan hareket</span>
              <span className={`mo-v ${bps === null ? "is-empty" : ""}`}>
                {bps === null ? DASH : `${formatBps(bps)} bps`}
              </span>
              <span className="mo-n">maliyet sonrası, reddedilen adayların toplamı</span>
            </div>
            <div className="mo-verdict">
              <span className={`mo-badge ${vtone}`}>{verdict.text}</span>
              <p className="mo-note">{verdict.note}</p>
            </div>
          </div>

          <Split missed={winners ?? 0} correct={correct ?? 0} unlabelled={unlabelled} />

          <div className="mo-legend">
            <span className="mo-dot is-correct">
              <i aria-hidden />
              Doğru ret — zarardan korudu <b>{formatInt(correct)}</b>
            </span>
            <span className="mo-dot is-missed">
              <i aria-hidden />
              Kaçırılan kazanç — para bırakıldı <b>{formatInt(winners)}</b>
            </span>
            {unlabelled > 0 && (
              <span className="mo-dot is-other">
                <i aria-hidden />
                Etiketlenmemiş — hüküm verilemedi <b>{formatInt(unlabelled)}</b>
              </span>
            )}
          </div>

          <div className="pfolio-grid">
            <Stat label="Pencere" value={formatInt(m.windows)} note="incelenen geçmiş dilim" />
            <Stat
              label="İncelenen ret"
              value={formatInt(m.rejections_examined)}
              note="denetime giren aday"
            />
            <Stat
              label="Kaçırılan kazanç"
              value={formatInt(m.missed_winners)}
              cls={winners === null ? "" : winners > 0 ? "warn" : ""}
              note="maliyet sonrası kazandıracaktı"
            />
            <Stat
              label="Doğru ret"
              value={formatInt(m.correct_rejections)}
              cls={correct === null ? "" : correct > 0 ? "pos" : ""}
              note="reddedilmesi doğruydu"
            />
            <Stat
              label="Doğru ret oranı"
              value={correctShare === null ? DASH : formatRate(correctShare, 1)}
              note="hüküm verilen retler içinde"
            />
          </div>

          {rows.length === 0 ? (
            <DataState
              title="Ret sebebine göre dağılım bildirilmedi"
              text="Denetim toplamları geldi ama hangi sebebin para bıraktığı gelmedi. Toplam, hangi kapının fazla sıkı olduğunu söylemez — bunu yalnızca sebep kırılımı söyler."
            />
          ) : (
            <ul className="mo-list">
              <li className="mo-row is-head">
                <span className="mo-rk">Ret sebebi</span>
                <span className="mo-rb">doğru ret · kaçırılan</span>
                <span className="mo-rn">Kaçırılan</span>
                <span className="mo-rn">Doğru</span>
                <span className="mo-rp">Kaçırma oranı</span>
              </li>
              {rows.map((r) => {
                const tot = r.missed + r.correct;
                const share = tot > 0 ? r.missed / tot : null;
                const scale = rowMax > 0 ? (tot / rowMax) * 100 : 0;
                return (
                  <li key={r.code} className="mo-row">
                    <span className="mo-rk" title={r.code}>
                      {rejectReasonLabel(r.code)}
                    </span>
                    <span className="mo-rb">
                      <span className="mo-rbar" style={{ width: `${scale}%` }}>
                        {r.correct > 0 && (
                          <span
                            className="mo-rfill is-correct"
                            style={{ width: tot > 0 ? `${(r.correct / tot) * 100}%` : "0%" }}
                          />
                        )}
                        {r.missed > 0 && (
                          <span
                            className="mo-rfill is-missed"
                            style={{ width: tot > 0 ? `${(r.missed / tot) * 100}%` : "0%" }}
                          />
                        )}
                      </span>
                    </span>
                    <span className="mo-rn is-missed">{formatInt(r.missed)}</span>
                    <span className="mo-rn is-correct">{formatInt(r.correct)}</span>
                    <span className="mo-rp">{share === null ? DASH : formatRate(share, 0)}</span>
                  </li>
                );
              })}
            </ul>
          )}

          {totalsDisagree && (
            <p className="idle-mismatch" role="status">
              Sebep kırılımı üst toplamlarla uyuşmuyor: kırılımda{" "}
              <b>{formatInt(rowMissed)}</b> kaçırılan ve <b>{formatInt(rowCorrect)}</b> doğru
              ret var, üstte ise <b>{formatInt(winners)}</b> ve <b>{formatInt(correct)}</b>{" "}
              bildirildi. Panel ikisini de gösterir; hangisinin doğru olduğunu bilmediği
              için birini seçip diğerini gizlemez.
            </p>
          )}
        </div>
      )}
    </Panel>
  );
}
