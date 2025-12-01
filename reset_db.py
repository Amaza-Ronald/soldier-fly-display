# save as reset_database_safe.py
import os
from app import app, db, User

# Stop any existing MQTT threads first
print("Stopping any running MQTT threads...")

with app.app_context():
    # Close all database sessions
    db.session.remove()
    
    # Delete the database file
    db_path = 'larvae_monitoring.db'
    if os.path.exists(db_path):
        os.remove(db_path)
        print(f"✅ Removed old database: {db_path}")
    
    # Create new database
    db.create_all()
    print("✅ New database created with updated schema")
    
    # Create test user (without checking if exists since we just reset)
    try:
        admin_user = User(username='admin')
        admin_user.set_password('admin123')
        db.session.add(admin_user)
        db.session.commit()
        print("✅ Admin user created (admin/admin123)")
    except Exception as e:
        print(f"Note: Could not create admin user: {e}")
        db.session.rollback()

print("✅ Database reset complete!")