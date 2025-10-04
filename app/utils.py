import os
import pint
import smtplib
import logging # Import the logging library
from email.message import EmailMessage
from flask import flash, url_for, current_app
from . import db, s
from .models import Achievement, UserAchievement, PantryItem, Ingredient

# --- Achievement Utilities ---
def award_achievement(user, achievement_name):
    """Awards an achievement to a user if they don't already have it."""
    achievement = db.session.query(Achievement).filter_by(name=achievement_name).first()
    if not achievement:
        print(f"WARN: Achievement '{achievement_name}' not found in database.")
        return

    exists = db.session.query(UserAchievement).filter_by(user_id=user.id, achievement_id=achievement.id).first()
    
    if not exists:
        new_unlock = UserAchievement(user_id=user.id, achievement_id=achievement.id)
        db.session.add(new_unlock)
        db.session.commit()
        flash(f'🏆 Achievement Unlocked: {achievement.name}! - {achievement.description}', 'success')

# --- Credit Utilities ---
def deduct_ai_credit(user):
    """Deducts one AI credit from a user if they are not on the elite plan."""
    if user.subscription_plan != 'elite' and user.ai_credits > 0:
        user.ai_credits -= 1

# --- Data Conversion Utilities ---
def convert_quantity_to_float(quantity_str):
    """Converts a string quantity (including fractions) to a float."""
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

# --- Unit Conversion (Pint) Setup ---
# The faulty density conversion feature has been completely removed.
ureg = pint.UnitRegistry()
ureg.load_definitions('app/unit_definitions.txt')

def sanitize_unit(unit_str):
    """Sanitizes and maps common cooking units to Pint-compatible units."""
    if not unit_str: return "dimensionless"
    unit_str = unit_str.lower().strip().rstrip('s') # Strip plurals automatically
    
    unit_map = {
        'oz': 'fluid_ounce', 'ounce': 'fluid_ounce',
        'lb': 'pound',
        'cup': 'cup',
        'tsp': 'teaspoon', 'teaspoon': 'teaspoon',
        'tbsp': 'tablespoon', 'tablespoon': 'tablespoon',
        'g': 'gram', 'gram': 'gram',
        'kg': 'kilogram',
        'ml': 'milliliter',
        'stick': 'stick_of_butter',
        'slice': 'slice',
        'each': 'each',
        'clove': 'clove',
        'head': 'head',
        'sprig': 'sprig',
        'bunch': 'bunch',
        'stalk': 'stalk',
        'ear': 'ear',
        'fillet': 'fillet',
        'leaf': 'leaf',
        'piece': 'piece',
        'pat': 'pat',
        'link': 'link',
        'strip': 'strip',
        'sheet': 'sheet',
    }
    return unit_map.get(unit_str, unit_str)


def consume_ingredients_from_recipe(user, recipe):
    updated, skipped = [], []
    all_pantry_items = PantryItem.query.filter_by(household_id=user.household_id).all()

    for req_ing in recipe.ingredients:
        if not req_ing.quantity or req_ing.quantity <= 0:
            continue

        pantry_item = None
        for item in all_pantry_items:
            if item.ingredient_id == req_ing.ingredient_id:
                pantry_item = item
                break
        
        if not pantry_item:
            search_term = req_ing.ingredient.name
            substitutes = [
                item for item in all_pantry_items 
                if search_term.lower() in item.ingredient.name.lower()
            ]
            if len(substitutes) == 1:
                pantry_item = substitutes[0]
            elif len(substitutes) > 1:
                sub_names = ", ".join([s.ingredient.name for s in substitutes])
                skipped.append(f"{search_term} (Multiple substitutes found: {sub_names})")
                continue

        if not pantry_item:
            continue
        
        try:
            recipe_qty = req_ing.quantity * ureg(sanitize_unit(req_ing.unit))
            pantry_qty = pantry_item.quantity * ureg(sanitize_unit(pantry_item.unit))
            
            if not recipe_qty.is_compatible_with(pantry_qty):
                raise pint.errors.DimensionalityError(pantry_qty.units, recipe_qty.units)

            new_pantry_qty = pantry_qty - recipe_qty.to(pantry_qty.units)
            
            pantry_item.quantity = max(0, new_pantry_qty.to(pantry_qty.units).magnitude)
            updated.append(pantry_item.ingredient.name)

        except pint.errors.DimensionalityError as e:
            skipped.append(f"{pantry_item.ingredient.name} (Cannot convert pantry unit '{e.units1}' to recipe unit '{e.units2}')")
        except pint.errors.UndefinedUnitError as e:
            skipped.append(f"{pantry_item.ingredient.name} (The unit '{e.unit_name}' is not recognized)")
        except Exception as e:
            # THIS IS THE FIX FOR LOGGING
            logging.error(f"An unexpected error occurred during pantry deduction for item '{pantry_item.ingredient.name}': {e}", exc_info=True)
            skipped.append(f"{pantry_item.ingredient.name} (An unexpected error occurred: {repr(e)})")
    
    return updated, skipped


# --- Email Utilities ---
def send_reset_email(user_email):
    token = s.dumps(user_email, salt='password-reset-salt')
    msg = EmailMessage()
    msg['Subject'] = 'Password Reset Request for Meal Engine'
    msg['From'] = os.getenv('MAIL_USERNAME')
    msg['To'] = user_email
    
    reset_url = url_for('auth.reset_password', token=token, _external=True)
    
    msg.set_content(
        f"Hello,\n\nA password reset has been requested for your Meal Engine account.\n"
        f"Please click the link below to reset your password. This link is valid for 30 minutes.\n\n"
        f"{reset_url}\n\n"
        f"If you did not request this, please ignore this email.\n\n"
        f"Thanks,\nThe Meal Engine Team"
    )

    try:
        with smtplib.SMTP(os.getenv('MAIL_SERVER'), int(os.getenv('MAIL_PORT'))) as server:
            if os.getenv('MAIL_USE_TLS', 'false').lower() == 'true':
                server.starttls()
            server.login(os.getenv('MAIL_USERNAME'), os.getenv('MAIL_PASSWORD'))
            server.send_message(msg)
        return True
    except Exception as e:
        current_app.logger.error(f"Failed to send password reset email: {e}")
        return False