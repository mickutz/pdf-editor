import io
import json
import uuid
import base64
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas
from PIL import Image

app = FastAPI(title="PDF Editor")

SESSIONS_DIR = Path("sessions")
SESSIONS_DIR.mkdir(exist_ok=True)

STATIC_DIR = Path("static")
STATIC_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")


def _session_path(session_id: str) -> Path:
    path = SESSIONS_DIR / session_id
    path.mkdir(exist_ok=True)
    return path


def _original_path(session_id: str) -> Path:
    return _session_path(session_id) / "original.pdf"


def _document_path(session_id: str) -> Path:
    return _session_path(session_id) / "document.pdf"


def _elements_path(session_id: str) -> Path:
    return _session_path(session_id) / "elements.json"


def _metadata_path(session_id: str) -> Path:
    return _session_path(session_id) / "metadata.json"


def _load_elements(session_id: str) -> list:
    p = _elements_path(session_id)
    return json.loads(p.read_text()) if p.exists() else []


def _save_elements(session_id: str, elements: list):
    _elements_path(session_id).write_text(json.dumps(elements, indent=2))


def _load_metadata(session_id: str) -> dict:
    p = _metadata_path(session_id)
    return json.loads(p.read_text()) if p.exists() else {}


def _save_metadata(session_id: str, metadata: dict):
    _metadata_path(session_id).write_text(json.dumps(metadata, indent=2))


def _render(session_id: str):
    """Rebuild document.pdf by compositing all elements onto original.pdf."""
    original = _original_path(session_id)
    elements = _load_elements(session_id)
    reader = PdfReader(str(original))

    by_page: dict[int, list] = {}
    for el in elements:
        by_page.setdefault(el["page"], []).append(el)

    writer = PdfWriter()
    modified_indices = []
    for i, page in enumerate(reader.pages):
        page_num = i + 1
        pw = float(page.mediabox.width)
        ph = float(page.mediabox.height)

        if page_num in by_page:
            packet = io.BytesIO()
            c = canvas.Canvas(packet, pagesize=(pw, ph))
            for el in by_page[page_num]:
                if el["type"] == "text":
                    try:
                        color = HexColor(el.get("font_color", "#000000"))
                    except Exception:
                        color = HexColor("#000000")
                    c.setFillColor(color)
                    c.setFont(el.get("font_name", "Helvetica"), el.get("font_size", 14))
                    c.drawString(el["x"], ph - el["y"], el["text"])
                elif el["type"] == "signature":
                    img_path = _session_path(session_id) / el["image_file"]
                    if img_path.exists():
                        w, h = el.get("width", 150), el.get("height", 75)
                        c.drawImage(str(img_path), el["x"], ph - el["y"] - h,
                                    width=w, height=h, mask="auto")
                elif el["type"] == "symbol":
                    try:
                        color = HexColor(el.get("color", "#000000"))
                    except Exception:
                        color = HexColor("#000000")
                    c.setFillColor(color)
                    size = el.get("size", 24)
                    c.setFont("Helvetica", size)
                    symbol_char = "✓" if el.get("symbol") == "tick" else "✕"
                    c.drawString(el["x"], ph - el["y"], symbol_char)
            c.save()
            packet.seek(0)
            page.merge_page(PdfReader(packet).pages[0])
            modified_indices.append(i)

        writer.add_page(page)

    for i in modified_indices:
        writer.pages[i].compress_content_streams()

    out = io.BytesIO()
    writer.write(out)
    _document_path(session_id).write_bytes(out.getvalue())


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse("static/index.html")


