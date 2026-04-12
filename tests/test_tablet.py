"""Tests for tablet operations."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lagent_tablets.state import TabletNode, TabletState
from lagent_tablets.tablet import (
    PREAMBLE_NAME,
    check_placeholder_language,
    compute_import_closure,
    declaration_hash,
    declaration_line,
    extract_declaration_name,
    extract_imports,
    extract_marker_name,
    extract_noderefs,
    extract_tablet_imports,
    find_name_conflicts,
    find_orphan_nodes,
    find_unsupported_nodes,
    generate_header_tex,
    generate_index_md,
    generate_node_lean,
    generate_readme_md,
    has_sorry,
    infer_main_result_targets_from_paper,
    is_valid_node_name,
    mask_comments_and_strings,
    main_result_target_issues,
    node_lean_path,
    node_tex_path,
    main_result_label_issues,
    regenerate_support_files,
    register_new_node,
    resolve_main_result_targets,
    scan_forbidden_keywords,
    validate_imports,
    validate_preamble_diff,
    validate_tex_format,
)


class TestLeanGeneration(unittest.TestCase):

    def test_generate_node_lean(self):
        content = generate_node_lean(
            "compactness_of_K",
            "theorem compactness_of_K (K : Set α) (hK : IsCompact K) : ∃ x ∈ K, True",
            ["Tablet.Preamble"],
        )
        self.assertIn("import Tablet.Preamble", content)
        self.assertIn("-- [TABLET NODE: compactness_of_K]", content)
        self.assertIn("theorem compactness_of_K", content)
        self.assertIn(":=", content)
        self.assertIn("sorry", content)

    def test_generate_node_lean_appends_assign(self):
        content = generate_node_lean("foo", "theorem foo : True", ["Tablet.Preamble"])
        self.assertIn("theorem foo : True :=", content)

    def test_generate_node_lean_no_double_assign(self):
        content = generate_node_lean("foo", "theorem foo : True :=", ["Tablet.Preamble"])
        self.assertNotIn(":= :=", content)

    def test_declaration_line_extraction(self):
        content = generate_node_lean("bar", "theorem bar (x : Nat) : x = x", ["Tablet.Preamble"])
        decl = declaration_line(content)
        self.assertIn("theorem bar", decl)
        self.assertIn(":=", decl)

    def test_declaration_hash_stable(self):
        content = generate_node_lean("baz", "lemma baz : True", ["Tablet.Preamble"])
        h1 = declaration_hash(content)
        h2 = declaration_hash(content)
        self.assertEqual(h1, h2)
        self.assertTrue(len(h1) > 10)

    def test_declaration_hash_changes_on_modification(self):
        c1 = generate_node_lean("a", "theorem a : True", ["Tablet.Preamble"])
        c2 = generate_node_lean("a", "theorem a : False", ["Tablet.Preamble"])
        self.assertNotEqual(declaration_hash(c1), declaration_hash(c2))

    def test_extract_marker_name(self):
        content = generate_node_lean("my_thm", "theorem my_thm : True", ["Tablet.Preamble"])
        self.assertEqual(extract_marker_name(content), "my_thm")

    def test_extract_declaration_name(self):
        content = generate_node_lean("my_thm", "theorem my_thm : True", ["Tablet.Preamble"])
        self.assertEqual(extract_declaration_name(content), "my_thm")

    def test_extract_imports(self):
        content = "import Tablet.Preamble\nimport Mathlib.Topology.Basic\n\ntheorem foo : True := sorry\n"
        self.assertEqual(extract_imports(content), ["Tablet.Preamble", "Mathlib.Topology.Basic"])

    def test_extract_tablet_imports(self):
        content = "import Tablet.Preamble\nimport Tablet.helper_a\nimport Mathlib.X\n"
        self.assertEqual(extract_tablet_imports(content), ["Preamble", "helper_a"])


class TestImportValidation(unittest.TestCase):

    def test_valid_imports(self):
        content = "import Tablet.Preamble\nimport Mathlib.Topology.Basic\n"
        self.assertEqual(validate_imports(content, ["Mathlib"]), [])

    def test_unauthorized_import(self):
        content = "import Tablet.Preamble\nimport SomeOther.Module\n"
        violations = validate_imports(content, ["Mathlib"])
        self.assertEqual(violations, ["SomeOther.Module"])

    def test_multiple_allowed_prefixes(self):
        content = "import Tablet.foo\nimport Mathlib.X\nimport MyLib.Y\n"
        self.assertEqual(validate_imports(content, ["Mathlib", "MyLib"]), [])


class TestMasking(unittest.TestCase):

    def test_line_comment(self):
        text = "hello -- sorry this is a comment\nworld"
        masked = mask_comments_and_strings(text)
        self.assertNotIn("sorry", masked)
        self.assertIn("hello", masked)
        self.assertIn("world", masked)

    def test_block_comment(self):
        text = "before /- sorry inside -/ after"
        masked = mask_comments_and_strings(text)
        self.assertNotIn("sorry", masked)
        self.assertIn("before", masked)
        self.assertIn("after", masked)

    def test_nested_block_comment(self):
        text = "a /- outer /- inner sorry -/ still outer -/ b"
        masked = mask_comments_and_strings(text)
        self.assertNotIn("sorry", masked)
        self.assertIn("a", masked)
        self.assertIn("b", masked)

    def test_string_literal(self):
        text = 'x "sorry" y'
        masked = mask_comments_and_strings(text)
        self.assertNotIn("sorry", masked)
        self.assertIn("x", masked)
        self.assertIn("y", masked)

    def test_sorry_outside_comments(self):
        text = "theorem foo : True := sorry"
        self.assertTrue(has_sorry(text))

    def test_sorry_only_in_comment(self):
        text = "theorem foo : True := by\n  -- sorry\n  exact trivial"
        self.assertFalse(has_sorry(text))


class TestForbiddenKeywords(unittest.TestCase):

    def test_detects_sorry(self):
        content = "theorem foo : True := sorry"
        hits = scan_forbidden_keywords(content, ["sorry"])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["keyword"], "sorry")

    def test_ignores_commented_sorry(self):
        content = "-- sorry\ntheorem foo : True := by exact trivial"
        hits = scan_forbidden_keywords(content, ["sorry"])
        self.assertEqual(len(hits), 0)

    def test_detects_axiom(self):
        content = "axiom cheat : False"
        hits = scan_forbidden_keywords(content, ["axiom"])
        self.assertEqual(len(hits), 1)

    def test_detects_unsafe(self):
        content = "unsafe def bad := unsafeCoerce ()"
        hits = scan_forbidden_keywords(content, ["unsafe"])
        self.assertEqual(len(hits), 1)

    def test_detects_hash_eval_directive(self):
        content = "#eval 1 + 1"
        hits = scan_forbidden_keywords(content, ["#eval"])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["keyword"], "#eval")

    def test_detects_implemented_by_attribute(self):
        content = "attribute [implemented_by foo] bar"
        hits = scan_forbidden_keywords(content, ["implemented_by"])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["keyword"], "implemented_by")


class TestTexValidation(unittest.TestCase):

    def test_valid_regular_node(self):
        tex = "\\begin{theorem}[Foo]\nStatement\n\\end{theorem}\n\n\\begin{proof}\nProof text\n\\end{proof}\n"
        errors = validate_tex_format(tex)
        self.assertEqual(errors, [])

    def test_valid_regular_corollary_node(self):
        tex = "\\begin{corollary}[Foo]\nStatement\n\\end{corollary}\n\n\\begin{proof}\nProof text\n\\end{proof}\n"
        errors = validate_tex_format(tex)
        self.assertEqual(errors, [])

    def test_valid_regular_helper_node(self):
        tex = "\\begin{helper}[Foo]\nStatement\n\\end{helper}\n\n\\begin{proof}\nProof text\n\\end{proof}\n"
        errors = validate_tex_format(tex)
        self.assertEqual(errors, [])

    def test_missing_statement(self):
        tex = "\\begin{proof}\nProof text\n\\end{proof}\n"
        errors = validate_tex_format(tex)
        self.assertTrue(any("Missing statement" in e for e in errors))

    def test_multiple_statements(self):
        tex = "\\begin{theorem}\nA\n\\end{theorem}\n\\begin{lemma}\nB\n\\end{lemma}\n"
        errors = validate_tex_format(tex)
        self.assertTrue(any("Multiple" in e for e in errors))

    def test_valid_preamble(self):
        tex = "\\begin{proposition}[BW]\nStatement\n\\end{proposition}\n"
        errors = validate_tex_format(tex, is_preamble=True)
        self.assertEqual(errors, [])

    def test_regular_nodes_reject_proposition(self):
        tex = "\\begin{proposition}[Foo]\nStatement\n\\end{proposition}\n"
        errors = validate_tex_format(tex)
        self.assertTrue(any("theorem/lemma/definition/corollary" in e for e in errors))

    def test_valid_preamble_with_definition(self):
        tex = "\\begin{definition}[thing]\nThing.\n\\end{definition}\n"
        errors = validate_tex_format(tex, is_preamble=True)
        self.assertEqual(errors, [])

    def test_preamble_rejects_proof(self):
        tex = "\\begin{proposition}\nA\n\\end{proposition}\n\\begin{proof}\nP\n\\end{proof}\n"
        errors = validate_tex_format(tex, is_preamble=True)
        self.assertTrue(any("proof" in e.lower() for e in errors))

    def test_extract_noderefs(self):
        tex = "By \\noderef{helper_a} and \\noderef{helper_b}, we have..."
        refs = extract_noderefs(tex)
        self.assertEqual(refs, ["helper_a", "helper_b"])

    def test_placeholder_detection(self):
        tex = "\\begin{proof}\nThis is obvious from the definition.\n\\end{proof}"
        hits = check_placeholder_language(tex)
        self.assertTrue(len(hits) > 0)

    def test_no_placeholder(self):
        tex = "\\begin{proof}\nBy applying Lemma 3.2 to the sequence, we obtain convergence.\n\\end{proof}"
        hits = check_placeholder_language(tex)
        self.assertEqual(hits, [])


class TestImportClosure(unittest.TestCase):

    def test_simple_chain(self):
        repo = Path(tempfile.mkdtemp())
        tdir = repo / "Tablet"
        tdir.mkdir()
        (tdir / "Preamble.lean").write_text("import Mathlib.Topology.Basic\n")
        (tdir / "helper_a.lean").write_text("import Tablet.Preamble\ntheorem helper_a : True := sorry\n")
        (tdir / "thm_main.lean").write_text("import Tablet.helper_a\ntheorem thm_main : True := sorry\n")

        closure = compute_import_closure(repo, "thm_main")
        self.assertEqual(closure, {"helper_a", "Preamble"})

    def test_diamond(self):
        repo = Path(tempfile.mkdtemp())
        tdir = repo / "Tablet"
        tdir.mkdir()
        (tdir / "Preamble.lean").write_text("import Mathlib.X\n")
        (tdir / "a.lean").write_text("import Tablet.Preamble\ntheorem a : True := sorry\n")
        (tdir / "b.lean").write_text("import Tablet.Preamble\ntheorem b : True := sorry\n")
        (tdir / "c.lean").write_text("import Tablet.a\nimport Tablet.b\ntheorem c : True := sorry\n")

        closure = compute_import_closure(repo, "c")
        self.assertEqual(closure, {"a", "b", "Preamble"})

    def test_missing_file(self):
        repo = Path(tempfile.mkdtemp())
        closure = compute_import_closure(repo, "nonexistent")
        self.assertEqual(closure, set())


class TestTargetSupportClosure(unittest.TestCase):

    def test_target_covering_leaf_is_supported(self):
        repo = Path(tempfile.mkdtemp())
        tdir = repo / "Tablet"
        tdir.mkdir()
        (tdir / "Preamble.lean").write_text("import Mathlib.X\n")
        (tdir / "Preamble.tex").write_text("\\begin{proposition}Imports.\\end{proposition}\n")
        (tdir / "helper.lean").write_text("import Tablet.Preamble\ntheorem helper : True := sorry\n")
        (tdir / "helper.tex").write_text("\\begin{helper}\nTrue.\n\\end{helper}\n\\begin{proof}\nProof.\n\\end{proof}\n")
        (tdir / "main_thm.lean").write_text("import Tablet.helper\ntheorem main_thm : True := sorry\n")
        (tdir / "main_thm.tex").write_text("\\begin{theorem}\nTrue.\n\\end{theorem}\n\\begin{proof}\nProof.\n\\end{proof}\n")

        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "helper": TabletNode(name="helper", kind="helper_lemma", status="open"),
            "main_thm": TabletNode(
                name="main_thm",
                kind="ordinary",
                status="open",
                paper_provenance={"start_line": 10, "end_line": 12, "tex_label": "main"},
            ),
        })
        unsupported = find_unsupported_nodes(tablet, repo, ["main"])
        self.assertEqual(unsupported, [])

    def test_unsupported_helper_outside_target_closure_is_flagged(self):
        repo = Path(tempfile.mkdtemp())
        tdir = repo / "Tablet"
        tdir.mkdir()
        (tdir / "Preamble.lean").write_text("import Mathlib.X\n")
        (tdir / "Preamble.tex").write_text("\\begin{proposition}Imports.\\end{proposition}\n")
        (tdir / "orphan.lean").write_text("import Tablet.Preamble\ntheorem orphan : True := sorry\n")
        (tdir / "orphan.tex").write_text("\\begin{helper}\nTrue.\n\\end{helper}\n\\begin{proof}\nProof.\n\\end{proof}\n")
        (tdir / "main_thm.lean").write_text("import Tablet.Preamble\ntheorem main_thm : True := sorry\n")
        (tdir / "main_thm.tex").write_text("\\begin{theorem}\nTrue.\n\\end{theorem}\n\\begin{proof}\nProof.\n\\end{proof}\n")

        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "orphan": TabletNode(name="orphan", kind="helper_lemma", status="open"),
            "main_thm": TabletNode(
                name="main_thm",
                kind="ordinary",
                status="open",
                paper_provenance={"start_line": 10, "end_line": 12, "tex_label": "main"},
            ),
        })
        unsupported = find_unsupported_nodes(tablet, repo, ["main"])
        self.assertEqual(unsupported, ["orphan"])

    def test_target_pruning_is_suspended_until_every_target_is_covered(self):
        repo = Path(tempfile.mkdtemp())
        tdir = repo / "Tablet"
        tdir.mkdir()
        (tdir / "Preamble.lean").write_text("import Mathlib.X\n")
        (tdir / "Preamble.tex").write_text("\\begin{proposition}Imports.\\end{proposition}\n")
        (tdir / "main_thm.lean").write_text("import Tablet.Preamble\ntheorem main_thm : True := sorry\n")
        (tdir / "main_thm.tex").write_text("\\begin{theorem}\nTrue.\n\\end{theorem}\n\\begin{proof}\nProof.\n\\end{proof}\n")

        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(
                name="main_thm",
                kind="ordinary",
                status="open",
                paper_provenance={"start_line": 10, "end_line": 12, "tex_label": "main"},
            ),
        })
        unsupported = find_unsupported_nodes(tablet, repo, ["main", "missing_target"])
        self.assertEqual(unsupported, [])

    def test_non_target_leaf_theorem_is_not_protected_by_env(self):
        repo = Path(tempfile.mkdtemp())
        tdir = repo / "Tablet"
        tdir.mkdir()
        (tdir / "Preamble.lean").write_text("import Mathlib.X\n")
        (tdir / "main_target.lean").write_text("import Tablet.Preamble\ntheorem main_target : True := sorry\n")
        (tdir / "main_target.tex").write_text(
            "\\begin{theorem}\nTrue.\n\\end{theorem}\n\\begin{proof}\nProof.\n\\end{proof}\n"
        )
        (tdir / "leaf_thm.lean").write_text("import Tablet.Preamble\ntheorem leaf_thm : True := sorry\n")
        (tdir / "leaf_thm.tex").write_text(
            "\\begin{theorem}\nTrue.\n\\end{theorem}\n\\begin{proof}\nProof.\n\\end{proof}\n"
        )

        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_target": TabletNode(
                name="main_target",
                kind="ordinary",
                status="open",
                paper_provenance={"start_line": 1, "end_line": 3, "tex_label": "main"},
            ),
            "leaf_thm": TabletNode(name="leaf_thm", kind="ordinary", status="open"),
        })
        unsupported = find_unsupported_nodes(tablet, repo, ["main"])
        self.assertEqual(unsupported, ["leaf_thm"])

    def test_helper_cannot_cover_main_result_label(self):
        repo = Path(tempfile.mkdtemp())
        tdir = repo / "Tablet"
        tdir.mkdir()
        (tdir / "Preamble.lean").write_text("import Mathlib.X\n")
        (tdir / "main_helper.lean").write_text("import Tablet.Preamble\ntheorem main_helper : True := sorry\n")
        (tdir / "main_helper.tex").write_text(
            "\\begin{helper}\nTrue.\n\\end{helper}\n\\begin{proof}\nProof.\n\\end{proof}\n"
        )

        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_helper": TabletNode(
                name="main_helper",
                kind="ordinary",
                status="open",
                paper_provenance={"start_line": 10, "end_line": 12, "tex_label": "main"},
            ),
        })
        issues = main_result_label_issues(tablet, repo, ["main"])
        self.assertEqual(
            issues,
            [
                {
                    "label": "main",
                    "kind": "helper_forbidden",
                    "nodes": ["main_helper"],
                    "reason": "Configured main-result label `main` is attached to helper node(s): main_helper.",
                },
                {
                    "label": "main",
                    "kind": "missing",
                    "nodes": [],
                    "reason": "Configured main-result label `main` is not covered by any non-helper node.",
                },
            ],
        )

    def test_find_orphan_nodes_is_now_plain_leaf_detection(self):
        repo = Path(tempfile.mkdtemp())
        tdir = repo / "Tablet"
        tdir.mkdir()
        (tdir / "Preamble.lean").write_text("import Mathlib.X\n")
        (tdir / "Preamble.tex").write_text("\\begin{proposition}Imports.\\end{proposition}\n")
        (tdir / "helper.lean").write_text("import Tablet.Preamble\ntheorem helper : True := sorry\n")
        (tdir / "helper.tex").write_text(
            "\\begin{helper}\nTrue.\n\\end{helper}\n\\begin{proof}\nProof.\n\\end{proof}\n"
        )

        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "helper": TabletNode(name="helper", kind="ordinary", status="open"),
            "main_thm": TabletNode(name="main_thm", kind="ordinary", status="open"),
        })
        orphans = find_orphan_nodes(tablet, repo)
        self.assertEqual(orphans, ["helper", "main_thm"])


class TestMainResultTargets(unittest.TestCase):

    def test_infers_targets_from_labeled_and_unlabeled_paper_statements(self):
        repo = Path(tempfile.mkdtemp())
        paper = repo / "paper.tex"
        paper.write_text(
            "Intro\n"
            "\\begin{theorem}\\label{main}\nA.\n\\end{theorem}\n"
            "Middle\n"
            "\\begin{corollary}\nB.\n\\end{corollary}\n",
            encoding="utf-8",
        )

        targets = infer_main_result_targets_from_paper(paper)
        self.assertEqual(
            targets,
            [
                {"start_line": 2, "end_line": 4, "tex_label": "main"},
                {"start_line": 6, "end_line": 8},
            ],
        )

    def test_ignores_theorem_blocks_in_tex_preamble_macros(self):
        repo = Path(tempfile.mkdtemp())
        paper = repo / "paper.tex"
        paper.write_text(
            "\\documentclass{amsart}\n"
            "\\newenvironment{thmnum}[1]{\n"
            "  \\setcounter{thmtemp}{\\value{theorem}}\n"
            "  \\setcounter{theorem}{#1}\n"
            "  \\addtocounter{theorem}{-1}\n"
            "  \\begin{theorem}\n"
            "}{\n"
            "  \\end{theorem}\n"
            "}\n"
            "\\begin{document}\n"
            "\\begin{theorem}\\label{main}\n"
            "A.\n"
            "\\end{theorem}\n"
            "\\begin{corollary}\n"
            "B.\n"
            "\\end{corollary}\n"
            "\\end{document}\n",
            encoding="utf-8",
        )

        targets = infer_main_result_targets_from_paper(paper)
        self.assertEqual(
            targets,
            [
                {"start_line": 11, "end_line": 13, "tex_label": "main"},
                {"start_line": 14, "end_line": 16},
            ],
        )

    def test_ignores_commented_out_statements_in_document_body(self):
        repo = Path(tempfile.mkdtemp())
        paper = repo / "paper.tex"
        paper.write_text(
            "\\begin{document}\n"
            "%\\begin{theorem}\\label{ghost}\n"
            "% Ghost.\n"
            "%\\end{theorem}\n"
            "\\begin{theorem}\\label{main}\n"
            "A.\n"
            "\\end{theorem}\n"
            "\\end{document}\n",
            encoding="utf-8",
        )

        targets = infer_main_result_targets_from_paper(paper)
        self.assertEqual(
            targets,
            [
                {"start_line": 5, "end_line": 7, "tex_label": "main"},
            ],
        )

    def test_resolve_main_result_targets_enriches_labels_with_line_ranges(self):
        repo = Path(tempfile.mkdtemp())
        paper = repo / "paper.tex"
        paper.write_text(
            "\\begin{theorem}\\label{main}\nA.\n\\end{theorem}\n",
            encoding="utf-8",
        )

        targets = resolve_main_result_targets(
            paper_path=paper,
            raw_labels=["main"],
        )
        self.assertEqual(
            targets,
            [{"start_line": 1, "end_line": 3, "tex_label": "main"}],
        )

    def test_line_range_target_can_be_covered_without_tex_label(self):
        repo = Path(tempfile.mkdtemp())
        tdir = repo / "Tablet"
        tdir.mkdir()
        (tdir / "Preamble.lean").write_text("import Mathlib.X\n", encoding="utf-8")
        (tdir / "main.lean").write_text("import Tablet.Preamble\ntheorem main : True := sorry\n", encoding="utf-8")
        (tdir / "main.tex").write_text(
            "\\begin{theorem}\nTrue.\n\\end{theorem}\n\\begin{proof}\nProof.\n\\end{proof}\n",
            encoding="utf-8",
        )

        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main": TabletNode(
                name="main",
                kind="ordinary",
                status="open",
                paper_provenance={"start_line": 10, "end_line": 12},
            ),
        })

        self.assertEqual(
            main_result_target_issues(
                tablet,
                repo,
                [{"start_line": 10, "end_line": 12}],
            ),
            [],
        )


class TestPreambleDiff(unittest.TestCase):

    def test_valid_addition(self):
        old = "import Mathlib.Topology.Basic\n"
        new = "import Mathlib.Topology.Basic\nimport Mathlib.Analysis.NormedSpace\n"
        errors = validate_preamble_diff(old, new, ["Mathlib"])
        self.assertEqual(errors, [])

    def test_removal_rejected(self):
        old = "import Mathlib.Topology.Basic\nimport Mathlib.Analysis.NormedSpace\n"
        new = "import Mathlib.Topology.Basic\n"
        errors = validate_preamble_diff(old, new, ["Mathlib"])
        self.assertTrue(any("removed" in e.lower() for e in errors))

    def test_unauthorized_addition(self):
        old = "import Mathlib.Topology.Basic\n"
        new = "import Mathlib.Topology.Basic\nimport SomeOther.Module\n"
        errors = validate_preamble_diff(old, new, ["Mathlib"])
        self.assertTrue(any("disallowed" in e.lower() for e in errors))


class TestNodeNaming(unittest.TestCase):

    def test_valid_names(self):
        self.assertTrue(is_valid_node_name("compactness_of_K"))
        self.assertTrue(is_valid_node_name("thm_3_2"))
        self.assertTrue(is_valid_node_name("a"))
        self.assertTrue(is_valid_node_name("_helper"))

    def test_invalid_names(self):
        self.assertFalse(is_valid_node_name(""))
        self.assertFalse(is_valid_node_name("3abc"))
        self.assertFalse(is_valid_node_name("has spaces"))
        self.assertFalse(is_valid_node_name("has.dots"))

    def test_reserved_names(self):
        self.assertFalse(is_valid_node_name("Preamble"))
        self.assertFalse(is_valid_node_name("Axioms"))

    def test_name_conflicts(self):
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "existing": TabletNode(name="existing", kind="helper_lemma", status="open"),
        })
        self.assertEqual(find_name_conflicts(tablet, ["new_node"]), [])
        self.assertEqual(find_name_conflicts(tablet, ["existing"]), ["existing"])


class TestSupportFileGeneration(unittest.TestCase):

    def test_index_md(self):
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed", title="Imports"),
            "thm_a": TabletNode(
                name="thm_a",
                kind="ordinary",
                status="closed",
                title="Theorem A",
                paper_provenance={"start_line": 10, "end_line": 12},
            ),
            "helper": TabletNode(name="helper", kind="helper_lemma", status="open", title="Helper"),
        })
        repo = Path(tempfile.mkdtemp())
        tdir = repo / "Tablet"
        tdir.mkdir()
        (tdir / "Preamble.lean").write_text("import Mathlib.X\n")
        (tdir / "Preamble.tex").write_text("\\begin{proposition}Imports.\\end{proposition}\n")
        (tdir / "thm_a.lean").write_text("import Tablet.Preamble\ntheorem thm_a : True := sorry\n")
        (tdir / "thm_a.tex").write_text("\\begin{theorem}True\\end{theorem}\n\\begin{proof}Proof\\end{proof}\n")
        (tdir / "helper.lean").write_text("import Tablet.Preamble\ntheorem helper : True := sorry\n")
        (tdir / "helper.tex").write_text("\\begin{helper}True\\end{helper}\n\\begin{proof}Proof\\end{proof}\n")

        index = generate_index_md(tablet, repo)
        self.assertIn("thm_a", index)
        self.assertIn("helper", index)
        self.assertIn("Closed:** 1", index)
        self.assertIn("| thm_a | theorem |", index)

    def test_readme_md(self):
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "thm_a": TabletNode(
                name="thm_a",
                kind="ordinary",
                status="open",
                title="Main Theorem",
                paper_provenance={"start_line": 10, "end_line": 12, "tex_label": "thm1"},
            ),
        })
        readme = generate_readme_md(tablet)
        self.assertIn("Main Theorem", readme)
        self.assertIn("lines 10-12; label=thm1", readme)

    def test_regenerate_creates_files(self):
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
        })
        repo = Path(tempfile.mkdtemp())
        tdir = repo / "Tablet"
        tdir.mkdir()
        (tdir / "Preamble.lean").write_text("import Mathlib.X\n")

        regenerate_support_files(tablet, repo)
        self.assertTrue((tdir / "INDEX.md").exists())
        self.assertTrue((tdir / "README.md").exists())
        self.assertTrue((tdir / "header.tex").exists())


if __name__ == "__main__":
    unittest.main()
