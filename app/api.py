import calendar
import json
import os
import random
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai
from flask import Blueprint, jsonify, request, flash, url_for, redirect, current_app
from flask_login import current_user, login_required
from sqlalchemy import and_
from datetime import date, timedelta, datetime

from . import db
from .decorators import require_ai_credits
from .models import (Ingredient, MealPlan, PantryItem, Recipe,
                     RecipeIngredient, SavedMeal, HistoricalPlan, ShoppingListItem)
from .utils import (award_achievement, convert_quantity_to_float,
                    deduct_ai_credit, sanitize_unit, ureg, pint, consume_ingredients_from_recipe)

api = Blueprint('api', __name__)

try:
    genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
except Exception as e:
    print(f"Error configuring Google AI: {e}")

@api.route('/ai-quick-add', methods=['POST'])
@login_required
@require_ai_credits
def ai_quick_add():
    ai_request_text = request.form.get('ai_request')
    if not ai_request_text:
        flash('Please enter a recipe request.', 'warning')
        return redirect(url_for('main.list_recipes'))

    prompt = f"""
        Generate a creative and delicious recipe based on the following user request: "{ai_request_text}".
        Give the recipe a suitable, creative name based on the request.
        Your output must be a single, valid JSON object with the following keys:
        - "name": The creative title of the recipe.
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
            ),
            request_options={"timeout": 25}
        )
        recipe_data = json.loads(response.text)

        if not recipe_data.get('name') or not recipe_data.get('instructions') or not recipe_data.get('ingredients'):
            flash('The AI returned an incomplete recipe. Please try a different request.', 'warning')
            return redirect(url_for('main.list_recipes'))
        
        if Recipe.query.filter_by(name=recipe_data['name'], household_id=current_user.household_id).first():
            flash(f'A recipe named "{recipe_data["name"]}" already exists. The AI generated a duplicate name.', 'info')
            return redirect(url_for('main.list_recipes'))

        new_recipe = Recipe(
            name=recipe_data['name'],
            instructions=recipe_data['instructions'],
            meal_type=recipe_data.get('meal_type', 'Main Course'),
            author=current_user,
            household_id=current_user.household_id
        )
        db.session.add(new_recipe)
        db.session.flush()
        
        ingredient_cache = {}
        for ing_data in recipe_data['ingredients']:
            ingredient_name = ing_data.get('name', '').strip()
            if not ingredient_name: continue
            
            lower_ingredient_name = ingredient_name.lower()
            
            if lower_ingredient_name in ingredient_cache:
                ingredient_obj = ingredient_cache[lower_ingredient_name]
            else:
                ingredient_obj = Ingredient.query.filter(db.func.lower(Ingredient.name) == lower_ingredient_name).first()
                if not ingredient_obj:
                    ingredient_obj = Ingredient(name=ingredient_name)
                    db.session.add(ingredient_obj)
                    db.session.flush()
                ingredient_cache[lower_ingredient_name] = ingredient_obj

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
        award_achievement(current_user, 'AI Assistant')
        flash(f'Successfully generated and saved "{recipe_data["name"]}"!', 'success')

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"AI Quick Add failed for user {current_user.email}. Error: {e}", exc_info=True)
        flash('The AI failed to generate a recipe. This can be due to a server timeout or an issue with the AI service. Please try your request again.', 'danger')
        
    return redirect(url_for('main.list_recipes'))

@api.route('/import-and-create-recipe', methods=['POST'])
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
        main_content = next((soup.select_one(s) for s in content_selectors if soup.select_one(s)), soup.body)
        page_text = ' '.join(main_content.get_text(separator=' ', strip=True).split())

        if len(page_text) < 150:
             return jsonify({'error': 'Could not extract enough readable content.'}), 400
        
        model = genai.GenerativeModel('gemini-2.5-pro')
        
        recipe_prompt = (f"""
            Analyze the following text from a recipe webpage and extract the recipe details.
            Your output must be a single, valid JSON object with keys: "name", "servings", "instructions", "meal_type", and "ingredients".
        """)
        
        recipe_response = model.generate_content(
            [recipe_prompt, page_text[:30000]],
            generation_config=genai.types.GenerationConfig(response_mime_type="application/json")
        )
        recipe_data = json.loads(recipe_response.text)

        if not all(k in recipe_data for k in ['name', 'instructions', 'ingredients']):
            return jsonify({'error': 'The AI could not understand the recipe from that URL.'}), 400
        
        ingredient_list_for_nutrition = ", ".join([f"{ing.get('quantity', '')} {ing.get('unit', '')} {ing.get('name', '')}" for ing in recipe_data['ingredients']])
        nutrition_prompt = f"""
            Analyze the ingredient list: {ingredient_list_for_nutrition} for {recipe_data.get('servings', 1) or 1} servings.
            Estimate nutritional info PER SERVING. Output a JSON object with keys: "calories", "protein", "fat", "carbs".
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
            
            recipe_ingredient = RecipeIngredient(
                recipe_id=new_recipe.id,
                ingredient_id=ingredient_obj.id,
                quantity=convert_quantity_to_float(ing_data.get('quantity', '0')),
                unit=ing_data.get('unit', '')
            )
            db.session.add(recipe_ingredient)
        
        deduct_ai_credit(current_user)
        db.session.commit()
        award_achievement(current_user, 'Web Scraper')
        flash(f'Successfully imported "{new_recipe.name}"! Please review the details.', 'success')
        return jsonify({'success': True, 'recipe_id': new_recipe.id})

    except requests.exceptions.RequestException:
        return jsonify({'error': f'Failed to fetch the URL.'}), 500
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'An unexpected error occurred during import.'}), 500

