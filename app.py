from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import os
import sqlite3

from flask import Flask, jsonify, render_template_string, request, redirect, url_for, flash

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("MINIBANK_DB", os.path.join(APP_DIR, "minibank-abc.db"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "development-only-change-me")


def connect_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with connect_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            account_number TEXT PRIMARY KEY,
            customer_name TEXT NOT NULL,
            balance_cents INTEGER NOT NULL DEFAULT 0 CHECK (balance_cents >= 0)
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_time TEXT NOT NULL,
            transaction_type TEXT NOT NULL CHECK (transaction_type IN ('DEPOSIT', 'TRANSFER')),
            from_account TEXT,
            to_account TEXT NOT NULL,
            amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
            FOREIGN KEY (from_account) REFERENCES accounts(account_number),
            FOREIGN KEY (to_account) REFERENCES accounts(account_number)
        );
        """)
        count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        if count == 0:
            conn.executemany(
                "INSERT INTO accounts (account_number, customer_name, balance_cents) VALUES (?, ?, ?)",
                [
                    ("100001", "Alya Pratama", 5_000_000),
                    ("100002", "Bima Santoso", 2_500_000),
                    ("100003", "Citra Wijaya", 1_000_000),
                ],
            )


def money_to_cents(value):
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Amount must be a valid number.")
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Amount must be greater than zero.")
    return int(amount * 100)


def format_money(cents):
    return f"{cents / 100:,.2f}"


def account_dict(row):
    return {
        "account_number": row["account_number"],
        "customer_name": row["customer_name"],
        "balance": round(row["balance_cents"] / 100, 2),
    }


def transaction_dict(row):
    return {
        "time": row["transaction_time"],
        "transaction_type": row["transaction_type"],
        "from_account": row["from_account"],
        "to_account": row["to_account"],
        "amount": round(row["amount_cents"] / 100, 2),
    }


def all_accounts():
    with connect_db() as conn:
        return [account_dict(row) for row in conn.execute(
            "SELECT account_number, customer_name, balance_cents FROM accounts ORDER BY account_number"
        )]


def all_transactions(limit=100):
    with connect_db() as conn:
        rows = conn.execute("""
            SELECT transaction_time, transaction_type, from_account, to_account, amount_cents
            FROM transactions ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
        return [transaction_dict(row) for row in rows]


@app.get("/")
def index():
    accounts = all_accounts()
    transactions = all_transactions()
    with connect_db() as conn:
        raw_accounts = conn.execute(
            "SELECT account_number, customer_name, balance_cents FROM accounts ORDER BY account_number"
        ).fetchall()
    return render_template_string(
        PAGE,
        accounts=accounts,
        raw_accounts=raw_accounts,
        transactions=transactions,
        endpoints=ENDPOINTS,
        format_money=format_money,
    )


@app.post("/deposit")
def deposit_form():
    try:
        account_number = request.form.get("account_number", "").strip()
        amount_cents = money_to_cents(request.form.get("amount", ""))
        perform_deposit(account_number, amount_cents)
        flash("Deposit completed.", "success")
    except (ValueError, LookupError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("index"))


@app.post("/transfer")
def transfer_form():
    try:
        from_account = request.form.get("from_account", "").strip()
        to_account = request.form.get("to_account", "").strip()
        amount_cents = money_to_cents(request.form.get("amount", ""))
        perform_transfer(from_account, to_account, amount_cents)
        flash("Transfer completed.", "success")
    except (ValueError, LookupError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("index"))


def perform_deposit(account_number, amount_cents):
    with connect_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute(
            "SELECT 1 FROM accounts WHERE account_number = ?", (account_number,)
        ).fetchone()
        if not exists:
            raise LookupError("Account not found.")
        conn.execute(
            "UPDATE accounts SET balance_cents = balance_cents + ? WHERE account_number = ?",
            (amount_cents, account_number),
        )
        conn.execute("""
            INSERT INTO transactions
            (transaction_time, transaction_type, from_account, to_account, amount_cents)
            VALUES (?, 'DEPOSIT', NULL, ?, ?)
        """, (datetime.now(timezone.utc).isoformat(timespec="seconds"), account_number, amount_cents))


