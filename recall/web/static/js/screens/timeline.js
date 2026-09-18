// Timeline - hand-drawn inline SVG. No charting library, by design.
//
// The rule that matters most here is spec 9.2:
//
//   The Timeline marks gap months with visible hatching labeled "no data". It
//   must never interpolate, smooth, average, or connect a line across a gap.
//   An empty month looks empty.
//
// So this draws bars, never a line. A bar with no data is not a bar of height
// zero that the eye slides past - it is a hatched block with the words "no
// data" on it. There is no path element anywhere in this file, and that is
// deliberate: you cannot accidentally connect across a gap with a <rect>.

import { api } from '../api.js';
import {
  clear, el, empty, errorNotice, loading, mount, num, plural, setTitle,
} from '../ui.js';

const SVG_NS = 'http://www.w3.org/2000/svg';

const KIND_LABELS = {
  message: 'Messages',
  event: 'Calendar entries',
  contact: 'Contacts',
  task: 'Tasks',
  note: 'Notes',
};

const state = { level: 'year', year: null, month: null };

export async function render({ params }) {
  setTitle('Timeline');

  state.level = params.get('level') || 'year';
  state.year = params.get('year') ? Number(params.get('year')) : null;
  state.month = params.get('month') ? Number(params.get('month')) : null;

  const root = el('div', {},
    el('h1', { class: 'page__title' }, 'Timeline'),
    el('p', { class: 'page__lede' },
      'Everything in the archive, by date. Click a bar to look closer. ' +
      'Periods with nothing in them are shown hatched and labelled ' +
      '— they are never smoothed over.'),
    el('div', { id: 'timeline-body' }, loading('Building the timeline')),
  );
  mount(root);

  await draw();
}

async function draw() {
  const host = document.getElementById('timeline-body');
  if (!host) return;

  let data;
  try {
    data = await api.timeline(state.level, state.year, state.month);
  } catch (err) {
    clear(host);
    host.append(errorNotice(err));
    return;
  }

  clear(host);

  if (!data.buckets.length) {
    host.append(empty('There is nothing on the timeline yet',
      data.empty_reason || 'Read some files into the archive first.'));
    host.append(el('a', { class: 'btn btn--primary', href: '#/sources' },
      'Go to the files found on this computer'));
    return;
  }

  host.append(
    breadcrumb(data),
    headline(data),
    el('div', { class: 'card' }, chart(data)),
    legend(data),
    undatedPanel(data.undated),
    exportPanel(),
  );
}

// --- the numbers above the chart -----------------------------------------

function headline(data) {
  const t = data.total || {};
  const row = el('div', { class: 'row mb-5' });

  row.append(el('span', { class: 'stat__value' }, num(t.value)));
  row.append(el('span', { class: 'stat__label' }, 'records with a date'));

  if (t.qualified) {
    row.append(el('span', { class: 'qualified-note' },
      t.estimated_missing
        ? `about ${num(t.estimated_missing)} more could not be read`
        : (t.qualifiers || []).map((q) => q.text).join('; ')));
    row.append(el('a', { class: 'health__link', href: '#/problems' }, 'Why?'));
  }

  if (data.span) {
    row.append(el('span', { class: 'muted' },
      `· ${data.span.first_year} to ${data.span.last_year}`));
  }
  return row;
}

function breadcrumb(data) {
  const row = el('div', { class: 'btn-row mb-5' });

  if (state.level !== 'year') {
    row.append(el('button', {
      class: 'btn', type: 'button',
      onclick: () => { state.level = 'year'; state.year = null; state.month = null; draw(); },
    }, 'All years'));
  }
  if (state.level === 'day' && state.year) {
    row.append(el('button', {
      class: 'btn', type: 'button',
      onclick: () => { state.level = 'month'; state.month = null; draw(); },
    }, `All of ${state.year}`));
  }

  const where = state.level === 'year'
    ? 'Showing every year'
    : state.level === 'month'
      ? `Showing the months of ${state.year}`
      : `Showing the days of ${monthName(state.month)} ${state.year}`;
  row.append(el('span', { class: 'muted' }, where));
  return row;
}

// --- the chart ------------------------------------------------------------

