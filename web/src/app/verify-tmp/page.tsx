/* TEMPORARY verification harness — deleted before this work is handed over. */
import { Tournament } from "@/components/dashboard/Tournament";
import { Finalists } from "@/components/dashboard/Finalists";
import { Champion } from "@/components/dashboard/Champion";
import { Health } from "@/components/dashboard/Health";
import type {
  ChampionState,
  FinalistRow,
  HealthState,
  TournamentArm,
  TournamentSummary,
} from "@/lib/types";

const NOW = 1789000000000;

const arm = (over: Partial<TournamentArm>): TournamentArm => ({
  key: "k",
  family: "momentum_burst",
  symbol: "BTCUSD",
  regime: "TRENDING_UP",
  horizon_seconds: 1800,
  exit_profile: "net_tp_050",
  stop_profile: "atr_2",
  trades: 84,
  win_rate: 0.41,
  avg_win: 0.42,
  avg_loss: 0.31,
  profit_factor: 0.94,
  expectancy: -0.07,
  fees: 0.9,
  slippage: 4.2,
  net_pnl: -5.88,
  max_drawdown: 12.4,
  in_sample: { trades: 54, profit_factor: 1.44, expectancy: 0.09, net_pnl: 4.86 },
  out_of_sample: { trades: 30, profit_factor: 0.71, expectancy: -0.36, net_pnl: -10.74 },
  oos_status: "FAILED",
  ...over,
});

const fullTournament: TournamentSummary = {
  generated_ms: NOW - 3600_000,
  candidates_evaluated: 4820,
  wall_clock_seconds: 5123,
  period: "2025-09-01 → 2026-09-01",
  fee_bps: 1,
  arms: [
    arm({ key: "a1" }),
    arm({
      key: "a2",
      family: "maker_reversion",
      symbol: "ETHUSD",
      regime: "RANGING",
      horizon_seconds: 5400,
      oos_status: "FINALIST",
      profit_factor: 1.31,
      expectancy: 0.08,
      net_pnl: 9.2,
      out_of_sample: { trades: 41, profit_factor: 1.28, expectancy: 0.11, net_pnl: 4.5 },
    }),
    arm({
      key: "a3",
      family: "breakout_continuation",
      symbol: "LTCUSD",
      regime: "BREAKOUT",
      oos_status: "MARGINAL",
      out_of_sample: { trades: 33, profit_factor: 1.07, expectancy: 0.01, net_pnl: 0.4 },
    }),
    arm({
      key: "a4",
      family: "panic_fade",
      symbol: "BTCUSD",
      regime: "PANIC",
      oos_status: "INSUFFICIENT_SAMPLE",
      out_of_sample: { trades: 7, profit_factor: 2.9, expectancy: 0.55, net_pnl: 3.9 },
    }),
    // The all-null arm: every optional figure absent.
    {
      key: "a5",
      family: null,
      symbol: null,
      regime: null,
      horizon_seconds: null,
      trades: null,
      win_rate: null,
      avg_win: null,
      avg_loss: null,
      profit_factor: null,
      expectancy: null,
      fees: null,
      slippage: null,
      net_pnl: null,
      max_drawdown: null,
      in_sample: null,
      out_of_sample: null,
      oos_status: null,
    },
    // The empty-object arm: the worker serving a bare row.
    {},
  ],
};

const finalistsFull: FinalistRow[] = [
  {
    key: "a2",
    family: "maker_reversion",
    symbol: "ETHUSD",
    regime: "RANGING",
    horizon_seconds: 5400,
    passed: true,
    reasons: [
      "PASS · net expectancy out-of-sample: +$0.11 per trade",
      "PASS · profit factor 1.28 ≥ 1.20",
      "FAIL · max drawdown $61.20 is 12.2% of a $500 account",
      "best-trade removal leaves expectancy at +$0.04",
    ],
    stress: [
      { label: "fees +25%", survived: true, net_pnl: 3.8 },
      { label: "slippage ×2", survived: false, net_pnl: -1.2 },
      { label: "latency ×2", survived: null, net_pnl: null },
    ],
    monte_carlo: {
      median_drawdown: 22.5,
      p95_drawdown: 61.2,
      worst_loss_streak: 9,
      prob_below_start: 0.34,
      samples: 2000,
    },
  },
  // A gate report with nothing in it but a key.
  { key: "bare" },
];

