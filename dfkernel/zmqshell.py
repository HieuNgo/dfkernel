"""A ZMQ-based subclass of InteractiveShell.
"""

from __future__ import print_function

from ipykernel.zmqshell import *
import ipykernel.zmqshell

import ast
import collections
import copy
import sys

from IPython.core import magic_arguments
from IPython.core.interactiveshell import InteractiveShellABC, \
    _assign_nodes, _single_targets_nodes
from IPython.core.interactiveshell import ExecutionResult, ExecutionInfo
from IPython.core.compilerop import CachingCompiler
from IPython.core.magic import magics_class, Magics, cell_magic, line_magic, \
    needs_local_scope
from IPython.core.history import HistoryManager
from IPython.core.error import InputRejected
from ipykernel.jsonutil import json_clean, encode_images
from ipython_genutils import py3compat
from ipython_genutils.py3compat import unicode_type
from dfkernel.displayhook import ZMQShellDisplayHook
from dfkernel.safe_attr import safe_attr
from traitlets import (
    Integer, Instance, Type, Unicode, validate
)
from warnings import warn
from typing import List as ListType
from ast import AST
import importlib

from dfkernel.dataflow import DataflowHistoryManager, DataflowFunctionManager, DataflowNamespace
from dfkernel.dflink import build_linked_result

#-----------------------------------------------------------------------------
# Functions and classes
#-----------------------------------------------------------------------------

class ZMQDisplayPublisher(ipykernel.zmqshell.ZMQDisplayPublisher):
    """A display publisher that publishes data using a ZeroMQ PUB socket."""

    def publish(self, data, metadata=None, source=None, transient=None,
            update=False):
        """Publish a display-data message

        Parameters
        ----------
        data: dict
            A mime-bundle dict, keyed by mime-type.
        metadata: dict, optional
            Metadata associated with the data.
        transient: dict, optional, keyword-only
            Transient data that may only be relevant during a live display,
            such as display_id.
            Transient data should not be persisted to documents.
        update: bool, optional, keyword-only
            If True, send an update_display_data message instead of display_data.
        """
        self._flush_streams()
        if metadata is None:
            metadata = {}
        if transient is None:
            transient = {}
        self._validate_data(data, metadata)
        content = {}
        content['data'] = encode_images(data)
        content['metadata'] = metadata
        content['transient'] = transient
        content['execution_count'] = self.get_execution_count()

        msg_type = 'update_display_data' if update else 'display_data'

        # Use 2-stage process to send a message,
        # in order to put it through the transform
        # hooks before potentially sending.
        msg = self.session.msg(
            msg_type, json_clean(content),
            parent=self.parent_header
        )

        # Each transform either returns a new
        # message or None. If None is returned,
        # the message has been 'used' and we return.
        for hook in self._hooks:
            msg = hook(msg)
            if msg is None:
                return

        self.session.send(
            self.pub_socket, msg, ident=self.topic,
        )

@magics_class
class FunctionMagics(Magics):
    @cell_magic
    def func(self, line, cell):
        #FIXME better argument parsing (-i and -o, intelligent split)
        arr = line.split('-o')
        ivars = [v.strip() for v in (arr[0].split(',')
                                     if arr[0].strip() != ""
                                     else [])]
        self.shell.dataflow_function_manager.set_cell_ivars(self.shell.uuid,
                                                            ivars)
        ovars = [v.strip() for v in (arr[1].split(',')
                                     if len(arr) > 1 and arr[1].strip() != ""
                                     else [])]
        self.shell.dataflow_function_manager.set_cell_ovars(self.shell.uuid,
                                                            ovars)
        self.shell.dataflow_function_manager.set_function_body(self.shell.uuid,
                                                               cell)

class nameddict(collections.Mapping):
    def __init__(self, *args, **kwargs):
        self.__raw_mapping__ = {}
        self._fields = []
        self.__field_mapping__ = {}

    def __getattr__(self, item):
        if item in self._fields:
            return self.__field_mapping__[item]
        return getattr(self.__raw_mapping__, item)

    def __getitem__(self, key):
        if key in self._fields:
            return self.__field_mapping__[key]
        return self.__raw_mapping__[key]

    def __iter__(self):
        return iter(self.__raw_mapping__)

    def __len__(self):
        return len(self.__raw_mapping__)

    @staticmethod
    def from_mapping(mapping):
        nd = nameddict()
        nd.__raw_mapping__ = nd
        for key, value in sorted(mapping.items(), key=lambda x: str(x[0])):
            attr = safe_attr(key)
            nd._fields.append(attr)
            nd.__field_mapping__[attr] = value
        return nd

