import io
import os
import sqlite3
import tempfile
from datetime import date, datetime, timedelta
import numpy as np
import pandas as pd
import streamlit as st

# ----------------------------------------------------------------
# PAGE CONFIGURATION & INITIALIZATION
# ----------------------------------------------------------------
st.set_page_config(
    page_title="Rofa Warehouse System",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded"
)

DB_FILE = os.environ.get("ROFA_DB", "rofa_warehouse_v2.db")
LEGACY_DB = "rofa_warehouse.db"

DOC_TYPES = ["IN", "OUT", "RETURN_IN", "RETURN_OUT", "TRANSFER", "ADJUST"]
DOC_PREFIX = {"IN": "IN", "OUT": "OUT", "RETURN_IN": "RIN", "RETURN_OUT": "ROUT", "TRANSFER": "TRF", "ADJUST": "ADJ"}
INCREASE = {"IN", "RETURN_IN"}
DECREASE = {"OUT", "RETURN_OUT"}
EPS = 1e-9

class AppError(Exception):
    def __init__(self, key, **params):
        super().__init__(key)
        self.key = key
        self.params = params

# ----------------------------------------------------------------
# DATABASE ENGINE LOGIC
# ----------------------------------------------------------------
def connect():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def q(sql, params=()):
    conn = connect()
    try:
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()

SCHEMA = """
CREATE TABLE IF NOT EXISTS warehouses(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
    location TEXT, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS categories(
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS items(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, category TEXT,
    unit TEXT NOT NULL DEFAULT 'Pcs', min_stock REAL NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0, price REAL NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1, notes TEXT);
CREATE TABLE IF NOT EXISTS partners(
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'SUPPLIER', phone TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS documents(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_no TEXT UNIQUE NOT NULL, type TEXT NOT NULL, date TEXT NOT NULL,
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
    to_warehouse_id INTEGER REFERENCES warehouses(id),
    partner_id INTEGER REFERENCES partners(id),
    reference TEXT, notes TEXT, status TEXT NOT NULL DEFAULT 'Approved',
    created_at TEXT, cancelled_at TEXT);
CREATE TABLE IF NOT EXISTS document_lines(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    qty REAL NOT NULL, unit_cost REAL NOT NULL DEFAULT 0,
    batch_no TEXT, expiry TEXT);
CREATE TABLE IF NOT EXISTS ledger(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
    in_qty REAL NOT NULL DEFAULT 0, out_qty REAL NOT NULL DEFAULT 0,
    date TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_ledger_item ON ledger(item_id, warehouse_id);
CREATE INDEX IF NOT EXISTS ix_ledger_doc ON ledger(document_id);
CREATE INDEX IF NOT EXISTS ix_lines_doc ON document_lines(document_id);
CREATE INDEX IF NOT EXISTS ix_docs_date ON documents(date);
"""

def init_db():
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM warehouses").fetchone()[0] == 0:
            conn.execute("INSERT INTO warehouses(code, name, location) VALUES ('MAIN', 'المخزن الرئيسي', '')")
        conn.commit()
    finally:
        conn.close()

init_db()

# Backend Helper Functions
def items_df(active_only=True):
    sql = "SELECT id, code, name, category, unit, min_stock, cost, price, active, notes FROM items"
    if active_only:
        sql += " WHERE active=1"
    return q(sql + " ORDER BY code")

def warehouses_df(active_only=True):
    sql = "SELECT id, code, name, location, active FROM warehouses"
    if active_only:
        sql += " WHERE active=1"
    return q(sql + " ORDER BY id")

def partners_df():
    return q("SELECT id, name, type, phone, notes FROM partners ORDER BY name")

def categories_df():
    return q("SELECT id, name FROM categories ORDER BY name")

def stock_rows():
    return q("""SELECT l.item_id, l.warehouse_id, SUM(l.in_qty - l.out_qty) AS qty
                FROM ledger l JOIN documents d ON d.id = l.document_id
                WHERE d.status = 'Approved' GROUP BY l.item_id, l.warehouse_id""")

def stock_overview(warehouse_id=None, include_inactive=False):
    df = items_df(active_only=not include_inactive)
    rows = stock_rows()
    if warehouse_id is not None and not rows.empty:
        rows = rows[rows["warehouse_id"] == warehouse_id]
    if rows.empty or df.empty:
        df["qty"] = 0.0
    else:
        tot = rows.groupby("item_id")["qty"].sum()
        df["qty"] = df["id"].map(tot).fillna(0.0).astype(float).round(6)
    df["value"] = df["qty"] * df["cost"].fillna(0)
    df["status"] = np.where(df["qty"] <= EPS, "out", np.where(df["qty"] <= df["min_stock"], "low", "ok"))
    return df

