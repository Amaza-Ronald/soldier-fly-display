# BSFwebdashboard.py - Main Flask application for BSF Larvae Monitoring Dashboard

from flask import Flask, render_template, request, redirect, url_for, jsonify, session, flash, send_file, Response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required
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

import threading
import paho.mqtt.client as mqtt

import gc
import uuid
import collections
from collections import OrderedDict


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
    'pool_timeout': 30        # Timeout for getting connection
}
app.config['JSONIFY_PRETTYPRINT_REGULAR'] = False  # Reduce JSON size
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload


db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'


# def cleanup_memory():
#     """Clean up old connections and memory"""
#     while True:
#         time.sleep(300)  # Run every 5 minutes
#         try:
#             # Clean up old stream clients (older than 30 minutes)
#             current_time = time.time()
#             expired_clients = []
#             for client_id, q in list(stream_clients.items()):
#                 # Simple cleanup - remove if queue has been empty for a while
#                 if q.qsize() == 0:  # Simple check, you might want more sophisticated logic
#                     expired_clients.append(client_id)
            
#             for client_id in expired_clients:
#                 if client_id in stream_clients:
#                     stream_clients.pop(client_id)
            
#             # Run garbage collection
#             gc.collect()
            
#         except Exception as e:
#             print(f"Cleanup error: {e}")

def cleanup_memory():
    """Memory cleanup with aggressive garbage collection for Render"""
    while True:
        time.sleep(60)  # Run every 1 minute (more aggressive)
        try:
            # Clean up inactive SSE clients
            removed = client_manager.cleanup_inactive(max_age_seconds=120)
            
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

# connected_clients = [] # To track connected clients for SSE
# stream_clients = {}   # Map of client_id -> queue.Queue for SSE stream management

# # Add this function right after the stream_clients definition:
# def broadcast_to_clients(data):
#     """Broadcast data to all connected SSE clients"""
#     disconnected_clients = []
    
#     for client_id, q in list(stream_clients.items()):
#         try:
#             q.put(data, timeout=2)
#             print(f"📤 Sent to client {client_id}: {data.get('type', 'unknown')}")
#         except queue.Full:
#             print(f"⚠️ Queue full for client {client_id}")
#         except Exception as e:
#             print(f"❌ Error sending to client {client_id}: {e}")
#             disconnected_clients.append(client_id)
    
#     # Clean up disconnected clients
#     for client_id in disconnected_clients:
#         if client_id in stream_clients:
#             stream_clients.pop(client_id)


# --- Thread-safe SSE Client Management ---
class ClientManager:
    """Thread-safe manager for SSE clients to prevent memory leaks"""
    def __init__(self):
        self.clients = OrderedDict()
        self.lock = threading.Lock()
        self.max_clients = 50  # Prevent memory exhaustion
        self.max_queue_size = 10  # Prevent queue memory bloat
    
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
    password_hash = db.Column(db.Text, nullable=False)  # CHANGED: String → Text (unlimited length)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class ImageFile(db.Model):
    __tablename__ = "image_files"
    id = db.Column(db.Integer, primary_key=True)
    tray_number = db.Column(db.Integer, nullable=False)
    
    # Use LargeBinary for PostgreSQL BYTEA
    image_data = db.Column(db.LargeBinary, nullable=False)
    image_format = db.Column(db.String(10), nullable=False)
    image_size = db.Column(db.Integer, nullable=False)
    
    # Metadata
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    avg_length = db.Column(db.Float, nullable=True)
    avg_weight = db.Column(db.Float, nullable=True)
    count = db.Column(db.Integer, nullable=True)
    
    # Use Text for PostgreSQL (better for large JSON)
    bounding_boxes = db.Column(db.Text, nullable=True)
    masks = db.Column(db.Text, nullable=True)

    def __repr__(self):
        return f"<ImageFile Tray {self.tray_number} - {self.timestamp}>"

