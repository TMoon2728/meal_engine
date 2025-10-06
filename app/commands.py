import click
from flask.cli import with_appcontext
from . import db
from .models import Achievement, Ingredient, RecipeIngredient, PantryItem

@click.command('init-achievements')
@with_appcontext
def init_achievements_command():
    """Initializes the database with all available achievements."""
    
    achievements_to_add = [
        {'name': 'First Steps', 'description': 'You created your account!', 'icon': 'fa-shoe-prints'},
        {'name': 'The Creator', 'description': 'You added your very first recipe.', 'icon': 'fa-pencil-alt'},
        {'name': 'AI Assistant', 'description': 'You generated your first recipe with AI.', 'icon': 'fa-magic'},
        {'name': 'Web Scraper', 'description': 'You imported your first recipe from the web.', 'icon': 'fa-link'},
        {'name': 'Weekly Planner', 'description': 'You saved your first weekly meal plan.', 'icon': 'fa-calendar-check'},
        {'name': 'Pantry Organizer', 'description': 'You added your first item to the pantry.', 'icon': 'fa-box-open'},
        {'name': 'Top Chef', 'description': 'You rated a recipe a full 5 stars.', 'icon': 'fa-star'},
        {'name': 'AI Architect', 'description': 'You generated your first meal plan with the AI Architect.', 'icon': 'fa-robot'},
        {'name': 'Quantum Chef', 'description': 'You discovered a strange new form of matter.', 'icon': 'fa-atom'}
    ]
    
    existing_achievements = {ach.name for ach in Achievement.query.all()}
    
    new_achievements_added = 0
    for ach_data in achievements_to_add:
        if ach_data['name'] not in existing_achievements:
            db.session.add(Achievement(**ach_data))
            new_achievements_added += 1
            
    if new_achievements_added > 0:
        db.session.commit()
        click.echo(f"Successfully added {new_achievements_added} new achievements to the database.")
    else:
        click.echo("Achievements are already up-to-date.")

@click.command('nuke-ingredients')
@with_appcontext
def nuke_ingredients_command():
    """
    Deletes ALL ingredients, pantry items, and recipe-ingredient links.
    This is a destructive operation for a complete reset.
    """
    if click.confirm('Are you ABSOLUTELY SURE you want to delete ALL master ingredients, pantry items, and recipe-ingredient links? This cannot be undone.'):
        try:
            # Delete in the correct order to respect foreign key constraints
            num_recipe_links = db.session.query(RecipeIngredient).delete()
            num_pantry_items = db.session.query(PantryItem).delete()
            num_ingredients = db.session.query(Ingredient).delete()
            
            db.session.commit()
            
            click.echo(f"Success! Deleted:")
            click.echo(f"- {num_ingredients} master ingredients")
            click.echo(f"- {num_pantry_items} pantry items")
            click.echo(f"- {num_recipe_links} recipe-ingredient links")
            click.echo("Your ingredient database is now empty.")
        except Exception as e:
            db.session.rollback()
            click.echo(f"An error occurred: {e}")
    else:
        click.echo("Operation cancelled.")