# from sqlalchemy import create_engine, Column, Integer, Float, DateTime
# from sqlalchemy.orm import sessionmaker, declarative_base 
# from datetime import datetime
# import paho.mqtt.client as mqtt # Import MQTT library
# import json # To parse incoming JSON data
# import time # For sleep

# # --- Database Configuration (Must match Flask's config) ---
# DATABASE_URL = "sqlite:///./larvae_monitoring.db" # Relative path assumes it's in the same directory
#                                                   # Make sure this path is accessible by this script

# engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
# SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
# Base = declarative_base()

# # --- Database Model for LarvaeData (Replicated from app.py/api.py) ---
# class LarvaeData(Base):
#     __tablename__ = "larvae_data" # Ensure this matches Flask's model table name

#     id = Column(Integer, primary_key=True, index=True)
#     tray_number = Column(Integer, nullable=False)
#     length = Column(Float, nullable=False)
#     width = Column(Float, nullable=False)
#     area = Column(Float, nullable=False)
#     weight = Column(Float, nullable=False)
#     count = Column(Integer, nullable=False)
#     timestamp = Column(DateTime, default=datetime.utcnow)

# # Ensure the database table exists
# Base.metadata.create_all(bind=engine)
# print("Database table 'larvae_data' ensured to exist.")


# # --- MQTT Configuration ---
# MQTT_BROKER = "broker.hivemq.com"
# MQTT_PORT = 1883
# MQTT_TOPIC = "bsf_monitor/larvae_data" # <--- IMPORTANT: This MUST match the topic in your Pi script!

# # --- MQTT Callbacks ---
# def on_connect(client, userdata, flags, rc):
#     if rc == 0:
#         print("Connected to MQTT Broker!")
#         client.subscribe(MQTT_TOPIC)
#         print(f"Subscribed to topic: {MQTT_TOPIC}")
#     else:
#         print(f"Failed to connect, return code {rc}\n")

# def on_message(client, userdata, msg):
#     print(f"Received message on topic '{msg.topic}': {msg.payload.decode()}")
#     try:
#         payload = json.loads(msg.payload.decode())

#         # Validate incoming data (basic check, more robust validation could be added)
#         required_keys = ["tray_number", "length", "width", "area", "weight", "count"]
#         if not all(key in payload for key in required_keys):
#             print("Error: Received payload is missing required keys.")
#             return

#         # Create a new database session for this message
#         db = SessionLocal()
#         try:
#             new_entry = LarvaeData(
#                 tray_number=payload["tray_number"],
#                 length=payload["length"],
#                 width=payload["width"],
#                 area=payload["area"],
#                 weight=payload["weight"],
#                 count=payload["count"],
#                 timestamp=datetime.utcnow() # Use current UTC time for consistency
#             )
#             db.add(new_entry)
#             db.commit()
#             db.refresh(new_entry) # Refresh to get the generated ID and timestamp
#             print(f"Successfully stored data for Tray {new_entry.tray_number}, ID: {new_entry.id}")
#         except Exception as e:
#             db.rollback() # Rollback the transaction on error
#             print(f"Error storing data to database: {e}")
#         finally:
#             db.close() # Always close the session

#     except json.JSONDecodeError:
#         print(f"Error: Could not decode JSON from message: {msg.payload.decode()}")
#     except Exception as e:
#         print(f"An unexpected error occurred in on_message: {e}")


# # --- Main Execution Block ---
# if __name__ == "__main__":
#     mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1) # Specify API version
#     mqtt_client.on_connect = on_connect
#     mqtt_client.on_message = on_message

#     try:
#         mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
#         mqtt_client.loop_forever() # Blocks and handles reconnections
#     except Exception as e:
#         print(f"Failed to connect to MQTT broker or loop error: {e}")
#     finally:
#         print("MQTT subscriber stopped.")



from sqlalchemy import create_engine, Column, Integer, Float, DateTime, String, LargeBinary
from sqlalchemy.orm import sessionmaker, declarative_base 
from datetime import datetime
import paho.mqtt.client as mqtt
import json
import time
import base64
from PIL import Image
from io import BytesIO

# --- Database Configuration (Must match Flask's config) ---
DATABASE_URL = "sqlite:///./larvae_monitoring.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- Database Models (UPDATED for BLOB storage) ---
class LarvaeData(Base):
    __tablename__ = "larvae_data"

    id = Column(Integer, primary_key=True, index=True)
    tray_number = Column(Integer, nullable=False)
    length = Column(Float, nullable=False)
    width = Column(Float, nullable=False)
    area = Column(Float, nullable=False)
    weight = Column(Float, nullable=False)
    count = Column(Integer, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)

