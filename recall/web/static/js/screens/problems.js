// Problems - the integrity queue, and the coverage map above it.
//
// Spec, screen 7: the coverage map "is the single most important picture in the
// application - it is the honest answer to 'what do I actually have?'"
//
// So it is drawn first, before any list, and it shows every month in the span
// including the empty ones. A month with no data is a hatched cell labelled as
// such, never an absent one - an absent cell is a gap the eye slides over,
// which is the failure this whole program exists to prevent.

import { api } from '../api.js';
import {
  clear, el, empty, errorDialog, errorNotice, field, loading, modal, monthName,
  mount, notice, num, plural, setTitle, severityTag,
} from '../ui.js';

const SVG_NS = 'http://www.w3.org/2000/svg';

const MONTH_LETTERS = ['J', 'F', 'M', 'A', 'M', 'J', 'J', 'A', 'S', 'O', 'N', 'D'];

const state = { severity: '', klass: '', showState: 'open' };

export async function render() {
  setTitle('Problems');

  const root = el('div', {},
    el('h1', { class: 'page__title' }, 'Problems'),
    el('p', { class: 'page__lede' },
      'Everything Recall knows is wrong, missing or uncertain about this ' +
      'archive. Nothing here has been hidden, smoothed over or guessed past.'),
    el('div', { id: 'coverage-map' }, loading('Drawing the coverage map')),
    el('div', { id: 'problem-filters' }),
    el('div', { id: 'problem-list' }, loading('Loading problems')),
  );
  mount(root);

  renderFilters();
  await Promise.all([loadMap(), loadList()]);
}

// --- the coverage map -----------------------------------------------------

async function loadMap() {
  const host = document.getElementById('coverage-map');
  if (!host) return;

  let data;
  try {
    data = await api.coverageMap();
  } catch (err) {
    clear(host);
    host.append(errorNotice(err));
    return;
  }

  clear(host);

  if (!data.years.length) {
    host.append(notice('info', 'There is no coverage to show yet',
      data.empty_reason || 'Read some files into the archive first.'));
    return;
  }

  host.append(el('div', { class: 'card' },
    el('h2', { class: 'card__title mt-0' }, 'What you actually have'),
    el('p', {},
      `Every month from ${data.first_year} to ${data.last_year}. The darker a ` +
      'square, the more records that month holds. A hatched square means ' +
      'nothing at all was found for that month.'),
    mapSvg(data),
    mapLegend(),
    data.undated.total
      ? el('p', { class: 'qualified-note' }, data.undated.note)
      : null,
  ));
}

function mapSvg(data) {
  const cell = 26;
  const gap = 3;
  const labelW = 62;
  const headerH = 26;
  const width = labelW + 12 * (cell + gap) + 60;
  const height = headerH + data.years.length * (cell + gap) + 10;

  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('width', '100%');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', describeMap(data));
  svg.style.maxWidth = `${width}px`;
  svg.style.height = 'auto';
  svg.append(hatchDefs());

  MONTH_LETTERS.forEach((letter, i) => {
    svg.append(text(labelW + i * (cell + gap) + cell / 2, 18, letter, {
      anchor: 'middle', size: 13, fill: 'var(--text-muted)',
    }));
  });

  data.years.forEach((year, rowIndex) => {
    const y = headerH + rowIndex * (cell + gap);

    svg.append(text(labelW - 10, y + cell * 0.72, String(year.year), {
      anchor: 'end', size: 14, fill: 'var(--text-muted)',
    }));

    year.months.forEach((month, i) => {
      const x = labelW + i * (cell + gap);
      svg.append(monthCell(month, x, y, cell, data.max_month));
    });

    svg.append(text(labelW + 12 * (cell + gap) + 8, y + cell * 0.72,
      year.total ? num(year.total) : '—', {
        anchor: 'start', size: 13, fill: 'var(--text-muted)',
      }));
  });

  const wrap = el('div', { class: 'svg-scroll' });
  wrap.append(svg);
  return wrap;
}

