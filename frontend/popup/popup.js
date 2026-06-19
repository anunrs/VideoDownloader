// ─── Elements ─────────────────────────────────────────────────────────────────

const stateEmpty   = document.getElementById('state-empty');
const stateLoading = document.getElementById('state-loading');
const streamList   = document.getElementById('stream-list');
const footerCount  = document.getElementById('footer-count');
const btnRefresh   = document.getElementById('btn-refresh');

// ─── Init ─────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', loadStreams);
btnRefresh.addEventListener('click', loadStreams);

// ─── Load streams from background ────────────────────────────────────────────

function loadStreams() {
  showState('loading');

  chrome.runtime.sendMessage({ type: 'GET_STREAMS' }, (response) => {
    const streams = response?.streams ?? [];

    if (streams.length === 0) {
      showState('empty');
      footerCount.textContent = '';
    } else {
      renderStreams(streams);
    }
  });
}

// ─── Render stream list ───────────────────────────────────────────────────────

function renderStreams(streams) {
  streamList.innerHTML = '';

  streams.forEach((stream) => {
    const li = document.createElement('li');
    li.className = 'stream-item';

    const badgeClass    = `badge-${stream.type.toLowerCase()}`;
    const ds            = stream.downloadState; // 'downloading' | 'done' | 'failed' | null
    const isDownloading = ds === 'downloading';
    const isDone        = ds === 'done';
    const isActive      = isDownloading || isDone;
    const btnLabel      = isDone ? 'Done \u2713' : isDownloading ? 'Downloading \u2193' : 'Download';
    const displayUrl    = friendlyUrl(stream.url, stream.type);

    li.innerHTML = `
      <span class="badge ${badgeClass}">${stream.type}</span>
      <span class="stream-info">
        <span class="stream-url" title="${escHtml(stream.url)}">${escHtml(displayUrl)}</span>
      </span>
      <button class="btn-download" data-url="${escHtml(stream.url)}" data-type="${escHtml(stream.type)}"
        ${isActive ? 'disabled' : ''}>
        ${btnLabel}
      </button>
    `;

    const btn = li.querySelector('.btn-download');
    if (isDone)        btn.classList.add('success');
    if (isDownloading) btn.classList.add('done');
    btn.addEventListener('click', onDownloadClick);
    streamList.appendChild(li);

    if (isDone) {
      showHint(btn, 'Saved to ~/Downloads/VideoDownloader.');
    } else if (isDownloading) {
      showHint(btn, 'Downloading via companion app \u2014 file will appear in ~/Downloads/VideoDownloader.');
    }
  });

  showState('list');
  footerCount.textContent = `${streams.length} stream${streams.length !== 1 ? 's' : ''} found`;

  // If any stream is still downloading, keep polling for completion
  if (streams.some(s => s.downloadState === 'downloading')) {
    startPolling();
  }
}

// ─── Download click handler ───────────────────────────────────────────────────

const DOWNLOAD_TIMEOUT_MS = 12000; // 12 s — enough for native host to connect

function onDownloadClick(e) {
  const btn        = e.currentTarget;
  const url        = btn.dataset.url;
  const streamType = btn.dataset.type;

  btn.disabled    = true;
  btn.textContent = 'Starting\u2026';

  // Safety net: if background never replies (e.g. service worker died),
  // reset the button so the user isn't stuck.
  const timeoutId = setTimeout(() => {
    setFailed(btn, 'No response from background. Try refreshing.');
  }, DOWNLOAD_TIMEOUT_MS);

  chrome.runtime.sendMessage({ type: 'DOWNLOAD', url, streamType }, (response) => {
    clearTimeout(timeoutId);

    // Message channel broke (service worker died, etc.)
    if (chrome.runtime.lastError || !response) {
      setFailed(btn, chrome.runtime.lastError?.message ?? 'No response. Reload the extension.');
      return;
    }

    if (response.success) {
      btn.textContent = 'Downloading \u2193';
      btn.classList.add('done');
      const hint = response.via === 'native'
        ? 'Downloading via companion app \u2014 file will appear in ~/Downloads/VideoDownloader.'
        : 'Check the Chrome download bar at the bottom of the window.';
      showHint(btn, hint);
      if (response.via === 'native') startPolling();
    } else if (response.canUseCompanion) {
      // Browser couldn't fetch it directly (CDN token, auth, etc.)
      setFailed(btn, 'Direct download blocked by CDN. Install the companion app for this type.');
    } else {
      // Native messaging / companion app error
      const isNotInstalled = response.error?.toLowerCase().includes('not installed') ||
                             response.error?.toLowerCase().includes('not found');
      if (isNotInstalled) {
        setFailed(btn, 'Companion app not set up. Run backend/install/install.bat first.');
      } else {
        const logNote = response.logFile
          ? `\nFull log: ${response.logFile}`
          : '';
        // Show last line of error (most specific) + log file location
        const errLines = (response.error || 'Download failed.').trim().split('\n');
        const shortErr = errLines[errLines.length - 1] || errLines[0];
        setFailed(btn, shortErr + logNote);
      }
    }
  });
}

