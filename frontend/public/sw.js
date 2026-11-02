// The installable app's service worker (scope /reef/). The app's own files are kept so it opens without a network;
// the API and MCP are never cached: a reading must never pass for the present. A redirect (signed out: a proxy in front
// sends the browser to its sign-in page) is passed through and never stored.
const CACHE = 'reef-app-v1';

self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

function keep(request, response) {
  if (response.ok && !response.redirected && response.type === 'basic') {
    const copy = response.clone();
    caches.open(CACHE).then((cache) => cache.put(request, copy));
  }
  return response;
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin || !url.pathname.startsWith('/reef/')) return;
  if (url.pathname.startsWith('/reef/api/') || url.pathname.startsWith('/reef/mcp')) return;
  if (url.pathname.startsWith('/reef/assets/')) {
    // hashed file names: a cached copy is always the right one
    event.respondWith(caches.match(request).then((hit) => hit || fetch(request).then((response) => keep(request, response))));
    return;
  }
  event.respondWith(
    fetch(request)
      .then((response) => keep(request, response))
      .catch(() => caches.match(request).then((hit) => hit || caches.match('/reef/'))),
  );
});
