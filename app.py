import io
import re
from dataclasses import dataclass
from typing import List, Dict, Any, Tuple
import streamlit as st
import pdfplumber
import pandas as pd
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import landscape, inch
from reportlab.lib import colors
from reportlab.lib.utils import simpleSplit

# ======================================================
# STREAMLIT CONFIG
# ======================================================
st.set_page_config(page_title="Amazon Towels — PDF Parser & 4×6 Labels", layout="wide")

# ======================================================
# CONSTANTS
# ======================================================
PAGE_SIZE = (6 * inch, 4 * inch)  # width, height (landscape will be applied in canvas)
LEFT_MARGIN = 0.35 * inch
RIGHT_MARGIN = 0.35 * inch
TOP_MARGIN = 0.5 * inch
BOTTOM_MARGIN = 0.25 * inch

# Font sizes per spec
FS_BUYER = 20
FS_INFO  = 11
FS_BUL   = 11
FS_FOOT  = 8

# English → Spanish color dictionary (extend as needed)
COLOR_ES = {
    "White":"Blanco","Black":"Negro","Gold":"Dorado","Silver":"Plateado","Red":"Rojo",
    "Blue":"Azul","Navy":"Azul Marino","Light Blue":"Azul Claro","Green":"Verde",
    "Pink":"Rosa","Hot Pink":"Rosa Fucsia","Lilac":"Lila","Purple":"Morado",
    "Yellow":"Amarillo","Beige":"Beige","Brown":"Marrón","Gray":"Gris","Grey":"Gris",
    "Orange":"Naranja","Mid Blue":"Azul Medio","Light Grey":"Gris Claro","Aqua":"Aguamarina"
}

# Product type mapping from SKU prefix
SKU_TYPE_MAP = {
    "Set-3Pcs": "3-Piece Towel Set",
    "Set-6Pcs": "6-Piece Towel Set",
    "HT-2Pcs" : "2-Piece Hand Towel Set",
    "BT-2Pcs" : "2-Piece Bath Towel Set",
    "BS-1Pcs" : "Oversized Bath Sheet"
}

# The customization keys we accept (prefixes on lines)
CUST_KEYS = [
    "Washcloth:", "Hand Towel:", "Bath Towel:", "Oversized Bath Sheet:", "Guest Towel:",
    # Some PDFs have First/Second naming for 6Pcs sets:
    "First Washcloth:", "Second Washcloth:",
    "First Hand Towel:", "Second Hand Towel:",
    "First Bath Towel:", "Second Bath Towel:"
]

# ======================================================
# HELPERS
# ======================================================
def derive_type_and_color_from_sku(sku: str) -> Tuple[str, str]:
    # SKU examples: Set-3Pcs-White, Set-6Pcs-Mid Blue, HT-2PCS-White (case differences)
    if not sku:
        return "", ""
    sku_clean = sku.strip()
    parts = re.split(r"[-–]", sku_clean)  # Split by hyphen
    if len(parts) >= 2:
        prefix = f"{parts[0]}-{parts[1]}".replace("2PCS","2Pcs").replace("1PCS","1Pcs")
        ptype = SKU_TYPE_MAP.get(prefix, prefix)
        # Color is usually the last token(s) after the prefix; join the rest minus ASIN bits
        color = sku_clean[len(prefix)+1:].strip()
    else:
        ptype, color = "", ""
    return ptype, color

def translate_thread_color(color_text: str) -> str:
    # Expect inputs like "Black (#000000)" or "Navy Blue (#4c5577)"
    base = color_text
    base = re.sub(r"\(#[0-9a-fA-F]{3,8}\)", "", base).strip()
    es = COLOR_ES.get(base, base)  # if unknown, keep same
    # Return "Español (English)" as spec suggests e.g., "Dorado (Gold)"
    return f"{es} ({base})"

def pick_quantity(block_text: str) -> int:
    # Try to find a Quantity value in the block (look for a standalone integer on a 'Quantity' line)
    m = re.search(r"\bQuantity\s+(\d+)", block_text, flags=re.IGNORECASE)
    if m:
        return int(m.group(1))
    # Try "Item subtotal" patterns that start with quantity lines (rare). Default to 1.
    return 1

def find_field_value(lines: List[str], startswith_key: str) -> str:
    for ln in lines:
        if ln.strip().startswith(startswith_key):
            return ln.split(":",1)[-1].strip()
    return ""

def collect_customization_lines(lines: List[str]) -> List[str]:
    result = []
    for ln in lines:
        for key in CUST_KEYS:
            if ln.strip().startswith(key):
                value = ln.split(":",1)[-1].strip()
                # normalize "First/Second" labels by dropping those words for display clarity
                disp_key = key.replace("First ","").replace("Second ","").strip()
                if value:
                    result.append(f"{disp_key} {value}")
                else:
                    result.append(f"{disp_key}".rstrip(":"))
                break
    return result

