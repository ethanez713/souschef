// Minimal service worker. Its job is to make the app installable and to keep
// the shell usable when the kitchen wifi drops -- not to cache application data,
// which would risk showing stale pantry levels and defeat the whole premise.
const SHELL = 'souschef-shell-v2';
const ASSETS = [
  '/static/app.css', '/static/ds.css', '/static/fonts/fonts.css', '/static/htmx.min.js',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(SHELL).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  // Static assets: cache-first. Everything else always goes to the network so
  // inventory is never served stale.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(caches.match(request).then((hit) => hit || fetch(request)));
  }
});
