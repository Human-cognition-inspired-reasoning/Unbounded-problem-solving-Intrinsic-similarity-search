"""Research attachment export and explicit maintenance; uses the server's environment."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

from files.preparation import digest


def export_participant(service, study_id, destination):
    """Snapshot metadata once; export immutable bytes with verified checksums."""
    target = Path(destination)
    target.mkdir(parents=True, exist_ok=False, mode=0o700)
    with service.store.write(study_id) as tx:
        state = tx.dump()
        rows = [row for row in tx.attachments() if row['message_id']]
    for row in rows:
        if row['backend'] != service.backend:
            raise RuntimeError('Export requires the original storage backend')
        for label, file in [('original', row['manifest']['original'])] + [
            (f"input-{f['index']}", f) for f in row['manifest']['files']
        ]:
            data = service.objects.read(file['key'], service.config['ATTACHMENT_MAX_DERIVED_BYTES'])
            if digest(data) != file['sha256']:
                raise RuntimeError(f"Checksum mismatch: {row['id']} {label}")
            relative = f"files/{row['id']}/{label}{Path(file['name']).suffix.lower()}"
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(data); path.chmod(0o600)
            file['exportPath'] = relative
        # Provider IDs are not needed for research reconstruction.
        row.pop('provider_refs', None)
    for name, payload in [('participant.json', state), ('attachments.json', rows)]:
        path = target / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        path.chmod(0o600)
    return len(rows)


def prune_unsubmitted(service, hours=24, apply=False):
    if hours < 24:
        raise ValueError('Unsubmitted attachments must be at least 24 hours old')
    before = datetime.now(timezone.utc) - timedelta(hours=hours)
    count = 0
    for study_id in service.store.participant_ids():
        for row in service.store.read(study_id).attachments():
            if row['message_id'] or datetime.fromisoformat(row['updated_at']) >= before:
                continue
            if row['backend'] != service.backend:
                continue
            # A process may have died mid-conversion. Recover only a day-old claim.
            if apply and row['status'] == 'processing':
                with service.store.write(study_id) as tx:
                    current = tx.attachment(row['id'])
                    if not current or current['message_id'] or datetime.fromisoformat(current['updated_at']) >= before:
                        continue
                    tx.update_attachment(row['id'], status='pending')
            if not apply or service.discard(study_id, row['id'], before=before):
                count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    export = sub.add_parser('export', help='Export submitted originals, exact model inputs, and participant records')
    export.add_argument('study_id'); export.add_argument('destination')
    prune = sub.add_parser('prune', help='List/count unsubmitted expired uploads; --apply deletes them')
    prune.add_argument('--hours', type=int, default=24); prune.add_argument('--apply', action='store_true')
    copies = sub.add_parser('clear-provider-copies', help='Delete this participant CSV copies from the current Claude workspace; local originals stay')
    copies.add_argument('study_id'); copies.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    from runtime.config import load_settings
    from runtime.services import open_attachment_service
    service = open_attachment_service(load_settings())
    service.require_enabled()
    if args.command == 'export':
        print(f"Exported {export_participant(service, args.study_id, args.destination)} submitted attachments")
    elif args.command == 'prune':
        print(f"{'Deleted' if args.apply else 'Eligible'}: {prune_unsubmitted(service, args.hours, args.apply)} unsubmitted attachments")
    else:
        from anthropic import Anthropic, NotFoundError
        prefix = digest(os.environ['ANTHROPIC_API_KEY'].encode())[:16] + ':'
        count = 0
        # Run while the participant is inactive: concurrent model requests may be using these IDs.
        with Anthropic(timeout=30, max_retries=1) as client:
            for row in service.store.read(args.study_id).attachments():
                refs = dict(row['provider_refs'])
                for key, file_id in list(refs.items()):
                    if not key.startswith(prefix):
                        continue
                    count += 1
                    if args.apply:
                        try: client.beta.files.delete(file_id)
                        except NotFoundError: pass
                        del refs[key]
                if args.apply:
                    with service.store.write(args.study_id) as tx:
                        tx.update_attachment(row['id'], provider_refs=refs)
        print(f"{'Deleted' if args.apply else 'Eligible'}: {count} provider copies")


if __name__ == '__main__':
    main()
