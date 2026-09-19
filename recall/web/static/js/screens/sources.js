// Files found - the inventory from Phase 0.
//
// Lists every Outlook file on this computer with its size, dates, and whether
// it is a duplicate, a cloud-only file, or locked by Outlook. Starts a search,
// shows live progress, cancels, and downloads cloud-only files only after
// showing what that would cost.

import { api } from '../api.js';
import {
  bytes, clear, date, debounce, el, empty, errorDialog, errorNotice, field,
  loading, modal, mount, notice, num, plural, progressBar, setTitle,
  severityTag, stat, tag,
} from '../ui.js';

const state = {
  sort: 'size',
  order: 'desc',
  filters: {},
  selected: new Set(),
  rows: [],
  total: 0,
};

let pollTimer = null;

export async function render() {
  setTitle('Files found');

  const root = el('div', {},
    el('h1', { class: 'page__title' }, 'Files found on this computer'),
    el('p', { class: 'page__lede' },
      'Everything below was found by searching your drives. Nothing here has ' +
      'been opened, changed, moved or deleted — Recall only reads.'),
    el('div', { id: 'summary' }),
    el('div', { id: 'job' }),
    el('div', { id: 'controls' }),
    el('div', { id: 'table' }, loading('Loading the list')),
  );
  mount(root);

  renderControls();
  await Promise.all([refreshSummary(), refreshTable()]);
  await pollJob();

  return () => { if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; } };
}

// --- summary --------------------------------------------------------------

async function refreshSummary() {
  const host = document.getElementById('summary');
  if (!host) return;
  try {
    const summary = await api.sourcesSummary();
    const c = summary.counts || {};
    clear(host);

    const attention = (c.unreadable || 0) + (c.failed || 0) + (c.placeholders || 0);
    const cards = el('div', { class: 'grid grid--4 mb-5' },
      stat('Files found', num(c.n), null),
      stat('Total size', bytes(c.total_bytes), null),
      stat('Already read', num(c.parsed),
        c.pending ? `${num(c.pending)} still to read` : 'all of them'),
      stat('Need attention', num(attention),
        attention ? 'cloud-only, locked or could not be read' : 'nothing is blocked',
        { alarming: attention > 0 }),
    );

    host.append(
      el('div', { class: 'notice notice--info' },
        el('p', { class: 'mb-0' }, summary.sentence)),
      cards,
    );

    if (summary.by_type && summary.by_type.length) {
      host.append(
        el('details', {},
          el('summary', {}, 'What kinds of file were found'),
          el('div', { class: 'table-wrap' },
            el('table', {},
              el('thead', {}, el('tr', {},
                el('th', {}, 'Type'),
                el('th', { class: 'num' }, 'How many'),
                el('th', { class: 'num' }, 'Size'),
              )),
              el('tbody', {}, ...summary.by_type.map((t) => el('tr', {},
                el('td', {}, `${t.ext}  —  ${typeName(t.ext)}`),
                el('td', { class: 'num' }, num(t.count)),
                el('td', { class: 'num' }, bytes(t.bytes)),
              ))),
            ),
          ),
        ),
      );
    }
  } catch (err) {
    clear(host);
    host.append(errorNotice(err));
  }
}

const TYPE_NAMES = {
  '.pst': 'Outlook data file', '.ost': 'Outlook offline mailbox',
  '.msg': 'saved Outlook message', '.eml': 'saved email',
  '.mbox': 'mailbox', '.mbx': 'Outlook Express 4 mailbox',
  '.dbx': 'Outlook Express folder', '.ics': 'calendar',
  '.vcs': 'older calendar', '.vcf': 'contact card',
  '.olm': 'Mac Outlook archive', '.wab': 'Windows Address Book',
  '.pab': 'Outlook address book',
};
function typeName(ext) { return TYPE_NAMES[ext] || 'file'; }

// --- controls -------------------------------------------------------------

