"""
File Search tool — file search.

Allows agents to search files and their content in the specified directory.
Supports glob patterns and content search.
"""

import fnmatch
import re
from pathlib import Path
from typing import Any

from .base import BaseTool, ToolResult

# Constants for search limits
MAX_MATCHES_PER_FILE = 100
MAX_DISPLAY_MATCHES = 10
MAX_LINE_LENGTH = 200
MAX_TOTAL_MATCHES = 500


class FileSearchTool(BaseTool):
    """
    Tool for searching files and their content.

    Supports:
    - File search by name (glob patterns)
    - File content search (regex or plain text)
    - Search depth limit
    - File content reading

    Example:
        tool = FileSearchTool(base_directory="./project")

        # Search files by pattern
        result = tool.execute(pattern="*.py")

        # Search by content
        result = tool.execute(query="def main", pattern="*.py")

        # Read file
        result = tool.execute(read_file="src/main.py")

    """

    def __init__(
        self,
        base_directory: str | Path = ".",
        max_results: int = 50,
        max_depth: int = 10,
        max_file_size: int = 100_000,  # 100KB
        max_read_size: int = 10_000,  # 10KB for reading a file
        allowed_extensions: list[str] | None = None,
    ):
        """
        Create FileSearchTool.

        Args:
            base_directory: Base directory for searching.
            max_results: Maximum number of results.
            max_depth: Maximum recursion depth.
            max_file_size: Maximum file size for content search.
            max_read_size: Maximum size for reading a file.
            allowed_extensions: Allowed extensions (None = all).

        """
        self._base_directory = Path(base_directory).resolve()
        self._max_results = max_results
        self._max_depth = max_depth
        self._max_file_size = max_file_size
        self._max_read_size = max_read_size
        self._allowed_extensions = set(allowed_extensions) if allowed_extensions else None

    @property
    def name(self) -> str:
        return "file_search"

    @property
    def description(self) -> str:
        return (
            "Search for files by name or content. "
            "Can find files matching a pattern, search text within files, "
            "or read file contents."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to match file names (e.g., '*.py', 'test_*.py'). Default: '*'",
                },
                "query": {
                    "type": "string",
                    "description": "Text or regex to search within file contents. Optional.",
                },
                "read_file": {
                    "type": "string",
                    "description": "Path to a specific file to read. If provided, other parameters are ignored.",
                },
                "directory": {
                    "type": "string",
                    "description": "Subdirectory to search in (relative to base). Default: base directory.",
                },
                "regex": {
                    "type": "boolean",
                    "description": "If true, treat 'query' as regex pattern. Default: false.",
                },
            },
            "required": [],
        }

    def _is_path_safe(self, path: Path) -> bool:
        """Check that the path is inside base_directory."""
        try:
            resolved = path.resolve()
            return resolved.is_relative_to(self._base_directory)
        except (ValueError, OSError):
            return False

    def _is_extension_allowed(self, path: Path) -> bool:
        """Check whether the file extension is allowed."""
        if self._allowed_extensions is None:
            return True
        return path.suffix.lower() in self._allowed_extensions

    def _read_file_content(self, path: Path) -> ToolResult:
        """Read file contents."""
        if not self._is_path_safe(path):
            return ToolResult(
                tool_name=self.name,
                success=False,
                error="Access denied: path outside base directory",
            )

        if not path.exists():
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"File not found: {path}",
            )

        if not path.is_file():
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"Not a file: {path}",
            )

        try:
            file_size = path.stat().st_size
            if file_size > self._max_read_size:
                # Read only the beginning
                with Path(path).open(encoding="utf-8", errors="replace") as f:
                    content = f.read(self._max_read_size)
                content += f"\n\n... (file truncated, showing first {self._max_read_size} bytes of {file_size} total)"
            else:
                with Path(path).open(encoding="utf-8", errors="replace") as f:
                    content = f.read()

            rel_path = path.relative_to(self._base_directory) if self._is_path_safe(path) else path
            return ToolResult(
                tool_name=self.name,
                success=True,
                output=f"=== {rel_path} ===\n{content}",
            )
        except PermissionError:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"Permission denied: {path}",
            )
        except (ValueError, OSError, UnicodeDecodeError) as e:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error=f"Error reading file: {e}",
            )

    def _find_files(
        self,
        pattern: str,
        directory: Path,
        depth: int = 0,
    ) -> list[Path]:
        """Recursively find files by pattern."""
        if depth > self._max_depth:
            return []

        results: list[Path] = []

        try:
            for item in directory.iterdir():
                if len(results) >= self._max_results:
                    break

                if not self._is_path_safe(item):
                    continue

                # Skip hidden files and directories
                if item.name.startswith("."):
                    continue

                if item.is_file():
                    if fnmatch.fnmatch(item.name, pattern) and self._is_extension_allowed(item):
                        results.append(item)
                elif item.is_dir():
                    # Recursive search
                    results.extend(self._find_files(pattern, item, depth + 1))

        except PermissionError:
            pass
        except OSError:
            pass

        return results[: self._max_results]

    def _search_in_file(self, path: Path, query: str, *, use_regex: bool) -> list[tuple[int, str]]:
        """Find matches in a file. Returns a list of (line number, line)."""
        matches: list[tuple[int, str]] = []

        if path.stat().st_size > self._max_file_size:
            return matches

        try:
            with Path(path).open(encoding="utf-8", errors="replace") as f:
                for line_num, line in enumerate(f, 1):
                    if use_regex:
                        if re.search(query, line):
                            matches.append((line_num, line.rstrip()))
                    elif query.lower() in line.lower():
                        matches.append((line_num, line.rstrip()))

                    if len(matches) >= MAX_MATCHES_PER_FILE:
                        break
        except (OSError, UnicodeDecodeError):
            pass

        return matches

    def execute(  # noqa: PLR0912
        self,
        pattern: str = "*",
        query: str = "",
        read_file: str = "",
        directory: str = "",
        *,
        regex: bool = False,
        **_kwargs: Any,
    ) -> ToolResult:
        """
        Execute file search.

        Args:
            pattern: Glob pattern for file names.
            query: Text to search inside files.
            read_file: Path to a file to read.
            directory: Subdirectory to search in.
            regex: Whether to use regex for query.

        Returns:
            ToolResult with search results.

        """
        # If reading a specific file was requested
        if read_file:
            file_path = Path(read_file)
            if not file_path.is_absolute():
                file_path = self._base_directory / file_path
            return self._read_file_content(file_path)

        # Determine the search directory
        search_dir = self._base_directory
        if directory:
            search_dir = self._base_directory / directory
            if not self._is_path_safe(search_dir):
                return ToolResult(
                    tool_name=self.name,
                    success=False,
                    error="Access denied: directory outside base path",
                )
            if not search_dir.exists():
                return ToolResult(
                    tool_name=self.name,
                    success=False,
                    error=f"Directory not found: {directory}",
                )

        # Search files
        files = self._find_files(pattern, search_dir)

        if not files:
            return ToolResult(
                tool_name=self.name,
                success=True,
                output=f"No files found matching pattern '{pattern}'",
            )

        # If query is set — search by content
        if query:
            try:
                if regex:
                    re.compile(query)  # Validate regex
            except re.error as e:
                return ToolResult(
                    tool_name=self.name,
                    success=False,
                    error=f"Invalid regex: {e}",
                )

            output_lines = [f"Search results for '{query}' in {len(files)} file(s):\n"]
            total_matches = 0

            for file_path in files:
                matches = self._search_in_file(file_path, query, use_regex=regex)
                if matches:
                    rel_path = file_path.relative_to(self._base_directory)
                    output_lines.append(f"\n=== {rel_path} ===")
                    for line_num, line in matches[:MAX_DISPLAY_MATCHES]:
                        # Trim long lines
                        display_line = line[:MAX_LINE_LENGTH] + "..." if len(line) > MAX_LINE_LENGTH else line
                        output_lines.append(f"  {line_num}: {display_line}")
                    if len(matches) > MAX_DISPLAY_MATCHES:
                        output_lines.append(f"  ... and {len(matches) - MAX_DISPLAY_MATCHES} more matches")
                    total_matches += len(matches)

                    if total_matches >= MAX_TOTAL_MATCHES:
                        output_lines.append(f"\n... (search limited to {MAX_TOTAL_MATCHES} matches)")
                        break

            if total_matches == 0:
                output_lines.append("No matches found.")
            else:
                output_lines.insert(1, f"Found {total_matches} match(es).")

            return ToolResult(
                tool_name=self.name,
                success=True,
                output="\n".join(output_lines),
            )

        # Just list files
        output_lines = [f"Found {len(files)} file(s) matching '{pattern}':\n"]
        for file_path in files:
            rel_path = file_path.relative_to(self._base_directory)
            size = file_path.stat().st_size
            output_lines.append(f"  {rel_path} ({size:,} bytes)")

        if len(files) >= self._max_results:
            output_lines.append(f"\n... (results limited to {self._max_results})")

        return ToolResult(
            tool_name=self.name,
            success=True,
            output="\n".join(output_lines),
        )
