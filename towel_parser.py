import io
import re
import copy
from dataclasses import dataclass
from typing import List, Dict
import streamlit as st
import pdfplumber
import pandas as pd
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch
from reportlab.lib import colors

# ======================================================
# CONFIG
# ======================================================
st.set_page_config(page_title="Amazon Towel Orders — Landscape Labels", layout="wide")
st.caption("🧠 Running towel_parser v1.9 (multi-item label sync + fixed table index)")

# English → Spanish thread-color dictionary
THREAD_COLOR_ES = {
    "White": "Blanco", "Black": "Negro", "Gold": "Dorado", "Silver": "Plateado",
    "Red": "Rojo", "Blue": "Azul", "Navy": "Azul Marino", "Light Blue": "Azul Claro",
    "Green": "Verde", "Pink": "Rosa", "Hot Pink": "Rosa Fucsia", "Lilac": "Lila",
    "Purple": "Morado", "Yellow": "Amarillo", "Beige": "Beige", "Brown": "Marrón",
    "Gray": "Gris", "Grey": "Gris", "Orange": "Naranja", "Teal": "Verde Azulado",
    "Ivory": "Marfil",
}

PRODUCT_TYPES = {
    "Set-6Pcs": {
        "label": "6-Piece Towel Set",
        "pieces": [
            ("First Washcloth", "Small"), ("Second Washcloth", "Small"),
            ("First Hand Towel", "Medium"), ("Second Hand Towel", "Medium"),
            ("First Bath Towel", "Large"), ("Second Bath Towel", "Large")
        ],
        "plural_word": "Sets",
    },
    "Set-3Pcs": {
        "label": "3-Piece Towel Set",
        "pieces": [
            ("Washcloth", "Small"), ("Hand Towel", "Medium"), ("Bath Towel", "Large")
        ],
        "plural_word": "Sets",
    },
    "HT-2PCS": {
        "label": "Hand Towels (2)",
        "pieces": [("First Hand Towel", "Medium"), ("Second Hand Towel", "Medium")],
        "plural_word": "Sets",
    },
    "BT-2Pcs": {
        "label": "Bath Towels (2)",
        "pieces": [("First Bath Towel", "Large"), ("Second Bath Towel", "Large")],
        "plural_word": "Sets",
    },
    "BS-1Pcs": {
        "label": "Bath Sheet (Oversized)",
        "pieces": [("Oversized Bath Sheet", "XL")],
        "plural_word": "Qty",
    },
}

SKU_PREFIXES = list(PRODUCT_TYPES.keys())
SKU_REGEX = re.compile(r"\b(" + "|".join([re.escape(p) for p in SKU_PREFIXES]) + r")-([A-Za-z ]+)\b")
ORDER_ID_REGEX = re.compile(r"\bOrder ID\b[: ]+([A0-9\-]+)", re.IGNORECASE)
ORDER_DATE_REGEX = re.compile(r"\bOrder Date\b[: ]+([A-Za-z0-9, ]+)")
SHIPPING_SERVICE_REGEX = re.compile(r"\bShipping Service\b[: ]+([A-Za-z ]+)")
BUYER_NAME_REGEX = re.compile(r"\bBuyer Name\b[: ]+(.+)")
SHIP_TO_REGEX = re.compile(r"Ship To:\s*(.+)")
FONT_REGEX = re.compile(r"(?:Choose Your Font|Font)\s*[:\-]\s*(.+)")
FONT_COLOR_REGEX = re.compile(r"(?:Font Color|Thread Color)\s*[:\-]\s*([A-Za-z ]+)")

def piece_line_regex(piece_name: str) -> re.Pattern:
    return re.compile(r"\b" + re.escape(piece_name) + r"\s*:\s*(.*)")

