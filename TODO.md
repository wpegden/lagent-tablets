# TODO: Post-burst reliability fixes

Once burst.py is 100% solid, address these:

1. **cycle.py validation rework**: Remove snapshot before/after comparison. After burst completion, directly check: did the active node's .lean file change? Run `lake env lean` on it. Feed compilation errors into outcome.

2. **prompts.py compilation error feedback**: When previous outcome is INVALID with build_output, show exact Lean errors prominently: "PREVIOUS CYCLE FAILED: Compilation errors:\n```\n{errors}\n```"

3. **health.py .lake management**: Add `ensure_lake_build` that runs `lake build Tablet` before verification to ensure oleans exist. Call at cycle start.

4. **cli.py simplification**: Remove dead code from old interactive model (adapter creation, process cleanup). Simplify main loop.

5. **cycle.py permission timing**: Verify setup_permissions runs before burst, snapshot after permissions. The script-based approach for Codex eliminates timing issues; interactive Claude/Gemini still need correct ordering.

6. **Orphan-status protection policy**: Decide which nodes should be protected from orphan status by default. In particular, clarify whether all paper-facing statements (not just `paper_main_result`) should be exempt, and whether that classification should be reviewer-assigned and persisted in tablet metadata.

7. **theorem_stating Lean-shortcut policy**: Consider amending the theorem-stating NL-proof worker prompt so that if the worker can immediately give a complete Lean proof of the current node from its children, it may do that instead of writing an NL proof. If enabled, the prompt must also include the exact deterministic checker command needed to validate the Lean proof, and the cycle semantics must make clear that a successfully closed Lean node supersedes the need for NL-proof repair on that node.

8. **`.tex` correspondence propagation policy**: Consider adopting the convention that a `.tex` statement change only propagates correspondence loss to importing ancestors when the changed node is a `definition`, not when it is a `lemma`/`theorem`/`corollary`. This would keep theorem-statement edits from reopening ancestor correspondence by default, while still treating definitional meaning changes as correspondence-relevant.

9. **Theorem/definition authoring guidance**: Add explicit agent guidance that `lemma`/`theorem`/`corollary` nodes should not double as definitions. If a paper-facing concept is being introduced, it should be modeled as its own definition node rather than embedded implicitly inside a result statement.
