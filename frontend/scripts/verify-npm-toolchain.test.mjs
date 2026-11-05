import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { PIN, inventory, retainProvenance, validateInstallation, verifyArchive } from './verify-npm-toolchain.mjs';

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
  }
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
  return { temporary, globalRoot, owner, roots, pin, refresh, change, refuse };
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
