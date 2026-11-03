/* 커밋 단계 비교 GUI. 데이터는 data/*.js 가 window.CMP 에 넣는다. */
(() => {
  const CMP = window.CMP || { runs: {} };
  const M = CMP.meta;
  const STAGES = ["S1", "S2", "S3", "S4"];
  const EOK = 1e8; // 원 -> 억원
  // 배출계수 (tCO2/MWh) — config/generators.yaml co2_ton_per_mwh. KPX 실측 배출 계산용. 유류는 모델에 계수가 없어 0.
  const CO2 = { nuclear: 0.01, bituminous_coal: 0.991, anthracite: 0.991, lng: 0.549, oil: 0, hydro: 0 };

  const METRICS = {
    red:  { label: "절감률", unit: "%", total: "19일 합산 절감률 (Rule 대비 MILP)", daily: "날짜별 절감률 (Rule 대비 MILP)", fmt: v => v.toFixed(2) + "%" },
    milp: { label: "MILP 비용", unit: "억원", total: "19일 MILP 비용 합계", daily: "날짜별 MILP 비용", fmt: v => fmt1(v) },
    rule: { label: "Rule 비용", unit: "억원", total: "19일 Rule 비용 합계", daily: "날짜별 Rule 비용", fmt: v => fmt1(v) },
    time: { label: "풀이 시간", unit: "s", total: "19일 중 최대 풀이 시간 (KPI 10초)", daily: "날짜별 MILP 풀이 시간 (KPI 10초)", fmt: v => v.toFixed(1) + "s" },
    co2:  { label: "MILP 배출", unit: "천 tCO2", total: "19일 MILP 배출 합계", daily: "날짜별 MILP 배출", fmt: v => fmt1(v) },
  };

  const state = { date: M.days[0].date, stage: "S4", ess: "off", metric: "red" };
  const $ = id => document.getElementById(id);
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const fmt1 = v => v.toLocaleString("ko-KR", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const fmt0 = v => v.toLocaleString("ko-KR", { maximumFractionDigits: 0 });
  const stageColor = s => css("--s" + s.slice(1));
  const run = (s, ess) => CMP.runs[`${s}_${ess}`];
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const sum = a => a.reduce((x, y) => x + y, 0);
  const dayIdx = () => M.days.findIndex(d => d.date === state.date);
  const HOURS = [...Array(24).keys()].map(h => `${String(h).padStart(2, "0")}시`);

  // ── Plotly 공통 ──────────────────────────────────────────────────────────
  const baseLayout = (extra = {}) => ({
    paper_bgcolor: css("--surface-1"), plot_bgcolor: css("--surface-1"),
    font: { family: getComputedStyle(document.body).fontFamily, size: 12, color: css("--text-secondary") },
    margin: { l: 64, r: 16, t: 12, b: 48 },
    showlegend: false,
    hoverlabel: { bgcolor: css("--surface-1"), bordercolor: css("--border"), font: { color: css("--text-primary") } },
    xaxis: { gridcolor: css("--grid"), linecolor: css("--border"), zeroline: false, tickfont: { color: css("--text-secondary") } },
    yaxis: { gridcolor: css("--grid"), linecolor: css("--border"), zeroline: false, tickfont: { color: css("--text-secondary") } },
    ...extra,
  });
  const CFG = { displayModeBar: false, responsive: true };
  const tenLine = () => ({
    type: "line", xref: "paper", x0: 0, x1: 1, y0: 10, y1: 10,
    line: { color: css("--critical"), width: 1, dash: "dot" },
  });
  const legendHtml = items => items.map(([c, t, cls]) =>
    `<span><i class="${cls || ""}" style="${cls ? "" : "background:" + c}"></i>${esc(t)}</span>`).join("");

  // ── 메인: 날짜별 발전원 출력 4패널 ─────────────────────────────────────────
  function panelData(i) {
    const s = state.stage;
    const off = run(s, "off"), on = run(s, "on");
    const dOff = off && off.days[i], dOn = on && on.days[i];
    const md = M.days[i];
    return {
      md,
      kpx: { by_fuel: md.kpx },
      rule: dOff ? { by_fuel: dOff.rule.by_fuel, day: dOff } : null,
      off: dOff && dOff.milp.by_fuel ? { by_fuel: dOff.milp.by_fuel, day: dOff } : null,
      on: dOn && dOn.milp.by_fuel ? { by_fuel: dOn.milp.by_fuel, day: dOn, ess: dOn.milp.ess } : null,
    };
  }

  const stackTotal = (by_fuel, t) => M.fuels.reduce((a, f) => a + ((by_fuel[f] || [])[t] || 0), 0);

  function renderMain() {
    const i = dayIdx();
    const P = panelData(i);
    const st = M.stages[state.stage];
    $("stageDesc").textContent = `${state.stage} = ${st.label} · ${st.desc}`;
    $("mixTitle").textContent = `${state.date} 발전원별 시간 출력 (MW) — ${state.stage} (${st.label})`;
    $("fuelLegend").innerHTML = legendHtml([
      ...M.fuels.map(f => [M.color[f], M.label[f]]),
      [css("--ess"), "ESS 방전 / 충전 (ESS 켬)"],
      [null, "순수요", "line"],
    ]);

    // 공통 y 범위
    let ymax = Math.max(...P.md.net_mw), ymin = 0;
    for (const k of ["kpx", "rule", "off", "on"]) {
      const p = P[k]; if (!p) continue;
      for (let t = 0; t < 24; t++) {
        const dis = p.ess ? p.ess.discharge_mw[t] : 0;
        ymax = Math.max(ymax, stackTotal(p.by_fuel, t) + dis);
        if (p.ess) ymin = Math.min(ymin, -p.ess.charge_mw[t]);
      }
    }
    const yr = [ymin * 1.1, ymax * 1.05];

    for (const k of ["kpx", "rule", "off", "on"]) {
      const p = P[k];
      const traces = p ? M.fuels.map(f => ({
        type: "scatter", mode: "lines", stackgroup: "pos", name: M.label[f], x: HOURS, y: p.by_fuel[f] || Array(24).fill(0),
        line: { width: 0.5, color: css("--surface-1") }, fillcolor: M.color[f],
        hovertemplate: `${M.label[f]} %{y:,.0f} MW<extra></extra>`,
      })) : [];
      if (p && p.ess) {
        traces.push({ type: "scatter", mode: "lines", stackgroup: "pos", name: "ESS 방전", x: HOURS, y: p.ess.discharge_mw,
          line: { width: 0.5, color: css("--surface-1") }, fillcolor: css("--ess"), hovertemplate: "ESS 방전 %{y:,.0f} MW<extra></extra>" });
        traces.push({ type: "scatter", mode: "lines", stackgroup: "neg", name: "ESS 충전", x: HOURS, y: p.ess.charge_mw.map(v => -v),
          line: { width: 0.5, color: css("--surface-1") }, fillcolor: css("--ess"), hovertemplate: "ESS 충전 %{y:,.0f} MW<extra></extra>" });
      }
      traces.push({ type: "scatter", mode: "lines", name: "순수요", x: HOURS, y: P.md.net_mw,
        line: { color: css("--text-primary"), width: 2, dash: "dot" }, hovertemplate: "순수요 %{y:,.0f} MW<extra></extra>" });
      Plotly.react("mix_" + k, traces, baseLayout({
        hovermode: "x unified", margin: { l: 52, r: 8, t: 8, b: 36 },
        yaxis: { ...baseLayout().yaxis, range: yr, tickformat: ",.0f", zeroline: true, zerolinecolor: css("--border") },
        xaxis: { ...baseLayout().xaxis, dtick: 3 },
      }), CFG);
    }

    // 패널 상단 KPI
    const kpxCo2 = sum(Object.entries(P.md.kpx).map(([f, v]) => sum(v) * (CO2[f] || 0)));
    $("kpi_kpx").innerHTML = `비용 <b>${fmt1(P.md.kpx_won / EOK)}</b> 억원 · 배출 <b>${fmt1(kpxCo2 / 1e3)}</b> 천 t<br>연료비만 (기동 · 무부하 이력 없음)`;
    const rd = P.rule && P.rule.day;
    $("kpi_rule").innerHTML = rd
      ? `비용 <b>${fmt1(rd.rule.total_won / EOK)}</b> 억원 · 배출 <b>${fmt1(rd.rule.carbon_t / 1e3)}</b> 천 t<br>KPX 대비 ${((rd.rule.total_won - P.md.kpx_won) / P.md.kpx_won * 100).toFixed(2)}%`
      : `<span class="missing">데이터 없음</span>`;
    for (const k of ["off", "on"]) {
      const p = P[k], d = p && p.day;
      if (!d) { $("kpi_" + k).innerHTML = `<span class="missing">${run(state.stage, k) ? esc(run(state.stage, k).days[i].status) : "데이터 없음"}</span>`; continue; }
      const red = (d.rule.total_won - d.milp.cost.total_won) / d.rule.total_won * 100;
      $("kpi_" + k).innerHTML = `비용 <b>${fmt1(d.milp.cost.total_won / EOK)}</b> 억원 · 배출 <b>${fmt1(d.milp.carbon_t / 1e3)}</b> 천 t<br>`
        + `Rule 대비 <b>${red.toFixed(2)}%</b> 절감 · 풀이 <span class="${d.size.solve_time_s >= 10 ? "bad" : ""}">${d.size.solve_time_s.toFixed(1)}s</span>`
        + (d.milp.failed.length ? ` · <span class="bad">제약 위반 ${d.milp.failed.length}</span>` : "");
    }
    renderFuelTbl(P);
  }

  function renderFuelTbl(P) {
    $("fuelTblTitle").textContent = `${state.date} 발전원별 일간 발전량 (MWh) — ${state.stage} (${M.stages[state.stage].label})`;
    const cols = [["KPX 실측", P.kpx], ["Rule", P.rule], ["MILP · ESS 끔", P.off], ["MILP · ESS 켬", P.on]];
    const cell = (p, fn) => p ? fn(p) : null;
    const row = (label, vals, cls = "", f = fmt0) =>
      `<tr class="${cls}"><td>${label}</td>${vals.map(v => `<td>${v == null ? "—" : f(v)}</td>`).join("")}</tr>`;
    let h = `<tr><th class="l">발전원</th>${cols.map(([n]) => `<th>${n}</th>`).join("")}</tr>`;
    for (const f of M.fuels) {
      h += row(`<span class="dot" style="background:${M.color[f]}"></span>${esc(M.label[f])}`,
        cols.map(([, p]) => cell(p, q => sum(q.by_fuel[f] || []))));
    }
    h += row(`<span class="dot" style="background:${css("--ess")}"></span>ESS 방전`, cols.map(([, p]) => p && p.ess ? p.ess.energy_discharged_mwh : null));
    h += row(`<span class="dot" style="background:${css("--ess")}"></span>ESS 충전`, cols.map(([, p]) => p && p.ess ? p.ess.energy_charged_mwh : null));
    h += row("발전기 합계", cols.map(([, p]) => cell(p, q => sum(M.fuels.map(f => sum(q.by_fuel[f] || []))))), "sum");
    h += row("순수요 합계", cols.map(() => sum(P.md.net_mw)));
    h += row("비용 (억원)", [P.md.kpx_won / EOK, P.rule && P.rule.day.rule.total_won / EOK,
      P.off && P.off.day.milp.cost.total_won / EOK, P.on && P.on.day.milp.cost.total_won / EOK], "sum", fmt1);
    const kpxCo2 = sum(Object.entries(P.md.kpx).map(([f, v]) => sum(v) * (CO2[f] || 0)));
    h += row("배출 (천 tCO2)", [kpxCo2 / 1e3, P.rule && P.rule.day.rule.carbon_t / 1e3,
      P.off && P.off.day.milp.carbon_t / 1e3, P.on && P.on.day.milp.carbon_t / 1e3], "", fmt1);
    $("fuelTbl").innerHTML = h;
  }

  // ── 접힌 섹션: 단계 요약 · 지표 비교 ────────────────────────────────────────
  const dayVal = (d, metric) => {
    if (!d || d.status !== "optimal") return null;
    const r = d.rule.total_won, m = d.milp.cost.total_won;
    switch (metric) {
      case "red": return (r - m) / r * 100;
      case "milp": return m / EOK;
      case "rule": return r / EOK;
      case "time": return d.size.solve_time_s;
      case "co2": return d.milp.carbon_t / 1e3;
    }
  };

  const summarize = R => {
    if (!R) return null;
    const D = R.days, opt = D.filter(d => d.status === "optimal");
    const rule = sum(opt.map(d => d.rule.total_won)), milp = sum(opt.map(d => d.milp.cost.total_won));
    const times = D.map(d => d.size ? d.size.solve_time_s : NaN);
    const reds = opt.map(d => dayVal(d, "red"));
    return {
      n: D.length, nOpt: opt.length, rule, milp, red: (rule - milp) / rule * 100,
      redMin: Math.min(...reds), redMax: Math.max(...reds),
      co2: sum(opt.map(d => d.milp.carbon_t)), co2Rule: sum(opt.map(d => d.rule.carbon_t)),
      tAvg: sum(times) / times.length, tMax: Math.max(...times), over10: times.filter(t => t >= 10).length,
      failMilp: sum(D.map(d => d.milp.failed.length)), failRule: sum(D.map(d => d.rule.failed.length)),
    };
  };
  const totalVal = (S, metric) => S && ({ red: S.red, milp: S.milp / EOK, rule: S.rule / EOK, time: S.tMax, co2: S.co2 / 1e3 }[metric]);
  const stageItems = () => STAGES.map(s => [stageColor(s), `${s} (${M.stages[s].label})`]);

  function renderSummary() {
    const kpxWon = sum(M.days.map(d => d.kpx_won));
    const kpxCo2 = sum(M.days.map(d => sum(Object.entries(d.kpx).map(([f, v]) => sum(v) * (CO2[f] || 0)))));
    let h = `<tr><th class="l">단계</th><th class="l">조합</th><th class="l">ESS</th><th>optimal</th>
      <th>Rule 합계 (억원)</th><th>MILP 합계 (억원)</th><th>절감률</th><th>일별 절감률 범위</th>
      <th>Rule 배출 (천 t)</th><th>MILP 배출 (천 t)</th><th>풀이 평균</th><th>풀이 최대</th><th>10초 이상</th><th>제약 위반 (MILP / Rule)</th></tr>`;
    h += `<tr class="kpx"><td>KPX 실측</td><td class="l">—</td><td class="l">—</td><td>—</td><td>${fmt1(kpxWon / EOK)}</td><td>—</td><td>—</td><td>—</td><td>${fmt1(kpxCo2 / 1e3)}</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td></tr>`;
    for (const s of STAGES) for (const ess of ["off", "on"]) {
      const S = summarize(run(s, ess));
      const head = `<td><span class="dot" style="background:${stageColor(s)}"></span><b>${s}</b></td><td class="l">${M.stages[s].label}</td><td class="l">${ess === "on" ? "켬" : "끔"}</td>`;
      if (!S) { h += `<tr>${head}<td colspan="11" class="missing">데이터 없음</td></tr>`; continue; }
      const sel = ess === state.ess ? ' style="background:var(--accent-bg)"' : "";
      h += `<tr${sel}>${head}
        <td class="${S.nOpt < S.n ? "bad" : ""}">${S.nOpt}/${S.n}</td>
        <td>${fmt1(S.rule / EOK)}</td><td>${fmt1(S.milp / EOK)}</td><td><b>${S.red.toFixed(2)}%</b></td>
        <td>${S.redMin.toFixed(2)} ~ ${S.redMax.toFixed(2)}%</td>
        <td>${fmt1(S.co2Rule / 1e3)}</td><td>${fmt1(S.co2 / 1e3)}</td>
        <td>${S.tAvg.toFixed(1)}s</td><td class="${S.tMax >= 10 ? "bad" : ""}">${S.tMax.toFixed(1)}s</td>
        <td class="${S.over10 ? "bad" : ""}">${S.over10}일</td>
        <td class="${S.failMilp + S.failRule ? "bad" : ""}">${S.failMilp} / ${S.failRule}</td></tr>`;
    }
    $("summaryTbl").innerHTML = h;
    $("summaryNote").innerHTML = [
      "절감률 = (Rule − MILP) / Rule. 19일 합산은 비용 합계로 계산한다.",
      "ESS 켬은 'MILP + ESS' 와 'ESS 없는 Rule' 비교다 (Rule-based 에는 ESS 가 없다).",
      "무부하 단계(S2 · S4)는 비용 정의 자체가 달라 Rule · MILP 모두 무부하비가 들어간다. 단계 간 비용 절대값보다 같은 단계 안의 절감률을 비교할 것.",
      "ESS 끔이면 ESS 용량(3번 커밋)이 결과에 영향을 주지 않으므로 S1 = S3, S2 = S4 가 기대값이다.",
    ].map(t => `<li>${esc(t)}</li>`).join("");
  }

  function renderTotal() {
    const m = METRICS[state.metric];
    $("totalTitle").textContent = `${m.total} — ESS ${state.ess === "on" ? "켬" : "끔"}`;
    $("stageLegend1").innerHTML = legendHtml(stageItems());
    const ys = STAGES.map(s => totalVal(summarize(run(s, state.ess)), state.metric));
    const trace = {
      type: "bar", x: STAGES.map(s => `${s} (${M.stages[s].label})`), y: ys,
      marker: { color: STAGES.map(stageColor), cornerradius: 4 },
      text: ys.map(v => v == null ? "" : m.fmt(v)), textposition: "outside", cliponaxis: false,
      textfont: { color: css("--text-primary") },
      hovertemplate: "%{x}<br>" + m.label + " %{text}<extra></extra>", width: 0.5,
    };
    const layout = baseLayout({ yaxis: { ...baseLayout().yaxis, title: { text: m.unit }, rangemode: "tozero" } });
    if (state.metric === "time") layout.shapes = [tenLine()];
    Plotly.react("totalChart", [trace], layout, CFG);
  }

  function renderDaily() {
    const m = METRICS[state.metric];
    $("dailyTitle").textContent = `${m.daily} — ESS ${state.ess === "on" ? "켬" : "끔"}`;
    $("stageLegend2").innerHTML = legendHtml(stageItems());
    const dates = M.days.map(d => d.date);
    const traces = STAGES.map(s => {
      const R = run(s, state.ess);
      return {
        type: "scatter", mode: "lines+markers", name: `${s} (${M.stages[s].label})`,
        x: dates, y: dates.map((_, i) => R ? dayVal(R.days[i], state.metric) : null),
        line: { color: stageColor(s), width: 2 }, marker: { size: 8, color: stageColor(s), line: { color: css("--surface-1"), width: 2 } },
        hovertemplate: `${s} %{y:.2f} ${m.unit}<extra></extra>`,
      };
    });
    const layout = baseLayout({ hovermode: "x unified", yaxis: { ...baseLayout().yaxis, title: { text: m.unit } },
      xaxis: { ...baseLayout().xaxis, type: "category", tickangle: -45 } });
    if (state.metric === "time") layout.shapes = [tenLine()];
    Plotly.react("dailyChart", traces, layout, CFG);
    $("dailyNote").textContent = state.ess === "off"
      ? "ESS 끔에서는 ESS 용량(3번 커밋)이 결과에 영향을 주지 않아 S1·S3, S2·S4 선이 겹친다 (뒤 단계 선이 위에 그려짐). 값은 아래 표에서 확인."
      : "";

    let h = `<tr><th class="l">날짜</th>${STAGES.map(s => `<th><span class="dot" style="background:${stageColor(s)}"></span>${s}</th>`).join("")}</tr>`;
    M.days.forEach((d, i) => {
      h += `<tr style="cursor:pointer" data-date="${d.date}" title="이 날짜를 위 발전원 비교로 보기"><td>${d.date}</td>` + STAGES.map(s => {
        const R = run(s, state.ess); const day = R && R.days[i];
        if (!day) return `<td class="missing">—</td>`;
        if (day.status !== "optimal") return `<td class="bad">${esc(day.status)}</td>`;
        const v = dayVal(day, state.metric);
        return `<td class="${state.metric === "time" && v >= 10 ? "bad" : ""}">${m.fmt(v)}</td>`;
      }).join("") + "</tr>";
    });
    $("dailyTbl").innerHTML = h;
    $("dailyTbl").querySelectorAll("tr[data-date]").forEach(tr => tr.onclick = () => {
      setDate(tr.dataset.date); window.scrollTo({ top: 0, behavior: "smooth" });
    });
  }

  function renderStages() {
    let h = `<tr><th class="l">단계</th><th class="l">커밋 조합</th><th class="l">설명</th><th>무부하</th><th>ESS 용량 (MWh)</th></tr>`;
    for (const s of STAGES) {
      const st = M.stages[s];
      h += `<tr><td><span class="dot" style="background:${stageColor(s)}"></span><b>${s}</b></td><td class="l">${st.label}</td>
        <td class="l">${esc(st.desc)}</td><td>${st.no_load ? "있음" : "없음"}</td><td>${fmt0(st.ess_mwh)}</td></tr>`;
    }
    h += `<tr><td colspan="5" class="l" style="color:var(--text-secondary)">${Object.entries(M.commits).map(([k, v]) => `${k} = ${esc(v)}`).join(" · ")}</td></tr>`;
    $("stageTbl").innerHTML = h;
  }

  const moreOpen = () => $("moreSection").open;
  function renderMore() { if (!moreOpen()) return; renderSummary(); renderTotal(); renderDaily(); renderStages(); }
  function renderAll() { renderMain(); renderMore(); }

  // ── 컨트롤 ───────────────────────────────────────────────────────────────
  function setDate(d) { state.date = d; $("dateSel").value = d; renderMain(); }
  $("subtitle").textContent = `날짜 ${M.days.length}일 (${M.days[0].date} ~ ${M.days[M.days.length - 1].date}) · 단가 ${M.fuel_cost_month} · 생성 ${M.generated_at}`;
  $("dateSel").innerHTML = M.days.map(d => `<option>${d.date}</option>`).join("");
  $("dateSel").onchange = e => setDate(e.target.value);
  $("prevDay").onclick = () => { const i = dayIdx(); if (i > 0) setDate(M.days[i - 1].date); };
  $("nextDay").onclick = () => { const i = dayIdx(); if (i < M.days.length - 1) setDate(M.days[i + 1].date); };
  $("stageSeg").innerHTML = STAGES.map(s => `<button data-v="${s}" aria-pressed="${s === state.stage}">${s} (${M.stages[s].label})</button>`).join("");
  $("metricSeg").innerHTML = Object.entries(METRICS).map(([k, m]) => `<button data-v="${k}" aria-pressed="${k === state.metric}">${m.label}</button>`).join("");
  const seg = (id, key, after) => $(id).querySelectorAll("button").forEach(b => b.onclick = () => {
    state[key] = b.dataset.v;
    $(id).querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", x === b));
    after();
  });
  seg("stageSeg", "stage", renderMain);
  seg("essSeg", "ess", renderMore);
  seg("metricSeg", "metric", () => { if (moreOpen()) { renderTotal(); renderDaily(); } });
  $("moreSection").addEventListener("toggle", renderMore);
  document.addEventListener("keydown", e => {
    if (e.target.tagName === "SELECT") return;
    if (e.key === "ArrowLeft") $("prevDay").click();
    if (e.key === "ArrowRight") $("nextDay").click();
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", renderAll);

  renderAll();
})();
