"""Shared Flask extensions for SKVM PANEL (single instances, no app import)."""
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_socketio import SocketIO
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message = "Please sign in to continue."
login_manager.login_message_category = "warning"
socketio = SocketIO(cors_allowed_origins=[], async_mode="eventlet", manage_session=True)
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address, default_limits=["240 per minute"],
                  storage_uri="memory://")
