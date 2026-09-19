// The item viewer - one record, in full.
//
// Two things the spec is specific about:
//
//   * a warning strip above the content for any finding touching this item -
//     a guessed encoding, an unknown timezone, a missing date;
//   * a provenance panel: "This item was found in 4 files: ..."
//
// The provenance panel is the visible half of the deduplication bargain. The
// archive stores one copy; it must never let that cost the user the knowledge
// of where the copies were.

import { api } from '../api.js';
import {
  bytes, clear, date, el, errorNotice, mount, notice, num, plural, setTitle, tag,
} from '../ui.js';

export async function render({ segments }) {
  const itemId = Number(segments[0]);
  if (!itemId) {
    mount(notice('error', 'No record was asked for',
      'The address in the bar does not name a record.'));
    return;
  }

  let item;
  try {
    item = await api.item(itemId);
  } catch (err) {
    mount(errorNotice(err));
    return;
  }

  setTitle(item.subject || 'Record');

  const root = el('div', {},
    el('div', { class: 'btn-row mb-3' },
      el('button', {
        class: 'btn', type: 'button', onclick: () => window.history.back(),
      }, 'Back'),
      el('a', { class: 'btn', href: '#/search' }, 'Search again'),
    ),

    // The warning strip goes above the content, not below it, so it is read
    // before the thing it is a warning about.
    warningStrip(item),

    el('h1', { class: 'page__title' }, item.subject || '(no subject)'),
    metaLine(item),
    participants(item),
    eventDetails(item),
    contactCard(item),
    body(item),
    attachments(item),
    threadPanel(item),
    provenance(item),
    rawHeaders(item),
  );
  mount(root);
}

// --- warnings -------------------------------------------------------------

function warningStrip(item) {
  const warnings = [];

  if (!item.occurred_utc) {
    warnings.push([
      'This record has no date',
      'Recall could not find a date for this record, and has not invented one. ' +
      'It is not on the timeline and not in any date range.',
    ]);
  } else if (!item.tz) {
    warnings.push([
      'The timezone of this record is not recorded',
      `The date is right. The time shown, ${item.occurred_local || item.occurred_utc}, ` +
      'may be out by a few hours, and no timezone has been assumed.',
    ]);
  }

  if (item.parse_confidence !== null && item.parse_confidence < 1.0) {
    warnings.push([
      'The text below was worked out, not read',
      `This record did not say what character set it was written in, so Recall ` +
      `worked it out from the bytes (confidence ${Math.round(item.parse_confidence * 100)}%). ` +
      'Accented letters, quotation marks and dashes may be wrong.',
    ]);
  }

  for (const finding of item.findings || []) {
    if (finding.code === 'unknown_timezone' || finding.code === 'no_date'
        || finding.code === 'low_confidence_text') {
      continue;   // already said above, in plainer words
    }
    warnings.push([finding.title, (finding.detail || '').split('\n\n')[0]]);
  }

  if (!warnings.length) return null;

  return el('div', { class: 'notice notice--warning' },
    el('div', { class: 'notice__title' },
      warnings.length === 1
        ? 'One thing to know about this record'
        : `${warnings.length} things to know about this record`),
    ...warnings.map(([title, detail]) => el('div', { class: 'mb-3' },
      el('div', { class: 'strong' }, title),
      el('div', { class: 'small' }, detail),
    )),
  );
}

// --- header ---------------------------------------------------------------

function metaLine(item) {
  const bits = [tag(kindWord(item.kind), 'plain')];

  if (item.occurred_utc) {
    bits.push(el('span', {}, date(item.occurred_utc, { withTime: true })));
    bits.push(el('span', { class: 'muted small' },
      item.tz ? `timezone: ${item.tz}` : 'timezone not recorded'));
  } else {
    bits.push(el('span', { class: 'qualified-note' }, 'no date'));
  }

  if (item.folder_path) {
    bits.push(el('span', { class: 'muted' }, `in ${item.folder_path}`));
  }
  if (item.importance && item.importance !== 'normal') {
    bits.push(tag(`${item.importance} importance`, 'info'));
  }
  for (const name of item.tags || []) bits.push(tag(name, 'info'));

  return el('div', { class: 'row mb-5' }, ...bits);
}

function kindWord(kind) {
  return ({
    message: 'Message', event: 'Calendar entry', contact: 'Contact',
    task: 'Task', note: 'Note',
  })[kind] || kind;
}

// --- people ---------------------------------------------------------------

const ROLE_WORDS = {
  from: 'From', to: 'To', cc: 'Cc', bcc: 'Bcc',
  organizer: 'Organiser', attendee: 'Attending', optional: 'Optional',
  resource: 'Room or resource',
};

