import { Panel } from "@/components/ui/Panel";
import { DataState, Skeleton } from "@/components/ui/DataState";
import {
  DASH,
  baseAsset,
  formatBps,
  formatCountdownMs,
  formatInt,
  formatMicroMoney,
  formatMoney,
  opportunityStatusLabel,
  opportunityStatusTone,
  rejectReasonLabel,
  strategyLabel,
  toNum,
} from "@/lib/format";
import { opportunityRows } from "@/lib/phase6";
import type { OpportunityBoard as BoardState, ScannerRow } from "@/lib/types";

/**
 * CANLI FIRSAT PANOSU — what the allocator is choosing between, right now.
 *
 * This is the trader's primary screen and it is deliberately the first table
 * on the page. Every other panel here explains something that already
 * happened; this one is the only place where the *decision in front of the
 * engine* is visible, ranked, with the refusals kept in the same list rather
 * than swept into a log.
 *
 * Three rules the table encodes:
 *
 *   * A rejected candidate carries its reason on the same row. A board that
 *     shows only what survived cannot tell you why the book is empty.
 *
 *   * A rejected candidate has NO RANK. Rank is a position in the queue the
 *     allocator was choosing from, and printing "7th" beside REDDEDİLDİ would
 *     claim it was in a queue it never entered.
 *
 *   * The EV column is labelled in bps and the notional it was sized against
 *     sits beside it. "+12 bps" on an unknown position size is not an
 *     expectation anybody can compare.
 */
