from flask import Flask, render_template, request, redirect, url_for, jsonify, session, flash, send_file, Response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
from collections import defaultdict
import os
import threading # For running MQTT in a separate thread
import base64
from PIL import Image
from io import BytesIO
from random import uniform, randint

# MQTT specific imports
import paho.mqtt.client as mqtt
import json
import time # Although not heavily used, keep it if needed for future sleep operations

# --- Flask App Configuration ---
app = Flask(__name__, static_folder='static')
app.secret_key = os.urandom(24)
# Ensure this path matches the database path both parts will write to
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///larvae_monitoring.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# --- Database Models (UPDATED for BLOB consistency with app.py) ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

# CHANGED: Updated ImageFile model to use BLOB storage like app.py
class ImageFile(db.Model):
    __tablename__ = "image_files"
    id = db.Column(db.Integer, primary_key=True)
    tray_number = db.Column(db.Integer, nullable=False)
    
    # CHANGED: Use BLOB fields for image storage instead of file paths
    image_data = db.Column(db.LargeBinary, nullable=False)  # Actual image binary data
    image_format = db.Column(db.String(10), nullable=False)  # jpeg, png, etc.
    image_size = db.Column(db.Integer, nullable=False)       # Size in bytes
    
    # Metadata
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    avg_length = db.Column(db.Float, nullable=True)
    avg_weight = db.Column(db.Float, nullable=True)
    count = db.Column(db.Integer, nullable=True)
    
    # Classification data
    bounding_boxes = db.Column(db.String, nullable=True)  # JSON string
    masks = db.Column(db.String, nullable=True)          # JSON string

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
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

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

# CHANGED: Add BLOB image serving routes like app.py
@app.route('/image/<int:image_id>')
@login_required
def get_image(image_id):
    """Serve image directly from database BLOB"""
    try:
        image_file = ImageFile.query.get_or_404(image_id)
        
        # Return image with proper MIME type
        return Response(
            image_file.image_data,
            mimetype=f'image/{image_file.image_format}',
            headers={
                'Content-Length': image_file.image_size,
                'Cache-Control': 'public, max-age=3600'  # Cache for 1 hour
            }
        )
    except Exception as e:
        app.logger.error(f"Error serving image {image_id}: {e}")
        return jsonify({"error": "Image not found"}), 404

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

