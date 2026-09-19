// People - everyone in the archive, and the merge review queue.
//
// Two rules from the spec govern this screen:
//
//   * A merge is proposed, never applied. Every suggestion shows its evidence,
//     including the reasons to doubt it, and needs a click.
//   * An address flagged over_merged_risk "must never appear as a top
//     correspondent without the warning attached". So the warning is rendered
//     from the same row as the name, not fetched separately - there is no code
//     path that draws one without the other.

import { api } from '../api.js';
import {
  clear, date, dateRange, debounce, el, empty, errorDialog, errorNotice,
  loading, modal, mount, notice, num, plural, setTitle, stat, tag,
} from '../ui.js';

const SVG_NS = 'http://www.w3.org/2000/svg';

const state = { sort: 'items', order: 'desc', search: '', includeSelf: true };

export async function render({ segments }) {
  if (segments.length && /^\d+$/.test(segments[0])) {
    return renderProfile(Number(segments[0]));
  }
  return renderList();
}

// --- the list -------------------------------------------------------------

async function renderList() {
  setTitle('People');

  const root = el('div', {},
    el('h1', { class: 'page__title' }, 'People'),
    el('p', { class: 'page__lede' },
      'Everyone who appears anywhere in the archive. Click a name to see ' +
      'their whole correspondence.'),
    el('div', { id: 'merge-queue' }),
    el('div', { class: 'toolbar' },
      el('div', { class: 'field' },
        el('label', { for: 'people-search' }, 'Search for a person'),
        el('input', {
          type: 'search', id: 'people-search',
          placeholder: 'a name or an email address',
          oninput: debounce(() => {
            state.search = document.getElementById('people-search').value.trim();
            loadList();
          }),
        })),
      el('div', { class: 'field' },
        el('label', { for: 'people-sort' }, 'Order by'),
        el('select', {
          id: 'people-sort',
          onchange: () => {
            state.sort = document.getElementById('people-sort').value;
            loadList();
          },
        },
          el('option', { value: 'items' }, 'How much they appear'),
          el('option', { value: 'name' }, 'Name'),
          el('option', { value: 'first' }, 'First contact'),
          el('option', { value: 'last' }, 'Last contact'),
          el('option', { value: 'org' }, 'Organisation'),
        )),
      el('label', { class: 'check' },
        el('input', {
          type: 'checkbox', checked: true,
          onchange: (e) => { state.includeSelf = e.target.checked; loadList(); },
        }),
        el('span', {}, 'Include your own addresses'),
      ),
    ),
    el('div', { id: 'people-list' }, loading('Loading people')),
  );
  mount(root);

  await Promise.all([loadMergeQueue(), loadList()]);
}

async function loadList() {
  const host = document.getElementById('people-list');
  if (!host) return;

  let data;
  try {
    data = await api.people({
      sort: state.sort,
      order: state.order,
      search: state.search,
      include_self: state.includeSelf,
    });
  } catch (err) {
    clear(host);
    host.append(errorNotice(err));
    return;
  }

  clear(host);

  if (!data.rows.length) {
    host.append(empty(
      state.search ? 'Nobody matches that' : 'Nobody is in the archive yet',
      state.search
        ? 'Try part of a name or an email address.'
        : 'Read some files into the archive first, on the "Files found" screen.',
    ));
    return;
  }

  host.append(
    el('p', { class: 'muted' },
      data.total > data.rows.length
        ? `Showing the first ${num(data.rows.length)} of ${num(data.total)} people.`
        : `${plural(data.total, 'person', 'people')}.`),
    el('div', { class: 'table-wrap' },
      el('table', {},
        el('thead', {}, el('tr', {},
          el('th', {}, 'Name'),
          el('th', {}, 'Organisation'),
          el('th', {}, 'Addresses'),
          el('th', {}, 'First contact'),
          el('th', {}, 'Last contact'),
          el('th', { class: 'num' }, 'Records'),
        )),
        el('tbody', {}, ...data.rows.map(personRow)),
      ),
    ),
  );
}

