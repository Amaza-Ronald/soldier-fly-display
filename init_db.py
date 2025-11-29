# init_db.py - Initialize PostgreSQL database
import os
import sys

# Add current directory to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from BSFwebdashboard import app, db, User

def init_database():
    with app.app_context():
        try:
            print("🚀 Initializing PostgreSQL database...")
            
            # Drop and recreate tables (clean start)
            db.drop_all()
            db.create_all()
            print("✅ Database tables created successfully!")
            
            # Create test user if none exists
            if not User.query.filter_by(username='testuser').first():
                admin_user = User(username='testuser')
                admin_user.set_password('password')
                db.session.add(admin_user)
                db.session.commit()
                print("✅ Test user 'testuser' with password 'password' created.")
            else:
                print("✅ Test user already exists.")
                
            print("🎉 Database initialization completed!")
            
        except Exception as e:
            db.session.rollback()
            print(f"❌ Database initialization failed: {e}")
            # Don't raise the exception - let the build continue
            print("⚠️  Continuing build despite database error...")

if __name__ == '__main__':
    init_database()