"""Rofa Warehouse - قاعدة البيانات وقواعد العمل (بدون أي كود واجهة)."""
import io
import os
import sqlite3
import tempfile
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

DB_FILE = os.environ.get("ROFA_DB", "rofa_warehouse_v2.db")
LEGACY_DB = "rofa_warehouse.db"  # قاعدة النسخة الأولى (يتم ترحيلها تلقائياً)

DOC_TYPES = ["IN", "OUT", "RETURN_IN", "RETURN_OUT", "TRANSFER", "ADJUST"]
DOC_PREFIX = {"IN": "IN", "OUT": "OUT", "RETURN_IN": "RIN", "RETURN_OUT": "ROUT",
              "TRANSFER": "TRF", "ADJUST": "ADJ"}
INCREASE = {"IN", "RETURN_IN"}
DECREASE = {"OUT", "RETURN_OUT"}
EPS = 1e-9


class AppError(Exception):
    """خطأ منطقي يعرض للمستخدم برسالة مترجمة (key + params)."""

    def __init__(self, key, **params):
        super().__init__(key)
        self.key = key
        self.params = params


# ---------------------------------------------------------------- الاتصال
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
    """ترحيل بيانات النسخة الأولى (إن وُجدت) إلى الهيكل الجديد مرة واحدة."""
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


# ---------------------------------------------------------------- جداول البيانات الأساسية
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


# ---------------------------------------------------------------- الأرصدة
def stock_rows():
    """رصيد كل صنف في كل مخزن (المستندات المعتمدة فقط)."""
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


# ---------------------------------------------------------------- المستندات
def next_doc_no(conn, doc_type):
    year = date.today().year
    n = conn.execute("SELECT COUNT(*) FROM documents WHERE type=?", (doc_type,)).fetchone()[0] + 1
    while True:
        no = f"{DOC_PREFIX[doc_type]}-{year}-{n:04d}"
        if not conn.execute("SELECT 1 FROM documents WHERE doc_no=?", (no,)).fetchone():
            return no
        n += 1


def peek_next_doc_no(doc_type):
    conn = connect()
    try:
        return next_doc_no(conn, doc_type)
    finally:
        conn.close()


