#!/usr/bin/env python

# Copyright (C) 2015, Wazuh Inc.
# Created by Wazuh, Inc. <info@wazuh.com>.
# This program is free software; you can redistribute it and/or modify it under the terms of GPLv2

import argparse
import asyncio
import logging
import os
import signal
import subprocess
import sys
from typing import List
import time

from wazuh.core import pyDaemonModule
from wazuh.core.authentication import generate_keypair, keypair_exists
from wazuh.core.common import WAZUH_SHARE, wazuh_gid, wazuh_uid, CONFIG_SERVER_SOCKET_PATH, WAZUH_RUN
from wazuh.core.config.client import CentralizedConfig
from wazuh.core.config.models.server import NodeType
from wazuh.core.cluster.cluster import clean_up
from wazuh.core.cluster.utils import ClusterLogger, context_tag, process_spawn_sleep, print_version, ping_unix_socket
from wazuh.core.exception import WazuhDaemonError
from wazuh.core.utils import clean_pid_files, create_wazuh_dir
from wazuh.core.wlogging import WazuhLogger
from wazuh.core.cluster.unix_server.server import start_unix_server
from wazuh.core.config.models.server import ServerConfig
from wazuh.core.task.order import get_orders


SERVER_DAEMON_NAME = 'wazuh-server'
COMMS_API_SCRIPT_PATH = WAZUH_SHARE / 'apis' / 'scripts' / 'wazuh_comms_apid.py'
COMMS_API_DAEMON_NAME = 'wazuh-comms-apid'
EMBEDDED_PYTHON_PATH = WAZUH_SHARE / 'framework' / 'python' / 'bin' / 'python3'
ENGINE_BINARY_PATH = WAZUH_SHARE / 'bin' / 'wazuh-engine'
ENGINE_DAEMON_NAME = 'wazuh-engined'
MANAGEMENT_API_SCRIPT_PATH = WAZUH_SHARE / 'api' / 'scripts' / 'wazuh_apid.py'
MANAGEMENT_API_DAEMON_NAME = 'wazuh-apid'


#
# Aux functions
#


def set_logging(debug_mode=0) -> WazuhLogger:
    """Set cluster logger.

    Parameters
    ----------
    debug_mode : int
        Debug mode.

    Returns
    -------
    WazuhLogger
        Cluster logger.
    """
    cluster_logger = ClusterLogger(
        debug_level=debug_mode,
        tag='%(asctime)s %(levelname)s: [%(tag)s] [%(subtag)s] %(message)s',
    )
    cluster_logger.setup_logger()
    return cluster_logger


def start_daemon(name: str, args: List[str]):
    """Start a daemon in a subprocess and validate that there were no errors during its execution.

    Parameters
    ----------
    name : str
        Daemon name.
    args : list
        Start command arguments.
    """
    main_logger.info(f'Starting {name}')

    try:
        p = subprocess.Popen(args)
        pid = p.pid
        # Wait two seconds to catch any failures during the execution. If the timeout is reached we consider
        # it successful
        returncode = p.wait(timeout=2)
        if returncode != 0:
            raise Exception(f'return code {returncode}')
    except subprocess.TimeoutExpired:
        # The command was executed without errors
        if name == ENGINE_DAEMON_NAME:
            pyDaemonModule.create_pid(ENGINE_DAEMON_NAME, pid)
        main_logger.info(f'Started {name} (pid: {pid})')
    except Exception as e:
        raise WazuhDaemonError(f'Error starting {name}: {e}')


def start_daemons(root: bool):
    """Start the engine and the management and communications APIs daemons in subprocesses.

    Parameters
    ----------
    root : bool
        Whether the script is running as root or not.
    """
    engine_log_level = {0: 'info', 1: 'debug', 2: 'trace'}

    daemons = {
        ENGINE_DAEMON_NAME: [ENGINE_BINARY_PATH, 'server', '-l', engine_log_level[debug_mode_], 'start'],
        COMMS_API_DAEMON_NAME: [EMBEDDED_PYTHON_PATH, COMMS_API_SCRIPT_PATH]
            + (['-r'] if root else []),
        MANAGEMENT_API_DAEMON_NAME: [EMBEDDED_PYTHON_PATH, MANAGEMENT_API_SCRIPT_PATH]
            + (['-r'] if root else []),
    }

    for name, args in daemons.items():
        start_daemon(name, args)


def shutdown_daemon(name: str):
    """Send a SIGTERM signal to the daemon process.

    Parameters
    ----------
    name : str
        Daemon name.
    """
    ppid = pyDaemonModule.get_parent_pid(name)
    if ppid is not None:
        main_logger.info(f'Shutting down {name} (pid: {ppid})')
        os.kill(ppid, signal.SIGTERM)

        if name == ENGINE_DAEMON_NAME:
            pyDaemonModule.delete_pid(name, ppid)


