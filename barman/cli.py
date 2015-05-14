# Copyright (C) 2011-2015 2ndQuadrant Italia (Devise.IT S.r.L.)
#
# This file is part of Barman.
#
# Barman is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Barman is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Barman.  If not, see <http://www.gnu.org/licenses/>.

"""
This module implements the interface with the command line and the logger.
"""
import logging
import os
import sys

from argh import ArghParser, named, arg, expects_obj
from argparse import SUPPRESS, ArgumentTypeError

from barman import output
from barman.infofile import BackupInfo
from barman import lockfile
from barman.server import Server
import barman.diagnose
import barman.config
from barman.utils import drop_privileges, configure_logging, parse_log_level


_logger = logging.getLogger(__name__)


def check_positive(value):
    """
    Check for a positive integer option

    :param value: str containing the value to check
    """
    if value is None:
        return None
    try:
        int_value = int(value)
    except Exception:
        raise ArgumentTypeError("'%s' is not a valid positive integer" % value)
    if int_value < 0:
        raise ArgumentTypeError("'%s' is not a valid positive integer" % value)
    return int_value


@named('list-server')
@arg('--minimal', help='machine readable output')
def list_server(minimal=False):
    """
    List available servers, with useful information
    """
    servers = get_server_list()
    for name in sorted(servers):
        server = servers[name]
        output.init('list_server', name, minimal=minimal)
        description = server.config.description
        # If server has errors
        if server.config.disabled:
            description += ("  (WARNING: Server temporarily disabled "
                           "due to configuration errors)")
        # If the server has been manually disabled
        elif not server.config.active:
            description += "  (WARNING: Server is not active)"
        output.result('list_server', name, description)
    output.close_and_exit()


def cron():
    """
    Run maintenance tasks
    """
    try:
        with lockfile.GlobalCronLock(barman.__config__.barman_lock_directory):
            servers = get_server_list(skip_inactive=True)
            for server in sorted(servers):
                server_error_output(servers[server], True)
                servers[server].cron()
    except lockfile.LockFileBusy:
        output.info("Another cron is running")

    except lockfile.LockFilePermissionDenied, e:
        output.error("Permission denied, unable to access '%s'", e)
    output.close_and_exit()


# noinspection PyUnusedLocal
def server_completer(prefix, parsed_args, **kwargs):
    global_config(parsed_args)
    for conf in barman.__config__.servers():
        if conf.name.startswith(prefix):
            yield conf.name


# noinspection PyUnusedLocal
def server_completer_all(prefix, parsed_args, **kwargs):
    global_config(parsed_args)
    current_list = getattr(parsed_args, 'server_name', None) or ()
    for conf in barman.__config__.servers():
        if conf.name.startswith(prefix) and conf.name not in current_list:
            yield conf.name
    if len(current_list) == 0 and 'all'.startswith(prefix):
        yield 'all'


# noinspection PyUnusedLocal
def backup_completer(prefix, parsed_args, **kwargs):
    global_config(parsed_args)
    server = get_server(parsed_args)
    backup_name = getattr(parsed_args, 'backup_id', None) or ''
    if server:
        backups = server.get_available_backups()
        for backup_id in sorted(backups, reverse=True):
            if backup_id.startswith(prefix):
                yield backup_id
        for special_id in ('latest', 'last', 'oldest', 'first'):
            if len(backups) > 0 and special_id.startswith(prefix):
                yield special_id


@arg('server_name', nargs='+',
     completer=server_completer_all,
     help="specifies the server names for the backup command "
          "('all' will show all available servers)")
@arg('--immediate-checkpoint',
     help='forces the initial checkpoint to be done as quickly as possible',
     dest='immediate_checkpoint',
     action='store_true',
     default=SUPPRESS)
@arg('--no-immediate-checkpoint',
     help='forces the initial checkpoint to be spreaded',
     dest='immediate_checkpoint',
     action='store_false',
     default=SUPPRESS)
@arg('--reuse-backup', nargs='?',
     choices=barman.config.REUSE_BACKUP_VALUES,
     default=None, const='link',
     help='use the previous backup to improve transfer-rate. '
          'If no argument is given "link" is assumed')
@arg('--retry-times',
     help='Number of retries after an error if base backup copy fails.',
     type=check_positive)
