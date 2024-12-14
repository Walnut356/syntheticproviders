from enum import IntEnum
import functools
import re

import lldb


class RustType(IntEnum):
    OTHER = 1
    STRUCT = 2
    TUPLE = 3
    CSTYLE_VARIANT = 4
    TUPLE_VARIANT = 5
    STRUCT_VARIANT = 6
    ENUM = 7
    EMPTY = 8
    SINGLETON_ENUM = 9
    REGULAR_ENUM = 10
    COMPRESSED_ENUM = 11
    REGULAR_UNION = 12

    STD_STRING = 13
    STD_OS_STRING = 14
    STD_STR = 15
    STD_SLICE = 16
    STD_VEC = 17
    STD_VEC_DEQUE = 18
    STD_BTREE_SET = 19
    STD_BTREE_MAP = 20
    STD_HASH_MAP = 21
    STD_HASH_SET = 22
    STD_RC = 23
    STD_ARC = 24
    STD_CELL = 25
    STD_REF = 26
    STD_REF_MUT = 27
    STD_REF_CELL = 28
    STD_NONZERO_NUMBER = 29
    STD_PATH = 30
    STD_PATHBUF = 31


TUPLE_REGEX = re.compile(r"")
STD_STRING_REGEX = re.compile(r"^(alloc::([a-z_]+::)+)String$")
STD_STR_REGEX = re.compile(r"^&(mut )?str$")
STD_SLICE_REGEX = re.compile(r"^&(mut )?\[.+\]$")
STD_OS_STRING_REGEX = re.compile(r"^(std::ffi::([a-z_]+::)+)OsString$")
STD_VEC_REGEX = re.compile(r"^(alloc::([a-z_]+::)+)Vec<.+>$")
STD_VEC_DEQUE_REGEX = re.compile(r"^(alloc::([a-z_]+::)+)VecDeque<.+>$")
STD_BTREE_SET_REGEX = re.compile(r"^(alloc::([a-z_]+::)+)BTreeSet<.+>$")
STD_BTREE_MAP_REGEX = re.compile(r"^(alloc::([a-z_]+::)+)BTreeMap<.+>$")
STD_HASH_MAP_REGEX = re.compile(r"^(std::collections::([a-z_]+::)+)HashMap<.+>$")
STD_HASH_SET_REGEX = re.compile(r"^(std::collections::([a-z_]+::)+)HashSet<.+>$")
STD_RC_REGEX = re.compile(r"^(alloc::([a-z_]+::)+)Rc<.+>$")
STD_ARC_REGEX = re.compile(r"^(alloc::([a-z_]+::)+)Arc<.+>$")
STD_CELL_REGEX = re.compile(r"^(core::([a-z_]+::)+)Cell<.+>$")
STD_REF_REGEX = re.compile(r"^(core::([a-z_]+::)+)Ref<.+>$")
STD_REF_MUT_REGEX = re.compile(r"^(core::([a-z_]+::)+)RefMut<.+>$")
STD_REF_CELL_REGEX = re.compile(r"^(core::([a-z_]+::)+)RefCell<.+>$")
STD_NONZERO_NUMBER_REGEX = re.compile(r"^(core::([a-z_]+::)+)NonZero<.+>$")
STD_PATHBUF_REGEX = re.compile(r"^(std::([a-z_]+::)+)PathBuf$")
STD_PATH_REGEX = re.compile(r"^&(mut )?(std::([a-z_]+::)+)Path$")
STD_OPTION_REGEX = re.compile(r"enum2\$<core::option::Option<")

# lldb defines a function similar to this, but it uses a gigantic if-elif block which is slow. The
# enumeration is literally just ints, so i turned it into a table.
NUM_TYPE_MAP = [
    (False, False),
    (False, False),
    (True, False),
    (True, True),
    (True, False),
    (True, False),
    (True, True),
    (True, False),
    (True, False),
    (True, False),
    (True, False),
    (True, True),
    (True, False),
    (True, True),
    (True, False),
    (True, True),
    (True, False),
    (True, True),
    (True, False),
    (True, True),
    (True, False),
    (False, False),
    (True, True),
    (True, True),
    (True, True),
    (True, True),
    (True, True),
    (True, True),
    (True, True),
    (False, False),
    (False, False),
    (False, False),
    (False, False),
    (False, False),
]


def type_name_from_val(valobj):
    # type: (lldb.SBValue) -> str
    """takes an SBValue. If that type is a c-style numeric type (e.g. int, unsigned long, double),
    returns the equivalent Rust type (e.g. i32, u64, f64). Attempts to resolve raw pointer types into
    their equivalent reference or rust-pointer types.
    """
    type = valobj.GetType()

    basic_type = type.GetBasicType()
    numeric, signed = NUM_TYPE_MAP[basic_type]

    if not numeric:
        return type_name_from_name(type.GetName()).replace(" >", ">")

    bit_width = type.GetByteSize() * 8
    if lldb.eBasicTypeHalf <= basic_type <= lldb.eBasicTypeLongDouble:
        return f"f{bit_width}"

    output = ""
    if signed:
        output = "i"
    else:
        output = "u"

    output += f"{bit_width}"

    return output

def type_name_from_type(type):
    # type: (lldb.SBType) -> str
    """takes an lldb.SBType. If that type is a c-style numeric type (e.g. int, unsigned long, double),
    returns the equivalent Rust type (e.g. i32, u64, f64). Attempts to resolve raw pointer types into
    their equivalent reference or rust-pointer types.
    """
    basic_type = type.GetBasicType()
    numeric, signed = NUM_TYPE_MAP[basic_type]

    if not numeric:
        name = type.GetName()
        # if valobj is not None and type.IsPointerType():
        #     name = resolve_pointer(valobj)
        return name

    bit_width = type.GetByteSize() * 8
    if lldb.eBasicTypeHalf <= basic_type <= lldb.eBasicTypeLongDouble:
        return f"f{bit_width}"

    output = ""
    if signed:
        output = "i"
    else:
        output = "u"

    output += f"{bit_width}"

    return output

# RECURSIVE
# The cache probably isn't strictly necessary, but due to the recursion and the number
# of string manipulations, I'd really rather be safe than sorry and not recalculate it every time.
@functools.cache
def type_name_from_name(name):
    # print(name)
    if name.startswith("enum2$<"):
        name = name.replace("enum2$<", "", 1)
        if name.endswith(" >"):
            name = name[:-2]
        elif name.endswith(">"):
            name = name[:-1]
        return type_name_from_name(name)
    elif name.startswith("core::option::Option"):
        name = name.replace("core::option::Option<", "", 1)
        if name.endswith(" >"):
            name = name[:-2]
        elif name.endswith(">"):
            name = name[:-1]
        return "Option<" + type_name_from_name(name) + ">"
    elif name.startswith("alloc::vec::Vec"):
        name = name.removeprefix("alloc::vec::Vec<")
        name = name.replace(",alloc::alloc::Global>", "", 1)
        return "Vec<" + type_name_from_name(name) + ">"
    else:
        return name


#DECL_REGEX = re.compile("let .*: (.*) =")


# def resolve_pointer(valobj):
#     declaration = valobj.GetDeclaration()
#     source_manager = valobj.target.GetSourceManager()
#     stream = lldb.SBStream()

#     source_manager.DisplaySourceLinesWithLineNumbers(
#         declaration.GetFileSpec(), declaration.GetLine(), 0, 0, "", stream
#     )

#     for groups in DECL_REGEX.groups(stream.GetData()):
#         pass
