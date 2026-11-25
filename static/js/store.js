// openSeedling dashboard: which status to show, and every request to the controller.
//
// A status arrives by the live stream or by a poll, and a slow poll can land
// after a newer push, or after the reply to a change made on this page. Each
// status carries the controller's revision; one older than what is already
// shown is dropped. While a change is on its way to the controller, statuses
// wait (for a few seconds at most: a photo takes half a minute, and the page
// should not stand still for it), and the reply names the revision that
// includes the change, so nothing older than the change is shown after it.

let floor = null; // the revision already shown, or the one a change's reply named
let writes = 0; // changes sent and not yet answered
let parked = null; // the newest status that arrived meanwhile
let show = () => {};

// "<boot>.<n>": boot tells one run of the controller from the next
function parse(rev) {
  const [boot, n] = String(rev || '')
    .split('.')
    .map(Number);
  return Number.isFinite(boot) && Number.isFinite(n) ? { boot, n } : null;
}
function older(a, b) {
  return !!a && !!b && a.boot === b.boot && a.n < b.n;
}

// fn(status) is called for each status that should be shown
export function onStatus(fn) {
  show = fn;
}

// Hand in a status from the stream or a poll.
export function offer(status) {
  if (writes > 0) {
    if (!parked || !older(parse(status.rev), parse(parked.rev))) parked = status;
    return;
  }
  const rev = parse(status.rev);
  if (older(rev, floor)) return;
  if (rev) floor = rev;
  show(status);
}

const PARK_MS = 3000; // how long statuses wait for a change's reply
const GIVE_UP_MS = 120000; // a request that has not answered by now never will

// fetch(), for everything sent to the controller.
export async function request(url, options = {}) {
  const changing = !!options.method && options.method !== 'GET';
  let waiting = changing;
  const settle = () => {
    if (!waiting) return;
    waiting = false;
    if (--writes === 0 && parked) {
      const status = parked;
      parked = null;
      offer(status);
    }
  };
  if (changing) {
    writes++;
    setTimeout(settle, PARK_MS);
  }
  try {
    const resp = await fetch(url, { signal: AbortSignal.timeout(GIVE_UP_MS), ...options });
    if (changing) {
      const rev = parse(resp.headers.get('X-Status-Rev'));
      if (rev && !older(rev, floor)) floor = rev;
    }
    return resp;
  } finally {
    settle();
  }
}
