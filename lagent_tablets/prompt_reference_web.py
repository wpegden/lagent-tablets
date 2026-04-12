from __future__ import annotations

import html
import shutil
from pathlib import Path
from typing import Iterable, Sequence


def _page_html(*, title: str, body_html: str) -> str:
    escaped_title = html.escape(title)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f7f2;
      --panel: #ffffff;
      --ink: #171717;
      --muted: #5e5e5e;
      --border: #d7d2c8;
      --accent: #1c5c8a;
    }}
    body {{
      margin: 0;
      padding: 24px;
      background: var(--bg);
      color: var(--ink);
      font: 16px/1.45 Georgia, "Iowan Old Style", "Palatino Linotype", serif;
    }}
    main {{
      max-width: 1100px;
      margin: 0 auto;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 24px;
      box-shadow: 0 10px 30px rgba(0,0,0,0.04);
    }}
    h1, h2 {{
      margin-top: 0;
      font-family: "Helvetica Neue", Arial, sans-serif;
    }}
    a {{
      color: var(--accent);
      text-decoration: none;
    }}
    a:hover {{
      text-decoration: underline;
    }}
    .meta {{
      color: var(--muted);
      margin-bottom: 20px;
      font-family: "Helvetica Neue", Arial, sans-serif;
    }}
    .grid {{
      display: grid;
      gap: 24px;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    }}
    .card {{
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 16px;
      background: #fcfcfa;
    }}
    ul {{
      padding-left: 20px;
    }}
    li + li {{
      margin-top: 8px;
    }}
    .actions {{
      margin-bottom: 16px;
      font-family: "Helvetica Neue", Arial, sans-serif;
    }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      background: #f5f3ee;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
      overflow-x: auto;
      font: 14px/1.4 Menlo, Consolas, monospace;
    }}
  </style>
</head>
<body>
  <main>
    {body_html}
  </main>
</body>
</html>
"""


def _render_file_page(*, title: str, relative_index: str, raw_rel: str, content: str) -> str:
    body = (
        f'<div class="actions"><a href="{html.escape(relative_index)}">Back to index</a>'
        f' | <a href="{html.escape(raw_rel)}">Open raw Markdown</a></div>'
        f"<h1>{html.escape(title)}</h1>"
        f"<pre>{html.escape(content)}</pre>"
    )
    return _page_html(title=title, body_html=body)


def _render_index(
    *,
    prompt_files: Sequence[Path],
    action_files: Sequence[Path],
    route_name: str,
) -> str:
    def section(title: str, url_prefix: str, files: Sequence[Path]) -> str:
        items = []
        for path in files:
            stem = path.stem
            label = html.escape(path.name)
            items.append(
                f'<li><a href="{url_prefix}/{html.escape(stem)}.html">{label}</a> '
                f'(<a href="raw/{url_prefix}/{label}">raw</a>)</li>'
            )
        return (
            f'<section class="card"><h2>{html.escape(title)}</h2>'
            f"<ul>{''.join(items)}</ul></section>"
        )

    body = (
        "<h1>Prompt Reference</h1>"
        f'<div class="meta">Generated prompt and action catalogs for web browsing. Route: '
        f'<code>/lagent-tablets/{html.escape(route_name)}/</code></div>'
        '<div class="grid">'
        f'{section("Prompt Catalog", "prompt-catalog", prompt_files)}'
        f'{section("Prompt Action Catalog", "prompt-action-catalog", action_files)}'
        "</div>"
    )
    return _page_html(title="Prompt Reference", body_html=body)


def _write_catalog_pages(
    *,
    source_files: Sequence[Path],
    site_subdir: Path,
    raw_subdir: Path,
) -> None:
    shutil.rmtree(site_subdir, ignore_errors=True)
    shutil.rmtree(raw_subdir, ignore_errors=True)
    site_subdir.mkdir(parents=True, exist_ok=True)
    raw_subdir.mkdir(parents=True, exist_ok=True)

    for md_path in source_files:
        raw_target = raw_subdir / md_path.name
        shutil.copyfile(md_path, raw_target)
        html_target = site_subdir / f"{md_path.stem}.html"
        html_target.write_text(
            _render_file_page(
                title=md_path.name,
                relative_index="../index.html",
                raw_rel=f"../raw/{raw_subdir.name}/{md_path.name}",
                content=md_path.read_text(encoding="utf-8"),
            ),
            encoding="utf-8",
        )


def publish_prompt_reference_web(
    *,
    static_root: Path,
    prompt_catalog_dir: Path,
    prompt_action_catalog_dir: Path,
    route_name: str = "prompt-reference",
    alias_projects: Iterable[str] = (),
) -> Path:
    site_root = static_root / route_name
    prompt_files = sorted(prompt_catalog_dir.glob("*.md"))
    action_files = sorted(prompt_action_catalog_dir.glob("*.md"))

    prompt_site = site_root / "prompt-catalog"
    action_site = site_root / "prompt-action-catalog"
    raw_prompt = site_root / "raw" / "prompt-catalog"
    raw_action = site_root / "raw" / "prompt-action-catalog"

    _write_catalog_pages(source_files=prompt_files, site_subdir=prompt_site, raw_subdir=raw_prompt)
    _write_catalog_pages(source_files=action_files, site_subdir=action_site, raw_subdir=raw_action)
    site_root.mkdir(parents=True, exist_ok=True)
    (site_root / "index.html").write_text(
        _render_index(
            prompt_files=prompt_files,
            action_files=action_files,
            route_name=route_name,
        ),
        encoding="utf-8",
    )

    for slug in alias_projects:
        project_dir = static_root / slug
        project_dir.mkdir(parents=True, exist_ok=True)
        alias_path = project_dir / route_name
        if alias_path.exists() or alias_path.is_symlink():
            alias_path.unlink()
        alias_path.symlink_to(site_root, target_is_directory=True)

    return site_root
