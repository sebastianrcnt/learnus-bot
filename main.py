import hashlib
import subprocess
import os
import logging
import threading
import time
import argparse
import dotenv

from typing import List
from dataclasses import dataclass
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from concurrent.futures import ThreadPoolExecutor
from rich.progress import Progress
from rich.logging import RichHandler

dotenv.load_dotenv(".env")

AUTH_INFO = {
    "username": os.environ.get("LEARNUS_USERNAME"),
    "password": os.environ.get("LEARNUS_PASSWORD"),
}

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.common.exceptions import (
    NoAlertPresentException,
    NoSuchElementException,
    UnexpectedAlertPresentException,
)

logger = logging.getLogger(__name__)
logger.addHandler(RichHandler(rich_tracebacks=True))
logger.setLevel("DEBUG")

# types
@dataclass
class Course:
    title: str
    link: str


@dataclass
class Vod:
    name: str
    link: str
    is_complete: bool


def delete_elements(driver, selector: str):
    driver.execute_script(
        f"""
        var elements = document.querySelectorAll('{selector}');
        for (var i = 0; i < elements.length; i++) {{
            elements[i].parentNode.removeChild(elements[i]);
        }}
    """
    )


def get_all_vods_under_course(driver, course: Course) -> List[Vod]:
    driver.get(course.link)
    vods: List[Vod] = []

    # get all elements with .course-box
    vod_elements = driver.find_elements(By.CSS_SELECTOR, ".vod.activity")

    for vod_element in vod_elements:
        # remove '.accesshide' element
        delete_elements(driver, ".accesshide")
        vod_name = vod_element.find_element(By.CSS_SELECTOR, "span.instancename").text
        vod_link = vod_element.find_element(By.CSS_SELECTOR, "a").get_attribute("href")

        try:
            vod_complete_icon = vod_element.find_element(By.CSS_SELECTOR, "img.icon")
            is_complete = vod_complete_icon.get_attribute("src").endswith(
                "completion-auto-y"
            )
            vods.append(
                Vod(
                    vod_name,
                    vod_link.replace("view.php", "viewer.php"), # view.php seems to be automatically redirected back to home page, so use viewer.php
                    is_complete,
                )
            )
            logger.debug(f"\tfound vod: {vod_name} at {vod_link}, is_complete: {is_complete}")
        except NoSuchElementException as e:
            ...

    # sometimes, the same vod is repeated, so do remove duplicates by using link as key
    vod_links_set = set()
    vods = [
        v for v in vods if not (v.link in vod_links_set or vod_links_set.add(v.link))
    ]

    return vods


def parse_time_to_secs(time_str: str) -> int:
    """splits the time string (hh:mm:ss, hh/mm is optional) and returns the time in seconds"""
    time_str_splitted = time_str.split(":")
    logger.info("timestr", time_str, time_str_splitted)

    if len(time_str_splitted == 1):
        return int(time_str_splitted[0])
    elif len(time_str_splitted == 2):
        return int(time_str_splitted[0]) * 60 + int(time_str_splitted[1])
    elif len(time_str_splitted == 3):
        return (
            int(time_str_splitted[0]) * 3600
            + int(time_str_splitted[1]) * 60
            + int(time_str_splitted[2])
        )
    else:
        raise ValueError(f"time_str is not in the correct format: {time_str}")


def _vod_get_current_progress(driver) -> float:
    progress_element = driver.find_element(
        By.CSS_SELECTOR, ".vjs-progress-control div.vjs-progress-holder"
    )
    value_now = progress_element.get_attribute("aria-valuenow")
    value_min = progress_element.get_attribute("aria-valuemin")
    value_max = progress_element.get_attribute("aria-valuemax")

    assert value_now is not None
    assert value_min is not None
    assert value_max is not None

    return (float(value_now) - float(value_min)) / (float(value_max) - float(value_min))


def _vod_set_to_highest_playback_rate(driver) -> float:
    # click the playback rate button
    _vjs_playback_rate_btn = driver.find_element(
        By.CSS_SELECTOR, "button.vjs-playback-rate"
    )
    _vjs_playback_rate_btn.click()
    # get the highest playback rate
    vjs_playback_rate_elements = driver.find_elements(
        By.CSS_SELECTOR,
        "div.vjs-playback-rate .vjs-menu .vjs-menu-item .vjs-menu-item-text",
    )
    vjs_playback_rate_elements[0].click()


def _vod_confirm_alert_if_exists(driver):
    try:
        alert = driver.switch_to.alert
        alert.accept()
        logger.info(f"alert: {alert.text}")
    except NoAlertPresentException:
        ...


def _vod_click_play_btn(driver) -> None:
    play_btn = driver.find_element(By.CSS_SELECTOR, "#my-video > button")
    play_btn.click()


def play_vod(driver, vod: Vod, course: Course, progress: Progress):
    driver.get(vod.link)
    time.sleep(1)
    _vod_confirm_alert_if_exists(driver)
    time.sleep(1)
    _vod_click_play_btn(driver)
    time.sleep(1)
    _vod_set_to_highest_playback_rate(driver)
    time.sleep(1)

    task1 = progress.add_task(
        f"at {threading.current_thread().name}>, playing vod {vod.name}", total=10000
    )

    while True:
        time.sleep(1)
        current_progress = _vod_get_current_progress(driver)
        progress.update(task1, completed=current_progress * 10000)

        if current_progress > 0.995:
            logger.info(f"reached 99.5% of the video, exiting...")
            break

    time.sleep(1)


