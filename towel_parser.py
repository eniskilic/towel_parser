import io
import re
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Tuple, Optional
from collections import defaultdict
import pdfplumber
import pandas as pd
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import landscape
from reportlab.lib.units import inch
from reportlab.lib import colors
import streamlit as st

# ======================================================
# CONFIG
# ======================================================
st.set_page_config(page_title="Amazon Towel Orders — Parser & 6x4 Labels", layout="wide")

# ======================================================
# MODELS
# ======================================================
@dataclass
class LineItem:
    order_id: str
    order_date: str
    shipping_service: str
    buyer_full_name: str
    sku: str
    product_type: str
    color: str
    quantity: int
    font: str
    thread_color: str
    customization: Dict[str, str]

# ======================================================
# CONSTANTS / REGEX
# ======================================================
ORDER_ID_RE = re.compile(r"^Order ID:\s*([0-9\-]+)(?:\s*Custom Order)?", re.IGNORECASE)
ORDER_DATE_RE = re.compile(r"^Order Date:\s*$", re.IGNORECASE)
SHIPPING_SERVICE_RE = re.compile(r"^Shipping Service:\s*$", re.IGNORECASE)
SHIP_TO_RE = re.compile(r"^Ship To:\s*$", re.IGNORECASE)
SKU_RE = re.compile(r"^SKU:\s*(.+)$", re.IGNORECASE)
CHOOSE_FONT_RE = re.compile(r"^Choose Your Font:\s*(.+)$", re.IGNORECASE)
FONT_COLOR_RE = re.compile(r"^Font Color:\s*([A-Za-z ]+)\s*(?:\(|$)", re.IGNORECASE)
STANDALONE_QTY_RE = re.compile(r"^\s*(\d+)\s*$")
CUSTOMIZATIONS_HEADER_RE = re.compile(r"^Customizations:\s*$", re.IGNORECASE)

# map sku prefix -> human readable type and expected pieces
PRODUCT_MAP = {
    "Set-6Pcs": ("6-pc Set", ["First Washcloth", "Second Washcloth",
                               "First Hand Towel", "Second Hand Towel",
                               "First Bath Towel", "Second Bath Towel"]),
    "Set-3Pcs": ("3-pc Set", ["Washcloth", "Hand Towel", "Bath Towel"]),
    "HT-2PCS": ("2-pc Hand Towels", ["First Hand Towel", "Second Hand Towel"]),
    "HT-2Pcs": ("2-pc Hand Towels", ["First Hand Towel", "Second Hand Towel"]),  # guard mixed case
    "BT-2Pcs": ("2-pc Bath Towels", ["First Bath Towel", "Second Bath Towel"]),
    "BS-1Pcs": ("Bath Sheet (Oversized)", ["Oversized Bath Sheet"]),
}

SIZE_CONVERSIONS = {
    "Washcloth": "Washcloth (Small)",
    "Hand Towel": "Hand Towel (Medium)",
    "Bath Towel": "Bath Towel (Large)",
    "Bath Sheet": "Bath Sheet (Oversized)",
}

