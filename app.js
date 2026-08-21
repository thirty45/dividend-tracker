/* 红利股票跟踪 - 前端逻辑（原生JS + ECharts） */
"use strict";

const $ = (s) => document.querySelector(s);
const state = {
  stocks: [], boards: [], snapshot: null,
  sort: { k: "dy", asc: false },
  search: "", industry: "",
  chart: null, trendCharts: [], current: null, k: null, h: null,
  divs: null, kPeriod: "day", kMode: "raw", raw: null, pyMap: null,
  insiderPage: 1,
  company: null, finChart: null, finGran: "annual", finKey: null,
  currentItem: null, tagFilter: null,
  funds: [], fundSort: { k: "scale_now", asc: false },
  fundSearch: "", view: "list", fundCurrent: null, fundChart: null,
  cbonds: { listed: [], pending: [] },
  hkStocks: [], hkSnap: null, hkPy: null, hkLoaded: false,
  hkSort: { k: "dy", asc: false }, hkSearch: "",
  hkChart: null, hkK: null, hkRaw: null, hkDivs: null,
  hkPeriod: "day", hkMode: "raw",
  hkFinGran: "annual", hkFinChart: null, hkFinKey: null,
  hkCurrent: null, hkCompany: null,
};

const fmt = (v, d = 2) =>
  v === null || v === undefined || isNaN(v) ? "—" : Number(v).toFixed(d);

// 股票/基金名称的拼音首字母（如 “长江电力” -> “cjdl”），供搜索用
function initials(name) {
  try {
    if (window.pinyinPro) {
      const arr = window.pinyinPro.pinyin(name, {
        pattern: "first", toneType: "none", type: "array",
      });
      return (arr || []).join("").toLowerCase();
    }
  } catch (e) { /* 拼音库不可用时忽略 */ }
  return "";
}

async function load() {
  try {
    const [snap, b, fnd, cbd, ins] = await Promise.all([
      fetch("data/snapshot.json").then((r) => r.json()),
      fetch("data/meta/boards.json").then((r) => r.json()),
      fetch("data/funds.json").then((r) => r.json()).catch(() => null),
      fetch("data/cbonds.json").then((r) => r.json()).catch(() => null),
      fetch("data/meta/insider_buys.json").then((r) => r.json()).catch(() => null),
    ]);
    state.snapshot = snap;
    state.stocks = snap.items || [];
    state.boards = (b && b.boards) || [];
    state.funds = (fnd && fnd.items) || [];
    state.cbonds = (cbd && cbd.listed) ? cbd : { listed: [], pending: [] };
    state.insiders = (ins && ins.items) || [];
    state.pyMap = new Map();
    state.stocks.forEach((s) => state.pyMap.set(s.code, initials(s.name || "")));
    const cbTotal = state.cbonds.listed.length + state.cbonds.pending.length;
    $("#meta").textContent =
      "数据日期 " + snap.date + " · 更新于(北京) " + toBJ(snap.updated_at) +
      " · 股票 " + snap.total + " 只" +
      (state.funds.length ? " · 基金 " + state.funds.length + " 只" : "") +
      (cbTotal ? " · 转债 " + cbTotal + " 只" : "");
    route();
  } catch (e) {
    $("#list-info").textContent = "数据加载失败：" + e.message;
  }
}

