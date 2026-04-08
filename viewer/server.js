const express = require('express');
const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const app = express();
const PORT = process.env.PORT || 3300;
const BASE = process.env.BASE_PATH || '/lagent-tablets';

// Default repo path
const REPO_PATH = process.env.REPO_PATH || '/home/leanagent/math/connectivity_gnp_tablets';
const STATE_DIR = path.join(REPO_PATH, '.agent-supervisor');

// Also write static files for nginx serving
const STATIC_OUT = process.env.STATIC_OUT || '/home/leanagent/lagent-tablets-web';

function git(args) {
  return execSync(`git ${args}`, { cwd: REPO_PATH, encoding: 'utf-8', timeout: 10000 }).trim();
}

function getCyclesFromGit() {
  let tags;
  try {
    tags = git('tag -l "cycle-*" --sort=version:refname').split('\n').filter(t => t);
  } catch { return []; }

  return tags.map(tag => {
    const cycle = parseInt(tag.replace('cycle-', ''), 10);
    let hash = '', timestamp = '', subject = '';
    try {
      const log = git(`log -1 --format=%H%n%aI%n%s ${tag}`);
      const parts = log.split('\n');
      hash = parts[0] || '';
      timestamp = parts[1] || '';
      subject = parts[2] || '';
    } catch {}

    // Read cycle_meta.json from that commit
    let meta = {};
    try {
      const raw = git(`show ${tag}:.agent-supervisor/cycle_meta.json`);
      meta = JSON.parse(raw);
    } catch {}

    return { cycle, hash, timestamp, message: subject, ...meta };
  });
}

function getCycleDiff(cycle) {
  const tag = `cycle-${cycle}`;
  const prevTag = `cycle-${cycle - 1}`;
  try {
    // Check if previous tag exists
    git(`rev-parse ${prevTag}`);
    return git(`diff ${prevTag} ${tag} -- Tablet/`);
  } catch {
    try {
      // First cycle — diff against empty tree
      return git(`diff 4b825dc642cb6eb9a060e54bf899d15f3bc9 ${tag} -- Tablet/`);
    } catch { return ''; }
  }
}

function writeStatic() {
  try {
    fs.mkdirSync(path.join(STATIC_OUT, 'api'), { recursive: true });

    // State
    const state = JSON.parse(fs.readFileSync(path.join(STATE_DIR, 'state.json'), 'utf-8'));
    const tablet = JSON.parse(fs.readFileSync(path.join(STATE_DIR, 'tablet.json'), 'utf-8'));
    fs.writeFileSync(path.join(STATIC_OUT, 'api', 'state.json'), JSON.stringify({ state, tablet }));

    // Cycles from git
    const cycles = getCyclesFromGit();
    fs.writeFileSync(path.join(STATIC_OUT, 'api', 'cycles.json'), JSON.stringify(cycles));

    // Nodes (with verification status)
    const nodes = buildNodes(tablet);
    const verif = getVerificationStatus(tablet);
    for (const [name, vs] of Object.entries(verif)) {
      if (nodes[name]) nodes[name].verification = vs;
    }
    fs.writeFileSync(path.join(STATIC_OUT, 'api', 'nodes.json'), JSON.stringify(nodes));

    // Copy index.html
    const htmlSrc = path.join(__dirname, 'public', 'index.html');
    if (fs.existsSync(htmlSrc)) {
      fs.copyFileSync(htmlSrc, path.join(STATIC_OUT, 'index.html'));
    }

  } catch (e) {
    console.error('Static write error:', e.message);
  }
}

function nodeContentHash(name) {
  // SHA-256 of .lean + .tex, matching _node_content_hash in cycle.py
  const crypto = require('crypto');
  const h = crypto.createHash('sha256');
  const tabletDir = path.join(REPO_PATH, 'Tablet');
  for (const ext of ['.lean', '.tex']) {
    const p = path.join(tabletDir, name + ext);
    try { h.update(fs.readFileSync(p)); } catch {}
  }
  return h.digest('hex').substring(0, 16);
}

function getVerificationStatus(tablet) {
  // Read per-node verification status from tablet.json.
  // Status is sticky UNLESS the node's content has changed since verification.
  const status = {};
  for (const [name, node] of Object.entries(tablet.nodes || {})) {
    if (name === 'Preamble') continue;
    const savedHash = node.verification_content_hash || '';
    const currentHash = savedHash ? nodeContentHash(name) : '';
    const contentChanged = savedHash && currentHash !== savedHash;

    const cs = contentChanged ? '?' : (node.correspondence_status || '?');
    const ss = contentChanged ? '?' : (node.soundness_status || '?');
    if (cs !== '?' || ss !== '?') {
      status[name] = { correspondence: cs, nl_proof: ss };
    }
  }
  return status;
}

