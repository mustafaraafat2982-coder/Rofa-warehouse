import io
import os
import sqlite3
import tempfile
from datetime import date, datetime, timedelta
import numpy as np
import pandas as pd
import streamlit as st

# ==============================================================================
# BACKEND: قاعدة البيانات وقواعد العمل
# ==============================================================================
DB_FILE = os.environ.get("ROFA_DB", "rofa_warehouse_v2.db")

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
    try:
        conn = connect()
        return pd.read_sql_query(sql, conn, params=params)
    except Exception as e:
        st.error(f"خطأ في قاعدة البيانات: {str(e)}")
        return pd.DataFrame()
    finally:
        conn.close()

SCHEMA = """
CREATE TABLE IF NOT EXISTS warehouses(
    id INTEGER PRIMARY KEY AUTOINCREMENT, 
    code TEXT UNIQUE NOT NULL, 
    name TEXT NOT NULL, 
    location TEXT, 
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS categories(
    id INTEGER PRIMARY KEY AUTOINCREMENT, 
    name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS items(
    id INTEGER PRIMARY KEY AUTOINCREMENT, 
    code TEXT UNIQUE NOT NULL, 
    name TEXT NOT NULL, 
    category TEXT, 
    unit TEXT NOT NULL DEFAULT 'Pcs', 
    min_stock REAL NOT NULL DEFAULT 0, 
    cost REAL NOT NULL DEFAULT 0, 
    price REAL NOT NULL DEFAULT 0, 
    weight REAL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1, 
    notes TEXT
);
CREATE TABLE IF NOT EXISTS partners(
    id INTEGER PRIMARY KEY AUTOINCREMENT, 
    name TEXT NOT NULL, 
    type TEXT NOT NULL DEFAULT 'SUPPLIER', 
    phone TEXT, 
    notes TEXT
);
CREATE TABLE IF NOT EXISTS documents(
    id INTEGER PRIMARY KEY AUTOINCREMENT, 
    doc_no TEXT UNIQUE NOT NULL, 
    type TEXT NOT NULL, 
    date TEXT NOT NULL, 
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id), 
    to_warehouse_id INTEGER REFERENCES warehouses(id), 
    partner_id INTEGER REFERENCES partners(id), 
    reference TEXT, 
    notes TEXT, 
    status TEXT NOT NULL DEFAULT 'Approved', 
    created_at TEXT, 
    cancelled_at TEXT
);
CREATE TABLE IF NOT EXISTS document_lines(
    id INTEGER PRIMARY KEY AUTOINCREMENT, 
    document_id INTEGER NOT NULL REFERENCES documents(id), 
    item_id INTEGER NOT NULL REFERENCES items(id), 
    qty REAL NOT NULL, 
    unit_cost REAL NOT NULL DEFAULT 0, 
    batch_no TEXT, 
    expiry TEXT
);
CREATE TABLE IF NOT EXISTS ledger(
    id INTEGER PRIMARY KEY AUTOINCREMENT, 
    document_id INTEGER NOT NULL REFERENCES documents(id), 
    item_id INTEGER NOT NULL REFERENCES items(id), 
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id), 
    in_qty REAL NOT NULL DEFAULT 0, 
    out_qty REAL NOT NULL DEFAULT 0, 
    date TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ledger_item ON ledger(item_id, warehouse_id);
CREATE INDEX IF NOT EXISTS ix_ledger_doc ON ledger(document_id);
CREATE INDEX IF NOT EXISTS ix_lines_doc ON document_lines(document_id);
CREATE INDEX IF NOT EXISTS ix_docs_date ON documents(date);
"""

def init_db():
    """تأكد من إنشاء قاعدة البيانات بشكل صحيح"""
    try:
        conn = connect()
        conn.executescript(SCHEMA)
        
        # تأكد من وجود المخزن الرئيسي
        if conn.execute("SELECT COUNT(*) FROM warehouses").fetchone()[0] == 0:
            conn.execute("INSERT INTO warehouses(code, name, location) VALUES ('MAIN', 'المخزن الرئيسي', '')")
            conn.commit()
        
        conn.close()
    except Exception as e:
        st.error(f"خطأ في تهيئة قاعدة البيانات: {str(e)}")

