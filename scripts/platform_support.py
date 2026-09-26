"""Small OS boundary: locks, shell-free launch, bounded pipe reads and cleanup."""
import errno
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time

WINDOWS = os.name == 'nt'
if WINDOWS:
    import msvcrt
else:
    import fcntl


def lock_file(handle):
    """Acquire immediately or raise BlockingIOError; never truncate/unlink locks."""
    if not WINDOWS:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    handle.seek(0)
    try:
        # Windows permits locking beyond EOF, so no racy initialization write.
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            raise BlockingIOError(error.errno, 'Worker lock is already held') from error
        raise


def unlock_file(handle):
    if WINDOWS:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle, fcntl.LOCK_UN)


def opencode_command(args, env=None):
    """Resolve native executables or the npm Node entrypoint, never a shell.

    OPENCODE_WORKER_BIN is one executable path, not a command string. Explicit
    .py wrappers use this interpreter (also useful for provider-free fixtures).
    Unknown batch/PowerShell wrappers fail closed instead of evaluating args.
    """
    env = os.environ if env is None else env
    override = env.get('OPENCODE_WORKER_BIN')
    if not WINDOWS and not override:
        return ['opencode', *args]
    target = os.path.expanduser(override) if override else 'opencode'
    executable = shutil.which(target, path=env.get('PATH', os.defpath))
    if override and Path(target).is_file():
        executable = str(Path(target).resolve())
    if not executable:
        raise FileNotFoundError(errno.ENOENT,
            'OpenCode CLI not found; install it on this host or set OPENCODE_WORKER_BIN')
    suffix = Path(executable).suffix.lower()
    if override and suffix == '.py':
        return [sys.executable, str(Path(executable).resolve()), *args]
    if not WINDOWS or suffix in ('.exe', '.com'):
        return [executable, *args]
    # Global npm and project-local node_modules/.bin installations. Invoke the
    # installed JS launcher so its own architecture/OPENCODE_BIN_PATH logic stays
    # authoritative. Do not choose an arbitrary platform binary ourselves.
    parent = Path(executable).resolve().parent
    candidates = [parent / 'node_modules/opencode-ai/bin/opencode']
    if parent.name == '.bin':
        candidates.append(parent.parent / 'opencode-ai/bin/opencode')
    launcher = next((p for p in candidates if p.is_file()), None)
    if launcher is not None:
        node = shutil.which('node.exe', path=env.get('PATH', os.defpath))
        if not node:
            raise FileNotFoundError(errno.ENOENT, 'The npm OpenCode launcher requires node.exe on PATH')
        return [node, str(launcher), *args]
    raise OSError(errno.ENOEXEC,
        'Unsupported OpenCode wrapper; set OPENCODE_WORKER_BIN to the native opencode.exe')


def grok_command(args, env=None):
    """Resolve one Grok Build executable without invoking a shell wrapper."""
    env = os.environ if env is None else env
    override = env.get('GROK_WORKER_BIN')
    target = os.path.expanduser(override) if override else 'grok'
    executable = shutil.which(target, path=env.get('PATH', os.defpath))
    if override and Path(target).is_file():
        executable = str(Path(target).resolve())
    if not executable:
        raise FileNotFoundError(errno.ENOENT,
            'Grok Build CLI not found; install it or set GROK_WORKER_BIN')
    suffix = Path(executable).suffix.lower()
    if override and suffix == '.py':
        return [sys.executable, str(Path(executable).resolve()), *args]
    if WINDOWS and suffix not in ('.exe', '.com'):
        raise OSError(errno.ENOEXEC,
            'Unsupported Grok wrapper; set GROK_WORKER_BIN to the native grok.exe')
    return [executable, *args]


def process_options():
    return ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS
            else {'start_new_session': True})


def create_job():
    if WINDOWS:
        from windows_job import WindowsJob
        return WindowsJob()
    return None


class PipeReader:
    """Drain both raw pipes without select() (Windows only selects sockets).

    At most 16 x 64 KiB chunks are queued. Each reader owns and closes its pipe;
    the controller never closes a pipe while another thread holds a read lock.
    """
    CHUNK_SIZE = 65536
    QUEUE_SIZE = 16

    def __init__(self, stdout, stderr):
        self.items = queue.Queue(maxsize=self.QUEUE_SIZE)
        self.stopped = threading.Event()
        self.active = 2
        self.threads = []
        streams = [('stdout', stdout), ('stderr', stderr)]
        try:
            for name, stream in streams:
                thread = threading.Thread(target=self._pump, args=(name, stream),
                                          name='opencode-worker-' + name, daemon=True)
                thread.start()
                self.threads.append(thread)
        except BaseException:
            self.stopped.set()
            for _, stream in streams[len(self.threads):]:
                stream.close()
            raise

    def _put(self, item):
        while not self.stopped.is_set():
            try:
                self.items.put(item, timeout=0.1)
                return
            except queue.Full:
                pass

    def _pump(self, name, stream):
        try:
            while not self.stopped.is_set():
                chunk = stream.read(self.CHUNK_SIZE)
                self._put((name, chunk))
                if not chunk:
                    break
        except OSError as error:
            self._put((name, error))
        finally:
            stream.close()

    def read(self, timeout):
        try:
            name, chunk = self.items.get(timeout=timeout)
        except queue.Empty:
            return []
        if isinstance(chunk, OSError):
            raise chunk
        if not chunk:
            self.active -= 1
            return []
        return [(name, chunk)]

    def close(self):
        self.stopped.set()
        deadline = time.monotonic() + 0.5
        for thread in self.threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        # If a host refuses child termination, a daemon reader may remain blocked
        # until that child closes the pipe. Never block cleanup indefinitely.


def terminate_tree(process, job=None):
    """Best effort; preserve the original interruption and observed evidence."""
    if WINDOWS:
        if job is not None and job.assigned:
            try:
                job.terminate()
            except OSError:
                pass  # Closing the non-inherited job handle is the second path.
        else:
            # Assignment failure: do not continue work outside the intended job.
            # This fallback is scoped to the just-launched PID, not a name scan.
            system_root = os.environ.get('SystemRoot', r'C:\Windows')
            try:
                subprocess.run([str(Path(system_root) / 'System32/taskkill.exe'),
                                '/PID', str(process.pid), '/T', '/F'],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            pass
        # A reaped leader does not imply its descendants stopped.
        grace = time.monotonic() + 0.5
        while time.monotonic() < grace:
            process.poll()
            try:
                os.killpg(process.pid, 0)
            except OSError:
                break
            time.sleep(0.02)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        process.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass
