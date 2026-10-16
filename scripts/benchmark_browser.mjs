// Live mode is GET-only. Calibration mode requires the isolated simulation fixture.
import {createRequire} from 'node:module';
import {parseArgs} from 'node:util';
import os from 'node:os';
const {values:args}=parseArgs({options:{url:{type:'string',default:'http://192.168.60.3:8080'},seconds:{type:'string',default:'20'},calibration:{type:'boolean',default:false},engine:{type:'string',default:'chromium'},iphone:{type:'boolean',default:false}}});
const require=createRequire(import.meta.url);
const {chromium,webkit,devices}=require(process.env.LOGGERHEAD_PLAYWRIGHT_MODULE || 'playwright');
if(!['chromium','webkit'].includes(args.engine))throw new Error('Engine must be chromium or webkit.');
const browser=await (args.engine==='webkit' ? webkit : chromium).launch({headless:true,...(args.engine==='chromium' && process.env.LOGGERHEAD_BROWSER_EXECUTABLE ? {executablePath:process.env.LOGGERHEAD_BROWSER_EXECUTABLE} : {})});
try{
  const page=await browser.newPage(args.iphone ? devices['iPhone 13'] : {viewport:{width:1365,height:900}});
  const session=args.engine==='chromium' ? await page.context().newCDPSession(page) : null;
  if(session)await session.send('Performance.enable');
  const requests=[], pending=new Map(), errors=[], phases=[];
  let peakPending=0, allowedSimulation=false;
  if(args.calibration){
    const info=await (await page.request.get(args.url+'/api/benchmark/info')).json();
    if(info.simulation!==true || info.isolated!==true)throw new Error('Refusing calibration: target is not the isolated simulation fixture.');
    allowedSimulation=true;
  }
  await page.route('**/*',route=>{
    const request=route.request(), path=new URL(request.url()).pathname;
    if(!['GET','HEAD'].includes(request.method()) && !(allowedSimulation && ['/api/calibration','/api/prime','/api/benchmark/delay'].includes(path)))return route.abort();
    return route.continue();
  });
  page.on('pageerror',error=>errors.push(error.message));
  page.on('request',request=>{
    pending.set(request,{path:new URL(request.url()).pathname,method:request.method(),start:Date.now()});
    peakPending=Math.max(peakPending,pending.size);
  });
  const finished=(request,failure=null)=>{
    const row=pending.get(request);if(!row)return;
    requests.push({...row,ms:Date.now()-row.start,failure});pending.delete(request);
  };
  page.on('requestfinished',request=>finished(request));
  page.on('requestfailed',request=>finished(request,request.failure()?.errorText));
  await page.addInitScript(()=>{
    window.benchmarkTasks=[];window.benchmarkLags=[];
    try{new PerformanceObserver(list=>window.benchmarkTasks.push(...list.getEntries().map(row=>row.duration))).observe({type:'longtask',buffered:true});}catch{}
    let previous=performance.now();setInterval(()=>{const now=performance.now();window.benchmarkLags.push(Math.max(0,now-previous-100));previous=now;},100);
  });
  await page.goto(args.url,{waitUntil:'domcontentloaded'});
  await page.waitForSelector('#heartbeatDot.ok');
  const metrics=async()=>session ? Object.fromEntries((await session.send('Performance.getMetrics')).metrics.map(row=>[row.name,row.value])) : {};
  const summarize=async(name,action)=>{
    const start=Date.now(), before=await metrics(), first=requests.length;
    peakPending=pending.size;
    const lagStart=await page.evaluate(()=>window.benchmarkLags.length), taskStart=await page.evaluate(()=>window.benchmarkTasks.length);
    const result=await action();
    const after=await metrics(), used=requests.slice(first), status=used.filter(row=>row.path==='/api/status').map(row=>row.ms).sort((a,b)=>a-b);
    const timing=await page.evaluate(({lagStart,taskStart})=>({max_event_loop_lag_ms:Math.max(0,...window.benchmarkLags.slice(lagStart)),long_tasks:window.benchmarkTasks.slice(taskStart)}),{lagStart,taskStart});
    phases.push({name,elapsed_ms:Date.now()-start,browser_task_ms:session ? 1000*(after.TaskDuration-before.TaskDuration) : null,browser_script_ms:session ? 1000*(after.ScriptDuration-before.ScriptDuration) : null,
      heap_bytes:after.JSHeapUsedSize,dom_nodes:after.Nodes,status_requests:status.length,status_p50_ms:status[Math.floor(status.length*.5)]??null,status_max_ms:status.at(-1)??null,
      max_pending_requests:peakPending,pending_status:[...pending.values()].filter(row=>row.path==='/api/status').length,request_failures:used.filter(row=>row.failure),...timing,result});
  };
  for(const tab of ['Dashboard','Config','Plots'])await summarize(tab,async()=>{
    await page.getByRole('button',{name:tab,exact:true}).click();
    await page.waitForTimeout(Number(args.seconds)*1000);
    return {heartbeat:await page.locator('#heartbeatText').textContent(),plot_state:await page.locator('#plot').getAttribute('data-state')};
  });
  if(allowedSimulation){
    await page.getByRole('button',{name:'Config',exact:true}).click();
    const pump=page.locator('[data-pump="dose1"]');
    await summarize('normal_mock_calibration',async()=>{
      await pump.getByRole('button',{name:'Run Calibration',exact:true}).click();
      await page.waitForTimeout(6500);
      return {message:await pump.locator('[data-cal-message]').textContent()};
    });
    await summarize('delayed_calibration_persistence',async()=>{
      await page.request.post(args.url+'/api/benchmark/delay',{data:{seconds:20}});
      await pump.getByRole('button',{name:'Run Calibration',exact:true}).click();
      await page.waitForTimeout(14000);
      const performance=await (await page.request.get(args.url+'/api/performance?stacks=1',{timeout:3000})).json();
      const rootProbe=await page.evaluate(async()=>{
        const start=performance.now(),controller=new AbortController(),timer=setTimeout(()=>controller.abort(),2000);
        try{const response=await fetch('/',{signal:controller.signal});await response.text();return {ok:response.ok,ms:performance.now()-start};}
        catch(error){return {ok:false,error:error.name,ms:performance.now()-start};}finally{clearTimeout(timer);}
      });
      const stopStart=Date.now();let stop;
      try{const response=await page.request.post(args.url+'/api/prime',{data:{id:'dose1',on:false},timeout:2000});stop={status:response.status(),ms:Date.now()-stopStart};}
      catch(error){stop={error:error.message.split('\n')[0],ms:Date.now()-stopStart};}
      const blocked=performance.threads.filter(row=>row.active_operations?.length).map(row=>({name:row.name,operations:row.active_operations,stack:row.stack}));
      await page.waitForTimeout(8000);
      return {root_probe:rootProbe,stop,blocked,message:await pump.locator('[data-cal-message]').textContent(),heartbeat:await page.locator('#heartbeatText').textContent()};
    });
  }
  console.log(JSON.stringify({target:args.url,simulation_calibration:allowedSimulation,engine:args.engine,iphone_viewport:args.iphone,browser:await browser.version(),browser_host:os.platform()+' '+os.arch(),phases,page_errors:errors,
    note:'Browser CPU/task times are on the browser host, not the Pi. GET-only on real hardware. Calibration uses simulated hardware and injected delays only.'},null,2));
}finally{await browser.close();}
