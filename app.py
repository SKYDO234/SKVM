"""SKVM PANEL — VPS hosting & management panel.

SKVM's own control plane (Incus + Docker backends, job worker,
capability detection):
  * Docker backend (Docker SDK) alongside the preserved Incus backend
  * Flask-Login auth, Flask-SQLAlchemy storage (skvm.db), CSRF, rate limits
  * Plans with server-side resource enforcement, admin panel, branding
  * Real-time web console (Flask-SocketIO + xterm.js) into the guest itself
  * tmate SSH sessions started/stopped inside the guest
No Minecraft / game-hosting functionality. No fake statistics: node
figures are labelled as node figures, VPS figures are read from the
guest/container itself, and unavailable backends fail honestly.
"""
from __future__ import annotations
import os
import re
import secrets
import threading
import time
from functools import wraps

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash

from core import backend, db
from core.config import (ALLOW_REGISTRATION, BACKEND, BIND, COOKIE_SECURE,
                         DATA_DIR, DB_PATH, DEFAULT_ADMIN_PASSWORD,
                         DEFAULT_ADMIN_USERNAME, DEFAULT_DISK_GB, DEFAULT_MEMORY_MB,
                         DEFAULT_VCPUS, HOSTNAME, MAX_VPS_PER_USER, PANEL_URL,
                         PORT, SECRET_KEY, TIMEZONE, node_token,
                         trust_proxy_enabled)
from core.crypto import decrypt_text, encrypt_text
from core.extensions import csrf, limiter, login_manager, socketio
from core.models import User

# --------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------

def create_app():
    app = Flask(__name__)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    app.config.update(
        SECRET_KEY=SECRET_KEY,
        SQLALCHEMY_DATABASE_URI=os.getenv("SKVM_DATABASE_URI", f"sqlite:///{DB_PATH}"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"check_same_thread": False, "timeout": 30}},
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=COOKIE_SECURE,
        PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
        MAX_CONTENT_LENGTH=1024 * 1024,
    )
    from core.extensions import db as sqla
    sqla.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)
    socketio.init_app(app, cors_allowed_origins=[], async_mode="eventlet", manage_session=True)
    if trust_proxy_enabled():
        # Behind the Codespaces/Gitpod/reverse-proxy forwarded host, make
        # request.host/is_secure reflect the public URL. Otherwise the
        # browser's public Referer fails Flask-WTF's CSRF host check.
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
    with app.app_context():
        db.init_db()
        # First-run bootstrap must also happen under Gunicorn (no __main__).
        bootstrap_admin()
        db.ensure_local_node(node_token())
    register_routes(app)
    register_console(app)
    return app


@login_manager.user_loader
def load_user(user_id):
    try:
        return db.get_user(int(user_id))
    except Exception:
        return None


def bootstrap_admin():
    """Create the first admin once (requires app context).

    Credentials come from SKVM_ADMIN_USERNAME / SKVM_ADMIN_PASSWORD when
    set; otherwise the documented first-run default admin/admin123 is
    used. The login page flow flags accounts still on that default so
    the operator is prompted to change it (Profile -> Change password).
    """
    if db.all_users():
        return None
    username = (os.getenv("SKVM_ADMIN_USERNAME") or DEFAULT_ADMIN_USERNAME).strip() or DEFAULT_ADMIN_USERNAME
    password = os.getenv("SKVM_ADMIN_PASSWORD") or DEFAULT_ADMIN_PASSWORD
    generated = not os.getenv("SKVM_ADMIN_PASSWORD")
    uid = db.create_user(username, password, "admin", max_vps=999999)
    cred = DATA_DIR / "first_admin_credentials.txt"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cred.write_text(f"SKVM PANEL first admin\nUsername: {username}\nPassword: {password}\nCreated: {db.utcnow()}\n", encoding="utf-8")
    try:
        os.chmod(cred, 0o600)
    except PermissionError:
        pass
    return (username, cred, generated, uid)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user.is_authenticated:
            return redirect(url_for("login", next=request.path))
        if not current_user.is_admin:
            abort(403)
        return fn(*a, **kw)
    return wrapper


def branding():
    s = db.all_settings()
    return {
        "site_name": s.get("site_name") or "SKVM PANEL",
        "tagline": s.get("site_tagline") or "",
        "logo_url": s.get("logo_url") or "",
        "accent": s.get("accent") or "#67e8f9",
        "website_url": s.get("website_url") or "",
        "discord_url": s.get("discord_url") or "",
        "support_email": s.get("support_email") or "",
        "maintenance": (s.get("maintenance_mode") or "false") == "true",
        "allow_registration": (s.get("allow_registration") or "true") == "true" and ALLOW_REGISTRATION,
    }


def own_vps_or_404(vps_id):
    v = db.get_vps(vps_id)
    if not v or (v.user_id != current_user.id and not current_user.is_admin):
        abort(404)
    return v


