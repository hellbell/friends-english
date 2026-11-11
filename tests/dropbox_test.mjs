// Dropbox playback: run the app's Dropbox code against a fake Dropbox API.
const html = await Deno.readTextFile(new URL('../index.html', import.meta.url));
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
let checks = 0;
function check(value, message) { if (!value) throw new Error(message); checks++; }
function equal(a, b, message) { check(JSON.stringify(a) === JSON.stringify(b), `${message}: ${JSON.stringify(a)}`); }
async function rejects(promise, message) { let failed = false; try { await promise; } catch { failed = true; } check(failed, message); }
function section(start, end) {
  const a = script.indexOf(start), b = script.indexOf(end, a + start.length);
  if (a < 0 || b < 0) throw new Error('Missing source boundary: ' + start);
  return script.slice(a, b);
}

const requests = [];
const api = { token: 200, listPages: [] };
async function fakeFetch(url, init = {}) {
  const body = init.body instanceof URLSearchParams ? Object.fromEntries(init.body) : init.body ? JSON.parse(init.body) : null;
  requests.push({ url, headers: init.headers || {}, body });
  if (url === 'https://api.dropboxapi.com/oauth2/token') {
    if (api.token !== 200) return Response.json({ error: 'invalid_grant' }, { status: api.token });
    return Response.json(body.grant_type === 'authorization_code'
      ? { access_token: 'access-1', refresh_token: 'refresh-1', expires_in: 14400 } : { access_token: 'access-2', expires_in: 14400 });
  }
  if (url.endsWith('/2/files/list_folder')) return Response.json(api.listPages[0]);
  if (url.endsWith('/2/files/list_folder/continue')) return Response.json(api.listPages[1]);
  if (url.endsWith('/2/files/get_temporary_link')) return Response.json({ link: 'https://dl.example/' + body.path });
  if (url === 'https://content.dropboxapi.com/2/files/download') return new Response('1\n00:00:01,000 --> 00:00:02,000\nHi\n');
  return Response.json({ error_summary: 'not_found/' }, { status: 409 });
}

const memory = new Map(), els = new Map();
const storage = { getItem: k => memory.get(k) ?? null, setItem: (k, v) => memory.set(k, String(v)), removeItem: k => memory.delete(k) };
const $ = sel => sel === 'meta[name="fe-dropbox"]' ? { content: 'app-key' }
  : els.get(sel) || els.set(sel, { hidden: false, textContent: '', classList: { toggle() {} } }).get(sel);
const location = { origin: 'https://example.github.io', pathname: '/friends-english/index.html', search: '', hash: '', href: '' };
const history = { replaced: null, replaceState(_, __, url) { this.replaced = url; } };
const C = { videos: [{ yt: 'kog7mwsDqnk', title: 'Recursive Language Models — Alex Zhang' }] };
const app = new Function('$', 'STATIC_MODE', 'C', 'location', 'history', 'localStorage', 'fetch', 'confirm', 'loadLibrary', 'toast',
  section('// ---------- Dropbox', '// ---------- 보관함 동기화') +
  'return { DB, dropboxInit, dropboxConnect, dropboxFinishLogin, dropboxToken, dropboxEpisodes, loadDropbox, dropboxMedia, dropboxDownload, dropboxDisconnect };')(
  $, true, C, location, history, storage, fakeFetch, () => true, () => {}, () => {});

// Folder layout as on the Mac: seasons with subtitles (one in Subs/), and downloaded talks with .m4a audio.
const file = (path, id) => ({ '.tag': 'file', name: path.split('/').pop(), path_display: path, id });
const entries = [
  { '.tag': 'folder', name: 'S01', path_display: '/S01', id: 'id:f1' },
  file('/S01/1x02 - The One With The Sonogram At The End.mp4', 'id:b1'),
  file('/S01/1x02 - The One With The Sonogram At The End.en.srt', 'id:b2'),
  file('/S01/1x01 - The One Where Monica Gets A Roommate.mp4', 'id:a1'),
  file('/S01/1x01 - The One Where Monica Gets A Roommate.en.srt', 'id:a2'),
  file('/S01/Subs/Friends - 1x01 - Pilot.ko.smi', 'id:a3'),
  file('/S01/1x01 - The One Where Monica Gets A Roommate.mkv', 'id:a9'),
  file('/YouTube AI/Recursive Language Models — Alex Zhang, MIT PhD [kog7mwsDqnk].mp4', 'id:c1'),
  file('/YouTube AI/Recursive Language Models — Alex Zhang, MIT PhD [kog7mwsDqnk].en.srt', 'id:c2'),
  file('/YouTube AI/Recursive Language Models — Alex Zhang, MIT PhD [kog7mwsDqnk].m4a', 'id:c4'),
  file('/YouTube AI/Unlisted Talk [abcdefghijk].mp4', 'id:d1'),
  file('/.DS_Store', 'id:x1'),
];
const eps = app.dropboxEpisodes(entries);
equal(eps.map(e => e.id), ['S01/1x01 - The One Where Monica Gets A Roommate.mp4', 'S01/1x02 - The One With The Sonogram At The End.mp4',
  'YouTube AI/Recursive Language Models — Alex Zhang, MIT PhD [kog7mwsDqnk].mp4', 'YouTube AI/Unlisted Talk [abcdefghijk].mp4'],
  'Episode ids are paths like the Mac server, seasons first, other formats skipped');