def perform_transfer(from_account, to_account, amount_cents):
    if from_account == to_account:
        raise ValueError("From-account and to-account must be different.")
    with connect_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        accounts = conn.execute(
            "SELECT account_number, balance_cents FROM accounts WHERE account_number IN (?, ?)",
            (from_account, to_account),
        ).fetchall()
        found = {row["account_number"]: row["balance_cents"] for row in accounts}
        if from_account not in found or to_account not in found:
            raise LookupError("Both accounts must exist.")
        if found[from_account] < amount_cents:
            raise ValueError("Insufficient balance.")
        conn.execute(
            "UPDATE accounts SET balance_cents = balance_cents - ? WHERE account_number = ?",
            (amount_cents, from_account),
        )
        conn.execute(
            "UPDATE accounts SET balance_cents = balance_cents + ? WHERE account_number = ?",
            (amount_cents, to_account),
        )
        conn.execute("""
            INSERT INTO transactions
            (transaction_time, transaction_type, from_account, to_account, amount_cents)
            VALUES (?, 'TRANSFER', ?, ?, ?)
        """, (datetime.now(timezone.utc).isoformat(timespec="seconds"), from_account, to_account, amount_cents))


# REST API: JSON responses; amounts are expressed in the major currency unit.
@app.get("/api")
def api_index():
    return jsonify({"service": "MiniBank-ABC", "endpoints": ENDPOINTS})


@app.get("/api/accounts")
def api_accounts():
    return jsonify({"accounts": all_accounts()})


@app.get("/api/accounts/<account_number>")
def api_account_detail(account_number):
    with connect_db() as conn:
        row = conn.execute(
            "SELECT account_number, customer_name, balance_cents FROM accounts WHERE account_number = ?",
            (account_number,),
        ).fetchone()
    if row is None:
        return jsonify({"error": "Account not found."}), 404
    return jsonify({"account": account_dict(row)})


@app.get("/api/transactions")
def api_transactions():
    try:
        limit = int(request.args.get("limit", "100"))
    except ValueError:
        return jsonify({"error": "limit must be an integer."}), 400
    if not 1 <= limit <= 500:
        return jsonify({"error": "limit must be between 1 and 500."}), 400
    return jsonify({"transactions": all_transactions(limit)})


@app.post("/api/deposit")
def api_deposit():
    data = request.get_json(silent=True) or {}
    try:
        account_number = str(data.get("account_number", "")).strip()
        amount_cents = money_to_cents(data.get("amount"))
        perform_deposit(account_number, amount_cents)
    except (ValueError, LookupError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"message": "Deposit completed.", "account_number": account_number,
                    "amount": round(amount_cents / 100, 2)}), 201


@app.post("/api/transfer")
def api_transfer():
    data = request.get_json(silent=True) or {}
    try:
        from_account = str(data.get("from_account", "")).strip()
        to_account = str(data.get("to_account", "")).strip()
        amount_cents = money_to_cents(data.get("amount"))
        perform_transfer(from_account, to_account, amount_cents)
    except (ValueError, LookupError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"message": "Transfer completed.", "from_account": from_account,
                    "to_account": to_account, "amount": round(amount_cents / 100, 2)}), 201


ENDPOINTS = [
    {"method": "GET", "path": "/", "description": "Web banking interface"},
    {"method": "GET", "path": "/api", "description": "List API endpoints"},
    {"method": "GET", "path": "/api/accounts", "description": "List all accounts"},
    {"method": "GET", "path": "/api/accounts/<account_number>", "description": "Get one account"},
    {"method": "GET", "path": "/api/transactions?limit=100", "description": "List transaction history"},
    {"method": "POST", "path": "/api/deposit", "description": "Deposit money (JSON)"},
    {"method": "POST", "path": "/api/transfer", "description": "Transfer money (JSON)"},
]

