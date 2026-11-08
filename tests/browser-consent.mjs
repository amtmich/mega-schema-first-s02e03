// Real browser regression test, using an already-installed Chromium and Node.
// Talks CDP over private process pipes; it opens no debugging port.
import { spawn } from 'node:child_process';
import { mkdir, mkdtemp, rm } from 'node:fs/promises';
import path from 'node:path';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const fixture = JSON.parse(input);
await mkdir(path.resolve('private'), { recursive: true });
const profile = await mkdtemp(path.resolve('private/browser-test-'));
const browser = spawn(fixture.chromium, [
  '--no-sandbox', '--headless', '--remote-debugging-pipe',
  '--disable-background-networking', '--disable-extensions', '--no-first-run',
  '--user-data-dir=' + profile, 'about:blank',
], { stdio: ['ignore', 'ignore', 'pipe', 'pipe', 'pipe'], detached: true });
let serial = 0, buffer = '', stderr = '', postedOrigin, callbackReferrerSent;
const pending = new Map();
browser.stderr.on('data', data => { stderr += data.toString(); });
for (const pipe of [browser.stdio[3], browser.stdio[4]]) {
  pipe.on('error', error => {
    for (const { reject } of pending.values()) reject(new Error(error.message + ': ' + stderr.slice(-1500)));
  });
}
const stop = () => { try { process.kill(-browser.pid, 'SIGTERM'); } catch {} };
process.on('SIGTERM', () => { stop(); process.exit(143); });
const deadline = setTimeout(() => {
  for (const { reject } of pending.values()) reject(new Error('Browser deadline exceeded'));
  stop();
}, 20000);
function command(method, params = {}, sessionId) {
  return new Promise((resolve, reject) => {
    const id = ++serial;
    pending.set(id, { resolve, reject });
    browser.stdio[3].write(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }) + '\0');
  });
}
browser.stdio[4].on('data', data => {
  buffer += data.toString();
  while (buffer.includes('\0')) {
    const end = buffer.indexOf('\0');
    const raw = buffer.slice(0, end); buffer = buffer.slice(end + 1);
    if (!raw) continue;
    const message = JSON.parse(raw);
    if (message.id && pending.has(message.id)) {
      const p = pending.get(message.id); pending.delete(message.id);
      if (message.error) p.reject(new Error(message.error.message)); else p.resolve(message.result);
    }
    if (message.method === 'Network.requestWillBeSent') {
      const request = message.params.request;
      if (request.method === 'POST' && new URL(request.url).pathname === '/authorize') {
        postedOrigin = request.headers.Origin ?? request.headers.origin;
      }
      if (request.method === 'GET' && new URL(request.url).pathname === '/health') {
        callbackReferrerSent = Object.keys(request.headers).some(key => key.toLowerCase() === 'referer');
      }
    }
  }
});
browser.on('exit', () => {
  for (const { reject } of pending.values()) reject(new Error('Chromium exited: ' + stderr.slice(-1500)));
});
try {
  const { targetId } = await command('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await command('Target.attachToTarget', { targetId, flatten: true });
  await command('Page.enable', {}, sessionId);
  await command('Network.enable', {}, sessionId);
  const evaluate = async expression => {
    const result = await command('Runtime.evaluate', { expression, returnByValue: true }, sessionId);
    if (result.exceptionDetails) throw new Error('Browser script failed');
    return result.result.value;
  };
  await command('Page.navigate', { url: fixture.url }, sessionId);
  const waitFor = async expression => {
    for (let i = 0; i < 100; i++) {
      const result = await evaluate(expression);
      if (result) return result;
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    throw new Error('Browser navigation did not finish');
  };
  await waitFor('Boolean(document.querySelector("#password"))');
  await evaluate('document.querySelector("#password").value=' + JSON.stringify(fixture.password));
  await evaluate('document.querySelector("button[value=allow]").click()');
  const result = await waitFor(`(() => {
    try { return JSON.parse(document.body.innerText); } catch { return null; }
  })()`);
  console.log(JSON.stringify({ body: result, postedOrigin, callbackReferrerSent }));
} finally {
  clearTimeout(deadline);
  stop();
  await new Promise(resolve => {
    if (browser.exitCode !== null || browser.signalCode !== null) return resolve();
    browser.once('exit', resolve);
    setTimeout(resolve, 2000).unref();
  });
  await rm(profile, { recursive: true, force: true });
}
