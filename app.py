import io
import os
import sqlite3
import tempfile
from datetime import date, datetime, timedelta
import numpy as np
import pandas as pd
import streamlit as st

# ==============================================================================
# 1. BACKEND: قاعدة البيانات وقواعد العمل (النظام الأساسي)
# ==============================================================================
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
CREATE TABLE IF NOT EXISTS warehouses(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, location TEXT, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, category TEXT, unit TEXT NOT NULL DEFAULT 'Pcs', min_stock REAL NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0, price REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, notes TEXT);
CREATE TABLE IF NOT EXISTS partners(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, type TEXT NOT NULL DEFAULT 'SUPPLIER', phone TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS documents(id INTEGER PRIMARY KEY AUTOINCREMENT, doc_no TEXT UNIQUE NOT NULL, type TEXT NOT NULL, date TEXT NOT NULL, warehouse_id INTEGER NOT NULL REFERENCES warehouses(id), to_warehouse_id INTEGER REFERENCES warehouses(id), partner_id INTEGER REFERENCES partners(id), reference TEXT, notes TEXT, status TEXT NOT NULL DEFAULT 'Approved', created_at TEXT, cancelled_at TEXT);
CREATE TABLE IF NOT EXISTS document_lines(id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL REFERENCES documents(id), item_id INTEGER NOT NULL REFERENCES items(id), qty REAL NOT NULL, unit_cost REAL NOT NULL DEFAULT 0, batch_no TEXT, expiry TEXT);
CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL REFERENCES documents(id), item_id INTEGER NOT NULL REFERENCES items(id), warehouse_id INTEGER NOT NULL REFERENCES warehouses(id), in_qty REAL NOT NULL DEFAULT 0, out_qty REAL NOT NULL DEFAULT 0, date TEXT NOT NULL);
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

def items_df(active_only=True):
    sql = "SELECT id, code, name, category, unit, min_stock, cost, price, active, notes FROM items"
    if active_only: sql += " WHERE active=1"
    return q(sql + " ORDER BY code")

def warehouses_df(active_only=True):
    sql = "SELECT id, code, name, location, active FROM warehouses"
    if active_only: sql += " WHERE active=1"
    return q(sql + " ORDER BY id")

def partners_df():
    return q("SELECT id, name, type, phone, notes FROM partners ORDER BY name")

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
    r = conn.execute("""SELECT COALESCE(SUM(l.in_qty - l.out_qty), 0) FROM ledger l
           JOIN documents d ON d.id = l.document_id
           WHERE d.status='Approved' AND l.item_id=? AND l.warehouse_id=?""", (item_id, warehouse_id)).fetchone()
    return round(r[0], 6)

