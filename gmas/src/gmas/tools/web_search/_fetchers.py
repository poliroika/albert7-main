"""URL and browser-based page fetchers."""

import contextlib
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from collections import deque
from pathlib import Path
from typing import Any, ClassVar, Self

from gmas.config.logging import logger

from ._html import SimpleHTMLParser
from ._utils import _empty_result, normalize_url

# ============================================================
# URLFetcher — HTTP page download and parsing
# ============================================================


class URLFetcher:
    """Download and parse web pages via httpx (with urllib fallback)."""

    DEFAULT_HEADERS: ClassVar[dict[str, str]] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def __init__(
        self,
        timeout: int = 12,
        max_content_length: int = 500_000,
        *,
        trust_env: bool = False,
    ):
        self._timeout = timeout
        self._max_content_length = max_content_length
        self._trust_env = trust_env
        self._httpx_client: Any = None

    def _get_httpx_client(self) -> Any:
        if self._httpx_client is None:
            import httpx

            self._httpx_client = httpx.Client(
                timeout=self._timeout,
                follow_redirects=True,
                headers=self.DEFAULT_HEADERS,
                max_redirects=10,
                trust_env=self._trust_env,
            )
        return self._httpx_client

    def close(self) -> None:
        if self._httpx_client is not None:
            with contextlib.suppress(Exception):
                self._httpx_client.close()
            self._httpx_client = None

    def _parse_response(self, html_content: str) -> dict[str, str]:
        title, content = SimpleHTMLParser.extract_text(html_content, max_length=self._max_content_length)
        return {"title": title, "content": content}

    def fetch(self, url: str, *, timeout: int | None = None) -> dict[str, Any]:
        """Download and parse a web page. Tries httpx first, falls back to urllib."""
        effective_timeout = timeout if timeout is not None else self._timeout
        try:
            return self._fetch_httpx(url, effective_timeout)
        except ImportError:
            pass
        except (OSError, ValueError) as exc:
            logger.debug("httpx fetch failed for {}, falling back to urllib: {}", url, exc)

        return self._fetch_urllib(url, effective_timeout)

    def _fetch_httpx(self, url: str, timeout: int | None = None) -> dict[str, Any]:
        import httpx

        effective_timeout = timeout if timeout is not None else self._timeout
        result = _empty_result(url)

        try:
            client = self._get_httpx_client()

            with client.stream("GET", url, timeout=effective_timeout) as resp:
                resp.raise_for_status()

                content_type = resp.headers.get("content-type", "")
                if "text/html" not in content_type and "text/plain" not in content_type:
                    result["error"] = f"Unsupported content type: {content_type}"
                    return result

                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes(chunk_size=16_384):
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= self._max_content_length:
                        break

                raw = b"".join(chunks)[: self._max_content_length]
                charset = resp.encoding or "utf-8"
                html_body = raw.decode(charset, errors="replace")

            parsed = self._parse_response(html_body)
            result["title"] = parsed["title"]
            result["content"] = parsed["content"]
            result["success"] = True

        except httpx.TimeoutException:
            result["error"] = f"Request timed out after {effective_timeout} seconds"
        except httpx.HTTPStatusError as e:
            result["error"] = f"HTTP Error {e.response.status_code}"
        except (httpx.HTTPError, OSError, ValueError) as e:
            result["error"] = f"Fetch error: {e}"

        return result

    def _fetch_urllib(self, url: str, timeout: int | None = None) -> dict[str, Any]:
        effective_timeout = timeout if timeout is not None else self._timeout
        result = _empty_result(url)

        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            result["error"] = f"Unsupported URL scheme: {parsed.scheme!r}"
            return result

        _make_request = urllib.request.Request
        _do_open = urllib.request.urlopen

        try:
            request = _make_request(url, headers=self.DEFAULT_HEADERS)

            with _do_open(request, timeout=effective_timeout) as response:
                content_type = response.headers.get("Content-Type", "")
                if "text/html" not in content_type and "text/plain" not in content_type:
                    result["error"] = f"Unsupported content type: {content_type}"
                    return result

                raw_content = response.read(self._max_content_length)

                charset = "utf-8"
                if "charset=" in content_type:
                    cs_match = re.search(r"charset=([^\s;]+)", content_type)
                    if cs_match:
                        charset = cs_match.group(1)

                try:
                    html_content = raw_content.decode(charset, errors="replace")
                except (UnicodeDecodeError, LookupError):
                    html_content = raw_content.decode("utf-8", errors="replace")

            parsed = self._parse_response(html_content)
            result["title"] = parsed["title"]
            result["content"] = parsed["content"]
            result["success"] = True

        except urllib.error.HTTPError as e:
            result["error"] = f"HTTP Error {e.code}: {e.reason}"
        except urllib.error.URLError as e:
            result["error"] = f"URL Error: {e.reason}"
        except TimeoutError:
            result["error"] = f"Request timed out after {self._timeout} seconds"
        except (ValueError, OSError, UnicodeDecodeError) as e:
            result["error"] = f"Fetch error: {e}"

        return result


# ============================================================
# BrowserFetcher — abstract base for browser-based fetchers
# ============================================================


