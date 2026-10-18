"""Owner-bound finite observations: receipt, process result and notification differ."""

from peer_tests.support import *  # noqa: F401,F403
from session_peers import watches

SID = 'ses_aaaaaaaaaaaaAAAAAAAAAAAAAA'


class WatchTests(Base):
    def setUp(self):
        super().setUp()
        self.owner = 'oc:' + SID
        self.wid = new_uuid()
        watches.paths(self.owner, create=True)
        self.base = watches.paths(self.owner, self.wid)

    def record(self, wake=False):
        return dict(id=self.wid, owner=self.owner, task='test task/head', timeout=5,
                    deadline=time.time()+5, cwd=str(self.root), log=self.base+'.log',
                    wake_consent=wake, status='launching', notification='not_attempted')

    def observe(self, program, wake=False):
        data = self.record(wake)
        watches.run_observation(data, [sys.executable, '-c', program], self.base)
        return watches.load(self.owner, self.wid)

    def test_exit_zero_is_not_task_success_and_no_consent_no_transport(self):
        with mock.patch.object(watches.opencode, 'deliver') as send:
            data = self.observe('print("ELAPSED last=running")')
        send.assert_not_called()
        self.assertEqual(data['exit_code'], 0)
        self.assertEqual(data['result'], 'command_exited')
        self.assertEqual(data['hint'], 'ELAPSED last=running')
        self.assertEqual(data['notification'], 'not_requested')
        self.assertIsNone(watches.listing(self.owner)[0]['handling'])

    def test_existing_result_token_and_nonzero_preserved(self):
        data = self.observe('print("RESULT=elapsed"); raise SystemExit(3)')
        self.assertEqual((data['exit_code'], data['hint']), (3, 'RESULT=elapsed'))

    def test_unknown_or_control_output_never_semantic_success(self):
        data = self.observe('print("success\\x1b[31m")')
        self.assertEqual(data['hint'], 'unknown')

    def test_one_notification_is_queued_not_handled(self):
        with mock.patch.object(watches.opencode, 'get_session'), \
                mock.patch.object(watches.opencode, 'deliver', return_value='msg_test') as send:
            data = self.observe('print("STOP=completed")', wake=True)
            watches._notify(data, self.base)
        self.assertEqual(send.call_count, 1)
        args, kwargs = send.call_args
        self.assertEqual(args[0], SID)
        self.assertTrue(kwargs['resume'])
        self.assertEqual(args[2]['message_id'], self.wid)
        self.assertIn('Not task completion or approval', args[1])
        receipt = watches.load(self.owner, self.wid)
        self.assertEqual(receipt['notification'], 'queued_unacknowledged')
        self.assertIsNone(watches.listing(self.owner)[0]['handling'])

    def test_transport_unknown_is_retained_no_retry(self):
        with mock.patch.object(watches.opencode, 'get_session'), \
                mock.patch.object(watches.opencode, 'deliver', side_effect=watches.opencode.OpenCodeError('unknown')) as send:
            data = self.observe('print("RESULT=ready")', wake=True)
            watches._notify(data, self.base)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(watches.load(self.owner, self.wid)['notification'], 'failed_or_unconfirmed')

    def test_paused_codex_does_not_queue_or_reset_any_budget(self):
        data = self.record(True)
        data.update(owner='codex:'+new_uuid(), result='command_exited', hint='unknown', exit_code=0)
        with mock.patch.object(watches.codex, 'resolve_thread', return_value={'id': data['owner'][6:], 'rollout_path': '/fake'}), \
                mock.patch.object(watches.codex, 'thread_is_held', return_value=(True, 1)), \
                mock.patch.object(watches.rollout, 'thread_is_paused', return_value=True), \
                mock.patch.object(watches.codex, 'codex_queue') as queue:
            watches._notify(data, self.base)
        queue.assert_not_called()
        self.assertEqual(data['notification'], 'skipped_paused')

    def test_codex_exact_owner_queues_once(self):
        data = self.record(True)
        data.update(owner='codex:'+new_uuid(), result='command_exited', hint='unknown', exit_code=0)
        thread = {'id': data['owner'][6:], 'rollout_path': '/fake', 'cwd': '/work', 'codex_home': '/home'}
        with mock.patch.object(watches.codex, 'resolve_thread', return_value=thread), \
                mock.patch.object(watches.codex, 'thread_is_held', return_value=(True, 1)), \
                mock.patch.object(watches.rollout, 'thread_is_paused', return_value=False), \
                mock.patch.object(watches.codex, 'codex_queue') as queue:
            watches._notify(data, self.base)
        self.assertEqual(queue.call_args.args[0], thread['id'])
        self.assertEqual(data['notification'], 'queued_unacknowledged')

    def test_missing_codex_owner_does_not_queue(self):
        data = self.record(True)
        data.update(owner='codex:'+new_uuid(), result='command_exited', hint='unknown', exit_code=0)
        with mock.patch.object(watches.codex, 'resolve_thread', return_value={'id': data['owner'][6:], 'rollout_path': '/fake'}), \
                mock.patch.object(watches.codex, 'thread_is_held', return_value=(None, None)), \
                mock.patch.object(watches.codex, 'codex_queue') as queue:
            watches._notify(data, self.base)
        queue.assert_not_called()
        self.assertEqual(data['notification'], 'owner_unavailable')

    def test_launch_error_private_receipt_survives(self):
        data = self.record()
        watches.run_observation(data, ['/definitely/missing/poller'], self.base)
        self.assertEqual(watches.load(self.owner, self.wid)['result'], 'launch_error')
        self.assertEqual(stat.S_IMODE(os.stat(self.base+'.json').st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.base)).st_mode), 0o700)

    def test_timeout_kills_own_group_and_is_not_success(self):
        data = self.record()
        data['timeout'] = .03
        with mock.patch.object(watches, 'TERM_GRACE', .03):
            watches.run_observation(data, [sys.executable, '-c', 'import time; time.sleep(.15)'], self.base)
        # Some host sandboxes refuse killpg even for an owned child. That is a
        # retained cleanup refusal, never a fake timeout-cleanup success.
        self.assertIn(data['result'], ('timeout', 'timeout_cleanup_refused'), data)
        if data['result'] == 'timeout_cleanup_refused':
            self.assertEqual(data['cleanup'], 'remaining_unknown')
        self.assertNotEqual(data['exit_code'], 0)

    def test_cleanup_permission_refusal_never_retries_or_looks_like_launch_failure(self):
        child = mock.Mock(pid=123, **{'wait.side_effect': subprocess.TimeoutExpired('poller', .1), 'poll.return_value': None})
        data = self.record()
        with mock.patch.object(watches.process, 'proc_start', return_value='same start'), \
                mock.patch.object(watches.subprocess, 'Popen', return_value=child), \
                mock.patch.object(watches.os, 'killpg', side_effect=PermissionError('denied')) as kill:
            watches.run_observation(data, ['poller'], self.base)
        self.assertEqual(data['result'], 'timeout_cleanup_refused')
        self.assertEqual(kill.call_args_list, [mock.call(123, signal.SIGTERM), mock.call(123, 0)])
        self.assertIsNone(data['exit_code'])

    def test_darwin_zombie_group_permission_then_gone_is_verified_cleanup(self):
        child = mock.Mock(pid=123, **{'wait.side_effect': subprocess.TimeoutExpired('poller', .1), 'poll.return_value': -15})
        data = self.record()
        with mock.patch.object(watches.process, 'proc_start', return_value='same start'), \
                mock.patch.object(watches.subprocess, 'Popen', return_value=child), \
                mock.patch.object(watches.os, 'killpg', side_effect=[None, PermissionError('zombie group'), ProcessLookupError()]) as kill, \
                mock.patch.object(watches, 'TERM_GRACE', 0):
            watches.run_observation(data, ['poller'], self.base)
        self.assertEqual(data['result'], 'timeout')
        self.assertEqual(data['cleanup'], 'verified_gone')
        self.assertEqual(kill.call_args_list, [mock.call(123, signal.SIGTERM), mock.call(123, signal.SIGKILL), mock.call(123, 0)])

    def test_timeout_group_includes_grandchild(self):
        ready = str(self.root/'grandchild-ready')
        orphan = str(self.root/'grandchild-survived')
        grandchild = ('import pathlib,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                      'pathlib.Path(%r).write_text("ready"); time.sleep(.6); pathlib.Path(%r).write_text("orphan")') % (ready, orphan)
        program = ('import pathlib,signal,subprocess,sys,time\n'
                   'signal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
                   'subprocess.Popen([sys.executable,"-c",%r])\n'
                   'while not pathlib.Path(%r).exists(): time.sleep(.005)\n'
                   'time.sleep(1)\n') % (grandchild, ready)
        data = self.record()
        data['timeout'] = .3
        with mock.patch.object(watches, 'TERM_GRACE', .03):
            watches.run_observation(data, [sys.executable, '-c', program], self.base)
        self.assertTrue(pathlib.Path(ready).exists(), data)
        self.assertEqual(data['result'], 'timeout', data)
        time.sleep(.65)
        self.assertFalse(pathlib.Path(orphan).exists(), 'grandchild escaped owned group cleanup')

    def test_owner_only_terminal_handling_and_idempotency(self):
        data = self.record()
        runtime = watches.runtime
        runtime.write_json_atomic(self.base+'.json', data)
        with self.assertRaises(ValueError):
            watches.handled(self.owner, self.wid, 'not actually finished')
        self.observe('print("STOP=failed")')
        watches.handled(self.owner, self.wid, 'held: failed gate; no merge')
        watches.handled(self.owner, self.wid, 'must not overwrite first handling')
        self.assertEqual(watches.listing(self.owner)[0]['handling']['next'], 'held: failed gate; no merge')
        with self.assertRaises(FileNotFoundError):
            watches.handled('oc:ses_bbbbbbbbbbbbBBBBBBBBBBBBBB', self.wid, 'wrong owner')

    def test_pid_reuse_and_lost_worker_unknown(self):
        data = self.record()
        data.update(worker_pid=1234, worker_start='old start', status='observing')
        watches.runtime.write_json_atomic(self.base+'.json', data)
        for start in (None, 'different start'):
            with mock.patch.object(watches.process, 'proc_start', return_value=start):
                self.assertEqual(watches.listing(self.owner)[0]['worker_liveness'], 'unknown_or_lost')

    def test_corrupt_handling_is_error_not_success(self):
        self.observe('print("RESULT=ready")')
        pathlib.Path(self.base+'.handled.json').write_text('{}')
        with self.assertRaises(ValueError):
            watches.listing(self.owner)
        with self.assertRaises(ValueError):
            watches.handled(self.owner, self.wid, 'do not hide corrupt receipt')

    def test_detached_launch_writes_real_finished_receipt(self):
        launched = watches.start(self.owner, 'finite test', 5,
            [sys.executable, '-c', 'print("RESULT=ready")'])
        for _ in range(300):
            receipt = watches.load(self.owner, launched['id'])
            if receipt['status'] == 'finished' and receipt['notification'] == 'not_requested':
                break
            time.sleep(.01)
        self.assertEqual(receipt['status'], 'finished', receipt)
        self.assertEqual(receipt['hint'], 'RESULT=ready')
        self.assertEqual(receipt['notification'], 'not_requested')

    def test_invalid_launch_never_forks(self):
        for timeout in (float('nan'), float('inf'), 0, -1, 86401):
            with mock.patch.object(watches.os, 'fork') as fork:
                with self.assertRaises(ValueError):
                    watches.start(self.owner, 'task', timeout, ['poller'])
                fork.assert_not_called()
        with self.assertRaises(ValueError):
            watches.start(self.owner, 'task', 1, [])

    def test_parser_requires_scope_and_rejects_owner_override(self):
        from session_peers import cli
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.build_parser().parse_args(['watch', 'start', '--task', 'x', '--timeout', '1', '--as', self.owner, '--', 'true'])

    def test_claude_gets_explicit_native_instruction(self):
        with mock.patch.object(watches.identity, 'caller_identity', return_value={'kind': 'cc', 'uuid': new_uuid()}):
            with self.assertRaisesRegex(ValueError, 'run_in_background'):
                watches.owner_now()
