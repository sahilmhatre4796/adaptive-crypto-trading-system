"""Fenced, append-only SQLite facts for offline M12 execution replay."""
from pathlib import Path
import sqlite3

from trading.replay_codec import decode, encode, identity


class PaperJournalError(ValueError):
    """Durable paper state cannot be trusted or advanced."""


SCHEMA = '''
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE commands (
  seq INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, operation TEXT NOT NULL,
  payload TEXT NOT NULL, previous_hash TEXT NOT NULL, entry_hash TEXT NOT NULL
);
CREATE TABLE outcomes (
  seq INTEGER PRIMARY KEY REFERENCES commands(seq), state TEXT NOT NULL
    CHECK(state IN ('APPLIED','QUARANTINED')),
  result TEXT NOT NULL, post_hash TEXT NOT NULL, previous_hash TEXT NOT NULL,
  entry_hash TEXT NOT NULL
);
'''


class PaperJournal:
    def __init__(self, path, *, baseline_hash, code_hash, create=False):
        self.path = Path(path)
        if (create and self.path.exists()) or (not create and not self.path.is_file()):
            raise PaperJournalError('Paper journal create/recovery path is inconsistent')
        try:
            self.db = sqlite3.connect(self.path, isolation_level=None, timeout=5)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA foreign_keys=ON')
            if create:
                self.db.executescript(SCHEMA)
                self.db.execute('BEGIN IMMEDIATE')
                try:
                    for key, value in (('baseline', baseline_hash), ('code', code_hash),
                                       ('epoch', '1'), ('head', '0'), ('head_hash', '0'*64),
                                       ('applied', '0'), ('applied_hash', '0'*64)):
                        self.db.execute('INSERT INTO meta VALUES (?,?)', (key, value))
                    self.db.commit()
                except BaseException:
                    self.db.rollback()
                    raise
            self.verify()
            if self._meta('baseline') != baseline_hash or self._meta('code') != code_hash:
                raise PaperJournalError('Paper baseline or code identity changed')
            if not create:
                self.db.execute('BEGIN IMMEDIATE')
                try:
                    epoch = int(self._meta('epoch')) + 1
                    self.db.execute('UPDATE meta SET value=? WHERE key=?', (str(epoch), 'epoch'))
                    self.db.commit()
                except BaseException:
                    self.db.rollback()
                    raise
            self.epoch = int(self._meta('epoch'))
            self.head = int(self._meta('head'))
            self.applied = int(self._meta('applied'))
        except (sqlite3.DatabaseError, ValueError) as exc:
            if hasattr(self, 'db'):
                self.db.close()
            raise PaperJournalError('Paper journal is corrupt, incompatible or incomplete') from exc

    def close(self):
        self.db.close()

    def _meta(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        if row is None:
            raise PaperJournalError('Missing paper journal metadata')
        return row[0]

    def verify(self):
        try:
            if self.db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                raise PaperJournalError('SQLite integrity check failed')
            keys = {key: value for key, value in self.db.execute('SELECT key,value FROM meta')}
            if set(keys) != {'baseline', 'code', 'epoch', 'head', 'head_hash',
                             'applied', 'applied_hash'}:
                raise PaperJournalError('Paper journal metadata is incomplete')
            commands = self.db.execute('SELECT seq,event_id,operation,payload,previous_hash,entry_hash '
                                       'FROM commands ORDER BY seq').fetchall()
            outcomes = self.db.execute('SELECT seq,state,result,post_hash,previous_hash,entry_hash '
                                       'FROM outcomes ORDER BY seq').fetchall()
            head_hash = applied_hash = '0'*64
            for expected, (seq, event_id, operation, payload, previous, entry) in enumerate(commands, 1):
                if (seq != expected or operation not in ('ACK', 'FILL', 'CANCEL')
                        or previous != head_hash or encode(decode(payload)) != payload
                        or entry != identity(seq, event_id, operation, payload, previous)):
                    raise PaperJournalError('Paper command chain is revised or out of order')
                head_hash = entry
            for expected, (seq, state, result, post, previous, entry) in enumerate(outcomes, 1):
                if (seq != expected or previous != applied_hash
                        or state not in ('APPLIED', 'QUARANTINED')
                        or not result or state == 'APPLIED' and len(post) != 64
                        or entry != identity(seq, state, result, post, previous)):
                    raise PaperJournalError('Paper outcome chain is revised or out of order')
                applied_hash = entry
            if (int(keys['head']) != len(commands) or keys['head_hash'] != head_hash
                    or int(keys['applied']) != len(outcomes) or keys['applied_hash'] != applied_hash
                    or len(commands) - len(outcomes) not in (0, 1)
                    or any(row[1] == 'QUARANTINED' for row in outcomes[:-1])
                    or outcomes and outcomes[-1][1] == 'QUARANTINED' and len(commands) != len(outcomes)):
                raise PaperJournalError('Paper journal head or committed prefix is incomplete')
            return tuple(commands), tuple(outcomes)
        except sqlite3.DatabaseError as exc:
            raise PaperJournalError('SQLite paper journal cannot be read') from exc

    def _fence(self):
        if int(self._meta('epoch')) != self.epoch:
            raise PaperJournalError('Stale paper journal writer epoch')
        if int(self._meta('head')) != self.head or int(self._meta('applied')) != self.applied:
            raise PaperJournalError('Paper journal expected head changed')

    def assert_owner(self):
        self._fence()

    def append(self, event_id, operation, payload):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self._fence()
            if self.head != self.applied:
                raise PaperJournalError('An unresolved paper command blocks new events')
            last = self.db.execute('SELECT state FROM outcomes ORDER BY seq DESC LIMIT 1').fetchone()
            if last and last[0] == 'QUARANTINED':
                raise PaperJournalError('Quarantined paper command requires manual resolution')
            seq, previous = self.head + 1, self._meta('head_hash')
            entry = identity(seq, event_id, operation, payload, previous)
            self.db.execute('INSERT INTO commands VALUES (?,?,?,?,?,?)',
                            (seq, event_id, operation, payload, previous, entry))
            self.db.execute('UPDATE meta SET value=? WHERE key=?', (str(seq), 'head'))
            self.db.execute('UPDATE meta SET value=? WHERE key=?', (entry, 'head_hash'))
            self.db.commit()
            self.head = seq
            return seq
        except BaseException:
            self.db.rollback()
            raise

    def finish(self, seq, state, result, post_hash):
        if state not in ('APPLIED', 'QUARANTINED') or not isinstance(result, str) or not result:
            raise PaperJournalError('Invalid paper command outcome')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self._fence()
            if seq != self.head or seq != self.applied + 1:
                raise PaperJournalError('Paper outcomes must commit in command order')
            previous = self._meta('applied_hash')
            entry = identity(seq, state, result, post_hash, previous)
            self.db.execute('INSERT INTO outcomes VALUES (?,?,?,?,?,?)',
                            (seq, state, result, post_hash, previous, entry))
            self.db.execute('UPDATE meta SET value=? WHERE key=?', (str(seq), 'applied'))
            self.db.execute('UPDATE meta SET value=? WHERE key=?', (entry, 'applied_hash'))
            self.db.commit()
            self.applied = seq
        except BaseException:
            self.db.rollback()
            raise

    def command(self, event_id):
        return self.db.execute('SELECT seq,operation,payload FROM commands WHERE event_id=?',
                               (event_id,)).fetchone()

    def outcome(self, seq):
        return self.db.execute('SELECT state,result,post_hash FROM outcomes WHERE seq=?',
                               (seq,)).fetchone()
