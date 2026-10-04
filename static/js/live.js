// openSeedling dashboard: Live updates: the event stream, polling fallback, and the light curve.
// An ES module: what it uses from the others is imported at the top, and what
// it offers is exported. Another module cannot assign one of its variables
// directly; it calls the set_<name>() exported at the bottom. Code that runs
// at page load is in start(), which main.js calls in a fixed order.
// ---- live updates ----
// EventSource pushes a status the moment something changes. The poll stays,
// slowed right down: a stream that dies quietly would otherwise freeze the
// page, and this way the worst case is the old 15-second behaviour.
import { render } from './light.js';
import { lightBackend, pollFloat } from './charts.js';
import { applyStatus, refresh } from './grid.js';

export const POLL_FAST=15000, POLL_SLOW=60000;
export let pollTimer=null, es=null, streamOk=false;
export function setPoll(ms){
  if(pollTimer)clearInterval(pollTimer);
  pollTimer=setInterval(refresh, ms);
}
export function initStream(){
  if(!('EventSource' in window))return;        // old browser: polling only
  try{ es=new EventSource('/api/stream'); }catch(e){ return; }
  es.addEventListener('status', ev=>{
    let j=null;
    try{ j=JSON.parse(ev.data); }catch(e){ return; }
    if(j && j.error==='warming up')return;
    if(!streamOk){ streamOk=true; setPoll(POLL_SLOW); }
    applyStatus(j);
  });
  es.onerror=()=>{
    // the browser reconnects on its own; until it does, poll at full speed
    if(streamOk){ streamOk=false; setPoll(POLL_FAST); }
  };
}
// The server fixes a stream's signed-in state when it opens, so a login or
// logout has to reopen it, or the next push would show the old view.
export function restartStream(){
  if(es){ es.close(); es=null; }
  streamOk=false; setPoll(POLL_FAST);
  initStream();
}
// A hidden tab keeps no stream: each one holds a server thread and one of six
// slots. Closed a minute after hiding, reopened with a fresh status on return.
export let hideTimer=null;

