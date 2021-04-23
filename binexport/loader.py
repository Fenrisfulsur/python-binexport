from __future__ import absolute_import
import logging
import os
import pathlib
import tempfile
from collections import defaultdict, OrderedDict

import networkx
from typing import Dict, List, Optional, Generator, Tuple, Union, Set
from binexport.binexport2_pb2 import BinExport2

def _get_instruction_address(pb: BinExport2, inst_idx: int) -> int:
    """
    Low level binexport protobuf function to return the address of an instruction
    given its index in the protobuf.
    :param pb: binexport protobuf object
    :param inst_idx: index of the instruction
    :return: address of the instruction
    """
    inst = pb.instruction[inst_idx]
    if inst.HasField('address'):
        return inst.address
    else:
        return _backtrack_instruction_address(pb, inst_idx)


def _backtrack_instruction_address(pb: BinExport2, idx) -> int:
    """
    Low level function to backtrack the instruction array for instruction that
    does not have the address field set
    :param pb: binexport protobuf object
    :param idx: index of the instruction
    :return: address of the instruction
    """
    tmp_sz = 0
    tmp_idx = idx
    if tmp_idx == 0:
        return pb.instruction[tmp_idx].address
    while True:
        tmp_idx -= 1
        tmp_sz += len(pb.instruction[tmp_idx].raw_bytes)
        if pb.instruction[tmp_idx].HasField('address'):
            break
    return pb.instruction[tmp_idx].address + tmp_sz


def _get_basic_block_addr(pb: BinExport2, bb_idx: int) -> int:
    """
    Low level function to retrieve the basic block address from its index.
    The function takes the first instruction of the basic block and retrieve
    its address.
    :param pb: binexport protobuf object
    :param bb_idx: index of the basic block
    :return: address of the basic block in the program
    """
    inst = pb.basic_block[bb_idx].instruction_index[0].begin_index
    return _get_instruction_address(pb, inst)


