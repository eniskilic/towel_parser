import io
import re
import streamlit as st

st.set_page_config(page_title="Test Clean File", layout="wide")

st.write("✅ Your Streamlit environment is clean and working!")



# ================================
# STREAMLIT CONFIG
# ================================
st.set_page_config(page_title="Amazon Towels - PDF Parser & 4x6 Labels", layout="wide")


# ================================
# CONSTANTS
# ================================
PAGE_SIZE = (6 * inch, 4 * inch)  # width, height
LEFT_MARGIN = 0.35 * inch
RIGHT_MARGIN = 0.35 * inch
TOP_MARGIN = 0.5 * inch
BOTTOM_MARGIN = 0.25 * inch

FS_BUYER = 20  # Buyer headline
FS_INFO = 11   # Order, product info, gift lines
FS_BUL = 11    # Customization bullets
FS_FOOT = 8    # Footer

# English -> Spanish color names (extend as needed)
COLOR_ES = {
    "White": "Blanco",
    "Black": "Negro",
    "Gold": "Dorado",
    "Silver": "Plateado",
    "Red": "Rojo",
    "Blue": "Azul",
    "Navy": "Azul Marino",
    "Light Blue": "Azul Claro",
    "Green": "Verde",
    "Pink": "Rosa",
    "Hot Pink": "Rosa Fucsia",
    "Lilac": "Lila",
    "Purple": "Morado",
    "Yellow": "Amarillo",
    "Beige": "Beige",
    "Brown": "Marrón",
    "Gray": "Gris",
    "Grey": "Gris",
    "Orange": "Naranja",
    "Mid Blue": "Azul Medio",
    "Light Grey": "Gris Claro",
    "Aqua": "Aguamarina",
}

# Map SKU prefix to product type
SKU_TYPE_MAP = {
    "Set-3Pcs": "3-Piece Towel Set",
    "Set-6Pcs": "6-Piece Towel Set",
    "HT-2Pcs": "2-Piece Hand Towel Set",
    "BT-2Pcs": "2-Piece Bath Towel Set",
    "BS-1Pcs": "Oversized Bath Sheet",
}

# Customization line starters we capture
CUST_KEYS = [
    "Washcloth:", "Hand Towel:", "Bath Towel:", "Oversized Bath Sheet:", "Guest Towel:",
    "First Washcloth:", "Second Washcloth:",
    "First Hand Towel:", "Second Hand Towel:",
    "First Bath Towel:", "Second Bath Towel:",
]


# ================================
# HELPERS
# ================================
def derive_type_and_color_from_sku(sku: str) -> Tuple[str, str]:
    """
    Example SKUs:
    - Set-3Pcs-White
    - Set-6Pcs-Mid Blue
    - HT-2PCS-White
    - BT-2Pcs-Beige
    - BS-1Pcs-Lilac
    """
    if not sku:
        return "", ""
    s = sku.strip()
    parts = re.split(r"[-–]", s)  # split by hyphen types
    if len(parts) < 2:
        return "", ""
    prefix = f"{parts[0]}-{parts[1]}"
    # normalize PCS capitalization
    prefix = prefix.replace("2PCS", "2Pcs").replace("1PCS", "1Pcs")
    ptype = SKU_TYPE_MAP.get(prefix, prefix)
    # everything after prefix and a hyphen is the color
    color = s[len(prefix) + 1:].strip() if len(s) > len(prefix) + 1 else ""
    return ptype, color


def translate_thread_color(raw: str) -> str:
    """
    Input like: 'White (#ffffff)' or 'Navy (#123456)' or 'Gold'
    Output: 'Blanco (White)' or 'Azul Marino (Navy)' etc.
    """
    if not raw:
        return ""
    base = re.sub(r"\(#[0-9a-fA-F]{3,8}\)", "", str(raw)).strip()
    es = COLOR_ES.get(base, base)
    return f"{es} ({base})"


def find_field_value(lines: List[str], startswith_key: str) -> str:
    """
    Return text after 'Key:' on the line that starts with that key.
    """
    k = startswith_key.strip()
    for ln in lines:
        s = ln.strip()
        if s.lower().startswith(k.lower()):
            return s.split(":", 1)[-1].strip()
    return ""


def collect_customization_lines(lines: List[str]) -> List[str]:
    """
    Collect all customization lines based on the known prefixes (CUST_KEYS).
    """
    out: List[str] = []
    for ln in lines:
        s = ln.strip()
        for key in CUST_KEYS:
            if s.startswith(key):
                value = s.split(":", 1)[-1].strip()
                # remove "First"/"Second" labels in the display key
                disp_key = key.replace("First ", "").replace("Second ", "").strip()
                if value:
                    out.append(f"{disp_key} {value}")
                else:
                    out.append(disp_key.rstrip(":"))
                break
    return out


