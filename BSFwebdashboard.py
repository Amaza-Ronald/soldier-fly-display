# BSFwebdashboard.py - Main Flask application for BSF Larvae Monitoring Dashboard

from flask import Flask, render_template, request, redirect, url_for, jsonify, session, flash, send_file, Response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import os
import base64
from PIL import Image
from io import BytesIO
from random import uniform, randint
import json
import time
from flask import Response, stream_with_context
import queue  # Add this import
# Add query optimization
from sqlalchemy.orm import load_only

import threading
import paho.mqtt.client as mqtt

import gc
import uuid
import collections
from collections import OrderedDict
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import random
import string


# # --- Flask App Configuration ---
# app = Flask(__name__, static_folder='static')
# app.secret_key = os.urandom(24)

# # PostgreSQL Database Configuration
# database_url = os.environ.get('DATABASE_URL', 'sqlite:///larvae_monitoring.db')

# # Fix for Render PostgreSQL URL format
# if database_url and database_url.startswith('postgres://'):
#     database_url = database_url.replace('postgres://', 'postgresql://', 1)

# app.config['SQLALCHEMY_DATABASE_URI'] = database_url
# app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
#     'pool_recycle': 300,
#     'pool_pre_ping': True
# }

# --- Flask App Configuration ---
app = Flask(__name__, static_folder='static')
app.secret_key = os.urandom(24)

# PostgreSQL Database Configuration
database_url = os.environ.get('DATABASE_URL', 'sqlite:///larvae_monitoring.db')

# Fix for Render PostgreSQL URL format
if database_url and database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_size': 5,           # Smaller pool for Render free tier
    'max_overflow': 10,       # Maximum overflow connections
    'pool_recycle': 300,      # Recycle connections after 5 minutes
    'pool_pre_ping': True,    # Verify connections before use
    'pool_timeout': 90        # Timeout for getting connection
}
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False  # Reduce JSON size
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload


db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'



# def cleanup_memory():
#     """Memory cleanup with aggressive garbage collection for Render"""
#     while True:
#         time.sleep(60)  # Run every 1 minute (more aggressive)
#         try:
#             # Clean up inactive SSE clients
#             removed = client_manager.cleanup_inactive(max_age_seconds=120)
            
#             # Aggressive garbage collection
#             gc.collect()
            
#             if removed:
#                 print(f"🧹 Memory cleanup: removed {len(removed)} clients")
            
#         except Exception as e:
#             print(f"⚠️ Cleanup error: {e}")


def cleanup_memory():
    """Memory cleanup with aggressive garbage collection for Render"""
    while True:
        time.sleep(30)  # Run every 30 seconds (changed from 60)
        try:
            # Clean up inactive SSE clients (reduced from 120)
            removed = client_manager.cleanup_inactive(max_age_seconds=60)
            
            # Force disconnect clients if too many
            with client_manager.lock:
                if len(client_manager.clients) > 5:
                    # Remove oldest clients
                    to_remove = list(client_manager.clients.keys())[:-5]
                    for client_id in to_remove:
                        client_manager.clients.pop(client_id, None)
                    print(f"🧹 Force removed {len(to_remove)} excess clients")
            
            # Aggressive garbage collection
            gc.collect()
            
            if removed:
                print(f"🧹 Memory cleanup: removed {len(removed)} clients")
            
        except Exception as e:
            print(f"⚠️ Cleanup error: {e}")
            

# Start cleanup thread (add this after your app initialization)
cleanup_thread = threading.Thread(target=cleanup_memory, daemon=True)
cleanup_thread.start()

# --- MQTT Configuration ---
MQTT_BROKER = "broker.hivemq.com"
MQTT_PORT = 1883
MQTT_TOPIC = "bsf_monitor/larvae_data"

mqtt_client = None
mqtt_thread = None


# --- Thread-safe SSE Client Management ---
class ClientManager:
    """Thread-safe manager for SSE clients to prevent memory leaks"""
    def __init__(self):
        self.clients = OrderedDict()
        self.lock = threading.Lock()
        self.max_clients = 10  # Prevent memory exhaustion
        self.max_queue_size = 5  # Prevent queue memory bloat
    
    def add_client(self, client_id):
        """Add a new client with a bounded queue"""
        with self.lock:
            if len(self.clients) >= self.max_clients:
                # Remove oldest client to prevent memory exhaustion
                oldest_id = next(iter(self.clients))
                self.clients.pop(oldest_id, None)
                print(f"🧹 Removed oldest client {oldest_id} to prevent memory overflow")
            
            client_queue = queue.Queue(maxsize=self.max_queue_size)
            self.clients[client_id] = {
                'queue': client_queue,
                'created': time.time(),
                'last_active': time.time()
            }
            print(f"✅ Added client {client_id}, total: {len(self.clients)}")
            return client_queue
    
    def remove_client(self, client_id):
        """Remove a client"""
        with self.lock:
            if client_id in self.clients:
                self.clients.pop(client_id, None)
                print(f"✅ Removed client {client_id}, remaining: {len(self.clients)}")
    
    def broadcast(self, data):
        """Broadcast data to all clients - thread-safe with timeout"""
        disconnected = []
        
        with self.lock:
            clients_copy = list(self.clients.items())
        
        for client_id, client_info in clients_copy:
            try:
                # Use put_nowait with bounded queue to prevent memory bloat
                client_info['queue'].put_nowait(data)
                client_info['last_active'] = time.time()
            except queue.Full:
                # Queue is full - client is not reading fast enough, remove it
                print(f"⚠️ Queue full for client {client_id}, removing")
                disconnected.append(client_id)
            except Exception as e:
                print(f"❌ Error sending to client {client_id}: {e}")
                disconnected.append(client_id)
        
        # Clean up disconnected clients
        for client_id in disconnected:
            self.remove_client(client_id)
        
        return len(clients_copy) - len(disconnected)
    
    def cleanup_inactive(self, max_age_seconds=120):
        """Remove inactive clients (older than max_age_seconds)"""
        current_time = time.time()
        removed = []
        
        with self.lock:
            to_remove = []
            for client_id, client_info in list(self.clients.items()):
                if current_time - client_info['last_active'] > max_age_seconds:
                    to_remove.append(client_id)
            
            for client_id in to_remove:
                self.clients.pop(client_id, None)
                removed.append(client_id)
        
        if removed:
            print(f"🧹 Cleaned up {len(removed)} inactive clients")
        return removed

# Initialize the thread-safe client manager
client_manager = ClientManager()

# Keep broadcast_to_clients for backward compatibility
def broadcast_to_clients(data):
    """Legacy function - use client_manager.broadcast instead"""
    return client_manager.broadcast(data)



# --- Database Models (UPDATED for PostgreSQL) ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=True)
    password_hash = db.Column(db.Text, nullable=False)  # CHANGED: String → Text (unlimited length)
    is_verified = db.Column(db.Boolean, default=False)
    verification_code = db.Column(db.String(6), nullable=True)
    verification_code_expires = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def generate_verification_code(self):
        """Generate a 6-digit verification code"""
        self.verification_code = ''.join(random.choices(string.digits, k=6))
        self.verification_code_expires = datetime.now(timezone.utc) + timedelta(minutes=15)
        return self.verification_code
    
    def verify_code(self, code):
        """Check if verification code is valid and not expired"""
        if not self.verification_code or not self.verification_code_expires:
            return False
        if datetime.now(timezone.utc) > self.verification_code_expires:
            return False
        return self.verification_code == code

