// openSeedling dashboard: The planting map and the planting history.
// An ES module: what it uses from the others is imported at the top, and what
// it offers is exported. Another module cannot assign one of its variables
// directly; it calls the set_<name>() exported at the bottom. Code that runs
// at page load is in start(), which main.js calls in a fixed order.
// ---- planting map: two trays, editable seed / equipment / sow date ----
import { curSetup, setupsList, trayInSetup } from './setups.js';
import { canEdit } from './photos.js';
import { clearCellUI } from './devices.js';
import { colL, refresh } from './grid.js';

export let trays={}, trayDirty={}, trayTimer=null, trayPending=0;
export function daysSince(iso){
  if(!iso)return null;
  const d=new Date(iso+'T00:00:00'), now=new Date();
  if(isNaN(d))return null;
  return Math.floor((new Date(now.getFullYear(),now.getMonth(),now.getDate())-d)/86400000);
}
export function renderTrays(j){
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
    // a cell counts when it holds something, not merely when it has a record
    // (a cleared cell keeps an empty one: "20 of 20" with A4 empty)
    const filled=Object.values(cells).filter(v=>v&&(v.seed||v.equipment||v.planted||v.sprouted||v.archived)).length;
    h+=`<div class="tray"><div class="tray-head">`
      +`<b>${esc(tr.label||('Tray '+id))}</b>`
      +`<span class="tsum">${filled} of ${rows*cols} cells filled</span>`
      +`</div>`
      // the grid scrolls sideways inside its card on a phone instead of
      // widening the whole page (5 columns of cells need ~600 px)
      +`<div class="tscroll"><div class="tgrid" style="--tcols:${cols}">`;
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
    h+='</div></div></div>';               // .tgrid, .tscroll, .tray
  }
  wrap.innerHTML=h;
  {const filled=Object.values(trays).reduce((n,t)=>n+Object.keys(t.cells||{}).length,0);
   const hint=document.getElementById('trayhint');
   if(hint)hint.style.display=filled?'none':'';}
}
// Single definition on purpose: a second `function esc` later in the file
// would silently win for the whole scope and (if weaker) let a quote in a
// seed name break out of an HTML attribute.
export function esc(s){
  return String(s==null?'':s).replace(/&/g,'&amp;').replace(/"/g,'&quot;')
    .replace(/'/g,'&#39;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
export function collectTray(id){
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
export async function saveTray(id){
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
export async function trayLayout(body){
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
export function renderTrayConfig(cfg){
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
export function initTrayConfig(){
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
export function initTrays(){
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
export let plantingHistory=[];
export async function loadPlantings(){
  try{
    const r=await fetch('/api/plantings');
    const j=await r.json();
    plantingHistory=j.plantings||[];
  }catch(e){plantingHistory=[];}
  renderPlantings();
}
export function renderPlantings(){
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
export function applyAuthTo(el){
  el.querySelectorAll('.editonly').forEach(e=>{e.style.display=canEdit?'':'none';});
}