PAGE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MiniBank-ABC</title>
  <style>
    :root {
      --ink:#14213d; --muted:#53657f; --line:#d9e1ea; --blue:#0875df;
      --blue-dark:#0756a8; --green:#1f9d50; --bg:#f4f7fb; --white:#fff;
    }
    * { box-sizing:border-box; }
    body { margin:0; color:var(--ink); background:var(--bg); font-family:Inter,system-ui,-apple-system,"Segoe UI",sans-serif; }
    .topbar { display:flex; align-items:center; justify-content:space-between; gap:20px; padding:15px max(22px,calc((100% - 1450px)/2)); color:#fff; background:linear-gradient(105deg,#0871d5,#0755aa); }
    .brand { display:flex; align-items:center; gap:14px; }
    .brand-icon { font-size:36px; line-height:1; }
    .brand h1 { margin:0; font-size:25px; letter-spacing:.1px; }
    .brand p { margin:2px 0 0; color:#e6f2ff; font-size:13px; }
    nav { display:flex; gap:4px; align-self:stretch; }
    nav a { display:flex; align-items:center; gap:9px; padding:10px 15px; color:#fff; text-decoration:none; border-radius:7px; font-size:14px; }
    nav a:hover, nav a.active { background:#064a93; }
    main { max-width:1450px; margin:15px auto 35px; padding:0 22px; }
    .flash { padding:11px 14px; margin:0 0 14px; border-radius:7px; font-size:14px; border:1px solid transparent; }
    .success { color:#176b36; background:#e5f8eb; border-color:#b9e9c8; }
    .error { color:#a52222; background:#fff0f0; border-color:#f2c7c7; }
    .dashboard { display:grid; grid-template-columns:1.55fr 1fr 1.05fr; gap:15px; align-items:stretch; }
    .card { min-width:0; background:var(--white); border:1px solid var(--line); border-radius:7px; padding:16px; box-shadow:0 2px 5px rgba(20,33,61,.035); }
    .section-head { display:flex; align-items:center; gap:12px; margin:0 0 15px; }
    .section-icon { display:flex; align-items:center; justify-content:center; flex:0 0 42px; height:42px; border-radius:50%; color:white; font-size:23px; font-weight:700; background:#2688e8; }
    .deposit-card .section-icon { background:var(--green); }
    .transfer-card .section-icon { background:#087af0; }
    .history-card .section-icon { background:#4399e5; }
    .api-card .section-icon { background:transparent; color:#1680ed; font-size:34px; }
    h2 { margin:0; font-size:17px; line-height:1.3; }
    .subtitle { margin:2px 0 0; color:var(--muted); font-size:12.5px; }
    .table-wrap { width:100%; overflow-x:auto; }
    table { width:100%; border-collapse:collapse; font-size:13px; }
    th,td { padding:8px 10px; text-align:left; border:1px solid #dce3eb; white-space:nowrap; }
    th { background:#edf1f5; font-weight:700; color:#17243a; }
    td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
    form { display:flex; flex-direction:column; gap:0; }
    label { font-size:12.5px; font-weight:700; margin:12px 0 6px; }
    select,input { width:100%; min-width:0; height:37px; padding:8px 10px; border:1px solid #cbd5e1; border-radius:5px; background:#fff; color:#17243a; font:inherit; font-size:13px; }
    select:focus,input:focus { outline:2px solid #93c5fd; border-color:#2582e8; }
    button { width:100%; margin-top:17px; min-height:42px; border:0; border-radius:5px; color:#fff; font:inherit; font-size:13px; font-weight:700; cursor:pointer; background:var(--green); }
    button:hover { filter:brightness(.94); }
    .transfer-card button { background:#087af0; }
    .wide-card { margin-top:15px; }
    .tag { display:inline-block; padding:3px 8px; border-radius:12px; font-size:10.5px; font-weight:750; }
    .tag.deposit { color:#fff; background:#21a34a; }
    .tag.transfer { color:#064b94; background:#a8d7ff; }
    .method { display:inline-block; min-width:61px; padding:3px 8px; border-radius:4px; color:#fff; text-align:center; font-size:10.5px; font-weight:750; }
    .method.get { background:#087af0; } .method.post { background:#21a34a; }
    .api-base { margin:0 0 9px; padding:12px 13px; border-radius:5px; background:#e5f3ff; font-size:12px; overflow-wrap:anywhere; }
    code { font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:12px; }
    .api-base code { color:#0756a8; background:#d0e8ff; padding:3px 5px; border-radius:3px; }
    .note { color:var(--muted); font-size:11px; margin:10px 0 0; }
    @media(max-width:1100px) { .dashboard { grid-template-columns:1fr 1fr; } .accounts-card { grid-column:1 / -1; } }
    @media(max-width:700px) { .topbar { align-items:flex-start; flex-direction:column; padding:15px 18px; } nav { width:100%; } nav a { padding:8px 10px; } main { padding:0 12px; } .dashboard { grid-template-columns:1fr; } .accounts-card { grid-column:auto; } .card { padding:13px; } }
  </style>
</head>
<body>
<header class="topbar">
  <div class="brand">
    <div class="brand-icon" aria-hidden="true">🏦</div>
    <div><h1>MiniBank-ABC</h1><p>Simple Banking Web Application</p></div>
  </div>
  <nav aria-label="Main navigation">
    <a class="active" href="#home">⌂ &nbsp;Home</a>
    <a href="#api">‹/› &nbsp;API Endpoints</a>
  </nav>
</header>
<main id="home">
  {% with messages = get_flashed_messages(with_categories=true) %}
    {% for category, message in messages %}<div class="flash {{ category }}">{{ "✓ " if category == "success" else "⚠ " }}{{ message }}</div>{% endfor %}
  {% endwith %}

  <div class="dashboard">
    <section class="card accounts-card">
      <div class="section-head"><div class="section-icon">👥</div><div><h2>Accounts</h2><p class="subtitle">List of all bank accounts</p></div></div>
      <div class="table-wrap"><table>
        <thead><tr><th>Account Number</th><th>Customer Name</th><th class="num">Balance</th></tr></thead>
        <tbody>
        {% for account in accounts %}
          <tr><td>{{ account.account_number }}</td><td>{{ account.customer_name }}</td><td class="num">{{ "{:,.2f}".format(account.balance) }}</td></tr>
        {% else %}<tr><td colspan="3">No accounts found.</td></tr>{% endfor %}
        </tbody>
      </table></div>
    </section>

    <section class="card deposit-card">
      <div class="section-head"><div class="section-icon">＋</div><div><h2>Deposit</h2><p class="subtitle">Add money to an account</p></div></div>
      <form method="post" action="/deposit">
        <label for="deposit-account">Account</label>
        <select id="deposit-account" name="account_number" required>
          {% for a in raw_accounts %}<option value="{{ a.account_number }}">{{ a.account_number }} - {{ a.customer_name }}</option>{% endfor %}
        </select>
        <label for="deposit-amount">Amount</label>
        <input id="deposit-amount" name="amount" type="number" min="0.01" step="0.01" placeholder="Enter amount (e.g. 1000)" required>
        <button type="submit">▣ &nbsp; Deposit</button>
      </form>
    </section>

    <section class="card transfer-card">
      <div class="section-head"><div class="section-icon">↔</div><div><h2>Transfer</h2><p class="subtitle">Transfer money between accounts</p></div></div>
      <form method="post" action="/transfer">
        <label for="from-account">From Account</label>
        <select id="from-account" name="from_account" required>
          {% for a in raw_accounts %}<option value="{{ a.account_number }}">{{ a.account_number }} - {{ a.customer_name }}</option>{% endfor %}
        </select>
        <label for="to-account">To Account</label>
        <select id="to-account" name="to_account" required>
          {% for a in raw_accounts %}<option value="{{ a.account_number }}" {% if loop.index == 2 %}selected{% endif %}>{{ a.account_number }} - {{ a.customer_name }}</option>{% endfor %}
        </select>
        <label for="transfer-amount">Amount</label>
        <input id="transfer-amount" name="amount" type="number" min="0.01" step="0.01" placeholder="Enter amount (e.g. 1000)" required>
        <button type="submit">↔ &nbsp; Transfer</button>
      </form>
    </section>
  </div>

  <section class="card wide-card history-card">
    <div class="section-head"><div class="section-icon">◷</div><div><h2>Transaction History</h2><p class="subtitle">Recent deposits and transfers</p></div></div>
    <div class="table-wrap"><table>
      <thead><tr><th>Time (UTC)</th><th>Transaction Type</th><th>From Account</th><th>To Account</th><th class="num">Amount</th></tr></thead>
      <tbody>
      {% for t in transactions %}
        <tr><td>{{ t.time }}</td><td><span class="tag {{ 'deposit' if t.transaction_type == 'DEPOSIT' else 'transfer' }}">{{ t.transaction_type }}</span></td><td>{{ t.from_account or "-" }}</td><td>{{ t.to_account }}</td><td class="num">{{ "{:,.2f}".format(t.amount) }}</td></tr>
      {% else %}<tr><td colspan="5">No transactions yet.</td></tr>{% endfor %}
      </tbody>
    </table></div>
    <p class="note">Showing the latest 100 transactions. Times are stored in UTC.</p>
  </section>

  <section class="card wide-card" id="api">
    <div class="section-head"><div class="section-icon">‹/›</div><div><h2>API Endpoints</h2><p class="subtitle">Available REST API endpoints</p></div></div>
    <p class="api-base">API base URL: <code>{{ request.host_url }}api</code></p>
    <div class="table-wrap"><table>
      <thead><tr><th>Method</th><th>Endpoint</th><th>Description</th></tr></thead>
      <tbody>{% for e in endpoints %}
        <tr><td><span class="method {{ e.method|lower }}">{{ e.method }}</span></td><td><code>{{ e.path }}</code></td><td>{{ e.description }}</td></tr>
      {% endfor %}</tbody>
    </table></div>
    <p class="note">POST endpoints accept JSON requests. This learning demo has no login, authentication, authorization, or production-grade banking controls.</p>
  </section>
</main>
</body>
</html>
"""

init_db()

if __name__ == "__main__":
    # Local learning demo only. Do not expose this development server to the internet.
    app.run(host="0.0.0.0", port=5000, debug=False)