def validate_resources(vcpus, memory_mb, disk_gb, plan):
    """Server-side plan + sanity enforcement. Returns error string or None."""
    try:
        vcpus, memory_mb, disk_gb = int(vcpus), int(memory_mb), int(disk_gb)
    except (TypeError, ValueError):
        return "CPU, RAM and disk must be whole numbers."
    if not (1 <= vcpus <= 64):
        return "vCPU must be between 1 and 64."
    if not (512 <= memory_mb <= 262144):
        return "RAM must be between 512 MiB and 256 GiB."
    if not (5 <= disk_gb <= 4096):
        return "Disk must be between 5 GiB and 4 TiB."
    if plan is not None:
        if vcpus > plan.max_vcpus:
            return f"Your {plan.name} plan allows at most {plan.max_vcpus} vCPU."
        if memory_mb > plan.max_memory_mb:
            return f"Your {plan.name} plan allows at most {plan.max_memory_mb} MiB RAM."
        if disk_gb > plan.max_disk_gb:
            return f"Your {plan.name} plan allows at most {plan.max_disk_gb} GiB disk."
    return None


def node_capacity_error(vcpus, memory_mb, disk_gb):
    """Block allocations that exceed the node's real physical capacity."""
    from core.capabilities import snapshot
    try:
        snap = snapshot()
    except Exception:
        return None
    used_cpu = sum(v.vcpus for v in db.list_all_vps())
    used_mem = sum(v.memory_mb for v in db.list_all_vps())
    used_disk = sum(v.disk_gb for v in db.list_all_vps())
    if used_cpu + vcpus > (snap.get("cpu_count") or 0):
        return f"Node CPU capacity reached ({used_cpu}/{snap.get('cpu_count')} vCPU allocated)."
    if used_mem + memory_mb > (snap.get("memory_total_mb") or 0):
        return f"Node RAM capacity reached ({used_mem}/{snap.get('memory_total_mb')} MiB allocated)."
    if used_disk + disk_gb > (snap.get("disk_total_gb") or 0):
        return f"Node disk capacity reached ({used_disk}/{snap.get('disk_total_gb')} GiB allocated)."
    return None


def audit(action, target="", detail=""):
    db.audit(current_user if current_user.is_authenticated else None, action, target,
             detail, request.remote_addr or "")


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

