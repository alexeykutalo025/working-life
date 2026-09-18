// Search - one large box, filters down the side, results with snippets.
//
// Keyboard, from the spec: `/` focuses the box, up and down move through the
// results, Enter opens one. A 72-year-old is not obliged to use them, but
// somebody reading a decade of mail will, and they cost nothing to provide.

import { api } from '../api.js';
import {
  clear, date, debounce, el, empty, errorNotice, loading, mount, num, plural,
  setTitle, tag,
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
};

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
        style: 'font-size:22px;min-height:56px',
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
    el('div', { style: 'display:grid;grid-template-columns:280px 1fr;gap:24px;align-items:start' },
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
    el('label', { for: 'filter-to', style: 'margin-top:8px' }, 'Up to this date'),
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

function kindLabel(kind) {
  return ({
    message: 'Messages', event: 'Calendar entries', contact: 'Contacts',
    task: 'Tasks', note: 'Notes',
  })[kind] || kind;
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
      limit: 50,
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
    el('span', { class: 'stat__value', style: 'font-size:26px' }, num(total.value)),
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

  const list = el('div', { class: 'stack' });
  data.results.forEach((result, index) => list.append(resultCard(result, index)));
  host.append(list);

  if (total.value > state.offset + data.results.length) {
    host.append(el('div', { class: 'btn-row' },
      state.offset > 0
        ? el('button', {
            class: 'btn', type: 'button',
            onclick: () => { state.offset = Math.max(0, state.offset - 50); runSearch(); },
          }, 'Previous 50')
        : null,
      el('button', {
        class: 'btn btn--primary', type: 'button',
        onclick: () => { state.offset += 50; runSearch(); },
      }, 'Next 50'),
      el('span', { class: 'muted' },
        `Showing ${num(state.offset + 1)} to ${num(state.offset + data.results.length)} of ${num(total.value)}`),
    ));
  }
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
    style: 'cursor:pointer;margin-bottom:12px',
    onclick: () => { window.location.hash = `#/item/${result.id}`; },
    onkeydown: (e) => {
      if (e.key === 'Enter') window.location.hash = `#/item/${result.id}`;
    },
    onfocus: () => { state.selected = index; },
  },
    el('div', { class: 'row', style: 'gap:10px' },
      tag(kindLabel(result.kind).replace(/s$/, ''), 'plain'),
      el('span', { class: 'strong', style: 'font-size:19px' },
        result.subject || '(no subject)'),
    ),
    el('div', { class: 'row muted small', style: 'gap:12px' },
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
