"""Language detection by extension and per-file size tiering.

Size tiers exist for downstream cost control: the tier decides how a file is
reviewed (single prompt, chunked, or summarized/skipped) before any tokens are
spent. Boundaries are on changed lines (additions + deletions):

- small  (<= 50):   fits comfortably in one prompt alongside context
- medium (<= 300):  still a single prompt, dominant cost driver
- large  (<= 1500): needs chunking; review per-hunk downstream
- huge   (> 1500):  candidate for summary-only treatment or explicit skip
"""

from pathlib import PurePosixPath

from .models import SizeTier

_SMALL_MAX = 50
_MEDIUM_MAX = 300
_LARGE_MAX = 1500

_EXTENSION_LANGUAGES = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".css": "css",
    ".dart": "dart",
    ".ex": "elixir",
    ".exs": "elixir",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".html": "html",
    ".java": "java",
    ".js": "javascript",
    ".json": "json",
    ".jsx": "javascript",
    ".kt": "kotlin",
    ".lua": "lua",
    ".md": "markdown",
    ".mjs": "javascript",
    ".php": "php",
    ".pl": "perl",
    ".proto": "protobuf",
    ".ps1": "powershell",
    ".py": "python",
    ".pyi": "python",
    ".r": "r",
    ".rb": "ruby",
    ".rs": "rust",
    ".scala": "scala",
    ".scss": "scss",
    ".sh": "shell",
    ".sql": "sql",
    ".swift": "swift",
    ".tf": "terraform",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".txt": "text",
    ".vue": "vue",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".zig": "zig",
}

_FILENAME_LANGUAGES = {
    ".gitignore": "gitignore",
    "cmakelists.txt": "cmake",
    "dockerfile": "dockerfile",
    "gemfile": "ruby",
    "justfile": "just",
    "makefile": "make",
    "rakefile": "ruby",
}


def detect_language(path: str) -> str | None:
    """Best-effort language from the file path; None when unknown."""
    name = PurePosixPath(path).name.lower()
    if name in _FILENAME_LANGUAGES:
        return _FILENAME_LANGUAGES[name]
    suffix = PurePosixPath(name).suffix
    return _EXTENSION_LANGUAGES.get(suffix)


def size_tier(changed_lines: int) -> SizeTier:
    if changed_lines <= _SMALL_MAX:
        return SizeTier.SMALL
    if changed_lines <= _MEDIUM_MAX:
        return SizeTier.MEDIUM
    if changed_lines <= _LARGE_MAX:
        return SizeTier.LARGE
    return SizeTier.HUGE
