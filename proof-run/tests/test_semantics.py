"""Regression tests for strict, observed-only Proof Run contracts."""
import copy
import inspect
import json
import tempfile
import unittest
from pathlib import Path

from proof_run import core


class SemanticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def record(self, calls, *, bound=True):
        recorder = core.Recorder(self.root)
        recorder.start('sample', 'test run')
        for item in calls:
            recorder.note_tool(*item[:3], **(item[3] if len(item) > 3 else {}))
        if 'bound' in inspect.signature(recorder.stop).parameters:
            return recorder.stop(bound=bound)
        return recorder.stop()  # exercise the legacy implementation in RED

    def verdict(self, contract, args, runtime=None, tool='stock_move'):
        return core.check_contract(contract, runtime=runtime or {},
                                   proposed_call={'tool': tool, 'args': args})

    def test_baseline_extra_and_unrecorded_combinations_block(self):
        c = self.record([('stock_move', {'src': 'A', 'dst': 'safe'}, {'ok': True}),
                         ('stock_move', {'src': 'B', 'dst': 'vault'}, {'ok': True})])
        self.assertEqual(self.verdict(c, {'src': 'A', 'dst': 'vault'})['action'], 'block')
        self.assertEqual(self.verdict(c, {'src': 'A', 'dst': 'safe', 'force': True})['action'], 'block')

    def test_baseline_nonzero_exit_is_rejected(self):
        r = core.Recorder(self.root); r.start('sample', 'intent')
        r.note_tool('stock_move', {}, {'exit_code': 1})
        with self.assertRaises(core.ContractError):
            r.stop()

    def test_partial_v2_without_schema_is_not_legacy(self):
        c = self.record([('stock_move', {'qty': 1}, {'ok': True})])
        del c['schema_version']
        self.assertEqual(self.verdict(c, {'qty': 1, 'force': True})['action'], 'block')

    def test_empty_result_fields_are_not_success_evidence(self):
        for value in (None, '', [], {}):
            with self.subTest(value=value):
                r = core.Recorder(self.root); r.start('sample', 'intent')
                r.note_tool('stock_move', {}, {'output': value})
                with self.assertRaises(core.ContractError): r.stop()

    def test_exact_complete_tuples_and_json_types(self):
        c = self.record([('stock_move', {'src': 'A', 'dst': 'safe', 'cfg': [None, {'x': True}]}, {'ok': True}),
                         ('stock_move', {'src': 'B', 'dst': 'vault', 'cfg': [None, {'x': True}]}, {'ok': True}),
                         ('stock_move', {'qty': 1}, {'ok': True}),
                         ('stock_move', {'qty': 3}, {'ok': True})])
        self.assertEqual(c['schema_version'], 2)
        for args in ({'src': 'A', 'dst': 'safe', 'cfg': [None, {'x': True}]}, {'qty': 1}):
            self.assertEqual(self.verdict(c, args)['action'], 'allow')
        for args in ({'src': 'A', 'dst': 'vault', 'cfg': [None, {'x': True}]},
                     {'qty': 1, 'force': True}, {'qty': 2}, {'qty': True},
                     {'src': 'A', 'dst': 'safe', 'cfg': [None, {'x': 1}]}):
            self.assertEqual(self.verdict(c, args)['action'], 'block')
        self.assertNotEqual(core.fact_key('get_status', {'x': True}), core.fact_key('get_status', {'x': 1}))
        self.assertEqual(core.fact_key('get_status', {'a': 1, 'b': [None]}), core.fact_key('get_status', {'b': [None], 'a': 1}))
        self.assertNotIn('memory', core.READ_ONLY_TOOLS)

    def test_fact_identity_boolean_and_numeric_equality(self):
        c = self.record([('get_status', {'id': 'A'}, {'blocked': False, 'count': 3}),
                         ('stock_move', {'qty': 1}, {'moved': 1})])
        key = core.fact_key('get_status', {'id': 'A'})
        self.assertEqual(self.verdict(c, {'qty': 1}, {key: {'blocked': False, 'count': 3}})['action'], 'allow')
        for facts in ({key: {'blocked': True, 'count': 3}}, {key: {'blocked': False, 'count': 4}},
                      {core.fact_key('get_status', {'id': 'B'}): {'blocked': False, 'count': 3}},
                      {key: {'blocked': 0, 'count': 3}}, {}):
            self.assertEqual(self.verdict(c, {'qty': 1}, facts)['action'], 'block')
        self.assertFalse(core._compare(True, '>=', 1))
        self.assertFalse(core._compare('4', '>=', '3'))
        self.assertTrue(core._compare(4, '>=', 3))

    def test_preconditions_only_since_previous_mutation(self):
        c = self.record([('get_status', {'id': 1}, {'state': 'open'}),
                         ('stock_move', {'qty': 1}, {'moved': 1}),
                         ('stock_move', {'qty': 2}, {'moved': 2}),
                         ('get_status', {'id': 1}, {'state': 'closed'})])
        self.assertEqual(c['steps'][0]['preconditions'][0]['value'], 'open')
        self.assertEqual(c['steps'][1]['preconditions'], [])
        self.assertEqual(self.verdict(c, {'qty': 2})['action'], 'allow')
        self.assertEqual(self.verdict(c, {'qty': 1})['action'], 'block')

    def test_latest_reads_and_alternative_step_specific_facts(self):
        c = self.record([('get_status', {'id': 1}, {'state': 'stale'}),
                         ('get_status', {'id': 1}, {'state': 'open'}),
                         ('stock_move', {'qty': 1}, {'moved': 1}),
                         ('get_status', {'id': 2}, {'state': 'ready'}),
                         ('stock_move', {'qty': 1}, {'moved': 1})])
        self.assertEqual(len(c['steps'][0]['preconditions']), 1)
        key = core.fact_key('get_status', {'id': 2})
        self.assertEqual(self.verdict(c, {'qty': 1}, {key: {'state': 'ready'}}), {'action': 'allow', 'step_index': 1})
        self.assertEqual(self.verdict(c, {'qty': 1}, {key: {'state': 'bad'}})['action'], 'block')

    def test_failure_and_absence_cannot_produce_contract(self):
        cases = [(None, {}), ({}, {}), ('{}', {}), ({'error': 'boom'}, {}),
                 ({'exit_code': 1}, {}), ({'success': False}, {}),
                 ({'ok': True}, {'is_error': True}), ({'ok': True}, {'success': False}),
                 ({'ok': True}, {'status': 'cancelled'}), ({'ok': True}, {'status': 'blocked'}),
                 ({'ok': True}, {'error_type': 'ToolError'}), ({'ok': True}, {'error_message': 'oops'})]
        for i, (result, metadata) in enumerate(cases):
            with self.subTest(result=result, metadata=metadata):
                r = core.Recorder(self.root / str(i)); r.start('sample', 'intent')
                with self.assertRaises(core.ContractError):
                    r.note_tool('stock_move', {'qty': 1}, result, **metadata)
                    r.stop(bound=True)
                self.assertFalse((self.root / str(i) / 'contracts' / 'sample.json').exists())
        c = self.record([('stock_move', {'qty': 1}, {'exit_code': 0})])
        self.assertTrue(core.check_evidence(c['steps'][0], {'exit_code': 0}))
        self.assertFalse(core.check_evidence(c['steps'][0], {'exit_code': 1}))
        self.assertFalse(core.check_evidence(c['steps'][0], {'exit_code': 0}, status='cancelled'))

    def test_evidence_requires_every_observed_field(self):
        c = self.record([('stock_move', {'qty': 3}, {'moved_qty': 3, 'ok': True})])
        step = c['steps'][0]
        self.assertTrue(core.check_evidence(step, {'moved_qty': 3, 'ok': True}))
        for result in ({'moved_qty': 0, 'ok': True}, {'moved_qty': 3}, {'moved_qty': 3, 'ok': 1}, None):
            self.assertFalse(core.check_evidence(step, result))
        c['evidence_status'] = 'unproven'
        self.assertEqual(self.verdict(c, {'qty': 3})['action'], 'block')

    def test_text_observations_are_digested_and_comparable(self):
        blob = 'sensitive page contents should not be serialized' * 5
        c = self.record([('get_status', {}, {'content': blob}),
                         ('stock_move', {}, {'output': blob})])
        self.assertNotIn(blob, json.dumps(c))
        key = core.fact_key('get_status', {})
        self.assertEqual(self.verdict(c, {}, {key: {'content': blob}})['action'], 'allow')
        self.assertEqual(self.verdict(c, {}, {key: {'content': blob + '!'}})['action'], 'block')
        self.assertTrue(core.check_evidence(c['steps'][0], {'output': blob}))
        self.assertFalse(core.check_evidence(c['steps'][0], {'output': blob + '!'}))

    def test_invalid_v2_fails_closed_even_for_missing_safety_fields(self):
        c = self.record([('stock_move', {'qty': 1}, {'ok': True})])
        for field in ('steps', 'action_window', 'preconditions', 'success_evidence', 'provenance', 'bound'):
            broken = copy.deepcopy(c); del broken[field]
            with self.subTest(field=field):
                verdict = self.verdict(broken, {'qty': 1})
                self.assertEqual(verdict['action'], 'block')
        broken = copy.deepcopy(c); broken['steps'][0]['preconditions'] = 'invalid'
        self.assertEqual(self.verdict(broken, {'qty': 1})['action'], 'block')
        self.assertIsNone(self.verdict(c, {}, tool='read_file'))


if __name__ == '__main__':
    unittest.main()
