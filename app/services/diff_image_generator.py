"""Service for rendering unified diffs as images."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont


@dataclass
class DiffLine:
    """Represents a single line in a diff with its type and content."""

    line_type: str  # 'add', 'del', 'context', 'header', 'hunk'
    content: str
    old_line_num: int | None = None
    new_line_num: int | None = None


@dataclass
class FileDiff:
    """Represents a diff for a single file."""

    filename: str
    lines: list[DiffLine]


# Color scheme
COLORS = {
    "background": "#1e1e1e",  # Dark background
    "header_bg": "#2d2d2d",  # Header background
    "header_text": "#e0e0e0",  # Header text
    "add_bg": "#1a3d1a",  # Green tint for additions
    "add_text": "#4ec94e",  # Green text for additions
    "del_bg": "#3d1a1a",  # Red tint for deletions
    "del_text": "#e06c6c",  # Red text for deletions
    "context_text": "#d4d4d4",  # Light gray for context
    "line_num_bg": "#252525",  # Line number background
    "line_num_text": "#858585",  # Line number text
    "hunk_text": "#569cd6",  # Blue for hunk headers
    "border": "#404040",  # Border color
}

# Font size
FONT_SIZE = 14
LINE_HEIGHT = 20
PADDING = 16
LINE_NUM_WIDTH = 50
GUTTER_WIDTH = 2


def _parse_diff(diff_text: str) -> list[FileDiff]:
    """Parse unified diff text into structured FileDiff objects."""
    files: list[FileDiff] = []
    current_file: FileDiff | None = None
    old_line = 0
    new_line = 0

    for line in diff_text.splitlines():
        # New file header
        if line.startswith("--- a/") or line.startswith("--- /"):
            filename = line[6:] if line.startswith("--- a/") else line[4:]
            if current_file and current_file.lines:
                files.append(current_file)
            current_file = FileDiff(filename=filename, lines=[])
            continue

        if line.startswith("+++ b/") or line.startswith("+++ /"):
            if current_file:
                current_file.filename = line[6:] if line.startswith("+++ b/") else line[4:]
            continue

        # Hunk header
        hunk_match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if hunk_match:
            if current_file is None:
                current_file = FileDiff(filename="unknown", lines=[])
            old_line = int(hunk_match.group(1))
            new_line = int(hunk_match.group(2))
            current_file.lines.append(DiffLine(line_type="hunk", content=line))
            continue

        # Skip index lines
        if line.startswith("index "):
            continue

        if current_file is None:
            continue

        # Diff content
        if line.startswith("+"):
            current_file.lines.append(DiffLine(line_type="add", content=line[1:], new_line_num=new_line))
            new_line += 1
        elif line.startswith("-"):
            current_file.lines.append(DiffLine(line_type="del", content=line[1:], old_line_num=old_line))
            old_line += 1
        elif line.startswith(" "):
            current_file.lines.append(
                DiffLine(
                    line_type="context",
                    content=line[1:],
                    old_line_num=old_line,
                    new_line_num=new_line,
                )
            )
            old_line += 1
            new_line += 1
        elif line.startswith("\\"):
            # "No newline at end of file" marker
            current_file.lines.append(DiffLine(line_type="context", content=line, old_line_num=None, new_line_num=None))

    if current_file and current_file.lines:
        files.append(current_file)

    return files


def _get_font(size: int = FONT_SIZE) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Get a monospace font, falling back to default if needed."""
    font_paths = [
        # macOS
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/Monaco.dfont",
        "/System/Library/Fonts/Courier.dfont",
        # Linux
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        # Windows
        "C:/Windows/Fonts/consola.ttf",
        "C:/Windows/Fonts/cour.ttf",
    ]

    for font_path in font_paths:
        try:
            return ImageFont.truetype(font_path, size)
        except OSError:
            continue

    # Fallback to default font
    return ImageFont.load_default()


