// Home - the archive at a glance, and the next sensible thing to do.
//
// Every total here is rendered through `honest()`, which cannot draw a number
// without also drawing whatever qualifies it. That is the spec 9.5 rule made
// structural rather than remembered.

import { api } from '../api.js';
import {
  add, bytes, date, el, errorNotice, honest, kindLabel, mount, notice, num,
  plural, setTitle,
} from '../ui.js';

export async function render() {
  setTitle('Home');

  let data;
  try {
    data = await api.home();
  } catch (err) {
    mount(errorNotice(err));
    return;
  }

  const root = el('div', {},
    el('h1', { class: 'page__title' }, 'Your archive'),
    el('p', { class: 'page__lede' },
      'Everything Recall has found and read, on this computer. Nothing is ' +
      'sent anywhere, and your original Outlook files are never changed.'),
  );

  if (!data.sources.found) {
    root.append(gettingStarted());
    mount(root);
    return;
  }

  // add(), not append(): four of these return null when there is nothing to
  // show, and append would put the word "null" on the page.
  add(root,
    headline(data),
    kindCards(data),
    undatedPanel(data),
    coveragePanel(data),
    nextSteps(data),
    detailPanel(data),
  );
  mount(root);
}

// --- the headline ---------------------------------------------------------

function headline(data) {
  const span = data.span;

  return el('div', { class: 'card' },
    el('div', { class: 'row baseline' },
      el('span', { class: 'stat__value stat__value--lead' },
        num(data.total.value)),
      el('span', { class: 'stat__label' }, 'records in the archive'),
    ),
    data.total.qualified
      ? el('p', { class: 'qualified-note mt-3' },
          data.total.estimated_missing
            ? `About ${num(data.total.estimated_missing)} more records exist that ` +
              'Recall could not read. The number above is exactly what it could.'
            : (data.total.qualifiers || []).map((q) => q.text).join(' · '))
      : null,
    span.first
      ? el('p', { class: 'mb-0' },
          `From ${date(span.first)} to ${date(span.last)} — `
          + plural(span.years, 'year') + ' of correspondence.')
      : null,
    span.implausible_dates
      ? el('p', { class: 'qualified-note' },
          span.implausible_dates === 1
            ? '1 record carries a date that cannot be right, and is not counted '
              + 'in that span. It is kept exactly as found and is still searchable.'
            : `${num(span.implausible_dates)} records carry a date that cannot be `
              + 'right, and are not counted in that span. They are kept exactly '
              + 'as found and are still searchable.')
      : null,
    data.total.qualified
      ? el('a', { class: 'health__link', href: '#/problems' },
          'What is missing, and why')
      : null,
  );
}

function kindCards(data) {
  if (!data.by_kind.length) return null;
  return el('div', { class: 'grid grid--4 mb-5' },
    ...data.by_kind.map((k) => el('div', { class: 'stat' },
      el('div', { class: 'stat__value' }, honest(k, { compact: true })),
      el('div', { class: 'stat__label' }, kindLabel(k.kind)),
    )),
  );
}

function undatedPanel(data) {
  if (!data.undated) return null;
  return notice('warning', `${plural(data.undated, 'record')} with no date at all`,
    el('p', {},
      'These are not on the timeline and not in any date range. No date has ' +
      'been invented for them — they are kept exactly as they were found.'),
    el('a', { class: 'btn', href: '#/search?undated=1' },
      'See the undated records'),
  );
}

function coveragePanel(data) {
  const coverage = data.coverage || {};
  if (!coverage.months) return null;

  const gaps = Number(coverage.gap_months || 0);
  const explained = Number(coverage.explained || 0);
  const unexplained = Math.max(0, gaps - explained);

  if (!gaps) {
    return notice('good', 'Every month is accounted for',
      `All ${plural(coverage.months, 'month')} between the first record and the ` +
      'last have something in them.');
  }

  return notice(unexplained ? 'warning' : 'info',
    `${plural(gaps, 'month')} with nothing in them`,
    el('p', {},
      unexplained
        ? `${num(unexplained)} of them ${unexplained === 1 ? 'has' : 'have'} not ` +
          'been explained yet. A gap can be perfectly real — a quiet year, a job ' +
          'change — or it can mean a mailbox has not been found. Recall ' +
          'cannot tell which, and will not guess.'
        : 'You have explained all of them. They stay on the timeline, with your ' +
          'notes, permanently.'),
    el('a', { class: 'btn', href: '#/problems' },
      'See the coverage map'),
  );
}

// --- what to do next ------------------------------------------------------

