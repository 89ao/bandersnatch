import asyncio
import logging
import sys
from collections.abc import AsyncGenerator
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp_xmlrpc.client import ServerProxy

import bandersnatch
from bandersnatch.config.proxy import get_aiohttp_proxy_kwargs, proxy_address_from_env

from .errors import PackageNotFound
from .utils import USER_AGENT

if sys.version_info >= (3, 8) and sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logger = logging.getLogger(__name__)
FIVE_HOURS_FLOAT = 5 * 60 * 60.0
PYPI_SERIAL_HEADER = "X-PYPI-LAST-SERIAL"


class StalePage(Exception):
    """We got a page back from PyPI that doesn't meet our expected serial."""


class XmlRpcError(aiohttp.ClientError):
    """Issue getting package listing from PyPI Repository"""


class Master:
    def __init__(
        self,
        url: str,
        timeout: float = 10.0,
        global_timeout: float | None = FIVE_HOURS_FLOAT,
        proxy: str | None = None,
        allow_non_https: bool = False,
        allow_upstream_serial_mismatch: bool = False,
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.global_timeout = global_timeout or FIVE_HOURS_FLOAT
        self.allow_upstream_serial_mismatch = allow_upstream_serial_mismatch

        proxy_url = proxy if proxy else proxy_address_from_env()
        self.proxy_kwargs = get_aiohttp_proxy_kwargs(proxy_url) if proxy_url else {}
        # testing self.proxy_kwargs b/c even if there is a proxy_url, get_aiohttp_proxy_kwargs may
        # still return {} if the url is invalid somehow
        if self.proxy_kwargs:
            logging.info("Using proxy URL %s", proxy_url)

        self.allow_non_https = allow_non_https
        if self.url.startswith("http://") and not self.allow_non_https:
            err = f"Master URL {url} is not https scheme"
            logger.error(err)
            raise ValueError(err)

        self.loop = asyncio.get_event_loop()

    async def __aenter__(self) -> "Master":
        logger.debug("Initializing Master's aiohttp ClientSession")
        custom_headers = {"User-Agent": USER_AGENT}
        skip_headers = {"User-Agent"}
        aiohttp_timeout = aiohttp.ClientTimeout(
            total=self.global_timeout,
            sock_connect=self.timeout,
            sock_read=self.timeout,
        )
        self.session = aiohttp.ClientSession(
            headers=custom_headers,
            skip_auto_headers=skip_headers,
            timeout=aiohttp_timeout,
            raise_for_status=True,
            **self.proxy_kwargs,
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        logger.debug("Closing Master's aiohttp ClientSession and waiting 0.1 seconds")
        await self.session.close()
        # Give time for things to actually close to avoid warnings
        # https://github.com/aio-libs/aiohttp/issues/1115
        await asyncio.sleep(0.1)

    async def check_for_stale_cache(
        self, path: str, required_serial: int | None, got_serial: int | None
    ) -> None:
        # The PYPI-LAST-SERIAL header allows us to identify cached entries,
        # e.g. via the public CDN or private, transparent mirrors and avoid us
        # injecting stale entries into the mirror without noticing.
        if required_serial is not None:
            # I am not making required_serial an optional argument because I
            # want you to think really hard before passing in None. This is a
            # really important check to achieve consistency and you should only
            # leave it out if you know what you're doing.
            if not got_serial or got_serial < required_serial:
                raise StalePage(
                    f"Expected PyPI serial {required_serial} for request {path} "
                    + f"but got {got_serial}. We can no longer issue a PURGE. "
                    + "Report issue to PyPA Warehouse GitHub if it persists ..."
                )

    async def get(
        self, path: str, required_serial: int | None, **kw: Any
    ) -> AsyncGenerator[aiohttp.ClientResponse, None]:
        logger.debug(f"Getting {path} (serial {required_serial})")
        if not path.startswith(("https://", "http://")):
            path = self.url + path
        async with self.session.get(path, **kw) as r:
            got_serial = (
                int(r.headers[PYPI_SERIAL_HEADER])
                if PYPI_SERIAL_HEADER in r.headers
                else None
            )
            await self.check_for_stale_cache(path, required_serial, got_serial)
            yield r

    # TODO: Add storage backend support / refactor - #554
    async def url_fetch(
        self,
        url: str,
        file_path: Path,
        executor: ProcessPoolExecutor | ThreadPoolExecutor | None = None,
        chunk_size: int = 65536,
    ) -> None:
        logger.info(f"Fetching {url}")

        await self.loop.run_in_executor(
            executor, partial(file_path.parent.mkdir, parents=True, exist_ok=True)
        )

        async with self.session.get(url) as response:
            with file_path.open("wb") as fd:
                while True:
                    chunk = await response.content.read(chunk_size)
                    if not chunk:
                        break
                    fd.write(chunk)

    @property
    def xmlrpc_url(self) -> str:
        return f"{self.url}/pypi"

    # TODO: Potentially make USER_AGENT more accessible from aiohttp-xmlrpc
    async def _gen_custom_headers(self) -> dict[str, str]:
        # Create dummy client so we can copy the USER_AGENT + prepend bandersnatch info
        dummy_client = ServerProxy(self.xmlrpc_url, loop=self.loop)
        custom_headers = {
            "User-Agent": (
                f"bandersnatch {bandersnatch.__version__} {dummy_client.USER_AGENT}"
            )
        }
        await dummy_client.close()
        return custom_headers

    async def _gen_xmlrpc_client(self) -> ServerProxy:
        custom_headers = await self._gen_custom_headers()
        client = ServerProxy(
            self.xmlrpc_url,
            client=self.session,
            loop=self.loop,
            headers=custom_headers,
        )
        return client

    # TODO: Add an async context manager to aiohttp-xmlrpc to replace this function
    async def rpc(self, method_name: str, serial: int = 0) -> Any:
        try:
            client = await self._gen_xmlrpc_client()
            method = getattr(client, method_name)
            if serial:
                return await method(serial)
            return await method()
        except TimeoutError as te:
            logger.error(f"Call to {method_name} @ {self.xmlrpc_url} timed out: {te}")

    async def all_packages(self) -> Any:
        all_packages_with_serial = await self.rpc("list_packages_with_serial")
        if not all_packages_with_serial:
            raise XmlRpcError("Unable to get full list of packages")
        return all_packages_with_serial

    async def changed_packages(self, last_serial: int) -> dict[str, int]:
        changelog = await self.rpc("changelog_since_serial", last_serial)
        if changelog is None:
            changelog = []

        packages: dict[str, int] = {}
        for package, _version, _time, _action, serial in changelog:
            if serial > packages.get(package, 0):
                packages[package] = serial
        return packages

    async def get_package_metadata(self, package_name: str, serial: int = 0) -> Any:
        try:
            metadata_generator = self.get(f"/pypi/{package_name}/json", serial)
            metadata_response = await metadata_generator.asend(None)
            metadata = await metadata_response.json()
            return metadata
        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                raise PackageNotFound(package_name)
            raise
