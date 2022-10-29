import os
import json
import asyncio
from cs50 import SQL
from flask import Flask, flash, redirect, render_template, request, session
from flask_session import Session
from tempfile import mkdtemp
from werkzeug.security import check_password_hash, generate_password_hash

from helpers import apology, login_required, lookup, usd, get_api_key, batch_lookup

# Configure application
app = Flask(__name__)

# Ensure templates are auto-reloaded
app.config["TEMPLATES_AUTO_RELOAD"] = True

# Custom filter
app.jinja_env.filters["usd"] = usd

# Configure session to use filesystem (instead of signed cookies)
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_TYPE"] = "filesystem"
Session(app)

# Configure CS50 Library to use SQLite database
db = SQL("sqlite:///finance.db")

# Make sure API key is set
if not get_api_key():
    raise RuntimeError("API_KEY not set")
else:
    os.environ["API_KEY"] = get_api_key()


@app.after_request
def after_request(response):
    """Ensure responses aren't cached"""
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Expires"] = 0
    response.headers["Pragma"] = "no-cache"
    return response


def get_user_stocks(user_id: int):
    """ Get all stocks owned by a user """
    stocks = db.execute("SELECT portfolio FROM active_stocks WHERE user_id = ?", user_id)

    # add the user to the table if they're not in it already
    if stocks is None or len(stocks) == 0:
        db.execute("INSERT INTO active_stocks (user_id, portfolio) VALUES (:user_id, :portfolio)",
                   user_id=user_id, portfolio=json.dumps({}))
        stocks = [{"portfolio": "{}"}]  # prevent error on line 55 by using this placeholder

    # get rid of every field except for stock-fields
    return json.loads(stocks[0].get("portfolio"))


async def add_to_companies_table(data):
    """ Add company & ticker to 'companies' table """
    ticker = data.get("symbol")
    name = data.get("name")

    # check if the stock is already in the table
    if not len(db.execute(f"SELECT name FROM companies WHERE ticker = '{ticker}'")):
        # add to collection
        db.execute(f"INSERT INTO companies (ticker, name) VALUES (:ticker, :name)", ticker=ticker, name=name)


@app.route("/")
@login_required
def index():
    """Show portfolio of stocks"""
    stocks = get_user_stocks(session["user_id"])
    stocks_formatted = []

    # Get company names
    names_to_query = ", ".join(f"'{ticker}'" for ticker in stocks.keys())
    _names = db.execute(f"SELECT * FROM companies WHERE ticker IN ({names_to_query})")
    names = {}
    for name in _names:
        names[name.get("ticker")] = name.get("name")

    # Get stock prices
    quotes = batch_lookup([name.get("ticker") for name in _names])

    if quotes is None:
        quotes = []

    prices = {}
    records = {}
    changes = {}
    for quote in quotes:
        ticker = quote.get("ticker")
        prices[ticker] = quote.get("price")
        records[ticker] = {}
        records[ticker]["high"] = quote.get("high")
        records[ticker]["low"] = quote.get("low")
        records[ticker]["yearly_high"] = quote.get("yearly_high")
        records[ticker]["yearly_low"] = quote.get("yearly_low")
        changes[ticker] = {}
        changes[ticker]["amount"] = quote.get("change")
        changes[ticker]["percent"] = quote.get("percent_change")

    for ticker, shares in stocks.items():
        change_is_pos = "True"
        if changes.get(ticker).get("amount") < 0:
            change_is_pos = "False"
        elif changes.get(ticker).get("amount") == 0:
            change_is_pos = "None"

        stocks_formatted.append({
            "symbol": ticker,
            "company": names.get(ticker),
            "high": records.get(ticker).get("high"),
            "low": records.get(ticker).get("low"),
            "yearly_high": records.get(ticker).get("yearly_high"),
            "yearly_low": records.get(ticker).get("yearly_low"),
            "change": changes.get(ticker).get("amount"),
            "change_is_pos": change_is_pos,
            "change_percent": f'{changes.get(ticker).get("percent")}%',
            "price": usd(float(prices.get(ticker))),
            "shares": shares,
            "value": usd(prices.get(ticker) * shares)
        })
        prices[ticker] = usd(prices[ticker])

    return render_template("index.html", stocks=stocks_formatted)


