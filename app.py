from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response, send_from_directory, session, g
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import desc, and_, func
from datetime import date, timedelta, datetime
import os, csv, random, json, io, re, uuid, calendar, time
from werkzeug.utils import secure_filename
import google.generativeai as genai
from dotenv import load_dotenv
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
import pint
from functools import wraps

from itsdangerous import URLSafeTimedSerializer
import smtplib
from email.message import EmailMessage

from whitenoise import WhiteNoise

import requests
from bs4 import BeautifulSoup
import stripe

import requests
from bs4 import BeautifulSoup
import stripe
from flask_migrate import Migrate 

load_dotenv()
app = Flask(__name__)

app.wsgi_app = WhiteNoise(app.wsgi_app, root='static/')

app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'a_default_secret_key_for_development')

app.config['STRIPE_SECRET_KEY'] = os.getenv('STRIPE_SECRET_KEY')
app.config['STRIPE_WEBHOOK_SECRET'] = os.getenv('STRIPE_WEBHOOK_SECRET')
stripe.api_key = app.config['STRIPE_SECRET_KEY']

PLAN_CREDITS = {
    'free': 5,
    'premium': 50,
    'elite': -1  # Use -1 to represent unlimited
}

HOUSEHOLD_LIMITS = {
    'free': 2,
    'premium': 5,
    'elite': float('inf')
}
app.config['HOUSEHOLD_LIMITS'] = HOUSEHOLD_LIMITS


STRIPE_PRICE_IDS = {
    'premium': os.getenv('STRIPE_PREMIUM_PRICE_ID'),
    'elite': os.getenv('STRIPE_ELITE_PRICE_ID')
}
app.config['PLAN_CREDITS'] = PLAN_CREDITS

database_url = os.getenv("DATABASE_URL")
if database_url and database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url or 'sqlite:///meal_engine.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'uploads'
db = SQLAlchemy(app)
migrate = Migrate(app, db)
bcrypt = Bcrypt(app)

s = URLSafeTimedSerializer(app.config['SECRET_KEY'])

@app.context_processor
def inject_cache_buster():
    return dict(cache_buster=int(time.time()))

@app.before_request
def before_request():
    g.current_month_name = date.today().strftime('%B')

def deduct_ai_credit(user):
    if user.subscription_plan != 'elite' and user.ai_credits > 0:
        user.ai_credits -= 1

def require_ai_credits(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if current_user.subscription_plan != 'elite' and current_user.ai_credits <= 0:
            if request.path.startswith('/api/'):
                return jsonify({
                    'error': 'You have run out of AI credits for this month.',
                    'redirect_url': url_for('pricing')
                }), 403
            else:
                flash('You have run out of AI credits. Please upgrade your plan to continue.', 'warning')
                return redirect(url_for('pricing'))
        return f(*args, **kwargs)
    return decorated_function

def convert_quantity_to_float(quantity_str):
    if not isinstance(quantity_str, str):
        try:
            return float(quantity_str)
        except (ValueError, TypeError):
            return 0.0
    
    try:
        unicodes = {'½': 0.5, '⅓': 0.33, '⅔': 0.67, '¼': 0.25, '¾': 0.75, '⅕': 0.2}
        if quantity_str in unicodes:
            return unicodes[quantity_str]

        if ' ' in quantity_str and '/' in quantity_str:
            parts = quantity_str.split(' ')
            whole_num = float(parts[0])
            frac_parts = parts[1].split('/')
            numerator = float(frac_parts[0])
            denominator = float(frac_parts[1])
            return whole_num + (numerator / denominator)
        elif '/' in quantity_str:
            frac_parts = quantity_str.split('/')
            numerator = float(frac_parts[0])
            denominator = float(frac_parts[1])
            return numerator / denominator
        else:
            return float(quantity_str)
    except (ValueError, ZeroDivisionError):
        return 0.0

ureg = pint.UnitRegistry()

cooking_conversions = {
    'all_purpose_flour': {'cup': '120 * gram'}, 'bread_flour': {'cup': '127 * gram'},
    'cake_flour': {'cup': '113 * gram'}, 'whole_wheat_flour': {'cup': '113 * gram'},
    'granulated_sugar': {'cup': '200 * gram'}, 'brown_sugar': {'cup': '213 * gram'},
    'powdered_sugar': {'cup': '113 * gram'}, 'baking_soda': {'cup': '220 * gram'},
    'baking_powder': {'cup': '184 * gram'}, 'cocoa_powder': {'cup': '85 * gram'},
    'cornstarch': {'cup': '113 * gram'}, 'salt': {'cup': '273 * gram'},
    'butter': {'cup': '227 * gram'}, 'oil': {'cup': '213 * gram'},
    'water': {'cup': '236 * gram'}, 'milk': {'cup': '241 * gram'},
    'heavy_cream': {'cup': '232 * gram'}, 'honey': {'cup': '340 * gram'},
    'molasses': {'cup': '340 * gram'}, 'chopped_nuts': {'cup': '113 * gram'},
    'oats': {'cup': '85 * gram'}, 'rice_uncooked': {'cup': '184 * gram'},
    'rice_cooked': {'cup': '170 * gram'}, 'parmesan_cheese': {'cup': '100 * gram'},
    'cooked_chicken': {'cup': '140 * gram'},
}

densities = {
    substance: ureg.parse_expression(conversions['cup']) / (1 * ureg.cup)
    for substance, conversions in cooking_conversions.items()
}

def mass_to_volume(ureg, quantity, substance):
    return quantity / densities[substance]
            
def volume_to_mass(ureg, quantity, substance):
    return quantity * densities[substance]
            
cooking_context = pint.Context('cooking')
cooking_context.add_transformation('[mass]', '[volume]', mass_to_volume)
cooking_context.add_transformation('[volume]', '[mass]', volume_to_mass)

ureg.add_context(cooking_context)

def sanitize_unit(unit_str):
    if not unit_str: return "dimensionless"
    unit_str = unit_str.lower().strip()
    
    unit_map = {
        'oz': 'fluid_ounce', 'ounce': 'fluid_ounce', 'ounces': 'fluid_ounce',
        'lb': 'pound', 'lbs': 'pound',
        'cup': 'cup', 'cups': 'cup',
        'tsp': 'teaspoon', 'tsps': 'teaspoon', 'teaspoons': 'teaspoon',
        'tbsp': 'tablespoon', 'tbsps': 'tablespoon', 'tablespoons': 'tablespoon',
        'g': 'gram', 'grams': 'gram',
        'kg': 'kilogram', 'kgs': 'kilogram',
        'ml': 'milliliter', 'milliliters': 'milliliter'
    }
    return unit_map.get(unit_str, unit_str)


login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

try:
    genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
except Exception as e:
    print(f"Error configuring Google AI: {e}")

class Household(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    recipes = db.relationship('Recipe', backref='household', lazy=True, cascade="all, delete-orphan")
    pantry_items = db.relationship('PantryItem', backref='household', lazy=True, cascade="all, delete-orphan")
    meal_plans = db.relationship('MealPlan', backref='household', lazy=True, cascade="all, delete-orphan")
    invitations = db.relationship('HouseholdInvitation', backref='household', lazy=True, cascade="all, delete-orphan")
    saved_meals = db.relationship('SavedMeal', backref='household', lazy=True, cascade="all, delete-orphan")
    historical_plans = db.relationship('HistoricalPlan', backref='household', lazy=True, cascade="all, delete-orphan")
    grocery_stores = db.relationship('GroceryStore', backref='household', lazy=True, cascade="all, delete-orphan")
    shopping_list_items = db.relationship('ShoppingListItem', backref='household', lazy=True, cascade="all, delete-orphan")


class HouseholdInvitation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False)
    token = db.Column(db.String(100), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(150), nullable=False)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'))
    household = db.relationship('Household', backref='members')
    recipes = db.relationship('Recipe', backref='author', lazy=True)
    
    subscription_plan = db.Column(db.String(50), nullable=False, default='free')
    stripe_customer_id = db.Column(db.String(255), unique=True, nullable=True)
    stripe_subscription_id = db.Column(db.String(255), unique=True, nullable=True)
    ai_credits = db.Column(db.Integer, nullable=False, default=5)

    @property
    def is_premium_or_elite(self):
        return self.subscription_plan in ['premium', 'elite']

class GroceryStore(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    search_url = db.Column(db.String(500), nullable=False)

class ShoppingListItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    category = db.Column(db.String(100), nullable=False, default='Other')
    is_checked = db.Column(db.Boolean, default=False)

class Recipe(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    instructions = db.Column(db.Text, nullable=False)
    servings = db.Column(db.Integer)
    prep_time = db.Column(db.String(50))
    cook_time = db.Column(db.String(50))
    is_favorite = db.Column(db.Boolean, default=False, nullable=False)
    meal_type = db.Column(db.String(50), nullable=False, default='Main Course')
    rating = db.Column(db.Integer, nullable=False, default=0)
    ingredients = db.relationship('RecipeIngredient', backref='recipe', lazy=True, cascade="all, delete-orphan")
    calories = db.Column(db.Float, nullable=True)
    protein = db.Column(db.Float, nullable=True)
    fat = db.Column(db.Float, nullable=True)
    carbs = db.Column(db.Float, nullable=True)

class Ingredient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    category = db.Column(db.String(50), nullable=True, default='Pantry')
    recipe_links = db.relationship('RecipeIngredient', backref='ingredient', lazy=True)
    pantry_items = db.relationship('PantryItem', backref='ingredient', lazy=True)

class RecipeIngredient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    recipe_id = db.Column(db.Integer, db.ForeignKey('recipe.id'), nullable=False)
    ingredient_id = db.Column(db.Integer, db.ForeignKey('ingredient.id'), nullable=False)
    quantity = db.Column(db.Float, nullable=False, default=0)
    unit = db.Column(db.String(50), nullable=True)

class MealPlan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False)
    meal_date = db.Column(db.Date, nullable=False)
    recipe_id = db.Column(db.Integer, db.ForeignKey('recipe.id'), nullable=True)
    recipe = db.relationship('Recipe')
    custom_item_name = db.Column(db.String(150), nullable=True)
    meal_slot = db.Column(db.String(50), nullable=False, default='Dinner')
    is_eaten = db.Column(db.Boolean, default=False, nullable=False)

class PantryItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False)
    ingredient_id = db.Column(db.Integer, db.ForeignKey('ingredient.id'), nullable=False)
    quantity = db.Column(db.Float, nullable=False, default=0)
    unit = db.Column(db.String(50), nullable=True)
    date_updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class SavedMeal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False)
    recipes = db.relationship('Recipe', secondary='saved_meal_recipe_link')

class SavedMealRecipeLink(db.Model):
    __tablename__ = 'saved_meal_recipe_link'
    saved_meal_id = db.Column(db.Integer, db.ForeignKey('saved_meal.id'), primary_key=True)
    recipe_id = db.Column(db.Integer, db.ForeignKey('recipe.id'), primary_key=True)

class HistoricalPlan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False)
    entries = db.relationship('HistoricalPlanEntry', backref='historical_plan', lazy=True, cascade="all, delete-orphan")

class HistoricalPlanEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    historical_plan_id = db.Column(db.Integer, db.ForeignKey('historical_plan.id'), nullable=False)
    day_of_week = db.Column(db.Integer, nullable=False)
    meal_slot = db.Column(db.String(50), nullable=False)
    recipe_id = db.Column(db.Integer, db.ForeignKey('recipe.id'), nullable=True)
    custom_item_name = db.Column(db.String(150), nullable=True)
    recipe = db.relationship('Recipe')

def send_reset_email(user_email, token):
    msg = EmailMessage()
    msg['Subject'] = 'Password Reset Request for Meal Engine'
    msg['From'] = os.getenv('MAIL_USERNAME')
    msg['To'] = user_email
    
    reset_url = url_for('reset_password', token=token, _external=True)
    
    msg.set_content(f"Hello,\n\nA password reset has been requested for your Meal Engine account.\nPlease click the link below to reset your password. This link is valid for 30 minutes.\n\n{reset_url}\n\nIf you did not request this, please ignore this email.\n\nThanks,\nThe Meal Engine Team")

    try:
        with smtplib.SMTP(os.getenv('MAIL_SERVER'), int(os.getenv('MAIL_PORT'))) as server:
            if os.getenv('MAIL_USE_TLS').lower() == 'true':
                server.starttls()
            server.login(os.getenv('MAIL_USERNAME'), os.getenv('MAIL_PASSWORD'))
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"Failed to send email: {e}")
        return False

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first()
        if user and bcrypt.check_password_hash(user.password, password):
            login_user(user, remember=True)
            next_page = request.args.get('next')
            return redirect(next_page) if next_page else redirect(url_for('index'))
        else:
            flash('Login Unsuccessful. Please check email and password', 'danger')
    return render_template('login.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        if User.query.filter_by(email=email).first():
            flash('Email address already in use.', 'warning')
            return redirect(url_for('signup'))
        
        new_household = Household(name=f"{email.split('@')[0]}'s Household")
        db.session.add(new_household)
        db.session.flush()

        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
        user = User(
            email=email,
            password=hashed_password,
            household_id=new_household.id,
            ai_credits=PLAN_CREDITS['free']
        )
        db.session.add(user)
        db.session.commit()
        
        flash('Your account has been created! You can now log in.', 'success')
        return redirect(url_for('login'))
    return render_template('signup.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        email = request.form.get('email')
        user = User.query.filter_by(email=email).first()
        if user:
            token = s.dumps(email, salt='password-reset-salt')
            if send_reset_email(email, token):
                flash('A password reset link has been sent to your email.', 'info')
            else:
                flash('There was an error sending the email. Please try again later.', 'danger')
        else:
            flash('A password reset link has been sent to your email.', 'info')
        return redirect(url_for('login'))
    return render_template('forgot_password.html')


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    try:
        email = s.loads(token, salt='password-reset-salt', max_age=1800)
    except:
        flash('The password reset link is invalid or has expired.', 'warning')
        return redirect(url_for('forgot_password'))

    user = User.query.filter_by(email=email).first()
    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('login'))

    if request.method == 'POST':
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        if password != confirm_password:
            flash('Passwords do not match.', 'danger')
            return render_template('reset_password.html')

        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
        user.password = hashed_password
        db.session.commit()
        flash('Your password has been successfully updated! Please log in.', 'success')
        return redirect(url_for('login'))

    return render_template('reset_password.html')

@app.route('/pricing')
@login_required
def pricing():
    return render_template('pricing.html', stripe_price_ids=STRIPE_PRICE_IDS)

@app.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    if current_user.stripe_subscription_id:
        flash("You already have an active subscription. Please manage it from your profile.", "info")
        return redirect(url_for('profile'))

    price_id = request.form.get('price_id')
    
    try:
        customer_id = current_user.stripe_customer_id
        
        if customer_id:
            try:
                stripe.Customer.retrieve(customer_id)
            except stripe.error.InvalidRequestError:
                print(f"Stale Stripe customer ID '{customer_id}' detected. Clearing.")
                customer_id = None
                current_user.stripe_customer_id = None
                db.session.commit()

        if not customer_id:
            customer = stripe.Customer.create(
                email=current_user.email,
                name=current_user.email.split('@')[0]
            )
            customer_id = customer.id
            current_user.stripe_customer_id = customer_id
            db.session.commit()
            print(f"Created new Stripe customer ID: {customer_id}")

        checkout_session = stripe.checkout.Session.create(
            customer=customer_id,
            client_reference_id=current_user.id,
            line_items=[{'price': price_id, 'quantity': 1}],
            mode='subscription',
            success_url=url_for('index', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=url_for('pricing', _external=True),
            allow_promotion_codes=True,
        )
        return redirect(checkout_session.url, code=303)

    except Exception as e:
        flash(f'Error creating checkout session: {str(e)}', 'danger')
        return redirect(url_for('pricing'))

def _update_user_subscription(user, subscription):
    price_id = subscription['items']['data'][0]['price']['id']
    
    print("\n--- Attempting to update user subscription ---")
    print(f"User: {user.email} (ID: {user.id})")
    print(f"Stripe Subscription ID: {subscription.id}")
    print(f"Received Price ID from Stripe: {price_id}")
    print(f"Expected Premium Price ID: {STRIPE_PRICE_IDS.get('premium')}")
    print(f"Expected Elite Price ID: {STRIPE_PRICE_IDS.get('elite')}")

    new_plan = 'free'
    if price_id == STRIPE_PRICE_IDS.get('premium'):
        new_plan = 'premium'
    elif price_id == STRIPE_PRICE_IDS.get('elite'):
        new_plan = 'elite'
    else:
        print(f"!!! CRITICAL WARNING: Price ID '{price_id}' does not match any known plan IDs. User will be set to 'free'.")

    user.subscription_plan = new_plan
    user.stripe_subscription_id = subscription.id
    user.stripe_customer_id = subscription.customer
    user.ai_credits = PLAN_CREDITS[new_plan]
    db.session.commit()
    print(f"--- User {user.email} successfully updated to '{new_plan}' plan in database. ---")

@app.route('/stripe-webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    webhook_secret = app.config['STRIPE_WEBHOOK_SECRET']
    
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        print(f"!!! ERROR: Invalid webhook signature: {e}")
        return 'Invalid signature', 400

    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        user_id = session.get('client_reference_id')
        
        if user_id:
            with app.app_context():
                user = db.session.get(User, int(user_id))
                if user:
                    subscription = stripe.Subscription.retrieve(session.get('subscription'))
                    _update_user_subscription(user, subscription)
                else:
                    print(f"!!! CRITICAL ERROR: Webhook user ID {user_id} not found in database.")
        else:
            print("!!! CRITICAL ERROR: Webhook received without client_reference_id.")

    if event['type'] in ['customer.subscription.updated', 'customer.subscription.deleted']:
        with app.app_context():
            subscription = event['data']['object']
            user = User.query.filter_by(stripe_subscription_id=subscription.id).first()
            if user:
                if subscription.get('cancel_at_period_end') or event['type'] == 'customer.subscription.deleted':
                    user.subscription_plan = 'free'
                    user.stripe_subscription_id = None
                    user.ai_credits = PLAN_CREDITS['free']
                    db.session.commit()
                    print(f"--- User {user.email}'s subscription cancelled. Downgraded to free. ---")
                else:
                    _update_user_subscription(user, subscription)

    if event['type'] == 'invoice.payment_succeeded':
        with app.app_context():
            invoice = event['data']['object']
            customer_id = invoice.get('customer')
            user = User.query.filter_by(stripe_customer_id=customer_id).first()
            if user and user.subscription_plan in PLAN_CREDITS:
                user.ai_credits = PLAN_CREDITS[user.subscription_plan]
                db.session.commit()
                print(f"--- AI credits for {user.email} have been reset for the new billing cycle. ---")

    return 'Success', 200

@app.route('/create-billing-portal-session', methods=['POST'])
@login_required
def create_billing_portal_session():
    if not current_user.stripe_customer_id or not current_user.stripe_subscription_id:
        flash('No billing information found for your account.', 'warning')
        return redirect(url_for('profile'))

    try:
        subscription = stripe.Subscription.retrieve(current_user.stripe_subscription_id)
        price_id = subscription['items']['data'][0]['price']['id']
        
        correct_plan = 'free'
        if price_id == STRIPE_PRICE_IDS['premium']: correct_plan = 'premium'
        elif price_id == STRIPE_PRICE_IDS['elite']: correct_plan = 'elite'
        
        if current_user.subscription_plan != correct_plan:
            current_user.subscription_plan = correct_plan
            current_user.ai_credits = PLAN_CREDITS[correct_plan]
            db.session.commit()
            flash('Your plan information was out of sync and has been corrected.', 'info')
            print(f"Corrected plan for {current_user.email} to {correct_plan}.")

        return_url = url_for('profile', _external=True)
        portal_session = stripe.billing_portal.Session.create(
            customer=current_user.stripe_customer_id,
            return_url=return_url
        )
        return redirect(portal_session.url, code=303)

    except stripe.error.InvalidRequestError as e:
        print(f"Stale subscription ID detected for user {current_user.email}. Error: {e}")
        current_user.subscription_plan = 'free'
        current_user.stripe_subscription_id = None
        current_user.ai_credits = PLAN_CREDITS['free']
        db.session.commit()
        flash('Your subscription data was out of sync and has been reset. Please upgrade your plan again.', 'warning')
        return redirect(url_for('pricing'))
    except Exception as e:
        flash(f"An unexpected error occurred: {str(e)}", "danger")
        return redirect(url_for('profile'))

@app.route('/')
def index():
    if not current_user.is_authenticated:
        return render_template('landing_page.html')
    
    today = date.today()
    start_of_week = today - timedelta(days=today.weekday())
    end_of_week = start_of_week + timedelta(days=6)
    
    start_of_month = today.replace(day=1)
    _, days_in_month = calendar.monthrange(today.year, today.month)
    end_of_month = today.replace(day=days_in_month)

    all_user_recipes = Recipe.query.filter_by(household_id=current_user.household_id).all()
    pantry_items_in_stock = PantryItem.query.filter_by(household_id=current_user.household_id).filter(PantryItem.quantity > 0).all()
    pantry_ingredient_ids = {item.ingredient_id for item in pantry_items_in_stock}
    recipes_can_make_count = 0
    for recipe in all_user_recipes:
        required_ingredient_ids = {ri.ingredient_id for ri in recipe.ingredients}
        if required_ingredient_ids and required_ingredient_ids.issubset(pantry_ingredient_ids):
            recipes_can_make_count += 1

    planned_meals_for_shopping = MealPlan.query.filter(
        MealPlan.household_id == current_user.household_id,
        MealPlan.recipe_id.isnot(None),
        MealPlan.meal_date.between(today, end_of_week)
    ).all()
    required = {}
    for meal in planned_meals_for_shopping:
        if not meal.recipe: continue
        for item in meal.recipe.ingredients:
            if not item.quantity or item.quantity == 0: continue
            required[item.ingredient.id] = required.get(item.ingredient.id, 0) + item.quantity
    
    items_to_buy_count = 0
    for ing_id, needed_qty in required.items():
        pantry_item = PantryItem.query.filter_by(household_id=current_user.household_id, ingredient_id=ing_id).first()
        if not pantry_item or pantry_item.quantity < needed_qty:
            items_to_buy_count += 1
    
    manual_shopping_items = ShoppingListItem.query.filter_by(household_id=current_user.household_id).count()
    items_to_buy_count += manual_shopping_items

    most_made_recipes = db.session.query(
        Recipe,
        func.count(MealPlan.recipe_id).label('meal_count')
    ).join(MealPlan, Recipe.id == MealPlan.recipe_id)\
    .filter(Recipe.household_id == current_user.household_id)\
    .group_by(Recipe.id)\
    .order_by(desc('meal_count'))\
    .limit(5).all()

    kitchen_stats = {
        'total_recipes': len(all_user_recipes),
        'pantry_items': len(pantry_items_in_stock),
        'favorite_recipes': Recipe.query.filter_by(household_id=current_user.household_id, is_favorite=True).count(),
        'recipes_can_make': recipes_can_make_count,
        'items_to_buy': items_to_buy_count
    }

    todays_meal_plan = MealPlan.query.filter_by(
        household_id=current_user.household_id,
        meal_date=today,
        meal_slot='Dinner'
    ).first()

    weekly_planned_meals = MealPlan.query.filter(
        MealPlan.household_id == current_user.household_id,
        MealPlan.meal_date.between(start_of_week, end_of_week),
        MealPlan.recipe_id.isnot(None)
    ).all()
    
    monthly_planned_meals = MealPlan.query.filter(
        MealPlan.household_id == current_user.household_id,
        MealPlan.meal_date.between(start_of_month, end_of_month),
        MealPlan.recipe_id.isnot(None)
    ).all()

    weekly_stats = {
        'scheduled': {'calories': 0, 'protein': 0, 'fat': 0, 'carbs': 0},
        'consumed': {'calories': 0, 'protein': 0, 'fat': 0, 'carbs': 0}
    }
    for meal in weekly_planned_meals:
        if meal.recipe and meal.recipe.calories:
            weekly_stats['scheduled']['calories'] += meal.recipe.calories
            weekly_stats['scheduled']['protein'] += meal.recipe.protein or 0
            weekly_stats['scheduled']['fat'] += meal.recipe.fat or 0
            weekly_stats['scheduled']['carbs'] += meal.recipe.carbs or 0
            if meal.is_eaten:
                weekly_stats['consumed']['calories'] += meal.recipe.calories
                weekly_stats['consumed']['protein'] += meal.recipe.protein or 0
                weekly_stats['consumed']['fat'] += meal.recipe.fat or 0
                weekly_stats['consumed']['carbs'] += meal.recipe.carbs or 0

    monthly_stats = {
        'scheduled': {'calories': 0, 'protein': 0, 'fat': 0, 'carbs': 0},
        'consumed': {'calories': 0, 'protein': 0, 'fat': 0, 'carbs': 0}
    }
    for meal in monthly_planned_meals:
        if meal.recipe and meal.recipe.calories:
            monthly_stats['scheduled']['calories'] += meal.recipe.calories
            monthly_stats['scheduled']['protein'] += meal.recipe.protein or 0
            monthly_stats['scheduled']['fat'] += meal.recipe.fat or 0
            monthly_stats['scheduled']['carbs'] += meal.recipe.carbs or 0
            if meal.is_eaten:
                monthly_stats['consumed']['calories'] += meal.recipe.calories
                monthly_stats['consumed']['protein'] += meal.recipe.protein or 0
                monthly_stats['consumed']['fat'] += meal.recipe.fat or 0
                monthly_stats['consumed']['carbs'] += meal.recipe.carbs or 0

    return render_template('index.html',
                           todays_meal_plan=todays_meal_plan,
                           weekly_stats=weekly_stats,
                           monthly_stats=monthly_stats,
                           kitchen_stats=kitchen_stats,
                           most_made_recipes=most_made_recipes)


@app.route('/recipes')
@login_required
def list_recipes():
    query = request.args.get('query', '')
    pantry_filter_active = request.args.get('filter') == 'pantry'
    favorites_filter_active = request.args.get('filter') == 'favorites'
    sort_order = request.args.get('sort', 'asc')
    
    base_query = Recipe.query.filter_by(household_id=current_user.household_id)
    
    if sort_order == 'desc':
        base_query = base_query.order_by(desc(Recipe.name))
    elif sort_order == 'rating':
        base_query = base_query.order_by(desc(Recipe.rating), Recipe.name)
    else:
        base_query = base_query.order_by(Recipe.name)

    if pantry_filter_active:
        pantry_items_in_stock = PantryItem.query.filter_by(household_id=current_user.household_id).filter(PantryItem.quantity > 0).all()
        pantry_ingredient_ids = {item.ingredient_id for item in pantry_items_in_stock}
        all_user_recipes = base_query.all()
        recipes = []
        for recipe in all_user_recipes:
            required_ingredient_ids = {ri.ingredient_id for ri in recipe.ingredients}
            if required_ingredient_ids and required_ingredient_ids.issubset(pantry_ingredient_ids):
                recipes.append(recipe)
    elif favorites_filter_active:
        recipes = base_query.filter_by(is_favorite=True).all()
    elif query:
        search_term = f"%{query}%"
        recipes = base_query.filter(db.or_(Recipe.name.ilike(search_term), Recipe.instructions.ilike(search_term))).all()
    else:
        recipes = base_query.all()
        
    return render_template('recipes.html', page_class='page-recipes', recipes=recipes, query=query, pantry_filter_active=pantry_filter_active, favorites_filter_active=favorites_filter_active, sort_order=sort_order)


@app.route('/ai-quick-add', methods=['POST'])
@login_required
@require_ai_credits
def ai_quick_add():
    recipe_name = request.form.get('recipe_name')
    if not recipe_name:
        flash('Please enter a recipe name.', 'warning')
        return redirect(url_for('list_recipes'))
    if Recipe.query.filter_by(name=recipe_name, household_id=current_user.household_id).first():
        flash('A recipe with this name already exists.', 'info')
        return redirect(url_for('list_recipes'))

    prompt = f"""
        Generate a standard recipe for "{recipe_name}".
        Your output must be a single, valid JSON object with the following keys:
        - "name": The title of the recipe.
        - "instructions": A single string with steps separated by '\\n'.
        - "meal_type": Must be one of 'Main Course', 'Side Dish', 'Dessert', 'Snack', or 'Meal Prep'.
        - "ingredients": An array of objects, where each object has "name", "quantity", and "unit".
    """
    
    try:
        model = genai.GenerativeModel('gemini-2.5-pro')
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                response_mime_type="application/json"
            )
        )
        
        recipe_data = json.loads(response.text)

        if not recipe_data.get('name') or not recipe_data.get('instructions') or not recipe_data.get('ingredients'):
            flash('The AI returned an incomplete recipe. Please try a different name.', 'warning')
            return redirect(url_for('list_recipes'))

        new_recipe = Recipe(
            name=recipe_data['name'],
            instructions=recipe_data['instructions'],
            meal_type=recipe_data.get('meal_type', 'Main Course'),
            author=current_user,
            household_id=current_user.household_id
        )
        db.session.add(new_recipe)
        db.session.flush()

        for ing_data in recipe_data['ingredients']:
            ingredient_name = ing_data.get('name', '').strip()
            if not ingredient_name: continue

            ingredient_obj = Ingredient.query.filter(db.func.lower(Ingredient.name) == db.func.lower(ingredient_name)).first()
            if not ingredient_obj:
                ingredient_obj = Ingredient(name=ingredient_name)
                db.session.add(ingredient_obj)
                db.session.flush()
            
            quantity_val = convert_quantity_to_float(ing_data.get('quantity', '0'))
            
            recipe_ingredient = RecipeIngredient(
                recipe_id=new_recipe.id,
                ingredient_id=ingredient_obj.id,
                quantity=quantity_val,
                unit=ing_data.get('unit', '')
            )
            db.session.add(recipe_ingredient)
        
        deduct_ai_credit(current_user)
        db.session.commit()
        flash(f'Successfully generated and saved "{recipe_data["name"]}"!', 'success')
    except Exception as e:
        db.session.rollback()
        print(f"AI Quick Add Error: {e}")
        print(f"AI Response Text:\n{response.text if 'response' in locals() else 'No response object'}")
        flash('The AI failed to generate a valid recipe. Please try again.', 'danger')
        
    return redirect(url_for('list_recipes'))


@app.route('/ingredients', methods=['GET', 'POST'])
@login_required
def list_ingredients():
    if request.method == 'POST':
        name = request.form.get('name')
        if name and not Ingredient.query.filter(db.func.lower(Ingredient.name) == db.func.lower(name.strip())).first():
            db.session.add(Ingredient(name=name.strip().title()))
            db.session.commit()
            flash(f'"{name}" added to master ingredient list.', 'success')
        else:
            flash(f'"{name}" already exists.', 'warning')
        return redirect(url_for('list_ingredients'))
    
    query = request.args.get('query', '')
    stock_filter = request.args.get('filter', 'all')
    
    base_query = Ingredient.query
    if query: base_query = base_query.filter(Ingredient.name.ilike(f"%{query}%"))
    
    pantry_items = {item.ingredient_id: item for item in PantryItem.query.filter_by(household_id=current_user.household_id).all()}
    
    if stock_filter == 'in_pantry':
        ingredient_ids_in_pantry = pantry_items.keys()
        base_query = base_query.filter(Ingredient.id.in_(ingredient_ids_in_pantry))
    
    all_ingredients = base_query.order_by(Ingredient.category, Ingredient.name).all()
    
    ingredient_data = [{'ingredient': ing, 'pantry_item': pantry_items.get(ing.id)} for ing in all_ingredients]
    
    categories = ['Produce', 'Meat & Seafood', 'Dairy & Eggs', 'Pantry', 'Spices & Seasonings', 'Bakery', 'Frozen', 'Other']
    
    return render_template('ingredients.html', ingredient_data=ingredient_data, query=query, stock_filter=stock_filter, categories=categories)

@app.route('/update-ingredient-category', methods=['POST'])
@login_required
def update_ingredient_category():
    ingredient_id = request.form.get('ingredient_id')
    new_category = request.form.get('category')
    
    ingredient = db.session.get(Ingredient, ingredient_id)
    if ingredient:
        ingredient.category = new_category
        db.session.commit()
        flash(f'Updated category for "{ingredient.name}".', 'info')
    
    return redirect(url_for('list_ingredients'))


@app.route('/update-pantry', methods=['POST'])
@login_required
def update_pantry():
    action = request.form.get('action')
    redirect_url = url_for('list_ingredients', filter=request.args.get('filter', 'all'), query=request.args.get('query', ''))

    if action == 'add':
        ingredient_id = int(request.form.get('ingredient_id'))
        quantity = float(request.form.get('quantity', 1))
        unit = request.form.get('unit', '')
        if ingredient_id and not PantryItem.query.filter_by(ingredient_id=ingredient_id, household_id=current_user.household_id).first():
            new_item = PantryItem(ingredient_id=ingredient_id, quantity=quantity, unit=unit, household_id=current_user.household_id)
            db.session.add(new_item)
            db.session.commit()
            flash(f'"{new_item.ingredient.name}" added to pantry.', 'success')

    elif action == 'update_quantity':
        pantry_item_id = request.form.get('pantry_item_id')
        quantity = float(request.form.get('quantity', 0))
        unit = request.form.get('unit', '')
        item = PantryItem.query.filter_by(id=pantry_item_id, household_id=current_user.household_id).first()
        if item:
            item.quantity = quantity
            item.unit = unit
            db.session.commit()
            flash(f'Updated "{item.ingredient.name}".', 'info')
    
    elif action == 'delete':
        pantry_item_id = request.form.get('pantry_item_id')
        item = PantryItem.query.filter_by(id=pantry_item_id, household_id=current_user.household_id).first()
        if item:
            flash(f'"{item.ingredient.name}" removed from pantry.', 'success')
            db.session.delete(item)
            db.session.commit()
            
    return redirect(redirect_url)

@app.route('/recipe/add', methods=['GET', 'POST'])
@login_required
def add_recipe():
    if request.method == 'POST':
        new_recipe = Recipe(
            name=request.form.get('name'),
            instructions=request.form.get('instructions') or "No instructions provided.",
            servings=int(request.form.get('servings')) if request.form.get('servings') else None,
            prep_time=request.form.get('prep_time'),
            cook_time=request.form.get('cook_time'),
            meal_type=request.form.get('meal_type'),
            author=current_user,
            household_id=current_user.household_id
        )
        db.session.add(new_recipe)
        db.session.commit()
        flash('Recipe added successfully! Please add its ingredients below.', 'success')
        return redirect(url_for('edit_recipe', recipe_id=new_recipe.id))
    
    return render_template('add_recipe.html', prefill={})

@app.route('/recipe/<int:recipe_id>')
@login_required
def view_recipe(recipe_id):
    recipe = Recipe.query.filter_by(id=recipe_id, household_id=current_user.household_id).first_or_404()
    return render_template('view_recipe.html', recipe=recipe)

@app.route('/recipe/<int:recipe_id>/cook')
@login_required
def cook_recipe(recipe_id):
    recipe = Recipe.query.filter_by(id=recipe_id, household_id=current_user.household_id).first_or_404()
    steps = [step.strip() for step in recipe.instructions.strip().split('\n') if step.strip()]
    return render_template('cooking_mode.html', recipe=recipe, steps=steps)

@app.route('/recipe/<int:recipe_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_recipe(recipe_id):
    recipe = Recipe.query.filter_by(id=recipe_id, household_id=current_user.household_id).first_or_404()
    ingredients = Ingredient.query.order_by(Ingredient.name).all()
    if request.method == 'POST':
        recipe.name = request.form.get('name')
        recipe.instructions = request.form.get('instructions') or "No instructions provided."
        recipe.servings = request.form.get('servings') if request.form.get('servings') else None
        recipe.prep_time = request.form.get('prep_time')
        recipe.cook_time = request.form.get('cook_time')
        recipe.meal_type = request.form.get('meal_type')
        
        recipe.calories = float(request.form.get('calories')) if request.form.get('calories') else None
        recipe.protein = float(request.form.get('protein')) if request.form.get('protein') else None
        recipe.fat = float(request.form.get('fat')) if request.form.get('fat') else None
        recipe.carbs = float(request.form.get('carbs')) if request.form.get('carbs') else None
        
        RecipeIngredient.query.filter_by(recipe_id=recipe.id).delete()
        ingredient_ids = request.form.getlist('ingredient[]')
        quantities = request.form.getlist('quantity[]')
        units = request.form.getlist('unit[]')
        for i in range(len(ingredient_ids)):
            if ingredient_ids[i] and quantities[i]:
                recipe_ingredient = RecipeIngredient(recipe_id=recipe.id, ingredient_id=int(ingredient_ids[i]), quantity=float(quantities[i]), unit=units[i])
                db.session.add(recipe_ingredient)
        db.session.commit()
        flash('Recipe updated successfully!', 'success')
        return redirect(url_for('view_recipe', recipe_id=recipe.id))
    return render_template('edit_recipe.html', recipe=recipe, ingredients=ingredients)

@app.route('/recipe/<int:recipe_id>/delete', methods=['POST'])
@login_required
def delete_recipe(recipe_id):
    recipe = Recipe.query.filter_by(id=recipe_id, household_id=current_user.household_id).first_or_404()
    db.session.delete(recipe)
    db.session.commit()
    flash('Recipe deleted successfully!', 'success')
    return redirect(url_for('list_recipes'))

@app.route('/saved-meals', methods=['GET', 'POST'])
@login_required
def saved_meals():
    if request.method == 'POST':
        name = request.form.get('name')
        if name:
            new_saved_meal = SavedMeal(name=name, household_id=current_user.household_id)
            db.session.add(new_saved_meal)
            db.session.commit()
            flash(f'Saved Meal "{name}" created. Now add recipes to it.', 'success')
            return redirect(url_for('edit_saved_meal', saved_meal_id=new_saved_meal.id))
    
    all_saved_meals = SavedMeal.query.filter_by(household_id=current_user.household_id).order_by(SavedMeal.name).all()
    return render_template('saved_meals.html', saved_meals=all_saved_meals)

@app.route('/saved-meal/<int:saved_meal_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_saved_meal(saved_meal_id):
    saved_meal = SavedMeal.query.filter_by(id=saved_meal_id, household_id=current_user.household_id).first_or_404()
    
    if request.method == 'POST':
        saved_meal.name = request.form.get('name')
        
        new_recipe_ids = request.form.getlist('recipe_ids')
        saved_meal.recipes = Recipe.query.filter(Recipe.id.in_(new_recipe_ids), Recipe.household_id==current_user.household_id).all()
        
        db.session.commit()
        flash(f'Saved Meal "{saved_meal.name}" updated successfully!', 'success')
        return redirect(url_for('saved_meals'))

    all_recipes = Recipe.query.filter_by(household_id=current_user.household_id).order_by(Recipe.name).all()
    current_recipe_ids = {recipe.id for recipe in saved_meal.recipes}
    
    return render_template('edit_saved_meal.html', saved_meal=saved_meal, all_recipes=all_recipes, current_recipe_ids=current_recipe_ids)

@app.route('/saved-meal/<int:saved_meal_id>/delete', methods=['POST'])
@login_required
def delete_saved_meal(saved_meal_id):
    saved_meal = SavedMeal.query.filter_by(id=saved_meal_id, household_id=current_user.household_id).first_or_404()
    flash(f'Saved Meal "{saved_meal.name}" has been deleted.', 'success')
    db.session.delete(saved_meal)
    db.session.commit()
    return redirect(url_for('saved_meals'))

@app.route('/manage-plans')
@login_required
def manage_plans():
    plans = HistoricalPlan.query.filter_by(household_id=current_user.household_id).order_by(HistoricalPlan.name).all()
    return render_template('manage_plans.html', plans=plans)

@app.route('/delete-plan/<int:plan_id>', methods=['POST'])
@login_required
def delete_historical_plan(plan_id):
    plan = HistoricalPlan.query.filter_by(id=plan_id, household_id=current_user.household_id).first_or_404()
    flash(f'The plan template "{plan.name}" has been deleted.', 'success')
    db.session.delete(plan)
    db.session.commit()
    return redirect(url_for('manage_plans'))


@app.route('/monthly-plan', methods=['GET', 'POST'])
@login_required
def monthly_plan():
    try:
        year = int(request.args.get('year', date.today().year))
        month = int(request.args.get('month', date.today().month))
    except ValueError:
        today = date.today()
        year = today.year
        month = today.month

    if not (1 <= month <= 12): month = date.today().month
    if not (1900 <= year <= 2100): year = date.today().year

    cal = calendar.Calendar(firstweekday=0)
    month_days = cal.monthdatescalendar(year, month)

    first_day_of_calendar = month_days[0][0]
    last_day_of_calendar = month_days[-1][-1]
    
    all_meals_in_view = MealPlan.query.filter(
        MealPlan.household_id == current_user.household_id,
        MealPlan.meal_date.between(first_day_of_calendar, last_day_of_calendar)
    ).all()

    daily_summaries = {}
    for day_row in month_days:
        for day in day_row:
            day_str = day.strftime('%Y-%m-%d')
            daily_summaries[day_str] = {'calories': 0, 'meals': []}

    for meal in all_meals_in_view:
        day_str = meal.meal_date.strftime('%Y-%m-%d')
        if day_str in daily_summaries:
            meal_name = meal.recipe.name if meal.recipe else meal.custom_item_name
            daily_summaries[day_str]['meals'].append(f"{meal.meal_slot}: {meal_name}")
            if meal.recipe and meal.recipe.calories:
                daily_summaries[day_str]['calories'] += meal.recipe.calories
    
    current_month_date = date(year, month, 1)
    prev_month_date = current_month_date - timedelta(days=1)
    next_month_date = current_month_date + timedelta(days=32)
    
    nav = {
        'current': current_month_date,
        'prev': prev_month_date,
        'next': next_month_date
    }
    
    start_of_month = date(year, month, 1)
    _, days_in_month = calendar.monthrange(year, month)
    end_of_month = date(year, month, days_in_month)
    
    all_meals_this_month = [m for m in all_meals_in_view if start_of_month <= m.meal_date <= end_of_month and m.recipe]
    
    monthly_stats = {
        'scheduled': {'calories': 0, 'protein': 0, 'fat': 0, 'carbs': 0},
        'consumed': {'calories': 0, 'protein': 0, 'fat': 0, 'carbs': 0}
    }
    for meal in all_meals_this_month:
        if meal.recipe and meal.recipe.calories:
            monthly_stats['scheduled']['calories'] += meal.recipe.calories
            monthly_stats['scheduled']['protein'] += meal.recipe.protein or 0
            monthly_stats['scheduled']['fat'] += meal.recipe.fat or 0
            monthly_stats['scheduled']['carbs'] += meal.recipe.carbs or 0
            if meal.is_eaten:
                monthly_stats['consumed']['calories'] += meal.recipe.calories
                monthly_stats['consumed']['protein'] += meal.recipe.protein or 0
                monthly_stats['consumed']['fat'] += meal.recipe.fat or 0
                monthly_stats['consumed']['carbs'] += meal.recipe.carbs or 0
            
    weekly_summaries = []
    for week in month_days:
        week_stats = {'scheduled': {'calories': 0}, 'consumed': {'calories': 0}}
        for day in week:
            for meal in all_meals_in_view:
                if meal.meal_date == day and meal.recipe and meal.recipe.calories:
                    week_stats['scheduled']['calories'] += meal.recipe.calories
                    if meal.is_eaten:
                        week_stats['consumed']['calories'] += meal.recipe.calories
        weekly_summaries.append(week_stats)
    
    return render_template('monthly_plan.html', page_class='page-monthly-plan',
                           calendar_data=month_days,
                           daily_summaries=daily_summaries,
                           nav=nav,
                           monthly_stats=monthly_stats,
                           weekly_summaries=weekly_summaries)

@app.route('/ai-architect')
@login_required
def ai_architect():
    today = date.today()
    all_recipes = Recipe.query.filter_by(household_id=current_user.household_id).order_by(Recipe.name).all()
    
    recipes_by_type = {
        'Main Course': [r for r in all_recipes if r.meal_type == 'Main Course'],
        'Side Dish': [r for r in all_recipes if r.meal_type == 'Side Dish'],
        'Snack': [r for r in all_recipes if r.meal_type == 'Snack'],
        'Meal Prep': [r for r in all_recipes if r.meal_type == 'Meal Prep']
    }
    
    recipes_for_js = {
        'Main Course': [{'id': r.id, 'name': r.name, 'meal_type': r.meal_type} for r in recipes_by_type['Main Course']],
        'Side Dish': [{'id': r.id, 'name': r.name, 'meal_type': r.meal_type} for r in recipes_by_type['Side Dish']],
        'Snack': [{'id': r.id, 'name': r.name, 'meal_type': r.meal_type} for r in recipes_by_type['Snack']],
        'Meal Prep': [{'id': r.id, 'name': r.name, 'meal_type': r.meal_type} for r in recipes_by_type['Meal Prep']]
    }

    return render_template('ai_architect.html', today=today, calendar=calendar, recipes_for_js=recipes_for_js)

@app.route('/household', methods=['GET'])
@login_required
def household_page():
    return redirect(url_for('profile'))

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    household_owner = current_user.household.members[0]
    member_limit = app.config['HOUSEHOLD_LIMITS'].get(household_owner.subscription_plan, 2)
    is_full = len(current_user.household.members) >= member_limit

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'generate_invite':
            if is_full:
                return jsonify({'error': f"Your household is full. The '{household_owner.subscription_plan}' plan allows for {int(member_limit)} members. The household owner needs to upgrade to add more."}), 403
            
            HouseholdInvitation.query.filter_by(household_id=current_user.household_id).delete()
            token = str(uuid.uuid4())
            expires = datetime.utcnow() + timedelta(hours=24)
            new_invitation = HouseholdInvitation(household_id=current_user.household_id, token=token, expires_at=expires)
            db.session.add(new_invitation)
            db.session.commit()
            invite_link = url_for('join_household', token=token, _external=True)
            return jsonify({'invite_link': invite_link})

        elif action == 'update_household_name':
            new_name = request.form.get('household_name')
            if new_name:
                current_user.household.name = new_name
                db.session.commit()
                flash('Household name updated successfully.', 'success')
            return redirect(url_for('profile'))

        elif action == 'add_store':
            store_name = request.form.get('name')
            search_url = request.form.get('search_url')
            if store_name and search_url and '{query}' in search_url:
                new_store = GroceryStore(household_id=current_user.household_id, name=store_name, search_url=search_url)
                db.session.add(new_store)
                db.session.commit()
                flash(f'Grocery store "{store_name}" added.', 'success')
            else:
                flash('Invalid store name or URL. Make sure the URL contains "{query}".', 'danger')
            return redirect(url_for('profile'))
        
        elif action == 'delete_store':
            store_id = request.form.get('store_id')
            store_to_delete = GroceryStore.query.filter_by(id=store_id, household_id=current_user.household_id).first()
            if store_to_delete:
                flash(f'Grocery store "{store_to_delete.name}" removed.', 'success')
                db.session.delete(store_to_delete)
                db.session.commit()
            return redirect(url_for('profile'))
        
        elif action == 'remove_member':
            member_id = int(request.form.get('member_id'))
            if member_id == current_user.id:
                flash("You cannot remove yourself from the household.", "warning")
                return redirect(url_for('profile'))
            
            member_to_remove = User.query.get(member_id)
            if member_to_remove and member_to_remove.household_id == current_user.household_id:
                new_household = Household(name=f"{member_to_remove.email.split('@')[0]}'s Household")
                db.session.add(new_household)
                db.session.flush()
                member_to_remove.household_id = new_household.id
                db.session.commit()
                flash(f"Removed {member_to_remove.email} from the household.", "success")
            else:
                flash("Member not found in your household.", "danger")
            return redirect(url_for('profile'))

    stores = GroceryStore.query.filter_by(household_id=current_user.household_id).order_by(GroceryStore.name).all()
    return render_template('profile.html', stores=stores, member_limit=member_limit, is_full=is_full)

@app.route('/join-household/<token>')
@login_required
def join_household(token):
    invitation = HouseholdInvitation.query.filter_by(token=token).first()

    if not invitation or invitation.expires_at < datetime.utcnow():
        flash('This invitation link is invalid or has expired.', 'danger')
        return redirect(url_for('profile'))

    if current_user.household_id == invitation.household_id:
        flash('You are already a member of this household.', 'info')
        return redirect(url_for('profile'))

    target_household = invitation.household
    if not target_household.members:
        flash("Cannot join an empty household through an invite.", "danger")
        return redirect(url_for('profile'))

    household_owner = target_household.members[0]
    member_limit = app.config['HOUSEHOLD_LIMITS'].get(household_owner.subscription_plan, 2)
    
    if len(target_household.members) >= member_limit:
        flash(f'The "{target_household.name}" household is full. The owner needs to upgrade their plan to add more members.', 'warning')
        return redirect(url_for('profile'))

    old_household = current_user.household
    if len(old_household.members) == 1:
        db.session.delete(old_household)

    current_user.household_id = invitation.household_id
    db.session.delete(invitation)
    db.session.commit()

    flash(f'You have successfully joined the "{invitation.household.name}" household!', 'success')
    return redirect(url_for('profile'))

@app.route('/api/import-and-create-recipe', methods=['POST'])
@login_required
@require_ai_credits
def import_and_create_recipe():
    data = request.get_json()
    url = data.get('url')
    if not url:
        return jsonify({'error': 'URL is required.'}), 400

    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, 'html.parser')
        
        content_selectors = ['article', 'main', '.recipe', '#recipe', '[class*="recipe-"]', '[id*="recipe-"]']
        main_content = None
        for selector in content_selectors:
            main_content = soup.select_one(selector)
            if main_content:
                break
        
        page_text = ' '.join((main_content or soup.body).get_text(separator=' ', strip=True).split())

        if len(page_text) < 150:
             return jsonify({'error': 'Could not extract enough readable content. The website may be blocking scrapers or has an unusual format.'}), 400
        
        model = genai.GenerativeModel('gemini-2.5-pro')
        
        recipe_prompt = (f"""
            Analyze the following text from a recipe webpage and extract the recipe details.
            Ignore comments, ads, and other non-recipe content. Focus only on the core recipe.
            Your output must be a single, valid JSON object with the following keys:
            - "name": The title of the recipe.
            - "servings": The number of servings as an integer. If not found, use null.
            - "instructions": A single string with steps separated by '\\n'.
            - "meal_type": Must be one of 'Main Course', 'Side Dish', 'Dessert', 'Snack', or 'Meal Prep'. If unsure, default to 'Main Course'.
            - "ingredients": An array of objects, where each object has "name", "quantity", and "unit".
        """)
        
        recipe_response = model.generate_content(
            [recipe_prompt, page_text[:30000]],
            generation_config=genai.types.GenerationConfig(response_mime_type="application/json")
        )
        
        try:
            recipe_data = json.loads(recipe_response.text)
        except json.JSONDecodeError:
            return jsonify({'error': 'The AI returned an invalid format. Please try another URL.'}), 500

        if not all(k in recipe_data for k in ['name', 'instructions', 'ingredients']):
            return jsonify({'error': 'The AI could not understand the recipe from that URL. It might work better with a different recipe site.'}), 400
        
        ingredient_list_for_nutrition = ", ".join([f"{ing.get('quantity', '')} {ing.get('unit', '')} {ing.get('name', '')}" for ing in recipe_data['ingredients']])
        nutrition_prompt = f"""
            Analyze the following ingredient list and estimate the nutritional information PER SERVING.
            Ingredient List: {ingredient_list_for_nutrition}
            Total Servings: {recipe_data.get('servings', 1) or 1}
            Your output must be a single, valid JSON object with only these keys, using only numbers for values: "calories", "protein", "fat", "carbs".
        """
        nutrition_response = model.generate_content(
            nutrition_prompt,
            generation_config=genai.types.GenerationConfig(response_mime_type="application/json")
        )
        nutrition_data = json.loads(nutrition_response.text)

        new_recipe = Recipe(
            name=recipe_data['name'],
            instructions=recipe_data['instructions'],
            servings=recipe_data.get('servings'),
            meal_type=recipe_data.get('meal_type', 'Main Course'),
            author=current_user,
            household_id=current_user.household_id,
            calories=nutrition_data.get('calories'),
            protein=nutrition_data.get('protein'),
            fat=nutrition_data.get('fat'),
            carbs=nutrition_data.get('carbs')
        )
        db.session.add(new_recipe)
        db.session.flush()

        for ing_data in recipe_data['ingredients']:
            ingredient_name = ing_data.get('name', '').strip()
            if not ingredient_name: continue

            ingredient_obj = Ingredient.query.filter(db.func.lower(Ingredient.name) == db.func.lower(ingredient_name)).first()
            if not ingredient_obj:
                ingredient_obj = Ingredient(name=ingredient_name.title())
                db.session.add(ingredient_obj)
                db.session.flush()
            
            quantity_val = convert_quantity_to_float(ing_data.get('quantity', '0'))
            
            recipe_ingredient = RecipeIngredient(
                recipe_id=new_recipe.id,
                ingredient_id=ingredient_obj.id,
                quantity=quantity_val,
                unit=ing_data.get('unit', '')
            )
            db.session.add(recipe_ingredient)
        
        deduct_ai_credit(current_user)
        db.session.commit()
        
        flash(f'Successfully imported "{new_recipe.name}"! Please review the details and AI-generated nutritional info.', 'success')
        return jsonify({'success': True, 'recipe_id': new_recipe.id})

    except requests.exceptions.RequestException:
        return jsonify({'error': f'Failed to fetch the URL. Please check the address and try again.'}), 500
    except Exception as e:
        db.session.rollback()
        print(f"Error during import: {e}")
        return jsonify({'error': 'An unexpected error occurred. The recipe format on the page may be too complex.'}), 500

@app.route('/api/build-plan', methods=['POST'])
@login_required
@require_ai_credits
def build_plan_api():
    data = request.get_json()
    duration = data.get('duration', 'week')
    theme = data.get('theme')
    use_pantry = data.get('use_pantry', False)
    focus_favorites = data.get('focus_favorites', False)
    takeout_days = int(data.get('takeout_days', 0))
    meal_slots_to_plan = data.get('meal_slots', ['Breakfast', 'Lunch', 'Dinner'])
    if not meal_slots_to_plan:
        meal_slots_to_plan = ['Breakfast', 'Lunch', 'Dinner']

    all_recipes = Recipe.query.filter_by(household_id=current_user.household_id).all()
    
    prompt_sections = []
    if 'Breakfast' in meal_slots_to_plan:
        breakfast_recipes = [r for r in all_recipes if r.meal_type in ('Breakfast', 'Snack')]
        breakfast_list = "\n".join([f"id: {r.id}, name: \"{r.name}\"" for r in breakfast_recipes]) if breakfast_recipes else "No breakfast recipes available."
        prompt_sections.append(f"Available Breakfasts:\n{breakfast_list}")

    if 'Lunch' in meal_slots_to_plan:
        lunch_recipes = [r for r in all_recipes if r.meal_type in ('Lunch', 'Side Dish', 'Main Course', 'Meal Prep')]
        lunch_list = "\n".join([f"id: {r.id}, name: \"{r.name}\"" for r in lunch_recipes]) if lunch_recipes else "No lunch recipes available."
        prompt_sections.append(f"Available Lunches:\n{lunch_list}")

    if 'Dinner' in meal_slots_to_plan:
        dinner_recipes = [r for r in all_recipes if r.meal_type in ('Main Course', 'Meal Prep')]
        dinner_list = "\n".join([f"id: {r.id}, name: \"{r.name}\"" for r in dinner_recipes]) if dinner_recipes else "No dinner recipes available."
        prompt_sections.append(f"Available Dinners:\n{dinner_list}")
    
    prompt_sections_str = "\n\n".join(prompt_sections)

    prompt_context = ""
    if use_pantry:
        pantry_items = PantryItem.query.filter(PantryItem.household_id == current_user.household_id, PantryItem.quantity > 0).all()
        if pantry_items:
            pantry_list = ", ".join([p.ingredient.name for p in pantry_items])
            prompt_context += f"\nCONTEXT: Prioritize recipes using: {pantry_list}."
    if focus_favorites:
        favorite_recipes = Recipe.query.filter(Recipe.household_id == current_user.household_id, Recipe.rating >= 4).all()
        if favorite_recipes:
            fav_list = ", ".join([f'"{r.name}"' for r in favorite_recipes])
            prompt_context += f"\nCONTEXT: The user enjoys these recipes: {fav_list}."

    meal_slots_str = ", ".join(meal_slots_to_plan)
    
    if duration == 'month':
        year = int(data.get('year'))
        month = int(data.get('month'))
        _, num_days = calendar.monthrange(year, month)
        
        instruction = (
            f"You must select recipes for the following meals: {meal_slots_str} for each of the {num_days} days of the month. Follow these rules:\n"
            "1. You MUST fill every requested meal slot for every day. Do not leave any requested slots empty.\n"
        )
        if 'Dinner' in meal_slots_to_plan:
            instruction += f"2. Create exactly {takeout_days} 'Takeout Night' dinners (id: null). Space them out logically, preferably on Fridays or Saturdays.\n"
        if 'Lunch' in meal_slots_to_plan and 'Dinner' in meal_slots_to_plan:
            instruction += "3. For economy and to reduce waste, plan for 'Leftovers' for lunch (id: null). A dinner should typically be followed by a 'Leftovers' lunch the next day. If the previous day was a 'Takeout Night' or you need variety, select a different recipe from the Available Lunches list.\n"
        instruction += (
            "4. Ensure high variety. Do not repeat the same dinner recipe within a 10-day period. Breakfasts can be repeated more often.\n"
            "5. The overall plan must feel logical and not random."
        )

        json_structure = (f"Your response MUST be ONLY a valid JSON object. The top-level keys are the days of the month as strings ('1', '2', ..., '{num_days}'). "
                          f"Each day's value must be another dictionary with ONLY these keys: {json.dumps(meal_slots_to_plan)}. The value for each meal slot is an object with 'id' and 'name'.")
    else: # week
        instruction = (
            f"You must select recipes for the following meals: {meal_slots_str} for each of the 7 days (Monday to Sunday). Follow these rules:\n"
            "1. You MUST fill every requested meal slot for every day.\n"
        )
        if 'Dinner' in meal_slots_to_plan:
             instruction += f"2. Create exactly {takeout_days} 'Takeout Night' dinners (id: null).\n"
        if 'Lunch' in meal_slots_to_plan and 'Dinner' in meal_slots_to_plan:
            instruction += "3. Plan for 'Leftovers' for lunch on at least 2-3 days, following a dinner from the previous night. If leftovers are not appropriate, choose from the Available Lunches list.\n"
        instruction += "4. Do not repeat recipes within the week."

        json_structure = ("Your response MUST be ONLY a valid JSON object. The top-level keys are the days of the week ('Monday',..., 'Sunday'). "
                          f"Each day's value must be another dictionary with ONLY these keys: {json.dumps(meal_slots_to_plan)}. The value for each meal slot is an object with 'id' and 'name'.")

    final_prompt = (
        f"You are a Meal Plan Architect. Create a diverse and logical meal plan based on the theme: '{theme}'.\n"
        f"{instruction}{prompt_context}\n\n"
        f"{prompt_sections_str}\n\n"
        f"{json_structure}"
    )
    
    try:
        model = genai.GenerativeModel('gemini-2.5-pro')
        response = model.generate_content(final_prompt, generation_config=genai.types.GenerationConfig(response_mime_type="application/json"))
        plan_data = json.loads(response.text.strip())

        response_payload = {'duration': duration, 'plan': plan_data}
        if duration == 'month':
            response_payload['year'] = year
            response_payload['month'] = month
        
        deduct_ai_credit(current_user)
        db.session.commit()
        
        return jsonify(response_payload)
    except Exception as e:
        db.session.rollback()
        print(f"Build Plan Error: {e}\nResponse Text: {response.text if 'response' in locals() else 'No response'}")
        return jsonify({'error': f'The AI failed to generate a valid plan. Details: {str(e)}'}), 500

@app.route('/api/save-ai-plan', methods=['POST'])
@login_required
def save_ai_plan():
    data = request.get_json()
    plan_data = data.get('plan')
    duration = data.get('duration')

    try:
        if duration == 'month':
            year = int(data.get('year'))
            month = int(data.get('month'))
            
            start_date = date(year, month, 1)
            end_date = date(year, month, calendar.monthrange(year, month)[1])

            MealPlan.query.filter(
                and_(
                    MealPlan.household_id == current_user.household_id,
                    MealPlan.meal_date.between(start_date, end_date)
                )
            ).delete(synchronize_session=False)

            for day_str, meals in plan_data.items():
                day_num = int(day_str)
                current_date = date(year, month, day_num)
                for slot, meal in meals.items():
                    if meal and meal.get('name') and meal['name'] != 'Unplanned':
                        new_entry = MealPlan(
                            household_id=current_user.household_id,
                            meal_date=current_date,
                            meal_slot=slot,
                            recipe_id=meal.get('id'),
                            custom_item_name=None if meal.get('id') else meal.get('name')
                        )
                        db.session.add(new_entry)
            
            flash(f'Your AI-generated plan for {start_date.strftime("%B %Y")} has been saved!', 'success')
            redirect_url = url_for('monthly_plan', year=year, month=month)

        else: # duration == 'week'
            today = date.today()
            start_of_week = today - timedelta(days=today.weekday())
            end_of_week = start_of_week + timedelta(days=6)

            MealPlan.query.filter(
                MealPlan.household_id == current_user.household_id,
                MealPlan.meal_date.between(start_of_week, end_of_week)
            ).delete(synchronize_session=False)

            days_of_week = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
            for i, day_name in enumerate(days_of_week):
                current_date = start_of_week + timedelta(days=i)
                meals = plan_data.get(day_name, {})
                for slot, meal in meals.items():
                    if meal and meal.get('name') and meal['name'] != 'Unplanned':
                        new_entry = MealPlan(
                            household_id=current_user.household_id,
                            meal_date=current_date,
                            meal_slot=slot,
                            recipe_id=meal.get('id'),
                            custom_item_name=None if meal.get('id') else meal.get('name')
                        )
                        db.session.add(new_entry)
            
            flash('Your AI-generated weekly plan has been saved!', 'success')
            redirect_url = url_for('meal_plan', start_date=start_of_week.strftime('%Y-%m-%d'))
        
        db.session.commit()
        return jsonify({'success': True, 'redirect_url': redirect_url})
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/set-rating/<int:recipe_id>', methods=['POST'])
@login_required
def set_rating(recipe_id):
    recipe = Recipe.query.filter_by(id=recipe_id, household_id=current_user.household_id).first_or_404()
    data = request.get_json()
    rating = data.get('rating')
    if rating is not None and 0 <= int(rating) <= 5:
        recipe.rating = int(rating)
        db.session.commit()
        return jsonify({'success': True, 'rating': recipe.rating})
    return jsonify({'success': False, 'message': 'Invalid rating.'}), 400

@app.route('/api/toggle-favorite/<int:recipe_id>', methods=['POST'])
@login_required
def toggle_favorite(recipe_id):
    recipe = Recipe.query.filter_by(id=recipe_id, household_id=current_user.household_id).first_or_404()
    recipe.is_favorite = not recipe.is_favorite
    db.session.commit()
    return jsonify({'is_favorite': recipe.is_favorite})

@app.route('/api/generate-from-ingredients', methods=['POST'])
@login_required
@require_ai_credits
def generate_from_ingredients_api():
    data = request.get_json()
    ingredients_text = data.get('ingredients', '')
    if not ingredients_text.strip(): return jsonify({'error': 'Please enter some ingredients.'}), 400
    prompt = (f"You are a creative chef with these ingredients: {ingredients_text}. Invent a practical recipe using them. Assume basic staples. Provide a complete recipe: name, ingredient list, and instructions.")
    try:
        model = genai.GenerativeModel('gemini-2.5-pro')
        response = model.generate_content(prompt)
        ai_response = response.text
        
        deduct_ai_credit(current_user)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        ai_response = "Sorry, the AI assistant is unavailable."
    return jsonify({'generated_recipe': ai_response})

@app.route('/api/remix-recipe', methods=['POST'])
@login_required
@require_ai_credits
def remix_recipe_api():
    data = request.get_json()
    recipe_id = data.get('recipe_id')
    remix_type = data.get('remix_type')
    recipe = Recipe.query.filter_by(id=recipe_id, household_id=current_user.household_id).first()
    if not recipe: return jsonify({'error': 'Recipe not found'}), 404
    
    prompt = (f"Please rewrite the recipe '{recipe.name}' to be '{remix_type}'. "
              "Your output must be a single, valid JSON object with the following keys: "
              "\"name\" (a creative new name for the remixed recipe), "
              "\"instructions\" (a single string with steps separated by '\\n'), "
              "\"ingredients\" (an array of objects, where each object has \"name\", \"quantity\", and \"unit\").")

    try:
        model = genai.GenerativeModel('gemini-2.5-pro')
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                response_mime_type="application/json"
            )
        )
        remixed_data = json.loads(response.text)
        
        if not all(k in remixed_data for k in ['name', 'instructions', 'ingredients']):
             raise ValueError("AI response was missing required keys.")

        deduct_ai_credit(current_user)
        db.session.commit()
        return jsonify({'remixed_recipe': remixed_data})
    except Exception as e:
        db.session.rollback()
        print(f"Remix recipe error: {e}")
        return jsonify({'error': 'Sorry, the AI assistant could not generate a valid recipe remix.'})