@magics_class
class OutputMagics(Magics):
    @magic_arguments.magic_arguments()
    @magic_arguments.argument(
        '-n', '--names', default="",
        help="""Specify output names."""
    )
    @magic_arguments.argument('expr', nargs='*',
        help="""Expression to output"""
    )

    def display(self, line, local_ns=None):
        args = magic_arguments.parse_argstring(self.display, line)

        names = args.names
        if (names and
                ((names.startswith('"') and names.endswith('"'))
                or (names.startswith("'") and names.endswith("'")))):
            names = names[1:-1]
        names = [x.strip() for x in names.split(',') if x.strip() != ""]

        line = ' '.join(args.expr)

        # follow %time code...
        expr = self.shell.input_transformer_manager.transform_cell(line)
        expr_ast = self.shell.compile.ast_parse(expr)
        expr_ast = self.shell.transform_ast(expr_ast)
        if len(expr_ast.body)==1 and isinstance(expr_ast.body[0], ast.Expr):
            mode = 'eval'
            source = '<display eval>'
            expr_ast = ast.Expression(expr_ast.body[0].value)
        else:
            mode = 'exec'
            source = '<display exec>'
        code = self.shell.compile(expr_ast, source, mode)

        glob = self.shell.user_ns
        if mode=='eval':
            try:
                out = eval(code, glob, local_ns)
            except:
                self.shell.showtraceback()
                return
        else:
            try:
                exec(code, glob, local_ns)
            except:
                self.shell.showtraceback()
                return
            out = None

        if isinstance(out, collections.Mapping):
            # wrap out according to names
            # if is dictionary-like
            return build_linked_result(self.shell.uuid, **out)
            # return nameddict.from_mapping(out)
        elif isinstance(out, collections.Sequence):
            # wrap out according to names or indicies
            names = [safe_attr(names[i] if i < len(names) else i)
                     for i in range(len(out))]
            return collections.namedtuple('namedtuple', ' '.join(names))(*out)
        else:
            if len(names) < 1:
                names = ['res']
            return collections.namedtuple('namedtuple', ' '.join(names))(out)
        return out

    @needs_local_scope
    @line_magic
    def split_out(self, line, local_ns=None):
        """Takes an output and splits into multiple outputs.
        mapping -> each key-value pair becomes a separate output
        tuple -> each tuple becomes a separate output
        list -> each entry becomes a separate output
        """
        return self.display(line, local_ns)

    @needs_local_scope
    @line_magic
    def name_out(self, line, local_ns=None):
        """Adds names to an output.
        Mirrors split_out except that this makes sense for a single output.
        object -> adds a name to the output
        """
        return self.display(line, local_ns)

# TODO move to its own package
def expr2id(node):
    """Convert ast node to valid python identifier.

    If the expression is just an identifier, return the identifier.
    If the expression contains a function, return the function name.
    If the expression contains an operator, return the name of the operator followed by the subexpressions.
    """
    # print("expr2id", ast.dump(node))
    if isinstance(node, str):
        node = ast.parse(node)
        if len(node.body) != 1:
            raise ValueError("Node should have only one expression")
        if not isinstance(node.body[0], ast.Expr):
            raise ValueError("Node must be an expression")
        node = node.body[0]

    if isinstance(node, ast.Name):
        return node.id
    elif isinstance(node, ast.Num):
        return 'c' # node.n
    elif isinstance(node, ast.Str):
        return 'c' # node.s
    elif isinstance(node, ast.Attribute):
        # concatenate?
        return expr2id(node.value) + "_" + node.attr
    elif isinstance(node, ast.Subscript):
        slice = ""
        if isinstance(node.slice, ast.Index):
            if isinstance(node.slice.value, ast.Num):
                slice = "_{}".format(node.slice.value.n)
            elif isinstance(node.slice.value, ast.Str):
                slice = "_{}".format(node.slice.value.s)
        return expr2id(node.value) + slice
    elif isinstance(node, ast.Index):
        return expr2id(node.value)
    else:
        return 'x'

