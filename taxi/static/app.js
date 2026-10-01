"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const dark = matchMedia("(prefers-color-scheme: dark)").matches;
const fmt = (v, d = 0) =>
  v == null ? "—" : typeof v === "number"
    ? v.toLocaleString("zh-CN", { maximumFractionDigits: d }) : v;
const big = (v) => v == null ? "—" : v >= 1e8 ? `${(v / 1e8).toFixed(2)} 亿` : v >= 1e4 ? `${(v / 1e4).toFixed(1)} 万` : fmt(v);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const SERVICE_LABEL = { yellow: "黄色出租车", green: "绿色出租车", fhvhv: "网约车 (HVFHV)" };
const COLORS = { yellow: "#f2b705", green: "#3aa35c", fhvhv: "#5b6cff",
  Yellow: "#f2b705", Green: "#3aa35c", Uber: "#2b2f38", Lyft: "#ff00bf", Via: "#2ba4d9", Juno: "#8a5cf6" };
if (dark) COLORS.Uber = "#c9ced8";
const DOW = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
const COL_LABEL = {
  zone: "区域", borough: "行政区", origin: "起点", destination: "终点", trips: "行程数",
  share_pct: "占比 %", avg_total: "平均实付 $", avg_miles: "平均英里", median_min: "中位时长 (分)",
  mph: "车速 mph", company: "公司", avg_fare: "平均车费 $", fare_per_mile: "$/英里",
  fare_per_min: "$/分钟", tip_pct: "小费率 %", driver_pay_pct: "司机分成 %", avg_wait_min: "平均等车 (分)",
  p90_wait_min: "P90 等车 (分)", cbd_fee_pct: "缴拥堵费 %", shared_pct: "拼车 %",
  hour: "时间", expected: "基线", ratio: "倍数", z_score: "Z 分数",
};

let STATUS = null;
const charts = new Map();

async function api(path, opts) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || r.statusText);
  return body;
}

function filterQuery() {
  const p = new URLSearchParams();
  p.set("services", $$("#svc input:checked").map((i) => i.value).join(","));
  if ($("#start").value) p.set("start", $("#start").value);
  if ($("#end").value) p.set("end", $("#end").value);
  if ($("#borough").value) p.set("borough", $("#borough").value);
  return p.toString();
}

function chart(card) {
  const el = $(".chart", card);
  if (!charts.has(el)) charts.set(el, echarts.init(el, dark ? "dark" : null));
  return charts.get(el);
}
addEventListener("resize", () => charts.forEach((c) => c.resize()));
const axisNum = (v) => (Math.abs(v) >= 1e4 ? `${+(v / 1e4).toFixed(1)}万` : v);
const base = { backgroundColor: "transparent", grid: { left: 12, right: 20, top: 40, bottom: 12, containLabel: true },
  tooltip: { trigger: "axis" }, legend: { top: 0, type: "scroll" } };

