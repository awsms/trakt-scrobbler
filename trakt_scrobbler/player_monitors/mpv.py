import json
import os
import sys
import threading
import time
import appdirs
import confuse
import glob
from configparser import ConfigParser
from pathlib import Path
from queue import Queue
from trakt_scrobbler import logger
from trakt_scrobbler.player_monitors.monitor import Monitor
from trakt_scrobbler.utils import is_url

if os.name == 'posix':
    import select
    import socket
elif os.name == 'nt':
    import win32api
    import win32event
    import win32file
    import win32pipe
    from winerror import (ERROR_BROKEN_PIPE, ERROR_MORE_DATA, ERROR_IO_PENDING,
                          ERROR_PIPE_BUSY)


class MPVMon(Monitor):
    name = 'mpv'
    exclude_import = True
    WATCHED_PROPS = frozenset(('pause', 'path', 'working-directory',
                               'duration', 'time-pos'))
    CONFIG_TEMPLATE = {
        "ipc_path": confuse.String(default="auto-detect"),
        "poll_interval": confuse.Number(default=10),
        # seconds to wait while reading data from mpv
        "read_timeout": confuse.Number(default=2),
        # seconds to wait while writing data to mpv
        "write_timeout": confuse.Number(default=60),
        # seconds to wait after one file ends to check for the next play
        # needed to make sure we don't reconnect too soon after end-file
        "restart_delay": confuse.Number(default=0.1)
    }

    def __init__(self, scrobble_queue):
        super().__init__(scrobble_queue)
        self.ipc_path = self.config['ipc_path']
        self.resolved_ipc_path = None
        self.read_timeout = self.config['read_timeout']
        self.write_timeout = self.config['write_timeout']
        self.poll_interval = self.config['poll_interval']
        self.restart_delay = self.config['restart_delay']
        self.buffer: bytes = b''
        self.ipc_lock = threading.Lock()  # for IPC write queue
        self.poll_timer = None
        self.write_queue = Queue()
        self.sent_commands = {}
        self.command_counter = 1
        self.vars = {}
        self.was_connected = False
        self._rescan_requested = False
        self._last_rescan = 0.0

    def reset_ipc_state(self):
        # A reconnect should start with a clean request/response mapping.
        self.buffer = b''
        self.sent_commands = {}
        self.command_counter = 1
        while not self.write_queue.empty():
            self.write_queue.get_nowait()
            self.write_queue.task_done()

    @classmethod
    def read_player_cfg(cls, auto_keys=None):
        if sys.platform == "darwin":
            conf_path = Path.home() / ".config" / "mpv" / "mpv.conf"
        else:
            conf_path = (
                Path(appdirs.user_config_dir("mpv", roaming=True, appauthor=False))
                / "mpv.conf"
            )
        mpv_conf = ConfigParser(
            allow_no_value=True, strict=False, inline_comment_prefixes="#"
        )
        mpv_conf.optionxform = lambda option: option
        mpv_conf.read_string("[root]\n" + conf_path.read_text(encoding="utf-8"))
        return {
            "ipc_path": lambda: mpv_conf.get("root", "input-ipc-server")
        }

    def run(self):
        while True:
            # POSIX uses _get_connection() (connect = check), Windows keeps can_connect()
            if os.name == 'posix':
                sock = self._get_connection()
                if sock is None:
                    logger.info('Unable to connect to MPV. Check ipc path.')
                    self._sleep_before_retry()
                    continue

                self.was_connected = True
                self.reset_ipc_state()
                self.update_vars()
                self.conn_loop(sock)
            else:
                if self.can_connect():
                    self.was_connected = True
                    self.reset_ipc_state()
                    self.update_vars()
                    self.conn_loop()
                else:
                    logger.info('Unable to connect to MPV. Check ipc path.')
                    self._sleep_before_retry()
                    continue

            if self.vars.get('state', 0) != 0:
                # create a 'stop' event in case the player didn't send 'end-file'
                self.vars['state'] = 0
                self.update_status()
            self.vars = {}
            if self.poll_timer:
                self.poll_timer.cancel()
            time.sleep(self.restart_delay)

    def _sleep_before_retry(self):
        retry_delay = self.poll_interval
        if self.was_connected:
            retry_delay = min(self.poll_interval, 1.0)
        time.sleep(retry_delay)

    def update_status(self):
        if not self.WATCHED_PROPS.issubset(self.vars):
            logger.warning("Incomplete media status info")
            return
        fpath = self.vars['path']
        if not is_url(fpath) and not Path(fpath).is_absolute():
            fpath = str(Path(self.vars['working-directory']) / fpath)

        # Update last known position if player is stopped
        pos = self.vars['time-pos']
        if self.vars['state'] == 0 and self.status['state'] == 2:
            pos += round(time.time() - self.status['time'], 3)
        pos = min(pos, self.vars['duration'])

        self.status = {
            'state': self.vars['state'],
            'filepath': fpath,
            'position': pos,
            'duration': self.vars['duration'],
            'time': time.time()
        }
        if self.status['state'] != 2 and hasattr(self, "_has_multiple_ipc_candidates"):
            now = time.time()
            if self._has_multiple_ipc_candidates() and now - self._last_rescan >= self.poll_interval:
                self._rescan_requested = True
                self._last_rescan = now
        self.handle_status_update()

    def update_vars(self):
        """Query mpv for required properties."""
        self.updated_props_count = 0
        for prop in self.WATCHED_PROPS:
            self.send_command(['get_property', prop])
        if self.poll_timer:
            self.poll_timer.cancel()
        self.poll_timer = threading.Timer(self.poll_interval, self.update_vars)
        self.poll_timer.name = 'mpvpoll'
        self.poll_timer.start()

    def handle_event(self, event):
        if event == 'end-file':
            # Since the player might be shutting down, we can't update vars.
            # Reuse the previous self.vars and only update the state value.
            # This might be inaccurate in terms of position, which is why
            # regular polling is needed
            self.vars['state'] = 0
            self.update_status()
        elif event == 'pause':
            self.vars['state'] = 1
            self.update_vars()
        elif event == 'unpause' or event == 'playback-restart':
            self.vars['state'] = 2
            self.update_vars()

    def handle_cmd_response(self, resp):
        command = self.sent_commands.pop(resp['request_id'], None)
        if command is None:
            logger.debug(f"Ignoring response for unknown request_id={resp['request_id']}")
            return
        if resp['error'] != 'success':
            logger.error(f'Error with command {command!s}. Response: {resp!s}')
            return
        elif command[0] != 'get_property':
            return
        param = command[1]
        data = resp['data']
        if param == 'pause':
            self.vars['state'] = 1 if data else 2
        if param in self.WATCHED_PROPS:
            self.vars[param] = data
            self.updated_props_count += 1
        if self.updated_props_count == len(self.WATCHED_PROPS):
            self.update_status()
            # resetting self.vars to {} here is a bad idea.

    def on_data(self, data: bytes):
        self.buffer += data
        partial_line = b""
        for line in self.buffer.splitlines(keepends=True):
            if line.endswith(b"\n"):
                # no need to strip, json.loads will handle it
                self.on_line(line)
            else:
                # partial line received
                # self.on_line() is called in next data batch
                partial_line = line
        self.buffer = partial_line


    def on_line(self, line: bytes):
        try:
            line_str = line.decode(encoding='utf-8', errors='ignore')
            mpv_json = json.loads(line_str)
        except json.JSONDecodeError:
            logger.warning('Invalid JSON received. Skipping.', exc_info=True)
            logger.debug(line)
            return
        if 'event' in mpv_json:
            self.handle_event(mpv_json['event'])
        elif 'request_id' in mpv_json:
            self.handle_cmd_response(mpv_json)

    def send_command(self, elements):
        with self.ipc_lock:
            command = {'command': elements, 'request_id': self.command_counter}
            self.sent_commands[self.command_counter] = elements
            self.command_counter += 1
            self.write_queue.put(str.encode(json.dumps(command) + '\n'))


