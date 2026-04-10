const express = require('express');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { execSync } = require('child_process');

const app = express();
const PORT = process.env.PORT || 3300;
const BASE = process.env.BASE_PATH || '/lagent-tablets';
const STATIC_OUT = process.env.STATIC_OUT || '/home/leanagent/lagent-tablets-web';
const PROJECTS_FILE = process.env.VIEWER_PROJECTS_FILE || path.join(__dirname, 'projects.json');
const LEGACY_REPO_PATH = process.env.REPO_PATH || '/home/leanagent/math/connectivity_gnp_tablets';
const LEGACY_PROJECT_SLUG = process.env.PROJECT_SLUG || path.basename(LEGACY_REPO_PATH).replace(/_tablets?$/, '');

function projectMap() {
  try {
    if (fs.existsSync(PROJECTS_FILE)) {
      const raw = JSON.parse(fs.readFileSync(PROJECTS_FILE, 'utf-8'));
      if (raw && typeof raw === 'object') return raw;
    }
  } catch (err) {
    console.error('Failed to read viewer projects file:', err.message);
  }
  return { [LEGACY_PROJECT_SLUG]: LEGACY_REPO_PATH };
}

function defaultProjectSlug() {
  return Object.keys(projectMap())[0] || LEGACY_PROJECT_SLUG;
}

function resolveRepoPath(project) {
  const slug = project || defaultProjectSlug();
  const mapping = projectMap();
  const repoPath = mapping[slug];
  if (!repoPath) throw new Error(`Unknown project: ${slug}`);
  return { slug, repoPath, stateDir: path.join(repoPath, '.agent-supervisor') };
}

function repoCacheSlug(repoPath) {
  const digest = crypto.createHash('sha1').update(repoPath).digest('hex').slice(0, 10);
  return `${path.basename(repoPath)}-${digest}`;
}

function backfillStateAtPath(repoPath, cycle) {
  return path.join(STATIC_OUT, 'api', 'backfill', repoCacheSlug(repoPath), 'state-at', `${cycle}.json`);
}

function readJsonFile(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf-8'));
}

function git(repoPath, args) {
  return execSync(`git ${args}`, { cwd: repoPath, encoding: 'utf-8', timeout: 10000 }).trim();
}

function readLiveViewerState(repoPath) {
  return readJsonFile(path.join(repoPath, '.agent-supervisor', 'viewer_state.json'));
}

function readHistoricalViewerState(repoPath, cycle) {
  const tag = `cycle-${cycle}`;
  try {
    const raw = git(repoPath, `show ${tag}:.agent-supervisor/viewer_state.json`);
    return JSON.parse(raw);
  } catch {}
  const fallback = backfillStateAtPath(repoPath, cycle);
  if (fs.existsSync(fallback)) {
    return readJsonFile(fallback);
  }
  throw new Error(`No viewer_state snapshot found for cycle ${cycle}`);
}

function getCyclesFromGit(repoPath) {
  let tags;
  try {
    tags = git(repoPath, 'tag -l "cycle-*" --sort=version:refname').split('\n').filter(t => /^cycle-\d+$/.test(t));
  } catch { return []; }

  return tags.map(tag => {
    const cycle = parseInt(tag.replace('cycle-', ''), 10);
    let hash = '', timestamp = '', subject = '';
    try {
      const log = git(repoPath, `log -1 --format=%H%n%aI%n%s ${tag}`);
      const parts = log.split('\n');
      hash = parts[0] || '';
      timestamp = parts[1] || '';
      subject = parts[2] || '';
    } catch {}

    // Read cycle_meta.json from that commit
    let meta = {};
    try {
      const raw = git(repoPath, `show ${tag}:.agent-supervisor/cycle_meta.json`);
      meta = JSON.parse(raw);
    } catch {}

    return { cycle, hash, timestamp, message: subject, ...meta };
  });
}

