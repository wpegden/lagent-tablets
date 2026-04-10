const express = require('express');
const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const app = express();
const PORT = process.env.PORT || 3300;
const BASE = process.env.BASE_PATH || '/lagent-tablets';
const STATIC_OUT = process.env.STATIC_OUT || '/home/leanagent/lagent-tablets-web';
const PROJECTS_ROOT = process.env.PROJECTS_ROOT || '/home/leanagent/math';
const DEFAULT_PROJECT_SLUG = process.env.DEFAULT_PROJECT_SLUG || '';
const LEGACY_REPO_PATH = process.env.REPO_PATH || '';

function isValidProjectSlug(slug) {
  return typeof slug === 'string' && /^[A-Za-z0-9._-]+$/.test(slug);
}

function configPathForRepo(repoPath) {
  return path.join(repoPath, 'lagent.config.json');
}

function viewerApiDir(repoPath) {
  return path.join(repoPath, '.agent-supervisor', 'viewer');
}

function chatsRepoDir(repoPath) {
  return path.join(repoPath, '.agent-supervisor', 'chats');
}

function discoverProjects() {
  if (LEGACY_REPO_PATH) {
    return [{ slug: path.basename(LEGACY_REPO_PATH), repoPath: LEGACY_REPO_PATH }];
  }
  if (!fs.existsSync(PROJECTS_ROOT)) return [];
  return fs.readdirSync(PROJECTS_ROOT, { withFileTypes: true })
    .filter(entry => entry.isDirectory() && isValidProjectSlug(entry.name))
    .map(entry => ({ slug: entry.name, repoPath: path.join(PROJECTS_ROOT, entry.name) }))
    .filter(entry => fs.existsSync(configPathForRepo(entry.repoPath)))
    .sort((a, b) => a.slug.localeCompare(b.slug));
}

function defaultProjectSlug() {
  if (DEFAULT_PROJECT_SLUG) return DEFAULT_PROJECT_SLUG;
  const projects = discoverProjects();
  return projects.length ? projects[0].slug : '';
}

function resolveRepoPath(project) {
  const slug = project || defaultProjectSlug();
  if (!isValidProjectSlug(slug)) throw new Error(`Invalid project: ${project}`);
  const repoPath = LEGACY_REPO_PATH && slug === path.basename(LEGACY_REPO_PATH)
    ? LEGACY_REPO_PATH
    : path.join(PROJECTS_ROOT, slug);
  if (!(LEGACY_REPO_PATH && repoPath === LEGACY_REPO_PATH) && !fs.existsSync(configPathForRepo(repoPath))) {
    throw new Error(`Unknown project: ${slug}`);
  }
  return { slug, repoPath, stateDir: path.join(repoPath, '.agent-supervisor') };
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
  const raw = git(repoPath, `show ${tag}:.agent-supervisor/viewer_state.json`);
  return JSON.parse(raw);
}

function chatCycleDir(cycle) {
  return `cycle-${String(cycle).padStart(4, '0')}`;
}

function readTextFileSafe(filePath) {
  try {
    return fs.readFileSync(filePath, 'utf-8');
  } catch {
    return '';
  }
}

function listWorkingTreeChatArtifacts(repoPath, cycle) {
  const root = path.join(chatsRepoDir(repoPath), chatCycleDir(cycle));
  if (!fs.existsSync(root)) return [];
  return sortArtifactNames(fs.readdirSync(root, { withFileTypes: true })
    .filter(entry => entry.isDirectory())
    .map(entry => entry.name)
  );
}

function listGitChatArtifacts(repoPath, cycle) {
  const chatsRepo = chatsRepoDir(repoPath);
  if (!fs.existsSync(path.join(chatsRepo, '.git'))) return [];
  const prefix = chatCycleDir(cycle) + '/';
  try {
    const files = git(chatsRepo, `ls-tree -r --name-only cycle-${cycle} -- ${prefix}`)
      .split('\n')
      .filter(Boolean);
    return sortArtifactNames(Array.from(new Set(
      files
        .filter(name => name.startsWith(prefix))
        .map(name => name.slice(prefix.length).split('/')[0])
        .filter(Boolean)
    )));
  } catch {
    return [];
  }
}

function readWorkingTreeChatFiles(repoPath, cycle, artifact) {
  const dir = path.join(chatsRepoDir(repoPath), chatCycleDir(cycle), artifact);
  return {
    prompt: readTextFileSafe(path.join(dir, 'prompt.txt')),
    output: readTextFileSafe(path.join(dir, 'output.log')),
    transcriptJsonl: readTextFileSafe(path.join(dir, 'transcript.jsonl')),
    transcriptJson: readTextFileSafe(path.join(dir, 'transcript.json')),
  };
}

function readGitChatFiles(repoPath, cycle, artifact) {
  const chatsRepo = chatsRepoDir(repoPath);
  const base = `${chatCycleDir(cycle)}/${artifact}`;
  const read = (name) => {
    try {
      return git(chatsRepo, `show cycle-${cycle}:${base}/${name}`);
    } catch {
      return '';
    }
  };
  return {
    prompt: read('prompt.txt'),
    output: read('output.log'),
    transcriptJsonl: read('transcript.jsonl'),
    transcriptJson: read('transcript.json'),
  };
}