class MPVPosixMon(MPVMon):
    exclude_import = os.name != 'posix'

    def __init__(self, scrobble_queue):
        super().__init__(scrobble_queue)

    def _get_connection(self):
        """
        Return an already-connected AF_UNIX socket, or None.

        This avoids TOCTOU by not doing "can_connect()" and later a separate connect().
        Wildcards are supported by expanding ipc_path with glob.
        """
        candidates = self._list_ipc_candidates()
        fallback_sock = None
        fallback_path = None
        fallback_vars = None

        for path in candidates:
            sock = socket.socket(socket.AF_UNIX)
            try:
                sock.connect(path)
            except (FileNotFoundError, ConnectionRefusedError, OSError):
                sock.close()
                continue

            if len(candidates) > 1:
                is_playing, vars_ = self._probe_socket(sock)
                if is_playing:
                    self.resolved_ipc_path = path
                    if vars_:
                        self.vars = vars_
                    return sock
                if fallback_sock is None:
                    fallback_sock = sock
                    fallback_path = path
                    fallback_vars = vars_
                else:
                    sock.close()
            else:
                self.resolved_ipc_path = path
                return sock

        if fallback_sock is not None:
            self.resolved_ipc_path = fallback_path
            if fallback_vars:
                self.vars = fallback_vars
            return fallback_sock

        self.resolved_ipc_path = None
        return None

    def _list_ipc_candidates(self, include_resolved=True):
        ipc_path = self.ipc_path
        if os.path.isdir(ipc_path):
            ipc_path = os.path.join(ipc_path, "*")
        candidates = []
        if include_resolved and self.resolved_ipc_path:
            candidates.append(self.resolved_ipc_path)
        if "*" in ipc_path:
            globbed = glob.glob(ipc_path)
            try:
                globbed.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            except FileNotFoundError:
                # Socket removed between glob and stat; leave unsorted.
                pass
            for path in globbed:
                if path not in candidates:
                    candidates.append(path)
        else:
            if ipc_path not in candidates:
                candidates.append(ipc_path)
        return candidates

    def _has_multiple_ipc_candidates(self):
        candidates = self._list_ipc_candidates(include_resolved=False)
        return len(candidates) > 1

    def _probe_socket(self, sock):
        """Return (is_playing, vars) for a connected socket."""
        buffer = b""
        sent_commands = {}
        command_counter = 1
        updated_props_count = 0
        vars_ = {}

        for prop in self.WATCHED_PROPS:
            command = {'command': ['get_property', prop], 'request_id': command_counter}
            sent_commands[command_counter] = ['get_property', prop]
            command_counter += 1
            sock.sendall(str.encode(json.dumps(command) + '\n'))

        deadline = time.time() + self.read_timeout
        while time.time() < deadline and updated_props_count < len(self.WATCHED_PROPS):
            timeout = max(0.0, deadline - time.time())
            r, _, _ = select.select([sock], [], [], timeout)
            if not r:
                break
            data = sock.recv(4096)
            if not data:
                break
            buffer += data
            partial_line = b""
            for line in buffer.splitlines(keepends=True):
                if not line.endswith(b"\n"):
                    partial_line = line
                    continue
                try:
                    resp = json.loads(line.decode(encoding='utf-8', errors='ignore'))
                except json.JSONDecodeError:
                    continue
                if 'request_id' not in resp:
                    continue
                command = sent_commands.get(resp['request_id'])
                if not command or resp.get('error') != 'success':
                    continue
                if command[0] != 'get_property':
                    continue
                param = command[1]
                data_val = resp.get('data')
                if param == 'pause':
                    vars_['state'] = 1 if data_val else 2
                if param in self.WATCHED_PROPS:
                    vars_[param] = data_val
                    updated_props_count += 1
            buffer = partial_line

        is_playing = vars_.get('state') == 2 and vars_.get('duration')
        return is_playing, vars_

    # Kept for API compatibility; run() no longer uses it on POSIX.
    def can_connect(self):
        sock = self._get_connection()
        if sock is None:
            return False
        sock.close()
        return True

    def conn_loop(self, sock):
        self.is_running = True
        sock_list = [sock]
        while self.is_running:
            r, _, _ = select.select(sock_list, [], [], self.read_timeout)
            if r:  # r == [sock]
                # socket has data to be read
                try:
                    data = sock.recv(4096)
                except ConnectionResetError:
                    self.is_running = False
                    break
                if len(data) == 0:
                    # EOF reached
                    self.is_running = False
                    break
                self.on_data(data)
            while not self.write_queue.empty():
                # block until sock can be written to
                _, w, _ = select.select([], sock_list, [], self.write_timeout)
                if not w:
                    logger.warning("Timed out writing to socket. Killing connection.")
                    self.is_running = False
                    break
                try:
                    sock.sendall(self.write_queue.get_nowait())
                except BrokenPipeError:
                    self.is_running = False
                    break
                else:
                    self.write_queue.task_done()
            if self._rescan_requested:
                self._rescan_requested = False
                self.is_running = False
                break
        sock.close()
        # Clear resolved path so reconnect can find a new IPC after mpv restarts.
        self.resolved_ipc_path = None
        while not self.write_queue.empty():
            self.write_queue.get_nowait()
            self.write_queue.task_done()
        logger.debug('Sock closed')


