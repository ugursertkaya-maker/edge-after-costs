/* TEMPORARY verification harness — deleted before this work is handed over. */
import { PredictiveEngine } from "@/components/dashboard/PredictiveEngine";
import { ModelQuality } from "@/components/dashboard/ModelQuality";
import { Explanations } from "@/components/dashboard/Explanations";
import type {
  PredictiveCandidate,
  PredictiveExplanation,
  PredictiveState,
} from "@/lib/types";

const NOW = 1789000000000;
const DAY = 86_400_000;

const cand = (over: Partial<PredictiveCandidate>): PredictiveCandidate => ({
  symbol: "BTCUSD",
  regime: "TRENDING_UP",
  model: "PredictiveEngine-v1",
  p_tp: 0.61,
  p_sl: 0.3,
  p_timeout: 0.09,
  expected_net_win: 0.62,
  expected_net_loss: 0.31,
  expected_value: 0.12,
  execution_cost: 0.04,
  confidence: 0.68,
  confidence_known: true,
  decision: "BUY",
  reject_reason: null,
  ...over,
});

const candidates: PredictiveCandidate[] = [
  cand({}),
  cand({
    symbol: "SOLUSD",
    regime: "VOLATILITY_EXPANSION",
    decision: "WATCH",
    expected_value: 0.002,
    confidence: 0.52,
  }),
  cand({
    symbol: "ETHUSD",
    regime: "PANIC",
    decision: "NO_TRADE",
    expected_value: -0.18,
    reject_reason: "NEGATIVE_EV",
    confidence: 0.44,
  }),
  cand({
    symbol: "ADAUSD",
    decision: "NO_TRADE",
    reject_reason: "MODEL_UNCERTAIN",
    confidence_known: false,
    confidence: null,
  }),
  // Every optional figure absent.
  {},
];

const calibrated = {
  brier: 0.2141,
  log_loss: 0.612,
  auc: 0.583,
  pr_auc: 0.401,
  max_abs_error: 0.061,
  calibrated: true,
  method: "isotonic",
  status: "CALIBRATED",
};

const full: PredictiveState = {
  model_version: "PredictiveEngine-v1.2.0",
  model_name: "hist_gbm",
  feature_version: "feat-2026.09",
  spec_key: "net_fixed:75|vol:3|h=3600",
  trained_to_ms: NOW - 30 * DAY,
  train_period: [NOW - 180 * DAY, NOW - 60 * DAY],
  validate_period: [NOW - 60 * DAY, NOW - 30 * DAY],
  test_period: [NOW - 30 * DAY, NOW - 1 * DAY],
  probability_threshold: 0.58,
  ev_threshold_usd: 0.05,
  calibration: calibrated,
  oos: {
    trades: 84,
    win_rate: 0.42,
    profit_factor: 1.24,
    expectancy: 0.071,
    net_pnl: 5.96,
    max_drawdown: 4.29,
  },
  candidates,
  rejections: {
    LOW_PROBABILITY: 1204,
    NEGATIVE_EV: 812,
    COST_TOO_HIGH: 341,
    MODEL_UNCERTAIN: 96,
    SPREAD_TOO_WIDE: 55,
    STALE_DATA: 12,
    CORRELATED_RISK: 7,
  },
  explanations: [
    {
      trade_id: "t-9001",
      symbol: "SOL/USD",
      side: "LONG",
      decision: "BUY",
      p_win: 0.68,
      expected_net_win: 0.62,
      expected_net_loss: 0.31,
      expected_value: 0.32,
      regime: "TRENDING_UP",
      spread_ok: true,
      liquidity: "STRONG",
      execution_style: "MAKER",
      model_version: "PredictiveEngine-v1",
      ts_ms: NOW - 420_000,
    },
    {
      trade_id: "t-9002",
      symbol: "BTC/USD",
      side: "LONG",
      decision: "NO_TRADE",
      p_win: 0.51,
      expected_net_win: 0.4,
      expected_net_loss: 0.44,
      expected_value: -0.06,
      regime: "RANGING",
      spread_ok: false,
      liquidity: 18400,
      execution_style: "TAKER",
      model_version: "PredictiveEngine-v1",
      ts_ms: NOW - 3_600_000,
    },
    // Every optional field absent.
    {},
  ],
};

