const SHELL="trade-desk-shell-v2";
const FILES=["./","index.html","manifest.webmanifest","icon-192.png","icon-512.png"];
self.addEventListener("install",e=>{e.waitUntil(caches.open(SHELL).then(c=>c.addAll(FILES)).then(()=>self.skipWaiting()));});
self.addEventListener("activate",e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==SHELL).map(k=>caches.delete(k)))).then(()=>self.clients.claim()));});
self.addEventListener("fetch",e=>{
  const u=new URL(e.request.url);
  if(e.request.method!=="GET"||u.origin!==location.origin) return;
  if(u.pathname.endsWith("data.json")){
    e.respondWith(fetch(e.request).then(r=>{const c=r.clone();caches.open(SHELL).then(x=>x.put("data.json",c));return r;}).catch(()=>caches.match("data.json")));
    return;
  }
  e.respondWith(fetch(e.request).then(r=>{const c=r.clone();caches.open(SHELL).then(x=>x.put(e.request,c));return r;}).catch(()=>caches.match(e.request).then(m=>m||caches.match("index.html"))));
});