@api.route('/build-plan', methods=['POST'])
@login_required
@require_ai_credits
def build_plan_api():
    data = request.get_json()
    duration = data.get('duration', 'week')
    theme = data.get('theme')
    use_pantry = data.get('use_pantry', False)
    focus_favorites = data.get('focus_favorites', False)
    takeout_days = int(data.get('takeout_days', 0))
    meal_slots_to_plan = data.get('meal_slots', ['Breakfast', 'Lunch', 'Dinner']) or ['Breakfast', 'Lunch', 'Dinner']

    all_recipes = Recipe.query.filter_by(household_id=current_user.household_id).all()
    
    prompt_sections = []
    if 'Breakfast' in meal_slots_to_plan:
        breakfast_recipes = [r for r in all_recipes if r.meal_type in ('Breakfast', 'Snack')]
        prompt_sections.append(f"Available Breakfasts:\n" + "\n".join([f"id: {r.id}, name: \"{r.name}\"" for r in breakfast_recipes]) if breakfast_recipes else "No breakfast recipes available.")

    if 'Lunch' in meal_slots_to_plan:
        lunch_recipes = [r for r in all_recipes if r.meal_type in ('Lunch', 'Side Dish', 'Main Course', 'Meal Prep')]
        prompt_sections.append(f"Available Lunches:\n" + "\n".join([f"id: {r.id}, name: \"{r.name}\"" for r in lunch_recipes]) if lunch_recipes else "No lunch recipes available.")

    if 'Dinner' in meal_slots_to_plan:
        dinner_recipes = [r for r in all_recipes if r.meal_type in ('Main Course', 'Meal Prep')]
        prompt_sections.append(f"Available Dinners:\n" + "\n".join([f"id: {r.id}, name: \"{r.name}\"" for r in dinner_recipes]) if dinner_recipes else "No dinner recipes available.")
    
    prompt_context = ""
    if use_pantry:
        pantry_items = PantryItem.query.filter(PantryItem.household_id == current_user.household_id, PantryItem.quantity > 0).all()
        if pantry_items: prompt_context += f"\nCONTEXT: Prioritize recipes using: {', '.join([p.ingredient.name for p in pantry_items])}."
    if focus_favorites:
        favorite_recipes = Recipe.query.filter(Recipe.household_id == current_user.household_id, Recipe.rating >= 4).all()
        if favorite_recipes: prompt_context += f"\nCONTEXT: The user enjoys these recipes: {', '.join([f'\"{r.name}\"' for r in favorite_recipes])}."

    if duration == 'month':
        year, month, num_days = int(data.get('year')), int(data.get('month')), calendar.monthrange(int(data.get('year')), int(data.get('month')))[1]
        instruction = (f"Select recipes for {', '.join(meal_slots_to_plan)} for each of the {num_days} days of the month. Rules:\n"
                       f"1. Fill every requested slot. 2. Create {takeout_days} 'Takeout Night' dinners (id: null), spaced out. "
                       "3. Plan for 'Leftovers' for lunch (id: null) after a dinner. 4. High variety, no dinner repeats in 10 days.")
        json_structure = f"Response must be ONLY a valid JSON object. Top-level keys are day strings ('1'-'{num_days}'). Values are dictionaries with keys {json.dumps(meal_slots_to_plan)}, where each value is an object with 'id' and 'name'."
    else: # week
        instruction = (f"Select recipes for {', '.join(meal_slots_to_plan)} for 7 days (Monday-Sunday). Rules:\n"
                       f"1. Fill every requested slot. 2. Create {takeout_days} 'Takeout Night' dinners (id: null). "
                       "3. Plan for 'Leftovers' for lunch 2-3 times. 4. No recipe repeats.")
        json_structure = f"Response must be ONLY a valid JSON object. Top-level keys are days ('Monday'-'Sunday'). Values are dictionaries with keys {json.dumps(meal_slots_to_plan)}, where each value is an object with 'id' and 'name'."

    final_prompt = (f"Create a diverse and logical meal plan with theme: '{theme}'.\n{instruction}{prompt_context}\n\n{'\n\n'.join(prompt_sections)}\n\n{json_structure}")
    
    try:
        model = genai.GenerativeModel('gemini-2.5-pro')
        response = model.generate_content(final_prompt, generation_config=genai.types.GenerationConfig(response_mime_type="application/json"))
        plan_data = json.loads(response.text.strip())

        response_payload = {'duration': duration, 'plan': plan_data}
        if duration == 'month': response_payload.update({'year': year, 'month': month})
        
        deduct_ai_credit(current_user)
        db.session.commit()
        award_achievement(current_user, 'AI Architect')
        return jsonify(response_payload)
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'The AI failed to generate a valid plan. Details: {str(e)}'}), 500