def register_routes(app):

    @app.context_processor
    def ctx():
        b = branding() if request.endpoint not in ("static",) else {
            "site_name": "SKVM PANEL", "tagline": "", "logo_url": "", "accent": "#67e8f9",
            "website_url": "", "discord_url": "", "support_email": "",
            "maintenance": False, "allow_registration": True}
        return {"brand": b, "timezone": TIMEZONE, "panel_url": PANEL_URL,
                "current_user": current_user,
                "default_pw_warning": bool(session.get("default_pw_warning"))}

    @app.before_request
    def maintenance_gate():
        if request.endpoint in ("static", "healthz") or request.path.startswith("/socket.io"):
            return None
        try:
            if db.get_setting("maintenance_mode", "false") == "true":
                if not (current_user.is_authenticated and current_user.is_admin):
                    if request.endpoint not in ("login", "logout"):
                        return render_template("maintenance.html", brand=branding()), 503
        except Exception:
            pass
        return None

    # ------------------------------------------------------------ public
    @app.route("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        return render_template("index.html", plans=db.list_plans(only_enabled=True),
                               os_images=db.list_os_images(only_enabled=True))

    @app.route("/register", methods=["GET", "POST"])
    @limiter.limit("6 per minute", methods=["POST"])
    def register():
        if not branding()["allow_registration"]:
            flash("Registration is currently disabled by the administrator.", "warning")
            return redirect(url_for("login"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            email = request.form.get("email", "").strip()
            password = request.form.get("password", "")
            if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                flash("Please enter a valid email address.", "danger")
            else:
                try:
                    plans = db.list_plans(only_enabled=True)
                    plan = plans[0] if plans else None
                    uid = db.create_user(username, password, max_vps=(plan.max_vps if plan else MAX_VPS_PER_USER),
                                         email=email or None, plan_id=(plan.id if plan else None))
                    user = db.get_user(uid)
                    login_user(user)
                    db.audit(user, "auth.register", username, "", request.remote_addr or "")
                    return redirect(url_for("dashboard"))
                except Exception as e:
                    flash(str(e), "danger")
        return render_template("auth.html", mode="register")

    @app.route("/login", methods=["GET", "POST"])
    @limiter.limit("8 per minute", methods=["POST"])
    def login():
        if request.method == "POST":
            user = db.verify_user(request.form.get("username", ""), request.form.get("password", ""))
            if not user or user.suspended:
                flash("Invalid username or password.", "danger")
            else:
                session.clear()
                login_user(user, remember=False)
                session.permanent = True
                session["default_pw_warning"] = bool(
                    user.username == DEFAULT_ADMIN_USERNAME
                    and check_password_hash(user.password_hash, DEFAULT_ADMIN_PASSWORD))
                db.update_user(user.id, last_login_at=db.utcnow())
                db.audit(user, "auth.login", user.username, "", request.remote_addr or "")
                nxt = request.args.get("next") or ""
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
        return render_template("auth.html", mode="login")

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        audit("auth.logout", current_user.username)
        logout_user()
        session.clear()
        return redirect(url_for("index"))

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True, "service": "skvm-panel", "version": "1.0.0",
                        "hostname": db.get_setting("host_name", HOSTNAME)})

    # ------------------------------------------------------------ user
    @app.route("/dashboard")
    @login_required
    def dashboard():
        from core.capabilities import snapshot
        vps = db.list_vps_for_user(current_user.id)
        jobs = db.list_jobs_for_user(current_user.id)
        plan = db.get_plan(current_user.plan_id) if current_user.plan_id else None
        return render_template("dashboard.html", vps=vps, jobs=jobs, caps=snapshot(),
                               plan=plan, usage=db.user_usage(current_user.id))

    @app.route("/vps/create", methods=["GET", "POST"])
    @login_required
    def create_vps():
        plans = db.list_plans(only_enabled=True)
        images = db.list_os_images(only_enabled=True)
        nodes = [n for n in db.list_nodes() if n.enabled]
        if request.method == "GET":
            plan = db.get_plan(current_user.plan_id) if current_user.plan_id else (plans[0] if plans else None)
            return render_template("create_vps.html", plans=plans, os_images=images, nodes=nodes,
                                   plan=plan, default_vcpus=int(db.get_setting("default_vcpus", DEFAULT_VCPUS)),
                                   default_memory=int(db.get_setting("default_memory_mb", DEFAULT_MEMORY_MB)),
                                   default_disk=int(db.get_setting("default_disk_gb", DEFAULT_DISK_GB)))
        # POST — validate everything server-side.
        try:
            plan_id = int(request.form.get("plan_id") or 0)
        except ValueError:
            plan_id = 0
        plan = db.get_plan(plan_id) if plan_id else None
        if plan is None or not plan.enabled:
            flash("Please choose an available plan.", "danger")
            return redirect(url_for("create_vps"))
        try:
            name = backend.safe_name(request.form.get("name", ""))
        except Exception as e:
            flash(str(e), "danger")
            return redirect(url_for("create_vps"))
        if db.get_vps_by_name(name):
            flash("That VPS name is already in use.", "danger")
            return redirect(url_for("create_vps"))
        os_slug = request.form.get("os", "")
        image = db.get_os_image(os_slug)
        if image is None or not image.enabled:
            flash("Please choose an available operating system.", "danger")
            return redirect(url_for("create_vps"))
        engine = request.form.get("engine", "container")
        if engine not in ("container", "vm"):
            engine = "container"
        effective_limit = min(current_user.max_vps, plan.max_vps) if plan else current_user.max_vps
        if db.count_vps(current_user.id) >= effective_limit:
            flash(f"VPS limit reached ({effective_limit} on your plan/quota). Contact an admin to raise it.", "warning")
            return redirect(url_for("dashboard"))
        err = validate_resources(request.form.get("vcpus"), request.form.get("memory"), request.form.get("disk"), plan)
        if err:
            flash(err, "danger")
            return redirect(url_for("create_vps"))
        vcpus = int(request.form.get("vcpus")); memory = int(request.form.get("memory")); disk = int(request.form.get("disk"))
        err = node_capacity_error(vcpus, memory, disk)
        if err:
            flash(err, "danger")
            return redirect(url_for("create_vps"))
        if not nodes:
            token = node_token()
            nid = db.ensure_local_node(token)
            node = db.get_node(nid)
        else:
            try:
                node = db.get_node(int(request.form.get("node_id") or nodes[0].id)) or nodes[0]
            except ValueError:
                node = nodes[0]
        try:
            chosen = backend.resolve_backend(node.backend if node.backend != "auto" else None)
        except Exception as e:
            flash(str(e), "danger")
            return redirect(url_for("create_vps"))
        if chosen == "docker" and engine == "vm":
            flash("Full VMs require the Incus backend; this node provisions Docker containers.", "warning")
            engine = "container"
        try:
            vps_id = db.create_vps(current_user.id, node.id, name, os_slug, engine, vcpus,
                                   memory, disk, name, backend=chosen, plan_id=plan.id)
            db.create_job(vps_id, "provision", "Queued for VPS creation")
            audit("vps.create", name, f"{os_slug} {engine} {vcpus}vCPU/{memory}MiB/{disk}GiB via {chosen}")
            flash("VPS creation started. Live progress is shown on your dashboard.", "info")
        except Exception as e:
            flash(str(e), "danger")
        return redirect(url_for("dashboard"))

    @app.route("/vps/<int:vps_id>")
    @login_required
    def vps_detail(vps_id):
        v = own_vps_or_404(vps_id)
        return render_template("vps.html", vps=v, jobs=db.list_jobs_for_vps(vps_id, 15),
                               root_password=decrypt_text(v.root_password_enc))

    @app.route("/vps/<int:vps_id>/<op>", methods=["POST"])
    @login_required
    def vps_action(vps_id, op):
        v = own_vps_or_404(vps_id)
        if op == "delete":
            force = request.form.get("force") == "1"
            try:
                backend.remove_instance(v)
            except Exception as e:
                if not force:
                    flash(f"Backend cleanup failed ({e}). Record kept — retry or use force delete.", "danger")
                    return redirect(url_for("vps_detail", vps_id=vps_id))
                flash(f"Backend cleanup failed ({e}); record removed anyway (force).", "warning")
            from core import tmate
            tmate.stop_session(v)
            db.delete_vps(vps_id)
            audit("vps.delete", v.name, f"force={force}")
            flash("VPS deleted.", "success")
            return redirect(url_for("admin_vps") if request.form.get("from_admin") else url_for("dashboard"))
        if op == "reinstall":
            # Real reinstall: destroy the instance, then re-run the same
            # provisioning pipeline (fresh guest, new root password).
            try:
                backend.remove_instance(v)
            except Exception as e:
                flash(f"Reinstall aborted — the current instance could not be removed: {e}", "danger")
                return redirect(url_for("vps_detail", vps_id=vps_id))
            from core import tmate
            tmate.stop_session(v)
            db.update_vps(vps_id, state="creating", container_id=None, ip_address=None,
                          tmate_ssh=None, tmate_web=None, last_error=None, desired_state="running")
            db.create_job(vps_id, "provision", "Queued for reinstall")
            audit("vps.reinstall", v.name)
            flash("Reinstall started — the VPS is being rebuilt from its OS image. "
                  "A new root password will be generated; live progress shows on your dashboard.", "info")
            return redirect(url_for("dashboard"))
        if op not in ("start", "stop", "restart"):
            abort(404)
        try:
            st = backend.do_action(v, op)
            db.update_vps(vps_id, state=("running" if st.get("state") == "running" else "stopped"),
                          ip_address=st.get("ip"), desired_state=("running" if op in ("start", "restart") else "stopped"),
                          last_error=None)
            audit(f"vps.{op}", v.name)
            message = f"VPS {op} completed."
            category = "success"
            flash(message, category)
        except Exception as e:
            db.update_vps(vps_id, state="error", last_error=str(e)[:1000])
            flash(str(e), "danger")
        return redirect(url_for("vps_detail", vps_id=vps_id))

    @app.route("/vps/<int:vps_id>/reset-password", methods=["POST"])
    @login_required
    def vps_reset_password(vps_id):
        v = own_vps_or_404(vps_id)
        if v.state != "running":
            flash("VPS must be running to reset its root password.", "warning")
            return redirect(url_for("vps_detail", vps_id=vps_id))
        try:
            pw = secrets.token_urlsafe(12)
            import shlex
            backend.exec_shell(v, f"echo {shlex.quote('root:' + pw)} | chpasswd && echo ok", timeout=60)
            db.update_vps(vps_id, root_password_enc=encrypt_text(pw))
            audit("vps.reset_password", v.name)
            flash("Root password reset. It is shown in the Access section below.", "success")
        except Exception as e:
            flash(str(e), "danger")
        return redirect(url_for("vps_detail", vps_id=vps_id))

    @app.route("/vps/<int:vps_id>/tmate/<op>", methods=["POST"])
    @login_required
    def vps_tmate(vps_id, op):
        v = own_vps_or_404(vps_id)
        from core import tmate
        if op == "start":
            if v.state != "running":
                flash("VPS must be running to start an SSH (tmate) session.", "warning")
            else:
                try:
                    ssh, web = tmate.start_session(v)
                    db.update_vps(vps_id, tmate_ssh=ssh, tmate_web=web)
                    audit("vps.tmate_start", v.name)
                    flash("tmate SSH session started — connection string is in the Access section.", "success")
                except Exception as e:
                    flash(str(e), "danger")
        elif op == "stop":
            tmate.stop_session(v)
            db.update_vps(vps_id, tmate_ssh=None, tmate_web=None)
            audit("vps.tmate_stop", v.name)
            flash("tmate session stopped.", "success")
        else:
            abort(404)
        return redirect(url_for("vps_detail", vps_id=vps_id))

    @app.route("/vps/<int:vps_id>/console")
    @login_required
    def vps_console(vps_id):
        v = own_vps_or_404(vps_id)
        return render_template("console.html", vps=v)

    @app.route("/vps/<int:vps_id>/run", methods=["POST"])
    @login_required
    @limiter.limit("30 per minute", methods=["POST"])
    def vps_run(vps_id):
        """Run one command in the guest (argv built server-side, no shell)."""
        v = own_vps_or_404(vps_id)
        command = (request.form.get("command") or "").strip()
        if not command or len(command) > 500:
            flash("Enter a command (max 500 characters).", "warning")
            return redirect(url_for("vps_detail", vps_id=vps_id))
        if v.state != "running":
            flash("VPS must be running to execute commands.", "warning")
            return redirect(url_for("vps_detail", vps_id=vps_id))
        try:
            # Runs inside the user's own VPS as root (that shell access is
            # the feature); it can never reach the panel host.
            rc, out = backend.exec_shell(v, command, timeout=60)
            audit("vps.run", v.name, command[:200])
            flash(f"$ {command}\n(exit {rc})\n{out[-3000:] or '(no output)'}", "console")
        except Exception as e:
            flash(str(e), "danger")
        return redirect(url_for("vps_detail", vps_id=vps_id))

    @app.route("/profile", methods=["GET", "POST"])
    @login_required
    def profile():
        if request.method == "POST":
            form = request.form.get("form", "")
            if form == "email":
                email = request.form.get("email", "").strip()
                if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                    flash("Please enter a valid email address.", "danger")
                else:
                    db.update_user(current_user.id, email=email or None)
                    flash("Profile saved.", "success")
            elif form == "password":
                if not check_password_hash(current_user.password_hash, request.form.get("current_password", "")):
                    flash("Current password is incorrect.", "danger")
                else:
                    try:
                        db.set_user_password(current_user.id, request.form.get("new_password", ""))
                        session.pop("default_pw_warning", None)
                        audit("auth.change_password", current_user.username)
                        flash("Password changed.", "success")
                    except Exception as e:
                        flash(str(e), "danger")
            return redirect(url_for("profile"))
        plan = db.get_plan(current_user.plan_id) if current_user.plan_id else None
        return render_template("profile.html", plan=plan, usage=db.user_usage(current_user.id),
                               my_vps=db.list_vps_for_user(current_user.id))

    # ------------------------------------------------------------ API
    @app.route("/api/jobs/<int:job_id>")
    @login_required
    def api_job(job_id):
        j = db.get_job(job_id)
        if not j:
            abort(404)
        v = db.get_vps(j.vps_id) if j.vps_id else None
        if not v or (v.user_id != current_user.id and not current_user.is_admin):
            abort(403)
        return jsonify({"id": j.id, "status": j.status, "progress": j.progress,
                        "message": j.message, "details": db.job_details(j),
                        "updated_at": j.updated_at})

    @app.route("/api/node")
    @login_required
    def api_node():
        from core.capabilities import snapshot
        return jsonify(snapshot())

    @app.route("/api/vps/<int:vps_id>/stats")
    @login_required
    def api_vps_stats(vps_id):
        v = own_vps_or_404(vps_id)
        try:
            st = backend.status(v)
            db.update_vps(vps_id, state=("running" if st.get("state") == "running" else
                                         "stopped" if st.get("state") == "stopped" else v.state),
                          ip_address=st.get("ip"))
        except Exception as e:
            return jsonify({"error": str(e)[:300], "state": v.state}), 200
        return jsonify(backend.guest_stats(db.get_vps(vps_id)))

    # ------------------------------------------------------------ admin
    @app.route("/admin")
    @admin_required
    def admin():
        from core.capabilities import snapshot
        return render_template("admin.html", caps=snapshot(), users=db.all_users(),
                               vps=db.list_all_vps(), nodes=db.list_nodes(),
                               plans=db.list_plans(), jobs=db.list_recent_jobs(15),
                               settings=db.all_settings())

    @app.route("/admin/settings", methods=["POST"])
    @admin_required
    def admin_settings():
        b = request.form
        accent = (b.get("accent") or "#67e8f9").strip()
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
            flash("Accent must be a hex colour like #67e8f9.", "danger")
            return redirect(url_for("admin"))
        for key in ("website_url", "discord_url", "logo_url"):
            val = (b.get(key) or "").strip()
            if val and not re.match(r"^https?://", val):
                flash(f"{key} must start with http:// or https:// (or be empty).", "danger")
                return redirect(url_for("admin"))
        pairs = {"site_name": "site_name", "site_tagline": "site_tagline", "logo_url": "logo_url",
                 "accent": "accent", "website_url": "website_url", "discord_url": "discord_url",
                 "support_email": "support_email", "host_name": "host_name",
                 "default_vcpus": "default_vcpus", "default_memory_mb": "default_memory_mb",
                 "default_disk_gb": "default_disk_gb", "max_vps_per_user": "max_vps_per_user",
                 "default_backend": "default_backend"}
        for form_key, key in pairs.items():
            if b.get(form_key) is not None:
                db.set_setting(key, b.get(form_key).strip())
        db.set_setting("maintenance_mode", "true" if b.get("maintenance_mode") == "on" else "false")
        db.set_setting("allow_registration", "true" if b.get("allow_registration") == "on" else "false")
        db.set_setting("auto_update", "true" if b.get("auto_update") == "on" else "false")
        audit("admin.settings", "settings")
        flash("Settings saved.", "success")
        return redirect(url_for("admin"))

    @app.route("/admin/users")
    @admin_required
    def admin_users():
        return render_template("admin_users.html", users=db.all_users(), plans=db.list_plans())

    @app.route("/admin/users/<int:user_id>", methods=["POST"])
    @admin_required
    def admin_user_update(user_id):
        user = db.get_user(user_id)
        if not user:
            abort(404)
        op = request.form.get("op", "save")
        try:
            if op == "delete":
                if user.id == current_user.id:
                    flash("You cannot delete your own account.", "danger")
                else:
                    for v in db.list_vps_for_user(user.id):
                        try:
                            backend.remove_instance(v)
                        except Exception:
                            pass
                        db.delete_vps(v.id)
                    db.delete_user(user.id)
                    audit("admin.user_delete", user.username)
                    flash(f"User {user.username} and their VPS records deleted.", "success")
            elif op == "password":
                db.set_user_password(user.id, request.form.get("new_password", ""))
                audit("admin.user_password", user.username)
                flash("Password updated.", "success")
            else:
                role = request.form.get("role", user.role)
                if user.id == current_user.id and role != "admin":
                    flash("You cannot demote your own account.", "danger")
                    return redirect(url_for("admin_users"))
                plan_id = int(request.form.get("plan_id") or 0) or None
                db.update_user(user.id, role=role, email=request.form.get("email", "").strip() or None,
                               plan_id=plan_id, is_suspended=1 if request.form.get("suspended") == "on" else 0)
                db.set_user_quota(user.id, int(request.form.get("max_vps", user.max_vps)))
                audit("admin.user_update", user.username)
                flash("User updated.", "success")
        except Exception as e:
            flash(str(e), "danger")
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/create", methods=["POST"])
    @admin_required
    def admin_user_create():
        try:
            plan_id = int(request.form.get("plan_id") or 0) or None
            plan = db.get_plan(plan_id) if plan_id else None
            uid = db.create_user(request.form.get("username", "").strip(),
                                 request.form.get("password", ""),
                                 role=request.form.get("role", "user"),
                                 max_vps=(plan.max_vps if plan else None),
                                 email=request.form.get("email", "").strip() or None, plan_id=plan_id)
            audit("admin.user_create", request.form.get("username", ""))
            flash("User created.", "success")
        except Exception as e:
            flash(str(e), "danger")
        return redirect(url_for("admin_users"))

    @app.route("/admin/vps")
    @admin_required
    def admin_vps():
        return render_template("admin_vps.html", vps=db.list_all_vps(), users=db.all_users(),
                               plans=db.list_plans(), os_images=db.list_os_images(only_enabled=True),
                               nodes=[n for n in db.list_nodes() if n.enabled])

    @app.route("/admin/vps/create", methods=["POST"])
    @admin_required
    def admin_create_vps():
        try:
            owner = db.get_user(int(request.form.get("user_id") or 0))
            if not owner:
                raise ValueError("Choose a valid owner")
            plan = db.get_plan(int(request.form.get("plan_id") or 0))
            name = backend.safe_name(request.form.get("name", ""))
            if db.get_vps_by_name(name):
                raise ValueError("That VPS name is already in use")
            image = db.get_os_image(request.form.get("os", ""))
            if image is None or not image.enabled:
                raise ValueError("Choose an available OS image")
            # Admins may exceed plan caps deliberately — validate sanity only.
            err = validate_resources(request.form.get("vcpus"), request.form.get("memory"),
                                     request.form.get("disk"), None)
            if err:
                raise ValueError(err)
            nodes = [n for n in db.list_nodes() if n.enabled] or [db.get_node(db.ensure_local_node(node_token()))]
            node = db.get_node(int(request.form.get("node_id") or nodes[0].id)) or nodes[0]
            chosen = backend.resolve_backend(node.backend if node.backend != "auto" else None)
            vps_id = db.create_vps(owner.id, node.id, name, image.slug,
                                   request.form.get("engine", "container"),
                                   int(request.form.get("vcpus")), int(request.form.get("memory")),
                                   int(request.form.get("disk")), name, backend=chosen,
                                   plan_id=(plan.id if plan else None))
            db.create_job(vps_id, "provision", "Queued by admin")
            audit("admin.vps_create", name, f"owner={owner.username}")
            flash("VPS queued for provisioning.", "success")
        except Exception as e:
            flash(str(e), "danger")
        return redirect(url_for("admin_vps"))

    @app.route("/admin/vps/<int:vps_id>/resources", methods=["POST"])
    @admin_required
    def admin_vps_resources(vps_id):
        v = db.get_vps(vps_id)
        if not v:
            abort(404)
        err = validate_resources(request.form.get("vcpus"), request.form.get("memory"),
                                 request.form.get("disk"), None)
        if err:
            flash(err, "danger")
            return redirect(url_for("admin_vps"))
        cpu, mem, disk = int(request.form["vcpus"]), int(request.form["memory"]), int(request.form["disk"])
        try:
            res = backend.resize(v, cpu, mem, disk) if v.state in ("running", "stopped") else {}
            db.update_vps(vps_id, vcpus=cpu, memory_mb=mem, disk_gb=disk)
            note = res.get("disk_note") if isinstance(res, dict) else None
            audit("admin.vps_resize", v.name, f"{cpu}/{mem}/{disk}")
            flash("VPS resources updated." + (f" {note}" if note else ""), "success")
        except Exception as e:
            # Record the intended allocation even if the live resize failed,
            # but tell the admin the truth.
            db.update_vps(vps_id, vcpus=cpu, memory_mb=mem, disk_gb=disk, last_error=str(e)[:500])
            flash(f"Saved allocation, but live resize failed: {e}", "warning")
        return redirect(url_for("admin_vps"))

    @app.route("/admin/nodes", methods=["GET", "POST"])
    @admin_required
    def admin_nodes():
        if request.method == "POST":
            op = request.form.get("op", "create")
            try:
                if op == "create":
                    db.create_node(request.form.get("name", ""), request.form.get("host", "127.0.0.1"),
                                   int(request.form.get("port", 5556)),
                                   request.form.get("token", "") or secrets.token_urlsafe(24),
                                   request.form.get("backend", "auto"))
                    flash("Node added.", "success")
                elif op.startswith("save:"):
                    nid = int(op.split(":", 1)[1])
                    db.update_node(nid, name=request.form.get("name", ""), host=request.form.get("host", ""),
                                   port=int(request.form.get("port", 5556)),
                                   backend=request.form.get("backend", "auto"),
                                   enabled=1 if request.form.get("enabled") == "on" else 0)
                    db.set_node_quota(nid, request.form.get("cpu_limit"), request.form.get("memory_limit_mb"),
                                      request.form.get("disk_limit_gb"))
                    flash("Node saved.", "success")
                elif op.startswith("delete:"):
                    db.delete_node(int(op.split(":", 1)[1]))
                    flash("Node deleted.", "success")
                audit(f"admin.node_{op.split(':')[0]}", request.form.get("name", ""))
            except Exception as e:
                flash(str(e), "danger")
            return redirect(url_for("admin_nodes"))
        from core.capabilities import snapshot
        return render_template("admin_nodes.html", nodes=db.list_nodes(), caps=snapshot())

    @app.route("/admin/plans", methods=["GET", "POST"])
    @admin_required
    def admin_plans():
        if request.method == "POST":
            op = request.form.get("op", "create")
            try:
                if op == "create":
                    db.create_plan(request.form.get("name", ""), int(request.form.get("max_vcpus", 1)),
                                   int(request.form.get("max_memory_mb", 512)), int(request.form.get("max_disk_gb", 5)),
                                   int(request.form.get("max_vps", 1)))
                elif op.startswith("save:"):
                    pid = int(op.split(":", 1)[1])
                    db.update_plan(pid, name=request.form.get("name", ""), max_vcpus=int(request.form.get("max_vcpus", 1)),
                                   max_memory_mb=int(request.form.get("max_memory_mb", 512)),
                                   max_disk_gb=int(request.form.get("max_disk_gb", 5)),
                                   max_vps=int(request.form.get("max_vps", 1)),
                                   enabled=1 if request.form.get("enabled") == "on" else 0)
                elif op.startswith("delete:"):
                    db.delete_plan(int(op.split(":", 1)[1]))
                audit("admin.plan", op)
                flash("Plans updated.", "success")
            except Exception as e:
                flash(str(e), "danger")
            return redirect(url_for("admin_plans"))
        return render_template("admin_plans.html", plans=db.list_plans())

    @app.route("/admin/os-images", methods=["GET", "POST"])
    @admin_required
    def admin_os_images():
        if request.method == "POST":
            op = request.form.get("op", "create")
            try:
                if op == "create":
                    db.create_os_image(request.form.get("slug", ""), request.form.get("label", ""),
                                       request.form.get("incus_image", ""), request.form.get("docker_image", ""),
                                       request.form.get("family", "debian"))
                elif op.startswith("save:"):
                    iid = int(op.split(":", 1)[1])
                    db.update_os_image(iid, label=request.form.get("label", ""), incus_image=request.form.get("incus_image", ""),
                                       docker_image=request.form.get("docker_image", ""),
                                       family=request.form.get("family", "debian"),
                                       enabled=1 if request.form.get("enabled") == "on" else 0)
                elif op.startswith("delete:"):
                    db.delete_os_image(int(op.split(":", 1)[1]))
                audit("admin.os_image", op)
                flash("OS images updated.", "success")
            except Exception as e:
                flash(str(e), "danger")
            return redirect(url_for("admin_os_images"))
        return render_template("admin_os.html", images=db.list_os_images())

    @app.route("/admin/logs")
    @admin_required
    def admin_logs():
        log_text = ""
        log_file = DATA_DIR / "skvm.log"
        if log_file.exists():
            try:
                log_text = log_file.read_text(encoding="utf-8", errors="replace")[-12000:]
            except Exception as e:
                log_text = f"(could not read log: {e})"
        return render_template("admin_logs.html", jobs=db.list_recent_jobs(100),
                               audit=db.list_audit(200), log_text=log_text)


