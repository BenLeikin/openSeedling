// openSeedling dashboard: Sensor readouts and units, and the chart grid.
// An ES module: what it uses from the others is imported at the top, and what
// it offers is exported. Another module cannot assign one of its variables
// directly; it calls the set_<name>() exported at the bottom. Code that runs
// at page load is in start(), which main.js calls in a fixed order.
// ---------------- sensors: readout, chart, overlay ----------------
import { S, agoStr } from './light.js';
import { inSetup, trayInSetup } from './setups.js';
import { set_playerFps } from './photos.js';
import { esc } from './trays.js';
import { dliBand, drawGrid, grid, refresh } from './grid.js';

export let sensorData={};
export let sampleMin=5, capMin=30, capOn=false, camHealth=null, presTrend=null, lightMetrics=null;
// Canopy comes from photos, which are taken only while the camera's light is
// on: stale only while due, counted from when they became due (null = not due,
// e.g. at night)
export let canopyDue=null;
export function readingStale(key, ts){
  if(!ts)return false;
  const now=Date.now()/1000;
  if(key.startsWith('canopy:'))
    return canopyDue!=null && now-Math.max(ts,canopyDue) > 3*capMin*60;
  if(key.startsWith('heat:'))return false;   // logged only while Auto runs
  return now-ts > 3*sampleMin*60;
}
export let probeCal={}, probeNames={}, probeDefaultCal=null;   // per-tray anchors, labels, fallback
export let probeFlags={};             // per-tray below_wet/above_dry from the server
export let filteredVals={};           // sensor -> transient-filtered value
export function probePct(c, v){
  if(!c || c.wet==null || c.dry==null || (c.dry-c.wet)<0.05) return null;
  return Math.max(0, Math.min(100, 100*(c.dry-v)/(c.dry-c.wet)));
}
export function probeMoisture(t, v){
  const p=probePct(probeCal[t], v);
  if(p!=null) return {pct:p, approx:false};
  const d=probePct(probeDefaultCal, v);
  return d==null ? null : {pct:d, approx:true};
}
export let chartHours=24;

export let units='imperial';
export function isMetric(){return units==='metric';}
export function c2f(c){return c*9/5+32;}
// display helpers: storage stays Celsius / hPa, only presentation switches
export function tDisp(c){return isMetric()?c:c*9/5+32;}
export function tUnit(){return isMetric()?'\u00b0C':'\u00b0F';}
export function tFromF(f){return isMetric()?(f-32)*5/9:f;}   // an F-stored setting, shown
export function tToF(v){return isMetric()?v*9/5+32:v;}       // ...and read back
export function pDisp(hpa){return isMetric()?hpa:hpa*0.0295299830714;}
export function pUnit(){return isMetric()?'hPa':'inHg';}
export function pDec(){return isMetric()?0:2;}
// key -> {group, label, value, unit}

export function sensorMeta(key, val){
  if(key==='temp:air')   return {group:'Environment', label:'Air',      value:tDisp(val).toFixed(1), unit:tUnit()};
  if(key==='humidity')   return {group:'Environment', label:'Humidity', value:val.toFixed(0),       unit:'%'};
  if(key==='lux')        return {group:'Environment', label:'Light',    value:Math.round(val).toLocaleString(), unit:'lx'};
  if(key.startsWith('lux:'))return {group:'Environment', label:'Light '+key.slice(4), value:Math.round(val).toLocaleString(), unit:'lx'};
  if(key==='ppfd')       return {group:'Environment', label:'PPFD',     value:Math.round(val).toLocaleString(), unit:'\u00b5mol'};
  if(key==='pressure'){
    const t=presTrend, ar=t?({down:'\u2198',up:'\u2197',flat:'\u2192'}[t.arrow]||''):'';
    return {group:'Environment', label:'Pressure', value:pDisp(val).toFixed(pDec()), unit:pUnit(),
            suffix:t?` <span class="ptrend ${t.arrow}">${ar} ${t.words}</span>`:'',
            title:t?`${t.change_3h>0?'+':''}${pDisp(t.change_3h).toFixed(pDec())} ${pUnit()} over 3h`
                    +(t.change_24h!=null?` \u00b7 ${t.change_24h>0?'+':''}${pDisp(t.change_24h).toFixed(pDec())} ${pUnit()} over 24h`:''):''};}
  if(key==='temp:soil')  return {group:'Soil',        label:'Soil temp',value:tDisp(val).toFixed(1), unit:tUnit()};
  if(key.startsWith('temp:soil_'))
                         return {group:'Soil',        label:'Soil temp '+key.split('_')[1], value:tDisp(val).toFixed(1), unit:tUnit()};
  if(key.startsWith('canopy:')){
    const t=key.slice(7);
    // canopy is measured over the tray's area, so it carries the tray's name
    const nm=((trayLabels[t])||('Tray '+t))+' canopy';
    return {group:'Growth', label:nm, value:val.toFixed(1), unit:'%',
            title:'share of plant pixels across the whole tray'};
  }
  if(key.startsWith('probe:')){
    const t=key.slice(6);
    const nm=probeNames[t]||('Soil moisture '+t);
    const m=probeMoisture(t, val);
    // server-side flag: the live reading sits outside this tray's anchors, so
    // the percentage is pegged and the dry alert is blind until recalibration
    const flag=probeFlags[t];
    const warn=flag?` <span class="calwarn" title="${flag==='below_wet'
      ?'reading is wetter than the wet anchor; recapture the wet point'
      :'reading is drier than the dry anchor; recapture the dry point'}">recal ${
      flag==='below_wet'?'wet':'dry'}</span>`:'';
    if(m) return {group:'Soil', label:nm,
                  value:(m.approx?'~':'')+m.pct.toFixed(0), unit:'%', suffix:warn,
                  title:val.toFixed(3)+'V'+(m.approx?' - estimated, not yet calibrated':'')};
    return {group:'Soil', label:nm, value:val.toFixed(3), unit:'V', suffix:warn};
  }
  const SYSL={'sys:mem_free':'Memory free','sys:app_mem':'Controller memory',
              'sys:screen_mem':'Screen memory','sys:swap':'Swap in use'};
  if(SYSL[key])return {group:'Device', label:SYSL[key], value:Math.round(val), unit:'MB',
                       title:'sampled every few minutes; free is what the kernel can still give out'};
  if(key==='heat:duty')  return {group:'Soil', label:'Heat mat power', value:Math.round(val), unit:'%',
                                  title:'the share of each 15-minute cycle the heat mat ran'};
  return {group:'Other', label:key, value:String(val), unit:''};
}
// Per-cell camera readings, laid out to match the physical trays. Canopy and
// surface dryness live in the same square because they describe the same cell;
// separate wrapped lists made it impossible to see which cell was which.
export function sensorLabel(key){
  const m=sensorMeta(key, 0);
  return m ? m.label : key;
}
// Sensor health, contradictions and post-fill verdicts. Collapsed by default:
// when everything is fine this is one line, and it only demands attention when
// something is actually wrong.
export function renderQuality(j){
  const wrap=document.getElementById('qualitywrap');
  const body=document.getElementById('qbody');
  const sum=document.getElementById('qsummary');
  if(!wrap||!body||!sum)return;
  const q=j.quality||{};
  const health=q.health||{};
  const keys=Object.keys(health).sort();
  if(!keys.length){wrap.style.display='none';return;}
  wrap.style.display='';
  const bad=keys.filter(k=>['poor','bad'].includes(health[k].grade));
  const fair=keys.filter(k=>health[k].grade==='fair');
  const notes=(q.contradictions||[]);
  const pf=q.postfill||{};
  const pfBad=Object.keys(pf).filter(t=>!pf[t].ok);
  sum.textContent = bad.length ? `\u00b7 ${bad.length} need attention`
    : (fair.length||notes.length||pfBad.length)
      ? `\u00b7 ${fair.length+notes.length+pfBad.length} to review`
      : '\u00b7 all good';
  sum.className = bad.length ? 'qbad' : '';
  let h='';
  for(const k of keys){
    const e=health[k];
    h+=`<div class="qrow"><span class="qname">${esc(sensorLabel(k))}</span>`
      +`<span class="qgrade ${esc(e.grade)}">${esc(e.grade)}</span>`
      +`<span class="qwhy">${esc((e.why||[]).join('; '))}</span></div>`;
  }
  for(const n of notes)
    h+=`<p class="qnote">${esc(n.message)}</p>`;
  for(const t of Object.keys(pf).sort())
    h+=`<p class="qnote ${pf[t].ok?'ok':'bad'}">${esc(pf[t].msg)}</p>`;
  body.innerHTML=h;
}

