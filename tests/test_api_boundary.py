"""ADR-0030: definition ownership, wire parity, loader and connection invariants."""
import ast
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from api import index
from pulse import config
from pulse.api import auth, http_utils, handlers as http_handlers
from pulse.legacy import service, cache, generation, publisher, signal_state, reader, health
from pulse.legacy import scaling, enrichment, pipeline, fast_hydrate, dispatch, refresh
from pulse.store import redis_store
from tests.api_boundary_fixture import wire_outputs
from tests.api_boundary_parity import BINDINGS, METHOD_BINDINGS, original_definition, verify_wrapper, baseline_dump


ROOT = Path(__file__).resolve().parents[1]
BASELINE = json.loads((ROOT / 'tests/fixtures/api_boundary_baseline.json').read_text())


class ApiBoundaryTests(unittest.TestCase):
    def test_every_pre_move_definition_has_one_unchanged_owner(self):
        definitions = {}
        owners = {}
        canonical_nodes = {}
        wrappers = {}
        method_nodes = {
            node.name: original_definition(node, METHOD_BINDINGS[node.name])
            for method_module in (refresh, http_handlers)
            for node in ast.parse(Path(method_module.__file__).read_text()).body
            if isinstance(node, ast.FunctionDef) and node.name in METHOD_BINDINGS
        }
        self.assertEqual(set(method_nodes), set(METHOD_BINDINGS))
        method_owners = set()
        for module in (service, config, redis_store, auth, http_utils, cache, generation, publisher, signal_state, reader, health,
                       scaling, enrichment, pipeline, fast_hydrate, dispatch, refresh, http_handlers):
            source = Path(module.__file__).read_text()
            source = source.replace('(ROOT / "api")', 'Path(__file__).parent')
            for node in ast.parse(source).body:
                if not isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Assign)):
                    continue
                name = node.name if hasattr(node, 'name') else node.targets[0].id
                if name == 'ROOT':
                    continue
                if name in METHOD_BINDINGS:
                    spec = METHOD_BINDINGS[name]
                    self.assertEqual(module.__name__, spec.get('module_path', 'pulse.legacy.' + spec['module']))
                    self.assertNotIn(name, method_owners)
                    method_owners.add(name)
                    continue
                if name == 'handler':
                    restored = set()
                    for i, method in enumerate(node.body):
                        if isinstance(method, ast.FunctionDef) and method.name in METHOD_BINDINGS:
                            verify_wrapper(method, method_nodes[method.name], METHOD_BINDINGS[method.name])
                            node.body[i] = method_nodes[method.name]
                            restored.add(method.name)
                    self.assertEqual(restored, set(METHOD_BINDINGS))
                if name in BINDINGS:
                    spec = BINDINGS[name]
                    if module is service:
                        self.assertTrue(spec['bindings'], f'Pure helper must be re-exported: {name}')
                        wrappers[name] = node
                        continue
                    self.assertEqual(module.__name__, 'pulse.legacy.' + spec['module'])
                    node = original_definition(node, spec)
                    canonical_nodes[name] = node
                self.assertNotIn(name, definitions, f'Duplicate implementation: {name}')
                definitions[name] = hashlib.sha256(baseline_dump(node).encode()).hexdigest()
                owners[name] = module.__name__
        self.assertEqual(definitions, BASELINE['definitions'])
        self.assertEqual(method_owners, set(METHOD_BINDINGS))
        self.assertEqual(set(canonical_nodes), set(BINDINGS))
        self.assertEqual(set(wrappers), {name for name, spec in BINDINGS.items() if spec['bindings']})
        for name, wrapper in wrappers.items():
            verify_wrapper(wrapper, canonical_nodes[name], BINDINGS[name])
        self.assertEqual(owners['VercelStorage'], 'pulse.store.redis_store')
        self.assertEqual(owners['_authorise_secret'], 'pulse.api.auth')
        self.assertEqual(owners['sanitize_utm_string'], 'pulse.api.http_utils')
        self.assertEqual(owners['SIGNAL_STATE_KEY_PREFIX'], 'pulse.config')
        self.assertEqual(owners['_CACHE'], 'pulse.legacy.service')
        self.assertEqual(owners['_publish_generation'], 'pulse.legacy.publisher')
        self.assertEqual(owners['_try_serve_from_cache'], 'pulse.legacy.cache')
        self.assertEqual(owners['_validate_generation_set'], 'pulse.legacy.generation')
        self.assertEqual(owners['_attach_signal_states'], 'pulse.legacy.signal_state')
        self.assertEqual(owners['_resolve_target_report'], 'pulse.legacy.reader')
        self.assertEqual(owners['_generation_health_snapshot'], 'pulse.legacy.health')
        self.assertEqual(owners['scale_and_score_report'], 'pulse.legacy.scaling')
        self.assertEqual(owners['_get_cached_oi'], 'pulse.legacy.enrichment')
        self.assertEqual(owners['_perform_optimized_pipeline'], 'pulse.legacy.pipeline')
        self.assertEqual(owners['_fast_hydrate'], 'pulse.legacy.fast_hydrate')
        self.assertEqual(owners['_get_cached_or_compute'], 'pulse.legacy.dispatch')

    def test_all_fifteen_http_wire_baselines_are_unchanged(self):
        self.assertEqual(len(BASELINE['http']), 15)
        self.assertEqual(wire_outputs(service), BASELINE['http'])

    def test_import_alias_preserves_patch_seams_and_single_state(self):
        self.assertIs(index, service)
        self.assertIs(index.VercelStorage, redis_store.VercelStorage)
        self.assertIs(index._CACHE, service._CACHE)
        self.assertIs(index._OI_CACHE, service._OI_CACHE)
        self.assertIs(index.ALLOWED_COMBINATIONS, config.ALLOWED_COMBINATIONS)
        self.assertEqual(service.ROOT, ROOT)
        with patch('api.index._refresh_backoff_status', return_value={'fixture': True}) as backoff:
            self.assertEqual(service._refresh_backoff_status(), {'fixture': True})
        backoff.assert_called_once()

    def test_spec_loader_can_retain_adapter_object_and_still_get_handler(self):
        name = '_pulse_test_vercel_loader'
        spec = importlib.util.spec_from_file_location(name, ROOT / 'api/index.py')
        adapter = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(adapter)
            self.assertIs(adapter.handler, service.handler)
            self.assertIs(sys.modules[name], service)
        finally:
            sys.modules.pop(name, None)

    def test_import_adapter_without_any_path_to_live_redis(self):
        script = '''
import os, sys, types
from unittest.mock import Mock
factory = Mock()
sys.modules['redis'] = types.SimpleNamespace(Redis=types.SimpleNamespace(from_url=factory))
os.environ['REDIS_URL'] = 'redis://test-only.invalid'
from api import index
from pulse.legacy import service
from pulse.store import redis_store
assert index is service
assert index.VercelStorage is redis_store.VercelStorage
assert index.handler is service.handler
assert factory.call_count == 0, 'Import initialized Redis'
assert redis_store.VercelStorage._client is None
'''
        result = subprocess.run([sys.executable, '-B', '-c', script], cwd=ROOT,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fallback_search_priority_and_corrupt_file_continuation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'api').mkdir()
            paths = (root / 'api/volatility_lw_report_60.json', root / 'volatility_lw_report_60.json',
                     root / 'api/volatility_lw_report.json', root / 'volatility_lw_report.json')
            for i, path in enumerate(paths):
                path.write_text(json.dumps({'fixture_tier': i}))
            with patch.object(service, 'ROOT', root), \
                    patch.object(redis_store.VercelStorage, 'get_client', side_effect=AssertionError('Fallback connected Redis')):
                for i, path in enumerate(paths):
                    self.assertEqual(service.load_precomputed_report('60'), {'fixture_tier': i})
                    path.write_text('{invalid')
                self.assertIsNone(service.load_precomputed_report('60'))

    def test_shared_client_is_initialized_once_under_concurrency(self):
        client = Mock()
        factory = Mock(return_value=client)
        fake_redis = types.SimpleNamespace(Redis=types.SimpleNamespace(from_url=factory))
        cls = redis_store.VercelStorage
        with patch.dict('os.environ', {'REDIS_URL': 'redis://test-only.invalid'}, clear=True), \
                patch.dict(sys.modules, {'redis': fake_redis}), \
                patch.object(cls, '_initialized', False), patch.object(cls, '_client', None), \
                patch.object(cls, '_retry_after', 0):
            with ThreadPoolExecutor(max_workers=16) as workers:
                clients = list(workers.map(lambda _: cls.get_client(), range(64)))
            self.assertTrue(all(value is client for value in clients))
            self.assertIs(index.VercelStorage.get_client(), client)
        factory.assert_called_once_with('redis://test-only.invalid', socket_timeout=3,
                                        socket_connect_timeout=3, decode_responses=False)
        client.ping.assert_called_once()

    def test_connection_failure_keeps_existing_backoff_and_recovery(self):
        client = Mock()
        factory = Mock(side_effect=[OSError('fixture failure'), client])
        fake_redis = types.SimpleNamespace(Redis=types.SimpleNamespace(from_url=factory))
        cls = redis_store.VercelStorage
        with patch.dict('os.environ', {'REDIS_URL': 'redis://test-only.invalid'}, clear=True), \
                patch.dict(sys.modules, {'redis': fake_redis}), \
                patch.object(cls, '_initialized', False), patch.object(cls, '_client', None), \
                patch.object(cls, '_retry_after', 0), patch.object(redis_store.time, 'time', return_value=100) as clock:
            self.assertIsNone(cls.get_client())
            self.assertEqual(cls._retry_after, 105)
            self.assertIsNone(cls.get_client())
            self.assertEqual(factory.call_count, 1)
            clock.return_value = 106
            self.assertIs(cls.get_client(), client)
            self.assertEqual(factory.call_count, 2)

    def test_atomic_batch_and_persistent_pointer_semantics_remain(self):
        client, pipe = Mock(), Mock()
        client.pipeline.return_value = pipe
        with patch.object(redis_store.VercelStorage, 'get_client', return_value=client):
            self.assertTrue(redis_store.VercelStorage.set_many_atomic({'pointer': ('generation', None),
                                                                      'payload': ('data', 60)}))
        client.pipeline.assert_called_once_with(transaction=True)
        self.assertEqual(pipe.set.call_args_list[0].args, ('pointer', b'generation'))
        self.assertEqual(pipe.set.call_args_list[0].kwargs, {})
        self.assertEqual(pipe.set.call_args_list[1].kwargs, {'ex': 60})
        pipe.execute.assert_called_once()


if __name__ == '__main__':
    unittest.main()