function getCycleDiff(repoPath, cycle) {
  const tag = `cycle-${cycle}`;
  const prevTag = `cycle-${cycle - 1}`;
  try {
    // Check if previous tag exists
    git(repoPath, `rev-parse ${prevTag}`);
    return git(repoPath, `diff ${prevTag} ${tag} -- Tablet/`);
  } catch {
    try {
      // First cycle — diff against empty tree
      return git(repoPath, `diff 4b825dc642cb6eb9a060e54bf899d15f3bc9 ${tag} -- Tablet/`);
    } catch { return ''; }
  }
}

function writeJsonIfChanged(filePath, value) {
  const next = JSON.stringify(value);
  try {
    const current = fs.readFileSync(filePath, 'utf-8');
    if (current === next) {
      fs.chmodSync(filePath, 0o644);
      return;
    }
  } catch {}
  fs.writeFileSync(filePath, next);
  fs.chmodSync(filePath, 0o644);
}

function writeProjectStatic(projectSlug, repoPath, { writeRoot = false } = {}) {
  const live = readLiveViewerState(repoPath);
  const cycles = getCyclesFromGit(repoPath);
  const roots = [path.join(STATIC_OUT, projectSlug)];
  if (writeRoot) roots.unshift(STATIC_OUT);

  for (const root of roots) {
    const apiRoot = path.join(root, 'api');
    const stateAtDir = path.join(apiRoot, 'state-at');
    fs.mkdirSync(apiRoot, { recursive: true });
    fs.mkdirSync(stateAtDir, { recursive: true });
    writeJsonIfChanged(path.join(apiRoot, 'viewer-state.json'), live);
    writeJsonIfChanged(path.join(apiRoot, 'cycles.json'), cycles);

    const validCycles = new Set();
    for (const entry of cycles) {
      const cycleNum = entry.cycle;
      if (!Number.isInteger(cycleNum)) continue;
      validCycles.add(String(cycleNum));
      const outFile = path.join(stateAtDir, `${cycleNum}.json`);
      try {
        writeJsonIfChanged(outFile, readHistoricalViewerState(repoPath, cycleNum));
      } catch {}
    }
    for (const entry of fs.readdirSync(stateAtDir)) {
      const match = entry.match(/^(\d+)\.json$/);
      if (!match) continue;
      if (validCycles.has(match[1])) continue;
      try { fs.unlinkSync(path.join(stateAtDir, entry)); } catch {}
    }

    const htmlSrc = path.join(__dirname, 'public', 'index.html');
    const htmlDest = path.join(root, 'index.html');
    if (fs.existsSync(htmlSrc)) {
      fs.mkdirSync(root, { recursive: true });
      fs.copyFileSync(htmlSrc, htmlDest);
      fs.chmodSync(htmlDest, 0o644);
    }
  }
}

function writeStatic() {
  try {
    const projects = projectMap();
    const defaultSlug = defaultProjectSlug();
    for (const [slug, repoPath] of Object.entries(projects)) {
      writeProjectStatic(slug, repoPath, { writeRoot: slug === defaultSlug });
    }
  } catch (e) {
    console.error('Static write error:', e.message);
  }
}

function projectFromRequest(req) {
  return req.params.project || defaultProjectSlug();
}

function sendIndex(_req, res) {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
}

