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
      class: 'qualified-note qualified-note__link',
      href: '#/problems',
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
  // `panel` is handed back so a caller can widen it: the folder chooser is a
  // two-pane layout and does not fit the default reading width.
  return { close, panel };
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

// --- form fields ----------------------------------------------------------
//
// A label above a control, with optional help below it. Four screens each had
// their own version of this, all slightly different, and a fifth wrote the
// markup by hand.

let fieldCounter = 0;

export function field(label, control, help) {
  if (!control.id) {
    fieldCounter += 1;
    control.id = `field-${fieldCounter}`;
  }
  return el('div', { class: 'field' },
    el('label', { for: control.id }, label),
    control,
    help ? el('div', { class: 'field__help' }, help) : null,
  );
}

/** A labelled dropdown, the commonest field by far. */
export function selectField(label, options, { value = '', onChange, help } = {}) {
  const select = el('select', {
    onchange: (e) => { if (onChange) onChange(e.target.value); },
  });
  for (const option of options) {
    select.append(el('option', {
      value: option.value,
      selected: String(option.value) === String(value),
    }, option.label));
  }
  return field(label, select, help);
}

// --- a number worth looking at --------------------------------------------

/**
 * One figure with its label. `alarming` is for a number that genuinely needs
 * attention - using the loud style for "all of them" teaches the user to
 * ignore the colour that matters.
 */
export function stat(label, value, note, { alarming = false, lead = false } = {}) {
  return el('div', { class: 'stat' },
    el('div', {
      class: `stat__value${lead ? ' stat__value--lead' : ''}${alarming ? ' qualified' : ''}`,
    }, value),
    el('div', { class: 'stat__label' }, label),
    note ? el('span', { class: alarming ? 'stat__qualifier' : 'check__note' }, note) : null,
  );
}

// --- words for things -----------------------------------------------------
//
// These were written out in four places between them, and drifted: one screen
// faked the singular by stripping an "s" off the plural.

const KIND_WORDS = {
  message: ['message', 'messages'],
  event: ['calendar entry', 'calendar entries'],
  contact: ['contact', 'contacts'],
  task: ['task', 'tasks'],
  note: ['note', 'notes'],
};

export function kindLabel(kind, { one = false } = {}) {
  const words = KIND_WORDS[kind];
  if (!words) return kind || 'record';
  const word = one ? words[0] : words[1];
  return word.charAt(0).toUpperCase() + word.slice(1);
}

const MONTHS = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
];

export function monthName(m) { return MONTHS[(m || 1) - 1]; }

// --- the dialog every screen needed ---------------------------------------

/** "That did not work", with one Close button. Was written three times. */
export function errorDialog(err) {
  const dialog = modal({
    title: 'That did not work',
    body: el('div', {}, el('p', {}, (err && err.message) || String(err))),
    actions: [
      el('button', {
        class: 'btn btn--primary', type: 'button', onclick: () => dialog.close(),
      }, 'Close'),
    ],
  });
  return dialog;
}


// --- paging ---------------------------------------------------------------
//
// One pager for every long list. Numbered pages as well as Previous and Next,
// because "page 7 of 13" tells somebody where they are in a way that two
// arrows never do, and because jumping back to the start of a 300-name list
// should not mean pressing Previous twelve times.

/** How many rows a page holds. The middle one is the default everywhere. */
export const PAGE_SIZES = [25, 50, 100];

/** Fewer, for screens whose rows are whole cards rather than table lines. */
export const CARD_PAGE_SIZES = [10, 25, 50];

/**
 * Previous / Next, numbered pages, and a count.
 *
 * `onGo(offset)` is called with the new offset; `onPageSize(size)` is
 * optional and adds the "per page" chooser.
 */
export function pager({ total, offset, pageSize, onGo, onPageSize,
                        unit = 'result', units = null, sizes = PAGE_SIZES }) {
  if (!total) return null;

  const pages = Math.max(1, Math.ceil(total / pageSize));
  const current = Math.floor(offset / pageSize) + 1;
  const from = total ? offset + 1 : 0;
  const to = Math.min(total, offset + pageSize);

  const goToPage = (page) => onGo((Math.min(Math.max(1, page), pages) - 1) * pageSize);

  const nav = el('nav', { class: 'pager', 'aria-label': 'Pages' });

  // "persons" is not a word anybody says, so the plural can be given outright.
  const word = total === 1 ? unit : (units || `${unit}s`);
  nav.append(el('p', { class: 'pager__count mb-0' },
    `Showing ${num(from)} to ${num(to)} of ${num(total)} ${word}`));

  if (pages > 1) {
    const buttons = el('div', { class: 'pager__pages' });

    buttons.append(el('button', {
      class: 'btn', type: 'button', disabled: current === 1,
      onclick: () => goToPage(current - 1),
    }, '← Previous'));

    for (const page of pageNumbers(current, pages)) {
      if (page === null) {
        buttons.append(el('span', { class: 'pager__gap', 'aria-hidden': 'true' }, '…'));
        continue;
      }
      buttons.append(el('button', {
        class: `btn pager__page${page === current ? ' btn--primary' : ''}`,
        type: 'button',
        'aria-label': `Page ${page} of ${pages}`,
        'aria-current': page === current ? 'page' : null,
        onclick: () => goToPage(page),
      }, String(page)));
    }

    buttons.append(el('button', {
      class: 'btn', type: 'button', disabled: current === pages,
      onclick: () => goToPage(current + 1),
    }, 'Next →'));

    nav.append(buttons);
  }

  if (onPageSize) {
    const select = el('select', {
      id: 'pager-size',
      onchange: (e) => onPageSize(Number(e.target.value)),
    });
    for (const size of sizes) {
      select.append(el('option', {
        value: String(size), selected: size === pageSize,
      }, `${size} at a time`));
    }
    const wrap = el('div', { class: 'pager__size' },
      el('label', { for: 'pager-size' }, 'Show'),
      select);
    nav.append(wrap);
  }

  return nav;
}