def items_df(active_only=True):
    try:
        sql = "SELECT id, code, name, category, unit, min_stock, cost, price, weight, active, notes FROM items"
        if active_only: 
            sql += " WHERE active=1"
        return q(sql + " ORDER BY code")
    except Exception as e:
        st.error(f"خطأ في جلب الأصناف: {str(e)}")
        return pd.DataFrame(columns=['id', 'code', 'name', 'category', 'unit', 'min_stock', 'cost', 'price', 'weight', 'active', 'notes'])

def warehouses_df(active_only=True):
    sql = "SELECT id, code, name, location, active FROM warehouses"
    if active_only: 
        sql += " WHERE active=1"
    return q(sql + " ORDER BY id")

def partners_df():
    return q("SELECT id, name, type, phone, notes FROM partners ORDER BY name")

def stock_rows():
    return q("""SELECT l.item_id, l.warehouse_id, SUM(l.in_qty - l.out_qty) AS qty
                FROM ledger l JOIN documents d ON d.id = l.document_id
                WHERE d.status = 'Approved' GROUP BY l.item_id, l.warehouse_id""")

def stock_overview(warehouse_id=None, include_inactive=False):
    df = items_df(active_only=not include_inactive)
    if df.empty:
        return pd.DataFrame(columns=['id', 'code', 'name', 'category', 'unit', 'min_stock', 'cost', 'price', 'weight', 'active', 'notes', 'qty', 'value', 'status'])
    
    rows = stock_rows()
    if warehouse_id is not None and not rows.empty:
        rows = rows[rows["warehouse_id"] == warehouse_id]
    
    if rows.empty:
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
    except Exception as e:
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

def update_item_in_db(item_id, field, value):
    """تحديث حقل معين في جدول الأصناف"""
    try:
        conn = connect()
        conn.execute(f"UPDATE items SET {field}=? WHERE id=?", (value, item_id))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        st.error(f"خطأ في التحديث: {str(e)}")
        return False

def add_product_to_inventory(code, name, category, unit, qty, weight, cost, price, min_stock, notes=""):
    """إضافة منتج جديد للمخزون مع الكمية والوزن"""
    conn = connect()
    try:
        # إضافة الصنف
        conn.execute("""INSERT INTO items(code, name, category, unit, min_stock, cost, price, weight, notes) 
                       VALUES (?,?,?,?,?,?,?,?,?)""", 
                    (code, name, category, unit, min_stock, cost, price, weight, notes))
        conn.commit()
        
        item_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        
        # إذا كانت هناك كمية، أضفها للمخزن
        if qty > 0:
            wh_id = conn.execute("SELECT id FROM warehouses WHERE code='MAIN'").fetchone()[0]
            today = date.today().isoformat()
            doc_no = next_doc_no(conn, "IN")
            
            # إنشاء مستند وارد
            cur = conn.execute("""INSERT INTO documents(doc_no, type, date, warehouse_id, status, created_at)
                                 VALUES (?, 'IN', ?, ?, 'Approved', ?)""",
                             (doc_no, today, wh_id, datetime.now().isoformat(timespec="seconds")))
            doc_id = cur.lastrowid
            
            # إضافة بند للمستند
            conn.execute("""INSERT INTO document_lines(document_id, item_id, qty, unit_cost) 
                           VALUES (?,?,?,?)""", (doc_id, item_id, qty, cost))
            
            # إضافة للحركة
            conn.execute("""INSERT INTO ledger(document_id, item_id, warehouse_id, in_qty, out_qty, date) 
                           VALUES (?,?,?,?,?,?)""", (doc_id, item_id, wh_id, qty, 0, today))
        
        conn.commit()
        return True, "تم إضافة المنتج بنجاح"
    except sqlite3.IntegrityError:
        return False, "كود الصنف موجود مسبقاً"
    except Exception as e:
        conn.rollback()
        return False, f"خطأ: {str(e)}"
    finally:
        conn.close()

# ==============================================================================
# FRONTEND: واجهة المستخدم
# ==============================================================================
st.set_page_config(page_title="نظام Rofa للمخازن", page_icon="📦", layout="wide", initial_sidebar_state="collapsed")

