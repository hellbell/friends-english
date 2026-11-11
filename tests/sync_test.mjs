// Note sync across devices: run the app's own sync code against an in-memory GitHub contents API.
const html = await Deno.readTextFile(new URL('../index.html', import.meta.url));
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
let checks = 0;
function check(value, message) { if (!value) throw new Error(message); checks++; }
function equal(a, b, message) { check(JSON.stringify(a) === JSON.stringify(b), `${message}: ${JSON.stringify(a)}`); }
function section(start, end) {
  const a = script.indexOf(start), b = script.indexOf(end, a + start.length);
  if (a < 0 || b < 0) throw new Error('Missing source boundary: ' + start);
  return script.slice(a, b);
}
const code = section('function cleanNote(', 'function validateBackup(') +
  section('function saveNotes()', 'function toggleSaveSentence(') +
  section('// ---------- 보관함 동기화', '// 단어 팝업');

const TOKEN = 'github_pat_' + 'x'.repeat(30);
const decode = b64 => new TextDecoder().decode(Uint8Array.from(atob(b64), c => c.charCodeAt(0)));
const encode = text => btoa(String.fromCharCode(...new TextEncoder().encode(text))).replace(/.{60}/g, '$&\n');  // GitHub wraps lines

// One file in a fake repository. Every write changes the sha; a stale sha gets 409 like GitHub.
const repo = { text: null, sha: null, writes: 0, big: false, onGet: null, onPut: null };
const once = key => { const hook = repo[key]; repo[key] = null; return hook ? hook() : null; };
async function githubFetch(url, init = {}) {
  const u = new URL(url), method = init.method || 'GET';
  if (u.origin !== 'https://api.github.com' || init.cache !== 'no-store') throw new Error('Unexpected request ' + url);
  if (init.headers?.Authorization !== 'Bearer ' + TOKEN) return Response.json({ message: 'Bad credentials' }, { status: 401 });
  if (u.pathname === '/repos/me/sync/git/blobs/' + repo.sha) return Response.json({ sha: repo.sha, content: encode(repo.text) });
  if (u.pathname !== '/repos/me/sync/contents/notes.json') return Response.json({ message: 'Not Found' }, { status: 404 });
  if (method === 'GET') {
    await once('onGet');
    return repo.text == null ? Response.json({ message: 'Not Found' }, { status: 404 })
      : Response.json({ sha: repo.sha, content: repo.big ? '' : encode(repo.text), encoding: repo.big ? 'none' : 'base64' });
  }
  const body = JSON.parse(init.body);
  await once('onPut');
  if (repo.text != null && !body.sha) return Response.json({ message: '"sha" wasn\'t supplied' }, { status: 422 });
  if (body.sha && body.sha !== repo.sha) return Response.json({ message: 'conflict' }, { status: 409 });
  repo.text = decode(body.content);
  repo.sha = 'sha' + ++repo.writes;
  return Response.json({ content: { sha: repo.sha } });
}
const shared = () => JSON.parse(repo.text);

// A device: its own browser storage, optional Mac server (remote mode), and the app code with stubs.
function device({ remote = false, episodes = [], token = TOKEN } = {}) {
  const memory = new Map();
  const store = { get: (k, d) => (memory.has(k) ? JSON.parse(memory.get(k)) : d), set: (k, v) => memory.set(k, JSON.stringify(v)) };
  const S = { remote, notes: [], episodes, en: [], koFor: [], ep: null, delay: 0 };
  const server = { notes: [] };
  const api = async (method, path, body) => {
    if (method === 'POST' && path === 'api/notes') server.notes.unshift(body);
    else if (method === 'DELETE') server.notes = server.notes.filter(n => n.id !== path.slice('api/notes/'.length));
    return { ok: true };
  };
  const els = new Map();
  const $ = sel => els.get(sel) || els.set(sel, { hidden: false, textContent: '', classList: { toggle() {} } }).get(sel);
  const portable = id => { const m = /\[([\w-]{11})\]\.mp4$/.exec(id); return m ? 'yt:' + m[1] : id; };
  const app = new Function('$', 'S', 'store', 'api', 'sync', 'toast', 'fetch', 'portableEpisodeId', 'refreshSaved', 'renderNotes',
    'epLabel', 'video', 'setTimeout', 'clearTimeout', 'sameEpisode',
    code + 'return { NS, syncNotes, addNote, removeNote, noteSyncConf };')(
    $, S, store, api, (m, p, b) => { if (S.remote) api(m, p, b); }, () => {}, githubFetch, portable, () => {}, () => {},
    ep => ep.title, { currentTime: 0 }, () => 0, () => {}, (x, y) => x === y || portable(x) === portable(y));
  app.NS.conf = app.noteSyncConf('me/sync ' + token);
  return {
    S, store, server, app, status: () => $('#sync-status').textContent,
    save(text, ep = 'yt:aaaaaaaaaaa') {  // the real addNote, on a sentence with a Korean line
      Object.assign(S, { ep: { id: ep, title: 'Talk' }, en: [{ start: S.notes.length + 1, text }], koFor: ['한국어 뜻 ☕'] });
      app.addNote('sentence', text, 0);
      return S.notes[0];
    },
  };
}

