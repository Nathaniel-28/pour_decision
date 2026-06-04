import os
import queue
import json
import csv
from io import StringIO
from flask import Flask, render_template, request, jsonify, session, Response
from flask_sqlalchemy import SQLAlchemy
import uuid
import re
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from difflib import SequenceMatcher
from werkzeug.security import generate_password_hash, check_password_hash
import google.genai as genai

app = Flask(__name__)
app.secret_key = 'pour_decision_super_secret'
app.permanent_session_lifetime = timedelta(minutes=5)

# PostgreSQL & Railway Deployment Optimization
db_url = os.environ.get('DATABASE_URL', 'sqlite:///pour_decision.db')
# Force SQLAlchemy to use the pg8000 driver
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+pg8000://", 1)
elif db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+pg8000://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Setup upload folder for product images
UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

db = SQLAlchemy(app)


# Helper function to enforce Philippine Time (PHT / UTC+8) without external timezone libraries
def get_pht():
    return datetime.utcnow() + timedelta(hours=8)


# --- Store Configuration Model ---
class StoreConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    small_oz = db.Column(db.String(30), default="8oz")
    small_price = db.Column(db.Float, default=0.0)
    medium_oz = db.Column(db.String(30), default="12oz")
    medium_price = db.Column(db.Float, default=20.0)
    large_oz = db.Column(db.String(30), default="16oz")
    large_price = db.Column(db.Float, default=40.0)
    oat_price = db.Column(db.Float, default=30.0)
    almond_price = db.Column(db.Float, default=30.0)
    soy_price = db.Column(db.Float, default=30.0)
    shot_price = db.Column(db.Float, default=40.0)
    category_sorts = db.Column(db.Text, default="{}")
    category_order = db.Column(db.Text, default='["Hot", "Cold", "Rice Meal", "Pastries", "Snacks", "Sandwiches"]')


# --- Administrative Accounts Model ---
class AdminAccount(db.Model):
    __tablename__ = 'admin_account'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(50), nullable=False, index=True)
    type = db.Column(db.String(50), default="Other")
    allergens = db.Column(db.String(100), default="None")
    ingredients = db.Column(db.Text, default="")
    image = db.Column(db.String(255), nullable=False)
    is_available = db.Column(db.Boolean, default=True, index=True)

    has_size = db.Column(db.Boolean, default=False)
    has_milk = db.Column(db.Boolean, default=False)
    has_sweetness = db.Column(db.Boolean, default=False)
    has_addons = db.Column(db.Boolean, default=False)


class Order(db.Model):
    id = db.Column(db.String(8), primary_key=True)
    total_price = db.Column(db.Float, nullable=False)
    payment_method = db.Column(db.String(20), nullable=False)
    order_type = db.Column(db.String(20), nullable=False, default="Dine-in")
    status = db.Column(db.String(20), default="Completed", index=True)
    void_reason = db.Column(db.String(100), nullable=True)
    timestamp = db.Column(db.DateTime, default=get_pht, index=True)
    items = db.relationship('OrderItem', backref='order', lazy=True)


class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.String(8), db.ForeignKey('order.id'), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id', ondelete='SET NULL'), nullable=True, index=True)
    name = db.Column(db.String(100), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Float, nullable=False)
    modifiers = db.Column(db.String(255), default="")


