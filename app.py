import io
import os
import sqlite3
import tempfile
from datetime import date, datetime, timedelta
import numpy as np
import pandas as pd
import streamlit as st
import base64

# ==============================================================================
# BACKEND - نظام مخزن الخامات
# ==============================================================================
DB_FILE = os.environ.get("ROFA_DB", "rofa_raw_materials.db")

DOC_TYPES = ["IN", "OUT", "ADJUST"]
DOC_PREFIX = {"IN": "IN", "OUT": "OUT", "ADJUST": "ADJ"}
INCREASE = {"IN"}
DECREASE = {"OUT"}
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
        return pd.DataFrame()
    finally:
        try:
            conn.close()
        except:
            pass

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_materials(
    id INTEGER PRIMARY KEY AUTOINCREMENT, 
    code TEXT UNIQUE NOT NULL, 
    name TEXT NOT NULL, 
    unit TEXT NOT NULL DEFAULT 'كجم',
    bag_weight REAL NOT NULL DEFAULT 25,
    opening_balance REAL NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1, 
    notes TEXT
);
CREATE TABLE IF NOT EXISTS movements(
    id INTEGER PRIMARY KEY AUTOINCREMENT, 
    date TEXT NOT NULL,
    material_id INTEGER NOT NULL REFERENCES raw_materials(id),
    movement_type TEXT NOT NULL,
    qty REAL NOT NULL,
    reference TEXT,
    party TEXT,
    notes TEXT,
    created_at TEXT
);
"""

def init_db():
    try:
        conn = connect()
        conn.executescript(SCHEMA)
        conn.close()
    except Exception as e:
        st.error(f"خطأ في تهيئة قاعدة البيانات: {str(e)}")

def materials_df(active_only=True):
    try:
        sql = "SELECT id, code, name, unit, bag_weight, opening_balance, active, notes FROM raw_materials"
        if active_only: sql += " WHERE active=1"
        return q(sql + " ORDER BY code")
    except:
        return pd.DataFrame(columns=['id', 'code', 'name', 'unit', 'bag_weight', 'opening_balance', 'active', 'notes'])

def movements_df():
    return q("""SELECT m.id, m.date, r.code, r.name, r.unit, m.movement_type, m.qty, m.reference, m.party, m.notes
                FROM movements m JOIN raw_materials r ON r.id = m.material_id
                ORDER BY m.date DESC, m.id DESC""")

def get_material_totals():
    """حساب إجمالي الوارد والمنصرف لكل خامة"""
    totals = q("""
        SELECT material_id,
               SUM(CASE WHEN movement_type='IN' THEN qty ELSE 0 END) as total_in,
               SUM(CASE WHEN movement_type='OUT' THEN qty ELSE 0 END) as total_out
        FROM movements
        GROUP BY material_id
    """)
    return totals

def get_current_balance(conn, material_id):
    """حساب الرصيد الحالي لخامة معينة"""
    material = conn.execute("SELECT opening_balance FROM raw_materials WHERE id=?", (material_id,)).fetchone()
    if not material:
        return 0
    opening = material[0]
    
    totals = conn.execute("""
        SELECT 
            COALESCE(SUM(CASE WHEN movement_type='IN' THEN qty ELSE 0 END), 0) as total_in,
            COALESCE(SUM(CASE WHEN movement_type='OUT' THEN qty ELSE 0 END), 0) as total_out
        FROM movements WHERE material_id=?
    """, (material_id,)).fetchone()
    
    return opening + totals[0] - totals[1]

def add_material(code, name, unit, bag_weight, opening_balance, notes=""):
    """إضافة خامة جديدة"""
    conn = connect()
    try:
        conn.execute("""INSERT INTO raw_materials(code, name, unit, bag_weight, opening_balance, notes) 
                       VALUES (?,?,?,?,?,?)""", (code, name, unit, bag_weight, opening_balance, notes))
        conn.commit()
        conn.close()
        return True, "تم إضافة الخامة بنجاح"
    except sqlite3.IntegrityError:
        return False, "كود الخامة موجود مسبقاً"
    except Exception as e:
        conn.rollback()
        return False, f"خطأ: {str(e)}"
    finally:
        try:
            conn.close()
        except:
            pass

def add_movement(date, material_id, movement_type, qty, reference="", party="", notes=""):
    """تسجيل حركة (وارد/منصرف)"""
    conn = connect()
    try:
        # التحقق من الرصيد الكافي في حالة الصرف
        if movement_type == "OUT":
            current_balance = get_current_balance(conn, material_id)
            if current_balance < qty:
                return False, f"الرصيد غير كافٍ. المتاح: {current_balance:.2f}، المطلوب: {qty}"
        
        cur = conn.execute("""INSERT INTO movements(date, material_id, movement_type, qty, reference, party, notes, created_at)
                             VALUES (?,?,?,?,?,?,?,?)""",
            (str(date), int(material_id), movement_type, float(qty), reference, party, notes, 
             datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        conn.close()
        return True, "تم تسجيل الحركة بنجاح"
    except Exception as e:
        try:
            conn.rollback()
            conn.close()
        except:
            pass
        return False, f"خطأ: {str(e)}"

def update_material(item_id, field, value):
    """تحديث حقل في جدول الخامات"""
    try:
        conn = connect()
        conn.execute(f"UPDATE raw_materials SET {field}=? WHERE id=?", (value, item_id))
        conn.commit()
        conn.close()
        return True
    except:
        return False

def delete_material(item_id):
    """حذف خامة"""
    try:
        conn = connect()
        # التحقق من عدم وجود حركات
        movements_count = conn.execute("SELECT COUNT(*) FROM movements WHERE material_id=?", (item_id,)).fetchone()[0]
        if movements_count > 0:
            conn.close()
            return False, "لا يمكن حذف خامة لها حركات مسجلة"
        conn.execute("DELETE FROM raw_materials WHERE id=?", (item_id,))
        conn.commit()
        conn.close()
        return True, "تم الحذف بنجاح"
    except Exception as e:
        try:
            conn.close()
        except:
            pass
        return False, f"خطأ: {str(e)}"

# ==============================================================================
# FRONTEND
# ==============================================================================
st.set_page_config(page_title="نظام ROFA لمخزن الخامات", page_icon="📦", layout="wide", initial_sidebar_state="collapsed")

init_db()

# LOGO - احفظ الصورة باسم rofa_logo.png في نفس المجلد
LOGO_PATH = "rofa_logo.png"
logo_html = ""
if os.path.exists(LOGO_PATH):
    with open(LOGO_PATH, "rb") as f:
        logo_b64 = base64.b64encode(f.read()).decode()
    logo_html = f'<img src="data:image/png;base64,{logo_b64}" style="height: 70px; margin-left: 20px;">'

# CSS - خلفية بيج + نصوص واضحة + لوجو
st.markdown(f"""
<style>
    /* BEIGE BACKGROUND */
    .stApp, .main .block-container {{
        background-color: #f5f0e1 !important;
    }}
    
    /* HEADER WITH LOGO ON LEFT */
    .header-container {{
        display: flex;
        align-items: center;
        justify-content: flex-start;
        padding: 15px 20px;
        background: linear-gradient(135deg, #1e3a8a 0%, #1e40af 100%);
        border-radius: 12px;
        margin-bottom: 20px;
        box-shadow: 0 4px 6px rgba(30, 58, 138, 0.3);
    }}
    
    .header-logo {{
        height: 70px;
        margin-left: 20px;
    }}
    
    .header-title {{
        font-size: 26px;
        font-weight: 700;
        color: white;
        margin: 0;
    }}
    
    .header-subtitle {{
        font-size: 13px;
        color: #dbeafe;
        margin: 4px 0 0 0;
    }}
    
    /* NAVIGATION - Dark Blue */
    .nav-container {{
        display: flex;
        gap: 10px;
        padding: 12px;
        background-color: #1e3a8a;
        border-radius: 12px;
        margin-bottom: 24px;
        box-shadow: 0 4px 6px rgba(30, 58, 138, 0.2);
    }}
    
    /* CLEAR TEXT IN INPUTS */
    .stTextInput > div > div > input, 
    .stNumberInput > div > div > input, 
    .stSelectbox > div > div > div, 
    .stTextArea > div > div > textarea {{
        background-color: #ffffff !important;
        color: #1e293b !important;
        border: 1px solid #cbd5e1 !important;
        font-weight: 500;
    }}
    
    /* Labels - Dark Blue */
    label, .stTextInput label, .stNumberInput label, .stSelectbox label, .stTextArea label {{
        color: #1e3a8a !important;
        font-weight: 600 !important;
        font-size: 14px !important;
    }}
    
    /* Buttons */
    .stButton > button {{
        background-color: #1e40af !important;
        color: white !important;
        border: none !important;
        font-weight: 600;
        padding: 10px 24px;
        border-radius: 8px;
    }}
    
    .stButton > button:hover {{ 
        background-color: #3b82f6 !important;
    }}
    
    /* Stat Cards */
    .stat-card {{
        background: white;
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 20px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.05);
        text-align: center;
        border-right: 4px solid #1e40af;
    }}
    
    .stat-value {{
        font-size: 28px;
        font-weight: 700;
        color: #1e40af;
        margin: 8px 0;
    }}
    
    .stat-label {{
        font-size: 14px;
        color: #64748b;
        font-weight: 600;
    }}
    
    #MainMenu, footer, header {{visibility: hidden;}}
    
    .section-header {{
        background-color: #1e3a8a;
        color: white;
        padding: 12px 20px;
        border-radius: 8px;
        margin: 24px 0 16px 0;
        font-size: 18px;
        font-weight: 700;
    }}
    
    .form-container {{
        background: white;
        padding: 24px;
        border-radius: 12px;
        border: 1px solid #e2e8f0;
        margin: 16px 0;
    }}
    
    /* Badge colors */
    .badge-in {{
        background-color: #dcfce7;
        color: #166534;
        padding: 4px 10px;
        border-radius: 6px;
        font-size: 12px;
        font-weight: 600;
    }}
    
    .badge-out {{
        background-color: #fee2e2;
        color: #991b1b;
        padding: 4px 10px;
        border-radius: 6px;
        font-size: 12px;
        font-weight: 600;
    }}
    
    .low-stock {{
        background-color: #fef3c7;
        color: #92400e;
    }}
