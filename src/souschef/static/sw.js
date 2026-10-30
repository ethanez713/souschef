// Shell assets stay cache-first. Navigations to /shopping and /pantry are
// network-first, and the latest response is kept, so the installed app can
// open that list and a read-only pantry when the server is unreachable.
// No other page is cached.
const SHELL = 'souschef-shell-v11';
const ASSETS = [
  '/static/app.css', '/static/ds.css', '/static/fonts/fonts.css', '/static/htmx.min.js',
  '/static/list-sync.js', '/static/list-page.js',
];
const LIST_PATHS = ['/shopping', '/pantry'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(SHELL).then((cache) => cache.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== SHELL).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('message', (event) => {
  if (!event.data || event.data.type !== 'cache-lists') return;
  event.waitUntil(refreshLists());
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname.startsWith('/static/')) {
    event.respondWith(caches.match(request).then((hit) => hit || fetch(request)));
    return;
  }

  const listPath = listPathOf(url);
  if (listPath && request.mode === 'navigate') {
    event.respondWith(networkFirst(request, listPath));
  }
});

function listPathOf(url) {
  let path = url.pathname;
  if (path.length > 1 && path.endsWith('/')) path = path.slice(0, -1);
  return LIST_PATHS.includes(path) ? path : '';
}

async function networkFirst(request, cacheKey) {
  const cache = await caches.open(SHELL);
  try {
    const response = await fetch(request);
    // A toast query must not replace the clean list the shop will open.
    if (response.ok && new URL(request.url).search === '') {
      try {
        await cache.put(cacheKey, response.clone());
      } catch {
        // A response the cache refuses must still be shown.
      }
    }
    return response;
  } catch (err) {
    const cached = await cache.match(cacheKey);
    if (cached) return cached;
    throw err;
  }
}

async function refreshLists() {
  const cache = await caches.open(SHELL);
  await Promise.all(LIST_PATHS.map(async (path) => {
    try {
      const response = await fetch(path, { cache: 'no-store' });
      if (response.ok) await cache.put(path, response);
    } catch {
      // Keep the last copy. A failed refresh must not wipe it.
    }
  }));
}
