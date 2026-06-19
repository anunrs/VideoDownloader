// content.js — runs in the isolated extension world on every page
// Responsibility: detect <video> element sources that may never produce a
// separate network request (e.g. src set inline, or loaded from cache).
// The heavy lifting (XHR / fetch network traffic) is already caught by
// background.js via chrome.webRequest.

// ─── Helpers ─────────────────────────────────────────────────────────────────

function reportUrl(url) {
  if (!url || url.startsWith('blob:') || url.startsWith('data:')) return;
  chrome.runtime.sendMessage({ type: 'VIDEO_FOUND', url });
}

// ─── Scan all <video> elements currently in DOM ───────────────────────────────

function scanVideoElements() {
  document.querySelectorAll('video').forEach((video) => {
    [video.src, video.currentSrc].filter(Boolean).forEach(reportUrl);

    video.querySelectorAll('source').forEach((source) => {
      if (source.src) reportUrl(source.src);
    });
  });
}

// ─── Watch for dynamically added / modified video elements ────────────────────

const observer = new MutationObserver((mutations) => {
  for (const mutation of mutations) {
    for (const node of mutation.addedNodes) {
      if (node.nodeType !== Node.ELEMENT_NODE) continue;

      if (node.tagName === 'VIDEO') {
        [node.src, node.currentSrc].filter(Boolean).forEach(reportUrl);
        node.querySelectorAll('source').forEach((s) => s.src && reportUrl(s.src));
      }

      // Also check descendants in case a whole subtree was injected
      node.querySelectorAll?.('video').forEach((v) => {
        [v.src, v.currentSrc].filter(Boolean).forEach(reportUrl);
        v.querySelectorAll('source').forEach((s) => s.src && reportUrl(s.src));
      });
    }

    // Catch src attribute changes on existing <video> elements
    if (
      mutation.type === 'attributes' &&
      mutation.attributeName === 'src' &&
      mutation.target.tagName === 'VIDEO'
    ) {
      reportUrl(mutation.target.src);
    }
  }
});

observer.observe(document.documentElement, {
  childList:  true,
  subtree:    true,
  attributes: true,
  attributeFilter: ['src']
});

// Run once on initial load
scanVideoElements();
