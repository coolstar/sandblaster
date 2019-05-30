#!/usr/bin/env python3

import sys
import argparse
import struct
import lief

CSTRING_SECTION = '__cstring'
CONST_SECTION = '__const'
DATA_SECTION = '__data'

def binary_get_word_size(binary: lief.MachO.Binary):
    assert(binary.header.magic in
        [lief.MachO.MACHO_TYPES.MAGIC, lief.MachO.MACHO_TYPES.MAGIC_64])
    return 4 if binary.header.magic == lief.MachO.MACHO_TYPES.MAGIC else 8

def unpack(bytes_list):
    return struct.unpack('<I' if len(bytes_list) == 4 else '<Q',
        bytes(bytes_list))[0]

def binary_get_string_from_address(binary: lief.MachO.Binary, vaddr: int):
    s = ''
    while True:
        try:
            byte = binary.get_content_from_virtual_address(vaddr, 1)
        except Exception as e:
            return None
        if byte == None or len(byte) == 0:
            return None
        byte = byte[0]
        if byte == 0:
            break
        vaddr += 1
        s += chr(byte)
    return s

def untag_pointer(p):
    return (p & ((1 << 48) -1)) | (0xffff << 48)

def get_cstring_section(binary: lief.MachO.Binary):
    seg = binary.get_segment('__TEXT')
    if seg:
        sects = [s for s in seg.sections if s.name == CSTRING_SECTION]
        assert(len(sects) == 1)
        return sects[0]
    return binary.get_section(CSTRING_SECTION)

def get_xref(binary: lief.MachO.Binary, vaddr: int):
    r = []
    word_size = binary_get_word_size(binary)
    i = 0
    for sect in binary.sections:
        content = sect.content[:len(sect.content)-len(sect.content)%word_size]
        content = [unpack(content[i:i+word_size])
            for i in range(0,len(content), word_size)]
        if word_size == 8:
            content = [untag_pointer(p) for p in content]
        r.extend((sect.virtual_address + i*word_size
            for i,p in enumerate(content) if p == vaddr))
    return r

def get_tables_section(binary: lief.MachO.Binary):
    str_sect = get_cstring_section(binary)
    strs = str_sect.search_all('default\x00')
    vaddr_str = str_sect.virtual_address + strs[0]
    xref_vaddrs = get_xref(binary, vaddr_str)
    if len(xref_vaddrs) > 0:
        sects = [binary.section_from_virtual_address(x) for x in xref_vaddrs]
        sects = [s for s in sects if 'const' in s.name.lower()]
        assert len(sects) >= 1 and all([sects[0] == s for s in sects])
        return sects[0]
    seg = binary.get_segment('__DATA')
    if seg:
        sects = [s for s in seg.sections if s.name == CONST_SECTION]
        assert(len(sects) <= 1)
        if len(sects) == 1:
            return sects[0]
    return binary.get_section(CONST_SECTION)

def get_data_section(binary: lief.MachO.Binary):
    seg = binary.get_segment('__DATA')
    if seg:
        sects = [s for s in seg.sections if s.name == DATA_SECTION]
        assert(len(sects) == 1)
        return sects[0]
    return binary.get_section(DATA_SECTION)


def is_vaddr_in_section(vaddr, section):
    return vaddr >= section.virtual_address \
        and vaddr < section.virtual_address + section.size

def extract_data_tables_from_section(binary, to_data, section):
    addr_size = binary_get_word_size(binary)
    startaddr = section.virtual_address
    endaddr = section.virtual_address + section.size
    tables = []
    vaddr = startaddr
    while vaddr <= endaddr - addr_size:
        ptr = unpack(
            binary.get_content_from_virtual_address(vaddr, addr_size))
        if addr_size == 8:
            ptr = untag_pointer(ptr)
        data = to_data(binary, ptr)
        if data == None:
            vaddr += addr_size
            continue
        table = [data]
        vaddr += addr_size
        while vaddr <= endaddr - addr_size:
            ptr = unpack(
                binary.get_content_from_virtual_address(vaddr, addr_size))
            if addr_size == 8:
                ptr = untag_pointer(ptr)
            data = to_data(binary, ptr)
            if data == None:
                break
            table.append(data)
            vaddr += addr_size
        if table not in tables:
            tables.append(table)
        vaddr += addr_size
    return tables

def extract_string_tables(binary: lief.MachO.Binary):
    return extract_data_tables_from_section(binary,
        binary_get_string_from_address, get_tables_section(binary))

def extract_separated_profiles(binary, string_tables):
    def get_profile_names():
        def transform(v):
            if len(v) <= 3:
                return None
            r = []
            tmp =[]
            for val in v:
                if val in ['default', '0123456789abcdef']:
                    r.append(tmp)
                    tmp = []
                else:
                    tmp.append(val)
            r.append(tmp)
            return r
        def get_sol(posible):
            r = [v for v in posible
                if 'com.apple.sandboxd' in v ]
            assert(len(r) == 1)
            return r[0]

        profile_names_v = [transform(v) for v in string_tables]
        profile_names_v = [v for v in profile_names_v if v != None]
        profile_names_v = [x for v in profile_names_v for x in v]
        return get_sol(profile_names_v)

    def get_profile_contents():
        def get_profile_content(binary, vaddr):
            addr_size = binary_get_word_size(binary)
            section = get_data_section(binary)
            if not is_vaddr_in_section(vaddr, section):
                return None
            data = binary.get_content_from_virtual_address(vaddr, 2*addr_size)
            if len(data) != 2*addr_size:
                return None
            data_vaddr = unpack(data[:addr_size])
            size = unpack(data[addr_size:])
            if not is_vaddr_in_section(vaddr, section):
                return None
            data = binary.get_content_from_virtual_address(data_vaddr, size)
            if len(data) != size:
                return None
            return bytes(data)
        contents_v = [v for v in extract_data_tables_from_section(binary, 
                get_profile_content, get_tables_section(binary))
            if len(v) > 3]
        assert(len(contents_v) == 1)
        return contents_v[0]

    profile_names = get_profile_names()
    profile_contents = get_profile_contents()
    assert(len(profile_names) == len(profile_contents))
    return zip(profile_names, profile_contents)

