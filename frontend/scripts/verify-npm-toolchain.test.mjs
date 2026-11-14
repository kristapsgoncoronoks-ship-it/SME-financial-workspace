import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { gzipSync } from 'node:zlib';
import { PIN, GLOB_CONSUMERS, GLOB_CLOSURE, globExports, inventory, parentInventory, overlayGlob, readGlobArchive, retainProvenance, validateInstallation, verifyArchive } from './verify-npm-toolchain.mjs';

const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');

function fixture(t) {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'bidit-npm-guard-'));
  t.after(() => fs.rmSync(temporary, { recursive: true, force: true }));
  const globalRoot = path.join(temporary, 'prefix/lib/node_modules');
  const owner = path.join(globalRoot, 'npm');
  const roots = Object.fromEntries(Object.keys(PIN.packages).map(name => [name, name === 'npm' ? owner : path.join(owner, 'node_modules', name)]));
  const pin = structuredClone(PIN);
  for (const [name, directory] of Object.entries(roots)) {
    fs.mkdirSync(directory, { recursive: true });
    const metadata = { name, version: pin.packages[name].version, license: 'MIT', main: 'index.js' };
    if (name === 'socks') metadata.dependencies = { 'ip-address': '^10.1.1' };
    if (name === 'socks-proxy-agent') metadata.dependencies = { socks: '^2.8.3' };
    if (name === 'node-gyp') metadata.dependencies = { undici: '^6.25.0' };
    if (name === 'ip-address') {
      metadata.main = 'dist/ip-address.js';
      metadata.types = 'dist/ip-address.d.ts';
      fs.mkdirSync(path.join(directory, 'dist'));
      fs.writeFileSync(path.join(directory, metadata.types), 'export {};\n');
    }
    fs.writeFileSync(path.join(directory, 'package.json'), JSON.stringify(metadata));
    fs.writeFileSync(path.join(directory, 'LICENSE'), `MIT notice for ${name}\n`);
    fs.writeFileSync(path.join(directory, metadata.main), 'module.exports = {};\n');
  }
  fs.mkdirSync(path.join(temporary, 'prefix/bin'), { recursive: true });
  fs.mkdirSync(path.join(owner, 'bin'));
  for (const name of ['npm', 'npx']) {
    const script = path.join(owner, 'bin', `${name}-cli.js`);
    fs.writeFileSync(script, `// ${name} fixture launcher\n`);
    pin.launchers[name] = hash(fs.readFileSync(script));
    fs.symlinkSync(script, path.join(temporary, 'prefix/bin', name));
  }
  function refresh(name) {
    Object.assign(pin.packages[name], inventory(roots[name], pin.packages[name].identityOnly), { licenseSha256: hash(fs.readFileSync(path.join(roots[name], 'LICENSE'))) });
    pin.parentTree = parentInventory(owner);
  }
  const globRoot = path.join(owner, 'node_modules/glob');
  for (const [name, relative, version, dependencies] of GLOB_CLOSURE) {
    const directory = path.join(owner, relative);
    fs.mkdirSync(directory, { recursive: true });
    const metadata = { name, version: version || '13.0.7', main: 'index.js', dependencies };
    if (name === 'glob') Object.assign(metadata, { type: 'module', main: './dist/commonjs/index.min.js', module: './dist/esm/index.min.js', types: './dist/commonjs/index.d.ts', exports: globExports(), engines: { node: '18 || 20 || >=22' }, license: 'BlueOak-1.0.0', publishConfig: { tag: 'v13-legacy' } });
    fs.writeFileSync(path.join(directory, 'package.json'), JSON.stringify(metadata));
    fs.writeFileSync(path.join(directory, 'index.js'), 'module.exports = {};\n');
  }
  for (const [relative, name, version, range] of GLOB_CONSUMERS) {
    const directory = path.join(owner, relative);
    fs.mkdirSync(directory, { recursive: true });
    const filename = path.join(directory, 'package.json');
    const metadata = fs.existsSync(filename) ? JSON.parse(fs.readFileSync(filename)) : { name, version, main: 'index.js' };
    metadata.dependencies = { ...metadata.dependencies, glob: range };
    fs.writeFileSync(filename, JSON.stringify(metadata));
  }
  for (const kind of ['commonjs', 'esm']) {
    fs.mkdirSync(path.join(globRoot, 'dist', kind), { recursive: true });
    fs.writeFileSync(path.join(globRoot, 'dist', kind, 'package.json'), JSON.stringify({ type: kind === 'commonjs' ? 'commonjs' : 'module' }));
    for (const name of ['index.js', 'index.min.js']) fs.writeFileSync(path.join(globRoot, 'dist', kind, name), kind === 'commonjs' ? 'module.exports = {};\n' : 'export {};\n');
    fs.writeFileSync(path.join(globRoot, 'dist', kind, 'index.d.ts'), 'export {};\n');
  }
  fs.writeFileSync(path.join(globRoot, 'LICENSE.md'), 'BlueOak fixture notice\n');
  fs.writeFileSync(path.join(roots['ip-address'], 'dist/ip-address.js'), `const { isIP } = require('node:net'); class Address4 { constructor(value) { this.value = value.split('/')[0]; if(isIP(this.value) !== 4) throw Error('invalid IPv4'); } correctForm() { return this.value; } } class Address6 { constructor(value) { this.value = value.split('/')[0]; if(isIP(this.value) !== 6) throw Error('invalid IPv6'); } correctForm() { return this.value; } to4() { return new Address4(this.value.split(':').pop()); } } module.exports={Address4,Address6};`);
  fs.writeFileSync(path.join(roots.socks, 'index.js'), 'module.exports = { SocksClient: { createConnection() {} } };');
  fs.writeFileSync(path.join(roots.undici, 'index.js'), 'module.exports = { Headers: globalThis.Headers, fetch: globalThis.fetch };');
  function refreshGlob() {
    Object.assign(pin.glob.current, inventory(globRoot));
    pin.glob.licenseSha256 = hash(fs.readFileSync(path.join(globRoot, 'LICENSE.md')));
    pin.parentTree = parentInventory(owner);
  }
  refreshGlob();
  for (const name of Object.keys(roots)) refresh(name);
  function change(name, update) {
    const filename = path.join(roots[name], 'package.json');
    const metadata = JSON.parse(fs.readFileSync(filename, 'utf8'));
    update(metadata);
    fs.writeFileSync(filename, JSON.stringify(metadata));
    refresh(name);
  }
  function refuse(pattern) {
    const ipManifest = path.join(roots['ip-address'], 'package.json');
    const before = fs.readFileSync(ipManifest);
    assert.throws(() => validateInstallation(globalRoot, pin), pattern);
    assert.deepEqual(fs.readFileSync(ipManifest), before, 'Validation must not change the old target');
    assert(!fs.existsSync(path.join(temporary, 'provenance')), 'Rejected input must not produce a receipt');
  }
  return { temporary, globalRoot, owner, roots, pin, refresh, refreshGlob, globRoot, change, refuse };
}

