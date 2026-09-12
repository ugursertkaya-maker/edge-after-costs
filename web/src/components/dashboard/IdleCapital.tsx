import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import {
  DASH,
  IDLE_GROUP_LABELS,
  IDLE_GROUP_NOTES,
  IDLE_GROUP_ORDER,
  IDLE_GROUP_VERDICTS,
  IDLE_REASONS_ALL,
  IDLE_REASON_NOTES,
  formatMoney,
  formatPct,
  formatRate,
  idleGroup,
  idleReasonLabel,
  toNum,
} from "@/lib/format";
import type { IdleGroup } from "@/lib/format";
import { reconcileIdle } from "@/lib/phase6";
import type { CapitalState, IdleByReason } from "@/lib/types";

/**
 * NEDEN SERMAYE BOŞTA — the panel the phase is actually decided on.
 *
 * The requirement is one sentence: *if $350 is idle, say exactly why.* So the
 * reasons are reported in DOLLARS, never as percentages of a percentage —
 * "%14 of the idle 95%" is a number nobody can act on, and $47.20 is.
 *
 * Phase 6 EXTENDS this panel rather than adding a second one beside it. The
 * six Phase 5 reasons became nine — COOLDOWN, SCANNER_DELAY, CAPITAL_STATE_BUG
 * and OTHER — and the fourth group they brought with them is a defect report:
 * `SCANNER_DELAY` and `CAPITAL_STATE_BUG` say the money is idle because WE are
 * broken, which is neither a market reading nor a portfolio decision. There is
 * exactly one idle attribution on this screen, because the same idle dollar
 * explained twice in two panels is how a dashboard starts disagreeing with
 * itself.
 *
 * The reasons do not mean the same kind of thing, and the layout says so
 * before the reader has looked at a legend. A tall NO_QUALIFIED_SIGNAL bar
 * means there was nothing to buy: widening the universe is exactly the fix. A
 * tall EXECUTION_COST bar means there WERE candidates and they were not worth
 * their own round trip: widening the universe does not help and plausibly
 * hurts, because every non-Tier-0 pair costs ~20 bps against ~2 bps on
 * Tier-0. Those are opposite conclusions from the same idle balance, so they
 * are grouped, coloured and captioned as opposites.
 *
 * The payload contract guarantees the reasons sum to the idle balance. When a
 * payload arrives where they do not, the discrepancy is printed — hiding it
 * behind a rounded total would make a broken measurement look like a clean
 * one.
 */

interface Row {
  code: string;
  usd: number;
  group: IdleGroup | null;
}

/** A near-tie is not a dominant reason, and calling one a finding when the
    runner-up is within a tenth of it would be the panel over-reading. */
const TIE_SHARE = 0.1;

function groupClass(g: IdleGroup | null): string {
  return `is-${g ?? "other"}`;
}

function Dominant({ rows, basis }: { rows: Row[]; basis: number }) {
  const top = rows[0];
  const second = rows[1];
  if (!top || top.usd <= 0 || basis <= 0) return null;

  const share = top.usd / basis;
  const tie =
    second && second.usd > 0 && (top.usd - second.usd) / basis < TIE_SHARE ? second : null;

  return (
    <div className={`idom ${groupClass(top.group)}`}>
      <span className="idom-eye">Boşta sermayenin en büyük sebebi</span>
      <div className="idom-head">
        <span className="idom-name" title={top.code}>
          {idleReasonLabel(top.code)}
        </span>
        <span className="idom-v">{formatMoney(top.usd)}</span>
        <span className="idom-p">{formatRate(share, 0)}</span>
      </div>
      <p className="idom-x">{IDLE_REASON_NOTES[top.code] ?? "Bu sebep için açıklama tanımlı değil; kod olduğu gibi gösteriliyor."}</p>
      {top.group && <p className="idom-verdict">{IDLE_GROUP_VERDICTS[top.group]}</p>}
      {tie && (
        <p className="idom-tie">
          Baskınlık net değil: <b>{idleReasonLabel(tie.code)}</b> {formatMoney(tie.usd)} ile
          başa baş. İki sebep birbirine bu kadar yakınken tek bir sonuç çıkarmak, ölçümün
          söylediğinden fazlasını söylemek olur.
        </p>
      )}
    </div>
  );
}