def slice_blocks_by_order_id(text: str) -> List[str]:
    """
    Split entire document text into blocks per order using the Order ID anchor.
    Keeps the Order ID at the start of each block.
    """
    parts = re.split(r"(?=Order ID:\s*\d{3}-\d{7}-\d{7})", text)
    return [p.strip() for p in parts if p.strip()]


def detect_quantity(chunk_text: str, fallback_block_text: str) -> int:
    """
    Try to find 'Quantity <n>' inside the item chunk; if not, try in the surrounding block.
    Default to 1 if missing.
    """
    m = re.search(r"\bQuantity\s+(\d+)", chunk_text, flags=re.IGNORECASE)
    if not m:
        m = re.search(r"\bQuantity\s+(\d+)", fallback_block_text, flags=re.IGNORECASE)
    return int(m.group(1)) if m else 1


def extract_items_from_block(block: str, source_name: str) -> List[Dict[str, Any]]:
    """
    Given an order block, split into SKU chunks and extract per-item fields.
    """
    items: List[Dict[str, Any]] = []

    lines = [ln for ln in block.splitlines() if ln.strip()]
    order_id = find_field_value(lines, "Order ID")
    buyer = find_field_value(lines, "Buyer Name")

    # Prefer Gift Message, but accept Gift Bag as an alternative marker
    gift_msg = ""
    for gift_key in ("Gift Message", "Gift Bag"):
        val = find_field_value(lines, gift_key)
        if val:
            gift_msg = f"{gift_key}: {val}"
            break

    # Split by SKU chunks (keep the delimiter)
    chunks = re.split(r"(?=SKU:\s*)", block)
    for ch in chunks:
        if "SKU:" not in ch:
            continue

        ch_lines = [ln for ln in ch.splitlines() if ln.strip()]
        sku = find_field_value(ch_lines, "SKU")
        if not sku:
            continue

        qty = detect_quantity(ch, block)

        # Font and thread color
        font = find_field_value(ch_lines, "Choose Your Font")
        if not font:
            font = find_field_value(ch_lines, "Embroidery Font")

        thread_color_raw = find_field_value(ch_lines, "Font Color")
        if not thread_color_raw:
            thread_color_raw = find_field_value(ch_lines, "Thread Color")
        thread_color = translate_thread_color(thread_color_raw) if thread_color_raw else ""

        # Customization lines
        cust_lines = collect_customization_lines(ch_lines)

        # Derive product type and towel color from SKU
        product_type, towel_color = derive_type_and_color_from_sku(sku)

        items.append(
            {
                "Order ID": order_id,
                "Buyer Name": buyer,
                "SKU": sku,
                "Product Type": product_type,
                "Towel Color": towel_color,
                "Font": font,
                "Thread Color": thread_color,
                "Customization Lines": cust_lines,
                "Quantity": qty,
                "Gift Message": gift_msg,
                "Source File": source_name,
            }
        )

    return items


def parse_pdfs_to_df(files: List[io.BytesIO]) -> pd.DataFrame:
    """
    Open each PDF, extract full text with pdfplumber, slice by order, then by SKU items.
    Build a DataFrame with the required columns.
    """
    records: List[Dict[str, Any]] = []

    for f in files:
        fname = getattr(f, "name", "uploaded.pdf")
        try:
            with pdfplumber.open(f) as pdf:
                pages_text: List[str] = []
                for page in pdf.pages:
                    try:
                        pages_text.append(page.extract_text() or "")
                    except Exception:
                        pages_text.append("")
                doc_text = "\n".join(pages_text)
        except Exception:
            # If a single file fails to open, skip it but do not crash
            continue

        for block in slice_blocks_by_order_id(doc_text):
            records.extend(extract_items_from_block(block, fname))

    df = pd.DataFrame(
        records,
        columns=[
            "Order ID",
            "Buyer Name",
            "SKU",
            "Product Type",
            "Towel Color",
            "Font",
            "Thread Color",
            "Customization Lines",
            "Quantity",
            "Gift Message",
            "Source File",
        ],
    )

    # Keep rows that have a valid Order ID pattern
    if not df.empty:
        mask = df["Order ID"].astype(str).str.contains(r"\d{3}-\d{7}-\d{7}", regex=True, na=False)
        df = df[mask].reset_index(drop=True)

    return df


# ================================
# LABEL RENDERING
# ================================
def draw_wrapped_text(
    c: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    width: float,
    font_name: str = "Helvetica",
    font_size: int = FS_INFO,
    leading: int = 13,
):
    """
    Draw wrapped text within the given width. Returns the next y coordinate (after the last line).
    """
    c.setFont(font_name, font_size)
    lines = simpleSplit(text, font_name, font_size, width)
    for i, ln in enumerate(lines):
        c.drawString(x, y - i * leading, ln)
    return y - len(lines) * leading