equal(eps.map(e => [e.title, e.season, e.episode, e.group, e.yt || null]), [
  ['The One Where Monica Gets A Roommate', 1, 1, 'S01', null], ['The One With The Sonogram At The End', 1, 2, 'S01', null],
  ['Recursive Language Models — Alex Zhang', null, null, 'YouTube AI', 'kog7mwsDqnk'], ['Unlisted Talk', null, null, 'YouTube AI', 'abcdefghijk'],
], 'Titles, episode codes, folders and YouTube ids');
equal(eps.map(e => e.files.map(f => f.name)), [['1x01 - The One Where Monica Gets A Roommate.en.srt', 'Friends - 1x01 - Pilot.ko.smi'],
  ['1x02 - The One With The Sonogram At The End.en.srt'], ['Recursive Language Models — Alex Zhang, MIT PhD [kog7mwsDqnk].en.srt'], []],
  'Subtitles pair by name or episode code, including Subs/');
equal(eps.map(e => e.dbxAudio), [null, null, 'id:c4', null], 'An .m4a next to a video is its audio-only file');
check(eps.every(e => e.kind === 'file' && e.source === 'dropbox'), 'Dropbox episodes play like files');

app.dropboxInit();
check(!els.get('#dropbox-btn').hidden && els.get('#dropbox-line').hidden, 'Connect button shows until this device logs in');
await app.dropboxConnect();
const authorize = new URL(location.href), login = JSON.parse(memory.get('fe-dropbox-login'));
equal([authorize.origin + authorize.pathname, ...['client_id', 'response_type', 'code_challenge_method', 'token_access_type', 'redirect_uri']
  .map(k => authorize.searchParams.get(k))], ['https://www.dropbox.com/oauth2/authorize', 'app-key', 'code', 'S256', 'offline',
  'https://example.github.io/friends-english/'], 'Login uses PKCE with offline access and the registered address');
const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(login.verifier)));
check(authorize.searchParams.get('code_challenge') === btoa(String.fromCharCode(...digest)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
  && authorize.searchParams.get('state') === login.state, 'Code challenge is the SHA-256 of the stored verifier');

location.search = `?code=the-code&state=wrong`;
await rejects(app.dropboxFinishLogin(), 'A login that comes back with another state is refused');
check(!memory.has('fe-dropbox') && history.replaced === '/friends-english/index.html', 'and stores nothing, cleaning the address');
memory.set('fe-dropbox-login', JSON.stringify(login));
location.search = `?code=the-code&state=${login.state}`;
await app.dropboxFinishLogin();
const exchange = requests.at(-1).body;
check(exchange.grant_type === 'authorization_code' && exchange.code === 'the-code' && exchange.code_verifier === login.verifier
  && exchange.redirect_uri === 'https://example.github.io/friends-english/', 'The code is exchanged with the verifier');
check(memory.get('fe-dropbox') === 'refresh-1' && !memory.has('fe-dropbox-login'), 'The refresh token stays on this device');
location.search = '';

app.DB.token = null;
check(await app.dropboxToken() === 'access-2' && requests.at(-1).body.grant_type === 'refresh_token', 'A new access token comes from the refresh token');
const count = requests.length;
await app.dropboxToken();
check(requests.length === count, 'and is reused while fresh');

api.listPages = [{ entries: entries.slice(0, 6), has_more: true, cursor: 'c1' }, { entries: entries.slice(6), has_more: false }];
check((await app.loadDropbox()).length === 4 && app.DB.count === 4, 'Listing follows has_more pages');
const talk = eps[2];
await app.dropboxMedia(talk);
check(talk.video === 'https://dl.example/id:c1' && talk.audioUrl === 'https://dl.example/id:c4' && Date.now() - talk.linkAt < 1000,
  'Temporary links for the video and its audio-only file');
const sub = new TextDecoder().decode(await talk.files[0].arrayBuffer());
check(sub.includes('-->') && requests.at(-1).headers['Dropbox-API-Arg'] === '{"path":"id:c2"}', 'Subtitles download by id (ASCII header)');

app.DB.token = null;
api.token = 400;
await rejects(app.dropboxToken(), 'A revoked login fails');
check(!memory.has('fe-dropbox') && app.DB.refresh === null && !els.get('#dropbox-btn').hidden, 'and the device shows the connect button again');

console.log(`PASS: ${checks} Dropbox checks (episode pairing, PKCE login, tokens, paging, links, subtitles)`);
