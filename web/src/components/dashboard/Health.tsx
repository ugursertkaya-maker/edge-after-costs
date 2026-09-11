import { Panel } from "@/components/ui/Panel";
import { Skeleton } from "@/components/ui/DataState";
import {
  DASH,
  formatDateTimeMs,
  formatHours,
  formatInt,
  formatMb,
  formatMs,
  formatNumber,
  formatPct,
  formatRelativeMs,
  toNum,
} from "@/lib/format";
import type { HealthState, Num, RecorderState, TapeState } from "@/lib/types";

/**
 * Tape retention on the server is 14 days (`app/recorder.py`, hourly gzip
 * NDJSON), so that window — not an invented target — is what the collected
 * hours are drawn against.
 */
const RETENTION_HOURS = 14 * 24;

/**
 * The tape recorder: research infrastructure, not trading infrastructure.
 *
 * A 1-minute bar contains no queue position, which is why maker-first is the
 * one structure V1 and V2 never properly tested. The tape is how that gets
 * tested at all, so "how many hours of tape exist" is the maker study's
 * progress bar and belongs on the same screen as the rest of the machine's
 * vital signs rather than in a log nobody reads.
 */
function Tape({
  recorder,
  tape,
}: {
  recorder: RecorderState | null | undefined;
  tape: TapeState | null | undefined;
}) {
  if (!recorder && !tape) {
    return (
      <div className="tape">
        <div className="tape-head">
          <span className="tape-t">Tape kaydedici</span>
          <span className="pill is-off">bildirilmiyor</span>
        </div>
        <p className="tape-note">
          Worker kaydedici durumunu henüz raporlamıyor. Bu, kaydedicinin kapalı olduğu
          anlamına gelmez — durumunun bilinmediği anlamına gelir, ve o ikisi burada
          aynı şey olarak gösterilmez.
        </p>
      </div>
    );
  }

  const enabled = recorder?.enabled;
  const hours = toNum(tape?.hours);
  const pct = hours === null ? 0 : Math.max(0, Math.min(100, (hours / RETENTION_HOURS) * 100));

  return (
    <div className="tape">
      <div className="tape-head">
        <span className="tape-t">Tape kaydedici</span>
        <span
          className={`pill ${enabled === true ? "is-pos" : enabled === false ? "is-warn" : "is-off"}`}
          title="Maker çalışması için ham tape kaydı"
        >
          {enabled === true ? "KAYDEDİYOR" : enabled === false ? "KAPALI" : DASH}
        </span>
        <span className="tape-n">
          {hours === null ? "kaset ölçülmedi" : `${formatHours(hours)} kaset`}
        </span>
      </div>

      {enabled === false && recorder?.disabled_reason && (
        <p className="tape-note is-warn">Kapalı: {recorder.disabled_reason}</p>
      )}

      <div className="tape-track" title="14 günlük saklama penceresine göre biriken kaset">
        <span className="tape-fill" style={{ width: `${pct}%` }} />
      </div>
      <div className="tape-scale">
        <span>0</span>
        <span>14 günlük saklama penceresi</span>
      </div>

      <div className="tape-grid">
        <span className="tape-item">
          <span className="tape-k">Mesaj</span>
          <span className="tape-v">{formatInt(recorder?.messages)}</span>
        </span>
        <span className="tape-item">
          <span className="tape-k">Yazılan</span>
          <span className="tape-v">{formatMb(recorder?.mb_written)}</span>
        </span>
        <span className="tape-item">
          <span className="tape-k">Dosya</span>
          <span className="tape-v">{formatInt(tape?.files)}</span>
        </span>
        <span className="tape-item">
          <span className="tape-k">Disk</span>
          <span className="tape-v">{formatMb(tape?.mb)}</span>
        </span>
      </div>

      <p className="tape-note">
        {toNum(tape?.oldest) === null && toNum(tape?.newest) === null
          ? "Kaset aralığı bildirilmedi."
          : `En eski ${formatDateTimeMs(tape?.oldest)} · en yeni ${formatDateTimeMs(tape?.newest)}`}
      </p>
    </div>
  );
}

type Tone = "pos" | "neg" | "warn" | "empty" | undefined;

