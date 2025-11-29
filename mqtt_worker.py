# mqtt_worker.py - MQTT Worker for Render with PostgreSQL
import os
import time
import paho.mqtt.client as mqtt
import json
import base64
from PIL import Image
from io import BytesIO
from datetime import datetime, timezone

# Import from your main app
from BSFwebdashboard import app, db, LarvaeData, ImageFile

print("🚀 Starting MQTT Worker with PostgreSQL...")

# Database configuration is inherited from BSFwebdashboard
database_url = os.environ.get('DATABASE_URL', 'sqlite:///larvae_monitoring.db')
print(f"📊 Using database: {database_url.split('@')[-1] if '@' in database_url else database_url}")

# MQTT Configuration
MQTT_BROKER = "broker.hivemq.com"
MQTT_PORT = 1883
MQTT_TOPIC = "bsf_monitor/larvae_data"

mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

def on_connect(client, userdata, flags, rc, properties):
    if rc == 0:
        print("✅ MQTT Worker Connected to Broker!")
        client.subscribe(MQTT_TOPIC)
        print(f"📡 Subscribed to: {MQTT_TOPIC}")
    else:
        print(f"❌ MQTT Connection failed: {rc}")

def on_message(client, userdata, msg):
    print(f"📨 MQTT Worker received message on topic: {msg.topic}")
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

        print(f"🔄 Processing data for Tray {tray_number}...")

        # Use Flask app context for database operations
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
                if image_data_base64:
                    print(f"🖼️ Processing image for Tray {tray_number}...")
                    image_bytes = base64.b64decode(image_data_base64)
                    
                    # Process image
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
                        print(f"📦 Compressed image to {image_size} bytes")
                    
                    # Create image record
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
                    print(f"✅ Image saved to PostgreSQL for Tray {tray_number}")

                # Commit transaction
                db.session.commit()
                print(f"✅ Data successfully saved to PostgreSQL for Tray {tray_number}")

            except Exception as e:
                db.session.rollback()
                print(f"❌ Database error: {e}")
            finally:
                db.session.remove()

    except json.JSONDecodeError as e:
        print(f"❌ JSON decode error: {e}")
    except Exception as e:
        print(f"❌ MQTT message processing error: {e}")

def run_mqtt_worker():
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    
    while True:
        try:
            print("🔌 Connecting to MQTT broker...")
            mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
            print("🔄 Starting MQTT loop...")
            mqtt_client.loop_forever()
        except Exception as e:
            print(f"❌ MQTT connection error: {e}")
            print("🔄 Reconnecting in 30 seconds...")
            time.sleep(30)

if __name__ == '__main__':
    print("🎯 MQTT Worker starting...")
    with app.app_context():
        run_mqtt_worker()























# # mqtt_worker.py - Separate MQTT worker for Render
# import os
# import time
# import paho.mqtt.client as mqtt
# import json
# import base64
# from PIL import Image
# from io import BytesIO
# from datetime import datetime

# # Use the same database configuration
# from BSFwebdashboard import app, db, LarvaeData, ImageFile

# MQTT_BROKER = "broker.hivemq.com"
# MQTT_PORT = 1883
# MQTT_TOPIC = "bsf_monitor/larvae_data"

# mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

# def on_connect(client, userdata, flags, rc, properties):
#     if rc == 0:
#         print("✅ MQTT Worker Connected to Broker!")
#         client.subscribe(MQTT_TOPIC)
#         print(f"Subscribed to: {MQTT_TOPIC}")
#     else:
#         print(f"❌ MQTT Connection failed: {rc}")

# def on_message(client, userdata, msg):
#     print(f"📨 MQTT Worker received message")
#     try:
#         data = json.loads(msg.payload.decode('utf-8'))
#         tray_number = data.get("tray_number")
#         image_data_base64 = data.get("image_data_base64")
#         bounding_boxes = data.get("bounding_boxes")
#         masks = data.get("masks")

#         required_keys = ["tray_number", "length", "width", "area", "weight", "count"]
#         if not all(key in data for key in required_keys):
#             print("❌ Missing required keys")
#             return

#         # Use Flask app context
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
                
#                 # Save image if present
#                 if image_data_base64:
#                     image_bytes = base64.b64decode(image_data_base64)
#                     img = Image.open(BytesIO(image_bytes))
#                     image_format = img.format.lower() if img.format else 'jpeg'
#                     image_size = len(image_bytes)
                    
#                     # Compress if too large
#                     if image_size > 2 * 1024 * 1024:
#                         output = BytesIO()
#                         img.save(output, format='JPEG', quality=85, optimize=True)
#                         image_bytes = output.getvalue()
#                         image_size = len(image_bytes)
#                         image_format = 'jpeg'
                    
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
#                     print(f"✅ Image saved to database for Tray {tray_number}")

#                 db.session.commit()
#                 print(f"✅ Data saved for Tray {tray_number}")

#             except Exception as e:
#                 db.session.rollback()
#                 print(f"❌ Database error: {e}")
#             finally:
#                 db.session.remove()

#     except Exception as e:
#         print(f"❌ MQTT message error: {e}")

# def run_mqtt_worker():
#     mqtt_client.on_connect = on_connect
#     mqtt_client.on_message = on_message
    
#     while True:
#         try:
#             print("🚀 Starting MQTT Worker...")
#             mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
#             mqtt_client.loop_forever()
#         except Exception as e:
#             print(f"❌ MQTT Worker error: {e}")
#             print("🔄 Reconnecting in 30 seconds...")
#             time.sleep(30)

# if __name__ == '__main__':
#     with app.app_context():
#         run_mqtt_worker()