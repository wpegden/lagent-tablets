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
    generate_header_tex,
    generate_index_md,
    generate_node_lean,
    generate_readme_md,
    has_sorry,
    is_valid_node_name,
    mask_comments_and_strings,
    node_lean_path,
    node_tex_path,
    regenerate_support_files,
    register_new_node,
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


class TestOrphanDetection(unittest.TestCase):

    def test_no_orphans(self):
        repo = Path(tempfile.mkdtemp())
        tdir = repo / "Tablet"
        tdir.mkdir()
        (tdir / "Preamble.lean").write_text("import Mathlib.X\n")
        (tdir / "helper.lean").write_text("import Tablet.Preamble\ntheorem helper : True := sorry\n")
        (tdir / "main_thm.lean").write_text("import Tablet.helper\ntheorem main_thm : True := sorry\n")

        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "helper": TabletNode(name="helper", kind="helper_lemma", status="open"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open"),
        })
        orphans = find_orphan_nodes(tablet, repo)
        self.assertEqual(orphans, [])

    def test_orphan_detected(self):
        repo = Path(tempfile.mkdtemp())
        tdir = repo / "Tablet"
        tdir.mkdir()
        (tdir / "Preamble.lean").write_text("import Mathlib.X\n")
        (tdir / "orphan.lean").write_text("import Tablet.Preamble\ntheorem orphan : True := sorry\n")
        (tdir / "main_thm.lean").write_text("import Tablet.Preamble\ntheorem main_thm : True := sorry\n")

        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "orphan": TabletNode(name="orphan", kind="helper_lemma", status="open"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open"),
        })
        orphans = find_orphan_nodes(tablet, repo)
        self.assertEqual(orphans, ["orphan"])

    def test_paper_main_result_not_orphan(self):
        repo = Path(tempfile.mkdtemp())
        tdir = repo / "Tablet"
        tdir.mkdir()
        (tdir / "Preamble.lean").write_text("import Mathlib.X\n")
        (tdir / "main_thm.lean").write_text("import Tablet.Preamble\ntheorem main_thm : True := sorry\n")

        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "main_thm": TabletNode(name="main_thm", kind="paper_main_result", status="open"),
        })
        orphans = find_orphan_nodes(tablet, repo)
        self.assertEqual(orphans, [])


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
            "thm_a": TabletNode(name="thm_a", kind="paper_main_result", status="closed", title="Theorem A"),
            "helper": TabletNode(name="helper", kind="helper_lemma", status="open", title="Helper"),
        })
        repo = Path(tempfile.mkdtemp())
        tdir = repo / "Tablet"
        tdir.mkdir()
        (tdir / "Preamble.lean").write_text("import Mathlib.X\n")
        (tdir / "thm_a.lean").write_text("import Tablet.Preamble\ntheorem thm_a : True := sorry\n")
        (tdir / "helper.lean").write_text("import Tablet.Preamble\ntheorem helper : True := sorry\n")

        index = generate_index_md(tablet, repo)
        self.assertIn("thm_a", index)
        self.assertIn("helper", index)
        self.assertIn("Closed:** 1", index)

    def test_readme_md(self):
        tablet = TabletState(nodes={
            "Preamble": TabletNode(name="Preamble", kind="preamble", status="closed"),
            "thm_a": TabletNode(name="thm_a", kind="paper_main_result", status="open", title="Main Theorem", paper_provenance="Thm 1.1"),
        })
        readme = generate_readme_md(tablet)
        self.assertIn("Main Theorem", readme)
        self.assertIn("Thm 1.1", readme)

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
