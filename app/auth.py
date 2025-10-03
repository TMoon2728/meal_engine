from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from . import db, bcrypt, s
from .models import User, Household, Recipe
from .utils import send_reset_email, award_achievement
from . import PLAN_CREDITS

auth = Blueprint('auth', __name__)

@auth.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first()
        if user and bcrypt.check_password_hash(user.password, password):
            login_user(user, remember=True)
            next_page = request.args.get('next')
            return redirect(next_page) if next_page else redirect(url_for('main.index'))
        else:
            flash('Login Unsuccessful. Please check email and password', 'danger')
    return render_template('login.html')

@auth.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        if User.query.filter_by(email=email).first():
            flash('Email address already in use.', 'warning')
            return redirect(url_for('auth.signup'))
        
        new_household = Household(name=f"{email.split('@')[0]}'s Household")
        db.session.add(new_household)
        db.session.flush()

        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')
        user = User(
            email=email,
            password=hashed_password,
            household_id=new_household.id,
            ai_credits=PLAN_CREDITS.get('free', 5)
        )
        db.session.add(user)
        db.session.flush()

        hint_instructions = (
            "Welcome to Meal Engine! This is your very first recipe to get you started.\n\n"
            "By the way, there are secrets hidden within this app for those with a taste for adventure.\n\n"
            "To unlock true culinary magic, follow the legendary kitchen command:\n"
            "Up, Up, Down, Down, Left, Right, Left, Right, Bake, Add."
        )

        first_recipe = Recipe(
            name="Your First Recipe! (Read Me)",
            instructions=hint_instructions,
            meal_type="Snack",
            author=user,
            household_id=new_household.id
        )
        db.session.add(first_recipe)
        db.session.commit()
        
        award_achievement(user, 'First Steps')

        flash('Your account has been created! You can now log in.', 'success')
        return redirect(url_for('auth.login'))
    return render_template('signup.html')

@auth.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('main.index'))

@auth.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    if request.method == 'POST':
        email = request.form.get('email')
        user = User.query.filter_by(email=email).first()
        if user:
            if send_reset_email(email):
                flash('A password reset link has been sent to your email.', 'info')
            else:
                flash('There was an error sending the email. Please try again later.', 'danger')
        else:
            # Show the same message to prevent user enumeration
            flash('A password reset link has been sent to your email.', 'info')
        return redirect(url_for('auth.login'))
    return render_template('forgot_password.html')

@auth.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    try:
        email = s.loads(token, salt='password-reset-salt', max_age=1800)
    except Exception:
        flash('The password reset link is invalid or has expired.', 'warning')
        return redirect(url_for('auth.forgot_password'))

    user = User.query.filter_by(email=email).first()
    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('auth.login'))

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
        return redirect(url_for('auth.login'))

    return render_template('reset_password.html')