function Attribution({
  byReason,
  idleUsd,
  idlePct,
}: {
  byReason: IdleByReason;
  idleUsd: number | null;
  idlePct: number | null;
}) {
  const rows: Row[] = Object.keys(byReason)
    .map((code) => ({ code, usd: toNum(byReason[code]), group: idleGroup(code) }))
    .filter((r): r is Row => r.usd !== null)
    .sort((a, b) => b.usd - a.usd);

  const recon = reconcileIdle(byReason, idleUsd, IDLE_REASONS_ALL);
  const missing = recon.missing;

  if (rows.length === 0) {
    return (
      <DataState
        title="Ayrıştırma kaydı boş geldi"
        text="Boşta sermaye ayrıştırması bildirildi ama içinde tek bir sebep yok. Bu bir dağılım değil, boş bir kayıttır — ve boş bir kayıt, sermayenin sebepsiz boşta durduğu anlamına gelmez."
      />
    );
  }

  // The bars are drawn against what the reasons actually add up to. The
  // reported idle balance is printed next to it, and when the two disagree the
  // strip below says by how much.
  const sum = recon.sum;
  const positive = rows.reduce((a, r) => a + Math.max(0, r.usd), 0);
  const basis = positive > 0 ? positive : 0;
  // Signed so the direction survives: POSITIVE means idle cash that nobody
  // attributed to anything, which is the defect §22 exists to catch.
  const unexplained = recon.unexplained;
  const mismatch = recon.mismatch;

  const groups = IDLE_GROUP_ORDER.map((g) => ({
    group: g,
    usd: rows.filter((r) => r.group === g).reduce((a, r) => a + Math.max(0, r.usd), 0),
  }));
  const unclassified = rows
    .filter((r) => r.group === null)
    .reduce((a, r) => a + Math.max(0, r.usd), 0);

  if (basis <= 0) {
    return (
      <div className="idle">
        <div className="idle-head">
          <span className="idle-total">{formatMoney(idleUsd ?? sum)}</span>
          <span className="idle-total-k">boşta sermaye</span>
        </div>
        <p className="xnote">
          Bildirilen sebeplerin tamamı sıfır. Bu, boşta sermaye olmadığı okumasıdır:
          hesabın tamamı ya piyasada ya da bu ayrıştırmanın dışında. Sıfır bir ölçümdür
          ve bu panelde eksik bir ölçümle aynı şekilde gösterilmez.
        </p>
      </div>
    );
  }

  return (
    <div className="idle">
      <div className="idle-head">
        <span className="idle-total">{formatMoney(idleUsd ?? sum)}</span>
        <span className="idle-total-k">
          boşta sermaye{idlePct === null ? "" : ` · sermayenin ${formatPct(idlePct)} kadarı`}
        </span>
        {idleUsd === null && (
          <span className="idle-total-n" title="Toplam bakiye bildirilmedi">
            sebeplerin toplamı
          </span>
        )}
      </div>

      {mismatch && unexplained !== null && (
        <div className="idle-gap" role="alert">
          <span className="idle-gap-tag">
            {unexplained > 0 ? "AÇIKLANAMAYAN NAKİT" : "FAZLA YAZILMIŞ NAKİT"}
          </span>
          <span className="idle-gap-v">{formatMoney(Math.abs(unexplained))}</span>
          <p className="idle-gap-x">
            Sözleşme sebeplerin boşta bakiyeye eşit olmasını garanti eder, bu veride
            etmiyor: sebepler toplamı <b>{formatMoney(sum)}</b>, bildirilen boşta bakiye{" "}
            <b>{formatMoney(idleUsd)}</b>.{" "}
            {unexplained > 0 ? (
              <>
                Yani <b>{formatMoney(unexplained)}</b> boşta duruyor ve hiçbir sebebe
                yazılmamış. Bu para bir yerde çalışmıyor ve neden çalışmadığını kimse
                söylemiyor — §22'nin kovaladığı tek kusur tam olarak budur.
              </>
            ) : (
              <>
                Sebepler, bildirilen boşta bakiyeden <b>{formatMoney(-unexplained)}</b>{" "}
                fazlasını açıklıyor. Bir dolar iki sebebe birden yazılmış ya da bakiye
                eksik bildirilmiş olmalı; her iki hâlde de aşağıdaki paylar olduğundan
                küçüktür.
              </>
            )}{" "}
            Aşağıdaki çubuklar sebeplerin kendi toplamına göre çizilmiştir; fark
            yuvarlanarak gizlenmemiştir çünkü bu bir görüntü sorunu değil, ölçüm
            kusurudur.
          </p>
        </div>
      )}

      <Dominant rows={rows} basis={basis} />

      <div className="idle-stack" role="img" aria-label="Boşta sermayenin sebep gruplarına dağılımı">
        {groups.map(
          (g) =>
            g.usd > 0 && (
              <span
                key={g.group}
                className={`idle-seg ${groupClass(g.group)}`}
                style={{ flexGrow: g.usd }}
                title={`${IDLE_GROUP_LABELS[g.group]} · ${formatMoney(g.usd)} · ${formatRate(
                  g.usd / basis,
                  0,
                )}`}
              >
                {formatRate(g.usd / basis, 0)}
              </span>
            ),
        )}
        {unclassified > 0 && (
          <span
            className="idle-seg is-other"
            style={{ flexGrow: unclassified }}
            title={`Sınıflandırılmamış sebepler · ${formatMoney(unclassified)}`}
          >
            {formatRate(unclassified / basis, 0)}
          </span>
        )}
      </div>

      <div className="idle-groups">
        {groups.map((g) => (
          <div key={g.group} className={`igrp ${groupClass(g.group)}`}>
            <div className="igrp-head">
              <i aria-hidden />
              <span className="igrp-k">{IDLE_GROUP_LABELS[g.group]}</span>
              <span className="igrp-v">{formatMoney(g.usd)}</span>
            </div>
            <p className="igrp-x">{IDLE_GROUP_NOTES[g.group]}</p>
            <p className="igrp-verdict">{IDLE_GROUP_VERDICTS[g.group]}</p>
          </div>
        ))}
        {unclassified > 0 && (
          <div className="igrp is-other">
            <div className="igrp-head">
              <i aria-hidden />
              <span className="igrp-k">SINIFLANDIRILMAMIŞ</span>
              <span className="igrp-v">{formatMoney(unclassified)}</span>
            </div>
            <p className="igrp-x">
              Worker, bu ekranın tanımadığı bir sebep kodu bildirdi. Kod olduğu gibi
              gösterilir; tanınmayan bir sebep gizlenmez.
            </p>
          </div>
        )}
      </div>

      <ul className="idle-list">
        {rows.map((r) => (
          <li key={r.code} className={`idle-row ${groupClass(r.group)}`}>
            <span className="idle-k" title={IDLE_REASON_NOTES[r.code] ?? r.code}>
              {idleReasonLabel(r.code)}
            </span>
            <span className="idle-bar">
              <span
                className={`idle-fill ${groupClass(r.group)}`}
                style={{ width: `${(Math.max(0, r.usd) / basis) * 100}%` }}
              />
            </span>
            <span className="idle-n">{formatMoney(r.usd)}</span>
            <span className="idle-p">{formatRate(Math.max(0, r.usd) / basis, 0)}</span>
          </li>
        ))}
      </ul>

      {missing.length > 0 && (
        <p className="idle-missing">
          Bildirilmeyen sebepler ({missing.length}):{" "}
          {missing.map((m) => idleReasonLabel(m)).join(" · ")}. Bildirilmemiş bir sebep
          sıfır dolar sayılmaz; ayrıştırmada hiç görünmemiştir ve bu iki ifade bu
          panelde birbirinin yerine kullanılmaz.
        </p>
      )}
    </div>
  );
}

