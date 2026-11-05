#!/usr/bin/env node
// Bounded loopback-only qualification of the actual verified npm installation.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import crypto from 'node:crypto';
import { validateInstallation, probeApi } from './verify-npm-toolchain.mjs';

const installation = validateInstallation(process.argv[2]);
const api = probeApi(installation);
const require = createRequire(path.join(installation.owner, 'package.json'));
const { SocksClient } = require('socks');
const { SocksProxyAgent } = require('socks-proxy-agent');
const undici = createRequire(path.join(installation.roots['node-gyp'], 'package.json'))('undici');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'bidit-npm-network-'));
const sockets = new Set();
const requests = [];
const proxyRequests = [];
let tarball;
const deadline = setTimeout(() => { throw new Error('Loopback npm qualification exceeded 30 seconds'); }, 30000);

function track(socket) {
  sockets.add(socket);
  socket.on('error', () => {});
  socket.on('close', () => sockets.delete(socket));
  socket.setTimeout(5000, () => socket.destroy());
  return socket;
}
const origin = http.createServer((request, response) => {
  requests.push(request.url);
  if (request.url === '/fixture.tgz') {
    response.writeHead(200, { 'content-type': 'application/octet-stream', 'content-length': tarball.length });
    response.end(tarball);
  } else if (request.url === '/bidit-socks-fixture') {
    const metadata = { name: 'bidit-socks-fixture', 'dist-tags': { latest: '1.0.0' }, versions: { '1.0.0': { name: 'bidit-socks-fixture', version: '1.0.0', dist: { tarball: `http://127.0.0.1:${origin.address().port}/fixture.tgz`, integrity: `sha512-${crypto.createHash('sha512').update(tarball).digest('base64')}` } } } };
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end(JSON.stringify(metadata));
  } else response.end('bidit-loopback-ok');
});
origin.on('connection', track);

// This fixture accepts unauthenticated CONNECT only to its own local origin.
// IPv6 ::1 requests route to the same IPv4 listener after checking the wire
// address, so this does not depend on external IPv6 routing or credentials.
const proxy = net.createServer(client => {
  track(client);
  let buffer = Buffer.alloc(0);
  let phase = 'greeting';
  function consume(chunk) {
    buffer = Buffer.concat([buffer, chunk]);
    if (phase === 'greeting') {
      if (buffer.length < 2 || buffer.length < 2 + buffer[1]) return;
      if (buffer[0] !== 5 || !buffer.subarray(2, 2 + buffer[1]).includes(0)) return client.destroy();
      buffer = buffer.subarray(2 + buffer[1]);
      client.write(Buffer.from([5, 0]));
      phase = 'request';
    }
    if (phase === 'request') {
      if (buffer.length < 5) return;
      if (buffer[0] !== 5 || buffer[1] !== 1 || buffer[2] !== 0) return client.destroy();
      const type = buffer[3];
      const length = type === 1 ? 4 : type === 4 ? 16 : type === 3 ? 1 + buffer[4] : 0;
      if (!length) return client.destroy();
      if (buffer.length < 4 + length + 2) return;
      const raw = buffer.subarray(4, 4 + length);
      const host = type === 1 ? [...raw].join('.') : type === 3 ? raw.subarray(1).toString() : [...Array(8)].map((_, index) => raw.readUInt16BE(index * 2).toString(16)).join(':');
      const port = buffer.readUInt16BE(4 + length);
      if (!['127.0.0.1', '0:0:0:0:0:0:0:1'].includes(host) || port !== origin.address().port) return client.destroy();
      proxyRequests.push({ host, type, port });
      buffer = buffer.subarray(4 + length + 2);
      phase = 'connected';
      client.removeListener('data', consume);
      const upstream = track(net.connect({ host: '127.0.0.1', port }, () => {
        client.write(Buffer.from([5, 0, 0, 1, 127, 0, 0, 1, 0, 0]));
        if (buffer.length) upstream.write(buffer);
        client.pipe(upstream).pipe(client);
      }));
      upstream.on('error', () => client.destroy());
      client.on('close', () => upstream.destroy());
    }
  }
  client.on('data', consume);
});

function runNpm(args, cwd) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [path.join(installation.owner, 'bin/npm-cli.js'), ...args], {
      cwd, env: { ...process.env, NO_PROXY: '', no_proxy: '', npm_config_noproxy: '', npm_config_update_notifier: 'false', npm_config_cache: path.join(temporary, 'cache') },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let output = '';
    const timeout = setTimeout(() => { child.kill(); reject(new Error('Local npm operation timed out')); }, 12000);
    child.stdout.on('data', bytes => { output += bytes; });
    child.stderr.on('data', bytes => { output += bytes; });
    child.on('error', error => { clearTimeout(timeout); reject(error); });
    child.on('exit', code => { clearTimeout(timeout); code === 0 ? resolve(output) : reject(new Error(`npm exited ${code}: ${output}`)); });
  });
}

