"""Classes for managing a browser via BrowserManager."""

import logging
from enum import Enum
from typing import Optional

from selenium.common.exceptions import InvalidSessionIdException
from selenium.common.exceptions import UnexpectedAlertPresentException
from selenium.common.exceptions import WebDriverException
from SeleniumLibrary import SeleniumLibrary
from SeleniumLibrary.errors import NoOpenBrowser

logger = logging.getLogger(__name__)


class BrowserManager:
    """A class for providing a common interface for the ScreenRecorder
    class to utilize when interacting with a browser
    """

    class BrowserManagerType(Enum):
        Selenium = "SELENIUM"

    def __init__(self, instance: SeleniumLibrary):
        """Initialize the ScreenRecorder.

        Args:
            instance (SeleniumLibrary): Instance that provides access to
            control the browser. Browser operations should be performable
            on this instance.

        Example:
            from RPA.Browser.Selenium import Selenium  # installed separately
            from ondemand.screen_recorder import BrowserManager

            selenium_instance = Selenium()
            browser_manager = BrowserManager(instance=selenium_instance)
        """
        self._instance: SeleniumLibrary = instance

        if isinstance(self._instance, SeleniumLibrary):
            self._browser_manager_type = self.BrowserManagerType.Selenium
        else:
            raise ValueError("Invalid BrowserManager instance provided.")

    def get_connection_pool_size(self) -> Optional[int]:
        try:
            return self._instance.driver.command_executor._conn.connection_pool_kw.get(
                "maxsize"
            )
        except NoOpenBrowser:
            raise RuntimeError(
                "Unable to retrieve connection pool size because a "
                "driver/connection pool has not yet been created."
            )

    def update_connection_pool_size(self, max_connections: int):
        """
        Updates the maximum size of the Selenium WebDriver's connection pool
        to allow for concurrent operations.

        This function is crucial for enabling simultaneous actions:
        - It allows taking screenshots on one connection while
        - Navigating the web page or performing other actions on the
            main thread.

        The function modifies the 'maxsize' parameter of the connection pool
        and then clears the existing pool to apply the new size.

        Key points:

        1. The default maxsize for the connection pool is 1 by default,
            which doesn't allow for concurrent operations.
        2. Increasing the pool size enables multiple concurrent connections,
            necessary for parallel actions like screenshots and page navigation.
        3. The function calls clear() on the connection pool, which according
            to the urllib3 documentation:
                Empty our store of pools and direct them all to close.
                This will not affect in-flight connections, but they will not
                be re-used after completion.
        """
        try:
            self._instance.driver.command_executor._conn.connection_pool_kw[
                "maxsize"
            ] = max_connections
            logger.info("Clearing existing connection pool to apply new pool size.")
            self._instance.driver.command_executor._conn.clear()
        except NoOpenBrowser:
            raise RuntimeError(
                "Unable to update connection pool size because a "
                "driver/connection pool has not yet been created."
            )

    def is_browser_open(self) -> bool:
        """Check if the browser is open."""
        if self._browser_manager_type == self.BrowserManagerType.Selenium:
            try:
                # Call driver getter - it will throw an error if the driver
                # is not yet opened
                self._instance.driver
                return True
            except NoOpenBrowser:
                return False
            except InvalidSessionIdException:
                # The browser session is not currently active - this likely
                # means the session crashed or was deleted. We should
                # return `False` to indicate that the page is not in a
                # valid `loaded` state.
                return False
        else:
            raise ValueError("Invalid BrowserManager instance provided.")

    def has_page_loaded(self) -> bool:
        """Check if the browser window is loaded."""
        if self._browser_manager_type == self.BrowserManagerType.Selenium:
            try:
                return self._instance.driver.current_url != "data:,"
            except UnexpectedAlertPresentException:
                # Thrown when an unexpected alert has appeared.
                # Usually raised when an unexpected modal is blocking the
                # webdriver from executing commands.
                # We return True here because a model appearing would
                # indicate that content has loaded on the page.
                return True
            except InvalidSessionIdException:
                # The browser session is not currently active - this likely
                # means the session crashed or was deleted. We should
                # return `False` to indicate that the page is not in a
                # valid `loaded` state.
                return False
            except WebDriverException as e:
                # Catch-all WebDriver exception that we default to if specific
                # exceptions are not detected
                logger.warning(
                    f"An unexpected WebDriverException occurred "
                    f"and was handled: {e}."
                )
                return False

        else:
            raise ValueError("Invalid BrowserManager instance detected.")

    def get_base64_screenshot(self) -> str:
        """Take screenshot and output as base64 string."""
        if self._browser_manager_type == self.BrowserManagerType.Selenium:
            base64_screenshot = self._instance.driver.get_screenshot_as_base64()
        else:
            raise ValueError("Invalid BrowserManager instance provided.")
        return base64_screenshot