class ProgramBinExport(dict):
    """
    Program class that represent the binexport with high-level functions
    and an easy to use API. It inherits from a dict which is used to
    reference all functions based on their address.
    """

    def __init__(self, file: Union[pathlib.Path, str]):
        """
        Program constructor. It takes the file path, parse the binexport and
        initialize all the functions and instructions.
        :param file: .BinExport file path
        """
        super(dict, self).__init__()
        self._pb = BinExport2()
        with open(file, 'rb') as f:
            self._pb.ParseFromString(f.read())
        self._mask = 0xFFFFFFFF if self.architecture.endswith("32") else 0xFFFFFFFFFFFFFFFF
        self.fun_names = {}

        # Make the data refs map
        self.data_refs = {}
        for entry in self.proto.data_reference[::-1]:
            if entry.instruction_index in self.data_refs:
                self.data_refs[entry.instruction_index].append(entry.address)
            else:
                self.data_refs[entry.instruction_index] = [entry.address]

        # Make the address comment (will be deprecated)
        self.addr_refs = {}
        for entry in self.proto.address_comment[::-1]:
            if entry.instruction_index in self.addr_refs:
                self.addr_refs[entry.instruction_index].append(self.proto.string_table[entry.string_table_index])
            else:
                self.addr_refs[entry.instruction_index] = [self.proto.string_table[entry.string_table_index]]

        # Make the string reference
        self.string_refs: Dict = {}
        for entry in self.proto.string_reference:
            self.string_refs[entry.instruction_index] = entry

        count_f = 0
        coll = 0
        # Load all the functions
        for i, pb_fun in enumerate(self.proto.flow_graph):
            f = FunctionBinExport(self, pb_fun)
            if f.addr in self:
                logging.error("Address collision for 0x%x" % f.addr)
                coll += 1
            self[f.addr] = f
            count_f += 1

        count_imp = 0
        # Load the callgraph
        cg = self.proto.call_graph
        for node in cg.vertex:
            if node.address not in self and node.type == cg.Vertex.IMPORTED:
                self[node.address] = FunctionBinExport(self, None, is_import=True, addr=node.address)
                count_imp += 1
            if node.address not in self and node.type == cg.Vertex.NORMAL:
                logging.error("Missing function address: 0x%x (%d)" % (node.address, node.type))

            self[node.address].type = node.type
            self[node.address].name = node.mangled_name
        for edge in cg.edge:
            src = cg.vertex[edge.source_vertex_index].address
            dst = cg.vertex[edge.target_vertex_index].address
            self[src].children.add(self[dst])
            self[dst].parents.add(self[src])

        for f in self.values():  # Create a map of function names for quick lookup later on
            self.fun_names[f.name] = f

        logging.debug("total all:%d, imported:%d collision:%d (total:%d)" %
                      (count_f, count_imp, coll, (count_f + count_imp + coll)))

    @staticmethod
    def from_binary_file(exec_file: Union[pathlib.Path, str], output_file: Union[str, pathlib.Path] = "", open_export: bool = True)\
            -> Optional['ProgramBinExport']:
        """
        Generate the .BinExport file for the given program and return an instance
        of ProgramBinExport.
        .. warning:: That function requires the module ``idascript``
        :param exec_file: executable file path
        :param output_file: BinExport output file
        :param open_export: whether or not to open the binexport after export
        :return: an instance of ProgramBinExport
        """
        from idascript import IDA

        if not output_file:
            output_file = binexport_file = pathlib.Path(exec_file).with_suffix('.BinExport')

        ida = IDA(exec_file,
                  script_file=None,
                  script_params=["BinExportAutoAction:BinExportBinary",
                                 f"BinExportModule:{output_file}"]
        )
        ida.start()
        retcode = ida.wait()

        logging.info("%s successfully exported to BinExport [code: %d]" % (exec_file, retcode))

        if output_file:
            output_file = pathlib.Path(output_file)
            binexport_file.rename(output_file)
            binexport_file = output_file

        if binexport_file.is_file():
            return ProgramBinExport(binexport_file) if open_export else None
        else:
            logging.error("export with IDA failed for some reasons (binexport not found)")
            return None

    def addr_mask(self, value: int) -> int:
        """
        Mask and address value depending on whether its a 32 or 64 bits CPU.
        Basically make sur the 32 high bits on 32 bits are set to 0 which is
        not the case by default on binexport.
        :param value: address value
        :return: address value masked with the address size of CPU
        """
        return value & self._mask

    @property
    def proto(self) -> BinExport2:
        """
        :return: Low-level BinExport2 protobuf object
        """
        return self._pb

    @property
    def name(self) -> str:
        """
        Return the name of the program (as exported by binexport)
        :return: name of the program
        """
        return self.proto.meta_information.executable_name

    @property
    def architecture(self) -> str:
        """
        Returns the architecture suffixed with address size ex: x86_64, x86_32
        :return: architecture name
        """
        return self.proto.meta_information.architecture_name

    def __repr__(self) -> str:
        return '<%s:%s>' % (type(self).__name__, self.name)


