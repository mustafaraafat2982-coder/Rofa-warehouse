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

        no = (doc_no or
