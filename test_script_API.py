import requests
import base64
import json
import os

# Configuration
BASE_URL = "http://localhost:8000"  # Your local Flask app
LOGIN_URL = f"{BASE_URL}/login"
UPLOAD_URL = f"{BASE_URL}/api/upload"

# Test credentials (use your actual test user)
USERNAME = "testuser"
PASSWORD = "password"

def test_image_upload():
    # Step 1: Login to get session
    session = requests.Session()
    
    print("Step 1: Logging in...")
    login_data = {
        "username": "admin",
        "password": "admin123"
    }
    
    try:
        login_response = session.post(LOGIN_URL, data=login_data)
        if login_response.status_code == 200:
            print("✅ Login successful")
        else:
            print(f"❌ Login failed: {login_response.status_code}")
            return
    except Exception as e:
        print(f"❌ Cannot connect to server: {e}")
        print("Make sure your Flask app is running on localhost:8000")
        return

    # Step 2: Prepare test image
    print("\nStep 2: Preparing test image...")
    
    # Option A: Use an existing image file
    image_path = "test_image.jpg"  # Change this to your test image path
    
    if not os.path.exists(image_path):
        # Option B: Create a simple test image programmatically
        print("Creating a test image...")
        from PIL import Image, ImageDraw
        img = Image.new('RGB', (400, 300), color='green')
        draw = ImageDraw.Draw(img)
        draw.rectangle([50, 50, 150, 100], fill='red', outline='white')
        draw.text((100, 150), "Test Image", fill='white')
        img.save(image_path)
        print(f"Created test image: {image_path}")
    
    # Read and encode image
    with open(image_path, "rb") as f:
        image_data_base64 = base64.b64encode(f.read()).decode('utf-8')
    
    # Step 3: Prepare upload payload
    print("\nStep 3: Preparing upload data...")
    payload = {
        'image_data': image_data_base64,
        'tray_number': 1,  # Using 999 for test data
        'count': 3,
        'avg_length': 15.5,
        'avg_weight': 120.3,
        'bounding_boxes': json.dumps([[10, 20, 50, 60], [30, 40, 70, 80], [100, 120, 180, 200]]),
        'masks': json.dumps([[[10, 20], [30, 40], [50, 60]], [[70, 80], [90, 100], [110, 120]]])
    }
    
    print(f"Image size: {len(image_data_base64)} characters")
    print(f"Tray number: {payload['tray_number']}")
    
    # Step 4: Upload image
    print("\nStep 4: Uploading image...")
    try:
        headers = {'Content-Type': 'application/json'}
        response = session.post(UPLOAD_URL, json=payload, headers=headers)
        
        print(f"Status Code: {response.status_code}")
        print(f"Response: {response.text}")
        
        if response.status_code == 200:
            result = response.json()
            print("✅ Upload successful!")
            print(f"   Image ID: {result.get('image_id')}")
            print(f"   Message: {result.get('message')}")
        else:
            print("❌ Upload failed!")
            
    except Exception as e:
        print(f"❌ Upload error: {e}")

if __name__ == "__main__":
    test_image_upload()