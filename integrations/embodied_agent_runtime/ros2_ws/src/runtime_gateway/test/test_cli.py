import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time

from ament_index_python.packages import get_package_prefix


PREFIX = Path(get_package_prefix('runtime_gateway'))
CLIENT = PREFIX / 'lib/runtime_gateway/runtime_gateway_client'
NODE = PREFIX / 'lib/runtime_gateway/runtime_gateway_node'


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def wait_for_listener(port, expected, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as client:
            client.settimeout(0.05)
            listening = client.connect_ex(('127.0.0.1', port)) == 0
        if listening == expected:
            return True
        time.sleep(0.02)
    return False


def parse_one_line(completed):
    lines = completed.stdout.splitlines()
    assert len(lines) == 1, completed.stdout
    return json.loads(lines[0])


def test_parameter_error_is_json_and_exit_two():
    completed = subprocess.run([str(CLIENT), 'submit'], capture_output=True, text=True)
    assert completed.returncode == 2
    assert parse_one_line(completed)['ok'] is False


def test_unreachable_gateway_returns_within_client_deadline():
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    port = listener.getsockname()[1]
    stop = threading.Event()

    def accept_without_grpc():
        connection, _ = listener.accept()
        with connection:
            stop.wait(5)

    thread = threading.Thread(target=accept_without_grpc)
    thread.start()
    environment = os.environ.copy()
    environment['RUNTIME_GATEWAY_ADDRESS'] = f'127.0.0.1:{port}'
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [str(CLIENT), 'get-stats'],
            env=environment,
            capture_output=True,
            text=True,
            timeout=3,
        )
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=1)
    assert time.monotonic() - started < 2.8
    assert completed.returncode == 1
    assert parse_one_line(completed)['ok'] is False


def test_success_rejection_and_escaping_are_single_json_lines(tmp_path):
    port = free_port()
    database = tmp_path / 'cli.sqlite3'
    process = subprocess.Popen(
        [
            str(NODE),
            '--ros-args',
            '-p',
            f'port:={port}',
            '-p',
            f'database_path:={database}',
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert wait_for_listener(port, True)
    environment = os.environ.copy()
    environment['RUNTIME_GATEWAY_ADDRESS'] = f'127.0.0.1:{port}'
    try:
        stats = subprocess.run(
            [str(CLIENT), 'get-stats'],
            env=environment,
            capture_output=True,
            text=True,
        )
        assert stats.returncode == 0
        assert parse_one_line(stats)['state'] == 'EMPTY'

        rejected = subprocess.run(
            [
                str(CLIENT),
                'submit',
                '--request-id',
                'reject-request',
                '--workflow',
                'uploaded_xml',
                '--task-id',
                'reject-task',
                '--target',
                'dock_a',
                '--timeout-ms',
                '3000',
            ],
            env=environment,
            capture_output=True,
            text=True,
        )
        assert rejected.returncode == 0
        assert parse_one_line(rejected)['state'] == 'REJECTED'

        task_id = 'quote-"-slash-\\-newline-\n'
        task = subprocess.run(
            [str(CLIENT), 'get-task', '--task-id', task_id],
            env=environment,
            capture_output=True,
            text=True,
        )
        assert task.returncode == 0
        assert parse_one_line(task)['task_id'] == task_id
    finally:
        process.send_signal(signal.SIGINT)
        process.wait(timeout=3)
    assert process.returncode == 0
    assert wait_for_listener(port, False)
