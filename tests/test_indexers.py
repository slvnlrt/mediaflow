"""Indexer diagnostics must distinguish identity, status and source without mutating either app."""
import unittest
from unittest.mock import Mock

from mediaflow_lib.qui import diagnose_indexers, _discover_indexers


class IndexerDiagnostics(unittest.TestCase):
    def setUp(self):
        self.ctx = Mock()
        self.ctx.cfg = {'secrets': {'prowlarr': 'fixture'}}
        self.base = 'http://prowlarr:9696'
        self.ctx.internal.return_value = self.base
        self.p = self.ctx.api.return_value
        self.p.request.return_value = [
            {'id': i, 'protocol': protocol, 'enable': active} for i, protocol, active in
            [(1, 'torrent', True), (2, 'torrent', True), (3, 'torrent', False), (4, 'usenet', True)]
        ]
        self.q = Mock()

    def row(self, i, enabled=True, **changes):
        return dict({'base_url': self.base + '/', 'backend': 'prowlarr', 'indexer_id': str(i), 'enabled': enabled}, **changes)

    def test_equal_totals_hide_mismatches_and_personal_sources_are_ignored(self):
        self.q.request.return_value = [self.row(1, False), self.row(3), self.row(2, base_url='http://other.invalid'),
                                       self.row(4, backend='native'), self.row('sensitive-value')]
        diagnose_indexers(self.ctx, self.q)
        messages = [call.args[0] for call in self.ctx.note.call_args_list]
        self.assertTrue(any('absents de QUI' in m and '[2]' in m for m in messages))
        self.assertTrue(any('désactivés dans QUI' in m and '[1]' in m for m in messages))
        self.assertTrue(any('inactifs dans Prowlarr' in m and '[3]' in m for m in messages))
        self.assertNotIn('sensitive-value', ' '.join(messages))
        self.assertNotIn('other.invalid', ' '.join(messages))
        self.assertNotIn('[4]', ' '.join(messages))
        for api in (self.q, self.p):
            self.assertTrue(all(call.args[0] == 'GET' for call in api.request.call_args_list))

    def test_discovery_only_adds_missing_enabled_torrents_and_never_reenables(self):
        discovered = [{'id': str(i), 'name': f'fixture-{i}', 'backend': 'prowlarr'} for i in range(1, 5)]
        self.q.request.side_effect = [{'indexers': discovered}, [self.row(1, False)], {'id': 99}]
        _discover_indexers(self.q, self.ctx, self.ctx.cfg, {})
        changes = [c.args for c in self.q.request.call_args_list if c.args[:2] != ('POST', '/api/torznab/indexers/discover') and c.args[0] != 'GET']
        self.assertEqual([(m, p, body['indexer_id']) for m, p, body in changes], [('POST', '/api/torznab/indexers', '2')])


if __name__ == '__main__':
    unittest.main()
