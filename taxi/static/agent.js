"use strict";
// Reuse the dashboard's API, formatting, escaping and table helpers.
let agentConfig = null;
let agentResult = null;
let agentTrend = null;
let agentBusy = false;

function updateAgentMode() {
  const ai = $("#agent-mode").value === "ai";
  $("#agent-question-box").hidden = !ai;
  $("#run-agent").textContent = ai ? t("开始 AI 分析") : t("生成决策简报");
  $("#run-agent").disabled = agentBusy || !agentConfig?.months.length || (ai && !agentConfig?.ai_configured);
  $("#agent-setup").textContent = ai && !agentConfig?.ai_configured
    ? t(agentConfig?.setup_message || "正在检查模型配置…")
    : ai ? tr("模型：{0}。请确认问题与所选范围一致。", agentConfig.model) : t("固定流程生成完整运营简报，不调用模型，不回答自定义问题。");
  $("#agent-data-policy").textContent = ai ? t("问题、分析范围和聚合证据会发送到 OpenAI；不发送原始行程。") : t("所有计算在本机完成。");
}

async function loadAgentConfig() {
  try {
    agentConfig = await api("/api/agent/status");
    const prior = $("#agent-month").value;
    const priorBorough = $("#agent-borough").value;
    $("#agent-month").innerHTML = agentConfig.months.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
    $("#agent-month").value = agentConfig.months.includes(prior) ? prior : agentConfig.default_month || "";
    $("#agent-borough").innerHTML = htmlT('<option value="">全部</option>') + agentConfig.boroughs.map(b => `<option>${esc(b)}</option>`).join("");
    $("#agent-borough").value = agentConfig.boroughs.includes(priorBorough) ? priorBorough : "";
    $("#agent-mode-badge").textContent = agentConfig.ai_configured ? t("AI 已配置 · 固定简报可用") : t("固定简报可用 · AI 待配置");
    if (!agentConfig.months.length) $("#agent-progress").textContent = t("请先在数据导入页导入连续三个月数据。");
    if (!loadAgentConfig.restored) {
      I18N.restore($("#tab-agent"));
      if (I18N.saved?.agentResult) renderAgent(I18N.saved.agentResult);
      loadAgentConfig.restored = true;
    }
    updateAgentMode();
  } catch (e) {
    $("#agent-progress").textContent = e.message;
    $("#run-agent").disabled = true;
  }
}