function chart(data) {
  const buckets = data.buckets;
  const width = Math.max(900, buckets.length * 46 + 120);
  const height = 420;
  const pad = { top: 24, right: 24, bottom: 86, left: 76 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;

  const max = Math.max(1, ...buckets.map((b) => b.total));
  const step = niceStep(max, 5);
  const axisTop = Math.ceil(max / step) * step;
  const slot = plotW / buckets.length;
  const barW = Math.min(38, Math.max(8, slot * 0.68));

  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('width', '100%');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', describeChart(data));
  svg.style.maxWidth = '100%';
  svg.style.height = 'auto';

  svg.append(defs());

  // Eras shaded behind the bars.
  for (const band of eraBands(data, buckets, pad, plotW, plotH, slot)) svg.append(band);

  // Y axis: a few gridlines with real numbers on them.
  for (const tick of yAxis(axisTop, step, pad, plotW, plotH)) svg.append(tick);

  // The bars.
  buckets.forEach((bucket, i) => {
    const x = pad.left + i * slot + (slot - barW) / 2;
    svg.append(bar(bucket, x, barW, pad, plotH, axisTop, data));
  });

  // X axis labels, thinned so they never overlap.
  const every = Math.ceil(buckets.length / Math.floor(plotW / 44));
  buckets.forEach((bucket, i) => {
    if (i % every !== 0 && i !== buckets.length - 1) return;
    const x = pad.left + i * slot + slot / 2;
    svg.append(text(x, pad.top + plotH + 22, bucket.label, {
      anchor: 'middle', size: 15, fill: 'var(--text-muted)',
    }));
  });

  // Axis lines.
  svg.append(line(pad.left, pad.top + plotH, pad.left + plotW, pad.top + plotH,
    'var(--border-strong)', 2));
  svg.append(line(pad.left, pad.top, pad.left, pad.top + plotH,
    'var(--border-strong)', 2));

  const wrap = el('div', { style: 'overflow-x:auto' });
  wrap.append(svg);
  return el('div', {}, wrap, chartTable(data));
}

function defs() {
  const defs = document.createElementNS(SVG_NS, 'defs');

  // The hatching that says "no data". A pattern, not a colour, so the meaning
  // survives a black-and-white print and colour blindness alike.
  const pattern = document.createElementNS(SVG_NS, 'pattern');
  pattern.setAttribute('id', 'gap-hatch');
  pattern.setAttribute('width', '8');
  pattern.setAttribute('height', '8');
  pattern.setAttribute('patternUnits', 'userSpaceOnUse');
  pattern.setAttribute('patternTransform', 'rotate(45)');

  const bg = document.createElementNS(SVG_NS, 'rect');
  bg.setAttribute('width', '8');
  bg.setAttribute('height', '8');
  bg.setAttribute('fill', 'var(--surface-2)');
  pattern.append(bg);

  const stripe = document.createElementNS(SVG_NS, 'rect');
  stripe.setAttribute('width', '3');
  stripe.setAttribute('height', '8');
  stripe.setAttribute('fill', 'var(--gap-ink)');
  stripe.setAttribute('opacity', '0.55');
  pattern.append(stripe);

  defs.append(pattern);
  return defs;
}

function bar(bucket, x, barW, pad, plotH, max, data) {
  const group = document.createElementNS(SVG_NS, 'g');
  const baseY = pad.top + plotH;

  if (!bucket.has_data) {
    // No data. A hatched block the full height of the plot, so an empty period
    // reads as "nothing here", not as "a very small amount here".
    const rect = document.createElementNS(SVG_NS, 'rect');
    rect.setAttribute('x', String(x));
    rect.setAttribute('y', String(pad.top));
    rect.setAttribute('width', String(barW));
    rect.setAttribute('height', String(plotH));
    rect.setAttribute('fill', 'url(#gap-hatch)');
    rect.setAttribute('stroke', 'var(--gap-ink)');
    rect.setAttribute('stroke-width', '1.5');
    rect.setAttribute('stroke-dasharray', '4 3');
    group.append(rect);

    const label = text(x + barW / 2, pad.top + plotH / 2, 'no data', {
      anchor: 'middle', size: 13, fill: 'var(--gap-ink)', weight: '700',
    });
    label.setAttribute('transform', `rotate(-90 ${x + barW / 2} ${pad.top + plotH / 2})`);
    group.append(label);

    if (bucket.explained) {
      const note = text(x + barW / 2, pad.top + 14, 'explained', {
        anchor: 'middle', size: 12, fill: 'var(--text-muted)',
      });
      group.append(note);
    }
  } else {
    // Stacked by kind, tallest-contributing kind at the bottom.
    let y = baseY;
    for (const kind of data.kinds) {
      const n = bucket.by_kind[kind] || 0;
      if (!n) continue;
      const h = (n / max) * plotH;
      const rect = document.createElementNS(SVG_NS, 'rect');
      rect.setAttribute('x', String(x));
      rect.setAttribute('y', String(y - h));
      rect.setAttribute('width', String(barW));
      rect.setAttribute('height', String(Math.max(1, h)));
      rect.setAttribute('fill', `var(--kind-${kind})`);
      group.append(rect);
      y -= h;
    }

    if (bucket.gap_class === 'partial' || bucket.gap_class === 'source_contradiction') {
      // Data, but not all of it. The bar is outlined in the gap ink so the
      // eye is told the number underneath is not the whole story.
      const outline = document.createElementNS(SVG_NS, 'rect');
      outline.setAttribute('x', String(x - 1));
      outline.setAttribute('y', String(y - 1));
      outline.setAttribute('width', String(barW + 2));
      outline.setAttribute('height', String(baseY - y + 2));
      outline.setAttribute('fill', 'none');
      outline.setAttribute('stroke', 'var(--gap-ink)');
      outline.setAttribute('stroke-width', '2.5');
      outline.setAttribute('stroke-dasharray', '5 3');
      group.append(outline);

      group.append(text(x + barW / 2, y - 8, '⚠', {
        anchor: 'middle', size: 16, fill: 'var(--gap-ink)',
      }));
    }
  }

  // The whole column is the click target, so it is comfortably over 44px.
  const hit = document.createElementNS(SVG_NS, 'rect');
  hit.setAttribute('x', String(x - 4));
  hit.setAttribute('y', String(pad.top));
  hit.setAttribute('width', String(barW + 8));
  hit.setAttribute('height', String(plotH));
  hit.setAttribute('fill', 'transparent');
  hit.setAttribute('tabindex', '0');
  hit.setAttribute('role', 'button');
  hit.style.cursor = 'pointer';
  hit.setAttribute('aria-label', describeBucket(bucket));

  const activate = () => drill(bucket);
  hit.addEventListener('click', activate);
  hit.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(); }
  });

  const title = document.createElementNS(SVG_NS, 'title');
  title.textContent = describeBucket(bucket);
  hit.append(title);
  group.append(hit);

  return group;
}

