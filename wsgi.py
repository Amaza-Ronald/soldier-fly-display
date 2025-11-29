from BSFwebdashboard import app, start_mqtt_thread
import time

# Start MQTT when the app loads (for production)
print("🚀 Initializing MQTT subscriber in production...")
start_mqtt_thread()

# Give MQTT a moment to start
time.sleep(3)

if __name__ == "__main__":
    app.run()