def seed_database():
    if not StoreConfig.query.first():
        db.session.add(StoreConfig())

    if not AdminAccount.query.first():
        hashed_pass = generate_password_hash("admin123")
        db.session.add(AdminAccount(username="admin", password_hash=hashed_pass))

    if not Product.query.first():
        initial_menu = [
            {"name": "Hot Espresso", "price": 100, "category": "Hot", "allergens": "None", "type": "Espresso",
             "image": "espresso.jpg", "ingredients": "A pure, concentrated shot of our signature coffee beans.",
             "has_size": True, "has_milk": True, "has_sweetness": True, "has_addons": True},
            {"name": "Hot Latte", "price": 150, "category": "Hot", "allergens": "Dairy", "type": "Espresso",
             "image": "latte.jpg", "ingredients": "Rich espresso balanced with steamed milk and a light layer of foam.",
             "has_size": True, "has_milk": True, "has_sweetness": True, "has_addons": True},
            {"name": "Hot Americano", "price": 120, "category": "Hot", "allergens": "None", "type": "Espresso",
             "image": "americano.jpg",
             "ingredients": "Espresso shots topped with hot water for a light layer of crema.",
             "has_size": True, "has_milk": False, "has_sweetness": True, "has_addons": True},
            {"name": "Hot Cappuccino", "price": 140, "category": "Hot", "allergens": "Dairy", "type": "Espresso",
             "image": "placeholder.jpg",
             "ingredients": "Dark, rich espresso lying in wait under a smoothed and stretched layer of thick milk foam.",
             "has_size": True, "has_milk": True, "has_sweetness": True, "has_addons": True},
            {"name": "Hot Mocha", "price": 160, "category": "Hot", "allergens": "Dairy", "type": "Espresso",
             "image": "placeholder.jpg", "ingredients": "Espresso with bittersweet mocha sauce and steamed milk.",
             "has_size": True, "has_milk": True, "has_sweetness": True, "has_addons": True},
            {"name": "Hot Macchiato", "price": 130, "category": "Hot", "allergens": "Dairy", "type": "Espresso",
             "image": "placeholder.jpg",
             "ingredients": "Freshly pulled espresso shots topped with a dollop of milk foam.",
             "has_size": True, "has_milk": True, "has_sweetness": True, "has_addons": True},
            {"name": "Hot Chocolate", "price": 140, "category": "Hot", "allergens": "Dairy", "type": "Non-Espresso",
             "image": "placeholder.jpg",
             "ingredients": "Steamed milk and mocha sauce topped with sweetened whipped cream.",
             "has_size": True, "has_milk": True, "has_sweetness": True, "has_addons": False},

            {"name": "Iced Matcha", "price": 160, "category": "Cold", "allergens": "Dairy", "type": "Non-Espresso",
             "image": "iced_matcha.jpg",
             "ingredients": "Premium matcha green tea powder blended with cold milk and ice.",
             "has_size": True, "has_milk": True, "has_sweetness": True, "has_addons": False},
            {"name": "Iced Latte", "price": 170, "category": "Cold", "allergens": "Dairy", "type": "Espresso",
             "image": "iced_latte.jpg", "ingredients": "Espresso and chilled milk poured over ice.",
             "has_size": True, "has_milk": True, "has_sweetness": True, "has_addons": True},
            {"name": "Cold Brew", "price": 160, "category": "Cold", "allergens": "None", "type": "Non-Espresso",
             "image": "cold_brew.jpg",
             "ingredients": "Coffee steeped in cool water for 20 hours to create a smooth, rich flavor.",
             "has_size": True, "has_milk": False, "has_sweetness": True, "has_addons": False},
            {"name": "Iced Mocha", "price": 180, "category": "Cold", "allergens": "Dairy", "type": "Espresso",
             "image": "placeholder.jpg",
             "ingredients": "Espresso combined with bittersweet mocha sauce and milk over ice.",
             "has_size": True, "has_milk": True, "has_sweetness": True, "has_addons": True},
            {"name": "Iced Macchiato", "price": 160, "category": "Cold", "allergens": "Dairy", "type": "Espresso",
             "image": "placeholder.jpg",
             "ingredients": "Rich espresso combined with vanilla-flavored syrup, milk and ice.",
             "has_size": True, "has_milk": True, "has_sweetness": True, "has_addons": True},
            {"name": "Caramel Frappe", "price": 190, "category": "Cold", "allergens": "Dairy", "type": "Espresso",
             "image": "placeholder.jpg",
             "ingredients": "Caramel syrup blended with coffee, milk and ice, topped with whipped cream.",
             "has_size": True, "has_milk": True, "has_sweetness": True, "has_addons": True},
            {"name": "Iced Tea", "price": 110, "category": "Cold", "allergens": "None", "type": "Non-Espresso",
             "image": "placeholder.jpg",
             "ingredients": "Classic sweetened black tea served over ice with a slice of lemon.",
             "has_size": True, "has_milk": False, "has_sweetness": True, "has_addons": False},
            {"name": "Mango Graham Shake", "price": 180, "category": "Cold", "allergens": "Dairy, Gluten",
             "type": "Non-Espresso", "image": "placeholder.jpg",
             "ingredients": "Fresh mangoes blended with milk, ice, and crushed graham crackers.",
             "has_size": True, "has_milk": False, "has_sweetness": True, "has_addons": False},

            {"name": "Tapsilog", "price": 185, "category": "Rice Meal", "allergens": "Egg, Soy", "type": "Other",
             "image": "tapsilog.jpg",
             "ingredients": "Marinated beef tapa, garlic fried rice, and a sunny-side-up egg."},
            {"name": "Longsilog", "price": 165, "category": "Rice Meal", "allergens": "Egg", "type": "Other",
             "image": "longsilog.jpg",
             "ingredients": "Sweet Filipino pork sausage, garlic fried rice, and a sunny-side-up egg."},
            {"name": "Tocilog", "price": 165, "category": "Rice Meal", "allergens": "Egg", "type": "Other",
             "image": "placeholder.jpg",
             "ingredients": "Sweet cured pork (tocino), garlic fried rice, and a sunny-side-up egg."},
            {"name": "Bangsilog", "price": 195, "category": "Rice Meal", "allergens": "Egg, Fish", "type": "Other",
             "image": "placeholder.jpg",
             "ingredients": "Marinated milkfish (bangus), garlic fried rice, and a sunny-side-up egg."},
            {"name": "Chicken Adobo", "price": 175, "category": "Rice Meal", "allergens": "Soy", "type": "Other",
             "image": "placeholder.jpg",
             "ingredients": "Classic Filipino chicken braised in soy sauce, vinegar, and garlic, served with steamed rice."},

            {"name": "Croissant", "price": 95, "category": "Pastries", "allergens": "Gluten, Dairy", "type": "Other",
             "image": "croissant.jpg", "ingredients": "A buttery, flaky, classic French pastry."},
            {"name": "Ube Pandesal", "price": 45, "category": "Pastries", "allergens": "Gluten, Dairy", "type": "Other",
             "image": "ube_pandesal.jpg", "ingredients": "Soft Filipino bread rolls flavored with sweet purple yam."},
            {"name": "Ensaymada", "price": 85, "category": "Pastries", "allergens": "Gluten, Dairy, Egg",
             "type": "Other", "image": "placeholder.jpg",
             "ingredients": "Sweet, fluffy Filipino brioche baked with butter and topped with grated cheese."},
            {"name": "Bibingka", "price": 110, "category": "Pastries", "allergens": "Dairy, Egg", "type": "Other",
             "image": "placeholder.jpg",
             "ingredients": "Traditional Filipino baked rice cake topped with salted egg and grated coconut."},

            {"name": "Turon", "price": 60, "category": "Snacks", "allergens": "Gluten", "type": "Other",
             "image": "placeholder.jpg",
             "ingredients": "Deep-fried banana rolls coated with caramelized brown sugar and jackfruit."},
            {"name": "Banana Cue", "price": 50, "category": "Snacks", "allergens": "None", "type": "Other",
             "image": "placeholder.jpg",
             "ingredients": "Deep-fried saba bananas coated in caramelized brown sugar on a skewer."},
            {"name": "Kamote Fries", "price": 85, "category": "Snacks", "allergens": "None", "type": "Other",
             "image": "placeholder.jpg", "ingredients": "Sweet potato fries lightly salted and fried to a crisp."},
            {"name": "French Fries", "price": 90, "category": "Snacks", "allergens": "None", "type": "Other",
             "image": "placeholder.jpg",
             "ingredients": "Classic shoestring potato fries, perfectly salted and golden crisp."},

            {"name": "Chicken Sandwich", "price": 150, "category": "Sandwiches", "allergens": "Gluten, Dairy, Egg",
             "type": "Other", "image": "placeholder.jpg",
             "ingredients": "Creamy chicken salad with lettuce on toasted bread."},
            {"name": "Tuna Sandwich", "price": 140, "category": "Sandwiches", "allergens": "Gluten, Fish, Egg",
             "type": "Other", "image": "placeholder.jpg",
             "ingredients": "Savory tuna salad spread with lettuce on toasted bread."},
            {"name": "Grilled Cheese", "price": 120, "category": "Sandwiches", "allergens": "Gluten, Dairy",
             "type": "Other", "image": "placeholder.jpg",
             "ingredients": "Melted cheddar and mozzarella cheese pressed between buttered, toasted bread."}
        ]

        for item in initial_menu:
            item.setdefault('has_size', False)
            item.setdefault('has_milk', False)
            item.setdefault('has_sweetness', False)
            item.setdefault('has_addons', False)
            db.session.add(Product(**item))

    db.session.commit()


with app.app_context():
    try:
        db.session.query(Product.is_available).first()
        db.session.query(OrderItem.modifiers).first()
        db.session.query(Product.has_size).first()
        db.session.query(StoreConfig.small_oz).first()
        db.session.query(AdminAccount.username).first()
    except Exception:
        db.session.rollback()
        db.drop_all()

    db.create_all()

    try:
        db.session.execute(db.text("SELECT category_sorts FROM store_config LIMIT 1"))
    except Exception:
        db.session.rollback()
        try:
            db.session.execute(db.text("ALTER TABLE store_config ADD COLUMN category_sorts TEXT DEFAULT '{}'"))
            db.session.commit()
        except:
            db.session.rollback()

    try:
        db.session.execute(db.text("SELECT category_order FROM store_config LIMIT 1"))
    except Exception:
        db.session.rollback()
        try:
            db.session.execute(db.text(
                "ALTER TABLE store_config ADD COLUMN category_order TEXT DEFAULT '[\"Hot\", \"Cold\", \"Rice Meal\", \"Pastries\", \"Snacks\", \"Sandwiches\"]'"))
            db.session.commit()
        except:
            db.session.rollback()

    try:
        db.session.execute(db.text("SELECT void_reason FROM \"order\" LIMIT 1"))
    except Exception:
        db.session.rollback()
        try:
            db.session.execute(db.text("ALTER TABLE \"order\" ADD COLUMN void_reason VARCHAR(100)"))
            db.session.commit()
        except:
            db.session.rollback()

    seed_database()

