import json
import os
import re
import io
import hashlib
import unicodedata
import csv
import zipfile
from xml.etree import ElementTree as ET
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import mysql.connector
from argon2 import PasswordHasher, exceptions as argon2_exceptions
from flask import Flask, Response, abort, flash, redirect, render_template, request, send_file, session, url_for
from flask_wtf.csrf import CSRFProtect
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

from invoice_ai import extract_invoice

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("FLASK_SECRET_KEY") or os.urandom(32),
    MAX_CONTENT_LENGTH=11 * 1024 * 1024,
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
)
csrf = CSRFProtect(app)
password_hasher = PasswordHasher()
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "pdf"}


def db_connection():
    return mysql.connector.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "root"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DATABASE", "facturaia"),
        charset="utf8mb4",
        collation="utf8mb4_unicode_ci",
    )


def query(sql, params=(), one=False, commit=False):
    conn = db_connection()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, params)
        result = None
        if cur.with_rows:
            result = cur.fetchone() if one else cur.fetchall()
        if commit:
            conn.commit()
            return cur.lastrowid
        return result
    finally:
        conn.close()


def current_user():
    if "user_id" not in session:
        return None
    return query(
        "SELECT u.id_usuario AS id, u.id_negocio AS business_id, u.nombre AS name, u.correo AS email, "
        "u.celular AS phone, u.rol AS role, n.nombre AS business_name "
        "FROM usuarios u JOIN negocios n ON n.id_negocio=u.id_negocio "
        "WHERE u.id_usuario=%s AND u.activo=1",
        (session["user_id"],), one=True,
    )


@app.before_request
def require_authentication():
    if request.endpoint in {"static", "login", "register", "health"}:
        return
    if "user_id" not in session:
        return redirect(url_for("login", next=request.path))
    if current_user() is None:
        session.clear()
        return redirect(url_for("login"))
    session.permanent = True


@app.context_processor
def inject_user():
    return {"usuario": current_user()}


def admin_required():
    user = current_user()
    if not user or user["role"] != "Administrador":
        abort(403)
    return user


def password_is_valid(value):
    return (len(value) >= 10 and re.search(r"[A-Z]", value) and re.search(r"[a-z]", value)
            and re.search(r"\d", value) and re.search(r"[^A-Za-z0-9]", value))


def verify_password(encoded, supplied):
    try:
        return password_hasher.verify(encoded, supplied)
    except (argon2_exceptions.VerificationError, argon2_exceptions.InvalidHashError):
        return False


def normalized_key(value):
    plain = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Za-z0-9]", "", plain).upper()


def same_supplier(first_nit, first_name, second_nit, second_name):
    first_nit = normalized_key(first_nit)
    second_nit = normalized_key(second_nit)
    if first_nit and second_nit:
        return first_nit == second_nit
    first_name = normalized_key(first_name)
    return bool(first_name) and first_name == normalized_key(second_name)


def reverse_invoice_stock(cur, invoice_id):
    cur.execute("SELECT m.id_producto,m.cambio_cantidad FROM movimientos_inventario m JOIN detalle_factura d ON d.id_detalle=m.id_detalle_factura WHERE d.id_factura=%s FOR UPDATE", (invoice_id,))
    for product_id, quantity in cur.fetchall():
        cur.execute("UPDATE productos SET existencia=existencia-%s WHERE id_producto=%s", (quantity, product_id))
    cur.execute("DELETE m FROM movimientos_inventario m JOIN detalle_factura d ON d.id_detalle=m.id_detalle_factura WHERE d.id_factura=%s", (invoice_id,))
    cur.execute("DELETE FROM detalle_factura WHERE id_factura=%s", (invoice_id,))


def draft_path(draft_id):
    if not re.fullmatch(r"[a-f0-9]{32}", draft_id or ""):
        raise ValueError("Borrador de factura no válido.")
    folder = os.path.join(app.instance_path, "drafts")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, draft_id + ".bin")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.errorhandler(413)
def upload_too_large(_error):
    flash("El archivo supera el límite de 10 MB. Selecciona uno más pequeño e inténtalo de nuevo.", "danger")
    return redirect(url_for("subir_factura"))