# ======================================================
# HELPERS
# ======================================================
def extract_text_lines(pdf_bytes: bytes) -> List[str]:
    """Return a flat list of lines for the whole PDF (page order kept)."""
    lines: List[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            page_lines = [ln.rstrip() for ln in txt.splitlines()]
            lines.extend(page_lines)
    return lines

def is_order_header(line: str) -> Optional[str]:
    m = ORDER_ID_RE.match(line.strip())
    return m.group(1) if m else None

def parse_order_date(lines: List[str], start_idx: int) -> str:
    # Look ahead for "Order Date:" then take the next non-empty line
    for i in range(start_idx, min(start_idx + 40, len(lines))):
        if ORDER_DATE_RE.match(lines[i].strip()):
            # next non-empty line
            for j in range(i+1, min(i+6, len(lines))):
                if lines[j].strip():
                    return lines[j].strip().rstrip(",")
            break
    return ""

def parse_shipping_service(lines: List[str], start_idx: int) -> str:
    for i in range(start_idx, min(start_idx + 40, len(lines))):
        if SHIPPING_SERVICE_RE.match(lines[i].strip()):
            for j in range(i+1, min(i+6, len(lines))):
                if lines[j].strip():
                    return lines[j].strip()
            break
    return ""

def parse_buyer_full_name(lines: List[str], start_idx: int) -> str:
    """Find the 'Ship To:' block and return the first line after it as the full name."""
    for i in range(start_idx, min(start_idx + 80, len(lines))):
        if SHIP_TO_RE.match(lines[i].strip()):
            # next non-empty line should be name
            for j in range(i+1, min(i+6, len(lines))):
                if lines[j].strip():
                    return lines[j].strip()
            break
    return ""

def split_orders(lines: List[str]) -> List[Tuple[int, int]]:
    """Return list of (start_idx, end_idx_exclusive) spans for each order using 'Order ID' headers only."""
    headers = []
    for i, ln in enumerate(lines):
        if is_order_header(ln):
            headers.append(i)
    spans = []
    for idx, start in enumerate(headers):
        end = headers[idx+1] if idx+1 < len(headers) else len(lines)
        spans.append((start, end))
    return spans

def parse_sku_color(sku_line: str) -> Tuple[str, str]:
    # sku like "Set-6Pcs-Mid Blue" => product "Set-6Pcs", color "Mid Blue"
    sku = sku_line.strip()
    color = ""
    product = sku
    if "-" in sku:
        parts = sku.split("-")
        # join all but last as product (to keep Set-6Pcs etc.), last is color (may have spaces)
        product = "-".join(parts[:2]) if sku.startswith(("Set", "HT", "BT", "BS")) else "-".join(parts[:-1])
        color = parts[-1].replace("_", " ").replace("Grey", "Gray")  # normalize UK/US spelling if present
        # Special case: BT-2Pcs-MidBlue vs BT-2Pcs-Mid Blue
        if color and re.match(r"^[A-Za-z]+Blue$", color):
            color = color.replace("Blue", " Blue")
        if product.startswith("Set-"):
            # product already ok; color may be multi word if more hyphens; capture from last hyphen onward
            color = sku.split("-", 2)[-1] if sku.startswith("Set-") else color
            # remove leading subtype like "3Pcs-" inside the remainder if any
            if product == "Set-6Pcs" and color.startswith("6Pcs-"):
                color = color.split("-", 1)[-1]
            if product == "Set-3Pcs" and color.startswith("3Pcs-"):
                color = color.split("-", 1)[-1]
    return product, color.strip()

def guess_product_type(product: str) -> Tuple[str, List[str]]:
    for key, (display, pieces) in PRODUCT_MAP.items():
        if product.startswith(key):
            return display, pieces
    # Fallback for monogram SKUs like "NAVY - B" or "BLACK - D"
    return ("Monogram Towels", [])

def find_quantity_near(lines: List[str], idx: int) -> int:
    """Look backwards up to 10 lines for a standalone number before the SKU section."""
    for j in range(idx-1, max(idx-11, -1), -1):
        m = STANDALONE_QTY_RE.match(lines[j])
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
    return 1

def parse_customizations(lines: List[str], start_idx: int, product_display: str, expected_pieces: List[str]) -> Tuple[str, str, Dict[str, str]]:
    """From a 'Customizations:' section, return (font, thread_color, customization dict)."""
    font = ""
    thread = ""
    custom: Dict[str, str] = {}

    # We scan forward from start_idx until we hit a blank line or next order/SKU block boundary
    i = start_idx
    while i < len(lines):
        ln = lines[i].strip()
        if not ln:
            # allow sparse gaps; break if we pass 25 lines
            if i - start_idx > 25:
                break
            i += 1
            continue

        # stops: a new 'Order ID:' or another 'SKU:' indicates next block
        if ORDER_ID_RE.match(ln) or SKU_RE.match(ln):
            break

        m = CHOOSE_FONT_RE.match(ln)
        if m:
            font = m.group(1).strip()
            i += 1
            continue
        m = FONT_COLOR_RE.match(ln)
        if m:
            thread = m.group(1).strip()
            i += 1
            continue

        # piece lines like "First Hand Towel: Jojo"
        if ":" in ln:
            key, val = ln.split(":", 1)
            key = key.strip()
            val = val.strip()
            # keep only expected piece labels for structured products
            if expected_pieces:
                # Normalize similar variants (e.g., '2Pcs Bath Towel:' pre-headers can appear; ignore them)
                if key in expected_pieces or any(key.endswith(p.split()[0]) for p in expected_pieces):
                    custom[key] = val
            else:
                # Monogram or unknown: collect useful fields
                if key.lower() in {"first hand towel", "second hand towel",
                                   "first bath towel", "second bath towel",
                                   "washcloth", "hand towel", "bath towel",
                                   "oversized bath sheet"}:
                    custom[key] = val
        i += 1

    return font, thread, custom

def parse_orders_from_text(lines: List[str]) -> List[LineItem]:
    items: List[LineItem] = []
    order_spans = split_orders(lines)
    for start, end in order_spans:
        order_id_line = lines[start]
        order_id = is_order_header(order_id_line) or ""
        order_date = parse_order_date(lines, start)
        shipping_service = parse_shipping_service(lines, start)
        buyer_full_name = parse_buyer_full_name(lines, start)

        # within this span, find each SKU block
        for i in range(start, end):
            m = SKU_RE.match(lines[i].strip())
            if not m:
                continue
            sku_raw = m.group(1).strip()
            product_code, color = parse_sku_color(sku_raw)
            product_display, expected_pieces = guess_product_type(product_code)

            # Look backwards for quantity as standalone number
            qty = find_quantity_near(lines, i)

            # Find the nearest 'Customizations:' after this SKU line
            font = ""
            thread = ""
            customization: Dict[str, str] = {}
            # search forward up to next 40 lines
            for j in range(i, min(i+40, end)):
                if CUSTOMIZATIONS_HEADER_RE.match(lines[j].strip()):
                    font, thread, customization = parse_customizations(lines, j+1, product_display, expected_pieces)
                    break

            # Concatenate empty pieces if missing to keep columns consistent later
            items.append(LineItem(
                order_id=order_id,
                order_date=order_date,
                shipping_service=shipping_service,
                buyer_full_name=buyer_full_name,
                sku=sku_raw,
                product_type=product_display,
                color=color,
                quantity=qty,
                font=font,
                thread_color=thread,
                customization=customization
            ))
    return items

def rows_for_dashboard(items: List[LineItem]) -> pd.DataFrame:
    records: List[Dict[str, Any]] = []
    for it in items:
        # concat customization into one line "a | b | c" in the expected order if we know it
        cust_parts: List[str] = []
        # derive order from product type
        order_map = {
            "6-pc Set": ["First Washcloth", "Second Washcloth", "First Hand Towel",
                         "Second Hand Towel", "First Bath Towel", "Second Bath Towel"],
            "3-pc Set": ["Washcloth", "Hand Towel", "Bath Towel"],
            "2-pc Hand Towels": ["First Hand Towel", "Second Hand Towel"],
            "2-pc Bath Towels": ["First Bath Towel", "Second Bath Towel"],
            "Bath Sheet (Oversized)": ["Oversized Bath Sheet"],
        }
        sequence = order_map.get(it.product_type, [])
        if sequence:
            for k in sequence:
                v = it.customization.get(k, "")
                if v:
                    cust_parts.append(v)
        else:
            # unknown / monogram: join any values we have
            for k, v in it.customization.items():
                if v:
                    cust_parts.append(v)
        cust_str = " | ".join(cust_parts)

        records.append({
            "Order ID": it.order_id,
            "Order Date": it.order_date,
            "Buyer Name": it.buyer_full_name,
            "Product Type": it.product_type,
            "Color": it.color,
            "Quantity": it.quantity,
            "Font": it.font,
            "Thread Color": it.thread_color,
            "Customization": cust_str,
            "Shipping Service": it.shipping_service,
            "SKU": it.sku,
        })
    df = pd.DataFrame.from_records(records)
    # ensure consistent column order
    cols = ["Order ID","Order Date","Buyer Name","Product Type","Color","Quantity",
            "Font","Thread Color","Customization","Shipping Service","SKU"]
    df = df[cols]
    return df

def draw_label(c: canvas.Canvas, data: Dict[str, Any]):
    """Draw a single 6x4 landscape label on the given canvas at origin (0,0)."""
    # Page size 6x4 landscape
    width, height = (6 * inch, 4 * inch)

    # Layout: left and right columns
    margin = 0.3 * inch
    gutter = 0.2 * inch
    col_width = (width - 2*margin - gutter) / 2

    # Left column positions
    x_left = margin
    y_top = height - margin

    def text(x, y, s, size=12, bold=False, color=colors.black):
        c.setFillColor(color)
        if bold:
            c.setFont("Helvetica-Bold", size)
        else:
            c.setFont("Helvetica", size)
        c.drawString(x, y, s)

    # LEFT
    y = y_top
    text(x_left, y, f"BUYER: {data.get('buyer','')}", size=12, bold=False)
    y -= 16
    text(x_left, y, f"PRODUCT TYPE: {data.get('product_type','')}", size=12, bold=False)
    y -= 16
    text(x_left, y, f"COLOR: {data.get('color','')}", size=16, bold=True)
    y -= 20
    text(x_left, y, f"THREAD/FONT COLOR: {data.get('thread','')}", size=16, bold=True)
    y -= 20
    text(x_left, y, f"FONT: {data.get('font','')}", size=12, bold=False)
    y -= 16
    qty = data.get("quantity", 1)
    text(x_left, y, f"QUANTITY: {qty} Set(s)", size=14, bold=True)

    # RIGHT
    x_right = margin + col_width + gutter
    y = y_top
    text(x_right, y, "CUSTOMIZATION:", size=9, bold=True, color=colors.gray)
    y -= 14

    # Each line of customization, apply size conversions on label only
    for label, value in data.get("custom_lines", []):
        # label may contain "Washcloth", "Hand Towel", etc.
        conv_label = label
        if "Washcloth" in label:
            conv_label = SIZE_CONVERSIONS["Washcloth"]
        elif "Hand Towel" in label:
            conv_label = SIZE_CONVERSIONS["Hand Towel"]
        elif "Bath Towel" in label:
            conv_label = SIZE_CONVERSIONS["Bath Towel"]
        elif "Bath Sheet" in label:
            conv_label = SIZE_CONVERSIONS["Bath Sheet"]
        text(x_right, y, f"{conv_label}: {value}", size=13, bold=False)
        y -= 16

def make_labels_pdf(rows: pd.DataFrame) -> bytes:
    """Return bytes of a merged PDF with one 6x4 label per row."""
    buffer = io.BytesIO()
    # Use exact 6x4 inches landscape
    pagesize = (6 * inch, 4 * inch)
    c = canvas.Canvas(buffer, pagesize=pagesize)
    for _, r in rows.iterrows():
        custom_lines: List[Tuple[str, str]] = []
        # Reconstruct lines by product type to preserve order on label
        pt = r["Product Type"]
        # map to expected sequence
        order_map = {
            "6-pc Set": ["First Washcloth", "Second Washcloth", "First Hand Towel",
                         "Second Hand Towel", "First Bath Towel", "Second Bath Towel"],
            "3-pc Set": ["Washcloth", "Hand Towel", "Bath Towel"],
            "2-pc Hand Towels": ["First Hand Towel", "Second Hand Towel"],
            "2-pc Bath Towels": ["First Bath Towel", "Second Bath Towel"],
            "Bath Sheet (Oversized)": ["Oversized Bath Sheet"],
        }
        sequence = order_map.get(pt, [])
        # We don't store per-piece values in df (only concatenated string). Try to recover from SKU-based hints if needed.
        # Prefer to split concatenated "Customization" back (best-effort).
        values = [v.strip() for v in str(r["Customization"]).split("|")] if r["Customization"] else []
        for i, piece in enumerate(sequence):
            val = values[i] if i < len(values) else ""
            if val:
                custom_lines.append((piece, val))

        payload = {
            "buyer": r["Buyer Name"],
            "product_type": r["Product Type"],
            "color": r["Color"],
            "thread": r["Thread Color"],
            "font": r["Font"],
            "quantity": r["Quantity"],
            "custom_lines": custom_lines,
        }
        draw_label(c, payload)
        c.showPage()
    c.save()
    return buffer.getvalue()

def to_excel_bytes(df: pd.DataFrame) -> bytes:
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Orders")
        # basic formatting
        wb = writer.book
        ws = writer.sheets["Orders"]
        for idx, col in enumerate(df.columns, 1):
            width = max(12, min(40, int(df[col].astype(str).map(len).max() or 12) + 2))
            ws.set_column(idx-1, idx-1, width)
    return out.getvalue()

# ======================================================
# UI
# ======================================================
st.title("Amazon Towel Orders — Parser & 6×4 Labels")

st.markdown(
    "Upload Amazon packing slip PDF(s). The app parses **Order ID, Date, Ship To (full name), Shipping Service,** "
    "and each item's **SKU, Quantity, Font, Thread Color, Customizations**. "
    "Multi-item orders are split into separate rows/labels."
)

uploaded_files = st.file_uploader("Upload PDF(s) from Amazon Seller Central", type=["pdf"], accept_multiple_files=True)

if uploaded_files:
    with st.spinner("Parsing PDFs…"):
        all_items: List[LineItem] = []
        for uf in uploaded_files:
            pdf_bytes = uf.read()
            lines = extract_text_lines(pdf_bytes)
            all_items.extend(parse_orders_from_text(lines))

    if not all_items:
        st.warning("No orders detected. Make sure these are Amazon packing slips.")
        st.stop()

    df = rows_for_dashboard(all_items)

    # Filters
    col1, col2 = st.columns(2)
    with col1:
        product_choices = ["(All)"] + sorted([p for p in df["Product Type"].dropna().unique()])
        sel_product = st.selectbox("Filter by Product Type", product_choices, index=0)
    with col2:
        color_choices = ["(All)"] + sorted([c for c in df["Color"].dropna().unique()])
        sel_color = st.selectbox("Filter by Color", color_choices, index=0)

    df_view = df.copy()
    if sel_product != "(All)":
        df_view = df_view[df_view["Product Type"] == sel_product]
    if sel_color != "(All)":
        df_view = df_view[df_view["Color"] == sel_color]

    st.subheader("Operations Dashboard")
    st.dataframe(df_view, use_container_width=True, height=420)

    # Downloads
    c1, c2, c3 = st.columns(3)
    with c1:
        csv_bytes = df_view.to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Download CSV", csv_bytes, file_name="towel_orders.csv", mime="text/csv")
    with c2:
        xls_bytes = to_excel_bytes(df_view)
        st.download_button("⬇️ Download Excel", xls_bytes, file_name="towel_orders.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    with c3:
        labels_pdf = make_labels_pdf(df_view)
        st.download_button("🖨️ Download ALL Labels (6x4 PDF)", labels_pdf, file_name="labels_6x4_all.pdf",
                           mime="application/pdf")

    # Per-row label generation
    st.markdown("---")
    st.subheader("Generate Individual Labels")
    if len(df_view) > 0:
        options = df_view["Order ID"] + " — " + df_view["Buyer Name"] + " — " + df_view["SKU"]
        selected = st.selectbox("Pick an item", options.tolist())
        if selected:
            idx = options[options == selected].index[0]
            one_row_pdf = make_labels_pdf(df_view.iloc[[idx]])
            st.download_button("🖨️ Download Selected Label (6x4 PDF)", one_row_pdf,
                               file_name="label_6x4_selected.pdf", mime="application/pdf")
else:
    st.info("➡️ Use the uploader above to select your packing slip PDF(s).")