# تهيئة قاعدة البيانات
init_db()

# CSS Styling
st.markdown("""
<style>
    .stApp, .main .block-container {
        background-color: #f8fafc !important;
    }
    
    .nav-container {
        display: flex;
        gap: 8px;
        padding: 16px 24px;
        background-color: #1e3a8a;
        border-radius: 12px;
        margin-bottom: 24px;
        box-shadow: 0 4px 6px rgba(30, 58, 138, 0.2);
    }
    
    .nav-button {
        flex: 1;
        background-color: #1e40af;
        color: white !important;
        padding: 14px 24px;
        border-radius: 8px;
        text-align: center;
        cursor: pointer;
        font-weight: 600;
        font-size: 15px;
        transition: all 0.3s;
        border: 2px solid transparent;
    }
    
    .nav-button:hover {
        background-color: #3b82f6;
        transform: translateY(-2px);
        box-shadow: 0 4px 12px rgba(59, 130, 246, 0.4);
    }
    
    .nav-button.active {
        background-color: #60a5fa;
        border-color: #93c5fd;
    }
    
    .stButton > button {
        background-color: #1e40af !important;
        color: white !important;
        border: none !important;
        font-weight: 600;
        padding: 10px 24px;
        border-radius: 8px;
    }
    
    .stButton > button:hover { 
        background-color: #3b82f6 !important;
    }
    
    .stat-card {
        background: white;
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 20px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.05);
        text-align: center;
        border-right: 4px solid #1e40af;
    }
    
    .stat-value {
        font-size: 32px;
        font-weight: 700;
        color: #1e40af;
        margin: 8px 0;
    }
    
    .stat-label {
        font-size: 14px;
        color: #64748b;
        font-weight: 600;
    }
    
    #MainMenu, footer, header {visibility: hidden;}
    
    .section-header {
        background-color: #1e3a8a;
        color: white;
        padding: 12px 20px;
        border-radius: 8px;
        margin: 24px 0 16px 0;
        font-size: 18px;
        font-weight: 700;
    }
</style>
""", unsafe_allow_html=True)

# Navigation State
if 'current_page' not in st.session_state:
    st.session_state.current_page = "dashboard"

# Navigation Bar
nav_pages = {
    "dashboard": "📊 لوحة التحكم",
    "inventory": "📦 المخزون والمنتجات",
    "add_product": "➕ إضافة منتج",
    "documents": "📝 المستندات",
    "ledger": "📋 سجل الحركات"
}

st.markdown('<div class="nav-container">', unsafe_allow_html=True)
cols = st.columns(len(nav_pages))
for idx, (page_key, page_name) in enumerate(nav_pages.items()):
    with cols[idx]:
        is_active = st.session_state.current_page == page_key
        if st.button(page_name, key=f"nav_{page_key}", use_container_width=True):
            st.session_state.current_page = page_key
            st.rerun()
st.markdown('</div>', unsafe_allow_html=True)

# ==============================================================================
# PAGES
# ==============================================================================

if st.session_state.current_page == "dashboard":
    st.markdown('<div class="section-header">📊 لوحة التحكم العامة</div>', unsafe_allow_html=True)
    
    try:
        df_stock = stock_overview()
        total_items = len(df_stock)
        total_value = df_stock["value"].sum()
        low_stock_count = len(df_stock[df_stock["status"] == "low"])
        total_qty = df_stock["qty"].sum()
        
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.markdown(f"""<div class="stat-card">
                <div class="stat-label">إجمالي الأصناف</div>
                <div class="stat-value">{total_items}</div>
            </div>""", unsafe_allow_html=True)
        with c2:
            st.markdown(f"""<div class="stat-card">
                <div class="stat-label">إجمالي الكمية</div>
                <div class="stat-value">{total_qty:,.0f}</div>
            </div>""", unsafe_allow_html=True)
        with c3:
            st.markdown(f"""<div class="stat-card">
                <div class="stat-label">قيمة المخزون</div>
                <div class="stat-value">{total_value:,.0f}</div>
            </div>""", unsafe_allow_html=True)
        with c4:
            st.markdown(f"""<div class="stat-card">
                <div class="stat-label">تنبيهات حد الأمان</div>
                <div class="stat-value" style="color: #dc2626;">{low_stock_count}</div>
            </div>""", unsafe_allow_html=True)

        st.markdown('<div class="section-header">️ الأصناف التي وصلت لحد الأمان</div>', unsafe_allow_html=True)
        if not df_stock.empty:
            low_items = df_stock[df_stock["status"] == "low"][["code", "name", "category", "qty", "min_stock", "unit"]]
            if not low_items.empty:
                st.dataframe(low_items, use_container_width=True, hide_index=True)
            else:
                st.success("✅ جميع الأصناف متوفرة بكميات آمنة.")
        else:
            st.info("📭 لا توجد أصناف مسجلة حتى الآن.")

        st.markdown('<div class="section-header"> آخر المستندات</div>', unsafe_allow_html=True)
        recent_docs = documents_df()
        if not recent_docs.empty:
            st.dataframe(recent_docs.head(5), use_container_width=True, hide_index=True)
        else:
            st.info("لا توجد مستندات مسجلة حتى الآن.")
    except Exception as e:
        st.error(f"خطأ في عرض لوحة التحكم: {str(e)}")

