import calendar
import random
import uuid
import io
import csv
from datetime import date, timedelta, datetime

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, current_app, jsonify, Response)
from flask_login import login_required, current_user
from sqlalchemy import desc, func, or_

from . import db
from .models import (Recipe, Ingredient, RecipeIngredient, MealPlan, PantryItem,
                     ShoppingListItem, SavedMeal, HistoricalPlan,
                     HistoricalPlanEntry, GroceryStore, HouseholdInvitation, User,
                     Household, Achievement, UserAchievement)
from .utils import award_achievement, ureg, sanitize_unit, pint

main = Blueprint('main', __name__)

@main.route('/')
def index():
    if not current_user.is_authenticated:
        return render_template('landing_page.html')
    
    recipe_count = Recipe.query.filter_by(household_id=current_user.household_id).count()
    five_star_count = Recipe.query.filter_by(household_id=current_user.household_id, rating=5).count()

    culinary_title = "Kitchen Apprentice"
    if recipe_count >= 50 and five_star_count >= 5:
        culinary_title = "Michelin Star Chef"
    elif recipe_count >= 30:
        culinary_title = "Head Chef"
    elif recipe_count >= 10:
        culinary_title = "Sous Chef"
    
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

    weekly_stats = {'scheduled': {}, 'consumed': {}}
    monthly_stats = {'scheduled': {}, 'consumed': {}}
    for nutrient in ['calories', 'protein', 'fat', 'carbs']:
        weekly_stats['scheduled'][nutrient] = sum(getattr(m.recipe, nutrient) or 0 for m in weekly_planned_meals if m.recipe)
        weekly_stats['consumed'][nutrient] = sum(getattr(m.recipe, nutrient) or 0 for m in weekly_planned_meals if m.recipe and m.is_eaten)
        monthly_stats['scheduled'][nutrient] = sum(getattr(m.recipe, nutrient) or 0 for m in monthly_planned_meals if m.recipe)
        monthly_stats['consumed'][nutrient] = sum(getattr(m.recipe, nutrient) or 0 for m in monthly_planned_meals if m.recipe and m.is_eaten)

    return render_template('index.html',
                           todays_meal_plan=todays_meal_plan,
                           weekly_stats=weekly_stats,
                           monthly_stats=monthly_stats,
                           kitchen_stats=kitchen_stats,
                           most_made_recipes=most_made_recipes,
                           culinary_title=culinary_title)

@main.route('/recipes')
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
        recipes = [r for r in all_user_recipes if r.ingredients and {ri.ingredient_id for ri in r.ingredients}.issubset(pantry_ingredient_ids)]
    elif favorites_filter_active:
        recipes = base_query.filter_by(is_favorite=True).all()
    elif query:
        search_term = f"%{query}%"
        recipes = base_query.filter(or_(Recipe.name.ilike(search_term), Recipe.instructions.ilike(search_term))).all()
    else:
        recipes = base_query.all()
        
    return render_template('recipes.html', page_class='page-recipes', recipes=recipes, query=query, pantry_filter_active=pantry_filter_active, favorites_filter_active=favorites_filter_active, sort_order=sort_order)

@main.route('/pantry', methods=['GET', 'POST'])
@login_required
def pantry():
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'delete':
            item = db.session.get(PantryItem, int(request.form.get('pantry_item_id')))
            if item and item.household_id == current_user.household_id:
                flash(f'"{item.ingredient.name}" removed from your pantry.', 'success')
                db.session.delete(item)
        
        elif action == 'add':
            ingredient_id = int(request.form.get('ingredient_id'))
            ingredient = db.session.get(Ingredient, ingredient_id)
            if ingredient:
                if not PantryItem.query.filter_by(ingredient_id=ingredient_id, household_id=current_user.household_id).first():
                    quantity = float(request.form.get('container_quantity') or 1)
                    unit = ingredient.consumable_unit if ingredient.is_container else ''
                    new_item = PantryItem(
                        ingredient_id=ingredient_id,
                        quantity=quantity,
                        unit=unit,
                        household_id=current_user.household_id
                    )
                    db.session.add(new_item)
                    award_achievement(current_user, 'Pantry Organizer')
                    flash(f'"{ingredient.name}" added to pantry.', 'success')
                else:
                    flash(f'"{ingredient.name}" is already in your pantry.', 'info')
            else:
                flash('Ingredient not found.', 'danger')

        db.session.commit()
        return redirect(url_for('main.pantry'))

    pantry_items = PantryItem.query.join(Ingredient).filter(
        PantryItem.household_id == current_user.household_id
    ).order_by(Ingredient.category, Ingredient.name).all()
    
    return render_template('pantry.html', pantry_items=pantry_items)