# FIXME should mix the pprint for seq and dict
# FIXME use normal printer if _fields doesn't exist
def tuple_formatter(arg, p, cycle):
    with p.group(1, '(', ')'):
        for i in range(len(arg)):
            if hasattr(arg, '_fields'):
                p.text(arg._fields[i] + ": ")
            p.pretty(arg[i])
            if i != len(arg) - 1:
                p.text(',')
                p.breakable()


class CellIdTransformer(ast.NodeTransformer):
    def visit_Subscript(self, node):
        super().generic_visit(node)
        if (isinstance(node.value, ast.Name) and
                    node.value.id == "Out" and
                isinstance(node.slice, ast.Index) and
                isinstance(node.slice.value, ast.Name)):
            return ast.copy_location(ast.Subscript(
                value=ast.Name(id=node.value.id, ctx=node.value.ctx),
                slice=ast.Index(value=ast.Str(s=node.slice.value.id)),
                ctx=node.ctx), node)
        return node

class FuncTransformer(ast.NodeTransformer):
    def visit(self, node):
        #No purpose examining nodes that can't possibly have func defs and expr
        if len(node.body) > 1:
            last_node = node.body[-1]
            out_names = []
            func_names = []
            top_level_stores = []
            if isinstance(last_node, ast.Expr):
                if isinstance(last_node.value, ast.Tuple):
                    for namenode in last_node.value.elts:
                        if(isinstance(namenode,ast.Num)):
                            return node
                        out_names.append(namenode.id)
                elif isinstance(last_node.value, ast.Num):
                    return node
                else:
                    out_names = [last_node.value.id]
                del node.body[-1]
            for topnode in node.body:
                if isinstance(topnode, ast.FunctionDef):
                    func_names.append(topnode.name)
                elif isinstance(topnode, ast.Assign):
                    for target in topnode.targets:
                        if (isinstance(target, ast.Tuple)):
                            for elt in target.elts:
                                top_level_stores.append(elt.id)
                        elif (isinstance(target, ast.Name)):
                            top_level_stores.append(target.id)
            intersection = list(set(func_names) & set(out_names))
            if len(intersection):
                initdef = "def __init__(self,args=None):\n\tself.func = eval('self.'+args)"
                calldef = "def __call__(self,*args):\n\treturn self.func(*args)"
                classdef = ast.ClassDef('dfpyclass', [], [], [ast.parse(initdef).body[0], ast.parse(calldef).body[0]],
                                        [])
                for topnode in node.body:
                    if isinstance(topnode, ast.FunctionDef):
                        args = [arg.arg for arg in topnode.args.args]
                        topnode.args.args = [ast.arg('self', None)] + topnode.args.args
                        for func_node in ast.walk(topnode):
                            if isinstance(func_node, ast.Name):
                                if isinstance(func_node.ctx, ast.Store):
                                    # Append user defined vars
                                    args.append(func_node.id)
                                elif func_node.id in top_level_stores and func_node.id not in args:
                                    func_node = ast.parse('self.' + func_node.id)
                classdef.body.extend(node.body)
                outs = []
                out_labels = []
                #Intersection must go first to define static vars
                for name in intersection:
                    outs.append('dfpyclass("' + name + '")')
                    out_labels.append(name)
                for name in out_names:
                    if (name not in func_names):
                        outs.append('dfpyclass.' + name)
                        out_labels.append(name)
                template = ','.join(out_labels) + '=' + ','.join(outs)
                last_node = ast.parse(template)
                node.body = [classdef] + last_node.body
        return node

