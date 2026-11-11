import sqlite3

def clean():
    conn = sqlite3.connect("data/soloearn.sqlite3")
    conn.execute("DELETE FROM financial_transactions")
    conn.execute("DELETE FROM expenses")
    conn.execute("DELETE FROM commercial_leads WHERE company_name IN ('Apex Retailers Ltd', 'Acme D2C')")
    conn.execute("DELETE FROM invoices")
    conn.execute("DELETE FROM opportunities WHERE title LIKE '%Acme D2C%'")
    conn.execute("DELETE FROM tasks WHERE title LIKE '%Pilot%'")
    conn.execute("DELETE FROM bank_statement_rows")
    conn.execute("DELETE FROM bank_statement_imports")
    conn.commit()
    conn.close()
    print("Database cleaned successfully.")

if __name__ == "__main__":
    clean()