class FunctionBinExport(dict):
    """
    Class that represent functions. It inherits from a dict which is used to
    reference all basic blocks by their address. Also references its parents
    and children (function it calls).
    """

    def __init__(self, program: ProgramBinExport, pb_fun: Optional[BinExport2.FlowGraph], is_import: bool = False,
                 addr: Optional[int] = None):
        """
        Constructor. Iterates the FlowGraph structure and initialize all the
        basic blocks and instruction accordingly.
        :param program: program (used to navigate pb fields contained inside)
        :param pb_fun: FlowGraph protobuf structure
        :param is_import: whether or not its an import function (if so does not initialize bb etc..)
        :param addr: address of the function (info avalaible in the call graph)
        """
        super(dict, self).__init__()
        self.addr = addr
        self.parents = set()
        self.children = set()
        self.graph = networkx.DiGraph()
        self._pb_type = None  # Set by the Program constructor
        self._name = None  # Set by the Program constructor (mangled name)

        if is_import:
            return

        self.addr = _get_basic_block_addr(program.proto, pb_fun.entry_basic_block_index)

        cur_state = [None, -2]  # correspond to [cur_addr, prev_idx]
        bb_map = {}
        rng_map = {}
        bb_count = 0
        for bb_idx in pb_fun.basic_block_index:
            splitted = []
            for rng in program.proto.basic_block[bb_idx].instruction_index:  # Ranges are in fact the true basic blocks!
                bb_count += 1
                bb = BasicBlockBinExport(program, self, rng, cur_state)

                if bb.addr in self:
                    logging.error("0x%x basic block address (0x%x) already in(idx:%d)" % (self.addr, bb.addr, bb_idx))
                self[bb.addr] = bb
                bb_map[bb_idx] = bb.addr
                self.graph.add_node(bb.addr)
                splitted.append(bb.addr)

            if len(splitted) > 1:
                for node1, node2 in zip(splitted, splitted[1:]):
                    self.graph.add_edge(node1, node2)
                    self[node1].children.add(self[node2])
                    self[node2].parents.add(self[node1])
                rng_map[bb_idx] = [splitted[0], splitted[-1]]

        if bb_count != len(self):
            logging.error("Wrong basic block number %x, bb:%d, self:%d" %
                          (self.addr, len(pb_fun.basic_block_index), len(self)))

        # Load the edges between blocks
        for edge in pb_fun.edge:
            # if the blocks were a range take the ones in the range
            src_idx = edge.source_basic_block_index
            bb_src = rng_map[src_idx][1] if src_idx in rng_map else bb_map[src_idx]
            tgt_idx = edge.target_basic_block_index
            bb_dst = rng_map[tgt_idx][0] if tgt_idx in rng_map else bb_map[tgt_idx]

            self.graph.add_edge(bb_src, bb_dst)
            self[bb_src].children.add(self[bb_dst])
            self[bb_dst].parents.add(self[bb_src])

    def __hash__(self) -> int:
        """
        Make function hashable to be able to store them in sets (for parents, children)
        :return: address of the function
        """
        return hash(self.addr)

    @property
    def name(self) -> str:
        """
        Name of the function if it exists otherwise like IDA with sub_XXX
        :return: name of the function
        """
        return self._name if self._name else "sub_%X" % self.addr

    @name.setter
    def name(self, name: str) -> None:
        """
        Function name setter (available in the call graph of the pb object)
        :param name: name to give the function
        :return: None
        """
        self._name = name

    @property
    def type(self) -> BinExport2.FlowGraph.Edge.Type:
        """
        Type of the function within [NORMAL, LIBRARY, THUNK, IMPORTED, INVALID]
        :return: type enum of the function
        """
        return self._pb_type

    @type.setter
    def type(self, value: BinExport2.FlowGraph.Edge.Type) -> None:
        """
        Set the type of the function (available in the call graph of the pb object)
        :param value: type enum to give the function
        :return: None
        """
        self._pb_type = value

    @property
    def string_references(self) -> Set[str]:
        """
        Return the list of string referenced in this function.
        :return: list of the string referenced
        """
        string_references: Set[str] = set()
        for block in self.values():
            string_references.update(block.string_references)

        return string_references

    def is_import(self) -> bool:
        """
        Returns whether or not the function is an import
        :return: boolean indicating if the function is an import
        """
        return self.type == BinExport2.CallGraph.Vertex.IMPORTED

    def __repr__(self) -> str:
        return '<%s: 0x%x>' % (type(self).__name__, self.addr)


