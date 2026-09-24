import os
import tempfile
import unittest
from pathlib import Path, PurePosixPath

from scripts.build_public_docs import (
    ManifestError,
    Rule,
    match_glob,
    parse_rules,
    select_files,
    stage_files,
)


class ParseRulesTests(unittest.TestCase):
    def test_parses_comments_globs_and_exclusions(self) -> None:
        rules = parse_rules("# public docs\ndocs/**/*.md\n!docs/private/**\n")

        self.assertEqual(
            rules,
            (
                Rule(pattern="docs/**/*.md", excluded=False, line=2),
                Rule(pattern="docs/private/**", excluded=True, line=3),
            ),
        )

    def test_rejects_unsafe_patterns(self) -> None:
        unsafe = ("../secret.md", "/etc/passwd", "~/file.md", "C:\\file.md", "docs\\*.md", "!")

        for pattern in unsafe:
            with self.subTest(pattern=pattern), self.assertRaises(ManifestError):
                parse_rules(pattern)

    def test_requires_a_positive_rule(self) -> None:
        with self.assertRaisesRegex(ManifestError, "positive"):
            parse_rules("# comment only\n!docs/private/**\n")


class GlobTests(unittest.TestCase):
    def test_double_star_matches_zero_or_more_directories(self) -> None:
        pattern = "docs/**/*.md"

        self.assertTrue(match_glob("docs/index.md", pattern))
        self.assertTrue(match_glob("docs/topic/index.md", pattern))
        self.assertTrue(match_glob("docs/中文 资料/示例.md", pattern))
        self.assertFalse(match_glob("docs/topic/index.txt", pattern))

    def test_single_star_does_not_cross_directories(self) -> None:
        self.assertTrue(match_glob("docs/index.md", "docs/*.md"))
        self.assertFalse(match_glob("docs/topic/index.md", "docs/*.md"))


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write(self, relative: str, content: str = "# Test\n") -> Path:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_selects_sorted_unique_files_and_applies_exclusions(self) -> None:
        self.write("docs/index.md")
        self.write("docs/中文 资料/a b.md")
        self.write("docs/private/secret.md")
        self.write("docs/not-selected.txt")
        rules = parse_rules("docs/**/*.md\ndocs/index.md\n!docs/private/**\n")

        selected = select_files(self.repo, rules)

        self.assertEqual(
            selected,
            (PurePosixPath("docs/index.md"), PurePosixPath("docs/中文 资料/a b.md")),
        )

    def test_fails_when_a_positive_rule_matches_nothing(self) -> None:
        self.write("docs/index.md")

        with self.assertRaisesRegex(ManifestError, "line 2"):
            select_files(self.repo, parse_rules("docs/**/*.md\ndocs/missing/*.md"))

    def test_fails_when_everything_is_excluded(self) -> None:
        self.write("docs/index.md")

        with self.assertRaisesRegex(ManifestError, "no publishable"):
            select_files(self.repo, parse_rules("docs/**/*.md\n!docs/**"))

    def test_rejects_sensitive_files_selected_by_a_wide_glob(self) -> None:
        self.write("docs/index.md")
        self.write("docs/SECRET.PEM", "private")

        with self.assertRaisesRegex(ManifestError, "sensitive"):
            select_files(self.repo, parse_rules("docs/**"))

    def test_rejects_additional_sensitive_file_names(self) -> None:
        self.write("docs/index.md")
        self.write("docs/credentials.json", "{}")

        with self.assertRaisesRegex(ManifestError, "sensitive"):
            select_files(self.repo, parse_rules("docs/**"))

    def test_rejects_high_confidence_secret_content(self) -> None:
        fake_token = "".join(("ghp_", "ABCDEFGHIJKL", "MNOPQRSTUVWX", "YZ1234567890"))
        self.write("docs/index.md", f"token = {fake_token}\n")

        with self.assertRaisesRegex(ManifestError, "GitHub token"):
            select_files(self.repo, parse_rules("docs/**/*.md"))

    def test_rejects_secret_keyword_from_maintained_scanner(self) -> None:
        fake_value = "".join(("super-secret-", "value-123456789"))
        self.write("docs/index.md", f'password = "{fake_value}"\n')

        with self.assertRaisesRegex(ManifestError, "Secret Keyword"):
            select_files(self.repo, parse_rules("docs/**/*.md"))

    def test_rejects_secret_scanner_allowlist_pragma(self) -> None:
        self.write(
            "docs/index.md",
            'password = "super-secret-value-123456789" <!-- pragma: allowlist secret -->\n',
        )

        with self.assertRaisesRegex(ManifestError, "allowlist pragmas"):
            select_files(self.repo, parse_rules("docs/**/*.md"))

    def test_rejects_active_html_and_script_urls(self) -> None:
        unsafe_documents = (
            "<script>alert(1)</script>",
            '<img src="x" onerror="alert(1)">',
            "[click](javascript:alert(1))",
            '[click](#){: onclick="alert(1)" }',
            '<iframe src="https://example.com"></iframe>',
            "![pixel](https://evil.example/pixel.png)",
            '<img src="//evil.example/pixel.png">',
            r'<img src="\\evil.example/pixel.png">',
            '<table background="https://evil.example/pixel.png"><tr><td>x</td></tr></table>',
            '<pre class="mermaid">graph TD;A--&gt;B</pre>',
        )
        for index, content in enumerate(unsafe_documents):
            with self.subTest(content=content):
                self.write(f"docs/unsafe-{index}.md", content)
                with self.assertRaisesRegex(ManifestError, "active content"):
                    select_files(self.repo, parse_rules(f"docs/unsafe-{index}.md"))

    def test_allows_active_content_examples_inside_code_fences(self) -> None:
        self.write("docs/index.md", "```html\n<script>alert(1)</script>\n```\n")

        selected = select_files(self.repo, parse_rules("docs/**/*.md"))

        self.assertEqual(selected, (PurePosixPath("docs/index.md"),))

    def test_allows_external_links_that_do_not_load_automatically(self) -> None:
        self.write("docs/index.md", "[官方网站](https://example.com/)\n")

        selected = select_files(self.repo, parse_rules("docs/**/*.md"))

        self.assertEqual(selected, (PurePosixPath("docs/index.md"),))

    def test_rejects_active_css_constructs(self) -> None:
        unsafe_stylesheets = (
            '@import url("https://example.com/tracker.css");\n',
            'background: image-set("https://example.com/tracker.png" 1x);\n',
            'background: -webkit-image-set("https://example.com/tracker.png" 1x);\n',
            r"background: \75\72\6c(https\3a\2f\2f evil.example/pixel.png);",
            r'background: \69mage-set("https:\2f\2f evil.example/pixel.png" 1x);',
        )
        for index, content in enumerate(unsafe_stylesheets):
            with self.subTest(content=content):
                self.write(f"docs/unsafe-{index}.css", content)
                with self.assertRaisesRegex(ManifestError, "active CSS"):
                    select_files(self.repo, parse_rules(f"docs/unsafe-{index}.css"))

    def test_rejects_a_selected_symlink(self) -> None:
        target = self.write("target.md")
        link = self.repo / "docs" / "linked.md"
        link.parent.mkdir(parents=True)
        os.symlink(target, link)

        with self.assertRaisesRegex(ManifestError, "symbolic link"):
            select_files(self.repo, parse_rules("docs/**/*.md"))

    def test_stages_files_relative_to_docs_and_generates_an_index(self) -> None:
        self.write("docs/topic/guide.md", "# Guide\n")
        selected = select_files(self.repo, parse_rules("docs/**/*.md"))
        destination = self.repo / ".publish" / "docs"

        staged = stage_files(self.repo, destination, selected, strip_prefix=PurePosixPath("docs"))

        self.assertEqual(staged, (PurePosixPath("topic/guide.md"),))
        self.assertEqual((destination / "topic/guide.md").read_text(encoding="utf-8"), "# Guide\n")
        self.assertTrue((destination / "index.md").is_file())

    def test_generated_index_escapes_hostile_file_names(self) -> None:
        self.write("docs/foo](javascript:alert(1)).md", "# Safe body\n")
        selected = select_files(self.repo, parse_rules("docs/**/*.md"))
        destination = self.repo / ".publish" / "docs"

        stage_files(self.repo, destination, selected, strip_prefix=PurePosixPath("docs"))

        index = (destination / "index.md").read_text(encoding="utf-8")
        self.assertNotIn("](javascript:", index)
        self.assertIn("javascript%3Aalert%281%29", index)

    def test_stage_rechecks_for_symlink_replacement(self) -> None:
        source = self.write("docs/index.md")
        selected = select_files(self.repo, parse_rules("docs/**/*.md"))
        source.unlink()
        os.symlink(self.write("replacement.md"), source)

        with self.assertRaisesRegex(ManifestError, "symbolic link"):
            stage_files(
                self.repo,
                self.repo / ".publish" / "docs",
                selected,
                strip_prefix=PurePosixPath("docs"),
            )


if __name__ == "__main__":
    unittest.main()
