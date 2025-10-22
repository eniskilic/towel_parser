
import io
import re
from dataclasses import dataclass, asdict
from typing import List, Dict, Tuple, Optional
import streamlit as st
import pdfplumber
import pandas as pd
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import inch, landscape, portrait
from reportlab.lib import colors

# =========================================
# CONFIG / CONSTANTS
# =========================================
st.set_page_config(page_title="Amazon Towel Orders — Parser, Labels & Summary", layout="wide")

THREAD_COLOR_ES = {
    "White": "Blanco", "Black": "Negro", "Gold": "Dorado", "Silver": "Plateado",
    "Red": "Rojo", "Blue": "Azul", "Navy": "Azul Marino", "Light Blue": "Azul Claro",
    "Green": "Verde", "Pink": "Rosa", "Hot Pink": "Rosa Fucsia", "Lilac": "Lila",
    "Purple": "Morado", "Yellow": "Amarillo", "Beige": "Beige", "Brown": "Marrón",
    "Gray": "Gris", "Grey": "Gris", "Orange": "Naranja", "Champagne": "Champán",
    "Dark Grey": "Gris Oscuro", "Aqua": "Aguamarina", "Mid Blue": "Azul Medio",
}

ORDER_ID_RE = re.compile(r"Order ID:\s*(\d{3}-\d{7}-\d{7})")
BUYER_NAME_RE = re.compile(r"Buyer Name:\s*([A-Za-z0-9\s\.'\-&]+)")
SKU_RE = re.compile(r"SKU:\s*([A-Za-z0-9\-]+)")
CHOOSE_FONT_RE = re.compile(r"Choose Your Font:\s*([A-Za-z0-9\s\-\._&]+)")
FONT_COLOR_RE = re.compile(r"Font Color:\s*([A-Za-z\s]+)\s*\(#?[A-Fa-f0-9]{0,8}\)?")
CUSTOM_LINE_RE = re.compile(r"^(Washcloth|Hand Towel|First Hand Towel|Second Hand Towel|Bath Towel|First Bath Towel|Second Bath Towel|First Washcloth|Second Washcloth|Oversized Bath Sheet)\s*:\s*(.+)$")
GIFT_MSG_RE = re.compile(r"(?i)Gift\s*(Message|Card|Bag)\s*:\s*(.+)")
LEADING_QTY_RE = re.compile(r"^(\d+)\s+(Personalized|ViaDante|Monogrammed|Set of|Hand Towel|Bath Towel|Bath Sheet)")

# =========================================
# DATA MODELS
# =========================================
@dataclass
class ParsedItem:
    order_id: str
    buyer_name: str
    sku: str
    product_type: str
    color: str
    thread_color_en: str
    thread_color_es: str
    customization: str
    font: str
    quantity: int
    gift_message: str
    source_file: str

# =========================================
# HELPER FUNCTIONS
# =========================================
def derive_product_type_and_color(sku: str) -> Tuple[str, str]:
    parts = sku.split("-")
    if len(parts) >= 2:
        prefix = "-".join(parts[:2]) if parts[0] in {"HT", "BT"} else parts[0]
    else:
        prefix = sku

    color = parts[-1] if len(parts) >= 2 else ""
    product_map = {
        "Set-6Pcs": "6-Piece Towel Set",
        "Set-3Pcs": "3-Piece Towel Set",
        "HT-2Pcs": "Hand Towel Set (2 pcs)",
        "BT-2Pcs": "Bath Towel Set (2 pcs)",
        "BS-1Pcs": "Bath Sheet (1 pc)",
        "HT": "Hand Towel Set (2 pcs)",
        "BT": "Bath Towel Set (2 pcs)",
        "BS": "Bath Sheet (1 pc)",
    }
    product_type = product_map.get(prefix, prefix)
    return product_type, color.replace("_", " ").replace("MidBlue", "Mid Blue")

def es_color(name: str) -> str:
    return THREAD_COLOR_ES.get(name.strip().title(), name)

def clean_text(txt: str) -> str:
    return re.sub(r"\s+", " ", txt).strip()