elif st.session_state.current_page == "inventory":
    st.markdown('<div class="section-header">📦 إدارة المخزون والمنتجات</div>', unsafe_allow_html=True)
    
    try:
        df_items = items_df(active_only=False)
        
        if not df_items.empty:
            st.markdown("### 📋 جدول المنتجات (قابل للتعديل)")
            st.markdown("يمكنك التعديل مباشرة في الجدول ثم الضغط على حفظ التغييرات")
            
            edited_df = st.data_editor(
                df_items[['id', 'code', 'name', 'category', 'unit', 'min_stock', 'cost', 'price', 'weight', 'notes']],
                column_config={
                    "id": st.column_config.NumberColumn("ID", disabled=True),
                    "code": st.column_config.TextColumn("الكود"),
                    "name": st.column_config.TextColumn("الاسم"),
                    "category": st.column_config.TextColumn("التصنيف"),
                    "unit": st.column_config.TextColumn("الوحدة"),
                    "min_stock": st.column_config.NumberColumn("حد الأمان"),
                    "cost": st.column_config.NumberColumn("التكلفة"),
                    "price": st.column_config.NumberColumn("السعر"),
                    "weight": st.column_config.NumberColumn("الوزن"),
                    "notes": st.column_config.TextColumn("ملاحظات"),
                },
                hide_index=True,
                use_container_width=True,
                num_rows="dynamic"
            )
            
            if st.button(" حفظ التغييرات", type="primary", use_container_width=True):
                for index, row in edited_df.iterrows():
                    if index < len(df_items):
                        orig_row = df_items.iloc[index]
                        if not row.equals(orig_row):
                            for col in ['code', 'name', 'category', 'unit', 'min_stock', 'cost', 'price', 'weight', 'notes']:
                                if row[col] != orig_row[col]:
                                    update_item_in_db(int(row['id']), col, row[col])
                st.success("✅ تم حفظ التغييرات بنجاح!")
                st.rerun()
            
            st.markdown("---")
            st.markdown("### 📊 حالة المخزون الحالية")
            stock_df = stock_overview()
            if not stock_df.empty:
                display_df = stock_df[['code', 'name', 'category', 'qty', 'unit', 'min_stock', 'status', 'value']].copy()
                display_df['القيمة'] = display_df['value'].apply(lambda x: f"{x:,.2f}")
                display_df['الحالة'] = display_df['status'].apply(lambda x: "⚠️ منخفض" if x == "low" else "✅ جيد" if x == "ok" else "❌ نفد")
                st.dataframe(display_df[['code', 'name', 'qty', 'unit', 'القيمة', 'الحالة']], use_container_width=True, hide_index=True)
        else:
            st.info("📭 لا توجد أصناف مسجلة. انتقل إلى صفحة 'إضافة منتج' لإضافة أصناف جديدة.")
    except Exception as e:
        st.error(f"خطأ في عرض المخزون: {str(e)}")