test('compatible owners, complete package bytes and actual consumer resolutions pass', t => {
  const f = fixture(t);
  const result = validateInstallation(f.globalRoot, f.pin);
  assert.equal(result.roots['ip-address'], path.join(f.owner, 'node_modules/ip-address'));
  assert.equal(result.versions.undici, '6.29.1');
  const receipt = retainProvenance(result, path.join(f.temporary, 'provenance'), { fixture: true }, f.pin);
  assert.equal(receipt.owner, 'npm@12.2.2');
  assert.deepEqual(fs.readFileSync(path.join(f.temporary, 'provenance/ip-address.LICENSE')), fs.readFileSync(path.join(f.roots['ip-address'], 'LICENSE')));
});

for (const name of ['npm', 'socks', 'ip-address', 'undici', 'node-gyp', 'socks-proxy-agent']) {
  test(`wrong ${name} owner version fails before a receipt`, t => {
    const f = fixture(t);
    f.change(name, metadata => { metadata.version = '0.0.0'; });
    f.refuse(/version/);
  });
}

test('renamed owner identity fails independently of its fixture checksum', t => {
  const f = fixture(t);
  f.change('npm', metadata => { metadata.name = 'unrelated-tool'; });
  f.refuse(/ownership/);
});

for (const [owner, dependency] of [['socks', 'ip-address'], ['node-gyp', 'undici'], ['socks-proxy-agent', 'socks']]) {
  test(`changed ${owner} dependency range fails independently of package hashes`, t => {
    const f = fixture(t);
    f.change(owner, metadata => { metadata.dependencies[dependency] = '*'; });
    f.refuse(/dependency range/);
  });
}

