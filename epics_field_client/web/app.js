"use strict";
const token = document.querySelector('meta[name="field-token"]').content;
const form = document.querySelector("#config-form");
const message = document.querySelector("#message");
let defaults = {};
let detectedCA = {};
let hasNameServer = false;
let currentJob = null;
let busy = false;
let toolCatalog = null;
let toolInputs = [];
let lastToolJob = null;

async function api(path, body) {
  const response = await fetch(path, {method: body === undefined ? "GET" : "POST",
    headers: {"X-Field-Token": token, "Content-Type": "application/json"},
    body: body === undefined ? undefined : JSON.stringify(body)});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `请求失败 (${response.status})`);
  return value;
}
function normalizeAddress(text) { return text.replace(/,/g, " ").trim().split(/\s+/).filter(Boolean).join(" "); }
function inheritedMode(address) {
  if (hasNameServer) return false;
  // Only inherit the exact canonical string the user sees; caproto splits on spaces.
  const rawAddress = detectedCA.EPICS_CA_ADDR_LIST || "";
  if (rawAddress !== normalizeAddress(rawAddress)) return false;
  const original = normalizeAddress(detectedCA.EPICS_CA_ADDR_LIST || "");
  return (original !== "" && address === original) || (address === "" && original === "" && /^(YES|1|TRUE)$/i.test(detectedCA.EPICS_CA_AUTO_ADDR_LIST || ""));
}
function environmentInteger(key, fallback, maximum) {
  if (!Object.hasOwn(detectedCA, key)) return fallback;
  const value = Number(detectedCA[key]);
  if (!Number.isInteger(value) || value < 1 || value > maximum) throw new Error(`检测到的 ${key} 无效，请由控制系统负责人修正后重新启动程序。`);
  return value;
}
function configuration(kind) {
  const result = {...defaults, collect_network: true, run_caget: kind === "real"};
  if (kind !== "real") return {...result, connection_mode: "explicit", address_list: "", auto_address: false, pv_name: ""};
  const address = normalizeAddress(form.elements.address_list.value);
  return {...result, address_list: address, pv_name: form.elements.pv_name.value.trim(),
    connection_mode: inheritedMode(address) ? "inherit" : "explicit", auto_address: false,
    server_port: environmentInteger("EPICS_CA_SERVER_PORT", defaults.server_port, 65535),
    repeater_port: environmentInteger("EPICS_CA_REPEATER_PORT", defaults.repeater_port, 65535),
    max_array_bytes: environmentInteger("EPICS_CA_MAX_ARRAY_BYTES", defaults.max_array_bytes, 16777216)};
}
function sourceHint() {
  const address = normalizeAddress(form.elements.address_list.value);
  document.querySelector("#config-source").textContent = inheritedMode(address) ?
    (address ? "已检测到本机 EPICS 配置，本次将沿用该配置。地址已自动填入，可按实际设备修改。" : "已检测到本机已有的自动搜索配置。填写 PV 后会沿用该配置读取，无需另填地址。") :
    address ? "将使用上面的地址；端口从本机环境读取，未设置时使用默认值。" :
    "未检测到可直接使用的设备地址，请填写控制系统负责人提供的 IOC／网关地址。";
}
function setBusy(value) {
  busy = value;
  for (const button of document.querySelectorAll("button[data-kind]")) button.disabled = value;
  for (const input of form.elements) if (input.name) input.disabled = value;
  for (const id of ["export-md", "export-json"]) document.getElementById(id).disabled = value || !currentJob;
  for (const control of document.querySelectorAll("#tool-form input, #tool-form textarea, #tool-select")) control.disabled = value;
  document.querySelector("#load-tools").disabled = value;
  const selected = selectedTool();
  document.querySelector("#run-tool").disabled = value || !selected?.enabled;
  document.querySelector("#export-tool").disabled = value || !lastToolJob;
}
function display(value) {
  if (value === null || value === undefined || value === "") return "未检测到";
  if (typeof value === "boolean") return value ? "是" : "否";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}