class LarvaeData(db.Model):
    __tablename__ = "larvae_data"
    id = db.Column(db.Integer, primary_key=True)
    tray_number = db.Column(db.Integer, nullable=False)
    length = db.Column(db.Float, nullable=False)
    width = db.Column(db.Float, nullable=False)
    area = db.Column(db.Float, nullable=False)
    weight = db.Column(db.Float, nullable=False)
    count = db.Column(db.Integer, nullable=False)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

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
        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            login_user(user)
            flash('Login successful!', 'success')
            return redirect(url_for('dashboard'))
        flash('Invalid username or password', 'danger')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    """Handles new user registration."""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if User.query.filter_by(username=username).first():
            flash('Username already exists', 'danger')
            return redirect(url_for('register'))

        try:
            user = User(username=username)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash('Registration successful! Please login.', 'success')
            return redirect(url_for('login'))
        except Exception as e:
            db.session.rollback()
            flash(f'Registration failed: {e}. Please try again.', 'danger')

    return render_template('register.html')

# # CHANGED: Add BLOB image serving routes like app.py
# @app.route('/image/<int:image_id>')
# @login_required
# def get_image(image_id):
#     """Serve image directly from database BLOB"""
#     try:
#         image_file = ImageFile.query.get_or_404(image_id)
        
#         # Return image with proper MIME type
#         return Response(
#             image_file.image_data,
#             mimetype=f'image/{image_file.image_format}',
#             headers={
#                 'Content-Length': image_file.image_size,
#                 'Cache-Control': 'public, max-age=3600'  # Cache for 1 hour
#             }
#         )
#     except Exception as e:
#         app.logger.error(f"Error serving image {image_id}: {e}")
#         return jsonify({"error": "Image not found"}), 404


@app.route('/image/<int:image_id>')
def get_image(image_id):
    try:
        image_file = ImageFile.query.get(image_id)
        if not image_file:
            return "Image not found", 404
            
        response = send_file(
            BytesIO(image_file.image_data), 
            mimetype=f'image/{image_file.image_format}'
        )
        
        # ADD CACHE HEADERS - Prevents multiple requests
        response.headers['Cache-Control'] = 'public, max-age=3600'  # Cache for 1 hour
        response.headers['Expires'] = (datetime.utcnow() + timedelta(hours=1)).strftime('%a, %d %b %Y %H:%M:%S GMT')
        
        return response
        
    except Exception as e:
        print(f"Error loading image {image_id}: {str(e)}")
        return "Error loading image", 500


@app.route('/image_thumbnail/<int:image_id>')
@login_required
def get_image_thumbnail(image_id):
    """Serve resized thumbnail from database BLOB"""
    try:
        image_file = ImageFile.query.get_or_404(image_id)
        
        # Create thumbnail
        img = Image.open(BytesIO(image_file.image_data))
        img.thumbnail((300, 300))  # Create thumbnail
        
        # Convert back to bytes
        output = BytesIO()
        img.save(output, format=image_file.image_format.upper())
        thumbnail_data = output.getvalue()
        
        return Response(
            thumbnail_data,
            mimetype=f'image/{image_file.image_format}',
            headers={
                'Content-Length': len(thumbnail_data),
                'Cache-Control': 'public, max-age=3600'
            }
        )
    except Exception as e:
        app.logger.error(f"Error serving thumbnail {image_id}: {e}")
        return jsonify({"error": "Thumbnail not found"}), 404

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
    """Get images from database - OPTIMIZED VERSION"""
    try:
        # Add query optimization
        from sqlalchemy.orm import load_only
        
        # Select only the columns we need
        query = ImageFile.query.options(
            load_only(
                ImageFile.id,
                ImageFile.tray_number,
                ImageFile.image_data,  # Keep this for base64 fallback
                ImageFile.image_format,
                ImageFile.timestamp,
                ImageFile.count,
                ImageFile.avg_length,
                ImageFile.avg_weight,
                ImageFile.bounding_boxes,
                ImageFile.masks,
                ImageFile.image_size
            )
        )
        
        if tray_number == 'all':
            images = query.order_by(ImageFile.timestamp.desc()).limit(20).all()
        else:
            images = query.filter_by(tray_number=int(tray_number))\
                         .order_by(ImageFile.timestamp.desc())\
                         .limit(20).all()
        
        image_list = []
        for img in images: 
            # KEEP BOTH: URL for compatibility AND base64 for performance
            image_url = url_for('get_image', image_id=img.id)
            thumbnail_url = url_for('get_image_thumbnail', image_id=img.id)
            
            image_list.append({
                "id": img.id,
                "tray": img.tray_number,
                "src": image_url,  # Keep original URL for compatibility
                "src_base64": None,  # Optional: add later if needed
                "thumbnail": thumbnail_url,
                "timestamp": img.timestamp.isoformat(),
                "count": img.count,
                "avgLength": img.avg_length,
                "avgWeight": img.avg_weight,
                "bounding_boxes": json.loads(img.bounding_boxes) if img.bounding_boxes else [],
                "masks": json.loads(img.masks) if img.masks else [],
                "size": img.image_size,
                "format": img.image_format
            })
        return jsonify(image_list)
    except Exception as e:
        print(f"Error fetching images: {e}")
        return jsonify([])