function personRow(p) {
  return el('tr', {},
    el('td', {},
      el('a', { class: 'cell-name', href: `#/people/${p.id}` },
        p.display_name || '(no name)'),
      p.is_self ? el('div', {}, tag('You', 'info')) : null,
      // The warning is built from the same row as the name. There is no way to
      // render this person without it.
      p.over_merged_risk
        ? el('div', { class: 'qualified-note' },
            'This address is used by more than one person — these figures ' +
            'are for a group, not an individual')
        : null,
      p.merged_count
        ? el('div', { class: 'cell-path' },
            `${plural(p.merged_count, 'other entry', 'other entries')} merged into this one`)
        : null,
    ),
    el('td', {}, p.org || ''),
    el('td', {},
      el('div', { class: 'cell-path' }, p.addresses.slice(0, 3).join(', ')),
      p.addresses.length > 3
        ? el('div', { class: 'cell-path' }, `and ${p.addresses.length - 3} more`)
        : null,
    ),
    el('td', { class: 'nowrap' }, date(p.first_seen_utc)),
    el('td', { class: 'nowrap' }, date(p.last_seen_utc)),
    el('td', { class: 'num' }, num(p.item_count)),
  );
}

// --- the merge review queue ----------------------------------------------

async function loadMergeQueue() {
  const host = document.getElementById('merge-queue');
  if (!host) return;

  let data;
  try {
    data = await api.mergeQueue();
  } catch (err) {
    clear(host);
    host.append(errorNotice(err));
    return;
  }

  clear(host);
  if (!data.proposals.length) return;

  const list = el('div', { class: 'stack' });
  for (const proposal of data.proposals) list.append(proposalCard(proposal));

  host.append(el('div', { class: 'card' },
    el('h2', { class: 'card__title mt-0' },
      `${plural(data.proposals.length, 'person', 'people')} may be listed twice`),
    el('p', {}, data.note),
    list,
  ));
}

function proposalCard(proposal) {
  const { a, b, evidence } = proposal;

  const card = el('div', { class: 'notice notice--info' },
    el('div', { class: 'notice__title' },
      `${a.display_name || '(no name)'} and ${b.display_name || '(no name)'}`),
    el('p', {}, proposal.reason + '.'),

    el('div', { class: 'grid grid--3 mb-3' },
      sideBySide(a),
      sideBySide(b),
    ),

    evidence.caution
      ? el('p', { class: 'qualified-note' }, evidence.caution)
      : null,

    el('details', {},
      el('summary', {}, 'The evidence behind this suggestion'),
      el('pre', { class: 'raw' }, JSON.stringify(evidence, null, 2)),
    ),

    el('div', { class: 'btn-row' },
      el('button', {
        class: 'btn btn--primary', type: 'button',
        onclick: () => confirmMerge(proposal),
      }, 'Yes, they are the same person'),
      el('button', {
        class: 'btn', type: 'button',
        onclick: () => dismissProposal(proposal, card),
      }, 'No, they are different people'),
      // Labelled by what actually differs between the two. When the suggestion
      // is "these two identical names are one person", two buttons both reading
      // "Look at Margaret" tell the user nothing about which is which.
      el('a', { class: 'btn', href: `#/people/${a.id}` }, `Look at ${distinguish(a, b)}`),
      el('a', { class: 'btn', href: `#/people/${b.id}` }, `Look at ${distinguish(b, a)}`),
    ),
  );
  return card;
}

function distinguish(person, other) {
  const name = (person.display_name || '').trim();
  const otherName = (other.display_name || '').trim();
  if (name && name.toLowerCase() !== otherName.toLowerCase()) {
    return name.split(/\s+/)[0];
  }
  return person.addresses[0] || `entry ${person.id}`;
}

function sideBySide(p) {
  return el('div', { class: 'stat' },
    el('div', { class: 'strong' }, p.display_name || '(no name)'),
    p.org ? el('div', { class: 'muted' }, p.org) : null,
    el('div', { class: 'cell-path' }, p.addresses.join(', ') || 'no address'),
    el('div', { class: 'muted small' },
      `${plural(p.item_count, 'record')} · ${dateRange(p.first_seen_utc, p.last_seen_utc)}`),
  );
}

