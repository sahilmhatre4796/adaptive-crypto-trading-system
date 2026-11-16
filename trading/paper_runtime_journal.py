"""Unit 4 operation vocabulary over the accepted fenced M12 SQLite protocol."""
import sqlite3

from trading.paper_journal import PaperJournal, PaperJournalError
from trading.replay_codec import decode, encode, identity


class RuntimeJournal(PaperJournal):
    """Retain Unit 3 storage/fencing; validate the additional runtime commands."""

    OPERATIONS = frozenset({'FRAME', 'TERMINAL', 'QUOTE', 'FUNDING', 'EXIT_FRAME',
                            'RISK', 'ADMIT', 'ACK', 'EXPIRE', 'FRAME_DONE',
                            'ENTRY_FILL', 'EXIT_FILL', 'CANCEL'})

    def verify(self):
        try:
            if self.db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                raise PaperJournalError('SQLite integrity check failed')
            keys = dict(self.db.execute('SELECT key,value FROM meta'))
            if set(keys) != {'baseline', 'code', 'epoch', 'head', 'head_hash',
                             'applied', 'applied_hash'}:
                raise PaperJournalError('Runtime journal metadata is incomplete')
            commands = self.db.execute(
                'SELECT seq,event_id,operation,payload,previous_hash,entry_hash '
                'FROM commands ORDER BY seq').fetchall()
            outcomes = self.db.execute(
                'SELECT seq,state,result,post_hash,previous_hash,entry_hash '
                'FROM outcomes ORDER BY seq').fetchall()
            head_hash = applied_hash = '0' * 64
            for expected, (seq, event_id, operation, payload, previous, entry) in enumerate(commands, 1):
                if (seq != expected or operation not in self.OPERATIONS
                        or previous != head_hash or encode(decode(payload)) != payload
                        or entry != identity(seq, event_id, operation, payload, previous)):
                    raise PaperJournalError('Runtime command chain is revised or out of order')
                head_hash = entry
            for expected, (seq, state, result, post, previous, entry) in enumerate(outcomes, 1):
                if (seq != expected or previous != applied_hash
                        or state not in ('APPLIED', 'QUARANTINED') or not result
                        or state == 'APPLIED' and len(post) != 64
                        or entry != identity(seq, state, result, post, previous)):
                    raise PaperJournalError('Runtime outcome chain is revised or out of order')
                applied_hash = entry
            if (int(keys['head']) != len(commands) or keys['head_hash'] != head_hash
                    or int(keys['applied']) != len(outcomes) or keys['applied_hash'] != applied_hash
                    or len(commands) - len(outcomes) not in (0, 1)
                    or any(row[1] == 'QUARANTINED' for row in outcomes[:-1])
                    or outcomes and outcomes[-1][1] == 'QUARANTINED' and len(commands) != len(outcomes)):
                raise PaperJournalError('Runtime journal head is incomplete')
            return tuple(commands), tuple(outcomes)
        except sqlite3.DatabaseError as exc:
            raise PaperJournalError('SQLite runtime journal cannot be read') from exc
