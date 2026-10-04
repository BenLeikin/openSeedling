// openSeedling dashboard: The status object, the schedule curve and the lighting-stage graphics.
// An ES module: what it uses from the others is imported at the top, and what
// it offers is exported. Another module cannot assign one of its variables
// directly; it calls the set_<name>() exported at the bottom. Code that runs
// at page load is in start(), which main.js calls in a fixed order.
import { ctlTarget } from './setups.js';
import { aligning, canEdit, cropping, parseRoi } from './photos.js';
import { camHealth, capMin, capOn, lightBackend, set_camHealth } from './charts.js';
import { esc } from './trays.js';
import { drawGrid, grid, gridEditable, refresh } from './grid.js';
import { durStr } from './cards.js';

export let S=null;

export function fmt(d){return d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});}
export function mins(d){return d.getHours()*60+d.getMinutes()+d.getSeconds()/60;}

export function curve(t,on,off,ramp,max){
  if(t<=on||t>=off)return 0;
  const fs=on+ramp, fe=off-ramp;
  if(fs>=fe){const mid=(on+off)/2;
    return t<=mid?max*(t-on)/(mid-on):max*(off-t)/(off-mid);}
  if(t<fs)return max*(t-on)/ramp;
  if(t>fe)return max*(off-t)/ramp;
  return max;
}

