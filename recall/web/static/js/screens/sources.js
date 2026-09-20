// Files found - the inventory from Phase 0.
//
// Lists every Outlook file on this computer with its size, dates, and whether
// it is a duplicate, a cloud-only file, or locked by Outlook. Starts a search,
// shows live progress, cancels, and downloads cloud-only files only after
// showing what that would cost.

import { api } from '../api.js';
import { folderName, openFolderPicker } from '../folderpicker.js';
import {
  add, bytes, clear, date, debounce, el, empty, errorDialog, errorNotice,
  field, loading, modal, mount, notice, num, pager, plural, progressBar,
  setTitle, severityTag, stat, tag,
} from '../ui.js';

const state = {
  sort: 'size',
  order: 'desc',
  filters: {},
  // id -> row, not a set of ids. A tick survives paging, and what can be done
  // with a ticked file - read it, or fetch it back from OneDrive first - is
  // decided from the row. Holding ids alone meant the buttons could only see
  // the page you were on: tick forty files across three pages and the bar said
  // forty while the button offered the twelve still on screen. That is the
  // whole reason this is a Map.
  selected: new Map(),
  rows: [],
  total: 0,
  offset: 0,
  pageSize: 50,
};

let pollTimer = null;
// True while any job is running, so the one-pass button cannot start a second.
let jobRunning = false;

/** Anything that changes which files match starts again at the first page. */
function reload() {
  state.offset = 0;
  return refreshTable();
}