function buildNodes(tablet) {
  const tabletDir = path.join(REPO_PATH, 'Tablet');
  const nodes = {};

  // Add Preamble as a visible node
  {
    const preamblePath = path.join(tabletDir, 'Preamble.lean');
    let preambleContent = '';
    try { preambleContent = fs.readFileSync(preamblePath, 'utf-8'); } catch {}
    if (preambleContent) {
      // Extract definitions from preamble
      const defs = [];
      for (const line of preambleContent.split('\n')) {
        if (line.match(/^(noncomputable\s+)?def\s/)) {
          defs.push(line.trim());
        }
      }
      nodes['Preamble'] = {
        kind: 'preamble',
        status: 'closed',
        title: 'Definitions',
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

    const leanPath = path.join(tabletDir, `${name}.lean`);
    const texPath = path.join(tabletDir, `${name}.tex`);

    let leanContent = '', texContent = '', imports = [], title = '';
    try { leanContent = fs.readFileSync(leanPath, 'utf-8'); } catch {}
    try { texContent = fs.readFileSync(texPath, 'utf-8'); } catch {}

    // Extract imports
    const importRe = /import\s+Tablet\.(\w+)/g;
    let m;
    while ((m = importRe.exec(leanContent)) !== null) {
      imports.push(m[1]);
    }

    // Extract title from tex
    const titleMatch = texContent.match(/\\begin\{(?:theorem|lemma|definition|proposition)\}\[(.*?)\]/);
    if (titleMatch) title = titleMatch[1];

    // Extract declaration (everything from theorem/lemma/def up to := sorry or := by)
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

    // Check sorry
    const hasSorry = /\bsorry\b/.test(leanContent.replace(/--.*$/gm, ''));

    nodes[name] = {
      ...meta,
      title,
      imports,
      declaration,
      hasSorry,
      leanContent,
      texContent,
    };
  }
  return nodes;
}

// Serve static files
app.use(BASE, express.static(path.join(__dirname, 'public')));

// API endpoints (also at base path)
app.get(`${BASE}/api/state.json`, (req, res) => {
  try {
    const state = JSON.parse(fs.readFileSync(path.join(STATE_DIR, 'state.json'), 'utf-8'));
    const tablet = JSON.parse(fs.readFileSync(path.join(STATE_DIR, 'tablet.json'), 'utf-8'));
    res.json({ state, tablet });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.get(`${BASE}/api/cycles.json`, (req, res) => {
  try {
    res.json(getCyclesFromGit());
  } catch { res.json([]); }
});

app.get(`${BASE}/api/nodes.json`, (req, res) => {
  try {
    const tablet = JSON.parse(fs.readFileSync(path.join(STATE_DIR, 'tablet.json'), 'utf-8'));
    const nodes = buildNodes(tablet);
    const verif = getVerificationStatus(tablet);
    for (const [name, vs] of Object.entries(verif)) {
      if (nodes[name]) nodes[name].verification = vs;
    }
    res.json(nodes);
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.get(`${BASE}/api/diff/:cycle`, (req, res) => {
  const cycle = parseInt(req.params.cycle, 10);
  if (isNaN(cycle)) return res.status(400).send('Invalid cycle');
  const diff = getCycleDiff(cycle);
  res.type('text/plain').send(diff);
});

// API: download tablet snapshot as zip
app.get(`${BASE}/api/download-tablet`, (req, res) => {
  try {
    const tablet = JSON.parse(fs.readFileSync(path.join(STATE_DIR, 'tablet.json'), 'utf-8'));
    const state = JSON.parse(fs.readFileSync(path.join(STATE_DIR, 'state.json'), 'utf-8'));
    const tabletDir = path.join(REPO_PATH, 'Tablet');
    const paperDir = path.join(REPO_PATH, 'paper');

    // Build README
    const nodeNames = Object.keys(tablet.nodes || {}).filter(n => n !== 'Preamble');
    const closedCount = nodeNames.filter(n => (tablet.nodes[n] || {}).status === 'closed').length;
    const nodeList = nodeNames.map(n => {
      const nd = tablet.nodes[n];
      return `  - ${n}: ${nd.status} (${nd.kind})${nd.difficulty ? ', ' + nd.difficulty : ''}${nd.title ? ' -- ' + nd.title : ''}`;
    }).join('\n');

    const readme = `# Proof Tablet Snapshot

This is a snapshot of a formalization-in-progress produced by the lagent-tablets supervisor.

## What is this?

Each .lean file in Tablet/ contains a Lean 4 theorem/lemma/definition with its formal statement.
Each .tex file contains the corresponding natural-language statement and proof.
Together they form a DAG (directed acyclic graph) of mathematical results that decompose the
source paper into individually provable nodes.

## Status

- Phase: ${state.phase || '?'}
- Cycle: ${state.cycle || '?'}
- Nodes: ${closedCount}/${nodeNames.length} closed
- Generated: ${new Date().toISOString()}

## Nodes

${nodeList}

## Structure

- \`Tablet/Preamble.lean\` — shared imports (no definitions here)
- \`Tablet/<name>.lean\` — Lean 4 declaration (theorem/lemma/def)
- \`Tablet/<name>.tex\` — natural-language statement + proof
- \`paper/\` — source paper being formalized
- \`tablet.json\` — machine-readable tablet state (node metadata, DAG structure)

## How to use

1. Open any .lean file to see the formal statement
2. Read the matching .tex file for the mathematical context
3. Nodes import each other via \`import Tablet.<name>\` — this defines the proof DAG
4. A node with \`sorry\` still needs its proof completed
5. Run \`lake build Tablet\` to check compilation (requires Lean 4 + mathlib)
`;

    // Create temp dir and assemble the zip contents
    const tmpDir = fs.mkdtempSync('/tmp/tablet-snapshot-');
    const snapDir = path.join(tmpDir, 'tablet-snapshot');
    fs.mkdirSync(path.join(snapDir, 'Tablet'), { recursive: true });
    fs.mkdirSync(path.join(snapDir, 'paper'), { recursive: true });

    // Copy Tablet/ files
    if (fs.existsSync(tabletDir)) {
      for (const f of fs.readdirSync(tabletDir)) {
        if (f.endsWith('.lean') || f.endsWith('.tex')) {
          fs.copyFileSync(path.join(tabletDir, f), path.join(snapDir, 'Tablet', f));
        }
      }
    }

    // Copy paper/ files
    if (fs.existsSync(paperDir)) {
      for (const f of fs.readdirSync(paperDir)) {
        fs.copyFileSync(path.join(paperDir, f), path.join(snapDir, 'paper', f));
      }
    }

    // Write README and tablet.json
    fs.writeFileSync(path.join(snapDir, 'README.md'), readme);
    fs.writeFileSync(path.join(snapDir, 'tablet.json'), JSON.stringify(tablet, null, 2));

    // Create zip
    const zipPath = path.join(tmpDir, 'tablet-snapshot.zip');
    execSync(`cd "${tmpDir}" && zip -r "${zipPath}" tablet-snapshot/`, { timeout: 10000 });

    // Send zip
    res.setHeader('Content-Type', 'application/zip');
    res.setHeader('Content-Disposition', `attachment; filename="tablet-snapshot-cycle${state.cycle || 0}.zip"`);
    const zipStream = fs.createReadStream(zipPath);
    zipStream.pipe(res);
    zipStream.on('end', () => {
      // Cleanup temp dir
      execSync(`rm -rf "${tmpDir}"`);
    });

  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Write static files on startup and every 30s
writeStatic();
setInterval(writeStatic, 30000);

// API: submit human feedback
app.use(express.json());

app.post(`${BASE}/api/feedback`, (req, res) => {
  try {
    const { action, feedback } = req.body;
    const statePath = path.join(STATE_DIR, 'state.json');
    const state = JSON.parse(fs.readFileSync(statePath, 'utf-8'));

    if (action === 'approve') {
      // Write a signal file that the supervisor reads
      const signalPath = path.join(STATE_DIR, 'human_approve.json');
      fs.writeFileSync(signalPath, JSON.stringify({
        action: 'approve',
        timestamp: new Date().toISOString(),
      }));
      res.json({ ok: true, message: 'Approval signal written. Supervisor will advance phase.' });

    } else if (action === 'feedback') {
      // Write human feedback that gets injected into the next worker prompt
      const feedbackPath = path.join(REPO_PATH, 'HUMAN_INPUT.md');
      fs.writeFileSync(feedbackPath, feedback || '');
      // Also write a signal to resume
      const signalPath = path.join(STATE_DIR, 'human_feedback.json');
      fs.writeFileSync(signalPath, JSON.stringify({
        action: 'feedback',
        feedback: feedback || '',
        timestamp: new Date().toISOString(),
      }));
      // Remove pause file if present to allow supervisor to continue
      const pausePath = path.join(STATE_DIR, 'pause');
      try { fs.unlinkSync(pausePath); } catch {}
      res.json({ ok: true, message: 'Feedback written. Supervisor will run another cycle.' });

    } else {
      res.status(400).json({ error: 'action must be "approve" or "feedback"' });
    }
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// API: get current human feedback status
app.get(`${BASE}/api/feedback`, (req, res) => {
  try {
    const state = JSON.parse(fs.readFileSync(path.join(STATE_DIR, 'state.json'), 'utf-8'));
    const awaiting = state.awaiting_human_input || false;
    const phase = state.phase || '';
    const lastReview = state.last_review || {};
    const humanInput = '';
    try { humanInput = fs.readFileSync(path.join(REPO_PATH, 'HUMAN_INPUT.md'), 'utf-8'); } catch {}

    res.json({
      awaiting_input: awaiting,
      phase,
      last_review_decision: lastReview.decision || '',
      last_review_reason: lastReview.reason || '',
      human_input: humanInput,
    });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.listen(PORT, () => {
  console.log(`Tablet viewer at http://localhost:${PORT}${BASE}`);
  console.log(`Static output: ${STATIC_OUT}`);
  console.log(`Repo: ${REPO_PATH}`);
});
