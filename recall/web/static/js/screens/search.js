// Search - one large box, filters down the side, results with snippets.
//
// Keyboard, from the spec: `/` focuses the box, up and down move through the
// results, Enter opens one. A 72-year-old is not obliged to use them, but
// somebody reading a decade of mail will, and they cost nothing to provide.

import { api } from '../api.js';
import {
  clear, date, debounce, el, empty, errorNotice, kindLabel, loading, mount,
  num, plural, setTitle, tag,
} from '../ui.js';

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
  selected: -1,
  results: [],
  //: 'cards' reads well; 'table' is for working with the figures.
  view: 'cards',
  //: Which kind's table is showing, when the results hold more than one.
  table_kind: '',
};

//: How many results a page holds, in either view.
const PAGE = 50;
const TABLE_PAGE = 50;

let keyHandler = null;

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
  const next = Math.max(0, Math.min(state.results.length - 1, state.selected + delta));
  state.selected = next;
  document.querySelectorAll('.search-result').forEach((node, i) => {
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

  clear(host);
  const panel = el('div', { class: 'card' }, el('h2', { class: 'card__title mt-0' }, 'Narrow it down'));

  panel.append(select('What kind of record', 'kind', [
    { value: '', label: 'Everything' },
    ...data.kinds.map((k) => ({
      value: k.id, label: `${kindLabel(k.id)} (${num(k.count)})`,
    })),
  ]));

  panel.append(el('div', { class: 'field' },
    el('label', { for: 'filter-from' }, 'From this date'),
    el('input', {
      type: 'text', id: 'filter-from', value: state.date_from,
      placeholder: '2003, or 2003-04',
      oninput: debounce((e) => { state.date_from = e.target.value.trim(); state.offset = 0; runSearch(); }, 400),
    }),
    el('label', { for: 'filter-to', class: 'mt-3' }, 'Up to this date'),
    el('input', {
      type: 'text', id: 'filter-to', value: state.date_to,
      placeholder: '2009, or 2009-12',
      oninput: debounce((e) => { state.date_to = e.target.value.trim(); state.offset = 0; runSearch(); }, 400),
    }),
  ));

  if (data.undated) {
    panel.append(el('label', { class: 'check' },
      el('input', {
        type: 'checkbox', checked: state.undated,
        onchange: (e) => { state.undated = e.target.checked; state.offset = 0; runSearch(); },
      }),
      el('span', {},
        `Only records with no date (${num(data.undated)})`,
        el('span', { class: 'check__note' },
          'These are not in any date range above, because they have no date.')),
    ));
  }

  panel.append(el('label', { class: 'check' },
    el('input', {
      type: 'checkbox',
      onchange: (e) => {
        state.has_attachments = e.target.checked ? 'true' : '';
        state.offset = 0;
        runSearch();
      },
    }),
    el('span', {}, 'Only records with attachments'),
  ));

  if (data.people.length) {
    panel.append(select('Person', 'person_id', [
      { value: '', label: 'Anyone' },
      ...data.people.map((p) => ({
        value: p.id, label: `${p.name || '(no name)'} (${num(p.count)})`,
      })),
    ]));
  }

  if (data.sources.length) {
    panel.append(select('Which file it came from', 'source_id', [
      { value: '', label: 'Any file' },
      ...data.sources.map((s) => ({
        value: s.id, label: `${fileName(s.path)} (${num(s.count)})`,
      })),
    ]));
  }

  if (data.folders.length) {
    panel.append(select('Folder', 'folder_id', [
      { value: '', label: 'Any folder' },
      ...data.folders.map((f) => ({
        value: f.id, label: `${f.path} (${num(f.count)})`,
      })),
    ]));
  }

  if (data.tags.length) {
    panel.append(select('Category', 'tag', [
      { value: '', label: 'Any category' },
      ...data.tags.map((t) => ({ value: t.name, label: `${t.name} (${num(t.count)})` })),
    ]));
  }

  panel.append(select('Order by', 'sort', [
    { value: 'relevance', label: 'Best match first' },
    { value: 'newest', label: 'Newest first' },
    { value: 'oldest', label: 'Oldest first' },
  ]));

  panel.append(el('button', {
    class: 'btn btn--block', type: 'button',
    onclick: () => {
      Object.assign(state, {
        kind: '', person_id: '', source_id: '', folder_id: '', tag: '',
        has_attachments: '', undated: false, date_from: '', date_to: '',
        offset: 0,
      });
      loadFilters();
      runSearch();
    },
  }, 'Clear all filters'));

  host.append(panel);
}

function select(label, key, options) {
  const id = `filter-${key}`;
  return el('div', { class: 'field' },
    el('label', { for: id }, label),
    el('select', {
      id,
      onchange: (e) => { state[key] = e.target.value; state.offset = 0; runSearch(); },
    }, ...options.map((o) => el('option', {
      value: o.value,
      selected: String(state[key]) === String(o.value),
    }, o.label))),
  );
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
      limit: PAGE,
      offset: state.offset,
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

  if (!data.results.length) {
    host.append(empty(
      state.q ? 'Nothing matches that' : 'Nothing here yet',
      state.q
        ? 'Try fewer words, or a different spelling. Recall searches the ' +
          'subject, the message text, everyone on it, and the text inside ' +
          'attachments.'
        : 'Type something in the box above, or use the filters to browse.',
    ));
    return;
  }

  host.append(viewSwitch());
  host.append(exportBar(total.value));

  if (state.view === 'table') {
    const table = el('div', { id: 'search-table' }, loading('Building the table'));
    host.append(table);
    drawTable(table);
    return;
  }

  const list = el('div', { class: 'stack' });
  data.results.forEach((result, index) => list.append(resultCard(result, index)));
  host.append(list);

  host.append(pager(total.value, data.results.length, (offset) => {
    state.offset = offset;
    runSearch();
  }));
}

/**
 * Previous / Next, and where you are.
 *
 * The whole block used to be conditional on there being a next page, so on the
 * last page "Previous" disappeared too and the only way back was to run the
 * search again.
 */
function pager(total, shown, go) {
  if (total <= shown && state.offset === 0) return null;

  const from = state.offset + 1;
  const to = state.offset + shown;

  return el('div', { class: 'btn-row mt-4' },
    state.offset > 0
      ? el('button', {
          class: 'btn', type: 'button',
          onclick: () => go(Math.max(0, state.offset - PAGE)),
        }, `Previous ${PAGE}`)
      : null,
    to < total
      ? el('button', {
          class: 'btn btn--primary', type: 'button',
          onclick: () => go(state.offset + PAGE),
        }, `Next ${PAGE}`)
      : null,
    el('span', { class: 'muted' },
      `Showing ${num(from)} to ${num(to)} of ${num(total)}`),
  );
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
      runSearch();
    },
  }, label);

  return el('div', { class: 'row mb-3' },
    el('span', { class: 'muted' }, 'Show as'),
    el('div', { class: 'btn-row' },
      button('cards', 'Readable list', 'One result at a time, with the matching words marked'),
      button('table', 'Table', 'Every column, as it appears in the spreadsheet'),
    ),
  );
}

async function drawTable(host) {
  let data;
  try {
    data = await api.searchTable({
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
      limit: TABLE_PAGE,
      offset: state.offset,
    });
  } catch (err) {
    clear(host);
    host.append(errorNotice(err));
    return;
  }

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

  const head = el('tr', {},
    ...data.columns.map((c) => el('th', { class: 'nowrap' }, data.headings[c] || c)));

  const body = el('tbody', {});
  for (const row of data.rows) {
    body.append(el('tr', {
      class: 'is-clickable',
      onclick: () => { window.location.hash = `#/item/${row.item_id}`; },
    },
      ...data.columns.map((c) => el('td', {}, cellText(row[c]))),
    ));
  }

  host.append(
    el('div', { class: 'table-wrap' }, el('table', {}, el('thead', {}, head), body)),
    el('p', { class: 'muted mt-3' },
      'These are the same columns you get in the spreadsheet. Click a row to ' +
      'open the record.'),
    pager(data.total.value, data.rows.length, (offset) => {
      state.offset = offset;
      runSearch();
    }),
  );
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
