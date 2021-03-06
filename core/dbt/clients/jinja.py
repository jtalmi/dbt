import codecs
import linecache
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from typing import (
    List, Union, Set, Optional, Dict, Any, Iterator, Type, NoReturn
)

import jinja2
import jinja2.ext
import jinja2.nativetypes  # type: ignore
import jinja2.nodes
import jinja2.parser
import jinja2.sandbox

from dbt.utils import (
    get_dbt_macro_name, get_docs_macro_name, get_materialization_macro_name,
    deep_map
)

from dbt.clients._jinja_blocks import BlockIterator, BlockData, BlockTag
from dbt.contracts.graph.compiled import CompiledSchemaTestNode
from dbt.contracts.graph.parsed import ParsedSchemaTestNode
from dbt.exceptions import (
    InternalException, raise_compiler_error, CompilationException,
    invalid_materialization_argument, MacroReturn
)
from dbt.flags import MACRO_DEBUGGING
from dbt.logger import GLOBAL_LOGGER as logger  # noqa


def _linecache_inject(source, write):
    if write:
        # this is the only reliable way to accomplish this. Obviously, it's
        # really darn noisy and will fill your temporary directory
        tmp_file = tempfile.NamedTemporaryFile(
            prefix='dbt-macro-compiled-',
            suffix='.py',
            delete=False,
            mode='w+',
            encoding='utf-8',
        )
        tmp_file.write(source)
        filename = tmp_file.name
    else:
        # `codecs.encode` actually takes a `bytes` as the first argument if
        # the second argument is 'hex' - mypy does not know this.
        rnd = codecs.encode(os.urandom(12), 'hex')  # type: ignore
        filename = rnd.decode('ascii')

    # put ourselves in the cache
    cache_entry = (
        len(source),
        None,
        [line + '\n' for line in source.splitlines()],
        filename
    )
    # linecache does in fact have an attribute `cache`, thanks
    linecache.cache[filename] = cache_entry  # type: ignore
    return filename


class MacroFuzzParser(jinja2.parser.Parser):
    def parse_macro(self):
        node = jinja2.nodes.Macro(lineno=next(self.stream).lineno)

        # modified to fuzz macros defined in the same file. this way
        # dbt can understand the stack of macros being called.
        #  - @cmcarthur
        node.name = get_dbt_macro_name(
            self.parse_assign_target(name_only=True).name)

        self.parse_signature(node)
        node.body = self.parse_statements(('name:endmacro',),
                                          drop_needle=True)
        return node


class MacroFuzzEnvironment(jinja2.sandbox.SandboxedEnvironment):
    def _parse(self, source, name, filename):
        return MacroFuzzParser(self, source, name, filename).parse()

    def _compile(self, source, filename):
        """Override jinja's compilation to stash the rendered source inside
        the python linecache for debugging when the appropriate environment
        variable is set.

        If the value is 'write', also write the files to disk.
        WARNING: This can write a ton of data if you aren't careful.
        """
        if filename == '<template>' and MACRO_DEBUGGING:
            write = MACRO_DEBUGGING == 'write'
            filename = _linecache_inject(source, write)

        return super()._compile(source, filename)  # type: ignore


class NativeSandboxEnvironment(MacroFuzzEnvironment):
    code_generator_class = jinja2.nativetypes.NativeCodeGenerator


class NativeSandboxTemplate(jinja2.nativetypes.NativeTemplate):  # mypy: ignore
    environment_class = NativeSandboxEnvironment

    def render(self, *args, **kwargs):
        """Render the template to produce a native Python type. If the
        result is a single node, its value is returned. Otherwise, the
        nodes are concatenated as strings. If the result can be parsed
        with :func:`ast.literal_eval`, the parsed value is returned.
        Otherwise, the string is returned.
        """
        vars = dict(*args, **kwargs)
        try:
            return jinja2.nativetypes.native_concat(
                self.root_render_func(self.new_context(vars)),
                preserve_quotes=True
            )
        except Exception:
            return self.environment.handle_exception()


NativeSandboxEnvironment.template_class = NativeSandboxTemplate  # type: ignore


class TemplateCache:

    def __init__(self):
        self.file_cache = {}

    def get_node_template(self, node):
        key = (node.package_name, node.original_file_path)

        if key in self.file_cache:
            return self.file_cache[key]

        template = get_template(
            string=node.raw_sql,
            ctx={},
            node=node,
        )
        self.file_cache[key] = template

        return template

    def clear(self):
        self.file_cache.clear()


template_cache = TemplateCache()