function table(container, headers, rows) {
  container.replaceChildren();
  const wrap = document.createElement("div"); wrap.className = "table-wrap";
  const element = document.createElement("table");
  const head = document.createElement("thead"); const header = document.createElement("tr");
  for (const text of headers) { const cell = document.createElement("th"); cell.textContent = text; header.append(cell); }
  head.append(header); element.append(head);
  const body = document.createElement("tbody");
  for (const row of rows) {
    const tr = document.createElement("tr");
    for (const value of row) { const cell = document.createElement("td"); cell.textContent = display(value); tr.append(cell); }
    body.append(tr);
  }
  element.append(body); wrap.append(element); container.append(wrap);
}
function paragraph(container, text, style="hint") {
  const p = document.createElement("p"); p.className = style; p.textContent = text; container.append(p);
}
function environmentSummary(environment) {
  const rows = [["操作系统", environment.distribution || environment.system], ["内核／系统版本", environment.release],
    ["处理器架构", environment.machine], ["电脑名称", environment.hostname], ["CPU 逻辑核心数", environment.cpu_count],
    ["Python", environment.python], ["Python 路径", environment.python_executable]];
  for (const [name, version] of Object.entries(environment.packages || {})) rows.push([name, version || "未安装"]);
  for (const [name, path] of Object.entries(environment.cli_paths || {})) rows.push([`${name} 命令`, path || "未安装／不在 PATH 中"]);
  table(document.querySelector("#computer-summary"), ["检测项目", "发现的信息"], rows);
  const caRows = Object.entries(environment.inherited_ca_environment || {});
  if (environment.ca_name_servers) caRows.push(["EPICS_CA_NAME_SERVERS", environment.ca_name_servers]);
  table(document.querySelector("#ca-summary"), ["配置项", "本机已有值"], caRows.length ? caRows : [["EPICS 环境变量", "未检测到；需要填写设备地址"]]);
  if (environment.unsupported_name_server_configured) paragraph(document.querySelector("#ca-summary"), "检测到了 name server 配置，当前适配器不支持。测试时请填写直接可访问的 IOC／网关地址。", "notice");
  const network = document.querySelector("#network-summary"); network.replaceChildren();
  if (environment.network_snapshot) {
    const snapshot = environment.network_snapshot;
    const interfaces = document.createElement("div");
    table(interfaces, ["网卡", "状态", "地址"], snapshot.interfaces.length ? snapshot.interfaces.map(row => [row.name, row.state, row.addresses.join(", ") || "没有 IP 地址"]) : [["网卡信息", "未取得", "请查看下方原因"]]);
    network.append(interfaces);
    const routes = document.createElement("div");
    table(routes, ["目标网段", "网关", "网卡"], snapshot.routes.length ? snapshot.routes.map(row => [row.destination, row.gateway || "直接连接", row.interface]) : [["路由信息", "未取得", "—"]]);
    network.append(routes);
    for (const error of snapshot.errors) paragraph(network, error, "error");
  } else {
    for (const evidence of environment.network_details || []) {
      paragraph(network, evidence.command.join(" "));
      const pre = document.createElement("pre"); pre.textContent = evidence.stdout || evidence.error || evidence.stderr || "未取得输出"; network.append(pre);
    }
    if (!environment.network_details?.length) paragraph(network, "本次未收集到网络信息。");
  }
  const note = document.querySelector("#platform-note");
  note.hidden = true;
  note.textContent = "";
}
function shortError(stage) {
  if (stage.error) return stage.error;
  if (stage.data?.error) return stage.data.error;
  return stage.raw?.content?.find(block => block.type === "text")?.text || "未取得有效结果";
}
function showResult(job) {
  currentJob = job.id;
  const report = job.report;
  document.querySelector("#collected").hidden = false;
  document.querySelector("#collected-time").textContent = new Date(report.finished_at).toLocaleString("zh-CN");
  environmentSummary(report.environment);
  const status = report.conclusion.status;
  const titles = {simulation_passed: "软件自检通过（本机模拟数据）", real_pv_read: "已读取到设备数据",
    environment_only: "本机信息收集完成，尚未测试设备连接", identity_unverified: "收到数据，但设备身份尚未确认",
    failed: report.test_kind === "simulation" ? "软件自检未通过" : "未读取到设备数据"};
  const box = document.querySelector("#conclusion"); box.textContent = titles[status] || report.conclusion.message; box.dataset.status = status;
  const info = report.stages.find(stage => stage.stage === "pv_info" && stage.ok)?.data;
  const reading = report.stages.find(stage => stage.stage === "get_pv" && stage.ok)?.data;
  document.querySelector("#reading").hidden = !reading || !Object.hasOwn(reading, "value");
  if (reading && Object.hasOwn(reading, "value")) {
    const value = reading.value;
    const shown = typeof value === "number" && Number.isFinite(value) ? new Intl.NumberFormat("zh-CN", {maximumSignificantDigits:12, useGrouping:false, notation:(value !== 0 && Math.abs(value)<0.0001)||Math.abs(value)>=1e9 ? "scientific":"standard"}).format(value) : display(value);
    document.querySelector("#reading-name").textContent = `${report.test_kind === "simulation" ? "模拟数据 · " : ""}${reading.name}`;
    document.querySelector("#reading-value").textContent = `${shown} ${reading.units === "degC" ? "°C" : reading.units || ""}`;
    document.querySelector("#reading-extra").textContent = `数据告警：${reading.severity} · 数据时间：${reading.timestamp || "未提供"}`;
  }
  const testRows = [["本次操作", {environment:"收集本机信息",simulation:"软件自检（不连接现场设备）",real:"真实设备读取"}[report.test_kind]]];
  if (report.test_kind !== "environment") testRows.push(
    ["测试 PV", report.tested_pv || "未进行设备读取"], ["配置来源", report.config.connection_mode === "inherit" ? "本机 EPICS 环境变量" : "本次填写的地址与默认参数"],
    ["本次传入搜索地址", report.environment.launch_ca_environment.EPICS_CA_ADDR_LIST],
    ["实际 PV 服务地址", info?.server], ["PV 类型", info?.native_type], ["IOC 授予读取权限", info?.read_access],
    ["IOC 授予写入权限", info?.write_access], ["数值是否与现场基准一致", "需要现场人员核对"]);
  else testRows.push(["设备连接", "尚未测试；收集电脑信息不会连接设备"]);
  for (const [key, value] of Object.entries(report.runtime_ca_environment || {})) testRows.push([`运行时 ${key}`, value]);
  table(document.querySelector("#test-summary"), ["检测项目", "本次结果"], testRows);
  const stageLabels = {mcp_initialize:"读取服务启动",get_connection_info:"连接模式检查",pv_info:"PV 连接与权限",get_pv:"PV 数值读取",mcp_session:"读取会话"};
  const stageRows = report.stages.map(stage => [stageLabels[stage.stage] || stage.stage, stage.ok ? "通过" : "失败", stage.ok ? (stage.elapsed_s === undefined ? "已取得响应" : `${stage.elapsed_s} 秒`) : shortError(stage)]);
  if (report.caget_baseline) {
    const result = report.caget_baseline;
    stageRows.push(["caget 对照", result.skipped ? "未运行" : result.returncode === 0 ? "成功" : "失败", result.reason || result.stdout || result.error || result.stderr]);
  }
  table(document.querySelector("#stage-summary"), ["检查环节", "结果", "发现的信息"], stageRows.length ? stageRows : [["设备连接", "未测试", "本次只收集本机信息"]]);
  const next = {simulation_passed:"自检使用模拟数据，现场设备仍需单独测试。",real_pv_read:"请与控制系统中的读数核对后，导出报告反馈。",failed:"请核对地址、PV 和网络条件，具体错误已列在上方。导出报告可供排查。",environment_only:"以上是实际检测到的信息。填写 PV 后可继续测试设备连接。",identity_unverified:"请导出报告核实连接身份，暂不判定现场设备接入成功。"};
  document.querySelector("#next-action").textContent = next[status] || report.conclusion.message;
  document.querySelector("#report-status").textContent = "报告已准备好，内容与上方本次收集结果一致。";
}
async function run(kind) {
  if (busy) return;
  currentJob = null;
  document.querySelector("#report-status").textContent = "本次收集完成后可导出报告。";
  document.querySelector("#conclusion").textContent = "尚未取得本次结果";
  document.querySelector("#reading").hidden = true;
  document.querySelector("#test-summary").replaceChildren(); document.querySelector("#stage-summary").replaceChildren();
  document.querySelector("#next-action").textContent = "";
  setBusy(false);
  const started = Date.now();
  let loadAfter = false;
  try {
    const config = configuration(kind);
    if (kind === "real" && !config.pv_name) { form.elements.pv_name.focus(); throw new Error("请填写一个已知可读的数据名称（PV）。"); }
    if (kind === "real" && config.connection_mode === "explicit" && !config.address_list) { form.elements.address_list.focus(); throw new Error("未检测到可用地址，请填写 IOC／CA 网关地址。"); }
    setBusy(true);
    message.textContent = kind === "real" ? "正在读取指定设备数据…" : kind === "simulation" ? "正在检查软件读取功能…" : "正在收集本机环境和网络信息…";
    if (kind === "real") await api("/api/config", config);
    const job = await api("/api/jobs", {config, kind});
    while (true) {
      await new Promise(resolve => setTimeout(resolve,700));
      const state = await api(`/api/jobs/${job.id}`);
      if (state.state === "error") throw new Error(state.error);
      if (state.state === "done") { showResult(state); message.textContent = "收集完成，实际发现的信息如下。"; loadAfter = kind === "real" && state.report.conclusion.real_connection_verified; break; }
      message.textContent = `正在${kind === "real" ? "读取设备" : kind === "simulation" ? "检查软件" : "收集环境信息"}，已等待 ${Math.floor((Date.now()-started)/1000)} 秒。`;
    }
  } catch (error) { message.textContent = error.message; document.querySelector("#report-status").textContent = "本次操作没有生成报告，请按提示重试。"; }
  finally { setBusy(false); }
  if (loadAfter) await loadTools();
}

