// openSeedling touchscreen summary: Little Buddy, the same characters and walk
// as the dashboard (static/js/buddy.js), crossing the summary's tiles. On when
// the dashboard's Little Buddy is on and Settings, External display allows it
// on the screen; never while the dark night screen is up. screen.js announces
// each status with a "screenstatus" event.
import { set_buddyOn, set_buddyPick, set_walkBoxes, startWalker } from './js/buddy.js';

set_walkBoxes('.tile');
set_buddyOn(false);
function follow(status) {
  const cfg = (status && status.settings) || {};
  const night = !document.getElementById('night').hidden;
  set_buddyOn(!!status && cfg.little_buddy !== false && cfg.screen_buddy !== false && !night);
  set_buddyPick(cfg.buddy_model || 'sprout');
}
document.addEventListener('screenstatus', ev => follow(ev.detail));
follow(window.screenStatus); // a status that arrived before this module ran
startWalker();