test('moved entry resolving outside the expected package is refused', t => {
  const f = fixture(t);
  const elsewhere = path.join(f.owner, 'node_modules/unexpected');
  fs.mkdirSync(elsewhere);
  fs.writeFileSync(path.join(elsewhere, 'index.js'), 'module.exports = {};');
  f.change('ip-address', metadata => { metadata.main = '../unexpected/index.js'; });
  f.refuse(/entry resolution/);
});

test('duplicate package is refused even when renamed on disk', t => {
  const f = fixture(t);
  fs.cpSync(f.roots['ip-address'], path.join(f.owner, 'node_modules/renamed-copy'), { recursive: true });
  f.refuse(/duplicate.*ownership/);
});

test('nested consumer override is refused before resolution can select it', t => {
  const f = fixture(t);
  fs.mkdirSync(path.join(f.roots.socks, 'node_modules'));
  fs.cpSync(f.roots['ip-address'], path.join(f.roots.socks, 'node_modules/ip-address'), { recursive: true });
  f.refuse(/duplicate.*ownership|Changed socks package bytes|Unexpected ip-address package directory/);
});

test('unexpected package directory is refused even without its own manifest', t => {
  const f = fixture(t);
  fs.mkdirSync(path.join(f.owner, 'node_modules/other/node_modules/ip-address'), { recursive: true });
  f.refuse(/Unexpected ip-address package directory/);
});

for (const escape of [false, true]) {
  test(`${escape ? 'escaping' : 'contained'} package directory symlink is refused`, t => {
    const f = fixture(t);
    const target = path.join(escape ? f.temporary : f.owner, 'relocated-ip-address');
    fs.renameSync(f.roots['ip-address'], target);
    fs.symlinkSync(target, f.roots['ip-address'], 'dir');
    f.refuse(/symlink/);
  });
}

test('global root symlink is refused', t => {
  const f = fixture(t);
  const alias = path.join(f.temporary, 'alias');
  fs.symlinkSync(f.globalRoot, alias, 'dir');
  assert.throws(() => validateInstallation(alias, f.pin), /symlink/);
});

test('global launcher pointing to another owner is refused', t => {
  const f = fixture(t);
  const launcher = path.join(f.temporary, 'prefix/bin/npm');
  const outside = path.join(f.temporary, 'another-npm-cli.js');
  fs.copyFileSync(path.join(f.owner, 'bin/npm-cli.js'), outside);
  fs.unlinkSync(launcher);
  fs.symlinkSync(outside, launcher);
  f.refuse(/launcher owner/);
});

test('changed launcher bytes are refused before consumer probes', t => {
  const f = fixture(t);
  fs.appendFileSync(path.join(f.owner, 'bin/npm-cli.js'), '// changed');
  f.refuse(/launcher bytes/);
});

