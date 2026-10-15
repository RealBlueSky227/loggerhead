// Simulation-only browser integration: all mutating requests are intercepted.
import assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {createRequire} from "node:module";
import {mkdir} from "node:fs/promises";
import path from "node:path";
import {fileURLToPath} from "node:url";
import {createInterface} from "node:readline";

const require = createRequire(import.meta.url);
const {chromium} = require(process.env.LOGGERHEAD_PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const python = process.env.LOGGERHEAD_PYTHON || path.join(root, process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python");
const server = spawn(python, ["scripts/benchmark_plots.py", "--serve", "--rows", "600"], {
  cwd:root, env:{...process.env, PYTHONPATH:root}, stdio:["pipe","pipe","pipe"], windowsHide:true,
});
let browser;
try {
  const address = await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error("Fixture server startup timed out")),15000);
    server.on("exit",code=>{clearTimeout(timer);reject(new Error(`Server exited: ${code}`));});
    server.stderr.on("data",data=>process.stderr.write(data));
    createInterface({input:server.stdout}).on("line",line=>{
      if(line.startsWith("{")){clearTimeout(timer);resolve(JSON.parse(line));}
    });
  });
  browser=await chromium.launch({headless:true,...(process.env.LOGGERHEAD_BROWSER_EXECUTABLE ? {executablePath:process.env.LOGGERHEAD_BROWSER_EXECUTABLE} : {})});
  const page=await browser.newPage({viewport:{width:1440,height:1000}}), errors=[], writes=[];
  page.on("pageerror",error=>errors.push(error.message));
  const base=`http://127.0.0.1:${address.port}`;
  const snapshot=await (await page.request.get(base+"/api/status")).json();
  snapshot.config.dosing=[]; snapshot.dosing={schedules:[],recent_runs:[],active:null,timezone:"UTC"};
  snapshot.calibrations={}; snapshot.manual_priming={}; snapshot.stepper_active=null;
  let failSave=true, operation=0;
  await page.route("**/api/**",async route=>{
    const request=route.request(), url=new URL(request.url());
    if(url.pathname==="/api/status")return route.fulfill({json:snapshot});
    if(request.method()!=="POST")return route.continue();
    const payload=request.postDataJSON(); writes.push({path:url.pathname,payload});
    if(url.pathname==="/api/config"){
      if(failSave)return route.fulfill({status:503,json:{ok:false,message:"write failed"}});
      snapshot.config=payload; snapshot.steppers=payload.steppers;
      snapshot.sense_ports=payload.sense_ports;
      snapshot.dosing.schedules=payload.dosing.map(row=>({id:row.id,paused:false,fault:"",next_ts:Date.now()/1000+3600}));
      return route.fulfill({json:{ok:true,config:payload,saved_at:Date.now()/1000}});
    }
    if(url.pathname==="/api/calibration"){
      operation++;
      snapshot.calibrations[payload.id]={operation_id:`run-${operation}`,state:"completed",reason:"max_steps",
        confirmed_steps:payload.steps,requested_steps:payload.steps,speed_steps_per_second:payload.speed_steps_per_second};
      return route.fulfill({status:202,json:{ok:true,operation_id:`run-${operation}`}});
    }
    if(url.pathname==="/api/dosing/preview")return route.fulfill({json:{ok:true,timezone:"UTC",schedules:payload.config.dosing.map(row=>({id:row.id,slots:[Date.now()/1000+3600,Date.now()/1000+7200]}))}});
    if(url.pathname==="/api/dosing/pause"){
      snapshot.dosing.schedules.find(row=>row.id===payload.id).paused=payload.paused;
      return route.fulfill({json:{ok:true}});
    }
    throw new Error(`Unexpected mutation: ${url.pathname}`);
  });
  await page.goto(base);
  await page.waitForSelector("#heartbeatDot.ok");
  await page.getByRole("button",{name:"Config",exact:true}).click();
  const pump=page.locator('[data-pump="dose1"]');
  await pump.getByLabel("Dose speed (step/s)",{exact:true}).fill("1000");
  await pump.getByLabel("Pump name",{exact:true}).fill("Alkalinity");
  await pump.getByLabel("Pump name",{exact:true}).evaluate(el=>el.dataset.identity="stable");
  await page.waitForTimeout(4300);
  assert.equal(await pump.getByLabel("Pump name",{exact:true}).inputValue(),"Alkalinity");
  assert.equal(await pump.getByLabel("Pump name",{exact:true}).getAttribute("data-identity"),"stable");
  assert.equal(await pump.getByLabel("Pump name",{exact:true}).evaluate(el=>document.activeElement===el),true);
  await page.locator("#saveConfig").click();
  await page.waitForFunction(()=>document.getElementById("configMessage").textContent.includes("write failed"));
  assert.equal(await pump.getByLabel("Pump name",{exact:true}).inputValue(),"Alkalinity");
  failSave=false;
  await page.locator("#saveConfig").click();
  await page.waitForFunction(()=>document.getElementById("configMessage").textContent.includes("Config saved at"));
  assert.equal(snapshot.config.steppers[0].dose_speed_steps_per_second,1000);
  assert.equal(snapshot.config.steppers[0].name,"Alkalinity");

  await pump.getByLabel("Test steps",{exact:true}).fill("800");
  await pump.getByLabel("Test speed (step/s)",{exact:true}).fill("1000");
  assert.equal(await pump.getByRole("button",{name:"Apply Calibration",exact:true}).isEnabled(),false);
  await pump.getByRole("button",{name:"Run Calibration",exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('[data-cal-message="dose1"]').textContent.includes("800 / 800"));
  await pump.getByLabel("Measured volume (ml)",{exact:true}).fill("2");
  await pump.getByRole("button",{name:"Apply Calibration",exact:true}).click();
  assert.equal(await pump.getByLabel("Calibration (steps/ml)",{exact:true}).inputValue(),"400");
  assert.match(await pump.locator("[data-cal-message]").textContent(),/staged/);
  assert.equal(snapshot.config.steppers[0].steps_per_ml,800);
  await page.locator("#saveConfig").click();
  await page.waitForFunction(()=>document.querySelector('[data-cal-message="dose1"]').textContent.includes("Calibration saved"));
  assert.equal(snapshot.config.steppers[0].steps_per_ml,400);

  await page.locator("#addDoseSchedule").click();
  const schedule=page.locator("[data-schedule]").first();
  assert.equal(await schedule.getByLabel("Enabled",{exact:true}).isChecked(),false);
  await schedule.getByLabel("Schedule name",{exact:true}).fill("Alkalinity daily");
  await schedule.getByLabel("Daily volume (ml)",{exact:true}).fill("12");
  await schedule.getByLabel("Doses per day",{exact:true}).fill("3");
  await schedule.getByLabel("Window start",{exact:true}).fill("22:00");
  await schedule.getByLabel("Window end",{exact:true}).fill("06:00");
  await schedule.getByLabel("Enabled",{exact:true}).check();
  await page.waitForTimeout(4300);
  assert.equal(await schedule.getByLabel("Daily volume (ml)",{exact:true}).inputValue(),"12");
  await page.locator("#dosingSchedules").getByRole("button",{name:"Preview",exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll(".dose-preview li").length===2);
  await page.locator("#saveConfig").click();
  await page.waitForFunction(()=>document.querySelector("[data-runtime]").textContent.includes("Next:"));
  assert.equal(snapshot.config.dosing[0].daily_volume_ml,12);
  assert.equal(snapshot.config.dosing[0].doses_per_day,3);
  assert.equal(snapshot.config.dosing[0].window_end,"06:00");
  assert.equal(snapshot.config.dosing[0].enabled,true);
  await schedule.getByRole("button",{name:"Pause",exact:true}).click();
  await page.waitForFunction(()=>document.querySelector("[data-runtime]").textContent==="Paused");
  await schedule.getByRole("button",{name:"Resume",exact:true}).click();
  await page.waitForFunction(()=>document.querySelector("[data-runtime]").textContent.includes("Next:"));
  await schedule.getByLabel("Daily volume (ml)",{exact:true}).fill("99");
  await pump.getByLabel("Pump name",{exact:true}).fill("Discard me");
  await page.locator("#cancelConfig").click();
  assert.equal(await pump.getByLabel("Pump name",{exact:true}).inputValue(),"Alkalinity");
  assert.equal(await schedule.getByLabel("Daily volume (ml)",{exact:true}).inputValue(),"12");

  snapshot.calibrations.dose1={...snapshot.calibrations.dose1,state:"incomplete",confirmed_steps:0,reason:"stopped"};
  await page.waitForTimeout(2300);
  await pump.getByLabel("Measured volume (ml)",{exact:true}).fill("2");
  assert.equal(await pump.getByRole("button",{name:"Apply Calibration",exact:true}).isEnabled(),false);
  const screenshots=process.env.LOGGERHEAD_SCREENSHOT_DIR;
  if(screenshots){await mkdir(screenshots,{recursive:true});await page.screenshot({path:path.join(screenshots,"dosing-desktop.png"),fullPage:true});}
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.setViewportSize({width:390,height:844});
  if(screenshots)await page.screenshot({path:path.join(screenshots,"dosing-mobile.png"),fullPage:true});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,
    JSON.stringify(await page.evaluate(()=>[...document.querySelectorAll("#config *,header *")].filter(el=>el.getBoundingClientRect().right>innerWidth).map(el=>({tag:el.tagName,class:el.className,width:el.getBoundingClientRect().width})))));
  if(screenshots){
    await pump.scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(screenshots,"dosing-mobile-pump.png")});
    await schedule.scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(screenshots,"dosing-mobile-schedule.png")});
  }
  assert.deepEqual(errors,[]);
  assert.equal(writes.filter(row=>row.path==="/api/calibration").length,1);
  console.log("Dosing browser checks passed: stable drafts, save errors, unified save/cancel, calibration provenance, schedule CRUD/preview/pause, desktop/mobile. Simulation only.");
} finally {
  await browser?.close();
  if(server.exitCode===null){
    const exited=new Promise(resolve=>server.once("exit",resolve));
    server.stdin.on("error",()=>{});server.stdin.end("shutdown\n");
    const timer=setTimeout(()=>server.kill(),3000);try{await exited;}finally{clearTimeout(timer);}
  }
}