function renderControls() {
  const host = document.getElementById('controls');
  clear(host);

  host.append(
    el('div', { class: 'btn-row mb-5' },
      el('button', {
        class: 'btn btn--primary btn--big',
        type: 'button',
        onclick: openDriveChooser,
      }, 'Find Outlook files on this computer'),
      el('button', {
        class: 'btn btn--big',
        type: 'button',
        onclick: () => openReadDialog(null),
      }, 'Read them into the archive'),
      el('button', {
        class: 'btn',
        type: 'button',
        onclick: async () => { await refreshSummary(); await refreshTable(); },
      }, 'Refresh this list'),
    ),

    el('div', { class: 'toolbar' },
      field('Search the list', el('input', {
        type: 'search', id: 'filter-search', placeholder: 'part of a file name or folder',
        oninput: debounce(() => {
          state.filters.search = document.getElementById('filter-search').value.trim();
          refreshTable();
        }),
      })),
      field('Show', el('select', {
        id: 'filter-kind',
        onchange: () => {
          const v = document.getElementById('filter-kind').value;
          state.filters.only_duplicates = v === 'duplicates';
          state.filters.only_placeholders = v === 'cloud';
          state.filters.only_problems = v === 'problems';
          state.filters.state = v === 'unread' ? 'pending' : (v === 'read' ? 'done' : '');
          refreshTable();
        },
      },
        el('option', { value: '' }, 'Everything'),
        el('option', { value: 'unread' }, 'Not read yet'),
        el('option', { value: 'read' }, 'Already read'),
        el('option', { value: 'duplicates' }, 'Duplicates only'),
        el('option', { value: 'cloud' }, 'Cloud-only files'),
        el('option', { value: 'problems' }, 'Files with problems'),
      )),
      field('Where it is', el('select', {
        id: 'filter-container',
        onchange: () => {
          state.filters.container = document.getElementById('filter-container').value;
          refreshTable();
        },
      },
        el('option', { value: '' }, 'Anywhere'),
        el('option', { value: 'local' }, 'On this computer'),
        el('option', { value: 'onedrive' }, 'In OneDrive'),
        el('option', { value: 'external' }, 'On a removable drive'),
        el('option', { value: 'network' }, 'On the network'),
      )),
    ),

    el('div', { class: 'btn-row mb-5', id: 'selection-actions' }),
  );
}

// --- the drive chooser ----------------------------------------------------

async function openDriveChooser() {
  let targets;
  try {
    targets = await api.scanTargets();
  } catch (err) {
    errorDialog(err);
    return;
  }

  const boxes = new Map();
  const list = el('div', { class: 'stack' });

  for (const t of targets) {
    const input = el('input', {
      type: 'checkbox',
      checked: t.exists,
      disabled: !t.exists,
    });
    boxes.set(t.path, { input, target: t });

    const size = t.total_bytes
      ? `${bytes(t.total_bytes)} in total, ${bytes(t.free_bytes)} free`
      : null;

    list.append(el('label', { class: 'check' },
      input,
      el('span', {},
        el('strong', {}, t.label),
        el('span', { class: 'check__note' }, t.path),
        size ? el('span', { class: 'check__note' }, size) : null,
        t.note ? el('span', { class: 'check__note' }, t.note) : null,
        !t.exists ? el('span', { class: 'check__note' }, 'Not available on this computer.') : null,
      ),
    ));
  }

  const fullHash = el('input', { type: 'checkbox' });

  // Folders picked by hand are appended here, ticked, so the user sees the
  // choice land in the same list as everything else.
  const picked = el('div', { class: 'stack' });

  const addFolder = (path) => {
    if (boxes.has(path)) {
      boxes.get(path).input.checked = true;
      return;
    }
    const input = el('input', { type: 'checkbox', checked: true });
    boxes.set(path, { input, target: { path } });
    picked.append(el('label', { class: 'check' },
      input,
      el('span', {},
        el('strong', {}, folderName(path)),
        el('span', { class: 'check__note' }, path),
        el('span', { class: 'check__note' }, 'A folder you chose just now.'),
      ),
    ));
  };

  const body = el('div', {},
    el('p', {},
      'Everything is ticked to start with, which searches the whole computer. ' +
      'Untick anything you want to leave out — searching one drive is much faster.'),
    list,
    picked,
    el('div', { class: 'btn-row' },
      el('button', {
        class: 'btn', type: 'button',
        onclick: () => openFolderPicker(addFolder),
      }, 'Choose a specific folder…'),
    ),
    el('p', { class: 'field__help' },
      'If you already know where your old mail is, pointing Recall straight at ' +
      'that folder takes seconds instead of an hour.'),
    el('hr', { class: 'rule' }),
    el('label', { class: 'check' },
      fullHash,
      el('span', {},
        el('strong', {}, 'Compare every file, including the very large ones'),
        el('span', { class: 'check__note' },
          'Slower, but needed before Recall can say for certain which large ' +
          'files are duplicates of each other.'),
      ),
    ),
  );

  const dialog = modal({
    title: 'Where should Recall look?',
    body,
    actions: [
      el('button', {
        class: 'btn btn--primary', type: 'button',
        onclick: async () => {
          const roots = [...boxes.values()]
            .filter(({ input }) => input.checked)
            .map(({ target }) => target.path);
          if (!roots.length) {
            body.prepend(notice('warning', 'Nothing is ticked',
              'Tick at least one place for Recall to look in.'));
            return;
          }
          dialog.close();
          try {
            await api.startScan(roots, fullHash.checked);
            pollJob();
          } catch (err) {
            errorDialog(err);
          }
        },
      }, 'Start looking'),
      el('button', { class: 'btn', type: 'button', onclick: () => dialog.close() }, 'Cancel'),
    ],
  });
}

