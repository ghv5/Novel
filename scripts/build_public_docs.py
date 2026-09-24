#!/usr/bin/env python3
"""Stage an allowlisted MkDocs document tree from publish-files.txt."""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import shutil
import sys
from dataclasses import dataclass
from functools import lru_cache
from html import escape
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import quote, urlsplit

import markdown
from detect_secrets import SecretsCollection
from detect_secrets.settings import default_settings


class ManifestError(ValueError):
    """Raised when the publication manifest or selected files are unsafe."""


@dataclass(frozen=True)
class Rule:
    pattern: str
    excluded: bool
    line: int


GLOB_MARKERS = frozenset("*?[")
SENSITIVE_NAMES = frozenset(
    {
        ".env",
        ".envrc",
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
        "secrets.json",
        "secrets.txt",
        "secrets.yaml",
        "secrets.yml",
    }
)
SECRET_PATTERNS = (
    ("private key", re.compile(br"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    ("GitHub token", re.compile(br"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("AWS access key", re.compile(br"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("Slack token", re.compile(br"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
)
ACTIVE_TAGS = frozenset(
    {
        "base",
        "button",
        "embed",
        "form",
        "iframe",
        "input",
        "link",
        "math",
        "meta",
        "object",
        "script",
        "select",
        "style",
        "svg",
        "textarea",
    }
)
URL_ATTRIBUTES = frozenset(
    {"action", "background", "formaction", "href", "poster", "src", "xlink:href"}
)
AUTOLOAD_ATTRIBUTES = frozenset({"background", "poster", "src", "xlink:href"})
ACTIVE_SCHEMES = ("javascript:", "vbscript:", "data:text/html")
ACTIVE_FILE_SUFFIXES = frozenset({".htm", ".html", ".js", ".mjs", ".svg", ".xhtml"})
CSS_ACTIVE_PATTERN = re.compile(
    r"@import\b|url\s*\(|(?:-webkit-)?image-set\s*\(|expression\s*\(|behavior\s*:|-moz-binding\s*:",
    re.IGNORECASE,
)
CSS_ESCAPE = re.compile(
    r"\\(?:([0-9a-fA-F]{1,6})[ \t\r\n\f]?|(\r\n|[\n\r\f])|(.))",
    re.DOTALL,
)
SECRET_ALLOWLIST_PRAGMA = re.compile(rb"pragma\s*:\s*allowlist\s+secret", re.IGNORECASE)


class _ActiveContentDetector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.finding: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._inspect(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._inspect(tag, attrs)

    def _inspect(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        if normalized_tag in ACTIVE_TAGS:
            self.finding = f"HTML tag <{normalized_tag}>"
            return
        class_value = next((value for name, value in attrs if name.lower() == "class" and value), "")
        if "mermaid" in class_value.lower().split():
            self.finding = "HTML class mermaid"
            return
        for name, value in attrs:
            normalized_name = name.lower()
            if normalized_name.startswith("on") or normalized_name in {"srcdoc", "style"}:
                self.finding = f"HTML attribute {normalized_name}"
                return
            if normalized_name == "srcset" and value:
                self.finding = "automatically loaded srcset"
                return
            if normalized_name in URL_ATTRIBUTES and value:
                normalized_value = re.sub(r"[\x00-\x20]+", "", value).lower()
                if normalized_value.startswith(ACTIVE_SCHEMES):
                    self.finding = f"URL scheme in {normalized_name}"
                    return
                if "\\" in normalized_value:
                    self.finding = f"unsafe URL separator in {normalized_name}"
                    return
                parsed = urlsplit(normalized_value)
                if normalized_name in AUTOLOAD_ATTRIBUTES and (parsed.scheme or normalized_value.startswith("//")):
                    self.finding = f"external resource in {normalized_name}"
                    return


def _decode_css_escapes(content: str) -> str:
    def replace(match: re.Match[str]) -> str:
        if match.group(1):
            codepoint = int(match.group(1), 16)
            if codepoint == 0 or codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
                return "\N{REPLACEMENT CHARACTER}"
            return chr(codepoint)
        if match.group(2):
            return ""
        return match.group(3) or ""

    return CSS_ESCAPE.sub(replace, content)


SENSITIVE_SUFFIXES = (
    ".env",
    ".jks",
    ".key",
    ".keystore",
    ".p12",
    ".pem",
    ".pfx",
)


def _validate_pattern(pattern: str, line: int) -> None:
    if not pattern:
        raise ManifestError(f"line {line}: pattern must not be empty")
    if "\x00" in pattern or "\\" in pattern:
        raise ManifestError(f"line {line}: pattern must use a relative POSIX path")
    if pattern.startswith(("/", "~/")) or re.match(r"^[A-Za-z]:", pattern):
        raise ManifestError(f"line {line}: pattern must be repository-relative")
    parts = PurePosixPath(pattern).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ManifestError(f"line {line}: unsafe path segment in {pattern!r}")


def parse_rules(text: str) -> tuple[Rule, ...]:
    rules: list[Rule] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        value = raw_line.strip()
        if not value or value.startswith("#"):
            continue
        excluded = value.startswith("!")
        pattern = value[1:] if excluded else value
        _validate_pattern(pattern, line_number)
        rules.append(Rule(pattern=pattern, excluded=excluded, line=line_number))
    if not any(not rule.excluded for rule in rules):
        raise ManifestError("manifest must contain at least one positive rule")
    return tuple(rules)


def match_glob(path: str, pattern: str) -> bool:
    path_parts = PurePosixPath(path).parts
    pattern_parts = PurePosixPath(pattern).parts

    @lru_cache(maxsize=None)
    def match(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        part = pattern_parts[pattern_index]
        if part == "**":
            return match(path_index, pattern_index + 1) or (
                path_index < len(path_parts) and match(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[path_index], part)
            and match(path_index + 1, pattern_index + 1)
        )

    return match(0, 0)


def _fixed_prefix(pattern: str) -> PurePosixPath:
    fixed: list[str] = []
    for part in PurePosixPath(pattern).parts:
        if part == "**" or any(marker in part for marker in GLOB_MARKERS):
            break
        fixed.append(part)
    return PurePosixPath(*fixed) if fixed else PurePosixPath(".")


def _is_sensitive(relative: PurePosixPath) -> bool:
    lower_parts = tuple(part.lower() for part in relative.parts)
    if any(part in {".git", ".github"} for part in lower_parts):
        return True
    name = lower_parts[-1]
    return name in SENSITIVE_NAMES or name.startswith(".env.") or name.endswith(SENSITIVE_SUFFIXES)


def _validate_selected_content(root: Path, relative: PurePosixPath) -> None:
    source = root.joinpath(*relative.parts)
    content = source.read_bytes()
    if SECRET_ALLOWLIST_PRAGMA.search(content):
        raise ManifestError(
            f"secret allowlist pragmas are not permitted in published content: {relative}"
        )
    for finding, pattern in SECRET_PATTERNS:
        if pattern.search(content):
            raise ManifestError(f"secret-like {finding} is not publishable: {relative}")
    detected_secrets = SecretsCollection()
    with default_settings():
        detected_secrets.scan_file(str(source))
    findings = detected_secrets.data.get(str(source), set())
    if findings:
        finding_types = ", ".join(sorted({finding.type for finding in findings}))
        raise ManifestError(f"secret scanner finding ({finding_types}) is not publishable: {relative}")

    suffix = relative.suffix.lower()
    if suffix in ACTIVE_FILE_SUFFIXES:
        raise ManifestError(f"active content file type is not publishable on the shared origin: {relative}")
    if suffix == ".css":
        try:
            css = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ManifestError(f"CSS must be valid UTF-8: {relative}") from error
        if CSS_ACTIVE_PATTERN.search(_decode_css_escapes(css)):
            raise ManifestError(f"active CSS construct is not publishable: {relative}")
        return
    if suffix not in {".md", ".markdown"}:
        return

    try:
        source_markdown = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ManifestError(f"Markdown must be valid UTF-8: {relative}") from error
    _validate_markdown_content(source_markdown, str(relative))


def _validate_markdown_content(source_markdown: str, location: str) -> None:
    rendered = markdown.markdown(
        source_markdown,
        extensions=["attr_list", "md_in_html", "pymdownx.superfences"],
    )
    detector = _ActiveContentDetector()
    detector.feed(rendered)
    detector.close()
    if detector.finding:
        raise ManifestError(f"active content is not publishable ({detector.finding}): {location}")


def _ensure_no_symlinks(root: Path, relative_root: PurePosixPath, line: int) -> None:
    scan_root = root if str(relative_root) == "." else root.joinpath(*relative_root.parts)
    if not scan_root.exists():
        return
    if scan_root.is_symlink():
        raise ManifestError(f"line {line}: symbolic link is not publishable: {relative_root}")
    for current, directories, files in os.walk(scan_root, followlinks=False):
        current_path = Path(current)
        for name in (*directories, *files):
            candidate = current_path / name
            if candidate.is_symlink():
                relative = candidate.relative_to(root).as_posix()
                raise ManifestError(f"line {line}: symbolic link is not publishable: {relative}")


def _repository_files(root: Path) -> tuple[PurePosixPath, ...]:
    files: list[PurePosixPath] = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if name != ".git"]
        current_path = Path(current)
        for filename in filenames:
            candidate = current_path / filename
            relative = PurePosixPath(candidate.relative_to(root).as_posix())
            files.append(relative)
    return tuple(sorted(files, key=str))


def select_files(root: Path, rules: Iterable[Rule]) -> tuple[PurePosixPath, ...]:
    root = root.resolve(strict=True)
    rule_list = tuple(rules)
    all_files = _repository_files(root)
    selected: set[PurePosixPath] = set()

    for rule in (item for item in rule_list if not item.excluded):
        _ensure_no_symlinks(root, _fixed_prefix(rule.pattern), rule.line)
        matches = tuple(path for path in all_files if match_glob(path.as_posix(), rule.pattern))
        if not matches:
            raise ManifestError(f"line {rule.line}: positive rule matched no files: {rule.pattern}")
        for path in matches:
            if _is_sensitive(path):
                raise ManifestError(f"line {rule.line}: sensitive file is not publishable: {path}")
            selected.add(path)

    excluded = {
        path
        for path in selected
        if any(rule.excluded and match_glob(path.as_posix(), rule.pattern) for rule in rule_list)
    }
    final = tuple(sorted(selected - excluded, key=str))
    if not final:
        raise ManifestError("manifest selected no publishable files after exclusions")
    for path in final:
        _validate_selected_content(root, path)
    return final


def _assert_safe_source(root: Path, relative: PurePosixPath) -> Path:
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ManifestError(f"symbolic link is not publishable: {relative}")
    if not current.is_file() or not current.resolve(strict=True).is_relative_to(root):
        raise ManifestError(f"source is not a safe regular file: {relative}")
    return current


def _generated_index(paths: Iterable[PurePosixPath], site_name: str) -> str:
    links: list[str] = []
    for path in paths:
        if path.suffix.lower() != ".md":
            continue
        label = escape(path.stem.replace("\r", " ").replace("\n", " "), quote=True)
        label = (
            label.replace("\\", "\\\\")
            .replace("[", "\\[")
            .replace("]", "\\]")
            .replace("(", "\\(")
            .replace(")", "\\)")
        )
        target = quote(path.as_posix(), safe="/._~-")
        links.append(f"- [{label}]({target})")
    generated = f"# {site_name}\n\n" + "\n".join(links) + "\n"
    _validate_markdown_content(generated, "generated index.md")
    return generated


def stage_files(
    root: Path,
    destination: Path,
    selected: Iterable[PurePosixPath],
    *,
    strip_prefix: PurePosixPath,
    site_name: str = "文档",
) -> tuple[PurePosixPath, ...]:
    root = root.resolve(strict=True)
    prefix_parts = strip_prefix.parts
    staged: list[PurePosixPath] = []
    destination.mkdir(parents=True, exist_ok=False)
    destination_root = destination.resolve(strict=True)

    for relative in selected:
        if relative.parts[: len(prefix_parts)] != prefix_parts:
            raise ManifestError(f"selected path is outside strip prefix: {relative}")
        output_relative = PurePosixPath(*relative.parts[len(prefix_parts) :])
        if not output_relative.parts:
            raise ManifestError(f"selected path has no output name: {relative}")
        source = _assert_safe_source(root, relative)
        target = destination_root.joinpath(*output_relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.parent.resolve(strict=True).is_relative_to(destination_root):
            raise ManifestError(f"destination escapes staging directory: {relative}")
        shutil.copyfile(source, target, follow_symlinks=False)
        staged.append(output_relative)

    staged_tuple = tuple(sorted(staged, key=str))
    index = destination_root / "index.md"
    if not index.exists():
        index.write_text(_generated_index(staged_tuple, site_name), encoding="utf-8")
    return staged_tuple


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path, default=Path("publish-files.txt"))
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--strip-prefix", default="docs")
    parser.add_argument("--site-name", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repository = args.repository.resolve(strict=True)
    manifest = args.manifest if args.manifest.is_absolute() else repository / args.manifest
    try:
        text = manifest.read_text(encoding="utf-8")
        rules = parse_rules(text)
        selected = select_files(repository, rules)
        staged = stage_files(
            repository,
            args.destination,
            selected,
            strip_prefix=PurePosixPath(args.strip_prefix),
            site_name=args.site_name,
        )
    except (OSError, UnicodeError, ManifestError) as error:
        print(f"publish manifest error: {error}", file=sys.stderr)
        return 2
    print(f"Staged {len(staged)} allowlisted files in {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