def wrap_text(text: str, max_chars: int = 56) -> List[str]:
    words = text.split()
    lines = []
    current = []
    cur_len = 0
    for w in words:
        add = (1 if current else 0) + len(w)
        if cur_len + add <= max_chars:
            current.append(w)
            cur_len += add
        else:
            lines.append(" ".join(current))
            current = [w]
            cur_len = len(w)
    if current:
        lines.append(" ".join(current))
    return lines

# =========================================
# PDF PARSING
# =========================================
def parse_pdfs(files: List) -> pd.DataFrame:
    parsed_rows: List[ParsedItem] = []

    for f in files:
        source_name = getattr(f, "name", "uploaded.pdf")
        with pdfplumber.open(f) as pdf:
            current_order_id: Optional[str] = None
            current_buyer: str = ""
            leading_qty_for_next_item: Optional[int] = None

            def flush_item_if_ready(item_ctx: Dict):
                if not item_ctx.get("sku"):
                    return
                sku = item_ctx.get("sku", "")
                product_type, color = derive_product_type_and_color(sku)
                font = item_ctx.get("font", "")
                thread_en = item_ctx.get("thread_color", "")
                thread_es = es_color(thread_en) if thread_en else ""
                customization_lines = item_ctx.get("custom_lines", [])
                customization = " / ".join([clean_text(x) for x in customization_lines]) if customization_lines else ""
                gift_message = item_ctx.get("gift_message", "")
                qty = item_ctx.get("quantity", 1)

                parsed_rows.append(
                    ParsedItem(
                        order_id=item_ctx.get("order_id", ""),
                        buyer_name=item_ctx.get("buyer_name", ""),
                        sku=sku,
                        product_type=product_type,
                        color=color,
                        thread_color_en=thread_en,
                        thread_color_es=thread_es,
                        customization=customization,
                        font=font,
                        quantity=qty,
                        gift_message=gift_message,
                        source_file=source_name,
                    )
                )

            item_ctx: Dict = {"custom_lines": []}

            for page in pdf.pages:
                try:
                    text = page.extract_text() or ""
                except Exception:
                    text = ""
                lines = [clean_text(x) for x in text.split("\n") if clean_text(x)]

                # Detect if this page starts a new order
                found_order_on_page = False
                for line in lines:
                    m_id = ORDER_ID_RE.search(line)
                    if m_id:
                        # new order: flush pending item
                        flush_item_if_ready(item_ctx)
                        item_ctx = {"custom_lines": []}
                        current_order_id = m_id.group(1)
                        item_ctx["order_id"] = current_order_id
                        item_ctx["buyer_name"] = item_ctx.get("buyer_name", current_buyer)
                        found_order_on_page = True
                        leading_qty_for_next_item = None
                        break

                # Parse fields on the page
                for line in lines:
                    # Buyer name
                    m_buyer = BUYER_NAME_RE.search(line)
                    if m_buyer:
                        current_buyer = m_buyer.group(1).strip()
                        item_ctx["buyer_name"] = current_buyer

                    # Gift message
                    m_gift = GIFT_MSG_RE.search(line)
                    if m_gift:
                        item_ctx["gift_message"] = m_gift.group(2).strip()

                    # Quantity indicator line (before product)
                    m_qty = LEADING_QTY_RE.match(line)
                    if m_qty:
                        try:
                            leading_qty_for_next_item = int(m_qty.group(1))
                        except Exception:
                            leading_qty_for_next_item = None

                    # SKU
                    m_sku = SKU_RE.search(line)
                    if m_sku:
                        # Flush previous SKU block
                        if item_ctx.get("sku"):
                            flush_item_if_ready(item_ctx)
                            item_ctx = {"custom_lines": [], "order_id": current_order_id, "buyer_name": current_buyer}

                        item_ctx["sku"] = m_sku.group(1).strip()
                        if leading_qty_for_next_item:
                            item_ctx["quantity"] = leading_qty_for_next_item
                            leading_qty_for_next_item = None
                        else:
                            item_ctx["quantity"] = 1

                    # Font
                    m_font = CHOOSE_FONT_RE.search(line)
                    if m_font:
                        item_ctx["font"] = m_font.group(1).strip()

                    # Thread color
                    m_color = FONT_COLOR_RE.search(line)
                    if m_color:
                        item_ctx["thread_color"] = m_color.group(1).strip()

                    # Customization lines
                    m_custom = CUSTOM_LINE_RE.match(line)
                    if m_custom:
                        val = m_custom.group(2).strip()
                        if val and not val.lower().startswith(("item subtotal","promotion","tax","grand total")):
                            item_ctx.setdefault("custom_lines", []).append(f"{m_custom.group(1)}: {val}")

            # File end: flush last item
            flush_item_if_ready(item_ctx)

    df = pd.DataFrame([asdict(r) for r in parsed_rows])
    if not df.empty:
        df["buyer_name"] = df["buyer_name"].fillna("").str.title()
        df["color"] = df["color"].fillna("").str.replace("_", " ").str.title()
        df["thread_color_en"] = df["thread_color_en"].fillna("").str.title()
        df["thread_color_es"] = df["thread_color_es"].fillna("")
        df["product_type"] = df["product_type"].fillna("")
        df["font"] = df["font"].fillna("")
        df["gift_message"] = df["gift_message"].fillna("")
        df["customization"] = df["customization"].fillna("")
        df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce").fillna(1).astype(int)
        df["order_id"] = df["order_id"].fillna("")
        df["sku"] = df["sku"].fillna("")
        df["source_file"] = df["source_file"].fillna("")
    return df