class ImageFile(db.Model):
    __tablename__ = "image_files"
    id = db.Column(db.Integer, primary_key=True)
    tray_number = db.Column(db.Integer, nullable=False,index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    
    # Use LargeBinary for PostgreSQL BYTEA - MADE OPTIONAL
    image_data = db.Column(db.LargeBinary, nullable=True)
    image_format = db.Column(db.String(10), nullable=True)
    image_size = db.Column(db.Integer, nullable=True)
    
    # Metadata
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),index=True)
    avg_length = db.Column(db.Float, nullable=True)
    avg_weight = db.Column(db.Float, nullable=True)
    count = db.Column(db.Integer, nullable=True)
    
    # Use Text for PostgreSQL (better for large JSON)
    bounding_boxes = db.Column(db.Text, nullable=True)
    masks = db.Column(db.Text, nullable=True)

    # ADDED: Composite index for common queries
    __table_args__ = (
        db.Index('idx_image_tray_timestamp', 'tray_number', 'timestamp'),
        db.Index('idx_image_user_tray', 'user_id', 'tray_number'),
    )

    def __repr__(self):
        return f"<ImageFile Tray {self.tray_number} - {self.timestamp}>"

class LarvaeData(db.Model):
    __tablename__ = "larvae_data"
    id = db.Column(db.Integer, primary_key=True)
    tray_number = db.Column(db.Integer, nullable=False,index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    length = db.Column(db.Float, nullable=False)
    width = db.Column(db.Float, nullable=False)
    area = db.Column(db.Float, nullable=False)
    weight = db.Column(db.Float, nullable=False)
    count = db.Column(db.Integer, nullable=False)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),index=True)

    # ADDED: Composite index for common queries
    __table_args__ = (
        db.Index('idx_larvae_tray_timestamp', 'tray_number', 'timestamp'),
        db.Index('idx_larvae_user_tray', 'user_id', 'tray_number'),
    )

    def __repr__(self):
        return f"<LarvaeData Tray {self.tray_number} - {self.timestamp}>"

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- Helper Functions ---
def get_latest_tray_data(tray_number):
    """Fetches the most recent larvae data entry for a specific tray."""
    return LarvaeData.query.filter_by(tray_number=tray_number).order_by(LarvaeData.timestamp.desc()).first()

def calculate_weight_distribution_backend(weights_array):
    """Calculates the distribution of larvae weights into predefined bins."""
    weight_bins = {
        "80-90": 0, "90-100": 0, "100-110": 0,
        "110-120": 0, "120-130": 0, "130-140": 0, "140+": 0
    }
    for weight in weights_array:
        if 80 <= weight < 90: weight_bins["80-90"] += 1
        elif 90 <= weight < 100: weight_bins["90-100"] += 1
        elif 100 <= weight < 110: weight_bins["100-110"] += 1
        elif 110 <= weight < 120: weight_bins["110-120"] += 1
        elif 120 <= weight < 130: weight_bins["120-130"] += 1
        elif 130 <= weight < 140: weight_bins["130-140"] += 1
        else: weight_bins["140+"] += 1
    return list(weight_bins.keys()), list(weight_bins.values())

# --- Flask Routes ---
@app.route('/')
def home():
    """Redirects the root URL to the login page."""
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Handles user login."""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if not username or not password:
            flash('Please enter both username and password', 'danger')
            return render_template('login.html')
        
        user = User.query.filter_by(username=username).first()

        if not user:
            flash('Account does not exist. Please register first.', 'danger')
            return render_template('login.html')
        
        if not user.check_password(password):
            flash('Incorrect password. Please try again.', 'danger')
            return render_template('login.html')
        
        # Login successful
        login_user(user)
        flash('Login successful!', 'success')
        return redirect(url_for('dashboard'))
        
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    """Handles new user registration without email."""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if User.query.filter_by(username=username).first():
            flash('Username already exists', 'danger')
            return redirect(url_for('register'))

        try:
            user = User(username=username)
            user.set_password(password)
            user.is_verified = True # Auto-verify since removed email
            
            db.session.add(user)
            db.session.commit()
            
            # Log the user in automatically after registration
            login_user(user)
            flash('Registration successful! Welcome to your dashboard.', 'success')
            return redirect(url_for('dashboard'))
                
        except Exception as e:
            db.session.rollback()
            flash(f'Registration failed: {e}. Please try again.', 'danger')

    return render_template('register.html')


@app.route('/verify_email/<int:user_id>', methods=['GET', 'POST'])
def verify_email(user_id):
    """Handles email verification with 6-digit code."""
    user = User.query.get(user_id)
    if not user:
        flash('User not found', 'danger')
        return redirect(url_for('register'))
    
    if user.is_verified:
        flash('Email already verified. Please login.', 'info')
        return redirect(url_for('login'))
    
    # Check if email was sent (from query param)
    email_sent = request.args.get('code_sent', 'true').lower() == 'true'
    show_code = not email_sent  # Show code on page if email wasn't sent
    
    if request.method == 'POST':
        code = request.form.get('code')
        
        if user.verify_code(code):
            user.is_verified = True
            user.verification_code = None
            user.verification_code_expires = None
            db.session.commit()
            flash('Email verified successfully! You can now login.', 'success')
            return redirect(url_for('login'))
        else:
            flash('Invalid or expired verification code. Please try again.', 'danger')
    
    return render_template('verify_email.html', user=user, show_code=show_code)


@app.route('/resend_code/<int:user_id>', methods=['POST'])
def resend_verification_code(user_id):
    """Resend verification code to user's email."""
    user = User.query.get(user_id)
    if not user:
        return jsonify({'success': False, 'message': 'User not found'}), 404
    
    if user.is_verified:
        return jsonify({'success': False, 'message': 'Email already verified'}), 400
    
    try:
        verification_code = user.generate_verification_code()
        db.session.commit()
        
        email_sent = send_verification_email(user.email, verification_code)
        
        if email_sent:
            return jsonify({'success': True, 'message': 'Verification code sent to your email'})
        else:
            # If email fails, return the code in the response
            return jsonify({
                'success': True, 
                'message': f'Email service unavailable. Your verification code is: {verification_code}',
                'show_code': True,
                'code': verification_code
            })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


def send_verification_email(email, code):
    """Send verification code via email using SMTP."""
    # Always print code to console for debugging
    print(f"\n{'='*60}")
    print(f"📧 VERIFICATION CODE for {email}: {code}")
    print(f"{'='*60}\n")
    
    try:
        # Email configuration - replace with your SMTP settings
        smtp_server = os.environ.get('SMTP_SERVER', '')
        smtp_port = int(os.environ.get('SMTP_PORT', 587))
        smtp_username = os.environ.get('SMTP_USERNAME', '')
        smtp_password = os.environ.get('SMTP_PASSWORD', '')
        from_email = os.environ.get('FROM_EMAIL', smtp_username)
        
        if not smtp_server or not smtp_username or not smtp_password:
            print("⚠️ SMTP not configured. Code displayed above.")
            return False
        
        # Create message
        msg = MIMEMultipart('alternative')
        msg['Subject'] = 'Email Verification - BSF Larvae Monitoring'
        msg['From'] = from_email
        msg['To'] = email
        
        # HTML email body
        html = f"""
        <html>
          <body style="font-family: Arial, sans-serif; padding: 20px;">
            <div style="max-width: 600px; margin: 0 auto; background-color: #f9f9f9; padding: 30px; border-radius: 10px;">
              <h2 style="color: #3498db;">Email Verification</h2>
              <p>Thank you for registering with BSF Larvae Monitoring Dashboard!</p>
              <p>Your verification code is:</p>
              <div style="background-color: #3498db; color: white; font-size: 32px; font-weight: bold; padding: 20px; text-align: center; border-radius: 5px; letter-spacing: 5px;">
                {code}
              </div>
              <p style="margin-top: 20px;">This code will expire in 15 minutes.</p>
              <p style="color: #7f8c8d; font-size: 12px;">If you didn't request this code, please ignore this email.</p>
            </div>
          </body>
        </html>
        """
        
        msg.attach(MIMEText(html, 'html'))
        
        # Send email
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(smtp_username, smtp_password)
            server.send_message(msg)
        
        print(f"✅ Verification email sent to {email}")
        return True
        
    except Exception as e:
        print(f"❌ Failed to send verification email: {e}")
        print(f"Verification code for {email}: {code}")
        return False



