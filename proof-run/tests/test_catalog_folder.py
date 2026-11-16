"""Keep the catalog-installed folder self-contained."""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PLUGIN_ROOT.parent
INLINE_LINK = re.compile(r"\]\(\s*(<[^>]+>|[^)\s]+)")
REFERENCE_LINK = re.compile(r"(?m)^\s*\[[^\]]+\]:\s*(<[^>\s]+>|[^\s]+)")


def markdown_targets(markdown: str) -> list[str]:
    targets = [match.group(1) for match in INLINE_LINK.finditer(markdown)]
    targets.extend(match.group(1) for match in REFERENCE_LINK.finditer(markdown))
    return [target[1:-1] if target.startswith("<") and target.endswith(">") else target for target in targets]


class CatalogFolderTests(unittest.TestCase):
    def test_markdown_links_stay_inside_the_catalog_folder(self):
        root = PLUGIN_ROOT.resolve()
        problems = []
        for markdown_file in sorted(PLUGIN_ROOT.rglob("*.md")):
            for target in markdown_targets(markdown_file.read_text(encoding="utf-8")):
                if target.startswith(("http://", "https://", "mailto:")) or target.startswith("#"):
                    continue
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc:
                    continue
                relative_target = unquote(parsed.path)
                resolved = (markdown_file.parent / relative_target).resolve()
                try:
                    resolved.relative_to(root)
                except ValueError:
                    problems.append(f"{markdown_file.relative_to(PLUGIN_ROOT)}: escapes folder: {target}")
                    continue
                if not resolved.exists():
                    problems.append(f"{markdown_file.relative_to(PLUGIN_ROOT)}: missing target: {target}")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_plugin_license_matches_repository_license(self):
        plugin_license = PLUGIN_ROOT / "LICENSE"
        repository_license = REPOSITORY_ROOT / "LICENSE"
        self.assertTrue(plugin_license.is_file(), "proof-run/LICENSE is missing")
        self.assertEqual(plugin_license.read_bytes(), repository_license.read_bytes())

    def test_manifest_matches_readme_and_declares_no_tools(self):
        manifest = (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
        readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        entry_match = re.search(r"(?m)^entry:\s*(\S+)\s*$", manifest)
        version_match = re.search(r"(?m)^version:\s*[\"']?([^\"'#\s]+)[\"']?\s*$", manifest)
        version_line = next(
            (line.strip() for line in readme.splitlines() if re.fullmatch(r"v\d+\.\d+\.\d+\.?", line.strip())),
            None,
        )
        if entry_match is None:
            self.fail("manifest entry is missing")
        self.assertEqual(entry_match.group(1), "__init__.py")
        if version_match is None:
            self.fail("manifest version is missing")
        if version_line is None:
            self.fail("README's first version line is missing")
        self.assertEqual(version_match.group(1), version_line.removeprefix("v").removesuffix("."))
        self.assertIsNone(re.search(r"(?m)^\s*provides_tools\s*:", manifest))

    def test_readme_opening_has_example_and_verification_evidence(self):
        readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        opening = "\n".join(readme.splitlines()[:20])
        self.assertIn("/proof start deploy", opening)
        self.assertIn("The suite has 187 tests", opening)
        self.assertIn("host-verification-2026-11-16.json", opening)

    def test_readme_documents_interrupted_run_cancel_and_no_live_test_claim(self):
        readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("interrupted", readme.lower())
        self.assertIn("/proof cancel", readme)
        positive_live_test_claim = re.compile(
            r"\b(?:live\s+)?(?:telegram|gateway)\s+(?:test|verification)\s+"
            r"(?:passed|succeeded|successful|confirmed)\b|"
            r"\b(?:passed|succeeded|verified|tested|confirmed)\b[^.\n]{0,60}"
            r"\b(?:live\s+)?(?:telegram|gateway)\b",
            re.IGNORECASE,
        )
        self.assertIsNone(positive_live_test_claim.search(readme))


if __name__ == "__main__":
    unittest.main()