elif st.session_state.current_page == "add_product":
    st.markdown('<div class="section-header">➕ إضافة منتج جديد للمخزون</div>', unsafe_allow_html=True)
    
    with st.form("add_product_form", clear_on_submit=False):
        c1, c2 = st.columns(2)
        with c1:
            code = st.text_input("كود الصنف *", placeholder="مثال: PRD-001")
            name = st.text_input("اسم المنتج *", placeholder="مثال: منتج_example")
            category = st.text_input("التصنيف", placeholder="مثال: إلكترونيات")
        
        with c2:
            unit = st.selectbox("الوحدة", ["قطعة", "كجم", "لتر", "علبة", "كرتونة", "متر"])
            min_stock = st.number_input("حد الأمان (الحد الأدنى)", min_value=0.0, value=0.0)
        
        st.markdown("#### 📦 الكمية والوزن")
        st.markdown("اختر ما تريد إدخاله (الكمية أو الوزن أو الاثنين معاً)")
        
        c3, c4 = st.columns(2)
        with c3:
            use_qty = st.checkbox("إدخال الكمية", value=True)
            qty = st.number_input("الكمية", min_value=0.0, value=0.0, disabled=not use_qty) if use_qty else 0
        
        with c4:
            use_weight = st.checkbox("إدخال الوزن", value=False)
            weight = st.number_input("الوزن", min_value=0.0, value=0.0, disabled=not use_weight) if use_weight else 0
        
        st.markdown("#### 💰 الأسعار")
        c5, c6 = st.columns(2)
        with c5:
            cost = st.number_input("التكلفة", min_value=0.0, value=0.0, step=0.01)
        with c6:
            price = st.number_input("سعر البيع", min_value=0.0, value=0.0, step=0.01)
        
        notes = st.text_area("ملاحظات إضافية", placeholder="أي معلومات إضافية عن المنتج...")
        
        submitted = st.form_submit_button("✅ إضافة المنتج للمخزون", type="primary", use_container_width=True)
        
        if submitted:
            if not code or not name:
                st.error("❌ يجب ملء حقلي الكود والاسم (الحقول المطلوبة)")
            else:
                success, message = add_product_to_inventory(
                    code=code,
                    name=name,
                    category=category,
                    unit=unit,
                    qty=qty if use_qty else 0,
                    weight=weight if use_weight else 0,
                    cost=cost,
                    price=price,
                    min_stock=min_stock,
                    notes=notes
                )
                
                if success:
                    st.success(f"✅ {message}")
                    if qty > 0:
                        st.info(f"📦 تم إضافة {qty} {unit} للمخزن الرئيسي")
                    if weight > 0:
                        st.info(f"⚖️ الوزن المسجل: {weight} كجم")
                    st.balloons()
                else:
                    st.error(f"❌ {message}")