def total_qty(conn, item_id):
    r = conn.execute("""SELECT COALESCE(SUM(l.in_qty - l.out_qty), 0) FROM ledger l
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
        clean.append({"item_id": int(iid), "qty": qty, "unit_cost": _num(ln.get("unit_cost")), "batch_no": _txt(ln.get("batch_no")), "expiry": _txt(ln.get("expiry"))})
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
                if av + EPS < n: 
                    code = conn.execute("SELECT code FROM items WHERE id=?", (iid,)).fetchone()[0]
                    raise AppError(f"الرصيد غير كافٍ للصنف {code}. المتاح: {av}، المطلوب: {n}")

        no = (doc_no or "").strip() or next_doc_no(conn, doc_type)
        cur = conn.execute("""INSERT INTO documents(doc_no, type, date, warehouse_id, to_warehouse_id, partner_id, reference, notes, status, created_at)
               VALUES (?,?,?,?,?,?,?,?, 'Approved', ?)""",
            (no, doc_type, str(doc_date), int(warehouse_id), int(to_warehouse_id) if doc_type == "TRANSFER" else None,
             int(partner_id) if partner_id else None, _txt(reference), _txt(notes), datetime.now().isoformat(timespec="seconds")))
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
            
            conn.execute("INSERT INTO document_lines(document_id, item_id, qty, unit_cost, batch_no, expiry) VALUES (?,?,?,?,?,?)", (doc_id, iid, qty, uc, ln["batch_no"], ln["expiry"]))
            legs = [(warehouse_id, qty, 0)] if doc_type in INCREASE else [(warehouse_id, 0, qty)] if doc_type in DECREASE else [(warehouse_id, 0, qty), (to_warehouse_id, qty, 0)]
            for wid, i_q, o_q in legs:
                conn.execute("INSERT INTO ledger(document_id, item_id, warehouse_id, in_qty, out_qty, date) VALUES (?,?,?,?,?,?)", (doc_id, iid, int(wid), i_q, o_q, str(doc_date)))
        conn.commit()
        return doc_id, no
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def documents_df():
    return q("""SELECT d.doc_no, d.type, d.date, d.status, d.reference, w.name AS warehouse, 
                       (SELECT COUNT(*) FROM document_lines x WHERE x.document_id = d.id) AS lines_count,
                       (SELECT COALESCE(SUM(ABS(x.qty) * x.unit_cost), 0) FROM document_lines x WHERE x.document_id = d.id) AS total_value
                FROM documents d JOIN warehouses w ON w.id = d.warehouse_id ORDER BY d.date DESC, d.id DESC""")

def ledger_df():
    return q("""SELECT l.date, d.doc_no, d.type, d.status, w.name AS warehouse, i.code, i.name AS item, i.unit, l.in_qty, l.out_qty
                FROM ledger l JOIN documents d ON d.id = l.document_id JOIN items i ON i.id = l.item_id JOIN warehouses w ON w.id = l.warehouse_id
                ORDER BY l.date DESC, l.id DESC""")

def load_demo_data():
    conn = connect()
    try:
        if conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] > 0:
            return False # Data already exists
        conn.execute("INSERT INTO categories(name) VALUES ('مواد خام'), ('منتجات تامة'), ('مستلزمات')")
        conn.execute("INSERT INTO warehouses(code, name, location) VALUES ('BR1', 'مخزن الفرع', 'الفرع الثاني')")
        conn.execute("INSERT INTO partners(name, type, phone) VALUES ('شركة النور للتوريدات', 'SUPPLIER', '01000000001')")
        conn.execute("INSERT INTO partners(name, type, phone) VALUES ('عميل - الشروق', 'CUSTOMER', '01000000003')")
        
        items = [("RM-001", "دقيق فاخر", "مواد خام", "كجم", 200, 18.0, 0.0), 
                 ("RM-002", "سكر أبيض", "مواد خام", "كجم", 150, 24.0, 0.0),
                 ("FG-001", "بسكويت سادة", "منتجات تامة", "علبة", 80, 12.0, 20.0),
                 ("PK-001", "كرتونة تغليف", "مستلزمات", "قطعة", 300, 4.0, 0.0)]
        for r in items:
            conn.execute("INSERT INTO items(code, name, category, unit, min_stock, cost, price) VALUES (?,?,?,?,?,?,?)", r)
        conn.commit()
        
        # Create an initial IN document
        wh_main = conn.execute("SELECT id FROM warehouses WHERE code='MAIN'").fetchone()[0]
        sup_id = conn.execute("SELECT id FROM partners WHERE type='SUPPLIER'").fetchone()[0]
        item_ids = {r[0]: r[1] for r in conn.execute("SELECT code, id FROM items").fetchall()}
        
        lines = [{"item_id": item_ids["RM-001"], "qty": 500, "unit_cost": 18.0}, {"item_id": item_ids["RM-002"], "qty": 300, "unit_cost": 24.0}]
        create_document("IN", date.today().isoformat(), wh_main, lines, partner_id=sup_id, reference="PO-1001")
        return True
    finally:
        conn.close()

# ==============================================================================
# 2. FRONTEND: واجهة المستخدم الحديثة (Light Theme)
# ==============================================================================
st.set_page_config(page_title="نظام Rofa للمخازن", page_icon="📦", layout="wide", initial_sidebar_state="expanded")

# Force Light Theme CSS
st.markdown("""
<style>
    /* Force Light Background Everywhere */
    .stApp, .stApp > header, .stApp > div, .stApp > div > div, .main .block-container {
        background-color: #f8fafc !important;
    }
    section[data-testid="stSidebar"], section[data-testid="stSidebar"] > div {
        background-color: #ffffff !important;
        border-right: 1px solid #e2e8f0;
    }
    /* Inputs & Selects */
    .stTextInput > div > div > input, .stNumberInput > div > div > input, .stSelectbox > div > div > div, .stTextArea > div > div > textarea {
        background-color: #ffffff !important;
        color: #1e293b !important;
        border: 1px solid #e2e8f0 !important;
    }
    /* Buttons */
    .stButton > button {
        background-color: #ffffff !important;
        color: #1e293b !important;
        border: 1px solid #e2e8f0 !important;
        font-weight: 600;
    }
    .stButton > button:hover { background-color: #f1f5f9 !important; }
    .stButton > button[kind="primary"] {
        background: linear-gradient(135deg, #7c3aed 0%, #6d28d9 100%) !important;
        color: white !important;
        border: none !important;
    }
    /* Hide Streamlit Branding */
    #MainMenu, footer, header {visibility: hidden;}
    
    /* Custom Components */
    .stat-card {
        background: white; border: 1px solid #e2e8f0; border-radius: 12px;
        padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); text-align: center;
    }
    .stat-value { font-size: 28px; font-weight: 700; color: #1e293b; margin: 8px 0; }
    .stat-label { font-size: 13px; color: #64748b; font-weight: 500; }
    .badge-low { background-color: #fee2e2; color: #991b1b; padding: 4px 10px; border-radius: 6px; font-size: 12px; font-weight: 600; }
    .badge-ok { background-color: #dcfce7; color: #166534; padding: 4px 10px; border-radius: 6px; font-size: 12px; font-weight: 600; }
</style>
""", unsafe_allow_html=True)

# Initialize DB
init_db()

# Sidebar Navigation
with st.sidebar:
    st.markdown("""
    <div style="display: flex; align-items: center; gap: 12px; padding: 20px 0; border-bottom: 1px solid #e2e8f0; margin-bottom: 20px;">
        <div style="width: 40px; height: 40px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); border-radius: 10px; display: flex; align-items: center; justify-content: center; color: white; font-size: 20px;">🏢</div>
        <div><h3 style="margin: 0; font-size: 18px; color: #1e293b;">Rofa Warehouse</h3><p style="margin: 2px 0 0; font-size: 12px; color: #64748b;">نظام إدارة المخازن</p></div>
    </div>
    """, unsafe_allow_html=True)
    
    page = st.radio("القائمة الرئيسية", ["📊 لوحة التحكم", "📦 الأصناف والمخازن", "📝 إنشاء مستند جديد", "📋 سجل الحركات (Ledger)", "⚙️ الإعدادات"], label_visibility="collapsed")

# ==============================================================================
# 3. PAGES LOGIC
# ==============================================================================

if page == "📊 لوحة التحكم":
    st.markdown("<h2 style='color: #1e293b; margin-bottom: 24px;'>لوحة التحكم العامة</h2>", unsafe_allow_html=True)
    
    df_stock = stock_overview()
    total_items = len(df_stock)
    total_value = df_stock["value"].sum()
    low_stock_count = len(df_stock[df_stock["status"] == "low"])
    
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown(f"""<div class="stat-card"><div class="stat-label">إجمالي الأصناف</div><div class="stat-value">{total_items}</div></div>""", unsafe_allow_html=True)
    with c2:
        st.markdown(f"""<div class="stat-card"><div class="stat-label">قيمة المخزون (تقريبي)</div><div class="stat-value">{total_value:,.0f} ج.م</div></div>""", unsafe_allow_html=True)
    with c3:
        st.markdown(f"""<div class="stat-card"><div class="stat-label">تنبيهات حد الأمان</div><div class="stat-value" style="color: #991b1b;">{low_stock_count}</div></div>""", unsafe_allow_html=True)

    st.markdown("<h3 style='color: #1e293b; margin-top: 32px;'>📉 الأصناف التي وصلت لحد الأمان</h3>", unsafe_allow_html=True)
    low_items = df_stock[df_stock["status"] == "low"][["code", "name", "category", "qty", "min_stock", "status"]]
    if not low_items.empty:
        low_items["status"] = low_items["status"].apply(lambda x: '<span class="badge-low">منخفض</span>')
        st.markdown(low_items.to_html(escape=False, index=False), unsafe_allow_html=True)
    else:
        st.success("✅ جميع الأصناف متوفرة بكميات آمنة.")

    st.markdown("<h3 style='color: #1e293b; margin-top: 32px;'>📄 آخر المستندات</h3>", unsafe_allow_html=True)
    recent_docs = documents_df().head(5)
    if not recent_docs.empty:
        st.dataframe(recent_docs, use_container_width=True, hide_index=True)

elif page == "📦 الأصناف والمخازن":
    st.markdown("<h2 style='color: #1e293b; margin-bottom: 24px;'>إدارة الأصناف</h2>", unsafe_allow_html=True)
    
    with st.expander("➕ إضافة صنف جديد", expanded=False):
        with st.form("add_item"):
            c1, c2, c3 = st.columns(3)
            with c1:
                code = st.text_input("كود الصنف *")
                name = st.text_input("اسم الصنف *")
            with c2:
                category = st.text_input("التصنيف")
                unit = st.text_input("الوحدة", value="Pcs")
            with c3:
                min_stock = st.number_input("حد الأمان", min_value=0.0, value=0.0)
                cost = st.number_input("التكلفة القياسية", min_value=0.0, value=0.0)
            
            if st.form_submit_button("حفظ الصنف", type="primary"):
                if code and name:
                    try:
                        conn = connect()
                        conn.execute("INSERT INTO items(code, name, category, unit, min_stock, cost) VALUES (?,?,?,?,?,?)", (code, name, category, unit, min_stock, cost))
                        conn.commit()
                        conn.close()
                        st.success("تم حفظ الصنف بنجاح!")
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error("خطأ: كود الصنف موجود مسبقاً!")
                else:
                    st.error("يجب ملء حقول الكود والاسم.")

    st.markdown("<h3 style='color: #1e293b; margin-top: 24px;'>قائمة الأصناف الحالية</h3>", unsafe_allow_html=True)
    st.dataframe(items_df(active_only=False), use_container_width=True, hide_index=True)

elif page == "📝 إنشاء مستند جديد":
    st.markdown("<h2 style='color: #1e293b; margin-bottom: 24px;'>إنشاء مستند مخزني</h2>", unsafe_allow_html=True)
    
    if 'doc_lines' not in st.session_state:
        st.session_state.doc_lines = []

    with st.form("doc_header"):
        c1, c2, c3 = st.columns(3)
        with c1:
            doc_type = st.selectbox("نوع المستند", ["IN", "OUT", "TRANSFER", "RETURN_IN", "RETURN_OUT"])
        with c2:
            doc_date = st.date_input("التاريخ", date.today())
        with c3:
            wh_df = warehouses_df()
            wh_id = st.selectbox("المخزن", wh_df['id'].tolist(), format_func=lambda x: wh_df[wh_df['id']==x]['name'].values[0])
        
        to_wh_id = None
        if doc_type == "TRANSFER":
            to_wh_id = st.selectbox("المخزن المحول إليه", wh_df['id'].tolist(), format_func=lambda x: wh_df[wh_df['id']==x]['name'].values[0])
            
        partner_id = None
        if doc_type in ["IN", "OUT", "RETURN_IN", "RETURN_OUT"]:
            part_df = partners_df()
            if not part_df.empty:
                partner_id = st.selectbox("الجهة (مورد/عميل)", [None] + part_df['id'].tolist(), format_func=lambda x: "اختر جهة" if x is None else part_df[part_df['id']==x]['name'].values[0])
                
        reference = st.text_input("رقم مرجعي (فاتورة/أمر تشغيل)")
        notes = st.text_input("ملاحظات")
        
        st.markdown("---")
        st.markdown("##### إضافة بند للمستند")
        items_list = items_df()
        if items_list.empty:
            st.warning("⚠️ يجب إضافة أصناف أولاً من صفحة 'الأصناف والمخازن'")
        else:
            item_options = {f"{row['code']} - {row['name']}": row['id'] for _, row in items_list.iterrows()}
            c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
            with c1:
                selected_item_name = st.selectbox("الصنف", list(item_options.keys()))
            with c2:
                qty = st.number_input("الكمية", min_value=0.01, value=1.0)
            with c3:
                cost = st.number_input("التكلفة", min_value=0.0, value=0.0)
            with c4:
                st.write("")
                st.write("")
                add_line = st.form_submit_button("إضافة للبند")
                
            if add_line:
                st.session_state.doc_lines.append({"item_id": item_options[selected_item_name], "qty": qty, "unit_cost": cost})
                st.rerun()

    # Display added lines
    if st.session_state.doc_lines:
        lines_df = pd.DataFrame(st.session_state.doc_lines)
        display_df = lines_df.merge(items_list[['id', 'code', 'name']], left_on='item_id', right_on='id')
        st.dataframe(display_df[['code', 'name', 'qty', 'unit_cost']], use_container_width=True, hide_index=True)
        
        c1, c2 = st.columns([1, 5])
        with c1:
            if st.button("مسح البنود"):
                st.session_state.doc_lines = []
                st.rerun()
        with c2:
            if st.button("✅ اعتماد وحفظ المستند", type="primary"):
                try:
                    doc_id, doc_no = create_document(doc_type, doc_date, wh_id, st.session_state.doc_lines, to_warehouse_id=to_wh_id, partner_id=partner_id, reference=reference, notes=notes)
                    st.success(f"تم إنشاء المستند بنجاح! رقم المستند: **{doc_no}**")
                    st.session_state.doc_lines = []
                    st.rerun()
                except AppError as e:
                    st.error(f"❌ خطأ في البيانات: {str(e)}")
                except Exception as e:
                    st.error(f"❌ خطأ في النظام: {str(e)}")

elif page == "📋 سجل الحركات (Ledger)":
    st.markdown("<h2 style='color: #1e293b; margin-bottom: 24px;'>سجل حركات المخزن (Ledger)</h2>", unsafe_allow_html=True)
    
    df_ledger = ledger_df()
    if not df_ledger.empty:
        st.dataframe(df_ledger, use_container_width=True, hide_index=True)
        
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df_ledger.to_excel(writer, index=False, sheet_name="Ledger")
        
        st.download_button(
            label="📥 تصدير السجل إلى Excel",
            data=buf.getvalue(),
            file_name=f"rofa_ledger_{date.today().strftime('%Y%m%d')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    else:
        st.info("لا توجد حركات مسجلة حتى الآن.")

elif page == "⚙️ الإعدادات":
    st.markdown("<h2 style='color: #1e293b; margin-bottom: 24px;'>إعدادات النظام</h2>", unsafe_allow_html=True)
    
    st.markdown("### بيانات تجريبية")
    st.write("استخدم هذا الزر لتعبئة النظام ببيانات تجريبية (أصناف، مخازن، موردين، وحركة أولية) لتجربة النظام.")
    if st.button("تحميل البيانات التجريبية", type="primary"):
        if load_demo_data():
            st.success("تم تحميل البيانات التجريبية بنجاح! سيتم إعادة تحميل الصفحة.")
            st.rerun()
        else:
            st.warning("النظام يحتوي بالفعل على بيانات. لم يتم إضافة بيانات تجريبية لتجنب التكرار.")

    st.markdown("---")
    st.markdown("### النسخ الاحتياطي")
    st.write("قم بتحميل نسخة من قاعدة البيانات الحالية لحفظها بأمان.")
    try:
        conn = connect()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        tmp.close()
        dst = sqlite3.connect(tmp.name)
        conn.backup(dst)
        dst.close()
        conn.close()
        
        with open(tmp.name, "rb") as f:
            db_bytes = f.read()
        os.unlink(tmp.name)
        
        st.download_button(
            label="💾 تحميل نسخة قاعدة البيانات (.db)",
            data=db_bytes,
            file_name=f"rofa_backup_{date.today().strftime('%Y%m%d')}.db",
            mime="application/x-sqlite3"
        )
    except Exception as e:
        st.error(f"فشل إنشاء النسخة: {e}")