# NLP Mappings
WORD_TO_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
               "nine": 9, "ten": 10}
NUM_TO_WORD = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine",
               10: "ten"}

clients = []


def notify_clients(message_dict):
    msg = f"data: {json.dumps(message_dict)}\n\n"
    for q in clients:
        q.put(msg)


@app.route('/stream')
def stream():
    def event_stream():
        q = queue.Queue()
        clients.append(q)
        try:
            while True:
                yield q.get()
        except GeneratorExit:
            if q in clients:
                clients.remove(q)

    return Response(event_stream(), mimetype="text/event-stream")


def get_best_fuzzy_match(text, items):
    best_match = None
    best_phrase = ""
    highest_ratio = 0.0
    words = text.split()

    max_len = min(5, len(words) + 1)
    for length in range(1, max_len):
        for i in range(len(words) - length + 1):
            phrase = " ".join(words[i:i + length])
            for item in items:
                ratio = SequenceMatcher(None, phrase, item['name'].lower()).ratio()
                if ratio > highest_ratio:
                    highest_ratio = ratio
                    best_match = item
                    best_phrase = phrase
    return best_match, best_phrase, highest_ratio


@app.route('/')
def index():
    products = Product.query.all()
    config = StoreConfig.query.first()

    cat_sorts = {}
    cat_order = ["Hot", "Cold", "Rice Meal", "Pastries", "Snacks", "Sandwiches"]
    if config:
        if config.category_sorts:
            try:
                cat_sorts = json.loads(config.category_sorts)
            except:
                pass
        if config.category_order:
            try:
                cat_order = json.loads(config.category_order)
            except:
                pass

    temp_menu = {}
    for p in products:
        if p.category not in temp_menu:
            temp_menu[p.category] = []
        temp_menu[p.category].append({
            "id": p.id, "name": p.name, "price": p.price, "allergens": p.allergens,
            "category": p.category, "type": p.type, "image": p.image,
            "ingredients": p.ingredients, "is_available": p.is_available,
            "has_size": p.has_size, "has_milk": p.has_milk,
            "has_sweetness": p.has_sweetness, "has_addons": p.has_addons
        })

    menu = {}
    # Prioritize saved categories configuration specifically for UI Render structure
    for cat in cat_order:
        if cat in temp_menu:
            menu[cat] = temp_menu[cat]
            sort_order = cat_sorts.get(cat, 'asc')
            menu[cat].sort(key=lambda x: x['name'].lower(), reverse=(sort_order == 'desc'))

    # Process and append any unconfigured remnant categories
    for cat in temp_menu:
        if cat not in menu:
            menu[cat] = temp_menu[cat]
            sort_order = cat_sorts.get(cat, 'asc')
            menu[cat].sort(key=lambda x: x['name'].lower(), reverse=(sort_order == 'desc'))

    return render_template('index.html', menu=menu, config=config)


@app.route('/admin')
def admin():
    if not session.get('admin_auth'):
        return render_template('login.html')
    return render_template('admin.html')


@app.route('/admin/login', methods=['POST'])
def admin_login():
    data = request.json or {}
    username = data.get('username', 'admin')
    password = data.get('password', '')

    admin_acct = AdminAccount.query.filter_by(username=username).first()
    if admin_acct and check_password_hash(admin_acct.password_hash, password):
        session.permanent = True
        session['admin_auth'] = True
        session['username'] = admin_acct.username
        return jsonify({"success": True})
    return jsonify({"success": False, "message": "Invalid credentials"}), 401


@app.route('/admin/logout', methods=['POST'])
def admin_logout():
    session.pop('admin_auth', None)
    session.pop('username', None)
    return jsonify({"success": True})


@app.route('/api/admin/change-password', methods=['POST'])
def change_password():
    if not session.get('admin_auth'):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json or {}
    current_password = data.get('current_password', '')
    new_password = data.get('new_password', '')

    username = session.get('username', 'admin')
    admin_acct = AdminAccount.query.filter_by(username=username).first()

    if not admin_acct or not check_password_hash(admin_acct.password_hash, current_password):
        return jsonify({"success": False, "message": "Incorrect current password"}), 400

    admin_acct.password_hash = generate_password_hash(new_password)
    db.session.commit()
    return jsonify({"success": True, "message": "Password changed successfully"})


@app.route('/api/settings', methods=['GET', 'POST'])
def manage_settings():
    if not session.get('admin_auth'):
        return jsonify({"error": "Unauthorized"}), 401

    config = StoreConfig.query.first()
    if not config:
        config = StoreConfig()
        db.session.add(config)
        db.session.commit()

    if request.method == 'GET':
        try:
            cat_sorts = json.loads(config.category_sorts) if config.category_sorts else {}
        except:
            cat_sorts = {}

        try:
            cat_order = json.loads(config.category_order) if config.category_order else ["Hot", "Cold", "Rice Meal",
                                                                                         "Pastries", "Snacks",
                                                                                         "Sandwiches"]
        except:
            cat_order = ["Hot", "Cold", "Rice Meal", "Pastries", "Snacks", "Sandwiches"]

        return jsonify({
            "small_oz": config.small_oz, "small_price": config.small_price,
            "medium_oz": config.medium_oz, "medium_price": config.medium_price,
            "large_oz": config.large_oz, "large_price": config.large_price,
            "oat_price": config.oat_price, "almond_price": config.almond_price,
            "soy_price": config.soy_price, "shot_price": config.shot_price,
            "category_sorts": cat_sorts,
            "category_order": cat_order
        })

    if request.method == 'POST':
        config.small_oz = request.json.get('small_oz', config.small_oz)
        config.small_price = float(request.json.get('small_price', config.small_price))
        config.medium_oz = request.json.get('medium_oz', config.medium_oz)
        config.medium_price = float(request.json.get('medium_price', config.medium_price))
        config.large_oz = request.json.get('large_oz', config.large_oz)
        config.large_price = float(request.json.get('large_price', config.large_price))
        config.oat_price = float(request.json.get('oat_price', config.oat_price))
        config.almond_price = float(request.json.get('almond_price', config.almond_price))
        config.soy_price = float(request.json.get('soy_price', config.soy_price))
        config.shot_price = float(request.json.get('shot_price', config.shot_price))

        db.session.commit()
        notify_clients({"type": "refresh"})
        return jsonify({"success": True})


