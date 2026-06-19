// ─── Service Worker Keep-Alive ────────────────────────────────────────────────
// MV3 service workers are killed after ~30 s of inactivity. Any pending
// sendResponse callbacks die with them. A repeating alarm wakes the SW every
// 25 s so in-flight downloads never lose their response channel.
chrome.alarms.create('sw-keepalive', { periodInMinutes: 25 / 60 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'sw-keepalive') { /* intentionally empty — just keep SW alive */ }
});

// ─── Stream Detection ────────────────────────────────────────────────────────

const VIDEO_MIME_TYPES = [
  'video/',
  'application/x-mpegurl',
  'application/vnd.apple.mpegurl',
  'application/dash+xml',
];

// File extensions that unambiguously identify video/stream files.
// Checked against the actual filename extension only (not the full URL string).
const VIDEO_EXT_SET = new Set(['.mp4', '.m3u8', '.mpd', '.webm', '.mkv', '.mov', '.flv', '.ts', '.m4v']);

// File extensions that are never video — block these before anything else.
const NON_VIDEO_EXT_SET = new Set([
  '.woff', '.woff2', '.eot', '.ttf', '.otf',
  '.css', '.js', '.mjs', '.html', '.htm',
  '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico', '.bmp', '.tiff',
  '.pdf', '.json', '.xml', '.txt', '.csv',
  '.zip', '.gz', '.tar', '.br', '.map',
]);

// URL path segments that are never video directories.
// Regex tested against the full URL (case-insensitive).
const NON_VIDEO_PATH_RE = /\/(fonts?|css|images?|img|icons?|sprites?|favicons?|static\/js)\//i;

// tabId -> Map<url, streamObject>
const detectedStreams = new Map();

// url -> { status: 'downloading' | 'done' | 'failed', tabId }
const downloadStates = new Map();

// url -> raw 'Cookie: ...' header value captured from the browser's own CDN request.
// chrome.cookies.getAll() misses partitioned / third-party cookies; intercepting
// the request headers gives us the exact Cookie string the browser sends.
const capturedStreamCookies = new Map();

// ── Helpers ───────────────────────────────────────────────────────────────────

// Extracts the filename extension from a URL's path component only.
// Strips query strings and hash fragments before checking.
// e.g. "https://cdn.example.com/fonts/lato.woff2?v=3" → ".woff2"
//      "https://cdn.example.com/hls/master.m3u8?token=x" → ".m3u8"
function urlFileExtension(rawUrl) {
  try {
    const pathname = new URL(rawUrl).pathname;
    const filename = pathname.split('/').pop() || '';
    const dot      = filename.lastIndexOf('.');
    return dot >= 0 ? filename.slice(dot).toLowerCase() : '';
  } catch {
    const path     = rawUrl.split('?')[0].split('#')[0];
    const filename = path.split('/').pop() || '';
    const dot      = filename.lastIndexOf('.');
    return dot >= 0 ? filename.slice(dot).toLowerCase() : '';
  }
}

function detectType(url, contentType) {
  const u = url.toLowerCase();
  const c = (contentType || '').toLowerCase();
  if (u.includes('.m3u8') || c.includes('mpegurl'))  return 'HLS';
  if (u.includes('.mpd')  || c.includes('dash+xml')) return 'DASH';
  if (u.includes('.mp4')  || u.includes('.m4v'))      return 'MP4';
  if (u.includes('.webm'))                            return 'WebM';
  if (u.includes('.mkv'))                             return 'MKV';
  if (u.includes('.ts'))                              return 'TS';
  return 'Video';
}

// Returns true for individual HLS segment files — useless without the manifest.
function isHlsSegment(url) {
  const ext = urlFileExtension(url);
  if (ext !== '.ts') return false;
  const u = url.toLowerCase().split('?')[0];
  return /[/\-_]\d+\.ts$/.test(u) ||
         u.includes('segment') ||
         u.includes('/seg')    ||
         u.includes('chunk');
}