@app.route('/api/save-new-recipe', methods=['POST'])
@login_required
def save_new_recipe():
    data = request.get_json()
    if not data or not data.get('name') or not data.get('instructions'):
        return jsonify({'success': False, 'message': 'Invalid recipe data.'}), 400
        
    try:
        new_recipe = Recipe(
            name=data['name'],
            instructions=data['instructions'],
            meal_type=data.get('meal_type', 'Main Course'),
            author=current_user,
            household_id=current_user.household_id
        )
        db.session.add(new_recipe)
        db.session.flush()

        if 'ingredients' in data and isinstance(data['ingredients'], list):
            for ing_data in data['ingredients']:
                ingredient_name = ing_data.get('name', '').strip()
                if not ingredient_name: continue

                ingredient_obj = Ingredient.query.filter(db.func.lower(Ingredient.name) == db.func.lower(ingredient_name)).first()
                if not ingredient_obj:
                    ingredient_obj = Ingredient(name=ingredient_name)
                    db.session.add(ingredient_obj)
                    db.session.flush()
                
                quantity_val = convert_quantity_to_float(ing_data.get('quantity', '0'))
                
                recipe_ingredient = RecipeIngredient(
                    recipe_id=new_recipe.id,
                    ingredient_id=ingredient_obj.id,
                    quantity=quantity_val,
                    unit=ing_data.get('unit', '')
                )
                db.session.add(recipe_ingredient)
        
        db.session.commit()
        flash(f'New recipe "{new_recipe.name}" saved successfully!', 'success')
        return jsonify({'success': True, 'recipe_id': new_recipe.id})

    except Exception as e:
        db.session.rollback()
        print(f"Save new recipe error: {e}")
        return jsonify({'success': False, 'message': 'An error occurred while saving.'}), 500


