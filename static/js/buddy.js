// openSeedling dashboard: Little Buddy, the wandering seedling.
// An ES module: what it uses from the others is imported at the top, and what
// it offers is exported. Another module cannot assign one of its variables
// directly; it calls the set_<name>() exported at the bottom. Code that runs
// at page load is in start(), which main.js calls in a fixed order.
// ---- the wandering seedling ----
// Every half minute a small seedling crosses one card, entering and leaving
// behind its edge. It sits at z-index:-1 inside the card, so it walks BEHIND
// the chips and buttons rather than over them.

export let buddyOn = true; // mirrors the "little buddy" setting
export let buddyPick = 'sprout'; // 'sprout' | 'pepper' | 'cat' | 'random'
export function buddyModel() {
  if (buddyPick !== 'random') return buddyPick in BUDDY_SPRITES ? buddyPick : 'sprout';
  const keys = Object.keys(BUDDY_SPRITES);
  return keys[Math.floor(Math.random() * keys.length)];
}
export const WALK_EVERY_MS = 30000;
// walk in, stop and wave, walk out. The pause fractions must match the
// walk-across keyframes in the stylesheet (36% and 64%).
export const WALK_DUR_MS = 11000,
  PAUSE_START = 0.36,
  PAUSE_END = 0.64;
export let walkTimer = null;

// Three characters, picked per outing. Each returns the SVG for one walker;
// they share the walk cycle, so a new one is a sprite function plus a case
// here, nothing more. The parts that animate carry fixed class names:
// .legs/.leg-a/.leg-b step, .body bobs, .arm waves during the pause.
export const BUDDY_MODELS = {
  sprout: 'Potted sprout',
  pepper: 'Chile pepper',
  cat: 'Avey',
  snail: 'Snail',
  ladybug: 'Ladybug',
  drop: 'Raindrop',
  bee: 'Bee',
  gnome: 'Garden gnome'
};

