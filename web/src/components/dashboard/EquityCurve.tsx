import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import { formatClockMs, formatMoney, formatSignedMicroMoney } from "@/lib/format";
import type { EquityCurve as Curve, Num, SeriesPoint } from "@/lib/types";

const W = 1000;
const H = 230;
const PAD = { top: 14, right: 66, bottom: 22, left: 8 };

interface Scale {
  x: (ts: number) => number;
  y: (v: number) => number;
}

function linePath(points: SeriesPoint[], s: Scale): string {
  return points
    .map((p, i) => `${i === 0 ? "M" : "L"}${s.x(p[0]).toFixed(2)},${s.y(p[1]).toFixed(2)}`)
    .join(" ");
}

/** The line closed down to the baseline, so the area can be filled. */
function areaPath(points: SeriesPoint[], s: Scale, baseY: number): string {
  if (points.length === 0) return "";
  const first = s.x(points[0][0]).toFixed(2);
  const last = s.x(points[points.length - 1][0]).toFixed(2);
  return `${linePath(points, s)} L${last},${baseY.toFixed(2)} L${first},${baseY.toFixed(2)} Z`;
}

/**
 * The equity curve, drawn by hand as inline SVG.
 *
 * No chart library: this is one series against one reference line and the
 * bundle cost of a charting runtime buys nothing here. Drawing it directly
 * also lets the baseline do the work it has to do — the $500 line is the axis
 * that matters, not zero, because the only question this panel answers is
 * "are we above or below where we started".
 *
 * Two series are shown deliberately: `points` is mark-to-market equity
 * (realised + open positions) and `realized` is booked P&L only. When they
 * diverge, the gap is exactly the unrealised exposure — a fact the metric
 * strip cannot show.
 */