export var trayLabels={};
export function renderSensors(j){
  sensorData=j.sensors||{};
  probeCal=(j.settings&&j.settings.probe_cal)||{};
  probeNames=(j.settings&&j.settings.probe_names)||{};
  trayLabels=Object.fromEntries(Object.entries((j.settings&&j.settings.trays)||{})
    .map(([id,t])=>[id,(t&&t.label)||('Tray '+id)]));
  probeFlags=j.probe_cal_flags||{};
  filteredVals=j.filtered||{};   // smoothed values for the chips; charts stay raw
  if(j.probe_default_cal)probeDefaultCal=j.probe_default_cal;
  presTrend=j.pressure_tendency||null;
  lightMetrics=j.light_metrics||null;
  if(j.settings&&j.settings.units)units=j.settings.units;
  if(j.settings&&j.settings.soil_temp_high_f!=null)
    soilTempHigh=+j.settings.soil_temp_high_f;
  if(j.settings&&j.settings.soil_temp_low_f!=null)
    soilTempLow=+j.settings.soil_temp_low_f;
  if(j.settings&&j.settings.humidity_high!=null)humHigh=+j.settings.humidity_high;
  if(j.settings&&j.settings.humidity_low!=null)humLow=+j.settings.humidity_low;
  if(j.settings){
    sampleMin=+j.settings.sample_interval_min||5;
    capMin=+j.settings.capture_interval_min||30;
    set_playerFps(+j.settings.player_fps||4);
    capOn=!!j.settings.capture_enabled;
  }
  if('canopy_due_since' in j)canopyDue=j.canopy_due_since;
  // charts drawn before this status arrived lacked calibration context
  // (probe %, thresholds); re-render them once from the cached series
  if(!window._chartCtxSynced && Object.keys(seriesData).length){
    window._chartCtxSynced=true; renderChartGrid();
  }
  const card=document.getElementById('sensorcard');
  const keys=Object.keys(sensorData).filter(inSetup);
  card.style.display=keys.length?'':'none';
  // grouped readout
  const groups={};
  for(const k of keys){
    if(k.startsWith('float:')||k.startsWith('reservoir:'))continue; // shown in the water controls instead
    if(k.startsWith('growth')||k.startsWith('dry:')||k.startsWith('moisture:'))
      continue;                               // drawn as tray grids below
    // chips show the smoothed value so the readout matches what the alerts
    // judge; the charts below keep the raw series
    const raw=sensorData[k].value;
    const v=(k in filteredVals && filteredVals[k]!=null) ? filteredVals[k] : raw;
    const missing = v==null || (typeof v==='number'&&isNaN(v));
    const m=sensorMeta(k, missing?0:v);
    m.key0=k;
    if(missing)m.value='-';                    // no reading -> dash
    (groups[m.group]=groups[m.group]||[]).push(m);
  }
  const order=['Environment','Soil','Moisture','Moisture (cam)','Growth','Dryness','Other'];
  let h='';
  for(const g of order){
    if(!groups[g])continue;
    h+=`<div class="sgroup"><h3>${g}</h3>`;
    for(const m of groups[g]){
      {const ts=sensorData[m.key0]&&sensorData[m.key0].ts;
       const isStale=readingStale(m.key0||'', ts);
       if(isStale){m.stale=true;m.title=(m.title?m.title+' \u00b7 ':'')
         +'last reading '+agoStr(new Date(ts*1000));}}
      {const fill=(m.unit==='%'&&!isNaN(parseFloat(m.value)))
          ?` style="--fill:${Math.max(0,Math.min(100,parseFloat(m.value)))}%" data-fill`:'' ;
       h+=`<span class="schip${m.stale?' stale':''}"${fill}${m.title?` title="${esc(m.title)}"`:''}>${esc(m.label)} <b>${m.value}</b><span class="u">${m.unit}</span>${m.suffix||''}</span>`;}
    }
    h+='</div>';
  }
  if(!h.trim()){
    h='<div class="emptystate">'
      +'<b>No sensors reporting yet.</b>'
      +'<p>Wire the ADS1115, BME/BMP280, BH1750 or DS18B20 to the I2C pins, then check '
      +'they appear in <code>i2cdetect -y 1</code>. Readings show up within one sample '
      +'interval of the service restarting.</p></div>';
  }
  document.getElementById('sreadout').innerHTML=h;
  {// derived light metrics ride alongside the Environment chips
   const env=document.querySelector('#sreadout .sgroup');
   if(lightMetrics&&env){
     const groups=[...document.querySelectorAll('#sreadout .sgroup')];
     const envg=groups.find(g=>/Environment/.test(g.querySelector('h3')?.textContent||''));
     if(envg){
       let extra='';
       if(lightMetrics.ppfd!=null)
         extra+=`<span class="schip" title="at canopy \u00b7 lux \u00f7 ${lightMetrics.k}`
           +`${lightMetrics.canopy&&lightMetrics.canopy!==1?` \u00d7 ${lightMetrics.canopy} canopy factor`:''}">`
           +`PPFD <b>${Math.round(lightMetrics.ppfd)}</b><span class="u">\u00b5mol</span></span>`;
       if(lightMetrics.dli!=null){
         const d=lightMetrics.dli, cls=(d>=dliBand.lo&&d<=dliBand.hi)?'ok':(d<dliBand.lo?'low':'high');
         extra+=`<span class="schip dli ${cls}" title="daily light integral so far today \u00b7 seedling target ${dliBand.lo}-${dliBand.hi} mol/m\u00b2/day">`
           +`DLI <b>${d.toFixed(1)}</b><span class="u">mol</span></span>`;
       }
       if(extra)envg.insertAdjacentHTML('beforeend',extra);
     }
   }}
  const pcctl=document.getElementById('probecal');
  if(pcctl)pcctl.style.display = keys.some(k=>k.startsWith('probe:')) ? '' : 'none';
  const ptc=document.getElementById('probetc');
  if(ptc)ptc.style.display = (keys.some(k=>k.startsWith('probe:'))
                              && keys.some(k=>k.startsWith('temp:soil'))) ? '' : 'none';
  // collapse the whole calibration section if nothing in it applies
  {const cw=document.querySelector('.calwrap');
   if(cw)cw.style.display=[pcctl,ptc].some(el=>el&&el.style.display!=='none')?'':'none';}
  if(grid)drawGrid();   // refresh per-cell overlay
}
// ---- chart grid: every sensor visible at once, grouped by section ----
export const CHART_SECTIONS=[
  // ordered by how often they drive a decision, not by sensor type
  {id:'soil',   title:'Soil',
   match:k=>k.startsWith('temp:soil')||k.startsWith('probe:')||k.startsWith('heat:')},
  {id:'env',    title:'Environment',
   match:k=>k==='temp:air'||k==='humidity'||k==='lux'||k.startsWith('lux:')||k==='ppfd'||k==='pressure'},
  {id:'growth', title:'Canopy',           match:k=>k.startsWith('canopy:')},
  {id:'device', title:'Device',           match:k=>k.startsWith('sys:')},
  {id:'other',  title:'Other',            match:k=>true},
];
export let seriesData={}, chartPlots={}, soilTempHigh=85, soilTempLow=80;
export let humHigh=60, humLow=40;
// lux and PPFD are the same measurement in two units, so the lux card names
// both rather than the app drawing two identical charts
export function ppfdFromLux(lx){
  const k=Number(S&&S.settings&&S.settings.lux_to_ppfd_k)||0;
  const cf=Number(S&&S.settings&&S.settings.canopy_factor)||1;
  return k>0 ? lx*cf/k : null;
}
export function chartHeadUnit(key){
  if(key!=='lux')return chartUnitFor(key);
  return ppfdFromLux(1)==null ? 'lx' : 'lx \u00b7 \u00b5mol/m\u00b2/s';
}

