/* 红利股票跟踪 - 前端逻辑（原生JS + ECharts） */
"use strict";

const $ = (s) => document.querySelector(s);
const state = {
  stocks: [], boards: [], snapshot: null,
  sort: { k: "dy", asc: false },
  search: "", industry: "",
  chart: null, trendCharts: [], current: null, k: null, h: null,
  company: null, finChart: null, finGran: "annual", finKey: null,
};

const fmt = (v, d = 2) =>
  v === null || v === undefined || isNaN(v) ? "—" : Number(v).toFixed(d);

async function load() {
  try {
    const [snap, b] = await Promise.all([
      fetch("data/snapshot.json").then((r) => r.json()),
      fetch("data/meta/boards.json").then((r) => r.json()),
    ]);
    state.snapshot = snap;
    state.stocks = snap.items || [];
    state.boards = (b && b.boards) || [];
    const up = (snap.updated_at || "").replace("T", " ").slice(0, 16);
    $("#meta").textContent =
      "数据日期 " + snap.date + " · 更新于 " + up + " · 共 " + snap.total + " 只";
    renderList();
    renderBoards();
  } catch (e) {
    $("#list-info").textContent = "数据加载失败：" + e.message;
  }
}

function filtered() {
  let list = state.stocks;
  if (state.industry) list = list.filter((s) => s.industry === state.industry);
  const q = state.search.trim().toLowerCase();
  if (q) {
    list = list.filter(
      (s) =>
        s.code.includes(q) ||
        (s.name || "").toLowerCase().includes(q) ||
        (s.industry || "").toLowerCase().includes(q) ||
        (s.tags || []).some((t) => t.toLowerCase().includes(q))
    );
  }
  const { k, asc } = state.sort;
  list = list.slice().sort((a, b) => {
    const va = a[k], vb = b[k];
    const na = va === null || va === undefined || va === "";
    const nb = vb === null || vb === undefined || vb === "";
    if (na && nb) return 0;
    if (na) return 1;
    if (nb) return -1;
    if (typeof va === "string" || typeof vb === "string")
      return asc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
    return asc ? va - vb : vb - va;
  });
  return list;
}

function renderList() {
  const tbody = $("#stock-table tbody");
  const list = filtered();
  tbody.innerHTML = list.map((s) => {
    const pct = s.pct === null || s.pct === undefined ? null : Number(s.pct);
    const pctCls = pct > 0 ? "red" : pct < 0 ? "green" : "";
    const pctTxt = pct === null ? "—" : (pct > 0 ? "+" : "") + fmt(pct);
    const tags = (s.tags || []).map((t) => `<span class="tag">${t}</span>`).join("");
    return `<tr data-code="${s.code}">
      <td>${s.code}</td><td>${s.name || "—"}</td>
      <td class="num">${fmt(s.close)}</td>
      <td class="num ${pctCls}">${pctTxt}</td>
      <td class="num">${fmt(s.dy)}</td>
      <td class="num">${fmt(s.pe)}</td>
      <td class="num">${fmt(s.pb)}</td>
      <td class="num">${fmt(s.mv, 1)}</td>
      <td class="num">${fmt(s.roe)}</td>
      <td>${s.industry || "—"}</td><td>${tags}</td></tr>`;
  }).join("");
  const info = [];
  if (state.industry) info.push(`行业筛选：${state.industry}`);
  info.push(`显示 ${list.length} / ${state.stocks.length} 只`);
  $("#list-info").textContent = info.join(" · ");
  tbody.querySelectorAll("tr").forEach((tr) =>
    tr.addEventListener("click", () => openDetail(tr.dataset.code))
  );
}

function renderBoards() {
  const tbody = $("#board-table tbody");
  tbody.innerHTML = state.boards.map((b) => {
    const pct = b.pct === null || b.pct === undefined ? null : Number(b.pct);
    const cls = pct > 0 ? "red" : pct < 0 ? "green" : "";
    const txt = pct === null ? "—" : (pct > 0 ? "+" : "") + fmt(pct);
    return `<tr data-ind="${b.name}">
      <td>${b.name}</td><td class="num">${fmt(b.mv_b, 1)}</td>
      <td class="num">${b.count}</td><td class="num">${fmt(b.avg_dy)}</td>
      <td class="num">${fmt(b.avg_pe)}</td><td class="num">${fmt(b.avg_roe)}</td>
      <td class="num ${cls}">${txt}</td></tr>`;
  }).join("");
  tbody.querySelectorAll("tr").forEach((tr) =>
    tr.addEventListener("click", () => {
      state.industry = tr.dataset.ind;
      showView("list");
      renderList();
    })
  );
}