export function EquityCurve({
  curve,
  startingEquity,
  loading,
}: {
  curve: Curve | null;
  startingEquity: Num;
  loading: boolean;
}) {
  const points = curve?.points ?? [];
  const realized = curve?.realized ?? [];
  const boundaries = curve?.version_boundaries ?? [];
  const base = startingEquity ?? 500;

  const all = [...points, ...realized];
  const hasData = points.length >= 2;

  let body: React.ReactNode;

  if (loading && !curve) {
    body = <Skeleton rows={5} />;
  } else if (!hasData) {
    body = (
      <DataState
        scanning
        title="Sermaye eğrisi henüz çizilmedi"
        text={`Eğri ${formatMoney(base, 0)} başlangıç sermayesinden başlar ve ilk kapanan turdan itibaren şekillenir. Şu an gösterilecek iki noktadan az veri var.`}
      />
    );
  } else {
    const xs = all.map((p) => p[0]);
    const ys = all.map((p) => p[1]);
    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    // The baseline is always inside the frame: an equity curve that never
    // shows where it started cannot be read.
    const minY = Math.min(...ys, base);
    const maxY = Math.max(...ys, base);
    const spanY = maxY - minY || Math.max(1, base * 0.01);
    const padY = spanY * 0.12;
    const lo = minY - padY;
    const hi = maxY + padY;
    const spanX = maxX - minX || 1;

    const plotW = W - PAD.left - PAD.right;
    const plotH = H - PAD.top - PAD.bottom;

    const s: Scale = {
      x: (ts) => PAD.left + ((ts - minX) / spanX) * plotW,
      y: (v) => PAD.top + (1 - (v - lo) / (hi - lo)) * plotH,
    };

    const baseY = s.y(base);
    const last = points[points.length - 1];
    const lastY = s.y(last[1]);
    const delta = last[1] - base;
    const up = delta >= 0;

    body = (
      <>
        <div className="curve-legend">
          <span className="legend-item">
            <span
              className="legend-swatch"
              style={{ borderTopColor: up ? "var(--pos)" : "var(--neg)" }}
            />
            Piyasa değeri (gerçekleşmemiş dahil)
          </span>
          <span className="legend-item">
            <span
              className="legend-swatch"
              style={{ borderTopColor: "var(--info)", borderTopStyle: "dashed" }}
            />
            Gerçekleşmiş P&amp;L
          </span>
          <span className="legend-item">
            <span
              className="legend-swatch"
              style={{ borderTopColor: "var(--fg-4)", borderTopStyle: "dotted" }}
            />
            Başlangıç {formatMoney(base, 0)}
          </span>
          {boundaries.length > 0 && (
            <span className="legend-item">
              <span
                className="legend-swatch"
                style={{ borderTopColor: "var(--warn)" }}
              />
              Simülatör sürümü değişti
            </span>
          )}
        </div>

        <svg
          className="curve"
          viewBox={`0 0 ${W} ${H}`}
          preserveAspectRatio="none"
          role="img"
          aria-label={`Sermaye eğrisi: ${formatMoney(base, 0)} başlangıçtan ${formatMoney(last[1])} seviyesine`}
        >
          <defs>
            {/* The fill is split at the baseline so gains read green and
                drawdowns read red without two separate series. */}
            <clipPath id="cb-above">
              <rect x="0" y="0" width={W} height={Math.max(0, baseY)} />
            </clipPath>
            <clipPath id="cb-below">
              <rect x="0" y={baseY} width={W} height={Math.max(0, H - baseY)} />
            </clipPath>
            <linearGradient id="cb-grad-up" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--pos)" stopOpacity="0.22" />
              <stop offset="100%" stopColor="var(--pos)" stopOpacity="0.01" />
            </linearGradient>
            <linearGradient id="cb-grad-down" x1="0" y1="1" x2="0" y2="0">
              <stop offset="0%" stopColor="var(--neg)" stopOpacity="0.22" />
              <stop offset="100%" stopColor="var(--neg)" stopOpacity="0.01" />
            </linearGradient>
          </defs>

          <path
            d={areaPath(points, s, baseY)}
            fill="url(#cb-grad-up)"
            clipPath="url(#cb-above)"
          />
          <path
            d={areaPath(points, s, baseY)}
            fill="url(#cb-grad-down)"
            clipPath="url(#cb-below)"
          />

          {/* Version boundaries: everything left of a marker was produced by a
              different simulator, so the two halves are not one experiment. */}
          {boundaries.map(([ts, version]) => {
            if (ts < minX || ts > maxX) return null;
            const x = s.x(ts);
            return (
              <g key={`${ts}-${version}`}>
                <line
                  x1={x}
                  y1={PAD.top - 6}
                  x2={x}
                  y2={H - PAD.bottom}
                  stroke="var(--warn)"
                  strokeWidth="1"
                  strokeDasharray="3 3"
                  opacity="0.75"
                />
                <text
                  x={x + 4}
                  y={PAD.top + 1}
                  fill="var(--warn)"
                  fontSize="9"
                  fontFamily="var(--font-mono)"
                  opacity="0.9"
                >
                  {version}
                </text>
              </g>
            );
          })}

          <line
            x1={PAD.left}
            y1={baseY}
            x2={W - PAD.right}
            y2={baseY}
            stroke="var(--fg-4)"
            strokeWidth="1"
            strokeDasharray="2 4"
          />

          {realized.length >= 2 && (
            <path
              d={linePath(realized, s)}
              fill="none"
              stroke="var(--info)"
              strokeWidth="1.25"
              strokeDasharray="4 3"
              strokeLinejoin="round"
              vectorEffect="non-scaling-stroke"
            />
          )}

          <path
            d={linePath(points, s)}
            fill="none"
            stroke={up ? "var(--pos)" : "var(--neg)"}
            strokeWidth="1.75"
            strokeLinejoin="round"
            strokeLinecap="round"
            vectorEffect="non-scaling-stroke"
          />

          <circle
            cx={s.x(last[0])}
            cy={lastY}
            r="2.5"
            fill={up ? "var(--pos)" : "var(--neg)"}
          />
          <text
            x={W - PAD.right + 8}
            y={Math.min(H - 6, Math.max(11, lastY + 3.5))}
            fill={up ? "var(--pos)" : "var(--neg)"}
            fontSize="11"
            fontWeight="600"
            fontFamily="var(--font-mono)"
          >
            {formatMoney(last[1])}
          </text>
          <text
            x={W - PAD.right + 8}
            y={baseY + 3.5}
            fill="var(--fg-4)"
            fontSize="9.5"
            fontFamily="var(--font-mono)"
          >
            {formatMoney(base, 0)}
          </text>

          <text
            x={PAD.left}
            y={H - 6}
            fill="var(--fg-4)"
            fontSize="9.5"
            fontFamily="var(--font-mono)"
          >
            {formatClockMs(minX)}
          </text>
          <text
            x={W - PAD.right}
            y={H - 6}
            textAnchor="end"
            fill="var(--fg-4)"
            fontSize="9.5"
            fontFamily="var(--font-mono)"
          >
            {formatClockMs(maxX)}
          </text>
        </svg>

        <p className="footnote" style={{ marginTop: 6 }}>
          Başlangıçtan bu yana{" "}
          <span className={up ? "pos" : "neg"}>{formatSignedMicroMoney(delta)}</span> ·{" "}
          {points.length} gözlem
          {boundaries.length > 0 &&
            ` · ${boundaries.length} simülatör sürüm sınırı (sınırın iki yanı aynı deney değildir)`}
        </p>
      </>
    );
  }

  return (
    <Panel
      idx="07"
      title="Sermaye eğrisi"
      note={`${formatMoney(base, 0)} başlangıçtan itibaren`}
    >
      {body}
    </Panel>
  );
}
