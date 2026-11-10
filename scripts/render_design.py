"""Optional artifact build: uv run --with reportlab --with pymupdf scripts/render_design.py."""

import html
import re
import tempfile
from pathlib import Path

import pymupdf
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

root = Path(__file__).resolve().parents[1]
destination = root / "docs" / "design.pdf"
source = (root / "docs" / "design.md").read_text()
body = ParagraphStyle(
    "body",
    fontName="Helvetica",
    fontSize=9.3,
    leading=12.2,
    spaceAfter=7,
    textColor=colors.HexColor("#253247"),
    alignment=TA_LEFT,
)
heading = ParagraphStyle(
    "heading",
    parent=body,
    fontName="Helvetica-Bold",
    fontSize=10.6,
    leading=13,
    spaceBefore=5,
    spaceAfter=5,
    textColor=colors.HexColor("#133F48"),
)
title = ParagraphStyle("title", parent=heading, fontSize=20, leading=24, spaceAfter=12)
references = ParagraphStyle("references", parent=body, fontSize=8, leading=10, spaceAfter=3)


def inline(value):
    value = html.escape(value.replace("—", "-").replace("–", "-"))
    value = re.sub(r"\[([^]]+)\]\(([^)]+)\)", r'<a href="\2" color="#176D7B">\1</a>', value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", value)
    return re.sub(r"`([^`]+)`", r'<font name="Courier">\1</font>', value)


story = []
for block in source.split("\n\n"):
    if block.startswith("# "):
        story.append(Paragraph("Training queue / Design note", title))
        story.append(
            Paragraph("SOUP BACKEND ENGINEER  |  DESIGN RECORDED BEFORE IMPLEMENTATION", references)
        )
        story.append(Spacer(1, 7))
    elif block.startswith("##"):
        story.append(Paragraph(inline(block.lstrip("# ")), heading))
    elif block.startswith("- "):
        for line in block.splitlines():
            story.append(Paragraph(inline(line[2:]), references))
    elif block.strip():
        story.append(Paragraph(inline(block.strip()), body))


def footer(canvas, document):
    canvas.setStrokeColor(colors.HexColor("#B7CFD0"))
    canvas.line(38, 31, A4[0] - 38, 31)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#617782"))
    canvas.drawString(38, 20, "Async FastAPI / SQLAlchemy / PostgreSQL / Redis")
    canvas.drawRightString(A4[0] - 38, 20, str(document.page))


document = SimpleDocTemplate(
    str(destination),
    pagesize=A4,
    rightMargin=38,
    leftMargin=38,
    topMargin=32,
    bottomMargin=40,
    title="Soup Queue - Design Before Code",
    author="AI-assisted implementation",
)
document.build(story, onFirstPage=footer, onLaterPages=footer)
pdf = pymupdf.open(destination)
if len(pdf) != 1:
    raise RuntimeError(f"Design note must be one page, got {len(pdf)}")
preview = Path(tempfile.gettempdir()) / "soup-design-preview.png"
pdf[0].get_pixmap(matrix=pymupdf.Matrix(1.8, 1.8)).save(preview)
print(f"Verified one-page design: {destination}\nPreview: {preview}")
