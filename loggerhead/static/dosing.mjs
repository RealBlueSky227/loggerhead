const clone = value => JSON.parse(JSON.stringify(value));
const escapeHTML = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[char]));
const numericPumpFields = ["steps_per_ml", "manual_speed_steps_per_second", "dose_speed_steps_per_second", "manual_max_seconds", "manual_max_steps"];
const numericScheduleFields = ["daily_volume_ml", "doses_per_day", "max_dose_ml", "max_run_seconds"];
const number = (value, label, {integer=false, zero=false, max=Infinity}={}) => {
  const parsed = typeof value === "string" && !value.trim() ? NaN : Number(value);
  if (!Number.isFinite(parsed) || (integer && !Number.isInteger(parsed)) || parsed < 0 || (!zero && parsed === 0) || parsed > max) {
    throw new Error(`${label} must be ${zero ? "nonnegative" : "positive"}${integer ? " and an integer" : ""}${max < Infinity ? `, at most ${max}` : ""}.`);
  }
  return parsed;
};

export function calibratedStepsPerMl(record, measuredMl, operationId) {
  if (!record || record.operation_id !== operationId || record.state !== "completed" || record.reason !== "max_steps" ||
      !(record.confirmed_steps > 0) || record.confirmed_steps !== record.requested_steps) {
    throw new Error("Calibration requires a fully completed, matching fixed-step run.");
  }
  return record.confirmed_steps / number(measuredMl, "Measured volume");
}

export function validateDosingConfig(config) {
  for (const pump of config.steppers || []) {
    for (const key of numericPumpFields) {
      const speed = key.includes("speed_");
      pump[key] = number(pump[key] ?? (key === "dose_speed_steps_per_second" ? 400 : NaN), key.replaceAll("_", " "), {
        integer: speed || key === "manual_max_steps", max: speed ? 5000 : Infinity,
      });
    }
  }
  const used = new Set(), ids = new Set();
  for (const schedule of config.dosing || []) {
    if (ids.has(schedule.id)) throw new Error("Schedule IDs must be unique.");
    ids.add(schedule.id);
    for (const key of numericScheduleFields) schedule[key] = number(schedule[key] ?? ({max_dose_ml:10,max_run_seconds:120}[key]), key.replaceAll("_", " "), {
      zero: key === "daily_volume_ml" || (key === "doses_per_day" && !schedule.enabled),
      integer: key === "doses_per_day", max: key === "doses_per_day" ? 96 : key === "max_run_seconds" ? 600 : Infinity,
    });
    for (const key of ["window_start", "window_end"]) {
      if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(schedule[key])) throw new Error("Schedule times must use HH:MM.");
    }
    if (schedule.enabled) {
      const pump = config.steppers.find(item => item.id === schedule.actuator);
      if (!pump || !schedule.stepper) throw new Error("Enabled schedules require an onboard stepper pump.");
      if (used.has(pump.id)) throw new Error("Only one enabled schedule is allowed per pump.");
      used.add(pump.id);
      if (!schedule.daily_volume_ml || !schedule.doses_per_day) throw new Error("Enabled schedules need a positive daily volume and dose count.");
      const ml = schedule.daily_volume_ml / schedule.doses_per_day;
      const steps = Math.floor(ml * pump.steps_per_ml);
      if (ml > schedule.max_dose_ml) throw new Error("Dose volume exceeds the per-dose limit.");
      if (steps < 1) throw new Error("Dose volume is smaller than one calibrated step.");
      if (steps / pump.dose_speed_steps_per_second + 2 > schedule.max_run_seconds) throw new Error("Dose does not fit the runtime limit, including startup margin.");
    }
  }
  return config;
}