@arg('--retry-sleep',
     help='Wait time after a failed base backup copy, before retrying.',
     type=check_positive)
@arg('--no-retry', help='Disable base backup copy retry logic.',
     dest='retry_times', action='store_const', const=0)
@expects_obj
def backup(args):
    """
    Perform a full backup for the given server (supports 'all')
    """
    servers = get_server_list(args, skip_inactive=True, skip_disabled=True)
    for name in sorted(servers):
        server = servers[name]
        if is_unknown_server(server, name):
            continue
        server_error_output(server, True)
        if args.reuse_backup is not None:
            server.config.reuse_backup = args.reuse_backup
        if args.retry_sleep is not None:
            server.config.basebackup_retry_sleep = args.retry_sleep
        if args.retry_times is not None:
            server.config.basebackup_retry_times = args.retry_times
        if hasattr(args, 'immediate_checkpoint'):
            server.config.immediate_checkpoint = args.immediate_checkpoint
        server.backup()
    output.close_and_exit()


@named('list-backup')
@arg('server_name', nargs='+',
     completer=server_completer_all,
     help="specifies the server name for the command "
          "('all' will show all available servers)")
@arg('--minimal', help='machine readable output', action='store_true')
@expects_obj
def list_backup(args):
    """
    List available backups for the given server (supports 'all')
    """
    servers = get_server_list(args)
    for name in sorted(servers):
        server = servers[name]
        output.init('list_backup', name, minimal=args.minimal)
        if is_unknown_server(server, name):
            continue
        server_error_output(server)
        server.list_backups()
    output.close_and_exit()


@arg('server_name', nargs='+',
     completer=server_completer_all,
     help='specifies the server name for the command')
@expects_obj
def status(args):
    """
    Shows live information and status of the PostgreSQL server
    """
    servers = get_server_list(args)
    for name in sorted(servers):
        server = servers[name]
        if is_unknown_server(server, name):
            continue
        server_error_output(server, False)
        output.init('status', name)
        server.status()
    output.close_and_exit()


@arg('server_name', nargs='+',
     completer=server_completer_all,
     help='specifies the server name for the command')
@expects_obj
def rebuild_xlogdb(args):
    """
    Rebuild the WAL file database guessing it from the disk content.
    """
    servers = get_server_list(args)
    for name in sorted(servers):
        server = servers[name]
        if is_unknown_server(server, name):
            continue
        server_error_output(server, True)
        server.rebuild_xlogdb()
    output.close_and_exit()


@arg('server_name',
     completer=server_completer,
     help='specifies the server name for the command')
@arg('--target-tli', help='target timeline', type=int)
@arg('--target-time',
     help='target time. You can use any valid unambiguous representation. e.g: "YYYY-MM-DD HH:MM:SS.mmm"')
@arg('--target-xid', help='target transaction ID')
@arg('--target-name',
     help='target name created previously with pg_create_restore_point() function call')
@arg('--exclusive',
     help='set target xid to be non inclusive', action="store_true")
@arg('--tablespace',
     help='tablespace relocation rule',
     metavar='NAME:LOCATION', action='append')
@arg('--remote-ssh-command',
     metavar='SSH_COMMAND',
     help='This options activates remote recovery, by specifying the secure shell command '
          'to be launched on a remote host. It is the equivalent of the "ssh_command" server'
          'option in the configuration file for remote recovery. Example: "ssh postgres@db2"')
@arg('backup_id',
     completer=backup_completer,
     help='specifies the backup ID to recover')
@arg('destination_directory',
     help='the directory where the new server is created')
@arg('--retry-times',
     help='Number of retries after an error if base backup copy fails.',
     type=check_positive)
@arg('--retry-sleep',
     help='Wait time after a failed base backup copy, before retrying.',
     type=check_positive)
@arg('--no-retry', help='Disable base backup copy retry logic.',
     dest='retry_times', action='store_const', const=0)
