#!/usr/bin/env node
// Verify the official build-only npm parent before application installation.
// Hash correspondence is not a publisher signature or build attestation.
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

export const PIN = Object.freeze({
  source: 'https://registry.npmjs.org/npm/-/npm-12.2.2.tgz',
  sha256: '07fecbf753e2c3bc10ebeb3e9ed8738f6a0cc1292998b79e2e9f1dd2a5525e0c',
  integrity: 'sha512-KglgbZyacf7jH+FHuNRqVuCzQi8VhI2a7J87Ix43FsPTuZoGWmIXXGdZtBUOA79vwL+IJREqURVzupA7PsQTcA==',
  launchers: { npm: '8e5f6f3429f8cdbe693cdc29904e9d5a7b127a494bd15c804bd54c7403bfcbe7', npx: '237adf8f3747cad8b9b62fcfd0d9c8d509a64e550337707f55100afcb79e8900' },
  packages: {
    npm: { version: '12.2.2', identityOnly: true, files: 2, bytes: 16602, manifestSha256: '631cbc5c78097ad8b4fe0b18ddc24eaa6f18d5eb9cd229fc866cec1cf2f1f58b', licenseSha256: '7610d223851f421d315df5e77974f1c68a04b97e02060e5bbbcf13d95e3ca257' },
    socks: { version: '2.8.9', files: 17, bytes: 86573, manifestSha256: '2a0b00b5ce1ee3cc9f6a72282ff34d69961a72383567294c177c6aa1631f621c', licenseSha256: '4f4f28866154c8d758241a64296e8790d45d7d912ff7b029930778c06d8f2a72' },
    'ip-address': { version: '10.7.5', files: 11, bytes: 110174, manifestSha256: 'be9d10e810ce96bf9e0240667da6aa93981317c610e9b29bfe5b5f6a62fa34bf', licenseSha256: 'f8d791359a50cbcac82c29cb7b0b1b889b28be76fdd35b8ee33f0f7c8704702a' },
    undici: { version: '6.29.1', files: 134, bytes: 1102783, manifestSha256: '4e960ae269a4d92ef66a164d84996db228803d0963800094fa87ed2f573c55cd', licenseSha256: 'a6db8096b2707bc0102d256917d4d33f298ba36d8c3f25de067a2b5bb379db27' },
    'node-gyp': { version: '13.0.0', identityOnly: true, files: 2, bytes: 2460, manifestSha256: '055bae8d5d5c5b1878c21ba201c732e9b217a72210a15805bace4ffd9c5370ce', licenseSha256: '662a1b0115251cfb29c6aed0f221f8847bc49c6365d1c53a62c9f4bccc2489c3' },
    'socks-proxy-agent': { version: '10.1.0', identityOnly: true, files: 2, bytes: 4253, manifestSha256: '9d62dfdd669ff77ee6ea899845ed9e9b2f5c466e49a9ed72b351ef5eeda514c1', licenseSha256: '8d8c55319c7729d57be811c747452636688d54f19701ee0752b6b15ad3771d9a' },
  },
});

const digest = (bytes, algorithm = 'sha256', encoding = 'hex') => crypto.createHash(algorithm).update(bytes).digest(encoding);
const inside = (parent, child) => child.startsWith(`${parent}${path.sep}`);

function regularPath(filename, directory = false) {
  const absolute = path.resolve(filename);
  // Refuse links in every ancestor, including a symlinked global root.
  for (let current = absolute; ; current = path.dirname(current)) {
    const info = fs.lstatSync(current);
    assert(!info.isSymbolicLink(), `Unexpected symlink: ${current}`);
    if (current === absolute) assert(directory ? info.isDirectory() : info.isFile(), `Unexpected file type: ${current}`);
    if (current === path.dirname(current)) break;
  }
  assert.equal(fs.realpathSync(absolute), absolute, 'Path must be canonical');
  return absolute;
}

export function verifyArchive(filename, pin = PIN) {
  const bytes = fs.readFileSync(regularPath(filename));
  assert.equal(digest(bytes), pin.sha256, 'npm archive SHA256 differs');
  assert.equal(`sha512-${digest(bytes, 'sha512', 'base64')}`, pin.integrity, 'npm archive SRI differs');
  return pin.sha256;
}

// The manifest hashes JSON.stringify(sorted [relative POSIX path, size, SHA256]
// triples). Complete protected leaf packages are checked, including their notice.
// The npm and immediate consumer identities select package.json + LICENSE.
export function inventory(directory, identityOnly = false) {
  const entries = [];
  function walk(current) {
    for (const name of fs.readdirSync(current).sort()) {
      const filename = path.join(current, name);
      const info = fs.lstatSync(filename);
      assert(!info.isSymbolicLink(), `Package contains a symlink: ${filename}`);
      if (info.isDirectory()) walk(filename);
      else {
        assert(info.isFile(), `Package contains an unsupported file: ${filename}`);
        const bytes = fs.readFileSync(filename);
        entries.push([path.relative(directory, filename).split(path.sep).join('/'), bytes.length, digest(bytes)]);
      }
    }
  }
  if (identityOnly) {
    for (const name of ['LICENSE', 'package.json']) {
      const bytes = fs.readFileSync(regularPath(path.join(directory, name)));
      entries.push([name, bytes.length, digest(bytes)]);
    }
  } else walk(directory);
  entries.sort((left, right) => left[0] < right[0] ? -1 : left[0] > right[0] ? 1 : 0);
  return { files: entries.length, bytes: entries.reduce((sum, entry) => sum + entry[1], 0), manifestSha256: digest(JSON.stringify(entries)) };
}

