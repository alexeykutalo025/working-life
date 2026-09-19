// Building the page, and the rules about how numbers are allowed to appear.

// --- DOM ------------------------------------------------------------------

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function mount(node) {
  const screen = document.getElementById('screen');
  clear(screen);
  screen.append(node);
  return screen;
}

// --- formatting -----------------------------------------------------------

export function bytes(n) {
  if (n === null || n === undefined) return 'unknown';
  if (n === 0) return '0 bytes';
  const units = ['bytes', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  const decimals = i === 0 ? 0 : (v < 10 ? 1 : 0);
  return `${v.toFixed(decimals)} ${units[i]}`;
}

export function num(n) {
  if (n === null || n === undefined) return 'unknown';
  return Number(n).toLocaleString();
}

export function plural(n, one, many) {
  return `${num(n)} ${n === 1 ? one : (many || one + 's')}`;
}

// A date we do not have is shown as "no date", never as a blank and never as
// a made-up one.
export function date(iso, { withTime = false } = {}) {
  if (!iso) return 'no date';
  const d = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
  if (Number.isNaN(d.getTime())) return iso;
  const opts = withTime
    ? { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }
    : { year: 'numeric', month: 'short', day: 'numeric' };
  return d.toLocaleDateString(undefined, opts);
}

export function dateRange(from, to) {
  if (!from && !to) return 'no dates';
  if (from && to) return `${date(from)} to ${date(to)}`;
  return date(from || to);
}

export function duration(seconds) {
  if (seconds === null || seconds === undefined) return null;
  const s = Math.round(seconds);
  if (s < 60) return `${s} second${s === 1 ? '' : 's'}`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} minute${m === 1 ? '' : 's'}`;
  const h = Math.floor(m / 60);
  const rem = m % 60;
  return `${h} hour${h === 1 ? '' : 's'}${rem ? ` ${rem} minute${rem === 1 ? '' : 's'}` : ''}`;
}

// --- the honest-count rule (spec 9.5) -------------------------------------
//
// A count that an open finding affects is never shown bare. It gets a marker
// and the explanation in reach. There is deliberately no way to render a
// qualified number without its qualifier.

export function count(value, qualifiers) {
  // A qualifier arrives as {code, text, estimated_loss, ...} from the API, or
  // as a plain string from a caller that built one. Both have to end up as
  // words: stringifying the object gives "[object Object]", which is worse
  // than no qualifier at all because it looks like the program is broken
  // rather than like the number is.
  const list = (qualifiers || [])
    .map((q) => (typeof q === 'string' ? q : q && q.text))
    .filter(Boolean);

  if (!list.length) return el('span', { class: 'strong' }, num(value));

  const text = list.join(' · ');
  return el('span', {},
    el('span', { class: 'strong qualified', title: text }, num(value)),
    ' ',
    el('span', { class: 'qualified-note' }, text),
  );
}

export function honest(envelope, { compact = false } = {}) {
  // The server sends {value, qualified, qualifiers:[...]}; anything else is a
  // plain number and is shown plainly.
  if (envelope && typeof envelope === 'object' && 'value' in envelope) {
    if (compact) return compactCount(envelope);
    return count(envelope.value, envelope.qualifiers);
  }
  return el('span', { class: 'strong' }, num(envelope));
}

// A qualified number in a row of cards. Spelling the reason out on every card
// repeats one sentence four times and buries the numbers it is qualifying, so
// the marker is short and the reason is on the card's title attribute and one
// click away. The rule still holds: the number is never shown unmarked.
export function compactCount(envelope) {
  const reasons = (envelope.qualifiers || [])
    .map((q) => (typeof q === 'string' ? q : q && q.text))
    .filter(Boolean);

  if (!reasons.length) return el('span', {}, num(envelope.value));

  const summary = envelope.estimated_missing
    ? `about ${num(envelope.estimated_missing)} more could not be read`
    : 'not the whole story';

  return el('span', { title: reasons.join(' · ') },
    el('span', { class: 'qualified' }, num(envelope.value)),
    ' ',
    el('a', {
      class: 'qualified-note',
      href: '#/problems',
      style: 'font-size:var(--size-small)',
    }, summary),
  );
}

// --- tags -----------------------------------------------------------------

const SEVERITY_WORDS = {
  critical: 'Critical',
  high: 'High',
  medium: 'Medium',
  info: 'Information',
};

export function severityTag(severity) {
  const word = SEVERITY_WORDS[severity] || severity || 'Unknown';
  return el('span', { class: `tag tag--${severity || 'medium'}` }, word);
}

export function tag(text, variant = 'plain') {
  return el('span', { class: `tag tag--${variant}` }, text);
}

// --- messages -------------------------------------------------------------

export function notice(kind, title, ...body) {
  return el('div', { class: `notice notice--${kind}`, role: kind === 'error' ? 'alert' : 'status' },
    title ? el('div', { class: 'notice__title' }, title) : null,
    ...body.map((b) => (b instanceof Node ? b : el('p', {}, b))),
  );
}

export function errorNotice(err) {
  const detail = err && err.detail && typeof err.detail === 'object'
    ? JSON.stringify(err.detail, null, 2)
    : null;
  return notice('error', 'That did not work',
    el('p', {}, err?.message || String(err)),
    detail ? el('details', {},
      el('summary', {}, 'Technical detail, for when you need to send it to someone'),
      el('pre', { class: 'raw' }, detail),
    ) : null,
  );
}

export function empty(title, ...body) {
  return el('div', { class: 'empty' },
    el('div', { class: 'empty__title' }, title),
    ...body.map((b) => (b instanceof Node ? b : el('p', {}, b))),
  );
}

export function loading(what = 'Loading') {
  return el('p', { class: 'muted spinner-text' }, what);
}

// --- modal ----------------------------------------------------------------

const FOCUSABLE =
  'button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
  'textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])';

/** Dialogs currently open, innermost last. */
const openModals = [];

/**
 * A dialog box.
 *
 * Dialogs stack. The folder chooser opens one from inside another, and an
 * earlier version cleared the whole mount point on open, which silently threw
 * away the dialog underneath along with everything the user had ticked in it.
 *
 * The keydown listener is removed on every way out, not only on Escape. It
 * used to survive a dialog closed by its own button, so a session spent
 * acknowledging findings accumulated one listener per dialog, every one of
 * them still calling close() on a dialog that was no longer there.
 */
export function modal({ title, body, actions, onClose }) {
  const root = document.getElementById('modal-root');
  const returnFocusTo = document.activeElement;
  let closed = false;

  const close = () => {
    if (closed) return;
    closed = true;
    document.removeEventListener('keydown', onKey, true);
    backdrop.remove();
    const at = openModals.indexOf(entry);
    if (at !== -1) openModals.splice(at, 1);
    // Hand focus back to whatever opened this, so a keyboard user is not
    // dropped at the top of the page.
    if (returnFocusTo && document.contains(returnFocusTo)) returnFocusTo.focus();
    if (onClose) onClose();
  };

  const panel = el('div', { class: 'modal' },
    el('h2', { class: 'modal__title' }, title),
    el('div', { class: 'modal__body' }, body),
    el('div', { class: 'modal__actions' }, ...(actions || [])),
  );

  const backdrop = el('div', {
    class: 'modal-backdrop',
    role: 'dialog',
    'aria-modal': 'true',
    'aria-label': title,
    onclick: (e) => { if (e.target === backdrop) close(); },
  }, panel);

  const entry = { close, backdrop };

  const onKey = (e) => {
    // Only the topmost dialog responds, or Escape would close the whole stack.
    if (openModals[openModals.length - 1] !== entry) return;

    if (e.key === 'Escape') {
      e.stopPropagation();
      close();
      return;
    }
    if (e.key !== 'Tab') return;

    // Keep Tab inside the dialog; behind it the page is inert to the eye and
    // should be inert to the keyboard too.
    const stops = [...panel.querySelectorAll(FOCUSABLE)]
      .filter((node) => node.offsetParent !== null || node === document.activeElement);
    if (!stops.length) return;

    const first = stops[0];
    const last = stops[stops.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  };

  document.addEventListener('keydown', onKey, true);

  root.append(backdrop);
  openModals.push(entry);

  const focusable = panel.querySelector(FOCUSABLE);
  if (focusable) focusable.focus();
  return { close };
}

// --- progress -------------------------------------------------------------

export function progressBar(job) {
  const known = job.total !== null && job.total !== undefined && job.total > 0;
  const pct = known ? Math.min(100, Math.round((job.done / job.total) * 100)) : null;

  const parts = [];
  if (known) parts.push(`${num(job.done)} of ${num(job.total)} (${pct}%)`);
  else if (job.done) parts.push(`${num(job.done)} so far`);
  if (job.rate_per_sec) parts.push(`${num(Math.round(job.rate_per_sec))} per second`);
  if (job.elapsed_sec) parts.push(`${duration(job.elapsed_sec)} elapsed`);
  parts.push(
    job.remaining_sec !== null && job.remaining_sec !== undefined
      ? `about ${duration(job.remaining_sec)} left`
      : 'time remaining cannot be estimated yet',
  );

  return el('div', { class: 'progress' },
    el('div', { class: 'progress__bar' },
      el('div', {
        class: known ? 'progress__fill' : 'progress__fill progress__fill--indeterminate',
        style: known ? `width:${pct}%` : '',
        role: 'progressbar',
        'aria-valuenow': known ? String(pct) : null,
        'aria-valuemin': '0',
        'aria-valuemax': '100',
        'aria-label': job.message || 'Working',
      }),
    ),
    el('div', { class: 'progress__text' }, parts.join(' · ')),
    job.current ? el('div', { class: 'progress__current' }, job.current) : null,
  );
}

// --- misc -----------------------------------------------------------------

export function setTitle(text) {
  document.title = text ? `${text} - Recall` : 'Recall';
}

export function debounce(fn, ms = 250) {
  let handle;
  return (...args) => {
    clearTimeout(handle);
    handle = setTimeout(() => fn(...args), ms);
  };
}
