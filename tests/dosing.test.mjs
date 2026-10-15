import assert from "node:assert/strict";
import test from "node:test";
import {calibratedStepsPerMl, DosingModel, validateDosingConfig} from "../loggerhead/static/dosing.mjs";

const pump = {id:"dose1",name:"Alk",assignment:"dose1",steps_per_ml:800,manual_speed_steps_per_second:400,
  dose_speed_steps_per_second:1000,manual_max_seconds:60,manual_max_steps:300000,microsteps:16,run_current_ma:700};
const schedule = {id:"alk",name:"Alk",actuator:"dose1",stepper:true,enabled:false,daily_volume_ml:24,doses_per_day:12,
  window_start:"08:00",window_end:"20:00",max_dose_ml:10,max_run_seconds:120,calibration_ml_per_minute:30};
const config = () => structuredClone({steppers:[pump],dosing:[schedule],sense_ports:[{number:1,device:"ds18b20"}],mqtt:{enabled:false}});

test("polling and focused inputs preserve unsaved pump/schedule drafts",()=>{
  const model=new DosingModel(); model.update({config:config()});
  model.edit("steppers","dose1","name","Alkalinity");
  model.edit("dosing","alk","daily_volume_ml","36");
  for(let i=0;i<5;i++)assert.equal(model.update({config:config()}),false);
  const merged=model.mergeConfig(config());
  assert.equal(merged.steppers[0].name,"Alkalinity");
  assert.equal(merged.dosing[0].daily_volume_ml,36);
  const clean=new DosingModel();clean.update({config:config()});
  const remote=config();remote.steppers[0].name="Remote";
  assert.equal(clean.update({config:remote},true),false);
  assert.equal(clean.pumps[0].name,"Alk");
});

test("unified merge preserves raw edits and unknown untouched fields",()=>{
  const model=new DosingModel(); model.update({config:config()});
  model.edit("steppers","dose1","manual_speed_steps_per_second","1000");
  const raw=config(); raw.mqtt.enabled=true;raw.steppers[0].hold_current_ma=25;
  raw.dosing[0].calibration_ml_per_minute=42;
  const merged=model.mergeConfig(raw);
  assert.equal(merged.steppers[0].manual_speed_steps_per_second,1000);
  assert.equal(merged.steppers[0].hold_current_ma,25);
  assert.equal(merged.dosing[0].calibration_ml_per_minute,42);
  assert.equal(merged.mqtt.enabled,true);
  assert.deepEqual(merged.sense_ports,raw.sense_ports);
  model.accept(merged); assert.equal(model.dirty,false);
});

test("schedule CRUD has unique IDs and disabled safe defaults",()=>{
  const model=new DosingModel();model.update({config:config()});
  const first=model.add(), second=model.add(); assert.notEqual(first,second);
  assert.equal(model.schedules.at(-1).enabled,false);
  model.remove(first);model.remove("alk");
  const merged=model.mergeConfig(config());
  assert.deepEqual(merged.dosing.map(s=>s.id),[second]);
});

test("calibration requires positive measured volume and exact matching verified run",()=>{
  const record={operation_id:"known",state:"completed",reason:"max_steps",requested_steps:8000,confirmed_steps:8000};
  assert.equal(calibratedStepsPerMl(record,"10","known"),800);
  for(const volume of [0,-1,"",NaN,Infinity])assert.throws(()=>calibratedStepsPerMl(record,volume,"known"));
  assert.throws(()=>calibratedStepsPerMl({...record,state:"incomplete"},10,"known"));
  assert.throws(()=>calibratedStepsPerMl({...record,confirmed_steps:7999},10,"known"));
  assert.throws(()=>calibratedStepsPerMl(record,10,"old"));
});

test("schedule validation enforces caps, dose speeds and calibrated sub-step bounds",()=>{
  const valid=config();valid.dosing[0].enabled=true;
  assert.equal(validateDosingConfig(valid).dosing[0].enabled,true);
  for(const mutate of [
    c=>{c.steppers[0].dose_speed_steps_per_second=5001;},
    c=>{c.dosing[0].window_start="25:00";},
    c=>{c.dosing[0].max_dose_ml=0.1;},
    c=>{c.dosing[0].max_run_seconds=1;},
    c=>{c.dosing[0].doses_per_day=1.5;},
    c=>{c.dosing.push({...c.dosing[0],id:"second"});},
    c=>{c.steppers[0].steps_per_ml=0.1;},
  ]) {const next=structuredClone(valid);mutate(next);assert.throws(()=>validateDosingConfig(next));}
});

test("raw/editor deletion conflicts report an error without losing edits",()=>{
  const model=new DosingModel();model.update({config:config()});
  model.edit("steppers","dose1","name","Edited");
  const raw=config();raw.steppers=[];
  assert.throws(()=>model.mergeConfig(raw),/remove/);
  assert.equal(model.pumps[0].name,"Edited");
});
