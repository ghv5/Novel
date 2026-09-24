import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.mkdocs_hooks import CSP_POLICY
from scripts.validate_built_site import SiteValidationError, validate_site


REPOSITORY = Path(__file__).resolve().parents[1]


class MkDocsConfigurationTests(unittest.TestCase):
    def test_external_theme_fonts_are_disabled(self) -> None:
        config = yaml.safe_load((REPOSITORY / "mkdocs.yml").read_text(encoding="utf-8"))

        self.assertIs(config["theme"]["font"], False)


class WorkflowConfigurationTests(unittest.TestCase):
    def test_build_runs_for_every_pull_request(self) -> None:
        workflow = yaml.load(
            (REPOSITORY / ".github/workflows/publish-docs.yml").read_text(encoding="utf-8"),
            Loader=yaml.BaseLoader,
        )

        pull_request = workflow["on"]["pull_request"] or {}
        self.assertNotIn("paths", pull_request)


class BuiltSiteValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.site = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write(self, relative: str, content: str) -> None:
        path = self.site / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def write_html(self, relative: str, content: str) -> None:
        meta = f'<meta http-equiv="Content-Security-Policy" content="{CSP_POLICY}">'
        self.write(relative, f"<html><head>{meta}</head><body>{content}</body></html>")

    def test_allows_local_resources_same_origin_canonical_and_external_links(self) -> None:
        self.write_html(
            "index.html",
            '<link rel="canonical" href="https://docs.gmy.zone/novel/">'
            '<link rel="stylesheet" href="assets/site.css">'
            '<img src="assets/logo.png">'
            '<a href="https://example.com/">Reference</a>',
        )
        self.write(
            "assets/site.css",
            """/* https://license.example/ */ body {
  background: url("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg'></svg>");
}
""",
        )

        validate_site(self.site, "https://docs.gmy.zone")

    def test_rejects_external_html_autoloads(self) -> None:
        unsafe_documents = (
            '<link rel="preconnect" href="https://fonts.example.com">',
            '<link rel="stylesheet" href="https://cdn.example.com/site.css">',
            '<img src="https://cdn.example.com/pixel.png">',
            r'<img src="\\cdn.example.com/pixel.png">',
            '<img srcset="local.png 1x, https://cdn.example.com/pixel.png 2x">',
            '<table background="//cdn.example.com/pixel.png"></table>',
            '<object data="https://cdn.example.com/document.pdf"></object>',
            '<meta http-equiv="refresh" content="0; url=https://cdn.example.com/">',
            r'<meta http-equiv="refresh" content="0; url=\\cdn.example.com/">',
            '<style>body { background: url("https://cdn.example.com/pixel.png"); }</style>',
        )
        for index, content in enumerate(unsafe_documents):
            with self.subTest(content=content):
                self.write_html(f"unsafe-{index}.html", content)
                with self.assertRaisesRegex(SiteValidationError, "external resource"):
                    validate_site(self.site, "https://docs.gmy.zone")
                (self.site / f"unsafe-{index}.html").unlink()

    def test_rejects_external_css_resources(self) -> None:
        unsafe_stylesheets = (
            'body { background: url("https://cdn.example.com/pixel.png"); }',
            'body { background: image-set("//cdn.example.com/pixel.png" 1x); }',
            r"body { background: \75\72\6c(https\3a\2f\2f cdn.example.com/pixel.png); }",
            r'body { background: \69mage-set("https:\2f\2f cdn.example.com/pixel.png" 1x); }',
            r"body { background: url(\\\\cdn.example.com/pixel.png); }",
            '.x { --open: "/*"; background: url("https://cdn.example.com/pixel.png"); --close: "*/"; }',
            r'.x { --open: \2f\2a; background: url("https://cdn.example.com/pixel.png"); --close: \2a\2f; }',
        )
        for index, content in enumerate(unsafe_stylesheets):
            with self.subTest(content=content):
                self.write(f"unsafe-{index}.css", content)
                with self.assertRaisesRegex(SiteValidationError, "external resource"):
                    validate_site(self.site, "https://docs.gmy.zone")
                (self.site / f"unsafe-{index}.css").unlink()

    def test_rejects_unclosed_style_and_comma_refresh(self) -> None:
        unsafe_documents = (
            '<style>body { background: url("https://cdn.example.com/pixel.png"); }',
            '<meta http-equiv="refresh" content="0, url=https://cdn.example.com/">',
        )
        for index, content in enumerate(unsafe_documents):
            with self.subTest(content=content):
                self.write_html(f"incomplete-{index}.html", content)
                with self.assertRaisesRegex(SiteValidationError, "external resource"):
                    validate_site(self.site, "https://docs.gmy.zone")
                (self.site / f"incomplete-{index}.html").unlink()

    def test_requires_exact_content_security_policy(self) -> None:
        unsafe_documents = (
            "<html><head></head><body>Missing CSP</body></html>",
            '<html><head><meta http-equiv="Content-Security-Policy" '
            'content="default-src *"></head><body>Weak CSP</body></html>',
        )
        for index, content in enumerate(unsafe_documents):
            with self.subTest(content=content):
                self.write(f"csp-{index}.html", content)
                with self.assertRaisesRegex(SiteValidationError, "Content-Security-Policy"):
                    validate_site(self.site, "https://docs.gmy.zone")
                (self.site / f"csp-{index}.html").unlink()

    def test_requires_csp_as_first_element_in_head(self) -> None:
        meta = f'<meta http-equiv="Content-Security-Policy" content="{CSP_POLICY}">'
        unsafe_documents = (
            f"<html><head></head><body>{meta}</body></html>",
            f"<html><head><script src='/early.js'></script>{meta}</head></html>",
            f"<html><head></head><body><head>{meta}</head></body></html>",
            f"<html><head></head><body><template><head>{meta}</head></template></body></html>",
        )
        for index, content in enumerate(unsafe_documents):
            with self.subTest(content=content):
                self.write(f"csp-position-{index}.html", content)
                with self.assertRaisesRegex(SiteValidationError, "first element in <head>"):
                    validate_site(self.site, "https://docs.gmy.zone")
                (self.site / f"csp-position-{index}.html").unlink()

    def test_rejects_symlinks_and_invalid_utf8(self) -> None:
        target = self.site / "outside.html"
        target.write_text("safe", encoding="utf-8")
        (self.site / "linked.html").symlink_to(target)
        with self.assertRaisesRegex(SiteValidationError, "symbolic link"):
            validate_site(self.site, "https://docs.gmy.zone")
        (self.site / "linked.html").unlink()
        target.write_bytes(b"\xff")
        with self.assertRaisesRegex(SiteValidationError, "UTF-8"):
            validate_site(self.site, "https://docs.gmy.zone")


if __name__ == "__main__":
    unittest.main()