function monthCell(month, x, y, size, max) {
  const group = document.createElementNS(SVG_NS, 'g');
  const rect = document.createElementNS(SVG_NS, 'rect');
  rect.setAttribute('x', String(x));
  rect.setAttribute('y', String(y));
  rect.setAttribute('width', String(size));
  rect.setAttribute('height', String(size));
  rect.setAttribute('rx', '3');

  let label;

  if (!month.in_span) {
    // Outside the archive entirely. Not a gap - there was never anything to
    // find here - so it is drawn as nothing rather than as a problem.
    rect.setAttribute('fill', 'transparent');
    rect.setAttribute('stroke', 'var(--border)');
    rect.setAttribute('stroke-width', '1');
    rect.setAttribute('stroke-dasharray', '2 3');
    label = `${monthName(month.number)} ${month.month.slice(0, 4)}: outside the archive's span`;
  } else if (!month.count) {
    rect.setAttribute('fill', 'url(#map-hatch)');
    rect.setAttribute('stroke', 'var(--gap-ink)');
    rect.setAttribute('stroke-width', '1.5');
    const why = month.gap_class === 'source_contradiction'
      ? 'no data - and a file that should cover it gave up nothing'
      : 'no data';
    label = `${monthName(month.number)} ${month.month.slice(0, 4)}: ${why}`
      + (month.explained ? ' (you have explained this)' : '');
    if (month.explained) {
      rect.setAttribute('opacity', '0.45');
    }
  } else {
    // Intensity by volume, on a square-root scale so one enormous month does
    // not flatten every other month to invisibility.
    const intensity = max > 0 ? Math.sqrt(month.count / max) : 0;
    rect.setAttribute('fill', 'var(--accent)');
    rect.setAttribute('opacity', String(0.2 + intensity * 0.8));
    label = `${monthName(month.number)} ${month.month.slice(0, 4)}: `
      + `${num(month.count)} records from ${plural(month.sources, 'file')}`;
    if (month.gap_class === 'soft_gap') {
      rect.setAttribute('stroke', 'var(--high)');
      rect.setAttribute('stroke-width', '2');
      label += ' - much quieter than usual';
    }
  }

  rect.setAttribute('tabindex', '0');
  rect.setAttribute('role', 'img');
  rect.setAttribute('aria-label', label);
  rect.style.cursor = month.in_span ? 'pointer' : 'default';

  const title = document.createElementNS(SVG_NS, 'title');
  title.textContent = label;
  rect.append(title);

  if (month.in_span) {
    const open = () => {
      window.location.hash = month.count
        ? `#/search?from=${month.month}&to=${month.month}`
        : `#/problems`;
    };
    rect.addEventListener('click', open);
    rect.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
    });
  }

  group.append(rect);
  return group;
}

function text(x, y, content, { anchor = 'start', size = 14, fill = 'var(--text)' } = {}) {
  const node = document.createElementNS(SVG_NS, 'text');
  node.setAttribute('x', String(x));
  node.setAttribute('y', String(y));
  node.setAttribute('text-anchor', anchor);
  node.setAttribute('font-size', String(size));
  node.setAttribute('fill', fill);
  node.setAttribute('font-family', 'inherit');
  node.textContent = content;
  return node;
}

