import stripe
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_required, current_user
from . import db
from .models import User

payments = Blueprint('payments', __name__)

def _update_user_subscription(user, subscription):
    """Helper function to update a user's subscription details in the database."""
    price_id = subscription['items']['data'][0]['price']['id']
    
    stripe_price_ids = current_app.config['STRIPE_PRICE_IDS']
    plan_credits = current_app.config['PLAN_CREDITS']
    
    new_plan = 'free' # Default plan
    if price_id == stripe_price_ids.get('premium'):
        new_plan = 'premium'
    elif price_id == stripe_price_ids.get('elite'):
        new_plan = 'elite'
    else:
        current_app.logger.warning(f"Price ID '{price_id}' does not match any known plan IDs. User '{user.email}' will be set to 'free'.")

    user.subscription_plan = new_plan
    user.stripe_subscription_id = subscription.id
    user.stripe_customer_id = subscription.customer
    user.ai_credits = plan_credits.get(new_plan, 0)
    db.session.commit()
    current_app.logger.info(f"User {user.email} successfully updated to '{new_plan}' plan.")

@payments.route('/pricing')
@login_required
def pricing():
    stripe_price_ids = current_app.config['STRIPE_PRICE_IDS']
    return render_template('pricing.html', stripe_price_ids=stripe_price_ids)

@payments.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    if current_user.stripe_subscription_id:
        flash("You already have an active subscription. Please manage it from your profile.", "info")
        return redirect(url_for('main.profile'))

    price_id = request.form.get('price_id')
    
    try:
        customer_id = current_user.stripe_customer_id
        
        if customer_id:
            try:
                stripe.Customer.retrieve(customer_id)
            except stripe.error.InvalidRequestError:
                current_app.logger.warning(f"Stale Stripe customer ID '{customer_id}' detected for user '{current_user.email}'. Clearing.")
                customer_id = None
                current_user.stripe_customer_id = None
                db.session.commit()

        if not customer_id:
            customer = stripe.Customer.create(
                email=current_user.email,
                name=current_user.email.split('@')[0]
            )
            customer_id = customer.id
            current_user.stripe_customer_id = customer_id
            db.session.commit()
            current_app.logger.info(f"Created new Stripe customer ID: {customer_id} for user '{current_user.email}'")

        checkout_session = stripe.checkout.Session.create(
            customer=customer_id,
            client_reference_id=current_user.id,
            line_items=[{'price': price_id, 'quantity': 1}],
            mode='subscription',
            success_url=url_for('main.index', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=url_for('payments.pricing', _external=True),
            allow_promotion_codes=True,
        )
        return redirect(checkout_session.url, code=303)

    except Exception as e:
        flash(f'Error creating checkout session: {str(e)}', 'danger')
        current_app.logger.error(f"Stripe checkout session creation failed for user '{current_user.email}': {e}")
        return redirect(url_for('payments.pricing'))

@payments.route('/stripe-webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    webhook_secret = current_app.config['STRIPE_WEBHOOK_SECRET']
    
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        current_app.logger.error(f"Invalid webhook signature: {e}")
        return 'Invalid signature', 400

    event_type = event['type']
    
    with current_app.app_context():
        if event_type == 'checkout.session.completed':
            session = event['data']['object']
            user_id = session.get('client_reference_id')
            if user_id:
                user = db.session.get(User, int(user_id))
                if user:
                    subscription = stripe.Subscription.retrieve(session.get('subscription'))
                    _update_user_subscription(user, subscription)
                else:
                    current_app.logger.error(f"Webhook user ID {user_id} not found in database.")
            else:
                current_app.logger.error("Webhook received without client_reference_id.")

        elif event_type in ['customer.subscription.updated', 'customer.subscription.deleted']:
            subscription = event['data']['object']
            user = User.query.filter_by(stripe_subscription_id=subscription.id).first()
            if user:
                if subscription.get('cancel_at_period_end') or event_type == 'customer.subscription.deleted':
                    user.subscription_plan = 'free'
                    user.stripe_subscription_id = None
                    user.ai_credits = current_app.config['PLAN_CREDITS'].get('free', 5)
                    db.session.commit()
                    current_app.logger.info(f"User {user.email}'s subscription cancelled. Downgraded to free.")
                else:
                    _update_user_subscription(user, subscription)

        elif event_type == 'invoice.payment_succeeded':
            invoice = event['data']['object']
            customer_id = invoice.get('customer')
            user = User.query.filter_by(stripe_customer_id=customer_id).first()
            if user and user.subscription_plan in current_app.config['PLAN_CREDITS']:
                user.ai_credits = current_app.config['PLAN_CREDITS'][user.subscription_plan]
                db.session.commit()
                current_app.logger.info(f"AI credits for {user.email} have been reset for the new billing cycle.")

    return 'Success', 200

@payments.route('/create-billing-portal-session', methods=['POST'])
@login_required
def create_billing_portal_session():
    if not current_user.stripe_customer_id or not current_user.stripe_subscription_id:
        flash('No billing information found for your account.', 'warning')
        return redirect(url_for('main.profile'))

    try:
        subscription = stripe.Subscription.retrieve(current_user.stripe_subscription_id)
        price_id = subscription['items']['data'][0]['price']['id']
        
        stripe_price_ids = current_app.config['STRIPE_PRICE_IDS']
        plan_credits = current_app.config['PLAN_CREDITS']
        
        correct_plan = 'free'
        if price_id == stripe_price_ids.get('premium'): correct_plan = 'premium'
        elif price_id == stripe_price_ids.get('elite'): correct_plan = 'elite'
        
        if current_user.subscription_plan != correct_plan:
            current_user.subscription_plan = correct_plan
            current_user.ai_credits = plan_credits.get(correct_plan, 0)
            db.session.commit()
            flash('Your plan information was out of sync and has been corrected.', 'info')
            current_app.logger.info(f"Corrected plan for {current_user.email} to {correct_plan}.")

        return_url = url_for('main.profile', _external=True)
        portal_session = stripe.billing_portal.Session.create(
            customer=current_user.stripe_customer_id,
            return_url=return_url
        )
        return redirect(portal_session.url, code=303)

    except stripe.error.InvalidRequestError as e:
        current_app.logger.warning(f"Stale subscription ID detected for user {current_user.email}. Error: {e}")
        current_user.subscription_plan = 'free'
        current_user.stripe_subscription_id = None
        current_user.ai_credits = current_app.config['PLAN_CREDITS'].get('free', 5)
        db.session.commit()
        flash('Your subscription data was out of sync and has been reset. Please upgrade your plan again.', 'warning')
        return redirect(url_for('payments.pricing'))
    except Exception as e:
        flash(f"An unexpected error occurred: {str(e)}", "danger")
        current_app.logger.error(f"Billing portal session creation failed for '{current_user.email}': {e}")
        return redirect(url_for('main.profile'))