@app.route('/api/settings/sort', methods=['POST'])
def save_sort_settings():
    if not session.get('admin_auth'):
        return jsonify({"error": "Unauthorized"}), 401

    config = StoreConfig.query.first()
    if config:
        config.category_sorts = json.dumps(request.json.get('category_sorts', {}))
        db.session.commit()
        notify_clients({"type": "refresh"})
    return jsonify({"success": True})


@app.route('/api/settings/category-order', methods=['POST'])
def save_category_order():
    if not session.get('admin_auth'):
        return jsonify({"error": "Unauthorized"}), 401

    config = StoreConfig.query.first()
    if config:
        config.category_order = json.dumps(request.json.get('category_order', []))
        db.session.commit()
        notify_clients({"type": "refresh"})
    return jsonify({"success": True})


@app.route('/api/products', methods=['GET', 'POST'])
def manage_products():
    if not session.get('admin_auth'):
        return jsonify({"error": "Unauthorized"}), 401

    if request.method == 'GET':
        products = Product.query.all()
        return jsonify([{
            "id": p.id, "name": p.name, "price": p.price, "category": p.category,
            "type": p.type, "allergens": p.allergens, "ingredients": p.ingredients, "image": p.image,
            "is_available": p.is_available, "has_size": p.has_size,
            "has_milk": p.has_milk, "has_sweetness": p.has_sweetness, "has_addons": p.has_addons
        } for p in products])

    if request.method == 'POST':
        try:
            name = request.form.get('name')
            price = float(request.form.get('price'))
            category = request.form.get('category')
            item_type = request.form.get('type', 'Other')
            allergens = request.form.get('allergens', 'None')
            ingredients = request.form.get('ingredients', '')

            has_size = str(request.form.get('has_size', '')).lower() == 'true'
            has_milk = str(request.form.get('has_milk', '')).lower() == 'true'
            has_sweetness = str(request.form.get('has_sweetness', '')).lower() == 'true'
            has_addons = str(request.form.get('has_addons', '')).lower() == 'true'

            image_file = request.files.get('image')
            image_filename = 'placeholder.jpg'

            if image_file and image_file.filename != '':
                filename = secure_filename(image_file.filename)
                unique_filename = f"{uuid.uuid4().hex}_{filename}"
                image_file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_filename))
                image_filename = f"uploads/{unique_filename}"

            new_product = Product(name=name, price=price, category=category, type=item_type,
                                  allergens=allergens, ingredients=ingredients, image=image_filename,
                                  has_size=has_size, has_milk=has_milk,
                                  has_sweetness=has_sweetness, has_addons=has_addons)
            db.session.add(new_product)
            db.session.commit()
            notify_clients({"type": "refresh"})
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/products/<int:pid>', methods=['PUT', 'DELETE'])
def update_delete_product(pid):
    if not session.get('admin_auth'):
        return jsonify({"error": "Unauthorized"}), 401

    product = Product.query.get_or_404(pid)

    if request.method == 'DELETE':
        db.session.delete(product)
        db.session.commit()
        notify_clients({"type": "refresh"})
        return jsonify({"success": True})

    if request.method == 'PUT':
        try:
            product.name = request.form.get('name', product.name)
            product.price = float(request.form.get('price', product.price))
            product.category = request.form.get('category', product.category)
            product.type = request.form.get('type', product.type)
            product.allergens = request.form.get('allergens', product.allergens)
            product.ingredients = request.form.get('ingredients', product.ingredients)

            product.has_size = str(request.form.get('has_size', '')).lower() == 'true'
            product.has_milk = str(request.form.get('has_milk', '')).lower() == 'true'
            product.has_sweetness = str(request.form.get('has_sweetness', '')).lower() == 'true'
            product.has_addons = str(request.form.get('has_addons', '')).lower() == 'true'

            image_file = request.files.get('image')
            if image_file and image_file.filename != '':
                filename = secure_filename(image_file.filename)
                unique_filename = f"{uuid.uuid4().hex}_{filename}"
                image_file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_filename))
                product.image = f"uploads/{unique_filename}"

            db.session.commit()
            notify_clients({"type": "refresh"})
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/products/<int:pid>/toggle', methods=['POST'])
def toggle_availability(pid):
    if not session.get('admin_auth'):
        return jsonify({"error": "Unauthorized"}), 401

    product = Product.query.get_or_404(pid)
    product.is_available = not product.is_available
    db.session.commit()
    notify_clients({
        "type": "toggle",
        "id": pid,
        "is_available": product.is_available,
        "name": product.name
    })
    return jsonify({"success": True, "is_available": product.is_available})


