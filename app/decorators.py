from functools import wraps
from flask import request, jsonify, flash, redirect, url_for
from flask_login import current_user

def require_ai_credits(f):
    """
    A decorator to verify that a user has enough AI credits to perform an action.
    Redirects to the pricing page or returns a JSON error if credits are insufficient.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Allow users on the 'elite' plan to bypass the credit check
        if current_user.subscription_plan == 'elite':
            return f(*args, **kwargs)

        # Check if the user has credits remaining
        if current_user.ai_credits <= 0:
            # Handle API requests with a JSON response
            if request.path.startswith('/api/'):
                return jsonify({
                    'error': 'You have run out of AI credits for this month.',
                    'redirect_url': url_for('payments.pricing')
                }), 403
            # Handle regular web page requests with a flash message and redirect
            else:
                flash('You have run out of AI credits. Please upgrade your plan to continue.', 'warning')
                return redirect(url_for('payments.pricing'))
        
        # If the user has credits, proceed with the original function
        return f(*args, **kwargs)
    return decorated_function