function showView(name) {
  ["list", "boards", "detail"].forEach((v) => {
    $("#view-" + v).hidden = v !== name;
  });
  $("#tab-list").classList.toggle("active", name === "list");
  $("#tab-boards").classList.toggle("active", name === "boards");
  window.scrollTo(0, 0);
}

async function openDetail(code) {
  showView("detail");
  state.current = code;
  state.finGran = "annual";
  state.finKey = null;
  document.querySelectorAll("#fin-gran button").forEach((b) =>
    b.classList.toggle("active", b.dataset.g === "annual"));
  const it = state.stocks.find((x) => x.code === code) || {};
  $("#d-title").textContent = `${it.name || code} (${code})`;
  $("#d-sub").textContent =
    "行业：" + (it.industry || "—") +
    " · 标签：" + ((it.tags || []).join(" / ") || "—");
  renderCards(it);
  state.trendCharts.forEach((c) => c && c.dispose());
  state.trendCharts = [];
  const [k, h] = await Promise.all([
    fetch("data/kline/" + code + ".json").then((r) => r.json()).catch(() => null),
    fetch("data/history/" + code + ".json").then((r) => r.json()).catch(() => null),
  ]);
  state.k = k;
  state.h = h;
  renderChart();
  renderTrends(h);
  loadCompany(code);
}

async function loadCompany(code) {
  $("#ci-loading").hidden = false;
  $("#ci-body").hidden = true;
  state.company = null;
  try {
    const d = await fetch("data/company/" + code + ".json").then((r) => r.json());
    state.company = d;
    renderCompany(d);
    $("#ci-loading").hidden = true;
    $("#ci-body").hidden = false;
  } catch (e) {
    $("#ci-loading").textContent = "公司基本面数据暂不可用（可能尚未抓取）。";
  }
}

function renderCards(it) {
  const pct = it.pct === null || it.pct === undefined ? null : Number(it.pct);
  const cls = pct > 0 ? "red" : pct < 0 ? "green" : "";
  const pctTxt = pct === null ? "—" : (pct > 0 ? "+" : "") + fmt(pct);
  const cards = [
    ["现价", fmt(it.close), ""],
    ["涨跌幅%", pctTxt, cls],
    ["股息率TTM%", fmt(it.dy), ""],
    ["市盈率TTM", fmt(it.pe), ""],
    ["市净率", fmt(it.pb), ""],
    ["总市值(亿)", fmt(it.mv, 1), ""],
    ["ROE%", fmt(it.roe), ""],
    ["ROE报告期", it.roe_date || "—", ""],
  ];
  $("#d-cards").innerHTML = cards
    .map(([k, v, c]) => `<div class="card"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`)
    .join("");
}

function ma(bars, n) {
  return bars.map((b, i) =>
    i < n - 1 ? null : +((bars.slice(i - n + 1, i + 1).reduce((s, x) => s + x.c, 0) / n).toFixed(2))
  );
}

function boll(bars, n = 20, k = 2) {
  return bars.map((b, i) => {
    if (i < n - 1) return null;
    const win = bars.slice(i - n + 1, i + 1);
    const m = win.reduce((s, x) => s + x.c, 0) / n;
    const v = win.reduce((s, x) => s + (x.c - m) * (x.c - m), 0) / n;
    const sd = Math.sqrt(v);
    return [+(m + k * sd).toFixed(2), +m.toFixed(2), +(m - k * sd).toFixed(2)];
  });
}