async function confirmMerge(proposal) {
  const keep = proposal.suggested_keep === proposal.a.id ? proposal.a : proposal.b;
  const merge = proposal.suggested_keep === proposal.a.id ? proposal.b : proposal.a;

  const keepFirst = el('input', {
    type: 'radio', name: 'keep', value: String(keep.id), checked: true,
  });
  const keepSecond = el('input', { type: 'radio', name: 'keep', value: String(merge.id) });

  const dialog = modal({
    title: 'Which name should be kept?',
    body: el('div', {},
      el('p', {},
        'Both entries and all their addresses will be joined together. ' +
        'Nothing is deleted, and this can be undone at any time.'),
      el('label', { class: 'check' }, keepFirst,
        el('span', {},
          el('strong', {}, keep.display_name || '(no name)'),
          el('span', { class: 'check__note' },
            `${plural(keep.item_count, 'record')} · ${keep.addresses.join(', ')}`))),
      el('label', { class: 'check' }, keepSecond,
        el('span', {},
          el('strong', {}, merge.display_name || '(no name)'),
          el('span', { class: 'check__note' },
            `${plural(merge.item_count, 'record')} · ${merge.addresses.join(', ')}`))),
    ),
    actions: [
      el('button', {
        class: 'btn btn--primary', type: 'button',
        onclick: async () => {
          const keepId = keepFirst.checked ? keep.id : merge.id;
          const mergeId = keepFirst.checked ? merge.id : keep.id;
          dialog.close();
          try {
            await api.mergePeople(keepId, mergeId);
            await Promise.all([loadMergeQueue(), loadList()]);
          } catch (err) {
            errorDialog(err);
          }
        },
      }, 'Join them together'),
      el('button', { class: 'btn', type: 'button', onclick: () => dialog.close() },
        'Cancel'),
    ],
  });
}

async function dismissProposal(proposal, card) {
  // "These are different people" is a decision about a finding, so it is
  // recorded as one: won't-fix, with a note, and the row is kept forever.
  const findingId = proposal.evidence.finding_id;
  card.replaceWith(notice('good', 'Noted',
    'They will stay as two separate people.'
    + (findingId ? '' : ' This will be suggested again after the next run unless '
       + 'you mark it won’t-fix on the Problems screen.')));
  if (findingId) {
    try {
      await api.setFindingState(findingId, 'wont_fix', 'The user says these are different people');
    } catch { /* the card is already gone; the state is cosmetic here */ }
  }
}

// --- one person -----------------------------------------------------------