def extract_sbops(binary, string_tables):
    def transform(v):
        if len(v) <= 3:
            return None
        idxs = []
        for idx,val in enumerate(v):
            if val == 'default':
                idxs.append(idx)
        return [v[idx:] for idx in idxs]

    def get_sol(posible):
        assert(len(posible) >= 1)
        sol = []
        if len(posible) > 1:
            cnt = min(len(v) for v in posible)
            for vals in zip(*[v[:cnt] for v in posible]):
                if not all(v == vals[0] for v in vals):
                    break
                sol.append(vals[0])
        else:
            sol.append(posible[0][0])
            for c in posible[0][1:]:
                if c in ['HOME','default']:
                    break
                sol.append(c)
        return sol

    sbops_v = [transform(v) for v in string_tables]
    sbops_v = [v for v in sbops_v if v != None and v != []]
    sbops_v = [x for v in sbops_v for x in v]
    return get_sol(sbops_v)

def get_ios_major_version(version: str):
    return int(version.split('.')[0])

def findall(searchin, pattern):
    i = searchin.find(pattern)
    while i != -1:
        yield i
        i = searchin.find(pattern, i+1)

def check_regex(data: bytes, base_index: int):
    if base_index + 0x10 > len(data):
        return False
    size = struct.unpack('<I', data[base_index: base_index+0x4])[0]
    version = struct.unpack('>I', data[base_index+0x4: base_index+0x8])[0]
    if size > 0x1000 or size < 0x8 or base_index + size + 4 > len(data):
        return False
    if version != 3:
        return False
    subsize = struct.unpack('<H', data[base_index+0x8: base_index+0xa])[0]
    return size == subsize + 6

def check_bundle(data: bytes, base_index: int, ios_version: int):
    if len(data) - base_index < 50:
        return False
    re_offset, aux = struct.unpack('<2H', data[base_index+2:base_index+6])
    if ios_version >= 12:
        count = (aux - re_offset)*4
        # bundle should be big
        if count < 0x10:
            return False
    else:
        count = aux
    if count > 0x1000 or re_offset < 0x10:
        return False
    re_offset = base_index + re_offset*8
    if len(data) - re_offset < count * 2:
        return False
    for off_index in range(re_offset, re_offset + 2*count, 2):
        index = struct.unpack('<H', data[off_index:off_index+2])[0]
        if index == 0:
            if off_index < re_offset + 2*count - 4:
                return False
            continue
        index = base_index + index*8
        if not check_regex(data, index):
            return False
    return True


def extract_bundle_profiles(binary: lief.MachO.Binary, ios_version: int):
    matches = []
    for section in binary.sections:
        if section.name == '__text':
            continue
        content = bytes(section.content)
        for index in findall(content, b'\x00\x80'):
            if check_bundle(content, index, ios_version):
                matches.append(content[index:])
    assert len(matches) == 1
    return matches[0]

def main(args):
    if type(args.binary) == lief.MachO.FatBinary:
        assert(args.binary.size == 1)
        binary = args.binary.at(0)
    else:
        binary = args.binary

    retcode = 0
    string_tables = extract_string_tables(binary)
    if args.sbops_file != None:
        sbops = extract_sbops(binary, string_tables)
        sbops_str = '\n'.join(sbops)
        if args.sbops_file == '-':
            print(sbops_str)
        else:
            try:
                with open(args.sbops_file, 'w') as f:
                    f.write(sbops_str+'\n')
            except IOError as e:
                retcode = e.errno
                print(e, file=sys.stderr)
    if args.sbs_dir != None:
        if args.version <= 8:
            profiles = extract_separated_profiles(binary, string_tables)
            for name, content in profiles:
                try:
                    with open(args.sbs_dir + '/' + name + '.sb.bin', 'wb') as f:
                        f.write(content)
                except IOError as e:
                    retcode = e.errno
                    print(e, file=sys.stderr)
        else:
            content = extract_bundle_profiles(binary, args.version)
            try:
                with open(args.sbs_dir + '/sandbox_bundle', 'wb') as f:
                    f.write(content)
            except IOError as e:
                retcode = e.errno
                print(e, file=sys.stderr)
    exit(retcode)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Sandbox profiles and operations extraction tool(iOS <9)')
    parser.add_argument('binary', metavar='BINARY', type=lief.MachO.parse,
        help='path to sandboxd(iOS 5-8) / sandbox(seatbelt) kernel exenstion'+
        '(iOS 2-4 and 9-11) / kernelcache(iOS 12) '+
        'in order to extract sandbox operations')
    parser.add_argument('version', metavar='VERSION',
        type=get_ios_major_version, help='iOS version for given binary')
    parser.add_argument('-o','--output-sbops', dest='sbops_file', type=str,
        default=None,
        help='path to sandbox profile operations store file')
    parser.add_argument('-O','--output-profiles', dest='sbs_dir', type=str,
        default=None,
        help='path to directory in which sandbox profiles should be stored')

    args = parser.parse_args()
    exit(main(args))
