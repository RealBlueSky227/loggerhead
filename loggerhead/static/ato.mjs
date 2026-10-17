const clone = value => JSON.parse(JSON.stringify(value));
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const DEFAULTS = {enabled:false, start_level:"dry", stop_level:null, max_run_time:60, max_refill_ml:0, max_daily_ml:0};

export function waterSensors(config) {
  return [...(config.water_level_sensors || []), ...(config.sense_ports || [])
    .filter(port => ["binary", "hydros_triple"].includes(port.device))
    .map(port => ({id:`sense_port_${port.number}_water`, name:port.name || `Sense Port ${port.number}`,
      sense_port:port.number, driver:port.device === "binary" ? "binary" : "hydros_triple_pwm"}))];
}
export const levelsFor = sensor => sensor?.driver === "binary" ? ["dry", "submerged"] : ["dry", "low", "normal", "high"];

export function validateATO(config) {
  const sensors = waterSensors(config), used = new Set(), ids = new Set();
  for (const profile of config.ato || []) {
    if (!profile.id || ids.has(profile.id) || !profile.name?.trim()) throw new Error("ATO requires a name and unique ID.");
    ids.add(profile.id);
    const primary = sensors.find(s => s.id === profile.primary_level_sensor), backup = sensors.find(s => s.id === profile.backup_failsafe_sensor);
    if (!primary || !backup) throw new Error("Select both configured water-level sensors.");
    if (primary.sense_port === backup.sense_port) throw new Error("Primary and backup must use different physical SensePorts.");
    const levels = levelsFor(primary), start = profile.start_level || "dry", stop = profile.stop_level || levels[1];
    if (!levels.includes(start) || !levels.includes(stop) || levels.indexOf(start) >= levels.indexOf(stop)) throw new Error("Start threshold must be below the stop threshold.");
    for (const key of ["max_run_time", "max_refill_ml", "max_daily_ml"]) {
      const value = Number(profile[key] ?? 0);
      if (!Number.isFinite(value) || value < 0 || (key === "max_run_time" && (value <= 0 || value > 3600))) throw new Error("ATO limits must be finite; runtime must be between 0 and 3600 seconds.");
      profile[key] = value;
    }
    const stepper = profile.actuator_device_type === "stepper";
    const device = (stepper ? config.steppers : config.equipment)?.find(item => item.id === profile.assigned_actuator);
    if (!device) throw new Error("Select a refill device matching its type.");
    const dormant=!profile.enabled && !profile.max_refill_ml && !profile.max_daily_ml;
    if (stepper && !dormant && (profile.max_refill_ml <= 0 || profile.max_daily_ml < profile.max_refill_ml || !(device.steps_per_ml > 0))) throw new Error("Stepper ATO needs calibration and positive refill/daily volume limits.");
    if (!stepper && (profile.max_refill_ml || profile.max_daily_ml)) throw new Error("Volume limits require a calibrated stepper.");
    if (profile.enabled) {
      const key = stepper ? `stepper:${device.assignment}` : `${device.driver}:${device.kasa_host}:${device.pin_or_outlet}`;
      if (used.has(key)) throw new Error("Enabled ATO profiles cannot share a refill device.");
      used.add(key);
      if ((config.dosing || []).some(d => d.enabled && d.actuator === profile.assigned_actuator)) throw new Error("ATO and chemical dosing cannot share an enabled pump.");
    }
  }
  return config;
}

