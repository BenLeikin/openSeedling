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
    <text x="${L}" y="${y(100)-4}" font-size="11" fill="#3f7d45">${S.max}%</text>`;
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
  showLightMode(S.light_override, S.manual_bright);
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
  document.getElementById('cfg').textContent=
    `GPIO${S.gpio} \u00b7 ${S.freq} Hz PWM \u00b7 updates every ${S.loop}s`;
  draw();
}

let lightMode='auto', dragging=false;
function showLightMode(mode, bright){
  mode = mode || 'auto';
  lightMode = mode;
  document.querySelectorAll('.lcbtn').forEach(b=>
    b.classList.toggle('on', b.dataset.mode===mode));
  const info=document.getElementById('lightinfo');
  if(info)info.textContent = (mode==='on'||mode==='off')
    ? 'holding '+mode+' - schedule paused' : '';
  const wrap=document.getElementById('lcslider');
  const rng=document.getElementById('lcrange');
  const val=document.getElementById('lcval');
  if(!wrap||!rng)return;
  const live = mode==='on';
  wrap.classList.toggle('dim', !live);
  rng.disabled = !live;
  if(bright!=null && !dragging){      // never move the thumb under the user
    rng.value=bright;
    if(val)val.textContent=bright+'%';
  }
}
async function setLight(mode, brightness, quiet){
  const info=document.getElementById('lightinfo');
  if(info && !quiet)info.textContent='...';
  const body={};
  if(mode!=null)body.mode=mode;
  if(brightness!=null)body.brightness=brightness;
  try{
    const r=await fetch('/api/light',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const j=await r.json().catch(()=>({}));
    if(r.ok&&j.ok){
      if(quiet){lightMode=j.mode;}          // mid-drag: don't touch the slider
      else {showLightMode(j.mode, j.brightness);refresh();}
    }
    else if(info)info.textContent = r.status===401?'log in to control the light'
                      :('failed: '+(j.error||('HTTP '+r.status)));
  }catch(e){if(info && !quiet)info.textContent='request failed';}
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
        +(camHealth.last_err?`<br><span class="camerr">${camHealth.last_err}</span>`:'');
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
  if(aligning){card.style.display='';return;}   // live preview owns the image
  if(!j.photo_count){
    img.style.display='none';
    document.getElementById('photoinfo').textContent='No photos yet.';
    card.style.display=canEdit?'':'none';   // keep visible so Take photo is reachable
    return;
  }
  img.style.display='';
  card.style.display='';
  img.src='/photo/latest?'+ (j.latest_photo_time||Date.now());
  const when=j.latest_photo_time?new Date(j.latest_photo_time):null;
  document.getElementById('photoinfo').textContent=
    (when?`Taken ${when.toLocaleString()}`:'')+` \u00b7 ${j.photo_count} photos so far`;
}
async function capturePhoto(){
  const btn=document.getElementById('capturebtn');
  const info=document.getElementById('captureinfo');
  if(!btn||btn.disabled)return;
  btn.disabled=true;
  if(info)info.textContent='Capturing\u2026 (~5s)';
  try{
    const r=await fetch('/api/capture',{method:'POST'});
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
    const r=await fetch('/api/preview',{method:'POST'});
    const j=await r.json();
    if(j.ok){
      const img=document.getElementById('photo');
      img.style.display='';
      img.src='/preview.jpg?'+j.ts;     // busy/error: keep the last frame
    }
  }catch(e){}
  if(aligning)alignTimer=setTimeout(alignTick,1200);
}
function startAlign(){
  if(aligning)return;
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

function fillForm(cfg){
  const f=document.getElementById('cfgform');
  for(const k of ['latitude','longitude','timezone','max_bright','ramp_min',
                  'sunrise_offset_min','sunset_offset_min',
                  'capture_interval_min','capture_brightness','roi',
                  'lux_to_ppfd_k','alert_sustain_min','alert_cooldown_hours',
                  'alert_dry_pct','alert_humidity_high'])
    if(f.elements[k] && document.activeElement!==f.elements[k])
      f.elements[k].value=cfg[k];
  {const ae=f.elements['alerts_enabled'];
   if(ae&&document.activeElement!==ae)ae.checked=cfg.alerts_enabled!==false;
   const cold=f.elements['alert_soil_low_f'];
   if(cold&&document.activeElement!==cold){
     const fv=+cfg.alert_soil_low_f||0;
     cold.value=fv?Math.round(tFromF(fv)):0;
   }
   const cl=document.getElementById('alcoldlbl');
   if(cl)cl.innerHTML=tUnit();}
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
}

let frames=[],fidx=0,ptimer=null;
function frameLabel(n){
  const m=n.match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})/);
  return m?`${m[2]}/${m[3]} ${m[4]}:${m[5]}`:n;
}
function showFrame(){
  if(!frames.length)return;
  document.getElementById('vframe').src='/thumb/'+frames[fidx];
  document.getElementById('scrub').value=fidx;
  document.getElementById('pframe').textContent=
    `${frameLabel(frames[fidx])} \u00b7 ${fidx+1}/${frames.length}`;
  (new Image()).src='/thumb/'+frames[(fidx+1)%frames.length];
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
async function loadFrames(){
  if(window._camOn===false)return;
  try{
    const r=await fetch('/api/photos');const j=await r.json();
    const had=frames.length;
    frames=j.names||[];
    const card=document.getElementById('videocard');
    if(frames.length<2){card.style.display='none';return;}
    card.style.display='';
    document.getElementById('scrub').max=frames.length-1;
    if(!had){fidx=frames.length-1;showFrame();}
  }catch(e){}
}
document.getElementById('playbtn').addEventListener('click',togglePlay);
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
    const r=await fetch('/api/render',{method:'POST'});
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
    if(r.ok){pw.value='';refresh();}
    else err.textContent='wrong password';
  }catch(e){err.textContent='login failed';}
}
async function doLogout(){
  try{await fetch('/api/logout',{method:'POST'});}catch(e){}
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
let dryCal={};                 // per-cell {wet,dry} brightness anchors
let probeCal={}, probeNames={}, probeDefaultCal=null;   // per-tray anchors, labels, fallback
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
const DRY_SPAN_DEFAULT=15;     // provisional wet->dry brightness span pre-calibration
function camMoisture(cell, b){
  const c=dryCal[cell];
  if(!c || c.wet==null) return null;          // not calibrated -> no %
  const wet=c.wet, dry=(c.dry!=null?c.dry:wet+DRY_SPAN_DEFAULT);
  if(dry<=wet) return null;
  return Math.max(0, Math.min(100, 100*(dry-b)/(dry-wet)));
}
let chartHours=168;

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
  if(key.startsWith('moisture:')){
    const cell=key.slice(9);
    const nm=(grid&&grid.names&&grid.names[cell])?grid.names[cell]:cell;
    return {group:'Moisture', label:nm, value:val.toFixed(0), unit:'%'};
  }
  if(key.startsWith('growth_px:')){
    const cell=key.slice(10);
    const nm=(grid&&grid.names&&grid.names[cell])?grid.names[cell]:cell;
    return {group:'Growth', label:nm+' area', value:Math.round(val).toLocaleString(), unit:'px'};
  }
  if(key.startsWith('growth:')){
    const cell=key.slice(7);
    const nm=(grid&&grid.names&&grid.names[cell])?grid.names[cell]:cell;
    return {group:'Growth', label:nm, value:val.toFixed(1), unit:'%'};
  }
  if(key.startsWith('probe:')){
    const t=key.slice(6);
    const nm=probeNames[t]||('Tray '+t);
    const m=probeMoisture(t, val);
    if(m) return {group:'Soil', label:nm,
                  value:(m.approx?'~':'')+m.pct.toFixed(0), unit:'%',
                  title:val.toFixed(3)+'V'+(m.approx?' - estimated, not yet calibrated':'')};
    return {group:'Soil', label:nm, value:val.toFixed(3), unit:'V'};
  }
  if(key.startsWith('dry:')){
    const cell=key.slice(4);
    const nm=(grid&&grid.names&&grid.names[cell])?grid.names[cell]:cell;
    const m=camMoisture(cell, val);
    if(m!=null) return {group:'Moisture (cam)', label:nm, value:m.toFixed(0), unit:'%'};
    // uncalibrated: show raw surface-brightness index (higher = drier)
    return {group:'Dryness', label:nm, value:val.toFixed(0), unit:''};
  }
  return {group:'Other', label:key, value:String(val), unit:''};
}
function renderSensors(j){
  sensorData=j.sensors||{};
  dryCal=(j.settings&&j.settings.dryness_cal)||{};
  probeCal=(j.settings&&j.settings.probe_cal)||{};
  probeNames=(j.settings&&j.settings.probe_names)||{};
  if(j.probe_default_cal)probeDefaultCal=j.probe_default_cal;
  presTrend=j.pressure_tendency||null;
  lightMetrics=j.light_metrics||null;
  if(j.settings&&j.settings.units)units=j.settings.units;
  if(j.settings&&j.settings.soil_temp_high_f!=null)
    soilTempHigh=+j.settings.soil_temp_high_f;
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
  const keys=Object.keys(sensorData);
  card.style.display=keys.length?'':'none';
  // grouped readout
  const groups={};
  for(const k of keys){
    if(k.startsWith('growth_px:'))continue;   // raw counts are chart-only
    if(k.startsWith('float:'))continue;       // shown in the water controls instead
    const v=sensorData[k].value;
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
       const lim=(m.key0&&(m.key0.startsWith('dry:')||m.key0.startsWith('growth:')))?3*capMin:3*sampleMin;
       const isStale=ts&&(Date.now()/1000-ts)>lim*60;
       if(isStale){m.stale=true;m.title=(m.title?m.title+' \u00b7 ':'')
         +'last reading '+agoStr(new Date(ts*1000));}}
      {const fill=(m.unit==='%'&&!isNaN(parseFloat(m.value)))
          ?` style="--fill:${Math.max(0,Math.min(100,parseFloat(m.value)))}%" data-fill`:'' ;
       h+=`<span class="schip${m.stale?' stale':''}"${fill}${m.title?` title="${m.title}"`:''}>${m.label} <b>${m.value}</b><span class="u">${m.unit}</span>${m.suffix||''}</span>`;}
    }
    h+='</div>';
  }
  if(!h){
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
         extra+=`<span class="schip" title="lux \u00f7 ${lightMetrics.k} (fixture spectrum factor)">`
           +`PPFD <b>${Math.round(lightMetrics.ppfd)}</b><span class="u">\u00b5mol</span></span>`;
       if(lightMetrics.dli!=null){
         const d=lightMetrics.dli, cls=(d>=6&&d<=12)?'ok':(d<6?'low':'high');
         extra+=`<span class="schip dli ${cls}" title="daily light integral so far today \u00b7 seedlings want 6-12 mol/m\u00b2/day">`
           +`DLI <b>${d.toFixed(1)}</b><span class="u">mol</span></span>`;
       }
       if(extra)envg.insertAdjacentHTML('beforeend',extra);
     }
   }}
  const dc=document.getElementById('drycal');
  if(dc)dc.style.display = keys.some(k=>k.startsWith('dry:')) ? '' : 'none';
  const pcctl=document.getElementById('probecal');
  if(pcctl)pcctl.style.display = keys.some(k=>k.startsWith('probe:')) ? '' : 'none';
  const ptc=document.getElementById('probetc');
  if(ptc)ptc.style.display = (keys.some(k=>k.startsWith('probe:'))
                              && keys.some(k=>k.startsWith('temp:soil'))) ? '' : 'none';
  // collapse the whole calibration section if nothing in it applies
  {const cw=document.querySelector('.calwrap');
   if(cw)cw.style.display=[dc,pcctl,ptc].some(el=>el&&el.style.display!=='none')?'':'none';}
  if(grid)drawGrid();   // refresh per-cell overlay
}
// ---- chart grid: every sensor visible at once, grouped by section ----
const CHART_SECTIONS=[
  // ordered by how often they drive a decision, not by sensor type
  {id:'soil',   title:'Soil',
   match:k=>k.startsWith('temp:soil')||k.startsWith('probe:')},
  {id:'env',    title:'Environment',
   match:k=>k==='temp:air'||k==='humidity'||k==='lux'||k==='ppfd'||k==='pressure'},
  {id:'cam',    title:'Camera moisture',  match:k=>k.startsWith('dry:')||k.startsWith('moisture:')},
  {id:'growth', title:'Growth',           match:k=>k.startsWith('growth:')},
  {id:'other',  title:'Other',            match:k=>true},
];
let seriesData={}, chartPlots={}, soilTempHigh=90;
function chartUnitFor(s){
  if(s.startsWith('temp:'))return tUnit();
  if(s.startsWith('humidity')||s.startsWith('moisture:')||s.startsWith('growth:'))return '%';
  if(s.startsWith('probe:')){const t=s.slice(6);
    return (probeCal[t]&&probeCal[t].wet!=null)||probeDefaultCal?'%':'V';}
  if(s.startsWith('dry:')){const c=dryCal[s.slice(4)];return (c&&c.wet!=null)?'%':'';}
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
  if(s.startsWith('dry:')){const cell=s.slice(4);
    const cal=dryCal[cell];
    if(cal&&cal.wet!=null)return v=>camMoisture(cell,v);
  }
  return v=>v;
}
let lastChartLoad=0;
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
function renderChartGrid(){
  const grid=document.getElementById('chartgrid');
  if(!grid)return;
  const keys=Object.keys(seriesData).filter(k=>!k.startsWith('float:')).sort();
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
      h+=`<div class="ccard">
            <div class="chead"><span>${m.label}<span class="cunit">${chartUnitFor(k)}</span></span>
              <span class="cstats" id="cs-${cssId(k)}">&mdash;</span></div>
            <svg class="cmini" id="cv-${cssId(k)}" viewBox="0 0 320 110"
                 preserveAspectRatio="none" role="img" aria-label="${m.label} history"></svg>
          </div>`;
    }
    h+='</div></div>';
  }
  grid.innerHTML=h;
  chartPlots={};
  for(const k of keys)drawMini(k);
}
function cssId(k){return k.replace(/[^a-zA-Z0-9]/g,'_');}
function drawMini(key){
  const svg=document.getElementById('cv-'+cssId(key));
  const stat=document.getElementById('cs-'+cssId(key));
  if(!svg)return;
  const W=320,H=110,P=6,B=16;         // B: bottom room for time labels
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
  // warning line on soil temp charts (chile germination upper limit)
  const hiLine=(key.startsWith('temp:soil')&&soilTempHigh>0)?tFromF(soilTempHigh):null;
  if(hiLine!=null){y0=Math.min(y0,hiLine);y1=Math.max(y1,hiLine);}  // keep it on-chart
  if(pct){y0=Math.min(y0,0);y1=Math.max(y1,100);}   // % charts on a fixed scale
  if(y0===y1){y0-=1;y1+=1;}
  const pad=(y1-y0)*0.08; if(!pct){y0-=pad;y1+=pad;}
  const sx=t=>P+(t-x0)/((x1-x0)||1)*(W-2*P);
  const sy=v=>H-B-(v-y0)/((y1-y0)||1)*(H-B-P);
  let h='';
  for(let i=0;i<=2;i++){const yy=P+(H-B-P)*i/2;
    h+=`<line x1="${P}" y1="${yy}" x2="${W-P}" y2="${yy}" stroke="#e6f0de" stroke-width="1"/>`;}
  const line=data.map(d=>`${sx(d[0]).toFixed(1)},${sy(d[1]).toFixed(1)}`).join(' ');
  const area=`${P},${H-B} ${line} ${W-P},${H-B}`;
  h+=`<polygon points="${area}" fill="rgba(74,124,89,0.10)"/>`;
  h+=`<polyline fill="none" stroke="#4a7c59" stroke-width="1.8" points="${line}"
        pathLength="1" class="cline" vector-effect="non-scaling-stroke"/>`;
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
  if(hiLine!=null){
    const hy=sy(hiLine);
    if(hy>=P&&hy<=H-B){
      h+=`<rect x="${P}" y="${P}" width="${W-2*P}" height="${Math.max(0,hy-P).toFixed(1)}"
            fill="rgba(181,50,47,0.07)"/>`;
      h+=`<line x1="${P}" y1="${hy.toFixed(1)}" x2="${W-P}" y2="${hy.toFixed(1)}"
            stroke="#b5322f" stroke-width="1.2" stroke-dasharray="5 4"
            vector-effect="non-scaling-stroke"/>`;
      h+=`<text x="${W-P-2}" y="${(hy-3).toFixed(1)}" text-anchor="end" font-size="9"
            fill="#b5322f">too warm ${Math.round(tFromF(soilTempHigh))}${tUnit()}</text>`;
    }
  }
  const fmtT=t=>{const d=new Date(t*1000);
    return chartHours<=24?d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})
                         :d.toLocaleDateString([],{month:'numeric',day:'numeric'});};
  h+=`<text x="${P}" y="${H-4}" font-size="9" fill="#7a8a72">${fmtT(x0)}</text>`;
  h+=`<text x="${W-P}" y="${H-4}" font-size="9" fill="#7a8a72" text-anchor="end">${fmtT(x1)}</text>`;
  h+=`<line class="hvl" y1="${P}" y2="${H-B}" stroke="#4a7c59" stroke-width="1"
        stroke-dasharray="3 3" style="display:none"/>`;
  h+=`<circle class="hdot" r="3" fill="#2e7d32" stroke="#fff" stroke-width="1.2" style="display:none"/>`;
  svg.innerHTML=h;
  const dec=(unit==='%')?0:(unit==='lx'?0:(unit==='inHg'?2:(unit==='hPa'?0:1)));
  const cur=ys[ys.length-1], lo=Math.min(...ys), hi=Math.max(...ys);
  const over=hiLine!=null&&cur>hiLine;
  const lim=(key.startsWith('dry:')||key.startsWith('growth:'))?3*capMin:3*sampleMin;
  const lastTs=xs[xs.length-1];
  const isStale=(Date.now()/1000-lastTs)>lim*60;
  svg.classList.toggle('cstale', isStale);
  if(stat)stat.innerHTML=`<b${over?' class="hot"':''}>${cur.toFixed(dec)}</b>`
    +` \u00b7 lo ${lo.toFixed(dec)} \u00b7 hi ${hi.toFixed(dec)}`
    +(isStale?` <span class="stalebadge" title="last point ${agoStr(new Date(lastTs*1000))}">stale</span>`:'');
  chartPlots[key]={unit,dec,
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
  const tip=document.getElementById('charttip');
  if(vl){vl.setAttribute('x1',best.x);vl.setAttribute('x2',best.x);vl.style.display='';}
  if(dot){dot.setAttribute('cx',best.x);dot.setAttribute('cy',best.y);dot.style.display='';}
  if(tip){
    tip.textContent=`${best.v.toFixed(plot.dec)}${plot.unit} \u00b7 ${plot.fmt(best.t)}`;
    tip.style.display='';tip.style.left=(e.clientX+12)+'px';tip.style.top=(e.clientY-32)+'px';
  }
}
function chartLeave(){
  document.querySelectorAll('svg.cmini .hvl, svg.cmini .hdot')
    .forEach(el=>el.style.display='none');
  const tip=document.getElementById('charttip');
  if(tip)tip.style.display='none';
}
function floatLabel(v){
  return v===null ? 'no sensor' : (v>=1 ? 'not full' : 'full');
}
async function pollFloat(){
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
  const anyRunning=Object.values(w.trays).some(t=>t.running);
  for(const t of Object.keys(w.trays).sort()){
    const tw=w.trays[t];
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
    else if(tw.last&&!tw.running)info.textContent='last: '+tw.last;
  }
}
async function waterAct(tray, body, msg){
  const info=document.getElementById('pumpinfo'+tray);
  if(info)info.textContent=msg;
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
async function calibrate(point){
  const info=document.getElementById('calinfo');info.textContent='saving...';
  try{
    const r=await fetch('/api/dryness_cal',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({point})});
    const j=await r.json().catch(()=>({}));
    if(r.ok&&j.ok){info.textContent=`${point} set (${j.cells} cells)`;refresh();}
    else info.textContent = r.status===401?'log in to calibrate'
                          :('failed: '+(j.error||('HTTP '+r.status)));
  }catch(e){info.textContent='calibration failed';}
}
async function calibrateProbe(tray, point){
  const info=document.getElementById('probecalinfo');if(info)info.textContent='sampling\u2026 (~2s)';
  try{
    const r=await fetch('/api/probe_cal',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({tray,point})});
    const j=await r.json().catch(()=>({}));
    if(r.ok&&j.ok){
      if(info)info.textContent=`Tray ${tray} ${point} = ${j.volts}V`
        + (j.noisy?` (noisy, spread ${j.spread}V)`:'');
      refresh();
    }
    else if(info)info.textContent = r.status===401?'log in to calibrate'
                        :('failed: '+(j.error||('HTTP '+r.status)));
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
let trays={}, trayDirty={}, trayTimer=null;
function daysSince(iso){
  if(!iso)return null;
  const d=new Date(iso+'T00:00:00'), now=new Date();
  if(isNaN(d))return null;
  return Math.floor((new Date(now.getFullYear(),now.getMonth(),now.getDate())-d)/86400000);
}
function renderTrays(j){
  const t=(j.settings&&j.settings.trays)||{};
  const sig=JSON.stringify(t);
  const wrap=document.getElementById('trayswrap');
  if(!wrap)return;
  // don't clobber what's being typed
  if(wrap.dataset.sig===sig && wrap.children.length)return;
  if(document.activeElement && document.activeElement.closest('#trayswrap'))return;
  wrap.dataset.sig=sig;
  trays=JSON.parse(sig);
  let h='';
  for(const id of Object.keys(trays).sort()){
    const tr=trays[id]||{}, cells=tr.cells||{};
    const rows=tr.rows||3, cols=tr.cols||4;
    const filled=Object.keys(cells).length;
    h+=`<div class="tray"><div class="tray-head"><b>${tr.label||('Tray '+id)}</b>`
      +`<span class="tsum">${filled} of ${rows*cols} cells filled</span></div>`
      +`<div class="tgrid" style="grid-template-columns:repeat(${cols},1fr)">`;
    for(let r=1;r<=rows;r++){
      for(let c=0;c<cols;c++){
        const cid=colL(c)+r, v=cells[cid]||{};
        const age=daysSince(v.planted);
        h+=`<div class="tcell${(v.seed||v.equipment||v.planted)?' filled':''}" data-tray="${id}" data-cell="${cid}">
              <span class="tid">${cid}<span class="tage">${age==null?'':(age+'d')}</span></span>
              <input class="tseed"  type="text" placeholder="seed"      value="${esc(v.seed||'')}">
              <input class="tequip" type="text" placeholder="equipment" value="${esc(v.equipment||'')}">
              <input class="tdate"  type="date" value="${esc(v.planted||'')}">
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
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;')
  .replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function collectTray(id){
  const cells={};
  document.querySelectorAll(`#trayswrap .tcell[data-tray="${id}"]`).forEach(el=>{
    const seed=el.querySelector('.tseed').value.trim();
    const equipment=el.querySelector('.tequip').value.trim();
    const planted=el.querySelector('.tdate').value;
    if(seed||equipment||planted)cells[el.dataset.cell]={seed,equipment,planted};
  });
  return cells;
}
async function saveTray(id){
  const info=document.getElementById('trayinfo');
  try{
    const r=await fetch('/api/trays',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({tray:id, cells:collectTray(id)})});
    const j=await r.json().catch(()=>({}));
    if(r.status===401){if(info)info.textContent='log in to edit the map';return;}
    if(info)info.textContent = (r.ok&&j.ok)?`saved (${j.count} cells)`
                                          :('save failed: '+(j.error||r.status));
    if(r.ok&&j.ok){
      const wrap=document.getElementById('trayswrap');
      if(wrap)wrap.dataset.sig='';            // let the next refresh re-render ages
      setTimeout(()=>{if(info&&/saved/.test(info.textContent))info.textContent='';},2500);
    }
  }catch(e){if(info)info.textContent='save failed';}
}
function initTrays(){
  const wrap=document.getElementById('trayswrap');
  if(!wrap)return;
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
}
function initSensors(){
  const cw=document.getElementById('calwet');
  if(cw)cw.addEventListener('click',()=>calibrate('wet'));
  const cd=document.getElementById('caldry');
  if(cd)cd.addEventListener('click',()=>calibrate('dry'));
  const pmap={probewet1:['1','wet'],probedry1:['1','dry'],probewet2:['2','wet'],probedry2:['2','dry']};
  for(const id in pmap){const b=document.getElementById(id);
    if(b)b.addEventListener('click',()=>calibrateProbe(pmap[id][0],pmap[id][1]));}
  {const t1=document.getElementById('tc1');if(t1)t1.addEventListener('click',()=>checkTempComp('1'));
   const t2=document.getElementById('tc2');if(t2)t2.addEventListener('click',()=>checkTempComp('2'));}
  const hc=document.getElementById('chartgrid');
  if(hc){hc.addEventListener('mousemove',chartMove);
         hc.addEventListener('mouseleave',chartLeave);}
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
function esc(s){return (s||'').replace(/[<>&]/g,'');}
function moistColor(p){const h=35+(210-35)*(Math.max(0,Math.min(100,p))/100);
  return `hsla(${h.toFixed(0)},60%,50%,0.28)`;}
function gridEditable(){return canEdit && grid && !grid.locked;}
function drawGrid(){
  const svg=document.getElementById('gridsvg');
  if(!grid||!svg)return;
  svg.style.display=grid.show?'':'none';
  if(!grid.show){svg.innerHTML='';return;}
  const C=grid.corners,R=grid.rows,K=grid.cols,S=1000;
  let h='';
  for(let r=0;r<R;r++)for(let c=0;c<K;c++){
    const p=[bil(C,c/K,r/R),bil(C,(c+1)/K,r/R),bil(C,(c+1)/K,(r+1)/R),bil(C,c/K,(r+1)/R)];
    const pts=p.map(q=>(q[0]*S).toFixed(1)+','+(q[1]*S).toFixed(1)).join(' ');
    const k=cellKey(r,c);
    const mo=sensorData['moisture:'+k];
    const fill=mo?moistColor(mo.value):'rgba(127,176,105,0.12)';
    h+=`<polygon class="gc" data-k="${k}" points="${pts}" fill="${fill}" stroke="#eafff0" stroke-width="2"/>`;
    const ctr=bil(C,(c+0.5)/K,(r+0.5)/R);
    const cx=(ctr[0]*S).toFixed(1); let yy=ctr[1]*S-3;
    h+=`<text x="${cx}" y="${yy.toFixed(1)}" class="glbl" text-anchor="middle">${k}</text>`;
    const nm=grid.names[k];
    if(nm){yy+=22;h+=`<text x="${cx}" y="${yy.toFixed(1)}" class="gnm" text-anchor="middle">${esc(nm)}</text>`;}
    if(mo){yy+=21;h+=`<text x="${cx}" y="${yy.toFixed(1)}" class="gmoist" text-anchor="middle">${mo.value.toFixed(0)}%</text>`;}
    const gr=sensorData['growth:'+k];
    if(gr){yy+=21;h+=`<text x="${cx}" y="${yy.toFixed(1)}" class="ggrow" text-anchor="middle">\u{1F331} ${gr.value.toFixed(0)}%</text>`;}
    const dr=sensorData['dry:'+k];
    if(dr){const dm=camMoisture(k,dr.value);yy+=21;
      h+=`<text x="${cx}" y="${yy.toFixed(1)}" class="gdry" text-anchor="middle">\u{1F4A7} ${dm!=null?dm.toFixed(0)+'%':dr.value.toFixed(0)}</text>`;}
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
    const r=await fetch('/api/detect_grid',{method:'POST'});const j=await r.json();
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
  try{
    S={...j,now:new Date(j.now),on:new Date(j.on),off:new Date(j.off),
       sunrise:new Date(j.sunrise),sunset:new Date(j.sunset)};
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
    {const rc=document.getElementById('reportctl');if(rc)rc.style.display=canEdit?'':'none';}
    fetchReport();
    requestAnimationFrame(fitReportHeight);
    handleGrid(j);
    renderSensors(j);
    renderTrays(j);
    renderSweep(j);
    renderDayProgress(j);
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
  if(f.elements['units'])body.units=f.elements['units'].value;
  for(const k of ['alert_sustain_min','alert_cooldown_hours','alert_dry_pct','alert_humidity_high'])
    if(f.elements[k])body[k]=parseInt(f.elements[k].value||0,10);
  if(f.elements['alerts_enabled'])body.alerts_enabled=f.elements['alerts_enabled'].checked;
  if(f.elements['alert_soil_low_f']){
    const shown=parseFloat(f.elements['alert_soil_low_f'].value||0);
    body.alert_soil_low_f=shown?Math.round(tToF(shown)):0;
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
  msg.textContent='Planting...';msg.className='';
  try{
    const r=await fetch('/api/settings',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const j=await r.json();
    if(r.ok){msg.textContent='Saved \u{1F331}';msg.className='ok';setTimeout(refresh,800);}
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
  if(r.species&&r.species.length)
    h+=`<div class="rsec"><h4>Species guesses</h4><ul>${r.species.map(s=>`<li><b>${esc(s.cell||'')}</b> ${esc(s.guess||'unsure')}${s.confidence?` <span class="rconf">(${esc(s.confidence)})</span>`:''}${s.why?` &mdash; ${esc(s.why)}`:''}</li>`).join('')}</ul></div>`;
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
    const r=await fetch('/api/report',{method:'POST'});
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

setInterval(refresh,15000);
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
  // straight line from origin to peak: how linear the dimming actually is
  h+=`<line x1="${sx(0)}" y1="${sy(0)}" x2="${sx(100)}" y2="${sy(maxL)}"
        stroke="#c9c9c9" stroke-width="1" stroke-dasharray="4 3"/>`;
  const line=pts.map(p=>`${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join(' ');
  h+=`<polyline fill="none" stroke="#e8b04b" stroke-width="2" points="${line}"
        vector-effect="non-scaling-stroke"/>`;
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
    const half=pts.find(p=>p[1]>=maxL/2);
    info.textContent=`peak ${Math.round(maxL).toLocaleString()} lx`
      +(half?` \u00b7 50% output at ${half[0]}% set`:'');
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
    const r=await fetch('/api/light_sweep',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({step:5,settle:2})});
    const j=await r.json().catch(()=>({}));
    if(r.status===401){if(info)info.textContent='log in first';return;}
    if(!j.ok){if(info)info.textContent=j.error||'failed';return;}
    if(info)info.textContent=`measuring\u2026 (~${j.estimate_seconds}s)`;
    if(btn)btn.textContent='Cancel';
  }catch(e){if(info)info.textContent='request failed';}
}
let sweepRunning=false, lastCurve=null;
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
  // how long the light has actually delivered today
  const lit=document.getElementById('dplit');
  if(lit)lit.textContent=(day&&day.lit_minutes)?durStr(day.lit_minutes*60000)+' lit':'';

  // DLI against the seedling target band (6-12), scaled to 16 mol
  const d=(day&&day.dli!=null)?day.dli:(lightMetrics&&lightMetrics.dli);
  const dfill=document.getElementById('dlifill');
  const dval=document.getElementById('dlival');
  if(dfill)dfill.style.width=Math.max(0,Math.min(100,(d||0)/16*100)).toFixed(1)+'%';
  if(dval){
    if(d==null){dval.textContent='building today\u2019s total';}
    else{
      const band=d<6?'below target so far':(d<=12?'in target':'above target');
      dval.innerHTML=`<b>${d.toFixed(1)}</b> mol \u00b7 ${band}`;
    }
  }
  // peak intensity reached today
  const stats=document.getElementById('daystats');
  if(stats){
    let h2='';
    if(day&&day.peak_ppfd!=null)
      h2+=`<dt>Peak today</dt><dd>${Math.round(day.peak_ppfd)} <small>\u00b5mol</small></dd>`;
    if(day&&day.peak_lux!=null)
      h2+=`<dt>Peak light</dt><dd>${Math.round(day.peak_lux).toLocaleString()} <small>lx</small></dd>`;
    stats.innerHTML=h2;
    stats.style.display=h2?'':'none';
  }
}
function durStr(ms){
  const m=Math.max(0,Math.round(ms/60000));
  if(m<60)return m+' min';
  return Math.floor(m/60)+'h '+String(m%60).padStart(2,'0')+'m';
}
function renderSweep(j){
  const btn=document.getElementById('sweepbtn');
  const info=document.getElementById('sweepinfo');
  const sw=j.sweep||{};
  const was=sweepRunning; sweepRunning=!!sw.running;
  if(btn)btn.textContent=sweepRunning?'Cancel':'Measure';
  if(sweepRunning&&info)info.textContent=`measuring\u2026 ${sw.pct}%`;
  if(was&&!sweepRunning&&info&&sw.error)info.textContent=sw.error;
  lastCurve=j.light_curve||null;
  if(!dragging)                                   // a drag owns the marker
    drawLightCurve(lastCurve, sweepRunning,
                   sweepRunning?null:(j.brightness!=null?j.brightness:null));
}
function initLight(){
  {const b=document.getElementById('sweepbtn');
   if(b)b.addEventListener('click',startSweep);}
  document.querySelectorAll('.lcbtn').forEach(b=>
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
[initAuth, initSensors, initGridSvg, initReport, initLight, initTrays].forEach(fn=>{  try{ fn(); }catch(e){ console.error(fn.name+' init failed:', e); }
});
refresh();
