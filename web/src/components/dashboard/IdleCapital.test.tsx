/**
 * §02 renders every reason code the worker can publish — including the ones it
 * cannot classify.
 *
 * Phase 6 grew `IdleReason` from six codes to ten and introduced a group that
 * is deliberately absent: `OTHER` is the residue bucket and `idleGroup()`
 * returns `null` for it on purpose. That makes every `IDLE_GROUP_*[group]`
 * lookup in this panel a place where an unguarded index would read `undefined`
 * and, one `.toFixed()` or `.toLocaleLowerCase()` later, throw during render —
 * which unmounts not just the panel but every panel below it, because the whole
 * dashboard is one client component with no boundary between the sections.
 *
 * So the contract this file pins down is narrow and blunt: an unclassified or
 * unknown reason must produce an explicit row, never an exception. It is
 * asserted by rendering the real component against real payload shapes rather
 * than by testing the helpers, because the helpers were never the fragile part
 * — the lookups that consume them are.
 */

import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";

import { IdleCapital } from "./IdleCapital.tsx";
import {
  IDLE_GROUP_LABELS,
  IDLE_GROUP_ORDER,
  IDLE_GROUP_NOTES,
  IDLE_GROUP_VERDICTS,
  IDLE_REASONS_ALL,
  idleGroup,
} from "@/lib/format.ts";
import type { CapitalState, IdleByReason } from "@/lib/types.ts";

/** All ten Phase 6 codes, every one of them carrying real dollars. */
const ALL_TEN: IdleByReason = {
  NO_QUALIFIED_SIGNAL: 1200,
  CORRELATION_LIMIT: 300,
  RISK_LIMIT: 250,
  LIQUIDITY_LIMIT: 175,
  EXECUTION_COST: 425,
  CASH_RESERVE: 900,
  COOLDOWN: 500,
  SCANNER_DELAY: 600,
  CAPITAL_STATE_BUG: 350,
  OTHER: 300,
};

function capital(byReason: IdleByReason | null, idleUsd: number | null = 5000): CapitalState {
  return {
    equity: 5000,
    deployed_usd: 0,
    deployed_pct: 0,
    idle_usd: idleUsd,
    idle_pct: 100,
    target_pct: 80,
    open_positions: 0,
    idle_by_reason: byReason,
  } as unknown as CapitalState;
}

const render = (byReason: IdleByReason | null, idleUsd: number | null = 5000) =>
  renderToStaticMarkup(<IdleCapital capital={capital(byReason, idleUsd)} loading={false} />);

test("every reason the worker can publish is rendered, OTHER included", () => {
  const html = render(ALL_TEN);
  // The ungrouped residue is shown as its own row rather than silently folded
  // into a group that would claim a diagnosis nobody made.
  assert.match(html, /Other/);
  assert.match(html, /UNCLASSIFIED/);
  // And every classified group still prints its own label.
  for (const group of IDLE_GROUP_ORDER) {
    assert.ok(
      html.includes(IDLE_GROUP_LABELS[group]),
      `group ${group} is missing from the attribution`,
    );
  }
});

test("an unknown reason code renders as itself instead of crashing the screen", () => {
  const html = render({ NO_QUALIFIED_SIGNAL: 2500, TIME_OF_DAY_VETO: 2500 });
  assert.match(html, /TIME_OF_DAY_VETO/);
  assert.match(html, /UNCLASSIFIED/);
});

test("a dominant reason with no group prints no group verdict and no undefined", () => {
  // OTHER alone is the worst case for the lookups: the dominant row's group is
  // null, so every `IDLE_GROUP_*[group]` read on that path must be skipped.
  const html = render({ OTHER: 5000 });
  assert.doesNotMatch(html, /undefined/);
  assert.match(html, /Largest reason capital is idle/);
});

test("degenerate attributions are reported, not thrown", () => {
  for (const [name, byReason, idleUsd] of [
    ["empty record", {}, 5000],
    ["all zero", Object.fromEntries(Object.keys(ALL_TEN).map((k) => [k, 0])), 5000],
    ["all null", Object.fromEntries(Object.keys(ALL_TEN).map((k) => [k, null])), 5000],
    ["no reported balance", ALL_TEN, null],
    ["balance the reasons do not add up to", ALL_TEN, 9000],
    ["a negative residue", { ...ALL_TEN, OTHER: -900 }, 5000],
  ] as [string, IdleByReason, number | null][]) {
    assert.doesNotThrow(() => render(byReason, idleUsd), name);
  }
  assert.doesNotThrow(() => render(null), "attribution absent entirely");
});

test("no group can exist without a label, a note and a verdict", () => {
  // The lookups above are only safe while this holds. A new Phase-7 group added
  // to `IDLE_REASON_GROUP` without its three strings would read `undefined` at
  // render time; it fails here instead.
  for (const code of IDLE_REASONS_ALL) {
    const group = idleGroup(code);
    if (group === null) continue;
    assert.ok(IDLE_GROUP_ORDER.includes(group), `${code} maps to ungrouped order: ${group}`);
    assert.equal(typeof IDLE_GROUP_LABELS[group], "string", `${group} has no label`);
    assert.equal(typeof IDLE_GROUP_NOTES[group], "string", `${group} has no note`);
    assert.equal(typeof IDLE_GROUP_VERDICTS[group], "string", `${group} has no verdict`);
  }
  assert.equal(idleGroup("OTHER"), null, "OTHER must stay the ungrouped residue");
});