export function curveLuxAt(pts, pct){
  // linear interpolation between the two measured points either side
  if(!pts.length)return null;
  if(pct<=pts[0][0])return pts[0][1];
  if(pct>=pts[pts.length-1][0])return pts[pts.length-1][1];
  for(let i=1;i<pts.length;i++){
    if(pts[i][0]>=pct){
      const [x0,y0]=pts[i-1],[x1,y1]=pts[i];
      return x1===x0?y1:y0+(y1-y0)*(pct-x0)/(x1-x0);
    }
  }
  return pts[pts.length-1][1];
}
export function drawLightCurve(curve, sweeping, nowPct){
  const svg=document.getElementById('lcurve');
  const wrap=document.getElementById('lcurvewrap');
  if(!svg||!wrap)return;
  if(lightBackend==='kasa'){wrap.style.display='none';return;}  // nothing to sweep
  wrap.style.display='';
  const pts=(curve&&curve.points)||[];
  if(!pts.length){
    svg.innerHTML=`<text x="160" y="66" text-anchor="middle" fill="#7a8a72" font-size="11">`
      +`${sweeping?'measuring\u2026':'no measurement yet'}</text>`;
    return;
  }
  const W=320,H=132,P=8,B=18,L=26;
  const maxL=Math.max(...pts.map(p=>p[1]))||1;
  const sx=v=>L+(v/100)*(W-L-P);
  const sy=v=>H-B-(v/maxL)*(H-B-P);
  let h='';
  for(let i=0;i<=2;i++){const y=P+(H-B-P)*i/2;
    h+=`<line x1="${L}" y1="${y}" x2="${W-P}" y2="${y}" stroke="#e6f0de" stroke-width="1"/>`;}
  // the ideal straight line. Raw: origin to peak, showing how far the fixture
  // is from linear. Calibrated: 1% (the dimmest lit level) to full, which the
  // calibrated curve should sit right on top of.
  if(curve.calibrated){
    h+=`<line x1="${sx(0)}" y1="${sy(0)}" x2="${sx(100)}" y2="${sy(maxL)}"
          stroke="#c9c9c9" stroke-width="1" stroke-dasharray="4 3"/>`;
    // the raw fixture, faint, for comparison
    const raw=(curve.rawPoints||[]);
    if(raw.length){
      const rl=raw.map(p=>`${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join(' ');
      h+=`<polyline fill="none" stroke="#e8b04b" stroke-opacity="0.28" stroke-width="1.2"
            points="${rl}" vector-effect="non-scaling-stroke"/>`;
    }
  } else {
    h+=`<line x1="${sx(0)}" y1="${sy(0)}" x2="${sx(100)}" y2="${sy(maxL)}"
          stroke="#c9c9c9" stroke-width="1" stroke-dasharray="4 3"/>`;
  }
  const line=pts.map(p=>`${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join(' ');
  h+=`<polyline fill="none" stroke="#e8b04b" stroke-width="2" points="${line}"
        vector-effect="non-scaling-stroke"/>`;
  if(!curve.calibrated)
    pts.forEach(p=>{h+=`<circle cx="${sx(p[0]).toFixed(1)}" cy="${sy(p[1]).toFixed(1)}" r="1.7" fill="#c98a1e"/>`;});
  h+=`<text x="${L}" y="${H-5}" font-size="9" fill="#7a8a72">0%</text>`;
  h+=`<text x="${W-P}" y="${H-5}" font-size="9" fill="#7a8a72" text-anchor="end">100%</text>`;
  h+=`<text x="2" y="${P+8}" font-size="9" fill="#7a8a72">${Math.round(maxL).toLocaleString()}</text>`;
  h+=`<text x="2" y="${H-B}" font-size="9" fill="#7a8a72">0 lx</text>`;
  // where the light is set right now, and what the curve says that delivers
  if(nowPct!=null&&!sweeping){
    const px=sx(Math.max(0,Math.min(100,nowPct)));
    const lx=curveLuxAt(pts,nowPct);
    const py=sy(lx);
    h+=`<line x1="${px.toFixed(1)}" y1="${P}" x2="${px.toFixed(1)}" y2="${H-B}"
          stroke="#4a7c59" stroke-width="1.2" stroke-dasharray="3 3"/>`;
    h+=`<circle cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="3.4"
          fill="#2e7d32" stroke="#fff" stroke-width="1.4"/>`;
    const label=`${Math.round(nowPct)}% \u2192 ${Math.round(lx).toLocaleString()} lx`;
    const wEst=label.length*5.3+8;
    const flip=px>W-wEst-P;                 // keep the label on-canvas near 100%
    h+=`<rect x="${(flip?px-wEst-3:px+3).toFixed(1)}" y="${P+1}" width="${wEst.toFixed(1)}" height="14"
          rx="3" fill="#2f4030" opacity="0.92"/>`;
    h+=`<text x="${(flip?px-wEst+1:px+7).toFixed(1)}" y="${P+11}" font-size="9.5"
          fill="#eafff0">${label}</text>`;
  }
  svg.innerHTML=h;
  const info=document.getElementById('sweepinfo');
  if(info&&!sweeping&&curve.ts){
    if(curve.stale){
      info.textContent='calibration is out of date \u00b7 press Calibrate';
    } else if(curve.calibrated){
      info.textContent=`calibrated \u00b7 linear to `
        +`${Math.round(maxL).toLocaleString()} lx`;
    } else {
      const half=pts.find(p=>p[1]>=maxL/2);
      info.textContent=`peak ${Math.round(maxL).toLocaleString()} lx`
        +(half?` \u00b7 50% output at ${half[0]}% set`:'')
        +' \u00b7 raw, not calibrated';
    }
  }
}
export async function startSweep(){
  const info=document.getElementById('sweepinfo');
  const btn=document.getElementById('sweepbtn');
  if(sweepRunning){
    await fetch('/api/light_sweep',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cancel:true})});
    return;
  }
  if(info)info.textContent='starting\u2026';
  try{
    // One button does the whole job: a fine raw sweep, then the calibration
    // built from it and switched on. A plain measure only ever showed the
    // fixture's physical curve, which never changes shape, while the button
    // that straightened it lived somewhere else in Settings.
    const r=await fetch('/api/light_sweep',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({linearize:true})});
    const j=await r.json().catch(()=>({}));
    if(r.status===401){if(info)info.textContent='log in first';return;}
    if(!j.ok){if(info)info.textContent=j.error||'failed';return;}
    if(info)info.textContent=`calibrating\u2026 about ${Math.round((j.estimate_seconds||180)/60)} min`;
    if(btn)btn.textContent='Cancel';
  }catch(e){if(info)info.textContent='request failed';}
}
export let sweepRunning=false, lastCurve=null;

// setters: other modules cannot assign an imported binding
export function set_lastCurve(v){ lastCurve=v; return v; }
export function set_sweepRunning(v){ sweepRunning=v; return v; }

// what ran at load time as a plain script; main.js calls it in the old order
export function start(){
document.addEventListener('visibilitychange',()=>{
  if(document.hidden){
    hideTimer=setTimeout(()=>{ if(es){ es.close(); es=null; streamOk=false; } },60000);
  }else{
    clearTimeout(hideTimer); hideTimer=null;
    if(!es){ restartStream(); refresh(); }
  }
});
setPoll(POLL_FAST);
initStream();
setInterval(render,60000);
setInterval(pollFloat,1500);
}