// Central gate: should this URL be added to the detected stream list?
function isVideoResource(url, contentType) {
  if (!url || url.startsWith('blob:') || url.startsWith('data:')) return false;

  const ext = urlFileExtension(url);

  // Hard-block known non-video file types (by filename extension)
  if (NON_VIDEO_EXT_SET.has(ext)) return false;

  // Hard-block known non-video path directories
  if (NON_VIDEO_PATH_RE.test(url)) return false;

  // Hard-block individual HLS segment chunks
  if (isHlsSegment(url)) return false;

  // Positive matches ─────────────────────────────────────────────────────────
  // 1. Filename extension is a known video type
  if (VIDEO_EXT_SET.has(ext)) return true;

  // 2. Streaming manifests sometimes live mid-path (e.g. /video.m3u8/quality/360)
  //    Use url.includes() only for these two, as they're unambiguous
  const ul = url.toLowerCase();
  if (ul.includes('.m3u8') || ul.includes('.mpd')) return true;

  // 3. Explicit video MIME type from the server
  if (VIDEO_MIME_TYPES.some(mt => contentType.toLowerCase().startsWith(mt))) return true;

  return false;
}

function addStream(tabId, url, contentType) {
  if (!isVideoResource(url, contentType)) return;

  if (!detectedStreams.has(tabId)) detectedStreams.set(tabId, new Map());
  const streams = detectedStreams.get(tabId);
  if (streams.has(url)) return;

  const type = detectType(url, contentType);
  streams.set(url, { url, type, contentType: contentType || '', timestamp: Date.now() });
  updateBadge(tabId, streams.size);
}

// Primary detection: intercept all completed network requests
chrome.webRequest.onCompleted.addListener(
  (details) => {
    if (details.tabId < 0) return;
    const contentType = details.responseHeaders
      ?.find(h => h.name.toLowerCase() === 'content-type')?.value || '';
    addStream(details.tabId, details.url, contentType);
  },
  { urls: ['<all_urls>'] },
  ['responseHeaders']
);

// Standard headers that every browser sends and that yt-dlp/ffmpeg set themselves.
// We skip these — only capture non-standard / auth-related headers.
const STANDARD_HDR = new Set([
  'accept', 'accept-encoding', 'accept-language', 'connection', 'host',
  'user-agent', 'upgrade-insecure-requests', 'te',
  'sec-ch-ua', 'sec-ch-ua-mobile', 'sec-ch-ua-platform',
  'sec-fetch-dest', 'sec-fetch-mode', 'sec-fetch-site', 'sec-fetch-user',
  'cache-control', 'pragma',
]);

// Capture ALL non-standard request headers the browser sends to any .m3u8 / .mpd URL.
// This includes Cookie, Authorization, bearer tokens, and any custom auth headers.
// Also records which header names were present so we can diagnose auth mechanisms.
chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    const url = details.url;
    if (!url.includes('.m3u8') && !url.includes('.mpd')) return;
    const allHeaders = details.requestHeaders || [];
    const authHeaders = allHeaders.filter(h => !STANDARD_HDR.has(h.name.toLowerCase()));
    // Store even if empty so we know the listener fired
    capturedStreamCookies.set(url, {
      fired: true,
      allNames: allHeaders.map(h => h.name.toLowerCase()),
      headers: authHeaders,  // [{name, value}, ...]
    });
  },
  { urls: ['<all_urls>'] },
  ['requestHeaders', 'extraHeaders']
);

// ─── Badge ────────────────────────────────────────────────────────────────────

function updateBadge(tabId, count) {
  chrome.action.setBadgeText({ text: count > 0 ? String(count) : '', tabId });
  chrome.action.setBadgeBackgroundColor({ color: '#e94560', tabId });
}

// ─── Tab Lifecycle ────────────────────────────────────────────────────────────