test('symlinked package file is refused even if it retains the same bytes', t => {
  const f = fixture(t);
  const filename = path.join(f.roots['ip-address'], 'LICENSE');
  fs.renameSync(filename, path.join(f.temporary, 'notice'));
  fs.symlinkSync(path.join(f.temporary, 'notice'), filename);
  f.refuse(/symlink/);
});

test('generated command links are allowed only inside .bin and within npm', t => {
  const f = fixture(t);
  const directory = path.join(f.owner, 'node_modules/.bin');
  fs.mkdirSync(directory);
  const link = path.join(directory, 'socks');
  fs.symlinkSync(path.join(f.roots.socks, 'index.js'), link);
  validateInstallation(f.globalRoot, f.pin);
  fs.unlinkSync(link);
  const outside = path.join(f.temporary, 'outside.js');
  fs.writeFileSync(outside, 'module.exports = {};');
  fs.symlinkSync(outside, link);
  f.refuse(/symlink/);
});

for (const mutation of ['tamper', 'extra', 'missing-license']) {
  test(`${mutation} in complete protected package is refused`, t => {
    const f = fixture(t);
    if (mutation === 'tamper') fs.appendFileSync(path.join(f.roots['ip-address'], 'dist/ip-address.js'), '// changed');
    if (mutation === 'extra') fs.writeFileSync(path.join(f.roots['ip-address'], 'unexpected.js'), '// extra');
    if (mutation === 'missing-license') fs.unlinkSync(path.join(f.roots['ip-address'], 'LICENSE'));
    f.refuse(/package bytes|ENOENT/);
  });
}

test('new runtime dependency is refused even with a matching fixture manifest', t => {
  const f = fixture(t);
  f.change('ip-address', metadata => { metadata.optionalDependencies = { surprise: '^1.0.0' }; });
  f.refuse(/optionalDependencies/);
});

test('new exports map is refused even with a matching fixture manifest', t => {
  const f = fixture(t);
  f.change('ip-address', metadata => { metadata.exports = { '.': './dist/ip-address.js', './package.json': './package.json' }; });
  f.refuse(/exports map/);
});

test('archive validation checks SHA256 and SRI over the actual regular file', t => {
  const f = fixture(t);
  const filename = path.join(f.temporary, 'parent.tgz');
  const bytes = Buffer.from('bounded archive integrity fixture');
  fs.writeFileSync(filename, bytes);
  const pin = { sha256: hash(bytes), integrity: `sha512-${crypto.createHash('sha512').update(bytes).digest('base64')}` };
  assert.equal(verifyArchive(filename, pin), pin.sha256);
  assert.throws(() => verifyArchive(filename, { ...pin, integrity: 'sha512-wrong' }), /SRI/);
  fs.appendFileSync(filename, 'tamper');
  assert.throws(() => verifyArchive(filename, pin), /SHA256/);
  const alias = path.join(f.temporary, 'archive-link');
  fs.symlinkSync(filename, alias);
  assert.throws(() => verifyArchive(alias, pin), /symlink/);
});

test('dangling provenance links are refused without overwriting another path', t => {
  const f = fixture(t);
  const result = validateInstallation(f.globalRoot, f.pin);
  const destination = path.join(f.temporary, 'provenance');
  fs.mkdirSync(destination);
  const outside = path.join(f.temporary, 'must-not-create');
  fs.symlinkSync(outside, path.join(destination, 'ip-address.LICENSE'));
  assert.throws(() => retainProvenance(result, destination, {}, f.pin), /symlink/);
  assert(!fs.existsSync(outside));
  assert(!fs.existsSync(path.join(destination, 'npm-parent.json')));
});