@app.route('/image/<int:image_id>')
@login_required
def get_image(image_id):
    return "Images are disabled", 404


@app.route('/image_thumbnail/<int:image_id>')
@login_required
def get_image_thumbnail(image_id):
    """Image thumbnails are disabled."""
    return jsonify({"error": "Images are disabled"}), 404

# # CHANGED: Updated image API to work with BLOB storage
# @app.route('/api/images/<tray_number>')
# @login_required
# def get_images(tray_number):
#     """Get images from database with BLOB data converted to URLs"""
#     try:
#         if tray_number == 'all':
#             images = ImageFile.query.order_by(ImageFile.timestamp.desc()).all()
#         else:
#             images = ImageFile.query.filter_by(tray_number=int(tray_number)).order_by(ImageFile.timestamp.desc()).all()
        
#         image_list = []
#         for img in images: 
#             image_list.append({
#                 "id": img.id,
#                 "tray": img.tray_number,
#                 "src": url_for('get_image', image_id=img.id),  # Use BLOB route
#                 "thumbnail": url_for('get_image_thumbnail', image_id=img.id),  # Thumbnail URL
#                 "timestamp": img.timestamp.isoformat(),
#                 "count": img.count,
#                 "avgLength": img.avg_length,
#                 "avgWeight": img.avg_weight,
#                 "bounding_boxes": json.loads(img.bounding_boxes) if img.bounding_boxes else [],
#                 "masks": json.loads(img.masks) if img.masks else [],
#                 "size": img.image_size,
#                 "format": img.image_format
#             })
#         return jsonify(image_list)
#     except Exception as e:
#         print(f"Error fetching images: {e}")
#         return jsonify([])



@app.route('/api/images/<tray_number>')
@login_required
def get_images(tray_number):
    """Images are optional/disabled; return empty list so dashboard still works."""
    return jsonify([])




# @app.route('/api/images/<tray_number>')
# @login_required
# def get_images(tray_number):
#     """Get images from database - MEMORY-OPTIMIZED VERSION"""
#     try:
        
#         query = ImageFile.query.options(
#             load_only(
#                 ImageFile.id,
#                 ImageFile.tray_number,
#                 ImageFile.image_format,
#                 ImageFile.timestamp,
#                 ImageFile.count,
#                 ImageFile.avg_length,
#                 ImageFile.avg_weight,
#                 ImageFile.image_size
#                 # REMOVED: image_data, bounding_boxes, masks from initial load
                
#             )
#         )
        
#         # CRITICAL: Limit to 8 most recent images only
#         if tray_number == 'all':
#             images = query.order_by(ImageFile.timestamp.desc()).limit(8).all()
#         else:
#             images = query.filter_by(tray_number=int(tray_number))\
#                          .order_by(ImageFile.timestamp.desc())\
#                          .limit(8).all()
        
#         image_list = []
#         for img in images: 
#             image_url = url_for('get_image', image_id=img.id)
            
#             image_list.append({
#                 "id": img.id,
#                 "tray": img.tray_number,
#                 "src": image_url,
#                 "timestamp": img.timestamp.isoformat(),
#                 "count": img.count,
#                 "avgLength": img.avg_length,
#                 "avgWeight": img.avg_weight,
#                 "size": img.image_size,
#                 "format": img.image_format,
#                 # Removed heavy data
#                 "bounding_boxes": [],
#                 "masks": []
#             })
#         return jsonify(image_list)
#     except Exception as e:
#         print(f"Error fetching images: {e}")
#         return jsonify([])


