# Amazon Towels — Packing Slip Parser & 4×6 Label Builder

A Streamlit app that parses Amazon Seller Central packing slip PDFs for custom embroidered towel orders,
extracts the relevant fields, and generates print‑ready **4×6 inch landscape** manufacturing labels.

## Features
- Upload one or more PDF packing slips.
- Parse into a clean table (pandas DataFrame).
- Generate a single multi‑page PDF: **one label per SKU line item**.
- Clear 5‑section layout per label (Header, Product Details, Customization, Gift Options, Footer).

## Run Locally
```bash
python -m venv .venv && . .venv/bin/activate  # (on Windows: .venv\Scripts\activate)
pip install -r requirements.txt
streamlit run app.py
```
