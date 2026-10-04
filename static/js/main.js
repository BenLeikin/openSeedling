// openSeedling dashboard entry point (loaded by index.html as a module).
// Each module declares its functions and state and exports them; what used to
// run at load time is in each module's start(), called here in the order the
// plain scripts used to load, after every module has been evaluated.
import * as m_light from './light.js';
import * as m_setups from './setups.js';
import * as m_photos from './photos.js';
import * as m_charts from './charts.js';
import * as m_trays from './trays.js';
import * as m_devices from './devices.js';
import * as m_grid from './grid.js';
import * as m_live from './live.js';
import * as m_buddy from './buddy.js';
import * as m_cards from './cards.js';

for (const m of [
  m_light,
  m_setups,
  m_photos,
  m_charts,
  m_trays,
  m_devices,
  m_grid,
  m_live,
  m_buddy,
  m_cards
]) {
  if (typeof m.start === 'function') m.start();
}
