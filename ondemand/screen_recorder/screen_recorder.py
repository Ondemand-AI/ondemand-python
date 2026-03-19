"""Classes for recording screen via ScreenRecorder."""

import atexit
import base64
import datetime
import logging
import os
import queue
import threading
import time
from io import BytesIO
from typing import Any, Optional

from imageio import get_reader, get_writer
from numpy import array
from PIL import Image
from selenium.common.exceptions import InvalidSessionIdException, WebDriverException
from SeleniumLibrary.errors import NoOpenBrowser

from ondemand.screen_recorder.browser_manager import BrowserManager

logger = logging.getLogger(__name__)


class ScreenRecorder:
    """A class to record screen activity of a programmatic browser
    instance and generate a video file.

    The ScreenRecorder class facilitates the recording of screen activity
    from a programmatic browser session and generates a video file of the
    recorded session.

    Hardware requirements:
        The ScreenRecorder requires 1 dedicated thread running in the
        background for screen capturing and a second non-dedicated thread
        for screenshot processing.

    Known limitations:
        - Does not handle screen size changes, only keeps frames of
            the most common size.
        - Does not manage scenarios where a new BrowserManager instance
            must be created (e.g., instance is deleted).

    Future improvements:
        - Prevent calling start_recording if automatically_start_recording
            is True

    Example:
        from RPA.Browser.Selenium import Selenium  # installed separately
        from ondemand.screen_recorder import ScreenRecorder, BrowserManager

        class YoutubeScraper(ScreenRecorder):
            def __init__(self) -> None:
                self._selenium_instance = Selenium()
                super().__init__(browser_manager=BrowserManager(instance=self._selenium_instance))

        youtube_scraper = YoutubeScraper()
        ... Perform actions here ...
        youtube_scraper.end_recording()
    """

    def __init__(
        self,
        browser_manager: BrowserManager,
        disable_screen_recorder: bool = False,
        use_real_fps: bool = True,
        automatically_start_recording: bool = True,
        video_height: int = 1080,
        video_width: int = 1920,
        output_folder_path: str = "output",
        output_video_name: Optional[str] = None,
        single_host_thread: bool = True,
        # Extremely jank - temp solution for patching ffmpeg dependencies
        video_writer: Any = None,
    ) -> None:
        """Initialize the ScreenRecorder.

        Args:
            browser_manager (BrowserManager): Class providing a common
                interface for interacting with the browser.
            disable_screen_recorder (bool): Disables the ScreenRecorder when
                set to `True`. The intent of `disable_screen_recorder` is to
                make it easier for a user to enable/disable the ScreenRecorder
                for production runs.
            use_real_fps (bool): When recording, we are not able to reliably
                record a constant frame rate due to the time it takes for
                the BrowserManager to take a screenshot. This time is
                dependent on both the underlying hardware and the
                BrowserManager code itself. In order to maximize the number
                of screenshots we are able to take, we limit the logic
                controlling the taking of screenshots. As result, we have
                chosen not to add logic to granularly control the rate at
                which screenshots are taken and in turn have a dynamically
                determined fps that can only be calculated after all frames
                of the video have been taken.

                If `use_real_fps` is True, we will calculate
                self._real_fps after all video frames have been taken
                and adjust the video to utilize self._real_fps. Adjusting
                the video to utilize self._real_fps is a time intensive
                process (about 6 minutes of processing per 60 minutes
                recorded) - to speed up video recording, it is recommended
                to set `use_real_fps` to False.
                If `use_real_fps` is False, we will utilize self._baseline_fps
                which will result in the video being either shorter or longer
                than reality. If self._baseline_fps is lower than
                self._real_fps, the output video will be longer (slower)
                than reality. If self._baseline_fps is higher than
                self._real_fps, the output video will be shorter (faster)
                than reality.
            automatically_start_recording (bool): If True, the recording
                will automatically start as soon as a browser is detected in
                the BrowserManager instance
            video_height (int): Height of the output video. Defaults to 1080.
            video_width (int): Width of the output video. Defaults to 1920.
            output_folder_path (str): Path to folder to store output video file.
            output_video_name (Optional[str]): Name of the output video file.
            single_host_thread (bool): The program is being run with
                only one thread/vCPU. In this case, we cannot have a dedicated
                screen recording thread - we must instead periodically give
                access back to the scheduler to allow operations form other
                threads to occur.
        """
        self._browser_manager: BrowserManager = browser_manager
        self._disable_screen_recorder = disable_screen_recorder
        self._use_real_fps = use_real_fps
        self._video_width = video_width
        self._video_height = video_height
        self._single_host_thread = single_host_thread

        # Max number of connections for the driver urllib connections pool.
        # We need this number to be > 1 so that we can handle connections
        # from the main application and the screen recording thread.
        self._max_connections = 2

        self._name = self.__class__.__name__

        if output_video_name is None:
            # If output_video_name is not provided, construct a default name
            # using the child classes name and datetime
            current_datetime = datetime.datetime.now()
            formatted_timestamp_string = current_datetime.strftime("%Y-%m-%d_%H:%M:%S")
            output_video_name = f"{self._name}_{formatted_timestamp_string}_video"
        self._output_filepath_without_extension = os.path.join(
            output_folder_path, output_video_name
        )
        self._file_extension = "mp4"
        self._output_filepath: str = (
            f"{self._output_filepath_without_extension}.{self._file_extension}"
        )

        # Variables used during recording
        self._recording_thread: Optional[threading.Thread] = None
        self._add_frames_to_video_thread: Optional[threading.Thread] = None
        self._stop_capturing_frames_event = threading.Event()
        self._frame_queue = queue.Queue()
        self._recording_length: Optional[int] = None
        self._real_fps: Optional[float] = None
        self._recording_started = False
        self._end_recording_called = False

        # Define settings for video writer and create writer instance
        self._baseline_fps = 1
        self._quality = 5  # must be between 1 and 10
        self._bitrate = "500k"
        self._codec = "libx264"
        self._ffmpeg_log_level = "error"
        self._ffmpeg_params = ["-vf", f"scale={self._video_width}:{self._video_height}"]
        self._video_writer = video_writer

        if automatically_start_recording:
            self.start_recording()

        # For testing, there is no API for checking which callbacks have been
        # registered. In order to ensure this code is testable, we are using
        # this approach of adding all callbacks that we would like to register
        # to the `_atexit_callbacks` list first (which can be checked in a test)
        # and then registering all the entries in the list.
        self._atexit_callbacks = []
        if self._disable_screen_recorder is False:
            self._atexit_callbacks.append(self._check_if_end_recording_called)
        for callback in self._atexit_callbacks:
            atexit.register(callback)

    @property
    def is_recording(self) -> bool:
        """Check if the recording is currently in progress."""
        return self._recording_thread is not None and self._recording_thread.is_alive()

    def _start_capturing_frames(self) -> None:
        """
        Start the frame capturing thread and cleanup after recording has
        been ended.
        """
        start_time = None

        while not self._stop_capturing_frames_event.is_set():
            while self._browser_manager.is_browser_open() is False:
                if self._stop_capturing_frames_event.is_set():
                    raise RuntimeError(
                        "Unable to start capturing frames. " "Browser was not opened."
                    )
                time.sleep(0.3)  # Wait until driver is opened

            # Update connection pool to support multiple connections
            connection_pool_size = self._browser_manager.get_connection_pool_size()
            if (
                connection_pool_size is None
                or connection_pool_size < self._max_connections
            ):
                logger.info(
                    f"Updating connection pool size to {self._max_connections}."
                )
                self._browser_manager.update_connection_pool_size(
                    max_connections=self._max_connections
                )

            while self._browser_manager.has_page_loaded() is False:
                if self._stop_capturing_frames_event.is_set():
                    raise RuntimeError(
                        "Unable to start capturing frames. Page did not load."
                    )
                time.sleep(0.3)  # Wait until a page has loaded

            if start_time is None:
                start_time = time.time()
                logger.info(f"Beginning video recording for {self._name}.")

            try:
                base64_screenshot = self._browser_manager.get_base64_screenshot()
                self._frame_queue.put(base64_screenshot)
            except NoOpenBrowser:
                logger.warning("Skipping video frame due to no browser")
            except InvalidSessionIdException:
                logger.warning(
                    "Skipping video frame due to invalid session id. This "
                    "likely means the session crashed or was deleted."
                )
            except WebDriverException as e:
                logger.error(
                    f"Unexpected WebDriverException occurred. "
                    f"Skipping frame. Error: {e}"
                )
            if self._single_host_thread:
                # If we only have 1 executing thread/vCPU, we must
                # periodically return access to the scheduler so
                # other threads can perform operations such as
                # signaling to this thread that it should terminate
                time.sleep(0.3)  # Cede control to other threads

        end_time = time.time()
        self._recording_length = end_time - start_time
        logger.info(f"Recording finished in {self._recording_length:.2f} seconds")

    def _start_adding_frames_to_video(self) -> None:
        logger.info(f"Starting to add frames to video for {self._name}.")
        frames_captured = 0
        while self._recording_thread.is_alive() or self._frame_queue.empty() is False:
            base64_screenshot = self._frame_queue.get()
            decoded_base64_screenshot = base64.b64decode(base64_screenshot)
            img = Image.open(BytesIO(decoded_base64_screenshot))
            resized_img = img.resize(
                size=(self._video_width, self._video_height),
                resample=Image.Resampling.BILINEAR,
            )
            ndarray_screenshot = array(resized_img)
            try:
                self._video_writer.append_data(ndarray_screenshot)
                frames_captured += 1

                if frames_captured % 100 == 0:
                    logger.info(f"{frames_captured} video frames written.")
            except ValueError as e:
                logger.warning(f"Invalid frame detected. Skipping frame. Error: {e}")

            # Sleep to allow other threads to get prioritized while waiting
            # additional frames to get added to the queue
            time.sleep(0.5)

        logger.info("All captured frames have been added to the output video.")

        if frames_captured == 0:
            raise RuntimeError("No frames have been added to the output video.")
        if self._recording_length is None:
            logger.warning(
                f"We managed to capture {frames_captured} video frames but "
                "recording length was not calculated. This implies that the "
                "'_start_capturing_frames' thread has not completed."
            )
        else:
            self._real_fps = frames_captured / self._recording_length
            logger.info(
                f"We managed to capture {frames_captured} video frames over "
                f"{self._recording_length:.2f} seconds which translates to "
                f"{self._real_fps:.2f} frames captured per second."
            )

    def _resize_video_to_use_real_frame_rate(self) -> None:
        """
        Update the video to use the real frame rate instead of the baseline
        frame rate.
        """
        if self._real_fps is None:
            raise ValueError("Invalid real_fps. Cannot update video fps to real fps.")

        logger.info(
            f"Updating frame rate from {self._baseline_fps} fps to"
            f" {self._real_fps} fps."
        )

        try:
            tmp_filename = (
                f"{self._output_filepath_without_extension}_tmp."
                f"{self._file_extension}"
            )
            reader = get_reader(self._output_filepath)
            writer = get_writer(
                tmp_filename,
                fps=self._real_fps,
                codec=self._codec,
                quality=self._quality,
                bitrate=self._bitrate,
                ffmpeg_log_level=self._ffmpeg_log_level,
                ffmpeg_params=self._ffmpeg_params,
            )
            for frame in reader:
                writer.append_data(frame)
            writer.close()
            logger.info("Frame rate updated successfully.")

            original_file_size = os.path.getsize(self._output_filepath)
            new_file_size = os.path.getsize(tmp_filename)
            logger.info(
                f"Original video file size: {original_file_size} bytes. "
                f"Updated video file size: {new_file_size} bytes."
            )

            # Rename old file instead of deleting for now. We want to
            # delete the file as the very last step in the process incase
            # there are any failures in the intermediary steps.
            tmp_filename_old_video = (
                f"{self._output_filepath_without_extension}_tmp_old."
                f"{self._file_extension}"
            )
            os.rename(self._output_filepath, tmp_filename_old_video)
            logger.info(
                f"Renamed old video file from {self._output_filepath} to "
                f"{tmp_filename_old_video}."
            )

            os.rename(tmp_filename, self._output_filepath)
            logger.info(
                f"Renamed new video file from {tmp_filename} to "
                f"{self._output_filepath}."
            )

            os.remove(tmp_filename_old_video)
            logger.info(f"Deleted old video file ({tmp_filename_old_video}).")

        except Exception as e:
            logger.error(f"Unable to update video frame rate: {e}")

    def _check_if_end_recording_called(self) -> None:
        if self._recording_started and not self._end_recording_called:
            raise RuntimeError(
                "'end_recording' not called but main thread has terminated."
            )

    def start_recording(self) -> None:
        """Start the recording thread."""
        if self._disable_screen_recorder:
            logger.info(
                f"ScreenRecorder for {self._name} is disabled. Recording "
                f"will not start. If you would like to enable recording, "
                f"update the `disable_screen_recorder` value to be `False`."
            )
            return

        self._recording_started = True

        # When instantiating the video_writer instance, the video_writer
        # opens up the video file for writing as part of initialization.
        # Because of this, we wait until the time of recording to initialize
        # the video_writer.
        if self._video_writer is None:
            self._video_writer = get_writer(
                self._output_filepath,
                fps=self._baseline_fps,
                codec=self._codec,
                quality=self._quality,
                bitrate=self._bitrate,
                ffmpeg_log_level=self._ffmpeg_log_level,
                ffmpeg_params=self._ffmpeg_params,
            )

        self._recording_thread = threading.Thread(
            target=self._start_capturing_frames,
            # Daemon thread will not block interpreter from exiting
            daemon=True,
        )
        self._recording_thread.start()
        self._add_frames_to_video_thread = threading.Thread(
            target=self._start_adding_frames_to_video,
            # Daemon thread will not block interpreter from exiting
            daemon=True,
        )
        self._add_frames_to_video_thread.start()
        # TODO: Make this sleep statement more robust
        # Wait a moment for thread to start up before returning control to main
        # We want the video recording thread to fully initialize before we
        # allow the main thread to begin performing operations with the
        # BrowserManager
        time.sleep(1)

    def end_recording(self) -> None:
        """Facilitates completion of recording and any cleanup."""

        if self._disable_screen_recorder:
            logger.info(
                f"ScreenRecorder for {self._name} is disabled. Recording "
                f"cannot be ended because it was never started. If you would "
                f"like the enable recording, update the "
                f"disable_screen_recorder` value to be `False`."
            )
            return

        self._end_recording_called = True
        logger.info(f"Ending recording for {self._name}.")

        if self.is_recording:
            self._stop_capturing_frames_event.set()

            if self._recording_thread:
                logger.info("Blocking until recording thread terminates.")
                self._recording_thread.join()
            else:
                logger.warning(
                    "Recording thread is not defined. Unable to "
                    "wait for thread termination as result."
                )
        else:
            logger.error(
                "Attempted to end recording, but no recording is in progress. "
                "The recording thread may have already been terminated."
            )

        if self._add_frames_to_video_thread:
            logger.info("Blocking until add_frames_to_video thread terminates.")
            self._add_frames_to_video_thread.join()
        else:
            logger.warning(
                "Add frames to video thread is not defined. Unable"
                "to wait for thread termination as result."
            )

        logger.info("Supporting threads are no longer running.")

        if self._video_writer:
            self._video_writer.close()
            logger.info("Video writing has completed.")
        else:
            logger.warning("Video writer is not defined. Unable to close video.")

        if self._use_real_fps:
            try:
                self._resize_video_to_use_real_frame_rate()
            except Exception as e:
                logger.error(f"Unable to resize video frame rate: {e}")

        try:
            # Verify video was successfully created
            file_size = os.path.getsize(self._output_filepath)
            logger.info(
                f"Video recording saved to {self._output_filepath}. "
                f"Video file size: {file_size} bytes."
            )
        except (FileNotFoundError, OSError):
            logger.error(f"Output video file does not exist at {self._output_filepath}")
