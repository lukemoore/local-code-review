import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from .models import Batch, Comment, Message, Snapshot
from .views import (comments_to_send, detect_repository, import_batch,
                    parse_diff)


class ReviewTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.directory = tempfile.TemporaryDirectory(prefix='svn-review-test-')
        cls.root = Path(cls.directory.name)
        cls.repository = cls.root / 'repository'
        cls.working_copy = cls.root / 'working copy'
        cls.command('svnadmin', 'create', str(cls.repository))
        cls.command('svn', 'checkout', cls.repository.as_uri(),
                    str(cls.working_copy))
        cls.source = cls.working_copy / 'sample.txt'
        cls.original = ''.join(f'line {n}\n' for n in range(1, 71))
        cls.source.write_text(cls.original)
        (cls.working_copy / 'removed.txt').write_text('removed\n')
        cls.command('svn', 'add', str(cls.source),
                    str(cls.working_copy / 'removed.txt'))
        cls.command('svn', 'commit', '-m', 'Test fixture',
                    str(cls.working_copy))

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()
        super().tearDownClass()

    @classmethod
    def command(cls, *args):
        return subprocess.run(args, check=True, capture_output=True).stdout

    def setUp(self):
        self.source.write_text(self.original.replace(
            'line 3\n', '<script>alert("source")</script>\n'))
        self.data_directory = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.data_directory.cleanup)
        self.override = override_settings(DATA_DIR=Path(self.data_directory.name))
        self.override.enable()
        self.addCleanup(self.override.disable)

    def post(self, action, data, status=200):
        response = self.client.post(
            f'/api/{action}/', json.dumps(data), content_type='application/json')
        self.assertEqual(response.status_code, status, response.content)
        return response.json()

    def create(self):
        result = self.post('create', {'path': str(self.working_copy)})
        return Snapshot.objects.get(pk=result['snapshot_id'])

    def comment(self, snapshot, body='Please explain this change.'):
        return self.post('comment', {
            'snapshot_id': snapshot.id, 'path': 'sample.txt',
            'start': 2, 'end': 4, 'body': body,
        })['comment_id']

    def response_file(self, handoff, responses):
        path = Path(handoff['response_path'])
        path.write_text(json.dumps({
            'batch_id': handoff['batch_id'], 'responses': responses,
        }))
        return path

    def test_export_collects_project_comments_with_original_diffs(self):
        first = self.create()
        first_id = self.comment(first)
        resolved_id = self.comment(first, 'Already done.')
        self.post('resolve', {'comment_id': resolved_id, 'resolved': True})
        self.source.write_text(self.original.replace('line 3\n', 'updated\n'))
        second_id = self.post('refresh', {
            'review_id': first.review_id})['snapshot_id']
        second = Snapshot.objects.get(pk=second_id)
        other = self.create()
        self.comment(other, 'Another project.')

        # A snapshot with no comments can send an older snapshot's threads.
        handoff = self.post('export', {'snapshot_id': second.id})
        self.assertEqual([b['id'] for b in Batch.objects.get(
            pk=handoff['batch_id']).blocks], [first_id])
        second_comment = self.comment(second)
        for selected in (first, second):
            handoff = self.post('export', {'snapshot_id': selected.id})
            directory = Path(handoff['response_path']).parent
            context = json.loads((directory / 'context.json').read_text())
            self.assertEqual([(b['id'], b['snapshot_id'])
                              for b in context['blocks']],
                             [(first_id, first.id), (second_comment, second.id)])
            self.assertEqual(len(context['snapshots']), 2)
            for source, expected in zip(context['snapshots'], (first, second)):
                self.assertEqual(source['snapshot_id'], expected.id)
                self.assertEqual(source['snapshot_sha256'], expected.digest)
                self.assertEqual(source['base_revision'], expected.revision)
                self.assertEqual((directory / source['diff_file']).read_text(),
                                 expected.diff)
                self.assertEqual(source['live_diff'] is None,
                                 expected.id != second.id)
        self.response_file(handoff, [
            {'comment_id': first_id, 'reply': 'First reply.'},
            {'comment_id': second_comment, 'reply': 'Second reply.'},
        ])
        result = self.post('import', {'snapshot_id': first.id})
        self.assertEqual(result['imported'], 2)
        self.assertEqual(result['reply_snapshot_id'], first.id)
        self.assertEqual(result['errors'], [])
        self.assertEqual(Message.objects.filter(
            comment__snapshot=first, author='codex').count(), 1)
        self.assertEqual(Message.objects.filter(
            comment__snapshot=second, author='codex').count(), 1)
        self.assertEqual(self.post('import', {
            'snapshot_id': second.id})['imported'], 0)
        data = self.client.get('/api/snapshot/', {'id': first.id}).json()
        self.assertIn(handoff['batch_id'], [b['id'] for b in data['batches']])

    def test_send_availability_tracks_unresolved_project_comments(self):
        first = self.create()
        self.source.write_text(self.original.replace('line 3\n', 'updated\n'))
        second_id = self.post('refresh', {
            'review_id': first.review_id})['snapshot_id']
        other = self.create()
        self.comment(other)
        for resolved in (None, False, True, False):
            if resolved is False and not Comment.objects.filter(
                    snapshot=first).exists():
                comment_id = self.comment(first)
            elif resolved is not None:
                self.post('resolve', {
                    'comment_id': comment_id, 'resolved': resolved})
            for snapshot_id in (first.id, second_id):
                data = self.client.get('/api/snapshot/', {
                    'id': snapshot_id}).json()
                self.assertEqual(data['has_comments_to_send'],
                                 resolved is False)
                if resolved is not False:
                    self.post('export', {'snapshot_id': snapshot_id}, status=400)
        self.assertContains(self.client.get('/'),
                            'class="primary" hidden>Send comments to agent')

    def test_answered_threads_require_a_user_followup_before_sending(self):
        first = self.create()
        comment_id = self.comment(first)
        handoff = self.post('export', {'snapshot_id': first.id})
        self.source.write_text(self.original.replace('line 3\n', 'updated\n'))
        self.response_file(handoff, [
            {'comment_id': comment_id, 'reply': 'Here is the answer.',
             'needs_review': True}])
        latest_id = self.post('import', {
            'snapshot_id': first.id})['snapshot_id']
        reply = Message.objects.get(comment_id=comment_id, author='codex')
        for reviewed in (False, True):
            if reviewed:
                self.post('reviewed', {'message_id': reply.id})
            for snapshot_id in (first.id, latest_id):
                data = self.client.get('/api/snapshot/', {
                    'id': snapshot_id}).json()
                self.assertFalse(data['has_comments_to_send'])
                self.post('export', {'snapshot_id': snapshot_id}, status=400)
        self.post('resolve', {'comment_id': comment_id, 'resolved': True})
        self.post('resolve', {'comment_id': comment_id, 'resolved': False})
        self.post('export', {'snapshot_id': latest_id}, status=400)

        # A new question must not resend another thread's answered question.
        fresh_id = self.comment(Snapshot.objects.get(pk=latest_id))
        handoff = self.post('export', {'snapshot_id': first.id})
        self.assertEqual([b['id'] for b in Batch.objects.get(
            pk=handoff['batch_id']).blocks], [fresh_id])
        self.post('resolve', {'comment_id': fresh_id, 'resolved': True})
        self.post('reply', {'comment_id': comment_id, 'body': 'Please clarify.'})
        for snapshot_id in (first.id, latest_id):
            data = self.client.get('/api/snapshot/', {'id': snapshot_id}).json()
            self.assertTrue(data['has_comments_to_send'])
            handoff = self.post('export', {'snapshot_id': snapshot_id})
            blocks = Batch.objects.get(pk=handoff['batch_id']).blocks
            self.assertEqual([b['id'] for b in blocks], [comment_id])
            self.assertEqual([m['author'] for m in blocks[0]['messages']],
                             ['user', 'codex', 'user'])
        self.response_file(handoff, [
            {'comment_id': comment_id, 'reply': 'Clarification.',
             'needs_review': False}])
        self.post('import', {'snapshot_id': latest_id})
        data = self.client.get('/api/snapshot/', {'id': first.id}).json()
        self.assertFalse(data['has_comments_to_send'])
        self.post('export', {'snapshot_id': first.id}, status=400)

    def test_one_session_comment_across_snapshots_and_edit_after_reply(self):
        snapshot = self.create()
        comment_id = self.post('comment', {
            'snapshot_id': snapshot.id, 'body': 'Session request'})['comment_id']
        self.source.write_text(self.original.replace('line 3', 'different'))
        next_id = self.post('refresh', {
            'review_id': snapshot.review_id})['snapshot_id']
        self.post('comment', {
            'snapshot_id': next_id, 'body': 'Duplicate'}, status=400)
        data = self.client.get('/api/snapshot/', {'id': next_id}).json()
        self.assertEqual(data['session_comment']['id'], comment_id)
        self.assertEqual(data['session_comment']['snapshot_id'], snapshot.id)
        self.post('reply', {'comment_id': comment_id, 'body': 'Follow-up'})
        handoff = self.post('export', {'snapshot_id': next_id})
        path = self.response_file(handoff, [{
            'comment_id': comment_id, 'reply': 'Done'}])
        import_batch(handoff['batch_id'], path)
        self.post('resolve', {'comment_id': comment_id, 'resolved': True})
        self.post('edit', {'comment_id': comment_id, 'body': 'Changed request'})
        comment = Comment.objects.get(pk=comment_id)
        self.assertFalse(comment.resolved)
        self.assertEqual(comment.message_set.count(), 3)
        message = comment.message_set.filter(author='user').latest('id')
        self.assertEqual(message.body, 'Changed request')
        self.assertEqual(message.version, 2)
        self.assertTrue(comments_to_send(snapshot.review).exists())
        self.assertEqual(Comment.objects.filter(
            snapshot__review=snapshot.review, path='').count(), 1)

    def test_late_reply_does_not_answer_followup(self):
        snapshot = self.create()
        comment_id = self.comment(snapshot, 'Request A')
        handoff = self.post('export', {'snapshot_id': snapshot.id})
        block = Batch.objects.get(pk=handoff['batch_id']).blocks[0]
        self.assertEqual(block['request'], {
            'id': block['messages'][0]['id'], 'version': 1})
        self.post('reply', {'comment_id': comment_id, 'body': 'Request B'})
        path = self.response_file(handoff, [{
            'comment_id': comment_id, 'request': block['request'],
            'reply': 'Answer A'}])
        import_batch(handoff['batch_id'], path)
        data = self.client.get('/api/snapshot/', {'id': snapshot.id}).json()
        self.assertTrue(data['has_comments_to_send'])
        self.assertTrue(data['comments'][0]['messages'][-1]['earlier_request'])
        next_handoff = self.post('export', {'snapshot_id': snapshot.id})
        next_block = Batch.objects.get(pk=next_handoff['batch_id']).blocks[0]
        self.assertNotEqual(next_block['request']['id'], block['request']['id'])

    def test_edits_and_reverse_order_handoffs(self):
        snapshot = self.create()
        comment_id = self.comment(snapshot, 'Request A')
        first = self.post('export', {'snapshot_id': snapshot.id})
        self.post('edit', {'comment_id': comment_id, 'body': 'Request B'})
        second = self.post('export', {'snapshot_id': snapshot.id})
        first_target = Batch.objects.get(pk=first['batch_id']).blocks[0]['request']
        second_target = Batch.objects.get(pk=second['batch_id']).blocks[0]['request']
        self.assertEqual(first_target['id'], second_target['id'])
        self.assertEqual(second_target['version'], first_target['version'] + 1)
        for handoff, text in ((second, 'Answer B'), (first, 'Answer A')):
            path = self.response_file(handoff, [{
                'comment_id': comment_id, 'reply': text}])
            import_batch(handoff['batch_id'], path)
            data = self.client.get('/api/snapshot/', {'id': snapshot.id}).json()
            self.assertFalse(data['has_comments_to_send'])
        replies = data['comments'][0]['messages'][1:]
        self.assertEqual([m['earlier_request'] for m in replies], [False, True])

    def test_followup_handoffs_imported_in_reverse_order(self):
        snapshot = self.create()
        comment_id = self.comment(snapshot, 'Request A')
        first = self.post('export', {'snapshot_id': snapshot.id})
        self.post('reply', {'comment_id': comment_id, 'body': 'Request B'})
        second = self.post('export', {'snapshot_id': snapshot.id})
        for handoff in (second, first):
            path = self.response_file(handoff, [{
                'comment_id': comment_id, 'reply': 'Answer'}])
            import_batch(handoff['batch_id'], path)
            self.assertFalse(comments_to_send(snapshot.review).exists())
        self.post('reply', {'comment_id': comment_id, 'body': 'Request C'})
        self.assertTrue(comments_to_send(snapshot.review).exists())

    def test_late_reply_does_not_answer_edited_request(self):
        snapshot = self.create()
        comment_id = self.comment(snapshot, 'Request A')
        handoff = self.post('export', {'snapshot_id': snapshot.id})
        # Returning to the original text must still be a new version.
        for text in ('Request B', 'Request A'):
            self.post('edit', {'comment_id': comment_id, 'body': text})
        path = self.response_file(handoff, [{
            'comment_id': comment_id, 'reply': 'Answer A'}])
        import_batch(handoff['batch_id'], path)
        data = self.client.get('/api/snapshot/', {'id': snapshot.id}).json()
        self.assertTrue(data['has_comments_to_send'])
        self.assertTrue(data['comments'][0]['messages'][-1]['earlier_request'])

    def test_legacy_handoffs_match_only_unchanged_frozen_requests(self):
        snapshot = self.create()
        for change in ('unchanged', 'followup', 'edit', 'old_edit'):
            with self.subTest(change=change):
                comment_id = self.comment(snapshot, 'Request A')
                handoff = self.post('export', {'snapshot_id': snapshot.id})
                batch = Batch.objects.get(pk=handoff['batch_id'])
                batch.blocks = [b for b in batch.blocks if b['id'] == comment_id]
                block = batch.blocks[0]
                block.pop('request')
                for message in block['messages']:
                    message.pop('version')
                batch.save(update_fields=['blocks'])
                if change == 'followup':
                    self.post('reply', {'comment_id': comment_id, 'body': 'B'})
                elif change == 'edit':
                    self.post('edit', {'comment_id': comment_id, 'body': 'B'})
                elif change == 'old_edit':
                    # Simulate an edit made before versions existed.
                    Message.objects.filter(comment_id=comment_id).update(body='B')
                path = self.response_file(handoff, [{
                    'comment_id': comment_id, 'reply': 'Legacy answer'}])
                import_batch(batch.id, path)
                # Simulate a reply imported before the schema upgrade too.
                for old_database in (False, True):
                    if old_database:
                        Message.objects.filter(comment_id=comment_id,
                            author='codex').update(
                                answered_message_id=None, answered_version=None)
                    self.assertEqual(comments_to_send(snapshot.review).filter(
                        pk=comment_id).exists(), change != 'unchanged')

    def test_wrong_request_version_rejects_entire_import(self):
        snapshot = self.create()
        comment_id = self.comment(snapshot)
        handoff = self.post('export', {'snapshot_id': snapshot.id})
        path = self.response_file(handoff, [{
            'comment_id': comment_id, 'reply': 'Wrong version',
            'request': {'id': 123456, 'version': 9}}])
        with self.assertRaisesMessage(ValueError, 'request does not match'):
            import_batch(handoff['batch_id'], path)
        self.assertFalse(Batch.objects.get(pk=handoff['batch_id']).imported)
        self.assertFalse(Message.objects.filter(author='codex').exists())

    def test_diff_source_separators_are_not_record_boundaries(self):
        source = 'keep\u2028+not an addition\fmore\vtext\x85end\r'
        diff = ('Index: first.txt\n@@ -1,2 +1,2 @@\n ' + source +
                '\n-old\n+new\n@@ -10 +10 @@\n-before\n+after\n'
                'Index: second.txt\n@@ -3 +3 @@\n-x\n+y')
        for suffix in ('', '\n'):
            self.assertEqual(''.join(f['diff'] for f in parse_diff(
                diff + suffix, include_diff=True)), diff + suffix)
            files = parse_diff(diff + suffix)
            self.assertEqual([f['path'] for f in files],
                             ['first.txt', 'second.txt'])
            rows = files[0]['rows']
            self.assertEqual(rows[1], {
                'kind': 'context', 'old': 1, 'new': 1, 'text': source})
            self.assertEqual([(r['text'], r['new']) for r in rows
                              if r['kind'] == 'add'], [('new', 2), ('after', 10)])
            self.assertEqual(files[1]['rows'][-1], {
                'kind': 'add', 'old': None, 'new': 3, 'text': 'y'})
        self.assertEqual(parse_diff(''), [])

    def test_capture_context_and_snapshot_preservation(self):
        first = self.create()
        comment_id = self.comment(first)
        self.assertIn('<script>', first.diff)
        rows = first.files[0]['rows']
        changed = next(row for row in rows if row['kind'] == 'add')
        self.assertEqual(changed['new'], 3)
        self.assertIsNone(changed['old'])
        self.assertTrue(any(row['kind'] == 'context' for row in rows))
        self.source.write_text(self.original.replace('line 3\n', 'revised\n'))
        second = self.post('refresh', {'review_id': first.review_id})
        self.assertNotEqual(second['snapshot_id'], first.id)
        first.refresh_from_db()
        self.assertIn('<script>', first.diff)
        self.assertEqual(Comment.objects.get(pk=comment_id).snapshot_id, first.id)
        response = self.client.get('/api/snapshot/', {'id': first.id}).json()
        self.assertEqual(len(response['snapshots']), 2)
        listing = self.client.get('/api/reviews/').json()['reviews']
        self.assertEqual(listing[0]['latest_snapshot'], second['snapshot_id'])

    def test_delete_unsent_comment_removes_its_messages(self):
        snapshot = self.create()
        comment_id = self.comment(snapshot)
        data = self.client.get('/api/snapshot/', {'id': snapshot.id}).json()
        self.assertTrue(data['comments'][0]['can_delete'])
        self.post('delete', {'comment_id': comment_id})
        self.assertFalse(Comment.objects.filter(pk=comment_id).exists())
        self.assertFalse(Message.objects.filter(comment_id=comment_id).exists())
        data = self.client.get('/api/snapshot/', {'id': snapshot.id}).json()
        self.assertEqual(data['comments'], [])
        self.assertFalse(data['has_comments_to_send'])
        self.post('delete', {'comment_id': comment_id}, status=404)

    def test_sent_comment_cannot_be_deleted_before_or_after_reply(self):
        first = self.create()
        comment_id = self.comment(first)
        self.source.write_text(self.original.replace('line 3\n', 'updated\n'))
        latest_id = self.post('refresh', {
            'review_id': first.review_id})['snapshot_id']
        handoff = self.post('export', {'snapshot_id': latest_id})
        self.assertEqual(handoff['comment_ids'], [comment_id])
        self.post('edit', {'comment_id': comment_id, 'body': 'Updated question'})
        for imported in (False, True):
            if imported:
                self.response_file(handoff, [
                    {'comment_id': comment_id, 'reply': 'An answer.'}])
                self.post('import', {'snapshot_id': latest_id})
            data = self.client.get('/api/snapshot/', {'id': first.id}).json()
            self.assertFalse(data['comments'][0]['can_delete'])
            for resolved in (False, True):
                self.post('resolve', {
                    'comment_id': comment_id, 'resolved': resolved})
                result = self.post('delete', {'comment_id': comment_id},
                                   status=400)
                self.assertIn('Resolve it instead', result['error'])
                comment = Comment.objects.get(pk=comment_id)
                self.assertEqual(comment.resolved, resolved)
                self.assertEqual(comment.message_set.count(), 2 if imported else 1)
        # Existing handoffs do not protect newly added, unsent comments.
        fresh_id = self.comment(first)
        data = self.client.get('/api/snapshot/', {'id': first.id}).json()
        self.assertTrue(next(c for c in data['comments']
                             if c['id'] == fresh_id)['can_delete'])
        self.post('delete', {'comment_id': fresh_id})
        self.assertTrue(Comment.objects.filter(pk=comment_id).exists())

    def test_edit_new_comment_preserves_message_and_exported_context(self):
        snapshot = self.create()
        comment_id = self.comment(snapshot, 'Original request')
        message = Message.objects.get(comment_id=comment_id)
        handoff = self.post('export', {'snapshot_id': snapshot.id})
        self.post('edit', {'comment_id': comment_id, 'body': 'Updated request'})
        message.refresh_from_db()
        self.assertEqual(message.body, 'Updated request')
        self.assertEqual(Message.objects.filter(comment_id=comment_id).count(), 1)
        batch = Batch.objects.get(pk=handoff['batch_id'])
        self.assertEqual(batch.blocks[0]['messages'][0]['body'],
                         'Original request')
        updated = self.post('export', {'snapshot_id': snapshot.id})
        batch = Batch.objects.get(pk=updated['batch_id'])
        self.assertEqual(batch.blocks[0]['messages'][0]['body'],
                         'Updated request')

    def test_edit_rejects_empty_text_and_threads_with_replies(self):
        snapshot = self.create()
        comment_id = self.comment(snapshot, 'Original request')
        self.post('edit', {'comment_id': comment_id, 'body': ' '}, status=400)
        Message.objects.create(comment_id=comment_id, author='codex',
                               body='Agent response')
        self.post('edit', {'comment_id': comment_id, 'body': 'Changed'},
                  status=400)
        self.assertEqual(Message.objects.filter(
            comment_id=comment_id, author='user').get().body, 'Original request')

    def test_reload_reuses_unchanged_snapshot(self):
        first = self.create()
        comment_id = self.comment(first)
        result = self.post('refresh', {'review_id': first.review_id})
        self.assertEqual(result, {
            'snapshot_id': first.id, 'snapshot_created': False})
        self.assertEqual(Snapshot.objects.filter(
            review=first.review).count(), 1)
        self.assertEqual(Comment.objects.get(pk=comment_id).snapshot_id,
                         first.id)

    def test_snapshot_numbers_belong_to_each_review(self):
        first = self.create()
        other = self.create()
        self.source.write_text(self.original + 'another change\n')
        latest = self.post('refresh', {'review_id': first.review_id})
        data = self.client.get('/api/snapshot/', {'id': first.id}).json()
        self.assertEqual([(s['id'], s['number']) for s in data['snapshots']],
                         [(latest['snapshot_id'], 2), (first.id, 1)])
        data = self.client.get('/api/snapshot/', {'id': other.id}).json()
        self.assertEqual([(s['id'], s['number']) for s in data['snapshots']],
                         [(other.id, 1)])

    def test_review_queue_spans_snapshots_and_can_be_acknowledged(self):
        first = self.create()
        comment_id = self.comment(first)
        fixed_id = self.comment(first, 'Fix this.')
        handoff = self.post('export', {'snapshot_id': first.id})
        self.assertIn('needs_review', handoff['prompt'])
        self.response_file(handoff, [
            {'comment_id': comment_id, 'reply': 'Which behavior do you want?',
             'needs_review': True},
            {'comment_id': fixed_id, 'reply': 'Fixed and checked.',
             'needs_review': False},
        ])
        self.source.write_text(self.original.replace('line 3\n', 'fixed\n'))
        result = self.post('import', {'snapshot_id': first.id})
        latest = result['snapshot_id']
        self.assertNotEqual(latest, first.id)
        message = Message.objects.get(comment_id=comment_id, author='codex')
        expected = [{'id': message.id, 'comment_id': comment_id,
                     'comment__snapshot_id': first.id}]
        for snapshot_id in (first.id, latest):
            data = self.client.get('/api/snapshot/', {
                'id': snapshot_id}).json()
            self.assertEqual(data['review_queue'], expected)
        other = self.create()
        self.assertEqual(self.client.get('/api/snapshot/', {
            'id': other.id}).json()['review_queue'], [])
        self.post('reviewed', {'message_id': message.id})
        self.assertEqual(self.client.get('/api/snapshot/', {
            'id': latest}).json()['review_queue'], [])
        self.assertFalse(Comment.objects.get(pk=comment_id).resolved)
        message.refresh_from_db()
        self.assertTrue(message.needs_review)
        self.assertTrue(message.reviewed)

    def test_review_flag_validation_is_atomic_and_legacy_replies_are_visible(self):
        snapshot = self.create()
        first = self.comment(snapshot)
        second = self.comment(snapshot, 'Another question')
        handoff = self.post('export', {'snapshot_id': snapshot.id})
        for invalid in ('false', 0, None, []):
            self.response_file(handoff, [
                {'comment_id': first, 'reply': 'Answer', 'needs_review': True},
                {'comment_id': second, 'reply': 'Answer',
                 'needs_review': invalid},
            ])
            result = self.post('import', {'snapshot_id': snapshot.id})
            self.assertTrue(result['errors'])
            self.assertFalse(Message.objects.filter(author='codex').exists())
            self.assertFalse(Batch.objects.get(pk=handoff['batch_id']).imported)
        self.response_file(handoff, [
            {'comment_id': c, 'reply': 'Legacy answer'} for c in (first, second)])
        self.post('import', {'snapshot_id': snapshot.id})
        self.assertEqual(Message.objects.filter(needs_review=True).count(), 2)
        self.post('import', {'snapshot_id': snapshot.id})
        self.assertEqual(Message.objects.filter(needs_review=True).count(), 2)
        self.post('resolve', {'comment_id': first, 'resolved': True})
        queue = self.client.get('/api/snapshot/', {
            'id': snapshot.id}).json()['review_queue']
        self.assertEqual([m['comment_id'] for m in queue], [second])
        self.post('reply', {'comment_id': second, 'body': 'Use option A.'})
        self.assertEqual(self.client.get('/api/snapshot/', {
            'id': snapshot.id}).json()['review_queue'], [])

    def test_handoff_file_diffs_and_live_verification(self):
        # Keep unrelated changes large enough to catch accidental inclusion.
        unrelated = self.working_copy / 'removed.txt'
        self.addCleanup(unrelated.write_bytes, unrelated.read_bytes())
        unrelated.write_text('unrelated\n' * 2000)
        snapshot = self.create()
        self.comment(snapshot)
        handoff = self.post('export', {'snapshot_id': snapshot.id})
        directory = Path(handoff['response_path']).parent
        context = json.loads((directory / 'context.json').read_text())
        source = context['snapshots'][0]
        self.assertEqual(len(source['files']), 2)
        parts = [(directory / f['diff_file']).read_text()
                 for f in source['files']]
        self.assertEqual(''.join(parts), snapshot.diff)
        focused = next(f for f in source['files'] if f['path'] == 'sample.txt')
        frozen = (directory / focused['diff_file']).read_text()
        self.assertNotIn('unrelated', frozen)
        self.assertLess(focused['bytes'], len(snapshot.diff.encode()) / 10)
        live = source['live_diff']
        self.assertEqual([f['path'] for f in live['files']], ['sample.txt'])

        def run(command):
            return subprocess.run(
                command, cwd=self.working_copy, input=b'',
                capture_output=True, check=True, timeout=60
            ).stdout.decode('utf-8', errors='replace')

        current = run(live['verify_command'])
        self.assertEqual(hashlib.sha256(current.encode()).hexdigest(),
                         source['snapshot_sha256'])
        self.assertEqual(run(live['files'][0]['command']), frozen)
        # Export-time eligibility is not proof that a later session matches.
        self.source.write_text('changed after export\n')
        current = run(live['verify_command'])
        self.assertNotEqual(hashlib.sha256(current.encode()).hexdigest(),
                            source['snapshot_sha256'])
        self.assertEqual((directory / focused['diff_file']).read_text(), frozen)

    def test_handoff_with_unavailable_working_copy(self):
        snapshot = self.create()
        self.comment(snapshot)
        with patch('review.views.detect_repository', side_effect=ValueError):
            handoff = self.post('export', {'snapshot_id': snapshot.id})
        directory = Path(handoff['response_path']).parent
        context = json.loads((directory / 'context.json').read_text())
        self.assertIsNone(context['snapshots'][0]['live_diff'])
        self.assertTrue(context['snapshots'][0]['files'])

    def test_handoff_literal_file_names(self):
        vcs = detect_repository(self.working_copy)
        names = ['file[1]@name.txt', 'file1@name.txt']
        for name in names:
            path = self.working_copy / name
            path.write_text('new content\n')
            target = name + '@' if vcs == 'svn' else name
            subprocess.run([vcs, 'add', '--', target], cwd=self.working_copy,
                           capture_output=True, check=True)
            self.addCleanup(path.unlink)
            if vcs == 'svn':
                self.addCleanup(subprocess.run,
                                ['svn', 'revert', '--', target],
                                cwd=self.working_copy, capture_output=True,
                                check=True)
        snapshot = self.create()
        self.post('comment', {'snapshot_id': snapshot.id, 'path': names[0],
                              'start': 1, 'end': 1, 'body': 'Explain this.'})
        handoff = self.post('export', {'snapshot_id': snapshot.id})
        directory = Path(handoff['response_path']).parent
        context = json.loads((directory / 'context.json').read_text())
        command = context['snapshots'][0]['live_diff']['files'][0]['command']
        output = subprocess.run(command, cwd=self.working_copy,
                                capture_output=True, check=True).stdout.decode()
        self.assertEqual([f['path'] for f in parse_diff(output)], [names[0]])

    def test_agent_handoff_import_and_followup(self):
        snapshot = self.create()
        comment_id = self.comment(snapshot)
        handoff = self.post('export', {'snapshot_id': snapshot.id})
        for required in (str(self.working_copy), 'context.json',
                         'response.json', comment_id):
            self.assertIn(required, handoff['prompt'])
        self.assertIn('new or existing coding-agent session', handoff['prompt'])
        self.assertIn('ASD-STE100', handoff['prompt'])
        self.assertIn('Markdown inside each reply string', handoff['prompt'])
        self.assertNotIn('Codex', handoff['prompt'])
        directory = Path(handoff['response_path']).parent
        context = json.loads((directory / 'context.json').read_text())
        self.assertEqual(context['blocks'][0]['messages'][0]['body'],
                         'Please explain this change.')
        self.assertFalse((directory / 'snapshot.diff').exists())
        self.assertNotIn('Read each frozen diff', handoff['prompt'])
        self.response_file(handoff, [
            {'comment_id': comment_id, 'reply': 'This changes the sample.'}])
        imported = self.post('import', {'snapshot_id': snapshot.id})
        self.assertEqual(imported, {
            'imported': 1, 'errors': [], 'warnings': [],
            'snapshot_id': snapshot.id, 'reply_snapshot_id': snapshot.id,
            'snapshot_created': False})
        self.assertEqual(self.post('import', {'snapshot_id': snapshot.id})[
            'imported'], 0)
        self.assertEqual(Message.objects.filter(author='codex').count(), 1)
        self.post('resolve', {'comment_id': comment_id, 'resolved': True})
        self.post('export', {'snapshot_id': snapshot.id}, status=400)
        self.post('reply', {'comment_id': comment_id, 'body': 'Why this way?'})
        self.assertFalse(Comment.objects.get(pk=comment_id).resolved)
        followup = self.post('export', {'snapshot_id': snapshot.id})
        batch = Batch.objects.get(pk=followup['batch_id'])
        self.assertEqual([m['author'] for m in batch.blocks[0]['messages']],
                         ['user', 'codex', 'user'])
        self.assertEqual(Batch.objects.get(pk=handoff['batch_id']).blocks[0][
            'messages'][-1]['body'], 'Please explain this change.')

    def test_import_captures_changes_and_preserves_reviewed_snapshot(self):
        original = self.create()
        comment_id = self.comment(original)
        handoff = self.post('export', {'snapshot_id': original.id})
        self.source.write_text(self.original.replace('line 3\n', 'fixed\n'))
        waiting = self.post('import', {'snapshot_id': original.id})
        self.assertEqual(waiting['snapshot_id'], original.id)
        self.assertEqual(Snapshot.objects.count(), 1)
        self.response_file(handoff, [
            {'comment_id': comment_id, 'reply': 'Fixed the code.'}])
        result = self.post('import', {'snapshot_id': original.id})
        current = Snapshot.objects.get(pk=result['snapshot_id'])
        self.assertTrue(result['snapshot_created'])
        self.assertIn('+fixed', current.diff)
        original.refresh_from_db()
        self.assertIn('<script>', original.diff)
        self.assertEqual(Comment.objects.get(pk=comment_id).snapshot_id,
                         original.id)
        self.assertEqual(Message.objects.filter(author='codex').count(), 1)
        repeated = self.post('import', {'snapshot_id': original.id})
        self.assertEqual(repeated['snapshot_id'], current.id)
        self.assertFalse(repeated['snapshot_created'])
        self.assertEqual(Snapshot.objects.count(), 2)
        # A previously imported response can still refresh a stale review.
        self.source.write_text(self.original)
        refreshed = self.post('import', {'snapshot_id': original.id})
        self.assertTrue(refreshed['snapshot_created'])
        self.assertEqual(Snapshot.objects.get(pk=refreshed['snapshot_id']).diff,
                         '')
        self.assertEqual(Message.objects.filter(author='codex').count(), 1)

    def test_imported_markdown_is_rendered_safely_and_kept_in_context(self):
        snapshot = self.create()
        comment_id = self.comment(snapshot)
        handoff = self.post('export', {'snapshot_id': snapshot.id})
        body = (
            '**Answer** with `code` and [docs](https://example.com).\n\n'
            '- First\n- Second\n\n'
            '```python\nprint("<safe>")\n```\n\n'
            '| Name | Value |\n| --- | --- |\n| x | 1 |\n\n'
            '<script>alert(1)</script>\n\n'
            '[unsafe](javascript:alert%281%29)\n\n'
            '![image](https://example.com/image.png)')
        self.response_file(handoff, [
            {'comment_id': comment_id, 'reply': body}])
        result = self.post('import', {'snapshot_id': snapshot.id})
        self.assertEqual(result['imported'], 1)
        data = self.client.get('/api/snapshot/', {'id': snapshot.id}).json()
        messages = data['comments'][0]['messages']
        self.assertNotIn('body_html', messages[0])
        self.assertEqual(messages[-1]['body'], body)
        html = messages[-1]['body_html']
        for expected in ('<strong>Answer</strong>', '<code>code</code>',
                         '<ul>', '<pre><code class="language-python">',
                         '<table>', 'href="https://example.com"',
                         '&lt;script&gt;', '&lt;safe&gt;'):
            self.assertIn(expected, html)
        for unsafe in ('<script', '<img', 'href="javascript:'):
            self.assertNotIn(unsafe, html)
        self.post('reply', {'comment_id': comment_id, 'body': 'Please clarify.'})
        followup = self.post('export', {'snapshot_id': snapshot.id})
        context = json.loads((Path(followup['response_path']).parent /
                              'context.json').read_text())
        saved = context['blocks'][0]['messages'][-2]
        self.assertEqual(saved['body'], body)
        self.assertNotIn('body_html', saved)

    def test_import_keeps_replies_when_diff_capture_fails(self):
        snapshot = self.create()
        comment_id = self.comment(snapshot)
        handoff = self.post('export', {'snapshot_id': snapshot.id})
        self.response_file(handoff, [
            {'comment_id': comment_id, 'reply': 'Answer saved.'}])
        with patch('review.views.capture_diff', side_effect=ValueError('Offline')):
            result = self.post('import', {'snapshot_id': snapshot.id})
        self.assertEqual(result['imported'], 1)
        self.assertEqual(result['errors'], [])
        self.assertIn('could not be refreshed', result['warnings'][0])
        self.assertEqual(result['snapshot_id'], snapshot.id)
        self.assertEqual(Message.objects.filter(author='codex').count(), 1)

    def test_svn_detection_ignores_injected_git_directory(self):
        with tempfile.TemporaryDirectory(dir=self.working_copy) as directory:
            nested = Path(directory)
            (nested / '.git').mkdir()
            child = nested / 'help'
            child.mkdir()
            self.assertEqual(detect_repository(child), 'svn')
            self.assertFalse((nested / '.git').exists())

    def test_svn_detection_with_empty_git_at_same_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.svn').mkdir()
            (root / '.git').mkdir()
            self.assertEqual(detect_repository(root), 'svn')
            self.assertFalse((root / '.git').exists())

    def test_svn_detection_when_empty_git_cannot_be_removed(self):
        with tempfile.TemporaryDirectory(dir=self.working_copy) as directory:
            nested = Path(directory)
            (nested / '.git').mkdir()
            with patch.object(Path, 'rmdir', side_effect=PermissionError):
                self.assertEqual(detect_repository(nested), 'svn')
            self.assertTrue((nested / '.git').exists())

    def test_svn_detection_preserves_nonempty_git_metadata(self):
        with tempfile.TemporaryDirectory(dir=self.working_copy) as directory:
            nested = Path(directory)
            (nested / '.git').mkdir()
            marker = nested / '.git' / 'keep'
            marker.write_text('Preserve this file.')
            self.assertEqual(detect_repository(nested), 'svn')
            self.assertEqual(marker.read_text(), 'Preserve this file.')

    def test_real_git_checkout_inside_svn_is_detected(self):
        with tempfile.TemporaryDirectory(dir=self.working_copy) as directory:
            nested = Path(directory)
            self.command('git', 'init', str(nested))
            self.assertEqual(detect_repository(nested), 'git')

    def test_invalid_import_is_atomic_and_changed_import_is_rejected(self):
        snapshot = self.create()
        first = self.comment(snapshot)
        second = self.comment(snapshot, 'Another question')
        handoff = self.post('export', {'snapshot_id': snapshot.id})
        valid = [{'comment_id': c, 'reply': 'Answer'} for c in (first, second)]
        invalid_sets = [valid[:1], valid + valid[:1],
                        [valid[0], {'comment_id': 'unknown', 'reply': 'No'}],
                        [valid[0], {'comment_id': second, 'reply': ''}]]
        for responses in invalid_sets:
            self.response_file(handoff, responses)
            result = self.post('import', {'snapshot_id': snapshot.id})
            self.assertTrue(result['errors'])
            self.assertEqual(Message.objects.filter(author='codex').count(), 0)
        self.response_file(handoff, valid)
        self.assertEqual(self.post('import', {'snapshot_id': snapshot.id})[
            'imported'], 2)
        valid[0]['reply'] = 'Changed'
        self.response_file(handoff, valid)
        self.assertTrue(self.post('import', {'snapshot_id': snapshot.id})['errors'])
        self.assertEqual(Message.objects.filter(author='codex').count(), 2)

    def test_invalid_ranges_and_hunk_gaps(self):
        self.source.write_text(self.original.replace(
            'line 3\n', 'first change\n').replace('line 65\n', 'last change\n'))
        snapshot = self.create()
        for path, start, end in [('sample.txt', 1, 70), ('missing', 1, 1),
                                 ('sample.txt', 4, 2), ('', 1, 2),
                                 ('sample.txt', True, 2)]:
            self.post('comment', {
                'snapshot_id': snapshot.id, 'path': path,
                'start': start, 'end': end, 'body': 'Question',
            }, status=400)
        self.assertFalse(Comment.objects.exists())
        self.post('comment', {'snapshot_id': snapshot.id, 'body': 'General'})

    def test_add_delete_properties_and_no_final_newline(self):
        added = self.working_copy / 'added @ file.txt'
        added.write_text('new text without newline')
        self.command('svn', 'add', str(added) + '@')
        self.command('svn', 'delete', str(self.working_copy / 'removed.txt'))
        self.command('svn', 'propset', 'review:test', 'property text', str(self.source))
        try:
            snapshot = self.create()
            files = {f['path']: f['rows'] for f in snapshot.files}
            self.assertIn('added @ file.txt', files)
            self.assertTrue(any(r['kind'] == 'add' and r['new'] == 1
                                for r in files['added @ file.txt']))
            self.assertTrue(any(r['kind'] == 'remove' and r['old'] == 1
                                for r in files['removed.txt']))
            self.assertTrue(any('Property changes' in r['text']
                                for r in files['sample.txt']))
        finally:
            self.command('svn', 'revert', '-R', str(self.working_copy))
            added.unlink()

    def test_read_only_methods_csrf_and_invalid_directory(self):
        self.post('create', {'path': str(self.root / 'missing')}, status=400)
        self.post('create', {'path': str(self.root)}, status=400)
        client = Client(enforce_csrf_checks=True)
        response = client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('csrftoken', response.cookies)
        self.assertEqual(client.post('/api/create/', '{}',
            content_type='application/json').status_code, 403)
        self.assertEqual(client.get('/api/create/').status_code, 400)
        self.assertEqual(client.get('/static/review/app.js').status_code, 200)

    def test_parser_does_not_confuse_source_with_headers(self):
        diff = ('Index: tricky.txt\n===\n--- tricky.txt\n+++ tricky.txt\n'
                '@@ -1,2 +1,2 @@\n--- source\n+++ source\n Index: source\n'
                '\\ No newline at end of file\n')
        files = parse_diff(diff)
        self.assertEqual(len(files), 1)
        content = [r for r in files[0]['rows'] if r['kind'] in
                   ('add', 'remove', 'context')]
        self.assertEqual([(r['old'], r['new']) for r in content],
                         [(1, None), (None, 1), (2, 2)])
        self.assertEqual(content[1]['text'], '++ source')

    def test_folder_browser(self):
        response = self.client.get('/api/directories/', {
            'path': str(self.working_copy)})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['path'], str(self.working_copy))
        self.assertEqual(data['parent'], str(self.root))
        self.assertIn('.svn', [child['name'] for child in data['directories']])
        self.assertNotIn('sample.txt', [child['name'] for child in
                                     data['directories']])
        for path in (self.source, self.root / 'missing'):
            self.assertEqual(self.client.get('/api/directories/', {
                'path': str(path)}).status_code, 400)