export function validateInstallation(globalRoot, pin = PIN) {
  const global = regularPath(globalRoot, true);
  const owner = regularPath(path.join(global, 'npm'), true);
  assert(inside(global, owner), 'npm must belong to the global root');
  const binaries = regularPath(path.resolve(global, '../../bin'), true);
  for (const [name, expected] of Object.entries(pin.launchers)) {
    const launcher = path.join(binaries, name);
    assert(fs.lstatSync(launcher).isSymbolicLink(), `Expected the npm-owned ${name} launcher link`);
    const script = regularPath(path.join(owner, 'bin', `${name}-cli.js`));
    assert.equal(fs.realpathSync(launcher), script, `Unexpected ${name} launcher owner`);
    assert.equal(digest(fs.readFileSync(script)), expected, `Changed ${name} launcher bytes`);
  }
  const roots = Object.fromEntries(Object.keys(pin.packages).map(name => [name, name === 'npm' ? owner : path.join(owner, 'node_modules', name)]));
  const seen = Object.fromEntries(Object.keys(roots).map(name => [name, []]));
  function scan(current) {
    for (const name of fs.readdirSync(current)) {
      const filename = path.join(current, name);
      const info = fs.lstatSync(filename);
      if (info.isSymbolicLink()) {
        // npm can generate command links; they must resolve to a regular file
        // within this owner. Package-directory links are always refused.
        const target = fs.realpathSync(filename);
        assert(path.basename(current) === '.bin' && inside(owner, target) && fs.statSync(target).isFile(), `Unexpected symlink: ${filename}`);
      } else if (info.isDirectory()) {
        if (Object.hasOwn(roots, name) && path.basename(current) === 'node_modules') assert.equal(filename, roots[name], `Unexpected ${name} package directory`);
        scan(filename);
      }
      else {
        assert(info.isFile(), `Unexpected file type: ${filename}`);
        if (name === 'package.json') {
          const metadata = JSON.parse(fs.readFileSync(filename, 'utf8'));
          if (Object.hasOwn(seen, metadata.name)) seen[metadata.name].push(current);
        }
      }
    }
  }
  scan(owner);
  const metadata = {};
  for (const [name, expected] of Object.entries(pin.packages)) {
    const root = regularPath(roots[name], true);
    assert(name === 'npm' || inside(owner, root), 'Dependency must belong to npm');
    assert.deepEqual(seen[name], [root], `Unexpected or duplicate ${name} ownership`);
    metadata[name] = JSON.parse(fs.readFileSync(regularPath(path.join(root, 'package.json')), 'utf8'));
    assert.equal(metadata[name].name, name, `Wrong ${name} owner`);
    assert.equal(metadata[name].version, expected.version, `Wrong ${name} version`);
    assert.deepEqual(inventory(root, expected.identityOnly), { files: expected.files, bytes: expected.bytes, manifestSha256: expected.manifestSha256 }, `Changed ${name} package bytes`);
    assert.equal(digest(fs.readFileSync(regularPath(path.join(root, 'LICENSE')))), expected.licenseSha256, `Changed ${name} license`);
  }
  const edges = [
    ['socks', 'ip-address', '^10.1.1'],
    ['socks-proxy-agent', 'socks', '^2.8.3'],
    ['node-gyp', 'undici', '^6.25.0'],
  ];
  for (const [consumer, dependency, range] of edges) {
    assert.equal(metadata[consumer].dependencies?.[dependency], range, `Changed ${consumer} dependency range`);
    const require = createRequire(path.join(roots[consumer], 'package.json'));
    const manifest = regularPath(require.resolve(`${dependency}/package.json`));
    assert.equal(manifest, path.join(roots[dependency], 'package.json'), `Unexpected ${consumer} resolution`);
    const entry = regularPath(require.resolve(dependency));
    assert(inside(roots[dependency], entry), `Unexpected ${consumer} entry resolution`);
    assert.equal(entry, path.resolve(roots[dependency], metadata[dependency].main || 'index.js'), `Changed ${dependency} entry`);
  }
  assert.equal(metadata['ip-address'].main, 'dist/ip-address.js');
  assert.equal(metadata['ip-address'].types, 'dist/ip-address.d.ts');
  assert.equal(metadata['ip-address'].license, 'MIT');
  assert(!Object.hasOwn(metadata['ip-address'], 'exports'), 'Unexpected ip-address exports map');
  for (const key of ['dependencies', 'optionalDependencies', 'peerDependencies']) assert.equal(Object.keys(metadata['ip-address'][key] || {}).length, 0, `Unexpected ip-address ${key}`);
  return { globalRoot: global, owner, roots, versions: Object.fromEntries(Object.entries(metadata).map(([name, value]) => [name, value.version])) };
}