function handleDownloadTablet(res, project) {
  const { repoPath, stateDir } = resolveRepoPath(project);
  const tablet = JSON.parse(fs.readFileSync(path.join(stateDir, 'tablet.json'), 'utf-8'));
  const state = JSON.parse(fs.readFileSync(path.join(stateDir, 'state.json'), 'utf-8'));
  const tabletDir = path.join(repoPath, 'Tablet');
  const paperDir = path.join(repoPath, 'paper');

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

  const tmpDir = fs.mkdtempSync('/tmp/tablet-snapshot-');
  const snapDir = path.join(tmpDir, 'tablet-snapshot');
  fs.mkdirSync(path.join(snapDir, 'Tablet'), { recursive: true });
  fs.mkdirSync(path.join(snapDir, 'paper'), { recursive: true });

  if (fs.existsSync(tabletDir)) {
    for (const f of fs.readdirSync(tabletDir)) {
      if (f.endsWith('.lean') || f.endsWith('.tex')) {
        fs.copyFileSync(path.join(tabletDir, f), path.join(snapDir, 'Tablet', f));
      }
    }
  }
  if (fs.existsSync(paperDir)) {
    for (const f of fs.readdirSync(paperDir)) {
      fs.copyFileSync(path.join(paperDir, f), path.join(snapDir, 'paper', f));
    }
  }

  fs.writeFileSync(path.join(snapDir, 'README.md'), readme);
  fs.writeFileSync(path.join(snapDir, 'tablet.json'), JSON.stringify(tablet, null, 2));

  const zipPath = path.join(tmpDir, 'tablet-snapshot.zip');
  execSync(`cd "${tmpDir}" && zip -r "${zipPath}" tablet-snapshot/`, { timeout: 10000 });

  res.setHeader('Content-Type', 'application/zip');
  res.setHeader('Content-Disposition', `attachment; filename="tablet-snapshot-cycle${state.cycle || 0}.zip"`);
  const zipStream = fs.createReadStream(zipPath);
  zipStream.pipe(res);
  zipStream.on('end', () => {
    execSync(`rm -rf "${tmpDir}"`);
  });
}

function handleFeedbackPost(req, res, project) {
  const { action, feedback } = req.body;
  const { repoPath, stateDir } = resolveRepoPath(project);
  JSON.parse(fs.readFileSync(path.join(stateDir, 'state.json'), 'utf-8'));

  if (action === 'approve') {
    const signalPath = path.join(stateDir, 'human_approve.json');
    fs.writeFileSync(signalPath, JSON.stringify({ action: 'approve', timestamp: new Date().toISOString() }));
    return res.json({ ok: true, message: 'Approval signal written. Supervisor will continue.' });
  }
  if (action === 'feedback') {
    const feedbackPath = path.join(repoPath, 'HUMAN_INPUT.md');
    fs.writeFileSync(feedbackPath, feedback || '');
    const signalPath = path.join(stateDir, 'human_feedback.json');
    fs.writeFileSync(signalPath, JSON.stringify({ action: 'feedback', feedback: feedback || '', timestamp: new Date().toISOString() }));
    const pausePath = path.join(stateDir, 'pause');
    try { fs.unlinkSync(pausePath); } catch {}
    return res.json({ ok: true, message: 'Feedback written. Supervisor will run another cycle.' });
  }
  return res.status(400).json({ error: 'action must be "approve" or "feedback"' });
}

function handleFeedbackGet(res, project) {
  const { repoPath, stateDir } = resolveRepoPath(project);
  const state = JSON.parse(fs.readFileSync(path.join(stateDir, 'state.json'), 'utf-8'));
  const awaiting = state.awaiting_human_input || false;
  const phase = state.phase || '';
  const lastReview = state.last_review || {};
  let humanInput = '';
  try { humanInput = fs.readFileSync(path.join(repoPath, 'HUMAN_INPUT.md'), 'utf-8'); } catch {}

  res.json({
    awaiting_input: awaiting,
    phase,
    last_review_decision: lastReview.decision || '',
    last_review_reason: lastReview.reason || '',
    human_input: humanInput,
  });
}

app.get(BASE, (_req, res) => res.redirect(`${BASE}/${defaultProjectSlug()}/`));
app.get(`${BASE}/`, (_req, res) => res.redirect(`${BASE}/${defaultProjectSlug()}/`));
app.get(`${BASE}/:project`, (req, res) => res.redirect(`${BASE}/${req.params.project}/`));
app.get(`${BASE}/:project/`, sendIndex);
app.use(BASE, express.static(path.join(__dirname, 'public')));