function hatchDefs() {
  const defs = document.createElementNS(SVG_NS, 'defs');
  const pattern = document.createElementNS(SVG_NS, 'pattern');
  pattern.setAttribute('id', 'map-hatch');
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

function mapLegend() {
  const swatch = (build, label) => {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('width', '20');
    svg.setAttribute('height', '20');
    svg.setAttribute('aria-hidden', 'true');
    svg.append(hatchDefs());
    svg.append(build());
    return el('span', { class: 'row row--tight' }, svg, el('span', {}, label));
  };

  const box = (fill, opacity, stroke) => () => {
    const rect = document.createElementNS(SVG_NS, 'rect');
    rect.setAttribute('width', '20');
    rect.setAttribute('height', '20');
    rect.setAttribute('rx', '3');
    rect.setAttribute('fill', fill);
    if (opacity) rect.setAttribute('opacity', opacity);
    if (stroke) {
      rect.setAttribute('stroke', stroke);
      rect.setAttribute('stroke-width', '1.5');
    }
    return rect;
  };

  return el('div', { class: 'row row--wide mt-3' },
    swatch(box('var(--accent)', '0.25'), 'a few records'),
    swatch(box('var(--accent)', '1'), 'a great many'),
    swatch(box('url(#map-hatch)', null, 'var(--gap-ink)'), 'no data at all'),
    swatch(box('var(--accent)', '0.6', 'var(--high)'), 'much quieter than usual'),
    swatch(box('transparent', null, 'var(--border)'), 'outside the archive'),
  );
}

function describeMap(data) {
  const gapYears = data.years.filter((y) => y.gap_months);
  return (
    `Coverage map, ${data.first_year} to ${data.last_year}, one square per month. `
    + `${num(data.total)} records in total. `
    + (gapYears.length
      ? `${gapYears.length} year(s) contain months with no data: `
        + gapYears.map((y) => `${y.year} (${y.gap_months})`).join(', ') + '.'
      : 'Every month in the span has something in it.')
  );
}

// --- filters --------------------------------------------------------------

function renderFilters() {
  const host = document.getElementById('problem-filters');
  clear(host);

  host.append(el('div', { class: 'toolbar' },
    field('Severity', el('select', {
      onchange: (e) => { state.severity = e.target.value; loadList(); },
    },
      el('option', { value: '' }, 'All severities'),
      el('option', { value: 'critical' }, 'Critical — data certainly lost'),
      el('option', { value: 'high' }, 'High — data probably lost or wrong'),
      el('option', { value: 'medium' }, 'Medium — something is uncertain'),
      el('option', { value: 'info' }, 'Information'),
    )),
    field('Kind of problem', el('select', {
      onchange: (e) => { state.klass = e.target.value; loadList(); },
    },
      el('option', { value: '' }, 'Everything'),
      el('option', { value: 'unreadable_files' }, 'Files that could not be read'),
      el('option', { value: 'coverage_gaps' }, 'Periods with no data'),
      el('option', { value: 'duplicate_accounts' }, 'People and accounts'),
      el('option', { value: 'record_quality' }, 'Records with something uncertain'),
    )),
    field('Show', el('select', {
      onchange: (e) => { state.showState = e.target.value; loadList(); },
    },
      el('option', { value: 'open' }, 'Still open'),
      el('option', { value: 'explained' }, 'Ones you have explained'),
      el('option', { value: 'wont_fix' }, "Ones you won't fix"),
      el('option', { value: 'resolved' }, 'Ones that went away'),
      el('option', { value: 'all' }, 'Everything, ever'),
    )),
  ));
}


// --- the list -------------------------------------------------------------

async function loadList() {
  const host = document.getElementById('problem-list');
  if (!host) return;

  let data;
  try {
    data = await api.findings({
      severity: state.severity,
      klass: state.klass,
      state: state.showState,
    });
  } catch (err) {
    clear(host);
    host.append(errorNotice(err));
    return;
  }

  clear(host);

  if (!data.findings.length) {
    host.append(empty(
      state.showState === 'open' && !state.severity && !state.klass
        ? 'Nothing is wrong'
        : 'Nothing matches those filters',
      state.showState === 'open' && !state.severity && !state.klass
        ? 'Every check passed. That is a statement about what Recall could ' +
          'check — it cannot know about a mailbox that was never on this computer.'
        : 'Change the filters above to see more.',
    ));
    return;
  }

  const bySeverity = new Map();
  for (const finding of data.findings) {
    if (!bySeverity.has(finding.severity)) bySeverity.set(finding.severity, []);
    bySeverity.get(finding.severity).push(finding);
  }

  const order = ['critical', 'high', 'medium', 'info'];
  const explanations = {
    critical: 'Data is certainly lost. These first.',
    high: 'Data is probably lost or wrong.',
    medium: 'Something is uncertain, and has been recorded as uncertain.',
    info: 'Worth knowing. Nothing is wrong.',
  };

  host.append(el('p', { class: 'muted' }, `${plural(data.findings.length, 'problem')}.`));

  for (const severity of order) {
    const group = bySeverity.get(severity);
    if (!group) continue;

    const byClass = new Map();
    for (const finding of group) {
      if (!byClass.has(finding.class)) byClass.set(finding.class, []);
      byClass.get(finding.class).push(finding);
    }

    const section = el('section', { class: 'mb-5' },
      el('h2', { class: 'row' }, severityTag(severity), `(${group.length})`),
      el('p', { class: 'muted' }, explanations[severity] || ''),
    );

    for (const [className, entries] of byClass) {
      section.append(el('h3', {}, `${data.classes[className] || className} (${entries.length})`));
      for (const finding of entries) section.append(findingCard(finding));
    }
    host.append(section);
  }
}

function findingCard(finding) {
  const facts = [];
  if (finding.estimated_loss) {
    facts.push(el('span', { class: 'qualified-note' },
      `about ${num(finding.estimated_loss)} records could not be read`));
  }
  if (finding.affected_count) {
    // For a coverage gap the count is months, not records. Calling months
    // "records affected" would tell the reader a gap of two months lost two
    // messages, which is precisely the number nobody knows.
    const unit = finding.code === 'hard_gap' ? 'month' : 'record';
    facts.push(el('span', { class: 'muted' },
      `${plural(finding.affected_count, unit)} affected`));
  }
  if (finding.period_start) {
    const period = finding.period_end && finding.period_end !== finding.period_start
      ? `${finding.period_start} to ${finding.period_end}`
      : finding.period_start;
    facts.push(el('span', { class: 'muted' }, period));
  }
  if (finding.source_path) {
    facts.push(el('span', { class: 'cell-path' }, finding.source_path));
  }
  if (finding.state !== 'open') {
    facts.push(el('span', { class: 'tag tag--plain' }, stateWord(finding.state)));
  }

  const card = el('div', { class: `notice notice--${noticeKind(finding.severity)}` },
    el('div', { class: 'notice__title' }, finding.title),
    facts.length ? el('div', { class: 'row mb-3' }, ...facts) : null,
    ...(finding.detail || '').split('\n\n').filter(Boolean).map((p) => el('p', {}, p)),
    finding.user_note
      ? el('p', {}, el('strong', {}, 'Your note: '), finding.user_note)
      : null,

    el('details', {},
      el('summary', {}, 'The technical detail, for when you send this to someone'),
      el('pre', { class: 'raw' }, technicalDetail(finding)),
    ),

    actions(finding),
  );
  return card;
}

function actions(finding) {
  const row = el('div', { class: 'btn-row' });
  const needsNote = finding.code === 'hard_gap' || finding.code === 'soft_gap'
    || finding.code === 'source_contradiction';

  if (finding.state === 'open') {
    row.append(el('button', {
      class: 'btn', type: 'button',
      onclick: () => setState(finding, 'acknowledged'),
    }, 'I have seen this'));
  }

  row.append(el('button', {
    class: 'btn btn--primary', type: 'button',
    onclick: () => explain(finding, needsNote),
  }, needsNote ? 'Explain what was happening' : 'Add a note'));

  if (finding.source_file_id) {
    row.append(el('button', {
      class: 'btn', type: 'button',
      onclick: () => retry(finding),
    }, 'Read that file again with the other reader'));
    row.append(el('a', { class: 'btn', href: '#/sources' }, 'Go to the file'));
  }
  if (finding.person_id) {
    row.append(el('a', { class: 'btn', href: `#/people/${finding.person_id}` },
      'Go to the person'));
  }
  if (finding.period_start) {
    row.append(el('a', {
      class: 'btn',
      href: `#/timeline?level=month&year=${finding.period_start.slice(0, 4)}`,
    }, 'See it on the timeline'));
  }

  if (finding.state !== 'wont_fix') {
    row.append(el('button', {
      class: 'btn', type: 'button',
      onclick: () => setState(finding, 'wont_fix'),
    }, 'Nothing to be done'));
  }

  return row;
}

function noticeKind(severity) {
  return ({ critical: 'error', high: 'warning', medium: 'info', info: 'info' })[severity]
    || 'info';
}

function stateWord(state) {
  return ({
    acknowledged: 'seen',
    explained: 'explained',
    resolved: 'gone on a later run',
    wont_fix: "won't fix",
  })[state] || state;
}

function technicalDetail(finding) {
  const lines = [
    `finding id: ${finding.id}`,
    `code:       ${finding.code}`,
    `severity:   ${finding.severity}`,
    `state:      ${finding.state}`,
    `first seen: ${finding.first_seen_utc}`,
    `last seen:  ${finding.last_seen_utc || '-'}`,
  ];
  if (finding.resolved_utc) lines.push(`resolved:   ${finding.resolved_utc}`);
  if (finding.evidence_json) {
    lines.push('');
    lines.push('evidence:');
    try {
      lines.push(JSON.stringify(JSON.parse(finding.evidence_json), null, 2));
    } catch {
      lines.push(finding.evidence_json);
    }
  }
  return lines.join('\n');
}

// --- actions --------------------------------------------------------------

async function setState(finding, newState, note) {
  try {
    await api.setFindingState(finding.id, newState, note);
    await Promise.all([loadList(), loadMap()]);
  } catch (err) {
    errorDialog(err);
  }
}

function explain(finding, required) {
  const input = el('textarea', {
    placeholder: 'For example: I was not using email yet. Or: that was the '
      + 'Contract Marketing server we lost in the move.',
  });
  if (finding.user_note) input.value = finding.user_note;

  const warning = el('div');

  const dialog = modal({
    title: 'What was happening?',
    body: el('div', {},
      el('p', {}, finding.title),
      el('p', {},
        'Write down what you know. Recall keeps the note with this problem ' +
        'permanently, and stops bringing it up in the health banner.'),
      el('p', { class: 'muted' },
        'The gap itself stays on the timeline, hatched, forever. Explaining ' +
        'something never deletes it — the archive keeps saying what it does ' +
        'not have, it just stops asking you about it.'),
      warning,
      el('label', { for: 'explain-note' }, 'Your note'),
      input,
    ),
    actions: [
      el('button', {
        class: 'btn btn--primary', type: 'button',
        onclick: async () => {
          const note = input.value.trim();
          if (required && !note) {
            clear(warning);
            warning.append(notice('warning', 'A note is needed here',
              'An unexplained explanation is not one. Write down whatever you ' +
              'know, even if it is only "I do not know why".'));
            return;
          }
          dialog.close();
          await setState(finding, note ? 'explained' : 'acknowledged', note);
        },
      }, 'Save this note'),
      el('button', { class: 'btn', type: 'button', onclick: () => dialog.close() },
        'Cancel'),
    ],
  });
  input.focus();
}

async function retry(finding) {
  const dialog = modal({
    title: 'Read that file again?',
    body: el('div', {},
      el('p', {},
        'Recall will read the file again using its other reader — Microsoft ' +
        'Outlook instead of the built-in one, or the other way round.'),
      el('p', {},
        'If it recovers more, the extra records are added. If it recovers less, ' +
        'the better result is kept. Either way the file itself is not changed, ' +
        'and this problem stays on the list with a note of what happened.'),
      el('p', { class: 'muted' },
        'A large file can take a long time. You can carry on using Recall while ' +
        'it works.'),
    ),
    actions: [
      el('button', {
        class: 'btn btn--primary', type: 'button',
        onclick: async () => {
          dialog.close();
          try {
            const result = await api.retryFinding(finding.id);
            showRetryResult(result);
            await Promise.all([loadList(), loadMap()]);
          } catch (err) {
            errorDialog(err);
          }
        },
      }, 'Yes, read it again'),
      el('button', { class: 'btn', type: 'button', onclick: () => dialog.close() },
        'Not now'),
    ],
  });
}

function showRetryResult(result) {
  const better = result.now_count > result.was_count;
  const dialog = modal({
    title: better ? 'That worked' : 'No better',
    body: el('div', {},
      el('p', {}, result.message),
      el('pre', { class: 'raw' },
        `before: ${num(result.was_count)} records (${result.was_backend || 'unknown'})\n`
        + `after:  ${num(result.now_count)} records (${result.now_backend || 'unknown'})`),
    ),
    actions: [el('button', {
      class: 'btn btn--primary', type: 'button', onclick: () => dialog.close(),
    }, 'Close')],
  });
}
