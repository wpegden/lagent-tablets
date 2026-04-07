# Agent CLI Reliability Notes (April 2026)

## Critical findings from GitHub issue research

### Claude `-p` mode
- **Infinite retry on permission denial** (#37935): agent loops forever on unresolvable permission denials
- **SIGTERM after 3-10 min** (#29642): long headless sessions killed randomly. Short tasks (<2 min) work.
- **`--dangerously-skip-permissions` broken** (#39523): hardcoded protection for `.claude/`, `.git/` etc overrides all bypass
- **Workarounds**: use `--max-turns`, chunk work into short sessions, wrap in `timeout`, avoid WebSearch

### Gemini `-p` mode
- **Sub-agent hangs indefinitely** (#21409): delegation to "generalist" sub-agent hangs forever. Workaround: prompt "do not use sub-agents"
- **YOLO mode broken in non-interactive** (#13561): still asks for approval in `-p` mode, hangs waiting for input
- **Non-interactive mode being rewritten**: not production-ready, acknowledged by maintainers

### Codex `exec` mode
- **Tool call hang regression** (#16364): v0.117.0+ hangs during tool calls. Pin to v0.116.0
- **Resume unreliable**: rollout files may not be written (#16994, #16897)
- **5-min dead websocket detection** (#17003): dead connections silently appear alive

## Defensive measures (must implement)

1. **External `timeout` on every CLI invocation** -- coreutils timeout as outer watchdog
2. **`--max-turns` where available** to bound execution
3. **Chunk long work into short sessions** -- state passed via files
4. **Memory limits** via cgroups (`systemd-run --scope -p MemoryMax=4G`)
5. **For Gemini**: include "do not delegate to sub-agents" in all prompts
6. **For Codex**: pin to pre-0.117.0 version
7. **For Claude**: avoid WebSearch, structure prompts for frequent output
8. **Always check both exit code AND output** -- exit 0 can have empty/truncated output
9. **File-based stall detection** -- don't trust the process is alive just because the PID exists
