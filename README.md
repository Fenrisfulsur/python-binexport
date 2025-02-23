# Python-Binexport

``python-binexport`` is a python module aiming to give a friendly interface to load
and manipulate binexport files.

## What is binexport ?

Binexport is a ``protobuf`` format used by Bindiff to extract IDA database and
to process them outside. It gives a very optimizated (in size) representation
of the program.

## Dependencies

As ``python-binexport`` entirely relies on `Binexport`, it **has to be installed first**.
The project is available at: https://github.com/google/binexport

Note that python-binexport **requires IDA >=7.2** (as it calls the ``BinExportBinary`` IDC function). 

**Warning**: Make sure the IDA Pro binexport plugin is properly installed (and working manually) before trying
    to use it from the python library (it can hang if not installed).


## Installation

    pip install python-binexport



## Python module usage

The main intended usage of ``python-binexport`` is as a python module.
The main entry point is the class ``ProgramBinExport`` which triggers the
loading of the whole file. Here is a snippet to iterate on every expression
of every instruction in the program:

```python
from binexport import ProgramBinExport

p = ProgramBinExport("myprogram.BinExport")
for fun_addr, fun in p.items():
    for bb_addr, bb in fun.items():
        for inst_addr, inst in bb.items():
            for operand in inst.operands:
                for exp in operand.expressions:
                    pass  # Do whatever at such deep level
```

Obviously ``ProgramBinExport``, ``FunctionBinExport``, ``InstructionBinExport`` and ``OperandBinExport``
all provides various attributes and method to get their type, and multiple other infos.

> If the module ``idascript`` is installed you can directly generate a BinExport
> file using the ``Program.from_binary_file`` static method.

## Command line usage

The executable script ``binexporter`` provides a very basic utility
to export a BinExport file straight from the command line *(without
having to launch IDA etc..)*. This is basically a wrapper for ``Program.from_binary_file``.