from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import desc
from datetime import date, timedelta, datetime
import os, csv, random, json, io, re, uuid
from werkzeug.utils import secure_filename
import google.generativeai as genai
from dotenv import load_dotenv
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
import pint 

import requests
from bs4 import BeautifulSoup

load_dotenv()
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key'
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URL", 'sqlite:///meal_engine.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'uploads'
db = SQLAlchemy(app)
bcrypt = Bcrypt(app)

def convert_quantity_to_float(quantity_str):
    if not isinstance(quantity_str, str):
        return float(quantity_str)
    
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

# --- Pint Unit Registry, Context, and Sanitizer ---
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
    recipe_id = db.Column(db.Integer, db.ForeignKey('recipe.id'), nullable=False)
    recipe = db.relationship('Recipe')
    meal_slot = db.Column(db.String(50), nullable=False, default='Dinner')

class PantryItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    household_id = db.Column(db.Integer, db.ForeignKey('household.id'), nullable=False)
    ingredient_id = db.Column(db.Integer, db.ForeignKey('ingredient.id'), nullable=False)
    quantity = db.Column(db.Float, nullable=False, default=0)
    unit = db.Column(db.String(50), nullable=True)
    date_updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

# --- Routes ---

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
        user = User(email=email, password=hashed_password, household_id=new_household.id)
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

@app.route('/')
def index():
    if not current_user.is_authenticated:
        return render_template('landing_page.html')
    
    with app.app_context():
        db.create_all()
    
    todays_meal_plan = MealPlan.query.filter_by(
        household_id=current_user.household_id, 
        meal_date=date.today(),
        meal_slot='Dinner'
    ).first()

    return render_template('index.html', todays_meal_plan=todays_meal_plan)


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
            if required_ingredient_ids.issubset(pantry_ingredient_ids):
                recipes.append(recipe)
    elif favorites_filter_active:
        recipes = base_query.filter_by(is_favorite=True).all()
    elif query:
        search_term = f"%{query}%"
        recipes = base_query.filter(db.or_(Recipe.name.ilike(search_term), Recipe.instructions.ilike(search_term))).all()
    else:
        recipes = base_query.all()
        
    return render_template('recipes.html', recipes=recipes, query=query, pantry_filter_active=pantry_filter_active, favorites_filter_active=favorites_filter_active, sort_order=sort_order)

