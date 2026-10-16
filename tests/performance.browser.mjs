// No physical hardware: isolated fixture, with status responses deliberately stalled.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {createRequire} from 'node:module';
import {createInterface} from 'node:readline';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const require=createRequire(import.meta.url);
const {webkit}=require(process.env.LOGGERHEAD_PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const python=process.env.LOGGERHEAD_PYTHON || path.join(root,process.platform==='win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
const server=spawn(python,['scripts/benchmark_calibration_fixture.py','--lifetime','90'],{
  cwd:root,env:{...process.env,PYTHONPATH:root},stdio:['pipe','pipe','pipe'],windowsHide:true,
});
let browser;
try{
  const address=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error('Fixture startup timed out')),15000);
    server.once('exit',code=>{clearTimeout(timer);reject(new Error(`Fixture exited: ${code}`));});
    server.stderr.on('data',data=>process.stderr.write(data));
    createInterface({input:server.stdout}).on('line',line=>{
      if(line.startsWith('{')){clearTimeout(timer);resolve(JSON.parse(line));}
    });
  });
  browser=await webkit.launch({headless:true});
  const page=await browser.newPage({viewport:{width:390,height:844}}),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(`http://127.0.0.1:${address.port}`);
  await page.waitForSelector('#heartbeatDot.ok');
  let calls=0;
  const blocked=[];
  await page.route('**/api/status',route=>{
    calls++;
    return new Promise(resolve=>blocked.push(async()=>{await route.abort().catch(()=>{});resolve();}));
  });
  await page.evaluate(()=>{for(let i=0;i<20;i++)refresh();});
  await page.waitForTimeout(3500);
  assert.equal(calls,1,'normal polling and repeated refresh must share one in-flight request');
  await page.waitForFunction(()=>!document.getElementById('heartbeatDot').classList.contains('ok'),null,{timeout:12000});
  assert.ok(calls<=2,'a timed-out request must not cause a request backlog');
  for(const release of blocked)await release();
  await page.unroute('**/api/status');
  await page.waitForSelector('#heartbeatDot.ok',{timeout:12000});
  // HTTP errors must not masquerade as a healthy backend.
  await page.route('**/api/status',route=>route.fulfill({status:503,json:{error:'injected'}}));
  await page.evaluate(()=>refresh());
  await page.waitForFunction(()=>!document.getElementById('heartbeatDot').classList.contains('ok'));
  await page.unroute('**/api/status');
  await page.evaluate(()=>refresh());
  await page.waitForSelector('#heartbeatDot.ok');
  assert.deepEqual(errors,[]);
  console.log('PASS: WebKit single-flight polling, timeout, HTTP error, recovery; no motor operations.');
}finally{
  await browser?.close();
  server.stdin.write('shutdown\n');
  await new Promise(resolve=>{if(server.exitCode!==null)return resolve();server.once('exit',resolve);});
}