const toolLabels = {get_connection_info:"连接与服务状态",get_command_log:"查看操作日志",reconnect:"重新连接",
  get_pv:"读取一个 PV",get_pvs:"批量读取 PV",pv_info:"PV 地址与权限",monitor_pv:"持续监测 PV",
  put_pv:"写入一个 PV",put_pvs:"批量写入 PV",apply_safe_state:"执行安全动作"};
const parameterLabels = {name:"PV 名称",names:"PV 名称列表",value:"写入值（JSON）",writes:"写入列表（JSON）",
  max_elements:"最多返回的数组元素",duration_s:"监测时长（秒）",max_updates:"最多采集的变化次数",
  max_points:"最多返回的数据点",limit:"日志条数",wait:"等待 IOC 确认完成",timeout_s:"等待完成的时间（秒）"};
function selectedTool() {
  return toolCatalog?.tools.find(tool => tool.name === document.querySelector("#tool-select").value);
}
function renderToolConnection() {
  document.querySelector("#tool-connection").textContent = `工具目标：${toolCatalog.connection.server} · PV：${toolCatalog.config.pv_name} · ${toolCatalog.write_pvs.length ? "允许写入：" + toolCatalog.write_pvs.join("、") : "只读模式；上游不会提供 put_pv／put_pvs。"}`;
}
function invalidateTools() {
  toolCatalog = null;
  lastToolJob = null;
  document.querySelector("#tool-workspace").hidden = true;
  document.querySelector("#tools-status").textContent = "连接参数已变化，请测试设备连接或重新加载工具。";
  document.querySelector("#run-tool").disabled = true;
}
function renderToolForm() {
  const tool = selectedTool();
  const fields = document.querySelector("#tool-fields"); fields.replaceChildren(); toolInputs = [];
  lastToolJob = null;
  document.querySelector("#tool-result").hidden = true;
  document.querySelector("#tool-result-status").textContent = tool?.disabled_reason || "填写参数后运行；返回结果将显示在这里。";
  document.querySelector("#tool-description").textContent = tool?.description || "";
  document.querySelector("#tool-schema").textContent = JSON.stringify(tool?.input_schema || {}, null, 2);
  document.querySelector("#write-confirmation").hidden = !tool?.write || !tool.enabled;
  document.querySelector("#confirm-write").checked = false;
  if (!tool) return;
  const required = new Set(tool.input_schema.required || []);
  for (const [key, spec] of Object.entries(tool.input_schema.properties || {})) {
    const label = document.createElement("label");
    const title = document.createElement("span"); title.textContent = `${key} · ${parameterLabels[key] || key}${required.has(key) ? "（必填）" : ""}`; label.append(title);
    const complex = !["string", "number", "integer", "boolean"].includes(spec.type);
    const input = document.createElement(complex ? "textarea" : "input");
    input.name = key; input.required = required.has(key) && spec.type !== "boolean";
    if (complex) { input.rows = 4; input.placeholder = "输入合法 JSON，例如 [\"PV:ONE\", \"PV:TWO\"]"; }
    else if (spec.type === "boolean") input.type = "checkbox";
    else if (["integer", "number"].includes(spec.type)) { input.type = "number"; input.step = spec.type === "integer" ? "1" : "any"; if (spec.minimum !== undefined) input.min = spec.minimum; if (spec.maximum !== undefined) input.max = spec.maximum; }
    else input.type = "text";
    if (spec.default !== undefined) { if (spec.type === "boolean") input.checked = spec.default; else input.value = complex ? JSON.stringify(spec.default, null, 2) : spec.default; }
    else if (key === "name") input.value = form.elements.pv_name.value.trim();
    else if (key === "names") input.value = JSON.stringify([form.elements.pv_name.value.trim()], null, 2);
    input.addEventListener("input",()=>{document.querySelector("#confirm-write").checked=false;});
    label.append(input); fields.append(label); toolInputs.push({key,spec,input,complex,required:required.has(key)});
  }
  if (!toolInputs.length) paragraph(fields, "此工具不需要参数。");
  setBusy(busy);
}
async function waitToolJob(job, statusElement) {
  const started = Date.now();
  while (true) {
    await new Promise(resolve=>setTimeout(resolve,700));
    const state = await api(`/api/jobs/${job.id}`);
    if (state.state === "error") throw new Error(state.error);
    if (state.state === "done") return state;
    statusElement.textContent = `正在执行，已等待 ${Math.floor((Date.now()-started)/1000)} 秒；请等待结果。`;
  }
}
async function loadTools() {
  if (busy) return;
  const status = document.querySelector("#tools-status");
  invalidateTools();
  try {
    const config = configuration("real");
    if (!config.pv_name) throw new Error("请填写一个已知可读的 PV，再加载工具。");
    setBusy(true); status.textContent = "正在连接 LabMCP 并读取工具清单…";
    const job = await api("/api/tools/connect", {config});
    toolCatalog = (await waitToolJob(job, status)).result;
    document.querySelector("#tool-workspace").hidden = false;
    status.textContent = `已加载当前服务的全部 ${toolCatalog.tools.length} 个工具。`;
    renderToolConnection();
    table(document.querySelector("#tool-list"), ["工具", "用途", "可运行"], toolCatalog.tools.map(tool=>[tool.name,toolLabels[tool.name] || tool.description,tool.enabled ? tool.write ? "写入（需要确认）" : "是" : tool.disabled_reason]));
    const select = document.querySelector("#tool-select"); select.replaceChildren();
    for (const tool of toolCatalog.tools) { const option = document.createElement("option"); option.value=tool.name; option.textContent=`${toolLabels[tool.name] || tool.name} · ${tool.name}${tool.enabled ? "" : "（不可执行）"}`; select.append(option); }
    select.value = "get_pv"; renderToolForm();
  } catch (error) { toolCatalog=null; status.textContent=error.message; }
  finally { setBusy(false); }
}
async function runTool(event) {
  event.preventDefault();
  if (busy) return;
  const status=document.querySelector("#tool-result-status"); const output=document.querySelector("#tool-result");
  lastToolJob=null; output.hidden=true;
  try {
    const tool=selectedTool(); if (!tool?.enabled) throw new Error("请先加载并选择可执行的工具。");
    const args={};
    for (const field of toolInputs) {
      const raw=field.input.value.trim();
      if (field.spec.type === "boolean") args[field.key]=field.input.checked;
      else if (!raw && !field.required) continue;
      else if (field.complex) { try { args[field.key]=JSON.parse(raw); } catch { throw new Error(`${field.key} 请填写合法 JSON。`); } }
      else if (["integer","number"].includes(field.spec.type)) { const value=Number(raw); if (!raw || !Number.isFinite(value)) throw new Error(`${field.key} 请填写有效数字。`); args[field.key]=value; }
      else args[field.key]=raw;
    }
    const confirmed=document.querySelector("#confirm-write").checked;
    if (tool.write && !confirmed) throw new Error("请核对 PV 与参数，并勾选本次写入确认。");
    document.querySelector("#confirm-write").checked=false;
    setBusy(true); status.textContent="正在运行工具…";
    const job=await api("/api/tools/call", {catalog_id:toolCatalog.id,name:tool.name,arguments:args,confirmed});
    const result=(await waitToolJob(job,status)).result; lastToolJob=job.id;
    output.hidden=false; output.textContent=JSON.stringify(result,null,2);
    if (result.catalog) { toolCatalog=result.catalog; renderToolConnection(); }
    if (result.connection_lost) { toolCatalog=null; document.querySelector("#tools-status").textContent="工具连接已中断，请重新加载；之前的结果保留如下。"; }
    status.textContent=toolOutcome(result);
    document.querySelector("#confirm-write").checked=false;
  } catch (error) { status.textContent=error.message; }
  finally { document.querySelector("#confirm-write").checked=false; setBusy(false); }
}
function toolOutcome(result) {
  const partial=result.data?.errors?.length || result.data?.error || result.data?.completed === false || result.data?.results?.some(row=>row.completed === false);
  return result.outcome_uncertain ? "写入结果不确定：请先读取 PV 核实，勿直接重复执行。" : !result.ok ? "工具执行失败，错误内容如下。" : partial ? "工具已返回，但部分项目失败或尚未完成，请查看结果。" : "工具执行完成，实际返回结果如下。";
}
document.querySelector("#load-tools").addEventListener("click",loadTools);
document.querySelector("#tool-select").addEventListener("change",renderToolForm);
document.querySelector("#tool-form").addEventListener("submit",runTool);
document.querySelector("#export-tool").addEventListener("click",async()=>{ const saved=currentJob; currentJob=lastToolJob; await exportReport("json"); currentJob=saved; });
async function exportReport(extension) {
  if (!currentJob || busy) return;
  try {
    const response = await fetch(`/api/reports/${currentJob}.${extension}`, {headers:{"X-Field-Token":token}});
    if (!response.ok) throw new Error("报告导出失败，请重试。");
    const url = URL.createObjectURL(await response.blob()); const link = document.createElement("a");
    link.href = url; link.download = `epics-report-${currentJob}.${extension}`; link.click(); setTimeout(()=>URL.revokeObjectURL(url),2000);
  } catch (error) { message.textContent = error.message; }
}
for (const button of document.querySelectorAll("button[data-kind]")) button.addEventListener("click",()=>run(button.dataset.kind));
document.querySelector("#export-md").addEventListener("click",()=>exportReport("md"));
document.querySelector("#export-json").addEventListener("click",()=>exportReport("json"));
form.addEventListener("submit",event=>event.preventDefault());
form.elements.address_list.addEventListener("input",()=>{sourceHint();invalidateTools();});
form.elements.pv_name.addEventListener("input",invalidateTools);
(async()=>{
  try {
    const bootstrap = await api("/api/bootstrap"); defaults = bootstrap.defaults; detectedCA = bootstrap.detected_ca_environment; hasNameServer = bootstrap.name_server_configured;
    form.elements.address_list.value = detectedCA.EPICS_CA_ADDR_LIST || bootstrap.saved_config.address_list || "";
    form.elements.pv_name.value = bootstrap.saved_config.pv_name || "";
    sourceHint(); await run("environment");
  } catch (error) { message.textContent = `检测未能启动：${error.message}`; }
})();