def available(conn, item_id, warehouse_id):
    r = conn.execute(
        """SELECT COALESCE(SUM(l.in_qty - l.out_qty), 0) FROM ledger l
           JOIN documents d ON d.id = l.document_id
           WHERE d.status='Approved' AND l.item_id=? AND l.warehouse_id=?""",
        (item_id, warehouse_id)).fetchone()
    return round(r[0], 6)

def total_qty(conn, item_id):
    r = conn.execute(
        """SELECT COALESCE(SUM(l.in_qty - l.out_qty), 0) FROM ledger l
           JOIN documents d ON d.id = l.document_id
           WHERE d.status='Approved' AND l.item_id=?""", (item_id,)).fetchone()
    return round(r[0], 6)

def next_doc_no(conn, doc_type):
    year = date.today().year
    n = conn.execute("SELECT COUNT(*) FROM documents WHERE type=?", (doc_type,)).fetchone()[0] + 1
    while True:
        no = f"{DOC_PREFIX[doc_type]}-{year}-{n:04d}"
        if not conn.execute("SELECT 1 FROM documents WHERE doc_no=?", (no,)).fetchone():
            return no
        n += 1

def _num(v, default=0.0):
    try:
        if v is None or pd.isna(v): return default
        return float(v)
    except (TypeError, ValueError): return default

def _txt(v):
    if v is None: return None
    try:
        if pd.isna(v): return None
    except (TypeError, ValueError): pass
    v = str(v).strip()
    return v or None

def create_document(doc_type, doc_date, warehouse_id, lines, to_warehouse_id=None, partner_id=None, reference="", notes="", doc_no=None):
    if doc_type not in DOC_TYPES or doc_type == "ADJUST":
        raise AppError("نوع المستند غير صحيح")
    clean = []
    for ln in lines:
        iid = ln.get("item_id")
        if iid is None or (isinstance(iid, float) and pd.isna(iid)): continue
        qty = _num(ln.get("qty"))
        if qty <= 0: raise AppError("الكمية يجب أن تكون أكبر من الصفر")
        clean.append({"item_id": int(iid), "qty": qty, "unit_cost": _num(ln.get("unit_cost")),
                      "batch_no": _txt(ln.get("batch_no")), "expiry": _txt(ln.get("expiry"))})
    if not clean: raise AppError("لا توجد بنود بالمستند")
    if doc_type == "TRANSFER" and (not to_warehouse_id or int(to_warehouse_id) == int(warehouse_id)):
        raise AppError("اختر مخزن تحويل مختلف")

    conn = connect()
    try:
        if doc_type in DECREASE or doc_type == "TRANSFER":
            need = {}
            for ln in clean: need[ln["item_id"]] = need.get(ln["item_id"], 0) + ln["qty"]
            for iid, n in need.items():
                av = available(conn, iid, warehouse_id)
                if av + EPS < n: raise AppError(f"الرصيد غير كافٍ للصنف ID: {iid}. المتاح: {av}، المطلوب: {n}")

        no = (doc_no or "").strip() or next_doc_no(conn, doc_type)
        cur = conn.execute(
            """INSERT INTO documents(doc_no, type, date, warehouse_id, to_warehouse_id, partner_id, reference, notes, status, created_at)
               VALUES (?,?,?,?,?,?,?,?, 'Approved', ?)""",
            (no, doc_type, str(doc_date), int(warehouse_id),
             int(to_warehouse_id) if doc_type == "TRANSFER" else None,
             int(partner_id) if partner_id else None, _txt(reference), _txt(notes),
             datetime.now().isoformat(timespec="seconds")))
        doc_id = cur.lastrowid

        for ln in clean:
            iid, qty, uc = ln["item_id"], ln["qty"], ln["unit_cost"]
            row = conn.execute("SELECT cost FROM items WHERE id=?", (iid,)).fetchone()
            cost = row[0] if row else 0.0
            if doc_type == "IN" and uc > 0:
                base = max(total_qty(conn, iid), 0.0)
                new_cost = (base * cost + qty * uc) / (base + qty)
                conn.execute("UPDATE items SET cost=? WHERE id=?", (round(new_cost, 4), iid))
            elif uc <= 0: uc = cost
            
            conn.execute("INSERT INTO document_lines(document_id, item_id, qty, unit_cost, batch_no, expiry) VALUES (?,?,?,?,?,?)",
                         (doc_id, iid, qty, uc, ln["batch_no"], ln["expiry"]))
            
            legs = [(warehouse_id, qty, 0)] if doc_type in INCREASE else [(warehouse_id, 0, qty)] if doc_type in DECREASE else [(warehouse_id, 0, qty), (to_warehouse_id, qty, 0)]
            for wid, i_q, o_q in legs:
                conn.execute("INSERT INTO ledger(document_id, item_id, warehouse_id, in_qty, out_qty, date) VALUES (?,?,?,?,?,?)",
                             (doc_id, iid, int(wid), i_q, o_q, str(doc_date)))
        conn.commit()
        return doc_id, no
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def create_adjustment(warehouse_id, counts, notes="", doc_date=None):
    doc_date = str(doc_date or date.today())
    conn = connect()
    try:
        changes = []
        for iid, counted in counts:
            book = available(conn, int(iid), warehouse_id)
            diff = round(float(counted) - book, 6)
            if abs(diff) > EPS: changes.append((int(iid), diff))
        if not changes: return None, 0
        no = next_doc_no(conn, "ADJUST")
        cur = conn.execute(
            """INSERT INTO documents(doc_no, type, date, warehouse_id, reference, notes, status, created_at)
               VALUES (?, 'ADJUST', ?, ?, 'Stock count', ?, 'Approved', ?)""",
            (no, doc_date, int(warehouse_id), _txt(notes), datetime.now().isoformat(timespec="seconds")))
        doc_id = cur.lastrowid
        for iid, diff in changes:
            cost = conn.execute("SELECT cost FROM items WHERE id=?", (iid,)).fetchone()[0] or 0.0
            conn.execute("INSERT INTO document_lines(document_id, item_id, qty, unit_cost) VALUES (?,?,?,?)", (doc_id, iid, diff, cost))
            conn.execute("INSERT INTO ledger(document_id, item_id, warehouse_id, in_qty, out_qty, date) VALUES (?,?,?,?,?,?)",
                         (doc_id, iid, int(warehouse_id), max(diff, 0), max(-diff, 0), doc_date))
        conn.commit()
        return no, len(changes)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def ledger_df():
    return q("""SELECT l.id AS ledger_id, l.date, d.doc_no, d.type, d.status, d.reference,
                       l.warehouse_id, w.name AS warehouse, l.item_id, i.code, i.name AS item, i.unit,
                       l.in_qty, l.out_qty, p.name AS partner
                FROM ledger l
                JOIN documents d ON d.id = l.document_id
                JOIN items i ON i.id = l.item_id
                JOIN warehouses w ON w.id = l.warehouse_id
                LEFT JOIN partners p ON p.id = d.partner_id
                ORDER BY l.date DESC, l.id DESC""")