# NEW: ImageFile model for BLOB storage
class ImageFile(Base):
    __tablename__ = "image_files"
    
    id = Column(Integer, primary_key=True, index=True)
    tray_number = Column(Integer, nullable=False)
    
    # BLOB fields for image storage
    image_data = Column(LargeBinary, nullable=False)  # Actual image binary data
    image_format = Column(String(10), nullable=False)  # jpeg, png, etc.
    image_size = Column(Integer, nullable=False)       # Size in bytes
    
    # Metadata
    timestamp = Column(DateTime, default=datetime.utcnow)
    avg_length = Column(Float, nullable=True)
    avg_weight = Column(Float, nullable=True)
    count = Column(Integer, nullable=True)
    
    # Classification data
    bounding_boxes = Column(String, nullable=True)  # JSON string
    masks = Column(String, nullable=True)          # JSON string

# Ensure the database tables exist
Base.metadata.create_all(bind=engine)
print("Database tables 'larvae_data' and 'image_files' ensured to exist.")

# --- MQTT Configuration ---
MQTT_BROKER = "broker.hivemq.com"
MQTT_PORT = 1883
MQTT_TOPIC = "bsf_monitor/larvae_data"

# --- MQTT Callbacks (UPDATED for BLOB storage) ---
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected to MQTT Broker!")
        client.subscribe(MQTT_TOPIC)
        print(f"Subscribed to topic: {MQTT_TOPIC}")
    else:
        print(f"Failed to connect, return code {rc}\n")

def on_message(client, userdata, msg):
    print(f"Received message on topic '{msg.topic}'")
    try:
        payload = json.loads(msg.payload.decode())

        # Extract image data if present
        image_data_base64 = payload.get("image_data_base64")
        bounding_boxes = payload.get("bounding_boxes")
        masks = payload.get("masks")

        # Validate required fields
        required_keys = ["tray_number", "length", "width", "area", "weight", "count"]
        if not all(key in payload for key in required_keys):
            print("Error: Received payload is missing required keys.")
            return

        # Create a new database session for this message
        db = SessionLocal()
        try:
            # Save larvae data
            new_entry = LarvaeData(
                tray_number=payload["tray_number"],
                length=payload["length"],
                width=payload["width"],
                area=payload["area"],
                weight=payload["weight"],
                count=payload["count"],
                timestamp=datetime.utcnow()
            )
            db.add(new_entry)

            # NEW: Save image to database BLOB if present
            if image_data_base64:
                try:
                    # Decode base64 image
                    image_bytes = base64.b64decode(image_data_base64)
                    
                    # Get image format and size
                    img = Image.open(BytesIO(image_bytes))
                    image_format = img.format.lower() if img.format else 'jpeg'
                    image_size = len(image_bytes)
                    
                    # Compress image if too large (optional)
                    if image_size > 2 * 1024 * 1024:  # 2MB
                        output = BytesIO()
                        img.save(output, format='JPEG', quality=85, optimize=True)
                        image_bytes = output.getvalue()
                        image_size = len(image_bytes)
                        image_format = 'jpeg'
                        print(f"📦 Compressed image from {len(base64.b64decode(image_data_base64))} to {image_size} bytes")
                    
                    # Create new image record with BLOB data
                    new_image_file = ImageFile(
                        tray_number=payload["tray_number"],
                        image_data=image_bytes,
                        image_format=image_format,
                        image_size=image_size,
                        avg_length=payload.get('avg_length'),
                        avg_weight=payload.get('avg_weight'),
                        count=payload.get('count'),
                        bounding_boxes=json.dumps(bounding_boxes) if bounding_boxes else None,
                        masks=json.dumps(masks) if masks else None
                    )
                    db.add(new_image_file)
                    print(f"✅ Image saved to database BLOB for Tray {payload['tray_number']}, Size: {image_size} bytes")
                    
                except Exception as img_error:
                    print(f"❌ Error processing image: {img_error}")
                    # Continue with larvae data even if image fails

            db.commit()
            print(f"✅ Data saved to database for Tray {payload['tray_number']}")

        except Exception as e:
            db.rollback()
            print(f"❌ Error storing data to database: {e}")
        finally:
            db.close()

    except json.JSONDecodeError:
        print(f"Error: Could not decode JSON from message: {msg.payload.decode()}")
    except Exception as e:
        print(f"An unexpected error occurred in on_message: {e}")

# --- Main Execution Block ---
if __name__ == "__main__":
    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    try:
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        print("🚀 Starting MQTT subscriber with BLOB image storage...")
        mqtt_client.loop_forever()
    except Exception as e:
        print(f"Failed to connect to MQTT broker or loop error: {e}")
    finally:
        print("MQTT subscriber stopped.")