from . import db, PLAN_CREDITS
from flask_login import UserMixin
from datetime import datetime
from sqlalchemy import UniqueConstraint

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
    ai_credits = db.Column(db.Integer, nullable=False, default=PLAN_CREDITS['free'])

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
    
    is_container = db.Column(db.Boolean, default=False, nullable=False, server_default='false')
    consumable_unit = db.Column(db.String(50), nullable=True)
    container_prompt = db.Column(db.String(255), nullable=True)

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

class Achievement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.String(255), nullable=False)
    icon = db.Column(db.String(50), nullable=False, default='fa-question-circle')

class UserAchievement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    achievement_id = db.Column(db.Integer, db.ForeignKey('achievement.id'), nullable=False)
    unlocked_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    user = db.relationship('User', backref=db.backref('achievements', cascade="all, delete-orphan"))
    achievement = db.relationship('Achievement')
    
    __table_args__ = (UniqueConstraint('user_id', 'achievement_id', name='_user_achievement_uc'),)