// --- picking one folder ---------------------------------------------------
//
// A file-explorer-shaped chooser, kept to what the job needs: folders only,
// because the user is choosing a place to search rather than a file to open.
// Nothing here is a tree view - one level at a time with a breadcrumb is far
// easier to follow than an expanding tree, and it cannot get into a state
// where the user has lost track of where they are.

function folderName(path) {
  const parts = String(path).replace(/[\\/]+$/, '').split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

function openFolderPicker(onChoose) {
  const listing = el('div', { class: 'stack' }, loading('Looking at this computer'));
  const where = el('div', { class: 'picker__where' });
  let current = null;

  const go = async (path) => {
    clear(listing);
    clear(where);
    listing.append(loading('Opening the folder'));

    let data;
    try {
      data = await api.browseFolders(path);
    } catch (err) {
      clear(listing);
      listing.append(errorNotice(err));
      return;
    }

    current = data.path;
    useThis.disabled = !data.path;
    clear(listing);

    // Where we are, as pieces you can click to climb back out.
    where.append(el('button', {
      class: 'btn btn--quiet', type: 'button', onclick: () => go(''),
    }, 'This computer'));
    for (const crumb of data.crumbs || []) {
      where.append(el('span', { class: 'picker__sep', 'aria-hidden': 'true' }, '›'));
      where.append(el('button', {
        class: 'btn btn--quiet', type: 'button', onclick: () => go(crumb.path),
      }, crumb.name));
    }

    if (data.parent) {
      listing.append(el('button', {
        class: 'btn btn--block picker__up', type: 'button',
        onclick: () => go(data.parent),
      }, '↑ Up one level'));
    }

    if (!data.readable) {
      listing.append(notice('warning', 'Recall cannot open this folder', data.note));
      return;
    }

    for (const entry of data.entries) {
      listing.append(el('button', {
        class: 'btn btn--block picker__folder', type: 'button',
        onclick: () => go(entry.path),
      },
        el('span', { class: 'picker__name' }, entry.name),
        entry.excluded_by_default
          ? el('span', { class: 'check__note' },
              'Normally skipped — Recall will search it because you chose it.')
          : null,
      ));
    }

    if (data.note) listing.append(el('p', { class: 'muted' }, data.note));
  };

  const useThis = el('button', {
    class: 'btn btn--primary', type: 'button', disabled: true,
    onclick: () => {
      if (!current) return;
      onChoose(current);
      dialog.close();
    },
  }, 'Use this folder');

  const dialog = modal({
    title: 'Which folder?',
    body: el('div', { class: 'picker' },
      el('p', {},
        'Open folders until you reach the one your old mail is in, then press ' +
        '“Use this folder”. Only folders are shown.'),
      where,
      listing,
    ),
    actions: [
      useThis,
      el('button', { class: 'btn', type: 'button', onclick: () => dialog.close() }, 'Cancel'),
    ],
  });

  go('');
}

// --- reading files into the archive ---------------------------------------

async function openReadDialog(ids) {
  let plan;
  try {
    plan = await api.extractPlan(ids);
  } catch (err) {
    errorDialog(err);
    return;
  }

  if (!plan.to_read && !plan.already_read) {
    errorDialog(new Error(
      'There is nothing to read. Search for Outlook files first.'));
    return;
  }

  const sample = el('input', { type: 'checkbox' });
  const kinds = el('select', {},
    el('option', { value: '' }, 'Everything — mail, calendar and contacts'),
    el('option', { value: 'mail' }, 'Mail only'),
    el('option', { value: 'calendar' }, 'Calendar only'),
    el('option', { value: 'contacts' }, 'Contacts only'),
  );
  const again = el('input', { type: 'checkbox' });

  const warnings = [];
  if (plan.cloud_only) {
    warnings.push(notice('info',
      `${plural(plan.cloud_only, 'file')} will be left out`,
      'They are stored in the cloud only. Tick them in the list below and ' +
      'download them first if you want them included.'));
  }
  if (plan.locked) {
    warnings.push(notice('warning',
      `${plural(plan.locked, 'file')} cannot be opened`,
      'Something is holding them open — usually Outlook. Close Outlook ' +
      'completely, search again, and they will be included.'));
  }

  const body = el('div', {},
    ...warnings,
    el('p', {},
      ids
        ? `Reading ${plural(plan.files, 'chosen file')}, ${bytes(plan.total_bytes)} in total.`
        : `Reading ${plural(plan.to_read, 'file')} not read yet, `
          + `${bytes(plan.total_bytes)} in total.`),
    el('p', {},
      el('strong', {}, 'How long: '), plan.estimate, '. ',
      el('span', { class: 'muted' }, plan.note)),

    el('label', { class: 'check' }, sample,
      el('span', {},
        el('strong', {}, 'Just read the first 50 records from each file, to check'),
        el('span', { class: 'check__note' },
          'Takes seconds. Do this first: it proves the files read correctly '
          + 'before you commit to a long run. Reading properly afterwards adds '
          + 'the rest without duplicating anything.'))),

    el('div', { class: 'field' },
      el('label', { for: 'read-kinds' }, 'What to read'),
      (kinds.id = 'read-kinds', kinds),
      el('div', { class: 'field__help' },
        'Reading one kind at a time is faster if you only want the calendar.')),

    plan.already_read
      ? el('label', { class: 'check' }, again,
          el('span', {},
            `Read the ${plural(plan.already_read, 'file')} already read again`,
            el('span', { class: 'check__note' },
              'Only useful after changing a setting. Nothing is duplicated '
              + 'either way.')))
      : null,

    el('p', { class: 'muted mb-0' },
      'You can stop at any time. Everything read so far is kept, and starting '
      + 'again carries on from where it stopped. Your original files are only '
      + 'ever read — never changed.'),
  );

  const dialog = modal({
    title: ids ? 'Read the chosen files?' : 'Read your Outlook files?',
    body,
    actions: [
      el('button', {
        class: 'btn btn--primary', type: 'button',
        onclick: async () => {
          dialog.close();
          try {
            await api.startExtract({
              ids: ids || [],
              kinds: kinds.value || null,
              sample: sample.checked ? 50 : 0,
              resume: !again.checked,
            });
            pollJob();
          } catch (err) {
            errorDialog(err);
          }
        },
      }, sample.checked ? 'Read a sample' : 'Start reading'),
      el('button', { class: 'btn', type: 'button', onclick: () => dialog.close() },
        'Not now'),
    ],
  });
}

// --- live job progress ----------------------------------------------------

async function pollJob() {
  const host = document.getElementById('job');
  if (!host) return;

  let job;
  try {
    job = await api.job();
  } catch {
    return;
  }

  clear(host);

  if (job.state === 'running') {
    host.append(el('div', { class: 'card' },
      el('h2', { class: 'card__title mt-0' }, jobTitle(job.kind)),
      el('p', {}, job.message),
      progressBar(job),
      el('div', { class: 'btn-row' },
        el('button', {
          class: 'btn btn--danger', type: 'button',
          onclick: async () => {
            try { await api.cancelJob(); } catch (err) { errorDialog(err); }
          },
        }, 'Stop, and keep what has been done so far'),
      ),
    ));
    pollTimer = setTimeout(pollJob, 1000);
    return;
  }

  if (job.state === 'done' && job.kind) {
    const d = job.detail || {};
    host.append(notice('good', jobTitle(job.kind) + ' finished',
      el('p', {}, job.message),
      d.sampled
        ? el('p', { class: 'qualified-note' },
            `That was a sample of the first ${num(d.sampled)} records per file, `
            + 'not the whole thing. Read them properly when you are ready - '
            + 'nothing will be duplicated.')
        : null,
      d.attachments_written
        ? el('p', {}, `${num(d.attachments_written)} attachments were saved.`)
        : null,
      d.threads && d.threads.threads
        ? el('p', {},
            `${num(d.threads.threads)} conversations were put back together.`)
        : null,
      d.skipped_unreadable
        ? el('p', { class: 'qualified-note' },
            `${num(d.skipped_unreadable)} chosen file(s) were left out because `
            + 'they are cloud-only or could not be opened.')
        : null,
      (d.failures && d.failures.length)
        ? el('details', {},
            el('summary', {},
              `${num(d.failures.length)} file(s) could not be read`),
            el('pre', { class: 'raw' },
              d.failures
                .map(([path, reason]) => `${path}\n    ${reason}`)
                .join('\n\n')))
        : null,
      job.detail && job.detail.skipped_roots && job.detail.skipped_roots.length
        ? el('p', {}, `These places were skipped because they do not exist: ${job.detail.skipped_roots.join(', ')}`)
        : null,
      job.detail && job.detail.covered_roots && job.detail.covered_roots.length
        ? el('p', {},
            'These were already inside somewhere else you chose, so they were ' +
            `searched once rather than twice: ${job.detail.covered_roots.join(', ')}`)
        : null,
      job.detail && job.detail.unreadable_dirs
        ? el('p', {},
            `${num(job.detail.unreadable_dirs)} folder(s) could not be opened and were ` +
            'skipped. That is normal for system folders; the log lists every one.')
        : null,
    ));
    await refreshSummary();
    await refreshTable();
    return;
  }

  if (job.state === 'canceled') {
    host.append(notice('warning', 'Stopped', el('p', {}, job.message)));
    await refreshSummary();
    await refreshTable();
    return;
  }

  if (job.state === 'failed') {
    host.append(notice('error', jobTitle(job.kind) + ' stopped with a problem',
      el('p', {}, job.message),
      el('p', {}, job.error || ''),
      job.detail && job.detail.traceback
        ? el('details', {},
            el('summary', {}, 'Technical detail, for when you need to send it to someone'),
            el('pre', { class: 'raw' }, job.detail.traceback))
        : null,
    ));
    await refreshTable();
  }
}

function jobTitle(kind) {
  return ({
    scan: 'Looking for Outlook files',
    extract: 'Reading your Outlook files',
    hydrate: 'Downloading from OneDrive',
    index: 'Building the search index',
  })[kind] || 'Work';
}

// --- the table ------------------------------------------------------------

async function refreshTable() {
  const host = document.getElementById('table');
  if (!host) return;

  let data;
  try {
    data = await api.sources({
      sort: state.sort,
      order: state.order,
      limit: 500,
      ...state.filters,
    });
  } catch (err) {
    clear(host);
    host.append(errorNotice(err));
    return;
  }

  state.rows = data.rows;
  state.total = data.total;
  clear(host);

  if (!data.rows.length) {
    host.append(empty(
      state.total === 0 && !Object.values(state.filters).some(Boolean)
        ? 'No Outlook files have been found yet'
        : 'Nothing matches those filters',
      state.total === 0
        ? 'Press "Find Outlook files on this computer" above to start looking.'
        : 'Change the filters to see more.',
    ));
    renderSelectionActions();
    return;
  }

  const header = (key, label, cls) => el('th', { class: cls || '' },
    el('button', {
      type: 'button',
      onclick: () => {
        if (state.sort === key) state.order = state.order === 'desc' ? 'asc' : 'desc';
        else { state.sort = key; state.order = 'desc'; }
        refreshTable();
      },
      'aria-label': `Sort by ${label}`,
    }, label + (state.sort === key ? (state.order === 'desc' ? '  ↓' : '  ↑') : '')),
  );

  const selectAll = el('input', {
    type: 'checkbox',
    'aria-label': 'Select every file in this list',
    onchange: (e) => {
      state.rows.forEach((r) => {
        if (e.target.checked) state.selected.add(r.id); else state.selected.delete(r.id);
      });
      refreshTable();
    },
  });

  host.append(
    el('p', { class: 'muted' },
      data.total > data.rows.length
        ? `Showing the first ${num(data.rows.length)} of ${num(data.total)} files.`
        : `Showing all ${plural(data.total, 'file')}.`),
    el('div', { class: 'table-wrap' },
      el('table', {},
        el('thead', {},
          el('tr', {},
            el('th', { class: 'col-check' }, selectAll),
            header('name', 'File'),
            header('type', 'Type'),
            header('size', 'Size', 'num'),
            header('modified', 'Last changed'),
            el('th', {}, 'Condition'),
            header('items', 'Records', 'num'),
          ),
        ),
        el('tbody', {}, ...data.rows.map(rowView)),
      ),
    ),
  );
  renderSelectionActions();
}

function rowView(r) {
  const box = el('input', {
    type: 'checkbox',
    checked: state.selected.has(r.id),
    'aria-label': `Select ${r.name}`,
    onchange: (e) => {
      if (e.target.checked) state.selected.add(r.id); else state.selected.delete(r.id);
      renderSelectionActions();
    },
  });

  const conditions = [];
  if (r.is_placeholder) conditions.push(tag('Cloud only — not downloaded', 'info'));
  if (!r.is_readable) conditions.push(tag('Locked — close Outlook', 'critical'));
  if (r.duplicate_of) conditions.push(tag('Duplicate', 'medium'));
  if (r.parse_state === 'done') conditions.push(tag('Read', 'good'));
  if (r.parse_state === 'failed') conditions.push(tag('Could not be read', 'critical'));
  if (r.parse_state === 'pending' && r.is_readable && !r.is_placeholder) {
    conditions.push(tag('Not read yet', 'plain'));
  }
  if (r.finding_count) conditions.push(severityTag(r.worst_severity));
  if (!conditions.length) conditions.push(tag('Fine', 'good'));

  return el('tr', { class: state.selected.has(r.id) ? 'is-selected' : '' },
    el('td', {}, box),
    el('td', {},
      el('div', { class: 'cell-name' }, r.name),
      el('div', { class: 'cell-path' }, r.folder),
      r.duplicate_of_path
        ? el('div', { class: 'cell-path' }, `Identical to: ${r.duplicate_of_path}`)
        : null,
      r.lock_error
        ? el('div', { class: 'cell-path' }, r.lock_error)
        : null,
      r.estimated_loss
        ? el('div', { class: 'qualified-note' },
            `about ${num(r.estimated_loss)} records could not be read from this file`)
        : null,
    ),
    el('td', {}, el('span', { class: 'nowrap' }, r.ext), el('div', { class: 'cell-path' }, typeName(r.ext))),
    el('td', { class: 'num nowrap' }, bytes(r.size_bytes)),
    el('td', { class: 'nowrap' }, date(r.mtime_utc)),
    el('td', {}, el('div', { class: 'row' }, ...conditions),
      el('div', { class: 'cell-path' }, r.comparison_state)),
    el('td', { class: 'num' }, r.parse_state === 'done' ? num(r.item_count) : '—'),
  );
}

function renderSelectionActions() {
  const host = document.getElementById('selection-actions');
  if (!host) return;
  clear(host);

  const n = state.selected.size;
  if (!n) {
    host.append(el('p', { class: 'muted mb-0' },
      'Tick files above to download cloud-only ones.'));
    return;
  }

  const chosen = state.rows.filter((r) => state.selected.has(r.id));
  const cloud = chosen.filter((r) => r.is_placeholder);
  const readable = chosen.filter((r) => !r.is_placeholder && r.is_readable);

  host.append(
    el('span', { class: 'strong' }, `${plural(n, 'file')} selected.`),
    readable.length
      ? el('button', {
          class: 'btn btn--primary', type: 'button',
          onclick: () => openReadDialog(readable.map((r) => r.id)),
        }, `Read ${plural(readable.length, 'file')} into the archive`)
      : null,
    cloud.length
      ? el('button', {
          class: 'btn', type: 'button',
          onclick: () => confirmHydrate(cloud.map((r) => r.id)),
        }, `Download ${plural(cloud.length, 'cloud-only file')} from OneDrive`)
      : null,
    el('button', {
      class: 'btn', type: 'button',
      onclick: () => { state.selected.clear(); refreshTable(); },
    }, 'Clear selection'),
  );
}

// --- OneDrive download, only after showing the cost -----------------------

async function confirmHydrate(ids) {
  let plan;
  try {
    plan = await api.hydratePlan(ids);
  } catch (err) {
    errorDialog(err);
    return;
  }

  const warnings = [];
  if (!plan.fits_on_disk) {
    warnings.push(notice('error', 'Not enough room on this drive',
      `Downloading these needs ${bytes(plan.total_bytes)} and there is only ` +
      `${bytes(plan.free_bytes)} free. Free up space first, or choose fewer files.`));
  }
  if (plan.over_batch_limit) {
    warnings.push(notice('warning', 'More than the safety limit for one batch',
      `That is ${plan.total_gb} GB, over the ${plan.batch_limit_gb} GB limit. ` +
      'Choose fewer files, or raise max_hydrate_batch_gb in config.toml.'));
  }

  const blocked = !plan.fits_on_disk || plan.over_batch_limit;

  const dialog = modal({
    title: 'Download these files from OneDrive?',
    body: el('div', {},
      ...warnings,
      el('p', {}, plan.sentence),
      el('p', {},
        'These files are stored in the cloud and their contents are not on this ' +
        'computer. Downloading them uses your internet connection, and may cost ' +
        'money on a metered connection. Nothing is downloaded until you press ' +
        'the button below.'),
      el('details', {},
        el('summary', {}, `The ${plural(plan.count, 'file')} that would be downloaded`),
        el('pre', { class: 'raw' },
          plan.files.map((f) => `${bytes(f.size_bytes).padStart(10)}  ${f.path}`).join('\n')),
      ),
    ),
    actions: [
      el('button', {
        class: 'btn btn--primary', type: 'button', disabled: blocked,
        onclick: async () => {
          dialog.close();
          try {
            await api.hydrate(ids);
            pollJob();
          } catch (err) { errorDialog(err); }
        },
      }, `Yes, download ${bytes(plan.total_bytes)}`),
      el('button', { class: 'btn', type: 'button', onclick: () => dialog.close() },
        'No, leave them in the cloud'),
    ],
  });
}
