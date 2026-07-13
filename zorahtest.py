
import time
import cv2
import os
import easyocr
from datetime import datetime
import numpy as np
import paho.mqtt.client as mqtt
import json
import requests
import base64
#from camera_capture import capture_image
import math
from skimage.measure import perimeter
import RPi.GPIO as GPIO
import subprocess
# --- Flat-Bug Model Imports ---
from flat_bug.predictor import Predictor
from flat_bug.config import DEFAULT_CFG, read_cfg
from flat_bug import logger as flatbug_logger, set_log_level


BUTTON_PIN = 3
# Set GPIO mode to BCM and set up the button pin
GPIO.setmode(GPIO.BCM)
# Use a pull-up resistor: pin is HIGH normally, LOW when pressed
GPIO.setup(BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
# --- Web API Configuration ---
# ADDED: Direct API endpoint for image uploads
WEB_APP_API_URL = "http://134.122.83.13:8000/api/upload"  # VPS server endpoint
# WEB_APP_API_URL = "http://localhost:8000/api/upload"  # For local testing

# API Authentication - Will be prompted at startup
API_USERNAME = None
API_PASSWORD = None

# --- MQTT Configuration ---
MQTT_BROKER = "broker.hivemq.com"
MQTT_PORT = 1883
MQTT_TOPIC = "bsf_monitor/larvae_data"

# --- Configuration ---
INPUT_IMAGE_DIR = "/home/autopheno/Documents/img"
PROCESSED_IMAGE_DIR = "/home/autopheno/Documents/processed_images"
OUTPUT_DETECTION_DIR = "/home/autopheno/Documents/BSF-pi-script/detected_images"

# EasyOCR Settings
EASYOCR_LANGUAGES = ['en']
EASYOCR_ALLOWLIST = '0123456789'
EASYOCR_BLOCKLIST = ''

# Script Timing
PROCESS_INTERVAL_SECONDS = 20

# Flat-Bug Model Configuration
FLATBUG_MODEL_PATH = "/home/autopheno/Documents/YoloRetrain.pt"
FLATBUG_DEVICE = "cpu"
FLATBUG_DTYPE = "float32"

# Calibration Factor (pixels per millimeter)
PIXELS_PER_MM = 0.132

# =============================================================================
def capture_image(DIR):
    if not os.path.exists(DIR):
        os.makedirs(DIR)
        print(f"Created directory: {DIR}")

    fileName = datetime.now().strftime("%Y-%m-%d-%H-%M-%S") + ".jpg"
    filePath = os.path.join(DIR, fileName)
    cmd = "rpicam-still -t 1000 -o " + filePath

    subprocess.call(cmd, shell=True)
    print('Image ' + fileName)

# === STEP 2: INITIALIZE EASY OCR
# =============================================================================
def initialize_easyocr():
    """Initializes and returns the EasyOCR reader object."""
    print("Initializing EasyOCR reader. This may download models on first run...")
    try:
        reader = easyocr.Reader(EASYOCR_LANGUAGES, recog_network='latin_g2', gpu=False)
        print("EasyOCR reader initialized successfully for integer-only recognition.")
        return reader
    except Exception as e:
        print(f"Error initializing EasyOCR: {e}")
        print("Please ensure you have an internet connection for the first run to download models.")
        exit()

# =============================================================================
# === STEP 4: LOAD FLATBUG MODEL
# =============================================================================
def initialize_flatbug_model():
    """Loads and returns the Flat-Bug predictor object."""
    print(f"Loading Flat-Bug model from: {FLATBUG_MODEL_PATH} on device: {FLATBUG_DEVICE}...")
    try:
        flatbug_config = DEFAULT_CFG
        flatbug_predictor = Predictor(
            FLATBUG_MODEL_PATH,
            device=FLATBUG_DEVICE,
            dtype=FLATBUG_DTYPE,
            cfg=flatbug_config
        )
        print("Flat-Bug model loaded successfully.")
        return flatbug_predictor
    except Exception as e:
        print(f"Error loading Flat-Bug model: {e}")
        print("Please ensure your model path is correct and flat-bug library is installed.")
        exit()

# =============================================================================
# === STEP 7: CONNECT TO MQTT BROKER
# =============================================================================
def on_connect(client, userdata, flags, rc, properties):
    """Callback function for when the MQTT client connects to the broker."""
    if rc == 0:
        print("Connected to MQTT Broker!")
    else:
        print(f"Failed to connect, return code {rc}\n")

def initialize_mqtt_client():
    """Initializes, connects, and returns the MQTT client object."""
    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqtt_client.on_connect = on_connect
    try:
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        mqtt_client.loop_start() 
        return mqtt_client
    except Exception as e:
        print(f"Failed to connect to MQTT broker: {e}")
        exit()

# =============================================================================
# === HELPER FUNCTIONS
# =============================================================================

def preprocess_image_for_easyocr(image):
    """Converts image to grayscale and applies median blur for OCR."""
    if image is None or image.size == 0:
        return None
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    denoised = cv2.medianBlur(gray, 3) 
    return denoised

def get_tray_id(image_path, reader):
    """
    Extracts integer text (assumed to be tray number) from an image using EasyOCR.
    Returns the extracted integer as a string and its confidence.
    """
    image = cv2.imread(image_path)
    if image is None:
        print(f"Error: Could not read image {image_path}")
        return None, 0

    processed_image = preprocess_image_for_easyocr(image)
    if processed_image is None:
        return None, 0

    extracted_integers = []
    all_confidences = []

    try:
        results = reader.readtext(processed_image, allowlist=EASYOCR_ALLOWLIST)

        for (bbox, text, confidence) in results:
            if text.strip():
                cleaned_text = ''.join(filter(str.isdigit, text.strip()))
                if cleaned_text:
                    try:
                        integer_value = int(cleaned_text)
                        extracted_integers.append(str(integer_value))
                        all_confidences.append(float(confidence))
                    except ValueError:
                        print(f"  Skipping non-integer segment after filtering: '{cleaned_text}' from original '{text}'")
                else:
                    print(f"  No digits found after filtering: '{text}'")
            else:
                print("  Skipping empty text detection.")

    except Exception as e:
        print(f"Error during EasyOCR processing: {e}")
        return None, 0

    if extracted_integers:
        final_extracted_text = extracted_integers[0]
        overall_avg_confidence = sum(all_confidences) / len(all_confidences) if all_confidences else 0
        return final_extracted_text, overall_avg_confidence * 100
    else:
        return None, 0


def get_tray_id_from_filename(image_path):
    """Fallback tray ID parser that extracts digits from filename when OCR fails."""
    filename = os.path.basename(image_path)
    digits = ''.join(ch for ch in filename if ch.isdigit())
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None

def detect_larvae(image_path, predictor):
    """Runs the Flat-Bug model on the image and returns prediction results."""
    print(f"Running Flat-Bug inference on {image_path}...")
    try:
        prediction_results = predictor.pyramid_predictions(
            image_path,
            scale_increment=2/3,
            scale_before=1.0,
            single_scale=False
        )
        return prediction_results
    except Exception as e:
        print(f"Error during Flat-Bug inference for {image_path}: {e}")
        import traceback
        traceback.print_exc()
        return None

def calculate_larva_metrics(bbox, mask=None):
    """
    Calculates larva length, width, area, and estimated weight based on bounding box and mask.
    Assumes bbox is [x1, y1, x2, y2] in pixels.
    """
    x1, y1, x2, y2 = bbox
    length_px = abs(y2 - y1)
    width_px = abs(x2 - x1)

    # 1. Binarize (in case it's not strictly 0s and 1s)
    mask_bin = (mask > 0).astype(np.uint8)
    mask_2d = np.squeeze(mask_bin)
    # 3. Calculate Area
    area_px = np.sum(mask_2d)

    # Fix 2: Use the 2D mask for perimeter calculation
    perimeter_px = perimeter(mask_2d)

    area_sq_mm = area_px * (PIXELS_PER_MM ** 2)
    prm = perimeter_px / 4
    prm_mm = prm * PIXELS_PER_MM
    width_px = prm_mm - math.sqrt(abs((prm_mm ** 2) - area_sq_mm))
    length_px = prm_mm + math.sqrt(abs((prm_mm ** 2) - area_sq_mm))
    length_mm = length_px * PIXELS_PER_MM
    width_mm = width_px * PIXELS_PER_MM

    estimated_weight_mg = (0.012 * area_sq_mm + 0.026 * length_mm + + 0.215) / 2
    # WEIGHT_PER_SQ_MM = 6.67
    # estimated_weight_mg = area_sq_mm * WEIGHT_PER_SQ_MM

    return length_mm, width_mm, area_sq_mm, estimated_weight_mg

# CHANGED: Simplified to only compute metrics (no image data)
def compute_and_aggregate_metrics(prediction_results, tray_number):
    """
    Processes prediction results to compute metrics for each larva.
    Returns metrics payload for MQTT and detected image path.
    """
    larvae_data_to_send = []
    total_count = 0
    detected_image_path = None  # NEW: Track detected image path

    if prediction_results and hasattr(prediction_results, 'boxes') and prediction_results.boxes is not None and len(prediction_results.boxes) > 0:
        total_count = len(prediction_results.boxes)
        print(f"Found {total_count} larvae in Tray {tray_number}.")

        # Save detection image locally (optional)
        detected_image_path = os.path.join(OUTPUT_DETECTION_DIR, f"detected_{tray_number}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg")
        prediction_results.plot(
            outpath=detected_image_path,
            masks=True,
            boxes=True,
            confidence=True,
            linewidth=2,
            contour_color=(0, 255, 0),
            box_color=(255, 0, 0)
        )
        print(f"Saved detection image to: {detected_image_path}")

        # Calculate metrics
        for larva_id in range(total_count):
            bbox_xyxy = prediction_results.boxes[larva_id].tolist()
            larva_confidence = prediction_results.confs[larva_id].item()

            mask = None
            if hasattr(prediction_results, 'masks') and prediction_results.masks is not None and len(prediction_results.masks) > larva_id:
                larva_mask_object = prediction_results.masks[larva_id]
                mask = larva_mask_object.data.cpu().numpy().astype(np.uint8)

            length_mm, width_mm, area_sq_mm, estimated_weight_mg = calculate_larva_metrics(bbox_xyxy, mask)

            larvae_data_to_send.append({
                "length": round(length_mm, 2),
                "width": round(width_mm, 2),
                "area": round(area_sq_mm, 2),
                "weight": round(estimated_weight_mg, 2),
                "count": 1
            })
            print(f"  Larva {larva_id + 1}: L={length_mm:.4f}mm, W={width_mm:.4f}mm, A={area_sq_mm:.4f}mmÂ², Wt={estimated_weight_mg:.4f}g")

    else:
        print(f"No larvae detected by Flat-Bug in Tray {tray_number}.")
        total_count = 0

    # Create metrics payload for MQTT
    if total_count > 0:
        avg_length = sum(d['length'] for d in larvae_data_to_send) / total_count
        avg_width = sum(d['width'] for d in larvae_data_to_send) / total_count
        avg_area = sum(d['area'] for d in larvae_data_to_send) / total_count
        avg_weight = sum(d['weight'] for d in larvae_data_to_send) / total_count
        
        # Extract individual weights for distribution
        individual_weights = [d['weight'] for d in larvae_data_to_send]

        payload = {
            "tray_number": tray_number,
            "length": round(avg_length, 4),
            "width": round(avg_width, 4),
            "area": round(avg_area, 4),
            "weight": round(avg_weight, 4),
            "count": total_count,
            "timestamp": datetime.utcnow().isoformat(),
            "individual_weights": individual_weights  # Add individual weights
        }
        return payload, total_count, detected_image_path  # Return detected image path
    else:
        # Return empty payload if no larvae detected
        payload = {
            "tray_number": tray_number,
            "length": 0,
            "width": 0,
            "area": 0,
            "weight": 0,
            "count": 0,
            "timestamp": datetime.utcnow().isoformat()
        }
        return payload, 0, None  # No detected image



def upload_image_to_api(image_path, tray_number, count, avg_length, avg_width, avg_area, avg_weight, individual_weights, bounding_boxes, masks):
    """Uploads detection data directly to web app API; image is optional."""
    try:
        image_data_base64 = None
        if image_path:
            # Read and compress image before encoding - PRESERVES ORIGINAL COLORS
            print(f"Reading and compressing image for Tray {tray_number}...")

            # Read image with OpenCV - this maintains original colors
            img = cv2.imread(image_path)
            if img is None:
                print(f"âŒ Could not read image: {image_path}")
                return False

            # Convert from BGR to RGB to maintain correct color representation
            img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

            # Resize if too large (same as your main app)
            max_dimension = 1200
            height, width = img_rgb.shape[:2]

            if max(height, width) > max_dimension:
                scale = max_dimension / max(height, width)
                new_width = int(width * scale)
                new_height = int(height * scale)
                img_rgb = cv2.resize(img_rgb, (new_width, new_height), interpolation=cv2.INTER_LANCZOS4)
                print(f"  Resized image from {width}x{height} to {new_width}x{new_height}")

            # Compress as JPEG
            encode_params = [cv2.IMWRITE_JPEG_QUALITY, 85]
            success, encoded_img = cv2.imencode('.jpg', img_rgb, encode_params)

            if not success:
                print("âŒ Failed to encode image")
                return None

            # Convert to base64
            image_data_base64 = base64.b64encode(encoded_img.tobytes()).decode('utf-8')
            file_size_kb = len(image_data_base64) / 1024

            print(f"  Compressed size: {file_size_kb:.1f} KB")
        else:
            print(f"âš ï¸ No image file for Tray {tray_number}; uploading metrics only")

        # TEST MODE: Send empty detection data
        print("ðŸ”„ TEST MODE: Sending empty detection data (bounding_boxes=[], masks=[])")
        
        # Prepare payload for API
        api_payload = {
            'username': API_USERNAME,  # Authentication
            'password': API_PASSWORD,  # Authentication
            'tray_number': tray_number,
            'count': count,
            'avg_length': avg_length,
            'avg_width': avg_width,
            'avg_area': avg_area,
            'avg_weight': avg_weight,
            'individual_weights': individual_weights if individual_weights else [],  # Add individual weights
            'bounding_boxes': json.dumps(bounding_boxes) if bounding_boxes else "[]",
            'masks': json.dumps(masks) if masks else "[]"
        }
        if image_data_base64:
            api_payload['image_data'] = image_data_base64
        
        print(f"Uploading payload for Tray {tray_number} to API...")
        
        # Use simple requests.post (NO Session) - This is what works in test script
        response = requests.post(
            WEB_APP_API_URL,
            json=api_payload,
            timeout=120,  # 2 minute timeout
            headers={'Content-Type': 'application/json'}
        )
        
        print(f"Response status: {response.status_code}")
        
        if response.status_code == 200:
            try:
                result = response.json()
                print(f"âœ… SUCCESS: {result.get('message', 'Upload successful')}")
                return True
            except json.JSONDecodeError as e:
                print(f"âš ï¸  Server returned 200 but invalid JSON: {e}")
                # Even if JSON fails, status 200 means upload likely worked
                print("âœ… Upload likely successful despite JSON issue")
                return True
        else:
            print(f"âŒ API upload failed: {response.status_code} - {response.text}")
            return False
            
    except requests.exceptions.Timeout:
        print("âŒ TIMEOUT: Upload took too long (over 120 seconds)")
        return False
    except requests.exceptions.ConnectionError as e:
        print(f"âŒ CONNECTION ERROR: {e}")
        return False
    except Exception as e:
        print(f"âŒ UNEXPECTED ERROR: {e}")
        return False



def test_api_connection():
    """Test basic connection to the API"""
    print("ðŸ” Testing API connection...")
    try:
        # Test if the main site is reachable
        response = requests.get("http://134.122.83.13:8000", timeout=10)
        if response.status_code == 200:
            print("âœ… Main site is reachable")
            return True
        else:
            print(f"âŒ Main site returned status: {response.status_code}")
            return False
    except Exception as e:
        print(f"âŒ Cannot reach main site: {e}")
        return False



# NEW: Retry logic for image upload
def upload_image_to_api_with_retry(image_path, tray_number, count, avg_length, avg_width, avg_area, avg_weight, individual_weights, bounding_boxes, masks, max_retries=2):
    """Upload image with retry logic for transient failures."""
    for attempt in range(max_retries + 1):
        print(f"📤 Upload attempt {attempt + 1}/{max_retries + 1} for Tray {tray_number}")
        success = upload_image_to_api(image_path, tray_number, count, avg_length, avg_width, avg_area, avg_weight, individual_weights, bounding_boxes, masks)
        if success:
            return True
        elif attempt < max_retries:
            wait_time = (attempt + 1) * 5  # 5, 10 seconds between retries
            print(f"ðŸ”„ Retry {attempt + 1}/{max_retries} in {wait_time} seconds...")
            time.sleep(wait_time)
        else:
            print(f"ðŸ’¥ All upload attempts failed for Tray {tray_number}")
    
    return False

# NEW: Extract bounding boxes and masks with limit
def extract_detection_data(prediction_results, max_detections=50):
    """Extracts bounding boxes and masks from prediction results for API upload, with limit to avoid oversized payloads."""
    bounding_boxes = []
    masks = []
    
    if prediction_results and hasattr(prediction_results, 'boxes') and prediction_results.boxes is not None:
        total_detected = len(prediction_results.boxes)
        # Limit the number of detections we send to avoid oversized payloads
        limited_count = min(total_detected, max_detections)
        
        print(f"ðŸ” Limiting API data: sending {limited_count} detections (of {total_detected} total)")
        
        for larva_id in range(limited_count):
            bbox_xyxy = prediction_results.boxes[larva_id].tolist()
            bounding_boxes.append(bbox_xyxy)
            
            if hasattr(prediction_results, 'masks') and prediction_results.masks is not None and len(prediction_results.masks) > larva_id:
                larva_mask_object = prediction_results.masks[larva_id]
                mask = larva_mask_object.data.cpu().numpy().astype(np.uint8)
                masks.append(mask.tolist())
    
    return bounding_boxes, masks

def publish_data(mqtt_client, payload):
    """Publishes the metrics data payload to the MQTT broker."""
    print(f"Publishing metrics data for Tray {payload['tray_number']} via MQTT...")
    try:
        mqtt_client.publish(MQTT_TOPIC, json.dumps(payload), qos=1)
        print(f"âœ… Metrics data published to MQTT for Tray {payload['tray_number']}")
        
    except Exception as mqtt_e:
        print(f"âŒ Error publishing data to MQTT broker: {mqtt_e}")

# =============================================================================
# === MAIN PROCESSING FUNCTION
# =============================================================================

def process_available_images(reader, predictor, mqtt_client):
    """
    Monitors the input directory for new images, processes them,
    and publishes data via both MQTT (metrics) and API (images).
    """
    print(f"\n--- Checking for new images in {INPUT_IMAGE_DIR} at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")

    # Get all image files
    image_files = [f for f in os.listdir(INPUT_IMAGE_DIR) 
                   if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp'))]
    
    if not image_files:
        print("No new images found in the input folder.")
        return False
    
    # Sort by modification time (newest first)
    image_files_with_time = [(f, os.path.getmtime(os.path.join(INPUT_IMAGE_DIR, f))) 
                             for f in image_files]
    image_files_with_time.sort(key=lambda x: x[1], reverse=True)
    
    # Process only the LATEST image
    filename = image_files_with_time[0][0]
    print(f"📸 Found {len(image_files)} images, processing LATEST: {filename}")
    
    image_path = os.path.join(INPUT_IMAGE_DIR, filename)
    print(f"Processing image: {image_path}")

    # Extract tray number
    tray_number_str, ocr_confidence = get_tray_id(image_path, reader)
    
    if tray_number_str:
        try:
            tray_number = int(tray_number_str)
            print(f"Detected Tray Number: {tray_number} (Confidence: {ocr_confidence:.2f}%)")
        except ValueError:
            print(f"Warning: Could not convert '{tray_number_str}' to an integer. Skipping.")
            tray_number = None
    else:
        print("No tray number detected by EasyOCR. Skipping.")
        tray_number = None

    if tray_number is None:
        tray_from_name = get_tray_id_from_filename(image_path)
        if tray_from_name is not None:
            tray_number = tray_from_name
            print(f"OCR tray detection failed, using filename tray number: {tray_number}")
        else:
            # Move image even if no tray number detected
            destination_path = os.path.join(PROCESSED_IMAGE_DIR, filename)
            os.rename(image_path, destination_path)
            print(f"Moved image (no tray number): {image_path} to {destination_path}")
            return False

    # Detect larvae
    prediction_results = detect_larvae(image_path, predictor)

    # CHANGED: Get metrics for MQTT and detected image path
    metrics_payload, total_count, detected_image_path = compute_and_aggregate_metrics(prediction_results, tray_number)

    # NEW: Extract detection data for API
    bounding_boxes, masks = extract_detection_data(prediction_results)

    # ADD THIS DEBUG LINE:
    print(f"ðŸ” DEBUG: Extracted {len(bounding_boxes)} bounding boxes, {len(masks)} masks")

    # Send metrics via MQTT
    publish_data(mqtt_client, metrics_payload)

    # Upload payload via API (image optional)
    api_image_path = detected_image_path if detected_image_path else None
    upload_success = upload_image_to_api_with_retry(
            image_path=api_image_path,
            tray_number=tray_number,
            count=total_count,
            avg_length=metrics_payload['length'],
            avg_width=metrics_payload['width'],
            avg_area=metrics_payload['area'],
            avg_weight=metrics_payload['weight'],
            individual_weights=metrics_payload.get('individual_weights', []),
            bounding_boxes=bounding_boxes,
            masks=masks,
            max_retries=2  # Will try 3 times total (original + 2 retries)
        )
        
    if not upload_success:
        print(f"âš ï¸  API upload failed for Tray {tray_number}, but metrics were sent via MQTT")

    # Move the processed image
    destination_path = os.path.join(PROCESSED_IMAGE_DIR, filename)
    os.rename(image_path, destination_path)
    print(f"Moved processed image: {image_path} to {destination_path}")
    
    return True

# =============================================================================
# === MAIN EXECUTION BLOCK
# =============================================================================

def main():
    """
    SarahMain function to initialize models and run the processing loop.
    Uses a non-blocking loop so the button remains responsive.
    """
    global API_USERNAME, API_PASSWORD
    
    # Prompt for credentials at startup
    print("\n" + "="*50)
    print("BSF LARVAE MONITORING SYSTEM")
    print("="*50)
    print("\nPlease enter your account credentials:")
    API_USERNAME = input("Username: ").strip()
    API_PASSWORD = input("Password: ").strip()
    print("\n✅ Credentials set. Starting system...\n")
    
    # Create directories if they don't exist
    os.makedirs(INPUT_IMAGE_DIR, exist_ok=True)
    os.makedirs(PROCESSED_IMAGE_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DETECTION_DIR, exist_ok=True)

    # Initialize components
    reader = initialize_easyocr()
    predictor = initialize_flatbug_model()
    mqtt_client = initialize_mqtt_client()

    # TEST CONNECTION FIRST
    print("=== CONNECTION TEST ===")
    test_api_connection()
    print("=======================\n")

    # --- TIMING VARIABLES ---
    # We set this to 0 initially so the auto-check runs once immediately on startup
    # (or set to time.time() if you want to wait 20s before the first auto-check)
    last_auto_check_time = 0 
    
    try:
        print(f"System Ready. Press button (GPIO {BUTTON_PIN}) or wait for auto-cycle.")
        
        while True:
            current_time = time.time()
            
            # --- 1. CHECK BUTTON (High Priority) ---
            if GPIO.input(BUTTON_PIN) == GPIO.LOW:
                print("\n--- Button Pressed! Capturing and Processing Images ---")

                # Manually trigger capture
                capture_image(INPUT_IMAGE_DIR)
                
                # Process immediately
                process_available_images(reader, predicfailure tor, mqtt_client)

                print("Button cycle completed.")
                
                # Reset the auto-timer so we don't double-process immediately after a button press
                last_auto_check_time = time.time()
                
                # Debounce: Wait 0.5s so one press doesn't register as two
                time.sleep(0.5) 
                
            # --- 2. CHECK AUTOMATIC INTERVAL (Low Priority) ---
            elif (current_time - last_auto_check_time) > PROCESS_INTERVAL_SECONDS:
                print(f"\n--- Auto-Interval Reached ({PROCESS_INTERVAL_SECONDS}s) ---")
                
                # Run the process check
                images_processed = process_available_images(reader, predictor, mqtt_client)
                
                if not images_processed:
                    print("Auto-check: No new images found.")
                
                # Reset the timer
                last_auto_check_time = time.time()

            # --- 3. SHORT SLEEP ---
            # This is critical. Sleep for a tiny amount (0.1s) to prevent 
            # the CPU from running at 100%, but keep the button responsive.
            time.sleep(0.1)

    except KeyboardInterrupt:
        # Ensure GPIO pins are cleaned up when you stop the script with Ctrl+C
        GPIO.cleanup()
        print("\nScript terminated by user. GPIO cleaned up.")
        
    finally:
        try:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
            print("MQTT client disconnected.")
        except:
            pass
        print("Program finished.")
    """
    while True:
    # Check for new images
    images_were_processed = process_available_images(reader, predictor, mqtt_client)
    
    # Capture new image if none found
    if not images_were_processed:
        print("No images to process. Capturing a new one.")
        capture_image(INPUT_IMAGE_DIR)
        print("Processing newly captured image...")
        process_available_images(reader, predictor, mqtt_client)

    print(f"\nWaiting for {PROCESS_INTERVAL_SECONDS} seconds before checking again...")
    time.sleep(PROCESS_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\nExiting program due to user interruption.")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        import traceback
        traceback.print_exc()
        
    finally:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        print("MQTT client disconnected.")
        print("Program finished.")
"""

if __name__ == "__main__":
    main()