@app.route('/process-voice', methods=['POST'])
def process_voice():
    data = request.json
    text = data.get('text', '').lower()

    products = Product.query.all()
    all_items = [
        {"name": p.name, "price": p.price, "allergens": p.allergens, "ingredients": p.ingredients, "type": p.type,
         "category": p.category, "is_available": p.is_available,
         "has_size": p.has_size, "has_milk": p.has_milk,
         "has_sweetness": p.has_sweetness, "has_addons": p.has_addons} for p in products]

    # Sort items by descending name length to ensure greedy matching (e.g. "Hot Latte" over "Latte")
    all_items.sort(key=lambda x: len(x['name']), reverse=True)

    config = StoreConfig.query.first()
    generic_not_found_reply = "Apologies, we actually don't have that item on our menu right now."

    clear_exact_phrases = ["cancel all", "remove all", "clear up", "clear my", "clear the", "clear tray", "clear order",
                           "empty my", "empty the", "remove everything"]
    if any(phrase in text for phrase in clear_exact_phrases) or text.strip() == "clear":
        return jsonify({"action": "clear_cart", "reply": "Got it, I've completely cleared your tray."})

    inquiry_keywords = ["what", "ingredients", "allergen", "contain", "inside", "made of"]
    if any(word in text for word in inquiry_keywords):
        for item in all_items:
            if item['name'].lower() in text:
                return jsonify({"action": "info",
                                "reply": f"Our {item['name']} is made with {item['ingredients']}. Just a heads-up, it contains {item['allergens']}."})
        return jsonify({"action": "none", "reply": "I'd love to help! Which specific item did you want to know about?"})

    avail_keywords = ["is available", "are available", "do you have", "do you sell", "selling", "is there any"]
    buy_keywords = ["add", "order", "get", "buy", "want", "give me", "i'll take", "increase", "decrease", "remove",
                    "two", "three", "four", "five"]
    is_asking_avail = any(word in text for word in avail_keywords) and not any(word in text for word in buy_keywords)

    if is_asking_avail:
        best_item, best_phrase, ratio = get_best_fuzzy_match(re.sub(r'[^\w\s]', '', text), all_items)
        if best_item and ratio >= 0.65:
            if best_item['is_available']:
                reply = f"Yes, we definitely have the {best_item['name']} ready for you! "
                prompts = []
                if best_item['has_size']:
                    prompts.append(
                        f"what size you'd prefer (Small ({config.small_oz}), Medium ({config.medium_oz}), or Large ({config.large_oz}))")
                if best_item['has_milk']:
                    prompts.append("your choice of milk")
                if best_item['has_sweetness']:
                    prompts.append("sweetness level")

                if prompts:
                    if len(prompts) == 1:
                        prompt_str = prompts[0]
                    elif len(prompts) == 2:
                        prompt_str = f"{prompts[0]} and {prompts[1]}"
                    else:
                        prompt_str = f"{', '.join(prompts[:-1])}, and {prompts[-1]}"
                    reply += f"Just let me know {prompt_str}. "

                reply += "Whenever you're ready, you can order here on the screen or just tell me!"
                return jsonify({"action": "none", "reply": reply})
            else:
                similar = [p for p in all_items if
                           p['is_available'] and p['category'] == best_item['category'] and p['name'] != best_item[
                               'name']]
                suggest_str = ""
                if len(similar) >= 2:
                    suggest_str = f"Instead, how about a nice {similar[0]['name']} or a {similar[1]['name']}? "
                elif len(similar) == 1:
                    suggest_str = f"We do have the {similar[0]['name']} available instead, if you'd like. "

                reply = f"Oh no, I'm so sorry! It looks like the {best_item['name']} is currently unavailable right now. " + suggest_str + "Feel free to check out the menu board for other great options!"
                return jsonify({"action": "none", "reply": reply})
        else:
            return jsonify({"action": "none", "reply": generic_not_found_reply})

    # Standard configuration mapping
    sizes = {
        "small": {"label": f"Small ({config.small_oz})", "price": config.small_price},
        "medium": {"label": f"Medium ({config.medium_oz})", "price": config.medium_price},
        "large": {"label": f"Large ({config.large_oz})", "price": config.large_price}
    }
    milk_options = {
        "whole milk": 0, "skim milk": 0,
        "oat milk": config.oat_price,
        "almond milk": config.almond_price,
        "soy milk": config.soy_price
    }
    sweetness_options = {"100% sugar": "100% Sugar", "75% sugar": "75% Sugar", "50% sugar": "50% Sugar",
                         "25% sugar": "25% Sugar", "0% sugar": "0% Sugar", "no sugar": "0% Sugar",
                         "less sweet": "50% Sugar", "less sugar": "50% Sugar", "half sugar": "50% Sugar",
                         "quarter sugar": "25% Sugar"}
    shot_pattern = r'\b(\d+|' + '|'.join(WORD_TO_NUM.keys()) + r')?\s*extra\s*shot(s)?\b'

    remove_keywords = ["remove", "delete", "cancel", "take off", "decrease", "reduce", "subtract", "drop", "lessen"]
    is_remove = any(word in text for word in remove_keywords)

    added_items = []
    removed_items = []
    unavailable_requested = []

    # 1. Evaluate & Extract Global Modifiers First
    global_size = None
    global_milk = None
    global_sweetness = None
    global_shots = None

    is_global = any(phrase in text for phrase in ["for all", "all of them", "all with"])
    if is_global:
        # Isolate text chunk after the very last mentioned product to safely grab global assignments
        last_prod_idx = -1
        for item in all_items:
            idx = text.rfind(item['name'].lower())
            if idx > last_prod_idx:
                last_prod_idx = idx

        global_text = text[last_prod_idx:] if last_prod_idx != -1 else text

        for sz_key, sz_data in sizes.items():
            if re.search(r'\b' + sz_key + r'\b', global_text):
                global_size = sz_data
                text = re.sub(r'\b' + sz_key + r'\b', "", text, count=1)
                break
        for m, p in milk_options.items():
            if m in global_text:
                global_milk = {"label": m.title(), "price": p}
                text = text.replace(m, "", 1)
                break
        for s, label in sweetness_options.items():
            if s in global_text:
                global_sweetness = label
                text = text.replace(s, "", 1)
                break
        shot_match = re.search(shot_pattern, global_text)
        if shot_match:
            qty_str = shot_match.group(1)
            shot_qty = int(qty_str) if qty_str and qty_str.isdigit() else (
                WORD_TO_NUM.get(qty_str, 1) if qty_str else 1)
            global_shots = {"qty": shot_qty, "price": shot_qty * config.shot_price,
                            "label": f"{shot_qty} extra shot{'s' if shot_qty > 1 else ''}"}
            text = re.sub(shot_pattern, "", text, count=1)

    # 2. Split Order by Common Phrase Structures to Isolates Specific Commands
    chunks = re.split(r',\s*and\s+|\s+and\s+|,\s*|\s+then\s+', text)

    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk: continue

        chunk_items = []
        temp_chunk = chunk
        # Retrieve all items mentioned within this specific isolated chunk
        for item in all_items:
            if item['name'].lower() in temp_chunk:
                chunk_items.append(item)
                temp_chunk = temp_chunk.replace(item['name'].lower(), " ")

        if not chunk_items:
            continue

        # Extract strictly localized modifiers meant solely for items inside this chunk
        local_size = None
        for sz_key, sz_data in sizes.items():
            if re.search(r'\b' + sz_key + r'\b', temp_chunk):
                local_size = sz_data
                temp_chunk = re.sub(r'\b' + sz_key + r'\b', "", temp_chunk, count=1)
                break

        local_milk = None
        for m, p in milk_options.items():
            if m in temp_chunk:
                local_milk = {"label": m.title(), "price": p}
                temp_chunk = temp_chunk.replace(m, "", 1)
                break

        local_sweetness = None
        for s, label in sweetness_options.items():
            if s in temp_chunk:
                local_sweetness = label
                temp_chunk = temp_chunk.replace(s, "", 1)
                break

        local_shots = None
        shot_match = re.search(shot_pattern, temp_chunk)
        if shot_match:
            qty_str = shot_match.group(1)
            shot_qty = int(qty_str) if qty_str and qty_str.isdigit() else (
                WORD_TO_NUM.get(qty_str, 1) if qty_str else 1)
            local_shots = {"qty": shot_qty, "price": shot_qty * config.shot_price,
                           "label": f"{shot_qty} extra shot{'s' if shot_qty > 1 else ''}"}

        # Validate Quantities and Merge Specific/Global Modifiers
        for item in chunk_items:
            qty = 1
            pattern = r'\b(\d+|' + '|'.join(WORD_TO_NUM.keys()) + r')\b(?:\s+\w+){0,4}\s*' + re.escape(
                item['name'].lower())
            match = re.search(pattern, chunk)
            if match:
                q_str = match.group(1)
                qty = int(q_str) if q_str.isdigit() else WORD_TO_NUM.get(q_str, 1)

            if is_remove:
                removed_items.append({"item": item, "quantity": qty})
                continue

            if not item['is_available']:
                unavailable_requested.append(item['name'])
                continue

            # Prioritize chunk-specific configurations over globals
            final_size = local_size or global_size
            final_milk = local_milk or global_milk
            final_sweetness = local_sweetness or global_sweetness
            final_shots = local_shots or global_shots

            unit_price = item['price']
            item_mods = []

            if item['has_size']:
                if final_size:
                    unit_price += final_size['price']
                    item_mods.append(final_size['label'])
                else:
                    unit_price += config.medium_price
                    item_mods.append(f"Medium ({config.medium_oz})")

            if item['has_milk'] and final_milk and final_milk['label'] != 'Whole Milk':
                unit_price += final_milk['price']
                item_mods.append(final_milk['label'])

            if item['has_sweetness'] and final_sweetness and final_sweetness != '100% Sugar':
                item_mods.append(final_sweetness)

            if item['has_addons'] and final_shots:
                unit_price += final_shots['price']
                item_mods.append(final_shots['label'])

            added_items.append({
                "item": item,
                "quantity": qty,
                "unit_price": unit_price,
                "modifiers": item_mods
            })

    # Response Generation Matrix
    if removed_items:
        reply_parts = []
        for i in removed_items:
            i_qty = i['quantity']
            qty_word = NUM_TO_WORD.get(i_qty, str(i_qty))
            i_name = i['item']['name'].lower()
            order_word = "orders" if i_qty > 1 else "order"

            if i['item']['has_size'] or i['item']['has_milk']:
                reply_parts.append(f"{qty_word} {i_name}")
            else:
                reply_parts.append(f"{qty_word} {order_word} of {i_name}")

        reply_text = ", ".join(reply_parts[:-1]) + " and " + reply_parts[-1] if len(reply_parts) > 1 else reply_parts[0]
        return jsonify({"action": "multi_remove", "items": removed_items,
                        "reply": f"Done! I've removed the {reply_text} from your tray."})

    elif added_items:
        reply_parts = []
        for i in added_items:
            i_qty = i['quantity']
            qty_word = NUM_TO_WORD.get(i_qty, str(i_qty))
            i_name = i['item']['name'].lower()

            size_str = ""
            other_mods = []
            for m in i['modifiers']:
                if any(s in m for s in ["Small", "Medium", "Large"]):
                    size_str = m.split(" ")[0].lower()
                else:
                    other_mods.append(m.lower())

            if i['item']['has_size'] or i['item']['has_milk'] or i['item']['has_sweetness']:
                base_str = f"{qty_word} {size_str} {i_name}".strip()
                if other_mods:
                    mods_str = ", ".join(other_mods[:-1]) + ", and " + other_mods[-1] if len(other_mods) > 1 else \
                        other_mods[0]
                    reply_parts.append(f"{base_str} with {mods_str}")
                else:
                    reply_parts.append(base_str)
            else:
                order_word = "orders" if i_qty > 1 else "order"
                reply_parts.append(f"{qty_word} {order_word} of {i_name}")

        reply_text = ", ".join(reply_parts[:-1]) + " and " + reply_parts[-1] if len(reply_parts) > 1 else reply_parts[0]
        final_reply = f"Got it! I've added {reply_text} to your tray. "
        if unavailable_requested:
            final_reply += f"Just to let you know, {', '.join(unavailable_requested)} is temporarily out of stock right now."

        return jsonify({"action": "multi_add", "items": added_items, "reply": final_reply})

    elif unavailable_requested:
        return jsonify({"action": "none",
                        "reply": f"Sorry about that, the {', '.join(unavailable_requested)} is temporarily unavailable at the moment."})

    generic_drinks = ["latte", "americano", "macchiato", "mocha", "cappuccino", "matcha"]
    if any(drink in text for drink in generic_drinks):
        return jsonify({"action": "none", "reply": "Would you like that drink Hot or Iced? Just let me know!"})

    # 3. Last Resort Standalone Item Fallback (Stricter Bounds Applied)
    if not added_items and not removed_items:
        clean_text = re.sub(r'[^\w\s]', '', text)
        best_item, best_phrase, ratio = get_best_fuzzy_match(clean_text, all_items)

        if ratio >= 0.75 and best_item:
            qty = 1
            pattern = r'\b(\d+|' + '|'.join(WORD_TO_NUM.keys()) + r')\b(?:\s+\w+){0,4}\s*' + re.escape(best_phrase)
            match = re.search(pattern, clean_text)
            if match:
                q_str = match.group(1)
                qty = int(q_str) if q_str.isdigit() else WORD_TO_NUM.get(q_str, 1)

            qty_word = NUM_TO_WORD.get(qty, str(qty))
            i_name = best_item['name'].lower()

            if is_remove:
                removed_items.append({"item": best_item, "quantity": qty})
                order_word = "orders" if qty > 1 else "order"
                reply_text = f"{qty_word} {i_name}" if best_item['has_size'] else f"{qty_word} {order_word} of {i_name}"
                return jsonify({
                    "action": "multi_remove",
                    "items": removed_items,
                    "reply": f"Done! I've removed the {reply_text} from your tray."
                })
            else:
                if not best_item['is_available']:
                    return jsonify({"action": "none",
                                    "reply": f"I believe you meant {best_item['name']}, but unfortunately it's out of stock right now."})

                # Safe Extraction check for isolated context matches
                local_size = None
                for sz_key, sz_data in sizes.items():
                    if re.search(r'\b' + sz_key + r'\b', text):
                        local_size = sz_data
                        break
                local_milk = None
                for m, p in milk_options.items():
                    if m in text:
                        local_milk = {"label": m.title(), "price": p}
                        break
                local_sweetness = None
                for s, label in sweetness_options.items():
                    if s in text:
                        local_sweetness = label
                        break
                local_shots = None
                shot_match = re.search(shot_pattern, text)
                if shot_match:
                    qty_str = shot_match.group(1)
                    shot_qty = int(qty_str) if qty_str and qty_str.isdigit() else (
                        WORD_TO_NUM.get(qty_str, 1) if qty_str else 1)
                    local_shots = {"qty": shot_qty, "price": shot_qty * config.shot_price,
                                   "label": f"{shot_qty} extra shot{'s' if shot_qty > 1 else ''}"}

                unit_price = best_item['price']
                item_mods = []
                size_str = ""
                other_mods = []

                if best_item['has_size']:
                    if local_size:
                        unit_price += local_size['price']
                        item_mods.append(local_size['label'])
                        size_str = local_size['label'].split(" ")[0].lower()
                    else:
                        unit_price += config.medium_price
                        item_mods.append(f"Medium ({config.medium_oz})")
                        size_str = "medium"

                if best_item['has_milk'] and local_milk and local_milk['label'] != 'Whole Milk':
                    unit_price += local_milk['price']
                    item_mods.append(local_milk['label'])

                if best_item['has_sweetness'] and local_sweetness and local_sweetness != '100% Sugar':
                    item_mods.append(local_sweetness)

                if best_item['has_addons'] and local_shots:
                    unit_price += local_shots['price']
                    item_mods.append(local_shots['label'])

                added_items.append(
                    {"item": best_item, "quantity": qty, "unit_price": unit_price, "modifiers": item_mods})

                for m in item_mods:
                    if not any(s in m for s in ["Small", "Medium", "Large"]):
                        other_mods.append(m.lower())

                if best_item['has_size'] or best_item['has_milk']:
                    base_str = f"{qty_word} {size_str} {i_name}".strip()
                    if other_mods:
                        mods_str = ", ".join(other_mods[:-1]) + ", and " + other_mods[-1] if len(other_mods) > 1 else \
                            other_mods[0]
                        reply_text = f"{base_str} with {mods_str}"
                    else:
                        reply_text = base_str
                else:
                    order_word = "orders" if qty > 1 else "order"
                    reply_text = f"{qty_word} {order_word} of {i_name}"

                return jsonify({
                    "action": "multi_add",
                    "items": added_items,
                    "reply": f"Got it! I've added {reply_text} to your tray."
                })
        else:
            if is_asking_avail or any(word in text for word in buy_keywords):
                return jsonify({"action": "none", "reply": generic_not_found_reply})

    return jsonify(
        {"action": "none", "reply": "Hmm, I didn't quite catch that order. Could you repeat that one more time?"})


