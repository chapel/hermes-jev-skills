import json
import unittest
from jev_skills.catalog import load_catalog


class Context:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def dispatch_tool(self, name, args):
        self.calls.append((name, args))
        return json.dumps(self.payload)


class CatalogTests(unittest.TestCase):
    def test_metadata_only_sorted_and_no_body_loading(self):
        ctx = Context({'success': True, 'count': 2, 'skills': [
            {'name': 'plugin:video', 'description': 'Exact video description.'},
            {'name': 'pdf', 'description': 'Exact PDF description.'}]})
        self.assertEqual(load_catalog(ctx), [
            {'name': 'pdf', 'description': 'Exact PDF description.'},
            {'name': 'plugin:video', 'description': 'Exact video description.'}])
        self.assertEqual(ctx.calls, [('skills_list', {})])

    def test_incomplete_or_ambiguous_catalog_rejected(self):
        for payload in [
            {'success': False}, {'success': True, 'skills': [], 'count': 1},
            {'success': True, 'count': 2, 'skills': [
                {'name': 'same', 'description': 'A'}, {'name': 'same', 'description': 'B'}]},
        ]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                load_catalog(Context(payload))

    def test_missing_description_not_invented(self):
        self.assertEqual(load_catalog(Context({'success': True, 'count': 1,
            'skills': [{'name': 'no-description', 'description': ''}]})), [])


if __name__ == '__main__':
    unittest.main()
