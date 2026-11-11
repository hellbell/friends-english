// Run the actual app functions with small browser stubs; no YouTube network requests.
const html = await Deno.readTextFile(new URL('../index.html', import.meta.url));
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new Function(script);
let checks = 0;
function check(value, message) { if (!value) throw new Error(message); checks++; }
function equal(a, b, message) { check(JSON.stringify(a) === JSON.stringify(b), `${message}: ${JSON.stringify(a)}`); }
function rejects(fn, message) { let rejected = false; try { fn(); } catch { rejected = true; } check(rejected, message); }
function section(start, end) {
  const a = script.indexOf(start), b = script.indexOf(end, a + start.length);
  if (a < 0 || b < 0) throw new Error('Missing source boundary: ' + start);
  return script.slice(a, b);
}
const elements = new Map();
function $(id) {
  if (!elements.has(id)) elements.set(id, { textContent: '', hidden: false, disabled: false, value: '', checked: false,
    setAttribute() {}, classList: { toggle() {} }, replaceChildren() {}, append() {} });
  return elements.get(id);
}
const memory = new Map();
let failStorage = false;
const localStorage = {
  getItem: k => memory.get(k) ?? null,
  setItem: (k, v) => { if (failStorage) throw new Error('quota'); memory.set(k, v); },
  removeItem: k => memory.delete(k),
};
const store = { get(k, fallback) { const v = localStorage.getItem('fe.' + k); return v == null ? fallback : JSON.parse(v); }, set(k, v) { localStorage.setItem('fe.' + k, JSON.stringify(v)); } };
const S = { episodes: [], ep: null, remote: false, notes: [], pos: {}, delays: {}, en: [], set: { loop: false, autoPause: false, dict: false } };
const video = { currentTime: 0, paused: true, seeking: false, duration: 100, play() { this.paused = false; return Promise.resolve(); }, pause() { this.paused = true; } };
const toasts = [];
let syncsScheduled = 0;
const app = new Function('$', 'S', 'video', 'store', 'localStorage', 'toast', 'fmt', 'applySettings', 'setEpisodes', 'renderNotes', 'refreshSaved', 'requestAnimationFrame', 'setFocus', 'gotoCue', 'PAD_END', 'scheduleNoteSync',
  section('const LEVELS =', '// ---------- 라이브러리 / 에피소드') +
  section('function cueAt(t)', '// ---------- 저장 상태') +
  section('function armStop(', "onPlayer('seeked'") +
  'return { youtubeId, youtubeEpisode, C, recommendVideos, mergeLibrary, rememberYoutube, hiddenVideos, portableEpisodeId, validateBackup, importBackup, clip, markClip, toggleClip, resetClip, tick, follow, armStop };'
)($, S, video, store, localStorage, msg => toasts.push(msg), n => String(n), () => {}, e => { S.episodes = e; }, () => {}, () => {}, () => {},
  i => { S.focus = i; }, i => { S.focus = i; S.apDone = -1; }, 0.2, () => { syncsScheduled++; });

const yt = 'aircAruvnKk';
for (const url of [yt, `https://youtube.com/watch?v=${yt}&t=10`, `youtu.be/${yt}?si=abc`, `https://www.youtube.com/shorts/${yt}`, `https://m.youtube.com/live/${yt}`, `https://www.youtube-nocookie.com/embed/${yt}`]) equal(app.youtubeId(url), yt, 'Supported video URL');
for (const url of [`https://youtube.com.evil.test/watch?v=${yt}`, `https://evil.test/${yt}`, `javascript:alert(1)`, 'https://youtube.com/playlist?list=foo', 'short', `https://youtube.com@evil.test/watch?v=${yt}`]) equal(app.youtubeId(url), null, 'Reject non-video and unsafe URL');
equal(app.youtubeEpisode({ yt, subs: ['https://evil.test/x.srt', '../secret.srt', `study-captions/${yt}.en.srt`] }).subs, [`study-captions/${yt}.en.srt`], 'Imported subtitle URLs are allowlisted');
equal(app.youtubeEpisode({ yt, duration: Infinity }).duration, null, 'Invalid duration does not reach player');