// Finite regular-file ustar fixtures exercise the separate archive boundary.
function tarFixture(entries, overrides = {}) {
  const blocks = [];
  for (const [name, content] of entries) {
    const bytes = Buffer.from(content);
    const header = Buffer.alloc(512);
    header.write(name);
    header.write(`${bytes.length.toString(8).padStart(11, '0')}\0`, 124, 'binary');
    header.fill(32, 148, 156);
    header[156] = overrides.type || 48;
    header.write('ustar\x0000', 257, 'binary');
    if (overrides.prefix) header.write(overrides.prefix, 345);
    const checksum = header.reduce((sum, byte) => sum + byte, 0);
    header.write(`${checksum.toString(8).padStart(6, '0')}\0 `, 148, 'binary');
    blocks.push(header, bytes, Buffer.alloc((512 - bytes.length % 512) % 512));
  }
  return Buffer.concat([...blocks, Buffer.alloc(1024)]);
}
function archiveFixture(f, entries, overrides = {}) {
  const tar = tarFixture(entries, overrides);
  const archive = gzipSync(tar);
  const filename = path.join(f.temporary, `glob-${crypto.randomUUID()}.tgz`);
  fs.writeFileSync(filename, archive);
  const triples = entries.map(([name, bytes]) => [name.slice(8), Buffer.byteLength(bytes), hash(bytes)]).sort((a, b) => a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0);
  Object.assign(f.pin.glob, { archiveBytes: archive.length, tarBytes: tar.length, sha256: hash(archive), integrity: `sha512-${crypto.createHash('sha512').update(archive).digest('base64')}` });
  Object.assign(f.pin.glob.current, { files: entries.length, bytes: triples.reduce((sum, [, size]) => sum + size, 0), manifestSha256: hash(JSON.stringify(triples)) });
  return filename;
}
function leafEntries(directory) {
  const entries = [];
  function walk(current) {
    for (const name of fs.readdirSync(current).sort()) {
      const filename = path.join(current, name);
      if (fs.statSync(filename).isDirectory()) walk(filename);
      else entries.push([`package/${path.relative(directory, filename).split(path.sep).join('/')}`, fs.readFileSync(filename)]);
    }
  }
  walk(directory);
  return entries;
}
function stockFixture(f) {
  const candidate = leafEntries(f.globRoot);
  const filename = path.join(f.globRoot, 'package.json');
  const metadata = JSON.parse(fs.readFileSync(filename));
  metadata.version = '13.0.6';
  delete metadata.publishConfig;
  fs.writeFileSync(filename, JSON.stringify(metadata));
  Object.assign(f.pin.glob.stock, inventory(f.globRoot));
  return archiveFixture(f, candidate);
}

test('official leaf overlay changes only glob and records distinct parent/leaf provenance', t => {
  const f = fixture(t);
  const before = parentInventory(f.owner);
  const archive = stockFixture(f);
  assert.throws(() => validateInstallation(f.globalRoot, f.pin), /glob version/);
  const result = overlayGlob(f.globalRoot, archive, f.pin);
  assert.equal(result.versions.glob, '13.0.7');
  assert.deepEqual(parentInventory(f.owner), before);
  assert(!fs.readdirSync(f.globalRoot).some(name => name.startsWith('.bidit-glob-')));
  assert(!fs.existsSync(path.join(f.temporary, 'provenance')));
  const receipt = retainProvenance(result, path.join(f.temporary, 'provenance'), {}, f.pin);
  assert.equal(receipt.leafOverlay.current, '13.0.7');
  assert.match(receipt.variant, /parent.*leaf overlay/);
  assert(receipt.unresolved.some(value => value.includes('metadata still includes')));
  assert.deepEqual(fs.readFileSync(path.join(f.temporary, 'provenance/glob.LICENSE')), fs.readFileSync(path.join(f.globRoot, 'LICENSE.md')));
  assert.throws(() => overlayGlob(f.globalRoot, archive, f.pin), /glob version/, 'Overlay is a stock-to-patched transition, not a permissive repeat mutation');
});

