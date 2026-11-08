"""Adversarial storage and lifecycle regression tests; scratch data only."""
import copy
import inspect
import multiprocessing
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from proof_run import core


def concurrent_save(root, contract, queue):
    try:
        queue.put(core.save_contract(Path(root), contract, new_version=True)['version'])
    except (core.ContractError, OSError, TypeError, ValueError) as exc:
        queue.put(type(exc).__name__ + ':' + str(exc))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'data'

    def record(self, root=None, bound=True):
        r = core.Recorder(root or self.root)
        r.start('sample', 'test')
        r.note_tool('stock_move', {'qty': 1}, {'moved': 1})
        if 'bound' in inspect.signature(r.stop).parameters:
            return r.stop(bound=bound)
        return r.stop()  # exercise the legacy implementation in RED

    def test_baseline_result_credentials_never_persisted(self):
        r = core.Recorder(self.root); r.start('sample', 'test')
        with self.assertRaises(core.ContractError):
            r.note_tool('stock_move', {}, {'ok': True, 'password': 'opaque'})
            r.stop()
        self.assertFalse((self.root / 'contracts' / 'sample.json').exists())

    def test_atomic_bound_version_history_and_modes(self):
        first = self.record()
        target = self.root / 'contracts' / 'sample.json'
        old = target.read_bytes()
        self.assertTrue(first['bound'])
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode) & 0o077, 0)
        second = self.record()
        self.assertEqual(second['version'], first['version'] + 1)
        self.assertEqual(second['supersedes'], first['version'])
        self.assertEqual(core.load_contract(self.root, 'sample'), second)
        self.assertEqual(core.list_contracts(self.root), [second])
        self.assertIn(old, [p.read_bytes() for p in (target.parent / 'history').rglob('*.json')])

    def test_failed_write_preserves_previous_active_bytes(self):
        c = self.record(); path = self.root / 'contracts' / 'sample.json'; old = path.read_bytes()
        with patch('os.replace', side_effect=OSError('simulated failed replacement')), self.assertRaises((core.ContractError, OSError)):
            core.save_contract(self.root, c, new_version=True)
        self.assertEqual(path.read_bytes(), old)
        self.assertTrue(core.load_contract(self.root, 'sample')['bound'])

    def test_post_replace_fsync_failure_restores_prior_contract(self):
        c = self.record(); path = self.root / 'contracts' / 'sample.json'; old = path.read_bytes()
        real_fsync = os.fsync
        failed = False
        def fsync_once(fd):
            nonlocal failed
            if not failed and stat.S_ISDIR(os.fstat(fd).st_mode) and path.read_bytes() != old:
                failed = True
                raise OSError('simulated directory fsync failure')
            return real_fsync(fd)
        with patch('os.fsync', side_effect=fsync_once), self.assertRaises(core.ContractError):
            core.save_contract(self.root, c, new_version=True)
        self.assertTrue(failed)
        self.assertEqual(path.read_bytes(), old)

    def test_secret_refusal_before_storage_or_activation(self):
        for i, secret in enumerate(({'password': 'opaque'}, {'nested': {'api_key': 'opaque'}},
                                    {'text': 'Bearer ' + 'a' * 24})):
            r = core.Recorder(self.root / str(i)); r.start('sample', 'test')
            with self.assertRaises(core.ContractError):
                r.note_tool('stock_move', secret, {'ok': True})
                r.stop(bound=True)
            self.assertFalse((self.root / str(i) / 'contracts' / 'sample.json').exists())
        c = self.record(); path = self.root / 'contracts' / 'sample.json'; old = path.read_bytes()
        c['steps'][0]['args']['authorization'] = 'opaque'
        with self.assertRaises(core.ContractError):
            core.save_contract(self.root, c)
        self.assertEqual(path.read_bytes(), old)

    def test_secret_in_unused_read_result_must_not_be_discarded(self):
        r = core.Recorder(self.root); r.start('sample', 'test')
        with self.assertRaises(core.ContractError):
            r.note_tool('get_status', {}, {'payload': {'private-key': 'opaque'}})
            r.note_tool('stock_move', {}, {'ok': True}); r.stop(bound=True)
        self.assertFalse((self.root / 'contracts' / 'sample.json').exists())

    def test_predictable_temp_symlink_cannot_touch_external_file(self):
        self.root.mkdir(); directory = self.root / 'contracts'; directory.mkdir()
        victim = Path(self.temp.name) / 'victim'; victim.write_text('KEEP')
        (directory / 'sample.json.tmp').symlink_to(victim)
        self.record()
        self.assertEqual(victim.read_text(), 'KEEP')
        self.assertFalse((directory / 'sample.json').is_symlink())

    def test_successful_last_check_does_not_clear_unproven_state(self):
        c = self.record()
        core.update_contract_state(self.root, 'sample', expected_version=c['version'], evidence_status='unproven')
        after = core.update_contract_state(self.root, 'sample', expected_version=c['version'], last_check={'verified': True})
        self.assertEqual(after['evidence_status'], 'unproven')
        verdict = core.check_contract(after, runtime={}, proposed_call={'tool': 'stock_move', 'args': {'qty': 1}})
        self.assertEqual(verdict['action'], 'block')

    def test_symlinked_contract_and_parent_rejected(self):
        outside = Path(self.temp.name) / 'outside'; outside.mkdir()
        (outside / 'sample.json').write_text('{}')
        self.root.mkdir()
        (self.root / 'contracts').symlink_to(outside, target_is_directory=True)
        for operation in (lambda: core.load_contract(self.root, 'sample'),
                          lambda: core.list_contracts(self.root), lambda: self.record()):
            with self.assertRaises(core.ContractError): operation()
        self.assertEqual((outside / 'sample.json').read_text(), '{}')

    def test_symlinked_target_invalid_name_and_malformed_are_not_skipped(self):
        self.record(); directory = self.root / 'contracts'
        outside = Path(self.temp.name) / 'outside.json'; outside.write_text('{}')
        (directory / 'linked.json').symlink_to(outside)
        with self.assertRaises(core.ContractError): core.load_contract(self.root, 'linked')
        with self.assertRaises(core.ContractError): core.list_contracts(self.root)
        (directory / 'linked.json').unlink(); (directory / 'broken.json').write_text('{')
        with self.assertRaises(core.ContractError): core.list_contracts(self.root)
        with self.assertRaises(core.ContractError): core.load_contract(self.root, '../outside')

    def test_minimal_legacy_metadata_can_be_rebound_to_schema_two(self):
        self.root.mkdir(); directory = self.root / 'contracts'; directory.mkdir()
        path = directory / 'sample.json'
        old = b'{"contract_id":"sample","version":5,"bound":true}\n'
        path.write_bytes(old)
        self.assertEqual(core.load_contract(self.root, 'sample')['version'], 5)
        self.assertEqual(len(core.list_contracts(self.root)), 1)
        new = self.record()
        self.assertEqual((new['schema_version'], new['version'], new['bound']), (2, 6, True))
        self.assertIn(old, [p.read_bytes() for p in (directory / 'history').rglob('*.json')])

    def test_late_version_outcome_does_not_overwrite_rebind(self):
        old = self.record(); new = self.record()
        result = core.update_contract_state(self.root, 'sample', expected_version=old['version'], evidence_status='unproven')
        self.assertIsNone(result)
        self.assertEqual(core.load_contract(self.root, 'sample'), new)
        changed = core.update_contract_state(self.root, 'sample', expected_version=new['version'], evidence_status='unproven', bound=False)
        self.assertFalse(changed['bound'])
        self.assertEqual(changed['version'], new['version'])
        with self.assertRaises(core.ContractError):
            core.update_contract_state(self.root, 'sample', version=500)

    def test_cross_thread_and_process_serialized_versions(self):
        c = self.record(); versions = []; errors = []
        def worker():
            try: versions.append(core.save_contract(self.root, copy.deepcopy(c), new_version=True)['version'])
            except (core.ContractError, OSError, TypeError, ValueError) as exc: errors.append(str(exc))
        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertFalse(errors)
        self.assertEqual(sorted(versions), list(range(2, 8)))
        ctx = multiprocessing.get_context('fork'); q = ctx.Queue()
        processes = [ctx.Process(target=concurrent_save, args=(str(self.root), c, q)) for _ in range(4)]
        for p in processes: p.start()
        for p in processes: p.join(15); self.assertEqual(p.exitcode, 0)
        self.assertEqual(sorted(q.get(timeout=2) for _ in processes), list(range(8, 12)))
        self.assertEqual(core.load_contract(self.root, 'sample')['version'], 11)


if __name__ == '__main__':
    unittest.main()