# =========================================
# PRODUCTION SUMMARY (SETS-ONLY VIEW)
# =========================================
def production_summary(df: pd.DataFrame) -> pd.DataFrame:
    # Returns a table grouped by Color with columns (counts shown as sets/pcs):
    # - 3/6-Pcs Sets  (Set-3Pcs + 2×Set-6Pcs)
    # - Hand Towel Sets (HT-2Pcs)
    # - Bath Towel Sets (BT-2Pcs)
    # - Bath Sheets (1 Pc) (BS-1Pcs)
    if df.empty:
        return pd.DataFrame(columns=["Color","3/6-Pcs Sets","Hand Towel Sets","Bath Towel Sets","Bath Sheets (1 Pc)"])

    def categorize_and_sets(sku: str, qty: int) -> Tuple[str, int]:
        sku_up = sku or ""
        if "Set-6Pcs" in sku_up:
            return "3/6-Pcs Sets", qty * 2
        if "Set-3Pcs" in sku_up:
            return "3/6-Pcs Sets", qty
        if "HT-2Pcs" in sku_up or re.match(r"^HT-?2Pcs", sku_up, re.I):
            return "Hand Towel Sets", qty
        if "BT-2Pcs" in sku_up or re.match(r"^BT-?2Pcs", sku_up, re.I):
            return "Bath Towel Sets", qty
        if "BS-1Pcs" in sku_up or sku_up.startswith("BS"):
            return "Bath Sheets (1 Pc)", qty
        return "", 0

    rows = []
    for _, r in df.iterrows():
        cat, sets = categorize_and_sets(r.get("sku",""), int(r.get("quantity",1)))
        if not cat:
            continue
        rows.append((r.get("color",""), cat, sets))

    if not rows:
        return pd.DataFrame(columns=["Color","3/6-Pcs Sets","Hand Towel Sets","Bath Towel Sets","Bath Sheets (1 Pc)"])

    tmp = pd.DataFrame(rows, columns=["Color","Category","Sets"])
    pivot = tmp.pivot_table(index="Color", columns="Category", values="Sets", aggfunc="sum", fill_value=0).reset_index()
    for col in ["3/6-Pcs Sets","Hand Towel Sets","Bath Towel Sets","Bath Sheets (1 Pc)"]:
        if col not in pivot.columns:
            pivot[col] = 0
    pivot = pivot[["Color","3/6-Pcs Sets","Hand Towel Sets","Bath Towel Sets","Bath Sheets (1 Pc)"]]
    pivot = pivot.sort_values(by=["Color"]).reset_index(drop=True)
    return pivot