// API endpoints
app.get(`${BASE}/api/viewer-state.json`, (req, res) => {
  try {
    const { repoPath } = resolveRepoPath(defaultProjectSlug());
    res.json(readLiveViewerState(repoPath));
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.get(`${BASE}/:project/api/viewer-state.json`, (req, res) => {
  try {
    const { repoPath } = resolveRepoPath(projectFromRequest(req));
    res.json(readLiveViewerState(repoPath));
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.get(`${BASE}/api/cycles.json`, (req, res) => {
  try {
    const { repoPath } = resolveRepoPath(defaultProjectSlug());
    res.json(getCyclesFromGit(repoPath));
  } catch { res.json([]); }
});

app.get(`${BASE}/:project/api/cycles.json`, (req, res) => {
  try {
    const { repoPath } = resolveRepoPath(projectFromRequest(req));
    res.json(getCyclesFromGit(repoPath));
  } catch { res.json([]); }
});

app.get(`${BASE}/api/state-at/:cycle`, (req, res) => {
  const cycle = parseInt(String(req.params.cycle).replace(/\.json$/, ''), 10);
  if (isNaN(cycle)) return res.status(400).json({ error: 'Invalid cycle' });
  try {
    const { repoPath } = resolveRepoPath(defaultProjectSlug());
    res.json(readHistoricalViewerState(repoPath, cycle));
  } catch (e) {
    res.status(404).json({ error: `Cycle ${cycle} not found: ${e.message}` });
  }
});

app.get(`${BASE}/:project/api/state-at/:cycle`, (req, res) => {
  const cycle = parseInt(String(req.params.cycle).replace(/\.json$/, ''), 10);
  if (isNaN(cycle)) return res.status(400).json({ error: 'Invalid cycle' });
  try {
    const { repoPath } = resolveRepoPath(projectFromRequest(req));
    res.json(readHistoricalViewerState(repoPath, cycle));
  } catch (e) {
    res.status(404).json({ error: `Cycle ${cycle} not found: ${e.message}` });
  }
});

app.get(`${BASE}/api/diff/:cycle`, (req, res) => {
  const cycle = parseInt(req.params.cycle, 10);
  if (isNaN(cycle)) return res.status(400).send('Invalid cycle');
  const { repoPath } = resolveRepoPath(defaultProjectSlug());
  const diff = getCycleDiff(repoPath, cycle);
  res.type('text/plain').send(diff);
});

app.get(`${BASE}/:project/api/diff/:cycle`, (req, res) => {
  const cycle = parseInt(req.params.cycle, 10);
  if (isNaN(cycle)) return res.status(400).send('Invalid cycle');
  const { repoPath } = resolveRepoPath(projectFromRequest(req));
  const diff = getCycleDiff(repoPath, cycle);
  res.type('text/plain').send(diff);
});

// API: download tablet snapshot as zip
app.get(`${BASE}/api/download-tablet`, (req, res) => {
  try {
    handleDownloadTablet(res, defaultProjectSlug());
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get(`${BASE}/:project/api/download-tablet`, (req, res) => {
  try {
    handleDownloadTablet(res, projectFromRequest(req));
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// API: submit human feedback
app.use(express.json());

app.post(`${BASE}/api/feedback`, (req, res) => {
  try {
    handleFeedbackPost(req, res, defaultProjectSlug());
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.post(`${BASE}/:project/api/feedback`, (req, res) => {
  try {
    handleFeedbackPost(req, res, projectFromRequest(req));
  } catch (e) {
    return res.status(500).json({ error: e.message });
  }
});

// API: get current human feedback status
app.get(`${BASE}/api/feedback`, (req, res) => {
  try {
    handleFeedbackGet(res, defaultProjectSlug());
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get(`${BASE}/:project/api/feedback`, (req, res) => {
  try {
    handleFeedbackGet(res, projectFromRequest(req));
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

function startServer() {
  return app.listen(PORT, () => {
    console.log(`Tablet viewer at http://localhost:${PORT}${BASE}/${defaultProjectSlug()}/`);
    console.log(`Projects: ${JSON.stringify(projectMap())}`);
  });
}

if (require.main === module) {
  startServer();
}

module.exports = {
  app,
  startServer,
  writeStatic,
};
