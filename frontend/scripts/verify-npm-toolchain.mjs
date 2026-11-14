#!/usr/bin/env node
// Verify the official build-only npm parent before application installation.
// Hash correspondence is not a publisher signature or build attestation.
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { gunzipSync } from 'node:zlib';
import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';

export const PIN = Object.freeze({
  source: 'https://registry.npmjs.org/npm/-/npm-12.2.2.tgz',
  sha256: '07fecbf753e2c3bc10ebeb3e9ed8738f6a0cc1292998b79e2e9f1dd2a5525e0c',
  integrity: 'sha512-KglgbZyacf7jH+FHuNRqVuCzQi8VhI2a7J87Ix43FsPTuZoGWmIXXGdZtBUOA79vwL+IJREqURVzupA7PsQTcA==',
  parentTree: { files: 1923, bytes: 12190082, manifestSha256: 'ff1cc2933960f3f8799a0c8fd52cde4e7438202397bd2c834d2809a107e3dd3d' },
  glob: {
    source: 'https://registry.npmjs.org/glob/-/glob-13.0.7.tgz',
    sha256: '58342994ba61c09d9c27365d0930195c5583fe31e80c1683544805cebc065fd4',
    integrity: 'sha512-wfsFgXQ8A3SEI9XGf7Nl3Y9l7ffSrGh7SUCT7vU/2CzYPep9kjxAn1S40F9OPKxAMGCVl7HtNlJhrCJUeUq83g==',
    archiveBytes: 364666, tarBytes: 1658880,
    stock: { version: '13.0.6', files: 20, bytes: 268988, manifestSha256: 'e96d12454a16cfbbf3a36cb21e71d0f34f8e21de4dcdec14625e38ff64899cca' },
    current: { version: '13.0.7', files: 65, bytes: 1607352, manifestSha256: 'dd7bc734619392667f4eea7b183b0445016711326111b91fff07b111abe7dea8' },
    licenseSha256: 'a49c9ba464796f65b59fca3f1e6ca40912df1e859f575383223f7ec6c5baae09',
  },
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

function validateParent(globalRoot, pin) {
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

// Only the canonical glob directory is replaceable. Every other parent file,
// including the raw glob dependency closure, stays byte-identical to npm's tarball.
export function parentInventory(owner) {
  const entries = [];
  function walk(current) {
    for (const name of fs.readdirSync(current).sort()) {
      const filename = path.join(current, name);
      const relative = path.relative(owner, filename).split(path.sep).join('/');
      if (relative === 'node_modules/glob') continue;
      const info = fs.lstatSync(filename);
      if (info.isSymbolicLink()) {
        const target = fs.realpathSync(filename);
        assert(path.basename(current) === '.bin' && inside(owner, target) && fs.statSync(target).isFile(), `Unexpected symlink: ${filename}`);
      } else if (info.isDirectory()) walk(filename);
      else {
        assert(info.isFile(), `Unexpected parent file type: ${filename}`);
        const bytes = fs.readFileSync(filename);
        entries.push([relative, bytes.length, digest(bytes)]);
      }
    }
  }
  walk(regularPath(owner, true));
  entries.sort((a, b) => a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0);
  return { files: entries.length, bytes: entries.reduce((sum, entry) => sum + entry[1], 0), manifestSha256: digest(JSON.stringify(entries)) };
}

export const GLOB_CONSUMERS = Object.freeze([
  ['.', 'npm', '12.2.2', '^13.0.6'],
  ['node_modules/@npmcli/map-workspaces', '@npmcli/map-workspaces', '6.0.0', '^13.0.0'],
  ['node_modules/@npmcli/package-json', '@npmcli/package-json', '8.0.0', '^13.0.0'],
  ['node_modules/cacache', 'cacache', '21.0.1', '^13.0.0'],
  ['node_modules/npm-packlist', 'npm-packlist', '11.3.0', '^13.0.6'],
]);
export const GLOB_CLOSURE = Object.freeze([
  ['glob', 'node_modules/glob', null, { minimatch: '^10.2.2', minipass: '^7.1.3', 'path-scurry': '^2.0.2' }],
  ['minimatch', 'node_modules/minimatch', '10.2.5', { 'brace-expansion': '^5.0.5' }],
  ['minipass', 'node_modules/minipass', '7.1.3', {}],
  ['path-scurry', 'node_modules/path-scurry', '2.0.2', { 'lru-cache': '^11.0.0', minipass: '^7.1.2' }],
  ['lru-cache', 'node_modules/lru-cache', '11.5.2', {}],
  ['brace-expansion', 'node_modules/brace-expansion', '5.0.12', { 'balanced-match': '^4.0.2' }],
  ['balanced-match', 'node_modules/balanced-match', '4.0.4', {}],
  ['minipass', 'node_modules/minipass-pipeline/node_modules/minipass', '3.3.6', { yallist: '^4.0.0' }],
]);
export function globExports() {
  const entry = (kind, minified = false) => ({ types: `./dist/${kind}/index.d.ts`, default: `./dist/${kind}/index${minified ? '.min' : ''}.js` });
  return { './package.json': './package.json', './raw': { import: entry('esm'), require: entry('commonjs') }, '.': { import: entry('esm', true), require: entry('commonjs', true) } };
}

function validateGlobPackage(root, expected, pin) {
  regularPath(root, true);
  const metadata = JSON.parse(fs.readFileSync(regularPath(path.join(root, 'package.json')), 'utf8'));
  assert.equal(metadata.name, 'glob', 'Wrong glob identity');
  assert.equal(metadata.version, expected.version, 'Wrong glob version');
  assert.equal(metadata.type, 'module', 'Changed glob module type');
  assert.equal(metadata.main, './dist/commonjs/index.min.js', 'Changed glob main');
  assert.equal(metadata.module, './dist/esm/index.min.js', 'Changed glob module');
  assert.equal(metadata.types, './dist/commonjs/index.d.ts', 'Changed glob types');
  assert.deepEqual(metadata.exports, globExports(), 'Changed glob exports');
  assert.deepEqual(metadata.engines, { node: '18 || 20 || >=22' }, 'Changed glob engines');
  assert.equal(metadata.license, 'BlueOak-1.0.0', 'Changed glob license identity');
  assert.deepEqual(metadata.dependencies, GLOB_CLOSURE[0][3], 'Changed glob dependency range');
  for (const key of ['optionalDependencies', 'peerDependencies']) assert.equal(Object.keys(metadata[key] || {}).length, 0, `Unexpected glob ${key}`);
  if (expected.version === pin.glob.current.version) assert.deepEqual(metadata.publishConfig, { tag: 'v13-legacy' }, 'Changed glob publication channel');
  assert.deepEqual(inventory(root), { files: expected.files, bytes: expected.bytes, manifestSha256: expected.manifestSha256 }, 'Changed glob package bytes');
  assert.equal(digest(fs.readFileSync(regularPath(path.join(root, 'LICENSE.md')))), pin.glob.licenseSha256, 'Changed glob notice');
}

function validateGlob(installation, pin, stock = false) {
  const { owner } = installation;
  const expected = stock ? pin.glob.stock : pin.glob.current;
  const root = path.join(owner, 'node_modules/glob');
  validateGlobPackage(root, expected, pin);
  const protectedNames = new Set(GLOB_CLOSURE.map(([name]) => name));
  const allowed = new Set(GLOB_CLOSURE.map(([, relative]) => relative));
  const seen = [];
  function scan(current) {
    for (const name of fs.readdirSync(current)) {
      const filename = path.join(current, name);
      const info = fs.lstatSync(filename);
      if (info.isDirectory()) {
        const relative = path.relative(owner, filename).split(path.sep).join('/');
        if (path.basename(current) === 'node_modules' && protectedNames.has(name)) assert(allowed.has(relative), `Unexpected ${name} package directory`);
        scan(filename);
      } else if (info.isFile() && name === 'package.json') {
        const metadata = JSON.parse(fs.readFileSync(filename, 'utf8'));
        if (protectedNames.has(metadata.name)) seen.push(path.relative(owner, current).split(path.sep).join('/'));
      }
    }
  }
  scan(owner);
  assert.deepEqual(seen.sort(), [...allowed].sort(), 'Unexpected or duplicate glob closure ownership');
  for (const [name, relative, version, dependencies] of GLOB_CLOSURE) {
    const directory = regularPath(path.join(owner, relative), true);
    const metadata = JSON.parse(fs.readFileSync(regularPath(path.join(directory, 'package.json')), 'utf8'));
    assert.equal(metadata.name, name, `Wrong ${name} closure identity`);
    assert.equal(metadata.version, version || expected.version, `Wrong ${name} closure version`);
    assert.deepEqual(metadata.dependencies || {}, dependencies, `Changed ${name} dependency range`);
    for (const key of ['optionalDependencies', 'peerDependencies']) assert.equal(Object.keys(metadata[key] || {}).length, 0, `Unexpected ${name} ${key}`);
    // The nested legacy minipass belongs to minipass-pipeline, not glob's raw API.
    if (relative.includes('minipass-pipeline/')) continue;
    for (const dependency of Object.keys(dependencies)) {
      const require = createRequire(path.join(directory, 'package.json'));
      const entry = regularPath(require.resolve(dependency));
      assert(inside(path.join(owner, 'node_modules', dependency), entry), `Unexpected ${name} raw dependency resolution`);
    }
  }
  for (const [relative, name, version, range] of GLOB_CONSUMERS) {
    const directory = regularPath(path.join(owner, relative), true);
    const metadata = JSON.parse(fs.readFileSync(regularPath(path.join(directory, 'package.json')), 'utf8'));
    assert.equal(metadata.name, name, 'Changed glob consumer identity');
    assert.equal(metadata.version, version, `Wrong ${name} consumer version`);
    assert.equal(metadata.dependencies?.glob, range, `Changed ${name} glob dependency range`);
    const require = createRequire(path.join(directory, 'package.json'));
    assert.equal(regularPath(require.resolve('glob')), path.join(root, 'dist/commonjs/index.min.js'), `Unexpected ${name} glob resolution`);
    assert.equal(regularPath(require.resolve('glob/raw')), path.join(root, 'dist/commonjs/index.js'), `Unexpected ${name} raw glob resolution`);
  }
  const esm = spawnSync(process.execPath, ['--input-type=module', '--eval', 'process.stdout.write(import.meta.resolve("glob"))'], { cwd: owner, env: {}, encoding: 'utf8', timeout: 5000 });
  assert.equal(esm.status, 0, 'Glob ESM resolution failed');
  assert.equal(esm.stdout, pathToFileURL(path.join(root, 'dist/esm/index.min.js')).href, 'Unexpected glob ESM resolution');
  assert.deepEqual(parentInventory(owner), pin.parentTree, 'Changed non-glob parent bytes');
  installation.roots.glob = root;
  installation.versions.glob = expected.version;
  return installation;
}

export function validateInstallation(globalRoot, pin = PIN) {
  return validateGlob(validateParent(globalRoot, pin), pin);
}

// This is intentionally the fixed published archive format, not a general tar
// extractor: 65 regular ustar files, one package/ prefix, no links/extensions.
export function readGlobArchive(filename, pin = PIN) {
  const descriptor = fs.openSync(regularPath(filename), fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  let archive;
  try {
    const info = fs.fstatSync(descriptor);
    assert(info.isFile(), 'Glob archive must be a regular file');
    assert.equal(info.size, pin.glob.archiveBytes, 'Glob archive length differs');
    archive = Buffer.alloc(pin.glob.archiveBytes);
    let offset = 0;
    while (offset < archive.length) {
      const count = fs.readSync(descriptor, archive, offset, archive.length - offset, offset);
      assert(count > 0, 'Glob archive shortened while reading');
      offset += count;
    }
    assert.equal(fs.fstatSync(descriptor).size, archive.length, 'Glob archive changed while reading');
  } finally {
    fs.closeSync(descriptor);
  }
  assert.equal(digest(archive), pin.glob.sha256, 'Glob archive SHA256 differs');
  assert.equal(`sha512-${digest(archive, 'sha512', 'base64')}`, pin.glob.integrity, 'Glob archive SRI differs');
  const tar = gunzipSync(archive, { maxOutputLength: pin.glob.tarBytes });
  assert.equal(tar.length, pin.glob.tarBytes, 'Glob tar length differs');
  const entries = [];
  const names = new Set();
  const text = bytes => bytes.toString('utf8').replace(/\0.*$/s, '');
  let offset = 0;
  while (offset + 512 <= tar.length && tar.subarray(offset, offset + 512).some(byte => byte !== 0)) {
    const header = tar.subarray(offset, offset + 512);
    const sum = header.reduce((total, byte, index) => total + (index >= 148 && index < 156 ? 32 : byte), 0);
    assert.equal(sum, Number.parseInt(text(header.subarray(148, 156)).trim(), 8), 'Invalid glob tar header checksum');
    assert.equal(header.subarray(257, 265).toString('binary'), 'ustar\x0000', 'Unexpected glob archive format');
    assert.equal(text(header.subarray(345, 500)), '', 'Unexpected glob tar prefix');
    assert.equal(header[156], 48, 'Glob archive permits regular files only');
    const name = text(header.subarray(0, 100));
    assert(name.startsWith('package/') && !name.includes('\\') && !name.includes('\ufffd') && !name.split('/').some(part => ['', '.', '..'].includes(part)), 'Unsafe glob archive path');
    assert(!names.has(name), 'Duplicate glob archive path');
    names.add(name);
    const sizeText = text(header.subarray(124, 136)).trim();
    assert(/^[0-7]+$/.test(sizeText), 'Invalid glob archive size');
    const size = Number.parseInt(sizeText, 8);
    const start = offset + 512;
    assert(size <= pin.glob.current.bytes && start + size <= tar.length, 'Glob archive entry exceeds bound');
    entries.push([name.slice(8), tar.subarray(start, start + size)]);
    assert(entries.length <= pin.glob.current.files, 'Too many glob archive files');
    offset = start + Math.ceil(size / 512) * 512;
  }
  assert(tar.length - offset >= 1024 && !tar.subarray(offset).some(byte => byte !== 0), 'Invalid glob tar ending');
  const triples = entries.map(([name, bytes]) => [name, bytes.length, digest(bytes)]).sort((a, b) => a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0);
  assert.deepEqual({ files: entries.length, bytes: entries.reduce((sum, [, bytes]) => sum + bytes.length, 0), manifestSha256: digest(JSON.stringify(triples)) }, { files: pin.glob.current.files, bytes: pin.glob.current.bytes, manifestSha256: pin.glob.current.manifestSha256 }, 'Changed glob archive contents');
  return entries;
}

export function overlayGlob(globalRoot, filename, pin = PIN) {
  const entries = readGlobArchive(filename, pin);
  const original = validateGlob(validateParent(globalRoot, pin), pin, true);
  const target = original.roots.glob;
  const stage = fs.mkdtempSync(path.join(original.globalRoot, '.bidit-glob-'));
  const candidate = path.join(stage, 'candidate');
  const previous = path.join(stage, 'previous');
  fs.mkdirSync(candidate);
  let detached = false;
  let installed = false;
  let cleanup = true;
  try {
    for (const [name, bytes] of entries) {
      const output = path.join(candidate, name);
      fs.mkdirSync(path.dirname(output), { recursive: true });
      fs.writeFileSync(output, bytes, { flag: 'wx', mode: 0o644 });
    }
    validateGlobPackage(candidate, pin.glob.current, pin);
    fs.renameSync(target, previous);
    detached = true;
    fs.renameSync(candidate, target);
    installed = true;
    const result = validateInstallation(globalRoot, pin);
    probeApi(result);
    return result;
  } catch (error) {
    try {
      if (installed) fs.rmSync(target, { recursive: true });
      if (detached) fs.renameSync(previous, target);
    } catch (rollbackError) {
      cleanup = false;
      throw new AggregateError([error, rollbackError], `Glob overlay rollback failed; original tree retained at ${previous}`);
    }
    throw error;
  } finally {
    if (cleanup) fs.rmSync(stage, { recursive: true, force: true });
  }
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

// Run the public default and raw APIs separately: minified entry points embed
// their own walker and cannot be qualified by inspecting only walker.js.
async function executeGlobProbe(installation, temporary) {
  const require = createRequire(path.join(installation.owner, 'package.json'));
  const implementations = [
    ['commonjs-default', require('glob')],
    ['commonjs-raw', require('glob/raw')],
    ['esm-default', await import(pathToFileURL(path.join(installation.roots.glob, 'dist/esm/index.min.js')).href)],
    ['esm-raw', await import(pathToFileURL(path.join(installation.roots.glob, 'dist/esm/index.js')).href)],
  ];
  assert.equal(fs.readdirSync(regularPath(temporary, true)).length, 0, 'Glob proof directory must be empty');
  const expected = ['root.txt', ...Array.from({ length: 5 }, (_, index) => `dir${index}/leaf.txt`)].sort();
  {
    for (const name of expected) {
      const filename = path.join(temporary, name);
      fs.mkdirSync(path.dirname(filename), { recursive: true });
      fs.writeFileSync(filename, 'owned finite glob API fixture\n', { flag: 'wx' });
    }
    const result = {};
    for (const [name, api] of implementations) {
      assert.deepEqual(api.globSync('**/*.txt', { cwd: temporary }).sort(), expected, `${name} sync result differs`);
      assert.deepEqual((await api.glob('**/*.txt', { cwd: temporary })).sort(), expected, `${name} async result differs`);
      assert.deepEqual([...api.globStreamSync('**/*.txt', { cwd: temporary })].sort(), expected, `${name} sync stream differs`);
      const streamed = [];
      for await (const value of api.globStream('**/*.txt', { cwd: temporary })) streamed.push(value);
      assert.deepEqual(streamed.sort(), expected, `${name} async stream differs`);
      const controller = new AbortController();
      const reason = new Error('bidit-owned-first-read-abort');
      let reads = 0;
      let pending = 0;
      let drained;
      const callbacksDone = new Promise(resolve => { drained = resolve; });
      const controlledFs = { ...fs, readdir(...args) {
        const callback = args.pop();
        const first = ++reads === 1;
        pending++;
        fs.readdir(...args, (...values) => {
          try {
            if (first) controller.abort(reason);
            callback(...values);
          } finally {
            if (--pending === 0) drained();
          }
        });
      } };
      await assert.rejects(api.glob('**/*.txt', { cwd: temporary, signal: controller.signal, fs: controlledFs }), error => error === reason, `${name} abort reason differs`);
      await callbacksDone;
      assert.equal(pending, 0, `${name} filesystem callbacks remain`);
      assert.equal(reads, 1, `${name} continued walking after abort`);
      assert.deepEqual((await api.glob('**/*.txt', { cwd: temporary })).sort(), expected, `${name} recovery differs`);
      result[name] = { sync: true, async: true, syncStream: true, asyncStream: true, abortReasonPreserved: true, abortedDirectoryReads: reads, pendingCallbacks: pending, recovery: true };
    }
    return result;
  }
}

// The parent owns and removes the fixture only after the child has exited. A
// callback-settlement regression therefore fails the build proof within 15s,
// writes no provenance, and cannot leave a live child racing fixture cleanup.
// This is a build qualification watchdog, not an application timeout policy.
export function probeGlobApi(installation) {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'bidit-glob-api-'));
  try {
    const child = spawnSync(process.execPath, [fileURLToPath(import.meta.url), 'glob-probe', installation.globalRoot, temporary], { cwd: installation.owner, env: {}, encoding: 'utf8', timeout: 15000, killSignal: 'SIGKILL', maxBuffer: 1024 * 1024 });
    assert.equal(child.status, 0, `Glob API proof failed or exceeded its 15-second watchdog: ${child.error?.message || child.stderr}`);
    return JSON.parse(child.stdout);
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}

export function retainProvenance(installation, destination, api, pin = PIN) {
  const target = path.resolve(destination);
  // Validation finishes before any provenance write. Refuse output links too.
  regularPath(path.dirname(target), true);
  if (!fs.existsSync(target)) fs.mkdirSync(target);
  regularPath(target, true);
  for (const filename of ['npm-parent.json', 'security-patch.txt', 'glob.LICENSE', ...Object.keys(pin.packages).map(name => `${name}.LICENSE`)]) {
    const output = path.join(target, filename);
    if (fs.lstatSync(output, { throwIfNoEntry: false })) regularPath(output);
  }
  const receipt = {
    owner: `npm@${pin.packages.npm.version}`, source: pin.source, sha256: pin.sha256, integrity: pin.integrity,
    verification: 'Pinned archive hashes and installed package correspondence; no independent publisher signature verified',
    variant: 'official npm parent plus pinned official glob legacy leaf overlay',
    leafOverlay: { package: 'glob', previous: pin.glob.stock.version, current: pin.glob.current.version, source: pin.glob.source, sha256: pin.glob.sha256, integrity: pin.glob.integrity, ...pin.glob.current, licenseSha256: pin.glob.licenseSha256 },
    nonGlobParent: pin.parentTree,
    versions: installation.versions, paths: installation.roots, api,
    repairs: [
      { package: 'ip-address', previous: '10.7.2', current: '10.7.5', advisory: 'GHSA-rxp7-6hxx-x8q5 / CVE-2026-111997', delivery: 'official fixed npm parent', files: 11, bytes: 110174, upstreamFullFiles: 30, upstreamFullBytes: 368787 },
      { package: 'undici', previous: '6.29.1', current: '6.29.1', advisory: 'GHSA-xjj6-f33q-929c / CVE-2026-109869', delivery: 'official parent retains prior downstream fix', upstreamArchiveSha256: '29c4b8e0b2d86cb8795571c1959012ce057a14ecfd34858335f47c31e770a04f' },
    ],
    scope: 'Build-only npm tooling. Full upstream leaf archives contain declarations/maps/README omitted by npm pruning. No served-asset equivalence claim.',
    unresolved: ['Glob 13.0.7 has the official abort backport but GHSA-3g3m-2fjr-6pw4 / CVE-2026-118773 metadata still includes it; preserve scanner findings and ratings', 'Minimatch 10.2.5 / GHSA-g3hv-75wg-m249 / CVE-2026-118764 remains; incompatible fixed majors are not forced'],
  };
  for (const name of Object.keys(pin.packages)) fs.copyFileSync(path.join(installation.roots[name], 'LICENSE'), path.join(target, `${name}.LICENSE`));
  fs.copyFileSync(path.join(installation.roots.glob, 'LICENSE.md'), path.join(target, 'glob.LICENSE'));
  fs.writeFileSync(path.join(target, 'npm-parent.json'), `${JSON.stringify(receipt, null, 2)}\n`);
  fs.writeFileSync(path.join(target, 'security-patch.txt'), `npm-version: 12.2.2\nfixed-parent: ${pin.source}\nsha256: ${pin.sha256}\nretained-fixed-dependency: undici 6.29.1\nfixed-dependency: ip-address 10.7.2 -> 10.7.5\nadvisories: GHSA-xjj6-f33q-929c / CVE-2026-109869; GHSA-rxp7-6hxx-x8q5 / CVE-2026-111997\nofficial-legacy-leaf: glob 13.0.6 -> 13.0.7\nleaf-sha256: ${pin.glob.sha256}\nleaf-advisory-metadata: still affected; retain findings and ratings\nprovenance: npm-parent.json\n`);
  return receipt;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const [mode, filename, destination] = process.argv.slice(2);
  if (mode === 'archive' && filename && !destination) verifyArchive(filename);
  else if (mode === 'glob-probe' && filename && destination) {
    const installation = validateInstallation(filename);
    console.log(JSON.stringify(await executeGlobProbe(installation, destination)));
  }
  else if (mode === 'overlay' && filename && destination) {
    const installation = overlayGlob(filename, destination);
    console.log(JSON.stringify({ versions: installation.versions, overlay: 'official glob legacy leaf', provenanceWritten: false }));
  }
  else if (mode === 'installed' && filename && destination) {
    const installation = validateInstallation(filename);
    const api = { ...probeApi(installation), glob: await probeGlobApi(installation) };
    retainProvenance(installation, destination, api);
    console.log(JSON.stringify({ versions: installation.versions, api }));
  } else throw new Error('Usage: verify-npm-toolchain.mjs archive TARBALL | overlay GLOBAL_ROOT GLOB_TARBALL | installed GLOBAL_ROOT PROVENANCE_DIRECTORY');
}
