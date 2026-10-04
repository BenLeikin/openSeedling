// openSeedling dashboard: Grow setups (tabs, light view, the Setups editor) and the USB camera's modes.
// One of several plain scripts loaded in order by index.html; they share one
// global scope (the split of the former app.js, 4 Oct). A function used at
// load time must be defined in this file or an earlier one.
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
  // the heat mat's power level shows on the heat mat's setup (all tabs if none)
  if(k.startsWith('heat:')){const hs=setupsList.find(s=>s.heat);return !hs||hs.id===cs.id;}
  if(k.startsWith('sys:'))return true;                 // the Pi's own memory, every tab
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
  const resSet=setupsList.find(s=>s.reservoir);
  document.body.classList.toggle('reselsewhere', !!(multi&&resSet&&cs&&resSet.id!==cs.id));
  const camElsewhere=!!(multi&&camSet&&cs&&camSet.id!==cs.id);
  document.body.classList.toggle('camelsewhere', camElsewhere);
  if(camElsewhere)document.body.classList.add('nocam');
  document.body.classList.toggle('fanelsewhere', !!(multi&&fanSet&&cs&&fanSet.id!==cs.id));
  const heatSet=setupsList.find(s=>s.heat);
  document.body.classList.toggle('heatelsewhere', !!(multi&&heatSet&&cs&&heatSet.id!==cs.id));
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
  // the Light response card shows and calibrates this fixture
  const c2=j.light2_cal||{};
  for(const k of ['light_curve','light_linear','light_linear_on','light_linear_stale','light_curve_effective'])
    j[k]=c2[k];
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
        reservoir:!!s.reservoir,heat:!!s.heat,
        dli_low:s.band[0],dli_high:s.band[1]}))));
  drawSetupConfig();
}
function drawSetupConfig(){
  const box=document.getElementById('setupcfg');
  if(!box||!setupDraft)return;
  const all=Object.keys(sensorData||{}).filter(k=>!k.startsWith('growth')&&!k.startsWith('dry:')
    &&!k.startsWith('moisture:')).sort();
  const luxKeys=[...new Set(['lux','lux:2',...all.filter(k=>/^lux(:|$)/.test(k))])];
  // only sensors that need placing: a tray's probe, float and canopy come with
  // the tray, the light sensor is picked above, the reservoir follows its box
  const keys=all.filter(k=>!/^(probe|canopy|float|reservoir):|^lux(:|$)/.test(k));
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
        <label><input type="checkbox" data-flag="reservoir"${s.reservoir?' checked':''}> Reservoir</label>
        <label><input type="checkbox" data-flag="heat"${s.heat?' checked':''}> Heat mat</label>
        <span class="fhint">one setup each; the fan follows this setup's light</span></div>
      <div class="setupsens"><b>Trays</b>${trayOpts.map(([id,lbl])=>`<label><input type="checkbox" data-t="${esc(id)}"`
        +`${(s.trays||[]).includes(id)?' checked':''}> ${esc(lbl)}</label>`).join('')}
        <span class="fhint">none ticked = all trays</span></div>
      <div class="setupsens"><b>Sensors</b>${keys.map(k=>`<label><input type="checkbox" data-k="${esc(k)}"`
        +`${(s.sensors||[]).includes(k)?' checked':''}> ${esc(sensorMeta(k,0).label)}</label>`).join('')}
        <span class="fhint">tick a sensor in each setup it serves (one air sensor
          between two close areas can count for both); a tray's probe, float and
          canopy come with the tray</span></div>
      ${setupDraft.length>1?'<button type="button" class="setuprm">Remove</button>':''}
    </fieldset>`).join('');
}
{
  const box=document.getElementById('setupcfg');
  if(box){
    box.addEventListener('input',ev=>{
      const fs=ev.target.closest('fieldset[data-i]');if(!fs||!setupDraft)return;
      const s=setupDraft[+fs.dataset.i];setupDirty=true;
      // either Save button sends it now; say so where both can be seen
      {const sm=document.getElementById('setupmsg');if(sm)sm.textContent='Unsaved changes';
       const m=document.getElementById('msg');if(m&&!/Planting/.test(m.textContent)){m.textContent='Unsaved changes';m.className='';}}
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
      formDirty.add('usb_width');formDirty.add('usb_height');   // set by code: no input event
    });
  }
}
