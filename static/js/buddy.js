// openSeedling dashboard: Little Buddy, the wandering seedling.
// One of several plain scripts loaded in order by index.html; they share one
// global scope (the split of the former app.js, 4 Oct). A function used at
// load time must be defined in this file or an earlier one.
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