# ======================================================
# DATA STRUCTURE
# ======================================================
@dataclass
class LineItem:
    order_id: str = ""
    order_date: str = ""
    shipping_service: str = ""
    buyer_name: str = ""
    sku_full: str = ""
    product_type: str = ""
    color: str = ""
    font_name: str = ""
    thread_color: str = ""
    quantity: int = 1
    customization: Dict[str, str] = None

    def to_row(self):
        custom_str = ""
        if self.customization:
            chunks = []
            for p, _sz in PRODUCT_TYPES.get(self.product_type, {"pieces": []})["pieces"]:
                val = (self.customization or {}).get(p, "").strip()
                if val:
                    chunks.append(f"{p}: {val}")
            custom_str = " | ".join(chunks)
        return {
            "Order ID": self.order_id,
            "Buyer Name": self.buyer_name,
            "Product Type": PRODUCT_TYPES.get(self.product_type, {}).get("label", self.product_type),
            "Color": self.color,
            "Quantity": self.quantity,
            "Font": self.font_name,
            "Thread Color": self.thread_color,
            "Shipping Service": self.shipping_service,
            "SKU": self.sku_full,
            "Customization Notes": custom_str
        }

# ======================================================
# HELPERS
# ======================================================
def _find_quantity_before_index(block_text: str, sku_start_index: int) -> int:
    pre_text = block_text[:sku_start_index]
    pre_lines = pre_text.splitlines()[-15:]
    for line in reversed(pre_lines):
        m = re.search(r'\bQuantity\b[^\d]*(\d+)\b', line, re.IGNORECASE)
        if m:
            return int(m.group(1))
    for line in reversed(pre_lines):
        m = re.match(r'^\s*(\d+)\s+[A-Za-z]', line)
        if m:
            return int(m.group(1))
    return 1

# ======================================================
# MULTI-ITEM SAFE EXTRACTOR
# ======================================================
def extract_items_from_block(block_text: str, order_meta: Dict[str, str]) -> List[LineItem]:
    items = []
    if not block_text.strip():
        return items

    sku_spans = [(m.start(), m.end()) for m in SKU_REGEX.finditer(block_text)]
    if not sku_spans:
        return items

    for i, (s, e) in enumerate(sku_spans):
        end = sku_spans[i + 1][0] if i + 1 < len(sku_spans) else len(block_text)
        chunk = block_text[s:end]

        # Every sub_chunk can have its own "Customizations:" block
        for sub_chunk in re.split(r"(?=Customizations?:)", chunk):
            sku_match = SKU_REGEX.search(sub_chunk)
            if not sku_match:
                continue

            prefix = sku_match.group(1)
            color = sku_match.group(2).strip()
            color = re.split(r"\b(Item|Tax|Promotion|Total|Subtotal|Shipping)\b", color, maxsplit=1)[0].strip()
            color = " ".join(w.capitalize() for w in color.split())
            sku_full = f"{prefix}-{color}"

            item = LineItem(
                order_id=order_meta.get("order_id", ""),
                order_date=order_meta.get("order_date", ""),
                shipping_service=order_meta.get("shipping_service", ""),
                buyer_name=order_meta.get("buyer_name", ""),
                sku_full=sku_full,
                product_type=prefix,
                color=color,
                quantity=_find_quantity_before_index(block_text, s),
                customization={}
            )

            fr, fcr = FONT_REGEX.search(sub_chunk), FONT_COLOR_REGEX.search(sub_chunk)
            if fr:
                item.font_name = fr.group(1).strip()
            if fcr:
                item.thread_color = fcr.group(1).strip()

            for piece_name, _size in PRODUCT_TYPES.get(prefix, {}).get("pieces", []):
                pr = piece_line_regex(piece_name).search(sub_chunk)
                if pr:
                    item.customization[piece_name] = pr.group(1).strip()

            if item.customization or item.font_name:
                items.append(copy.deepcopy(item))

    return items

