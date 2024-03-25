import datetime
import json
import uuid

from flask import (
    Blueprint, render_template, request, make_response, session, 
    abort, current_app, url_for, redirect)
from sqlalchemy import or_, func
from sqlalchemy.exc import IntegrityError
from webauthn.helpers.exceptions import InvalidRegistrationResponse, InvalidAuthenticationResponse
from webauthn.helpers.structs import (
    AuthenticationCredential, PublicKeyCredentialCreationOptions)
from webauthn.helpers import (
    base64url_to_bytes, bytes_to_base64url, parse_registration_credential_json,
    parse_authentication_credential_json)

from models import db, User
from auth import security


auth = Blueprint("auth", __name__, template_folder="templates")


@auth.route("/register")
def register():
    return render_template("auth/register.html")


@auth.route("/create-user", methods=["POST"])
def create_user():
    """Handle creation of new users from the user creation form."""
    name = request.form.get("name")
    username = request.form.get("username")
    email = request.form.get("email")

    user = User(name=name, username=username, email=email)
    # Temporary naive error handling
    try:
        db.session.add(user)
        db.session.commit()
    except IntegrityError:
        return render_template(
            "auth/_partials/user_creation_form.html",
            error="That username or email address is already in use. "
            "Please enter a different one.",
        )
    
    pcco_json = security.prepare_credential_creation(user)

    res = make_response(
        render_template(
            "auth/_partials/register_credential.html",
            public_credential_creation_options=pcco_json,
        )
    )
    session['registration_user_uid'] = user.uid

    return res

@auth.route("/add-credential", methods=["POST"])
def add_credential():
    """Receive a newly registered credentials to validate and save."""
    user_uid = session.get("registration_user_uid")
    if not user_uid:
        abort(make_response("Error user not found", 400))

    registration_data = request.get_data()

    ## Use py_webauthn to parse the registration data
    registration_json_data = json.loads(registration_data)
    registration_credential = parse_registration_credential_json(registration_json_data)

    user = User.query.filter_by(uid=user_uid).first()

    try:
        security.verify_and_save_credential(user, registration_credential)

        session["registration_user_uid"] = None
        res = make_response('{"verified": true}', 201)
        res.set_cookie(
            "user_uid",
            user.uid,
            httponly=True,
            secure=True,
            samesite="strict",
            max_age=datetime.timedelta(days=30), #TODO: change to less
        )
        
        current_app.logger.info('success?') #, flush=True)

        return res
    except InvalidRegistrationResponse as e:
        current_app.logger.error('Invalid registration response: ' + str(e))
        abort(make_response('{"verified": false}', 400))


@auth.route("/login", methods=["GET"])
def login():
    """Prepare to login user with passwordless auth"""
    user_uid = request.cookies.get("user_uid")
    user = User.query.filter_by(uid=user_uid).first()

    # If not remembered, we render login page w/o username
    if not user:
        return render_template("auth/login.html", username=None, auth_options=None)
    
    # If remembered we prepare the with username and options
    auth_options = security.prepare_login_with_credential(user)
    session['login_user_uid'] = user.uid

    return render_template("auth/login.html", username=user.username, auth_options=auth_options)


@auth.route("/prepare-login", methods=["POST"])
def prepare_login():
    """Prepare login options for a user based on their username or email"""
    username_or_email = request.form.get("username_email", "").lower()
    # The lower function just does case insensitivity for us.
    user = User.query.filter(
        or_(
            func.lower(User.username) == username_or_email,
            func.lower(User.email) == username_or_email,
        )
    ).first()

    # If no user matches, send back the form with an error message
    if not user:
        return render_template(
            "auth/_partials/username_form.html", error="No matching user found"
        )

    auth_options = security.prepare_login_with_credential(user)

    res = make_response(
        render_template(
            "auth/_partials/select_login.html",
            auth_options=auth_options,
            username=user.username,
        )
    )

    # Set the user uid on the session to get when we are authenticating later.
    session["login_user_uid"] = user.uid
    res.set_cookie(
        "user_uid",
        user.uid,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=datetime.timedelta(days=30),
    )
    return res


@auth.route("/login-switch-user")
def login_switch_user():
    """Remove a remembered user and show the username form again."""
    session["login_user_uid"] = None
    res = make_response(redirect(url_for('auth.login')))
    res.delete_cookie('user_uid')
    return res


@auth.route("/verify-login-credential", methods=["POST"])
def verify_login_credential():
    """Remove a remembered user and show the username form again."""
    user_uid = session.get("login_user_uid")
    user = User.query.filter_by(uid=user_uid).first()
    if not user:
        abort(make_response('{"verified": false}', 400))
    
    authetication_data = request.get_data()
    # current_app.logger.info(f'authetication_data(type: {type(authetication_data)}): ' + str(authetication_data))
    authetication_json_data = json.loads(authetication_data)

    authentication_credential = parse_authentication_credential_json(authetication_json_data)
    try:
        security.verify_authentication_credential(user, authentication_credential)
        return make_response('{"verified": true}')
    except InvalidAuthenticationResponse as e:
        current_app.logger.error('Invalid authentication response: ' + str(e))
        abort(make_response('{"verified": false}', 400))