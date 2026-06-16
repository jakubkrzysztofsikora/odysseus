# Implementation Plan: Add User Authentication

## Overview
Add basic user authentication to a simple Flask web application. This includes user model, login/logout endpoints, and session management.

## Preconditions
- Current branch: main
- Codebase: Simple Flask app in `app.py` with basic routes
- Dependencies: Flask, Flask-SQLAlchemy, Flask-Login installed
- No existing authentication system

## Phases

### Phase 1: Add User Model and Database Setup
**Description**: Create User model and initialize database

**File Changes**:
1. `app.py`:
   - Add imports: `from flask_sqlalchemy import SQLAlchemy`, `from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user`
   - Add SQLAlchemy and LoginManager initialization after app creation
   - Add User model class with id, username, password fields
   - Add user_loader callback for Flask-Login

2. `requirements.txt`:
   - Add: `Flask-SQLAlchemy==3.0.5`
   - Add: `Flask-Login==0.6.2`

**Success Criteria**:
- Automated: `python -c "from app import db, User; print('Imports successful')"` runs without errors
- Manual: User model has username and password fields

**Dependencies**: None (first phase)

---

### Phase 2: Add Authentication Routes
**Description**: Add login, logout, and register routes

**File Changes**:
1. `app.py`:
   - Add `/register` route (GET and POST)
   - Add `/login` route (GET and POST)
   - Add `/logout` route
   - Add password hashing using werkzeug.security

**Success Criteria**:
- Automated: `python -c "from app import app; print([r.rule for r in app.url_map.iter_rules()])"` shows /register, /login, /logout
- Manual: Routes are accessible in browser

**Dependencies**: Phase 1

---

### Phase 3: Add Protected Route and Navigation
**Description**: Add a protected dashboard route and update navigation

**File Changes**:
1. `app.py`:
   - Add `/dashboard` route with `@login_required` decorator
   - Add template rendering for dashboard

2. `templates/base.html` (new file):
   - Create base template with navigation links
   - Show login/logout links based on authentication state

3. `templates/dashboard.html` (new file):
   - Create dashboard template showing user info

**Success Criteria**:
- Automated: `python -c "from app import app; print('/dashboard' in [r.rule for r in app.url_map.iter_rules()])"` returns True
- Manual: Dashboard accessible only when logged in

**Dependencies**: Phase 2

---

## Rollback Plan
If any phase fails, revert to the previous git commit and investigate the issue.

## Estimated Time
- Phase 1: 15 minutes
- Phase 2: 20 minutes
- Phase 3: 15 minutes
