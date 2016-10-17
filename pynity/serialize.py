import io
import logging
import uuid

from . import utils, ioutils, engine, rtti, stringtable

log = logging.getLogger("pynity.serialize")

class SerializedFile(ioutils.AutoCloseable):

    versions = [5, 6, 8, 9, 14, 15]

    @classmethod
    def probe_path(cls, path):
        with ioutils.ChunkedFileIO.open(path, "rb") as fp:
            return cls.probe_file(fp)

    @classmethod
    def probe_file(cls, file):
        r = ioutils.BinaryIO(file, order=ioutils.BIG_ENDIAN)

        # get file size
        r.seek(0, io.SEEK_END)
        file_size = r.tell()
        r.seek(0, io.SEEK_SET)

        # check for minimum header size
        if file_size < 16:
            return False

        # read some parts of the header
        r.seek(4, io.SEEK_SET)
        header_file_size = r.read_int32()
        header_version = r.read_int32()
        r.seek(0, io.SEEK_SET)

        # check version range
        if not cls.versions[0] <= header_version <= cls.versions[-1]:
            return False

        # check file size
        return file_size == header_file_size

    def __init__(self, file, archive=None):
        self._cached_types = {}
        self._archive = archive

        # open file from string or use it directly, depending on the type
        if isinstance(file, str):
            fp = ioutils.ChunkedFileIO.open(file, "rb")
        else:
            fp = file

        r = self.r = ioutils.BinaryIO(fp, order=ioutils.BIG_ENDIAN)

        # read metadata
        self._read_header(r)
        self._read_types(r)
        self._read_object_infos(r)
        self._read_script_types(r)
        self._read_externals(r)

        # create object list from metadata
        self._read_objects(r)

    def _read_header(self, r):
        if not r:
            r = self.r

        header = self.header = utils.ObjectDict()
        header.metadata_size = r.read_int32()
        header.file_size = r.read_int32()
        header.version = r.read_int32()
        header.data_offset = r.read_int32()

        if header.version not in self.versions:
            raise NotImplementedError("Unsupported format version: %d"
                                      % header.version)

        if header.data_offset > header.file_size:
            raise SerializedFileError("Invalid data offset: %d"
                                      % header.data_offset)

        if header.metadata_size > header.file_size:
            raise SerializedFileError("Invalid metadata size: %d"
                                      % header.metadata_size)

        # newer formats usually use little-endian for the rest of the file
        if header.version > 8:
            header.endianness = r.read_int8()
            r.read(3) # reserved
            r.order = header.endianness
        elif header.version > 5:
            r.order = ioutils.LITTLE_ENDIAN

    def _read_types(self, r):
        types = self.types = utils.ObjectDict()

        # older formats store the object data before the structure data
        if self.header.version < 9:
            types_offset = self.header.file_size - self.header.metadata_size + 1
            r.seek(types_offset)

        if self.header.version > 6:
            types.signature = r.read_cstring()
            types.attributes = r.read_int32()
        elif self._archive:
            types.signature = self._archive.header.unity_revision

        if self.header.version > 13:
            types.embedded = r.read_bool8()

        types.classes = {}
        types_raw = self.types_raw = {}

        num_classes = r.read_int32()

        if self.header.version <= 13:
            types.embedded = num_classes > 0

        for _ in range(num_classes):
            class_type = utils.ObjectDict()
            class_id = r.read_int32()

            if self.header.version > 13:
                if class_id < 0:
                    class_type.script_id = r.read_hex(16)

                class_type.old_type_hash = r.read_hex(16)

                if types.embedded:
                    type_pos = r.tell()
                    class_type.type_tree = rtti.read_type_node(r)
                    type_size = r.tell() - type_pos

                    r.seek(type_pos)
                    types_raw[class_id] = r.read(type_size)
            else:
                type_pos = r.tell()
                class_type.type_tree = rtti.read_type_node_old(r)
                type_size = r.tell() - type_pos

                r.seek(type_pos)
                types_raw[class_id] = r.read(type_size)

            if class_id in types.classes:
                raise SerializedFileError("Duplicate class ID %d" % class_id)

            types.classes[class_id] = class_type

        # padding
        if 6 < self.header.version < 13:
            r.read_int32()

    def _read_object_infos(self, r):
        object_infos = self.object_infos = {}

        num_entries = r.read_int32()

        for _ in range(num_entries):
            if self.header.version > 13:
                r.align(4)
                path_id = r.read_uint64()
            else:
                path_id = r.read_uint32()

            obj_info = utils.ObjectDict()
            obj_info.byte_start = r.read_uint32()
            obj_info.byte_size = r.read_uint32()
            obj_info.type_id = r.read_int32()
            obj_info.class_id = r.read_int16()

            if obj_info.byte_start > self.header.file_size:
                raise SerializedFileError("Invalid byte start: %d" % obj_info.byte_start)

            if obj_info.byte_size > self.header.file_size:
                raise SerializedFileError("Invalid byte size: %d" % obj_info.byte_start)

            if self.header.version > 13:
                obj_info.script_type_index = r.read_int16()
            else:
                obj_info.is_destroyed = r.read_bool16()

            if self.header.version > 14:
                obj_info.stripped = r.read_bool8()

            if path_id in object_infos:
                raise SerializedFileError("Duplicate path ID: %d" % path_id)

            object_infos[path_id] = obj_info

    def _read_script_types(self, r):
        script_types = self.script_types = []

        # script types exist in newer versions only
        if self.header.version < 11:
            return

        num_entries = r.read_int32()

        for _ in range(num_entries):
            r.align(4)

            script_type = utils.ObjectDict()
            script_type.serialized_file_index = r.read_int32()
            script_type.identifier_in_file = r.read_int64()

            script_types.append(script_type)

    def _read_externals(self, r):
        externals = self.externals = []

        num_entries = r.read_int32()
        for _ in range(num_entries):
            external = utils.ObjectDict()

            if self.header.version > 5:
                external.asset_path = r.read_cstring()

            external.guid = uuid.UUID(bytes=r.read(16))
            external.type = r.read_int32()
            external.file_path = r.read_cstring()

            externals.append(external)

    def _read_objects(self, r):
        type_db = rtti.Database()
        objects = self.objects = {}

        for path_id, obj_info in self.object_infos.items():
            # get object type class
            obj_type = None
            obj_class = self.types.classes.get(obj_info.type_id)

            if obj_class and "type_tree" in obj_class:
                obj_type = obj_class.type_tree
            elif obj_info.type_id <= 0:
                # script types are never stored in database, so don't even try
                continue
            elif obj_info.type_id in self._cached_types:
                obj_type = self._cached_types[obj_info.type_id]
            else:
                # use embedded object type tree or load it from database otherwise
                if self.header.version > 13:
                    # object_class should always be defined in newer formats
                    assert obj_class

                    try:
                        with type_db.open(obj_info.type_id,
                                          obj_class.old_type_hash) as fp:
                            obj_type = rtti.read_type_node(fp)
                    except rtti.TypeException as ex:
                        log.warning(ex)
                else:
                    try:
                        with type_db.open_old(obj_info.type_id,
                                              self.types.signature) as fp:
                            obj_type = rtti.read_type_node_old(fp)
                    except rtti.TypeException as ex:
                        log.warning(ex)

                self._cached_types[obj_info.type_id] = obj_type

            if not obj_type:
                continue

            objects[path_id] = SerializedObject(self, path_id, obj_info, obj_type)

    def objects_by_class(self, *class_id):
        for obj in self.objects.values():
            if obj.info.class_id in class_id:
                yield obj

    def close(self):
        self.r.close()