const catalog = JSON.parse(await Deno.readTextFile(new URL('../catalog.json', import.meta.url)));
app.C.videos = catalog.videos;
const defaults = { query: '', level: '', topic: '', duration: '', ready: false, sort: 'recommended' };
const recommend = overrides => app.recommendVideos(catalog.videos, { ...defaults, ...overrides });
const selected = recommend({ collection: 'latent-space-11' });
equal(selected.length, 11, 'Selected collection contains the requested eleven episodes');
equal(recommend({ collection: 'latent-space-11', ready: true }).length, 11, 'All eleven downloaded episodes have bilingual study transcripts');
check(selected.every(v => app.mergeLibrary([]).some(ep => ep.yt === v.yt)), 'All selected episodes appear in the library');
store.set('hiddenVideos', ['yt:' + selected[0].yt]);
check(!app.mergeLibrary([]).some(ep => ep.yt === selected[0].yt), 'Selected episodes still respect library removals');
store.set('hiddenVideos', []);
const bilingual = catalog.videos.filter(v => v.subs?.some(p => p.endsWith('.en.srt')) && v.subs?.some(p => p.endsWith('.ko.srt')));
equal(recommend({ ready: true }).map(v => v.yt).sort(), bilingual.map(v => v.yt).sort(), 'Every bilingual study video is available');
check(recommend({ query: 'AI', level: 'advanced' }).every(v => v.level === 'advanced'), 'Search composes with level');
check(recommend({ topic: 'conversation' }).every(v => v.topics.includes('conversation')), 'Topic filter');
check(recommend({ duration: '600' }).every(v => v.duration > 0 && v.duration <= 600), 'Unknown durations excluded from length filter');
check(recommend({ duration: 'long' }).every(v => v.duration > 1200), 'Long video filter');
equal(recommend({ query: 'nonexistent-zzzz' }).length, 0, 'Honest empty search result');
check(recommend({ query: '일상' }).length > 0, 'Korean topic search');
check(recommend({ sort: 'newest' })[0].published, 'New videos sort first');
const first = recommend({})[0];
const ranked = app.recommendVideos(catalog.videos, defaults, { ['yt:' + first.yt]: { t: 99, d: 100 } });
check(ranked[0].yt !== first.yt, 'Completed videos rank below unseen videos');

const localId = 'YouTube AI/Video [kog7mwsDqnk].mp4';
const local = { id: localId, title: 'Downloaded video', kind: 'file' };
const merged = app.mergeLibrary([local]);
check(!merged.some(e => e.yt === 'kog7mwsDqnk'), 'Prepared video does not duplicate an existing downloaded episode');
app.rememberYoutube(app.youtubeEpisode({ yt, title: 'Neural network' }));
app.rememberYoutube(app.youtubeEpisode({ yt, title: 'Neural network updated' }));
equal(store.get('youtube', []).length, 1, 'Repeated additions deduplicate');
check(app.mergeLibrary([]).some(e => e.yt === yt), 'Custom video survives reload');
store.set('hiddenVideos', ['yt:kog7mwsDqnk']);
check(!app.mergeLibrary([]).some(e => e.yt === 'kog7mwsDqnk'), 'Removed prepared video stays hidden');
app.rememberYoutube(app.youtubeEpisode({ yt: 'kog7mwsDqnk' }));
check(app.mergeLibrary([]).some(e => e.yt === 'kog7mwsDqnk'), 'User can restore removed recommendation');
equal(app.portableEpisodeId(localId), 'yt:kog7mwsDqnk', 'Downloaded AI progress maps to YouTube');
equal(app.portableEpisodeId('Season 01/Friends.S01E01.mp4'), 'Season 01/Friends.S01E01.mp4', 'Local Friends ID preserved');