function dmi(bars, n = 14) {
  const tr = [], pdm = [], ndm = [];
  for (let i = 0; i < bars.length; i++) {
    if (i === 0) { tr.push(0); pdm.push(0); ndm.push(0); continue; }
    const h = bars[i].h, l = bars[i].l, pc = bars[i - 1].c;
    tr.push(Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc)));
    const up = h - pc, dn = pc - l;
    pdm.push(up > dn && up > 0 ? up : 0);
    ndm.push(dn > up && dn > 0 ? dn : 0);
  }
  const atr = [], pdi = [], ndi = [], dx = [];
  let a = 0, sp = 0, sn = 0;
  for (let i = 0; i < bars.length; i++) {
    if (i < n) {
      a += tr[i]; sp += pdm[i]; sn += ndm[i];
      if (i === n - 1) { a /= n; sp /= n; sn /= n; }
    } else {
      a = (a * (n - 1) + tr[i]) / n;
      sp = (sp * (n - 1) + pdm[i]) / n;
      sn = (sn * (n - 1) + ndm[i]) / n;
    }
    atr.push(a); pdi.push(a ? 100 * sp / a : 0); ndi.push(a ? 100 * sn / a : 0);
    dx.push(pdi[i] + ndi[i] > 0 ? 100 * Math.abs(pdi[i] - ndi[i]) / (pdi[i] + ndi[i]) : 0);
  }
  const adx = [];
  let ad = 0;
  for (let i = 0; i < bars.length; i++) {
    if (i < 2 * n) {
      ad += dx[i];
      if (i === 2 * n - 1) ad /= n;
    } else {
      ad = (ad * (n - 1) + dx[i]) / n;
    }
    adx.push(i < 2 * n - 1 ? null : +ad.toFixed(2));
  }
  return {
    pdi: pdi.map((v, i) => (i < n - 1 ? null : +v.toFixed(2))),
    ndi: ndi.map((v, i) => (i < n - 1 ? null : +v.toFixed(2))),
    adx,
  };
}

function line(name, data, color, width = 1) {
  return {
    name, type: "line", data, showSymbol: false, smooth: true,
    lineStyle: { width, color: color || undefined }, symbol: "none",
  };
}

function renderChart() {
  const el = $("#chart");
  if (!state.k || !state.k.bars || !state.k.bars.length) {
    el.innerHTML = '<div class="muted" style="padding:20px">暂无K线数据</div>';
    return;
  }
  if (!state.chart) {
    state.chart = echarts.init(el);
    window.addEventListener("resize", () => state.chart.resize());
  }
  const bars = state.k.bars;
  const dates = bars.map((b) => b.d);
  const ohlc = bars.map((b) => [b.o, b.c, b.l, b.h]);
  const vol = bars.map((b) => b.v);
  const showMA = $("#ck-ma").checked;
  const showB = $("#ck-boll").checked;
  const showD = $("#ck-dmi").checked;
  const series = [{
    name: "K线", type: "candlestick", data: ohlc,
    itemStyle: {
      color: "#d32f2f", color0: "#2e7d32",
      borderColor: "#d32f2f", borderColor0: "#2e7d32",
    },
  }];
  const legend = ["K线"];
  if (showMA) {
    [["MA5", ma(bars, 5), "#f6a800"], ["MA10", ma(bars, 10), "#e0439e"],
     ["MA20", ma(bars, 20), "#1e5eff"], ["MA60", ma(bars, 60), "#7b1fa2"]]
      .forEach(([n, d, c]) => { series.push(line(n, d, c)); legend.push(n); });
  }
  if (showB) {
    const bl = boll(bars);
    [["BOLL上", bl.map((x) => x && x[0]), "#f57c00"],
     ["BOLL中", bl.map((x) => x && x[1]), "#f9a825"],
     ["BOLL下", bl.map((x) => x && x[2]), "#f57c00"]]
      .forEach(([n, d, c]) => { series.push(line(n, d, c)); legend.push(n); });
  }
  let sub;
  if (showD) {
    const dm = dmi(bars);
    sub = [
      line("+DI", dm.pdi, "#e53935"),
      line("-DI", dm.ndi, "#43a047"),
      line("ADX", dm.adx, "#1e88e5"),
    ];
    legend.push("+DI", "-DI", "ADX");
  } else {
    sub = [{
      name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: vol,
      itemStyle: { color: (p) => (bars[p.dataIndex].c >= bars[p.dataIndex].o ? "#d32f2f" : "#2e7d32") },
    }];
    legend.push("成交量");
  }
  series.push(...sub);
  const option = {
    animation: false,
    legend: { data: legend, top: 0, type: "scroll", textStyle: { fontSize: 11 } },
    tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    grid: [
      { left: 10, right: 14, top: 34, height: "52%", containLabel: true },
      { left: 10, right: 14, top: "74%", height: "14%", containLabel: true },
    ],
    xAxis: [
      { type: "category", data: dates, gridIndex: 0, boundaryGap: true, axisLine: { onZero: false }, axisLabel: { show: false } },
      { type: "category", data: dates, gridIndex: 1, axisLabel: { show: false }, axisTick: { show: false } },
    ],
    yAxis: [
      { scale: true, gridIndex: 0, splitLine: { lineStyle: { color: "#eef0f2" } } },
      { scale: true, gridIndex: 1, axisLabel: { show: false }, splitLine: { show: false } },
    ],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1], start: 55, end: 100 },
      { type: "slider", xAxisIndex: [0, 1], top: "90%", height: 16, start: 55, end: 100 },
    ],
    series,
  };
  state.chart.setOption(option, true);
}

