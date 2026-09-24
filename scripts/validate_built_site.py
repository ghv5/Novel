#!/usr/bin/env python3
"""Reject third-party resources in a generated documentation site."""

from __future__ import annotations

import argparse
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

import tinycss2

if __package__:
    from .mkdocs_hooks import CSP_POLICY, validate_csp_first_in_head
else:
    from mkdocs_hooks import CSP_POLICY, validate_csp_first_in_head


class SiteValidationError(ValueError):
    """Raised when generated output could load an untrusted resource."""


AUTOLOAD_ATTRIBUTES = frozenset({"background", "poster", "src", "xlink:href"})
EXTERNAL_CANDIDATE = re.compile(r"(?:https?:)?//[^\s'\"()<>]+", re.IGNORECASE)
RESOURCE_STRING_FUNCTIONS = frozenset(
    {"cross-fade", "image", "image-set", "-webkit-image-set", "src", "url"}
)


def _origin(value: str) -> tuple[str, str]:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise SiteValidationError("allowed origin must be an HTTPS origin")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise SiteValidationError("allowed origin must not include a path, query, or fragment")
    return parsed.scheme.lower(), parsed.netloc.lower()


def _assert_resource_url(value: str, allowed_origin: tuple[str, str], location: str) -> None:
    normalized = re.sub(r"[\x00-\x20]+", "", value)
    if "\\" in normalized:
        raise SiteValidationError(f"external resource uses an unsafe URL separator in {location}")
    if not normalized or normalized.startswith(("#", "/")) and not normalized.startswith("//"):
        return
    parsed = urlsplit(normalized)
    if normalized.lower().startswith("data:image/"):
        return
    if normalized.startswith("//"):
        candidate = (allowed_origin[0], parsed.netloc.lower())
    elif parsed.scheme in {"http", "https"}:
        candidate = (parsed.scheme.lower(), parsed.netloc.lower())
    elif not parsed.scheme and not parsed.netloc:
        return
    else:
        raise SiteValidationError(f"external resource uses an unsafe scheme in {location}")
    if candidate != allowed_origin:
        raise SiteValidationError(f"external resource is not allowed in {location}")


