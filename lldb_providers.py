from enum import IntEnum
from pydoc import resolve
import re
import sys
import lldb
from lldb import (
    SBType,
    SBValue,
    SBData,
    SBError,
    eBasicTypeLong,
    eBasicTypeUnsignedLong,
    eBasicTypeUnsignedChar,
)

from lldb.formatters import Logger
from rust_types import STD_OPTION_REGEX, type_name_from_type, type_name_from_val

# Note: Minimum Python Version
# Per a recently merged RFC (https://github.com/llvm/llvm-project/pull/114807)
# python 3.8 is recommended as the minimum, since it is the minimum to run LLVM's test suite
# In LLVM 21 python >=3.8 will be a requirement

####################################################################################################
# This file contains two kinds of pretty-printers: summary and synthetic.
#
# Important classes from LLDB module:
#   SBValue: the value of a variable, a register, or an expression
#   SBType:  the data type; each SBValue has a corresponding SBType
#
# Summary provider is a function with the type `(SBValue, dict) -> str`.
#   The first parameter is the object encapsulating the actual variable being displayed;
#   The second parameter is an internal support parameter used by LLDB, and you should not touch it.
#
# Synthetic children is the way to provide a children-based representation of the object's value.
# Synthetic provider is a class that implements the following interface:
#
#     class SyntheticChildrenProvider:
#         def __init__(self, SBValue, dict)
#         def num_children(self)
#         def get_child_index(self, str)
#         def get_child_at_index(self, int)
#         def update(self)
#         def has_children(self)
#         def get_value(self)
#         def get_type_name(self)
#
#
# You can find more information and examples here:
#   1. https://lldb.llvm.org/varformats.html
#   2. https://lldb.llvm.org/use/python-reference.html
#   3. https://lldb.llvm.org/python_reference/lldb.formatters.cpp.libcxx-pysrc.html
#   4. https://github.com/llvm-mirror/lldb/tree/master/examples/summaries/cocoa
####################################################################################################

# A priority with this code, despite it being in python, is performance. Waiting on the debugger
# to populate values is pretty terrible user experience. This file is small enough, and touched
# infrequently enough, that a little bit of copy-pasting and some slightly obtuse code is probably
# worth it to save the overhead of some of python's more unfortunate performance characteristics.

PY3 = sys.version_info[0] == 3


class ValueBuilder:
    def __init__(self, valobj: lldb.SBValue):
        # type: (SBValue) -> ValueBuilder
        self.valobj = valobj
        process = valobj.GetProcess()
        self.endianness = process.GetByteOrder()
        self.pointer_size = process.GetAddressByteSize()

    def from_int(self, name, value):
        # type: (str, int) -> SBValue
        type = self.valobj.GetType().GetBasicType(eBasicTypeLong)
        data = SBData.CreateDataFromSInt64Array(
            self.endianness, self.pointer_size, [value]
        )
        return self.valobj.CreateValueFromData(name, data, type)

    def from_uint(self, name, value):
        # type: (str, int) -> SBValue
        type = self.valobj.GetType().GetBasicType(eBasicTypeUnsignedLong)
        data = SBData.CreateDataFromUInt64Array(
            self.endianness, self.pointer_size, [value]
        )
        return self.valobj.CreateValueFromData(name, data, type)


def unwrap_unique_or_non_null(unique_or_nonnull):
    # BACKCOMPAT: rust 1.32
    # https://github.com/rust-lang/rust/commit/7a0911528058e87d22ea305695f4047572c5e067
    # BACKCOMPAT: rust 1.60
    # https://github.com/rust-lang/rust/commit/2a91eeac1a2d27dd3de1bf55515d765da20fd86f
    ptr = unique_or_nonnull.GetChildMemberWithName("pointer")
    return ptr if ptr.TypeIsPointerType() else ptr.GetChildAtIndex(0)


class DefaultSyntheticProvider:
    __slots__ = "valobj"
    def __init__(self, valobj: SBValue, dict):
        # logger = Logger.Logger()
        # logger >> "Default synthetic provider for " + str(valobj.GetName())
        self.valobj = valobj

    def num_children(self) -> int:
        return self.valobj.GetNumChildren()

    def get_child_index(self, name: str) -> int:
        return self.valobj.GetIndexOfChildWithName(name)

    def get_child_at_index(self, index: int) -> SBValue:
        return self.valobj.GetChildAtIndex(index)

    def update(self):
        pass

    def has_children(self) -> bool:
        return self.valobj.MightHaveChildren()

    def get_type_name(self) -> str:
        return type_name_from_val(self.valobj)