# =========================================
# PDF GENERATION — MANUFACTURING LABELS (4x6 Landscape)
# =========================================
def generate_manufacturing_labels(df: pd.DataFrame) -> bytes:
    if df.empty:
        return b""
    buf = io.BytesIO()
    page_size = landscape((4*inch, 6*inch))
    c = canvas.Canvas(buf, pagesize=page_size)

    for _, r in df.iterrows():
        width, height = 6*inch, 4*inch
        left = 0.35*inch

        buyer = (r.get("buyer_name","") or "")[:60]
        order_id = r.get("order_id","") or ""
        product = f"{r.get('product_type','')} – {r.get('color','')}"
        thread_es = r.get("thread_color_es","") or ""
        thread_en = r.get("thread_color_en","") or ""
        font_name = r.get("font","") or ""
        qty = int(r.get("quantity",1))
        customization = r.get("customization","") or ""

        # Header
        c.setFont("Helvetica-Bold", 20)
        c.drawString(left, height - 0.55*inch, buyer)

        c.setFont("Helvetica", 11)
        c.drawString(left, height - 0.85*inch, f"Order ID: {order_id}")
        c.drawString(left, height - 1.10*inch, f"Product: {product}")
        if thread_es or thread_en:
            if thread_es and thread_en and thread_es.lower() != thread_en.lower():
                thread_line = f"Thread Color: {thread_es} ({thread_en})"
            else:
                thread_line = f"Thread Color: {thread_es or thread_en}"
            c.drawString(left, height - 1.35*inch, thread_line)
        if font_name:
            c.drawString(left, height - 1.60*inch, f"Font: {font_name}")
        c.drawString(left, height - 1.85*inch, f"Quantity: {qty}")

        # Customization (bulleted, wrapped)
        y = height - 2.20*inch
        c.setFont("Helvetica", 11)
        if customization:
            parts = [p.strip() for p in customization.split("/") if p.strip()]
            for p in parts:
                lines = wrap_text(p, max_chars=64)
                for ln in lines:
                    c.drawString(left, y, f"• {ln}")
                    y -= 0.22*inch
                    if y < 0.40*inch:
                        break
                if y < 0.40*inch:
                    break
        else:
            c.drawString(left, y, "• (No customization text)")

        # Footer
        c.setFont("Helvetica", 8)
        c.setFillColor(colors.grey)
        c.drawRightString(width - 0.25*inch, 0.25*inch, r.get("source_file",""))
        c.setFillColor(colors.black)

        c.showPage()

    c.save()
    buf.seek(0)
    return buf.getvalue()

# =========================================
# PDF GENERATION — GIFT LABELS (4x6 Portrait)
# =========================================
def generate_gift_labels(df: pd.DataFrame) -> bytes:
    gifts = df[df["gift_message"].str.strip() != ""] if not df.empty else pd.DataFrame()
    if gifts.empty:
        return b""
    buf = io.BytesIO()
    page_size = portrait((4*inch, 6*inch))
    c = canvas.Canvas(buf, pagesize=page_size)

    for _, r in gifts.iterrows():
        width, height = 4*inch, 6*inch
        msg = r.get("gift_message","") or ""
        buyer = r.get("buyer_name","") or ""
        order_id = r.get("order_id","") or ""

        c.setFont("Helvetica-Oblique", 16)
        lines = wrap_text(msg, max_chars=28)
        start_y = height/2 + (len(lines)*0.18*inch)
        y = start_y
        for ln in lines:
            text_width = c.stringWidth(ln, "Helvetica-Oblique", 16)
            c.drawString((width - text_width)/2, y, ln)
            y -= 0.3*inch

        c.setFont("Helvetica", 9)
        footer = f"{buyer} — {order_id}"
        text_width = c.stringWidth(footer, "Helvetica", 9)
        c.drawString((width - text_width)/2, 0.4*inch, footer)

        c.showPage()

    c.save()
    buf.seek(0)
    return buf.getvalue()