// Counts are whole things. An axis reading "2, 2, 1, 1, 0" because it divided
// a maximum of 2 into four parts is not a rounding error the reader can see
// past - it looks like the chart is broken. The step is chosen to be a whole
// number, so every label is distinct and every one is a count that could
// actually occur.
function yAxis(top, step, pad, plotW, plotH) {
  const out = [];
  for (let value = 0; value <= top; value += step) {
    const y = pad.top + plotH - (value / top) * plotH;
    out.push(line(pad.left, y, pad.left + plotW, y, 'var(--border)', 1));
    out.push(text(pad.left - 10, y + 5, num(value), {
      anchor: 'end', size: 14, fill: 'var(--text-muted)',
    }));
  }
  return out;
}

function niceStep(max, targetTicks) {
  const raw = Math.max(1, max / targetTicks);
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  for (const multiple of [1, 2, 5, 10]) {
    const step = multiple * magnitude;
    if (step >= raw) return Math.max(1, Math.round(step));
  }
  return Math.max(1, Math.round(10 * magnitude));
}

function eraBands(data, buckets, pad, plotW, plotH, slot) {
  const out = [];
  for (const era of data.eras || []) {
    const from = indexForDate(buckets, era.start_utc, 0);
    const to = indexForDate(buckets, era.end_utc, buckets.length - 1);
    if (from === null || to === null || to < from) continue;

    const rect = document.createElementNS(SVG_NS, 'rect');
    rect.setAttribute('x', String(pad.left + from * slot));
    rect.setAttribute('y', String(pad.top));
    rect.setAttribute('width', String((to - from + 1) * slot));
    rect.setAttribute('height', String(plotH));
    rect.setAttribute('fill', era.color || 'var(--accent)');
    rect.setAttribute('opacity', '0.1');
    out.push(rect);

    out.push(text(pad.left + from * slot + 6, pad.top + 16, era.name, {
      anchor: 'start', size: 14, fill: 'var(--text-muted)', weight: '600',
    }));
  }
  return out;
}