export function draw(){
  if(!S)return;
  const W=640,H=240,L=34,R=12,T=20,B=34;
  const on=mins(S.on),off=mins(S.off),now=mins(S.now);
  const x=m=>L+(W-L-R)*m/1440, y=p=>H-B-(H-T-B)*p/100;
  let pts=[];
  for(let m=0;m<=1440;m+=4)pts.push([x(m),y(curve(m,on,off,S.ramp,S.max))]);
  const line=pts.map((p,i)=>(i?'L':'M')+p[0].toFixed(1)+' '+p[1].toFixed(1)).join('');
  const area=line+`L${x(1440)} ${y(0)} L${x(0)} ${y(0)} Z`;
  const hours=[0,6,12,18,24].map(h=>
    `<text x="${x(h*60)}" y="${H-12}" text-anchor="middle" font-size="11" fill="#3f7d45">${String(h).padStart(2,'0')}:00</text>
     <line x1="${x(h*60)}" y1="${T}" x2="${x(h*60)}" y2="${H-B}" stroke="#d8e6cd" stroke-width="1"/>`).join('');
  const sunM=mins(S.sunrise),setM=mins(S.sunset);
  document.getElementById('chart').innerHTML=`
    <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#e8b04b" stop-opacity=".85"/>
      <stop offset="55%" stop-color="#7fb069" stop-opacity=".75"/>
      <stop offset="100%" stop-color="#3f7d45" stop-opacity=".35"/>
    </linearGradient></defs>
    ${hours}
    <line x1="${L}" y1="${y(0)}" x2="${W-R}" y2="${y(0)}" stroke="#27432e" stroke-width="1.5"/>
    <path d="${area}" fill="url(#g)"/>
    <path d="${line}" fill="none" stroke="#27432e" stroke-width="2"/>
    <text x="${x(sunM)}" y="${T-5}" font-size="14" text-anchor="middle">&#127774;</text>
    <text x="${x(setM)}" y="${T-5}" font-size="14" text-anchor="middle">&#127771;</text>
    <line x1="${x(now)}" y1="${T}" x2="${x(now)}" y2="${y(0)}" stroke="#b3543a" stroke-width="2"/>
    <circle class="nowdot" cx="${x(now)}" cy="${y(curve(now,on,off,S.ramp,S.max))}" r="6"
            fill="#b3543a" stroke="#fff" stroke-width="2"/>
    <text x="${L}" y="${y(100)-4}" font-size="11" fill="#3f7d45">${S.max}%</text>
    ${schedHandles(x,y,on,off,T,H,B)}`;
  bindSchedDrag();
}
// the on/off edges are draggable in the modes where those are real settings
export function schedDraggable(){
  return canEdit && S && (S.schedule_mode==='fixed'||S.schedule_mode==='duration'
    ||(S.schedule_mode==='light2'&&S.off>S.on));
}
export function schedHandles(x,y,on,off,T,H,B){
  if(!schedDraggable())return '';
  const top=T, bot=H-B;
  const h=(m,id,label)=>`
    <g class="schandle" data-edge="${id}" style="cursor:ew-resize">
      <line x1="${x(m)}" y1="${top}" x2="${x(m)}" y2="${bot}"
            stroke="#27432e" stroke-width="1.5" stroke-dasharray="4 3"/>
      <rect x="${x(m)-7}" y="${top-14}" width="14" height="14" rx="3" fill="#27432e"/>
      <text x="${x(m)}" y="${top-3}" text-anchor="middle" font-size="10" fill="#fff">${label}</text>
      <rect class="schit" x="${x(m)-14}" y="${top}" width="28" height="${bot-top}"
            fill="transparent"/>
    </g>`;
  // in duration mode the start edge sets the day length, the end edge moves the anchor
  return h(on,'on',S.schedule_mode==='duration'?'\u21c6':'\u25b8')+h(off,'off','\u25c2');
}
export let schedDrag=null;
export function bindSchedDrag(){
  const svg=document.getElementById('chart');
  if(!svg||svg.dataset.schedBound)return;
  svg.dataset.schedBound='1';
  const W=640,L=34,R=12;
  const xToMin=ev=>{
    const r=svg.getBoundingClientRect();
    const px=(ev.clientX-r.left)/r.width*W;          // viewBox units
    const m=(px-L)/(W-L-R)*1440;
    return Math.max(0,Math.min(1439,Math.round(m/5)*5));   // snap to 5 min
  };
  svg.addEventListener('pointerdown',ev=>{
    const g=ev.target.closest('.schandle');
    if(!g||!schedDraggable())return;
    schedDrag={edge:g.dataset.edge};
    svg.setPointerCapture&&svg.setPointerCapture(ev.pointerId);
    ev.preventDefault();
  });
  svg.addEventListener('pointermove',ev=>{
    if(!schedDrag)return;
    const m=xToMin(ev);
    // live preview: move the local window and redraw without waiting for a save
    if(schedDrag.edge==='on')S.on=minsToDate(S.on,m);
    else S.off=minsToDate(S.off,m);
    schedDrag.value=m;
    draw();
    const info=document.getElementById('lightinfo');
    if(info)info.textContent=`${fmt(S.on)} \u2192 ${fmt(S.off)} `
      +`(${durStr(Math.max(0,S.off-S.on))})`;
  });
  const finish=async ev=>{
    if(!schedDrag)return;
    const edge=schedDrag.edge, m=schedDrag.value;
    schedDrag=null;
    if(m==null){refresh();return;}
    const hhmm=`${String(Math.floor(m/60)).padStart(2,'0')}:${String(m%60).padStart(2,'0')}`;
    const body={};
    if(S.schedule_mode==='light2'){
      body[edge==='on'?'light2_start':'light2_end']=hhmm;
      try{
        const r=await fetch('/api/settings',{method:'POST',
          headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        const j=await r.json().catch(()=>({}));
        const info=document.getElementById('lightinfo');
        if(info&&j.ok===false)info.textContent=(j.errors&&Object.values(j.errors)[0])||'could not update the schedule';
      }catch(e){}
      refresh();
      return;
    }
    if(S.schedule_mode==='fixed'){
      body[edge==='on'?'fixed_on':'fixed_off']=hhmm;
    }else{                                    // duration: end anchors, start sets length
      if(edge==='off')body.duration_end=hhmm;
      else body.duration_hours=Math.max(0,(S.off-S.on)/3600000);
    }
    try{
      const r=await fetch('/api/schedule',{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      const j=await r.json().catch(()=>({}));
      const info=document.getElementById('lightinfo');
      if(info&&!j.ok)info.textContent=j.error||'could not update the schedule';
    }catch(e){}
    refresh();
  };
  svg.addEventListener('pointerup',finish);
  svg.addEventListener('pointercancel',finish);
}
export function minsToDate(ref,m){
  const d=new Date(ref);
  d.setHours(Math.floor(m/60),m%60,0,0);
  return d;
}

// ---- lighting-stage graphics: graphic follows the actual phase ----
export const BULB={
  day:'<svg viewBox="0 0 100 100" class="sunsvg"><g class="raygroup"><path d="M61.0 32.0 Q50.0 3.0 50.0 3.0 L39.0 32.0 Z"/><path d="M68.5 39.9 Q73.5 9.3 73.5 9.3 L49.5 28.9 Z"/><path d="M71.1 50.5 Q90.7 26.5 90.7 26.5 L60.1 31.5 Z"/><path d="M68.0 61.0 Q97.0 50.0 97.0 50.0 L68.0 39.0 Z"/><path d="M60.1 68.5 Q90.7 73.5 90.7 73.5 L71.1 49.5 Z"/><path d="M49.5 71.1 Q73.5 90.7 73.5 90.7 L68.5 60.1 Z"/><path d="M39.0 68.0 Q50.0 97.0 50.0 97.0 L61.0 68.0 Z"/><path d="M31.5 60.1 Q26.5 90.7 26.5 90.7 L50.5 71.1 Z"/><path d="M28.9 49.5 Q9.3 73.5 9.3 73.5 L39.9 68.5 Z"/><path d="M32.0 39.0 Q3.0 50.0 3.0 50.0 L32.0 61.0 Z"/><path d="M39.9 31.5 Q9.3 26.5 9.3 26.5 L28.9 50.5 Z"/><path d="M50.5 28.9 Q26.5 9.3 26.5 9.3 L31.5 39.9 Z"/></g>'
     +'<circle class="sundisk" cx="50" cy="50" r="23"/>'
     +'<g class="face"><circle cx="43" cy="48" r="3"/><circle cx="57" cy="48" r="3"/>'
     +'<path d="M44 55 Q50 61 56 55" fill="none" stroke-width="2.6" stroke-linecap="round"/></g></svg>',
  rise:'<svg viewBox="0 0 100 100" class="risesvg"><g class="rays rerays">'
     +'<line x1="50" y1="28" x2="50" y2="14"/><line x1="34" y1="34" x2="26" y2="23"/>'
     +'<line x1="66" y1="34" x2="74" y2="23"/><line x1="28" y1="46" x2="14" y2="41"/>'
     +'<line x1="72" y1="46" x2="86" y2="41"/></g>'
     +'<circle class="redisk" cx="50" cy="50" r="17"/>'
     +'<g class="reface"><circle cx="44" cy="48" r="2.5"/><circle cx="56" cy="48" r="2.5"/>'
     +'<path d="M45 55 Q50 60 55 55" fill="none" stroke-width="2.4" stroke-linecap="round"/></g>'
     +'<line class="horizon" x1="8" y1="73" x2="92" y2="73"/></svg>',
  set:'<svg viewBox="0 0 100 100" class="setsvg"><g class="rays serays">'
     +'<line x1="50" y1="30" x2="50" y2="18"/><line x1="35" y1="36" x2="28" y2="26"/>'
     +'<line x1="65" y1="36" x2="72" y2="26"/><line x1="29" y1="48" x2="16" y2="44"/>'
     +'<line x1="71" y1="48" x2="84" y2="44"/></g>'
     +'<circle class="sedisk" cx="50" cy="54" r="17"/>'
     +'<g class="seface"><circle cx="44" cy="52" r="2.5"/><circle cx="56" cy="52" r="2.5"/>'
     +'<path d="M45 59 Q50 63 55 59" fill="none" stroke-width="2.4" stroke-linecap="round"/></g>'
     +'<line class="horizon" x1="8" y1="73" x2="92" y2="73"/></svg>'
};
export function moonPhase(date){
  // illuminated fraction (0 new .. 1 full) and waxing flag, from the synodic month
  const synodic=29.530588853;
  const knownNew=Date.UTC(2000,0,6,18,14,0)/86400000;  // a reference new moon (days)
  const days=date.getTime()/86400000;
  let age=((days-knownNew)%synodic+synodic)%synodic;
  return {fraction:(1-Math.cos(2*Math.PI*age/synodic))/2, waxing:age<synodic/2, age};
}
export function litMoonPath(cx,cy,R,f,waxing){
  if(f<=0.005)return '';                                   // new moon: nothing lit
  if(f>=0.995)return `M ${cx} ${cy-R} A ${R} ${R} 0 1 1 ${cx} ${cy+R} A ${R} ${R} 0 1 1 ${cx} ${cy-R} Z`;
  const rx=(R*Math.abs(2*f-1)).toFixed(2);
  const litRight=waxing, gibbous=f>0.5;
  const outer=litRight?1:0;
  const bulgeRight=litRight?!gibbous:gibbous;              // crescent bulges to lit side; gibbous to dark
  const inner=bulgeRight?0:1;
  return `M ${cx} ${cy-R} A ${R} ${R} 0 0 ${outer} ${cx} ${cy+R} A ${rx} ${R} 0 0 ${inner} ${cx} ${cy-R} Z`;
}
export function moonSvg(){
  const {fraction,waxing}=moonPhase(new Date());
  const cx=50,cy=48,R=30, lit=litMoonPath(cx,cy,R,fraction,waxing);
  return '<svg viewBox="0 0 100 100" class="moonsvg">'
    +`<circle class="moondark" cx="${cx}" cy="${cy}" r="${R}"/>`
    +(lit?`<path class="moonlit" d="${lit}"/>`:'')
    +'<path class="star" d="M84 20 l1.5 3.4 3.4 1.5 -3.4 1.5 -1.5 3.4 -1.5 -3.4 -3.4 -1.5 3.4 -1.5 z"/>'
    +'<circle class="star" cx="77" cy="38" r="1.6"/>'
    +'<circle class="star" cx="86" cy="56" r="1.4"/></svg>';
}

export function setBulb(stage){
  const el=document.getElementById('bulb');
  if(!el||el.dataset.stage===stage)return;   // only swap on change (keeps animation steady)
  el.dataset.stage=stage;
  el.className='sun is-'+stage;
  el.innerHTML=stage==='night'?moonSvg():(BULB[stage]||BULB.day);
}

export function phaseOf(){
  const on=mins(S.on),off=mins(S.off),now=mins(S.now);
  if(now<on)return['Night','Lights come on at '+fmt(S.on),'night'];
  if(now<on+S.ramp)return['Morning ramp','Full brightness at '+fmt(new Date(S.on.getTime()+S.ramp*60000)),'rise'];
  if(now<off-S.ramp)return['Full light','Evening ramp begins at '+fmt(new Date(S.off.getTime()-S.ramp*60000)),'day'];
  if(now<off)return['Evening ramp','Lights off at '+fmt(S.off),'set'];
  return['Night','Lights come on tomorrow around '+fmt(S.on),'night'];
}

export function render(){
  if(!S)return;
  document.getElementById('pct').textContent=Math.round(S.brightness)+'%';
  {const ph=document.querySelector('.aphase');
   if(ph)ph.style.setProperty('--lum',(S.brightness/100).toFixed(2));}
  {// A status poll issued BEFORE the mode POST can land after it, carrying the
   // old override and snapping the buttons back. Hold the chosen mode until
   // the server reports it.
   let m=S.light_override;
   if(pendingMode!=null){
     if(m===pendingMode)pendingMode=null;      // server agrees; release
     else m=pendingMode;
   }
   showLightMode(m, S.manual_bright, true);}   // from a status: may release
  document.getElementById('bulb').style.setProperty('--glow',S.brightness/100);
  const[p,n,stage]=phaseOf();
  document.getElementById('phase').textContent=p;
  document.getElementById('next').textContent=n;
  setBulb(stage);
  const dayLen=(S.off-S.on)/60000;
  document.getElementById('facts').innerHTML=`
    <dt>&#127774; Sunrise</dt><dd>${fmt(S.sunrise)}</dd>
    <dt>&#127771; Sunset</dt><dd>${fmt(S.sunset)}</dd>
    <dt>&#128161; Lights on</dt><dd>${fmt(S.on)}</dd>
    <dt>&#128164; Lights off</dt><dd>${fmt(S.off)}</dd>
    <dt>&#127804; Photoperiod</dt><dd>${Math.floor(dayLen/60)}h ${Math.round(dayLen%60)}m</dd>
    <dt>&#9202; Ramp length</dt><dd>${S.ramp} min</dd>`;
  draw();
}

export let lightMode='auto', dragging=false;
export let pendingMode=null;   // mode the user just picked, held until status agrees
export function showLightMode(mode, bright, fromStatus){
  mode = mode || 'auto';
  lightMode = mode;
  document.querySelectorAll('.lcbtn:not(.fanbtn):not(.heatbtn)').forEach(b=>
    b.classList.toggle('on', b.dataset.mode===mode));
  const info=document.getElementById('lightinfo');
  if(info){
    let t=(mode==='on'||mode==='off') ? 'holding '+mode+' - schedule paused' : '';
    if(lightBackend==='kasa'){
      // the plug is a network dependency: say plainly when it isn't answering
      const k=(S&&S.kasa)||{};
      if(k.ok===false)t=(t?t+' \u00b7 ':'')+'plug not responding: '+(k.error||'');
      else t=(t?t+' \u00b7 ':'')+'smart plug'+(k.on==null?'':(k.on?' on':' off'));
    }
    // Below the driver's minimum every setting gives the same light, so say
    // so rather than letting the slider imply a dimness it cannot produce.
    const lin=S&&S.light_linear, floor=lin&&lin.min_output_pct;
    const b=S?Number(S.brightness):null;
    if(floor>2 && S && S.light_linear_on && b>0 && b<floor)
      t=(t?t+' \u00b7 ':'')+`below the fixture's minimum: holding `
        +`${Math.round(floor)}%`;
    info.textContent=t;
    info.className=(lightBackend==='kasa'&&S&&S.kasa&&S.kasa.ok===false)?'err':'';
  }
  const wrap=document.getElementById('lcslider');
  const rng=document.getElementById('lcrange');
  const val=document.getElementById('lcval');
  if(!wrap||!rng)return;
  if(lightBackend==='kasa'){
    // the plug cannot dim: showing a brightness slider would be a control that
    // lies about what it does
    wrap.style.display='none';
    return;
  }
  wrap.style.display='';
  const live = mode==='on';
  wrap.classList.toggle('dim', !live);
  rng.disabled = !live;
  // Never move the thumb under the user, and never repaint a status that was
  // built before their change landed: hold the requested value until the
  // server reports it, or until the hold times out.
  if(brightHold!=null){
    // Only a STATUS can release the hold. The reply to the POST echoes the
    // value back, so releasing on that let the next status, still carrying
    // the old brightness, repaint it and snap the slider back.
    if(fromStatus && bright!=null && Math.round(bright)===Math.round(brightHold)){
      brightHold=null;
    }else{
      rng.value=brightHold;
      if(val)val.textContent=Math.round(brightHold)+'%';
      return;
    }
  }
  if(bright!=null && !dragging){
    rng.value=bright;
    if(val)val.textContent=bright+'%';
  }
}
// The brightness the user asked for, held until the server confirms it. A
// status built before the POST landed would otherwise repaint the old value
// and then correct itself, which reads as the slider jumping back.
export let brightHold=null, brightHoldTimer=null;
export function holdBright(v){
  brightHold=v;
  clearTimeout(brightHoldTimer);
  brightHoldTimer=setTimeout(()=>{brightHold=null; refresh();}, 6000);
}

export async function setLight(mode, brightness, quiet){
  if(ctlTarget==='second')return setLight2(mode, brightness, quiet);
  const info=document.getElementById('lightinfo');
  if(info && !quiet)info.textContent='...';
  const body={};
  if(mode!=null)body.mode=mode;
  if(brightness!=null){body.brightness=brightness; holdBright(brightness);}
  try{
    const r=await fetch('/api/light',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const j=await r.json().catch(()=>({}));
    if(r.ok&&j.ok){
      if(j.brightness!=null)holdBright(j.brightness);   // what it accepted
      if(quiet){lightMode=j.mode;}          // mid-drag: don't touch the slider
      else {
        pendingMode=j.mode;
        showLightMode(j.mode, j.brightness);
        refresh();
        // the plug is a network round trip made by the control loop, so the
        // first status after the POST can still show the old plug state
        if(lightBackend==='kasa')setTimeout(refresh, 2500);
      }
    }
    else if(info)info.textContent = r.status===401?'log in to control the light'
                      :('failed: '+(j.error||('HTTP '+r.status)));
  }catch(e){if(info && !quiet)info.textContent='request failed';}
}
// The second light has no /api/light of its own: its mode and brightness are
// the light2_override and light2_bright settings.
export async function setLight2(mode, brightness, quiet){
  const info=document.getElementById('lightinfo');
  const body={};
  if(mode!=null)body.light2_override=mode;
  if(brightness!=null){body.light2_bright=Math.round(brightness);holdBright(body.light2_bright);}
  try{
    const r=await fetch('/api/settings',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const j=await r.json().catch(()=>({}));
    if(r.ok&&j.ok!==false){
      if(mode!=null){pendingMode=mode;showLightMode(mode, brightness!=null?body.light2_bright:S.manual_bright);}
      if(!quiet)refresh();
    }else if(info)info.textContent=r.status===401?'log in to control the light'
      :('failed: '+((j.errors&&Object.values(j.errors)[0])||j.error||('HTTP '+r.status)));
  }catch(e){if(info&&!quiet)info.textContent='request failed';}
}
// While dragging, push the level at ~8/sec so the light tracks the slider
// instead of waiting for release. Trailing call guarantees the final value.
export let dragTimer=null, dragPending=null;
export function pushBrightness(v){
  dragPending=v;
  if(dragTimer)return;
  dragTimer=setTimeout(()=>{
    dragTimer=null;
    const val=dragPending; dragPending=null;
    if(val!=null)setLight(null, val, true);
  }, 120);
}
export function agoStr(d){
  const m=Math.max(0,Math.round((Date.now()-d)/60000));
  if(m<60)return m+' min ago';
  const h=Math.floor(m/60);
  if(h<48)return h+'h '+(m%60)+'m ago';
  return Math.floor(h/24)+' days ago';
}
export function renderPhoto(j){  const card=document.getElementById('photocard');
  const img=document.getElementById('photo');
  set_camHealth(j.camera||null);
  {const warn=document.getElementById('camwarn');
   if(warn){
    let msg='', cls='';
    if(camHealth&&camHealth.fails>0){
      const ago=camHealth.last_ok?agoStr(new Date(camHealth.last_ok)):'never';
      msg=`\u26a0 Camera not responding \u00b7 ${camHealth.fails} failed attempt${camHealth.fails>1?'s':''}`
        +` \u00b7 last good photo ${ago}`
        +(camHealth.last_err?`<br><span class="camerr">${esc(camHealth.last_err)}</span>`:'');
      cls='camwarn err';
    } else if(capOn&&j.latest_photo_time){
      const ageMin=(Date.now()-new Date(j.latest_photo_time))/60000;
      const inDay=j.on&&j.off&&Date.now()>=+new Date(j.on)&&Date.now()<=+new Date(j.off);
      if(inDay&&ageMin>2*capMin){
        msg=`\u23f1 No new photo in ${agoStr(new Date(j.latest_photo_time)).replace(' ago','')} `
          +`(expected every ${capMin} min)`;
        cls='camwarn amber';
      }
    }
    warn.className=cls||'camwarn'; warn.innerHTML=msg;
    warn.style.display=msg?'':'none';
    if(img)img.classList.toggle('camdead', !!(camHealth&&camHealth.fails>0));
  }}
  if(aligning||cropping){card.style.display='';return;}   // those modes own the image
  if(!j.photo_count){
    img.style.display='none';
    document.getElementById('photoinfo').textContent='No photos yet.';
    card.style.display=canEdit?'':'none';   // keep visible so Take photo is reachable
    return;
  }
  img.style.display='';
  card.style.display='';
  // Show the flattened view by default: that is the corrected, top-down image
  // and what the analysis works from. Only while the grid is unlocked (i.e.
  // you are dragging corners) does it fall back to the raw frame, because you
  // cannot place corners on an image that has already been rectified.
  const stamp=j.latest_photo_time||Date.now();
  const editing=gridEditable();
  const flatOn=!(j.settings && j.settings.timelapse_flatten===false);
  window._flatOn=flatOn;
  const flat=flatOn && !editing && grid && grid.corners && grid.corners.length===4;
  // the view crop applies to the unflattened photo; flattened is already the tray
  const roi=(!flat && !editing)?parseRoi(j.settings&&j.settings.roi):null;
  img.dataset.flat=flat?'1':'';
  img.dataset.crop=roi?roi.join(','):'';
  {const rb=document.getElementById('cropreset');
   if(rb)rb.style.display=(j.settings&&parseRoi(j.settings.roi))?'':'none';}
  if(flat){
    img.onerror=()=>{                       // no corners yet, or rectify failed
      if(img.dataset.flat){img.dataset.flat='';img.src='/photo/latest?'+stamp;}
    };
    img.src='/rectified.jpg?t='+encodeURIComponent(stamp);
  }else if(roi){
    img.onerror=()=>{                       // crop failed: show the full frame
      if(img.dataset.crop){img.dataset.crop='';img.src='/photo/latest?'+stamp;drawGrid();}
    };
    img.src='/photo/cropped.jpg?t='+encodeURIComponent(stamp)+'&r='+encodeURIComponent(roi.join(','));
  }else{
    img.onerror=null;
    img.src='/photo/latest?'+stamp;
  }
  const when=j.latest_photo_time?new Date(j.latest_photo_time):null;
  document.getElementById('photoinfo').textContent=
    (when?`Taken ${when.toLocaleString()}`:'')+` \u00b7 ${j.photo_count} photos so far`
    + (img.dataset.flat?' \u00b7 flattened'
       :(img.dataset.crop?' \u00b7 cropped'
       :(flatOn&&!editing?' \u00b7 raw frame (unlock grid to place corners)':' \u00b7 raw frame')));
}
export async function capturePhoto(){
  const btn=document.getElementById('capturebtn');
  const info=document.getElementById('captureinfo');
  if(!btn||btn.disabled)return;
  btn.disabled=true;
  const say=t=>{if(info){info.textContent=t;info.title=t;}};
  say('Capturing\u2026 (~5s)');
  try{
    const r=await fetch('/api/capture',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const j=await r.json();
    if(j.ok){
      say('Saved.');
      await refresh();                       // pulls the new photo + count
      setTimeout(()=>say(''),2500);
    }else{
      say(j.error||('HTTP '+r.status));
    }
  }catch(e){
    say('Request failed.');
  }finally{
    btn.disabled=false;
  }
}

// setters: other modules cannot assign an imported binding
export function set_S(v){ S=v; return v; }
export function set_dragPending(v){ dragPending=v; return v; }
export function set_dragTimer(v){ dragTimer=v; return v; }
export function set_dragging(v){ dragging=v; return v; }