def shutdown_server(server_pid: int):
    """Terminate daemons and server parent and child processes.

    Parameters
    ----------
    server_pid : int
        Server process ID.
    """
    daemons = [ENGINE_DAEMON_NAME, MANAGEMENT_API_DAEMON_NAME, COMMS_API_DAEMON_NAME]
    for daemon in daemons:
        shutdown_daemon(daemon)

    main_logger.info('Waiting for daemons shutdown.')
    while pyDaemonModule.check_for_daemons_shutdown(daemons):
        time.sleep(1)

    # Terminate the cluster
    pyDaemonModule.delete_child_pids(SERVER_DAEMON_NAME, server_pid, main_logger)
    pyDaemonModule.delete_pid(SERVER_DAEMON_NAME, server_pid)


#
# Master main
#
async def master_main(args: argparse.Namespace, server_config: ServerConfig, logger: WazuhLogger):
    """Start the master node main process.

    Parameters
    ----------
    args : argparse.Namespace
        Script arguments.
    server_config : ServerConfig
        Server configuration.
    logger : WazuhLogger
        Cluster logger.
    """
    from wazuh.core.cluster import local_server, master

    tag = 'Master'
    context_tag.set(tag)
    start_unix_server(tag)

    while not ping_unix_socket(CONFIG_SERVER_SOCKET_PATH):
        main_logger.info(f"Configuration server is not available, retrying...")
        time.sleep(1)

    my_server = master.Master(
        performance_test=args.performance_test,
        concurrency_test=args.concurrency_test,
        logger=logger,
        server_config=server_config,
    )

    # Spawn pool processes
    if my_server.task_pool is not None:
        my_server.task_pool.map(process_spawn_sleep, range(my_server.task_pool._max_workers))

    my_local_server = local_server.LocalServerMaster(
        performance_test=args.performance_test,
        logger=logger,
        concurrency_test=args.concurrency_test,
        node=my_server,
        server_config=server_config,
    )

    # initialize server
    my_server_task = my_server.start()
    my_local_server_task = my_local_server.start()
    tasks = [my_server_task, my_local_server_task]

    # Initialize daemons
    start_daemons(args.root)

    await asyncio.gather(*tasks)


#
# Worker main
#
async def worker_main(args: argparse.Namespace, server_config: ServerConfig, logger: WazuhLogger):
    """Start main process of a worker node.

    Parameters
    ----------
    args : argparse.Namespace
        Script arguments.
    server_config : ServerConfig
        Server configuration.
    logger : WazuhLogger
        Cluster logger.
    """
    from concurrent.futures import ProcessPoolExecutor

    from wazuh.core.cluster import local_server, worker

    tag = 'Worker'
    context_tag.set(tag)
    start_unix_server(tag)

    while not ping_unix_socket(CONFIG_SERVER_SOCKET_PATH):
        main_logger.info(f"Configuration server is not available, retrying...")
        time.sleep(1)

    # Pool is defined here so the child process is not recreated when the connection with master node is broken.
    try:
        task_pool = ProcessPoolExecutor(max_workers=1)
    # Handle exception when the user running Wazuh cannot access /dev/shm
    except (FileNotFoundError, PermissionError):
        main_logger.warning(
            "In order to take advantage of Wazuh 4.3.0 cluster improvements, the directory '/dev/shm' must be "
            "accessible by the 'wazuh' user. Check that this file has permissions to be accessed by all users. "
            'Changing the file permissions to 777 will solve this issue.'
        )
        main_logger.warning(
            'The Wazuh cluster will be run without the improvements added in Wazuh 4.3.0 and higher versions.'
        )
        task_pool = None

    daemons_initialized = False

    while True:
        my_client = worker.Worker(
            performance_test=args.performance_test,
            concurrency_test=args.concurrency_test,
            file=args.send_file,
            string=args.send_string,
            logger=logger,
            server_config=server_config,
            task_pool=task_pool,
        )
        my_local_server = local_server.LocalServerWorker(
            performance_test=args.performance_test,
            logger=logger,
            concurrency_test=args.concurrency_test,
            node=my_client,
            server_config=server_config,
        )

        # Spawn pool processes
        if my_client.task_pool is not None:
            my_client.task_pool.map(process_spawn_sleep, range(my_client.task_pool._max_workers))
        try:
            my_client_task = my_client.start()
            my_local_server_task = my_local_server.start()
            tasks = [my_client_task, my_local_server_task]

            # Initialize the daemons one time
            if not daemons_initialized:
                # Initialize daemons
                start_daemons(args.root)
                daemons_initialized = True

            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logging.info("Connection with server has been lost. Reconnecting in 10 seconds.")
            await asyncio.sleep(server_config.worker.intervals.connection_retry)


