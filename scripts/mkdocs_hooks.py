"""MkDocs hooks that enforce the site's browser security boundary."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from typing import Mapping


CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)
CSP_META = f'<meta http-equiv="Content-Security-Policy" content="{CSP_POLICY}">'


class _CSPPositionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.body_seen = False
        self.csp_count = 0
        self.head_count = 0
        self.head_element_count = 0
        self.in_head = False
        self.pre_head_element = False

    @staticmethod
    def _is_csp(tag: str, attrs: list[tuple[str, str | None]]) -> tuple[bool, str | None]:
        values = {name.lower(): value for name, value in attrs if value is not None}
        is_csp = tag == "meta" and values.get("http-equiv", "").strip().lower() == (
            "content-security-policy"
        )
        return is_csp, values.get("content")

    def _start(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        if normalized_tag == "html":
            return
        if normalized_tag == "head":
            if self.head_count or self.body_seen or self.pre_head_element:
                raise ValueError("Content-Security-Policy must be the first element in <head>")
            self.head_count = 1
            self.in_head = True
            return
        if normalized_tag == "body":
            self.body_seen = True
            self.in_head = False
            return

        is_csp, content = self._is_csp(normalized_tag, attrs)
        if is_csp:
            self.csp_count += 1
            if (
                not self.in_head
                or self.head_element_count
                or self.csp_count != 1
                or content != CSP_POLICY
            ):
                raise ValueError("Content-Security-Policy must be the first element in <head>")
        if self.in_head:
            self.head_element_count += 1
        elif not self.head_count:
            self.pre_head_element = True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "head":
            self.in_head = False

    def require_valid(self) -> None:
        if self.head_count != 1 or self.csp_count != 1:
            raise ValueError("Content-Security-Policy must be the first element in <head>")


def validate_csp_first_in_head(output: str) -> None:
    """Require one exact CSP as the first element of the document's main head."""
    parser = _CSPPositionParser()
    parser.feed(output)
    parser.close()
    parser.require_valid()


def on_post_page(output: str, page: object, config: object, **kwargs: object) -> str:
    """Place CSP before any page resource can load."""
    if output.count("<head>") != 1:
        raise ValueError("generated page must contain exactly one <head> element")
    hardened = output.replace("<head>", f"<head>{CSP_META}", 1)
    validate_csp_first_in_head(hardened)
    return hardened


def on_post_build(config: Mapping[str, object], **kwargs: object) -> None:
    """Harden theme-generated HTML, including pages outside on_post_page."""
    site_value = config.get("site_dir")
    if not isinstance(site_value, (str, Path)):
        raise ValueError("MkDocs site_dir is unavailable")
    site = Path(site_value).resolve(strict=True)
    for path in sorted(site.rglob("*.html")):
        if path.is_symlink():
            raise ValueError(f"generated HTML must not be a symbolic link: {path.relative_to(site)}")
        output = path.read_text(encoding="utf-8")
        if CSP_META in output:
            validate_csp_first_in_head(output)
            continue
        path.write_text(on_post_page(output, page=None, config=config), encoding="utf-8")