for (const version of ['13.0.6', '14.0.2', '0.0.0']) {
  test(`final installation refuses glob ${version} independently of refreshed leaf checksum`, t => {
    const f = fixture(t);
    const filename = path.join(f.globRoot, 'package.json');
    const metadata = JSON.parse(fs.readFileSync(filename));
    metadata.version = version;
    fs.writeFileSync(filename, JSON.stringify(metadata));
    f.refreshGlob();
    f.refuse(/glob version/);
  });
}
for (const field of ['exports', 'engines', 'dependencies', 'optionalDependencies', 'license', 'main', 'publishConfig']) {
  test(`changed glob ${field} fails independently of refreshed leaf checksum`, t => {
    const f = fixture(t);
    const filename = path.join(f.globRoot, 'package.json');
    const metadata = JSON.parse(fs.readFileSync(filename));
    metadata[field] = field === 'license' || field === 'main' ? 'unexpected' : { unexpected: '*' };
    fs.writeFileSync(filename, JSON.stringify(metadata));
    f.refreshGlob();
    f.refuse(/glob/);
  });
}
for (const relative of ['dist/commonjs/index.min.js', 'dist/esm/index.min.js', 'dist/commonjs/index.js', 'dist/esm/index.js', 'LICENSE.md']) {
  test(`modified actual glob ${relative} is refused`, t => {
    const f = fixture(t);
    fs.appendFileSync(path.join(f.globRoot, relative), '// tamper');
    f.refuse(/glob package bytes/);
  });
}
for (const kind of ['duplicate', 'empty-directory', 'nested-shadow', 'link']) {
  test(`unexpected glob ownership ${kind} is refused`, t => {
    const f = fixture(t);
    if (kind === 'link') {
      const moved = path.join(f.owner, 'moved-glob');
      fs.renameSync(f.globRoot, moved);
      fs.symlinkSync(moved, f.globRoot);
    } else {
      const destination = path.join(f.owner, kind === 'nested-shadow' ? 'node_modules/cacache/node_modules/glob' : kind === 'empty-directory' ? 'node_modules/other/node_modules/glob' : 'node_modules/renamed');
      if (kind === 'empty-directory') fs.mkdirSync(destination, { recursive: true });
      else fs.cpSync(f.globRoot, destination, { recursive: true });
    }
    f.refuse(/glob|symlink/);
  });
}
test('the exact two minipass owners are accepted and an additional renamed copy is refused', t => {
  const f = fixture(t);
  validateInstallation(f.globalRoot, f.pin);
  fs.cpSync(path.join(f.owner, 'node_modules/minipass'), path.join(f.owner, 'node_modules/minipass-extra'), { recursive: true });
  f.pin.parentTree = parentInventory(f.owner);
  f.refuse(/duplicate glob closure ownership/);
});
for (const [relative, name] of GLOB_CONSUMERS) {
  test(`changed ${name} glob range is refused independently of parent checksum`, t => {
    const f = fixture(t);
    const filename = path.join(f.owner, relative, 'package.json');
    const metadata = JSON.parse(fs.readFileSync(filename));
    metadata.dependencies.glob = '*';
    fs.writeFileSync(filename, JSON.stringify(metadata));
    if (name === 'npm') f.refresh('npm');
    f.pin.parentTree = parentInventory(f.owner);
    f.refuse(/glob dependency range/);
  });
}
test('a changed unrelated parent file is refused before overlay and leaves outside sentinels untouched', t => {
  const f = fixture(t);
  const archive = stockFixture(f);
  const sentinel = path.join(f.temporary, 'outside');
  fs.writeFileSync(sentinel, 'unchanged');
  fs.writeFileSync(path.join(f.owner, 'unrelated-new-file'), 'tamper');
  const before = fs.readFileSync(path.join(f.globRoot, 'package.json'));
  assert.throws(() => overlayGlob(f.globalRoot, archive, f.pin), /non-glob parent bytes/);
  assert.deepEqual(fs.readFileSync(path.join(f.globRoot, 'package.json')), before);
  assert.equal(fs.readFileSync(sentinel, 'utf8'), 'unchanged');
  assert(!fs.readdirSync(f.globalRoot).some(name => name.startsWith('.bidit-glob-')));
});
for (const name of ['package/../escape', '/absolute', 'package//empty', 'package/./dot', 'package/back\\slash']) {
  test(`glob archive refuses unsafe name ${name}`, t => {
    const f = fixture(t);
    const archive = archiveFixture(f, [[name, 'bounded']]);
    assert.throws(() => readGlobArchive(archive, f.pin), /Unsafe glob archive path/);
    assert(!fs.existsSync(path.join(f.temporary, 'escape')));
  });
}
for (const type of ['1', '2', '5', 'x', 'g']) {
  test(`glob archive refuses non-regular type ${type}`, t => {
    const f = fixture(t);
    const archive = archiveFixture(f, [['package/index.js', 'bounded']], { type: type.charCodeAt(0) });
    assert.throws(() => readGlobArchive(archive, f.pin), /regular files only/);
  });
}
test('glob archive refuses duplicate names before extraction', t => {
  const f = fixture(t);
  const archive = archiveFixture(f, [['package/index.js', 'one'], ['package/index.js', 'two']]);
  assert.throws(() => readGlobArchive(archive, f.pin), /Duplicate glob archive path/);
});
test('glob archive verifies independent SHA and SRI before decompressing', t => {
  const f = fixture(t);
  const archive = archiveFixture(f, [['package/index.js', 'bounded']]);
  const original = fs.readFileSync(archive);
  assert.throws(() => readGlobArchive(archive, { ...f.pin, glob: { ...f.pin.glob, integrity: 'sha512-wrong' } }), /SRI/);
  fs.writeFileSync(archive, Buffer.alloc(original.length));
  assert.throws(() => readGlobArchive(archive, f.pin), /SHA256/);
  fs.writeFileSync(archive, original);
  assert.equal(readGlobArchive(archive, f.pin).length, 1);
});

