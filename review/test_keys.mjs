#!/usr/bin/env node
// Keyboard contract test. Drives the real UI in real Chrome over CDP, because the
// bug this exists to catch — a shortcut firing while an edit is open but unfocused
// — is invisible to any test that reasons about the handler instead of pressing keys.
//
//   rm -rf /tmp/kfix && cp -R "$(../bin/knowledge config --path)" /tmp/kfix
//   KNOWLEDGE_DIR=/tmp/kfix python3 review.py --port 8799 &
//   "$CHROME" --headless=new \
//     --remote-debugging-port=9333 --remote-debugging-address=127.0.0.1 \
//     --user-data-dir=/tmp/chrome-review-test --no-first-run about:blank &
//   node test_keys.mjs
//
// $CHROME is whatever Chrome/Chromium is called on your machine, e.g.
//   macOS "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
//   Linux  google-chrome | chromium
// Override CDP_PORT and APP_URL if you moved either port.
const PORT = process.env.CDP_PORT || '9333';
const APP = process.env.APP_URL || 'http://127.0.0.1:8799/';
const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
const ws = new WebSocket(list.find(t => t.type === 'page').webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener('open', r));
let id = 0; const w = new Map();
ws.addEventListener('message', ev => { const m = JSON.parse(ev.data); if (m.id && w.has(m.id)) { w.get(m.id)(m); w.delete(m.id); } });
const send = (method, params = {}) => new Promise(res => { const n = ++id; w.set(n, res); ws.send(JSON.stringify({ id: n, method, params })); });
const evalx = async e => (await send('Runtime.evaluate', { expression: e, returnByValue: true, awaitPromise: true })).result?.result?.value;
const key = async k => {
  const named = k === 'Escape' || k === 'Enter';
  const b = { key: k, windowsVirtualKeyCode: named ? (k === 'Enter' ? 13 : 27) : k.toUpperCase().charCodeAt(0) };
  await send('Input.dispatchKeyEvent', { type: 'keyDown', ...b, text: named ? '' : k });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', ...b });
  await new Promise(r => setTimeout(r, 170));
};
const st = () => evalx(`JSON.stringify({screen:document.querySelector('.arm')?'arm':document.querySelector('table.grp')?'picker':document.querySelector('.done')?'done':'card',editing,focus:document.activeElement.tagName,text:texts[0],left:V.left})`).then(JSON.parse);

let fails = 0;
const ok = (name, cond, got) => { console.log(`${cond ? '  ok  ' : 'FAIL  '}${name}${cond ? '' : `  → ${JSON.stringify(got)}`}`); if (!cond) fails++; };

await send('Page.enable'); await send('Runtime.enable');
await send('Page.navigate', { url: APP }); await new Promise(r => setTimeout(r, 1200));
await key('Enter');                       // arm
await key('a');                           // review everything
await key('e');                           // open the edit box
const opened = await st();
ok('e opens the edit box', opened.editing === true && opened.focus === 'TEXTAREA', opened);

// The regression: half-typed edit, focus knocked off the box (a click on the card
// background does exactly this), then every bare-key shortcut in turn.
await evalx(`(()=>{const t=document.getElementById('t0');t.value='HALF-TYPED EDIT';t.dispatchEvent(new Event('input'));t.blur();})()`);
const before = await st();
ok('edit survives losing focus', before.editing === true && before.text === 'HALF-TYPED EDIT', before);
for (const k of ['g', 'x', 'y', 's', 'u', '1', 'm', '?']) {
  await key(k);
  const s = await st();
  ok(`${k} is inert while editing`,
     s.editing === true && s.screen === 'card' && s.text === 'HALF-TYPED EDIT' && s.left === before.left, s);
}
await key('Escape');
const closed = await st();
ok('Escape closes edit and keeps the text', closed.editing === false && closed.text === 'HALF-TYPED EDIT', closed);
await key('g');
const picker = await st();
ok('g reaches the picker once edit is closed', picker.screen === 'picker', picker);

console.log(fails ? `\n${fails} failure(s)` : '\nall good');
process.exit(fails ? 1 : 0);