function setFailed(btn, message) {
  btn.textContent = 'Failed';
  btn.classList.add('failed');
  btn.disabled = true;
  showHint(btn, message);
  setTimeout(() => {
    btn.disabled    = false;
    btn.textContent = 'Download';
    btn.classList.remove('failed');
    btn.title = '';
    // Remove the hint row
    const hint = btn.closest('li')?.nextElementSibling;
    if (hint?.classList.contains('hint-row')) hint.remove();
  }, 5000);
}

// ─── Polling for completion ───────────────────────────────────────────────────

let _pollTimer = null;

function startPolling() {
  if (_pollTimer) return;
  _pollTimer = setInterval(pollDownloadStatus, 2000);
}

function stopPolling() {
  clearInterval(_pollTimer);
  _pollTimer = null;
}

function pollDownloadStatus() {
  chrome.runtime.sendMessage({ type: 'GET_STREAMS' }, (response) => {
    const streams = response?.streams ?? [];
    let anyDownloading = false;

    streamList.querySelectorAll('.btn-download').forEach((btn) => {
      const url    = btn.dataset.url;
      const stream = streams.find(s => s.url === url);
      if (!stream) return;

      const ds = stream.downloadState;

      if (ds === 'downloading') {
        anyDownloading = true;
        return;
      }

      if (ds === 'done' && !btn.classList.contains('success')) {
        btn.textContent = 'Done \u2713';
        btn.classList.remove('done');
        btn.classList.add('success');
        // Swap the "Downloading..." hint to a completion message
        const li   = btn.closest('li');
        const prev = li?.nextElementSibling;
        if (prev?.classList.contains('hint-row')) prev.remove();
        showHint(btn, 'Saved to ~/Downloads/VideoDownloader.');
      } else if (ds === 'failed' && !btn.classList.contains('failed')) {
        setFailed(btn, 'Download failed. Check log: ~/Downloads/VideoDownloader/logs/downloader.log');
      }
    });

    if (!anyDownloading) stopPolling();
  });
}

// ─── UI helpers ───────────────────────────────────────────────────────────────

// Inserts a small info row below the stream item with a message
function showHint(btn, message) {
  const li = btn.closest('li');
  if (!li) return;
  // Remove any previous hint
  const prev = li.nextElementSibling;
  if (prev?.classList.contains('hint-row')) prev.remove();

  const row = document.createElement('li');
  row.className = 'hint-row';
  row.textContent = message;
  li.insertAdjacentElement('afterend', row);
}

function showState(state) {
  stateEmpty.classList.add('hidden');
  stateLoading.classList.add('hidden');
  streamList.classList.add('hidden');

  if (state === 'empty')   stateEmpty.classList.remove('hidden');
  if (state === 'loading') stateLoading.classList.remove('hidden');
  if (state === 'list')    streamList.classList.remove('hidden');
}

// Returns a shorter display string for well-known URL patterns.
function friendlyUrl(url, type) {
  try {
    const u = new URL(url);
    if (type === 'YouTube') {
      const v = u.searchParams.get('v');
      return v ? `youtube.com/watch?v=${v}` : url;
    }
    if (type === 'Instagram') {
      // Show e.g. "instagram.com/reel/ABC123/"
      return `instagram.com${u.pathname}`;
    }
  } catch {}
  return url;
}

function escHtml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