function table(rows, numeric = true, labels = COL_LABEL) {
  if (!rows.length) return '<p class="muted">无数据</p>';
  const cols = Object.keys(rows[0]);
  const isNum = (c) => numeric && typeof rows.find((r) => r[c] != null)?.[c] === "number";
  return `<table><thead><tr>${cols.map((c) => `<th class="${isNum(c) ? "num" : ""}">${esc(labels[c] || c)}</th>`).join("")}</tr></thead>
  <tbody>${rows.map((r) => `<tr>${cols.map((c) => `<td class="${isNum(c) ? "num" : ""}">${esc(fmt(r[c], 2))}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

function pivot(rows, x, series, y) {
  const xs = [...new Set(rows.map((r) => r[x]))];
  const ss = [...new Set(rows.map((r) => r[series]))];
  const idx = new Map(xs.map((v, i) => [v, i]));
  const data = Object.fromEntries(ss.map((s) => [s, xs.map(() => null)]));
  rows.forEach((r) => (data[r[series]][idx.get(r[x])] = r[y]));
  return { xs, ss, data };
}

const RENDER = {
  daily(card, rows) {
    const { xs, ss, data } = pivot(rows, "day", "service", "trips");
    const ma = pivot(rows, "day", "service", "ma7");
    const total = xs.map((_, i) => ss.reduce((a, s) => a + (ma.data[s][i] || 0), 0));
    chart(card).setOption({ ...base, xAxis: { type: "category", data: xs },
      yAxis: { type: "value", name: "行程/日", axisLabel: { formatter: axisNum } },
      series: [...ss.map((s) => ({ name: SERVICE_LABEL[s], type: "bar", stack: "t", data: data[s],
        itemStyle: { color: COLORS[s] }, barCategoryGap: "15%" })),
        { name: "合计 7 日均线", type: "line", data: total, symbol: "none", smooth: true,
          lineStyle: { width: 2.5, color: dark ? "#fff" : "#1d2330" }, itemStyle: { color: dark ? "#fff" : "#1d2330" } }],
      dataZoom: [{ type: "inside" }] }, true);
  },
  heatmap(card, rows) {
    const max = Math.max(...rows.map((r) => r.avg_trips));
    chart(card).setOption({ ...base, tooltip: { position: "top",
        formatter: (p) => `${DOW[p.value[1]]} ${p.value[0]}:00<br>平均 ${fmt(p.value[2])} 单/小时` },
      grid: { left: 12, right: 20, top: 10, bottom: 70, containLabel: true },
      xAxis: { type: "category", data: [...Array(24).keys()].map((h) => `${h}时`), splitArea: { show: true } },
      yAxis: { type: "category", data: DOW, inverse: true },
      visualMap: { min: 0, max, calculable: true, orient: "horizontal", left: "center", bottom: 0,
        inRange: { color: dark ? ["#1a1e26", "#7a5c00", "#f2b705", "#fff3c4"] : ["#fffbea", "#f7d35c", "#e08a00", "#7a2e00"] } },
      series: [{ type: "heatmap", data: rows.map((r) => [r.hour, r.dow - 1, r.avg_trips]) }] }, true);
  },
  market_share(card, rows) {
    const { xs, ss, data } = pivot(rows, "month", "company", "share_pct");
    chart(card).setOption({ ...base, xAxis: { type: "category", data: xs.map((m) => m.slice(0, 7)) },
      yAxis: { type: "value", max: 100, axisLabel: { formatter: "{value}%" } },
      series: ss.map((s) => ({ name: s, type: "bar", stack: "s", data: data[s], itemStyle: { color: COLORS[s] } })) }, true);
  },
  distance_hist(card, rows) {
    const { xs, ss, data } = pivot(rows, "miles", "service", "trips");
    chart(card).setOption({ ...base, xAxis: { type: "category", name: "英里", data: xs.map((m) => (m === 30 ? "30+" : m)) },
      yAxis: { type: "value", axisLabel: { formatter: axisNum } },
      series: ss.map((s) => ({ name: SERVICE_LABEL[s], type: "bar", stack: "d", data: data[s], itemStyle: { color: COLORS[s] } })) }, true);
  },
  top_zones(card, rows) {
    const r = [...rows].reverse();
    chart(card).setOption({ ...base, grid: { left: 12, right: 60, top: 10, bottom: 12, containLabel: true }, legend: { show: false },
      xAxis: { type: "value", axisLabel: { formatter: axisNum } }, yAxis: { type: "category", data: r.map((x) => `${x.zone}（${x.borough}）`) },
      tooltip: { trigger: "axis", formatter: (p) => { const x = r[p[0].dataIndex];
        return `${esc(x.zone)}<br>${fmt(x.trips)} 次 · ${x.share_pct}%<br>平均实付 $${x.avg_total} · ${x.avg_miles} 英里`; } },
      series: [{ type: "bar", data: r.map((x) => x.trips), itemStyle: { color: "#f2b705" },
        label: { show: true, position: "right", formatter: (p) => `${r[p.dataIndex].share_pct}%` } }] }, true);
  },
  borough_flows(card, rows) {
    const names = new Set();
    const links = rows.map((r) => { names.add(r.source); names.add(r.target + " "); return { source: r.source, target: r.target + " ", value: r.trips }; });
    chart(card).setOption({ backgroundColor: "transparent", tooltip: { trigger: "item" },
      series: [{ type: "sankey", left: 8, right: 110, data: [...names].map((name) => ({ name })), links,
        emphasis: { focus: "adjacency" }, lineStyle: { color: "gradient", opacity: 0.35 } }] }, true);
  },
  od_pairs(card, rows) { $(".table", card).innerHTML = table(rows); },
  airports(card, rows) {
    const hours = [...Array(24).keys()];
    const groups = {};
    rows.forEach((r) => ((groups[`${r.airport} ${r.direction}`] ||= hours.map(() => 0))[r.hour] = r.trips));
    chart(card).setOption({ ...base, xAxis: { type: "category", data: hours.map((h) => `${h}时`) }, yAxis: { type: "value", axisLabel: { formatter: axisNum } },
      series: Object.entries(groups).map(([name, data]) => ({ name, type: "line", data, smooth: true, symbol: "none",
        lineStyle: { type: name.includes("前往") ? "dashed" : "solid" } })) }, true);
  },
  economics(card, rows) { $(".table", card).innerHTML = table(rows); },
  speed_by_hour(card, rows) {
    const { xs, ss, data } = pivot(rows, "hour", "day_type", "mph");
    chart(card).setOption({ ...base, xAxis: { type: "category", data: xs.map((h) => `${h}时`) },
      yAxis: { type: "value", name: "mph", scale: true },
      series: ss.map((s) => ({ name: s, type: "line", data: data[s], smooth: true })) }, true);
  },
  wait_times(card, rows) {
    const p50 = pivot(rows, "hour", "company", "p50_wait");
    const p90 = pivot(rows, "hour", "company", "p90_wait");
    chart(card).setOption({ ...base, xAxis: { type: "category", data: p50.xs.map((h) => `${h}时`) },
      yAxis: { type: "value", name: "分钟" },
      series: [...p50.ss.map((s) => ({ name: `${s} 中位`, type: "line", data: p50.data[s], itemStyle: { color: COLORS[s] }, symbol: "none" })),
        ...p90.ss.map((s) => ({ name: `${s} P90`, type: "line", data: p90.data[s], itemStyle: { color: COLORS[s] }, symbol: "none", lineStyle: { type: "dashed" } }))] }, true);
  },
  anomalies(card, rows) {
    $(".table", card).innerHTML = table(rows.map((r) => ({ ...r, hour: r.hour.replace("T", " ").slice(0, 16) })));
  },
};

function cardHead(card, name) {
  if ($(".card-head", card)) return;
  const a = STATUS.analyses[name];
  card.insertAdjacentHTML("afterbegin",
    `<div class="card-head"><h3>${esc(a.title)}</h3><div class="meta"><span class="badge"></span><button class="show-sql">SQL</button></div></div><p class="doc">${esc(a.doc)}</p>`);
  $(".show-sql", card).onclick = () => openSql(name, card.dataset.sql);
}

async function load(card) {
  const name = card.dataset.a;
  cardHead(card, name);
  card.classList.add("loading");
  try {
    const res = await api(`/api/analysis/${name}?${filterQuery()}`);
    card.dataset.sql = res.sql;
    const badge = $(".badge", card);
    badge.textContent = `${fmt(res.ms, 1)} ms`;
    badge.classList.toggle("cached", res.cached);
    $(".error", card)?.remove();
    RENDER[name](card, res.rows);
    return res.cached ? 0 : res.ms;
  } catch (e) {
    $(".error", card)?.remove();
    card.insertAdjacentHTML("beforeend", `<p class="error">${esc(e.message)}</p>`);
    return 0;
  } finally {
    card.classList.remove("loading");
  }
}

async function loadKpis() {
  const { rows: [k], ms } = await api(`/api/analysis/overview?${filterQuery()}`);
  const items = [["行程数", fmt(k.trips)], ["日均行程", fmt(k.trips_per_day)], ["乘客总支付", "$" + big(k.revenue)],
    ["平均里程", fmt(k.avg_miles, 2) + " mi"], ["中位 / P90 时长（分钟）", `${fmt(k.median_min, 1)} / ${fmt(k.p90_min, 1)}`],
    ["平均车速", fmt(k.avg_mph, 1) + " mph"], ["小费率（刷卡/App）", fmt(k.tip_pct, 1) + "%"], ["覆盖天数", fmt(k.days)]];
  $("#kpis").innerHTML = items.map(([l, v]) => `<div class="kpi"><div class="v">${v}</div><div class="l">${l}</div></div>`).join("");
  return ms;
}

async function refreshVisible() {
  const tab = $(".tab:not([hidden])");
  const cards = $$("[data-a]", tab);
  const t0 = performance.now();
  const jobs = cards.map(load);
  if (tab.id === "tab-demand") jobs.push(loadKpis());
  if (!jobs.length) return;
  const ms = await Promise.all(jobs);
  $("#total-ms").textContent = `${cards.length + (tab.id === "tab-demand")} 个查询 · 数据库耗时合计 ${fmt(ms.reduce((a, b) => a + b, 0), 0)} ms · 端到端 ${fmt(performance.now() - t0, 0)} ms`;
  cards.forEach((c) => $(".chart", c) && charts.get($(".chart", c))?.resize());
}

// ---- SQL dialog
function openSql(name, sql) {
  const d = $("#sql-dialog");
  $("#dlg-title").textContent = STATUS.analyses[name].title;
  $("#dlg-sql").textContent = sql || "";
  $("#dlg-plan").textContent = "";
  $("#dlg-explain").onclick = async () => {
    $("#dlg-plan").textContent = "执行中…";
    try { $("#dlg-plan").textContent = (await api(`/api/explain/${name}?${filterQuery()}`)).plan; }
    catch (e) { $("#dlg-plan").textContent = e.message; }
  };
  d.showModal();
}
$("#dlg-close").onclick = () => $("#sql-dialog").close();

// ---- benchmark
$("#run-bench").onclick = async (ev) => {
  ev.target.disabled = true;
  $("#bench-out").innerHTML = '<p class="muted">运行中…（Parquet 直接查询需要现场解码与规范化，会更慢）</p>';
  try {
    const b = await api("/api/bench");
    const max = Math.max(...b.results.flatMap((r) => [r.table_ms, r.parquet_ms || 0]));
    $("#bench-out").innerHTML = `<p>数据量：<b>${fmt(b.rows)}</b> 行</p><div class="table"><table><thead><tr>
      <th>查询</th><th class="num">DuckDB 表 (ms)</th><th></th><th class="num">原始 Parquet (ms)</th><th></th><th class="num">吞吐（行/秒）</th></tr></thead><tbody>
      ${b.results.map((r) => `<tr title="${esc(r.sql)}"><td>${esc(r.query)}</td>
        <td class="num">${fmt(r.table_ms, 1)}</td><td><div class="bar"><i style="width:${(100 * r.table_ms) / max}%"></i></div></td>
        <td class="num">${fmt(r.parquet_ms, 1)}</td><td><div class="bar"><i style="width:${(100 * (r.parquet_ms || 0)) / max}%"></i></div></td>
        <td class="num">${fmt(r.rows_per_sec)}</td></tr>`).join("")}</tbody></table></div>`;
  } catch (e) { $("#bench-out").innerHTML = `<p class="error">${esc(e.message)}</p>`; }
  ev.target.disabled = false;
};

// ---- SQL lab
const EXAMPLES = {
  "各公司按小时份额": `SELECT hour(pickup_at) AS h, company, count(*) AS trips,
       round(100 * count(*) / sum(count(*)) OVER (PARTITION BY hour(pickup_at)), 1) AS pct
FROM trips GROUP BY ALL ORDER BY h, trips DESC;`,
  "拥堵费前后对比": `SELECT pickup_at::DATE AS day, count(*) FILTER (WHERE cbd_fee > 0) AS cbd_trips,
       count(*) AS trips, round(100 * cbd_trips / trips, 1) AS pct
FROM trips GROUP BY ALL ORDER BY day;`,
  "跨区通勤 Top": `SELECT pz.zone AS origin, dz.zone AS dest, count(*) AS trips
FROM trips t JOIN zones pz ON pz.location_id = t.pu_location_id JOIN zones dz ON dz.location_id = t.do_location_id
WHERE pz.borough <> dz.borough AND isodow(pickup_at) <= 5 AND hour(pickup_at) BETWEEN 7 AND 9
GROUP BY ALL ORDER BY trips DESC LIMIT 20;`,
  "导入吞吐": `SELECT service, month, loaded_rows, raw_rows - loaded_rows AS dropped, seconds,
       round(loaded_rows / seconds) AS rows_per_sec FROM ingested_files ORDER BY month, service;`,
};
$("#examples").innerHTML = Object.keys(EXAMPLES).map((k) => `<button data-k="${esc(k)}">${esc(k)}</button>`).join("");
$$("#examples button").forEach((b) => (b.onclick = () => ($("#sql").value = EXAMPLES[b.dataset.k])));
async function runSql() {
  const out = $("#sql-out");
  out.innerHTML = '<p class="muted">执行中…</p>';
  try {
    const r = await api("/api/sql", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sql: $("#sql").value }) });
    const rows = r.rows.map((row) => Object.fromEntries(r.columns.map((c, i) => [c, row[i]])));
    out.innerHTML = `<p class="muted">${rows.length}${r.truncated ? "+（已截断）" : ""} 行 · ${fmt(r.ms, 1)} ms</p><div class="table">${table(rows, true, {})}</div>`;
  } catch (e) { out.innerHTML = `<p class="error">${esc(e.message)}</p>`; }
}
$("#run-sql").onclick = runSql;
$("#sql").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) runSql(); });

// ---- ingest
function renderFiles() {
  $("#files").innerHTML = `<div class="table">${table(STATUS.files.map((f) => ({
    服务: f.service, 月份: f.month, 原始行数: f.raw_rows, 导入行数: f.loaded_rows, "MB": f.mb, "秒": f.seconds, "行/秒": f.rows_per_sec })))}</div>`;
}
let polling = null;
async function pollJobs() {
  const jobs = await api("/api/jobs");
  $("#jobs").innerHTML = jobs.slice(0, 5).map((j) => `<div class="job"><b>任务 #${j.id}</b>
    <span class="muted">${j.finished ? "完成" : "进行中"} · ${j.elapsed}s</span>
    ${table(j.files.map((f) => ({ 文件: f.file_name, 状态: f.state,
      下载: f.size ? `${Math.round((100 * f.downloaded) / f.size)}%` : "", 行数: f.rows, 秒: f.seconds, 错误: f.error || "" })))}</div>`).join("");
  if (jobs.some((j) => !j.finished)) return;
  clearInterval(polling); polling = null;
  STATUS = await api("/api/status"); renderStatus(); renderFiles();
}
$("#start-ingest").onclick = async () => {
  try {
    await api("/api/ingest", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ services: $$("#ingest-svc input:checked").map((i) => i.value), months: $("#ingest-months").value }) });
    if (!polling) polling = setInterval(pollJobs, 1000);
    pollJobs();
  } catch (e) { $("#jobs").innerHTML = `<p class="error">${esc(e.message)}</p>`; }
};

// ---- boot
function renderStatus() {
  const s = STATUS;
  $("#chips").innerHTML = [["行程", fmt(s.trips)], ["数据库", `${fmt(s.db_mb, 1)} MB`],
    ["范围", s.first_day ? `${s.first_day} ~ ${s.last_day}` : "空"], ["DuckDB", s.duckdb_version], ["线程", s.threads]]
    .map(([l, v]) => `<span class="chip">${l} <b>${esc(v)}</b></span>`).join("");
  if (s.first_day && !$("#start").value) { $("#start").value = s.first_day; $("#end").value = s.last_day; }
}

async function boot() {
  STATUS = await api("/api/status");
  const boxes = (id, checked) => ($(id).insertAdjacentHTML("beforeend", STATUS.services.map((s) =>
    `<label><input type="checkbox" value="${s}" ${checked(s) ? "checked" : ""}> ${SERVICE_LABEL[s]}</label>`).join("")));
  boxes("#svc", () => true);
  boxes("#ingest-svc", (s) => s !== "fhvhv");
  $("#borough").insertAdjacentHTML("beforeend", STATUS.boroughs.map((b) => `<option>${b}</option>`).join(""));
  renderStatus(); renderFiles();
  $$("#tabs button").forEach((b) => (b.onclick = () => {
    $$("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
    $$(".tab").forEach((t) => (t.hidden = t.id !== `tab-${b.dataset.tab}`));
    if (b.dataset.tab === "ingest") pollJobs();
    if (STATUS.trips) refreshVisible();
  }));
  $("#apply").onclick = refreshVisible;
  if (STATUS.trips) refreshVisible();
  else $$("#tabs button").find((b) => b.dataset.tab === "ingest").click();
}
boot();