@expects_obj
def recover(args):
    """
    Recover a server at a given time or xid
    """
    server = get_server(args, True)
    if is_unknown_server(server, args.server_name):
        output.close_and_exit()
    # Retrieves the backup
    backup = parse_backup_id(server, args)
    if backup is None or backup.status != BackupInfo.DONE:
        output.error("Unknown backup '%s' for server '%s'",
                     args.backup_id, args.server_name)
        output.close_and_exit()

    # decode the tablespace relocation rules
    tablespaces = {}
    if args.tablespace:
        for rule in args.tablespace:
            try:
                tablespaces.update([rule.split(':', 1)])
            except ValueError:
                output.error(
                    "Invalid tablespace relocation rule '%s'\n"
                    "HINT: The valid syntax for a relocation rule is "
                    "NAME:LOCATION", rule)
                output.close_and_exit()

    # validate the rules against the tablespace list
    valid_tablespaces = [tablespace_data.name for tablespace_data in
                         backup.tablespaces] if backup.tablespaces else []
    for item in tablespaces:
        if item not in valid_tablespaces:
            output.error("Invalid tablespace name '%s'\n"
                         "HINT: Please use any of the following "
                         "tablespaces: %s",
                         item, ', '.join(valid_tablespaces))
            output.close_and_exit()

    # explicitly disallow the rsync remote syntax (common mistake)
    if ':' in args.destination_directory:
        output.error(
            "The destination directory parameter "
            "cannot contain the ':' character\n"
            "HINT: If you want to do a remote recovery you have to use "
            "the --remote-ssh-command option")
        output.close_and_exit()
    if args.retry_sleep is not None:
        server.config.basebackup_retry_sleep = args.retry_sleep
    if args.retry_times is not None:
        server.config.basebackup_retry_times = args.retry_times
    server.recover(backup,
                   args.destination_directory,
                   tablespaces=tablespaces,
                   target_tli=args.target_tli,
                   target_time=args.target_time,
                   target_xid=args.target_xid,
                   target_name=args.target_name,
                   exclusive=args.exclusive,
                   remote_command=args.remote_ssh_command)

    output.close_and_exit()


@named('show-server')
@arg('server_name', nargs='+',
     completer=server_completer_all,
     help="specifies the server names to show ('all' will show all available servers)")
@expects_obj
def show_server(args):
    """
    Show all configuration parameters for the specified servers
    """
    servers = get_server_list(args)
    for name in sorted(servers):
        server = servers[name]
        if is_unknown_server(server, name):
            continue
        server_error_output(server, False, False)
        output.init('show_server', name)
        server.show()
    output.close_and_exit()


@arg('server_name', nargs='+',
     completer=server_completer_all,
     help="specifies the server names to check "
          "('all' will check all available servers)")
@arg('--nagios', help='Nagios plugin compatible output', action='store_true')
@expects_obj
def check(args):
    """
    Check if the server configuration is working.

    This command returns success if every checks pass,
    or failure if any of these fails
    """
    if args.nagios:
        output.set_output_writer(output.NagiosOutputWriter())
    servers = get_server_list(args, skip_inactive=True)
    for name in sorted(servers):
        server = servers[name]
        if is_unknown_server(server, name):
            continue
        # If the server is not active
        if not server.config.active:
            name += ' (not active)'
        output.init('check', name)
        server.check()
    output.close_and_exit()


def diagnose():
    """
    Diagnostic command (for support and problems detection purpose)
    """
    servers = get_server_list(on_error_stop=False, suppress_error=True)
    # errors list with duplicate paths between servers
    errors_list = []
    if servers:
        errors_list = barman.__config__.servers_msg_list
    barman.diagnose.exec_diagnose(servers, errors_list)
    output.close_and_exit()


@named('show-backup')
@arg('server_name',
     completer=server_completer,
     help='specifies the server name for the command')
@arg('backup_id',
     completer=backup_completer,
     help='specifies the backup ID')
@expects_obj
def show_backup(args):
    """
    This method shows a single backup information
    """
    server = get_server(args)
    if not is_unknown_server(server, args.server_name):
        # Retrieves the backup
        backup_info = parse_backup_id(server, args)
        if backup_info is None:
            output.error("Unknown backup '%s' for server '%s'" % (
                args.backup_id, args.server_name))
        else:
            server.show_backup(backup_info)
    output.close_and_exit()


@named('list-files')
@arg('server_name',
     completer=server_completer,
     help='specifies the server name for the command')
@arg('backup_id',
     completer=backup_completer,
     help='specifies the backup ID')