def get_script_arguments() -> argparse.Namespace:
    """Get script arguments.

    Returns
    -------
    argparse.Namespace
        Arguments passed to the script.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--version', help='Print version', action='store_true', dest='version')

    subparsers = parser.add_subparsers(title='subcommands', help='Management operations')

    start_parser = subparsers.add_parser('start', help='Start Wazuh server')
    ####################################################################################################################
    # Dev options - Silenced in the help message.
    ####################################################################################################################
    # Performance test - value stored in args.performance_test will be used to send a request of that size in bytes to
    # all clients/to the server.
    start_parser.add_argument('--performance_test', type=int, dest='performance_test', help=argparse.SUPPRESS)
    # Concurrency test - value stored in args.concurrency_test will be used to send that number of requests in a row,
    # without sleeping.
    start_parser.add_argument('--concurrency_test', type=int, dest='concurrency_test', help=argparse.SUPPRESS)
    # Send string test - value stored in args.send_string variable will be used to send a string with that size in bytes
    # to the server. Only implemented in worker nodes.
    start_parser.add_argument('--string', help=argparse.SUPPRESS, type=int, dest='send_string')
    # Send file test - value stored in args.send_file variable is the path of a file to send to the server. Only
    # implemented in worker nodes.
    start_parser.add_argument('--file', help=argparse.SUPPRESS, type=str, dest='send_file')
    ####################################################################################################################
    start_parser.add_argument('-r', '--root', help='Run as root', action='store_true', dest='root')

    start_parser.set_defaults(func=start)

    stop_parser = subparsers.add_parser('stop', help='Stop Wazuh server')
    stop_parser.set_defaults(func=stop)

    status_parser = subparsers.add_parser('status', help='Show the Wazuh server status')
    status_parser.set_defaults(func=status)

    return parser


def start():
    """Start function of the wazuh-server script in charge of starting the server process."""
    try:
        server_pid = pyDaemonModule.get_wazuh_server_pid(SERVER_DAEMON_NAME)
        if server_pid:
            print(f'The server is already running on process {server_pid}')
            sys.exit(1)
    except StopIteration:
        pass

    try:
        server_config = CentralizedConfig.get_server_config()
    except Exception as e:
        main_logger.error(e)
        sys.exit(1)

    # Clean cluster files from previous executions
    clean_up()

    # Create /run/wazuh-server
    create_wazuh_dir(WAZUH_RUN)

    # Check for unused PID files
    clean_pid_files(SERVER_DAEMON_NAME)

    # Drop privileges to wazuh
    if not args.root:
        os.setgid(wazuh_gid())
        os.setuid(wazuh_uid())

    server_pid = os.getpid()
    pyDaemonModule.create_pid(SERVER_DAEMON_NAME, server_pid)
    main_logger.info(f'Starting server (pid: {server_pid})')

    if server_config.node.type == NodeType.MASTER:
        main_function = master_main

        # Generate JWT signing key pair if it doesn't exist
        if not keypair_exists():
            main_logger.info('Generating JWT signing key pair')
            generate_keypair()
    else:
        main_function = worker_main

    # Create a strong reference to prevent the tasks from being garbage collected.
    background_tasks = set()
    try:
        loop = asyncio.new_event_loop()
        background_tasks.add(loop.create_task(get_orders(main_logger)))
        loop.run_until_complete(main_function(args, server_config, main_logger))
    except KeyboardInterrupt:
        main_logger.info('SIGINT received. Shutting down...')
    except MemoryError:
        main_logger.error("Directory '/tmp' needs read, write & execution " "permission for 'wazuh' user")
    except WazuhDaemonError as e:
        main_logger.error(e)
    except Exception as e:
        main_logger.error(f'Unhandled exception: {e}')
    finally:
        shutdown_server(server_pid)


def stop():
    """Stop the Wazuh server running in background."""
    try:
        server_pid = pyDaemonModule.get_wazuh_server_pid(SERVER_DAEMON_NAME)
    except StopIteration:
        main_logger.warning('Wazuh server is not running.')
        sys.exit(0)

    shutdown_server(server_pid)
    os.kill(server_pid, signal.SIGTERM)


def status():
    """Show the status of the Wazuh server."""
    daemons = [SERVER_DAEMON_NAME, COMMS_API_DAEMON_NAME, MANAGEMENT_API_DAEMON_NAME, ENGINE_DAEMON_NAME]
    running_processes = pyDaemonModule.get_running_processes()

    for daemon in daemons:
        status = 'running'
        if daemon not in running_processes:
            status = 'not running'
        print(f'{daemon} is {status}...')


if __name__ == '__main__':
    from wazuh.core import pyDaemonModule

    parser = get_script_arguments()
    args = parser.parse_args()
    if args.version:
        print_version()
        sys.exit(0)

    # Set logger
    try:
        debug_mode_ = CentralizedConfig.get_server_config().logging.get_level_value()
    except Exception:
        debug_mode_ = 0

    main_logger = set_logging(debug_mode=debug_mode_)

    if hasattr(args, 'func'):
        args.func()
    else:
        parser.print_help()