async function renderProfile(personId) {
  let p;
  try {
    p = await api.person(personId);
  } catch (err) {
    mount(errorNotice(err));
    return;
  }

  setTitle(p.display_name || 'Person');

  const root = el('div', {},
    el('a', { class: 'btn mb-3', href: '#/people' }, 'Back to everyone'),
    el('h1', { class: 'page__title' }, p.display_name || '(no name)'),
    p.org ? el('p', { class: 'page__lede' }, p.org + (p.role ? ` · ${p.role}` : '')) : null,
  );

  if (p.over_merged_risk) {
    root.append(notice('warning', 'This is not one person',
      el('p', {},
        'This address is used by more than one person — a reception desk, ' +
        'an accounts department, or a shared mailbox. Everything below counts ' +
        'the whole group together.'),
      el('p', { class: 'mb-0' },
        'Recall will not split it up: there is no way to tell from the mail ' +
        'which message was written by which person, and guessing would put ' +
        'words in somebody’s mouth.'),
    ));
  }

  if (p.is_self) {
    root.append(notice('info', 'This is you',
      'Recall knows this is one of your own addresses, from the list in config.toml.'));
  }

  root.append(
    el('div', { class: 'grid grid--4 mb-5' },
      stat('Records', num(p.item_count)),
      stat('First contact', date(p.first_seen_utc)),
      stat('Last contact', date(p.last_seen_utc)),
      stat('Addresses', num(p.address_count)),
    ),
  );

  if (p.undated_count) {
    root.append(notice('warning',
      `${plural(p.undated_count, 'record')} with no date`,
      'These are not on the graph below, and no date has been guessed for them.'));
  }

  if (p.per_year.length) {
    root.append(el('div', { class: 'card' },
      el('h2', { class: 'card__title mt-0' }, 'Correspondence over time'),
      sparkline(p.per_year),
    ));
  }

  root.append(identitiesPanel(p));

  if (p.top_correspondents.length) {
    root.append(el('div', { class: 'card' },
      el('h2', { class: 'card__title mt-0' }, 'Who else was on these records'),
      el('div', { class: 'table-wrap' },
        el('table', {},
          el('thead', {}, el('tr', {},
            el('th', {}, 'Person'),
            el('th', { class: 'num' }, 'Records in common'),
          )),
          el('tbody', {}, ...p.top_correspondents.map((c) => el('tr', {},
            el('td', {},
              el('a', { href: `#/people/${c.id}` }, c.display_name || '(no name)'),
              // Again: the warning is emitted from the same place as the name.
              c.over_merged_risk
                ? el('div', { class: 'qualified-note' },
                    'a shared address, not one person')
                : null,
            ),
            el('td', { class: 'num' }, num(c.shared)),
          ))),
        ),
      ),
    ));
  }

  if (p.merged_people.length) {
    root.append(el('div', { class: 'card' },
      el('h2', { class: 'card__title mt-0' }, 'Entries joined into this one'),
      el('p', {}, 'Nothing was deleted. Any of these can be separated again.'),
      el('div', { class: 'stack' }, ...p.merged_people.map((m) => el('div', { class: 'row' },
        el('span', { class: 'strong' }, m.display_name || '(no name)'),
        el('button', {
          class: 'btn', type: 'button',
          onclick: async () => {
            try {
              await api.unmergePerson(m.id);
              renderProfile(personId);
            } catch (err) { errorDialog(err); }
          },
        }, 'Separate this one again'),
      ))),
    ));
  }

  if (p.findings.length) {
    root.append(el('div', { class: 'card' },
      el('h2', { class: 'card__title mt-0' }, 'Things to know about this person'),
      el('div', { class: 'stack' }, ...p.findings.map((f) => el('div', {},
        el('div', { class: 'strong' }, f.title),
        el('p', { class: 'small' }, (f.detail || '').split('\n')[0]),
      ))),
    ));
  }

  root.append(recentItems(p));
  mount(root);
}

function identitiesPanel(p) {
  return el('div', { class: 'card' },
    el('h2', { class: 'card__title mt-0' }, 'Every address used'),
    el('div', { class: 'table-wrap' },
      el('table', {},
        el('thead', {}, el('tr', {},
          el('th', {}, 'Address'),
          el('th', {}, 'Kind'),
          el('th', {}, 'Name used with it'),
          el('th', {}, 'First seen'),
          el('th', {}, 'Last seen'),
          el('th', { class: 'num' }, 'Times used'),
        )),
        el('tbody', {}, ...p.identities.map((i) => el('tr', {},
          el('td', { class: 'cell-path' }, i.address),
          el('td', {}, addressKind(i.address_type)),
          el('td', {}, i.raw_display_name || ''),
          el('td', { class: 'nowrap' }, date(i.first_seen_utc)),
          el('td', { class: 'nowrap' }, date(i.last_seen_utc)),
          el('td', { class: 'num' }, num(i.use_count)),
        ))),
      ),
    ),
  );
}

function addressKind(type) {
  return ({
    smtp: 'email address',
    ex: 'internal company address',
    phone: 'telephone number',
    none: 'a name with no address',
  })[type] || type;
}