@app.route("/buy", methods=["GET", "POST"])
@login_required
def buy():
    """Buy shares of stock"""
    if request.method == "POST":
        symbol = request.form.get("symbol")
        shares = request.form.get("shares")  # value will always be a number because of the js in the html
        NAVBAR_SIZE = request.form.get("navbar_size")

        if not symbol:
            return apology("Symbol field cannot be blank")

        if not shares:
            return apology("Shares field cannot be blank")

        if not shares.isnumeric() and not isinstance(shares, int):
            return apology("Shares to buy must be a whole number")

        shares = int(shares)
        data = lookup(symbol)

        if data is None:
            return apology(f"'{symbol}' does not exist")

        asyncio.run(add_to_companies_table(data))  # non-blocking
        price = data.get("price") * shares
        balance = db.execute("SELECT cash FROM users WHERE id = ?", session["user_id"])[0].get("cash")

        if balance < price:
            return apology(f"Too poor! Bal: {usd(balance)}\nPrice: {usd(price)}")

        active_stocks = get_user_stocks(session["user_id"])

        if symbol in active_stocks.keys():
            active_stocks[symbol] += shares
        else:
            active_stocks[symbol] = shares

        # Process transaction
        db.execute("UPDATE users SET cash=:cash WHERE id = :id", cash=(balance - price), id=session["user_id"])
        db.execute("UPDATE active_stocks SET portfolio=:updated_active_stocks WHERE user_id = :id",
                   updated_active_stocks=json.dumps(active_stocks), id=session["user_id"])

        # Log transaction
        db.execute("INSERT INTO history (stock, shares, price, user_id, type) VALUES (:stock, :shares, :price, :user_id, :type)",
                   stock=symbol,
                   shares=shares,
                   price=usd(price),
                   user_id=session["user_id"],
                   type="buy"
                   )

        return render_template("buy_w_toast.html", shares=shares, symbol=symbol, price=usd(price), new_balance=usd(balance - price), price_individiual=usd(data.get("price")), updated_stock_amount=active_stocks[symbol], NAVBAR_SIZE=NAVBAR_SIZE)

    return render_template("buy.html")


@app.route("/history")
@login_required
def history():
    """Show history of transactions"""
    transactions_formatted = []

    # Ex. {'id': 126, 'stock': 'AAPL', 'shares': 1, 'price': '$149.35', 'Timestamp': '2022-10-27 04:17:17', 'user_id': 4, 'type': 'buy'}
    transactions = db.execute(f"SELECT * FROM history WHERE user_id = {session['user_id']}")

    # Get company names
    names_to_query = ", ".join(f"'{ticker}'" for ticker in set([t.get("stock") for t in transactions]))
    _names = db.execute(f"SELECT * FROM companies WHERE ticker IN ({names_to_query})")
    names = {}
    for name in _names:
        names[name.get("ticker")] = name.get("name")

    for transaction in transactions:
        transactions_formatted.append({
            "stock": transaction.get("stock"),
            "company": names.get(transaction.get("stock")),
            "date": transaction.get("Timestamp").split(" ")[0],
            "time": transaction.get("Timestamp").split(" ")[1],
            "shares": transaction.get("shares"),
            "value": transaction.get("price"),  # shares * price_at_purchase
            "type": transaction.get("type")
        })

    return render_template("history.html", transactions=transactions_formatted)


@app.route("/login", methods=["GET", "POST"])
def login():
    """Log user in"""

    # Forget any user_id
    session.clear()

    # User reached route via POST (as by submitting a form via POST)
    if request.method == "POST":

        # Ensure username was submitted
        if not request.form.get("username"):
            return apology("must provide username", 403)

        # Ensure password was submitted
        elif not request.form.get("password"):
            return apology("must provide password", 403)

        # Query database for username
        rows = db.execute("SELECT * FROM users WHERE username = ?", request.form.get("username"))

        # Ensure username exists and password is correct
        if len(rows) != 1 or not check_password_hash(rows[0]["hash"], request.form.get("password")):
            return apology("invalid username and/or password", 403)

        # Remember which user has logged in
        session["user_id"] = rows[0]["id"]

        # Redirect user to home page
        return redirect("/")

    # User reached route via GET (as by clicking a link or via redirect)
    else:
        return render_template("login.html")