function artifactTitle(name) {
  let attempt = null;
  let base = name;
  let m = name.match(/^(.*)_attempt_(\d+)$/);
  if (m) {
    base = m[1];
    attempt = Number(m[2]);
  }
  if (base === 'worker_handoff') return attempt ? `Worker attempt ${attempt}` : 'Worker';
  if (base === 'reviewer_decision') return attempt ? `Reviewer attempt ${attempt}` : 'Reviewer';
  m = base.match(/^correspondence_result_(\d+)$/);
  if (m) return attempt ? `Correspondence ${Number(m[1]) + 1} attempt ${attempt}` : `Correspondence ${Number(m[1]) + 1}`;
  m = base.match(/^nl_proof_(.+)_(\d+)$/);
  if (m) {
    const title = `Soundness ${m[1]} (${Number(m[2]) + 1})`;
    return attempt ? `${title} attempt ${attempt}` : title;
  }
  const fallback = base.replace(/_/g, ' ');
  return attempt ? `${fallback} attempt ${attempt}` : fallback;
}

function artifactSortKey(name) {
  let attempt = 0;
  let base = name;
  let m = name.match(/^(.*)_attempt_(\d+)$/);
  if (m) {
    base = m[1];
    attempt = Number(m[2]);
  }
  if (base === 'worker_handoff') return [0, 0, '', attempt, name];
  if (base === 'reviewer_decision') return [3, 0, '', attempt, name];
  m = base.match(/^correspondence_result_(\d+)$/);
  if (m) return [1, Number(m[1]), '', attempt, name];
  m = base.match(/^nl_proof_(.+)_(\d+)$/);
  if (m) return [2, Number(m[2]), String(m[1]), attempt, name];
  return [4, 0, base, attempt, name];
}

function sortArtifactNames(names) {
  return [...names].sort((a, b) => {
    const ka = artifactSortKey(a);
    const kb = artifactSortKey(b);
    for (let i = 0; i < ka.length; i++) {
      if (ka[i] < kb[i]) return -1;
      if (ka[i] > kb[i]) return 1;
    }
    return 0;
  });
}

function collectTextParts(value, parts) {
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (trimmed) parts.push(trimmed);
    return;
  }
  if (Array.isArray(value)) {
    for (const item of value) collectTextParts(item, parts);
    return;
  }
  if (!value || typeof value !== 'object') return;
  if (typeof value.text === 'string') {
    const trimmed = value.text.trim();
    if (trimmed) parts.push(trimmed);
  }
  for (const key of ['content', 'parts', 'chunks', 'value']) {
    if (key in value) collectTextParts(value[key], parts);
  }
}

function normalizeTranscriptEntry(role, text, kind = 'message', title = '') {
  const trimmed = (text || '').trim();
  if (!trimmed) return null;
  return { role: role || 'entry', kind, title: title || '', text: trimmed };
}

function parseCodexOutputEntries(text) {
  const entries = [];
  for (const rawLine of (text || '').split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    let rec;
    try {
      rec = JSON.parse(line);
    } catch {
      continue;
    }
    if (rec.type === 'item.completed' && rec.item && rec.item.type === 'agent_message') {
      const entry = normalizeTranscriptEntry('assistant', rec.item.text || '', 'message', 'Assistant');
      if (entry) entries.push(entry);
      continue;
    }
    if (rec.item && rec.item.type === 'command_execution' && (rec.type === 'item.completed' || rec.type === 'item.started')) {
      const command = String(rec.item.command || '').trim();
      const output = String(rec.item.aggregated_output || '').trim();
      const label = rec.type === 'item.started' ? 'Command (running)' : 'Command';
      const combined = [command, output].filter(Boolean).join('\n\n');
      const entry = normalizeTranscriptEntry('tool', combined, 'command', label);
      if (entry) entries.push(entry);
    }
  }
  return entries;
}

function parseJsonlTranscriptEntries(text) {
  const entries = [];
  for (const rawLine of (text || '').split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    let rec;
    try {
      rec = JSON.parse(line);
    } catch {
      continue;
    }
    const msg = rec.message && typeof rec.message === 'object' ? rec.message : rec;
    const role = msg.role || rec.role || rec.type || '';
    const parts = [];
    collectTextParts(msg.content ?? rec.content ?? msg, parts);
    const entry = normalizeTranscriptEntry(role, parts.join('\n\n'), 'message', role || 'Entry');
    if (entry) entries.push(entry);
  }
  return entries;
}

function parseJsonTranscriptEntries(text) {
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    return [];
  }
  const entries = [];
  const messages = Array.isArray(data?.messages) ? data.messages : [];
  for (const msg of messages) {
    const role = msg.role || msg.author || msg.speaker || '';
    const parts = [];
    collectTextParts(msg.content ?? msg.parts ?? msg, parts);
    const entry = normalizeTranscriptEntry(role, parts.join('\n\n'), 'message', role || 'Entry');
    if (entry) entries.push(entry);
  }
  if (entries.length) return entries;
  const parts = [];
  collectTextParts(data, parts);
  const fallback = normalizeTranscriptEntry('entry', parts.join('\n\n'), 'message', 'Transcript');
  return fallback ? [fallback] : [];
}

