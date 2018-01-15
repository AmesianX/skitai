'''# --------------------------------------------------------------------------
Events

def decorate (app):
    @app.on ("User:added")
    def user_added (was, user, form): pass    
    
    @app.on ("User:updated")
    def user_updated (was, user, form): pass
    
     @app.on ("User:password:updated")
    def password_updated (was, user): pass
    
    @app.on ("User:password:reset-requested")
    def password_reset_requested (was, username, form): pass
# --------------------------------------------------------------------------'''

import re
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q

# Validators --------------------------------------------

RX_EMAIL = re.compile ('(([^<>()\[\]\\.,;:\s@"]+(\.[^<>()\[\]\\.,;:\s@"]+)*)|(".+"))@((\[[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}])|(([a-zA-Z\-0-9]+\.)+[a-zA-Z]{2,}))$')
def is_vaild_email (string):
    return RX_EMAIL.match (string)

def is_valid_length (string):
    return (4 < len (string) < 20)        

def is_valid_password (password):
    if not is_valid_length (password):
        return "Invalid password, required 4 - 20 characters"
    try:
        validate_password (password)
    except django.core.exceptions.ValidationError as why:
        return why.messages

def push_error_messages (was, msgs):
    if not msgs:
        return
    if isinstance (msgs, str):
        msgs = [msgs]
    for msg in msgs:
        was.mbox.push (msg, "error", icon = "new_releases")            

def update_password (was, username, password, template):
    errors = is_valid_password (password)
    if errors:
        push_error_messages (was, errors)
        return was.render (template)    
    user = User.objects.get (username = username)
    user.set_password (password)
    user.save ()    
    was.django.update_session_auth_hash (user)    
    was.app.emit ("User:password:updated", user)


# App Decorator ---------------------------------------------------------
        
def decorate (app):    
    # Handlers ---------------------------------------------------------        
    @app.login_handler
    def login_handler (was):
        if was.django.user.is_authenticated ():
            was.request.user = was.django.user
            was.django.session.set_expiry (300) #extend timeout
            return
        
        if was.request.args.get ("_csrf_token"):
            if not was.csrf_verify ():
                return was.response ("400 Bad Request")
        
        next_url = not was.request.uri.endswith ("signout") and was.request.uri or ""
        return was.redirect (was.ab ("signin", next_url))
    
    @app.staff_member_check_handler
    def staff_member_check_handler (was):
        if not was.django.user.is_staff:
            return was.response ("403 Staff Permission Required")

    @app.permission_check_handler
    def permission_check_handler (was, perms):
        if isinstance (perms, str):
            perms = (prems,)
        if not was.django.user.has_perms (perms):            
            return was.response ("403 Permission Denied")    
    
    # Views ---------------------------------------------------------        
    @app.route ("/regist/signout")
    def signout (was):
        was.django.logout ()
        was.mbox.push ("Signed out successfully", "success")    
        return was.redirect (was.ab ('index'))
        
    @app.route ("/regist/signin")
    def signin (was, next_url = None, **form):
        if was.django.user.is_authenticated ():    
            return was.redirect (next_url)
    
        if form.get ("username"):
            user = was.django.authenticate (
                form ["username"], 
                form ["password"]
            )
            if user is not None:
                was.django.login (user)                
                return was.redirect (next_url or was.ab ('index'))
            else:
                was.mbox.push ("Invalid user name or password", "error", icon = "new_releases")
        return was.render ("regist/signin.html", next_url = next_url or was.ab ("index"))

    @app.route ("/regist/signup")
    def signup (was, next_url = None, **form):
        def show_form (msg = None):
            push_error_messages (was, msg)
            return was.render ("regist/signup.html", next_url = next_url or was.ab ("index"), form = form)
        
        if was.django.user.is_authenticated ():    
            return was.redirect (next_url)
        
        if "username" not in form:
            return show_form ()
        
        # username ---------------------------------------------
        username = form.get ("username", "")
        if not is_valid_length (username):
            return show_form ("Invalid user ID, required 4 - 20 characters")
        try: user = User.objects.get (username = username)
        except ObjectDoesNotExist: pass
        else: return show_form ("User ID already exists, try anohter ID. please")
        
        # password  ---------------------------------------------
        errors = is_valid_password (form.get ("password"))
        if errors:
            return show_form (errors)
        
        # email  -------------------------------------------------
        if "email" in form and not is_vaild_email (form ["email"]):
            return show_form ("Invalid email address")
        if User.objects.filter (email__iexact = form ["email"]).count():        
            return show_form ("Email already exists, If you want recover your account <a href='{}'>click here</a>".format (was.ab ('forgot_password')))         
        
        user = User (username = form ["username"], email = form ["email"])
        user.set_password (form ["password"])
        user.save ()
        was.app.emit ('User:added', user, form)
        
        was.mbox.push ("Sign up success, thank you", "info")
        return signin (was, next_url, **form)
    
    @app.route ("/regist/account")
    @app.login_required
    def account (was, **form):
        if "email" not in form:
            return was.render ("regist/account.html", form = form)
        
        # email  -------------------------------------------------
        if "email" in form and not is_vaild_email (form ["email"]):
            was.mbox.push ("Invalid email", "error", icon = "new_releases")
            return was.render ("regist/account.html", form = form)
        if User.objects.filter (~Q (username__iexact = was.django.user.username), email__iexact = form ["email"]).count():        
            was.mbox.push ("Email already exists with another username", "error", icon = "new_releases")
            return was.render ("regist/account.html", form = form)

        user = User.objects.get (username = was.django.user.username)
        user.email = form ["email"]
        user.save ()
        was.app.emit ('User:updated', user, form)
        
        was.mbox.push ("Account updated successfully", "info")
        return was.redirect (was.ab ("index"))
    
    @app.route ("/regist/forgot-password")
    def forgot_password (was, **form):    
        if form.get ("email"):
            try:
                user = User.objects.filter (email = form ["email"]).order_by ("-id")[0]
            except IndexError:    
                was.mbox.push ("Email does not exists, check your email please", "error", icon = "new_releases")
                return was.render ('regist/forgot-password.html')
            was.app.emit ("User:password:reset-requested", user.username, form)
            was.mbox.push ("Email has been sent. check your email, please", "info")
            return was.redirect (was.ab ('index'))
        return was.render ('regist/forgot-password.html')

    @app.route ("/regist/reset-password")
    def reset_password (was, t, **form):
        if not t:
            # need token
            return was.response ("400 Bad Request")
        
        username = was.detoken (t)
        if not username:
            was.mbox.push ("Your token invalid or had been expired, request agian please", "error", icon = "new_releases")
            return was.render ('regist/forgot-password.html', username = username)
        
        if not form.get ("password"):
            return was.render ('regist/reset-password.html', username = username)
        
        nextform = update_password (was, username, form ["password"], 'regist/reset-password.html')
        if nextform:
            return nextform
        
        was.rmtoken (t)
        was.mbox.push ("Your password has been reset and changed", "info")
        return signin (was, was.ab ('index'), **{"username": username, "password":  form ["password"]})
        
    @app.route ("/regist/change-password")
    @app.login_required
    def change_password (was, **form):    
        if not form.get ("password"):
            return was.render ('regist/change-password.html')
        
        username = was.django.user.username
        nextform = update_password (was, username, form ["password"], 'regist/change-password.html')
        if nextform:
            return nextform
    
        was.mbox.push ("Your password has been changed", "info")
        return was.redirect (was.ab ('index'))
    