@app.route('/api/suggest-recipes')
@login_required
def suggest_recipes():
    all_recipes = Recipe.query.filter_by(household_id=current_user.household_id).all()
    sample_size = min(len(all_recipes), 7)
    suggested = random.sample(all_recipes, sample_size) if sample_size > 0 else []
    return jsonify([{'id': r.id, 'name': r.name, 'meal_type': r.meal_type} for r in suggested])

@app.route('/api/search-recipes')
@login_required
def search_recipes_api():
    query = request.args.get('query', '')
    if query:
        search_term = f"%{query}%"
        results = Recipe.query.filter_by(household_id=current_user.household_id).filter(Recipe.name.ilike(search_term)).limit(10).all()
        return jsonify([{'id': r.id, 'name': r.name, 'meal_type': r.meal_type} for r in results])
    return jsonify([])

@app.route('/api/get-saved-meals')
@login_required
def get_saved_meals():
    saved_meals = SavedMeal.query.filter_by(household_id=current_user.household_id).all()
    output = []
    for meal in saved_meals:
        output.append({
            'id': meal.id,
            'name': meal.name,
            'recipes': [{'id': r.id, 'name': r.name, 'meal_type': r.meal_type} for r in meal.recipes]
        })
    return jsonify(output)


@app.route('/api/consume-recipe/<int:recipe_id>', methods=['POST'])
@login_required
def consume_recipe(recipe_id):
    recipe = Recipe.query.filter_by(id=recipe_id, household_id=current_user.household_id).first_or_404()
    pantry_items = {item.ingredient_id: item for item in current_user.household.pantry_items}
    
    updated_items = []
    skipped_items = []

    for req_ing in recipe.ingredients:
        if not req_ing.quantity or req_ing.ingredient_id not in pantry_items:
            continue

        pantry_item = pantry_items[req_ing.ingredient_id]
        ing_substance = req_ing.ingredient.name.lower().replace(" ", "_")
        
        with ureg.context('cooking', substance=ing_substance):
            try:
                recipe_unit = sanitize_unit(req_ing.unit)
                pantry_unit = sanitize_unit(pantry_item.unit)
                
                recipe_qty = req_ing.quantity * ureg(recipe_unit)
                pantry_qty = pantry_item.quantity * ureg(pantry_unit)
                
                if not recipe_qty.is_compatible_with(pantry_qty):
                    raise pint.errors.DimensionalityError(recipe_qty.units, pantry_qty.units)

                pantry_qty_in_recipe_units = pantry_qty.to(recipe_qty.units)
                
                new_pantry_qty_in_recipe_units = pantry_qty_in_recipe_units - recipe_qty
                new_pantry_qty = new_pantry_qty_in_recipe_units.to(pantry_qty.units)
                
                pantry_item.quantity = max(0, new_pantry_qty.magnitude)
                updated_items.append(req_ing.ingredient.name)

            except Exception:
                skipped_items.append(f"{req_ing.ingredient.name} (Unit conversion failed)")
    
    db.session.commit()
    
    return jsonify({
        'status': 'success',
        'message': 'Pantry updated.',
        'updated': updated_items,
        'skipped': skipped_items
    })

