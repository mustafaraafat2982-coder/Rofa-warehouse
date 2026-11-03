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
    page_title="Rofa-warehouse System",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="collapsed"
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
        _migrate_legacy(conn)
    finally:
        conn.close()

def _migrate_legacy(conn):
    if not os.path.exists(LEGACY_DB) or os.path.abspath(LEGACY_DB) == os.path.abspath(DB_FILE):
        return
    if conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] > 0:
        return
    old = None
    try:
        old = sqlite3.connect(LEGACY_DB)
        tables = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"items", "stock_documents", "stock_ledger"} <= tables:
            return
        wh = conn.execute("SELECT id FROM warehouses ORDER BY id LIMIT 1").fetchone()[0]
        for r in old.execute("SELECT id, code, name, unit, min_stock, notes FROM items"):
            conn.execute("INSERT INTO items(id, code, name, unit, min_stock, notes) VALUES (?,?,?,?,?,?)", r)
        tmap = {"IN": "IN", "OUT": "OUT", "RETURN": "RETURN_IN", "ADJUST": "ADJUST"}
        for r in old.execute("SELECT id, doc_no, type, date, reference, status FROM stock_documents"):
            conn.execute(
                "INSERT INTO documents(id, doc_no, type, date, warehouse_id, reference, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (r[0], r[1], tmap.get(r[2], r[2]), r[3], wh, r[4], r[5] or "Approved", r[3]))
        for r in old.execute("SELECT document_id, item_id, qty, batch_no FROM stock_document_items"):
            conn.execute("INSERT INTO document_lines(document_id, item_id, qty, batch_no) VALUES (?,?,?,?)", r)
        for r in old.execute("SELECT item_id, in_qty, out_qty, document_id, date FROM stock_ledger"):
            conn.execute(
                "INSERT INTO ledger(item_id, in_qty, out_qty, document_id, warehouse_id, date) VALUES (?,?,?,?,?,?)",
                (r[0], r[1] or 0, r[2] or 0, r[3], wh, r[4] or date.today().isoformat()))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        if old is not None:
            old.close()

init_db()

# ----------------------------------------------------------------
# DATA HELPERS
# ----------------------------------------------------------------
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

def _item_code(conn, item_id):
    r = conn.execute("SELECT code FROM items WHERE id=?", (item_id,)).fetchone()
    return r[0] if r else str(item_id)

def create_document(doc_type, doc_date, warehouse_id, lines, to_warehouse_id=None, partner_id=None, reference="", notes="", doc_no=None):
    if doc_type not in DOC_TYPES or doc_type == "ADJUST":
        raise AppError("err_bad_type")
    clean = []
    for ln in lines:
        iid = ln.get("item_id")
        if iid is None or (isinstance(iid, float) and pd.isna(iid)): continue
        qty = _num(ln.get("qty"))
        if qty <= 0: raise AppError("err_qty_positive")
        clean.append({"item_id": int(iid), "qty": qty, "unit_cost": _num(ln.get("unit_cost")),
                      "batch_no": _txt(ln.get("batch_no")), "expiry": _txt(ln.get("expiry"))})
    if not clean: raise AppError("err_no_lines")
    if doc_type == "TRANSFER":
        if not to_warehouse_id or int(to_warehouse_id) == int(warehouse_id):
            raise AppError("err_transfer_wh")

    conn = connect()
    try:
        if doc_type in DECREASE or doc_type == "TRANSFER":
            need = {}
            for ln in clean: need[ln["item_id"]] = need.get(ln["item_id"], 0) + ln["qty"]
            for iid, n in need.items():
                av = available(conn, iid, warehouse_id)
                if av + EPS < n: raise AppError("err_insufficient", item=_item_code(conn, iid), avail=av, need=n)

        no = (doc_no or "").strip() or next_doc_no(conn, doc_type)
        if conn.execute("SELECT 1 FROM documents WHERE doc_no=?", (no,)).fetchone():
            raise AppError("err_doc_exists", doc_no=no)

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
            if row is None: raise AppError("err_item_missing")
            cost = row[0] or 0.0
            if doc_type == "IN" and uc > 0:
                base = max(total_qty(conn, iid), 0.0)
                new_cost = (base * cost + qty * uc) / (base + qty)
                conn.execute("UPDATE items SET cost=? WHERE id=?", (round(new_cost, 4), iid))
            elif uc <= 0: uc = cost
            conn.execute(
                "INSERT INTO document_lines(document_id, item_id, qty, unit_cost, batch_no, expiry) VALUES (?,?,?,?,?,?)",
                (doc_id, iid, qty, uc, ln["batch_no"], ln["expiry"]))
            legs = [(warehouse_id, qty, 0)] if doc_type in INCREASE else [(warehouse_id, 0, qty)] if doc_type in DECREASE else [(warehouse_id, 0, qty), (to_warehouse_id, qty, 0)]
            for wid, i_q, o_q in legs:
                conn.execute(
                    "INSERT INTO ledger(document_id, item_id, warehouse_id, in_qty, out_qty, date) VALUES (?,?,?,?,?,?)",
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
            conn.execute(
                "INSERT INTO ledger(document_id, item_id, warehouse_id, in_qty, out_qty, date) VALUES (?,?,?,?,?,?)",
                (doc_id, iid, int(warehouse_id), max(diff, 0), max(-diff, 0), doc_date))
        conn.commit()
        return no, len(changes)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def ledger_df(include_cancelled=False):
    df = q("""SELECT l.id AS ledger_id, l.date, d.doc_no, d.type, d.status, d.reference,
                       l.warehouse_id, w.name AS warehouse, l.item_id, i.code, i.name AS item, i.unit,
                       l.in_qty, l.out_qty, p.name AS partner
              FROM ledger l
              JOIN documents d ON d.id = l.document_id
              JOIN items i ON i.id = l.item_id
              JOIN warehouses w ON w.id = l.warehouse_id
              LEFT JOIN partners p ON p.id = d.partner_id
              ORDER BY l.date DESC, l.id DESC""")
    if df.empty:
        df["balance"] = pd.Series(dtype=float)
        return df
    net = np.where(df["status"] == "Approved", df["in_qty"] - df["out_qty"], 0.0)
    df["balance"] = pd.Series(net, index=df.index).groupby([df["item_id"], df["warehouse_id"]]).cumsum().round(6)
    if not include_cancelled:
        df = df[df["status"] == "Approved"]
    return df

# ----------------------------------------------------------------
# MULTI-LANGUAGE TRANSLATIONS & UI
# ----------------------------------------------------------------
LANG = {
    "ar": {
        "title": "نظام Rofa-warehouse لإدارة المخازن والدورة المستندية",
        "nav_dash": "📊 لوحة التحكم والأرصدة",
        "nav_items": "🏷️ إدارة الأصناف والجهات",
        "nav_docs": "🔄 الدورات المستندية (وارد/صادر/تحويل)",
        "nav_ledger": "📋 سجل الحركات والتصدير",
        "nav_audit": "⚖️ الجرد والتسوية الآلية",
        "total_items": "إجمالي الأصناف المعرفة",
        "total_qty": "إجمالي كمية المخزون",
        "low_stock": "أصناف بلغت حد الأمان ⚠️",
        "current_stock_title": "موقف المخزون الحالي",
        "no_items": "لا توجد أصناف مسجلة حتى الآن.",
        "lang_btn": "English 🇺🇸"
    },
    "en": {
        "title": "Rofa-warehouse Management System",
        "nav_dash": "📊 Dashboard & Stock",
        "nav_items": "🏷️ Master Data (Items & Partners)",
        "nav_docs": "🔄 Document Workflows (In/Out/Trf)",
        "nav_ledger": "📋 Master Ledger & Export",
        "nav_audit": "⚖️ Stock Audit & Adjustment",
        "total_items": "Total Items",
        "total_qty": "Total Stock Qty",
        "low_stock": "Min Stock Alert ⚠️",
        "current_stock_title": "Current Inventory Status",
        "no_items": "No items recorded yet.",
        "lang_btn": "العربية 🇪🇬"
    }
}

if 'lang' not in st.session_state:
    st.session_state.lang = 'ar'

t = LANG[st.session_state.lang]
is_ar = st.session_state.lang == 'ar'
direction = "rtl" if is_ar else "ltr"

st.markdown(f"""
    <style>
    body, html, [class*="css"] {{
        direction: {direction};
        text-align: {"right" if is_ar else "left"};
        font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
    }}
    .metric-card {{
        background-color: #1e293b;
        color: white;
        padding: 20px;
        border-radius: 12px;
        box-shadow: 0 4px 6px rgba(0,0,0,0.2);
        text-align: center;
    }}
    </style>
""", unsafe_allow_html=True)

top_col1, top_col2 = st.columns([8, 2])
with top_col2:
    if st.button(t["lang_btn"]):
        st.session_state.lang = 'en' if st.session_state.lang == 'ar' else 'ar'
        st.rerun()

with top_col1:
    st.markdown(f"## 🏢 {t['title']}")

st.markdown("---")

selected_tab = st.radio(
    "Navigation",
    [t["nav_dash"], t["nav_items"], t["nav_docs"], t["nav_ledger"], t["nav_audit"]],
    horizontal=True,
    label_visibility="collapsed"
)

st.markdown("---")

# ----------------------------------------------------------------
# TAB 1: DASHBOARD
# ----------------------------------------------------------------
if selected_tab == t["nav_dash"]:
    df_stock = stock_overview()
    total_items_count = len(df_stock)
    total_qty_sum = df_stock["qty"].sum() if not df_stock.empty else 0.0
    low_stock_count = len(df_stock[df_stock["status"] == "low"]) if not df_stock.empty else 0

    c1, c2, c3 = st.columns(3)
    with c1: st.markdown(f"""<div class="metric-card"><h3>{total_items_count}</h3><p>{t['total_items']}</p></div>""", unsafe_allow_html=True)
    with c2: st.markdown(f"""<div class="metric-card"><h3>{total_qty_sum:,.2f}</h3><p>{t['total_qty']}</p></div>""", unsafe_allow_html=True)
    with c3: st.markdown(f"""<div class="metric-card" style="background-color: #7f1d1d;"><h3>{low_stock_count}</h3><p>{t['low_stock']}</p></div>""", unsafe_allow_html=True)

    st.markdown(f"### 📦 {t['current_stock_title']}")
    if not df_stock.empty:
        st.dataframe(df_stock[["code", "name", "category", "unit", "min_stock", "cost", "qty", "value", "status"]], use_container_width=True)
    else:
        st.info(t["no_items"])

# ----------------------------------------------------------------
# TAB 2: MASTER DATA MANAGEMENT
# ----------------------------------------------------------------
elif selected_tab == t["nav_items"]:
    st.subheader("🏷️ Item Master Data / إضافة وتعريف أصناف جديدة")
    with st.form("add_item_form"):
        c1, c2, c3 = st.columns(3)
        with c1: code = st.text_input("Item Code / كود الصنف *")
        with c2: name = st.text_input("Item Name / اسم الصنف *")
        with c3: category = st.text_input("Category / التصنيف")
        
        c4, c5, c6 = st.columns(3)
        with c4: unit = st.text_input("Unit / الوحدة", value="Pcs")
        with c5: min_stock = st.number_input("Min Stock Limit / حد الأمان", min_value=0.0, value=10.0)
        with c6: cost = st.number_input("Standard Cost / التكلفة القياسية", min_value=0.0, value=0.0)
        
        notes = st.text_input("Notes / ملاحظات")
        submit_item = st.form_submit_button("Save Item / حفظ الصنف")
        
        if submit_item:
            if code and name and unit:
                try:
                    conn = connect()
                    conn.execute("INSERT INTO items (code, name, category, unit, min_stock, cost, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (code, name, category, unit, min_stock, cost, notes))
                    conn.commit()
                    conn.close()
                    st.success("Item saved successfully! / تم حفظ الصنف بنجاح!")
                except sqlite3.IntegrityError:
                    st.error("Error: Code already exists! / كود الصنف موجود مسبقاً!")
            else:
                st.warning("Fill required fields! / برجاء ملء الحقول الإجبارية!")

    st.markdown("---")
    st.dataframe(items_df(active_only=False), use_container_width=True)

# ----------------------------------------------------------------
# TAB 3: DOCUMENT WORKFLOWS
# ----------------------------------------------------------------
elif selected_tab == t["nav_docs"]:
    st.subheader("🔄 Document Workflows (Inbound / Outbound / Transfer)")
    
    df_items = items_df()
    df_wh = warehouses_df()
    df_part = partners_df()
    
    if df_items.empty or df_wh.empty:
        st.warning("Please define items and warehouses first!")
    else:
        with st.form("doc_workflow_form"):
            c1, c2, c3 = st.columns(3)
            with c1: doc_type = st.selectbox("Document Type / نوع المستند", ["IN", "OUT", "RETURN_IN", "RETURN_OUT", "TRANSFER"])
            with c2: doc_date = st.date_input("Date / التاريخ", value=date.today())
            with c3: doc_no = st.text_input("Doc No / رقم المستند (تلقائي إن ترك فارغاً)")
            
            c4, c5, c6 = st.columns(3)
            with c4:
                wh_id = st.selectbox("From Warehouse / من مخزن", df_wh['id'].tolist(), format_func=lambda x: df_wh[df_wh['id']==x]['name'].values[0])
            with c5:
                to_wh_id = st.selectbox("To Warehouse (Transfer only) / إلى مخزن", [None] + df_wh['id'].tolist(), format_func=lambda x: "None" if x is None else df_wh[df_wh['id']==x]['name'].values[0])
            with c6:
                partner_id = st.selectbox("Partner / المورد أو العميل", [None] + df_part['id'].tolist(), format_func=lambda x: "None" if x is None else df_part[df_part['id']==x]['name'].values[0]) if not df_part.empty else None

            reference = st.text_input("Reference / المرجع (رقم الفاتورة أو أمر التشغيل)")
            notes = st.text_input("Notes / ملاحظات")
            
            st.markdown("#### Item Line / بند الحركة")
            item_id = st.selectbox("Item / الصنف", df_items['id'].tolist(), format_func=lambda x: f"{df_items[df_items['id']==x]['code'].values[0]} - {df_items[df_items['id']==x]['name'].values[0]}")
            qty = st.number_input("Quantity / الكمية", min_value=0.01, value=1.0)
            unit_cost = st.number_input("Unit Cost / سعر الوحدة", min_value=0.0, value=0.0)
            batch_no = st.text_input("Batch No / رقم التشغيلة")
            
            submit_doc = st.form_submit_button("Post Document / اعتماد المستند")
            
            if submit_doc:
                try:
                    lines = [{"item_id": item_id, "qty": qty, "unit_cost": unit_cost, "batch_no": batch_no}]
                    d_id, d_no = create_document(doc_type, doc_date, wh_id, lines, to_warehouse_id=to_wh_id, partner_id=partner_id, reference=reference, notes=notes, doc_no=doc_no)
                    st.success(f"Document {d_no} posted successfully!")
                except AppError as e:
                    st.error(f"Error: {e.key}")
                except Exception as e:
                    st.error(f"System Error: {e}")

# ----------------------------------------------------------------
# TAB 4: MASTER LEDGER LOG & EXPORT
# ----------------------------------------------------------------
elif selected_tab == t["nav_ledger"]:
    st.subheader("📋 Master Ledger Log / سجل الحركات الكلي")
    df_led = ledger_df()
    if not df_led.empty:
        st.dataframe(df_led, use_container_width=True)
        
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as w:
            df_led.to_excel(w, index=False, sheet_name="Ledger")
            
        st.download_button(
            label="📥 Export Ledger to Excel / تصدير السجل لإكسيل",
            data=buf.getvalue(),
            file_name=f"rofa_ledger_{datetime.now().strftime('%Y%m%d')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    else:
        st.info("No ledger movements recorded yet.")

# ----------------------------------------------------------------
# TAB 5: AUDIT & RECONCILIATION
# ----------------------------------------------------------------
elif selected_tab == t["nav_audit"]:
    st.subheader("⚖️ Inventory Audit & Reconciliation / الجرد والتسوية المخزنية")
    df_wh = warehouses_df()
    
    if not df_wh.empty:
        selected_wh = st.selectbox("Select Warehouse for Audit / اختر المخزن للجرد", df_wh['id'].tolist(), format_func=lambda x: df_wh[df_wh['id']==x]['name'].values[0])
        df_stock = stock_overview(warehouse_id=selected_wh)
        
        if not df_stock.empty:
            with st.form("audit_form"):
                counts = []
                for _, row in df_stock.iterrows():
                    c1, c2, c3 = st.columns([3, 2, 2])
                    with c1: st.write(f"**{row['code']} - {row['name']}**")
                    with c2: st.write(f"Book: {row['qty']} {row['unit']}")
                    with c3:
                        phys = st.number_input(f"Physical Qty", min_value=0.0, value=float(row['qty']), key=f"aud_{row['id']}")
                        counts.append((row['id'], phys))
                
                notes = st.text_input("Audit Notes / ملاحظات لجنة الجرد")
                submit_audit = st.form_submit_button("Process Reconciliation / اعتماد وتسوية الفروق")
                
                if submit_audit:
                    try:
                        no, count = create_adjustment(selected_wh, counts, notes=notes)
                        if no:
                            st.success(f"Audit document {no} generated with {count} adjustments!")
                        else:
                            st.info("No stock differences found.")
                    except Exception as e:
                        st.error(f"Audit Error: {e}")
