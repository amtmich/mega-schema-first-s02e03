"""Persistent single-owner workspace. OAuth credentials are stored as hashes."""
import contextlib
import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.directory / 'workspace.sqlite3'
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS notes (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT UNIQUE NOT NULL, text TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY, filename TEXT NOT NULL, mime TEXT NOT NULL,
                    body BLOB NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS records (
                    kind TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                    expires REAL NOT NULL, PRIMARY KEY(kind,key));
            ''')
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def connect(self):
        with sqlite3.connect(self.path, timeout=15) as db:
            db.row_factory = sqlite3.Row
            yield db

    def put(self, kind, key, value, ttl):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO records VALUES (?,?,?,?)',
                       (kind, key, json.dumps(value), time.time() + ttl))

    def get(self, kind, key, consume=False):
        with self.connect() as db:
            if consume:
                db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT value,expires FROM records WHERE kind=? AND key=?',
                             (kind, key)).fetchone()
            if row and (consume or row['expires'] <= time.time()):
                db.execute('DELETE FROM records WHERE kind=? AND key=?', (kind, key))
            if row and row['expires'] > time.time():
                return json.loads(row['value'])
        return None

    def delete(self, kind, key):
        with self.connect() as db:
            db.execute('DELETE FROM records WHERE kind=? AND key=?', (kind, key))

    def write_note(self, text):
        note_id = secrets.token_urlsafe(18)
        with self.connect() as db:
            db.execute('INSERT INTO notes(id,text,created) VALUES (?,?,?)',
                       (note_id, text, time.time()))
        return {'id': note_id}

    def search_notes(self, query):
        # instr avoids SQL wildcard interpretation. Unicode matching is done in Python.
        with self.connect() as db:
            rows = db.execute('SELECT id,text,created FROM notes ORDER BY sequence DESC').fetchall()
        return {'notes': [dict(row) for row in rows if query.casefold() in row['text'].casefold()][:100]}

    def save_file(self, filename, mime, body):
        file_id = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('INSERT INTO files VALUES (?,?,?,?,?)',
                       (file_id, filename, mime, body, time.time()))
        return file_id

    def file(self, file_id):
        with self.connect() as db:
            row = db.execute('SELECT filename,mime,body FROM files WHERE id=?', (file_id,)).fetchone()
        return dict(row) if row else None

    def prune(self):
        with self.connect() as db:
            db.execute('DELETE FROM records WHERE expires <= ?', (time.time(),))
