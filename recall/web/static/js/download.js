// Downloading a file, with a bar that means something.
//
// A workbook is built before any of it can be sent: on a real archive that is
// minutes of reading records out and writing them into sheets, and a plain
// link shows nothing at all through it. Nothing on screen, nothing in the
// downloads tray, no way to tell a slow export from a broken button - so it
// gets pressed again, and again.
//
// So the page asks the server to build the file, watches it being built with
// the same bar the reading jobs use, and lets the browser save it when it is
// there. The bar never invents a number: while the file is being zipped up
// there is nothing to count, and it says so rather than sitting at 99%.

import { ApiError, api } from './api.js';
import { clear, el, errorNotice, notice, num, progressBar } from './ui.js';

//: Often enough that the bar moves, rarely enough to leave the build alone.
const POLL_MS = 800;

/**
 * Build the export described by `params`, then hand the file to the browser.
 *
 * `button` is the one that was pressed: it is disabled and relabelled while
 * this runs, because only one file is built at a time and a second press
 * would get nothing but the server saying so. `host` is where the bar goes.
 */
export async function downloadWithProgress(button, params, host) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = 'Preparing…';
  clear(host);

  const panel = el('div', { class: 'card' });
  host.append(panel);

  const draw = (status) => {
    clear(panel);
    panel.append(
      el('p', { class: 'mt-0' }, status.message),
      progressBar(status),
    );
  };

  try {
    let status = await api.exportPrepare(params);
    draw(status);

    while (status.state === 'running') {
      await pause(POLL_MS);
      status = await api.exportProgress(status.token);
      draw(status);
    }

    if (status.state !== 'ready') {
      // The server's own words, with its traceback behind the disclosure.
      throw new ApiError(status.message, 0, status.error ? { error: status.error } : null);
    }

    saveFile(api.exportDownloadUrl({ token: status.token }));

    clear(host);
    host.append(notice(status.is_complete ? 'good' : 'warning',
      `${num(status.records)} record(s) ready`,
      el('p', {}, status.message),
      el('p', { class: 'muted mb-0' },
        `Your browser is saving ${fileName(status.file)}. A copy is kept in `
        + 'Recall’s own exports folder as well, so nothing is lost if you '
        + 'cannot find the download.'),
    ));
  } catch (err) {
    clear(host);
    host.append(errorNotice(err));
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

function pause(ms) {
  return new Promise((resolve) => { setTimeout(resolve, ms); });
}

// A link the page clicks on the user's behalf. The file is already built, so
// this is the browser's ordinary save, with its own progress and its own
// question about where to put it.
function saveFile(url) {
  const link = el('a', { href: url, download: '' });
  document.body.append(link);
  link.click();
  link.remove();
}

function fileName(path) {
  return path ? String(path).split(/[\\/]/).pop() : 'the file';
}
