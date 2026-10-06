// Service worker mínimo: solo lo necesario para que el navegador
// considere la página "instalable" como app. No cachea nada agresivamente
// para que los datos (RAW embebido en index.html) siempre se vean frescos.
//
// IMPORTANTE: el listener de 'fetch' NO llama a respondWith().
// Si se llama a respondWith(fetch(...)) y esa petición de red falla
// (mala señal, error momentáneo, etc.), el navegador considera que
// TODO el fetch fallo -- en iOS/Safari esto rompe la pagina completa
// con "Safari no puede abrir la pagina". Al no llamar respondWith(),
// el navegador simplemente maneja la peticion de forma normal, como si
// no hubiera service worker interceptandola -- sin riesgo de que un
// error de red tumbe la app.
self.addEventListener('install', function(e){
  self.skipWaiting();
});
self.addEventListener('activate', function(e){
  self.clients.claim();
});
self.addEventListener('fetch', function(e){
  // Intencionalmente vacio: no interceptar peticiones.
});