const agentLabels = {
  month:t("月份"), service:t("服务"), trips:t("行程数"), calendar_days:t("日历天"), trips_per_day:t("日均行程"),
  daily_change_pct:t("日均环比 %"), share_pct:t("所选服务内份额 %"), passenger_payments:t("乘客支付 $"),
  zone:t("区域"), zone_id:t("区域编号"), borough:t("行政区"), day_type:t("日期类型"), time_band:t("时段"),
  previous_trips:t("上月行程"), current_trips:t("本月行程"), first_daily:t("首月日均"), previous_daily:t("上月日均"),
  current_daily:t("本月日均"), daily_delta:t("日均变化"), change_pct:t("变化 %"), avg_wait_min:t("平均等车（分）"),
  imported:t("已导入"), raw_rows:t("原始行数"), loaded_rows:t("有效行数"), observed_days:t("有行程天数"),
  expected_days:t("日历天数"), excluded_pct:t("排除 %"), complete:t("覆盖完整")
};
function evidenceTable(rows) {
  return table(rows.map(r => Object.fromEntries(Object.entries(r).map(([k,v]) =>
    [k, typeof v === "boolean" ? (v ? t("是") : t("否")) : k === "service" ? SERVICE_LABEL[v] : t(v)]))), true, agentLabels);
}
function drawAgentTrend(p) {
  if (typeof echarts === "undefined") {
    $("#agent-trend").innerHTML = evidenceTable(p.monthly);
    return;
  }
  agentTrend ||= echarts.init($("#agent-trend"), dark ? "dark" : null);
  agentTrend.setOption({ ...base, tooltip: {trigger:"axis"},
    xAxis: {type:"category", data:p.scope.months}, yAxis: {type:"value", name:t("有效行程 / 日")},
    series:p.scope.services.map(s => ({name:SERVICE_LABEL[s], type:"line", symbolSize:8,
      data:p.monthly.filter(r => r.service === s).map(r => r.trips_per_day), itemStyle:{color:COLORS[s]}}))
  }, true);
  agentTrend.resize();
}
function renderAgent(result) {
  agentResult = result;
  const p = result.evidence;
  $("#agent-summary").textContent = `${result.mode === "ai" ? t("AI 分析 · ") + result.model : result.mode === "data_check" ? t("数据检查 · 未调用模型") : t("固定简报 · 非 AI 生成")} · ${result.generated_at} · ${result.elapsed_seconds}s${result.cached ? t(" · 复用同版本证据") : ""}`;
  $("#agent-answer").textContent = result.answer_i18n?.[I18N.language] || result.answer;
  if (result.mode === "ai" && result.language && result.language !== I18N.language)
    $("#agent-summary").textContent += " · " + t("已有 AI 回答保留原语言；新分析会使用当前语言。");
  const latest = p.monthly.filter(r => r.month === p.scope.months.at(-1));
  const n = latest.reduce((a,r) => a+r.trips,0);
  const stats = [[t("最新月有效行程"),fmt(n)], [t("文件 / 日期检查"),p.ready ? t("通过") : t("待补齐")],
    [t("展示的增长分组"),p.opportunities.length], [t("展示的下降分组"),p.declines.length]];
  $("#agent-kpis").innerHTML = stats.map(([l,v])=>`<div class="kpi"><div class="v">${esc(v)}</div><div class="l">${esc(l)}</div></div>`).join("");
  $("#agent-trace").innerHTML = result.trace.length ? `<details><summary>${t("工具执行记录")}</summary>${result.trace.map(t=>`<p>${esc(t.evidence_id)} · ${esc(t.tool)} · ${tr("{0} 条聚合记录", t.rows)}</p>`).join("")}</details>` : "";
  $("#agent-evidence").hidden = false;
  $("#agent-opportunities").innerHTML = evidenceTable(p.opportunities);
  $("#agent-declines").innerHTML = evidenceTable(p.declines);
  $("#agent-coverage").innerHTML = evidenceTable(p.coverage);
  $("#agent-method").textContent = t(p.methodology) + "\n" + p.warnings.map(t).join("\n");
  $("#agent-queries").innerHTML = p.queries.map(q=>`<details><summary>${esc(q.label)} · ${q.ms} ms</summary><pre>${esc(q.sql)}\n\nParameters: ${esc(JSON.stringify(q.parameters))}</pre></details>`).join("");
  $("#agent-export").disabled = false;
  drawAgentTrend(p);
}
$("#agent-mode").onchange = updateAgentMode;
$$("#agent-examples button").forEach(b=>b.onclick=()=>{$("#agent-question").value=b.textContent;});
$("#run-agent").onclick = async () => {
  const services = $$("#agent-services input:checked").map(i=>i.value);
  if (!services.length) { $("#agent-progress").textContent = t("请至少选择一种服务。"); return; }
  agentBusy = true;
  updateAgentMode();
  $("#agent-export").disabled = true;
  $("#agent-answer").textContent = "";
  $("#agent-kpis").innerHTML = "";
  $("#agent-trace").innerHTML = "";
  $("#agent-evidence").hidden = true;
  $("#agent-summary").textContent = t("正在生成本次分析…");
  $("#agent-progress").textContent = t("正在检查数据覆盖并计算月度、区域和时段变化…");
  const started = performance.now();
  const ticker = setInterval(()=>{
    $("#agent-progress").textContent = tr("分析进行中 · {0} 秒；完成后显示证据与结果。", Math.floor((performance.now()-started)/1000));
  },1000);
  try {
    const result = await api("/api/agent/run", {method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({end_month:$("#agent-month").value, services, borough:$("#agent-borough").value||null,
        language:I18N.language, mode:$("#agent-mode").value, question:$("#agent-question").value})});
    renderAgent(result);
    $("#agent-progress").textContent = result.status === "insufficient_data" ? t("数据检查未通过，请查看需补齐的月份与服务。") : t("分析完成。请结合内部运营数据验证建议。");
  } catch(e) {
    $("#agent-progress").textContent = e.message;
    $("#agent-summary").textContent = t("本次分析未完成，请检查提示后重试。");
  } finally {
    clearInterval(ticker); agentBusy=false; updateAgentMode();
  }
};
$("#agent-export").onclick = () => {
  if (!agentResult) return;
  const exported = {...agentResult, language:I18N.language,
    answer:agentResult.answer_i18n?.[I18N.language] || agentResult.answer,
    evidence:{...agentResult.evidence, methodology:t(agentResult.evidence.methodology),
      warnings:agentResult.evidence.warnings.map(t),
      opportunities:agentResult.evidence.opportunities.map(r=>({...r,day_type:t(r.day_type)})),
      declines:agentResult.evidence.declines.map(r=>({...r,day_type:t(r.day_type)}))}};
  const blob = new Blob([JSON.stringify(exported,null,2)],{type:"application/json;charset=utf-8"});
  const url=URL.createObjectURL(blob), a=document.createElement("a");
  a.href=url; a.download=`mobility-brief-${agentResult.evidence.scope.months.at(-1)}.json`;
  a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
};
window.addEventListener("resize",()=>agentTrend?.resize());
$("[data-tab=agent]").addEventListener("click",()=>{loadAgentConfig(); setTimeout(()=>agentTrend?.resize(),0);});
loadAgentConfig();