@app.route('/api/mark-meal-eaten', methods=['POST'])
@login_required
def mark_meal_eaten():
    data = request.get_json()
    meal_date_str = data.get('date')
    meal_slot = data.get('slot')

    if not meal_date_str or not meal_slot:
        return jsonify({'success': False, 'error': 'Missing date or slot information.'}), 400

    try:
        meal_date_obj = datetime.strptime(meal_date_str, '%Y-%m-%d').date()
        
        meals_to_update = MealPlan.query.filter_by(
            household_id=current_user.household_id,
            meal_date=meal_date_obj,
            meal_slot=meal_slot
        ).all()

        if not meals_to_update:
            return jsonify({'success': True, 'message': 'No meals to mark.'})

        new_status = not meals_to_update[0].is_eaten
        for meal in meals_to_update:
            meal.is_eaten = new_status
        
        db.session.commit()
        return jsonify({'success': True, 'is_eaten': new_status})

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/consume-meal', methods=['POST'])
@login_required
def consume_meal():
    data = request.get_json()
    meal_date_str = data.get('meal_date')
    meal_slot = data.get('meal_slot')

    if not meal_date_str or not meal_slot:
        return jsonify({'status': 'error', 'message': 'Missing meal_date or meal_slot.'}), 400

    meal_date_obj = datetime.strptime(meal_date_str, '%Y-%m-%d').date()
    
    planned_meal_entries = MealPlan.query.filter_by(
        household_id=current_user.household_id,
        meal_date=meal_date_obj,
        meal_slot=meal_slot,
        recipe_id=db.not_(None)
    ).all()

    if not planned_meal_entries:
        return jsonify({'status': 'info', 'message': 'No recipes to consume for this meal.'})

    pantry_items = {item.ingredient_id: item for item in current_user.household.pantry_items}
    updated_items = []
    skipped_items = []

    required_ingredients = {}
    for entry in planned_meal_entries:
        for req_ing in entry.recipe.ingredients:
            if not req_ing.quantity: continue
            key = (req_ing.ingredient_id, req_ing.unit)
            if key not in required_ingredients:
                required_ingredients[key] = {'quantity': 0, 'ingredient': req_ing.ingredient}
            required_ingredients[key]['quantity'] += req_ing.quantity

    for (ing_id, unit), details in required_ingredients.items():
        if ing_id not in pantry_items: continue
        
        pantry_item = pantry_items[ing_id]
        ing_substance = details['ingredient'].name.lower().replace(" ", "_")
        
        with ureg.context('cooking', substance=ing_substance):
            try:
                recipe_unit = sanitize_unit(unit)
                pantry_unit = sanitize_unit(pantry_item.unit)
                
                required_total_qty = details['quantity'] * ureg(recipe_unit)
                pantry_qty = pantry_item.quantity * ureg(pantry_unit)
                
                if not required_total_qty.is_compatible_with(pantry_qty):
                    raise pint.errors.DimensionalityError(required_total_qty.units, pantry_qty.units)

                pantry_qty_converted = pantry_qty.to(required_total_qty.units)
                new_pantry_qty_converted = pantry_qty_converted - required_total_qty
                new_pantry_qty_native = new_pantry_qty_converted.to(pantry_qty.units)
                
                pantry_item.quantity = max(0, new_pantry_qty_native.magnitude)
                updated_items.append(details['ingredient'].name)

            except Exception:
                skipped_items.append(f"{details['ingredient'].name} (Unit conversion failed)")
    
    db.session.commit()
    
    return jsonify({
        'status': 'success',
        'message': 'Pantry updated based on your meal.',
        'updated': updated_items,
        'skipped': skipped_items
    })