# ======================================================
# PARSER
# ======================================================
def parse_pdf_files(uploaded_files) -> List[LineItem]:
    all_items = []
    for uf in uploaded_files:
        with pdfplumber.open(uf) as pdf:
            current_order = {}
            carry_text = ""
            for page in pdf.pages:
                text = page.extract_text() or ""
                header = "\n".join(text.splitlines()[:10])
                header_order = ORDER_ID_REGEX.search(header)
                ship_to = SHIP_TO_REGEX.search(header)

                if header_order:
                    if carry_text.strip():
                        all_items.extend(extract_items_from_block(carry_text, current_order))
                        carry_text = ""

                    current_order = {
                        "order_id": header_order.group(1).strip(),
                        "order_date": (ORDER_DATE_REGEX.search(text) or re.match(r"$^","")).group(1).strip()
                        if ORDER_DATE_REGEX.search(text) else "",
                        "shipping_service": (SHIPPING_SERVICE_REGEX.search(text) or re.match(r"$^","")).group(1).strip()
                        if SHIPPING_SERVICE_REGEX.search(text) else "",
                        "buyer_name": ship_to.group(1).strip()
                        if ship_to else (BUYER_NAME_REGEX.search(text).group(1).strip()
                        if BUYER_NAME_REGEX.search(text) else "")
                    }
                    carry_text = text
                else:
                    carry_text += "\n" + text

            if carry_text.strip():
                all_items.extend(extract_items_from_block(carry_text, current_order))
    return all_items

# ======================================================
# GROUPING + LABEL BUILDER
# ======================================================
def group_items(items: List[LineItem]) -> List[LineItem]:
    grouped = {}
    for item in items:
        custom_str = "|".join(f"{k}:{v}" for k, v in sorted((item.customization or {}).items()))
        key = (item.order_id, item.sku_full, item.font_name, item.thread_color, custom_str)
        if key not in grouped:
            grouped[key] = copy.deepcopy(item)
        else:
            grouped[key].quantity += item.quantity
    return list(grouped.values())

def build_labels_pdf(items: List[LineItem]) -> bytes:
    buf = io.BytesIO()
    PAGE_W, PAGE_H = 6 * inch, 4 * inch
    c = canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))

    for item in items:
        x0, y = 0.4 * inch, PAGE_H - 0.5 * inch
        line_gap, big_gap = 14, 18

        # Header
        c.setFont("Helvetica-Bold", 12)
        ship = item.shipping_service or "Standard"
        c.drawString(x0, y, f"Order ID: {item.order_id}")
        c.drawRightString(PAGE_W - 0.4 * inch, y, f"Shipping: {ship}")
        y -= big_gap

        # Buyer
        c.setFont("Helvetica-Bold", 12)
        c.drawString(x0, y, f"Buyer: {item.buyer_name[:50]}")
        y -= line_gap

        # Product & Quantity
        c.setFont("Helvetica", 12)
        c.drawString(x0, y, f"Product: {PRODUCT_TYPES.get(item.product_type, {}).get('label', item.product_type)}")
        y -= big_gap
        c.setFont("Helvetica-BoldOblique", 16)
        plural = PRODUCT_TYPES.get(item.product_type, {}).get("plural_word", "Qty")
        c.drawString(x0, y, f"Quantity: {item.quantity} {plural}")
        y -= big_gap

        # Colors
        c.setFont("Helvetica-Bold", 16)
        c.drawString(x0, y, f"Towel Color: {item.color.upper()}")
        y -= big_gap
        es = THREAD_COLOR_ES.get(item.thread_color.title(), "")
        c.drawString(x0, y, f"Thread Color: {item.thread_color.upper()}  |  {es.upper() if es else ''}")
        y -= big_gap

        # Divider
        c.setStrokeColor(colors.lightgrey)
        c.setLineWidth(1)
        c.line(x0, y, PAGE_W - 0.4 * inch, y)
        y -= big_gap

        # Customization
        c.setFont("Helvetica-Bold", 12)
        c.drawString(x0, y, "CUSTOMIZATION:")
        y -= big_gap + 4

        c.setFont("Times-Italic", 14)
        for piece_name, size in PRODUCT_TYPES.get(item.product_type, {}).get("pieces", []):
            val = (item.customization or {}).get(piece_name, "")
            if val:
                c.drawString(x0, y, f"{piece_name} ({size}): {val}")
                y -= 16

        c.showPage()

    c.save()
    return buf.getvalue()