export function chartUnitFor(s){
  if(s.startsWith('temp:'))return tUnit();
  if(s.startsWith('humidity')||s.startsWith('canopy:')||s.startsWith('heat:'))return '%';
  if(s.startsWith('sys:'))return 'MB';
  if(s.startsWith('probe:')){const t=s.slice(6);
    return (probeCal[t]&&probeCal[t].wet!=null)||probeDefaultCal?'%':'V';}
  if(s.startsWith('lux'))return 'lx';
  if(s==='pressure')return pUnit();
  if(s==='ppfd')return '\u00b5mol/m\u00b2/s';
  return '';
}
export function convertFor(s){
  if(s.startsWith('temp:'))return v=>tDisp(v);
  if(s==='pressure')return v=>pDisp(v);
  if(s.startsWith('probe:')){const t=s.slice(6);
    return v=>{const m=probeMoisture(t,v);return m==null?v:m.pct;};}
  return v=>v;
}
export let lastChartLoad=0, lastHostLoad=0;
export async function loadChart(){
  lastChartLoad=Date.now();
  const info=document.getElementById('chartinfo');
  if(info)info.textContent='loading\u2026';
  try{
    const r=await fetch(`/api/series_all?hours=${chartHours}`);
    const j=await r.json();
    seriesData=j.series||{};
    if(info)info.textContent='';
    renderChartGrid();
  }catch(e){if(info)info.textContent='charts unavailable';}
}
export let expandedCharts=new Set();
export function renderChartGrid(){
  const grid=document.getElementById('chartgrid');
  if(!grid)return;
  // remember which cards are open: the grid is rebuilt on every reload
  document.querySelectorAll('.ccard.expanded').forEach(c=>expandedCharts.add(c.id));
  // PPFD is lux scaled by a constant, so a separate card would draw the same
  // line twice. It rides along on the lux chart as a second unit instead.
  const keys=Object.keys(seriesData).filter(k=>!k.startsWith('float:')
    &&!k.startsWith('reservoir:')&&k!=='ppfd'&&inSetup(k)).sort();
  if(!keys.length){
    const haveNow=Object.keys(sensorData||{}).length>0;
    grid.innerHTML='<div class="emptystate">'
      +(haveNow
        ? '<b>Collecting history.</b><p>Charts appear once a few samples are logged, '
          +'usually within 15 minutes of the first reading.</p>'
        : '<b>No history yet.</b><p>Once sensors are connected and reporting, their '
          +'charts build up here automatically.</p>')
      +'</div>';
    return;
  }
  const used=new Set();
  let h='';
  for(const sec of CHART_SECTIONS){
    const mine=keys.filter(k=>!used.has(k)&&sec.match(k));
    if(!mine.length)continue;
    mine.forEach(k=>used.add(k));
    h+=`<div class="csection${sec.id==='soil'?' primary':''}"><h3>${sec.title}</h3>`
      +`<div class="cgrid">`;
    for(const k of mine){
      const m=sensorMeta(k,0);
      h+=`<div class="ccard" id="cc-${cssId(k)}">
            <div class="chead"><span>${esc(m.label)}<span class="cunit">${chartHeadUnit(k)}</span></span>
              <span class="cstats" id="cs-${cssId(k)}">&mdash;</span>
              <button type="button" class="cexpand" data-key="${cssId(k)}"
                      title="Expand this chart" aria-label="Expand ${esc(m.label)} chart">\u2922</button></div>
            <svg class="cmini" id="cv-${cssId(k)}" viewBox="0 0 320 110"
                 preserveAspectRatio="none" role="img" aria-label="${esc(m.label)} history"></svg>
          </div>`;
    }
    h+='</div></div>';
  }
  grid.innerHTML=h;
  for(const id of expandedCharts){
    const c=document.getElementById(id);
    if(c){
      c.classList.add('expanded');
      const b=c.querySelector('.cexpand');
      if(b){b.textContent='\u2921';b.title='Shrink this chart';}
    }
  }
  chartPlots={};
  for(const k of keys)drawMini(k);
  // Redraw a chart whenever its box changes size: when it first becomes
  // visible, on a window resize, or when the layout settles. The SVG is drawn
  // 1:1 in its own pixels; drawn at a stale size and stretched to fit
  // (preserveAspectRatio none), its text came out squashed or stretched.
  if(chartRO){chartRO.disconnect();grid.querySelectorAll('svg.cmini').forEach(s=>chartRO.observe(s));}
  layoutChartRows();
  if(rowRO){rowRO.disconnect();rowRO.observe(grid);}
}
// Every row of charts fills the width, the charts in a row the same size,
// and rows balanced: 4 charts that fit 3 across go 2 + 2, not 3 + 1.
// The column count per section comes from the width available and the
// smallest readable chart (340 px for Soil, 260 px for the rest).
export function layoutChartRows(){
  document.querySelectorAll('#chartgrid .cgrid').forEach(g=>{
    const cards=[...g.children].filter(c=>c.classList.contains('ccard')&&!c.classList.contains('expanded'));
    const n=cards.length; if(!n)return;
    const W=g.clientWidth, gap=10;
    const minW=g.closest('.csection.primary')?340:260;
    const cmax=Math.max(1,Math.floor((W+gap)/(minW+gap)));
    const rows=Math.ceil(n/cmax), cols=Math.ceil(n/rows);
    g.style.setProperty('--cols',cols);
  });
}
export var rowRO=('ResizeObserver' in window)?new ResizeObserver(()=>requestAnimationFrame(layoutChartRows)):null;
export var chartRO=('ResizeObserver' in window)?new ResizeObserver(entries=>{
  for(const e of entries){
    const svg=e.target, vb=(svg.getAttribute('viewBox')||'').split(' ');
    const w=Math.round(e.contentRect.width), h=Math.round(e.contentRect.height);
    if(w<10||h<10||(Math.abs(+vb[2]-w)<=1&&Math.abs(+vb[3]-h)<=1))continue;
    const key=Object.keys(seriesData).find(k=>'cv-'+cssId(k)===svg.id);
    if(key)requestAnimationFrame(()=>drawMini(key));
  }
}):null;
export function cssId(k){return k.replace(/[^a-zA-Z0-9]/g,'_');}
// round-number ticks between a and b, about n of them
export function niceTicks(a,b,n){
  const span=Math.max(1e-9,b-a), raw=span/Math.max(1,n);
  const mag=Math.pow(10,Math.floor(Math.log10(raw))), f=raw/mag;
  const step=(f<1.5?1:f<3?2:f<7?5:10)*mag;
  const ticks=[];
  for(let v=Math.ceil(a/step)*step; v<=b+step*1e-6; v+=step)ticks.push(+v.toPrecision(12));
  return {ticks,step};
}
// time ticks at round local hours (or midnights for long ranges), with labels
// that never collide
export function timeTicks(x0,x1,sx,top,bot,W,FS,big){
  const span=(x1-x0)/3600;
  const hrs=span<=8?1:span<=30?(big?2:3):span<=80?12:span<=200?24:span<=400?48:120;
  let out='', lastRight=-1e9;
  const d=new Date(x0*1000); d.setMinutes(0,0,0);
  if(hrs>=24)d.setHours(0);
  else d.setHours(Math.ceil(d.getHours()/hrs)*hrs);
  while(d.getTime()/1000<x0)d.setTime(d.getTime()+hrs*3600000);
  for(let t=d.getTime()/1000; t<=x1; ){
    const x=sx(t);
    const lab=hrs>=24?new Date(t*1000).toLocaleDateString([],{month:'numeric',day:'numeric'})
      :new Date(t*1000).toLocaleTimeString([],{hour:'numeric'}).replace(':00','');
    out+=`<line x1="${x.toFixed(1)}" y1="${top}" x2="${x.toFixed(1)}" y2="${bot}" class="cgl cvgrid"/>`;
    const w=lab.length*FS*0.6;
    if(x-w/2>lastRight+6&&x-w/2>0&&x+w/2<W){
      out+=`<text x="${x.toFixed(1)}" y="${bot+FS+3}" class="cxax" font-size="${FS}" text-anchor="middle">${lab}</text>`;
      lastRight=x+w/2;
    }
    const nd=new Date(t*1000); nd.setHours(nd.getHours()+hrs); t=nd.getTime()/1000;
  }
  return out;
}
// shaded lights-off hours, from today's schedule repeated over the window
export function nightBands(x0,x1,sx,top,bot){
  if(!S||!S.on||!S.off||(x1-x0)>8*86400)return '';
  const minOf=ms=>{const d=new Date(ms);return d.getHours()*60+d.getMinutes();};
  const onM=minOf(+S.on), offM=minOf(+S.off);
  if(onM===offM)return '';
  let out='';
  const d=new Date(x0*1000); d.setHours(0,0,0,0); d.setDate(d.getDate()-1);
  for(;d.getTime()/1000<x1;d.setDate(d.getDate()+1)){
    const day=d.getTime()/1000;
    let a=day+offM*60, b=day+onM*60;
    if(b<=a)b+=86400;                    // off in the evening, on next morning
    a=Math.max(a,x0); b=Math.min(b,x1);
    if(b>a)out+=`<rect x="${sx(a).toFixed(1)}" y="${top}" width="${(sx(b)-sx(a)).toFixed(1)}" height="${bot-top}" class="cnight"/>`;
  }
  return out;
}
export function drawMini(key){
  const svg=document.getElementById('cv-'+cssId(key));
  const stat=document.getElementById('cs-'+cssId(key));
  if(!svg)return;
  const card=document.getElementById('cc-'+cssId(key));
  const big=!!(card&&card.classList.contains('expanded'));
  // match the viewBox to the element's real pixel size: one unit = one CSS
  // pixel, so text renders at its natural shape at any card width. A fixed
  // viewBox stretched to fit would smear the labels (badly so on a phone).
  const r=svg.getBoundingClientRect();
  if(r.width<10||r.height<10)return;        // not laid out yet: the resize observer draws it
  const W=Math.max(200,Math.round(r.width));
  const H=Math.max(80,Math.round(r.height));
  const P=big?12:6, B=big?24:16;
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  const FS=big?12:10, FS2=big?13:11;   // now honest px sizes
  const conv=convertFor(key), unit=chartUnitFor(key);
  const data=(seriesData[key]||[]).map(([t,v])=>[t,conv(v)])
                                  .filter(d=>d[1]!=null&&!isNaN(d[1]));
  if(data.length<2){
    svg.innerHTML=`<text x="${W/2}" y="${H/2}" text-anchor="middle" fill="#7a8a72" font-size="11">not enough data</text>`;
    if(stat)stat.innerHTML='&mdash;';return;
  }
  const xs=data.map(d=>d[0]), ys=data.map(d=>d[1]);
  const x0=Math.min(...xs), x1=Math.max(...xs);
  let y0=Math.min(...ys), y1=Math.max(...ys);
  const pct=unit==='%';
  // target band: soil temp uses the germination window, humidity its own
  // comfort range. Both draw through the same code below.
  const isSoil=key.startsWith('temp:soil');
  const isHum=key==='humidity';
  let hiLine=null, loLine=null, hiTxt='', loTxt='';
  if(isSoil){
    if(soilTempHigh>0){hiLine=tFromF(soilTempHigh);hiTxt=`${Math.round(tFromF(soilTempHigh))}${tUnit()}`;}
    if(soilTempLow>0){loLine=tFromF(soilTempLow);loTxt=`${Math.round(tFromF(soilTempLow))}${tUnit()}`;}
  }else if(isHum){
    if(humHigh>0){hiLine=humHigh;hiTxt=`${humHigh}%`;}
    if(humLow>0){loLine=humLow;loTxt=`${humLow}%`;}
  }
  if(hiLine!=null){y0=Math.min(y0,hiLine);y1=Math.max(y1,hiLine);}
  if(loLine!=null){y0=Math.min(y0,loLine);y1=Math.max(y1,loLine);}
  if(pct){y0=Math.min(y0,0);y1=Math.max(y1,100);}   // % charts on a fixed scale
  if(y0===y1){y0-=1;y1+=1;}
  const pad=(y1-y0)*0.08; if(!pct){y0-=pad;y1+=pad;}
  const sx=t=>P+(t-x0)/((x1-x0)||1)*(W-2*P);
  const sy=v=>H-B-(v-y0)/((y1-y0)||1)*(H-B-P);
  let h='';
  // lights-off hours, shaded, so day/night patterns explain themselves
  h+=nightBands(x0,x1,sx,P,H-B);
  // value gridlines at round numbers, labelled on the left
  {const yt=niceTicks(y0,y1,big?5:3);
   const ydec=Math.max(0,Math.min(3,-Math.floor(Math.log10(yt.step)+1e-9)));
   for(const v of yt.ticks){const yy=sy(v).toFixed(1);
     h+=`<line x1="${P}" y1="${yy}" x2="${W-P}" y2="${yy}" class="cgl"/>`
       // label above its line, or below it when the line is at the very top
       +`<text x="${P+2}" y="${(sy(v)-3<FS?sy(v)+FS:sy(v)-3).toFixed(1)}" class="cyax" font-size="${FS}">${v.toFixed(ydec)}</text>`;}}
  // time gridlines at round hours or days
  h+=timeTicks(x0,x1,sx,P,H-B,W,FS,big);
  // Split the series where sampling stopped. Drawing one unbroken line across
  // an outage claims readings that were never taken; each run of real data is
  // solid, and the interval between runs is a faint dashed bridge so the shape
  // still reads while the absence is visible.
  const gaps=[];
  for(let i=1;i<data.length;i++)gaps.push(data[i][0]-data[i-1][0]);
  const sorted=gaps.slice().sort((a,b)=>a-b);
  const typical=sorted.length?sorted[Math.floor(sorted.length/2)]:0;
  // 2.5x the usual spacing: tolerant of jitter, tight enough to catch a
  // restart or a sensor dropping out for a couple of cycles
  const gapLimit=typical>0 ? typical*2.5 : Infinity;
  const runs=[]; let run=[data[0]];
  for(let i=1;i<data.length;i++){
    if(data[i][0]-data[i-1][0]>gapLimit){runs.push(run);run=[];}
    run.push(data[i]);
  }
  runs.push(run);
  const pt=d=>`${sx(d[0]).toFixed(1)},${sy(d[1]).toFixed(1)}`;
  // the heat mat's power holds for a whole window: draw it as steps, not slopes
  const stepped=key.startsWith('heat:');
  const stepPts=r=>{const o=[];r.forEach((d,i)=>{if(i)o.push([d[0],r[i-1][1]]);o.push(d);});return o;};
  for(const r0 of runs){
    const r=stepped?stepPts(r0):r0;
    if(r.length<2){
      // a lone sample between two outages still deserves to be visible
      if(r.length===1)
        h+=`<circle cx="${sx(r[0][0]).toFixed(1)}" cy="${sy(r[0][1]).toFixed(1)}"
              r="2" fill="#4a7c59"/>`;
      continue;
    }
    const seg=r.map(pt).join(' ');
    const x0s=sx(r[0][0]).toFixed(1), x1s=sx(r[r.length-1][0]).toFixed(1);
    h+=`<polygon points="${x0s},${H-B} ${seg} ${x1s},${H-B}"
          fill="rgba(74,124,89,0.10)"/>`;
    h+=`<polyline fill="none" stroke="#4a7c59" stroke-width="1.8" points="${seg}"
          pathLength="1" class="cline" vector-effect="non-scaling-stroke"/>`;
  }
  for(let i=1;i<runs.length;i++){
    const a0=runs[i-1][runs[i-1].length-1], b0=runs[i][0];
    h+=`<line x1="${sx(a0[0]).toFixed(1)}" y1="${sy(a0[1]).toFixed(1)}"
          x2="${sx(b0[0]).toFixed(1)}" y2="${sy(b0[1]).toFixed(1)}"
          stroke="#4a7c59" stroke-width="1.4" stroke-dasharray="3 4" opacity="0.45"
          vector-effect="non-scaling-stroke" class="cgap"/>`;
  }
  if(key==='pressure'&&data.length>=4){
    // least-squares fit across the window: the slope is the weather signal
    const n=data.length;
    const mx=xs.reduce((a,b)=>a+b,0)/n, my=ys.reduce((a,b)=>a+b,0)/n;
    let num=0,den=0;
    for(let i=0;i<n;i++){num+=(xs[i]-mx)*(ys[i]-my);den+=(xs[i]-mx)**2;}
    if(den>0){
      const m0=num/den;
      const fy=t=>my+m0*(t-mx);
      const c=(v)=>Math.max(P,Math.min(H-B,sy(v)));
      h+=`<line x1="${sx(x0).toFixed(1)}" y1="${c(fy(x0)).toFixed(1)}"
            x2="${sx(x1).toFixed(1)}" y2="${c(fy(x1)).toFixed(1)}"
            stroke="#8a8a8a" stroke-width="1.2" stroke-dasharray="4 3"
            vector-effect="non-scaling-stroke" opacity="0.85"/>`;
    }
  }
  if(hiLine!=null||loLine!=null){
    const clamp=v=>Math.max(P,Math.min(H-B,v));
    // the target band between the two bounds, so "in range" reads at a glance
    if(hiLine!=null&&loLine!=null){
      const top=clamp(sy(hiLine)), bot=clamp(sy(loLine));
      if(bot>top)
        h+=`<rect x="${P}" y="${top.toFixed(1)}" width="${W-2*P}"
              height="${(bot-top).toFixed(1)}" fill="rgba(74,124,89,0.10)"/>`;
    }
    if(hiLine!=null){
      const hy=sy(hiLine);
      if(hy>=P&&hy<=H-B){
        h+=`<rect x="${P}" y="${P}" width="${W-2*P}" height="${Math.max(0,hy-P).toFixed(1)}"
              fill="rgba(181,50,47,0.07)"/>`;
        h+=`<line x1="${P}" y1="${hy.toFixed(1)}" x2="${W-P}" y2="${hy.toFixed(1)}"
              stroke="#b5322f" stroke-width="1.2" stroke-dasharray="5 4"
              vector-effect="non-scaling-stroke"/>`;
        h+=`<text x="${W-P-2}" y="${(hy-3).toFixed(1)}" text-anchor="end" font-size="${FS}"
              fill="#b5322f" font-size="${FS}">${hiTxt}</text>`;
      }
    }
    if(loLine!=null){
      const ly=sy(loLine);
      if(ly>=P&&ly<=H-B){
        h+=`<rect x="${P}" y="${ly.toFixed(1)}" width="${W-2*P}"
              height="${Math.max(0,(H-B)-ly).toFixed(1)}" fill="rgba(58,110,165,0.07)"/>`;
        h+=`<line x1="${P}" y1="${ly.toFixed(1)}" x2="${W-P}" y2="${ly.toFixed(1)}"
              stroke="#3a6ea5" stroke-width="1.2" stroke-dasharray="5 4"
              vector-effect="non-scaling-stroke"/>`;
        h+=`<text x="${W-P-2}" y="${(ly+10).toFixed(1)}" text-anchor="end" font-size="${FS}"
              fill="#3a6ea5" font-size="${FS}">${loTxt}</text>`;
      }
    }
  }
  const fmtT=t=>{const d=new Date(t*1000);
    return chartHours<=24?d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})
                         :d.toLocaleDateString([],{month:'numeric',day:'numeric'});};
  void fmtT;
  h+=`<line class="hvl" y1="${P}" y2="${H-B}" stroke="#4a7c59" stroke-width="1"
        stroke-dasharray="3 3" style="display:none"/>`;
  h+=`<circle class="hdot" r="${big?4.5:3}" fill="#2e7d32" stroke="#fff" stroke-width="1.2" style="display:none"/>`;
  h+=`<g class="hlbl" style="display:none">
        <rect rx="3" fill="#2f4030" opacity="0.92"/>
        <text font-size="${FS2}" fill="#eafff0"></text>
      </g>`;
  svg.innerHTML=h;
  const dec=(unit==='%')?0:(unit==='lx'?0:(unit==='inHg'?2:(unit==='hPa'?0:1)));
  const cur=ys[ys.length-1], lo=Math.min(...ys), hi=Math.max(...ys);
  const over=(hiLine!=null&&cur>hiLine)||(loLine!=null&&cur<loLine);
  const lastTs=xs[xs.length-1];
  const isStale=readingStale(key, lastTs);
  svg.classList.toggle('cstale', isStale);
  const ppfdNow = key==='lux' ? ppfdFromLux(cur) : null;
  if(stat)stat.innerHTML=`<b${over?' class="hot"':''}>${cur.toFixed(dec)}</b>`
    +(ppfdNow!=null?` <span class="alt2">${Math.round(ppfdNow)} \u00b5mol</span>`:'')
    +` \u00b7 lo ${lo.toFixed(dec)} \u00b7 hi ${hi.toFixed(dec)}`
    +(isStale?` <span class="stalebadge" title="last point ${agoStr(new Date(lastTs*1000))}">stale</span>`:'');
  chartPlots[key]={unit,dec,W,H,P,B,big,
    alt: key==='lux' ? ppfdFromLux : null,
    pts:data.map(d=>({x:sx(d[0]),y:sy(d[1]),v:d[1],t:d[0]})),
    gap:gapLimit,
    fmt:t=>new Date(t*1000).toLocaleString([],{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'})};
}
export function chartMove(e){
  const svg=e.target.closest('svg.cmini');
  if(!svg)return;
  const key=Object.keys(chartPlots).find(k=>'cv-'+cssId(k)===svg.id);
  const plot=key&&chartPlots[key];
  if(!plot)return;
  const ctm=svg.getScreenCTM&&svg.getScreenCTM();
  if(!ctm)return;
  const sp=svg.createSVGPoint(); sp.x=e.clientX; sp.y=e.clientY;
  const loc=sp.matrixTransform(ctm.inverse());
  let best=null,bd=1e9;
  for(const pt of plot.pts){const d=Math.abs(pt.x-loc.x);if(d<bd){bd=d;best=pt;}}
  if(!best)return;
  // every chart follows the same moment, so soil, air, light and the heat
  // mat's power can be read against each other
  for(const k of Object.keys(chartPlots))
    showCross(k, best.t, k===key);
  const tip=document.getElementById('charttip');
  if(tip&&!plot.big){
    tip.textContent=plot.fmt(best.t);
    tip.style.display='';tip.style.left=(e.clientX+12)+'px';tip.style.top=(e.clientY-32)+'px';
  }else if(tip){tip.style.display='none';}
}
export function showCross(k, t, own){
  const plot=chartPlots[k], svg=document.getElementById('cv-'+cssId(k));
  if(!plot||!svg)return;
  const vl=svg.querySelector('.hvl'),dot=svg.querySelector('.hdot'),lbl=svg.querySelector('.hlbl');
  // nearest point in time; nothing if the series has no reading near then
  let lo=0,hi=plot.pts.length-1;
  while(hi-lo>1){const m=(lo+hi)>>1;if(plot.pts[m].t<t)lo=m;else hi=m;}
  const best=Math.abs(plot.pts[lo].t-t)<=Math.abs(plot.pts[hi].t-t)?plot.pts[lo]:plot.pts[hi];
  const near=Math.abs(best.t-t)<=Math.max(plot.gap||0,600);
  for(const el of [vl,dot,lbl])if(el)el.style.display=near?'':'none';
  if(!near)return;
  vl.setAttribute('x1',best.x);vl.setAttribute('x2',best.x);
  dot.setAttribute('cx',best.x);dot.setAttribute('cy',best.y);
  const txt=lbl.querySelector('text'), rect=lbl.querySelector('rect');
  let s1=`${best.v.toFixed(plot.dec)}${plot.unit}`;
  if(plot.alt){const p=plot.alt(best.v);if(p!=null)s1+=` / ${Math.round(p)} \u00b5mol`;}
  const label=(own&&plot.big)?`${s1}  \u00b7  ${plot.fmt(best.t)}`:s1;
  txt.textContent=label;
  const cw=label.length*(plot.big?7.6:5.6)+10, ch=plot.big?22:16;
  let bx=best.x+8;
  if(bx+cw>plot.W-plot.P)bx=best.x-cw-8;
  const by=Math.max(plot.P, Math.min(plot.H-plot.B-ch, best.y-ch/2));
  rect.setAttribute('x',bx); rect.setAttribute('y',by);
  rect.setAttribute('width',cw); rect.setAttribute('height',ch);
  txt.setAttribute('x',bx+5); txt.setAttribute('y',by+ch-(plot.big?7:5));
}
export function chartLeave(){
  document.querySelectorAll('svg.cmini .hvl, svg.cmini .hdot, svg.cmini .hlbl')
    .forEach(el=>el.style.display='none');
  const tip=document.getElementById('charttip');
  if(tip)tip.style.display='none';
}
export function floatLabel(v){
  return v===null ? 'no sensor' : (v>=1 ? 'not full' : 'full');
}
export let pumpActive=false;   // float state only changes while a pump runs; the 15s
                        // status poll covers the idle case, so don't hammer
                        // /api/float at 1.5s from every open tab
export async function pollFloat(){
  if(!pumpActive)return;
  try{
    const r=await fetch('/api/float');
    if(!r.ok)return;
    const j=await r.json();
    for(const t in (j.floats||{})){
      const fs=document.getElementById('floatstate'+t);
      if(fs)fs.textContent=floatLabel(j.floats[t]);
    }
  }catch(e){}
}
export function renderWater(j){
  const w=j.water;const box=document.getElementById('waterctl');
  if(!w||!w.trays){box.style.display='none';return;}
  box.style.display='';
  {// auto-watering arm/disarm, above everything: it is the switch that matters
   // on a setup tab, the switch arms that setup's pump trays only
   const mine=Object.keys(w.trays).filter(trayInSetup);
   autoWaterTrays=mine;
   const armed=new Set(w.armed||[]);
   const allB=w.auto_blockers||{};
   const blockers=Object.fromEntries(Object.entries(allB).filter(([t])=>mine.includes(t)));
   const names=Object.keys(blockers);
   let row=document.getElementById('autowrow');
   if(!row){
     row=document.createElement('div');
     row.className='waterctl'; row.id='autowrow';
     row.innerHTML='<span class="wtray">Auto-water</span>'
       +'<button type="button" id="autowbtn"></button>'
       +'<span id="autowinfo" class="fhint"></span>';
     box.prepend(row);
     row.querySelector('#autowbtn').addEventListener('click',toggleAutoWater);
   }
   const btn=document.getElementById('autowbtn');
   const info=document.getElementById('autowinfo');
   const on=mine.length>0&&mine.every(t=>armed.has(t));
   autoWaterOn=on;
   row.style.display=mine.length?'':'none';
   btn.textContent=on?'Disarm':'Arm';
   btn.className=on?'on':'';
   btn.disabled=!on && names.length>0;
   if(names.length){
     // never arm on an untrustworthy probe: say exactly which tray and why
     info.innerHTML='<b>Not ready:</b> '+names.map(t=>
       'tray '+esc(t)+' '+esc(blockers[t].join(', '))).join(' \u00b7 ');
     info.className='fhint autowbad';
   }else if(on){
     info.textContent='armed \u00b7 fills a tray when its probe reads below '
       +(w.moisture_threshold_pct)+'%, then waits '+(w.pump_cooldown_min)+' min';
     info.className='fhint';
   }else{
     info.textContent='off \u00b7 ready to arm';
     info.className='fhint';
   }
  }
  {// source reservoir level row, above the tray rows
   const res=w.reservoir||{};
   let row=document.getElementById('resrow');
   if(res.wired&&res.state){
     if(!row){
       row=document.createElement('div');
       row.className='waterctl'; row.id='resrow';
       row.innerHTML='<span class="wtray">Reservoir</span>'
         +'<span class="schip">Level <b id="resstate">--</b></span>'
         +'<span id="resnote" class="fhint"></span>';
       box.prepend(row);
     }
     const st=document.getElementById('resstate');
     const note=document.getElementById('resnote');
     const labels={full:'full',ok:'ok',empty:'EMPTY',fault:'sensor fault'};
     st.textContent=labels[res.state]||res.state;
     st.className=res.state==='empty'?'resbad'
                 :(res.state==='fault'?'reswarn':'resok');
     note.textContent=res.state==='empty'?'pump runs refused until refilled'
                     :(res.state==='fault'?'high sensor wet, low sensor dry - check mounting':'');
   } else if(row){row.remove();}
  }
  const anyRunning=Object.values(w.trays).some(t=>t.running);
  pumpActive=anyRunning;
  for(const t of Object.keys(w.trays).sort()){
    const tw=w.trays[t];
    {const r0=document.getElementById('wrow'+t);if(r0)r0.style.display=trayInSetup(t)?'':'none';}
    let row=document.getElementById('wrow'+t);
    if(!row){
      row=document.createElement('div');
      row.className='waterctl'; row.id='wrow'+t;
      row.innerHTML=
        `<span class="wtray" id="wlabel${t}">Tray ${t}</span>`
        +`<span class="schip">Float <b id="floatstate${t}">--</b></span>`
        +`<span class="schip">Pump today <b id="pumptoday${t}">0</b><span class="u">s</span></span>`
        +`<button type="button" id="fillbtn${t}">Fill to float</button>`
        +`<button type="button" id="pumpbtn${t}">Test pump</button>`
        +`<input type="number" id="pumpsecs${t}" value="3" min="1" max="20" aria-label="Tray ${t} pump seconds">`
        +`<span class="u">s</span>`
        +`<span id="pumpinfo${t}" role="status"></span>`;
      box.appendChild(row);
      document.getElementById('fillbtn'+t).addEventListener('click',()=>waterAct(t,{until_full:true},'filling...'));
      document.getElementById('pumpbtn'+t).addEventListener('click',()=>{
        const secs=+document.getElementById('pumpsecs'+t).value||3;
        waterAct(t,{seconds:secs},'starting...');});
    }
    // the pump and float belong to the tray: name the row after it
    const lbl=document.getElementById('wlabel'+t);
    if(lbl)lbl.textContent=trayLabels[t]||('Tray '+t);
    document.getElementById('floatstate'+t).textContent=floatLabel(tw.float);
    document.getElementById('pumptoday'+t).textContent=tw.today_seconds;
    const pb=document.getElementById('pumpbtn'+t), fb=document.getElementById('fillbtn'+t);
    const dead=!tw.pump_hw;
    pb.disabled=fb.disabled=dead||anyRunning;   // one pump at a time
    pb.textContent=tw.running?'Pumping...':'Test pump';
    fb.textContent=tw.running?'Filling...':'Fill to float';
    const info=document.getElementById('pumpinfo'+t);
    if(dead)info.textContent='no pump hardware';
    else if(tw.last&&!tw.running){
      // when as well as what: "how long since it was watered" is the question
      // the tray card is usually asked, and it now survives a restart
      // agoStr takes a timestamp in milliseconds; last_run is in seconds
      const ago=tw.last_run ? agoStr(tw.last_run*1000) : '';
      info.textContent='last: '+tw.last+(ago?` \u00b7 ${ago}`:'');
    }
  }
}
export let lightBackend='pwm';  // 'kasa' means on/off only: no slider, no sweep
export let autoWaterOn=false;   // whether this tab's pump trays are all armed
export let autoWaterTrays=[];   // the pump trays the Arm switch covers on this tab
export async function toggleAutoWater(){
  const info=document.getElementById('autowinfo');
  const want=!autoWaterOn;
  if(want && !confirm('Arm auto-watering? The controller will fill a tray on its '
     +'own when the probe reads dry.'))return;
  try{
    const r=await fetch('/api/auto_water',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({enabled:want,trays:autoWaterTrays})});
    const j=await r.json().catch(()=>({}));
    if(r.status===401){if(info)info.textContent='log in first';return;}
    if(!j.ok&&info){info.textContent=j.error||'failed';info.className='fhint autowbad';}
  }catch(e){if(info)info.textContent='request failed';}
  refresh();
}

export async function waterAct(tray, body, msg){
  const info=document.getElementById('pumpinfo'+tray);
  if(info)info.textContent=msg;
  pumpActive=true;   // watch the float live from the moment the run starts
  try{
    const r=await fetch('/api/pump',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({...body, tray})});
    const j=await r.json().catch(()=>({}));
    if(r.status===401){if(info)info.textContent='log in to run the pump';return;}
    if(!j.ok&&info)info.textContent=j.error||('HTTP '+r.status);
    // progress/result arrives via the status poll (running/last) + live float
  }catch(e){if(info)info.textContent='request failed';}
}
export async function calibrateProbe(tray, point, force, volts){
  const info=document.getElementById('probecalinfo');
  if(info){
    info.className='';
    info.textContent=force ? 'storing it\u2026'
      : 'watching the probe for 15s to be sure it has settled\u2026';
  }
  try{
    const r=await fetch('/api/probe_cal',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({tray,point,force:!!force,
                           volts:(force&&volts!=null)?volts:undefined})});
    const j=await r.json().catch(()=>({}));
    if(r.status===401){if(info)info.textContent='log in to calibrate';return;}
    if(!info)return;
    if(j.ok){
      const extra=(j.notes&&j.notes.length)?' \u00b7 '+esc(j.notes.join('; ')):'';
      info.className=j.forced?'calwarnmsg':'calokmsg';
      info.innerHTML=`Tray ${esc(tray)} ${esc(point)} anchor set to `
        +`<b>${j.volts}V</b>${j.forced?' (stored despite the warning)':''}${extra}`;
      refresh();
      return;
    }
    // refused: say why, and offer the override rather than hiding it
    info.className='calbadmsg';
    const why=(j.problems&&j.problems.length)?j.problems:[j.error||'could not capture'];
    info.innerHTML='<b>Not stored.</b> '+esc(why.join(' Also: '))
      + (j.can_force ? ` <button type="button" class="calforce" `
          +`data-tray="${esc(tray)}" data-point="${esc(point)}" `
          +`data-volts="${j.volts}">store ${j.volts}V anyway</button>` : '');
  }catch(e){if(info)info.textContent='calibration failed';}
}
export async function checkTempComp(tray, apply){
  const info=document.getElementById('probetcinfo');
  if(info)info.textContent='analyzing\u2026';
  try{
    const r=await fetch('/api/probe_tempcomp',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({tray, hours:72, apply:!!apply})});
    const j=await r.json().catch(()=>({}));
    if(!info)return;
    if(r.status===401){info.textContent='log in first';return;}
    if(!j.ok){info.textContent=j.error||('HTTP '+r.status);return;}
    if(j.applied){info.textContent=`Tray ${tray}: applied ${j.coeff} V/F`;refresh();return;}
    const strong=Math.abs(j.r)>0.7 && j.span>=5;
    info.innerHTML=`Tray ${tray}: ${j.coeff} V/F (r=${j.r}, ${j.span}\u00b0F span, n=${j.n})`
      + (j.warning?` \u2013 ${j.warning}`:'')
      + (strong?` <a href="#" id="tcapply${tray}">apply</a>`:'');
    const a=document.getElementById('tcapply'+tray);
    if(a)a.addEventListener('click',e=>{e.preventDefault();checkTempComp(tray,true);});
  }catch(e){if(info)info.textContent='request failed';}
}

// setters: other modules cannot assign an imported binding
export function set_camHealth(v){ camHealth=v; return v; }
export function set_chartHours(v){ chartHours=v; return v; }
export function set_lastHostLoad(v){ lastHostLoad=v; return v; }
export function set_lightBackend(v){ lightBackend=v; return v; }
export function set_units(v){ units=v; return v; }