@app.route('/add-to-plan/<int:recipe_id>')
@login_required
def add_recipe_to_plan(recipe_id):
    recipe = Recipe.query.filter_by(id=recipe_id, household_id=current_user.household_id).first_or_404()
    today = date.today()
    
    new_plan_entry = MealPlan(meal_date=today, recipe_id=recipe.id, household_id=current_user.household_id, meal_slot='Dinner')
    db.session.add(new_plan_entry)
    db.session.commit()
    flash(f'"{recipe.name}" was added to your plan for dinner today!', 'success')
    
    return redirect(url_for('meal_plan'))


@app.route('/meal-plan', methods=['GET', 'POST'])
@login_required
def meal_plan():
    start_date_str = request.args.get('start_date')
    today = date.today()
    
    if start_date_str:
        try:
            start_of_week = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        except ValueError:
            start_of_week = today - timedelta(days=today.weekday())
    else:
        start_of_week = today - timedelta(days=today.weekday())

    if request.method == 'POST':
        week_start_str = request.form.get('week_start_date')
        week_start_date = datetime.strptime(week_start_str, '%Y-%m-%d').date()
        end_of_week = week_start_date + timedelta(days=6)

        MealPlan.query.filter(
            MealPlan.household_id == current_user.household_id,
            MealPlan.meal_date.between(week_start_date, end_of_week)
        ).delete(synchronize_session=False)
        db.session.commit()

        for i in range(7):
            current_day = week_start_date + timedelta(days=i)
            day_str = current_day.strftime('%Y-%m-%d')
            
            for slot in ['Breakfast', 'Lunch', 'Dinner', 'Snack']:
                recipe_ids = request.form.getlist(f'day-{day_str}-{slot}-recipe[]')
                for recipe_id in recipe_ids:
                    if recipe_id and recipe_id.isdigit():
                        db.session.add(MealPlan(meal_date=current_day, recipe_id=int(recipe_id), household_id=current_user.household_id, meal_slot=slot))
                
                custom_items = request.form.getlist(f'day-{day_str}-{slot}-custom[]')
                for item_name in custom_items:
                    if item_name:
                         db.session.add(MealPlan(meal_date=current_day, custom_item_name=item_name, household_id=current_user.household_id, meal_slot=slot))
        
        db.session.commit()

        historical_plan_name = request.form.get('historical_plan_name')
        if historical_plan_name:
            new_hist_plan = HistoricalPlan(name=historical_plan_name, household_id=current_user.household_id)
            db.session.add(new_hist_plan)
            db.session.flush()

            newly_saved_entries = MealPlan.query.filter(
                MealPlan.household_id == current_user.household_id,
                MealPlan.meal_date.between(week_start_date, end_of_week)
            ).all()

            for entry in newly_saved_entries:
                hist_entry = HistoricalPlanEntry(
                    historical_plan_id=new_hist_plan.id,
                    day_of_week=entry.meal_date.weekday(),
                    meal_slot=entry.meal_slot,
                    recipe_id=entry.recipe_id,
                    custom_item_name=entry.custom_item_name
                )
                db.session.add(hist_entry)

            db.session.commit()
            flash(f'Meal plan saved and also stored as "{historical_plan_name}"!', 'success')
        else:
            flash('Meal plan saved successfully!', 'success')

        return redirect(url_for('meal_plan', start_date=week_start_str))

    end_of_week = start_of_week + timedelta(days=6)
    prev_week_start = start_of_week - timedelta(days=7)
    next_week_start = start_of_week + timedelta(days=7)

    days_of_week = [(start_of_week + timedelta(days=i)) for i in range(7)]
    
    all_meals = MealPlan.query.filter(
        MealPlan.household_id == current_user.household_id,
        MealPlan.meal_date.between(start_of_week, end_of_week)
    ).all()
    
    planned_meals = {day.strftime('%Y-%m-%d'): {'Breakfast': [], 'Lunch': [], 'Dinner': [], 'Snack': []} for day in days_of_week}
    for meal in all_meals:
        day_str = meal.meal_date.strftime('%Y-%m-%d')
        if day_str in planned_meals and meal.meal_slot in planned_meals[day_str]:
            planned_meals[day_str][meal.meal_slot].append(meal)

    TRAY_CATEGORY_MAP = {
        'Main Course': 'Main Course', 'Dinner': 'Main Course',
        'Side Dish': 'Side Dish', 'Dessert': 'Dessert',
        'Snack': 'Snack', 'Breakfast': 'Snack', 'Appetizer': 'Snack',
        'Meal Prep': 'Meal Prep'
    }
    tray_categories = sorted(list(set(TRAY_CATEGORY_MAP.values())))
    recipes_by_type = {category: [] for category in tray_categories}
    recipes_for_js = {category: [] for category in tray_categories}
    initial_tray_recipes_js = {category: [] for category in tray_categories}

    all_recipes = Recipe.query.filter_by(household_id=current_user.household_id).order_by(Recipe.name).all()
    
    for r in all_recipes:
        normalized_meal_type = r.meal_type.strip().title() if r.meal_type else 'Main Course'
        tray_category = TRAY_CATEGORY_MAP.get(normalized_meal_type, 'Main Course')
        if tray_category in recipes_by_type:
            recipes_by_type[tray_category].append(r)
            
    for category, recipe_list in recipes_by_type.items():
        recipes_for_js[category] = [{'id': r.id, 'name': r.name, 'meal_type': r.meal_type} for r in recipe_list]

    initial_tray_recipes = Recipe.query.filter_by(household_id=current_user.household_id).order_by(desc(Recipe.id)).limit(5).all()
    for r in initial_tray_recipes:
        normalized_meal_type = r.meal_type.strip().title() if r.meal_type else 'Main Course'
        tray_category = TRAY_CATEGORY_MAP.get(normalized_meal_type, 'Main Course')
        if tray_category in initial_tray_recipes_js:
            initial_tray_recipes_js[tray_category].append({'id': r.id, 'name': r.name, 'meal_type': r.meal_type})

    historical_plans = HistoricalPlan.query.filter_by(household_id=current_user.household_id).order_by(HistoricalPlan.name).all()
    
    daily_stats = {day.strftime('%Y-%m-%d'): {'scheduled': {'calories': 0}, 'consumed': {'calories': 0}} for day in days_of_week}
    weekly_stats = {'scheduled': {'calories': 0}, 'consumed': {'calories': 0}}

    for meal in all_meals:
        if meal.recipe:
            day_str = meal.meal_date.strftime('%Y-%m-%d')
            calories = meal.recipe.calories or 0
            
            daily_stats[day_str]['scheduled']['calories'] += calories
            weekly_stats['scheduled']['calories'] += calories

            if meal.is_eaten:
                daily_stats[day_str]['consumed']['calories'] += calories
                weekly_stats['consumed']['calories'] += calories


    return render_template('meal_plan.html',
                           page_class='page-meal-plan',
                           days=days_of_week,
                           planned_meals=planned_meals,
                           recipes_by_type=recipes_by_type,
                           recipes_for_js=recipes_for_js,
                           initial_tray_recipes_js=initial_tray_recipes_js,
                           historical_plans=historical_plans,
                           start_of_week=start_of_week,
                           prev_week_start=prev_week_start,
                           next_week_start=next_week_start,
                           daily_stats=daily_stats,
                           weekly_stats=weekly_stats,
                           today=today)