# ======================================================
# STREAMLIT UI
# ======================================================
st.title("🧵 Amazon Towel Orders — 4×6 Landscape Labels")
files = st.file_uploader("Upload PDF files", type=["pdf"], accept_multiple_files=True)

# Sidebar re-parse button
if st.sidebar.button("🔄 Re-parse Files"):
    if files:
        st.session_state.parsed_items = parse_pdf_files(files)
        st.sidebar.success("Re-parsed successfully!")
    else:
        st.sidebar.warning("Please upload PDFs first.")

tabs = st.tabs(["📄 Table View", "🏷️ Labels", "📊 End of Day Summary"])

# ---- TAB 1 ----
with tabs[0]:
    if files:
        if "parsed_items" not in st.session_state:
            st.session_state.parsed_items = parse_pdf_files(files)
        items = st.session_state.parsed_items
        if not items:
            st.warning("No towel line items detected.")
        else:
            df = pd.DataFrame([i.to_row() for i in items])
            df.index = df.index + 1
            df.index.name = "No."
            st.table(df)  # preserves numbering
            st.download_button("⬇️ Download CSV", df.to_csv(index=False).encode("utf-8"), "towel_orders.csv")
    else:
        st.info("Upload PDFs to see parsed results.")

# ---- TAB 2 ----
with tabs[1]:
    if files:
        if "parsed_items" not in st.session_state:
            st.session_state.parsed_items = parse_pdf_files(files)
        items = group_items(st.session_state.parsed_items)
        if items:
            df = pd.DataFrame([i.to_row() for i in items])
            all_ids = [
                f"{idx+1}. {r['Order ID']} | {r['SKU']} | {r['Font']} | {r['Thread Color']}"
                for idx, r in df.iterrows()
            ]
            key_map = {
                f"{idx+1}. {i.order_id} | {i.sku_full} | {i.font_name} | {i.thread_color}": i
                for idx, i in enumerate(items)
            }
            selected = st.multiselect("Select items to include:", all_ids, default=all_ids)
            selected_items = [key_map[k] for k in selected if k in key_map]
            if st.button("🖨️ Build 4×6 Labels PDF"):
                pdf_bytes = build_labels_pdf(selected_items)
                st.download_button("⬇️ Download 4×6 Labels (PDF)", pdf_bytes, "towel_labels_grouped.pdf")
        else:
            st.warning("Nothing parsed yet.")
    else:
        st.info("Upload PDFs to generate labels.")

# ---- TAB 3 ----
with tabs[2]:
    if files:
        if "parsed_items" not in st.session_state:
            st.session_state.parsed_items = parse_pdf_files(files)
        items = group_items(st.session_state.parsed_items)
        if items:
            df = pd.DataFrame([i.to_row() for i in items])
            st.subheader("📅 End of the Day Summary")
            st.markdown(f"**Total Orders:** {df['Order ID'].nunique()}  \n**Total Quantity:** {df['Quantity'].sum()}")
            st.markdown("### 🧺 By Product Type")
            st.dataframe(df.groupby("Product Type")["Quantity"].sum().reset_index().rename(columns={"Quantity": "Total"}))
            st.markdown("### 🎨 By Towel Color")
            st.dataframe(df.groupby("Color")["Quantity"].sum().reset_index().rename(columns={"Quantity": "Total"}))
            st.markdown("### 🧵 By Thread Color (English | Spanish)")
            df["Thread Color (ES)"] = df["Thread Color"].apply(lambda c: THREAD_COLOR_ES.get(str(c).title(), ""))
            st.dataframe(df.groupby(["Thread Color", "Thread Color (ES)"])["Quantity"].sum().reset_index().rename(columns={"Quantity": "Total"}))
        else:
            st.info("Upload PDFs to generate summary.")
    else:
        st.info("Upload PDFs to generate summary.")