function buildArtifactChatData(name, files) {
  const entries = [];
  if (files.prompt) {
    entries.push({
      role: 'prompt',
      kind: 'prompt',
      title: 'Prompt',
      text: files.prompt.trim(),
    });
  }
  if (files.output) entries.push(...parseCodexOutputEntries(files.output));
  else if (files.transcriptJsonl) entries.push(...parseJsonlTranscriptEntries(files.transcriptJsonl));
  else if (files.transcriptJson) entries.push(...parseJsonTranscriptEntries(files.transcriptJson));
  return {
    id: name,
    title: artifactTitle(name),
    entries,
    hasTranscript: Boolean(files.output || files.transcriptJsonl || files.transcriptJson),
  };
}

function currentInFlightCycle(repoPath) {
  const viewer = readLiveViewerState(repoPath);
  return Number(viewer?.meta?.in_flight_cycle || viewer?.state?.cycle || 0);
}

function readLiveChats(repoPath) {
  const cycle = currentInFlightCycle(repoPath);
  if (!cycle) return { cycle: 0, source: 'live', artifacts: [] };
  const artifacts = listWorkingTreeChatArtifacts(repoPath, cycle)
    .map(name => buildArtifactChatData(name, readWorkingTreeChatFiles(repoPath, cycle, name)));
  return { cycle, source: 'live', artifacts };
}

function readHistoricalChats(repoPath, cycle) {
  const artifacts = listGitChatArtifacts(repoPath, cycle)
    .map(name => buildArtifactChatData(name, readGitChatFiles(repoPath, cycle, name)));
  return { cycle, source: 'git', artifacts };
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

function ensureSymlink(linkPath, targetPath) {
  fs.mkdirSync(path.dirname(linkPath), { recursive: true });
  try {
    const existing = fs.lstatSync(linkPath);
    if (existing.isSymbolicLink() && fs.readlinkSync(linkPath) === targetPath) return;
    fs.rmSync(linkPath, { recursive: true, force: true });
  } catch {}
  fs.symlinkSync(targetPath, linkPath);
}

function writeProjectStatic(projectSlug, repoPath, { writeRoot = false } = {}) {
  const roots = [path.join(STATIC_OUT, projectSlug)];
  if (writeRoot) roots.unshift(STATIC_OUT);
  const apiTarget = viewerApiDir(repoPath);
  const htmlSrc = path.join(__dirname, 'public', 'index.html');

  for (const root of roots) {
    fs.mkdirSync(root, { recursive: true });
    ensureSymlink(path.join(root, 'api'), apiTarget);
    if (fs.existsSync(htmlSrc)) {
      ensureSymlink(path.join(root, 'index.html'), htmlSrc);
    }
  }
}

function writeStatic() {
  try {
    const projects = discoverProjects();
    const defaultSlug = defaultProjectSlug();
    for (const { slug, repoPath } of projects) {
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

app.get(`${BASE}/api/chats.json`, (req, res) => {
  try {
    const { repoPath } = resolveRepoPath(defaultProjectSlug());
    res.json(readLiveChats(repoPath));
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get(`${BASE}/:project/api/chats.json`, (req, res) => {
  try {
    const { repoPath } = resolveRepoPath(projectFromRequest(req));
    res.json(readLiveChats(repoPath));
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get(`${BASE}/api/chats-at/:cycle`, (req, res) => {
  const cycle = parseInt(String(req.params.cycle).replace(/\.json$/, ''), 10);
  if (isNaN(cycle)) return res.status(400).json({ error: 'Invalid cycle' });
  try {
    const { repoPath } = resolveRepoPath(defaultProjectSlug());
    res.json(readHistoricalChats(repoPath, cycle));
  } catch (e) {
    res.status(404).json({ error: `Chat cycle ${cycle} not found: ${e.message}` });
  }
});

app.get(`${BASE}/:project/api/chats-at/:cycle`, (req, res) => {
  const cycle = parseInt(String(req.params.cycle).replace(/\.json$/, ''), 10);
  if (isNaN(cycle)) return res.status(400).json({ error: 'Invalid cycle' });
  try {
    const { repoPath } = resolveRepoPath(projectFromRequest(req));
    res.json(readHistoricalChats(repoPath, cycle));
  } catch (e) {
    res.status(404).json({ error: `Chat cycle ${cycle} not found: ${e.message}` });
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
    console.log(`Projects root: ${PROJECTS_ROOT}`);
  });
}

if (require.main === module) {
  startServer();
}

module.exports = {
  app,
  buildArtifactChatData,
  parseCodexOutputEntries,
  parseJsonlTranscriptEntries,
  parseJsonTranscriptEntries,
  readHistoricalViewerState,
  readHistoricalChats,
  readLiveViewerState,
  readLiveChats,
  startServer,
  writeStatic,
};