function renderTrends(h) {
  const box = $("#trends");
  box.innerHTML = "";
  if (!h || !h.length) {
    box.innerHTML = '<div class="muted">暂无指标历史</div>';
    return;
  }
  const ds = h.map((x) => x.date);
  const defs = [
    ["股息率TTM%", "dy", "#e53935"],
    ["市盈率TTM", "pe", "#1e5eff"],
    ["ROE%", "roe", "#43a047"],
  ];
  defs.forEach(([title, key, color]) => {
    const wrap = document.createElement("div");
    wrap.className = "t";
    wrap.innerHTML = `<h4>${title}</h4><div></div>`;
    box.appendChild(wrap);
    const chart = echarts.init(wrap.querySelector("div"));
    state.trendCharts.push(chart);
    chart.setOption({
      animation: false,
      grid: { left: 8, right: 12, top: 8, bottom: 4, containLabel: true },
      tooltip: { trigger: "axis" },
      xAxis: { type: "category", data: ds, axisLabel: { fontSize: 10 } },
      yAxis: { type: "value", scale: true, axisLabel: { fontSize: 10 } },
      series: [{
        type: "line", data: h.map((x) => x[key]), showSymbol: false,
        lineStyle: { width: 1.5, color }, areaStyle: { opacity: 0.06 },
      }],
    });
  });
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function renderCompany(d) {
  renderHolders(d.holders || {});
  renderDividend(d.dividend || {});
  renderFinance(d.finance || {});
  renderExecutives(d.executives || []);
  renderProfile(d.profile || {});
}

function renderHolders(h) {
  const ctrl = h.controller || "—";
  const cs = h.controller_shareholder || "—";
  $("#ci-holders").innerHTML =
    `<div class="holder-facts"><span><b>实际控制人</b>${esc(ctrl)}</span>` +
    `<span><b>控股股东</b>${esc(cs)}</span></div>`;
  const tb = $("#holder-table tbody");
  tb.innerHTML = (h.top10 || []).map((x) => {
    const chg = x.change == null ? "—" : String(x.change);
    const ratio = x.ratio == null ? "—" : fmt(x.ratio) + "%";
    const shares = x.shares == null ? "—" : Math.round(x.shares).toLocaleString();
    return `<tr><td>${x.rank ?? "—"}</td><td>${esc(x.name || "—")}</td>` +
      `<td class="num">${shares}</td><td class="num">${ratio}</td>` +
      `<td>${esc(chg)}</td></tr>`;
  }).join("");
}

function renderDividend(dd) {
  const avg = dd.avg5 == null ? "—" : fmt(dd.avg5, 2) + " 亿元";
  $("#ci-dividend-sum").innerHTML =
    `<span class="pill">近5年平均分红额度：<b>${avg}</b></span>` +
    `<span class="pill">历年分红记录：<b>${(dd.years || []).length}</b> 年</span>`;
  const tb = $("#dividend-table tbody");
  tb.innerHTML = (dd.years || []).map((y) => {
    const tot = y.total_div == null ? "—" : fmt(y.total_div, 2);
    const ratio = y.ratio == null ? "—" : fmt(y.ratio, 2) + "%";
    const ps = y.per_share == null ? "—" : fmt(y.per_share, 3);
    return `<tr><td>${y.year}</td><td class="num">${tot}</td>` +
      `<td class="num">${ratio}</td><td class="num">${ps}</td></tr>`;
  }).join("");
}

const FIN_GROUP_TITLES = { key: "关键指标", profit: "盈利能力", risk: "财务风险" };
function renderFinance(fin) {
  const groups = fin.groups || {};
  const wrap = $("#ci-finance");
  wrap.innerHTML = "";
  for (const gkey of ["key", "profit", "risk"]) {
    const items = groups[gkey] || [];
    if (!items.length) continue;
    const box = document.createElement("div");
    box.className = "fin-group";
    box.innerHTML = `<div class="fin-group-title">${FIN_GROUP_TITLES[gkey]}</div>`;
    const tbl = document.createElement("table");
    tbl.className = "fin-table";
    tbl.innerHTML = `<thead><tr><th>指标</th><th class="num">年度</th>` +
      `<th class="num">同比</th><th class="num">半年</th><th class="num">同比</th>` +
      `<th class="num">季度</th><th class="num">同比</th></tr></thead><tbody>` +
      items.map((it) => {
        const cell = (o) => {
          if (!o) return `<td class="num">—</td><td class="num">—</td>`;
          const v = o.v == null ? "—" : fmt(o.v, 2);
          let ycls = "num yoy", y = "—";
          if (o.yoy != null) {
            y = (o.yoy > 0 ? "+" : "") + fmt(o.yoy, 1) + "%";
            ycls += o.yoy > 0 ? " red" : " green";
          }
          return `<td class="num">${v}</td><td class="${ycls}">${y}</td>`;
        };
        return `<tr data-key="${it.key}" data-name="${it.name}" data-unit="${it.unit}" class="fin-row">` +
          `<td>${it.name}</td>${cell(it.annual)}${cell(it.half)}${cell(it.quarter)}</tr>`;
      }).join("") + `</tbody>`;
    box.appendChild(tbl);
    wrap.appendChild(box);
  }
  wrap.querySelectorAll(".fin-row").forEach((tr) =>
    tr.addEventListener("click", () => {
      state.finKey = tr.dataset.key;
      state.finName = tr.dataset.name;
      state.finUnit = tr.dataset.unit;
      renderFinanceChart();
    })
  );
  const first = wrap.querySelector(".fin-row");
  if (first) {
    state.finKey = first.dataset.key;
    state.finName = first.dataset.name;
    state.finUnit = first.dataset.unit;
    renderFinanceChart();
  } else if (state.finChart) {
    state.finChart.dispose(); state.finChart = null;
  }
}

function granLabel(g) { return g === "annual" ? "年度" : g === "half" ? "半年" : "季度"; }

function renderFinanceChart() {
  const fin = state.company && state.company.finance;
  if (!fin || !state.finKey) return;
  const gran = state.finGran;
  const series = (fin.periods || [])
    .filter((p) => p.type === gran)
    .sort((a, b) => a.date.localeCompare(b.date));
  if (!series.length) {
    $("#fin-title").textContent = state.finName + "（" + granLabel(gran) + "：无数据）";
    if (state.finChart) { state.finChart.dispose(); state.finChart = null; }
    return;
  }
  const labels = series.map((p) => p.date);
  const vals = series.map((p) => (p.vals[state.finKey] == null ? null : +p.vals[state.finKey].toFixed(2)));
  const yoys = series.map((p) => (p.yoy[state.finKey] == null ? null : +p.yoy[state.finKey].toFixed(1)));
  $("#fin-title").textContent = `${state.finName}（${granLabel(gran)}）单位：${state.finUnit || ""}`;
  if (!state.finChart) {
    state.finChart = echarts.init($("#finance-chart"));
    window.addEventListener("resize", () => state.finChart && state.finChart.resize());
  }
  state.finChart.setOption({
    animation: false,
    legend: { data: ["数值", "同比增长率"], top: 0, textStyle: { fontSize: 11 } },
    tooltip: { trigger: "axis" },
    grid: { left: 10, right: 14, top: 34, bottom: 30, containLabel: true },
    xAxis: { type: "category", data: labels, axisLabel: { fontSize: 10, rotate: labels.length > 8 ? 45 : 0 } },
    yAxis: [
      { type: "value", name: "数值", scale: true, axisLabel: { fontSize: 10 } },
      { type: "value", name: "同比%", axisLabel: { fontSize: 10, formatter: "{value}%" } },
    ],
    series: [
      { name: "数值", type: "bar", data: vals, itemStyle: { color: "#1e5eff" },
        label: { show: vals.length <= 12, position: "top", fontSize: 10 } },
      { name: "同比增长率", type: "line", yAxisIndex: 1, data: yoys, smooth: true,
        symbol: "circle", symbolSize: 6, itemStyle: { color: "#e53935" }, lineStyle: { width: 2 } },
    ],
  }, true);
}

function renderExecutives(ex) {
  const tb = $("#exec-table tbody");
  if (!ex || !ex.length) { tb.innerHTML = `<tr><td colspan="8" class="muted">暂无高管增减持记录</td></tr>`; return; }
  tb.innerHTML = ex.slice(0, 50).map((e) => {
    const dir = e.direction === "增持" ? `<span class="red">增持</span>`
      : e.direction === "减持" ? `<span class="green">减持</span>` : esc(e.direction || "—");
    const sh = e.shares == null ? "—" : (e.shares > 0 ? "+" : "") + Math.round(e.shares).toLocaleString();
    const price = e.price == null ? "—" : fmt(e.price, 2);
    const amt = e.amount == null ? "—" : Math.round(e.amount).toLocaleString();
    return `<tr><td>${e.date || "—"}</td><td>${esc(e.name || "—")}</td>` +
      `<td>${esc(e.title || "—")}</td><td>${dir}</td>` +
      `<td class="num">${sh}</td><td class="num">${price}</td>` +
      `<td class="num">${amt}</td><td>${esc(e.reason || "—")}</td></tr>`;
  }).join("");
}

function renderProfile(p) {
  if (!p || !p.org_name) { $("#ci-profile").innerHTML = `<div class="muted">暂无简介</div>`; return; }
  const row = (k, v) => v ? `<div class="pf-row"><span class="pf-k">${k}</span><span class="pf-v">${esc(v)}</span></div>` : "";
  const web = p.website ? `<a href="${esc(p.website)}" target="_blank" rel="noopener">${esc(p.website)}</a>` : "";
  $("#ci-profile").innerHTML =
    `<div class="pf-head">${esc(p.org_name)}${p.org_name_en ? ' <span class="muted">(' + esc(p.org_name_en) + ")</span>" : ""}</div>` +
    row("法定代表人", p.legal_person) + row("董事长", p.chairman) + row("总经理", p.president) +
    row("董事会秘书", p.secretary) + row("所属行业", p.industry) + row("上市板块", p.listing) +
    row("员工人数", p.emp_num == null ? "" : Math.round(p.emp_num).toLocaleString() + " 人") +
    row("注册地址", p.reg_address || p.address) + row("公司官网", web) +
    (p.business_scope ? `<div class="pf-row"><span class="pf-k">经营范围</span><span class="pf-v">${esc(p.business_scope)}</span></div>` : "") +
    (p.profile ? `<div class="pf-profile">${esc(p.profile)}</div>` : "");
}

function setupEvents() {
  $("#search").addEventListener("input", (e) => {
    state.search = e.target.value;
    renderList();
  });
  document.querySelectorAll("#stock-table th[data-k]").forEach((th) =>
    th.addEventListener("click", () => {
      const k = th.dataset.k;
      if (state.sort.k === k) state.sort.asc = !state.sort.asc;
      else state.sort = { k, asc: k === "code" || k === "name" || k === "industry" };
      renderList();
    })
  );
  $("#tab-list").addEventListener("click", () => showView("list"));
  $("#tab-boards").addEventListener("click", () => showView("boards"));
  $("#back").addEventListener("click", () => showView("list"));
  ["ck-ma", "ck-boll", "ck-dmi"].forEach((id) =>
    document.getElementById(id).addEventListener("change", renderChart)
  );
  document.querySelectorAll("#fin-gran button").forEach((b) =>
    b.addEventListener("click", () => {
      document.querySelectorAll("#fin-gran button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.finGran = b.dataset.g;
      renderFinanceChart();
    })
  );
}

setupEvents();
load();