class SerializedFileError(Exception):
    pass

class SerializedObject():

    _read_prim = {
        "bool":             ioutils.BinaryIO.read_bool8,
        "SInt8":            ioutils.BinaryIO.read_int8,
        "UInt8":            ioutils.BinaryIO.read_uint8,
        "char":             ioutils.BinaryIO.read_uint8,
        "SInt16":           ioutils.BinaryIO.read_int16,
        "short":            ioutils.BinaryIO.read_int16,
        "UInt16":           ioutils.BinaryIO.read_uint16,
        "unsigned short":   ioutils.BinaryIO.read_uint16,
        "SInt32":           ioutils.BinaryIO.read_int32,
        "int":              ioutils.BinaryIO.read_int32,
        "UInt32":           ioutils.BinaryIO.read_uint32,
        "unsigned int":     ioutils.BinaryIO.read_uint32,
        "SInt64":           ioutils.BinaryIO.read_int64,
        "long":             ioutils.BinaryIO.read_int64,
        "UInt64":           ioutils.BinaryIO.read_uint64,
        "unsigned long":    ioutils.BinaryIO.read_uint64,
        "float":            ioutils.BinaryIO.read_float,
        "double":           ioutils.BinaryIO.read_double,
    }

    _cached_classes = {}

    def __init__(self, file, path_id, info, type):
        self._instance = None
        self._file = file

        self.path_id = path_id
        self.info = info
        self.type = type

        self._start = self._file.header.data_offset + self.info.byte_start
        self._end = self._start + self.info.byte_size

    def _deserialize(self, r, obj_type):
        if log.isEnabledFor(logging.DEBUG):
            log.debug("%d %s %s", r.tell(), obj_type.type, obj_type.name)

        if obj_type.is_array:
            # unpack "Array" objects to native Python arrays
            type_size = obj_type.children[0]
            type_data = obj_type.children[1]

            size = self._deserialize(r, type_size)
            if type_data.type in ("SInt8", "UInt8", "char"):
                # fix size for AudioClips that are linked with .resS files
                if self._file.header.version <= 13:
                    size = min(size, self._end - r.tell())

                # read byte array
                obj = r.read(size)
            else:
                # read generic array
                obj = []
                for _ in range(size):
                    obj.append(self._deserialize(r, type_data))

            # arrays always need to be aligned in version 5 or newer
            if self._file.header.version > 5:
                r.align(4)
        elif obj_type.size > 0 and not obj_type.children:
            # no children and size greater zero -> primitive
            if obj_type.type not in self._read_prim:
                raise SerializedObjectError("Unknown primitive type: " + obj_type.type)

            obj = self._read_prim[obj_type.type](r)

            # align if flagged
            if obj_type.meta_flag & 0x4000 != 0:
                r.align(4)
        else:
            # complex object with children
            obj_class = self._cached_classes.get(obj_type.type)
            if not obj_class:
                obj_class = type(obj_type.type, (engine.Object,), {})
                self._cached_classes[obj_type.type] = obj_class

            obj = obj_class()

            for child in obj_type.children:
                obj[child.name] = self._deserialize(r, child)

        if obj_type.type == "string":
            # convert string objects to native Python strings
            try:
                obj = obj.Array.decode("utf-8")
            except UnicodeDecodeError:
                # could be a TextAsset that contains binary data, return raw string
                log.warning("Can't decode string at %d as UTF-8, "
                            "using raw data instead", r.tell())
                obj = obj.Array
        elif obj_type.type == "vector":
            # unpack collection containers
            obj = obj.Array

        return obj

    @property
    def instance(self):
        if self._instance:
            return self._instance

        r = self._file.r
        r.seek(self._start, io.SEEK_SET)

        self._instance = self._deserialize(r, self.type)

        obj_size = r.tell() - self._start
        if obj_size != self.info.byte_size:
            raise SerializedObjectError("Wrong object size for path %d: %d != %d"
                                        % (self.path_id, obj_size, obj_info.byte_size))

        return self._instance

class SerializedObjectError(Exception):
    pass