export async function render({ params } = {}) {
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
  // The job card goes up before the two list queries. Coming back to this
  // screen while a pass is running used to mean waiting for a summary and a
  // page of rows - both querying a database being actively written - before
  // anything said that something was happening at all.
  await pollJob();
  await Promise.all([refreshSummary(), refreshTable()]);

  // Home links here with ?start=1 to open the dialog straight away, so a first
  // run is one click from the dashboard rather than two.
  if (params && params.get && params.get('start') && !jobRunning) openFullPass();

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
    el('div', { class: 'card mb-5' },
      el('h2', { class: 'card__title mt-0' }, 'Everything, in one go'),
      el('button', {
        class: 'btn btn--primary btn--big btn--block',
        type: 'button',
        disabled: jobRunning ? true : null,
        onclick: openFullPass,
      }, 'Find every Outlook file and read it into the archive'),
      jobRunning
        ? el('p', { class: 'muted small mt-3 mb-0' },
            'Something is running below. This can start again once it has finished.')
        : el('p', { class: 'muted small mt-3 mb-0' },
            'Recall searches the drives you choose, brings down anything stored ' +
            'in the cloud only, and reads the lot into the archive. You will be ' +
            'shown exactly what that costs before anything starts, and you can ' +
            'stop at any time.'),
    ),

    el('details', { class: 'mb-5' },
      el('summary', {}, 'Do it step by step instead'),
      el('div', { class: 'btn-row mt-3' },
        el('button', {
          class: 'btn btn--big',
          type: 'button',
          onclick: openDriveChooser,
        }, 'Find Outlook files on this computer'),
        el('button', {
          class: 'btn btn--big',
          type: 'button',
          onclick: () => openReadDialog(null),
        }, 'Read them into the archive'),
      ),
    ),

    el('div', { class: 'btn-row mb-5' },
      el('button', {
        class: 'btn',
        type: 'button',
        onclick: async () => { await refreshSummary(); await reload(); },
      }, 'Refresh this list'),
    ),

    el('div', { class: 'toolbar' },
      field('Search the list', el('input', {
        type: 'search', id: 'filter-search', placeholder: 'part of a file name or folder',
        oninput: debounce(() => {
          state.filters.search = document.getElementById('filter-search').value.trim();
          reload();
        }),
      })),
      field('Show', el('select', {
        id: 'filter-kind',
        onchange: () => {
          const v = document.getElementById('filter-kind').value;
          state.filters.only_duplicates = v === 'duplicates';
          state.filters.only_placeholders = v === 'cloud';
          state.filters.only_copied = v === 'copied';
          state.filters.only_problems = v === 'problems';
          state.filters.state = v === 'unread' ? 'pending' : (v === 'read' ? 'done' : '');
          reload();
        },
      },
        el('option', { value: '' }, 'Everything'),
        el('option', { value: 'unread' }, 'Not read yet'),
        el('option', { value: 'read' }, 'Already read'),
        el('option', { value: 'duplicates' }, 'Duplicates only'),
        el('option', { value: 'cloud' }, 'Cloud-only files'),
        el('option', { value: 'copied' }, 'Files Recall has its own copy of'),
        el('option', { value: 'problems' }, 'Files with problems'),
      )),
      field('Where it is', el('select', {
        id: 'filter-container',
        onchange: () => {
          state.filters.container = document.getElementById('filter-container').value;
          reload();
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

// --- find, download and read, in one pass ---------------------------------
//
// One dialog, shown once, carrying the whole cost: what comes down from the
// cloud, what that needs in disk, and what will be read. Everything after the
// button is unattended. The cost is still shown before a byte moves - that
// rule has not changed, only the number of times the user has to agree to it.

async function openFullPass() {
  let plan;
  try {
    plan = await api.readAllPlan();
  } catch (err) {
    errorDialog(err);
    return;
  }

  const d = plan.disk;
  const willDownload = plan.download.count > 0;

  const skipCloud = el('input', { type: 'checkbox' });
  const sample = el('input', { type: 'checkbox' });
  const confirm = el('button', { class: 'btn btn--primary', type: 'button' });

  // One rule, and only one: there is no room. Nothing else stops the pass -
  // not a locked file, not a missing Outlook, not an empty list. An empty list
  // is precisely what this is for.
  const blocked = () => !skipCloud.checked && willDownload && !d.fits;

  function syncConfirm() {
    confirm.disabled = blocked();
    if (sample.checked) confirm.textContent = 'Start — read a sample of each file';
    else if (skipCloud.checked || !willDownload) {
      confirm.textContent = 'Start — search and read';
    } else {
      confirm.textContent = `Start — download ${bytes(plan.download.bytes)} and read everything`;
    }
  }
  skipCloud.onchange = syncConfirm;
  sample.onchange = syncConfirm;

  const body = [];

  if (willDownload && !d.fits) {
    body.push(notice('error', 'Not enough room on this drive',
      `Bringing these down needs about ${bytes(d.needed_bytes)} of room while it ` +
      `works, and there is ${bytes(d.free_bytes)} free. Free up some space, or ` +
      'tick the box below to leave the cloud files where they are this time.'));
  }

  body.push(el('p', {}, plan.sentence));

  if (!plan.scan.last_scan_utc) {
    body.push(notice('info', 'Recall has not searched this computer yet',
      'The numbers below cannot be worked out until it has. Recall searches ' +
      'first, then downloads, then reads.'));
  }

  body.push(
    el('p', { class: 'strong mb-0' }, 'What will happen'),
    el('ol', {},
      el('li', {}, el('strong', {}, 'It searches. '),
        'Names, sizes and dates only — no file is opened.'),
      // Only promised when there is something to download. A step that will
      // not happen is as misleading in a list as a cost that is not real.
      willDownload
        ? el('li', {}, el('strong', {}, 'It downloads. '),
            'Anything stored in the cloud only is copied into Recall’s own ' +
            'folder and kept there, so it never has to be downloaded twice.')
        : null,
      el('li', {}, el('strong', {}, 'It reads. '),
        'Mail, calendar entries, contacts and attachments go into the archive.'),
    ),
  );

  const cards = [
    willDownload
      ? stat('Files to download', num(plan.download.count), bytes(plan.download.bytes))
      : null,
    willDownload
      ? stat('Room needed while it works', bytes(d.needed_bytes),
          d.doubles_up ? 'about twice the download size' : null,
          { alarming: !d.fits })
      : null,
    willDownload ? stat('Room free', bytes(d.free_bytes)) : null,
    stat('Files to read', num(plan.read.count), bytes(plan.read.bytes)),
  ].filter(Boolean);
  body.push(el('div', { class: willDownload ? 'grid grid--4' : 'grid grid--3' }, ...cards));

  if (willDownload && d.doubles_up) {
    body.push(
      el('p', {},
        'Each cloud file is copied into Recall’s own folder and kept. Windows ' +
        'insists on filling in the OneDrive original on this computer first, and ' +
        'there is no way round that, so for a while both copies are here. That is ' +
        'why the room needed is about twice the download. Nothing is removed from ' +
        'OneDrive, and your own files are never changed.'),
      el('pre', { class: 'raw' }, d.dest_path),
    );
  }

  if (plan.locked.outlook) {
    body.push(notice('warning',
      `${plural(plan.locked.outlook, 'file')} held open by Outlook`,
      plan.locked.outlook_available
        ? 'Windows will not let Recall open these directly, so Recall will ask ' +
          'Outlook to read them instead. Leave Outlook running.'
        : 'Outlook is not installed on this computer, so there is no other way ' +
          'in. These will be skipped, and listed at the end.'));
  }

  if (willDownload) {
    body.push(el('p', {},
      'Downloading uses your internet connection, and may cost money on a ' +
      'metered connection. Nothing is downloaded until you press the button below.'));
  }

  const est = plan.estimate;
  body.push(el('p', {},
    el('strong', {}, 'How long: '),
    willDownload && est.download
      ? `${est.download} to download, then ${est.read} to read. `
      : `${est.read}. `,
    el('span', { class: 'muted' }, est.note)));

  if (willDownload) {
    body.push(el('label', { class: 'check' }, skipCloud,
      el('span', {},
        'Leave the cloud files where they are this time',
        el('span', { class: 'check__note' },
          'They stay on the list and stay in the cloud. You can come back to them.'))));
  }

  body.push(el('label', { class: 'check' }, sample,
    el('span', {},
      'Read only the first 50 records of each file',
      el('span', { class: 'check__note' },
        'A quick way to check everything reads correctly before committing to a ' +
        'long run.'))));

  if (willDownload && plan.download.files.length) {
    body.push(el('details', {},
      el('summary', {},
        plan.download.listed < plan.download.count
          ? `The ${num(plan.download.listed)} largest of ${num(plan.download.count)} files to download`
          : `The ${plural(plan.download.count, 'file')} to download`),
      el('pre', { class: 'raw' },
        plan.download.files
          .map((f) => `${bytes(f.size_bytes).padStart(10)}  ${f.path}`)
          .join('\n')),
    ));
  }

  body.push(el('p', { class: 'muted mb-0' }, plan.scan.note));

  const dialog = modal({
    title: 'Read everything Outlook left on this computer?',
    body: el('div', { class: 'stack' }, ...body),
    actions: [
      confirm,
      el('button', { class: 'btn', type: 'button', onclick: () => dialog.close() },
        'Not now'),
    ],
  });

  confirm.onclick = async () => {
    dialog.close();
    try {
      await api.readAll({
        skip_download: skipCloud.checked,
        sample: sample.checked ? 50 : 0,
      });
    } catch (err) {
      if (err.status === 409) {
        const busy = modal({
          title: 'Something is already running',
          body: el('p', {},
            'Recall does one of these at a time. You can watch it below, or stop ' +
            'it first and start again.'),
          actions: [
            el('button', {
              class: 'btn btn--primary', type: 'button',
              onclick: () => { busy.close(); pollJob(); },
            }, 'Show me'),
          ],
        });
        return;
      }
      errorDialog(err);
      return;
    }
    renderControls();
    pollJob();
  };

  syncConfirm();
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

  // Good enough to choose a word with. A path ending in a known extension is a
  // file; anything else is treated as a folder, and being wrong here costs a
  // slightly odd sentence rather than anything that matters.
  const looksLikeAFile = (path) => /\.[A-Za-z0-9]{1,5}$/.test(String(path));

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
        el('span', { class: 'check__note' },
          looksLikeAFile(path)
            ? 'One file you chose just now.'
            : 'A folder you chose just now.'),
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
        onclick: () => openFolderPicker((paths) => paths.forEach(addFolder)),
      }, 'Choose specific folders or files…'),
    ),
    el('p', { class: 'field__help' },
      'If you already know where your old mail is, pointing Recall straight at ' +
      'it takes seconds instead of an hour. You can pick as many folders and ' +
      'files as you like, from anywhere on the computer.'),
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
      }, 'Start reading'),
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

  // Re-draw the controls only when a job starts or stops. Doing it every tick
  // would rebuild the filter box under the user's cursor once a second.
  if (jobRunning !== (job.state === 'running')) {
    jobRunning = job.state === 'running';
    renderControls();
  }

  if (job.state === 'running') {
    const stop = el('button', {
      class: 'btn btn--danger', type: 'button',
      onclick: async (e) => {
        // Without this the button stays live and unchanged, and a user who
        // sees nothing happen presses it four more times.
        e.target.disabled = true;
        e.target.textContent = 'Stopping…';
        try { await api.cancelJob(); } catch (err) { errorDialog(err); }
      },
    }, stopLabel(job.phase));

    host.append(el('div', { class: 'card' },
      el('h2', { class: 'card__title mt-0' }, jobTitle(job.kind)),
      job.phase_count ? phaseStrip(job) : null,
      el('p', {}, job.message),
      progressBar(job),
      el('div', { class: 'btn-row' }, stop),
      el('p', { class: 'muted small mb-0' }, stopNote(job.phase)),
    ));
    pollTimer = setTimeout(pollJob, 1000);
    return;
  }

  if ((job.state === 'done' || job.state === 'canceled') && job.kind === 'read_all') {
    host.append(passResult(job));
    await refreshSummary();
    await reload();
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
    await reload();
    return;
  }

  if (job.state === 'canceled') {
    host.append(notice('warning', 'Stopped', el('p', {}, job.message)));
    await refreshSummary();
    await reload();
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
    await reload();
  }
}

function jobTitle(kind) {
  return ({
    scan: 'Looking for Outlook files',
    extract: 'Reading your Outlook files',
    hydrate: 'Downloading from OneDrive',
    index: 'Building the search index',
    read_all: 'Finding, downloading and reading everything',
  })[kind] || 'Work';
}

/** What the whole pass did, once it has stopped for any reason.
 *
 * A table rather than a run of paragraphs, because there are seven numbers and
 * seven sentences is a wall. Rows worth nothing are left out entirely - a
 * printed zero invites the question "why is it zero".
 */
function passResult(job) {
  const d = job.detail || {};
  const stopped = d.stopped_for;
  const canceled = job.state === 'canceled';

  const rows = [
    ['Files found by the search', num(d.files_found)],
    d.files_copied
      ? ['Files downloaded and kept here',
         `${num(d.files_copied)} — ${bytes(d.bytes_copied)}`]
      : null,
    ['Files read', num(d.files_read)],
    ['Records saved', num(d.items_written)],
    d.duplicates_collapsed
      ? ['Records already in the archive, not added again',
         num(d.duplicates_collapsed)]
      : null,
    d.attachments_written
      ? ['Attachments saved', num(d.attachments_written)] : null,
  ].filter(Boolean);

  const kind = stopped === 'disk' || stopped === 'workdir' ? 'error'
    : (canceled ? 'warning' : 'good');
  const title = stopped === 'disk' ? 'Stopped: this drive filled up'
    : stopped === 'workdir' ? 'Stopped: the working folder is inside OneDrive'
    : canceled ? 'Stopped' : 'Finished';

  const body = [
    el('p', {}, job.message),
    el('div', { class: 'table-wrap' },
      el('table', { class: 'table--keyvalue' },
        el('tbody', {}, ...rows.map(([label, value]) => el('tr', {},
          el('th', {}, label),
          el('td', { class: 'num' }, value),
        ))))),
  ];

  if (canceled || stopped) {
    body.push(el('p', {},
      'Nothing is lost. Running it again carries on from here — files already ' +
      'downloaded are not downloaded twice, and records already saved are not ' +
      'saved twice.'));
  }
  if (d.skipped_download) {
    body.push(el('p', { class: 'qualified-note' },
      'The cloud-only files were left where they are, because you asked for ' +
      'that. They are still on the list.'));
  }
  if (d.downloads_failed) {
    body.push(el('p', { class: 'qualified-note' },
      `${plural(d.downloads_failed, 'file')} could not be downloaded. They are ` +
      'listed below, and they stay on the list to try again.'));
  }
  if (d.locked_attempted) {
    body.push(el('p', {},
      `${plural(d.locked_attempted, 'file')} were being held open by Outlook, ` +
      'so Outlook was asked to read them. Those cannot be compared against ' +
      'other files for duplicates, because comparing needs the same lock.'));
  }
  if (d.failures && d.failures.length) {
    body.push(el('details', {},
      el('summary', {}, `${plural(d.failures.length, 'file')} did not work out`),
      el('pre', { class: 'raw' },
        d.failures.map(([path, reason]) => `${path}\n    ${reason}`).join('\n\n'))));
  }

  // Always, not only when something went wrong. A total is only honest if the
  // thing that qualifies it is as easy to find as the number itself.
  body.push(
    el('p', { class: 'qualified-note' },
      'These count what Recall could read. Where a file was only partly ' +
      'readable, the Problems screen says how much is thought to be missing — ' +
      'those numbers are never folded into the totals above.'),
    el('div', { class: 'btn-row' },
      d.items_written
        ? el('a', { class: 'btn btn--primary', href: '#/search' }, 'Search the archive')
        : null,
      el('a', { class: 'btn', href: '#/problems' }, 'See what could not be read'),
      stopped === 'disk'
        ? el('button', { class: 'btn', type: 'button', onclick: openFullPass },
            'Try again from here')
        : null,
    ),
  );

  return notice(kind, title, ...body);
}

const PHASE_NAMES = {
  scanning: 'Searching your drives',
  downloading: 'Downloading from the cloud',
  comparing: 'Looking for identical copies',
  reading: 'Reading into the archive',
};
const PHASE_ORDER = ['scanning', 'downloading', 'comparing', 'reading'];

/** All four steps, always listed.
 *
 * A bare "Step 2 of 4" tells you where you are but not what is still coming,
 * and on a pass that runs for hours the next thing that will happen is the
 * question people actually have. State is a word as well as a shape, so it
 * does not depend on noticing a colour.
 */
function phaseStrip(job) {
  const at = PHASE_ORDER.indexOf(job.phase);
  // A step that was not needed - nothing to download, nothing new to compare -
  // must not read as "finished". Position alone cannot tell the two apart, so
  // the job says which steps it actually entered.
  const ran = new Set((job.detail && job.detail.phases_entered) || PHASE_ORDER);

  return el('ol', { class: 'steps', 'aria-live': 'polite' },
    ...PHASE_ORDER.map((name, i) => {
      const now = at === i;
      const past = at > i;
      const skipped = past && !ran.has(name);
      const done = past && !skipped;
      return el('li', {
        class: 'steps__item'
          + (now ? ' is-now' : '')
          + (done ? ' is-done' : '')
          + (skipped ? ' is-skipped' : ''),
        'aria-current': now ? 'step' : null,
      },
        el('span', {}, PHASE_NAMES[name]),
        el('span', { class: 'steps__note' },
          now ? 'happening now'
            : done ? 'finished'
            : skipped ? 'nothing to do'
            : 'not started yet'),
      );
    }),
  );
}

function stopLabel(phase) {
  return ({
    scanning: 'Stop, and keep the files found so far',
    downloading: 'Stop after this file, and keep what has come down',
    comparing: 'Stop',
    reading: 'Stop, and keep the records read so far',
  })[phase] || 'Stop, and keep what has been done so far';
}

function stopNote(phase) {
  return ({
    scanning: 'The list keeps everything found up to now.',
    downloading:
      'The file coming down now is not kept — its half-copy is deleted, so ' +
      'nothing broken is left behind. Every file already downloaded stays.',
    reading:
      'Starting again carries on from where it stopped. Nothing is read twice.',
  })[phase] || 'Everything finished so far is kept.';
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
      limit: state.pageSize,
      offset: state.offset,
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

  // Narrowing a filter while on page nine can leave page nine past the end.
  // Step back to the last real page rather than show a blank list under a
  // pager insisting there is more.
  if (!data.rows.length && data.total > 0 && state.offset > 0) {
    state.offset = Math.max(0, (Math.ceil(data.total / state.pageSize) - 1) * state.pageSize);
    return refreshTable();
  }

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
        reload();
      },
      'aria-label': `Sort by ${label}`,
    }, label + (state.sort === key ? (state.order === 'desc' ? '  ↓' : '  ↑') : '')),
  );

  // This ticks everything on *this page*, which is all it has ever done - the
  // label used to claim the whole list.
  const here = data.rows.filter((r) => state.selected.has(r.id)).length;

  const selectAll = el('input', {
    type: 'checkbox',
    checked: here === data.rows.length,
    'aria-label': 'Tick every file on this page',
    title: 'Tick every file on this page',
    onchange: (e) => {
      state.rows.forEach((r) => {
        if (e.target.checked) state.selected.set(r.id, r);
        else state.selected.delete(r.id);
      });
      refreshTable();
    },
  });
  // Part of a page ticked is neither on nor off, and a box showing only those
  // two states says something untrue about the third.
  selectAll.indeterminate = here > 0 && here < data.rows.length;

  add(host,
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
    pager({
      total: data.total,
      offset: state.offset,
      pageSize: state.pageSize,
      unit: 'file',
      onGo: (offset) => {
        state.offset = offset;
        refreshTable();
      },
      onPageSize: (size) => {
        state.pageSize = size;
        state.offset = 0;
        refreshTable();
      },
    }),
  );
  renderSelectionActions();
}