# Add this route to your existing Flask routes
@app.route('/api/upload', methods=['POST'])
def upload_image():
    """Handle image upload and store in database as BLOB - requires authentication"""
    try:
        # Check for authentication (username and password in JSON)
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        username = data.get('username')
        password = data.get('password')
        
        if not username or not password:
            return jsonify({"error": "Authentication required: username and password must be provided"}), 401
        
        # Authenticate user
        user = User.query.filter_by(username=username).first()
        if not user or not user.check_password(password):
            return jsonify({"error": "Invalid credentials"}), 401
        
        # Email verification is no longer required. Auto-heal older accounts
        # that may still have is_verified=False from previous flows.
        if not user.is_verified:
            user.is_verified = True
            db.session.commit()

        image_data = data.get('image_data')

        def _to_int(value, default=0):
            try:
                if value is None or value == "":
                    return default
                return int(value)
            except (ValueError, TypeError):
                return default

        def _to_float(value, default=0.0):
            try:
                if value is None or value == "":
                    return default
                return float(value)
            except (ValueError, TypeError):
                return default

        tray_number = data.get('tray_number')
        if tray_number is None or str(tray_number).strip() == "":
            tray_number = data.get('tray_id')
        if tray_number is None or str(tray_number).strip() == "":
            tray_number = data.get('tray')
        count = _to_int(data.get('count', 0), 0)
        avg_length = _to_float(data.get('avg_length', 0), 0.0)
        avg_width = _to_float(data.get('avg_width', 0), 0.0)
        avg_area = _to_float(data.get('avg_area', 0), 0.0)
        avg_weight = _to_float(data.get('avg_weight', 0), 0.0)
        individual_weights = data.get('individual_weights', [])
        if not isinstance(individual_weights, list):
            individual_weights = []
        individual_weights = [_to_float(w, None) for w in individual_weights]
        individual_weights = [w for w in individual_weights if w is not None]
        bounding_boxes_json = data.get('bounding_boxes', '[]')
        masks_json = data.get('masks', '[]')
        
        if tray_number is None or str(tray_number).strip() == "":
            return jsonify({"error": "Missing tray ID. Use one of: tray_number, tray_id, tray"}), 400

        try:
            tray_number = int(tray_number)
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid tray ID. It must be a number"}), 400

        # Process image if provided (make it optional)
        image_binary = None
        image_format = None
        image_size = None
        
        if image_data:
            # Decode the base64 image
            try:
                image_binary = base64.b64decode(image_data)
            except Exception as e:
                return jsonify({"error": "Invalid image data"}), 400

            # Process image for BLOB storage
            try:
                img = Image.open(BytesIO(image_binary))
                image_format = img.format.lower() if img.format else 'jpeg'
                image_size = len(image_binary)
                
                # Compress if too large (optional)
                if image_size > 2 * 1024 * 1024:  # 2MB
                    output = BytesIO()
                    img.save(output, format='JPEG', quality=85, optimize=True)
                    image_binary = output.getvalue()
                    image_size = len(image_binary)
                    image_format = 'jpeg'
            except Exception as e:
                return jsonify({"error": "Invalid image format"}), 400
        else:
            print(f"⚠️ No image provided for Tray {tray_number} - storing data only")
        
        # NEW: Save individual larvae data for weight distribution
        if individual_weights and len(individual_weights) > 0:
            print(f"💾 Saving {len(individual_weights)} individual larvae entries for Tray {tray_number}")
            for weight in individual_weights:
                larvae_entry = LarvaeData(
                    tray_number=tray_number,
                    user_id=user.id,
                    length=avg_length,
                    width=avg_width,
                    area=avg_area,
                    weight=weight,  # Individual weight
                    count=1,  # Each entry is one larva
                    timestamp=datetime.now(timezone.utc)
                )
                db.session.add(larvae_entry)
        
        # If individual weights are missing, still store one summary data point
        if not individual_weights:
            larvae_entry = LarvaeData(
                tray_number=tray_number,
                user_id=user.id,
                length=avg_length,
                width=avg_width,
                area=avg_area,
                weight=avg_weight,
                count=count if count else 0,
                timestamp=datetime.now(timezone.utc)
            )
            db.session.add(larvae_entry)

        try:
            db.session.commit()

            update_data = {
                'type': 'new_data',
                'tray_number': tray_number,
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'image_id': None,
                'count': count,
                'avg_length': avg_length,
                'avg_weight': avg_weight
            }

            broadcast_to_clients(update_data)

            return jsonify({
                "message": "Data saved successfully",
                "tray_number": tray_number,
                "image_saved": False
            }), 200

        except Exception as e:
            db.session.rollback()
            return jsonify({"error": "Database error: " + str(e)}), 500

    except Exception as e:
        print(f"Error during image upload: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/get_upload_data/<int:image_id>')
@login_required
def get_upload_data(image_id):
    """
    Fetches data for a specific upload/image (by image_id) for current user.
    This shows only the larvae data associated with that specific upload.
    """
    try:
        # Get the image to verify ownership and get tray number
        image = ImageFile.query.filter_by(id=image_id, user_id=current_user.id).first()
        if not image:
            return jsonify({"error": "Image not found or access denied"}), 404
        
        # Get larvae data for this specific upload (same tray, same timestamp window)
        # We'll get data within 1 minute of the image upload time
        from datetime import timedelta
        time_window_start = image.timestamp - timedelta(minutes=1)
        time_window_end = image.timestamp + timedelta(minutes=1)
        
        upload_data = LarvaeData.query.filter_by(
            tray_number=image.tray_number,
            user_id=current_user.id
        ).filter(
            LarvaeData.timestamp >= time_window_start,
            LarvaeData.timestamp <= time_window_end
        ).order_by(LarvaeData.timestamp.asc()).all()

        if not upload_data:
            return jsonify({"error": f"No data found for upload {image_id}"}), 404

        # Process growth data (for single upload, just one data point)
        growth_data = {"days": [1], "length": [], "weight": []}
        
        # Calculate averages for this upload
        avg_length = sum(e.length for e in upload_data) / len(upload_data)
        avg_weight = sum(e.weight for e in upload_data) / len(upload_data)
        growth_data["length"].append(round(avg_length, 1))
        growth_data["weight"].append(round(avg_weight, 3))

        # Get metrics
        total_count = len(upload_data)  # Each entry is 1 larva

        # Calculate weight distribution
        weight_bins = {
            "0.08-0.10": 0, "0.10-0.12": 0, "0.12-0.14": 0,
            "0.14-0.16": 0, "0.16-0.18": 0, "0.18-0.20": 0, "0.20+": 0
        }

        for entry in upload_data:
            weight = entry.weight
            if 0.08 <= weight < 0.10: weight_bins["0.08-0.10"] += 1
            elif 0.10 <= weight < 0.12: weight_bins["0.10-0.12"] += 1
            elif 0.12 <= weight < 0.14: weight_bins["0.12-0.14"] += 1
            elif 0.14 <= weight < 0.16: weight_bins["0.14-0.16"] += 1
            elif 0.16 <= weight < 0.18: weight_bins["0.16-0.18"] += 1
            elif 0.18 <= weight < 0.20: weight_bins["0.18-0.20"] += 1
            elif weight >= 0.20: weight_bins["0.20+"] += 1

        return jsonify({
            "metrics": {
                "length": round(avg_length, 1),
                "width": round(sum(e.width for e in upload_data) / len(upload_data), 1),
                "area": round(sum(e.area for e in upload_data) / len(upload_data), 1),
                "weight": round(avg_weight, 3),
                "count": total_count
            },
            "growthData": growth_data,
            "weightDistribution": {
                "ranges": list(weight_bins.keys()),
                "counts": list(weight_bins.values())
            },
            "timestamp": image.timestamp.isoformat()
        })
    except Exception as e:
        app.logger.error(f"Error fetching upload data for image {image_id}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/get_tray_data/<int:tray_number>')
@login_required
def get_tray_data(tray_number):
    """
    Fetches and processes historical data for a specific tray ID using
    larvae measurements only.
    """
    try:
        # Get all larvae measurements for this tray
        tray_entries = LarvaeData.query.filter_by(tray_number=tray_number, user_id=current_user.id)\
                                      .order_by(LarvaeData.timestamp.asc())\
                                      .all()

        if not tray_entries:
            return jsonify({"error": f"No data found for tray {tray_number}"}), 404

        # Build growth data - one point per timestamp group
        growth_data = {"days": [], "length": [], "weight": []}
        all_larvae_data = []
        
        start_time = tray_entries[0].timestamp
        
        # Group by exact timestamp so multiple larvae from the same capture stay together
        grouped_data = {}
        for entry in tray_entries:
            grouped_data.setdefault(entry.timestamp, []).append(entry)
            all_larvae_data.append(entry)

        for timestamp in sorted(grouped_data.keys()):
            entries = grouped_data[timestamp]
            hours_elapsed = (timestamp - start_time).total_seconds() / 3600
            avg_length = sum(l.length for l in entries) / len(entries)
            avg_weight = sum(l.weight for l in entries) / len(entries)

            growth_data["days"].append(round(hours_elapsed, 1))
            growth_data["length"].append(round(avg_length, 1))
            growth_data["weight"].append(round(avg_weight, 3))

        latest_entries = grouped_data[sorted(grouped_data.keys())[-1]]
        if latest_entries:
            latest_metrics = {
                "length": round(sum(l.length for l in latest_entries) / len(latest_entries), 1),
                "width": round(sum(l.width for l in latest_entries) / len(latest_entries), 1),
                "area": round(sum(l.area for l in latest_entries) / len(latest_entries), 1),
                "weight": round(sum(l.weight for l in latest_entries) / len(latest_entries), 3),
                "count": sum(l.count for l in latest_entries)
            }
        else:
            latest_metrics = {"length": 0, "width": 0, "area": 0, "weight": 0, "count": 0}

        # Calculate weight distribution across ALL uploads
        weight_bins = {
            "0.08-0.10": 0, "0.10-0.12": 0, "0.12-0.14": 0,
            "0.14-0.16": 0, "0.16-0.18": 0, "0.18-0.20": 0, "0.20+": 0
        }

        for larva in all_larvae_data:
            weight = larva.weight
            if 0.08 <= weight < 0.10: weight_bins["0.08-0.10"] += 1
            elif 0.10 <= weight < 0.12: weight_bins["0.10-0.12"] += 1
            elif 0.12 <= weight < 0.14: weight_bins["0.12-0.14"] += 1
            elif 0.14 <= weight < 0.16: weight_bins["0.14-0.16"] += 1
            elif 0.16 <= weight < 0.18: weight_bins["0.16-0.18"] += 1
            elif 0.18 <= weight < 0.20: weight_bins["0.18-0.20"] += 1
            elif weight >= 0.20: weight_bins["0.20+"] += 1

        return jsonify({
            "metrics": latest_metrics,
            "growthData": growth_data,
            "weightDistribution": {
                "ranges": list(weight_bins.keys()),
                "counts": list(weight_bins.values())
            },
            "timestamp": latest_entries[0].timestamp.isoformat()
        })
    except Exception as e:
        app.logger.error(f"Error fetching tray data for tray {tray_number}: {e}")
        return jsonify({"error": str(e)}), 500
    

@app.route('/health')
def health_check():
    try:
        # Simple database check
        db.session.execute('SELECT 1')
        return {'status': 'healthy', 'database': 'connected'}, 200
    except Exception as e:
        return {'status': 'unhealthy', 'error': str(e)}, 500
    
# Add this route to debug stream clients
@app.route('/debug/stream_clients')
def debug_stream_clients():
    """Debug endpoint to monitor SSE clients"""
    client_info = {}
    
    with client_manager.lock:
        for client_id, info in client_manager.clients.items():
            client_info[client_id] = {
                'queue_size': info['queue'].qsize(),
                'created': info['created'],
                'last_active': info['last_active'],
                'age_seconds': time.time() - info['created']
            }
    
    return {
        'total_clients': len(client_manager.clients),
        'max_clients': client_manager.max_clients,
        'clients': client_info
    }
    

@app.route('/get_combined_tray_data')
@login_required
def get_combined_tray_data():
    """
    Fetches and processes combined data from all trays for the current user.
    Works with or without images - groups data by timestamp.
    """
    try:
        # Get all unique tray numbers
        tray_numbers = db.session.query(LarvaeData.tray_number)\
                                 .filter_by(user_id=current_user.id)\
                                 .distinct().all()
        tray_numbers = [t[0] for t in tray_numbers]
        
        if not tray_numbers:
            return jsonify({"error": "No data available"}), 404

        # Build growth data for each tray separately
        trays_growth_data = {}
        all_larvae = []
        
        for tray_num in tray_numbers:
            # Get all larvae data for this tray (ordered by timestamp)
            larvae_data = LarvaeData.query.filter_by(tray_number=tray_num, user_id=current_user.id)\
                                         .order_by(LarvaeData.timestamp.asc())\
                                         .all()
            
            if not larvae_data:
                continue
            
            all_larvae.extend(larvae_data)
            
            # Group larvae data by time (every measurement point is one data point)
            tray_growth = {"days": [], "weight": []}
            start_time = larvae_data[0].timestamp
            
            # Group by timestamp to avoid duplicates
            grouped_data = {}
            for larva in larvae_data:
                ts = larva.timestamp
                if ts not in grouped_data:
                    grouped_data[ts] = []
                grouped_data[ts].append(larva)
            
            for timestamp in sorted(grouped_data.keys()):
                larvae_at_time = grouped_data[timestamp]
                hours_elapsed = (timestamp - start_time).total_seconds() / 3600
                avg_weight = sum(l.weight for l in larvae_at_time) / len(larvae_at_time)
                
                tray_growth["days"].append(round(hours_elapsed, 1))
                tray_growth["weight"].append(round(avg_weight, 3))
            
            # Only include trays with at least 2 data points for growth trend
            if len(tray_growth["days"]) >= 2:
                trays_growth_data[str(tray_num)] = tray_growth

        # Calculate combined metrics from ALL larvae
        if all_larvae:
            combined_metrics = {
                "length": round(sum(l.length for l in all_larvae) / len(all_larvae), 1),
                "width": round(sum(l.width for l in all_larvae) / len(all_larvae), 1),
                "area": round(sum(l.area for l in all_larvae) / len(all_larvae), 1),
                "weight": round(sum(l.weight for l in all_larvae) / len(all_larvae), 3),
                "count": len(all_larvae)
            }
        else:
            combined_metrics = {"length": 0, "width": 0, "area": 0, "weight": 0, "count": 0}

        # Calculate weight distribution from all larvae
        weight_bins = {
            "0.08-0.10": 0, "0.10-0.12": 0, "0.12-0.14": 0,
            "0.14-0.16": 0, "0.16-0.18": 0, "0.18-0.20": 0, "0.20+": 0
        }

        for larva in all_larvae:
            weight = larva.weight
            if 0.08 <= weight < 0.10: weight_bins["0.08-0.10"] += 1
            elif 0.10 <= weight < 0.12: weight_bins["0.10-0.12"] += 1
            elif 0.12 <= weight < 0.14: weight_bins["0.12-0.14"] += 1
            elif 0.14 <= weight < 0.16: weight_bins["0.14-0.16"] += 1
            elif 0.16 <= weight < 0.18: weight_bins["0.16-0.18"] += 1
            elif 0.18 <= weight < 0.20: weight_bins["0.18-0.20"] += 1
            elif weight >= 0.20: weight_bins["0.20+"] += 1

        return jsonify({
            "metrics": combined_metrics,
            "traysGrowthData": trays_growth_data,  # New: per-tray growth data
            "weightDistribution": {
                "ranges": list(weight_bins.keys()),
                "counts": list(weight_bins.values())
            },
            "timestamp": datetime.utcnow().isoformat()
        })
    except Exception as e:
        app.logger.error(f"Error fetching combined tray data: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/compare')
@login_required
def compare_page():
    """Render the comparison page"""
    return render_template('compare.html', username=current_user.username)


@app.route('/api/compare_trays')
@login_required
def compare_trays():
    """
    Get comparison data: latest average weight for each unique tray.
    Returns data for scatter plot.
    """
    try:
        # Get all unique tray numbers
        tray_numbers = db.session.query(ImageFile.tray_number)\
                                 .filter_by(user_id=current_user.id)\
                                 .distinct().all()
        tray_numbers = [t[0] for t in tray_numbers]
        
        if not tray_numbers:
            return jsonify({"error": "No data available"}), 404
        
        comparison_data = []
        
        for tray_num in tray_numbers:
            # Get the LATEST image upload for this tray
            latest_image = ImageFile.query.filter_by(
                tray_number=tray_num,
                user_id=current_user.id
            ).order_by(ImageFile.timestamp.desc()).first()
            
            if not latest_image:
                continue
            
            # Get larvae data for this latest upload
            from datetime import timedelta
            time_window_start = latest_image.timestamp - timedelta(minutes=1)
            time_window_end = latest_image.timestamp + timedelta(minutes=1)
            
            larvae = LarvaeData.query.filter_by(
                tray_number=tray_num,
                user_id=current_user.id
            ).filter(
                LarvaeData.timestamp >= time_window_start,
                LarvaeData.timestamp <= time_window_end
            ).all()
            
            if larvae:
                avg_weight = sum(l.weight for l in larvae) / len(larvae)
                avg_length = sum(l.length for l in larvae) / len(larvae)
                count = len(larvae)
                
                comparison_data.append({
                    "tray_number": tray_num,
                    "avg_weight": round(avg_weight, 3),
                    "avg_length": round(avg_length, 1),
                    "count": count,
                    "timestamp": latest_image.timestamp.isoformat()
                })
        
        return jsonify({"trays": comparison_data})
        
    except Exception as e:
        app.logger.error(f"Error in compare_trays: {e}")
        return jsonify({"error": str(e)}), 500

        for entry in all_larvae:
            weight = entry.weight
            if 0.08 <= weight < 0.10: weight_bins["0.08-0.10"] += 1
            elif 0.10 <= weight < 0.12: weight_bins["0.10-0.12"] += 1
            elif 0.12 <= weight < 0.14: weight_bins["0.12-0.14"] += 1
            elif 0.14 <= weight < 0.16: weight_bins["0.14-0.16"] += 1
            elif 0.16 <= weight < 0.18: weight_bins["0.16-0.18"] += 1
            elif 0.18 <= weight < 0.20: weight_bins["0.18-0.20"] += 1
            elif weight >= 0.20: weight_bins["0.20+"] += 1

        return jsonify({
            "metrics": combined_metrics,
            "growthData": growth_data,
            "weightDistribution": {
                "ranges": list(weight_bins.keys()),
                "counts": list(weight_bins.values())
            },
            "timestamp": datetime.utcnow().isoformat()
        })
    except Exception as e:
        app.logger.error(f"Error fetching combined tray data: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/get_comparison_data')
@login_required
def get_comparison_data():
    """
    Fetches data for all trays belonging to the current user to allow comparison on the dashboard.
    Includes latest metrics, growth data, and all individual weights for distribution.
    """
    try:
        trays_data_for_comparison = {}

        # Dynamically get all unique tray numbers from the database for the current user
        unique_trays = LarvaeData.query.with_entities(LarvaeData.tray_number)\
                                       .filter_by(user_id=current_user.id).distinct().all()

        # Iterate through each unique tray number found
        for (tray_num,) in unique_trays:
            # Fetch all historical data for the current tray and user, ordered by timestamp
            all_tray_data = LarvaeData.query.filter_by(tray_number=tray_num, user_id=current_user.id)\
                                          .order_by(LarvaeData.timestamp.asc())\
                                          .all()

            if not all_tray_data:
                # If a tray has no data, include empty data for it so the frontend can handle it
                trays_data_for_comparison[str(tray_num)] = {
                    'latest': {'length': 0.0, 'width': 0.0, 'area': 0.0, 'weight': 0.0, 'count': 0},
                    'growthData': {'days': [], 'length': [], 'weight': []},
                    'allWeights': [] # Empty list for weight distribution if no data
                }
                continue # Move to the next tray

            # --- Process Growth Data for this Tray: Get latest measurement per day ---
            growth_data_for_tray = {
                "days": [],
                "length": [],
                "weight": []
            }
            daily_latest_data = {} # Key: day_number, Value: LarvaeData entry (the latest for that day)

            start_date = all_tray_data[0].timestamp.date() # Start date for this specific tray

            for entry in all_tray_data:
                day_number = (entry.timestamp.date() - start_date).days + 1
                # Overwrite with the current entry; since data is sorted ascending,
                # the last entry for a specific day will be the latest.
                daily_latest_data[day_number] = entry

            # Populate growth_data lists from the processed daily data
            for day in sorted(daily_latest_data.keys()):
                entry = daily_latest_data[day]
                growth_data_for_tray["days"].append(round(day, 1))
                growth_data_for_tray["length"].append(round(entry.length, 1))
                growth_data_for_tray["weight"].append(round(entry.weight, 1))

            # --- Process All Individual Weights for this Tray's Distribution ---
            all_individual_weights = [entry.weight for entry in all_tray_data]

            # --- Get Latest Metrics for this Tray ---
            latest_entry = all_tray_data[-1]
            
            # --- Calculate Total Count (sum of all individual entries) ---
            total_count = sum(entry.count for entry in all_tray_data)

            trays_data_for_comparison[str(tray_num)] = {
                'latest': {
                    'length': round(latest_entry.length, 1),
                    'width': round(latest_entry.width, 1),
                    'area': round(latest_entry.area, 1),
                    'weight': round(latest_entry.weight, 3),  # Show 3 decimals
                    'count': total_count  # Use sum instead of latest_entry.count
                },
                'growthData': growth_data_for_tray,
                'allWeights': all_individual_weights # This is the key for comparison weight distribution
            }

        return jsonify({
            'trays': trays_data_for_comparison,
            'timestamp': datetime.utcnow().isoformat()
        })
    except Exception as e:
        # Log the error for debugging purposes
        app.logger.error(f"Error in get_comparison_data: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/dashboard')
@login_required
def dashboard():
    """Renders the main dashboard page."""
    # Build tray list from larvae measurements so the dashboard works without images
    tray_numbers = db.session.query(LarvaeData.tray_number)\
                             .filter_by(user_id=current_user.id)\
                             .distinct()\
                             .all()
    tray_numbers = [t[0] for t in tray_numbers]

    tray_data_for_template = {}
    for tray_number in tray_numbers:
        tray_entries = LarvaeData.query.filter_by(tray_number=tray_number, user_id=current_user.id)\
                                       .order_by(LarvaeData.timestamp.asc())\
                                       .all()
        if not tray_entries:
            continue

        latest_entry = tray_entries[-1]
        tray_data_for_template[str(tray_number)] = {
            'tray_number': tray_number,
            'image_id': None,
            'timestamp': latest_entry.timestamp.isoformat(),
            'count': sum(entry.count for entry in tray_entries)
        }

    return render_template('dashboard.html', tray_data=tray_data_for_template)



# @app.route('/stream')
# def event_stream():
#     """Memory-safe Server-Sent Events endpoint - FIXED NON-BLOCKING VERSION"""
#     # Generate a unique client ID
#     client_id = request.args.get('client_id', f"client_{uuid.uuid4().hex[:8]}_{int(time.time())}")
    
#     def generate():
#         # Get a bounded queue from the client manager
#         client_queue = client_manager.add_client(client_id)
        
#         try:
#             # Send initial connection message
#             yield f"data: {json.dumps({'type': 'connected', 'message': 'Stream started', 'client_id': client_id})}\n\n"
            
#             # Use a short timeout to prevent gunicorn worker blocking
#             heartbeat_interval = 25  # Render timeout is 30 seconds, use 25 to be safe
#             last_heartbeat = time.time()
            
#             while True:
#                 try:
#                     # NON-BLOCKING: Check for data with short timeout
#                     try:
#                         # Use short timeout instead of blocking forever
#                         data = client_queue.get(timeout=60)  # 1 second timeout
#                         yield f"data: {json.dumps(data)}\n\n"
#                         last_heartbeat = time.time()
#                         continue
#                     except queue.Empty:
#                         # No data available - check if we need heartbeat
#                         pass
                    
#                     # Send heartbeat to keep connection alive and prevent timeouts
#                     current_time = time.time()
#                     if current_time - last_heartbeat > heartbeat_interval:
#                         yield f"data: {json.dumps({'type': 'heartbeat', 'timestamp': current_time})}\n\n"
#                         last_heartbeat = current_time
                    
#                 except (GeneratorExit, BrokenPipeError, ConnectionResetError):
#                     # Client disconnected normally
#                     break
#                 except Exception as e:
#                     print(f"⚠️ Stream error for {client_id}: {str(e)}")
#                     break
                    
#         finally:
#             # ALWAYS clean up when generator exits
#             client_manager.remove_client(client_id)
    
#     return Response(
#         generate(),
#         mimetype='text/event-stream',
#         headers={
#             'Cache-Control': 'no-cache',
#             'X-Accel-Buffering': 'no',
#             'Content-Type': 'text/event-stream; charset=utf-8'
#         }
#     )


@app.route('/stream')
def event_stream():
    """Memory-safe SSE with aggressive timeout for Render"""
    client_id = request.args.get('client_id', f"client_{uuid.uuid4().hex[:8]}_{int(time.time())}")
    
    def generate():
        client_queue = client_manager.add_client(client_id)
        
        try:
            # Send initial connection
            yield f"data: {json.dumps({'type': 'connected', 'message': 'Stream started', 'client_id': client_id})}\n\n"
            
            # CRITICAL: Use shorter timeout for Render's 512MB limit
            heartbeat_interval = 20  # Reduced from 25
            max_connection_time = 300  # 5 minutes max connection time
            connection_start = time.time()
            last_heartbeat = time.time()
            
            while True:
                # CRITICAL: Force disconnect after max_connection_time
                if time.time() - connection_start > max_connection_time:
                    print(f"⏰ Force disconnecting client {client_id} after {max_connection_time}s")
                    break
                
                try:
                    # Use shorter timeout
                    data = client_queue.get(timeout=0.5)  # Changed from 60 to 0.5
                    yield f"data: {json.dumps(data)}\n\n"
                    last_heartbeat = time.time()
                    continue
                except queue.Empty:
                    pass
                
                # Send heartbeat
                current_time = time.time()
                if current_time - last_heartbeat > heartbeat_interval:
                    yield f"data: {json.dumps({'type': 'heartbeat', 'timestamp': current_time})}\n\n"
                    last_heartbeat = current_time
                
        except (GeneratorExit, BrokenPipeError, ConnectionResetError):
            pass
        finally:
            client_manager.remove_client(client_id)
    
    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Content-Type': 'text/event-stream; charset=utf-8'
        }
    )


@app.errorhandler(RuntimeError)
def handle_runtime_error(e):
    if "working outside of request context" in str(e).lower():
        print("Request context error - likely in stream endpoint")
        return "Internal server error", 500
    raise e

@app.route('/logout')
@login_required
def logout():
    """Logs out the current user."""
    logout_user()
    flash('You have been logged out', 'success')
    return redirect(url_for('login'))


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
@login_required
def catch_all(path):
    """Catch all routes and redirect to dashboard for client-side routing"""
    return redirect(url_for('dashboard'))


# Add MQTT callbacks (after your routes)
def on_connect(client, userdata, flags, rc, properties):
    """Callback function for when the MQTT client connects to the broker."""
    if rc == 0:
        print("✅ MQTT Connected to Broker!")
        client.subscribe(MQTT_TOPIC)
        print(f"Subscribed to topic: {MQTT_TOPIC}")
    else:
        print(f"❌ MQTT Connection failed: {rc}")

# def on_message(client, userdata, msg):
#     """Callback function for when an MQTT message is received."""
#     print(f"📨 MQTT received message on {msg.topic}")
#     try:
#         data = json.loads(msg.payload.decode('utf-8'))
        
#         tray_number = data.get("tray_number")
#         image_data_base64 = data.get("image_data_base64")
#         bounding_boxes = data.get("bounding_boxes")
#         masks = data.get("masks")

#         # Validate incoming data
#         required_keys = ["tray_number", "length", "width", "area", "weight", "count"]
#         if not all(key in data for key in required_keys):
#             print("❌ Missing required keys in MQTT message")
#             return

#         # Use Flask's app context
#         with app.app_context():
#             try:
#                 # Save larvae data
#                 new_entry = LarvaeData(
#                     tray_number=data["tray_number"],
#                     length=data["length"],
#                     width=data["width"],
#                     area=data["area"],
#                     weight=data["weight"],
#                     count=data["count"],
#                     timestamp=datetime.now(timezone.utc)
#                 )
#                 db.session.add(new_entry)
                
#                 # Save image if present
#                 if image_data_base64:
#                     try:
#                         image_bytes = base64.b64decode(image_data_base64)
#                         img = Image.open(BytesIO(image_bytes))
#                         image_format = img.format.lower() if img.format else 'jpeg'
#                         image_size = len(image_bytes)
                        
#                         # Compress if too large
#                         if image_size > 2 * 1024 * 1024:
#                             output = BytesIO()
#                             img.save(output, format='JPEG', quality=85, optimize=True)
#                             image_bytes = output.getvalue()
#                             image_size = len(image_bytes)
#                             image_format = 'jpeg'
                        
#                         new_image_file = ImageFile(
#                             tray_number=tray_number,
#                             image_data=image_bytes,
#                             image_format=image_format,
#                             image_size=image_size,
#                             avg_length=data.get('avg_length'),
#                             avg_weight=data.get('avg_weight'),
#                             count=data.get('count'),
#                             bounding_boxes=json.dumps(bounding_boxes) if bounding_boxes else None,
#                             masks=json.dumps(masks) if masks else None,
#                             timestamp=datetime.now(timezone.utc)
#                         )
#                         db.session.add(new_image_file)
#                         print(f"✅ Image saved via MQTT for Tray {tray_number}")
                        
#                     except Exception as img_error:
#                         print(f"❌ Image processing error: {img_error}")
#                         # Continue without image

#                 db.session.commit()
#                 print(f"✅ MQTT data saved for Tray {tray_number}")

#             except Exception as e:
#                 db.session.rollback()
#                 print(f"❌ Database error in MQTT: {e}")
#             finally:
#                 db.session.remove()

#     except json.JSONDecodeError:
#         print(f"❌ JSON decode error in MQTT message")
#     except Exception as e:
#         print(f"❌ MQTT message processing error: {e}")


# def on_message(client, userdata, msg):
#     """Callback function for when an MQTT message is received."""
#     print(f"📨 MQTT received message on {msg.topic}")
#     try:
#         data = json.loads(msg.payload.decode('utf-8'))
        
#         tray_number = data.get("tray_number")
#         image_data_base64 = data.get("image_data_base64")
#         bounding_boxes = data.get("bounding_boxes")
#         masks = data.get("masks")

#         # Validate incoming data
#         required_keys = ["tray_number", "length", "width", "area", "weight", "count"]
#         if not all(key in data for key in required_keys):
#             print("❌ Missing required keys in MQTT message")
#             return

#         # Use Flask's app context
#         with app.app_context():
#             try:
#                 # Save larvae data
#                 new_entry = LarvaeData(
#                     tray_number=data["tray_number"],
#                     length=data["length"],
#                     width=data["width"],
#                     area=data["area"],
#                     weight=data["weight"],
#                     count=data["count"],
#                     timestamp=datetime.now(timezone.utc)
#                 )
#                 db.session.add(new_entry)
                
#                 # Save image if present
#                 image_saved = False
#                 if image_data_base64:
#                     try:
#                         image_bytes = base64.b64decode(image_data_base64)
#                         img = Image.open(BytesIO(image_bytes))
#                         image_format = img.format.lower() if img.format else 'jpeg'
#                         image_size = len(image_bytes)
                        
#                         # Compress if too large
#                         if image_size > 2 * 1024 * 1024:
#                             output = BytesIO()
#                             img.save(output, format='JPEG', quality=85, optimize=True)
#                             image_bytes = output.getvalue()
#                             image_size = len(image_bytes)
#                             image_format = 'jpeg'
                        
#                         new_image_file = ImageFile(
#                             tray_number=tray_number,
#                             image_data=image_bytes,
#                             image_format=image_format,
#                             image_size=image_size,
#                             avg_length=data.get('avg_length'),
#                             avg_weight=data.get('avg_weight'),
#                             count=data.get('count'),
#                             bounding_boxes=json.dumps(bounding_boxes) if bounding_boxes else None,
#                             masks=json.dumps(masks) if masks else None,
#                             timestamp=datetime.now(timezone.utc)
#                         )
#                         db.session.add(new_image_file)
#                         image_saved = True
#                         print(f"✅ Image saved via MQTT for Tray {tray_number}")
                        
#                     except Exception as img_error:
#                         print(f"❌ Image processing error: {img_error}")
#                         # Continue without image

#                 db.session.commit()
#                 print(f"✅ MQTT data saved for Tray {tray_number}")

#                 # BROADCAST UPDATE TO ALL CONNECTED CLIENTS
#                 update_data = {
#                     'type': 'new_data',
#                     'tray_number': tray_number,
#                     'timestamp': datetime.now(timezone.utc).isoformat(),
#                     'image_saved': image_saved,
#                     'metrics': {
#                         'length': data["length"],
#                         'width': data["width"],
#                         'area': data["area"],
#                         'weight': data["weight"],
#                         'count': data["count"]
#                     }
#                 }
                
#                 # BROADCAST TO ALL STREAM CLIENTS - MAKE SURE THIS EXISTS
#                 for client_id, q in list(stream_clients.items()):
#                     try:
#                         q.put(data, timeout=1)
#                         print(f"✅ Sent to stream client {client_id}")
#                     except queue.Full:
#                         print(f"❌ Queue full for client {client_id}")
#                     except Exception as e:
#                         print(f"❌ Error sending to client {client_id}: {e}")
                
#                 # Broadcast to all connected clients
#                 for client in connected_clients[:]:  # Use slice copy to avoid modification during iteration
#                     try:
#                         client_queue, last_id = client
#                         client_queue.put(update_data)
#                     except Exception as e:
#                         print(f"❌ Error sending to client: {e}")
#                         connected_clients.remove(client)

#             except Exception as e:
#                 db.session.rollback()
#                 print(f"❌ Database error in MQTT: {e}")
#             finally:
#                 db.session.remove()

#     except json.JSONDecodeError:
#         print(f"❌ JSON decode error in MQTT message")
#     except Exception as e:
#         print(f"❌ MQTT message processing error: {e}")


def on_message(client, userdata, msg):
    """Callback function for when an MQTT message is received."""
    print(f"📨 MQTT received message on {msg.topic}")
    print("⚠️ MQTT data reception disabled - using API for individual weights")
    # MQTT now only used for monitoring, not saving data
    # API endpoint /api/upload handles all data storage with individual weights
    return
    
    # OLD CODE DISABLED - keeping for reference
    try:
        data = json.loads(msg.payload.decode('utf-8'))
        
        tray_number = data.get("tray_number")
        image_data_base64 = data.get("image_data_base64")
        bounding_boxes = data.get("bounding_boxes")
        masks = data.get("masks")

        # Validate incoming data
        required_keys = ["tray_number", "length", "width", "area", "weight", "count"]
        if not all(key in data for key in required_keys):
            print("❌ Missing required keys in MQTT message")
            return

        # Use Flask's app context
        with app.app_context():
            try:
                # Save larvae data
                new_entry = LarvaeData(
                    tray_number=data["tray_number"],
                    length=data["length"],
                    width=data["width"],
                    area=data["area"],
                    weight=data["weight"],
                    count=data["count"],
                    timestamp=datetime.now(timezone.utc)
                )
                db.session.add(new_entry)
                
                # Save image if present
                image_saved = False
                if image_data_base64:
                    try:
                        image_bytes = base64.b64decode(image_data_base64)
                        img = Image.open(BytesIO(image_bytes))
                        image_format = img.format.lower() if img.format else 'jpeg'
                        image_size = len(image_bytes)
                        
                        # Compress if too large
                        if image_size > 2 * 1024 * 1024:
                            output = BytesIO()
                            img.save(output, format='JPEG', quality=85, optimize=True)
                            image_bytes = output.getvalue()
                            image_size = len(image_bytes)
                            image_format = 'jpeg'
                        
                        new_image_file = ImageFile(
                            tray_number=tray_number,
                            image_data=image_bytes,
                            image_format=image_format,
                            image_size=image_size,
                            avg_length=data.get('avg_length'),
                            avg_weight=data.get('avg_weight'),
                            count=data.get('count'),
                            bounding_boxes=json.dumps(bounding_boxes) if bounding_boxes else None,
                            masks=json.dumps(masks) if masks else None,
                            timestamp=datetime.now(timezone.utc)
                        )
                        db.session.add(new_image_file)
                        image_saved = True
                        print(f"✅ Image saved via MQTT for Tray {tray_number}")
                        
                    except Exception as img_error:
                        print(f"❌ Image processing error: {img_error}")
                        # Continue without image

                db.session.commit()
                print(f"✅ MQTT data saved for Tray {tray_number}")

                # BROADCAST UPDATE TO ALL SSE CLIENTS
                update_data = {
                    'type': 'new_data',
                    'tray_number': tray_number,
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                    'image_saved': image_saved,
                    'metrics': {
                        'length': data["length"],
                        'width': data["width"],
                        'area': data["area"],
                        'weight': data["weight"],
                        'count': data["count"]
                    }
                }
                
                # Broadcast to all connected clients
                broadcast_to_clients(update_data)

            except Exception as e:
                db.session.rollback()
                print(f"❌ Database error in MQTT: {e}")
            finally:
                db.session.remove()

    except json.JSONDecodeError:
        print(f"❌ JSON decode error in MQTT message")
    except Exception as e:
        print(f"❌ MQTT message processing error: {e}")


def run_mqtt_subscriber():
    """Run MQTT subscriber with reconnection logic"""
    global mqtt_client
    
    while True:
        try:
            print("🚀 Starting MQTT subscriber...")
            mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            mqtt_client.on_connect = on_connect
            mqtt_client.on_message = on_message
            
            mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
            print("🔄 Starting MQTT loop...")
            mqtt_client.loop_forever()
            
        except Exception as e:
            print(f"❌ MQTT error: {e}")
            print("🔄 Reconnecting in 30 seconds...")
            time.sleep(30)

def start_mqtt_thread():
    """Start MQTT in a background thread"""
    global mqtt_thread
    try:
        mqtt_thread = threading.Thread(target=run_mqtt_subscriber)
        mqtt_thread.daemon = True  # Thread will be killed when main process exits
        mqtt_thread.start()
        print("✅ MQTT thread started successfully")
    except Exception as e:
        print(f"❌ Failed to start MQTT thread: {e}")

# # --- Main Execution Block ---
# if __name__ == '__main__':
#     # Initialize database
#     with app.app_context():
#         db.create_all()
#         # Create test user if none exists
#         if not User.query.filter_by(username='testuser').first():
#             admin_user = User(username='testuser')
#             admin_user.set_password('password')
#             db.session.add(admin_user)
#             db.session.commit()
#             print("Test user 'testuser' with password 'password' created.")

#     # Start MQTT in background thread
#     print("=== MQTT SETUP ===")
#     start_mqtt_thread()
    
#     # Wait a moment to see if MQTT connects
#     time.sleep(5)

#     # Get port from environment variable
#     port = int(os.environ.get('PORT', 8000))
#     debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    
#     print("Starting Flask application...")
#     app.run(host='0.0.0.0', port=port, debug=debug_mode, use_reloader=False)

# --- Main Execution Block ---
if __name__ == '__main__':
    # Initialize database safely (create tables only if they don't exist)
    with app.app_context():
        try:
            db.create_all()
            print("✅ Database tables checked/created")
            
            # Create test user if none exists
            if not User.query.filter_by(username='testuser').first():
                try:
                    admin_user = User(username='testuser')
                    admin_user.set_password('password')
                    db.session.add(admin_user)
                    db.session.commit()
                    print("✅ Test user created")
                except Exception as e:
                    db.session.rollback()
                    print(f"⚠️ Could not create test user: {e}")
        except Exception as e:
            print(f"⚠️ Database initialization warning: {e}")

    # Start MQTT in background thread
    print("=== MQTT SETUP ===")
    start_mqtt_thread()
    
    # Get port from environment variable
    port = int(os.environ.get('PORT', 8000))
    
    # For Render deployment
    if 'RENDER' in os.environ:
        # Production settings for Render
        app.run(host='0.0.0.0', port=port, debug=False)
    else:
        # Local development settings
        app.run(host='0.0.0.0', port=port, debug=True, use_reloader=False)