chrome.tabs.onRemoved.addListener((tabId) => {
  const streams = detectedStreams.get(tabId);
  if (streams) for (const url of streams.keys()) capturedStreamCookies.delete(url);
  detectedStreams.delete(tabId);
  for (const [url, state] of downloadStates) {
    if (state.tabId === tabId) downloadStates.delete(url);
  }
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status === 'loading' && changeInfo.url) {
    // New page navigation — reset streams and download states for this tab
    const streams = detectedStreams.get(tabId);
    if (streams) for (const url of streams.keys()) capturedStreamCookies.delete(url);
    detectedStreams.delete(tabId);
    for (const [url, state] of downloadStates) {
      if (state.tabId === tabId) downloadStates.delete(url);
    }
    chrome.action.setBadgeText({ text: '', tabId });
  }
});

// ─── Messages from Popup & Content Script ────────────────────────────────────

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {

  // Content script found a <video> src
  if (message.type === 'VIDEO_FOUND' && sender.tab) {
    addStream(sender.tab.id, message.url, '');
    return; // no response needed
  }

  // Popup is requesting the stream list for the active tab
  if (message.type === 'GET_STREAMS') {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      const tabId   = tabs[0]?.id;
      const streams = tabId
        ? Array.from(detectedStreams.get(tabId)?.values() || [])
        : [];
      // Attach persisted download state so popup can restore button appearance
      const streamsWithState = streams.map(s => ({
        ...s,
        downloadState: downloadStates.get(s.url)?.status ?? null,
      }));
      sendResponse({ streams: streamsWithState });
    });
    return true; // keep channel open (async response)
  }

  // Popup is requesting a download
  if (message.type === 'DOWNLOAD') {
    // Grab the active tab URL, then collect cookies for both the page and the
    // stream URL so the companion app can authenticate with CDN/site extractors.
    // chrome.cookies returns already-decrypted values, bypassing the locked DB.
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      const pageUrl   = tabs[0]?.url || '';
      const tabId     = tabs[0]?.id ?? -1;
      const streamUrl = message.url;

      // Collect cookies for the page URL and the stream URL, then merge.
      const getCookies = (url) => new Promise((resolve) => {
        if (!url.startsWith('http')) return resolve([]);
        chrome.cookies.getAll({ url }, (c) => resolve(c || []));
      });

      Promise.all([getCookies(pageUrl), getCookies(streamUrl)]).then(([pageCookies, streamCookies]) => {
        // Deduplicate by domain+name (page cookies take precedence)
        const seen = new Map();
        for (const c of [...streamCookies, ...pageCookies]) {
          seen.set(`${c.domain}\t${c.name}`, c);
        }
        const cookies = Array.from(seen.values()).map(c => ({
          domain:   c.domain,
          path:     c.path,
          secure:   c.secure,
          expires:  c.expirationDate ? Math.floor(c.expirationDate) : 0,
          name:     c.name,
          value:    c.value,
        }));
        // Pass auth headers we intercepted from the browser's own CDN request.
        // capturedInfo.headers = [{name,value}] of non-standard headers (Cookie, Authorization, etc.)
        // capturedInfo.fired   = true if the listener ran for this URL at all
        const capturedInfo     = capturedStreamCookies.get(streamUrl);
        const capturedHeaders  = capturedInfo?.headers  || [];
        const listenerFired    = capturedInfo?.fired    || false;
        const allHeaderNames   = capturedInfo?.allNames || [];
        handleDownload(streamUrl, message.streamType, pageUrl, sendResponse, tabId,
                       cookies, capturedHeaders, listenerFired, allHeaderNames);
      });
    });
    return true;
  }
});

// ─── Download Logic ───────────────────────────────────────────────────────────

// Types that are discrete files the browser can download directly.
const DIRECT_DOWNLOAD_TYPES = new Set(['MP4', 'WebM', 'MKV', 'MOV', 'Video']);