@api.route('/save-ai-plan', methods=['POST'])
@login_required
def save_ai_plan():
    data = request.get_json()
    plan_data = data.get('plan')
    duration = data.get('duration')

    try:
        if duration == 'month':
            year, month = int(data.get('year')), int(data.get('month'))
            start_date, end_date = date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
            MealPlan.query.filter(and_(MealPlan.household_id == current_user.household_id, MealPlan.meal_date.between(start_date, end_date))).delete(synchronize_session=False)

            for day_str, meals in plan_data.items():
                current_date = date(year, month, int(day_str))
                for slot, meal in meals.items():
                    if meal and meal.get('name') and meal['name'] != 'Unplanned':
                        db.session.add(MealPlan(household_id=current_user.household_id, meal_date=current_date, meal_slot=slot, recipe_id=meal.get('id'), custom_item_name=None if meal.get('id') else meal.get('name')))
            
            flash(f'Your AI-generated plan for {start_date.strftime("%B %Y")} has been saved!', 'success')
            redirect_url = url_for('main.monthly_plan', year=year, month=month)
        else: # week
            today = date.today()
            start_of_week = today - timedelta(days=today.weekday())
            MealPlan.query.filter(MealPlan.household_id == current_user.household_id, MealPlan.meal_date.between(start_of_week, start_of_week + timedelta(days=6))).delete(synchronize_session=False)

            for i, day_name in enumerate(['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']):
                current_date = start_of_week + timedelta(days=i)
                for slot, meal in plan_data.get(day_name, {}).items():
                    if meal and meal.get('name') and meal['name'] != 'Unplanned':
                        db.session.add(MealPlan(household_id=current_user.household_id, meal_date=current_date, meal_slot=slot, recipe_id=meal.get('id'), custom_item_name=None if meal.get('id') else meal.get('name')))
            
            flash('Your AI-generated weekly plan has been saved!', 'success')
            redirect_url = url_for('main.meal_plan', start_date=start_of_week.strftime('%Y-%m-%d'))
        
        db.session.commit()
        return jsonify({'success': True, 'redirect_url': redirect_url})
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@api.route('/set-rating/<int:recipe_id>', methods=['POST'])
@login_required
def set_rating(recipe_id):
    recipe = Recipe.query.filter_by(id=recipe_id, household_id=current_user.household_id).first_or_404()
    rating = request.get_json().get('rating')
    if rating is not None and 0 <= int(rating) <= 5:
        recipe.rating = int(rating)
        db.session.commit()
        if recipe.rating == 5: award_achievement(current_user, 'Top Chef')
        return jsonify({'success': True, 'rating': recipe.rating})
    return jsonify({'success': False, 'message': 'Invalid rating.'}), 400

@api.route('/toggle-favorite/<int:recipe_id>', methods=['POST'])
@login_required
def toggle_favorite(recipe_id):
    recipe = Recipe.query.filter_by(id=recipe_id, household_id=current_user.household_id).first_or_404()
    recipe.is_favorite = not recipe.is_favorite
    db.session.commit()
    return jsonify({'is_favorite': recipe.is_favorite})

