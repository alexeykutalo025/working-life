// Choosing a folder, laid out the way Windows File Explorer lays it out.
//
// The person this is built for has used Explorer for thirty years. Borrowing
// its shape means there is nothing new to learn: a tree of folders down the
// left, the contents on the right, an address bar across the top, and buttons
// to switch how the contents are shown.
//
// Where it deliberately differs from Explorer:
//
//   * One click opens a folder, and one click chooses a file. Explorer uses a
//     single click to select and a double click to open, a distinction that
//     catches people out. Here there is nothing to open a file *into*, so the
//     two actions never collide.
//   * The tree on the left holds folders only. A tree of files would be
//     unusable on a real mailbox folder, and files are chosen on the right.
//   * A file Recall has no reader for is still listed, greyed, saying so.
//     Hiding it would leave somebody hunting for a file that is right there.
//
// "Thumbnail view" is called Tiles here. Explorer shows a thumbnail for a
// picture or a document; a folder has no thumbnail to show, so that view
// renders a large folder icon and the name, which is what Explorer does too.

import { api } from './api.js';
import { date, el, clear, errorNotice, loading, modal, notice, plural } from './ui.js';

const SVG_NS = 'http://www.w3.org/2000/svg';

/** Which view the user last chose. Remembered per browser, never uploaded. */
const VIEW_KEY = 'recall.folderView';

const VIEWS = [
  { id: 'tiles', label: 'Tiles', hint: 'Large icons, easiest to see' },
  { id: 'list', label: 'List', hint: 'Names only, several to a row' },
  { id: 'details', label: 'Details', hint: 'Name, date changed and type' },
];

function storedView() {
  try {
    const stored = localStorage.getItem(VIEW_KEY);
    return VIEWS.some((v) => v.id === stored) ? stored : 'tiles';
  } catch {
    return 'tiles';
  }
}

function rememberView(id) {
  try { localStorage.setItem(VIEW_KEY, id); } catch { /* private window */ }
}

// --- icons ----------------------------------------------------------------
//
// Drawn rather than loaded: there is no network at runtime, so there are no
// icon fonts and no image files. Every icon is decorative and sits beside a
// name, so each is hidden from a screen reader rather than described twice.

function folderIcon(size = 20, { open = false } = {}) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  svg.classList.add('folder-icon');

  const body = document.createElementNS(SVG_NS, 'path');
  body.setAttribute('d', open
    ? 'M3 6.5A1.5 1.5 0 0 1 4.5 5h4.2l1.8 2h7A1.5 1.5 0 0 1 19 8.5V9H7.6a2 '
      + '2 0 0 0-1.9 1.4L3 19z'
    : 'M3 6.5A1.5 1.5 0 0 1 4.5 5h4.2l1.8 2h7A1.5 1.5 0 0 1 19 8.5v9a1.5 '
      + '1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 3 17.5z');
  body.setAttribute('fill', 'currentColor');
  svg.append(body);

  if (open) {
    const lid = document.createElementNS(SVG_NS, 'path');
    lid.setAttribute('d', 'M6.2 10.5h14.3L18 19H3.5z');
    lid.setAttribute('fill', 'currentColor');
    lid.setAttribute('opacity', '0.75');
    svg.append(lid);
  }
  return svg;
}

function driveIcon(size = 20) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  svg.classList.add('folder-icon');

  const box = document.createElementNS(SVG_NS, 'rect');
  box.setAttribute('x', '3'); box.setAttribute('y', '6');
  box.setAttribute('width', '18'); box.setAttribute('height', '12');
  box.setAttribute('rx', '2');
  box.setAttribute('fill', 'currentColor');
  svg.append(box);

  const light = document.createElementNS(SVG_NS, 'circle');
  light.setAttribute('cx', '17.5'); light.setAttribute('cy', '12');
  light.setAttribute('r', '1.3');
  light.setAttribute('fill', 'var(--bg)');
  svg.append(light);
  return svg;
}