elif st.session_state.current_page == "documents":
    st.markdown('<div class="section-header">📝 المستندات المخزنية</div>', unsafe_allow_html=True)
    
    if 'doc_lines' not in st.session_state:
        st.session_state.doc_lines = []

    with st.expander("📝 إنشاء مستند جديد", expanded=True):
        with st.form("doc_header"):
            c1, c2, c3 = st.columns(3)
            with c1:
                doc_type = st.selectbox("نوع المستند", ["IN (وارد)", "OUT (صادر)", "TRANSFER (تحويل)", "RETURN_IN (مرتجع وارد)", "RETURN_OUT (مرتجع صادر)"])
            with c2:
                doc_date = st.date_input("التاريخ", date.today())
            with c3:
                wh_df = warehouses_df()
                if not wh_df.empty:
                    wh_id = st.selectbox("المخزن", wh_df['id'].tolist(), format_func=lambda x: wh_df[wh_df['id']==x]['name'].values[0])
                else:
                    st.warning("لا توجد مخازن مسجلة")
                    wh_id = None
            
            to_wh_id = None
            if wh_df is not None and not wh_df.empty and "TRANSFER" in doc_type:
                to_wh_id = st.selectbox("المخزن المحول إليه", wh_df['id'].tolist(), format_func=lambda x: wh_df[wh_df['id']==x]['name'].values[0])
                
            partner_id = None
            if "IN" in doc_type or "OUT" in doc_type or "RETURN" in doc_type:
                part_df = partners_df()
                if part_df is not None and not part_df.empty:
                    partner_id = st.selectbox("الجهة (مورد/عميل)", [None] + part_df['id'].tolist(), format_func=lambda x: "اختر جهة" if x is None else part_df[part_df['id']==x]['name'].values[0])
                    
            reference = st.text_input("رقم مرجعي (فاتورة/أمر تشغيل)")
            notes = st.text_input("ملاحظات")
            
            st.markdown("---")
            st.markdown("##### إضافة بند للمستند")
            items_list = items_df()
            if items_list is None or items_list.empty:
                st.warning("⚠️ يجب إضافة أصناف أولاً من صفحة 'إضافة منتج'")
            else:
                item_options = {f"{row['code']} - {row['name']}": row['id'] for _, row in items_list.iterrows()}
                c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
                with c1:
                    selected_item_name = st.selectbox("الصنف", list(item_options.keys()), key="doc_item")
                with c2:
                    qty = st.number_input("الكمية", min_value=0.01, value=1.0, key="doc_qty")
                with c3:
                    cost = st.number_input("التكلفة", min_value=0.0, value=0.0, key="doc_cost")
                with c4:
                    st.write("")
                    st.write("")
                    add_line = st.form_submit_button("➕ إضافة", use_container_width=True)
                    
                if add_line:
                    st.session_state.doc_lines.append({"item_id": item_options[selected_item_name], "qty": qty, "unit_cost": cost})
                    st.rerun()

        if st.session_state.doc_lines:
            lines_df = pd.DataFrame(st.session_state.doc_lines)
            if items_list is not None and not items_list.empty:
                display_df = lines_df.merge(items_list[['id', 'code', 'name']], left_on='item_id', right_on='id')
                st.dataframe(display_df[['code', 'name', 'qty', 'unit_cost']], use_container_width=True, hide_index=True)
            
            c1, c2 = st.columns([1, 5])
            with c1:
                if st.button("🗑️ مسح البنود"):
                    st.session_state.doc_lines = []
                    st.rerun()
            with c2:
                if st.button("✅ اعتماد وحفظ المستند", type="primary", use_container_width=True):
                    try:
                        doc_type_code = doc_type.split(" ")[0]
                        doc_id, doc_no = create_document(doc_type_code, doc_date, wh_id, st.session_state.doc_lines, to_warehouse_id=to_wh_id, partner_id=partner_id, reference=reference, notes=notes)
                        st.success(f"✅ تم إنشاء المستند بنجاح! رقم المستند: **{doc_no}**")
                        st.session_state.doc_lines = []
                        st.rerun()
                    except AppError as e:
                        st.error(f" خطأ في البيانات: {str(e)}")
                    except Exception as e:
                        st.error(f"❌ خطأ في النظام: {str(e)}")

    st.markdown("---")
    st.markdown("### 📋 المستندات المسجلة")
    docs_df = documents_df()
    if docs_df is not None and not docs_df.empty:
        st.dataframe(docs_df, use_container_width=True, hide_index=True)
    else:
        st.info("لا توجد مستندات مسجلة حتى الآن.")

elif st.session_state.current_page == "ledger":
    st.markdown('<div class="section-header">📋 سجل الحركات المخزنية (Ledger)</div>', unsafe_allow_html=True)
    
    df_ledger = ledger_df()
    if df_ledger is not None and not df_ledger.empty:
        st.markdown("### 📊 سجل الحركات")
        st.dataframe(df_ledger, use_container_width=True, hide_index=True)
        
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df_ledger.to_excel(writer, index=False, sheet_name="Ledger")
        
        st.download_button(
            label="📥 تصدير السجل إلى Excel",
            data=buf.getvalue(),
            file_name=f"rofa_ledger_{date.today().strftime('%Y%m%d')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )
    else:
        st.info("📭 لا توجد حركات مسجلة حتى الآن.")

# Footer
st.markdown("---")
st.markdown("""
<div style='text-align: center; color: #64748b; padding: 20px; font-size: 14px;'>
    <strong>نظام Rofa لإدارة المخازن</strong> | الإصدار 2.0 | © 2024
</div>
""", unsafe_allow_html=True)
