import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "boat-chat"))
import json
import unittest
from unittest.mock import patch
import app


class AnswerQualityTests(unittest.TestCase):
    def context(self, mode='concise'):
        return {'boat_facts': {}, 'answer_mode': mode, 'query_plan': {}}

    def test_simple_numeric_answer_avoids_model(self):
        with patch.object(app, 'answer_from_facts', return_value='42 gal.'), patch.object(app, 'call_model') as model:
            self.assertEqual(app.generate_answer('Fuel?', self.context()), '42 gal.')
            model.assert_not_called()

    def test_modes_and_followups_use_calculation_as_evidence(self):
        for extra in ({'answer_mode': 'explain'}, {'answer_mode': 'diagnose'},
                      {'answer_mode': 'checklist'}, {'interpreted_question': 'Fuel yesterday; why?'},
                      {'query_plan': {'complex': True}}):
            context = dict(self.context(), **extra)
            with self.subTest(extra=extra), patch.object(app, 'answer_from_facts', return_value='42 gal.'), patch.object(app, 'call_model', return_value='Detailed answer.') as model:
                self.assertEqual(app.generate_answer('Fuel?', context), 'Detailed answer.')
                self.assertEqual(model.call_args.args[1]['computed_answer'], '42 gal.')
                self.assertEqual(app.compact_context(context)['computed_answer'], '42 gal.')

    def test_model_failure_preserves_computed_result(self):
        with patch.object(app, 'answer_from_facts', return_value='42 gal.'), patch.object(app, 'call_model', return_value=app.fallback_answer('Fuel?', {}, 'offline')):
            self.assertEqual(app.generate_answer('Fuel?', self.context('explain')), '42 gal.')

    def test_clarification_is_not_reinterpreted(self):
        with patch.object(app, 'answer_from_clarification', return_value='Which trip?'), patch.object(app, 'call_model') as model:
            self.assertEqual(app.generate_answer('Fuel?', self.context('diagnose')), 'Which trip?')
            model.assert_not_called()

    def test_budget_keeps_extrema_and_computed_values_over_bulk_samples(self):
        context = {'query_plan': {'signals': ['battery']}, 'computed_answer': 'Minimum 12.1 V at 06:00.',
                   'battery_voltage': {'min': 12.1, 'min_time': '06:00', 'samples': [{'value': 13, 'padding': 'x'*200}]*100},
                   'local_docs': [{'excerpt': 'x'*5000}]}
        decoded = json.loads(app.serialize_context_with_budget(context, 600))
        self.assertEqual(decoded['battery_voltage']['min_time'], '06:00')
        self.assertEqual(decoded['computed_answer'], context['computed_answer'])
        self.assertTrue(decoded['context_trimming']['incomplete'])
        self.assertNotIn('samples', decoded['battery_voltage'])
        self.assertLessEqual(len(app.serialize_context_with_budget(context, 600)), 600)

    def test_mode_instruction_requires_complete_answer(self):
        self.assertIn('Answer every part', app.build_prompt('Compare batteries', self.context()))


if __name__ == '__main__':
    unittest.main()