@app.route('/save-order', methods=['POST'])
def save_order():
    try:
        data = request.json
        tid = str(uuid.uuid4())[:8].upper()

        new_order = Order(
            id=tid,
            total_price=data['total'],
            payment_method=data['payment'],
            order_type=data.get('order_type', 'Dine-in')
        )
        db.session.add(new_order)

        for cart_item in data['cart']:
            product = Product.query.filter_by(name=cart_item['name']).first()
            p_id = product.id if product else None

            db.session.add(OrderItem(
                order_id=tid,
                product_id=p_id,
                name=cart_item['name'],
                quantity=cart_item['quantity'],
                price=cart_item['price'],
                modifiers=", ".join(cart_item.get('modifiers', []))
            ))

        db.session.commit()
        return jsonify({"success": True, "tracking_id": tid})
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/orders/<oid>/void', methods=['POST'])
def void_order(oid):
    if not session.get('admin_auth'):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json or {}
    reason = data.get('reason', 'No reason provided')

    order = Order.query.get_or_404(oid)
    order.status = 'Voided'
    order.void_reason = reason
    db.session.commit()
    return jsonify({"success": True})


@app.route('/api/sales')
def api_sales():
    if not session.get('admin_auth'):
        return jsonify({"error": "Unauthorized"}), 401

    date_filter = request.args.get('date')

    # Query all orders to track both valid and voided for comprehensive hourly charts
    query = Order.query
    if date_filter:
        try:
            start_date = datetime.strptime(date_filter, '%Y-%m-%d')
            end_date = start_date.replace(hour=23, minute=59, second=59)
            query = query.filter(Order.timestamp >= start_date, Order.timestamp <= end_date)
        except ValueError:
            pass

    orders = query.order_by(Order.timestamp.desc()).all()

    total_sales = 0
    total_valid_orders = 0
    items_sold = {}
    hourly_data = {}
    transactions = []

    for o in orders:
        hour_str = o.timestamp.strftime('%H:00')
        if hour_str not in hourly_data:
            hourly_data[hour_str] = {"order_count": 0, "void_count": 0, "items_sold": {}, "void_reasons": {}}

        # Categorize metrics depending on the order status
        if o.status != 'Voided':
            total_sales += o.total_price
            total_valid_orders += 1
            hourly_data[hour_str]["order_count"] += 1
            for item in o.items:
                items_sold[item.name] = items_sold.get(item.name, 0) + item.quantity
                hourly_data[hour_str]["items_sold"][item.name] = hourly_data[hour_str]["items_sold"].get(item.name,
                                                                                                         0) + item.quantity
        else:
            hourly_data[hour_str]["void_count"] += 1
            reason = o.void_reason or "Unknown Reason"
            hourly_data[hour_str]["void_reasons"][reason] = hourly_data[hour_str]["void_reasons"].get(reason, 0) + 1

        cart_items = [{"name": item.name, "quantity": item.quantity, "price": item.price, "modifiers": item.modifiers}
                      for item in o.items]
        transactions.append({
            "id": o.id,
            "time": o.timestamp.strftime('%I:%M %p'),
            "type": o.order_type,
            "payment": o.payment_method,
            "total": o.total_price,
            "status": o.status,
            "void_reason": o.void_reason,
            "items": cart_items
        })

    return jsonify({
        "total_sales": total_sales,
        "total_valid_orders": total_valid_orders,
        "items_sold": items_sold,
        "hourly_data": hourly_data,
        "transactions": transactions
    })


