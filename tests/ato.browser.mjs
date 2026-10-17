// All writes are intercepted against a simulation-only local fixture.
import assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {createRequire} from "node:module";
import {mkdir} from "node:fs/promises";
import path from "node:path";
import {fileURLToPath} from "node:url";
import {createInterface} from "node:readline";

const require=createRequire(import.meta.url), engines=require(process.env.LOGGERHEAD_PLAYWRIGHT_MODULE || "playwright");
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),"..");
const python=process.env.LOGGERHEAD_PYTHON || path.join(root,process.platform==="win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python");
const server=spawn(python,["scripts/benchmark_plots.py","--serve","--rows","600"],{cwd:root,env:{...process.env,PYTHONPATH:root},stdio:["pipe","pipe","pipe"],windowsHide:true});
let browser;
try {
  const address=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error("Fixture timed out")),15000);
    server.on("exit",code=>{clearTimeout(timer);reject(new Error(`Fixture exited ${code}`));});
    server.stderr.on("data",data=>process.stderr.write(data));
    createInterface({input:server.stdout}).on("line",line=>{if(line.startsWith("{")){clearTimeout(timer);resolve(JSON.parse(line));}});
  });
  const engine=process.env.LOGGERHEAD_BROWSER_ENGINE || "chromium";
  browser=await engines[engine].launch({headless:true,...(engine==="chromium" && process.env.LOGGERHEAD_BROWSER_EXECUTABLE ? {executablePath:process.env.LOGGERHEAD_BROWSER_EXECUTABLE} : {})});
  const page=await browser.newPage({viewport:{width:1400,height:1000}}), errors=[], writes=[];
  page.on("pageerror",error=>errors.push(error.message));
  const base=`http://127.0.0.1:${address.port}`;
  const snapshot=await (await page.request.get(base+"/api/status")).json();
  snapshot.config.ato=[];
  for(const port of snapshot.config.sense_ports.slice(0,2))port.device="hydros_triple";
  snapshot.ato_runtime={profiles:{},recent_runs:[]};
  let failSave=false, failReset=true;
  await page.route("**/api/**",async route=>{
    const request=route.request(), url=new URL(request.url());
    if(url.pathname==="/api/status")return route.fulfill({json:snapshot});
    if(request.method()!=="POST")return route.continue();
    const payload=request.postDataJSON();writes.push({path:url.pathname,payload});
    if(url.pathname==="/api/config") {
      if(failSave)return route.fulfill({status:503,json:{ok:false,message:"Storage unavailable"}});
      snapshot.config=payload;snapshot.sense_ports=payload.sense_ports;
      for(const p of payload.ato)snapshot.ato_runtime.profiles[p.id]={status:p.enabled ? "IDLE" : "DISABLED",primary:"normal",backup:"dry",faulted:false};
      return route.fulfill({json:{ok:true,config:payload,saved_at:Date.now()/1000}});
    }
    if(url.pathname==="/api/ato/reset") {
      if(failReset)return route.fulfill({status:409,json:{ok:false,message:"Backup high-water failsafe is still active."}});
      snapshot.ato_runtime.profiles[payload.id].faulted=false;
      snapshot.ato_runtime.profiles[payload.id].fault_reason="";
      snapshot.ato_runtime.profiles[payload.id].status="IDLE";
      return route.fulfill({json:{ok:true}});
    }
    throw new Error(`Unexpected write: ${url.pathname}`);
  });
  await page.goto(base);await page.waitForSelector("#heartbeatDot.ok");
  await page.getByRole("button",{name:"Config",exact:true}).click();
  await page.locator("#addATO").click();
  let row=page.locator("[data-ato]").first();
  assert.equal(await row.getByLabel("ATO enabled",{exact:true}).isChecked(),false);
  await row.getByLabel("ATO name",{exact:true}).fill("Sump refill");
  await row.getByLabel("Primary sensor",{exact:true}).selectOption("sense_port_1_water");
  await row.getByLabel("Backup high-water sensor",{exact:true}).selectOption("sense_port_2_water");
  await row.getByLabel("Refill device",{exact:true}).selectOption("ac1");
  await row.getByLabel("Start at or below",{exact:true}).selectOption("low");
  await row.getByLabel("Stop at or above",{exact:true}).selectOption("normal");
  await row.getByLabel("ATO name",{exact:true}).focus();
  await row.getByLabel("ATO name",{exact:true}).evaluate(el=>el.dataset.identity="stable");
  await page.waitForTimeout(4300);
  assert.equal(await row.getByLabel("ATO name",{exact:true}).inputValue(),"Sump refill");
  assert.equal(await row.getByLabel("ATO name",{exact:true}).getAttribute("data-identity"),"stable");
  assert.equal(await row.getByLabel("ATO name",{exact:true}).evaluate(el=>el===document.activeElement),true);
  failSave=true;await page.locator("#saveConfig").click();
  await page.waitForFunction(()=>document.getElementById("configMessage").textContent.includes("Storage unavailable"));
  assert.equal(await row.getByLabel("ATO name",{exact:true}).inputValue(),"Sump refill");
  failSave=false;await page.locator("#saveConfig").click();
  await page.waitForFunction(()=>document.getElementById("configMessage").textContent.includes("Config saved"));
  const id=snapshot.config.ato[0].id;
  assert.equal(snapshot.config.ato[0].enabled,false);
  await row.getByLabel("Refill device type",{exact:true}).selectOption("stepper");
  await row.getByLabel("Refill device",{exact:true}).selectOption("dose1");
  await row.getByLabel("Maximum refill (ml)",{exact:true}).fill("10");
  await row.getByLabel("Maximum rolling 24h volume (ml)",{exact:true}).fill("100");
  await row.getByLabel("ATO enabled",{exact:true}).check();
  await page.locator("#saveConfig").click();
  await page.waitForFunction(()=>document.getElementById("configMessage").textContent.includes("Config saved"));
  assert.equal(snapshot.config.ato[0].assigned_actuator,"dose1");
  assert.equal(snapshot.config.ato[0].max_daily_ml,100);
  await row.getByLabel("ATO name",{exact:true}).fill("Discard me");
  await page.locator("#cancelConfig").click();
  assert.equal(await row.getByLabel("ATO name",{exact:true}).inputValue(),"Sump refill");
  await row.getByLabel("Stop at or above",{exact:true}).selectOption("low");
  const before=writes.length;await page.locator("#saveConfig").click();
  assert.match(await page.locator("#configMessage").textContent(),/Start threshold/);
  assert.equal(writes.length,before);
  await page.locator("#cancelConfig").click();
  snapshot.ato_runtime.profiles[id]={status:"FAULT",primary:"low",backup:"high",faulted:true,fault_reason:"Backup high water",elapsed_seconds:4,dispensed_ml:1};
  await page.evaluate(()=>document.dispatchEvent(new Event("loggerhead:refresh-request")));
  await page.waitForFunction(()=>document.querySelector('[data-ato-runtime]').textContent.includes("FAULT"));
  await row.getByRole("button",{name:"Reset Fault",exact:true}).click();
  await page.waitForFunction(()=>document.getElementById("atoConfigMessage").textContent.includes("still active"));
  failReset=false;await row.getByRole("button",{name:"Reset Fault",exact:true}).click();
  await page.waitForFunction(()=>document.getElementById("atoConfigMessage").textContent.includes("accepted"));
  const screenshots=process.env.LOGGERHEAD_SCREENSHOT_DIR;
  if(screenshots){await mkdir(screenshots,{recursive:true});await page.locator("#atoConfig").screenshot({path:path.join(screenshots,`ato-config-${engine}.png`)});}
  await page.setViewportSize({width:390,height:844});await page.waitForTimeout(300);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
  if(screenshots)await page.locator("#atoConfig").screenshot({path:path.join(screenshots,`ato-mobile-${engine}.png`)});
  await page.getByRole("button",{name:"Dashboard",exact:true}).click();
  await page.waitForSelector("#atoOverview .card");
  assert.match(await page.locator("#atoOverview").textContent(),/Sump refill/);
  await page.getByRole("button",{name:"Config",exact:true}).click();
  await row.getByRole("button",{name:"Delete ATO",exact:true}).click();
  assert.equal(await page.locator("[data-ato]").count(),0);
  await page.locator("#cancelConfig").click();assert.equal(await page.locator("[data-ato]").count(),1);
  assert.deepEqual(errors,[]);
  console.log(`ATO ${engine} checks passed: CRUD, save/cancel, stable drafts, limits, reset errors, dashboard and mobile.`);
} finally {
  await browser?.close();
  if(server.exitCode===null){const exited=new Promise(resolve=>server.once("exit",resolve));server.stdin.on("error",()=>{});server.stdin.end("shutdown\n");const timer=setTimeout(()=>server.kill(),3000);try{await exited;}finally{clearTimeout(timer);}}
}
