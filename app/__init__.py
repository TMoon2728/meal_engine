import os
import time
from datetime import date
import stripe
from flask import Flask, g, render_template
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, current_user
from flask_migrate import Migrate
from whitenoise import WhiteNoise
from itsdangerous import URLSafeTimedSerializer
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize extensions
db = SQLAlchemy()
bcrypt = Bcrypt()
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
migrate = Migrate()
s = URLSafeTimedSerializer(os.getenv('SECRET_KEY', 'a_default_secret_key_for_development'))

# Plan constants
PLAN_CREDITS = {
    'free': 5,
    'premium': 50,
    'elite': -1
}

HOUSEHOLD_LIMITS = {
    'free': 2,
    'premium': 5,
    'elite': float('inf')
}

STRIPE_PRICE_IDS = {
    'premium': os.getenv('STRIPE_PREMIUM_PRICE_ID'),
    'elite': os.getenv('STRIPE_ELITE_PRICE_ID')
}

def create_app():
    """Create and configure an instance of the Flask application."""
    # When using an app package, Flask automatically looks for 'templates' and 'static' folders inside it.
    app = Flask(__name__)

    # --- CONFIGURATION ---
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'a_default_secret_key_for_development')
    app.config['STRIPE_SECRET_KEY'] = os.getenv('STRIPE_SECRET_KEY')
    app.config['STRIPE_WEBHOOK_SECRET'] = os.getenv('STRIPE_WEBHOOK_SECRET')
    
    database_url = os.getenv("DATABASE_URL")
    if database_url and database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
        
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url or 'sqlite:///meal_engine.db'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['UPLOAD_FOLDER'] = 'uploads'

    app.config['PLAN_CREDITS'] = PLAN_CREDITS
    app.config['HOUSEHOLD_LIMITS'] = HOUSEHOLD_LIMITS
    app.config['STRIPE_PRICE_IDS'] = STRIPE_PRICE_IDS

    # --- INITIALIZE EXTENSIONS ---
    db.init_app(app)
    bcrypt.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)
    
    # Configure Stripe API key
    stripe.api_key = app.config['STRIPE_SECRET_KEY']

    # --- WSGI MIDDLEWARE ---
    # Correctly configure WhiteNoise to serve from the app's static folder
    app.wsgi_app = WhiteNoise(app.wsgi_app, root='app/static/')

    # --- BLUEPRINTS ---
    with app.app_context():
        # Import models here to avoid circular imports
        from . import models

        @login_manager.user_loader
        def load_user(user_id):
            return db.session.get(models.User, int(user_id))

        # Import and register blueprints
        from .main import main as main_blueprint
        app.register_blueprint(main_blueprint)

        from .auth import auth as auth_blueprint
        app.register_blueprint(auth_blueprint, url_prefix='/auth')

        from .api import api as api_blueprint
        app.register_blueprint(api_blueprint, url_prefix='/api')
        
        from .payments import payments as payments_blueprint
        app.register_blueprint(payments_blueprint)

        # Initialize achievements command
        from .commands import init_achievements_command
        app.cli.add_command(init_achievements_command)

        # --- CONTEXT PROCESSORS & BEFORE REQUEST ---
        @app.context_processor
        def inject_cache_buster():
            return dict(cache_buster=int(time.time()))

        @app.before_request
        def before_request():
            g.current_month_name = date.today().strftime('%B')

        return app