/**
 * Which page numbers to show: the two ends, and a run around where you are.
 *
 * `null` marks a gap. Nine pages fit; three hundred do not, and a row of three
 * hundred buttons is no more use than none.
 *
 * The run is a fixed width that slides rather than a window that shrinks at
 * the edges. On page 1 of 9 a shrinking window gives "1 2 … 9", which offers
 * no way at all to reach page 5; a sliding run gives "1 2 3 4 5 … 9".
 */
export function pageNumbers(current, pages, span = 5) {
  if (pages <= span + 2) {
    return Array.from({ length: pages }, (_, i) => i + 1);
  }

  let first = Math.max(1, current - Math.floor(span / 2));
  let last = first + span - 1;
  if (last > pages) {
    last = pages;
    first = Math.max(1, last - span + 1);
  }

  const wanted = new Set([1, pages]);
  for (let p = first; p <= last; p += 1) wanted.add(p);

  const out = [];
  let previous = 0;
  for (const page of [...wanted].sort((a, b) => a - b)) {
    // An ellipsis standing in for one page is worse than the page itself: it
    // costs the same room and hides a place you might want to go.
    if (page - previous === 2) out.push(previous + 1);
    else if (page - previous > 2) out.push(null);
    out.push(page);
    previous = page;
  }
  return out;
}


// --- tabs -----------------------------------------------------------------
//
// One screen holding two separate things. Built to the ARIA tabs pattern
// rather than as a row of buttons, because a tablist tells a screen reader
// "these are alternative views of one screen" and a row of buttons does not.
//
// The keyboard behaviour is the part that is easy to leave out and the part
// that makes it a real tablist: Left/Right move between tabs, Home and End
// jump to the ends, and only the selected tab is in the Tab order, so tabbing
// past the strip lands in the panel rather than walking through every tab.

let tabSetCounter = 0;

/**
 * A tab strip and its panels.
 *
 * `items` is [{ id, label, count, panel }]. `panel` is the element to show.
 * `onSelect(id)` is called whenever the selection changes, so a screen can put
 * the choice in the URL.
 *
 * Returns { element, select(id), selected }.
 */
export function tabs({ items, selected, onSelect, label = 'Sections' }) {
  const live = items.filter(Boolean);
  if (!live.length) return { element: el('div', {}), select: () => {}, selected: null };

  tabSetCounter += 1;
  const setId = `tabs-${tabSetCounter}`;
  let current = live.some((t) => t.id === selected) ? selected : live[0].id;

  const strip = el('div', { class: 'tabs__strip', role: 'tablist', 'aria-label': label });
  const panels = el('div', { class: 'tabs__panels' });
  const buttons = new Map();

  const show = (id, { focus = false } = {}) => {
    current = id;
    for (const item of live) {
      const on = item.id === current;
      const button = buttons.get(item.id);
      button.classList.toggle('is-selected', on);
      button.setAttribute('aria-selected', on ? 'true' : 'false');
      // Only the selected tab is tabbable; the arrows move between them.
      button.tabIndex = on ? 0 : -1;
      item.panel.hidden = !on;
    }
    if (focus) buttons.get(current).focus();
    if (onSelect) onSelect(current);
  };

  const step = (from, delta) => {
    const at = live.findIndex((t) => t.id === from);
    const next = (at + delta + live.length) % live.length;
    show(live[next].id, { focus: true });
  };

  for (const item of live) {
    // A panel may already have an id the screen looks itself up by; taking it
    // over would break every getElementById pointed at it.
    const panelId = item.panel.id || `${setId}-panel-${item.id}`;
    const tabId = `${setId}-tab-${item.id}`;

    const button = el('button', {
      class: 'tabs__tab',
      type: 'button',
      role: 'tab',
      id: tabId,
      'aria-controls': panelId,
      onclick: () => show(item.id),
      onkeydown: (e) => {
        if (e.key === 'ArrowRight') { e.preventDefault(); step(item.id, 1); }
        else if (e.key === 'ArrowLeft') { e.preventDefault(); step(item.id, -1); }
        else if (e.key === 'Home') { e.preventDefault(); show(live[0].id, { focus: true }); }
        else if (e.key === 'End') {
          e.preventDefault();
          show(live[live.length - 1].id, { focus: true });
        }
      },
    },
      el('span', {}, item.label),
      item.count === null || item.count === undefined
        ? null
        : el('span', { class: 'tabs__count' }, num(item.count)),
    );

    buttons.set(item.id, button);
    strip.append(button);

    item.panel.id = panelId;
    item.panel.setAttribute('role', 'tabpanel');
    item.panel.setAttribute('aria-labelledby', tabId);
    item.panel.tabIndex = 0;
    panels.append(item.panel);
  }

  show(current);

  return {
    element: el('div', { class: 'tabs' }, strip, panels),
    select: (id) => show(id),
    get selected() { return current; },
  };
}