class ZMQInteractiveShell(ipykernel.zmqshell.ZMQInteractiveShell):
    """A subclass of InteractiveShell for ZMQ."""

    displayhook_class = Type(ZMQShellDisplayHook)
    display_pub_class = Type(ZMQDisplayPublisher)

    execution_count = Integer(0)
    # UUID passed from notebook interface
    uuid = Unicode()
    dataflow_history_manager = Instance(DataflowHistoryManager)
    dataflow_function_manager = Instance(DataflowFunctionManager)

    def __init__(self, *args, **kwargs):
        if 'user_ns' not in kwargs or kwargs['user_ns'] is None:
            kwargs['user_ns'] = DataflowNamespace()
        super().__init__(*args, **kwargs)
        #FIXME: This is really just a simple fix to turn it on with Kernel boot, but this seems like a bandaid fix
        self.ast_node_interactivity = 'last_expr_or_assign'
        self.ast_transformers.append(CellIdTransformer())
        # self.ast_transformers.append(FuncTransformer())
        self.display_formatter.formatters["text/plain"].for_type(tuple, tuple_formatter)

    def run_cell_as_execute_request(self, code, uuid, store_history=False, silent=False,
                                    shell_futures=True, update_downstream_deps=False):
        return self.kernel.inner_execute_request(code, uuid, silent, store_history)

    def _showtraceback(self, etype, evalue, stb):
        # try to preserve ordering of tracebacks and print statements
        sys.stdout.flush()
        sys.stderr.flush()

        exc_content = {
            u'traceback' : stb,
            u'ename' : unicode_type(etype.__name__),
            u'evalue' : py3compat.safe_unicode(evalue),
            u'execution_count': self.uuid,
        }

        dh = self.displayhook
        # Send exception info over pub socket for other clients than the caller
        # to pick up
        topic = None
        if dh.topic:
            topic = dh.topic.replace(b'execute_result', b'error')

        exc_msg = dh.session.send(dh.pub_socket, u'error', json_clean(exc_content),
                                  dh.parent_header, ident=topic)

        # FIXME - Once we rely on Python 3, the traceback is stored on the
        # exception object, so we shouldn't need to store it here.
        self._last_traceback = stb

    def init_magics(self):
        super(ZMQInteractiveShell, self).init_magics()
        self.register_magics(FunctionMagics)
        self.register_magics(OutputMagics)

    # FIXME hack to be notified of change before it happens?
    @validate('uuid')
    def _uuid_to_change(self, proposal):
        # print("UUID TO CHANGE TO", proposal['value'], file=sys.__stdout__)
        if hasattr(sys.stdout, 'get_execution_count'):
            sys.stdout.flush()
        if hasattr(sys.stderr, 'get_execution_count'):
            sys.stderr.flush()
        return proposal['value']

    def run_cell(self, raw_cell, uuid=None, code_dict={}, output_tags={},
                     store_history=False, silent=False, shell_futures=True,
                     update_downstream_deps=False):
        """Run a complete IPython cell.

        Parameters
        ----------
        raw_cell : str
          The code (including IPython code such as %magic functions) to run.
        store_history : bool
          If True, the raw and translated cell will be stored in IPython's
          history. For user code calling back into IPython's machinery, this
          should be set to False.
        silent : bool
          If True, avoid side-effects, such as implicit displayhooks and
          and logging.  silent=True forces store_history=False.
        shell_futures : bool
          If True, the code will share future statements with the interactive
          shell. It will both be affected by previous __future__ imports, and
          any __future__ imports in the code will affect the shell. If False,
          __future__ imports are not shared in either direction.

        Returns
        -------
        result : :class:`ExecutionResult`
        """

        # print("CODE_DICT:", code_dict)
        # print("RUNNING CELL", uuid, raw_cell)
        # print("RUN_CELL USER_NS:", self.user_ns)
        self._last_traceback = None

        if store_history:
            self.dataflow_history_manager.update_codes(code_dict)
            self.user_ns._add_links(output_tags)
            # also put the current cell into the cache and force recompute
            if uuid not in code_dict:
                self.dataflow_history_manager.update_code(uuid, raw_cell)
            self.dataflow_history_manager.update_flags(
                store_history=store_history,
                silent=silent,
                shell_futures=shell_futures,
                update_downstream_deps=update_downstream_deps)

        info = ExecutionInfo(
            raw_cell, store_history, silent, shell_futures)
        result = ExecutionResult(info)

        if (not raw_cell) or raw_cell.isspace():
            self.last_execution_succeeded = True
            return result

        if silent:
            store_history = False

        if store_history:
            result.execution_count = uuid

        def error_before_exec(value):
            result.error_before_exec = value
            self.last_execution_succeeded = False
            return result

        self.events.trigger('pre_execute')
        if not silent:
            self.events.trigger('pre_run_cell')

        # If any of our input transformation (input_transformer_manager or
        # prefilter_manager) raises an exception, we store it in this variable
        # so that we can display the error after logging the input and storing
        # it in the history.
        preprocessing_exc_tuple = None
        try:
            # Static input transformations
            cell = self.input_transformer_manager.transform_cell(raw_cell)
        except SyntaxError:
            preprocessing_exc_tuple = sys.exc_info()
            cell = raw_cell  # cell has to exist so it can be stored/logged
        else:
            if len(cell.splitlines()) == 1:
                # Dynamic transformations - only applied for single line commands
                with self.builtin_trap:
                    try:
                        # use prefilter_lines to handle trailing newlines
                        # restore trailing newline for ast.parse
                        cell = self.prefilter_manager.prefilter_lines(cell) + '\n'
                    except Exception:
                        # don't allow prefilter errors to crash IPython
                        preprocessing_exc_tuple = sys.exc_info()

        # Store raw and processed history
        if store_history:
            self.execution_count += 1
            # store cur_execution_count because of recursion
            cur_execution_count = self.execution_count
            # print("STORING INPUTS:", self.execution_count)
            self.history_manager.store_inputs(self.execution_count,
                                              cell, raw_cell)
        if not silent:
            self.logger.log(cell, raw_cell)

        # Display the exception if input processing failed.
        if preprocessing_exc_tuple is not None:
            self.showtraceback(preprocessing_exc_tuple)
            # if store_history:
            #     self.execution_count += 1
            return error_before_exec(preprocessing_exc_tuple[2])

        # Our own compiler remembers the __future__ environment. If we want to
        # run code with a separate __future__ environment, use the default
        # compiler
        compiler = self.compile if shell_futures else CachingCompiler()

        with self.builtin_trap:
            # TODO seems that uuid is more appropriate than execution_count here
            cell_name = self.compile.cache(cell, uuid)

            with self.display_trap:
                # Compile to bytecode
                try:
                    code_ast = compiler.ast_parse(cell, filename=cell_name)
                except self.custom_exceptions as e:
                    etype, value, tb = sys.exc_info()
                    self.CustomTB(etype, value, tb)
                    return error_before_exec(e)
                except IndentationError as e:
                    self.showindentationerror()
                    # if store_history:
                    #     self.execution_count += 1
                    return error_before_exec(e)
                except (OverflowError, SyntaxError, ValueError, TypeError,
                        MemoryError) as e:
                    self.showsyntaxerror()
                    # if store_history:
                    #     self.execution_count += 1
                    return error_before_exec(e)

                # Apply AST transformations
                try:
                    code_ast = self.transform_ast(code_ast)
                except InputRejected as e:
                    self.showtraceback()
                    # if store_history:
                    #     self.execution_count += 1
                    return error_before_exec(e)

                internalnodes = []
                for node in ast.walk(code_ast):
                    if(isinstance(node,ast.Name) and isinstance(node.ctx,ast.Store)):
                        internalnodes.append(node.id)

                # Give the displayhook a reference to our ExecutionResult so it
                # can fill in the output value.

                # displayhook exec_result changed to reflect recursion
                old_result = self.displayhook.exec_result
                self.displayhook.exec_result = result
                old_uuid = self.uuid
                self.uuid = uuid
                self.user_ns._start_uuid(self.uuid)

                # user_ns = copy.copy(self.user_ns)

                # Execute the user code
                interactivity = "none" if silent else self.ast_node_interactivity
                has_raised = self.run_ast_nodes(code_ast.body, cell_name,
                                                interactivity=interactivity, compiler=compiler, result=result)

                self.last_execution_succeeded = not has_raised

                # Reset this so later displayed values do not modify the
                # ExecutionResult
                self.displayhook.exec_result = old_result
                self.uuid = old_uuid
                self.user_ns._revisit_uuid(self.uuid)

                # self.user_ns = user_ns

                if(not self.last_execution_succeeded):
                    for j in self.dataflow_history_manager.storeditems:
                        self.dataflow_history_manager.remove_dependencies(j['parent'],j['child'])

                if (type(result.result).__name__ == 'LinkedResult'):
                    result.result.__sethist__(self.dataflow_history_manager)

                self.dataflow_history_manager.storeditems = []
                self.events.trigger('post_execute')
                if not silent:
                    self.events.trigger('post_run_cell')

        if not has_raised:
            if store_history:
                # Write output to the database. Does nothing unless
                # history output logging is enabled.
                # print("STORING HISTORY", cur_execution_count)
                self.history_manager.store_output(cur_execution_count)
                # print("STORING UPDATE VALUE:", uuid, result)
                self.dataflow_history_manager.update_value(uuid, result.result)
                self.dataflow_history_manager.set_not_stale(uuid)

                # Each cell is a *single* input, regardless of how many lines it has
                # self.execution_count += 1

            if store_history:
                result.internal_nodes = internalnodes
                result.imm_upstream_deps = self.dataflow_history_manager.get_upstream(uuid)
                result.all_upstream_deps = self.dataflow_history_manager.all_upstream(uuid)
                result.update_downstreams = []
                for i in result.imm_upstream_deps:
                    result.update_downstreams.append({'key':i, 'data':self.dataflow_history_manager.get_downstream(i)})
                result.imm_downstream_deps = self.dataflow_history_manager.get_downstream(uuid)
                result.all_downstream_deps = self.dataflow_history_manager.all_downstream(uuid)
                if(type(result.result).__name__ == 'LinkedResult'):
                    for i in result.result.keys():
                        result.imm_downstream_deps += self.dataflow_history_manager.get_downstream(uuid+i)
                        result.all_downstream_deps += self.dataflow_history_manager.all_downstream(uuid+i)


        return result

    def get_linked_vars(self, node):
        create_node = True
        append_node = True
        vars = []
        no_link_vars = []
        if isinstance(node, _assign_nodes):
            asg = node
            if isinstance(asg, ast.Assign) and len(asg.targets) == 1:
                target = asg.targets[0]
            elif isinstance(asg, _single_targets_nodes):
                target = asg.target
            else:
                target = None
            if isinstance(target, ast.Name):
                vars.append(target.id)
            elif isinstance(target, ast.Tuple):
                for elt in target.elts:
                    if not isinstance(elt, ast.Name):
                        create_node = False
                        break
                    vars.append(elt.id)
            else:
                create_node = False
        elif isinstance(node, ast.Expr):
            append_node = False
            if isinstance(node.value, ast.Tuple):
                asg = node.value
                for elt in asg.elts:
                    if (not isinstance(elt, ast.Name) or
                            self.user_ns._is_external_link(elt.id, self.uuid)):
                        create_node = False
                        break
                    vars.append(elt.id)
            elif isinstance(node.value, ast.Name):
                elt = node.value
                if self.user_ns._is_external_link(elt.id, self.uuid):
                    create_node = False
                else:
                    vars.append(elt.id)
            else:
                create_node = False
        else:
            create_node = False
        return vars, create_node, append_node

    def run_ast_nodes(self, nodelist:ListType[AST], cell_name:str, interactivity='last_expr',
                        compiler=compile, result=None):
        no_link_vars = []
        auto_add_libs = True # FIXME add a configuration option that sets this
        if interactivity == 'last_expr_or_assign':
            vars, create_node, append_node = self.get_linked_vars(nodelist[-1])
            no_link_vars.extend(vars)

            if auto_add_libs:
                lnames = []
                for elt in nodelist:
                    if (isinstance(elt, ast.Import) or
                            isinstance(elt,ast.ImportFrom)):
                        for name in elt.names:
                            if name.asname:
                                lnames.append(name.asname)
                            else:
                                if '.' in name.name:
                                    lnames.append(name.name.split('.',1)[0])
                                else:
                                    lnames.append(name.name)
                if len(lnames) > 0:
                    diff = set(lnames) - set(vars)
                    if len(diff) > 0:
                        create_node = True
                        append_node = True
                        vars = list(diff) + vars

            if create_node:
                keywords = [ast.keyword(var, ast.Name(var, ast.Load())) for var in vars]
                nnode = ast.Expr(ast.Call(ast.Name('_build_linked_result', ast.Load()), [ast.Str(self.uuid)], keywords))
                ast.fix_missing_locations(nnode)
                if append_node:
                    nodelist.append(nnode)
                else:
                    nodelist[-1] = nnode
                # also need to pull off the values so they don't recurse on themselves

            interactivity = 'last_expr'

        # print("DO NOT LINK", no_link_vars)
        self.user_ns.__do_not_link__.update(no_link_vars)
        res = super().run_ast_nodes(nodelist, cell_name, interactivity, compiler, result)
        self.user_ns.__do_not_link__.difference_update(no_link_vars)
        return res

    def run_code(self, code_obj, result=None):
        """Execute a code object.

        When an exception occurs, self.showtraceback() is called to display a
        traceback.

        Parameters
        ----------
        code_obj : code object
          A compiled code object, to be executed
        result : ExecutionResult, optional
          An object to store exceptions that occur during execution.

        Returns
        -------
        False : successful execution.
        True : an error occurred.
        """
        # Set our own excepthook in case the user code tries to call it
        # directly, so that the IPython crash handler doesn't get triggered
        old_excepthook, sys.excepthook = sys.excepthook, self.excepthook

        # we save the original sys.excepthook in the instance, in case config
        # code (such as magics) needs access to it.
        self.sys_excepthook = old_excepthook
        outflag = True  # happens in more places, so it's easier as default
        try:
            try:
                self.hooks.pre_run_code_hook()
                # rprint('Running code', repr(code_obj)) # dbg
                # user_global_ns = {}
                exec(code_obj, self.user_global_ns, self.user_ns)
            finally:
                # Reset our crash handler in place
                sys.excepthook = old_excepthook
        except SystemExit as e:
            if result is not None:
                result.error_in_exec = e
            self.showtraceback(exception_only=True)
            warn("To exit: use 'exit', 'quit', or Ctrl-D.", stacklevel=1)
        except self.custom_exceptions:
            etype, value, tb = sys.exc_info()
            if result is not None:
                result.error_in_exec = value
            self.CustomTB(etype, value, tb)
        except:
            if result is not None:
                result.error_in_exec = sys.exc_info()[1]
            self.showtraceback(running_compiled_code=True)
        else:
            outflag = False
        return outflag

    def init_user_ns(self):
        """Initialize all user-visible namespaces to their minimum defaults.

                Certain history lists are also initialized here, as they effectively
                act as user namespaces.

                Notes
                -----
                All data structures here are only filled in, they are NOT reset by this
                method.  If they were not empty before, data will simply be added to
                therm.
                """
        # This function works in two parts: first we put a few things in
        # user_ns, and we sync that contents into user_ns_hidden so that these
        # initial variables aren't shown by %who.  After the sync, we add the
        # rest of what we *do* want the user to see with %who even on a new
        # session (probably nothing, so they really only see their own stuff)

        # The user dict must *always* have a __builtin__ reference to the
        # Python standard __builtin__ namespace,  which must be imported.
        # This is so that certain operations in prompt evaluation can be
        # reliably executed with builtins.  Note that we can NOT use
        # __builtins__ (note the 's'),  because that can either be a dict or a
        # module, and can even mutate at runtime, depending on the context
        # (Python makes no guarantees on it).  In contrast, __builtin__ is
        # always a module object, though it must be explicitly imported.

        # For more details:
        # http://mail.python.org/pipermail/python-dev/2001-April/014068.html
        ns = {}

        # make global variables for user access to the histories
        ns['_ih'] = self.history_manager.input_hist_parsed
        # ns['_oh'] = self.history_manager.output_hist
        ns['_dh'] = self.history_manager.dir_hist
        ns['_oh'] = self.dataflow_history_manager
        ns['_func'] = self.dataflow_function_manager

        # user aliases to input and output histories.  These shouldn't show up
        # in %who, as they can have very large reprs.
        ns['In'] = self.history_manager.input_hist_parsed
        # ns['Out'] = self.history_manager.output_hist
        ns['Out'] = self.dataflow_history_manager
        ns['Func'] = self.dataflow_function_manager
        ns['_build_linked_result'] = build_linked_result
        ns['_ns'] = self.user_ns

        # Store myself as the public api!!!
        ns['get_ipython'] = self.get_ipython

        ns['exit'] = self.exiter
        ns['quit'] = self.exiter

        # Sync what we've added so far to user_ns_hidden so these aren't seen
        # by %who
        self.user_ns_hidden.update(ns)

        # Anything put into ns now would show up in %who.  Think twice before
        # putting anything here, as we really want %who to show the user their
        # stuff, not our variables.

        # Finally, update the real user's namespace
        self.user_ns.update(ns)


    def init_history(self):
        """Sets up the command history, and starts regular autosaves."""
        self.history_manager = HistoryManager(shell=self, parent=self)
        self.dataflow_history_manager = DataflowHistoryManager(shell=self)
        self.dataflow_function_manager = \
            DataflowFunctionManager(self.dataflow_history_manager)
        self.configurables.append(self.history_manager)

    # def prepare_user_module(self, user_module=None, user_ns=None):
    #     print("USER_NS", user_ns, file=sys.__stdout__, flush=True)
    #
    #     return super().prepare_user_module(user_module, user_ns)


InteractiveShellABC.register(ZMQInteractiveShell)
