"""Fenced, local SQLite command journal for offline M9.3 replay."""
from __future__ import annotations

from pathlib import Path
import sqlite3

from trading.replay_codec import decode, digest, encode, identity


class JournalError(ValueError):
    """A journal cannot safely accept or replay a command."""


class FencedWriter(JournalError):
    """This actor graph has lost its journal writer epoch."""


SCHEMA = '''
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE frames (
  frame_id TEXT PRIMARY KEY, seq INTEGER NOT NULL UNIQUE, at_ns INTEGER NOT NULL UNIQUE,
  body TEXT NOT NULL, body_hash TEXT NOT NULL, count INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('SEALED','COMPLETE'))
);
CREATE TABLE commands (
  command_id TEXT PRIMARY KEY, frame_id TEXT REFERENCES frames(frame_id),
  parent_id TEXT REFERENCES commands(command_id), order_key TEXT NOT NULL,
  kind TEXT NOT NULL, at_ns INTEGER NOT NULL, payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('PENDING','APPLIED')),
  outcome TEXT, effects TEXT, state_digest TEXT, applied_seq INTEGER UNIQUE
);
CREATE INDEX command_order ON commands(frame_id, order_key);
CREATE TABLE applied_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, command_id TEXT NOT NULL UNIQUE REFERENCES commands(command_id),
  previous_hash TEXT NOT NULL, entry_hash TEXT NOT NULL
);
CREATE TABLE jobs (
  job_id TEXT PRIMARY KEY, seq INTEGER NOT NULL UNIQUE, source_command TEXT NOT NULL REFERENCES commands(command_id),
  source TEXT NOT NULL, source_hash TEXT NOT NULL, state TEXT NOT NULL
    CHECK(state IN ('PENDING','READY','COMPLETE','FAILED')),
  views TEXT, next_view INTEGER NOT NULL DEFAULT 0, error_code TEXT
);
'''


