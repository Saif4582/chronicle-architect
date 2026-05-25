import sqlite3

conn = sqlite3.connect('data/chronicle.db')
c = conn.cursor()

# Show current users
c.execute('SELECT id, username, role FROM users')
print('Before:', c.fetchall())

# Set first user to owner
c.execute('UPDATE users SET role = "owner" WHERE id = (SELECT MIN(id) FROM users)')
conn.commit()

# Show after
c.execute('SELECT id, username, role FROM users')
print('After:', c.fetchall())

conn.close()