@main.route('/ingredients', methods=['GET', 'POST'])
@login_required
def list_ingredients():
    if request.method == 'POST':
        name = request.form.get('name')
        if name and not Ingredient.query.filter(db.func.lower(Ingredient.name) == db.func.lower(name.strip())).first():
            new_ingredient = Ingredient(name=name.strip().title())
            db.session.add(new_ingredient)
            db.session.commit()
            flash(f'"{name}" added to master ingredient list.', 'success')
        else:
            flash(f'"{name}" already exists or is invalid.', 'warning')
        return redirect(url_for('main.list_ingredients'))
    
    query = request.args.get('query', '')
    base_query = Ingredient.query
    if query:
        base_query = base_query.filter(Ingredient.name.ilike(f"%{query}%"))
    
    all_ingredients = base_query.order_by(Ingredient.category, Ingredient.name).all()
    
    ingredient_data = [{'ingredient': ing} for ing in all_ingredients]
    categories = ['Produce', 'Meat & Seafood', 'Dairy & Eggs', 'Pantry', 'Spices & Seasonings', 'Bakery', 'Frozen', 'Other']
    return render_template('ingredients.html', ingredient_data=ingredient_data, query=query, categories=categories)

@main.route('/update-ingredient-details', methods=['POST'])
@login_required
def update_ingredient_details():
    ingredient_id = request.form.get('ingredient_id')
    ingredient = db.session.get(Ingredient, int(ingredient_id))
    if ingredient:
        ingredient.category = request.form.get('category')
        ingredient.is_container = 'is_container' in request.form
        ingredient.consumable_unit = request.form.get('consumable_unit', '').strip() or None
        ingredient.container_prompt = request.form.get('container_prompt', '').strip() or None
        db.session.commit()
        flash(f'Details for "{ingredient.name}" updated.', 'success')
    return redirect(url_for('main.list_ingredients', query=request.args.get('query', '')))

# The old /update-pantry route has been removed as its logic is now in /pantry

@main.route('/recipe/add', methods=['GET', 'POST'])
@login_required
def add_recipe():
    if request.method == 'POST':
        new_recipe = Recipe(name=request.form.get('name'), instructions=request.form.get('instructions') or "No instructions provided.", servings=int(request.form.get('servings')) if request.form.get('servings') else None, prep_time=request.form.get('prep_time'), cook_time=request.form.get('cook_time'), meal_type=request.form.get('meal_type'), author=current_user, household_id=current_user.household_id)
        db.session.add(new_recipe)
        db.session.commit()
        award_achievement(current_user, 'The Creator')
        flash('Recipe added successfully! Please add its ingredients below.', 'success')
        return redirect(url_for('main.edit_recipe', recipe_id=new_recipe.id))
    return render_template('add_recipe.html', prefill={})

@main.route('/recipe/<int:recipe_id>')
@login_required
def view_recipe(recipe_id):
    recipe = Recipe.query.filter_by(id=recipe_id, household_id=current_user.household_id).first_or_404()
    if recipe.name == "Schrödinger's Soufflé":
        recipe.meal_type = random.choice(['Dessert', 'Snack', 'Side Dish'])
        flash("By observing the soufflé, you've collapsed its wave function into a single state!", "info")
    return render_template('view_recipe.html', recipe=recipe)