# class EmptySyntheticProvider:
#     def __init__(self, valobj, dict):
#         # type: (SBValue, dict) -> EmptySyntheticProvider
#         # logger = Logger.Logger()
#         # logger >> "[EmptySyntheticProvider] for " + str(valobj.GetName())
#         self.valobj = valobj

#     def num_children(self):
#         # type: () -> int
#         return 0

#     def get_child_index(self, name):
#         # type: (str) -> int
#         return None

#     def get_child_at_index(self, index):
#         # type: (int) -> SBValue
#         return None

#     def update(self):
#         # type: () -> None
#         pass

#     def has_children(self):
#         # type: () -> bool
#         return False


class PrimitiveSyntheticProvider:
    def __init__(self, valobj, dict):
        # type: (SBValue, dict) -> DefaultSyntheticProvider
        # logger = Logger.Logger()
        # logger >> "Default synthetic provider for " + str(valobj.GetName())
        self.valobj = valobj

    def num_children(self):
        # type: () -> int
        return 0

    def get_child_index(self, name):
        # type: (str) -> int
        return -1

    def get_child_at_index(self, index):
        # type: (int) -> SBValue
        return None

    def update(self):
        # type: () -> None
        pass

    def has_children(self):
        # type: () -> bool
        return self.valobj.MightHaveChildren()

    def get_type_name(self):
        # type: () -> str
        return type_name_from_val(self.valobj)


# def u8_summary_provider(valobj, dict):
#     val: int = valobj.GetValueAsUnsigned()
#     if 32 <= val < 127:
#         return f"{val}  '{val:c}'"
#     else:
#         return f"{val}  {val:#x}"


# class Stdu8SyntheticProvider(DefaultSyntheticProvider):
    # def __init__(self, valobj, dict):
    #     # type: (SBValue, dict) -> str
    #     valobj = valobj.Cast(valobj.GetTarget().module[0].FindFirstType("u8"))
    #     self.valobj = valobj
    #     # self.valobj.GetValue = lambda : "eef"

    # def num_children(self, max_children):
    #     return 2

    # def has_children(self):
    #     return True

    # def get_child_index(self, name):
    #     # return 0
    #     if name == "hex":
    #         return 0
    #     elif name == "ascii":
    #         return 1

    #     return -1

    # def get_child_at_index(self, index):
    #     # dec = self.valobj.CreateValueFromExpression(f"{index}", f"{self.valobj.GetName()}")
    #     # dec = self.
    #     # return dec
    #     if index == 0:
    #         hex = f"{self.valobj.GetValueAsUnsigned():#x}"
    #         return self.valobj.CreateValueFromExpression("hex", f'"{hex}"')
    #     elif index == 1:
    #         char = f"{self.valobj.GetValueAsUnsigned():c}"
    #         return self.valobj.CreateValueFromExpression("utf8", f'""{char}"";')

    # def GetSyntheticValue(self):
    #     self.valobj

    # def update(self):
    #     self.dec_ch = self.valobj.synthetic_child_from_data(
    #         "hex", self.valobj.GetData(), self.valobj.GetType()
    #     )
    #     self.dec_ch.SetFormat(lldb.eFormatChar)
    # def get_type_name(self):
    #     return "u8"