@app.route("/logout")
def logout():
    """Log user out"""

    # Forget any user_id
    session.clear()

    # Redirect user to login form
    return redirect("/")


@app.route("/quote", methods=["GET", "POST"])
@login_required
def quote():
    """Get stock quote."""
    if request.method == "POST":
        symbol = request.form.get("symbol")
        data = lookup(symbol)

        if data is None:
            return apology(f"'{symbol}' does not exist")

        asyncio.run(add_to_companies_table(data))  # non-blocking

        # Example: {'name': 'Apple Inc', 'price': 143.75, 'symbol': 'AAPL'}
        return render_template("quoted.html", company=data.get("name"), price=usd(data.get("price")), symbol=data.get("symbol"))

    return render_template("quote.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    """Register user"""
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        confirmation = request.form.get("confirmation")

        if not username:
            return apology("Username field cannot be blank")

        users = db.execute("SELECT * FROM users WHERE username = ?", username)
        if len(users):
            return apology("Username already exists")

        if not password:
            return apology("Password field cannot be blank")

        if not confirmation:
            return apology("Confirm password field cannot be blank")

        if len(password) < 8:
            return apology("Password must be at least 8 characters long")

        if password != confirmation:
            return apology("Passwords do not match")

        # ID and cash fields will be automatically added
        db.execute(f"INSERT INTO users (username, hash) VALUES (:username, :password)",
                   username=username, password=generate_password_hash(password))

        # Log the user in
        rows = db.execute("SELECT * FROM users WHERE username = ?", username)
        session["user_id"] = rows[0]["id"]
        return redirect("/")

    return render_template("register.html")


@app.route("/sell", methods=["GET", "POST"])
@login_required
def sell():
    """Sell shares of stock"""
    active_stocks = get_user_stocks(session["user_id"])

    if request.method == "POST":
        symbol = request.form.get("symbol")
        shares = request.form.get("shares")  # value comping will always be a number because of the js in the html
        NAVBAR_SIZE = request.form.get("navbar_size")

        if not symbol:
            return apology("Symbol field cannot be blank")

        if not shares:
            return apology("Shares field cannot be blank")

        if not shares.isnumeric() and not isinstance(shares, int):
            return apology("Shares to buy must be a whole number")

        shares = int(shares)
        data = lookup(symbol)

        if data is None:
            return apology(f"'{symbol}' does not exist")

        price = data.get("price") * shares
        balance = db.execute("SELECT cash FROM users WHERE id = ?", session["user_id"])[0].get("cash")

        # the user does not have any of this stock
        if symbol not in active_stocks.keys():
            return apology(f"Missing stocks! Have: 0\nSelling: {shares}")

        # trying to sell more than they have
        if shares > active_stocks[symbol]:
            return apology(f"Missing stocks! Have: {active_stocks[symbol]}\nSelling: {shares}")

        # "sell" the socks before updating the table
        active_stocks[symbol] -= shares

        # Process transaction
        db.execute("UPDATE users SET cash=:cash WHERE id = :id", cash=(balance + price), id=session["user_id"])
        db.execute("UPDATE active_stocks SET portfolio=:updated_active_stocks WHERE user_id = :id",
                   updated_active_stocks=json.dumps(active_stocks), id=session["user_id"])

        # Log transaction
        db.execute("INSERT INTO history (stock, shares, price, user_id, type) VALUES (:stock, :shares, :price, :user_id, :type)",
                   stock=symbol,
                   shares=shares,
                   price=usd(price),
                   user_id=session["user_id"],
                   type="sell"
                   )

        return render_template("sell_w_toast.html", active_stocks=[s for s in active_stocks.keys() if active_stocks[s] != 0], shares=shares, symbol=symbol, price=usd(price), new_balance=usd(balance + price), price_individiual=usd(data.get("price")), updated_stock_amount=active_stocks[symbol], NAVBAR_SIZE=NAVBAR_SIZE)

    return render_template("sell.html", active_stocks=[s for s in active_stocks.keys() if active_stocks[s] != 0])