@main.route('/recipe/<int:recipe_id>/cook')
@login_required
def cook_recipe(recipe_id):
    recipe = Recipe.query.filter_by(id=recipe_id, household_id=current_user.household_id).first_or_404()
    steps = [step.strip() for step in recipe.instructions.strip().split('\n') if step.strip()]
    return render_template('cooking_mode.html', recipe=recipe, steps=steps)

@main.route('/recipe/<int:recipe_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_recipe(recipe_id):
    recipe = Recipe.query.filter_by(id=recipe_id, household_id=current_user.household_id).first_or_404()
    ingredients = Ingredient.query.order_by(Ingredient.name).all()
    if request.method == 'POST':
        recipe.name = request.form.get('name')
        recipe.instructions = request.form.get('instructions') or "No instructions provided."
        recipe.servings = int(request.form.get('servings')) if request.form.get('servings') else None
        recipe.prep_time = request.form.get('prep_time')
        recipe.cook_time = request.form.get('cook_time')
        recipe.meal_type = request.form.get('meal_type')
        recipe.calories = float(request.form.get('calories')) if request.form.get('calories') else None
        recipe.protein = float(request.form.get('protein')) if request.form.get('protein') else None
        recipe.fat = float(request.form.get('fat')) if request.form.get('fat') else None
        recipe.carbs = float(request.form.get('carbs')) if request.form.get('carbs') else None
        RecipeIngredient.query.filter_by(recipe_id=recipe.id).delete()
        for i in range(len(request.form.getlist('ingredient[]'))):
            ing_id = request.form.getlist('ingredient[]')[i]
            qty = request.form.getlist('quantity[]')[i]
            if ing_id and qty:
                db.session.add(RecipeIngredient(recipe_id=recipe.id, ingredient_id=int(ing_id), quantity=float(qty), unit=request.form.getlist('unit[]')[i]))
        db.session.commit()
        flash('Recipe updated successfully!', 'success')
        return redirect(url_for('main.view_recipe', recipe_id=recipe.id))
    return render_template('edit_recipe.html', recipe=recipe, ingredients=ingredients)

@main.route('/recipe/<int:recipe_id>/delete', methods=['POST'])
@login_required
def delete_recipe(recipe_id):
    recipe = Recipe.query.filter_by(id=recipe_id, household_id=current_user.household_id).first_or_404()
    db.session.delete(recipe)
    db.session.commit()
    flash('Recipe deleted successfully!', 'success')
    return redirect(url_for('main.list_recipes'))

@main.route('/saved-meals', methods=['GET', 'POST'])
@login_required
def saved_meals():
    if request.method == 'POST':
        if name := request.form.get('name'):
            new_saved_meal = SavedMeal(name=name, household_id=current_user.household_id)
            db.session.add(new_saved_meal)
            db.session.commit()
            flash(f'Saved Meal "{name}" created. Now add recipes to it.', 'success')
            return redirect(url_for('main.edit_saved_meal', saved_meal_id=new_saved_meal.id))
    all_saved_meals = SavedMeal.query.filter_by(household_id=current_user.household_id).order_by(SavedMeal.name).all()
    return render_template('saved_meals.html', saved_meals=all_saved_meals)

@main.route('/saved-meal/<int:saved_meal_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_saved_meal(saved_meal_id):
    saved_meal = SavedMeal.query.filter_by(id=saved_meal_id, household_id=current_user.household_id).first_or_404()
    if request.method == 'POST':
        saved_meal.name = request.form.get('name')
        new_recipe_ids = request.form.getlist('recipe_ids')
        saved_meal.recipes = Recipe.query.filter(Recipe.id.in_(new_recipe_ids), Recipe.household_id==current_user.household_id).all()
        db.session.commit()
        flash(f'Saved Meal "{saved_meal.name}" updated successfully!', 'success')
        return redirect(url_for('main.saved_meals'))
    all_recipes = Recipe.query.filter_by(household_id=current_user.household_id).order_by(Recipe.name).all()
    current_recipe_ids = {r.id for r in saved_meal.recipes}
    return render_template('edit_saved_meal.html', saved_meal=saved_meal, all_recipes=all_recipes, current_recipe_ids=current_recipe_ids)

@main.route('/saved-meal/<int:saved_meal_id>/delete', methods=['POST'])
@login_required
def delete_saved_meal(saved_meal_id):
    saved_meal = SavedMeal.query.filter_by(id=saved_meal_id, household_id=current_user.household_id).first_or_404()
    flash(f'Saved Meal "{saved_meal.name}" has been deleted.', 'success')
    db.session.delete(saved_meal)
    db.session.commit()
    return redirect(url_for('main.saved_meals'))

@main.route('/manage-plans')
@login_required
def manage_plans():
    plans = HistoricalPlan.query.filter_by(household_id=current_user.household_id).order_by(HistoricalPlan.name).all()
    return render_template('manage_plans.html', plans=plans)

@main.route('/delete-plan/<int:plan_id>', methods=['POST'])
@login_required
def delete_historical_plan(plan_id):
    plan = HistoricalPlan.query.filter_by(id=plan_id, household_id=current_user.household_id).first_or_404()
    flash(f'The plan template "{plan.name}" has been deleted.', 'success')
    db.session.delete(plan)
    db.session.commit()
    return redirect(url_for('main.manage_plans'))

@main.route('/monthly-plan', methods=['GET'])
@login_required
def monthly_plan():
    today = date.today()
    try:
        year = int(request.args.get('year', today.year))
        month = int(request.args.get('month', today.month))
    except (ValueError, TypeError):
        year, month = today.year, today.month
    
    cal = calendar.Calendar(firstweekday=0)
    month_days = cal.monthdatescalendar(year, month)
    first_day, last_day = month_days[0][0], month_days[-1][-1]
    
    all_meals_in_view = MealPlan.query.filter(MealPlan.household_id == current_user.household_id, MealPlan.meal_date.between(first_day, last_day)).all()

    daily_summaries = {day.strftime('%Y-%m-%d'): {'calories': 0, 'meals': []} for week in month_days for day in week}
    for meal in all_meals_in_view:
        day_str = meal.meal_date.strftime('%Y-%m-%d')
        meal_name = meal.recipe.name if meal.recipe else meal.custom_item_name
        if meal_name:
            daily_summaries[day_str]['meals'].append(f"{meal.meal_slot}: {meal_name}")
            if meal.recipe and meal.recipe.calories:
                daily_summaries[day_str]['calories'] += meal.recipe.calories
    
    current_month_date = date(year, month, 1)
    nav = {
        'current': current_month_date,
        'prev': current_month_date - timedelta(days=1),
        'next': current_month_date + timedelta(days=32)
    }
    
    start_of_month = date(year, month, 1)
    end_of_month = date(year, month, calendar.monthrange(year, month)[1])
    all_meals_this_month = [m for m in all_meals_in_view if start_of_month <= m.meal_date <= end_of_month and m.recipe]
    
    monthly_stats = {'scheduled': {}, 'consumed': {}}
    for nutrient in ['calories', 'protein', 'fat', 'carbs']:
        monthly_stats['scheduled'][nutrient] = sum(getattr(m.recipe, nutrient) or 0 for m in all_meals_this_month)
        monthly_stats['consumed'][nutrient] = sum(getattr(m.recipe, nutrient) or 0 for m in all_meals_this_month if m.is_eaten)
        
    weekly_summaries = []
    for week in month_days:
        week_stats = {'scheduled': {'calories': 0}, 'consumed': {'calories': 0}}
        for day in week:
            for meal in all_meals_in_view:
                if meal.meal_date == day and meal.recipe and meal.recipe.calories:
                    week_stats['scheduled']['calories'] += meal.recipe.calories
                    if meal.is_eaten: week_stats['consumed']['calories'] += meal.recipe.calories
        weekly_summaries.append(week_stats)
    
    return render_template('monthly_plan.html', page_class='page-monthly-plan', calendar_data=month_days, daily_summaries=daily_summaries, nav=nav, monthly_stats=monthly_stats, weekly_summaries=weekly_summaries)

@main.route('/ai-architect')
@login_required
def ai_architect():
    all_recipes = Recipe.query.filter_by(household_id=current_user.household_id).order_by(Recipe.name).all()
    recipes_by_type = {
        'Main Course': [r for r in all_recipes if r.meal_type == 'Main Course'],
        'Side Dish': [r for r in all_recipes if r.meal_type == 'Side Dish'],
        'Snack': [r for r in all_recipes if r.meal_type == 'Snack'],
        'Meal Prep': [r for r in all_recipes if r.meal_type == 'Meal Prep']
    }
    recipes_for_js = {k: [{'id': r.id, 'name': r.name, 'meal_type': r.meal_type} for r in v] for k, v in recipes_by_type.items()}
    return render_template('ai_architect.html', today=date.today(), calendar=calendar, recipes_for_js=recipes_for_js)

@main.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    household_owner = current_user.household.members[0]
    member_limit = current_app.config['HOUSEHOLD_LIMITS'].get(household_owner.subscription_plan, 2)
    is_full = len(current_user.household.members) >= member_limit
    is_unlimited = (member_limit == float('inf'))

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'generate_invite':
            if is_full and not is_unlimited: return jsonify({'error': f"Your household is full."}), 403
            HouseholdInvitation.query.filter_by(household_id=current_user.household_id).delete()
            token = str(uuid.uuid4())
            new_invitation = HouseholdInvitation(household_id=current_user.household_id, token=token, expires_at=datetime.utcnow() + timedelta(hours=24))
            db.session.add(new_invitation)
            db.session.commit()
            return jsonify({'invite_link': url_for('main.join_household', token=token, _external=True)})
        elif action == 'remove_member':
            member_to_remove = db.session.get(User, int(request.form.get('member_id')))
            if member_to_remove and member_to_remove.household_id == current_user.household_id and member_to_remove.id != current_user.id:
                new_household = Household(name=f"{member_to_remove.email.split('@')[0]}'s Household")
                db.session.add(new_household)
                db.session.flush()
                member_to_remove.household_id = new_household.id
                db.session.commit()
                flash(f"Removed {member_to_remove.email} from the household.", "success")
        elif action == 'update_household_name' and (new_name := request.form.get('household_name')):
            current_user.household.name = new_name
            db.session.commit()
            flash('Household name updated.', 'success')
        elif action == 'add_store' and (name := request.form.get('name')) and (url := request.form.get('search_url')) and '{query}' in url:
            db.session.add(GroceryStore(household_id=current_user.household_id, name=name, search_url=url))
            db.session.commit()
            flash(f'Grocery store "{name}" added.', 'success')
        elif action == 'delete_store' and (store_id := request.form.get('store_id')):
            store = db.session.get(GroceryStore, int(store_id))
            if store and store.household_id == current_user.household_id:
                db.session.delete(store)
                db.session.commit()
                flash(f'Grocery store "{store.name}" removed.', 'success')
        return redirect(url_for('main.profile'))

    stores = GroceryStore.query.filter_by(household_id=current_user.household_id).order_by(GroceryStore.name).all()
    all_achievements = Achievement.query.order_by(Achievement.name).all()
    unlocked_achievement_ids = {ua.achievement_id for ua in current_user.achievements}
    return render_template('profile.html', 
                           stores=stores, 
                           member_limit=member_limit, 
                           is_full=is_full,
                           all_achievements=all_achievements,
                           unlocked_achievement_ids=unlocked_achievement_ids,
                           is_unlimited=is_unlimited)

@main.route('/join-household/<token>')
@login_required
def join_household(token):
    invitation = HouseholdInvitation.query.filter_by(token=token).first()
    if not invitation or invitation.expires_at < datetime.utcnow():
        flash('This invitation link is invalid or has expired.', 'danger')
        return redirect(url_for('main.profile'))
    if current_user.household_id == invitation.household_id:
        flash('You are already a member of this household.', 'info')
        return redirect(url_for('main.profile'))
    
    target_household = invitation.household
    if not target_household.members:
        flash("Cannot join an empty household through an invite.", "danger")
        return redirect(url_for('main.profile'))

    household_owner = target_household.members[0]
    member_limit = current_app.config['HOUSEHOLD_LIMITS'].get(household_owner.subscription_plan, 2)
    if len(target_household.members) >= member_limit:
        flash(f'The "{target_household.name}" household is full.', 'warning')
        return redirect(url_for('main.profile'))

    old_household = current_user.household
    current_user.household_id = invitation.household_id
    if len(old_household.members) == 0: db.session.delete(old_household)
    db.session.delete(invitation)
    db.session.commit()
    flash(f'You have successfully joined the "{invitation.household.name}" household!', 'success')
    return redirect(url_for('main.profile'))

@main.route('/add-to-plan/<int:recipe_id>')
@login_required
def add_recipe_to_plan(recipe_id):
    recipe = Recipe.query.filter_by(id=recipe_id, household_id=current_user.household_id).first_or_404()
    db.session.add(MealPlan(meal_date=date.today(), recipe_id=recipe.id, household_id=current_user.household_id, meal_slot='Dinner'))
    db.session.commit()
    flash(f'"{recipe.name}" was added to your plan for dinner today!', 'success')
    return redirect(url_for('main.meal_plan'))

@main.route('/meal-plan', methods=['GET', 'POST'])
@login_required
def meal_plan():
    start_date_str = request.args.get('start_date')
    today = date.today()
    start_of_week = datetime.strptime(start_date_str, '%Y-%m-%d').date() if start_date_str else today - timedelta(days=today.weekday())

    if request.method == 'POST':
        week_start_date = datetime.strptime(request.form.get('week_start_date'), '%Y-%m-%d').date()
        end_of_week = week_start_date + timedelta(days=6)
        MealPlan.query.filter(MealPlan.household_id == current_user.household_id, MealPlan.meal_date.between(week_start_date, end_of_week)).delete(synchronize_session=False)

        for i in range(7):
            current_day = week_start_date + timedelta(days=i)
            day_str = current_day.strftime('%Y-%m-%d')
            for slot in ['Breakfast', 'Lunch', 'Dinner', 'Snack']:
                for recipe_id in request.form.getlist(f'day-{day_str}-{slot}-recipe[]'):
                    if recipe_id.isdigit(): db.session.add(MealPlan(meal_date=current_day, recipe_id=int(recipe_id), household_id=current_user.household_id, meal_slot=slot))
                for item_name in request.form.getlist(f'day-{day_str}-{slot}-custom[]'):
                    if item_name: db.session.add(MealPlan(meal_date=current_day, custom_item_name=item_name, household_id=current_user.household_id, meal_slot=slot))
        
        if historical_plan_name := request.form.get('historical_plan_name'):
            new_hist_plan = HistoricalPlan(name=historical_plan_name, household_id=current_user.household_id)
            db.session.add(new_hist_plan)
            db.session.flush()
            for entry in MealPlan.query.filter(MealPlan.household_id == current_user.household_id, MealPlan.meal_date.between(week_start_date, end_of_week)).all():
                db.session.add(HistoricalPlanEntry(historical_plan_id=new_hist_plan.id, day_of_week=entry.meal_date.weekday(), meal_slot=entry.meal_slot, recipe_id=entry.recipe_id, custom_item_name=entry.custom_item_name))
            flash(f'Meal plan saved and also stored as "{historical_plan_name}"!', 'success')
            award_achievement(current_user, 'Weekly Planner')
        else:
            flash('Meal plan saved successfully!', 'success')

        db.session.commit()
        return redirect(url_for('main.meal_plan', start_date=week_start_date.strftime('%Y-%m-%d')))

    end_of_week = start_of_week + timedelta(days=6)
    days_of_week = [start_of_week + timedelta(days=i) for i in range(7)]
    all_meals = MealPlan.query.filter(MealPlan.household_id == current_user.household_id, MealPlan.meal_date.between(start_of_week, end_of_week)).all()
    planned_meals = {day.strftime('%Y-%m-%d'): {slot: [] for slot in ['Breakfast', 'Lunch', 'Dinner', 'Snack']} for day in days_of_week}
    for meal in all_meals:
        if meal.meal_date.strftime('%Y-%m-%d') in planned_meals:
            planned_meals[meal.meal_date.strftime('%Y-%m-%d')][meal.meal_slot].append(meal)
    
    TRAY_CATEGORY_MAP = {'Main Course': 'Main Course', 'Dinner': 'Main Course', 'Side Dish': 'Side Dish', 'Dessert': 'Dessert', 'Snack': 'Snack', 'Breakfast': 'Snack', 'Appetizer': 'Snack', 'Meal Prep': 'Meal Prep'}
    tray_categories = sorted(list(set(TRAY_CATEGORY_MAP.values())))
    all_recipes = Recipe.query.filter_by(household_id=current_user.household_id).order_by(Recipe.name).all()
    recipes_by_type = {category: [r for r in all_recipes if TRAY_CATEGORY_MAP.get(r.meal_type.strip().title() if r.meal_type else 'Main Course', 'Main Course') == category] for category in tray_categories}
    recipes_for_js = {category: [{'id': r.id, 'name': r.name, 'meal_type': r.meal_type} for r in recipe_list] for category, recipe_list in recipes_by_type.items()}
    
    initial_tray_recipes = Recipe.query.filter_by(household_id=current_user.household_id).order_by(desc(Recipe.id)).limit(5).all()
    initial_tray_recipes_js = {}
    for r in initial_tray_recipes:
        tray_category = TRAY_CATEGORY_MAP.get(r.meal_type.strip().title() if r.meal_type else 'Main Course', 'Main Course')
        initial_tray_recipes_js.setdefault(tray_category, []).append({'id': r.id, 'name': r.name, 'meal_type': r.meal_type})
    
    historical_plans = HistoricalPlan.query.filter_by(household_id=current_user.household_id).order_by(HistoricalPlan.name).all()
    
    daily_stats = {day.strftime('%Y-%m-%d'): {'scheduled': {'calories': 0}, 'consumed': {'calories': 0}} for day in days_of_week}
    weekly_stats = {'scheduled': {'calories': 0}, 'consumed': {'calories': 0}}
    for meal in all_meals:
        if meal.recipe and meal.recipe.calories:
            day_str = meal.meal_date.strftime('%Y-%m-%d')
            calories = meal.recipe.calories
            if day_str in daily_stats:
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
                           prev_week_start=start_of_week - timedelta(7),
                           next_week_start=start_of_week + timedelta(7),
                           daily_stats=daily_stats,
                           weekly_stats=weekly_stats,
                           today=today)

@main.route('/shopping-list', methods=['GET', 'POST'])
@login_required
def shopping_list():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add_manual_item':
            item = ShoppingListItem(household_id=current_user.household_id, name=request.form['name'], category=request.form['category'])
            db.session.add(item)
            flash(f'"{item.name}" added.', 'success')
        elif action == 'delete_manual_item':
            item = db.session.get(ShoppingListItem, int(request.form.get('item_id')))
            if item and item.household_id == current_user.household_id:
                flash(f'"{item.name}" removed.', 'info')
                db.session.delete(item)
        db.session.commit()
        return redirect(url_for('main.shopping_list'))

    end_date = date.today() + timedelta(days=6)
    planned_meals = MealPlan.query.filter(MealPlan.household_id == current_user.household_id, MealPlan.recipe_id.isnot(None), MealPlan.meal_date.between(date.today(), end_date)).all()
    required = {}
    for meal in planned_meals:
        for item in meal.recipe.ingredients:
            if not item.quantity: continue
            key = (item.ingredient.id, item.ingredient.name, item.ingredient.category)
            required.setdefault(key, {'quantity': 0, 'units': set()})['quantity'] += item.quantity
            required[key]['units'].add(item.unit)

    pantry_stock = {item.ingredient_id: item for item in PantryItem.query.filter_by(household_id=current_user.household_id).all()}
    to_buy, in_pantry = {}, {}
    for (ing_id, name, cat), details in required.items():
        needed_qty_val = details['quantity']
        recipe_unit_str = sanitize_unit(next(iter(details['units'])) if details['units'] else '')
        buy_details = {'quantity': needed_qty_val, 'units': details['units'], 'note': "Not in pantry", 'category': cat or 'Other'}
        should_buy = True
        if ing_id in pantry_stock:
            pantry_item = pantry_stock[ing_id]
            in_pantry[name] = pantry_item
            try:
                required_qty = needed_qty_val * ureg(recipe_unit_str)
                pantry_qty = pantry_item.quantity * ureg(sanitize_unit(pantry_item.unit))
                if required_qty.is_compatible_with(pantry_qty):
                    if pantry_qty.to(required_qty.units) >= required_qty:
                        should_buy = False
                    else:
                        amount_to_buy = required_qty - pantry_qty.to(required_qty.units)
                        buy_details = {'quantity': amount_to_buy.magnitude, 'units': {str(amount_to_buy.units)}, 'note': None, 'category': cat or 'Other'}
            except (pint.errors.DimensionalityError, pint.errors.UndefinedUnitError):
                buy_details['note'] = f"Unit Mismatch! Check pantry: you have {pantry_item.quantity} {pantry_item.unit or ''}"
        if should_buy: to_buy[name] = buy_details
    
    grouped_list = {}
    for name, details in to_buy.items():
        grouped_list.setdefault(details.get('category', 'Other'), {})[name] = details
    for item in ShoppingListItem.query.filter_by(household_id=current_user.household_id).all():
        grouped_list.setdefault(item.category, {})[item.name] = {'quantity': None, 'units': [], 'note': 'Manually added', 'manual_id': item.id}
    
    stores = GroceryStore.query.filter_by(household_id=current_user.household_id).order_by(GroceryStore.name).all()
    return render_template('shopping_list.html', page_class='page-shopping-list', grouped_list=grouped_list, ingredients_in_pantry=in_pantry, stores=stores)

@main.route('/export/recipes')
@login_required
def export_recipes():
    recipes = Recipe.query.filter_by(household_id=current_user.household_id).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['id', 'name', 'instructions', 'servings', 'prep_time', 'cook_time', 'meal_type', 'is_favorite', 'rating', 'author_email'])
    for r in recipes:
        writer.writerow([r.id, r.name, r.instructions, r.servings, r.prep_time, r.cook_time, r.meal_type, r.is_favorite, r.rating, r.author.email])
    output.seek(0)
    return Response(output, mimetype="text/csv", headers={"Content-Disposition":"attachment;filename=recipes.csv"})

@main.route('/export/recipe_ingredients')
@login_required
def export_recipe_ingredients():
    recipe_ids = [r.id for r in current_user.household.recipes]
    recipe_ingredients = RecipeIngredient.query.filter(RecipeIngredient.recipe_id.in_(recipe_ids)).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['recipe_id', 'ingredient_name', 'quantity', 'unit'])
    for ri in recipe_ingredients:
        writer.writerow([ri.recipe_id, ri.ingredient.name, ri.quantity, ri.unit])
    output.seek(0)
    return Response(output, mimetype="text/csv", headers={"Content-Disposition":"attachment;filename=recipe_ingredients.csv"})