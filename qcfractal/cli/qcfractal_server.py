"""
A command line interface to the qcfractal server.
"""

import argparse
import shutil
import sys

import yaml

import qcfractal

from .cli_utils import install_signal_handlers
from ..config import DatabaseSettings, FractalConfig, FractalServerSettings
from ..postgres_harness import PostgresHarness
from ..storage_sockets import storage_socket_factory


def ensure_postgres_alive(psql):
    if not psql.is_alive():
        try:
            print("\nCould not detect a PostgreSQL from configuration options, starting a PostgreSQL server.\n")
            psql.start()
        except ValueError as e:
            print(str(e))
            sys.exit(1)


def parse_args():
    parser = argparse.ArgumentParser(description='A CLI for the QCFractalServer.')
    parser.add_argument('--version', action='version', version=f"{qcfractal.__version__}")

    subparsers = parser.add_subparsers(dest="command")

    ### Init subcommands
    init = subparsers.add_parser('init', help="Initializes a QCFractal server and database information.")
    db_init = init.add_argument_group('Database Settings')
    for field in DatabaseSettings.field_names():
        cli_name = "--db-" + field.replace("_", "-")
        db_init.add_argument(cli_name, **DatabaseSettings.help_info(field))

    server_init = init.add_argument_group('Server Settings')
    for field in FractalServerSettings.field_names():
        cli_name = "--" + field.replace("_", "-")
        server_init.add_argument(cli_name, **FractalServerSettings.help_info(field))

    init.add_argument("--overwrite-config", action='store_true', help="Overwrites the current configuration file.")
    init.add_argument("--clear-database",
                      action='store_true',
                      help="Clear the content of the given database and initialize it.")
    init.add_argument("--base-folder", **FractalConfig.help_info("base_folder"))

    ### Start subcommands
    start = subparsers.add_parser('start', help="Starts a QCFractal server instance.")
    start.add_argument("--base-folder", **FractalConfig.help_info("base_folder"))

    # Allow port and logfile to be altered on the fly
    fractal_args = start.add_argument_group('Server Settings')
    for field in ["port", "logfile"]:
        cli_name = "--" + field.replace("_", "-")
        fractal_args.add_argument(cli_name, **FractalServerSettings.help_info(field))

    fractal_args.add_argument("--server-name", **FractalServerSettings.help_info("name"))
    fractal_args.add_argument(
        "--start-periodics",
        default=True,
        type=bool,
        help="Expert! Can disable periodic update (services, heartbeats) if False. Useful when running behind a proxy."
    )

    fractal_args.add_argument("--disable-ssl",
                              default=False,
                              type=bool,
                              help="Disables SSL if present, if False a SSL cert will be created for you.")
    fractal_args.add_argument("--tls-cert", type=str, default=None, help="Certificate file for TLS (in PEM format)")
    fractal_args.add_argument("--tls-key", type=str, default=None, help="Private key file for TLS (in PEM format)")

    ### Upgrade subcommands
    upgrade = subparsers.add_parser('upgrade', help="Upgrade QCFractal database.")
    upgrade.add_argument("--base-folder", **FractalConfig.help_info("base_folder"))

    compute_args = start.add_argument_group('Local Computation Settings')
    compute_args.add_argument("--local-manager",
                              const=-1,
                              default=None,
                              action='store',
                              nargs='?',
                              type=int,
                              help='Creates a local pool QueueManager attached to the server.')

    ### Config subcommands
    info = subparsers.add_parser('info', help="Manage users and permissions on a QCFractal server instance.")
    info.add_argument("category", nargs="?", default="config", choices=["config", "alembic"], help="The config category to show.")
    info.add_argument("--base-folder", **FractalConfig.help_info("base_folder"))

    ### User subcommands
    user = subparsers.add_parser('user', help="Configure a QCFractal server instance.")
    user.add_argument("--base-folder", **FractalConfig.help_info("base_folder"))

    user_subparsers = user.add_subparsers(dest="user_command")

    user_add = user_subparsers.add_parser("add", help="Add a user to the QCFractal server.")
    user_add.add_argument("username", default=None, type=str, help="The username to add.")
    user_add.add_argument("--password", default=None, type=str, required=False,
                          help="The password for the user. If None, a default one will be created and printed.")
    user_add.add_argument("--permissions", nargs='+', default=None, type=str, required=True,
                          help="Permissions for the user. Allowed values: read, write, queue, compute, admin.")

    user_show = user_subparsers.add_parser("info", help="Show the user's current permissions.")
    user_show.add_argument("username", default=None, type=str, help="The username to show.")

    user_modify = user_subparsers.add_parser("modify", help="Change a user's password or permissions.")
    user_modify.add_argument("username", default=None, type=str, help="The username to modify.")
    user_modify_password = user_modify.add_mutually_exclusive_group()
    user_modify_password.add_argument("--password", type=str, default=None, required=False,
                                      help="Change the user's password to the specified value.")
    user_modify_password.add_argument("--reset-password", action='store_true',
                                      help="Reset the user's password. A new password will be generated and printed.")
    user_modify.add_argument("--permissions", nargs='+', default=None, type=str, required=False,
                             help="Change the users's permissions. Allowed values: read, write, compute, queue, admin.")

    user_remove = user_subparsers.add_parser("remove", help="Remove a user.")
    user_remove.add_argument("username", default=None, type=str, help="The username to remove.")

    ### Move args around
    args = vars(parser.parse_args())

    ret = {}
    ret["database"] = {}
    ret["fractal"] = {}
    for key, value, in args.items():

        # DB bucket
        if ("db_" in key) and (key.replace("db_", "") in DatabaseSettings.field_names()):
            if value is None:
                continue
            ret["database"][key.replace("db_", "")] = value

        # Fractal bucket
        elif key in FractalServerSettings.field_names():
            if value is None:
                continue
            ret["fractal"][key] = value

        # Additional base values that should be none
        elif key in ["base_folder"]:
            if value is None:
                continue
            ret[key] = value
        else:
            ret[key] = value

    if args["command"] is None:
        parser.print_help(sys.stderr)
        sys.exit(1)

    return ret