@app.post("/upload")
async def upload_pdf(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    session_id = str(uuid.uuid4())
    content = await file.read()

    _original_path(session_id).write_bytes(content)
    _document_path(session_id).write_bytes(content)
    _save_elements(session_id, [])
    _save_metadata(session_id, {"filename": file.filename})

    reader = PdfReader(io.BytesIO(content))
    first = reader.pages[0]
    pages = []
    for p in reader.pages:
        pages.append({"width": float(p.mediabox.width), "height": float(p.mediabox.height)})

    return {
        "session_id": session_id,
        "filename": file.filename,
        "page_count": len(reader.pages),
        "pages": pages,
    }


@app.get("/elements/{session_id}")
async def list_elements(session_id: str):
    if not _original_path(session_id).exists():
        raise HTTPException(status_code=404, detail="Session not found")
    return _load_elements(session_id)


@app.post("/add-text")
async def add_text(
    session_id: str = Form(...),
    page_number: int = Form(...),
    text: str = Form(...),
    x: float = Form(...),
    y: float = Form(...),
    font_size: float = Form(14),
    font_color: str = Form("#000000"),
    font_name: str = Form("Helvetica"),
):
    if not _original_path(session_id).exists():
        raise HTTPException(status_code=404, detail="Session not found")

    element = {
        "id": str(uuid.uuid4()),
        "type": "text",
        "page": page_number,
        "x": x,
        "y": y,
        "text": text,
        "font_size": font_size,
        "font_color": font_color,
        "font_name": font_name,
    }
    elements = _load_elements(session_id)
    elements.append(element)
    _save_elements(session_id, elements)
    _render(session_id)
    return element


@app.post("/add-signature")
async def add_signature(
    session_id: str = Form(...),
    page_number: int = Form(...),
    x: float = Form(...),
    y: float = Form(...),
    width: float = Form(150),
    height: float = Form(75),
    image: UploadFile = File(...),
):
    if not _original_path(session_id).exists():
        raise HTTPException(status_code=404, detail="Session not found")

    img_content = await image.read()
    img = Image.open(io.BytesIO(img_content)).convert("RGBA")
    bg = Image.new("RGBA", img.size, (255, 255, 255, 0))
    img = Image.alpha_composite(bg, img)

    image_filename = f"sig_{uuid.uuid4().hex}.png"
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    (_session_path(session_id) / image_filename).write_bytes(buf.getvalue())

    element = {
        "id": str(uuid.uuid4()),
        "type": "signature",
        "page": page_number,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "image_file": image_filename,
    }
    elements = _load_elements(session_id)
    elements.append(element)
    _save_elements(session_id, elements)
    _render(session_id)
    return element


@app.post("/add-symbol")
async def add_symbol(
    session_id: str = Form(...),
    page_number: int = Form(...),
    symbol: str = Form(...),
    x: float = Form(...),
    y: float = Form(...),
    size: float = Form(24),
    color: str = Form("#000000"),
):
    if not _original_path(session_id).exists():
        raise HTTPException(status_code=404, detail="Session not found")

    element = {
        "id": str(uuid.uuid4()),
        "type": "symbol",
        "page": page_number,
        "symbol": symbol,
        "x": x,
        "y": y,
        "size": size,
        "color": color,
    }
    elements = _load_elements(session_id)
    elements.append(element)
    _save_elements(session_id, elements)
    _render(session_id)
    return element


@app.put("/update-element/{session_id}/{element_id}")
async def update_element(session_id: str, element_id: str, updates: dict):
    if not _original_path(session_id).exists():
        raise HTTPException(status_code=404, detail="Session not found")

    elements = _load_elements(session_id)
    protected = {"id", "type", "image_file"}
    for el in elements:
        if el["id"] == element_id:
            for k, v in updates.items():
                if k not in protected:
                    el[k] = v
            _save_elements(session_id, elements)
            _render(session_id)
            return el

    raise HTTPException(status_code=404, detail="Element not found")


@app.delete("/delete-element/{session_id}/{element_id}")
async def delete_element(session_id: str, element_id: str):
    if not _original_path(session_id).exists():
        raise HTTPException(status_code=404, detail="Session not found")

    elements = _load_elements(session_id)
    remaining, deleted = [], None
    for el in elements:
        if el["id"] == element_id:
            deleted = el
            if el["type"] == "signature":
                (_session_path(session_id) / el["image_file"]).unlink(missing_ok=True)
        else:
            remaining.append(el)

    if deleted is None:
        raise HTTPException(status_code=404, detail="Element not found")

    _save_elements(session_id, remaining)
    _render(session_id)
    return {"status": "ok"}


@app.get("/preview/{session_id}")
async def preview(session_id: str):
    doc = _document_path(session_id)
    if not doc.exists():
        raise HTTPException(status_code=404, detail="Session not found")
    return JSONResponse({"pdf_base64": base64.b64encode(doc.read_bytes()).decode()})


@app.get("/download/{session_id}")
async def download(session_id: str):
    doc = _document_path(session_id)
    if not doc.exists():
        raise HTTPException(status_code=404, detail="Session not found")
    metadata = _load_metadata(session_id)
    filename = metadata.get("filename", "document.pdf")
    return FileResponse(str(doc), media_type="application/pdf", filename=filename)