function participants(item) {
  if (!item.participants.length) return null;

  const byRole = new Map();
  for (const p of item.participants) {
    if (!byRole.has(p.role)) byRole.set(p.role, []);
    byRole.get(p.role).push(p);
  }

  const rows = [];
  for (const [role, people] of byRole) {
    rows.push(el('tr', {},
      el('th', {},
        ROLE_WORDS[role] || role),
      el('td', {}, ...people.flatMap((p, i) => [
        i ? el('span', {}, ', ') : null,
        personLink(p),
      ]).filter(Boolean)),
    ));
  }

  return el('div', { class: 'card' },
    el('table', { class: 'table--keyvalue' }, el('tbody', {}, ...rows)),
  );
}

function personLink(p) {
  const label = p.display_name || p.address || '(unnamed)';
  const node = p.person_id
    ? el('a', { href: `#/people/${p.person_id}` }, label)
    : el('span', {}, label);

  const extras = [];
  if (p.display_name && p.address && p.display_name !== p.address) {
    extras.push(el('span', { class: 'muted small' }, ` <${p.address}>`));
  }
  if (p.address_type === 'ex') {
    // An Exchange DN was never resolved to an address. The raw value stays
    // visible rather than being replaced by something invented.
    extras.push(el('span', { class: 'qualified-note' },
      ' internal company address, never resolved to an email address'));
  }
  if (p.response_status) {
    extras.push(el('span', { class: 'muted small' }, ` (${p.response_status})`));
  }
  return el('span', {}, node, ...extras);
}

// --- kind-specific --------------------------------------------------------

function eventDetails(item) {
  if (item.kind !== 'event') return null;

  const rows = [];
  if (item.location) rows.push(['Where', item.location]);
  if (item.end_utc) rows.push(['Ends', date(item.end_utc, { withTime: true })]);
  if (item.all_day) rows.push(['All day', 'yes']);
  if (item.busy_status) rows.push(['Shown as', item.busy_status]);
  if (item.meeting_status) rows.push(['Meeting', item.meeting_status]);

  if (item.is_recurring_master) {
    const rule = item.recurrence || {};
    rows.push([
      'Repeats',
      rule.rrule_text || (rule.unparsed
        ? 'the repeat rule could not be read — only this occurrence is shown, '
          + 'and no repeats have been invented'
        : JSON.stringify(rule.rrule || rule)),
    ]);
  }

  if (!rows.length) return null;
  return el('div', { class: 'card' },
    el('table', { class: 'table--keyvalue' },
      el('tbody', {}, ...rows.map(([label, value]) => el('tr', {},
        el('th', {}, label),
        el('td', {}, String(value)),
      )))),
  );
}

function contactCard(item) {
  if (item.kind !== 'contact' || !item.contact) return null;
  const card = item.contact;

  const rows = [];
  const add = (label, value) => { if (value) rows.push([label, value]); };

  add('Full name', card.display_name);
  add('Company', card.organization);
  add('Job title', card.title);
  add('Department', card.department);
  add('Email', (card.emails || []).join(', '));
  for (const [slot, number] of Object.entries(card.phones || {})) {
    add(`Telephone (${slot})`, number);
  }
  for (const [slot, address] of Object.entries(card.addresses || {})) {
    const parts = Object.values(address || {}).filter(Boolean);
    if (parts.length) add(`Address (${slot})`, parts.join(', '));
  }
  add('Web', card.web);
  add('Birthday', card.birthday);
  add('Notes', card.notes);

  if (card.recovery_method) {
    rows.push(['How this was recovered', card.recovery_method]);
  }

  if (!rows.length) return null;
  return el('div', { class: 'card' },
    el('h2', { class: 'card__title mt-0' }, 'Contact card'),
    el('table', { class: 'table--keyvalue' },
      el('tbody', {}, ...rows.map(([label, value]) => el('tr', {},
        el('th', {}, label),
        el('td', {}, String(value)),
      )))),
  );
}

// --- body -----------------------------------------------------------------

function body(item) {
  if (!item.body_text && !item.body_html) return null;

  const panel = el('div', { class: 'card' });

  if (item.body_text) {
    panel.append(el('pre', { class: 'prewrap mb-0' }, item.body_text));
  }

  if (item.body_html) {
    // The original HTML is offered as text, never rendered. Rendering mail
    // HTML would execute whatever is in it and fetch whatever it references,
    // and this program makes no network requests.
    panel.append(el('details', { class: 'mt-4' },
      el('summary', {}, 'The original HTML of this message, as text'),
      el('p', { class: 'muted small' },
        'Shown as text rather than as a web page. Displaying it would make ' +
        'your computer fetch whatever it points at, and Recall never goes ' +
        'online.'),
      el('pre', { class: 'raw' }, item.body_html),
    ));
  }

  return panel;
}

// --- attachments ----------------------------------------------------------