@app.route('/api/load-historical-plan/<int:plan_id>', methods=['GET'])
@login_required
def load_historical_plan(plan_id):
    plan = HistoricalPlan.query.filter_by(id=plan_id, household_id=current_user.household_id).first_or_404()
    
    plan_data = {}
    for entry in plan.entries:
        day_key = str(entry.day_of_week)
        if day_key not in plan_data:
            plan_data[day_key] = {}
        if entry.meal_slot not in plan_data[day_key]:
            plan_data[day_key][entry.meal_slot] = []

        item_data = {'type': 'custom', 'name': entry.custom_item_name}
        if entry.recipe_id:
            item_data = {'type': 'recipe', 'id': entry.recipe.id, 'name': entry.recipe.name, 'meal_type': entry.recipe.meal_type}
        
        plan_data[day_key][entry.meal_slot].append(item_data)

    return jsonify(plan_data)


@app.route('/shopping-list', methods=['GET', 'POST'])
@login_required
def shopping_list():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add_manual_item':
            name = request.form.get('name')
            category = request.form.get('category')
            if name and category:
                new_item = ShoppingListItem(
                    household_id=current_user.household_id,
                    name=name,
                    category=category
                )
                db.session.add(new_item)
                db.session.commit()
                flash(f'"{name}" added to your shopping list.', 'success')
        elif action == 'delete_manual_item':
            item_id = request.form.get('item_id')
            item_to_delete = ShoppingListItem.query.filter_by(id=item_id, household_id=current_user.household_id).first()
            if item_to_delete:
                flash(f'"{item_to_delete.name}" was removed from your list.', 'info')
                db.session.delete(item_to_delete)
                db.session.commit()
        return redirect(url_for('shopping_list'))

    today = date.today()
    end_date_for_shopping = today + timedelta(days=6)

    all_planned_meals = MealPlan.query.filter(
        MealPlan.household_id == current_user.household_id,
        MealPlan.recipe_id.isnot(None),
        MealPlan.meal_date.between(today, end_date_for_shopping)
    ).all()

    manual_items = ShoppingListItem.query.filter_by(household_id=current_user.household_id).all()
    
    required = {}
    for meal in all_planned_meals:
        if not meal.recipe: continue
        for item in meal.recipe.ingredients:
            if not item.quantity or item.quantity == 0: continue
            
            ing_key = (item.ingredient.id, item.ingredient.name, item.ingredient.category)
            if ing_key not in required:
                required[ing_key] = {'quantity': 0, 'units': set()}
            required[ing_key]['quantity'] += item.quantity
            required[ing_key]['units'].add(item.unit)

    pantry_stock = {item.ingredient_id: item for item in PantryItem.query.filter_by(household_id=current_user.household_id).all()}
    
    to_buy = {}
    in_pantry = {}

    for (ing_id, ing_name, ing_category), details in required.items():
        needed_qty_val = details['quantity']
        recipe_unit_str = sanitize_unit(next(iter(details['units'])) if details['units'] else '')
        ing_substance = ing_name.lower().replace(" ", "_")

        should_buy = True
        buy_details = {'quantity': needed_qty_val, 'units': details['units'], 'note': "Not in pantry", 'category': ing_category or 'Other'}

        if ing_id in pantry_stock:
            pantry_item = pantry_stock[ing_id]
            in_pantry[ing_name] = pantry_item
            pantry_unit_str = sanitize_unit(pantry_item.unit)
            
            with ureg.context('cooking', substance=ing_substance):
                try:
                    required_qty = needed_qty_val * ureg(recipe_unit_str)
                    pantry_qty = pantry_item.quantity * ureg(pantry_unit_str)
                    
                    if not required_qty.is_compatible_with(pantry_qty): raise pint.errors.DimensionalityError(required_qty.units, pantry_qty.units)

                    pantry_qty_converted = pantry_qty.to(required_qty.units)
                    
                    if pantry_qty_converted >= required_qty:
                        should_buy = False
                    else:
                        amount_to_buy = required_qty - pantry_qty_converted
                        buy_details = {'quantity': amount_to_buy.magnitude, 'units': {str(amount_to_buy.units)}, 'note': None, 'category': ing_category or 'Other'}

                except Exception:
                    buy_details['note'] = f"Unit Mismatch! Check pantry: you have {pantry_item.quantity} {pantry_item.unit or ''}"

        if should_buy:
            to_buy[ing_name] = buy_details
            
    grouped_list = {}
    for name, details in to_buy.items():
        category = details.get('category', 'Other')
        if category not in grouped_list:
            grouped_list[category] = {}
        grouped_list[category][name] = details
    
    for item in manual_items:
        category = item.category
        if category not in grouped_list:
            grouped_list[category] = {}
        grouped_list[category][item.name] = {'quantity': None, 'units': [], 'note': 'Manually added', 'manual_id': item.id}


    stores = GroceryStore.query.filter_by(household_id=current_user.household_id).order_by(GroceryStore.name).all()
    
    return render_template('shopping_list.html', page_class='page-shopping-list',
                           grouped_list=grouped_list,
                           ingredients_in_pantry=in_pantry,
                           stores=stores)

