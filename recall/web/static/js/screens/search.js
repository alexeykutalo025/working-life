// Search - one large box, filters down the side, results with snippets.
//
// Keyboard, from the spec: `/` focuses the box, up and down move through the
// results, Enter opens one. A 72-year-old is not obliged to use them, but
// somebody reading a decade of mail will, and they cost nothing to provide.

import { api } from '../api.js';
import {
  add, clear, date, debounce, el, empty, errorNotice, kindLabel, loading,
  mount, num, pager, plural, setTitle, tag,
} from '../ui.js';

// --- what the user chose last time ----------------------------------------
//
// Kept in this browser, never uploaded - there is no network at runtime. Every
// read is wrapped, because a private window can throw on the first touch of
// localStorage and a search screen that will not open over a remembered
// column width would be an absurd way to lose the archive.

const VIEW_KEY = 'recall.searchView';
const HIDDEN_KEY = 'recall.searchColumns';
const WIDTH_KEY = 'recall.searchColumnWidths';

const VIEWS = ['table', 'cards'];

//: Columns switched off until somebody asks for them: an id the row already
//: carries, a mail header nobody reads, and a flag that repeats the column
//: beside it. Hidden columns are what gets stored, not shown ones, so a column
//: added to the export later turns up on screen instead of staying invisible.
const HIDDEN_BY_DEFAULT = ['item_id', 'message_id', 'timezone_known'];

//: A spreadsheet measures a column in characters and a screen in pixels. The
//: server sends the width the workbook uses; this turns it into one.
const PX_PER_CHAR = 7.4;
const MIN_COLUMN_PX = 72;
const MAX_SEED_PX = 440;

function storedView() {
  try {
    const stored = localStorage.getItem(VIEW_KEY);
    return VIEWS.includes(stored) ? stored : 'table';
  } catch {
    return 'table';
  }
}

