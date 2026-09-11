"use client";

import { useEffect, useState } from "react";
import { Rail } from "@/components/dashboard/Rail";
import { MetricStrip } from "@/components/dashboard/MetricStrip";
import { Scanner } from "@/components/dashboard/Scanner";
import { Positions } from "@/components/dashboard/Positions";
import { Trades } from "@/components/dashboard/Trades";
import { Strategies } from "@/components/dashboard/Strategies";
import { EquityCurve } from "@/components/dashboard/EquityCurve";
import { Health } from "@/components/dashboard/Health";
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

        <Scanner
          rows={data?.scanner ?? null}
          loading={loading}
          connected={data?.header.data_connected ?? false}
        />

        <Positions rows={data?.positions ?? null} loading={loading} />

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

        <p className="footnote">
          Tüm işlemler simülasyondur. Binance.US herkese açık piyasa verisi
          kullanılır; imzalı istek, API anahtarı veya emir gönderim yolu yoktur.
          Tüm zaman damgaları epoch milisaniyedir, tutarlar USD'dir.
        </p>
      </main>
    </div>
  );
}
