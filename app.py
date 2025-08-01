from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import desc
from datetime import date, timedelta, datetime
import os
import csv
from werkzeug.utils import secure_filename
import random
from flask import Response
import io
import json
import google.generativeai as genai
from dotenv import load_dotenv

# --- Load API Key from .env file ---
load_dotenv()

# --- App Configuration ---
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your_secret_key'
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URL", 'sqlite:///meal_engine.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'uploads'
db = SQLAlchemy(app)

# --- Configure AI ---
try:
    genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
    print("DEBUG: Google AI configured successfully from .env file.")
except Exception as e:
    print(f"Error configuring Google AI. Is the key in your .env file correct? Error: {e}")

# --- Database Models ---
class Recipe(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    instructions = db.Column(db.Text, nullable=False)
    servings = db.Column(db.Integer)
    prep_time = db.Column(db.String(50))
    cook_time = db.Column(db.String(50))
    ingredients = db.relationship('RecipeIngredient', backref='recipe', lazy=True, cascade="all, delete-orphan")
    meal_plan_entries = db.relationship('MealPlan', backref='recipe', lazy=True)

class Ingredient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    recipes = db.relationship('RecipeIngredient', backref='ingredient', lazy=True)
    pantry_item = db.relationship('PantryItem', backref='ingredient', uselist=False, cascade="all, delete-orphan")

class RecipeIngredient(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    recipe_id = db.Column(db.Integer, db.ForeignKey('recipe.id'), nullable=False)
    ingredient_id = db.Column(db.Integer, db.ForeignKey('ingredient.id'), nullable=False)
    quantity = db.Column(db.Float, nullable=False, default=0)
    unit = db.Column(db.String(50), nullable=True)

class MealPlan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    meal_date = db.Column(db.Date, nullable=False)
    recipe_id = db.Column(db.Integer, db.ForeignKey('recipe.id'), nullable=False)

class PantryItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ingredient_id = db.Column(db.Integer, db.ForeignKey('ingredient.id'), unique=True, nullable=False)
    quantity = db.Column(db.Float, nullable=False, default=0)
    unit = db.Column(db.String(50), nullable=True)
    stock_level = db.Column(db.String(20), nullable=False, default='Full')
    date_updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# --- Core Routes ---
@app.route('/')
def index():
    # This command ensures tables are created on the first request in a new environment
    with app.app_context():
        db.create_all()
    all_recipes = Recipe.query.all()
    random_recipe = random.choice(all_recipes) if all_recipes else None
    return render_template('index.html', random_recipe=random_recipe)

@app.route('/recipes')
def list_recipes():
    query = request.args.get('query', '')
    pantry_filter_active = request.args.get('filter') == 'pantry'
    sort_order = request.args.get('sort', 'asc')
    base_query = Recipe.query
    if sort_order == 'desc':
        base_query = base_query.order_by(desc(Recipe.name))
    else:
        base_query = base_query.order_by(Recipe.name)
    if pantry_filter_active:
        pantry_items_in_stock = PantryItem.query.filter(PantryItem.quantity > 0).all()
        pantry_stock = {item.ingredient_id: item.quantity for item in pantry_items_in_stock}
        all_recipes = base_query.all()
        recipes = []
        for recipe in all_recipes:
            can_make = True
            for required in recipe.ingredients:
                if required.ingredient_id not in pantry_stock or pantry_stock[required.ingredient_id] < required.quantity:
                    can_make = False
                    break
            if can_make:
                recipes.append(recipe)
    elif query:
        search_term = f"%{query}%"
        recipes = base_query.filter(db.or_(Recipe.name.ilike(search_term), Recipe.instructions.ilike(search_term))).all()
    else:
        recipes = base_query.all()
    return render_template('recipes.html', recipes=recipes, query=query, pantry_filter_active=pantry_filter_active, sort_order=sort_order)


# --- AI Quick Add Route ---
@app.route('/ai-quick-add', methods=['POST'])
def ai_quick_add():
    recipe_name = request.form.get('recipe_name')
    if not recipe_name:
        flash('Please enter a recipe name.', 'warning')
        return redirect(url_for('list_recipes'))
    if Recipe.query.filter(db.func.lower(Recipe.name) == db.func.lower(recipe_name)).first():
        flash('A recipe with this name already exists.', 'info')
        return redirect(url_for('list_recipes'))
    prompt = (
        f"You are a recipe database assistant. Generate a typical recipe for: '{recipe_name}'. "
        "Your response MUST be ONLY a valid JSON object with two keys: "
        "1. 'instructions': A string with step-by-step instructions, using '\\n' for new lines. "
        "2. 'ingredients': An array of objects, each with 'name' (string), 'quantity' (float/int), and 'unit' (string)."
    )
    try:
        model = genai.GenerativeModel('gemini-1.5-pro')
        response = model.generate_content(prompt)
        clean_response = response.text.strip().replace('```json', '').replace('```', '')
        recipe_data = json.loads(clean_response)
        new_recipe = Recipe(name=recipe_name, instructions=recipe_data['instructions'])
        db.session.add(new_recipe)
        db.session.flush()
        for ing_data in recipe_data['ingredients']:
            ingredient_name = ing_data['name'].strip()
            ingredient_obj = Ingredient.query.filter(db.func.lower(Ingredient.name) == db.func.lower(ingredient_name)).first()
            if not ingredient_obj:
                ingredient_obj = Ingredient(name=ingredient_name)
                db.session.add(ingredient_obj)
                db.session.flush()
            recipe_ingredient = RecipeIngredient(
                recipe_id=new_recipe.id,
                ingredient_id=ingredient_obj.id,
                quantity=float(ing_data.get('quantity', 0)),
                unit=ing_data.get('unit', '')
            )
            db.session.add(recipe_ingredient)
        db.session.commit()
        flash(f'Successfully generated and saved "{recipe_name}"!', 'success')
    except Exception as e:
        db.session.rollback()
        print(f"AI Quick Add Error: {e}")
        flash('The AI failed to generate a valid recipe. Please try a different name.', 'danger')
    return redirect(url_for('list_recipes'))


# --- Unified Ingredient & Pantry Routes ---
@app.route('/ingredients', methods=['GET', 'POST'])
def list_ingredients():
    if request.method == 'POST':
        name = request.form.get('name')
        if name and not Ingredient.query.filter_by(name=name).first():
            new_ingredient = Ingredient(name=name)
            db.session.add(new_ingredient)
            db.session.commit()
            flash(f'"{name}" added to master list.', 'success')
        return redirect(url_for('list_ingredients'))
    query = request.args.get('query', '')
    stock_filter = request.args.get('filter', 'all')
    base_query = Ingredient.query
    if query:
        base_query = base_query.filter(Ingredient.name.ilike(f"%{query}%"))
    if stock_filter == 'in_pantry':
        base_query = base_query.join(PantryItem).filter(PantryItem.quantity > 0)
    elif stock_filter == 'low':
        base_query = base_query.join(PantryItem).filter(PantryItem.stock_level == 'Low')
    elif stock_filter == 'out':
        base_query = base_query.join(PantryItem).filter(PantryItem.stock_level == 'Out')
    filtered_ingredients = base_query.order_by(Ingredient.name).all()
    pantry_items = {item.ingredient_id: item for item in PantryItem.query.all()}
    ingredient_data = []
    for ing in filtered_ingredients:
        ingredient_data.append({
            'ingredient': ing,
            'pantry_item': pantry_items.get(ing.id)
        })
    return render_template('ingredients.html', ingredient_data=ingredient_data, query=query, stock_filter=stock_filter)

@app.route('/update-pantry', methods=['POST'])
def update_pantry():
    action = request.form.get('action')
    # This ensures we stay on the same filtered/searched page after an update
    redirect_url = url_for('list_ingredients', filter=request.args.get('filter', 'all'), query=request.args.get('query', ''))
    if action == 'add':
        ingredient_id = int(request.form.get('ingredient_id'))
        if ingredient_id and not PantryItem.query.filter_by(ingredient_id=ingredient_id).first():
            new_item = PantryItem(ingredient_id=ingredient_id, quantity=1, unit="", stock_level='Full')
            db.session.add(new_item)
            db.session.commit()
            flash(f'"{new_item.ingredient.name}" added to pantry.', 'success')
    elif action == 'update_quantity':
        pantry_item_id = request.form.get('pantry_item_id')
        quantity = float(request.form.get('quantity', 0))
        item = PantryItem.query.get(pantry_item_id)
        if item:
            item.quantity = quantity
            db.session.commit()
            flash(f'Updated "{item.ingredient.name}" quantity.', 'info')
    elif action == 'update_stock':
        pantry_item_id = request.form.get('pantry_item_id')
        stock_level = request.form.get('stock_level')
        item = PantryItem.query.get(pantry_item_id)
        if item:
            item.stock_level = stock_level
            db.session.commit()
            flash(f'Set "{item.ingredient.name}" stock to {stock_level}.', 'info')
    elif action == 'delete':
        pantry_item_id = request.form.get('pantry_item_id')
        item = PantryItem.query.get(pantry_item_id)
        if item:
            flash(f'"{item.ingredient.name}" removed from pantry.', 'success')
            db.session.delete(item)
            db.session.commit()
    return redirect(redirect_url)


# --- Individual Recipe Routes ---
@app.route('/recipe/add', methods=['GET', 'POST'])
def add_recipe():
    if request.method == 'POST':
        new_recipe = Recipe(name=request.form.get('name'), instructions=request.form.get('instructions') or "No instructions provided.", servings=int(request.form.get('servings')) if request.form.get('servings') else None, prep_time=request.form.get('prep_time'), cook_time=request.form.get('cook_time'))
        db.session.add(new_recipe)
        db.session.commit()
        flash('Recipe added successfully! Please add its ingredients below.', 'success')
        return redirect(url_for('edit_recipe', recipe_id=new_recipe.id))
    return render_template('add_recipe.html')

@app.route('/recipe/<int:recipe_id>')
def view_recipe(recipe_id):
    recipe = Recipe.query.get_or_404(recipe_id)
    return render_template('view_recipe.html', recipe=recipe)

@app.route('/recipe/<int:recipe_id>/edit', methods=['GET', 'POST'])
def edit_recipe(recipe_id):
    recipe = Recipe.query.get_or_404(recipe_id)
    ingredients = Ingredient.query.order_by(Ingredient.name).all()
    if request.method == 'POST':
        recipe.name = request.form.get('name')
        recipe.instructions = request.form.get('instructions') or "No instructions provided."
        recipe.servings = request.form.get('servings')
        recipe.prep_time = request.form.get('prep_time')
        recipe.cook_time = request.form.get('cook_time')
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
def delete_recipe(recipe_id):
    recipe = Recipe.query.get_or_404(recipe_id)
    db.session.delete(recipe)
    db.session.commit()
    flash('Recipe deleted successfully!', 'success')
    return redirect(url_for('list_recipes'))


# --- AI Architect Routes ---
@app.route('/ai-architect')
def ai_architect():
    today = date.today()
    start_of_week = today - timedelta(days=today.weekday())
    return render_template('ai_architect.html', today=today, start_of_week=start_of_week)

@app.route('/api/build-plan', methods=['POST'])
def build_plan_api():
    data = request.get_json()
    theme = data.get('theme')
    all_recipes = Recipe.query.all()
    if len(all_recipes) < 7:
        return jsonify({'error': 'You need at least 7 recipes to build a full week plan.'}), 400
    recipe_list_text = "\n".join([f"id: {r.id}, name: \"{r.name}\"" for r in all_recipes])
    prompt = (f"You are a Meal Plan Architect. Analyze the recipes and select exactly 7 for the theme: '{theme}'.\n\nRecipes:\n{recipe_list_text}\n\nYour response MUST be ONLY a valid JSON array of 7 objects, each with 'id' and 'name'.")
    try:
        model = genai.GenerativeModel('gemini-1.5-pro')
        response = model.generate_content(prompt)
        clean_response = response.text.strip().replace('```json', '').replace('```', '')
        plan_data = json.loads(clean_response)
        if len(plan_data) != 7: raise ValueError("AI did not return 7 recipes.")
        return jsonify(plan_data)
    except Exception as e:
        print(f"AI Meal Plan Generation Error: {e}")
        return jsonify({'error': 'The AI failed to generate a valid plan. Please try again.'}), 500


# --- Other AI API Routes ---
@app.route('/api/generate-from-ingredients', methods=['POST'])
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
        print(f"AI Generation Error: {e}")
        ai_response = "Sorry, the AI assistant is unavailable."
    return jsonify({'generated_recipe': ai_response})

@app.route('/api/remix-recipe', methods=['POST'])
def remix_recipe_api():
    data = request.get_json()
    recipe_id = data.get('recipe_id')
    remix_type = data.get('remix_type')
    recipe = Recipe.query.get(recipe_id)
    if not recipe: return jsonify({'error': 'Recipe not found'}), 404
    ingredient_list = "\n".join([f"- {ri.quantity} {ri.unit} of {ri.ingredient.name}" for ri in recipe.ingredients])
    prompt = (f"Rewrite this recipe to be '{remix_type}':\n\n{recipe.name}\n\nIngredients:\n{ingredient_list}\n\nInstructions:\n{recipe.instructions}")
    try:
        model = genai.GenerativeModel('gemini-1.5-pro')
        response = model.generate_content(prompt)
        ai_response = response.text
    except Exception as e:
        print(f"AI Generation Error: {e}")
        ai_response = "Sorry, the AI assistant is unavailable."
    return jsonify({'remixed_recipe': ai_response})

@app.route('/api/suggest-recipes')
def suggest_recipes():
    all_recipes = Recipe.query.all()
    sample_size = min(len(all_recipes), 7)
    suggested = random.sample(all_recipes, sample_size) if sample_size > 0 else []
    return jsonify([{'id': r.id, 'name': r.name} for r in suggested])

@app.route('/api/search-recipes')
def search_recipes_api():
    query = request.args.get('query', '')
    if query:
        search_term = f"%{query}%"
        results = Recipe.query.filter(Recipe.name.ilike(search_term)).limit(10).all()
        return jsonify([{'id': r.id, 'name': r.name} for r in results])
    return jsonify([])


# --- Meal Plan & Shopping List Routes ---
@app.route('/add-to-plan/<int:recipe_id>')
def add_recipe_to_plan(recipe_id):
    recipe = Recipe.query.get_or_404(recipe_id)
    today = date.today()
    start_of_week = today - timedelta(days=today.weekday())
    end_of_week = start_of_week + timedelta(days=6)
    planned_meals = MealPlan.query.filter(MealPlan.meal_date.between(start_of_week, end_of_week)).all()
    planned_dates = {meal.meal_date for meal in planned_meals}
    first_available_day = None
    for i in range(7):
        current_day = start_of_week + timedelta(days=i)
        if current_day not in planned_dates:
            first_available_day = current_day
            break
    if first_available_day:
        new_plan_entry = MealPlan(meal_date=first_available_day, recipe_id=recipe.id)
        db.session.add(new_plan_entry)
        db.session.commit()
        flash(f'"{recipe.name}" was added to your plan for {first_available_day.strftime("%A")}!', 'success')
    else:
        flash('Your meal plan for this week is already full!', 'warning')
    return redirect(url_for('meal_plan'))

@app.route('/meal-plan', methods=['GET', 'POST'])
def meal_plan():
    today = date.today()
    start_of_week = today - timedelta(days=today.weekday())
    if request.method == 'POST':
        MealPlan.query.delete()
        for i in range(7):
            day_str = (start_of_week + timedelta(days=i)).strftime('%Y-%m-%d')
            recipe_id = request.form.get(f'day-{day_str}')
            if recipe_id and recipe_id.isdigit():
                meal_plan_entry = MealPlan(meal_date=(start_of_week + timedelta(days=i)), recipe_id=int(recipe_id))
                db.session.add(meal_plan_entry)
        db.session.commit()
        flash('Meal plan saved successfully!', 'success')
        return redirect(url_for('meal_plan'))
    days_of_week = [(start_of_week + timedelta(days=i)) for i in range(7)]
    planned_meals = {meal.meal_date.strftime('%Y-%m-%d'): Recipe.query.get(meal.recipe_id) for meal in MealPlan.query.all()}
    return render_template('meal_plan.html', days=days_of_week, planned_meals=planned_meals)

@app.route('/shopping-list')
def shopping_list():
    today = date.today()
    start_of_week = today - timedelta(days=today.weekday())
    end_of_week = start_of_week + timedelta(days=6)
    planned_meals = MealPlan.query.filter(MealPlan.meal_date.between(start_of_week, end_of_week)).all()
    required = {}
    for meal in planned_meals:
        if meal.recipe:
            for item in meal.recipe.ingredients:
                ing_name = item.ingredient.name
                if ing_name not in required:
                    required[ing_name] = {'quantity': 0, 'units': set(), 'is_spice_or_staple': False}
                if item.quantity == 0 or not item.unit or item.unit.lower() == 'to taste':
                    required[ing_name]['is_spice_or_staple'] = True
                required[ing_name]['quantity'] += item.quantity
                if item.unit: required[ing_name]['units'].add(item.unit)
    pantry_stock = {item.ingredient.name: item for item in PantryItem.query.all()}
    to_buy = {}
    in_pantry = {}
    for name, details in required.items():
        if name in pantry_stock:
            pantry_item = pantry_stock[name]
            in_pantry[name] = {'details': details, 'pantry_item': pantry_item}
            if not details['is_spice_or_staple']:
                needed = details['quantity']
                if pantry_item.unit in details['units'] and pantry_item.quantity < needed:
                    to_buy[name] = {'quantity': needed - pantry_item.quantity, 'units': details['units']}
            else:
                if pantry_item.stock_level == 'Low': to_buy[name] = {'quantity': None, 'units': {'Check Level'}}
                elif pantry_item.stock_level == 'Out': to_buy[name] = {'quantity': 1, 'units': {'Container'}}
        else:
            to_buy[name] = details
    return render_template('shopping_list.html', ingredients_to_buy=to_buy, ingredients_in_pantry=in_pantry)


# --- CSV Upload and Export Routes ---
@app.route('/upload', methods=['GET', 'POST'])
def upload_file():
    if request.method == 'POST':
        if 'file' not in request.files: flash('No file part', 'danger'); return redirect(request.url)
        file = request.files['file']
        if file.filename == '': flash('No selected file', 'danger'); return redirect(request.url)
        upload_type = request.form.get('upload_type')
        if not upload_type: flash('Please select an upload type', 'danger'); return redirect(request.url)
        if file:
            filename = secure_filename(file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            try:
                if upload_type == 'recipes':
                    count = process_recipes_csv(filepath)
                    flash(f'Successfully processed {count} recipes.', 'success')
                elif upload_type == 'recipe_ingredients':
                    count = process_recipe_ingredients_csv(filepath)
                    flash(f'Successfully processed {count} ingredient links.', 'success')
            except Exception as e:
                db.session.rollback()
                flash(f'An error occurred: {e}', 'danger')
            return redirect(url_for('upload_file'))
    return render_template('upload.html')

def process_recipes_csv(filepath):
    with open(filepath, mode='r', encoding='utf-8') as infile:
        reader = csv.DictReader(infile)
        count = 0
        for row in reader:
            instructions_text = row.get('instructions') or "No instructions provided."
            recipe_id = row.get('id')
            recipe = Recipe.query.get(recipe_id) if recipe_id else None
            if recipe:
                recipe.name = row.get('name')
                recipe.servings = row.get('servings')
                recipe.prep_time = row.get('prep_time')
                recipe.cook_time = row.get('cook_time')
                recipe.instructions = instructions_text
            else:
                new_recipe = Recipe(name=row.get('name'),servings=row.get('servings'),prep_time=row.get('prep_time'),cook_time=row.get('cook_time'),instructions=instructions_text)
                db.session.add(new_recipe)
            count += 1
        db.session.commit()
    return count

def process_recipe_ingredients_csv(filepath):
    RecipeIngredient.query.delete()
    with open(filepath, mode='r', encoding='utf-8') as infile:
        reader = csv.DictReader(infile)
        count = 0
        for row in reader:
            recipe_id = row.get('recipe_id')
            ingredient_name = row.get('ingredient_name', '').strip()
            if not (recipe_id and ingredient_name): continue
            recipe = Recipe.query.get(recipe_id)
            if not recipe: continue
            ingredient = Ingredient.query.filter_by(name=ingredient_name).first()
            if not ingredient:
                ingredient = Ingredient(name=ingredient_name)
                db.session.add(ingredient)
                db.session.flush()
            new_link = RecipeIngredient(recipe_id=recipe.id, ingredient_id=ingredient.id, quantity=float(row.get('quantity', 0)), unit=row.get('unit', ''))
            db.session.add(new_link)
            count += 1
        db.session.commit()
    return count


if __name__ == '__main__':
    with app.app_context():
        if not os.path.exists(app.config['UPLOAD_FOLDER']):
            os.makedirs(app.config['UPLOAD_FOLDER'])
        db.create_all()
    app.run(debug=True)