import type { ReactNode } from "react";
import {
  DASH,
  checkpointLabel,
  formatDuration,
  makerTakerLabel,
  strengthPct,
} from "@/lib/format";
import type { Num } from "@/lib/types";

/**
 * The small parts the two cost tables share.
 *
 * They live together because they encode one argument each, and that argument
 * has to read the same in İŞLEM GEÇMİŞİ as it does in AÇIK POZİSYONLAR: a
 * maker fill is not a taker fill, 26 minutes is not 4 minutes, and a cost is
 * not income.
 */

/** The 25-minute checkpoint is a checkpoint, not a lock — but a DUE one is
    a position the engine owes an answer on, so it is the only warning tone. */
export function CheckpointBadge({ status }: { status: string | null | undefined }) {
  if (!status) return <span className="muted">{DASH}</span>;
  const key = String(status).toUpperCase();
  const cls =
    key === "DUE"
      ? "is-due"
      : key === "TRAIL"
        ? "is-trail"
        : key === "KEEP"
          ? "is-keep"
          : "is-pending";
  return (
    <span className={`chk ${cls}`} title={key}>
      {checkpointLabel(key)}
    </span>
  );
}

/** Maker is 0% on this venue. The badge carries the fee consequence, which is
    why only MAKER gets the positive tone. */
export function MakerTakerBadge({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="muted">{DASH}</span>;
  const key = String(value).toUpperCase();
  return (
    <span
      className={`mt ${key === "MAKER" ? "is-maker" : "is-taker"}`}
      title={key === "MAKER" ? "Maker dolum — komisyon %0" : "Taker dolum — komisyon ödendi"}
    >
      {makerTakerLabel(key)}
    </span>
  );
}

const CHECKPOINT_MIN = 25;
const TIME_STOP_MIN = 60;

/**
 * Where a position sits inside its 25–60 minute window.
 *
 * The track is the whole 60-minute horizon with the checkpoint marked at 25.
 * Reading "17 dk" as a number tells you nothing without both boundaries in
 * view; reading it as a position on this track tells you immediately whether
 * the engine still has room or is about to be forced out by the time stop.
 */
export function HorizonMeter({ ageSeconds }: { ageSeconds: Num }) {
  if (ageSeconds === null || ageSeconds === undefined) {
    return <span className="muted">{DASH}</span>;
  }
  const minutes = ageSeconds / 60;
  const phase =
    minutes >= TIME_STOP_MIN ? "over" : minutes >= CHECKPOINT_MIN ? "mid" : "early";
  const fill = Math.max(1.5, Math.min(100, (minutes / TIME_STOP_MIN) * 100));

  return (
    <span className="horizon" title={`${formatDuration(ageSeconds)} · 25 dk kontrol · 60 dk zaman stopu`}>
      <span className={`horizon-track is-${phase}`}>
        <span className="horizon-fill" style={{ width: `${fill}%` }} />
        <span
          className="horizon-tick"
          style={{ left: `${(CHECKPOINT_MIN / TIME_STOP_MIN) * 100}%` }}
          aria-hidden
        />
      </span>
      <span className={`horizon-val is-${phase}`}>{formatDuration(ageSeconds)}</span>
    </span>
  );
}

/** The ranker score behind the entry, as a bar plus its own reading. */
export function StrengthBar({ value }: { value: Num }) {
  const pct = strengthPct(value);
  if (pct === null) return <span className="muted">{DASH}</span>;
  return (
    <span className="strength" title="Sinyal gücü">
      <span className="strength-track">
        <span className="strength-fill" style={{ width: `${pct}%` }} />
      </span>
      <span className="strength-val">{pct.toFixed(0)}</span>
    </span>
  );
}

export interface CostItem {
  k: string;
  v: string;
  /** Tone class for the value: pos / neg / dim / muted / warn. */
  cls?: string;
  title?: string;
}

/**
 * The expanded cost breakdown.
 *
 * Laid out as a wrapping grid rather than as more table columns, because the
 * per-trade cost stack is fifteen figures and a fifteen-column table is a
 * horizontal scrollbar pretending to be a report. Each group keeps its own
 * heading so the arithmetic — gross, minus costs, equals net — is visible as
 * a shape and not only as numbers.
 */
export function CostGroup({
  title,
  items,
  wide,
}: {
  title: string;
  items: CostItem[];
  wide?: boolean;
}) {
  return (
    <div className={`cgroup${wide ? " is-wide" : ""}`}>
      <p className="cgroup-t">{title}</p>
      <dl className="cgrid">
        {items.map((it) => (
          <div className="citem" key={it.k}>
            <dt>{it.k}</dt>
            <dd className={it.cls} title={it.title}>
              {it.v}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/**
 * The three-term identity, inline in an expanded row: what the move earned,
 * what it cost to capture, what is left. The minus and the equals sign are
 * drawn because they are the point — this is an equation, not three stats.
 */
export function NetEquation({
  gross,
  cost,
  net,
  grossCls,
  netCls,
}: {
  gross: string;
  cost: string;
  net: string;
  grossCls?: string;
  netCls?: string;
}) {
  return (
    <div className="eq">
      <span className="eq-cell">
        <span className="eq-k">Brüt P&amp;L</span>
        <span className={`eq-v ${grossCls ?? ""}`}>{gross}</span>
      </span>
      <span className="eq-op" aria-hidden>
        −
      </span>
      <span className="eq-cell">
        <span className="eq-k">Toplam işlem maliyeti</span>
        <span className="eq-v neg">{cost}</span>
      </span>
      <span className="eq-op" aria-hidden>
        =
      </span>
      <span className="eq-cell is-final">
        <span className="eq-k">NET P&amp;L</span>
        <span className={`eq-v ${netCls ?? ""}`}>{net}</span>
      </span>
    </div>
  );
}

/** The affordance on every expandable row. */
export function Chevron({ open }: { open: boolean }) {
  return (
    <span className={`chev${open ? " is-open" : ""}`} aria-hidden>
      ▸
    </span>
  );
}

/** A labelled figure inside a panel body — used by the portfolio grid. */
export function Stat({
  label,
  value,
  note,
  cls,
  title,
  children,
}: {
  label: string;
  value?: string;
  note?: string;
  cls?: string;
  title?: string;
  children?: ReactNode;
}) {
  return (
    <div className="pstat" title={title}>
      <span className="pstat-k">{label}</span>
      {children ?? <span className={`pstat-v ${cls ?? ""}`}>{value ?? DASH}</span>}
      <span className="pstat-n">{note ?? ""}</span>
    </div>
  );
}