@app.route("/", methods=["GET", "POST"])
@app.route("/registro", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        business = request.form.get("business", "").strip()
        nit = request.form.get("nit", "").strip()
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not request.form.get("privacy"):
            flash("Debes aceptar la política de tratamiento de datos.", "danger")
        elif not all((business, nit, name, email, phone)):
            flash("Completa todos los campos requeridos.", "danger")
        elif password != confirm_password:
            flash("Las contraseñas no coinciden.", "danger")
        elif not password_is_valid(password):
            flash("La contraseña debe cumplir todos los requisitos indicados.", "danger")
        elif query("SELECT id_usuario FROM usuarios WHERE correo=%s", (email,), one=True):
            flash("Ese correo ya está registrado.", "danger")
        else:
            conn = db_connection()
            try:
                cur = conn.cursor()
                cur.execute("INSERT INTO negocios(nombre, nit_cedula) VALUES(%s,%s)", (business, nit))
                business_id = cur.lastrowid
                cur.execute("INSERT INTO usuarios(id_negocio,nombre,correo,celular,clave_hash,rol) VALUES(%s,%s,%s,%s,%s,'Administrador')",
                            (business_id, name, email, phone, password_hasher.hash(password)))
                conn.commit()
            except mysql.connector.IntegrityError:
                conn.rollback()
                flash("El correo o la identificación del negocio ya están registrados.", "danger")
                return render_template("register.html")
            finally:
                conn.close()
            flash("Negocio registrado. Ya puedes iniciar sesión.", "success")
            return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    email = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        try:
            user = query("SELECT id_usuario AS id,id_negocio AS business_id,clave_hash AS password_hash,activo AS active,intentos_fallidos AS failed_attempts,bloqueo_hasta AS locked_until FROM usuarios WHERE correo=%s", (email,), one=True)
            now = datetime.now()
            if user and user["locked_until"] and user["locked_until"] > now:
                flash("Acceso temporalmente bloqueado. Intenta más tarde.", "danger")
            elif user and not user["active"]:
                flash("Esta cuenta está desactivada. Solicita al administrador que la active.", "danger")
            elif user and verify_password(user["password_hash"], request.form.get("password", "")):
                query("UPDATE usuarios SET intentos_fallidos=0,bloqueo_hasta=NULL,ultimo_acceso=NOW() WHERE id_usuario=%s", (user["id"],), commit=True)
                session.clear()
                session["user_id"] = user["id"]
                session.permanent = True
                destination = request.args.get("next", "")
                return redirect(destination if destination.startswith("/") and not destination.startswith("//") else url_for("dashboard"))
            else:
                if user:
                    attempts = user["failed_attempts"] + 1
                    locked = now + timedelta(minutes=15) if attempts >= 5 else None
                    query("UPDATE usuarios SET intentos_fallidos=%s,bloqueo_hasta=%s WHERE id_usuario=%s", (attempts, locked, user["id"]), commit=True)
                flash("Correo o contraseña incorrectos.", "danger")
        except mysql.connector.Error:
            app.logger.exception("No se pudo completar el inicio de sesión por un error de base de datos")
            flash("No se pudo validar el acceso en la base de datos. Comprueba que MySQL esté iniciado, que exista la base facturaia y que la tabla usuarios tenga las columnas definidas en schema.sql.", "danger")
    return render_template("login.html", email=email)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/dashboard")
def dashboard():
    user = current_user()
    biz = user["business_id"]
    period = request.args.get("period", "12m")
    if period not in {"12m", "year", "24m"}:
        period = "12m"
    today = date.today()
    end_date = today + timedelta(days=1)
    current_month = today.replace(day=1)
    months = 12 if period == "12m" else 24 if period == "24m" else today.month
    month_names = ("Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic")

    def month_offset(value, offset):
        month_index = value.year * 12 + value.month - 1 + offset
        return date(month_index // 12, month_index % 12 + 1, 1)

    start_date = date(today.year, 1, 1) if period == "year" else month_offset(current_month, -(months - 1))
    previous_start = month_offset(start_date, -months) if period != "year" else date(today.year - 1, 1, 1)
    previous_end = start_date if period != "year" else end_date.replace(year=today.year - 1)
    stats = query("SELECT COUNT(*) AS invoices, COALESCE(SUM(total),0) AS spent FROM facturas WHERE id_negocio=%s AND estado='Confirmada' AND fecha_factura >= %s AND fecha_factura < %s", (biz, start_date, end_date), one=True)
    previous_stats = query("SELECT COUNT(*) AS invoices, COALESCE(SUM(total),0) AS spent FROM facturas WHERE id_negocio=%s AND estado='Confirmada' AND fecha_factura >= %s AND fecha_factura < %s", (biz, previous_start, previous_end), one=True)
    products = query("SELECT COUNT(*) AS n FROM productos WHERE id_negocio=%s", (biz,), one=True)["n"]
    suppliers = query("SELECT COUNT(*) AS n FROM proveedores WHERE id_negocio=%s", (biz,), one=True)["n"]
    recent = query("SELECT f.id_factura AS id,f.numero_factura AS invoice_number,f.fecha_factura AS invoice_date,f.total,f.estado AS status,p.nombre AS supplier_name,(SELECT COUNT(*) FROM detalle_factura d WHERE d.id_factura=f.id_factura) AS item_count FROM facturas f LEFT JOIN proveedores p ON p.id_proveedor=f.id_proveedor WHERE f.id_negocio=%s ORDER BY f.fecha_creacion DESC LIMIT 6", (biz,))
    monthly_rows = query("SELECT DATE_FORMAT(fecha_factura,'%%Y-%%m') AS month_key,COALESCE(SUM(total),0) AS total FROM facturas WHERE id_negocio=%s AND estado='Confirmada' AND fecha_factura >= %s AND fecha_factura < %s GROUP BY DATE_FORMAT(fecha_factura,'%%Y-%%m') ORDER BY month_key", (biz, start_date, end_date))
    totals_by_month = {row["month_key"]: float(row["total"] or 0) for row in monthly_rows}
    month_series = []
    for offset in range(months):
        month_date = month_offset(start_date, offset)
        month_key = month_date.strftime("%Y-%m")
        month_series.append({"label": f"{month_names[month_date.month - 1]} {str(month_date.year)[-2:]}", "value": totals_by_month.get(month_key, 0)})
    top_products = query("SELECT d.descripcion AS name,COALESCE(SUM(d.cantidad),0) AS quantity,COALESCE(SUM(d.total_linea),0) AS total FROM detalle_factura d JOIN facturas f ON f.id_factura=d.id_factura WHERE f.id_negocio=%s AND f.estado='Confirmada' AND f.fecha_factura >= %s AND f.fecha_factura < %s GROUP BY d.descripcion ORDER BY quantity DESC,total DESC LIMIT 5", (biz, start_date, end_date))
    top_suppliers = query("SELECT p.nombre AS name,COALESCE(SUM(f.total),0) AS total FROM facturas f JOIN proveedores p ON p.id_proveedor=f.id_proveedor WHERE f.id_negocio=%s AND f.estado='Confirmada' AND f.fecha_factura >= %s AND f.fecha_factura < %s GROUP BY p.id_proveedor,p.nombre ORDER BY total DESC LIMIT 5", (biz, start_date, end_date))
    return render_template("dashboard.html", stats=stats, previous_stats=previous_stats, products=products, suppliers=suppliers,
                           ultimas_facturas=recent, month_series=month_series, top_products=top_products,
                           top_suppliers=top_suppliers, period=period, period_start=start_date, period_end=today)


@app.route("/facturas")
def facturas():
    user = current_user()
    term = request.args.get("q", "").strip()
    recent_id = request.args.get("reciente", type=int)
    invoices = query("SELECT f.id_factura AS id,f.numero_factura AS invoice_number,f.fecha_factura AS invoice_date,f.subtotal,f.iva AS tax,f.total,f.estado AS status,p.nombre AS supplier_name FROM facturas f LEFT JOIN proveedores p ON p.id_proveedor=f.id_proveedor WHERE f.id_negocio=%s AND (%s='' OR f.numero_factura LIKE %s OR p.nombre LIKE %s) ORDER BY f.fecha_factura DESC,f.id_factura DESC",
                     (user["business_id"], term, f"%{term}%", f"%{term}%"))
    if recent_id:
        invoices.sort(key=lambda invoice: invoice["id"] != recent_id)
    return render_template("facturas.html", facturas=invoices, search=term, recent_id=recent_id)


@app.route("/subir-factura", methods=["GET", "POST"])
def subir_factura():
    user = current_user()
    if request.method == "GET" and request.args.get("discard") == "1":
        old_draft = session.pop("invoice_draft", None)
        if old_draft:
            try:
                os.remove(draft_path(old_draft["id"]))
            except FileNotFoundError:
                pass
        return redirect(url_for("subir_factura"))
    if request.method == "POST" and request.form.get("action") == "extract":
        uploaded = request.files.get("invoice_file")
        if not uploaded or not uploaded.filename:
            flash("Selecciona una imagen JPG/PNG o un archivo PDF.", "danger")
            return redirect(url_for("subir_factura"))
        filename = secure_filename(uploaded.filename)
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        content = uploaded.read()
        if extension not in ALLOWED_EXTENSIONS or uploaded.mimetype not in {"image/jpeg", "image/png", "application/pdf", "application/octet-stream"}:
            flash("Formato no válido. Usa JPG, PNG o PDF.", "danger")
        elif not content or len(content) > 10 * 1024 * 1024:
            flash("El archivo está vacío o supera 10 MB.", "danger")
        elif not ((extension in {"jpg", "jpeg"} and content.startswith(b"\xff\xd8\xff"))
                  or (extension == "png" and content.startswith(b"\x89PNG\r\n\x1a\n"))
                  or (extension == "pdf" and content.startswith(b"%PDF-"))):
            flash("El contenido del archivo no coincide con su formato.", "danger")
        elif not os.environ.get("GEMINI_API_KEY"):
            flash("Falta configurar GEMINI_API_KEY para activar la extracción gratuita con Gemini.", "danger")
        else:
            try:
                extracted = extract_invoice(content, extension, filename)
                draft_id = os.urandom(16).hex()
                with open(draft_path(draft_id), "wb") as draft_file:
                    draft_file.write(content)
                previous = session.get("invoice_draft")
                if previous:
                    try:
                        os.remove(draft_path(previous["id"]))
                    except FileNotFoundError:
                        pass
                session["invoice_draft"] = {"id": draft_id, "filename": filename, "extension": extension,
                                             "mime": {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "pdf": "application/pdf"}[extension], "data": extracted}
                return render_template("subir_factura.html", draft=extracted, filename=filename, extension=extension)
            except (RuntimeError, ValueError) as exc:
                flash(str(exc), "danger")
            except Exception:
                app.logger.exception("Invoice extraction failed")
                flash("No se pudo leer la factura. Verifica la calidad del archivo e inténtalo de nuevo.", "danger")
        return redirect(url_for("subir_factura"))
    if request.method == "POST" and request.form.get("action") == "save":
        draft = session.get("invoice_draft")
        if not draft:
            flash("La revisión expiró. Vuelve a cargar la factura.", "danger")
            return redirect(url_for("subir_factura"))
        try:
            data = {"supplier": request.form.get("supplier", "").strip(), "nit": request.form.get("nit", "").strip(),
                    "invoice_number": request.form.get("invoice_number", "").strip(), "invoice_date": request.form.get("invoice_date", ""),
                    "resolution": request.form.get("resolution", "").strip(), "subtotal": Decimal(request.form.get("subtotal", "0") or "0"),
                    "tax": Decimal(request.form.get("tax", "0") or "0"), "total": Decimal(request.form.get("total", "0") or "0"),
                    "items": []}
            for idx, name in enumerate(request.form.getlist("item_name")):
                if name.strip():
                    item = {"name": name.strip(), "quantity": Decimal(request.form.getlist("item_quantity")[idx]),
                            "unit_price": Decimal(request.form.getlist("item_price")[idx]), "discount": Decimal(request.form.getlist("item_discount")[idx] or "0"),
                            "tax": Decimal(request.form.getlist("item_tax")[idx] or "0")}
                    if min(item["quantity"], item["unit_price"], item["discount"], item["tax"]) < 0:
                        raise ValueError("Las cantidades y valores de cada producto deben ser cero o mayores.")
                    data["items"].append(item)
            if not all((data["supplier"], data["invoice_number"], data["invoice_date"])):
                raise ValueError("Completa proveedor, número y fecha antes de guardar.")
            if data["total"] < 0 or data["subtotal"] < 0 or data["tax"] < 0:
                raise ValueError("Los valores de la factura no pueden ser negativos.")
            if not request.form.get("reviewed"):
                raise ValueError("Confirma que revisaste los campos extraídos antes de guardar.")
            header_difference = abs(data["subtotal"] + data["tax"] - data["total"])
            items_total = sum((item["quantity"] * item["unit_price"] - item["discount"] + item["tax"] for item in data["items"]), Decimal("0"))
            difference = max(header_difference, abs(items_total - data["total"]) if data["items"] else Decimal("0"))
            justification = request.form.get("justification", "").strip()
            if difference > Decimal("1.00") and not request.form.get("confirm_calculation"):
                raise ValueError("Los totales difieren en más de $1. Marca la casilla para confirmar que revisaste la diferencia.")
            if difference > Decimal("1.00") and len(justification) < 8:
                raise ValueError("Escribe una justificación de al menos 8 caracteres para guardar la diferencia.")
            conn = db_connection()
            cur = conn.cursor()
            try:
                cur.execute("SELECT id_negocio FROM negocios WHERE id_negocio=%s FOR UPDATE", (user["business_id"],))
                cur.fetchone()
                with open(draft_path(draft["id"]), "rb") as draft_file:
                    original_content = draft_file.read()
                file_hash = hashlib.sha256(original_content).hexdigest()
                cur.execute("SELECT id_factura FROM facturas WHERE id_negocio=%s AND SHA2(archivo_original,256)=%s LIMIT 1 FOR UPDATE",
                            (user["business_id"], file_hash))
                if cur.fetchone():
                    raise ValueError("Este mismo archivo de factura ya fue registrado. No se permite duplicarlo aunque cambies el número u otros datos.")
                normalized_nit = re.sub(r"[^A-Za-z0-9]", "", data["nit"]).upper()
                if normalized_nit:
                    cur.execute("SELECT id_proveedor FROM proveedores WHERE id_negocio=%s AND REPLACE(REPLACE(REPLACE(UPPER(nit),'.',''),'-',''),' ','')=%s LIMIT 1 FOR UPDATE",
                                (user["business_id"], normalized_nit))
                else:
                    cur.execute("SELECT id_proveedor FROM proveedores WHERE id_negocio=%s AND nit IS NULL AND LOWER(nombre)=LOWER(%s) LIMIT 1 FOR UPDATE",
                                (user["business_id"], data["supplier"]))
                row = cur.fetchone()
                if row:
                    supplier_id = row[0]
                    cur.execute("UPDATE proveedores SET nombre=%s WHERE id_proveedor=%s", (data["supplier"], supplier_id))
                else:
                    if data["nit"]:
                        cur.execute("INSERT INTO proveedores(id_negocio,nombre,nit) VALUES(%s,%s,%s)", (user["business_id"], data["supplier"], data["nit"]))
                        supplier_id = cur.lastrowid
                    else:
                        cur.execute("SELECT id_proveedor FROM proveedores WHERE id_negocio=%s AND nit IS NULL AND LOWER(nombre)=LOWER(%s) LIMIT 1", (user["business_id"], data["supplier"]))
                        row = cur.fetchone()
                        if row:
                            supplier_id = row[0]
                        else:
                            cur.execute("INSERT INTO proveedores(id_negocio,nombre) VALUES(%s,%s)", (user["business_id"], data["supplier"]))
                            supplier_id = cur.lastrowid
                normalized_invoice_number = normalized_key(data["invoice_number"])
                cur.execute("SELECT f.numero_factura,p.nit,p.nombre FROM facturas f JOIN proveedores p ON p.id_proveedor=f.id_proveedor WHERE f.id_negocio=%s",
                            (user["business_id"],))
                for existing_number, existing_nit, existing_name in cur.fetchall():
                    if (normalized_key(existing_number) == normalized_invoice_number
                            and same_supplier(data["nit"], data["supplier"], existing_nit, existing_name)):
                        raise ValueError("Esta factura ya está registrada para el proveedor. No se permiten números duplicados aunque cambien el formato, la fecha o el total.")
                cur.execute("INSERT INTO facturas(id_negocio,id_proveedor,id_usuario_creador,numero_factura,fecha_factura,numero_resolucion,subtotal,iva,total,estado,nombre_archivo,tipo_archivo,archivo_original,confianza_campos) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'Confirmada',%s,%s,%s,%s)",
                            (user["business_id"], supplier_id, user["id"], data["invoice_number"], data["invoice_date"], data["resolution"], data["subtotal"], data["tax"], data["total"], draft["filename"], draft["mime"], original_content, json.dumps(draft["data"].get("confidence", {}))))
                invoice_id = cur.lastrowid
                for item in data["items"]:
                    cur.execute("INSERT INTO detalle_factura(id_factura,descripcion,cantidad,valor_unitario,descuento,iva_linea,total_linea) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                                (invoice_id, item["name"], item["quantity"], item["unit_price"], item["discount"], item["tax"], item["quantity"] * item["unit_price"] - item["discount"] + item["tax"]))
                    item_id = cur.lastrowid
                    cur.execute("SELECT id_producto FROM productos WHERE id_negocio=%s AND nombre=%s FOR UPDATE", (user["business_id"], item["name"]))
                    product = cur.fetchone()
                    if product:
                        product_id = product[0]
                    else:
                        cur.execute("INSERT INTO productos(id_negocio,nombre,existencia) VALUES(%s,%s,0)", (user["business_id"], item["name"]))
                        product_id = cur.lastrowid
                    cur.execute("UPDATE productos SET existencia=existencia+%s WHERE id_producto=%s", (item["quantity"], product_id))
                    cur.execute("INSERT INTO movimientos_inventario(id_negocio,id_producto,id_usuario,id_detalle_factura,cambio_cantidad,motivo) VALUES(%s,%s,%s,%s,%s,'Compra por factura')",
                                (user["business_id"], product_id, user["id"], item_id, item["quantity"]))
                audit_details = {"number": data["invoice_number"], "justification": justification or None}
                cur.execute("INSERT INTO registro_auditoria(id_negocio,id_usuario,tipo_entidad,id_entidad,accion,detalles) VALUES(%s,%s,'factura',%s,'creada',%s)", (user["business_id"], user["id"], invoice_id, json.dumps(audit_details)))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            session.pop("invoice_draft", None)
            try:
                os.remove(draft_path(draft["id"]))
            except FileNotFoundError:
                pass
            flash("Factura guardada y confirmada.", "success")
            return redirect(url_for("facturas", reciente=invoice_id))
        except (InvalidOperation, ValueError) as exc:
            flash(str(exc) or "Revisa los datos numéricos de la factura.", "danger")
            original_data = draft.get("data", {})
            confidence = original_data.get("confidence", {})
            original_items = original_data.get("items", [])
            corrected_items = []
            for index, name in enumerate(request.form.getlist("item_name")):
                if name.strip():
                    corrected_items.append({
                        "name": name,
                        "quantity": request.form.getlist("item_quantity")[index],
                        "unit_price": request.form.getlist("item_price")[index],
                        "discount": request.form.getlist("item_discount")[index],
                        "tax": request.form.getlist("item_tax")[index],
                        "confidence": original_items[index].get("confidence") if index < len(original_items) else None,
                    })
            corrected_data = {
                **original_data,
                "supplier": request.form.get("supplier", ""),
                "nit": request.form.get("nit", ""),
                "invoice_number": request.form.get("invoice_number", ""),
                "invoice_date": request.form.get("invoice_date", ""),
                "resolution": request.form.get("resolution", ""),
                "subtotal": request.form.get("subtotal", ""),
                "tax": request.form.get("tax", ""),
                "total": request.form.get("total", ""),
                "justification": request.form.get("justification", ""),
                "confirm_calculation": request.form.get("confirm_calculation"),
                "confidence": confidence,
                "items": corrected_items,
            }
            return render_template("subir_factura.html", draft=corrected_data, filename=draft.get("filename"), extension=draft.get("extension"))
        except mysql.connector.IntegrityError:
            flash("La factura o uno de sus productos ya existe. Revisa los datos e inténtalo de nuevo.", "danger")
            return render_template("subir_factura.html", draft=draft.get("data"), filename=draft.get("filename"))
        except mysql.connector.Error:
            app.logger.exception("No se pudo guardar la factura en MySQL")
            flash("No se pudo guardar la factura en MySQL. Comprueba que la base facturaia esté actualizada con schema.sql e inténtalo de nuevo.", "danger")
            return render_template("subir_factura.html", draft=draft.get("data"), filename=draft.get("filename"), extension=draft.get("extension"))
    draft = session.get("invoice_draft", {})
    return render_template("subir_factura.html", draft=draft.get("data"), filename=draft.get("filename"), extension=draft.get("extension"))


@app.get("/subir-factura/original")
def invoice_draft_file():
    draft = session.get("invoice_draft")
    if not draft:
        abort(404)
    with open(draft_path(draft["id"]), "rb") as draft_file:
        content = draft_file.read()
    return send_file(io.BytesIO(content), mimetype=draft["mime"], download_name=draft["filename"], as_attachment=False)


@app.get("/facturas/<int:invoice_id>")
def invoice_detail(invoice_id):
    user = current_user()
    invoice = query("SELECT f.id_factura AS id,f.numero_factura AS invoice_number,f.fecha_factura AS invoice_date,f.numero_resolucion AS resolution_number,f.subtotal,f.iva AS tax,f.total,f.estado AS status,p.nombre AS supplier_name,p.nit AS tax_id FROM facturas f JOIN proveedores p ON p.id_proveedor=f.id_proveedor WHERE f.id_factura=%s AND f.id_negocio=%s", (invoice_id, user["business_id"]), one=True)
    if not invoice:
        abort(404)
    invoice["items"] = query("SELECT id_detalle AS id,descripcion AS description,cantidad AS quantity,valor_unitario AS unit_price,descuento AS discount_amount,iva_linea AS tax_amount,total_linea AS line_total FROM detalle_factura WHERE id_factura=%s", (invoice_id,))
    return render_template("invoice_detail.html", invoice=invoice)


@app.route("/facturas/<int:invoice_id>/editar", methods=["GET", "POST"])
def edit_invoice(invoice_id):
    user = admin_required()
    invoice = query("SELECT f.id_factura AS id,f.id_proveedor AS supplier_id,f.numero_factura AS invoice_number,f.fecha_factura AS invoice_date,f.numero_resolucion AS resolution_number,f.subtotal,f.iva AS tax,f.total,f.estado AS status,p.nombre AS supplier_name,p.nit AS tax_id FROM facturas f JOIN proveedores p ON p.id_proveedor=f.id_proveedor WHERE f.id_factura=%s AND f.id_negocio=%s", (invoice_id, user["business_id"]), one=True)
    if not invoice:
        abort(404)
    if invoice["status"] != "Confirmada":
        flash("Solo se pueden editar facturas confirmadas.", "danger")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    if request.method == "GET":
        invoice["items"] = query("SELECT descripcion AS description,cantidad AS quantity,valor_unitario AS unit_price,descuento AS discount_amount,iva_linea AS tax_amount FROM detalle_factura WHERE id_factura=%s", (invoice_id,))
        return render_template("editar_factura.html", invoice=invoice)

    item_names = request.form.getlist("item_name")
    quantities = request.form.getlist("item_quantity")
    prices = request.form.getlist("item_price")
    discounts = request.form.getlist("item_discount")
    taxes = request.form.getlist("item_tax")
    try:
        data = {
            "supplier": request.form.get("supplier", "").strip(),
            "nit": request.form.get("nit", "").strip(),
            "invoice_number": request.form.get("invoice_number", "").strip(),
            "invoice_date": date.fromisoformat(request.form.get("invoice_date", "")),
            "resolution": request.form.get("resolution", "").strip(),
            "subtotal": Decimal(request.form.get("subtotal", "0") or "0"),
            "tax": Decimal(request.form.get("tax", "0") or "0"),
            "total": Decimal(request.form.get("total", "0") or "0"),
            "items": [],
        }
        if not all(len(values) == len(item_names) for values in (quantities, prices, discounts, taxes)):
            raise ValueError("Revisa las líneas de productos e inténtalo de nuevo.")
        for index, name in enumerate(item_names):
            if name.strip():
                item = {"name": name.strip(), "quantity": Decimal(quantities[index]),
                        "unit_price": Decimal(prices[index]), "discount": Decimal(discounts[index] or "0"),
                        "tax": Decimal(taxes[index] or "0")}
                if min(item["quantity"], item["unit_price"], item["discount"], item["tax"]) < 0:
                    raise ValueError("Las cantidades y valores deben ser cero o mayores.")
                data["items"].append(item)
        if not all((data["supplier"], data["invoice_number"])):
            raise ValueError("Completa el proveedor y el número de factura.")
        if min(data["subtotal"], data["tax"], data["total"]) < 0:
            raise ValueError("Los valores de la factura no pueden ser negativos.")

        conn = db_connection()
        cur = conn.cursor()
        try:
            cur.execute("SELECT id_negocio FROM negocios WHERE id_negocio=%s FOR UPDATE", (user["business_id"],))
            cur.fetchone()
            cur.execute("SELECT id_factura FROM facturas WHERE id_factura=%s AND id_negocio=%s FOR UPDATE", (invoice_id, user["business_id"]))
            if not cur.fetchone():
                abort(404)

            cur.execute("SELECT f.id_factura,f.numero_factura,p.nit,p.nombre FROM facturas f JOIN proveedores p ON p.id_proveedor=f.id_proveedor WHERE f.id_negocio=%s AND f.id_factura<>%s", (user["business_id"], invoice_id))
            normalized_invoice_number = normalized_key(data["invoice_number"])
            for existing_id, existing_number, existing_nit, existing_name in cur.fetchall():
                if (normalized_key(existing_number) == normalized_invoice_number
                        and same_supplier(data["nit"], data["supplier"], existing_nit, existing_name)):
                    raise ValueError("Ya existe otra factura con ese número para el mismo proveedor.")

            cur.execute("SELECT id_proveedor,nit,nombre FROM proveedores WHERE id_negocio=%s FOR UPDATE", (user["business_id"],))
            supplier_id = None
            for existing_supplier_id, existing_nit, existing_name in cur.fetchall():
                if same_supplier(data["nit"], data["supplier"], existing_nit, existing_name):
                    supplier_id = existing_supplier_id
                    break
            if supplier_id is None:
                cur.execute("INSERT INTO proveedores(id_negocio,nombre,nit) VALUES(%s,%s,%s)",
                            (user["business_id"], data["supplier"], data["nit"] or None))
                supplier_id = cur.lastrowid

            reverse_invoice_stock(cur, invoice_id)
            cur.execute("UPDATE facturas SET id_proveedor=%s,numero_factura=%s,fecha_factura=%s,numero_resolucion=%s,subtotal=%s,iva=%s,total=%s WHERE id_factura=%s AND id_negocio=%s",
                        (supplier_id, data["invoice_number"], data["invoice_date"], data["resolution"], data["subtotal"], data["tax"], data["total"], invoice_id, user["business_id"]))
            for item in data["items"]:
                line_total = item["quantity"] * item["unit_price"] - item["discount"] + item["tax"]
                cur.execute("INSERT INTO detalle_factura(id_factura,descripcion,cantidad,valor_unitario,descuento,iva_linea,total_linea) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                            (invoice_id, item["name"], item["quantity"], item["unit_price"], item["discount"], item["tax"], line_total))
                item_id = cur.lastrowid
                cur.execute("SELECT id_producto FROM productos WHERE id_negocio=%s AND nombre=%s FOR UPDATE", (user["business_id"], item["name"]))
                product = cur.fetchone()
                if product:
                    product_id = product[0]
                else:
                    cur.execute("INSERT INTO productos(id_negocio,nombre,existencia) VALUES(%s,%s,0)", (user["business_id"], item["name"]))
                    product_id = cur.lastrowid
                cur.execute("UPDATE productos SET existencia=existencia+%s WHERE id_producto=%s", (item["quantity"], product_id))
                cur.execute("INSERT INTO movimientos_inventario(id_negocio,id_producto,id_usuario,id_detalle_factura,cambio_cantidad,motivo) VALUES(%s,%s,%s,%s,%s,'Edición de factura')",
                            (user["business_id"], product_id, user["id"], item_id, item["quantity"]))
            cur.execute("INSERT INTO registro_auditoria(id_negocio,id_usuario,tipo_entidad,id_entidad,accion,detalles) VALUES(%s,%s,'factura',%s,'editada',%s)",
                        (user["business_id"], user["id"], invoice_id, json.dumps({"number": data["invoice_number"]})))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        flash("Factura actualizada y el inventario recalculado.", "success")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))
    except (InvalidOperation, ValueError) as exc:
        flash(str(exc) or "Revisa los valores ingresados.", "danger")
    except mysql.connector.Error:
        app.logger.exception("No se pudo editar la factura")
        flash("No se pudo editar la factura en MySQL. No se guardaron cambios.", "danger")

    invoice.update({"supplier_name": request.form.get("supplier", ""), "tax_id": request.form.get("nit", ""),
                    "invoice_number": request.form.get("invoice_number", ""), "invoice_date": request.form.get("invoice_date", ""),
                    "resolution_number": request.form.get("resolution", ""), "subtotal": request.form.get("subtotal", ""),
                    "tax": request.form.get("tax", ""), "total": request.form.get("total", "")})
    invoice["items"] = [{"description": name, "quantity": quantities[i], "unit_price": prices[i],
                         "discount_amount": discounts[i], "tax_amount": taxes[i]}
                        for i, name in enumerate(item_names) if name.strip()]
    return render_template("editar_factura.html", invoice=invoice)


@app.post("/facturas/<int:invoice_id>/eliminar")
def delete_invoice(invoice_id):
    user = admin_required()
    conn = db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id_negocio FROM negocios WHERE id_negocio=%s FOR UPDATE", (user["business_id"],))
        cur.fetchone()
        cur.execute("SELECT numero_factura FROM facturas WHERE id_factura=%s AND id_negocio=%s FOR UPDATE", (invoice_id, user["business_id"]))
        invoice = cur.fetchone()
        if not invoice:
            abort(404)
        reverse_invoice_stock(cur, invoice_id)
        cur.execute("INSERT INTO registro_auditoria(id_negocio,id_usuario,tipo_entidad,id_entidad,accion,detalles) VALUES(%s,%s,'factura',%s,'eliminada',%s)",
                    (user["business_id"], user["id"], invoice_id, json.dumps({"number": invoice[0]})))
        cur.execute("DELETE FROM facturas WHERE id_factura=%s AND id_negocio=%s", (invoice_id, user["business_id"]))
        conn.commit()
        flash("Factura eliminada y el inventario actualizado.", "success")
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        app.logger.exception("No se pudo eliminar la factura")
        flash("No se pudo eliminar la factura. No se guardaron cambios.", "danger")
    finally:
        conn.close()
    return redirect(url_for("facturas"))


@app.get("/facturas/<int:invoice_id>/archivo")
def invoice_file(invoice_id):
    user = current_user()
    invoice = query("SELECT tipo_archivo AS mime_type,nombre_archivo AS source_name,archivo_original AS original_file FROM facturas WHERE id_factura=%s AND id_negocio=%s", (invoice_id, user["business_id"]), one=True)
    if not invoice:
        abort(404)
    content = invoice["original_file"]
    return send_file(io.BytesIO(content), mimetype=invoice["mime_type"], download_name=invoice["source_name"], as_attachment=False)


@app.route("/proveedores", methods=["GET", "POST"])
def proveedores():
    user = current_user()
    if request.method == "POST":
        admin_required()
        name, nit = request.form.get("name", "").strip(), request.form.get("nit", "").strip()
        if not name:
            flash("El proveedor requiere un nombre.", "danger")
        else:
            try:
                query("INSERT INTO proveedores(id_negocio,nombre,nit,telefono,correo) VALUES(%s,%s,%s,%s,%s)",
                      (user["business_id"], name, nit or None, request.form.get("phone", "").strip(), request.form.get("email", "").strip()), commit=True)
                flash("Proveedor creado.", "success")
            except mysql.connector.IntegrityError:
                flash("Ya existe un proveedor con ese NIT.", "danger")
        return redirect(url_for("proveedores"))
    rows = query("SELECT p.id_proveedor AS id,p.nombre AS name,p.nit AS tax_id,p.telefono AS phone,p.correo AS email,p.activo AS active,COUNT(f.id_factura) AS invoice_count FROM proveedores p LEFT JOIN facturas f ON f.id_proveedor=p.id_proveedor WHERE p.id_negocio=%s GROUP BY p.id_proveedor ORDER BY p.nombre", (user["business_id"],))
    return render_template("proveedores.html", proveedores=rows)


@app.route("/inventario", methods=["GET", "POST"])
def inventario():
    user = current_user()
    if request.method == "POST":
        admin_required()
        try:
            name = request.form.get("name", "").strip()
            quantity = Decimal(request.form.get("quantity", "0"))
            minimum = Decimal(request.form.get("minimum", "0"))
            query("INSERT INTO productos(id_negocio,nombre,unidad,existencia,cantidad_minima) VALUES(%s,%s,%s,%s,%s)",
                  (user["business_id"], name, request.form.get("unit", "und").strip(), quantity, minimum), commit=True)
            flash("Producto agregado al inventario.", "success")
        except (InvalidOperation, mysql.connector.IntegrityError):
            flash("No se pudo crear el producto. Verifica el nombre y las cantidades.", "danger")
        return redirect(url_for("inventario"))
    products = query("SELECT id_producto AS id,nombre AS name,unidad AS unit,existencia AS quantity,cantidad_minima AS min_quantity,CASE WHEN existencia=0 THEN 'Agotado' WHEN existencia<=cantidad_minima THEN 'Stock bajo' ELSE 'Disponible' END AS state FROM productos WHERE id_negocio=%s ORDER BY nombre", (user["business_id"],))
    return render_template("inventario.html", productos=products)


def report_filter_data(user):
    today = date.today()
    earliest_invoice = query("SELECT MIN(fecha_factura) AS first_date FROM facturas WHERE id_negocio=%s", (user["business_id"],), one=True)["first_date"]
    start_default = earliest_invoice or today
    try:
        start = date.fromisoformat(request.args["desde"]) if request.args.get("desde") else start_default
        end = date.fromisoformat(request.args.get("hasta", today.isoformat()))
    except ValueError:
        flash("El rango de fechas no es válido.", "danger")
        start, end = start_default, today
    if start > end:
        flash("La fecha inicial no puede ser posterior a la final.", "danger")
        start, end = end, start
    providers = query("SELECT id_proveedor AS id,nombre AS name FROM proveedores WHERE id_negocio=%s AND activo=1 ORDER BY nombre", (user["business_id"],))
    provider_id = request.args.get("proveedor", "")
    if provider_id and not any(str(row["id"]) == provider_id for row in providers):
        provider_id = ""
    status = request.args.get("estado", "Confirmada")
    if status not in {"Confirmada", "Anulada", "Todas"}:
        status = "Confirmada"
    search = request.args.get("q", "").strip()[:100]
    conditions = ["f.id_negocio=%s", "f.fecha_factura BETWEEN %s AND %s"]
    params = [user["business_id"], start, end]
    if status != "Todas":
        conditions.append("f.estado=%s")
        params.append(status)
    if provider_id:
        conditions.append("f.id_proveedor=%s")
        params.append(int(provider_id))
    if search:
        conditions.append("(f.numero_factura LIKE %s OR p.nombre LIKE %s OR EXISTS (SELECT 1 FROM detalle_factura ds WHERE ds.id_factura=f.id_factura AND ds.descripcion LIKE %s))")
        params.extend([f"%{search}%"] * 3)
    return {"start": start, "end": end, "providers": providers, "provider_id": provider_id,
            "status": status, "search": search, "where": " AND ".join(conditions), "params": params}


def report_summary(where, params):
    summary = query(
        "SELECT COUNT(DISTINCT f.id_factura) AS invoice_count,COALESCE(SUM(f.total),0) AS spent,COALESCE(AVG(f.total),0) AS average,COUNT(DISTINCT f.id_proveedor) AS supplier_count "
        f"FROM facturas f JOIN proveedores p ON p.id_proveedor=f.id_proveedor WHERE {where}", params, one=True,
    )
    units = query(
        "SELECT COALESCE(SUM(d.cantidad),0) AS units FROM detalle_factura d JOIN facturas f ON f.id_factura=d.id_factura JOIN proveedores p ON p.id_proveedor=f.id_proveedor WHERE " + where,
        params, one=True,
    )["units"]
    return summary, units


def report_change(current, previous):
    if not previous:
        return None
    return round((float(current) - float(previous)) * 100 / float(previous), 1)


@app.get("/reportes")
def reportes():
    user = current_user()
    filters = report_filter_data(user)
    where, params = filters["where"], filters["params"]
    summary, units = report_summary(where, params)
    duration = (filters["end"] - filters["start"]).days + 1
    previous_end = filters["start"] - timedelta(days=1)
    previous_start = previous_end - timedelta(days=duration - 1)
    previous_params = list(params)
    previous_params[1:3] = [previous_start, previous_end]
    previous_summary, previous_units = report_summary(where, previous_params)
    changes = {"invoices": report_change(summary["invoice_count"], previous_summary["invoice_count"]),
               "spent": report_change(summary["spent"], previous_summary["spent"]),
               "units": report_change(units, previous_units),
               "suppliers": report_change(summary["supplier_count"], previous_summary["supplier_count"])}

    by_month = query(
        "SELECT YEAR(f.fecha_factura) AS report_year,MONTH(f.fecha_factura) AS report_month,COALESCE(SUM(f.total),0) AS total,COUNT(*) AS invoice_count "
        f"FROM facturas f JOIN proveedores p ON p.id_proveedor=f.id_proveedor WHERE {where} GROUP BY YEAR(f.fecha_factura),MONTH(f.fecha_factura) ORDER BY report_year,report_month", params,
    )
    all_supplier_totals = query(
        "SELECT p.nombre AS name,SUM(f.total) AS total FROM facturas f JOIN proveedores p ON p.id_proveedor=f.id_proveedor "
        f"WHERE {where} GROUP BY p.id_proveedor,p.nombre ORDER BY total DESC", params,
    )
    supplier_totals = all_supplier_totals[:5]
    if len(all_supplier_totals) > 5:
        supplier_totals.append({"name": "Otros", "total": sum((row["total"] for row in all_supplier_totals[5:]), Decimal("0"))})
    top_products = query(
        "SELECT d.descripcion AS name,SUM(d.cantidad) AS quantity,SUM(d.total_linea) AS total "
        "FROM detalle_factura d JOIN facturas f ON f.id_factura=d.id_factura JOIN proveedores p ON p.id_proveedor=f.id_proveedor "
        f"WHERE {where} GROUP BY d.descripcion ORDER BY total DESC LIMIT 5", params,
    )
    try:
        page = max(1, min(int(request.args.get("pagina", "1")), 100000))
    except ValueError:
        page = 1
    page_size = 10
    row_count = query(
        "SELECT COUNT(*) AS n FROM facturas f JOIN proveedores p ON p.id_proveedor=f.id_proveedor LEFT JOIN detalle_factura d ON d.id_factura=f.id_factura WHERE " + where,
        params, one=True,
    )["n"]
    max_pages = max(1, (row_count + page_size - 1) // page_size)
    page = min(page, max_pages)
    rows = query(
        "SELECT f.id_factura AS id,f.fecha_factura AS invoice_date,f.numero_factura AS invoice_number,p.nombre AS supplier_name,COALESCE(d.descripcion,'Sin detalle') AS product_name,COALESCE(d.cantidad,0) AS quantity,COALESCE(d.valor_unitario,0) AS unit_price,COALESCE(d.total_linea,0) AS line_total,f.estado AS status "
        "FROM facturas f JOIN proveedores p ON p.id_proveedor=f.id_proveedor LEFT JOIN detalle_factura d ON d.id_factura=f.id_factura "
        f"WHERE {where} ORDER BY f.fecha_factura DESC,f.id_factura DESC,d.id_detalle LIMIT %s OFFSET %s",
        [*params, page_size, (page - 1) * page_size],
    )
    monthly_totals = {(int(row["report_year"]), int(row["report_month"])): row for row in by_month}
    month_names = ("Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic")
    months = []
    if summary["invoice_count"]:
        cursor_month = filters["start"].replace(day=1)
        final_month = filters["end"].replace(day=1)
        while cursor_month <= final_month:
            row = monthly_totals.get((cursor_month.year, cursor_month.month), {})
            months.append({
                "label": f"{month_names[cursor_month.month - 1]} {cursor_month.year}",
                "total": float(row.get("total") or 0),
                "invoice_count": int(row.get("invoice_count") or 0),
            })
            cursor_month = (cursor_month.replace(day=28) + timedelta(days=4)).replace(day=1)
    supplier_chart = [{"name": row["name"], "total": float(row["total"])} for row in supplier_totals]
    return render_template(
        "reportes.html", summary=summary, units=units, changes=changes, months=months,
        supplier_totals=supplier_totals, supplier_chart=supplier_chart, top_products=top_products, rows=rows,
        providers=filters["providers"], provider_id=filters["provider_id"], status=filters["status"], search=filters["search"],
        start_date=filters["start"].isoformat(), end_date=filters["end"].isoformat(),
        page=page, max_pages=max_pages, row_count=row_count, page_size=page_size,
    )


def report_export_rows(filters):
    return query(
        "SELECT f.fecha_factura AS invoice_date,f.numero_factura AS invoice_number,p.nombre AS supplier_name,COALESCE(d.descripcion,'Sin detalle') AS product_name,COALESCE(d.cantidad,0) AS quantity,COALESCE(d.valor_unitario,0) AS unit_price,COALESCE(d.total_linea,0) AS line_total,f.estado AS status "
        "FROM facturas f JOIN proveedores p ON p.id_proveedor=f.id_proveedor LEFT JOIN detalle_factura d ON d.id_factura=f.id_factura "
        f"WHERE {filters['where']} ORDER BY f.fecha_factura DESC,f.id_factura DESC,d.id_detalle", filters["params"],
    )


def export_rows_as_matrix(rows):
    headers = ["Fecha", "Factura", "Proveedor", "Producto", "Cantidad", "Valor unitario", "Valor total", "Estado"]
    matrix = [headers]
    matrix.extend([[row["invoice_date"], row["invoice_number"], row["supplier_name"], row["product_name"],
                    row["quantity"], row["unit_price"], row["line_total"], row["status"]] for row in rows])
    return matrix


@app.get("/reportes/exportar.csv")
def export_report_csv():
    user = current_user()
    filters = report_filter_data(user)
    rows = report_export_rows(filters)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerows(export_rows_as_matrix(rows))
    return Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=reporte_facturas.csv"})


@app.get("/reportes/exportar.xlsx")
def export_report_xlsx():
    user = current_user()
    matrix = export_rows_as_matrix(report_export_rows(report_filter_data(user)))
    spreadsheet_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    office_rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    content_ns = "http://schemas.openxmlformats.org/package/2006/content-types"
    worksheet = ET.Element(f"{{{spreadsheet_ns}}}worksheet")
    sheet_data = ET.SubElement(worksheet, f"{{{spreadsheet_ns}}}sheetData")

    def column_name(number):
        name = ""
        while number:
            number, remainder = divmod(number - 1, 26)
            name = chr(65 + remainder) + name
        return name

    for row_index, values in enumerate(matrix, 1):
        row_node = ET.SubElement(sheet_data, f"{{{spreadsheet_ns}}}row", {"r": str(row_index)})
        for column_index, value in enumerate(values, 1):
            cell = ET.SubElement(row_node, f"{{{spreadsheet_ns}}}c", {"r": f"{column_name(column_index)}{row_index}"})
            if isinstance(value, (int, float, Decimal)):
                ET.SubElement(cell, f"{{{spreadsheet_ns}}}v").text = str(value)
            else:
                cell.set("t", "inlineStr")
                inline = ET.SubElement(cell, f"{{{spreadsheet_ns}}}is")
                ET.SubElement(inline, f"{{{spreadsheet_ns}}}t").text = str(value)

    workbook = ET.Element(f"{{{spreadsheet_ns}}}workbook")
    sheets = ET.SubElement(workbook, f"{{{spreadsheet_ns}}}sheets")
    ET.SubElement(sheets, f"{{{spreadsheet_ns}}}sheet", {"name": "Reporte", "sheetId": "1", f"{{{office_rel_ns}}}id": "rId1"})
    workbook_rels = ET.Element(f"{{{package_rel_ns}}}Relationships")
    ET.SubElement(workbook_rels, f"{{{package_rel_ns}}}Relationship", {"Id": "rId1", "Type": f"{office_rel_ns}/worksheet", "Target": "worksheets/sheet1.xml"})
    package_rels = ET.Element(f"{{{package_rel_ns}}}Relationships")
    ET.SubElement(package_rels, f"{{{package_rel_ns}}}Relationship", {"Id": "rId1", "Type": f"{office_rel_ns}/officeDocument", "Target": "xl/workbook.xml"})
    content_types = ET.Element(f"{{{content_ns}}}Types")
    ET.SubElement(content_types, f"{{{content_ns}}}Default", {"Extension": "rels", "ContentType": "application/vnd.openxmlformats-package.relationships+xml"})
    ET.SubElement(content_types, f"{{{content_ns}}}Default", {"Extension": "xml", "ContentType": "application/xml"})
    ET.SubElement(content_types, f"{{{content_ns}}}Override", {"PartName": "/xl/workbook.xml", "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"})
    ET.SubElement(content_types, f"{{{content_ns}}}Override", {"PartName": "/xl/worksheets/sheet1.xml", "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"})

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as workbook_file:
        workbook_file.writestr("[Content_Types].xml", ET.tostring(content_types, encoding="utf-8", xml_declaration=True))
        workbook_file.writestr("_rels/.rels", ET.tostring(package_rels, encoding="utf-8", xml_declaration=True))
        workbook_file.writestr("xl/workbook.xml", ET.tostring(workbook, encoding="utf-8", xml_declaration=True))
        workbook_file.writestr("xl/_rels/workbook.xml.rels", ET.tostring(workbook_rels, encoding="utf-8", xml_declaration=True))
        workbook_file.writestr("xl/worksheets/sheet1.xml", ET.tostring(worksheet, encoding="utf-8", xml_declaration=True))
    return Response(output.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=reporte_facturas.xlsx"})


@app.get("/usuarios")
def users():
    user = admin_required()
    rows = query("SELECT id_usuario AS id,nombre AS name,correo AS email,celular AS phone,rol AS role,activo AS active FROM usuarios WHERE id_negocio=%s ORDER BY nombre", (user["business_id"],))
    return render_template("users.html", users=rows)


@app.post("/usuarios/crear")
def create_user():
    admin = admin_required()
    name, email, password = request.form.get("name", "").strip(), request.form.get("email", "").strip().lower(), request.form.get("password", "")
    role = request.form.get("role", "Comerciante")
    if role not in {"Administrador", "Comerciante"} or not name or not email or not password_is_valid(password):
        flash("Revisa los datos, el rol y los requisitos de contraseña.", "danger")
    else:
        try:
            query("INSERT INTO usuarios(id_negocio,nombre,correo,celular,clave_hash,rol) VALUES(%s,%s,%s,%s,%s,%s)",
                  (admin["business_id"], name, email, request.form.get("phone", "").strip(), password_hasher.hash(password), role), commit=True)
            flash("Usuario creado.", "success")
        except mysql.connector.IntegrityError:
            flash("Ese correo ya está registrado.", "danger")
    return redirect(url_for("users"))


@app.post("/usuarios/<int:user_id>/estado")
def toggle_user(user_id):
    admin = admin_required()
    if user_id == admin["id"]:
        flash("No puedes desactivar tu propia cuenta.", "danger")
    else:
        target = query("SELECT rol AS role,activo AS active FROM usuarios WHERE id_usuario=%s AND id_negocio=%s", (user_id, admin["business_id"]), one=True)
        admins = query("SELECT COUNT(*) AS n FROM usuarios WHERE id_negocio=%s AND rol='Administrador' AND activo=1", (admin["business_id"],), one=True)["n"]
        if target and target["active"] and target["role"] == "Administrador" and admins <= 1:
            flash("El negocio debe conservar al menos un Administrador activo.", "danger")
        else:
            query("UPDATE usuarios SET activo=1-activo WHERE id_usuario=%s AND id_negocio=%s", (user_id, admin["business_id"]), commit=True)
            flash("Estado de usuario actualizado.", "success")
    return redirect(url_for("users"))


@app.post("/usuarios/<int:user_id>/editar")
def edit_user(user_id):
    admin = admin_required()
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    role = request.form.get("role", "")
    if not name or not email or role not in {"Administrador", "Comerciante"}:
        flash("Revisa el nombre, correo y rol.", "danger")
    elif user_id == admin["id"] and role != "Administrador":
        flash("No puedes quitarte el rol de Administrador.", "danger")
    else:
        try:
            target = query("SELECT rol AS role FROM usuarios WHERE id_usuario=%s AND id_negocio=%s", (user_id, admin["business_id"]), one=True)
            admins = query("SELECT COUNT(*) AS n FROM usuarios WHERE id_negocio=%s AND rol='Administrador' AND activo=1", (admin["business_id"],), one=True)["n"]
            if target and target["role"] == "Administrador" and role != "Administrador" and admins <= 1:
                flash("El negocio debe conservar al menos un Administrador activo.", "danger")
                return redirect(url_for("users"))
            query("UPDATE usuarios SET nombre=%s,correo=%s,rol=%s WHERE id_usuario=%s AND id_negocio=%s",
                  (name, email, role, user_id, admin["business_id"]), commit=True)
            flash("Usuario actualizado.", "success")
        except mysql.connector.IntegrityError:
            flash("Ese correo ya está registrado.", "danger")
    return redirect(url_for("users"))


@app.get("/perfil")
def perfil():
    return render_template("perfil.html")


if __name__ == "__main__":
    if not app.config["SECRET_KEY"]:
        raise RuntimeError("Configura FLASK_SECRET_KEY antes de iniciar la aplicación.")
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1", port=int(os.environ.get("PORT", "5000")))
