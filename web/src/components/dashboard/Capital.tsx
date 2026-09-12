import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import { Stat } from "@/components/dashboard/bits";
import {
  DASH,
  DEPLOY_BAND,
  DEPLOY_CEILING,
  formatDuration,
  formatInt,
  formatMoney,
  formatPct,
  formatPoints,
  formatRatio,
  formatSignedMicroMoney,
  toNum,
  tone,
} from "@/lib/format";
import type { CapitalState } from "@/lib/types";

/**
 * SERMAYE KULLANIMI — the headline panel.
 *
 * Phase 3 measured a peak of two concurrent positions with 99.5% of capital
 * idle, and that reading was buried inside a portfolio table nobody scrolls
 * to. So this panel exists to make one comparison unmissable: how much of the
 * account is at work, against how much was supposed to be.
 *
 * The comparison is drawn as a single proportional bar rather than as two
 * percentages side by side, because "we are at 4% of an 80% target" is a
 * subtraction the reader should not have to perform. The target band is on the
 * same track as the fill; the distance between them IS the finding.
 *
 * Every figure is optional. An absent measurement is an em dash and an absent
 * panel says it has not been measured — a screen that drew an empty bar would
 * be claiming the account is 100% idle, which is a different statement.
 */

const [BAND_LO, BAND_HI] = DEPLOY_BAND;
const TICKS = [0, 25, 50, 75, 100];

function clampPct(v: number): number {
  return Math.max(0, Math.min(100, v));
}

type DeployKind = "empty" | "under" | "in" | "over" | "full";

function deployKind(pct: number | null): DeployKind {
  if (pct === null) return "empty";
  if (pct >= DEPLOY_CEILING) return "full";
  if (pct > BAND_HI) return "over";
  if (pct < BAND_LO) return "under";
  return "in";
}

const KIND_CLASS: Record<DeployKind, string> = {
  empty: "is-empty",
  under: "is-under",
  in: "is-in",
  over: "is-over",
  full: "is-full",
};

const KIND_TEXT: Record<DeployKind, string> = {
  empty: "Kullanım oranı ölçülmedi.",
  under: `Kullanım, hedef bandın (%${BAND_LO}–${BAND_HI}) altında: sermayenin büyük bölümü çalışmıyor.`,
  in: `Kullanım hedef bandın (%${BAND_LO}–${BAND_HI}) içinde.`,
  over: `Kullanım hedef bandın üzerinde: nakit tamponu inceldi, yeni bir aday için yer kalmamış olabilir.`,
  full: `Sermayenin neredeyse tamamı piyasada. Bu noktada rotasyon değil, maruziyet riski konuşulur.`,
};

/**
 * The deployment meter.
 *
 * The band is drawn on the track itself and not in a legend: a target that
 * lives in a caption is a target the eye never compares against.
 */
function Meter({
  deployedPct,
  idlePct,
  targetPct,
  kind,
}: {
  deployedPct: number | null;
  idlePct: number | null;
  targetPct: number | null;
  kind: DeployKind;
}) {
  const fill = deployedPct === null ? 0 : clampPct(deployedPct);
  const label =
    deployedPct === null
      ? "Kullanım oranı ölçülmedi"
      : `Sermayenin ${formatPct(deployedPct)} kadarı kullanımda, ${
          idlePct === null ? "geri kalanı" : formatPct(idlePct)
        } boşta`;

  return (
    <div className={`cap-meter ${KIND_CLASS[kind]}`}>
      <div className="cap-legend">
        <span className="cap-leg is-dep">
          <i aria-hidden />
          Kullanılan <b>{deployedPct === null ? DASH : formatPct(deployedPct)}</b>
        </span>
        <span className="cap-leg is-idle">
          <i aria-hidden />
          Boşta <b>{idlePct === null ? DASH : formatPct(idlePct)}</b>
        </span>
        <span className="cap-leg is-band">
          <i aria-hidden />
          Hedef bandı <b>%{BAND_LO}–{BAND_HI}</b>
        </span>
        {targetPct !== null && (
          <span className="cap-leg is-target">
            <i aria-hidden />
            Hedef <b>{formatPct(targetPct)}</b>
          </span>
        )}
      </div>

      <div className="cap-track" role="img" aria-label={label}>
        <span
          className="cap-band"
          style={{ left: `${BAND_LO}%`, width: `${BAND_HI - BAND_LO}%` }}
          title={`Faz 5 hedef bandı: sermayenin %${BAND_LO}–${BAND_HI} kadarı piyasada`}
        />
        <span
          className={`cap-fill ${deployedPct === null ? "is-unknown" : ""}`}
          style={{ width: `${fill}%` }}
        />
        {targetPct !== null && (
          <span
            className="cap-target"
            style={{ left: `${clampPct(targetPct)}%` }}
            title={`Bildirilen hedef: ${formatPct(targetPct)}`}
          />
        )}
      </div>

      <div className="cap-scale" aria-hidden>
        {TICKS.map((t) => (
          <span
            key={t}
            className={`cap-tick${t === 0 ? " is-first" : t === 100 ? " is-last" : ""}`}
            style={{ left: `${t}%` }}
          >
            %{t}
          </span>
        ))}
      </div>
    </div>
  );
}