@arg('--target', choices=('standalone', 'data', 'wal', 'full'),
     default='standalone',
     help='''
     Possible values are: data (just the data files), standalone (base backup files, including required WAL files),
     wal (just WAL files between the beginning of base backup and the following one (if any) or the end of the log) and
     full (same as data + wal). Defaults to %(default)s
     '''
)
@expects_obj
def list_files(args):
    """
    List all the files for a single backup
    """
    server = get_server(args)
    if is_unknown_server(server, args.server_name):
        output.close_and_exit()
    # Retrieves the backup
    backup = parse_backup_id(server, args)
    if backup is None:
        output.error("Unknown backup '%s' for server '%s'", args.backup_id,
                     args.server_name)
        output.close_and_exit()
    for line in backup.get_list_of_files(args.target):
        output.info(line, log=False)
    output.close_and_exit()


@arg('server_name',
     completer=server_completer,
     help='specifies the server name for the command')
@arg('backup_id',
     completer=backup_completer,
     help='specifies the backup ID')
@expects_obj
def delete(args):
    """
    Delete a backup
    """
    server = get_server(args, True)
    if is_unknown_server(server, args.server_name):
        output.close_and_exit()
    # Retrieves the backup
    backup = parse_backup_id(server, args)
    if backup is None:
        output.error("Unknown backup '%s' for server '%s'", args.backup_id,
                     args.server_name)
        output.close_and_exit()
    server.delete_backup(backup)
    output.close_and_exit()


def global_config(args):
    """
    Set the configuration file
    """
    if hasattr(args, 'config'):
        filename = args.config
    else:
        try:
            filename = os.environ['BARMAN_CONFIG_FILE']
        except KeyError:
            filename = None
    config = barman.config.Config(filename)
    barman.__config__ = config

    # change user if needed
    try:
        drop_privileges(config.user)
    except OSError:
        msg = "ERROR: please run barman as %r user" % config.user
        raise SystemExit(msg)
    except KeyError:
        msg = "ERROR: the configured user %r does not exists" % config.user
        raise SystemExit(msg)

    # configure logging
    log_level = parse_log_level(config.log_level)
    configure_logging(config.log_file,
                      log_level or barman.config.DEFAULT_LOG_LEVEL,
                      config.log_format)
    if log_level is None:
        _logger.warn('unknown log_level in config file: %s', config.log_level)

    # configure output
    if args.format != output.DEFAULT_WRITER or args.quiet or args.debug:
        output.set_output_writer(args.format,
                                 quiet=args.quiet,
                                 debug=args.debug)

    # Load additional configuration files
    _logger.debug('Loading additional configuration files')
    config.load_configuration_files_directory()
    # We must validate the configuration here in order to have
    # both output and logging configured
    config.validate_global_config()

    _logger.debug('Initialised Barman version %s (config: %s)',
                  barman.__version__, config.config_file)


def get_server(args, dangerous=False):
    """
    Get a single server retrieving his configuration

    :param args: an argparse namespace containing a single server_name parameter
    :param bool dangerous: bool used to identify dangerous commands invocations
    """
    config = barman.__config__.get_server(args.server_name)
    # Get a list of configuration errors from all the servers
    global_error_list = barman.__config__.servers_msg_list

    if global_error_list:
        # Output errors
        for conflict_paths in global_error_list:
            output.error(conflict_paths)
        output.close_and_exit()
    else:
        # If no configuration available for the requested server, return None
        if not config:
            return None
        server = Server(config)
        # Display errors if necessary
        server_error_output(server, dangerous)
        return server


def server_error_output(server, is_error=False, check_active=True):
    """
    Get a Server object, and check if is disabled.
    If disabled display all the configuration errors.

    :param barman.server.Server server: the server configuration that should
        contain errors
    :param bool is_error: identify the severity of the error
    :param bool check_active: perform check for active parameter
    """

    # If the server is disabled, output errors
    if server.config.disabled:
        if is_error:
            # Output all the messages as errors, and exit terminating the run.
            for message in server.config.msg_list:
                output.error(message)
            output.close_and_exit()
        else:
            # Non blocking error, output as warning
            for message in server.config.msg_list:
                output.warning(message)
        # Filter for active/not active messages
        check_active = False
    # Check if the server is active and output it's status
    if check_active:
        # server not active
        if not server.config.active:
            if is_error:
                # Blocking error, output and terminate the run
                output.error('Not active server: %s' % server.config.name)
                output.close_and_exit()
            else:
                output.warning('Not active server: %s' % server.config.name)


