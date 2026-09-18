// Home - the archive at a glance, and the next sensible thing to do.
//
// Every number on this screen is countable from the database. Where a number is
// affected by something Recall could not read, it is shown with the reason
// attached rather than as a clean-looking total (spec 9.5).

import { api } from '../api.js';
import {
  bytes, el, errorNotice, mount, notice, num, plural, setTitle,
} from '../ui.js';

export async function render() {
  setTitle('Home');

  let summary;
  try {
    summary = await api.sourcesSummary();
  } catch (err) {
    mount(errorNotice(err));
    return;
  }

  const c = summary.counts || {};
  const nothingFound = !c.n;
  const nothingRead = !c.parsed;

  const root = el('div', {},
    el('h1', { class: 'page__title' }, 'Your archive'),
    el('p', { class: 'page__lede' },
      'Everything Recall has found and read, on this computer. Nothing is ' +
      'sent anywhere, and your original Outlook files are never changed.'),
  );

  if (nothingFound) {
    root.append(
      notice('info', 'Nothing has been found yet',
        el('p', {},
          'Recall has not searched this computer. The first step is to find ' +
          'out what Outlook files you have — that only reads file names ' +
          'and sizes, and opens nothing.'),
      ),
      el('div', { class: 'btn-row mb-5' },
        el('a', { class: 'btn btn--primary btn--big', href: '#/sources' },
          'Find Outlook files on this computer'),
      ),
      whatHappensNext(),
    );
    mount(root);
    return;
  }

  root.append(
    el('div', { class: 'grid grid--4 mb-5' },
      statCard('Outlook files found', num(c.n), bytes(c.total_bytes) + ' in total'),
      statCard('Files read so far', num(c.parsed),
        c.pending ? `${num(c.pending)} still to read` : 'all of them'),
      statCard('Duplicate files', num(c.duplicates),
        c.uncompared
          ? `${num(c.uncompared)} not compared yet, so this may rise`
          : 'identical copies of another file',
        Boolean(c.uncompared)),
      statCard('Files with a problem', num((c.unreadable || 0) + (c.failed || 0)),
        c.placeholders ? `${num(c.placeholders)} more are cloud-only` : null,
        Boolean(c.unreadable || c.failed)),
    ),
  );

  if (nothingRead) {
    root.append(
      notice('info', 'Nothing has been read out of those files yet',
        el('p', {},
          `Recall has found ${plural(c.n, 'file')}, and has not opened ` +
          `${c.n === 1 ? 'it' : 'any of them'} yet. Until it does, there is no ` +
          'mail, calendar or contacts in the archive to look at.'),
        el('p', {},
          'Go to "Files found", tick the files you want, and start reading. ' +
          'Reading a sample of 50 records from each file first is a quick way ' +
          'to check everything works before a long run.'),
      ),
    );
  }

  root.append(
    el('h2', {}, 'What to do next'),
    el('div', { class: 'btn-row mb-5' },
      el('a', { class: 'btn btn--primary btn--big', href: '#/sources' },
        nothingRead ? 'Read these files into the archive' : 'See the files found'),
      c.placeholders
        ? el('a', { class: 'btn btn--big', href: '#/sources' },
            `Deal with ${plural(c.placeholders, 'cloud-only file')}`)
        : null,
      (c.unreadable || c.failed)
        ? el('a', { class: 'btn btn--big', href: '#/problems' },
            'Look at what could not be read')
        : null,
    ),
  );

  if (summary.last_scan) {
    const s = summary.last_scan;
    let roots = [];
    try { roots = JSON.parse(s.roots_json || '[]'); } catch { roots = []; }
    root.append(
      el('details', {},
        el('summary', {}, 'The last search of this computer'),
        el('p', {}, `Started ${s.started_utc}, finished ${s.finished_utc || 'not yet'}. ` +
          `State: ${s.state}. Looked at ${num(s.files_seen)} files and found ` +
          `${num(s.candidates_found)} Outlook files.`),
        el('p', {}, 'Places searched:'),
        el('pre', { class: 'raw' }, roots.join('\n') || '(none recorded)'),
      ),
    );
  }

  mount(root);
}

function statCard(label, value, note, qualified = false) {
  return el('div', { class: 'stat' },
    el('div', { class: qualified ? 'stat__value qualified' : 'stat__value' }, value),
    el('div', { class: 'stat__label' }, label),
    note
      ? el('span', { class: qualified ? 'stat__qualifier' : 'check__note' }, note)
      : null,
  );
}

function whatHappensNext() {
  return el('div', { class: 'card' },
    el('h2', { class: 'card__title mt-0' }, 'What Recall is going to do'),
    el('ol', { style: 'max-width:68ch;padding-left:24px' },
      el('li', {}, el('p', {},
        el('strong', {}, 'Find your files. '),
        'Recall searches the drives you tick for anything Outlook made — ' +
        '.pst, .ost, .msg and a dozen older formats. It reads only names, sizes ' +
        'and dates. It opens nothing.')),
      el('li', {}, el('p', {},
        el('strong', {}, 'Show you what it found. '),
        'A list, biggest first, marking duplicates, files stored in the cloud, ' +
        'and files Outlook is holding open.')),
      el('li', {}, el('p', {},
        el('strong', {}, 'Read the ones you choose. '),
        'Mail, calendar entries, contacts and attachments come out into one ' +
        'searchable archive. You can stop at any time and carry on later.')),
      el('li', {}, el('p', {},
        el('strong', {}, 'Tell you what it could not read. '),
        'This matters more than the rest. If a file is damaged, or a year is ' +
        'missing, Recall says so plainly and says how much is affected. It ' +
        'never quietly fills in a gap.')),
    ),
    el('p', { class: 'mb-0 muted' },
      'Your original files are never modified, moved, renamed or deleted at any ' +
      'point. Everything Recall builds goes in its own separate folder.'),
  );
}