class BasicBlockBinExport(OrderedDict):
    """
    Basic block class: For convenience represented as an ordered dict rather than
    a list.
    """

    def __init__(self, program: ProgramBinExport, function: FunctionBinExport,
                 rng: BinExport2.BasicBlock.IndexRange, state: list):
        """
        Basic Block constructor

        :param program: Reference to the program
        :param function: Reference to the function
        :param rng: Index range of the basic block
        :param state: List of [cur_addr, prev_idx]
        """
        super(OrderedDict, self).__init__()
        self._addr = None
        self.parents = set()
        self.children = set()
        for idx in range(rng.begin_index, (rng.end_index if rng.end_index else rng.begin_index + 1)):

            if idx != state[1] + 1:  # if the current idx is different from the previous range or bb
                state[0] = None  # reset the addr has we have no guarantee on the continuity of the address

            pb_inst = program.proto.instruction[idx]

            if pb_inst.HasField('address'):  # If the instruction have an address set (can be 0)
                if state[0] is not None and state[0] != pb_inst.address:
                    # logging.warning("cur_addr different from inst address: %x != %x (%d) (%d->%d)" %
                    #                                    (cur_addr, pb_inst.address, bb_idx, prev_idx, idx))
                    pass  # might be legit if within the basic block there is data
                    # thus within the same range not contiguous address can co-exists
                state[0] = pb_inst.address  # set the address to the one of inst regardless cur_addr was set
            else:
                if not state[0]:  # if cur_addr_not set backtrack to get it
                    state[0] = _get_instruction_address(program.proto, idx)

            # At this point we should have a cur_addr correctly set to the right instruction address
            if not self._addr:
                self._addr = state[0]

            # At this point do the instruction initialization
            inst = InstructionBinExport(program, function, state[0], idx)
            self._append_instruction(inst)
            if idx in program.data_refs:  # Add some
                inst.data_refs = program.data_refs[idx]
            if idx in program.addr_refs:
                inst.addr_refs = program.addr_refs[idx]

            state[0] += len(pb_inst.raw_bytes)  # increment the cur_addr with the address size
            state[1] = idx

    @property
    def string_references(self) -> Set[str]:
        """
        Retrieve the list of string referenced in the basic block
        :return: list of deduplicated string used in the basic block
        """
        string_references = set()
        for instruction in self.values():
            instruction_str = instruction.string_references
            if instruction_str:
                string_references.add(instruction_str)

        return string_references

    @property
    def addr(self) -> int:
        """
        Returns the basic block instruction
        :return: basic block address
        """
        return self._addr

    def _append_instruction(self, instruction) -> None:
        """
        Utility function to add an instruction in the basic block.
        :param instruction: InstructionBinExport object
        :return: None
        """
        self[instruction.addr] = instruction

    def __str__(self) -> str:
        return "\n".join(str(i) for i in self.values())

    def __repr__(self):
        return "<%s:0x%x>" % (type(self).__name__, self.addr)

    def __hash__(self) -> int:
        """
        Make function hashable to be able to store them in sets (for parents, children)
        :return: address of the function
        """
        return hash(self.addr)


class InstructionBinExport:
    """
    Instruction class. It represent an instruction with its operands.
    """

    def __init__(self, program: ProgramBinExport, fun: FunctionBinExport, addr: int, i_idx: int):
        """
        Instruction constructor.
        :param program: program object (which contains protobuf structure)
        :param fun: function object (within which the instruction is located)
        :param addr: address of the instruction (computed outside)
        :param i_idx: instuction index in the protobuf data structure
        """
        self._addr = addr
        self._program = program
        self._function = fun
        self._idx = i_idx
        self.data_refs = []
        self.addr_refs = []

    @property
    def addr(self) -> int:
        """
        Address of the instruction
        :return: address of the instruction
        """
        return self._addr

    @property
    def mnemonic(self) -> str:
        """
        Returns the mnemonic string as gathered by binexport
        :return: mnemonic string (with prefix)
        """
        return self._program.proto.mnemonic[self._program.proto.instruction[self._idx].mnemonic_index].name

    def _me(self) -> BinExport2.Instruction:
        """
        Returns the Instruction object in the binexport structure
        :return: Instruction binexport object
        """
        return self._program.proto.instruction[self._idx]

    @property
    def operands(self):
        """
        Returns a list of the operands which class are instanciated dynamically on-demand.
        :return: list of operand objects
        """
        return [OperandBinExport(self._program, self._function, self, op_idx, op_offset)
                for op_offset, op_idx in enumerate(self._me().operand_index)]

    @property
    def comment(self) -> str:
        """
        Returns the string of the comment (if binexport did exported them which is
        apparently not the case)
        :return: comment string
        """
        if len(self.data_refs) >= len(self.addr_refs):
            ith = len(self.data_refs)
        else:
            ith = 0
        if self.addr_refs[ith:]:
            last = self.addr_refs[-1]
            if self.is_function_entry():
                if last == self._program[self.addr].name:
                    try:
                        return self.addr_refs[-2]
                    except IndexError:
                        return ""
            else:
                return last
        else:
            return ""

    def is_function_entry(self) -> bool:
        """
        Returns whether or not the instruction is the entrypoint of a function
        :return: boolean if instruction is the entrypoint
        """
        return self.addr in self._program

    @property
    def string_references(self) -> Union[str, None]:
        """
        Return the list of the string referenced in the instruction
        :return: set of the strings (may be empty)
        """
        string_reference = None
        entry = self._program.string_refs.get(self._idx, None)
        if entry:
            string_reference = self._program.proto.string_table[entry.string_table_index]

        return string_reference

    def __str__(self) -> str:
        return '%s %s' % (self.mnemonic, ", ".join(str(o) for o in self.operands))

    def __repr__(self) -> str:
        return "<%s 0x%x: %s %s>" % \
               (type(self).__name__, self.addr, self.mnemonic, ", ".join(str(x) for x in self.operands))


