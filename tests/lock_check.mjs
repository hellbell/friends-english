// Open a locked Pages build with the gate's own crypto code; tests/test_build.py runs this.
//   deno run --allow-read=SITE --allow-write=OUT tests/lock_check.mjs SITE OUT PASSWORD
// Writes the decrypted app, catalog and subtitles to OUT, and checks that wrong keys and swapped files fail.
const [site, out, password] = Deno.args;
const gate = await Deno.readTextFile(`${site}/index.html`);
const script = gate.match(/<script>([\s\S]*?)<\/script>/)[1];
const start = script.indexOf('// ---- crypto'), end = script.indexOf('// ---- screen');
if (start < 0 || end < 0) throw new Error('Missing crypto section in the gate');
let checks = 0;
function check(value, message) { if (!value) throw new Error(message); checks++; }
async function rejects(promise, message) {
  let rejected = false;
  try { await promise; } catch { rejected = true; }
  check(rejected, message);
}

const page = {};  // window of the unlocked app
const location = { href: 'https://example.github.io/friends-english/index.html?v=1#x' };
const lib = new Function('window', 'location', script.slice(start, end) +
  'return { LOCK, bytes, base64, deriveKey, unseal, sealedFetch };')(page, location);

const raw = await lib.deriveKey(password);
await Deno.mkdir(`${out}/study-captions`, { recursive: true });
await Deno.writeFile(`${out}/index.html`, new Uint8Array(await lib.unseal(raw, lib.bytes(lib.LOCK.app), 'index.html')));
await rejects(lib.deriveKey(password + '!').then(key => lib.unseal(key, lib.bytes(lib.LOCK.app), 'index.html')), 'Wrong password fails');

// Plain fetch of the static host; `swap` serves one sealed file under another name,
// `stale` stands for an old copy in the browser cache that only cache: 'reload' skips.
const requests = [], swap = new Map(), stale = new Map();
page.fetch = async (input, init) => {
  requests.push({ input: String(input), init });
  const url = new URL(String(input), location.href);
  if (url.origin !== 'https://example.github.io' || !url.pathname.startsWith('/friends-english/')) return new Response('elsewhere');
  const name = url.pathname.slice('/friends-english/'.length);
  if (stale.has(name) && init?.cache !== 'reload') return new Response(stale.get(name));
  try { return new Response(await Deno.readFile(`${site}/${swap.get(name) ?? name}`)); }
  catch { return new Response('missing', { status: 404 }); }
};
// Install the wrapper from source text, as the gate injects it into the app page.
new Function('window', 'location', `(${lib.sealedFetch})(${JSON.stringify(lib.base64(raw))});`)(page, location);

const names = ['catalog.json', ...[...Deno.readDirSync(`${site}/study-captions`)].map(e => `study-captions/${e.name}`)];
check(names.length > 1, 'Build has subtitles');
for (const name of names) {
  const response = await page.fetch(name, name === 'catalog.json' ? { cache: 'reload' } : undefined);
  check(response.ok, `Opened ${name}`);
  await Deno.writeFile(`${out}/${name}`, new Uint8Array(await response.arrayBuffer()));
}
check(requests.some(r => r.input === 'catalog.json' && r.init?.cache === 'reload'), 'Fetch options pass through');
stale.set('catalog.json', '{"videos": []}');  // e.g. the unlocked catalog from before the lock
const reread = await page.fetch('catalog.json');
check((await reread.json()).videos.length > 0 && requests.at(-1).init?.cache === 'reload', 'A stale cached copy is fetched again past the cache');
stale.clear();
swap.set('study-captions/aaaaaaaaaaa.en.srt', 'catalog.json');
await rejects(page.fetch('study-captions/aaaaaaaaaaa.en.srt'), 'A sealed file under another name fails');
swap.set('catalog.json', names[1]);
await rejects(page.fetch('catalog.json'), 'A subtitle served as the catalog fails');
check((await page.fetch('study-captions/bbbbbbbbbbb.ko.srt')).status === 404, 'Missing subtitle keeps its status');
check((await (await page.fetch('icon.png')).arrayBuffer()).byteLength === (await Deno.readFile(`${site}/icon.png`)).length, 'Public files pass through');
check(await (await page.fetch('https://www.youtube.com/oembed?format=json')).text() === 'elsewhere', 'Other sites pass through');
check(await (await page.fetch('/other/catalog.json')).text() === 'elsewhere', 'Paths outside the app pass through');
console.log(`${checks} lock checks passed`);