function storedObject(key) {
  try {
    const parsed = JSON.parse(localStorage.getItem(key) || '{}');
    // Storage holds whatever was put there, including what an older version
    // wrote and whatever a person types into a console.
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

function remember(key, value) {
  try {
    localStorage.setItem(key, typeof value === 'string' ? value : JSON.stringify(value));
  } catch { /* private window, or storage full */ }
}

const state = {
  q: '',
  kind: '',
  person_id: '',
  source_id: '',
  folder_id: '',
  tag: '',
  has_attachments: '',
  undated: false,
  date_from: '',
  date_to: '',
  sort: 'relevance',
  offset: 0,
  pageSize: 50,
  selected: -1,
  results: [],
  //: The table is what this archive is for - columns that sort and export -
  //: so it opens first. Cards read better one at a time. Either way, whichever
  //: was last used is what comes back.
  view: storedView(),
  //: Which kind's table is showing, when the results hold more than one.
  table_kind: '',
  //: Columns the user turned off, keyed by kind. See HIDDEN_BY_DEFAULT.
  hidden: storedObject(HIDDEN_KEY),
  //: Column widths in pixels, keyed by kind, where one has been dragged.
  widths: storedObject(WIDTH_KEY),
};

/** The last table the server sent, so hiding a column costs no request. */
let tableData = null;

/** Whether the column panel is open, kept across redraws of the table. */
let columnsOpen = false;

/** The filter lists the server last sent, so a chip can name what it holds. */
let filterData = null;

let keyHandler = null;

// --- what a filter is -----------------------------------------------------
//
// One description of each filter, used by both the sidebar and the chips above
// the results. Two lists would drift, and the pair that drifted would be the
// control that sets a filter and the chip that claims to clear it.

const FILTERS = [
  {
    key: 'kind',
    label: 'Kind',
    describe: (v) => kindLabel(v),
  },
  {
    key: 'person_id',
    label: 'Person',
    describe: (v) => lookup('people', v, (p) => p.name || '(no name)'),
  },
  {
    key: 'source_id',
    label: 'From file',
    describe: (v) => lookup('sources', v, (s) => fileName(s.path)),
  },
  {
    key: 'folder_id',
    label: 'Folder',
    describe: (v) => lookup('folders', v, (f) => f.path),
  },
  {
    key: 'tag',
    label: 'Category',
    describe: (v) => v,
  },
  {
    key: 'date_from',
    label: 'From',
    describe: (v) => v,
  },
  {
    key: 'date_to',
    label: 'Up to',
    describe: (v) => v,
  },
  {
    key: 'has_attachments',
    label: 'Only',
    describe: () => 'records with attachments',
  },
  {
    key: 'undated',
    label: 'Only',
    describe: () => 'records with no date',
  },
];

/** Name the thing an id refers to, falling back to the id if it has gone. */
function lookup(listName, value, describe) {
  const list = (filterData && filterData[listName]) || [];
  const row = list.find((r) => String(r.id) === String(value));
  return row ? describe(row) : `#${value}`;
}

/** Every filter currently narrowing the results. */
function activeFilters() {
  return FILTERS.filter((f) => {
    const v = state[f.key];
    return v !== '' && v !== false && v !== null && v !== undefined;
  });
}

function clearFilter(key) {
  state[key] = key === 'undated' ? false : '';
  state.offset = 0;
  loadFilters();
  runSearch();
}

function clearAllFilters() {
  for (const f of FILTERS) state[f.key] = f.key === 'undated' ? false : '';
  state.offset = 0;
  loadFilters();
  runSearch();
}

export async function render({ params }) {
  setTitle('Search');

  state.q = params.get('q') || '';
  state.date_from = params.get('from') || '';
  state.date_to = params.get('to') || '';
  state.person_id = params.get('person') || '';
  state.kind = params.get('kind') || '';
  state.undated = params.get('undated') === '1';
  state.offset = 0;
  state.selected = -1;

  const root = el('div', {},
    el('h1', { class: 'page__title' }, 'Search'),
    el('div', { class: 'card' },
      el('label', { for: 'search-box' },
        'Search everything in the archive'),
      el('input', {
        type: 'search',
        id: 'search-box',
        value: state.q,
        placeholder: 'a name, a word, a company — anything you remember',
        class: 'input--lead',
        oninput: debounce((e) => {
          state.q = e.target.value;
          state.offset = 0;
          runSearch();
        }, 350),
        onkeydown: (e) => {
          if (e.key === 'Enter') { state.offset = 0; runSearch(); }
        },
      }),
      el('p', { class: 'field__help' },
        'Put words in "quotation marks" to find that exact phrase. ' +
        'Use AND, OR and NOT in capitals to combine words. ' +
        'Press the / key at any time to come back to this box.'),
      el('div', { id: 'search-understood' }),
    ),
    el('div', { class: 'with-sidebar' },
      el('aside', { id: 'search-filters' }, loading('Loading filters')),
      el('div', { id: 'search-results' }),
    ),
  );
  mount(root);

  installKeys();
  await loadFilters();
  await runSearch();

  const box = document.getElementById('search-box');
  if (box && !state.q) box.focus();

  return () => {
    if (keyHandler) document.removeEventListener('keydown', keyHandler);
    keyHandler = null;
  };
}

// --- keyboard -------------------------------------------------------------

function installKeys() {
  if (keyHandler) document.removeEventListener('keydown', keyHandler);

  keyHandler = (e) => {
    const box = document.getElementById('search-box');
    const typing = document.activeElement
      && ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName);

    if (e.key === '/' && !typing) {
      e.preventDefault();
      if (box) { box.focus(); box.select(); }
      return;
    }
    if (!state.results.length) return;

    if (e.key === 'ArrowDown' && (!typing || document.activeElement === box)) {
      e.preventDefault();
      move(1);
    } else if (e.key === 'ArrowUp' && (!typing || document.activeElement === box)) {
      e.preventDefault();
      move(-1);
    } else if (e.key === 'Enter' && state.selected >= 0 && document.activeElement !== box) {
      e.preventDefault();
      const result = state.results[state.selected];
      if (result) window.location.hash = `#/item/${result.id}`;
    }
  };
  document.addEventListener('keydown', keyHandler);
}

function move(delta) {
  // There is no .search-result in table view, so the arrow keys used to do
  // nothing at all there - silently, which is the worst way for a key to fail.
  const nodes = document.querySelectorAll(
    state.view === 'table' ? '#search-table tbody tr' : '.search-result');
  if (!nodes.length) return;

  const next = Math.max(0, Math.min(nodes.length - 1, state.selected + delta));
  state.selected = next;
  nodes.forEach((node, i) => {
    node.classList.toggle('is-selected', i === next);
    if (i === next) {
      node.scrollIntoView({ block: 'nearest' });
      node.focus({ preventScroll: true });
    }
  });
}

// --- filters --------------------------------------------------------------

async function loadFilters() {
  const host = document.getElementById('search-filters');
  if (!host) return;

  let data;
  try {
    data = await api.searchFilters();
  } catch (err) {
    clear(host);
    host.append(errorNotice(err));
    return;
  }

  filterData = data;
  clear(host);

  const panel = el('div', { class: 'card filters' });
  const active = activeFilters();

  panel.append(el('div', { class: 'filters__head' },
    el('h2', { class: 'card__title mt-0 mb-0' }, 'Narrow it down'),
    el('div', { id: 'filters-clear' }),
  ));

  panel.append(group('When', ['date_from', 'date_to', 'undated'],
    el('div', { class: 'field' },
      el('label', { for: 'filter-from' }, 'From this date'),
      dateInput('filter-from', 'date_from', '2003, or 2003-04'),
      el('label', { for: 'filter-to', class: 'mt-3' }, 'Up to this date'),
      dateInput('filter-to', 'date_to', '2009, or 2009-12'),
      data.span && data.span.first
        ? el('div', { class: 'field__help' },
            `The archive runs from ${String(data.span.first).slice(0, 4)} to `
            + `${String(data.span.last).slice(0, 4)}.`)
        : null,
    ),
    data.undated
      ? checkFilter('undated', `Only records with no date (${num(data.undated)})`,
          'These are in no date range at all, because they have no date.')
      : null,
  ));

  panel.append(group('What', ['kind', 'has_attachments'],
    select('Kind of record', 'kind', [
      { value: '', label: 'Everything' },
      ...data.kinds.map((k) => ({
        value: k.id, label: `${kindLabel(k.id)} (${num(k.count)})`,
      })),
    ]),
    checkFilter('has_attachments', 'Only records with attachments', null, 'true'),
  ));

  if (data.people.length) {
    panel.append(group('Who', ['person_id'],
      longSelect('Person', 'person_id', 'Anyone', data.people,
        (p) => `${p.name || '(no name)'} (${num(p.count)})`),
    ));
  }

  const whereKeys = ['source_id', 'folder_id', 'tag'];
  if (data.sources.length || data.folders.length || data.tags.length) {
    panel.append(group('Where it came from', whereKeys,
      data.sources.length
        ? longSelect('File it came from', 'source_id', 'Any file', data.sources,
            (s) => `${fileName(s.path)} (${num(s.count)})`)
        : null,
      data.folders.length
        ? longSelect('Folder', 'folder_id', 'Any folder', data.folders,
            (f) => `${f.path} (${num(f.count)})`)
        : null,
      data.tags.length
        ? select('Category', 'tag', [
            { value: '', label: 'Any category' },
            ...data.tags.map((t) => ({ value: t.name, label: `${t.name} (${num(t.count)})` })),
          ])
        : null,
    ));
  }

  panel.append(select('Order by', 'sort', [
    { value: 'relevance', label: 'Best match first' },
    { value: 'newest', label: 'Newest first' },
    { value: 'oldest', label: 'Oldest first' },
  ]));

  host.append(panel);
  syncFilterChrome();
}

/**
 * One section of the sidebar.
 *
 * Open when it holds an active filter, so a narrowed search never hides the
 * thing doing the narrowing behind a closed heading.
 */
function group(title, keys, ...children) {
  const live = keys.filter((k) => state[k] !== '' && state[k] !== false).length;
  return el('details', {
    class: 'filters__group',
    open: live > 0 || undefined,
    dataset: { keys: keys.join(',') },
  },
    el('summary', {}, title, el('span', { class: 'filters__badge' })),
    ...children,
  );
}

/**
 * Bring the sidebar's counts up to date without rebuilding it.
 *
 * Typing in the date box must not rebuild the sidebar - that would destroy the
 * input mid-keystroke and throw the cursor away - but the "Clear all" and the
 * badges still have to tell the truth. So only the chrome is redrawn, never
 * the controls themselves.
 */
function syncFilterChrome() {
  const active = activeFilters();

  const clearHost = document.getElementById('filters-clear');
  if (clearHost) {
    clear(clearHost);
    // Only offered when there is something to clear. A permanently-lit
    // "Clear all filters" trains the eye to ignore it.
    if (active.length) {
      clearHost.append(el('button', {
        class: 'btn btn--quiet', type: 'button', onclick: clearAllFilters,
      }, `Clear all ${active.length}`));
    }
  }

  document.querySelectorAll('.filters__group').forEach((node) => {
    const keys = (node.dataset.keys || '').split(',').filter(Boolean);
    const live = keys.filter((k) => state[k] !== '' && state[k] !== false).length;
    const badge = node.querySelector('.filters__badge');
    if (badge) badge.textContent = live ? String(live) : '';
  });
}

function select(label, key, options) {
  const id = `filter-${key}`;
  const isSet = state[key] !== '' && state[key] !== false;
  return el('div', { class: `field${isSet ? ' field--set' : ''}` },
    el('label', { for: id }, label),
    el('select', {
      id,
      onchange: (e) => { state[key] = e.target.value; state.offset = 0; loadFilters(); runSearch(); },
    }, ...options.map((o) => el('option', {
      value: o.value,
      selected: String(state[key]) === String(o.value),
    }, o.label))),
  );
}

/**
 * A dropdown with a box to narrow it first.
 *
 * The folder list can hold two hundred entries and the people list a hundred.
 * Scrolling a native dropdown that long to find one name is miserable, so the
 * list is filtered as you type and the dropdown only ever holds what matched.
 */
function longSelect(label, key, anyLabel, rows, describe) {
  const id = `filter-${key}`;
  const isSet = state[key] !== '' && state[key] !== false;

  const options = (list) => [
    el('option', { value: '', selected: !isSet }, anyLabel),
    ...list.map((row) => el('option', {
      value: row.id,
      selected: String(state[key]) === String(row.id),
    }, describe(row))),
  ];

  const dropdown = el('select', {
    id,
    onchange: (e) => { state[key] = e.target.value; state.offset = 0; loadFilters(); runSearch(); },
  }, ...options(rows));

  const count = el('div', { class: 'field__help' },
    `${plural(rows.length, 'choice')}`);

  const narrow = el('input', {
    type: 'search',
    class: 'filters__narrow',
    'aria-label': `Narrow the ${label.toLowerCase()} list`,
    placeholder: 'type to narrow this list',
    oninput: debounce((e) => {
      const needle = e.target.value.trim().toLowerCase();
      const matched = needle
        ? rows.filter((row) => describe(row).toLowerCase().includes(needle))
        : rows;
      clear(dropdown);
      for (const option of options(matched)) dropdown.append(option);
      clear(count);
      count.append(needle
        ? `${plural(matched.length, 'match', 'matches')} of ${num(rows.length)}`
        : plural(rows.length, 'choice'));
    }, 200),
  });

  return el('div', { class: `field${isSet ? ' field--set' : ''}` },
    el('label', { for: id }, label),
    rows.length > 12 ? narrow : null,
    dropdown,
    rows.length > 12 ? count : null,
  );
}

/** A checkbox that is itself a filter, bound to state both ways. */
function checkFilter(key, label, note, onValue = true) {
  return el('label', { class: 'check' },
    el('input', {
      type: 'checkbox',
      checked: state[key] === onValue || state[key] === true,
      onchange: (e) => {
        state[key] = e.target.checked ? onValue : (onValue === true ? false : '');
        state.offset = 0;
        loadFilters();
        runSearch();
      },
    }),
    el('span', {}, label,
      note ? el('span', { class: 'check__note' }, note) : null),
  );
}

/**
 * A date box that says when it cannot read what was typed.
 *
 * The server treats an unreadable date as no filter at all, so without this
 * the only sign of a typo is a result count that quietly does not change.
 */
function dateInput(id, key, placeholder) {
  const help = el('div', { class: 'field__error' });

  const check = (value) => {
    clear(help);
    if (value && !/^\d{4}(-\d{2}(-\d{2})?)?$/.test(value)) {
      help.append(`"${value}" is not a date Recall can read. `
        + 'Use a year, a year and month, or a full date.');
      return false;
    }
    return true;
  };

  const box = el('input', {
    type: 'text', id, value: state[key], placeholder,
    oninput: debounce((e) => {
      const value = e.target.value.trim();
      if (!check(value)) return;
      state[key] = value;
      state.offset = 0;
      runSearch();
    }, 400),
  });

  check(state[key]);
  return el('div', {}, box, help);
}

function fileName(path) {
  return String(path).split(/[\\/]/).pop();
}

// --- results --------------------------------------------------------------

async function runSearch() {
  const host = document.getElementById('search-results');
  if (!host) return;

  clear(host);
  host.append(loading('Searching'));

  let data;
  try {
    data = await api.search({
      q: state.q,
      kind: state.kind,
      person_id: state.person_id,
      source_id: state.source_id,
      folder_id: state.folder_id,
      tag: state.tag,
      has_attachments: state.has_attachments,
      undated: state.undated ? '1' : '',
      date_from: state.date_from,
      date_to: state.date_to,
      sort: state.sort,
      // The table fetches its own rows from /api/search/table. This request is
      // still needed for the count, the qualifiers and what the search box was
      // understood to mean - none of which depend on the page size - so it is
      // asked for one result rather than fifty snippets nobody will see.
      limit: state.view === 'table' ? 1 : state.pageSize,
      offset: state.view === 'table' ? 0 : state.offset,
    });
  } catch (err) {
    clear(host);
    host.append(errorNotice(err));
    return;
  }

  state.results = data.results;
  state.selected = -1;

  const understood = document.getElementById('search-understood');
  clear(understood);
  if (data.fell_back) {
    understood.append(el('p', { class: 'qualified-note' }, data.understood));
  }

  clear(host);

  // The honest-count rule: a total that something affects is never bare.
  const total = data.total;
  const header = el('div', { class: 'row mb-3' },
    el('span', { class: 'stat__value stat__value--inline' }, num(total.value)),
    el('span', { class: 'stat__label' },
      total.value === 1 ? 'record found' : 'records found'),
  );

  if (total.qualified) {
    const notes = (total.qualifiers || []).map((q) => q.text);
    if (total.index_note) notes.push(total.index_note);
    header.append(el('span', { class: 'qualified-note' }, notes.join(' · ')));
    header.append(el('a', { class: 'health__link', href: '#/problems' }, 'Why?'));
  }
  host.append(header);
  // chipRow is null when nothing is narrowing the results, which is most of
  // the time - and append printed that null above every unfiltered search.
  add(host, chipRow());
  syncFilterChrome();

  if (!data.results.length) {
    const active = activeFilters();
    host.append(empty(
      state.q ? 'Nothing matches that' : 'Nothing matches those filters',
      state.q
        ? 'Try fewer words, or a different spelling. Recall searches the ' +
          'subject, the message text, everyone on it, and the text inside ' +
          'attachments.'
        : 'Type something in the box above, or widen the filters.',
      // When filters are doing the excluding, say so and offer the way out,
      // rather than leaving somebody to wonder why their archive looks empty.
      active.length
        ? el('div', { class: 'btn-row' },
            el('button', {
              class: 'btn btn--primary', type: 'button', onclick: clearAllFilters,
            }, `Clear ${active.length === 1 ? 'the filter' : `all ${active.length} filters`}`))
        : null,
    ));
    return;
  }

  add(host, viewSwitch(), exportBar(total.value));

  if (state.view === 'table') {
    const table = el('div', { id: 'search-table' }, loading('Building the table'));
    host.append(table);
    drawTable(table);
    return;
  }

  const list = el('div', { class: 'stack' });
  data.results.forEach((result, index) => list.append(resultCard(result, index)));
  add(host, list, resultsPager(total.value));
}

/** The shared pager, wired to the search's own offset. */
function resultsPager(total) {
  return pager({
    total,
    offset: state.offset,
    pageSize: state.pageSize,
    unit: 'record',
    onGo: (offset) => {
      state.offset = offset;
      runSearch();
      const top = document.getElementById('search-results');
      if (top) top.scrollIntoView({ block: 'start', behavior: 'smooth' });
    },
    onPageSize: (size) => {
      state.pageSize = size;
      state.offset = 0;
      runSearch();
    },
  });
}

/**
 * The filters currently applied, as chips over the results.
 *
 * The sidebar says what you *can* narrow by; this says what you *have*
 * narrowed by, where the eye already is - and lets each one go individually,
 * which "Clear all filters" at the bottom of a sidebar never did.
 */
function chipRow() {
  const active = activeFilters();
  if (!active.length) return null;

  const row = el('div', { class: 'chips' },
    el('span', { class: 'chips__label' }, 'Narrowed to'));

  for (const filter of active) {
    const value = filter.describe(state[filter.key]);
    row.append(el('button', {
      class: 'chip', type: 'button',
      'aria-label': `Stop narrowing by ${filter.label.toLowerCase()} ${value}`,
      title: 'Remove this filter',
      onclick: () => clearFilter(filter.key),
    },
      el('span', { class: 'chip__label' }, `${filter.label}: `),
      el('span', { class: 'chip__value' }, String(value)),
      el('span', { class: 'chip__x', 'aria-hidden': 'true' }, '×'),
    ));
  }

  if (active.length > 1) {
    row.append(el('button', {
      class: 'btn btn--quiet', type: 'button', onclick: clearAllFilters,
    }, 'Clear them all'));
  }

  return row;
}

// --- the same results as a table ------------------------------------------
//
// Built from the export columns, so what is on the screen is what is in the
// spreadsheet. A client checking one against the other should never find a
// difference - that is the point of having both.

function viewSwitch() {
  const button = (view, label, hint) => el('button', {
    class: `btn ${state.view === view ? 'btn--primary' : ''}`,
    type: 'button',
    'aria-pressed': state.view === view ? 'true' : 'false',
    title: hint,
    onclick: () => {
      if (state.view === view) return;
      state.view = view;
      state.offset = 0;
      state.selected = -1;
      remember(VIEW_KEY, view);
      runSearch();
    },
  }, label);

  return el('div', { class: 'row mb-3' },
    el('span', { class: 'muted' }, 'Show as'),
    el('div', { class: 'btn-row' },
      button('table', 'Table', 'Every column, as it appears in the spreadsheet'),
      button('cards', 'Readable list', 'One result at a time, with the matching words marked'),
    ),
  );
}

async function drawTable(host) {
  try {
    tableData = await api.searchTable({
      q: state.q,
      kind: state.kind || state.table_kind,
      person_id: state.person_id,
      source_id: state.source_id,
      folder_id: state.folder_id,
      tag: state.tag,
      has_attachments: state.has_attachments,
      undated: state.undated ? '1' : '',
      date_from: state.date_from,
      date_to: state.date_to,
      limit: state.pageSize,
      offset: state.offset,
    });
  } catch (err) {
    clear(host);
    host.append(errorNotice(err));
    return;
  }
  renderTable(host);
}

/**
 * Draw the table from what the server last sent.
 *
 * Separate from fetching it, because turning a column on or off changes
 * nothing about which records match - asking the server again for the same
 * rows would make a tick box feel like a page load.
 */
function renderTable(host) {
  const data = tableData;
  if (!data) return;

  clear(host);

  if (!data.rows.length) {
    host.append(empty('Nothing to put in a table',
      'These results have no rows that can be laid out in columns yet.'));
    return;
  }

  // More than one kind of record needs more than one table: a calendar entry
  // and a contact share almost no columns.
  if (data.kinds.length > 1) {
    const row = el('div', { class: 'row mb-3' },
      el('span', { class: 'muted' }, 'Which records'));
    for (const k of data.kinds) {
      row.append(el('button', {
        class: `btn ${k.kind === data.showing ? 'btn--primary' : ''}`,
        type: 'button',
        'aria-pressed': k.kind === data.showing ? 'true' : 'false',
        onclick: () => {
          state.table_kind = k.kind;
          state.offset = 0;
          runSearch();
        },
      }, `${k.label} (${num(k.count)})`));
    }
    host.append(row);
  }

  if (data.total.qualified) {
    const notes = (data.total.qualifiers || []).map((q) => q.text);
    host.append(el('p', { class: 'qualified-note' },
      `${notes.join(' · ')} `,
      el('a', { class: 'health__link', href: '#/problems' }, 'Why?')));
  }

  const kind = data.showing || '';
  const shown = data.columns.filter((c) => !hiddenColumns(kind).has(c));

  add(host, columnPanel(host, data, kind, shown));

  const head = el('tr', {});
  for (const column of shown) {
    const th = el('th', {}, data.headings[column] || column);
    th.style.width = `${columnWidth(kind, column, data.widths[column])}px`;
    th.append(widthGrip(th, kind, column, data));
    head.append(th);
  }

  const body = el('tbody', {});
  data.rows.forEach((row) => {
    const tr = el('tr', {
      class: 'is-clickable',
      tabindex: '0',
      onclick: () => { window.location.hash = `#/item/${row.item_id}`; },
      onkeydown: (e) => {
        if (e.key === 'Enter') window.location.hash = `#/item/${row.item_id}`;
      },
    });
    for (const column of shown) {
      const text = cellText(row[column]);
      // Fixed column widths mean a long value is clipped. The full text is on
      // the record itself, one click away, and here on hover in the meantime.
      tr.append(el('td', { title: text.length > 32 ? text : null }, text));
    }
    body.append(tr);
  });

  // The arrow keys walk these rows; Enter opens whichever one is on.
  state.results = data.rows.map((r) => ({ id: r.item_id }));
  state.selected = -1;

  add(host,
    el('div', { class: 'table-wrap' },
      el('table', { class: 'table--columns' }, el('thead', {}, head), body)),
    el('p', { class: 'muted mt-3' },
      'These are the same columns you get in the spreadsheet. Click a row to ' +
      'open the record, or drag the edge of a heading to change its width.'),
    resultsPager(data.total.value),
  );
}

// --- which columns, and how wide ------------------------------------------

/** The columns this kind currently has switched off. */
function hiddenColumns(kind) {
  const stored = state.hidden[kind];
  return new Set(Array.isArray(stored) ? stored : HIDDEN_BY_DEFAULT);
}

function setHiddenColumns(kind, columns) {
  state.hidden[kind] = [...columns];
  remember(HIDDEN_KEY, state.hidden);
}

/** A column's width in pixels: what was dragged, else what the sheet uses. */
function columnWidth(kind, column, sheetWidth) {
  const stored = (state.widths[kind] || {})[column];
  if (Number.isFinite(stored) && stored >= MIN_COLUMN_PX) return Math.round(stored);
  return seedWidth(sheetWidth);
}

function seedWidth(sheetWidth) {
  const px = (Number(sheetWidth) || 16) * PX_PER_CHAR + 16;
  return Math.round(Math.min(MAX_SEED_PX, Math.max(MIN_COLUMN_PX, px)));
}

function setColumnWidth(kind, column, px) {
  if (!state.widths[kind]) state.widths[kind] = {};
  state.widths[kind][column] = px;
  remember(WIDTH_KEY, state.widths);
}

/**
 * The control for showing and hiding columns.
 *
 * A calendar entry has twenty-two columns and several of them are long free
 * text, so the table is unreadable until somebody can put the ones they do not
 * want away. Nothing is hidden that is not listed here with its box unticked,
 * and "Show every column" brings the lot back.
 */
function columnPanel(host, data, kind, shown) {
  const hidden = hiddenColumns(kind);

  const panel = el('details', { class: 'columns-panel', open: columnsOpen || null },
    el('summary', {},
      `Columns — showing ${num(shown.length)} of ${num(data.columns.length)}`));
  panel.addEventListener('toggle', () => { columnsOpen = panel.open; });

  const grid = el('div', { class: 'columns-panel__grid' });
  for (const column of data.columns) {
    const id = `column-${kind}-${column}`;
    grid.append(el('label', { class: 'columns-panel__item', for: id },
      el('input', {
        type: 'checkbox',
        id,
        checked: !hidden.has(column),
        onchange: (e) => {
          // Only columns this kind actually has. The defaults name a few that
          // belong to other kinds, and storing those would leave the list
          // describing columns that are not there.
          const next = new Set(
            [...hiddenColumns(kind)].filter((c) => data.columns.includes(c)));
          if (e.target.checked) next.delete(column); else next.add(column);

          // A table with no columns is not a table, and the way back from one
          // is not obvious. The last column stays.
          if (!data.columns.some((c) => !next.has(c))) {
            e.target.checked = true;
            return;
          }
          setHiddenColumns(kind, next);
          renderTable(host);
        },
      }),
      el('span', {}, data.headings[column] || column)));
  }

  panel.append(
    grid,
    el('div', { class: 'btn-row mt-3' },
      el('button', {
        class: 'btn', type: 'button',
        onclick: () => { setHiddenColumns(kind, []); renderTable(host); },
      }, 'Show every column'),
      el('button', {
        class: 'btn', type: 'button',
        title: 'Back to the columns and widths this screen started with',
        onclick: () => {
          delete state.hidden[kind];
          delete state.widths[kind];
          remember(HIDDEN_KEY, state.hidden);
          remember(WIDTH_KEY, state.widths);
          renderTable(host);
        },
      }, 'Reset the layout'),
    ),
  );

  return panel;
}

/**
 * The drag handle on the right edge of a heading.
 *
 * It is focusable and answers the arrow keys, because dragging a four-pixel
 * strip is not something everybody can do - and the person this program is
 * for is seventy-two.
 */
function widthGrip(th, kind, column, data) {
  const heading = data.headings[column] || column;

  const apply = (px) => {
    const width = Math.max(MIN_COLUMN_PX, Math.round(px));
    th.style.width = `${width}px`;
    grip.setAttribute('aria-valuenow', String(width));
    return width;
  };

  const grip = el('span', {
    class: 'col-grip',
    role: 'separator',
    'aria-orientation': 'vertical',
    tabindex: '0',
    'aria-label': `Width of the ${heading} column`,
    'aria-valuenow': String(parseInt(th.style.width, 10) || 0),
    title: 'Drag, or use the left and right arrow keys',
    onclick: (e) => e.stopPropagation(),
    onpointerdown: (e) => {
      e.preventDefault();
      e.stopPropagation();
      const startX = e.clientX;
      const startWidth = th.getBoundingClientRect().width;
      grip.setPointerCapture(e.pointerId);
      grip.classList.add('is-dragging');

      const onMove = (ev) => apply(startWidth + ev.clientX - startX);
      const onDone = () => {
        grip.removeEventListener('pointermove', onMove);
        grip.removeEventListener('pointerup', onDone);
        grip.removeEventListener('pointercancel', onDone);
        grip.classList.remove('is-dragging');
        setColumnWidth(kind, column, parseInt(th.style.width, 10));
      };
      grip.addEventListener('pointermove', onMove);
      grip.addEventListener('pointerup', onDone);
      grip.addEventListener('pointercancel', onDone);
    },
    onkeydown: (e) => {
      const step = e.shiftKey ? 48 : 16;
      const now = parseInt(th.style.width, 10) || th.getBoundingClientRect().width;
      let next;
      if (e.key === 'ArrowLeft') next = now - step;
      else if (e.key === 'ArrowRight') next = now + step;
      else if (e.key === 'Home') next = seedWidth(data.widths[column]);
      else return;
      e.preventDefault();
      e.stopPropagation();
      setColumnWidth(kind, column, apply(next));
    },
  });

  return grip;
}

function cellText(value) {
  if (value === null || value === undefined) return '';
  const text = String(value);
  // A whole mail body in one cell makes every row unreadable; the record
  // itself is one click away.
  return text.length > 300 ? `${text.slice(0, 300)}…` : text;
}

// --- saving what is on screen ---------------------------------------------

function exportBar(total) {
  const status = el('div', { id: 'export-status' });
  const withAttachments = el('input', { type: 'checkbox' });

  const save = async (format) => {
    clear(status);
    status.append(loading('Saving'));
    try {
      const result = await api.exportSearch({
        format,
        q: state.q,
        kind: state.kind || null,
        person_id: state.person_id || null,
        source_id: state.source_id || null,
        folder_id: state.folder_id || null,
        tag: state.tag || null,
        has_attachments: state.has_attachments ? true : null,
        undated: state.undated,
        date_from: state.date_from || null,
        date_to: state.date_to || null,
        copy_attachments: withAttachments.checked,
      });

      clear(status);
      const incomplete = result.files.filter((f) => !f.is_complete);
      const lines = result.files.map((f) =>
        `${f.file}\n    ${num(f.count)} records`
        + (f.integrity_file ? `\n    what is missing: ${f.integrity_file}` : ''));
      if (result.attachments) {
        lines.push(
          `${result.attachments.folder}\n    `
          + `${num(result.attachments.copied)} attachments copied`
          + (result.attachments.missing
            ? `, ${num(result.attachments.missing)} missing`
            : ''));
      }
      status.append(el('div', {
        class: incomplete.length ? 'notice notice--warning' : 'notice notice--good',
      },
        el('div', { class: 'notice__title' }, result.message),
        el('pre', { class: 'raw' }, lines.join('\n\n')),
        incomplete.length
          ? el('p', { class: 'qualified-note mb-0' },
              'Read the note beside each file before relying on it. Recall has ' +
              'written down exactly what is missing from what you just saved.')
          : null,
      ));
    } catch (err) {
      clear(status);
      status.append(errorNotice(err));
    }
  };

  // The obvious action, on its own, outside the disclosure: one workbook
  // holding every kind of record, with the Integrity sheet in front of it.
  const downloadRow = el('div', { class: 'row mb-3' },
    el('a', {
      class: 'btn btn--primary',
      href: api.exportDownloadUrl(exportParams()),
      download: '',
    }, `Download these ${num(total)} results as Excel`),
    el('span', { class: 'muted' },
      'One workbook, a sheet for each kind of record, and a sheet saying what '
      + 'is missing from it.'),
  );

  const more = el('details', { class: 'mb-3' },
    el('summary', {}, 'Other ways to save these results'),
    el('p', {},
      'Saves exactly what is listed below, with a plain-language note of what ' +
      'is missing or uncertain in this particular set of records. That note is ' +
      'not optional.'),
    el('label', { class: 'check' }, withAttachments,
      el('span', {}, 'Also copy the attachments out into a folder',
        el('span', { class: 'check__note' },
          'Each one keeps its own name, with a list saying which message it '
          + 'came from.'))),
    el('div', { class: 'btn-row' },
      el('button', { class: 'btn btn--primary', type: 'button', onclick: () => save('csv') },
        'Save as CSV for Excel'),
      el('button', { class: 'btn', type: 'button', onclick: () => save('xlsx') },
        'Save as an Excel workbook'),
      el('button', { class: 'btn', type: 'button', onclick: () => save('markdown') },
        'Save as a readable document'),
      el('button', { class: 'btn', type: 'button', onclick: () => save('json') },
        'Save as JSON'),
    ),
    status,
  );

  return el('div', {}, downloadRow, more);
}

/** The current filters, in the shape both export routes expect. */
function exportParams() {
  return {
    q: state.q,
    kind: state.kind || state.table_kind || null,
    person_id: state.person_id || null,
    source_id: state.source_id || null,
    folder_id: state.folder_id || null,
    tag: state.tag || null,
    has_attachments: state.has_attachments ? true : null,
    undated: state.undated,
    date_from: state.date_from || null,
    date_to: state.date_to || null,
  };
}

function resultCard(result, index) {
  const warnings = [];
  if (result.warnings.includes('unknown_timezone')) {
    warnings.push('the timezone of this record is not recorded');
  }
  if (result.warnings.includes('low_confidence_text')) {
    warnings.push('the text of this record was worked out, not read');
  }
  if (result.warnings.includes('implausible_date')) {
    warnings.push('the date on this record cannot be right');
  }
  if (!result.occurred_utc) warnings.push('this record has no date');

  const card = el('article', {
    class: 'card search-result',
    tabindex: '0',
    onclick: () => { window.location.hash = `#/item/${result.id}`; },
    onkeydown: (e) => {
      if (e.key === 'Enter') window.location.hash = `#/item/${result.id}`;
    },
    onfocus: () => { state.selected = index; },
  },
    el('div', { class: 'row row--tight' },
      tag(kindLabel(result.kind, { one: true }), 'plain'),
      el('span', { class: 'strong result__subject' },
        result.subject || '(no subject)'),
    ),
    el('div', { class: 'row muted small' },
      el('span', {}, result.occurred_utc ? date(result.occurred_utc, { withTime: true }) : 'no date'),
      !result.timezone_known && result.occurred_utc
        ? el('span', { class: 'qualified-note' }, 'timezone not recorded')
        : null,
      result.people.length ? el('span', {}, result.people.join(', ')) : null,
      result.location ? el('span', {}, result.location) : null,
      result.has_attachments ? tag('has attachments', 'info') : null,
    ),
    result.snippet
      ? el('p', { class: 'mb-0', html: result.snippet })
      : (result.preview ? el('p', { class: 'mb-0 muted' }, result.preview) : null),
    warnings.length
      ? el('div', { class: 'qualified-note' }, warnings.join(' · '))
      : null,
  );
  return card;
}