class RefSyntheticProvider(DefaultSyntheticProvider):
    def __init__(self, valobj, dict):
        # type: (SBValue, dict) -> DefaultSyntheticProvider
        # logger = Logger.Logger()
        # logger >> "Default synthetic provider for " + str(valobj.GetName())
        self.valobj = valobj

    def get_type_name(self) -> str:
        type: SBType = self.valobj.GetType()
        name_parts: list[str] = []

        # "&&" indicates an rval reference. This doesn't technically mean anything in Rust, but the
        # debug info is generated as such so we can differentiate between "ref-to-ref" (illegal in
        # TypeSystemClang) and "ref-to-pointer".
        #
        # Whenever there is a "&&", we can be sure that the pointer it is pointing to is actually
        # supposed to be a reference. (e.g. u8 *&& -> &mut &mut u8)
        was_r_ref: bool = False
        ptr_type: SBType = type
        ptee_type: SBType = type.GetPointeeType()

        while ptr_type.is_pointer or ptr_type.is_reference:
            # remove the `const` modifier as it indicates the const-ness of any pointer/ref pointing *to* it
            # not its own constness
            # For example:
            # const u8 *const * -> &&u8
            # u8 *const * -> &&mut u8
            # const u8 ** -> &mut &u8
            # u8 ** -> &mut &mut u8
            ptr_name: str = ptr_type.GetName().removesuffix("const")
            ptee_name: str = ptee_type.GetName()

            is_ref: bool = False

            if was_r_ref or ptr_name[-1] == "&":
                is_ref = True

            was_r_ref = ptr_name[-2:] == "&&"

            is_const: bool = False

            if ptee_type.is_pointer or ptee_type.is_reference:
                if ptee_name.endswith("const"):
                    if is_ref:
                        name_parts.append("&")
                    else:
                        name_parts.append("*const ")
                else:
                    if is_ref:
                        name_parts.append("&mut ")
                    else:
                        name_parts.append("*mut ")

            else:
                if ptee_name.startswith("const "):
                    if is_ref:
                        name_parts.append("&")
                    else:
                        name_parts.append("*const ")
                else:
                    if is_ref:
                        name_parts.append("&mut ")
                    else:
                        name_parts.append("*mut ")

            ptr_type = ptee_type
            ptee_type = ptee_type.GetPointeeType()

        name_parts.append(type_name_from_type(ptr_type.GetUnqualifiedType()))
        return "".join(name_parts)

def RefSummaryProvider(valobj: SBValue, dict) -> str:
    while True:
        t: lldb.SBType = valobj.GetType()
        if t.is_pointer or t.is_reference:
            valobj = valobj.Dereference()
        else:
            break
    return valobj.value


class ArraySyntheticProvider:
    def __init__(self, valobj, dict):
        self.valobj = valobj
        self.update()

    def update(self):
        self.children = self.valobj.children
        self.length = len(self.children)

        # this turns C-style type names (long, unsigned char, etc.) into rust type names (i32, u8, etc.)
        # we cast the whole array to the target type as it's cheaper than casting each individual value
        unresolved_type = self.valobj.GetType().GetArrayElementType()
        self.element_type_name = type_name_from_type(unresolved_type)
        self.element_type = self.valobj.target.FindFirstType(self.element_type_name)
        new_array_type = self.element_type.GetArrayType(self.length)
        self.valobj.Cast(new_array_type)

    def num_children(self):
        return self.valobj.GetNumChildren()

    def get_child_index(self, name):
        index = name.lstrip("[").rstrip("]")
        if index.isdigit():
            return int(index)

        return -1

    def get_child_at_index(self, index):
        # type: (int) -> SBValue
        if not 0 <= index < self.length:
            return None
        return self.children[index]

    def has_children(self):
        return True

    def get_type_name(self):
        return f"[{self.element_type_name}; {self.length}]"


class StdSliceSyntheticProvider:
    def __init__(self, valobj, dict):
        self.valobj = valobj
        self.update()

    def update(self):
        self.length = self.valobj.GetChildMemberWithName("length").GetValueAsUnsigned()
        self.data_ptr = self.valobj.GetChildMemberWithName("data_ptr")
        unresolved_type = self.data_ptr.GetType().GetPointeeType()
        self.element_type_name = type_name_from_type(unresolved_type)
        self.element_type = self.valobj.target.FindFirstType(self.element_type_name)
        self.element_size = self.element_type.GetByteSize()

    def num_children(self):
        return self.length

    def get_child_index(self, name):
        index = name.lstrip("[").rstrip("]")
        if index.isdigit():
            return int(index)

        return -1

    def get_child_at_index(self, index):
        # type: (int) -> SBValue
        if not 0 <= index < self.length:
            return None
        start = self.data_ptr.GetValueAsUnsigned()
        address = start + index * self.element_size
        element = self.data_ptr.CreateValueFromAddress(
            f"[{index}]", address, self.element_type
        )
        return element

    def has_children(self):
        return True

    def get_type_name(self):
        if self.valobj.GetTypeName().startswith("ref_mut"):
            return f"&mut [{self.element_type_name}]"
        else:
            return f"&[{self.element_type_name}]"

        # element_name = ""
        # if self.num_children > 0:
        #     element_name =
        # else:

        #     element_name = self.element_type.GetName()