export class DosingModel {
  constructor() { this.status = {}; this.pumps = []; this.schedules = []; this.edits = {steppers: new Map(), dosing: new Map()}; this.added = new Set(); this.removed = new Set(); this.hash = ""; }
  get dirty() { return this.edits.steppers.size > 0 || this.edits.dosing.size > 0 || this.added.size > 0 || this.removed.size > 0; }
  update(status, focused=false) {
    this.status = status;
    const hash = JSON.stringify([status.config?.steppers, status.config?.dosing]);
    if (this.dirty || focused || hash === this.hash || !status.config) return false;
    this.accept(status.config); return true;
  }
  accept(config) {
    this.pumps = clone(config.steppers || []);
    this.schedules = clone(config.dosing || []).map(row => ({enabled:false, max_dose_ml:10, max_run_seconds:120, ...row}));
    this.edits = {steppers:new Map(), dosing:new Map()}; this.added.clear(); this.removed.clear();
    this.hash = JSON.stringify([config.steppers, config.dosing]);
  }
  edit(section, id, key, value) {
    const item = (section === "steppers" ? this.pumps : this.schedules).find(row => row.id === id);
    if (!item) throw new Error("Configuration item is no longer available.");
    item[key] = value;
    if (section === "dosing" && key === "actuator") item.stepper = this.pumps.some(pump => pump.id === value);
    const keys = this.edits[section].get(id) || new Set(); keys.add(key);
    if (section === "dosing" && key === "actuator") keys.add("stepper");
    this.edits[section].set(id, keys);
  }
  add() {
    const id = globalThis.crypto?.randomUUID?.() || `dose-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    this.schedules.push({id, name:"New schedule", actuator:this.pumps[0]?.id || "", stepper:true, enabled:false,
      daily_volume_ml:0, doses_per_day:12, window_start:"08:00", window_end:"20:00",
      calibration_ml_per_minute:30, max_dose_ml:10, max_run_seconds:120});
    this.added.add(id); return id;
  }
  remove(id) { this.schedules = this.schedules.filter(row => row.id !== id); this.removed.add(id); this.added.delete(id); this.edits.dosing.delete(id); }
  mergeConfig(payload) {
    const next = clone(payload);
    for (const section of ["steppers", "dosing"]) {
      const drafts = section === "steppers" ? this.pumps : this.schedules;
      next[section] = (next[section] || []).filter(row => section !== "dosing" || !this.removed.has(row.id)).map(row => {
        const keys = this.edits[section].get(row.id), draft = drafts.find(item => item.id === row.id);
        if (!keys) return row;
        for (const key of keys) row[key] = draft[key];
        return row;
      });
      for (const id of this.edits[section].keys()) {
        if (!next[section].some(row => row.id === id) && !this.added.has(id)) throw new Error("Raw config and editor remove the same item differently; cancel one edit before saving.");
      }
    }
    for (const row of this.schedules) if (this.added.has(row.id)) next.dosing.push(clone(row));
    return validateDosingConfig(next);
  }
}

export function mountDosingEditor(doc, bridge, post) {
  const pumpRoot = doc.getElementById("pumpConfig"), scheduleRoot = doc.getElementById("dosingSchedules");
  if (!pumpRoot || !scheduleRoot) return null;
  const model = new DosingModel(), worksheets = new Map();
  let previews = null;
  const message = (id, text, error=false) => { const el=doc.getElementById(id); if (el) { el.textContent=text; el.className=`message ${error ? "error" : ""}`; } };
  const dirty = () => { previews=null; message("configMessage", "Unsaved pump or schedule changes."); updateRuntime(); };
  const refresh = () => doc.dispatchEvent(new Event("loggerhead:refresh-request"));
  const date = ts => ts ? new Date(ts*1000).toLocaleString() : "None";
  function worksheet(pump) {
    if (!worksheets.has(pump.id)) worksheets.set(pump.id, {steps:"4000", speed:String(pump.manual_speed_steps_per_second), volume:"", operationId:null, blocked:false, message:""});
    return worksheets.get(pump.id);
  }
  function field(section, item, key, label, type="number", extra="") {
    return `<label><span>${escapeHTML(label)}</span><input aria-label="${escapeHTML(label)}" data-section="${section}" data-id="${escapeHTML(item.id)}" data-key="${key}" type="${type}" ${type === "number" ? 'step="any"' : ""} value="${escapeHTML(item[key] ?? (key === "dose_speed_steps_per_second" ? 400 : ""))}" ${extra}></label>`;
  }
  function renderPumps() {
    pumpRoot.innerHTML = model.pumps.map(pump => {
      const work=worksheet(pump), id=escapeHTML(pump.id);
      return `<div class="card" data-pump="${id}"><h3>${escapeHTML(pump.name)}</h3><div class="status-line">${escapeHTML(pump.assignment)} | ${escapeHTML(pump.microsteps)} microsteps | ${escapeHTML(pump.run_current_ma)} mA configured</div>
        <div class="field-grid">${field("steppers",pump,"name","Pump name","text")}${field("steppers",pump,"steps_per_ml","Calibration (steps/ml)")}${field("steppers",pump,"manual_speed_steps_per_second","Prime speed (step/s)")}${field("steppers",pump,"dose_speed_steps_per_second","Dose speed (step/s)")}${field("steppers",pump,"manual_max_seconds","Prime limit (s)")}${field("steppers",pump,"manual_max_steps","Prime step limit")}</div>
        <fieldset class="calibration"><legend>Measured Calibration</legend><div class="field-grid">${[ ["steps","Test steps"], ["speed","Test speed (step/s)"], ["volume","Measured volume (ml)"] ].map(([key,label])=>`<label><span>${label}</span><input aria-label="${label}" type="number" step="any" data-calibration="${id}" data-work="${key}" value="${escapeHTML(work[key])}"></label>`).join("")}</div>
        <div class="toolbar"><button data-action="calibrate" data-id="${id}">Run Calibration</button><button data-action="stop" data-id="${id}">Stop</button><button data-action="apply" data-id="${id}">Apply Calibration</button></div><div class="message" data-cal-message="${id}" role="status"></div></fieldset></div>`;
    }).join("");
  }
  function renderSchedules() {
    scheduleRoot.innerHTML = `<div class="toolbar"><button data-action="preview">Preview</button><span class="message" id="dosePreviewMessage" role="status"></span><span class="status-line">${escapeHTML(model.status.dosing?.timezone || "Controller local time")}</span></div>` + model.schedules.map(schedule => {
      const id=escapeHTML(schedule.id), legacy=!schedule.stepper;
      const pumps = model.pumps.map(pump=>`<option value="${escapeHTML(pump.id)}" ${pump.id===schedule.actuator ? "selected" : ""}>${escapeHTML(pump.name)}</option>`).join("");
      return `<div class="dosing-schedule" data-schedule="${id}"><div class="field-grid">
      ${field("dosing",schedule,"name","Schedule name","text")}<label><span>Pump</span><select aria-label="Pump" data-section="dosing" data-id="${id}" data-key="actuator">${legacy ? `<option value="${escapeHTML(schedule.actuator)}" selected>Legacy relay: ${escapeHTML(schedule.actuator)}</option>` : ""}${pumps}</select></label>
      <label><span>Enabled</span><input aria-label="Enabled" type="checkbox" data-section="dosing" data-id="${id}" data-key="enabled" ${schedule.enabled ? "checked" : ""} ${legacy ? "disabled" : ""}></label>
      ${field("dosing",schedule,"daily_volume_ml","Daily volume (ml)")}${field("dosing",schedule,"doses_per_day","Doses per day")}${field("dosing",schedule,"window_start","Window start","time")}${field("dosing",schedule,"window_end","Window end","time")}${field("dosing",schedule,"max_dose_ml","Dose limit (ml)")}${field("dosing",schedule,"max_run_seconds","Runtime limit (s)")}</div>
      <div class="status-line" data-summary="${id}"></div><div class="message" data-runtime="${id}"></div><div class="toolbar"><button data-action="pause" data-id="${id}">Pause</button><button data-action="reset" data-id="${id}">Reset Fault</button><button data-action="remove" data-id="${id}">Remove</button></div><ul class="dose-preview" data-preview="${id}"></ul></div>`;
    }).join("");
  }
  function updateRuntime() {
    const occupied = !!model.status.stepper_active || Object.values(model.status.manual_priming || {}).some(Boolean);
    for (const pump of model.pumps) {
      const card=[...pumpRoot.querySelectorAll("[data-pump]")].find(el=>el.dataset.pump===pump.id);
      if (!card) continue;
      const work=worksheet(pump), record=model.status.calibrations?.[pump.id];
      if (record && !work.operationId && !work.blocked) {
        work.operationId=record.operation_id;
        work.steps=String(record.requested_steps); work.speed=String(record.speed_steps_per_second);
        for (const key of ["steps","speed"]) { const input=card.querySelector(`[data-work="${key}"]`); if (input && doc.activeElement!==input) input.value=work[key]; }
      }
      const active=!!model.status.manual_priming?.[pump.id] || model.status.stepper_active===pump.id;
      card.querySelector('[data-action="calibrate"]').disabled=occupied;
      card.querySelector('[data-action="stop"]').disabled=!active;
      let usable=false;
      try { calibratedStepsPerMl(record,work.volume,work.operationId); usable=true; } catch { /* Incomplete measurements are not actionable. */ }
      card.querySelector('[data-action="apply"]').disabled=!usable || occupied;
      const feedback=card.querySelector("[data-cal-message]");
      const own=record && record.operation_id===work.operationId;
      feedback.textContent=work.message || (own ? `${record.state}: ${record.confirmed_steps} / ${record.requested_steps} verified steps${record.error ? `; ${record.error}` : ""}` : work.operationId ? "Calibration result unavailable." : "");
      feedback.className=`message ${own && ["failed","incomplete"].includes(record.state) ? "error" : ""}`;
    }
    for (const row of scheduleRoot.querySelectorAll("[data-schedule]")) {
      const id=row.dataset.schedule, schedule=model.schedules.find(item=>item.id===id);
      const pump=model.pumps.find(item=>item.id===schedule.actuator);
      const ml=Number(schedule.daily_volume_ml)/Number(schedule.doses_per_day), steps=pump ? Math.floor(ml*Number(pump.steps_per_ml)) : 0;
      row.querySelector("[data-summary]").textContent=Number.isFinite(ml) ? `${ml.toFixed(3)} ml requested per dose | ${steps} steps | ${pump ? (steps/Number(pump.dose_speed_steps_per_second || 400)).toFixed(1) : "-"} s estimated` : "Set a positive dose count.";
      const runtime=model.status.dosing?.schedules.find(item=>item.id===id), active=model.status.dosing?.active?.schedule_id===id;
      const unsaved=model.added.has(id) || model.edits.dosing.has(id) || model.edits.steppers.has(schedule.actuator);
      row.querySelector("[data-runtime]").textContent=unsaved ? "Unsaved schedule" : runtime?.fault || (runtime?.paused ? "Paused" : active ? "Dosing" : schedule.enabled ? `Next: ${date(runtime?.next_ts)}` : "Disabled");
      const pause=row.querySelector('[data-action="pause"]');
      pause.textContent=runtime?.paused ? "Resume" : active ? "Stop / Pause" : "Pause";
      pause.disabled=!runtime || !!runtime.fault;
      row.querySelector('[data-action="reset"]').hidden=!runtime?.fault;
      row.querySelector('[data-action="remove"]').disabled=active || !!runtime?.fault;
      row.querySelector('[data-action="remove"]').title=runtime?.fault ? "Reset the fault before removing this schedule" : "Remove schedule";
      const preview=previews?.schedules.find(item=>item.id===id);
      row.querySelector("[data-preview]").innerHTML=preview ? preview.slots.map(ts=>`<li>${escapeHTML(date(ts))}</li>`).join("") : "";
    }
    const history=doc.getElementById("dosingHistory"), records=model.status.dosing?.recent_runs || [];
    history.innerHTML=records.length ? `<div class="dosing-table"><table><thead><tr><th>Started</th><th>Schedule</th><th>Pump</th><th>Requested ml</th><th>Verified steps</th><th>Status</th></tr></thead><tbody>${records.map(row=>`<tr><td>${escapeHTML(date(row.started_at))}</td><td>${escapeHTML(row.schedule_id)}</td><td>${escapeHTML(row.pump_id)}</td><td>${escapeHTML(row.requested_ml)}</td><td>${escapeHTML(row.confirmed_steps)} / ${escapeHTML(row.requested_steps)}</td><td title="${escapeHTML(row.error)}">${escapeHTML(row.status)}</td></tr>`).join("")}</tbody></table></div>` : '<div class="status-line">No scheduled dosing runs.</div>';
  }
  function render() { renderPumps(); renderSchedules(); updateRuntime(); }
  function statusChanged(status) {
    const focused=pumpRoot.contains(doc.activeElement) || scheduleRoot.contains(doc.activeElement);
    if (model.update(status, focused)) render(); else updateRuntime();
  }
  const input = event => {
    const el=event.target;
    if (el.dataset.section) {
      model.edit(el.dataset.section,el.dataset.id,el.dataset.key,el.type==="checkbox" ? el.checked : el.value);
      if (el.dataset.key === "actuator") {
        const row=el.closest("[data-schedule]");
        row.querySelector('[data-key="enabled"]').disabled=!model.schedules.find(item=>item.id===el.dataset.id).stepper;
      }
      dirty();
    } else if (el.dataset.calibration) {
      const pump=model.pumps.find(item=>item.id===el.dataset.calibration), work=worksheet(pump);
      work[el.dataset.work]=el.value; work.message="";
      if (el.dataset.work !== "volume") { work.operationId=null; work.blocked=true; }
      updateRuntime();
    }
  };
  pumpRoot.addEventListener("input",input); scheduleRoot.addEventListener("input",input);
  // Select controls consistently emit change on older mobile browsers.
  scheduleRoot.addEventListener("change",event=>{ if (event.target.tagName === "SELECT") input(event); });
  const action = async event => {
    const button=event.target.closest("[data-action]"); if (!button) return;
    const id=button.dataset.id, what=button.dataset.action;
    try {
      if (what === "remove") { model.remove(id); renderSchedules(); dirty(); return; }
      if (what === "preview") {
        previews=await post("/api/dosing/preview",{config:model.mergeConfig(model.status.config)});
        message("dosePreviewMessage", "Preview ready."); updateRuntime(); return;
      }
      if (what === "pause") {
        const runtime=model.status.dosing?.schedules.find(item=>item.id===id);
        await post("/api/dosing/pause",{id,paused:!runtime?.paused}); refresh(); return;
      }
      if (what === "reset") { await post("/api/dosing/reset",{id}); refresh(); return; }
      const pump=model.pumps.find(item=>item.id===id), work=worksheet(pump);
      if (what === "stop") { await post("/api/prime",{id,on:false}); work.message="Stop requested."; refresh(); }
      if (what === "calibrate") {
        const steps=number(work.steps,"Test steps",{integer:true}), speed=number(work.speed,"Test speed",{integer:true,max:5000});
        work.volume=""; pumpRoot.querySelectorAll('[data-work="volume"]').forEach(el=>{if(el.dataset.calibration===id)el.value="";});
        button.disabled=true;
        const result=await post("/api/calibration",{id,steps,speed_steps_per_second:speed});
        work.operationId=result.operation_id; work.blocked=false; work.message="";
        model.status={...model.status,stepper_active:id,manual_priming:{...model.status.manual_priming,[id]:true}};
        refresh();
      }
      if (what === "apply") {
        const value=calibratedStepsPerMl(model.status.calibrations?.[id],work.volume,work.operationId);
        model.edit("steppers",id,"steps_per_ml",value);
        for (const el of pumpRoot.querySelectorAll('[data-key="steps_per_ml"]')) if(el.dataset.id===id)el.value=String(value);
        work.message=`${value.toFixed(3)} steps/ml staged for Save Config.`; dirty();
      }
      updateRuntime();
    } catch(error) {
      if (id && ["calibrate","apply","stop"].includes(what)) { worksheet(model.pumps.find(p=>p.id===id)).message=error.message; updateRuntime(); }
      else message("dosePreviewMessage",error.message,true);
      message("configMessage",error.message,true);
    } finally { button.disabled=false; updateRuntime(); }
  };
  pumpRoot.addEventListener("click",action); scheduleRoot.addEventListener("click",action);
  doc.getElementById("addDoseSchedule").onclick=()=>{model.add(); renderSchedules(); dirty();};
  doc.addEventListener("loggerhead:status",event=>statusChanged(event.detail));
  doc.addEventListener("loggerhead:config-saved",event=>{model.accept(event.detail); for(const work of worksheets.values())if(work.message.includes("staged"))work.message="Calibration saved."; previews=null; render();});
  doc.addEventListener("loggerhead:config-canceled",()=>{model.accept(bridge.getStatus().config); for(const work of worksheets.values())work.message=""; previews=null; render();});
  if (!doc.getElementById("dosing-styles")) {
    const style=doc.createElement("style"); style.id="dosing-styles";
    style.textContent=`#pumpConfig h3{font-size:16px;margin:0 0 8px}#pumpConfig input,#dosingSchedules input,#dosingSchedules select{width:100%;min-width:0}#dosingSchedules input[type=checkbox]{width:18px}.calibration{min-width:0;border:0;border-top:1px solid var(--line);padding:12px 0 0;margin:14px 0 0}.calibration legend{font-size:14px;padding:0 8px 0 0}.calibration .toolbar{gap:6px;margin-top:10px}.calibration button{font-size:12px;padding:8px}.dosing-schedule{border-top:1px solid var(--line);padding:16px 0}.dosing-schedule .toolbar{margin-top:8px}.dose-preview{display:flex;flex-wrap:wrap;gap:8px 16px;list-style:none;padding:0;font-size:12px;color:var(--muted)}.dosing-table{overflow:auto;max-width:100%}.dosing-table table{width:100%;border-collapse:collapse;font-size:12px}.dosing-table th,.dosing-table td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}button:disabled{opacity:.45;cursor:default}#pumpConfig .message{overflow-wrap:anywhere}`;
    doc.head.append(style);
  }
  const initial=bridge.getStatus(); if (initial.config) statusChanged(initial);
  return {model,mergeConfig:payload=>model.mergeConfig(payload),statusChanged};
}

if (typeof window !== "undefined" && document.getElementById("pumpConfig")) {
  const post=async(path,payload)=>{
    const response=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    const body=await response.json().catch(()=>({}));
    if(!response.ok || body.ok===false)throw new Error(body.message || `Request failed (${response.status}).`);
    return body;
  };
  window.loggerheadDosing=mountDosingEditor(document,window.loggerheadConfigBridge,post);
}