@app.route('/export/recipes')
@login_required
def export_recipes():
    recipes = Recipe.query.filter_by(household_id=current_user.household_id).all()
    output = io.StringIO()
    writer = csv.writer(output)
    
    writer.writerow(['id', 'name', 'instructions', 'servings', 'prep_time', 'cook_time', 'meal_type', 'is_favorite', 'rating', 'author_email'])
    for recipe in recipes:
        writer.writerow([recipe.id, recipe.name, recipe.instructions, recipe.servings, recipe.prep_time, recipe.cook_time, recipe.meal_type, recipe.is_favorite, recipe.rating, recipe.author.email])
    
    output.seek(0)
    return Response(output, mimetype="text/csv", headers={"Content-Disposition":"attachment;filename=recipes.csv"})

@app.route('/export/recipe_ingredients')
@login_required
def export_recipe_ingredients():
    user_recipe_ids = [r.id for r in current_user.household.recipes]
    recipe_ingredients = RecipeIngredient.query.filter(RecipeIngredient.recipe_id.in_(user_recipe_ids)).all()
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    writer.writerow(['recipe_id', 'ingredient_name', 'quantity', 'unit'])
    for ri in recipe_ingredients:
        writer.writerow([ri.recipe_id, ri.ingredient.name, ri.quantity, ri.unit])
        
    output.seek(0)
    return Response(output, mimetype="text/csv", headers={"Content-Disposition":"attachment;filename=recipe_ingredients.csv"})


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)