// A sparkline of records per year. Every year in the span is present, including
// the empty ones, and an empty year is drawn as a gap in the bars rather than
// as a line passing over it.
function sparkline(perYear) {
  const width = Math.max(600, perYear.length * 26);
  const height = 120;
  const pad = 24;
  const max = Math.max(1, ...perYear.map((y) => y.count));
  const slot = (width - pad * 2) / perYear.length;
  const barW = Math.max(4, slot * 0.7);

  const quiet = perYear.filter((y) => !y.count).length;

  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('width', '100%');
  svg.setAttribute('role', 'img');
  // A summary, not a recital. Reading out forty year-and-count pairs is not a
  // description of a chart, it is the chart read aloud badly.
  svg.setAttribute('aria-label',
    `Records per year from ${perYear[0].year} to ${perYear[perYear.length - 1].year}. `
    + `${perYear.length - quiet} of ${perYear.length} years have records; `
    + `${quiet} have none and are shown hatched. The busiest year holds ${max}.`);
  svg.style.maxWidth = '100%';
  svg.style.height = 'auto';
  svg.append(sparkDefs());

  perYear.forEach((y, i) => {
    const x = pad + i * slot + (slot - barW) / 2;
    const h = (y.count / max) * (height - 46);
    const rect = document.createElementNS(SVG_NS, 'rect');
    rect.setAttribute('x', String(x));
    rect.setAttribute('width', String(barW));

    if (y.count) {
      rect.setAttribute('y', String(height - 26 - h));
      rect.setAttribute('height', String(Math.max(2, h)));
      rect.setAttribute('fill', 'var(--accent)');
    } else {
      // A year with nothing in it is hatched and full height, like every other
      // chart in this program. Drawing it as an invisible bar was the one place
      // an empty period slid past the eye unremarked.
      rect.setAttribute('y', String(20));
      rect.setAttribute('height', String(height - 46));
      rect.setAttribute('fill', 'url(#spark-hatch)');
      rect.setAttribute('stroke', 'var(--gap-ink)');
      rect.setAttribute('stroke-width', '1');
    }

    const title = document.createElementNS(SVG_NS, 'title');
    title.textContent = y.count
      ? `${y.year}: ${y.count} records`
      : `${y.year}: no data`;
    rect.append(title);
    svg.append(rect);

    if (i % Math.ceil(perYear.length / 14) === 0 || i === perYear.length - 1) {
      const label = document.createElementNS(SVG_NS, 'text');
      label.setAttribute('x', String(x + barW / 2));
      label.setAttribute('y', String(height - 8));
      label.setAttribute('text-anchor', 'middle');
      label.setAttribute('font-size', '13');
      label.setAttribute('fill', 'var(--text-muted)');
      label.textContent = String(y.year);
      svg.append(label);
    }
  });

  const wrap = el('div', { class: 'svg-scroll' });
  wrap.append(svg);
  return el('div', {}, wrap,
    quiet
      ? el('p', { class: 'muted small mb-0' },
          `${plural(quiet, 'year')} with no data at all, shown hatched.`)
      : null,
  );
}

/** The hatching that means "no data", the same idea as the other two charts. */
function sparkDefs() {
  const defs = document.createElementNS(SVG_NS, 'defs');
  const pattern = document.createElementNS(SVG_NS, 'pattern');
  pattern.setAttribute('id', 'spark-hatch');
  pattern.setAttribute('width', '6');
  pattern.setAttribute('height', '6');
  pattern.setAttribute('patternUnits', 'userSpaceOnUse');
  pattern.setAttribute('patternTransform', 'rotate(45)');

  const bg = document.createElementNS(SVG_NS, 'rect');
  bg.setAttribute('width', '6');
  bg.setAttribute('height', '6');
  bg.setAttribute('fill', 'var(--surface-2)');
  pattern.append(bg);

  const stripe = document.createElementNS(SVG_NS, 'rect');
  stripe.setAttribute('width', '2.5');
  stripe.setAttribute('height', '6');
  stripe.setAttribute('fill', 'var(--gap-ink)');
  stripe.setAttribute('opacity', '0.6');
  pattern.append(stripe);

  defs.append(pattern);
  return defs;
}

function recentItems(p) {
  if (!p.recent_items.length) {
    return el('div', { class: 'card' },
      el('h2', { class: 'card__title mt-0' }, 'Their records'),
      el('p', { class: 'mb-0 muted' }, 'Nothing yet.'));
  }
  return el('div', { class: 'card' },
    el('h2', { class: 'card__title mt-0' }, 'Their most recent records'),
    el('div', { class: 'table-wrap' },
      el('table', {},
        el('thead', {}, el('tr', {},
          el('th', {}, 'When'),
          el('th', {}, 'What'),
          el('th', {}, 'Subject'),
          el('th', {}, 'Their part'),
        )),
        el('tbody', {}, ...p.recent_items.map((i) => el('tr', {},
          el('td', { class: 'nowrap' }, date(i.occurred_utc)),
          el('td', {}, i.kind),
          el('td', {}, i.subject || '(no subject)'),
          el('td', {}, i.role),
        ))),
      ),
    ),
  );
}