# Add this route to your existing Flask routes

@app.route('/api/upload', methods=['POST'])
#@login_required
def upload_image():
    """Handle image upload and store in database as BLOB"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        image_data = data.get('image_data')
        tray_number = data.get('tray_number')
        count = data.get('count', 0)
        avg_length = data.get('avg_length', 0)
        avg_weight = data.get('avg_weight', 0)
        bounding_boxes_json = data.get('bounding_boxes', '[]')
        masks_json = data.get('masks', '[]')
        
        if not image_data or not tray_number:
            return jsonify({"error": "Missing image data or tray number"}), 400

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
        
        # Create ImageFile entry with BLOB data
        try:
            new_image = ImageFile(
                tray_number=tray_number,
                image_data=image_binary,
                image_format=image_format,
                image_size=image_size,
                count=count,
                avg_length=avg_length,
                avg_weight=avg_weight,
                bounding_boxes=bounding_boxes_json,
                masks=masks_json
            )
            
            db.session.add(new_image)
            db.session.commit()

            # # ✅ NEW: BROADCAST IMAGE UPLOAD TO ALL CONNECTED CLIENTS
            # update_data = {
            #     'type': 'new_image',
            #     'tray_number': tray_number,
            #     'timestamp': datetime.now(timezone.utc).isoformat(),
            #     'image_id': new_image.id,
            #     'count': count,
            #     'avg_length': avg_length,
            #     'avg_weight': avg_weight
            # }
            
            # # Broadcast to all connected clients
            # for client in connected_clients[:]:
            #     try:
            #         client_queue, last_id = client
            #         client_queue.put(update_data)
            #     except Exception as e:
            #         print(f"❌ Error sending to client: {e}")
            #         connected_clients.remove(client)
            
            # return jsonify({
            #     "message": "Image saved to database successfully",
            #     "image_id": new_image.id,
            #     "size": image_size,
            #     "tray_number": tray_number
            # }), 200


            # ✅ NEW: BROADCAST IMAGE UPLOAD TO ALL CONNECTED CLIENTS
            update_data = {
                'type': 'new_image',
                'tray_number': tray_number,
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'image_id': new_image.id,
                'count': count,
                'avg_length': avg_length,
                'avg_weight': avg_weight
            }
            
            # Broadcast to all connected clients using broadcast_to_clients function
            broadcast_to_clients(update_data)
            
            return jsonify({
                "message": "Image saved to database successfully",
                "image_id": new_image.id,
                "size": image_size,
                "tray_number": tray_number
            }), 200

        except Exception as e:
            db.session.rollback()
            return jsonify({"error": "Database error: " + str(e)}), 500

    except Exception as e:
        print(f"Error during image upload: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/get_tray_data/<int:tray_number>')
@login_required
def get_tray_data(tray_number):
    """
    Fetches and processes historical data for a specific tray,
    including growth data and weight distribution.
    """
    try:
        # Get all historical data for the specified tray, ordered by timestamp
        tray_data = LarvaeData.query.filter_by(tray_number=tray_number)\
                                  .order_by(LarvaeData.timestamp.asc())\
                                  .all()

        if not tray_data:
            return jsonify({"error": f"No data found for tray {tray_number}"}), 404

        # Process growth data (latest entry per day)
        growth_data = {"days": [], "length": [], "weight": []}
        daily_data = {}

        if tray_data:
            start_date = tray_data[0].timestamp.date()
            for entry in tray_data:
                day_number = (entry.timestamp.date() - start_date).days + 1
                daily_data[day_number] = entry  # Keep latest entry per day

            for day in sorted(daily_data.keys()):
                entry = daily_data[day]
                growth_data["days"].append(day)
                growth_data["length"].append(round(entry.length, 1))
                growth_data["weight"].append(round(entry.weight, 1))

        # Get latest metrics for the tray
        latest_entry = tray_data[-1] if tray_data else None

        # Calculate weight distribution for all data in the tray
        weight_bins = {
            "80-90": 0, "90-100": 0, "100-110": 0,
            "110-120": 0, "120-130": 0, "130-140": 0, "140+": 0
        }

        for entry in tray_data:
            weight = entry.weight
            if 80 <= weight < 90: weight_bins["80-90"] += 1
            elif 90 <= weight < 100: weight_bins["90-100"] += 1
            elif 100 <= weight < 110: weight_bins["100-110"] += 1
            elif 110 <= weight < 120: weight_bins["110-120"] += 1
            elif 120 <= weight < 130: weight_bins["120-130"] += 1
            elif 130 <= weight < 140: weight_bins["130-140"] += 1
            else: weight_bins["140+"] += 1

        return jsonify({
            "metrics": {
                "length": round(latest_entry.length, 1) if latest_entry else 0,
                "width": round(latest_entry.width, 1) if latest_entry else 0,
                "area": round(latest_entry.area, 1) if latest_entry else 0,
                "weight": round(latest_entry.weight, 1) if latest_entry else 0,
                "count": latest_entry.count if latest_entry else 0
            },
            "growthData": growth_data,
            "weightDistribution": {
                "ranges": list(weight_bins.keys()),
                "counts": list(weight_bins.values())
            },
            "timestamp": latest_entry.timestamp.isoformat() if latest_entry else datetime.utcnow().isoformat()
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
    Fetches and processes combined data from all trays,
    providing overall metrics, growth, and weight distribution.
    """
    try:
        # Get all unique tray numbers
        tray_numbers = [result[0] for result in db.session.query(LarvaeData.tray_number).distinct().all()]

        if not tray_numbers:
            return jsonify({"error": "No tray data available"}), 404

        # Get all historical data from all trays
        all_data = []
        latest_entries = [] # To calculate combined latest metrics

        for tray_num in tray_numbers:
            tray_data = LarvaeData.query.filter_by(tray_number=tray_num).all()
            all_data.extend(tray_data)
            latest_entry = LarvaeData.query.filter_by(tray_number=tray_num)\
                                         .order_by(LarvaeData.timestamp.desc())\
                                         .first()
            if latest_entry:
                latest_entries.append(latest_entry)

        if not all_data:
            return jsonify({"error": "No data available"}), 404

        # Calculate combined latest metrics (average for length/width/area/weight, sum for count)
        combined_metrics = {
            "length": round(sum(e.length for e in latest_entries)/len(latest_entries), 1) if latest_entries else 0,
            "width": round(sum(e.width for e in latest_entries)/len(latest_entries), 1) if latest_entries else 0,
            "area": round(sum(e.area for e in latest_entries)/len(latest_entries), 1) if latest_entries else 0,
            "weight": round(sum(e.weight for e in latest_entries)/len(latest_entries), 1) if latest_entries else 0,
            "count": sum(e.count for e in latest_entries) if latest_entries else 0
        }

        # Calculate combined growth data (average per day across all trays)
        growth_data = {"days": [], "length": [], "weight": []}
        day_data = defaultdict(list) # Stores all entries for a given 'day'

        # Find the earliest timestamp among all data to set a consistent start day for combined growth
        earliest_timestamp = min(entry.timestamp for entry in all_data)

        for entry in all_data:
            day = (entry.timestamp.date() - earliest_timestamp.date()).days + 1
            day_data[day].append(entry)

        for day, entries in sorted(day_data.items()):
            growth_data["days"].append(day)
            growth_data["length"].append(round(sum(e.length for e in entries)/len(entries), 1))
            growth_data["weight"].append(round(sum(e.weight for e in entries)/len(entries), 1))

        # Calculate combined weight distribution for all data
        weight_bins = {
            "80-90": 0, "90-100": 0, "100-110": 0,
            "110-120": 0, "120-130": 0, "130-140": 0, "140+": 0
        }

        for entry in all_data:
            weight = entry.weight
            if 80 <= weight < 90: weight_bins["80-90"] += 1
            elif 90 <= weight < 100: weight_bins["90-100"] += 1
            elif 100 <= weight < 110: weight_bins["100-110"] += 1
            elif 110 <= weight < 120: weight_bins["110-120"] += 1
            elif 120 <= weight < 130: weight_bins["120-130"] += 1
            elif 130 <= weight < 140: weight_bins["130-140"] += 1
            else: weight_bins["140+"] += 1

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
    Fetches data for all trays to allow comparison on the dashboard.
    Includes latest metrics, growth data, and all individual weights for distribution.
    """
    try:
        trays_data_for_comparison = {}

        # Dynamically get all unique tray numbers from the database
        unique_trays = LarvaeData.query.with_entities(LarvaeData.tray_number).distinct().all()

        # Iterate through each unique tray number found
        for (tray_num,) in unique_trays:
            # Fetch all historical data for the current tray, ordered by timestamp
            all_tray_data = LarvaeData.query.filter_by(tray_number=tray_num)\
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

            trays_data_for_comparison[str(tray_num)] = {
                'latest': {
                    'length': round(latest_entry.length, 1),
                    'width': round(latest_entry.width, 1),
                    'area': round(latest_entry.area, 1),
                    'weight': round(latest_entry.weight, 1),
                    'count': latest_entry.count
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
    # Dynamically get all unique tray numbers from database
    unique_tray_numbers_raw = LarvaeData.query.with_entities(LarvaeData.tray_number).distinct().all()

    # Convert list of tuples to a sorted list of integers
    unique_tray_numbers = sorted([tray_num for (tray_num,) in unique_tray_numbers_raw])

    # Create a dictionary to hold the tray numbers to be passed to the template.
    # The actual data for each tray will be fetched by JavaScript via AJAX.
    tray_data_for_template = {}
    for tray_num in unique_tray_numbers:
        tray_data_for_template[tray_num] = {} # Empty dict, frontend only needs the keys

    return render_template('dashboard.html', tray_data=tray_data_for_template)


# --- Server-Sent Events (SSE) for Real-Time Updates ---
# --- Server-Sent Events (SSE) for Real-Time Updates ---
# @app.route('/stream')
# def event_stream():
#     client_id = request.args.get('client_id', str(time.time()))
    
#     def generate():
#         # Create a new queue for this client
#         q = queue.Queue()
#         stream_clients[client_id] = q
        
#         print(f"🎯 New SSE client connected: {client_id}")
        
#         try:
#             # Send initial connection message
#             yield f"data: {json.dumps({'type': 'connected', 'message': 'Stream started', 'client_id': client_id})}\n\n"
            
#             while True:
#                 try:
#                     # Wait for messages with timeout
#                     data = q.get(timeout=15)
#                     if data:
#                         yield f"data: {json.dumps(data)}\n\n"
#                 except queue.Empty:
#                     # Send heartbeat to keep connection alive
#                     yield f"data: {json.dumps({'type': 'heartbeat', 'timestamp': time.time()})}\n\n"
                    
#         except GeneratorExit:
#             # Client disconnected
#             print(f"🎯 Client disconnected: {client_id}")
#         except Exception as e:
#             print(f"🎯 Stream error for {client_id}: {str(e)}")
#         finally:
#             # Clean up
#             if client_id in stream_clients:
#                 stream_clients.pop(client_id)
#                 print(f"🎯 Removed client {client_id}")
    
#     return Response(
#         generate(),
#         mimetype='text/event-stream',
#         headers={
#             'Cache-Control': 'no-cache',
#             'Connection': 'keep-alive',
#             'X-Accel-Buffering': 'no'
#         }
#     )

# @app.route('/stream')
# def event_stream():
#     """Memory-safe Server-Sent Events endpoint - NON-BLOCKING VERSION"""
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
#             last_data_sent = time.time()
            
#             while True:
#                 try:
#                     # NON-BLOCKING: Check for data with very short timeout
#                     try:
#                         # Use get_nowait() or very short timeout to avoid blocking workers
#                         data = client_queue.get_nowait()  # NON-BLOCKING
#                         yield f"data: {json.dumps(data)}\n\n"
#                         last_data_sent = time.time()
#                         last_heartbeat = time.time()
#                         continue
#                     except queue.Empty:
#                         # No data available - continue to heartbeat check
#                         pass
                    
#                     # Send heartbeat to keep connection alive and prevent timeouts
#                     current_time = time.time()
#                     if current_time - last_heartbeat > heartbeat_interval:
#                         yield f"data: {json.dumps({'type': 'heartbeat', 'timestamp': current_time})}\n\n"
#                         last_heartbeat = current_time
                    
#                     # Small sleep to prevent CPU spinning but keep connection responsive
#                     # This yields control back to event loop
#                     time.sleep(0.1)  # 100ms sleep
                    
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
    """Memory-safe Server-Sent Events endpoint - FIXED NON-BLOCKING VERSION"""
    # Generate a unique client ID
    client_id = request.args.get('client_id', f"client_{uuid.uuid4().hex[:8]}_{int(time.time())}")
    
    def generate():
        # Get a bounded queue from the client manager
        client_queue = client_manager.add_client(client_id)
        
        try:
            # Send initial connection message
            yield f"data: {json.dumps({'type': 'connected', 'message': 'Stream started', 'client_id': client_id})}\n\n"
            
            # Use a short timeout to prevent gunicorn worker blocking
            heartbeat_interval = 25  # Render timeout is 30 seconds, use 25 to be safe
            last_heartbeat = time.time()
            
            while True:
                try:
                    # NON-BLOCKING: Check for data with short timeout
                    try:
                        # Use short timeout instead of blocking forever
                        data = client_queue.get(timeout=1.0)  # 1 second timeout
                        yield f"data: {json.dumps(data)}\n\n"
                        last_heartbeat = time.time()
                        continue
                    except queue.Empty:
                        # No data available - check if we need heartbeat
                        pass
                    
                    # Send heartbeat to keep connection alive and prevent timeouts
                    current_time = time.time()
                    if current_time - last_heartbeat > heartbeat_interval:
                        yield f"data: {json.dumps({'type': 'heartbeat', 'timestamp': current_time})}\n\n"
                        last_heartbeat = current_time
                    
                except (GeneratorExit, BrokenPipeError, ConnectionResetError):
                    # Client disconnected normally
                    break
                except Exception as e:
                    print(f"⚠️ Stream error for {client_id}: {str(e)}")
                    break
                    
        finally:
            # ALWAYS clean up when generator exits
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