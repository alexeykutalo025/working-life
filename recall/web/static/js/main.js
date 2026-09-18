// The shell: hash routing, the theme switch, and the health banner.

import { api, ApiError } from './api.js';
import { clear, el, errorNotice, loading, mount, setTitle } from './ui.js';

// --- theme ----------------------------------------------------------------

const THEME_KEY = 'recall.theme';

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme === 'system' ? '' : theme;
  const label = document.getElementById('theme-toggle-label');
  if (label) {
    const dark = theme === 'dark'
      || (theme !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches);
    label.textContent = dark ? 'Light screen' : 'Dark screen';
  }
}

function currentTheme() {
  try {
    return localStorage.getItem(THEME_KEY) || 'system';
  } catch {
    return 'system';
  }
}

function initTheme() {
  applyTheme(currentTheme());
  const button = document.getElementById('theme-toggle');
  if (!button) return;
  button.addEventListener('click', () => {
    const dark = document.documentElement.dataset.theme === 'dark'
      || (document.documentElement.dataset.theme === ''
          && window.matchMedia('(prefers-color-scheme: dark)').matches);
    const next = dark ? 'light' : 'dark';
    try { localStorage.setItem(THEME_KEY, next); } catch { /* private window */ }
    applyTheme(next);
  });
}

// --- health banner --------------------------------------------------------
//
// Spec 9.6: always visible, never hidden, collapsed or dismissible. Until the
// integrity engine has anything to say it reports the state of the archive
// itself, so the strip is never an empty space the user learns to ignore.

async function refreshHealth() {
  const host = document.getElementById('health-banner');
  if (!host) return;
  try {
    const [summary, health] = await Promise.all([
      api.sourcesSummary(),
      api.findingsSummary(),
    ]);
    const c = summary.counts || {};

    let level = 'clear';
    let text;

    if (health.total_open) {
      // The findings are the truth about the archive's health. The banner says
      // what they say, at the severity they carry - it never softens a critical
      // finding into a reassuring sentence.
      level = health.worst_severity === 'critical' ? 'critical'
        : health.worst_severity === 'high' ? 'high'
          : health.worst_severity === 'medium' ? 'medium' : 'info';
      text = health.sentence;
    } else if (!c.n) {
      level = 'info';
      text = 'Nothing has been found yet. Start with "Find Outlook files on this computer".';
    } else if (!c.parsed) {
      level = 'info';
      text = `${c.n} file(s) found, none read yet. Nothing is in the archive so far.`;
    } else {
      text = 'No problems have been found in what has been read so far.';
    }

    const extras = [];
    if (c.placeholders) extras.push(`${c.placeholders} file(s) are in the cloud only`);
    if (c.uncompared) extras.push(`${c.uncompared} not yet compared for duplicates`);
    if (extras.length) text += ` · ${extras.join(' · ')}`;

    clear(host);
    host.className = `health health--${level}`;
    host.append(
      el('span', { class: 'health__label' }, 'Archive health:'),
      el('span', { class: 'health__text' }, text),
      el('a', { class: 'health__link', href: '#/problems' },
        health.total_open ? `See all ${health.total_open} problems` : 'See all problems'),
    );
  } catch (err) {
    clear(host);
    host.className = 'health health--critical';
    host.append(
      el('span', { class: 'health__label' }, 'Archive health:'),
      el('span', { class: 'health__text' },
        err instanceof ApiError ? err.message : 'The health of the archive could not be checked.'),
    );
  }
}

// --- routing --------------------------------------------------------------

// The screens that exist, in the order they appear across the top. Each entry
// is added here as its screen is built, so the navigation never offers a door
// that opens onto nothing.
const NAV = [
  { path: '/', label: 'Home' },
  { path: '/sources', label: 'Files found' },
  { path: '/timeline', label: 'Timeline' },
  { path: '/people', label: 'People' },
];

const routes = {
  '': () => import('./screens/home.js'),
  '/': () => import('./screens/home.js'),
  '/sources': () => import('./screens/sources.js'),
  '/timeline': () => import('./screens/timeline.js'),
  '/people': () => import('./screens/people.js'),
};

function buildNav() {
  const nav = document.getElementById('nav');
  if (!nav) return;
  clear(nav);
  for (const entry of NAV) {
    nav.append(el('a', { class: 'nav__link', href: `#${entry.path}` }, entry.label));
  }
}

let teardown = null;

function parseHash() {
  const raw = window.location.hash.replace(/^#/, '') || '/';
  const [pathPart, queryPart] = raw.split('?');
  const segments = pathPart.split('/').filter(Boolean);
  const base = segments.length ? '/' + segments[0] : '/';
  return {
    base,
    segments: segments.slice(1),
    params: new URLSearchParams(queryPart || ''),
    raw,
  };
}

function markNav(base) {
  document.querySelectorAll('.nav__link').forEach((link) => {
    const target = link.getAttribute('href').replace(/^#/, '') || '/';
    const active = target === base;
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
}

async function route() {
  const { base, segments, params } = parseHash();
  markNav(base);

  if (typeof teardown === 'function') {
    try { teardown(); } catch { /* a screen that fails to tidy up must not block the next one */ }
    teardown = null;
  }

  const loader = routes[base];
  if (!loader) {
    mount(el('div', {},
      el('h1', { class: 'page__title' }, 'That page does not exist'),
      el('p', { class: 'page__lede' },
        'The address in the bar does not match any screen in Recall.'),
      el('a', { class: 'btn btn--primary', href: '#/' }, 'Go to Home'),
    ));
    setTitle('Not found');
    return;
  }

  mount(loading());
  try {
    const module = await loader();
    teardown = await module.render({ segments, params });
  } catch (err) {
    console.error(err);
    mount(errorNotice(err));
  }
  document.getElementById('main').focus({ preventScroll: true });
  refreshHealth();
}

// --- start ----------------------------------------------------------------

initTheme();
buildNav();
window.addEventListener('hashchange', route);
window.addEventListener('DOMContentLoaded', route);
if (document.readyState !== 'loading') route();

// Keep the banner honest while a long job changes the numbers underneath it.
setInterval(refreshHealth, 15000);

export { route };
