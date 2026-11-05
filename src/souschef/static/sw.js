// Shell assets are fetched first and kept for offline. Navigations to /shopping and /pantry are
// network-first, and the latest response is kept, so the installed app can
// open that list and a read-only pantry when the server is unreachable.
// No other page is cached; any other navigation that cannot reach the server goes to the
// cached list, because the app reopens at its start URL (Home) after being killed.
const SHELL = 'souschef-shell-v15';
const ASSETS = [
  '/static/app.css', '/static/ds.css', '/static/fonts/fonts.css', '/static/htmx.min.js',
  '/static/list-sync.js', '/static/list-page.js',
];
const LIST_PATHS = ['/shopping', '/pantry'];

self.addEventListener('install', (event) => {
  const fresh = ASSETS.map((path) => new Request(path, { cache: 'reload' }));
  event.waitUntil(caches.open(SHELL).then((cache) => cache.addAll(fresh)).then(() => self.skipWaiting()));
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
    event.respondWith(staticAsset(request));
    return;
  }

  if (request.mode !== 'navigate') return;
  const listPath = listPathOf(url);
  event.respondWith(listPath ? networkFirst(request, listPath) : listWhenUnreachable(request));
});

// `tailscale serve` stays up on the Windows host and answers 502 (504 on a hung upstream) while
// the server itself is stopped. Keep in step with proxyAnsweredForStoppedServer in list-sync.js.
function proxyAnsweredForStoppedServer(response) {
  return response.status === 502 || response.status === 504;
}

async function listWhenUnreachable(request) {
  let response;
  try {
    response = await fetch(request);
    if (!proxyAnsweredForStoppedServer(response)) return response;
  } catch (err) {
    response = null;
  }
  const cache = await caches.open(SHELL);
  if (await cache.match('/shopping')) return Response.redirect('/shopping', 302);
  if (response) return response;
  return Response.error();
}

// Pages link assets as app.css?v=<mtime>. Fetch first so a new version is never
// shadowed, keep each response, and offline fall back to any cached version.
// Modules (list-page.js and its imports) carry no ?v and the server sends no Cache-Control,
// so the browser's HTTP cache would hand back a heuristically fresh old copy: revalidate.
async function staticAsset(request) {
  const cache = await caches.open(SHELL);
  try {
    const response = await fetch(request, { cache: 'no-cache' });
    if (proxyAnsweredForStoppedServer(response)) {
      return (await cache.match(request, { ignoreSearch: true })) || response;
    }
    if (response.ok) {
      try {
        await cache.put(request, response.clone());
      } catch {
        // A response the cache refuses must still be shown.
      }
    }
    return response;
  } catch (err) {
    const cached = await cache.match(request, { ignoreSearch: true });
    if (cached) return cached;
    throw err;
  }
}

function listPathOf(url) {
  let path = url.pathname;
  if (path.length > 1 && path.endsWith('/')) path = path.slice(0, -1);
  return LIST_PATHS.includes(path) ? path : '';
}

async function networkFirst(request, cacheKey) {
  const cache = await caches.open(SHELL);
  try {
    const response = await fetch(request);
    if (proxyAnsweredForStoppedServer(response)) {
      const cached = await cache.match(cacheKey);
      if (cached) return cached;
      return response;
    }
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
