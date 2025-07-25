import argparse
import asyncio
import logging
import logging.config
import shutil
import sys
from configparser import ConfigParser
from pathlib import Path
from tempfile import gettempdir

import bandersnatch.configuration
import bandersnatch.delete
import bandersnatch.log
import bandersnatch.master
import bandersnatch.mirror
import bandersnatch.verify
from bandersnatch.config.exceptions import ConfigError, ConfigFileNotFound
from bandersnatch.storage import storage_backend_plugins

# See if we have uvloop and use if so
try:
    import uvloop

    uvloop.install()
except ImportError:
    pass

logger = logging.getLogger(__name__)  # pylint: disable=C0103


def _delete_parser(subparsers: argparse._SubParsersAction) -> None:
    d = subparsers.add_parser(
        "delete",
        help=(
            "Consulte metadata (locally or remotely) and delete "
            + "entire package artifacts."
        ),
    )
    d.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Do not download or delete files",
    )
    d.add_argument(
        "--workers",
        type=int,
        default=0,
        help="# of parallel iops [Defaults to bandersnatch.conf]",
    )
    d.add_argument("pypi_packages", nargs="*")
    d.set_defaults(op="delete")


def _mirror_parser(subparsers: argparse._SubParsersAction) -> None:
    m = subparsers.add_parser(
        "mirror",
        help="Performs a one-time synchronization with the PyPI master server.",
    )
    m.add_argument(
        "--force-check",
        action="store_true",
        default=False,
        help=(
            "Force bandersnatch to reset the PyPI serial (move serial file to /tmp) to "
            + "perform a full sync"
        ),
    )
    m.set_defaults(op="mirror")


def _verify_parser(subparsers: argparse._SubParsersAction) -> None:
    v = subparsers.add_parser(
        "verify", help="Read in Metadata and check package file validity"
    )
    v.add_argument(
        "--delete",
        action="store_true",
        default=False,
        help="Enable deletion of packages not active",
    )
    v.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Do not download or delete files",
    )
    v.add_argument(
        "--json-update",
        action="store_true",
        default=False,
        help="Enable updating JSON from PyPI",
    )
    v.add_argument(
        "--workers",
        type=int,
        default=0,
        help="# of parallel iops [Defaults to bandersnatch.conf]",
    )
    v.set_defaults(op="verify")


def _sync_parser(subparsers: argparse._SubParsersAction) -> None:
    m = subparsers.add_parser(
        "sync",
        help="Synchronize specific packages with the PyPI master server.",
    )
    m.add_argument(
        "packages",
        metavar="package",
        nargs="+",
        help="The name of package to sync",
    )
    m.set_defaults(op="sync")
    m.add_argument(
        "--skip-simple-root",
        action="store_true",
        default=False,
        help="Skip updating simple index root page",
    )


def _make_parser() -> argparse.ArgumentParser:
    # Separated so sphinx-argparse-cli can do its auto documentation magic.
    parser = argparse.ArgumentParser(
        description="PyPI PEP 381 mirroring client.", prog="bandersnatch"
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {bandersnatch.__version__}"
    )
    parser.add_argument(
        "-c",
        "--config",
        default="/etc/bandersnatch.conf",
        help="use configuration file (default: %(default)s)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Turn on extra logging (DEBUG level)",
    )

    subparsers = parser.add_subparsers()
    _delete_parser(subparsers)
    _mirror_parser(subparsers)
    _verify_parser(subparsers)
    _sync_parser(subparsers)

    return parser


async def async_main(args: argparse.Namespace, config: ConfigParser) -> int:
    if args.op.lower() == "delete":
        allow_upstream_serial_mismatch = config.getboolean("mirror", "allow-upstream-serial-mismatch", fallback=False)
        async with bandersnatch.master.Master(
            config.get("mirror", "master"),
            config.getfloat("mirror", "timeout"),
            config.getfloat("mirror", "global-timeout", fallback=None),
            allow_upstream_serial_mismatch=allow_upstream_serial_mismatch,
        ) as master:
            return await bandersnatch.delete.delete_packages(config, args, master)
    elif args.op.lower() == "verify":
        return await bandersnatch.verify.metadata_verify(config, args)
    elif args.op.lower() == "sync":
        return await bandersnatch.mirror.mirror(
            config, args.packages, not args.skip_simple_root
        )

    if args.force_check:
        storage_plugin = next(iter(storage_backend_plugins()))
        status_file = (
            storage_plugin.PATH_BACKEND(config.get("mirror", "directory")) / "status"
        )
        if status_file.exists():
            tmp_status_file = Path(gettempdir()) / "status"
            try:
                shutil.move(str(status_file), tmp_status_file)
                logger.debug(
                    "Force bandersnatch to check everything against the master PyPI"
                    + f" - status file moved to {tmp_status_file}"
                )
            except OSError as e:
                logger.error(
                    f"Could not move status file ({status_file} to "
                    + f" {tmp_status_file}): {e}"
                )
        else:
            logger.info(
                f"No status file to move ({status_file}) - Full sync will occur"
            )

    return await bandersnatch.mirror.mirror(config)


def main(loop: asyncio.AbstractEventLoop | None = None) -> int:
    parser = _make_parser()
    if len(sys.argv) < 2:
        parser.print_help()
        parser.exit()

    args = parser.parse_args()

    bandersnatch.log.setup_logging(args)

    # Prepare default config file if needed.
    config_path = Path(args.config)
    try:
        logger.info("Reading configuration file '%s'", config_path)
        config = bandersnatch.configuration.BandersnatchConfig(config_path)
    except ConfigFileNotFound:
        logger.warning(f"Config file '{args.config}' missing, creating default config.")
        logger.warning("Please review the config file, then run 'bandersnatch' again.")
        bandersnatch.configuration.create_example_config(config_path)
        return 1
    except ConfigError as err:
        logger.error("Unable to load configuration: %s", err)
        logger.debug("Error details:", exc_info=err)
        return 2

    user_log_config = config.get("mirror", "log-config", fallback=None)
    if user_log_config:
        logging.config.fileConfig(Path(user_log_config))

    if loop:
        loop.set_debug(args.debug)
    return asyncio.run(async_main(args, config))


if __name__ == "__main__":
    exit(main())