test('oversized compressed glob input is refused before reading its contents', t => {
  const f = fixture(t);
  const archive = archiveFixture(f, [['package/index.js', 'bounded']]);
  fs.appendFileSync(archive, 'extra');
  const read = fs.readSync;
  let reads = 0;
  fs.readSync = (...args) => { reads++; return read(...args); };
  try { assert.throws(() => readGlobArchive(archive, f.pin), /archive length/); }
  finally { fs.readSync = read; }
  assert.equal(reads, 0);
});
for (const failure of ['restore-rename', 'remove-candidate']) {
  test(`failed ${failure} rollback retains the original tree for recovery`, t => {
    const f = fixture(t);
    if (failure === 'remove-candidate') {
      fs.writeFileSync(path.join(f.roots['ip-address'], 'dist/ip-address.js'), 'module.exports = {};');
      f.refresh('ip-address');
    }
    const archive = stockFixture(f);
    const rename = fs.renameSync;
    const remove = fs.rmSync;
    fs.renameSync = (source, destination) => {
      if (failure === 'restore-rename' && (source.endsWith('/candidate') || source.endsWith('/previous'))) throw Error('injected rename failure');
      return rename(source, destination);
    };
    fs.rmSync = (target, options) => {
      if (failure === 'remove-candidate' && target === f.globRoot) throw Error('injected removal failure');
      return remove(target, options);
    };
    try { assert.throws(() => overlayGlob(f.globalRoot, archive, f.pin), /original tree retained at/); }
    finally { fs.renameSync = rename; fs.rmSync = remove; }
    const stages = fs.readdirSync(f.globalRoot).filter(name => name.startsWith('.bidit-glob-'));
    assert.equal(stages.length, 1);
    const old = JSON.parse(fs.readFileSync(path.join(f.globalRoot, stages[0], 'previous/package.json')));
    assert.equal(old.version, '13.0.6');
    assert(!fs.existsSync(path.join(f.temporary, 'provenance')));
  });
}