const championFull: ChampionState = {
  strategy: "maker_reversion",
  version: "v3.1.0",
  started_ms: NOW - 86400_000 * 3,
  paper_equity: 503.11,
  oos_profit_factor: 1.28,
  live_profit_factor: 0.83,
  expectancy: -0.04,
  max_drawdown: 18.9,
  trades: 62,
};

const baseHealth: HealthState = {
  ws_connected: true,
  rest_ok: true,
  book_age_ms: 220,
  updates_per_sec: 3.2,
  reconnects: 1,
  dropped: 0,
  worker_cpu_pct: 21,
  worker_mem_mb: 402,
  db_ok: true,
  last_evaluation_ms: NOW - 2000,
  mode: "PAPER",
};

function Case({ t, children }: { t: string; children: React.ReactNode }) {
  return (
    <section style={{ marginBottom: 24 }}>
      <p className="footnote">### CASE {t}</p>
      {children}
    </section>
  );
}

export default function Verify() {
  return (
    <div className="shell">
      <main className="content">
        <Case t="1-full-tournament">
          <Tournament tournament={fullTournament} loading={false} now={NOW} />
        </Case>
        <Case t="2-no-tournament">
          <Tournament tournament={null} loading={false} now={NOW} />
        </Case>
        <Case t="3-tournament-loading">
          <Tournament tournament={null} loading now={NOW} />
        </Case>
        <Case t="4-tournament-empty-arms">
          <Tournament
            tournament={{ candidates_evaluated: 12, arms: [] }}
            loading={false}
            now={NOW}
          />
        </Case>
        <Case t="5-tournament-all-null">
          <Tournament
            tournament={{
              generated_ms: null,
              candidates_evaluated: null,
              wall_clock_seconds: null,
              period: null,
              fee_bps: null,
              arms: [{}],
            }}
            loading={false}
            now={NOW}
          />
        </Case>
        <Case t="6-finalists-full">
          <Finalists finalists={finalistsFull} loading={false} />
        </Case>
        <Case t="7-finalists-empty">
          <Finalists finalists={[]} loading={false} />
        </Case>
        <Case t="8-finalists-absent">
          <Finalists finalists={null} loading={false} />
        </Case>
        <Case t="9-finalists-failed-entry">
          <Finalists
            finalists={[{ key: "x", passed: false, reasons: ["FAIL · only 11 OOS trades"] }]}
            loading={false}
          />
        </Case>
        <Case t="10-champion-full">
          <Champion champion={championFull} loading={false} now={NOW} />
        </Case>
        <Case t="11-champion-null">
          <Champion champion={null} loading={false} now={NOW} />
        </Case>
        <Case t="12-champion-all-null">
          <Champion
            champion={{
              strategy: null,
              version: null,
              started_ms: null,
              paper_equity: null,
              oos_profit_factor: null,
              live_profit_factor: null,
              expectancy: null,
              max_drawdown: null,
              trades: null,
            }}
            loading={false}
            now={NOW}
          />
        </Case>
        <Case t="13-champion-one-pf-only">
          <Champion
            champion={{ strategy: "x", oos_profit_factor: 1.4 }}
            loading={false}
            now={NOW}
          />
        </Case>
        <Case t="14-health-with-tape">
          <Health
            health={{
              ...baseHealth,
              recorder: { enabled: true, messages: 4120334, mb_written: 812.4, disabled_reason: null },
              tape: {
                files: 190,
                mb: 806.1,
                hours: 190,
                oldest: NOW - 190 * 3600_000,
                newest: NOW - 3600_000,
              },
            }}
            mode="PAPER"
            now={NOW}
            loading={false}
          />
        </Case>
        <Case t="15-health-recorder-off">
          <Health
            health={{
              ...baseHealth,
              recorder: { enabled: false, messages: 0, mb_written: 0, disabled_reason: "disk alanı %92" },
              tape: { files: null, mb: null, hours: null, oldest: null, newest: null },
            }}
            mode="PAPER"
            now={NOW}
            loading={false}
          />
        </Case>
        <Case t="16-health-no-tape-keys">
          <Health health={baseHealth} mode="PAPER" now={NOW} loading={false} />
        </Case>
      </main>
    </div>
  );
}