function fileIcon(size = 20, { readable = true } = {}) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  svg.classList.add('folder-icon', readable ? 'file-icon' : 'file-icon--unknown');

  const sheet = document.createElementNS(SVG_NS, 'path');
  sheet.setAttribute('d', 'M6 3h7.2L19 8.6V20a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z');
  sheet.setAttribute('fill', 'currentColor');
  svg.append(sheet);

  const fold = document.createElementNS(SVG_NS, 'path');
  fold.setAttribute('d', 'M13 3l6 5.6h-5a1 1 0 0 1-1-1z');
  fold.setAttribute('fill', 'var(--bg)');
  fold.setAttribute('opacity', '0.55');
  svg.append(fold);
  return svg;
}

/** The right icon for a listing entry: drive, folder, or file. */
function iconForEntry(entry, size) {
  if (entry.kind === 'file') {
    return fileIcon(size, { readable: entry.readable_kind !== false });
  }
  return iconFor(entry.path, size);
}

function iconFor(path, size) {
  return isDriveRoot(path) ? driveIcon(size) : folderIcon(size);
}

function bytesShort(n) {
  if (n === null || n === undefined) return '';
  if (n === 0) return '0 bytes';
  const units = ['bytes', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(i === 0 ? 0 : (v < 10 ? 1 : 0))} ${units[i]}`;
}

/** Why a file cannot be chosen, or null when it can. */
function whyNotChoosable(entry) {
  if (entry.kind !== 'file') return null;
  if (entry.readable_kind === false) {
    return 'Recall has no reader for this kind of file';
  }
  return null;
}

function isDriveRoot(path) {
  return /^[A-Za-z]:[\\/]?$/.test(String(path || '').trim());
}

export function folderName(path) {
  const parts = String(path).replace(/[\\/]+$/, '').split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

// --- the picker -----------------------------------------------------------

/**
 * Open the folder chooser.
 *
 * `onChoose(path)` is called with the folder the user settled on, once.
 */
export function openFolderPicker(onChoose) {
  const state = {
    path: null,             // null means the top, where the drives are
    listing: null,
    view: storedView(),
    history: [],            // where we have been, for the Back button
    loadedTree: new Map(),  // path -> entries, so the tree is fetched once
    expanded: new Set(),
    // Everything ticked so far, as path -> {path, name, kind}. It survives
    // moving about, which is the whole point: somebody whose mail is spread
    // over three folders and two loose files should be able to gather all five
    // and search them in one go.
    chosen: new Map(),
    // Where the last tick was, so shift-clicking can fill in the range - the
    // difference between ticking forty files and ticking one forty times.
    anchor: null,
  };

  // Which button belongs to which folder, so focus can be put back where it
  // was after the tree is redrawn.
  const twistyFor = new Map();
  const branchFor = new Map();

  const address = el('div', { class: 'picker__address' });
  const tree = el('nav', { class: 'picker__tree', 'aria-label': 'Folders' });
  const content = el('div', { class: 'picker__content' });
  const viewRow = el('div', { class: 'picker__views' });
  const status = el('div', { class: 'picker__status' });
  const basket = el('div', { class: 'picker__basket' });
  const tickAll = el('div', { class: 'picker__tick-all-row' });

  const back = el('button', {
    class: 'btn', type: 'button', disabled: true,
    onclick: () => {
      const previous = state.history.pop();
      if (previous !== undefined) go(previous, { remember: false });
    },
  }, '← Back');

  const up = el('button', {
    class: 'btn', type: 'button', disabled: true,
    onclick: () => go(state.listing ? state.listing.parent : null),
  }, '↑ Up');

  const choose = el('button', {
    class: 'btn btn--primary', type: 'button', disabled: true,
    onclick: () => {
      // Nothing ticked means "the folder I am standing in", which keeps the
      // simple case simple: open a folder, press the button, done.
      const paths = state.chosen.size
        ? [...state.chosen.keys()]
        : (state.path ? [state.path] : []);
      if (!paths.length) return;
      onChoose(paths);
      dialog.close();
    },
  }, 'Search this folder');

  // --- moving about -------------------------------------------------------

  async function go(path, { remember = true, focusTree = false } = {}) {
    if (remember && state.path !== path) {
      state.history.push(state.path);
      if (state.history.length > 50) state.history.shift();
    }

    clear(content);
    content.append(loading('Opening the folder'));

    let listing;
    try {
      listing = await api.browseFolders(path || '');
    } catch (err) {
      clear(content);
      content.append(errorNotice(err));
      return;
    }

    state.path = listing.path;
    state.listing = listing;
    state.anchor = null;
    state.loadedTree.set(listing.path || '', listing.entries);

    // Everything above the current folder is open in the tree, so the user can
    // always see where they are in relation to everything else.
    for (const crumb of listing.crumbs || []) state.expanded.add(crumb.path);
    state.expanded.add('');

    drawAddress();
    drawContent();
    // Only put focus back in the tree when the tree is where the click came
    // from; stealing it after a tile or a breadcrumb would be worse than
    // losing it.
    drawTree(focusTree ? { focusPath: state.path || '', focusKind: 'branch' } : {});
    drawButtons();
  }

  function drawButtons() {
    back.disabled = state.history.length === 0;
    up.disabled = !(state.listing && state.listing.parent) && !state.path;
    if (state.listing && !state.listing.parent && state.path) up.disabled = false;

    const ticked = state.chosen.size;
    if (ticked) {
      choose.disabled = false;
      choose.textContent = ticked === 1
        ? `Search ${folderName([...state.chosen.keys()][0])}`
        : `Search these ${ticked}`;
      choose.title = [...state.chosen.keys()].join(', ');
    } else {
      choose.disabled = !state.path;
      choose.textContent = state.path
        ? `Search ${folderName(state.path)}`
        : 'Search this folder';
      choose.title = state.path || '';
    }
    drawBasket();
  }

  // --- the address bar ----------------------------------------------------

  function drawAddress() {
    clear(address);
    const crumb = (label, path, isLast) => el('button', {
      class: `btn btn--quiet picker__crumb${isLast ? ' is-here' : ''}`,
      type: 'button',
      'aria-current': isLast ? 'true' : null,
      onclick: () => go(path),
    }, label);

    const crumbs = (state.listing && state.listing.crumbs) || [];
    address.append(crumb('This computer', '', crumbs.length === 0));
    crumbs.forEach((c, i) => {
      address.append(el('span', { class: 'picker__sep', 'aria-hidden': 'true' }, '›'));
      address.append(crumb(c.name, c.path, i === crumbs.length - 1));
    });
  }

  // --- the tree down the left --------------------------------------------

  /**
   * Redraw the tree, putting the user back where they were.
   *
   * The whole tree is rebuilt on every expand, which is cheap enough at these
   * sizes but throws away two things the user cares about: how far down they
   * had scrolled, and what the keyboard was on. Without restoring focus, every
   * press of a twisty dumps a keyboard user back at the top of the dialog.
   */
  function drawTree({ focusPath = null, focusKind = 'twisty' } = {}) {
    const scroll = tree.scrollTop;

    // The tree is rebuilt from scratch, so the record of which button belongs
    // to which folder is rebuilt with it. Finding them by selector instead
    // would mean escaping a Windows path into a CSS selector, and a Windows
    // path is mostly backslashes.
    twistyFor.clear();
    branchFor.clear();

    clear(tree);
    tree.append(treeNode({ name: 'This computer', path: '' }, 0, true));
    tree.scrollTop = scroll;

    if (focusPath === null) return;
    const target = (focusKind === 'twisty' ? twistyFor : branchFor).get(focusPath);
    if (target) target.focus({ preventScroll: true });
  }

  function treeNode(entry, depth, isRoot = false) {
    const path = entry.path;
    const open = state.expanded.has(path);
    const here = (state.path || '') === path;
    const children = state.loadedTree.get(path);

    const row = el('div', { class: 'picker__node' });

    // The twisty. Whether a folder has anything inside it is only known once
    // it has been opened, so every folder gets one and an empty folder simply
    // opens onto nothing - far cheaper than scanning the whole tree up front.
    const twisty = el('button', {
      class: 'picker__twisty',
      type: 'button',
      'aria-expanded': open ? 'true' : 'false',
      'aria-label': `${open ? 'Collapse' : 'Expand'} ${entry.name}`,
      onclick: async (e) => {
        e.stopPropagation();
        if (open) {
          state.expanded.delete(path);
        } else {
          state.expanded.add(path);
          if (!state.loadedTree.has(path)) {
            // Show the twisty as open straight away, so a slow folder does not
            // look like a press that did nothing.
            drawTree({ focusPath: path });
            await loadForTree(path);
          }
        }
        drawTree({ focusPath: path });
      },
    }, open ? '▾' : '▸');
    twistyFor.set(path, twisty);
    row.append(twisty);

    const branch = el('button', {
      class: `picker__branch${here ? ' is-here' : ''}`,
      type: 'button',
      'aria-current': here ? 'true' : null,
      onclick: () => go(path, { focusTree: true }),
    },
      isRoot ? driveIcon(18) : iconFor(path, 18),
      el('span', { class: 'picker__branch-name' }, entry.name),
    );
    branchFor.set(path, branch);
    row.append(branch);

    const node = el('div', { class: 'picker__level' }, row);
    node.style.setProperty('--depth', String(depth));

    if (open) {
      if (children === undefined) {
        node.append(el('div', { class: 'picker__level-children' },
          el('p', { class: 'muted small mb-0 picker__loading' }, 'Opening…')));
      } else if (!children.length) {
        node.append(el('div', { class: 'picker__level-children' },
          el('p', { class: 'muted small mb-0 picker__loading' }, 'Nothing inside')));
      } else {
        const kids = el('div', { class: 'picker__level-children' });
        // Folders only down here. A tree of files would be unusable, and the
        // contents pane on the right is where files are chosen.
        const folders = children.filter((c) => c.kind !== 'file');
        if (!folders.length) {
          kids.append(el('p', { class: 'muted small mb-0 picker__loading' },
            'No folders inside'));
        }
        for (const child of folders) kids.append(treeNode(child, depth + 1));
        node.append(kids);
      }
    }
    return node;
  }

  async function loadForTree(path) {
    try {
      const listing = await api.browseFolders(path || '');
      state.loadedTree.set(path, listing.readable ? listing.entries : []);
    } catch {
      // A folder the tree cannot open is shown as empty rather than as an
      // error box inside a sidebar; opening it properly says why.
      state.loadedTree.set(path, []);
    }
  }

  // --- the contents pane --------------------------------------------------

  function drawViews() {
    clear(viewRow);
    viewRow.append(el('span', { class: 'muted small' }, 'Show as'));
    for (const v of VIEWS) {
      viewRow.append(el('button', {
        class: `btn ${state.view === v.id ? 'btn--primary' : ''}`,
        type: 'button',
        title: v.hint,
        'aria-pressed': state.view === v.id ? 'true' : 'false',
        onclick: () => {
          if (state.view === v.id) return;
          state.view = v.id;
          rememberView(v.id);
          drawContent();
          drawViews();
        },
      }, v.label));
    }
  }

  function drawContent() {
    const listing = state.listing;
    clear(content);
    clear(status);
    drawViews();

    if (!listing) return;

    if (!listing.readable) {
      content.append(notice('warning', 'Recall cannot open this folder', listing.note));
      return;
    }

    if (!listing.entries.length) {
      content.append(el('div', { class: 'empty' },
        el('div', { class: 'empty__title' }, 'Nothing inside this folder'),
        el('p', { class: 'mb-0' },
          'That is not a problem — you can still search this folder. Press the '
          + 'button below.')));
      return;
    }

    // A folder is opened by clicking it; a file is ticked, because there is
    // nothing to open it into. Either way the tick box does the same job for
    // both, which is what makes gathering several possible.
    const activate = (entry) => {
      if (entry.kind === 'file') {
        toggle(entry);
        return;
      }
      go(entry.path);
    };

    clear(tickAll);
    const all = tickAllRow();
    if (all) tickAll.append(all);

    if (state.view === 'details') content.append(detailsView(listing, activate));
    else if (state.view === 'list') content.append(listView(listing, activate));
    else content.append(tilesView(listing, activate));

    const folders = listing.entries.filter((e) => e.kind !== 'file').length;
    const files = listing.entries.length - folders;
    const counted = [];
    if (folders) counted.push(plural(folders, 'folder'));
    if (files) counted.push(plural(files, 'file'));
    status.append(el('span', { class: 'muted small' }, `${counted.join(' and ')} inside`));

    const skipped = listing.entries.filter((e) => e.excluded_by_default).length;
    if (skipped) {
      status.append(el('span', { class: 'muted small' },
        `· ${skipped} normally skipped, marked below`));
    }

  }

  // --- ticking things -----------------------------------------------------

  function toggle(entry, { shift = false } = {}) {
    if (whyNotChoosable(entry)) return;
    const entries = (state.listing && state.listing.entries) || [];

    if (shift && state.anchor !== null) {
      // Shift fills in everything between the last tick and this one, the way
      // it does in Explorer. Forty files in two clicks rather than forty.
      const from = entries.findIndex((e) => e.path === state.anchor);
      const to = entries.findIndex((e) => e.path === entry.path);
      if (from !== -1 && to !== -1) {
        const want = !state.chosen.has(entry.path);
        for (let i = Math.min(from, to); i <= Math.max(from, to); i += 1) {
          const item = entries[i];
          if (whyNotChoosable(item)) continue;
          if (want) state.chosen.set(item.path, pick(item));
          else state.chosen.delete(item.path);
        }
        state.anchor = entry.path;
        drawContent();
        drawButtons();
        return;
      }
    }

    if (state.chosen.has(entry.path)) state.chosen.delete(entry.path);
    else state.chosen.set(entry.path, pick(entry));

    state.anchor = entry.path;
    drawContent();
    drawButtons();
  }

  function pick(entry) {
    return { path: entry.path, name: entry.name, kind: entry.kind || 'folder' };
  }

  /** A tick box, the same in all three views. */
  function tickBox(entry) {
    const why = whyNotChoosable(entry);
    return el('input', {
      type: 'checkbox',
      class: 'picker__tick',
      checked: state.chosen.has(entry.path),
      disabled: why ? true : null,
      'aria-label': `Search ${entry.name}`,
      onclick: (e) => {
        e.stopPropagation();       // ticking is not opening
        toggle(entry, { shift: e.shiftKey });
      },
    });
  }

  /** Tick or untick everything in this folder at once. */
  function tickAllRow() {
    const entries = ((state.listing && state.listing.entries) || [])
      .filter((e) => !whyNotChoosable(e));
    if (!entries.length) return null;

    const allTicked = entries.every((e) => state.chosen.has(e.path));

    return el('label', { class: 'check picker__tick-all' },
      el('input', {
        type: 'checkbox',
        checked: allTicked,
        onchange: () => {
          for (const entry of entries) {
            if (allTicked) state.chosen.delete(entry.path);
            else state.chosen.set(entry.path, pick(entry));
          }
          drawContent();
          drawButtons();
        },
      }),
      el('span', {}, allTicked
        ? 'Untick everything in this folder'
        : 'Tick everything in this folder'),
    );
  }

  /** What has been gathered so far, and a way to take any of it back out. */
  function drawBasket() {
    clear(basket);
    if (!state.chosen.size) {
      basket.append(el('p', { class: 'muted small mb-0' },
        'Nothing ticked. Pressing the button searches the folder you are in. '
        + 'Tick folders and files to gather several together.'));
      return;
    }

    basket.append(el('div', { class: 'picker__basket-head' },
      el('strong', {}, `${state.chosen.size} chosen`),
      el('button', {
        class: 'btn btn--quiet', type: 'button',
        onclick: () => {
          state.chosen.clear();
          drawContent();
          drawButtons();
        },
      }, 'Clear them all'),
    ));

    const list = el('ul', { class: 'picker__basket-list' });
    for (const item of state.chosen.values()) {
      list.append(el('li', {},
        iconForEntry(item, 16),
        el('span', { class: 'picker__basket-name', title: item.path }, item.name),
        el('button', {
          class: 'btn btn--quiet', type: 'button',
          'aria-label': `Take ${item.name} out of the list`,
          onclick: () => {
            state.chosen.delete(item.path);
            drawContent();
            drawButtons();
          },
        }, 'Remove'),
      ));
    }
    basket.append(list);
  }

  function skipNote(entry) {
    if (entry.excluded_by_default) {
      return el('span', { class: 'picker__skip' }, 'normally skipped');
    }
    const why = whyNotChoosable(entry);
    return why ? el('span', { class: 'picker__skip' }, 'cannot be read') : null;
  }

  /** The classes and title every entry shares, whichever view is drawn. */
  function entryAttrs(entry, extra) {
    const why = whyNotChoosable(entry);
    const chosen = state.chosen.has(entry.path);
    return {
      class: `${extra}${chosen ? ' is-selected' : ''}${why ? ' is-unreadable' : ''}`,
      type: 'button',
      disabled: why ? true : null,
      title: why ? `${entry.path} — ${why}` : entry.path,
    };
  }

  function tilesView(listing, activate) {
    const grid = el('div', { class: 'picker__tiles' });
    for (const entry of listing.entries) {
      grid.append(el('div', { class: 'picker__tile-wrap' },
        tickBox(entry),
        el('button', {
          ...entryAttrs(entry, 'picker__tile'),
          onclick: () => activate(entry),
        },
          iconForEntry(entry, 44),
          el('span', { class: 'picker__tile-name' }, entry.name),
          entry.kind === 'file' && entry.size !== null
            ? el('span', { class: 'picker__tile-note' }, bytesShort(entry.size))
            : null,
          skipNote(entry),
        ),
      ));
    }
    return grid;
  }

  function listView(listing, activate) {
    const wrap = el('div', { class: 'picker__list' });
    for (const entry of listing.entries) {
      wrap.append(el('div', { class: 'picker__list-row' },
        tickBox(entry),
        el('button', {
          ...entryAttrs(entry, 'picker__list-item'),
          onclick: () => activate(entry),
        },
          iconForEntry(entry, 18),
          el('span', { class: 'picker__list-name' }, entry.name),
          skipNote(entry),
        ),
      ));
    }
    return wrap;
  }

  function detailsView(listing, activate) {
    const body = el('tbody', {});
    for (const entry of listing.entries) {
      const why = whyNotChoosable(entry);
      const chosen = state.chosen.has(entry.path);

      body.append(el('tr', {
        class: `${why ? 'is-unreadable' : 'is-clickable'}${chosen ? ' is-selected' : ''}`,
        onclick: why ? null : () => activate(entry),
        title: why ? `${entry.path} — ${why}` : entry.path,
      },
        el('td', { class: 'col-check' }, tickBox(entry)),
        el('td', {},
          el('span', { class: 'picker__cell-name' },
            iconForEntry(entry, 18),
            el('span', {}, entry.name))),
        el('td', { class: 'nowrap num' },
          entry.kind === 'file' ? bytesShort(entry.size) : ''),
        el('td', { class: 'nowrap' },
          entry.modified ? date(entry.modified) : 'not known'),
        el('td', {}, describeType(entry)),
      ));
    }

    return el('div', { class: 'table-wrap picker__details' },
      el('table', {},
        el('thead', {}, el('tr', {},
          el('th', { class: 'col-check' }, el('span', { class: 'visually-hidden' }, 'Chosen')),
          el('th', {}, 'Name'),
          el('th', { class: 'num' }, 'Size'),
          el('th', {}, 'Date changed'),
          el('th', {}, 'Type'),
        )),
        body,
      ));
  }

  /** The Type column, which is also where a folder's or file's caveat goes. */
  function describeType(entry) {
    if (entry.kind === 'file') {
      const what = entry.ext ? `${entry.ext.replace('.', '').toUpperCase()} file` : 'File';
      return entry.readable_kind === false
        ? `${what} — Recall has no reader for this kind`
        : what;
    }
    return entry.excluded_by_default
      ? 'Folder — normally skipped, but Recall will search it if you choose it'
      : 'Folder';
  }

  // --- the dialog ---------------------------------------------------------

  const dialog = modal({
    title: 'Which folder?',
    body: el('div', { class: 'picker' },
      el('p', { class: 'picker__lede' },
        'Open folders to look inside. Tick anything you want searched — as '
        + 'many folders and files as you like, from anywhere.'),
      el('div', { class: 'picker__bar' },
        el('div', { class: 'btn-row' }, back, up),
        address,
      ),
      el('div', { class: 'picker__toolbar' }, viewRow, status),
      tickAll,
      el('div', { class: 'picker__panes' }, tree, content),
      basket,
    ),
    actions: [
      choose,
      el('button', { class: 'btn', type: 'button', onclick: () => dialog.close() },
        'Cancel'),
    ],
  });

  dialog.panel.classList.add('modal--wide');
  dialog.panel.querySelector('.modal__body').classList.add('modal__body--fill');
  go('', { remember: false });
  return dialog;
}