def _num(v, default=0.0):
    try:
        if v is None or pd.isna(v):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _txt(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    v = str(v).strip()
    return v or None


def _item_code(conn, item_id):
    r = conn.execute("SELECT code FROM items WHERE id=?", (item_id,)).fetchone()
    return r[0] if r else str(item_id)


def create_document(doc_type, doc_date, warehouse_id, lines, to_warehouse_id=None,
                    partner_id=None, reference="", notes="", doc_no=None):
    """ينشئ مستنداً (وارد/صادر/مرتجع/تحويل) كعملية واحدة متكاملة (all-or-nothing)."""
    if doc_type not in DOC_TYPES or doc_type == "ADJUST":
        raise AppError("err_bad_type")
    clean = []
    for ln in lines:
        iid = ln.get("item_id")
        if iid is None or (isinstance(iid, float) and pd.isna(iid)):
            continue
        qty = _num(ln.get("qty"))
        if qty <= 0:
            raise AppError("err_qty_positive")
        clean.append({"item_id": int(iid), "qty": qty, "unit_cost": _num(ln.get("unit_cost")),
                      "batch_no": _txt(ln.get("batch_no")), "expiry": _txt(ln.get("expiry"))})
    if not clean:
        raise AppError("err_no_lines")
    if doc_type == "TRANSFER":
        if not to_warehouse_id or int(to_warehouse_id) == int(warehouse_id):
            raise AppError("err_transfer_wh")

    conn = connect()
    try:
        # منع الرصيد السالب
        if doc_type in DECREASE or doc_type == "TRANSFER":
            need = {}
            for ln in clean:
                need[ln["item_id"]] = need.get(ln["item_id"], 0) + ln["qty"]
            for iid, n in need.items():
                av = available(conn, iid, warehouse_id)
                if av + EPS < n:
                    raise AppError("err_insufficient", item=_item_code(conn, iid), avail=av, need=n)

        no = (doc_no or "").strip() or next_doc_no(conn, doc_type)
        if conn.execute("SELECT 1 FROM documents WHERE doc_no=?", (no,)).fetchone():
            raise AppError("err_doc_exists", doc_no=no)

        cur = conn.execute(
            """INSERT INTO documents(doc_no, type, date, warehouse_id, to_warehouse_id, partner_id,
                                     reference, notes, status, created_at)
               VALUES (?,?,?,?,?,?,?,?, 'Approved', ?)""",
            (no, doc_type, str(doc_date), int(warehouse_id),
             int(to_warehouse_id) if doc_type == "TRANSFER" else None,
             int(partner_id) if partner_id else None, _txt(reference), _txt(notes),
             datetime.now().isoformat(timespec="seconds")))
        doc_id = cur.lastrowid

        for ln in clean:
            iid, qty, uc = ln["item_id"], ln["qty"], ln["unit_cost"]
            row = conn.execute("SELECT cost FROM items WHERE id=?", (iid,)).fetchone()
            if row is None:
                raise AppError("err_item_missing")
            cost = row[0] or 0.0
            if doc_type == "IN" and uc > 0:
                # متوسط التكلفة المرجّح
                base = max(total_qty(conn, iid), 0.0)
                new_cost = (base * cost + qty * uc) / (base + qty)
                conn.execute("UPDATE items SET cost=? WHERE id=?", (round(new_cost, 4), iid))
            elif uc <= 0:
                uc = cost
            conn.execute(
                "INSERT INTO document_lines(document_id, item_id, qty, unit_cost, batch_no, expiry) VALUES (?,?,?,?,?,?)",
                (doc_id, iid, qty, uc, ln["batch_no"], ln["expiry"]))
            if doc_type in INCREASE:
                legs = [(warehouse_id, qty, 0)]
            elif doc_type in DECREASE:
                legs = [(warehouse_id, 0, qty)]
            else:  # TRANSFER
                legs = [(warehouse_id, 0, qty), (to_warehouse_id, qty, 0)]
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
    """تسوية جرد: counts = [(item_id, counted_qty)]. يحسب الفرق لحظة الاعتماد."""
    doc_date = str(doc_date or date.today())
    conn = connect()
    try:
        changes = []
        for iid, counted in counts:
            book = available(conn, int(iid), warehouse_id)
            diff = round(float(counted) - book, 6)
            if abs(diff) > EPS:
                changes.append((int(iid), diff))
        if not changes:
            return None, 0
        no = next_doc_no(conn, "ADJUST")
        cur = conn.execute(
            """INSERT INTO documents(doc_no, type, date, warehouse_id, reference, notes, status, created_at)
               VALUES (?, 'ADJUST', ?, ?, 'Stock count', ?, 'Approved', ?)""",
            (no, doc_date, int(warehouse_id), _txt(notes), datetime.now().isoformat(timespec="seconds")))
        doc_id = cur.lastrowid
        for iid, diff in changes:
            cost = conn.execute("SELECT cost FROM items WHERE id=?", (iid,)).fetchone()[0] or 0.0
            conn.execute("INSERT INTO document_lines(document_id, item_id, qty, unit_cost) VALUES (?,?,?,?)",
                         (doc_id, iid, diff, cost))
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


def cancel_document(doc_id):
    """إلغاء مستند (لا يُحذف - يبقى في السجل بحالة ملغي) بشرط ألا يسبّب رصيداً سالباً."""
    conn = connect()
    try:
        d = conn.execute("SELECT status FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not d:
            raise AppError("err_doc_missing")
        if d[0] != "Approved":
            raise AppError("err_already_cancelled")
        rows = conn.execute(
            """SELECT item_id, warehouse_id, SUM(in_qty - out_qty) FROM ledger
               WHERE document_id=? GROUP BY item_id, warehouse_id""", (doc_id,)).fetchall()
        for iid, wid, net in rows:
            if net > EPS:
                av = available(conn, iid, wid)
                if av + EPS < net:
                    raise AppError("err_cancel_insufficient", item=_item_code(conn, iid), avail=av, need=net)
        conn.execute("UPDATE documents SET status='Cancelled', cancelled_at=? WHERE id=?",
                     (datetime.now().isoformat(timespec="seconds"), doc_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_document_meta(doc_id, reference, notes):
    conn = connect()
    try:
        conn.execute("UPDATE documents SET reference=?, notes=? WHERE id=?",
                     (_txt(reference), _txt(notes), doc_id))
        conn.commit()
    finally:
        conn.close()


def documents_df():
    return q("""
        SELECT d.id, d.doc_no, d.type, d.date, d.status, d.reference, d.notes,
               w.name AS warehouse, w2.name AS to_warehouse, p.name AS partner,
               (SELECT COUNT(*) FROM document_lines x WHERE x.document_id = d.id) AS lines,
               (SELECT COALESCE(SUM(ABS(x.qty)), 0) FROM document_lines x WHERE x.document_id = d.id) AS total_qty,
               (SELECT COALESCE(SUM(ABS(x.qty) * x.unit_cost), 0) FROM document_lines x WHERE x.document_id = d.id) AS total_value
        FROM documents d
        JOIN warehouses w ON w.id = d.warehouse_id
        LEFT JOIN warehouses w2 ON w2.id = d.to_warehouse_id
        LEFT JOIN partners p ON p.id = d.partner_id
        ORDER BY d.date DESC, d.id DESC""")


def document_lines_df(doc_id):
    return q("""SELECT i.code, i.name, i.unit, x.qty, x.unit_cost, x.qty * x.unit_cost AS value,
                       x.batch_no, x.expiry
                FROM document_lines x JOIN items i ON i.id = x.item_id
                WHERE x.document_id = ? ORDER BY x.id""", (doc_id,))


# ---------------------------------------------------------------- كارت الحركة والتقارير
def ledger_df(include_cancelled=False):
    df = q("""SELECT l.id AS ledger_id, l.date, d.doc_no, d.type, d.status, d.reference,
                     l.warehouse_id, w.name AS warehouse, l.item_id, i.code, i.name AS item, i.unit,
                     l.in_qty, l.out_qty, p.name AS partner
              FROM ledger l
              JOIN documents d ON d.id = l.document_id
              JOIN items i ON i.id = l.item_id
              JOIN warehouses w ON w.id = l.warehouse_id
              LEFT JOIN partners p ON p.id = d.partner_id
              ORDER BY l.date, l.id""")
    if df.empty:
        df["balance"] = pd.Series(dtype=float)
        return df
    net = np.where(df["status"] == "Approved", df["in_qty"] - df["out_qty"], 0.0)
    df["balance"] = pd.Series(net, index=df.index).groupby([df["item_id"], df["warehouse_id"]]).cumsum().round(6)
    if not include_cancelled:
        df = df[df["status"] == "Approved"]
    return df


def movement_summary(d_from, d_to, warehouse_id=None):
    led = ledger_df()
    items = items_df()
    if warehouse_id is not None and not led.empty:
        led = led[led["warehouse_id"] == warehouse_id]
    res = items[["id", "code", "name", "category", "unit", "cost"]].copy()
    if led.empty:
        res["opening"] = res["in_qty"] = res["out_qty"] = 0.0
    else:
        led = led.assign(net=led["in_qty"] - led["out_qty"])
        before = led[led["date"] < d_from].groupby("item_id")["net"].sum()
        within = led[(led["date"] >= d_from) & (led["date"] <= d_to)]
        res["opening"] = res["id"].map(before).fillna(0.0)
        res["in_qty"] = res["id"].map(within.groupby("item_id")["in_qty"].sum()).fillna(0.0)
        res["out_qty"] = res["id"].map(within.groupby("item_id")["out_qty"].sum()).fillna(0.0)
    res["closing"] = res["opening"] + res["in_qty"] - res["out_qty"]
    res["value"] = res["closing"] * res["cost"]
    return res.drop(columns=["id"])


def slow_moving(days, warehouse_id=None):
    ov = stock_overview(warehouse_id)
    ov = ov[ov["qty"] > EPS].copy()
    led = ledger_df()
    if not led.empty:
        led = led[(led["out_qty"] > 0) & (led["type"].isin(["OUT", "RETURN_OUT"]))]
        if warehouse_id is not None:
            led = led[led["warehouse_id"] == warehouse_id]
    last = led.groupby("item_id")["date"].max() if not led.empty else pd.Series(dtype=object)
    ov["last_out"] = ov["id"].map(last)
    today = date.today()
    ov["days"] = pd.to_numeric(
        ov["last_out"].map(lambda d: (today - date.fromisoformat(d)).days if isinstance(d, str) else None),
        errors="coerce")
    return ov[ov["days"].isna() | (ov["days"] >= days)]


# ---------------------------------------------------------------- تعديل الجداول مباشرة (Quick edit)
GRID = {
    "items": {
        "cols": ["code", "name", "category", "unit", "min_stock", "cost", "price", "active", "notes"],
        "required": ["code", "name", "unit"], "label": "code",
        "defaults": {"unit": "Pcs", "min_stock": 0, "cost": 0, "price": 0, "active": 1},
        "usage": "SELECT (SELECT COUNT(*) FROM ledger WHERE item_id=:id) + "
                 "(SELECT COUNT(*) FROM document_lines WHERE item_id=:id)"},
    "warehouses": {
        "cols": ["code", "name", "location", "active"],
        "required": ["code", "name"], "label": "name", "defaults": {"active": 1},
        "usage": "SELECT (SELECT COUNT(*) FROM documents WHERE warehouse_id=:id OR to_warehouse_id=:id) + "
                 "(SELECT COUNT(*) FROM ledger WHERE warehouse_id=:id)"},
    "partners": {
        "cols": ["name", "type", "phone", "notes"],
        "required": ["name"], "label": "name", "defaults": {"type": "SUPPLIER"},
        "usage": "SELECT COUNT(*) FROM documents WHERE partner_id=:id"},
    "categories": {
        "cols": ["name"], "required": ["name"], "label": "name", "defaults": {},
        "usage": "SELECT COUNT(*) FROM items WHERE category=(SELECT name FROM categories WHERE id=:id)"},
}


def _clean(v):
    if v is None:
        return None
    if isinstance(v, (bool, np.bool_)):
        return int(v)
    if isinstance(v, np.generic):
        v = v.item()
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def _blank(v):
    return v is None or v == 0 and isinstance(v, bool) or v == ""


def apply_grid_changes(table, orig, edited):
    """يطبّق تعديلات الجدول (إضافة/تعديل/حذف). كل شيء أو لا شيء.
    يعيد (counts, errors) حيث errors قائمة (key, params)."""
    cfg = GRID[table]
    cols = cfg["cols"]
    errors, added, updated, deleted = [], 0, 0, 0
    conn = connect()
    try:
        orig_map = {}
        for rec in orig.to_dict("records"):
            orig_map[int(rec["id"])] = {c: _clean(rec.get(c)) for c in cols}
        seen = set()
        for rec in edited.to_dict("records"):
            rid = rec.get("id")
            new = {c: _clean(rec.get(c)) for c in cols}
            label = new.get(cfg["label"]) or ""
            if rid is None or pd.isna(rid):  # صف جديد
                if all(v is None or v == 0 for v in new.values()):
                    continue
                for c, dv in cfg["defaults"].items():
                    if new[c] is None:
                        new[c] = dv
                if any(new[c] is None for c in cfg["required"]):
                    errors.append(("err_required", {"row": label or "?"}))
                    continue
                try:
                    conn.execute(f"INSERT INTO {table}({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                 [new[c] for c in cols])
                    added += 1
                except sqlite3.IntegrityError:
                    errors.append(("err_duplicate", {"row": label}))
            else:
                rid = int(rid)
                seen.add(rid)
                old = orig_map.get(rid)
                if old is None or old == new:
                    continue
                for c, dv in cfg["defaults"].items():
                    if new[c] is None:
                        new[c] = dv
                if any(new[c] is None for c in cfg["required"]):
                    errors.append(("err_required", {"row": label or str(rid)}))
                    continue
                try:
                    conn.execute(f"UPDATE {table} SET {','.join(c + '=?' for c in cols)} WHERE id=?",
                                 [new[c] for c in cols] + [rid])
                    if table == "categories" and old["name"] != new["name"]:
                        conn.execute("UPDATE items SET category=? WHERE category=?", (new["name"], old["name"]))
                    updated += 1
                except sqlite3.IntegrityError:
                    errors.append(("err_duplicate", {"row": label}))
        for rid in set(orig_map) - seen:  # صفوف محذوفة
            used = conn.execute(cfg["usage"], {"id": rid}).fetchone()[0]
            label = orig_map[rid].get(cfg["label"]) or str(rid)
            if used:
                errors.append(("err_in_use", {"row": label}))
                continue
            conn.execute(f"DELETE FROM {table} WHERE id=?", (rid,))
            deleted += 1
        if errors:
            conn.rollback()
            return {"added": 0, "updated": 0, "deleted": 0}, errors
        conn.commit()
        return {"added": added, "updated": updated, "deleted": deleted}, []
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- تصدير / نسخ احتياطي
def export_excel(sheets):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for name, df in sheets.items():
            df.to_excel(w, index=False, sheet_name=str(name)[:31])
    return buf.getvalue()


def backup_bytes():
    src = connect()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    try:
        dst = sqlite3.connect(tmp.name)
        src.backup(dst)
        dst.close()
        with open(tmp.name, "rb") as f:
            return f.read()
    finally:
        src.close()
        os.unlink(tmp.name)


def restore_db(data):
    if not data.startswith(b"SQLite format 3"):
        raise AppError("err_restore")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.write(data)
    tmp.close()
    try:
        chk = sqlite3.connect(tmp.name)
        names = {r[0] for r in chk.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        chk.close()
        if not {"items", "documents", "ledger"} <= names:
            raise AppError("err_restore")
        with open(tmp.name, "rb") as f:
            payload = f.read()
        with open(DB_FILE, "wb") as f:
            f.write(payload)
    finally:
        os.unlink(tmp.name)
    init_db()


# ---------------------------------------------------------------- بيانات تجريبية
def load_demo():
    conn = connect()
    try:
        if conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] > 0:
            raise AppError("err_demo_nonempty")
        for c in ["مواد خام", "منتجات تامة", "مستلزمات تعبئة"]:
            conn.execute("INSERT INTO categories(name) VALUES (?)", (c,))
        conn.execute("INSERT OR IGNORE INTO warehouses(code, name, location) VALUES ('BR1', 'مخزن الفرع', 'الفرع الثاني')")
        for p in [("شركة النور للتوريدات", "SUPPLIER", "01000000001"), ("مصنع الأمل", "SUPPLIER", "01000000002"),
                  ("عميل - الشروق", "CUSTOMER", "01000000003"), ("عميل - الندى", "CUSTOMER", "01000000004")]:
            conn.execute("INSERT INTO partners(name, type, phone) VALUES (?,?,?)", p)
        demo = [("RM-001", "دقيق فاخر", "مواد خام", "كجم", 200, 18, 0), ("RM-002", "سكر", "مواد خام", "كجم", 150, 24, 0),
                ("RM-003", "زيت عباد الشمس", "مواد خام", "لتر", 100, 55, 0),
                ("FG-001", "بسكويت سادة", "منتجات تامة", "علبة", 80, 12, 20),
                ("FG-002", "كعك بالتمر", "منتجات تامة", "علبة", 60, 15, 25),
                ("PK-001", "كرتونة 12 قطعة", "مستلزمات تعبئة", "قطعة", 300, 4, 0),
                ("PK-002", "شريط لاصق", "مستلزمات تعبئة", "رول", 40, 18, 0),
                ("PK-003", "أكياس تغليف", "مستلزمات تعبئة", "كيلو", 50, 70, 0)]
        for r in demo:
            conn.execute("INSERT INTO items(code, name, category, unit, min_stock, cost, price) VALUES (?,?,?,?,?,?,?)", r)
        conn.commit()
    finally:
        conn.close()

    ids = {r.code: r.id for r in items_df().itertuples()}
    whs = warehouses_df()
    main, br = int(whs.id.iloc[0]), int(whs.id.iloc[-1])
    parts = partners_df()
    sup = int(parts[parts.type == "SUPPLIER"].id.iloc[0])
    cus = int(parts[parts.type == "CUSTOMER"].id.iloc[0])
    today = date.today()
    d = lambda n: (today - timedelta(days=n)).isoformat()
    L = lambda code, qty, uc=0: {"item_id": ids[code], "qty": qty, "unit_cost": uc}
    create_document("IN", d(28), main, [L("RM-001", 800, 18), L("RM-002", 500, 24), L("RM-003", 300, 55),
                                         L("PK-001", 900, 4), L("PK-002", 120, 18), L("PK-003", 90, 70)],
                    partner_id=sup, reference="PO-1001")
    create_document("IN", d(22), main, [L("FG-001", 250, 12), L("FG-002", 140, 15)], reference="WO-2001")
    create_document("OUT", d(18), main, [L("RM-001", 620), L("RM-002", 380), L("RM-003", 215)], reference="WO-2001")
    create_document("OUT", d(14), main, [L("FG-001", 120), L("FG-002", 70)], partner_id=cus, reference="INV-501")
    create_document("TRANSFER", d(10), main, [L("FG-001", 60), L("FG-002", 25)], to_warehouse_id=br)
    create_document("OUT", d(6), main, [L("PK-001", 820), L("PK-003", 48)], reference="WO-2002")
    create_document("OUT", d(3), br, [L("FG-002", 20)], partner_id=cus, reference="INV-502")
    create_document("RETURN_IN", d(1), main, [L("FG-001", 5)], partner_id=cus, reference="RET-9")