class ReplayJournal:
    def __init__(self, path, *, manifest, code_hash, create=False):
        self.path = Path(path)
        creating = create
        if creating and self.path.exists():
            raise JournalError('Run journal already exists')
        if not creating and not self.path.is_file():
            raise JournalError('Run journal does not exist')
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        try:
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('PRAGMA busy_timeout=5000')
            if creating:
                self.db.executescript(SCHEMA)
                self.db.execute('BEGIN IMMEDIATE')
                try:
                    for key, value in (('manifest', encode(manifest)), ('code_hash', code_hash),
                                       ('epoch', '1'), ('head', '0'), ('chain', '0'*64)):
                        self.db.execute('INSERT INTO meta VALUES (?,?)', (key, value))
                    self.db.commit()
                except BaseException:
                    self.db.rollback()
                    raise
            else:
                self.verify_storage()
                if self.meta('manifest') != encode(manifest):
                    raise JournalError('Run configuration changed')
                if self.meta('code_hash') != code_hash:
                    raise JournalError('Run code identity changed')
                self.db.execute('BEGIN IMMEDIATE')
                try:
                    epoch = int(self.meta('epoch'))+1
                    self.db.execute('UPDATE meta SET value=? WHERE key=?', (str(epoch), 'epoch'))
                    self.db.commit()
                except BaseException:
                    self.db.rollback()
                    raise
            self.epoch = int(self.meta('epoch'))
            self.head = int(self.meta('head'))
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def meta(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        if row is None:
            raise JournalError('Missing journal metadata')
        return row[0]

    def _write(self, action):
        try:
            self.db.execute('BEGIN IMMEDIATE')
            if int(self.meta('epoch')) != self.epoch or int(self.meta('head')) != self.head:
                raise FencedWriter('Writer epoch or journal head changed')
            result = action()
            next_head = self.head+1
            self.db.execute('UPDATE meta SET value=? WHERE key=?', (str(next_head), 'head'))
            self.db.commit()
            self.head = next_head
            return result
        except BaseException:
            self.db.rollback()
            raise

    def seal(self, frame, ordered):
        """Commit all frame bytes and root PENDING commands in one transaction."""
        if (len(ordered) != len(frame.commands)
                or {item.command_id for item, _ in ordered}
                != {item.command_id for item in frame.commands}
                or len({key for _, key in ordered}) != len(ordered)):
            raise JournalError('Frame command order is incomplete or duplicated')
        body, body_hash = encode(frame), digest(encode(frame))
        prior = self.db.execute('SELECT body_hash,seq,state FROM frames WHERE frame_id=?',
                                (frame.frame_id,)).fetchone()
        if prior is not None:
            if prior[0] != body_hash:
                raise JournalError('Cannot revise a sealed frame')
            return prior[1], prior[2]

        def action():
            row = self.db.execute('SELECT MAX(seq),MAX(at_ns) FROM frames').fetchone()
            sequence = (row[0] or 0)+1
            if row[1] is not None and frame.as_of.value <= row[1]:
                raise JournalError('New frame cutoff must be strictly later')
            latest_applied = self.last_applied_time()
            if latest_applied is not None and frame.as_of.value <= latest_applied:
                raise JournalError('New frame cutoff must follow committed journal work')
            self.db.execute('INSERT INTO frames VALUES (?,?,?,?,?,?,?)',
                            (frame.frame_id, sequence, frame.as_of.value, body, body_hash,
                             len(frame.commands), 'SEALED'))
            for envelope, order_key in ordered:
                payload = encode(envelope.payload)
                self.db.execute('INSERT INTO commands '
                                '(command_id,frame_id,parent_id,order_key,kind,at_ns,payload,payload_hash,state) '
                                'VALUES (?,?,?,?,?,?,?,?,?)',
                                (envelope.command_id, frame.frame_id, None, order_key, envelope.kind,
                                 frame.as_of.value, payload, digest(payload), 'PENDING'))
            return sequence, 'SEALED'
        return self._write(action)

    def command(self, command_id):
        row = self.db.execute('SELECT command_id,frame_id,parent_id,order_key,kind,at_ns,payload,payload_hash,'
                              'state,outcome,effects,state_digest,applied_seq FROM commands WHERE command_id=?',
                              (command_id,)).fetchone()
        if row is None:
            raise JournalError('Unknown journal command')
        keys = ('command_id','frame_id','parent_id','order_key','kind','at_ns','payload','payload_hash',
                'state','outcome','effects','state_digest','applied_seq')
        return dict(zip(keys, row))

    def next_core(self):
        row = self.db.execute('SELECT c.command_id FROM commands c JOIN frames f ON c.frame_id=f.frame_id '
                              "WHERE c.state='PENDING' ORDER BY f.seq,c.order_key LIMIT 1").fetchone()
        return self.command(row[0]) if row else None

    def add_direct(self, command_id, kind, at, payload, *, parent_id=None, order_key=''):
        data = encode(payload)
        prior = self.db.execute('SELECT kind,at_ns,payload_hash,state FROM commands WHERE command_id=?',
                                (command_id,)).fetchone()
        if prior is not None:
            if prior[:3] != (kind, at.value, digest(data)):
                raise JournalError('Cannot revise a journal command')
            return prior[3]

        def action():
            self.db.execute('INSERT INTO commands '
                            '(command_id,frame_id,parent_id,order_key,kind,at_ns,payload,payload_hash,state) '
                            'VALUES (?,NULL,?,?,?,?,?,?,?)',
                            (command_id, parent_id, order_key, kind, at.value, data, digest(data), 'PENDING'))
            return 'PENDING'
        return self._write(action)

    def apply(self, command_id, result, effects, state_digest, *, children=(), jobs=(),
              job_progress=None, job_failure=None):
        outcome, changes = encode(result), encode(effects)

        def action():
            row = self.command(command_id)
            if row['state'] != 'PENDING':
                raise JournalError('Command already applied')
            previous = self.meta('chain')
            entry_hash = identity(previous, command_id, row['payload_hash'], outcome, changes, state_digest)
            cursor = self.db.execute('INSERT INTO applied_log(command_id,previous_hash,entry_hash) VALUES (?,?,?)',
                                     (command_id, previous, entry_hash))
            self.db.execute("UPDATE commands SET state='APPLIED',outcome=?,effects=?,state_digest=?,applied_seq=? "
                            'WHERE command_id=?',
                            (outcome, changes, state_digest, cursor.lastrowid, command_id))
            self.db.execute('UPDATE meta SET value=? WHERE key=?', (entry_hash, 'chain'))
            for child_id, kind, payload, ordinal in children:
                data = encode(payload)
                self.db.execute('INSERT INTO commands '
                                '(command_id,frame_id,parent_id,order_key,kind,at_ns,payload,payload_hash,state) '
                                'VALUES (?,?,?,?,?,?,?,?,?)',
                                (child_id, row['frame_id'], command_id,
                                 row['order_key']+f'.{ordinal:06d}', kind, row['at_ns'], data,
                                 digest(data), 'PENDING'))
            for job_id, source in jobs:
                data = encode(source)
                existing = self.db.execute('SELECT source_hash FROM jobs WHERE job_id=?', (job_id,)).fetchone()
                if existing is not None:
                    if existing[0] != digest(data):
                        raise JournalError('Notification source revised')
                    continue
                next_seq = self.db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM jobs').fetchone()[0]
                self.db.execute('INSERT INTO jobs(job_id,seq,source_command,source,source_hash,state) '
                                'VALUES (?,?,?,?,?,?)',
                                (job_id, next_seq, command_id, data, digest(data), 'PENDING'))
            if job_progress is not None:
                job_id, next_view, total = job_progress
                self.db.execute('UPDATE jobs SET next_view=?,state=? WHERE job_id=?',
                                (next_view, 'COMPLETE' if next_view == total else 'READY', job_id))
            if job_failure is not None:
                job_id, reason = job_failure
                self.db.execute("UPDATE jobs SET state='FAILED',error_code=? WHERE job_id=?",
                                (reason, job_id))
            if row['frame_id'] is not None:
                outstanding = self.db.execute("SELECT 1 FROM commands WHERE frame_id=? AND state='PENDING' LIMIT 1",
                                              (row['frame_id'],)).fetchone()
                if outstanding is None:
                    self.db.execute("UPDATE frames SET state='COMPLETE' WHERE frame_id=?", (row['frame_id'],))
            return cursor.lastrowid
        return self._write(action)

    def job(self, job_id):
        row = self.db.execute('SELECT job_id,seq,source_command,source,source_hash,state,views,next_view,error_code '
                              'FROM jobs WHERE job_id=?', (job_id,)).fetchone()
        if row is None:
            raise JournalError('Unknown notification job')
        return dict(zip(('job_id','seq','source_command','source','source_hash','state','views','next_view','error_code'), row))

    def next_job(self):
        row = self.db.execute("SELECT job_id FROM jobs WHERE state IN ('PENDING','READY') ORDER BY seq LIMIT 1").fetchone()
        return self.job(row[0]) if row else None

    def materialize_job(self, job_id, events):
        data = encode(events)
        def action():
            row = self.job(job_id)
            if row['state'] != 'PENDING':
                raise JournalError('Job already materialized')
            self.db.execute("UPDATE jobs SET views=?,state=? WHERE job_id=?",
                            (data, 'READY' if events else 'COMPLETE', job_id))
        self._write(action)

    def fail_job(self, job_id, reason):
        def action():
            self.db.execute("UPDATE jobs SET state='FAILED',error_code=? WHERE job_id=? AND state='PENDING'",
                            (reason, job_id))
        self._write(action)

    def unfinished_at_or_before(self, at):
        return self.db.execute("SELECT 1 FROM frames WHERE at_ns<=? AND state!='COMPLETE' LIMIT 1",
                               (at.value,)).fetchone() is not None

    def last_applied_time(self):
        row = self.db.execute("SELECT MAX(at_ns) FROM commands WHERE state='APPLIED'").fetchone()
        return row[0]

    def applied(self):
        rows = self.db.execute('SELECT command_id FROM applied_log ORDER BY seq').fetchall()
        return [self.command(row[0]) for row in rows]

    def verify_storage(self):
        try:
            if self.db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise JournalError('SQLite integrity check failed')
            if self.db.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise JournalError('SQLite foreign key check failed')
            for frame_id, body, body_hash, count, state in self.db.execute(
                    'SELECT frame_id,body,body_hash,count,state FROM frames'):
                frame = decode(body)
                if digest(body) != body_hash or frame.frame_id != frame_id or len(frame.commands) != count:
                    raise JournalError('Sealed frame changed')
                roots = self.db.execute('SELECT command_id,kind,at_ns,payload FROM commands '
                                        'WHERE frame_id=? AND parent_id IS NULL', (frame_id,)).fetchall()
                expected = {item.command_id: (item.kind, frame.as_of.value, encode(item.payload))
                            for item in frame.commands}
                if (len(roots) != count or
                        any(expected.get(command_id) != (kind, at_ns, payload)
                            for command_id, kind, at_ns, payload in roots)):
                    raise JournalError('Sealed frame root command changed')
                pending = self.db.execute("SELECT 1 FROM commands WHERE frame_id=? AND state='PENDING' LIMIT 1",
                                          (frame_id,)).fetchone() is not None
                if (state == 'COMPLETE') == pending:
                    raise JournalError('Frame completion marker changed')
            applied_count = 0
            for command_id, payload, payload_hash, state, outcome, effects, state_digest, applied_seq in self.db.execute(
                    'SELECT command_id,payload,payload_hash,state,outcome,effects,state_digest,applied_seq '
                    'FROM commands'):
                if digest(payload) != payload_hash:
                    raise JournalError('Journal command payload changed')
                decode(payload)
                if state == 'APPLIED':
                    applied_count += 1
                    if state_digest is None or applied_seq is None:
                        raise JournalError('Applied command lacks effect sequence')
                    decode(outcome)
                    decode(effects)
                elif any(item is not None for item in (outcome, effects, state_digest, applied_seq)):
                    raise JournalError('Pending command has committed effects')
            if applied_count != self.db.execute('SELECT COUNT(*) FROM applied_log').fetchone()[0]:
                raise JournalError('Applied command log is incomplete')
            chain = '0'*64
            for seq, command_id, previous, entry_hash in self.db.execute(
                    'SELECT seq,command_id,previous_hash,entry_hash FROM applied_log ORDER BY seq'):
                row = self.command(command_id)
                if (row['state'] != 'APPLIED' or row['applied_seq'] != seq or previous != chain or
                        identity(chain, command_id, row['payload_hash'], row['outcome'],
                                 row['effects'], row['state_digest']) != entry_hash):
                    raise JournalError('Journal effect chain changed')
                chain = entry_hash
            if chain != self.meta('chain'):
                raise JournalError('Journal chain head changed')
            prior_job_seq, prior_source_seq = 0, 0
            for seq, source_command, source, source_hash, views, next_view, state in self.db.execute(
                    'SELECT seq,source_command,source,source_hash,views,next_view,state '
                    'FROM jobs ORDER BY seq'):
                if digest(source) != source_hash:
                    raise JournalError('Notification job source changed')
                decode(source)
                if views is not None:
                    items = decode(views)
                    if type(items) is not tuple or not 0 <= next_view <= len(items):
                        raise JournalError('Notification view cursor changed')
                    if (state == 'COMPLETE') != (next_view == len(items)) and state != 'FAILED':
                        raise JournalError('Notification completion changed')
                source_row = self.command(source_command)
                if (seq != prior_job_seq+1 or source_row['state'] != 'APPLIED'
                        or source_row['applied_seq'] < prior_source_seq):
                    raise JournalError('Notification FIFO order changed')
                prior_job_seq, prior_source_seq = seq, source_row['applied_seq']
            decode(self.meta('manifest'))
        except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
            raise JournalError('Journal validation failed: '+str(exc)) from exc
