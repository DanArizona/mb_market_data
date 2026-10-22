"""Fast scalar traversal must preserve metadata validation and detachment."""
from collections import UserDict
from datetime import date, datetime, timezone
from types import MappingProxyType
import unittest

from mb_market_data.sampling_membership import SamplingHierarchyRevision


def value(metadata):
    return SamplingHierarchyRevision(session_date=date(2026, 11, 2), revision=0,
        effective_at=datetime(2026, 11, 2, 15, tzinfo=timezone.utc),
        uni_symbols=('AAPL',), focus_symbols=(), hot_symbols=(), source='test', metadata=metadata)


class MetadataValidationTests(unittest.TestCase):
    def test_nested_mappings_and_flat_scalars_keep_canonical_identity(self):
        normal = value({'rows': [None, True, 2, 1.25, '東京'], 'nested': {'empty': []}})
        wrapped = value(UserDict(rows=(None, True, 2, 1.25, '東京'),
            nested=MappingProxyType({'empty': ()})))
        self.assertEqual(normal.content_sha256, wrapped.content_sha256)
        self.assertEqual(normal.metadata, wrapped.metadata)

    def test_caller_mutation_cannot_change_frozen_value_or_hash(self):
        data = {'rows': ['AAPL', 'MSFT'], 'nested': [{'x': [1, 2]}]}
        revision = value(data); sha = revision.content_sha256
        data['rows'].append('changed'); data['nested'][0]['x'].append(3)
        self.assertEqual(revision.metadata['rows'], ('AAPL', 'MSFT'))
        self.assertEqual(revision.metadata['nested'][0]['x'], (1, 2))
        self.assertEqual(revision.content_sha256, sha)

    def test_nonfinite_numbers_remain_rejected_in_flat_and_nested_arrays(self):
        for number in (float('inf'), float('-inf'), float('nan')):
            for data in ({'rows': [number]}, {'rows': [{'x': number}]}):
                with self.subTest(data=data), self.assertRaises(ValueError):
                    value(data)

    def test_nonstring_keys_remain_rejected_in_nested_metadata(self):
        for data in ({1: 'invalid'}, {'rows': [{1: 'invalid'}]}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                value(data)

    def test_unsupported_values_remain_rejected_after_valid_scalar_lists(self):
        value({'rows': ['AAPL', 'MSFT']})
        for item in (object(), {1, 2}, complex(1, 2)):
            with self.subTest(item=item), self.assertRaises(ValueError):
                value({'rows': ['AAPL', item]})


if __name__ == '__main__':
    unittest.main()
