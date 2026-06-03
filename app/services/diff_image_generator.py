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

    This renders the code content with 🟢/🔴 indicators for added/deleted lines.

    Args:
        command: The command content from the Edit tool

    Returns:
        PNG image bytes
    """
    font = _get_font()

    # Parse the command to extract lines with indicators
    lines = command.splitlines()

    # Calculate dimensions
    max_line_len = max((len(line) for line in lines), default=20)
    width = min(1200, _calculate_text_width("x" * (max_line_len + 30), font) + PADDING * 2)
    height = len(lines) * LINE_HEIGHT + PADDING * 2

    # Create image
    img = Image.new("RGB", (width, height), COLORS["background"])
    draw = ImageDraw.Draw(img)

    y = PADDING

    for line in lines:
        # Determine line type based on content
        # Lines starting with + are additions, - are deletions
        if line.startswith("+"):
            indicator = "🟢"
            text_color = COLORS["add_text"]
            display_line = line[1:]  # Remove the + prefix
        elif line.startswith("-"):
            indicator = "🔴"
            text_color = COLORS["del_text"]
            display_line = line[1:]  # Remove the - prefix
        else:
            indicator = "  "
            text_color = COLORS["context_text"]
            display_line = line

        # Draw indicator
        draw.text((PADDING, y + 2), indicator, fill=text_color, font=font)

        # Draw content
        content_x = PADDING + 30
        # Truncate content if too long
        max_chars = (width - content_x - PADDING) // (FONT_SIZE // 2)
        display_content = display_line[:max_chars]
        draw.text((content_x, y + 2), display_content, fill=text_color, font=font)

        y += LINE_HEIGHT

    # Crop to actual content height
    img = img.crop((0, 0, width, y + PADDING))

    # Add subtle border
    border_img = Image.new("RGB", (img.width + 4, img.height + 4), COLORS["border"])
    border_img.paste(img, (2, 2))

    # Convert to bytes
    buffer = io.BytesIO()
    border_img.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


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