@api.route('/generate-from-ingredients', methods=['POST'])
@login_required
@require_ai_credits
def generate_from_ingredients_api():
    ingredients_text = request.get_json().get('ingredients', '')
    if not ingredients_text.strip(): return jsonify({'error': 'Please enter some ingredients.'}), 400
    prompt = f"You are a creative chef with: {ingredients_text}. Invent a practical recipe using them. Assume basic staples. Provide a complete recipe: name, ingredient list, and instructions."
    try:
        model = genai.GenerativeModel('gemini-2.5-pro')
        response = model.generate_content(prompt)
        deduct_ai_credit(current_user)
        db.session.commit()
        return jsonify({'generated_recipe': response.text})
    except Exception:
        db.session.rollback()
        return jsonify({'error': "Sorry, the AI assistant is unavailable."})

@api.route('/remix-recipe', methods=['POST'])
@login_required
@require_ai_credits
def remix_recipe_api():
    data = request.get_json()
    recipe = Recipe.query.filter_by(id=data.get('recipe_id'), household_id=current_user.household_id).first()
    if not recipe: return jsonify({'error': 'Recipe not found'}), 404
    
    prompt = (f"Rewrite the recipe '{recipe.name}' to be '{data.get('remix_type')}'. "
              "Output a valid JSON object with keys: \"name\" (a creative new name), "
              "\"instructions\" (a single string with steps separated by '\\n'), "
              "\"ingredients\" (an array of objects with \"name\", \"quantity\", \"unit\").")
    try:
        model = genai.GenerativeModel('gemini-2.5-pro')
        response = model.generate_content(prompt, generation_config=genai.types.GenerationConfig(response_mime_type="application/json"))
        remixed_data = json.loads(response.text)
        if not all(k in remixed_data for k in ['name', 'instructions', 'ingredients']): raise ValueError("Missing keys.")
        deduct_ai_credit(current_user)
        db.session.commit()
        return jsonify({'remixed_recipe': remixed_data})
    except Exception:
        db.session.rollback()
        return jsonify({'error': 'Sorry, the AI could not generate a valid recipe remix.'})