export function IdleCapital({
  capital,
  loading,
}: {
  capital: CapitalState | null | undefined;
  loading: boolean;
}) {
  const c = capital ?? null;
  const byReason = c?.idle_by_reason ?? null;
  const idleUsd = toNum(c?.idle_usd);
  const idlePct = toNum(c?.idle_pct);

  const meta = byReason ? (
    <span className="pill is-off" title="Ayrıştırmada bildirilen sebep sayısı">
      {Object.keys(byReason).filter((k) => toNum(byReason[k]) !== null).length} sebep
      bildirildi
    </span>
  ) : null;

  return (
    <Panel
      idx="02"
      title="Neden sermaye boşta"
      note="boşta bakiyenin sebep sebep dolar dağılımı"
      meta={meta}
      footer="Sebepler dolar cinsindendir çünkü karar dolar cinsindendir: bir yüzdenin yüzdesi üzerine işlem yapılamaz. Bu panelin ayırdığı şey tek bir cümledir — NİTELİKLİ SİNYAL YOK, alınacak bir şey olmadığını söyler ve evreni genişletmek tam olarak bunun çözümüdür; İCRA MALİYETİ ise adayların var olduğunu ve kendi gidiş-dönüş maliyetini çıkaramadığını söyler, ki bu durumda evreni genişletmek yardımcı olmaz, daha pahalı çiftler ekleyerek durumu kötüleştirir. İkisi aynı boşta bakiyeden çıkan zıt sonuçlardır. Reddedilen adayların sonradan ne yaptığı §16'da ölçülür."
    >
      {loading && !c ? (
        <Skeleton rows={5} />
      ) : !c ? (
        <div className="fin-empty">
          <div className="fin-empty-head">
            <span className="state-mark" aria-hidden />
            <p className="fin-empty-t">Boşta sermaye henüz ayrıştırılmadı</p>
          </div>
          <p className="fin-empty-x">
            Portföy katmanı bu ekrana bir ayrıştırma bildirmiyor. Bu, sermayenin sebepsiz
            boşta durduğu anlamına gelmez — henüz hiçbir doların bir sebebe yazılmadığı
            anlamına gelir, ve bu iki ifade burada birbirinin yerine kullanılmaz.
            Ayrıştırma geldiğinde her boşta dolar aşağıdaki dokuz sebepten birine
            yazılacak ve hangisinin baskın olduğu bu fazın cevabı olacak.
          </p>
          <ReasonReference />
        </div>
      ) : !byReason ? (
        <div className="fin-empty">
          <div className="fin-empty-head">
            <span className="state-mark" aria-hidden />
            <p className="fin-empty-t">Kullanım bildirildi, ayrıştırma bildirilmedi</p>
          </div>
          <p className="fin-empty-x">
            Sermaye kullanımı ölçüldü{idleUsd === null ? "" : ` ve ${formatMoney(idleUsd)} boşta görünüyor`}
            , ancak bu tutarın sebep dağılımı gelmedi. Toplamı bilmek, sebebini bilmek
            değildir: bu panelin var oluş sebebi tam olarak o farktır.
          </p>
          <ReasonReference />
        </div>
      ) : (
        <Attribution byReason={byReason} idleUsd={idleUsd} idlePct={idlePct} />
      )}
    </Panel>
  );
}

