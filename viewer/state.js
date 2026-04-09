const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { execFileSync } = require('child_process');

function createFsSnapshot(repoPath) {
  const cache = new Map();

  function read(relPath, encoding) {
    const key = `${encoding || 'buffer'}:${relPath}`;
    if (cache.has(key)) return cache.get(key);
    try {
      const value = fs.readFileSync(path.join(repoPath, relPath), encoding);
      cache.set(key, value);
      return value;
    } catch {
      const empty = encoding ? '' : Buffer.alloc(0);
      cache.set(key, empty);
      return empty;
    }
  }

  return {
    readText(relPath) {
      return read(relPath, 'utf-8');
    },
    readBuffer(relPath) {
      return read(relPath, null);
    },
  };
}

function createGitSnapshot(repoPath, tag) {
  const cache = new Map();

  function read(relPath) {
    if (cache.has(relPath)) return cache.get(relPath);
    try {
      const value = execFileSync('git', ['show', `${tag}:${relPath}`], {
        cwd: repoPath,
        encoding: 'buffer',
        timeout: 10000,
      });
      cache.set(relPath, value);
      return value;
    } catch {
      const empty = Buffer.alloc(0);
      cache.set(relPath, empty);
      return empty;
    }
  }

  return {
    readText(relPath) {
      return read(relPath).toString('utf8');
    },
    readBuffer(relPath) {
      return read(relPath);
    },
  };
}

function nodeContentHash(name, snapshot) {
  const h = crypto.createHash('sha256');
  for (const ext of ['.lean', '.tex']) {
    const content = snapshot.readBuffer(`Tablet/${name}${ext}`);
    if (content.length) h.update(content);
  }
  return h.digest('hex').substring(0, 16);
}

function getVerificationStatus(tablet, snapshot, options = {}) {
  const invalidateOnContentChange = options.invalidateOnContentChange !== false;
  const status = {};
  for (const [name, node] of Object.entries(tablet.nodes || {})) {
    if (name === 'Preamble') continue;
    const savedHash = node.verification_content_hash || '';
    const currentHash = invalidateOnContentChange && savedHash ? nodeContentHash(name, snapshot) : '';
    const contentChanged = invalidateOnContentChange && savedHash && currentHash !== savedHash;

    const cs = contentChanged ? '?' : (node.correspondence_status || '?');
    let ss = contentChanged ? '?' : (node.soundness_status || '?');
    if (node.status === 'closed') ss = 'pass';
    status[name] = { correspondence: cs, nl_proof: ss };
  }
  return status;
}

function buildNodes(tablet, snapshot) {
  const nodes = {};

  {
    const preambleContent = snapshot.readText('Tablet/Preamble.lean');
    if (preambleContent) {
      const defs = [];
      for (const line of preambleContent.split('\n')) {
        if (line.match(/^(noncomputable\s+)?def\s/)) {
          defs.push(line.trim());
        }
      }
      nodes.Preamble = {
        kind: 'preamble',
        status: 'closed',
        title: 'Preamble',
        imports: [],
        declaration: defs.join('\n'),
        hasSorry: /\bsorry\b/.test(preambleContent.replace(/--.*$/gm, '')),
        leanContent: preambleContent,
        texContent: '',
      };
    }
  }

  for (const [name, meta] of Object.entries(tablet.nodes || {})) {
    if (meta.kind === 'preamble') continue;

    const leanContent = snapshot.readText(`Tablet/${name}.lean`);
    const texContent = snapshot.readText(`Tablet/${name}.tex`);
    const imports = [];

    const importRe = /import\s+Tablet\.(\w+)/g;
    let m;
    while ((m = importRe.exec(leanContent)) !== null) {
      imports.push(m[1]);
    }

    let title = '';
    let texEnv = '';
    const envMatch = texContent.match(/\\begin\{(theorem|lemma|definition|proposition|corollary)\}(?:\[(.*?)\])?/);
    if (envMatch) {
      texEnv = envMatch[1];
      title = envMatch[2] || '';
    }

    let declaration = '';
    const lines = leanContent.split('\n');
    let inDecl = false;
    for (const line of lines) {
      if (!inDecl && line.match(/^(theorem|lemma|def|noncomputable\s+def)\s/)) {
        inDecl = true;
      }
      if (inDecl) {
        declaration += (declaration ? '\n' : '') + line;
        if (line.includes(':= sorry') || line.includes(':= by') || line.trimEnd().endsWith(':=')) {
          break;
        }
      }
    }

    const hasSorry = /\bsorry\b/.test(leanContent.replace(/--.*$/gm, ''));

    nodes[name] = {
      ...meta,
      title,
      texEnv,
      imports,
      declaration,
      hasSorry,
      leanContent,
      texContent,
    };
  }
  return nodes;
}

function buildVerifiedNodes(tablet, snapshot, options = {}) {
  const nodes = buildNodes(tablet, snapshot);
  const verif = getVerificationStatus(tablet, snapshot, options);
  for (const [name, vs] of Object.entries(verif)) {
    if (nodes[name]) nodes[name].verification = vs;
  }
  return nodes;
}

module.exports = {
  buildNodes,
  buildVerifiedNodes,
  createFsSnapshot,
  createGitSnapshot,
  getVerificationStatus,
  nodeContentHash,
};
