import unittest
import tempfile
from pathlib import Path

from scripts.mkdocs_hooks import CSP_META, on_post_build, on_post_page


class MkDocsHookTests(unittest.TestCase):
    def test_injects_csp_first_in_head(self) -> None:
        output = "<!doctype html><html><head><title>Page</title></head><body></body></html>"

        hardened = on_post_page(output, page=None, config=None)

        self.assertIn(f"<head>{CSP_META}<title>", hardened)

    def test_rejects_missing_or_duplicate_head(self) -> None:
        for output in ("<html></html>", "<head></head><head></head>"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                on_post_page(output, page=None, config=None)

    def test_post_build_hardens_generated_404_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory)
            regular = site / "index.html"
            not_found = site / "404.html"
            regular.write_text(f"<html><head>{CSP_META}</head></html>", encoding="utf-8")
            not_found.write_text("<html><head><title>404</title></head></html>", encoding="utf-8")

            on_post_build({"site_dir": str(site)})

            self.assertEqual(regular.read_text(encoding="utf-8").count(CSP_META), 1)
            self.assertIn(f"<head>{CSP_META}<title>", not_found.read_text(encoding="utf-8"))

    def test_post_build_rejects_misplaced_csp(self) -> None:
        unsafe_documents = (
            f"<html><head></head><body>{CSP_META}</body></html>",
            f"<html><head><script src='/early.js'></script>{CSP_META}</head></html>",
            f"<html><head></head><body><head>{CSP_META}</head></body></html>",
            f"<html><head></head><body><template><head>{CSP_META}</head></template></body></html>",
        )
        for index, content in enumerate(unsafe_documents):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                site = Path(directory)
                (site / f"unsafe-{index}.html").write_text(content, encoding="utf-8")

                with self.assertRaisesRegex(ValueError, "first element in <head>"):
                    on_post_build({"site_dir": str(site)})


if __name__ == "__main__":
    unittest.main()
