// static/js/service-worker.js

const CACHE_NAME = 'meal-engine-cache-v1';
const urlsToCache = [
  '/',
  '/static/css/style.css',
  // We will let the app cache pages as they are visited
];

// On install, cache the core assets
self.addEventListener('install', event => {
  console.log('Service Worker: Installing...');
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => {
        console.log('Service Worker: Caching app shell');
        return cache.addAll(urlsToCache);
      })
  );
});

// On activate, take control immediately
self.addEventListener('activate', event => {
  console.log('Service Worker: Activating...');
  // This line is new: it ensures the new service worker activates immediately
  return self.clients.claim();
});

// On fetch, serve from cache if possible
self.addEventListener('fetch', event => {
  console.log('Service Worker: Fetching', event.request.url);
  event.respondWith(
    caches.match(event.request)
      .then(response => {
        // If we have a response in the cache, serve it
        if (response) {
          console.log('Service Worker: Found in cache', event.request.url);
          return response;
        }
        // Otherwise, fetch from the network
        console.log('Service Worker: Not in cache, fetching from network', event.request.url);
        return fetch(event.request);
      })
  );
});