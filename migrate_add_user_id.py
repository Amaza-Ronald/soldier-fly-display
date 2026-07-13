#!/usr/bin/env python3
"""
Migration script to add user_id column to LarvaeData and ImageFile tables
Assigns all existing data to testuser (user_id=1)
"""
from BSFwebdashboard import app, db, User, LarvaeData, ImageFile
from sqlalchemy import text

def migrate():
    with app.app_context():
        print("🚀 Starting migration: Adding user_id to tables...")
        
        # Get testuser ID (should be 1)
        testuser = User.query.filter_by(username='testuser').first()
        if not testuser:
            print("❌ Error: testuser not found! Creating testuser...")
            testuser = User(username='testuser', email='test@example.com', is_verified=True)
            testuser.set_password('test123')
            db.session.add(testuser)
            db.session.commit()
        
        print(f"✅ Found testuser with ID: {testuser.id}")
        
        # Check if columns already exist
        try:
            with db.engine.connect() as conn:
                # Check LarvaeData
                result = conn.execute(text("PRAGMA table_info(larvae_data)"))
                larvae_columns = [row[1] for row in result]
                
                if 'user_id' not in larvae_columns:
                    print("📝 Adding user_id to larvae_data table...")
                    conn.execute(text(f"ALTER TABLE larvae_data ADD COLUMN user_id INTEGER DEFAULT {testuser.id} NOT NULL"))
                    conn.execute(text("CREATE INDEX idx_larvae_user_tray ON larvae_data(user_id, tray_number)"))
                    conn.commit()
                    print("✅ Added user_id to larvae_data")
                else:
                    print("⚠️ user_id already exists in larvae_data")
                
                # Check ImageFile
                result = conn.execute(text("PRAGMA table_info(image_files)"))
                image_columns = [row[1] for row in result]
                
                if 'user_id' not in image_columns:
                    print("📝 Adding user_id to image_files table...")
                    conn.execute(text(f"ALTER TABLE image_files ADD COLUMN user_id INTEGER DEFAULT {testuser.id} NOT NULL"))
                    conn.execute(text("CREATE INDEX idx_image_user_tray ON image_files(user_id, tray_number)"))
                    conn.commit()
                    print("✅ Added user_id to image_files")
                else:
                    print("⚠️ user_id already exists in image_files")
        
        except Exception as e:
            print(f"❌ Migration error: {e}")
            raise
        
        # Verify data counts
        larvae_count = LarvaeData.query.filter_by(user_id=testuser.id).count()
        image_count = ImageFile.query.filter_by(user_id=testuser.id).count()
        
        print(f"\n✅ Migration complete!")
        print(f"   - LarvaeData records for testuser: {larvae_count}")
        print(f"   - ImageFile records for testuser: {image_count}")
        print(f"\n🎉 All existing data is now owned by testuser (ID: {testuser.id})")

if __name__ == "__main__":
    migrate()