/**
 * The six reasons, printed whenever there is no attribution to show.
 *
 * Same job as the gate list in the FİNALİSTLER panel: an empty panel only
 * means something if the vocabulary it will be filled with is visible next to
 * it. It is also the fastest way to see that these six are not interchangeable.
 */
function ReasonReference() {
  return (
    <div className="gateref">
      <p className="gateref-t">Her boşta dolar bu dokuz sebepten birine yazılır</p>
      <ol className="gateref-l">
        {IDLE_REASONS_ALL.map((code, i) => (
          <li key={code}>
            <span className="gateref-n">{String(i + 1).padStart(2, "0")}</span>
            <span>
              <b className={`iref ${groupClass(idleGroup(code))}`}>{idleReasonLabel(code)}</b>{" "}
              — {IDLE_REASON_NOTES[code] ?? DASH}
            </span>
          </li>
        ))}
      </ol>
      <p className="gateref-f">
        Dokuzu da &ldquo;para çalışmıyor&rdquo; der, ama farklı şeyler ister:
        NİTELİKLİ SİNYAL YOK daha geniş bir evren ister, İCRA MALİYETİ ve LİKİDİTE
        SINIRI daha ucuz bir borsa ister, korelasyon/risk/rezerv/soğuma ise hiçbir şey
        istemez — onlar zaten verilmiş kararlardır. TARAYICI GECİKMESİ ve SERMAYE
        DURUMU KUSURU ise piyasa hakkında hiçbir şey söylemez; onlar bizim hatamızdır
        ve sıfırlanana kadar diğerlerinin okumasını da kirletirler.
      </p>
    </div>
  );
}
