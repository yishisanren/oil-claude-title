"""跨平台内核文件锁；进程退出后自动释放，不依赖锁文件是否存在。"""
import errno
import sys


def acquire(stream):
    if sys.platform == 'win32':
        import msvcrt
        stream.seek(0)
        try:
            # Windows 允许锁定 EOF 之外的字节，无需向锁文件写入占位内容。
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise BlockingIOError(exc.errno, '话题正在命名') from exc
            raise
    else:
        import fcntl
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def release(stream):
    if sys.platform == 'win32':
        import msvcrt
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream, fcntl.LOCK_UN)