def generate_labels_pdf(df: pd.DataFrame) -> bytes:
    """
    Create a multi-page PDF with one 4x6 landscape page per DataFrame row.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=landscape(PAGE_SIZE))
    page_w, page_h = landscape(PAGE_SIZE)

    for _, row in df.iterrows():
        x0 = LEFT_MARGIN
        x1 = page_w - RIGHT_MARGIN
        y_top = page_h - TOP_MARGIN
        y_bot = BOTTOM_MARGIN
        width = x1 - x0

        # Section 1 - Header (compact top bar)
        header_1 = f"Order ID: {row.get('Order ID', '')}     Qty: {row.get('Quantity', 1)}"
        header_2 = f"Buyer: {str(row.get('Buyer Name', '')).strip()}"
        c.setFont("Helvetica", FS_INFO)
        c.drawString(x0, y_top, header_1)
        y = y_top - 16
        c.setFont("Helvetica-Bold", FS_BUYER)
        c.drawString(x0, y, header_2)
        y -= 10  # spacing

        # Section 2 - Product Details (enlarged text)
        c.setFont("Helvetica-Bold", FS_INFO)
        y -= 6
        y = draw_wrapped_text(c, f"Towel Type: {row.get('Product Type', '')}", x0, y, width, font_name="Helvetica-Bold", font_size=FS_INFO)
        y -= 2
        y = draw_wrapped_text(c, f"Towel Color: {row.get('Towel Color', '')}", x0, y, width, font_name="Helvetica-Bold", font_size=FS_INFO)
        y -= 2
        thread_disp = str(row.get("Thread Color", "") or "")
        if thread_disp:
            y = draw_wrapped_text(c, f"Thread Color: {thread_disp}", x0, y, width, font_name="Helvetica-Bold", font_size=FS_INFO)
        y -= 6

        # Section 3 - Customization (expanded middle area, up to 6 visible lines)
        custom_lines = row.get("Customization Lines", []) or []
        max_lines = 6
        c.setFont("Helvetica", FS_BUL)
        y -= 2
        for cl in custom_lines[:max_lines]:
            # Use ASCII-safe bullet for reliability
            y = draw_wrapped_text(c, f"- {cl}", x0, y, width, font_name="Helvetica", font_size=FS_BUL, leading=14)
            y -= 2

        # Section 4 - Gift Options (optional)
        gift_text = str(row.get("Gift Message", "") or "").strip()
        if gift_text:
            y -= 6
            c.setFont("Helvetica", FS_INFO)
            y = draw_wrapped_text(c, "Gift Wrap: NO", x0, y, width, font_name="Helvetica", font_size=FS_INFO)
            y -= 2
            y = draw_wrapped_text(c, "Gift Note: YES", x0, y, width, font_name="Helvetica", font_size=FS_INFO)
            # If you want to print a small excerpt of the note, uncomment the next line:
            # y = draw_wrapped_text(c, gift_text, x0 + 10, y, width - 10, font_name="Helvetica-Oblique", font_size=FS_INFO)

        # Section 5 - Footer (small gray, right aligned)
        c.setFont("Helvetica", FS_FOOT)
        c.setFillColor(colors.grey)
        src = f"Source: {row.get('Source File', '')}"
        tw = c.stringWidth(src, "Helvetica", FS_FOOT)
        c.drawString(x1 - tw, y_bot, src)
        c.setFillColor(colors.black)

        c.showPage()

    c.save()
    buf.seek(0)
    return buf.getvalue()


# ================================
# UI
# ================================
st.title("Amazon Towels - Packing Slip Parser & 4x6 Label Builder")

tab1, tab2 = st.tabs(["Upload & Parse PDFs", "Generate Manufacturing Labels"])

with tab1:
    st.subheader("1) Upload your Amazon packing slip PDFs")
    uploaded = st.file_uploader("Upload one or more PDFs", type=["pdf"], accept_multiple_files=True)

    if st.button("Parse PDFs"):
        if not uploaded:
            st.warning("Please upload at least one PDF.")
        else:
            df = parse_pdfs_to_df(uploaded)
            st.success(f"Parsed {len(df)} line items.")
            st.dataframe(df, use_container_width=True)
            st.session_state["parsed_df"] = df

with tab2:
    st.subheader("2) Build Manufacturing Labels (4x6 in Landscape)")
    st.caption('One page per SKU line item. Margins: 0.35" left/right, 0.5" top, 0.25" bottom.')
    if "parsed_df" not in st.session_state:
        st.info("Parse PDFs first on the previous tab.")
    else:
        df = st.session_state["parsed_df"]
        if st.button("Build Manufacturing Labels PDF"):
            pdf_bytes = generate_labels_pdf(df)
            st.download_button(
                label="Download Labels PDF",
                data=pdf_bytes,
                file_name="manufacturing_labels_4x6.pdf",
                mime="application/pdf",
            )