# CHANGED: Updated image API to work with BLOB storage
@app.route('/api/images/<tray_number>')
@login_required
def get_images(tray_number):
    """Get images from database with BLOB data converted to URLs"""
    try:
        if tray_number == 'all':
            images = ImageFile.query.order_by(ImageFile.timestamp.desc()).all()
        else:
            images = ImageFile.query.filter_by(tray_number=int(tray_number)).order_by(ImageFile.timestamp.desc()).all()
        
        image_list = []
        for img in images: 
            image_list.append({
                "id": img.id,
                "tray": img.tray_number,
                "src": url_for('get_image', image_id=img.id),  # Use BLOB route
                "thumbnail": url_for('get_image_thumbnail', image_id=img.id),  # Thumbnail URL
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

@app.route('/logout')
@login_required
def logout():
    """Logs out the current user."""
    logout_user()
    flash('You have been logged out', 'success')
    return redirect(url_for('login'))

# # --- MQTT Configuration ---
# MQTT_BROKER = "broker.hivemq.com"
# MQTT_PORT = 1883
# MQTT_TOPIC = "bsf_monitor/larvae_data" # IMPORTANT: This MUST match the topic in your data publisher!

# mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

# # CHANGED: Fixed MQTT callbacks to use BLOB storage and correct variable references
# def on_connect(client, userdata, flags, rc, properties):
#     """Callback function for when the MQTT client connects to the broker."""
#     if rc == 0:
#         print("Connected to MQTT Broker!")
#         client.subscribe(MQTT_TOPIC)
#         print(f"Subscribed to topic: {MQTT_TOPIC}")
#     else:
#         print(f"Failed to connect, return code {rc}\n")

# def on_message(client, userdata, msg):
#     """Callback function for when an MQTT message is received."""
#     print(f"Received message on topic {msg.topic}")
#     try:
#         data = json.loads(msg.payload.decode('utf-8'))

#         # CHANGED: Extract all variables from data, not undefined ones
#         tray_number = data.get("tray_number")
#         bounding_boxes = data.get("bounding_boxes")
#         masks = data.get("masks")
#         image_data_base64 = data.get("image_data_base64")

#         # Validate incoming data
#         required_keys = ["tray_number", "length", "width", "area", "weight", "count"]
#         if not all(key in data for key in required_keys):
#             print("Error: Received payload is missing required keys.")
#             return

#         # Use Flask's app context to interact with the database in the MQTT thread
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
#                     timestamp=datetime.utcnow()
#                 )
#                 db.session.add(new_entry)
                
#                 # CHANGED: Save image to database BLOB if present (no file system storage)
#                 if image_data_base64:
#                     # Decode base64 image
#                     image_bytes = base64.b64decode(image_data_base64)
                    
#                     # Get image format and size
#                     img = Image.open(BytesIO(image_bytes))
#                     image_format = img.format.lower() if img.format else 'jpeg'
#                     image_size = len(image_bytes)
                    
#                     # Compress image if too large (optional)
#                     if image_size > 2 * 1024 * 1024:  # 2MB
#                         output = BytesIO()
#                         img.save(output, format='JPEG', quality=85, optimize=True)
#                         image_bytes = output.getvalue()
#                         image_size = len(image_bytes)
#                         image_format = 'jpeg'
                    
#                     # Create new image record with BLOB data
#                     new_image_file = ImageFile(
#                         tray_number=tray_number,
#                         image_data=image_bytes,
#                         image_format=image_format,
#                         image_size=image_size,
#                         avg_length=data.get('avg_length'),
#                         avg_weight=data.get('avg_weight'),
#                         count=data.get('count'),
#                         bounding_boxes=json.dumps(bounding_boxes) if bounding_boxes else None,
#                         masks=json.dumps(masks) if masks else None
#                     )
#                     db.session.add(new_image_file)
#                     print(f"✅ Image saved to database BLOB for Tray {tray_number}, Size: {image_size} bytes")

#                 db.session.commit()
#                 print(f"✅ Data saved to database for Tray {tray_number}")

#             except Exception as e:
#                 db.session.rollback()
#                 print(f"❌ Error storing data to database: {e}")
#             finally:
#                 db.session.remove()

#     except json.JSONDecodeError:
#         print(f"Error: Could not decode JSON from message: {msg.payload.decode()}")
#     except Exception as e:
#         print(f"An unexpected error occurred in on_message: {e}")


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
@login_required
def catch_all(path):
    """Catch all routes and redirect to dashboard for client-side routing"""
    return redirect(url_for('dashboard'))


# # --- MQTT Thread Function 
# def run_mqtt_subscriber():
#     # Assign the callbacks
#     mqtt_client.on_connect = on_connect
#     mqtt_client.on_message = on_message

#     # Start the client loop
#     try:
#         mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
#         mqtt_client.loop_forever() # This is a blocking call
#     except Exception as e:
#         print(f"Failed to connect to MQTT broker or loop error: {e}")
#     finally:
#         print("MQTT subscriber stopped.")


# --- Main Execution Block ---
if __name__ == '__main__':
    # Initialize database
    with app.app_context():
        db.create_all()
        # Create test user if none exists
        if not User.query.filter_by(username='testuser').first():
            admin_user = User(username='testuser')
            admin_user.set_password('password')
            db.session.add(admin_user)
            db.session.commit()
            print("Test user 'testuser' with password 'password' created.")

    # # Debug MQTT startup
    # print("=== MQTT DEBUG ===")
    # print(f"RENDER environment: {os.environ.get('RENDER', 'Not set')}")
    # print("Starting MQTT thread...")
    
    # try:
    #     mqtt_thread = threading.Thread(target=run_mqtt_subscriber)
    #     mqtt_thread.daemon = True
    #     mqtt_thread.start()
    #     print("✅ MQTT thread started successfully")
    #     print(f"MQTT thread alive: {mqtt_thread.is_alive()}")
    # except Exception as e:
    #     print(f"❌ MQTT thread failed: {e}")

    # Get port from environment variable (Render provides this)
    port = int(os.environ.get('PORT', 8000))
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    
    print("Starting Flask application...")
    app.run(host='0.0.0.0', port=port, debug=debug_mode, use_reloader=False)





# from flask import Flask, render_template, request, redirect, url_for, jsonify, session, flash, send_file
# from flask_sqlalchemy import SQLAlchemy
# from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required
# from werkzeug.security import generate_password_hash, check_password_hash
# from datetime import datetime, timedelta
# from collections import defaultdict
# import os
# import threading # For running MQTT in a separate thread
# import base64
# from PIL import Image
# from io import BytesIO
# from random import uniform, randint

# # MQTT specific imports
# import paho.mqtt.client as mqtt
# import json
# import time # Although not heavily used, keep it if needed for future sleep operations

# # --- Flask App Configuration ---
# app = Flask(__name__, static_folder='static')
# app.secret_key = os.urandom(24)
# # Ensure this path matches the database path both parts will write to
# app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///larvae_monitoring.db'
# app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False


# db = SQLAlchemy(app)
# login_manager = LoginManager(app)
# login_manager.login_view = 'login'

# # --- Database Models (From app.py) ---
# class User(UserMixin, db.Model):
#     id = db.Column(db.Integer, primary_key=True)
#     username = db.Column(db.String(50), unique=True, nullable=False)
#     password_hash = db.Column(db.String(128), nullable=False)
#     created_at = db.Column(db.DateTime, default=datetime.utcnow)

#     def set_password(self, password):
#         self.password_hash = generate_password_hash(password)

#     def check_password(self, password):
#         return check_password_hash(self.password_hash, password)

# class ImageFile(db.Model):
#     __tablename__ = "image_files"
#     id = db.Column(db.Integer, primary_key=True)
#     tray_number = db.Column(db.Integer, nullable=False)
#     file_path = db.Column(db.String(255), nullable=False)
#     timestamp = db.Column(db.DateTime, default=datetime.utcnow)
#     avg_length = db.Column(db.Float, nullable=True)
#     avg_weight = db.Column(db.Float, nullable=True)
#     count = db.Column(db.Integer, nullable=True)
#     # New fields to store classification data
#     bounding_boxes = db.Column(db.String, nullable=True) # Stored as JSON string
#     masks = db.Column(db.String, nullable=True) # Stored as JSON string

# class LarvaeData(db.Model):
#     # Explicitly set table name for consistency, matching the original mqtt_subscriber.py's table name
#     __tablename__ = "larvae_data"
#     id = db.Column(db.Integer, primary_key=True)
#     tray_number = db.Column(db.Integer, nullable=False)
#     length = db.Column(db.Float, nullable=False)
#     width = db.Column(db.Float, nullable=False)
#     area = db.Column(db.Float, nullable=False)
#     weight = db.Column(db.Float, nullable=False)
#     count = db.Column(db.Integer, nullable=False)
#     timestamp = db.Column(db.DateTime, default=datetime.utcnow)

#     def __repr__(self):
#         return f"<LarvaeData Tray {self.tray_number} - {self.timestamp}>"

# @login_manager.user_loader
# def load_user(user_id):
#     return User.query.get(int(user_id))

# # --- Helper Functions (From app.py) ---
# def get_latest_tray_data(tray_number):
#     """Fetches the most recent larvae data entry for a specific tray."""
#     return LarvaeData.query.filter_by(tray_number=tray_number).order_by(LarvaeData.timestamp.desc()).first()

# def calculate_weight_distribution_backend(weights_array):
#     """Calculates the distribution of larvae weights into predefined bins."""
#     weight_bins = {
#         "80-90": 0, "90-100": 0, "100-110": 0,
#         "110-120": 0, "120-130": 0, "130-140": 0, "140+": 0
#     }
#     for weight in weights_array:
#         if 80 <= weight < 90: weight_bins["80-90"] += 1
#         elif 90 <= weight < 100: weight_bins["90-100"] += 1
#         elif 100 <= weight < 110: weight_bins["100-110"] += 1
#         elif 110 <= weight < 120: weight_bins["110-120"] += 1
#         elif 120 <= weight < 130: weight_bins["120-130"] += 1
#         elif 130 <= weight < 140: weight_bins["130-140"] += 1
#         else: weight_bins["140+"] += 1
#     return list(weight_bins.keys()), list(weight_bins.values())

# # --- Flask Routes (From app.py) ---
# @app.route('/')
# def home():
#     """Redirects the root URL to the login page."""
#     return redirect(url_for('login'))

# @app.route('/login', methods=['GET', 'POST'])
# def login():
#     """Handles user login."""
#     if request.method == 'POST':
#         username = request.form.get('username')
#         password = request.form.get('password')
#         user = User.query.filter_by(username=username).first()

#         if user and user.check_password(password):
#             login_user(user)
#             flash('Login successful!', 'success')
#             return redirect(url_for('dashboard'))
#         flash('Invalid username or password', 'danger')
#     return render_template('login.html')

# @app.route('/register', methods=['GET', 'POST'])
# def register():
#     """Handles new user registration."""
#     if request.method == 'POST':
#         username = request.form.get('username')
#         password = request.form.get('password')

#         if User.query.filter_by(username=username).first():
#             flash('Username already exists', 'danger')
#             return redirect(url_for('register'))

#         try:
#             user = User(username=username)
#             user.set_password(password)
#             db.session.add(user)
#             db.session.commit()
#             flash('Registration successful! Please login.', 'success')
#             return redirect(url_for('login'))
#         except Exception as e:
#             db.session.rollback()
#             flash(f'Registration failed: {e}. Please try again.', 'danger')

#     return render_template('register.html')

# @app.route('/get_tray_data/<int:tray_number>')
# @login_required
# def get_tray_data(tray_number):
#     """
#     Fetches and processes historical data for a specific tray,
#     including growth data and weight distribution.
#     """
#     try:
#         # Get all historical data for the specified tray, ordered by timestamp
#         tray_data = LarvaeData.query.filter_by(tray_number=tray_number)\
#                                   .order_by(LarvaeData.timestamp.asc())\
#                                   .all()

#         if not tray_data:
#             return jsonify({"error": f"No data found for tray {tray_number}"}), 404

#         # Process growth data (latest entry per day)
#         growth_data = {"days": [], "length": [], "weight": []}
#         daily_data = {}

#         if tray_data:
#             start_date = tray_data[0].timestamp.date()
#             for entry in tray_data:
#                 day_number = (entry.timestamp.date() - start_date).days + 1
#                 daily_data[day_number] = entry  # Keep latest entry per day

#             for day in sorted(daily_data.keys()):
#                 entry = daily_data[day]
#                 growth_data["days"].append(day)
#                 growth_data["length"].append(round(entry.length, 1))
#                 growth_data["weight"].append(round(entry.weight, 1))

#         # Get latest metrics for the tray
#         latest_entry = tray_data[-1] if tray_data else None

#         # Calculate weight distribution for all data in the tray
#         weight_bins = {
#             "80-90": 0, "90-100": 0, "100-110": 0,
#             "110-120": 0, "120-130": 0, "130-140": 0, "140+": 0
#         }

#         for entry in tray_data:
#             weight = entry.weight
#             if 80 <= weight < 90: weight_bins["80-90"] += 1
#             elif 90 <= weight < 100: weight_bins["90-100"] += 1
#             elif 100 <= weight < 110: weight_bins["100-110"] += 1
#             elif 110 <= weight < 120: weight_bins["110-120"] += 1
#             elif 120 <= weight < 130: weight_bins["120-130"] += 1
#             elif 130 <= weight < 140: weight_bins["130-140"] += 1
#             else: weight_bins["140+"] += 1

#         return jsonify({
#             "metrics": {
#                 "length": round(latest_entry.length, 1) if latest_entry else 0,
#                 "width": round(latest_entry.width, 1) if latest_entry else 0,
#                 "area": round(latest_entry.area, 1) if latest_entry else 0,
#                 "weight": round(latest_entry.weight, 1) if latest_entry else 0,
#                 "count": latest_entry.count if latest_entry else 0
#             },
#             "growthData": growth_data,
#             "weightDistribution": {
#                 "ranges": list(weight_bins.keys()),
#                 "counts": list(weight_bins.values())
#             },
#             "timestamp": latest_entry.timestamp.isoformat() if latest_entry else datetime.utcnow().isoformat()
#         })
#     except Exception as e:
#         app.logger.error(f"Error fetching tray data for tray {tray_number}: {e}")
#         return jsonify({"error": str(e)}), 500



# # UPDATED: Image API route for BLOB storage
# @app.route('/api/images/<tray_number>')
# @login_required
# def get_images(tray_number):
#     """Get images from database with BLOB data converted to URLs"""
#     try:
#         if 'json' not in globals():
#             import json
            
#         if tray_number == 'all':
#             images = ImageFile.query.order_by(ImageFile.timestamp.desc()).all()
#         else:
#             images = ImageFile.query.filter_by(tray_number=int(tray_number)).order_by(ImageFile.timestamp.desc()).all()
        
#         image_list = []
#         for img in images: 
#             image_list.append({
#                 "id": img.id,  # NEW: Include image ID
#                 "tray": img.tray_number,
#                 "src": url_for('get_image', image_id=img.id),  # UPDATED: Use BLOB route
#                 "thumbnail": url_for('get_image_thumbnail', image_id=img.id),  # NEW: Thumbnail URL
#                 "timestamp": img.timestamp.isoformat(),
#                 "count": img.count,
#                 "avgLength": img.avg_length,
#                 "avgWeight": img.avg_weight,
#                 "bounding_boxes": json.loads(img.bounding_boxes) if img.bounding_boxes else [],
#                 "masks": json.loads(img.masks) if img.masks else [],
#                 "size": img.image_size,  # NEW: Image size
#                 "format": img.image_format  # NEW: Image format
#             })
#         return jsonify(image_list)
#     except Exception as e:
#         print(f"Error fetching images: {e}")
#         return jsonify([])
    
# # Route to serve the actual image files from the storage directory
# @app.route('/images/<path:filename>')
# def get_image_file(filename):
#     """Serve images from the IMAGE_STORAGE_DIR."""
#     # Ensure the path is secure and valid
#     return send_file(os.path.join(IMAGE_STORAGE_DIR, filename), mimetype='image/jpeg')

# @app.route('/get_combined_tray_data')
# @login_required
# def get_combined_tray_data():
#     """
#     Fetches and processes combined data from all trays,
#     providing overall metrics, growth, and weight distribution.
#     """
#     try:
#         # Get all unique tray numbers
#         tray_numbers = [result[0] for result in db.session.query(LarvaeData.tray_number).distinct().all()]

#         if not tray_numbers:
#             return jsonify({"error": "No tray data available"}), 404

#         # Get all historical data from all trays
#         all_data = []
#         latest_entries = [] # To calculate combined latest metrics

#         for tray_num in tray_numbers:
#             tray_data = LarvaeData.query.filter_by(tray_number=tray_num).all()
#             all_data.extend(tray_data)
#             latest_entry = LarvaeData.query.filter_by(tray_number=tray_num)\
#                                          .order_by(LarvaeData.timestamp.desc())\
#                                          .first()
#             if latest_entry:
#                 latest_entries.append(latest_entry)

#         if not all_data:
#             return jsonify({"error": "No data available"}), 404

#         # Calculate combined latest metrics (average for length/width/area/weight, sum for count)
#         combined_metrics = {
#             "length": round(sum(e.length for e in latest_entries)/len(latest_entries), 1) if latest_entries else 0,
#             "width": round(sum(e.width for e in latest_entries)/len(latest_entries), 1) if latest_entries else 0,
#             "area": round(sum(e.area for e in latest_entries)/len(latest_entries), 1) if latest_entries else 0,
#             "weight": round(sum(e.weight for e in latest_entries)/len(latest_entries), 1) if latest_entries else 0,
#             "count": sum(e.count for e in latest_entries) if latest_entries else 0
#         }

#         # Calculate combined growth data (average per day across all trays)
#         growth_data = {"days": [], "length": [], "weight": []}
#         day_data = defaultdict(list) # Stores all entries for a given 'day'

#         # Find the earliest timestamp among all data to set a consistent start day for combined growth
#         earliest_timestamp = min(entry.timestamp for entry in all_data)

#         for entry in all_data:
#             day = (entry.timestamp.date() - earliest_timestamp.date()).days + 1
#             day_data[day].append(entry)

#         for day, entries in sorted(day_data.items()):
#             growth_data["days"].append(day)
#             growth_data["length"].append(round(sum(e.length for e in entries)/len(entries), 1))
#             growth_data["weight"].append(round(sum(e.weight for e in entries)/len(entries), 1))

#         # Calculate combined weight distribution for all data
#         weight_bins = {
#             "80-90": 0, "90-100": 0, "100-110": 0,
#             "110-120": 0, "120-130": 0, "130-140": 0, "140+": 0
#         }

#         for entry in all_data:
#             weight = entry.weight
#             if 80 <= weight < 90: weight_bins["80-90"] += 1
#             elif 90 <= weight < 100: weight_bins["90-100"] += 1
#             elif 100 <= weight < 110: weight_bins["100-110"] += 1
#             elif 110 <= weight < 120: weight_bins["110-120"] += 1
#             elif 120 <= weight < 130: weight_bins["120-130"] += 1
#             elif 130 <= weight < 140: weight_bins["130-140"] += 1
#             else: weight_bins["140+"] += 1

#         return jsonify({
#             "metrics": combined_metrics,
#             "growthData": growth_data,
#             "weightDistribution": {
#                 "ranges": list(weight_bins.keys()),
#                 "counts": list(weight_bins.values())
#             },
#             "timestamp": datetime.utcnow().isoformat()
#         })
#     except Exception as e:
#         app.logger.error(f"Error fetching combined tray data: {e}")
#         return jsonify({"error": str(e)}), 500

# @app.route('/get_comparison_data')
# @login_required
# def get_comparison_data():
#     """
#     Fetches data for all trays to allow comparison on the dashboard.
#     Includes latest metrics, growth data, and all individual weights for distribution.
#     """
#     try:
#         trays_data_for_comparison = {}

#         # Dynamically get all unique tray numbers from the database
#         unique_trays = LarvaeData.query.with_entities(LarvaeData.tray_number).distinct().all()

#         # Iterate through each unique tray number found
#         for (tray_num,) in unique_trays:
#             # Fetch all historical data for the current tray, ordered by timestamp
#             all_tray_data = LarvaeData.query.filter_by(tray_number=tray_num)\
#                                           .order_by(LarvaeData.timestamp.asc())\
#                                           .all()

#             if not all_tray_data:
#                 # If a tray has no data, include empty data for it so the frontend can handle it
#                 trays_data_for_comparison[str(tray_num)] = {
#                     'latest': {'length': 0.0, 'width': 0.0, 'area': 0.0, 'weight': 0.0, 'count': 0},
#                     'growthData': {'days': [], 'length': [], 'weight': []},
#                     'allWeights': [] # Empty list for weight distribution if no data
#                 }
#                 continue # Move to the next tray

#             # --- Process Growth Data for this Tray: Get latest measurement per day ---
#             growth_data_for_tray = {
#                 "days": [],
#                 "length": [],
#                 "weight": []
#             }
#             daily_latest_data = {} # Key: day_number, Value: LarvaeData entry (the latest for that day)

#             start_date = all_tray_data[0].timestamp.date() # Start date for this specific tray

#             for entry in all_tray_data:
#                 day_number = (entry.timestamp.date() - start_date).days + 1
#                 # Overwrite with the current entry; since data is sorted ascending,
#                 # the last entry for a specific day will be the latest.
#                 daily_latest_data[day_number] = entry

#             # Populate growth_data lists from the processed daily data
#             for day in sorted(daily_latest_data.keys()):
#                 entry = daily_latest_data[day]
#                 growth_data_for_tray["days"].append(round(day, 1))
#                 growth_data_for_tray["length"].append(round(entry.length, 1))
#                 growth_data_for_tray["weight"].append(round(entry.weight, 1))

#             # --- Process All Individual Weights for this Tray's Distribution ---
#             all_individual_weights = [entry.weight for entry in all_tray_data]

#             # --- Get Latest Metrics for this Tray ---
#             latest_entry = all_tray_data[-1]

#             trays_data_for_comparison[str(tray_num)] = {
#                 'latest': {
#                     'length': round(latest_entry.length, 1),
#                     'width': round(latest_entry.width, 1),
#                     'area': round(latest_entry.area, 1),
#                     'weight': round(latest_entry.weight, 1),
#                     'count': latest_entry.count
#                 },
#                 'growthData': growth_data_for_tray,
#                 'allWeights': all_individual_weights # This is the key for comparison weight distribution
#             }

#         return jsonify({
#             'trays': trays_data_for_comparison,
#             'timestamp': datetime.utcnow().isoformat()
#         })
#     except Exception as e:
#         # Log the error for debugging purposes
#         app.logger.error(f"Error in get_comparison_data: {e}")
#         return jsonify({"error": str(e)}), 500

# @app.route('/dashboard')
# @login_required
# def dashboard():
#     """Renders the main dashboard page."""
#     # Dynamically get all unique tray numbers from the database
#     unique_tray_numbers_raw = LarvaeData.query.with_entities(LarvaeData.tray_number).distinct().all()

#     # Convert list of tuples to a sorted list of integers
#     unique_tray_numbers = sorted([tray_num for (tray_num,) in unique_tray_numbers_raw])

#     # Create a dictionary to hold the tray numbers to be passed to the template.
#     # The actual data for each tray will be fetched by JavaScript via AJAX.
#     tray_data_for_template = {}
#     for tray_num in unique_tray_numbers:
#         tray_data_for_template[tray_num] = {} # Empty dict, frontend only needs the keys

#     return render_template('dashboard.html', tray_data=tray_data_for_template)

# @app.route('/logout')
# @login_required
# def logout():
#     """Logs out the current user."""
#     logout_user()
#     flash('You have been logged out', 'success')
#     return redirect(url_for('login'))

# # --- MQTT Configuration (From mqtt_subscriber.py) ---
# MQTT_BROKER = "broker.hivemq.com"
# MQTT_PORT = 1883
# MQTT_TOPIC = "bsf_monitor/larvae_data" # IMPORTANT: This MUST match the topic in your data publisher!

# # --- MQTT Callbacks (Modified to use Flask-SQLAlchemy's db.session) ---
# def on_connect(client, userdata, flags, rc, properties):
#     """Callback function for when the MQTT client connects to the broker."""
#     if rc == 0:
#         print("Connected to MQTT Broker!")
#         client.subscribe(MQTT_TOPIC)
#         print(f"Subscribed to topic: {MQTT_TOPIC}")
#     else:
#         print(f"Failed to connect, return code {rc}\n")

# def on_message(client, userdata, msg):
#     """Callback function for when an MQTT message is received."""
#     print(f"Received message on topic {msg.topic}")
#     try:
#         data = json.loads(msg.payload.decode('utf-8'))

#         # Extract relevant fields from the incoming data
#         bounding_boxes = data.get("bounding_boxes")
#         masks = data.get("masks")

#         image_data_base64 = data.get("image_data_base64")
#         timestamp_str = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
#         image_filename = f"tray_{tray_number}_{timestamp_str}.jpg"
#         image_path = os.path.join(IMAGE_STORAGE_DIR, image_filename)
        
#         # Decode and save the image file
#         if image_data_base64:
#             image_bytes = base64.b64decode(image_data_base64)
#             with open(image_path, 'wb') as f:
#                 f.write(image_bytes)
#             print(f"Image saved to {image_path}")

#         # Validate incoming data (basic check, more robust validation could be added)
#         required_keys = ["tray_number", "length", "width", "area", "weight", "count"]
#         if not all(key in data for key in required_keys):
#             print("Error: Received payload is missing required keys.")
#             return

#         # Use Flask's app context to interact with the database in the MQTT thread
#         with app.app_context():
#             try:
#                 new_entry = LarvaeData(
#                     tray_number=data["tray_number"],
#                     length=data["length"],
#                     width=data["width"],
#                     area=data["area"],
#                     weight=data["weight"],
#                     count=data["count"],
#                     timestamp=datetime.utcnow() # Use current UTC time for consistency
#                 )
                
#                 new_image_file = ImageFile(
#                 tray_number=data["tray_number"],
#                 file_path=image_path,
#                 avg_length=avg_length,
#                 avg_weight=avg_weight,
#                 count=data["count"],
#                 # Store bounding boxes and masks as JSON strings
#                 bounding_boxes=json.dumps(bounding_boxes) if bounding_boxes else None,
#                 masks=json.dumps(masks) if masks else None
#                 )
#                 db.session.add(new_image_file)
#                 db.session.add(new_entry)
#                 db.session.commit()
#                 print(f"Successfully stored data for Tray {new_entry.tray_number}, ID: {new_entry.id}")
#             except Exception as e:
#                 db.session.rollback() # Rollback the transaction on error
#                 print(f"Error storing data to database: {e}")
#             finally:
#                 # Ensure the session is removed after each message processing.
#                 # This is crucial for thread safety with Flask-SQLAlchemy's scoped sessions.
#                 db.session.remove()

#     except json.JSONDecodeError:
#         print(f"Error: Could not decode JSON from message: {msg.payload.decode()}")
#     except Exception as e:
#         print(f"An unexpected error occurred in on_message: {e}")

# # --- MQTT Thread Function ---
# def run_mqtt_subscriber():
#     """Initializes and runs the MQTT client in a separate thread."""
#     mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2) # Specify API version
#     mqtt_client.on_connect = on_connect
#     mqtt_client.on_message = on_message

#     try:
#         mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
#         mqtt_client.loop_forever() # Blocks and handles reconnections
#     except Exception as e:
#         print(f"Failed to connect to MQTT broker or loop error: {e}")
#     finally:
#         print("MQTT subscriber stopped.")


# # UPDATED: Upload API for BLOB storage
# @app.route('/api/upload', methods=['POST'])
# @login_required
# def upload_image():
#     """Handle image upload and store in database as BLOB"""
#     try:
#         data = request.get_json()
#         if not data:
#             return jsonify({"error": "No data provided"}), 400

#         image_data = data.get('image_data')
#         tray_number = data.get('tray_number')
#         count = data.get('count')
#         avg_length = data.get('avg_length')
#         avg_weight = data.get('avg_weight')
#         bounding_boxes_json = data.get('bounding_boxes')
#         masks_json = data.get('masks')
        
#         if not image_data or not tray_number:
#             return jsonify({"error": "Missing image data or tray number"}), 400

#         # Decode the base64 image
#         image_binary = base64.b64decode(image_data)
        
#         # NEW: Process image for BLOB storage
#         img = Image.open(BytesIO(image_binary))
#         image_format = img.format.lower() if img.format else 'jpeg'
#         image_size = len(image_binary)
        
#         # Compress if too large
#         if image_size > 2 * 1024 * 1024:  # 2MB
#             output = BytesIO()
#             img.save(output, format='JPEG', quality=85, optimize=True)
#             image_binary = output.getvalue()
#             image_size = len(image_binary)
#             image_format = 'jpeg'
        
#         # NEW: Create ImageFile entry with BLOB data
#         new_image = ImageFile(
#             tray_number=tray_number,
#             image_data=image_binary,
#             image_format=image_format,
#             image_size=image_size,
#             count=count,
#             avg_length=avg_length,
#             avg_weight=avg_weight,
#             bounding_boxes=bounding_boxes_json,
#             masks=masks_json
#         )
        
#         db.session.add(new_image)
#         db.session.commit()
        
#         return jsonify({
#             "message": "Image saved to database successfully",
#             "image_id": new_image.id,
#             "size": image_size
#         }), 200

#     except Exception as e:
#         print(f"Error during image upload: {e}")
#         db.session.rollback()
#         return jsonify({"error": "Internal server error"}), 500


# # --- Main Execution Block ---
# if __name__ == '__main__':
#     # Initialize database and add dummy data within Flask app context
#     with app.app_context():
#         db.create_all() # Creates tables if they don't exist
#         # Create test user if none exists
#         if not User.query.filter_by(username='testuser').first():
#             admin_user = User(username='testuser')
#             admin_user.set_password('password')
#             db.session.add(admin_user)
#             db.session.commit()
#             print("Test user 'testuser' with password 'password' created.")

#         # Optional: Add some dummy data for new trays if the database is empty
#         if not LarvaeData.query.first():
#             print("Adding dummy data for demonstration.")
#             from random import uniform, randint

#             # Add data for tray 1
#             for i in range(1, 10):
#                 timestamp = datetime.utcnow() - timedelta(days=9 - i)
#                 db.session.add(LarvaeData(
#                     tray_number=1,
#                     length=round(uniform(10, 20), 1),
#                     width=round(uniform(2, 4), 1),
#                     area=round(uniform(20, 80), 1),
#                     weight=round(uniform(90, 150), 1),
#                     count=randint(100, 500),
#                     timestamp=timestamp
#                 ))

#             # Add data for tray 2
#             for i in range(1, 8):
#                 timestamp = datetime.utcnow() - timedelta(days=7 - i)
#                 db.session.add(LarvaeData(
#                     tray_number=2,
#                     length=round(uniform(12, 22), 1),
#                     width=round(uniform(2.5, 4.5), 1),
#                     area=round(uniform(25, 90), 1),
#                     weight=round(uniform(95, 160), 1),
#                     count=randint(80, 400),
#                     timestamp=timestamp
#                 ))

#             # Add data for tray 3
#             for i in range(1, 12):
#                 timestamp = datetime.utcnow() - timedelta(days=11 - i)
#                 db.session.add(LarvaeData(
#                     tray_number=3,
#                     length=round(uniform(9, 18), 1),
#                     width=round(uniform(1.8, 3.8), 1),
#                     area=round(uniform(18, 70), 1),
#                     weight=round(uniform(85, 145), 1),
#                     count=randint(120, 600),
#                     timestamp=timestamp
#                 ))

#             # Add dummy data for new trays (e.g., 156, 256, 356) to ensure they appear
#             for tray in [156, 256, 356]:
#                 for i in range(1, 7):
#                     timestamp = datetime.utcnow() - timedelta(days=6 - i)
#                     db.session.add(LarvaeData(
#                         tray_number=tray,
#                         length=round(uniform(15, 25), 1),
#                         width=round(uniform(3, 5), 1),
#                         area=round(uniform(30, 100), 1),
#                         weight=round(uniform(100, 180), 1),
#                         count=randint(150, 700),
#                         timestamp=timestamp
#                     ))
#             db.session.commit()
#             print("Dummy data added for demonstration including new trays.")

#     # Start MQTT subscriber in a separate thread
#     mqtt_thread = threading.Thread(target=run_mqtt_subscriber)
#     mqtt_thread.daemon = True # Allows the main program to exit even if the thread is still running
#     mqtt_thread.start()
#     print("MQTT subscriber thread started.")

#     # Start Flask app in the main thread
#     print("Starting Flask application...")
#     # use_reloader=False is important to prevent the MQTT thread from being started twice
#     # when Flask's debug mode is active.
#     app.run(host='0.0.0.0', port=8000, debug=True, use_reloader=False)