def server_init(args, config):
    # alembic stamp head

    print("Initializing QCFractal configuration.")
    # Configuration settings

    config.base_path.mkdir(parents=True, exist_ok=True)
    overwrite_config = args.get("overwrite_config", False)
    clear_database = args.get("clear_database", False)

    psql = PostgresHarness(config, quiet=False, logger=print)

    # Make sure we do not delete anything.
    if config.config_file_path.exists():
        print()
        if not overwrite_config:
            print("QCFractal configuration file already exists, to overwrite use '--overwrite-config' "
                  "or use the `qcfractal-server config` command line to alter settings.")
            sys.exit(2)
        else:
            user_required_input = f"REMOVEALLDATA {str(config.database_path)}"
            print("!WARNING! A QCFractal configuration is currently initialized")
            print(
                f"!WARNING! Overwriting will delete all current Fractal data, this includes all data in {str(config.database_path)}."
            )
            print("!WARNING! Please use `qcfractal-server config` to alter configuration settings instead.")
            print()
            print(f"!WARNING! If you are sure you wish to proceed please type '{user_required_input}' below.")

            inp = input("  > ")
            print()
            if inp == user_required_input:
                print("All data will be removed from the current QCFractal instance.")
                psql.shutdown()
                shutil.rmtree(str(config.database_path), ignore_errors=True)
            else:
                print("Input does not match 'REMOVEALLDATA', exiting.")
                sys.exit(1)

    # WARNING! Passwords do not currently work.
    # if config.database.password is None:
    #     print("  Database password is None, generating a new private key.")
    #     config.database.password = secrets.token_urlsafe(16)

    print_config = config.dict()
    print_config["database"]["password"] = "**************"
    print_config = yaml.dump(print_config, default_flow_style=False)
    print("\n>>> Settings found:\n")
    print(print_config)

    print("\n>>> Writing settings...")
    config.config_file_path.write_text(yaml.dump(config.dict(), default_flow_style=False))

    print("\n>>> Setting up PostgreSQL...\n")
    config.database_path.mkdir(exist_ok=True)
    if config.database.own:
        try:
            psql.initialize_postgres()
        except ValueError as e:
            print(str(e))
            sys.exit(1)
    else:
        print(
            "Own was set to False, QCFractal will expect a live PostgreSQL server with the above connection information."
        )

    if config.database.own or clear_database:

        print("\n>>> Initializing database schema...\n")
        try:
            psql.init_database()
        except ValueError as e:
            print(str(e))
            sys.exit(1)

    # create tables and stamp version (if not)
    print("\n>>> Finishing up...")
    print("\n>>> Success! Please run `qcfractal-server start` to boot a FractalServer!")