class BaseMacroGenerator:
    def __init__(self, context: Optional[Dict[str, Any]] = None) -> None:
        self.context: Optional[Dict[str, Any]] = context

    def get_template(self):
        raise NotImplementedError('get_template not implemented!')

    def get_name(self) -> str:
        raise NotImplementedError('get_name not implemented!')

    def get_macro(self):
        name = self.get_name()
        template = self.get_template()
        # make the module. previously we set both vars and local, but that's
        # redundant: They both end up in the same place
        module = template.make_module(vars=self.context, shared=False)
        macro = module.__dict__[get_dbt_macro_name(name)]
        module.__dict__.update(self.context)
        return macro

    @contextmanager
    def exception_handler(self) -> Iterator[None]:
        try:
            yield
        except (TypeError, jinja2.exceptions.TemplateRuntimeError) as e:
            raise_compiler_error(str(e))

    def call_macro(self, *args, **kwargs):
        if self.context is None:
            raise InternalException(
                'Context is still None in call_macro!'
            )
        assert self.context is not None

        macro = self.get_macro()

        with self.exception_handler():
            try:
                return macro(*args, **kwargs)
            except MacroReturn as e:
                return e.value


class MacroStack(threading.local):
    def __init__(self):
        super().__init__()
        self.call_stack = []

    @property
    def depth(self) -> int:
        return len(self.call_stack)

    def push(self, name):
        self.call_stack.append(name)

    def pop(self, name):
        got = self.call_stack.pop()
        if got != name:
            raise InternalException(f'popped {got}, expected {name}')


class MacroGenerator(BaseMacroGenerator):
    def __init__(
        self,
        macro,
        context: Optional[Dict[str, Any]] = None,
        node: Optional[Any] = None,
        stack: Optional[MacroStack] = None
    ) -> None:
        super().__init__(context)
        self.macro = macro
        self.node = node
        self.stack = stack

    def get_template(self):
        return template_cache.get_node_template(self.macro)

    def get_name(self) -> str:
        return self.macro.name

    @contextmanager
    def exception_handler(self) -> Iterator[None]:
        try:
            yield
        except (TypeError, jinja2.exceptions.TemplateRuntimeError) as e:
            raise_compiler_error(str(e), self.macro)
        except CompilationException as e:
            e.stack.append(self.macro)
            raise e

    @contextmanager
    def track_call(self):
        if self.stack is None or self.node is None:
            yield
        else:
            unique_id = self.macro.unique_id
            depth = self.stack.depth
            # only mark depth=0 as a dependency
            if depth == 0:
                self.node.depends_on.add_macro(unique_id)
            self.stack.push(unique_id)
            try:
                yield
            finally:
                self.stack.pop(unique_id)

    def __call__(self, *args, **kwargs):
        with self.track_call():
            return self.call_macro(*args, **kwargs)


class QueryStringGenerator(BaseMacroGenerator):
    def __init__(
        self, template_str: str, context: Dict[str, Any]
    ) -> None:
        super().__init__(context)
        self.template_str: str = template_str
        env = get_environment()
        self.template = env.from_string(
            self.template_str,
            globals=self.context,
        )

    def get_name(self) -> str:
        return 'query_comment_macro'

    def get_template(self):
        """Don't use the template cache, we don't have a node"""
        return self.template

    def __call__(self, connection_name: str, node) -> str:
        return str(self.call_macro(connection_name, node))


class MaterializationExtension(jinja2.ext.Extension):
    tags = ['materialization']

    def parse(self, parser):
        node = jinja2.nodes.Macro(lineno=next(parser.stream).lineno)
        materialization_name = \
            parser.parse_assign_target(name_only=True).name

        adapter_name = 'default'
        node.args = []
        node.defaults = []

        while parser.stream.skip_if('comma'):
            target = parser.parse_assign_target(name_only=True)

            if target.name == 'default':
                pass

            elif target.name == 'adapter':
                parser.stream.expect('assign')
                value = parser.parse_expression()
                adapter_name = value.value

            else:
                invalid_materialization_argument(
                    materialization_name, target.name
                )

        node.name = get_materialization_macro_name(
            materialization_name, adapter_name
        )

        node.body = parser.parse_statements(('name:endmaterialization',),
                                            drop_needle=True)

        return node


class DocumentationExtension(jinja2.ext.Extension):
    tags = ['docs']

    def parse(self, parser):
        node = jinja2.nodes.Macro(lineno=next(parser.stream).lineno)
        docs_name = parser.parse_assign_target(name_only=True).name

        node.args = []
        node.defaults = []
        node.name = get_docs_macro_name(docs_name)
        node.body = parser.parse_statements(('name:enddocs',),
                                            drop_needle=True)
        return node


def _is_dunder_name(name):
    return name.startswith('__') and name.endswith('__')