# --------------------------------------------------------------------------
# Real-time console (Flask-SocketIO + xterm.js, styled after webssh.html)
# --------------------------------------------------------------------------

_consoles: dict[str, dict] = {}
_consoles_lock = threading.Lock()


def _console_close(sid):
    with _consoles_lock:
        s = _consoles.pop(sid, None)
    if not s:
        return
    try:
        if s.get("proc") is not None:
            s["proc"].terminate()
    except Exception:
        pass
    try:
        if s.get("sock") is not None:
            s["sock"].close()
    except Exception:
        pass


def register_console(app):

    @socketio.on("connect", namespace="/console")
    def console_connect(auth=None):
        if not current_user.is_authenticated:
            return False
        try:
            vps_id = int(request.args.get("vps_id") or 0)
        except ValueError:
            return False
        v = db.get_vps(vps_id)
        if not v or (v.user_id != current_user.id and not current_user.is_admin):
            return False
        if v.state != "running":
            socketio.emit("output", "\r\n\x1b[33mVPS is not running. Start it first, then reconnect.\x1b[0m\r\n",
                          namespace="/console", to=request.sid)
            return False
        _console_close(request.sid)
        session_obj = {"vps_id": v.id, "user_id": current_user.id, "proc": None,
                       "sock": None, "fd": None, "exec_id": None, "backend": v.backend,
                       "name": v.name}
        try:
            if v.backend == "docker":
                import docker as docker_sdk
                client = docker_sdk.from_env(timeout=10)
                from core.docker_engine import container_name
                api = client.api
                exec_id = api.exec_create(container_name(v.name), ["/bin/bash", "-l"],
                                          tty=True, stdin=True)["Id"]
                sock = api.exec_start(exec_id, socket=True, tty=True)
                raw = getattr(sock, "_sock", sock)
                session_obj.update({"sock": raw, "exec_id": exec_id, "client": client})
                with _consoles_lock:
                    _consoles[request.sid] = session_obj
                socketio.start_background_task(_docker_reader, request.sid, raw)
            else:
                import pty as pty_mod
                import subprocess
                master, slave = pty_mod.openpty()
                proc = subprocess.Popen(["incus", "exec", v.name, "--", "/bin/bash", "-l"],
                                        stdin=slave, stdout=slave, stderr=slave, close_fds=True)
                os.close(slave)
                session_obj.update({"proc": proc, "fd": master})
                with _consoles_lock:
                    _consoles[request.sid] = session_obj
                socketio.start_background_task(_pty_reader, request.sid, master, proc)
        except Exception as e:
            socketio.emit("output", f"\r\n\x1b[31mConsole failed to start: {e}\x1b[0m\r\n",
                          namespace="/console", to=request.sid)
            return False
        db.audit(current_user, "vps.console_open", v.name, "", request.remote_addr or "")
        socketio.emit("output", f"\x1b[38;5;245mConnected to {v.name} ({v.backend}). Type 'exit' to end the shell.\x1b[0m\r\n",
                      namespace="/console", to=request.sid)
        return True

    def _pty_reader(sid, fd, proc):
        import select
        while True:
            if proc.poll() is not None:
                break
            r, _, _ = select.select([fd], [], [], 0.2)
            if fd in r:
                try:
                    data = os.read(fd, 8192)
                except OSError:
                    break
                if not data:
                    break
                socketio.emit("output", data.decode("utf-8", "replace"), namespace="/console", to=sid)
            socketio.sleep(0.01)
        socketio.emit("output", "\r\n\x1b[33mSession ended.\x1b[0m\r\n", namespace="/console", to=sid)
        _console_close(sid)

    def _docker_reader(sid, raw):
        raw.settimeout(0.3)
        import socket as socket_mod
        while True:
            try:
                data = raw.recv(8192)
            except (socket_mod.timeout, TimeoutError):
                socketio.sleep(0.01)
                continue
            except Exception:
                break
            if not data:
                break
            socketio.emit("output", data.decode("utf-8", "replace"), namespace="/console", to=sid)
        socketio.emit("output", "\r\n\x1b[33mSession ended.\x1b[0m\r\n", namespace="/console", to=sid)
        _console_close(sid)

    @socketio.on("input", namespace="/console")
    def console_input(data):
        if not current_user.is_authenticated:
            return False
        with _consoles_lock:
            s = _consoles.get(request.sid)
        if not s or s.get("user_id") != current_user.id:
            return False
        payload = (data or {}).get("data", "")
        if not isinstance(payload, str):
            return False
        try:
            if s.get("fd") is not None:
                os.write(s["fd"], payload.encode())
            elif s.get("sock") is not None:
                s["sock"].sendall(payload.encode())
        except Exception:
            _console_close(request.sid)

    @socketio.on("resize", namespace="/console")
    def console_resize(data):
        if not current_user.is_authenticated:
            return
        with _consoles_lock:
            s = _consoles.get(request.sid)
        if not s or s.get("user_id") != current_user.id:
            return
        try:
            cols = max(20, min(400, int((data or {}).get("cols", 80))))
            rows = max(5, min(200, int((data or {}).get("rows", 24))))
        except (TypeError, ValueError):
            return
        try:
            if s.get("fd") is not None:
                import fcntl
                import struct
                import termios
                fcntl.ioctl(s["fd"], termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            elif s.get("exec_id") and s.get("client") is not None:
                s["client"].api.exec_resize(s["exec_id"], height=rows, width=cols)
        except Exception:
            pass

    @socketio.on("ping_console", namespace="/console")
    def console_ping(data=None):
        # Real round-trip latency: the client timestamps this exchange.
        return {"t": time.time()}

    @socketio.on("disconnect", namespace="/console")
    def console_disconnect():
        _console_close(request.sid)


app = create_app()

if __name__ == "__main__":
    with app.app_context():
        db.init_db()
        boot = bootstrap_admin()
        db.ensure_local_node(node_token())
        if boot:
            print(f"[SKVM] First admin '{boot[0]}' created — credentials written to {boot[1]} (chmod 600).")
    socketio.run(app, host=BIND, port=PORT, debug=False)