// CI 生成的 updated_at 为 UTC（GitHub Actions 默认时区），转为北京时间显示
function toBJ(s) {
  const t = Date.parse((s || "").replace(" ", "T") + "Z");
  if (isNaN(t)) return (s || "").replace("T", " ").slice(0, 16);
  const d = new Date(t + 8 * 3600 * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return d.getUTCFullYear() + "-" + p(d.getUTCMonth() + 1) + "-" + p(d.getUTCDate()) +
    " " + p(d.getUTCHours()) + ":" + p(d.getUTCMinutes());
}

// 过滤规则：删除 PE>150 或 PE<0 的股票；删除股息率连续2年<1%的股票（null/缺失保留）
const stockOK = (s) =>
  (s.pe == null || (s.pe <= 150 && s.pe >= 0)) && !s.dy_bad2;

function filtered() {
  let list = state.stocks;
  list = list.filter(stockOK);
  if (state.industry) list = list.filter((s) => s.industry === state.industry);
  const q = state.search.trim().toLowerCase();
  if (q) {
    list = list.filter(
      (s) =>
        s.code.includes(q) ||
        (s.name || "").toLowerCase().includes(q) ||
        ((state.pyMap && state.pyMap.get(s.code)) || "").includes(q) ||
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

function stockRowHTML(s) {
  const badges = [];
  if (s.dy != null && s.dy >= 5) {
    badges.push('<span class="bdg bdg-super" title="股息率≥5%（超高股息）">超</span>');
  } else if (s.dy != null && s.dy >= 3) {
    badges.push('<span class="bdg bdg-high" title="股息率≥3%（高股息）">高</span>');
  }
  if (s.ins_up) badges.push('<span class="bdg bdg-up" title="近1年高管增持">增</span>');
  if (s.ins_down) badges.push('<span class="bdg bdg-down" title="近1年高管减持">减</span>');
  const pct = s.pct == null ? null : Number(s.pct);
  const pctCls = pct > 0 ? "red" : pct < 0 ? "green" : "";
  const pctTxt = pct == null ? "—" : (pct > 0 ? "+" : "") + fmt(pct);
  const m = (v) => v == null ? "—" : (v > 0 ? "+" : "") + fmt(v);
  const mc = (v) => v == null ? "" : v > 0 ? "red" : v < 0 ? "green" : "";
  const tags = (s.tags || []).filter((t) => t !== "基金持仓")
    .map((t) => `<span class="tag" data-tag="${esc(t)}">${esc(t)}</span>`).join("");
  return `<tr data-code="${s.code}">
    <td class="stock-id">${esc(s.name || "—")}(${s.code})${badges.join("")}</td>
    <td class="num">${fmt(s.close)}</td>
    <td class="num ${pctCls}">${pctTxt}</td>
    <td class="num ${mc(s.pct_1m)}">${m(s.pct_1m)}</td>
    <td class="num ${mc(s.pct_3m)}">${m(s.pct_3m)}</td>
    <td class="num">${fmt(s.dy)}</td>
    <td class="num">${fmt(s.pe)}</td>
    <td class="num">${fmt(s.pb)}</td>
    <td class="num">${fmt(s.pb5)}</td>
    <td class="num">${fmt(s.mv, 1)}</td>
    <td class="num">${fmt(s.roe)}</td>
    <td class="num ${mc(s.exp_ret)}">${m(s.exp_ret)}</td>
    <td class="num ${mc(s.exp_ret_pb5)}">${m(s.exp_ret_pb5)}</td>
    <td>${s.industry || "—"}</td><td>${tags}</td></tr>`;
}

function renderList() {
  const tbody = $("#stock-table tbody");
  const list = filtered();
  tbody.innerHTML = list.map(stockRowHTML).join("");
  const info = [];
  if (state.industry) info.push(`行业筛选：${state.industry}`);
  info.push(`显示 ${list.length} / ${state.stocks.length} 只`);
  $("#list-info").textContent = info.join(" · ");
  tbody.querySelectorAll("tr").forEach((tr) =>
    tr.addEventListener("click", (e) => {
      if (e.target.closest(".tag")) return;
      location.hash = "#/stock/" + tr.dataset.code;
    })
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
  ["list", "boards", "detail", "tag", "funds", "fund-detail", "cbonds",
   "hk", "hk-detail"].forEach((v) => {
    const el = $("#view-" + v);
    if (el) el.hidden = v !== name;
  });
  $("#tab-list").classList.toggle("active", name === "list");
  $("#tab-boards").classList.toggle("active", name === "boards");
  $("#tab-funds").classList.toggle("active", name === "funds" || name === "fund-detail");
  $("#tab-cbonds").classList.toggle("active", name === "cbonds");
  $("#tab-hk").classList.toggle("active", name === "hk" || name === "hk-detail");
  state.view = name;
  window.scrollTo(0, 0);
}

async function openDetail(code) {
  showView("detail");
  state.current = code;
  state.finGran = "annual";
  state.finKey = null;
  const it = state.stocks.find((x) => x.code === code) || {};
  state.currentItem = it;
  document.querySelectorAll("#fin-gran button").forEach((b) =>
    b.classList.toggle("active", b.dataset.g === "annual"));
  $("#d-title").textContent = `${it.name || code} (${code})`;
  $("#d-sub").textContent =
    "行业：" + (it.industry || "—") +
    " · 标签：" + ((it.tags || []).join(" / ") || "—");
  renderCards(it);
  state.trendCharts.forEach((c) => c && c.dispose());
  state.trendCharts = [];
  const [k, h, dv, rw] = await Promise.all([
    fetch("data/kline/" + code + ".json").then((r) => r.json()).catch(() => null),
    fetch("data/history/" + code + ".json").then((r) => r.json()).catch(() => null),
    fetch("data/dividends/" + code + ".json").then((r) => r.json()).catch(() => null),
    fetch("data/kline_raw/" + code + ".json").then((r) => r.json()).catch(() => null),
  ]);
  state.k = k;
  state.h = h;
  state.raw = rw;
  state.divs = Array.isArray(dv)
    ? dv.reduce((m, x) => { m[x.ex_date] = x; return m; }, {})
    : null;
  // 图表渲染异常不能阻断公司信息加载
  try { renderChart(); } catch (e) { console.error("renderChart:", e); }
  try { renderTrends(h); } catch (e) { console.error("renderTrends:", e); }
  loadCompany(code);
}

async function loadCompany(code, attempt = 0) {
  const loading = $("#ci-loading");
  const body = $("#ci-body");
  if (!loading || !body) return;
  loading.hidden = false;
  loading.textContent = "加载公司基本面数据中…";
  body.hidden = true;
  state.company = null;
  try {
    const d = await fetch("data/company/" + code + ".json").then((r) => r.json());
    if (!d || typeof d !== "object") throw new Error("空数据");
    state.company = d;
    renderCompany(d);
    loading.hidden = true;
    body.hidden = false;
  } catch (e) {
    if (attempt === 0) {
      // 首次失败自动重试一次（应对瞬时加载问题）
      await new Promise((r) => setTimeout(r, 400));
      loadCompany(code, 1);
      return;
    }
    loading.textContent = "公司基本面数据暂不可用（" + code + "：" + e.message + "）。";
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

function isoWeek(dateStr) {
  const dt = new Date(dateStr + "T00:00:00");
  dt.setDate(dt.getDate() - ((dt.getDay() + 6) % 7)); // 回到本周一
  const m = String(dt.getMonth() + 1).padStart(2, "0");
  const dd = String(dt.getDate()).padStart(2, "0");
  return dt.getFullYear() + "-" + m + "-" + dd;
}

// 由日K聚合出周K/月K（open=首日开盘，close=末日收盘，high/low=区间极值，vol=合计）
function aggregateBars(bars, period) {
  const out = [];
  let cur = null;
  for (const b of bars) {
    const k = period === "month" ? b.d.slice(0, 7) : isoWeek(b.d);
    if (!cur || cur.k !== k) {
      if (cur) out.push(cur.bar);
      cur = { k, bar: { d: k, o: b.o, c: b.c, h: b.h, l: b.l, v: b.v } };
    } else {
      const x = cur.bar;
      x.c = b.c;
      x.h = Math.max(x.h, b.h);
      x.l = Math.min(x.l, b.l);
      x.v += b.v;
    }
  }
  if (cur) out.push(cur.bar);
  return out;
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
  // 复权模式：默认未复权（raw），缺失时回退前复权
  let bars = state.k.bars;
  const rawBars = state.raw && state.raw.bars && state.raw.bars.length
    ? state.raw.bars : null;
  if (state.kMode === "raw" && rawBars) bars = rawBars;
  const fqNote = $("#k-fq-note");
  if (fqNote) {
    fqNote.textContent = (state.kMode === "raw" && !rawBars)
      ? "未复权数据未生成，暂显示前复权"
      : "";
  }
  if (state.kPeriod !== "day") bars = aggregateBars(bars, state.kPeriod);
  const zoomStart = Math.max(0, 100 - 120 / bars.length * 100); // 默认显示近约半年
  const dates = bars.map((b) => b.d);
  const ohlc = bars.map((b) => [b.o, b.c, b.l, b.h]);
  const vol = bars.map((b) => b.v);
  const showMA = $("#ck-ma").checked;
  const showB = $("#ck-boll").checked;
  const showD = $("#ck-dmi").checked;
  // 除权除息日：日K在对应K线上标 S（悬停显示分红方案）
  const markData = (state.divs && state.kPeriod === "day")
    ? bars.map((b) => (state.divs[b.d] ? { coord: [b.d, b.l], value: "S" } : null))
      .filter(Boolean)
    : [];
  const series = [{
    name: "K线", type: "candlestick", data: ohlc,
    itemStyle: {
      color: "#d32f2f", color0: "#2e7d32",
      borderColor: "#d32f2f", borderColor0: "#2e7d32",
    },
    markPoint: markData.length ? {
      symbol: "circle", symbolSize: 15,
      itemStyle: { color: "#b08500" },
      label: { formatter: "S", color: "#fff", fontSize: 10, position: "inside" },
      data: markData,
    } : undefined,
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
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross" },
      formatter: (params) => {
        const arr = Array.isArray(params) ? params : [params];
        const first = arr[0];
        const idx = first ? first.dataIndex : -1;
        const d = idx >= 0 && bars[idx] ? bars[idx].d : "";
        const lines = [];
        const div = state.divs && state.divs[d];
        if (div) {
          lines.push("<b>" + d + " 除权除息</b>");
          lines.push('<span style="color:#b08500">分红方案：' +
            esc(div.profile || "—") + "</span>");
        } else if (d) {
          lines.push("<b>" + d + "</b>");
        }
        for (const p of arr) {
          const nm = p.seriesName;
          if (nm === "K线" && Array.isArray(p.value)) {
            lines.push("开盘：" + p.value[0]);
            lines.push("收盘：" + p.value[1]);
            lines.push("最低：" + p.value[2]);
            lines.push("最高：" + p.value[3]);
          } else if (nm === "成交量") {
            lines.push(p.marker + nm + "：" +
              (p.value == null ? "—" : Number(p.value).toLocaleString()));
          } else if (nm && nm !== "K线") {
            lines.push(p.marker + nm + "：" +
              (p.value == null ? "—" : Number(p.value).toFixed(2)));
          }
        }
        return lines.join("<br>");
      },
    },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    grid: [
      { left: 10, right: 14, top: 34, height: "52%", containLabel: true },
      { left: 10, right: 14, top: "74%", height: "14%", containLabel: true },
    ],
    xAxis: [
      {
        type: "category", data: dates, gridIndex: 0, boundaryGap: true,
        axisLine: { onZero: false },
        axisLabel: {
          show: true, fontSize: 10, hideOverlap: true,
          formatter: (v) => (v && v.length > 7 ? v.slice(5) : v),
        },
      },
      { type: "category", data: dates, gridIndex: 1, axisLabel: { show: false }, axisTick: { show: false } },
    ],
    yAxis: [
      { scale: true, gridIndex: 0, splitLine: { lineStyle: { color: "#eef0f2" } } },
      { scale: true, gridIndex: 1, axisLabel: { show: false }, splitLine: { show: false } },
    ],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1], start: zoomStart, end: 100 },
      { type: "slider", xAxisIndex: [0, 1], top: "90%", height: 16, start: zoomStart, end: 100 },
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
  const it = state.currentItem || {};
  renderHolders(d.holders || {});
  renderDividend(d.dividend || {}, it);
  renderFinance(d.finance || {});
  renderExecutives(d.executives || []);
  renderProfile(d.profile || {});
}

function renderHolders(h) {
  const soe = h.is_state_owned ? "（国有）" : "（民营）";
  const ctrl = (h.controller || "—") + soe;
  const cs = (h.controller_shareholder || "—") + soe;
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

function renderDividend(dd, it) {
  const avg = dd.avg5 == null ? "—" : fmt(dd.avg5, 2) + " 亿元";
  const dy = it && it.dy != null ? fmt(it.dy, 2) + "%" : "—";
  const dy5 = it && it.div_yield_5y != null ? fmt(it.div_yield_5y, 2) + "%" : "—";
  const dps = it && it.dps != null ? fmt(it.dps, 3) + " 元" : "—";
  $("#ci-dividend-sum").innerHTML =
    `<span class="pill">股息率(TTM)：<b>${dy}</b></span>` +
    `<span class="pill">近5年平均股息率：<b>${dy5}</b></span>` +
    `<span class="pill">每股分红(含配送股折现)：<b>${dps}</b></span>` +
    `<span class="pill">近5年平均分红额度：<b>${avg}</b></span>` +
    `<span class="pill">历年分红记录：<b>${(dd.years || []).length}</b> 年</span>`;
  const tb = $("#dividend-table tbody");
  tb.innerHTML = (dd.years || []).map((y) => {
    const tot = y.total_div == null ? "—" : fmt(y.total_div, 2);
    const ratio = y.ratio == null ? "—" : fmt(y.ratio, 2) + "%";
    const ps = y.per_share == null ? "—" : fmt(y.per_share, 3);
    // 配送股现金等价 = 除权后开盘价 × 每股配送股数（送股+转增）
    const st = (y.send_ratio || 0) + (y.trans_ratio || 0);
    const extra = (y.ex_open && st) ? fmt(y.ex_open * st, 3) : "—";
    const real = y.per_share_real != null ? fmt(y.per_share_real, 3) : "—";
    const exOpen = y.ex_open == null ? "—" : fmt(y.ex_open, 2);
    // 股息率口径：每股现金分红 ÷ 该年度不复权年末收盘价（本地计算；早年由新浪年线补齐）
    const rate = y.yield_annual != null ? fmt(y.yield_annual, 2) : "—";
    return `<tr><td>${y.year}</td><td class="plan-cell">${y.plan ? esc(y.plan) : "—"}</td>` +
      `<td class="num">${ps}</td><td class="num">${extra}</td>` +
      `<td class="num"><b>${real}</b></td><td class="num">${exOpen}</td>` +
      `<td class="num">${rate}</td><td class="num">${tot}</td>` +
      `<td class="num">${ratio}</td></tr>`;
  }).join("");
}

const FIN_GROUP_TITLES = { key: "关键指标", profit: "盈利能力", risk: "财务风险" };

function getFinChart() {
  const el = $("#finance-chart");
  let c = echarts.getInstanceByDom(el);
  if (!c) c = echarts.init(el);
  return c;
}

function granLabel(g) {
  return { annual: "年报", half: "二季度", q1: "一季度", q3: "三季度" }[g] || g;
}

function parkFinChart() {
  const el = $("#finance-chart");
  if (el && el.closest(".fin-table")) $("#fin-chart-holder").appendChild(el);
}

function renderFinance(fin) {
  parkFinChart();
  const groups = fin.groups || {};
  const gran = state.finGran;
  const periods = (fin.periods || [])
    .filter((p) => p.type === gran)
    .sort((a, b) => a.date.localeCompare(b.date))
    .slice(-10)
    .reverse(); // 从左到右：新 -> 旧，最多10年
  state.finCols = periods.length + 1;
  const colLabel = (p) => {
    const y = String(p.year || p.date.slice(0, 4));
    return { q1: y + "Q1", half: y + "H1", q3: y + "Q3", annual: y }[gran] || y;
  };
  let html = '<div class="table-wrap fin-wrap"><table class="fin-table">';
  html += "<thead><tr><th>指标</th>" +
    periods.map((p) => `<th class="num">${colLabel(p)}</th>`).join("") +
    "</tr></thead><tbody>";
  for (const gkey of ["key", "profit", "risk"]) {
    const items = groups[gkey] || [];
    if (!items.length) continue;
    html += `<tr class="fin-group-row"><td colspan="${state.finCols}">${FIN_GROUP_TITLES[gkey]}</td></tr>`;
    for (const it of items) {
      const unit = it.unit ? ` <span class="fin-unit">${esc(it.unit)}</span>` : "";
      let cells = "";
      for (const p of periods) {
        const v = (p.vals || {})[it.key];
        const yoy = (p.yoy || {})[it.key];
        let cell = "—";
        if (v != null) {
          cell = fmt(v, 2);
          if (yoy != null) {
            const cls = yoy > 0 ? "red" : yoy < 0 ? "green" : "";
            cell += `<span class="${cls} fin-yoy">(${(yoy > 0 ? "+" : "") + fmt(yoy, 1)}%)</span>`;
          }
        }
        cells += `<td class="num">${cell}</td>`;
      }
      html += `<tr class="fin-row" data-key="${it.key}" data-name="${esc(it.name)}" data-unit="${esc(it.unit || "")}">` +
        `<td>${it.name}${unit}</td>${cells}</tr>`;
    }
  }
  html += "</tbody></table></div>";
  const wrap = $("#ci-finance");
  wrap.innerHTML = html;
  wrap.querySelectorAll(".fin-row").forEach((tr) =>
    tr.addEventListener("click", () => selectMetric(tr))
  );
  // 指标图默认不展示，点击指标行才展示
  getFinChart().clear();
  const cbar = document.querySelector(".finance-chart-bar");
  if (cbar) cbar.hidden = true;
  const holder = $("#fin-chart-holder");
  if (holder) holder.hidden = true;
}

function selectMetric(tr) {
  const cbar = document.querySelector(".finance-chart-bar");
  if (cbar) cbar.hidden = false;
  parkFinChart();
  document.querySelectorAll(".fin-chart-row").forEach((r) => r.remove());
  document.querySelectorAll(".fin-row").forEach((x) => x.classList.remove("active"));
  tr.classList.add("active");
  state.finKey = tr.dataset.key;
  state.finName = tr.dataset.name;
  state.finUnit = tr.dataset.unit;
  const td = document.createElement("td");
  td.colSpan = state.finCols || 2;
  const slot = document.createElement("tr");
  slot.className = "fin-chart-row";
  slot.appendChild(td);
  tr.parentNode.insertBefore(slot, tr.nextSibling);
  td.appendChild($("#finance-chart"));
  renderFinanceChart();
}

function renderFinanceChart() {
  const fin = state.company && state.company.finance;
  if (!fin || !state.finKey) return;
  const gran = state.finGran;
  const series = (fin.periods || [])
    .filter((p) => p.type === gran)
    .sort((a, b) => a.date.localeCompare(b.date));
  if (!series.length) {
    $("#fin-title").textContent = state.finName + "（" + granLabel(gran) + "：无数据）";
    getFinChart().clear();
    return;
  }
  const labels = series.map((p) => String(p.year || p.date.slice(0, 4)));
  const vals = series.map((p) => (p.vals[state.finKey] == null ? null : +p.vals[state.finKey].toFixed(2)));
  const yoys = series.map((p) => (p.yoy[state.finKey] == null ? null : +p.yoy[state.finKey].toFixed(1)));
  $("#fin-title").textContent = `${state.finName}（${granLabel(gran)}）单位：${state.finUnit || ""}`;
  const c = getFinChart();
  c.setOption({
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
  c.resize();
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

// ==================== 港股通模块 ====================
const HK_FIN_ROWS = [
  ["roe", "净资产收益率(加权)", "%"],
  ["eps", "每股收益(基本)", "HKD"],
  ["eps_ttm", "每股收益(TTM)", "HKD"],
  ["bps", "每股净资产", "HKD"],
  ["pe", "市盈率(TTM)", ""],
  ["pb", "市净率", ""],
  ["revenue", "营业收入", "亿"],
  ["profit", "归母净利润", "亿"],
  ["gross_margin", "毛利率", "%"],
  ["net_margin", "净利率", "%"],
  ["debt_ratio", "资产负债率", "%"],
  ["roa", "总资产收益率", "%"],
  ["roic", "投入资本回报率", "%"],
];

async function loadHk() {
  if (state.hkLoaded) return;
  state.hkLoaded = true;
  try {
    const snap = await fetch("data/hk/snapshot.json").then((r) => r.json());
    state.hkSnap = snap;
    state.hkStocks = snap.items || [];
    state.hkPy = new Map();
    state.hkStocks.forEach((s) => state.hkPy.set(s.code, initials(s.name || "")));
    const meta = $("#meta");
    if (meta && snap.total) {
      meta.textContent += " · 港股通 " + snap.total + " 只";
    }
  } catch (e) {
    console.error("港股通数据加载失败:", e);
  }
}

function hkFiltered() {
  let list = state.hkStocks.slice();
  const q = state.hkSearch.trim().toLowerCase();
  if (q) {
    list = list.filter(
      (s) =>
        s.code.includes(q) ||
        (s.name || "").toLowerCase().includes(q) ||
        ((state.hkPy && state.hkPy.get(s.code)) || "").includes(q) ||
        (s.industry || "").toLowerCase().includes(q)
    );
  }
  const { k, asc } = state.hkSort;
  list.sort((a, b) => {
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

function hkRowHTML(s) {
  const pct = s.pct == null ? null : Number(s.pct);
  const pctCls = pct > 0 ? "red" : pct < 0 ? "green" : "";
  const pctTxt = pct == null ? "—" : (pct > 0 ? "+" : "") + fmt(pct);
  const m = (v) => v == null ? "—" : (v > 0 ? "+" : "") + fmt(v);
  const mc = (v) => v == null ? "" : v > 0 ? "red" : v < 0 ? "green" : "";
  return `<tr data-code="${s.code}">
    <td class="stock-id">${esc(s.name || "—")}(${s.code})</td>
    <td class="num">${fmt(s.close)}</td>
    <td class="num ${pctCls}">${pctTxt}</td>
    <td class="num ${mc(s.pct_1m)}">${m(s.pct_1m)}</td>
    <td class="num ${mc(s.pct_3m)}">${m(s.pct_3m)}</td>
    <td class="num">${fmt(s.dy)}</td>
    <td class="num">${fmt(s.dps, 3)}</td>
    <td class="num">${fmt(s.div_ratio)}</td>
    <td class="num">${fmt(s.pe)}</td>
    <td class="num">${fmt(s.pb)}</td>
    <td class="num">${fmt(s.mv, 1)}</td>
    <td class="num">${fmt(s.roe)}</td>
    <td class="num ${mc(s.ytd)}">${m(s.ytd)}</td>
    <td>${s.industry || "—"}</td></tr>`;
}

function renderHkList() {
  const tbody = $("#hk-table tbody");
  if (!tbody) return;
  const list = hkFiltered();
  tbody.innerHTML = list.map(hkRowHTML).join("");
  const info = [`显示 ${list.length} / ${state.hkStocks.length} 只`];
  if (state.hkSearch.trim()) info.push("搜索：" + state.hkSearch.trim());
  const el = $("#hk-info");
  if (el) el.textContent = info.join(" · ");
  tbody.querySelectorAll("tr").forEach((tr) =>
    tr.addEventListener("click", () => { location.hash = "#/hk/" + tr.dataset.code; })
  );
}

function renderHkRankings() {
  const box = $("#hk-rankings");
  if (!box) return;
  const all = state.hkStocks;
  const block = (title, rows, key, emptyText) => {
    if (!rows.length) {
      if (!emptyText) return "";
      return `<div class="rank-block"><div class="rank-title">${title}</div>` +
        `<ol class="rank-list"><li class="rank-empty">${emptyText}</li></ol></div>`;
    }
    const items = rows.map((s, i) => {
      const v = s[key];
      const cls = v > 0 ? "red" : v < 0 ? "green" : "";
      const txt = (v > 0 ? "+" : "") + fmt(v);
      return `<li><span class="rk">${i + 1}</span>` +
        `<span class="rc" data-code="${s.code}">${esc(s.name)}(${s.code})</span>` +
        `<span class="rv num ${cls}">${txt}</span></li>`;
    }).join("");
    return `<div class="rank-block"><div class="rank-title">${title}</div><ol class="rank-list">${items}</ol></div>`;
  };
  const highDy = all.filter((s) => s.dy != null).sort((a, b) => b.dy - a.dy).slice(0, 10);
  const down7 = all.filter((s) => s.down7 && s.pct_7d != null)
    .sort((a, b) => a.pct_7d - b.pct_7d).slice(0, 10);
  const yin7 = all.filter((s) => s.yin7 && s.pct_7d != null)
    .sort((a, b) => a.pct_7d - b.pct_7d).slice(0, 10);
  const yang7 = all.filter((s) => s.yang7 && s.pct_7d != null)
    .sort((a, b) => b.pct_7d - a.pct_7d).slice(0, 10);
  box.innerHTML = block("股息率TTM Top10", highDy, "dy") +
    block("连跌7天", down7, "pct_7d", "今日暂无满足条件的股票") +
    block("连续7天阴线", yin7, "pct_7d", "今日暂无满足条件的股票") +
    block("连续7天阳线", yang7, "pct_7d", "今日暂无满足条件的股票");
  box.querySelectorAll(".rc").forEach((el) =>
    el.addEventListener("click", () => { location.hash = "#/hk/" + el.dataset.code; })
  );
}

function renderHkCards(it) {
  const pct = it.pct == null ? null : Number(it.pct);
  const cls = pct > 0 ? "red" : pct < 0 ? "green" : "";
  const pctTxt = pct == null ? "—" : (pct > 0 ? "+" : "") + fmt(pct);
  const cards = [
    ["现价(HKD)", fmt(it.close), ""],
    ["涨跌幅%", pctTxt, cls],
    ["股息率TTM%", fmt(it.dy), ""],
    ["每股股息(HKD)", fmt(it.dps, 3), ""],
    ["派息比率%", fmt(it.div_ratio), ""],
    ["市盈率TTM", fmt(it.pe), ""],
    ["市净率", fmt(it.pb), ""],
    ["总市值(亿港元)", fmt(it.mv, 1), ""],
    ["ROE%", fmt(it.roe), ""],
    ["年初至今%", fmt(it.ytd), ""],
  ];
  const el = $("#hk-cards");
  if (el) {
    el.innerHTML = cards
      .map(([k, v, c]) => `<div class="card"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`)
      .join("");
  }
}

async function openHkDetail(code) {
  showView("hk-detail");
  state.hkCurrent = code;
  state.hkFinGran = "annual";
  state.hkFinKey = null;
  state.hkCompany = null;
  const it = state.hkStocks.find((x) => x.code === code) || {};
  const t = $("#hk-title");
  if (t) t.textContent = (it.name || code) + " (" + code + ")";
  const sub = $("#hk-sub");
  if (sub) {
    let ld = it.list_date ? String(it.list_date) : "";
    if (ld.length === 8) ld = ld.slice(0, 4) + "-" + ld.slice(4, 6) + "-" + ld.slice(6, 8);
    sub.textContent = "港股通 · " + (it.industry || "—") +
      (ld ? " · 上市日期 " + ld : "");
  }
  renderHkCards(it);
  const [k, rw, comp] = await Promise.all([
    fetch("data/hk/kline/" + code + ".json").then((r) => r.json()).catch(() => null),
    fetch("data/hk/kline_raw/" + code + ".json").then((r) => r.json()).catch(() => null),
    fetch("data/hk/company/" + code + ".json").then((r) => r.json()).catch(() => null),
  ]);
  state.hkK = k;
  state.hkRaw = rw;
  state.hkCompany = comp;
  state.hkDivs = (comp && comp.dividend && comp.dividend.records)
    ? comp.dividend.records.reduce((m, x) => { if (x.ex_date) m[x.ex_date] = x; return m; }, {})
    : null;
  try { hkRenderChart(); } catch (e) { console.error("hkRenderChart:", e); }
  renderHkDividend((comp && comp.dividend) || {});
  renderHkFinance((comp && comp.financial) || {});
}

function hkRenderChart() {
  const el = $("#hk-chart");
  if (!el) return;
  if (!state.hkK || !state.hkK.bars || !state.hkK.bars.length) {
    el.innerHTML = '<div class="muted" style="padding:20px">暂无K线数据</div>';
    return;
  }
  if (!state.hkChart) {
    state.hkChart = echarts.init(el);
    window.addEventListener("resize", () => state.hkChart.resize());
  }
  let bars = state.hkK.bars;
  const rawBars = state.hkRaw && state.hkRaw.bars && state.hkRaw.bars.length
    ? state.hkRaw.bars : null;
  if (state.hkMode === "raw" && rawBars) bars = rawBars;
  const fqNote = $("#hk-fq-note");
  if (fqNote) {
    fqNote.textContent = (state.hkMode === "raw" && !rawBars)
      ? "未复权数据未生成，暂显示前复权" : "";
  }
  if (state.hkPeriod !== "day") bars = aggregateBars(bars, state.hkPeriod);
  const zoomStart = Math.max(0, 100 - 120 / bars.length * 100);
  const dates = bars.map((b) => b.d);
  const ohlc = bars.map((b) => [b.o, b.c, b.l, b.h]);
  const vol = bars.map((b) => b.v);
  const showMA = $("#hk-ck-ma").checked;
  const showB = $("#hk-ck-boll").checked;
  const showD = $("#hk-ck-dmi").checked;
  const markData = (state.hkDivs && state.hkPeriod === "day")
    ? bars.map((b) => (state.hkDivs[b.d] ? { coord: [b.d, b.l], value: "S" } : null))
      .filter(Boolean)
    : [];
  const series = [{
    name: "K线", type: "candlestick", data: ohlc,
    itemStyle: {
      color: "#d32f2f", color0: "#2e7d32",
      borderColor: "#d32f2f", borderColor0: "#2e7d32",
    },
    markPoint: markData.length ? {
      symbol: "circle", symbolSize: 15,
      itemStyle: { color: "#b08500" },
      label: { formatter: "S", color: "#fff", fontSize: 10, position: "inside" },
      data: markData,
    } : undefined,
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
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross" },
      formatter: (params) => {
        const arr = Array.isArray(params) ? params : [params];
        const first = arr[0];
        const idx = first ? first.dataIndex : -1;
        const d = idx >= 0 && bars[idx] ? bars[idx].d : "";
        const lines = [];
        const div = state.hkDivs && state.hkDivs[d];
        if (div) {
          lines.push("<b>" + d + " 除净</b>");
          lines.push('<span style="color:#b08500">方案：' +
            esc(div.plan || "—") + "</span>");
        } else if (d) {
          lines.push("<b>" + d + "</b>");
        }
        for (const p of arr) {
          const nm = p.seriesName;
          if (nm === "K线" && Array.isArray(p.value)) {
            lines.push("开盘：" + p.value[0]);
            lines.push("收盘：" + p.value[1]);
            lines.push("最低：" + p.value[2]);
            lines.push("最高：" + p.value[3]);
          } else if (nm === "成交量") {
            lines.push(p.marker + nm + "：" +
              (p.value == null ? "—" : Number(p.value).toLocaleString()));
          } else if (nm && nm !== "K线") {
            lines.push(p.marker + nm + "：" +
              (p.value == null ? "—" : Number(p.value).toFixed(2)));
          }
        }
        return lines.join("<br>");
      },
    },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    grid: [
      { left: 52, right: 16, top: 26, height: "58%" },
      { left: 52, right: 16, top: "72%", height: "20%" },
    ],
    xAxis: [
      {
        type: "category", data: dates, boundaryGap: true,
        axisLabel: { fontSize: 10, showMaxLabel: true },
        axisLine: { lineStyle: { color: "#999" } },
      },
      {
        type: "category", gridIndex: 1, data: dates, boundaryGap: true,
        axisLabel: { show: false }, axisLine: { lineStyle: { color: "#999" } },
      },
    ],
    yAxis: [
      { scale: true, axisLabel: { fontSize: 10 } },
      { gridIndex: 1, scale: true, axisLabel: { fontSize: 10 } },
    ],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1], start: zoomStart, end: 100 },
      { type: "slider", xAxisIndex: [0, 1], bottom: 0, height: 18, start: zoomStart, end: 100 },
    ],
    series,
  };
  state.hkChart.setOption(option, true);
}

function renderHkDividend(dd) {
  const sum = $("#hk-div-sum");
  const recs = dd.records || [];
  if (sum) {
    sum.innerHTML =
      `<span class="pill">股息率(TTM)：<b>${fmt(dd.dy, 2)}%</b></span>` +
      `<span class="pill">近12个月每股派息：<b>${fmt(dd.ttm_dps, 3)} HKD</b></span>` +
      `<span class="pill">派息比率(TTM)：<b>${fmt(dd.ttm_div_ratio, 1)}%</b></span>` +
      `<span class="pill">分红记录：<b>${recs.length}</b> 笔</span>`;
  }
  const tb = $("#hk-div-table tbody");
  if (!tb) return;
  tb.innerHTML = recs.map((r) =>
    `<tr><td>${r.year || "—"}</td><td>${esc(r.type || "—")}</td>` +
    `<td class="num">${r.dps == null ? "—" : fmt(r.dps, 4)}</td>` +
    `<td>${r.ex_date || "—"}</td><td>${r.pay_date || "—"}</td>` +
    `<td class="plan-cell">${r.plan ? esc(r.plan) : "—"}</td></tr>`).join("");
}

function hkFinType(p) {
  const t = p.rtype || "";
  if (t.includes("年报")) return "annual";
  if (t.includes("中报")) return "half";
  if (t.includes("一季报")) return "q1";
  if (t.includes("三季报")) return "q3";
  return "annual";
}

function hkFinLabel(p) {
  const y = (p.date || "").slice(0, 4);
  const t = hkFinType(p);
  return { q1: y + "Q1", half: y + "H1", q3: y + "Q3", annual: y }[t] || y;
}

function renderHkFinance(fin) {
  const thead = $("#hk-fin-table thead");
  const tbody = $("#hk-fin-table tbody");
  if (!thead || !tbody) return;
  const gran = state.hkFinGran;
  const periods = (fin.periods || [])
    .filter((p) => hkFinType(p) === gran)
    .sort((a, b) => (a.date || "").localeCompare(b.date || ""))
    .slice(-10)
    .reverse();
  thead.innerHTML = "<tr><th>指标</th>" +
    periods.map((p) => `<th class="num">${hkFinLabel(p)}</th>`).join("") + "</tr>";
  tbody.innerHTML = HK_FIN_ROWS.map(([k, name]) => {
    const sel = state.hkFinKey === k ? ' class="sel"' : "";
    return `<tr data-key="${k}"${sel}><td>${name}</td>` +
      periods.map((p) => {
        const v = p[k];
        return `<td class="num">${v == null ? "—" : fmt(v, 2)}</td>`;
      }).join("") + "</tr>";
  }).join("");
  tbody.querySelectorAll("tr[data-key]").forEach((tr) =>
    tr.addEventListener("click", () => {
      state.hkFinKey = (state.hkFinKey === tr.dataset.key) ? null : tr.dataset.key;
      renderHkFinance(fin);
      renderHkFinChart(fin);
    })
  );
  renderHkFinChart(fin);
}

function renderHkFinChart(fin) {
  const el = $("#hk-fin-chart");
  const btn = $("#hk-fin-chart-toggle");
  if (!el) return;
  const fin2 = fin || {};
  const periods = (fin2.periods || []).slice()
    .sort((a, b) => (a.date || "").localeCompare(b.date || ""));
  const shown = !!state.hkFinKey && periods.length > 0;
  el.hidden = !shown;
  if (btn) btn.textContent = shown ? "收起指标图" : "展示指标图";
  if (!shown) return;
  const k = state.hkFinKey;
  const row = HK_FIN_ROWS.find((r) => r[0] === k) || [k, k, ""];
  if (!state.hkFinChart) {
    state.hkFinChart = echarts.init(el);
    window.addEventListener("resize", () => state.hkFinChart && state.hkFinChart.resize());
  }
  state.hkFinChart.setOption({
    animation: false,
    grid: { left: 10, right: 16, top: 28, bottom: 44, containLabel: true },
    tooltip: { trigger: "axis" },
    legend: { data: [row[1]], top: 0, textStyle: { fontSize: 11 } },
    xAxis: { type: "category", data: periods.map((p) => p.date || ""),
             axisLabel: { fontSize: 10, rotate: 30 } },
    yAxis: { type: "value", scale: true, axisLabel: { fontSize: 10 } },
    series: [{
      name: row[1], type: "line", smooth: true, connectNulls: false,
      data: periods.map((p) => (p[k] == null ? null : p[k])),
    }],
    dataZoom: [{ type: "inside" }],
  }, true);
}

function route() {
  const h = location.hash || "#/";
  if (h.startsWith("#/hk/")) {
    loadHk().then(() => openHkDetail(decodeURIComponent(h.slice(5))));
    return;
  }
  if (h.startsWith("#/hk")) {
    loadHk().then(() => {
      renderHkList();
      try { renderHkRankings(); } catch (e) { /* 榜单异常不影响列表 */ }
      showView("hk");
    });
    return;
  }
  if (h.startsWith("#/fund/")) {
    openFundDetail(decodeURIComponent(h.slice(7)));
    return;
  }
  if (h.startsWith("#/funds")) {
    renderFunds();
    showView("funds");
    return;
  }
  if (h.startsWith("#/cbonds")) {
    renderCbonds();
    showView("cbonds");
    return;
  }
  if (h.startsWith("#/stock/")) {
    openDetail(decodeURIComponent(h.slice(8)));
    return;
  }
  if (h.startsWith("#/tag/")) {
    renderTagView(decodeURIComponent(h.slice(6)));
    return;
  }
  if (h.startsWith("#/boards")) {
    renderBoards();
    showView("boards");
    return;
  }
  renderList();
  try { renderRankings(); } catch (e) { /* 单个榜单异常不影响页面 */ }
  try { renderInsiderRank(); } catch (e) { /* 单个榜单异常不影响页面 */ }
  showView("list");
}

function renderTagView(tag) {
  state.tagFilter = tag;
  const list = state.stocks.filter((s) => (s.tags || []).includes(tag) && stockOK(s));
  $("#tag-title").textContent = "标签：" + tag + "（" + list.length + " 只）";
  const tbody = $("#tag-table tbody");
  tbody.innerHTML = list.map(stockRowHTML).join("");
  $("#tag-info").textContent = "显示 " + list.length + " 只";
  tbody.querySelectorAll("tr").forEach((tr) =>
    tr.addEventListener("click", (e) => {
      if (e.target.closest(".tag")) return;
      location.hash = "#/stock/" + tr.dataset.code;
    })
  );
  showView("tag");
}

function renderRankings() {
  const box = $("#rankings");
  if (!box) return;
  const all = state.stocks;
  const banks = all.filter((s) => (s.industry || "").includes("银行") && s.dy != null)
    .sort((a, b) => b.dy - a.dy).slice(0, 10);
  const cons = all.filter((s) => (s.div_years || 0) >= 5);
  const up = cons.filter((s) => s.pct_1m != null).sort((a, b) => b.pct_1m - a.pct_1m).slice(0, 10);
  const down = cons.filter((s) => s.pct_1m != null).sort((a, b) => a.pct_1m - b.pct_1m).slice(0, 10);
  // 连跌7天 / 连续7天阴线 / 连续5天跌破布林下轨（按近7日涨跌幅升序，跌幅大的排前面）
  const down7 = all.filter((s) => s.down7 && s.pct_7d != null)
    .sort((a, b) => a.pct_7d - b.pct_7d).slice(0, 10);
  const yin7 = all.filter((s) => s.yin7 && s.pct_7d != null)
    .sort((a, b) => a.pct_7d - b.pct_7d).slice(0, 10);
  const yang7 = all.filter((s) => s.yang7 && s.pct_7d != null)
    .sort((a, b) => b.pct_7d - a.pct_7d).slice(0, 10);
  const boll5 = all.filter((s) => s.boll5 && s.pct_7d != null)
    .sort((a, b) => a.pct_7d - b.pct_7d).slice(0, 10);
  const block = (title, rows, key, emptyText) => {
    if (!rows.length) {
      if (!emptyText) return "";
      return `<div class="rank-block"><div class="rank-title">${title}</div>` +
        `<ol class="rank-list"><li class="rank-empty">${emptyText}</li></ol></div>`;
    }
    const items = rows.map((s, i) => {
      const v = s[key];
      const cls = v > 0 ? "red" : v < 0 ? "green" : "";
      const txt = (v > 0 ? "+" : "") + fmt(v);
      return `<li><span class="rk">${i + 1}</span>` +
        `<span class="rc" data-code="${s.code}">${esc(s.name)}(${s.code})</span>` +
        `<span class="rv num ${cls}">${txt}</span></li>`;
    }).join("");
    return `<div class="rank-block"><div class="rank-title">${title}</div><ol class="rank-list">${items}</ol></div>`;
  };
  box.innerHTML = block("银行股息率 Top10", banks, "dy") +
    block("连续分红≥5年 · 近1月涨幅 Top10", up, "pct_1m") +
    block("连续分红≥5年 · 近1月跌幅 Top10", down, "pct_1m") +
    block("连跌7天", down7, "pct_7d", "今日暂无满足条件的股票") +
    block("连续7天阴线", yin7, "pct_7d", "今日暂无满足条件的股票") +
    block("连续7天阳线", yang7, "pct_7d", "今日暂无满足条件的股票") +
    block("连续5天跌破布林下轨", boll5, "pct_7d", "今日暂无满足条件的股票");
  box.querySelectorAll(".rc").forEach((el) =>
    el.addEventListener("click", () => { location.hash = "#/stock/" + el.dataset.code; }));
}

// 近1年高管增持排名：最近增持的排在最前（全宽表格）
function renderInsiderRank() {
  const box = $("#insider-rank");
  if (!box) return;
  const tbody = box.querySelector("tbody");
  if (!tbody) { box.hidden = true; return; }
  const list = state.insiders || [];
  if (!list.length) { box.hidden = true; return; }
  box.hidden = false;
  const per = 15;
  const pages = Math.max(1, Math.ceil(list.length / per));
  if (state.insiderPage > pages) state.insiderPage = pages;
  if (state.insiderPage < 1) state.insiderPage = 1;
  const page = state.insiderPage;
  const shown = list.slice((page - 1) * per, page * per);
  tbody.innerHTML = shown.map((x) => {
    const sh = x.shares == null ? "—" : Math.round(x.shares).toLocaleString();
    const rt = x.ratio == null ? "—" : fmt(x.ratio, 2);
    return `<tr data-code="${x.code}">
      <td>${x.date || "—"}</td>
      <td>${esc(x.name || "—")}(${x.code})</td>
      <td>${esc(x.position || "—")}</td>
      <td class="num">${sh}</td>
      <td class="num">${rt}</td>
      <td>${esc(x.reason || "—")}</td></tr>`;
  }).join("");
  const info = $("#insider-info");
  if (info) {
    info.innerHTML =
      '<span>共 ' + list.length + " 只 · 第 " + page + " / " + pages + " 页</span>" +
      '<button class="mini-btn" id="insider-prev"' + (page <= 1 ? " disabled" : "") + ">上一页</button>" +
      '<button class="mini-btn" id="insider-next"' + (page >= pages ? " disabled" : "") + ">下一页</button>";
    const prevBtn = document.getElementById("insider-prev");
    const nextBtn = document.getElementById("insider-next");
    if (prevBtn) prevBtn.addEventListener("click", () => { state.insiderPage -= 1; renderInsiderRank(); });
    if (nextBtn) nextBtn.addEventListener("click", () => { state.insiderPage += 1; renderInsiderRank(); });
  }
  tbody.querySelectorAll("tr").forEach((tr) =>
    tr.addEventListener("click", () => { location.hash = "#/stock/" + tr.dataset.code; })
  );
}

// ---------------------------------------------------------------------------
// 红利 / 宽基基金列表 + 净值走势
// ---------------------------------------------------------------------------
function fundVal(f, k) {
  if (k && k.indexOf("scale_cells.") === 0) {
    const cell = f.scale_cells && f.scale_cells[k.slice(12)];
    return cell ? cell.value : null;
  }
  return f[k];
}

function fundFiltered() {
  let list = state.funds;
  const q = state.fundSearch.trim().toLowerCase();
  if (q) {
    list = list.filter((f) =>
      f.code.includes(q) ||
      (f.name || "").toLowerCase().includes(q) ||
      initials(f.name || "").includes(q) ||
      (f.index_name || "").toLowerCase().includes(q) ||
      (f.type || "").toLowerCase().includes(q)
    );
  }
  const { k, asc } = state.fundSort;
  list = list.slice().sort((a, b) => {
    const va = fundVal(a, k), vb = fundVal(b, k);
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

function fundScaleCell(cell) {
  if (!cell) return "—";
  const d = cell.delta, p = cell.pct;
  const cs = d > 0 ? "red" : d < 0 ? "green" : "";
  const sign = d > 0 ? "+" : "";
  return fmt(cell.value, 2) + ' <span class="' + cs + '">(' + sign + fmt(d, 2) +
    ", " + sign + fmt(p, 2) + "%)</span>";
}

function fundRowHTML(f) {
  const m = (v) => v == null ? "—" : (v > 0 ? "+" : "") + fmt(v);
  const mc = (v) => v == null ? "" : v > 0 ? "red" : v < 0 ? "green" : "";
  return `<tr data-code="${f.code}">
    <td>${esc(f.name || "—")}(${f.code})</td>
    <td>${f.nav_date || "—"}</td>
    <td class="num ${mc(f.chg_today)}">${m(f.chg_today)}</td>
    <td>${f.type || "—"}</td>
    <td>${esc(f.index_name || "—")}</td>
    <td class="num ${mc(f.pct_1m)}">${m(f.pct_1m)}</td>
    <td class="num ${mc(f.pct_3m)}">${m(f.pct_3m)}</td>
    <td class="num ${mc(f.pct_6m)}">${m(f.pct_6m)}</td>
    <td class="num ${mc(f.pct_12m)}">${m(f.pct_12m)}</td>
    <td class="num">${f.div_count == null ? "—" : f.div_count}</td>
    <td class="num">${fmt(f.div_ratio)}</td>
    <td class="num">${fmt(f.pe)}</td>
    <td class="num">${fmt(f.pb)}</td>
    <td class="num">${fmt(f.scale_now, 2)}</td>
    <td class="num">${fundScaleCell(f.scale_cells && f.scale_cells["1"])}</td>
    <td class="num">${fundScaleCell(f.scale_cells && f.scale_cells["3"])}</td>
    <td class="num">${fundScaleCell(f.scale_cells && f.scale_cells["6"])}</td>
    <td class="num">${fundScaleCell(f.scale_cells && f.scale_cells["12"])}</td>
  </tr>`;
}

// ===== 可转债 =====
function cbRowHTML(b) {
  const price = b.price == null ? "—" : Number(b.price).toFixed(3);
  const prCls = b.premium_rt == null ? "" : b.premium_rt > 0 ? "red" : b.premium_rt < 0 ? "green" : "";
  const pr = b.premium_rt == null ? "—" : (b.premium_rt > 0 ? "+" : "") + Number(b.premium_rt).toFixed(2) + "%";
  const ration = b.ration == null ? "—" : Number(b.ration).toFixed(4);
  const stock = b.stock_name
    ? `${esc(b.stock_name)}(${esc(b.stock_code)})`
    : "—";
  // 配售1手/2手：需股数(所需资金 = 正股价 × 股数)
  const need = (n, amt) =>
    n == null ? "—" : `${n}股` + (amt != null ? `(${amt.toLocaleString()}元)` : "");
  // 正股涨跌幅（近5/10工作日累计、过会至今）
  const pct = (v) => v == null ? "—" : (v > 0 ? "+" : "") + Number(v).toFixed(2) + "%";
  const pctCls = (v) => v == null ? "" : v > 0 ? "red" : v < 0 ? "green" : "";
  const rd = b.review_date || "—";
  return `<tr data-code="${b.code}">
    <td>${esc(b.name || "—")}(${b.code})</td>
    <td class="num">${price}</td>
    <td class="num ${prCls}">${pr}</td>
    <td>${b.apply_date || "—"}</td>
    <td>${b.list_date || "—"}</td>
    <td>${b.record_date || "—"}</td>
    <td class="num">${ration}</td>
    <td class="num">${need(b.need1, b.amt1)}</td>
    <td class="num">${need(b.need2, b.amt2)}</td>
    <td class="num ${pctCls(b.pct5)}">${pct(b.pct5)}</td>
    <td class="num ${pctCls(b.pct10)}">${pct(b.pct10)}</td>
    <td class="num ${pctCls(b.pct_review)}" title="过会日 ${rd}">${pct(b.pct_review)}</td>
    <td>${stock}</td>
  </tr>`;
}

function renderCbonds() {
  const c = state.cbonds || { listed: [], pending: [] };
  const p = document.querySelector("#cb-table-pending tbody");
  const l = document.querySelector("#cb-table-listed tbody");
  if (p) p.innerHTML = c.pending.map(cbRowHTML).join("");
  if (l) l.innerHTML = c.listed.map(cbRowHTML).join("");
  const pi = document.getElementById("cb-pending-info");
  const li = document.getElementById("cb-listed-info");
  if (pi) pi.textContent = "已过会 · 未上市（含待申购 / 待上市）共 " + c.pending.length + " 只，按申购日期倒序";
  if (li) li.textContent = "已上市 共 " + c.listed.length + " 只，按上市日期倒序";
}

function renderFunds() {
  const tbody = $("#fund-table tbody");
  if (!tbody) return;
  const list = fundFiltered();
  tbody.innerHTML = list.map(fundRowHTML).join("");
  const info = [];
  if (state.fundSearch.trim()) info.push("搜索：" + state.fundSearch.trim());
  info.push("显示 " + list.length + " / " + state.funds.length + " 只");
  $("#fund-info").textContent = info.join(" · ");
  tbody.querySelectorAll("tr").forEach((tr) =>
    tr.addEventListener("click", () => { location.hash = "#/fund/" + tr.dataset.code; })
  );
}

async function openFundDetail(code) {
  showView("fund-detail");
  const it = state.funds.find((x) => x.code === code) || {};
  state.fundCurrent = it;
  $("#fd-title").textContent = (it.name || code) + " (" + code + ")";
  $("#fd-sub").textContent =
    "类型：" + (it.type || "—") +
    " · 跟踪指数：" + (it.index_name || "—") +
    " · 近12月分红 " + (it.div_count == null ? "—" : it.div_count) + " 次" +
    " · 当前规模 " + (it.scale_now == null ? "—" : fmt(it.scale_now, 2) + " 亿");
  try {
    const d = await fetch("data/fund_nav/" + code + ".json").then((r) => r.json());
    renderFundNav(d);
  } catch (e) {
    const el = $("#fd-chart");
    if (el) el.innerHTML = '<div class="muted" style="padding:20px">暂无净值走势数据</div>';
  }
}

function renderFundNav(d) {
  const el = $("#fd-chart");
  if (!el) return;
  if (!d || !d.nav || !d.nav.length) {
    el.innerHTML = '<div class="muted" style="padding:20px">暂无净值走势数据</div>';
    return;
  }
  if (!state.fundChart) {
    state.fundChart = echarts.init(el);
    window.addEventListener("resize", () => state.fundChart && state.fundChart.resize());
  }
  const dates = d.nav.map((x) => x[0]);
  const vals = d.nav.map((x) => x[1]);
  state.fundChart.setOption({
    animation: false,
    grid: { left: 10, right: 14, top: 24, bottom: 44, containLabel: true },
    tooltip: { trigger: "axis" },
    xAxis: { type: "category", data: dates, axisLabel: { fontSize: 10, showMaxLabel: true } },
    yAxis: { type: "value", scale: true, axisLabel: { fontSize: 10 } },
    dataZoom: [
      { type: "inside", start: 35, end: 100 },
      { type: "slider", start: 35, end: 100, height: 16, bottom: 8 },
    ],
    series: [{
      type: "line", data: vals, showSymbol: false, smooth: true,
      lineStyle: { width: 1.5, color: "#1e5eff" },
      areaStyle: { opacity: 0.06 },
    }],
  }, true);
}

// 表头吸顶：页面下滚时把表头行钉在「顶部 header 框下方」（header 框本身 sticky 常驻最上方）
function pinTableHeaders() {
  const off = (document.querySelector("header") || { offsetHeight: 0 }).offsetHeight || 0;
  document.querySelectorAll("#stock-table thead, #tag-table thead, #fund-table thead, #cb-table-pending thead, #cb-table-listed thead").forEach((thead) => {
    const wrap = thead.closest(".table-wrap");
    const r = wrap.getBoundingClientRect();
    const th = thead.getBoundingClientRect();
    if (r.top < off && r.bottom > off + th.height) {
      thead.style.position = "relative";
      thead.style.transform = "translateY(" + (off - r.top) + "px)";
      thead.style.zIndex = "20"; // 盖过站点头部条(z10)
      thead.style.background = "#fafbfc";
      thead.style.boxShadow = "0 2px 6px rgba(0,0,0,.12)";
    } else {
      thead.style.transform = "";
      thead.style.zIndex = "";
      thead.style.background = "";
      thead.style.boxShadow = "";
    }
  });
}
window.addEventListener("scroll", () => requestAnimationFrame(pinTableHeaders), { passive: true });
window.addEventListener("resize", pinTableHeaders);

function setupEvents() {
  // 表格容器滚轮处理：鼠标在列表上「上下滚动」→ 列表左右平移看指标；
  // 左右手势/横滑 → 表格上下翻行；无横向溢出的小表保持页面滚动。
  document.querySelectorAll(".table-wrap").forEach((wrap) => {
    wrap.addEventListener("wheel", (e) => {
      e.preventDefault();
      const horiz = wrap.scrollWidth > wrap.clientWidth;
      const vert = wrap.scrollHeight > wrap.clientHeight;
      if (Math.abs(e.deltaY) >= Math.abs(e.deltaX)) {
        if (horiz) wrap.scrollLeft += e.deltaY;      // 上下滚轮 → 左右平移
        else if (vert) wrap.scrollTop += e.deltaY;   // 仅纵向溢出 → 正常翻行
        else window.scrollBy(0, e.deltaY);           // 无溢出 → 交给页面
      } else {
        if (vert) wrap.scrollTop += e.deltaX;        // 左右手势 → 上下翻行
        else if (horiz) wrap.scrollLeft += e.deltaX;
      }
    }, { passive: false });
  });
  pinTableHeaders();
  $("#search").addEventListener("input", (e) => {
    const q = e.target.value;
    if (state.view === "funds" || state.view === "fund-detail") {
      state.fundSearch = q;
      renderFunds();
    } else if (state.view === "hk" || state.view === "hk-detail") {
      state.hkSearch = q;
      renderHkList();
    } else {
      state.search = q;
      renderList();
    }
  });
  document.querySelectorAll("#stock-table th[data-k]").forEach((th) =>
    th.addEventListener("click", () => {
      const k = th.dataset.k;
      if (state.sort.k === k) state.sort.asc = !state.sort.asc;
      else state.sort = { k, asc: k === "code" || k === "name" || k === "industry" };
      renderList();
    })
  );
  $("#tab-list").addEventListener("click", () => { location.hash = "#/"; });
  $("#tab-boards").addEventListener("click", () => { location.hash = "#/boards"; });
  $("#tab-funds").addEventListener("click", () => { location.hash = "#/funds"; });
  $("#tab-cbonds").addEventListener("click", () => { location.hash = "#/cbonds"; });
  $("#tab-hk").addEventListener("click", () => { location.hash = "#/hk"; });
  $("#back").addEventListener("click", () => { location.hash = "#/"; });
  $("#back-fund").addEventListener("click", () => { location.hash = "#/funds"; });
  $("#hk-back").addEventListener("click", () => { location.hash = "#/hk"; });
  $("#tag-back").addEventListener("click", () => { location.hash = "#/"; });
  document.querySelectorAll("#hk-table th[data-k]").forEach((th) =>
    th.addEventListener("click", () => {
      const k = th.dataset.k;
      if (state.hkSort.k === k) state.hkSort.asc = !state.hkSort.asc;
      else state.hkSort = { k, asc: k === "code" || k === "name" || k === "industry" };
      renderHkList();
    })
  );
  document.querySelectorAll("#fund-table th[data-k]").forEach((th) =>
    th.addEventListener("click", () => {
      const k = th.dataset.k;
      if (state.fundSort.k === k) state.fundSort.asc = !state.fundSort.asc;
      else state.fundSort = { k, asc: k === "code" || k === "name" || k === "type" || k === "index_name" };
      renderFunds();
    })
  );
  ["ck-ma", "ck-boll", "ck-dmi"].forEach((id) =>
    document.getElementById(id).addEventListener("change", renderChart)
  );
  ["hk-ck-ma", "hk-ck-boll", "hk-ck-dmi"].forEach((id) =>
    document.getElementById(id).addEventListener("change", hkRenderChart)
  );
  document.querySelectorAll("#k-period button").forEach((b) =>
    b.addEventListener("click", () => {
      document.querySelectorAll("#k-period button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.kPeriod = b.dataset.p;
      renderChart();
    })
  );
  document.querySelectorAll("#k-fq button").forEach((b) =>
    b.addEventListener("click", () => {
      document.querySelectorAll("#k-fq button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.kMode = b.dataset.m;
      renderChart();
    })
  );
  document.querySelectorAll("#hk-period button").forEach((b) =>
    b.addEventListener("click", () => {
      document.querySelectorAll("#hk-period button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.hkPeriod = b.dataset.p;
      hkRenderChart();
    })
  );
  document.querySelectorAll("#hk-fq button").forEach((b) =>
    b.addEventListener("click", () => {
      document.querySelectorAll("#hk-fq button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.hkMode = b.dataset.m;
      hkRenderChart();
    })
  );
  document.querySelectorAll("#fin-gran button").forEach((b) =>
    b.addEventListener("click", () => {
      document.querySelectorAll("#fin-gran button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.finGran = b.dataset.g;
      if (state.company) renderFinance(state.company.finance || {});
    })
  );
  document.querySelectorAll("#hk-fin-gran button").forEach((b) =>
    b.addEventListener("click", () => {
      document.querySelectorAll("#hk-fin-gran button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.hkFinGran = b.dataset.g;
      if (state.hkCompany) renderHkFinance(state.hkCompany.financial || {});
    })
  );
  $("#hk-fin-chart-toggle").addEventListener("click", () => {
    if (!state.hkFinKey) state.hkFinKey = "roe";
    else state.hkFinKey = null;
    renderHkFinance((state.hkCompany && state.hkCompany.financial) || {});
  });
  // 标签点击 -> 站内标签页（事件委托，兼容列表/标签页）
  document.addEventListener("click", (e) => {
    const t = e.target.closest && e.target.closest(".tag");
    if (t && t.dataset.tag) location.hash = "#/tag/" + encodeURIComponent(t.dataset.tag);
  });
  window.addEventListener("hashchange", route);
}

setupEvents();
load();