function Cell({
  label,
  value,
  tone,
  title,
}: {
  label: string;
  value: string;
  tone?: Tone;
  title?: string;
}) {
  const cls = value === DASH ? "is-empty" : tone ? `is-${tone}` : "";
  return (
    <div className="hcell" title={title}>
      <span className="hcell-k">{label}</span>
      <span className={`hcell-v ${cls}`}>
        {tone === "pos" || tone === "neg" ? (
          <span className="dot" aria-hidden />
        ) : null}
        {value}
      </span>
    </div>
  );
}

function link(ok: boolean | undefined): { text: string; tone: Tone } {
  if (ok === undefined) return { text: DASH, tone: "empty" };
  return ok ? { text: "BAĞLI", tone: "pos" } : { text: "KOPUK", tone: "neg" };
}

/**
 * The machine's own vital signs.
 *
 * Book freshness is the one that can quietly cost money: a connected socket
 * that stopped delivering still reads "BAĞLI", so the age of the book is
 * shown next to it and turns amber long before anything else notices.
 */
export function Health({
  health,
  mode,
  now,
  loading,
}: {
  health: HealthState | null;
  mode: string | null;
  now: number;
  loading: boolean;
}) {
  if (loading && !health) {
    return (
      <Panel idx="08" title="Sistem sağlığı" flush>
        <Skeleton rows={4} />
      </Panel>
    );
  }

  const h = health;
  const ws = link(h?.ws_connected);
  const rest = link(h?.rest_ok);
  const db = link(h?.db_ok);

  const bookAge: Num = h?.book_age_ms ?? null;
  const bookTone: Tone =
    bookAge === null ? "empty" : bookAge > 2000 ? "neg" : bookAge > 750 ? "warn" : "pos";

  const cpu = h?.worker_cpu_pct ?? null;
  const cpuTone: Tone = cpu === null ? "empty" : cpu > 85 ? "neg" : cpu > 65 ? "warn" : undefined;

  // The worker container is capped at 768 MB on the production host, so the
  // thresholds below are that cap, not an abstract percentage.
  const mem = h?.worker_mem_mb ?? null;
  const memTone: Tone = mem === null ? "empty" : mem > 690 ? "neg" : mem > 600 ? "warn" : undefined;

  const dropped = h?.dropped ?? null;
  const lastEval = h?.last_evaluation_ms ?? null;
  const evalTone: Tone =
    lastEval === null ? "empty" : now - lastEval > 30000 ? "neg" : now - lastEval > 10000 ? "warn" : undefined;

  return (
    <Panel
      idx="08"
      title="Sistem sağlığı"
      note="worker · besleme · veritabanı · tape"
      flush
      footer="Bağlı bir soket veri akıtmayı sessizce bırakabilir; bu yüzden bağlantı durumu tek başına yeterli değil, kitap tazeliği ile birlikte okunur. Bayat kitapla karar üretilmez."
    >
      <div className="health-grid">
        <Cell label="WebSocket" value={ws.text} tone={ws.tone} />
        <Cell label="REST" value={rest.text} tone={rest.tone} />
        <Cell
          label="Kitap tazeliği"
          value={formatMs(bookAge)}
          tone={bookTone}
          title="Son emir defteri güncellemesinin üzerinden geçen süre"
        />
        <Cell
          label="Güncelleme / sn"
          value={formatNumber(h?.updates_per_sec, 1)}
          title="Saniyede işlenen besleme mesajı"
        />
        <Cell label="Reconnect" value={formatInt(h?.reconnects)} />
        <Cell
          label="Düşen mesaj"
          value={formatInt(dropped)}
          tone={dropped !== null && dropped > 0 ? "warn" : undefined}
        />
        <Cell label="Worker CPU" value={formatPct(cpu)} tone={cpuTone} />
        <Cell
          label="Worker RAM"
          value={mem === null ? DASH : `${formatNumber(mem, 0)} MB`}
          tone={memTone}
          title="Konteyner limiti 768 MB"
        />
        <Cell label="Veritabanı" value={db.text} tone={db.tone} />
        <Cell
          label="Son değerlendirme"
          value={formatRelativeMs(lastEval, now)}
          tone={evalTone}
        />
        <Cell label="Mod" value={h?.mode ?? mode ?? DASH} />
      </div>

      <Tape recorder={h?.recorder} tape={h?.tape} />
    </Panel>
  );
}