export function buddySprout() {
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

export function buddyPepper() {
  // a chile pod: broad shoulders under a calyx cap, tapering to a curved point
  return `<g class="legs">
      <line class="leg-a" x1="15" y1="29" x2="11.5" y2="35" style="transform-origin:15px 29px"/>
      <line class="leg-b" x1="15" y1="29" x2="18.5" y2="35" style="transform-origin:15px 29px"/>
    </g>
    <g class="body" style="transform-origin:15px 29px">
      <path class="pod" d="M10.2 15.4C10.2 13.6 12.2 12.6 15 12.6S19.9 13.6 19.9 15.6C19.9 21.2 18.9 26.6 16.6 30.4C15.9 31.6 14.9 31.8 14.3 30.8C11.7 26.8 10.2 21 10.2 15.4Z"/>
      <path class="pglint" d="M12.1 16.4C12.1 15 12.9 14.4 13.9 14.2"/>
      <path class="calyx" d="M10.4 15.2C10.9 13.1 12.6 12 15 12S19.1 13.1 19.6 15.2C18.7 14.3 17.7 14.5 16.9 14.1C16.3 14.9 13.7 14.9 13.1 14.1C12.3 14.5 11.3 14.3 10.4 15.2Z"/>
      <path class="pstem" d="M15 12.4C15 10.8 15.6 9.6 17 8.9"/>
      <circle class="pcheek" cx="11.6" cy="20.4" r="1.1"/>
      <circle class="pcheek" cx="18.4" cy="20.4" r="1.1"/>
      <circle class="peye" cx="13" cy="18.6" r="0.95"/>
      <circle class="peye" cx="17" cy="18.6" r="0.95"/>
      <path class="pmouth" d="M13.4 20.8q1.6 1.4 3.2 0"/>
      <line class="arm parm" x1="19.8" y1="18.8" x2="24.4" y2="15.2" style="transform-origin:19.8px 18.8px"/>
    </g>`;
}

export function buddyCat() {
  // Avey, in profile facing left: a rounded body on four legs (the far pair a
  // shade darker, behind), the head over the chest, the tail up in an S.
  // The tail takes the place of the wave during the pause.
  return `<g class="legs">
      <line class="leg-b cleg cleg-far" x1="11.4" y1="23" x2="11.4" y2="29.2" style="transform-origin:11.4px 23px"/>
      <line class="leg-a cleg cleg-far" x1="21.8" y1="23" x2="21.8" y2="29.2" style="transform-origin:21.8px 23px"/>
      <line class="leg-a cleg" x1="8.6" y1="23" x2="8.6" y2="29.6" style="transform-origin:8.6px 23px"/>
      <line class="leg-b cleg" x1="19" y1="23" x2="19" y2="29.6" style="transform-origin:19px 23px"/>
    </g>
    <g class="body" style="transform-origin:15px 25px">
      <path class="arm tail" d="M22.4 20.2C25.6 19.6 26.2 16.8 24.6 15C23.8 14.1 24.2 13 25.2 12.8" style="transform-origin:22.4px 20.2px"/>
      <rect class="fur" x="6.4" y="16.6" width="17" height="8.4" rx="4.2"/>
      <path class="fur" d="M4.6 10l-0.9-4.6 4.1 2.4z"/>
      <path class="fur" d="M13.4 10l0.9-4.6-4.1 2.4z"/>
      <circle class="fur" cx="9" cy="13.4" r="5.4"/>
      <path class="inner-ear" d="M5 9.3l-0.4-2.3 2 1.2z"/>
      <path class="inner-ear" d="M13 9.3l0.4-2.3-2 1.2z"/>
      <circle class="cat-cheek" cx="5.9" cy="15.2" r="0.9"/>
      <circle class="cat-cheek" cx="12.1" cy="15.2" r="0.9"/>
      <circle class="eye" cx="7" cy="12.9" r="1"/>
      <circle class="eye" cx="11" cy="12.9" r="1"/>
      <circle class="eye-shine" cx="7.35" cy="12.55" r="0.32"/>
      <circle class="eye-shine" cx="11.35" cy="12.55" r="0.32"/>
      <path class="nose" d="M8.4 14.6h1.2l-0.6 0.7z"/>
      <path class="mouth cat-mouth" d="M8.1 15.7q0.45 0.6 0.9 0q0.45 0.6 0.9 0"/>
      <path class="whisker" d="M4.4 14.6L1.4 14"/>
      <path class="whisker" d="M4.4 15.6L1.6 16.2"/>
      <path class="whisker" d="M13.6 14.6L16.6 14"/>
      <path class="whisker" d="M13.6 15.6L16.4 16.2"/>
    </g>`;
}

export function buddySnail() {
  // the slow one: BUDDY_DURATION gives it its own, slower crossing. In
  // profile facing left: a long foot from head to tail, the shell on its back
  // with the spiral inside it, eye stalks up from the head and two short
  // feelers below them.
  return `<g class="legs">
      <line class="leg-a stalk" x1="6.6" y1="23.2" x2="4.6" y2="17.4" style="transform-origin:6.6px 23.2px"/>
      <line class="leg-b stalk" x1="9" y1="23.2" x2="9.8" y2="17.4" style="transform-origin:9px 23.2px"/>
    </g>
    <g class="body" style="transform-origin:15px 27px">
      <path class="snail-body" d="M3.2 29.2C3.2 25.2 4.8 22.4 7.8 22.4C10.4 22.4 11.8 24.4 12.2 26.6L25.4 27.4C27.6 27.6 27.8 29.8 25.6 30.1L5.6 30.4C4 30.4 3.2 30 3.2 29.2Z"/>
      <line class="feeler" x1="4.4" y1="25.2" x2="2.4" y2="24.2"/>
      <circle class="shell" cx="17.8" cy="20.6" r="7.2"/>
      <path class="shell-line" d="M17.80 19.90 L17.91 19.85 L18.04 19.81 L18.19 19.80 L18.34 19.82 L18.49 19.86 L18.64 19.93 L18.79 20.04 L18.92 20.17 L19.03 20.33 L19.12 20.51 L19.18 20.71 L19.21 20.93 L19.20 21.16 L19.16 21.40 L19.07 21.63 L18.94 21.85 L18.78 22.06 L18.57 22.25 L18.34 22.40 L18.07 22.53 L17.78 22.61 L17.47 22.64 L17.15 22.63 L16.82 22.56 L16.51 22.45 L16.20 22.28 L15.92 22.06 L15.67 21.79 L15.46 21.48 L15.29 21.13 L15.18 20.76 L15.12 20.36 L15.13 19.94 L15.20 19.53 L15.33 19.12 L15.53 18.73 L15.80 18.36 L16.12 18.04 L16.50 17.76 L16.92 17.53 L17.38 17.38 L17.87 17.29 L18.37 17.27 L18.89 17.34 L19.39 17.48 L19.87 17.70 L20.33 18.00 L20.74 18.37 L21.09 18.81 L21.38 19.30 L21.60 19.83 L21.73 20.41 L21.78 21.00 L21.73 21.61 L21.59 22.21 L21.36 22.79 L21.04 23.34 L20.64 23.84 L20.16 24.28 L19.60 24.65 L18.99 24.93 L18.34 25.13 L17.65 25.22 L16.95 25.21 L16.26 25.09 L15.57 24.86 L14.92 24.53 L14.33 24.10 L13.79 23.58 L13.34 22.97 L12.97 22.30 L12.71 21.57 L12.56 20.80 L12.53 20.01 L12.62 19.21 L12.83 18.43 L13.15 17.67 L13.60 16.97 L14.15 16.34 L14.79 15.78 L15.52 15.33 L16.32 14.99 L17.17 14.77 L18.05 14.68 L18.94 14.72 L19.83 14.90 L20.69 15.21 L21.50 15.66 L22.24 16.22 L22.90 16.90" fill="none"/>
      <circle class="stalk-eye" cx="4.6" cy="17.2" r="1.25"/>
      <circle class="stalk-eye" cx="9.8" cy="17.2" r="1.25"/>
      <circle class="eye-shine" cx="4.95" cy="16.85" r="0.38"/>
      <circle class="eye-shine" cx="10.15" cy="16.85" r="0.38"/>
      <path class="mouth" d="M4.8 27q1.4 1.1 2.8 0"/>
      <line class="arm stalk-wave" x1="9" y1="23.2" x2="9.8" y2="17.4" style="transform-origin:9px 23.2px"/>
    </g>`;
}

export function buddyLadybug() {
  // A seven-spot ladybug, from above. In flight it lifts its two shell halves
  // (the elytra) up and out and the thin wings folded underneath do the
  // flapping, so the shell is two halves hinged behind the pronotum (the black
  // shield with two white patches), with the body and wings beneath them.
  // Three black spots on each half and one shared at the front.
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
        <path class="spot" d="M15 14.6a1.5 1.5 0 0 0 0 3z"/>
        <circle class="spot" cx="10.6" cy="18.8" r="1.45"/>
        <circle class="spot" cx="9.4" cy="23.2" r="1.2"/>
        <circle class="spot" cx="12.6" cy="25.4" r="1.1"/>
        <path class="shell-edge" d="M15 14.4v13.2"/>
      </g>
      <g class="elytron elytron-r" style="transform-origin:15px 15.5px">
        <path class="shell-red" d="M15 14a8 7 0 0 1 0 14z"/>
        <path class="spot" d="M15 14.6a1.5 1.5 0 0 1 0 3z"/>
        <circle class="spot" cx="19.4" cy="18.8" r="1.45"/>
        <circle class="spot" cx="20.6" cy="23.2" r="1.2"/>
        <circle class="spot" cx="17.4" cy="25.4" r="1.1"/>
        <path class="shell-edge" d="M15 14.4v13.2"/>
      </g>
      <path class="pronotum" d="M10.2 15.4C10.6 12.8 12.6 11.6 15 11.6S19.4 12.8 19.8 15.4C18.2 14.6 16.6 14.3 15 14.3S11.8 14.6 10.2 15.4Z"/>
      <ellipse class="pron-white" cx="11.9" cy="13.9" rx="1.05" ry="0.75"/>
      <ellipse class="pron-white" cx="18.1" cy="13.9" rx="1.05" ry="0.75"/>
      <circle class="head-dark" cx="15" cy="10.6" r="3.3"/>
      <circle class="eye-white" cx="13.8" cy="10.3" r="0.85"/>
      <circle class="eye-white" cx="16.2" cy="10.3" r="0.85"/>
      <path class="antenna" d="M13.4 8l-1.3-2"/>
      <path class="antenna" d="M16.6 8l1.3-2"/>
      <circle class="head-dark" cx="12" cy="5.8" r=".65"/>
      <circle class="head-dark" cx="18" cy="5.8" r=".65"/>
    </g>`;
}

export function buddyDrop() {
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

export function buddyBee() {
  // a flyer: no legs, two wings that flap about their own roots, mirrored.
  // The stripes are bands of the body itself (clipped to its outline), the
  // face sits in the top yellow band, and a small stinger points down.
  return `<g class="body">
      <ellipse class="wing wing-l" cx="9.5" cy="13.6" rx="5.5" ry="3.6" style="transform-origin:13.5px 15.5px"/>
      <ellipse class="wing wing-r" cx="20.5" cy="13.6" rx="5.5" ry="3.6" style="transform-origin:16.5px 15.5px"/>
      <path class="stinger" d="M14.1 27.6L15 29.6L15.9 27.6Z"/>
      <ellipse class="bee-body" cx="15" cy="21" rx="7.4" ry="7"/>
      <path class="band" d="M7.75 20.2H22.25A7.4 7 0 0 1 22.4 21.2V22.2H7.6V21.2A7.4 7 0 0 1 7.75 20.2Z"/>
      <path class="band" d="M8.55 24.6H21.45A7.4 7 0 0 1 20.7 25.9L20.2 26.4H9.8L9.3 25.9A7.4 7 0 0 1 8.55 24.6Z"/>
      <circle class="beye" cx="12.7" cy="17" r="1.1"/>
      <circle class="beye" cx="17.3" cy="17" r="1.1"/>
      <circle class="eye-shine" cx="13.05" cy="16.65" r="0.35"/>
      <circle class="eye-shine" cx="17.65" cy="16.65" r="0.35"/>
      <path class="bmouth" d="M13.6 18.5q1.4 1.1 2.8 0"/>
      <path class="antenna dark" d="M13 14.6l-1.4-3.4"/>
      <path class="antenna dark" d="M17 14.6l1.4-3.4"/>
    </g>`;
}

export function buddyGnome() {
  // a garden gnome: tall pointed red hat with a bent tip, a full white beard
  // over the mouth, a round nose, green coat and brown boots
  return `<g class="legs">
      <line class="leg-a boot" x1="12.5" y1="28" x2="10.5" y2="33.5" style="transform-origin:12.5px 28px"/>
      <line class="leg-b boot" x1="17.5" y1="28" x2="19.5" y2="33.5" style="transform-origin:17.5px 28px"/>
    </g>
    <g class="body" style="transform-origin:15px 28px">
      <path class="coat" d="M9.4 29c0-5.4 2-8.6 5.6-8.6s5.6 3.2 5.6 8.6z"/>
      <circle class="face" cx="15" cy="16.6" r="4.2"/>
      <circle class="gcheek" cx="11.7" cy="17.6" r="1.05"/>
      <circle class="gcheek" cx="18.3" cy="17.6" r="1.05"/>
      <path class="beard" d="M10.6 17.8C10.6 23 12.6 26.4 15 26.4S19.4 23 19.4 17.8C18.3 19 16.8 19.4 15 19.4S11.7 19 10.6 17.8Z"/>
      <circle class="eye" cx="13.4" cy="16" r=".9"/>
      <circle class="eye" cx="16.6" cy="16" r=".9"/>
      <circle class="gnose" cx="15" cy="17.9" r="1.2"/>
      <path class="hat" d="M9.8 14.6C10.6 9.8 13.6 6 18.4 4.2C17.2 7.6 18.6 11 20.2 14.6Z"/>
      <ellipse class="hat-brim" cx="15" cy="14.5" rx="5.6" ry="1.05"/>
      <line class="arm coat-arm" x1="19.3" y1="24.2" x2="23.6" y2="20.6" style="transform-origin:19.3px 24.2px"/>
    </g>`;
}

export const BUDDY_SPRITES = {
  sprout: buddySprout,
  pepper: buddyPepper,
  cat: buddyCat,
  snail: buddySnail,
  ladybug: buddyLadybug,
  drop: buddyDrop,
  bee: buddyBee,
  gnome: buddyGnome
};
// the snail walks at its own pace; everything else shares the standard cycle
export const BUDDY_DURATION = { snail: 20000 };
// Sprites drawn in profile have a natural facing. The walk flips them with
// scaleX so they always face the way they are travelling; a sprite drawn
// facing LEFT needs the opposite sign from one drawn facing right, or it
// moonwalks in one direction. Front-facing sprites are symmetric enough that
// either sign looks correct.
export const BUDDY_FACES_LEFT = { cat: true, snail: true };
// Characters that would naturally fly cruise through the card instead of
// walking along its floor, and at the pause they loop the loop instead of
// waving. The raindrop falls rather than flies, so it keeps walking.
export const BUDDY_FLIES = { bee: true, ladybug: true };
// headroom a loop needs above the flyer: two radii plus its own height
export const LOOP_HEADROOM = 76;


export function walkerSvg(model) {
  const draw = BUDDY_SPRITES[model] || buddySprout;
  const name = model in BUDDY_SPRITES ? model : 'sprout';
  // flyers get an inner group so the hover and the loop can move the whole
  // sprite without fighting the travel transform on the svg itself
  // two layers so the bob and the loop never fight over one transform: the
  // outer one bobs the whole time, the inner one does the loop inside it
  const inner = BUDDY_FLIES[name] ? `<g class="bob"><g class="flyer">${draw()}</g></g>` : draw();
  return `<svg class="walker buddy-${name}${BUDDY_FLIES[name] ? ' flying' : ''}" viewBox="0 0 30 36" aria-hidden="true" focusable="false">${inner}</svg>`;
}

export function walkOnce() {
  // never interrupt: one seedling at a time, and none while the tab is hidden
  if (!buddyOn || document.hidden || document.querySelector('.walkwrap')) return;
  const cards = [...document.querySelectorAll('.card')].filter(c => {
    if (c.offsetParent === null) return false; // hidden card
    const r = c.getBoundingClientRect();
    return r.width > 200 && r.height > 90; // room to walk
  });
  if (!cards.length) return;
  const card = cards[Math.floor(Math.random() * cards.length)];
  const wrap = document.createElement('div');
  wrap.className = 'walkwrap';
  const model = buddyModel();
  wrap.innerHTML = walkerSvg(model);
  const dur = BUDDY_DURATION[model] || WALK_DUR_MS;
  card.appendChild(wrap);
  const w = card.clientWidth,
    rtl = Math.random() < 0.5;
  const walker = wrap.querySelector('.walker');
  // start and end fully outside the clip, so it emerges from behind the edge
  walker.style.setProperty('--walk-from', (rtl ? w + 40 : -40) + 'px');
  walker.style.setProperty('--walk-to', (rtl ? -40 : w + 40) + 'px');
  // stop somewhere in the middle third, not dead centre every time
  const mid = Math.round(w * (0.34 + Math.random() * 0.32)) - 15;
  walker.style.setProperty('--walk-mid', (rtl ? mid : mid) + 'px');
  // rtl means travelling right-to-left, so the character must face left
  const facesLeft = !!BUDDY_FACES_LEFT[model];
  walker.style.setProperty('--walk-dir', facesLeft ? (rtl ? 1 : -1) : rtl ? -1 : 1);
  walker.style.setProperty('--walk-dur', dur + 'ms');
  if (BUDDY_FLIES[model]) {
    // cruise somewhere in the upper-middle of the card, low enough that the
    // loop still fits under the top edge on a short card
    const h = card.clientHeight;
    const want = Math.round(h * (0.35 + Math.random() * 0.25));
    walker.style.bottom = Math.max(10, Math.min(want, h - LOOP_HEADROOM)) + 'px';
  }
  // legs stop and the arm waves only while it is standing still
  const pauseAt = setTimeout(() => walker.classList.add('pausing'), dur * PAUSE_START);
  const resumeAt = setTimeout(() => walker.classList.remove('pausing'), dur * PAUSE_END);
  setTimeout(() => {
    clearTimeout(pauseAt);
    clearTimeout(resumeAt);
    wrap.remove();
  }, dur + 400);
}

export function startWalker() {
  if (walkTimer) return;
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return; // no ambient motion for anyone who opted out
  walkTimer = setInterval(walkOnce, WALK_EVERY_MS);
}

// setters: other modules cannot assign an imported binding
export function set_buddyOn(v) {
  buddyOn = v;
  return v;
}
export function set_buddyPick(v) {
  buddyPick = v;
  return v;
}