def _calculate_text_width(text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> int:
    """Calculate text width using font metrics."""
    try:
        bbox = font.getbbox(text)
        return int(bbox[2] - bbox[0])
    except Exception:
        # Rough estimate: 0.6 * font_size per character
        return int(len(text) * FONT_SIZE * 0.6)


def render_diff_to_image(diff_text: str, max_width: int = 1200) -> bytes:
    """Render unified diff text to a PNG image.

    Args:
        diff_text: Unified diff text
        max_width: Maximum image width in pixels

    Returns:
        PNG image bytes
    """
    files = _parse_diff(diff_text)
    if not files:
        # Create a simple "no changes" image
        files = [FileDiff(filename="(no changes)", lines=[DiffLine(line_type="context", content="No modifications detected")])]

    font = _get_font()

    # Calculate dimensions
    total_lines = sum(len(f.lines) + 2 for f in files)  # +2 for filename header per file
    content_height = total_lines * LINE_HEIGHT + PADDING * 2

    # Calculate required width
    max_line_len = 0
    for file_diff in files:
        for line in file_diff.lines:
            max_line_len = max(max_line_len, len(line.content))
    content_width = min(max_width, _calculate_text_width("x" * (max_line_len + 20), font) + PADDING * 2)

    # Create image
    img = Image.new("RGB", (content_width, content_height), COLORS["background"])
    draw = ImageDraw.Draw(img)

    y = PADDING

    for file_diff in files:
        # Draw file header
        header_bg = COLORS["header_bg"]
        draw.rectangle([(0, y), (content_width, y + LINE_HEIGHT)], fill=header_bg)
        draw.text((PADDING, y + 2), f"📄 {file_diff.filename}", fill=COLORS["header_text"], font=font)
        y += LINE_HEIGHT

        # Draw separator
        draw.line([(0, y), (content_width, y)], fill=COLORS["border"], width=1)
        y += 1

        # Draw diff lines
        for line in file_diff.lines:
            # Background
            if line.line_type == "add":
                bg_color = COLORS["add_bg"]
                text_color = COLORS["add_text"]
            elif line.line_type == "del":
                bg_color = COLORS["del_bg"]
                text_color = COLORS["del_text"]
            else:
                bg_color = COLORS["background"]
                text_color = COLORS["context_text"] if line.line_type == "context" else COLORS["hunk_text"]

            # Draw background
            draw.rectangle([(0, y), (content_width, y + LINE_HEIGHT)], fill=bg_color)

            # Draw line numbers
            line_num_x = PADDING
            if line.old_line_num is not None:
                draw.text((line_num_x, y + 2), f"{line.old_line_num:4d}", fill=COLORS["line_num_text"], font=font)
            line_num_x += LINE_NUM_WIDTH
            if line.new_line_num is not None:
                draw.text((line_num_x, y + 2), f"{line.new_line_num:4d}", fill=COLORS["line_num_text"], font=font)

            # Draw gutter
            gutter_x = PADDING + LINE_NUM_WIDTH * 2 + GUTTER_WIDTH
            if line.line_type == "add":
                draw.text((gutter_x - 12, y + 2), "+", fill=text_color, font=font)
            elif line.line_type == "del":
                draw.text((gutter_x - 12, y + 2), "-", fill=text_color, font=font)

            # Draw content
            content_x = gutter_x + 4
            # Truncate content if too long
            max_chars = (content_width - content_x - PADDING) // (FONT_SIZE // 2)
            display_content = line.content[:max_chars]
            draw.text((content_x, y + 2), display_content, fill=text_color, font=font)

            y += LINE_HEIGHT

        # Add spacing between files
        y += LINE_HEIGHT // 2

    # Crop to actual content height
    img = img.crop((0, 0, content_width, y + PADDING))

    # Add subtle border
    border_img = Image.new("RGB", (img.width + 4, img.height + 4), COLORS["border"])
    border_img.paste(img, (2, 2))

    # Convert to bytes
    buffer = io.BytesIO()
    border_img.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def render_permission_diff_to_image(command: str) -> bytes:
    """Render permission diff content (Edit tool command) as an image.

    This renders the code with line numbers, syntax highlighting, and
    red/green backgrounds for deleted/added lines.

    Args:
        command: The command content from the Edit tool

    Returns:
        PNG image bytes
    """
    font = _get_font()

    # Parse the command to extract lines with indicators
    raw_lines = command.splitlines()

    # Calculate dimensions
    max_line_len = max((len(line) for line in raw_lines), default=20)
    width = min(1200, _calculate_text_width("x" * (max_line_len + 30), font) + PADDING * 2)

    # Calculate total height including file header and project path
    total_lines = len(raw_lines) + 2  # +2 for file header and project path
    height = total_lines * LINE_HEIGHT + PADDING * 2

    # Create image
    img = Image.new("RGB", (width, height), COLORS["background"])
    draw = ImageDraw.Draw(img)

    y = PADDING

    # Draw file header (empty line like in the example)
    y += LINE_HEIGHT

    # Draw code lines with line numbers and syntax highlighting
    line_num = 1
    for line in raw_lines:
        # Determine line type based on content
        if line.startswith("+"):
            line_type = "add"
            display_line = line[1:]  # Remove the + prefix
        elif line.startswith("-"):
            line_type = "del"
            display_line = line[1:]  # Remove the - prefix
        else:
            line_type = "context"
            display_line = line

        # Draw background based on line type
        if line_type == "add":
            bg_color = COLORS["add_bg"]
        elif line_type == "del":
            bg_color = COLORS["del_bg"]
        else:
            bg_color = COLORS["background"]
        draw.rectangle([(0, y), (width, y + LINE_HEIGHT)], fill=bg_color)

        # Draw line number
        line_num_str = f"{line_num:2d}"
        draw.text((PADDING, y + 2), line_num_str, fill=COLORS["line_num_text"], font=font)

        # Draw +/- indicator
        indicator_x = PADDING + 30
        if line_type == "add":
            draw.text((indicator_x, y + 2), "+", fill=COLORS["add_text"], font=font)
        elif line_type == "del":
            draw.text((indicator_x, y + 2), "-", fill=COLORS["del_text"], font=font)

        # Draw code content with syntax highlighting
        content_x = indicator_x + 15
        _draw_syntax_highlighted_line(draw, display_line, content_x, y + 2, font, line_type)

        y += LINE_HEIGHT
        line_num += 1

    # Draw project path at the bottom
    y += LINE_HEIGHT // 2
    draw.text((PADDING, y), "📂", fill=COLORS["context_text"], font=font)

    # Crop to actual content height
    img = img.crop((0, 0, width, y + LINE_HEIGHT))

    # Add subtle border
    border_img = Image.new("RGB", (img.width + 4, img.height + 4), COLORS["border"])
    border_img.paste(img, (2, 2))

    # Convert to bytes
    buffer = io.BytesIO()
    border_img.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _draw_syntax_highlighted_line(
    draw: ImageDraw.ImageDraw,
    line: str,
    x: int,
    y: int,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    line_type: str,
) -> None:
    """Draw a line of code with Python syntax highlighting."""
    # Simple token-based highlighting
    tokens = _tokenize_python(line)
    current_x = x

    for token_type, token_value in tokens:
        if token_type == "keyword":
            color = "#569cd6"  # Blue for keywords
        elif token_type == "builtin":
            color = "#dcdcaa"  # Yellow for builtins
        elif token_type == "string":
            color = "#ce9178"  # Orange for strings
        elif token_type == "comment":
            color = "#6a9955"  # Green for comments
        elif token_type == "number":
            color = "#b5cea8"  # Light green for numbers
        elif token_type == "operator":
            color = "#d4d4d4"  # Light gray for operators
        elif token_type == "function":
            color = "#dcdcaa"  # Yellow for function names
        else:
            if line_type == "add":
                color = COLORS["add_text"]
            elif line_type == "del":
                color = COLORS["del_text"]
            else:
                color = COLORS["context_text"]

        draw.text((current_x, y), token_value, fill=color, font=font)
        current_x += _calculate_text_width(token_value, font)


def _tokenize_python(line: str) -> list[tuple[str, str]]:
    """Simple tokenizer for Python code with basic syntax highlighting."""
    import keyword

    keywords = set(keyword.kwlist)
    builtins = {"print", "range", "len", "int", "str", "float", "list", "dict", "set", "tuple", "bool", "None", "True", "False"}

    tokens: list[tuple[str, str]] = []
    i = 0
    n = len(line)

    while i < n:
        # Skip whitespace
        if line[i].isspace():
            j = i
            while j < n and line[j].isspace():
                j += 1
            tokens.append(("whitespace", line[i:j]))
            i = j
            continue

        # Comments
        if line[i] == "#":
            tokens.append(("comment", line[i:]))
            break

        # Strings (single and double quotes)
        if line[i] in ('"', "'"):
            quote = line[i]
            j = i + 1
            # Check for triple quotes
            if i + 2 < n and line[i + 1] == quote and line[i + 2] == quote:
                quote = line[i : i + 3]
                j = i + 3
                while j < n:
                    if line[j : j + 3] == quote:
                        j += 3
                        break
                    j += 1
            else:
                while j < n and line[j] != quote:
                    if line[j] == "\\":
                        j += 1
                    j += 1
                if j < n:
                    j += 1
            tokens.append(("string", line[i:j]))
            i = j
            continue

        # f-strings
        if line[i] == "f" and i + 1 < n and line[i + 1] in ('"', "'"):
            quote = line[i + 1]
            j = i + 2
            if i + 3 < n and line[i + 2] == quote and line[i + 3] == quote:
                quote = line[i + 1 : i + 4]
                j = i + 4
                while j < n:
                    if line[j : j + 3] == quote:
                        j += 3
                        break
                    j += 1
            else:
                while j < n and line[j] != quote:
                    if line[j] == "\\":
                        j += 1
                    j += 1
                if j < n:
                    j += 1
            tokens.append(("string", line[i:j]))
            i = j
            continue

        # Numbers
        if line[i].isdigit():
            j = i
            while j < n and (line[j].isdigit() or line[j] in ".xXeE"):
                j += 1
            tokens.append(("number", line[i:j]))
            i = j
            continue

        # Identifiers and keywords
        if line[i].isalpha() or line[i] == "_":
            j = i
            while j < n and (line[j].isalnum() or line[j] == "_"):
                j += 1
            word = line[i:j]
            if word in keywords:
                tokens.append(("keyword", word))
            elif word in builtins:
                tokens.append(("builtin", word))
            elif j < n and line[j] == "(":
                tokens.append(("function", word))
            else:
                tokens.append(("identifier", word))
            i = j
            continue

        # Operators and punctuation
        tokens.append(("operator", line[i]))
        i += 1

    return tokens


def render_diff_summary_to_image(file_count: int, add_count: int, del_count: int, diff_text: str) -> bytes:
    """Render a summary image for the diff.

    Args:
        file_count: Number of files changed
        add_count: Number of lines added
        del_count: Number of lines deleted
        diff_text: Full diff text for preview

    Returns:
        PNG image bytes
    """
    font = _get_font(FONT_SIZE + 2)
    small_font = _get_font(FONT_SIZE)

    # Calculate dimensions
    width = 400
    height = 200

    # Create image
    img = Image.new("RGB", (width, height), COLORS["background"])
    draw = ImageDraw.Draw(img)

    # Draw title
    draw.text((PADDING, PADDING), "📊 Code Changes Summary", fill=COLORS["header_text"], font=font)

    # Draw stats
    y = PADDING + 40
    stats = [
        f"📄 Files changed: {file_count}",
        f"➕ Lines added: {add_count}",
        f"➖ Lines deleted: {del_count}",
    ]
    for stat in stats:
        draw.text((PADDING, y), stat, fill=COLORS["context_text"], font=small_font)
        y += 28

    # Draw preview (first few lines)
    y += 20
    draw.line([(PADDING, y), (width - PADDING, y)], fill=COLORS["border"], width=1)
    y += 10

    preview_lines = diff_text.splitlines()[:5]
    for line in preview_lines:
        if len(line) > 50:
            line = line[:47] + "..."
        draw.text((PADDING, y), line, fill=COLORS["hunk_text"], font=small_font)
        y += 20

    # Add border
    border_img = Image.new("RGB", (width + 4, height + 4), COLORS["border"])
    border_img.paste(img, (2, 2))

    buffer = io.BytesIO()
    border_img.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