const probe = device().app;
check(probe.noteSyncConf('me/sync ' + TOKEN)?.repo === 'me/sync', 'Sync setting is "owner/repo token"');
for (const bad of ['', 'me/sync', TOKEN, 'me sync ' + TOKEN, 'me/sync short', `me/sync ${TOKEN}"`]) equal(probe.noteSyncConf(bad), null, 'Reject malformed sync setting');

const phone = device(), laptop = device();
const a = phone.save('Café — it was pure serendipity.');
await phone.app.syncNotes();
equal(shared().notes.map(n => n.id), [a.id], 'First device uploads its note');
const fields = n => [n.id, n.kind, n.text, n.sent, n.ko, n.ep, n.epLabel, n.t, n.at];
let writes = repo.writes;
await phone.app.syncNotes();
equal(repo.writes, writes, 'A device does not upload again when its own notes are unchanged');
await laptop.app.syncNotes();
equal(laptop.S.notes.map(fields), [fields(a)], 'Second device receives the same note');
equal(laptop.store.get('notes', []).map(n => n.ko), ['한국어 뜻 ☕'], 'Received notes are kept in browser storage, text intact');
check(laptop.status().startsWith('보관함 동기화됨'), 'Status shows the last sync');
writes = repo.writes;
await laptop.app.syncNotes();
equal(repo.writes, writes, 'Nothing is uploaded when nothing changed');

laptop.app.removeNote(laptop.S.notes[0]);
await laptop.app.syncNotes();
check(shared().notes.length === 0 && a.id in shared().deleted, 'A deletion reaches the shared file');
await phone.app.syncNotes();
check(phone.S.notes.length === 0 && a.id in phone.store.get('deletedNotes', {}), 'A note deleted elsewhere disappears and stays deleted');

const b = phone.save('Saved on the phone.'), c = laptop.save('Saved on the laptop.');
repo.onPut = () => phone.app.syncNotes();  // the phone uploads while the laptop is uploading
await laptop.app.syncNotes();
equal(shared().notes.map(n => n.id).sort(), [b.id, c.id].sort(), 'Both notes survive a write conflict');
await phone.app.syncNotes();
equal(phone.S.notes.map(n => n.id).sort(), [b.id, c.id].sort(), 'The phone then receives the laptop note');

repo.onGet = () => { laptop.save('Typed while syncing.'); };
await laptop.app.syncNotes();
check(laptop.S.notes.some(n => n.text === 'Typed while syncing.'), 'A note saved during a sync is not lost');
await laptop.app.syncNotes();
check(shared().notes.some(n => n.text === 'Typed while syncing.'), 'and goes up with the next sync');

const file = 'YouTube AI/Talk [abcdefghijk].mp4';
const mac = device({ remote: true, episodes: [{ id: file }] });
const own = { id: 'macnote1', kind: 'word', text: 'gist', sent: 'Get the gist.', ko: '요지', ep: file, epLabel: 'Talk', t: 5, at: 1000 };
const gone = { ...own, id: 'macnote2', text: 'stale', at: 900 };
mac.S.notes = [own, gone];
mac.server.notes = [own, gone];
phone.store.set('deletedNotes', { ...phone.store.get('deletedNotes', {}), macnote2: Date.now() });
const d = phone.save('Saved on the YouTube version.', 'yt:abcdefghijk');
await phone.app.syncNotes();
await mac.app.syncNotes();
equal(mac.server.notes.map(n => n.id).sort(), [...shared().notes.map(n => n.id)].sort(), 'The Mac server gets new notes and drops deleted ones');
check(!mac.server.notes.some(n => n.id === 'macnote2'), 'A note deleted on the phone is deleted on the Mac server');
equal(mac.server.notes.find(n => n.id === d.id).ep, file, 'A YouTube note attaches to the downloaded file on the Mac');
equal(shared().notes.find(n => n.id === own.id).ep, 'yt:abcdefghijk', 'The shared file keeps portable episode ids');

const stranger = device({ token: 'github_pat_' + 'y'.repeat(30) });
stranger.save('Not allowed.');
await stranger.app.syncNotes();
check(stranger.app.NS.error.includes('만료') && stranger.status().startsWith('동기화 실패'), 'A rejected token is reported');

const kept = repo.text;
writes = repo.writes;
repo.text = '{not json';
await phone.app.syncNotes();
check(phone.app.NS.error === '동기화 파일을 읽지 못했습니다' && repo.writes === writes, 'A broken shared file is reported, not overwritten');
const junk = JSON.parse(kept);
junk.notes.push({ ...own, id: 'bad kind', kind: 'video' }, { ...own, id: 'proto', ep: '__proto__' });
Object.assign(junk.deleted, { 'not valid!': 1, later: 'yesterday' });
repo.text = JSON.stringify(junk);
await phone.app.syncNotes();
check(!phone.S.notes.some(n => n.id === 'proto' || n.id === 'bad kind') && !('not valid!' in shared().deleted) && !('later' in shared().deleted),
  'Malformed shared entries are skipped and cleaned');

repo.big = true;
const e = phone.save('Read through the blob API.');
await phone.app.syncNotes();
check(shared().notes.some(n => n.id === e.id) && !phone.app.NS.error, 'A shared file over 1 MB is read through the blob API');
repo.big = false;

console.log(`PASS: ${checks} sync checks (two devices, deletions, write conflicts, Mac server, bad tokens and files)`);