class BrowserFetcher(ABC):
    """Abstract base for browser-based fetchers that render JavaScript."""

    _QUICK_PAGE_LOAD_TIMEOUT: ClassVar[int] = 8
    _QUICK_EXTRA_WAIT: ClassVar[float] = 0.5
    _CLICK_SETTLE_DELAY: ClassVar[float] = 1.0
    _SUBMIT_SETTLE_DELAY: ClassVar[float] = 2.0
    _CRAWL_POLITENESS_DELAY: ClassVar[float] = 0.5
    _SCROLL_STABILITY_POLLS: ClassVar[int] = 2
    _MIN_SCROLL_WAIT: ClassVar[float] = 0.08
    _MAX_SCROLL_WAIT: ClassVar[float] = 0.35
    MIN_FALLBACK_CONTENT: ClassVar[int] = 200

    @abstractmethod
    def fetch(self, url: str, *, quick: bool = False) -> dict[str, Any]: ...

    @abstractmethod
    def fetch_with_wait(
        self,
        url: str,
        wait_for_selector: str | None = None,
        wait_timeout: int | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def click_element(self, selector: str, wait_timeout: int | None = None) -> dict[str, Any]: ...

    @abstractmethod
    def fill_input(
        self,
        selector: str,
        value: str,
        *,
        submit: bool = False,
        clear_first: bool = True,
        wait_timeout: int | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def extract_links(
        self,
        selector: str = "a[href]",
        *,
        base_url_filter: str | None = None,
        max_links: int = 50,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def execute_js(self, script: str) -> dict[str, Any]: ...

    @abstractmethod
    def get_current_url(self) -> str: ...

    @abstractmethod
    def get_page_content(self) -> dict[str, Any]: ...

    @abstractmethod
    def crawl(
        self,
        start_url: str,
        *,
        max_pages: int = 10,
        max_depth: int = 2,
        url_filter: str | None = None,
        link_selector: str = "a[href]",
        extract_content: bool = True,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def warm_up(self) -> bool: ...

    @abstractmethod
    def close(self) -> None: ...

    def supports_advanced_session(self) -> bool:
        return False

    def _unsupported_advanced_action(self, action: str) -> dict[str, Any]:
        return {"success": False, "error": f"Action '{action}' is supported only by the Playwright backend."}

    def list_tabs(self) -> dict[str, Any]:
        raise NotImplementedError(self._unsupported_advanced_action("list_tabs")["error"])

    def open_tab(
        self,
        url: str = "",
        *,
        wait_for_selector: str | None = None,
        background: bool = False,
    ) -> dict[str, Any]:
        raise NotImplementedError(self._unsupported_advanced_action("open_tab")["error"])

    def switch_tab(self, index: int) -> dict[str, Any]:
        raise NotImplementedError(self._unsupported_advanced_action("switch_tab")["error"])

    def close_tab(self, index: int | None = None) -> dict[str, Any]:
        raise NotImplementedError(self._unsupported_advanced_action("close_tab")["error"])

    def screenshot(
        self,
        path: str = "",
        *,
        selector: str | None = None,
        full_page: bool = False,
    ) -> dict[str, Any]:
        raise NotImplementedError(self._unsupported_advanced_action("screenshot")["error"])

    def list_frames(self) -> dict[str, Any]:
        raise NotImplementedError(self._unsupported_advanced_action("list_frames")["error"])

    def get_cookies(self, urls: list[str] | None = None) -> dict[str, Any]:
        raise NotImplementedError(self._unsupported_advanced_action("get_cookies")["error"])

    def add_cookies(self, cookies: list[dict[str, Any]]) -> dict[str, Any]:
        raise NotImplementedError(self._unsupported_advanced_action("add_cookies")["error"])

    def storage_state(self, path: str = "") -> dict[str, Any]:
        raise NotImplementedError(self._unsupported_advanced_action("storage_state")["error"])

    def start_tracing(
        self,
        *,
        screenshots: bool = True,
        snapshots: bool = True,
        sources: bool = True,
    ) -> dict[str, Any]:
        raise NotImplementedError(self._unsupported_advanced_action("start_tracing")["error"])

    def stop_tracing(self, path: str = "") -> dict[str, Any]:
        raise NotImplementedError(self._unsupported_advanced_action("stop_tracing")["error"])

    def get_network_events(self, *, limit: int = 100, clear: bool = False) -> dict[str, Any]:
        raise NotImplementedError(self._unsupported_advanced_action("network_events")["error"])

    def download(
        self,
        selector: str,
        *,
        path: str = "",
        wait_timeout: int | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError(self._unsupported_advanced_action("download")["error"])

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()


# ============================================================
# SeleniumFetcher
# ============================================================


class SeleniumFetcher(BrowserFetcher):
    """
    Fetcher based on Selenium WebDriver for full page rendering.

    Requires: ``pip install selenium``
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        browser: str = "auto",
        wait_timeout: int = 15,
        page_load_timeout: int = 30,
        max_content_length: int = 500_000,
        scroll_to_bottom: bool = False,
        scroll_pause: float = 1.0,
        max_scrolls: int = 5,
        extra_wait: float = 0.0,
        user_agent: str | None = None,
        window_size: tuple[int, int] = (1920, 1080),
        proxy: str | None = None,
        disable_images: bool = False,
        trust_env: bool = False,
    ):
        import threading

        self._headless = headless
        self._browser = browser.lower()
        self._wait_timeout = wait_timeout
        self._page_load_timeout = page_load_timeout
        self._max_content_length = max_content_length
        self._scroll_to_bottom = scroll_to_bottom
        self._scroll_pause = scroll_pause
        self._max_scrolls = max_scrolls
        self._extra_wait = extra_wait
        self._user_agent = user_agent
        self._window_size = window_size
        self._proxy = proxy
        self._disable_images = disable_images
        self._trust_env = trust_env

        self._driver: Any = None
        self._driver_error: str | None = None
        self._lock = threading.RLock()

    @staticmethod
    def _ensure_dependencies() -> None:
        from importlib.util import find_spec

        if find_spec("selenium") is None:
            msg = "Selenium is required for SeleniumFetcher. Install it with: pip install selenium"
            raise ImportError(msg)

    _AUTO_ORDER_WINDOWS: ClassVar[list[str]] = ["edge", "chrome", "firefox"]
    _AUTO_ORDER_OTHER: ClassVar[list[str]] = ["chrome", "firefox", "edge"]

    def _create_driver(self) -> Any:
        self._ensure_dependencies()

        _creators = {
            "chrome": self._create_chrome_driver,
            "firefox": self._create_firefox_driver,
            "edge": self._create_edge_driver,
        }

        if self._browser != "auto":
            if self._browser not in _creators:
                msg = f"Unsupported browser: {self._browser}. Use 'chrome', 'firefox', 'edge', or 'auto'."
                raise ValueError(msg)
            return _creators[self._browser]()

        import platform

        order = self._AUTO_ORDER_WINDOWS if platform.system() == "Windows" else self._AUTO_ORDER_OTHER

        last_error: Exception | None = None
        for name in order:
            try:
                driver = _creators[name]()
                logger.debug("Auto-detected browser: {}", name)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Browser {} unavailable: {}", name, exc)
                last_error = exc
            else:
                return driver

        msg = "No supported browser found. Install Chrome, Firefox, or Edge."
        raise RuntimeError(msg) from last_error

    def _create_chrome_driver(self) -> Any:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        options = Options()
        if self._headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--remote-allow-origins=*")
        options.add_argument(f"--window-size={self._window_size[0]},{self._window_size[1]}")
        options.add_argument("--disable-blink-features=AutomationControlled")

        if self._user_agent:
            options.add_argument(f"--user-agent={self._user_agent}")
        if self._proxy:
            options.add_argument(f"--proxy-server={self._proxy}")
        elif not self._trust_env:
            options.add_argument("--no-proxy-server")
        if self._disable_images:
            prefs = {"profile.managed_default_content_settings.images": 2}
            options.add_experimental_option("prefs", prefs)

        return webdriver.Chrome(service=Service(), options=options)

    def _create_firefox_driver(self) -> Any:
        from selenium import webdriver
        from selenium.webdriver.firefox.options import Options
        from selenium.webdriver.firefox.service import Service

        options = Options()
        if self._headless:
            options.add_argument("--headless")
        options.set_preference("general.useragent.override", self._user_agent or "")
        if self._proxy:
            from urllib.parse import urlparse

            parsed = urlparse(self._proxy)
            options.set_preference("network.proxy.type", 1)
            options.set_preference("network.proxy.http", parsed.hostname or "")
            options.set_preference("network.proxy.http_port", parsed.port or 8080)
        elif not self._trust_env:
            options.set_preference("network.proxy.type", 0)
        if self._disable_images:
            options.set_preference("permissions.default.image", 2)

        return webdriver.Firefox(service=Service(), options=options)

    def _create_edge_driver(self) -> Any:
        from selenium import webdriver
        from selenium.webdriver.edge.options import Options
        from selenium.webdriver.edge.service import Service

        options = Options()
        if self._headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--remote-allow-origins=*")
        options.add_argument(f"--window-size={self._window_size[0]},{self._window_size[1]}")
        options.add_argument("--disable-blink-features=AutomationControlled")

        if self._user_agent:
            options.add_argument(f"--user-agent={self._user_agent}")
        if self._proxy:
            options.add_argument(f"--proxy-server={self._proxy}")
        elif not self._trust_env:
            options.add_argument("--no-proxy-server")
        if self._disable_images:
            prefs = {"profile.managed_default_content_settings.images": 2}
            options.add_experimental_option("prefs", prefs)

        return webdriver.Edge(service=Service(), options=options)

    def _get_driver(self) -> Any:
        if self._driver_error is not None:
            raise RuntimeError(self._driver_error)
        if self._driver is None:
            try:
                self._driver = self._create_driver()
                self._driver.set_page_load_timeout(self._page_load_timeout)
            except Exception as exc:
                self._driver_error = f"Browser failed to start: {exc}"
                raise
        return self._driver

    def warm_up(self) -> bool:
        try:
            self._get_driver()
        except Exception:  # noqa: BLE001
            return False
        else:
            return True

    def _scroll_page(self, driver: Any) -> None:
        import time

        def _sleep_brief(duration: float) -> None:
            if duration > 0:
                time.sleep(duration)

        def _wait_for_height_stable(previous_height: int) -> int:
            max_wait = max(self._MIN_SCROLL_WAIT, min(self._scroll_pause, self._MAX_SCROLL_WAIT))
            poll_interval = max(self._MIN_SCROLL_WAIT, min(max_wait / 3, 0.15))
            stable_polls = 0
            last_seen = previous_height
            deadline = time.monotonic() + max_wait

            while time.monotonic() < deadline:
                _sleep_brief(poll_interval)
                new_height = driver.execute_script("return document.body.scrollHeight")
                if new_height == last_seen:
                    stable_polls += 1
                    if stable_polls >= self._SCROLL_STABILITY_POLLS:
                        return new_height
                else:
                    stable_polls = 0
                    last_seen = new_height
            return last_seen

        last_height = driver.execute_script("return document.body.scrollHeight")

        for _ in range(self._max_scrolls):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            new_height = _wait_for_height_stable(last_height)
            if new_height == last_height:
                break
            last_height = new_height

    def _wait_for_document_ready(self, driver: Any, timeout: int | None = None) -> None:
        from selenium.webdriver.support.ui import WebDriverWait

        ready_timeout = timeout or self._wait_timeout
        with contextlib.suppress(Exception):
            WebDriverWait(driver, ready_timeout).until(
                lambda d: d.execute_script("return document.readyState") in {"interactive", "complete"}
            )

    @staticmethod
    def _webdriver_exception_type() -> type[Exception]:
        try:
            from selenium.common.exceptions import WebDriverException
        except Exception:  # noqa: BLE001

            class WebDriverException(Exception):
                """Fallback when selenium is not importable."""

            return WebDriverException
        return WebDriverException

    def fetch(self, url: str, *, quick: bool = False) -> dict[str, Any]:
        import time

        WebDriverException = self._webdriver_exception_type()

        with self._lock:
            result = _empty_result(url)

            try:
                driver = self._get_driver()

                if quick:
                    driver.set_page_load_timeout(min(self._page_load_timeout, self._QUICK_PAGE_LOAD_TIMEOUT))

                nav_ok = True
                try:
                    driver.get(url)
                except WebDriverException as exc:
                    nav_ok = False
                    result["error"] = f"Navigation error: {exc}"
                    logger.debug("SeleniumFetcher: navigation to {} failed: {}", url, exc)
                finally:
                    if quick:
                        driver.set_page_load_timeout(self._page_load_timeout)

                if not nav_ok:
                    current_url = driver.current_url or ""
                    if (
                        not current_url
                        or current_url.startswith(("about:", "data:"))
                        or normalize_url(current_url) != normalize_url(url)
                    ):
                        return result

                wait = min(self._extra_wait, self._QUICK_EXTRA_WAIT) if quick else self._extra_wait
                if wait > 0:
                    time.sleep(wait)

                if self._scroll_to_bottom and not quick:
                    self._scroll_page(driver)

                result["title"] = driver.title or ""

                _, content = SimpleHTMLParser.extract_text(
                    driver.page_source,
                    max_length=self._max_content_length,
                )
                result["content"] = content

                if self._is_browser_error_page(content, result["title"]):
                    result["error"] = f"Navigation failed: {result['title'] or url}"
                    result["content"] = ""
                else:
                    result["success"] = True

            except Exception as e:  # noqa: BLE001
                error_type = type(e).__name__
                result["error"] = f"Selenium error ({error_type}): {e}"
                logger.debug("SeleniumFetcher error for {}: {}", url, result["error"])

            return result

    _BROWSER_ERROR_PATTERNS: ClassVar[tuple[str, ...]] = (
        "server ip address could not be found",
        "err_name_not_resolved",
        "err_connection_refused",
        "err_connection_timed_out",
        "this site can't be reached",
        "this page can't be displayed",
        "dns_probe_finished_nxdomain",
        "unable to connect to the internet",
        "name not resolved",
        "net::err_",
        "hmm. we're having trouble finding that site",
        "can't reach this page",
    )

    @classmethod
    def _is_browser_error_page(cls, content: str, title: str) -> bool:
        haystack = (content + " " + title).lower()
        return any(pattern in haystack for pattern in cls._BROWSER_ERROR_PATTERNS)

    def fetch_with_wait(
        self,
        url: str,
        wait_for_selector: str | None = None,
        wait_timeout: int | None = None,
    ) -> dict[str, Any]:
        import time

        with self._lock:
            result = _empty_result(url)

            try:
                driver = self._get_driver()
                driver.get(url)

                if wait_for_selector:
                    from selenium.webdriver.common.by import By
                    from selenium.webdriver.support import expected_conditions
                    from selenium.webdriver.support.ui import WebDriverWait

                    timeout = wait_timeout or self._wait_timeout
                    WebDriverWait(driver, timeout).until(
                        expected_conditions.presence_of_element_located((By.CSS_SELECTOR, wait_for_selector))
                    )
                elif self._extra_wait > 0:
                    time.sleep(self._extra_wait)

                if self._scroll_to_bottom:
                    self._scroll_page(driver)

                result["title"] = driver.title or ""

                _, content = SimpleHTMLParser.extract_text(
                    driver.page_source,
                    max_length=self._max_content_length,
                )
                result["content"] = content
                result["success"] = True

            except Exception as e:  # noqa: BLE001
                error_type = type(e).__name__
                result["error"] = f"Selenium error ({error_type}): {e}"
                logger.debug("SeleniumFetcher.fetch_with_wait error for {}: {}", url, result["error"])

            return result

    def click_element(self, selector: str, wait_timeout: int | None = None) -> dict[str, Any]:
        with self._lock:
            result = _empty_result(clicked_text="")

            try:
                from selenium.webdriver.common.by import By
                from selenium.webdriver.support import expected_conditions
                from selenium.webdriver.support.ui import WebDriverWait

                driver = self._get_driver()
                timeout = wait_timeout or self._wait_timeout

                element = WebDriverWait(driver, timeout).until(
                    expected_conditions.element_to_be_clickable((By.CSS_SELECTOR, selector))
                )
                result["clicked_text"] = element.text or element.get_attribute("textContent") or ""
                element.click()
                self._wait_for_document_ready(driver, timeout=timeout)

                result["url"] = driver.current_url
                result["title"] = driver.title or ""
                result["success"] = True

            except Exception as e:  # noqa: BLE001
                result["error"] = f"Click error ({type(e).__name__}): {e}"

            return result

    def fill_input(
        self,
        selector: str,
        value: str,
        *,
        submit: bool = False,
        clear_first: bool = True,
        wait_timeout: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            result = _empty_result()

            try:
                from selenium.webdriver.common.by import By
                from selenium.webdriver.common.keys import Keys
                from selenium.webdriver.support import expected_conditions
                from selenium.webdriver.support.ui import WebDriverWait

                driver = self._get_driver()
                timeout = wait_timeout or self._wait_timeout

                element = WebDriverWait(driver, timeout).until(
                    expected_conditions.presence_of_element_located((By.CSS_SELECTOR, selector))
                )

                if clear_first:
                    element.clear()

                element.send_keys(value)

                if submit:
                    element.send_keys(Keys.RETURN)
                    self._wait_for_document_ready(driver, timeout=timeout)

                result["url"] = driver.current_url
                result["title"] = driver.title or ""
                result["success"] = True

            except Exception as e:  # noqa: BLE001
                result["error"] = f"Fill error ({type(e).__name__}): {e}"

            return result

    def extract_links(
        self,
        selector: str = "a[href]",
        *,
        base_url_filter: str | None = None,
        max_links: int = 50,
    ) -> dict[str, Any]:
        with self._lock:
            result = _empty_result(links=[], count=0)

            try:
                from selenium.webdriver.common.by import By

                driver = self._get_driver()
                result["url"] = driver.current_url

                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                links: list[dict[str, str]] = []

                for elem in elements:
                    href = elem.get_attribute("href") or ""
                    if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
                        continue

                    if base_url_filter and not href.startswith(base_url_filter):
                        continue

                    links.append(
                        {
                            "url": href,
                            "text": (elem.text or "").strip()[:200],
                            "title": (elem.get_attribute("title") or "").strip()[:200],
                        }
                    )

                    if len(links) >= max_links:
                        break

                result["links"] = links
                result["count"] = len(links)
                result["success"] = True

            except Exception as e:  # noqa: BLE001
                result["error"] = f"Extract links error ({type(e).__name__}): {e}"

            return result

    def execute_js(self, script: str) -> dict[str, Any]:
        with self._lock:
            result = _empty_result(return_value=None)

            try:
                driver = self._get_driver()
                return_value = driver.execute_script(script)
                result["url"] = driver.current_url
                result["return_value"] = str(return_value) if return_value is not None else None
                result["success"] = True

            except Exception as e:  # noqa: BLE001
                result["error"] = f"JS execution error ({type(e).__name__}): {e}"

            return result

    def get_current_url(self) -> str:
        with self._lock:
            try:
                driver = self._get_driver()
            except Exception:  # noqa: BLE001
                return ""
            else:
                return driver.current_url

    def get_page_content(self) -> dict[str, Any]:
        with self._lock:
            result = _empty_result()

            try:
                driver = self._get_driver()
                result["url"] = driver.current_url
                result["title"] = driver.title or ""

                _, content = SimpleHTMLParser.extract_text(
                    driver.page_source,
                    max_length=self._max_content_length,
                )
                result["content"] = content
                result["success"] = True

            except Exception as e:  # noqa: BLE001
                result["error"] = f"Get content error ({type(e).__name__}): {e}"

            return result

    def crawl(
        self,
        start_url: str,
        *,
        max_pages: int = 10,
        max_depth: int = 2,
        url_filter: str | None = None,
        link_selector: str = "a[href]",
        extract_content: bool = True,
    ) -> dict[str, Any]:
        import time
        from urllib.parse import urlparse

        with self._lock:
            result = _empty_result(pages=[], total_pages=0)

            if url_filter is None:
                parsed = urlparse(start_url)
                url_filter = f"{parsed.scheme}://{parsed.netloc}"

            visited: set[str] = set()
            queue: deque[tuple[str, int]] = deque([(start_url, 0)])
            pages: list[dict[str, Any]] = []

            try:
                while queue and len(pages) < max_pages:
                    current_url, depth = queue.popleft()

                    current_url = current_url.split("#")[0].rstrip("/")
                    if current_url in visited:
                        continue
                    visited.add(current_url)

                    fetch_result = self.fetch(current_url)
                    if not fetch_result["success"]:
                        continue

                    page_info: dict[str, Any] = {
                        "url": current_url,
                        "title": fetch_result["title"],
                        "depth": depth,
                        "links_found": 0,
                    }

                    if extract_content:
                        page_info["content"] = fetch_result["content"]

                    if depth < max_depth:
                        links_result = self.extract_links(
                            selector=link_selector,
                            base_url_filter=url_filter,
                            max_links=50,
                        )
                        if links_result["success"]:
                            page_info["links_found"] = links_result["count"]
                            for link in links_result["links"]:
                                link_url = link["url"].split("#")[0].rstrip("/")
                                if link_url not in visited and len(queue) < max_pages * 2:
                                    queue.append((link_url, depth + 1))

                    pages.append(page_info)
                    time.sleep(self._CRAWL_POLITENESS_DELAY)

                result["pages"] = pages
                result["total_pages"] = len(pages)
                result["success"] = True

            except Exception as e:  # noqa: BLE001
                result["error"] = f"Crawl error ({type(e).__name__}): {e}"
                result["pages"] = pages
                result["total_pages"] = len(pages)

            return result

    def close(self) -> None:
        with self._lock:
            if self._driver is not None:
                with contextlib.suppress(Exception):
                    self._driver.quit()
                self._driver = None


# ============================================================
# PlaywrightFetcher
# ============================================================


class _PlaywrightWorkerError(Exception):
    """Wraps any exception raised inside the Playwright worker thread."""


class PlaywrightFetcher(BrowserFetcher):
    """
    Fetcher based on Playwright for full page rendering.

    All Playwright calls run inside a dedicated worker thread that
    owns a fresh ``asyncio`` event loop to avoid ``nest_asyncio`` /
    Windows ``ProactorEventLoop`` issues.

    Supports richer session-level browser features than Selenium:
    multi-tab browsing, screenshots, downloads, tracing, network event
    capture, cookies, and persisted storage state/HAR recording via
    constructor options.

    Requires: ``pip install playwright`` then ``playwright install``
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        browser: str = "chromium",
        wait_timeout: int = 15,
        page_load_timeout: int = 30,
        max_content_length: int = 500_000,
        scroll_to_bottom: bool = False,
        scroll_pause: float = 1.0,
        max_scrolls: int = 5,
        extra_wait: float = 0.0,
        user_agent: str | None = None,
        window_size: tuple[int, int] = (1920, 1080),
        proxy: str | None = None,
        disable_images: bool = False,
        trust_env: bool = False,
        har_path: str | None = None,
        storage_state_path: str | None = None,
        storage_state: dict[str, Any] | None = None,
    ):
        self._headless = headless
        self._browser_type = browser.lower()
        self._wait_timeout = wait_timeout
        self._page_load_timeout = page_load_timeout
        self._max_content_length = max_content_length
        self._scroll_to_bottom = scroll_to_bottom
        self._scroll_pause = scroll_pause
        self._max_scrolls = max_scrolls
        self._extra_wait = extra_wait
        self._user_agent = user_agent
        self._window_size = window_size
        self._proxy = proxy
        self._disable_images = disable_images
        self._trust_env = trust_env
        self._har_path = har_path
        self._storage_state_path = storage_state_path
        self._storage_state = storage_state

        self._playwright: Any = None
        self._browser_instance: Any = None
        self._context: Any = None
        self._page: Any = None
        self._pages: list[Any] = []
        self._active_page_index = 0
        self._network_events: deque[dict[str, Any]] = deque(maxlen=1000)
        self._tracing_active = False
        self._startup_error: str | None = None

        import queue as _queue_mod
        import threading

        self._task_queue: _queue_mod.Queue[tuple[Any, tuple[Any, ...], dict[str, Any], _queue_mod.Queue[Any]]] = (
            _queue_mod.Queue()
        )
        self._worker: threading.Thread | None = None
        self._worker_ready = threading.Event()
        self._closed = False

    # ------------------------------------------------------------------
    # Worker thread
    # ------------------------------------------------------------------

    def _start_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        import threading

        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()
        self._worker_ready.wait(timeout=60)
        if self._startup_error is not None:
            raise RuntimeError(self._startup_error)

    @staticmethod
    def _get_clean_loop_factory() -> Any:
        """
        Return a pristine event-loop factory for the worker thread.

        This strips the ``nest_asyncio`` monkey-patches from the loop
        implementation that Playwright sync API will instantiate via
        ``asyncio.new_event_loop()``.
        """
        import asyncio
        import sys

        if sys.platform == "win32":
            _loop_base = asyncio.ProactorEventLoop
        else:
            _loop_base = asyncio.SelectorEventLoop

        _clean_loop_cls: type[Any]
        if not getattr(_loop_base, "_nest_patched", False):
            _clean_loop_cls = _loop_base
        else:
            _base_loop = asyncio.BaseEventLoop
            restored: dict[str, object] = {}
            for attr in ("run_until_complete", "run_forever", "_run_once", "_check_running"):
                if attr in _base_loop.__dict__:
                    restored[attr] = _base_loop.__dict__[attr]

            # On Windows, BaseEventLoop._run_once lacks IOCP/subprocess support.
            # Walk the MRO between _loop_base and BaseEventLoop to find an
            # unpatched override (e.g. BaseProactorEventLoop._run_once) that
            # preserves IOCP so that subprocess transport works correctly.
            for _cls in _loop_base.__mro__[1:]:
                if _cls is _base_loop:
                    break
                if "_run_once" in _cls.__dict__:
                    restored["_run_once"] = _cls.__dict__["_run_once"]
                    break

            _clean_loop_cls = type("_CleanLoop", (_loop_base,), restored)

        return _clean_loop_cls

    @classmethod
    @contextlib.contextmanager
    def _patch_asyncio_new_event_loop(cls) -> Any:
        """
        Temporarily route ``asyncio.new_event_loop()`` to an unpatched loop.

        Playwright sync API creates its own loop inside ``start()``. In
        notebook environments, ``nest_asyncio`` often monkey-patches the
        default loop class in a way that breaks subprocess support on
        Windows. Replacing only ``asyncio.new_event_loop`` during startup
        avoids global event-loop policy changes and keeps the workaround
        narrowly scoped.
        """
        import asyncio

        original_new_event_loop = asyncio.new_event_loop
        clean_loop_factory = cls._get_clean_loop_factory()
        asyncio.new_event_loop = clean_loop_factory
        try:
            yield
        finally:
            asyncio.new_event_loop = original_new_event_loop

    def _worker_loop(self) -> None:
        import queue as _queue_mod

        try:
            from playwright.sync_api import Error

            _pw_error_cls: type[Exception] = Error
        except ImportError:
            _pw_error_cls = OSError

        try:
            self._launch_browser()
        except (ImportError, RuntimeError, OSError, NotImplementedError, _pw_error_cls) as exc:
            self._startup_error = f"Playwright browser failed to start: {exc}"
            self._worker_ready.set()
            return

        self._worker_ready.set()

        _worker_exc_types = (
            RuntimeError,
            ValueError,
            TypeError,
            AttributeError,
            OSError,
            TimeoutError,
            _pw_error_cls,
        )

        while True:
            try:
                item = self._task_queue.get(timeout=1)
            except _queue_mod.Empty:
                if self._closed:
                    break
                continue

            fn, args, kwargs, result_q = item
            if fn is None:
                result_q.put(None)
                break
            try:
                rv = fn(*args, **kwargs)
                result_q.put(("ok", rv))
            except _worker_exc_types as exc:
                result_q.put(("err", exc))

        self._close_browser()

    _WORKER_CALL_TIMEOUT: ClassVar[int] = 120

    def _run_in_worker(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        import queue as _queue_mod

        if self._closed:
            msg = "PlaywrightFetcher is closed"
            raise RuntimeError(msg)
        if self._startup_error is not None:
            raise RuntimeError(self._startup_error)
        self._start_worker()

        result_q: _queue_mod.Queue[Any] = _queue_mod.Queue()
        self._task_queue.put((fn, args, kwargs, result_q))

        try:
            item = result_q.get(timeout=self._WORKER_CALL_TIMEOUT)
        except _queue_mod.Empty:
            logger.exception(
                "PlaywrightFetcher: worker did not respond within {} s — forcing shutdown",
                self._WORKER_CALL_TIMEOUT,
            )
            self._force_close()
            msg = f"Playwright worker timed out after {self._WORKER_CALL_TIMEOUT}s"
            raise TimeoutError(msg) from None

        if item is None:
            msg = "Playwright worker shut down unexpectedly"
            raise RuntimeError(msg)

        tag, payload = item
        if tag == "err":
            msg = f"{type(payload).__name__}: {payload}"
            raise _PlaywrightWorkerError(msg) from payload
        return payload

    # ------------------------------------------------------------------
    # Playwright lifecycle (runs inside _worker thread)
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_dependencies() -> None:
        from importlib.util import find_spec

        if find_spec("playwright") is None:
            msg = (
                "Playwright is required for PlaywrightFetcher. "
                "Install it with: pip install playwright && playwright install"
            )
            raise ImportError(msg)

    def _launch_browser(self) -> None:
        self._ensure_dependencies()
        from playwright.sync_api import sync_playwright

        with self._patch_asyncio_new_event_loop():
            self._playwright = sync_playwright().start()

        browser_types = {
            "chromium": self._playwright.chromium,
            "firefox": self._playwright.firefox,
            "webkit": self._playwright.webkit,
        }
        if self._browser_type not in browser_types:
            msg = f"Unsupported browser: {self._browser_type}. Use 'chromium', 'firefox', or 'webkit'."
            raise ValueError(msg)

        launcher = browser_types[self._browser_type]

        launch_kwargs: dict[str, Any] = {"headless": self._headless}
        if self._proxy:
            launch_kwargs["proxy"] = {"server": self._proxy}

        if not self._proxy and not self._trust_env:
            if self._browser_type in ("chromium", "chrome"):
                launch_kwargs.setdefault("args", [])
                launch_kwargs["args"].append("--no-proxy-server")
            elif self._browser_type == "firefox":
                launch_kwargs.setdefault("firefox_user_prefs", {})
                launch_kwargs["firefox_user_prefs"]["network.proxy.type"] = 0

        self._browser_instance = launcher.launch(**launch_kwargs)

        context_kwargs = self._build_context_kwargs_impl()

        self._context = self._browser_instance.new_context(**context_kwargs)
        self._pages = []
        self._active_page_index = 0
        self._network_events.clear()
        self._tracing_active = False

        if self._disable_images:
            self._context.route("**/*.{png,jpg,jpeg,gif,svg,webp,ico}", lambda route: route.abort())

        self._page = self._new_page_impl(make_active=True)

    def _build_context_kwargs_impl(self) -> dict[str, Any]:
        if self._storage_state_path and self._storage_state is not None:
            msg = "Use either storage_state_path or storage_state, not both."
            raise ValueError(msg)

        context_kwargs: dict[str, Any] = {
            "viewport": {"width": self._window_size[0], "height": self._window_size[1]},
            "accept_downloads": True,
        }
        if self._user_agent:
            context_kwargs["user_agent"] = self._user_agent
        if self._har_path:
            har_path = Path(self._har_path).resolve()
            har_path.parent.mkdir(parents=True, exist_ok=True)
            context_kwargs["record_har_path"] = str(har_path)
        if self._storage_state_path:
            storage_path = Path(self._storage_state_path).resolve()
            if not storage_path.is_file():
                msg = f"Storage state file not found: {storage_path}"
                raise FileNotFoundError(msg)
            context_kwargs["storage_state"] = str(storage_path)
        elif self._storage_state is not None:
            context_kwargs["storage_state"] = self._storage_state

        return context_kwargs

    def _close_browser(self) -> None:
        self._pages.clear()
        self._page = None
        if self._context is not None:
            with contextlib.suppress(Exception):
                self._context.close()
            self._context = None
        if self._browser_instance is not None:
            with contextlib.suppress(Exception):
                self._browser_instance.close()
            self._browser_instance = None
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                self._playwright.stop()
            self._playwright = None

    # ------------------------------------------------------------------
    # Internal helpers (executed inside worker)
    # ------------------------------------------------------------------

    def _scroll_page_impl(self) -> None:
        page = self._page
        last_height = page.evaluate("document.body.scrollHeight")

        for _ in range(self._max_scrolls):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            new_height = self._wait_for_height_stable_impl(last_height)
            if new_height == last_height:
                break
            last_height = new_height

    def _wait_for_page_delay_impl(self, seconds: float) -> None:
        if seconds <= 0:
            return
        page = self._page
        timeout_ms = max(0, int(seconds * 1000))
        if timeout_ms <= 0:
            return
        wait_for_timeout = getattr(page, "wait_for_timeout", None)
        if callable(wait_for_timeout):
            with contextlib.suppress(Exception):
                wait_for_timeout(timeout_ms)
                return

        import time

        time.sleep(seconds)

    def _wait_for_height_stable_impl(self, previous_height: int) -> int:
        page = self._page
        max_wait = max(self._MIN_SCROLL_WAIT, min(self._scroll_pause, self._MAX_SCROLL_WAIT))
        poll_interval = max(self._MIN_SCROLL_WAIT, min(max_wait / 3, 0.15))
        stable_polls = 0
        last_seen = previous_height

        while True:
            self._wait_for_page_delay_impl(poll_interval)
            new_height = page.evaluate("document.body.scrollHeight")
            if new_height == last_seen:
                stable_polls += 1
                if stable_polls >= self._SCROLL_STABILITY_POLLS:
                    return new_height
            else:
                stable_polls = 0
                last_seen = new_height

            max_wait -= poll_interval
            if max_wait <= 0:
                return last_seen

    def _wait_for_locator_impl(self, selector: str, timeout_ms: int) -> Any:
        page = self._ensure_active_page_impl()
        locator = page.locator(selector).first
        locator.wait_for(state="attached", timeout=timeout_ms)
        return locator

    def _wait_for_load_after_action_impl(self, timeout_ms: int) -> None:
        try:
            from playwright.sync_api import Error as PlaywrightError
        except ImportError:
            suppress_errors: tuple[type[BaseException], ...] = (RuntimeError, OSError)
        else:
            suppress_errors = (PlaywrightError, OSError)

        wait_timeout = min(timeout_ms, self._page_load_timeout * 1000)
        if wait_timeout <= 0:
            return

        with contextlib.suppress(*suppress_errors):
            self._page.wait_for_load_state("domcontentloaded", timeout=wait_timeout)

    @staticmethod
    def _normalize_text_payload(text: str) -> str:
        return (text or "").replace("\xa0", " ").replace("\r", "\n").replace("\t", " ").strip()

    def _extract_page_payload_impl(self, page: Any, *, max_length: int) -> dict[str, str]:
        raw_payload = page.evaluate(
            """
            () => {
                const primary = document.querySelector("main, article, div[role='main']");
                const body = document.body || document.documentElement;
                const clean = (value) => (value || "")
                    .replace(/\\u00a0/g, " ")
                    .replace(/\\r/g, "\\n")
                    .replace(/[ \\t]+/g, " ")
                    .replace(/\\n{3,}/g, "\\n\\n")
                    .trim();

                return {
                    title: document.title || "",
                    primaryText: primary ? clean(primary.innerText || primary.textContent || "") : "",
                    bodyText: body ? clean(body.innerText || body.textContent || "") : "",
                    usedMain: Boolean(primary)
                };
            }
            """
        )
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        title = str(payload.get("title", "") or "")
        primary_text = self._normalize_text_payload(str(payload.get("primaryText", "") or ""))
        body_text = self._normalize_text_payload(str(payload.get("bodyText", "") or ""))
        used_main = bool(payload.get("usedMain"))
        text = primary_text

        if (not used_main or len(primary_text) < self.MIN_FALLBACK_CONTENT) and len(body_text) > len(text):
            text = body_text

        if len(text) < self.MIN_FALLBACK_CONTENT or (used_main and len(body_text) > len(text) * 2):
            _, parsed_text = SimpleHTMLParser.extract_text(page.content(), max_length=max_length)
            if len(parsed_text) > len(text):
                text = parsed_text

        if len(text) > max_length:
            text = text[:max_length] + "\n\n... (content truncated)"

        return {"title": title, "content": text}

    @staticmethod
    def _resolve_output_path(path: str, *, suffix: str, default_name: str | None = None) -> str:
        import tempfile

        if path:
            resolved = Path(path).resolve()
        elif default_name:
            resolved = Path(default_name).resolve()
        else:
            fd, temp_path = tempfile.mkstemp(suffix=suffix)
            os.close(fd)
            resolved = Path(temp_path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return str(resolved)

    @staticmethod
    def _is_page_open(page: Any) -> bool:
        is_closed = getattr(page, "is_closed", None)
        if callable(is_closed):
            with contextlib.suppress(Exception):
                closed = is_closed()
                if isinstance(closed, bool):
                    return not closed
        return True

    def _append_network_event_impl(self, event: dict[str, Any]) -> None:
        self._network_events.append(event)

    def _register_page_listeners_impl(self, page: Any) -> None:
        def _on_request(request: Any) -> None:
            self._append_network_event_impl(
                {
                    "type": "request",
                    "url": request.url,
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "page_index": self._pages.index(page) if page in self._pages else -1,
                }
            )

        def _on_response(response: Any) -> None:
            with contextlib.suppress(Exception):
                request = response.request
                self._append_network_event_impl(
                    {
                        "type": "response",
                        "url": response.url,
                        "status": response.status,
                        "method": request.method,
                        "resource_type": request.resource_type,
                        "page_index": self._pages.index(page) if page in self._pages else -1,
                    }
                )

        def _on_request_failed(request: Any) -> None:
            failure_text = ""
            with contextlib.suppress(Exception):
                failure = request.failure
                if isinstance(failure, dict):
                    failure_text = str(failure.get("errorText", ""))
                elif failure is not None:
                    failure_text = str(failure)
            self._append_network_event_impl(
                {
                    "type": "request_failed",
                    "url": request.url,
                    "method": request.method,
                    "resource_type": request.resource_type,
                    "failure": failure_text,
                    "page_index": self._pages.index(page) if page in self._pages else -1,
                }
            )

        page.on("request", _on_request)
        page.on("response", _on_response)
        page.on("requestfailed", _on_request_failed)

    def _new_page_impl(self, *, make_active: bool = True) -> Any:
        if self._context is None:
            msg = "Playwright browser context is not available"
            raise RuntimeError(msg)

        page = self._context.new_page()
        self._register_page_listeners_impl(page)
        page.set_default_timeout(self._wait_timeout * 1000)
        page.set_default_navigation_timeout(self._page_load_timeout * 1000)
        self._pages.append(page)

        if make_active or self._page is None:
            self._page = page
            self._active_page_index = len(self._pages) - 1

        return page

    def _sync_pages_impl(self) -> list[Any]:
        current_page = self._page
        self._pages = [page for page in self._pages if self._is_page_open(page)]

        if not self._pages:
            self._page = None
            self._active_page_index = 0
            return []

        if current_page in self._pages:
            self._active_page_index = self._pages.index(current_page)
            self._page = current_page
        else:
            self._active_page_index = min(self._active_page_index, len(self._pages) - 1)
            self._page = self._pages[self._active_page_index]

        return self._pages

    def _ensure_active_page_impl(self) -> Any:
        pages = self._sync_pages_impl()
        if not pages:
            return self._new_page_impl(make_active=True)
        return self._page

    def _set_active_page_impl(self, index: int) -> Any:
        pages = self._sync_pages_impl()
        if not pages:
            return self._new_page_impl(make_active=True)
        if index < 0 or index >= len(pages):
            msg = f"Invalid tab index: {index}"
            raise IndexError(msg)
        self._active_page_index = index
        self._page = pages[index]
        return self._page

    def _snapshot_page_impl(self, page: Any, index: int) -> dict[str, Any]:
        title = ""
        with contextlib.suppress(Exception):
            title = page.title() or ""
        url = ""
        with contextlib.suppress(Exception):
            url = page.url or ""
        return {
            "index": index,
            "url": url,
            "title": title,
            "active": page is self._page,
        }

    def _fetch_impl(self, url: str, *, quick: bool = False) -> dict[str, Any]:
        try:
            from playwright.sync_api import Error as PlaywrightError
        except ImportError:
            playwright_error_cls: type[Exception] = RuntimeError
        else:
            playwright_error_cls = PlaywrightError

        result = _empty_result(url)
        page = self._ensure_active_page_impl()

        timeout_ms = (
            min(self._page_load_timeout, self._QUICK_PAGE_LOAD_TIMEOUT) * 1000
            if quick
            else self._page_load_timeout * 1000
        )

        nav_ok = True
        try:
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        except (playwright_error_cls, OSError) as exc:
            nav_ok = False
            result["error"] = f"Navigation error: {exc}"
            logger.debug("PlaywrightFetcher: navigation to {} failed: {}", url, exc)

        if not nav_ok:
            current_url = page.url or ""
            if (
                not current_url
                or current_url.startswith(("about:", "data:"))
                or normalize_url(current_url) != normalize_url(url)
            ):
                return result

        wait = min(self._extra_wait, self._QUICK_EXTRA_WAIT) if quick else self._extra_wait
        if wait > 0:
            self._wait_for_page_delay_impl(wait)

        if self._scroll_to_bottom and not quick:
            self._scroll_page_impl()

        try:
            payload = self._extract_page_payload_impl(page, max_length=self._max_content_length)
            result["title"] = payload["title"]
            result["content"] = payload["content"]
            result["success"] = bool(result["content"])
        except (playwright_error_cls, OSError, AttributeError) as exc:
            result["error"] = f"Content extraction error: {exc}"
            logger.debug("PlaywrightFetcher: content extraction failed for {}: {}", url, exc)

        return result

    def _fetch_with_wait_impl(
        self,
        url: str,
        wait_for_selector: str | None = None,
        wait_timeout: int | None = None,
    ) -> dict[str, Any]:
        result = _empty_result(url)
        page = self._ensure_active_page_impl()

        page.goto(url, wait_until="domcontentloaded")

        if wait_for_selector:
            timeout_ms = (wait_timeout or self._wait_timeout) * 1000
            self._wait_for_locator_impl(wait_for_selector, timeout_ms)
        elif self._extra_wait > 0:
            self._wait_for_page_delay_impl(self._extra_wait)

        if self._scroll_to_bottom:
            self._scroll_page_impl()

        payload = self._extract_page_payload_impl(page, max_length=self._max_content_length)
        result["title"] = payload["title"]
        result["content"] = payload["content"]
        result["success"] = True
        return result

    def _click_impl(self, selector: str, wait_timeout: int | None = None) -> dict[str, Any]:
        result = _empty_result(clicked_text="")
        page = self._ensure_active_page_impl()
        timeout_ms = (wait_timeout or self._wait_timeout) * 1000

        locator = self._wait_for_locator_impl(selector, timeout_ms)
        result["clicked_text"] = (locator.text_content(timeout=timeout_ms) or "").strip()
        locator.click(timeout=timeout_ms)
        self._wait_for_load_after_action_impl(timeout_ms)

        result["url"] = page.url
        result["title"] = page.title() or ""
        result["success"] = True
        return result

    def _fill_impl(
        self,
        selector: str,
        value: str,
        *,
        submit: bool = False,
        clear_first: bool = True,
        wait_timeout: int | None = None,
    ) -> dict[str, Any]:
        result = _empty_result()
        page = self._ensure_active_page_impl()
        timeout_ms = (wait_timeout or self._wait_timeout) * 1000

        locator = self._wait_for_locator_impl(selector, timeout_ms)

        if clear_first:
            locator.clear(timeout=timeout_ms)

        locator.fill(value, timeout=timeout_ms)

        if submit:
            locator.press("Enter", timeout=timeout_ms)
            self._wait_for_load_after_action_impl(timeout_ms)

        result["url"] = page.url
        result["title"] = page.title() or ""
        result["success"] = True
        return result

    def _extract_links_impl(
        self,
        selector: str = "a[href]",
        *,
        base_url_filter: str | None = None,
        max_links: int = 50,
    ) -> dict[str, Any]:
        result = _empty_result(links=[], count=0)
        page = self._ensure_active_page_impl()
        result["url"] = page.url

        elements = page.locator(selector)
        total = elements.count()
        links: list[dict[str, str]] = []

        for idx in range(total):
            elem = elements.nth(idx)
            href = elem.get_attribute("href") or ""
            if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
                continue

            if base_url_filter and not href.startswith(base_url_filter):
                continue

            links.append(
                {
                    "url": href,
                    "text": ((elem.text_content() or "").strip())[:200],
                    "title": (elem.get_attribute("title") or "").strip()[:200],
                }
            )

            if len(links) >= max_links:
                break

        result["links"] = links
        result["count"] = len(links)
        result["success"] = True
        return result

    def _execute_js_impl(self, script: str) -> dict[str, Any]:
        result = _empty_result(return_value=None)
        page = self._ensure_active_page_impl()
        return_value = page.evaluate(script)
        result["url"] = page.url
        result["return_value"] = str(return_value) if return_value is not None else None
        result["success"] = True
        return result

    def _get_current_url_impl(self) -> str:
        page = self._ensure_active_page_impl()
        return page.url if page else ""

    def _get_page_content_impl(self) -> dict[str, Any]:
        result = _empty_result()
        page = self._ensure_active_page_impl()
        result["url"] = page.url
        payload = self._extract_page_payload_impl(page, max_length=self._max_content_length)
        result["title"] = payload["title"]
        result["content"] = payload["content"]
        result["success"] = True
        return result

    def _list_tabs_impl(self) -> dict[str, Any]:
        pages = self._sync_pages_impl()
        if not pages:
            pages = [self._new_page_impl(make_active=True)]
        tabs = [self._snapshot_page_impl(page, idx) for idx, page in enumerate(pages)]
        return {"success": True, "tabs": tabs, "count": len(tabs)}

    def _open_tab_impl(
        self,
        url: str = "",
        *,
        wait_for_selector: str | None = None,
        background: bool = False,
    ) -> dict[str, Any]:
        page = self._new_page_impl(make_active=not background)
        timeout_ms = self._wait_timeout * 1000

        if url:
            page.goto(url, timeout=self._page_load_timeout * 1000, wait_until="domcontentloaded")
            if wait_for_selector:
                page.locator(wait_for_selector).first.wait_for(state="attached", timeout=timeout_ms)
            elif self._extra_wait > 0:
                self._wait_for_page_delay_impl(self._extra_wait)

        self._sync_pages_impl()
        index = self._pages.index(page)
        if not background:
            self._set_active_page_impl(index)

        result = self._snapshot_page_impl(page, index)
        result["success"] = True
        return result

    def _switch_tab_impl(self, index: int) -> dict[str, Any]:
        page = self._set_active_page_impl(index)
        result = self._snapshot_page_impl(page, index)
        result["success"] = True
        return result

    def _close_tab_impl(self, index: int | None = None) -> dict[str, Any]:
        pages = self._sync_pages_impl()
        if not pages:
            page = self._new_page_impl(make_active=True)
            result = self._snapshot_page_impl(page, 0)
            result["success"] = True
            result["closed_index"] = None
            return result

        close_index = self._active_page_index if index is None else index
        if close_index < 0 or close_index >= len(pages):
            msg = f"Invalid tab index: {close_index}"
            raise IndexError(msg)

        page = pages[close_index]
        with contextlib.suppress(Exception):
            page.close()
        self._sync_pages_impl()

        if not self._pages:
            page = self._new_page_impl(make_active=True)
            active_index = 0
        else:
            active_index = min(close_index, len(self._pages) - 1)
            page = self._set_active_page_impl(active_index)

        result = self._snapshot_page_impl(page, active_index)
        result["success"] = True
        result["closed_index"] = close_index
        return result

    def _screenshot_impl(
        self,
        path: str = "",
        *,
        selector: str | None = None,
        full_page: bool = False,
    ) -> dict[str, Any]:
        page = self._ensure_active_page_impl()
        output_path = self._resolve_output_path(path, suffix=".png")

        if selector:
            locator = self._wait_for_locator_impl(selector, self._wait_timeout * 1000)
            locator.screenshot(path=output_path)
        else:
            page.screenshot(path=output_path, full_page=full_page)

        return {
            "success": True,
            "path": output_path,
            "url": page.url,
            "title": page.title() or "",
        }

    def _list_frames_impl(self) -> dict[str, Any]:
        page = self._ensure_active_page_impl()
        frames: list[dict[str, Any]] = []

        for index, frame in enumerate(page.frames):
            name = ""
            with contextlib.suppress(Exception):
                name = frame.name or ""
            url = ""
            with contextlib.suppress(Exception):
                url = frame.url or ""
            frames.append({"index": index, "name": name, "url": url})

        return {"success": True, "frames": frames, "count": len(frames)}

    def _get_cookies_impl(self, urls: list[str] | None = None) -> dict[str, Any]:
        if self._context is None:
            msg = "Playwright browser context is not available"
            raise RuntimeError(msg)
        cookies = self._context.cookies(urls or None)
        return {"success": True, "cookies": cookies, "count": len(cookies)}

    def _add_cookies_impl(self, cookies: list[dict[str, Any]]) -> dict[str, Any]:
        if self._context is None:
            msg = "Playwright browser context is not available"
            raise RuntimeError(msg)
        self._context.add_cookies(cookies)
        return {"success": True, "count": len(cookies)}

    def _storage_state_impl(self, path: str = "") -> dict[str, Any]:
        if self._context is None:
            msg = "Playwright browser context is not available"
            raise RuntimeError(msg)

        output_path = ""
        kwargs: dict[str, Any] = {}
        if path:
            output_path = self._resolve_output_path(path, suffix=".json")
            kwargs["path"] = output_path

        state = self._context.storage_state(**kwargs)
        return {"success": True, "state": state, "path": output_path}

    def _start_tracing_impl(
        self,
        *,
        screenshots: bool = True,
        snapshots: bool = True,
        sources: bool = True,
    ) -> dict[str, Any]:
        if self._context is None:
            msg = "Playwright browser context is not available"
            raise RuntimeError(msg)
        if self._tracing_active:
            return {"success": False, "error": "Tracing is already active"}

        self._context.tracing.start(screenshots=screenshots, snapshots=snapshots, sources=sources)
        self._tracing_active = True
        return {"success": True}

    def _stop_tracing_impl(self, path: str = "") -> dict[str, Any]:
        if self._context is None:
            msg = "Playwright browser context is not available"
            raise RuntimeError(msg)
        if not self._tracing_active:
            return {"success": False, "error": "Tracing is not active"}

        output_path = self._resolve_output_path(path, suffix=".zip")
        self._context.tracing.stop(path=output_path)
        self._tracing_active = False
        return {"success": True, "path": output_path}

    def _get_network_events_impl(self, *, limit: int = 100, clear: bool = False) -> dict[str, Any]:
        limit = max(1, limit)
        events = list(self._network_events)[-limit:]
        if clear:
            self._network_events.clear()
        return {"success": True, "events": events, "count": len(events)}

    def _download_impl(
        self,
        selector: str,
        *,
        path: str = "",
        wait_timeout: int | None = None,
    ) -> dict[str, Any]:
        page = self._ensure_active_page_impl()
        timeout_ms = (wait_timeout or self._wait_timeout) * 1000
        locator = self._wait_for_locator_impl(selector, timeout_ms)

        with page.expect_download(timeout=timeout_ms) as download_info:
            locator.click(timeout=timeout_ms)

        download = download_info.value
        suggested_name = download.suggested_filename
        output_path = self._resolve_output_path(path, suffix=".bin", default_name=suggested_name if not path else None)
        download.save_as(output_path)
        self._wait_for_load_after_action_impl(timeout_ms)

        return {
            "success": True,
            "path": output_path,
            "suggested_filename": suggested_name,
            "url": page.url,
        }

    def _crawl_impl(
        self,
        start_url: str,
        *,
        max_pages: int = 10,
        max_depth: int = 2,
        url_filter: str | None = None,
        link_selector: str = "a[href]",
        extract_content: bool = True,
    ) -> dict[str, Any]:
        from urllib.parse import urlparse

        result = _empty_result(pages=[], total_pages=0)

        if url_filter is None:
            parsed = urlparse(start_url)
            url_filter = f"{parsed.scheme}://{parsed.netloc}"

        visited: set[str] = set()
        q: deque[tuple[str, int]] = deque([(start_url, 0)])
        pages: list[dict[str, Any]] = []

        while q and len(pages) < max_pages:
            current_url, depth = q.popleft()

            current_url = current_url.split("#")[0].rstrip("/")
            if current_url in visited:
                continue
            visited.add(current_url)

            fetch_result = self._fetch_impl(current_url)
            if not fetch_result["success"]:
                continue

            page_info: dict[str, Any] = {
                "url": current_url,
                "title": fetch_result["title"],
                "depth": depth,
                "links_found": 0,
            }

            if extract_content:
                page_info["content"] = fetch_result["content"]

            if depth < max_depth:
                links_result = self._extract_links_impl(
                    selector=link_selector,
                    base_url_filter=url_filter,
                    max_links=50,
                )
                if links_result["success"]:
                    page_info["links_found"] = links_result["count"]
                    for link in links_result["links"]:
                        link_url = link["url"].split("#")[0].rstrip("/")
                        if link_url not in visited and len(q) < max_pages * 2:
                            q.append((link_url, depth + 1))

            pages.append(page_info)
            self._wait_for_page_delay_impl(self._CRAWL_POLITENESS_DELAY)

        result["pages"] = pages
        result["total_pages"] = len(pages)
        result["success"] = True
        return result

    # ------------------------------------------------------------------
    # Public API — delegates to worker thread
    # ------------------------------------------------------------------

    def supports_advanced_session(self) -> bool:
        return True

    def warm_up(self) -> bool:
        try:
            self._start_worker()
        except RuntimeError:
            return False
        else:
            return True

    def fetch(self, url: str, *, quick: bool = False) -> dict[str, Any]:
        try:
            return self._run_in_worker(self._fetch_impl, url, quick=quick)
        except (_PlaywrightWorkerError, RuntimeError, TimeoutError) as e:
            result = _empty_result(url)
            result["error"] = f"Playwright error ({type(e).__name__}): {e}"
            logger.debug("PlaywrightFetcher error for {}: {}", url, result["error"])
            return result

    def fetch_with_wait(
        self,
        url: str,
        wait_for_selector: str | None = None,
        wait_timeout: int | None = None,
    ) -> dict[str, Any]:
        try:
            return self._run_in_worker(
                self._fetch_with_wait_impl,
                url,
                wait_for_selector,
                wait_timeout,
            )
        except (_PlaywrightWorkerError, RuntimeError, TimeoutError) as e:
            result = _empty_result(url)
            result["error"] = f"Playwright error ({type(e).__name__}): {e}"
            return result

    def click_element(self, selector: str, wait_timeout: int | None = None) -> dict[str, Any]:
        try:
            return self._run_in_worker(self._click_impl, selector, wait_timeout)
        except (_PlaywrightWorkerError, RuntimeError, TimeoutError) as e:
            result = _empty_result(clicked_text="")
            result["error"] = f"Click error ({type(e).__name__}): {e}"
            return result

    def fill_input(
        self,
        selector: str,
        value: str,
        *,
        submit: bool = False,
        clear_first: bool = True,
        wait_timeout: int | None = None,
    ) -> dict[str, Any]:
        try:
            return self._run_in_worker(
                self._fill_impl,
                selector,
                value,
                submit=submit,
                clear_first=clear_first,
                wait_timeout=wait_timeout,
            )
        except (_PlaywrightWorkerError, RuntimeError, TimeoutError) as e:
            result = _empty_result()
            result["error"] = f"Fill error ({type(e).__name__}): {e}"
            return result

    def extract_links(
        self,
        selector: str = "a[href]",
        *,
        base_url_filter: str | None = None,
        max_links: int = 50,
    ) -> dict[str, Any]:
        try:
            return self._run_in_worker(
                self._extract_links_impl,
                selector,
                base_url_filter=base_url_filter,
                max_links=max_links,
            )
        except (_PlaywrightWorkerError, RuntimeError, TimeoutError) as e:
            result = _empty_result(links=[], count=0)
            result["error"] = f"Extract links error ({type(e).__name__}): {e}"
            return result

    def execute_js(self, script: str) -> dict[str, Any]:
        try:
            return self._run_in_worker(self._execute_js_impl, script)
        except (_PlaywrightWorkerError, RuntimeError, TimeoutError) as e:
            result = _empty_result(return_value=None)
            result["error"] = f"JS execution error ({type(e).__name__}): {e}"
            return result

    def get_current_url(self) -> str:
        try:
            return self._run_in_worker(self._get_current_url_impl)
        except (_PlaywrightWorkerError, RuntimeError, TimeoutError):
            return ""

    def get_page_content(self) -> dict[str, Any]:
        try:
            return self._run_in_worker(self._get_page_content_impl)
        except (_PlaywrightWorkerError, RuntimeError, TimeoutError) as e:
            result = _empty_result()
            result["error"] = f"Get content error ({type(e).__name__}): {e}"
            return result

    def list_tabs(self) -> dict[str, Any]:
        try:
            return self._run_in_worker(self._list_tabs_impl)
        except (_PlaywrightWorkerError, RuntimeError, TimeoutError) as e:
            return {"success": False, "error": f"List tabs error ({type(e).__name__}): {e}", "tabs": [], "count": 0}

    def open_tab(
        self,
        url: str = "",
        *,
        wait_for_selector: str | None = None,
        background: bool = False,
    ) -> dict[str, Any]:
        try:
            return self._run_in_worker(
                self._open_tab_impl,
                url,
                wait_for_selector=wait_for_selector,
                background=background,
            )
        except (_PlaywrightWorkerError, RuntimeError, TimeoutError) as e:
            return {"success": False, "error": f"Open tab error ({type(e).__name__}): {e}"}

    def switch_tab(self, index: int) -> dict[str, Any]:
        try:
            return self._run_in_worker(self._switch_tab_impl, index)
        except (_PlaywrightWorkerError, RuntimeError, TimeoutError) as e:
            return {"success": False, "error": f"Switch tab error ({type(e).__name__}): {e}"}

    def close_tab(self, index: int | None = None) -> dict[str, Any]:
        try:
            return self._run_in_worker(self._close_tab_impl, index)
        except (_PlaywrightWorkerError, RuntimeError, TimeoutError) as e:
            return {"success": False, "error": f"Close tab error ({type(e).__name__}): {e}"}

    def screenshot(
        self,
        path: str = "",
        *,
        selector: str | None = None,
        full_page: bool = False,
    ) -> dict[str, Any]:
        try:
            return self._run_in_worker(self._screenshot_impl, path, selector=selector, full_page=full_page)
        except (_PlaywrightWorkerError, RuntimeError, TimeoutError) as e:
            return {"success": False, "error": f"Screenshot error ({type(e).__name__}): {e}"}

    def list_frames(self) -> dict[str, Any]:
        try:
            return self._run_in_worker(self._list_frames_impl)
        except (_PlaywrightWorkerError, RuntimeError, TimeoutError) as e:
            return {"success": False, "error": f"List frames error ({type(e).__name__}): {e}", "frames": [], "count": 0}

    def get_cookies(self, urls: list[str] | None = None) -> dict[str, Any]:
        try:
            return self._run_in_worker(self._get_cookies_impl, urls)
        except (_PlaywrightWorkerError, RuntimeError, TimeoutError) as e:
            return {
                "success": False,
                "error": f"Get cookies error ({type(e).__name__}): {e}",
                "cookies": [],
                "count": 0,
            }

    def add_cookies(self, cookies: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            return self._run_in_worker(self._add_cookies_impl, cookies)
        except (_PlaywrightWorkerError, RuntimeError, TimeoutError) as e:
            return {"success": False, "error": f"Add cookies error ({type(e).__name__}): {e}"}

    def storage_state(self, path: str = "") -> dict[str, Any]:
        try:
            return self._run_in_worker(self._storage_state_impl, path)
        except (_PlaywrightWorkerError, RuntimeError, TimeoutError) as e:
            return {"success": False, "error": f"Storage state error ({type(e).__name__}): {e}"}

    def start_tracing(
        self,
        *,
        screenshots: bool = True,
        snapshots: bool = True,
        sources: bool = True,
    ) -> dict[str, Any]:
        try:
            return self._run_in_worker(
                self._start_tracing_impl,
                screenshots=screenshots,
                snapshots=snapshots,
                sources=sources,
            )
        except (_PlaywrightWorkerError, RuntimeError, TimeoutError) as e:
            return {"success": False, "error": f"Start tracing error ({type(e).__name__}): {e}"}

    def stop_tracing(self, path: str = "") -> dict[str, Any]:
        try:
            return self._run_in_worker(self._stop_tracing_impl, path)
        except (_PlaywrightWorkerError, RuntimeError, TimeoutError) as e:
            return {"success": False, "error": f"Stop tracing error ({type(e).__name__}): {e}"}

    def get_network_events(self, *, limit: int = 100, clear: bool = False) -> dict[str, Any]:
        try:
            return self._run_in_worker(self._get_network_events_impl, limit=limit, clear=clear)
        except (_PlaywrightWorkerError, RuntimeError, TimeoutError) as e:
            return {
                "success": False,
                "error": f"Network events error ({type(e).__name__}): {e}",
                "events": [],
                "count": 0,
            }

    def download(
        self,
        selector: str,
        *,
        path: str = "",
        wait_timeout: int | None = None,
    ) -> dict[str, Any]:
        try:
            return self._run_in_worker(self._download_impl, selector, path=path, wait_timeout=wait_timeout)
        except (_PlaywrightWorkerError, RuntimeError, TimeoutError) as e:
            return {"success": False, "error": f"Download error ({type(e).__name__}): {e}"}

    def crawl(
        self,
        start_url: str,
        *,
        max_pages: int = 10,
        max_depth: int = 2,
        url_filter: str | None = None,
        link_selector: str = "a[href]",
        extract_content: bool = True,
    ) -> dict[str, Any]:
        try:
            return self._run_in_worker(
                self._crawl_impl,
                start_url,
                max_pages=max_pages,
                max_depth=max_depth,
                url_filter=url_filter,
                link_selector=link_selector,
                extract_content=extract_content,
            )
        except (_PlaywrightWorkerError, RuntimeError, TimeoutError) as e:
            result = _empty_result(pages=[], total_pages=0)
            result["error"] = f"Crawl error ({type(e).__name__}): {e}"
            return result

    def _force_close(self) -> None:
        logger.warning("PlaywrightFetcher: force-closing browser process")
        self._closed = True

        browser = self._browser_instance
        if browser is not None:
            killed = False
            proc = getattr(browser, "process", None)
            if proc is not None:
                with contextlib.suppress(Exception):
                    proc.kill()
                    killed = True

            if not killed:
                with contextlib.suppress(Exception):
                    browser.close()
                    killed = True

            if not killed:
                import signal

                pid: int | None = None
                with contextlib.suppress(Exception):
                    impl = getattr(browser, "_impl_obj", None)
                    if impl is not None:
                        bp = getattr(impl, "_browser_process", None)
                        if bp is not None:
                            pid = getattr(bp, "pid", None)
                if pid is not None:
                    with contextlib.suppress(Exception):
                        os.kill(pid, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
                        killed = True

            if not killed:
                logger.warning("PlaywrightFetcher: could not locate browser process to kill — resources may leak")

        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=5)
            if worker.is_alive():
                logger.warning(
                    "PlaywrightFetcher: worker thread still alive after "
                    "force-close — it will be abandoned as a daemon thread"
                )

        self._page = None
        self._pages = []
        self._context = None
        self._browser_instance = None
        self._playwright = None
        self._startup_error = "PlaywrightFetcher was force-closed"

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        if self._worker is not None and self._worker.is_alive():
            import queue as _queue_mod

            result_q: _queue_mod.Queue[Any] = _queue_mod.Queue()
            self._task_queue.put((None, (), {}, result_q))
            self._worker.join(timeout=10)

            if self._worker.is_alive():
                logger.warning("PlaywrightFetcher: worker did not exit within 10 s — force-closing browser process")
                self._force_close()
        else:
            self._close_browser()