def slice_blocks_by_order_id(text: str) -> List[str]:
    # Split the whole PDF text into blocks starting from "Order ID:" until next "Order ID:"
    # Ensure "Order ID:" remains in each block
    parts = re.split(r"(?=Order ID:\s*\d{3}-\d{7}-\d{7})", text)
    blocks = [p.strip() for p in parts if p.strip()]
    return blocks

def extract_items_from_block(block: str, source_name: str) -> List[Dict[str,Any]]:
    # Each block may contain multiple "SKU:" occurrences (multi-item orders)
    # We'll split by "SKU:" markers and parse per chunk.
    items = []
    # For global values in the block:
    lines = [ln for ln in block.splitlines() if ln.strip()]
    order_id = find_field_value(lines, "Order ID")
    buyer    = find_field_value(lines, "Buyer Name")
    gift_msg = ""
    for prefix in ("Gift Message", "Gift Bag"):
        val = find_field_value(lines, prefix)
        if val:
            gift_msg = f"{prefix}: {val}"
            break

    # Break on SKUs but keep the "SKU:" text in chunks
    chunks = re.split(r"(?=SKU:\s*)", block)
    for ch in chunks:
        if "SKU:" not in ch:
            continue
        ch_lines = [ln for ln in ch.splitlines() if ln.strip()]
        sku      = find_field_value(ch_lines, "SKU")
        if not sku:
            continue
        qty = pick_quantity(block)  # quantity usually appears above the product details once per item row

        # find font & thread color relative to this chunk
        font = find_field_value(ch_lines, "Choose Your Font")
        if not font:
            # Some formats show "Choose Embroidery Length" and "Embroidery Font" for blankets; keep optional
            font = find_field_value(ch_lines, "Embroidery Font")
        thread_color = find_field_value(ch_lines, "Font Color")
        if not thread_color:
            thread_color = find_field_value(ch_lines, "Thread Color")

        # customization lines
        cust = collect_customization_lines(ch_lines)

        # derive product type/color from SKU
        ptype, tcolor = derive_type_and_color_from_sku(sku)

        # normalize thread color display
        thread_disp = translate_thread_color(thread_color) if thread_color else ""

        items.append({
            "Order ID": order_id,
            "Buyer Name": buyer,
            "SKU": sku,
            "Product Type": ptype,
            "Towel Color": tcolor,
            "Font": font,
            "Thread Color": thread_disp,
            "Customization Lines": cust,
            "Quantity": qty,
            "Gift Message": gift_msg,
            "Source File": source_name
        })
    return items

def parse_pdfs_to_df(files: List[io.BytesIO]) -> pd.DataFrame:
    collected: List[Dict[str,Any]] = []
    for f in files:
        fname = getattr(f, "name", "uploaded.pdf")
        with pdfplumber.open(f) as pdf:
            full_text = []
            for page in pdf.pages:
                try:
                    full_text.append(page.extract_text() or "")
                except Exception:
                    full_text.append("")
            doc_text = "\n".join(full_text)
        # Split by Order ID blocks and parse
        for block in slice_blocks_by_order_id(doc_text):
            collected.extend(extract_items_from_block(block, fname))
    df = pd.DataFrame(collected, columns=[
        "Order ID","Buyer Name","SKU","Product Type","Towel Color","Font",
        "Thread Color","Customization Lines","Quantity","Gift Message","Source File"
    ])
    # If any rows have empty Order ID (malformed split), drop them
    df = df[df["Order ID"].astype(str).str.contains(r"\d{3}-\d{7}-\d{7}", na=False)].reset_index(drop=True)
    return df

# ======================================================
# LABEL RENDERING
# ======================================================
def draw_wrapped_text(c: canvas.Canvas, text: str, x: float, y: float, width: float, font_name="Helvetica", font_size=FS_INFO, bullet=False, leading=13):
    c.setFont(font_name, font_size)
    # Add a bullet if requested
    if bullet:
        # draw bullet as "• "
        text = f"• {text}"
    lines = simpleSplit(text, font_name, font_size, width)
    for i, ln in enumerate(lines):
        c.drawString(x, y - i * leading, ln)
    # return the y coordinate after drawing (last baseline)
    return y - (len(lines)) * leading

