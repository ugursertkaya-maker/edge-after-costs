"use client";

import { useEffect, useState } from "react";
import { Rail } from "@/components/dashboard/Rail";
import { MetricStrip } from "@/components/dashboard/MetricStrip";
import { Scanner } from "@/components/dashboard/Scanner";
import { CostSplit } from "@/components/dashboard/CostSplit";
import { Capital } from "@/components/dashboard/Capital";
import { IdleCapital } from "@/components/dashboard/IdleCapital";
import { Portfolio } from "@/components/dashboard/Portfolio";
import { Positions } from "@/components/dashboard/Positions";
import { Trades } from "@/components/dashboard/Trades";
import { Strategies } from "@/components/dashboard/Strategies";
import { EquityCurve } from "@/components/dashboard/EquityCurve";
import { Health } from "@/components/dashboard/Health";
import { Tournament } from "@/components/dashboard/Tournament";
import { Finalists } from "@/components/dashboard/Finalists";
import { Champion } from "@/components/dashboard/Champion";
import { PredictiveEngine } from "@/components/dashboard/PredictiveEngine";
import { ModelQuality } from "@/components/dashboard/ModelQuality";
import { Explanations } from "@/components/dashboard/Explanations";
import { MissedOpportunities } from "@/components/dashboard/MissedOpportunities";
import { Banner } from "@/components/ui/DataState";
import { useApiHealth, useDashboard } from "@/lib/useDashboard";

/**
 * The whole terminal is one screen fed by one read. Splitting it into routes
 * would mean several polls of the same worker on a 2-vCPU host, and would hide
 * the thing that matters most: the scanner's refusals and the P&L they
 * produced belong in the same field of view.
 */
export default function DashboardPage() {
  const [ledger, setLedger] = useState("base");
  const { data, loading, error, failures, lastOkMs } = useDashboard(ledger);
  const { ok: apiOk } = useApiHealth();

  // One clock for every relative timestamp on the screen, so ages tick in step
  // and each component does not read Date.now() at its own cadence.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  // One failed poll is a blip on a shared host; three is an outage worth
  // saying out loud, because everything below is then stale by definition.
  const stale = failures >= 2;
  const startingEquity = data?.header.starting_equity ?? null;

  return (
    <div className="shell">
      <Rail
        data={data}
        ledger={ledger}
        onLedger={setLedger}
        lastOkMs={lastOkMs}
        stale={stale}
        apiOk={apiOk}
        now={now}
      />

      <main className="content">
        {error && stale && (
          <Banner
            tone="err"
            tag="Bağlantı"
            detail={`${failures} ardışık başarısız okuma`}
          >
            {error} Aşağıdaki değerler son başarılı okumaya aittir, güncel
            değildir.{" "}
            {apiOk === false
              ? "API sürecine hiç ulaşılamıyor."
              : apiOk
                ? "API ayakta — sorun panel verisini üreten worker tarafında."
                : ""}
          </Banner>
        )}

        {data && data.live_orders_enabled && (
          <Banner tone="err" tag="Kritik">
            Gerçek emir bayrağı AÇIK görünüyor. V1 kâğıt üzerinde çalışmak
            zorundadır — bu durum bir kusurdur, işlem yapmadan önce worker
            durdurulmalıdır.
          </Banner>
        )}

        {data && !data.header.data_connected && (
          <Banner tone="warn" tag="Besleme">
            Exchange verisi kopuk. Bayat defterle karar üretilmediği için
            tarayıcı yeni sinyal açmayacak; açık pozisyonlar son bilinen fiyatla
            değerlenmektedir.
          </Banner>
        )}

        <MetricStrip metrics={data?.metrics ?? null} startingEquity={startingEquity} />

        <CostSplit metrics={data?.metrics ?? null} />

        {/* Phase 5 — capital utilisation, at the top of the screen because it
            is the reading the whole phase turns on. §01 says how much of the
            account is at work against the target band; §02 says, in dollars,
            why the rest is not. They are deliberately the first two panels:
            Phase 3 measured 99.5% idle cash from a portfolio table nobody
            scrolled to. */}
        <Capital capital={data?.capital ?? null} loading={loading} />

        <IdleCapital capital={data?.capital ?? null} loading={loading} />

        <Scanner
          rows={data?.scanner ?? null}
          loading={loading}
          connected={data?.header.data_connected ?? false}
        />

        <Portfolio portfolio={data?.portfolio ?? null} loading={loading} />

        <Positions
          rows={data?.positions ?? null}
          loading={loading}
          now={now}
          generatedMs={data?.generated_ms ?? null}
        />

        <div className="col-2">
          <EquityCurve
            curve={data?.equity_curve ?? null}
            startingEquity={startingEquity}
            loading={loading}
          />
          <Health
            health={data?.health ?? null}
            mode={data?.mode ?? null}
            now={now}
            loading={loading}
          />
        </div>

        <Strategies rows={data?.strategies ?? null} loading={loading} />

        <Trades rows={data?.trades ?? null} loading={loading} />

        {/* Phase 3 — the profitability search. These three read as one
            argument and are kept in this order on purpose: every arm that was
            tried, then the handful that survived the gates, then whatever of
            it is actually running in paper. */}
        <Tournament tournament={data?.tournament ?? null} loading={loading} now={now} />

        <Finalists finalists={data?.finalists ?? null} loading={loading} />

        <Champion champion={data?.champion ?? null} loading={loading} now={now} />

        {/* Phase 4 — the predictive entry engine. Same order as the Phase 3
            block above and for the same reason: what the model says about
            each candidate, then what the model itself is worth, then the
            stored values behind every decision it actually took. The panels
            share one `predictive` object and each one renders its own absent
            state, because that object is expected to be missing. */}
        <PredictiveEngine predictive={data?.predictive ?? null} loading={loading} />

        <ModelQuality predictive={data?.predictive ?? null} loading={loading} />

        <Explanations predictive={data?.predictive ?? null} loading={loading} now={now} />

        {/* Phase 5 — the post-hoc rejection audit. It sits with the research
            panels rather than next to §02 on purpose: it is computed with
            future data and must never be read as a live signal. */}
        <MissedOpportunities missed={data?.missed ?? null} loading={loading} />

        <p className="footnote">
          Tüm işlemler simülasyondur. Binance.US herkese açık piyasa verisi
          kullanılır; imzalı istek, API anahtarı veya emir gönderim yolu yoktur.
          Tüm zaman damgaları epoch milisaniyedir, tutarlar USD'dir.
        </p>
      </main>
    </div>
  );
}
