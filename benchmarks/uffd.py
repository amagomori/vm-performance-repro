import ctypes
import errno
import json
import mmap
import os
import socket
import threading

from benchmarks.firecracker import Stopwatch

UFFDIO_COPY = 0xC028AA03
UFFDIO_UNREGISTER = 0x8010AA01
libc = ctypes.CDLL(None, use_errno=True)
libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]


class Copy(ctypes.Structure):
    _fields_ = [("dst", ctypes.c_uint64), ("src", ctypes.c_uint64), ("len", ctypes.c_uint64),
                ("mode", ctypes.c_uint64), ("copy", ctypes.c_int64)]


class Range(ctypes.Structure):
    _fields_ = [("start", ctypes.c_uint64), ("len", ctypes.c_uint64)]


def data_extents(path, page_size):
    extents = []
    with open(path, "rb") as f:
        fd, end, pos = f.fileno(), os.fstat(f.fileno()).st_size, 0
        while pos < end:
            try:
                start = os.lseek(fd, pos, os.SEEK_DATA)
            except OSError:
                break
            stop = os.lseek(fd, start, os.SEEK_HOLE)
            start, stop = start // page_size * page_size, min(end, -(-stop // page_size) * page_size)
            if extents and start <= extents[-1][1]:
                extents[-1][1] = max(extents[-1][1], stop)
            else:
                extents.append([start, stop])
            pos = stop
    return extents


class Source:
    def __init__(self, path):
        self.file = open(path, "rb")
        self.map = mmap.mmap(self.file.fileno(), 0, access=mmap.ACCESS_COPY)
        self.view = ctypes.c_char.from_buffer(self.map)
        self.addr = ctypes.addressof(self.view)
        self.extents = {}

    def extents_for(self, page_size):
        if page_size not in self.extents:
            self.extents[page_size] = data_extents(self.file.name, page_size)
        return self.extents[page_size]

    def close(self):
        del self.view
        self.map.close()
        self.file.close()


def ioctl(fd, request, arg):
    return ctypes.get_errno() if libc.ioctl(fd, request, ctypes.byref(arg)) != 0 else 0


def copy(uffd, dst, src, length, page_size):
    done = 0
    while done < length:
        arg = Copy(dst + done, src + done, length - done, 0, 0)
        err = ioctl(uffd, UFFDIO_COPY, arg)
        if err == 0:
            return
        if err == errno.EEXIST:
            done += arg.copy if arg.copy > 0 else page_size
        elif err != errno.EAGAIN:
            raise OSError(err, os.strerror(err))
        else:
            done += max(arg.copy, 0)


class Prefetcher(threading.Thread):
    def __init__(self, sock_path, source):
        super().__init__(daemon=True)
        self.source = source
        self.copied = 0
        self.seconds = 0.0
        self.error = None
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(sock_path))
        self.listener.listen(1)
        self.listener.settimeout(60)

    def run(self):
        try:
            conn, _ = self.listener.accept()
            with conn:
                msg, fds, _, _ = socket.recv_fds(conn, 4096, 1)
                uffd = fds[0]
                with Stopwatch() as watch:
                    for region in json.loads(msg):
                        self.fill(uffd, region)
                os.close(uffd)
                self.seconds = watch.seconds
        except Exception as e:
            self.error = e
        finally:
            self.listener.close()

    def fill(self, uffd, region):
        page, base, offset, size = region["page_size"], region["base_host_virt_addr"], region["offset"], region["size"]
        for start, stop in self.source.extents_for(page):
            start, stop = max(start, offset), min(stop, offset + size)
            if start < stop:
                copy(uffd, base + start - offset, self.source.addr + start, stop - start, page)
                self.copied += stop - start
        err = ioctl(uffd, UFFDIO_UNREGISTER, Range(base, size))
        if err:
            raise OSError(err, os.strerror(err))