const uncalibrated: PredictiveState = {
  ...full,
  calibration: {
    brier: 0.3312,
    log_loss: 0.911,
    auc: 0.504,
    pr_auc: 0.22,
    max_abs_error: 0.27,
    calibrated: false,
    method: "none",
    status: "UNCALIBRATED",
  },
};

const unmeasured: PredictiveState = {
  ...full,
  calibration: { status: "UNMEASURED" },
};

const allNull: PredictiveState = {
  model_version: null,
  model_name: null,
  feature_version: null,
  spec_key: null,
  trained_to_ms: null,
  train_period: null,
  validate_period: null,
  test_period: null,
  probability_threshold: null,
  ev_threshold_usd: null,
  calibration: null,
  oos: null,
  candidates: null,
  rejections: null,
  explanations: null,
};

const rejectionsOnly: PredictiveState = {
  model_version: "PredictiveEngine-v1.2.0",
  calibration: calibrated,
  candidates: [],
  explanations: [],
  rejections: {
    LOW_PROBABILITY: 8321,
    NEGATIVE_EV: 44,
    NO_LIQUIDITY: 3,
    REGIME_NOT_SUPPORTED: 91,
  },
};

const leaky: PredictiveState = {
  ...full,
  // Test window starts before validation ends — the leak the timeline exists
  // to catch.
  validate_period: [NOW - 60 * DAY, NOW - 20 * DAY],
  test_period: [NOW - 30 * DAY, NOW - 1 * DAY],
};

const bareExplanations: PredictiveExplanation[] = [{}, { symbol: "ETH/USD" }];

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
        <Case t="1-engine-full-calibrated">
          <PredictiveEngine predictive={full} loading={false} />
        </Case>
        <Case t="2-engine-absent">
          <PredictiveEngine predictive={null} loading={false} />
        </Case>
        <Case t="3-engine-loading">
          <PredictiveEngine predictive={null} loading />
        </Case>
        <Case t="4-engine-uncalibrated">
          <PredictiveEngine predictive={uncalibrated} loading={false} />
        </Case>
        <Case t="5-engine-calibration-unmeasured">
          <PredictiveEngine predictive={unmeasured} loading={false} />
        </Case>
        <Case t="6-engine-zero-candidates">
          <PredictiveEngine predictive={rejectionsOnly} loading={false} />
        </Case>
        <Case t="7-engine-all-null">
          <PredictiveEngine predictive={allNull} loading={false} />
        </Case>

        <Case t="8-quality-full">
          <ModelQuality predictive={full} loading={false} />
        </Case>
        <Case t="9-quality-absent">
          <ModelQuality predictive={null} loading={false} />
        </Case>
        <Case t="10-quality-all-null">
          <ModelQuality predictive={allNull} loading={false} />
        </Case>
        <Case t="11-quality-uncalibrated">
          <ModelQuality predictive={uncalibrated} loading={false} />
        </Case>
        <Case t="12-quality-non-chronological">
          <ModelQuality predictive={leaky} loading={false} />
        </Case>
        <Case t="13-quality-no-oos">
          <ModelQuality predictive={{ ...full, oos: null }} loading={false} />
        </Case>

        <Case t="14-explanations-full">
          <Explanations predictive={full} loading={false} now={NOW} />
        </Case>
        <Case t="15-explanations-absent">
          <Explanations predictive={null} loading={false} now={NOW} />
        </Case>
        <Case t="16-explanations-rejections-only-no-trades">
          <Explanations predictive={rejectionsOnly} loading={false} now={NOW} />
        </Case>
        <Case t="17-explanations-all-null">
          <Explanations predictive={allNull} loading={false} now={NOW} />
        </Case>
        <Case t="18-explanations-uncalibrated-probabilities">
          <Explanations predictive={uncalibrated} loading={false} now={NOW} />
        </Case>
        <Case t="19-explanations-bare-rows">
          <Explanations
            predictive={{ ...full, explanations: bareExplanations, rejections: {} }}
            loading={false}
            now={NOW}
          />
        </Case>
        <Case t="20-explanations-no-tally">
          <Explanations
            predictive={{ ...full, rejections: null }}
            loading={false}
            now={NOW}
          />
        </Case>
      </main>
    </div>
  );
}
