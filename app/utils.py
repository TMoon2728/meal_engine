import os
import pint
import smtplib
from email.message import EmailMessage
from flask import flash, url_for, current_app
from . import db, s
from .models import Achievement, UserAchievement, PantryItem

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
ureg = pint.UnitRegistry()
ureg.load_definitions('app/unit_definitions.txt')

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
    """Sanitizes and maps common cooking units to Pint-compatible units."""
    if not unit_str: return "dimensionless"
    unit_str = unit_str.lower().strip().rstrip('s') # Strip plurals automatically
    
    unit_map = {
        # Standard Volume/Weight
        'oz': 'fluid_ounce', 'ounce': 'fluid_ounce',
        'lb': 'pound',
        'cup': 'cup',
        'tsp': 'teaspoon', 'teaspoon': 'teaspoon',
        'tbsp': 'tablespoon', 'tablespoon': 'tablespoon',
        'g': 'gram', 'gram': 'gram',
        'kg': 'kilogram',
        'ml': 'milliliter',
        
        # Custom Mapped Units
        'stick': 'stick_of_butter',
        
        # Countable Dimensionless Units
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
    # Return the mapped unit, or the original string if no mapping is found
    return unit_map.get(unit_str, unit_str)


def consume_ingredients_from_recipe(user, recipe):
    """
    Deducts a recipe's ingredients from a user's household pantry.
    Returns two lists: updated items and skipped items.
    """
    pantry_items = {item.ingredient_id: item for item in user.household.pantry_items}
    updated, skipped = [], []
    for req_ing in recipe.ingredients:
        if not req_ing.quantity or req_ing.ingredient_id not in pantry_items:
            continue
        pantry_item = pantry_items[req_ing.ingredient_id]
        with ureg.context('cooking', substance=req_ing.ingredient.name.lower().replace(" ", "_")):
            try:
                recipe_qty = req_ing.quantity * ureg(sanitize_unit(req_ing.unit))
                pantry_qty = pantry_item.quantity * ureg(sanitize_unit(pantry_item.unit))
                
                if not recipe_qty.is_compatible_with(pantry_qty):
                    raise pint.errors.DimensionalityError(recipe_qty.units, pantry_qty.units, "Units are not compatible")

                new_pantry_qty = pantry_qty.to(recipe_qty.units) - recipe_qty
                
                pantry_item.quantity = max(0, new_pantry_qty.to(pantry_qty.units).magnitude)
                updated.append(req_ing.ingredient.name)
            except Exception as e:
                skipped.append(f"{req_ing.ingredient.name} (Error: '{e}')")
    
    return updated, skipped


# --- Email Utilities ---
def send_reset_email(user_email):
    """Generates a password reset token and sends the email."""
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