# --- AI Reporting & CSV Exports ---
@app.route('/api/ai-report', methods=['GET'])
def generate_ai_report():
    if not session.get('admin_auth'):
        return jsonify({"error": "Unauthorized"}), 401

    timeframe = request.args.get('timeframe', 'day')
    date_str = request.args.get('date')

    if date_str:
        try:
            base_date = datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            base_date = get_pht()
    else:
        base_date = get_pht()

    end_date = base_date.replace(hour=23, minute=59, second=59)

    # Establish bounds for current and previous tracking blocks
    if timeframe == 'week':
        start_date = (base_date - timedelta(days=6)).replace(hour=0, minute=0, second=0)
        prev_start = start_date - timedelta(days=7)
    elif timeframe == 'month':
        start_date = (base_date - timedelta(days=29)).replace(hour=0, minute=0, second=0)
        prev_start = start_date - timedelta(days=30)
    else:  # day
        start_date = base_date.replace(hour=0, minute=0, second=0)
        prev_start = start_date - timedelta(days=1)

    prev_end = start_date - timedelta(seconds=1)

    # Fetch Prior Frame Data to Calculate Sales Percentage Gaps
    prev_orders = Order.query.filter(
        Order.timestamp >= prev_start,
        Order.timestamp <= prev_end,
        Order.status != 'Voided'
    ).all()
    prev_sales = sum(o.total_price for o in prev_orders)

    orders = Order.query.filter(
        Order.timestamp >= start_date,
        Order.timestamp <= end_date
    ).all()

    total_sales = 0
    total_products_sold = 0
    items_sold = {}
    hourly_data = {}
    void_reasons_count = {}
    total_voids = 0

    for o in orders:
        hour = o.timestamp.strftime('%H:00')

        if o.status != 'Voided':
            total_sales += o.total_price
            hourly_data[hour] = hourly_data.get(hour, 0) + 1
            for item in o.items:
                items_sold[item.name] = items_sold.get(item.name, 0) + item.quantity
                total_products_sold += item.quantity
        else:
            total_voids += 1
            reason = o.void_reason or "Unknown"
            void_reasons_count[reason] = void_reasons_count.get(reason, 0) + 1

    # Format Chronological Peak Hours & Gather Order Volumes
    sorted_hours = sorted(hourly_data.items(), key=lambda x: x[1], reverse=True)
    top_hours = [h[0] for h in sorted_hours[:5]] if sorted_hours else []
    peak_hours = sorted(top_hours)

    total_peak_vol = sum(hourly_data[h] for h in peak_hours) if peak_hours else 0
    avg_peak_vol = total_peak_vol // len(peak_hours) if peak_hours else 0

    sorted_items = sorted(items_sold.items(), key=lambda x: x[1], reverse=True)
    top_items = [i[0] for i in sorted_items[:3]] if sorted_items else []

    top_void_reason = max(void_reasons_count, key=void_reasons_count.get) if void_reasons_count else "None"

    if prev_sales > 0:
        sales_gap_pct = ((total_sales - prev_sales) / prev_sales) * 100
    else:
        sales_gap_pct = 100.0 if total_sales > 0 else 0.0

    # AI Generation Logic
    api_key = os.environ.get('GEMINI_API_KEY')
    ai_recommendation = ""

    if not orders:
        ai_recommendation = "No sales data found for the selected timeframe. Ensure your store is operating or adjust the date filter."
    else:
        context_prompt = f"""
        You are an AI business analyst for a coffee shop. 
        Analyze this data and provide a highly detailed, varied, and effective action plan. Do NOT use markdown bold/italics excessively, keep it readable.

        DATA POINTS:
        - Timeframe: {timeframe}
        - Current Sales: ₱{total_sales}
        - Previous Timeframe Sales: ₱{prev_sales}
        - Sales Growth/Decline: {sales_gap_pct:.2f}%
        - Total Products Sold: {total_products_sold}
        - Top Items: {', '.join(top_items)}
        - Peak Hours (Chronological): {', '.join(peak_hours)}
        - Average Transactions During Peak: {avg_peak_vol}
        - Total Voided Orders: {total_voids}
        - Top Void Reason: {top_void_reason}

        REQUIRED OUTPUT FORMAT (Must address all 4 points clearly):
        1. Sales Comparison: State the percentage gap compared to the previous timeframe and briefly analyze this trend.
        2. Peak Hours & Manpower: List the peak hours chronologically. Based on the transaction volume ({avg_peak_vol} avg orders per peak hour), recommend exactly how many staff/baristas are needed to survive these surges.
        3. Void Mitigation: Since "{top_void_reason}" is the highest cause of voids, provide a specific, actionable, and effective plan to prevent this and recover lost revenue.
        4. Upselling Tactics: Provide creative, diverse tricks and tactics for staff to effectively upsell add-ons, sizes, or pairings based on the top items.
        """
        try:
            if api_key:
                genai.configure(api_key=api_key)
                model = genai.GenerativeModel('gemini-pro')
                response = model.generate_content(context_prompt)
                ai_recommendation = response.text
            else:
                raise Exception("No API Key Provided")
        except Exception:
            # Smart Fallback Expert System if Gemini is Unavailable
            dir_text = "an increase" if sales_gap_pct >= 0 else "a decrease"
            ai_recommendation = f"1. Sales Comparison: Current sales show {dir_text} of {abs(sales_gap_pct):.2f}% compared to the previous {timeframe}.\n\n"
            ai_recommendation += f"2. Peak Hours & Manpower: Your peak hours occur chronologically at {', '.join(peak_hours)}. With an average volume of {avg_peak_vol} orders per peak hour, we recommend assigning at least 2-3 staff members during these specific blocks.\n\n"
            ai_recommendation += f"3. Void Mitigation: You had {total_voids} voided orders primarily due to '{top_void_reason}'. To mitigate this, implement a double-check validation step at the POS and train staff to confirm orders verbally before final punch-in.\n\n"
            ai_recommendation += f"4. Upselling Tactics: Capitalize on top items like {', '.join(top_items)} by offering bundle pairings (e.g., 'Would you like a pastry with that?') or suggesting size upgrades and extra espresso shots to boost average order value."

    return jsonify({
        "timeframe": timeframe,
        "start_date": start_date.strftime('%Y-%m-%d'),
        "end_date": end_date.strftime('%Y-%m-%d'),
        "total_sales": total_sales,
        "total_products": total_products_sold,
        "peak_hours": peak_hours,
        "ai_recommendation": ai_recommendation,
        "items_sold": dict(sorted_items)
    })


