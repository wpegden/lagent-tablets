from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lagent_tablets.prompt_reference_web import publish_prompt_reference_web


class TestPromptReferenceWeb(unittest.TestCase):
    def test_publish_prompt_reference_web_writes_site_and_alias(self) -> None:
        root = Path(tempfile.mkdtemp())
        static_root = root / "static"
        prompt_dir = root / "prompt_catalog"
        action_dir = root / "prompt_action_catalog"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        action_dir.mkdir(parents=True, exist_ok=True)
        (prompt_dir / "README.md").write_text("# Prompt Catalog\n", encoding="utf-8")
        (prompt_dir / "alpha.md").write_text("# Alpha\nbody\n", encoding="utf-8")
        (action_dir / "README.md").write_text("# Prompt Action Catalog\n", encoding="utf-8")
        (action_dir / "beta.md").write_text("# Beta\nactions\n", encoding="utf-8")

        site_root = publish_prompt_reference_web(
            static_root=static_root,
            prompt_catalog_dir=prompt_dir,
            prompt_action_catalog_dir=action_dir,
            route_name="prompt-reference",
            alias_projects=["extremal"],
        )

        self.assertTrue((site_root / "index.html").exists())
        self.assertTrue((site_root / "prompt-catalog" / "alpha.html").exists())
        self.assertTrue((site_root / "prompt-action-catalog" / "beta.html").exists())
        self.assertEqual(
            (site_root / "raw" / "prompt-catalog" / "alpha.md").read_text(encoding="utf-8"),
            "# Alpha\nbody\n",
        )
        alias = static_root / "extremal" / "prompt-reference"
        self.assertTrue(alias.is_symlink())
        self.assertEqual(alias.resolve(), site_root.resolve())