def create_undefined(node=None):
    class Undefined(jinja2.Undefined):
        def __init__(self, hint=None, obj=None, name=None, exc=None):
            super().__init__(hint=hint, name=name)
            self.node = node
            self.name = name
            self.hint = hint
            # jinja uses these for safety, so we have to override them.
            # see https://github.com/pallets/jinja/blob/master/jinja2/sandbox.py#L332-L339 # noqa
            self.unsafe_callable = False
            self.alters_data = False

        def __getitem__(self, name):
            # Propagate the undefined value if a caller accesses this as if it
            # were a dictionary
            return self

        def __getattr__(self, name):
            if name == 'name' or _is_dunder_name(name):
                raise AttributeError(
                    "'{}' object has no attribute '{}'"
                    .format(type(self).__name__, name)
                )

            self.name = name

            return self.__class__(hint=self.hint, name=self.name)

        def __call__(self, *args, **kwargs):
            return self

        def __reduce__(self):
            raise_compiler_error(f'{self.name} is undefined', node=node)

    return Undefined


def get_environment(
    node=None,
    capture_macros: bool = False,
    native: bool = False,
) -> jinja2.Environment:
    args: Dict[str, List[Union[str, Type[jinja2.ext.Extension]]]] = {
        'extensions': ['jinja2.ext.do']
    }

    if capture_macros:
        args['undefined'] = create_undefined(node)

    args['extensions'].append(MaterializationExtension)
    args['extensions'].append(DocumentationExtension)

    env_cls: Type[jinja2.Environment]
    if native:
        env_cls = NativeSandboxEnvironment
    else:
        env_cls = MacroFuzzEnvironment

    return env_cls(**args)


@contextmanager
def catch_jinja(node=None) -> Iterator[None]:
    try:
        yield
    except jinja2.exceptions.TemplateSyntaxError as e:
        e.translated = False
        raise CompilationException(str(e), node) from e
    except jinja2.exceptions.UndefinedError as e:
        raise CompilationException(str(e), node) from e
    except CompilationException as exc:
        exc.add_node(node)
        raise


def parse(string):
    with catch_jinja():
        return get_environment().parse(str(string))


def get_template(
    string: str,
    ctx: Dict[str, Any],
    node=None,
    capture_macros: bool = False,
    native: bool = False,
):
    with catch_jinja(node):
        env = get_environment(node, capture_macros, native=native)

        template_source = str(string)
        return env.from_string(template_source, globals=ctx)


def render_template(template, ctx: Dict[str, Any], node=None) -> str:
    with catch_jinja(node):
        return template.render(ctx)


def get_rendered(
    string: str,
    ctx: Dict[str, Any],
    node=None,
    capture_macros: bool = False,
    native: bool = False,
) -> str:
    template = get_template(
        string,
        ctx,
        node,
        capture_macros=capture_macros,
        native=native,
    )

    return render_template(template, ctx, node)


def undefined_error(msg) -> NoReturn:
    raise jinja2.exceptions.UndefinedError(msg)


def extract_toplevel_blocks(
    data: str,
    allowed_blocks: Optional[Set[str]] = None,
    collect_raw_data: bool = True,
) -> List[Union[BlockData, BlockTag]]:
    """Extract the top level blocks with matching block types from a jinja
    file, with some special handling for block nesting.

    :param data: The data to extract blocks from.
    :param allowed_blocks: The names of the blocks to extract from the file.
        They may not be nested within if/for blocks. If None, use the default
        values.
    :param collect_raw_data: If set, raw data between matched blocks will also
        be part of the results, as `BlockData` objects. They have a
        `block_type_name` field of `'__dbt_data'` and will never have a
        `block_name`.
    :return: A list of `BlockTag`s matching the allowed block types and (if
        `collect_raw_data` is `True`) `BlockData` objects.
    """
    return BlockIterator(data).lex_for_blocks(
        allowed_blocks=allowed_blocks,
        collect_raw_data=collect_raw_data
    )


SCHEMA_TEST_KWARGS_NAME = '_dbt_schema_test_kwargs'


def add_rendered_test_kwargs(
    context: Dict[str, Any],
    node: Union[ParsedSchemaTestNode, CompiledSchemaTestNode],
    capture_macros: bool = False,
) -> None:
    """Render each of the test kwargs in the given context using the native
    renderer, then insert that value into the given context as the special test
    keyword arguments member.
    """
    looks_like_func = r'^\s*(env_var|ref|var|source|doc)\s*\(.+\)\s*$'

    # we never care about the keypath
    def _convert_function(value: Any, _: Any) -> Any:
        if isinstance(value, str):
            if re.match(looks_like_func, value) is not None:
                # curly braces to make rendering happy
                value = f'{{{{ {value} }}}}'

            value = get_rendered(
                value, context, node, capture_macros=capture_macros,
                native=True
            )

        return value

    kwargs = deep_map(_convert_function, node.test_metadata.kwargs)
    context[SCHEMA_TEST_KWARGS_NAME] = kwargs