class _GeneratedHTMLValidator(HTMLParser):
    def __init__(self, allowed_origin: tuple[str, str], location: str) -> None:
        super().__init__(convert_charrefs=True)
        self.allowed_origin = allowed_origin
        self.location = location
        self.style_chunks: list[str] | None = None
        self.csp_policy: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._inspect(tag, attrs)
        if tag.lower() == "style":
            self.style_chunks = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._inspect(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self.style_chunks is not None:
            self.style_chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "style" and self.style_chunks is not None:
            _validate_css("".join(self.style_chunks), self.allowed_origin, f"{self.location} style")
            self.style_chunks = None

    def close(self) -> None:
        super().close()
        if self.style_chunks is not None:
            _validate_css("".join(self.style_chunks), self.allowed_origin, f"{self.location} style")
            self.style_chunks = None
        if self.csp_policy != CSP_POLICY:
            raise SiteValidationError(f"required Content-Security-Policy is missing in {self.location}")

    def _inspect(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        values = {name.lower(): value for name, value in attrs if value is not None}
        for name in AUTOLOAD_ATTRIBUTES:
            value = values.get(name)
            if value:
                _assert_resource_url(value, self.allowed_origin, f"{self.location} {normalized_tag}[{name}]")
        if normalized_tag == "link" and values.get("href"):
            _assert_resource_url(values["href"], self.allowed_origin, f"{self.location} link[href]")
        if normalized_tag == "object" and values.get("data"):
            _assert_resource_url(values["data"], self.allowed_origin, f"{self.location} object[data]")
        http_equiv = values.get("http-equiv", "").strip().lower()
        if normalized_tag == "meta" and http_equiv == "content-security-policy":
            if self.csp_policy is not None or values.get("content") != CSP_POLICY:
                raise SiteValidationError(f"invalid Content-Security-Policy in {self.location}")
            self.csp_policy = values["content"]
        if normalized_tag == "meta" and http_equiv == "refresh":
            parts = re.split(r"[;,]", values.get("content", ""), maxsplit=1)
            if len(parts) == 2:
                resource = re.sub(r"^\s*url\s*=\s*", "", parts[1], flags=re.IGNORECASE).strip(" \t'\"")
                if resource:
                    _assert_resource_url(resource, self.allowed_origin, f"{self.location} meta[content]")
        for name in ("imagesrcset", "srcset"):
            if values.get(name):
                for candidate in values[name].split(","):
                    resource = candidate.strip().split(maxsplit=1)[0]
                    _assert_resource_url(resource, self.allowed_origin, f"{self.location} {normalized_tag}[{name}]")
        if values.get("style"):
            _validate_css(
                values["style"],
                self.allowed_origin,
                f"{self.location} {normalized_tag}[style]",
                declarations=True,
            )


def _validate_component_values(
    tokens: Iterable[object],
    allowed_origin: tuple[str, str],
    location: str,
    *,
    strings_are_resources: bool = False,
) -> None:
    for token in tokens:
        token_type = getattr(token, "type", "")
        if token_type == "error":
            raise SiteValidationError(f"invalid CSS in {location}")
        if token_type == "url" or strings_are_resources and token_type == "string":
            _assert_resource_url(token.value, allowed_origin, location)
        if token_type == "function":
            resource_function = token.lower_name in RESOURCE_STRING_FUNCTIONS
            _validate_component_values(
                token.arguments,
                allowed_origin,
                location,
                strings_are_resources=resource_function,
            )
        nested = getattr(token, "content", None)
        if nested is not None:
            _validate_component_values(nested, allowed_origin, location)


def _validate_css(
    content: str,
    allowed_origin: tuple[str, str],
    location: str,
    *,
    declarations: bool = False,
) -> None:
    if declarations:
        nodes = tinycss2.parse_declaration_list(content, skip_comments=True, skip_whitespace=True)
    else:
        nodes = tinycss2.parse_stylesheet(content, skip_comments=True, skip_whitespace=True)
    for node in nodes:
        if node.type == "error":
            raise SiteValidationError(f"invalid CSS in {location}")
        prelude = getattr(node, "prelude", ())
        if node.type == "at-rule" and node.lower_at_keyword == "import":
            _validate_component_values(prelude, allowed_origin, location, strings_are_resources=True)
        else:
            _validate_component_values(prelude, allowed_origin, location)
        content_tokens = getattr(node, "content", None)
        if content_tokens is not None:
            _validate_component_values(content_tokens, allowed_origin, location)


def validate_site(site: Path, allowed_origin: str) -> None:
    site = site.resolve(strict=True)
    if not site.is_dir():
        raise SiteValidationError(f"site is not a directory: {site}")
    trusted_origin = _origin(allowed_origin)
    for path in sorted(site.rglob("*")):
        if path.is_symlink():
            raise SiteValidationError(f"symbolic link is not allowed in built site: {path.relative_to(site)}")
        if not path.is_file() or path.suffix.lower() not in {".css", ".html"}:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise SiteValidationError(f"HTML and CSS must be UTF-8: {path.relative_to(site)}") from error
        location = path.relative_to(site).as_posix()
        if path.suffix.lower() == ".css":
            _validate_css(content, trusted_origin, location)
        else:
            try:
                validate_csp_first_in_head(content)
            except ValueError as error:
                raise SiteValidationError(f"{error} in {location}") from error
            validator = _GeneratedHTMLValidator(trusted_origin, location)
            validator.feed(content)
            validator.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--allowed-origin", required=True)
    args = parser.parse_args(argv)
    try:
        validate_site(args.site, args.allowed_origin)
    except (OSError, SiteValidationError) as error:
        print(f"built site validation error: {error}", file=sys.stderr)
        return 2
    print(f"Validated generated site resources in {args.site}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
