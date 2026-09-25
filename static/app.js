let S=null;

function fmt(d){return d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});}
function mins(d){return d.getHours()*60+d.getMinutes()+d.getSeconds()/60;}

function curve(t,on,off,ramp,max){
  if(t<=on||t>=off)return 0;
  const fs=on+ramp, fe=off-ramp;
  if(fs>=fe){const mid=(on+off)/2;
    return t<=mid?max*(t-on)/(mid-on):max*(off-t)/(off-mid);}
  if(t<fs)return max*(t-on)/ramp;
  if(t>fe)return max*(off-t)/ramp;
  return max;
}

function draw(){
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
function schedDraggable(){
  return canEdit && S && (S.schedule_mode==='fixed'||S.schedule_mode==='duration'
    ||(S.schedule_mode==='light2'&&S.off>S.on));
}
function schedHandles(x,y,on,off,T,H,B){
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
let schedDrag=null;
function bindSchedDrag(){
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
function minsToDate(ref,m){
  const d=new Date(ref);
  d.setHours(Math.floor(m/60),m%60,0,0);
  return d;
}

// ---- lighting-stage graphics: graphic follows the actual phase ----
const BULB={
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
function moonPhase(date){
  // illuminated fraction (0 new .. 1 full) and waxing flag, from the synodic month
  const synodic=29.530588853;
  const knownNew=Date.UTC(2000,0,6,18,14,0)/86400000;  // a reference new moon (days)
  const days=date.getTime()/86400000;
  let age=((days-knownNew)%synodic+synodic)%synodic;
  return {fraction:(1-Math.cos(2*Math.PI*age/synodic))/2, waxing:age<synodic/2, age};
}
function litMoonPath(cx,cy,R,f,waxing){
  if(f<=0.005)return '';                                   // new moon: nothing lit
  if(f>=0.995)return `M ${cx} ${cy-R} A ${R} ${R} 0 1 1 ${cx} ${cy+R} A ${R} ${R} 0 1 1 ${cx} ${cy-R} Z`;
  const rx=(R*Math.abs(2*f-1)).toFixed(2);
  const litRight=waxing, gibbous=f>0.5;
  const outer=litRight?1:0;
  const bulgeRight=litRight?!gibbous:gibbous;              // crescent bulges to lit side; gibbous to dark
  const inner=bulgeRight?0:1;
  return `M ${cx} ${cy-R} A ${R} ${R} 0 0 ${outer} ${cx} ${cy+R} A ${rx} ${R} 0 0 ${inner} ${cx} ${cy-R} Z`;
}
function moonSvg(){
  const {fraction,waxing}=moonPhase(new Date());
  const cx=50,cy=48,R=30, lit=litMoonPath(cx,cy,R,fraction,waxing);
  return '<svg viewBox="0 0 100 100" class="moonsvg">'
    +`<circle class="moondark" cx="${cx}" cy="${cy}" r="${R}"/>`
    +(lit?`<path class="moonlit" d="${lit}"/>`:'')
    +'<path class="star" d="M84 20 l1.5 3.4 3.4 1.5 -3.4 1.5 -1.5 3.4 -1.5 -3.4 -3.4 -1.5 3.4 -1.5 z"/>'
    +'<circle class="star" cx="77" cy="38" r="1.6"/>'
    +'<circle class="star" cx="86" cy="56" r="1.4"/></svg>';
}

function setBulb(stage){
  const el=document.getElementById('bulb');
  if(!el||el.dataset.stage===stage)return;   // only swap on change (keeps animation steady)
  el.dataset.stage=stage;
  el.className='sun is-'+stage;
  el.innerHTML=stage==='night'?moonSvg():(BULB[stage]||BULB.day);
}

function phaseOf(){
  const on=mins(S.on),off=mins(S.off),now=mins(S.now);
  if(now<on)return['Night','Lights come on at '+fmt(S.on),'night'];
  if(now<on+S.ramp)return['Morning ramp','Full brightness at '+fmt(new Date(S.on.getTime()+S.ramp*60000)),'rise'];
  if(now<off-S.ramp)return['Full light','Evening ramp begins at '+fmt(new Date(S.off.getTime()-S.ramp*60000)),'day'];
  if(now<off)return['Evening ramp','Lights off at '+fmt(S.off),'set'];
  return['Night','Lights come on tomorrow around '+fmt(S.on),'night'];
}

function render(){
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

let lightMode='auto', dragging=false;
let pendingMode=null;   // mode the user just picked, held until status agrees
function showLightMode(mode, bright, fromStatus){
  mode = mode || 'auto';
  lightMode = mode;
  document.querySelectorAll('.lcbtn:not(.fanbtn)').forEach(b=>
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
let brightHold=null, brightHoldTimer=null;
function holdBright(v){
  brightHold=v;
  clearTimeout(brightHoldTimer);
  brightHoldTimer=setTimeout(()=>{brightHold=null; refresh();}, 6000);
}

async function setLight(mode, brightness, quiet){
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
async function setLight2(mode, brightness, quiet){
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
let dragTimer=null, dragPending=null;
function pushBrightness(v){
  dragPending=v;
  if(dragTimer)return;
  dragTimer=setTimeout(()=>{
    dragTimer=null;
    const val=dragPending; dragPending=null;
    if(val!=null)setLight(null, val, true);
  }, 120);
}
function agoStr(d){
  const m=Math.max(0,Math.round((Date.now()-d)/60000));
  if(m<60)return m+' min ago';
  const h=Math.floor(m/60);
  if(h<48)return h+'h '+(m%60)+'m ago';
  return Math.floor(h/24)+' days ago';
}
function renderPhoto(j){  const card=document.getElementById('photocard');
  const img=document.getElementById('photo');
  camHealth=j.camera||null;
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
async function capturePhoto(){
  const btn=document.getElementById('capturebtn');
  const info=document.getElementById('captureinfo');
  if(!btn||btn.disabled)return;
  btn.disabled=true;
  if(info)info.textContent='Capturing\u2026 (~5s)';
  try{
    const r=await fetch('/api/capture',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const j=await r.json();
    if(j.ok){
      if(info)info.textContent='Saved.';
      await refresh();                       // pulls the new photo + count
      setTimeout(()=>{if(info)info.textContent='';},2500);
    }else{
      if(info)info.textContent=j.error||('HTTP '+r.status);
    }
  }catch(e){
    if(info)info.textContent='Request failed.';
  }finally{
    btn.disabled=false;
  }
}
// ---- grow setups: separate areas, each with its own light and sensors ----
// The server sends every setup's measured day and verdict in `setups`; the tab
// chosen here decides which one the Day and Plan cards show and which sensors
// the chips and charts include. A setup with no sensors ticked shows them all.
var setupsList=[], selSetup=null;
try{ selSetup=localStorage.getItem('setup'); }catch(e){}
function curSetup(){
  return setupsList.find(s=>s.id===selSetup)||setupsList[0]||null;
}
function trayInSetup(id){
  const cs=curSetup();
  if(!cs||setupsList.length<2||!cs.trays||!cs.trays.length)return true;
  return cs.trays.includes(String(id));
}
function inSetup(k){
  const cs=curSetup();
  if(!cs||setupsList.length<2)return true;
  // a tray's own sensors (probe, canopy, float) come with the tray
  const m=/^(probe|canopy|float):(.+)$/.exec(k);
  if(m&&cs.trays&&cs.trays.length&&cs.trays.includes(m[2]))return true;
  if(!cs.sensors||!cs.sensors.length)return !(m&&cs.trays&&cs.trays.length);
  if(k===cs.lux||(k==='ppfd'&&cs.lux==='lux'))return true;
  return cs.sensors.includes(k);
}
function applySetup(j){
  setupsList=j.setups||[];
  const cs=curSetup();
  if(cs){
    // the light views read these; point them at the chosen setup
    j.day_light=cs.day; j.light_plan=cs.plan;
    if(j.light_metrics)j.light_metrics={...j.light_metrics,dli:cs.day?cs.day.dli:null};
    setDliBand({dli_target_low:cs.band[0],dli_target_high:cs.band[1]});
  }else setDliBand(j.settings);
  const nav=document.getElementById('setuptabs');
  if(nav){
    if(setupsList.length<2){nav.style.display='none';nav.innerHTML='';}
    else{
      nav.style.display='';
      const html=setupsList.map(s=>`<button type="button" data-id="${esc(s.id)}"`
        +` class="${cs&&s.id===cs.id?'on':''}">${esc(s.name)}</button>`).join('');
      if(nav.innerHTML!==html)nav.innerHTML=html;
    }
  }
  {const lbl=document.getElementById('dlisetup');
   if(lbl)lbl.textContent=(setupsList.length>1&&cs)?' \u00b7 '+cs.name:'';}
  applyLightView(j, cs);
  // the camera's cards and the fan controls live on their own setup's tab
  const multi=setupsList.length>1;
  const camSet=setupsList.find(s=>s.camera), fanSet=setupsList.find(s=>s.fan);
  const camElsewhere=!!(multi&&camSet&&cs&&camSet.id!==cs.id);
  document.body.classList.toggle('camelsewhere', camElsewhere);
  if(camElsewhere)document.body.classList.add('nocam');
  document.body.classList.toggle('fanelsewhere', !!(multi&&fanSet&&cs&&fanSet.id!==cs.id));
  renderSetupConfig(j);
}
// Which light the Light card, the day phase and the schedule chart describe:
// the selected setup's. The second light is shown through the same controls
// by mapping its schedule onto S; writes go to its own settings.
var ctlTarget='main';
function hhmmToday(ref,hhmm){
  const m=/^(\d{1,2}):(\d{2})$/.exec(hhmm||'');
  const d=new Date(ref); if(m)d.setHours(+m[1],+m[2],0,0); return d;
}
function applyLightView(j, cs){
  const l2=j.light2||{};
  ctlTarget=(cs&&cs.light==='second'&&l2.fixture)?'second':'main';
  const tgt=document.getElementById('lctarget');
  if(tgt)tgt.textContent=(setupsList.length>1&&cs&&cs.light_label)?' \u00b7 '+cs.light_label:'';
  const line=document.getElementById('l2line');
  if(line&&setupsList.length>1)line.dataset.hide='1';else if(line)delete line.dataset.hide;
  if(ctlTarget!=='second')return;
  S.brightness=+l2.level||0;
  S.light_override=l2.override||'auto';
  S.manual_bright=+l2.bright||0;
  S.max=+l2.bright||0;
  S.ramp=+l2.ramp_min||0;
  S.on=hhmmToday(S.now,l2.start); S.off=hhmmToday(S.now,l2.end);
  S.schedule_mode='light2';
  S.light_linear_on=false;
  lightBackend='pwm';                 // the second light always dims by PWM
}
{
  const nav=document.getElementById('setuptabs');
  if(nav)nav.addEventListener('click',ev=>{
    const b=ev.target.closest('button[data-id]');if(!b)return;
    selSetup=b.dataset.id;
    try{localStorage.setItem('setup',selSetup);}catch(e){}
    window._chartCtxSynced=false;
    refresh();
    renderChartGrid();
  });
}
// Settings, Setups: an editable copy, redrawn from the server only when the
// user is not in the middle of changing it
var setupDraft=null, setupDirty=false, lightOpts=[];
// the lights as physical fixtures ("AC fixture (dim line)", "5V LED panel"),
// from the server, which knows the wiring; a stored choice the hardware no
// longer offers stays listed so saving does not silently drop it
function lightChoices(cur){
  const out=lightOpts.map(o=>[o.value,o.label]);
  if(cur&&!out.some(o=>o[0]===cur))out.push([cur,cur==='second'?'Second light (not available)':cur]);
  out.push(['','None']);
  return out;
}
var trayOpts=[], setupOptSig='';
function renderSetupConfig(j){
  lightOpts=j.light_options||[];
  trayOpts=Object.entries((j.settings&&j.settings.trays)||{}).sort((a,b)=>a[0].localeCompare(b[0]))
    .map(([id,t])=>[id,(t&&t.label)||('Tray '+id)]);
  const box=document.getElementById('setupcfg');
  // what the editor offers: a new tray, a sensor that appeared, a light change
  const optSig=JSON.stringify([lightOpts,trayOpts,Object.keys(sensorData||{}).sort()]);
  const optsChanged=optSig!==setupOptSig; setupOptSig=optSig;
  if(box&&setupDirty){
    // keep the unsaved edits, but show the new choices (unless mid-typing)
    if(optsChanged&&!(document.activeElement&&document.activeElement.closest('#setupcfg')))drawSetupConfig();
    return;
  }
  if(!box)return;
  setupDraft=JSON.parse(JSON.stringify((j.settings&&j.settings.setups&&j.settings.setups.length)
    ? j.settings.setups
    : (j.setups||[]).map(s=>({id:s.id,name:s.name,light:s.light,lux:s.lux,
        k:null,sensors:s.sensors,trays:s.trays||[],fan:!!s.fan,camera:!!s.camera,
        dli_low:s.band[0],dli_high:s.band[1]}))));
  drawSetupConfig();
}
function drawSetupConfig(){
  const box=document.getElementById('setupcfg');
  if(!box||!setupDraft)return;
  const keys=Object.keys(sensorData||{}).filter(k=>!k.startsWith('growth')&&!k.startsWith('dry:')
    &&!k.startsWith('moisture:')).sort();
  const luxKeys=[...new Set(['lux','lux:2',...keys.filter(k=>k.startsWith('lux'))])];
  box.innerHTML=setupDraft.map((s,i)=>`<fieldset class="setupedit" data-i="${i}">
      <div class="frow">
        <div><label>Name <input data-f="name" value="${esc(s.name||'')}" maxlength="40"></label></div>
        <div><label>Light <select data-f="light">
          ${lightChoices(s.light).map(([v,t])=>
            `<option value="${esc(v)}"${(s.light||'')===v?' selected':''}>${esc(t)}</option>`).join('')}
        </select></label></div>
        <div><label>Light sensor <select data-f="lux">
          ${[...luxKeys.map(k=>[k,sensorMeta(k,0).label+' ('+k+')'+(k in (sensorData||{})?'':' \u00b7 not detected')]),['','None']].map(([v,t])=>
            `<option value="${esc(v)}"${(s.lux||'')===v?' selected':''}>${esc(t)}</option>`).join('')}
        </select></label></div>
        <div><label>Lux per &micro;mol (blank = global) <input data-f="k" type="number" min="10" max="200" step="1" value="${s.k==null?'':s.k}"></label></div>
        <div><label>DLI target low <input data-f="dli_low" type="number" min="0.5" max="65" step="0.5" value="${s.dli_low}"></label></div>
        <div><label>DLI target high <input data-f="dli_high" type="number" min="1" max="65" step="0.5" value="${s.dli_high}"></label></div>
      </div>
      <div class="setupsens"><b>Here</b>
        <label><input type="checkbox" data-flag="fan"${s.fan?' checked':''}> Fan</label>
        <label><input type="checkbox" data-flag="camera"${s.camera?' checked':''}> Camera</label>
        <span class="fhint">one setup each; the fan follows this setup's light</span></div>
      <div class="setupsens"><b>Trays</b>${trayOpts.map(([id,lbl])=>`<label><input type="checkbox" data-t="${esc(id)}"`
        +`${(s.trays||[]).includes(id)?' checked':''}> ${esc(lbl)}</label>`).join('')}
        <span class="fhint">none ticked = all trays</span></div>
      <div class="setupsens"><b>Sensors</b>${keys.map(k=>`<label><input type="checkbox" data-k="${esc(k)}"`
        +`${(s.sensors||[]).includes(k)?' checked':''}> ${esc(sensorMeta(k,0).label)}</label>`).join('')}
        <span class="fhint">a tray's probe, float and canopy come with the tray</span></div>
      ${setupDraft.length>1?'<button type="button" class="setuprm">Remove</button>':''}
    </fieldset>`).join('');
}
{
  const box=document.getElementById('setupcfg');
  if(box){
    box.addEventListener('input',ev=>{
      const fs=ev.target.closest('fieldset[data-i]');if(!fs||!setupDraft)return;
      const s=setupDraft[+fs.dataset.i];setupDirty=true;
      const f=ev.target.dataset.f, k=ev.target.dataset.k;
      if(f)s[f]=(f==='dli_low'||f==='dli_high')?parseFloat(ev.target.value)
        :(f==='k'?(ev.target.value===''?null:parseFloat(ev.target.value)):ev.target.value);
      if(k){const set=new Set(s.sensors||[]);ev.target.checked?set.add(k):set.delete(k);s.sensors=[...set];}
      const fl=ev.target.dataset.flag;
      if(fl){
        // one fan, one camera: ticking it here takes it from any other setup
        setupDraft.forEach((o,i)=>{if(i!==+fs.dataset.i)o[fl]=false;});
        s[fl]=ev.target.checked;
        box.querySelectorAll(`input[data-flag="${fl}"]`).forEach(cb=>{
          if(cb!==ev.target)cb.checked=false;});
      }
      const tr=ev.target.dataset.t;
      if(tr){const set=new Set(s.trays||[]);ev.target.checked?set.add(tr):set.delete(tr);s.trays=[...set];}
    });
    box.addEventListener('click',ev=>{
      if(!ev.target.classList.contains('setuprm'))return;
      const fs=ev.target.closest('fieldset[data-i]');
      setupDraft.splice(+fs.dataset.i,1);setupDirty=true;drawSetupConfig();
    });
  }
  const add=document.getElementById('setupadd');
  if(add)add.addEventListener('click',()=>{
    if(!setupDraft)setupDraft=[];
    setupDraft.push({name:'Setup '+(setupDraft.length+1),light:'',lux:'',k:null,sensors:[],trays:[],dli_low:10,dli_high:15});
    setupDirty=true;drawSetupConfig();
  });
  const save=document.getElementById('setupsave');
  if(save)save.addEventListener('click',async()=>{
    const msg=document.getElementById('setupmsg');
    try{
      const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({setups:setupDraft||[]})});
      const j=await r.json().catch(()=>({}));
      if(!r.ok||j.ok===false){if(msg)msg.textContent=(j.errors&&j.errors.setups)||j.error||('HTTP '+r.status);return;}
      if(msg)msg.textContent='Saved.';
      setupDirty=false;refresh();
    }catch(e){if(msg)msg.textContent='Request failed.';}
  });
}

// ---- camera modes: the sizes the USB camera offers, largest first ----
async function loadCameraModes(){
  const sel=document.getElementById('usbmodes');
  if(!sel||sel.dataset.loaded)return;
  sel.dataset.loaded='1';
  try{
    const r=await fetch('/api/camera_modes');const j=await r.json();
    if(!j.modes||!j.modes.length){sel.innerHTML='<option value="">no modes reported</option>';return;}
    sel.innerHTML='<option value="">choose\u2026</option>'+j.modes.map((m,i)=>
      `<option value="${esc(m)}">${esc(m)}${i===0?' (full sensor)':''}</option>`).join('');
  }catch(e){sel.dataset.loaded='';}
}
{
  const sel=document.getElementById('usbmodes');
  if(sel){
    sel.addEventListener('focus',loadCameraModes);
    sel.addEventListener('pointerdown',loadCameraModes);
    sel.addEventListener('change',()=>{
      const m=/^(\d+)x(\d+)$/.exec(sel.value);if(!m)return;
      const f=document.getElementById('cfgform');
      f.elements['usb_width'].value=m[1];f.elements['usb_height'].value=m[2];
    });
  }
}

// ---- view crop: drag a rectangle on the full photo ----
// The crop is a view setting: stored photos stay full, and the snapshot,
// scrubber, video and AI report are cut to it. Only used when photos are
// not flattened (a flattened photo is already just the tray).
function parseRoi(s){
  const m=String(s||'').trim().split(',').map(Number);
  return (m.length===4&&m.every(n=>isFinite(n)&&n>=0&&n<=1)&&m[2]>=0.05&&m[3]>=0.05)?m:null;
}
var cropping=false, cropSel=null, cropDrag=null;   // var: renderPhoto reads it
function drawCrop(){
  const svg=document.getElementById('guidesvg');
  if(!svg)return;
  const r=cropSel;
  svg.innerHTML=r
    ? `<path d="M0 0H1000V1000H0Z M${r[0]*1000} ${r[1]*1000}v${r[3]*1000}h${r[2]*1000}v${-r[3]*1000}Z"`
      +` fill="rgba(0,0,0,.45)" fill-rule="evenodd"/>`
      +`<rect x="${r[0]*1000}" y="${r[1]*1000}" width="${r[2]*1000}" height="${r[3]*1000}" fill="none"`
      +` stroke="#ffd54a" stroke-width="3" stroke-dasharray="12 9" vector-effect="non-scaling-stroke"/>`
    : '';
  const info=document.getElementById('photoinfo');
  if(info)info.textContent=r
    ? `Crop ${Math.round(r[2]*100)}% \u00d7 ${Math.round(r[3]*100)}% of the frame \u00b7 drag to redraw`
    : 'Drag a rectangle over the area to keep';
  if(info&&window._flatOn)info.textContent+=' \u00b7 note: Show photos flattened is on, so the crop applies once it is off';
}
function startCrop(){
  if(cropping)return;
  if(aligning)stopAlign();
  cropping=true;
  const img=document.getElementById('photo');
  img.dataset.flat='';img.dataset.crop='';img.onerror=null;
  img.src='/photo/latest?'+Date.now();          // shown until the live frame lands
  // then a fresh full frame from the camera, uncropped: choose from what the
  // camera sees now, in the same mode the photos are taken in
  fetch('/api/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
    .then(r=>r.json()).then(j=>{if(cropping&&j&&j.ok)img.src='/preview.jpg?'+j.ts;})
    .catch(()=>{});
  document.getElementById('gridsvg').style.display='none';
  const svg=document.getElementById('guidesvg');
  svg.style.display='';svg.style.pointerEvents='auto';svg.style.cursor='crosshair';
  cropSel=parseRoi(document.querySelector('[name=roi]')&&document.querySelector('[name=roi]').value);
  document.getElementById('cropctl').style.display='';
  // only the crop controls while choosing: fewer buttons, no wrapping
  for(const id of ['cropbtn','capturebtn','alignbtn','cropreset']){const b=document.getElementById(id);if(b)b.style.display='none';}
  drawCrop();
}
function stopCrop(){
  cropping=false;cropDrag=null;
  const svg=document.getElementById('guidesvg');
  svg.style.display='none';svg.style.pointerEvents='';svg.style.cursor='';svg.innerHTML='';
  document.getElementById('cropctl').style.display='none';
  for(const id of ['cropbtn','capturebtn','alignbtn']){const b=document.getElementById(id);if(b)b.style.display='';}
  refresh();
}
async function saveCrop(roiStr){
  const info=document.getElementById('photoinfo');
  try{
    const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({roi:roiStr})});
    const j=await r.json().catch(()=>({}));
    if(!r.ok||j.ok===false){if(info)info.textContent='Crop not saved: '+((j.errors&&j.errors.roi)||j.error||('HTTP '+r.status));return;}
    const el=document.querySelector('[name=roi]');if(el)el.value=roiStr;
  }catch(e){if(info)info.textContent='Crop not saved: request failed';return;}
  stopCrop();
}
function cropPoint(ev){
  const b=document.getElementById('guidesvg').getBoundingClientRect();
  return [Math.min(1,Math.max(0,(ev.clientX-b.left)/b.width)),
          Math.min(1,Math.max(0,(ev.clientY-b.top)/b.height))];
}
{
  const svg=document.getElementById('guidesvg');
  if(svg){
    svg.addEventListener('pointerdown',ev=>{
      if(!cropping)return;
      ev.preventDefault();svg.setPointerCapture(ev.pointerId);
      cropDrag=cropPoint(ev);cropSel=null;drawCrop();
    });
    svg.addEventListener('pointermove',ev=>{
      if(!cropping||!cropDrag)return;
      const p=cropPoint(ev);
      const x=Math.min(p[0],cropDrag[0]),y=Math.min(p[1],cropDrag[1]);
      cropSel=[x,y,Math.abs(p[0]-cropDrag[0]),Math.abs(p[1]-cropDrag[1])];
      drawCrop();
    });
    svg.addEventListener('pointerup',()=>{
      if(!cropping)return;
      cropDrag=null;
      if(cropSel&&(cropSel[2]<0.05||cropSel[3]<0.05))cropSel=null;   // a click, not a box
      drawCrop();
    });
  }
  const cb=document.getElementById('cropbtn');
  if(cb)cb.addEventListener('click',startCrop);
  const cs=document.getElementById('cropsave');
  if(cs)cs.addEventListener('click',()=>{
    if(!cropSel){const i=document.getElementById('photoinfo');if(i)i.textContent='Drag a rectangle first, or choose Full frame';return;}
    saveCrop(cropSel.map(v=>v.toFixed(3)).join(','));
  });
  const cf=document.getElementById('cropfull');
  if(cf)cf.addEventListener('click',()=>saveCrop(''));
  const cr=document.getElementById('cropreset');
  if(cr)cr.addEventListener('click',()=>saveCrop(''));
  const cc=document.getElementById('cropcancel');
  if(cc)cc.addEventListener('click',stopCrop);
}
let aligning=false, alignTimer=null;
function drawGuides(){
  const svg=document.getElementById('guidesvg');
  if(!svg)return;
  const el=document.querySelector('[name=roi]');
  const m=((el&&el.value)||'').trim().split(',').map(s=>parseFloat(s));
  let roi='';
  if(m.length===4 && m.every(n=>!isNaN(n)&&n>=0&&n<=1)){
    roi=`<rect x="${m[0]*1000}" y="${m[1]*1000}" width="${m[2]*1000}" height="${m[3]*1000}" `
       +`fill="none" stroke="#ffd54a" stroke-width="3" stroke-dasharray="12 9" vector-effect="non-scaling-stroke"/>`;
  }
  svg.innerHTML=
    '<line x1="333" y1="0" x2="333" y2="1000"/><line x1="667" y1="0" x2="667" y2="1000"/>'+
    '<line x1="0" y1="333" x2="1000" y2="333"/><line x1="0" y1="667" x2="1000" y2="667"/>'+
    '<line x1="500" y1="468" x2="500" y2="532"/><line x1="468" y1="500" x2="532" y2="500"/>'+
    roi;
}
async function alignTick(){
  if(!aligning)return;
  try{
    const r=await fetch('/api/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const j=await r.json();
    if(j.ok){
      const img=document.getElementById('photo');
      img.style.display='';
      img.src='/preview.jpg?'+j.ts;     // busy/error: keep the last frame
      const info=document.getElementById('photoinfo');
      // sharpness score: bigger is sharper for this scene; walk the focus
      // setting and keep whatever maximizes it
      if(info&&j.sharpness!=null)
        info.textContent='Live preview \u00b7 sharpness '+Math.round(j.sharpness)
          +' (higher = sharper)';
    }
  }catch(e){}
  if(aligning)alignTimer=setTimeout(alignTick,1200);
}
function startAlign(){
  if(aligning)return;
  if(cropping)stopCrop();
  aligning=true;
  const btn=document.getElementById('alignbtn');
  if(btn){btn.textContent='\u23F9 Stop align';btn.classList.add('on');}
  document.getElementById('gridsvg').style.display='none';   // hide grid while aiming
  const g=document.getElementById('guidesvg');if(g)g.style.display='';
  drawGuides();
  document.getElementById('photocard').style.display='';
  document.getElementById('photoinfo').textContent='Live preview \u00b7 move the camera; the frame updates';
  alignTick();
}
function stopAlign(){
  aligning=false;
  if(alignTimer){clearTimeout(alignTimer);alignTimer=null;}
  const btn=document.getElementById('alignbtn');
  if(btn){btn.textContent='\uD83C\uDFAF Align';btn.classList.remove('on');}
  const g=document.getElementById('guidesvg');if(g)g.style.display='none';
  refresh();   // restore the normal snapshot and grid overlay
}

// Values the user just saved, held until a status arrives that reflects them.
// Without this, any poll landing between the POST and the server's next status
// repaints the form with the OLD value, so a changed dropdown visibly snaps
// back before snapping forward again.
let pendingSave={};
function formHolds(key, cfg){
  const f=document.getElementById('cfgform');
  if(f&&f.elements[key]&&document.activeElement===f.elements[key])return true;
  if(!(key in pendingSave))return false;
  // eslint-disable-next-line eqeqeq
  if(cfg && String(cfg[key])===String(pendingSave[key])){
    delete pendingSave[key];        // server agrees; stop holding
    return false;
  }
  return true;                      // still stale, keep what the user chose
}

function fillForm(cfg){
  if(!cfg)return;
  const f=document.getElementById('cfgform');
  for(const k of ['latitude','longitude','timezone','max_bright','ramp_min',
                  'sunrise_offset_min','sunset_offset_min',
                  'capture_interval_min','capture_brightness','roi',
                  'lux_to_ppfd_k','canopy_factor','duration_hours','cam_rotate','usb_device',
                  'usb_width','usb_height','usb_exposure_time_absolute','usb_gain',
                  'usb_focus_absolute','usb_white_balance_temperature','humidity_low','humidity_high','fan_humidity_on','fan_min_speed','alert_sustain_min','alert_cooldown_hours',
                  'alert_dry_pct','alert_humidity_high','alert_dli_low','alert_dli_high',
                  'moisture_threshold_pct','pump_cooldown_min',
                  'fill_max_seconds','pump_daily_max_seconds','pump_max_seconds',
                  'probe_median_depth','auto_wet_cal_max_move','light_floor_pct',
                  'live_interval_s',
                  'light2_start','light2_end','light2_bright','light2_ramp_min'])
    if(f.elements[k] && k in cfg && !formHolds(k, cfg))   // absent: leave it
      f.elements[k].value=cfg[k];
  {// schedule mode: populate its fields and show only that mode's block
   const sm=f.elements['schedule_mode'];
   if(sm&&!formHolds('schedule_mode',cfg))sm.value=cfg.schedule_mode||'solar';
   const lb=f.elements['light_backend'];
   if(lb&&!formHolds('light_backend',cfg))lb.value=cfg.light_backend||'pwm';
   for(const k of ['fixed_on','fixed_off','duration_end'])
     if(f.elements[k]&&!formHolds(k,cfg))
       f.elements[k].value=cfg[k]||'';
   showScheduleMode(sm?sm.value:'solar');}
  {const cb=f.elements['camera_backend'];
   if(cb&&document.activeElement!==cb)cb.value=cfg.camera_backend||'rpicam';
   // only show the UVC controls when a USB camera is selected
   document.querySelectorAll('.usbonly').forEach(el=>
     el.style.display=(cb&&cb.value==='usb')?'':'none');
   for(const [name] of USB_AUTO){
     const el=f.elements[name];
     if(el&&document.activeElement!==el)el.checked=!!cfg[name];
   }
   syncUsbAuto();}
  {const cr=f.elements['cam_rectify'];
   if(cr&&document.activeElement!==cr)cr.checked=cfg.cam_rectify!==false;
   const tf=f.elements['timelapse_flatten'];
   if(tf&&document.activeElement!==tf)tf.checked=cfg.timelapse_flatten!==false;
}
  {const fw=f.elements['fan_with_light'];
   if(fw&&document.activeElement!==fw)fw.checked=cfg.fan_with_light!==false;}
  {const ae=f.elements['alerts_enabled'];
   if(ae&&document.activeElement!==ae)ae.checked=cfg.alerts_enabled!==false;
   const lo=f.elements['soil_temp_low_f'];
   if(lo&&document.activeElement!==lo){
     const fv=+cfg.soil_temp_low_f||0;
     lo.value=fv?Math.round(tFromF(fv)):0;
   }
   const ll=document.getElementById('threshlolbl');
   if(ll)ll.innerHTML=tUnit();}
  {const u=f.elements['units'];
   if(u&&document.activeElement!==u){u.value=cfg.units||'imperial';units=u.value;}
   const th=f.elements['soil_temp_high_f'];
   if(th&&document.activeElement!==th){
     const fv=+cfg.soil_temp_high_f||0;
     th.value=fv?Math.round(tFromF(fv)):0;
   }
   const lbl=document.getElementById('threshlbl');
   if(lbl)lbl.innerHTML=tUnit();}
  if(document.activeElement!==f.elements['capture_enabled'])
    f.elements['capture_enabled'].checked=!!cfg['capture_enabled'];
  if(f.elements['camera_enabled']&&document.activeElement!==f.elements['camera_enabled'])
    f.elements['camera_enabled'].checked=!!cfg['camera_enabled'];
  applyTheme(cfg['theme']||'auto');
  for(const k of ['kasa_host','kasa_user'])
    if(f.elements[k]&&!formHolds(k,cfg))f.elements[k].value=cfg[k]||'';
  if(f.elements['little_buddy']&&!formHolds('little_buddy',cfg))
    f.elements['little_buddy'].checked=cfg['little_buddy']!==false;
  if(f.elements['auto_wet_cal']&&!formHolds('auto_wet_cal',cfg))
    f.elements['auto_wet_cal'].checked=!!cfg['auto_wet_cal'];
  if(f.elements['light_linear_on']&&!formHolds('light_linear_on',cfg))
    f.elements['light_linear_on'].checked=!!cfg['light_linear_on'];
  if(f.elements['dim_below_min']&&!formHolds('dim_below_min',cfg))
    f.elements['dim_below_min'].value=cfg['dim_below_min']||'hold';
  if(f.elements['light2_on']&&!formHolds('light2_on',cfg))
    f.elements['light2_on'].checked=!!cfg['light2_on'];
  if(f.elements['light2_override']&&!formHolds('light2_override',cfg))
    f.elements['light2_override'].value=cfg['light2_override']||'auto';
  buddyOn = cfg['little_buddy']!==false;
  if(f.elements['buddy_model']&&!formHolds('buddy_model',cfg))
    f.elements['buddy_model'].value=cfg['buddy_model']||'sprout';
  buddyPick = cfg['buddy_model']||'sprout';
}

let frames=[],fidx=0,ptimer=null;
function frameLabel(n){
  const m=n.match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})/);
  return m?`${m[2]}/${m[3]} ${m[4]}:${m[5]}`:n;
}
function frameTs(n){
  // filename encodes local capture time; _m suffix (manual) parses the same
  const m=n.match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/);
  if(!m)return null;
  return Math.floor(new Date(+m[1],+m[2]-1,+m[3],+m[4],+m[5],+m[6]).getTime()/1000);
}
let ctxTimer=null;
function loadFrameContext(name){
  // debounced: fires only when scrubbing pauses, never per-frame in playback
  const el=document.getElementById('pctx');
  if(!el)return;
  clearTimeout(ctxTimer);
  ctxTimer=setTimeout(async()=>{
    const ts=frameTs(name);
    if(ts==null){el.textContent='';return;}
    try{
      const r=await fetch('/api/frame_context?ts='+ts);
      const j=await r.json();
      const d=j.readings||{};
      const bits=[];
      if(d.soil_c!=null)bits.push(`soil ${tDisp(d.soil_c).toFixed(1)}${tUnit()}`);
      if(d.air_c!=null)bits.push(`air ${tDisp(d.air_c).toFixed(1)}${tUnit()}`);
      if(d.humidity!=null)bits.push(`${d.humidity.toFixed(0)}% RH`);
      if(d.lux!=null)bits.push(`${Math.round(d.lux).toLocaleString()} lx`);
      el.textContent=bits.length?' \u00b7 '+bits.join(' \u00b7 '):'';
    }catch(e){el.textContent='';}
  },350);
}
function showFrame(){
  if(!frames.length)return;
  document.getElementById('vframe').src='/thumb/'+frames[fidx]+'?v='+thumbsV;
  document.getElementById('scrub').value=fidx;
  document.getElementById('pframe').textContent=
    `${frameLabel(frames[fidx])} \u00b7 ${fidx+1}/${frames.length}`;
  loadFrameContext(frames[fidx]);
  (new Image()).src='/thumb/'+frames[(fidx+1)%frames.length]+'?v='+thumbsV;
}
function stopPlay(){
  if(ptimer){clearInterval(ptimer);ptimer=null;}
  document.getElementById('playbtn').innerHTML='&#9654; Grow';
}
function togglePlay(){
  if(ptimer){stopPlay();return;}
  if(fidx>=frames.length-1)fidx=0;
  document.getElementById('playbtn').innerHTML='&#9208; Pause';
  ptimer=setInterval(()=>{
    if(fidx>=frames.length-1){stopPlay();return;}
    fidx++;showFrame();
  },125);
}
var thumbsV=0;          // bumped by the server when thumbnails are rebuilt
async function loadFrames(){
  if(window._camOn===false)return;
  try{
    const r=await fetch('/api/photos');const j=await r.json();
    const had=frames.length;
    frames=j.names||[];
    if(j.v!=null&&j.v!==thumbsV){thumbsV=j.v;if(had)showFrame();}  // rebuilt: refetch
    const card=document.getElementById('videocard');
    if(frames.length<2){card.style.display='none';return;}
    card.style.display='';
    document.getElementById('scrub').max=frames.length-1;
    if(!had){fidx=frames.length-1;showFrame();}
  }catch(e){}
}
document.getElementById('playbtn').addEventListener('click',togglePlay);
{const rt=document.getElementById('resettlbtn');
 if(rt)rt.addEventListener('click',()=>resetTimelapse(false));}
document.getElementById('renderbtn').addEventListener('click',async()=>{
  const info=document.getElementById('renderinfo');
  const dl=document.getElementById('dlbtn');
  const btn=document.getElementById('renderbtn');
  dl.style.display='none';          // hide download instantly, no race
  btn.disabled=true;
  btn.textContent='\u23F3 Rendering...';
  renderStart=Date.now();clearRenderTimer();renderTimer=setInterval(tickRender,1000);
  info.textContent='Starting render...';
  try{
    const r=await fetch('/api/render',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const j=await r.json();
    if(!r.ok){info.textContent=j.error||'Render failed to start';btn.disabled=false;
      btn.textContent='\uD83C\uDFA5 Render video';}
  }catch(e){info.textContent='Render failed to start';btn.disabled=false;
    btn.textContent='\uD83C\uDFA5 Render video';}
});
let renderTimer=null, renderStart=null;
function clearRenderTimer(){if(renderTimer){clearInterval(renderTimer);renderTimer=null;}}
function tickRender(){
  if(renderStart===null)return;
  const s=Math.floor((Date.now()-renderStart)/1000);
  const mm=String(Math.floor(s/60)).padStart(2,'0'),ss=String(s%60).padStart(2,'0');
  const f=window._renderFrames?` of ${window._renderFrames} frames`:'';
  document.getElementById('renderinfo').textContent=
    `Rendering${f}... ${mm}:${ss} elapsed`;
}
function renderVideoState(j){
  const info=document.getElementById('renderinfo');
  const dl=document.getElementById('dlbtn');
  const btn=document.getElementById('renderbtn');
  const st=j.render||{};
  const running=(st.state==='running');
  btn.disabled=running;
  btn.textContent=running?'\u23F3 Rendering...':'\uD83C\uDFA5 Render video';
  if(running){
    window._renderFrames=st.frames||0;
    if(renderStart===null){
      renderStart=st.started?new Date(st.started).getTime():Date.now();
      clearRenderTimer();renderTimer=setInterval(tickRender,1000);
    }
    tickRender();
  } else {
    clearRenderTimer();renderStart=null;
    if(!canEdit && st.state!=='running'){
      // read-only viewer: hide render chatter, keep the video timestamp
      if(j.video_time){
        const w=new Date(j.video_time);
        info.textContent='Video from '+w.toLocaleString();
      } else info.textContent='';
    } else if(st.state==='error'){
      info.textContent='Render error'+(st.elapsed?` after ${Math.round(st.elapsed)}s`:'')+
        ': '+st.msg;
    } else if(j.video_time){
      const when=new Date(j.video_time);
      info.textContent='Video from '+when.toLocaleString()+
        (st.state==='done'?' \u00b7 '+st.msg:'');
    } else {info.textContent='No video rendered yet';}
  }
  dl.style.display=(!running && j.video_time)?'':'none';
  if(!running && j.video_time)
    dl.href='/video?t='+encodeURIComponent(j.video_time);
}
document.getElementById('scrub').addEventListener('input',ev=>{
  stopPlay();fidx=+ev.target.value;showFrame();
});

// ---------------- auth / read-only ----------------
let canEdit=true, authEnabled=false;
function applyAuth(j){
  authEnabled = !!j.auth_enabled;
  canEdit = (!authEnabled) || !!j.authed;
  document.body.classList.toggle('readonly', authEnabled && !canEdit);
  const box=document.getElementById('authbox');
  box.style.display = authEnabled ? 'flex' : 'none';
  document.getElementById('pw').style.display       = (authEnabled && !canEdit)?'':'none';
  document.getElementById('loginbtn').style.display = (authEnabled && !canEdit)?'':'none';
  document.getElementById('rolabel').style.display  = (authEnabled && !canEdit)?'':'none';
  document.getElementById('logoutbtn').style.display= (authEnabled && canEdit)?'':'none';
}
async function doLogin(){
  const pw=document.getElementById('pw');
  const err=document.getElementById('loginerr');err.textContent='';
  try{
    const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({password:pw.value})});
    if(r.ok){pw.value='';restartStream();refresh();}
    else err.textContent='wrong password';
  }catch(e){err.textContent='login failed';}
}
async function doLogout(){
  try{await fetch('/api/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});}catch(e){}
  restartStream();
  refresh();
}
function initAuth(){
  document.getElementById('loginbtn').addEventListener('click',doLogin);
  document.getElementById('logoutbtn').addEventListener('click',doLogout);
  document.getElementById('pw').addEventListener('keydown',e=>{if(e.key==='Enter')doLogin();});
}

// ---------------- sensors: readout, chart, overlay ----------------
let sensorData={};
let sampleMin=5, capMin=30, capOn=false, camHealth=null, presTrend=null, lightMetrics=null;
let probeCal={}, probeNames={}, probeDefaultCal=null;   // per-tray anchors, labels, fallback
let probeFlags={};             // per-tray below_wet/above_dry from the server
let filteredVals={};           // sensor -> transient-filtered value
function probePct(c, v){
  if(!c || c.wet==null || c.dry==null || (c.dry-c.wet)<0.05) return null;
  return Math.max(0, Math.min(100, 100*(c.dry-v)/(c.dry-c.wet)));
}
function probeMoisture(t, v){
  const p=probePct(probeCal[t], v);
  if(p!=null) return {pct:p, approx:false};
  const d=probePct(probeDefaultCal, v);
  return d==null ? null : {pct:d, approx:true};
}
let chartHours=24;

let units='imperial';
function isMetric(){return units==='metric';}
function c2f(c){return c*9/5+32;}
// display helpers: storage stays Celsius / hPa, only presentation switches
function tDisp(c){return isMetric()?c:c*9/5+32;}
function tUnit(){return isMetric()?'\u00b0C':'\u00b0F';}
function tFromF(f){return isMetric()?(f-32)*5/9:f;}   // an F-stored setting, shown
function tToF(v){return isMetric()?v*9/5+32:v;}       // ...and read back
function pDisp(hpa){return isMetric()?hpa:hpa*0.0295299830714;}
function pUnit(){return isMetric()?'hPa':'inHg';}
function pDec(){return isMetric()?0:2;}
// key -> {group, label, value, unit}

function sensorMeta(key, val){
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
    const nm=(probeNames[t]?probeNames[t]:'Tray '+t)+' canopy';
    return {group:'Growth', label:nm, value:val.toFixed(1), unit:'%',
            title:'share of plant pixels across the whole tray'};
  }
  if(key.startsWith('probe:')){
    const t=key.slice(6);
    const nm=probeNames[t]||('Tray '+t);
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
  return {group:'Other', label:key, value:String(val), unit:''};
}
// Per-cell camera readings, laid out to match the physical trays. Canopy and
// surface dryness live in the same square because they describe the same cell;
// separate wrapped lists made it impossible to see which cell was which.
function sensorLabel(key){
  const m=sensorMeta(key, 0);
  return m ? m.label : key;
}
// Sensor health, contradictions and post-fill verdicts. Collapsed by default:
// when everything is fine this is one line, and it only demands attention when
// something is actually wrong.
function renderQuality(j){
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

function renderSensors(j){
  sensorData=j.sensors||{};
  probeCal=(j.settings&&j.settings.probe_cal)||{};
  probeNames=(j.settings&&j.settings.probe_names)||{};
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
    capOn=!!j.settings.capture_enabled;
  }
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
       const lim=(m.key0&&m.key0.startsWith('canopy:'))?3*capMin:3*sampleMin;
       const isStale=ts&&(Date.now()/1000-ts)>lim*60;
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
const CHART_SECTIONS=[
  // ordered by how often they drive a decision, not by sensor type
  {id:'soil',   title:'Soil',
   match:k=>k.startsWith('temp:soil')||k.startsWith('probe:')},
  {id:'env',    title:'Environment',
   match:k=>k==='temp:air'||k==='humidity'||k==='lux'||k.startsWith('lux:')||k==='ppfd'||k==='pressure'},
  {id:'growth', title:'Canopy',           match:k=>k.startsWith('canopy:')},
  {id:'other',  title:'Other',            match:k=>true},
];
let seriesData={}, chartPlots={}, soilTempHigh=85, soilTempLow=80;
let humHigh=60, humLow=40;
// lux and PPFD are the same measurement in two units, so the lux card names
// both rather than the app drawing two identical charts
function ppfdFromLux(lx){
  const k=Number(S&&S.settings&&S.settings.lux_to_ppfd_k)||0;
  const cf=Number(S&&S.settings&&S.settings.canopy_factor)||1;
  return k>0 ? lx*cf/k : null;
}
function chartHeadUnit(key){
  if(key!=='lux')return chartUnitFor(key);
  return ppfdFromLux(1)==null ? 'lx' : 'lx \u00b7 \u00b5mol/m\u00b2/s';
}

function chartUnitFor(s){
  if(s.startsWith('temp:'))return tUnit();
  if(s.startsWith('humidity')||s.startsWith('canopy:'))return '%';
  if(s.startsWith('probe:')){const t=s.slice(6);
    return (probeCal[t]&&probeCal[t].wet!=null)||probeDefaultCal?'%':'V';}
  if(s.startsWith('lux'))return 'lx';
  if(s==='pressure')return pUnit();
  if(s==='ppfd')return '\u00b5mol/m\u00b2/s';
  return '';
}
function convertFor(s){
  if(s.startsWith('temp:'))return v=>tDisp(v);
  if(s==='pressure')return v=>pDisp(v);
  if(s.startsWith('probe:')){const t=s.slice(6);
    return v=>{const m=probeMoisture(t,v);return m==null?v:m.pct;};}
  return v=>v;
}
let lastChartLoad=0, lastHostLoad=0;
async function loadChart(){
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
let expandedCharts=new Set();
function renderChartGrid(){
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
}
function cssId(k){return k.replace(/[^a-zA-Z0-9]/g,'_');}
function drawMini(key){
  const svg=document.getElementById('cv-'+cssId(key));
  const stat=document.getElementById('cs-'+cssId(key));
  if(!svg)return;
  const card=document.getElementById('cc-'+cssId(key));
  const big=!!(card&&card.classList.contains('expanded'));
  // match the viewBox to the element's real pixel size: one unit = one CSS
  // pixel, so text renders at its natural shape at any card width. A fixed
  // viewBox stretched to fit would smear the labels (badly so on a phone).
  const r=svg.getBoundingClientRect();
  const W=Math.max(200,Math.round(r.width)||320);
  const H=Math.max(80,Math.round(r.height)||(big?300:110));
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
  for(let i=0;i<=2;i++){const yy=P+(H-B-P)*i/2;
    h+=`<line x1="${P}" y1="${yy}" x2="${W-P}" y2="${yy}" stroke="#e6f0de" stroke-width="1"/>`;}
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
  for(const r of runs){
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
  h+=`<text x="${P}" y="${H-6}" font-size="${FS}" fill="#7a8a72">${fmtT(x0)}</text>`;
  h+=`<text x="${W-P}" y="${H-6}" font-size="${FS}" fill="#7a8a72" text-anchor="end">${fmtT(x1)}</text>`;
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
  const lim=key.startsWith('canopy:')?3*capMin:3*sampleMin;
  const lastTs=xs[xs.length-1];
  const isStale=(Date.now()/1000-lastTs)>lim*60;
  svg.classList.toggle('cstale', isStale);
  const ppfdNow = key==='lux' ? ppfdFromLux(cur) : null;
  if(stat)stat.innerHTML=`<b${over?' class="hot"':''}>${cur.toFixed(dec)}</b>`
    +(ppfdNow!=null?` <span class="alt2">${Math.round(ppfdNow)} \u00b5mol</span>`:'')
    +` \u00b7 lo ${lo.toFixed(dec)} \u00b7 hi ${hi.toFixed(dec)}`
    +(isStale?` <span class="stalebadge" title="last point ${agoStr(new Date(lastTs*1000))}">stale</span>`:'');
  chartPlots[key]={unit,dec,W,H,P,B,big,
    alt: key==='lux' ? ppfdFromLux : null,
    pts:data.map(d=>({x:sx(d[0]),y:sy(d[1]),v:d[1],t:d[0]})),
    fmt:t=>new Date(t*1000).toLocaleString([],{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'})};
}
function chartMove(e){
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
  const vl=svg.querySelector('.hvl'),dot=svg.querySelector('.hdot');
  const lbl=svg.querySelector('.hlbl');
  const tip=document.getElementById('charttip');
  if(vl){vl.setAttribute('x1',best.x);vl.setAttribute('x2',best.x);vl.style.display='';}
  if(dot){dot.setAttribute('cx',best.x);dot.setAttribute('cy',best.y);dot.style.display='';}
  // value rides on the crosshair itself, so it reads without a floating tooltip
  if(lbl){
    const txt=lbl.querySelector('text'), rect=lbl.querySelector('rect');
    let s1=`${best.v.toFixed(plot.dec)}${plot.unit}`;
    if(plot.alt){                       // lux: name the PPFD equivalent too
      const p=plot.alt(best.v);
      if(p!=null)s1+=` / ${Math.round(p)} \u00b5mol`;
    }
    const s2=plot.fmt(best.t);
    const label=plot.big?`${s1}  \u00b7  ${s2}`:s1;
    txt.textContent=label;
    const cw=label.length*(plot.big?7.6:5.6)+10, ch=plot.big?22:16;
    // keep the box inside the plot area at either edge
    let bx=best.x+8;
    if(bx+cw>plot.W-plot.P)bx=best.x-cw-8;
    const by=Math.max(plot.P, Math.min(plot.H-plot.B-ch, best.y-ch/2));
    rect.setAttribute('x',bx); rect.setAttribute('y',by);
    rect.setAttribute('width',cw); rect.setAttribute('height',ch);
    txt.setAttribute('x',bx+5); txt.setAttribute('y',by+ch-(plot.big?7:5));
    lbl.style.display='';
  }
  if(tip&&!plot.big){
    tip.textContent=plot.fmt(best.t);
    tip.style.display='';tip.style.left=(e.clientX+12)+'px';tip.style.top=(e.clientY-32)+'px';
  }else if(tip){tip.style.display='none';}
}
function chartLeave(){
  document.querySelectorAll('svg.cmini .hvl, svg.cmini .hdot, svg.cmini .hlbl')
    .forEach(el=>el.style.display='none');
  const tip=document.getElementById('charttip');
  if(tip)tip.style.display='none';
}
function floatLabel(v){
  return v===null ? 'no sensor' : (v>=1 ? 'not full' : 'full');
}
let pumpActive=false;   // float state only changes while a pump runs; the 15s
                        // status poll covers the idle case, so don't hammer
                        // /api/float at 1.5s from every open tab
async function pollFloat(){
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
function renderWater(j){
  const w=j.water;const box=document.getElementById('waterctl');
  if(!w||!w.trays){box.style.display='none';return;}
  box.style.display='';
  {// auto-watering arm/disarm, above everything: it is the switch that matters
   const blockers=w.auto_blockers||{};
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
   const on=!!w.auto_water;
   autoWaterOn=on;
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
    const lbl=document.getElementById('wlabel'+t);
    if(lbl&&probeNames[t])lbl.textContent=probeNames[t];
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
let lightBackend='pwm';  // 'kasa' means on/off only: no slider, no sweep
let autoWaterOn=false;   // mirrors water.auto_water from the last status poll
async function toggleAutoWater(){
  const info=document.getElementById('autowinfo');
  const want=!autoWaterOn;
  if(want && !confirm('Arm auto-watering? The controller will fill a tray on its '
     +'own when the probe reads dry.'))return;
  try{
    const r=await fetch('/api/auto_water',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:want})});
    const j=await r.json().catch(()=>({}));
    if(r.status===401){if(info)info.textContent='log in first';return;}
    if(!j.ok&&info){info.textContent=j.error||'failed';info.className='fhint autowbad';}
  }catch(e){if(info)info.textContent='request failed';}
  refresh();
}

async function waterAct(tray, body, msg){
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
async function calibrateProbe(tray, point, force, volts){
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
async function checkTempComp(tray, apply){
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
// ---- planting map: two trays, editable seed / equipment / sow date ----
let trays={}, trayDirty={}, trayTimer=null, trayPending=0;
function daysSince(iso){
  if(!iso)return null;
  const d=new Date(iso+'T00:00:00'), now=new Date();
  if(isNaN(d))return null;
  return Math.floor((new Date(now.getFullYear(),now.getMonth(),now.getDate())-d)/86400000);
}
function renderTrays(j){
  const t=(j.settings&&j.settings.trays)||{};
  const cs0=curSetup();
  const sig=JSON.stringify(t)+'|'+(setupsList.length>1&&cs0?JSON.stringify(cs0.trays||[]):'');
  const wrap=document.getElementById('trayswrap');
  if(!wrap)return;
  // don't clobber what's being typed, or a click whose save is still in flight
  // (a poll started before the POST would otherwise return pre-save data)
  if(trayPending>0)return;
  if(wrap.dataset.sig===sig && wrap.children.length)return;
  if(document.activeElement && document.activeElement.closest('#trayswrap'))return;
  wrap.dataset.sig=sig;
  trays=JSON.parse(JSON.stringify(t));
  let h='';
  for(const id of Object.keys(trays).sort().filter(trayInSetup)){
    const tr=trays[id]||{}, cells=tr.cells||{};
    const rows=tr.rows||3, cols=tr.cols||4;
    const filled=Object.keys(cells).length;
    h+=`<div class="tray"><div class="tray-head">`
      +`<b>${esc(tr.label||('Tray '+id))}</b>`
      +`<span class="tsum">${filled} of ${rows*cols} cells filled</span>`
      +`</div>`
      +`<div class="tgrid" style="grid-template-columns:repeat(${cols},1fr)">`;
    for(let r=1;r<=rows;r++){
      for(let c=0;c<cols;c++){
        const cid=colL(c)+r, v=cells[cid]||{};
        const sownAge=daysSince(v.planted), sprAge=daysSince(v.sprouted);
        const toSprout=(v.planted&&v.sprouted)
          ? Math.round((new Date(v.sprouted+'T00:00:00')-new Date(v.planted+'T00:00:00'))/86400000)
          : null;
        const filled=!!(v.seed||v.equipment||v.planted||v.sprouted||v.archived);
        const cls=['tcell']; if(filled)cls.push('filled');
        if(v.archived)cls.push('archived'); else if(v.sprouted)cls.push('sprouted');
        // badge: age since sprouting once up, else age since sowing
        const badge=v.archived?'out':(v.sprouted?(sprAge==null?'':sprAge+'d'):(sownAge==null?'':sownAge+'d'));
        // hidden fields stay in the DOM (just not shown) so nothing is lost
        const hid=new Set((v.hide||[]).filter(f=>!f.startsWith('!')));
        // the optional extras stay out of the way until used or switched on
        for(const f of ['count','source','notes'])
          if(!v[f] && !(v.hide||[]).includes('!'+f)) hid.add(f);
        const hcl=f=>hid.has(f)?' fhidden':'';
        h+=`<div class="${cls.join(' ')}" data-tray="${id}" data-cell="${cid}"
              data-sprouted="${esc(v.sprouted||'')}" data-archived="${esc(v.archived||'')}"
              data-seed="${esc(v.seed||'')}"
              data-hide="${esc((v.hide||[]).join(','))}">
              <span class="tid">${cid}<span class="tage">${badge}</span>
                <button type="button" class="tfields editonly" aria-label="Choose fields for ${cid}"
                        title="Choose which fields this cell shows">\u22ef</button></span>
              <div class="tmenu" hidden>
                ${[['seed','Seed'],['equipment','Equipment'],['planted','Sown date'],
                   ['sprouted','Sprout date'],['count','Seed count'],
                   ['source','Seed source'],['notes','Notes']].map(([f,lbl])=>
                  `<label><input type="checkbox" data-field="${f}"${hid.has(f)?'':' checked'}> ${lbl}</label>`).join('')}
              </div>
              <input class="tseed${hcl('seed')}"  type="text" placeholder="seed"      value="${esc(v.seed||'')}">
              <input class="tequip${hcl('equipment')}" type="text" placeholder="equipment" value="${esc(v.equipment||'')}">
              <label class="tdrow trow-planted${hcl('planted')}"><span class="tdlbl">Sown</span>
                <input class="tdate" type="date" value="${esc(v.planted||'')}"></label>
              <label class="tdrow trow-sprouted${hcl('sprouted')}${v.sprouted?'':' fhidden'}"><span class="tdlbl up">Up</span>
                <input class="tdate tsprdate" type="date" value="${esc(v.sprouted||'')}"></label>
              <label class="tdrow trow-count${hcl('count')}"><span class="tdlbl">Seeds</span>
                <input class="tcount" type="number" min="0" max="99"
                       value="${v.count?esc(String(v.count)):''}" placeholder="0"></label>
              <input class="tsource trow-source${hcl('source')}" type="text"
                     placeholder="seed source / year" value="${esc(v.source||'')}">
              <textarea class="tnotes trow-notes${hcl('notes')}" rows="2"
                     placeholder="notes">${esc(v.notes||'')}</textarea>
              <div class="tstat">${
                v.archived ? `transplanted ${v.archived}`
                : (toSprout!=null ? `${toSprout}d to sprout`
                : (v.sprouted ? 'sprouted' : (v.planted?'not up yet':'&nbsp;')))}</div>
              <div class="tacts editonly">
                <button type="button" class="tsprout" data-cell="${cid}" data-tray="${id}"
                        title="${v.sprouted?'Mark as not sprouted':'Mark sprouted today'}"
                        ><span class="blong">${v.sprouted?'Un-sprout':'Sprouted'}</span><span
                         class="bshort">${v.sprouted?'\u21b6':'\u2713'}</span></button>
                <button type="button" class="tmoved" data-cell="${cid}" data-tray="${id}"
                        title="Potted on: record it and empty the cell"
                        ><span class="blong">Transplanted</span><span
                         class="bshort">\u2913</span></button>
                <button type="button" class="tdied" data-cell="${cid}" data-tray="${id}"
                        title="Lost it: record it and empty the cell"
                        ><span class="blong">Died</span><span
                         class="bshort">\u2715</span></button>
              </div>
            </div>`;
      }
    }
    h+='</div></div>';
  }
  wrap.innerHTML=h;
  {const filled=Object.values(trays).reduce((n,t)=>n+Object.keys(t.cells||{}).length,0);
   const hint=document.getElementById('trayhint');
   if(hint)hint.style.display=filled?'none':'';}
}
// Single definition on purpose: a second `function esc` later in the file
// would silently win for the whole scope and (if weaker) let a quote in a
// seed name break out of an HTML attribute.
function esc(s){
  return String(s==null?'':s).replace(/&/g,'&amp;').replace(/"/g,'&quot;')
    .replace(/'/g,'&#39;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function collectTray(id){
  const cells={};
  document.querySelectorAll(`#trayswrap .tcell[data-tray="${id}"]`).forEach(el=>{
    const seed=el.querySelector('.tseed').value.trim();
    const equipment=el.querySelector('.tequip').value.trim();
    const planted=el.querySelector('.tdate:not(.tsprdate)').value;
    const sprEl=el.querySelector('.tsprdate');
    const sprouted=sprEl?sprEl.value:(el.dataset.sprouted||'');
    const archived=el.dataset.archived||'';
    const hide=(el.dataset.hide||'').split(',').filter(Boolean);
    const source=(el.querySelector('.tsource')||{}).value||'';
    const notes=(el.querySelector('.tnotes')||{}).value||'';
    const count=parseInt((el.querySelector('.tcount')||{}).value||0,10)||0;
    if(seed||equipment||planted||sprouted||archived||hide.length||source||notes||count)
      cells[el.dataset.cell]={seed,equipment,planted,sprouted,archived,
                              source:source.trim(),notes:notes.trim(),count,hide};
  });
  return cells;
}
async function saveTray(id){
  const info=document.getElementById('trayinfo');
  trayPending++;
  try{
    const r=await fetch('/api/trays',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({tray:id, cells:collectTray(id)})});
    const j=await r.json().catch(()=>({}));
    if(r.status===401){if(info)info.textContent='log in to edit the map';return;}
    if(info)info.textContent = (r.ok&&j.ok)?`saved (${j.count} cells)`
                                          :('save failed: '+(j.error||r.status));
    if(r.ok&&j.ok)
      setTimeout(()=>{if(info&&/saved/.test(info.textContent))info.textContent='';},2500);
  }catch(e){if(info)info.textContent='save failed';}
  finally{trayPending=Math.max(0,trayPending-1);}
}
async function trayLayout(body){
  const info=document.getElementById('trayaddinfo')||document.getElementById('trayinfo');
  try{
    const r=await fetch('/api/tray_layout',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const j=await r.json().catch(()=>({}));
    if(r.status===401){if(info)info.textContent='log in first';return;}
    if(!j.ok&&j.needs_confirm&&!body.confirm){   // ask once, never loop
      // never destroy planting records without saying exactly what is at stake
      const what=j.dropped?`Cells ${j.dropped.join(', ')} fall outside the new grid`
                          :`This tray has ${j.filled} filled cells`;
      if(window.confirm(`${what}. Continue and discard them?`))
        return trayLayout({...body, confirm:true});
      if(info)info.textContent='cancelled';
      refresh();
      return;
    }
    if(!j.ok&&info){info.textContent=j.error||'failed';refresh();return;}
    if(info)info.textContent='';
    const w=document.getElementById('trayswrap');
    if(w)w.dataset.sig='';
    refresh();
  }catch(e){if(info)info.textContent='request failed';}
}
function renderTrayConfig(cfg){
  const box=document.getElementById('traycfg');
  if(!box)return;
  const trays=(cfg&&cfg.trays)||{};
  const sig=JSON.stringify(Object.entries(trays).map(([id,t])=>
    [id,t.label,t.rows,t.cols,Object.keys(t.cells||{}).length]));
  if(box.dataset.sig===sig)return;               // don't clobber typing
  if(document.activeElement&&document.activeElement.closest('#traycfg'))return;
  box.dataset.sig=sig;
  let h='';
  for(const id of Object.keys(trays).sort()){
    const t=trays[id]||{};
    const filled=Object.keys(t.cells||{}).length;
    const wired=(id==='1'||id==='2');
    h+=`<div class="trayrow" data-tray="${id}">
          <input class="tlabelin" type="text" value="${esc(t.label||('Tray '+id))}"
                 data-tray="${id}" aria-label="Tray ${id} name" maxlength="40">
          <span class="tdims">
            <input class="tdim" type="number" min="1" max="12" value="${t.cols||3}"
                   data-tray="${id}" data-dim="cols" aria-label="Columns">
            <span class="tx">\u00d7</span>
            <input class="tdim" type="number" min="1" max="12" value="${t.rows||4}"
                   data-tray="${id}" data-dim="rows" aria-label="Rows">
          </span>
          <span class="tmeta">${filled} filled${wired?'':' \u00b7 no probe/pump'}</span>
          <button type="button" class="trm" data-tray="${id}" title="Remove tray">\u2715</button>
        </div>`;
  }
  h+=`<div class="trayadd"><button type="button" id="trayaddbtn">+ Add tray</button>
        <span id="trayaddinfo" role="status"></span></div>`;
  box.innerHTML=h;
}
function initTrayConfig(){
  const box=document.getElementById('traycfg');
  if(!box||box.dataset.bound)return;
  box.dataset.bound='1';
  box.addEventListener('click',ev=>{
    if(ev.target.id==='trayaddbtn'){trayLayout({action:'add'});return;}
    const rm=ev.target.closest('.trm');
    if(rm)trayLayout({action:'remove',tray:rm.dataset.tray});
  });
  box.addEventListener('change',ev=>{
    const d=ev.target.closest('.tdim');
    if(d){
      const body={action:'resize',tray:d.dataset.tray};
      body[d.dataset.dim]=parseInt(d.value,10)||1;
      box.dataset.sig='';
      trayLayout(body);
      return;
    }
    const l=ev.target.closest('.tlabelin');
    if(l){box.dataset.sig='';
          trayLayout({action:'resize',tray:l.dataset.tray,label:l.value});}
  });
}
function initTrays(){
  const wrap=document.getElementById('trayswrap');
  if(!wrap||wrap.dataset.bound)return;   // binding twice would make every
  wrap.dataset.bound='1';                // toggle immediately undo itself
  const queue=e=>{
    const cell=e.target.closest('.tcell');
    if(!cell)return;
    cell.classList.toggle('filled', !!collectTray(cell.dataset.tray)[cell.dataset.cell]);
    trayDirty[cell.dataset.tray]=true;
    const info=document.getElementById('trayinfo');
    if(info)info.textContent='saving\u2026';
    clearTimeout(trayTimer);
    trayTimer=setTimeout(()=>{                 // debounce: save after typing stops
      const ids=Object.keys(trayDirty); trayDirty={};
      ids.forEach(saveTray);
    },800);
  };
  wrap.addEventListener('input',queue);
  wrap.addEventListener('change',queue);
  // field menu: open one at a time, close on outside click
  wrap.addEventListener('click',ev=>{
    const fb=ev.target.closest('.tfields');
    if(fb){
      const menu=fb.closest('.tcell').querySelector('.tmenu');
      const wasOpen=!menu.hidden;
      wrap.querySelectorAll('.tmenu').forEach(m=>m.hidden=true);
      menu.hidden=wasOpen;
      ev.stopPropagation();
      return;
    }
    if(!ev.target.closest('.tmenu'))
      wrap.querySelectorAll('.tmenu').forEach(m=>m.hidden=true);
  });
  document.addEventListener('click',ev=>{
    if(!ev.target.closest('#trayswrap'))
      wrap.querySelectorAll('.tmenu').forEach(m=>m.hidden=true);
  });
  wrap.addEventListener('change',ev=>{
    const cb=ev.target.closest('.tmenu input[type=checkbox]');
    if(!cb)return;
    const cell=cb.closest('.tcell');
    const field=cb.dataset.field;
    const hide=new Set((cell.dataset.hide||'').split(',').filter(Boolean));
    if(cb.checked)hide.delete(field); else hide.add(field);
    // remember an explicit "show" for the optional extras, which are hidden by
    // default: without it the next render would hide an empty row again.
    // Must happen before dataset.hide is written, or it never reaches the save.
    if(['count','source','notes'].includes(field)){
      if(cb.checked)hide.add('!'+field); else hide.delete('!'+field);
    }
    cell.dataset.hide=[...hide].join(',');
    // toggle the matching row without re-rendering, so the menu stays put
    const target=field==='seed'?cell.querySelector('.tseed')
      :field==='equipment'?cell.querySelector('.tequip')
      :cell.querySelector('.trow-'+field);
    if(target)target.classList.toggle('fhidden',!cb.checked);
    if(trays[cell.dataset.tray]){
      const tc=(trays[cell.dataset.tray].cells=trays[cell.dataset.tray].cells||{});
      tc[cell.dataset.cell]=Object.assign(
        {seed:'',equipment:'',planted:'',sprouted:'',archived:''},
        tc[cell.dataset.cell]||{}, {hide:[...hide]});
    }
    wrap.dataset.sig=JSON.stringify(trays);
    saveTray(cell.dataset.tray);
  });
  wrap.addEventListener('click',async ev=>{
    const end=ev.target.closest('.tmoved,.tdied');
    if(end){
      const outcome=end.classList.contains('tdied')?'died':'transplanted';
      const cell=end.closest('.tcell');
      const seed=(cell.dataset.seed||'').trim();
      const what=seed?`"${seed}" in ${cell.dataset.cell}`:cell.dataset.cell;
      if(!window.confirm(`Record ${what} as ${outcome} and empty the cell?\n\n`
        +`It stays in the planting history and still counts toward this `
        +`variety's germination rate.`))return;
      // the button keeps focus after a click, and renderTrays deliberately
      // skips re-rendering while focus is inside the tray area (so a poll can't
      // clobber typing). Drop focus or the cell appears to do nothing until the
      // next manual refresh.
      end.blur();
      const tr=cell.dataset.tray, cid=cell.dataset.cell;
      // Hold off tray re-renders until our own refresh lands. A poll started
      // before this POST returns pre-delete data, and without the guard it
      // repaints the cell straight back in.
      // Held across the refresh, not just the POST: releasing it earlier lets a
      // poll that started before the delete repaint the cell straight back in.
      trayPending++;
      let done=false;
      try{
        const r=await fetch('/api/planting_end',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({tray:tr,cell:cid,outcome})});
        const j=await r.json().catch(()=>({}));
        if(j.ok)done=true; else alert(j.error||'Could not record that.');
      }catch(e){alert('Request failed.');}
      if(!done){trayPending=Math.max(0,trayPending-1);return;}
      // empty it locally and on screen now, so the click has a visible effect
      // instead of waiting on a round trip
      if(trays[tr]&&trays[tr].cells)delete trays[tr].cells[cid];
      clearCellUI(cell);
      const wrapEl=document.getElementById('trayswrap');
      if(wrapEl)wrapEl.dataset.sig=JSON.stringify(trays);
      loadPlantings();
      try{ await refresh(); }
      finally{ trayPending=Math.max(0,trayPending-1); }
      return;
    }
    const b=ev.target.closest('.tsprout');
    if(!b)return;
    const cell=b.closest('.tcell');
    const field=b.classList.contains('tsprout')?'sprouted':'archived';
    const today=new Date();
    const iso=`${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,'0')}-`
      +`${String(today.getDate()).padStart(2,'0')}`;
    const val=cell.dataset[field]?'':iso;             // toggle
    cell.dataset[field]=val;
    // the sprout date also has a visible field once set: collectTray reads
    // that, so clearing only the dataset would leave the old date in place
    if(field==='sprouted'){
      const el=cell.querySelector('.tsprdate');
      if(el){
        el.value=val;
        const row=el.closest('.tdrow');
        const hid=new Set((cell.dataset.hide||'').split(',').filter(Boolean));
        if(row)row.classList.toggle('fhidden', !val || hid.has('sprouted'));
      }
    }
    const tray=cell.dataset.tray, cid=cell.dataset.cell;
    // mirror into the local copy: a refresh landing before the save round-trips
    // would otherwise re-render from stale server data and undo the click
    if(trays[tray]){
      const tc=(trays[tray].cells=trays[tray].cells||{});
      tc[cid]=Object.assign({seed:'',equipment:'',planted:'',sprouted:'',archived:''},
                            tc[cid]||{}, {[field]:val});
    }
    wrap.dataset.sig=JSON.stringify(trays);           // keep the guard in step
    saveTray(tray);
  });
}
// ---- planting history ----
// Cells are cleared when a plant is transplanted or lost, so this is where the
// record of what grew where actually lives.
let plantingHistory=[];
async function loadPlantings(){
  try{
    const r=await fetch('/api/plantings');
    const j=await r.json();
    plantingHistory=j.plantings||[];
  }catch(e){plantingHistory=[];}
  renderPlantings();
}
function renderPlantings(){
  const wrap=document.getElementById('histwrap');
  const body=document.getElementById('histbody');
  const sum=document.getElementById('histsummary');
  if(!wrap||!body)return;
  if(!plantingHistory.length){wrap.style.display='none';return;}
  wrap.style.display='';
  const died=plantingHistory.filter(p=>p.outcome==='died').length;
  const moved=plantingHistory.length-died;
  if(sum)sum.textContent=`\u00b7 ${moved} transplanted, ${died} lost`;
  let h='';
  for(const p of plantingHistory){
    const days=(p.planted&&p.sprouted)
      ? Math.round((new Date(p.sprouted)-new Date(p.planted))/86400000) : null;
    const bits=[];
    if(p.planted)bits.push('sown '+esc(p.planted));
    bits.push(p.sprouted?`sprouted ${esc(p.sprouted)}${days!=null?` (${days}d)`:''}`
                        :'never sprouted');
    if(p.ended)bits.push(esc(p.ended));
    h+=`<div class="hrow">
          <span class="hcell">${esc(p.tray)}/${esc(p.cell)}</span>
          <span class="hseed">${esc(p.seed||'(no variety)')}</span>
          <span class="houtcome ${p.outcome==='died'?'bad':'ok'}">${esc(p.outcome)}</span>
          <span class="hwhen">${bits.join(' \u00b7 ')}</span>
          <button type="button" class="hrestore editonly" data-id="${p.id}"
                  title="Put this back in its cell">restore</button>
        </div>`;
  }
  body.innerHTML=h;
  applyAuthTo(body);
}
function applyAuthTo(el){
  el.querySelectorAll('.editonly').forEach(e=>{e.style.display=canEdit?'':'none';});
}
// ---- smart plug setup ----
// Scan, pick, test. The password field is never populated from the server (it
// is redacted like the dashboard hash), so a blank one means "leave it alone"
// rather than "clear it".
async function plugScan(){
  const info=document.getElementById('pluginfo');
  const list=document.getElementById('pluglist');
  const f=document.getElementById('cfgform');
  if(info)info.textContent='scanning the local network\u2026';
  if(list){list.hidden=true;list.innerHTML='';}
  try{
    const r=await fetch('/api/plug_discover',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({user:f.elements['kasa_user'].value,
                           pass:f.elements['kasa_pass'].value})});
    const j=await r.json().catch(()=>({}));
    if(r.status===401){info.textContent='log in first';return;}
    if(!j.ok){info.textContent=j.error||'scan failed';return;}
    const devs=j.devices||[];
    if(!devs.length){info.textContent='no plugs found on this subnet';return;}
    info.textContent=`${devs.length} found \u00b7 pick one to use it`;
    list.innerHTML=devs.map(d=>{
      const why=d.needs_auth ? '<span class="plugauth">needs your TP-Link login</span>'
             : (d.error ? `<span class="plugauth">${esc(d.error)}</span>`
                        : `<span class="plugstate">${d.on?'on':'off'}</span>`);
      return `<button type="button" class="plugpick" data-host="${esc(d.host)}">
                <b>${esc(d.alias||d.model||'plug')}</b>
                <span class="plughost">${esc(d.host)}</span>${why}</button>`;
    }).join('');
    list.hidden=false;
  }catch(e){if(info)info.textContent='scan failed';}
}

async function plugTest(){
  const info=document.getElementById('pluginfo');
  const f=document.getElementById('cfgform');
  if(info)info.textContent='connecting\u2026';
  try{
    const r=await fetch('/api/plug_test',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({host:f.elements['kasa_host'].value,
                           user:f.elements['kasa_user'].value,
                           pass:f.elements['kasa_pass'].value})});
    const j=await r.json().catch(()=>({}));
    if(r.status===401){info.textContent='log in first';return;}
    if(!j.ok){
      info.textContent=(j.error||'could not connect')+(j.hint?' \u2014 '+j.hint:'');
      info.className='fhint plugbad';
      return;
    }
    info.className='fhint plugok';
    info.textContent=`${j.alias||j.model||'plug'} responded \u00b7 currently `
      +`${j.on?'on':'off'} \u00b7 remember to save`;
  }catch(e){if(info)info.textContent='request failed';}
}

function initPlug(){
  const scan=document.getElementById('plugscan');
  const test=document.getElementById('plugtest');
  const list=document.getElementById('pluglist');
  if(scan)scan.addEventListener('click',plugScan);
  if(test)test.addEventListener('click',plugTest);
  if(list)list.addEventListener('click',ev=>{
    const b=ev.target.closest('.plugpick');
    if(!b)return;
    const f=document.getElementById('cfgform');
    f.elements['kasa_host'].value=b.dataset.host;
    list.hidden=true;
    const info=document.getElementById('pluginfo');
    if(info)info.textContent=`${b.dataset.host} selected \u00b7 test it, then save`;
  });
}

function initBackup(){
  const box=document.getElementById('backupsecrets');
  const btn=document.getElementById('backupbtn');
  const info=document.getElementById('backupinfo');
  if(!btn)return;
  const sync=()=>{
    btn.href = '/api/backup' + (box && box.checked ? '?secrets=1' : '');
    if(info)info.textContent = (box && box.checked)
      ? 'database, settings and .env \u2014 keep this file private'
      : 'database, settings and planting history';
  };
  if(box)box.addEventListener('change',sync);
  sync();
}

function initPlantings(){
  const t=document.getElementById('histtoggle'), b=document.getElementById('histbody');
  if(t&&b)t.addEventListener('click',()=>{
    const open=b.hasAttribute('hidden');
    if(open)b.removeAttribute('hidden'); else b.setAttribute('hidden','');
    t.setAttribute('aria-expanded', open?'true':'false');
  });
  if(b)b.addEventListener('click',async ev=>{
    const btn=ev.target.closest('.hrestore');
    if(!btn)return;
    btn.blur();
    try{
      const r=await fetch('/api/planting_restore',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({id:Number(btn.dataset.id)})});
      const j=await r.json().catch(()=>({}));
      if(!j.ok){alert(j.error||'Could not restore that.');return;}
    }catch(e){alert('Request failed.');return;}
    const wrapEl=document.getElementById('trayswrap');
    if(wrapEl)wrapEl.dataset.sig='';       // the restored cell must reappear now
    refresh(); loadPlantings();
  });
  loadPlantings();
}

// Empty a cell in place. Used for the instant feedback after Transplanted or
// Died: the authoritative redraw follows from the next status, but the click
// should not look ignored while that round trip happens.
function clearCellUI(cell){
  cell.querySelectorAll('input,textarea').forEach(el=>{
    el.value='';
    el.setAttribute('value','');   // keep the attribute in step with the
  });                              // property, so the markup stays honest
  cell.dataset.sprouted=''; cell.dataset.archived=''; cell.dataset.seed='';
  cell.classList.remove('sprouted','archived','filled');
  const age=cell.querySelector('.tage'); if(age)age.textContent='';
  const st=cell.querySelector('.tstat'); if(st)st.textContent='';
}

// Theme: "auto" leaves it to the device's own preference, which the stylesheet
// handles through a media query; light and dark force it with an attribute.
// The browser chrome colour is kept in step so the phone address bar matches.
// The header button names the mode it will switch you to, which is how a
// two-state toggle stays unambiguous: "Dark Mode" means pressing it gives you
// dark. "auto" resolves to whatever the device is currently doing, so the
// first press always lands on the opposite of what you can see.
let themeMode='auto';
function isDarkNow(mode){
  return mode==='dark' || (mode!=='light' && window.matchMedia
    && window.matchMedia('(prefers-color-scheme: dark)').matches);
}
function labelTheme(){
  const btn=document.getElementById('themebtn');
  if(btn)btn.textContent = isDarkNow(themeMode) ? 'Light Mode' : 'Dark Mode';
}
async function toggleTheme(){
  const next = isDarkNow(themeMode) ? 'light' : 'dark';
  themeMode=next;
  applyTheme(next);                    // instant: never wait on the round trip
  try{
    const r=await fetch('/api/settings',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({theme:next})});
    const j=await r.json().catch(()=>({}));
    if(r.status===401||!j.ok)return;    // read-only: it still applies for this visit
    pendingSave.theme=next;             // hold it until a status echoes it back
  }catch(e){}
}
// ---- thunderstorm ----
// A button in the Light settings, shown only on the wiring that can do it:
// an AC fixture on a 0-10V dim line. The 5V panel has too little range and
// the smart plug cannot dim at all, so on those the server refuses and the
// button stays hidden rather than offering something that will not work.
let stormBusy=false;
async function summonStorm(){
  if(stormBusy)return;
  const btn=document.getElementById('stormbtn');
  const info=document.getElementById('storminfo');
  stormBusy=true;
  if(btn)btn.disabled=true;
  try{
    const r=await fetch('/api/lightning',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({seconds:20,style:'storm'})});
    const j=await r.json().catch(()=>({}));
    if(r.status===401){
      if(info)info.textContent='log in first';
      stormBusy=false; if(btn)btn.disabled=false; return;
    }
    if(!j.ok){
      if(info)info.textContent=j.error||'could not start';
      stormBusy=false; if(btn)btn.disabled=false; return;
    }
    const secs=j.seconds||20;
    let left=secs;
    if(info)info.textContent=`storm running \u2014 ${left}s`;
    const tick=setInterval(()=>{
      left-=1;
      if(info)info.textContent=`storm running \u2014 ${left}s`;
      if(left<=0){
        clearInterval(tick);
        stormBusy=false;
        if(btn)btn.disabled=false;
        if(info)info.textContent='done, back to the schedule';
      }
    },1000);
  }catch(e){
    if(info)info.textContent='request failed';
    stormBusy=false; if(btn)btn.disabled=false;
  }
}
function renderStorm(j){
  const row=document.getElementById('stormrow');
  if(!row)return;
  // the server decides: it knows the wiring and whether the channel opened
  row.style.display=(j.lightning && j.lightning.available && canEdit) ? '' : 'none';
}
// ---- light calibration ----
// A fine raw sweep (every percent) and an inverse lookup, so the dashboard's
// percent means a fraction of the fixture's real output. The sweep's own
// progress is shown by the existing sweep card; this just starts it and
// reports what the calibration found.
async function startCalibration(){
  const info=document.getElementById('lininfo');
  const btn=document.getElementById('linbtn');
  try{
    const r=await fetch('/api/light_sweep',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({linearize:true})});
    const j=await r.json().catch(()=>({}));
    if(r.status===401){if(info)info.textContent='log in first';return;}
    if(!j.ok){if(info)info.textContent=j.error||'could not start';return;}
    if(btn)btn.disabled=true;
    if(info)info.textContent=`calibrating, about ${Math.round((j.estimate_seconds||180)/60)} minutes. `
      +'The light will step through its whole range.';
  }catch(e){if(info)info.textContent='request failed';}
}
function renderCalibration(j){
  const info=document.getElementById('lininfo');
  const btn=document.getElementById('linbtn');
  const running=!!(j.sweep&&j.sweep.running);
  if(btn)btn.disabled=running;
  const lin=j.light_linear;
  if(!info||running)return;
  if(j.light_linear_stale){
    info.textContent='The stored calibration was built by an older version and '
      +'is not being used. Press Calibrate to rebuild it.';
    return;
  }
  if(lin&&lin.table===undefined&&lin.cutoff_raw!==undefined){
    const when=lin.ts?new Date(lin.ts*1000).toLocaleDateString():'';
    const floorNote = (lin.min_output_pct>2)
      ? ` The driver cannot hold less than about ${Math.round(lin.min_output_pct)}% `
        +`of full, so settings below that hold that lowest level, or cycle on `
        +`and off to average down if you switch that on.`
      : '';
    info.textContent=`Calibrated ${when}: light appears at ${lin.cutoff_raw}% `
      +`and reaches full output by ${lin.saturation_raw}% raw, `
      +`${Math.round(lin.peak_lux).toLocaleString()} lx peak.${floorNote} `
      +(j.light_linear_on?'In use.':'Not in use; tick the box to apply it.');
  }
}

// ---- second light ----
function renderLight2(j){
  const el=document.getElementById('l2line');
  if(!el)return;
  const l=j.light2;
  if(!l||!l.enabled||el.dataset.hide){el.style.display='none';return;}
  el.style.display='';
  const name=l.fixture==='pwm'?'5V panel':(l.fixture==='dim'?'dim fixture':'second light');
  if(!l.fixture){
    el.textContent=`Second light: ${l.why}`;
    el.className='l2line l2bad';
    return;
  }
  el.className='l2line';
  const sched=l.override==='auto'?` \u00b7 ${l.start}\u2013${l.end}`:'';
  el.textContent=`Second light (${name}): ${Math.round(l.level)}% \u00b7 ${l.why}${sched}`;
}

function initStorm(){
  const btn=document.getElementById('stormbtn');
  if(btn)btn.addEventListener('click',summonStorm);
  const lb=document.getElementById('linbtn');
  if(lb)lb.addEventListener('click',startCalibration);
}

function initTheme(){
  const btn=document.getElementById('themebtn');
  if(btn)btn.addEventListener('click',toggleTheme);
  labelTheme();
}

function applyTheme(mode){
  const root=document.documentElement;
  if(mode==='light'||mode==='dark')root.setAttribute('data-theme',mode);
  else root.removeAttribute('data-theme');
  const dark = mode==='dark' || (mode!=='light' && window.matchMedia
    && window.matchMedia('(prefers-color-scheme: dark)').matches);
  const meta=document.querySelector('meta[name="theme-color"]');
  if(meta)meta.setAttribute('content', dark?'#141a15':'#f0f6ea');
  themeMode=mode;
  labelTheme();
}

function initSensors(){
  const pmap={probewet1:['1','wet'],probedry1:['1','dry'],probewet2:['2','wet'],probedry2:['2','dry']};
  for(const id in pmap){const b=document.getElementById(id);
    if(b)b.addEventListener('click',()=>calibrateProbe(pmap[id][0],pmap[id][1]));}
  {const info=document.getElementById('probecalinfo');
   if(info)info.addEventListener('click',ev=>{
     const b=ev.target.closest('.calforce');
     if(b)calibrateProbe(b.dataset.tray, b.dataset.point, true,
                         b.dataset.volts!=null?parseFloat(b.dataset.volts):null);
   });}
  {const t1=document.getElementById('tc1');if(t1)t1.addEventListener('click',()=>checkTempComp('1'));
   const t2=document.getElementById('tc2');if(t2)t2.addEventListener('click',()=>checkTempComp('2'));}
  const hc=document.getElementById('chartgrid');
  if(hc){
    hc.addEventListener('mousemove',chartMove);
    hc.addEventListener('mouseleave',chartLeave);
    hc.addEventListener('pointerdown',chartMove);      // tap/click reads a point
    {let rt=null;
     window.addEventListener('resize',()=>{             // viewBox follows the box
       clearTimeout(rt);
       rt=setTimeout(()=>{for(const k in chartPlots)drawMini(k);},150);
     });}
    hc.addEventListener('click',ev=>{
      const b=ev.target.closest('.cexpand');
      if(!b)return;
      const card=document.getElementById('cc-'+b.dataset.key);
      if(!card)return;
      const nowBig=card.classList.toggle('expanded');
      if(nowBig)expandedCharts.add(card.id); else expandedCharts.delete(card.id);
      b.textContent=nowBig?'\u2921':'\u2922';
      b.title=nowBig?'Shrink this chart':'Expand this chart';
      const key=Object.keys(seriesData).find(k=>cssId(k)===b.dataset.key);
      if(key)drawMini(key);                            // redraw at the new size
    });
  }
  loadChart();                       // initial draw; range buttons reload
  document.querySelectorAll('#ranges button').forEach(b=>{
    b.addEventListener('click',()=>{
      chartHours=+b.dataset.h;
      document.querySelectorAll('#ranges button').forEach(x=>x.classList.remove('on'));
      b.classList.add('on');loadChart();});
  });
}

// ---------------- cell grid overlay ----------------
let grid=null, gdrag=-1, gridDirty=false;
function colL(c){return String.fromCharCode(65+c);}
function cellKey(r,c){return colL(c)+(r+1);}
function bil(C,u,v){
  const t=[(1-u)*C[0][0]+u*C[1][0],(1-u)*C[0][1]+u*C[1][1]];
  const b=[(1-u)*C[3][0]+u*C[2][0],(1-u)*C[3][1]+u*C[2][1]];
  return [(1-v)*t[0]+v*b[0],(1-v)*t[1]+v*b[1]];
}
function gridEditable(){return canEdit && grid && !grid.locked;}
function drawGrid(){
  const svg=document.getElementById('gridsvg');
  if(!grid||!svg)return;
  if(cropping){svg.style.display='none';return;}   // the crop box owns the photo
  svg.style.display=grid.show?'':'none';
  if(!grid.show){svg.innerHTML='';return;}
  // On the flattened view the image IS the tray rectangle, so the cells are
  // even splits of the frame; the saved corners describe the raw frame and
  // would land in the wrong places here.
  const photo=document.getElementById('photo');
  const flat=!!(photo && photo.dataset.flat);
  const cr=(!flat && photo && photo.dataset.crop)?photo.dataset.crop.split(',').map(Number):null;
  // corners are saved against the full frame; on a cropped view, re-express
  // them relative to the crop so the cells stay on the trays
  const C=flat?[[0,0],[1,0],[1,1],[0,1]]
         :(cr?grid.corners.map(p=>[(p[0]-cr[0])/cr[2],(p[1]-cr[1])/cr[3]]):grid.corners);
  const R=grid.rows,K=grid.cols,S=1000;
  let h='';
  for(let r=0;r<R;r++)for(let c=0;c<K;c++){
    const p=[bil(C,c/K,r/R),bil(C,(c+1)/K,r/R),bil(C,(c+1)/K,(r+1)/R),bil(C,c/K,(r+1)/R)];
    const pts=p.map(q=>(q[0]*S).toFixed(1)+','+(q[1]*S).toFixed(1)).join(' ');
    const k=cellKey(r,c);
    const fill='rgba(127,176,105,0.12)';
    h+=`<polygon class="gc" data-k="${k}" points="${pts}" fill="${fill}" stroke="#eafff0" stroke-width="2"/>`;
    const ctr=bil(C,(c+0.5)/K,(r+0.5)/R);
    const cx=(ctr[0]*S).toFixed(1); let yy=ctr[1]*S-3;
    h+=`<text x="${cx}" y="${yy.toFixed(1)}" class="glbl" text-anchor="middle">${k}</text>`;
    const nm=grid.names[k];
    if(nm){yy+=22;h+=`<text x="${cx}" y="${yy.toFixed(1)}" class="gnm" text-anchor="middle">${esc(nm)}</text>`;}
  }
  if(gridEditable())for(let i=0;i<4;i++)
    h+=`<circle class="gh" data-i="${i}" cx="${(C[i][0]*S).toFixed(1)}" cy="${(C[i][1]*S).toFixed(1)}" r="16"/>`;
  svg.innerHTML=h;
}
function ptFrac(svg,e){
  const r=svg.getBoundingClientRect();
  return [Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),
          Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))];
}
async function saveGrid(){
  gridDirty=true;                 // pending local edit; block poll-sync until saved
  const info=document.getElementById('gridinfo');
  try{
    const r=await fetch('/api/grid',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(grid)});
    if(!r.ok){
      const j=await r.json().catch(()=>({}));
      if(info)info.textContent = r.status===401
        ? 'not saved \u2014 log in to edit the grid'
        : ('grid not saved: '+(j.error||('HTTP '+r.status)));
      return;                      // stay dirty so a poll won't revert unsaved edits
    }
    gridDirty=false;               // saved; tabs may sync again
    if(info && /not saved|HTTP|log in/.test(info.textContent)) info.textContent='';
  }catch(e){ if(info)info.textContent='grid not saved (request failed)'; }
}
async function detectGrid(){
  if(!gridEditable())return;
  const info=document.getElementById('gridinfo');info.textContent='Detecting...';
  try{
    const r=await fetch('/api/detect_grid',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const j=await r.json();
    if(j.ok&&j.corners){grid.corners=j.corners;if(!grid.show){grid.show=true;
      document.getElementById('gridshow').checked=true;}
      drawGrid();saveGrid();info.textContent='Detected \u2014 drag corners to fine-tune.';}
    else info.textContent=j.error||'Detection failed; place corners by hand.';
  }catch(e){info.textContent='Detection unavailable; place corners by hand.';}
}
function syncGridControls(){
  if(!grid)return;
  document.getElementById('gridshow').checked=!!grid.show;
  document.getElementById('gridrows').value=grid.rows;
  document.getElementById('gridcols').value=grid.cols;
  applyGridLock();
}
function initGridSvg(){
  const svg=document.getElementById('gridsvg');
  svg.addEventListener('pointerdown',e=>{
    if(!gridEditable())return;
    if(e.target.classList.contains('gh')){
      gdrag=+e.target.dataset.i;svg.setPointerCapture(e.pointerId);e.preventDefault();}
  });
  svg.addEventListener('pointermove',e=>{
    if(gdrag<0||!grid)return;grid.corners[gdrag]=ptFrac(svg,e);drawGrid();});
  svg.addEventListener('pointerup',()=>{if(gdrag>=0){gdrag=-1;saveGrid();}});
  svg.addEventListener('click',e=>{
    if(!canEdit)return;
    if(!e.target.classList.contains('gc'))return;
    const k=e.target.dataset.k,cur=grid.names[k]||'';
    const v=prompt('Name for cell '+k+':',cur);
    if(v!==null){if(v.trim())grid.names[k]=v.trim();else delete grid.names[k];
      drawGrid();saveGrid();}
  });
  document.getElementById('gridshow').addEventListener('change',e=>{
    grid.show=e.target.checked;drawGrid();saveGrid();});
  document.getElementById('detectbtn').addEventListener('click',detectGrid);
  {const cb=document.getElementById('capturebtn');if(cb)cb.addEventListener('click',capturePhoto);}
  {const ab=document.getElementById('alignbtn');if(ab)ab.addEventListener('click',()=>aligning?stopAlign():startAlign());}
  const upd=()=>{if(!gridEditable()){syncGridControls();return;}
    grid.rows=Math.max(1,Math.min(12,+document.getElementById('gridrows').value||4));
    grid.cols=Math.max(1,Math.min(12,+document.getElementById('gridcols').value||4));
    drawGrid();saveGrid();};
  document.getElementById('gridrows').addEventListener('change',upd);
  document.getElementById('gridcols').addEventListener('change',upd);
  const lockBtn=document.getElementById('gridlock');
  if(lockBtn)lockBtn.addEventListener('click',()=>{
    if(!grid||!canEdit)return;
    grid.locked=!grid.locked;
    applyGridLock();drawGrid();saveGrid();
  });
}
function applyGridLock(){
  if(!grid)return;
  const locked=!!grid.locked;
  document.body.classList.toggle('gridlocked',locked);
  const btn=document.getElementById('gridlock');
  if(btn)btn.textContent=locked?'\uD83D\uDD13 Unlock grid':'\uD83D\uDD12 Lock grid';
}
function adoptGrid(g){
  grid=g;
  if(!grid.names)grid.names={};
  if(grid.locked===undefined)grid.locked=false;
  syncGridControls();
}
function handleGrid(j){
  if(j.settings&&j.settings.grid){
    const srv=j.settings.grid;
    if(grid===null){
      adoptGrid(srv);
    } else if(gdrag<0 && !gridDirty && JSON.stringify(srv)!==JSON.stringify(grid)){
      // another tab/device saved a newer grid; sync to it instead of holding
      // a stale copy that could later overwrite the saved one
      adoptGrid(srv);
    }
    if(gdrag<0)drawGrid();
  }
}

async function refresh(){
  let j=null;
  try{
    const r=await fetch('/api/status');
    if(!r.ok && r.status!==503)throw 0;
    j=await r.json();
  }catch(e){
    // the server genuinely didn't answer
    document.getElementById('phase').textContent='Controller unreachable';
    return;
  }
  applyStatus(j);
}

// The whole render, split out so a pushed status and a polled one go through
// exactly the same path. Anything that renders differently depending on how
// the data arrived is a bug waiting to happen.
function applyStatus(j){
  try{
    S={...j,now:new Date(j.now),on:new Date(j.on),off:new Date(j.off),
       sunrise:new Date(j.sunrise),sunset:new Date(j.sunset)};
    // Same hold as the form fields: until the server echoes a saved backend,
    // keep the chosen one. Otherwise a stale poll flips the slider and sweep
    // card back into view for one cycle and the whole card jumps.
    lightBackend=('light_backend' in pendingSave)
      ? pendingSave.light_backend
      : (j.light_backend||'pwm');   // set before any renderer reads it
    fillForm(j.settings);
    {const camOn=!!(j.settings&&j.settings.camera_enabled);
     for(const id of ['photocard','videocard','reportcard']){
       const el=document.getElementById(id);
       if(el)el.dataset.camoff=camOn?'':'1';
       if(el&&!camOn)el.style.display='none';
     }
     window._camOn=camOn;
     // collapse the media column entirely, or its grid track sits empty
     document.body.classList.toggle('nocam', !camOn);}
    if(window._camOn)renderPhoto(j);
    renderVideoState(j);
    loadFrames();
    applyAuth(j);
    setAiControls(j.settings);
    applySetup(j);
    {const rc=document.getElementById('reportctl');if(rc)rc.style.display=canEdit?'':'none';}
    fetchReport();
    requestAnimationFrame(fitReportHeight);
    handleGrid(j);
    renderSensors(j);
    renderQuality(j);
    renderStorm(j);
    renderCalibration(j);
    renderLight2(j);
    renderTrays(j);
    renderTrayConfig(j.settings);
    renderSweep(j);
    renderFocus(j);
    renderFan(j);
    renderDayProgress(j);
    renderLightPlan(j);
    if(Date.now()-lastHostLoad>60000){lastHostLoad=Date.now();loadHost();}
    if(Date.now()-lastChartLoad>120000)loadChart();   // history every ~2 min
    renderWater(j);
    render();
  }catch(e){
    // the server answered but our page code failed: usually a stale cached
    // script after a deploy. Say so instead of blaming the controller.
    console.error('render error:', e);
    document.getElementById('phase').textContent='Page error \u2013 hard-refresh (Ctrl-Shift-R)';
  }
}

document.getElementById('cfgform').addEventListener('submit',async ev=>{
  ev.preventDefault();
  const f=ev.target,msg=document.getElementById('msg');
  const body={};
  for(const k of ['latitude','longitude','max_bright','ramp_min',
                  'sunrise_offset_min','sunset_offset_min',
                  'capture_interval_min','capture_brightness'])
    body[k]=parseFloat(f.elements[k].value);
  body.timezone=f.elements['timezone'].value.trim();
  body.roi=f.elements['roi'].value.trim();
  if(f.elements['lux_to_ppfd_k'])
    body.lux_to_ppfd_k=parseFloat(f.elements['lux_to_ppfd_k'].value||0);
  if(f.elements['canopy_factor'])
    body.canopy_factor=parseFloat(f.elements['canopy_factor'].value||1);
  if(f.elements['cam_rotate'])body.cam_rotate=parseInt(f.elements['cam_rotate'].value||0,10);
  if(f.elements['camera_backend'])body.camera_backend=f.elements['camera_backend'].value;
  if(f.elements['usb_device'])body.usb_device=f.elements['usb_device'].value.trim();
  for(const k of ['usb_auto_focus','usb_auto_exposure_on','usb_auto_white_balance'])
    if(f.elements[k])body[k]=f.elements[k].checked;
  for(const k of ['usb_width','usb_height','usb_exposure_time_absolute','usb_gain',
                  'usb_focus_absolute','usb_white_balance_temperature'])
    if(f.elements[k])body[k]=parseInt(f.elements[k].value||0,10);
  if(f.elements['cam_rectify'])body.cam_rectify=f.elements['cam_rectify'].checked;
  if(f.elements['timelapse_flatten'])body.timelapse_flatten=f.elements['timelapse_flatten'].checked;
  for(const k of ['humidity_low','humidity_high','fan_humidity_on','fan_min_speed'])
    if(f.elements[k])body[k]=parseInt(f.elements[k].value||0,10);
  if(f.elements['live_interval_s'])
    body.live_interval_s=parseInt(f.elements['live_interval_s'].value||0,10);
  if(f.elements['schedule_mode'])body.schedule_mode=f.elements['schedule_mode'].value;
  if(f.elements['light_backend'])body.light_backend=f.elements['light_backend'].value;
  for(const k of ['fixed_on','fixed_off','duration_end'])
    if(f.elements[k])body[k]=f.elements[k].value;
  if(f.elements['duration_hours'])
    body.duration_hours=parseFloat(f.elements['duration_hours'].value||0);
  if(f.elements['units'])body.units=f.elements['units'].value;
  for(const k of ['alert_sustain_min','alert_cooldown_hours','alert_dry_pct','alert_humidity_high',
                  'moisture_threshold_pct','pump_cooldown_min','fill_max_seconds',
                  'pump_daily_max_seconds','pump_max_seconds','probe_median_depth'])
    if(f.elements[k])body[k]=parseInt(f.elements[k].value||0,10);
  for(const k of ['alert_dli_low','alert_dli_high'])
    if(f.elements[k])body[k]=parseFloat(f.elements[k].value||0);
  if(f.elements['alerts_enabled'])body.alerts_enabled=f.elements['alerts_enabled'].checked;
  if(f.elements['soil_temp_low_f']){
    const shown=parseFloat(f.elements['soil_temp_low_f'].value||0);
    body.soil_temp_low_f=shown?Math.round(tToF(shown)):0;
  }
  if(f.elements['soil_temp_high_f']){
    const shown=parseFloat(f.elements['soil_temp_high_f'].value||0);
    // the number was typed in whatever unit the field was showing, which is
    // the CURRENT `units`, not the one being saved: converting with the new
    // selection would silently rescale the threshold when toggling systems
    body.soil_temp_high_f=shown?Math.round(tToF(shown)):0;   // store F
  }
  body.capture_enabled=f.elements['capture_enabled'].checked;
  if(f.elements['camera_enabled'])
    body.camera_enabled=f.elements['camera_enabled'].checked;
  if(f.elements['little_buddy'])
    body.little_buddy=f.elements['little_buddy'].checked;
  if(f.elements['auto_wet_cal'])
    body.auto_wet_cal=f.elements['auto_wet_cal'].checked;
  if(f.elements['light_linear_on'])
    body.light_linear_on=f.elements['light_linear_on'].checked;
  if(f.elements['dim_below_min'])body.dim_below_min=f.elements['dim_below_min'].value;
  if(f.elements['light2_on'])body.light2_on=f.elements['light2_on'].checked;
  for(const k of ['light2_start','light2_end','light2_override'])
    if(f.elements[k])body[k]=f.elements[k].value;
  for(const k of ['light2_bright','light2_ramp_min'])
    if(f.elements[k])body[k]=parseInt(f.elements[k].value||0,10);
  if(f.elements['auto_wet_cal_max_move'])
    body.auto_wet_cal_max_move=parseFloat(f.elements['auto_wet_cal_max_move'].value||0.1);
  if(f.elements['light_floor_pct'])
    body.light_floor_pct=parseFloat(f.elements['light_floor_pct'].value||0);
  for(const k of ['kasa_host','kasa_user'])
    if(f.elements[k])body[k]=f.elements[k].value.trim();
  // blank means "keep the stored one": the field is never filled from the
  // server, so submitting an empty string would silently wipe the password
  if(f.elements['kasa_pass']&&f.elements['kasa_pass'].value)
    body.kasa_pass=f.elements['kasa_pass'].value;

  if(f.elements['buddy_model'])
    body.buddy_model=f.elements['buddy_model'].value;
  msg.textContent='Planting...';msg.className='';
  try{
    const r=await fetch('/api/settings',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const j=await r.json();
    const errs=j.errors&&Object.keys(j.errors);
    // a rejected field inside a collapsed section would be invisible: open the
    // sections holding any errors so the message points at something on screen
    for(const k of (errs||[])){
      const el=f.elements[k];
      const grp=el&&el.closest&&el.closest('details.fgroup');
      if(grp)grp.open=true;
    }
    // hold every accepted field until the server echoes it back
    for(const k of (j.saved||[]))
      if(k in body)pendingSave[k]=body[k];
    if(r.ok&&j.ok){msg.textContent='Saved \u{1F331}';msg.className='ok';refresh();}
    else if(errs&&errs.length){
      // everything valid was saved; say exactly which fields were rejected
      msg.textContent='Saved, except: '
        +errs.map(k=>k+' ('+j.errors[k]+')').join(', ');
      msg.className='err';refresh();
    }
    else{msg.textContent=j.error||'Save failed';msg.className='err';}
  }catch(e){msg.textContent='Save failed';msg.className='err';}
});

setInterval(()=>{const d=new Date();
  document.getElementById('clock').textContent=d.toLocaleTimeString();
  if(S){S.now=d;}},1000);
// ---------------- AI garden report ----------------
function rBadge(h){
  const m={good:['Healthy','rbg-good'],watch:['Watch','rbg-watch'],problem:['Problem','rbg-problem']};
  const v=m[h]||['\u2014','rbg-watch'];
  return `<span class="rbadge ${v[1]}">${v[0]}</span>`;
}
function rList(title,arr){
  if(!arr||!arr.length)return '';
  return `<div class="rsec"><h4>${title}</h4><ul>${arr.map(x=>`<li>${esc(String(x))}</li>`).join('')}</ul></div>`;
}
function rAgo(ts){
  if(!ts)return '';
  return new Date(ts*1000).toLocaleString([],{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
}
function fitReportHeight(){
  const card=document.getElementById('reportcard');
  const media=document.querySelector('.amedia');
  if(!card||!media)return;
  if(window.innerWidth<1100){card.style.maxHeight='';return;}  // single column: let it flow
  const top=card.getBoundingClientRect().top;
  const mediaBottom=media.getBoundingClientRect().bottom;
  card.style.maxHeight=Math.max(220,Math.round(mediaBottom-top))+'px';
}
function renderReport(j){
  const body=document.getElementById('reportbody'); if(!body)return;
  if(j.generating){body.innerHTML='<p class="rmuted">Generating report\u2026</p>';return;}
  if(j.have_key===false){body.innerHTML='<p class="rmuted">No API key on the controller. Add a <code>.anthropic_key</code> file (or set ANTHROPIC_API_KEY) to enable AI reports.</p>';return;}
  if(j.ok===null||j.ok===undefined){body.innerHTML='<p class="rmuted">No report yet. Generate one, or enable the daily report.</p>';return;}
  if(j.ok===false){body.innerHTML=`<p class="rmuted">Last attempt failed: ${esc(j.error||'unknown error')}</p>`;return;}
  const r=j.report||{};
  let h=`<div class="rhead">${rBadge(r.overall_health)}<span class="rtime">${rAgo(j.ts)}${j.model?' \u00b7 '+esc(j.model):''}</span></div>`;
  if(r.summary)h+=`<p class="rsummary">${esc(r.summary)}</p>`;
  const f=[];
  if(r.germination&&r.germination.sprouted!=null&&r.germination.total_cells!=null)
    f.push(`Germinated ${r.germination.sprouted}/${r.germination.total_cells}`);
  if(r.growth_stage)f.push('Stage: '+esc(r.growth_stage));
  if(r.light&&r.light.assessment)f.push('Light: '+esc(r.light.assessment));
  if(r.water&&r.water.assessment)f.push('Water: '+esc(r.water.assessment));
  if(f.length)h+=`<p class="rfacts">${f.join(' \u00b7 ')}</p>`;
  if(r.light&&r.light.reason)h+=`<p class="rreason"><b>Light:</b> ${esc(r.light.reason)}</p>`;
  if(r.water&&r.water.reason)h+=`<p class="rreason"><b>Water:</b> ${esc(r.water.reason)}</p>`;
  h+=rList('Concerns',r.concerns);
  h+=rList('Recommendations',r.recommendations);
  if(r.per_cell&&r.per_cell.length)
    h+=`<div class="rsec"><h4>Cell notes</h4><ul>${r.per_cell.map(c=>`<li><b>${esc(c.cell||'')}</b> ${esc(c.note||'')}</li>`).join('')}</ul></div>`;
  // Only cells that look wrong are worth showing: the planting map already
  // records what is in every cell, so a list of confirmations is noise.
  {const vc=(r.variety_check||[]).filter(v=>v && v.looks_consistent===false);
   if(vc.length)
     h+=`<div class="rsec"><h4>Possible mix-ups</h4><ul>${vc.map(v=>
       `<li><b>${esc(v.cell||'')}</b> recorded as ${esc(v.expected||'?')}`
       +`${v.why?` &mdash; ${esc(v.why)}`:''}</li>`).join('')}</ul></div>`;
   // tolerate reports generated before this change
   if(!r.variety_check && r.species && r.species.length)
     h+=`<div class="rsec"><h4>Species guesses</h4><ul>${r.species.map(sp=>
       `<li><b>${esc(sp.cell||'')}</b> ${esc(sp.guess||'unsure')}</li>`).join('')}</ul></div>`;}
  if(r.confidence)h+=`<p class="rconf">Confidence: ${esc(r.confidence)}${j.parse_error?' \u00b7 (reply was not structured JSON)':''}</p>`;
  body.innerHTML=h;
}
let lastReportSig=null;
async function fetchReport(){
  try{
    const r=await fetch('/api/report');
    const j=await r.json();
    const sig=`${j.ts}|${j.generating}|${j.ok}|${j.have_key}`;
    if(sig!==lastReportSig){           // only re-render when something changed
      lastReportSig=sig;
      renderReport(j);
      requestAnimationFrame(fitReportHeight);
    }
    if(j.generating)setTimeout(fetchReport,4000);   // poll faster until it lands
  }catch(e){}
}
async function genReport(){
  const info=document.getElementById('reportinfo');if(info)info.textContent='working\u2026';
  document.getElementById('reportbody').innerHTML='<p class="rmuted">Generating report\u2026 this takes ~20s.</p>';
  try{
    const r=await fetch('/api/report',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const j=await r.json();
    if(info)info.textContent='';
    if(j.ok){renderReport({...j,have_key:true});lastReportSig=`${j.ts}|${j.generating}|${j.ok}|true`;}
    else renderReport({ok:false,error:j.error||('HTTP '+r.status)});
    requestAnimationFrame(fitReportHeight);
  }catch(e){
    if(info)info.textContent='';
    document.getElementById('reportbody').innerHTML='<p class="rmuted">Request timed out, but it may still be generating. Reload in a moment to see it.</p>';
  }
}
// The seedling DLI target band, from Settings, Targets. The bar's scale grows
// to fit a high band: 16 mol for a 6 to 12 band, 24 for 15 to 20, and so on.
var dliBand={lo:15,hi:20,max:24};   // var: read by renders that can run before this line
function setDliBand(s){
  if(!s)return;
  const lo=parseFloat(s.dli_target_low), hi=parseFloat(s.dli_target_high);
  if(!(lo>0&&hi>lo))return;
  const max=[16,24,32,40,48,60,80].find(m=>m>=hi*1.15)||Math.ceil(hi*1.15);
  if(lo===dliBand.lo&&hi===dliBand.hi&&max===dliBand.max&&dliBand.drawn)return;
  dliBand={lo,hi,max,drawn:true};
  const pct=v=>(v/max*100).toFixed(2)+'%';
  const band=document.getElementById('dliband');
  if(band){band.style.left=pct(lo);band.style.width=pct(hi-lo);}
  const sc=document.getElementById('dliscale');
  if(sc)sc.innerHTML=`<i style="left:${pct(lo)}"></i><i style="left:${pct(hi)}"></i>`
    +`<span class="s0" style="left:0">0</span>`
    +`<span style="left:${pct(lo)}">${lo}</span>`
    // the word "target" only fits between the two numbers on a wide band
    +((hi-lo)/max>=0.3?`<span class="sband" style="left:${pct((lo+hi)/2)}">target</span>`:'')
    +`<span style="left:${pct(hi)}">${hi}</span>`
    +`<span class="s16" style="left:100%">${max} mol</span>`;
}
function setAiControls(s){
  if(!s)return;
  const en=document.getElementById('aienabled'),t=document.getElementById('aitime');
  if(en&&document.activeElement!==en)en.checked=!!s.ai_enabled;
  if(t&&document.activeElement!==t&&s.ai_report_hour!=null){
    const h=String(s.ai_report_hour).padStart(2,'0');
    const m=String(s.ai_report_minute||0).padStart(2,'0');
    t.value=`${h}:${m}`;
  }
}
async function saveAi(){
  const en=document.getElementById('aienabled'),t=document.getElementById('aitime');
  const parts=(t.value||'08:00').split(':');
  const h=Math.min(23,Math.max(0,parseInt(parts[0],10)||0));
  const m=Math.min(59,Math.max(0,parseInt(parts[1],10)||0));
  try{await fetch('/api/ai_settings',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ai_enabled:en.checked,ai_report_hour:h,ai_report_minute:m})});}catch(e){}
}
async function resetTimelapse(confirmed){
  const info=document.getElementById('renderinfo');
  try{
    const r=await fetch('/api/reset_timelapse',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(confirmed?{confirm:true,clear_readings:true}:{})});
    const j=await r.json().catch(()=>({}));
    if(r.status===401){if(info)info.textContent='log in first';return;}
    if(!j.ok&&j.needs_confirm&&!confirmed){   // ask once, never loop
      if(window.confirm(`Archive ${j.photos} photos and start a new timelapse?\n\n`
        +`They are moved to timelapse_archive/, not deleted. Per-cell camera `
        +`readings are cleared too, since they were measured against the old `
        +`camera position. Probe, temperature and light history is kept.`))
        return resetTimelapse(true);
      return;
    }
    if(!j.ok){if(info)info.textContent=j.error||'failed';return;}
    if(info)info.textContent=`archived ${j.archived} photos`;
    refresh();
  }catch(e){if(info)info.textContent='request failed';}
}
function initReport(){
  const gb=document.getElementById('genreport');if(gb)gb.addEventListener('click',genReport);
  const en=document.getElementById('aienabled');if(en)en.addEventListener('change',saveAi);
  const t=document.getElementById('aitime');if(t)t.addEventListener('change',saveAi);
  window.addEventListener('resize',fitReportHeight);
  if('ResizeObserver' in window){
    const m=document.querySelector('.amedia');
    if(m)new ResizeObserver(()=>fitReportHeight()).observe(m);
  }
  fetchReport();
}

// ---- live updates ----
// EventSource pushes a status the moment something changes. The poll stays,
// slowed right down: a stream that dies quietly would otherwise freeze the
// page, and this way the worst case is the old 15-second behaviour.
const POLL_FAST=15000, POLL_SLOW=60000;
let pollTimer=null, es=null, streamOk=false;
function setPoll(ms){
  if(pollTimer)clearInterval(pollTimer);
  pollTimer=setInterval(refresh, ms);
}
function initStream(){
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
function restartStream(){
  if(es){ es.close(); es=null; }
  streamOk=false; setPoll(POLL_FAST);
  initStream();
}
// A hidden tab keeps no stream: each one holds a server thread and one of six
// slots. Closed a minute after hiding, reopened with a fresh status on return.
let hideTimer=null;
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
function curveLuxAt(pts, pct){
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
function drawLightCurve(curve, sweeping, nowPct){
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
async function startSweep(){
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
let sweepRunning=false, lastCurve=null;
// ---- the wandering seedling ----
// Every half minute a small seedling crosses one card, entering and leaving
// behind its edge. It sits at z-index:-1 inside the card, so it walks BEHIND
// the chips and buttons rather than over them.
let buddyOn=true;        // mirrors the "little buddy" setting
let buddyPick='sprout';  // 'sprout' | 'pepper' | 'cat' | 'random'
function buddyModel(){
  if(buddyPick!=='random')return buddyPick in BUDDY_SPRITES ? buddyPick : 'sprout';
  const keys=Object.keys(BUDDY_SPRITES);
  return keys[Math.floor(Math.random()*keys.length)];
}
const WALK_EVERY_MS=30000;
// walk in, stop and wave, walk out. The pause fractions must match the
// walk-across keyframes in the stylesheet (36% and 64%).
const WALK_DUR_MS=11000, PAUSE_START=0.36, PAUSE_END=0.64;
let walkTimer=null;

// Three characters, picked per outing. Each returns the SVG for one walker;
// they share the walk cycle, so a new one is a sprite function plus a case
// here, nothing more. The parts that animate carry fixed class names:
// .legs/.leg-a/.leg-b step, .body bobs, .arm waves during the pause.
const BUDDY_MODELS={sprout:'Potted sprout', pepper:'Chile pepper', cat:'Avey',
  snail:'Snail', ladybug:'Ladybug', drop:'Raindrop', bee:'Bee', gnome:'Garden gnome'};

function buddySprout(){
  return `<g class="legs">
      <line class="leg-a" x1="15" y1="26" x2="11" y2="33" style="transform-origin:15px 26px"/>
      <line class="leg-b" x1="15" y1="26" x2="19" y2="33" style="transform-origin:15px 26px"/>
    </g>
    <g class="body" style="transform-origin:15px 26px">
      <line class="stem" x1="15" y1="22" x2="15" y2="13"/>
      <path class="leaf-l" d="M15 16c-5.5 0-8.5-2.8-8.5-6.6 4.7-1 8.5 1.9 8.5 6.6z"/>
      <path class="leaf-r" d="M15 13.6c0-4.7 2.8-7.5 7.5-6.6.9 4.7-2.8 7.5-7.5 6.6z"/>
      <path class="pot" d="M8.4 22h13.2l-1.5 8.2a1.6 1.6 0 0 1-1.6 1.3h-7a1.6 1.6 0 0 1-1.6-1.3z"/>
      <rect class="pot-rim" x="7.6" y="20.2" width="14.8" height="2.6" rx="1"/>
      <circle class="cheek" cx="10.9" cy="28.2" r="1.1"/>
      <circle class="cheek" cx="19.1" cy="28.2" r="1.1"/>
      <circle class="eye" cx="12.6" cy="26.4" r="0.9"/>
      <circle class="eye" cx="17.4" cy="26.4" r="0.9"/>
      <path class="mouth" d="M13.2 28.6q1.8 1.4 3.6 0"/>
      <line class="arm sarm" x1="20.6" y1="25.4" x2="25" y2="21.6" style="transform-origin:20.6px 25.4px"/>
    </g>`;
}

function buddyPepper(){
  return `<g class="legs">
      <line class="leg-a" x1="15" y1="30" x2="11.5" y2="35" style="transform-origin:15px 30px"/>
      <line class="leg-b" x1="15" y1="30" x2="18.5" y2="35" style="transform-origin:15px 30px"/>
    </g>
    <g class="body" style="transform-origin:15px 30px">
      <path class="pod" d="M15 13c4.4 0 6.8 3.6 6.8 8.4 0 5.4-3 9-6.8 9s-6.8-3.6-6.8-9c0-4.8 2.4-8.4 6.8-8.4z"/>
      <path class="calyx" d="M12.4 12.6h5.2l-0.6 1.9h-4z"/>
      <line class="pstem" x1="15" y1="12.6" x2="15" y2="9.6"/>
      <circle class="pcheek" cx="10.9" cy="23" r="1.1"/>
      <circle class="pcheek" cx="19.1" cy="23" r="1.1"/>
      <circle class="peye" cx="12.7" cy="21" r="0.95"/>
      <circle class="peye" cx="17.3" cy="21" r="0.95"/>
      <path class="pmouth" d="M13.2 23.4q1.8 1.5 3.6 0"/>
      <line class="arm parm" x1="21.2" y1="21.4" x2="25.6" y2="17.6" style="transform-origin:21.2px 21.4px"/>
    </g>`;
}

function buddyCat(){
  // side profile: a cat walking across should look like it is going somewhere.
  // The tail takes the place of the wave during the pause.
  return `<g class="legs">
      <line class="leg-a cleg" x1="9" y1="25" x2="9" y2="29.5" style="transform-origin:9px 25px"/>
      <line class="leg-b cleg" x1="19.5" y1="25" x2="19.5" y2="29.5" style="transform-origin:19.5px 25px"/>
    </g>
    <g class="body" style="transform-origin:15px 25px">
      <path class="arm tail" d="M22.5 22c1.6-0.6 2.6-2.2 2.2-3.8" style="transform-origin:22.5px 22px"/>
      <path class="fur" d="M7.5 17.5h13.5a1.5 1.5 0 0 1 1.5 1.5v4.5a1.5 1.5 0 0 1-1.5 1.5H7.5a1.5 1.5 0 0 1-1.5-1.5V19a1.5 1.5 0 0 1 1.5-1.5z"/>
      <circle class="fur" cx="9" cy="13" r="5.4"/>
      <path class="fur" d="M4.8 9.6l-0.7-3.9 3.3 2z"/>
      <path class="fur" d="M13.2 9.6l0.7-3.9-3.3 2z"/>
      <path class="inner-ear" d="M5.3 9.2l-0.3-1.9 1.6 1z"/>
      <path class="inner-ear" d="M12.7 9.2l0.3-1.9-1.6 1z"/>
      <circle class="eye" cx="7" cy="12.6" r="0.95"/>
      <circle class="eye" cx="11" cy="12.6" r="0.95"/>
      <path class="inner-ear" d="M8.4 14.8h1.2l-0.6 0.7z"/>
      <path class="whisker" d="M4.6 14.4L1.8 13.8"/>
      <path class="whisker" d="M4.6 15.4L2 16"/>
      <path class="whisker" d="M13.4 14.4L16.2 13.8"/>
    </g>`;
}

function buddySnail(){
  // the slow one: SNAIL_DUR overrides the shared duration so it actually
  // reads as a snail rather than a shell on a normal walk cycle
  return `<g class="legs">
      <line class="leg-a stalk" x1="7" y1="23.5" x2="5.2" y2="17.6" style="transform-origin:7px 23.5px"/>
      <line class="leg-b stalk" x1="9.8" y1="23.5" x2="10.6" y2="17.6" style="transform-origin:9.8px 23.5px"/>
    </g>
    <g class="body" style="transform-origin:15px 27px">
      <ellipse class="foot-pad" cx="14" cy="28.4" rx="11" ry="2.4"/>
      <path class="snail-head" d="M4.6 28.4c0-4 1.7-6.6 4.6-6.6 2.6 0 4.2 2.2 4.4 6.6z"/>
      <circle class="shell" cx="18.4" cy="20.6" r="7.4"/>
      <path class="shell-line" d="M18.4 20.6a4 4 0 1 1 4-4" fill="none"/>
      <path class="shell-line" d="M18.4 20.6a6.4 6.4 0 1 0 6.4-6.4" fill="none"/>
      <circle class="eye" cx="5.2" cy="17.2" r="1.3"/>
      <circle class="eye" cx="10.6" cy="17.2" r="1.3"/>
      <path class="mouth" d="M6.6 26.2q1.8 1.3 3.6 0"/>
      <line class="arm stalk-wave" x1="9.8" y1="23.5" x2="10.6" y2="17.6" style="transform-origin:9.8px 23.5px"/>
    </g>`;
}

function buddyLadybug(){
  // In flight a ladybug lifts its two shell halves (the elytra) up and out,
  // and the thin wings folded underneath do the flapping. So the shell is
  // drawn as two halves hinged behind the head, with the body and wings
  // beneath them: closed, the halves cover the wings entirely.
  return `<g class="body">
      <g class="lwing lwing-l" style="transform-origin:13.5px 17.5px">
        <path class="lwing-m" d="M13.5 17.5C8.5 14.2 .5 14.8 -2.8 18.4C-4.4 20.4 -2.6 23.4 1 22.9C6 22.2 11 20.4 13.5 17.5z"/>
        <path class="lwing-v" d="M13.5 17.5Q5 18.2 -2.4 20.3"/>
      </g>
      <g class="lwing lwing-r" style="transform-origin:16.5px 17.5px">
        <path class="lwing-m" d="M16.5 17.5C21.5 14.2 29.5 14.8 32.8 18.4C34.4 20.4 32.6 23.4 29 22.9C24 22.2 19 20.4 16.5 17.5z"/>
        <path class="lwing-v" d="M16.5 17.5Q25 18.2 32.4 20.3"/>
      </g>
      <ellipse class="bug-belly" cx="15" cy="21.5" rx="5.2" ry="6.2"/>
      <g class="elytron elytron-l" style="transform-origin:15px 15.5px">
        <path class="shell-red" d="M15 14a8 7 0 0 0 0 14z"/>
        <path class="shell-edge" d="M15 14.4v13.2"/>
        <circle class="spot" cx="10.5" cy="19.5" r="1.5"/>
        <circle class="spot" cx="11.5" cy="24.5" r="1.2"/>
      </g>
      <g class="elytron elytron-r" style="transform-origin:15px 15.5px">
        <path class="shell-red" d="M15 14a8 7 0 0 1 0 14z"/>
        <path class="shell-edge" d="M15 14.4v13.2"/>
        <circle class="spot" cx="19.5" cy="19.5" r="1.5"/>
        <circle class="spot" cx="18.5" cy="24.5" r="1.2"/>
      </g>
      <circle class="head-dark" cx="15" cy="13.5" r="4.4"/>
      <circle class="eye-white" cx="13.3" cy="13" r="1"/>
      <circle class="eye-white" cx="16.7" cy="13" r="1"/>
      <path class="antenna" d="M12.8 10.6l-1.5-2.2"/>
      <path class="antenna" d="M17.2 10.6l1.5-2.2"/>
      <circle class="head-dark" cx="11.1" cy="8" r=".7"/>
      <circle class="head-dark" cx="18.9" cy="8" r=".7"/>
    </g>`;
}


function buddyDrop(){
  return `<g class="legs">
      <line class="leg-a drop-leg" x1="12.5" y1="27" x2="10" y2="33.5" style="transform-origin:12.5px 27px"/>
      <line class="leg-b drop-leg" x1="17.5" y1="27" x2="20" y2="33.5" style="transform-origin:17.5px 27px"/>
    </g>
    <g class="body" style="transform-origin:15px 27px">
      <path class="drop" d="M15 9c4 5 6.6 8.4 6.6 12A6.6 6.6 0 0 1 8.4 21c0-3.6 2.6-7 6.6-12z"/>
      <path class="glint" d="M11.4 20.5a3.6 3.6 0 0 1 2.2-4.6" fill="none"/>
      <circle class="dcheek" cx="10.6" cy="23.6" r="1.2"/>
      <circle class="dcheek" cx="19.4" cy="23.6" r="1.2"/>
      <circle class="eye" cx="12.8" cy="21.5" r="1.1"/>
      <circle class="eye" cx="17.2" cy="21.5" r="1.1"/>
      <path class="mouth" d="M13.2 24q1.8 1.5 3.6 0"/>
      <line class="arm drop-arm" x1="20.9" y1="22.4" x2="25.4" y2="19" style="transform-origin:20.9px 22.4px"/>
    </g>`;
}

function buddyBee(){
  // a flyer: no legs, and two wings that flap about their own roots, mirrored
  return `<g class="body">
      <ellipse class="wing wing-l" cx="9.5" cy="14" rx="5.5" ry="3.6" style="transform-origin:13.5px 15.5px"/>
      <ellipse class="wing wing-r" cx="20.5" cy="14" rx="5.5" ry="3.6" style="transform-origin:16.5px 15.5px"/>
      <ellipse class="bee-body" cx="15" cy="21" rx="7.4" ry="6.6"/>
      <path class="stripe" d="M9.2 17.6h11.6"/>
      <path class="stripe" d="M8 22h14"/>
      <path class="stripe" d="M9.6 26.2h10.8"/>
      <circle class="beye" cx="12.6" cy="19.8" r="1.15"/>
      <circle class="beye" cx="17.4" cy="19.8" r="1.15"/>
      <path class="bmouth" d="M13 21.6q2 1.5 4 0"/>
      <path class="antenna dark" d="M13 15.5l-1.4-3.4"/>
      <path class="antenna dark" d="M17 15.5l1.4-3.4"/>
    </g>`;
}


function buddyGnome(){
  return `<g class="legs">
      <line class="leg-a boot" x1="12.5" y1="28" x2="10.5" y2="33.5" style="transform-origin:12.5px 28px"/>
      <line class="leg-b boot" x1="17.5" y1="28" x2="19.5" y2="33.5" style="transform-origin:17.5px 28px"/>
    </g>
    <g class="body" style="transform-origin:15px 28px">
      <path class="coat" d="M9.4 29c0-5.4 2-8.6 5.6-8.6s5.6 3.2 5.6 8.6z"/>
      <path class="beard" d="M15 25.6c-3.2 0-5.2-2.2-5.2-5.4h10.4c0 3.2-2 5.4-5.2 5.4z"/>
      <circle class="face" cx="15" cy="16.5" r="4.3"/>
      <circle class="gcheek" cx="11.4" cy="17.6" r="1.1"/>
      <circle class="gcheek" cx="18.6" cy="17.6" r="1.1"/>
      <circle class="eye" cx="13.3" cy="16" r=".95"/>
      <circle class="eye" cx="16.7" cy="16" r=".95"/>
      <path class="hat" d="M15 8.4c3.4 0 5.6 2.6 5.6 5.6H9.4c0-3 2.2-5.6 5.6-5.6z"/>
      <ellipse class="hat-brim" cx="15" cy="14.2" rx="5.8" ry="1.1"/>
      <line class="arm coat-arm" x1="19.3" y1="24.2" x2="23.6" y2="20.6" style="transform-origin:19.3px 24.2px"/>
    </g>`;
}

const BUDDY_SPRITES={sprout:buddySprout, pepper:buddyPepper, cat:buddyCat,
                     snail:buddySnail, ladybug:buddyLadybug, drop:buddyDrop,
                     bee:buddyBee, gnome:buddyGnome};
// the snail walks at its own pace; everything else shares the standard cycle
const BUDDY_DURATION={snail:20000};
// Sprites drawn in profile have a natural facing. The walk flips them with
// scaleX so they always face the way they are travelling; a sprite drawn
// facing LEFT needs the opposite sign from one drawn facing right, or it
// moonwalks in one direction. Front-facing sprites are symmetric enough that
// either sign looks correct.
const BUDDY_FACES_LEFT={cat:true, snail:true};
// Characters that would naturally fly cruise through the card instead of
// walking along its floor, and at the pause they loop the loop instead of
// waving. The raindrop falls rather than flies, so it keeps walking.
const BUDDY_FLIES={bee:true, ladybug:true};
// headroom a loop needs above the flyer: two radii plus its own height
const LOOP_HEADROOM=76;

function walkerSvg(model){
  const draw=BUDDY_SPRITES[model]||buddySprout;
  const name=model in BUDDY_SPRITES ? model : 'sprout';
  // flyers get an inner group so the hover and the loop can move the whole
  // sprite without fighting the travel transform on the svg itself
  // two layers so the bob and the loop never fight over one transform: the
  // outer one bobs the whole time, the inner one does the loop inside it
  const inner=BUDDY_FLIES[name]
    ? `<g class="bob"><g class="flyer">${draw()}</g></g>` : draw();
  return `<svg class="walker buddy-${name}${BUDDY_FLIES[name]?' flying':''}" viewBox="0 0 30 36" aria-hidden="true" focusable="false">${inner}</svg>`;
}

function walkOnce(){
  // never interrupt: one seedling at a time, and none while the tab is hidden
  if(!buddyOn || document.hidden || document.querySelector('.walkwrap'))return;
  const cards=[...document.querySelectorAll('.card')].filter(c=>{
    if(c.offsetParent===null)return false;              // hidden card
    const r=c.getBoundingClientRect();
    return r.width>200 && r.height>90;                  // room to walk
  });
  if(!cards.length)return;
  const card=cards[Math.floor(Math.random()*cards.length)];
  const wrap=document.createElement('div');
  wrap.className='walkwrap';
  const model=buddyModel();
  wrap.innerHTML=walkerSvg(model);
  const dur=BUDDY_DURATION[model]||WALK_DUR_MS;
  card.appendChild(wrap);
  const w=card.clientWidth, rtl=Math.random()<0.5;
  const walker=wrap.querySelector('.walker');
  // start and end fully outside the clip, so it emerges from behind the edge
  walker.style.setProperty('--walk-from', (rtl? w+40 : -40)+'px');
  walker.style.setProperty('--walk-to',   (rtl? -40 : w+40)+'px');
  // stop somewhere in the middle third, not dead centre every time
  const mid=Math.round(w*(0.34+Math.random()*0.32)) - 15;
  walker.style.setProperty('--walk-mid',  (rtl? mid : mid)+'px');
  // rtl means travelling right-to-left, so the character must face left
  const facesLeft=!!BUDDY_FACES_LEFT[model];
  walker.style.setProperty('--walk-dir',
    facesLeft ? (rtl ? 1 : -1) : (rtl ? -1 : 1));
  walker.style.setProperty('--walk-dur',  dur+'ms');
  if(BUDDY_FLIES[model]){
    // cruise somewhere in the upper-middle of the card, low enough that the
    // loop still fits under the top edge on a short card
    const h=card.clientHeight;
    const want=Math.round(h*(0.35+Math.random()*0.25));
    walker.style.bottom=Math.max(10, Math.min(want, h-LOOP_HEADROOM))+'px';
  }
  // legs stop and the arm waves only while it is standing still
  const pauseAt=setTimeout(()=>walker.classList.add('pausing'), dur*PAUSE_START);
  const resumeAt=setTimeout(()=>walker.classList.remove('pausing'), dur*PAUSE_END);
  setTimeout(()=>{
    clearTimeout(pauseAt); clearTimeout(resumeAt); wrap.remove();
  }, dur+400);
}

function startWalker(){
  if(walkTimer)return;
  if(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches)
    return;                       // no ambient motion for anyone who opted out
  walkTimer=setInterval(walkOnce, WALK_EVERY_MS);
}

// ---- Pi health tiles ----
function hostTile(label, value, sub, cls){
  // numeric tiles are short and keep the large size; text values (hostname, IP)
  // can be long, so step the size down by length rather than breaking mid-word
  const plain=String(value).replace(/<[^>]*>/g,'');
  if(!/^[\d.,:%\s-]+$/.test(plain)){
    cls=(cls?cls+' ':'')+(plain.length>10?'long':'text');
  }
  return `<div class="htile"><div class="hlabel">${label}</div>`
    +`<div class="hvalue${cls?' '+cls:''}" title="${esc(plain)}">${value}</div>`
    +`<div class="hsub">${sub||'&nbsp;'}</div></div>`;
}
function upStr(sec){
  if(sec==null)return null;
  const d=Math.floor(sec/86400), h=Math.floor(sec%86400/3600), m=Math.floor(sec%3600/60);
  if(d)return `${d}d ${h}h`;
  if(h)return `${h}h ${String(m).padStart(2,'0')}m`;
  return `${m}m`;
}
async function loadHost(){
  const grid=document.getElementById('hgrid');
  if(!grid)return;
  try{
    const r=await fetch('/api/host');
    const h=await r.json();
    let out='';
    if(h.cpu_temp_c!=null){
      const f=h.cpu_temp_c*9/5+32;
      // Pi soft-throttles at 80C, hard at 85C
      const cls=h.cpu_temp_c>=80?'bad':(h.cpu_temp_c>=70?'warn':'good');
      out+=hostTile('CPU temp',
        isMetric()?`${h.cpu_temp_c.toFixed(1)}\u00b0C`:`${f.toFixed(1)}\u00b0F`,
        isMetric()?`${f.toFixed(0)}\u00b0F`:`${h.cpu_temp_c.toFixed(0)}\u00b0C`, cls);
    }
    if(h.load){
      const per=h.load['1m']/(h.load.cores||1);
      out+=hostTile('Load', h.load['1m'].toFixed(2),
        `${h.load.cores} core${h.load.cores>1?'s':''}`, per>1?'warn':'');
    }
    if(h.memory)
      out+=hostTile('Memory', h.memory.percent+'%',
        `${h.memory.used_mb}/${h.memory.total_mb} MB`, h.memory.percent>=90?'bad':'');
    if(h.disk)
      out+=hostTile('Disk', h.disk.percent+'%',
        `${h.disk.used_gb}/${h.disk.total_gb} GB`,
        h.disk.percent>=90?'bad':(h.disk.percent>=75?'warn':''));
    if(h.uptime_seconds!=null)
      out+=hostTile('Uptime', upStr(h.uptime_seconds), '');
    if(h.throttled){
      const t=h.throttled;
      const val=t.now.length?t.now[0]:(t.since_boot.length?'recovered':'healthy');
      const sub=t.now.length?'happening now'
        :(t.since_boot.length?`since boot: ${t.since_boot.join(', ')}`:'no issues');
      out+=hostTile('Power', val, sub, t.now.length?'bad':(t.since_boot.length?'warn':'good'));
    }
    if(h.core_voltage!=null)
      out+=hostTile('Core V', h.core_voltage.toFixed(4)+'V',
        h.cpu_mhz!=null?`${h.cpu_mhz} MHz`:'');
    else if(h.cpu_mhz!=null)
      out+=hostTile('CPU clock', h.cpu_mhz+' MHz','');
    if(h.host)
      out+=hostTile('Host', h.host, h.ip||'');
    if(h.wifi)
      out+=hostTile('WiFi', h.wifi.percent+'%',
        `${h.wifi.iface} ${h.wifi.dbm} dBm`, h.wifi.percent<35?'warn':'');
    grid.innerHTML=out||'<p class="rmuted">No device stats available.</p>';
  }catch(e){
    grid.innerHTML='<p class="rmuted">Device stats unavailable.</p>';
  }
}
function renderLightPlan(j){
  const box=document.getElementById('lplan');
  if(!box)return;
  const p=j.light_plan;
  if(!p){box.style.display='none';return;}
  box.style.display='';
  box.className='lplan '+p.status;
  const dot=document.getElementById('lpdot');
  if(dot)dot.className='lpdot '+p.status;
  const title=document.getElementById('lptitle');
  if(title){
    const when = p.day ? ` (${p.day})` : '';
    title.textContent = p.status==='no_sensor' ? 'No light sensor'
      : p.status==='pending'
      ? `Measuring \u00b7 ${(p.full_day||0).toFixed(1)} mol so far`
      : p.status==='ok'
      ? `On track \u00b7 ${p.full_day.toFixed(1)} mol${when}`
      : (p.status==='low'
         ? `Short on light \u00b7 ${p.full_day.toFixed(1)} mol${when}`
         : `More light than needed \u00b7 ${p.full_day.toFixed(1)} mol${when}`);
  }
  const ul=document.getElementById('lpadvice');
  if(ul)ul.innerHTML=(p.advice||[]).map(a=>`<li>${a}</li>`).join('');
}
// Daily light through today (solid) and yesterday (dashed) against the target:
// the band the day should end in, and the wedge where the total should be by
// each hour of the photoperiod.
function drawDliChart(day){
  const svg=document.getElementById('dlichart');
  if(!svg)return;
  const cv=day&&day.curve;
  if(!cv||(!(cv.today||[]).length&&!(cv.yesterday||[]).length)){svg.style.display='none';return;}
  svg.style.display='';
  const W=320,H=130,L=28,R=8,T=8,B=18;
  const {lo,hi,max}=dliBand;
  const top=Math.max(max, ...[...(cv.today||[]),...(cv.yesterday||[])].map(p=>p[1]*1.1));
  const x=m=>L+(W-L-R)*m/1440, y=v=>H-B-(H-T-B)*Math.min(v,top)/top;
  const path=pts=>pts.map((p,i)=>(i?'L':'M')+x(p[0]).toFixed(1)+' '+y(p[1]).toFixed(1)).join('');
  const on=S?mins(S.on):420, off=S?mins(S.off):1140;
  let h='';
  // target band for the whole day, across the chart
  h+=`<rect x="${L}" y="${y(hi).toFixed(1)}" width="${W-L-R}" height="${(y(lo)-y(hi)).toFixed(1)}" class="dcband"/>`;
  // pace wedge: 0 at lights on, the band at lights off
  if(off>on)h+=`<path d="M${x(on).toFixed(1)} ${y(0)} L${x(off).toFixed(1)} ${y(hi).toFixed(1)} `
    +`L${x(1440).toFixed(1)} ${y(hi).toFixed(1)} L${x(1440).toFixed(1)} ${y(lo).toFixed(1)} `
    +`L${x(off).toFixed(1)} ${y(lo).toFixed(1)} Z" class="dcpace"/>`;
  for(const hr of [0,6,12,18,24])
    h+=`<line x1="${x(hr*60)}" y1="${T}" x2="${x(hr*60)}" y2="${H-B}" class="dcgrid"/>`
      +`<text x="${x(hr*60)}" y="${H-5}" class="dcax" text-anchor="middle">${String(hr).padStart(2,'0')}</text>`;
  for(const v of [lo,hi])
    h+=`<text x="${L-4}" y="${(y(v)+3).toFixed(1)}" class="dcax" text-anchor="end">${v}</text>`;
  if((cv.yesterday||[]).length)h+=`<path d="${path(cv.yesterday)}" class="dcyest"/>`;
  if((cv.today||[]).length){
    const last=cv.today[cv.today.length-1];
    h+=`<path d="${path(cv.today)}" class="dctoday"/>`
      +`<circle cx="${x(last[0]).toFixed(1)}" cy="${y(last[1]).toFixed(1)}" r="3" class="dcnow"/>`;
  }
  svg.innerHTML=h;
}
function renderDayProgress(j){
  const wrap=document.getElementById('dayprog');
  if(!wrap||!S.on||!S.off)return;
  const on=+S.on, off=+S.off, now=Date.now();
  const span=off-on;
  if(!(span>0)){wrap.style.display='none';return;}
  wrap.style.display='';
  const frac=Math.max(0,Math.min(1,(now-on)/span));
  const fill=document.getElementById('dpfill');
  const marker=document.getElementById('dpnow');
  if(fill)fill.style.width=(frac*100).toFixed(1)+'%';
  if(marker)marker.style.left=(frac*100).toFixed(1)+'%';
  const left=document.getElementById('dpleft');
  if(left){
    if(now<on)      left.textContent='lights on in '+durStr(on-now);
    else if(now>off)left.textContent='lights off \u00b7 on again tomorrow';
    else            left.textContent=durStr(off-now)+' of light left';
  }
  const day=j.day_light||null;
  drawDliChart(day);
  // how long the light has actually delivered today
  const lit=document.getElementById('dplit');
  if(lit)lit.textContent=(day&&day.lit_minutes)?durStr(day.lit_minutes*60000)+' lit':'';

  // DLI against the seedling target band from Settings, Targets
  const {lo:BLO,hi:BHI,max:BMAX}=dliBand;
  const d=(day&&day.dli!=null)?day.dli:(lightMetrics&&lightMetrics.dli);
  const dfill=document.getElementById('dlifill');
  const dval=document.getElementById('dlival');
  const pace=document.getElementById('dlipace');
  if(dfill)dfill.style.width=Math.max(0,Math.min(100,(d||0)/BMAX*100)).toFixed(1)+'%';

  // Follow the schedule. During the photoperiod the fair comparison is not the
  // whole day's target band but where the total should be by NOW: that band
  // scaled by how far through the lit day we are. Judging a noon total of 5.0
  // against the full-day 6 called it "below target" while it was exactly on
  // pace. After lights out the full band applies again.
  const lo=BLO*frac, hi=BHI*frac;
  const during=frac>0&&frac<1;
  const pm=document.getElementById('dlipacemark');
  if(pm){
    pm.style.left=Math.max(0,Math.min(100,lo/BMAX*100)).toFixed(1)+'%';
    pm.style.width=Math.max(0.6,Math.min(100,(hi-lo)/BMAX*100)).toFixed(1)+'%';
    pm.style.display=during?'':'none';
    pm.title=`where today\u2019s total should be by now: ${lo.toFixed(1)}\u2013${hi.toFixed(1)} mol`;
  }
  // one verdict drives both the words and the fill color, so they can never
  // disagree: pace while the lights are on, the full-day target after
  let state='';
  if(d!=null){
    state = during ? (d<lo?'low':(d<=hi?'ok':'high'))
                   : (d<BLO?'low':(d<=BHI?'ok':'high'));
  }
  if(dfill)dfill.className='dlifill'+(state?' '+state:'');
  if(dval){
    if(d==null){dval.textContent='building today\u2019s total';}
    else if(during){
      const where={low:'behind pace',ok:'on pace',high:'ahead of pace'}[state];
      dval.innerHTML=`<b>${d.toFixed(1)}</b> mol \u00b7 ${where}`;
      dval.className='dli'+state;
    }else{
      const band={low:'below target',ok:'in target',high:'above target'}[state];
      dval.innerHTML=`<b>${d.toFixed(1)}</b> mol \u00b7 ${band}`;
      dval.className='dli'+state;
    }
  }
  if(pace){
    const fc=day&&day.forecast_remaining;
    if(d==null||frac<=0){pace.textContent='';}
    else if(frac>=1){
      pace.textContent='day complete';
      pace.className='dlipace';
    }else if(fc!=null){
      // schedule-aware: today's total = banked + what the remaining ramp and
      // full-brightness hours will deliver, from the measured light curve
      const proj=d+fc;
      const verdict=proj<BLO?'behind':(proj<=BHI?'on track':'ahead');
      pace.innerHTML=`forecast <b>${proj.toFixed(1)}</b> \u00b7 ${verdict}`;
      pace.className='dlipace '+(proj<BLO?'low':(proj<=BHI?'ok':'high'));
      pace.title=`${d.toFixed(1)} banked + ${fc.toFixed(1)} from the rest of `
        +'today\u2019s schedule (measured light curve)';
    }else if(frac<0.08){
      pace.textContent='too early to project';
      pace.className='dlipace';
    }else{
      const proj=d/frac;                       // fallback: no sweep on file yet
      const verdict=proj<BLO?'behind':(proj<=BHI?'on track':'ahead');
      pace.innerHTML=`projected <b>${proj.toFixed(1)}</b> \u00b7 ${verdict}`;
      pace.className='dlipace '+(proj<BLO?'low':(proj<=BHI?'ok':'high'));
      pace.title='rough estimate from today\u2019s average so far; '
        +'measure the light response curve for a schedule-aware forecast';
    }
  }
  // peak intensity reached today
  const stats=document.getElementById('daystats');
  if(stats){
    // each pair wrapped so the label and value stay on one line together;
    // a bare dt/dd sequence in a flex row separates them
    let h2='';
    if(day&&day.peak_ppfd!=null)
      h2+=`<div><dt>Peak today</dt><dd>${Math.round(day.peak_ppfd)} <small>\u00b5mol</small></dd></div>`;
    if(day&&day.peak_lux!=null)
      h2+=`<div><dt>Peak light</dt><dd>${Math.round(day.peak_lux).toLocaleString()} <small>lx</small></dd></div>`;
    stats.innerHTML=h2;
    stats.style.display=h2?'':'none';
  }
}
function durStr(ms){
  const m=Math.max(0,Math.round(ms/60000));
  if(m<60)return m+' min';
  return Math.floor(m/60)+'h '+String(m%60).padStart(2,'0')+'m';
}
let fanDragging=false, fanDragTimer=null, fanDragPending=null;
function renderFan(j){
  const row=document.getElementById('fanrow');
  const sl=document.getElementById('fanslider');
  if(!row)return;
  const f=j.fan;
  if(!f||!f.hw){row.style.display='none';if(sl)sl.style.display='none';return;}
  row.style.display='';
  document.querySelectorAll('.fanbtn').forEach(b=>
    b.classList.toggle('on', b.dataset.mode===f.mode));
  const info=document.getElementById('faninfo');
  if(info)info.textContent=f.on?`${f.speed}% \u00b7 ${f.reason}`
                               :(f.mode==='auto'?`idle \u00b7 ${f.reason}`:'off');
  // the slider edits manual speed in "on", auto speed in "auto"; hidden in "off"
  if(sl){
    sl.style.display=(f.mode==='off')?'none':'';
    sl.classList.toggle('dim', f.mode==='off');
    const rng=document.getElementById('fanrange'), val=document.getElementById('fanval');
    const target=(f.mode==='on')?f.manual_speed:f.auto_speed;
    if(rng&&!fanDragging&&document.activeElement!==rng)rng.value=target;
    if(val&&!fanDragging)val.textContent=(rng?rng.value:target)+'%';
    const lbl=sl.querySelector('.fanslabel');
    if(lbl)lbl.textContent=(f.mode==='on')?'speed':'auto speed';
  }
}
function pushFanSpeed(v){
  fanDragPending=v;
  if(fanDragTimer)return;
  fanDragTimer=setTimeout(()=>{
    fanDragTimer=null;
    const val=fanDragPending; fanDragPending=null;
    if(val!=null)sendFanSpeed(val);
  },150);
}
async function sendFanSpeed(v){
  const mode=(document.querySelector('.fanbtn.on')||{dataset:{}}).dataset.mode||'auto';
  const body=(mode==='on')?{speed:v}:{auto_speed:v};
  try{
    await fetch('/api/fan',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)});
  }catch(e){}
}
async function setFan(mode){
  const info=document.getElementById('faninfo');
  if(info)info.textContent='\u2026';
  try{
    const r=await fetch('/api/fan',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})});
    const j=await r.json().catch(()=>({}));
    if(r.status===401&&info){info.textContent='log in first';return;}
    if(!j.ok&&info)info.textContent=j.error||'failed';
  }catch(e){if(info)info.textContent='request failed';}
  refresh();
}
let focusRunning=false;
async function startFocusSweep(){
  const info=document.getElementById('focusinfo');
  if(focusRunning){
    await fetch('/api/focus_sweep',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cancel:true})});
    return;
  }
  if(info)info.textContent='starting\u2026';
  try{
    const r=await fetch('/api/focus_sweep',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
    const j=await r.json().catch(()=>({}));
    if(r.status===401){if(info)info.textContent='log in first';return;}
    if(!j.ok){if(info)info.textContent=j.error||'failed';return;}
    if(info)info.textContent=`sweeping\u2026 (~${j.estimate_seconds}s; timelapse pauses)`;
  }catch(e){if(info)info.textContent='request failed';}
}
function renderFocus(j){
  const btn=document.getElementById('focusbtn');
  const info=document.getElementById('focusinfo');
  const f=j.focus||{};
  const was=focusRunning; focusRunning=!!f.running;
  if(btn)btn.textContent=focusRunning?'Cancel':'Focus sweep';
  if(focusRunning&&info)
    info.textContent=`sweeping\u2026 ${f.step}${f.total?'/'+f.total:''} points`;
  if(was&&!focusRunning&&info){
    if(f.error)info.textContent=f.error;
    else if(f.best)info.textContent=
      `pinned focus ${f.best.focus} (score ${f.best.score}, ${f.best.tested} points)`;
    else info.textContent='cancelled';
  }
}
function renderSweep(j){
  const btn=document.getElementById('sweepbtn');
  const info=document.getElementById('sweepinfo');
  const sw=j.sweep||{};
  const was=sweepRunning; sweepRunning=!!sw.running;
  if(btn)btn.textContent=sweepRunning?'Cancel':'Calibrate';
  if(sweepRunning&&info)info.textContent=`measuring\u2026 ${sw.pct}%`;
  if(was&&!sweepRunning&&info&&sw.error)info.textContent=sw.error;
  // with a calibration in use, chart what the dashboard DELIVERS (a straight
  // line if it worked) rather than the raw fixture, which never changes shape
  lastCurve = j.light_curve_effective
    ? {points:j.light_curve_effective, ts:(j.light_curve||{}).ts, calibrated:true,
       rawPoints:(j.light_curve||{}).points||[]}
    : (j.light_curve ? {...j.light_curve, stale:!!j.light_linear_stale} : null);
  if(!dragging)                                   // a drag owns the marker
    drawLightCurve(lastCurve, sweepRunning,
                   sweepRunning?null:(j.brightness!=null?j.brightness:null));
}
function showScheduleMode(mode){
  document.querySelectorAll('.modeblock').forEach(b=>{
    b.style.display=(b.dataset.mode===mode)?'':'none';
  });
}
// show a manual block only when its auto checkbox is clear
const USB_AUTO=[['usb_auto_focus','manual-focus'],
                ['usb_auto_exposure_on','manual-exposure'],
                ['usb_auto_white_balance','manual-wb']];
function syncUsbAuto(){
  const f=document.getElementById('cfgform');
  if(!f)return;
  const usb=(f.elements['camera_backend']||{}).value==='usb';
  for(const [name,cls] of USB_AUTO){
    const on=f.elements[name] && f.elements[name].checked;
    document.querySelectorAll('.'+cls).forEach(el=>
      el.style.display=(usb && !on)?'':'none');
  }
}
function initCameraBackend(){
  for(const [name] of USB_AUTO){
    const cb=document.querySelector(`[name=${name}]`);
    if(cb && !cb.dataset.bound){cb.dataset.bound='1';
      cb.addEventListener('change',syncUsbAuto);}
  }
  const cb=document.querySelector('[name=camera_backend]');
  if(!cb||cb.dataset.bound)return;
  cb.dataset.bound='1';
  cb.addEventListener('change',()=>{
    document.querySelectorAll('.usbonly').forEach(el=>
      el.style.display=(cb.value==='usb')?'':'none');
    syncUsbAuto();
  });
}
function initSchedule(){
  const sm=document.querySelector('[name=schedule_mode]');
  if(!sm)return;
  sm.addEventListener('change',()=>showScheduleMode(sm.value));
}
function initLight(){
  {const row=document.getElementById('lightctl')||document.body;
   if(row.dataset.lightBound)return;            // double-binding would fire
   row.dataset.lightBound='1';}                 // every click twice
  {const b=document.getElementById('sweepbtn');
   if(b)b.addEventListener('click',startSweep);}
  {const qt=document.getElementById('qtoggle'), qb=document.getElementById('qbody');
   if(qt&&qb)qt.addEventListener('click',()=>{
     const open=qb.hasAttribute('hidden');
     if(open)qb.removeAttribute('hidden'); else qb.setAttribute('hidden','');
     qt.setAttribute('aria-expanded', open?'true':'false');});}
  {const fb=document.getElementById('focusbtn');
   if(fb)fb.addEventListener('click',startFocusSweep);}
  document.querySelectorAll('.fanbtn').forEach(b=>
    b.addEventListener('click',()=>setFan(b.dataset.mode)));
  {const fr=document.getElementById('fanrange'), fv=document.getElementById('fanval');
   if(fr){
     const start=()=>{fanDragging=true;};
     const end=()=>{if(!fanDragging)return;fanDragging=false;
       clearTimeout(fanDragTimer);fanDragTimer=null;
       sendFanSpeed(+fr.value).then(()=>refresh());};
     fr.addEventListener('pointerdown',start);
     fr.addEventListener('keydown',start);
     fr.addEventListener('input',()=>{
       if(fv)fv.textContent=fr.value+'%';
       if(fanDragging)pushFanSpeed(+fr.value);
     });
     fr.addEventListener('pointerup',end);
     fr.addEventListener('pointercancel',end);
     fr.addEventListener('change',end);
     fr.addEventListener('blur',end);
   }}
  document.querySelectorAll('.lcbtn:not(.fanbtn)').forEach(b=>
    b.addEventListener('click',()=>setLight(b.dataset.mode)));
  const rng=document.getElementById('lcrange');
  const val=document.getElementById('lcval');
  if(!rng)return;
  const startDrag=()=>{dragging=true;};
  const endDrag=()=>{
    if(!dragging)return;
    dragging=false;
    if(dragTimer){clearTimeout(dragTimer);dragTimer=null;}
    dragPending=null;
    setLight(null, +rng.value);        // final value, with UI sync
  };
  rng.addEventListener('pointerdown',startDrag);
  rng.addEventListener('keydown',startDrag);
  rng.addEventListener('input',()=>{
    const v=+rng.value;
    if(val)val.textContent=v+'%';           // slider label tracks instantly
    if(dragging){
      pushBrightness(v);                    // light tracks, throttled
      // the readouts follow the drag rather than the 15s status poll
      const pctEl=document.getElementById('pct');
      if(pctEl)pctEl.textContent=v+'%';
      const ph=document.querySelector('.aphase');
      if(ph)ph.style.setProperty('--lum',(v/100).toFixed(2));
      drawLightCurve(lastCurve, false, v);
    }
  });
  rng.addEventListener('pointerup',endDrag);
  rng.addEventListener('pointercancel',endDrag);
  rng.addEventListener('keyup',endDrag);
  rng.addEventListener('blur',endDrag);
  rng.addEventListener('change',()=>{        // click-to-jump, no drag involved
    if(!dragging)setLight(null, +rng.value);
  });
}
[initAuth, initSensors, initGridSvg, initReport, initLight, initTrays, initSchedule, initTrayConfig, initCameraBackend, initPlantings, initBackup, initPlug, initTheme, initStorm, startWalker].forEach(fn=>{  try{ fn(); }catch(e){ console.error(fn.name+' init failed:', e); }
});
refresh();
