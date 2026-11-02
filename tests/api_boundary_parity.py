"""ADR-0031: validate exact dependency bindings/wrappers before AST comparison."""
import ast
import copy
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BINDINGS = json.loads((ROOT / 'tests/fixtures/legacy_dependency_bindings.json').read_text())
METHOD_BINDINGS = json.loads((ROOT / 'tests/fixtures/legacy_method_bindings.json').read_text())
METHOD_BINDINGS.update(json.loads((ROOT / 'tests/fixtures/http_dependency_bindings.json').read_text()))


def baseline_dump(node):
    """Match the baseline's Python 3.14 AST format on the declared Python 3.12.

    Only empty structural lists and absent optional fields are omitted. Actual
    values, list ordering and populated fields remain part of the original hash.
    """
    if isinstance(node, ast.AST):
        fields = []
        for name, value in ast.iter_fields(node):
            if value is None and getattr(type(node), name, ...) is None:
                continue
            if isinstance(value, list) and not value and not isinstance(node, ast.Constant):
                continue
            fields.append(f'{name}={baseline_dump(value)}')
        return f'{type(node).__name__}({", ".join(fields)})'
    if isinstance(node, list):
        return '[' + ', '.join(baseline_dump(value) for value in node) + ']'
    return repr(node)


def is_docstring(node):
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def same(actual, expected, label):
    if ast.dump(actual, include_attributes=False) != ast.dump(expected, include_attributes=False):
        raise AssertionError(label)


def original_definition(node, spec):
    """Remove only the exact declared required parameters and binding assignments."""
    node = copy.deepcopy(node)
    bindings = spec['bindings']
    if not bindings:
        if spec.get('static'):
            if node.decorator_list:
                raise AssertionError(f'{node.name}: canonical static helper must be undecorated')
            node.decorator_list = [ast.Name(id='staticmethod', ctx=ast.Load())]
        return node
    if [arg.arg for arg in node.args.kwonlyargs] != list(bindings.values()):
        raise AssertionError(f'{node.name}: changed dependency parameters')
    if node.args.kw_defaults != [None] * len(bindings):
        raise AssertionError(f'{node.name}: dependency must be explicit, not default-captured')
    node.args.kwonlyargs = []
    node.args.kw_defaults = []
    position = int(bool(node.body) and is_docstring(node.body[0]))
    for old_name, dependency in bindings.items():
        expected = ast.parse(f'{old_name} = {dependency}').body[0]
        same(node.body[position], expected, f'{node.name}: unexpected dependency binding')
        del node.body[position]
    if spec.get('details_dict'):
        if node.args.kwarg is not None or node.args.args[-1].arg != 'details':
            raise AssertionError('Status details must remain a separate dictionary')
        node.args.kwarg = node.args.args.pop()
    if spec.get('method'):
        node.args.args.insert(0, ast.arg(arg='self'))
        for alias, method in spec.get('receiver_calls', {}).items():
            calls = [item for item in ast.walk(node) if isinstance(item, ast.Call)
                     and isinstance(item.func, ast.Name) and item.func.id == alias]
            if len(calls) != 1:
                raise AssertionError(f'{node.name}: expected exactly one declared receiver call')
            calls[0].func = ast.Attribute(value=ast.Name(id='self', ctx=ast.Load()), attr=method, ctx=ast.Load())
    return node


def verify_wrapper(wrapper, implementation, spec):
    """Composition may only forward the old arguments plus named dependencies."""
    same(wrapper.args, implementation.args, f'{wrapper.name}: legacy signature changed')
    if [ast.dump(item) for item in wrapper.decorator_list] != [ast.dump(item) for item in implementation.decorator_list]:
        raise AssertionError(f'{wrapper.name}: decorators changed')
    position = int(bool(wrapper.body) and is_docstring(wrapper.body[0]))
    if position:
        same(wrapper.body[0], implementation.body[0], f'{wrapper.name}: legacy docstring changed')
    elif is_docstring(implementation.body[0]):
        raise AssertionError(f'{wrapper.name}: lost legacy docstring')
    if len(wrapper.body) != position + 1:
        raise AssertionError(f'{wrapper.name}: wrapper contains additional logic')
    forwarded = [arg.arg for arg in wrapper.args.posonlyargs + wrapper.args.args]
    if spec.get('method'):
        if forwarded.pop(0) != 'self':
            raise AssertionError('Expected original method receiver')
    if spec.get('details_dict'):
        forwarded.append('details')
    for old_name, dependency in spec['bindings'].items():
        expression = 'self.' + spec['receiver_calls'][old_name] if old_name in spec.get('receiver_calls', {}) else old_name
        forwarded.append(f'{dependency}={expression}')
    alias = spec.get('alias', 'legacy_' + spec['module'])
    expected = ast.parse(f'return {alias}.{wrapper.name}({", ".join(forwarded)})').body[0]
    same(wrapper.body[position], expected, f'{wrapper.name}: changed dependency wiring')