# ----------------------------------------------------------------
# MULTI-LANGUAGE TRANSLATIONS
# ----------------------------------------------------------------
LANG = {
    "ar": {
        "title": "نظام Rofa لإدارة المخازن",
        "subtitle": "Logistics Manager",
        "nav_dash": "Dashboard",
        "nav_items": "Inventory",
        "nav_notifications": "Notifications",
        "nav_shipments": "Shipments",
        "nav_receiving": "Receiving",
        "nav_zones": "Zones & Locations",
        "nav_suppliers": "Suppliers",
        "nav_reports": "Reports",
        "nav_dispatch": "DISPATCH",
        "nav_driver_radio": "Driver Radio",
        "nav_manifests": "Manifests",
        "nav_pinned": "PINNED ROUTERS",
        "nav_add_route": "Add new route",
        "nav_help": "Help Center",
        "total_items": "Total Items",
        "total_qty": "Total Stock Qty",
        "low_stock": "Min Stock Alert ⚠️",
        "current_stock_title": "Current Inventory Status",
        "no_items": "No items recorded yet.",
        "lang_btn": "English 🇺🇸",
        "pending_inbound": "Pending Inbound",
        "processing": "Processing",
        "awaiting_pickup": "Awaiting Pickup",
        "completed": "Completed",
        "delayed": "Delayed",
        "ai_notification": "24 shipments are currently being routed by AI Agent automatically",
        "dismiss": "Dismiss",
        "view_log": "View Log →",
        "shipment_id": "Shipment ID",
        "description": "Description",
        "location": "Location",
        "type": "Type",
        "priority": "Priority",
        "carrier": "Carrier",
        "contact": "Contact",
        "urgent": "Urgent",
        "high": "High",
        "medium": "Medium",
        "low": "Low",
        "outbound": "Outbound",
        "inbound": "Inbound",
        "transfer": "Transfer",
        "return": "Return",
        "scan_barcode": "Scan Barcode",
        "new_shipment": "+ New Shipment",
        "last_7_days": "Last 7 Days",
        "daily": "Daily",
        "priority_filter": "Priority",
        "page": "Page",
        "of": "of",
        "rows_per_page": "Rows per page"
    },
    "en": {
        "title": "Central Warehouse",
        "subtitle": "Logistics Manager",
        "nav_dash": "Dashboard",
        "nav_items": "Inventory",
        "nav_notifications": "Notifications",
        "nav_shipments": "Shipments",
        "nav_receiving": "Receiving",
        "nav_zones": "Zones & Locations",
        "nav_suppliers": "Suppliers",
        "nav_reports": "Reports",
        "nav_dispatch": "DISPATCH",
        "nav_driver_radio": "Driver Radio",
        "nav_manifests": "Manifests",
        "nav_pinned": "PINNED ROUTERS",
        "nav_add_route": "Add new route",
        "nav_help": "Help Center",
        "total_items": "Total Items",
        "total_qty": "Total Stock Qty",
        "low_stock": "Min Stock Alert ⚠️",
        "current_stock_title": "Current Inventory Status",
        "no_items": "No items recorded yet.",
        "lang_btn": "العربية 🇪🇬",
        "pending_inbound": "Pending Inbound",
        "processing": "Processing",
        "awaiting_pickup": "Awaiting Pickup",
        "completed": "Completed",
        "delayed": "Delayed",
        "ai_notification": "24 shipments are currently being routed by AI Agent automatically",
        "dismiss": "Dismiss",
        "view_log": "View Log →",
        "shipment_id": "Shipment ID",
        "description": "Description",
        "location": "Location",
        "type": "Type",
        "priority": "Priority",
        "carrier": "Carrier",
        "contact": "Contact",
        "urgent": "Urgent",
        "high": "High",
        "medium": "Medium",
        "low": "Low",
        "outbound": "Outbound",
        "inbound": "Inbound",
        "transfer": "Transfer",
        "return": "Return",
        "scan_barcode": "Scan Barcode",
        "new_shipment": "+ New Shipment",
        "last_7_days": "Last 7 Days",
        "daily": "Daily",
        "priority_filter": "Priority",
        "page": "Page",
        "of": "of",
        "rows_per_page": "Rows per page"
    }
}