export function Capital({
  capital,
  loading,
}: {
  capital: CapitalState | null | undefined;
  loading: boolean;
}) {
  const c = capital ?? null;

  const equity = toNum(c?.equity);
  const deployedUsd = toNum(c?.deployed_usd);
  const idleUsd = toNum(c?.idle_usd);

  // A percentage the worker did not send is derived from the dollars when both
  // of those are present — and marked as derived, because a computed reading
  // and a reported one are not the same evidence. Nothing is derived from
  // nothing: with no equity there is no denominator and the cell stays an em
  // dash.
  const reportedDeployedPct = toNum(c?.deployed_pct);
  const reportedIdlePct = toNum(c?.idle_pct);
  const canDerive = equity !== null && equity > 0;
  const deployedPct =
    reportedDeployedPct ??
    (canDerive && deployedUsd !== null ? (deployedUsd / equity) * 100 : null);
  const idlePct =
    reportedIdlePct ?? (canDerive && idleUsd !== null ? (idleUsd / equity) * 100 : null);
  const derived =
    (reportedDeployedPct === null && deployedPct !== null) ||
    (reportedIdlePct === null && idlePct !== null);

  const targetPct = toNum(c?.target_pct);
  const kind = deployKind(deployedPct);

  // The contract says deployed + idle is the equity. When it does not, the
  // panel says so rather than quietly drawing a bar that adds up.
  const identityGap =
    equity !== null && deployedUsd !== null && idleUsd !== null
      ? deployedUsd + idleUsd - equity
      : null;
  const identityBroken = identityGap !== null && Math.abs(identityGap) > 0.005;

  const gapPoints =
    targetPct !== null && deployedPct !== null ? targetPct - deployedPct : null;
  const gapUsd =
    gapPoints !== null && equity !== null ? (gapPoints / 100) * equity : null;
  const shareOfTarget =
    targetPct !== null && targetPct > 0 && deployedPct !== null
      ? (deployedPct / targetPct) * 100
      : null;

  const meta = c ? (
    <>
      <span
        className={`pill ${
          kind === "in" ? "is-pos" : kind === "full" ? "is-neg" : kind === "empty" ? "is-off" : "is-warn"
        }`}
        title="Piyasada çalışan sermayenin payı"
      >
        {deployedPct === null ? "kullanım ölçülmedi" : `${formatPct(deployedPct)} kullanımda`}
      </span>
      {targetPct !== null && (
        <span className="panel-note" title="Bu koşunun hedeflediği kullanım oranı">
          hedef {formatPct(targetPct)}
        </span>
      )}
      {toNum(c.open_positions) !== null && (
        <span className="panel-note">{formatInt(c.open_positions)} açık pozisyon</span>
      )}
    </>
  ) : null;

  return (
    <Panel
      idx="01"
      title="Sermaye kullanımı"
      note="ne kadarı çalışıyor, ne kadarı boşta, hedefin neresindeyiz"
      meta={meta}
      footer="Kullanım oranı bir performans ölçüsü değildir: boşta duran para kaybetmez, sadece kazanmaz. Bu panel oranı hedefle birlikte gösterir çünkü asıl soru şudur — sermaye, işlem üretilemediği için mi boşta, yoksa üretilen işlemler kendi maliyetini çıkaramadığı için mi. Cevap §02'dedir. Kullanımı hedefe yaklaştırmak için eşikler indirilmez; negatif beklenen değerli bir işlem hâlâ İŞLEM YOK'tur."
    >
      {loading && !c ? (
        <Skeleton rows={5} />
      ) : !c ? (
        <DataState
          title="Sermaye kullanımı henüz ölçülmedi"
          text="Worker bu ekrana kullanım alanlarını bildirmiyor. Bu, sermayenin tamamının boşta olduğu anlamına gelmez — hiç ölçülmediği anlamına gelir, ve bu panel ölçülmemiş bir oranı sıfır olarak çizmez. Portföy katmanı boşta sermaye ayrıştırmasını üretmeye başladığında kullanılan tutar, boşta tutar ve hedef bandı burada tek bir çubuk üzerinde görünecek."
        />
      ) : (
        <div className="cap">
          <div className="cap-heads">
            <div className="cap-cell">
              <span className="cap-k">Sermaye</span>
              <span className={`cap-v ${equity === null ? "is-empty" : ""}`}>
                {formatMoney(equity)}
              </span>
              <span className="cap-n">hesabın tamamı</span>
            </div>

            <div className="cap-cell is-dep">
              <span className="cap-k">Kullanılan sermaye</span>
              <span className={`cap-v ${deployedUsd === null ? "is-empty" : "pos"}`}>
                {formatMoney(deployedUsd)}
              </span>
              <span className="cap-n">
                {deployedPct === null
                  ? "piyasada çalışan tutar"
                  : `${formatPct(deployedPct)} · piyasada çalışan tutar`}
              </span>
            </div>

            <div className={`cap-cell is-idle ${KIND_CLASS[kind]}`}>
              <span className="cap-k">Boşta sermaye</span>
              <span className={`cap-v ${idleUsd === null ? "is-empty" : ""}`}>
                {formatMoney(idleUsd)}
              </span>
              <span className="cap-n">
                {idlePct === null
                  ? "hiçbir pozisyonda değil"
                  : `${formatPct(idlePct)} · hiçbir pozisyonda değil`}
              </span>
            </div>
          </div>

          <Meter
            deployedPct={deployedPct}
            idlePct={idlePct}
            targetPct={targetPct}
            kind={kind}
          />

          <p className={`cap-read ${KIND_CLASS[kind]}`}>
            {KIND_TEXT[kind]}{" "}
            {targetPct === null
              ? `Bu koşu için hedef bildirilmedi; çubuktaki tarama Faz 5'in %${BAND_LO}–${BAND_HI} bandıdır.`
              : shareOfTarget === null
                ? `Hedef ${formatPct(targetPct)}.`
                : `Hedef ${formatPct(targetPct)}, ölçülen ${formatPct(
                    deployedPct as number,
                  )} — yani hedefin ${formatPct(shareOfTarget, 0)} kadarı. Aradaki ${formatPoints(
                    gapPoints,
                  )}${gapUsd === null ? "" : `, ${formatMoney(Math.abs(gapUsd))} demektir`}.`}
          </p>

          {identityBroken && (
            <p className="cap-warn">
              Tutarsızlık: kullanılan {formatMoney(deployedUsd)} artı boşta{" "}
              {formatMoney(idleUsd)}, bildirilen {formatMoney(equity)} sermayeye eşit
              değil — fark {formatMoney(identityGap)}. Panel bu farkı yuvarlayıp
              gizlemez; çubuk bildirilen oranlarla çizilmiştir.
            </p>
          )}

          <div className="pfolio-grid">
            <Stat
              label="Açık pozisyon"
              value={formatInt(c.open_positions)}
              note="adet · hedef 8–15"
            />
            <Stat
              label="Nitelikli fırsat"
              value={formatInt(c.qualified_opportunities)}
              note="kapıları geçen aday"
            />
            <Stat
              label="Reddedilen fırsat"
              value={formatInt(c.rejected_opportunities)}
              note="kapılarda elenen aday"
            />
            <Stat
              label="Günlük sermaye devri"
              value={formatRatio(c.turnover_per_day)}
              note="işlem hacmi / sermaye"
            />
            <Stat
              label="Ort. pozisyon"
              value={formatMoney(c.avg_position_usd)}
              note="hedef $10–50"
            />
            <Stat
              label="Ort. tutma süresi"
              value={formatDuration(c.avg_holding_seconds)}
              note="pozisyon başına"
            />
            <Stat
              label="Bugünkü NET P&L"
              value={formatSignedMicroMoney(c.net_pnl_today)}
              cls={tone(c.net_pnl_today) ?? ""}
              note="maliyetler düşülmüş"
            />
            <Stat
              label="$100 kullanılan başına NET"
              value={formatSignedMicroMoney(c.net_pnl_per_100_deployed)}
              cls={tone(c.net_pnl_per_100_deployed) ?? ""}
              note="çalışan sermayenin verimi"
              title="Boşta duran para bu oranı bozmaz; yalnızca piyasaya konan sermaye üzerinden ölçülür."
            />
          </div>

          {derived && (
            <p className="cap-note">
              Bildirilmeyen kullanım oranları tutarlardan türetildi (kullanılan ÷
              sermaye). Türetilmiş bir oran, ölçülmüş bir oranla aynı kanıt değildir;
              worker oranları bildirdiğinde bu satır kaybolur.
            </p>
          )}
        </div>
      )}
    </Panel>
  );
}