class GitReviewTests(TestCase):
    post = ReviewTests.post
    create = ReviewTests.create
    comment = ReviewTests.comment
    response_file = ReviewTests.response_file
    test_capture_context_and_snapshot_preservation = (
        ReviewTests.test_capture_context_and_snapshot_preservation)
    test_agent_handoff_import_and_followup = (
        ReviewTests.test_agent_handoff_import_and_followup)
    test_handoff_file_diffs_and_live_verification = (
        ReviewTests.test_handoff_file_diffs_and_live_verification)
    test_handoff_literal_file_names = ReviewTests.test_handoff_literal_file_names
    test_import_captures_changes_and_preserves_reviewed_snapshot = (
        ReviewTests.test_import_captures_changes_and_preserves_reviewed_snapshot)

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='git-review-test-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.working_copy = self.root / 'working copy'
        self.working_copy.mkdir()
        self.git('init')
        self.git('config', 'user.name', 'Review Test')
        self.git('config', 'user.email', 'review@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        self.source = self.working_copy / 'sample.txt'
        self.original = ''.join(f'line {n}\n' for n in range(1, 71))
        self.source.write_text(self.original)
        (self.working_copy / 'removed.txt').write_text('removed\n')
        self.git('add', '.')
        self.git('commit', '-m', 'Test fixture')
        self.source.write_text(self.original.replace(
            'line 3\n', '<script>alert("source")</script>\n'))
        self.override = override_settings(DATA_DIR=self.root / 'data')
        self.override.enable()
        self.addCleanup(self.override.disable)

    def git(self, *arguments):
        return subprocess.run(
            ['git', '-C', str(self.working_copy), *arguments],
            check=True, capture_output=True).stdout.decode().strip()

    def test_staged_unstaged_added_deleted_and_untracked(self):
        self.git('add', 'sample.txt')
        self.source.write_text(self.source.read_text() + 'unstaged\n')
        names = ['space name.txt', 'café.txt', 'tab\tname.txt',
                 'quote"name.txt', 'back\\slash.txt']
        for name in names:
            (self.working_copy / name).write_text('added without newline')
            self.git('add', '--', name)
        (self.working_copy / 'removed.txt').unlink()
        (self.working_copy / 'untracked.txt').write_text('excluded\n')
        snapshot = self.create()
        self.assertEqual(snapshot.revision, self.git('rev-parse', 'HEAD'))
        files = {f['path']: f['rows'] for f in snapshot.files}
        self.assertEqual(set(files), {*names, 'sample.txt', 'removed.txt'})
        self.assertIn('<script>', snapshot.diff)
        self.assertIn('+unstaged', snapshot.diff)
        for name in names:
            self.assertTrue(any(r['kind'] == 'add' and r['new'] == 1
                                for r in files[name]))
        self.assertEqual(''.join(f['diff'] for f in parse_diff(
            snapshot.diff, include_diff=True)), snapshot.diff)
        self.assertTrue(any(r['kind'] == 'remove'
                            for r in files['removed.txt']))

    def test_subdirectory_and_unborn_branch(self):
        self.working_copy = self.root / 'new repo'
        self.working_copy.mkdir()
        self.git('init')
        folder = self.working_copy / 'nested'
        folder.mkdir()
        (folder / 'new.txt').write_text('first\n')
        (self.working_copy / 'outside.txt').write_text('outside\n')
        self.git('add', '.')
        self.working_copy = folder
        snapshot = self.create()
        self.assertEqual(snapshot.revision, 'unborn HEAD')
        self.assertEqual([f['path'] for f in snapshot.files], ['new.txt'])
        self.post('comment', {'snapshot_id': snapshot.id, 'path': 'new.txt',
                              'start': 1, 'end': 1, 'body': 'Explain this.'})
        handoff = self.post('export', {'snapshot_id': snapshot.id})
        context = json.loads((Path(handoff['response_path']).parent /
                              'context.json').read_text())
        live = context['snapshots'][0]['live_diff']
        for command in (live['verify_command'], live['files'][0]['command']):
            output = subprocess.run(command, cwd=self.working_copy, input=b'',
                                    capture_output=True, check=True).stdout
            self.assertEqual(output.decode(), snapshot.diff)

    def test_worktree_and_clean_snapshot(self):
        worktree = self.root / 'linked worktree'
        self.git('worktree', 'add', '-b', 'test-worktree', str(worktree))
        self.working_copy = worktree
        self.assertTrue((worktree / '.git').is_file())
        self.assertEqual(self.create().files, [])
        (worktree / 'sample.txt').write_text('changed\n')
        self.assertEqual(self.create().files[0]['path'], 'sample.txt')

    def test_binary_mode_rename_and_external_diff_configuration(self):
        binary = self.working_copy / 'binary space.bin'
        binary.write_bytes(b'\x00original')
        self.git('add', str(binary))
        self.git('commit', '-m', 'Binary fixture')
        binary.write_bytes(b'\x00changed')
        self.git('mv', 'removed.txt', 'renamed.txt')
        self.git('update-index', '--chmod=+x', 'renamed.txt')
        self.git('config', 'diff.external', 'nonexistent-review-diff-tool')
        snapshot = self.create()
        files = {f['path']: f['rows'] for f in snapshot.files}
        self.assertIn('removed.txt', files)
        self.assertIn('renamed.txt', files)
        self.assertTrue(any('Binary files' in r['text']
                            for r in files['binary space.bin']))

    def test_page_title_and_nearest_repository(self):
        self.assertContains(self.client.get('/'), '<title>Code Review</title>')
        nested = self.working_copy / 'nested svn'
        repository = self.root / 'svn repository'
        ReviewTests.command('svnadmin', 'create', str(repository))
        ReviewTests.command('svn', 'checkout', repository.as_uri(), str(nested))
        self.working_copy = nested
        snapshot = self.create()
        self.assertEqual(snapshot.revision, '0')
        self.assertEqual(snapshot.diff, '')