if 'lang' not in st.session_state:
    st.session_state.lang = 'ar'

t = LANG[st.session_state.lang]
is_ar = st.session_state.lang == 'ar'
direction = "rtl" if is_ar else "ltr"

# FORCE LIGHT THEME - Strong CSS Override
st.markdown("""
<style>
    /* FORCE LIGHT MODE ON EVERYTHING */
    .stApp, .stApp > header, .stApp > div, .stApp > div > div {
        background-color: #f8fafc !important;
    }
    
    /* Sidebar Light Background */
    section[data-testid="stSidebar"], section[data-testid="stSidebar"] > div {
        background-color: #ffffff !important;
    }
    
    /* Main Content Area */
    .main .block-container {
        background-color: #f8fafc !important;
    }
    
    /* All containers */
    div[data-testid="stVerticalBlock"], div[data-testid="stHorizontalBlock"] {
        background-color: transparent !important;
    }
    
    /* Streamlit buttons light */
    .stButton > button {
        background-color: #ffffff !important;
        color: #1e293b !important;
        border: 1px solid #e2e8f0 !important;
    }
    
    .stButton > button:hover {
        background-color: #f1f5f9 !important;
    }
    
    .stButton > button[kind="primary"] {
        background: linear-gradient(135deg, #7c3aed 0%, #6d28d9 100%) !important;
        color: white !important;
        border: none !important;
    }
    
    /* Text inputs light */
    .stTextInput > div > div > input {
        background-color: #ffffff !important;
        color: #1e293b !important;
        border: 1px solid #e2e8f0 !important;
    }
    
    /* Select boxes light */
    .stSelectbox > div > div > div {
        background-color: #ffffff !important;
        color: #1e293b !important;
    }
    
    /* Number inputs light */
    .stNumberInput > div > div > input {
        background-color: #ffffff !important;
        color: #1e293b !important;
    }
    
    /* Hide default Streamlit elements */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
</style>
""", unsafe_allow_html=True)