export function probeApi(installation) {
  const require = createRequire(path.join(installation.roots.socks, 'package.json'));
  const { Address4, Address6 } = require('ip-address');
  assert.equal(new Address4('192.0.2.42/24').correctForm(), '192.0.2.42');
  assert.equal(new Address6('2001:db8::42/64').correctForm(), '2001:db8::42');
  assert.equal(new Address6('::ffff:192.0.2.42').to4().correctForm(), '192.0.2.42');
  for (const [Address, invalid] of [[Address4, '999.1.1.1'], [Address4, '192.0.2'], [Address6, '2001:db8:::1'], [Address6, 'not-an-ip']]) assert.throws(() => new Address(invalid));
  assert.equal(new Address4('127.0.0.1').correctForm(), '127.0.0.1');
  const socks = require('socks');
  assert.equal(typeof socks.SocksClient.createConnection, 'function');
  const gypRequire = createRequire(path.join(installation.roots['node-gyp'], 'package.json'));
  const undici = gypRequire('undici');
  assert.equal(new undici.Headers({ 'x-bidit': 'fixed-parent' }).get('x-bidit'), 'fixed-parent');
  assert.equal(typeof undici.fetch, 'function');
  return { ipv4: true, ipv6: true, malformedInputRejected: true, recovery: true, socksApi: true, undiciApi: true };
}

export function retainProvenance(installation, destination, api, pin = PIN) {
  const target = path.resolve(destination);
  // Validation finishes before any provenance write. Refuse output links too.
  regularPath(path.dirname(target), true);
  if (!fs.existsSync(target)) fs.mkdirSync(target);
  regularPath(target, true);
  for (const filename of ['npm-parent.json', 'security-patch.txt', ...Object.keys(pin.packages).map(name => `${name}.LICENSE`)]) {
    const output = path.join(target, filename);
    if (fs.lstatSync(output, { throwIfNoEntry: false })) regularPath(output);
  }
  const receipt = {
    owner: `npm@${pin.packages.npm.version}`, source: pin.source, sha256: pin.sha256, integrity: pin.integrity,
    verification: 'Pinned archive hashes and installed package correspondence; no independent publisher signature verified',
    versions: installation.versions, paths: installation.roots, api,
    repairs: [
      { package: 'ip-address', previous: '10.7.2', current: '10.7.5', advisory: 'GHSA-rxp7-6hxx-x8q5 / CVE-2026-111997', delivery: 'official fixed npm parent', files: 11, bytes: 110174, upstreamFullFiles: 30, upstreamFullBytes: 368787 },
      { package: 'undici', previous: '6.29.1', current: '6.29.1', advisory: 'GHSA-xjj6-f33q-929c / CVE-2026-109869', delivery: 'official parent retains prior downstream fix', upstreamArchiveSha256: '29c4b8e0b2d86cb8795571c1959012ce057a14ecfd34858335f47c31e770a04f' },
    ],
    scope: 'Build-only npm tooling. Full upstream leaf archives contain declarations/maps/README omitted by npm pruning. No served-asset equivalence claim.',
    unresolved: ['glob 13.0.6 and minimatch 10.2.5 remain; incompatible fixed majors are not forced'],
  };
  for (const name of Object.keys(pin.packages)) fs.copyFileSync(path.join(installation.roots[name], 'LICENSE'), path.join(target, `${name}.LICENSE`));
  fs.writeFileSync(path.join(target, 'npm-parent.json'), `${JSON.stringify(receipt, null, 2)}\n`);
  fs.writeFileSync(path.join(target, 'security-patch.txt'), `npm-version: 12.2.2\nfixed-parent: ${pin.source}\nsha256: ${pin.sha256}\nretained-fixed-dependency: undici 6.29.1\nfixed-dependency: ip-address 10.7.2 -> 10.7.5\nadvisories: GHSA-xjj6-f33q-929c / CVE-2026-109869; GHSA-rxp7-6hxx-x8q5 / CVE-2026-111997\nprovenance: npm-parent.json\n`);
  return receipt;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const [mode, filename, destination] = process.argv.slice(2);
  if (mode === 'archive' && filename && !destination) verifyArchive(filename);
  else if (mode === 'installed' && filename && destination) {
    const installation = validateInstallation(filename);
    const api = probeApi(installation);
    retainProvenance(installation, destination, api);
    console.log(JSON.stringify({ versions: installation.versions, api }));
  } else throw new Error('Usage: verify-npm-toolchain.mjs archive TARBALL | installed GLOBAL_ROOT PROVENANCE_DIRECTORY');
}