def sequence_formatter(output, valobj, dict):
    # type: (str,lldb.SBValue, _) -> str
    length = valobj.GetNumChildren()

    long = False
    for i in range(0, length):
        if len(output) > 32:
            long = True
            break
        child = valobj.GetChildAtIndex(i)
        output += f"{child.value}, "
    if long:
        output = f"(len: {length}) " + output + "..."
    else:
        output = output[:-2]

    return output


def ArraySummaryProvider(valobj, dict):
    output = sequence_formatter("[", valobj, dict)
    output += "]"
    return output


# Type summary
def StdSliceSummaryProvider(valobj, dict):
    output = sequence_formatter("&[", valobj, dict)
    output += "]"
    return output


class MSVCStdVecSyntheticProvider:
    def __init__(self, valobj: lldb.SBValue, dict):
        self.valobj = valobj
        self.update()

    def update(self):
        self.length = self.valobj.GetChildMemberWithName("len").GetValueAsUnsigned()
        self.data_ptr = (
            self.valobj.GetChildMemberWithName("buf")
            .GetChildMemberWithName("inner")
            .GetChildMemberWithName("ptr")
            .GetChildMemberWithName("pointer")
            .GetChildMemberWithName("pointer")
        )

        # annoyingly, vec's constituent type isn't guaranteed to be contained anywhere useful.
        # Some functions have it, but those functions only exist in binary when they're used.
        # that means it's time for string-based garbage.

        # acquire the first generic parameter via its type name
        _, _, end = self.valobj.GetTypeName().partition("<")
        element_name, _, _ = end.partition(",")

        # this works even for built-in rust types like `u32` because internally it's just a `typedef`
        # i really REALLY wish there was a better way to do this, but either i'm dumb or lldb does
        # not care about generic parameters AND the type data doesn't exist in the vec
        self.element_type = self.valobj.target.FindFirstType(element_name)

        self.element_size = self.element_type.GetByteSize()

    def num_children(self):
        return self.length

    def get_child_index(self, name):
        index = name.lstrip("[").rstrip("]")
        if index.isdigit():
            return int(index)

        return -1

    def get_child_at_index(self, index):
        # type: (int) -> SBValue
        if not (0 <= index < self.length):
            return None
        start = self.data_ptr.GetValueAsUnsigned()
        offset = start + index * self.element_size
        element = self.data_ptr.CreateValueFromAddress(
            f"[{index}]", offset, self.element_type
        )
        return element

    def has_children(self):
        return True

    def get_type_name(self):
        return f"Vec<{self.element_type.name}>"


def StdVecSummaryProvider(valobj, dict):
    output = sequence_formatter("vec![", valobj, dict)
    output += "]"
    return output


class MSVCTupleSyntheticProvider(DefaultSyntheticProvider):
    def get_type_name(self):
        output = "("
        for i in range(self.valobj.GetNumChildren()):
            name = type_name_from_val(self.valobj.GetChildAtIndex(i))
            output += f"{name}, "
        output = output[:-2] + ")"

        return output

class MSVCUnitSyntheticProvider(DefaultSyntheticProvider):
    def get_type_name(self):
        return "()"


def TupleSummaryProvider(valobj, dict):
    output = sequence_formatter("(", valobj, dict)
    output += ")"
    return output


class MSVCstrSyntheticProvider:
    def __init__(self, valobj, dict):
        # type: (SBValue, dict) -> SyntheticProvider
        self.valobj = valobj
        self.update()

    def update(self):
        # type: () -> None
        self.data_ptr = self.valobj.GetChildMemberWithName("data_ptr")
        self.length = self.valobj.GetChildMemberWithName("length").GetValueAsUnsigned()

    def has_children(self):
        # type: () -> bool
        return True

    def num_children(self):
        # type: () -> int
        return self.length

    def get_child_index(self, name):
        # type: (str) -> int
        index = name.lstrip("[").rstrip("]")
        if index.isdigit():
            return int(index)

        return -1

    def get_child_at_index(self, index):
        # type: (int) -> SBValue
        if not 0 <= index < self.length:
            return None
        start = self.data_ptr.GetValueAsUnsigned()
        address = start + index
        element = self.data_ptr.CreateValueFromAddress(
            f"[{index}]", address, self.data_ptr.GetType().GetPointeeType()
        )
        return element

    def get_type_name(self):
        return "&str"