// Types that are streaming manifests — downloading them as-is gives you a
// text playlist file, not actual video. The companion app (yt-dlp + ffmpeg)
// must assemble the segments.
const STREAMING_TYPES = new Set(['HLS', 'DASH', 'TS']);

function handleDownload(url, streamType, pageUrl, sendResponse, tabId,
                       cookies = [], capturedHeaders = [], listenerFired = false, allHeaderNames = []) {
  if (STREAMING_TYPES.has(streamType)) {
    // Respond to the popup immediately so the MV3 sendResponse channel doesn't
    // time out while yt-dlp runs (which can take minutes).
    // Errors are written to the downloader log file.
    downloadStates.set(url, { status: 'downloading', tabId });
    sendToNativeHost({
      action: 'download', url, streamType, pageUrl, cookies,
      capturedHeaders,   // [{name,value}] non-standard headers from browser's CDN request
      listenerFired,     // true if onBeforeSendHeaders fired for this URL
      allHeaderNames,    // all header names the browser sent (for diagnostics)
    }, (result) => {
      downloadStates.set(url, { status: result.success ? 'done' : 'failed', tabId });
      if (!result.success) {
        console.error('[Download] native host error:', result.error);
      }
    });
    sendResponse({ success: true, via: 'native' });
    return;
  }

  if (DIRECT_DOWNLOAD_TYPES.has(streamType)) {
    const filename = deriveFilename(url, streamType);
    chrome.downloads.download({ url, filename }, (downloadId) => {
      if (chrome.runtime.lastError) {
        // Initiation failed (bad URL, blocked scheme, etc.) — surface the error
        // clearly instead of silently retrying through the companion app
        sendResponse({
          success: false,
          error: `Browser download failed: ${chrome.runtime.lastError.message}. Try the companion app.`,
          canUseCompanion: true,
        });
      } else {
        sendResponse({ success: true, downloadId, via: 'browser' });
      }
    });
    return;
  }

  // Unknown type — attempt companion app as last resort
  sendToNativeHost({ action: 'download', url, streamType, pageUrl }, sendResponse);
}

function deriveFilename(url, type) {
  try {
    const pathname = new URL(url).pathname;
    const name     = pathname.split('/').pop() || 'video';
    return name.includes('.') ? name : `${name}.${type.toLowerCase()}`;
  } catch {
    return `video_${Date.now()}.${type.toLowerCase()}`;
  }
}

// ─── Native Messaging (Companion App) ────────────────────────────────────────

let nativePort = null;
const pendingCallbacks = new Map();
let callbackId = 0;

function sendToNativeHost(message, callback) {
  const id = ++callbackId;
  pendingCallbacks.set(id, callback);

  try {
    if (!nativePort) {
      nativePort = chrome.runtime.connectNative('com.videodownloader.host');

      nativePort.onMessage.addListener((response) => {
        if (response.type === 'progress') return; // fire-and-forget progress update
        // Match response to its callback by id (host echoes it back)
        const cbId = response.id ?? id;
        const cb   = pendingCallbacks.get(cbId);
        if (cb) {
          pendingCallbacks.delete(cbId);
          cb(response);
        }
      });

      nativePort.onDisconnect.addListener(() => {
        // Read lastError IMMEDIATELY — Chrome clears it after this tick
        const errMsg = chrome.runtime.lastError?.message ?? '';
        nativePort = null;

        const userMsg = errMsg.toLowerCase().includes('not found') || errMsg.toLowerCase().includes('cannot find')
          ? 'Companion app not installed. Run backend/install/install.bat first.'
          : errMsg || 'Companion app disconnected unexpectedly.';

        pendingCallbacks.forEach((cb) => cb({ success: false, error: userMsg }));
        pendingCallbacks.clear();
      });
    }

    nativePort.postMessage({ ...message, id });

  } catch (err) {
    // postMessage can throw if the port is already disconnected
    pendingCallbacks.delete(id);
    callback({ success: false, error: `Native messaging error: ${err.message}` });
  }
}