def server_info(args, config):

    psql = PostgresHarness(config, quiet=False, logger=print)

    if args["category"] == "config":
        print(f"Displaying QCFractal configuration:\n")
        print(yaml.dump(config.dict(), default_flow_style=False))
    elif args["category"] == "alembic":
        print(f"Displaying QCFractal Alembic CLI configuration:\n")
        print(" ".join(psql.alembic_commands()))


def server_start(args, config):
    # check if db not current, ask for upgrade

    print("Starting a QCFractal server.\n")

    print(f"QCFractal server base folder: {config.base_folder}")
    # Build an optional adapter
    if args["local_manager"]:
        ncores = args["local_manager"]
        if ncores == -1:
            ncores = None

        from concurrent.futures import ProcessPoolExecutor

        adapter = ProcessPoolExecutor(max_workers=ncores)

    else:
        adapter = None

    print("\n>>> Examining SSL Certificates...")
    # Handle SSL
    if args["disable_ssl"]:
        print("\nSSL disabled.")
        ssl_options = False
    else:
        ssl_certs = sum(args[x] is not None for x in ["tls_key", "tls_cert"])
        if ssl_certs == 0:
            ssl_options = True
            print("\nAutogenerated SSL certificates, clients must use 'verify=False' when connecting.")
        elif ssl_certs == 2:
            ssl_options = {"crt": args["tls_cert"], "key": args["tls_key"]}
            print("\nReading SSL certificates.")
        else:
            raise KeyError("Both tls-cert and tls-key must be passed in.")

    # Build the server itself
    if config.fractal.logfile is None:
        logfile = None
    else:
        logfile = str(config.base_path / config.fractal.logfile)

    print("\n>>> Checking the PostgreSQL connection...")
    psql = PostgresHarness(config, quiet=False, logger=print)

    ensure_postgres_alive(psql)

    # make sure DB is created
    psql.create_database(config.database.database_name)

    print("\n>>> Initializing the QCFractal server...")
    try:
        server = qcfractal.FractalServer(
            name=args.get("server_name", None) or config.fractal.name,
            port=config.fractal.port,
            compress_response=config.fractal.compress_response,

            # Security
            security=config.fractal.security,
            allow_read=config.fractal.allow_read,
            ssl_options=ssl_options,

            # Database
            storage_uri=config.database_uri(safe=False, database=""),
            storage_project_name=config.database.database_name,
            query_limit=config.fractal.query_limit,

            # Log options
            logfile_prefix=logfile,
            log_apis=config.fractal.log_apis,
            geo_file_path=config.geo_file_path(),

            # Queue options
            service_frequency=config.fractal.service_frequency,
            heartbeat_frequency=config.fractal.heartbeat_frequency,
            max_active_services=config.fractal.max_active_services,
            queue_socket=adapter)

    except Exception as e:
        print("Fatal during server startup:\n")
        print(str(e))
        print("\nFailed to start the server, shutting down.")
        sys.exit(1)

    print(f"Server: {str(server)}")

    # Register closing
    install_signal_handlers(server.loop, server.stop)

    # Blocks until keyboard interupt
    print("\n>>> Starting the QCFractal server...")
    server.start(start_periodics=args["start_periodics"])


