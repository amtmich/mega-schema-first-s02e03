#!/usr/bin/env python3
"""Local-only administration. No public owner backdoor exists."""
import argparse
import json

from server import read_config, ROOT
from storage import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('clients')
    revoke = commands.add_parser('revoke-client')
    revoke.add_argument('client_id')
    for name in ('delete-note', 'delete-file'):
        child = commands.add_parser(name)
        child.add_argument('id')
    args = parser.parse_args()
    store = Store(read_config().get('DATA_DIR', str(ROOT / 'private' / 'data')))
    with store.connect() as db:
        if args.command == 'clients':
            for row in db.execute("SELECT key,value FROM records WHERE kind='client'"):
                value = json.loads(row['value'])
                print(json.dumps({'client_id': row['key'], 'client_name': value.get('client_name')}))
        elif args.command == 'revoke-client':
            removed = 0
            for row in db.execute("SELECT kind,key,value FROM records WHERE kind IN ('access','refresh','code','pending')").fetchall():
                if json.loads(row['value']).get('client_id') == args.client_id:
                    db.execute('DELETE FROM records WHERE kind=? AND key=?', (row['kind'], row['key']))
                    removed += 1
            db.execute("DELETE FROM records WHERE kind='client' AND key=?", (args.client_id,))
            print('Revoked credential records:', removed)
        else:
            table = 'notes' if args.command == 'delete-note' else 'files'
            cursor = db.execute('DELETE FROM ' + table + ' WHERE id=?', (args.id,))
            print('Deleted records:', cursor.rowcount)


if __name__ == '__main__':
    main()