function indexForDate(buckets, iso, fallback) {
  if (!iso) return fallback;
  const key = iso.slice(0, buckets[0].key.length);
  const exact = buckets.findIndex((b) => b.key === key);
  if (exact !== -1) return exact;
  const after = buckets.findIndex((b) => b.key > key);
  return after === -1 ? buckets.length - 1 : after;
}

// The chart is a picture; this is the same data as text, for anyone who cannot
// read the picture and for anyone who wants the exact numbers.
function chartTable(data) {
  const rows = data.buckets.map((b) => el('tr', {},
    el('td', {}, b.label),
    el('td', { class: 'num' }, b.has_data ? num(b.total) : 'no data'),
    ...data.kinds.map((k) => el('td', { class: 'num' },
      b.by_kind[k] ? num(b.by_kind[k]) : '—')),
    el('td', {}, b.has_data
      ? (b.source_count ? plural(b.source_count, 'file') : '')
      : el('span', { class: 'tag tag--critical' },
          b.explained ? 'No data (explained)' : 'No data')),
  ));

  return el('details', { class: 'mb-3' },
    el('summary', {}, 'The same figures as a table'),
    el('div', { class: 'table-wrap' },
      el('table', {},
        el('thead', {}, el('tr', {},
          el('th', {}, 'Period'),
          el('th', { class: 'num' }, 'Total'),
          ...data.kinds.map((k) => el('th', { class: 'num' }, KIND_LABELS[k] || k)),
          el('th', {}, 'Came from'),
        )),
        el('tbody', {}, ...rows),
      ),
    ),
  );
}

function legend(data) {
  const items = data.kinds
    .filter((k) => data.buckets.some((b) => b.by_kind[k]))
    .map((kind) => el('span', { class: 'row', style: 'gap:8px' },
      swatch(`var(--kind-${kind})`),
      el('span', {}, KIND_LABELS[kind] || kind),
    ));

  items.push(el('span', { class: 'row', style: 'gap:8px' },
    swatch('url(#gap-hatch)', true),
    el('span', {}, 'No data — nothing was found for this period'),
  ));

  return el('div', { class: 'row mb-5', style: 'gap:24px' }, ...items);
}

function swatch(fill, hatched = false) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('width', '22');
  svg.setAttribute('height', '22');
  svg.setAttribute('aria-hidden', 'true');
  if (hatched) svg.append(defs());
  const rect = document.createElementNS(SVG_NS, 'rect');
  rect.setAttribute('width', '22');
  rect.setAttribute('height', '22');
  rect.setAttribute('fill', fill);
  if (hatched) {
    rect.setAttribute('stroke', 'var(--gap-ink)');
    rect.setAttribute('stroke-width', '1.5');
  }
  svg.append(rect);
  return svg;
}

function undatedPanel(undated) {
  if (!undated || !undated.total) return null;
  return el('div', { class: 'notice notice--warning' },
    el('div', { class: 'notice__title' },
      `${plural(undated.total, 'record')} with no date at all`),
    el('p', {},
      'These are not on the timeline above, and Recall has not guessed a date ' +
      'for any of them. They are kept exactly as found.'),
    el('p', { class: 'mb-0' },
      Object.entries(undated.by_kind)
        .map(([k, n]) => `${num(n)} ${(KIND_LABELS[k] || k).toLowerCase()}`)
        .join(', ')),
  );
}

function exportPanel() {
  const status = el('div', { id: 'export-status' });

  return el('div', { class: 'card' },
    el('h2', { class: 'card__title mt-0' }, 'Save the calendar to a file'),
    el('p', {},
      'A spreadsheet of every calendar entry, ready to open in Excel. ' +
      'Alongside it Recall writes a plain-language note saying exactly what ' +
      'is missing or uncertain in the file — that note is not optional.'),
    el('div', { class: 'btn-row' },
      el('button', {
        class: 'btn btn--primary', type: 'button',
        onclick: (e) => runExport(e.target, 'csv', status),
      }, 'Save as CSV for Excel'),
      el('button', {
        class: 'btn', type: 'button',
        onclick: (e) => runExport(e.target, 'xlsx', status),
      }, 'Save as an Excel workbook'),
      el('button', {
        class: 'btn', type: 'button',
        onclick: (e) => runExport(e.target, 'markdown', status),
      }, 'Save as a readable document'),
    ),
    status,
  );
}

