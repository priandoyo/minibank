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
    :root { color-scheme: light; --ink:#182230; --muted:#667085; --line:#e4e7ec; --blue:#175cd3; --bg:#f6f8fb; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif; background:var(--bg); color:var(--ink); }
    header { background:#102a56; color:white; padding:25px max(20px,calc((100% - 1180px)/2)); }
    header h1 { margin:0 0 5px; font-size:26px; } header p { margin:0; color:#dbeafe; }
    main { max-width:1180px; margin:24px auto; padding:0 18px 40px; }
    .grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:18px; }
    .card { background:white; border:1px solid var(--line); border-radius:14px; padding:20px; box-shadow:0 2px 5px #10182808; margin-bottom:18px; }
    h2 { font-size:18px; margin:0 0 15px; } h3 { font-size:15px; margin:0 0 10px; }
    .sub { color:var(--muted); font-size:13px; margin-top:-8px; margin-bottom:14px; }
    table { width:100%; border-collapse:collapse; font-size:14px; }
    th,td { text-align:left; padding:11px 9px; border-bottom:1px solid #eef0f3; vertical-align:top; }
    th { color:var(--muted); font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.03em; }
    td.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
    .table-wrap { overflow-x:auto; }
    label { display:block; font-size:13px; font-weight:600; margin:12px 0 6px; }
    input,select { width:100%; padding:10px 11px; border:1px solid #d0d5dd; border-radius:8px; background:white; font:inherit; }
    button { margin-top:15px; border:0; border-radius:8px; background:var(--blue); color:white; padding:10px 15px; font-weight:650; cursor:pointer; }
    button:hover { background:#1849a9; }
    .flash { padding:11px 13px; border-radius:8px; margin-bottom:14px; font-size:14px; }
    .success { background:#ecfdf3; color:#027a48; } .error { background:#fef3f2; color:#b42318; }
    code { background:#f2f4f7; padding:3px 5px; border-radius:4px; font-size:12px; overflow-wrap:anywhere; }
    .method { display:inline-block; min-width:46px; padding:3px 6px; border-radius:5px; background:#eff8ff; color:#175cd3; font-size:11px; font-weight:700; text-align:center; }
    .note { color:var(--muted); font-size:12px; margin-top:12px; }
    @media(max-width:760px) { .grid { grid-template-columns:1fr; } header { padding:22px 18px; } main { margin-top:16px; } }
  </style>
</head>
<body>
<header>
  <h1>MiniBank-ABC</h1>
  <p>Simple banking demo · Flask REST API · SQLite</p>
</header>
<main>
  {% with messages = get_flashed_messages(with_categories=true) %}
    {% for category, message in messages %}<div class="flash {{ category }}">{{ message }}</div>{% endfor %}
  {% endwith %}

  <section class="card">
    <h2>Accounts</h2>
    <p class="sub">Demo accounts and current balances (illustrative amounts, not real money).</p>
    <div class="table-wrap"><table>
      <thead><tr><th>Account number</th><th>Customer name</th><th style="text-align:right">Balance</th></tr></thead>
      <tbody>
      {% for account in accounts %}
        <tr><td>{{ account.account_number }}</td><td>{{ account.customer_name }}</td><td class="num">{{ "{:,.2f}".format(account.balance) }}</td></tr>
      {% else %}<tr><td colspan="3">No accounts found.</td></tr>{% endfor %}
      </tbody>
    </table></div>
  </section>

  <div class="grid">
    <section class="card">
      <h2>Deposit</h2>
      <form method="post" action="/deposit">
        <label for="deposit-account">Account</label>
        <select id="deposit-account" name="account_number" required>
          {% for a in raw_accounts %}<option value="{{ a.account_number }}">{{ a.account_number }} — {{ a.customer_name }}</option>{% endfor %}
        </select>
        <label for="deposit-amount">Amount</label>
        <input id="deposit-amount" name="amount" type="number" min="0.01" step="0.01" placeholder="e.g. 100000" required>
        <button type="submit">Deposit</button>
      </form>
    </section>

    <section class="card">
      <h2>Transfer</h2>
      <form method="post" action="/transfer">
        <label for="from-account">From account</label>
        <select id="from-account" name="from_account" required>
          {% for a in raw_accounts %}<option value="{{ a.account_number }}">{{ a.account_number }} — {{ a.customer_name }}</option>{% endfor %}
        </select>
        <label for="to-account">To account</label>
        <select id="to-account" name="to_account" required>
          {% for a in raw_accounts %}<option value="{{ a.account_number }}" {% if loop.index == 2 %}selected{% endif %}>{{ a.account_number }} — {{ a.customer_name }}</option>{% endfor %}
        </select>
        <label for="transfer-amount">Amount</label>
        <input id="transfer-amount" name="amount" type="number" min="0.01" step="0.01" placeholder="e.g. 25000" required>
        <button type="submit">Transfer</button>
      </form>
    </section>
  </div>

  <section class="card">
    <h2>Transaction History</h2>
    <div class="table-wrap"><table>
      <thead><tr><th>Time (UTC)</th><th>Transaction type</th><th>From account</th><th>To account</th><th style="text-align:right">Amount</th></tr></thead>
      <tbody>
      {% for t in transactions %}
        <tr><td>{{ t.time }}</td><td>{{ t.transaction_type }}</td><td>{{ t.from_account or "—" }}</td><td>{{ t.to_account }}</td><td class="num">{{ "{:,.2f}".format(t.amount) }}</td></tr>
      {% else %}<tr><td colspan="5">No transactions yet.</td></tr>{% endfor %}
      </tbody>
    </table></div>
    <p class="note">Showing the latest 100 transactions. Times are stored in UTC.</p>
  </section>

  <section class="card">
    <h2>Available API Endpoints</h2>
    <p class="sub">Base URL: <code>http://127.0.0.1:5000</code></p>
    <div class="table-wrap"><table>
      <thead><tr><th>Method</th><th>Endpoint</th><th>Description</th></tr></thead>
      <tbody>{% for e in endpoints %}
        <tr><td><span class="method">{{ e.method }}</span></td><td><code>{{ e.path }}</code></td><td>{{ e.description }}</td></tr>
      {% endfor %}</tbody>
    </table></div>
    <p class="note">POST endpoints expect JSON when called as APIs. This learning demo has no login, authentication, authorization, or production-grade banking controls.</p>
  </section>
</main>
</body>
</html>
"""

init_db()

if __name__ == "__main__":
    # Local learning demo only. Do not expose this development server to the internet.
    app.run(host="0.0.0.0", port=5000, debug=False)