class MPVWinMon(MPVMon):
    exclude_import = os.name != 'nt'

    def __init__(self, scrobble_queue):
        super().__init__(scrobble_queue)
        self.file_handle = None
        self.is_running = False
        self._read_buf = win32file.AllocateReadBuffer(1024)
        self._transact_buf = win32file.AllocateReadBuffer(512)
        self._read_all_buf = win32file.AllocateReadBuffer(512)

    def can_connect(self):
        return win32file.GetFileAttributes(self.ipc_path) == win32file.FILE_ATTRIBUTE_NORMAL

    def _transact(self, write_data):
        """Wrapper over TransactNamedPipe"""
        read_buf = self._transact_buf
        err, data = win32pipe.TransactNamedPipe(self.file_handle, write_data, read_buf)
        while err == ERROR_MORE_DATA:
            err, d = win32file.ReadFile(self.file_handle, read_buf)
            data += d
        return data

    def _read_all_data(self):
        """Read all the remaining data on the pipe"""
        data = b""
        read_buf = self._read_all_buf
        while win32file.GetFileSize(self.file_handle):
            _, d = win32file.ReadFile(self.file_handle, read_buf)
            data += d
        return data

    def _call(self, method, *args, max_retries=5):
        """Call a pipe API method and retry if necessary"""
        try:
            return method(*args)
        except win32file.error as e:
            if e.args[0] == ERROR_BROKEN_PIPE:
                self.is_running = False
            elif e.args[0] == ERROR_PIPE_BUSY and max_retries != 0:
                # something in the pipe, read the data and retry
                data = self._call(self._read_all_data)
                if not self.is_running:
                    return
                if data:
                    self.on_data(data)
                return self._call(method, *args, max_retries=max_retries - 1)
            else:
                raise

    def conn_loop(self):
        # we could be using the same IPC pipe as some other tool
        # (very likely, some mpv wrapper like syncplay.) If it has already
        # connected to the pipe and written to it, we will get an error
        # if we try to connect now. So handle it gracefully and retry.
        for _ in range(5):
            try:
                self.file_handle = win32file.CreateFile(
                    self.ipc_path,
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0,
                    None,
                    win32file.OPEN_EXISTING,
                    win32file.FILE_FLAG_OVERLAPPED,
                    None
                )
            except win32file.error as e:
                if e.args[0] == ERROR_PIPE_BUSY:
                    # racing with someone else? retry.
                    continue
                else:
                    raise
            else:
                break
        else:
            logger.error(f"Failed to connect to pipe, is there a race?")
            return

        if self.file_handle == win32file.INVALID_HANDLE_VALUE:
            err = win32api.FormatMessage(win32api.GetLastError())
            logger.error(f"Failed to connect to pipe: {err}")
            self.file_handle = None
            return

        # needed for blocking on read
        overlapped = win32file.OVERLAPPED()
        overlapped.hEvent = win32event.CreateEvent(None, 0, 0, None)

        # needed for transactions
        win32pipe.SetNamedPipeHandleState(
            self.file_handle, win32pipe.PIPE_READMODE_MESSAGE, None, None)
        self.is_running = True
        while self.is_running:
            val = self._call(win32file.ReadFile, self.file_handle, self._read_buf, overlapped)
            if not self.is_running:
                break
            err, data = val
            if err != 0 and err != ERROR_IO_PENDING:
                logger.warning(f"Unexpected read result {err}. Quitting.")
                logger.debug(f"data={bytes(data)}")
                self.is_running = False
                break
            if err == ERROR_IO_PENDING:
                err = win32event.WaitForSingleObject(
                    overlapped.hEvent, self.read_timeout)

            if err == win32event.WAIT_OBJECT_0:  # data is available
                data = bytes(data)
                line = data[:data.find(b"\n")]
                self.on_line(line)

            while not self.write_queue.empty():
                # first see if mpv sent some data that needs to be read
                data = self._call(self._read_all_data)
                if not self.is_running:
                    break
                if data:
                    self.on_data(data)
                # cancel all remaining reads/writes. Should be benign
                win32file.CancelIo(self.file_handle)

                write_data = self.write_queue.get_nowait()
                data = self._call(self._transact, write_data)
                if not self.is_running:
                    break
                self.on_line(data[:-1])

        self.is_running = False
        self.file_handle.close()
        self.file_handle = None
        logger.debug('Pipe closed.')