function nextSteps(data) {
  if (!data.next_steps.length) return null;

  return el('div', {},
    el('h2', {}, 'What to do next'),
    el('div', { class: 'grid grid--3 mb-5' },
      ...data.next_steps.slice(0, 3).map((step) => el('div', { class: 'card' },
        el('a', {
          class: step.primary ? 'btn btn--primary btn--block' : 'btn btn--block',
          href: step.href,
        }, step.label),
        el('p', { class: 'muted small mt-3 mb-0' },
          step.why),
      )),
    ),
    data.next_steps.length > 3
      ? el('div', { class: 'btn-row mb-5' },
          ...data.next_steps.slice(3).map((step) =>
            el('a', { class: 'btn', href: step.href }, step.label)),
        )
      : null,
  );
}

// --- the detail -----------------------------------------------------------

function detailPanel(data) {
  const s = data.sources;
  const a = data.attachments;

  const rows = [
    ['Files found on this computer', num(s.found)],
    ['Files read into the archive', num(s.read_ok)],
    ['Files not read yet', num(s.pending)],
    ['Files that could not be read', num(s.failed)],
    ['Files that are duplicates of another', num(s.duplicates)],
    ['Files stored in the cloud only', num(s.cloud_only)],
    ['Cloud files Recall has copied here', num(s.cloud_copied)],
    ['Total size of those files', bytes(s.total_bytes)],
    ['People in the archive', num(data.people)],
    ['Attachments kept', num(a.n)],
    ['Of which are distinct files', num(a.distinct_files)],
    ['Space used by the archive', bytes(data.storage.archive_bytes)],
    ['Space used by attachments', bytes(data.storage.blobs_bytes)],
  ];

  return el('div', {},
    el('h2', {}, 'The detail'),
    !data.index.complete
      ? notice('warning', 'The search index is not complete',
          el('p', {}, data.index.note),
          el('p', {},
            'Until it is, searching will not find those records.'),
          // There is a button for this. Telling somebody to type a command in
          // a black window is not an instruction a non-programmer can follow.
          el('div', { class: 'btn-row' },
            el('button', {
              class: 'btn btn--primary', type: 'button',
              onclick: (e) => buildIndex(e.target),
            }, 'Finish the search index now')))
      : null,
    el('div', { class: 'card' },
      el('div', { class: 'table-wrap' },
        el('table', { class: 'table--keyvalue' },
          el('tbody', {}, ...rows.map(([label, value]) => el('tr', {},
            el('th', {}, label),
            el('td', { class: 'num' }, value),
          ))),
        ),
      ),
      el('p', { class: 'muted small mb-0 mt-3' },
        'Everything Recall builds lives in:'),
      el('pre', { class: 'raw' }, data.storage.workdir),
      el('p', { class: 'muted small mb-0' },
        'Your original Outlook files are not in there, and are never changed.'),
    ),
  );
}

// --- before anything has been found ---------------------------------------

function gettingStarted() {
  return el('div', {},
    notice('info', 'Nothing has been found yet',
      el('p', {},
        'Recall has not searched this computer. The first step is to find out ' +
        'what Outlook files you have — that reads file names and sizes ' +
        'only, and opens nothing.'),
    ),
    el('div', { class: 'btn-row mb-5' },
      el('a', { class: 'btn btn--primary btn--big', href: '#/sources?start=1' },
        'Find every Outlook file and read it into the archive'),
    ),
    el('div', { class: 'card' },
      el('h2', { class: 'card__title mt-0' }, 'What Recall is going to do'),
      el('ol', {},
        step('Find your files.',
          'Recall searches the drives you tick for anything Outlook made — ' +
          '.pst, .ost, .msg and a dozen older formats. It reads only names, ' +
          'sizes and dates. It opens nothing.'),
        step('Bring down anything stored in the cloud only.',
          'Those files look like files but their contents are not on this ' +
          'computer. Recall copies each one into its own folder and keeps it ' +
          'there, so it is readable now and re-readable later without ' +
          'downloading again. Your OneDrive copy is not touched, and you are ' +
          'shown the total size before anything is downloaded.'),
        step('Read them.',
          'Mail, calendar entries, contacts and attachments come out into one ' +
          'searchable archive. You can stop at any time and carry on later.'),
        step('Tell you what it could not read.',
          'This matters more than the rest. If a file is damaged, or a year is ' +
          'missing, Recall says so plainly and says how much is affected. It ' +
          'never quietly fills in a gap.'),
      ),
      el('p', { class: 'mb-0 muted' },
        'Your original files are never modified, moved, renamed or deleted at ' +
        'any point. Everything Recall builds goes in its own separate folder.'),
    ),
  );
}

function step(title, body) {
  return el('li', {}, el('p', {}, el('strong', {}, title + ' '), body));
}


/** Build the rest of the search index, and say what happened either way. */
async function buildIndex(button) {
  const previous = button.textContent;
  button.disabled = true;
  button.textContent = 'Working…';
  try {
    await api.buildIndex();
    button.replaceWith(el('span', { class: 'strong' },
      'Done. Everything is searchable now.'));
    await render();
  } catch (err) {
    button.disabled = false;
    button.textContent = previous;
    button.after(errorNotice(err));
  }
}