def _vod_get_video_m3u8_link(driver, vod: Vod):
    try:
        driver.get(vod.link)
        time.sleep(3)
        m3u8_link = driver.find_element(By.CSS_SELECTOR, "video source").get_attribute(
            "src"
        )
    except UnexpectedAlertPresentException:
        _vod_confirm_alert_if_exists(driver)
        time.sleep(1)

    m3u8_link = driver.find_element(By.CSS_SELECTOR, "video source").get_attribute(
        "src"
    )
    return m3u8_link


def do_login(driver, username: str, password: str) -> None:
    # go to login page
    driver.get("https://ys.learnus.org/login/method/sso.php")

    # get the input element and type in the username
    username_btn = driver.find_element(By.CSS_SELECTOR, 'input[name="username"]')
    username_btn.send_keys(username)

    password_btn = driver.find_element(By.CSS_SELECTOR, 'input[name="password"]')
    password_btn.send_keys(password)

    login_btn = driver.find_element(By.CSS_SELECTOR, 'input[name="loginbutton"]')
    login_btn.click()

    time.sleep(3)


def get_all_courses(driver) -> List[Course]:
    course_boxes = driver.find_elements(By.CSS_SELECTOR, ".course-box")
    courses: List[Course] = []

    for course_box in course_boxes:
        course_link_element = course_box.find_element(By.CSS_SELECTOR, "a.course-link")
        course_link = course_link_element.get_attribute("href")
        assert course_link is not None

        delete_elements(driver, ".semester-name")
        course_title = course_box.find_element(By.CSS_SELECTOR, ".course-title h3").text
        courses.append(Course(course_title, course_link))

    return courses


def build_driver(headless: bool = True):
    options = FirefoxOptions()
    options.add_argument("--mute-audio")
    if headless:
        options.add_argument("--headless")
    driver = webdriver.Firefox(options=options)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


def play_vod_in_seperate_thread(course: Course, vod: Vod, progress: Progress, headless: bool):
    logger.info(f"playing vod: {vod.name} in a seperate thread, thread: {threading.current_thread().name}")
    logger.debug("building driver...")
    driver = build_driver(headless=headless)
    logger.debug("doing login...")
    do_login(driver, AUTH_INFO["username"], AUTH_INFO["password"])
    logger.debug(f"completed login")
    logger.debug(f"playing vod: {vod.name} at course {course.title}")
    play_vod(driver, vod, course, progress)
    logger.debug(f"completed playing vod: {vod.name}")
    driver.close()


def main(
    headless: bool = True,
    max_threads: int = 2,
    download: bool = False,
):
    try:
        result = subprocess.run(["pgrep -l firefox"], shell=True, check=True)
        logger.info(f"finding running firefox instances...: {result.stdout}")
        logger.info(f"killing all firefox instances...")
        result = subprocess.run(["pkill firefox"], shell=True, check=True)
        logger.info(f"result: {result.stdout}")
    except Exception as e:
        logger.warning(f"error while killing firefox: {e}")

    driver = build_driver(headless=headless)
    logger.info("doing login...")
    do_login(driver, AUTH_INFO["username"], AUTH_INFO["password"])
    logger.info("completed login")

    logger.info("getting all courses")
    courses = get_all_courses(driver)

    logger.info(f"found {len(courses)} courses")

    for i in range(len(courses)):
        course = courses[i]
        logger.debug(f"\tcourse<{i}>: {course.title}")
    
    non_completed_vods: List[Vod] = []
    all_vods: List[Vod] = []

    for course in courses:
        vods = get_all_vods_under_course(driver, course)
        for vod in vods:
            all_vods.append(vod)
            if not vod.is_complete:
                non_completed_vods.append(vod)

    logger.info(f"found {len(non_completed_vods)} non-completed vods, and {len(all_vods)} vods in total")

    if download:
        for vod in all_vods:
            logger.info(f"downloading vod: {vod.name}")
            m3u8_link = _vod_get_video_m3u8_link(driver, vod)
            filename = (
                hashlib.md5((course.title + vod.name).encode()).hexdigest() + ".mp4"
            )

            if os.path.exists(filename):
                logger.info(f"file already exists: {filename}")
                continue

            from m3u8downloader.main import M3u8Downloader

            downloader = M3u8Downloader(m3u8_link, filename, tempdir=".", poolsize=5)
            downloader.start()

    logger.info(f"closing driver")
    driver.close()


    logger.info(f"start playing non-completed vods")
    with Progress() as progress:
        with ThreadPoolExecutor(
            max_workers=max_threads, thread_name_prefix="vod-streaming-thread"
        ) as executor:
            futures = []
            for vod in non_completed_vods:
                future = executor.submit(
                    play_vod_in_seperate_thread, course, vod, progress, headless
                )
                futures.append(future)

            for future in futures:
                future.result()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Play VODs from learnus")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="run the browser in headless mode",
    )
    parser.add_argument(
        "--max-threads",
        type=int,
        default=2,
        help="maximum number of threads to use",
    )

    parser.add_argument(
        "--download",
        action="store_true",
        help="download the vods instead of playing them",
    )

    args = parser.parse_args()

    try:
        main(
            headless=args.headless,
            max_threads=args.max_threads,
            download=args.download,
        )
        os.system("pkill firefox")
    except KeyboardInterrupt as e:
        logger.info("exiting...")
        os.system("pkill firefox")
        raise e
