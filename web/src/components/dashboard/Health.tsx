import { Panel } from "@/components/ui/Panel";
import { Skeleton } from "@/components/ui/DataState";
import {
  DASH,
  formatInt,
  formatMs,
  formatNumber,
  formatPct,
  formatRelativeMs,
} from "@/lib/format";
import type { HealthState, Num } from "@/lib/types";

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
      note="worker · besleme · veritabanı"
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
    </Panel>
  );
}