def is_unknown_server(server, name):
    """
    Basic control Server not None

    :param barman.server.Server server: the server we want to check
    :param str name: the name of the server
    :return: boolean
    """
    if server is None:
        output.error("Unknown server '%s'" % name)
        return True
    return False


def get_server_list(args=None, skip_inactive=False, skip_disabled=False,
                    on_error_stop=True, suppress_error=False):
    """
    Get the server list from the configuration

    If args the parameter is None or arg.server_name[0] is 'all'
    returns all defined servers

    :param args: an argparse namespace containing a list server_name parameter
    :param bool skip_inactive: skip inactive servers when 'all' is required
    :param bool skip_disabled: skip disabled servers when 'all' is required
    :param bool on_error_stop: stop if an error is found
    :param bool suppress_error: suppress display of errors (e.g. diagnose)
    """
    server_dict = {}

    barman_config_servers = barman.__config__.servers()
    # Get a list of configuration errors from all the servers
    global_error_list = barman.__config__.servers_msg_list

    # Global errors have higher priority
    if global_error_list:
        # Output the list of global errors
        if not suppress_error:
            for conflict_paths in global_error_list:
                output.error(conflict_paths)

        # If requested, exit on first error
        if on_error_stop:
            output.close_and_exit()
            return

    # If no argument provided or server_name[0] is 'all'
    if args is None or args.server_name[0] == 'all':
        # Then return a list of all the configured servers
        for conf in barman_config_servers:
            # Skip inactive servers, if requested
            if skip_inactive and not conf.active:
                output.info("Skipping inactive server '%s'"
                            % conf.name)
                continue

            # Skip disabled servers, if requested
            if skip_disabled and conf.disabled:
                output.info("Skipping temporarily disabled server '%s'"
                            % conf.name)
                continue

            # Create a server
            server = Server(conf)
            server_dict[conf.name] = server
    else:
        # Manage a list of servers as arguments
        for server in args.server_name:
            conf = barman.__config__.get_server(server)
            if conf is None:
                server_dict[server] = None
            else:
                server_dict[server] = Server(conf)

    return server_dict

def parse_backup_id(server, args):
    """
    Parses backup IDs including special words such as latest, oldest, etc.

    :param Server server: server object to search for the required backup
    :param args: command lien arguments namespace
    :rtype BackupInfo,None: the decoded backup_info object
    """
    if args.backup_id in ('latest', 'last'):
        backup_id = server.get_last_backup()
    elif args.backup_id in ('oldest', 'first'):
        backup_id = server.get_first_backup()
    else:
        backup_id = args.backup_id
    return server.get_backup(backup_id)


def main():
    """
    The main method of Barman
    """
    p = ArghParser()
    p.add_argument('-v', '--version', action='version',
                   version=barman.__version__)
    p.add_argument('-c', '--config',
                   help='uses a configuration file '
                        '(defaults: %s)'
                        % ', '.join(barman.config.Config.CONFIG_FILES),
                   default=SUPPRESS)
    p.add_argument('-q', '--quiet', help='be quiet', action='store_true')
    p.add_argument('-d', '--debug', help='debug output', action='store_true')
    p.add_argument('-f', '--format', help='output format',
                   choices=output.AVAILABLE_WRITERS.keys(),
                   default=output.DEFAULT_WRITER)
    p.add_commands(
        [
            cron,
            list_server,
            show_server,
            status,
            check,
            diagnose,
            backup,
            list_backup,
            show_backup,
            list_files,
            recover,
            delete,
            rebuild_xlogdb,
        ]
    )
    # noinspection PyBroadException
    try:
        p.dispatch(pre_call=global_config)
    except KeyboardInterrupt:
        msg = "Process interrupted by user (KeyboardInterrupt)"
        output.exception(msg)
    except Exception, e:
        msg = "%s\nSee log file for more details." % e
        output.exception(msg)

    # cleanup output API and exit honoring output.error_occurred and
    # output.error_exit_code
    output.close_and_exit()


if __name__ == '__main__':
    # This code requires the mock module and allow us to test
    # bash completion inside the IDE debugger
    try:
        # noinspection PyUnresolvedReferences
        import mock
        sys.stdout = mock.Mock(wraps=sys.stdout)
        sys.stdout.isatty.return_value = True
        os.dup2(2, 8)
    except ImportError:
        pass
    main()