def MSVCstrSummaryProvider(valobj, dict):
    pointer = valobj.GetNonSyntheticValue().GetChildMemberWithName("data_ptr")
    length = (
        valobj.GetNonSyntheticValue()
        .GetChildMemberWithName("length")
        .GetValueAsUnsigned()
    )
    if length <= 0:
        return ""

    error = lldb.SBError()
    process = pointer.GetProcess()
    data = process.ReadMemory(pointer.GetValueAsUnsigned(), length, error)
    if error.Success():
        return data.decode("utf8", "replace")
    else:
        raise Exception("ReadMemory error: %s", error.GetCString())


class StdStringSyntheticProvider:
    def __init__(self, valobj, dict):
        # type: (SBValue, dict) -> SyntheticProvider
        self.valobj = valobj
        self.update()

    def update(self):
        # type: () -> None
        inner_vec = self.valobj.GetChildMemberWithName("vec").GetNonSyntheticValue()
        self.data_ptr = (
            inner_vec.GetChildMemberWithName("buf")
            .GetChildMemberWithName("inner")
            .GetChildMemberWithName("ptr")
            .GetChildMemberWithName("pointer")
            .GetChildMemberWithName("pointer")
        )
        self.length = inner_vec.GetChildMemberWithName("len").GetValueAsUnsigned()
        self.element_type = self.data_ptr.GetType().GetPointeeType()

    def has_children(self):
        # type: () -> bool
        return True

    def num_children(self):
        # type: () -> int
        return self.length

    def get_child_index(self, name):
        # type: (str) -> int
        index = name.lstrip("[").rstrip("]")
        if index.isdigit():
            return int(index)

        return -1

    def get_child_at_index(self, index):
        # type: (int) -> SBValue
        if not 0 <= index < self.length:
            return None
        start = self.data_ptr.GetValueAsUnsigned()
        address = start + index
        element = self.data_ptr.CreateValueFromAddress(
            f"[{index}]", address, self.element_type
        )
        element.SetFormat(lldb.eFormatChar)
        return element

    def get_type_name(self):
        return "String"


def StdStringSummaryProvider(valobj, dict):
    inner_vec = (
        valobj.GetNonSyntheticValue()
        .GetChildMemberWithName("vec")
        .GetNonSyntheticValue()
    )

    pointer = (
        inner_vec.GetChildMemberWithName("buf")
        .GetChildMemberWithName("inner")
        .GetChildMemberWithName("ptr")
        .GetChildMemberWithName("pointer")
        .GetChildMemberWithName("pointer")
    )

    length = inner_vec.GetChildMemberWithName("len").GetValueAsUnsigned()

    if length <= 0:
        return ""
    error = lldb.SBError()
    process = pointer.GetProcess()
    data = process.ReadMemory(pointer.GetValueAsUnsigned(), length, error)
    if error.Success():
        return data.decode("utf8", "replace")
    else:
        raise Exception("ReadMemory error: %s", error.GetCString())


class VariantType(IntEnum):
    DISCRIMINANT = (0,)
    TUPLE = (1,)
    STRUCT = (2,)