def generate_labels_pdf(df: pd.DataFrame) -> bytes:
    # One page per line item
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=landscape(PAGE_SIZE))
    page_w, page_h = landscape(PAGE_SIZE)

    for _, row in df.iterrows():
        # margins and writable area
        x0 = LEFT_MARGIN
        x1 = page_w - RIGHT_MARGIN
        y_top = page_h - TOP_MARGIN
        y_bot = BOTTOM_MARGIN
        width = x1 - x0

        # Section 1 — Header (compact top bar): Order ID + Qty; Buyer below as headline
        header1 = f"Order ID: {row['Order ID']}     Qty: {row.get('Quantity', 1)}"
        header2 = f"Buyer: {row.get('Buyer Name','').strip()}"
        c.setFont("Helvetica", FS_INFO)
        c.drawString(x0, y_top, header1)
        y = y_top - 16
        c.setFont("Helvetica-Bold", FS_BUYER)
        c.drawString(x0, y, header2)
        y -= 10  # space

        # Section 2 — Product Details (enlarged text)
        c.setFont("Helvetica-Bold", FS_INFO)
        y -= 6
        y = draw_wrapped_text(c, f"Towel Type: {row.get('Product Type','')}", x0, y, width, font_name="Helvetica-Bold", font_size=FS_INFO)
        y -= 2
        y = draw_wrapped_text(c, f"Towel Color: {row.get('Towel Color','')}", x0, y, width, font_name="Helvetica-Bold", font_size=FS_INFO)
        y -= 2
        thread = row.get("Thread Color","")
        if thread:
            y = draw_wrapped_text(c, f"Thread Color: {thread}", x0, y, width, font_name="Helvetica-Bold", font_size=FS_INFO)
        y -= 6

        # Section 3 — Customization (expanded middle area) bullets, up to 6 lines, wrap if long
        c.setFont("Helvetica", FS_BUL)
        custom_lines = row.get("Customization Lines", []) or []
        # Keep at most 6 visible lines as spec suggests; if more, they will still wrap within width
        max_lines = 6
        y -= 2
        for i, cl in enumerate(custom_lines[:max_lines]):
            y = draw_wrapped_text(c, cl, x0, y, width, font_name="Helvetica", font_size=FS_BUL, bullet=True, leading=14)
            y -= 2

        # Section 4 — Gift Options (optional). Show "Gift Wrap: NO / Gift Note: YES" based on presence.
        gift_text = str(row.get("Gift Message","")).strip()
        if gift_text:
            y -= 6
            c.setFont("Helvetica", FS_INFO)
            y = draw_wrapped_text(c, "Gift Wrap: NO", x0, y, width, font_name="Helvetica", font_size=FS_INFO)
            y -= 2
            y = draw_wrapped_text(c, "Gift Note: YES", x0, y, width, font_name="Helvetica", font_size=FS_INFO)
            # If you want to include a small snippet of the note itself, uncomment below:
            # y = draw_wrapped_text(c, gift_text, x0+10, y, width-10, font_name="Helvetica-Oblique", font_size=FS_INFO)
        # else: don't show the section

        # Section 5 — Footer (gray text, right-aligned, source file name)
        c.setFont("Helvetica", FS_FOOT)
        c.setFillColor(colors.grey)
        src = f"Source: {row.get('Source File','')}"
        tw = c.stringWidth(src, "Helvetica", FS_FOOT)
        c.drawString(x1 - tw, y_bot, src)
        c.setFillColor(colors.black)

        c.showPage()

    c.save()
    buf.seek(0)
    return buf.getvalue()

# ======================================================
# UI
# ======================================================
st.title("Amazon Towels — Packing Slip Parser & 4×6 Label Builder")

tab1, tab2 = st.tabs(["Upload & Parse PDFs", "Generate Manufacturing Labels"])

with tab1:
    st.subheader("1) Upload your Amazon packing slip PDFs")
    upload_files = st.file_uploader("Upload one or more PDFs", type=["pdf"], accept_multiple_files=True)

    if st.button("Parse PDFs"):
        if not upload_files:
            st.warning("Please upload at least one PDF.")
        else:
            df = parse_pdfs_to_df(upload_files)
            st.success(f"Parsed {len(df)} line items.")
            st.dataframe(df, use_container_width=True)
            st.session_state["parsed_df"] = df

with tab2:
    st.subheader("2) Build Manufacturing Labels (4×6 in Landscape)")
    st.caption("One page per SKU line item. Uses 0.35\" left/right margins, 0.5\" top, 0.25\" bottom.")
    if "parsed_df" not in st.session_state:
        st.info("Parse PDFs first on the previous tab.")
    else:
        df: pd.DataFrame = st.session_state["parsed_df"]
        if st.button("Build Manufacturing Labels PDF"):
            pdf_bytes = generate_labels_pdf(df)
            st.download_button(
                label="Download Labels PDF",
                data=pdf_bytes,
                file_name="manufacturing_labels_4x6.pdf",
                mime="application/pdf"
            )