const note = { id: 'note-1', kind: 'sentence', text: 'Hello.', sent: 'Hello.', ko: '안녕.', ep: 'yt:' + yt, epLabel: 'Test', t: 12, at: 1000 };
const backup = { app: 'friends-english', version: 1, notes: [note], pos: { ['yt:' + yt]: { t: 42, d: 100 } }, delays: { ['yt:' + yt]: .5 }, youtube: [{ yt }], last: 'yt:' + yt };
equal(app.validateBackup(backup).notes[0].text, 'Hello.', 'Valid backup round-trip');
rejects(() => app.validateBackup({ ...backup, app: 'other' }), 'Reject unrelated JSON');
rejects(() => app.validateBackup({ ...backup, notes: [{ ...note, t: '12' }] }), 'Reject malformed note timestamp');
rejects(() => app.validateBackup({ ...backup, notes: [{ ...note, kind: 'script' }] }), 'Reject unsupported note type');
rejects(() => app.validateBackup({ ...backup, pos: JSON.parse('{"__proto__":{"t":0,"d":1}}') }), 'Reject prototype keys');
rejects(() => app.validateBackup({ ...backup, pos: { bad: { t: -1, d: 100 } } }), 'Reject negative playback position');
rejects(() => app.validateBackup({ ...backup, youtube: [{ yt: 'invalid' }] }), 'Reject malformed backup videos');
const makeFile = data => ({ size: 1000, text: async () => JSON.stringify(data) });
S.pos = { ['yt:' + yt]: { t: 70, d: 100 } };
await app.importBackup(makeFile(backup));
equal(S.notes.length, 1, 'Import adds saved sentence');
check(toasts.at(-1).includes('가져왔습니다') && syncsScheduled === 1, 'Import reports success and schedules note sync');
equal(S.pos['yt:' + yt].t, 70, 'Import keeps current progress');
await app.importBackup(makeFile(backup));
equal(S.notes.length, 1, 'Import twice does not duplicate notes');
const before = JSON.stringify(S);
await app.importBackup(makeFile({ ...backup, notes: [{ ...note, t: 'bad' }] }));
equal(JSON.stringify(S), before, 'Invalid backup does not change state');
await app.importBackup({ size: 11 * 1024 * 1024 });
equal(JSON.stringify(S), before, 'Oversized backup does not change state');
failStorage = true;
await app.importBackup(makeFile({ ...backup, notes: [{ ...note, id: 'note-2' }] }));
equal(JSON.stringify(S), before, 'Storage failure does not change in-memory state');
check(toasts.at(-1).includes('저장 공간'), 'Storage failure is shown');
failStorage = false;
S.remote = true;
await app.importBackup(makeFile(backup));
equal(S.notes.length, 1, 'Server mode does not silently import only into browser');
S.remote = false;

S.ep = { id: 'yt:' + yt };
video.currentTime = 10; app.markClip('a');
video.currentTime = 8; app.markClip('b');
equal(app.clip.b, null, 'B must follow A');
video.currentTime = 20; app.markClip('b');
Object.assign(S.set, { loop: true, autoPause: true, dict: true });
app.toggleClip();
equal(video.currentTime, 10, 'A–B loop starts at A');
equal(S.set, { loop: false, autoPause: false, dict: false }, 'A–B loop disables conflicting study modes');
video.currentTime = 20.1; app.tick();
equal(video.currentTime, 10, 'A–B repeats without any subtitles');
video.currentTime = 25; app.markClip('a');
equal(app.clip.b, null, 'Changing A beyond B clears invalid range');
equal(app.clip.on, false, 'Editing bounds stops loop');
app.resetClip();
equal(app.clip, { a: null, b: null, on: false }, 'Episode changes can clear clip state');

// 문장마다 멈춤: follow()는 rAF와 timeupdate 양쪽에서 불린다
S.en = [{ start: 1, end: 3 }, { start: 3, end: 5 }, { start: 6.5, end: 8.5 }];
Object.assign(S, { focus: 0, apDone: -1, delay: 0 });
S.set.autoPause = true;
Object.assign(video, { paused: false, currentTime: 2.9 }); app.follow();
check(!video.paused, 'Keeps playing inside the sentence');
video.currentTime = 3.01; app.follow();
check(video.paused && S.apDone === 0 && S.focus === 0, 'Pauses at the end of the sentence');
video.paused = false; app.follow();
check(!video.paused && S.focus === 1, 'Resuming does not stop again at the same sentence');
video.currentTime = 5.25; app.follow();
check(video.paused && S.apDone === 1, 'Pauses at the next sentence end after the pad');
S.focus = 2; app.armStop(9);
equal(S.apDone, 2, 'Resume position past the sentence end does not pause there');
S.en = []; S.set.autoPause = false; video.paused = true;

// Basic wiring: all literal ID selectors resolve, and generated markup stays unique.
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]);
equal(new Set(ids).size, ids.length, 'Unique DOM IDs');
for (const [, id] of script.matchAll(/\$\(['"]#([a-z][\w-]*)['"]\)/g)) check(ids.includes(id), 'DOM element exists: ' + id);
check(html.includes('aria-orientation="vertical" aria-controls="side"'), 'Panel width divider retained');
check(!html.includes('id="side-height"'), 'No sidebar height control');
check(html.includes('name="referrer" content="strict-origin-when-cross-origin"'), 'YouTube receives page origin');
check(script.includes("if (STATIC_MODE) throw new Error('browser storage')"), 'Static build skips private state API');
console.log(`PASS: ${checks} app checks (recommendations, storage, backup, A–B playback, auto-pause, DOM wiring)`);
