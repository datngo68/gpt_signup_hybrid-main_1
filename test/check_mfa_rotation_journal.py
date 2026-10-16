"""Real SQLite persistence and OS account-lock checks, no live accounts."""
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.engine import DatabaseEngine
from db.mfa_rotation import RotationJournal


def main():
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
        root = Path(tmp)
        engine = DatabaseEngine(root / 'journal.db')
        journal = RotationJournal(engine)
        journal.install()
        journal.save('test@example.invalid', {'phase': 'disable_unknown', 'old_factor_id': 'old'})
        second = DatabaseEngine(root / 'journal.db')
        other = RotationJournal(second)
        assert other.get('TEST@example.invalid')['phase'] == 'disable_unknown'
        with journal.lock('test@example.invalid', root / 'locks'):
            try:
                with other.lock('TEST@example.invalid', root / 'locks'):
                    raise AssertionError('concurrent lock allowed')
            except OSError:
                pass
        with other.lock('TEST@example.invalid', root / 'locks'):
            pass
        engine.close()
        second.close()
    print('PASS: durable SQLite journal, case-insensitive account lock, lock release')


if __name__ == '__main__':
    main()