# Modern UI Styling
st.markdown(f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
    
    * {{
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    }}
    
    /* Header Section */
    .warehouse-header {{
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 20px 24px;
        border-bottom: 1px solid #e2e8f0;
        margin-bottom: 20px;
        background-color: #ffffff;
    }}
    
    .warehouse-icon {{
        width: 40px;
        height: 40px;
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        border-radius: 10px;
        display: flex;
        align-items: center;
        justify-content: center;
        color: white;
        font-size: 20px;
    }}
    
    .warehouse-title h2 {{
        margin: 0;
        font-size: 18px;
        font-weight: 600;
        color: #1e293b;
    }}
    
    .warehouse-title p {{
        margin: 2px 0 0 0;
        font-size: 13px;
        color: #64748b;
        font-weight: 500;
    }}
    
    /* Stat Cards */
    .stat-card {{
        background: white;
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 20px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.05);
        transition: all 0.2s;
    }}
    
    .stat-card:hover {{
        box-shadow: 0 4px 12px rgba(0,0,0,0.1);
        transform: translateY(-2px);
    }}
    
    .stat-label {{
        font-size: 13px;
        color: #64748b;
        font-weight: 500;
        margin-bottom: 8px;
        display: flex;
        align-items: center;
        gap: 6px;
    }}
    
    .stat-value {{
        font-size: 28px;
        font-weight: 700;
        color: #1e293b;
        margin-bottom: 8px;
    }}
    
    .stat-trend {{
        font-size: 12px;
        font-weight: 600;
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 4px 8px;
        border-radius: 6px;
    }}
    
    .trend-up {{
        background-color: #dcfce7;
        color: #166534;
    }}
    
    .trend-down {{
        background-color: #fee2e2;
        color: #991b1b;
    }}
    
    /* Notification Banner */
    .notification-banner {{
        background: linear-gradient(135deg, #7c3aed 0%, #6d28d9 100%);
        color: white;
        padding: 14px 20px;
        border-radius: 10px;
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 24px;
        box-shadow: 0 4px 6px rgba(124, 58, 237, 0.2);
    }}
    
    .notification-content {{
        display: flex;
        align-items: center;
        gap: 12px;
        font-weight: 500;
    }}
    
    .notification-actions {{
        display: flex;
        gap: 12px;
    }}
    
    .notification-btn {{
        background: rgba(255,255,255,0.2);
        border: none;
        color: white;
        padding: 6px 12px;
        border-radius: 6px;
        cursor: pointer;
        font-size: 13px;
        font-weight: 500;
        transition: all 0.2s;
    }}
    
    .notification-btn:hover {{
        background: rgba(255,255,255,0.3);
    }}
    
    /* Filter Bar */
    .filter-bar {{
        display: flex;
        gap: 12px;
        margin-bottom: 20px;
        flex-wrap: wrap;
    }}
    
    .filter-select {{
        background: white;
        border: 1px solid #e2e8f0;
        border-radius: 8px;
        padding: 8px 16px;
        font-size: 13px;
        font-weight: 500;
        color: #475569;
        cursor: pointer;
        transition: all 0.2s;
    }}
    
    .filter-select:hover {{
        border-color: #cbd5e1;
        box-shadow: 0 1px 3px rgba(0,0,0,0.1);
    }}
    
    /* Table Styling */
    .modern-table {{
        width: 100%;
        border-collapse: separate;
        border-spacing: 0;
        background: white;
        border-radius: 12px;
        overflow: hidden;
        box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    }}
    
    .modern-table th {{
        background-color: #f8fafc;
        padding: 12px 16px;
        text-align: left;
        font-size: 12px;
        font-weight: 600;
        color: #64748b;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        border-bottom: 1px solid #e2e8f0;
    }}
    
    .modern-table td {{
        padding: 14px 16px;
        border-bottom: 1px solid #f1f5f9;
        font-size: 14px;
        color: #334155;
        background-color: white;
    }}
    
    .modern-table tr:hover td {{
        background-color: #f8fafc;
    }}
    
    .modern-table tr:last-child td {{
        border-bottom: none;
    }}
    
    /* Status Badges */
    .badge {{
        display: inline-flex;
        align-items: center;
        padding: 6px 12px;
        border-radius: 8px;
        font-size: 12px;
        font-weight: 600;
        letter-spacing: 0.3px;
    }}
    
    .badge-urgent {{
        background-color: #fef3c7;
        color: #92400e;
    }}
    
    .badge-high {{
        background-color: #ffedd5;
        color: #c2410c;
    }}
    
    .badge-medium {{
        background-color: #dbeafe;
        color: #1e40af;
    }}
    
    .badge-low {{
        background-color: #d1fae5;
        color: #047857;
    }}
    
    /* Carrier Badge */
    .carrier-badge {{
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 6px 12px;
        background: #f1f5f9;
        border-radius: 8px;
        font-size: 13px;
        font-weight: 500;
        color: #475569;
    }}
    
    .carrier-icon {{
        width: 24px;
        height: 24px;
        border-radius: 6px;
        display: flex;
        align-items: center;
        justify-content: center;
        font-weight: 700;
        font-size: 11px;
        color: white;
    }}
    
    /* Type Icons */
    .type-icon {{
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-size: 13px;
        font-weight: 500;
    }}
    
    .type-outbound {{ color: #059669; }}
    .type-inbound {{ color: #2563eb; }}
    .type-transfer {{ color: #d97706; }}
    .type-return {{ color: #dc2626; }}
    
    /* Buttons */
    .btn-primary {{
        background: linear-gradient(135deg, #7c3aed 0%, #6d28d9 100%);
        color: white;
        border: none;
        padding: 10px 20px;
        border-radius: 8px;
        font-weight: 600;
        font-size: 14px;
        cursor: pointer;
        transition: all 0.2s;
        box-shadow: 0 2px 4px rgba(124, 58, 237, 0.3);
    }}
    
    .btn-primary:hover {{
        transform: translateY(-1px);
        box-shadow: 0 4px 8px rgba(124, 58, 237, 0.4);
    }}
    
    .btn-secondary {{
        background: white;
        color: #475569;
        border: 1px solid #e2e8f0;
        padding: 10px 20px;
        border-radius: 8px;
        font-weight: 600;
        font-size: 14px;
        cursor: pointer;
        transition: all 0.2s;
    }}
    
    .btn-secondary:hover {{
        background: #f8fafc;
        border-color: #cbd5e1;
    }}
    
    /* Top Actions */
    .top-actions {{
        display: flex;
        gap: 12px;
        margin-bottom: 24px;
    }}
    
    /* Page Title */
    .page-title {{
        font-size: 24px;
        font-weight: 700;
        color: #1e293b;
        margin-bottom: 24px;
    }}
    
    /* Custom Scrollbar */
    ::-webkit-scrollbar {{
        width: 8px;
        height: 8px;
    }}
    
    ::-webkit-scrollbar-track {{
        background: #f1f5f9;
        border-radius: 4px;
    }}
    
    ::-webkit-scrollbar-thumb {{
        background: #cbd5e1;
        border-radius: 4px;
    }}
    
    ::-webkit-scrollbar-thumb:hover {{
        background: #94a3b8;
    }}
    
    /* Checkbox styling */
    input[type="checkbox"] {{
        width: 16px;
        height: 16px;
        cursor: pointer;
        accent-color: #7c3aed;
    }}
    </style>
""", unsafe_allow_html=True)

# ----------------------------------------------------------------
# SIDEBAR NAVIGATION
# ----------------------------------------------------------------
with st.sidebar:
    # Warehouse Header
    st.markdown(f"""
    <div class="warehouse-header">
        <div class="warehouse-icon">🏢</div>
        <div class="warehouse-title">
            <h2>{t['title']}</h2>
            <p>{t['subtitle']}</p>
        </div>
    </div>
    """, unsafe_allow_html=True)
    
    # Search Box
    st.text_input("Search...", placeholder="Search...", label_visibility="collapsed")
    
    st.markdown("<div style='height: 20px;'></div>", unsafe_allow_html=True)
    
    # Navigation Menu
    st.markdown("**Navigation**")
    
    menu_items = [
        ("📊", t["nav_dash"]),
        ("📦", t["nav_items"]),
        ("🔔", t["nav_notifications"]),
        ("🚚", t["nav_shipments"]),
        ("📥", t["nav_receiving"]),
        ("📍", t["nav_zones"]),
        ("🤝", t["nav_suppliers"]),
        ("📈", t["nav_reports"]),
    ]
    
    for icon, label in menu_items:
        if st.button(f"{icon} {label}", key=label, use_container_width=True):
            st.session_state.current_page = label
    
    st.markdown("<div style='margin: 20px 0; border-top: 1px solid #e2e8f0;'></div>", unsafe_allow_html=True)
    st.markdown(f"**{t['nav_dispatch']}**")
    st.button(f"📻 {t['nav_driver_radio']}", key=t["nav_driver_radio"], use_container_width=True)
    st.button(f"📋 {t['nav_manifests']}", key=t["nav_manifests"], use_container_width=True)
    
    st.markdown("<div style='margin: 20px 0; border-top: 1px solid #e2e8f0;'></div>", unsafe_allow_html=True)
    st.markdown(f"**{t['nav_pinned']}**")
    st.button(f"🗺️ #RTE-19 East Coast Express...", key="route1", use_container_width=True)
    st.button(f"🗺️ #RTE-42 Local Restock Run...", key="route2", use_container_width=True)
    st.button(f"+ {t['nav_add_route']}", key="add_route", use_container_width=True)
    
    st.markdown("<div style='margin: 20px 0; border-top: 1px solid #e2e8f0;'></div>", unsafe_allow_html=True)
    st.button(f"❓ {t['nav_help']}", key=t["nav_help"], use_container_width=True)
    
    # Language Switcher
    st.markdown("<div style='position: fixed; bottom: 20px; left: 20px; right: 20px;'>", unsafe_allow_html=True)
    if st.button(t["lang_btn"], key="lang_switch", use_container_width=True):
        st.session_state.lang = 'en' if st.session_state.lang == 'ar' else 'ar'
        st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

# ----------------------------------------------------------------
# MAIN CONTENT AREA
# ----------------------------------------------------------------
# Page Title and Actions
col1, col2 = st.columns([3, 1])
with col1:
    st.markdown(f"<h1 class='page-title'>{t['nav_shipments']}</h1>", unsafe_allow_html=True)

with col2:
    col_a, col_b = st.columns(2)
    with col_a:
        st.button(f"📷 {t['scan_barcode']}", key="scan", use_container_width=True)
    with col_b:
        st.button(f"{t['new_shipment']}", key="new_ship", type="primary", use_container_width=True)

st.markdown("<div style='height: 20px;'></div>", unsafe_allow_html=True)

# Statistics Cards
stats_data = {
    t["pending_inbound"]: {"value": 42, "trend": "+18%", "up": True},
    t["processing"]: {"value": 128, "trend": "+12%", "up": True},
    t["awaiting_pickup"]: {"value": 36, "trend": "-4%", "up": False},
    t["completed"]: {"value": 892, "trend": "+22%", "up": True},
    t["delayed"]: {"value": 8, "trend": "+5%", "up": True},
}

cols = st.columns(5)
for idx, (label, data) in enumerate(stats_data.items()):
    with cols[idx]:
        trend_class = "trend-up" if data["up"] else "trend-down"
        trend_icon = "↑" if data["up"] else "↓"
        st.markdown(f"""
        <div class="stat-card">
            <div class="stat-label">{label}</div>
            <div class="stat-value">{data['value']}</div>
            <span class="stat-trend {trend_class}">{trend_icon} {data['trend']}</span>
        </div>
        """, unsafe_allow_html=True)

st.markdown("<div style='height: 24px;'></div>", unsafe_allow_html=True)

# Filter Bar
st.markdown(f"""
<div class="filter-bar">
    <select class="filter-select">
        <option>{t['last_7_days']}</option>
        <option>Last 30 Days</option>
        <option>This Month</option>
    </select>
    <select class="filter-select">
        <option>{t['daily']}</option>
        <option>Weekly</option>
        <option>Monthly</option>
    </select>
    <div style="flex: 1;"></div>
    <select class="filter-select">
        <option>{t['priority_filter']}</option>
        <option>Urgent</option>
        <option>High</option>
        <option>Medium</option>
    </select>
    <select class="filter-select">
        <option>{t['last_7_days']}</option>
    </select>
    <select class="filter-select">
        <option>{t['daily']}</option>
    </select>
</div>
""", unsafe_allow_html=True)

# AI Notification Banner
st.markdown(f"""
<div class="notification-banner">
    <div class="notification-content">
        <span>🤖</span>
        <span>{t['ai_notification']}</span>
    </div>
    <div class="notification-actions">
        <button class="notification-btn">{t['dismiss']}</button>
        <button class="notification-btn">{t['view_log']}</button>
    </div>
</div>
""", unsafe_allow_html=True)

# Sample Shipments Data
shipments_data = [
    {"id": "#SHP-842", "desc": "Pallet of Electronics (45 units)", "location": "Zone A - Dock 4", "type": t["outbound"], "priority": t["urgent"], "carrier": "FedEx Freight", "contact": "dispatch@fedex.com"},
    {"id": "#SHP-841", "desc": "Medical Supplies Restock", "location": "Cold Storage B", "type": t["inbound"], "priority": t["urgent"], "carrier": "DHL Express", "contact": "med@dhl.com"},
    {"id": "#SHP-840", "desc": "Office Furniture (200 lbs)", "location": "Receiving Bay 2", "type": t["inbound"], "priority": t["high"], "carrier": "UPS Supply", "contact": "freight@ups.com"},
    {"id": "#SHP-839", "desc": "Seasonal Clothing Apparel", "location": "Zone C - Aisle 12", "type": t["outbound"], "priority": t["medium"], "carrier": "XPO Logistics", "contact": "routing@xpo.co"},
    {"id": "#SHP-838", "desc": "Warehouse Racking Equipment", "location": "Staging Area", "type": t["transfer"], "priority": t["medium"], "carrier": "Internal Fleet", "contact": "fleet@warehouse.org"},
    {"id": "#SHP-837", "desc": "Defective Parts Return", "location": "QA Hold Area", "type": t["return"], "priority": t["medium"], "carrier": "USPS Commercial", "contact": "returns@usps.gov"},
    {"id": "#SHP-836", "desc": "Automotive Parts Bulk", "location": "Zone B - Dock 1", "type": t["outbound"], "priority": t["medium"], "carrier": "Old Dominion", "contact": "auto@odfl.com"},
    {"id": "#SHP-835", "desc": "Packaging Supplies", "location": "Supply Closet A", "type": t["inbound"], "priority": t["medium"], "carrier": "Uline Trcks", "contact": "delivery@uline.com"},
]

# Priority badge mapping
priority_map = {
    t["urgent"]: "badge-urgent",
    t["high"]: "badge-high",
    t["medium"]: "badge-medium",
    t["low"]: "badge-low"
}

# Type icon mapping
type_icons = {
    t["outbound"]: "📤",
    t["inbound"]: "📥",
    t["transfer"]: "⇄",
    t["return"]: "↩"
}

# Carrier colors
carrier_colors = {
    "FedEx Freight": "#4b3621",
    "DHL Express": "#d40511",
    "UPS Supply": "#b38b00",
    "XPO Logistics": "#005eb8",
    "Internal Fleet": "#64748b",
    "USPS Commercial": "#333366",
    "Old Dominion": "#e31837",
    "Uline Trcks": "#003366",
    "Sysco Logistics": "#009639",
    "Safety Transport": "#ff6b35"
}

# Build HTML Table
table_html = """
<table class="modern-table">
    <thead>
        <tr>
            <th><input type="checkbox"></th>
            <th>""" + t["shipment_id"] + """</th>
            <th>""" + t["description"] + """</th>
            <th>""" + t["location"] + """</th>
            <th>""" + t["type"] + """</th>
            <th>""" + t["priority"] + """</th>
            <th>""" + t["carrier"] + """</th>
            <th>""" + t["contact"] + """</th>
        </tr>
    </thead>
    <tbody>
"""

for shipment in shipments_data:
    priority_class = priority_map.get(shipment["priority"], "badge-medium")
    type_icon = type_icons.get(shipment["type"], "📦")
    carrier_initial = shipment["carrier"][0]
    carrier_color = carrier_colors.get(shipment["carrier"], "#64748b")
    
    table_html += f"""
        <tr>
            <td><input type="checkbox"></td>
            <td style="font-weight: 600;">{shipment["id"]}</td>
            <td>{shipment["desc"]}</td>
            <td>{shipment["location"]}</td>
            <td><span class="type-icon type-{shipment["type"].lower()}">{type_icon} {shipment["type"]}</span></td>
            <td><span class="badge {priority_class}">{shipment["priority"]}</span></td>
            <td>
                <div class="carrier-badge">
                    <div class="carrier-icon" style="background-color: {carrier_color};">{carrier_initial}</div>
                    {shipment["carrier"]}
                </div>
            </td>
            <td style="color: #64748b; font-size: 13px;">{shipment["contact"]}</td>
        </tr>
    """

table_html += """
    </tbody>
</table>
"""

st.markdown(table_html, unsafe_allow_html=True)

# Pagination
st.markdown(f"""
<div style="display: flex; justify-content: space-between; align-items: center; margin-top: 20px; padding: 0 16px; color: #64748b; font-size: 13px;">
    <div>{t['page']} <strong>1</strong> {t['of']} <strong>13</strong></div>
    <div style="display: flex; align-items: center; gap: 12px;">
        <span>{t['rows_per_page']}</span>
        <select class="filter-select" style="padding: 6px 12px;">
            <option>10</option>
            <option>25</option>
            <option>50</option>
        </select>
        <div style="display: flex; gap: 4px;">
            <button class="filter-select" style="padding: 6px 12px;" disabled>&lt;</button>
            <button class="filter-select" style="padding: 6px 12px;">&gt;</button>
        </div>
    </div>
</div>
""", unsafe_allow_html=True)