@app.route('/ai-quick-add', methods=['POST'])
@login_required
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
        - "meal_type": Must be one of 'Main Course', 'Side Dish', or 'Dessert'.
        - "ingredients": An array of objects, where each object has "name", "quantity", and "unit".
    """
    
    try:
        model = genai.GenerativeModel('gemini-1.5-pro')
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
    
    # This route no longer needs to prefill from GET requests, so we pass an empty dict
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
        recipe.servings = request.form.get('servings')
        recipe.prep_time = request.form.get('prep_time')
        recipe.cook_time = request.form.get('cook_time')
        recipe.meal_type = request.form.get('meal_type')
        
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

@app.route('/ai-architect')
@login_required
def ai_architect():
    today = date.today()
    start_of_week = today - timedelta(days=today.weekday())
    return render_template('ai_architect.html', today=today, start_of_week=start_of_week)

@app.route('/household', methods=['GET', 'POST'])
@login_required
def household_page():
    if request.method == 'POST':
        HouseholdInvitation.query.filter_by(household_id=current_user.household_id).delete()
        
        token = str(uuid.uuid4())
        expires = datetime.utcnow() + timedelta(hours=24)
        new_invitation = HouseholdInvitation(household_id=current_user.household_id, token=token, expires_at=expires)
        db.session.add(new_invitation)
        db.session.commit()
        
        invite_link = url_for('join_household', token=token, _external=True)
        return jsonify({'invite_link': invite_link})

    return render_template('household.html')

@app.route('/join-household/<token>')
@login_required
def join_household(token):
    invitation = HouseholdInvitation.query.filter_by(token=token).first()

    if not invitation or invitation.expires_at < datetime.utcnow():
        flash('This invitation link is invalid or has expired.', 'danger')
        return redirect(url_for('household_page'))

    if current_user.household_id == invitation.household_id:
        flash('You are already a member of this household.', 'info')
        return redirect(url_for('household_page'))

    old_household = current_user.household
    if len(old_household.members) == 1:
        db.session.delete(old_household)

    current_user.household_id = invitation.household_id
    db.session.delete(invitation)
    db.session.commit()

    flash(f'You have successfully joined the "{invitation.household.name}" household!', 'success')
    return redirect(url_for('household_page'))


# --- THIS IS THE NEW WORKFLOW ---
@app.route('/api/import-and-create-recipe', methods=['POST'])
@login_required
def import_and_create_recipe():
    data = request.get_json()
    url = data.get('url')
    if not url:
        return jsonify({'error': 'URL is required.'}), 400

    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36'}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, 'html.parser')
        main_content = soup.find('main') or soup.find('article') or soup.body
        page_text = ' '.join(main_content.get_text().split())

        if len(page_text) < 100:
             return jsonify({'error': 'Could not extract enough readable content from the URL.'}), 400

        prompt = (f"""
            Analyze the following text from a recipe webpage and extract the recipe details.
            Your output must be a single, valid JSON object with the following keys:
            - "name": The title of the recipe.
            - "instructions": A single string with steps separated by '\\n'.
            - "meal_type": Must be one of 'Main Course', 'Side Dish', or 'Dessert'.
            - "ingredients": An array of objects, where each object has "name", "quantity", and "unit".
        """)
        
        model = genai.GenerativeModel('gemini-1.5-pro')
        ai_response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                response_mime_type="application/json"
            )
        )
        recipe_data = json.loads(ai_response.text)

        if not recipe_data.get('name') or not recipe_data.get('instructions') or not recipe_data.get('ingredients'):
            return jsonify({'error': 'The AI could not understand the recipe from that URL.'}), 400
        
        # All validation passed, create the recipe and ingredients
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
        
        db.session.commit()
        
        flash(f'Successfully imported "{new_recipe.name}"! Please review the details.', 'success')
        return jsonify({'success': True, 'recipe_id': new_recipe.id})

    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Failed to fetch the URL.'}), 500
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'An unexpected error occurred during import.'}), 500


@app.route('/api/build-plan', methods=['POST'])
@login_required
def build_plan_api():
    data = request.get_json()
    theme = data.get('theme')
    use_pantry = data.get('use_pantry', False)
    focus_favorites = data.get('focus_favorites', False)

    all_recipes = Recipe.query.filter_by(household_id=current_user.household_id).all()
    if len(all_recipes) < 7:
        return jsonify({'error': 'You need at least 7 recipes in your database to build a full week plan.'}), 400
    
    prompt_context = ""
    if use_pantry:
        pantry_items = PantryItem.query.filter_by(household_id=current_user.household_id).filter(PantryItem.quantity > 0).all()
        if pantry_items:
            pantry_list = ", ".join([p.ingredient.name for p in pantry_items])
            prompt_context += f"\nCONTEXT: Please prioritize recipes that use these ingredients from the household pantry: {pantry_list}."
    if focus_favorites:
        favorite_recipes = Recipe.query.filter_by(household_id=current_user.household_id).filter(Recipe.rating >= 4).all()
        if favorite_recipes:
            fav_list = ", ".join([f'"{r.name}"' for r in favorite_recipes])
            prompt_context += f"\nCONTEXT: The user's household loves these recipes, so try to include some of them: {fav_list}."

    recipe_list_text = "\n".join([f"id: {r.id}, name: \"{r.name}\", rating: {r.rating}" for r in all_recipes])
    prompt = (f"You are a Meal Plan Architect. Analyze the recipes and select exactly 7 for the theme: '{theme}'.{prompt_context}\n\nRecipes:\n{recipe_list_text}\n\nYour response MUST be ONLY a valid JSON array of 7 objects, each with 'id' and 'name'.")
    try:
        model = genai.GenerativeModel('gemini-1.5-pro')
        response = model.generate_content(prompt)
        plan_data = json.loads(response.text.strip().replace('```json', '').replace('```', ''))
        
        valid_user_recipe_ids = {r.id for r in all_recipes}
        for meal in plan_data:
            if meal.get('id') not in valid_user_recipe_ids:
                return jsonify({'error': 'The AI returned an invalid plan, suggesting a recipe you do not have. Please try generating the plan again.'}), 400

        if len(plan_data) != 7: raise ValueError("AI did not return 7 recipes.")
        return jsonify(plan_data)
    except Exception as e:
        return jsonify({'error': 'The AI failed to generate a valid plan. Please try again.'}), 500

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
def generate_from_ingredients_api():
    data = request.get_json()
    ingredients_text = data.get('ingredients', '')
    if not ingredients_text.strip(): return jsonify({'error': 'Please enter some ingredients.'}), 400
    prompt = (f"You are a creative chef with these ingredients: {ingredients_text}. Invent a practical recipe using them. Assume basic staples. Provide a complete recipe: name, ingredient list, and instructions.")
    try:
        model = genai.GenerativeModel('gemini-1.5-pro')
        response = model.generate_content(prompt)
        ai_response = response.text
    except Exception as e:
        ai_response = "Sorry, the AI assistant is unavailable."
    return jsonify({'generated_recipe': ai_response})

@app.route('/api/remix-recipe', methods=['POST'])
@login_required
def remix_recipe_api():
    data = request.get_json()
    recipe_id = data.get('recipe_id')
    remix_type = data.get('remix_type')
    recipe = Recipe.query.filter_by(id=recipe_id, household_id=current_user.household_id).first()
    if not recipe: return jsonify({'error': 'Recipe not found'}), 404
    ingredient_list = "\n".join([f"- {ri.quantity} {ri.unit} of {ri.ingredient.name}" for ri in recipe.ingredients])
    
    prompt = (f"Please rewrite the recipe '{recipe.name}' to be '{remix_type}'. Provide a completely new version: new name, full ingredient list, and step-by-step instructions.")

    try:
        model = genai.GenerativeModel('gemini-1.5-pro')
        response = model.generate_content(prompt)
        ai_response = response.text
    except Exception as e:
        ai_response = "Sorry, the AI assistant is unavailable."
    return jsonify({'remixed_recipe': ai_response})

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
    today = date.today()
    start_of_week = today - timedelta(days=today.weekday())

    if request.method == 'POST':
        end_of_week = start_of_week + timedelta(days=6)
        MealPlan.query.filter(
            MealPlan.household_id == current_user.household_id, 
            MealPlan.meal_date.between(start_of_week, end_of_week)
        ).delete()
        
        for i in range(7):
            current_day = start_of_week + timedelta(days=i)
            day_str = current_day.strftime('%Y-%m-%d')
            
            for slot in ['Breakfast', 'Lunch', 'Dinner']:
                recipe_ids = request.form.getlist(f'day-{day_str}-{slot}[]')
                for recipe_id in recipe_ids:
                    if recipe_id and recipe_id.isdigit():
                        meal_plan_entry = MealPlan(
                            meal_date=current_day, 
                            recipe_id=int(recipe_id), 
                            household_id=current_user.household_id,
                            meal_slot=slot
                        )
                        db.session.add(meal_plan_entry)

        db.session.commit()
        flash('Meal plan saved successfully!', 'success')
        return redirect(url_for('meal_plan'))

    days_of_week = [(start_of_week + timedelta(days=i)) for i in range(7)]
    all_meals = MealPlan.query.filter_by(household_id=current_user.household_id).filter(MealPlan.meal_date.between(start_of_week, start_of_week + timedelta(days=6))).all()
    
    planned_meals = {day.strftime('%Y-%m-%d'): {'Breakfast': [], 'Lunch': [], 'Dinner': []} for day in days_of_week}
    for meal in all_meals:
        if meal.recipe:
            day_str = meal.meal_date.strftime('%Y-%m-%d')
            if meal.meal_slot in planned_meals[day_str]:
                planned_meals[day_str][meal.meal_slot].append(meal.recipe)

    all_recipes = Recipe.query.filter_by(household_id=current_user.household_id).order_by(Recipe.name).all()
    
    recipes_by_type = {
        'Main Course': [r for r in all_recipes if r.meal_type == 'Main Course'],
        'Side Dish': [r for r in all_recipes if r.meal_type == 'Side Dish'],
        'Dessert': [r for r in all_recipes if r.meal_type == 'Dessert']
    }
    
    recipes_for_js = {
        'Main Course': [{'id': r.id, 'name': r.name, 'meal_type': r.meal_type} for r in recipes_by_type['Main Course']],
        'Side Dish': [{'id': r.id, 'name': r.name, 'meal_type': r.meal_type} for r in recipes_by_type['Side Dish']],
        'Dessert': [{'id': r.id, 'name': r.name, 'meal_type': r.meal_type} for r in recipes_by_type['Dessert']]
    }

    return render_template('meal_plan.html', 
                           days=days_of_week, 
                           planned_meals=planned_meals, 
                           recipes_by_type=recipes_by_type,
                           recipes_for_js=recipes_for_js)

@app.route('/shopping-list')
@login_required
def shopping_list():
    today = date.today()
    start_of_week = today - timedelta(days=today.weekday())
    end_of_week = start_of_week + timedelta(days=6)
    
    planned_meals = MealPlan.query.filter_by(household_id=current_user.household_id).filter(MealPlan.meal_date.between(start_of_week, end_of_week)).all()
    required = {}
    for meal in planned_meals:
        if meal.recipe:
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

    return render_template('shopping_list.html', 
                           grouped_list=grouped_list,
                           ingredients_in_pantry=in_pantry)

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