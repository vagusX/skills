"""Queue handoff, ambiguous delivery recovery, and original-request receipts."""

import asyncio
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import sys
import tempfile
import sqlite3
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agent_manager_runtime.approvals import answer_receipt, queue_answer
from agent_manager_runtime.codex import process_start
from agent_manager_runtime.controller import Controller
from agent_manager_runtime.cli import dispatch, parser
from agent_manager_runtime.notifications import acknowledge, bind_entry, deliver, inbox, notify
from agent_manager_runtime.panel import notify_pending
from agent_manager_runtime.store import Store, decode, encode, identifier
from unittest.mock import patch

ENTRY = '01a1befa-15e7-7fa2-96de-7266ea1f6303'
OTHER = '01a1befa-a180-7a92-b798-fa4ee66a562f'


class NativeQueue:
    def __init__(self):
        self.queued, self.turns, self.adds = [], [], []
        self.lose_response = False
        self.on_add = None

    async def history(self, thread):
        return self.turns

    async def call(self, method, params):
        if method == 'thread/queue/list':
            return {'data': list(self.queued), 'nextCursor': None}
        if method == 'thread/queue/delete':
            self.queued = [q for q in self.queued if q['id'] != params['queuedSubmissionId']]
            return {}
        if method == 'thread/queue/add':
            self.adds.append(params)
            row = {'id': identifier(), **params}
            self.queued.append(row)
            if self.on_add:
                self.on_add()
            if self.lose_response:
                raise RuntimeError('response lost after durable enqueue')
            return {'queuedSubmission': row}
        raise AssertionError('handoff must not resume/start/steer the App thread: ' + method)

    def consume(self, event_id):
        self.queued = [q for q in self.queued if q['clientUserMessageId'] != event_id]
        self.turns.append({'id': 'entry-turn', 'status': 'completed',
                           'items': [{'type': 'userMessage', 'clientId': event_id}]})


class NotificationTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {'CODEX_THREAD_ID': ENTRY})
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()
        self.store = Store(Path(self.temp.name) / 'state')
        self.main = self.store.setup(self.project)
        bind_entry(self.store, ENTRY)
        self.native = NativeQueue()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def event(self, key='one'):
        with self.store.transaction():
            return notify(self.store, key, self.main['id'], 'result', parent_id=None)

    def flush(self):
        asyncio.run(deliver(self.store, self.native))

    def row(self, job):
        return self.store.one('SELECT * FROM jobs WHERE id=?', (job['id'],))

    def test_binding_persists_without_a_fifth_table_or_managed_thread_takeover(self):
        self.store.db.execute("UPDATE chats SET thread_id=? WHERE role='main'", (OTHER,))
        with self.assertRaises(ValueError):
            bind_entry(self.store, OTHER)
        self.store.close()
        self.store = Store(Path(self.temp.name) / 'state')
        self.assertEqual(self.store.main()['entry_thread_id'], ENTRY)
        self.assertEqual({r['name'] for r in self.store.all("SELECT name FROM sqlite_master WHERE type='table'")}, {'chats','runs','jobs','sources'})

    def test_legacy_database_adds_entry_binding_without_changing_existing_thread(self):
        schema = self.store.one("SELECT sql FROM sqlite_master WHERE name='chats'")['sql'].replace(', entry_thread_id TEXT', '')
        directory = Path(self.temp.name) / 'legacy'
        directory.mkdir()
        db = sqlite3.connect(directory / 'state.sqlite3')
        db.execute(schema)
        db.execute("INSERT INTO chats(id,role,title,cwd,thread_id,created_at) VALUES(?,?,?,?,?,0)",
                   (self.main['id'],'main','existing',str(self.project.resolve()),OTHER))
        db.commit(); db.close()
        restored = Store(directory)
        try:
            self.assertEqual(restored.main()['thread_id'], OTHER)
            self.assertIsNone(restored.main()['entry_thread_id'])
            self.assertEqual(restored.setup(self.project)['id'], self.main['id'])
        finally:
            restored.close()

    def test_continue_preserves_stdin_constraints_in_one_followup(self):
        self.store.db.execute("UPDATE chats SET hold=? WHERE id=?", (encode({'request':'pause'}), self.main['id']))
        original = 'Continue only to summarize. Do not execute or request permissions.\n'
        args = parser().parse_args(['continue', self.main['id']])
        with patch('sys.stdin', io.StringIO(original)):
            job = dispatch(args, self.store)
        self.assertEqual(decode(job['payload'])['text'], original)
        self.assertIsNone(self.store.main()['hold'])
        self.assertEqual(len(self.store.all("SELECT * FROM jobs WHERE kind='followup'")), 1)

    def test_same_event_only_enqueues_once_and_keeps_original_destination(self):
        job = self.event()
        bind_entry(self.store, OTHER)
        self.assertEqual(self.event()['id'], job['id'])
        self.flush()
        self.flush()
        self.assertEqual(len(self.native.adds), 1)
        self.assertEqual(self.native.adds[0]['threadId'], ENTRY)
        self.assertEqual(self.native.adds[0]['clientUserMessageId'], job['id'])
        self.assertEqual(self.row(job)['status'], 'queued')
        self.assertNotIn('acknowledged_at', decode(self.row(job)['result_ref']))

    def test_lost_enqueue_response_recovers_queue_receipt_without_resending(self):
        job = self.event()
        self.native.lose_response = True
        self.flush()
        self.assertEqual(self.row(job)['status'], 'unknown')
        self.store.close()
        self.store = Store(Path(self.temp.name) / 'state')
        self.flush()
        self.assertEqual(self.row(job)['status'], 'queued')
        self.assertEqual(len(self.native.adds), 1)

    def test_consumption_gap_is_unknown_until_history_confirms_client_id(self):
        job = self.event()
        self.flush()
        self.native.queued.clear()  # Core accepted, rollout hasn't been written yet.
        self.flush()
        self.assertEqual(self.row(job)['status'], 'unknown')
        self.flush()
        self.assertEqual(len(self.native.adds), 1)
        self.native.consume(job['id'])
        self.flush()
        self.assertEqual(self.row(job)['status'], 'received')
        self.assertEqual(decode(self.row(job)['result_ref'])['turn_id'], 'entry-turn')
        with self.assertRaises(ValueError):
            acknowledge(self.store, job['id'], OTHER)
        first = acknowledge(self.store, job['id'], ENTRY)
        self.assertEqual(first, acknowledge(self.store, job['id'], ENTRY))
        self.flush()
        self.assertEqual(self.row(job)['status'], 'done')
        self.assertEqual(len(self.native.adds), 1)

    def test_ack_arriving_before_enqueue_response_is_not_overwritten(self):
        job = self.event()
        self.native.on_add = lambda: acknowledge(self.store, job['id'], ENTRY)
        self.flush()
        result = decode(self.row(job)['result_ref'])
        self.assertEqual(self.row(job)['status'], 'done')
        self.assertEqual(result['phase'], 'acknowledged')
        self.assertIn('queued_submission_id', result)

    def test_ack_wins_over_a_late_lost_enqueue_response(self):
        job = self.event()
        self.native.on_add = lambda: acknowledge(self.store, job['id'], ENTRY)
        self.native.lose_response = True
        self.flush()
        self.assertEqual(self.row(job)['status'], 'done')
        self.assertIsNone(self.row(job)['error'])

    def test_superseded_report_does_not_create_another_entry_message(self):
        report = {'thread_id':'worker','turn_id':'old','call_id':'old-report'}
        self.store.db.execute('UPDATE chats SET report_ref=? WHERE id=?', (encode(report), self.main['id']))
        job = notify(self.store, 'old-result', self.main['id'], 'result', parent_id=None, report_ref=report)
        self.store.db.execute('UPDATE chats SET report_ref=? WHERE id=?', (encode({'turn_id':'new'}), self.main['id']))
        self.flush()
        self.assertEqual(self.native.adds, [])
        self.assertEqual(self.row(job)['status'], 'done')
        self.assertEqual(decode(self.row(job)['result_ref'])['phase'], 'superseded')
        self.assertTrue(asyncio.run(inbox(self.store, self.native, job['id']))[0]['superseded'])

    def question(self):
        job = self.store.submit('Original isolated test request')
        run = self.store.claim(self.main['id'])
        started = process_start(os.getpid())
        self.store.db.execute("UPDATE chats SET thread_id='native-main' WHERE id=?", (self.main['id'],))
        self.store.db.execute("UPDATE runs SET state='running',thread_id='native-main',turn_id='native-turn',server_pid=?,server_start=? WHERE id=?", (os.getpid(), started, run['id']))
        (self.store.directory / 'daemon.json').write_text(encode({'pid': os.getpid(), 'started': started}))
        self.responses = []
        async def respond(request_id, response):
            self.responses.append((request_id, response))
        self.client = SimpleNamespace(process=SimpleNamespace(pid=os.getpid()), started=started, respond=respond)
        self.controller = Controller(self.store)
        self.controller.clients['main'] = self.client
        with redirect_stdout(io.StringIO()):
            asyncio.run(self.controller.on_request(self.client, {'method':'item/commandExecution/requestApproval','id':7,
                'params':{'threadId':'native-main','turnId':'native-turn','command':'test only','availableDecisions':['decline','cancel']}}))
        return self.store.one("SELECT * FROM jobs WHERE kind='question'")

    def test_native_question_wakes_once_and_answer_returns_only_to_original_rpc(self):
        q = self.question()
        event = self.store.one("SELECT * FROM jobs WHERE kind='notification'")
        self.flush()
        self.assertEqual(len(self.native.adds), 1)
        details = asyncio.run(inbox(self.store, self.native, event['id']))[0]
        self.assertEqual(details['question']['id'], q['id'])
        self.assertEqual(details['question']['params']['command'], 'test only')
        queue_answer(self.store, q['id'], {'decision':'decline'})
        self.assertEqual(answer_receipt(self.store, q['id'])['phase'], 'answer_queued')
        asyncio.run(self.controller.answer_questions())
        asyncio.run(self.controller.answer_questions())
        self.assertEqual(self.responses, [(7, {'decision':'decline'})])
        self.assertEqual(answer_receipt(self.store, q['id'])['phase'], 'sent')
        asyncio.run(self.controller.on_event(self.client, {'method':'serverRequest/resolved','params':{'requestId':7,'threadId':'native-main'}}))
        self.assertEqual(answer_receipt(self.store, q['id'])['phase'], 'resolved')
        self.flush()
        self.assertEqual(len(self.native.queued), 0)
        self.assertEqual(len(self.store.all('SELECT * FROM runs')), 1)

    def test_native_cancel_holds_original_work_until_explicit_continue(self):
        q = self.question()
        queue_answer(self.store, q['id'], {'decision':'cancel'})
        asyncio.run(self.controller.answer_questions())
        asyncio.run(self.controller.on_event(self.client, {'method':'serverRequest/resolved','params':{'requestId':7}}))
        hold = decode(self.store.main()['hold'])
        self.assertEqual(hold['question_id'], q['id'])
        self.assertTrue(hold['requires_human_continue'])
        self.assertEqual(answer_receipt(self.store, q['id'])['phase'], 'resolved')

    def test_management_correction_is_not_forwarded_as_final_result(self):
        work_id = identifier()
        self.store.db.execute("INSERT INTO chats(id,role,title,cwd,created_at) VALUES(?,?,?,?,0)",
                              (work_id,'worker','test worker',str(self.project)))
        wake, _ = self.store.enqueue('wake', self.main['id'], {'work_id':work_id,'state':'ready'})
        run = self.store.claim(self.main['id'])
        self.store.enqueue('followup', work_id, {'text':'finish original acceptance'}, parent_id=wake['id'])
        with redirect_stdout(io.StringIO()):
            asyncio.run(Controller(self.store).completed(run, {'id':'main-turn','status':'completed'}))
        self.assertEqual(self.store.all("SELECT * FROM jobs WHERE kind='notification'"), [])
        final, _ = self.store.enqueue('wake', self.main['id'], {'work_id':work_id,'state':'ready'})
        final_run = self.store.claim(self.main['id'])
        with redirect_stdout(io.StringIO()):
            asyncio.run(Controller(self.store).completed(final_run, {'id':'final-turn','status':'completed'}))
        event = self.store.one("SELECT * FROM jobs WHERE kind='notification'")
        self.assertEqual(decode(event['payload'])['ref']['turn_id'], 'final-turn')
        self.assertEqual(event['chat_id'], work_id)
        with redirect_stdout(io.StringIO()):
            asyncio.run(Controller(self.store).completed(final_run, {'id':'final-turn','status':'completed'}))
        self.assertEqual(len(self.store.all("SELECT * FROM jobs WHERE kind='notification'")), 1)

    def test_pause_after_answer_was_queued_prevents_its_delivery(self):
        q = self.question()
        queue_answer(self.store, q['id'], {'decision':'decline'})
        self.store.db.execute('UPDATE chats SET hold=? WHERE id=?', (encode({'request':'pause'}), self.main['id']))
        asyncio.run(self.controller.answer_questions())
        self.assertEqual(self.responses, [])
        self.assertEqual(answer_receipt(self.store, q['id'])['phase'], 'expired')

    def test_expired_question_does_not_wake_or_notify_the_desktop(self):
        q = self.question()
        self.store.db.execute("UPDATE jobs SET status='done',error='connection ended' WHERE id=?", (q['id'],))
        self.flush()
        self.assertEqual(self.native.adds, [])
        with patch('agent_manager_runtime.panel.notify', side_effect=AssertionError('entry chat is the notification channel')):
            notify_pending(self.store, {}, set())

    def test_mismatched_connection_cannot_confirm_an_answer(self):
        q = self.question()
        other_client = SimpleNamespace(process=self.client.process, started='different process start')
        asyncio.run(self.controller.on_event(other_client, {'method':'serverRequest/resolved','params':{'requestId':7}}))
        self.assertEqual(answer_receipt(self.store, q['id'])['phase'], 'pending')

    def test_request_closed_without_an_answer_is_not_a_delivery_receipt(self):
        q = self.question()
        asyncio.run(self.controller.on_event(self.client, {'method':'serverRequest/resolved','params':{'requestId':7}}))
        self.assertEqual(answer_receipt(self.store, q['id'])['phase'], 'expired')

    def test_answer_write_failure_is_reported_once_without_replaying(self):
        q = self.question()
        queue_answer(self.store, q['id'], {'decision':'decline'})
        calls = []
        async def fail(request_id, response):
            calls.append(request_id)
            raise OSError('connection write failed')
        self.client.respond = fail
        asyncio.run(self.controller.answer_questions())
        asyncio.run(self.controller.answer_questions())
        self.assertEqual(calls, [7])
        self.assertEqual(answer_receipt(self.store, q['id'])['phase'], 'unknown')
        warnings = self.store.all("SELECT * FROM jobs WHERE kind='notification' AND parent_id=?", (q['id'],))
        self.assertEqual(len(warnings), 2)  # Initial question and its delivery failure.
        self.assertEqual(sum(decode(j['payload'])['kind']=='failure' for j in warnings), 1)


if __name__ == '__main__':
    unittest.main()