class MSVCEnumSyntheticProvider:
    def __init__(self, valobj, dict):
        # type: (SBValue, dict) -> SyntheticProvider
        self.valobj = valobj.GetNonSyntheticValue()
        num_children = self.valobj.GetNumChildren()
        # self.is_sum = num_children != 0
        # if self.is_sum:
        #     self.variant_count = num_children - 1
        # see: compiler\rustc_codegen_llvm\src\debuginfo\metadata\enums\cpp_like.rs
        # niche enums will use DISCR_BEGIN and DISCR_END, non-niche will use DISCR_EXACT
        #
        # Unfortunately, you can't actually *read* these values, as they're static fields.
        # My best guess is that it's because the types of the variants are technically fake,
        # thus they have no SBModule, thus LLDB can't figure out where to read the information from.
        self.is_niche = False

        for i in range(num_children - 1):
            t: lldb.SBType = self.valobj.GetChildAtIndex(i).GetType()

            if t.name.endswith(f"Variant{i}") and not t.GetStaticFieldWithName("DISCR_EXACT").IsValid():
                self.is_niche = True
                break

        self.update()

    def update(self):
        # type: () -> None
        if self.is_niche:
            self.variant = self.valobj
            return

        self.tag = self.valobj.GetChildMemberWithName("tag").GetValueAsUnsigned()

        var = (
            self.valobj.GetNonSyntheticValue()
            .GetChildMemberWithName(f"variant{self.tag}")
            .GetNonSyntheticValue()
        )
        self.variant = var.GetChildMemberWithName("value").GetNonSyntheticValue()
        if self.variant.GetNumChildren() == 0:
            self.variant_type = VariantType.DISCRIMINANT
        elif self.variant.GetChildAtIndex(0).GetName() == "__0":
            self.variant_type = VariantType.TUPLE
        else:
            self.variant_type = VariantType.STRUCT

        try:
            t_name = (
                var.GetType()
                .GetStaticFieldWithName("NAME")
                .GetType()
                .enum_members[self.tag]
                .name
            )
            self.variant_name = t_name
        except:
            self.variant_name = ""

    def has_children(self):
        # type: () -> bool
        return self.variant.MightHaveChildren()

    def num_children(self):
        # type: () -> int
        return self.variant.GetNumChildren()

    def get_child_index(self, name):
        # type: (str) -> int
        return self.variant.GetIndexOfChildWithName(name)

    def get_child_at_index(self, index):
        # type: (int) -> SBValue
        return self.variant.GetChildAtIndex(index)

    def get_type_name(self):
        # type: () -> str
        # skip the "enum2$<" prefix and ">" suffix
        # return self.valobj.GetTypeName().removeprefix("enum2$<").removesuffix(">")
        return type_name_from_val(self.valobj)

# Niche enums are populated on a "best case" basis. See the rust debug info layout before continuing
# https://github.com/rust-lang/rust/blob/caa81728c37f5ccfa9a0979574b9272a67f8a286/compiler/rustc_codegen_llvm/src/debuginfo/metadata/enums/cpp_like.rs#L49
#
# The issue is that LLDB cannot currently inspect the static class members DISCR_EXACT/DISCR_BEGIN/DISCR_END
# to determine their values, it can only see that they exist at all. SBType.GetStaticFieldWithName exists, but
# does not always returns none when we try to inspect its integer value. This means it is either exceedingly
# difficult or downright impossible to check `is_in_range` and determine which variant to use at time of
# writing.
#
# @Walnut356: I've fiddled with this for several hours and I'm not seeing a way around it. Either rust needs
# to change the debuginfo sent to LLVM, or LLDB needs an alternative way to inspect static class members.
# I'm no expert on clang internals, so bear with me:
#
# SBTypeStaticField calls GetConstantValue, which internally calls lldb_private::CompilerDecl::GetConstantValue()
# that function internally calls TypeSystem::DeclGetConstantValue() (which in this case uses TypeSystemClang)
#
# TypeSystemClang casts a void* to a clang::Decl, then attempts to raise it to a VarDecl (which extends NamdedDecl which
# extends Decl). That cast may fail, iunno.
# If it doesn't, it attempts to call VarDecl::getInit(), which returns an Expr, which I think is inspecting AST nodes?
# Except those AST nodes won't exist since these values were conjured out of thin air, so it probably fails there.
# If it doesn't, it attempts to call VarDecl::getIntegerConstantExpr, which again inspects AST nodes for the value
# that was assigned to it, but that value won't exist since, again, this value has been hallucinated directly into the
# type system.
#
# The public API just doesn't have the tools for me to
def MSVCEnumSummaryProvider(valobj, dict):
    if valobj.IsSynthetic():
        valobj = valobj.GetNonSyntheticValue()
    val = MSVCEnumSyntheticProvider(valobj, dict)
    if val.is_niche:
        return str(val.tag)
    if val.variant_type == VariantType.TUPLE:
        return f"{val.variant_name}{TupleSummaryProvider(val.variant, dict)}"
    elif val.variant_type == VariantType.STRUCT:
        var_list = (
            str(val.variant.GetNonSyntheticValue()).split("= ", 1)[1].splitlines()
        )
        vars = [x.strip() for x in var_list if x not in ("{", "}")]
        if vars[0][0] == "(":
            vars[0] = vars[0][1:]
        if vars[-1][-1] == ")":
            vars[-1] = vars[-1][:-1]

        return f'{val.variant_name}{{{", ".join(vars)}}}'
    else:
        return val.variant_name


class OptionSyntheticProvider(MSVCEnumSyntheticProvider):
    def get_type_name(self):
        return type_name_from_val(self.valobj)