class OperandBinExport:
    """
    Class that represent an operand. The class goal is mainly
    to iterate the operand expression.
    """

    __sz_lookup = {'b1': 1, 'b2': 2, 'b4': 4, 'b8': 8, 'b10': 10, 'b16': 16, 'b32': 32, 'b64': 64}
    __sz_name = {1: 'byte', 2: 'word', 4: 'dword', 8: "qword", 10: 'b10', 16: "xmmword", 32: "ymmword", 64: "zmmword"}

    def __init__(self, program: ProgramBinExport, fun: FunctionBinExport, inst: InstructionBinExport, op_idx: int, op_offset: int):
        """
        Constructor. Takes both the program, function and instruction which are used
        to compute various attributes
        :param program: Program object
        :param fun: Function object
        :param inst: Instruction object
        :param op_idx: operand index in protobuf structure
        :param op_offset: operand index in the instruction (first, second ...)
        """

        self._program = program
        self._function = fun
        self._instruction = inst
        self._idx = op_idx

        # In some cases, the string ref may be attached to an operand (and an expression)
        # If this is the case do it.
        self.string_reference = None
        entry = self._program.string_refs.get(inst._idx, None)
        if entry and entry.operand_expression_index == op_offset:
            self.string_reference = self._program.proto.string_table[entry.string_table_index]

    def _me(self) -> BinExport2.Operand:
        """
        Returns the operand object in the protobuf structure
        :return: protobuf operand
        """
        return self._program.proto.operand[self._idx]

    def __iter_expressions(self) -> Generator[Tuple[str, Union[str, int], int, int], None, None]:
        """
        Low-level expression generator. Some simple types are converted to IDA style
        types (libname, codname etc...)
        :return: Generator of (low-level) expressions
        """
        size = None
        for idx in self._me().expression_index:
            exp = self._program.proto.expression[idx]
            if exp.type == BinExport2.Expression.SYMBOL:  # If the expression is a symbol
                if exp.symbol in self._program.fun_names:  # If it is a function name
                    f = self._program.fun_names[exp.symbol]
                    if f.type == BinExport2.CallGraph.Vertex.NORMAL:
                        yield ('codname', exp.symbol, idx, exp.parent_index)
                    elif f.type == BinExport2.CallGraph.Vertex.LIBRARY:
                        yield ('libname', exp.symbol, idx, exp.parent_index)
                    elif f.type == BinExport2.CallGraph.Vertex.IMPORTED:
                        yield ('impname', exp.symbol, idx, exp.parent_index)
                    elif f.type == BinExport2.CallGraph.Vertex.THUNK:
                        yield ('cname', exp.symbol, idx, exp.parent_index)
                    else:
                        pass  # invalid fucntion type just ignore it
                else:
                    yield ('locname', exp.symbol, idx, exp.parent_index)  # for var_, arg_

            elif exp.type == BinExport2.Expression.IMMEDIATE_INT:  # If the expression is an immediate
                if exp.immediate in self._instruction.data_refs:
                    s = "%s_%X" % (self.__sz_name[size], exp.immediate)
                    yield ('datname', s, idx, exp.parent_index)
                else:
                    if exp.immediate in self._program:  # if it is a function
                        yield ('codname', "sub_%X" % exp.immediate, idx, exp.parent_index)
                    elif exp.immediate in self._function:  # its a basic block address
                        yield ('codname', 'loc_%X' % exp.immediate, idx, exp.parent_index)
                    else:
                        yield ('number', self._program.addr_mask(exp.immediate), idx, exp.parent_index)

            elif exp.type == BinExport2.Expression.IMMEDIATE_FLOAT:
                print("IMMEDIATE FLOAT ignored:", exp)
            elif exp.type == BinExport2.Expression.OPERATOR:
                yield ('symbol', exp.symbol, idx, exp.parent_index)
            elif exp.type == BinExport2.Expression.REGISTER:
                yield ('reg', exp.symbol, idx, exp.parent_index)
            elif exp.type == BinExport2.Expression.DEREFERENCE:
                yield ('symbol', exp.symbol, idx, exp.parent_index)
            elif exp.type == BinExport2.Expression.SIZE_PREFIX:
                size = self.__sz_lookup[exp.symbol]
            else:
                print("woot:", exp)

    @property
    def expressions(self) -> Generator[Dict[str, Union[str, int]], None, None]:
        """
        Iterates over all the operand expression in a pre-order manner
        (binary operator first). Each item of the generator is a dict
        containing a 'type' and a 'value' field.
        :return: Generator of expression dict items
        """
        for elt in self.__iter_expressions():
            yield {'type': elt[0], 'value': elt[1]}

    @property
    def byte_size(self) -> int:
        """
        Size of the operand in bytes.
        :return: operand size in bytes
        """
        exp = self._program.proto.expression[self._me().expression_index[0]]
        if exp.type == BinExport2.Expression.SIZE_PREFIX:
            return self.__sz_lookup[exp.symbol]
        else:
            raise Exception("First expression not byte size..")

    @property
    def type(self) -> BinExport2.Expression.Type:
        """
        Returns the type of the operand using the binexport protobuf enum type
        :return: enum type
        """
        for exp in (self._program.proto.expression[idx] for idx in self._me().expression_index):
            if exp.type in [BinExport2.Expression.SIZE_PREFIX, BinExport2.Expression.OPERATOR]:
                continue
            else:
                return exp.type

        # if we reach here something necessarily went wrong
        if len(self._me().expression_index) == 1 and self._program.architecture.startswith("ARM"):
            if self._program.proto.expression[self._me().expression_index[0]].type == BinExport2.Expression.OPERATOR:
                return BinExport2.Expression.OPERATOR  # Specific handling of some ARM flags typed as OPERATOR
            else:
                logging.error("Unknown case for operand type on ARM: %s" % str(self))
        else:
            logging.error("No type found for operand: %s %d" % (str(self), len(self._me().expression_index)))

    def __str__(self) -> str:
        """
        Formatted string of the operand (shown in-order)
        :return: string of the operand
        """

        class Tree:
            def __init__(self, idx: int=None, value: Union[str, int]='', operator: bool=False):
                self.childs = []
                self.value = value
                self.idx = idx
                self.operator = operator

            def __str__(self):
                inv = {"{": "}", "[": "]", '!': ''}

                if isinstance(self.value, int):
                    final_s = hex(self.value)
                else:
                    final_s = str(self.value)

                if self.operator and self.value not in inv:
                    final_s += '('

                if len(self.childs) > 1:
                    final_s += ','.join(str(child) for child in self.childs)
                elif self.childs:
                    final_s += str(self.childs[0])

                if self.operator:
                    final_s += inv.get(self.value, ')')

                return final_s

        exps = list(self.__iter_expressions())
        tree = dict()
        min_idx = default_min_idx = 999999
        removed_nodes = dict()
        for (type_t, value, idx, p_idx) in exps:

            if value == ',' and type_t == 'symbol':
                removed_nodes[idx] = p_idx
                continue

            p_idx = removed_nodes.get(p_idx, p_idx)
            min_idx = p_idx if p_idx < min_idx else min_idx

            if p_idx not in tree:
                tree[p_idx] = Tree(idx=p_idx)

            parent = tree[p_idx]
            tree[idx] = Tree(idx=idx, value=value, operator=type_t == 'symbol')
            parent.childs.append(tree[idx])

        # FIXME(dm) sometimes, binexport mess up with operands that have no expressions associated
        if min_idx == default_min_idx:
            return ''

        return str(tree[min_idx])

    def __repr__(self) -> str:
        return "<Op:%s>" % str(self)