@app.route('/api/export-report', methods=['GET'])
def export_report():
    if not session.get('admin_auth'):
        return "Unauthorized", 401

    timeframe = request.args.get('timeframe', 'day')
    date_str = request.args.get('date')
    if date_str:
        try:
            base_date = datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            base_date = get_pht()
    else:
        base_date = get_pht()

    end_date = base_date.replace(hour=23, minute=59, second=59)

    if timeframe == 'week':
        start_date = (base_date - timedelta(days=6)).replace(hour=0, minute=0, second=0)
    elif timeframe == 'month':
        start_date = (base_date - timedelta(days=29)).replace(hour=0, minute=0, second=0)
    else:  # day
        start_date = base_date.replace(hour=0, minute=0, second=0)

    orders = Order.query.filter(
        Order.timestamp >= start_date,
        Order.timestamp <= end_date
    ).all()

    si = StringIO()
    cw = csv.writer(si)

    # Export Headers & Metadata
    cw.writerow(['Pour Decision - AI Sales Report'])
    cw.writerow(['Timeframe:', timeframe.capitalize()])
    cw.writerow(['Date Range:', f"{start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}"])
    cw.writerow([])

    valid_orders = [o for o in orders if o.status != 'Voided']
    total_sales = sum(o.total_price for o in valid_orders)
    total_products = sum(sum(i.quantity for i in o.items) for o in valid_orders)
    cw.writerow(['Summary Overview'])
    cw.writerow(['Total Sales (PHP):', total_sales])
    cw.writerow(['Total Valid Orders:', len(valid_orders)])
    cw.writerow(['Total Voided Orders:', len(orders) - len(valid_orders)])
    cw.writerow(['Total Products Sold:', total_products])
    cw.writerow([])

    cw.writerow(
        ['Transaction ID', 'Date', 'Time', 'Order Type', 'Payment Method', 'Status', 'Void Reason', 'Total Price',
         'Items Ordered'])
    for o in orders:
        items_str = " | ".join(
            [f"{i.quantity}x {i.name} ({i.modifiers})" if i.modifiers else f"{i.quantity}x {i.name}" for i in o.items])
        cw.writerow([
            o.id,
            o.timestamp.strftime('%Y-%m-%d'),
            o.timestamp.strftime('%I:%M %p'),
            o.order_type,
            o.payment_method,
            o.status,
            o.void_reason or "N/A",
            o.total_price,
            items_str
        ])

    output = si.getvalue()
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=PourDecision_Sales_{timeframe}.csv"}
    )


if __name__ == '__main__':
    app.run(debug=True, threaded=True)