export class ATOModel {
  constructor() { this.rows=[]; this.edits=new Map(); this.added=new Set(); this.removed=new Set(); this.hash=""; }
  get dirty() { return !!(this.edits.size || this.added.size || this.removed.size); }
  accept(config) { this.rows=clone(config.ato || []).map(row=>({...DEFAULTS,...row})); this.edits.clear(); this.added.clear(); this.removed.clear(); this.hash=JSON.stringify(config.ato); }
  update(config, focused=false) { if (this.dirty || focused || this.hash===JSON.stringify(config.ato)) return false; this.accept(config); return true; }
  edit(id, key, value) {
    const row=this.rows.find(item=>item.id===id); row[key]=value;
    if (!this.edits.has(id)) this.edits.set(id,new Set());
    this.edits.get(id).add(key);
  }
  add() {
    const id=globalThis.crypto?.randomUUID?.() || `ato-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    this.rows.push({...DEFAULTS,id,name:"New ATO",primary_level_sensor:"",backup_failsafe_sensor:"",actuator_device_type:"relay",assigned_actuator:""}); this.added.add(id);
  }
  remove(id) { this.rows=this.rows.filter(row=>row.id!==id); this.removed.add(id); this.added.delete(id); this.edits.delete(id); }
  mergeConfig(payload) {
    const config=clone(payload);
    config.ato=(config.ato || []).filter(row=>!this.removed.has(row.id)).map(row=>{
      const draft=this.rows.find(item=>item.id===row.id);
      for (const key of this.edits.get(row.id) || []) row[key]=draft[key];
      return row;
    });
    for (const id of this.edits.keys()) if (!this.added.has(id) && !config.ato.some(row=>row.id===id)) throw new Error("Raw config removed an ATO with unsaved editor changes.");
    for (const row of this.rows) if(this.added.has(row.id))config.ato.push(clone(row));
    return validateATO(config);
  }
}

export function mountATO(doc, bridge, post) {
  const root=doc.getElementById("atoConfig"), dashboard=doc.getElementById("atoOverview"), history=doc.getElementById("atoHistory");
  if (!root) return null;
  const model=new ATOModel(); let status={}, catalogHash="";
  const feedback=(text,error=false)=>{for(const id of ["atoMessage","atoConfigMessage"]){const el=doc.getElementById(id);if(el){el.textContent=text;el.className=`message ${error ? "error" : "ok"}`;}}};
  const dirty=()=>{const el=doc.getElementById("configMessage");el.textContent="Unsaved ATO changes.";el.className="message";};
  const options=(items,value)=>`<option value="">Select</option>`+items.map(([id,name])=>`<option value="${esc(id)}" ${id===value ? "selected" : ""}>${esc(name)}</option>`).join("")+
    (value && !items.some(([id])=>id===value) ? `<option value="${esc(value)}" selected>Unavailable: ${esc(value)}</option>` : "");
  const select=(row,key,label,items)=>`<label><span>${label}</span><select aria-label="${label}" data-key="${key}" data-id="${esc(row.id)}">${options(items,row[key])}</select></label>`;
  const field=(row,key,label,type="number")=>`<label><span>${label}</span><input aria-label="${label}" data-key="${key}" data-id="${esc(row.id)}" type="${type}" ${type==="number" ? 'step="any" min="0"' : ""} value="${esc(row[key])}"></label>`;
  function render() {
    const config=status.config || {}, sensors=waterSensors(config);
    root.innerHTML=model.rows.map(row=>{
      const levels=levelsFor(sensors.find(sensor=>sensor.id===row.primary_level_sensor));
      const shown={...row,stop_level:row.stop_level || levels[1]};
      const devices=row.actuator_device_type==="stepper" ? config.steppers : config.equipment;
      return `<div class="ato-profile" data-ato="${esc(row.id)}"><div class="field-grid">
        ${field(row,"name","ATO name","text")}<label><span>Enabled</span><input type="checkbox" aria-label="ATO enabled" data-key="enabled" data-id="${esc(row.id)}" ${row.enabled ? "checked" : ""}></label>
        ${select(row,"primary_level_sensor","Primary sensor",sensors.map(s=>[s.id,`${s.name} (Sense Port ${s.sense_port})`]))}
        ${select(row,"backup_failsafe_sensor","Backup high-water sensor",sensors.map(s=>[s.id,`${s.name} (Sense Port ${s.sense_port})`]))}
        ${select(row,"actuator_device_type","Refill device type",[["relay","Relay pump"],["stepper","Stepper pump"]])}
        ${select(row,"assigned_actuator","Refill device",(devices || []).map(d=>[d.id,d.name]))}
        ${select(shown,"start_level","Start at or below",levels.slice(0,-1).map(level=>[level,level.toUpperCase()]))}
        ${select(shown,"stop_level","Stop at or above",levels.slice(1).map(level=>[level,level.toUpperCase()]))}
        ${field(row,"max_run_time","Maximum refill runtime (s)")}
        ${row.actuator_device_type==="stepper" ? field(row,"max_refill_ml","Maximum refill (ml)")+field(row,"max_daily_ml","Maximum rolling 24h volume (ml)") : ""}
        </div><div class="status-line" data-ato-runtime="${esc(row.id)}"></div><div class="toolbar"><button data-ato-reset="${esc(row.id)}">Reset Fault</button><button data-ato-remove="${esc(row.id)}" class="hollow">Delete ATO</button></div></div>`;
    }).join("");
    runtime();
  }
  function runtime() {
    const profiles=status.ato_runtime?.profiles || {};
    for(const row of root.querySelectorAll("[data-ato]")) {
      const state=profiles[row.dataset.ato];
      row.querySelector("[data-ato-runtime]").textContent=state ? `${state.status}${state.fault_reason ? `: ${state.fault_reason}` : state.waiting_reason ? `: ${state.waiting_reason}` : ""}` : "Unsaved ATO";
      row.querySelector("[data-ato-reset]").disabled=!state?.faulted || !!state?.running;
      row.querySelector("[data-ato-remove]").disabled=!!state?.faulted || !!state?.running;
    }
    dashboard.innerHTML=(status.config?.ato || []).map(profile=>{
      const state=profiles[profile.id] || {}, sensor=waterSensors(status.config).find(s=>s.id===profile.primary_level_sensor);
      const device=[...(status.config.equipment || []),...(status.config.steppers || [])].find(d=>d.id===profile.assigned_actuator);
      return `<div class="card"><div class="label">${esc(profile.name)} | ${profile.enabled ? "Enabled" : "Disabled"}</div><div class="value ${state.faulted ? "hot" : ""}">${esc(state.status || "IDLE")}</div>
        <div class="status-line">Primary: ${esc(state.primary || "unknown")} | Backup: ${esc(state.backup || "unknown")}</div>
        <div class="status-line">Start: ${esc(profile.start_level || "dry")} | Stop: ${esc(profile.stop_level || levelsFor(sensor)[1])}</div>
        <div class="status-line">${esc(device?.name || profile.assigned_actuator)} | ${Number(state.elapsed_seconds || 0).toFixed(1)} s${profile.actuator_device_type==="stepper" ? ` | ${Number(state.dispensed_ml || 0).toFixed(3)} ml estimated` : ""}</div>
        <div class="status-line">${esc(state.fault_reason || state.waiting_reason || state.stop_reason || "")}</div>${state.faulted ? `<button data-ato-reset="${esc(profile.id)}" ${state.running ? "disabled" : ""}>Reset Fault</button>` : ""}</div>`;
    }).join("");
    const records=status.ato_runtime?.recent_runs || [];
    history.innerHTML=records.length ? `<div class="ato-table"><table><thead><tr><th>Started</th><th>ATO</th><th>Duration</th><th>Estimated ml</th><th>Possible ml</th><th>Stop reason</th></tr></thead><tbody>${records.map(row=>`<tr><td>${esc(new Date(row.started_at*1000).toLocaleString())}</td><td>${esc(row.profile_id)}</td><td>${Number(row.duration).toFixed(1)} s</td><td>${row.steps_per_ml ? (row.confirmed_steps/row.steps_per_ml).toFixed(3) : "N/A"}</td><td>${row.steps_per_ml ? Number(row.charged_ml).toFixed(3) : "N/A"}</td><td>${esc(row.reason)}</td></tr>`).join("")}</tbody></table></div>` : '<div class="status-line">No refill history.</div>';
  }
  root.addEventListener("input", event=>{
    const el=event.target; if(!el.dataset.key)return;
    model.edit(el.dataset.id,el.dataset.key,el.type==="checkbox" ? el.checked : el.value); dirty();
  });
  root.addEventListener("change",event=>{
    const el=event.target; if(el.tagName!=="SELECT")return;
    model.edit(el.dataset.id,el.dataset.key,el.value);
    if(el.dataset.key==="actuator_device_type") {
      model.edit(el.dataset.id,"assigned_actuator","");
      if(el.value==="relay")for(const key of ["max_refill_ml","max_daily_ml"])model.edit(el.dataset.id,key,0);
    }
    if(el.dataset.key==="primary_level_sensor") {model.edit(el.dataset.id,"start_level","dry");model.edit(el.dataset.id,"stop_level",null);}
    dirty(); if(["actuator_device_type","primary_level_sensor"].includes(el.dataset.key))render();
  });
  const action=async event=>{
    const button=event.target.closest("[data-ato-reset],[data-ato-remove]");if(!button)return;
    if(button.dataset.atoRemove){model.remove(button.dataset.atoRemove);render();dirty();return;}
    button.disabled=true;
    try{await post("/api/ato/reset",{id:button.dataset.atoReset});feedback("ATO fault reset accepted.");doc.dispatchEvent(new Event("loggerhead:refresh-request"));}
    catch(error){feedback(error.message,true);}
    finally{button.disabled=false;}
  };
  root.addEventListener("click",action);dashboard.addEventListener("click",action);
  doc.getElementById("addATO").onclick=()=>{model.add();render();dirty();};
  function update(next) {
    status=next; const hash=JSON.stringify([next.config?.sense_ports,next.config?.water_level_sensors,next.config?.equipment,next.config?.steppers]);
    const changed=model.update(next.config,root.contains(doc.activeElement));
    if(changed || (!model.dirty && !root.contains(doc.activeElement) && hash!==catalogHash)){catalogHash=hash;render();}else runtime();
  }
  doc.addEventListener("loggerhead:status",event=>update(event.detail));
  doc.addEventListener("loggerhead:config-saved",event=>{model.accept(event.detail);status={...status,config:event.detail};render();});
  doc.addEventListener("loggerhead:config-canceled",()=>{model.accept(bridge.getStatus().config);render();feedback("");});
  if(bridge.getStatus().config)update(bridge.getStatus());
  return {model,mergeConfig:payload=>model.mergeConfig(payload)};
}

if(typeof window!=="undefined") {
  const post=async(path,payload)=>{
    const response=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    const body=await response.json();if(!response.ok || body.ok===false)throw new Error(body.message || `Request failed (${response.status}).`);return body;
  };
  window.loggerheadATO=mountATO(document,window.loggerheadConfigBridge,post);
}