# =========================================
# STREAMLIT UI
# =========================================
def main():
    st.title("🧺 Amazon Towel Orders — Parser, Labels & Production Summary")

    tabs = st.tabs(["📤 Upload PDFs", "📋 Orders", "📊 Production Summary", "🎁 Gift Labels", "🖨️ Manufacturing Labels"])

    with tabs[0]:
        st.write("Upload one or more Amazon packing slip PDFs. The app will parse orders, items, and customizations.")
        uploaded = st.file_uploader("Upload Amazon Packing Slip PDFs", type=["pdf"], accept_multiple_files=True)
        if uploaded:
            if st.button("🔍 Parse PDFs"):
                df = parse_pdfs(uploaded)
                st.session_state["orders_df"] = df
                st.success(f"Parsed {len(df)} item rows from {len(uploaded)} PDF(s).")
                st.dataframe(df, use_container_width=True)
            else:
                st.info("Click **Parse PDFs** to begin.")

    orders_df = st.session_state.get("orders_df", pd.DataFrame())

    with tabs[1]:
        st.subheader("Parsed Orders Table")
        if orders_df.empty:
            st.info("No data yet. Go to **Upload PDFs** tab and parse files.")
        else:
            c1, c2, c3 = st.columns(3)
            with c1:
                color_filter = st.multiselect("Filter by Color", sorted(orders_df["color"].dropna().unique().tolist()))
            with c2:
                sku_filter = st.multiselect("Filter by SKU", sorted(orders_df["sku"].dropna().unique().tolist()))
            with c3:
                buyer_filter = st.text_input("Filter by Buyer (contains)")
            view_df = orders_df.copy()
            if color_filter:
                view_df = view_df[view_df["color"].isin(color_filter)]
            if sku_filter:
                view_df = view_df[view_df["sku"].isin(sku_filter)]
            if buyer_filter:
                view_df = view_df[view_df["buyer_name"].str.contains(buyer_filter, case=False, na=False)]
            st.dataframe(view_df, use_container_width=True)
            st.download_button("⬇️ Download CSV", data=view_df.to_csv(index=False).encode("utf-8"), file_name="parsed_orders.csv", mime="text/csv")

    with tabs[2]:
        st.subheader("Production Summary (Sets Only)")
        if orders_df.empty:
            st.info("No data yet. Parse PDFs first.")
        else:
            summary = production_summary(orders_df)
            st.dataframe(summary, use_container_width=True)
            st.download_button("⬇️ Download Summary CSV", data=summary.to_csv(index=False).encode("utf-8"), file_name="production_summary.csv", mime="text/csv")

    with tabs[3]:
        st.subheader("Generate Gift Labels (4×6 Portrait)")
        if orders_df.empty:
            st.info("No data yet. Parse PDFs first.")
        else:
            if st.button("🎁 Build Gift Labels PDF"):
                pdf_bytes = generate_gift_labels(orders_df)
                if not pdf_bytes:
                    st.warning("No gift messages found.")
                else:
                    st.download_button("⬇️ Download Gift Labels PDF", data=pdf_bytes, file_name="gift_labels_4x6.pdf", mime="application/pdf")

    with tabs[4]:
        st.subheader("Generate Manufacturing Labels (4×6 Landscape)")
        if orders_df.empty:
            st.info("No data yet. Parse PDFs first.")
        else:
            generate_for_all = st.checkbox("Generate for entire parsed dataset", value=True)
            subset_df = orders_df
            if not generate_for_all:
                chosen_orders = st.multiselect("Select Order IDs", sorted(orders_df["order_id"].unique().tolist()))
                subset_df = orders_df[orders_df["order_id"].isin(chosen_orders)] if chosen_orders else pd.DataFrame()

            if st.button("🖨️ Build Manufacturing Labels PDF"):
                if subset_df.empty:
                    st.warning("No rows selected.")
                else:
                    pdf_bytes = generate_manufacturing_labels(subset_df)
                    if not pdf_bytes:
                        st.error("Failed to build labels.")
                    else:
                        st.download_button("⬇️ Download Manufacturing Labels PDF", data=pdf_bytes, file_name="manufacturing_labels_4x6.pdf", mime="application/pdf")

if __name__ == "__main__":
    main()