function rowView(r) {
  const box = el('input', {
    type: 'checkbox',
    checked: state.selected.has(r.id),
    'aria-label': `Select ${r.name}`,
    onchange: (e) => {
      if (e.target.checked) state.selected.set(r.id, r);
      else state.selected.delete(r.id);
      renderSelectionActions();
      // Redrawing the whole table for one tick would move the row out from
      // under the pointer, so the two things that depend on it are updated
      // where they stand.
      const tr = e.target.closest('tr');
      if (tr) tr.classList.toggle('is-selected', e.target.checked);
      const all = document.querySelector('.col-check input');
      if (all) {
        const here = state.rows.filter((x) => state.selected.has(x.id)).length;
        all.checked = here === state.rows.length;
        all.indeterminate = here > 0 && here < state.rows.length;
      }
    },
  });

  const conditions = [];
  // "Copy kept by Recall", not "Downloaded": the fact worth noticing is that a
  // second copy now exists on this computer and will stay there. That is the
  // only place the user meets it after the dialog has closed.
  if (r.has_local_copy) conditions.push(tag('Copy kept by Recall', 'good'));
  else if (r.is_placeholder) conditions.push(tag('Cloud only — not downloaded', 'info'));
  // Telling someone to close Outlook is simply untrue once Outlook has read it.
  if (r.read_by_outlook) conditions.push(tag('Outlook read this one', 'good'));
  else if (!r.is_readable) conditions.push(tag('Locked — close Outlook', 'critical'));
  if (r.duplicate_of) conditions.push(tag('Duplicate', 'medium'));
  if (r.parse_state === 'done' && !r.read_by_outlook) conditions.push(tag('Read', 'good'));
  if (r.parse_state === 'failed') conditions.push(tag('Could not be read', 'critical'));
  if (r.parse_state === 'pending' && (r.has_local_copy || (r.is_readable && !r.is_placeholder))) {
    conditions.push(tag('Not read yet', 'plain'));
  }
  if (r.finding_count) conditions.push(severityTag(r.worst_severity));
  if (!conditions.length) conditions.push(tag('Fine', 'good'));

  return el('tr', { class: state.selected.has(r.id) ? 'is-selected' : '' },
    el('td', {}, box),
    el('td', {},
      el('div', { class: 'cell-name' }, r.name),
      el('div', { class: 'cell-path' }, r.folder),
      r.local_copy_path
        ? el('div', { class: 'cell-path' }, `Recall's copy: ${r.local_copy_path}`)
        : null,
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
      'Tick files above to work on just those. The button at the top does ' +
      'everything in one go.'));
    return;
  }

  // Every ticked file, not only the ones on this page. See state.selected.
  const chosen = [...state.selected.values()];
  const cloud = chosen.filter((r) => r.is_placeholder);
  const readable = chosen.filter((r) => !r.is_placeholder && r.is_readable);
  const stuck = chosen.length - cloud.length - readable.length;
  const onThisPage = state.rows.filter((r) => state.selected.has(r.id)).length;

  // el() drops nulls; append() renders them as the word "null", which is
  // exactly what this bar has been showing between its buttons.
  host.append(...[
    el('span', { class: 'strong' },
      onThisPage < n
        ? `${plural(n, 'file')} ticked, ${num(onThisPage)} of them on this page.`
        : `${plural(n, 'file')} ticked.`),
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
    stuck
      ? el('span', { class: 'qualified-note mb-0' },
          `${plural(stuck, 'ticked file')} cannot be read while Outlook holds it open.`)
      : null,
    el('button', {
      class: 'btn', type: 'button',
      onclick: () => { state.selected.clear(); refreshTable(); },
    }, 'Clear the ticks'),
  ].filter(Boolean));
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
