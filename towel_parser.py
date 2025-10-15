import io
import re
from dataclasses import dataclass
from typing import List, Dict
import streamlit as st
import pdfplumber
import pandas as pd
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch
from reportlab.lib import colors
import requests
from datetime import datetime

# ======================================================
# CONFIG
# ======================================================
st.set_page_config(page_title="Amazon Towel Orders — Landscape Labels", layout="wide")

# Airtable Configuration
AIRTABLE_API_KEY = st.secrets.get("AIRTABLE_API_KEY", "")
AIRTABLE_BASE_ID = st.secrets.get("AIRTABLE_BASE_ID", "")
AIRTABLE_TABLE_NAME = "Orders"

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
    "