async function runExport(button, format, status) {
  button.disabled = true;
  const previous = button.textContent;
  button.textContent = 'Saving…';
  clear(status);

  try {
    const result = await api.exportData(format, 'calendar');
    const bits = [
      el('div', { class: 'notice__title' }, 'Saved'),
      el('p', {}, `${num(result.exported_count)} calendar entries written to:`),
      el('pre', { class: 'raw' }, result.file),
    ];
    if (result.integrity_file) {
      bits.push(el('p', {}, 'The note about what is missing is beside it:'));
      bits.push(el('pre', { class: 'raw' }, result.integrity_file));
    }
    if (!result.is_complete) {
      bits.push(el('p', { class: 'qualified-note' },
        result.estimated_missing
          ? `This export is not complete: about ${num(result.estimated_missing)} ` +
            'more records could not be read. The note beside the file explains why.'
          : 'This export has known problems. The note beside the file explains them.'));
    }
    status.append(el('div', {
      class: result.is_complete ? 'notice notice--good' : 'notice notice--warning',
    }, ...bits));
  } catch (err) {
    status.append(errorNotice(err));
  } finally {
    button.disabled = false;
    button.textContent = previous;
  }
}

// --- behaviour ------------------------------------------------------------

function drill(bucket) {
  if (state.level === 'year' && bucket.has_data) {
    state.level = 'month';
    state.year = bucket.year;
    draw();
    return;
  }
  if (state.level === 'month' && bucket.has_data) {
    state.level = 'day';
    state.year = bucket.year;
    state.month = bucket.month;
    draw();
    return;
  }
  if (state.level === 'day' || !bucket.has_data) {
    // A day, or an empty period: send the user to Search filtered to it, so
    // "what do I actually have here" is one click from the picture.
    window.location.hash = `#/search?from=${bucket.key}&to=${bucket.key}`;
  }
}

function describeBucket(b) {
  if (!b.has_data) {
    return `${b.label}: no data${b.explained ? ' (you have explained this)' : ''}. ` +
           'Nothing was found for this period.';
  }
  const parts = Object.entries(b.by_kind)
    .filter(([, n]) => n)
    .map(([k, n]) => `${num(n)} ${(KIND_LABELS[k] || k).toLowerCase()}`);
  let text = `${b.label}: ${num(b.total)} records (${parts.join(', ')})`;
  if (b.source_count) text += `, from ${plural(b.source_count, 'file')}`;
  if (b.gap_class === 'source_contradiction') {
    text += '. Warning: a file that should cover this period gave up nothing from it.';
  } else if (b.gap_class === 'partial') {
    text += '. Warning: part of this period has no data.';
  }
  return text;
}

function describeChart(data) {
  const withData = data.buckets.filter((b) => b.has_data).length;
  const without = data.buckets.length - withData;
  return (
    `Bar chart of records by ${state.level}. ` +
    `${withData} periods have data; ${without} have none and are shown hatched. ` +
    'The same figures are in the table below.'
  );
}

function monthName(m) {
  return ['January', 'February', 'March', 'April', 'May', 'June', 'July',
    'August', 'September', 'October', 'November', 'December'][(m || 1) - 1];
}

// --- svg helpers ----------------------------------------------------------

function text(x, y, content, { anchor = 'start', size = 14, fill = 'var(--text)', weight = '400' } = {}) {
  const node = document.createElementNS(SVG_NS, 'text');
  node.setAttribute('x', String(x));
  node.setAttribute('y', String(y));
  node.setAttribute('text-anchor', anchor);
  node.setAttribute('font-size', String(size));
  node.setAttribute('font-weight', weight);
  node.setAttribute('fill', fill);
  node.setAttribute('font-family', 'inherit');
  node.textContent = content;
  return node;
}

function line(x1, y1, x2, y2, stroke, width) {
  const node = document.createElementNS(SVG_NS, 'line');
  node.setAttribute('x1', String(x1));
  node.setAttribute('y1', String(y1));
  node.setAttribute('x2', String(x2));
  node.setAttribute('y2', String(y2));
  node.setAttribute('stroke', stroke);
  node.setAttribute('stroke-width', String(width));
  return node;
}
