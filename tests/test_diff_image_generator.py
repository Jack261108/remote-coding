"""Tests for diff image generator."""

from __future__ import annotations

import pytest

from app.services.diff_image_generator import (
    _parse_diff,
    render_diff_summary_to_image,
    render_diff_to_image,
    render_permission_diff_to_image,
)

SAMPLE_DIFF = """\
--- a/test.py
+++ b/test.py
@@ -1,5 +1,6 @@
 import sys

-def hello():
-    print("hello")
+def hello(name: str = "world"):
+    print(f"hello {name}")
+    return True

"""


def test_parse_diff_basic():
    """Test basic diff parsing."""
    files = _parse_diff(SAMPLE_DIFF)
    assert len(files) == 1
    assert files[0].filename == "test.py"
    assert len(files[0].lines) > 0


def test_parse_diff_multiple_files():
    """Test parsing diff with multiple files."""
    diff = """\
--- a/file1.py
+++ b/file1.py
@@ -1,3 +1,3 @@
-old
+new

--- a/file2.py
+++ b/file2.py
@@ -1,3 +1,3 @@
-old2
+new2
"""
    files = _parse_diff(diff)
    assert len(files) == 2
    assert files[0].filename == "file1.py"
    assert files[1].filename == "file2.py"


def test_parse_diff_empty():
    """Test parsing empty diff."""
    files = _parse_diff("")
    assert len(files) == 0


def test_parse_diff_no_changes():
    """Test parsing diff with no actual changes."""
    diff = """\
--- a/test.py
+++ b/test.py
@@ -1,3 +1,3 @@
 unchanged
 unchanged
 unchanged
"""
    files = _parse_diff(diff)
    assert len(files) == 1
    # Should have hunk header and context lines
    assert files[0].lines[0].line_type == "hunk"
    assert all(line.line_type == "context" for line in files[0].lines[1:])


def test_render_diff_to_image_basic():
    """Test basic image rendering."""
    image_bytes = render_diff_to_image(SAMPLE_DIFF)
    assert len(image_bytes) > 0
    # Check PNG magic bytes
    assert image_bytes[:4] == b"\x89PNG"


def test_render_diff_to_image_empty():
    """Test rendering empty diff."""
    image_bytes = render_diff_to_image("")
    assert len(image_bytes) > 0


def test_render_diff_to_image_large():
    """Test rendering large diff."""
    # Create a large diff
    lines = ["--- a/large.py\n", "+++ b/large.py\n", "@@ -1,100 +1,100 @@\n"]
    for i in range(100):
        lines.append(f" line {i}\n")
    for i in range(50):
        lines.append(f"+added line {i}\n")
    diff = "".join(lines)

    image_bytes = render_diff_to_image(diff)
    assert len(image_bytes) > 0


def test_render_diff_summary_basic():
    """Test basic summary rendering."""
    image_bytes = render_diff_summary_to_image(
        file_count=3,
        add_count=50,
        del_count=20,
        diff_text=SAMPLE_DIFF,
    )
    assert len(image_bytes) > 0
    assert image_bytes[:4] == b"\x89PNG"


def test_render_diff_to_image_with_special_chars():
    """Test rendering diff with special characters."""
    diff = """\
--- a/test.py
+++ b/test.py
@@ -1,3 +1,3 @@
-old = "hello"
+new = "world 'with' \\"quotes\\""
"""
    image_bytes = render_diff_to_image(diff)
    assert len(image_bytes) > 0


def test_render_diff_to_image_with_unicode():
    """Test rendering diff with unicode characters."""
    diff = """\
--- a/test.py
+++ b/test.py
@@ -1,3 +1,3 @@
-old = "hello"
+new = "你好世界"
"""
    image_bytes = render_diff_to_image(diff)
    assert len(image_bytes) > 0


@pytest.mark.parametrize("max_width", [600, 800, 1200, 1920])
def test_render_diff_to_image_different_widths(max_width: int):
    """Test rendering with different max widths."""
    image_bytes = render_diff_to_image(SAMPLE_DIFF, max_width=max_width)
    assert len(image_bytes) > 0


def test_parse_diff_line_numbers():
    """Test that line numbers are correctly parsed."""
    diff = """\
--- a/test.py
+++ b/test.py
@@ -1,5 +1,6 @@
 import sys

-def hello():
-    print("hello")
+def hello(name: str = "world"):
+    print(f"hello {name}")
+    return True
"""
    files = _parse_diff(diff)
    assert len(files) == 1

    # Check line numbers
    add_lines = [line for line in files[0].lines if line.line_type == "add"]
    del_lines = [line for line in files[0].lines if line.line_type == "del"]
    context_lines = [line for line in files[0].lines if line.line_type == "context"]

    # Should have some added lines with new_line_num
    assert any(line.new_line_num is not None for line in add_lines)
    # Should have some deleted lines with old_line_num
    assert any(line.old_line_num is not None for line in del_lines)
    # Should have some context lines with both line numbers
    assert any(line.old_line_num is not None and line.new_line_num is not None for line in context_lines)


def test_render_permission_diff_to_image_basic():
    """Test basic permission diff rendering."""
    command = "+def hello(name: str = 'world'):\n-    print('hello')\n+    print(f'hello {name}')"
    image_bytes = render_permission_diff_to_image(command)
    assert len(image_bytes) > 0
    assert image_bytes[:4] == b"\x89PNG"


def test_render_permission_diff_to_image_additions_only():
    """Test rendering with only additions."""
    command = "+line1\n+line2\n+line3"
    image_bytes = render_permission_diff_to_image(command)
    assert len(image_bytes) > 0


def test_render_permission_diff_to_image_deletions_only():
    """Test rendering with only deletions."""
    command = "-line1\n-line2\n-line3"
    image_bytes = render_permission_diff_to_image(command)
    assert len(image_bytes) > 0


def test_render_permission_diff_to_image_context_only():
    """Test rendering with only context lines (no +/-)."""
    command = "line1\nline2\nline3"
    image_bytes = render_permission_diff_to_image(command)
    assert len(image_bytes) > 0


def test_render_permission_diff_to_image_empty():
    """Test rendering empty command."""
    image_bytes = render_permission_diff_to_image("")
    assert len(image_bytes) > 0
