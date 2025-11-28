#!/bin/bash
# Install dependencies
pip install -r requirements.txt

# Initialize database
python -c "
from BSFwebdashboard import app, db, User
with app.app_context():
    db.create_all()
    if not User.query.filter_by(username='testuser').first():
        from werkzeug.security import generate_password_hash
        admin_user = User(username='testuser')
        admin_user.set_password('password')
        db.session.add(admin_user)
        db.session.commit()
        print('Default user created: testuser/password')
"