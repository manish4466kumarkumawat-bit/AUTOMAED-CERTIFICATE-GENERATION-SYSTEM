import os
import uuid
import sqlite3
import smtplib
import io
import time
import json
import qrcode
import pandas as pd
import zipfile
import logging
import re
from flask import Flask, render_template, request, redirect, send_file, session, url_for, jsonify
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib.colors import HexColor
from datetime import datetime
from email.message import EmailMessage
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = "your-super-secret-key-min-32-characters-long-here"

BASE_URL = "https://yourdomain.com"
EMAIL_ADDRESS = "your-email@gmail.com"
EMAIL_PASSWORD = "your-app-password"

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "certificates.db")
CERT_DIR = os.path.join(BASE, "certificates")
QR_DIR = os.path.join(BASE, "qr_codes")
TPL_DIR = os.path.join(BASE, "static/templates")

ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'webp'}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB

for d in [CERT_DIR, QR_DIR, TPL_DIR]:
    os.makedirs(d, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_db_connection():
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

# ================== DATABASE INITIALIZATION ==================
def init_db():
    con = get_db_connection()
    # Create tables if not exist
    con.execute("""CREATE TABLE IF NOT EXISTS admins
        (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, password TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS companies
        (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT, phone TEXT, 
         username TEXT UNIQUE, password TEXT, created_at TEXT, is_active INTEGER DEFAULT 1)""")
    con.execute("""CREATE TABLE IF NOT EXISTS certificates
        (id TEXT PRIMARY KEY, company_id INTEGER, name TEXT, course TEXT, email TEXT, 
         date TEXT, email_sent INTEGER DEFAULT 0, template_id INTEGER, 
         FOREIGN KEY(company_id) REFERENCES companies(id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS templates
        (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER, name TEXT, bg_path TEXT, 
         created_at TEXT, is_shared INTEGER DEFAULT 0, shared_by_admin INTEGER DEFAULT 0,
         is_company_customized INTEGER DEFAULT 0, original_template_id INTEGER,
         company_custom_data TEXT,
         FOREIGN KEY(company_id) REFERENCES companies(id),
         FOREIGN KEY(original_template_id) REFERENCES templates(id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS template_elements
        (id INTEGER PRIMARY KEY AUTOINCREMENT, template_id INTEGER, el_name TEXT, 
         x_pct REAL, y_pct REAL, size_pct REAL, f_color TEXT, f_family TEXT, 
         is_qr INTEGER DEFAULT 0, is_bold INTEGER DEFAULT 0,
         text_shadow INTEGER DEFAULT 0, glow_effect INTEGER DEFAULT 0,
         opacity REAL DEFAULT 1.0, letter_spacing INTEGER DEFAULT 0,
         line_height REAL DEFAULT 1.0, el_type TEXT DEFAULT 'text',
         bg_color TEXT, border_color TEXT, border_width INTEGER DEFAULT 0,
         text_align TEXT DEFAULT 'center', rotation REAL DEFAULT 0,
         FOREIGN KEY(template_id) REFERENCES templates(id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS email_configuration
        (id INTEGER PRIMARY KEY AUTOINCREMENT, company_id INTEGER UNIQUE, from_email TEXT,
         from_name TEXT, subject_template TEXT, body_template TEXT,
         created_at TEXT, updated_at TEXT,
         FOREIGN KEY(company_id) REFERENCES companies(id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS template_images
        (id INTEGER PRIMARY KEY AUTOINCREMENT, template_id INTEGER, image_path TEXT,
         image_type TEXT, x_pct REAL, y_pct REAL, width_pct REAL, height_pct REAL,
         opacity REAL DEFAULT 1.0, z_index INTEGER, created_at TEXT,
         FOREIGN KEY(template_id) REFERENCES templates(id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS audit_logs
        (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT,
         resource_type TEXT, resource_id TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
         ip_address TEXT)""")
    # Indexes
    con.execute("CREATE INDEX IF NOT EXISTS idx_certs_company ON certificates(company_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_templates_admin ON templates(shared_by_admin)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_elements_template ON template_elements(template_id)")
    # Default admin user
    try:
        password_hash = generate_password_hash("admin123", method='pbkdf2:sha256')
        con.execute("INSERT INTO admins (username, password) VALUES (?, ?)", ("admin", password_hash))
    except sqlite3.IntegrityError:
        pass
    con.commit()
    con.close()

init_db()

# ================== PDF GENERATION ==================
def generate_pdf_from_template(cid, data_dict, template_id):
    try:
        con = get_db_connection()
        tpl = con.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone()
        elements = con.execute("SELECT * FROM template_elements WHERE template_id=?", (template_id,)).fetchall()
        con.close()
        if not tpl:
            logger.error(f"Template {template_id} not found")
            return False
        pdf_path = os.path.join(CERT_DIR, f"{cid}.pdf")
        c = canvas.Canvas(pdf_path, pagesize=landscape(A4))
        width, height = landscape(A4)

        # Draw background image
        bg_img_path = os.path.join(TPL_DIR, tpl['bg_path'])
        if os.path.exists(bg_img_path):
            try:
                c.drawImage(bg_img_path, 0, 0, width=width, height=height)
            except Exception as e:
                logger.error(f"Background Error: {e}")

        # Generate QR code
        qr_path = os.path.join(QR_DIR, f"{cid}.png")
        try:
            qr = qrcode.make(f"{BASE_URL}/verify/{cid}")
            qr.save(qr_path)
        except Exception as e:
            logger.error(f"QR Generation Error: {e}")

        # Draw elements
        for el in elements:
            try:
                value = data_dict.get(el['el_name'], el['el_name'])
                x = (el['x_pct'] / 100) * width
                y_from_top = (el['y_pct'] / 100) * height
                y = height - y_from_top
                if el['is_qr']:
                    if os.path.exists(qr_path):
                        try:
                            qr_size = el['size_pct'] if el['size_pct'] else 60
                            c.drawImage(qr_path, x - qr_size/2, y - qr_size/2, qr_size, qr_size)
                        except Exception as e:
                            logger.error(f"QR Drawing Error: {e}")
                else:
                    try:
                        font_size = el['size_pct'] if el['size_pct'] else 16
                        font_name = "Helvetica-Bold" if el['is_bold'] else "Helvetica"
                        c.setFont(font_name, font_size)
                        try:
                            c.setFillColor(HexColor(el['f_color'] or "#000000"))
                        except:
                            c.setFillColor(HexColor("#000000"))
                        c.drawCentredString(x, y, str(value))
                    except Exception as e:
                        logger.error(f"Text Drawing Error: {e}")
            except Exception as e:
                logger.error(f"Element Error: {e}")
                continue
        c.save()
        if os.path.exists(qr_path):
            os.remove(qr_path)
        logger.info(f"PDF saved: {pdf_path}")
        return True
    except Exception as e:
        logger.error(f"PDF Generation Error: {e}")
        return False

# ================== EMAIL SENDING ==================
def send_email_with_pdf(cid, to_email, student_name, company_id=None):
    try:
        con = get_db_connection()
        if company_id:
            config = con.execute("SELECT * FROM email_configuration WHERE company_id=?", (company_id,)).fetchone()
        else:
            config = None
        con.close()
        from_email = config['from_email'] if config else EMAIL_ADDRESS
        from_name = config['from_name'] if config else "Certificate System"
        subject_template = config['subject_template'] if config else "Your Certificate"
        body_template = config['body_template'] if config else "Dear [Student Name],\n\nPlease find attached your certificate.\n\nVerify: [Verification Link]"
        pdf_path = os.path.join(CERT_DIR, f"{cid}.pdf")
        if not os.path.exists(pdf_path):
            return False
        subject = subject_template
        body = body_template
        replacements = {
            "[Student Name]": student_name,
            "[Verification Link]": f"{BASE_URL}/verify/{cid}",
            "[Certificate ID]": cid,
            "[Date]": datetime.now().strftime("%d-%m-%Y"),
        }
        for key, value in replacements.items():
            subject = subject.replace(key, value)
            body = body.replace(key, value)
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = f"{from_name} <{from_email}>"
        msg['To'] = to_email
        msg.set_content(body)
        with open(pdf_path, 'rb') as f:
            msg.add_attachment(f.read(), maintype='application', subtype='pdf', filename=f"{student_name}_certificate.pdf")
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            smtp.send_message(msg)
        logger.info(f"Email sent to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Email Error: {e}")
        return False

# ================== Routes ==================

@app.route("/")
def index():
    return render_template("index.html")

# ================== Admin routes... (your existing code) ==================
# (Include all your admin routes here, as in your code above)

# ... (your existing admin routes code remains unchanged) ...

# ================== Company routes ==================

@app.route("/company/advanced-editor")
@app.route("/company/advanced-editor/<int:tid>")
def company_advanced_editor(tid=None):
    """Company advanced template editor"""
    if "company" not in session:
        return redirect("/company")
    return render_template("company_advanced_editor.html", template_id=tid)

# ... (rest of your company routes) ...

# ================== Error Handlers ==================

@app.errorhandler(404)
def not_found(error):
    return render_template("index.html"), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"500 error: {error}")
    return render_template("index.html"), 500

# ================== Main ==================

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)