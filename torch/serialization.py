import os
import sys
import tempfile
import tarfile
import pickle
import shutil
import struct
from contextlib import closing, contextmanager
if sys.version_info[0] == 2:
    import cPickle as pickle
else:
    import pickle

import torch

DEFAULT_PROTOCOL = 2

LONG_SIZE = struct.Struct('=l').size
INT_SIZE = struct.Struct('=i').size
SHORT_SIZE = struct.Struct('=h').size

def _add_to_tar(fn, tar_file, name):
    tmp_file = tempfile.NamedTemporaryFile(delete=False)
    fn(tmp_file)
    tmp_file.close()

    tar_file.add(tmp_file.name, arcname=name)
    if os.path.isfile(tmp_file.name):
        os.remove(tmp_file.name)


@contextmanager
def mkdtemp():
    path = tempfile.mkdtemp()
    yield path
    shutil.rmtree(path)


# TODO: choose pickle protocol
def save(obj, f, pickle_module=pickle, pickle_protocol=DEFAULT_PROTOCOL):
    serialized_tensors = {}
    serialized_storages = {}

    def persistent_id(obj):
        if torch.is_tensor(obj):
            serialized_tensors[obj._cdata] = obj
            return str(obj._cdata)
        elif torch.is_storage(obj):
            serialized_storages[obj._cdata] = obj
            return str(obj._cdata)
        return None

    def save_tensors(f):
        pickle_module.dump(len(serialized_tensors), f, protocol=pickle_protocol)
        for key, tensor in serialized_tensors.items():
            storage = tensor.storage()
            if storage is not None:
                storage_id = storage._cdata
                serialized_storages[storage_id] = storage
            else:
                storage_id = None

            pickle_module.dump((key, type(tensor), storage_id), f, protocol=pickle_protocol)
            f.flush()
            tensor._write_metadata(f)

    def save_storages(f):
        storage_views = []
        storage_views_roots = {}

        for key, storage in serialized_storages.items():
            root, offset = storage._root_storage()
            if root is not storage:
                storage_views_roots[root._cdata] = root
                storage_views.append((storage._cdata, root._cdata, offset,
                    storage.size()))
        for view_info in storage_views:
            del serialized_storages[view_info[0]]
        serialized_storages.update(storage_views_roots)

        pickle_module.dump(len(serialized_storages), f, protocol=pickle_protocol)
        for key, storage in serialized_storages.items():
            pickle_module.dump((key, type(storage)), f, protocol=pickle_protocol)
            f.flush()
            storage._write_file(f)

        pickle_module.dump(storage_views, f, protocol=pickle_protocol)

    def pickle_objects(f):
        pickler = pickle_module.Pickler(f, protocol=pickle_protocol)
        pickler.persistent_id = persistent_id
        pickler.dump(obj)

    def save_sys_info(f):
        sys_info = dict(
            protocol_version=1000,
            little_endian=sys.byteorder == 'little',
            type_sizes = dict(
                short=SHORT_SIZE,
                int=INT_SIZE,
                long=LONG_SIZE,
            ),
        )
        pickle_module.dump(sys_info, f, protocol=pickle_protocol)

    with closing(tarfile.open(fileobj=f, mode='w:', format=tarfile.PAX_FORMAT)) as tar:
        _add_to_tar(save_sys_info, tar, 'sys_info')
        _add_to_tar(pickle_objects, tar, 'pickle')
        _add_to_tar(save_tensors, tar, 'tensors')
        _add_to_tar(save_storages, tar, 'storages')


def load(f, pickle_module=pickle):
    deserialized_objects = {}

    def persistent_load(saved_id):
        return deserialized_objects[int(saved_id)]

    with closing(tarfile.open(fileobj=f, mode='r:', format=tarfile.PAX_FORMAT)) as tar, \
         mkdtemp() as tmpdir:

        def extract(f, init):
            num_storages = pickle_module.load(f)
            for i in range(num_storages):
                args = pickle_module.load(f)
                key, args = args[0], args[1:]
                obj = init(*args)
                deserialized_objects[key] = obj

        tar.extract('storages', path=tmpdir)
        with open(os.path.join(tmpdir, 'storages'), 'rb', 0) as f:
            extract(f, lambda storage_type: storage_type._new_with_file(f))
            storage_views = pickle_module.load(f)
            for target_cdata, root_cdata, offset, size in storage_views:
                root = deserialized_objects[root_cdata]
                deserialized_objects[target_cdata] = root[offset:offset+size]

        tar.extract('tensors', path=tmpdir)
        with open(os.path.join(tmpdir, 'tensors'), 'rb', 0) as f:
            def deserialize_tensor(tensor_type, storage_id):
                storage = deserialized_objects.get(storage_id, None)
                return tensor_type._new_with_metadata_file(f, storage)
            extract(f, deserialize_tensor)

        pickle_file = tar.extractfile('pickle')
        unpickler = pickle_module.Unpickler(pickle_file)
        unpickler.persistent_load = persistent_load
        result = unpickler.load()
        return result