function get(url, agent) {
  return new Promise((resolve, reject) => {
    const request = http.get(url, { agent, timeout: 5000 }, response => {
      let body = '';
      response.setEncoding('utf8');
      response.on('data', bytes => { body += bytes; });
      response.on('end', () => resolve(body));
    });
    request.on('timeout', () => request.destroy(new Error('Loopback request timed out')));
    request.on('error', reject);
  });
}

try {
  await new Promise(resolve => origin.listen(0, '127.0.0.1', resolve));
  await new Promise(resolve => proxy.listen(0, '127.0.0.1', resolve));
  const originPort = origin.address().port;
  const proxyPort = proxy.address().port;
  const url = `http://127.0.0.1:${originPort}`;
  for (const host of ['127.0.0.1', '::1']) {
    const { socket } = await SocksClient.createConnection({ proxy: { host: '127.0.0.1', port: proxyPort, type: 5 }, command: 'connect', destination: { host, port: originPort }, timeout: 5000 });
    socket.destroy();
  }
  await assert.rejects(SocksClient.createConnection({ proxy: { host: '127.0.0.1', port: proxyPort, type: 5 }, command: 'connect', destination: { host: '127.0.0.1', port: -1 }, timeout: 1000 }));
  const agent = new SocksProxyAgent(`socks5h://127.0.0.1:${proxyPort}`);
  assert.equal(await get(`${url}/proxy-recovery`, agent), 'bidit-loopback-ok');
  agent.destroy();
  const response = await undici.request(`${url}/undici`);
  assert.equal(await response.body.text(), 'bidit-loopback-ok');
  const source = path.join(temporary, 'package');
  const app = path.join(temporary, 'app');
  fs.mkdirSync(source);
  fs.mkdirSync(app);
  fs.writeFileSync(path.join(source, 'package.json'), JSON.stringify({ name: 'bidit-socks-fixture', version: '1.0.0', main: 'index.js', scripts: { install: 'node -e "require(\'node:fs\').writeFileSync(\'script-executed\',\'unexpected\')"' } }));
  fs.writeFileSync(path.join(source, 'index.js'), 'module.exports = "local-package-ok";\n');
  fs.writeFileSync(path.join(app, 'package.json'), '{"private":true}');
  await runNpm(['pack', '--ignore-scripts', '--pack-destination', temporary], source);
  tarball = fs.readFileSync(path.join(temporary, 'bidit-socks-fixture-1.0.0.tgz'));
  fs.rmSync(path.join(temporary, 'cache'), { recursive: true, force: true });
  const beforeInstall = proxyRequests.length;
  await runNpm(['install', '--ignore-scripts', '--no-audit', '--no-fund', '--package-lock=false', '--registry', url, '--proxy', `socks5h://127.0.0.1:${proxyPort}`, '--https-proxy', `socks5h://127.0.0.1:${proxyPort}`, '--noproxy', '', 'bidit-socks-fixture@1.0.0'], app);
  assert(proxyRequests.length > beforeInstall, 'npm must use the controlled SOCKS proxy');
  assert.equal(createRequire(path.join(app, 'package.json'))('bidit-socks-fixture'), 'local-package-ok');
  assert(!fs.existsSync(path.join(app, 'node_modules/bidit-socks-fixture/script-executed')), 'Lifecycle scripts must remain disabled');
  assert(proxyRequests.some(request => request.type === 4), 'IPv6 SOCKS address encoding must be exercised');
  assert(requests.includes('/fixture.tgz'));
  console.log(JSON.stringify({ node: process.version, versions: installation.versions, api, loopbackOnly: true, socksIPv4: true, socksIPv6Encoding: true, malformedSocksOptionsRejected: true, proxyRecovery: true, undiciHttp: true, npmLocalTarInstallViaSocks: true, lifecycleScriptsDisabled: true, proxyConnections: proxyRequests.length, httpRequests: requests }, null, 2));
} finally {
  clearTimeout(deadline);
  for (const socket of sockets) socket.destroy();
  await Promise.all([new Promise(resolve => proxy.close(resolve)), new Promise(resolve => origin.close(resolve)), undici.getGlobalDispatcher().close()]);
  fs.rmSync(temporary, { recursive: true, force: true });
}