</style>
""", unsafe_allow_html=True)

# Navigation
if 'current_page' not in st.session_state:
    st.session_state.current_page = "raw_materials"

# HEADER WITH LOGO ON LEFT
st.markdown(f"""
<div class="header-container">
    {logo_html if logo_html else '<div style="width: 70px; height: 70px; background: white; border-radius: 10px; display: flex; align-items: center; justify-content: center; margin-left: 20px; font-size: 32px;">📦</div>'}
    <div>
        <h1 class="header-title">نظام ROFA لمخزن الخامات</h1>
        <p class="header-subtitle">إدارة خامات البلاستيك والأنواع المختلفة - الوارد والمنصرف والأرصدة</p>
    </div>
</div>
""", unsafe_allow_html=True)

# Navigation Bar
nav_pages = {
    "raw_materials": "📦 مخزن الخام (الرئيسية)",
    "movements": "🔄 حركة الخام",
    "add_material": "➕ إضافة خامة جديدة",
    "reports": "📊 التقارير"
}

st.markdown('<div class="nav-container">', unsafe_allow_html=True)
cols = st.columns(len(nav_pages))
for idx, (page_key, page_name) in enumerate(nav_pages.items()):
    with cols[idx]:
        if st.button(page_name, key=f"nav_{page_key}", use_container_width=True):
            st.session_state.current_page = page_key
            st.rerun()
st.markdown('</div>', unsafe_allow_html=True)

# ==============================================================================
# PAGE 1: مخزن الخام (الشاشة الرئيسية)
# ==============================================================================
if st.session_state.current_page == "raw_materials":
    st.markdown('<div class="section-header">📦 مخزن الخام - لوحة الأرصدة الرئيسية</div>', unsafe_allow_html=True)
    
    try:
        df_materials = materials_df(active_only=False)
        df_totals = get_material_totals()
        
        if not df_materials.empty:
            # دمج البيانات
            if not df_totals.empty:
                df_materials = df_materials.merge(df_totals, left_on='id', right_on='material_id', how='left')
                df_materials['total_in'] = df_materials['total_in'].fillna(0)
                df_materials['total_out'] = df_materials['total_out'].fillna(0)
            else:
                df_materials['total_in'] = 0
                df_materials['total_out'] = 0
            
            # حساب المتبقي وعدد الشكاير
            df_materials['remaining'] = df_materials['opening_balance'] + df_materials['total_in'] - df_materials['total_out']
            df_materials['bags_count'] = (df_materials['remaining'] / df_materials['bag_weight']).round(2)
            
            # إحصائيات
            total_materials = len(df_materials)
            total_remaining = df_materials['remaining'].sum()
            total_value_kg = total_remaining
            low_stock_count = 0
            
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                st.markdown(f"""<div class="stat-card">
                    <div class="stat-label">إجمالي الخامات</div>
                    <div class="stat-value">{total_materials}</div>
                </div>""", unsafe_allow_html=True)
            with c2:
                st.markdown(f"""<div class="stat-card">
                    <div class="stat-label">إجمالي المتبقي (كجم)</div>
                    <div class="stat-value">{total_remaining:,.2f}</div>
                </div>""", unsafe_allow_html=True)
            with c3:
                total_bags = df_materials['bags_count'].sum()
                st.markdown(f"""<div class="stat-card">
                    <div class="stat-label">إجمالي الشكاير</div>
                    <div class="stat-value">{total_bags:,.1f}</div>
                </div>""", unsafe_allow_html=True)
            with c4:
                st.markdown(f"""<div class="stat-card">
                    <div class="stat-label">خامات منخفضة</div>
                    <div class="stat-value" style="color: #dc2626;">{low_stock_count}</div>
                </div>""", unsafe_allow_html=True)
            
            st.markdown("---")
            st.markdown("### 📋 جدول أرصدة الخامات (قابل للتعديل)")
            st.info("💡 يمكنك التعديل مباشرة في الجدول - العمود 'المتبقي' و'عدد الشكاير' يُحسبان تلقائياً")
            
            display_df = df_materials[['code', 'name', 'unit', 'bag_weight', 'opening_balance', 'total_in', 'total_out', 'remaining', 'bags_count', 'notes']].copy()
            display_df.columns = ['الكود', 'اسم الخامة', 'الوحدة', 'وزن الشكارة', 'الافتتاحي', 'الوارد', 'المنصرف', 'المتبقي (كجم)', 'عدد الشكاير', 'ملاحظات']
            
            st.dataframe(display_df, use_container_width=True, hide_index=True)
            
            st.markdown("---")
            st.markdown("### ✏️ تعديل الخامات")
            
            edited_df = st.data_editor(
                df_materials[['id', 'code', 'name', 'unit', 'bag_weight', 'opening_balance', 'notes']],
                column_config={
                    "id": st.column_config.NumberColumn("ID", disabled=True),
                    "code": st.column_config.TextColumn("الكود"),
                    "name": st.column_config.TextColumn("اسم الخامة"),
                    "unit": st.column_config.TextColumn("الوحدة"),
                    "bag_weight": st.column_config.NumberColumn("وزن الشكارة (كجم)"),
                    "opening_balance": st.column_config.NumberColumn("الرصيد الافتتاحي"),
                    "notes": st.column_config.TextColumn("ملاحظات"),
                },
                hide_index=True,
                use_container_width=True,
                num_rows="dynamic"
            )
            
            if st.button("💾 حفظ التغييرات", type="primary", use_container_width=True):
                for index, row in edited_df.iterrows():
                    if index < len(df_materials):
                        orig_row = df_materials.iloc[index]
                        if not row.equals(orig_row):
                            for col in ['code', 'name', 'unit', 'bag_weight', 'opening_balance', 'notes']:
                                if row[col] != orig_row[col]:
                                    update_material(int(row['id']), col, row[col])
                st.success("✅ تم حفظ التغييرات بنجاح!")
                st.rerun()
        else:
            st.info("📭 لا توجد خامات مسجلة. انتقل إلى صفحة 'إضافة خامة جديدة' للبدء.")
    except Exception as e:
        st.error(f"خطأ: {str(e)}")

# ==============================================================================
# PAGE 2: حركة الخام
# ==============================================================================
elif st.session_state.current_page == "movements":
    st.markdown('<div class="section-header">🔄 حركة الخام - سجل الحركات اليومية</div>', unsafe_allow_html=True)
    
    # نموذج تسجيل حركة جديدة
    with st.form("movement_form", clear_on_submit=True):
        st.markdown('<div class="form-container">', unsafe_allow_html=True)
        
        c1, c2, c3 = st.columns(3)
        with c1:
            movement_date = st.date_input("التاريخ", date.today())
        with c2:
            movement_type = st.selectbox("نوع الحركة", ["IN (وارد)", "OUT (منصرف)"])
        with c3:
            materials_list = materials_df()
            if not materials_list.empty:
                material_options = {f"{row['code']} - {row['name']}": row['id'] for _, row in materials_list.iterrows()}
                selected_material = st.selectbox("اختر الخامة (بالكود)", list(material_options.keys()))
            else:
                st.warning("⚠️ يجب إضافة خامات أولاً")
                selected_material = None
        
        c4, c5 = st.columns(2)
        with c4:
            qty = st.number_input("الكمية", min_value=0.01, value=0.0, step=0.01)
        with c5:
            reference = st.text_input("رقم مرجعي (فاتورة/أمر)", placeholder="مثال: PO-123")
        
        c6, c7 = st.columns(2)
        with c6:
            party = st.text_input("الجهة / السبب", placeholder="مثال: مورد / خط إنتاج 1")
        with c7:
            notes = st.text_input("ملاحظات")
        
        st.markdown('</div>', unsafe_allow_html=True)
        
        submitted = st.form_submit_button("✅ تسجيل الحركة", type="primary", use_container_width=True)
        
        if submitted:
            if selected_material is None:
                st.error("❌ يجب اختيار خامة")
            elif qty <= 0:
                st.error("❌ الكمية يجب أن تكون أكبر من صفر")
            else:
                success, message = add_movement(
                    date=movement_date,
                    material_id=material_options[selected_material],
                    movement_type=movement_type.split(" ")[0],
                    qty=qty,
                    reference=reference,
                    party=party,
                    notes=notes
                )
                
                if success:
                    st.success(f"✅ {message}")
                    st.balloons()
                    # عرض الرصيد الحالي بعد الحركة
                    conn = connect()
                    current_balance = get_current_balance(conn, material_options[selected_material])
                    conn.close()
                    st.info(f"📊 الرصيد الحالي للخامة: **{current_balance:,.2f} كجم**")
                else:
                    st.error(f"❌ {message}")
    
    st.markdown("---")
    st.markdown("### 📋 سجل الحركات المسجلة")
    
    df_movements = movements_df()
    if not df_movements.empty:
        # إضافة عمود الرصيد الحالي لكل حركة
        conn = connect()
        balances = []
        for _, row in df_movements.iterrows():
            balance = get_current_balance(conn, row['material_id'])
            balances.append(balance)
        conn.close()
        df_movements['current_balance'] = balances
        
        display_movements = df_movements[['date', 'code', 'name', 'unit', 'movement_type', 'qty', 'current_balance', 'reference', 'party']].copy()
        display_movements.columns = ['التاريخ', 'الكود', 'اسم الخامة', 'الوحدة', 'النوع', 'الكمية', 'الرصيد الحالي', 'المرجع', 'الجهة']
        display_movements['النوع'] = display_movements['النوع'].apply(lambda x: '📥 وارد' if x == 'IN' else '📤 منصرف')
        
        st.dataframe(display_movements, use_container_width=True, hide_index=True)
        
        # تصدير Excel
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            display_movements.to_excel(writer, index=False, sheet_name="حركة الخام")
        
        st.download_button(
            label="📥 تصدير الحركات إلى Excel",
            data=buf.getvalue(),
            file_name=f"rofa_movements_{date.today().strftime('%Y%m%d')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )
    else:
        st.info("📭 لا توجد حركات مسجلة حتى الآن.")

# ==============================================================================
# PAGE 3: إضافة خامة جديدة
# ==============================================================================
elif st.session_state.current_page == "add_material":
    st.markdown('<div class="section-header">➕ إضافة خامة جديدة للمخزن</div>', unsafe_allow_html=True)
    
    with st.form("add_material_form", clear_on_submit=True):
        st.markdown('<div class="form-container">', unsafe_allow_html=True)
        
        st.markdown("#### 📝 البيانات الأساسية")
        c1, c2, c3 = st.columns(3)
        with c1:
            code = st.text_input("كود الخامة *", placeholder="مثال: 1801 أو 218W")
        with c2:
            name = st.text_input("اسم الخامة *", placeholder="مثال: لينير ناشف")
        with c3:
            unit = st.selectbox("الوحدة", ["كجم", "طن", "لتر", "قطعة"])
        
        st.markdown("#### ⚖️ الوزن والرصيد")
        c4, c5 = st.columns(2)
        with c4:
            bag_weight = st.number_input("وزن الشكارة القياسي (كجم)", min_value=0.1, value=25.0, step=0.5, help="الوزن القياسي للشكارة لحساب عدد الشكاير")
        with c5:
            opening_balance = st.number_input("الرصيد الافتتاحي (كجم)", min_value=0.0, value=0.0, step=0.01, help="رصيد أول الفترة من الجرد السابق")
        
        notes = st.text_area("ملاحظات", placeholder="أي معلومات إضافية عن الخامة...")
        
        st.markdown('</div>', unsafe_allow_html=True)
        
        submitted = st.form_submit_button("✅ إضافة الخامة", type="primary", use_container_width=True)
        
        if submitted:
            if not code or not name:
                st.error("❌ يجب ملء حقلي الكود والاسم")
            else:
                success, message = add_material(
                    code=code, name=name, unit=unit, 
                    bag_weight=bag_weight, opening_balance=opening_balance, notes=notes
                )
                
                if success:
                    st.success(f"✅ {message}")
                    st.info(f"📦 الخامة: **{name}** (كود: {code})")
                    st.info(f"⚖️ وزن الشكارة: {bag_weight} كجم")
                    st.info(f"📊 الرصيد الافتتاحي: {opening_balance} كجم = {opening_balance/bag_weight:.2f} شكارة")
                    st.balloons()
                    
                    st.markdown("---")
                    st.markdown("### ✅ خطوات التحقق:")
                    st.markdown("1. انتقل إلى صفحة **مخزن الخام** للتأكد من ظهور الخامة")
                    st.markdown("2. انتقل إلى صفحة **حركة الخام** وسجل حركة واردة تجريبية")
                    st.markdown("3. تأكد أن الرصيد تحدد تلقائياً في الشاشة الرئيسية")
                else:
                    st.error(f"❌ {message}")

# ==============================================================================
# PAGE 4: التقارير
# ==============================================================================
elif st.session_state.current_page == "reports":
    st.markdown('<div class="section-header">📊 التقارير والإحصائيات</div>', unsafe_allow_html=True)
    
    try:
        df_materials = materials_df(active_only=False)
        df_totals = get_material_totals()
        
        if not df_materials.empty:
            if not df_totals.empty:
                df_materials = df_materials.merge(df_totals, left_on='id', right_on='material_id', how='left')
                df_materials['total_in'] = df_materials['total_in'].fillna(0)
                df_materials['total_out'] = df_materials['total_out'].fillna(0)
            else:
                df_materials['total_in'] = 0
                df_materials['total_out'] = 0
            
            df_materials['remaining'] = df_materials['opening_balance'] + df_materials['total_in'] - df_materials['total_out']
            df_materials['bags_count'] = (df_materials['remaining'] / df_materials['bag_weight']).round(2)
            
            # تقرير شامل
            report_df = df_materials[['code', 'name', 'unit', 'opening_balance', 'total_in', 'total_out', 'remaining', 'bags_count']].copy()
            report_df.columns = ['الكود', 'اسم الخامة', 'الوحدة', 'الافتتاحي', 'الوارد', 'المنصرف', 'المتبقي (كجم)', 'عدد الشكاير']
            
            st.markdown("### 📋 تقرير شامل لمخزن الخامات")
            st.dataframe(report_df, use_container_width=True, hide_index=True)
            
            # إجماليات
            st.markdown("---")
            st.markdown("### 📊 الإجماليات")
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                st.metric("إجمالي الافتتاحي", f"{report_df['الافتتاحي'].sum():,.2f} كجم")
            with c2:
                st.metric("إجمالي الوارد", f"{report_df['الوارد'].sum():,.2f} كجم")
            with c3:
                st.metric("إجمالي المنصرف", f"{report_df['المنصرف'].sum():,.2f} كجم")
            with c4:
                st.metric("إجمالي المتبقي", f"{report_df['المتبقي (كجم)'].sum():,.2f} كجم")
            
            # تصدير التقرير
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                report_df.to_excel(writer, index=False, sheet_name="تقرير المخزن")
            
            st.download_button(
                label="📥 تصدير التقرير إلى Excel",
                data=buf.getvalue(),
                file_name=f"rofa_report_{date.today().strftime('%Y%m%d')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )
        else:
            st.info("📭 لا توجد بيانات للتقرير")
    except Exception as e:
        st.error(f"خطأ: {str(e)}")

# Footer
st.markdown("---")
st.markdown("""
<div style='text-align: center; color: #64748b; padding: 20px; font-size: 14px;'>
    <strong>نظام ROFA لمخزن الخامات</strong> | إدارة خامات البلاستيك والأنواع المختلفة | © 2024
</div>
""", unsafe_allow_html=True)