@api.route('/save-new-recipe', methods=['POST'])
@login_required
def save_new_recipe():
    data = request.get_json()
    if not data or not data.get('name') or not data.get('instructions'):
        return jsonify({'success': False, 'message': 'Invalid recipe data.'}), 400
    try:
        new_recipe = Recipe(name=data['name'], instructions=data['instructions'], meal_type=data.get('meal_type', 'Main Course'), author=current_user, household_id=current_user.household_id)
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
                
                db.session.add(RecipeIngredient(recipe_id=new_recipe.id, ingredient_id=ingredient_obj.id, quantity=convert_quantity_to_float(ing_data.get('quantity', '0')), unit=ing_data.get('unit', '')))
        
        db.session.commit()
        flash(f'New recipe "{new_recipe.name}" saved successfully!', 'success')
        return jsonify({'success': True, 'recipe_id': new_recipe.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': 'An error occurred while saving.'}), 500

@api.route('/suggest-recipes')
@login_required
def suggest_recipes():
    all_recipes = Recipe.query.filter_by(household_id=current_user.household_id).all()
    sample_size = min(len(all_recipes), 7)
    return jsonify([{'id': r.id, 'name': r.name, 'meal_type': r.meal_type} for r in (random.sample(all_recipes, sample_size) if sample_size > 0 else [])])

@api.route('/search-recipes')
@login_required
def search_recipes_api():
    query = request.args.get('query', '')
    if query:
        results = Recipe.query.filter_by(household_id=current_user.household_id).filter(Recipe.name.ilike(f"%{query}%")).limit(10).all()
        return jsonify([{'id': r.id, 'name': r.name, 'meal_type': r.meal_type} for r in results])
    return jsonify([])

@api.route('/get-saved-meals')
@login_required
def get_saved_meals():
    return jsonify([{'id': meal.id, 'name': meal.name, 'recipes': [{'id': r.id, 'name': r.name, 'meal_type': r.meal_type} for r in meal.recipes]} for meal in SavedMeal.query.filter_by(household_id=current_user.household_id).all()])

@api.route('/mark-meal-eaten', methods=['POST'])
@login_required
def mark_meal_eaten():
    data = request.get_json()
    try:
        meal_date = datetime.strptime(data.get('date'), '%Y-%m-%d').date()
        meal_slot = data.get('slot')
        
        meals_to_update = MealPlan.query.filter_by(
            household_id=current_user.household_id,
            meal_date=meal_date,
            meal_slot=meal_slot
        ).all()

        if not meals_to_update:
            return jsonify({'success': True, 'message': 'No meals to mark.'})

        new_status = not meals_to_update[0].is_eaten
        
        for meal in meals_to_update:
            meal.is_eaten = new_status

        if new_status:  # Only consume ingredients when marking as EATEN
            all_updated_items = set()
            all_skipped_items = set()
            for meal in meals_to_update:
                if meal.recipe:
                    updated, skipped = consume_ingredients_from_recipe(current_user, meal.recipe)
                    all_updated_items.update(updated)
                    all_skipped_items.update(skipped)
            
            if all_updated_items:
                flash(f'Pantry updated for: {", ".join(list(all_updated_items)[:5])}.', 'info')
            if all_skipped_items:
                flash(f'Could not update pantry for: {", ".join(list(all_skipped_items)[:3])}. Please check units.', 'warning')

        db.session.commit()
        return jsonify({'success': True, 'is_eaten': new_status})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@api.route('/load-historical-plan/<int:plan_id>', methods=['GET'])
@login_required
def load_historical_plan(plan_id):
    plan = HistoricalPlan.query.filter_by(id=plan_id, household_id=current_user.household_id).first_or_404()
    plan_data = {}
    for entry in plan.entries:
        day_key = str(entry.day_of_week)
        if day_key not in plan_data: plan_data[day_key] = {}
        if entry.meal_slot not in plan_data[day_key]: plan_data[day_key][entry.meal_slot] = []
        item_data = {'type': 'custom', 'name': entry.custom_item_name}
        if entry.recipe_id: item_data = {'type': 'recipe', 'id': entry.recipe.id, 'name': entry.recipe.name, 'meal_type': entry.recipe.meal_type}
        plan_data[day_key][entry.meal_slot].append(item_data)
    return jsonify(plan_data)

@api.route('/stock-pantry-from-list', methods=['POST'])
@login_required
def stock_pantry_from_list():
    data = request.get_json()
    items_to_add = data.get('items', [])

    try:
        for item_data in items_to_add:
            item_name = item_data.get('name')
            if not item_name: continue

            ingredient = Ingredient.query.filter(db.func.lower(Ingredient.name) == db.func.lower(item_name)).first()
            if not ingredient:
                ingredient = Ingredient(name=item_name.title(), category='Other')
                db.session.add(ingredient)
                db.session.flush()

            pantry_item = PantryItem.query.filter_by(
                household_id=current_user.household_id,
                ingredient_id=ingredient.id
            ).first()

            quantity_to_add = convert_quantity_to_float(item_data.get('quantity', '0'))
            unit_to_add = item_data.get('unit', '')

            if pantry_item:
                try:
                    with ureg.context('cooking', substance=ingredient.name.lower().replace(" ", "_")):
                        existing_qty = pantry_item.quantity * ureg(sanitize_unit(pantry_item.unit))
                        new_qty = quantity_to_add * ureg(sanitize_unit(unit_to_add))

                        if existing_qty.is_compatible_with(new_qty):
                            pantry_item.quantity = (existing_qty + new_qty.to(existing_qty.units)).magnitude
                        else:
                            pantry_item.quantity += quantity_to_add
                except (pint.errors.DimensionalityError, pint.errors.UndefinedUnitError):
                    pantry_item.quantity += quantity_to_add
            else:
                pantry_item = PantryItem(
                    household_id=current_user.household_id,
                    ingredient_id=ingredient.id,
                    quantity=quantity_to_add,
                    unit=unit_to_add
                )
                db.session.add(pantry_item)

            manual_id = item_data.get('manual_id')
            if manual_id:
                manual_item = db.session.get(ShoppingListItem, int(manual_id))
                if manual_item and manual_item.household_id == current_user.household_id:
                    db.session.delete(manual_item)
        
        db.session.commit()
        flash(f'{len(items_to_add)} items successfully added to your pantry!', 'success')
        return jsonify({'success': True})

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@api.route('/pantry-item/<int:pantry_item_id>', methods=['POST'])
@login_required
def update_pantry_item(pantry_item_id):
    item = db.session.get(PantryItem, pantry_item_id)
    if not item or item.household_id != current_user.household_id:
        return jsonify({'success': False, 'message': 'Item not found.'}), 404
    
    data = request.get_json()
    new_quantity = data.get('quantity')
    new_unit = data.get('unit')

    if new_quantity is not None:
        try:
            item.quantity = float(new_quantity)
        except (ValueError, TypeError):
            return jsonify({'success': False, 'message': 'Invalid quantity.'}), 400

    if new_unit is not None:
        item.unit = new_unit

    db.session.commit()
    return jsonify({'success': True, 'message': 'Pantry item updated.'})