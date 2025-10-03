import sys
import os

print("--- Starting self-aware migration script ---")

# Get the absolute path of the directory where this script (migrate.py) is located.
project_root = os.path.dirname(os.path.abspath(__file__))
print(f"Project root identified as: {project_root}")

# Forcefully add the project root directory to the Python path.
sys.path.insert(0, project_root)
print(f"Updated Python Path: {sys.path}")

# Now that the path is guaranteed to be correct, this import will work.
from app import create_app, db
from flask_migrate import upgrade

print("Starting migration...")

# Create an app instance using the application factory
app = create_app()

# Use the app context to run the database upgrade
with app.app_context():
    print("Application context created. Applying database migrations...")
    upgrade()
    print("Migrations applied successfully.")
    print("--- Migration script finished ---")