export function OpportunityBoard({
  board,
  scanner,
  loading,
  connected,
  now,
}: {
  board: BoardState | null | undefined;
  scanner: ScannerRow[] | null;
  loading: boolean;
  connected: boolean;
  now: number;
}) {
  const { rows, derived } = opportunityRows(board?.candidates ?? board?.entries, scanner);

  const queued = rows.filter((r) => String(r.status ?? "").toUpperCase() === "QUEUED").length;
  const selected = rows.filter((r) => String(r.status ?? "").toUpperCase() === "SELECTED").length;
  const rejected = rows.filter((r) => String(r.status ?? "").toUpperCase() === "REJECT").length;
  const evaluated = toNum(board?.evaluated) ?? toNum(board?.size) ?? rows.length;

  // Why entries left the board. An open record, like every other tally on this
  // screen: a reason the worker adds later still reaches the operator.
  const dropped = Object.entries(board?.dropped ?? {})
    .map(([k, v]) => [k, toNum(v)] as const)
    .filter((e): e is readonly [string, number] => e[1] !== null && e[1] > 0)
    .sort((a, b) => b[1] - a[1]);

  const meta = (
    <>
      <span className="pill is-off" title="Bu turda değerlendirilen sembol sayısı">
        {formatInt(evaluated)} aday
      </span>
      <span className="pill is-pos" title="Her satır simülasyondur; gerçek emir yoktur.">
        PAPER
      </span>
    </>
  );

  return (
    <Panel
      idx="17"
      title="Canlı fırsat panosu"
      note="tahsisçinin seçim yaptığı sıra — beklenen NET EV'ye göre"
      meta={meta}
      flush
      footer={
        derived
          ? "KAYNAK: tarayıcı satırları. Worker henüz kendi kuyruğunu (`opportunities.entries`) yayınlamıyor, bu yüzden sıra tarayıcının net edge okumasından TÜRETİLMİŞTİR — tahsisçinin gerçek kuyruğu değildir ve ikisi ayrıldığında fark tam olarak bir seçim kusurunun yaşayacağı yerdir. EV sütunu BASIS POINT'tir (1 bps = %0,01), dolar değildir; pozisyon büyüklüğü gelmeden dolar karşılığı hesaplanamaz. Reddedilen adayın sırası yoktur: hiç girmediği bir kuyrukta yer göstermek olurdu. Tüm satırlar kâğıt üzerindedir, gerçek emir sayısı sıfırdır."
          : "EV sütunu BASIS POINT'tir (1 bps = %0,01). Dolar karşılığı, yalnızca adayın nominal büyüklüğü de bildirildiğinde gösterilir — denominatörü olmayan bir beklenti adaylar arasında karşılaştırılamaz. Reddedilen adayın sırası yoktur. Tüm satırlar kâğıt üzerindedir, gerçek emir sayısı sıfırdır."
      }
    >
      {loading && rows.length === 0 ? (
        <Skeleton rows={6} />
      ) : rows.length === 0 ? (
        <DataState
          scanning={connected}
          title="Panoda aday yok"
          text={
            connected
              ? "Tarayıcı çalışıyor ama hiçbir sembol henüz aday üretmedi. Bu boş bir tablo değil, bir okumadır: şu anda tahsisçinin önünde seçilecek bir şey yok."
              : "Exchange bağlantısı kopuk. Bayat defterle aday sıralanmaz, bu yüzden pano kasıtlı olarak boş."
          }
        />
      ) : (
        <>
          <div className="obar">
            <span className="obar-k">Bu turda</span>
            <span className="obar-i is-selected">
              <b>{formatInt(selected)}</b> seçildi
            </span>
            <span className="obar-i is-queued">
              <b>{formatInt(queued)}</b> sırada
            </span>
            <span className="obar-i is-reject">
              <b>{formatInt(rejected)}</b> reddedildi
            </span>
            <span className="obar-n">
              {formatInt(evaluated)} aday değerlendirildi
              {derived ? " · tarayıcıdan türetildi" : ""}
              {toNum(board?.max_size) === null
                ? ""
                : ` · pano kapasitesi ${formatInt(board?.max_size ?? null)}`}
            </span>
          </div>

          {dropped.length > 0 && (
            <p className="obar-drop">
              Panodan düşen adaylar:{" "}
              {dropped.map(([k, v]) => `${k} ${formatInt(v)}`).join(" · ")}. Düşmek
              reddedilmek değildir — bir aday süresi dolduğu, daha iyisine yenildiği veya
              artık geçerli olmadığı için de panodan çıkar.
            </p>
          )}

          <div className="tbl-wrap">
            <table className="tbl">
              <thead>
                <tr>
                  <th className="c" title="Tahsisçinin sırası. Reddedilen adayda yoktur.">
                    Sıra
                  </th>
                  <th className="l">Coin</th>
                  <th className="l">Strateji</th>
                  <th title="Beklenen NET beklenen değer, basis point">Beklenen net EV</th>
                  <th title="Adayın nominal büyüklüğü — EV'nin denominatörü">Nominal</th>
                  <th title="EV'nin dolar karşılığı, yalnızca worker büyüklükle birlikte bildirdiğinde">
                    Net EV ($)
                  </th>
                  <th className="l" title="Episode kimliği — aynı impuls yeni bir giriş üretemez">
                    Episode
                  </th>
                  <th title="Sıradaki adayın geçerlilik süresi">Geçerlilik</th>
                  <th className="c">Durum</th>
                  <th className="l">Ret gerekçesi</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => {
                  const status = String(r.status ?? "").toUpperCase();
                  const isReject = status === "REJECT";
                  const ev = toNum(r.net_ev_bps);
                  const evCls = ev === null ? "muted" : ev > 0 ? "pos" : ev < 0 ? "neg" : "dim";
                  const rank = toNum(r.rank);
                  return (
                    <tr key={`${r.symbol ?? "?"}-${r.episode_id ?? i}`}>
                      <td className="c rank">
                        {rank === null ? <span className="muted">{DASH}</span> : formatInt(rank)}
                      </td>
                      <td className="l sym" title={r.symbol ?? undefined}>
                        {r.symbol ? baseAsset(r.symbol) : DASH}
                      </td>
                      <td className="l dim" title={r.strategy ?? undefined}>
                        {r.strategy ? strategyLabel(r.strategy) : DASH}
                      </td>
                      <td className={evCls} title="bps, net">
                        {formatBps(ev)}
                      </td>
                      <td className={r.notional_usd ? "dim" : "muted"}>
                        {formatMoney(r.notional_usd ?? null)}
                      </td>
                      <td className={toNum(r.net_ev_usd) === null ? "muted" : evCls}>
                        {toNum(r.net_ev_usd) === null
                          ? DASH
                          : formatMicroMoney(r.net_ev_usd ?? null)}
                      </td>
                      <td className="l dim ep" title={r.episode_id ?? "Episode kimliği bildirilmedi"}>
                        {r.episode_id ? r.episode_id.slice(0, 8) : DASH}
                      </td>
                      <td className={toNum(r.expires_ms) === null ? "muted" : "dim"}>
                        {toNum(r.expires_ms) === null
                          ? DASH
                          : formatCountdownMs(r.expires_ms ?? null, now)}
                      </td>
                      <td className="c">
                        <span className={`ostat ${opportunityStatusTone(r.status)}`}>
                          {opportunityStatusLabel(r.status)}
                        </span>
                      </td>
                      <td
                        className={`l ${isReject ? "warn" : "muted"}`}
                        title={r.reject_reason ?? undefined}
                      >
                        {r.reject_reason ? rejectReasonLabel(r.reject_reason) : DASH}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Panel>
  );
}