def server_upgrade(args, config):
    # alembic upgrade head

    print("Upgrading QCFractal server.\n")

    print(f"QCFractal server base folder: {config.base_folder}")

    print("\n>>> Checking the PostgreSQL connection...")
    psql = PostgresHarness(config, quiet=False, logger=print)

    ensure_postgres_alive(psql)

    print("\n>>> Upgrading the Database...")

    try:
        psql.upgrade()
    except ValueError as e:
        print(str(e))
        sys.exit(1)


def server_user(args, config):

    print("QCFractal server user function.\n")

    print(f"QCFractal server base folder: {config.base_folder}")

    print("\n>>> Checking the PostgreSQL connection...")

    psql = PostgresHarness(config, quiet=False, logger=print)
    ensure_postgres_alive(psql)

    storage = storage_socket_factory(config.database_uri(safe=False))

    print(args)
    try:
        if args["user_command"] == "add":
            print("\n>>> Adding new user...")
            success, pw = storage.add_user(args["username"], password=args["password"], permissions=args["permissions"])
            if success:
                print(f"\n>>> New user successfully added, password:\n{pw}")
                if config.fractal.security is None:
                    print("Warning: security is disabled. To enable security, change the configuration YAML field "
                          "fractal:security to local.")
            else:
                print("\n>>> Failed to add user. Perhaps the username is already taken?")
                sys.exit(1)
        elif args["user_command"] == "info":
            print(f"\n>>> Showing permissions for user '{args['username']}'...")
            permissions = storage.get_user_permissions(args["username"])
            if permissions is None:
                print("Username not found!")
                sys.exit(1)
            else:
                print(permissions)
        elif args["user_command"] == "modify":
            print(f"\n>>> Modifying user '{args['username']}'...")
            success, message = storage.modify_user(args["username"],
                                                   args["password"],
                                                   args["reset_password"],
                                                   args["permissions"])
            if success:
                info = "Successfully modified user\n"
                if message is not None:
                    info += "with message: " + message
                print(info)
            else:
                print("Failed to modify user\nwith message:", message)
                sys.exit(1)
        elif args["user_command"] == "remove":
            print(f"\n>>> Removing user '{args['username']}'...")
            if storage.remove_user(args["username"]):
                print("Successfully removed user.")
            else:
                print("Failed to remove user.")
                sys.exit(1)

    except Exception as e:
        print(type(e), str(e))
        sys.exit(1)


def main(args=None):

    # Grab CLI args if not present
    if args is None:
        args = parse_args()

    command = args.pop("command")

    # More default manipulation to get supersets correct
    config_kwargs = ["base_folder", "fractal", "database"]
    config_args = {}
    for x in config_kwargs:
        if x in args:
            config_args[x] = args.pop(x)

    config = FractalConfig(**config_args)

    # Merge files
    if command != "init":
        if not config.base_path.exists():
            print(f"Could not find configuration file path: {config.base_path}")
            sys.exit(1)
        if not config.config_file_path.exists():
            print(f"Could not find configuration file: {config.config_file_path}")
            sys.exit(1)

        file_dict = FractalConfig(**yaml.load(config.config_file_path.read_text(), Loader=yaml.FullLoader)).dict()
        config_dict = config.dict(skip_defaults=True)

        # Only fractal options can be changed by user input parameters
        file_dict["fractal"] = {**file_dict.pop("fractal"), **config_dict.pop("fractal")}

        config = FractalConfig(**file_dict)

    if command == "init":
        server_init(args, config)
    elif command == "info":
        server_info(args, config)
    elif command == "start":
        server_start(args, config)
    elif command == 'upgrade':
        server_upgrade(args, config)
    elif command == 'user':
        server_user(args, config)


if __name__ == '__main__':
    main()