function attachments(item) {
  if (!item.attachments.length) return null;

  return el('div', { class: 'card' },
    el('h2', { class: 'card__title mt-0' },
      plural(item.attachments.length, 'attachment')),
    el('div', { class: 'table-wrap' },
      el('table', {},
        el('thead', {}, el('tr', {},
          el('th', {}, 'Name'),
          el('th', {}, 'Kind'),
          el('th', { class: 'num' }, 'Size'),
          el('th', {}, 'Searchable inside?'),
          el('th', {}, ''),
        )),
        el('tbody', {}, ...item.attachments.map((a) => el('tr', {},
          el('td', {},
            el('span', { class: 'cell-name' }, a.filename || '(no name)'),
            a.is_inline
              ? el('div', { class: 'cell-path' }, 'part of the message itself')
              : null,
          ),
          el('td', {}, a.mime_type || ''),
          el('td', { class: 'num' }, bytes(a.size_bytes)),
          el('td', {}, extractWord(a)),
          el('td', {},
            a.content_hash
              ? el('div', { class: 'row' },
                  el('a', {
                    class: 'btn', href: `/api/attachments/${a.id}`, download: a.filename || '',
                  }, 'Save'),
                  a.text_length
                    ? el('button', {
                        class: 'btn', type: 'button',
                        onclick: () => showText(a),
                      }, 'Read the text')
                    : null,
                )
              : el('span', { class: 'qualified-note' }, 'the contents were never read'),
          ),
        ))),
      ),
    ),
  );
}

function extractWord(a) {
  return ({
    done: a.text_length ? 'yes' : 'opened, but there was no text in it',
    unsupported: 'no — not a kind of file with text in it',
    failed: 'no — it could not be read',
    skipped: 'no — too large to look inside',
    pending: 'not looked at yet',
  })[a.extract_state] || a.extract_state;
}

async function showText(a) {
  const { modal } = await import('../ui.js');
  let text = 'Loading...';
  try {
    const response = await fetch(`/api/attachments/${a.id}/text`);
    text = await response.text();
  } catch (err) {
    text = String(err);
  }
  const dialog = modal({
    title: a.filename || 'Attachment',
    body: el('pre', { class: 'raw' }, text),
    actions: [el('button', {
      class: 'btn btn--primary', type: 'button', onclick: () => dialog.close(),
    }, 'Close')],
  });
}

// --- thread ---------------------------------------------------------------

function threadPanel(item) {
  if (!item.thread || item.thread.messages.length < 2) return null;

  return el('div', { class: 'card' },
    el('h2', { class: 'card__title mt-0' },
      `This is part of a conversation of ${plural(item.thread.messages.length, 'message')}`),
    el('ol', {},
      ...item.thread.messages.map((m) => el('li', {},
        m.id === item.id
          ? el('span', { class: 'strong' }, `${m.subject || '(no subject)'} — this one`)
          : el('a', { href: `#/item/${m.id}` }, m.subject || '(no subject)'),
        el('span', { class: 'muted small' },
          ` · ${m.occurred_utc ? date(m.occurred_utc) : 'no date'}`),
      )),
    ),
  );
}

// --- provenance -----------------------------------------------------------

function provenance(item) {
  const sources = item.sources || [];

  return el('div', { class: 'card' },
    el('h2', { class: 'card__title mt-0' },
      sources.length === 1
        ? 'This record was found in one file'
        : `This record was found in ${plural(sources.length, 'file')}`),
    sources.length > 1
      ? el('p', {},
          'The archive keeps one copy of it, and remembers every place it came ' +
          'from. Nothing was thrown away.')
      : null,
    sources.length
      ? el('div', { class: 'table-wrap' },
          el('table', {},
            el('thead', {}, el('tr', {},
              el('th', {}, 'File'),
              el('th', {}, 'Folder inside it'),
              el('th', {}, 'Read with'),
            )),
            el('tbody', {}, ...sources.map((s) => el('tr', {},
              el('td', { class: 'cell-path' }, s.path),
              el('td', {}, s.folder || ''),
              el('td', {}, readerWord(s.parse_backend)),
            ))),
          ),
        )
      : el('p', { class: 'muted mb-0' },
          'No source file is recorded for this record, which should not happen. ' +
          'It is worth reporting.'),
  );
}

function readerWord(backend) {
  return ({
    pypff: 'the built-in Outlook reader',
    com: 'Microsoft Outlook',
    eml: 'as an email file',
    mbox: 'as a mailbox',
    msg: 'as a saved Outlook message',
    olm: 'as a Mac Outlook archive',
    dbx: 'as an Outlook Express folder',
    mbx: 'as an Outlook Express 4 mailbox',
    eudora: 'as a Eudora mailbox',
    icalendar: 'as a calendar file',
    vcalendar: 'as an old calendar file',
    vcard: 'as a contact card',
    'wab-salvage': 'recovered from a Windows Address Book',
  })[backend] || backend || 'unknown';
}

// --- raw ------------------------------------------------------------------

function rawHeaders(item) {
  if (!item.raw_headers) return null;
  return el('details', {},
    el('summary', {}, 'The technical headers of this record'),
    el('p', { class: 'muted small' },
      'Everything the original file recorded about this message, as it was ' +
      'written. Useful if you are sending it to someone for help.'),
    el('pre', { class: 'raw' }, item.raw_headers),
  );
}
