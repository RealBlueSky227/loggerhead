import assert from "node:assert/strict";
import test from "node:test";
import {ATOModel, validateATO, waterSensors, levelsFor} from "../loggerhead/static/ato.mjs";

const config=()=>({sense_ports:[{number:1,name:"Sump",device:"hydros_triple"},{number:2,name:"Overflow",device:"binary"}],
  equipment:[{id:"ac1",name:"Refill",driver:"mcp23017_relay",pin_or_outlet:"AC1"}],
  steppers:[{id:"dose1",name:"Water pump",assignment:"dose1",steps_per_ml:800}],dosing:[],
  ato:[{id:"ato",name:"Top Off",primary_level_sensor:"sense_port_1_water",backup_failsafe_sensor:"sense_port_2_water",
    assigned_actuator:"ac1",actuator_device_type:"relay",enabled:false,max_run_time:60,max_refill_ml:0,max_daily_ml:0}],unknown:{keep:true}});

test("catalog uses stable physical-port water sensor IDs and binary thresholds",()=>{
  assert.deepEqual(waterSensors(config()).map(row=>row.id),["sense_port_1_water","sense_port_2_water"]);
  assert.deepEqual(levelsFor(waterSensors(config())[1]),["dry","submerged"]);
});
test("ATO drafts preserve focused edits across polling and merge unrelated/raw fields",()=>{
  const model=new ATOModel(), original=config();model.accept(original);
  model.edit("ato","name","Fresh water");model.edit("ato","max_run_time","90");
  for(let i=0;i<10;i++)assert.equal(model.update(original),false);
  const raw=config();raw.unknown.extra="raw";raw.ato[0].max_daily_ml=0;
  const saved=model.mergeConfig(raw);
  assert.equal(saved.ato[0].name,"Fresh water");assert.equal(saved.ato[0].max_run_time,90);
  assert.deepEqual(saved.unknown,{keep:true,extra:"raw"});assert.equal(original.ato[0].name,"Top Off");
  model.accept(original);assert.equal(model.dirty,false);assert.equal(model.rows[0].name,"Top Off");
});
test("new ATO is disabled; add/delete and cancel retain stable IDs",()=>{
  const model=new ATOModel();model.accept(config());model.add();const added=model.rows.at(-1);
  assert.equal(added.enabled,false);assert.notEqual(added.id,"ato");model.remove(added.id);
  assert.equal(model.mergeConfig(config()).ato.length,1);
  model.remove("ato");assert.deepEqual(model.mergeConfig(config()).ato,[]);
  model.accept(config());assert.equal(model.rows.length,1);
});
test("thresholds, independent backup, references, and finite limits are validated",()=>{
  for(const changes of [{start_level:"normal",stop_level:"low"},{backup_failsafe_sensor:"sense_port_1_water"},
    {max_run_time:"NaN"},{max_run_time:0},{assigned_actuator:"missing"},{max_refill_ml:1}]){
    const candidate=config();Object.assign(candidate.ato[0],changes);assert.throws(()=>validateATO(candidate));
  }
});
test("stepper requires volume limits and rejects enabled chemical dosing conflict",()=>{
  const candidate=config();Object.assign(candidate.ato[0],{actuator_device_type:"stepper",assigned_actuator:"dose1",max_refill_ml:10,max_daily_ml:100,enabled:true});
  assert.equal(validateATO(candidate).ato[0].max_daily_ml,100);
  candidate.dosing=[{actuator:"dose1",enabled:true}];assert.throws(()=>validateATO(candidate),/chemical/);
  candidate.dosing=[];candidate.ato[0].max_daily_ml=5;assert.throws(()=>validateATO(candidate),/limits/);
});
test("raw removal conflict does not discard unsaved ATO edits",()=>{
  const model=new ATOModel();model.accept(config());model.edit("ato","name","Draft");const raw=config();raw.ato=[];
  assert.throws(()=>model.mergeConfig(raw),/Raw config removed/);assert.equal(model.rows[0].name,"Draft");
});
